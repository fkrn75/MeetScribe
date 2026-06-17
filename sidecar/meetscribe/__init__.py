"""MeetScribe 사이드카 — 회의 녹음 화자분리 회의록 파이프라인.

완전 로컬(WhisperX) 처리 엔진. FastAPI 로컬 API + SSE 진행률로 Tauri 셸과 통신.
데이터 계약은 :mod:`meetscribe.schemas`, 런타임 설정은 :mod:`meetscribe.config`.
"""

__version__ = "0.1.0"
