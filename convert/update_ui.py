"""Qt side of the updater: dialogs, the Help menu entries, the thread hand-off.
Logic is in update_service.py; this file only shows things and passes the answers back."""
import threading

from PySide6.QtCore import QObject, QProcess, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QMessageBox

from . import devfolder
from .conversion import user_python_dir


class _Poster(QObject):
    """Runs a callable on the thread this object lives in (the main thread).  The slot must be a
    method of this QObject: a lambda would run on the emitting (worker) thread."""
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run)

    @Slot(object)
    def _run(self, fn):
        fn()


class QtUpdateUi:
    def __init__(self, window, service_getter):
        self.window = window
        self.service = service_getter      # the service is created after the ui
        self.poster = _Poster()

    # threading helpers handed to the service
    def post(self, fn):
        self.poster.call.emit(fn)

    @staticmethod
    def run_async(fn):
        threading.Thread(target=fn, daemon=True, name="PeelConvert-update").start()

    # ---- ui interface ---------------------------------------------------------------
    def ask_auto(self):
        box = QMessageBox(self.window)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Updates")
        box.setText("Do you want PeelConvert to check for updates automatically?")
        box.setInformativeText("New conversions and fixes are downloaded from the project's GitHub releases "
                               "and checked against a signature before use. They take effect the next time "
                               "the app starts. You can change this under Help.")
        yes = box.addButton("Yes, check automatically", QMessageBox.YesRole)
        no = box.addButton("No, I will check myself", QMessageBox.NoRole)
        box.exec()
        if box.clickedButton() is yes:
            return True
        if box.clickedButton() is no:
            return False
        return None

    def confirm_install(self, version, notes):
        box = QMessageBox(self.window)
        box.setWindowTitle("Update available")
        box.setText("Version %s is available. Install it?" % version)
        box.setInformativeText("It is used from the next start.")
        if notes:
            box.setDetailedText(notes)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        return box.exec() == QMessageBox.Yes

    def notify_installed(self, version, notes):
        box = QMessageBox(self.window)
        box.setWindowTitle("Updated")
        box.setText("PeelConvert's conversions were updated to %s." % version)
        if notes:
            box.setDetailedText(notes)
        restart = box.addButton("Restart now", QMessageBox.AcceptRole)
        box.addButton("Later", QMessageBox.RejectRole)
        service = self.service()
        if service is not None and not service.can_restart_now():
            restart.setEnabled(False)
            box.setInformativeText("Conversions are running; restart when they finish.")
        else:
            box.setInformativeText("Restart to use it.")
        box.exec()
        if box.clickedButton() is restart and restart.isEnabled():
            self.restart()

    def notify_up_to_date(self):
        QMessageBox.information(self.window, "Updates", "You have the latest version.")

    def warn_modified(self, files):
        box = QMessageBox(self.window)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Update")
        box.setText("PeelConvert's bundled Python files were changed.")
        box.setInformativeText("An update would replace those changes. Move your work to the userPython "
                               "folder first, where updates never touch it.")
        box.setDetailedText("\n".join(files))
        open_btn = box.addButton("Open userPython folder", QMessageBox.ActionRole)
        update_btn = box.addButton("Update anyway", QMessageBox.AcceptRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            return "open"
        if box.clickedButton() is update_btn:
            return "update"
        return "cancel"

    def open_user_folder(self):
        folder = user_python_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def error(self, text):
        QMessageBox.warning(self.window, "Updates", text)

    # ---- restart --------------------------------------------------------------------
    def restart(self):
        import _peelhost
        QProcess.startDetached(_peelhost.exe_path(), QApplication.arguments()[1:])
        QTimer.singleShot(0, QApplication.quit)


def developer_folder_dialog(window, settings, restart):
    """Help > Developer folder...: point the app at a clone of convert-public, clone one, or turn it off."""
    folder = devfolder.current(settings)
    box = QMessageBox(window)
    box.setWindowTitle("Developer folder")
    box.setText("Developer folder: %s" % (folder or "off"))
    box.setInformativeText("When set, PeelConvert loads its Python code (conversions, ops) from a clone of the "
                           "convert-public repository instead of its own copy, so you or your coding agent can "
                           "change an operation and try it on real files. Python changes take effect after a "
                           "restart; conversion files are re-read by Reload. Updates are paused while it is on.")
    choose = box.addButton("Choose a clone...", QMessageBox.ActionRole)
    clone = box.addButton("Clone it for me...", QMessageBox.ActionRole)
    clone.setEnabled(devfolder.git_available())
    clone.setToolTip("" if clone.isEnabled() else "git was not found on this computer")
    off = box.addButton("Turn off", QMessageBox.DestructiveRole)
    off.setEnabled(bool(folder))
    box.addButton("Close", QMessageBox.RejectRole)
    box.exec()
    picked = box.clickedButton()
    target = None
    if picked is choose:
        target = QFileDialog.getExistingDirectory(window, "Folder containing convert/ and manifest.json", folder)
    elif picked is clone:
        parent = QFileDialog.getExistingDirectory(window, "Where to put the convert-public folder")
        if parent:
            from pathlib import Path
            target = str(Path(parent) / "convert-public")
            error = devfolder.clone(target)
            if error:
                QMessageBox.warning(window, "Developer folder", "Could not clone:\n\n" + error)
                return
    elif picked is off:
        devfolder.set_folder(settings, "")
        restart_prompt(window, "The developer folder is off.", restart)
        return
    if not target:
        return
    try:
        devfolder.set_folder(settings, target)
    except ValueError as exc:
        QMessageBox.warning(window, "Developer folder", str(exc))
        return
    restart_prompt(window, "Using %s" % target, restart)


def restart_prompt(window, text, restart):
    box = QMessageBox(window)
    box.setWindowTitle("Developer folder")
    box.setText(text)
    box.setInformativeText("Restart PeelConvert to apply it.")
    now = box.addButton("Restart now", QMessageBox.AcceptRole)
    box.addButton("Later", QMessageBox.RejectRole)
    box.exec()
    if box.clickedButton() is now:
        restart()


def install_update_service(window, controller):
    """Called from convert_startup.install(). Never raises: updates are optional."""
    try:
        import _peelhost
        from pathlib import Path
        from . import updater
        from .conversion import user_data_dir
        from .update_service import UpdateService

        class Host:
            api = _peelhost.host_api()
            can_verify = staticmethod(_peelhost.can_verify)
            verify = staticmethod(_peelhost.verify)
            source = staticmethod(_peelhost.source)
            exe_path = staticmethod(_peelhost.exe_path)
            bundled_dir = staticmethod(_peelhost.bundled_dir)

        holder = {}
        ui = QtUpdateUi(window, lambda: holder.get("service"))
        root = Path(user_data_dir()) / "python"
        service = UpdateService(Host, updater.Settings(user_data_dir() / "settings.json"), updater.State(root),
                                ui, ui.post, ui.run_async, is_busy=controller.is_busy)
        holder["service"] = service

        help_menu = window.findChild(QMenu, "helpMenu")
        if help_menu is not None:
            help_menu.addAction("Check for updates...").triggered.connect(lambda: service.run_check(manual=True))
            auto = help_menu.addAction("Update automatically")
            auto.setCheckable(True)
            auto.setChecked(bool(service.settings.get("auto_update")))
            auto.toggled.connect(service.set_auto)
            help_menu.addAction("Developer folder...").triggered.connect(
                lambda: developer_folder_dialog(window, service.settings, ui.restart))
        QTimer.singleShot(2000, service.startup)       # after the window is up
        return service
    except Exception as exc:  # noqa: BLE001
        import sys
        print("updates unavailable: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return None
