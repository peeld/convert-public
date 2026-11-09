"""Talk to an AI service for "Describe a conversion".  Standard library only, no SDK, so the
embedded Python needs nothing new.  One provider today (Anthropic's Messages API); the loop in
agent_loop.py only sees `complete()`, so another provider can be added by writing a class with
the same method.

Messages use Anthropic's own shape (a list of {"role", "content": [blocks]}), which the loop keeps.

    model = AnthropicModel(api_key, "model-name")
    reply = model.complete(system_text, messages, tools, cancel)
    reply.text, reply.tool_calls, reply.content (to append to messages), reply.usage

Failures raise ModelError with a message fit to show the person and a `kind`: auth, rate,
network, server, bad_request, cancelled.
"""
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_TOKENS = 4096
_RETRY_STATUS = {429, 500, 502, 503, 504, 529}


class ModelError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Reply:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    content: list = field(default_factory=list)       # assistant blocks, to put back in the history
    stop_reason: str = ""
    usage: dict = field(default_factory=dict)         # input, output, cache_read, cache_write


class AnthropicModel:
    def __init__(self, api_key, model, url=API_URL, timeout=120.0, retries=2, sleep=time.sleep):
        self.api_key = api_key
        self.model = model
        self.url = url
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep

    # ---- the one call the loop uses ----
    def complete(self, system, messages, tools, cancel=None):
        body = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            # the system text and the tool list are the same every request: let the service cache them
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "tools": self._tool_defs(tools),
        }
        data = self._post(body, cancel)
        return self._reply(data)

    @staticmethod
    def _tool_defs(tools):
        defs = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in tools]
        if defs:
            defs[-1]["cache_control"] = {"type": "ephemeral"}
        return defs

    # ---- http ----
    def _post(self, body, cancel):
        payload = json.dumps(body).encode("utf-8")
        headers = {"content-type": "application/json", "x-api-key": self.api_key,
                   "anthropic-version": API_VERSION}
        attempt = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise ModelError("cancelled", "Cancelled.")
            try:
                request = urllib.request.Request(self.url, payload, headers, method="POST")
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = self._detail(exc)
                if exc.code in _RETRY_STATUS and attempt < self.retries:
                    attempt += 1
                    self._sleep(2.0 * attempt)
                    continue
                raise self._error(exc.code, detail)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                if attempt < self.retries:
                    attempt += 1
                    self._sleep(2.0 * attempt)
                    continue
                raise ModelError("network", "Could not reach the AI service. Check your internet connection "
                                            "and try again.")
            except ValueError:
                raise ModelError("server", "The AI service sent a reply that could not be read. Try again.")

    @staticmethod
    def _detail(exc):
        try:
            return json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except (ValueError, OSError, AttributeError):
            return ""

    @staticmethod
    def _error(status, detail):
        if status in (401, 403):
            return ModelError("auth", "The AI service did not accept your API key. Open Settings and check it.")
        if status == 429:
            return ModelError("rate", "The AI service is busy or your rate limit was reached. "
                                      "Wait a minute and try again.")
        if status >= 500:
            return ModelError("server", "The AI service had a problem (%d). Try again in a moment." % status)
        extra = (" It said: " + detail) if detail else ""
        return ModelError("bad_request", "The AI service rejected the request." + extra
                          + " If it mentions the model, change the model name in Settings.")

    @staticmethod
    def _reply(data):
        content = [b for b in data.get("content", []) if isinstance(b, dict)]
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        calls = [ToolCall(b.get("id", ""), b.get("name", ""), b.get("input") if isinstance(b.get("input"), dict) else {})
                 for b in content if b.get("type") == "tool_use"]
        usage = data.get("usage", {})
        return Reply(text=text, tool_calls=calls, content=content, stop_reason=data.get("stop_reason", ""),
                     usage={"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0),
                            "cache_read": usage.get("cache_read_input_tokens", 0),
                            "cache_write": usage.get("cache_creation_input_tokens", 0)})
