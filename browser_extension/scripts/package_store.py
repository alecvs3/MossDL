"""Create deterministic, allowlisted store ZIPs and a reproducible reviewer source ZIP."""
from pathlib import Path
import hashlib
import json
import shutil
import struct
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "release"
OUT.mkdir(exist_ok=True)
version = json.loads((ROOT / "build.config.json").read_text())["extension_version"]

def archive(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for source, name in sorted(files, key=lambda pair: pair[1]):
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, source.read_bytes())

for browser in ("chrome", "firefox"):
    folder = ROOT / "build" / browser
    allowed = ["manifest.json", "background.js", "content.js", "handoff.js", "popup.js", "popup.html", "popup.css"]
    allowed += [f"icons/icon-{size}.png" for size in (16, 32, 48, 128)]
    for size in (16, 32, 48, 128):
        raw = (folder / f"icons/icon-{size}.png").read_bytes()
        if struct.unpack(">II", raw[16:24]) != (size, size):
            raise ValueError("Incorrect icon dimensions")
    manifest = json.loads((folder / "manifest.json").read_text())
    if manifest["version"] != version:
        raise ValueError("Build version is stale; run npm run build")
    archive(OUT / f"mossdl-{browser}-{version}.zip", [(folder / name, name) for name in allowed])

files = [(ROOT / name, name) for name in ("package.json", "package-lock.json", "tsconfig.json", "playwright.config.ts", "build.config.json", "manifest.chrome.json", "manifest.firefox.json", "README.md", "PRIVACY.md")]
for directory in ("src", "scripts", "assets", "tests", "store"):
    files += [(path, path.relative_to(ROOT).as_posix()) for path in (ROOT / directory).rglob("*") if path.is_file() and "__pycache__" not in path.parts]
archive(OUT / f"mossdl-reviewer-source-{version}.zip", files)
for source in (ROOT / "store").glob("*.md"):
    shutil.copyfile(source, OUT / source.name)
shutil.copyfile(ROOT / "PRIVACY.md", OUT / "PRIVACY.md")
(OUT / "listing").mkdir(exist_ok=True)
shutil.copyfile(ROOT / "assets/icons/icon-128.png", OUT / "listing/icon-128.png")
archives = sorted(OUT.glob("*.zip"))
(OUT / "SHA256SUMS.txt").write_text("\n".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in archives) + "\n")
print(json.dumps({"archives": [str(path) for path in archives]}, indent=2))
