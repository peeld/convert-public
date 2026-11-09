"""Fake command-line tools for the tests.

A fake tool is written as Python source.  On POSIX it is an executable script with a
#! line; Windows cannot run #! scripts through subprocess, so there it is a .cmd wrapper
that runs the same source with this Python.  Either way the caller gets a path it can
put in settings.json (or tools.find) and run like any tool.
"""
import os
import stat
import sys
from pathlib import Path


def make_tool(directory, name, source):
    directory = Path(directory)
    script = directory / (name + "_tool.py")
    script.write_text(source, encoding="utf-8")
    if sys.platform == "win32":
        exe = directory / (name + ".cmd")
        exe.write_text('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, script), encoding="utf-8")
    else:
        exe = directory / name
        exe.write_text("#!%s\n%s" % (sys.executable, source), encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe
