"""Build the release zip and its file list.

    python tools/build_release.py <out-dir>

Writes into <out-dir>:
  convert-<version>.zip   the shipped files (convert/, convert_startup.py, manifest.json)
  files.json              {"version", "host_api", "files": {path: sha256}} - this is what gets signed
Signing (detached Ed25519 signature of files.json) is done on the release PC, not here.
Output is deterministic: sorted entries, fixed timestamps.

    python tools/build_release.py --listing    rewrite files.json in the repo root (the bundled copy's
                                               own file list; the app compares it with the files on disk
                                               to notice hand edits). Run before tagging a release.
    python tools/build_release.py --check      fail if that file is out of date (release.yml runs this)
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


def contents():
    """{path: bytes} of every shipped file (files.json itself is not shipped inside the zip's list)."""
    out = {}
    for f in files():
        rel = f.relative_to(ROOT).as_posix()
        data = f.read_bytes()
        out[rel] = data.replace(b"\r\n", b"\n") if f.suffix in (".py", ".json", ".md") else data
    return out


def listing_text(data):
    manifest = json.loads((ROOT / "manifest.json").read_text("utf-8"))
    hashes = {rel: hashlib.sha256(b).hexdigest() for rel, b in data.items()}
    return manifest, json.dumps({"version": manifest["version"], "host_api": manifest["host_api"],
                                 "files": hashes}, indent=2, sort_keys=True) + "\n"


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = contents()
    manifest, text = listing_text(data)
    zip_path = out / ("convert-%s.zip" % manifest["version"])
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, b in data.items():
            info = zipfile.ZipInfo(rel, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, b)
    (out / "files.json").write_text(text, "utf-8")
    print("built", zip_path, "with", len(data), "files")


if __name__ == "__main__":
    if sys.argv[1:] == ["--listing"]:
        (ROOT / "files.json").write_text(listing_text(contents())[1], "utf-8", newline="\n")
        print("wrote files.json")
    elif sys.argv[1:] == ["--check"]:
        try:
            current = (ROOT / "files.json").read_text("utf-8").replace("\r\n", "\n")
        except OSError:
            current = ""
        if current != listing_text(contents())[1]:
            sys.exit("files.json is out of date: run  python tools/build_release.py --listing  and commit it")
        print("files.json is current")
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else "dist")
