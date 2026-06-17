"""파이프라인 오케스트레이션 — 전 단계를 순서대로 조립 + 장시간 안정성 처방.

CONTRACT T1 의 ``run_pipeline`` 진입점. 단계 순서:

    preprocess → transcribe → align → diarize → merge → TranscriptionResult

장시간(1h+) 안정성 처방(설계서 §10 적대검증, team-lead 지시):
  ① 청크 단위 화자분리/정렬 — 각 단계 모듈이 cfg.*_chunk_sec 로 자체 처리.
  ② 단계별 체크포인트 — 중간 산출을 임시 JSON 으로 저장해 재개(resume) 가능.
  ③ should_cancel() 주기 확인 — 단계 경계마다 검사, 신호 시 즉시 중단·정리.
  ④ 모델 해제 + torch.cuda.empty_cache() — 각 단계 종료 후(cfg.sequential_load).
  ⑤ 임시 WAV 정리 — finally 에서 항상 제거.

무거운 import(torch)는 메모리 회수 함수 안에서만 lazy import.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from ..config import AppConfig
from ..schemas import (
    DiarizationTurn,
    JobStage,
    Segment,
    TranscriptionResult,
)
from . import align as align_mod
from . import diarize as diarize_mod
from . import merge as merge_mod
from . import preprocess as preprocess_mod
from . import transcribe as transcribe_mod

logger = logging.getLogger(__name__)

# 진행률 콜백 타입(문서용). CONTRACT: Callable[[JobStage, float, str], None]
ProgressCb = Callable[[JobStage, float, str], None]
CancelCb = Callable[[], bool]


class PipelineCancelled(Exception):
    """should_cancel() 신호로 파이프라인이 중단되었음을 알리는 예외."""


def run_pipeline(
    audio_path: str,
    cfg: AppConfig,
    progress: ProgressCb | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    should_cancel: CancelCb | None = None,
) -> TranscriptionResult:
    """오디오 한 건을 끝까지 처리해 TranscriptionResult 를 반환한다.

    Args:
        audio_path: 원본 미디어 경로(any 포맷).
        cfg: 런타임 설정(SSOT). 이 함수는 cfg 를 수정하지 않는다.
        progress: 단계·로컬진행률(0~1)·메시지 콜백(선택).
        min_speakers / max_speakers: 화자 수 힌트(선택).
        should_cancel: 주기적으로 호출해 True 면 즉시 중단(선택).

    Returns:
        TranscriptionResult(language·duration·segments·speakers·audio_path).

    Raises:
        PipelineCancelled: 취소 신호를 받은 경우.
        FileNotFoundError / RuntimeError: 입력 부재·ffmpeg/모델 실패 등.
    """
    started = time.monotonic()
    # 체크포인트·임시 WAV 를 모아 둘 작업 디렉토리(작업별 격리, 끝나면 정리).
    work_dir = Path(tempfile.mkdtemp(prefix="meetscribe_job_"))
    wav_path: str | None = None

    try:
        _check_cancel(should_cancel)

        # ── 1) 전처리: any → 16kHz mono WAV ──────────────────────────────
        if progress:
            progress(JobStage.PREPROCESS, 0.0, "오디오 전처리 중")
        wav_path = preprocess_mod.to_wav(audio_path, str(work_dir / "audio_16k.wav"))
        if progress:
            progress(JobStage.PREPROCESS, 1.0, "전처리 완료")
        _check_cancel(should_cancel)

        # ── 2) STT(faster-whisper) ───────────────────────────────────────
        segments, language = _stage_transcribe(wav_path, cfg, progress, work_dir, should_cancel)
        _check_cancel(should_cancel)

        # ── 3) 정렬(whisperx wav2vec2) — 실패해도 graceful degrade ────────
        segments = _stage_align(
            segments, wav_path, language, cfg, progress, work_dir, should_cancel
        )
        _check_cancel(should_cancel)

        # ── 4) 화자분리(pyannote, 청크) ──────────────────────────────────
        turns = _stage_diarize(
            wav_path, cfg, min_speakers, max_speakers, progress, work_dir, should_cancel
        )
        _check_cancel(should_cancel)

        # ── 5) 병합(화자 부여) ───────────────────────────────────────────
        if progress:
            progress(JobStage.MERGE, 0.0, "화자 병합 중")
        segments = merge_mod.assign_speakers(segments, turns)
        if progress:
            progress(JobStage.MERGE, 1.0, "화자 병합 완료")

        duration = _result_duration(segments, wav_path)
        speakers = _collect_speakers(segments, turns)

        result = TranscriptionResult(
            language=language,
            duration=duration,
            segments=segments,
            speakers=speakers,
            audio_path=audio_path,
        )

        elapsed = time.monotonic() - started
        logger.info(
            "파이프라인 완료: %.1fs, 세그먼트 %d, 화자 %d",
            elapsed,
            len(segments),
            len(speakers),
        )
        if progress:
            progress(JobStage.DONE, 1.0, "완료")
        return result

    finally:
        # ⑤ 임시 WAV·체크포인트·작업 디렉토리 정리(취소·실패 시에도 항상).
        _cleanup_dir(work_dir)


# ─────────────────────────────────────────────────────────────
# 단계 래퍼 — 체크포인트 저장 + 단계 종료 후 메모리 회수
# ─────────────────────────────────────────────────────────────
def _stage_transcribe(
    wav_path: str,
    cfg: AppConfig,
    progress: ProgressCb | None,
    work_dir: Path,
    should_cancel: CancelCb | None,
) -> tuple[list[Segment], str]:
    """STT 단계. 체크포인트가 있으면 재사용, 끝나면 모델 메모리 회수."""
    ckpt = work_dir / "01_transcribe.json"
    cached = _load_segments_checkpoint(ckpt)
    if cached is not None:
        logger.info("STT 체크포인트 재사용: %s", ckpt)
        return cached.segments, cached.language

    try:
        segments, language = transcribe_mod.transcribe(wav_path, cfg, progress)
    finally:
        # ④ 단계 종료 후 GPU 메모리 회수(sequential_load 시 다음 단계 적재 공간 확보).
        _release_memory(cfg)

    _save_segments_checkpoint(ckpt, segments, language)
    return segments, language


def _stage_align(
    segments: list[Segment],
    wav_path: str,
    language: str,
    cfg: AppConfig,
    progress: ProgressCb | None,
    work_dir: Path,
    should_cancel: CancelCb | None,
) -> list[Segment]:
    """정렬 단계. 실패는 align 모듈이 내부에서 흡수(원본 반환)."""
    ckpt = work_dir / "02_align.json"
    cached = _load_segments_checkpoint(ckpt)
    if cached is not None:
        logger.info("정렬 체크포인트 재사용: %s", ckpt)
        return cached.segments

    try:
        aligned = align_mod.align(segments, wav_path, language, cfg, progress)
    finally:
        _release_memory(cfg)

    _save_segments_checkpoint(ckpt, aligned, language)
    return aligned


def _stage_diarize(
    wav_path: str,
    cfg: AppConfig,
    min_speakers: int | None,
    max_speakers: int | None,
    progress: ProgressCb | None,
    work_dir: Path,
    should_cancel: CancelCb | None,
) -> list[DiarizationTurn]:
    """화자분리 단계(청크). 끝나면 모델 메모리 회수."""
    ckpt = work_dir / "03_diarize.json"
    cached = _load_turns_checkpoint(ckpt)
    if cached is not None:
        logger.info("화자분리 체크포인트 재사용: %s", ckpt)
        return cached

    try:
        turns = diarize_mod.diarize(wav_path, cfg, min_speakers, max_speakers, progress)
    finally:
        _release_memory(cfg)

    _save_turns_checkpoint(ckpt, turns)
    return turns


# ─────────────────────────────────────────────────────────────
# ③ 취소 / ④ 메모리 회수
# ─────────────────────────────────────────────────────────────
def _check_cancel(should_cancel: CancelCb | None) -> None:
    """취소 신호 확인 — True 면 PipelineCancelled 를 던져 즉시 중단한다.

    단계 경계마다 호출한다(단계 내부 세밀 취소는 각 모듈 진행 콜백에 위임).
    """
    if should_cancel is not None and should_cancel():
        logger.info("취소 신호 감지 — 파이프라인을 중단합니다.")
        raise PipelineCancelled("사용자 취소")


def _release_memory(cfg: AppConfig) -> None:
    """단계 종료 후 GPU 캐시를 비운다(cfg.sequential_load 시 OOM 예방).

    torch 미설치/CPU 환경에서도 안전(예외 무시). 무거운 import 는 여기서 lazy.
    """
    if not cfg.sequential_load:
        return
    try:
        import gc

        import torch  # lazy

        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
    except Exception:  # noqa: BLE001 — 메모리 회수 실패는 치명적이지 않음
        pass


# ─────────────────────────────────────────────────────────────
# ② 체크포인트 입출력 (재개 가능)
# ─────────────────────────────────────────────────────────────
class _SegmentsCheckpoint:
    """세그먼트 체크포인트 역직렬화 결과(language 동봉)."""

    __slots__ = ("segments", "language")

    def __init__(self, segments: list[Segment], language: str) -> None:
        self.segments = segments
        self.language = language


def _save_segments_checkpoint(path: Path, segments: list[Segment], language: str) -> None:
    """세그먼트+언어를 임시 JSON 으로 저장한다(원자적 교체)."""
    payload = {
        "language": language,
        "segments": [s.model_dump() for s in segments],
    }
    _atomic_write_json(path, payload)


def _load_segments_checkpoint(path: Path) -> _SegmentsCheckpoint | None:
    """세그먼트 체크포인트를 읽는다. 없거나 손상 시 None(재계산)."""
    data = _read_json(path)
    if not data:
        return None
    try:
        segments = [Segment.model_validate(s) for s in data.get("segments", [])]
        language = str(data.get("language", "ko"))
        return _SegmentsCheckpoint(segments, language)
    except Exception:  # noqa: BLE001 — 손상 체크포인트는 무시하고 재계산
        logger.warning("손상된 세그먼트 체크포인트 무시: %s", path)
        return None


def _save_turns_checkpoint(path: Path, turns: list[DiarizationTurn]) -> None:
    """화자 turn 체크포인트 저장."""
    _atomic_write_json(path, {"turns": [t.model_dump() for t in turns]})


def _load_turns_checkpoint(path: Path) -> list[DiarizationTurn] | None:
    """화자 turn 체크포인트 로드. 없거나 손상 시 None."""
    data = _read_json(path)
    if not data:
        return None
    try:
        return [DiarizationTurn.model_validate(t) for t in data.get("turns", [])]
    except Exception:  # noqa: BLE001
        logger.warning("손상된 화자분리 체크포인트 무시: %s", path)
        return None


def _atomic_write_json(path: Path, payload: dict) -> None:
    """임시 파일에 쓰고 교체 — 중간에 죽어도 부분 파일이 남지 않게."""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        # BOM 없는 UTF-8(다른 도구가 읽어도 안전).
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        # 체크포인트는 최적화일 뿐 — 실패해도 파이프라인은 계속.
        logger.warning("체크포인트 쓰기 실패(%s): %s", path, exc)


def _read_json(path: Path) -> dict | None:
    """JSON 읽기. 파일 없음/파싱 오류는 None."""
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────
# 결과 메타 계산
# ─────────────────────────────────────────────────────────────
def _result_duration(segments: list[Segment], wav_path: str | None) -> float:
    """결과 길이(초). WAV 길이 우선, 실패 시 마지막 세그먼트 끝 시각."""
    dur = diarize_mod._audio_duration_sec(wav_path) if wav_path else 0.0
    if dur > 0:
        return dur
    return max((s.end for s in segments), default=0.0)


def _collect_speakers(segments: list[Segment], turns: list[DiarizationTurn]) -> list[str]:
    """등장 화자 목록(정렬). 세그먼트 화자 우선, 비면 turn 라벨로 보완."""
    labels = {s.speaker for s in segments if s.speaker}
    if not labels:
        labels = {t.speaker for t in turns}
    return sorted(labels)


def _cleanup_dir(work_dir: Path) -> None:
    """작업 디렉토리(임시 WAV·체크포인트) 전체 삭제. 실패는 경고만."""
    import shutil

    try:
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("작업 디렉토리 정리 실패(%s): %s", work_dir, exc)
