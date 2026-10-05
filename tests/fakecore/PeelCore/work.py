import enum
import threading


class LogStream(enum.IntEnum):
    Stdout = 0
    Stderr = 1


class OpState(enum.IntEnum):
    Invalid = 0
    Ready = 1
    Running = 2
    Finished = 3
    Failed = 4
    Cancelled = 5


class Params:
    def __init__(self):
        self._d = {}

    def set(self, name, value):
        self._d[name] = value

    def value(self, name, default=None):
        return self._d.get(name, default)


class Op:
    def __init__(self, *args):
        self._name = args[-1]
        self._state = OpState.Invalid
        self._props = Params()
        self._outputs = Params()
        self._cancel_requested = False
        self._logs = []
        self._fraction = 0.0
        self._status_text = ""
        self.status_history = []
        self.error = ""
        self._lock = threading.RLock()
        self.settled = threading.Event()

    def name(self):
        return self._name

    def state(self):
        return self._state

    def properties(self):
        return self._props

    def outputs(self):
        return self._outputs

    def setProperty(self, name, value):
        self._props.set(name, value)
        self.revalidate()

    def validationProblems(self):
        return self.problems

    def revalidate(self):
        if self._state in (OpState.Invalid, OpState.Ready):
            self.problems = self.validate()
            self._state = OpState.Invalid if self.problems else OpState.Ready
        return self._state

    def launch(self, inputs, upstream):
        if self._state != OpState.Ready:
            return False
        self._state = OpState.Running
        self.settled.clear()
        self.start(inputs, upstream)
        return True

    def requestCancel(self):
        self._cancel_requested = True
        self.onCancel()

    def complete(self, outputs=None):
        with self._lock:
            if self._state != OpState.Running:
                return False
            if self._cancel_requested:
                return self.acknowledgeCancel()
            self._outputs = outputs or Params()
            self._fraction = 1.0
            self._state = OpState.Finished
        self.settled.set()
        return True

    def fail(self, message):
        with self._lock:
            if self._state != OpState.Running:
                return False
            if self._cancel_requested:
                return self.acknowledgeCancel()
            self.error = message
            self._state = OpState.Failed
        self.settled.set()
        return True

    def acknowledgeCancel(self):
        with self._lock:
            self._state = OpState.Cancelled
        self.settled.set()
        return True

    def setProgress(self, fraction):
        self._fraction = min(1.0, max(0.0, fraction))

    def setStatus(self, status):
        self._status_text = status
        self.status_history.append(status)

    def progress(self):
        return self._fraction

    def status(self):
        return self._status_text

    def log(self, stream, text):
        self._logs.append((stream, text))

    def logs(self):
        return list(self._logs)
