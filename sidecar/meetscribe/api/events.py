"""진행률 가중치·계산 (SSE 이벤트의 percent 산출).

파이프라인 단계마다 소요 비중이 다르다. 8GB VRAM·1시간 녹음 기준 체감 시간으로
대략적인 가중치를 둔다(설계서 §진행률). 전체 진행률(0~100)은:

    overall = (앞선 단계들의 가중치 합) + (현재 단계 가중치 × 단계내 로컬진행률 0~1)

단계내 로컬진행률은 각 단계 모듈이 ProgressCb로 0~1을 흘려준 값이다.
무거운 import는 전혀 없고 schemas 의 enum 만 참조하므로 어디서든 안전하게 import 된다.
"""

from __future__ import annotations

from ..schemas import JobStage

# ─────────────────────────────────────────────────────────────
# 단계 가중치 (합 = 100). CONTRACT T2 명시값.
#   PREPROCESS 5 / TRANSCRIBE 45 / ALIGN 20 / DIARIZE 25 / MERGE 5
# ─────────────────────────────────────────────────────────────
STAGE_WEIGHTS: dict[JobStage, float] = {
    JobStage.PREPROCESS: 5.0,
    JobStage.TRANSCRIBE: 45.0,
    JobStage.ALIGN: 20.0,
    JobStage.DIARIZE: 25.0,
    JobStage.MERGE: 5.0,
}

# 진행률 산출에 참여하는 단계 순서(가중치 누적의 기준).
# QUEUED/DONE/FAILED/CANCELLED 는 percent 계산에서 특수 처리한다.
_STAGE_ORDER: list[JobStage] = [
    JobStage.PREPROCESS,
    JobStage.TRANSCRIBE,
    JobStage.ALIGN,
    JobStage.DIARIZE,
    JobStage.MERGE,
]


def _clamp01(x: float) -> float:
    """로컬 진행률을 0~1 범위로 가둔다(콜백이 약간 벗어난 값을 줘도 안전)."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def overall_percent(stage: JobStage, local: float) -> float:
    """현재 단계와 단계내 로컬진행률(0~1)로 전체 진행률(0~100)을 계산한다.

    - QUEUED: 아직 시작 전 → 0
    - DONE: 완료 → 100
    - FAILED / CANCELLED: 진행률 의미 없음 → 0 (상태로 구분)
    - 그 외(실제 처리 단계): 앞 단계 가중치 합 + 현재 단계 가중치 × local
    """
    if stage == JobStage.QUEUED:
        return 0.0
    if stage == JobStage.DONE:
        return 100.0
    if stage in (JobStage.FAILED, JobStage.CANCELLED):
        return 0.0

    # 앞선 단계들의 가중치를 모두 누적한다.
    base = 0.0
    for s in _STAGE_ORDER:
        if s == stage:
            break
        base += STAGE_WEIGHTS.get(s, 0.0)

    base += STAGE_WEIGHTS.get(stage, 0.0) * _clamp01(local)

    # 부동소수 오차로 100을 살짝 넘는 일이 없게 가둔다.
    return min(100.0, round(base, 2))
