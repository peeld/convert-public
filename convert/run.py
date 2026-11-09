"""Running one conversion on one file, with no work framework around it.

    run_conversion(conversion, source, output, tool_path, log=..., progress=..., status=...,
                   cancel=threading.Event(), on_process=..., values=None, details=None) -> output

It writes a temporary file beside `output` (paths.temp_name), renames it to `output`
when the tool succeeded, and removes it otherwise.  ConvertOp (ops.py) runs it on a
worker thread and turns the callbacks into the Op's log, progress and status; a
worker process or a script can call it the same way.

Callbacks (each optional, called from the calling thread or a reader thread):
    log(stream, text)     stream is "stdout" or "stderr"
    progress(fraction)    0.0 to 1.0
    status(text)          a short line for a UI
    on_process(proc)      every child process, so a canceller can terminate() it

values: what the caller knows about the file (conversion.VALUES), given to the conversion
in Job.values (only the names it declares).  details: a dict the conversion's run() fills
with what to keep about the run (Job.details).

Outcome: returns `output`; raises Cancelled when `cancel` was set (nothing written),
ConversionError with a message for the user when it failed (nothing written).

No PeelCore and no Qt here.
"""
import collections
import os
import re
import subprocess
import threading

from . import paths
from .conversion import TOOL_FLAGS, ConversionError, Job

_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Cancelled(Exception):
    """The run was cancelled; nothing was written."""


def _none(*_):
    pass


def run_conversion(conversion, source, output, tool_path, log=None, progress=None, status=None,
                   cancel=None, on_process=None, values=None, details=None):
    log, progress, status, on_process = log or _none, progress or _none, status or _none, on_process or _none
    cancel = cancel or threading.Event()
    given = {k: v for k, v in (values or {}).items() if k in conversion.values and v not in (None, "")}
    job = Job(input=source, output=paths.temp_name(output), tool_path=tool_path,
              values=given, details=details if details is not None else {})
    try:
        os.makedirs(os.path.dirname(job.output), exist_ok=True)
        if conversion.runs_itself:
            code, tail = _run_own(conversion, job, log, status, cancel, on_process)
        else:
            code, tail = _run_tool(conversion, job, log, progress, status, cancel, on_process)
        if cancel.is_set():
            log("stdout", "cancelled")
            status("Cancelled")
            raise Cancelled()
        if code != 0:
            raise ConversionError("%s exited with code %d: %s" % (conversion.tool, code, " | ".join(tail[-3:])))
        if os.path.exists(output):
            raise ConversionError("output exists")
        status("Finishing")
        os.replace(job.output, output)
    except BaseException:
        _remove(job.output)
        if cancel.is_set():
            raise Cancelled() from None
        raise
    log("stdout", "wrote " + output)
    status("Done")
    return output


def _run_own(conversion, job, log, status, cancel, on_process):
    """A conversion that does its own work (conversion.runs_itself)."""
    log("stdout", "run: %s %s" % (job.tool_path, os.path.basename(job.input)))
    status("Working")

    def track(proc):
        on_process(proc)
        if cancel.is_set():
            proc.terminate()

    conversion.run(job, cancel, track)
    return 0, []


class _Progress:
    """ffmpeg's -progress lines (stdout) against the Duration it prints (stderr).  The two
    come on separate pipes and threads, so a progress line may arrive before the duration:
    the times seen so far are kept and reported, in order, once the duration is known."""

    def __init__(self, log, progress, status):
        self.log, self.progress, self.status = log, progress, status
        self.lock = threading.Lock()
        self.duration = 0.0
        self.early = []         # times seen before the duration
        self.pct = -1
        self.logged = -1

    def set_duration(self, seconds):
        with self.lock:
            if self.duration:
                return
            self.duration = seconds
            early, self.early = self.early, []
            for t in early:
                self._report(t)

    def set_time(self, seconds):
        with self.lock:
            if self.duration:
                self._report(seconds)
            else:
                self.early.append(seconds)

    def _report(self, seconds):
        fraction = min(1.0, seconds / self.duration)
        self.progress(fraction)
        percent = int(fraction * 100)
        if percent != self.pct:
            self.pct = percent
            self.status("Converting: %d%%" % percent)
        if percent // 10 > self.logged // 10:
            self.logged = percent
            self.log("stdout", "progress %d%%" % percent)


def _run_tool(conversion, job, log, progress, status, cancel, on_process):
    argv = [job.tool_path, *TOOL_FLAGS]
    argv += conversion.build_command(job)
    log("stdout", "run: " + " ".join(argv))
    status("Converting")
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    on_process(proc)
    if cancel.is_set():           # cancelled before the launch
        proc.terminate()
    tracker = _Progress(log, progress, status)
    tail = collections.deque(maxlen=12)

    def read_stderr():
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            match = _DURATION.search(line)
            if match:
                h, m, s = match.groups()
                tracker.set_duration(int(h) * 3600 + int(m) * 60 + float(s))
            tail.append(line)
            log("stderr", line)

    err_thread = threading.Thread(target=read_stderr, daemon=True)
    err_thread.start()
    for raw in proc.stdout:
        # key=value lines; out_time_us (and the misnamed out_time_ms) are microseconds
        key, _, value = raw.decode("utf-8", "replace").strip().partition("=")
        if key in ("out_time_us", "out_time_ms"):
            try:
                tracker.set_time(int(value) / 1_000_000)
            except ValueError:
                pass
    code = proc.wait()
    err_thread.join(5)
    proc.stdout.close()
    proc.stderr.close()
    return code, list(tail)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
