"""How many conversions run at once.

Every ConvertOp is independent, so the executor would start them all together.
ffmpeg already uses every core, so by default one runs at a time: an Op waits
here for a slot (staying RUNNING, with a log line) and can be cancelled while it
waits.  Chaining the Ops with edges would also serialise them, but then one
failed file would mark every file after it Dead.
"""
import threading

_cond = threading.Condition()
_slots = 1
_busy = 0


def set_slots(count):
    global _slots
    with _cond:
        _slots = max(1, int(count))
        _cond.notify_all()


def slots():
    return _slots


def acquire(cancel, poll=0.1):
    """Wait for a free slot.  False if `cancel` (a threading.Event) was set first."""
    global _busy
    with _cond:
        while _busy >= _slots:
            if cancel.is_set():
                return False
            _cond.wait(poll)
        if cancel.is_set():
            return False
        _busy += 1
        return True


def release():
    global _busy
    with _cond:
        _busy = max(0, _busy - 1)
        _cond.notify_all()
