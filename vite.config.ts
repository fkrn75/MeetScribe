import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// Tauri 2 프론트엔드용 Vite 설정.
// 공식 가이드(https://v2.tauri.app/start/frontend/) 권장값을 따른다.
//   - dev 서버 포트 1420 고정(Tauri devUrl과 일치)
//   - HMR/clearScreen 등 Tauri 개발 편의 설정
//   - VITE_SIDECAR_PORT 같은 사이드카 주입 환경변수는 `VITE_` prefix로 노출
const host = process.env.TAURI_DEV_HOST;

export default defineConfig({
  plugins: [svelte()],

  // import.meta.env 로 노출할 환경변수 prefix.
  // 사이드카 포트(VITE_SIDECAR_PORT)와 Tauri 빌드 변수(TAURI_)를 허용.
  envPrefix: ["VITE_", "TAURI_"],

  // Tauri는 고정 포트를 기대한다(실패 시 즉시 에러로 알 수 있게 strictPort).
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1421,
        }
      : undefined,
    watch: {
      // src-tauri(Rust) 변경은 Vite가 감시하지 않는다(Tauri가 담당).
      ignored: ["**/src-tauri/**"],
    },
  },

  build: {
    // Tauri 번들이 가리킬 정적 산출물 위치(= src-tauri/tauri.conf.json 의 frontendDist: "../dist").
    outDir: "dist",
    emptyOutDir: true,
    // 디버그 빌드 시 소스맵, 릴리스 시 최소화.
    target: process.env.TAURI_ENV_PLATFORM === "windows" ? "chrome105" : "safari13",
    minify: !process.env.TAURI_ENV_DEBUG ? "esbuild" : false,
    sourcemap: !!process.env.TAURI_ENV_DEBUG,
  },
});
