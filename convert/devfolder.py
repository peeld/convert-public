"""The developer folder: a clone of convert-public that the app loads instead of its bundled
copy, so a person (or their coding agent) can change operations and try them on real files.
Off by default.  Stored in settings.json as {"python": {"developer_folder": "C:/..."}}; the C++
loader (src/pythonSource.cpp) reads it at startup, so a change takes effect on the next start.
(Reload re-reads conversion JSON from the running folder, but Python code needs a restart.)
"""
import shutil
import subprocess
from pathlib import Path

REPO_URL = "https://github.com/peeld/convert-public.git"
KEY = "developer_folder"


def problems(folder):
    """Why `folder` cannot be used as the developer folder; empty if it can."""
    p = Path(folder)
    if not p.is_dir():
        return ["not a folder"]
    out = []
    for name in ("convert/__init__.py", "convert_startup.py", "manifest.json"):
        if not (p / name).is_file():
            out.append("missing " + name)
    return out


def current(settings):
    return (settings.get(KEY) or "").strip()


def set_folder(settings, folder):
    """Turn the developer folder on (or off with ""/None).  Raises ValueError if it is not usable."""
    if not folder:
        settings.set(KEY, "")
        return
    bad = problems(folder)
    if bad:
        raise ValueError("%s: %s" % (folder, "; ".join(bad)))
    settings.set(KEY, str(Path(folder)))


def git_available(which=shutil.which):
    return which("git") is not None


def clone(dest, run=subprocess.run):
    """git clone convert-public into `dest` (a folder that does not exist yet or is empty).
    Returns None on success, or the error text."""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        return "%s is not empty" % dest
    try:
        done = run(["git", "clone", "--depth", "1", REPO_URL, str(dest)], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    return None if done.returncode == 0 else (done.stderr or done.stdout or "git clone failed").strip()
