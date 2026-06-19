<script lang="ts">
  /**
   * 드롭존 — 오디오 파일을 드래그&드롭하거나 마이크를 클릭해서 선택한다.
   * 비주얼은 Hero(나무 마이크 + 별파형 아우라)를 쓰고, 이 래퍼가 드롭/클릭을 처리한다.
   * 파일은 같은 PC에 있으므로 로컬 경로만 상위로 전달(업로드 X, 설계서 3.3).
   */
  import { onMount, onDestroy } from "svelte";
  import Hero from "./Hero.svelte";

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
    hint = "브라우저에서는 파일 경로를 얻을 수 없습니다. 데스크탑 앱을 사용하세요.";
  }
</script>

<div
  class="dz"
  class:drag={dragOver}
  class:disabled
  role="button"
  tabindex="0"
  aria-label="회의 녹음 파일 선택"
  ondragenter={onDragEnter}
  ondragover={onDragEnter}
  ondragleave={onDragLeave}
  ondrop={onDrop}
  onclick={pickViaDialog}
  onkeydown={(e) => (e.key === "Enter" || e.key === " ") && pickViaDialog()}
>
  <Hero>
    <div class="title">회의 녹음 파일을 이 위치에 드롭해 주세요</div>
    <div class="sub">또는 아이콘을 클릭해서 시작 — {ACCEPT.slice(0, 5).join(", ")} 등</div>
    {#if hint}
      <div class="hint">{hint}</div>
    {/if}
  </Hero>
</div>

<style>
  .dz {
    border-radius: 18px;
    padding: 10px 16px;
    cursor: pointer;
    user-select: none;
    transition:
      box-shadow 0.18s,
      background 0.18s;
  }
  .dz.drag {
    background: color-mix(in srgb, var(--accent, #3b6fd4) 10%, transparent);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent, #3b6fd4) 55%, transparent);
  }
  .dz.disabled {
    opacity: 0.5;
    pointer-events: none;
  }
  .title {
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.01em;
    color: var(--text, #e6e9ef);
  }
  .sub {
    margin-top: 8px;
    font-size: 14px;
    color: var(--muted, #9aa3ad);
  }
  .hint {
    margin-top: 8px;
    font-size: 12px;
    color: var(--warn, #e0a14a);
  }
</style>
