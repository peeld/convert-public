"""Conversions: what the combo box offers, and how each one builds its command.

A conversion is declared in a JSON file, optionally with a Python class for
anything JSON cannot say, or defined by a Python class alone.  Both kinds are
loaded from the shipped `conversions` folder (next to this file) and then from
the user's `conversions` folder, so a later definition with the same id
replaces an earlier one.  A bad definition is skipped and reported in
`registry.problems`; it never stops the app.

JSON (one object, or a list of objects, per file):

    {
      "id": "video.to-mp4",
      "label": "Video to MP4 (H.264 + AAC)",
      "group": "Video",
      "tool": "ffmpeg",
      "inputs": ["mov", "mkv"],
      "output_ext": "mp4",
      "command": ["-i", "{input}", "-c:v", "libx264", "-c:a", "aac", "{output}"],
      "class": "my_file.MyConversion",
      "args": null
    }

`group` is required and must not be empty: it is the name the group combo box shows
("Video", "Audio", ...) and files the conversion under.  `class` is optional: "<stem of a .py file in the same folder>.<ClassName>" or a
dotted import path.  `args` is reserved for a later round and ignored.

Python:

    from convert import Conversion, register

    @register
    class Mp3(Conversion):
        id = "audio.to-mp3"
        label = "Audio to MP3"
        group = "Audio"
        inputs = ("wav", "flac")
        output_ext = "mp3"
        command = ("-i", "{input}", "-q:a", "2", "{output}")

Override `build_command(job)` (and `accepts`) when a list of arguments is not enough.
"""
import importlib
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

JSON_FIELDS = ("id", "label", "group", "tool", "inputs", "output_ext", "command", "args", "class")


@dataclass
class Job:
    """What build_command() is given.  `output` is the temporary file ffmpeg writes."""
    input: str
    output: str
    tool_path: str = ""


class Conversion:
    id = ""
    label = ""
    group = ""           # required, never empty: "Audio", "Video", ... (the group combo box)
    tool = "ffmpeg"
    inputs = ()          # accepted input extensions, no dot; empty or "*" = anything
    output_ext = ""
    command = ()         # tool arguments with {input} and {output}
    args = None          # reserved: arguments the user will be able to set

    def accepts(self, path):
        wanted = [e.lower().lstrip(".") for e in self.inputs]
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        return not wanted or "*" in wanted or ext in wanted

    def output_name(self, input_path):
        stem = os.path.splitext(os.path.basename(input_path))[0]
        return stem + "." + self.output_ext.lstrip(".")

    def build_command(self, job):
        """Arguments for the tool (not the executable itself)."""
        return [a.replace("{input}", job.input).replace("{output}", job.output) for a in self.command]

    def problems(self):
        out = []
        if not self.id:
            out.append("no id")
        if not self.label:
            out.append("no label")
        if not isinstance(self.group, str) or not self.group.strip():
            out.append("no group")
        if not self.tool:
            out.append("no tool")
        if not self.output_ext:
            out.append("no output_ext")
        if type(self).build_command is Conversion.build_command:
            if not self.command:
                out.append("no command")
            else:
                text = " ".join(str(a) for a in self.command)
                for token in ("{input}", "{output}"):
                    if token not in text:
                        out.append("command has no " + token)
        return out


class ConversionRegistry:
    def __init__(self):
        self._items = {}
        self.problems = []      # text lines for the user, newest last

    # ---- registering -------------------------------------------------------
    def register(self, conversion, source=""):
        """Add a Conversion instance or class.  Returns it, or None if it is not valid."""
        if isinstance(conversion, type):
            conversion = conversion()
        found = conversion.problems()
        if found:
            self.problems.append("%s: %s" % (source or conversion.id or "?", "; ".join(found)))
            return None
        self._items[conversion.id] = conversion
        return conversion

    def get(self, conversion_id):
        return self._items.get(conversion_id)

    def all(self):
        return sorted(self._items.values(), key=lambda c: (c.group.lower(), c.label.lower(), c.id))

    def clear(self):
        self._items.clear()
        self.problems = []

    # ---- loading -----------------------------------------------------------
    def load_dir(self, folder):
        folder = Path(folder)
        if not folder.is_dir():
            return
        modules = {}
        for path in sorted(folder.glob("*.py")):
            module = self._load_py(path)
            if module is not None:
                modules[path.stem] = module
        for path in sorted(folder.glob("*.json")):
            self._load_json(path, modules)

    def load_all(self, extra_dirs=()):
        """Shipped definitions, then the user's: later ones replace earlier ones."""
        self.clear()
        for folder in (shipped_dir(), *extra_dirs):
            self.load_dir(folder)

    def _load_py(self, path):
        name = "convert_user_" + path.stem
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)       # @register adds its classes
            return module
        except Exception as exc:
            self.problems.append("%s: %s: %s" % (path.name, type(exc).__name__, exc))
            return None

    def _load_json(self, path, modules):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.problems.append("%s: %s" % (path.name, exc))
            return
        if isinstance(data, dict) and "conversions" in data:
            data = data["conversions"]
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            self._from_json(entry, path.name, modules)

    def _from_json(self, entry, source, modules):
        if not isinstance(entry, dict):
            self.problems.append("%s: not an object" % source)
            return
        label = "%s (%s)" % (entry.get("id", "?"), source)
        unknown = sorted(set(entry) - set(JSON_FIELDS))
        if unknown:
            self.problems.append("%s: unknown field(s) %s" % (label, ", ".join(unknown)))
            return
        for field in ("inputs", "command"):
            value = entry.get(field)
            if value is not None and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
                self.problems.append("%s: %s must be a list of strings" % (label, field))
                return
        cls = Conversion
        if entry.get("class"):
            cls = self._resolve_class(entry["class"], modules)
            if cls is None:
                self.problems.append("%s: cannot find class %s" % (label, entry["class"]))
                return
        try:
            conv = cls()
        except Exception as exc:
            self.problems.append("%s: class %s failed: %s" % (label, entry["class"], exc))
            return
        for field in JSON_FIELDS:
            if field in entry and field != "class":
                value = entry[field]
                setattr(conv, field, tuple(value) if field in ("inputs", "command") else value)
        self.register(conv, label)

    @staticmethod
    def _resolve_class(dotted, modules):
        module_name, _, class_name = dotted.rpartition(".")
        if not module_name:
            return None
        module = modules.get(module_name)
        if module is None:
            try:
                module = importlib.import_module(module_name)
            except Exception:
                return None
        cls = getattr(module, class_name, None)
        return cls if isinstance(cls, type) and issubclass(cls, Conversion) else None


registry = ConversionRegistry()


def register(cls_or_instance):
    """Decorator (or call) that adds a Conversion to the registry."""
    registry.register(cls_or_instance, source=getattr(cls_or_instance, "__name__", ""))
    return cls_or_instance


# ---- where things live -----------------------------------------------------------
def shipped_dir():
    return Path(__file__).resolve().parent / "conversions"


def user_data_dir():
    """The same folder the C++ side keeps PeelConvert.ini in."""
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "PeelConvert"


def user_dir():
    return user_data_dir() / "conversions"
