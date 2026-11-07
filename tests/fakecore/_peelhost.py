"""A stand-in for the `_peelhost` module the app's exe builds in (core's
pythonhost/pythonHost.cpp), host API 3.  The app's name is deliberately not a real
app's: convert-public must not depend on which app runs it.  No user_data_dir():
convert.host then uses the same APPDATA rule as the exe, which the tests point at
a temporary folder."""
import os
import sys

APP_NAME = "TestApp"


def host_api():
    return 3


def app_name():
    return APP_NAME


def can_verify():
    return False


def verify(data, signature):
    return False


def source():
    return ("bundled", "", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def exe_path():
    return sys.executable


def bundled_dir():
    return source()[2]
