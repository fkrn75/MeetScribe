"""JSON 내보내기 — 결과 원본을 그대로 직렬화(다른 도구·재처리용).

CONTRACT: result.model_dump_json. Pydantic v2 모델이므로 model_dump_json 으로
스키마 그대로(언어/길이/세그먼트/단어/화자) 보존한다. 표준 라이브러리만 사용한다.
"""

from __future__ import annotations

from ..schemas import TranscriptionResult


def write(result: TranscriptionResult, out_path: str) -> str:
    """TranscriptionResult 를 JSON 으로 직렬화해 out_path 에 저장한다."""
    # indent=2 로 사람이 읽기 좋게. 한글이 \uXXXX 로 깨지지 않도록 Pydantic 기본(ensure_ascii=False).
    payload = result.model_dump_json(indent=2)

    # BOM 없는 UTF-8(브라우저·표준 JSON 파서 호환).
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(payload)

    return out_path
