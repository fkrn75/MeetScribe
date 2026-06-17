"""F5 병합 — 화자분리 turn 과 STT/정렬 세그먼트를 결합해 speaker 를 부여한다.

규칙(CONTRACT):
- 각 단어(정렬된 경우)와 세그먼트에 **시간상 가장 많이 겹치는 화자**를 부여한다.
- 겹치는 turn 이 하나도 없으면, **가장 가까운(인접) turn** 의 화자를 부여한다.
- 단어 레벨 화자가 있으면 세그먼트 화자는 그 다수결로 정한다(정렬 성공 시 더 정확).

이 단계는 순수 파이썬 산술만 사용 — ML 의존성 없음(무거운 import 불필요).
turns 가 비어 있으면(화자분리 생략/실패) 입력 세그먼트를 그대로 반환한다.
"""

from __future__ import annotations

from collections import Counter

from ..schemas import DiarizationTurn, Segment, Word


def assign_speakers(segments: list[Segment], turns: list[DiarizationTurn]) -> list[Segment]:
    """세그먼트(및 단어)에 화자를 부여한 새 세그먼트 목록을 반환한다.

    Args:
        segments: STT/정렬 산출 세그먼트.
        turns: 화자분리 turn(시간 구간별 화자).

    Returns:
        speaker 가 채워진 세그먼트 목록(원본 비파괴 — 복사본 반환).
    """
    if not segments:
        return segments
    if not turns:
        # 화자분리가 없으면 그대로(speaker=None 유지). 비파괴 복사.
        return [s.model_copy(deep=True) for s in segments]

    # 시작 시각 기준 정렬 — 인접 turn 탐색을 효율화.
    ordered_turns = sorted(turns, key=lambda t: (t.start, t.end))

    result: list[Segment] = []
    for seg in segments:
        new_seg = seg.model_copy(deep=True)

        if new_seg.words:
            # 단어 단위로 화자 부여 → 세그먼트 화자는 단어 다수결.
            for w in new_seg.words:
                w.speaker = _speaker_for_interval(w.start, w.end, ordered_turns, seg)
            new_seg.speaker = _majority_speaker(new_seg.words) or _speaker_for_interval(
                seg.start, seg.end, ordered_turns, seg
            )
        else:
            # 단어가 없으면(정렬 생략) 세그먼트 구간 전체로 화자 부여.
            new_seg.speaker = _speaker_for_interval(seg.start, seg.end, ordered_turns, seg)

        result.append(new_seg)

    return result


def _speaker_for_interval(
    start: float | None,
    end: float | None,
    ordered_turns: list[DiarizationTurn],
    fallback_seg: Segment,
) -> str | None:
    """[start,end) 와 가장 많이 겹치는 화자. 겹침 없으면 가장 가까운 turn 의 화자.

    start/end 가 None(정렬 실패 단어)이면 fallback 세그먼트 구간을 사용한다.
    """
    s = start if start is not None else fallback_seg.start
    e = end if end is not None else fallback_seg.end
    if e < s:
        s, e = e, s

    best_speaker: str | None = None
    best_overlap = 0.0
    nearest_speaker: str | None = None
    nearest_dist = float("inf")

    for t in ordered_turns:
        # 겹침 길이 = max(0, min(끝) - max(시작))
        overlap = min(e, t.end) - max(s, t.start)
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = t.speaker

        # 겹침이 없을 때를 대비한 최근접 거리(구간 간 간격).
        if overlap <= 0:
            if t.end < s:
                dist = s - t.end
            elif t.start > e:
                dist = t.start - e
            else:
                dist = 0.0
            if dist < nearest_dist:
                nearest_dist = dist
                nearest_speaker = t.speaker

    if best_speaker is not None:
        return best_speaker
    return nearest_speaker


def _majority_speaker(words: list[Word]) -> str | None:
    """단어들의 화자 다수결(가장 많은 단어를 가진 화자). 없으면 None."""
    labels = [w.speaker for w in words if w.speaker]
    if not labels:
        return None
    return Counter(labels).most_common(1)[0][0]
