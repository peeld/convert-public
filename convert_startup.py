"""Run by PeelConvert at startup (src/appPython.cpp): install() connects the
window to the conversion system.  check() is what `PeelConvert --python-check` prints.

`convertapp` is built into the exe: shiboken6 generates it from src/bindings.h
and src/bindings.xml.  `PeelCore` (the work framework: Op, Pipeline, ...) is
built in too.  convertapp.mainWindow() is the app's MainWindow, owned by C++.
"""
import importlib
import os
import ssl
import sys

import convertapp
import PySide6
from PySide6.QtCore import qVersion
from PySide6.QtWidgets import QMainWindow, QMessageBox

_controller = None      # keeps the signal connections alive
_updates = None         # the update service (convert/update_service.py)


def main_window():
    return convertapp.mainWindow()


def install():
    global _controller
    from convert.app import Controller
    win = main_window()
    win.setMessage(f"Python {sys.version.split()[0]}, PySide6 {PySide6.__version__}")
    _controller = Controller(win.convertPanel())
    menu = win.menuBar().addMenu("&Python")
    menu.addAction("About Python...").triggered.connect(
        lambda: QMessageBox.information(win, "Python", report()))
    global _updates
    from convert.update_ui import install_update_service
    _updates = install_update_service(win, _controller)


def loaded_dlls():
    """Paths of every module loaded in this process (Windows only)."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes
    psapi = ctypes.WinDLL("psapi")
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    process = kernel32.GetCurrentProcess()
    needed = wintypes.DWORD()
    mods = (wintypes.HMODULE * 2048)()
    psapi.EnumProcessModules.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
    if not psapi.EnumProcessModules(process, mods, ctypes.sizeof(mods), ctypes.byref(needed)):
        return []
    kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    out = []
    buf = ctypes.create_unicode_buffer(32768)
    for mod in mods[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
        if kernel32.GetModuleFileNameW(mod, buf, len(buf)):
            out.append(buf.value)
    return out


def _dlls_named(dlls, *prefixes):
    return [d for d in dlls if os.path.basename(d).lower().startswith(prefixes)]


def _tls():
    from PySide6.QtNetwork import QSslSocket
    return QSslSocket.supportsSsl(), QSslSocket.activeBackend(), QSslSocket.sslLibraryVersionString()


def _import_pyside_modules():
    """Import every Qt module this PySide6 has: each must find its Qt DLLs
    (next to the exe once staged). Returns the failures as text."""
    failed = []
    for name in PySide6.__all__:
        try:
            importlib.import_module(f"PySide6.{name}")
        except ImportError as exc:
            failed.append(f"PySide6.{name}: {exc}")
    return failed


def report():
    from convert import registry
    from convert import tools
    ok, backend, qt_ssl = _tls()
    dlls = loaded_dlls()
    lines = [
        f"Python {sys.version}",
        f"  home {sys.prefix}",
        f"  path {sys.path}",
        f"PySide6 {PySide6.__version__}, Qt runtime {qVersion()}",
        f"Python ssl: {ssl.OPENSSL_VERSION}",
        f"Qt TLS: backend {backend!r}, supportsSsl {ok}, {qt_ssl}",
        f"Main window: {type(main_window()).__module__}.{type(main_window()).__name__} "
        f"{main_window().windowTitle()!r}, message {main_window().message()!r}",
        f"Conversions: {len(registry.all())}, problems: {registry.problems}",
        f"ffmpeg: {tools.find('ffmpeg')}",
        "Loaded:",
    ]
    lines += [f"  {d}" for d in _dlls_named(dlls, "qt6core", "libssl", "libcrypto", "python3",
                                            "pyside6", "shiboken6")]
    return "\n".join(lines)


def check():
    """report(), raising if a PySide6 module doesn't import, the process has
    a second Qt or OpenSSL loaded, Qt's TLS isn't OpenSSL, the main window
    can't be reached, PeelCore is missing, or a shipped conversion is bad.
    (A missing ffmpeg is not a failure: the app offers to install it.)"""
    problems = _import_pyside_modules()
    try:
        from PeelCore import work
        work.Params()
        from convert import registry, user_dir
        registry.load_all([user_dir()])
        if not registry.all():
            problems.append("no conversions loaded")
        problems += [f"conversion: {p}" for p in registry.problems]
    except Exception as exc:
        problems.append(f"convert/PeelCore: {type(exc).__name__}: {exc}")
    text = report()
    dlls = loaded_dlls()
    win = main_window()
    if not isinstance(win, convertapp.MainWindow) or not isinstance(win, QMainWindow):
        problems.append(f"main window is a {type(win)}, want convertapp.MainWindow")
    elif not win.message().startswith("Python "):
        problems.append(f"MainWindow.message() is {win.message()!r}: install() didn't run")
    if sys.platform == "win32":
        for prefix in ("qt6core", "libssl", "libcrypto"):
            found = _dlls_named(dlls, prefix)
            if len(found) != 1:
                problems.append(f"expected one {prefix}*.dll loaded, got {found}")
        # With every PySide6 module imported: no Qt DLL may come from two places.
        by_name = {}
        for d in _dlls_named(dlls, "qt6"):
            by_name.setdefault(os.path.basename(d).lower(), []).append(d)
        problems += [f"{name} loaded twice: {paths}" for name, paths in by_name.items() if len(paths) > 1]
    ok, backend, _ = _tls()
    if not ok or backend != "openssl":
        problems.append(f"Qt TLS backend is {backend!r} (supportsSsl {ok}), want openssl")
    if problems:
        raise RuntimeError(text + "\n\nFAILED:\n  " + "\n  ".join(problems))
    return text + "\n\nOK"
