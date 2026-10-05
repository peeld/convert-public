"""A tiny stand-in for the real PeelCore module (the shiboken-generated work framework),
so the Python side can be tested without the C++ build.  Only what convert uses."""
from . import work  # noqa: F401
