<script lang="ts">
  /**
   * 히어로 비주얼 — 나무 마이크 + 별파형 아우라 5겹.
   * 각 겹은 센터 고정 회전(속도·방향 제각각) + 미세 호흡(scale) + 깊은 밝기 맥동을
   * 서로 다른 주기로 독립 재생해 유기적으로 일렁이게 한다(회전 중심은 항상 정중앙).
   * 아우라는 검은 배경 글로우라 mix-blend-mode:screen 으로 빛만 남는다. 하단(children)에 안내문구/진행바.
   */
  import type { Snippet } from "svelte";
  import micUrl from "../../assets/hero/mic.png";
  import w1 from "../../assets/hero/wave1.png";
  import w2 from "../../assets/hero/wave2.png";
  import w3 from "../../assets/hero/wave3.png";
  import w4 from "../../assets/hero/wave4.png";
  import w5 from "../../assets/hero/wave5.png";

  // active=처리 중 → 아우라를 더 빠르게.
  let { active = false, children }: { active?: boolean; children?: Snippet } = $props();
  const waves = [w1, w2, w3, w4, w5];
</script>

<div class="hero" class:active>
  <div class="stage">
    {#each waves as w, i}
      <img class="aura w{i + 1}" src={w} alt="" aria-hidden="true" draggable="false" />
    {/each}
    <img class="mic" src={micUrl} alt="MeetScribe" draggable="false" />
  </div>
  <div class="caption">{@render children?.()}</div>
</div>

<style>
  .hero {
    display: flex;
    flex-direction: column;
    align-items: center;
    /* 글로우(아우라)가 무대 밖으로 넘치므로 캡션을 충분히 내려 겹침을 피한다(레퍼런스처럼).
       72px ≈ 밝은 코어(470겹) 하단을 캡션이 넘어서는 값(무대 340 기준 계산). */
    gap: 72px;
  }
  .stage {
    position: relative;
    width: 340px;
    height: 340px;
  }
  /* 아우라 글로우(검은 배경) → screen 으로 빛만 남는다.
     중앙정렬은 absolute + 개별 translate 속성(-50% -50%)으로 박고,
     rotate/scale 은 애니메이션 전용으로 합성한다(회전 중심이 정중앙에 고정). */
  .aura {
    position: absolute;
    left: 50%;
    top: 50%;
    /* 중앙 고정은 개별 translate 속성(상수)으로 준다. rotate/scale 애니메이션과
       합성돼도 회전 중심이 정중앙에 박힌다. (transform 속성으로 -50% 를 주면 rotate 와
       적용 순서가 꼬여 중심이 같이 돌아가며 어긋나므로 금지.) */
    translate: -50% -50%;
    width: 470px;
    height: auto;
    pointer-events: none;
    mix-blend-mode: screen;
    will-change: rotate, scale, opacity;
    z-index: 1;
  }
  /* 각 겹은 3개 애니메이션을 합성한다(개별 transform 속성이라 충돌 없음):
       rotate(센터 고정 회전·속도/방향 제각각)
       + scale(msWave: 같은 주기 1.2s, 비트처럼 아주 짧게 팍 커졌다 바로 원복. delay 를
         0.08s 씩 어긋내 비트가 바깥으로 살짝 번진다)
       + opacity(0.1 수준까지 깊게 어두워졌다 복귀, 이건 겹마다 랜덤한 깜빡임).
     translate(위치 이동)은 회전 중심을 같이 밀어버리므로 쓰지 않는다 → 회전은 항상 정중앙. */
  .w1 {
    animation: msSpinCW 67s linear infinite, msWave 1.2s ease-out 0s infinite,
      msDimA 4.2s ease-in-out -1.1s infinite;
  }
  .w2 {
    animation: msSpinCCW 49s linear infinite, msWave 1.2s ease-out -0.08s infinite,
      msDimB 5.7s ease-in-out -3.3s infinite;
  }
  .w3 {
    animation: msSpinCW 81s linear infinite, msWave 1.2s ease-out -0.16s infinite,
      msDimC 3.8s ease-in-out -0.7s infinite;
  }
  /* 녹색(05) 겹 — 가장 외곽. 현재 숨김. 다시 보이려면 아래 display:none 줄만 제거. */
  .w4 {
    display: none;
    width: 520px;
    animation: msSpinCCW 43s linear infinite, msWave 1.2s ease-out -0.24s infinite,
      msDimB 6.4s ease-in-out -2.5s infinite;
  }
  .w5 {
    animation: msSpinCW 55s linear infinite, msWave 1.2s ease-out -0.32s infinite,
      msDimA 5.1s ease-in-out -4s infinite;
  }
  .mic {
    position: absolute;
    left: 50%;
    top: 50%;
    /* 중앙 고정(translate 속성). 위치 이동(부유)은 빼고 비트 진동(msMicBeat=scale)만 —
       scale 은 translate 와 다른 속성이라 중앙정렬을 안 깨고 합성된다. */
    translate: -50% -50%;
    z-index: 2;
    width: 145px;
    height: auto;
    filter: drop-shadow(0 10px 26px rgba(0, 0, 0, 0.55));
    animation: msMicBeat 1.2s ease-out 0s infinite;
    user-select: none;
  }

  /* 회전: 속도(43~81s)와 방향(CW/CCW)을 겹마다 달리해 랜덤하게. */
  @keyframes msSpinCW {
    to {
      rotate: 360deg;
    }
  }
  @keyframes msSpinCCW {
    to {
      rotate: -360deg;
    }
  }
  /* 스케일 비트(msWave) — 평소엔 기본 크기(1.0)로 쉬다가, 비트처럼 아주 짧게 팍 커졌다
     바로 원복한다(어택 7%≈0.08s, 릴리즈 ~18%≈0.22s, 나머지 82%는 정지). ease-out 으로 스냅.
     겹마다 delay 가 0.08s 씩 어긋나 비트가 바깥으로 살짝 번진다. 중심 대칭이라 피벗 불변. */
  @keyframes msWave {
    0% {
      scale: 1;
    }
    7% {
      scale: 1.08;
    }
    18% {
      scale: 1;
    }
    100% {
      scale: 1;
    }
  }
  /* 마이크 전용 — 비트(msWave)와 같은 1.2s 에 맞춰 아주 작게(±2.5%) 진동. */
  @keyframes msMicBeat {
    0% {
      scale: 1;
    }
    7% {
      scale: 1.025;
    }
    18% {
      scale: 1;
    }
    100% {
      scale: 1;
    }
  }
  /* 밝기 맥동: 1 → 0.1 수준까지 확실히 어두워졌다 복귀(잘 보이게). 겹마다 폭/주기 다름. */
  @keyframes msDimA {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.1;
    }
  }
  @keyframes msDimB {
    0%,
    100% {
      opacity: 1;
    }
    45% {
      opacity: 0.14;
    }
  }
  @keyframes msDimC {
    0%,
    100% {
      opacity: 0.95;
    }
    55% {
      opacity: 0.08;
    }
  }
  .caption {
    text-align: center;
    text-shadow: 0 1px 6px rgba(0, 0, 0, 0.7);
  }
  /* 변환 시작 전(idle, .active 없음): 마이크만 보이고 별 파동(아우라)은 숨긴다.
     변환(.active)에 들어가면 아우라가 나타나며 애니메이션이 돈다. 마이크는 idle 에선 정지. */
  .hero:not(.active) .aura {
    display: none;
  }
  .hero:not(.active) .mic {
    animation: none;
  }
  /* 접근성: 모션 최소화 선호 시 정지. */
  @media (prefers-reduced-motion: reduce) {
    .aura,
    .mic {
      animation: none;
    }
  }
</style>
