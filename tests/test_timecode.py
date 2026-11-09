"""convert.timecode and the timecode operations (Burn In Timecode, Export WAV) without ffmpeg."""
import os
import struct
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "fakecore"))
sys.path.insert(0, str(HERE.parent))

from convert import conversion as conv_mod, timecode, tools     # noqa: E402
from convert.conversion import ConversionError                  # noqa: E402
from convert.run import run_conversion                          # noqa: E402

NTSC = Fraction(30000, 1001)


def ops():
    registry = conv_mod.registry
    registry.load_all()
    from convert_user_timecode_ops import BurnInTimecode, ExportWav, set_time_reference, start_at
    return registry, BurnInTimecode, ExportWav, set_time_reference, start_at


class TimecodeTests(unittest.TestCase):
    def test_rates(self):
        self.assertEqual(timecode.parse_rate("30000/1001"), NTSC)
        self.assertEqual(timecode.parse_rate("25"), 25)
        for bad in ("", None, "x", "0", "-1", "1/0"):
            self.assertIsNone(timecode.parse_rate(bad), bad)
        self.assertEqual(timecode.rate_text(NTSC), "30000/1001")
        self.assertEqual(timecode.nominal(NTSC), 30)

    def test_non_drop(self):
        self.assertEqual(timecode.tc_frames("01:00:00:00", 25), 90000)
        self.assertEqual(timecode.frames_tc(90001, 25), "01:00:00:01")
        self.assertFalse(timecode.is_drop("01:00:00:00"))

    def test_drop_frame_known_values(self):
        # 29.97 DF: frames 0 and 1 of each minute are skipped, except every tenth minute.
        self.assertEqual(timecode.tc_frames("00:00:59;29", NTSC), 1799)
        self.assertEqual(timecode.tc_frames("00:01:00;02", NTSC), 1800)
        self.assertEqual(timecode.tc_frames("00:10:00;00", NTSC), 17982)
        self.assertEqual(timecode.tc_frames("01:00:00;00", NTSC), 107892)
        self.assertEqual(timecode.frames_tc(1800, NTSC, drop=True), "00:01:00;02")
        self.assertEqual(timecode.frames_tc(17982, NTSC, drop=True), "00:10:00;00")
        self.assertTrue(timecode.is_drop("01:00:00;00"))

    def test_drop_frame_round_trip(self):
        for rate in (NTSC, Fraction(60000, 1001)):
            for n in list(range(0, 4000)) + [107891, 107892, 863999, 1000000]:
                text = timecode.frames_tc(n, rate, drop=True)
                self.assertEqual(timecode.tc_frames(text, rate), n, (rate, n, text))

    def test_an_hour_of_drop_frame_is_an_hour_of_real_time(self):
        self.assertAlmostEqual(float(timecode.seconds("01:00:00;00", NTSC)), 3600, delta=0.004)

    def test_bad_timecode(self):
        with self.assertRaises(ValueError):
            timecode.tc_frames("1:00:00", 25)


class StartAtTests(unittest.TestCase):
    def test_same_rate_converted_rate_and_none(self):
        start_at = ops()[4]
        self.assertEqual(start_at("01:00:00:00", Fraction(25), Fraction(25)), ("01:00:00:00", []))
        self.assertEqual(start_at("01:00:00:00", None, Fraction(25)), ("01:00:00:00", []))
        tc, warnings = start_at("01:00:00:00", Fraction(25), Fraction(50))
        self.assertEqual(tc, "01:00:00:00")             # the same instant at 50 fps
        self.assertTrue(warnings)
        self.assertEqual(start_at("", None, Fraction(25))[0], "00:00:00:00")


def _bwf(path, sample_rate=48000):
    """A minimal RIFF WAVE with fmt, bext (zeroed) and an empty data chunk."""
    fmt = struct.pack("<HHIIHH", 1, 1, sample_rate, sample_rate * 3, 3, 24)
    bext = bytes(602)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"bext" + struct.pack("<I", len(bext)) + bext + b"data" + struct.pack("<I", 0)
    Path(path).write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


class TimeReferenceTests(unittest.TestCase):
    def test_writes_samples_since_midnight(self):
        set_time_reference = ops()[3]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.wav")
            _bwf(path)
            self.assertEqual(set_time_reference(path, "01:00:00:00", Fraction(25)), (172800000, 48000))
            data = Path(path).read_bytes()
            at = data.index(b"bext") + 8 + 338
            self.assertEqual(struct.unpack("<Q", data[at:at + 8])[0], 172800000)
            # 29.97 DF: an hour of labels is 3600 s less 3.6 ms of real time
            ref, _ = set_time_reference(path, "01:00:00;00", NTSC)
            self.assertEqual(ref, int(Fraction(107892) / NTSC * 48000))

    def test_not_a_wav(self):
        set_time_reference = ops()[3]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.wav")
            Path(path).write_bytes(b"nope" * 10)
            with self.assertRaises(ConversionError):
                set_time_reference(path, "01:00:00:00", Fraction(25))


class DefinitionTests(unittest.TestCase):
    def test_registered_and_described_as_data(self):
        registry = ops()[0]
        self.assertEqual(registry.problems, [])
        burn, wav = registry.get("video.burn-in-timecode"), registry.get("audio.export-bwf")
        self.assertEqual(burn.output_name("/x/a.MOV"), "a.tc.mov")
        self.assertEqual(wav.output_name("/x/a.mp4"), "a.wav")
        d = {e["id"]: e for e in registry.describe()}
        self.assertEqual(d["video.burn-in-timecode"]["values"], ["tc_start", "tc_rate", "video_rate"])
        self.assertEqual(d["audio.export-bwf"]["values"], ["tc_start", "tc_rate"])
        self.assertEqual(d["video.to-mp4"]["values"], [])

    def test_unknown_value_is_a_problem(self):
        c = conv_mod.Conversion()
        c.values = ("tc_start", "colour")
        self.assertIn("unknown values: colour", c.problems())

    def test_run_conversion_passes_declared_values_and_returns_details(self):
        seen = {}

        class Echo(conv_mod.Conversion):
            id, label, group, output_ext, runs_itself = "echo", "Echo", "G", "txt", True
            values = ("tc_start",)

            def run(self, job, cancel, on_process=None):
                seen.update(job.values)
                Path(job.output).write_text("x")
                job.details["used"] = job.values.get("tc_start")

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "a.mov")
            Path(src).write_text("v")
            details = {}
            run_conversion(Echo(), src, os.path.join(tmp, "a.txt"), "tool",
                           values={"tc_start": "01:00:00:00", "video_rate": "25", "tc_rate": ""},
                           details=details)
        self.assertEqual(seen, {"tc_start": "01:00:00:00"})    # only what it declares, nothing empty
        self.assertEqual(details, {"used": "01:00:00:00"})


class HostToolTests(unittest.TestCase):
    def test_the_host_says_which_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, "my-ffprobe")
            Path(exe).write_text("")
            old = os.environ.get("PEEL_TOOL_FFPROBE")
            os.environ["PEEL_TOOL_FFPROBE"] = exe
            try:
                self.assertEqual(tools.find("ffprobe"), exe)
                os.environ["PEEL_TOOL_FFPROBE"] = os.path.join(tmp, "missing")
                self.assertNotEqual(tools.find("ffprobe"), os.path.join(tmp, "missing"))
            finally:
                if old is None:
                    os.environ.pop("PEEL_TOOL_FFPROBE", None)
                else:
                    os.environ["PEEL_TOOL_FFPROBE"] = old


if __name__ == "__main__":
    unittest.main()
