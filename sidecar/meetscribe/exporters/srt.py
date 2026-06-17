"""SRT 자막 내보내기 — 화자 prefix 가 붙은 자막.

표준 SRT 블록 형식:
    1
    00:00:01,000 --> 00:00:04,500
    화자: 텍스트
    (빈 줄)

- 타임코드는 'HH:MM:SS,mmm' (밀리초, 콤마 구분 — SRT 규격).
- 화자 미상이면 prefix 를 생략하고 텍스트만 넣는다(자막 가독성).
표준 라이브러리만 사용한다.
"""

from __future__ import annotations

from ..schemas import TranscriptionResult


def _fmt_srt_time(seconds: float) -> str:
    """초 → 'HH:MM:SS,mmm' (SRT 규격, 밀리초 콤마)."""
    if not seconds or seconds < 0:
        seconds = 0.0
    millis_total = int(round(seconds * 1000))
    hours, rem = divmod(millis_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write(result: TranscriptionResult, out_path: str) -> str:
    """세그먼트를 SRT 자막 블록으로 써서 out_path 에 저장한다."""
    blocks: list[str] = []
    for idx, seg in enumerate(result.segments, start=1):
        text = (seg.text or "").strip()
        # 화자가 있으면 'speaker: text', 없으면 텍스트만.
        line = f"{seg.speaker}: {text}" if seg.speaker else text

        block = (
            f"{idx}\n"
            f"{_fmt_srt_time(seg.start)} --> {_fmt_srt_time(seg.end)}\n"
            f"{line}\n"
        )
        blocks.append(block)

    # 블록 사이 빈 줄(SRT 규격). BOM 없는 UTF-8.
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(blocks))

    return out_path
