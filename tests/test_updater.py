import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fakecore"))

from convert import updater as u  # noqa: E402


def release(version, api=(1, 1), files=None, tamper=None, extra=None):
    manifest = {"schema": 1, "name": "convert", "version": version, "host_api": {"min": api[0], "max": api[1]}}
    files = files or {"manifest.json": json.dumps(manifest), "convert/__init__.py": "x = 1\n"}
    listing = {"version": version, "host_api": manifest["host_api"],
               "files": {k: hashlib.sha256(v.encode()).hexdigest() for k, v in files.items()}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in {**files, **(tamper or {}), **(extra or {})}.items():
            z.writestr(k, v)
    base = "https://x/%s/" % version
    blobs = {base + "list": json.dumps(listing).encode(), base + "sig": b"good", base + "zip": buf.getvalue()}
    upd = {"version": version, "notes": "", "zip_url": base + "zip", "list_url": base + "list", "sig_url": base + "sig"}
    return upd, blobs


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = u.State(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, upd, blobs, running="0.1.0", api=1, sig_ok=True, check_ok=True):
        return u.install(upd, running, api, self.state, blobs.__getitem__,
                         lambda data, sig: sig_ok and sig == b"good", lambda folder: check_ok)

    def test_versions_compare_numerically(self):
        self.assertGreater(u.parse_version("0.10.0"), u.parse_version("0.9.0"))
        with self.assertRaises(u.UpdateError):
            u.parse_version("1.2")

    def test_good_install_stages_and_marks_pending(self):
        upd, blobs = release("0.2.0")
        self.assertEqual(self.install(upd, blobs), "0.2.0")
        self.assertTrue((self.state.version_dir("0.2.0") / "convert" / "__init__.py").is_file())
        self.assertEqual((self.state["current"], self.state["pending"]), ("0.2.0", True))
        self.assertFalse((Path(self.tmp.name) / "staging" / "0.2.0").exists())

    def test_bad_signature_changes_nothing(self):
        upd, blobs = release("0.2.0")
        with self.assertRaises(u.UpdateError):
            self.install(upd, blobs, sig_ok=False)
        self.assertIsNone(self.state["current"])
        self.assertFalse(self.state.version_dir("0.2.0").exists())

    def test_tampered_file_is_rejected(self):
        upd, blobs = release("0.2.0", tamper={"convert/__init__.py": "evil\n"})
        with self.assertRaises(u.UpdateError):
            self.install(upd, blobs)
        self.assertIsNone(self.state["current"])

    def test_extra_file_in_zip_is_rejected(self):
        upd, blobs = release("0.2.0", extra={"convert/evil.py": "x"})
        with self.assertRaises(u.UpdateError):
            self.install(upd, blobs)

    def test_path_traversal_is_rejected(self):
        manifest = json.dumps({"version": "0.2.0", "host_api": {"min": 1, "max": 1}})
        upd, blobs = release("0.2.0", files={"manifest.json": manifest, "../evil.py": "x"})
        with self.assertRaises(u.UpdateError):
            self.install(upd, blobs)
        self.assertFalse((Path(self.tmp.name).parent / "evil.py").exists())

    def test_downgrade_and_same_version_refused(self):
        upd, blobs = release("0.1.0")
        with self.assertRaises(u.UpdateError):
            self.install(upd, blobs, running="0.1.0")

    def test_host_api_too_new_is_refused(self):
        upd, blobs = release("0.2.0", api=(2, 2))
        with self.assertRaises(u.UpdateError):
            self.install(upd, blobs, api=1)

    def test_failing_check_changes_nothing(self):
        upd, blobs = release("0.2.0")
        with self.assertRaises(u.UpdateError):
            self.install(upd, blobs, check_ok=False)
        self.assertFalse(self.state.version_dir("0.2.0").exists())
        self.assertIsNone(self.state["current"])

    def test_rollback_after_a_failed_start(self):
        for v in ("0.2.0", "0.3.0"):
            upd, blobs = release(v)
            self.install(upd, blobs, running="0.1.0")
            u.mark_good(self.state) if v == "0.2.0" else None
        self.assertEqual(self.state["previous"], "0.2.0")
        # first start of 0.3.0 does not mark good; the next start sees pending twice
        self.assertEqual(u.boot(self.state, started_pending_before=False), "0.3.0")
        self.assertEqual(u.boot(self.state, started_pending_before=True), "0.2.0")
        self.assertTrue(self.state.is_bad("0.3.0"))
        self.assertFalse(self.state["pending"])
        # and 0.3.0 is not offered again
        rel = {"tag_name": "v0.3.0", "assets": [{"name": n, "browser_download_url": "u"}
                                                   for n in ("convert-0.3.0.zip", "files.json", "files.json.sig")]}
        self.assertIsNone(u.find_update(lambda url: rel, "0.2.0", 1, self.state))

    def test_old_versions_are_pruned(self):
        for v in ("0.2.0", "0.3.0", "0.4.0"):
            upd, blobs = release(v)
            self.install(upd, blobs)
        names = sorted(p.name for p in (Path(self.tmp.name) / "versions").iterdir())
        self.assertEqual(names, ["0.3.0", "0.4.0"])

    def test_find_update(self):
        names = ("convert-0.2.0.zip", "files.json", "files.json.sig")
        rel = {"tag_name": "v0.2.0", "body": "notes", "assets": [{"name": n, "browser_download_url": "u/" + n} for n in names]}
        found = u.find_update(lambda url: rel, "0.1.0", 1, self.state)
        self.assertEqual((found["version"], found["notes"]), ("0.2.0", "notes"))
        self.assertIsNone(u.find_update(lambda url: rel, "0.2.0", 1, self.state))
        self.assertIsNone(u.find_update(lambda url: {**rel, "draft": True}, "0.1.0", 1, self.state))
        self.assertIsNone(u.find_update(lambda url: {**rel, "assets": []}, "0.1.0", 1, self.state))

    def test_bundled_newer_wins_and_downloads_are_dropped(self):
        upd, blobs = release("0.2.0")
        self.install(upd, blobs)
        self.assertEqual(u.newest_allowed("0.1.0", self.state), "0.2.0")
        self.assertIsNone(u.newest_allowed("0.3.0", self.state))
        u.drop_stale("0.3.0", self.state)
        self.assertIsNone(self.state["current"])
        self.assertFalse(self.state.version_dir("0.2.0").exists())


if __name__ == "__main__":
    unittest.main()
