/**
 * MeetScribe 데이터 계약 (TS 미러).
 *
 * 사이드카 `sidecar/meetscribe/schemas.py` (SSOT, Pydantic v2)를 TypeScript로
 * **1:1 미러링**한 것이다. 백엔드 모델이 바뀌면 이 파일도 같이 바꾼다.
 * 필드명은 백엔드 JSON 그대로(snake_case)를 유지한다 — 변환하지 않는다.
 */

// ─────────────────────────────────────────────────────────────
// 런타임 enum (TS에서는 문자열 유니온 + const 객체로 표현)
// ─────────────────────────────────────────────────────────────

/** 추론 디바이스. schemas.Device */
export type Device = "cuda" | "cpu";

/** faster-whisper compute_type. schemas.ComputeType */
export type ComputeType = "float16" | "int8_float16" | "int8" | "float32";

/**
 * 파이프라인 단계. 진행률 가중치의 키이기도 하다(events.py).
 * schemas.JobStage
 */
export type JobStage =
  | "queued"
  | "preprocess"
  | "transcribe"
  | "align"
  | "diarize"
  | "merge"
  | "done"
  | "failed"
  | "cancelled";

/** JobStage 값 상수(스위치/표시용). */
export const JobStage = {
  QUEUED: "queued",
  PREPROCESS: "preprocess",
  TRANSCRIBE: "transcribe",
  ALIGN: "align",
  DIARIZE: "diarize",
  MERGE: "merge",
  DONE: "done",
  FAILED: "failed",
  CANCELLED: "cancelled",
} as const satisfies Record<string, JobStage>;

/** 작업 전체 상태. schemas.JobStatus */
export type JobStatus = "pending" | "running" | "done" | "failed" | "cancelled";

/** JobStatus 값 상수. */
export const JobStatus = {
  PENDING: "pending",
  RUNNING: "running",
  DONE: "done",
  FAILED: "failed",
  CANCELLED: "cancelled",
} as const satisfies Record<string, JobStatus>;

/** 내보내기 형식. schemas.ExportFormat */
export type ExportFormat = "txt" | "srt" | "docx" | "json";

/** ExportFormat 값 상수(툴바 버튼 순회용). */
export const ExportFormat = {
  TXT: "txt",
  SRT: "srt",
  DOCX: "docx",
  JSON: "json",
} as const satisfies Record<string, ExportFormat>;

// ─────────────────────────────────────────────────────────────
// 파이프라인 산출 데이터
// ─────────────────────────────────────────────────────────────

/**
 * 단어 단위 타임스탬프 (정렬 단계 산출). schemas.Word
 * 정렬 실패 단어는 start/end가 null일 수 있다(graceful degrade).
 */
export interface Word {
  word: string;
  start: number | null;
  end: number | null;
  score: number | null;
  speaker: string | null;
}

/**
 * 발화 세그먼트. 단계가 진행될수록 필드가 채워진다. schemas.Segment
 * - STT 직후: start/end/text (speaker=null, words=[])
 * - 정렬 후: words 채워짐
 * - 병합 후: speaker 부여됨
 */
export interface Segment {
  start: number;
  end: number;
  text: string;
  speaker: string | null;
  words: Word[];
}

/** 화자분리 산출 — 누가 언제 말했는가(텍스트 무관). schemas.DiarizationTurn */
export interface DiarizationTurn {
  speaker: string;
  start: number;
  end: number;
}

/** 파이프라인 최종 산출(= 회의록 원본 데이터). schemas.TranscriptionResult */
export interface TranscriptionResult {
  language: string;
  duration: number;
  segments: Segment[];
  speakers: string[];
  audio_path: string | null;
}

// ─────────────────────────────────────────────────────────────
// 작업/진행률 (API ↔ 프론트)
// ─────────────────────────────────────────────────────────────

/** SSE로 푸시되는 진행률 이벤트 1건. schemas.ProgressEvent */
export interface ProgressEvent {
  job_id: string;
  stage: JobStage;
  percent: number; // 0~100, 전체 진행률
  message: string;
  eta_seconds: number | null;
}

// ─────────────────────────────────────────────────────────────
// API 요청/응답 모델
// ─────────────────────────────────────────────────────────────

/**
 * POST /jobs 요청 바디. 파일은 같은 PC이므로 경로만 전달(업로드 X). schemas.TranscribeRequest
 * language/model은 백엔드 기본값(ko / large-v3)이 있으므로 선택 필드로 둔다.
 */
export interface TranscribeRequest {
  audio_path: string;
  language?: string;
  model?: string;
  min_speakers?: number | null;
  max_speakers?: number | null;
}

/** GET /jobs/{id} 응답. schemas.JobInfo */
export interface JobInfo {
  job_id: string;
  status: JobStatus;
  stage: JobStage;
  percent: number;
  message: string;
  error: string | null;
  result: TranscriptionResult | null;
}

/** GET /system 응답 — GPU/디바이스/모델 가용성. schemas.SystemInfo */
export interface SystemInfo {
  device: Device;
  gpu_name: string | null;
  vram_total_mb: number | null;
  cuda_available: boolean;
  hf_token_present: boolean;
  ffmpeg_available: boolean;
}

/** POST /jobs/{id}/export 요청. schemas.ExportRequest */
export interface ExportRequest {
  format: ExportFormat;
  out_path: string;
}

/** GET /health 응답(최소 형태). CONTRACT app.py 참조. */
export interface HealthInfo {
  status: string;
  version?: string;
}
