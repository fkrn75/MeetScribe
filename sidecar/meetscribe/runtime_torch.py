"""torch 온디맨드 런타임 로더.

동결본(_internal)에는 torch/torchaudio 본체가 없다(인스톨러 축소: 7.4GB→0.5GB).
첫 실행 시 PyTorch 공식 휠(cu128)을 사용자 캐시(~/.meetscribe/runtime)에 받아
sys.path 앞에 주입해 import 가능하게 한다(모델 가중치를 HF 에서 받는 것과 같은 방식).

공개 함수:
- ensure_on_path(): 캐시가 있으면 sys.path 맨 앞에 넣는다(동결 _internal 보다 우선).
- torch_available(): 캐시/동결 어디서든 torch import 가능 여부.
- start_install(): 백그라운드 스레드로 휠 다운로드+추출 시작(멱등).
- state(): 진행 상태 스냅샷(API/프론트 폴링용).

설계 메모:
- Windows cu128 torch 휠은 CUDA DLL 을 휠 내부에 번들하고 nvidia-* 별도 의존이
  없다(그 의존은 Linux 한정 마커). 따라서 휠 2개(torch 3.22GB + torchaudio)만
  받으면 되고, pip 의존 해소가 필요 없다.
- 휠은 그 자체가 zip → zipfile 로 추출만 하면 import 가능(동결 환경의 pip 호출
  함정을 회피).
- numpy 등 순수 의존은 동결 _internal 에 이미 포함되어 있으므로 캐시엔
  torch/torchaudio 만 둔다(버전은 동결 빌드 때의 .venv 와 동일 → ABI 호환).
"""
from __future__ import annotations

import os
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path

# 캐시 경로. 환경변수 MEETSCRIBE_RUNTIME 로 재정의 가능
# (검증 시 기존 .venv\Lib\site-packages 를 가리키게 해 다운로드 없이 테스트 가능).
RUNTIME_DIR = Path(
    os.environ.get("MEETSCRIBE_RUNTIME") or (Path.home() / ".meetscribe" / "runtime")
)

# cu128 cp312 win_amd64 휠. torch 본체+CUDA DLL 내장, torchaudio 는 동적링크라 작다.
# (Content-Length 실측치 — 진행률 분모로 사용. 정확치 않아도 표시용이라 무방.)
_WHEELS = [
    (
        "torch",
        "https://download.pytorch.org/whl/cu128/torch-2.8.0%2Bcu128-cp312-cp312-win_amd64.whl",
        3_461_384_651,
    ),
    (
        "torchaudio",
        "https://download.pytorch.org/whl/cu128/torchaudio-2.8.0%2Bcu128-cp312-cp312-win_amd64.whl",
        4_672_000,
    ),
]
_TOTAL = sum(w[2] for w in _WHEELS)

_lock = threading.Lock()
_thread: "threading.Thread | None" = None
_state = {
    "stage": "idle",  # idle | downloading | extracting | ready | error
    "downloaded": 0,
    "total": _TOTAL,
    "message": "",
}


def ensure_on_path() -> None:
    """캐시 디렉터리가 있으면 sys.path 맨 앞에 둔다(동결본보다 우선 import)."""
    if RUNTIME_DIR.is_dir():
        p = str(RUNTIME_DIR)
        if p not in sys.path:
            sys.path.insert(0, p)


def torch_available() -> bool:
    """torch 를 import 할 수 있는지(캐시 또는 동결 어디서든)."""
    ensure_on_path()
    try:
        import torch  # noqa: F401

        return True
    except Exception:
        return False


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def state() -> dict:
    """진행 상태 스냅샷(+torch_ready, +progress). API/프론트 폴링용."""
    with _lock:
        s = dict(_state)
    s["torch_ready"] = torch_available()
    s["progress"] = (s["downloaded"] / s["total"]) if s["total"] else 0.0
    return s


def _download_wheel(url: str, dest: Path, base_done: int) -> None:
    """휠 1개를 청크로 받으며 누적 진행률(_state.downloaded)을 갱신한다."""
    req = urllib.request.Request(url, headers={"User-Agent": "MeetScribe"})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as out:
        done = base_done
        while True:
            chunk = resp.read(1024 * 1024)  # 1MB 청크
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            _set(downloaded=done)


def _run_install() -> None:
    """휠들을 받아 RUNTIME_DIR 에 추출한다(백그라운드 스레드 본체)."""
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        base = 0
        for name, url, size in _WHEELS:
            _set(stage="downloading", message=f"{name} 다운로드 중")
            whl = RUNTIME_DIR / f"{name}.whl"
            _download_wheel(url, whl, base)
            base += size
            _set(stage="extracting", message=f"{name} 압축 해제 중")
            with zipfile.ZipFile(whl) as zf:
                zf.extractall(RUNTIME_DIR)
            whl.unlink(missing_ok=True)  # 추출 후 휠(zip) 삭제 — 공간 절약
        # 최종 검증: 새로 받은 torch 가 실제로 import 되는지.
        ensure_on_path()
        import importlib

        importlib.invalidate_caches()
        import torch  # noqa: F401

        _set(stage="ready", message="음성 엔진 준비 완료")
    except Exception as exc:  # noqa: BLE001 — 첫 실행 설치 최상위에서 한 번에 보고
        _set(stage="error", message=f"설치 실패: {exc}")


def start_install() -> bool:
    """백그라운드 설치를 시작한다. 이미 진행 중이면 False(멱등)."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _state.update(stage="downloading", downloaded=0, message="시작 중")
        _thread = threading.Thread(target=_run_install, daemon=True, name="torch-install")
        _thread.start()
    return True
