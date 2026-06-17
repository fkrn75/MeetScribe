/**
 * 전역 상태 스토어 (job / transcript / speakers).
 *
 * Svelte writable store 기반. 백엔드 원본(TranscriptionResult)과 프론트 편집 상태
 * (화자 이름·색상 매핑, 검색어 등)를 분리해 보관한다.
 *   - job        : 현재 작업의 진행 상태(단계·퍼센트·메시지·에러)
 *   - transcript : 백엔드가 돌려준 세그먼트 결과(편집은 화자 라벨 위주)
 *   - speakers   : 화자 id → {label, color} 메타(프론트 전용, 이름변경/병합/색상)
 */

import { derived, get, writable } from "svelte/store";
import type { JobStage, JobStatus, Segment, TranscriptionResult } from "./types";

// ─────────────────────────────────────────────────────────────
// 화자 색상 팔레트 (설계서 5.3 결과 뷰 기준)
// ─────────────────────────────────────────────────────────────

/** 화자에 순서대로 배정되는 기본 색상. 설계서 예시(#3b6fd4/#d4456f)를 앞에 둔다. */
export const SPEAKER_PALETTE: readonly string[] = [
  "#3b6fd4", // 파랑
  "#d4456f", // 분홍/빨강
  "#2fa86a", // 초록
  "#e08b2f", // 주황
  "#8a5cd6", // 보라
  "#21a0a0", // 청록
  "#c0573e", // 벽돌
  "#5c7a99", // 청회색
];

/** 화자 인덱스(0-based)에 대응하는 팔레트 색상을 반환(초과 시 순환). */
export function paletteColor(index: number): string {
  const len = SPEAKER_PALETTE.length;
  // noUncheckedIndexedAccess 대응: 모듈러 인덱스는 항상 유효하지만 명시적 보장.
  return SPEAKER_PALETTE[((index % len) + len) % len] ?? "#5c7a99";
}

// ─────────────────────────────────────────────────────────────
// 작업(job) 상태
// ─────────────────────────────────────────────────────────────

/** 프론트가 추적하는 작업 상태 스냅샷. */
export interface JobState {
  jobId: string | null;
  status: JobStatus;
  stage: JobStage;
  percent: number;
  message: string;
  error: string | null;
  /** ETA(초). 사이드카가 제공할 때만 채워진다. */
  etaSeconds: number | null;
}

/** 작업 시작 전 초기 상태. */
export const INITIAL_JOB: JobState = {
  jobId: null,
  status: "pending",
  stage: "queued",
  percent: 0,
  message: "",
  error: null,
  etaSeconds: null,
};

export const job = writable<JobState>({ ...INITIAL_JOB });

/** 작업 상태를 초기화(새 파일 선택 시). */
export function resetJob(): void {
  job.set({ ...INITIAL_JOB });
}

// ─────────────────────────────────────────────────────────────
// 화자(speaker) 메타
// ─────────────────────────────────────────────────────────────

/** 화자 표시 메타(프론트 편집 대상). id는 백엔드 라벨(예: SPEAKER_00). */
export interface SpeakerMeta {
  id: string;
  label: string; // 표시 이름(기본은 id, 사용자가 변경)
  color: string;
}

/** id → SpeakerMeta 매핑. */
export const speakers = writable<Map<string, SpeakerMeta>>(new Map());

/**
 * 세그먼트/결과에 등장하는 화자 목록으로 speakers 맵을 (재)초기화한다.
 * 기존에 사용자가 바꾼 label/color는 같은 id면 보존한다.
 */
export function initSpeakers(ids: string[]): void {
  speakers.update((prev) => {
    const next = new Map<string, SpeakerMeta>();
    ids.forEach((id, i) => {
      const old = prev.get(id);
      next.set(id, {
        id,
        label: old?.label ?? id,
        color: old?.color ?? paletteColor(i),
      });
    });
    return next;
  });
}

/** 화자 이름 변경. */
export function renameSpeaker(id: string, label: string): void {
  speakers.update((m) => {
    const cur = m.get(id);
    if (cur) m.set(id, { ...cur, label });
    return new Map(m);
  });
}

/** 화자 색상 변경. */
export function recolorSpeaker(id: string, color: string): void {
  speakers.update((m) => {
    const cur = m.get(id);
    if (cur) m.set(id, { ...cur, color });
    return new Map(m);
  });
}

/**
 * 두 화자를 병합한다(fromId → intoId). 차별화 기능(설계서 9: 화자 라벨 불안정 대응).
 * - transcript의 모든 세그먼트/단어 speaker를 intoId로 치환
 * - speakers 맵에서 fromId 제거
 */
export function mergeSpeaker(fromId: string, intoId: string): void {
  if (fromId === intoId) return;

  transcript.update((t) => {
    if (!t) return t;
    const segments = t.segments.map((seg) => remapSegmentSpeaker(seg, fromId, intoId));
    const speakerList = t.speakers.filter((s) => s !== fromId);
    return { ...t, segments, speakers: speakerList };
  });

  speakers.update((m) => {
    m.delete(fromId);
    return new Map(m);
  });
}

/** 세그먼트(및 하위 단어)의 화자 라벨을 치환한 새 세그먼트 반환. */
function remapSegmentSpeaker(seg: Segment, fromId: string, intoId: string): Segment {
  const speaker = seg.speaker === fromId ? intoId : seg.speaker;
  const words = seg.words.map((w) =>
    w.speaker === fromId ? { ...w, speaker: intoId } : w,
  );
  return { ...seg, speaker, words };
}

// ─────────────────────────────────────────────────────────────
// 전사 결과(transcript)
// ─────────────────────────────────────────────────────────────

/** 백엔드 원본 결과. 작업 완료 전엔 null. */
export const transcript = writable<TranscriptionResult | null>(null);

/** 결과를 세팅하고 화자 메타까지 초기화한다(작업 완료 시 호출). */
export function setTranscript(result: TranscriptionResult): void {
  transcript.set(result);
  // 결과에 speakers가 없으면 세그먼트에서 추출.
  const ids =
    result.speakers.length > 0
      ? result.speakers
      : uniqueSpeakersFromSegments(result.segments);
  initSpeakers(ids);
}

/** 세그먼트들에서 등장 화자(중복 제거, 등장 순서)를 뽑는다. */
function uniqueSpeakersFromSegments(segments: Segment[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const seg of segments) {
    if (seg.speaker && !seen.has(seg.speaker)) {
      seen.add(seg.speaker);
      out.push(seg.speaker);
    }
  }
  return out;
}

/** 특정 세그먼트의 텍스트를 편집(인덱스 기준). */
export function editSegmentText(index: number, text: string): void {
  transcript.update((t) => {
    if (!t) return t;
    const seg = t.segments[index];
    if (!seg) return t;
    const segments = t.segments.slice();
    segments[index] = { ...seg, text };
    return { ...t, segments };
  });
}

// ─────────────────────────────────────────────────────────────
// 검색 / 파생 상태
// ─────────────────────────────────────────────────────────────

/** 트랜스크립트 검색어(대소문자 무시 부분일치). */
export const searchQuery = writable<string>("");

/** 검색어에 맞춰 필터링된 세그먼트(+원본 인덱스 보존). */
export const filteredSegments = derived(
  [transcript, searchQuery],
  ([$transcript, $query]): Array<{ index: number; segment: Segment }> => {
    if (!$transcript) return [];
    const q = $query.trim().toLowerCase();
    const rows = $transcript.segments.map((segment, index) => ({ index, segment }));
    if (!q) return rows;
    return rows.filter((r) => r.segment.text.toLowerCase().includes(q));
  },
);

/** id로 화자 메타를 조회(없으면 합리적 기본값). 컴포넌트에서 동기 조회용. */
export function speakerMetaOf(id: string | null): SpeakerMeta {
  if (!id) return { id: "", label: "화자 미지정", color: "#9aa3ad" };
  const m = get(speakers).get(id);
  return m ?? { id, label: id, color: "#5c7a99" };
}

/** 전체 상태 초기화(새 작업 시작 시 일괄). */
export function resetAll(): void {
  resetJob();
  transcript.set(null);
  speakers.set(new Map());
  searchQuery.set("");
}
