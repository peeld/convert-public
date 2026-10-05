"""Output-path rules.  Pure functions: no Qt, no PeelCore, easy to test.

The output directory field means:
    blank                      the folder of each source file
    text with no / or \\        a subfolder of the source file's folder; created without asking
    text with a / or \\         a path (relative ones are relative to the source folder);
                               if it does not exist the user is asked before it is created

An output may never be the same path as its input, and may not already exist.

Every function takes `pm`, a path module (os.path by default, ntpath or
posixpath in tests), and `exists`, a callable, so the rules can be tested for
Windows paths on any machine.
"""
import os
import re
from dataclasses import dataclass

_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def has_separator(text):
    return "/" in text or "\\" in text


def is_absolute(text, pm=os.path):
    return bool(pm.isabs(text) or _DRIVE.match(text) or text.startswith("\\\\"))


@dataclass
class OutputDir:
    path: str            # the directory the file goes in
    create: bool         # it does not exist yet
    ask: bool            # ...and the user must agree before it is created


def resolve_dir(out_text, source, pm=os.path, exists=os.path.exists):
    """Where the converted file for `source` goes, per the rules above."""
    text = (out_text or "").strip()
    source_dir = pm.dirname(source)
    if not text:
        path = source_dir
    elif not has_separator(text):
        path = pm.join(source_dir, text)
    elif is_absolute(text, pm):
        path = text
    else:
        path = pm.join(source_dir, text)
    path = pm.normpath(path)
    create = not exists(path)
    return OutputDir(path, create, ask=create and has_separator(text))


def output_path(conversion, source, out_text, pm=os.path, exists=os.path.exists):
    """(OutputDir, full output path) for one file."""
    out_dir = resolve_dir(out_text, source, pm, exists)
    return out_dir, pm.join(out_dir.path, conversion.output_name(pm.basename(source)))


def same_path(a, b, pm=os.path):
    return pm.normcase(pm.normpath(a)) == pm.normcase(pm.normpath(b))


def check_output(source, output, pm=os.path, exists=os.path.exists):
    """Problems with writing `output` for `source`; empty if it is allowed."""
    if not output:
        return []
    if same_path(source, output, pm):
        return ["output would overwrite its input"]
    if exists(output):
        return ["output exists"]
    return []


def temp_name(output, pm=os.path):
    """Where ffmpeg writes before the rename: keeps the extension, so ffmpeg still
    picks the right container.  a.mp4 -> a.converting.mp4"""
    stem, ext = pm.splitext(output)
    return stem + ".converting" + ext
