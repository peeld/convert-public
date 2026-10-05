"""When to check, what to ask, what to tell.  The decisions live here and talk to the user
only through a `ui` object, so they are tested without Qt (tests/test_update_service.py).
convert/update_ui.py is the Qt implementation of `ui`.

ui methods (all called on the main thread):
    ask_auto()                      -> True / False / None   "check for updates automatically?"
    confirm_install(version, notes) -> bool                  manual check found one
    notify_installed(version, notes)                         automatic install finished
    notify_up_to_date()                                      manual check, nothing newer
    warn_modified(files)            -> "update" | "open" | "cancel"
    open_user_folder()
    error(text)
host (the exe, via _peelhost): api, can_verify(), verify(data, sig), source() -> (kind, version, folder),
    exe_path(), bundled_dir().
"""
import datetime
import subprocess

from . import updater


class UpdateService:
    def __init__(self, host, settings, state, ui, post, run_async, is_busy=lambda: False,
                 fetch_json=updater.fetch_json, download=updater.https_get, now=None):
        self.host, self.settings, self.state, self.ui = host, settings, state, ui
        self.post, self.run_async, self.is_busy = post, run_async, is_busy
        self.fetch_json, self.download = fetch_json, download
        self.now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.working = False

    # ---- what is running ----------------------------------------------------------------
    def enabled(self):
        """Updates need verification keys in this build, and normal (bundled/update) code running."""
        kind = self.host.source()[0]
        return self.host.can_verify() and kind in ("bundled", "update")

    def running_version(self):
        return self.host.source()[1] or "0.0.0"

    # ---- startup ------------------------------------------------------------------------
    def startup(self):
        if not self.enabled():
            return
        updater.drop_stale(self.host_bundled_version(), self.state)
        auto = self.settings.get("auto_update")
        if auto is None:
            answer = self.ui.ask_auto()
            if answer is None:
                return              # dismissed: ask again next start
            self.settings.set("auto_update", bool(answer))
            auto = answer
        if auto and updater.check_due(self.settings.get("last_check"), self.now()):
            self.run_check(manual=False)

    def host_bundled_version(self):
        import json
        from pathlib import Path
        try:
            return json.loads((Path(self.host.bundled_dir()) / "manifest.json").read_text("utf-8"))["version"]
        except (OSError, ValueError, KeyError):
            return "0.0.0"

    def set_auto(self, on):
        self.settings.set("auto_update", bool(on))

    # ---- checking -----------------------------------------------------------------------
    def run_check(self, manual):
        if self.working or not self.enabled():
            if manual and not self.enabled():
                self.ui.error("Updates are not available in this build (no update keys, or you are using a developer folder).")
            return
        self.working = True

        def work():
            try:
                found = updater.find_update(self.fetch_json, self.running_version(), self.host.api, self.state)
                err = None
            except Exception as exc:  # noqa: BLE001 - network, JSON, anything: report, never crash
                found, err = None, "%s: %s" % (type(exc).__name__, exc)
            self.post(lambda: self._checked(found, err, manual))

        self.run_async(work)

    def _checked(self, found, err, manual):
        self.working = False
        if err:
            if manual:
                self.ui.error("Could not check for updates.\n\n" + err)
            return
        self.settings.set("last_check", self.now().isoformat())
        if not found:
            if manual:
                self.ui.notify_up_to_date()
            return
        if manual:
            if not self.ui.confirm_install(found["version"], found["notes"]):
                return
        modified = updater.modified_files(self.host.bundled_dir())
        if modified:
            choice = self.ui.warn_modified(modified)
            if choice == "open":
                self.ui.open_user_folder()
                return
            if choice != "update":
                return
        elif not manual and not self.settings.get("auto_update"):
            return
        self.install(found)

    # ---- installing ---------------------------------------------------------------------
    def install(self, found):
        self.working = True

        def work():
            try:
                version = updater.install(found, self.running_version(), self.host.api, self.state,
                                          self.download, self.host.verify, self._run_check)
                err = None
            except Exception as exc:  # noqa: BLE001
                version, err = None, "%s: %s" % (type(exc).__name__, exc)
            self.post(lambda: self._installed(found, version, err))

        self.run_async(work)

    def _installed(self, found, version, err):
        self.working = False
        if err:
            self.ui.error("The update was not installed. Nothing was changed.\n\n" + err)
        else:
            self.ui.notify_installed(version, found["notes"])

    def _run_check(self, folder):
        """Start the exe on the staged folder with --python-check, like the build check."""
        try:
            done = subprocess.run([self.host.exe_path(), "--python-check", "--python-dir", str(folder)],
                                  capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0

    def can_restart_now(self):
        return not self.is_busy()
