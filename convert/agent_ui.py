"""Qt side of "let an AI write conversions": the Connect window, the menu and button, and the
watcher that picks up files an agent saved.  The logic is in agent_service.py, mcp_tools.py and
mcp_server.py; this file only shows things.

Everything here is Python, so it ships as a normal Python update (no new installer).
"""
import threading

from PySide6.QtCore import QObject, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout)

from . import mcp_tools
from .cloud_client import CloudClient, CloudError
from .agent_service import AgentService, client_setups
from .conversion import user_dir
from .mcp_server import starter_prompt

_INTRO = ("An AI assistant can build new conversions for you. Connect it once, then just tell it what "
          "you want (for example \"turn my phone videos into small GIFs\"). It writes the conversion, "
          "tests it, and adds it to the list in this app.\n"
          "Conversions are saved on this computer only. The AI can only use tools PeelConvert already "
          "has, and it cannot change your files. (What you type to the AI app goes to that AI's company, "
          "as usual.)")


def _heading(text):
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    font.setPointSize(font.pointSize() + 1)
    label.setFont(font)
    return label


def _note(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


def _line():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


def _copy(text, button, done="Copied"):
    QGuiApplication.clipboard().setText(text)
    old = button.text()
    button.setText(done)
    QTimer.singleShot(1500, lambda: button.setText(old))


class _CloudBridge(QObject):
    done = Signal(object)          # ("ok", reply) or ("error", CloudError)


class ConnectDialog(QDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Connect an AI agent")
        self.setMinimumWidth(640)
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.addWidget(_note(_INTRO))
        root.addWidget(_line())

        # 1. switch on
        root.addWidget(_heading("1. Turn on the connection"))
        row = QHBoxLayout()
        self.status = QLabel()
        self.toggle = QPushButton()
        self.toggle.clicked.connect(self._toggle)
        row.addWidget(self.status, 1)
        row.addWidget(self.toggle)
        root.addLayout(row)

        # 2. connect the AI app
        root.addWidget(_heading("2. Connect your AI app"))
        root.addWidget(_note("Pick the app you use. Then copy the text below into it, once."))
        self.client = QComboBox()
        self.setups = []
        self.client.currentIndexChanged.connect(self._show_setup)
        root.addWidget(self.client)
        self.where = _note("")
        root.addWidget(self.where)
        self.snippet = QPlainTextEdit()
        self.snippet.setReadOnly(True)
        self.snippet.setLineWrapMode(QPlainTextEdit.NoWrap)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        self.snippet.setFont(mono)
        self.snippet.setFixedHeight(110)
        root.addWidget(self.snippet)
        row = QHBoxLayout()
        self.copy_setup = QPushButton("Copy")
        self.copy_setup.clicked.connect(lambda: _copy(self.snippet.toPlainText(), self.copy_setup))
        row.addStretch(1)
        row.addWidget(self.copy_setup)
        root.addLayout(row)
        root.addWidget(_note("The text contains a private link that only works on this computer. "
                             "Do not share it."))

        # 3. ask
        root.addWidget(_heading("3. Ask for a conversion"))
        root.addWidget(_note("Describe what you want, copy the request, and paste it into your AI app."))
        row = QHBoxLayout()
        self.what = QLineEdit()
        self.what.setPlaceholderText("e.g. turn any video into a small animated GIF")
        self.copy_ask = QPushButton("Copy request")
        self.copy_ask.clicked.connect(lambda: _copy(starter_prompt(self.what.text()), self.copy_ask))
        row.addWidget(self.what, 1)
        row.addWidget(self.copy_ask)
        root.addLayout(row)

        # cloud account
        root.addWidget(_line())
        root.addWidget(_heading("Cloud account"))
        row = QHBoxLayout()
        self.cloud_state = QLabel("Not checked yet")
        self.cloud_state.setWordWrap(True)
        self.cloud_button = QPushButton("Check sign-in")
        self.cloud_button.setToolTip("Signs in with this computer's PeelConvert license")
        self.cloud_button.clicked.connect(self._check_cloud)
        row.addWidget(self.cloud_state, 1)
        row.addWidget(self.cloud_button)
        root.addLayout(row)
        self.cloud = CloudClient()
        self._cloud_bridge = _CloudBridge()
        self._cloud_bridge.done.connect(self._cloud_done)

        # 4. what it made
        root.addWidget(_line())
        root.addWidget(_heading("Conversions your AI made"))
        self.made = QListWidget()
        self.made.setFixedHeight(90)
        root.addWidget(self.made)
        row = QHBoxLayout()
        delete = QPushButton("Delete selected")
        delete.clicked.connect(self._delete)
        folder = QPushButton("Open folder")
        folder.clicked.connect(open_folder)
        reset = QPushButton("Reset private link")
        reset.setToolTip("Makes a new private link. AI apps connected with the old one stop working.")
        reset.clicked.connect(self._reset)
        row.addWidget(delete)
        row.addWidget(folder)
        row.addStretch(1)
        row.addWidget(reset)
        root.addLayout(row)
        self.path_note = _note("Saved on this computer only: %s" % user_dir())
        root.addWidget(self.path_note)
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        row.addWidget(close)
        root.addLayout(row)

        self._shown = None
        self._tick = QTimer(self)
        self._tick.setInterval(2000)
        self._tick.timeout.connect(self.refresh_list)
        self.refresh()

    # ---- showing state ----
    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self._tick.start()

    def hideEvent(self, event):
        self._tick.stop()
        super().hideEvent(event)

    def refresh(self):
        on = self.service.running
        if on:
            self.status.setText("<b style='color:#4caf50'>●</b>  On: AI apps on this computer can connect")
            self.toggle.setText("Turn off")
        elif self.service.last_error:
            self.status.setText("<b style='color:#e57373'>●</b>  Could not turn on: %s" % self.service.last_error)
            self.toggle.setText("Try again")
        else:
            self.status.setText("<b style='color:#9e9e9e'>●</b>  Off")
            self.toggle.setText("Turn on")
        keep = self.client.currentIndex()
        self.client.blockSignals(True)
        self.client.clear()
        self.setups = client_setups(self.service.url())
        for name, _, _ in self.setups:
            self.client.addItem(name)
        self.client.setCurrentIndex(max(0, keep))
        self.client.blockSignals(False)
        self._show_setup()
        self.refresh_list()

    def _show_setup(self):
        on = self.service.running
        for widget in (self.client, self.snippet, self.copy_setup, self.what, self.copy_ask):
            widget.setEnabled(on)
        if not on:
            self.where.setText("Turn the connection on in step 1 first.")
            self.snippet.setPlainText("")
            return
        _, where, text = self.setups[max(0, self.client.currentIndex())]
        self.where.setText(where)
        self.snippet.setPlainText(text)

    def refresh_list(self):
        items = mcp_tools.ai_conversions()
        shown = [(c["id"], c["label"]) for c in items]
        if shown == self._shown:
            return
        self._shown = shown
        self.made.clear()
        if not items:
            self.made.addItem("(nothing yet: ask your AI for a conversion)")
            self.made.item(0).setFlags(Qt.NoItemFlags)
        for conv in items:
            self.made.addItem("%s   [%s]" % (conv["label"], conv["group"]))
            self.made.item(self.made.count() - 1).setData(Qt.UserRole, conv["id"])

    # ---- cloud ----
    def _check_cloud(self):
        self.cloud_button.setEnabled(False)
        self.cloud_state.setText("Checking...")

        def work():
            try:
                self._cloud_bridge.done.emit(("ok", self.cloud.sign_in()))
            except CloudError as exc:
                self._cloud_bridge.done.emit(("error", exc))
            except Exception as exc:      # noqa: BLE001
                self._cloud_bridge.done.emit(("error", CloudError("internal", "Something went wrong: %s" % exc)))
        threading.Thread(target=work, daemon=True).start()

    def _cloud_done(self, result):
        self.cloud_button.setEnabled(True)
        kind, value = result
        if kind == "ok":
            plan = value.get("plan") or {}
            features = ", ".join(plan.get("features") or []) or "none"
            self.cloud_state.setText("<b style='color:#4caf50'>●</b>  Signed in (license %s). Includes: %s"
                                     % (plan.get("license", "?"), features))
        else:
            self.cloud_state.setText("<b style='color:#e57373'>●</b>  %s" % value)

    # ---- actions ----
    def _toggle(self):
        self.service.set_enabled(not self.service.running)
        self.refresh()

    def _reset(self):
        if QMessageBox.question(self, "Reset private link",
                                "AI apps you connected before will stop working until you paste the new "
                                "text into them. Continue?") == QMessageBox.Yes:
            self.service.reset_link()
            self.refresh()

    def _delete(self):
        item = self.made.currentItem()
        conv_id = item.data(Qt.UserRole) if item else None
        if not conv_id:
            return
        if QMessageBox.question(self, "Delete conversion", "Delete \"%s\"?" % item.text().split("   [")[0]) \
                == QMessageBox.Yes:
            try:
                mcp_tools.delete_conversion({"id": conv_id})
            except mcp_tools.ToolError as exc:
                QMessageBox.warning(self, "Delete conversion", str(exc))
            self.refresh_list()


def open_folder():
    folder = user_dir()
    folder.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


def _folder_signature():
    folder = user_dir()
    if not folder.is_dir():
        return ()
    out = []
    for path in sorted(folder.iterdir()):
        if path.suffix in (".json", ".py"):
            try:
                stat = path.stat()
            except OSError:
                continue
            out.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(out)


class AgentUi:
    """Owns the service, the dialog, the AI menu, the panel button and the folder watcher."""

    def __init__(self, window, controller):
        self.window = window
        self.controller = controller
        self.service = AgentService()
        self.dialog = None
        self.describe = None
        try:
            user_dir().mkdir(parents=True, exist_ok=True)     # the home of AI-made conversions
        except OSError:
            pass
        if self.service.enabled:
            self.service.start()
        QApplication.instance().aboutToQuit.connect(self.service.stop)

        menu = window.menuBar().addMenu("&AI")
        menu.addAction("Describe a conversion...").triggered.connect(self.show_describe)
        menu.addAction("Connect an AI agent...").triggered.connect(self.show_dialog)
        menu.addAction("Open the AI conversions folder").triggered.connect(open_folder)
        self._add_panel_button()

        self._signature = _folder_signature()
        self._known = {c["id"] for c in mcp_tools.ai_conversions()}
        self._timer = QTimer(window)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    def show_dialog(self):
        if self.dialog is None:
            self.dialog = ConnectDialog(self.service, self.window)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def show_describe(self):
        from .describe_ui import DescribeDialog
        if self.describe is None:
            self.describe = DescribeDialog(self._added, self.window)
        self.describe.show()
        self.describe.raise_()
        self.describe.activateWindow()

    def _added(self, definition):
        """The person pressed Add in the describe window: show the new conversion."""
        self._signature = _folder_signature()
        self._known = {c["id"] for c in mcp_tools.ai_conversions()}
        self.controller.reload()
        self.window.setMessage("Added: %s" % definition.get("label", definition["id"]))
        try:
            for combo in self.controller.panel.findChildren(QComboBox):
                index = next((i for i in range(combo.count())
                              if combo.itemData(i) == definition["id"] or combo.itemText(i) == definition.get("label")), -1)
                if index >= 0:
                    combo.setCurrentIndex(index)
                    break
        except Exception:     # noqa: BLE001 - selecting it is a nicety
            pass

    def _add_panel_button(self):
        """Put a 'Connect AI...' button next to Reload.  The menu works even if this cannot."""
        try:
            panel = self.controller.panel
            reload_button = next(b for b in panel.findChildren(QPushButton) if b.text() == "Reload")
            describe = QPushButton("Describe a conversion...", panel)
            describe.setToolTip("Type what you want and let AI build the conversion")
            describe.clicked.connect(self.show_describe)
            button = QPushButton("Connect AI...", panel)
            button.setToolTip("Let an AI assistant write new conversions for you")
            button.clicked.connect(self.show_dialog)
            outer = panel.layout()
            for i in range(outer.count()):
                row = outer.itemAt(i).layout()
                if row is not None and row.indexOf(reload_button) >= 0:
                    at = row.indexOf(reload_button) + 1
                    row.insertWidget(at, describe)
                    row.insertWidget(at + 1, button)
                    return
            button.deleteLater()
            describe.deleteLater()
        except Exception:     # noqa: BLE001 - cosmetic; the AI menu is the fallback
            pass

    def _poll(self):
        """A file appeared or changed in the conversions folder (an agent saved one): reload."""
        signature = _folder_signature()
        if signature == self._signature:
            return
        self._signature = signature
        self.controller.reload()
        now = {c["id"]: c["label"] for c in mcp_tools.ai_conversions()}
        added = [label for cid, label in now.items() if cid not in self._known]
        self._known = set(now)
        if added:
            self.window.setMessage("New conversion from your AI: %s" % ", ".join(added))
            if self.dialog is not None:
                self.dialog.refresh_list()


def install_agent_ui(window, controller):
    """Called from convert_startup.install().  Never raises: the AI feature is optional."""
    try:
        return AgentUi(window, controller)
    except Exception as exc:      # noqa: BLE001
        import sys
        print("AI connection unavailable: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return None
