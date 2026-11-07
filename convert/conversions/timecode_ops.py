"""Operations that keep a video's timecode: Burn In Timecode and Export WAV.

Both use the start timecode and rates the app knows (Job.values: tc_start, tc_rate,
video_rate, see conversion.VALUES) and read any it doesn't from the file with ffprobe.
What they used goes in Job.details for the app's record of the run.

Burn In Timecode: the video re-encoded as H.264 with its timecode drawn on each frame,
its audio copied, and a timecode track from the same start (<stem>.tc.mov). H.264 is
libopenh264 when the ffmpeg has it (LGPL builds), else libx264. drawtext gets a font
file found here (PEEL_FONT, else a system monospace font), so it needs no fontconfig.

Export WAV: the first audio stream as 24-bit PCM Broadcast WAV at its own sample rate
(<stem>.wav). The bext time reference is the start timecode as samples since midnight
(frames / the timecode's exact rate x the sample rate); description and comment say the
timecode as written.
"""
import os
import shutil
import struct
import subprocess
from fractions import Fraction

from convert import Conversion, register
from convert import mediainfo, timecode, tools
from convert.conversion import ConversionError

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
VIDEO = ("mov", "mp4", "m4v", "mkv", "avi", "mxf", "webm", "mts", "m2ts", "ts", "mpg", "mpeg", "wmv")


# ---- what the file says, when the app didn't --------------------------------------------
def file_values(path, cancel=None, on_process=None):
    """{"tc_start", "tc_rate", "video_rate"} read with ffprobe; "" for what isn't there.
    The timecode is taken to count at the video's rate."""
    ffprobe = tools.find("ffprobe")
    if ffprobe is None:
        raise ConversionError("ffprobe not found: it reads the video's timecode and rate")
    try:
        meta = mediainfo.build_meta(mediainfo.probe(ffprobe, path, cancel, on_process), path)
    except mediainfo.ProbeError as exc:
        raise ConversionError(str(exc))
    video = next((v for v in meta.get("video", []) if not v.get("attached_picture")), {})
    rate = video.get("frame_rate_text", "")
    return {"tc_start": meta.get("timecode") or "", "tc_rate": rate, "video_rate": rate}


def _values(job, cancel, on_process, wanted):
    """job.values, with what's missing of `wanted` read from the file."""
    values = dict(job.values)
    missing = [k for k in wanted if not values.get(k)]
    if missing:
        found = file_values(job.input, cancel, on_process)
        values.update({k: found.get(k, "") for k in missing})
    return values


def _app_name():
    from convert import host
    try:
        return host.app_name()
    except Exception:       # outside an app, with no name configured
        return ""


# ---- running ffmpeg -------------------------------------------------------------------
def _run(argv, cancel, on_process):
    if cancel is not None and cancel.is_set():
        raise ConversionError("cancelled")
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    if on_process:
        on_process(proc)
    _, err = proc.communicate()
    if proc.returncode != 0:
        lines = [l for l in err.decode("utf-8", "replace").splitlines() if l.strip()]
        raise ConversionError("ffmpeg failed: " + ("\n".join(lines[-8:]) or "exit code %d" % proc.returncode))


def _h264_encoder(ffmpeg):
    out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
                         errors="replace", creationflags=_NO_WINDOW).stdout
    for name in ("libopenh264", "libx264"):
        if (" %s " % name) in out:
            return name
    raise ConversionError("this ffmpeg has no H.264 encoder (libopenh264 or libx264)")


def _escape(text, chars):
    return "".join("\\" + c if c in chars or c == "\\" else c for c in text)


def _filter_value(text):
    """A drawtext option value, escaped for the option parser and then the filtergraph's."""
    return _escape(_escape(text, "':"), "'[],;")


def font():
    """A monospace TTF for drawtext: PEEL_FONT, else a system one."""
    candidates = [os.environ.get("PEEL_FONT", "")]
    if os.name == "nt":
        fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        candidates += [os.path.join(fonts, name) for name in ("consola.ttf", "lucon.ttf", "cour.ttf")]
    else:
        candidates += ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                       "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
                       "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
                       "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
                       "/usr/share/fonts/liberation-mono/LiberationMono-Regular.ttf",
                       "/System/Library/Fonts/Menlo.ttc"]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    if shutil.which("fc-match"):
        found = subprocess.run(["fc-match", "-f", "%{file}", "monospace"], capture_output=True, text=True).stdout
        if found and os.path.isfile(found):
            return found
    raise ConversionError("no font for the timecode: set PEEL_FONT to a .ttf file")


def start_at(tc, tc_rate, rate):
    """The start timecode to burn in at the video's rate, and warnings: the video's own
    when it counts at that rate; converted when it counts at another; else midnight."""
    if not tc:
        return "00:00:00:00", ["no timecode: burnt in from 00:00:00:00"]
    tc_rate = tc_rate or rate
    if tc_rate == rate:
        return tc, []
    frames = round(timecode.seconds(tc, tc_rate) * rate)
    converted = timecode.frames_tc(frames, rate, timecode.is_drop(tc) and timecode.nominal(rate) in (30, 60))
    return converted, ["timecode %s counts at %s, the video at %s: burnt in from %s" % (tc, tc_rate, rate, converted)]


@register
class BurnInTimecode(Conversion):
    id = "video.burn-in-timecode"
    label = "Burn In Timecode"
    group = "Video"
    tool = "ffmpeg"
    inputs = VIDEO
    output_ext = "mov"
    output = "{stem}.tc.mov"
    values = ("tc_start", "tc_rate", "video_rate")
    runs_itself = True
    bitrate = "20M"

    def run(self, job, cancel, on_process=None):
        values = _values(job, cancel, on_process, self.values)
        rate = timecode.parse_rate(values.get("video_rate"))
        if rate is None:
            raise ConversionError("the video's frame rate isn't known")
        start, warnings = start_at(values.get("tc_start"), timecode.parse_rate(values.get("tc_rate")), rate)
        drawtext = ":".join([
            "drawtext=fontfile=" + _filter_value(font()),
            "timecode=" + _filter_value(start),
            "rate=" + timecode.rate_text(rate),
            "fontsize=h/16", "fontcolor=white",
            "box=1", "boxcolor=black@0.6", "boxborderw=8",
            "x=(w-tw)/2", "y=h-th-h/24",
        ])
        encoder = _h264_encoder(job.tool_path)
        extra = ["-allow_skip_frames", "0"] if encoder == "libopenh264" else []
        _run([job.tool_path, "-hide_banner", "-nostdin", "-y", "-i", job.input,
              "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0",
              "-vf", drawtext + ",format=yuv420p",
              "-c:v", encoder, "-b:v", self.bitrate, *extra,
              "-c:a", "copy", "-timecode", start, "-f", "mov", job.output], cancel, on_process)
        job.details.update({"tc_start": start, "rate": timecode.rate_text(rate), "warnings": warnings})


# ---- Broadcast WAV ------------------------------------------------------------------
def _wav_chunks(fp):
    """{chunk id: (offset of its data, size)} for a RIFF/RF64 WAVE file, up to its data chunk."""
    head = fp.read(12)
    if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
        raise ConversionError("not a WAV file")
    chunks = {}
    while True:
        header = fp.read(8)
        if len(header) < 8:
            return chunks
        cid, size = header[:4], struct.unpack("<I", header[4:])[0]
        chunks[cid] = (fp.tell(), size)
        if cid == b"data":
            return chunks
        fp.seek(size + (size & 1), os.SEEK_CUR)


def set_time_reference(path, tc, tc_rate):
    """Write tc as the bext time reference (samples since midnight at the file's sample
    rate); returns (time reference, sample rate)."""
    with open(path, "r+b") as fp:
        chunks = _wav_chunks(fp)
        if b"fmt " not in chunks or b"bext" not in chunks:
            raise ConversionError("ffmpeg wrote no fmt or bext chunk")
        fp.seek(chunks[b"fmt "][0] + 4)
        sample_rate = struct.unpack("<I", fp.read(4))[0]
        reference = int(timecode.seconds(tc, tc_rate) * sample_rate)
        # bext: Description[256] Originator[32] OriginatorReference[32] Date[10] Time[8], then TimeReference.
        fp.seek(chunks[b"bext"][0] + 338)
        fp.write(struct.pack("<II", reference & 0xFFFFFFFF, reference >> 32))
    return reference, sample_rate


@register
class ExportWav(Conversion):
    id = "audio.export-bwf"
    label = "Export WAV (Broadcast WAV with timecode)"
    group = "Audio"
    tool = "ffmpeg"
    inputs = VIDEO
    output_ext = "wav"
    output = "{stem}.wav"
    values = ("tc_start", "tc_rate")
    runs_itself = True

    @staticmethod
    def _originator():
        name = _app_name()
        return ["-metadata", "encoded_by=" + name, "-metadata", "originator=" + name] if name else []

    def run(self, job, cancel, on_process=None):
        values = _values(job, cancel, on_process, self.values)
        tc = values.get("tc_start") or ""
        tc_rate = timecode.parse_rate(values.get("tc_rate"))
        timed = bool(tc and tc_rate)
        metadata, warnings = [], []
        if timed:
            timecode.tc_frames(tc, tc_rate)     # that it parses, before ffmpeg runs
            label = "%s@%s" % (tc, timecode.rate_text(tc_rate))
            metadata = ["-metadata", "description=timecode " + label, "-metadata", "comment=timecode " + label]
        else:
            warnings.append("no timecode: the WAV's time reference is 0")
        try:
            _run([job.tool_path, "-hide_banner", "-nostdin", "-y", "-i", job.input,
                  "-map", "0:a:0", "-vn", "-c:a", "pcm_s24le", "-write_bext", "1", "-rf64", "auto",
                  *self._originator(), *metadata, "-f", "wav", job.output], cancel, on_process)
        except ConversionError as exc:
            if "matches no streams" in str(exc):
                raise ConversionError("the video has no audio") from None
            raise
        reference, sample_rate = set_time_reference(job.output, tc, tc_rate) if timed else (0, None)
        job.details.update({"tc_start": tc, "sample_rate": sample_rate, "time_reference": reference,
                            "warnings": warnings})
