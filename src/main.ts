// 앱 진입점. Svelte 5 `mount` API로 App을 #app에 부착한다.
import { mount } from "svelte";
import "./app.css";
import App from "./App.svelte";

const target = document.getElementById("app");
if (!target) {
  throw new Error("#app 엘리먼트를 찾을 수 없습니다 (index.html 확인).");
}

const app = mount(App, { target });

export default app;
