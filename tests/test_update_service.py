import datetime
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fakecore"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from convert import updater as u  # noqa: E402
from convert.update_service import UpdateService  # noqa: E402
from test_updater import release  # noqa: E402

NOW = datetime.datetime(2026, 10, 5, 12, 0, tzinfo=datetime.timezone.utc)


class FakeUi:
    def __init__(self, **answers):
        self.answers, self.log = answers, []

    def ask_auto(self):
        self.log.append("ask_auto")
        return self.answers.get("auto", True)

    def confirm_install(self, version, notes):
        self.log.append("confirm " + version)
        return self.answers.get("confirm", True)

    def notify_installed(self, version, notes):
        self.log.append("installed " + version)

    def notify_up_to_date(self):
        self.log.append("up_to_date")

    def warn_modified(self, files):
        self.log.append("modified %s" % files)
        return self.answers.get("modified", "cancel")

    def open_user_folder(self):
        self.log.append("open")

    def error(self, text):
        self.log.append("error")


class FakeHost:
    api = 1

    def __init__(self, root, kind="bundled", version="0.1.0", can=True, sig_ok=True):
        self.root, self._kind, self._version, self._can, self.sig_ok = root, kind, version, can, sig_ok
        bundled = Path(root) / "bundled"
        bundled.mkdir(exist_ok=True)
        (bundled / "manifest.json").write_text(json.dumps({"version": "0.1.0"}))

    def can_verify(self):
        return self._can

    def verify(self, data, sig):
        return self.sig_ok and sig == b"good"

    def source(self):
        return (self._kind, self._version, str(self.root))

    def exe_path(self):
        return "exe"

    def bundled_dir(self):
        return str(Path(self.root) / "bundled")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = u.State(self.root / "python")
        self.settings = u.Settings(self.root / "settings.json")
        upd, self.blobs = release("0.2.0")
        names = ("convert-0.2.0.zip", "files.json", "files.json.sig")
        self.rel = {"tag_name": "v0.2.0", "body": "notes",
                    "assets": [{"name": n, "browser_download_url": u_} for n, u_ in
                               zip(names, (upd["zip_url"], upd["list_url"], upd["sig_url"]))]}

    def tearDown(self):
        self.tmp.cleanup()

    def service(self, ui, host=None, release_json=None):
        host = host or FakeHost(self.root)
        svc = UpdateService(host, self.settings, self.state, ui, post=lambda fn: fn(), run_async=lambda fn: fn(),
                            fetch_json=lambda url: release_json or self.rel, download=self.blobs.__getitem__,
                            now=lambda: NOW)
        svc._run_check = lambda folder: True
        return svc

    # ---- first start ----------------------------------------------------------------
    def test_asks_once_and_remembers_yes(self):
        ui = FakeUi(auto=True)
        self.service(ui).startup()
        self.assertEqual(self.settings.get("auto_update"), True)
        self.assertIn("installed 0.2.0", ui.log)
        ui2 = FakeUi()
        self.service(ui2).startup()
        self.assertNotIn("ask_auto", ui2.log)

    def test_no_means_never_asked_again_and_never_checks(self):
        ui = FakeUi(auto=False)
        self.service(ui).startup()
        self.assertEqual(self.settings.get("auto_update"), False)
        self.assertEqual(ui.log, ["ask_auto"])
        ui2 = FakeUi()
        self.service(ui2).startup()
        self.assertEqual(ui2.log, [])

    def test_dismissed_question_is_asked_next_time(self):
        ui = FakeUi(auto=None)
        self.service(ui).startup()
        self.assertIsNone(self.settings.get("auto_update"))

    def test_checks_at_most_once_a_day(self):
        self.settings.set("auto_update", True)
        self.settings.set("last_check", (NOW - datetime.timedelta(hours=3)).isoformat())
        ui = FakeUi()
        self.service(ui).startup()
        self.assertEqual(ui.log, [])
        self.settings.set("last_check", (NOW - datetime.timedelta(hours=25)).isoformat())
        self.service(ui).startup()
        self.assertIn("installed 0.2.0", ui.log)
        self.assertEqual(self.settings.get("last_check"), NOW.isoformat())

    # ---- off switches -----------------------------------------------------------------
    def test_no_keys_or_developer_folder_means_no_updates(self):
        for host in (FakeHost(self.root, can=False), FakeHost(self.root, kind="developer"), FakeHost(self.root, kind="override")):
            ui = FakeUi()
            self.service(ui, host).startup()
            self.assertEqual(ui.log, [])
        ui = FakeUi()
        self.service(ui, FakeHost(self.root, can=False)).run_check(manual=True)
        self.assertEqual(ui.log, ["error"])

    # ---- manual -----------------------------------------------------------------------
    def test_manual_check_asks_before_installing(self):
        ui = FakeUi(confirm=False)
        self.service(ui).run_check(manual=True)
        self.assertEqual(ui.log, ["confirm 0.2.0"])
        self.assertIsNone(self.state["current"])
        ui = FakeUi(confirm=True)
        self.service(ui).run_check(manual=True)
        self.assertEqual(ui.log, ["confirm 0.2.0", "installed 0.2.0"])

    def test_manual_check_with_nothing_new(self):
        ui = FakeUi()
        self.service(ui, release_json={**self.rel, "tag_name": "v0.1.0"}).run_check(manual=True)
        self.assertEqual(ui.log, ["up_to_date"])

    def test_network_failure_is_silent_when_automatic_and_reported_when_manual(self):
        def boom(url):
            raise OSError("offline")
        for manual, expect in ((False, []), (True, ["error"])):
            ui = FakeUi()
            svc = self.service(ui)
            svc.fetch_json = boom
            svc.run_check(manual=manual)
            self.assertEqual(ui.log, expect)
            self.assertFalse(svc.working)

    def test_bad_signature_installs_nothing_and_says_so(self):
        ui = FakeUi(confirm=True)
        self.service(ui, FakeHost(self.root, sig_ok=False)).run_check(manual=True)
        self.assertEqual(ui.log, ["confirm 0.2.0", "error"])
        self.assertIsNone(self.state["current"])

    # ---- hand-edited bundled files ---------------------------------------------------------
    def make_bundle(self, edit):
        b = self.root / "bundled"
        b.mkdir(exist_ok=True)
        (b / "convert").mkdir(exist_ok=True)
        (b / "convert" / "a.py").write_text("x = 1\n")
        (b / "files.json").write_text(json.dumps({"files": {"convert/a.py": hashlib.sha256(b"x = 1\n").hexdigest()}}))
        if edit:
            (b / "convert" / "a.py").write_text("x = 2\n")
        return b

    def test_modified_files_are_detected(self):
        self.make_bundle(edit=False)
        self.assertEqual(u.modified_files(self.root / "bundled"), [])
        self.make_bundle(edit=True)
        (self.root / "bundled" / "convert" / "mine.py").write_text("")
        self.assertEqual(u.modified_files(self.root / "bundled"), ["convert/a.py", "convert/mine.py (added)"])
        self.assertEqual(u.modified_files(self.root / "nowhere"), [])

    def test_crlf_does_not_count_as_an_edit(self):
        b = self.make_bundle(edit=False)
        (b / "convert" / "a.py").write_bytes(b"x = 1\r\n")
        self.assertEqual(u.modified_files(b), [])

    def test_automatic_update_stops_when_bundled_files_were_edited(self):
        self.make_bundle(edit=True)
        self.settings.set("auto_update", True)
        for choice, installed in (("cancel", False), ("open", False), ("update", True)):
            self.state.data.update(current=None, previous=None)
            self.settings.set("last_check", None)
            ui = FakeUi(modified=choice)
            self.service(ui).startup()
            self.assertEqual(any(x.startswith("installed") for x in ui.log), installed, choice)
            self.assertEqual("open" in ui.log, choice == "open")

    # ---- settings ----------------------------------------------------------------------
    def test_settings_keep_other_keys(self):
        (self.root / "settings.json").write_text(json.dumps({"tools": {"ffmpeg": "C:/f.exe"}}))
        self.settings.set("auto_update", True)
        data = json.loads((self.root / "settings.json").read_text())
        self.assertEqual(data, {"tools": {"ffmpeg": "C:/f.exe"}, "python": {"auto_update": True}})

    def test_check_due(self):
        self.assertTrue(u.check_due(None, NOW))
        self.assertTrue(u.check_due("garbage", NOW))
        self.assertFalse(u.check_due((NOW - datetime.timedelta(hours=1)).isoformat(), NOW))
        self.assertTrue(u.check_due((NOW + datetime.timedelta(days=3)).isoformat(), NOW))   # clock went backwards

    def test_only_project_urls_are_fetched(self):
        with self.assertRaises(u.UpdateError):
            u.https_get("https://evil.example/x")
        with self.assertRaises(u.UpdateError):
            u.https_get("http://github.com/peeld/convert-public/releases/download/x")


if __name__ == "__main__":
    unittest.main()
