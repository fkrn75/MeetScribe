"""MeetScribe 데이터 계약 (SSOT — Single Source of Truth).

파이프라인 단계 간, 그리고 API ↔ 프론트엔드 간 주고받는 모든 데이터 구조를
여기 한 곳에서 정의한다. 모든 팀원/모듈은 이 모델을 **import해서만** 쓰고,
이 파일은 수정하지 않는다(지휘자 소유). Pydantic v2 기준.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────
# 런타임 enum
# ─────────────────────────────────────────────────────────────
class Device(str, Enum):
    """추론 디바이스."""

    CUDA = "cuda"
    CPU = "cpu"


class ComputeType(str, Enum):
    """faster-whisper compute_type. 8GB VRAM OOM 시 INT8_FLOAT16으로 하향."""

    FLOAT16 = "float16"
    INT8_FLOAT16 = "int8_float16"
    INT8 = "int8"
    FLOAT32 = "float32"


# ─────────────────────────────────────────────────────────────
# 파이프라인 산출 데이터
# ─────────────────────────────────────────────────────────────
class Word(BaseModel):
    """단어 단위 타임스탬프 (정렬 단계 산출).

    정렬에 실패한 단어는 start/end가 None일 수 있다(graceful degrade).
    """

    word: str
    start: Optional[float] = None
    end: Optional[float] = None
    score: Optional[float] = None
    speaker: Optional[str] = None


class Segment(BaseModel):
    """발화 세그먼트. 단계가 진행될수록 필드가 채워진다.

    - STT 직후: start/end/text (speaker=None, words=[])
    - 정렬 후: words 채워짐
    - 병합 후: speaker 부여됨
    """

    start: float
    end: float
    text: str
    speaker: Optional[str] = None
    words: list[Word] = Field(default_factory=list)


class DiarizationTurn(BaseModel):
    """화자분리 산출 — 누가 언제 말했는가(텍스트 무관)."""

    speaker: str
    start: float
    end: float


class TranscriptionResult(BaseModel):
    """파이프라인 최종 산출(= 회의록 원본 데이터)."""

    language: str
    duration: float
    segments: list[Segment]
    speakers: list[str] = Field(default_factory=list)
    audio_path: Optional[str] = None


# ─────────────────────────────────────────────────────────────
# 작업/진행률 (API ↔ 프론트)
# ─────────────────────────────────────────────────────────────
class JobStage(str, Enum):
    """파이프라인 단계. 진행률 가중치의 키이기도 하다(events.py)."""

    QUEUED = "queued"
    PREPROCESS = "preprocess"
    TRANSCRIBE = "transcribe"
    ALIGN = "align"
    DIARIZE = "diarize"
    MERGE = "merge"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStatus(str, Enum):
    """작업 전체 상태."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProgressEvent(BaseModel):
    """SSE로 푸시되는 진행률 이벤트 1건."""

    job_id: str
    stage: JobStage
    percent: float = 0.0  # 0~100, 전체 진행률
    message: str = ""
    eta_seconds: Optional[float] = None


# ─────────────────────────────────────────────────────────────
# API 요청/응답 모델
# ─────────────────────────────────────────────────────────────
class TranscribeRequest(BaseModel):
    """POST /jobs 요청 바디. 파일은 같은 PC이므로 경로만 전달(업로드 X)."""

    audio_path: str
    language: str = "ko"
    model: str = "large-v3"
    min_speakers: Optional[int] = None
    max_speakers: Optional[int] = None


class JobInfo(BaseModel):
    """GET /jobs/{id} 응답."""

    job_id: str
    status: JobStatus
    stage: JobStage = JobStage.QUEUED
    percent: float = 0.0
    message: str = ""
    error: Optional[str] = None
    result: Optional[TranscriptionResult] = None


class SystemInfo(BaseModel):
    """GET /system 응답 — GPU/디바이스/모델 가용성."""

    device: Device
    gpu_name: Optional[str] = None
    vram_total_mb: Optional[int] = None
    cuda_available: bool = False
    hf_token_present: bool = False
    ffmpeg_available: bool = False


class ExportFormat(str, Enum):
    """내보내기 형식."""

    TXT = "txt"
    SRT = "srt"
    DOCX = "docx"
    JSON = "json"


class ExportRequest(BaseModel):
    """POST /jobs/{id}/export 요청."""

    format: ExportFormat
    out_path: str
