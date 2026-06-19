"""F3 음성 인식(STT) — faster-whisper 로 16kHz WAV → 세그먼트.

설계 요점:
- ``faster-whisper`` (CTranslate2 백엔드) 사용. large-v3 기준 8GB VRAM 가정.
- VAD 필터로 무음 구간을 제거해 환각(hallucination)·연산 낭비를 줄인다.
- ``condition_on_previous_text=False`` : 긴 회의에서 이전 문맥 누적으로 인한
  반복·드리프트(특히 화자 전환 잦은 회의)를 차단(설계서 §10 적대검증).
- compute_type/batch_size 는 cfg 값을 따르되, OOM 시 점진 완화는 runner 가
  ``cfg.downgrade_for_oom()`` 로 재시도한다.

무거운 import(``faster_whisper``·``torch``)는 모두 함수 내부 lazy import.
"""

from __future__ import annotations

import logging

from ..config import AppConfig
from ..schemas import ComputeType, JobStage, Segment

logger = logging.getLogger(__name__)

# faster-whisper 내장 VAD(Silero) 파라미터 — 회의 음성 기준 보수적 설정.
_VAD_PARAMETERS = {
    "min_silence_duration_ms": 500,  # 0.5초 이상 무음에서 끊음
    "speech_pad_ms": 200,  # 경계 잘림 방지용 패딩
}


def load_model(cfg: AppConfig):
    """faster-whisper 모델을 로드해 반환한다(호출자가 수명 관리).

    runner 가 순차 로드/언로드(sequential_load)를 직접 제어할 수 있도록
    모델 생성을 분리해 둔다.
    """
    from faster_whisper import WhisperModel  # lazy: 미설치 환경 보호

    logger.info(
        "Whisper 모델 로드: model=%s device=%s compute=%s",
        cfg.model,
        cfg.torch_device,
        cfg.compute_type.value,
    )
    return WhisperModel(
        cfg.model,
        device=cfg.torch_device,
        compute_type=cfg.compute_type.value,
        download_root=str(cfg.cache_dir),
    )


def transcribe(
    wav_path: str,
    cfg: AppConfig,
    progress=None,
    should_cancel=None,
) -> tuple[list[Segment], str]:
    """WAV 를 인식해 (세그먼트 목록, 감지 언어) 를 반환한다.

    Args:
        wav_path: 16kHz mono WAV 경로(preprocess 산출).
        cfg: 런타임 설정(모델·디바이스·compute_type·언어 등).
        progress: ``(JobStage, local_progress[0~1], message)`` 콜백(선택).

    Returns:
        (segments, language). segments 는 start/end/text 만 채워진 상태
        (speaker=None, words=[] — 이후 align/merge 단계에서 채움).
    """
    if progress:
        progress(JobStage.TRANSCRIBE, 0.0, "음성 인식 시작")

    model = load_model(cfg)
    try:
        segments, language = _run_transcribe(model, wav_path, cfg, progress, should_cancel)
    finally:
        # 호출자(runner)가 sequential_load 면 여기서 즉시 해제 효과를 보도록 참조 해제.
        # (실제 cuda 캐시 비우기는 runner 가 단계 종료 후 일괄 수행.)
        del model

    if progress:
        progress(JobStage.TRANSCRIBE, 1.0, f"음성 인식 완료({len(segments)}개 세그먼트)")
    return segments, language


def _run_transcribe(model, wav_path: str, cfg: AppConfig, progress, should_cancel=None):
    """실제 인식 루프. 언어는 cfg.language 강제(기본 ko), 없으면 자동 감지."""
    # 순환 import 회피용 lazy import(runner 가 이 모듈을 먼저 적재하므로 top-level 금지).
    from .runner import PipelineCancelled

    # cfg.language 가 빈 문자열/None 이면 자동 감지(language=None)
    forced_language = cfg.language or None

    # faster-whisper 는 제너레이터를 반환 → 순회하며 진행률을 갱신한다.
    seg_iter, info = model.transcribe(
        wav_path,
        language=forced_language,
        beam_size=5,
        vad_filter=True,
        vad_parameters=_VAD_PARAMETERS,
        condition_on_previous_text=False,  # 긴 회의 드리프트 차단(설계서 §10)
        word_timestamps=False,  # 단어 타임스탬프는 align 단계(wav2vec2)가 더 정확
    )

    detected_language = getattr(info, "language", None) or forced_language or "ko"
    total_duration = float(getattr(info, "duration", 0.0)) or 0.0

    results: list[Segment] = []
    for seg in seg_iter:
        # 세그먼트마다 취소 확인 → 긴 인식 도중에도 즉시 중단(제너레이터 소비 중단 = 연산 중단).
        if should_cancel is not None and should_cancel():
            raise PipelineCancelled("사용자 취소")
        results.append(
            Segment(
                start=float(seg.start),
                end=float(seg.end),
                text=(seg.text or "").strip(),
                speaker=None,
                words=[],
            )
        )
        # 인식한 끝 시각 / 전체 길이 로 대략적 진행률 추정(제너레이터라 총개수 미지).
        if progress and total_duration > 0:
            local = min(0.99, float(seg.end) / total_duration)
            progress(JobStage.TRANSCRIBE, local, f"인식 중 {seg.end:.0f}s / {total_duration:.0f}s")

    return results, detected_language


def is_oom_error(exc: BaseException) -> bool:
    """예외가 CUDA/메모리 부족 류인지 휴리스틱 판정(runner 재시도 판단용)."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(k in text for k in ("out of memory", "cuda", "cublas", "alloc"))


# compute_type 힌트(문서용): float16 → int8_float16 → int8 순으로 메모리 절감.
_COMPUTE_FALLBACK_ORDER = (
    ComputeType.FLOAT16,
    ComputeType.INT8_FLOAT16,
    ComputeType.INT8,
)
