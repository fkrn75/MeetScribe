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
    reassignSegmentSpeaker,
    type SpeakerMeta,
  } from "../stores";

  // 화자 메타 맵(반응형). id → SpeakerMeta.
  let speakerMap = $derived($speakers);
  // 화자 선택 목록(팝오버 표시·숫자키 순서). 삽입 순서 보존.
  let speakerList = $derived(Array.from(speakerMap.values()));

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

  // ── 화자 재할당(짧은 발화 수동 교정) ──
  // 팝오버가 열린 세그먼트 인덱스(null이면 닫힘).
  let pickerIndex = $state<number | null>(null);

  function togglePicker(index: number) {
    pickerIndex = pickerIndex === index ? null : index;
  }
  function pickSpeaker(index: number, id: string | null) {
    reassignSegmentSpeaker(index, id);
    pickerIndex = null;
  }
  /** 팝오버에서 숫자키(1~N)로 빠르게 화자 선택, Esc로 닫기. */
  function onPickerKey(e: KeyboardEvent, index: number) {
    if (e.key === "Escape") {
      pickerIndex = null;
      return;
    }
    const n = Number(e.key);
    if (Number.isInteger(n) && n >= 1 && n <= speakerList.length) {
      e.preventDefault();
      const sp = speakerList[n - 1];
      if (sp) pickSpeaker(index, sp.id);
    }
  }
</script>

<svelte:window
  onclick={(e) => {
    // 팝오버 바깥 클릭 시 닫기(칩·팝오버는 .chip-wrap 내부라 유지).
    if (
      pickerIndex !== null &&
      !(e.target as Element | null)?.closest?.(".chip-wrap")
    ) {
      pickerIndex = null;
    }
  }}
/>

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

          <span class="chip-wrap">
            <button
              class="chip"
              style="--chip:{meta.color}"
              title="클릭해서 화자 변경(숫자키로 빠른 선택)"
              onclick={() => togglePicker(row.index)}
              onkeydown={(e) => onPickerKey(e, row.index)}
            >
              <span class="dot" style="background:{meta.color}"></span>
              {meta.label}
            </button>
            {#if pickerIndex === row.index}
              <!-- 화자 선택 팝오버: 숫자키 1~N 또는 클릭으로 재할당 -->
              <div class="picker" role="menu">
                {#each speakerList as sp, i (sp.id)}
                  <button
                    class="picker-item"
                    class:active={sp.id === row.segment.speaker}
                    role="menuitem"
                    onclick={() => pickSpeaker(row.index, sp.id)}
                  >
                    <span class="num">{i + 1}</span>
                    <span class="dot" style="background:{sp.color}"></span>
                    <span class="pl">{sp.label}</span>
                  </button>
                {/each}
              </div>
            {/if}
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
  .chip-wrap {
    position: relative;
    display: inline-flex;
    height: fit-content;
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
    border: none;
    font-family: inherit;
    cursor: pointer;
  }
  .chip:hover {
    background: color-mix(in srgb, var(--chip) 30%, transparent);
  }
  .picker {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    z-index: 20;
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 140px;
    padding: 4px;
    background: var(--panel, #1b212b);
    border: 1px solid var(--border, #3a4150);
    border-radius: 8px;
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.35);
  }
  .picker-item {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 5px 8px;
    background: none;
    border: none;
    border-radius: 6px;
    color: var(--text, #e6e9ef);
    font-size: 12.5px;
    text-align: left;
    cursor: pointer;
  }
  .picker-item:hover {
    background: var(--panel-hover, #222a36);
  }
  .picker-item.active {
    background: color-mix(in srgb, var(--accent, #3b6fd4) 18%, transparent);
  }
  .num {
    flex: none;
    width: 16px;
    height: 16px;
    border-radius: 4px;
    background: var(--input, #161b22);
    color: var(--muted, #9aa3ad);
    font-size: 10px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .pl {
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
