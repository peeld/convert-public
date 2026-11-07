"""The user's own start-up code: every <user data>/userPython/startup/*.py, in name order.

An app calls run(window) once its window is up. Each file is imported as its own
module (user_startup_<stem>) and, if it defines install(window), that is called with the
app's main window (a QMainWindow), so it can add menus, docks or shortcuts:

    # ~/.config/PeelPost/userPython/startup/my_tools.py   (%APPDATA%\\PeelPost\\... on Windows)
    def install(window):
        menu = window.menuBar().addMenu("My Tools")
        menu.addAction("Hello").triggered.connect(lambda: print("hello"))

A file that fails to import or install is reported and skipped; it never stops the app.
No update or installer writes in userPython, so this code survives both. Operations of
your own go in userPython/conversions instead (conversion.py).

No PeelCore or Qt imported here; the window is whatever the app passes.
"""
import importlib.util
import sys
import traceback

from .conversion import user_python_dir


def startup_dir():
    return user_python_dir() / "startup"


def run(window, folder=None):
    """Import and install every startup file.  Returns (loaded names, problem lines)."""
    folder = folder if folder is not None else startup_dir()
    loaded, problems = [], []
    if not folder.is_dir():
        return loaded, problems
    for path in sorted(folder.glob("*.py")):
        name = "user_startup_" + path.stem
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            install = getattr(module, "install", None)
            if callable(install):
                install(window)
            loaded.append(path.name)
        except Exception as exc:     # noqa: BLE001 - the user's code must not stop the app
            sys.modules.pop(name, None)
            problems.append("%s: %s: %s" % (path.name, type(exc).__name__, exc))
            traceback.print_exc()
    return loaded, problems
