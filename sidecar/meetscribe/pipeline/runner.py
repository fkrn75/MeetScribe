"""파이프라인 오케스트레이션 — 전 단계를 순서대로 조립 + 장시간 안정성 처방.

CONTRACT T1 의 ``run_pipeline`` 진입점. 단계 순서:

    preprocess → transcribe → align → diarize → merge → TranscriptionResult

장시간(1h+) 안정성 처방(설계서 §10 적대검증, team-lead 지시):
  ① 청크 단위 화자분리/정렬 — 각 단계 모듈이 cfg.*_chunk_sec 로 자체 처리.
  ② 단계별 체크포인트 — 중간 산출을 임시 JSON 으로 저장해 재개(resume) 가능.
  ③ should_cancel() 주기 확인 — 단계 경계마다 검사, 신호 시 즉시 중단·정리.
  ④ 모델 해제 + torch.cuda.empty_cache() — 각 단계 종료 후(cfg.sequential_load).
  ⑤ 임시 WAV 정리 — finally 에서 항상 제거.
  ⑥ 프로세스 격리(P0 처방 ③) — cfg.diarize_in_subprocess 가 True 면 화자분리를
     별도 subprocess 로 돌려, 단계 종료 시 프로세스째 죽여 OS 가 GPU/RAM 을 완전
     회수하게 한다. empty_cache() 로는 못 푸는 메모리 단편화·드라이버 누수까지
     차단. 기본 False(in-process) → 기존 동작 불변(회귀 안전 불변식).

무거운 import(torch)는 메모리 회수 함수 안에서만 lazy import.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..config import AppConfig
from ..schemas import (
    DiarizationTurn,
    JobStage,
    Segment,
    TranscriptionResult,
)
# 패키지 __init__ 이 `from .align import align` 로 모듈을 동명 함수로 가린다.
# 그래서 `from . import align as align_mod` 도, `import meetscribe.pipeline.align as
# align_mod` 도 (둘 다 getattr(pipeline, 'align') 경로라) 함수를 잡아
# 'function' object has no attribute 'align' 이 난다. importlib.import_module 은
# sys.modules 의 모듈 객체를 직접 반환하므로 가림과 무관하게 항상 모듈을 보장한다.
import importlib

align_mod = importlib.import_module("meetscribe.pipeline.align")
diarize_mod = importlib.import_module("meetscribe.pipeline.diarize")
merge_mod = importlib.import_module("meetscribe.pipeline.merge")
preprocess_mod = importlib.import_module("meetscribe.pipeline.preprocess")
transcribe_mod = importlib.import_module("meetscribe.pipeline.transcribe")
from ._diarize_worker import (
    ERROR_PREFIX,
    PROGRESS_PREFIX,
    serialize_cfg,
)

logger = logging.getLogger(__name__)

# 진행률 콜백 타입(문서용). CONTRACT: Callable[[JobStage, float, str], None]
ProgressCb = Callable[[JobStage, float, str], None]
CancelCb = Callable[[], bool]


class PipelineCancelled(Exception):
    """should_cancel() 신호로 파이프라인이 중단되었음을 알리는 예외."""


def run_pipeline(
    audio_path: str,
    cfg: AppConfig,
    progress: ProgressCb | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    should_cancel: CancelCb | None = None,
) -> TranscriptionResult:
    """오디오 한 건을 끝까지 처리해 TranscriptionResult 를 반환한다.

    Args:
        audio_path: 원본 미디어 경로(any 포맷).
        cfg: 런타임 설정(SSOT). 이 함수는 cfg 를 수정하지 않는다.
        progress: 단계·로컬진행률(0~1)·메시지 콜백(선택).
        min_speakers / max_speakers: 화자 수 힌트(선택).
        should_cancel: 주기적으로 호출해 True 면 즉시 중단(선택).

    Returns:
        TranscriptionResult(language·duration·segments·speakers·audio_path).

    Raises:
        PipelineCancelled: 취소 신호를 받은 경우.
        FileNotFoundError / RuntimeError: 입력 부재·ffmpeg/모델 실패 등.
    """
    started = time.monotonic()
    # torch 2.6+ 는 torch.load(weights_only=True) 가 기본 → pyannote/whisperx 의
    # VAD·화자분리 체크포인트(omegaconf 등 비텐서 객체 포함) 로드가 UnpicklingError 로
    # 깨진다. HF 공식(신뢰) 모델이므로 weights_only=False 로 되돌리는 셔틀을 1회 적용.
    from ._compat import ensure_speechbrain_compat, ensure_torch_load_compat
    ensure_torch_load_compat()
    ensure_speechbrain_compat()
    # 체크포인트·임시 WAV 를 모아 둘 작업 디렉토리(작업별 격리, 끝나면 정리).
    work_dir = Path(tempfile.mkdtemp(prefix="meetscribe_job_"))
    wav_path: str | None = None

    try:
        _check_cancel(should_cancel)

        # ── 1) 전처리: any → 16kHz mono WAV ──────────────────────────────
        if progress:
            progress(JobStage.PREPROCESS, 0.0, "오디오 전처리 중")
        wav_path = preprocess_mod.to_wav(audio_path, str(work_dir / "audio_16k.wav"))
        if progress:
            progress(JobStage.PREPROCESS, 1.0, "전처리 완료")
        _check_cancel(should_cancel)

        # ── 2) STT(faster-whisper) ───────────────────────────────────────
        segments, language = _stage_transcribe(wav_path, cfg, progress, work_dir, should_cancel)
        _check_cancel(should_cancel)

        # ── 3) 정렬(whisperx wav2vec2) — 실패해도 graceful degrade ────────
        segments = _stage_align(
            segments, wav_path, language, cfg, progress, work_dir, should_cancel
        )
        _check_cancel(should_cancel)

        # ── 4) 화자분리(pyannote, 청크) ──────────────────────────────────
        turns = _stage_diarize(
            wav_path, cfg, min_speakers, max_speakers, progress, work_dir, should_cancel
        )
        _check_cancel(should_cancel)

        # ── 5) 병합(화자 부여) ───────────────────────────────────────────
        if progress:
            progress(JobStage.MERGE, 0.0, "화자 병합 중")
        segments = merge_mod.assign_speakers(segments, turns)
        if progress:
            progress(JobStage.MERGE, 1.0, "화자 병합 완료")

        duration = _result_duration(segments, wav_path)
        speakers = _collect_speakers(segments, turns)

        result = TranscriptionResult(
            language=language,
            duration=duration,
            segments=segments,
            speakers=speakers,
            audio_path=audio_path,
        )

        elapsed = time.monotonic() - started
        logger.info(
            "파이프라인 완료: %.1fs, 세그먼트 %d, 화자 %d",
            elapsed,
            len(segments),
            len(speakers),
        )
        if progress:
            progress(JobStage.DONE, 1.0, "완료")
        return result

    finally:
        # ⑤ 임시 WAV·체크포인트·작업 디렉토리 정리(취소·실패 시에도 항상).
        _cleanup_dir(work_dir)


# ─────────────────────────────────────────────────────────────
# 단계 래퍼 — 체크포인트 저장 + 단계 종료 후 메모리 회수
# ─────────────────────────────────────────────────────────────
def _stage_transcribe(
    wav_path: str,
    cfg: AppConfig,
    progress: ProgressCb | None,
    work_dir: Path,
    should_cancel: CancelCb | None,
) -> tuple[list[Segment], str]:
    """STT 단계. 체크포인트가 있으면 재사용, 끝나면 모델 메모리 회수."""
    ckpt = work_dir / "01_transcribe.json"
    cached = _load_segments_checkpoint(ckpt)
    if cached is not None:
        logger.info("STT 체크포인트 재사용: %s", ckpt)
        return cached.segments, cached.language

    try:
        segments, language = transcribe_mod.transcribe(wav_path, cfg, progress)
    finally:
        # ④ 단계 종료 후 GPU 메모리 회수(sequential_load 시 다음 단계 적재 공간 확보).
        _release_memory(cfg)

    _save_segments_checkpoint(ckpt, segments, language)
    return segments, language


def _stage_align(
    segments: list[Segment],
    wav_path: str,
    language: str,
    cfg: AppConfig,
    progress: ProgressCb | None,
    work_dir: Path,
    should_cancel: CancelCb | None,
) -> list[Segment]:
    """정렬 단계. 실패는 align 모듈이 내부에서 흡수(원본 반환)."""
    ckpt = work_dir / "02_align.json"
    cached = _load_segments_checkpoint(ckpt)
    if cached is not None:
        logger.info("정렬 체크포인트 재사용: %s", ckpt)
        return cached.segments

    try:
        aligned = align_mod.align(segments, wav_path, language, cfg, progress)
    finally:
        _release_memory(cfg)

    _save_segments_checkpoint(ckpt, aligned, language)
    return aligned


def _stage_diarize(
    wav_path: str,
    cfg: AppConfig,
    min_speakers: int | None,
    max_speakers: int | None,
    progress: ProgressCb | None,
    work_dir: Path,
    should_cancel: CancelCb | None,
) -> list[DiarizationTurn]:
    """화자분리 단계(청크). 끝나면 모델 메모리 회수.

    cfg.diarize_in_subprocess 로 두 경로를 분기한다(P0 처방 ③):
      - False(기본): 기존 in-process diarize() 경로 — **동작 불변(회귀 안전)**.
      - True: _run_diarize_subprocess() — 별도 프로세스로 돌려 끝나면 프로세스째
        죽여 OS 가 GPU/RAM 을 완전 회수. 결과는 양쪽 모두 동일하게 체크포인트 저장.
    """
    ckpt = work_dir / "03_diarize.json"
    cached = _load_turns_checkpoint(ckpt)
    if cached is not None:
        logger.info("화자분리 체크포인트 재사용: %s", ckpt)
        return cached

    if cfg.diarize_in_subprocess:
        # ⑥ 프로세스 격리 경로: subprocess 가 메모리를 통째로 가지고 죽으므로
        # in-process GPU 캐시 회수(_release_memory)는 불필요(프로세스 종료가 더 강한 회수).
        turns = _run_diarize_subprocess(
            wav_path, cfg, min_speakers, max_speakers, progress, work_dir, should_cancel
        )
    else:
        # 기존 in-process 경로(기본). 단계 종료 후 GPU 캐시 회수.
        try:
            turns = diarize_mod.diarize(wav_path, cfg, min_speakers, max_speakers, progress)
        finally:
            _release_memory(cfg)

    _save_turns_checkpoint(ckpt, turns)
    return turns


# ─────────────────────────────────────────────────────────────
# ⑥ 프로세스 격리 — 화자분리 subprocess 실행 헬퍼 (P0 처방 ③)
# ─────────────────────────────────────────────────────────────
# 취소 신호 후 proc.terminate() → 이 시간(초) 안에 안 죽으면 kill().
_DIARIZE_TERMINATE_GRACE_SEC = 10.0


def _run_diarize_subprocess(
    wav_path: str,
    cfg: AppConfig,
    min_speakers: int | None,
    max_speakers: int | None,
    progress: ProgressCb | None,
    work_dir: Path,
    should_cancel: CancelCb | None,
) -> list[DiarizationTurn]:
    """diarize 를 별도 subprocess(_diarize_worker)로 실행한다.

    프로토콜(_diarize_worker 모듈과 1:1 계약):
      - payload(JSON)를 임시 파일로 써서 ``--payload-file`` 로 넘긴다(stdin 파이프
        교착·인코딩 이슈 회피, Windows 안전).
      - 자식 stdout = 결과 JSON 한 줄(``list[DiarizationTurn]`` dump).
      - 자식 stderr = ``PROGRESS <local> <msg>`` / ``ERROR <msg>`` 라인.
      - 종료코드 0=성공.

    취소: should_cancel() 가 True 가 되면 proc.terminate() → grace 초 후 kill().
    이 경우 PipelineCancelled 를 던진다(상위 finally 가 work_dir 정리).

    반환·예외 형태는 in-process diarize() 와 동일해야 한다(호출부 무수정 호환):
      - 성공: list[DiarizationTurn]
      - 토큰 미설정·모델 실패 등: RuntimeError(자식 stderr ERROR 를 메시지로 전파)
    """
    # 1) payload 작성(필요 필드만 직렬화) → 임시 파일.
    payload = {
        "wav_path": wav_path,
        "min_speakers": min_speakers,
        "max_speakers": max_speakers,
        "cfg": serialize_cfg(cfg),
    }
    payload_path = work_dir / "03_diarize_payload.json"
    # BOM 없는 UTF-8(자식은 utf-8-sig 로 읽어 BOM 도 흡수). 한글 보존.
    with open(payload_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    # 2) 자식 프로세스 기동. 현재 인터프리터로 모듈 실행(동일 venv 보장).
    #    -u: 표준스트림 unbuffered → PROGRESS 라인을 실시간으로 받기 위함.
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "meetscribe.pipeline._diarize_worker",
        "--payload-file",
        str(payload_path),
    ]
    logger.info("화자분리 subprocess 기동: %s", " ".join(cmd))

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # 텍스트 모드(라인 단위). 인코딩 명시(Windows 기본 cp949 회피).
        text=True,
        encoding="utf-8",
        errors="replace",
        # 출력을 라인 버퍼링으로(자식이 -u 라 사실상 즉시 flush).
        bufsize=1,
    )

    # 3) stdout(결과 JSON)을 **별도 스레드**로 끝까지 빨아들인다.
    #    이유(교착 방지): 우리가 stderr 만 읽는 동안 자식이 큰 결과를 stdout 으로
    #    쓰면(3h/수천 turn → 64KB 파이프 버퍼 초과) 자식은 stdout write 에서 막히고
    #    우리는 stderr read 에서 막혀 데드락. stdout 을 동시에 비워 이를 차단한다.
    stdout_chunks: list[str] = []

    def _drain_stdout() -> None:
        out = proc.stdout
        if out is None:
            return
        try:
            for chunk in out:  # EOF(자식 종료)까지 블로킹 읽기.
                stdout_chunks.append(chunk)
        except Exception:  # noqa: BLE001 — 파이프 조기 종료 등은 무시
            pass

    stdout_thread = threading.Thread(target=_drain_stdout, daemon=True)
    stdout_thread.start()

    # 4) stderr 를 줄 단위로 읽으며 PROGRESS 중계·ERROR 수집·취소 폴링.
    err_message = _pump_diarize_subprocess(proc, progress, should_cancel)

    # 5) 자식 종료 대기 + stdout 스레드 합류(잔여 버퍼까지 모두 회수).
    proc.wait()
    stdout_thread.join(timeout=_DIARIZE_TERMINATE_GRACE_SEC)
    stdout_data = "".join(stdout_chunks)
    returncode = proc.returncode

    # 6) 결과 판정.
    if returncode != 0:
        # 취소로 죽인 경우: 음수(시그널) 또는 grace 후 kill → 명확히 취소로 처리.
        if should_cancel is not None and should_cancel():
            logger.info("화자분리 subprocess 취소로 종료(code=%s).", returncode)
            raise PipelineCancelled("사용자 취소(화자분리 subprocess)")
        # 그 외 비정상 종료: 자식 ERROR 메시지를 RuntimeError 로 승격(in-process 동등).
        detail = err_message or f"종료코드 {returncode}"
        raise RuntimeError(f"화자분리 subprocess 실패: {detail}")

    turns = _parse_diarize_stdout(stdout_data)
    logger.info("화자분리 subprocess 완료: turn %d", len(turns))
    return turns


def _pump_diarize_subprocess(
    proc: "subprocess.Popen[str]",
    progress: ProgressCb | None,
    should_cancel: CancelCb | None,
) -> str | None:
    """자식 stderr 를 줄 단위로 읽어 PROGRESS 를 중계하고 ERROR 를 모은다.

    - ``PROGRESS <local> <msg>``: progress 콜백을 JobStage.DIARIZE 로 호출.
    - ``ERROR <msg>``: 마지막 에러 메시지로 보관(반환).
    - 매 라인 사이 should_cancel() 폴링 → True 면 terminate→(grace)→kill 후 반환.

    stderr 는 줄 단위 블로킹 ``readline`` 으로 읽는다(자식이 -u 라 라인 즉시 도착).
    취소 응답성은 "라인 사이"가 한계지만, 화자분리는 청크마다 PROGRESS 를 흘리므로
    실무상 충분히 자주 확인된다.

    Returns:
        수집한 마지막 ERROR 메시지(없으면 None).
    """
    last_error: str | None = None
    stderr = proc.stderr
    if stderr is None:  # 방어(파이프 미설정) — 정상 경로에선 발생 안 함.
        return None

    for raw in stderr:
        line = raw.rstrip("\r\n")
        if not line:
            continue

        if line.startswith(PROGRESS_PREFIX):
            _relay_progress_line(line, progress)
        elif line.startswith(ERROR_PREFIX):
            # "ERROR " 접두사 제거 후 본문 보관.
            last_error = line[len(ERROR_PREFIX):].strip()
            logger.warning("화자분리 subprocess ERROR: %s", last_error)
        else:
            # 그 외 라인(자식 라이브러리 stderr 로그 등)은 디버그로만.
            logger.debug("[diarize-worker] %s", line)

        # 취소 폴링: 신호 시 프로세스 종료(terminate→grace→kill).
        if should_cancel is not None and should_cancel():
            logger.info("취소 신호 감지 — 화자분리 subprocess 종료 시도.")
            _terminate_proc(proc)
            break

    return last_error


def _relay_progress_line(line: str, progress: ProgressCb | None) -> None:
    """``PROGRESS <local> <msg>`` 한 줄을 파싱해 progress 콜백으로 중계한다.

    형식이 어긋나면 조용히 무시(진행률 중계는 최적화일 뿐, 실패해도 비치명적).
    """
    if progress is None:
        return
    # "PROGRESS", "<local>", "<msg...>" 3토막(메시지에 공백 포함 가능).
    parts = line.split(" ", 2)
    if len(parts) < 2:
        return
    try:
        local = float(parts[1])
    except ValueError:
        return
    message = parts[2] if len(parts) >= 3 else ""
    try:
        progress(JobStage.DIARIZE, min(1.0, max(0.0, local)), message)
    except Exception:  # noqa: BLE001 — 콜백 오류가 파이프라인을 깨지 않게
        logger.debug("progress 콜백 중계 실패(무시)", exc_info=True)


def _terminate_proc(proc: "subprocess.Popen[str]") -> None:
    """자식을 정중히 종료(terminate) → grace 초 내 미종료 시 강제 종료(kill).

    OS 가 프로세스 자원을 완전히 회수하도록 보장(프로세스 격리의 핵심)."""
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001 — 이미 죽었을 수 있음
        return
    try:
        proc.wait(timeout=_DIARIZE_TERMINATE_GRACE_SEC)
    except subprocess.TimeoutExpired:
        logger.warning("화자분리 subprocess 가 grace 내 미종료 — kill() 강제 종료.")
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _parse_diarize_stdout(stdout_data: str | None) -> list[DiarizationTurn]:
    """자식 stdout(결과 JSON 한 줄)을 list[DiarizationTurn] 로 역직렬화한다.

    자식은 ``[t.model_dump(), ...]`` 를 한 줄로 출력한다. 여러 줄이 섞여 와도
    (라이브러리가 stdout 으로 뭔가 찍는 비정상 상황) **JSON 배열인 마지막 라인**을
    골라 파싱한다(견고성).
    """
    if not stdout_data:
        raise RuntimeError("화자분리 subprocess 가 결과를 출력하지 않았습니다(빈 stdout).")

    # 마지막 비어있지 않은 라인부터 역순으로 JSON 배열 파싱 시도.
    lines = [ln for ln in stdout_data.splitlines() if ln.strip()]
    for line in reversed(lines):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, list):
            return [DiarizationTurn.model_validate(t) for t in data]

    raise RuntimeError("화자분리 subprocess 결과 파싱 실패(JSON 배열 라인 없음).")


# ─────────────────────────────────────────────────────────────
# ③ 취소 / ④ 메모리 회수
# ─────────────────────────────────────────────────────────────
def _check_cancel(should_cancel: CancelCb | None) -> None:
    """취소 신호 확인 — True 면 PipelineCancelled 를 던져 즉시 중단한다.

    단계 경계마다 호출한다(단계 내부 세밀 취소는 각 모듈 진행 콜백에 위임).
    """
    if should_cancel is not None and should_cancel():
        logger.info("취소 신호 감지 — 파이프라인을 중단합니다.")
        raise PipelineCancelled("사용자 취소")


def _release_memory(cfg: AppConfig) -> None:
    """단계 종료 후 GPU 캐시를 비운다(cfg.sequential_load 시 OOM 예방).

    torch 미설치/CPU 환경에서도 안전(예외 무시). 무거운 import 는 여기서 lazy.
    """
    if not cfg.sequential_load:
        return
    try:
        import gc

        import torch  # lazy

        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
    except Exception:  # noqa: BLE001 — 메모리 회수 실패는 치명적이지 않음
        pass


# ─────────────────────────────────────────────────────────────
# ② 체크포인트 입출력 (재개 가능)
# ─────────────────────────────────────────────────────────────
class _SegmentsCheckpoint:
    """세그먼트 체크포인트 역직렬화 결과(language 동봉)."""

    __slots__ = ("segments", "language")

    def __init__(self, segments: list[Segment], language: str) -> None:
        self.segments = segments
        self.language = language


def _save_segments_checkpoint(path: Path, segments: list[Segment], language: str) -> None:
    """세그먼트+언어를 임시 JSON 으로 저장한다(원자적 교체)."""
    payload = {
        "language": language,
        "segments": [s.model_dump() for s in segments],
    }
    _atomic_write_json(path, payload)


def _load_segments_checkpoint(path: Path) -> _SegmentsCheckpoint | None:
    """세그먼트 체크포인트를 읽는다. 없거나 손상 시 None(재계산)."""
    data = _read_json(path)
    if not data:
        return None
    try:
        segments = [Segment.model_validate(s) for s in data.get("segments", [])]
        language = str(data.get("language", "ko"))
        return _SegmentsCheckpoint(segments, language)
    except Exception:  # noqa: BLE001 — 손상 체크포인트는 무시하고 재계산
        logger.warning("손상된 세그먼트 체크포인트 무시: %s", path)
        return None


def _save_turns_checkpoint(path: Path, turns: list[DiarizationTurn]) -> None:
    """화자 turn 체크포인트 저장."""
    _atomic_write_json(path, {"turns": [t.model_dump() for t in turns]})


def _load_turns_checkpoint(path: Path) -> list[DiarizationTurn] | None:
    """화자 turn 체크포인트 로드. 없거나 손상 시 None."""
    data = _read_json(path)
    if not data:
        return None
    try:
        return [DiarizationTurn.model_validate(t) for t in data.get("turns", [])]
    except Exception:  # noqa: BLE001
        logger.warning("손상된 화자분리 체크포인트 무시: %s", path)
        return None


def _atomic_write_json(path: Path, payload: dict) -> None:
    """임시 파일에 쓰고 교체 — 중간에 죽어도 부분 파일이 남지 않게."""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        # BOM 없는 UTF-8(다른 도구가 읽어도 안전).
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        # 체크포인트는 최적화일 뿐 — 실패해도 파이프라인은 계속.
        logger.warning("체크포인트 쓰기 실패(%s): %s", path, exc)


def _read_json(path: Path) -> dict | None:
    """JSON 읽기. 파일 없음/파싱 오류는 None."""
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────
# 결과 메타 계산
# ─────────────────────────────────────────────────────────────
def _result_duration(segments: list[Segment], wav_path: str | None) -> float:
    """결과 길이(초). WAV 길이 우선, 실패 시 마지막 세그먼트 끝 시각."""
    dur = diarize_mod._audio_duration_sec(wav_path) if wav_path else 0.0
    if dur > 0:
        return dur
    return max((s.end for s in segments), default=0.0)


def _collect_speakers(segments: list[Segment], turns: list[DiarizationTurn]) -> list[str]:
    """등장 화자 목록(정렬). 세그먼트 화자 우선, 비면 turn 라벨로 보완."""
    labels = {s.speaker for s in segments if s.speaker}
    if not labels:
        labels = {t.speaker for t in turns}
    return sorted(labels)


def _cleanup_dir(work_dir: Path) -> None:
    """작업 디렉토리(임시 WAV·체크포인트) 전체 삭제. 실패는 경고만."""
    import shutil

    try:
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("작업 디렉토리 정리 실패(%s): %s", work_dir, exc)
