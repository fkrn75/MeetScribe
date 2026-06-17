# MeetScribe 구현 계약서 (팀원 필독)

> 이 문서는 **코드 골격 병렬 구현**을 위한 인터페이스 계약이다. 각 팀원은 자기 담당
> 디렉토리만 만들고, 아래 시그니처·데이터 흐름을 **반드시 준수**한다. 충돌 방지가 목적.

## ⚠️ 전제 (반드시 인지)
- **실행 검증 불가**: 이 PC엔 아직 Rust/Tauri 툴체인과 ML 패키지(torch·whisperx·pyannote)가
  **미설치**, Python도 3.14(ML 휠 부재 위험). 그래서 **이번 산출물은 "돌아갈 수 있게 작성된 코드 골격"**이다.
  - 검증 수준: Python은 `py_compile`(구문)까지, 프론트는 타입/구조까지. **실제 모델 호출 실행은 환경 구축 후.**
  - 무거운 import(`torch`, `whisperx`, `pyannote`, `faster_whisper`, `python-docx`)는 **반드시 함수 내부 lazy import**.
    모듈 최상단에서 import하면 미설치 환경에서 `py_compile`은 통과해도 모듈 로드가 깨진다. 최상단은 표준 라이브러리와 `meetscribe.*`만.
- **schemas.py / config.py 는 읽기 전용**(지휘자 SSOT). import해서 쓰되 수정하지 말 것.
- 한국어 주석. 기존 문서(설계서/기능명세서) 톤 유지.

## 📁 디렉토리 구조 & 담당
```
MeetScribe/
├── sidecar/                         # Python 사이드카
│   ├── meetscribe/
│   │   ├── __init__.py   ✅지휘자
│   │   ├── schemas.py    ✅지휘자 (SSOT, 읽기전용)
│   │   ├── config.py     ✅지휘자 (SSOT, 읽기전용)
│   │   ├── pipeline/      ── T1(pipeline)
│   │   │   ├── __init__.py
│   │   │   ├── preprocess.py   # F2 ffmpeg → 16kHz mono WAV
│   │   │   ├── transcribe.py   # F3 faster-whisper STT
│   │   │   ├── align.py        # F5 wav2vec2(ko) 단어 정렬
│   │   │   ├── diarize.py      # F4 pyannote 화자분리 (청크 단위)
│   │   │   ├── merge.py        # F5 화자↔단어 병합
│   │   │   └── runner.py       # 전체 오케스트레이션·체크포인트·프로세스격리 훅
│   │   ├── api/           ── T2(api)
│   │   │   ├── __init__.py
│   │   │   ├── app.py          # FastAPI 라우팅
│   │   │   ├── jobs.py         # 작업 큐·백그라운드 스레드·취소
│   │   │   └── events.py       # SSE 진행률(단계 가중치)
│   │   ├── exporters/     ── T2(api)
│   │   │   ├── __init__.py     # export(result, fmt, dst) 디스패치
│   │   │   ├── txt.py · srt.py · docx_.py · json_.py
│   │   └── cli.py        ── T1(pipeline)  # M1 CLI 엔트리(파일→SRT)
│   ├── requirements.txt  ── T2(api)
│   └── pyproject.toml    ── T2(api)       # PyInstaller 진입점 포함
├── src/                  ── T3(frontend, Svelte+TS)
│   ├── main.ts · App.svelte · app.css
│   └── lib/
│       ├── api.ts            # 사이드카 호출·SSE 구독
│       ├── stores.ts         # job/transcript/speakers 상태
│       └── components/       # Dropzone · ProgressBar · TranscriptView · SpeakerPanel · Toolbar (.svelte)
├── index.html · package.json · vite.config.ts · svelte.config.js · tsconfig.json   ── T3
└── src-tauri/            ── T4(tauri, Rust)
    ├── src/main.rs           # 사이드카 spawn/health/kill lifecycle
    ├── tauri.conf.json       # externalBin, 권한
    ├── capabilities/default.json
    ├── Cargo.toml · build.rs
```
**규칙: 위 표의 담당 외 파일은 건드리지 않는다.** 루트 `README.md`/`.gitignore`/`docs/`는 지휘자 소유.

## 🔁 데이터 흐름 (파이프라인)
```
audio(any) ──preprocess──▶ wav(16k mono)
   ──transcribe──▶ list[Segment]              (start/end/text, 언어)
   ──align──────▶ list[Segment]               (+words[], 실패 시 원본 유지)
   ──diarize────▶ list[DiarizationTurn]       (화자 구간, 청크 처리)
   ──merge──────▶ list[Segment]               (+speaker)
   ──▶ TranscriptionResult ──export──▶ txt/srt/docx/json
```

## 🧩 인터페이스 시그니처 (계약 — 이대로 구현)
`ProgressCb = Callable[[JobStage, float, str], None]` (단계·로컬진행률0~1·메시지). 모두 선택 인자, 기본 None.

### T1 — pipeline
```python
# preprocess.py
def to_wav(src_path: str, dst_path: str | None = None) -> str: ...   # 16kHz mono WAV 경로 반환(ffmpeg subprocess)

# transcribe.py
def transcribe(wav_path: str, cfg: AppConfig, progress: ProgressCb | None = None) -> tuple[list[Segment], str]: ...
#   faster-whisper, VAD, condition_on_previous_text=False. 반환: (segments, language)

# align.py
def align(segments: list[Segment], wav_path: str, language: str, cfg: AppConfig,
          progress: ProgressCb | None = None) -> list[Segment]: ...
#   whisperx 한국어 wav2vec2 정렬. 실패 시 입력 segments 그대로 반환(graceful degrade) + 경고 로그.

# diarize.py
def diarize(wav_path: str, cfg: AppConfig, min_speakers: int | None = None,
            max_speakers: int | None = None, progress: ProgressCb | None = None) -> list[DiarizationTurn]: ...
#   pyannote 3.1. cfg.diarization_chunk_sec 단위 청크 처리 후 화자 라벨 정합·병합.

# merge.py
def assign_speakers(segments: list[Segment], turns: list[DiarizationTurn]) -> list[Segment]: ...
#   각 단어/세그먼트에 최대 겹침 화자 부여. 겹침 없으면 인접 turn.

# runner.py  ← 전체 조립 + 장시간 처방
def run_pipeline(audio_path: str, cfg: AppConfig, progress: ProgressCb | None = None,
                 min_speakers: int | None = None, max_speakers: int | None = None,
                 should_cancel: Callable[[], bool] | None = None) -> TranscriptionResult: ...
#   단계 순서대로 호출. 단계별 체크포인트(중간 산출 임시 저장), should_cancel() 주기 확인,
#   임시 WAV·GPU 메모리(torch.cuda.empty_cache) 회수. cfg.sequential_load면 단계마다 모델 해제.

# cli.py
def main(argv: list[str] | None = None) -> int: ...   # `python -m meetscribe.cli <audio> [--srt out]`
```

### T2 — api / exporters
```python
# jobs.py
class JobManager:
    def submit(self, req: TranscribeRequest) -> str: ...        # job_id 반환, 백그라운드 스레드에서 run_pipeline
    def get(self, job_id: str) -> JobInfo: ...
    def cancel(self, job_id: str) -> None: ...                  # should_cancel 신호
    def subscribe(self, job_id: str) -> "AsyncIterator[ProgressEvent]": ...  # events.py와 연동

# events.py — 단계 가중치(합 100). 전체 percent = 누적가중 + 단계내 로컬진행*가중
STAGE_WEIGHTS = {PREPROCESS:5, TRANSCRIBE:45, ALIGN:20, DIARIZE:25, MERGE:5}
def overall_percent(stage: JobStage, local: float) -> float: ...

# app.py (FastAPI) — 엔드포인트
#   GET  /health                 → {"status":"ok","version":...}
#   GET  /system                 → SystemInfo
#   POST /jobs        (TranscribeRequest) → {"job_id":...}
#   GET  /jobs/{id}              → JobInfo
#   GET  /jobs/{id}/events       → SSE 스트림 of ProgressEvent (text/event-stream)
#   POST /jobs/{id}/cancel       → 204
#   POST /jobs/{id}/export (ExportRequest) → {"out_path":...}

# exporters/__init__.py
def export(result: TranscriptionResult, fmt: ExportFormat, out_path: str) -> str: ...   # 형식별 디스패치
```
**txt**: `[mm:ss] 화자: 텍스트` · **srt**: 자막(화자 prefix) · **docx**: python-docx(화자별 문단) · **json**: result.model_dump_json.

### T3 — frontend api.ts (사이드카 base URL = `http://127.0.0.1:<PORT>`, 기본 8765; `VITE_SIDECAR_PORT`로 주입)
```ts
createJob(req): Promise<{job_id: string}>
getJob(id): Promise<JobInfo>
cancelJob(id): Promise<void>
exportJob(id, format, outPath): Promise<{out_path: string}>
getSystem(): Promise<SystemInfo>
subscribeProgress(id, onEvent: (e: ProgressEvent)=>void): EventSource   // GET /jobs/{id}/events
```
TS 타입(JobInfo/Segment/ProgressEvent/SystemInfo/ExportFormat)은 `src/lib/types.ts`에 schemas.py와 1:1로 미러링.

### T4 — tauri
- `main.rs`: 앱 시작 시 `sidecar/`(externalBin) spawn → `/health` 폴링 → 준비되면 메인 윈도우. 앱 종료 시 사이드카 kill(좀비 방지). 포트는 환경변수로 프론트에 전달.
- `tauri.conf.json`: `bundle.externalBin`에 사이드카 바이너리, `build.devUrl`/`frontendDist`는 vite. CSP에서 `127.0.0.1:<port>` 허용.
- Tauri 2 권한: `capabilities/default.json`.

## ✅ 완료 기준 (각 팀원)
1. 담당 파일 전부 작성 + 한국어 주석.
2. Python 담당은 **`python -m py_compile <각 .py>` 통과**(구문). lazy import 규칙 준수.
3. 인터페이스 시그니처가 이 계약과 일치(타 팀원이 import해도 깨지지 않게).
4. 경계가 닿는 팀원과 **SendMessage로 1회 이상 정합 확인**(T1↔T2 schemas 사용, T2↔T3 엔드포인트/필드명, T3↔T4 포트 주입).
5. 끝나면 자기 TaskUpdate를 completed로 + team-lead에게 **핵심 요약(파일 목록 + 막힌 점)** 보고.
