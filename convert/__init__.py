"""The file operations of the Peel apps (convert-public).

The app owns the window and the work queue (a work::Pipeline) and its own glue.
This package knows about conversions and tools, and works in any app:

    host        what the running app says: its name, its user data folder (no Qt, no PeelCore)
    conversion  Conversion base class, JSON and Python loading, the registry
    paths       output-path rules (pure functions, no Qt, no PeelCore)
    tools       finding and downloading ffmpeg and other tools
    run         one conversion on one file, with callbacks (no Qt, no PeelCore)
    runner      how many child processes run at once
    ops         ConvertOp and InstallToolOp (work::Op subclasses; need PeelCore)
    update_*    self-update from signed releases (update_ui needs PySide6)
    check       `python -m convert.check` validates every conversion definition

Only `conversion` and `paths` are imported here, so they work without PeelCore.
"""
from .conversion import Conversion, Job, migrate_user_conversions, register, registry, user_dir, user_python_dir

__all__ = ["Conversion", "Job", "register", "registry", "user_dir",
           "user_python_dir", "migrate_user_conversions"]
