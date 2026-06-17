import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

// Svelte 5 설정. TypeScript 전처리를 위해 vitePreprocess 사용.
export default {
  preprocess: vitePreprocess(),
};
