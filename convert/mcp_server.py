"""A small MCP (Model Context Protocol) server, standard library only.

It lets an AI agent (Claude Code, Cursor, ...) write new conversions for PeelConvert.  The tools
are in mcp_tools.py; this file is only the protocol and two ways to talk it:

    McpHttpServer   Streamable HTTP on 127.0.0.1, run inside the app (agent_service.py starts it).
                    The URL carries a secret token:  http://127.0.0.1:<port>/mcp/<token>
    serve_stdio()   newline-delimited JSON on stdin/stdout, for `python -m convert.mcp_server`
                    when the app is not running (any Python 3.8+, no packages needed).

Pure Python: no PeelCore, no Qt.
"""
import hmac
import json
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import agent_guide, mcp_tools

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
GUIDE_URI = "peelconvert://guide"
MAX_BODY = 1_000_000
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

_PARSE_ERROR, _INVALID_REQUEST, _METHOD_NOT_FOUND, _INVALID_PARAMS = -32700, -32600, -32601, -32602


def _version():
    try:
        manifest = Path(__file__).resolve().parent.parent / "manifest.json"
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", "0"))
    except (OSError, ValueError):
        return "0"


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)


PROMPT = ("Use the PeelConvert tools to make a new conversion for me: {what}\n\n"
          "First call get_authoring_guide. Check what ffmpeg supports, test your draft until it works, "
          "then save it. When you are done, tell me in plain words what it is called and where to find "
          "it in the app.")


class Handler:
    """Transport-independent: feed it one decoded JSON-RPC message, get the reply (or None)."""

    def handle(self, message):
        if isinstance(message, list):
            replies = [r for r in (self.handle(m) for m in message) if r is not None]
            return replies or None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, _INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        if "method" not in message:
            return None                         # a reply to something we never sent
        request_id = message.get("id")
        notification = "id" not in message
        method = message["method"]
        params = message.get("params") or {}
        fn = getattr(self, "m_" + str(method).replace("/", "_"), None)
        if fn is None:
            return None if notification else _error(request_id, _METHOD_NOT_FOUND, "unknown method " + str(method))
        try:
            result = fn(params)
        except _Invalid as exc:
            return None if notification else _error(request_id, _INVALID_PARAMS, str(exc))
        except Exception as exc:                # noqa: BLE001 - a bug must not kill the server
            traceback.print_exc(file=sys.stderr)
            return None if notification else _error(request_id, -32603, "internal error: %s" % exc)
        return None if notification else _result(request_id, result)

    # ---- lifecycle ----
    def m_initialize(self, params):
        wanted = params.get("protocolVersion")
        return {"protocolVersion": wanted if wanted in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "peelconvert", "title": "PeelConvert", "version": _version()},
                "instructions": agent_guide.INSTRUCTIONS}

    def m_ping(self, params):
        return {}

    def m_notifications_initialized(self, params):
        return None

    # ---- tools ----
    def m_tools_list(self, params):
        return {"tools": [{"name": t["name"], "description": t["description"], "inputSchema": t["schema"],
                          "annotations": {"readOnlyHint": t["readOnly"],
                                          "destructiveHint": t["name"] == "delete_conversion",
                                          "openWorldHint": False}}
                         for t in mcp_tools.TOOLS]}

    def m_tools_call(self, params):
        tool = mcp_tools.BY_NAME.get(params.get("name"))
        if tool is None:
            raise _Invalid("unknown tool %r" % params.get("name"))
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _Invalid("arguments must be an object")
        try:
            return {"content": [{"type": "text", "text": _text(tool["fn"](arguments))}]}
        except mcp_tools.ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception as exc:                # noqa: BLE001 - tell the agent, keep running
            traceback.print_exc(file=sys.stderr)
            return {"content": [{"type": "text", "text": "internal error: %s: %s" % (type(exc).__name__, exc)}],
                    "isError": True}

    # ---- resources and prompts (a convenience: the tools alone are enough) ----
    def m_resources_list(self, params):
        return {"resources": [{"uri": GUIDE_URI, "name": "How to write a conversion",
                               "mimeType": "text/markdown"}]}

    def m_resources_read(self, params):
        if params.get("uri") != GUIDE_URI:
            raise _Invalid("unknown resource")
        return {"contents": [{"uri": GUIDE_URI, "mimeType": "text/markdown", "text": agent_guide.GUIDE}]}

    def m_prompts_list(self, params):
        return {"prompts": [{"name": "new_conversion", "title": "Make a new conversion",
                             "description": "Ask for a conversion in plain words.",
                             "arguments": [{"name": "what", "description": "What should it convert, and how?",
                                            "required": True}]}]}

    def m_prompts_get(self, params):
        if params.get("name") != "new_conversion":
            raise _Invalid("unknown prompt")
        what = str((params.get("arguments") or {}).get("what") or "(describe it)")
        return {"messages": [{"role": "user", "content": {"type": "text", "text": PROMPT.format(what=what)}}]}


class _Invalid(Exception):
    pass


def starter_prompt(what):
    """The same words as the new_conversion prompt, for the app's 'copy a starter prompt' button."""
    return PROMPT.format(what=what.strip() or "(describe what you want)")


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):      # a client hanging up is not news
            super().handle_error(request, client_address)


# ---- HTTP ----------------------------------------------------------------------------------
class McpHttpServer:
    """Streamable HTTP, POST only, JSON replies.  Bound to 127.0.0.1; the URL path holds a secret."""

    def __init__(self, token, port=0, handler=None):
        self.token = token
        self.handler = handler or Handler()
        outer = self

        class Request(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "PeelConvertMCP"

            def log_message(self, *args):
                pass

            def _reply(self, status, body=b"", content_type="application/json", extra=()):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in extra:
                    self.send_header(key, value)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _allowed(self):
                host = urlsplit("//" + (self.headers.get("Host") or "")).hostname or ""
                if host.lower() not in _LOCAL_HOSTS:
                    return 403
                origin = self.headers.get("Origin")
                if origin and (urlsplit(origin).hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
                    return 403
                path = urlsplit(self.path).path.rstrip("/")
                supplied = None
                if path.startswith("/mcp/"):
                    supplied = path[len("/mcp/"):]
                elif path == "/mcp":
                    auth = self.headers.get("Authorization") or ""
                    supplied = auth[7:] if auth.lower().startswith("bearer ") else None
                else:
                    return 404
                if supplied is None or not hmac.compare_digest(supplied.encode(), outer.token.encode()):
                    return 401
                return 0

            def do_POST(self):
                status = self._allowed()
                if status:
                    return self._reply(status, json.dumps({"error": "not allowed"}).encode())
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if not 0 < length <= MAX_BODY:
                    return self._reply(413, b'{"error": "bad length"}')
                try:
                    message = json.loads(self.rfile.read(length).decode("utf-8"))
                except ValueError:
                    return self._reply(400, json.dumps(_error(None, _PARSE_ERROR, "parse error")).encode())
                reply = outer.handler.handle(message)
                if reply is None:
                    return self._reply(202)
                self._reply(200, json.dumps(reply, ensure_ascii=False).encode("utf-8"))

            def _no(self):
                status = self._allowed()
                self._reply(status or 405, b"", extra=[("Allow", "POST")])

            do_GET = do_DELETE = do_PUT = _no

        self._server = _QuietServer(("127.0.0.1", port), Request)
        self._thread = None

    @property
    def port(self):
        return self._server.server_address[1]

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True,
                                        name="PeelConvert-mcp")
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


# ---- stdio ---------------------------------------------------------------------------------
def serve_stdio(stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    handler = Handler()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handler.handle(json.loads(line))
        except ValueError:
            reply = _error(None, _PARSE_ERROR, "parse error")
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def main():
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    serve_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
