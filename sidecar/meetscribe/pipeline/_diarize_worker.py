"""화자분리 subprocess 진입점 (적대검증 P0 처방 ③ — 프로세스 격리).

장시간(2~3h) 회의에서 pyannote 화자분리가 메모리를 O(n²)로 폭발시킨다
(3h/23화자 18GB+). ``torch.cuda.empty_cache()`` 로는 프로세스가 잡은
GPU/RAM 단편화·드라이버 누수를 못 푼다. 진짜 방어는 diarize 단계를 **이
모듈을 별도 프로세스로 띄워** 돌리고, 끝나면 부모(runner)가 프로세스째 죽여
OS 가 메모리를 완전히 회수하게 하는 것이다.

실행 방법(부모가 ``subprocess.Popen`` 으로 호출)::

    python -m meetscribe.pipeline._diarize_worker

입출력 프로토콜(부모 ``runner._run_diarize_subprocess`` 와 1:1 계약):

- **입력**: stdin 으로 JSON 한 줄(payload)을 받는다. argv 로 파일 경로를
  넘길 수도 있다(``--payload-file <path>``; Windows 등에서 stdin 이 곤란할 때).
  payload 스키마::

      {
        "wav_path": str,                  # 16kHz mono WAV
        "min_speakers": int | null,
        "max_speakers": int | null,
        "cfg": { ...직렬화된 필요 필드만... }  # _serialize_cfg 참조
      }

- **출력(결과)**: 성공 시 stdout 에 ``list[DiarizationTurn]`` 을 JSON **한 줄**로
  출력한다(각 turn 은 ``model_dump()`` dict). 부모는 이 한 줄만 파싱한다.
- **진행률**: stderr 에 ``PROGRESS <local 0~1> <msg>`` 라인 프로토콜로 흘린다.
  (stdout 은 결과 JSON 전용이라 진행률을 섞지 않는다.)
- **오류**: 실패 시 stderr 에 ``ERROR <message>`` 한 줄 + 비정상 종료코드(1).

무거운 import(``pyannote``·``torch``)는 ``diarize.diarize()`` 내부에서 이미
lazy import 되므로, 이 모듈 최상단에서는 표준 라이브러리와 ``meetscribe.*`` 만
import 한다(미설치 환경 ``py_compile`` 안전).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# meetscribe.* 는 ML 의존성이 없으므로 최상단 import 안전(설계 계약).
from ..config import AppConfig
from ..schemas import ComputeType, Device, JobStage


# 부모(runner)와 공유하는 진행률 라인 접두사. 양쪽이 같은 상수를 봐야 하므로
# runner 에서 이 이름을 import 해 쓴다(프로토콜 단일 출처).
PROGRESS_PREFIX = "PROGRESS"
ERROR_PREFIX = "ERROR"


# ─────────────────────────────────────────────────────────────
# cfg 직렬화/역직렬화 (Path/Enum → JSON 안전)
# ─────────────────────────────────────────────────────────────
def serialize_cfg(cfg: AppConfig) -> dict:
    """diarize 에 필요한 cfg 필드만 JSON 안전한 dict 로 직렬화한다.

    경계 원칙: subprocess 에는 **화자분리에 필요한 최소 필드**만 넘긴다.
    Path 는 문자열로, Enum 은 ``.value`` 문자열로 변환해 JSON 직렬화가
    절대 깨지지 않게 한다(부모/자식 모두 표준 json 만 사용).

    diarize 경로가 실제로 읽는 필드:
      - hf_token, torch_device(=device), diarization_chunk_sec
    함께 넘기는 필드(향후 diarize 가 참조하거나 AppConfig 복원 일관성용):
      - model, language, compute_type, batch_size, align_chunk_sec,
        sequential_load, cache_dir
    """
    return {
        "model": cfg.model,
        "language": cfg.language,
        # Enum → 문자열 값("cuda"/"cpu", "float16" 등). 역직렬화 때 Enum 으로 복원.
        "device": cfg.device.value,
        "compute_type": cfg.compute_type.value,
        "batch_size": cfg.batch_size,
        "diarization_chunk_sec": cfg.diarization_chunk_sec,
        "align_chunk_sec": cfg.align_chunk_sec,
        "sequential_load": cfg.sequential_load,
        # gated 모델 토큰(없으면 자식이 명확히 실패).
        "hf_token": cfg.hf_token,
        # Path → 문자열. 역직렬화 때 Path 로 복원.
        "cache_dir": str(cfg.cache_dir),
    }


def deserialize_cfg(data: dict) -> AppConfig:
    """serialize_cfg 가 만든 dict 를 AppConfig 로 복원한다(자식 프로세스 측).

    config.py 는 수정 금지(읽기 전용)이므로, 여기서 dict → 생성자 인자로
    매핑해 AppConfig 를 직접 구성한다. 누락 필드는 AppConfig 기본값에 맡긴다
    (방어적: 부모/자식 버전이 어긋나도 최대한 복원).
    """
    kwargs: dict = {}

    if "model" in data:
        kwargs["model"] = data["model"]
    if "language" in data:
        kwargs["language"] = data["language"]
    if data.get("device") is not None:
        # 문자열 값 → Device Enum. 알 수 없는 값이면 기본 감지에 맡김(키 생략).
        try:
            kwargs["device"] = Device(data["device"])
        except ValueError:
            pass
    if data.get("compute_type") is not None:
        try:
            kwargs["compute_type"] = ComputeType(data["compute_type"])
        except ValueError:
            pass
    if "batch_size" in data:
        kwargs["batch_size"] = data["batch_size"]
    if "diarization_chunk_sec" in data:
        kwargs["diarization_chunk_sec"] = data["diarization_chunk_sec"]
    if "align_chunk_sec" in data:
        kwargs["align_chunk_sec"] = data["align_chunk_sec"]
    if "sequential_load" in data:
        kwargs["sequential_load"] = data["sequential_load"]
    if "hf_token" in data:
        kwargs["hf_token"] = data["hf_token"]
    if data.get("cache_dir") is not None:
        # 문자열 → Path 복원.
        kwargs["cache_dir"] = Path(data["cache_dir"])

    return AppConfig(**kwargs)


# ─────────────────────────────────────────────────────────────
# 진행률 / 오류 라인 출력 (stderr)
# ─────────────────────────────────────────────────────────────
def _emit_progress(local: float, message: str) -> None:
    """``PROGRESS <local> <msg>`` 한 줄을 stderr 로 흘린다(즉시 flush).

    부모는 stderr 를 줄 단위로 읽어 이 라인을 파싱→progress 콜백으로 중계한다.
    local 은 0~1 로 클램프(부모의 단계 가중치 계산을 깨지 않게).
    """
    try:
        clamped = min(1.0, max(0.0, float(local)))
    except (TypeError, ValueError):
        clamped = 0.0
    # 메시지에 줄바꿈이 섞이면 프로토콜이 깨지므로 공백으로 치환.
    safe_msg = str(message).replace("\n", " ").replace("\r", " ")
    sys.stderr.write(f"{PROGRESS_PREFIX} {clamped:.4f} {safe_msg}\n")
    sys.stderr.flush()


def _emit_error(message: str) -> None:
    """``ERROR <msg>`` 한 줄을 stderr 로 흘린다(즉시 flush)."""
    safe_msg = str(message).replace("\n", " ").replace("\r", " ")
    sys.stderr.write(f"{ERROR_PREFIX} {safe_msg}\n")
    sys.stderr.flush()


def _progress_adapter(stage: JobStage, local: float, message: str) -> None:
    """diarize 가 호출하는 ``ProgressCb`` → stderr PROGRESS 라인 어댑터.

    diarize 는 ``(JobStage, local, msg)`` 로 콜백하지만, 자식은 단계(stage)를
    알 필요가 없다(부모가 항상 DIARIZE 단계로 매핑). 그래서 local·msg 만 흘린다.
    """
    _emit_progress(local, message)


# ─────────────────────────────────────────────────────────────
# payload 입력
# ─────────────────────────────────────────────────────────────
def _read_payload(argv: list[str]) -> dict:
    """payload(JSON)를 읽는다. ``--payload-file <path>`` 우선, 없으면 stdin.

    - argv 에 ``--payload-file <path>`` 가 있으면 그 파일을 읽는다(대용량/인코딩
      안전, Windows 에서 stdin 파이프가 곤란할 때 부모가 선택).
    - 그 외에는 stdin 전체를 읽어 JSON 으로 파싱한다.
    """
    payload_file: str | None = None
    for i, arg in enumerate(argv):
        if arg == "--payload-file" and i + 1 < len(argv):
            payload_file = argv[i + 1]
            break

    if payload_file:
        # BOM 없는 UTF-8 가정(부모가 그렇게 쓴다). utf-8-sig 로 BOM 도 흡수.
        text = Path(payload_file).read_text(encoding="utf-8-sig")
    else:
        text = sys.stdin.read()

    return json.loads(text)


# ─────────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────────
def run(argv: list[str] | None = None) -> int:
    """worker 본체. payload 를 읽어 diarize 실행 → 결과 JSON 을 stdout 으로 출력.

    Returns:
        종료 코드(0=성공, 1=실패). 부모는 종료 코드로 성공/실패를 판정한다.
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        payload = _read_payload(argv)
    except Exception as exc:  # noqa: BLE001 — 입력 파싱 실패는 ERROR 로 알림
        _emit_error(f"payload 파싱 실패: {exc}")
        return 1

    try:
        wav_path = payload["wav_path"]
        min_speakers = payload.get("min_speakers")
        max_speakers = payload.get("max_speakers")
        cfg = deserialize_cfg(payload.get("cfg", {}))
    except Exception as exc:  # noqa: BLE001
        _emit_error(f"payload 필드 오류: {exc}")
        return 1

    # diarize 는 무거운 import(pyannote·torch)를 함수 내부에서 lazy 로 한다.
    try:
        # torch 2.6+ weights_only 호환 셔틀(pyannote 체크포인트 로드). subprocess 는
        # 깨끗한 프로세스라 speechbrain 오염은 없지만 weights_only 는 여기서도 필요.
        from ._compat import ensure_speechbrain_compat, ensure_torch_load_compat

        ensure_torch_load_compat()
        ensure_speechbrain_compat()  # 깨끗한 subprocess + k2_fsa 스텁 → speechbrain 순회 통과
        # __init__ 이 동명 함수로 모듈을 가리므로(`from . import diarize` 는 함수가 잡힘)
        # importlib 로 모듈 객체를 직접 확보한다(runner 와 동일한 회피).
        import importlib

        diarize_mod = importlib.import_module("meetscribe.pipeline.diarize")

        _emit_progress(0.0, "subprocess 화자분리 시작")
        turns = diarize_mod.diarize(
            wav_path,
            cfg,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            progress=_progress_adapter,
        )
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 부모에 ERROR 로 전달
        _emit_error(f"화자분리 실패: {exc}")
        return 1

    # 결과를 stdout 에 JSON 한 줄로. ensure_ascii=False(한글 보존), BOM 없음.
    try:
        out = json.dumps([t.model_dump() for t in turns], ensure_ascii=False)
        sys.stdout.write(out)
        sys.stdout.write("\n")
        sys.stdout.flush()
    except Exception as exc:  # noqa: BLE001
        _emit_error(f"결과 직렬화 실패: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    # 모듈 실행(python -m meetscribe.pipeline._diarize_worker)의 표준 진입점.
    raise SystemExit(run())
