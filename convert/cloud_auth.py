"""Prove to the cloud who this installation is, using the license the app already has.

The C++ license code keeps, encrypted for the Windows user in %LOCALAPPDATA%\\PeelConvert\\license.dat,
a JSON object {product_slug, machine_secret, offline_jwt}.  The offline_jwt's payload also holds the
machine id hash the server knows this PC by ("machine"), so nothing has to be measured again here.
Requests are signed exactly like the C++ machine check-in: HMAC-SHA256 of
machine_id + product_slug + timestamp + nonce (+ the NIC list, which we leave empty) with the
machine secret.

This is the ONLY file that touches the secret.  Later it can be replaced by a small C++ call that
signs without handing Python the secret; nothing else would change (see Cloud AI Plan.md, phase 7).
Pure Python, no Qt.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "PeelConvert"                    # licenseConfig.h kAppName
DEFAULT_SERVER = "https://peeldev.com"      # licenseConfig.h kServerUrl


class CloudAuthError(Exception):
    """`code` is one of: not_licensed, unreadable_license."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Credentials:
    product_slug: str
    machine_secret: str
    machine_id: str


def server_url():
    return (os.environ.get("PEELCONVERT_CLOUD_URL") or DEFAULT_SERVER).rstrip("/")


def license_path():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME / "license.dat"


def _unprotect(data):
    """Windows DPAPI for the current user, no extra entropy (as the C++ store writes it)."""
    import sys
    if sys.platform != "win32":
        raise OSError("license.dat can only be read on Windows")
    from .describe_settings import _dpapi
    return _dpapi(False, data, None)


def _jwt_payload(token):
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def load_credentials(path=None, unprotect=_unprotect):
    """Read the license the app is using.  Raises CloudAuthError if there is none."""
    path = Path(path) if path else license_path()
    try:
        cipher = path.read_bytes()
    except OSError:
        raise CloudAuthError("not_licensed", "PeelConvert is not activated on this computer yet.")
    try:
        data = json.loads(unprotect(cipher).decode("utf-8"))
        machine = _jwt_payload(data["offline_jwt"])["machine"]
        return Credentials(str(data["product_slug"]), str(data["machine_secret"]), str(machine))
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        raise CloudAuthError("unreadable_license", "The saved license could not be read. Open the license "
                                                   "window and activate again.")


def signed_headers(creds, app_version="", now=None, nonce=None):
    """Headers for a machine-signed request (the same scheme as machine-checkin)."""
    timestamp = str(int(time.time() if now is None else now))
    nonce = nonce or secrets.token_hex(16)
    message = (creds.machine_id + creds.product_slug + timestamp + nonce).encode()      # no NIC list
    signature = hmac.new(creds.machine_secret.encode(), message, hashlib.sha256).hexdigest()
    headers = {"X-Machine-ID": creds.machine_id, "X-Product-Slug": creds.product_slug,
               "X-Timestamp": timestamp, "X-Nonce": nonce, "X-Signature": signature}
    if app_version:
        headers["X-Peel-App-Version"] = app_version
    return headers
