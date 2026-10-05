"""The Ops the C++ host queues: ConvertOp (one file, one conversion) and
InstallToolOp (download a tool).  Both are asynchronous: start() returns at
once and a worker thread calls complete(), fail() or acknowledgeCancel().

Both report progress (setProgress, 0.0 to 1.0) and a status line (setStatus) for the UI.

Properties of a ConvertOp (all strings):
    conversion   id of a registered Conversion
    input        full path of the source file
    output       full path of the file to write; empty until the output directory is resolved
    conflict     set by the app when another queued file would write the same output
"""
import collections
import os
import re
import subprocess
import sys
import threading

from PeelCore import work

from . import paths, runner, tools
from .conversion import Job, registry

CONVERT_TYPE = "convert.job"
INSTALL_TYPE = "tool.install"

_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_DOWNLOADED = re.compile(r"downloaded (\d+)%")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
        job = Job(input=source, output=paths.temp_name(output), tool_path=exe)
        self._cancel.clear()
        threading.Thread(target=self._run, args=(conversion, job, output), daemon=True,
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

    def _run(self, conversion, job, output):
        try:
            self.log(work.LogStream.Stdout, "waiting for a free slot")
            self.setStatus("Waiting for a free slot")
            if not runner.acquire(self._cancel):
                self.acknowledgeCancel()
                return
            try:
                code, tail = self._execute(conversion, job)
            finally:
                runner.release()
            self._finish(code, tail, job, output)
        except Exception as exc:        # never leave the Op RUNNING
            _remove(job.output)
            if self._cancel.is_set():
                self.acknowledgeCancel()
            else:
                self.log(work.LogStream.Stderr, "%s: %s" % (type(exc).__name__, exc))
                self.fail("%s: %s" % (type(exc).__name__, exc))

    def _execute(self, conversion, job):
        os.makedirs(os.path.dirname(job.output), exist_ok=True)
        argv = [job.tool_path, "-nostdin", "-hide_banner", "-nostats", "-progress", "pipe:1", "-y"]
        argv += conversion.build_command(job)
        self.log(work.LogStream.Stdout, "run: " + " ".join(argv))
        self.setStatus("Converting")
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        with self._proc_lock:
            self._proc = proc
        if self._cancel.is_set():           # cancelled between the slot and the launch
            proc.terminate()
        state = {"duration": 0.0, "last": -1, "pct": -1}
        tail = collections.deque(maxlen=12)
        err_thread = threading.Thread(target=self._read_stderr, args=(proc, state, tail), daemon=True)
        err_thread.start()
        for raw in proc.stdout:
            self._progress(raw.decode("utf-8", "replace").strip(), state)
        code = proc.wait()
        err_thread.join(5)
        proc.stdout.close()
        proc.stderr.close()
        with self._proc_lock:
            self._proc = None
        return code, list(tail)

    def _read_stderr(self, proc, state, tail):
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            match = _DURATION.search(line)
            if match and not state["duration"]:
                h, m, s = match.groups()
                state["duration"] = int(h) * 3600 + int(m) * 60 + float(s)
            tail.append(line)
            self.log(work.LogStream.Stderr, line)

    def _progress(self, line, state):
        # ffmpeg -progress: key=value lines; out_time_us (and the misnamed out_time_ms) are microseconds
        key, _, value = line.partition("=")
        if key in ("out_time_us", "out_time_ms") and state["duration"]:
            try:
                seconds = int(value) / 1_000_000
            except ValueError:
                return
            fraction = min(1.0, seconds / state["duration"])
            self.setProgress(fraction)
            percent = int(fraction * 100)
            if percent != state["pct"]:
                state["pct"] = percent
                self.setStatus("Converting: %d%%" % percent)
            if percent // 10 > state["last"] // 10:
                state["last"] = percent
                self.log(work.LogStream.Stdout, "progress %d%%" % percent)

    def _finish(self, code, tail, job, output):
        if self._cancel.is_set():
            _remove(job.output)
            self.log(work.LogStream.Stdout, "cancelled")
            self.setStatus("Cancelled")
            self.acknowledgeCancel()
        elif code != 0:
            _remove(job.output)
            self.fail("ffmpeg exited with code %d: %s" % (code, " | ".join(tail[-3:])))
        elif os.path.exists(output):
            _remove(job.output)
            self.fail("output exists")
        else:
            self.setStatus("Finishing")
            os.replace(job.output, output)
            self.log(work.LogStream.Stdout, "wrote " + output)
            self.setStatus("Done")
            out = work.Params()
            out.set("output", output)
            self.complete(out)


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


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def register_types(op_registry):
    """Teach a work.OpRegistry the two types, so a saved queue can be rebuilt."""
    op_registry.registerType(CONVERT_TYPE, lambda id, name: ConvertOp(id, name))
    op_registry.registerType(INSTALL_TYPE, lambda id, name: InstallToolOp(id, name))
