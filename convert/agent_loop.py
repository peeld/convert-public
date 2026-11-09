"""The "Describe a conversion" agent loop.  No Qt: the UI runs `Session.send` on a worker thread
and receives events through a callback.

    session = Session(model, emit)         # emit(event_dict) is called from the worker thread
    session.send("turn videos into small gifs")     # blocks until the agent is done or stops
    session.cancel()                                  # from any thread

Events (all dicts with "type"): status {text}, message {text}, question {text}, proposal {definition,
test, warnings, replaces}, error {kind, text}, done {rounds, usage}.

The agent can only call the read/test tools and propose_conversion (mcp_tools.AGENT_TOOLS); nothing
is saved from here.  Only the person's Add button saves.
"""
import json
import threading
import time

from . import agent_guide, mcp_tools
from .agent_model import ModelError

MAX_ROUNDS = 12
MAX_RESULT_CHARS = 6000

STATUS = {
    "get_authoring_guide": "Reading how conversions are written...",
    "list_conversions": "Looking at the conversions you already have...",
    "get_conversion": "Looking at an existing conversion...",
    "list_tools": "Checking which tools are installed...",
    "tool_capabilities": "Checking what the tool can do...",
    "validate_conversion": "Checking the draft...",
    "test_conversion": "Trying it on a sample file...",
    "propose_conversion": "Getting it ready for you...",
}


class Session:
    def __init__(self, model, emit, usage_log=None):
        self.model = model
        self.emit = emit
        self.usage_log = usage_log
        self.messages = []
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self._cancel = threading.Event()
        self._pending = []          # tool_results still owed after a proposal ended the last turn

    def cancel(self):
        self._cancel.set()

    def reset(self):
        self.messages = []
        self._pending = []

    def send(self, text):
        """Add the person's message and run the agent until it stops. Never raises."""
        self._cancel.clear()
        before = dict(self.usage)
        content = self._pending + [{"type": "text", "text": text}]
        self._pending = []
        self.messages.append({"role": "user", "content": content})
        rounds = 0
        try:
            while True:
                if self._cancel.is_set():
                    raise ModelError("cancelled", "Cancelled.")
                if rounds >= MAX_ROUNDS:
                    self.emit({"type": "error", "kind": "limit",
                               "text": "That took too many steps and I stopped. Try describing it more simply, "
                                       "or in smaller pieces."})
                    break
                rounds += 1
                reply = self.model.complete(agent_guide.DESCRIBE_RULES, self.messages,
                                            mcp_tools.AGENT_TOOLS, self._cancel)
                self._count(reply.usage)
                self.messages.append({"role": "assistant", "content": reply.content})
                if reply.text:
                    self.emit({"type": "message", "text": reply.text})
                if not reply.tool_calls:
                    if reply.text.rstrip().endswith("?"):
                        self.emit({"type": "question", "text": reply.text})
                    break
                results, proposal = self._run_tools(reply.tool_calls)
                if proposal is not None:
                    # The turn ends here so the person can choose. The tool_results are sent with
                    # their next message (the API needs them right after the tool_use blocks).
                    self._pending = results
                    self.emit(dict(proposal, type="proposal"))
                    break
                self.messages.append({"role": "user", "content": results})
        except ModelError as exc:
            if exc.kind == "cancelled":
                self._repair()
            else:
                self.emit({"type": "error", "kind": exc.kind, "text": str(exc)})
        except Exception as exc:      # noqa: BLE001 - the UI must always get an ending
            self.emit({"type": "error", "kind": "internal", "text": "Something went wrong: %s" % exc})
        self._log(rounds, before)
        self.emit({"type": "done", "rounds": rounds, "usage": dict(self.usage)})

    # ---- internals ----
    def _run_tools(self, calls):
        results, proposal = [], None
        for call in calls:
            if self._cancel.is_set():
                results.append(self._result(call.id, "cancelled", True))
                continue
            tool = mcp_tools.AGENT_BY_NAME.get(call.name)
            if tool is None:
                results.append(self._result(call.id, "unknown tool %r; use only the tools you were given" % call.name, True))
                continue
            self.emit({"type": "status", "text": STATUS.get(call.name, "Working...")})
            args = dict(call.arguments)
            args.pop("input_path", None)      # the agent must not point at the person's files
            try:
                out = tool["fn"](args)
                is_error = False
            except mcp_tools.ToolError as exc:
                out, is_error = {"error": str(exc)}, True
            except Exception as exc:      # noqa: BLE001
                out, is_error = {"error": "%s: %s" % (type(exc).__name__, exc)}, True
            if call.name == "propose_conversion" and not is_error and proposal is None:
                proposal = {k: out[k] for k in ("definition", "test", "warnings", "replaces")}
            results.append(self._result(call.id, out if is_error or not isinstance(out, str) else out, is_error))
        return results, proposal

    @staticmethod
    def _result(call_id, out, is_error):
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n...(cut; ask for less)"
        block = {"type": "tool_result", "tool_use_id": call_id, "content": text}
        if is_error:
            block["is_error"] = True
        return block

    def _repair(self):
        """After a cancel the history may end with tool_use blocks that have no result; drop that
        turn so the next message starts clean."""
        while self.messages and self.messages[-1]["role"] == "assistant":
            self.messages.pop()
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()

    def _count(self, usage):
        for key in self.usage:
            self.usage[key] += int(usage.get(key, 0) or 0)

    def _log(self, rounds, before):
        """One line per request: counts only, never what the person typed."""
        if not self.usage_log:
            return
        try:
            with open(self.usage_log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"t": int(time.time()), "rounds": rounds, **{k: self.usage[k] - before[k] for k in self.usage}}) + "\n")
        except OSError:
            pass
