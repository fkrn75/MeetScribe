"""F5 단어 정렬 — whisperx 의 wav2vec2(한국어) 강제 정렬로 단어 타임스탬프 부여.

Whisper 의 세그먼트 단위 타임스탬프는 거칠다. whisperx 의 forced alignment
(언어별 wav2vec2 음향 모델)로 **단어 단위** start/end 를 얻어, 이후 화자 병합의
해상도를 높인다(설계서: 한국어 wav2vec2 정렬 모델 내장이 핵심 차별점).

장시간 안정성(설계서 §10):
- 정렬은 cfg.align_chunk_sec(기본 10분) 단위로 끊어 처리해 메모리 폭발을 막는다.
- **graceful degrade**: 정렬 모델 로드/추론이 실패하면 입력 segments 를 그대로
  반환하고 경고만 남긴다(전체 파이프라인은 계속 진행).

무거운 import(``whisperx``·``torch``)는 함수 내부 lazy import.
"""

from __future__ import annotations

import logging

from ..config import AppConfig
from ..schemas import JobStage, Segment, Word

logger = logging.getLogger(__name__)


def align(
    segments: list[Segment],
    wav_path: str,
    language: str,
    cfg: AppConfig,
    progress=None,
) -> list[Segment]:
    """세그먼트에 단어 타임스탬프를 채워 반환한다. 실패 시 입력 그대로 반환.

    Args:
        segments: STT 산출 세그먼트(start/end/text).
        wav_path: 16kHz mono WAV 경로.
        language: 정렬 언어 코드(예: "ko").
        cfg: 런타임 설정(디바이스·청크 길이).
        progress: ``(JobStage, local[0~1], message)`` 콜백(선택).

    Returns:
        words 가 채워진 세그먼트 목록. 정렬 불가 시 입력과 동일한 목록.
    """
    if not segments:
        return segments

    if progress:
        progress(JobStage.ALIGN, 0.0, "단어 정렬 시작")

    try:
        result = _align_impl(segments, wav_path, language, cfg, progress)
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 degrade 로 흡수
        # graceful degrade: 정렬 실패가 전체 회의록 생성을 막아선 안 된다.
        logger.warning(
            "단어 정렬 실패 — 원본 세그먼트로 진행합니다(graceful degrade): %s", exc
        )
        if progress:
            progress(JobStage.ALIGN, 1.0, "정렬 생략(원본 타임스탬프 사용)")
        return segments

    if progress:
        progress(JobStage.ALIGN, 1.0, "단어 정렬 완료")
    return result


def _align_impl(
    segments: list[Segment],
    wav_path: str,
    language: str,
    cfg: AppConfig,
    progress,
) -> list[Segment]:
    """실제 whisperx 정렬. 청크 단위로 끊어 모델을 재사용한다."""
    import whisperx  # lazy: 미설치 환경 보호

    device = cfg.torch_device

    # 언어별 정렬 모델(wav2vec2) 로드. 한국어는 whisperx 가 기본 매핑을 제공.
    align_model, metadata = whisperx.load_align_model(
        language_code=language,
        device=device,
        model_dir=str(cfg.cache_dir),
    )

    try:
        # 전체 오디오를 1회 로드(whisperx 유틸). 청크는 세그먼트 묶음으로 나눈다.
        audio = whisperx.load_audio(wav_path)

        chunk_sec = float(cfg.align_chunk_sec) if cfg.align_chunk_sec else 0.0
        chunks = _chunk_segments(segments, chunk_sec)

        aligned: list[Segment] = []
        total = len(chunks)
        for idx, group in enumerate(chunks):
            # whisperx.align 입력 형식: [{"start","end","text"}, ...]
            wx_segments = [{"start": s.start, "end": s.end, "text": s.text} for s in group]
            out = whisperx.align(
                wx_segments,
                align_model,
                metadata,
                audio,
                device,
                return_char_alignments=False,
            )
            aligned.extend(_to_segments(out, group))

            if progress and total > 0:
                progress(JobStage.ALIGN, (idx + 1) / total, f"정렬 {idx + 1}/{total} 청크")

        return aligned
    finally:
        # 모델 참조 해제(실제 cuda 캐시 비우기는 runner 가 단계 종료 후 일괄 수행).
        del align_model


def _chunk_segments(segments: list[Segment], chunk_sec: float) -> list[list[Segment]]:
    """세그먼트를 시간 길이 기준으로 그룹핑한다(chunk_sec<=0 이면 단일 그룹).

    경계에서 세그먼트를 쪼개지 않고, 한 세그먼트는 통째로 한 청크에 넣는다.
    """
    if chunk_sec <= 0 or not segments:
        return [list(segments)]

    chunks: list[list[Segment]] = []
    current: list[Segment] = []
    window_start = segments[0].start

    for seg in segments:
        if current and (seg.end - window_start) > chunk_sec:
            chunks.append(current)
            current = []
            window_start = seg.start
        current.append(seg)
    if current:
        chunks.append(current)
    return chunks


def _to_segments(wx_out: dict, fallback: list[Segment]) -> list[Segment]:
    """whisperx.align 출력(dict)을 Segment 목록으로 변환한다.

    출력이 비정상이면 입력 fallback 을 그대로 사용(부분 degrade).
    """
    out_segments = wx_out.get("segments") if isinstance(wx_out, dict) else None
    if not out_segments:
        return fallback

    result: list[Segment] = []
    for i, seg in enumerate(out_segments):
        words = [
            Word(
                word=str(w.get("word", "")),
                start=_as_float(w.get("start")),
                end=_as_float(w.get("end")),
                score=_as_float(w.get("score")),
                speaker=None,
            )
            for w in (seg.get("words") or [])
        ]
        # 정렬 결과에 start/end 가 없으면 원본 세그먼트 시간으로 보정.
        base = fallback[i] if i < len(fallback) else None
        start = _as_float(seg.get("start"))
        end = _as_float(seg.get("end"))
        result.append(
            Segment(
                start=start if start is not None else (base.start if base else 0.0),
                end=end if end is not None else (base.end if base else 0.0),
                text=str(seg.get("text", base.text if base else "")).strip(),
                speaker=None,
                words=words,
            )
        )
    return result


def _as_float(value) -> float | None:
    """None/누락 안전 float 변환."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
