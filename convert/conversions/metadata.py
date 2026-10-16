"""Extract metadata: foo.mov -> foo.mov.meta (JSON; resolution, codecs, frame count, timecode...).

The output keeps the full input name and adds ".meta", so it never collides with a
converted copy (foo.mp4) and sits next to the source it describes.  The work is done by
convert/mediainfo.py (ffprobe); its docstring describes the file's layout.
"""
import os

from convert import Conversion, register
from convert import mediainfo
from convert.conversion import ConversionError


@register
class MediaMeta(Conversion):
    id = "media.meta"
    label = "Extract metadata (.meta JSON)"
    group = "Info"
    tool = "ffprobe"
    inputs = ()                  # any file: ffprobe decides whether it is media
    output_ext = "meta"
    runs_itself = True

    def output_name(self, input_path):
        return os.path.basename(input_path) + ".meta"

    def run(self, job, cancel, on_process=None):
        try:
            mediainfo.extract(job.tool_path, job.input, job.output, cancel, on_process)
        except mediainfo.ProbeError as exc:
            raise ConversionError(str(exc))
