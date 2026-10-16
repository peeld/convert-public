"""FBX metadata: read a .fbx with ufbx and describe it as the `.meta` document.

Pure Python (no PeelCore, no Qt).  The `ufbx` module (pyufbx, a compiled package shipped
with the app) is imported only when a file is read, so everything else here, including
`build_meta()` on any object shaped like a ufbx scene, works and is tested without it.

    unavailable()           -> "" or why FBX files cannot be read in this build
    extract(path, dest)     -> reads `path`, writes the JSON to `dest`, returns the dict
    build_meta(scene, path) -> the dict for an already loaded ufbx scene

The `.meta` document (schema 1, kind "fbx") looks like:

    {
      "schema": 1, "kind": "fbx",
      "file":    {"name": "rig.fbx", "size": 4821376, "encoding": "binary", "version": 7500},
      "creator": "FBX SDK/FBX Plugins version 2018.1.1",
      "scene":   {"up_axis": "+Y", "front_axis": "+Z", "right_axis": "+X", "y_up": true,
                  "z_up": false, "unit_meters": 0.01, "unit": "centimeters"},
      "counts":  {"nodes": 67, "meshes": 1, "vertices": 384, "faces": 512, "triangles": 512,
                  "materials": 1, "textures": 0, "bones": 64, "skins": 1, "blend_shapes": 0,
                  "cameras": 0, "lights": 0, "takes": 1},          nodes includes the scene root
      "meshes":  [{"name": "Rig Mesh", "vertices": 384, "faces": 512, "triangles": 512,
                   "materials": 1, "has_uvs": true, "has_normals": true}],
      "textures":[{"name": "...", "path": "textures/body.png", "embedded": false}],
      "skeleton":{"bones": 64, "roots": ["Root"], "depth": 12},
      "timecode":{"mode": "30 fps", "frame_rate": 30.0, "frame_rate_text": "30/1",
                  "drop_frame": false, "protocol": "default",
                  "start": {"timecode": "14:28:13:19", "frame": 1562809.25},
                  "end":   {"timecode": "14:30:56:26", "frame": 1567706.25}},
      "takes":   {"take name": {"start": {...}, "end": {...}, "duration": 163.233333, "frames": 4897}},
      "warnings": []
    }

Keys with no value are left out, as in the media `.meta` files.

Timecode notes:
  * ufbx gives take times in seconds.  A frame number is seconds times the exact frame
    rate (from the time mode, never from ufbx's 32-bit float rate), rounded to 6 decimals,
    so a start a quarter frame past frame 1562809 is written 1562809.25, not 1562809.2500001.
  * The timecode text always uses the whole frame.  Frame numbers and hours are never
    wrapped (a take at 14:28 is frame 1.5 million; 25 hours stays 25).
  * Drop-frame modes (30 fps drop, NTSC drop) write `;` before the frames.
  * When the file's protocol is "frame count" only frame numbers are written, no text.
"""
import importlib.util
import math
import os
import shutil
import tempfile
from fractions import Fraction

from . import mediainfo

SCHEMA = 1
BUILTIN_MESSAGE = "FBX support is not part of this build of PeelConvert (the ufbx library is missing)"


class FbxError(Exception):
    """The file could not be read (the message is fit to show the user)."""


# ---- time modes (ufbx / FBX SDK numbering) ---------------------------------------------
# mode: (name, exact rate or None to use the file's rate, drop frame)
TIME_MODES = {
    0: ("default", None, False),
    1: ("120 fps", Fraction(120), False),
    2: ("100 fps", Fraction(100), False),
    3: ("60 fps", Fraction(60), False),
    4: ("50 fps", Fraction(50), False),
    5: ("48 fps", Fraction(48), False),
    6: ("30 fps", Fraction(30), False),
    7: ("30 fps drop frame", Fraction(30), True),
    8: ("NTSC drop frame", Fraction(30000, 1001), True),
    9: ("NTSC full frame", Fraction(30000, 1001), False),
    10: ("PAL 25 fps", Fraction(25), False),
    11: ("24 fps", Fraction(24), False),
    12: ("1000 fps", Fraction(1000), False),
    13: ("film 23.976 fps", Fraction(24000, 1001), False),
    14: ("custom", None, False),
    15: ("96 fps", Fraction(96), False),
    16: ("72 fps", Fraction(72), False),
    17: ("59.94 fps", Fraction(60000, 1001), False),
    18: ("119.88 fps", Fraction(120000, 1001), False),
}
PROTOCOLS = {0: "SMPTE", 1: "frame count", 2: "default"}
AXES = {0: "+X", 1: "-X", 2: "+Y", 3: "-Y", 4: "+Z", 5: "-Z"}
UNITS = [(1.0, "meters"), (0.01, "centimeters"), (0.001, "millimeters"), (1000.0, "kilometers"),
         (0.0254, "inches"), (0.3048, "feet"), (0.9144, "yards"), (1609.344, "miles")]


def frame_rate(mode, file_fps):
    """(Fraction rate, drop-frame flag, mode name) for a time mode.  `file_fps` is only used
    for modes that carry no fixed rate (default, custom, unknown)."""
    name, rate, drop = TIME_MODES.get(mode, ("unknown mode %s" % mode, None, False))
    if rate is None:
        rate = Fraction(str(round(float(file_fps or 30.0), 6))).limit_denominator(100000)
    return rate, drop, name


def frame_of(seconds, rate):
    """(whole frame, fraction of a frame) for a time in seconds, at 6 decimals."""
    frames = round(seconds * float(rate), 6)
    whole = math.floor(frames)
    return whole, round(frames - whole, 6)


def frame_value(whole, sub):
    """The `frame` field: an int on a frame boundary, else a decimal such as 1562809.25."""
    return whole if not sub else round(whole + sub, 6)


def timecode_text(whole, rate, drop):
    """HH:MM:SS:FF (HH:MM:SS;FF for drop frame) for a whole frame number.  Hours do not wrap."""
    sign = "-" if whole < 0 else ""
    frame = abs(whole)
    nominal = max(1, int(round(rate)))
    if drop:
        dropped = 2 * max(1, nominal // 30)
        per_minute = nominal * 60 - dropped
        per_ten = nominal * 600 - 9 * dropped
        tens, rest = divmod(frame, per_ten)
        frame += 9 * dropped * tens
        if rest > dropped:
            frame += dropped * ((rest - dropped) // per_minute)
    seconds, frames = divmod(frame, nominal)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    width = max(2, len(str(nominal - 1)))
    return "%s%02d:%02d:%02d%s%0*d" % (sign, hours, minutes, seconds, ";" if drop else ":", width, frames)


def time_point(seconds, rate, drop, with_text):
    whole, sub = frame_of(seconds, rate)
    out = {}
    if with_text:
        out["timecode"] = timecode_text(whole, rate, drop)
    out["frame"] = frame_value(whole, sub)
    return out


# ---- other value helpers -----------------------------------------------------------------
def axis_name(value):
    return AXES.get(int(value))


def unit_name(unit_meters):
    for meters, name in UNITS:
        if math.isclose(unit_meters, meters, rel_tol=1e-6):
            return name
    return None


def _put(target, key, value):
    if value is not None and value != "":
        target[key] = value


def _count(items):
    try:
        return len(items)
    except TypeError:
        return 0


def _depth(node):
    depth = 0
    while getattr(node, "parent", None) is not None:
        node = node.parent
        depth += 1
    return depth


# ---- building the document ----------------------------------------------------------------
def build_meta(scene, path):
    meta = {"schema": SCHEMA, "kind": "fbx"}
    warnings = []

    info = meta["file"] = {"name": os.path.basename(path)}
    try:
        _put(info, "size", os.path.getsize(path))
    except OSError:
        pass
    md = scene.metadata
    info["encoding"] = "ascii" if md.ascii else "binary"
    _put(info, "version", int(md.version) if md.version else None)
    if md.big_endian:
        info["big_endian"] = True
    _put(meta, "creator", md.creator or None)

    st = scene.settings
    scene_info = meta["scene"] = {}
    up, front, right = axis_name(st.axes.up), axis_name(st.axes.front), axis_name(st.axes.right)
    _put(scene_info, "up_axis", up)
    _put(scene_info, "front_axis", front)
    _put(scene_info, "right_axis", right)
    if up:
        scene_info["y_up"] = up == "+Y"
        scene_info["z_up"] = up == "+Z"
    unit = float(st.unit_meters)
    scene_info["unit_meters"] = unit
    _put(scene_info, "unit", unit_name(unit))
    original_up = axis_name(st.original_axis_up) if int(st.original_axis_up) in AXES else None
    if original_up and original_up != up:
        scene_info["original_up_axis"] = original_up
    original_unit = float(st.original_unit_meters or 0)
    if original_unit and not math.isclose(original_unit, unit, rel_tol=1e-9):
        scene_info["original_unit_meters"] = original_unit

    nodes = list(scene.nodes)
    meshes = list(scene.meshes)
    bone_nodes = [n for n in nodes if getattr(n, "bone", None) is not None]
    counts = meta["counts"] = {
        "nodes": len(nodes), "meshes": len(meshes),
        "vertices": sum(m.num_vertices for m in meshes),
        "faces": sum(m.num_faces for m in meshes),
        "triangles": sum(m.num_triangles for m in meshes),
        "materials": _count(scene.materials), "textures": _count(scene.textures),
        "bones": _count(scene.bones), "skins": _count(scene.skin_deformers),
        "blend_shapes": _count(scene.blend_shapes), "cameras": _count(scene.cameras),
        "lights": _count(scene.lights), "takes": _count(scene.anim_stacks),
    }

    mesh_list = []
    for node in nodes:
        mesh = getattr(node, "mesh", None)
        if mesh is None:
            continue
        entry = {"name": mesh.name or node.name or "", "vertices": mesh.num_vertices,
                 "faces": mesh.num_faces, "triangles": mesh.num_triangles,
                 "materials": _count(mesh.materials),
                 "has_uvs": mesh.vertex_uvs is not None, "has_normals": mesh.vertex_normals is not None}
        mesh_list.append(entry)
    if mesh_list:
        meta["meshes"] = mesh_list

    texture_list = []
    for tex in scene.textures:
        entry = {}
        _put(entry, "name", tex.name)
        _put(entry, "path", tex.filename)
        _put(entry, "relative_path", tex.relative_filename if tex.relative_filename != tex.filename else None)
        _put(entry, "absolute_path", tex.absolute_filename if tex.absolute_filename != tex.filename else None)
        entry["embedded"] = bool(tex.content)
        texture_list.append(entry)
    if texture_list:
        meta["textures"] = texture_list

    if bone_nodes:
        roots = [n.name for n in bone_nodes
                 if not (n.parent is not None and getattr(n.parent, "bone", None) is not None)]
        meta["skeleton"] = {"bones": counts["bones"], "roots": roots,
                            "depth": max(_depth(n) for n in bone_nodes)}

    rate, drop, mode_name = frame_rate(int(st.time_mode), st.frames_per_second)
    protocol = int(st.time_protocol)
    with_text = PROTOCOLS.get(protocol, "default") != "frame count"
    tc = meta["timecode"] = {"mode": mode_name, "frame_rate": round(float(rate), 6),
                             "frame_rate_text": "%d/%d" % (rate.numerator, rate.denominator),
                             "drop_frame": drop, "protocol": PROTOCOLS.get(protocol, "protocol %d" % protocol)}

    takes = {}
    starts, ends = [], []
    for index, stack in enumerate(scene.anim_stacks):
        name = stack.name or "take %d" % (index + 1)
        key, n = name, 1
        while key in takes:
            n += 1
            key = "%s (%d)" % (name, n)
        if key != name:
            warnings.append("take name '%s' is used more than once; later ones are listed as '%s (2)', ..." % (name, name))
        begin, end = float(stack.time_begin), float(stack.time_end)
        starts.append(begin)
        ends.append(end)
        takes[key] = {"start": time_point(begin, rate, drop, with_text),
                      "end": time_point(end, rate, drop, with_text),
                      "duration": round(end - begin, 6),
                      "frames": int(round((end - begin) * float(rate)))}
    if takes:
        tc["start"] = time_point(min(starts), rate, drop, with_text)
        tc["end"] = time_point(max(ends), rate, drop, with_text)
    meta["takes"] = takes
    meta["warnings"] = sorted(set(warnings))
    return meta


# ---- reading files ---------------------------------------------------------------------------
def unavailable():
    """'' when FBX files can be read here, else a message for the user."""
    return "" if importlib.util.find_spec("ufbx") is not None else BUILTIN_MESSAGE


class _TempCopy:
    """Context manager for a scene loaded from a temporary ASCII-named copy of a file."""

    def __init__(self, folder, scene_cm):
        self._folder, self._cm = folder, scene_cm

    def __enter__(self):
        return self._cm.__enter__()

    def __exit__(self, *exc):
        try:
            return self._cm.__exit__(*exc)
        finally:
            shutil.rmtree(self._folder, ignore_errors=True)


def load(path):
    """Open `path` with ufbx.  Returns a context manager that gives the scene.

    pyufbx can only open files by name (its load_memory() is a stub), and the C library may
    not open every non-ASCII name; those are retried from a temporary copy."""
    if unavailable():
        raise FbxError(BUILTIN_MESSAGE)
    import ufbx
    if not os.path.isfile(path):
        raise FbxError("file not found: %s" % path)
    try:
        return ufbx.load_file(path)
    except Exception as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        if path.isascii():
            raise FbxError("cannot read as FBX: %s" % first)
        folder = tempfile.mkdtemp(prefix="peelfbx")
        try:
            copy = os.path.join(folder, "scene.fbx")
            shutil.copyfile(path, copy)
            return _TempCopy(folder, ufbx.load_file(copy))
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise FbxError("cannot read as FBX: %s" % first)


def load_bytes(data):
    """Like load(), for FBX bytes (the startup self-check uses this): written to a temporary
    file first."""
    folder = tempfile.mkdtemp(prefix="peelfbx")
    copy = os.path.join(folder, "scene.fbx")
    try:
        with open(copy, "wb") as f:
            f.write(data)
        return _TempCopy(folder, load(copy))
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def extract(path, dest):
    """Read `path`, write the `.meta` JSON to `dest`, return the dict."""
    try:
        with load(path) as scene:
            meta = build_meta(scene, path)
    except FbxError:
        raise
    except Exception as exc:
        raise FbxError("cannot read as FBX: %s" % (str(exc).strip().splitlines() or [type(exc).__name__])[0])
    mediainfo.write_meta(meta, dest)
    return meta
