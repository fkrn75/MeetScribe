"""FastAPI 로컬 서버 — Tauri 셸이 호출하는 사이드카 API.

CONTRACT T2 엔드포인트
---------------------
    GET  /health                 → {"status":"ok","version":...}
    GET  /system                 → SystemInfo
    POST /jobs        (TranscribeRequest) → {"job_id":...}
    GET  /jobs/{id}              → JobInfo
    GET  /jobs/{id}/events       → SSE 스트림 of ProgressEvent (text/event-stream)
    POST /jobs/{id}/cancel       → 204
    POST /jobs/{id}/export (ExportRequest) → {"out_path":...}

설계 요점
--------
- 같은 PC(로컬) 전제 → 파일 업로드 없이 경로만 받는다. 외부 노출 안 함.
- 무거운 ML import 는 전혀 하지 않는다(torch·whisperx 등). 시스템 정보 조회의
  torch 도 함수 안 lazy import 라 미설치여도 /system 은 동작한다.
- JobManager 는 lifespan 에서 1개 만들어 app.state 에 보관, 메인 이벤트 루프를 주입한다.

기본 포트: 8765 (uvicorn 기동은 __init__.run 또는 외부 실행기에서).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .. import __version__
from ..schemas import (
    ExportRequest,
    JobInfo,
    SystemInfo,
    TranscribeRequest,
)
from .jobs import JobManager


def create_app() -> FastAPI:
    """FastAPI 앱을 구성해 반환한다(uvicorn 진입점이 이걸 import)."""

    # ── lifespan: JobManager 생성 + 이벤트 루프 주입(시작 시) ──
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager = JobManager()
        # 워커 스레드 → SSE 큐 브리지를 위해 메인 루프를 넘긴다.
        manager.set_loop(asyncio.get_running_loop())
        app.state.manager = manager
        yield
        # 종료 훅: 진행 중 작업에 취소 신호(좀비 워커 방지). 데몬 스레드라
        # 프로세스 종료 시 강제 정리되지만, 협조적 중단을 먼저 시도한다.
        manager.cancel_all()

    app = FastAPI(
        title="MeetScribe Sidecar",
        version=__version__,
        summary="로컬 화자분리 회의록 처리 엔진",
        lifespan=lifespan,
    )

    # 프론트(Tauri devUrl / localhost vite)에서의 호출 허용.
    # 로컬 전용이므로 느슨하게 두되, 운영에선 Tauri origin 만 와도 무방.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _manager() -> JobManager:
        return app.state.manager  # type: ignore[no-any-return]

    # ── GET /health ───────────────────────────────────────────
    @app.get("/health")
    async def health() -> dict[str, Any]:
        """헬스 체크 — Tauri 가 사이드카 준비를 폴링한다."""
        return {"status": "ok", "version": __version__}

    # ── GET /system ───────────────────────────────────────────
    @app.get("/system", response_model=SystemInfo)
    async def system() -> SystemInfo:
        """GPU/디바이스/모델 가용성 조회(설정 화면용)."""
        return _collect_system_info(_manager())

    # ── POST /jobs ────────────────────────────────────────────
    @app.post("/jobs")
    async def create_job(req: TranscribeRequest) -> dict[str, str]:
        """전사 작업 제출 → 백그라운드 처리 시작. {"job_id":...} 반환."""
        job_id = _manager().submit(req)
        return {"job_id": job_id}

    # ── GET /jobs/{id} ────────────────────────────────────────
    @app.get("/jobs/{job_id}", response_model=JobInfo)
    async def get_job(job_id: str) -> JobInfo:
        """작업 상태 조회(SSE 를 못 쓰는 폴백·결과 회수용)."""
        try:
            return _manager().get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"작업 없음: {job_id}")

    # ── GET /jobs/{id}/events (SSE) ───────────────────────────
    @app.get("/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request) -> StreamingResponse:
        """진행률 SSE 스트림(text/event-stream). 작업 종료 시 자동 종료."""
        manager = _manager()
        if not manager.exists(job_id):
            raise HTTPException(status_code=404, detail=f"작업 없음: {job_id}")

        async def event_gen():
            # SSE 프레이밍: 각 이벤트를 'data: <json>\n\n' 으로 전송.
            async for ev in manager.subscribe(job_id):
                # 클라이언트가 끊으면 즉시 중단(리소스 회수).
                if await request.is_disconnected():
                    break
                payload = ev.model_dump_json()
                yield f"data: {payload}\n\n"

        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 프록시 버퍼링 방지(혹시 모를 중간 프록시 대비).
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(
            event_gen(), media_type="text/event-stream", headers=headers
        )

    # ── POST /jobs/{id}/cancel ────────────────────────────────
    @app.post("/jobs/{job_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
    async def cancel_job(job_id: str) -> Response:
        """작업 취소 신호. 멱등(이미 끝난 작업도 204)."""
        try:
            _manager().cancel(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"작업 없음: {job_id}")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ── POST /jobs/{id}/export ────────────────────────────────
    @app.post("/jobs/{job_id}/export")
    async def export_job(job_id: str, req: ExportRequest) -> dict[str, str]:
        """완료된 작업 결과를 지정 형식으로 저장 → {"out_path":...}."""
        manager = _manager()
        try:
            info = manager.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"작업 없음: {job_id}")

        if info.result is None:
            raise HTTPException(
                status_code=409,
                detail="아직 결과가 없습니다(작업 미완료 또는 실패).",
            )

        # exporters 는 무거운 의존성이 없다(docx 만 lazy). 동기 I/O 라 짧다.
        from ..exporters import export

        try:
            out_path = export(info.result, req.format, req.out_path)
        except ModuleNotFoundError as exc:
            # python-docx 미설치 등 — 형식 지원 불가를 명확히.
            raise HTTPException(
                status_code=501,
                detail=f"해당 형식 내보내기에 필요한 패키지가 없습니다: {exc.name}",
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"내보내기 실패: {exc}")

        return {"out_path": out_path}

    # ── GET /runtime (torch 온디맨드 상태) ─────────────────────
    @app.get("/runtime")
    async def runtime_status() -> dict[str, Any]:
        """torch 온디맨드 런타임 상태. 프론트가 폴링해 첫 실행 설치 모달을 띄운다.
        torch 는 동결본에 없으므로(인스톨러 축소), 캐시에 받기 전엔 정렬/화자분리가 막힌다.
        STT(faster-whisper/ctranslate2)는 torch 없이도 동작한다."""
        from .. import runtime_torch

        return runtime_torch.state()

    # ── POST /runtime/install (torch 휠 다운로드 시작) ─────────
    @app.post("/runtime/install", status_code=status.HTTP_202_ACCEPTED)
    async def runtime_install() -> dict[str, Any]:
        """torch 휠(약 3.2GB) 다운로드를 백그라운드로 시작(멱등). 진행은 /runtime 폴링."""
        from .. import runtime_torch

        started = runtime_torch.start_install()
        return {"started": started, **runtime_torch.state()}

    return app


def _collect_system_info(manager: JobManager) -> SystemInfo:
    """torch/ffmpeg 가용성을 조사해 SystemInfo 를 만든다(모두 lazy·안전)."""
    from ..schemas import Device

    cfg = manager.config

    device = Device.CPU
    gpu_name: str | None = None
    vram_total_mb: int | None = None
    cuda_available = False

    # torch 는 미설치일 수 있으므로 함수 안에서 시도.
    try:
        import torch  # type: ignore

        cuda_available = bool(torch.cuda.is_available())
        if cuda_available:
            device = Device.CUDA
            try:
                idx = torch.cuda.current_device()
                gpu_name = torch.cuda.get_device_name(idx)
                props = torch.cuda.get_device_properties(idx)
                vram_total_mb = int(props.total_memory // (1024 * 1024))
            except Exception:
                # 이름/메모리 조회 실패는 치명적이지 않음.
                pass
    except Exception:
        # torch 미설치/로드 실패 → CPU 로 보고.
        pass

    # ffmpeg 존재 여부(전처리 필수). 파이프라인의 탐색 로직을 재사용해
    # 판정을 일원화한다(향후 번들 ffmpeg 경로 등 변경 시 자동 일치).
    # lazy import: preprocess 가 무거운 의존성을 끌어오지 않지만 관례상 함수 안에서.
    try:
        from ..pipeline.preprocess import ffmpeg_available as _ffmpeg_available

        ffmpeg_available = _ffmpeg_available()
    except Exception:
        # 파이프라인 모듈 로드 실패 시 안전 폴백(PATH 직접 탐색).
        import shutil

        ffmpeg_available = shutil.which("ffmpeg") is not None

    # HF 토큰(pyannote gated 모델 다운로드용) 보유 여부.
    hf_token_present = bool(cfg.hf_token)

    return SystemInfo(
        device=device,
        gpu_name=gpu_name,
        vram_total_mb=vram_total_mb,
        cuda_available=cuda_available,
        hf_token_present=hf_token_present,
        ffmpeg_available=ffmpeg_available,
    )


# 모듈 전역 app — uvicorn 'meetscribe.api.app:app' 으로 띄울 수 있게.
app = create_app()
