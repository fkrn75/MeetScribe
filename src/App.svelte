<script lang="ts">
  /**
   * MeetScribe 메인 화면.
   *
   * 화면 상태 머신(설계서 5.2):
   *   idle       — 드롭존(파일 대기)
   *   processing — 진행바(단계·퍼센트·취소)
   *   done       — 결과 뷰 + 화자 패널 + 내보내기 툴바
   *   error      — 오류 메시지 + 다시 시도
   *
   * 작업 흐름: 파일 경로 → createJob → subscribeProgress(SSE) → 완료 시 getJob으로 result 적재.
   */
  import { onDestroy, onMount } from "svelte";
  import {
    createJob,
    cancelJob,
    getJob,
    getSystem,
    subscribeProgress,
    ApiError,
  } from "./lib/api";
  import {
    job,
    resetAll,
    setTranscript,
    transcript,
    type JobState,
  } from "./lib/stores";
  import type { ProgressEvent, SystemInfo } from "./lib/types";

  import Dropzone from "./lib/components/Dropzone.svelte";
  import Hero from "./lib/components/Hero.svelte";
  import ProgressBar from "./lib/components/ProgressBar.svelte";
  import TranscriptView from "./lib/components/TranscriptView.svelte";
  import SpeakerPanel from "./lib/components/SpeakerPanel.svelte";
  import Toolbar from "./lib/components/Toolbar.svelte";

  // 화면 단계.
  type View = "idle" | "processing" | "done" | "error";
  let view = $state<View>("idle");

  // 현재 작업 상태(스토어 미러). 구독으로 갱신.
  let jobState = $state<JobState>($job);
  const unsubJob = job.subscribe((v) => (jobState = v));

  // 선택된 파일 경로(표시용).
  let pickedPath = $state<string>("");
  // 취소 진행 플래그.
  let cancelling = $state(false);
  // 시스템 정보(GPU/ffmpeg 경고 배너용).
  let system = $state<SystemInfo | null>(null);

  // 활성 SSE 핸들. 정리용으로 보관.
  let es: EventSource | null = null;

  onDestroy(() => {
    unsubJob();
    es?.close();
  });

  // 시작 시 시스템 점검(GPU/ffmpeg/HF토큰 경고용). 실패해도 치명적이지 않음. 1회만.
  onMount(() => {
    void loadSystem();
  });

  async function loadSystem() {
    try {
      system = await getSystem();
    } catch {
      system = null; // 사이드카 미기동 등 — 배너 생략
    }
  }

  /** 파일 선택됨 → 작업 생성·진행 구독. */
  async function onPick(path: string) {
    pickedPath = path;
    resetAll();
    job.update((j) => ({ ...j, jobId: null, status: "pending", stage: "queued" }));
    view = "processing";
    cancelling = false;

    try {
      const { job_id } = await createJob({ audio_path: path });
      job.update((j) => ({ ...j, jobId: job_id, status: "running" }));
      startProgress(job_id);
    } catch (e) {
      failWith(e, "작업을 시작하지 못했습니다");
    }
  }

  /** SSE 구독 시작. 종료 단계 도달 시 결과를 적재한다. */
  function startProgress(jobId: string) {
    es?.close();
    es = subscribeProgress(jobId, {
      onEvent: (ev: ProgressEvent) => {
        job.update((j) => ({
          ...j,
          stage: ev.stage,
          percent: ev.percent,
          message: ev.message,
          etaSeconds: ev.eta_seconds,
        }));
      },
      onDone: (ev: ProgressEvent) => {
        if (ev.stage === "done") {
          void finishJob(jobId);
        } else if (ev.stage === "failed") {
          job.update((j) => ({ ...j, status: "failed", error: ev.message || "처리 실패" }));
          view = "error";
        } else if (ev.stage === "cancelled") {
          job.update((j) => ({ ...j, status: "cancelled" }));
          view = "idle";
        }
      },
      onError: () => {
        // EventSource가 끊겼지만 작업은 끝났을 수 있다 → 폴링으로 확인.
        void pollOnce(jobId);
      },
    });
  }

  /** 완료 시 최종 JobInfo를 받아 결과를 스토어에 적재. */
  async function finishJob(jobId: string) {
    try {
      const info = await getJob(jobId);
      if (info.result) {
        setTranscript(info.result);
        job.update((j) => ({ ...j, status: "done", stage: "done", percent: 100 }));
        view = "done";
      } else if (info.error) {
        job.update((j) => ({ ...j, status: "failed", error: info.error }));
        view = "error";
      } else {
        // 결과 미도착 — 한 번 더 폴링 여지.
        void pollOnce(jobId);
      }
    } catch (e) {
      failWith(e, "결과를 불러오지 못했습니다");
    }
  }

  /** SSE 끊김 등 보조 폴링(1회). 완료/실패면 화면 전환. */
  async function pollOnce(jobId: string) {
    try {
      const info = await getJob(jobId);
      if (info.status === "done" && info.result) {
        setTranscript(info.result);
        job.update((j) => ({ ...j, status: "done", stage: "done", percent: 100 }));
        view = "done";
      } else if (info.status === "failed") {
        job.update((j) => ({ ...j, status: "failed", error: info.error ?? "처리 실패" }));
        view = "error";
      }
      // running이면 SSE 자동 재연결을 기다린다.
    } catch {
      // 폴링 실패는 조용히 무시(재연결 대기).
    }
  }

  /** 취소 버튼. */
  async function onCancel() {
    if (!jobState.jobId || cancelling) return;
    cancelling = true;
    try {
      await cancelJob(jobState.jobId);
      // 실제 cancelled 전환은 SSE onDone(cancelled) 또는 폴링이 처리.
    } catch (e) {
      failWith(e, "취소에 실패했습니다");
    } finally {
      cancelling = false;
    }
  }

  /** 오류 공통 처리. */
  function failWith(e: unknown, prefix: string) {
    const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
    job.update((j) => ({ ...j, status: "failed", error: `${prefix}: ${msg}` }));
    view = "error";
  }

  /** 처음으로(새 파일). */
  function reset() {
    es?.close();
    es = null;
    pickedPath = "";
    resetAll();
    view = "idle";
  }

  // 파일명만 추출(표시용).
  let pickedName = $derived(pickedPath ? pickedPath.split(/[\\/]/).pop() : "");
</script>

<main class="app">
  <header class="topbar">
    <div class="brand">
      <span class="logo">📝</span>
      <span class="name">MeetScribe</span>
      <span class="tag">로컬 회의록</span>
    </div>
    {#if pickedName}
      <div class="file">{pickedName}</div>
    {/if}
    {#if view === "done" || view === "error"}
      <button class="new" onclick={reset}>새 파일</button>
    {/if}
  </header>

  <!-- 시스템 경고 배너(ffmpeg/CUDA/HF 토큰) -->
  {#if system}
    {#if !system.ffmpeg_available}
      <div class="banner warn">⚠️ ffmpeg를 찾을 수 없습니다. 오디오 전처리가 실패할 수 있습니다.</div>
    {:else if !system.cuda_available}
      <div class="banner warn">
        ⚠️ GPU(CUDA) 미감지 — CPU 모드로 동작합니다(처리에 시간이 더 걸립니다).
      </div>
    {/if}
    {#if !system.hf_token_present}
      <div class="banner info">ℹ️ HF 토큰이 없으면 화자 분리가 제한될 수 있습니다(설정에서 입력).</div>
    {/if}
  {/if}

  <section class="content">
    {#if view === "idle"}
      <div class="center">
        <Dropzone onPick={onPick} />
      </div>
    {:else if view === "processing"}
      <div class="center">
        <Hero active>
          <div class="pbar">
            <ProgressBar
              bare
              stage={jobState.stage}
              percent={jobState.percent}
              message={jobState.message}
              etaSeconds={jobState.etaSeconds}
              cancelling={cancelling}
              onCancel={onCancel}
            />
          </div>
        </Hero>
      </div>
    {:else if view === "error"}
      <div class="center narrow">
        <div class="error-box">
          <div class="error-title">처리 중 문제가 발생했습니다</div>
          <div class="error-msg">{jobState.error ?? "알 수 없는 오류"}</div>
          <button class="retry" onclick={reset}>다시 시도</button>
        </div>
      </div>
    {:else if view === "done"}
      <div class="result">
        <div class="result-main">
          <Toolbar jobId={jobState.jobId} />
          <div class="transcript-wrap">
            {#if $transcript}
              <TranscriptView />
            {/if}
          </div>
        </div>
        <aside class="result-side">
          <SpeakerPanel />
        </aside>
      </div>
    {/if}
  </section>
</main>

<style>
  .app {
    display: flex;
    flex-direction: column;
    height: 100vh;
    min-height: 0;
  }
  .topbar {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 12px 18px;
    /* 레퍼런스의 따뜻한 다크 상단바(배경 #1a1512 보다 한 톤 위) */
    border-bottom: 1px solid #2d231b;
    background: #211a15;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .logo {
    font-size: 18px;
  }
  .name {
    font-weight: 700;
    font-size: 16px;
  }
  .tag {
    font-size: 11px;
    color: var(--muted, #9aa3ad);
    border: 1px solid #3a2e22;
    padding: 1px 7px;
    border-radius: 999px;
  }
  .file {
    font-size: 12.5px;
    color: var(--muted, #9aa3ad);
    margin-left: auto;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 40%;
  }
  .new {
    margin-left: 12px;
    padding: 6px 14px;
    border: 1px solid var(--border, #3a4150);
    border-radius: 8px;
    background: transparent;
    color: var(--text, #e6e9ef);
    cursor: pointer;
    font-size: 13px;
  }
  .new:hover {
    border-color: var(--accent, #3b6fd4);
  }
  .banner {
    padding: 8px 18px;
    font-size: 12.5px;
  }
  .banner.warn {
    background: color-mix(in srgb, var(--warn, #e0a14a) 18%, transparent);
    color: var(--warn, #e0a14a);
  }
  .banner.info {
    background: color-mix(in srgb, var(--accent, #3b6fd4) 14%, transparent);
    color: var(--text, #e6e9ef);
  }
  .content {
    flex: 1 1 auto;
    min-height: 0;
    padding: 18px;
    overflow: hidden;
  }
  .center {
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .center.narrow > :global(*) {
    width: min(560px, 100%);
  }
  .pbar {
    width: min(460px, 82vw);
  }

  /* 결과: 좌(트랜스크립트) + 우(화자 패널) */
  .result {
    display: grid;
    grid-template-columns: 1fr 300px;
    gap: 16px;
    height: 100%;
    min-height: 0;
  }
  .result-main {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-height: 0;
  }
  .transcript-wrap {
    flex: 1 1 auto;
    min-height: 0;
    padding: 14px;
    border-radius: 12px;
    background: var(--panel, #1b2029);
  }
  .result-side {
    min-height: 0;
    overflow-y: auto;
  }

  .error-box {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 22px;
    border-radius: 12px;
    background: var(--panel, #1b2029);
    border: 1px solid var(--danger, #d4456f);
  }
  .error-title {
    font-weight: 700;
    color: var(--danger, #d4456f);
  }
  .error-msg {
    font-size: 13px;
    color: var(--text, #e6e9ef);
    word-break: break-word;
  }
  .retry {
    align-self: flex-start;
    padding: 7px 16px;
    border: 1px solid var(--accent, #3b6fd4);
    border-radius: 8px;
    background: var(--accent, #3b6fd4);
    color: #fff;
    cursor: pointer;
    font-size: 13px;
  }
</style>
