param(
  [string]$Output = "src-tauri/resources/transfer-engine",
  [string]$TransferCoreOutput = "src-tauri/resources/transfer-core",
  [string]$ArchiveWorkerOutput = "src-tauri/resources/archive-worker"
)

$ErrorActionPreference = "Stop"
if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
  throw "Install PyInstaller before building the bundled Python engine."
}

pyinstaller --clean --onefile --name transfer-engine --distpath $Output --workpath .build/pyinstaller --specpath .build/pyinstaller engine_entrypoint.py

New-Item -ItemType Directory -Force -Path $TransferCoreOutput | Out-Null
cargo build --manifest-path src-tauri/Cargo.toml --release --bin transfer-core
$coreName = if ($env:OS -eq "Windows_NT") { "transfer-core.exe" } else { "transfer-core" }
Copy-Item -Force "src-tauri/target/release/$coreName" (Join-Path $TransferCoreOutput $coreName)

New-Item -ItemType Directory -Force -Path $ArchiveWorkerOutput | Out-Null
cargo build --manifest-path src-tauri/Cargo.toml --release --bin archive-worker
$archiveName = if ($env:OS -eq "Windows_NT") { "archive-worker.exe" } else { "archive-worker" }
Copy-Item -Force "src-tauri/target/release/$archiveName" (Join-Path $ArchiveWorkerOutput $archiveName)
