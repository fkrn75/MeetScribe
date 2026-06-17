"""내보내기 디스패치 — TranscriptionResult → txt/srt/docx/json 파일.

CONTRACT T2:
    export(result, fmt, out_path) -> str   # 형식별 디스패치, 저장 경로 반환

각 형식 구현은 형제 모듈(txt/srt/docx_/json_)에 있다. docx 만 외부 패키지
(python-docx)에 의존하므로 해당 모듈 안에서 lazy import 한다 — 이 패키지를
import 하는 것만으로 python-docx 가 필요해지지는 않는다.
"""

from __future__ import annotations

from ..schemas import ExportFormat, TranscriptionResult
from . import docx_, json_, srt, txt

# 형식 → 작성 함수 매핑. 각 writer 시그니처: (result, out_path) -> str
_WRITERS = {
    ExportFormat.TXT: txt.write,
    ExportFormat.SRT: srt.write,
    ExportFormat.DOCX: docx_.write,
    ExportFormat.JSON: json_.write,
}


def export(result: TranscriptionResult, fmt: ExportFormat, out_path: str) -> str:
    """결과를 지정 형식으로 out_path 에 저장하고 그 경로를 반환한다.

    fmt 는 ExportFormat enum. 문자열("txt" 등)이 들어와도 받아주도록 보정한다.
    """
    # 호출자가 문자열을 넘겨도 동작하도록 enum 으로 정규화한다.
    if not isinstance(fmt, ExportFormat):
        fmt = ExportFormat(str(fmt).lower())

    writer = _WRITERS.get(fmt)
    if writer is None:  # enum 범위 밖은 들어올 수 없지만 방어적으로 처리
        raise ValueError(f"지원하지 않는 내보내기 형식: {fmt}")

    return writer(result, out_path)


__all__ = ["export"]
