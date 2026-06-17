"""F2 전처리 — 임의 포맷 오디오/비디오 → 16kHz mono WAV.

WhisperX/faster-whisper·pyannote 는 16kHz mono PCM 을 표준 입력으로 가정한다.
무거운 디코더 의존성을 파이썬에 끌어들이지 않고, 시스템 ``ffmpeg`` 를
subprocess 로 호출해 변환한다(가장 견고하고 포맷 커버리지가 넓다).

ffmpeg 미설치 환경 대비: 호출 실패 시 명확한 안내 메시지를 담은 예외를 던진다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

# 파이프라인 표준 샘플레이트/채널 (모델 입력 규격)
TARGET_SAMPLE_RATE = 16_000
TARGET_CHANNELS = 1


def ffmpeg_available() -> bool:
    """PATH 에 ffmpeg 실행 파일이 있는지 확인(SystemInfo 용으로도 재사용)."""
    return shutil.which("ffmpeg") is not None


def to_wav(src_path: str, dst_path: str | None = None) -> str:
    """오디오/비디오를 16kHz mono WAV 로 변환하고 그 경로를 반환한다.

    Args:
        src_path: 원본 미디어 경로(any 포맷 — mp4/m4a/mp3/wav 등 ffmpeg 지원 범위).
        dst_path: 출력 WAV 경로. None 이면 임시 파일을 생성한다.
                  (임시 파일 정리는 호출자(runner)가 책임진다.)

    Returns:
        변환된 16kHz mono WAV 파일 경로.

    Raises:
        FileNotFoundError: 원본 파일이 없을 때.
        RuntimeError: ffmpeg 미설치 또는 변환 실패 시.
    """
    if not os.path.isfile(src_path):
        raise FileNotFoundError(f"입력 오디오를 찾을 수 없습니다: {src_path}")

    if not ffmpeg_available():
        raise RuntimeError(
            "ffmpeg 를 찾을 수 없습니다. 오디오 전처리에는 ffmpeg 가 필요합니다. "
            "(설치 후 PATH 에 추가하거나 사이드카에 동봉하세요.)"
        )

    if dst_path is None:
        # 호출자가 정리할 수 있도록 닫힌 임시 파일 경로만 만든다.
        fd, dst_path = tempfile.mkstemp(suffix=".wav", prefix="meetscribe_")
        os.close(fd)

    # -y: 덮어쓰기, -vn: 비디오 트랙 제거, -ac 1: 모노, -ar 16000: 16kHz,
    # -acodec pcm_s16le: 16-bit PCM(WhisperX 표준), -loglevel error: 잡음 억제.
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        src_path,
        "-vn",
        "-ac",
        str(TARGET_CHANNELS),
        "-ar",
        str(TARGET_SAMPLE_RATE),
        "-acodec",
        "pcm_s16le",
        "-loglevel",
        "error",
        dst_path,
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:  # 실행 자체가 불가(권한 등)
        raise RuntimeError(f"ffmpeg 실행에 실패했습니다: {exc}") from exc

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"ffmpeg 변환 실패(코드 {proc.returncode}): {stderr or '알 수 없는 오류'}"
        )

    if not os.path.isfile(dst_path) or os.path.getsize(dst_path) == 0:
        raise RuntimeError("ffmpeg 가 빈 WAV 를 생성했습니다(입력 오디오 트랙 확인 필요).")

    return dst_path
