"""런타임 설정·디바이스 선택 (SSOT — 지휘자 소유, 수정 금지).

장시간(1h+) 안정성과 8GB VRAM 제약(설계서 §10 적대검증)을 코드 기본값에 반영:
- 화자분리/정렬을 청크 단위로 처리(pyannote O(n²)·메모리 폭발 차단)
- 모델 순차 로드/언로드(STT+diar 동시 적재 시 8GB 초과)
- OOM 시 compute_type/batch_size 하향 여지
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .schemas import ComputeType, Device


def detect_device() -> Device:
    """CUDA 사용 가능하면 CUDA, 아니면 CPU. torch 미설치 환경에서도 안전."""
    try:
        import torch  # lazy: 무거운 import는 함수 안에서

        if torch.cuda.is_available():
            return Device.CUDA
    except Exception:
        pass
    return Device.CPU


def default_cache_dir() -> Path:
    """모델 캐시 경로. 환경변수 MEETSCRIBE_CACHE로 재정의 가능."""
    base = os.environ.get("MEETSCRIBE_CACHE") or (Path.home() / ".meetscribe" / "models")
    return Path(base)


@dataclass
class AppConfig:
    """앱 전역 런타임 설정. API 기동 시 1회 구성해 파이프라인에 전달한다."""

    model: str = "large-v3"
    language: str = "ko"
    device: Device = field(default_factory=detect_device)

    # 8GB VRAM: float16 기본. OOM 시 INT8_FLOAT16 → batch_size 하향 순으로 완화.
    compute_type: ComputeType = ComputeType.FLOAT16
    batch_size: int = 8

    # 장시간 처방(설계서 §10): 청크 단위 화자분리·정렬로 메모리 폭발 차단.
    diarization_chunk_sec: float = 600.0  # 10분 단위
    align_chunk_sec: float = 600.0

    # STT와 화자분리 모델을 동시에 올리면 8GB 초과 → 순차 로드/언로드.
    sequential_load: bool = True

    # 적대검증 P0 처방 ③ — 프로세스 격리:
    # 화자분리(pyannote)를 별도 subprocess 에서 돌려, 단계 종료 시 프로세스째
    # 죽여 OS 가 GPU/RAM 을 완전히 회수하게 한다. empty_cache() 로는 못 푸는
    # 메모리 단편화·드라이버 누수까지 차단(설계서 §10, pyannote 3h/23화자 18GB+).
    # 기본 False(in-process). 환경 구축 후 2~3h 장시간에서 OOM 붕괴가 확인되면
    # True 로 켠다. runner 가 이 플래그로 in-process / subprocess 경로를 분기.
    diarize_in_subprocess: bool = field(
        default_factory=lambda: os.environ.get("MEETSCRIBE_DIARIZE_SUBPROCESS", "").lower()
        in ("1", "true", "yes")
    )

    # pyannote는 gated 모델 → HF 토큰 필요(약관 동의용, 무료).
    hf_token: Optional[str] = field(default_factory=lambda: os.environ.get("HF_TOKEN"))

    cache_dir: Path = field(default_factory=default_cache_dir)

    @property
    def torch_device(self) -> str:
        """torch/whisperx에 넘길 디바이스 문자열('cuda'|'cpu')."""
        return self.device.value

    def downgrade_for_oom(self) -> None:
        """OOM 발생 시 점진적 완화: compute_type 하향 → batch_size 반감."""
        if self.compute_type is ComputeType.FLOAT16:
            self.compute_type = ComputeType.INT8_FLOAT16
        elif self.batch_size > 1:
            self.batch_size = max(1, self.batch_size // 2)
