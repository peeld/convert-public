"""Qt side of "Describe a conversion": a window where the person types what they want, watches
progress, reviews the result and chooses Add or Discard.  The logic is in agent_loop.py (no Qt).

The agent runs on a worker thread; its events come back to the UI thread through a Signal.
"""
import threading
import webbrowser

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QStackedLayout, QVBoxLayout, QWidget)

from . import describe_settings, mcp_tools
from .agent_loop import Session
from .agent_model import AnthropicModel

KEY_URL = "https://console.anthropic.com/settings/keys"

_INTRO = ("Tell PeelConvert what you want to turn into what, in your own words. It will build the "
          "conversion, try it on a small test file, and show it to you before adding it.")
_PRIVACY = ("Your description, and the conversions you already have, are sent to the AI service you "
            "chose, using your own key. Your files are never sent. Nothing is added until you say so.")


class Bridge(QObject):
    event = Signal(object)


def _note(text):
    label = QLabel(text)
    label.setWordWrap(True)
    return label


class DescribeDialog(QDialog):
    def __init__(self, on_added, parent=None, model_factory=None):
        super().__init__(parent)
        self.setWindowTitle("Describe a conversion")
        self.setMinimumWidth(560)
        self.on_added = on_added
        self.model_factory = model_factory or (lambda key, name: AnthropicModel(key, name))
        self.session = None
        self.thread = None
        self.proposal = None
        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)

        root = QVBoxLayout(self)
        root.setSpacing(8)
        self.pages = QStackedLayout()
        root.addLayout(self.pages)
        self.pages.addWidget(self._build_setup())
        self.pages.addWidget(self._build_main())
        self._show_page()

    # ---- setup page (key) ----
    def _build_setup(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.addWidget(_note("To describe a conversion in your own words, PeelConvert needs an API key from "
                            "an AI service (Anthropic). You pay that service directly for what you use; a "
                            "conversion usually costs a few cents."))
        link = QPushButton("Get a key (opens your browser)")
        link.clicked.connect(lambda: webbrowser.open(KEY_URL))
        box.addWidget(link)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("Paste your key here")
        box.addWidget(self.key_edit)
        row = QHBoxLayout()
        row.addWidget(QLabel("Model"))
        self.model_edit = QLineEdit(describe_settings.load().get("model", describe_settings.DEFAULT_MODEL))
        self.model_edit.setToolTip("Leave this alone unless the AI service tells you the model name is wrong.")
        row.addWidget(self.model_edit, 1)
        box.addLayout(row)
        box.addWidget(_note(_PRIVACY))
        self.key_state = _note("")
        box.addWidget(self.key_state)
        row = QHBoxLayout()
        row.addStretch(1)
        self.remove_key = QPushButton("Remove saved key")
        self.remove_key.clicked.connect(self._remove_key)
        self.save_key = QPushButton("Save")
        self.save_key.setDefault(True)
        self.save_key.clicked.connect(self._save_key)
        row.addWidget(self.remove_key)
        row.addWidget(self.save_key)
        box.addLayout(row)
        return page

    def _save_key(self):
        key = self.key_edit.text().strip()
        model = self.model_edit.text().strip() or describe_settings.DEFAULT_MODEL
        if key:
            describe_settings.set_key(key)
        describe_settings.save(model)
        self.key_edit.clear()
        self.session = None
        self._show_page()

    def _remove_key(self):
        describe_settings.clear_key()
        self.session = None
        self._show_page()

    # ---- main page ----
    def _build_main(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.addWidget(_note(_INTRO))
        self.ask = QPlainTextEdit()
        self.ask.setPlaceholderText("For example: turn any video into a small animated GIF")
        self.ask.setFixedHeight(80)
        box.addWidget(self.ask)
        row = QHBoxLayout()
        self.settings_button = QPushButton("Key and model...")
        self.settings_button.clicked.connect(lambda: self.pages.setCurrentIndex(0))
        self.go = QPushButton("Make it")
        self.go.setDefault(True)
        self.go.clicked.connect(self._go)
        self.cancel_button = QPushButton("Stop")
        self.cancel_button.clicked.connect(self._stop)
        row.addWidget(self.settings_button)
        row.addStretch(1)
        row.addWidget(self.cancel_button)
        row.addWidget(self.go)
        box.addLayout(row)

        self.progress = QLabel("")
        box.addWidget(self.progress)
        self.said = QPlainTextEdit()
        self.said.setReadOnly(True)
        self.said.setFixedHeight(90)
        self.said.hide()
        box.addWidget(self.said)

        self.card = QFrame()
        self.card.setFrameShape(QFrame.StyledPanel)
        card = QVBoxLayout(self.card)
        self.card_title = QLabel()
        font = self.card_title.font()
        font.setBold(True)
        self.card_title.setFont(font)
        self.card_body = _note("")
        self.card_body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        card.addWidget(self.card_title)
        card.addWidget(self.card_body)
        row = QHBoxLayout()
        row.addStretch(1)
        self.discard = QPushButton("Discard")
        self.discard.clicked.connect(self._discard)
        self.add = QPushButton("Add to my conversions")
        self.add.clicked.connect(self._add)
        row.addWidget(self.discard)
        row.addWidget(self.add)
        card.addLayout(row)
        self.card.hide()
        box.addWidget(self.card)
        return page

    def _show_page(self):
        has_key = bool(describe_settings.get_key())
        self.pages.setCurrentIndex(1 if has_key else 0)
        self.remove_key.setEnabled(has_key)
        self.key_state.setText("A key is saved on this computer (%s)." % describe_settings.mask(describe_settings.get_key())
                               if has_key else "")
        self._busy(False)

    def _busy(self, busy):
        self.go.setEnabled(not busy)
        self.ask.setEnabled(not busy)
        self.cancel_button.setVisible(busy)

    # ---- running ----
    def _go(self):
        text = self.ask.toPlainText().strip()
        if not text:
            return
        if self.session is None:
            key = describe_settings.get_key()
            if not key:
                self._show_page()
                return
            model = self.model_factory(key, describe_settings.load().get("model") or describe_settings.DEFAULT_MODEL)
            self.session = Session(model, self.bridge.event.emit,
                                   usage_log=str(describe_settings.user_data_dir() / "describe-usage.jsonl"))
        self.card.hide()
        self.proposal = None
        self.said.clear()
        self.said.hide()
        self.progress.setText("Starting...")
        self._busy(True)
        self.thread = threading.Thread(target=self.session.send, args=(text,), daemon=True)
        self.thread.start()

    def _stop(self):
        if self.session:
            self.session.cancel()
            self.progress.setText("Stopping...")

    def _on_event(self, event):
        kind = event.get("type")
        if kind == "status":
            self.progress.setText(event["text"])
        elif kind in ("message", "question"):
            if kind == "message":
                self.said.appendPlainText(event["text"])
                self.said.show()
        elif kind == "proposal":
            self._show_card(event)
        elif kind == "error":
            self.progress.setText("")
            self.said.appendPlainText(event["text"])
            self.said.show()
            if event.get("kind") == "auth":
                self.pages.setCurrentIndex(0)
        elif kind == "done":
            if not self.proposal:
                self.progress.setText("")
            else:
                self.progress.setText("Ready: check it below.")
            self._busy(False)

    def _show_card(self, event):
        self.proposal = event
        d, t = event["definition"], event.get("test") or {}
        self.card_title.setText(d.get("label", d["id"]) + ("   (replaces your earlier version)" if event.get("replaces") else ""))
        types = ", ".join(d.get("inputs") or []) or "any file the tool can read"
        lines = ["Group: %s" % d.get("group", ""), "Takes: %s" % types, "Makes: .%s" % d.get("output_ext", "?")]
        if t.get("output_name"):
            lines.append("Tried on a sample: made %s (%s bytes) in %s s" % (
                t["output_name"], t.get("output_bytes", "?"), t.get("seconds", "?")))
        for warning in event.get("warnings") or []:
            lines.append("Note: %s" % warning)
        self.card_body.setText("\n".join(lines))
        self.card.show()

    def _discard(self):
        self.card.hide()
        self.proposal = None
        self.progress.setText("Discarded. Describe a change, or something new.")

    def _add(self):
        if not self.proposal:
            return
        definition = self.proposal["definition"]
        try:
            mcp_tools.save_conversion({"definition": definition, "overwrite": bool(self.proposal.get("replaces"))})
        except mcp_tools.ToolError as exc:
            QMessageBox.warning(self, "Add conversion", str(exc))
            return
        self.card.hide()
        self.proposal = None
        self.progress.setText("Added: %s" % definition.get("label", definition["id"]))
        self.on_added(definition)

    def closeEvent(self, event):
        self._stop()
        super().closeEvent(event)
