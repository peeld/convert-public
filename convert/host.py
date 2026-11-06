"""What the app running this code tells it: its name, its user data folder, the host API.

convert-public runs inside several apps and never assumes which one.  Everything it
needs to know about the app comes from here, and this module asks the app's
`_peelhost` module (built into the exe, host API 2 and later).

Outside an app (tests, `python -m convert.check`) there is no `_peelhost`: set the
app with `configure(app_name=...)` or the PEEL_APP_NAME environment variable.
"""
import os
from pathlib import Path

_app_name = None


class HostError(RuntimeError):
    """No app to ask: not running inside one, and nothing configured."""


def _peelhost():
    try:
        import _peelhost
    except ImportError:
        return None
    return _peelhost


def api():
    """The host API level of the running app, 0 outside an app."""
    host = _peelhost()
    return host.host_api() if host is not None else 0


def configure(app_name=None):
    """For code running outside an app: which app's folders to use.  None clears it."""
    global _app_name
    _app_name = app_name or None


def app_name():
    """The app's name, e.g. for its user data folder and for a User-Agent."""
    if _app_name:
        return _app_name
    host = _peelhost()
    if host is not None and hasattr(host, "app_name"):
        return host.app_name()
    name = os.environ.get("PEEL_APP_NAME", "").strip()
    if name:
        return name
    raise HostError("not running inside a Peel app: set PEEL_APP_NAME or call convert.host.configure()")


def user_data_dir():
    """The app's user data folder: settings.json, tools, userPython, downloaded updates."""
    host = _peelhost()
    if not _app_name and host is not None and hasattr(host, "user_data_dir"):
        return Path(host.user_data_dir())
    # The same rule as the host (core's pythonSource.cpp userDataDir()).
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / app_name()
