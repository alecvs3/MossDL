# Exercise the generated production NSIS install/uninstall logic under a distinct
# product identity, so the user's real installation and registry entry stay intact.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot/..").Path
$generated = Join-Path $root 'src-tauri/target/release/nsis/x64'
$work = Join-Path $root '.build/installer-smoke'
New-Item -ItemType Directory -Force -Path $work | Out-Null
$install = Join-Path $work 'installed'
$output = Join-Path $work 'validation-setup.exe'
$text = Get-Content -LiteralPath (Join-Path $generated 'installer.nsi') -Raw
$text = $text.Replace('!define PRODUCTNAME "MossDL"', '!define PRODUCTNAME "MossDLValidation"')
$text = $text.Replace('!define MAINBINARYNAME "MossDL"', '!define MAINBINARYNAME "MossDLValidation"')
$text = $text.Replace('!define BUNDLEID "ai.transfer.manager"', '!define BUNDLEID "ai.transfer.manager.validation"')
$text = $text.Replace('  File "${MAINBINARYSRCPATH}"', '  File /oname=${MAINBINARYNAME}.exe "${MAINBINARYSRCPATH}"')
$text = $text.Replace('!define OUTFILE "nsis-output.exe"', ('!define OUTFILE "' + $output + '"'))
$source = Join-Path $work 'validation.nsi'
[IO.File]::WriteAllText($source, $text, [Text.UTF8Encoding]::new($false))
$nsis = Join-Path $env:LOCALAPPDATA 'tauri/NSIS/makensis.exe'
Push-Location $generated
try {
  & $nsis /NOCD /V2 $source
  if ($LASTEXITCODE -ne 0) { throw 'Validation installer compilation failed' }
} finally { Pop-Location }
$setup = Start-Process -FilePath $output -ArgumentList @('/S', '/NS', ('/D=' + $install)) -WindowStyle Hidden -Wait -PassThru
if ($setup.ExitCode -ne 0) { throw "Installer failed: $($setup.ExitCode)" }
foreach ($relative in @('MossDLValidation.exe', 'uninstall.exe', 'resources/transfer-engine/transfer-engine.exe', 'resources/transfer-core/transfer-core.exe')) {
  if (-not (Test-Path -LiteralPath (Join-Path $install $relative))) { throw "Missing installed file: $relative" }
}
$registry = 'HKCU:/Software/Microsoft/Windows/CurrentVersion/Uninstall/MossDLValidation'
if (-not (Test-Path $registry)) { throw 'Uninstall registration missing' }
python "$PSScriptRoot/verify_packaged_engine.py" (Join-Path $install 'resources')
if ($LASTEXITCODE -ne 0) { throw 'Installed engine smoke failed' }
$uninstall = Start-Process -FilePath (Join-Path $install 'uninstall.exe') -ArgumentList @('/S', ('_?=' + $install)) -WindowStyle Hidden -Wait -PassThru
if ($uninstall.ExitCode -ne 0) { throw "Uninstaller failed: $($uninstall.ExitCode)" }
if (Test-Path -LiteralPath (Join-Path $install 'MossDLValidation.exe')) { throw 'Application remains after uninstall' }
if (Test-Path -LiteralPath (Join-Path $install 'resources/transfer-engine/transfer-engine.exe')) { throw 'Engine remains after uninstall' }
if (Test-Path $registry) { throw 'Uninstall registry entry remains' }
Write-Output 'NSIS install/uninstall smoke passed (isolated product identity)'
