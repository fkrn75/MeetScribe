"""F4 화자분리 — pyannote 3.1 로 "누가 언제 말했는가"(DiarizationTurn) 산출.

장시간 안정성(설계서 §10 적대검증)이 이 단계의 핵심 위험:
- pyannote 의 클러스터링은 길이에 민감(메모리·시간 폭발). 그래서 오디오를
  cfg.diarization_chunk_sec(기본 10분) 단위로 잘라 청크별로 돌린 뒤,
  **청크 간 화자 라벨을 정합(stitch)** 해 하나의 전역 화자 체계로 병합한다.
- 청크 경계의 화자 동일성은 직전 청크와의 시간적 인접 + 라벨 빈도 휴리스틱으로
  잇는다(완벽한 화자 재식별은 아님 — 임베딩 기반 재식별은 후속 과제).

무거운 import(``pyannote.audio``·``torch``)는 함수 내부 lazy import.
HF gated 모델이므로 cfg.hf_token 이 없으면 명확한 예외를 던진다.
"""

from __future__ import annotations

import logging
import os

# pyannote 4.0 은 기본적으로 처리한 파일 길이·화자 수를 외부 서버(otel.pyannote.ai)로
# 전송한다(telemetry/metrics.py, config.yaml metrics_enabled: true). MeetScribe 의
# '완전 로컬·외부 전송 0' 원칙에 위배되므로, pyannote import 전에 끈다. metrics.py 는
# import 시 이 환경변수를 읽어 활성화 여부를 정한다(없으면 config 기본값 true).
os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "false")

from ..config import AppConfig
from ..schemas import DiarizationTurn, JobStage

logger = logging.getLogger(__name__)


def diarize(
    wav_path: str,
    cfg: AppConfig,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    progress=None,
    should_cancel=None,
) -> list[DiarizationTurn]:
    """오디오를 화자 구간으로 분리해 turn 목록을 반환한다(청크 처리).

    Args:
        wav_path: 16kHz mono WAV 경로.
        cfg: 런타임 설정(디바이스·청크 길이·hf_token).
        min_speakers / max_speakers: 화자 수 힌트(선택, pyannote 에 전달).
        progress: ``(JobStage, local[0~1], message)`` 콜백(선택).

    Returns:
        시간순 정렬된 DiarizationTurn 목록(speaker/start/end).

    Raises:
        RuntimeError: HF 토큰 미설정 등 화자분리 불가 시.
    """
    if progress:
        progress(JobStage.DIARIZE, 0.0, "화자분리 시작")

    if not cfg.hf_token:
        # pyannote 모델은 gated → 약관 동의·토큰 필수. 조용히 실패하지 않는다.
        raise RuntimeError(
            "화자분리에는 Hugging Face 토큰이 필요합니다(HF_TOKEN). "
            f"'{cfg.diarization_model}' 모델 약관 동의 후 토큰을 설정하세요."
        )

    pipeline = _load_pipeline(cfg)

    try:
        from .runner import PipelineCancelled  # lazy: 순환 import 회피

        duration = _audio_duration_sec(wav_path)
        chunk_sec = float(cfg.diarization_chunk_sec) if cfg.diarization_chunk_sec else 0.0

        if chunk_sec <= 0 or duration <= chunk_sec:
            # 짧은 오디오: 한 번에 처리(단일 블로킹 호출이라 시작 직전에 취소 확인).
            if should_cancel is not None and should_cancel():
                raise PipelineCancelled("사용자 취소")
            turns = _diarize_window(
                pipeline, wav_path, 0.0, duration, min_speakers, max_speakers
            )
            if progress:
                progress(JobStage.DIARIZE, 1.0, f"화자분리 완료({_count_speakers(turns)}명)")
            return _merge_adjacent(turns)

        # 긴 오디오: 청크별 처리 후 라벨 정합.
        # 주의: min_speakers 를 각 청크에 그대로 주면, 그 구간에 실제로 말한 사람이
        # 적어도 억지로 그 수만큼 쪼개 과분할이 된다(예: 5명 회의의 10분 구간에 2명만
        # 말했는데 5명으로 분리). 청크 경로에선 상한(max)만 적용하고 하한은 풀어 둔다.
        # (단일 청크 경로에서는 min/max 를 모두 그대로 적용해 인원을 정확히 고정한다.)
        turns = _diarize_chunked(
            pipeline, wav_path, duration, chunk_sec, None, max_speakers, progress, should_cancel
        )
        if progress:
            progress(JobStage.DIARIZE, 1.0, f"화자분리 완료({_count_speakers(turns)}명)")
        return _merge_adjacent(turns)
    finally:
        # 모델 참조 해제(cuda 캐시 비우기는 runner 가 단계 종료 후 일괄 수행).
        del pipeline


def _load_pipeline(cfg: AppConfig):
    """pyannote 파이프라인을 로드해 지정 디바이스로 옮긴다."""
    import torch  # lazy
    from pyannote.audio import Pipeline  # lazy

    model_id = cfg.diarization_model
    logger.info("pyannote 파이프라인 로드: %s (device=%s)", model_id, cfg.torch_device)
    # pyannote 4.0: use_auth_token deprecated → token. (3.1 모델도 token 으로 로드된다.)
    pipeline = Pipeline.from_pretrained(model_id, token=cfg.hf_token)
    if pipeline is None:
        raise RuntimeError(
            f"pyannote 파이프라인 로드 실패: {model_id} "
            "(토큰 권한 또는 약관 동의를 확인하세요)."
        )
    # community-1(통합 모델)은 clustering/segmentation instantiate 구조가 3.1과 달라
    # 하이퍼파라미터 재정의가 불필요하고 오히려 깨질 수 있다(임베딩이 명확해 파라미터에
    # 안정적 — 실측 확인). 3.1 계열만 튜닝을 적용한다.
    if "community" not in model_id.lower():
        _apply_clustering_threshold(pipeline, cfg)
    pipeline.to(torch.device(cfg.torch_device))
    return pipeline


def _apply_clustering_threshold(pipeline, cfg: AppConfig) -> None:
    """화자분리 하이퍼파라미터(clustering.threshold·segmentation.min_duration_off)를 재정의한다.

    pyannote 3.1 SpeakerDiarization 의 구조를 가정한다. 모델 교체로 구조가 달라지면
    조용히 건너뛴다(튜닝 실패는 치명적이지 않음 — 기본값으로 동작).
    - clustering.threshold: ↑ 화자 병합 / ↓ 분할(참석자 수 지정 시 영향 거의 사라짐).
    - segmentation.min_duration_off: 화자 전환으로 볼 최소 침묵. ↑ 하면 짧은 침묵(기침·
      호흡)으로 인한 과분할을 억제한다.
    둘 다 기본값(threshold 없음 + min_duration_off 0)이면 파이프라인을 손대지 않는다(회귀 안전).
    """
    threshold = cfg.clustering_threshold
    min_dur_off = float(getattr(cfg, "min_duration_off", 0.0) or 0.0)
    if threshold is None and min_dur_off <= 0.0:
        return
    try:
        # 전체 dict 를 요구하므로, 지정 안 한 값은 3.1 기본값으로 채워 함께 전달한다.
        pipeline.instantiate(
            {
                "clustering": {
                    "method": "centroid",
                    "min_cluster_size": 12,
                    "threshold": float(threshold) if threshold is not None else 0.7045654963945799,
                },
                "segmentation": {
                    "min_duration_off": min_dur_off,
                },
            }
        )
        logger.info(
            "화자분리 파라미터 재정의: threshold=%s, min_duration_off=%.2f",
            f"{threshold:.4f}" if threshold is not None else "기본",
            min_dur_off,
        )
    except Exception as e:  # noqa: BLE001 — 기본값 유지하고 계속(치명적이지 않음)
        logger.warning("화자분리 파라미터 적용 실패 — 기본값으로 진행: %s", e)


def _extract_annotation(output):
    """파이프라인 출력에서 화자분리 Annotation 을 꺼낸다.

    pyannote 4.0(community-1)은 ``DiarizeOutput``(speaker_diarization 등 보유)을,
    3.1 계열은 ``Annotation`` 을 직접 반환한다. 양쪽을 모두 받아 itertracks 가능한
    Annotation 으로 정규화한다.
    """
    for attr in ("speaker_diarization", "diarization", "annotation"):
        if hasattr(output, attr):
            return getattr(output, attr)
    return output


def _diarize_window(
    pipeline,
    wav_path: str,
    offset_sec: float,
    duration_sec: float,
    min_speakers: int | None,
    max_speakers: int | None,
) -> list[DiarizationTurn]:
    """[offset, offset+duration) 구간만 잘라 화자분리하고, 전역 시간축으로 보정한다.

    soundfile 로 해당 구간 파형만 직접 읽어(torchcodec 우회) ``pipeline({"waveform",
    "sample_rate"})`` 로 넘긴다. 잘린 파형은 0초부터 시작하므로, 결과 turn 시간에
    offset 을 더해 **전역 시간축**으로 복원한다.
    """
    import torch  # lazy
    import soundfile as sf  # lazy

    apply_kwargs: dict = {}
    if min_speakers is not None:
        apply_kwargs["min_speakers"] = min_speakers
    if max_speakers is not None:
        apply_kwargs["max_speakers"] = max_speakers

    # pyannote 4.0 기본 디코더(torchcodec)는 ffmpeg 공유 DLL 의존이라 Windows 에서
    # 로드가 깨질 수 있다. preprocess 가 16kHz mono PCM 을 보장하므로, soundfile 로
    # 부분 구간만 프레임 단위로 직접 읽어 디코더를 우회한다(파형 직접 입력).
    info = sf.info(wav_path)
    sample_rate = info.samplerate
    if duration_sec > 0:
        start_f = max(0, int(offset_sec * sample_rate))
        stop_f = min(info.frames, int((offset_sec + duration_sec) * sample_rate))
        data, sample_rate = sf.read(wav_path, start=start_f, stop=stop_f, dtype="float32")
        time_offset = offset_sec  # 잘린 파형은 0 기준 → 전역 시간으로 보정
    else:
        data, sample_rate = sf.read(wav_path, dtype="float32")
        time_offset = 0.0
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)  # 혹시 스테레오면 모노화
    waveform = torch.from_numpy(data).unsqueeze(0)  # (1, N)

    output = pipeline({"waveform": waveform, "sample_rate": sample_rate}, **apply_kwargs)
    annotation = _extract_annotation(output)

    turns: list[DiarizationTurn] = []
    for segment, _track, speaker in annotation.itertracks(yield_label=True):
        turns.append(
            DiarizationTurn(
                speaker=str(speaker),
                start=float(segment.start) + time_offset,
                end=float(segment.end) + time_offset,
            )
        )
    turns.sort(key=lambda t: (t.start, t.end))
    return turns


def _diarize_chunked(
    pipeline,
    wav_path: str,
    duration_sec: float,
    chunk_sec: float,
    min_speakers: int | None,
    max_speakers: int | None,
    progress,
    should_cancel=None,
) -> list[DiarizationTurn]:
    """오디오를 chunk_sec 단위로 처리하고 청크 간 화자 라벨을 정합해 합친다."""
    from .runner import PipelineCancelled  # lazy: 순환 import 회피

    all_turns: list[DiarizationTurn] = []
    # 청크 로컬 라벨 → 전역 라벨 매핑(직전 청크 라벨 재사용으로 연속성 확보).
    label_map: dict[str, str] = {}
    next_global = 0  # 새 전역 화자 인덱스(SPEAKER_00, 01, ...)
    prev_chunk_turns: list[DiarizationTurn] = []

    n_chunks = max(1, int((duration_sec + chunk_sec - 1) // chunk_sec))
    offset = 0.0
    chunk_idx = 0
    while offset < duration_sec:
        # 청크마다 취소 확인.
        if should_cancel is not None and should_cancel():
            raise PipelineCancelled("사용자 취소")
        window = min(chunk_sec, duration_sec - offset)
        local_turns = _diarize_window(
            pipeline, wav_path, offset, window, min_speakers, max_speakers
        )

        # 이 청크의 로컬 화자 라벨을 전역 라벨로 정합.
        local_map, next_global = _stitch_labels(
            local_turns, prev_chunk_turns, label_map, next_global
        )
        relabeled = [
            DiarizationTurn(speaker=local_map[t.speaker], start=t.start, end=t.end)
            for t in local_turns
        ]
        all_turns.extend(relabeled)
        prev_chunk_turns = relabeled

        chunk_idx += 1
        if progress and n_chunks > 0:
            progress(
                JobStage.DIARIZE,
                min(0.99, chunk_idx / n_chunks),
                f"화자분리 {chunk_idx}/{n_chunks} 청크",
            )
        offset += window

    all_turns.sort(key=lambda t: (t.start, t.end))
    return all_turns


def _stitch_labels(
    local_turns: list[DiarizationTurn],
    prev_chunk_turns: list[DiarizationTurn],
    label_map: dict[str, str],
    next_global: int,
) -> tuple[dict[str, str], int]:
    """청크 로컬 라벨을 전역 라벨로 잇는다(휴리스틱).

    전략: 직전 청크의 마지막 부분 화자와 현재 청크 첫 부분 화자가 시간적으로
    맞닿아 있으면 같은 전역 라벨로 본다. 그 외 새 로컬 라벨은 새 전역 라벨 할당.
    완벽한 임베딩 기반 재식별이 아니라, 경계 끊김을 줄이는 보수적 정합이다.

    Returns:
        (이 청크용 local→global 매핑, 갱신된 next_global).
    """
    local_map: dict[str, str] = {}

    # 직전 청크의 "지배 화자"(가장 길게 말한 전역 라벨)를 후보로.
    prev_dominant = _dominant_speaker(prev_chunk_turns)
    # 이 청크의 지배 화자(로컬 라벨).
    local_dominant = _dominant_speaker(local_turns)

    # 경계 연속성: 직전 지배 화자가 있으면 이 청크 지배 화자에 우선 매핑.
    if prev_dominant is not None and local_dominant is not None:
        local_map[local_dominant] = prev_dominant

    # 나머지 로컬 라벨은 안정적 순서로 새 전역 라벨 부여.
    for t in local_turns:
        if t.speaker in local_map:
            continue
        global_label = f"SPEAKER_{next_global:02d}"
        local_map[t.speaker] = global_label
        next_global += 1

    return local_map, next_global


def _dominant_speaker(turns: list[DiarizationTurn]) -> str | None:
    """말한 총 시간이 가장 긴 화자 라벨을 반환(없으면 None)."""
    if not turns:
        return None
    totals: dict[str, float] = {}
    for t in turns:
        totals[t.speaker] = totals.get(t.speaker, 0.0) + max(0.0, t.end - t.start)
    return max(totals.items(), key=lambda kv: kv[1])[0]


def _merge_adjacent(turns: list[DiarizationTurn], gap: float = 0.5) -> list[DiarizationTurn]:
    """같은 화자의 인접/근접 turn 을 병합해 조각화를 줄인다(gap 초 이하면 연결)."""
    if not turns:
        return turns
    ordered = sorted(turns, key=lambda t: (t.start, t.end))
    merged: list[DiarizationTurn] = [ordered[0].model_copy()]
    for t in ordered[1:]:
        last = merged[-1]
        if t.speaker == last.speaker and t.start - last.end <= gap:
            # 끝 시각만 확장(겹치거나 근접한 같은 화자).
            if t.end > last.end:
                last.end = t.end
        else:
            merged.append(t.model_copy())
    return merged


def _count_speakers(turns: list[DiarizationTurn]) -> int:
    """고유 화자 수."""
    return len({t.speaker for t in turns})


def _audio_duration_sec(wav_path: str) -> float:
    """WAV 길이(초)를 구한다. 표준 라이브러리 ``wave`` 사용(추가 의존성 없음).

    16kHz mono PCM(preprocess 산출) 전제. 실패 시 0.0 반환(→ 단일 처리로 폴백).
    """
    import contextlib
    import wave

    try:
        with contextlib.closing(wave.open(wav_path, "rb")) as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 1
            return frames / float(rate)
    except Exception:  # noqa: BLE001 — 길이 측정 실패는 치명적이지 않음
        logger.warning("WAV 길이 측정 실패 — 단일 청크로 처리합니다: %s", wav_path)
        return 0.0
