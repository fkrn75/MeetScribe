<script lang="ts">
  /**
   * 화자 패널 — 화자 이름 변경 · 색상 변경 · 병합(동일인 합치기).
   * 화자 라벨 불안정(SPEAKER_00 ≠ 동일인) 문제의 수동 보정 UI(설계서 9, 차별화 기능).
   */
  import {
    speakers,
    renameSpeaker,
    recolorSpeaker,
    mergeSpeaker,
    type SpeakerMeta,
  } from "../stores";

  // 화자 목록(맵 → 배열, id 등장 순 유지).
  let list = $derived<SpeakerMeta[]>(Array.from($speakers.values()));

  // 병합 UI 상태: 어떤 화자를 다른 화자로 합칠지.
  let mergeFrom = $state<string>("");
  let mergeInto = $state<string>("");

  function doMerge() {
    if (mergeFrom && mergeInto && mergeFrom !== mergeInto) {
      mergeSpeaker(mergeFrom, mergeInto);
      mergeFrom = "";
      mergeInto = "";
    }
  }
</script>

<div class="panel">
  <h3 class="head">화자 ({list.length})</h3>

  {#if list.length === 0}
    <div class="empty">아직 화자가 없습니다.</div>
  {:else}
    <ul class="speaker-list">
      {#each list as sp (sp.id)}
        <li class="speaker">
          <input
            class="color"
            type="color"
            value={sp.color}
            aria-label="{sp.label} 색상"
            oninput={(e) => recolorSpeaker(sp.id, (e.currentTarget as HTMLInputElement).value)}
          />
          <input
            class="name"
            type="text"
            value={sp.label}
            aria-label="{sp.id} 이름"
            onchange={(e) => renameSpeaker(sp.id, (e.currentTarget as HTMLInputElement).value)}
          />
          <span class="id" title="백엔드 라벨">{sp.id}</span>
        </li>
      {/each}
    </ul>

    <!-- 병합: from → into -->
    {#if list.length >= 2}
      <div class="merge">
        <div class="merge-title">화자 병합</div>
        <div class="merge-row">
          <select bind:value={mergeFrom} aria-label="합칠 화자">
            <option value="" disabled>합칠 화자</option>
            {#each list as sp (sp.id)}
              <option value={sp.id}>{sp.label}</option>
            {/each}
          </select>
          <span class="arrow">→</span>
          <select bind:value={mergeInto} aria-label="대상 화자">
            <option value="" disabled>대상 화자</option>
            {#each list as sp (sp.id)}
              <option value={sp.id} disabled={sp.id === mergeFrom}>{sp.label}</option>
            {/each}
          </select>
          <button
            class="merge-btn"
            onclick={doMerge}
            disabled={!mergeFrom || !mergeInto || mergeFrom === mergeInto}
          >
            병합
          </button>
        </div>
        <div class="merge-hint">두 화자가 같은 사람이면 하나로 합칩니다(되돌리기 불가).</div>
      </div>
    {/if}
  {/if}
</div>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 14px;
    border-radius: 12px;
    background: var(--panel, #1b2029);
    color: var(--text, #e6e9ef);
  }
  .head {
    margin: 0;
    font-size: 14px;
    font-weight: 700;
  }
  .empty {
    font-size: 12.5px;
    color: var(--muted, #9aa3ad);
  }
  .speaker-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .speaker {
    display: grid;
    grid-template-columns: 28px 1fr auto;
    gap: 8px;
    align-items: center;
  }
  .color {
    width: 28px;
    height: 28px;
    padding: 0;
    border: 1px solid var(--border, #3a4150);
    border-radius: 6px;
    background: none;
    cursor: pointer;
  }
  .name {
    padding: 5px 8px;
    border: 1px solid var(--border, #3a4150);
    border-radius: 6px;
    background: var(--input, #161b22);
    color: var(--text, #e6e9ef);
    font-size: 13px;
  }
  .id {
    font-size: 11px;
    color: var(--muted, #9aa3ad);
    font-variant-numeric: tabular-nums;
  }
  .merge {
    margin-top: 6px;
    padding-top: 10px;
    border-top: 1px solid var(--border, #3a4150);
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .merge-title {
    font-size: 12.5px;
    font-weight: 600;
    color: var(--muted, #9aa3ad);
  }
  .merge-row {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .merge-row select {
    flex: 1 1 0;
    min-width: 0;
    padding: 5px 6px;
    border: 1px solid var(--border, #3a4150);
    border-radius: 6px;
    background: var(--input, #161b22);
    color: var(--text, #e6e9ef);
    font-size: 12.5px;
  }
  .arrow {
    color: var(--muted, #9aa3ad);
    flex: none;
  }
  .merge-btn {
    padding: 5px 12px;
    border: 1px solid var(--accent, #3b6fd4);
    border-radius: 6px;
    background: var(--accent, #3b6fd4);
    color: #fff;
    font-size: 12.5px;
    cursor: pointer;
    flex: none;
  }
  .merge-btn:disabled {
    opacity: 0.45;
    cursor: default;
  }
  .merge-hint {
    font-size: 11px;
    color: var(--muted, #9aa3ad);
  }
</style>
