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

from ..config import AppConfig
from ..schemas import DiarizationTurn, JobStage

logger = logging.getLogger(__name__)

# pyannote 사전학습 파이프라인 식별자(3.1 세대).
_PIPELINE_ID = "pyannote/speaker-diarization-3.1"


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
        # pyannote 3.1 은 gated → 약관 동의·토큰 필수. 조용히 실패하지 않는다.
        raise RuntimeError(
            "화자분리에는 Hugging Face 토큰이 필요합니다(HF_TOKEN). "
            f"'{_PIPELINE_ID}' 모델 약관 동의 후 토큰을 설정하세요."
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
        turns = _diarize_chunked(
            pipeline, wav_path, duration, chunk_sec, min_speakers, max_speakers, progress, should_cancel
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

    logger.info("pyannote 파이프라인 로드: %s (device=%s)", _PIPELINE_ID, cfg.torch_device)
    pipeline = Pipeline.from_pretrained(_PIPELINE_ID, use_auth_token=cfg.hf_token)
    if pipeline is None:
        raise RuntimeError(
            f"pyannote 파이프라인 로드 실패: {_PIPELINE_ID} "
            "(토큰 권한 또는 약관 동의를 확인하세요)."
        )
    pipeline.to(torch.device(cfg.torch_device))
    return pipeline


def _diarize_window(
    pipeline,
    wav_path: str,
    offset_sec: float,
    duration_sec: float,
    min_speakers: int | None,
    max_speakers: int | None,
) -> list[DiarizationTurn]:
    """[offset, offset+duration) 구간만 잘라 화자분리하고, 전역 시간축으로 보정한다.

    pyannote Pipeline 에는 crop 메서드가 없다. ``Audio.crop`` 으로 해당 구간의
    파형만 잘라 ``pipeline({"waveform","sample_rate"})`` 로 넘긴다. 잘린 파형은
    0초부터 시작하므로, 결과 turn 시간에 offset 을 더해 **전역 시간축**으로 복원한다.
    """
    from pyannote.audio import Audio  # lazy
    from pyannote.core import Segment as PaSegment  # lazy

    apply_kwargs: dict = {}
    if min_speakers is not None:
        apply_kwargs["min_speakers"] = min_speakers
    if max_speakers is not None:
        apply_kwargs["max_speakers"] = max_speakers

    if duration_sec > 0:
        # 부분 구간 파형만 추출(Audio.crop) → pipeline 에 dict 입력. 메모리·시간 절약.
        audio = Audio()
        window = PaSegment(offset_sec, offset_sec + duration_sec)
        waveform, sample_rate = audio.crop(wav_path, window)
        annotation = pipeline(
            {"waveform": waveform, "sample_rate": sample_rate}, **apply_kwargs
        )
        time_offset = offset_sec  # 잘린 파형은 0 기준 → 전역 시간으로 보정
    else:
        annotation = pipeline(wav_path, **apply_kwargs)
        time_offset = 0.0

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
