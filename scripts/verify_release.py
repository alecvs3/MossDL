"""Verify updater signatures and feed URLs before publishing release assets."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from Crypto.Signature import eddsa


def verify_signature(asset: Path, encoded: str, public: str) -> None:
    public_packet = base64.b64decode(base64.b64decode(public).decode().splitlines()[1])
    lines = base64.b64decode(encoded).decode().splitlines()
    packet = base64.b64decode(lines[1])
    if len(packet) != 74 or packet[2:10] != public_packet[2:10]:
        raise ValueError(f"Signing key mismatch: {asset.name}")
    if packet[:2] != b"ED" or not lines[2].startswith("trusted comment: "):
        raise ValueError(f"Unsupported signature format: {asset.name}")
    verifier = eddsa.new(eddsa.import_public_key(public_packet[10:]), "rfc8032")
    verifier.verify(hashlib.blake2b(asset.read_bytes(), digest_size=64).digest(), packet[10:])
    verifier.verify(packet[10:] + lines[2][17:].encode(), base64.b64decode(lines[3]))


def main() -> None:
    folder = Path(sys.argv[1])
    public = os.environ["MOSSDL_UPDATER_PUBKEY"].strip()
    feed = json.loads((folder / "latest.json").read_text(encoding="utf-8-sig"))
    platform = feed["platforms"]["windows-x86_64"]
    url = urlsplit(platform["url"])
    installer = folder / Path(url.path).name
    if url.scheme != "https" or f"/v{feed['version']}/" not in url.path:
        raise ValueError("Release feed URL does not match its version")
    if platform["signature"].strip() != Path(str(installer) + ".sig").read_text().strip():
        raise ValueError("Feed signature does not match installer signature")
    for asset in folder.iterdir():
        if asset.suffix in {".exe", ".msi"}:
            verify_signature(asset, Path(str(asset) + ".sig").read_text().strip(), public)
    files = sorted(p for p in folder.rglob("*")
                   if p.is_file() and (p.parent == folder and p.name != "SHA256SUMS.txt"
                                       or p.suffix == ".zip"))
    (folder / "SHA256SUMS.txt").write_text("".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(folder).as_posix()}\n"
        for p in files), encoding="utf-8")
    print(f"Release {feed['version']}: installer signatures and update feed verified")


if __name__ == "__main__":
    main()
