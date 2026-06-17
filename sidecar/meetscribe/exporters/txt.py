"""TXT 내보내기 — 사람이 읽는 회의록 텍스트.

형식(CONTRACT): 한 세그먼트당 한 줄
    [mm:ss] 화자: 텍스트

- 화자 미상(speaker=None)이면 "화자 미상" 으로 표기.
- 1시간이 넘어가면 mm 이 60 이상이 되어도 그대로 누적 분으로 표기([72:05] 등).
표준 라이브러리만 사용한다.
"""

from __future__ import annotations

from ..schemas import TranscriptionResult

_UNKNOWN_SPEAKER = "화자 미상"


def _fmt_timestamp(seconds: float) -> str:
    """초 → 'mm:ss'. 60분이 넘으면 분을 계속 누적한다(예: 72:05)."""
    total = int(seconds) if seconds and seconds > 0 else 0
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def write(result: TranscriptionResult, out_path: str) -> str:
    """세그먼트를 '[mm:ss] 화자: 텍스트' 줄로 써서 out_path 에 저장한다."""
    lines: list[str] = []
    for seg in result.segments:
        speaker = seg.speaker or _UNKNOWN_SPEAKER
        text = (seg.text or "").strip()
        lines.append(f"[{_fmt_timestamp(seg.start)}] {speaker}: {text}")

    # BOM 없이 UTF-8 로 저장(다른 도구·OS 호환). 줄바꿈은 '\n' 통일.
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
        if lines:
            f.write("\n")  # 마지막 줄 끝에도 개행

    return out_path
