"""Extract FBX metadata: foo.fbx -> foo.fbx.meta (JSON; axes, units, counts, timecode, takes).

Same naming as the media `.meta` files: the full input name plus ".meta", beside the
source.  The work is done by convert/fbxinfo.py (ufbx); its docstring describes the file.
Needs no external program ("builtin"), but needs the ufbx library, which ships with the
app: an older build without it marks the row invalid with a message instead of failing.
"""
import os

from convert import Conversion, register
from convert import fbxinfo
from convert.conversion import ConversionError


@register
class FbxMeta(Conversion):
    id = "fbx.meta"
    label = "Extract FBX metadata (.meta JSON)"
    group = "Info"
    tool = "builtin"
    inputs = ("fbx",)
    output_ext = "meta"
    runs_itself = True

    def output_name(self, input_path):
        return os.path.basename(input_path) + ".meta"

    def unavailable(self):
        return fbxinfo.unavailable()

    def run(self, job, cancel, on_process=None):
        try:
            fbxinfo.extract(job.input, job.output)
        except fbxinfo.FbxError as exc:
            raise ConversionError(str(exc))
