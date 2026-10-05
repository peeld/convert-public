"""PeelConvert's Python side.

The C++ host owns the window and the work queue (a work::Pipeline).  This
package knows about conversions and tools:

    conversion  Conversion base class, JSON and Python loading, the registry
    paths       output-path rules (pure functions, no Qt, no PeelCore)
    tools       finding and downloading ffmpeg and other tools
    runner      how many child processes run at once
    ops         ConvertOp and InstallToolOp (work::Op subclasses; need PeelCore)
    app         wires the window's signals to the above (needs the convertapp module)
    check       `python -m convert.check` validates every conversion definition

Only `conversion` and `paths` are imported here, so they work without PeelCore.
"""
from .conversion import Conversion, Job, migrate_user_conversions, register, registry, user_dir, user_python_dir

__all__ = ["Conversion", "Job", "register", "registry", "user_dir",
           "user_python_dir", "migrate_user_conversions"]
