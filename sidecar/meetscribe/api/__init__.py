"""MeetScribe API 패키지 — FastAPI 사이드카 서버.

구성:
- :mod:`meetscribe.api.app`   — FastAPI 라우팅(app 객체 제공)
- :mod:`meetscribe.api.jobs`  — JobManager(작업 큐·백그라운드 스레드·취소)
- :mod:`meetscribe.api.events`— 단계 가중치 진행률 계산

이 패키지 최상단에서는 무거운 ML 의존성을 import 하지 않는다(fastapi/uvicorn 만).
torch·whisperx 등은 실제 작업 실행 시점에 워커 스레드 안에서 lazy import 된다.
"""

from __future__ import annotations

import os

# 사이드카 기본 포트(CONTRACT T3·T4 정합). 환경변수 MEETSCRIBE_PORT 로 재정의 가능.
DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"  # 로컬 전용 — 외부 인터페이스에 바인드하지 않는다.


def get_app():
    """FastAPI app 객체를 반환한다(테스트·외부 실행기용 지연 로드)."""
    from .app import app

    return app


def resolve_port(port: int | None = None) -> int:
    """기동 포트를 결정한다.

    우선순위: 인자 > MEETSCRIBE_PORT > VITE_SIDECAR_PORT > DEFAULT_PORT(8765).

    VITE_SIDECAR_PORT 도 받는 이유: Tauri 셸(T4)이 사이드카 spawn 시 이 이름으로
    포트를 env 주입하고, 동일 값을 Vite 경유로 프론트(T3)에도 노출하는 구성을
    쓸 수 있어 양쪽 이름을 모두 지원한다(정합). MEETSCRIBE_PORT 가 더 우선.
    """
    env_port = os.environ.get("MEETSCRIBE_PORT") or os.environ.get("VITE_SIDECAR_PORT")
    return int(port or env_port or DEFAULT_PORT)


def run(host: str | None = None, port: int | None = None) -> None:
    """사이드카 서버를 기동한다(콘솔 스크립트·PyInstaller 진입점).

    포트는 resolve_port() 규칙(인자 > MEETSCRIBE_PORT > VITE_SIDECAR_PORT > 8765).
    호스트는 보안상 127.0.0.1 고정이 기본(로컬 IPC 성격).
    """
    import uvicorn

    resolved_host = host or os.environ.get("MEETSCRIBE_HOST") or DEFAULT_HOST
    resolved_port = resolve_port(port)

    # app 을 import 경로 문자열이 아닌 객체로 넘긴다(PyInstaller 동결 환경 호환).
    uvicorn.run(get_app(), host=resolved_host, port=resolved_port, log_level="info")


def main() -> int:
    """콘솔 스크립트 진입점(meetscribe-sidecar). 인자 없이 기본 설정으로 기동."""
    run()
    return 0


__all__ = ["run", "main", "get_app", "resolve_port", "DEFAULT_PORT", "DEFAULT_HOST"]
