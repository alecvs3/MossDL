[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$baselinePath = Join-Path $root 'tests/fixtures/quality/full_gate_baseline.json'
$reportPath = Join-Path $root '.test-artifacts/quality-gate.json'
$baseline = Get-Content -Raw $baselinePath | ConvertFrom-Json

& python (Join-Path $root 'scripts/check_all.py')
$gateExit = $LASTEXITCODE
if ($null -eq $gateExit) { throw 'check_all.py did not provide an exit code' }

if (-not (Test-Path $reportPath)) {
  throw "Full quality gate did not create $reportPath"
}
$report = Get-Content -Raw $reportPath | ConvertFrom-Json
$failed = @($report.results | Where-Object { $_.status -ne 'passed' })

if ($failed.Count -eq 0) {
  Write-Output 'QUALITY BASELINE PASS: full gate is clean'
  exit 0
}

if ($gateExit -eq 0) { throw 'Quality gate report contains failures but check_all.py exited zero' }
if ($failed.Count -ne 1) { throw "QUALITY BASELINE FAIL: unexpected failure count $($failed.Count)" }

$failureLines = @($failed[0].output_tail -split "`r?`n" | Where-Object {
  $_ -match '^\s*(FAIL|ERROR):\s+'
})
if ($failureLines.Count -ne 1) {
  throw "QUALITY BASELINE FAIL: expected exactly one failure diagnostic, found $($failureLines.Count)"
}

$allowed = @($baseline.allowed_outcomes | Where-Object {
  $_.failed_check -eq $failed[0].name -and
  $failureLines[0] -like "*$($_.signature)*" -and
  $failed[0].output_tail -like "*$($_.signature)*" -and
  $failed[0].output_tail -like "*$($_.assertion)*"
})
if ($allowed.Count -ne 1) {
  throw "QUALITY BASELINE FAIL: failure is new or differs from the tracked telemetry signature"
}
$summaryMatch = [regex]::Match($failed[0].output_tail, 'FAILED \((?<details>[^)]*)\)')
if (-not $summaryMatch.Success) { throw 'QUALITY BASELINE FAIL: unittest summary is missing' }
$details = $summaryMatch.Groups['details'].Value
$failureCountMatch = [regex]::Match($details, '(^|,\s*)failures=(?<count>\d+)')
$errorCountMatch = [regex]::Match($details, '(^|,\s*)errors=(?<count>\d+)')
$expectedFailures = [int]$allowed[0].failure_count
$expectedErrors = [int]$allowed[0].error_count
$actualFailures = if ($failureCountMatch.Success) { [int]$failureCountMatch.Groups['count'].Value } else { 0 }
$actualErrors = if ($errorCountMatch.Success) { [int]$errorCountMatch.Groups['count'].Value } else { 0 }
if ($actualFailures -ne $expectedFailures -or $actualErrors -ne $expectedErrors) {
  throw "QUALITY BASELINE FAIL: expected failures=$expectedFailures/errors=$expectedErrors, found failures=$actualFailures/errors=$actualErrors"
}
Write-Output "QUALITY BASELINE PASS: allowed pre-existing failure $($failed[0].name)"
exit 0
