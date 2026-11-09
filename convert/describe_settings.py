"""Settings for "Describe a conversion": which AI service, which model, and the person's API key.

Pure Python (no Qt).  Files in %APPDATA%\\PeelConvert\\ (next to settings.json):

    describe.json   {"provider": "anthropic", "model": "..."}     plain, nothing secret
    describe.key    the API key, encrypted for this Windows user with DPAPI (the same
                    protection the license file gets).  Never written as plain text on
                    Windows, never logged, never sent anywhere except the AI service.

On other systems (development and tests only) the key file holds the key base64-encoded with
owner-only permissions: that is NOT encryption, and the app is Windows-only.
"""
import base64
import ctypes
import json
import os
import sys

from .conversion import user_data_dir

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-sonnet-5-5"
_ENTROPY = b"PeelConvert describe key v1"
_PLAIN = b"PLAIN:"


# ---- protecting the key ------------------------------------------------------------------
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.addressof(buf)), buf        # keep buf alive with the blob


def _dpapi(protect, data, entropy_bytes=_ENTROPY):
    from ctypes import wintypes
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pblob = ctypes.POINTER(_Blob)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    entropy, entropy_buf = _blob(entropy_bytes) if entropy_bytes else (None, None)
    entropy_ref = ctypes.byref(entropy) if entropy is not None else None
    source, source_buf = _blob(data)
    out = _Blob()
    if protect:
        crypt32.CryptProtectData.argtypes = [pblob, wintypes.LPCWSTR, pblob, ctypes.c_void_p,
                                             ctypes.c_void_p, wintypes.DWORD, pblob]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        ok = crypt32.CryptProtectData(ctypes.byref(source), "PeelConvert API key", entropy_ref,
                                      None, None, 1, ctypes.byref(out))        # 1 = never show UI
    else:
        crypt32.CryptUnprotectData.argtypes = [pblob, ctypes.c_void_p, pblob, ctypes.c_void_p,
                                               ctypes.c_void_p, wintypes.DWORD, pblob]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, entropy_ref,
                                        None, None, 1, ctypes.byref(out))
    if not ok:
        raise OSError("Windows could not %s the key: %s" % ("protect" if protect else "unprotect",
                                                            ctypes.WinError(ctypes.get_last_error())))
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(data):
    if sys.platform == "win32":
        return _dpapi(True, data)
    return _PLAIN + base64.b64encode(data)


def unprotect(data):
    if sys.platform == "win32":
        return _dpapi(False, data)
    if not data.startswith(_PLAIN):
        raise OSError("unreadable key file")
    return base64.b64decode(data[len(_PLAIN):])


# ---- the settings ------------------------------------------------------------------------
def settings_path():
    return user_data_dir() / "describe.json"


def key_path():
    return user_data_dir() / "describe.key"


def load():
    """{'provider', 'model'} with defaults filled in."""
    data = {}
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if not isinstance(data, dict):
        data = {}
    model = data.get("model") if isinstance(data.get("model"), str) and data.get("model").strip() else DEFAULT_MODEL
    return {"provider": DEFAULT_PROVIDER, "model": model.strip()}


def save(model):
    user_data_dir().mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps({"provider": DEFAULT_PROVIDER, "model": (model or DEFAULT_MODEL).strip()},
                                          indent=2) + "\n", encoding="utf-8")


def get_key():
    """The saved API key, or None.  Never raises: a key that cannot be read counts as missing."""
    try:
        text = unprotect(key_path().read_bytes()).decode("utf-8").strip()
    except (OSError, ValueError):
        return None
    return text or None


def set_key(key):
    key = (key or "").strip()
    if not key:
        raise ValueError("the key is empty")
    user_data_dir().mkdir(parents=True, exist_ok=True)
    path = key_path()
    path.write_bytes(protect(key.encode("utf-8")))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def clear_key():
    try:
        key_path().unlink()
    except OSError:
        pass


def mask(key):
    """'sk-ant-...wxyz' style text that is safe to show."""
    if not key:
        return ""
    return (key[:7] + "..." + key[-4:]) if len(key) > 14 else "..."
