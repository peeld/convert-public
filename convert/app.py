"""Connects the window (convertapp.ConvertPanel, C++) to conversions and Ops.

The panel owns the Pipeline, the executor and the table.  This controller
listens to the panel's signals and fills the Pipeline with Ops:

    filesAdded(paths, conversionId)   -> one ConvertOp per file
    outputDirChanged(text)            -> re-resolve every output not running yet
    startClicked(text)                -> resolve, ask about new folders, then panel.run()
    installToolRequested()            -> queue an InstallToolOp
    reloadRequested()                 -> re-scan the conversions folders
    queueIdle()                       -> revalidate (a tool may have just been installed)
"""
import os

from PeelCore import work

from . import paths, tools
from .conversion import migrate_user_conversions, registry, user_dir
from .ops import ConvertOp, InstallToolOp, CONVERT_TYPE


def _prop(op, name, default=""):
    return op.properties().value(name, default)


def _editable(op):
    return op.state() in (work.OpState.Ready, work.OpState.Invalid)


class Controller:
    def __init__(self, panel):
        self.panel = panel
        self.pipeline = panel.pipeline()
        panel.filesAdded.connect(self.add_files)
        panel.outputDirChanged.connect(self.output_dir_changed)
        panel.startClicked.connect(self.start)
        panel.installToolRequested.connect(self.install_tool)
        panel.reloadRequested.connect(self.reload)
        panel.queueIdle.connect(self.queue_idle)
        self.reload()
        if self.install_missing_tools():   # first run: fetch ffmpeg without being asked
            self.panel.run()
            self.refresh_notice()

    # ---- conversions ---------------------------------------------------------------
    def reload(self):
        registry.load_all([user_dir()])
        items = registry.all()
        self.panel.setConversions([c.id for c in items], [c.label for c in items], [c.group for c in items])
        for op in self.convert_ops():
            if _editable(op):
                op.revalidate()
        self.refresh_notice()

    def refresh_notice(self):
        lines = []
        install_button = False
        if not registry.all():
            lines.append("No conversions are defined. Add .json files to %s" % user_dir())
        missing = sorted({c.tool for c in registry.all() if tools.find(c.tool) is None})
        installing = self.installing()
        for name in missing:
            if name in installing:
                lines.append("Downloading %s... (see the queue)" % name)
            elif tools.install_problems(name):
                lines.append("%s is not installed. Put it on the PATH, or set its path in settings.json "
                             "(%s)." % (name, user_dir().parent))
            else:
                lines.append("%s is not installed." % name)
                install_button = True
        if registry.problems:
            extra = " (and %d more)" % (len(registry.problems) - 1) if len(registry.problems) > 1 else ""
            lines.append("Skipped a conversion definition: %s%s" % (registry.problems[0], extra))
        self.panel.setNotice("\n".join(lines), install_button)

    def convert_ops(self):
        return [op for op in self.pipeline.ops() if isinstance(op, ConvertOp)]

    # ---- adding files --------------------------------------------------------------
    def add_files(self, files, conversion_id):
        conversion = registry.get(conversion_id)
        out_text = self.panel.outputDir()
        for path in files:
            path = os.path.normpath(path)
            op = ConvertOp(os.path.basename(path))
            op.setProperty("conversion", conversion_id)
            op.setProperty("input", path)
            if conversion is not None:
                op.setProperty("output", paths.output_path(conversion, path, out_text)[1])
            self.pipeline.addOp(op)         # the Pipeline revalidates it
        self.mark_conflicts()
        self.refresh_notice()

    # ---- outputs -------------------------------------------------------------------
    def assign_outputs(self, out_text):
        for op in self.convert_ops():
            if not _editable(op):
                continue
            conversion = registry.get(_prop(op, "conversion"))
            output = paths.output_path(conversion, _prop(op, "input"), out_text)[1] if conversion else ""
            if output != _prop(op, "output"):
                op.setProperty("output", output)
        self.mark_conflicts()
        for op in self.convert_ops():
            if _editable(op):
                op.revalidate()

    def mark_conflicts(self):
        """Two files that would write the same output: the later one is INVALID."""
        taken = {}
        for op in self.convert_ops():
            state = op.state()
            output = _prop(op, "output")
            if not output or state not in (work.OpState.Ready, work.OpState.Invalid, work.OpState.Running):
                continue
            key = os.path.normcase(os.path.normpath(output))
            message = ""
            if key in taken:
                message = "same output as %s" % taken[key]
            else:
                taken[key] = op.name()
            if _editable(op) and message != _prop(op, "conflict"):
                op.setProperty("conflict", message)

    def output_dir_changed(self, text):
        self.assign_outputs(text)

    # ---- start ---------------------------------------------------------------------
    def start(self, out_text):
        self.assign_outputs(out_text)
        ready = [op for op in self.convert_ops() if op.state() == work.OpState.Ready]
        if not ready:
            self.panel.setNotice("Nothing is ready to run. Rows marked INVALID say what to fix.", False)
            return
        to_create = {}
        for op in ready:
            conversion = registry.get(_prop(op, "conversion"))
            out_dir, _ = paths.output_path(conversion, _prop(op, "input"), out_text)
            if out_dir.create:
                to_create[os.path.normcase(out_dir.path)] = out_dir
        ask = [d.path for d in to_create.values() if d.ask]
        if ask and not self.panel.confirm("Create folder?",
                                          "These folders do not exist:\n\n%s\n\nCreate them?" % "\n".join(ask)):
            return
        for out_dir in to_create.values():
            try:
                os.makedirs(out_dir.path, exist_ok=True)
            except OSError as exc:
                self.panel.setNotice("Cannot create %s: %s" % (out_dir.path, exc), False)
                return
        self.refresh_notice()
        self.panel.run()

    # ---- tools ---------------------------------------------------------------------
    def install_tool(self):
        """The Install button: queue a download for every missing tool that has one."""
        if self.install_missing_tools():
            self.panel.run()

    def installing(self):
        """Names of tools with a download queued or running."""
        return {_prop(op, "tool") for op in self.pipeline.ops()
                if isinstance(op, InstallToolOp)
                and op.state() in (work.OpState.Ready, work.OpState.Running, work.OpState.Invalid)}

    def install_missing_tools(self):
        """Queue an InstallToolOp for each missing tool that has a download and none
        queued already.  Returns True if it queued any (the caller starts the queue)."""
        queued = False
        busy = self.installing()
        for name in sorted({c.tool for c in registry.all() if tools.find(c.tool) is None}):
            if name in busy or tools.install_problems(name):
                continue
            op = InstallToolOp("Install " + name)
            op.setProperty("tool", name)
            self.pipeline.addOp(op)
            queued = True
        return queued

    def queue_idle(self):
        for op in self.convert_ops():
            if op.state() == work.OpState.Invalid:
                op.revalidate()
        self.mark_conflicts()
        self.refresh_notice()
