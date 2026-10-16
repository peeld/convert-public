"""Media metadata: run ffprobe on a file and turn its JSON into the `.meta` document.

Pure Python (no PeelCore, no Qt), so it is testable with a plain interpreter.

    probe(ffprobe, path)      -> ffprobe's own JSON (dict); raises ProbeError
    build_meta(raw, path)     -> the dict written to `<file>.meta`
    write_meta(meta, dest)    -> UTF-8 JSON file

The `.meta` document (schema 1) looks like:

    {
      "schema": 1,
      "file": {"name": "foo.mov", "size": 123, "container": "mov,mp4,m4a,3gp,3g2,mj2",
               "container_long": "QuickTime / MOV", "duration": 10.01, "bit_rate": 5000000},
      "timecode": "01:00:00:00",              first timecode found, else null
      "video": [{"index": 0, "codec": "prores", "profile": "HQ", "width": 1920, "height": 1080,
                 "resolution": "1920x1080", "pix_fmt": "yuv422p10le", "bit_depth": 10,
                 "frame_rate": 25.0, "frame_rate_text": "25/1", "frame_count": 250,
                 "frame_count_source": "container",     "container" | "packets" | "estimated"
                 "duration": 10.0, "bit_rate": ..., "timecode": "01:00:00:00", "rotation": 0,
                 "color_space": ..., "color_range": ..., "color_transfer": ..., "color_primaries": ...,
                 "field_order": ..., "display_aspect_ratio": ..., "sample_aspect_ratio": ...}],
      "audio": [{"index": 1, "codec": ..., "sample_rate": 48000, "channels": 2,
                 "channel_layout": "stereo", "duration": ..., "bit_rate": ..., "language": ...}],
      "subtitle": [...], "data": [...],       other streams (tmcd timecode tracks land in "data")
      "tags": {...}                           container tags (title, creation_time, ...)
    }

Keys with no value are left out of a stream, never written as null (except top-level
"timecode", which is always present so a reader can test it without `in`).
"""
import json
import os
import re
import subprocess

SCHEMA = 1
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_TIMECODE = re.compile(r"^\d{1,2}[:;]\d{2}[:;]\d{2}[:;.]\d{2,3}$")


class ProbeError(Exception):
    """ffprobe could not read the file (the message is fit to show the user)."""


# ---- running ffprobe -----------------------------------------------------------------
def _run(argv, cancel=None, on_process=None):
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    if on_process is not None:
        on_process(proc)            # lets the caller terminate it on cancel
    out, err = proc.communicate()
    if cancel is not None and cancel.is_set():
        raise ProbeError("cancelled")
    if proc.returncode != 0:
        lines = [l for l in err.decode("utf-8", "replace").splitlines() if l.strip()]
        raise ProbeError("ffprobe exited with code %d: %s" % (proc.returncode, " | ".join(lines[-3:]) or "no message"))
    try:
        return json.loads(out.decode("utf-8", "replace") or "{}")
    except ValueError as exc:
        raise ProbeError("ffprobe gave unreadable output: %s" % exc)


def probe(ffprobe, path, cancel=None, on_process=None):
    """ffprobe's JSON for `path`.  If a video stream does not state its frame count,
    a second pass counts its packets (reads the whole file, but does not decode)."""
    base = [ffprobe, "-v", "error", "-hide_banner", "-print_format", "json",
            "-show_format", "-show_streams"]
    raw = _run(base + ["-i", path], cancel, on_process)
    if not raw.get("streams"):
        raise ProbeError("no media streams found (not a media file?)")
    if any(s.get("codec_type") == "video" and not _int(s.get("nb_frames"))
           and not _attached_picture(s) for s in raw["streams"]):
        counted = _run(base + ["-count_packets", "-select_streams", "v", "-i", path], cancel, on_process)
        packets = {s.get("index"): s.get("nb_read_packets") for s in counted.get("streams", [])}
        for s in raw["streams"]:
            if s.get("index") in packets:
                s["nb_read_packets"] = packets[s["index"]]
    return raw


# ---- converting ----------------------------------------------------------------------
def _int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number != float("inf") else None   # not NaN/inf


def _rate(text):
    """'30000/1001' -> 29.97002997...  ; '0/0' or junk -> None."""
    try:
        num, _, den = str(text).partition("/")
        den = den or "1"
        return float(num) / float(den) if float(den) else None
    except ValueError:
        return None


def _attached_picture(stream):
    return bool(stream.get("disposition", {}).get("attached_pic"))


def _tag(tags, name):
    for key, value in (tags or {}).items():
        if key.lower() == name:
            return value
    return None


def _timecode(stream):
    value = _tag(stream.get("tags"), "timecode")
    if value and _TIMECODE.match(str(value).strip()):
        return str(value).strip()
    for side in stream.get("side_data_list", []) or []:
        if side.get("side_data_type", "").lower().startswith("smpte") and side.get("timecode"):
            return side["timecode"]
    return None


def _rotation(stream):
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            return int(round(_float(side["rotation"]) or 0))
    rotate = _float(_tag(stream.get("tags"), "rotate"))
    return int(round(rotate)) if rotate is not None else None


def _put(target, key, value):
    if value is not None and value != "" and value != "unknown" and value != "N/A":
        target[key] = value


def _common(stream):
    out = {"index": stream.get("index")}
    _put(out, "codec", stream.get("codec_name"))
    _put(out, "codec_long", stream.get("codec_long_name"))
    _put(out, "profile", stream.get("profile"))
    _put(out, "codec_tag", stream.get("codec_tag_string") if stream.get("codec_tag_string", "")[:1] != "[" else None)
    _put(out, "duration", _float(stream.get("duration")))
    _put(out, "bit_rate", _int(stream.get("bit_rate")))
    _put(out, "language", _tag(stream.get("tags"), "language"))
    _put(out, "title", _tag(stream.get("tags"), "title"))
    return out


def _video(stream):
    out = _common(stream)
    width, height = _int(stream.get("width")), _int(stream.get("height"))
    _put(out, "width", width)
    _put(out, "height", height)
    if width and height:
        out["resolution"] = "%dx%d" % (width, height)
    _put(out, "pix_fmt", stream.get("pix_fmt"))
    _put(out, "bit_depth", _int(stream.get("bits_per_raw_sample")))
    rate_text = stream.get("avg_frame_rate")
    rate = _rate(rate_text)
    if not rate:
        rate_text = stream.get("r_frame_rate")
        rate = _rate(rate_text)
    if rate:
        out["frame_rate"] = round(rate, 6)
        out["frame_rate_text"] = rate_text
    frames = _int(stream.get("nb_frames"))
    source = "container"
    if not frames:
        frames, source = _int(stream.get("nb_read_packets")), "packets"
    if not frames and rate and _float(stream.get("duration")):
        frames, source = int(round(rate * _float(stream["duration"]))), "estimated"
    if frames and not _attached_picture(stream):
        out["frame_count"] = frames
        out["frame_count_source"] = source
    _put(out, "timecode", _timecode(stream))
    rotation = _rotation(stream)
    _put(out, "rotation", rotation)
    for key in ("color_space", "color_range", "color_transfer", "color_primaries", "field_order",
                "display_aspect_ratio", "sample_aspect_ratio"):
        _put(out, key, stream.get(key))
    if _attached_picture(stream):
        out["attached_picture"] = True
    return out


def _audio(stream):
    out = _common(stream)
    _put(out, "sample_rate", _int(stream.get("sample_rate")))
    _put(out, "channels", _int(stream.get("channels")))
    _put(out, "channel_layout", stream.get("channel_layout"))
    _put(out, "sample_fmt", stream.get("sample_fmt"))
    _put(out, "bit_depth", _int(stream.get("bits_per_raw_sample")) or _int(stream.get("bits_per_sample")))
    return out


def _other(stream):
    out = _common(stream)
    _put(out, "timecode", _timecode(stream))
    return out


def build_meta(raw, path):
    """The `.meta` document for ffprobe's JSON `raw` of the file at `path`."""
    fmt = raw.get("format", {})
    streams = raw.get("streams", [])
    meta = {"schema": SCHEMA, "kind": "media", "file": {}, "timecode": None}      # timecode filled in below
    info = meta["file"]
    info["name"] = os.path.basename(path)
    _put(info, "size", _int(fmt.get("size")))
    _put(info, "container", fmt.get("format_name"))
    _put(info, "container_long", fmt.get("format_long_name"))
    _put(info, "duration", _float(fmt.get("duration")))
    _put(info, "bit_rate", _int(fmt.get("bit_rate")))
    _put(info, "start_time", _float(fmt.get("start_time")))
    meta["video"] = [_video(s) for s in streams if s.get("codec_type") == "video"]
    meta["audio"] = [_audio(s) for s in streams if s.get("codec_type") == "audio"]
    meta["subtitle"] = [_other(s) for s in streams if s.get("codec_type") == "subtitle"]
    meta["data"] = [_other(s) for s in streams if s.get("codec_type") not in ("video", "audio", "subtitle")]
    timecode = _tag(fmt.get("tags"), "timecode")
    if not (timecode and _TIMECODE.match(str(timecode).strip())):
        timecode = next((s["timecode"] for group in ("video", "data", "audio") for s in meta[group]
                         if s.get("timecode")), None)
    meta["timecode"] = str(timecode).strip() if timecode else None
    tags = {k: v for k, v in (fmt.get("tags") or {}).items() if str(v).strip()}
    if tags:
        meta["tags"] = tags
    return meta


def write_meta(meta, dest):
    with open(dest, "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
        f.write("\n")


def extract(ffprobe, path, dest, cancel=None, on_process=None):
    """probe + build + write.  Returns the meta dict."""
    meta = build_meta(probe(ffprobe, path, cancel, on_process), path)
    write_meta(meta, dest)
    return meta
