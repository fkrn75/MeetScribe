# MeetScribe 개발용 사이드카 바이너리 배치 스크립트
# ─────────────────────────────────────────────────────────────
# 배경: src-tauri/tauri.conf.json 의 externalBin 은
#   binaries/meetscribe-sidecar-<target-triple>.exe 를 spawn 한다.
# 배포(번들)용은 PyInstaller 로 사이드카를 동결해 이 경로에 넣지만(후속 작업),
# 개발 단계에선 venv 의 콘솔 스크립트(meetscribe-sidecar.exe)를 그대로 복사해 쓴다.
#   - 이 exe 는 venv python 절대경로를 내장하므로 sidecar/.venv 가 그 자리에 있어야 동작(개발 PC 전용).
#   - .gitignore 가 *.exe 를 무시하므로 이 산출물은 커밋되지 않는다(머신 종속 → 정상).
#
# 사용:  프로젝트 루트에서  →  powershell -ExecutionPolicy Bypass -File scripts/setup-dev-sidecar.ps1
# 선행:  sidecar/.venv 에 'pip install -e sidecar' 로 meetscribe-sidecar 콘솔 스크립트가 생성돼 있어야 함.
# 이후:  HF_TOKEN 환경변수를 둔 채  npm run tauri dev

$ErrorActionPreference = 'Stop'

# scripts/ 의 부모 = 프로젝트 루트
$root = Split-Path -Parent $PSScriptRoot
$venvExe = Join-Path $root 'sidecar\.venv\Scripts\meetscribe-sidecar.exe'

if (-not (Test-Path $venvExe)) {
    Write-Error "venv 사이드카 콘솔 스크립트 없음: $venvExe`n→ 먼저 'pip install -e sidecar' 로 생성하세요."
}

# rustc 호스트 트리플 자동 감지(예: x86_64-pc-windows-msvc)
$hostLine = (rustc -vV | Select-String '^host:').ToString()
$triple = $hostLine.Split(':')[1].Trim()

$dstDir = Join-Path $root 'src-tauri\binaries'
$dst = Join-Path $dstDir "meetscribe-sidecar-$triple.exe"

New-Item -ItemType Directory -Force $dstDir | Out-Null
Copy-Item $venvExe $dst -Force

Write-Host "배치 완료: $dst" -ForegroundColor Green
Write-Host "이제 'npm run tauri dev' 로 앱을 띄울 수 있습니다(HF_TOKEN 환경변수 필요)." -ForegroundColor Cyan
