import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fakecore"))

from convert import devfolder, updater  # noqa: E402


def make_repo(root):
    (root / "convert").mkdir(parents=True)
    for f in ("convert/__init__.py", "convert_startup.py", "manifest.json"):
        (root / f).write_text("")


class DevFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = updater.Settings(self.root / "settings.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_off_by_default(self):
        self.assertEqual(devfolder.current(self.settings), "")

    def test_a_clone_can_be_selected_and_cleared(self):
        make_repo(self.root / "clone")
        devfolder.set_folder(self.settings, self.root / "clone")
        self.assertEqual(Path(devfolder.current(self.settings)), self.root / "clone")
        devfolder.set_folder(self.settings, "")
        self.assertEqual(devfolder.current(self.settings), "")

    def test_a_wrong_folder_is_refused_and_nothing_is_stored(self):
        (self.root / "other").mkdir()
        with self.assertRaises(ValueError) as cm:
            devfolder.set_folder(self.settings, self.root / "other")
        self.assertIn("missing convert/__init__.py", str(cm.exception))
        self.assertEqual(devfolder.current(self.settings), "")
        with self.assertRaises(ValueError):
            devfolder.set_folder(self.settings, self.root / "nope")

    def test_other_settings_survive(self):
        (self.root / "settings.json").write_text(json.dumps({"tools": {"a": "b"}}))
        make_repo(self.root / "clone")
        devfolder.set_folder(self.settings, self.root / "clone")
        self.assertEqual(json.loads((self.root / "settings.json").read_text())["tools"], {"a": "b"})

    def test_clone_refuses_a_non_empty_folder_and_reports_git_errors(self):
        (self.root / "full").mkdir()
        (self.root / "full" / "x").write_text("")
        self.assertIn("not empty", devfolder.clone(self.root / "full"))
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(returncode=128, stderr="fatal: no network", stdout="")
        self.assertEqual(devfolder.clone(self.root / "new", run=fake_run), "fatal: no network")
        self.assertEqual(calls[0][:2], ["git", "clone"])
        self.assertIsNone(devfolder.clone(self.root / "ok", run=lambda *a, **k: SimpleNamespace(returncode=0)))

    def test_git_available(self):
        self.assertTrue(devfolder.git_available(lambda name: "/usr/bin/git"))
        self.assertFalse(devfolder.git_available(lambda name: None))


if __name__ == "__main__":
    unittest.main()
