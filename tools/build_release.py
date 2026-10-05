"""Build the release zip and its file list.

    python tools/build_release.py <out-dir>

Writes into <out-dir>:
  convert-<version>.zip   the shipped files (convert/, convert_startup.py, manifest.json)
  files.json              {"version", "host_api", "files": {path: sha256}} - this is what gets signed
Signing (detached Ed25519 signature of files.json) is done on the release PC, not here.
Output is deterministic: sorted entries, fixed timestamps.
"""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIP = ["convert", "convert_startup.py", "manifest.json"]
SKIP_PARTS = {"__pycache__"}


def files():
    out = []
    for item in SHIP:
        p = ROOT / item
        found = [p] if p.is_file() else sorted(p.rglob("*"))
        for f in found:
            if f.is_file() and not (SKIP_PARTS & set(f.parts)) and f.suffix != ".pyc":
                out.append(f)
    return sorted(out, key=lambda f: f.relative_to(ROOT).as_posix())


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT / "manifest.json").read_text("utf-8"))
    hashes = {}
    zip_path = out / ("convert-%s.zip" % manifest["version"])
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files():
            rel = f.relative_to(ROOT).as_posix()
            data = f.read_bytes().replace(b"\r\n", b"\n") if f.suffix in (".py", ".json", ".md") else f.read_bytes()
            hashes[rel] = hashlib.sha256(data).hexdigest()
            info = zipfile.ZipInfo(rel, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    listing = {"version": manifest["version"], "host_api": manifest["host_api"], "files": hashes}
    (out / "files.json").write_text(json.dumps(listing, indent=2, sort_keys=True) + "\n", "utf-8")
    print("built", zip_path, "with", len(hashes), "files")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dist")
