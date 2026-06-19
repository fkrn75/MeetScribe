# MeetScribe 동결 사이드카 → Tauri 번들 배치 스크립트
# ─────────────────────────────────────────────────────────────
# PyInstaller onedir 산출(sidecar/dist/meetscribe-sidecar/)을 Tauri 번들 위치로 옮긴다:
#   - 메인 exe   → src-tauri/binaries/meetscribe-sidecar-<target-triple>.exe  (externalBin 규칙)
#   - _internal/ → src-tauri/resources/_internal/                            (bundle.resources)
# 런타임: Tauri 가 Windows 설치 루트에 sidecar exe 와 resources 를 같은 폴더로 전개하므로
#         onedir 부트로더가 자기 옆 _internal 을 정상 로드한다(Rust 수정 불필요).
#
# 사용:  프로젝트 루트에서  →  powershell -ExecutionPolicy Bypass -File scripts/stage-sidecar-bundle.ps1
# 선행:  sidecar 에서  pyinstaller meetscribe-sidecar.spec  로 dist/ 생성 완료.

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot          # scripts/ 의 부모 = 프로젝트 루트
$dist = Join-Path $root 'sidecar\dist\meetscribe-sidecar'
$distExe = Join-Path $dist 'meetscribe-sidecar.exe'
$distInternal = Join-Path $dist '_internal'

if (-not (Test-Path $distExe)) {
    Write-Error "동결 산출 exe 없음: $distExe`n→ 먼저 sidecar 에서 'pyinstaller meetscribe-sidecar.spec' 실행"
}
if (-not (Test-Path $distInternal)) {
    Write-Error "동결 산출 _internal 없음: $distInternal (onedir 빌드가 맞는지 확인)"
}

# rustc 호스트 트리플 자동 감지(예: x86_64-pc-windows-msvc) — externalBin suffix 와 일치시킨다.
$hostLine = (rustc -vV | Select-String '^host:').ToString()
$triple = $hostLine.Split(':')[1].Trim()

# 1) 메인 exe → binaries/ (target-triple suffix 부착)
$binDir = Join-Path $root 'src-tauri\binaries'
New-Item -ItemType Directory -Force $binDir | Out-Null
$dstExe = Join-Path $binDir "meetscribe-sidecar-$triple.exe"
Copy-Item $distExe $dstExe -Force
Write-Host "exe 배치 완료: $dstExe" -ForegroundColor Green

# 2) _internal/ → resources/_internal/ (기존 것 제거 후 통째 복사)
$resDir = Join-Path $root 'src-tauri\resources'
$dstInternal = Join-Path $resDir '_internal'
New-Item -ItemType Directory -Force $resDir | Out-Null
if (Test-Path $dstInternal) { Remove-Item $dstInternal -Recurse -Force }
Copy-Item $distInternal $dstInternal -Recurse -Force
$sz = "{0:N1}" -f ((Get-ChildItem $dstInternal -Recurse -File | Measure-Object Length -Sum).Sum / 1GB)
Write-Host "_internal 배치 완료: $dstInternal ($sz GB)" -ForegroundColor Green

Write-Host "스테이징 완료. 이제 'npm run tauri build' 로 .msi/.exe 를 생성하세요." -ForegroundColor Cyan
