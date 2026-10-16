"""Tests for convert/fbxinfo.py and the fbx.meta conversion.

Timecode maths and build_meta() use fake scenes, so they run anywhere.  Tests that read a
real file need the ufbx module; the ones using the two mocap files also need testdata/mocap
(fetched with the mocap-testdata package; never in git) and skip when either is missing."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace as NS

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE / "fakecore"))

from convert import conversion as conv_mod   # noqa: E402
from convert import fbxinfo                  # noqa: E402

HAVE_UFBX = importlib.util.find_spec("ufbx") is not None


def mocap_dir():
    for base in (HERE.parent.parent / "testdata" / "mocap", Path(os.environ.get("PEEL_MOCAP_TESTDATA", "."))):
        if (base / "Brandon1_sc005_002.fbx").is_file():
            return base
    return None


class TimecodeTests(unittest.TestCase):
    def test_non_drop(self):
        self.assertEqual(fbxinfo.timecode_text(1562809, Fraction(30), False), "14:28:13:19")
        self.assertEqual(fbxinfo.timecode_text(0, Fraction(24), False), "00:00:00:00")
        self.assertEqual(fbxinfo.timecode_text(24 * 3600 * 25, Fraction(24), False), "25:00:00:00")

    def test_high_frame_rate_pads_frames(self):
        self.assertEqual(fbxinfo.timecode_text(1001, Fraction(120), False), "00:00:08:041")

    def test_drop_frame(self):
        r = Fraction(30000, 1001)
        self.assertEqual(fbxinfo.timecode_text(1799, r, True), "00:00:59;29")
        self.assertEqual(fbxinfo.timecode_text(1800, r, True), "00:01:00;02")
        self.assertEqual(fbxinfo.timecode_text(17981, r, True), "00:09:59;29")
        self.assertEqual(fbxinfo.timecode_text(17982, r, True), "00:10:00;00")
        self.assertEqual(fbxinfo.timecode_text(107892, r, True), "01:00:00;00")

    def test_frame_of_and_sub_frames(self):
        whole, sub = fbxinfo.frame_of(1562809.25 / 30, Fraction(30))
        self.assertEqual((whole, sub), (1562809, 0.25))
        self.assertEqual(fbxinfo.frame_value(whole, sub), 1562809.25)
        self.assertEqual(fbxinfo.frame_value(10, 0.0), 10)
        self.assertIsInstance(fbxinfo.frame_value(10, 0.0), int)

    def test_frame_rate_modes(self):
        self.assertEqual(fbxinfo.frame_rate(6, 29.0)[0], Fraction(30))
        rate, drop, name = fbxinfo.frame_rate(8, 0)
        self.assertEqual((rate, drop), (Fraction(30000, 1001), True))
        self.assertEqual(fbxinfo.frame_rate(14, 12.5)[0], Fraction(25, 2))      # custom uses the file
        self.assertIn("unknown", fbxinfo.frame_rate(99, 30)[2])

    def test_names(self):
        self.assertEqual(fbxinfo.axis_name(2), "+Y")
        self.assertEqual(fbxinfo.unit_name(0.01), "centimeters")


def fake_scene(**kw):
    node = lambda name, parent=None: NS(name=name, parent=parent, children=[], mesh=None, bone=None)
    root = node("RootNode")
    hips = node("Hips", root)
    spine = node("Spine", hips)
    hips.children.append(spine)
    root.children.append(hips)
    mesh = NS(name="Body", num_vertices=8, num_faces=6, num_triangles=12, materials=[1], vertex_uvs=NS(exists=True),
              vertex_normals=NS(exists=False))
    for n in (hips, spine):
        n.bone = object()
    base = dict(
        metadata=NS(ascii=False, version=7400, creator="Test", big_endian=False),
        settings=NS(axes=NS(up=4, front=2, right=0), unit_meters=1.0, time_mode=11, time_protocol=2,
                    frames_per_second=24.0, original_axis_up=4, original_unit_meters=1.0),
        anim_stacks=[NS(name="Take 001", time_begin=3600.0, time_end=3601.0)],
        nodes=[root, hips, spine], meshes=[mesh], materials=[1], textures=[], bones=[hips.bone, spine.bone],
        skin_deformers=[], blend_shapes=[], cameras=[], lights=[], anim_curves=[])
    base.update(kw)
    return NS(**base)


class BuildMetaTests(unittest.TestCase):
    def build(self, **kw):
        return fbxinfo.build_meta(fake_scene(**kw), "C:/x/rig.fbx")

    def test_basics(self):
        m = self.build()
        self.assertEqual((m["schema"], m["kind"]), (1, "fbx"))
        self.assertEqual(m["file"]["name"], "rig.fbx")
        self.assertEqual(m["file"]["encoding"], "binary")
        self.assertEqual(m["scene"]["up_axis"], "+Z")
        self.assertTrue(m["scene"]["z_up"])
        self.assertFalse(m["scene"]["y_up"])
        self.assertEqual(m["scene"]["unit"], "meters")
        self.assertEqual(m["counts"]["nodes"], 3)
        self.assertEqual(m["counts"]["triangles"], 12)

    def test_take_timecode(self):
        m = self.build()
        take = m["takes"]["Take 001"]
        self.assertEqual(take["start"], {"timecode": "01:00:00:00", "frame": 86400})
        self.assertEqual(take["end"]["frame"], 86424)
        self.assertEqual(take["frames"], 24)

    def test_frame_count_protocol_has_no_text(self):
        m = self.build(settings=NS(axes=NS(up=2, front=4, right=0), unit_meters=0.01, time_mode=11,
                                   time_protocol=1, frames_per_second=24.0,
                                   original_axis_up=2, original_unit_meters=0.01))
        self.assertNotIn("timecode", m["takes"]["Take 001"]["start"])
        self.assertEqual(m["takes"]["Take 001"]["start"]["frame"], 86400)

    def test_ascii_flag(self):
        m = self.build(metadata=NS(ascii=True, version=7300, creator="", big_endian=False))
        self.assertEqual(m["file"]["encoding"], "ascii")
        self.assertNotIn("creator", m)

    def test_no_takes(self):
        m = self.build(anim_stacks=[])
        self.assertEqual(m["counts"]["takes"], 0)
        self.assertFalse(m.get("takes"))


@unittest.skipUnless(HAVE_UFBX, "ufbx is not installed")
class RealFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ascii_file(self):
        src = self.root / "tiny.fbx"
        src.write_text(ASCII_FBX, encoding="ascii")
        out = self.root / "tiny.fbx.meta"
        meta = fbxinfo.extract(str(src), str(out))
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), json.loads(json.dumps(meta)))
        self.assertEqual(meta["file"]["encoding"], "ascii")
        self.assertEqual(meta["scene"]["up_axis"], "+Z")
        self.assertEqual(meta["scene"]["unit"], "meters")

    def test_load_bytes(self):
        with fbxinfo.load_bytes(ASCII_FBX.encode("ascii")) as scene:
            self.assertEqual(fbxinfo.build_meta(scene, "x.fbx")["file"]["encoding"], "ascii")

    def test_non_ascii_name(self):
        src = self.root / "g\u00e4be \u2603.fbx"
        src.write_text(ASCII_FBX, encoding="ascii")
        meta = fbxinfo.extract(str(src), str(self.root / "out.meta"))
        self.assertEqual(meta["file"]["name"], src.name)

    def test_not_an_fbx(self):
        src = self.root / "bad.fbx"
        src.write_bytes(b"this is not an fbx file")
        with self.assertRaises(fbxinfo.FbxError):
            fbxinfo.extract(str(src), str(self.root / "bad.fbx.meta"))

    def test_missing_file(self):
        with self.assertRaises(fbxinfo.FbxError):
            fbxinfo.extract(str(self.root / "nope.fbx"), str(self.root / "nope.meta"))

    @unittest.skipUnless(mocap_dir(), "testdata/mocap is not present")
    def test_mocap_files(self):
        cases = {"Brandon1_sc005_002.fbx": ("14:28:13:19", 1562809.25, "14:30:56:26", 1567706.25, 4897),
                 "Gabe1_sc005_004.fbx": ("14:38:21:28", 1581058.5, "14:41:09:01", 1586071.5, 5013)}
        for name, (t0, f0, t1, f1, frames) in cases.items():
            with self.subTest(name):
                meta = fbxinfo.extract(str(mocap_dir() / name), str(self.root / (name + ".meta")))
                self.assertEqual((meta["scene"]["up_axis"], meta["scene"]["front_axis"], meta["scene"]["right_axis"]),
                                 ("+Y", "+Z", "+X"))
                self.assertEqual(meta["scene"]["unit"], "centimeters")
                self.assertEqual(meta["counts"]["nodes"], 67)
                self.assertEqual(meta["counts"]["bones"], 64)
                self.assertEqual(meta["counts"]["triangles"], 512)
                self.assertEqual(meta["timecode"]["frame_rate"], 30.0)
                take = next(iter(meta["takes"].values()))
                self.assertEqual((take["start"]["timecode"], take["start"]["frame"]), (t0, f0))
                self.assertEqual((take["end"]["timecode"], take["end"]["frame"]), (t1, f1))
                self.assertEqual(take["frames"], frames)


ASCII_FBX = """; FBX 7.3.0 project file
FBXHeaderExtension:  {
	FBXHeaderVersion: 1003
	FBXVersion: 7300
	Creator: "hand made test"
}
GlobalSettings:  {
	Version: 1000
	Properties70:  {
		P: "UpAxis", "int", "Integer", "",2
		P: "UpAxisSign", "int", "Integer", "",1
		P: "FrontAxis", "int", "Integer", "",1
		P: "FrontAxisSign", "int", "Integer", "",-1
		P: "CoordAxis", "int", "Integer", "",0
		P: "CoordAxisSign", "int", "Integer", "",1
		P: "UnitScaleFactor", "double", "Number", "",100
		P: "TimeMode", "enum", "", "",11
	}
}
Takes:  {
	Current: "Take 001"
	Take: "Take 001" {
		FileName: "Take_001.tak"
		LocalTime: 46186158000,92372316000
		ReferenceTime: 46186158000,92372316000
	}
}
"""


class ConversionTests(unittest.TestCase):
    def test_registered_and_named(self):
        from convert.conversions.fbx_meta import FbxMeta
        c = FbxMeta()
        self.assertEqual(c.problems(), [])
        self.assertEqual(c.output_name("C:/a/foo.fbx"), "foo.fbx.meta")
        self.assertTrue(c.accepts("foo.FBX"))
        self.assertFalse(c.accepts("foo.obj"))

    def test_unavailable_message_when_ufbx_missing(self):
        from convert.conversions.fbx_meta import FbxMeta
        real = fbxinfo.importlib.util.find_spec
        fbxinfo.importlib.util.find_spec = lambda name, *a: None if name == "ufbx" else real(name, *a)
        try:
            self.assertEqual(FbxMeta().unavailable(), fbxinfo.BUILTIN_MESSAGE)
            with self.assertRaises(fbxinfo.FbxError):
                fbxinfo.load("x.fbx")
        finally:
            fbxinfo.importlib.util.find_spec = real


@unittest.skipUnless(HAVE_UFBX, "ufbx is not installed")
class OpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._env = dict(os.environ)
        os.environ["APPDATA"] = str(self.root / "appdata")
        (self.root / "appdata" / "PeelConvert").mkdir(parents=True)
        conv_mod.registry.clear()
        from convert.conversions.fbx_meta import FbxMeta
        conv_mod.registry.register(FbxMeta())
        from convert import ops
        from PeelCore import work
        self.ops, self.work = ops, work

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def make(self, name="tiny.fbx", text=ASCII_FBX):
        src = self.root / name
        src.write_text(text, encoding="ascii")
        op = self.ops.ConvertOp(name)
        op.setProperty("conversion", "fbx.meta")
        op.setProperty("input", str(src))
        op.setProperty("output", str(self.root / (name + ".meta")))
        return op, self.root / (name + ".meta")

    def run_op(self, op):
        self.assertEqual(op.state(), self.work.OpState.Ready, op.validationProblems())
        self.assertTrue(op.launch(self.work.Params(), {}))
        self.assertTrue(op.settled.wait(20))

    def test_writes_meta(self):
        op, out = self.make()
        self.run_op(op)
        self.assertEqual(op.state(), self.work.OpState.Finished)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["kind"], "fbx")

    def test_bad_file_fails_with_message(self):
        op, out = self.make(text="junk")
        self.run_op(op)
        self.assertEqual(op.state(), self.work.OpState.Failed)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
