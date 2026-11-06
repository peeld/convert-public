"""The Ops an app queues: ConvertOp (one file, one conversion) and
InstallToolOp (download a tool).  ConvertOp's work is run.run_conversion(), which needs
no Op: a worker process can call it directly.  Both are asynchronous: start() returns at
once and a worker thread calls complete(), fail() or acknowledgeCancel().

Both report progress (setProgress, 0.0 to 1.0) and a status line (setStatus) for the UI.

Properties of a ConvertOp (all strings):
    conversion   id of a registered Conversion
    input        full path of the source file
    output       full path of the file to write; empty until the output directory is resolved
    conflict     set by the app when another queued file would write the same output
"""
import os
import re
import threading

from PeelCore import work

from . import paths, runner, tools
from .conversion import ConversionError, registry
from .run import Cancelled, run_conversion

CONVERT_TYPE = "convert.job"
INSTALL_TYPE = "tool.install"

_DOWNLOADED = re.compile(r"downloaded (\d+)%")


def _prop(op, name, default=""):
    return op.properties().value(name, default)


class ConvertOp(work.Op):
    def __init__(self, *args):
        super().__init__(*args)
        self._cancel = threading.Event()
        self._proc = None
        self._proc_lock = threading.Lock()

    def typeName(self):
        return CONVERT_TYPE

    # ---- checks -------------------------------------------------------------------
    def validate(self):
        conversion = registry.get(_prop(self, "conversion"))
        if conversion is None:
            return ["unknown conversion '%s'" % _prop(self, "conversion")]
        source = _prop(self, "input")
        problems = []
        if not os.path.isfile(source):
            problems.append("input file not found")
        elif not conversion.accepts(source):
            ext = os.path.splitext(source)[1] or "(no extension)"
            problems.append("'%s' does not accept %s files" % (conversion.label, ext))
        if tools.find(conversion.tool) is None:
            problems.append("%s is not installed" % conversion.tool)
        reason = conversion.unavailable()
        if reason:
            problems.append(reason)
        problems += paths.check_output(source, _prop(self, "output"))
        conflict = _prop(self, "conflict")
        if conflict:
            problems.append(conflict)
        if not _prop(self, "output"):
            problems.append("no output path yet")
        return problems

    # ---- running ------------------------------------------------------------------
    def start(self, inputs, upstream):
        conversion = registry.get(_prop(self, "conversion"))
        exe = tools.find(conversion.tool)
        source, output = _prop(self, "input"), _prop(self, "output")
        self._cancel.clear()
        threading.Thread(target=self._run, args=(conversion, source, output, exe), daemon=True,
                         name="convert-" + os.path.basename(source)).start()

    def onCancel(self):
        self._cancel.set()
        with self._proc_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _track(self, proc):
        with self._proc_lock:
            self._proc = proc

    def _log(self, stream, text):
        self.log(work.LogStream.Stderr if stream == "stderr" else work.LogStream.Stdout, text)

    def _run(self, conversion, source, output, exe):
        """Worker thread: wait for a slot, then run.run_conversion() with the Op as its callbacks."""
        try:
            self.log(work.LogStream.Stdout, "waiting for a free slot")
            self.setStatus("Waiting for a free slot")
            if not runner.acquire(self._cancel):
                self.acknowledgeCancel()
                return
            try:
                written = run_conversion(conversion, source, output, exe, log=self._log,
                                         progress=self.setProgress, status=self.setStatus,
                                         cancel=self._cancel, on_process=self._track)
            finally:
                runner.release()
                self._track(None)
            out = work.Params()
            out.set("output", written)
            self.complete(out)
        except Cancelled:
            self.acknowledgeCancel()
        except ConversionError as exc:  # the tool failed, or a runs_itself conversion said why
            self.log(work.LogStream.Stderr, str(exc))
            self.fail(str(exc))
        except Exception as exc:        # never leave the Op RUNNING
            if self._cancel.is_set():
                self.acknowledgeCancel()
            else:
                self.log(work.LogStream.Stderr, "%s: %s" % (type(exc).__name__, exc))
                self.fail("%s: %s" % (type(exc).__name__, exc))


class InstallToolOp(work.Op):
    """Property: tool (name in tools.json)."""

    def __init__(self, *args):
        super().__init__(*args)
        self._cancel = threading.Event()
        self._logged = -1

    def typeName(self):
        return INSTALL_TYPE

    def validate(self):
        return tools.install_problems(_prop(self, "tool"))

    def start(self, inputs, upstream):
        self._cancel.clear()
        self._logged = -1
        threading.Thread(target=self._run, args=(_prop(self, "tool"),), daemon=True).start()

    def onCancel(self):
        self._cancel.set()

    def _report(self, text):
        """tools.install's progress callback: a log line, the status, and the download
        percentage (the download is the first 90% of the work, unpacking the rest)."""
        match = _DOWNLOADED.match(text)
        if match:
            percent = int(match.group(1))
            self.setProgress(0.9 * percent / 100)
            if percent // 10 > self._logged:        # the log gets every 10%, the status every update
                self._logged = percent // 10
                self.log(work.LogStream.Stdout, text)
        else:
            self.log(work.LogStream.Stdout, text)
            if text.startswith("checksum OK"):
                self.setProgress(0.9)
        self.setStatus(text)

    def _run(self, name):
        try:
            exe = tools.install(name, self._report, self._cancel)
            out = work.Params()
            out.set("path", exe)
            self.log(work.LogStream.Stdout, "installed " + exe)
            self.setStatus("Installed")
            self.complete(out)
        except tools.Cancelled:
            self.acknowledgeCancel()
        except Exception as exc:
            self.log(work.LogStream.Stderr, str(exc))
            self.fail(str(exc))


def register_types(op_registry):
    """Teach a work.OpRegistry the two types, so a saved queue can be rebuilt."""
    op_registry.registerType(CONVERT_TYPE, lambda id, name: ConvertOp(id, name))
    op_registry.registerType(INSTALL_TYPE, lambda id, name: InstallToolOp(id, name))
