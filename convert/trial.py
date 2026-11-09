"""Try a conversion on a sample file without touching the user's files.

Pure Python (no PeelCore, no Qt).  Used by the MCP tools (mcp_tools.py) so an AI agent can
test a draft before saving it, and runnable from a plain interpreter in tests.

    make_sample(conversion, folder)      -> (path, warnings)   a tiny generated input file
    run_trial(conversion, source, ...)   -> dict               what happened, JSON-friendly
"""
import json
import os
import subprocess
import threading
import time

from . import paths, tools
from .conversion import TOOL_FLAGS, ConversionError, Job

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VIDEO_EXTS = {"mov", "mkv", "avi", "webm", "mp4", "m4v", "wmv", "flv", "mpg", "mpeg", "ts", "mts",
              "m2ts", "3gp", "ogv"}
AUDIO_EXTS = {"wav", "flac", "mp3", "m4a", "aac", "ogg", "opus", "wma", "aiff", "aif"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "bmp", "gif", "tif", "tiff", "webp"}

# containers where the generic mpeg4 + aac pair is known to work in an LGPL ffmpeg
_MPEG4_AAC = {"mp4", "mov", "mkv", "m4v", "ts", "mts", "m2ts", "3gp"}
_TAIL_LINES = 15


class TrialError(Exception):
    """The trial could not even start (no tool, no sample); the message is fit to show."""


def _ffmpeg_run(ffmpeg, args, timeout=60):
    argv = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"] + args
    proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, creationflags=_NO_WINDOW)
    return proc.returncode, proc.stderr.decode("utf-8", "replace").strip()


def _first_ext(conversion):
    for ext in conversion.inputs:
        ext = ext.lower().lstrip(".")
        if ext and ext != "*":
            return ext
    return "mkv"


def make_sample(conversion, folder):
    """A short generated file the conversion accepts, in `folder`.  Returns (path, warnings)."""
    ffmpeg = tools.find("ffmpeg")
    if not ffmpeg:
        raise TrialError("ffmpeg is not installed, so no sample file can be made. Pass input_path, "
                         "or ask the person to let PeelConvert install ffmpeg first.")
    ext = _first_ext(conversion)
    video = ["-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25"]
    audio = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    if ext in AUDIO_EXTS:
        kind, args = "audio", audio
    elif ext in IMAGE_EXTS:
        kind, args = "image", video + ["-frames:v", "1"]
    else:
        kind = "video"
        args = video + audio + ["-shortest"]
        if ext in _MPEG4_AAC:
            args += ["-c:v", "mpeg4", "-c:a", "aac"]
    warnings = []
    path = os.path.join(folder, "sample." + ext)
    code, err = _ffmpeg_run(ffmpeg, args + [path])
    if code != 0:
        fallback = {"audio": "wav", "image": "png", "video": "mkv"}[kind]
        warnings.append("could not generate a sample .%s (%s); used .%s instead"
                        % (ext, err.splitlines()[-1] if err else "ffmpeg failed", fallback))
        path = os.path.join(folder, "sample." + fallback)
        args = args + (["-c:v", "mpeg4", "-c:a", "aac"] if kind == "video" else [])
        code, err = _ffmpeg_run(ffmpeg, args + [path])
        if code != 0:
            raise TrialError("could not generate a sample file: " + err)
    return path, warnings


def describe_output(path):
    """A short, JSON-friendly summary of a media file (via ffprobe), or {} if that is not possible."""
    ffprobe = tools.find("ffprobe")
    if not ffprobe or not os.path.isfile(path):
        return {}
    try:
        proc = subprocess.run([ffprobe, "-v", "error", "-hide_banner", "-print_format", "json",
                               "-show_format", "-show_streams", "-i", path], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                              creationflags=_NO_WINDOW)
        raw = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    out = {}
    fmt = raw.get("format", {})
    if fmt.get("format_name"):
        out["container"] = fmt["format_name"]
    if fmt.get("duration"):
        try:
            out["duration_s"] = round(float(fmt["duration"]), 2)
        except ValueError:
            pass
    streams = []
    for s in raw.get("streams", []):
        item = {"type": s.get("codec_type"), "codec": s.get("codec_name")}
        for key in ("width", "height", "pix_fmt", "sample_rate", "channels"):
            if s.get(key) not in (None, ""):
                item[key] = s[key]
        if s.get("r_frame_rate") and s.get("codec_type") == "video":
            item["frame_rate"] = s["r_frame_rate"]
        streams.append(item)
    if streams:
        out["streams"] = streams
    return out


def run_trial(conversion, source, timeout=60.0):
    """Run `conversion` on `source` (read only) with the output in a temporary folder.

    Returns a dict: ok, command, exit_code, seconds, messages (the last lines the tool printed),
    and when it worked output (name, size, and ffprobe's view of it).  Raises TrialError when the
    tool is missing.  The temporary output is always deleted."""
    import tempfile
    exe = tools.find(conversion.tool)
    if not exe:
        raise TrialError("%s is not installed on this computer yet" % conversion.tool)
    if not os.path.isfile(source):
        raise TrialError("input file not found: " + source)
    reason = conversion.unavailable()
    if reason:
        raise TrialError(reason)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="peelconvert-trial-") as tmp:
        output = os.path.join(tmp, conversion.output_name(source))
        job = Job(input=os.path.abspath(source), output=paths.temp_name(output), tool_path=exe)
        result = {"ok": False, "input": os.path.basename(source)}
        if conversion.runs_itself:
            result.update(_run_own(conversion, job, timeout))
        else:
            argv = [exe, *TOOL_FLAGS, *conversion.build_command(job)]
            shown = [a.replace(job.input, "<input>").replace(job.output, "<output>") for a in argv[1:]]
            result["command"] = [os.path.basename(argv[0])] + shown
            result.update(_run_process(argv, timeout))
        result["seconds"] = round(time.monotonic() - started, 2)
        if result.get("exit_code") == 0:
            result.pop("messages", None)          # a good run's banner is noise; a failure keeps its tail
        if result.get("exit_code") == 0 and os.path.isfile(job.output):
            size = os.path.getsize(job.output)
            result["output"] = {"name": conversion.output_name(source), "bytes": size}
            if size == 0:
                result["problem"] = "the tool finished but wrote an empty file"
            else:
                result["ok"] = True
                result["output"].update(describe_output(job.output))
        elif result.get("exit_code") == 0:
            result["problem"] = "the tool finished without writing the output file"
    return result


def _run_process(argv, timeout):
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    try:
        _, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
        return {"exit_code": None, "problem": "timed out after %g seconds" % timeout,
                "messages": _tail(err)}
    return {"exit_code": proc.returncode, "messages": _tail(err)}


def _run_own(conversion, job, timeout):
    cancel = threading.Event()
    box = {}
    procs = []

    def work():
        try:
            os.makedirs(os.path.dirname(job.output), exist_ok=True)
            conversion.run(job, cancel, procs.append)
            box["exit_code"] = 0
        except ConversionError as exc:
            box.update(exit_code=1, messages=[str(exc)])
        except Exception as exc:      # noqa: BLE001 - report, never crash the server
            box.update(exit_code=1, messages=["%s: %s" % (type(exc).__name__, exc)])

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        cancel.set()
        for proc in procs:
            try:
                proc.terminate()
            except OSError:
                pass
        return {"exit_code": None, "problem": "timed out after %g seconds" % timeout}
    return box


def _tail(data):
    lines = [l.rstrip() for l in data.decode("utf-8", "replace").splitlines() if l.strip()]
    return lines[-_TAIL_LINES:]
