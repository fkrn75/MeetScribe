"""STT 파이프라인 패키지 — 오디오 → 화자분리 회의록.

데이터 흐름(설계서 §데이터 흐름, CONTRACT 참조):

    audio(any) ──preprocess──▶ wav(16k mono)
       ──transcribe──▶ list[Segment]        (start/end/text, 언어)
       ──align──────▶ list[Segment]         (+words[], 실패 시 원본 유지)
       ──diarize────▶ list[DiarizationTurn] (화자 구간, 청크 처리)
       ──merge──────▶ list[Segment]         (+speaker)
       ──▶ TranscriptionResult

각 단계 모듈은 무거운 ML 의존성(torch·whisperx·pyannote·faster_whisper)을
**함수 내부에서 lazy import** 한다. 이 패키지 최상단에서는 표준 라이브러리와
``meetscribe.*`` 만 import 하므로, ML 패키지 미설치 환경에서도 모듈 로드가 깨지지 않는다.

전체 오케스트레이션(체크포인트·취소·메모리 회수)은 :mod:`meetscribe.pipeline.runner` 가 담당한다.
"""

from __future__ import annotations

from .align import align
from .diarize import diarize
from .merge import assign_speakers
from .preprocess import to_wav
from .runner import run_pipeline
from .transcribe import transcribe

__all__ = [
    "to_wav",
    "transcribe",
    "align",
    "diarize",
    "assign_speakers",
    "run_pipeline",
]
