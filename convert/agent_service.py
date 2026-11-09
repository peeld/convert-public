"""Whether the app accepts AI agents, and the settings that go with it.

Pure Python (no Qt).  The choice and the secret link live in
%APPDATA%\\PeelConvert\\agent.json so they survive restarts and updates:

    {"enabled": true, "port": 47615, "token": "<random>"}

The server only listens on 127.0.0.1 and is off until the person turns it on in the
"Connect an AI agent" window (agent_ui.py).
"""
import json
import os
import secrets
import threading

from .conversion import user_data_dir
from .mcp_server import McpHttpServer

DEFAULT_PORT = 47615
SERVER_NAME = "peelconvert"


def config_path():
    return user_data_dir() / "agent.json"


class AgentService:
    def __init__(self, path=None, port_default=DEFAULT_PORT):
        self.path = path or config_path()
        self._lock = threading.RLock()
        self._server = None
        self.last_error = ""
        data = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self.enabled = bool(data.get("enabled", False))
        self.port = int(data["port"]) if isinstance(data.get("port"), int) else port_default
        self.token = data.get("token") if isinstance(data.get("token"), str) and len(data["token"]) >= 16 \
            else secrets.token_urlsafe(24)

    # ---- state ----
    @property
    def running(self):
        return self._server is not None

    def url(self):
        return "http://127.0.0.1:%d/mcp/%s" % (self.port, self.token)

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            temp.write_text(json.dumps({"enabled": self.enabled, "port": self.port, "token": self.token},
                                       indent=2) + "\n", encoding="utf-8")
            os.replace(temp, self.path)
        except OSError as exc:
            self.last_error = "could not save %s: %s" % (self.path.name, exc)

    # ---- control ----
    def start(self):
        """Listen.  Returns True when running.  If the usual port is taken, another is used (and kept)."""
        with self._lock:
            if self._server is not None:
                return True
            self.last_error = ""
            server = None
            for port in (self.port, 0):
                try:
                    server = McpHttpServer(self.token, port)
                    break
                except OSError as exc:
                    self.last_error = "could not listen on port %d: %s" % (port, exc)
            if server is None:
                return False
            server.start()
            self._server = server
            self.last_error = ""
            self.port = server.port
            self.enabled = True
            self._save()
            return True

    def stop(self):
        with self._lock:
            server, self._server = self._server, None
            if server is not None:
                server.stop()

    def set_enabled(self, on):
        with self._lock:
            if on:
                return self.start()
            self.stop()
            self.enabled = False
            self._save()
            return True

    def reset_link(self):
        """A new secret: agents connected with the old link stop working."""
        with self._lock:
            was_running = self.running
            self.stop()
            self.token = secrets.token_urlsafe(24)
            self._save()
            if was_running:
                self.start()


# ---- what to tell the person, per AI app -------------------------------------------------
def client_setups(url):
    """[(name, where_to_put_it, text_to_copy)] for the apps people use."""
    server_json = json.dumps({"mcpServers": {SERVER_NAME: {"url": url}}}, indent=2)
    return [
        ("Claude Code", "Open a terminal and paste this once:",
         "claude mcp add --transport http %s %s" % (SERVER_NAME, url)),
        ("Cursor", "Add this to  %USERPROFILE%\\.cursor\\mcp.json  (create the file if it is missing), "
                   "then restart Cursor:", server_json),
        ("VS Code (Copilot)", "Run the command \"MCP: Open User Configuration\" and add this, then start "
                              "the server from the editor:",
         json.dumps({"servers": {SERVER_NAME: {"type": "http", "url": url}}}, indent=2)),
        ("Claude Desktop", "Needs Node.js installed. Add this to  %APPDATA%\\Claude\\claude_desktop_config.json  "
                           "(Settings > Developer > Edit Config), then restart Claude:",
         json.dumps({"mcpServers": {SERVER_NAME: {"command": "npx",
                                                    "args": ["-y", "mcp-remote", url, "--allow-http"]}}}, indent=2)),
        ("Codex CLI", "Add this to  ~/.codex/config.toml , then restart Codex:",
         "[mcp_servers.%s]\nurl = \"%s\"" % (SERVER_NAME, url)),
        ("Another app", "Add a new MCP server of type \"HTTP\" / \"Streamable HTTP\" with this address:", url),
    ]
