<script lang="ts">
  /**
   * 툴바 — 결과를 형식별(txt/srt/docx/json)로 내보낸다.
   * 저장 경로는 Tauri save 다이얼로그로 고르고, 사이드카 POST /jobs/{id}/export 를 호출한다.
   * (내보내기 자체는 백엔드 exporters가 수행 — 화자 이름 등 프론트 편집 반영은 백엔드 계약에 따름.)
   */
  import { exportJob, ApiError } from "../api";
  import { ExportFormat, type ExportFormat as ExportFmt } from "../types";

  let { jobId }: { jobId: string | null } = $props();

  // 내보내기 진행/결과 상태.
  let busy = $state<ExportFmt | null>(null);
  let notice = $state("");
  let isError = $state(false);

  /** 형식별 버튼 정의(라벨·확장자). */
  const FORMATS: Array<{ fmt: ExportFmt; label: string }> = [
    { fmt: ExportFormat.TXT, label: "TXT" },
    { fmt: ExportFormat.SRT, label: "SRT" },
    { fmt: ExportFormat.DOCX, label: "DOCX" },
    { fmt: ExportFormat.JSON, label: "JSON" },
  ];

  function isTauri(): boolean {
    return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
  }

  /** 형식 클릭 → 저장 경로 선택 → export 호출. */
  async function handleExport(fmt: ExportFmt) {
    if (!jobId || busy) return;
    notice = "";
    isError = false;

    let outPath: string | null = null;
    if (isTauri()) {
      try {
        const { save } = await import("@tauri-apps/plugin-dialog");
        outPath = await save({
          defaultPath: `meetscribe.${fmt}`,
          filters: [{ name: fmt.toUpperCase(), extensions: [fmt] }],
        });
      } catch (e) {
        showError(`저장 위치 선택 실패: ${e instanceof Error ? e.message : String(e)}`);
        return;
      }
      if (!outPath) return; // 사용자가 취소
    } else {
      // 웹 프리뷰: 다이얼로그 불가 → 안내.
      showError("데스크탑 앱에서 내보내기 경로를 선택할 수 있습니다.");
      return;
    }

    busy = fmt;
    try {
      const res = await exportJob(jobId, fmt, outPath);
      notice = `저장됨: ${res.out_path}`;
      isError = false;
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      showError(`내보내기 실패: ${msg}`);
    } finally {
      busy = null;
    }
  }

  function showError(msg: string) {
    notice = msg;
    isError = true;
  }
</script>

<div class="toolbar">
  <span class="label">내보내기</span>
  <div class="buttons">
    {#each FORMATS as f (f.fmt)}
      <button
        class="export-btn"
        onclick={() => handleExport(f.fmt)}
        disabled={!jobId || busy !== null}
      >
        {busy === f.fmt ? "…" : f.label}
      </button>
    {/each}
  </div>
  {#if notice}
    <span class="notice" class:error={isError}>{notice}</span>
  {/if}
</div>

<style>
  .toolbar {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    padding: 10px 14px;
    border-radius: 10px;
    background: var(--panel, #1b2029);
    color: var(--text, #e6e9ef);
  }
  .label {
    font-size: 13px;
    font-weight: 600;
    color: var(--muted, #9aa3ad);
  }
  .buttons {
    display: flex;
    gap: 6px;
  }
  .export-btn {
    padding: 6px 14px;
    border: 1px solid var(--border, #3a4150);
    border-radius: 8px;
    background: var(--input, #161b22);
    color: var(--text, #e6e9ef);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
  }
  .export-btn:hover:not(:disabled) {
    border-color: var(--accent, #3b6fd4);
    color: var(--accent, #3b6fd4);
  }
  .export-btn:disabled {
    opacity: 0.45;
    cursor: default;
  }
  .notice {
    font-size: 12px;
    color: var(--muted, #9aa3ad);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 360px;
  }
  .notice.error {
    color: var(--danger, #d4456f);
  }
</style>
