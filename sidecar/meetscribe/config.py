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


def _env_float(name: str, default: Optional[float] = None) -> Optional[float]:
    """환경변수를 float로 파싱(없거나 형식 오류면 default)."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


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
    # 화자분리는 "단일 처리 우선" — 청크로 쪼개면 경계에서 라벨 정합 오류(다른 사람이
    # 한 화자로 합쳐지거나 같은 사람이 갈라짐)가 생긴다. pyannote 의 segmentation/embedding
    # 은 길이와 무관하게 거의 일정한 GPU 메모리를 쓰고(슬라이딩 배치), 클러스터링만 길이에
    # 비례하는데 소수 화자면 그 비용이 작다(2h ≈ 거리행렬 수십 MB). 그래서 2시간 이하
    # 회의는 통째로 한 번에 돌려 전역 클러스터링으로 라벨 일관성을 보장한다.
    # 이 임계를 넘는 초장시간만 청크로 분할되며, 그 경계는 임베딩 기반 재식별로 잇는다
    # (단순 지배화자 휴리스틱은 다른 사람을 합치므로 폐기 예정 — _stitch_labels 참조).
    diarization_chunk_sec: float = 7200.0  # 2시간 — 이 이하 회의는 단일 처리(청크 경계 없음)
    align_chunk_sec: float = 600.0

    # 화자분리 클러스터링 임계값(고급, 선택). None이면 pyannote 3.1 기본(약 0.705).
    # 낮추면 화자를 더 잘게 나누고(과분할↑), 높이면 더 합친다(과병합↑).
    # 참석자 수를 지정하면(min=max) 클러스터 수가 강제돼 이 값은 거의 영향이 없다 →
    # 인원 미지정 '자동' 모드의 미세조정용. 환경변수 MEETSCRIBE_CLUSTER_THRESHOLD로도 설정.
    clustering_threshold: Optional[float] = field(
        default_factory=lambda: _env_float("MEETSCRIBE_CLUSTER_THRESHOLD")
    )

    # 화자 전환으로 볼 최소 침묵(초) — pyannote segmentation.min_duration_off.
    # 0.0이면 짧은 침묵도 화자 전환으로 봐 과분할 위험. 0.5로 올리면 기침·호흡 같은
    # 짧은 침묵을 무시해 과분할(화자 수 과다 추정)을 억제한다(quality 트랙 권고).
    # 끄려면 환경변수 MEETSCRIBE_MIN_DURATION_OFF=0 으로 설정.
    min_duration_off: float = field(
        default_factory=lambda: float(_env_float("MEETSCRIBE_MIN_DURATION_OFF", 0.5))
    )

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
