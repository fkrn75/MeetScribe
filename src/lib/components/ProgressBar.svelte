<script lang="ts">
  /**
   * 진행바 — 현재 단계명 + 전체 퍼센트 + (선택)ETA + 취소 버튼.
   * 장시간 작업이 1급 요구사항이므로 단계/퍼센트/취소를 항상 노출한다(설계서 1.3).
   */
  import type { JobStage } from "../types";

  let {
    stage,
    percent,
    message = "",
    etaSeconds = null,
    cancelling = false,
    onCancel,
  }: {
    stage: JobStage;
    percent: number;
    message?: string;
    etaSeconds?: number | null;
    cancelling?: boolean;
    onCancel: () => void;
  } = $props();

  /** 단계 한글 라벨. */
  const STAGE_LABEL: Record<JobStage, string> = {
    queued: "대기 중",
    preprocess: "전처리",
    transcribe: "음성 인식",
    align: "단어 정렬",
    diarize: "화자 분리",
    merge: "병합",
    done: "완료",
    failed: "실패",
    cancelled: "취소됨",
  };

  // 0~100 범위로 클램프(표시 안전).
  let pct = $derived(Math.max(0, Math.min(100, Math.round(percent))));
  let label = $derived(STAGE_LABEL[stage] ?? stage);
  let isTerminal = $derived(stage === "done" || stage === "failed" || stage === "cancelled");

  /** 초 → "m분 s초" 표기. */
  function fmtEta(sec: number): string {
    const s = Math.max(0, Math.round(sec));
    const m = Math.floor(s / 60);
    const r = s % 60;
    return m > 0 ? `약 ${m}분 ${r}초 남음` : `약 ${r}초 남음`;
  }
</script>

<div class="progress">
  <div class="row">
    <span class="stage">{label}</span>
    <span class="pct">{pct}%</span>
  </div>

  <div class="track" role="progressbar" aria-valuenow={pct} aria-valuemin="0" aria-valuemax="100">
    <div
      class="fill"
      class:failed={stage === "failed"}
      class:cancelled={stage === "cancelled"}
      style="width: {pct}%"
    ></div>
  </div>

  <div class="row meta">
    <span class="message">{message}</span>
    {#if etaSeconds != null && !isTerminal}
      <span class="eta">{fmtEta(etaSeconds)}</span>
    {/if}
  </div>

  {#if !isTerminal}
    <button class="cancel" onclick={onCancel} disabled={cancelling}>
      {cancelling ? "취소 중…" : "취소"}
    </button>
  {/if}
</div>

<style>
  .progress {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 18px;
    border-radius: 12px;
    background: var(--panel, #1b2029);
    color: var(--text, #e6e9ef);
  }
  .row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }
  .stage {
    font-weight: 600;
    font-size: 15px;
  }
  .pct {
    font-variant-numeric: tabular-nums;
    color: var(--muted, #9aa3ad);
  }
  .track {
    height: 10px;
    border-radius: 6px;
    background: var(--track, #2a3040);
    overflow: hidden;
  }
  .fill {
    height: 100%;
    background: var(--accent, #3b6fd4);
    border-radius: 6px;
    transition: width 0.3s ease;
  }
  .fill.failed {
    background: var(--danger, #d4456f);
  }
  .fill.cancelled {
    background: var(--muted, #9aa3ad);
  }
  .meta {
    font-size: 12.5px;
    color: var(--muted, #9aa3ad);
    min-height: 16px;
  }
  .message {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .eta {
    white-space: nowrap;
    margin-left: 12px;
  }
  .cancel {
    align-self: flex-end;
    margin-top: 4px;
    padding: 6px 16px;
    border: 1px solid var(--border, #3a4150);
    border-radius: 8px;
    background: transparent;
    color: var(--text, #e6e9ef);
    cursor: pointer;
    font-size: 13px;
  }
  .cancel:hover:not(:disabled) {
    background: var(--danger, #d4456f);
    border-color: var(--danger, #d4456f);
    color: #fff;
  }
  .cancel:disabled {
    opacity: 0.5;
    cursor: default;
  }
</style>
