"""작업 관리 — 큐 + 백그라운드 스레드 + 취소 + 진행률 브로드캐스트.

CONTRACT T2 (JobManager):
    submit(req) -> job_id          # 백그라운드 스레드에서 run_pipeline 실행
    get(job_id) -> JobInfo
    cancel(job_id) -> None         # should_cancel 신호
    subscribe(job_id) -> AsyncIterator[ProgressEvent]   # events SSE 연동

설계 요점
--------
- run_pipeline 은 동기·장시간(CPU/GPU 바운드) 함수다. FastAPI 이벤트 루프를
  막지 않도록 **워커 스레드**에서 돌린다(메인 스레드 차단 금지).
- 파이프라인이 ProgressCb 로 흘리는 (stage, local, msg) 를 events.overall_percent 로
  전체 percent 로 환산해 ProgressEvent 를 만들고, 작업별 구독자(SSE)에게 브로드캐스트한다.
- 취소: cancel() 이 Event 를 set → run_pipeline 에 넘긴 should_cancel() 이 True 를
  반환 → 파이프라인이 협조적으로 중단. 스레드 강제 종료는 하지 않는다(GPU 리소스 누수 방지).
- 무거운 import(torch·whisperx 등)는 직접 하지 않는다. runner import 조차
  **워커 스레드 안에서 lazy import** 해, 미설치 환경에서도 app 로드는 깨지지 않는다.

스레드 ↔ asyncio 경계
--------------------
- 워커는 일반 스레드, SSE 구독자는 asyncio 코루틴이다. 그 사이를 잇기 위해
  작업마다 구독자별 asyncio.Queue 를 두고, 워커 스레드에서
  loop.call_soon_threadsafe 로 안전하게 put 한다. (App 기동 시 set_loop 로
  메인 이벤트 루프를 주입받는다.)
"""

from __future__ import annotations

import asyncio
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from ..config import AppConfig
from ..schemas import (
    JobInfo,
    JobStage,
    JobStatus,
    ProgressEvent,
    TranscribeRequest,
    TranscriptionResult,
)
from .events import overall_percent

# SSE 구독자 큐에 넣는 종료 표식(스트림 끝).
_SENTINEL = object()


@dataclass
class _Job:
    """작업 1건의 서버측 상태(스레드·취소신호·구독자 포함)."""

    job_id: str
    request: TranscribeRequest
    status: JobStatus = JobStatus.PENDING
    stage: JobStage = JobStage.QUEUED
    percent: float = 0.0
    message: str = ""
    error: Optional[str] = None
    result: Optional[TranscriptionResult] = None

    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    # 이 작업을 구독 중인 SSE 큐들(여러 탭이 동시에 볼 수 있음).
    subscribers: list["asyncio.Queue"] = field(default_factory=list)
    finished: bool = False  # done/failed/cancelled 도달 여부(구독 종료 판정)

    def to_info(self) -> JobInfo:
        """현재 상태를 응답 모델(JobInfo)로 스냅샷."""
        return JobInfo(
            job_id=self.job_id,
            status=self.status,
            stage=self.stage,
            percent=self.percent,
            message=self.message,
            error=self.error,
            result=self.result,
        )


class JobManager:
    """작업 큐·워커 스레드·취소·진행률 브로드캐스트의 단일 관리자.

    FastAPI app 의 lifespan 에서 1개 생성해 공유한다(앱 전역 단일 인스턴스).
    """

    def __init__(self, cfg: AppConfig | None = None) -> None:
        # cfg 미지정이면 기본 설정으로 구성(디바이스 자동 감지 포함).
        self._cfg = cfg or AppConfig()
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()  # _jobs·구독자 목록 보호
        # SSE put 을 위해 메인 이벤트 루프를 주입받는다(set_loop).
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── 이벤트 루프 주입 ───────────────────────────────────────
    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """app 기동 시 메인 asyncio 루프를 등록한다(스레드→코루틴 브리지용)."""
        self._loop = loop

    @property
    def config(self) -> AppConfig:
        return self._cfg

    # ── 제출 ──────────────────────────────────────────────────
    def submit(self, req: TranscribeRequest) -> str:
        """작업을 등록하고 워커 스레드를 띄운 뒤 job_id 를 반환한다."""
        job_id = uuid.uuid4().hex
        job = _Job(job_id=job_id, request=req)

        with self._lock:
            self._jobs[job_id] = job

        thread = threading.Thread(
            target=self._run, args=(job,), name=f"meetscribe-job-{job_id[:8]}", daemon=True
        )
        job.thread = thread
        thread.start()
        return job_id

    # ── 조회 ──────────────────────────────────────────────────
    def get(self, job_id: str) -> JobInfo:
        """job_id 의 현재 상태를 반환한다. 없으면 KeyError."""
        job = self._require(job_id)
        return job.to_info()

    def exists(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._jobs

    # ── 취소 ──────────────────────────────────────────────────
    def cancel(self, job_id: str) -> None:
        """취소 신호를 보낸다. 파이프라인이 협조적으로 중단한다."""
        job = self._require(job_id)
        # 이미 끝난 작업은 무시(멱등).
        if job.finished:
            return
        job.cancel_event.set()
        job.message = "취소 요청됨 — 현재 단계 종료 후 중단합니다."

    def cancel_all(self) -> None:
        """진행 중인 모든 작업에 취소 신호(앱 종료 시 좀비 워커 방지)."""
        with self._lock:
            job_ids = list(self._jobs.keys())
        for job_id in job_ids:
            try:
                self.cancel(job_id)
            except KeyError:
                pass

    # ── 구독(SSE) ─────────────────────────────────────────────
    async def subscribe(self, job_id: str) -> AsyncIterator[ProgressEvent]:
        """job_id 진행률을 비동기로 흘려준다. 작업 종료 시 스트림이 닫힌다.

        - 구독 시작 즉시 현재 상태 1건을 먼저 보낸다(늦게 붙은 구독자 동기화).
        - 이미 끝난 작업이면 마지막 상태 1건만 보내고 종료.
        """
        job = self._require(job_id)

        queue: asyncio.Queue = asyncio.Queue()
        # 현재 상태 스냅샷을 첫 이벤트로.
        first = self._make_event(job)

        with self._lock:
            if job.finished:
                # 끝난 작업: 마지막 상태만 주고 끝.
                yield first
                return
            job.subscribers.append(queue)

        try:
            # 첫 스냅샷 전달.
            yield first
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                yield item
        finally:
            # 구독 해제(끊긴 연결 정리).
            with self._lock:
                if queue in job.subscribers:
                    job.subscribers.remove(queue)

    # ── 내부: 워커 본체 ───────────────────────────────────────
    def _run(self, job: _Job) -> None:
        """백그라운드 스레드에서 파이프라인을 실행한다."""
        job.status = JobStatus.RUNNING
        job.stage = JobStage.PREPROCESS
        self._emit(job, JobStage.PREPROCESS, 0.0, "시작합니다.")

        try:
            # lazy import: 미설치 환경에서 app 로드가 깨지지 않도록 여기서 import.
            # (취소 예외 PipelineCancelled 은 아래 except 에서 클래스명으로 식별한다 —
            #  import 자체가 실패하는 경우에도 안전하게 잡기 위함.)
            from ..pipeline.runner import run_pipeline

            # 요청에 model/language 가 있으면 작업별 설정에 반영(원본 cfg 는 보존).
            cfg = self._cfg_for(job.request)

            def progress(stage: JobStage, local: float, msg: str) -> None:
                """파이프라인 → 서버 진행률 콜백(단계·로컬 0~1·메시지)."""
                self._emit(job, stage, local, msg)

            def should_cancel() -> bool:
                return job.cancel_event.is_set()

            result = run_pipeline(
                job.request.audio_path,
                cfg,
                progress=progress,
                min_speakers=job.request.min_speakers,
                max_speakers=job.request.max_speakers,
                should_cancel=should_cancel,
            )

            # run_pipeline 이 취소를 예외 없이 반환할 수도 있으니 신호를 한 번 더 확인.
            if job.cancel_event.is_set():
                self._finish_cancelled(job)
                return

            job.result = result
            self._finish_done(job)

        except Exception as exc:  # noqa: BLE001 — 워커 최후 방어선
            # 취소로 보는 두 경우 모두 CANCELLED 로 마감:
            #   (a) runner 의 PipelineCancelled 예외, (b) 취소 신호가 켜진 상태의 예외.
            # PipelineCancelled 는 try 안에서 lazy import 라 여기선 클래스명으로 식별한다.
            is_cancelled = job.cancel_event.is_set() or type(exc).__name__ == "PipelineCancelled"
            if is_cancelled:
                self._finish_cancelled(job)
            else:
                job.error = f"{type(exc).__name__}: {exc}"
                # 디버깅용 트레이스백은 서버 로그(stderr)로(전체 스택).
                traceback.print_exc()
                self._finish_failed(job, job.error)

    # ── 내부: 상태 전이 + 이벤트 방출 ─────────────────────────
    def _emit(self, job: _Job, stage: JobStage, local: float, msg: str) -> None:
        """진행 중 상태를 갱신하고 구독자에게 ProgressEvent 를 브로드캐스트한다."""
        # 취소 신호가 와 있으면 단계는 갱신하되 메시지로 취소 진행을 알린다.
        job.stage = stage
        job.percent = overall_percent(stage, local)
        if msg:
            job.message = msg
        self._broadcast(self._make_event(job))

    def _finish_done(self, job: _Job) -> None:
        job.status = JobStatus.DONE
        job.stage = JobStage.DONE
        job.percent = 100.0
        job.message = "완료되었습니다."
        self._close(job)

    def _finish_failed(self, job: _Job, error: str) -> None:
        job.status = JobStatus.FAILED
        job.stage = JobStage.FAILED
        job.message = "실패했습니다."
        job.error = error
        self._close(job)

    def _finish_cancelled(self, job: _Job) -> None:
        job.status = JobStatus.CANCELLED
        job.stage = JobStage.CANCELLED
        job.message = "취소되었습니다."
        self._close(job)

    def _close(self, job: _Job) -> None:
        """최종 이벤트를 보내고 모든 구독 스트림을 닫는다."""
        job.finished = True
        self._broadcast(self._make_event(job))
        # 모든 구독자에게 종료 표식 전달.
        with self._lock:
            subs = list(job.subscribers)
        for q in subs:
            self._put_threadsafe(q, _SENTINEL)

    def _make_event(self, job: _Job) -> ProgressEvent:
        """현재 작업 상태로 ProgressEvent 1건 생성."""
        return ProgressEvent(
            job_id=job.job_id,
            stage=job.stage,
            percent=job.percent,
            message=job.message,
        )

    def _broadcast(self, event: ProgressEvent) -> None:
        """이벤트를 모든 구독 큐에 스레드세이프하게 넣는다."""
        with self._lock:
            # job_id 로 해당 작업 구독자만 찾는다.
            job = self._jobs.get(event.job_id)
            subs = list(job.subscribers) if job else []
        for q in subs:
            self._put_threadsafe(q, event)

    def _put_threadsafe(self, q: "asyncio.Queue", item: object) -> None:
        """워커 스레드 → asyncio.Queue 안전 put(메인 루프에 스케줄).

        루프가 아직 주입 안 됐거나 닫혔으면 조용히 무시한다(구독자 없음과 동치).
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(q.put_nowait, item)
        except RuntimeError:
            # 루프 종료 경합 — 무시(앱 종료 중).
            pass

    # ── 내부: 헬퍼 ────────────────────────────────────────────
    def _require(self, job_id: str) -> _Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def _cfg_for(self, req: TranscribeRequest) -> AppConfig:
        """요청값(model/language)을 반영한 작업용 설정을 만든다.

        전역 cfg 를 직접 바꾸면 동시 작업에 간섭하므로 얕은 복제 후 덮어쓴다.
        """
        import copy

        cfg = copy.copy(self._cfg)
        if req.model:
            cfg.model = req.model
        if req.language:
            cfg.language = req.language
        return cfg
