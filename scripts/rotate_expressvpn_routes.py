#!/usr/bin/env python3
"""
ExpressVPN & Proxy Route Manager for Nexload / Transfer Manager.

Features:
- Query ExpressVPN CLI status and list all regions
- Safely test public IP and ISP via ip-api
- Register SOCKS5/HTTP proxy endpoints or WireGuard profiles directly into Transfer Manager's DB
- Control the Dockerized SOCKS5 container (isolated from host gateway)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

APP_DB = Path(os.environ.get("APPDATA", "")) / "ai.transfer.manager" / "downloads.sqlite3"
EXPRESSVPN_CTL = r"C:\Program Files\ExpressVPN\expressvpnctl.exe"


def get_public_ip() -> dict:
    req = urllib.request.Request("http://ip-api.com/json", headers={"User-Agent": "Nexload-RouteTool/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def expressvpnctl_exec(*args) -> str:
    if not os.path.exists(EXPRESSVPN_CTL):
        return ""
    try:
        res = subprocess.run([EXPRESSVPN_CTL, *args], capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception as exc:
        return f"Error: {exc}"


def register_profile(profile_id: str, kind: str, endpoint: str | None, region: str | None = None) -> None:
    if not APP_DB.exists():
        print(f"Transfer Manager DB not found at: {APP_DB}")
        return

    con = sqlite3.connect(APP_DB)
    con.execute(
        """
        INSERT INTO route_profiles (id, kind, endpoint, credential_ref, region, healthcheck_url, enabled, config_json)
        VALUES (?, ?, ?, NULL, ?, 'https://api.ipify.org?format=json', 1, '{}')
        ON CONFLICT(id) DO UPDATE SET
            endpoint=excluded.endpoint,
            kind=excluded.kind,
            region=excluded.region,
            enabled=1
        """,
        (profile_id, kind, endpoint, region),
    )
    con.commit()
    con.close()
    print(f"[OK] Saved Route Profile into Nexload DB: {profile_id} ({kind} -> {endpoint or 'interface'})")


def list_profiles() -> None:
    if not APP_DB.exists():
        print(f"Transfer Manager DB not found at: {APP_DB}")
        return

    con = sqlite3.connect(APP_DB)
    rows = con.execute("SELECT id, kind, endpoint, region, enabled FROM route_profiles").fetchall()
    con.close()
    print("\n=== CURRENT REGISTERED NEXLOAD ROUTE PROFILES ===")
    for row in rows:
        status = "ENABLED" if row[4] else "DISABLED"
        print(f" - [{status}] ID: {row[0]:<25} Kind: {row[1]:<12} Endpoint: {str(row[2]):<25} Region: {row[3]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ExpressVPN & Proxy Route Manager")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show ExpressVPN and public IP status")
    sub.add_parser("regions", help="List available ExpressVPN regions")
    sub.add_parser("list", help="List route profiles currently registered in Transfer Manager")

    reg_parser = sub.add_parser("register", help="Register a proxy route profile in Transfer Manager")
    reg_parser.add_argument("--id", required=True, help="Profile ID (e.g. expressvpn-socks5)")
    reg_parser.add_argument("--kind", default="socks5", choices=["socks5", "http_proxy", "docker_socks5", "wireguard", "direct"])
    reg_parser.add_argument("--endpoint", default=None, help="Proxy URL (e.g. socks5://127.0.0.1:1080)")
    reg_parser.add_argument("--region", default=None, help="Region label (e.g. Canada)")

    rotate_parser = sub.add_parser("rotate-cli", help="Switch ExpressVPN system region via expressvpnctl (Warning: drops connection!)")
    rotate_parser.add_argument("region", nargs="?", default="smart", help="Region name to connect to")

    args = parser.parse_args()

    if args.command == "status" or not args.command:
        print("=== EXPRESSVPN STATUS ===")
        if os.path.exists(EXPRESSVPN_CTL):
            print(f"Status: {expressvpnctl_exec('status')}")
            print(f"Region: {expressvpnctl_exec('get', 'region')}")
            print(f"Protocol: {expressvpnctl_exec('get', 'protocol')}")
            print(f"VPN IP: {expressvpnctl_exec('get', 'vpnip')}")
        else:
            print("ExpressVPN CLI not installed at default path.")

        print("\n=== CURRENT PUBLIC IP ===")
        try:
            ip_data = get_public_ip()
            print(f"IP      : {ip_data.get('query')}")
            print(f"Location: {ip_data.get('city')}, {ip_data.get('regionName')}, {ip_data.get('country')}")
            print(f"ISP     : {ip_data.get('isp')} ({ip_data.get('org')})")
        except Exception as e:
            print(f"Failed to fetch public IP: {e}")

    elif args.command == "regions":
        regions = expressvpnctl_exec("get", "regions").splitlines()
        print(f"=== {len(regions)} AVAILABLE REGIONS ===")
        for r in regions[:40]:
            print(f"  {r}")
        if len(regions) > 40:
            print(f"  ... and {len(regions) - 40} more.")

    elif args.command == "list":
        list_profiles()

    elif args.command == "register":
        register_profile(args.id, args.kind, args.endpoint, args.region)

    elif args.command == "rotate-cli":
        print("[WARNING] Switching the host VPN adapter terminates active TCP sockets and IDE streams!")
        print(f"Connecting to: {args.region}...")
        res = expressvpnctl_exec("connect", args.region)
        print(res)


if __name__ == "__main__":
    main()
