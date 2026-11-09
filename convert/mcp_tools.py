"""The tools an AI agent can call over MCP (see mcp_server.py for the protocol).

Pure Python (no PeelCore, no Qt), so they run in the app, in `python -m convert.mcp_server`,
and in tests.  Every tool is a function taking the arguments dict and returning a
JSON-friendly value; it raises ToolError for a problem the agent should read and fix.

What an agent may and may not do is decided here, not in the prompt:

    * it writes only JSON definitions (arguments for a tool), never code
    * only tools listed in tools.json (ffmpeg) can be named
    * files go to  <user data>/userPython/conversions/ai.<id>.json  and nowhere else
    * built-in conversions and the person's own hand-written ones cannot be replaced
    * tests run in a temporary folder; the person's files are only ever read
"""
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from . import agent_guide, tools, trial
from .conversion import (TOOL_FLAGS, ConversionRegistry, shipped_dir, user_dir)

AI_PREFIX = "ai."
_ID = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)+$")
_EXT = re.compile(r"^[a-z0-9]{1,10}$")
_PATHY = re.compile(r"^([A-Za-z]:[\\/]|[\\/]|~)|(^|[\\/])\.\.([\\/]|$)|^file:", re.I)
_PY_ID = re.compile(r"^\s*id\s*=\s*[\"']([^\"']+)[\"']", re.M)
_ALLOWED_FIELDS = ("id", "label", "group", "tool", "inputs", "output_ext", "command")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_lock = threading.RLock()          # one save/delete/test at a time
_tested = {}                       # fingerprint -> summary of the passing test, this session only


class ToolError(Exception):
    """A problem the agent can act on; the message goes back to it as the tool's result."""


# ---- reading what exists -----------------------------------------------------------------
def _json_registry(folder):
    reg = ConversionRegistry()
    folder = Path(folder)
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            reg._load_json(path, {})     # JSON only: never execute a .py from this thread
    return reg


def _py_ids(folder):
    ids = set()
    folder = Path(folder)
    if folder.is_dir():
        for path in folder.glob("*.py"):
            try:
                ids.update(_PY_ID.findall(path.read_text(encoding="utf-8")))
            except OSError:
                pass
    return ids


def shipped_ids():
    return set(_json_registry(shipped_dir()).all_ids()) | _py_ids(shipped_dir())


def _user_hand_ids():
    """Ids from the person's own files (anything not made by an agent)."""
    ids = _py_ids(user_dir())
    folder = user_dir()
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            if path.name.startswith(AI_PREFIX):
                continue
            reg = ConversionRegistry()
            reg._load_json(path, {})
            ids.update(reg.all_ids())
    return ids


def ai_path(conversion_id):
    return user_dir() / (AI_PREFIX + conversion_id + ".json")


def ai_conversions():
    """The conversions agents saved: [{id, label, group, path}], by label."""
    out = []
    folder = user_dir()
    if folder.is_dir():
        for path in sorted(folder.glob(AI_PREFIX + "*.json")):
            reg = ConversionRegistry()
            reg._load_json(path, {})
            for conv in reg.all():
                out.append({"id": conv.id, "label": conv.label, "group": conv.group, "path": str(path)})
    return sorted(out, key=lambda c: c["label"].lower())


def _summary(conv, origin):
    return {"id": conv.id, "label": conv.label, "group": conv.group, "tool": conv.tool,
            "inputs": list(conv.inputs), "output_ext": conv.output_ext, "command": list(conv.command),
            "origin": origin}


def _all_conversions():
    shipped = _json_registry(shipped_dir())
    user = _json_registry(user_dir())
    ai_ids = {c["id"] for c in ai_conversions()}
    items = {c.id: _summary(c, "built-in") for c in shipped.all()}
    for c in user.all():
        items[c.id] = _summary(c, "made by an AI agent" if c.id in ai_ids else "written by the person")
    return sorted(items.values(), key=lambda c: (c["group"].lower(), c["label"].lower()))


# ---- tools --------------------------------------------------------------------------------
def get_authoring_guide(args):
    return agent_guide.GUIDE


def list_conversions(args):
    query = str(args.get("query") or "").lower()
    group = str(args.get("group") or "").lower()
    items = [c for c in _all_conversions()
             if (not group or c["group"].lower() == group)
             and (not query or query in (c["id"] + " " + c["label"] + " " + " ".join(c["inputs"])
                                         + " " + c["output_ext"]).lower())]
    return {"count": len(items), "conversions": items,
            "note": "Conversions written in code (for example metadata extraction) are not listed."}


def get_conversion(args):
    wanted = str(args.get("id") or "")
    for c in _all_conversions():
        if c["id"] == wanted:
            return c
    raise ToolError("no conversion with id %r (try list_conversions)" % wanted)


def list_tools(args):
    out = []
    for name, entry in sorted(tools.manifest().items()):
        path = tools.find(name)
        out.append({"name": name, "installed": bool(path), "path": path or "",
                    "usable_in_conversions": not entry.get("bundled_with"),
                    "note": ("ships inside %s; use for inspecting files, not as a conversion tool"
                             % entry["bundled_with"]) if entry.get("bundled_with") else ""})
    return out


_CAPS = {"encoders": "-encoders", "decoders": "-decoders", "muxers": "-muxers", "demuxers": "-demuxers",
         "filters": "-filters", "formats": "-formats", "pix_fmts": "-pix_fmts", "version": "-version"}


def tool_capabilities(args):
    tool = str(args.get("tool") or "ffmpeg")
    what = str(args.get("what") or "")
    if tool != "ffmpeg":
        raise ToolError("only ffmpeg can be inspected for now")
    if what not in _CAPS:
        raise ToolError("what must be one of: " + ", ".join(sorted(_CAPS)))
    exe = tools.find("ffmpeg")
    if not exe:
        raise ToolError("ffmpeg is not installed yet; ask the person to open PeelConvert and let it install ffmpeg")
    try:
        proc = subprocess.run([exe, "-hide_banner", _CAPS[what]], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
                              creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError("could not run ffmpeg: %s" % exc)
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    contains = str(args.get("contains") or "").lower()
    if contains:
        lines = [l for l in lines if contains in l.lower()]
    limit = max(1, min(int(args.get("limit") or 60), 300))
    shown = lines[:limit]
    return {"what": what, "contains": contains, "matches": len(lines),
            "lines": shown, "truncated": len(lines) > len(shown)}


# ---- validating and saving ----------------------------------------------------------------
def _definition(args, key="definition"):
    value = args.get(key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ToolError("%s is not valid JSON: %s" % (key, exc))
    if not isinstance(value, dict):
        raise ToolError("%s must be a JSON object like the example in get_authoring_guide" % key)
    return value


def _normalise(entry):
    entry = dict(entry)
    for key in ("id", "label", "group", "tool", "output_ext"):
        if isinstance(entry.get(key), str):
            entry[key] = entry[key].strip()
    if isinstance(entry.get("id"), str):
        entry["id"] = entry["id"].lower()
    if isinstance(entry.get("output_ext"), str):
        entry["output_ext"] = entry["output_ext"].lower().lstrip(".")
    if isinstance(entry.get("inputs"), list):
        entry["inputs"] = [e.lower().lstrip(".") if isinstance(e, str) else e for e in entry["inputs"]]
    entry.setdefault("tool", "ffmpeg")
    return entry


def check_definition(entry):
    """-> (problems, warnings, normalised entry, Conversion or None)."""
    problems, warnings = [], []
    entry = _normalise(entry)
    for key in sorted(set(entry) - set(_ALLOWED_FIELDS)):
        problems.append("field %r is not allowed (allowed: %s)" % (key, ", ".join(_ALLOWED_FIELDS)))
    if problems:
        return problems, warnings, entry, None
    reg = ConversionRegistry()
    reg._from_json(entry, "definition", {})
    problems += [p.replace("definition: ", "").replace(" (definition)", "") for p in reg.problems]
    conv = reg.get(entry.get("id")) if not problems else None
    cid = entry.get("id")
    if isinstance(cid, str) and not _ID.match(cid):
        problems.append("id %r should look like 'category.what', lowercase letters, digits and hyphens "
                        "(example: video.to-gif)" % cid)
    if conv is None:
        return problems, warnings, entry, None
    usable = {n for n, e in tools.manifest().items() if not e.get("bundled_with")}
    if conv.tool not in usable:
        problems.append("tool %r is not available to conversions; use one of: %s"
                        % (conv.tool, ", ".join(sorted(usable)) or "(none)"))
    if not _EXT.match(conv.output_ext):
        problems.append("output_ext %r must be a plain extension like 'mp3'" % conv.output_ext)
    for ext in conv.inputs:
        if ext != "*" and not _EXT.match(ext):
            problems.append("input extension %r must be a plain extension like 'wav'" % ext)
    if len(conv.label) > 80:
        problems.append("label is too long (80 characters at most)")
    for item in conv.command:
        if item in TOOL_FLAGS:
            problems.append("command must not contain %r: PeelConvert adds that itself" % item)
        elif _PATHY.search(item.replace("{input}", "").replace("{output}", "")):
            problems.append("command item %r looks like a file path; use only {input} and {output}" % item)
    if conv.command.count("{output}") != 1 and "{output}" in conv.command:
        problems.append("{output} must appear exactly once")
    if conv.tool == "ffmpeg" and "-i" not in conv.command:
        warnings.append("no '-i' in command: ffmpeg needs '-i', '{input}' to read the file")
    if cid in shipped_ids():
        problems.append("id %r belongs to a built-in conversion and cannot be replaced; pick another id" % cid)
    elif cid in _user_hand_ids():
        problems.append("id %r belongs to a conversion the person wrote by hand; pick another id" % cid)
    if tools.find(conv.tool) is None:
        warnings.append("%s is not installed on this computer yet, so it cannot be tested or used until it is"
                        % conv.tool)
    return problems, warnings, entry, conv


def _fingerprint(entry):
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode("utf-8")).hexdigest()


def validate_conversion(args):
    problems, warnings, entry, _ = check_definition(_definition(args))
    return {"ok": not problems, "problems": problems, "warnings": warnings}


def test_conversion(args):
    warnings = []
    if args.get("id") and not args.get("definition"):
        wanted = str(args["id"])
        reg = _json_registry(shipped_dir())
        reg2 = _json_registry(user_dir())
        conv = reg2.get(wanted) or reg.get(wanted)
        if conv is None:
            raise ToolError("no JSON conversion with id %r" % wanted)
        entry = None
    else:
        problems, warnings, entry, conv = check_definition(_definition(args))
        if problems:
            return {"ok": False, "stage": "validation", "problems": problems, "warnings": warnings}
    timeout = max(5.0, min(float(args.get("timeout_seconds") or 60), 300.0))
    given = args.get("input_path")
    with _lock, tempfile.TemporaryDirectory(prefix="peelconvert-sample-") as sample_dir:
        try:
            if given:
                source = str(given)
                if not conv.accepts(source):
                    warnings.append("input_path does not have one of the extensions this conversion accepts")
            else:
                source, extra = trial.make_sample(conv, sample_dir)
                warnings += extra
            result = trial.run_trial(conv, source, timeout)
        except trial.TrialError as exc:
            raise ToolError(str(exc))
    result["warnings"] = warnings
    if entry is not None and result.get("ok"):
        with _lock:
            _tested[_fingerprint(entry)] = _test_summary(result)
    result["next"] = ("Works. Call save_conversion with the same definition." if result.get("ok")
                      else "Failed. Read 'messages', change one thing, and test again.")
    return result


def _test_summary(result):
    out = result.get("output") or {}
    return {"seconds": result.get("seconds"), "output_bytes": out.get("bytes"),
            "output_name": out.get("name"), "streams": out.get("streams", []),
            "container": out.get("container")}


def propose_conversion(args):
    """For the in-app agent only (never offered over MCP): check a definition that has passed
    test_conversion and hand it back for the person to approve.  Writes nothing."""
    problems, warnings, entry, conv = check_definition(_definition(args))
    if problems:
        raise ToolError("cannot propose yet; fix these first: " + "; ".join(problems))
    with _lock:
        tested = _tested.get(_fingerprint(entry))
    if tested is None:
        raise ToolError("this exact definition has not passed test_conversion yet; test it first, "
                        "and propose only the definition that passed")
    out = {key: entry[key] for key in _ALLOWED_FIELDS if key in entry}
    if not out.get("inputs"):
        out.pop("inputs", None)
    return {"proposed": True, "definition": out, "test": tested, "warnings": warnings,
            "replaces": ai_path(entry["id"]).is_file(),
            "note": "Shown to the person, who will choose Add or Discard. Do not say it is saved."}


def save_conversion(args):
    problems, warnings, entry, conv = check_definition(_definition(args))
    if problems:
        raise ToolError("not saved; fix these first: " + "; ".join(problems))
    with _lock:
        path = ai_path(entry["id"])
        if path.exists() and not args.get("overwrite"):
            raise ToolError("you already saved %r; pass overwrite=true to replace it" % entry["id"])
        out = {key: entry[key] for key in _ALLOWED_FIELDS if key in entry}
        if not out.get("inputs"):
            out.pop("inputs", None)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
        tested = _fingerprint(entry) in _tested
    if not tested:
        warnings.append("this exact definition was not tested successfully in this session; "
                        "run test_conversion to be sure it works")
    return {"saved": True, "id": entry["id"], "label": entry["label"], "file": str(path),
            "warnings": warnings,
            "tell_the_person": "'%s' is now in PeelConvert's operation list under '%s' (press Reload "
                               "if it has not appeared after a few seconds)." % (entry["label"], entry["group"])}


def delete_conversion(args):
    wanted = str(args.get("id") or "")
    with _lock:
        path = ai_path(wanted)
        if not _ID.match(wanted) or not path.is_file():
            raise ToolError("%r is not a conversion made by an agent; only those can be deleted "
                            "(see list_conversions, origin 'made by an AI agent')" % wanted)
        path.unlink()
    return {"deleted": wanted}


# ---- registry ------------------------------------------------------------------------------
def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


_DEF = {"type": "object", "description": "A conversion: id, label, group, tool, inputs, output_ext, command "
        "(see get_authoring_guide).", "properties": {
            "id": {"type": "string"}, "label": {"type": "string"}, "group": {"type": "string"},
            "tool": {"type": "string"}, "inputs": {"type": "array", "items": {"type": "string"}},
            "output_ext": {"type": "string"}, "command": {"type": "array", "items": {"type": "string"}}}}

TOOLS = [
    {"name": "get_authoring_guide", "fn": get_authoring_guide, "readOnly": True,
     "description": "READ THIS FIRST. How to write a PeelConvert conversion: JSON format, rules for the "
                    "command, ffmpeg tips and the workflow.",
     "schema": _obj({})},
    {"name": "list_conversions", "fn": list_conversions, "readOnly": True,
     "description": "List the conversions the app already has (id, label, group, inputs, output, command). "
                    "Use it to reuse group names, avoid duplicates and copy the style.",
     "schema": _obj({"query": {"type": "string", "description": "optional text to look for"},
                     "group": {"type": "string", "description": "optional group name, e.g. Audio"}})},
    {"name": "get_conversion", "fn": get_conversion, "readOnly": True,
     "description": "One conversion's full definition by id.",
     "schema": _obj({"id": {"type": "string"}}, ["id"])},
    {"name": "list_tools", "fn": list_tools, "readOnly": True,
     "description": "The programs PeelConvert can run (ffmpeg...) and whether they are installed.",
     "schema": _obj({})},
    {"name": "tool_capabilities", "fn": tool_capabilities, "readOnly": True,
     "description": "Ask the installed ffmpeg what it supports, so you do not use a codec it lacks. "
                    "Example: what='encoders', contains='opus'.",
     "schema": _obj({"tool": {"type": "string", "enum": ["ffmpeg"]},
                     "what": {"type": "string", "enum": sorted(_CAPS)},
                     "contains": {"type": "string", "description": "only lines containing this text"},
                     "limit": {"type": "integer", "description": "max lines (default 60)"}}, ["what"])},
    {"name": "validate_conversion", "fn": validate_conversion, "readOnly": True,
     "description": "Check a draft definition without running or saving it. Returns problems and warnings.",
     "schema": _obj({"definition": _DEF}, ["definition"])},
    {"name": "test_conversion", "fn": test_conversion, "readOnly": True,
     "description": "Run a draft (or an existing conversion by id) on a generated sample file in a temporary "
                    "folder, and report the exit code, ffmpeg's last messages and what the output is. "
                    "Nothing is saved and no user file is changed. Do this until it works, then save.",
     "schema": _obj({"definition": _DEF, "id": {"type": "string", "description": "test an existing JSON conversion instead"},
                     "input_path": {"type": "string", "description": "optional real file to use as input (only read)"},
                     "timeout_seconds": {"type": "number"}})},
    {"name": "save_conversion", "fn": save_conversion, "readOnly": False,
     "description": "Save a conversion so it appears in PeelConvert's list. Saved on this computer only. "
                    "Use overwrite=true to replace one you saved before.",
     "schema": _obj({"definition": _DEF, "overwrite": {"type": "boolean"}}, ["definition"])},
    {"name": "delete_conversion", "fn": delete_conversion, "readOnly": False,
     "description": "Delete a conversion that an agent saved earlier (never built-in or hand-written ones).",
     "schema": _obj({"id": {"type": "string"}}, ["id"])},
]

BY_NAME = {t["name"]: t for t in TOOLS}

# What the in-app agent may call: everything that only reads or tests, plus propose_conversion.
# It has no save or delete: only the person's Add button saves.
PROPOSE_TOOL = {"name": "propose_conversion", "fn": propose_conversion, "readOnly": True,
                "description": "Show the finished conversion to the person for approval. Call it once, with "
                               "exactly the definition that just passed test_conversion. It does not save; "
                               "the person chooses Add or Discard.",
                "schema": _obj({"definition": _DEF}, ["definition"])}
AGENT_TOOLS = [t for t in TOOLS if t["readOnly"]] + [PROPOSE_TOOL]
AGENT_BY_NAME = {t["name"]: t for t in AGENT_TOOLS}
