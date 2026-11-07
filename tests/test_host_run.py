"""convert.host (what the app tells this package) and convert.run (one conversion with no Op).

Run from the python folder:   python -m unittest discover -s tests -v
"""
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE / "fakecore"))

import _peelhost                                   # noqa: E402  the fake one
from convert import host                           # noqa: E402
from convert.conversion import Conversion, ConversionError   # noqa: E402
from convert.run import Cancelled, run_conversion  # noqa: E402
from test_convert import FAKE_FFMPEG, mp4          # noqa: E402


class HostTests(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["APPDATA"] = self.tmp.name

    def tearDown(self):
        host.configure(None)
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def test_the_app_names_itself(self):
        self.assertEqual(host.app_name(), _peelhost.APP_NAME)
        self.assertEqual(host.api(), 3)
        self.assertEqual(host.user_data_dir(), Path(self.tmp.name) / _peelhost.APP_NAME)

    def test_the_app_can_give_its_folder(self):
        _peelhost.user_data_dir = lambda: "/somewhere/else"
        try:
            self.assertEqual(host.user_data_dir(), Path("/somewhere/else"))
        finally:
            del _peelhost.user_data_dir

    def test_configure_wins_outside_an_app(self):
        host.configure("OtherApp")
        self.assertEqual(host.app_name(), "OtherApp")
        self.assertEqual(host.user_data_dir(), Path(self.tmp.name) / "OtherApp")

    def test_no_app_is_an_error(self):
        saved = host._peelhost
        host._peelhost = lambda: None          # no _peelhost: not inside an app
        os.environ.pop("PEEL_APP_NAME", None)
        try:
            with self.assertRaises(host.HostError):
                host.app_name()
            os.environ["PEEL_APP_NAME"] = "EnvApp"
            self.assertEqual(host.app_name(), "EnvApp")
            self.assertEqual(host.user_data_dir(), Path(self.tmp.name) / "EnvApp")
            self.assertEqual(host.api(), 0)
        finally:
            host._peelhost = saved


@unittest.skipIf(sys.platform == "win32", "the fake ffmpeg is a #! script")
class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tool = self.root / "ffmpeg"
        self.tool.write_text(FAKE_FFMPEG.format(python=sys.executable))
        self.tool.chmod(self.tool.stat().st_mode | stat.S_IEXEC)
        self.logs, self.fractions, self.statuses = [], [], []

    def tearDown(self):
        self.tmp.cleanup()

    def run_it(self, text="hello", conversion=None, **kw):
        src = self.root / "a.mov"
        src.write_text(text)
        out = self.root / "out" / "a.mp4"
        result = run_conversion(conversion or mp4(), str(src), str(out), str(self.tool),
                                log=lambda s, t: self.logs.append((s, t)), progress=self.fractions.append,
                                status=self.statuses.append, **kw)
        return result, out

    def test_writes_the_output_and_reports(self):
        result, out = self.run_it()
        self.assertEqual(result, str(out))
        self.assertEqual(out.read_text(), "converted:hello")
        self.assertEqual(list(out.parent.iterdir()), [out])        # no temp file left
        self.assertEqual(self.fractions, [0.2, 0.5, 1.0])
        self.assertIn("Converting: 50%", self.statuses)
        self.assertEqual(self.statuses[-1], "Done")
        self.assertIn(("stderr", "  Duration: 00:00:10.00, start: 0.000000"), self.logs)
        self.assertIn(("stdout", "progress 100%"), self.logs)

    def test_failure_raises_and_writes_nothing(self):
        with self.assertRaises(ConversionError) as caught:
            self.run_it("FAIL")
        self.assertIn("boom", str(caught.exception))
        self.assertFalse(list((self.root / "out").iterdir()))

    def test_existing_output_is_not_replaced(self):
        out = self.root / "out" / "a.mp4"
        out.parent.mkdir()
        out.write_text("keep")
        with self.assertRaises(ConversionError):
            self.run_it()
        self.assertEqual(out.read_text(), "keep")
        self.assertEqual(list(out.parent.iterdir()), [out])

    def test_cancel_terminates_the_tool(self):
        cancel = threading.Event()
        procs = []

        def cancel_soon():
            while not procs:
                time.sleep(0.05)
            time.sleep(0.3)
            cancel.set()
            procs[0].terminate()

        threading.Thread(target=cancel_soon, daemon=True).start()
        started = time.monotonic()
        with self.assertRaises(Cancelled):
            self.run_it("SLOW", cancel=cancel, on_process=procs.append)
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(list((self.root / "out").iterdir()))

    def test_a_conversion_that_runs_itself(self):
        class Upper(Conversion):
            id, label, group, output_ext, runs_itself = "t.up", "Upper", "Text", "txt", True

            def run(self, job, cancel, on_process=None):
                Path(job.output).write_text(Path(job.input).read_text().upper())

        result, out = self.run_it("abc", conversion=Upper())
        self.assertEqual(Path(result).read_text(), "ABC")
        self.assertEqual(self.statuses[-1], "Done")


if __name__ == "__main__":
    unittest.main()
