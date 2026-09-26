$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LogDir = Join-Path $Root ".test-artifacts"
$LogPath = Join-Path $LogDir "tauri-dev.log"
$Vite = $null

New-Item -ItemType Directory -Force $LogDir | Out-Null

function Write-DevLog([string] $Message) {
    $redacted = $Message -replace '(?i)(authorization|cookie|token|secret|password|signature)[^\r\n]*', '$1=<redacted>'
    Add-Content -LiteralPath $LogPath -Value ("[{0:u}] {1}" -f (Get-Date), $redacted)
}

function Test-Vite {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:1420/" -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

function Get-ExistingApp {
    return Get-Process -Name "transfer-manager" -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
}

function Focus-ExistingApp($existing) {
    if ($null -eq $existing) { return $false }
    try {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.AppActivate($existing.Id)
    } catch {
        Write-DevLog "Existing app detected but could not be focused: $($_.Exception.GetType().Name)"
    }
    return $true
}

function Test-ExeFresh([string] $path) {
    if (-not (Test-Path -LiteralPath $path)) { return $false }
    $exeTime = (Get-Item -LiteralPath $path).LastWriteTimeUtc
    $sourceRoot = Join-Path $Root "src-tauri"
    $latestSource = Get-ChildItem -LiteralPath $sourceRoot -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object {
            $_.FullName -notlike "*\src-tauri\target\*" -and
            ($_.Extension -in @(".rs", ".toml", ".json", ".jsonc") -or $_.Name -eq "build.rs")
        } |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    return ($null -eq $latestSource -or $exeTime -ge $latestSource.LastWriteTimeUtc)
}

try {
    $exe = Join-Path $Root "src-tauri\target\debug\transfer-manager.exe"
    $existing = Get-ExistingApp
    $exeFresh = Test-ExeFresh $exe
    if ($null -ne $existing -and $exeFresh -and (Test-Vite)) {
        if (Focus-ExistingApp $existing) {
            Write-DevLog "Existing Transfer Manager dev instance focused"
            exit 0
        }
    }
    if ($null -ne $existing) {
        $existingPath = $null
        try { $existingPath = $existing.Path } catch {}
        if ($existingPath -and ((Resolve-Path $existingPath).Path -eq (Resolve-Path $exe).Path)) {
            # This is the same exact GUI binary, so replacing it is safe when
            # switching a packaged instance into managed development mode.
            Write-DevLog "Replacing existing standalone Transfer Manager instance"
            Stop-Process -Id $existing.Id -Force -ErrorAction Stop
            $existing.WaitForExit(5000)
        } else {
            Write-DevLog "Unrelated Transfer Manager process detected; refusing to terminate it"
            throw "another Transfer Manager executable is already running"
        }
    }

    if (-not (Test-Vite)) {
        Write-DevLog "Starting launcher-owned Vite process"
        $Vite = Start-Process -FilePath "npm.cmd" -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1") `
            -WorkingDirectory $Root -WindowStyle Hidden -PassThru
        $ready = $false
        for ($i = 0; $i -lt 60; $i++) {
            if ($Vite.HasExited) { throw "Vite exited before becoming ready" }
            if (Test-Vite) { $ready = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $ready) { throw "Vite did not become ready on 127.0.0.1:1420" }
    }

    if (-not $exeFresh -or -not (Test-Path -LiteralPath $exe)) {
        Write-DevLog "Building the development GUI executable"
        $buildOut = Join-Path $LogDir "tauri-build.stdout.log"
        $buildErr = Join-Path $LogDir "tauri-build.stderr.log"
        $cargo = (Get-Command cargo.exe -ErrorAction Stop).Source
        # Invoke Cargo directly from this hidden launcher.  PowerShell's
        # Start-Process -Wait can leave a remoting Process wrapper blocked on
        # N: drives even after cargo has exited, preventing the GUI launch.
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & $cargo "build" "--manifest-path" (Join-Path $Root "src-tauri\Cargo.toml") "--bin" "transfer-manager" *> $buildOut
        $buildExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorAction
        if ($buildExitCode -ne 0 -or -not (Test-Path -LiteralPath $exe)) {
            Write-DevLog ("Cargo build failed with exit code {0}" -f $buildExitCode)
            throw "Tauri GUI executable was not produced"
        }
        Write-DevLog ("Cargo build completed with exit code {0}" -f $buildExitCode)
    }

    $env:TRANSFER_DEV_URL = "http://127.0.0.1:1420/"
    Write-DevLog "Launching Transfer Manager GUI"
    $app = Start-Process -FilePath $exe -WorkingDirectory $Root -PassThru
    $windowReady = $false
    for ($i = 0; $i -lt 30; $i++) {
        if ($app.HasExited) { throw "Transfer Manager exited before creating its main window" }
        $current = Get-Process -Id $app.Id -ErrorAction SilentlyContinue
        if ($null -ne $current -and $current.MainWindowHandle -ne 0) {
            $windowReady = $true
            Write-DevLog ("Transfer Manager main window ready (pid {0})" -f $app.Id)
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $windowReady) {
        Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
        throw "Transfer Manager did not create a visible main window within 15 seconds"
    }
    $app.WaitForExit()
    Write-DevLog ("Transfer Manager exited with code {0}" -f $app.ExitCode)
    exit $app.ExitCode
} catch {
    Write-DevLog ("Startup failure: {0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message)
    exit 1
} finally {
    if ($null -ne $Vite -and -not $Vite.HasExited) {
        Write-DevLog "Stopping launcher-owned Vite process"
        Stop-Process -Id $Vite.Id -Force -ErrorAction SilentlyContinue
    }
}
