param(
  [string]$Output = "src-tauri/resources/transfer-engine",
  [string]$TransferCoreOutput = "src-tauri/resources/transfer-core",
  [string]$ArchiveWorkerOutput = "src-tauri/resources/archive-worker"
)

$ErrorActionPreference = "Stop"
if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
  throw "Install PyInstaller before building the bundled Python engine."
}

$challengeRules = (Resolve-Path "engine/challenge_rules.json").Path
$tlds = (Resolve-Path "engine/tlds.txt").Path
pyinstaller --clean --onefile --name transfer-engine `
  --add-data "${challengeRules}:engine" `
  --add-data "${tlds}:engine" `
  --collect-all patchright `
  --exclude-module playwright `
  --exclude-module camoufox `
  --exclude-module numpy `
  --exclude-module PIL `
  --exclude-module Pillow `
  --exclude-module onnxruntime `
  --exclude-module ddddocr `
  --exclude-module scipy `
  --exclude-module torch `
  --exclude-module tkinter `
  --distpath $Output --workpath .build/pyinstaller --specpath .build/pyinstaller engine_entrypoint.py
if ($LASTEXITCODE -ne 0) { throw "Engine bundling failed." }

New-Item -ItemType Directory -Force -Path $TransferCoreOutput | Out-Null
cargo build --manifest-path src-tauri/Cargo.toml --release --bin transfer-core
if ($LASTEXITCODE -ne 0) { throw "Transfer core build failed." }
$coreName = if ($env:OS -eq "Windows_NT") { "transfer-core.exe" } else { "transfer-core" }
Copy-Item -Force "src-tauri/target/release/$coreName" (Join-Path $TransferCoreOutput $coreName)

New-Item -ItemType Directory -Force -Path $ArchiveWorkerOutput | Out-Null
cargo build --manifest-path src-tauri/Cargo.toml --release --bin archive-worker
if ($LASTEXITCODE -ne 0) { throw "Archive worker build failed." }
$archiveName = if ($env:OS -eq "Windows_NT") { "archive-worker.exe" } else { "archive-worker" }
Copy-Item -Force "src-tauri/target/release/$archiveName" (Join-Path $ArchiveWorkerOutput $archiveName)
