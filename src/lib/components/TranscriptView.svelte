<script lang="ts">
  /**
   * 결과 뷰 — 화자별 색상 행으로 트랜스크립트를 표시한다(설계서 5.3).
   * 각 행: [mm:ss 시각] · [화자칩(색상)] · [텍스트(클릭→편집)].
   * 상단 검색창으로 텍스트 부분일치 필터(stores.filteredSegments).
   */
  import {
    filteredSegments,
    searchQuery,
    speakers,
    editSegmentText,
    type SpeakerMeta,
  } from "../stores";

  // 화자 메타 맵(반응형). id → SpeakerMeta.
  let speakerMap = $derived($speakers);

  /** 초 → mm:ss (1시간 넘으면 h:mm:ss). */
  function fmtTime(sec: number): string {
    const s = Math.max(0, Math.floor(sec));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const mm = String(m).padStart(2, "0");
    const ss = String(r).padStart(2, "0");
    return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  }

  /** 화자 메타 조회(없으면 회색 미지정). */
  function metaOf(id: string | null): SpeakerMeta {
    if (id) {
      const m = speakerMap.get(id);
      if (m) return m;
      return { id, label: id, color: "#5c7a99" };
    }
    return { id: "", label: "미지정", color: "#9aa3ad" };
  }

  // 현재 인라인 편집 중인 행 인덱스(null이면 없음).
  let editingIndex = $state<number | null>(null);
  let editingText = $state("");

  function startEdit(index: number, text: string) {
    editingIndex = index;
    editingText = text;
  }
  function commitEdit() {
    if (editingIndex != null) {
      editSegmentText(editingIndex, editingText);
    }
    editingIndex = null;
  }
  function cancelEdit() {
    editingIndex = null;
  }
</script>

<div class="transcript">
  <div class="toolbar">
    <input
      class="search"
      type="search"
      placeholder="대화 내용 검색…"
      bind:value={$searchQuery}
      aria-label="트랜스크립트 검색"
    />
    {#if $searchQuery.trim()}
      <span class="count">{$filteredSegments.length}건</span>
    {/if}
  </div>

  {#if $filteredSegments.length === 0}
    <div class="empty">표시할 발화가 없습니다.</div>
  {:else}
    <div class="rows">
      {#each $filteredSegments as row (row.index)}
        {@const meta = metaOf(row.segment.speaker)}
        <div class="seg-row">
          <span class="time">{fmtTime(row.segment.start)}</span>

          <span class="chip" style="--chip:{meta.color}">
            <span class="dot" style="background:{meta.color}"></span>
            {meta.label}
          </span>

          {#if editingIndex === row.index}
            <!-- svelte-ignore a11y_autofocus -->
            <textarea
              class="edit"
              bind:value={editingText}
              autofocus
              rows="2"
              onblur={commitEdit}
              onkeydown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) commitEdit();
                else if (e.key === "Escape") cancelEdit();
              }}
            ></textarea>
          {:else}
            <button
              class="text"
              title="클릭해서 편집"
              onclick={() => startEdit(row.index, row.segment.text)}
            >
              {row.segment.text}
            </button>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .transcript {
    display: flex;
    flex-direction: column;
    gap: 10px;
    height: 100%;
    min-height: 0;
  }
  .toolbar {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .search {
    flex: 1 1 auto;
    padding: 8px 12px;
    border: 1px solid var(--border, #3a4150);
    border-radius: 8px;
    background: var(--input, #161b22);
    color: var(--text, #e6e9ef);
    font-size: 13px;
  }
  .count {
    font-size: 12px;
    color: var(--muted, #9aa3ad);
    white-space: nowrap;
  }
  .rows {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
  }
  .seg-row {
    display: grid;
    grid-template-columns: 56px minmax(96px, max-content) 1fr;
    gap: 10px;
    align-items: start;
    padding: 7px 6px;
    border-bottom: 1px solid var(--border-faint, #232a35);
  }
  .seg-row:hover {
    background: var(--panel-hover, #222a36);
  }
  .time {
    font-variant-numeric: tabular-nums;
    font-size: 12px;
    color: var(--muted, #9aa3ad);
    padding-top: 3px;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 2px 9px;
    border-radius: 999px;
    font-size: 12.5px;
    font-weight: 600;
    color: var(--chip, #3b6fd4);
    background: color-mix(in srgb, var(--chip) 16%, transparent);
    height: fit-content;
    white-space: nowrap;
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex: none;
  }
  .text {
    text-align: left;
    background: none;
    border: none;
    color: var(--text, #e6e9ef);
    font-size: 14px;
    line-height: 1.5;
    cursor: text;
    padding: 1px 2px;
    border-radius: 4px;
  }
  .text:hover {
    background: var(--input, #161b22);
  }
  .edit {
    width: 100%;
    resize: vertical;
    padding: 6px 8px;
    border: 1px solid var(--accent, #3b6fd4);
    border-radius: 6px;
    background: var(--input, #161b22);
    color: var(--text, #e6e9ef);
    font-size: 14px;
    line-height: 1.5;
    font-family: inherit;
  }
  .empty {
    padding: 32px;
    text-align: center;
    color: var(--muted, #9aa3ad);
    font-size: 13px;
  }
</style>
