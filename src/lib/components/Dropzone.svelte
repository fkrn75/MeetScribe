<script lang="ts">
  /**
   * 드롭존 — 오디오 파일을 드래그&드롭하거나 클릭해서 선택한다.
   *
   * 파일은 같은 PC에 있으므로 **로컬 경로만** 상위로 전달한다(업로드 X, 설계서 3.3).
   * - Tauri 환경: OS 드래그&드롭 이벤트로 절대경로를 얻거나, 다이얼로그로 선택.
   * - 일반 브라우저(개발/프리뷰): File 객체엔 절대경로가 없으므로 안내만 표시.
   */
  import { onMount, onDestroy } from "svelte";

  // 부모가 넘기는 콜백: 선택된 절대경로를 전달.
  let { onPick, disabled = false }: { onPick: (path: string) => void; disabled?: boolean } =
    $props();

  let dragOver = $state(false);
  let hint = $state("");
  // Tauri 드롭 구독 해제 함수(있으면 onDestroy에서 호출).
  let unlisten: null | (() => void) = null;

  /** 허용 확장자(표시·필터용). 실제 검증은 사이드카 ffmpeg가 한다. */
  const ACCEPT = ["mp3", "m4a", "wav", "flac", "ogg", "aac", "mp4", "mov", "mkv", "webm"];

  /** Tauri 런타임인지(웹 프리뷰와 분기). */
  function isTauri(): boolean {
    return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
  }

  onMount(async () => {
    if (!isTauri()) return;
    // Tauri 2 드래그&드롭 이벤트 구독(동적 import로 웹 빌드에서 안전).
    try {
      const { getCurrentWebview } = await import("@tauri-apps/api/webview");
      unlisten = await getCurrentWebview().onDragDropEvent((event) => {
        const payload = event.payload;
        if (payload.type === "over") {
          dragOver = true;
        } else if (payload.type === "drop") {
          dragOver = false;
          const first = payload.paths?.[0];
          if (first) onPick(first);
        } else {
          dragOver = false;
        }
      });
    } catch {
      // Tauri API 미존재 시 무시(웹).
    }
  });

  onDestroy(() => {
    unlisten?.();
  });

  /** 클릭 → 파일 선택 다이얼로그(Tauri) 또는 안내(웹). */
  async function pickViaDialog() {
    if (disabled) return;
    if (!isTauri()) {
      hint = "데스크탑 앱에서 실행하면 파일을 직접 선택할 수 있습니다.";
      return;
    }
    try {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const selected = await open({
        multiple: false,
        filters: [{ name: "오디오/영상", extensions: ACCEPT }],
      });
      if (typeof selected === "string") onPick(selected);
    } catch (e) {
      hint = `파일 선택 실패: ${e instanceof Error ? e.message : String(e)}`;
    }
  }

  // 웹 환경의 HTML5 드래그 이벤트(시각 피드백용). 경로는 Tauri 경로에서만 확정.
  function onDragEnter(e: DragEvent) {
    e.preventDefault();
    if (!disabled) dragOver = true;
  }
  function onDragLeave(e: DragEvent) {
    e.preventDefault();
    dragOver = false;
  }
  function onDrop(e: DragEvent) {
    e.preventDefault();
    dragOver = false;
    if (isTauri()) return; // 경로는 Tauri 이벤트가 처리
    // 웹: File.path가 없으므로 안내.
    hint = "브라우저에서는 파일 경로를 얻을 수 없습니다. 데스크탑 앱을 사용하세요.";
  }
</script>

<div
  class="dropzone"
  class:drag={dragOver}
  class:disabled
  role="button"
  tabindex="0"
  ondragenter={onDragEnter}
  ondragover={onDragEnter}
  ondragleave={onDragLeave}
  ondrop={onDrop}
  onclick={pickViaDialog}
  onkeydown={(e) => (e.key === "Enter" || e.key === " ") && pickViaDialog()}
>
  <div class="icon">🎙️</div>
  <div class="title">회의 녹음 파일을 여기에 드롭</div>
  <div class="sub">또는 클릭해서 선택 · {ACCEPT.slice(0, 5).join(", ")} 등</div>
  {#if hint}
    <div class="hint">{hint}</div>
  {/if}
</div>

<style>
  .dropzone {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 48px 24px;
    border: 2px dashed var(--border, #3a4150);
    border-radius: 14px;
    background: var(--panel, #1b2029);
    color: var(--text, #e6e9ef);
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
    user-select: none;
  }
  .dropzone.drag {
    border-color: var(--accent, #3b6fd4);
    background: var(--panel-hover, #222a36);
  }
  .dropzone.disabled {
    opacity: 0.5;
    pointer-events: none;
  }
  .icon {
    font-size: 40px;
  }
  .title {
    font-size: 16px;
    font-weight: 600;
  }
  .sub {
    font-size: 12.5px;
    color: var(--muted, #9aa3ad);
  }
  .hint {
    margin-top: 6px;
    font-size: 12px;
    color: var(--warn, #e0a14a);
  }
</style>
