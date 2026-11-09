"""cloud_auth (license file, signing) and cloud_client against a local fake of the cloud API.
The fake checks the signature with the same formula licensing's _authenticate_machine uses.
Run from the python folder:  python -m unittest discover -s tests"""
import base64
import hashlib
import hmac
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from convert import cloud_auth, cloud_client   # noqa: E402

SECRET = "ab" * 32
MACHINE = "c" * 64


def fake_jwt(payload):
    part = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return "h.%s.s" % part


def license_bytes(**over):
    data = {"product_slug": "peelconvert", "machine_secret": SECRET, "offline_jwt": fake_jwt({"machine": MACHINE})}
    data.update(over)
    return json.dumps(data).encode()


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name, "license.dat")

    def test_reads_credentials_and_machine_id_from_the_offline_token(self):
        self.path.write_bytes(b"cipher")
        creds = cloud_auth.load_credentials(self.path, unprotect=lambda c: license_bytes())
        self.assertEqual((creds.product_slug, creds.machine_secret, creds.machine_id), ("peelconvert", SECRET, MACHINE))

    def test_missing_file_is_not_licensed(self):
        with self.assertRaises(cloud_auth.CloudAuthError) as cm:
            cloud_auth.load_credentials(self.path)
        self.assertEqual(cm.exception.code, "not_licensed")

    def test_unreadable_variants(self):
        self.path.write_bytes(b"cipher")
        bad = [lambda c: b"not json", lambda c: license_bytes(offline_jwt="nodots"),
               lambda c: json.dumps({"product_slug": "x"}).encode(), lambda c: (_ for _ in ()).throw(OSError("dpapi"))]
        for unprotect in bad:
            with self.assertRaises(cloud_auth.CloudAuthError) as cm:
                cloud_auth.load_credentials(self.path, unprotect=unprotect)
            self.assertEqual(cm.exception.code, "unreadable_license")

    def test_signature_matches_the_server_formula(self):
        creds = cloud_auth.Credentials("peelconvert", SECRET, MACHINE)
        h = cloud_auth.signed_headers(creds, "1.2.3", now=1700000000, nonce="n0nce")
        msg = (MACHINE + "peelconvert" + "1700000000" + "n0nce" + "").encode()
        self.assertEqual(h["X-Signature"], hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest())
        self.assertEqual(h["X-Peel-App-Version"], "1.2.3")
        self.assertNotIn("X-Machine-Macs", h)
        self.assertNotIn(SECRET, json.dumps(h))
        self.assertNotEqual(cloud_auth.signed_headers(creds)["X-Nonce"], cloud_auth.signed_headers(creds)["X-Nonce"])


class Fake(BaseHTTPRequestHandler):
    nonces = set()
    now = 1000
    tokens_issued = 0
    mode = "ok"            # ok | no_entitlement | down

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/cloud/v1/status":
            return self._send(200, {"status": "ok", "min_app_version": "", "message": ""})
        if self.path == "/api/cloud/v1/me":
            if self.headers.get("Authorization") != "Bearer tok%d" % Fake.tokens_issued:
                return self._send(401, {"error": "Your sign-in was not accepted.", "code": "license_invalid"})
            return self._send(200, {"license": "aaaaaaaa", "features": ["cloud_link"]})
        self._send(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        if Fake.mode == "down":
            return self._send(503, {"error": "Cloud services are not available right now.", "code": "service_unavailable"})
        h = self.headers
        msg = (h["X-Machine-ID"] + h["X-Product-Slug"] + h["X-Timestamp"] + h["X-Nonce"]).encode()
        good = hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, h["X-Signature"]):
            return self._send(401, {"error": "Invalid signature."})
        if h["X-Nonce"] in Fake.nonces:
            return self._send(401, {"error": "Replayed request."})
        Fake.nonces.add(h["X-Nonce"])
        if Fake.mode == "no_entitlement":
            return self._send(403, {"error": "This license does not include cloud features.", "code": "entitlement_missing"})
        Fake.tokens_issued += 1
        self._send(200, {"token": "tok%d" % Fake.tokens_issued, "expires_at": Fake.now + 900,
                         "plan": {"product": "peelconvert", "features": ["cloud_link"]}})

    def log_message(self, *a):
        pass


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        Fake.nonces, Fake.now, Fake.tokens_issued, Fake.mode = set(), 1000, 0, "ok"
        self.clock = [1000]
        creds = cloud_auth.Credentials("peelconvert", SECRET, MACHINE)
        self.client = cloud_client.CloudClient("http://127.0.0.1:%d" % self.server.server_port,
                                               load_credentials=lambda: creds, version="1.0.0",
                                               clock=lambda: self.clock[0])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_sign_in_returns_plan_and_token(self):
        reply = self.client.sign_in()
        self.assertEqual(reply["token"], "tok1")
        self.assertEqual(self.client.plan["features"], ["cloud_link"])

    def test_token_is_reused_then_renewed_near_expiry(self):
        self.assertEqual(self.client.token(), "tok1")
        self.assertEqual(self.client.token(), "tok1")
        self.assertEqual(Fake.tokens_issued, 1)
        self.clock[0] = 1000 + 900 - 30                      # inside the 60 s margin
        Fake.now = self.clock[0]
        self.assertEqual(self.client.token(), "tok2")

    def test_me_uses_the_token(self):
        self.assertEqual(self.client.me()["license"], "aaaaaaaa")

    def test_status_needs_no_license(self):
        client = cloud_client.CloudClient(self.client.base_url, load_credentials=lambda: 1 / 0)
        self.assertEqual(client.status()["status"], "ok")

    def test_server_errors_become_cloud_errors(self):
        Fake.mode = "no_entitlement"
        with self.assertRaises(cloud_client.CloudError) as cm:
            self.client.sign_in()
        self.assertEqual((cm.exception.code, cm.exception.status), ("entitlement_missing", 403))
        self.assertIn("cloud features", str(cm.exception))
        Fake.mode = "down"
        with self.assertRaises(cloud_client.CloudError) as cm:
            self.client.sign_in()
        self.assertEqual(cm.exception.code, "service_unavailable")

    def test_not_licensed_is_reported_without_a_network_call(self):
        def none():
            raise cloud_auth.CloudAuthError("not_licensed", "PeelConvert is not activated on this computer yet.")
        client = cloud_client.CloudClient("http://127.0.0.1:1", load_credentials=none)
        with self.assertRaises(cloud_client.CloudError) as cm:
            client.sign_in()
        self.assertEqual(cm.exception.code, "not_licensed")

    def test_unreachable_server(self):
        client = cloud_client.CloudClient("http://127.0.0.1:1", load_credentials=lambda: cloud_auth.Credentials("p", SECRET, MACHINE),
                                          timeout=2)
        with self.assertRaises(cloud_client.CloudError) as cm:
            client.sign_in()
        self.assertEqual(cm.exception.code, "network")

    def test_sign_out_forgets_the_token(self):
        self.client.sign_in()
        self.client.sign_out()
        self.assertIsNone(self.client.plan)
        self.assertEqual(self.client.token(), "tok2")


if __name__ == "__main__":
    unittest.main()
