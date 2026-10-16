"""Tools: the programs conversions run (ffmpeg to start with).

`tools.json` lists each tool: where to download it and how to check the download.
A copy next to this file ships with the app; one in the user's data folder
replaces entries of the same name, so users and agents can add tools.

    {
      "tools": {
        "ffmpeg": {
          "exe": "ffmpeg",
          "download": {
            "windows-x86_64": {
              "url": "https://.../ffmpeg.zip",
              "sha256": "<64 hex digits>",      or  "sha256_url": "https://.../checksums.sha256"
              (a sha256sum-style list; the line for the zip's file name, or "sha256_name", is used)
              "path": "ffmpeg-*/bin/ffmpeg.exe"  (glob of the executable inside the zip)
            }
          }
        }
      }
    }

Finding a tool, in order: a path set in the user's settings.json
({"tools": {"ffmpeg": "C:/tools/ffmpeg.exe"}}), the app's tools folder, the PATH.
A download is refused unless it matches a SHA-256, and nothing is unpacked from
a download that failed the check.
"""
import fnmatch
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from .conversion import user_data_dir


class ToolError(Exception):
    pass


class Cancelled(Exception):
    pass


def platform_key():
    machine = platform.machine().lower()
    arch = "x86_64" if machine in ("amd64", "x86_64") else ("arm64" if machine in ("arm64", "aarch64") else machine)
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    return "%s-%s" % (system, arch)


def tools_dir():
    return user_data_dir() / "tools"


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def manifest():
    """name -> entry; the user's tools.json replaces shipped entries of the same name."""
    merged = {}
    for path in (Path(__file__).resolve().parent / "tools.json", user_data_dir() / "tools.json"):
        merged.update(_read_json(path).get("tools", {}))
    return merged


def configured_path(name):
    value = _read_json(user_data_dir() / "settings.json").get("tools", {}).get(name)
    return value if value and os.path.isfile(value) else None


def _exe_names(name, entry):
    exe = (entry or {}).get("exe", name)
    return [exe + ".exe", exe] if sys.platform == "win32" else [exe]


BUILTIN = "builtin"      # a conversion whose "tool" is code inside the app: nothing to find or install


def owner(name):
    """The tool whose download provides `name`: tools.json "bundled_with" (ffprobe is
    in ffmpeg's zip), else `name` itself."""
    return manifest().get(name, {}).get("bundled_with") or name


def find(name):
    """Full path to the tool, or None.  BUILTIN is reported as the interpreter."""
    if name == BUILTIN:
        return sys.executable or BUILTIN
    path = configured_path(name)
    if path:
        return path
    entry = manifest().get(name, {})
    parent = owner(name)
    root = tools_dir() / parent
    if root.is_dir():
        for exe in _exe_names(name, entry):
            for hit in sorted(root.rglob(exe)):
                if hit.is_file():
                    return str(hit)
    if parent != name:                      # next to the parent tool, wherever that was found
        beside = find(parent)
        if beside:
            for exe in _exe_names(name, entry):
                sibling = Path(beside).with_name(exe)
                if sibling.is_file():
                    return str(sibling)
    for exe in _exe_names(name, entry):
        hit = shutil.which(exe)
        if hit:
            return hit
    return None


def install_problems(name):
    """Why `name` cannot be downloaded, or []."""
    name = owner(name)
    entry = manifest().get(name)
    if entry is None:
        return ["no tool called %s in tools.json" % name]
    spec = entry.get("download", {}).get(platform_key())
    if not spec:
        return ["tools.json has no download for %s on %s" % (name, platform_key())]
    if not spec.get("url"):
        return ["tools.json has no url for %s on %s" % (name, platform_key())]
    if not (spec.get("sha256") or spec.get("sha256_url")):
        return ["tools.json has no sha256 for %s: downloads are not trusted without one" % name]
    return []


def _fetch(url, dest, progress, cancel):
    request = urllib.request.Request(url, headers={"User-Agent": "PeelConvert"})
    with urllib.request.urlopen(request, timeout=30) as response, open(dest, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        last = 0.0
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            now = time.monotonic()
            if progress and now - last >= 0.25:         # a few times a second, not per chunk
                last = now
                if total:
                    progress("downloaded %d%% (%d of %d MB)" % (done * 100 // total, done >> 20, total >> 20))
                else:
                    progress("downloaded %d MB" % (done >> 20))


def _sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_checksums(text, filename=None):
    """The hash for `filename` from a sha256sum-style listing ("<hash>  <name>" per line,
    name optionally prefixed with * or ./).  A single bare hash is accepted when no
    filename is given."""
    lines = [line.split(None, 1) for line in text.splitlines() if line.strip()]
    if filename is None:
        return lines[0][0].lower() if lines else ""
    for parts in lines:
        if len(parts) == 2 and parts[1].strip().lstrip("*").lstrip("./") == filename:
            return parts[0].lower()
    return ""


def _expected_hash(spec):
    if spec.get("sha256"):
        return spec["sha256"].strip().lower()
    filename = spec.get("sha256_name") or spec["url"].rsplit("/", 1)[-1]
    request = urllib.request.Request(spec["sha256_url"], headers={"User-Agent": "PeelConvert"})
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read(1024 * 1024).decode("utf-8", "replace")
    found = parse_checksums(text, filename)
    if not found:
        raise ToolError("%s lists no checksum for %s" % (spec["sha256_url"], filename))
    return found


def _safe_members(archive, target):
    root = os.path.realpath(target)
    for member in archive.namelist():
        dest = os.path.realpath(os.path.join(target, member))
        if dest != root and not dest.startswith(root + os.sep):
            raise ToolError("the download contains an unsafe path: %s" % member)
    return archive.namelist()


def install(name, progress=None, cancel=None):
    """Download, check and unpack `name` (or the tool it is bundled with) into the tools
    folder.  Returns the exe path."""
    name = owner(name)
    problems = install_problems(name)
    if problems:
        raise ToolError(problems[0])
    entry = manifest()[name]
    spec = entry["download"][platform_key()]
    final = tools_dir() / name
    final.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="install-", dir=final.parent))
    try:
        archive_path = work / "download.zip"
        if progress:
            progress("downloading " + spec["url"])
        _fetch(spec["url"], archive_path, progress, cancel)
        expected = _expected_hash(spec)
        actual = _sha256_of(archive_path)
        if actual != expected:
            raise ToolError("SHA-256 mismatch: expected %s, got %s; the download was thrown away" % (expected, actual))
        if progress:
            progress("checksum OK, unpacking")
        unpacked = work / "unpacked"
        with zipfile.ZipFile(archive_path) as archive:
            _safe_members(archive, unpacked)
            archive.extractall(unpacked)
        pattern = spec.get("path") or ("*/" + _exe_names(name, entry)[0])
        hits = [p for p in unpacked.rglob("*")
                if p.is_file() and fnmatch.fnmatch(p.relative_to(unpacked).as_posix(), pattern)]
        if not hits:
            raise ToolError("%s was not found in the download (looked for %s)" % (name, pattern))
        if final.exists():
            shutil.rmtree(final)
        shutil.move(str(unpacked), str(final))
        exe = final / hits[0].relative_to(unpacked)
        if sys.platform != "win32":
            exe.chmod(exe.stat().st_mode | 0o111)
        return str(exe)
    finally:
        shutil.rmtree(work, ignore_errors=True)
