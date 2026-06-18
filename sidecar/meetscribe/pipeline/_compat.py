"""torch 2.6+ 호환 셔틀 — ``torch.load`` 의 weights_only 기본값 변경 대응.

torch 2.6 부터 ``torch.load(weights_only=True)`` 가 기본값이 되면서, pyannote /
whisperx 가 쓰는 VAD·정렬·화자분리 체크포인트(omegaconf.ListConfig 등 **비텐서**
객체를 포함)의 로드가 ``_pickle.UnpicklingError: Weights only load failed`` 로
깨진다(2026-06 실측, torch 2.8.0 + whisperx 3.7.9 + pyannote 3.4.0).

이 모델들은 Hugging Face 공식 배포(신뢰 소스)이므로, ``torch.load`` 를
weights_only=False(2.5 이전 동작)로 되돌려 로드되게 한다. 파이프라인이 어떤 모델을
로드하기 **전에** 1회 적용하면, VAD/STT/정렬/화자분리 전 경로가 함께 해결된다.

대안으로 ``torch.serialization.add_safe_globals([...])`` 가 더 좁은 허용이지만,
필요한 글로벌 목록이 라이브러리 버전마다 달라 취약하다. 신뢰 소스 전제에서
전역 되돌림이 가장 견고하다.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_applied = False


def ensure_torch_load_compat() -> None:
    """``torch.load`` 가 weights_only=False 로 동작하도록 1회 패치한다.

    - torch 미설치/구버전(weights_only 인자 없음)에서도 안전(예외 무시).
    - 멱등: 여러 번 호출해도 한 번만 패치한다(중복 래핑 방지).
    """
    global _applied
    if _applied:
        return
    try:
        import torch  # lazy: 무거운 import 는 호출 시점에만

        orig_load = torch.load
        if getattr(orig_load, "_meetscribe_patched", False):
            _applied = True
            return

        def _patched_load(*args, **kwargs):  # type: ignore[no-untyped-def]
            # 강제 False: pyannote/lightning(cloud_io)은 weights_only=True 를 **명시**
            # 전달하므로 setdefault 로는 못 막는다(TorchVersion 등 글로벌 거부). 신뢰
            # 소스(HF 공식 체크포인트)이므로 항상 False 로 덮어써 2.6+ 안전로더를 우회한다.
            kwargs["weights_only"] = False
            return orig_load(*args, **kwargs)

        _patched_load._meetscribe_patched = True  # type: ignore[attr-defined]
        torch.load = _patched_load  # type: ignore[assignment]
        _applied = True
        logger.info("torch.load 호환 셔틀 적용(weights_only=False) — pyannote/whisperx 체크포인트 로드용.")
    except Exception:  # noqa: BLE001 — 패치 실패가 파이프라인을 막지 않게
        logger.debug("torch.load 호환 셔틀 적용 실패(무시)", exc_info=True)


_sb_applied = False


def ensure_speechbrain_compat() -> None:
    """speechbrain 1.x 의 lazy 모듈(k2_fsa/nlp/numba 등)이 inspect 모듈 순회에서
    강제 import 되며 의존성 누락으로 ``Lazy import ... failed`` 로 깨지는 **근본 원인**을
    고친다(2026-06 실측).

    speechbrain `LazyModule.ensure_module` 은 호출자가 inspect.py 이면 import 를
    건너뛰도록 방어하지만, 그 체크가 ``filename.endswith("/inspect.py")`` 라
    **Unix 경로만** 인식한다. Windows 는 ``...\\inspect.py`` 라 매번 놓쳐서, pyannote
    임베딩 로드 시의 inspect 순회가 모든 lazy 모듈을 강제 import 한다(k2_fsa→nlp→
    numba…). basename 비교로 OS 무관하게 inspect.py 를 인식하도록 ``ensure_module`` 을
    런타임 패치한다. DeprecatedModuleRedirect 는 super().ensure_module 을 부르므로
    함께 고쳐진다(개별 스텁 두더지잡기 불필요)."""
    global _sb_applied
    if _sb_applied:
        return
    try:
        import importlib as _il
        import inspect as _inspect
        import os as _os
        import sys as _sys

        from speechbrain.utils import importutils as _iu

        def _patched_ensure_module(self, stacklevel: int):
            importer_frame = None
            try:
                importer_frame = _inspect.getframeinfo(_sys._getframe(stacklevel + 1))
            except AttributeError:
                pass
            # 원본 endswith("/inspect.py") 는 Windows 경로를 놓친다 → basename 비교로 교체.
            if (
                importer_frame is not None
                and _os.path.basename(importer_frame.filename) == "inspect.py"
            ):
                raise AttributeError()
            if self.lazy_module is None:
                if self.package is None:
                    self.lazy_module = _il.import_module(self.target)
                else:
                    self.lazy_module = _il.import_module(f".{self.target}", self.package)
            return self.lazy_module

        _iu.LazyModule.ensure_module = _patched_ensure_module
        _sb_applied = True
        logger.info("speechbrain LazyModule.ensure_module 패치(Windows inspect.py 인식) 적용.")
    except Exception:  # noqa: BLE001
        logger.debug("speechbrain importutils 패치 실패(무시)", exc_info=True)
