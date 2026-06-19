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
    getRuntime,
    installRuntime,
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
  import type { ProgressEvent, RuntimeState, SystemInfo, TranscribeRequest } from "./lib/types";

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
  // 예상 참석자 수(선택). 입력하면 화자 분리에 "정확히 N명"으로 힌트를 준다(min=max=N).
  // 비우면 pyannote 가 자동 감지. 회의는 인원이 명확한 경우가 많아 정확도를 크게 끌어올린다.
  let expectedSpeakers = $state<number | null>(null);
  // 취소 진행 플래그.
  let cancelling = $state(false);
  // 시스템 정보(GPU/ffmpeg 경고 배너용).
  let system = $state<SystemInfo | null>(null);

  // torch 온디맨드 런타임 상태(첫 실행 다운로드 게이트).
  // torch 는 인스톨러 축소를 위해 동결본에서 제외됐다 → 첫 실행 시 캐시로 받는다.
  let runtime = $state<RuntimeState | null>(null);
  let runtimePolling = false;
  // 사이드카 응답을 받았는데 torch 가 아직 없으면 설치 게이트(모달)를 띄운다.
  let needRuntime = $derived(runtime !== null && !runtime.torch_ready);

  // 활성 SSE 핸들. 정리용으로 보관.
  let es: EventSource | null = null;

  onDestroy(() => {
    unsubJob();
    es?.close();
  });

  // 시작 시 시스템 점검(GPU/ffmpeg/HF토큰 경고용). 실패해도 치명적이지 않음. 1회만.
  onMount(() => {
    void loadSystem();
    void checkRuntime();
    // 멈춤 복구: 작업을 시작했는데(jobId 존재) 아직 완료 화면이 아니면 결과를 재회수한다.
    // 처리는 끝났는데 완료 통지를 놓쳐 처리 화면에 멈춘 경우(또는 개발 중 HMR 재마운트)의
    // 안전망 — 사이드카 메모리에 결과가 남아 있으면 재처리 없이 그대로 살려낸다.
    if (jobState.jobId && jobState.status !== "done") {
      view = "processing";
      void pollUntilResult(jobState.jobId);
    }
  });

  async function loadSystem() {
    try {
      system = await getSystem();
    } catch {
      system = null; // 사이드카 미기동 등 — 배너 생략
    }
  }

  /** torch 온디맨드 상태 확인. 미준비면 게이트(모달)가 뜬다. */
  async function checkRuntime() {
    try {
      runtime = await getRuntime();
      // 앱 재시작 시 이미 다운로드가 진행 중이면 폴링을 이어간다.
      if (runtime && !runtime.torch_ready && runtime.stage === "downloading") {
        pollRuntime();
      }
    } catch {
      runtime = null; // 사이드카 미기동 — health/배너로 커버, 게이트 생략
    }
  }

  /** torch 휠(약 3.2GB) 다운로드 시작 → 진행률 폴링. */
  async function startRuntimeInstall() {
    try {
      runtime = await installRuntime();
      pollRuntime();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
      if (runtime) runtime = { ...runtime, stage: "error", message: msg };
    }
  }

  /** ready/error 도달까지 1.5초 간격으로 /runtime 폴링. */
  function pollRuntime() {
    if (runtimePolling) return;
    runtimePolling = true;
    const timer = setInterval(async () => {
      try {
        runtime = await getRuntime();
        if (runtime.torch_ready || runtime.stage === "ready" || runtime.stage === "error") {
          clearInterval(timer);
          runtimePolling = false;
        }
      } catch {
        // 일시 실패는 무시(다음 틱에 재시도)
      }
    }, 1500);
  }

  /** 파일 선택됨 → 작업 생성·진행 구독. */
  async function onPick(path: string) {
    pickedPath = path;
    resetAll();
    job.update((j) => ({ ...j, jobId: null, status: "pending", stage: "queued" }));
    view = "processing";
    cancelling = false;

    try {
      // 참석자 수를 알면 화자 분리 정확도를 위해 정확히 그 수로 고정한다(min=max=N).
      const req: TranscribeRequest = { audio_path: path };
      if (expectedSpeakers && expectedSpeakers > 0) {
        req.min_speakers = expectedSpeakers;
        req.max_speakers = expectedSpeakers;
      }
      const { job_id } = await createJob(req);
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
        // 결과 미도착(완료 통지와 result 저장 사이의 레이스) — 짧게 재시도해 회수.
        void pollUntilResult(jobId);
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

  /**
   * 완료 통지를 받았는데 result 가 아직이면 짧은 간격으로 끈질기게 회수한다.
   *
   * 백엔드는 완료 신호(done)를 result 저장 직후에 보내지만, 만일의 레이스(완료 통지가
   * result 보다 먼저 도달)에도 화면이 멈추지 않도록 하는 안전망이다. 0.4초 간격으로
   * 최대 15회(약 6초) 재시도하며, 그 안에 result 가 오면 결과 화면으로 전환한다.
   */
  async function pollUntilResult(jobId: string, attempts = 15) {
    for (let i = 0; i < attempts; i++) {
      await new Promise((r) => setTimeout(r, 400));
      try {
        const info = await getJob(jobId);
        if (info.result) {
          setTranscript(info.result);
          job.update((j) => ({ ...j, status: "done", stage: "done", percent: 100 }));
          view = "done";
          return;
        }
        if (info.status === "failed") {
          job.update((j) => ({ ...j, status: "failed", error: info.error ?? "처리 실패" }));
          view = "error";
          return;
        }
      } catch {
        // 일시적 조회 실패는 무시하고 다음 시도.
      }
    }
    // 끝내 결과를 못 받음(매우 드묾) — 멈춘 채로 두지 말고 명확히 안내한다.
    failWith(
      new Error("처리는 완료됐지만 결과를 받지 못했습니다. '새 파일'로 다시 시도해 주세요."),
      "결과 수신 실패",
    );
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
        <div class="idle-stack">
          <Dropzone onPick={onPick} />
          <div class="opt">
            <label class="opt-label" for="spk">예상 참석자 수</label>
            <input
              id="spk"
              class="opt-input"
              type="number"
              min="1"
              max="30"
              bind:value={expectedSpeakers}
              placeholder="자동"
            />
            <span class="opt-hint">알면 입력하세요 — 화자 분리가 더 정확해집니다 (비우면 자동 감지)</span>
          </div>
        </div>
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

<!-- torch 온디맨드 설치 게이트: 동결본에 torch 가 없을 때 첫 실행 1회 다운로드를 유도한다. -->
{#if needRuntime && runtime}
  <div class="rt-overlay">
    <div class="rt-modal">
      <div class="rt-icon">🎙️</div>
      <div class="rt-title">음성 엔진 설치 (최초 1회)</div>
      <div class="rt-desc">
        화자 분리·정렬을 위해 음성 엔진(PyTorch, 약 3.2GB)을 한 번만 내려받습니다.
        받은 파일은 다음 실행부터 재사용되며, 음성 파일은 항상 이 PC에서만 처리됩니다.
      </div>

      {#if runtime.stage === "idle" || runtime.stage === "error"}
        {#if runtime.stage === "error"}
          <div class="rt-err">설치 실패: {runtime.message}</div>
        {/if}
        <button class="rt-btn" onclick={startRuntimeInstall}>다운로드 시작 (약 3.2GB)</button>
      {:else}
        <div class="rt-prog">
          <div class="rt-track">
            <div class="rt-fill" style="width:{Math.round(runtime.progress * 100)}%"></div>
          </div>
          <div class="rt-stat">
            {runtime.message} — {(runtime.downloaded / 1e9).toFixed(2)} / {(runtime.total / 1e9).toFixed(2)} GB
            ({Math.round(runtime.progress * 100)}%)
          </div>
        </div>
      {/if}
    </div>
  </div>
{/if}

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

  /* idle: 드롭존 + 참석자 수 옵션 */
  .idle-stack {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 18px;
    width: min(560px, 100%);
  }
  .opt {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 12px;
    width: 100%;
    padding: 14px 16px;
    border-radius: 12px;
    background: var(--panel, #1b2029);
    border: 1px solid #2d231b;
  }
  .opt-label {
    font-size: 13px;
    font-weight: 600;
    color: var(--text, #e6e9ef);
  }
  .opt-input {
    width: 84px;
    padding: 7px 10px;
    border-radius: 8px;
    border: 1px solid var(--border, #3a4150);
    background: #141a22;
    color: var(--text, #e6e9ef);
    font-size: 14px;
  }
  .opt-input:focus {
    outline: none;
    border-color: var(--accent, #3b6fd4);
  }
  .opt-hint {
    flex: 1 1 100%;
    font-size: 12px;
    color: var(--muted, #9aa3ad);
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

  /* ── torch 온디맨드 설치 게이트(오버레이) ── */
  .rt-overlay {
    position: fixed;
    inset: 0;
    z-index: 100;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(0, 0, 0, 0.72);
    backdrop-filter: blur(2px);
  }
  .rt-modal {
    width: min(460px, 90vw);
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 26px;
    border-radius: 16px;
    background: var(--panel, #1b2029);
    border: 1px solid #2d231b;
    box-shadow: 0 18px 60px rgba(0, 0, 0, 0.5);
    text-align: center;
  }
  .rt-icon {
    font-size: 34px;
  }
  .rt-title {
    font-weight: 700;
    font-size: 17px;
    color: var(--text, #e6e9ef);
  }
  .rt-desc {
    font-size: 13px;
    line-height: 1.55;
    color: var(--muted, #9aa3ad);
  }
  .rt-btn {
    margin-top: 6px;
    padding: 11px 18px;
    border: none;
    border-radius: 10px;
    background: var(--accent, #3b6fd4);
    color: #fff;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
  }
  .rt-btn:hover {
    filter: brightness(1.08);
  }
  .rt-err {
    font-size: 12.5px;
    color: var(--danger, #d4456f);
  }
  .rt-prog {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 4px;
  }
  .rt-track {
    height: 10px;
    border-radius: 999px;
    background: #2a3340;
    overflow: hidden;
  }
  .rt-fill {
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, #3b6fd4, #5b8df0);
    transition: width 0.3s ease;
  }
  .rt-stat {
    font-size: 12px;
    color: var(--muted, #9aa3ad);
  }
</style>
