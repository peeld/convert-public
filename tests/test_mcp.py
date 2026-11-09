"""MCP server, its tools, the sample-file trial and the AI connection settings.

Plain Python, no app: uses a fake "ffmpeg" script.  Run from the python folder:
    python -m unittest discover -s tests
"""
import http.client
import json
import os
import socket
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE / "fakecore"))

from faketool import make_tool                                # noqa: E402
from convert import agent_service, host, mcp_server, mcp_tools   # noqa: E402
from convert.conversion import ConversionRegistry, user_dir   # noqa: E402

FAKE_FFMPEG = r'''import sys
a = sys.argv[1:]
out = a[-1]
if "-encoders" in a:
    print(" V....D mpeg4                MPEG-4 part 2\n A....D libopus             libopus Opus")
    sys.exit(0)
if "lavfi" in a:
    open(out, "wb").write(b"SAMPLE"); sys.exit(0)
if "-i" in a:
    if "libx264" in a:
        sys.stderr.write("Unknown encoder 'libx264'\n"); sys.exit(1)
    open(out, "wb").write(open(a[a.index("-i") + 1], "rb").read() + b"!")
    sys.exit(0)
sys.exit(2)
'''

GOOD = {"id": "audio.to-ogg", "label": "Audio to OGG", "group": "Audio", "tool": "ffmpeg",
        "inputs": ["wav", "flac"], "output_ext": "ogg", "command": ["-i", "{input}", "-c:a", "libopus", "{output}"]}


def call(handler, name, **arguments):
    reply = handler.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments}})
    result = reply["result"]
    text = result["content"][0]["text"]
    try:
        return result.get("isError", False), json.loads(text)
    except ValueError:
        return result.get("isError", False), text


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._env = dict(os.environ)
        os.environ["APPDATA"] = str(self.root / "appdata")
        fake = make_tool(self.root, "ffmpeg", FAKE_FFMPEG)
        data = host.user_data_dir()          # the app's folder: TestApp under the fake _peelhost
        data.mkdir(parents=True, exist_ok=True)
        (data / "settings.json").write_text(json.dumps({"tools": {"ffmpeg": str(fake)}}))
        self.handler = mcp_server.Handler()
        mcp_tools._tested.clear()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()


class ProtocolTests(Base):
    def test_initialize_lists_tools_and_gives_instructions(self):
        reply = self.handler.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2025-03-26"}})["result"]
        self.assertEqual(reply["protocolVersion"], "2025-03-26")
        self.assertIn("get_authoring_guide", reply["instructions"])
        tools = self.handler.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertTrue({"save_conversion", "test_conversion", "validate_conversion",
                         "get_authoring_guide", "tool_capabilities"} <= names)
        self.assertTrue(all(t["inputSchema"]["type"] == "object" for t in tools))

    def test_unknown_version_falls_back_and_notifications_get_no_reply(self):
        reply = self.handler.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "1999-01-01"}})["result"]
        self.assertEqual(reply["protocolVersion"], mcp_server.SUPPORTED_VERSIONS[0])
        self.assertIsNone(self.handler.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(self.handler.handle({"jsonrpc": "2.0", "method": "no/such"}))

    def test_errors(self):
        self.assertEqual(self.handler.handle({"jsonrpc": "2.0", "id": 1, "method": "no/such"})["error"]["code"], -32601)
        bad = self.handler.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}})
        self.assertEqual(bad["error"]["code"], -32602)
        self.assertEqual(self.handler.handle({"hello": 1})["error"]["code"], -32600)

    def test_resource_and_prompt(self):
        res = self.handler.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                                   "params": {"uri": mcp_server.GUIDE_URI}})["result"]
        self.assertIn("conversion", res["contents"][0]["text"])
        prompt = self.handler.handle({"jsonrpc": "2.0", "id": 1, "method": "prompts/get",
                                      "params": {"name": "new_conversion", "arguments": {"what": "make gifs"}}})
        self.assertIn("make gifs", prompt["result"]["messages"][0]["content"]["text"])

    def test_stdio(self):
        import io
        out = io.StringIO()
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), "", "not json"]
        mcp_server.serve_stdio(io.StringIO("\n".join(lines) + "\n"), out)
        replies = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual(replies[0]["result"], {})
        self.assertEqual(replies[1]["error"]["code"], -32700)


class ValidateTests(Base):
    def problems(self, **changes):
        entry = dict(GOOD, **changes)
        entry = {k: v for k, v in entry.items() if v is not None}
        _, result = call(self.handler, "validate_conversion", definition=entry)
        return result

    def test_good(self):
        result = self.problems()
        self.assertTrue(result["ok"], result)

    def test_normalises_extensions_and_id(self):
        self.assertTrue(self.problems(output_ext=".OGG", inputs=[".WAV"], id="Audio.To-Ogg")["ok"])

    def test_rejections(self):
        cases = {
            "class": self.problems(**{"class": "x.Y"}),
            "unknown tool": self.problems(tool="cmd"),
            "owned flag": self.problems(command=["-y", "-i", "{input}", "{output}"]),
            "path": self.problems(command=["-i", "{input}", "C:\\Windows\\x.ogg", "{output}"]),
            "dotdot": self.problems(command=["-i", "{input}", "../x.ogg", "{output}"]),
            "no output": self.problems(command=["-i", "{input}"]),
            "bad id": self.problems(id="ogg"),
            "built-in id": self.problems(id="video.to-mp4"),
            "no group": self.problems(group=""),
            "bundled tool": self.problems(tool="ffprobe"),
            "typo field": self.problems(outputext="x"),
        }
        for name, result in cases.items():
            self.assertFalse(result["ok"], name)
            self.assertTrue(result["problems"], name)

    def test_definition_may_be_a_json_string(self):
        error, result = call(self.handler, "validate_conversion", definition=json.dumps(GOOD))
        self.assertFalse(error)
        self.assertTrue(result["ok"])
        error, _ = call(self.handler, "validate_conversion", definition="{nope")
        self.assertTrue(error)


class SaveTests(Base):
    def test_save_list_load_delete(self):
        error, saved = call(self.handler, "save_conversion", definition=GOOD)
        self.assertFalse(error, saved)
        path = user_dir() / "ai.audio.to-ogg.json"
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text())["output_ext"], "ogg")
        self.assertTrue(any("not tested" in w for w in saved["warnings"]))

        reg = ConversionRegistry()
        reg.load_all([user_dir()])
        self.assertEqual(reg.problems, [])            # the app's own loader accepts it
        self.assertEqual(reg.get("audio.to-ogg").label, "Audio to OGG")

        _, listing = call(self.handler, "list_conversions", query="audio.to-ogg")
        self.assertEqual([c["origin"] for c in listing["conversions"]], ["made by an AI agent"])
        self.assertEqual([c["id"] for c in mcp_tools.ai_conversions()], ["audio.to-ogg"])

        error, again = call(self.handler, "save_conversion", definition=GOOD)
        self.assertTrue(error)
        self.assertIn("overwrite", again)
        error, _ = call(self.handler, "save_conversion", definition=dict(GOOD, label="New label"), overwrite=True)
        self.assertFalse(error)

        error, deleted = call(self.handler, "delete_conversion", id="audio.to-ogg")
        self.assertFalse(error, deleted)
        self.assertFalse(path.exists())

    def test_cannot_touch_built_in_or_hand_written(self):
        folder = user_dir()
        folder.mkdir(parents=True)
        (folder / "mine.json").write_text(json.dumps(dict(GOOD, id="my.own", label="Mine")))
        error, _ = call(self.handler, "save_conversion", definition=dict(GOOD, id="my.own"))
        self.assertTrue(error)
        error, _ = call(self.handler, "delete_conversion", id="my.own")
        self.assertTrue(error)
        error, _ = call(self.handler, "delete_conversion", id="video.to-mp4")
        self.assertTrue(error)
        self.assertTrue((folder / "mine.json").exists())
        error, _ = call(self.handler, "delete_conversion", id="../../x")
        self.assertTrue(error)

    def test_hand_written_python_ids_are_protected(self):
        folder = user_dir()
        folder.mkdir(parents=True)
        (folder / "own.py").write_text('class X:\n    id = "py.own"\n')
        error, _ = call(self.handler, "save_conversion", definition=dict(GOOD, id="py.own"))
        self.assertTrue(error)

    def test_invalid_is_not_written(self):
        error, text = call(self.handler, "save_conversion", definition=dict(GOOD, tool="powershell"))
        self.assertTrue(error)
        self.assertFalse(list(user_dir().glob("*")) if user_dir().exists() else [])


class TrialTests(Base):
    def test_working_conversion_then_save_has_no_warning(self):
        error, result = call(self.handler, "test_conversion", definition=GOOD)
        self.assertFalse(error, result)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["output"]["name"], "sample.ogg")
        self.assertGreater(result["output"]["bytes"], 0)
        self.assertEqual(os.path.splitext(result["command"][0])[0], "ffmpeg")   # the command is reported (.exe/.cmd aside)
        _, saved = call(self.handler, "save_conversion", definition=GOOD)
        self.assertEqual(saved["warnings"], [])

    def test_failing_conversion_reports_messages(self):
        bad = dict(GOOD, command=["-i", "{input}", "-c:v", "libx264", "{output}"])
        error, result = call(self.handler, "test_conversion", definition=bad)
        self.assertFalse(error)
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("libx264", " ".join(result["messages"]))

    def test_invalid_definition_stops_before_running(self):
        _, result = call(self.handler, "test_conversion", definition=dict(GOOD, tool="cmd"))
        self.assertEqual(result["stage"], "validation")

    def test_given_input_is_only_read(self):
        source = self.root / "mine.wav"
        source.write_bytes(b"DATA")
        _, result = call(self.handler, "test_conversion", definition=GOOD, input_path=str(source))
        self.assertTrue(result["ok"], result)
        self.assertEqual(source.read_bytes(), b"DATA")
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.suffix == ".ogg"), [])

    def test_existing_conversion_by_id(self):
        _, result = call(self.handler, "test_conversion", id="audio.to-flac")
        self.assertTrue(result["ok"], result)

    def test_missing_tool_is_a_clear_error(self):
        (host.user_data_dir() / "settings.json").write_text("{}")
        old = os.environ["PATH"]
        os.environ["PATH"] = ""
        try:
            error, text = call(self.handler, "test_conversion", definition=GOOD)
        finally:
            os.environ["PATH"] = old
        self.assertTrue(error)
        self.assertIn("not installed", text)

    def test_capabilities_filter(self):
        _, result = call(self.handler, "tool_capabilities", what="encoders", contains="opus")
        self.assertEqual(len(result["lines"]), 1)
        error, _ = call(self.handler, "tool_capabilities", what="bogus")
        self.assertTrue(error)


class HttpTests(Base):
    def setUp(self):
        super().setUp()
        self.server = mcp_server.McpHttpServer("t" * 24, 0)
        self.server.start()

    def tearDown(self):
        self.server.stop()
        super().tearDown()

    def post(self, path, body, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        if host:
            h["Host"] = host
        conn.request("POST", path, json.dumps(body), h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    def test_valid_token_in_path(self):
        status, data = self.post("/mcp/" + "t" * 24, self.PING)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["result"], {})

    def test_bearer_header(self):
        status, _ = self.post("/mcp", self.PING, {"Authorization": "Bearer " + "t" * 24})
        self.assertEqual(status, 200)
        self.assertEqual(self.post("/mcp", self.PING)[0], 401)

    def test_wrong_token_host_origin(self):
        self.assertEqual(self.post("/mcp/wrong", self.PING)[0], 401)
        self.assertEqual(self.post("/other", self.PING)[0], 404)
        self.assertEqual(self.post("/mcp/" + "t" * 24, self.PING, host="evil.example.com")[0], 403)
        self.assertEqual(self.post("/mcp/" + "t" * 24, self.PING, {"Origin": "https://evil.example.com"})[0], 403)

    def test_notification_is_202_and_get_is_405(self):
        status, _ = self.post("/mcp/" + "t" * 24, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(status, 202)
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port)
        conn.request("GET", "/mcp/" + "t" * 24)
        self.assertEqual(conn.getresponse().status, 405)

    def test_garbage_body(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port)
        conn.request("POST", "/mcp/" + "t" * 24, "{nope", {"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 400)


class ServiceTests(Base):
    def make(self, **kw):
        return agent_service.AgentService(self.root / "agent.json", **kw)

    def test_defaults_off_and_url(self):
        svc = self.make()
        self.assertFalse(svc.enabled)
        self.assertFalse(svc.running)
        self.assertTrue(svc.url().startswith("http://127.0.0.1:"))
        self.assertGreaterEqual(len(svc.token), 16)

    def test_enable_persists_and_serves(self):
        svc = self.make(port_default=0)
        self.assertTrue(svc.set_enabled(True))
        try:
            self.assertTrue(svc.running)
            path = svc.url().replace("http://127.0.0.1:%d" % svc.port, "")
            conn = http.client.HTTPConnection("127.0.0.1", svc.port)
            conn.request("POST", path, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                         {"Content-Type": "application/json"})
            self.assertEqual(conn.getresponse().status, 200)
            again = self.make()
            self.assertTrue(again.enabled)
            self.assertEqual(again.token, svc.token)
            self.assertEqual(again.port, svc.port)
        finally:
            svc.stop()
        self.assertFalse(svc.running)

    def test_busy_port_falls_back(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        svc = self.make(port_default=blocker.getsockname()[1])
        try:
            self.assertTrue(svc.start())
            self.assertNotEqual(svc.port, blocker.getsockname()[1])
        finally:
            svc.stop()
            blocker.close()

    def test_reset_link_changes_token_and_keeps_running(self):
        svc = self.make(port_default=0)
        svc.start()
        try:
            old = svc.token
            svc.reset_link()
            self.assertNotEqual(svc.token, old)
            self.assertTrue(svc.running)
        finally:
            svc.stop()

    def test_turning_off_is_remembered(self):
        svc = self.make(port_default=0)
        svc.set_enabled(True)
        svc.set_enabled(False)
        self.assertFalse(self.make().enabled)

    def test_setups_contain_the_link_for_every_app(self):
        url = "http://127.0.0.1:1/mcp/abc"
        setups = agent_service.client_setups(url)
        self.assertGreaterEqual(len(setups), 5)
        for name, where, text in setups:
            self.assertIn(url, text, name)


if __name__ == "__main__":
    unittest.main()
