"""M1 CLI 엔트리 — 오디오 파일 한 건을 처리해 SRT(기본)/콘솔로 출력.

사용 예:
    python -m meetscribe.cli 회의.m4a --srt 회의.srt
    python -m meetscribe.cli 회의.m4a            # SRT 경로 생략 시 입력명+.srt

GUI/사이드카 없이 파이프라인을 단독 검증하기 위한 최소 진입점이다.
진행률은 표준에러로 한 줄씩 출력한다(파이프라인 콜백 연결).

exporters 패키지(T2 담당)가 준비되어 있으면 그쪽 SRT 라이터를 쓰고,
없으면 이 파일의 내장 SRT 포맷터로 폴백한다(팀원 간 구현 순서 비의존).
무거운 import 는 파이프라인 내부에서만 발생하므로 여기 최상단은 표준 라이브러리뿐이다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import AppConfig
from .schemas import JobStage, TranscriptionResult


def main(argv: list[str] | None = None) -> int:
    """CLI 진입점. 성공 0, 실패 비0 반환.

    Args:
        argv: 인자 목록(None 이면 sys.argv[1:] 사용).

    Returns:
        프로세스 종료 코드.
    """
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    src = Path(args.audio)
    if not src.is_file():
        print(f"오류: 입력 파일을 찾을 수 없습니다: {src}", file=sys.stderr)
        return 2

    # 설정 구성(언어/모델은 CLI 인자로 덮어쓰기 가능).
    cfg = AppConfig()
    if args.language:
        cfg.language = args.language
    if args.model:
        cfg.model = args.model

    out_path = Path(args.srt) if args.srt else src.with_suffix(".srt")

    # run_pipeline 은 무거운 import 를 내부에서 수행(여기 최상단은 가볍게 유지).
    from .pipeline.runner import PipelineCancelled, run_pipeline

    try:
        result = run_pipeline(
            str(src),
            cfg,
            progress=_console_progress,
            min_speakers=args.min_speakers,
            max_speakers=args.max_speakers,
            should_cancel=None,
        )
    except PipelineCancelled:
        print("취소되었습니다.", file=sys.stderr)
        return 130
    except FileNotFoundError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — CLI 최상위에서 한 번에 보고
        print(f"파이프라인 실패: {exc}", file=sys.stderr)
        return 1

    _write_srt(result, out_path)
    print(
        f"완료: {out_path} "
        f"(언어={result.language}, 세그먼트={len(result.segments)}, 화자={len(result.speakers)})",
        file=sys.stderr,
    )
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """CLI 인자 파싱."""
    parser = argparse.ArgumentParser(
        prog="meetscribe.cli",
        description="오디오/비디오 → 화자분리 SRT(MeetScribe 파이프라인 단독 실행)",
    )
    parser.add_argument("audio", help="입력 오디오/비디오 파일 경로")
    parser.add_argument("--srt", help="출력 SRT 경로(생략 시 입력명.srt)")
    parser.add_argument("--language", help="강제 언어 코드(기본 ko, 빈값이면 자동감지)")
    parser.add_argument("--model", help="Whisper 모델(기본 large-v3)")
    parser.add_argument("--min-speakers", type=int, default=None, dest="min_speakers",
                        help="최소 화자 수 힌트")
    parser.add_argument("--max-speakers", type=int, default=None, dest="max_speakers",
                        help="최대 화자 수 힌트")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG 로그 출력")
    return parser.parse_args(argv)


def _console_progress(stage: JobStage, local: float, message: str) -> None:
    """진행률 콜백 — 표준에러에 한 줄로 출력."""
    print(f"[{stage.value:>10}] {local * 100:5.1f}%  {message}", file=sys.stderr)


def _write_srt(result: TranscriptionResult, out_path: Path) -> None:
    """결과를 SRT 로 저장. exporters(T2)가 있으면 그쪽을, 없으면 내장 포맷터 사용."""
    # T2 exporters 가 준비되어 있으면 우선 사용(구현 순서 비의존).
    try:
        from .exporters import export  # type: ignore
        from .schemas import ExportFormat

        export(result, ExportFormat.SRT, str(out_path))
        return
    except Exception:  # noqa: BLE001 — 미구현/에러 시 내장 폴백
        pass

    out_path.write_text(_to_srt(result), encoding="utf-8")


def _to_srt(result: TranscriptionResult) -> str:
    """내장 SRT 포맷터(폴백). '화자: 텍스트' 형태로 자막을 만든다."""
    lines: list[str] = []
    for idx, seg in enumerate(result.segments, start=1):
        speaker = f"{seg.speaker}: " if seg.speaker else ""
        lines.append(str(idx))
        lines.append(f"{_srt_ts(seg.start)} --> {_srt_ts(seg.end)}")
        lines.append(f"{speaker}{seg.text}".strip())
        lines.append("")  # 자막 블록 구분 빈 줄
    return "\n".join(lines)


def _srt_ts(seconds: float) -> str:
    """초 → SRT 타임코드(HH:MM:SS,mmm)."""
    if seconds < 0:
        seconds = 0.0
    ms_total = int(round(seconds * 1000))
    hh, ms_total = divmod(ms_total, 3_600_000)
    mm, ms_total = divmod(ms_total, 60_000)
    ss, ms = divmod(ms_total, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


if __name__ == "__main__":
    raise SystemExit(main())
