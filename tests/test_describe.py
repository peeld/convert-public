"""Describe-a-conversion: the agent loop (scripted fake model), the model client (local fake
HTTP server) and the key settings.  Run from the python folder:  python -m unittest discover -s tests
"""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from convert import agent_loop, describe_settings, mcp_tools   # noqa: E402
from convert.agent_model import AnthropicModel, ModelError, Reply, ToolCall   # noqa: E402
from test_mcp import Base, GOOD   # noqa: E402


def reply(text="", *calls):
    content = ([{"type": "text", "text": text}] if text else []) + \
              [{"type": "tool_use", "id": "t%d" % i, "name": n, "input": a} for i, (n, a) in enumerate(calls)]
    return Reply(text=text, tool_calls=[ToolCall("t%d" % i, n, a) for i, (n, a) in enumerate(calls)],
                 content=content, usage={"input": 10, "output": 5, "cache_read": 0, "cache_write": 0})


class FakeModel:
    """Plays a script: each entry is a Reply or a callable(messages) -> Reply, or an Exception."""
    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def complete(self, system, messages, tools, cancel=None):
        self.seen.append(json.loads(json.dumps(messages)))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(messages) if callable(step) else step


def run(model, text="make ogg", session=None):
    events = []
    s = session or agent_loop.Session(model, events.append)
    if session:
        s.emit = events.append
    s.send(text)
    return s, events


def kinds(events):
    return [e["type"] for e in events]


class LoopTests(Base):
    def test_clean_success_ends_in_a_proposal_and_saves_nothing(self):
        model = FakeModel([
            reply("", ("get_authoring_guide", {})),
            reply("", ("test_conversion", {"definition": GOOD})),
            reply("Here it is.", ("propose_conversion", {"definition": GOOD})),
        ])
        s, ev = run(model)
        self.assertEqual(kinds(ev)[-2:], ["proposal", "done"])
        proposal = next(e for e in ev if e["type"] == "proposal")
        self.assertEqual(proposal["definition"]["id"], "audio.to-ogg")
        self.assertTrue(proposal["test"]["output_name"])
        self.assertFalse(mcp_tools.ai_path("audio.to-ogg").exists())
        self.assertIn("status", kinds(ev))

    def test_failed_test_then_fix(self):
        bad = dict(GOOD, command=["-i", "{input}", "-c:v", "libx264", "{output}"])
        model = FakeModel([
            reply("", ("test_conversion", {"definition": bad})),
            reply("", ("test_conversion", {"definition": GOOD})),
            reply("", ("propose_conversion", {"definition": GOOD})),
        ])
        s, ev = run(model)
        self.assertIn("proposal", kinds(ev))
        second = model.seen[1][-1]["content"][0]
        self.assertIn('"ok": false', second["content"])

    def test_propose_refused_when_untested(self):
        model = FakeModel([reply("", ("propose_conversion", {"definition": GOOD})),
                           reply("Sorry, testing first.")])
        s, ev = run(model)
        self.assertNotIn("proposal", kinds(ev))
        result = model.seen[1][-1]["content"][0]
        self.assertTrue(result.get("is_error"))

    def test_round_limit(self):
        model = FakeModel([reply("", ("list_tools", {})) for _ in range(agent_loop.MAX_ROUNDS + 2)])
        s, ev = run(model)
        err = next(e for e in ev if e["type"] == "error")
        self.assertEqual(err["kind"], "limit")
        self.assertEqual(kinds(ev)[-1], "done")

    def test_forbidden_tool_is_refused(self):
        model = FakeModel([reply("", ("save_conversion", {"definition": GOOD})), reply("ok")])
        s, ev = run(model)
        self.assertTrue(model.seen[1][-1]["content"][0].get("is_error"))
        self.assertFalse(mcp_tools.ai_path("audio.to-ogg").exists())

    def test_input_path_is_stripped(self):
        seen = {}
        original = mcp_tools.AGENT_BY_NAME["test_conversion"]["fn"]

        def spy(args):
            seen.update(args)
            return original(args)
        mcp_tools.AGENT_BY_NAME["test_conversion"]["fn"] = spy
        try:
            run(FakeModel([reply("", ("test_conversion", {"definition": GOOD, "input_path": "C:/secret.wav"})),
                           reply("done")]))
        finally:
            mcp_tools.AGENT_BY_NAME["test_conversion"]["fn"] = original
        self.assertNotIn("input_path", seen)

    def test_follow_up_after_proposal_sends_pending_results_first(self):
        model = FakeModel([
            reply("", ("test_conversion", {"definition": GOOD})),
            reply("", ("propose_conversion", {"definition": GOOD})),
            reply("Changed."),
        ])
        s, ev = run(model)
        s.send("make it smaller")
        last = model.seen[2][-1]["content"]
        self.assertEqual(last[0]["type"], "tool_result")
        self.assertEqual(last[-1]["type"], "text")

    def test_model_error_becomes_error_event(self):
        s, ev = run(FakeModel([ModelError("auth", "bad key")]))
        self.assertEqual([e["type"] for e in ev], ["error", "done"])
        self.assertEqual(ev[0]["kind"], "auth")

    def test_cancel_leaves_history_clean(self):
        events = []
        holder = {}

        def cancel_now(messages):
            holder["s"].cancel()
            return reply("", ("list_tools", {}))
        model = FakeModel([cancel_now, reply("fine")])
        s = agent_loop.Session(model, events.append)
        holder["s"] = s
        s.send("one")
        self.assertEqual(kinds(events)[-1], "done")
        self.assertNotIn("error", kinds(events))
        s.send("two")
        for a, b in zip(model.seen[1], model.seen[1][1:]):
            self.assertNotEqual(a["role"], b["role"])

    def test_question_event(self):
        s, ev = run(FakeModel([reply("Which size do you want?")]))
        self.assertIn("question", kinds(ev))

    def test_usage_is_counted_and_logged_without_text(self):
        log = self.root / "usage.jsonl"
        s = agent_loop.Session(FakeModel([reply("hi")]), lambda e: None, usage_log=str(log))
        s.send("my secret request")
        line = log.read_text()
        self.assertIn('"input": 10', line)
        self.assertNotIn("secret", line)


class Fake(BaseHTTPRequestHandler):
    plan = []
    log = []

    def do_POST(self):
        Fake.log.append((self.headers.get("x-api-key"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        status, body = Fake.plan.pop(0)
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        Fake.plan, Fake.log = [], []
        self.model = AnthropicModel("k", "m", url="http://127.0.0.1:%d/" % self.server.server_port,
                                    sleep=lambda s: None)

    def tearDown(self):
        self.server.shutdown()

    TOOLS = [{"name": "x", "description": "d", "schema": {"type": "object", "properties": {}}}]

    def test_reply_parsed(self):
        Fake.plan = [(200, {"content": [{"type": "text", "text": "hi"},
                                        {"type": "tool_use", "id": "a", "name": "x", "input": {"q": 1}}],
                            "stop_reason": "tool_use", "usage": {"input_tokens": 3, "output_tokens": 4}})]
        r = self.model.complete("sys", [{"role": "user", "content": "yo"}], self.TOOLS)
        self.assertEqual(r.text, "hi")
        self.assertEqual(r.tool_calls[0].arguments, {"q": 1})
        self.assertEqual(r.usage["output"], 4)
        key, body = Fake.log[0]
        self.assertEqual(key, "k")
        self.assertEqual(body["tools"][0]["input_schema"]["type"], "object")
        self.assertIn("cache_control", body["system"][0])

    def test_auth_error(self):
        Fake.plan = [(401, {"error": {"message": "no"}})]
        with self.assertRaises(ModelError) as cm:
            self.model.complete("s", [], self.TOOLS)
        self.assertEqual(cm.exception.kind, "auth")

    def test_overloaded_is_retried(self):
        Fake.plan = [(529, {}), (200, {"content": [{"type": "text", "text": "ok"}]})]
        self.assertEqual(self.model.complete("s", [], self.TOOLS).text, "ok")
        self.assertEqual(len(Fake.log), 2)

    def test_gives_up_after_retries(self):
        Fake.plan = [(529, {})] * 3
        with self.assertRaises(ModelError) as cm:
            self.model.complete("s", [], self.TOOLS)
        self.assertEqual(cm.exception.kind, "server")

    def test_bad_request_mentions_model(self):
        Fake.plan = [(400, {"error": {"message": "model: not found"}})]
        with self.assertRaises(ModelError) as cm:
            self.model.complete("s", [], self.TOOLS)
        self.assertIn("model", str(cm.exception))

    def test_cancelled_before_sending(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(ModelError) as cm:
            self.model.complete("s", [], self.TOOLS, ev)
        self.assertEqual(cm.exception.kind, "cancelled")
        self.assertEqual(Fake.log, [])


class SettingsTests(Base):
    def test_key_roundtrip_and_clear(self):
        self.assertFalse(describe_settings.get_key())
        describe_settings.set_key("sk-test-1234567890")
        self.assertEqual(describe_settings.get_key(), "sk-test-1234567890")
        self.assertNotIn(b"sk-test", describe_settings.key_path().read_bytes())
        self.assertNotIn("1234567", describe_settings.mask("sk-test-1234567890"))
        describe_settings.clear_key()
        self.assertFalse(describe_settings.get_key())

    def test_model_default_and_save(self):
        self.assertEqual(describe_settings.load()["model"], describe_settings.DEFAULT_MODEL)
        describe_settings.save("other-model")
        self.assertEqual(describe_settings.load()["model"], "other-model")


if __name__ == "__main__":
    unittest.main()
