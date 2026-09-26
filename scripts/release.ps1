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

python scripts/build_browser_extension.py package --output "$out/browser-extension"
if ($LASTEXITCODE -ne 0) { throw "Browser extension packaging failed." }

$overrides = @{ bundle = @{ windows = @{} } }
if ($env:WINDOWS_SIGN_COMMAND) {
  $overrides.bundle.windows.signCommand = $env:WINDOWS_SIGN_COMMAND
} else {
  Write-Warning "WINDOWS_SIGN_COMMAND is not set: the installers will be unsigned (SmartScreen will warn users)."
}
$updates = $env:TAURI_SIGNING_PRIVATE_KEY -and $env:MOSSDL_UPDATER_PUBKEY -and $env:MOSSDL_UPDATE_URL
if ($updates) {
  $overrides.bundle.createUpdaterArtifacts = $true
} else {
  Write-Warning "Update signing is not configured: this build will report 'updates not configured' in Settings."
}
$configFile = Join-Path $out "tauri.release.json"
$overrides | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $configFile

npx tauri build --config $configFile
if ($LASTEXITCODE -ne 0) { throw "tauri build failed." }

$bundle = "src-tauri/target/release/bundle"
Get-ChildItem "$bundle/nsis/*.exe", "$bundle/msi/*.msi", "$bundle/nsis/*.sig", "$bundle/msi/*.sig" -ErrorAction SilentlyContinue |
  Copy-Item -Destination $out

if ($updates) {
  $installer = Get-ChildItem "$bundle/nsis/*-setup.exe" | Select-Object -First 1
  $signature = Get-Content "$($installer.FullName).sig" -Raw
  $base = if ($env:MOSSDL_DOWNLOAD_BASE_URL) { $env:MOSSDL_DOWNLOAD_BASE_URL.TrimEnd("/") } else { throw "Set MOSSDL_DOWNLOAD_BASE_URL to write latest.json." }
  @{
    version = $version
    pub_date = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    platforms = @{ "windows-x86_64" = @{ signature = $signature.Trim(); url = "$base/$($installer.Name)" } }
  } | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $out "latest.json")
}

Write-Host "Release $version is in $out"
