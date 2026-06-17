// MeetScribe 데스크탑 셸 — Tauri 2 진입점.
//
// 책임(설계서 §3.4 사이드카 Lifecycle):
//   1. 앱 시작 시 Python 사이드카(externalBin = "meetscribe-sidecar")를 spawn.
//      - 포트는 환경변수 VITE_SIDECAR_PORT(기본 8765)로 결정하고, 같은 값을
//        사이드카에도 env로 넘겨 uvicorn이 그 포트에 바인딩하게 한다.
//   2. `http://127.0.0.1:<port>/health` 를 폴링 → 200이 오면(서버 준비됨)
//      숨겨둔 메인 윈도우를 표시한다(콜드스타트 동안 빈 창 깜빡임 방지).
//   3. 앱 종료 시 사이드카 프로세스를 확실히 kill → 좀비 프로세스 방지.
//
// UI 로직은 100% 프론트(WebView). 여기서는 셸/lifecycle만 다룬다.
//
// 포트 전달 계약(4팀원 합의 = 정적 8765 고정):
//   - 포트는 고정값(기본 8765). 키 VITE_SIDECAR_PORT로 양쪽(프론트·사이드카)이 동일값 공유.
//   - 프론트는 `import.meta.env.VITE_SIDECAR_PORT ?? "8765"`로 읽는다(빌드타임 주입+폴백).
//   - Rust는 같은 env(없으면 8765)를 사이드카에 .env로 넘긴다 → 3자 포트 일치.
//   - 동적 빈 포트 할당은 채택하지 않음(팀 합의). 고정값이라 invoke 조회 커맨드 불필요.
//
// ⚠️ 이 PC엔 Rust/Tauri 툴체인 미설치 → cargo build 검증 불가. 구조·API 사용 정확성까지.

// 릴리스 빌드(Windows)에서 콘솔 창이 함께 뜨지 않도록.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent, State, WindowEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// 사이드카가 listen할 기본 포트. 프론트(VITE_SIDECAR_PORT)와 반드시 일치.
const DEFAULT_SIDECAR_PORT: u16 = 8765;
/// externalBin에 등록한 사이드카 바이너리 base name(tauri.conf.json과 일치).
const SIDECAR_BIN: &str = "meetscribe-sidecar";

/// 살아있는 사이드카 자식 프로세스 핸들을 앱 전역에서 공유.
///
/// 종료 시 kill에 쓰며, 한 번 kill하면 take()로 비운다(중복 kill 방지).
/// Mutex<Option<..>>: 아직 안 떴거나(None) 이미 종료한(None) 상태를 표현.
#[derive(Default)]
struct SidecarProcess(Mutex<Option<CommandChild>>);

impl SidecarProcess {
    /// 사이드카를 kill하고 핸들을 비운다. 이미 비어있으면 무시(멱등).
    fn kill(&self) {
        if let Ok(mut guard) = self.0.lock() {
            if let Some(child) = guard.take() {
                // 실패해도 종료 흐름을 막지 않는다(이미 죽었을 수 있음).
                let _ = child.kill();
            }
        }
    }
}

/// 사이드카 포트를 확정한다(팀 합의: 정적 고정).
/// VITE_SIDECAR_PORT 환경변수에서 읽되, 없거나 파싱 실패면 기본 8765.
fn resolve_port() -> u16 {
    std::env::var("VITE_SIDECAR_PORT")
        .ok()
        .and_then(|s| s.trim().parse::<u16>().ok())
        .unwrap_or(DEFAULT_SIDECAR_PORT)
}

/// 사이드카를 spawn한다. 자식 핸들을 SidecarProcess 상태에 저장하고,
/// 사이드카의 stdout/stderr를 백그라운드에서 흘려보내 로그로 남긴다.
fn spawn_sidecar(app: &tauri::AppHandle, port: u16) -> Result<(), String> {
    // externalBin 사이드카 커맨드 구성.
    let sidecar = app
        .shell()
        .sidecar(SIDECAR_BIN)
        .map_err(|e| format!("사이드카 커맨드 생성 실패({SIDECAR_BIN}): {e}"))?
        // 포트를 사이드카에 env로 전달 → uvicorn이 같은 포트에 바인딩.
        .env("VITE_SIDECAR_PORT", port.to_string());

    let (mut rx, child) = sidecar
        .spawn()
        .map_err(|e| format!("사이드카 spawn 실패: {e}"))?;

    // 자식 핸들 저장(종료 시 kill용).
    let state: State<SidecarProcess> = app.state();
    if let Ok(mut guard) = state.0.lock() {
        *guard = Some(child);
    }

    // 사이드카 출력 펌프: 채널이 닫힐 때까지(=프로세스 종료) 이벤트를 소비.
    // 받아만 두지 않으면 파이프 버퍼가 차서 사이드카가 블록될 수 있다.
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    println!("[sidecar] {}", String::from_utf8_lossy(&line));
                }
                CommandEvent::Stderr(line) => {
                    eprintln!("[sidecar:err] {}", String::from_utf8_lossy(&line));
                }
                CommandEvent::Error(err) => {
                    eprintln!("[sidecar:error] {err}");
                }
                CommandEvent::Terminated(payload) => {
                    eprintln!("[sidecar] 종료됨: {:?}", payload);
                    break;
                }
                _ => {}
            }
        }
    });

    Ok(())
}

/// /health 를 폴링해 서버가 준비되면 메인 윈도우를 표시한다.
///
/// 별 스레드에서 동작(메인/UI 스레드 블로킹 금지). 일정 시도 안에 준비되지 않으면
/// 그래도 창을 띄워(사용자가 에러 화면이라도 보게) 영구 숨김 상태를 피한다.
fn wait_health_then_show(app: tauri::AppHandle, port: u16) {
    std::thread::spawn(move || {
        let url = format!("http://127.0.0.1:{port}/health");
        // 각 시도 타임아웃 2s, 0.5s 간격, 최대 ~120s 대기(첫 실행 콜드스타트 여유).
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(2))
            .build();

        let client = match client {
            Ok(c) => c,
            Err(e) => {
                eprintln!("[health] HTTP 클라이언트 생성 실패: {e} → 창 강제 표시");
                show_main(&app);
                return;
            }
        };

        const MAX_ATTEMPTS: u32 = 240; // 240 * 0.5s = 120s
        for attempt in 1..=MAX_ATTEMPTS {
            match client.get(&url).send() {
                Ok(resp) if resp.status().is_success() => {
                    println!("[health] 준비 완료(시도 {attempt}) → 메인 윈도우 표시");
                    show_main(&app);
                    return;
                }
                _ => {
                    // 아직 listen 전이거나 비정상 응답 → 잠시 후 재시도.
                    std::thread::sleep(Duration::from_millis(500));
                }
            }
        }

        // 끝내 준비 안 됨: 그래도 창은 띄운다(프론트가 연결 실패 UI를 보여줄 수 있게).
        eprintln!("[health] 제한시간 내 준비 실패 → 창 강제 표시(프론트에서 에러 처리)");
        show_main(&app);
    });
}

/// 메인 윈도우를 표시하고 포커스를 준다. 윈도우가 없으면(이미 닫힘) 무시.
fn show_main(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.set_focus();
    }
}

fn main() {
    tauri::Builder::default()
        // 사이드카 spawn/kill에 필요한 shell 플러그인.
        .plugin(tauri_plugin_shell::init())
        // 프론트의 파일 열기/저장 다이얼로그(@tauri-apps/plugin-dialog) 지원.
        .plugin(tauri_plugin_dialog::init())
        // 사이드카 핸들 공유 상태 등록.
        .manage(SidecarProcess::default())
        .setup(|app| {
            let handle = app.handle().clone();
            let port = resolve_port();
            println!("[setup] 사이드카 포트 = {port}");

            // 1) 사이드카 기동. 실패해도 앱은 떠야 하므로(에러 표시 위해) 로그만 남기고 진행.
            if let Err(e) = spawn_sidecar(&handle, port) {
                eprintln!("[setup] {e}");
            }

            // 2) /health 준비되면 메인 윈도우 표시(별 스레드, 논블로킹).
            wait_health_then_show(handle, port);

            Ok(())
        })
        // 메인 윈도우가 닫히면 사이드카도 즉시 정리(좀비 방지).
        .on_window_event(|window, event| {
            if let WindowEvent::Destroyed = event {
                if window.label() == "main" {
                    let state: State<SidecarProcess> = window.state();
                    state.kill();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("Tauri 앱 빌드 실패")
        // 앱 전체 종료 시점에도 한 번 더 kill(멱등) → 어떤 경로로 끝나도 사이드카 회수.
        .run(|app, event| {
            if let RunEvent::ExitRequested { .. } | RunEvent::Exit = event {
                let state: State<SidecarProcess> = app.state();
                state.kill();
            }
        });
}
