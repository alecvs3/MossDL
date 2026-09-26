<#
.SYNOPSIS
  ExpressVPN Route & Proxy Controller for Nexload / Transfer Manager.
.DESCRIPTION
  This script provides safe, non-disruptive proxy endpoint discovery, Docker-based SOCKS5 container
  controls, and system VPN rotation helper with active connection safety warnings.
#>

[CmdletBinding()]
param (
    [Parameter(Position = 0)]
    [ValidateSet("status", "list-regions", "test-ip", "rotate", "docker-up", "docker-down", "register-route")]
    [string]$Action = "status",

    [Parameter(Position = 1)]
    [string]$Target = ""
)

$ExpressVpnCtl = "C:\Program Files\ExpressVPN\expressvpnctl.exe"
$AppDb = "$env:APPDATA\ai.transfer.manager\downloads.sqlite3"

function Show-Status {
    Write-Host "`n=== EXPRESSVPN HOST STATUS ===" -ForegroundColor Cyan
    if (Test-Path $ExpressVpnCtl) {
        & $ExpressVpnCtl status
        $region = & $ExpressVpnCtl get region
        $vpnip = & $ExpressVpnCtl get vpnip
        $state = & $ExpressVpnCtl get connectionstate
        Write-Host "Active Region: $region" -ForegroundColor Yellow
        Write-Host "Assigned VPN IP: $vpnip" -ForegroundColor Yellow
        Write-Host "State: $state" -ForegroundColor Yellow
    } else {
        Write-Host "expressvpnctl.exe not found at $ExpressVpnCtl" -ForegroundColor Red
    }

    Write-Host "`n=== LIVE PUBLIC IP (from this shell) ===" -ForegroundColor Cyan
    try {
        $ipInfo = Invoke-RestMethod -Uri "http://ip-api.com/json" -TimeoutSec 4
        Write-Host "Public IP : $($ipInfo.query)" -ForegroundColor Green
        Write-Host "Country   : $($ipInfo.country) ($($ipInfo.countryCode))" -ForegroundColor Green
        Write-Host "City      : $($ipInfo.city), $($ipInfo.regionName)" -ForegroundColor Green
        Write-Host "ISP       : $($ipInfo.isp)" -ForegroundColor Green
    } catch {
        Write-Host "Failed to query public IP: $_" -ForegroundColor Red
    }
}

function List-Regions {
    Write-Host "`n=== AVAILABLE EXPRESSVPN REGIONS ===" -ForegroundColor Cyan
    if (Test-Path $ExpressVpnCtl) {
        $regions = & $ExpressVpnCtl get regions
        $count = ($regions | Measure-Object).Count
        Write-Host "Found $count regions." -ForegroundColor Green
        $regions | Select-Object -First 30 | ForEach-Object { Write-Host "  $_" -ForegroundColor Gray }
        if ($count -gt 30) {
            Write-Host "  ... and $($count - 30) more. (Run with specific region to connect)" -ForegroundColor DarkGray
        }
    }
}

function Rotate-Vpn {
    param([string]$NewRegion)
    Write-Host "`n[WARNING] Full-system VPN switching resets the virtual network adapter." -ForegroundColor Yellow
    Write-Host "[WARNING] This briefly interrupts active WebSocket and TCP streams (including remote AI sessions)." -ForegroundColor Yellow
    
    if (-not $NewRegion) {
        $choices = @("canada-toronto", "usa-new-york", "usa-chicago", "uk-london", "germany-frankfurt-1", "netherlands-amsterdam")
        $current = & $ExpressVpnCtl get region
        $filtered = $choices | Where-Object { $_ -ne $current }
        $NewRegion = $filtered | Get-Random
    }

    Write-Host "Switching ExpressVPN host connection to: $NewRegion..." -ForegroundColor Cyan
    & $ExpressVpnCtl connect $NewRegion
    Start-Sleep -Seconds 3
    Show-Status
}

function Register-RouteProfile {
    param([string]$Endpoint, [string]$ProfileId, [string]$Kind)
    if (-not $Endpoint) {
        $Endpoint = "socks5://127.0.0.1:1080"
    }
    if (-not $ProfileId) {
        $ProfileId = "expressvpn-docker-socks5"
    }
    if (-not $Kind) {
        $Kind = "socks5"
    }

    Write-Host "`nRegistering proxy route into Transfer Manager database..." -ForegroundColor Cyan
    $pythonCmd = @"
import sqlite3
db_path = r'$AppDb'
con = sqlite3.connect(db_path)
con.execute('''
INSERT INTO route_profiles (id, kind, endpoint, credential_ref, region, healthcheck_url, enabled, config_json)
VALUES (?, ?, ?, NULL, ?, 'https://api.ipify.org?format=json', 1, '{}')
ON CONFLICT(id) DO UPDATE SET endpoint=excluded.endpoint, enabled=1
''', ('$ProfileId', '$Kind', '$Endpoint', 'expressvpn-isolated'))
con.commit()
con.close()
print('Successfully registered route profile: $ProfileId -> $Endpoint')
"@
    python -c $pythonCmd
}

switch ($Action) {
    "status"         { Show-Status }
    "list-regions"   { List-Regions }
    "test-ip"        { Show-Status }
    "rotate"         { Rotate-Vpn -NewRegion $Target }
    "register-route" { Register-RouteProfile -Endpoint $Target }
    "docker-up"      {
        Write-Host "Launching isolated ExpressVPN Docker SOCKS5 container..." -ForegroundColor Cyan
        Set-Location "$PSScriptRoot\expressvpn-docker"
        docker compose up -d
        Set-Location $PSScriptRoot
    }
    "docker-down"    {
        Write-Host "Stopping ExpressVPN Docker SOCKS5 container..." -ForegroundColor Cyan
        Set-Location "$PSScriptRoot\expressvpn-docker"
        docker compose down
        Set-Location $PSScriptRoot
    }
}
