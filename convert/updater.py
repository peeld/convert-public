"""Self-update of the Python side, from signed GitHub releases.

Pure logic: every outside effect is passed in, so it is tested without network, Qt
or the exe.  The host (C++ / convert_startup) supplies:

    fetch_json(url) -> dict          GET and parse JSON (the GitHub releases API)
    download(url) -> bytes           GET a release asset
    verify(data, signature) -> bool  Ed25519 check against the two public keys compiled into the exe
    run_check(folder) -> bool        load every conversion from `folder` (like --python-check)

Layout under the update root (%APPDATA%/PeelConvert/python):

    versions/<version>/...           unpacked releases (convert/, convert_startup.py, manifest.json)
    state.json                       see State below; C++ reads this to pick the folder at startup

State (state.json):
    {"current": "0.2.0",   version to load, or null for the bundled copy
     "previous": "0.1.5",  version to fall back to (kept for one release), or null
     "pending": true,      current has been installed but has not yet reached "good"
     "booted": false,      pending, and the app has already started once with it
     "bad": ["0.1.9"]}     versions that failed; never offered again

Startup protocol (src/pythonSource.cpp does the same in C++): if `pending` and `booted`, the
previous start never reached good -> add `current` to `bad`, current = previous, pending and
booted = false.  If `pending` and not `booted`, set `booted` and load `current`.  Once Python
has started cleanly, mark_good() clears both.  The loader never installs; only install() does.
"""
import hashlib
import io
import json
import re
import shutil
import zipfile
from pathlib import Path

RELEASES_URL = "https://api.github.com/repos/peeld/convert-public/releases/latest"
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class UpdateError(Exception):
    """The update was refused; nothing was changed."""


def parse_version(text):
    m = _VERSION.match(str(text).strip())
    if not m:
        raise UpdateError("bad version: %r" % (text,))
    return tuple(int(x) for x in m.groups())


def compatible(manifest, host_api):
    api = manifest.get("host_api", {})
    return isinstance(api.get("min"), int) and isinstance(api.get("max"), int) and api["min"] <= host_api <= api["max"]


# ---- state -------------------------------------------------------------------------
class State:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "state.json"
        self.data = {"current": None, "previous": None, "pending": False, "booted": False, "bad": []}
        try:
            loaded = json.loads(self.path.read_text("utf-8"))
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, ValueError):
            pass

    def save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), "utf-8")
        tmp.replace(self.path)

    def __getitem__(self, key):
        return self.data[key]

    def version_dir(self, version):
        return self.root / "versions" / version

    def is_bad(self, version):
        return version in self.data["bad"]


def boot(state):
    """What the loader does at startup.  Returns the version to load (None = bundled)."""
    d = state.data
    if d["pending"] and d["booted"]:
        if d["current"] and d["current"] not in d["bad"]:
            d["bad"].append(d["current"])
        d["current"], d["previous"], d["pending"], d["booted"] = d["previous"], None, False, False
        state.save()
    elif d["pending"]:
        d["booted"] = True
        state.save()
    return d["current"]


def mark_good(state):
    state.data["pending"] = False
    state.data["booted"] = False
    state.save()


def newest_allowed(bundled_version, state):
    """Which version to run: the downloaded one only if it is newer than the bundled copy."""
    cur = state.data["current"]
    if cur and parse_version(cur) > parse_version(bundled_version):
        return cur
    return None


# ---- checking ----------------------------------------------------------------------
def find_update(fetch_json, running_version, host_api, state):
    """The newest release that is newer than what runs, not known-bad and compatible, or None.
    Returns {"version", "notes", "zip_url", "list_url", "sig_url"}."""
    rel = fetch_json(RELEASES_URL)
    if rel.get("draft") or rel.get("prerelease"):
        return None
    version = str(rel.get("tag_name", "")).lstrip("v")
    if parse_version(version) <= parse_version(running_version) or state.is_bad(version):
        return None
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    names = ("convert-%s.zip" % version, "files.json", "files.json.sig")
    if any(n not in assets for n in names):
        return None
    return {"version": version, "notes": rel.get("body") or "", "zip_url": assets[names[0]],
            "list_url": assets[names[1]], "sig_url": assets[names[2]]}


# ---- installing --------------------------------------------------------------------
def install(update, running_version, host_api, state, download, verify, run_check):
    """Download, verify and stage `update`.  Raises UpdateError and changes nothing on failure.
    On success the version is in versions/, current/previous/pending are set, and the new
    code is used from the next start."""
    version = update["version"]
    if parse_version(version) <= parse_version(running_version):
        raise UpdateError("not newer than the running version")
    if state.is_bad(version):
        raise UpdateError("version %s failed before" % version)

    listing_bytes = download(update["list_url"])
    if not verify(listing_bytes, download(update["sig_url"])):
        raise UpdateError("signature check failed")
    listing = json.loads(listing_bytes.decode("utf-8"))
    if listing.get("version") != version:
        raise UpdateError("file list is for another version")
    if not compatible({"host_api": listing.get("host_api", {})}, host_api):
        raise UpdateError("needs a newer PeelConvert (host API)")

    with zipfile.ZipFile(io.BytesIO(download(update["zip_url"]))) as z:
        wanted = listing["files"]
        if sorted(z.namelist()) != sorted(wanted):
            raise UpdateError("zip contents do not match the signed file list")
        staging = state.root / "staging" / version
        if staging.exists():
            shutil.rmtree(staging)
        try:
            for name, sha in wanted.items():
                if name.startswith(("/", "\\")) or ".." in Path(name).parts or ":" in name:
                    raise UpdateError("unsafe path in release: %s" % name)
                data = z.read(name)
                if hashlib.sha256(data).hexdigest() != sha:
                    raise UpdateError("hash mismatch: %s" % name)
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            manifest = json.loads((staging / "manifest.json").read_text("utf-8"))
            if manifest.get("version") != version or not compatible(manifest, host_api):
                raise UpdateError("manifest does not match the release")
            if not run_check(staging):
                raise UpdateError("the new version failed its own check")
            final = state.version_dir(version)
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                shutil.rmtree(final)
            staging.replace(final)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    d = state.data
    d["previous"], d["current"], d["pending"], d["booted"] = d["current"], version, True, False
    state.save()
    prune(state)
    return version


def prune(state):
    """Keep only current and previous; delete other downloaded versions."""
    keep = {state.data["current"], state.data["previous"]}
    vroot = state.root / "versions"
    if vroot.is_dir():
        for p in vroot.iterdir():
            if p.name not in keep:
                shutil.rmtree(p, ignore_errors=True)


def drop_stale(bundled_version, state):
    """After the app itself was upgraded: if bundled is newer than what was downloaded, forget the downloads."""
    cur = state.data["current"]
    if cur and parse_version(cur) <= parse_version(bundled_version):
        state.data.update(current=None, previous=None, pending=False, booted=False)
        state.save()
        prune(state)
