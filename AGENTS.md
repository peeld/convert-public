# AGENTS.md — PeelConvert: editing and adding operations

This file is for coding agents changing what PeelConvert can do: adding a
conversion, changing an existing one, adding a tool other than ffmpeg, or adding
a new kind of Op. Read it before you touch `python/convert/`.

Build, packaging and release are covered in `README.md`; the work framework
(Op, Pipeline, executor) is designed in `Work Framework.md` and documented as
built in `core/cpp/work/README.md`. This file covers only the conversion system
that sits on top of them.

> **Reading this in an installed copy?** It ships next to `PeelConvert.exe` as
> `AGENTS.md` and describes the source tree. In an install, the source
> `python/` folder is `scripts/`, there is no C++ source or `build.py`, and
> the install folder is usually read-only and replaced on update. Add or
> override things in the user folders instead, which need no rebuild and
> survive updates:
>
> | What | Where |
> |---|---|
> | conversions (`*.json`, `*.py`) | `%APPDATA%\PeelConvert\conversions\` |
> | tool definitions / downloads | `%APPDATA%\PeelConvert\tools.json` |
> | path to a tool you already have | `%APPDATA%\PeelConvert\settings.json` → `{"tools": {"<name>": "C:/path/tool.exe"}}` |
>
> Then press **Reload** in the app. Recipe A (§4) and the `.py` part of §4.1 work
> this way. Recipes B (§5, until its refactor has shipped) and C (§6), and any
> C++ change, need the source repository.

---

## 1. The big picture

PeelConvert is a Qt/C++ app with an embedded Python. The split is strict:

| Side | Owns | Knows nothing about |
|---|---|---|
| C++ (`src/`) | the window, the queue table, the `work::Pipeline`, the `work::StepExecutor` that runs it, the Op info window | conversions, tools, ffmpeg, output-path rules |
| Python (`python/convert/`) | conversion definitions, tool lookup and download, output-path rules, the Op subclasses that do the work | widgets (it only calls the panel's public methods) |

The C++ panel emits signals ("files were added", "Start was clicked"); a Python
`Controller` listens, creates Ops and adds them to the panel's Pipeline; the
executor (driven by the Qt event loop) calls each Op's `start()`; the Op runs
the tool on a worker thread and reports progress, logs and its final state back
through thread-safe Op methods; the table follows the Pipeline automatically.

```
 ConvertPanel (C++)                      Controller (python/convert/app.py)
 ───────────────────                     ──────────────────────────────────
 filesAdded(paths, conversionId) ──────► add_files: one ConvertOp per file,
                                          pipeline.addOp(op)
 outputDirChanged(text) ───────────────► assign_outputs: recompute "output"
 startClicked(outputDir) ──────────────► start: create folders, panel.run()
 installToolRequested() ───────────────► install_tool: queue InstallToolOp
 reloadRequested() ────────────────────► reload: re-scan conversion folders
 queueIdle() ──────────────────────────► queue_idle: revalidate, refresh notice

 work::StepExecutor ── op.start(inputs, upstream) ──► ConvertOp / InstallToolOp
                                                      (python/convert/ops.py)
 table / log / info window ◄── setProgress, setStatus, log, complete/fail/acknowledgeCancel
```

### 1.1 How Python gets loaded

1. `src/main.cpp` creates `MainWindow` (which contains a `ConvertPanel`) and calls
   `appPythonStart()` in `src/appPython.cpp`.
2. `appPythonStart()` registers two built-in modules, **`PeelCore` first**, then
   `convertapp` (it uses PeelCore's types), starts Python, and calls
   `convert_startup.install()`.
3. `python/convert_startup.py: install()` builds `convert.app.Controller(win.convertPanel())`
   and keeps it in a module global so its signal connections live as long as the app.
4. `PeelConvert --python-check` calls `convert_startup.check()`, which imports
   every PySide6 module, loads the conversions, and fails if any shipped
   definition is invalid. `build.py package` runs this check on the installed app.

Where the `python/` folder comes from:

- **Dev build** (`build.py run`): the source folder `python/` itself is on `sys.path`
  (`PEEL_APP_PYTHON_DIR`). Edit a `.py` file and restart the app: no rebuild needed.
- **Staged / installed** (`build.py stage`, `run --staged`, the installer): `python/` is
  copied to `<app>/scripts/`. Re-run `stage` after Python changes.
- The **Reload** button re-reads conversion definitions (JSON and `.py` files in the
  conversion folders) but does **not** reload the `convert` package modules. Changes to
  `ops.py`, `app.py`, `tools.py` etc. need an app restart.

### 1.2 The two built-in Python modules

| Module | Generated from | What Python uses |
|---|---|---|
| `PeelCore` (`from PeelCore import work`) | `core/bindings/peelcore.{h,xml}` via `peel_add_core_python()` | `work.Op` (subclass it), `work.Params`, `work.OpState`, `work.LogStream`, `work.Pipeline`, `work.OpRegistry` |
| `convertapp` | `src/bindings.h` + `src/bindings.xml` via `peel_add_python_bindings()` | `convertapp.mainWindow()`, `MainWindow.convertPanel()`, every public method and signal of `ConvertPanel` |

Both are compiled into the exe. Neither can be imported by a plain `python`
outside the app, which is why the tests use a fake PeelCore (§7).

---

## 2. The Python package, file by file

All paths are under `python/`.

| File | Role | Imports PeelCore? |
|---|---|---|
| `convert_startup.py` | Entry points called from C++: `install()` and `check()`/`report()`. | yes (in `check`) |
| `convert/__init__.py` | Re-exports `Conversion`, `Job`, `register`, `registry`, `user_dir`. Only imports `conversion`, so conversion files can `from convert import ...` without PeelCore. | no |
| `convert/conversion.py` | `Conversion` base class, `Job`, the `ConversionRegistry` (JSON and `.py` loading), folder locations. | no |
| `convert/conversions/*.json` | Shipped conversion definitions (`ffmpeg.json` today). Any `.py` here is loaded too. | — |
| `convert/paths.py` | Output-directory rules and the temp-file name. Pure functions, testable with `ntpath`/`posixpath`. | no |
| `convert/tools.py` + `tools.json` | Find a tool's executable; download, verify (SHA-256) and unpack it. | no |
| `convert/runner.py` | Global slot limit: how many conversion processes run at once (default 1). | no |
| `convert/ops.py` | `ConvertOp` and `InstallToolOp`, the `work.Op` subclasses that do the work. | yes |
| `convert/app.py` | `Controller`: wires the panel's signals to the registry, paths, tools and Ops. | yes |
| `convert/check.py`, `__main__.py` | `python -m convert.check [folder ...]`: load and validate definitions, exit 1 on problems. | no |
| `tests/test_convert.py`, `tests/fakecore/` | Unit tests, with a Python fake of `PeelCore.work`. | fake |

Keep that import boundary: **`conversion`, `paths`, `tools`, `runner` must never
import PeelCore, Qt or `convertapp`.** That is what lets `convert.check` and most
tests run with a plain Python.

### 2.1 Conversions (`conversion.py`)

A **conversion** is one entry in the combo box: "turn files of these types into
this type, by running this tool with these arguments".

```python
class Conversion:
    id = ""            # unique, stable, e.g. "video.to-mp4". Stored in each Op's "conversion" property.
    label = ""         # shown in the operation combo box; sorted by label within a group
    group = ""         # REQUIRED, never empty: "Video", "Audio", "Image"... fills the group combo box
    tool = "ffmpeg"    # a tool name: key in tools.json / settings.json, or an exe on PATH
    inputs = ()        # accepted extensions without the dot; empty or "*" = anything
    output_ext = ""    # extension of the file written
    command = ()       # tool arguments with {input} and {output} placeholders
    args = None        # reserved for user-settable arguments; ignored today

    def accepts(self, path) -> bool          # extension check, case-insensitive
    def output_name(self, input_path) -> str  # "<stem>.<output_ext>"
    def build_command(self, job) -> list[str] # arguments only, NOT the executable
    def problems(self) -> list[str]           # why this definition is invalid; [] if fine
```

`Job` is what `build_command` receives:

```python
@dataclass
class Job:
    input: str        # full path of the source file
    output: str       # full path of the TEMP file to write (see 2.3), not the final name
    tool_path: str    # full path of the tool's executable
```

`problems()` requires `id`, `label`, a non-blank `group`, `tool`, `output_ext`, and — **only if
`build_command` is not overridden** — a non-empty `command` containing both
`{input}` and `{output}`.

**Loading order** (`registry.load_all`): the shipped folder
`python/convert/conversions/`, then the user folder
`%APPDATA%\PeelConvert\conversions\`. In each folder, every `*.py` is executed
first (sorted by name), then every `*.json`. A later definition with the same
`id` replaces an earlier one, so a user can override a shipped conversion.

**Errors never stop the app.** A broken file or invalid definition is skipped
and a line is appended to `registry.problems`; the panel shows the first one in
the yellow notice, and `--python-check` / `convert.check` fail on any.

**JSON format.** A file holds one object, a list of objects, or
`{"conversions": [...]}`. Allowed keys are exactly `JSON_FIELDS`:
`id, label, group, tool, inputs, output_ext, command, args, class`. **Any other key
rejects the entry** (this catches typos). `inputs` and `command` must be lists
of strings. `class` names a `Conversion` subclass, either
`"<stem of a .py in the same folder>.<ClassName>"` or a dotted import path; the
JSON fields are then set on an instance of that class.

**Python format.** A `.py` file in a conversions folder can define and register
a class:

```python
from convert import Conversion, register

@register
class Mp3(Conversion):
    id = "audio.to-mp3"
    label = "Audio to MP3"
    group = "Audio"
    inputs = ("wav", "flac")
    output_ext = "mp3"
    command = ("-i", "{input}", "-q:a", "2", "{output}")
```

The file is imported under the module name `convert_user_<stem>`. It is
re-executed on every Reload, so keep module-level code side-effect free.

### 2.2 Output paths (`paths.py`)

The Output field means:

| Field text | Output folder | Created? |
|---|---|---|
| blank | the source file's folder | — |
| a name without `/` or `\` | that subfolder of the source file's folder | silently, at Start |
| anything with `/` or `\` | that path (relative = relative to the source folder) | after a Yes/No question, at Start |

`output_path(conversion, source, out_text)` returns `(OutputDir, full_path)`
using `conversion.output_name()`. `check_output()` rejects an output equal to
its input (case-insensitive on Windows) or one that already exists. Two queued
files with the same output: the later becomes INVALID ("same output as ...") —
the Controller's `mark_conflicts()` does that through the `conflict` property.

### 2.3 Running a conversion (`ops.py: ConvertOp`)

Properties (all strings, set by the Controller):

| Property | Meaning |
|---|---|
| `conversion` | id of a registered Conversion |
| `input` | full path of the source |
| `output` | full path of the final file; empty until resolved |
| `conflict` | non-empty when another queued Op writes the same output |

Life of one ConvertOp:

1. **Created** in `Controller.add_files`, properties set, `pipeline.addOp(op)`.
   The Pipeline calls `validate()`; the Op is READY or INVALID.
2. **`validate()`** returns a list of human-readable problems (shown in the
   table's Error column): unknown conversion, missing input, extension not
   accepted, tool not found, output problems, conflict, no output yet.
   It is called again whenever a property changes, on Reload, and when the queue
   goes idle (a tool may have just been installed).
3. **`start(inputs, upstream)`** runs on the **Qt main thread with the GIL held**.
   It must return at once: it builds a `Job` with
   `output = paths.temp_name(output)` (`a.mp4` → `a.converting.mp4`, keeping the
   extension so the tool picks the right format) and starts a daemon thread.
4. **Worker thread (`_run`)**: waits for a slot in `runner` (status
   "Waiting for a free slot"; cancellable), then `_execute`:
   - argv = `[tool_path, "-nostdin", "-hide_banner", "-nostats", "-progress", "pipe:1", "-y"] + conversion.build_command(job)`
   - `subprocess.Popen` with `CREATE_NO_WINDOW`, stdin = DEVNULL.
   - stderr is read on a second thread: every line goes to the Op's stderr log,
     the last 12 are kept for the error message, and ffmpeg's `Duration:` line
     gives the total length.
   - stdout is parsed as ffmpeg `-progress` output (`out_time_us=...`) to call
     `setProgress()` and `setStatus("Converting: N%")`. **stdout lines are not logged.**
5. **`_finish`**: cancelled → delete the temp file, `acknowledgeCancel()`;
   non-zero exit → delete temp file, `fail("ffmpeg exited with code N: <last stderr lines>")`;
   final output appeared meanwhile → `fail("output exists")`;
   otherwise `os.replace(temp, output)` and `complete(Params{output: path})`.
6. Any exception in the worker → temp file removed, `fail(...)` (or
   `acknowledgeCancel()` if cancel was requested). **An Op must never be left RUNNING.**
7. **`onCancel()`** (called from the Cancel/Remove/Clear buttons) sets the cancel
   event and terminates the process if there is one.

### 2.4 Tools (`tools.py`, `tools.json`)

`tools.find(name)` returns the executable path or `None`, trying in order:

1. `%APPDATA%\PeelConvert\settings.json` → `{"tools": {"<name>": "C:/path/to/exe"}}`
2. `%APPDATA%\PeelConvert\tools\<name>\**\<exe>` (where downloads are unpacked)
3. `PATH` (`shutil.which`)

`<exe>` is the entry's `"exe"` (default: the tool name), with `.exe` tried first on Windows.

`tools.json` (shipped copy next to `tools.py`, merged with
`%APPDATA%\PeelConvert\tools.json`, user entries win per name):

```json
{
  "tools": {
    "<name>": {
      "exe": "<executable name without .exe>",
      "download": {
        "windows-x86_64": {
          "url": "https://.../archive.zip",
          "sha256": "<64 hex>"            // OR
          "sha256_url": "https://.../checksums.sha256",
          "sha256_name": "<file name in that list, default: last part of url>",
          "path": "*/bin/<exe>.exe",      // glob of the exe inside the zip
          "note": "free text: licence, why this build"
        }
      }
    }
  }
}
```

Rules enforced by `install_problems()` / `install()`:

- Platform keys come from `platform_key()`: `windows-x86_64`, `windows-arm64`,
  `linux-x86_64`, `macos-arm64`, ...
- **A download without `sha256` or `sha256_url` is refused.** Never add one.
- Only **`.zip`** archives are supported. Members that escape the target folder
  are refused. The archive is unpacked into a temp folder and moved into
  `tools/<name>` only after the checksum and the `path` glob both succeed.
- A tool with no usable download is still usable from PATH or settings.json;
  the notice then tells the user to install it themselves instead of showing
  the Install button.

The Controller is generic over tool names: on start-up and on the Install
button it queues an `InstallToolOp` for every tool that some registered
conversion uses, that `find()` cannot locate, and that has a valid download.

### 2.5 Concurrency (`runner.py`)

Every ConvertOp is independent (no Pipeline edges), so the executor starts all
READY Ops at once; each then blocks in `runner.acquire()` until a slot is free.
`runner.set_slots(n)` changes the limit (default 1, because ffmpeg uses every
core). The slot pool is shared by **all** conversions regardless of tool. Do not
serialise Ops with edges instead: one failure would mark every later Op Dead.

---

## 3. The C++ ↔ Python interface

Everything public on `ConvertPanel` (`src/convertpanel.h`) is callable from Python:

| Python call | Effect |
|---|---|
| `panel.pipeline()` | the `work.Pipeline` (C++-owned; never store Ops elsewhere as the source of truth) |
| `panel.outputDir()` / `setOutputDir(text)` | the Output field |
| `panel.setConversions(ids, labels, groups)` | fill the group and operation combo boxes (same-length lists; groups must be non-empty; the id comes back in `filesAdded`) |
| `panel.setNotice(text, installButton=False)` | yellow banner; empty text hides it |
| `panel.confirm(title, text)` | modal Yes/No, returns bool |
| `panel.run()` | unpause the executor; it pauses itself and emits `queueIdle` when nothing is left |
| `panel.cancelAll()`, `panel.running()` | |

Signals: `filesAdded(QStringList, QString)`, `startClicked(QString)`,
`outputDirChanged(QString)`, `reloadRequested()`, `installToolRequested()`,
`queueIdle()`.

The table, the per-row log and the Op info window (double-click) are generic:
they show any Op's name, type name, state, progress, status, validation problems,
properties, outputs and logs. **A new Op type or a new conversion needs no C++
change to be displayed.**

Op API used from Python (from `PeelCore.work.Op`):

| Method | Thread | Notes |
|---|---|---|
| `typeName()` | override | stable string, used to rebuild saved queues |
| `validate()` | override | list of problem strings; `[]` = READY |
| `start(inputs, upstream)` | override; main thread, GIL held | return quickly |
| `onCancel()` | override | stop the work; then call `acknowledgeCancel()` from the worker |
| `properties().value(name, default)` | any | |
| `setProperty(name, value)` | main thread | rejected while RUNNING; triggers revalidation |
| `revalidate()` | main thread | re-run `validate()` on a READY/INVALID Op |
| `complete(params)`, `fail(msg)`, `acknowledgeCancel()` | any | exactly one of these ends a run; if cancel was requested, cancel wins |
| `setProgress(0.0–1.0)`, `setStatus(text)` | any | runtime only, drives the progress bar |
| `log(work.LogStream.Stdout/Stderr, text)` | any | per-Op log, shown under the table |

Values in `Params` are bool, int, float or str only. Pass paths, not data.

### 3.1 When you need a C++ change

Only when Python needs a new UI capability (a new widget, signal or method):

1. Add the public method or signal to `ConvertPanel` (or `MainWindow`) in
   `src/convertpanel.{h,cpp}`. Use Qt value types (`QString`, `QStringList`, `bool`, `int`).
2. If it returns a pointer Python must not own, add a `<modify-function>` with
   `<define-ownership owner="c++"/>` in `src/bindings.xml` (see `pipeline()`).
3. A **new class** exposed to Python needs: an `<object-type>` in
   `src/bindings.xml`, its header included from `src/bindings.h`, its name in
   `CLASSES` and its header in `DEPENDS` of `peel_add_python_bindings` in
   `CMakeLists.txt`, and its sources in `peel_add_app(... SOURCES ...)`.
4. `python build.py build` regenerates the bindings. Then use it from
   `convert/app.py`.
5. Keep conversion knowledge out of C++. The panel stays generic.

---

## 4. Recipe A — a new conversion that uses ffmpeg

Usually JSON only, no code.

1. Pick the file: add to `python/convert/conversions/ffmpeg.json` to ship it, or
   create a new `*.json` in that folder for a separate group. (Users put theirs in
   `%APPDATA%\PeelConvert\conversions\`.)
2. Add an entry:
   ```json
   {
     "id": "video.to-mov-prores",
     "label": "Video to MOV (ProRes 422)",
     "tool": "ffmpeg",
     "inputs": ["mov", "mkv", "mp4"],
     "output_ext": "mov",
     "command": ["-i", "{input}", "-c:v", "prores_ks", "-profile:v", "2", "-c:a", "pcm_s16le", "{output}"]
   }
   ```
   - `id`: `<category>.<verb-or-target>`, lowercase, stable forever (saved in Op properties).
   - Do **not** add `-y`, `-nostdin`, `-hide_banner`, `-nostats` or `-progress`:
     `ConvertOp` adds them.
   - `{output}` is the temp file (`name.converting.mov`); ffmpeg infers the
     container from its extension, so `output_ext` must be a real extension. If the
     extension is ambiguous, add `-f <format>` before `{output}`.
   - Put `{input}` and `{output}` as whole arguments. Never build a shell string:
     there is no shell, each list item is one argv entry, so spaces in paths are safe.
   - Same-format conversions (e.g. `mp4` → `mp4`) are allowed; the path rules
     reject the case where the output would overwrite the input.
3. Check the codec exists in the **shipped ffmpeg build**: `tools.json` downloads the
   BtbN **LGPL** build, which has **no libx264/libx265** (and no other GPL
   libraries). A conversion relying on them works only with a user-supplied ffmpeg.
   Prefer LGPL-available encoders, or document the requirement in the label.
4. Validate: from `python/`, `python -m convert.check` (lists every conversion,
   exit 1 on any problem).
5. Run the tests (§7); `test_shipped_definitions_are_valid` covers shipped JSON.
6. Try it in the app: Reload, add a file, Start. Double-click the row to see the
   exact command line (first stdout log line, `run: ...`) and ffmpeg's stderr.

### 4.1 When JSON is not enough

Use a Python class when the arguments depend on the input (probing, choosing a
filter by extension, multiple inputs derived from the source, etc.):

```python
# python/convert/conversions/thumbnails.py
import os
from convert import Conversion

class Thumbnail(Conversion):
    def build_command(self, job):
        seek = "00:00:01" if os.path.getsize(job.input) > 1_000_000 else "0"
        return ["-ss", seek, "-i", job.input, "-frames:v", "1", "-q:v", "2", job.output]

    def accepts(self, path):                       # optional: anything beyond extensions
        return super().accepts(path) and os.path.getsize(path) > 0
```
```json
{ "id": "video.thumbnail", "label": "Video thumbnail (JPG)", "inputs": ["mp4", "mov"],
  "output_ext": "jpg", "class": "thumbnails.Thumbnail" }
```

Or register the class directly with `@register` and skip the JSON. Rules:

- `build_command` returns arguments only (not the exe) and must write to `job.output`.
- It runs on the worker thread: it may do slow I/O, but must not touch Qt.
- It must not raise for a valid input; an exception fails the Op with its message.
- Overriding `build_command` disables the `command`/`{input}`/`{output}` check, so test it.
- Need a new JSON key? Add it to `JSON_FIELDS`, give `Conversion` a default
  attribute, extend `_from_json` validation if it is not a plain value, and add
  tests. Unknown keys are otherwise rejected on purpose.

---

## 5. Recipe B — a conversion that uses a different tool

**Important:** today `ConvertOp` is ffmpeg-specific in several places. A
conversion with `"tool": "magick"` would load and validate, but would run
`magick -nostdin -hide_banner -nostats -progress pipe:1 -y ...` and fail. The
first non-ffmpeg tool therefore needs a small refactor of `ops.py` before its
definitions will work.

### 5.1 What is ffmpeg-specific today

| Where (`python/convert/ops.py`) | ffmpeg assumption |
|---|---|
| `ConvertOp._execute`: argv prefix | `-nostdin -hide_banner -nostats -progress pipe:1 -y` |
| `ConvertOp._execute`: stdout loop → `_progress` | stdout is `key=value` progress; other lines are dropped, not logged |
| `_read_stderr` / `_DURATION` | total length comes from a `Duration:` line on stderr |
| `_finish` | error text says "ffmpeg exited with code N" |
| `paths.temp_name` docstring | "where ffmpeg writes" (behaviour is generic) |

Everything else is already tool-agnostic: `Conversion.tool`, `tools.find`,
`tools.json`, `InstallToolOp`, the Controller's missing-tool notice and Install
button, validation ("<tool> is not installed"), the slot runner.

### 5.2 Refactor to make (once)

Move the tool-specific parts behind `Conversion` hooks, with defaults that
reproduce today's ffmpeg behaviour exactly, so `ffmpeg.json` keeps working
unchanged:

1. In `conversion.py`, add to `Conversion`:
   - `def base_args(self, job) -> list[str]` — arguments put before
     `build_command(job)`. Default: the current ffmpeg prefix when
     `self.tool == "ffmpeg"`, otherwise `[]`.
   - `def parse_stdout(self, line, state) -> float | None` and
     `def parse_stderr(self, line, state) -> float | None` — return a progress
     fraction 0..1 when the line reveals one, else `None`. `state` is a dict the
     Op keeps per run. Default for ffmpeg: today's `_DURATION` and `out_time_us`
     logic; default for other tools: `None`.
   - Optionally `def success(self, code, job) -> bool` (default `code == 0`) for
     tools that return non-zero on warnings, and `def temp_output(self, output)`
     (default `paths.temp_name(output)`) for tools that insist on their own
     output names.
   - Keep these out of `JSON_FIELDS` unless a JSON form is needed; a tool with
     special parsing gets a Python subclass (`"class": ...`).
2. In `ops.py`, make `_execute` call `conversion.base_args(job)`, feed each stdout
   and stderr line to the matching `parse_*` hook, call `setProgress`/`setStatus`
   when it returns a value, **log non-progress stdout lines** to the stdout log,
   and use `conversion.tool` in the failure message.
3. Run the existing tests unchanged — they cover the ffmpeg path (progress,
   status, temp-file rename, failure cleanup, cancel). Add tests for a second fake
   tool (see §7) with no progress output and with its own progress format.
4. Update `convert_startup.report()` if it should list every tool, not just ffmpeg.

### 5.3 Then, for each new tool

1. **Make it findable.** Add an entry to `python/convert/tools.json` with `exe`
   and, if it can be downloaded, a per-platform `download` with `url`, a
   `sha256` or `sha256_url`, and the `path` glob of the exe inside the zip.
   Record licence and source in `note`. Pin a versioned URL with a fixed
   `sha256` when the publisher offers one; use `sha256_url` only for rolling
   releases that publish a checksum list. If the publisher only ships `.7z`,
   `.tar.xz` or an installer, `install()` must be extended first (keep the
   checksum-before-unpack and safe-path rules), or ship no download and let the
   user set the path in settings.json.
2. **Write the conversions** with `"tool": "<name>"`, in a new
   `python/convert/conversions/<name>.json` (one file per tool keeps things tidy).
   `command` holds the tool's own arguments with `{input}` and `{output}`.
   The tool must:
   - read the input path and write exactly the path in `{output}` (the temp
     file, which keeps the final extension);
   - not prompt (stdin is closed) and not open a window;
   - exit 0 on success and non-zero on failure (or override `success`).
3. **Progress**: if the tool prints progress, subclass `Conversion`, override
   `parse_stdout`/`parse_stderr`, and point the JSON `class` at it. Without
   progress the bar stays empty and the status reads "Converting" until done.
4. **Concurrency**: if the tool is single-threaded, more slots may help, but the
   pool is global; changing `runner` to per-tool pools is a separate change.
5. Check: `python -m convert.check`, the unit tests, then the app with the tool
   (a) on PATH, (b) set in settings.json, (c) missing — the notice should say so
   and offer Install only if a download is defined.

Example (after the refactor):

```json
{
  "id": "image.to-webp-magick",
  "label": "Image to WebP (ImageMagick)",
  "tool": "magick",
  "inputs": ["png", "jpg", "jpeg", "tif", "tiff", "bmp"],
  "output_ext": "webp",
  "command": ["{input}", "-quality", "85", "{output}"]
}
```

---

## 6. Recipe C — a new kind of Op (not a file conversion)

For work that is not "one input file → one output file with a tool" (e.g. merging
several files, probing media info, cleaning the tools folder), write a new
`work.Op` subclass in `python/convert/ops.py`, following `InstallToolOp`:

```python
NEW_TYPE = "convert.something"

class SomethingOp(work.Op):
    def __init__(self, *args):            # (name) or (uuid, name) when rebuilt from a save
        super().__init__(*args)
        self._cancel = threading.Event()

    def typeName(self):
        return NEW_TYPE

    def validate(self):                   # cheap, no side effects, called often
        return [] if _prop(self, "input") else ["no input"]

    def start(self, inputs, upstream):    # main thread, GIL held: return at once
        self._cancel.clear()
        threading.Thread(target=self._run, daemon=True).start()

    def onCancel(self):
        self._cancel.set()                # and stop any child process

    def _run(self):
        try:
            ...                           # do the work; check self._cancel often
            if self._cancel.is_set():
                self.acknowledgeCancel()
                return
            out = work.Params()
            out.set("result", "...")
            self.complete(out)
        except Exception as exc:          # never leave the Op RUNNING
            self.log(work.LogStream.Stderr, str(exc))
            self.fail(str(exc))
```

Then:

1. Register it in `register_types()` so a saved queue can rebuild it.
2. Create and queue it from `convert/app.py` (`self.pipeline.addOp(op)`, then
   `self.panel.run()` if it should start immediately). If the Controller filters
   by class (`convert_ops()`, `installing()`), decide whether the new Op belongs
   in those lists.
3. If it depends on another Op's result, add an edge
   (`pipeline.addEdge(upstream.id(), op.id())`); its `start()` receives the
   upstream outputs keyed by upstream UUID in `upstream`.
4. If it needs a new button or signal, see §3.1.
5. If it calls a new `work.Op` method, add that method to the fake in
   `tests/fakecore/PeelCore/work.py` too.

Rules for every Op:

- Exactly one of `complete`, `fail`, `acknowledgeCancel` per run, on every path,
  including exceptions and cancel-before-launch.
- Never block in `start()` or `validate()`; never touch Qt widgets from a worker thread.
- Properties are configuration and are frozen while RUNNING; per-run data goes in outputs and logs.
- Clean up partial output on failure and cancel (write to a temp name, rename on success).

---

## 7. Testing

From `python/`:

```
python -m unittest discover -s tests -v     # unit tests: no build, no real ffmpeg
python -m convert.check                     # validate shipped + user conversion definitions
```

- `tests/fakecore/PeelCore/work.py` is a Python stand-in for the C++ `work`
  module (states, properties, revalidate, launch, complete/fail/cancel,
  progress, status, logs). Keep it in step with the real API you use.
- `OpTests` runs `ConvertOp` end to end against `FAKE_FFMPEG`, a Python script
  written to a temp folder and pointed to by a temp `settings.json` (`APPDATA` is
  redirected per test). Copy that pattern for a new tool: write a fake exe
  script that mimics its argv, output, progress format and exit codes, including
  a failure input and a slow input for cancel tests.
- Pure logic (`paths`, `tools.parse_checksums`, loading) is tested without Ops;
  put new pure helpers in non-PeelCore modules so they stay testable.

In the real app (after `python build.py build` when C++ changed):

```
python build.py run -- --python-check        # embedded Python, PeelCore, every shipped conversion
python build.py run                          # manual: add files, Start, double-click a row for logs
python build.py stage && python build.py run --staged -- --python-check   # what the installer ships
```

---

## 8. Checklist before you finish

- [ ] `python -m unittest discover -s tests -v` passes (from `python/`).
- [ ] `python -m convert.check` prints no `PROBLEM:` lines.
- [ ] New shipped tool downloads have a checksum and a `note` with the licence.
- [ ] Codecs/features used exist in the build the app downloads (LGPL ffmpeg: no x264/x265).
- [ ] No PeelCore/Qt import added to `conversion`, `paths`, `tools`, `runner`.
- [ ] Every new Op path ends in `complete`, `fail` or `acknowledgeCancel`, and removes temp output.
- [ ] C++ touched only for new UI capability; `bindings.xml`/`CMakeLists.txt` updated together.
- [ ] `--python-check` passes on a built app if C++ or startup code changed.
- [ ] Docstrings at the top of changed modules still describe them (they are the reference docs).
