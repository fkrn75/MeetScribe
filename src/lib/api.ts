/**
 * 사이드카(FastAPI) 호출 + SSE 구독 클라이언트.
 *
 * 모든 통신은 같은 PC의 localhost(127.0.0.1) 사이드카와 이뤄진다(외부 전송 0).
 * 엔드포인트·필드명은 CONTRACT.md 의 T2(api) 절과 schemas.py를 따른다.
 */

import type {
  ExportFormat,
  HealthInfo,
  JobInfo,
  ProgressEvent,
  SystemInfo,
  TranscribeRequest,
} from "./types";

// ─────────────────────────────────────────────────────────────
// base URL 결정
// ─────────────────────────────────────────────────────────────

/**
 * 사이드카 포트. Tauri/빌드 단계에서 VITE_SIDECAR_PORT 주입, 없거나 빈 값이면 기본 8765.
 * (T4 합의 최종형: 빌드타임 주입 + 런타임 폴백. env가 미정의/빈문자열이어도 8765로 안전 폴백 →
 *  base URL이 `http://127.0.0.1:` 처럼 깨지지 않게 `||` 사용.)
 */
const SIDECAR_PORT = import.meta.env.VITE_SIDECAR_PORT || "8765";

/** 사이드카 base URL. 항상 루프백 주소만 사용한다. */
export const BASE_URL = `http://127.0.0.1:${SIDECAR_PORT}`;

// ─────────────────────────────────────────────────────────────
// 공통 fetch 헬퍼
// ─────────────────────────────────────────────────────────────

/** API 오류를 표준화해 전달하는 예외 타입. */
export class ApiError extends Error {
  readonly status: number;
  readonly url: string;

  constructor(status: number, url: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.url = url;
  }
}

/**
 * 사이드카에 JSON 요청을 보내고 JSON 응답을 파싱한다.
 * 204(No Content)는 undefined 를 반환한다.
 */
async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const url = `${BASE_URL}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
    });
  } catch (e) {
    // 네트워크 단계 실패(사이드카 미기동 등)
    const reason = e instanceof Error ? e.message : String(e);
    throw new ApiError(0, url, `사이드카에 연결할 수 없습니다: ${reason}`);
  }

  if (!res.ok) {
    // 가능하면 FastAPI의 detail 메시지를 끌어온다.
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      // 본문이 JSON이 아니면 statusText 유지
    }
    throw new ApiError(res.status, url, detail);
  }

  // 204 등 본문 없는 응답 처리
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

// ─────────────────────────────────────────────────────────────
// 엔드포인트 래퍼
// ─────────────────────────────────────────────────────────────

/** GET /health — 사이드카 준비 확인(주로 Tauri lifecycle이 쓰지만 디버그용 노출). */
export function getHealth(): Promise<HealthInfo> {
  return request<HealthInfo>("/health", { method: "GET" });
}

/** GET /system — GPU/디바이스/모델/ffmpeg 가용성. */
export function getSystem(): Promise<SystemInfo> {
  return request<SystemInfo>("/system", { method: "GET" });
}

/** POST /jobs — 작업 생성. 파일은 경로만 전달(업로드 X). */
export function createJob(
  req: TranscribeRequest,
): Promise<{ job_id: string }> {
  return request<{ job_id: string }>("/jobs", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

/** GET /jobs/{id} — 작업 상태/결과 조회(폴링·완료 시 result 포함). */
export function getJob(id: string): Promise<JobInfo> {
  return request<JobInfo>(`/jobs/${encodeURIComponent(id)}`, {
    method: "GET",
  });
}

/** POST /jobs/{id}/cancel — 취소 신호. 204 반환. */
export async function cancelJob(id: string): Promise<void> {
  await request<void>(`/jobs/${encodeURIComponent(id)}/cancel`, {
    method: "POST",
  });
}

/** POST /jobs/{id}/export — 결과를 형식별 파일로 내보낸다. */
export function exportJob(
  id: string,
  format: ExportFormat,
  outPath: string,
): Promise<{ out_path: string }> {
  return request<{ out_path: string }>(
    `/jobs/${encodeURIComponent(id)}/export`,
    {
      method: "POST",
      body: JSON.stringify({ format, out_path: outPath }),
    },
  );
}

// ─────────────────────────────────────────────────────────────
// SSE 진행률 구독
// ─────────────────────────────────────────────────────────────

/** subscribeProgress 콜백 모음(선택). */
export interface ProgressHandlers {
  /** 진행률 이벤트 1건 수신마다 호출. */
  onEvent: (e: ProgressEvent) => void;
  /** 스트림 오류(연결 끊김 등). EventSource는 자동 재연결을 시도한다. */
  onError?: (ev: Event) => void;
  /** 작업이 종료 상태(done/failed/cancelled)에 도달하면 1회 호출. */
  onDone?: (e: ProgressEvent) => void;
}

/** 작업을 종료로 보는 단계 집합. */
const TERMINAL_STAGES = new Set(["done", "failed", "cancelled"]);

/**
 * GET /jobs/{id}/events 를 EventSource로 구독한다.
 *
 * 백엔드는 ProgressEvent를 JSON으로 직렬화해 SSE `data:` 라인에 싣는다(가정).
 * 종료 단계(done/failed/cancelled) 이벤트가 오면 onDone 호출 후 자동으로 close 한다.
 * 호출 측은 반환된 EventSource를 보관했다가 화면 이탈/취소 시 `.close()` 해야 한다.
 *
 * NOTE: api 팀원이 event 이름 분리/종료 시그널 방식을 통지하면 여기만 조정한다.
 */
export function subscribeProgress(
  id: string,
  handlers: ProgressHandlers | ((e: ProgressEvent) => void),
): EventSource {
  // 콜백 1개만 넘긴 단축 호출도 허용.
  const h: ProgressHandlers =
    typeof handlers === "function" ? { onEvent: handlers } : handlers;

  const url = `${BASE_URL}/jobs/${encodeURIComponent(id)}/events`;
  const es = new EventSource(url);

  es.onmessage = (ev: MessageEvent<string>) => {
    if (!ev.data) return;
    let parsed: ProgressEvent;
    try {
      parsed = JSON.parse(ev.data) as ProgressEvent;
    } catch {
      // 파싱 불가한 라인은 무시(주석/keep-alive 등)
      return;
    }
    h.onEvent(parsed);

    if (TERMINAL_STAGES.has(parsed.stage)) {
      h.onDone?.(parsed);
      es.close();
    }
  };

  es.onerror = (ev: Event) => {
    h.onError?.(ev);
    // EventSource는 기본적으로 재연결을 시도한다.
    // 연결이 완전히 닫힌(CLOSED) 경우에만 정리한다.
    if (es.readyState === EventSource.CLOSED) {
      es.close();
    }
  };

  return es;
}
