"""Talk to PeelConvert's cloud API.  Standard library only, no Qt.

    client = CloudClient()
    info = client.sign_in()       # {"token", "expires_at", "plan": {...}}  or raises CloudError
    client.me()                   # what this sign-in may do
    client.status()               # public: ok / degraded / maintenance
    client.token()                # a valid token, signing in again only when it is about to expire

Tokens live in memory only.  They last 15 minutes, and getting a new one re-checks the license on the
server, so a cancelled license stops working within that time.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import cloud_auth

REFRESH_MARGIN = 60          # seconds before expiry at which a new token is fetched


class CloudError(Exception):
    """`code` is the server's code (license_invalid, license_expired, entitlement_missing, app_too_old,
    token_expired, service_unavailable, rate_limited...) or one of ours: not_licensed,
    unreadable_license, network, bad_reply.  `message` is fit to show the person."""
    def __init__(self, code, message, status=0):
        super().__init__(message)
        self.code = code
        self.status = status


def app_version():
    try:
        manifest = Path(__file__).resolve().parent.parent / "manifest.json"
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", ""))
    except (OSError, ValueError):
        return ""


class CloudClient:
    def __init__(self, base_url=None, load_credentials=cloud_auth.load_credentials, version=None, timeout=20.0,
                 clock=time.time):
        self.base_url = (base_url or cloud_auth.server_url()).rstrip("/")
        self._load = load_credentials
        self.version = app_version() if version is None else version
        self.timeout = timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._token = None
        self._expires = 0
        self.plan = None

    # ---- public calls ----
    def status(self):
        return self._request("GET", "/api/cloud/v1/status")

    def sign_in(self):
        """Always signs in anew and returns the server's reply."""
        try:
            headers = cloud_auth.signed_headers(self._load(), self.version)
        except cloud_auth.CloudAuthError as exc:
            raise CloudError(exc.code, str(exc))
        reply = self._request("POST", "/api/licensing/cloud-token/", headers=headers, body={})
        if not isinstance(reply, dict) or not reply.get("token"):
            raise CloudError("bad_reply", "The cloud service sent a reply that could not be used.")
        with self._lock:
            self._token, self._expires, self.plan = reply["token"], int(reply.get("expires_at") or 0), reply.get("plan")
        return reply

    def token(self):
        with self._lock:
            fresh = self._token and self._clock() < self._expires - REFRESH_MARGIN
            token = self._token
        if fresh:
            return token
        return self.sign_in()["token"]

    def me(self):
        return self._request("GET", "/api/cloud/v1/me", headers={"Authorization": "Bearer " + self.token()})

    def sign_out(self):
        with self._lock:
            self._token, self._expires, self.plan = None, 0, None

    # ---- http ----
    def _request(self, method, path, headers=None, body=None):
        data = json.dumps(body).encode() if body is not None else None
        all_headers = {"Accept": "application/json", **(headers or {})}
        if data is not None:
            all_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data, all_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._error(exc)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            raise CloudError("network", "Could not reach PeelConvert's cloud service. Check your internet connection.")
        except ValueError:
            raise CloudError("bad_reply", "The cloud service sent a reply that could not be read.")

    @staticmethod
    def _error(exc):
        try:
            body = json.loads(exc.read().decode("utf-8"))
            code, message = body.get("code") or "", body.get("error") or ""
        except (ValueError, OSError, AttributeError):
            code, message = "", ""
        if not code:
            code = {429: "rate_limited", 404: "license_invalid", 503: "service_unavailable"}.get(exc.code, "server")
        if not isinstance(message, str) or not message:
            message = "The cloud service could not do that (%d)." % exc.code
        if exc.code == 404 and code == "license_invalid":
            message = "This computer is not registered for cloud features. Activate your license first."
        return CloudError(code, message, exc.code)
