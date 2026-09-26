# Builds a MossDL release: quality gate, bundled engine and core, store-ready
# browser extensions, and the Windows installers (NSIS and MSI).
#
# Optional, from the environment (nothing is signed or published without them):
#   WINDOWS_SIGN_COMMAND        Code signing, run by Tauri per file with %1 as the
#                               path, e.g. 'signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /a "%1"'
#                               or Azure Trusted Signing's 'trusted-signing-cli ... %1'.
#   TAURI_SIGNING_PRIVATE_KEY   (+ _PASSWORD) The update signing key from `npx tauri signer generate`.
#   MOSSDL_UPDATER_PUBKEY      Its public half; baked into the app to verify updates.
#   MOSSDL_UPDATE_URL          Where the app reads latest.json, e.g. https://example.com/mossdl/latest.json
#   MOSSDL_DOWNLOAD_BASE_URL   Where the installers will be hosted; used to write latest.json.
#
# Output: dist/release/<version>/ with the installers, extension zips and, when
# updates are configured, the .sig files and latest.json to upload.
param([switch]$SkipGate)

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path "$PSScriptRoot/..")
$version = (Get-Content src-tauri/tauri.conf.json -Raw | ConvertFrom-Json).version
$out = "dist/release/$version"
New-Item -ItemType Directory -Force -Path $out | Out-Null

if (-not $SkipGate) {
  python scripts/check_all.py --profile fast
  if ($LASTEXITCODE -ne 0) { throw "Quality gate failed; not building a release." }
}

& "$PSScriptRoot/build_engine.ps1"
if ($LASTEXITCODE -ne 0) { throw "Engine build failed." }
python "$PSScriptRoot/verify_packaged_engine.py"
if ($LASTEXITCODE -ne 0) { throw "Packaged engine smoke failed." }

$overrides = @{ bundle = @{ windows = @{} } }
if ($env:WINDOWS_SIGN_COMMAND) {
  $overrides.bundle.windows.signCommand = $env:WINDOWS_SIGN_COMMAND
} else {
  Write-Warning "WINDOWS_SIGN_COMMAND is not set: the installers will be unsigned (SmartScreen will warn users)."
}
$updates = $env:TAURI_SIGNING_PRIVATE_KEY -and $env:MOSSDL_UPDATER_PUBKEY -and $env:MOSSDL_UPDATE_URL
if ($updates) {
  $overrides.bundle.createUpdaterArtifacts = $true
  $overrides.plugins = @{ updater = @{
    pubkey = $env:MOSSDL_UPDATER_PUBKEY.Trim()
    endpoints = @($env:MOSSDL_UPDATE_URL)
  } }
} else {
  Write-Warning "Update signing is not configured: this build will report 'updates not configured' in Settings."
}

New-Item -ItemType Directory -Force -Path ".build" | Out-Null
$configFile = Join-Path (Resolve-Path ".build") "tauri.release.json"
$overrides | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $configFile

npm run build
if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }
$previousConfig = $env:TAURI_CONFIG
try {
  $env:TAURI_CONFIG = Get-Content $configFile -Raw
  cargo build --manifest-path src-tauri/Cargo.toml --release --features custom-protocol --bin transfer-manager
  if ($LASTEXITCODE -ne 0) { throw "Desktop build failed." }
} finally { $env:TAURI_CONFIG = $previousConfig }
Copy-Item src-tauri/target/release/transfer-manager.exe src-tauri/target/release/MossDL.exe
npx tauri bundle --config $configFile
if ($LASTEXITCODE -ne 0) { throw "Installer bundling/signing failed." }

New-Item -ItemType Directory -Force -Path $out | Out-Null

python scripts/build_browser_extension.py package --output "$out/browser-extension"
if ($LASTEXITCODE -ne 0) { throw "Browser extension packaging failed." }

$bundle = "src-tauri/target/release/bundle"
Get-ChildItem "$bundle/nsis/*_$($version)_*.exe", "$bundle/msi/*_$($version)_*.msi", "$bundle/nsis/*_$($version)_*.sig", "$bundle/msi/*_$($version)_*.sig" -ErrorAction SilentlyContinue |
  Copy-Item -Destination $out

if ($updates) {
  $installer = Get-Item "$bundle/nsis/MossDL_$($version)_x64-setup.exe"
  $signature = Get-Content "$($installer.FullName).sig" -Raw
  $base = if ($env:MOSSDL_DOWNLOAD_BASE_URL) { $env:MOSSDL_DOWNLOAD_BASE_URL.TrimEnd("/") } else { "https://github.com/alecvs3/MossDL/releases/download/v$version" }
  @{
    version = $version
    pub_date = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    platforms = @{ "windows-x86_64" = @{ signature = $signature.Trim(); url = "$base/$($installer.Name)" } }
  } | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $out "latest.json")
  python "$PSScriptRoot/verify_release.py" $out
  if ($LASTEXITCODE -ne 0) { throw "Release signature verification failed." }
}

Write-Host "Release $version is in $out"
