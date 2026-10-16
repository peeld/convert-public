"""Run from the python folder:   python -m unittest discover -s tests -v

Uses a fake PeelCore (tests/fakecore) and a fake "ffmpeg" (a Python script), so it
needs neither the C++ build nor a real ffmpeg.
"""
import json
import ntpath
import os
import posixpath
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE / "fakecore"))

from convert import conversion as conv_mod            # noqa: E402
from convert import paths                             # noqa: E402
from convert.conversion import Conversion, ConversionRegistry, Job   # noqa: E402


def mp4():
    c = Conversion()
    c.id, c.label, c.inputs, c.output_ext = "t.mp4", "To MP4", ("mov", "mkv"), "mp4"
    c.group = "Video"
    c.command = ("-i", "{input}", "{output}")
    return c


class PathRuleTests(unittest.TestCase):
    """The table in the design plan, for Windows paths, whatever machine runs this."""
    pm = ntpath
    src = "C:\\v\\a.mov"

    def plan(self, conv, out_text, existing=("C:\\v",)):
        exists = lambda p: ntpath.normcase(ntpath.normpath(p)) in {ntpath.normcase(e) for e in existing}
        return paths.output_path(conv, self.src, out_text, self.pm, exists)

    def test_blank_is_the_source_folder(self):
        d, out = self.plan(mp4(), "")
        self.assertEqual(out, "C:\\v\\a.mp4")
        self.assertFalse(d.create)

    def test_blank_same_extension_is_rejected(self):
        c = mp4()
        c.output_ext = "mov"
        _, out = self.plan(c, "")
        self.assertEqual(paths.check_output(self.src, out, self.pm, lambda p: False),
                         ["output would overwrite its input"])

    def test_case_is_ignored_on_windows(self):
        self.assertEqual(paths.check_output("C:\\v\\a.mov", "c:\\V\\A.MOV", self.pm, lambda p: False),
                         ["output would overwrite its input"])

    def test_name_is_a_subfolder_created_silently(self):
        d, out = self.plan(mp4(), "mp4")
        self.assertEqual(out, "C:\\v\\mp4\\a.mp4")
        self.assertTrue(d.create)
        self.assertFalse(d.ask)

    def test_trailing_separator_makes_it_a_path_and_asks(self):
        d, out = self.plan(mp4(), "mp4\\")
        self.assertEqual(d.path, "C:\\v\\mp4")
        self.assertTrue(d.ask)

    def test_full_path_asks_only_when_missing(self):
        d, _ = self.plan(mp4(), "C:\\v2")        # a bare name would be a subfolder; with a drive it is a path
        self.assertTrue(d.ask)
        d, _ = self.plan(mp4(), "C:\\v")
        self.assertFalse(d.create)
        self.assertFalse(d.ask)

    def test_same_folder_same_extension_via_full_path(self):
        c = mp4()
        c.output_ext = "mov"
        _, out = self.plan(c, "C:\\v")
        self.assertEqual(paths.check_output(self.src, out, self.pm, lambda p: False),
                         ["output would overwrite its input"])
        _, out = self.plan(c, "C:\\v2")
        self.assertEqual(paths.check_output(self.src, out, self.pm, lambda p: False), [])

    def test_existing_output_is_invalid(self):
        self.assertEqual(paths.check_output(self.src, "C:\\v\\a.mp4", self.pm, lambda p: True), ["output exists"])

    def test_posix_paths_work_too(self):
        d, out = paths.output_path(mp4(), "/v/a.mov", "out", posixpath, lambda p: True)
        self.assertEqual(out, "/v/out/a.mp4")
        d, out = paths.output_path(mp4(), "/v/a.mov", "/x/y", posixpath, lambda p: False)
        self.assertEqual(out, "/x/y/a.mp4")
        self.assertTrue(d.ask)

    def test_temp_name_keeps_the_extension(self):
        self.assertEqual(paths.temp_name("C:\\v\\a.mp4", ntpath), "C:\\v\\a.converting.mp4")


class ConversionTests(unittest.TestCase):
    def test_accepts_by_extension(self):
        c = mp4()
        self.assertTrue(c.accepts("X.MOV"))
        self.assertFalse(c.accepts("x.wav"))
        c.inputs = ()
        self.assertTrue(c.accepts("anything.xyz"))

    def test_build_command(self):
        self.assertEqual(mp4().build_command(Job("in x.mov", "out.mp4")), ["-i", "in x.mov", "out.mp4"])

    def test_problems(self):
        c = Conversion()
        self.assertIn("no id", c.problems())
        c = mp4()
        c.command = ("-i", "{input}")
        self.assertEqual(c.problems(), ["command has no {output}"])

    def test_shipped_definitions_are_valid(self):
        r = ConversionRegistry()
        r.load_dir(conv_mod.shipped_dir())
        self.assertEqual(r.problems, [])
        self.assertGreaterEqual(len(r.all()), 9)
        self.assertIsNotNone(r.get("video.to-mp4"))


class LoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        (self.dir / name).write_text(textwrap.dedent(text), encoding="utf-8")

    def load(self):
        r = ConversionRegistry()
        r.load_dir(self.dir)
        return r

    def test_json_object_and_list(self):
        self.write("a.json", """{"id":"a","label":"A","group":"G","output_ext":"x","command":["{input}","{output}"]}""")
        self.write("b.json", """[{"id":"b","label":"B","group":"G","output_ext":"y","command":["{input}","{output}"]},
                                 {"id":"c","label":"C","group":"G","output_ext":"z","command":["{input}","{output}"]}]""")
        self.assertEqual([c.id for c in self.load().all()], ["a", "b", "c"])

    def test_bad_definitions_are_skipped_and_reported(self):
        self.write("bad.json", """[{"id":"x","label":"X","group":"G","output_ext":"x"},
                                   {"id":"y","label":"Y","group":"G","output_ext":"y","command":["{input}","{output}"],"bogus":1},
                                   {"id":"ok","label":"OK","group":"G","output_ext":"o","command":["{input}","{output}"]}]""")
        self.write("broken.json", "{not json")
        r = self.load()
        self.assertEqual([c.id for c in r.all()], ["ok"])
        self.assertEqual(len(r.problems), 3)

    def test_python_file_registers_with_decorator(self):
        self.write("mine.py", """
            from convert import Conversion, register

            @register
            class Mine(Conversion):
                id = "mine"
                label = "Mine"
                group = "G"
                output_ext = "m"
                command = ("{input}", "{output}")
        """)
        r = ConversionRegistry()
        old = conv_mod.registry
        try:
            conv_mod.registry = r
            import convert
            convert.registry = r
            r.load_dir(self.dir)
        finally:
            conv_mod.registry = old
            convert.registry = old
        self.assertIsNotNone(r.get("mine"))

    def test_a_missing_or_blank_group_is_rejected(self):
        self.write("g.json", """[{"id":"n","label":"N","output_ext":"x","command":["{input}","{output}"]},
                                 {"id":"b","label":"B","group":"  ","output_ext":"x","command":["{input}","{output}"]},
                                 {"id":"ok","label":"OK","group":"Audio","output_ext":"x","command":["{input}","{output}"]}]""")
        r = self.load()
        self.assertIsNone(r.get("n"))
        self.assertIsNone(r.get("b"))
        self.assertIsNotNone(r.get("ok"))
        self.assertEqual(len(r.problems), 2)
        self.assertTrue(all("no group" in line for line in r.problems))

    def test_json_names_a_class_in_a_py_file_beside_it(self):
        self.write("custom.py", """
            from convert import Conversion

            class Doubler(Conversion):
                def build_command(self, job):
                    return ["--twice", job.input, job.output]
        """)
        self.write("c.json", """{"id":"d","label":"D","group":"G","output_ext":"d","inputs":["wav"],"class":"custom.Doubler"}""")
        c = self.load().get("d")
        self.assertIsNotNone(c)
        self.assertEqual(c.build_command(Job("i", "o")), ["--twice", "i", "o"])

    def test_later_folder_replaces_same_id(self):
        self.write("a.json", """{"id":"a","label":"First","group":"G","output_ext":"x","command":["{input}","{output}"]}""")
        other = Path(tempfile.mkdtemp())
        (other / "a.json").write_text("""{"id":"a","label":"Second","group":"G","output_ext":"x","command":["{input}","{output}"]}""")
        r = ConversionRegistry()
        r.load_dir(self.dir)
        r.load_dir(other)
        self.assertEqual(r.get("a").label, "Second")


class UserPythonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self.tmp.name
        self.root = Path(self.tmp.name) / "PeelConvert"

    def tearDown(self):
        if self.old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self.old_appdata
        self.tmp.cleanup()

    def test_user_conversions_live_inside_userpython(self):
        from convert.conversion import user_dir, user_python_dir
        self.assertEqual(user_python_dir(), self.root / "userPython")
        self.assertEqual(user_dir(), self.root / "userPython" / "conversions")

    def test_old_folder_is_migrated_without_overwriting(self):
        from convert.conversion import migrate_user_conversions, user_dir
        old = self.root / "conversions"
        old.mkdir(parents=True)
        (old / "a.json").write_text("A")
        (old / "b.json").write_text("B")
        user_dir().mkdir(parents=True)
        (user_dir() / "b.json").write_text("NEW B")
        moved, left = migrate_user_conversions()
        self.assertEqual((moved, left), (["a.json"], ["b.json"]))
        self.assertEqual((user_dir() / "a.json").read_text(), "A")
        self.assertEqual((user_dir() / "b.json").read_text(), "NEW B")
        self.assertTrue((old / "b.json").exists())      # left for the user to sort out
        self.assertEqual(migrate_user_conversions(), ([], ["b.json"]))

    def test_nothing_to_migrate(self):
        from convert.conversion import migrate_user_conversions
        self.assertEqual(migrate_user_conversions(), ([], []))

    def test_user_conversion_overrides_a_shipped_one_and_is_marked(self):
        from convert.conversion import shipped_dir
        shipped_id = ConversionRegistry()
        shipped_id.load_dir(shipped_dir())
        some = shipped_id.all()[0].id
        user = Path(self.tmp.name) / "u"
        user.mkdir()
        (user / "x.json").write_text(json.dumps({"id": some, "label": "Mine", "group": "G", "output_ext": "x",
                                                 "command": ["{input}", "{output}"]}))
        (user / "y.json").write_text(json.dumps({"id": "brand.new", "label": "New", "group": "G", "output_ext": "x",
                                                 "command": ["{input}", "{output}"]}))
        r = ConversionRegistry()
        r.load_all([user])
        self.assertEqual(r.get(some).label, "Mine")
        self.assertEqual(r.overrides, {some})
        r.load_all([])
        self.assertEqual(r.overrides, set())


FAKE_FFMPEG = r'''#!{python}
import sys, time
args = sys.argv[1:]
out = args[-1]
mode = open(out.rsplit(".converting", 1)[0] + ".mode").read().strip() if False else "ok"
inp = args[args.index("-i") + 1]
text = open(inp).read()
sys.stderr.write("  Duration: 00:00:10.00, start: 0.000000\n")
sys.stderr.flush()
if text.startswith("FAIL"):
    sys.stderr.write("boom: bad input\n"); sys.exit(1)
if text.startswith("SLOW"):
    time.sleep(30)
for i in (2, 5, 10):
    print("out_time_us=%d" % (i * 1000000)); sys.stdout.flush()
open(out, "w").write("converted:" + text)
print("progress=end")
'''


class OpTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.appdata = self.root / "appdata"
        self.appdata.mkdir()
        self._env = dict(os.environ)
        os.environ["APPDATA"] = str(self.appdata)
        fake = self.root / "ffmpeg"
        fake.write_text(FAKE_FFMPEG.format(python=sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        (self.appdata / "PeelConvert").mkdir()
        (self.appdata / "PeelConvert" / "settings.json").write_text(json.dumps({"tools": {"ffmpeg": str(fake)}}))
        conv_mod.registry.clear()
        conv_mod.registry.register(mp4())
        from convert import ops
        from PeelCore import work
        self.ops, self.work = ops, work

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def make(self, text="hello", name="a.mov", output=None):
        src = self.root / name
        src.write_text(text)
        op = self.ops.ConvertOp(name)
        op.setProperty("conversion", "t.mp4")
        op.setProperty("input", str(src))
        out = output or str(self.root / "out" / (src.stem + ".mp4"))
        op.setProperty("output", out)
        return op, src, Path(out)

    def run_op(self, op, timeout=20):
        self.assertEqual(op.state(), self.work.OpState.Ready, op.validationProblems())
        self.assertTrue(op.launch(self.work.Params(), {}))
        self.assertTrue(op.settled.wait(timeout), "op did not settle")



class OpTests(OpTestBase):
    def test_converts_and_renames_the_temp_file(self):
        op, src, out = self.make()
        self.run_op(op)
        self.assertEqual(op.state(), self.work.OpState.Finished)
        self.assertEqual(out.read_text(), "converted:hello")
        self.assertFalse(list(out.parent.glob("*.converting.*")))
        self.assertEqual(op.outputs().value("output"), str(out))
        text = [t for _, t in op.logs()]
        self.assertIn("progress 100%", text)

    def test_reports_progress_and_status(self):
        op, src, out = self.make()
        self.run_op(op)
        self.assertEqual(op.progress(), 1.0)
        self.assertEqual(op.status(), "Done")
        self.assertIn("Waiting for a free slot", op.status_history)
        self.assertIn("Converting: 50%", op.status_history)

    def test_failure_leaves_no_file(self):
        op, src, out = self.make("FAIL please")
        self.run_op(op)
        self.assertEqual(op.state(), self.work.OpState.Failed)
        self.assertIn("boom", op.error)
        self.assertFalse(out.exists())
        self.assertFalse(list(out.parent.glob("*")))

    def test_cancel_kills_the_process_and_removes_the_part_file(self):
        op, src, out = self.make("SLOW")
        self.assertTrue(op.launch(self.work.Params(), {}))
        import time
        time.sleep(1.0)
        op.requestCancel()
        self.assertTrue(op.settled.wait(10))
        self.assertEqual(op.state(), self.work.OpState.Cancelled)
        self.assertFalse(out.exists())

    def test_validation(self):
        op, src, out = self.make()
        self.assertEqual(op.state(), self.work.OpState.Ready)
        op.setProperty("output", str(src))
        self.assertIn("output would overwrite its input", op.validationProblems())
        op.setProperty("output", str(out))
        out.parent.mkdir(exist_ok=True)
        out.write_text("already")
        op.revalidate()
        self.assertIn("output exists", op.validationProblems())
        op.setProperty("conflict", "same output as other.mov")
        self.assertIn("same output as other.mov", op.validationProblems())

    def test_wrong_extension_and_missing_file(self):
        op, src, out = self.make(name="a.wav")
        self.assertTrue(any("does not accept" in p for p in op.validationProblems()))
        op.setProperty("input", str(self.root / "nope.mov"))
        self.assertIn("input file not found", op.validationProblems())

    def test_missing_tool_is_reported(self):
        (self.appdata / "PeelConvert" / "settings.json").write_text("{}")
        from convert import tools
        orig = tools.shutil.which
        tools.shutil.which = lambda name: None
        try:
            op, src, out = self.make()
            self.assertIn("ffmpeg is not installed", op.validationProblems())
        finally:
            tools.shutil.which = orig

    def test_slots_run_one_at_a_time(self):
        from convert import runner
        import threading
        cancel = threading.Event()
        self.assertTrue(runner.acquire(cancel))
        got = []
        t = threading.Thread(target=lambda: got.append(runner.acquire(cancel)))
        t.start()
        t.join(0.4)
        self.assertTrue(t.is_alive(), "second acquire should wait for the slot")
        cancel.set()
        t.join(2)
        self.assertEqual(got, [False])
        runner.release()


class ToolTests(unittest.TestCase):
    CHECKSUMS = ("aaa111  ffmpeg-master-latest-win64-gpl.zip\n"
                 "BBB222 *ffmpeg-master-latest-win64-lgpl.zip\n"
                 "ccc333  ./other.zip\n")

    def test_unknown_tool_cannot_be_installed(self):
        from convert import tools
        self.assertTrue(tools.install_problems("no-such-tool"))

    def test_ffmpeg_is_downloadable_on_windows_with_a_checksum_source(self):
        from convert import tools
        orig = tools.platform_key
        tools.platform_key = lambda: "windows-x86_64"
        try:
            self.assertEqual(tools.install_problems("ffmpeg"), [])
            spec = tools.manifest()["ffmpeg"]["download"]["windows-x86_64"]
            self.assertIn("BtbN/FFmpeg-Builds", spec["url"])
            self.assertTrue(spec["url"].endswith("win64-lgpl.zip"))
            self.assertTrue(spec["sha256_url"].endswith("checksums.sha256"))
        finally:
            tools.platform_key = orig

    def test_ffprobe_comes_with_ffmpeg(self):
        from convert import tools
        self.assertEqual(tools.owner("ffprobe"), "ffmpeg")
        self.assertEqual(tools.owner("ffmpeg"), "ffmpeg")
        orig = tools.platform_key
        tools.platform_key = lambda: "windows-x86_64"
        try:
            self.assertEqual(tools.install_problems("ffprobe"), [])      # installs ffmpeg's zip
        finally:
            tools.platform_key = orig

    def test_ffprobe_is_found_beside_a_configured_ffmpeg(self):
        from convert import tools
        tmp = tempfile.TemporaryDirectory()
        saved = os.environ.get("APPDATA")
        try:
            os.environ["APPDATA"] = tmp.name
            (Path(tmp.name) / "PeelConvert").mkdir()
            bin_dir = Path(tmp.name) / "bin"
            bin_dir.mkdir()
            names = ["ffmpeg.exe", "ffprobe.exe"] if sys.platform == "win32" else ["ffmpeg", "ffprobe"]
            for name in names:
                (bin_dir / name).write_text("x")
            (Path(tmp.name) / "PeelConvert" / "settings.json").write_text(
                json.dumps({"tools": {"ffmpeg": str(bin_dir / names[0])}}))
            self.assertEqual(Path(tools.find("ffprobe")), bin_dir / names[1])
        finally:
            if saved is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = saved
            tmp.cleanup()

    def test_parse_checksums_picks_the_line_for_the_file(self):
        from convert import tools
        self.assertEqual(tools.parse_checksums(self.CHECKSUMS, "ffmpeg-master-latest-win64-lgpl.zip"), "bbb222")
        self.assertEqual(tools.parse_checksums(self.CHECKSUMS, "other.zip"), "ccc333")
        self.assertEqual(tools.parse_checksums(self.CHECKSUMS, "missing.zip"), "")
        self.assertEqual(tools.parse_checksums("DEADBEEF\n"), "deadbeef")

    def test_download_with_wrong_checksum_is_refused(self):
        import hashlib, zipfile, io
        from convert import tools
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("ffmpeg-x/bin/ffmpeg", "#!/bin/sh\n")
        data = buf.getvalue()
        tmp = tempfile.TemporaryDirectory()
        saved = (tools.platform_key, tools._fetch, tools._expected_hash, tools.user_data_dir, tools.manifest)
        try:
            tools.platform_key = lambda: "test-x"
            tools.user_data_dir = lambda: Path(tmp.name)
            tools.manifest = lambda: {"fake": {"exe": "ffmpeg", "download": {"test-x": {
                "url": "http://x/f.zip", "sha256_url": "http://x/c", "path": "*/bin/ffmpeg"}}}}
            tools._fetch = lambda url, dest, progress, cancel: Path(dest).write_bytes(data)
            tools._expected_hash = lambda spec: "0" * 64
            with self.assertRaises(tools.ToolError):
                tools.install("fake")
            self.assertEqual(list((Path(tmp.name) / "tools").glob("*")), [])       # nothing left behind
            tools._expected_hash = lambda spec: hashlib.sha256(data).hexdigest()
            exe = tools.install("fake")
            self.assertTrue(Path(exe).is_file())
        finally:
            (tools.platform_key, tools._fetch, tools._expected_hash, tools.user_data_dir, tools.manifest) = saved
            tmp.cleanup()


# ---- metadata (.meta) -------------------------------------------------------------------
RAW_MOV = {
    "format": {"format_name": "mov,mp4", "format_long_name": "QuickTime / MOV", "duration": "10.01",
               "size": "1000", "bit_rate": "800", "tags": {"encoder": "x"}},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "prores", "width": 1920, "height": 1080,
         "avg_frame_rate": "30000/1001", "nb_frames": "300", "duration": "10.01",
         "tags": {"timecode": "01:02:03;04"},
         "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]},
        {"index": 1, "codec_type": "audio", "codec_name": "pcm_s24le", "sample_rate": "48000", "channels": 2},
        {"index": 2, "codec_type": "data", "codec_tag_string": "tmcd", "tags": {"timecode": "01:02:03;04"}},
    ],
}


class MediaInfoTests(unittest.TestCase):
    def meta(self, raw=RAW_MOV, path="/x/foo.mov"):
        from convert import mediainfo
        return mediainfo.build_meta(raw, path)

    def test_video_fields(self):
        v = self.meta()["video"][0]
        self.assertEqual(v["resolution"], "1920x1080")
        self.assertEqual((v["width"], v["height"]), (1920, 1080))
        self.assertEqual(v["frame_count"], 300)
        self.assertEqual(v["frame_count_source"], "container")
        self.assertAlmostEqual(v["frame_rate"], 29.97003, places=5)
        self.assertEqual(v["frame_rate_text"], "30000/1001")
        self.assertEqual(v["rotation"], -90)
        self.assertEqual(v["timecode"], "01:02:03;04")

    def test_top_level_timecode_and_file(self):
        m = self.meta()
        self.assertEqual(m["schema"], 1)
        self.assertEqual(m["timecode"], "01:02:03;04")
        self.assertEqual(m["file"]["name"], "foo.mov")
        self.assertEqual(m["file"]["duration"], 10.01)
        self.assertEqual(m["audio"][0]["sample_rate"], 48000)

    def test_timecode_is_null_when_absent(self):
        raw = {"format": {}, "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264",
                                          "width": 2, "height": 2, "r_frame_rate": "25/1"}]}
        m = self.meta(raw)
        self.assertIn("timecode", m)
        self.assertIsNone(m["timecode"])
        self.assertNotIn("timecode", m["video"][0])

    def test_frame_count_falls_back_to_packets_then_estimate(self):
        stream = {"index": 0, "codec_type": "video", "r_frame_rate": "25/1", "duration": "4.0"}
        v = self.meta({"format": {}, "streams": [dict(stream, nb_read_packets="99")]})["video"][0]
        self.assertEqual((v["frame_count"], v["frame_count_source"]), (99, "packets"))
        v = self.meta({"format": {}, "streams": [stream]})["video"][0]
        self.assertEqual((v["frame_count"], v["frame_count_source"]), (100, "estimated"))

    def test_junk_values_are_left_out(self):
        raw = {"format": {"duration": "N/A"}, "streams": [{"index": 0, "codec_type": "video",
               "avg_frame_rate": "0/0", "r_frame_rate": "0/0", "nb_frames": "N/A", "bit_rate": "N/A"}]}
        m = self.meta(raw)
        self.assertNotIn("duration", m["file"])
        for key in ("frame_rate", "frame_count", "bit_rate"):
            self.assertNotIn(key, m["video"][0])


FAKE_FFPROBE = r"""#!{python}
import json, sys
args = sys.argv[1:]
path = args[args.index("-i") + 1]
text = open(path).read()
if text.startswith("FAIL"):
    sys.stderr.write("moov atom not found\n"); sys.exit(1)
if text.startswith("SLOW"):
    import time; time.sleep(30)
video = {{"index": 0, "codec_type": "video", "codec_name": "h264", "width": 640, "height": 360,
          "avg_frame_rate": "25/1", "tags": {{"timecode": "10:00:00:00"}}}}
if "-count_packets" in args:
    video["nb_read_packets"] = "50"
print(json.dumps({{"format": {{"format_name": "mov", "duration": "2.0"}}, "streams": [video]}}))
"""


class MetaOpTests(OpTestBase):
    def setUp(self):
        super().setUp()
        fake = self.root / "ffprobe"
        fake.write_text(FAKE_FFPROBE.format(python=sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        settings = self.appdata / "PeelConvert" / "settings.json"
        data = json.loads(settings.read_text())
        data["tools"]["ffprobe"] = str(fake)
        settings.write_text(json.dumps(data))
        conv_mod.registry.register(self._meta_conversion())

    @staticmethod
    def _meta_conversion():
        import importlib.util
        path = Path(conv_mod.shipped_dir()) / "metadata.py"
        spec = importlib.util.spec_from_file_location("convert_meta_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.MediaMeta()

    def make_meta(self, text="hello", name="a.mov"):
        src = self.root / name
        src.write_text(text)
        op = self.ops.ConvertOp(name)
        op.setProperty("conversion", "media.meta")
        op.setProperty("input", str(src))
        op.setProperty("output", str(self.root / (name + ".meta")))
        return op, src, self.root / (name + ".meta")

    def test_output_is_input_name_plus_meta(self):
        self.assertEqual(self._meta_conversion().output_name("C:/clips/foo.mov"), "foo.mov.meta")

    def test_writes_json_beside_the_source(self):
        op, src, out = self.make_meta()
        self.run_op(op)
        self.assertEqual(op.state(), self.work.OpState.Finished)
        meta = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(meta["video"][0]["resolution"], "640x360")
        self.assertEqual(meta["video"][0]["frame_count"], 50)          # second pass: packets
        self.assertEqual(meta["timecode"], "10:00:00:00")
        self.assertEqual(sorted(p.name for p in self.root.glob("a.*")), ["a.mov", "a.mov.meta"])

    def test_unreadable_file_fails_with_ffprobes_message_and_leaves_nothing(self):
        op, src, out = self.make_meta("FAIL")
        self.run_op(op)
        self.assertEqual(op.state(), self.work.OpState.Failed)
        self.assertIn("moov atom not found", op.error)
        self.assertFalse(out.exists())
        self.assertEqual(list(self.root.glob("*.converting*")), [])

    def test_cancel_stops_ffprobe(self):
        op, src, out = self.make_meta("SLOW")
        self.assertTrue(op.launch(self.work.Params(), {}))
        import time; time.sleep(1)
        op.requestCancel()
        self.assertTrue(op.settled.wait(10))
        self.assertEqual(op.state(), self.work.OpState.Cancelled)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
