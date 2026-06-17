/// <reference types="svelte" />
/// <reference types="vite/client" />

// 커스텀 환경변수 타입 선언.
// 사이드카 포트는 Tauri/빌드 단계에서 VITE_SIDECAR_PORT 로 주입된다(없으면 기본 8765).
interface ImportMetaEnv {
  readonly VITE_SIDECAR_PORT?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
