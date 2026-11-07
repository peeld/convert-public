"""SMPTE timecode as text and frame counts (no PeelCore, no Qt).

The Python mirror of core's TCValue rules (core/docs/ref/CORE_CATALOGUE.md §2):

    parse_rate("30000/1001")      -> Fraction(30000, 1001); also "29.97", "25"; None if not a rate
    rate_text(Fraction)           -> "30000/1001"
    tc_frames("01:00:00;00", r)   -> frames since midnight at r (';' or ',' before FF = drop frame)
    frames_tc(n, r, drop=False)   -> "HH:MM:SS:FF" (';' when drop)
    is_drop("01:00:00;00")        -> True

A timecode counts whole frames at its nominal rate (30 for 29.97); drop frame skips
two labels (four at 59.94) at the start of every minute except every tenth, so the
label tracks the clock. Seconds since midnight are frames / the exact rate.
"""
import re
from fractions import Fraction

_TC = re.compile(r"^(\d{2}):(\d{2}):(\d{2})([:;.,])(\d{2,3})$")


def parse_rate(text):
    """'30000/1001', '29.97' or a number -> Fraction; None when empty or not a rate."""
    try:
        rate = Fraction(str(text)) if text not in (None, "") else None
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate and rate > 0 else None


def rate_text(rate):
    return "%d/%d" % (rate.numerator, rate.denominator)


def nominal(rate):
    """The frames per second a timecode's frame field counts to: 30 for 29.97."""
    return round(rate)


def is_drop(tc):
    m = _TC.match(tc or "")
    return bool(m and m[4] in ";,")


def tc_frames(tc, rate):
    """'HH:MM:SS:FF' (';' or ',' before FF for drop frame) -> frames since midnight at rate."""
    m = _TC.match(tc or "")
    if not m:
        raise ValueError("not a timecode: %r" % (tc,))
    hh, mm, ss, sep, ff = int(m[1]), int(m[2]), int(m[3]), m[4], int(m[5])
    fps = nominal(rate)
    frames = ((hh * 60 + mm) * 60 + ss) * fps + ff
    if sep in ";,":
        drop = fps // 15
        minutes = hh * 60 + mm
        frames -= drop * (minutes - minutes // 10)
    return frames


def frames_tc(frames, rate, drop=False):
    """frames since midnight at rate -> 'HH:MM:SS:FF' (';' when drop)."""
    fps = nominal(rate)
    if drop:
        d = fps // 15
        per10 = fps * 600 - d * 9
        per1 = fps * 60 - d
        tens, rem = divmod(frames, per10)
        frames += d * 9 * tens + (d * ((rem - d) // per1) if rem > d else 0)
    ff = frames % fps
    s = frames // fps
    return "%02d:%02d:%02d%s%02d" % (s // 3600 % 24, s // 60 % 60, s % 60, ";" if drop else ":", ff)


def seconds(tc, rate):
    """Real time since midnight of a timecode counting at rate (exact)."""
    return Fraction(tc_frames(tc, rate)) / rate
