"""A local stand-in for model providers, for tests/test_agent_drivers.py. Loopback only; nothing leaves the machine.

    srv = FakeModels().start();  srv.url                 base URL (http://127.0.0.1:<port>)
    srv.requests                                         [{'path','method','headers','body','n'}] in arrival order
    srv.of(model) -> [request]                           the requests that named this model
    srv.release                                          threading.Event: lets a "stall" scenario finish

It serves both API shapes; the request's ``model`` field picks the scenario and ``n`` is how many requests have
named that model on that path so far (1-based), so a scenario can answer differently on a retry or on the second loop step.

    POST /v1/messages            server-sent events in the messages-API format
    GET  /v1/models              a model list in that API's shape
    POST /v1/chat/completions    server-sent events in the chat-completions format
    GET  /v1/models  with an Authorization header -> the chat-completions list shape; /nolist/models -> 404

Scenarios (both shapes unless noted): text, tools, rate, ratelong, auth, cut, cutearly, stall, five, maxtok,
budget, edit; messages only: streamerr, thinking is part of tools; chat only: nostreamopts, nokey, fragments.
"""
from __future__ import annotations

import http.server
import json
import threading

KEY = "sk-test-DRIVER-KEY-41c2"
EDIT_OPS = {"ops": [{"op": "set_field", "ref": "R1", "field": "Value", "value": "4k7"}]}


class FakeModels:
    def __init__(self):
        self.requests: list = []
        self.counts: dict = {}
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.httpd = None
        self.url = ""

    def start(self) -> "FakeModels":
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _note(self, body):
                model = (body or {}).get("model")
                with fake.lock:
                    key = (self.path, model)
                    n = fake.counts[key] = fake.counts.get(key, 0) + 1
                    fake.requests.append({"path": self.path, "method": self.command, "body": body, "n": n,
                                          "headers": {k.lower(): v for k, v in self.headers.items()}})
                return model, n

            def json(self, status, obj, headers=None):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def sse(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

            def ev(self, obj, name=None):
                self.wfile.write(((f"event: {name}\n" if name else "") + "data: " + (obj if isinstance(obj, str) else json.dumps(obj))
                                  + "\n\n").encode())
                self.wfile.flush()

            def do_GET(self):
                self._note(None)
                if self.path.startswith("/v1/models"):
                    if self.headers.get("x-api-key") is None:           # the chat-completions list shape
                        return self.json(200, {"object": "list", "data": [{"id": "local-small", "object": "model"},
                                                                         {"id": "local-large", "object": "model"}]})
                    if self.headers.get("x-api-key") != KEY:
                        return self.json(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
                    return self.json(200, {"data": [{"id": "model-a", "display_name": "Model A", "type": "model"},
                                                    {"id": "model-b", "display_name": "Model B", "type": "model"}],
                                           "has_more": False, "first_id": "model-a", "last_id": "model-b"})
                return self.json(404, {"error": {"message": "no such route", "type": "not_found_error"}})

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                except ValueError:
                    return self.json(400, {"error": {"message": "bad json", "type": "invalid_request_error"}})
                model, n = self._note(body)
                try:
                    if self.path == "/v1/messages":
                        _messages(self, fake, body, model, n)
                    elif self.path.endswith("/chat/completions"):
                        _chat(self, fake, body, model, n)
                    else:
                        self.json(404, {"error": {"message": "no such route", "type": "not_found_error"}})
                except (OSError, ValueError):
                    pass                                   # the client went away (cancel tests)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        return self

    def of(self, model: str) -> list:
        return [r for r in self.requests if (r["body"] or {}).get("model") == model]

    def stop(self):
        self.release.set()
        if self.httpd:
            self.httpd.shutdown()


# ── messages format ───────────────────────────────────────────────────────
def _m_start(h, usage=None):
    h.sse()
    h.ev({"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "content": [],
                                               "model": "x", "stop_reason": None,
                                               "usage": usage or {"input_tokens": 100, "cache_creation_input_tokens": 10,
                                                                  "cache_read_input_tokens": 80, "output_tokens": 1}}},
         "message_start")
    h.ev({"type": "ping"}, "ping")


def _m_text(h, index, *pieces):
    h.ev({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}}, "content_block_start")
    for p in pieces:
        h.ev({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": p}}, "content_block_delta")
    h.ev({"type": "content_block_stop", "index": index}, "content_block_stop")


def _m_tool(h, index, tid, name, args):
    h.ev({"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": tid, "name": name, "input": {}}},
         "content_block_start")
    raw = json.dumps(args)
    for i in range(0, len(raw), 7):                            # the arguments arrive in fragments
        h.ev({"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": raw[i:i + 7]}},
             "content_block_delta")
    h.ev({"type": "content_block_stop", "index": index}, "content_block_stop")


def _m_end(h, stop="end_turn", out=5):
    h.ev({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": out}},
         "message_delta")
    h.ev({"type": "message_stop"}, "message_stop")


def _messages(h, fake, body, model, n):
    if model == "auth":
        return h.json(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
    if model == "rate" and n == 1:
        return h.json(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}, {"retry-after": "0.3"})
    if model == "ratelong":
        return h.json(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}, {"retry-after": "600"})
    if model == "five" and n == 1:
        return h.json(500, {"type": "error", "error": {"type": "api_error", "message": "internal"}})
    if model == "cutearly" and n == 1:
        return _m_start(h)                                      # the stream ends before any content
    if model == "streamerr" and n == 1:
        _m_start(h)
        return h.ev({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, "error")
    if model in ("text", "rate", "five", "cutearly", "streamerr"):
        _m_start(h)
        _m_text(h, 0, "Hello", " world")
        return _m_end(h)
    if model == "cut":
        _m_start(h)
        return h.ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}) or \
            h.ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}})
    if model == "stall":
        _m_start(h)
        h.ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        h.ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "thinking"}})
        return fake.release.wait(30)
    if model == "maxtok":
        _m_start(h)
        _m_text(h, 0, "A long ans")
        return _m_end(h, "max_tokens")
    if model in ("tools", "budget", "edit"):
        content = body["messages"][-1]["content"]
        if not (isinstance(content, list) and any(b.get("type") == "tool_result" for b in content)):     # the first step
            _m_start(h, {"input_tokens": 1000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 1})
            h.ev({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            h.ev({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "plan"}})
            h.ev({"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SIG123"}})
            h.ev({"type": "content_block_stop", "index": 0})
            _m_text(h, 1, "Checking.")
            if model == "edit":
                _m_tool(h, 2, "toolu_1", "sch_apply", EDIT_OPS)
            else:
                _m_tool(h, 2, "toolu_1", "copper_help", {})
                _m_tool(h, 3, "toolu_2", "no_such_tool", {"x": 1})
            return _m_end(h, "tool_use", 40)
        _m_start(h, {"input_tokens": 50, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 1000, "output_tokens": 1})
        results = [b for b in body["messages"][-1]["content"] if b.get("type") == "tool_result"]
        _m_text(h, 0, " Done: " + ",".join("error" if r.get("is_error") else "ok" for r in results) + ".")
        return _m_end(h, "end_turn", 12)
    return h.json(404, {"type": "error", "error": {"type": "not_found_error", "message": f"model: {model}"}})


# ── chat-completions format ───────────────────────────────────────────────
def _c(h, delta=None, finish=None, usage=None):
    obj = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "model": "x",
           "choices": [] if delta is None and finish is None else [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        obj["usage"] = usage
    h.ev(obj)


def _c_text_done(h, body, *pieces, usage=True):
    h.sse()
    _c(h, {"role": "assistant", "content": ""})
    for p in pieces:
        _c(h, {"content": p})
    _c(h, {}, "stop")
    if usage and (body.get("stream_options") or {}).get("include_usage"):
        _c(h, usage={"prompt_tokens": 200, "completion_tokens": 7, "total_tokens": 207,
                     "prompt_tokens_details": {"cached_tokens": 150}})
    h.ev("[DONE]")


def _chat(h, fake, body, model, n):
    if model == "auth":
        return h.json(401, {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error", "code": "invalid_api_key"}})
    if model == "rate" and n == 1:
        return h.json(429, {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}}, {"retry-after-ms": "300"})
    if model == "ratelong":
        return h.json(429, {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}}, {"retry-after": "600"})
    if model == "five" and n == 1:
        return h.json(503, {"error": {"message": "overloaded", "type": "server_error"}})
    if model == "nostreamopts" and "stream_options" in body:
        return h.json(400, {"error": {"message": "Unrecognized request argument supplied: stream_options", "type": "invalid_request_error"}})
    if model in ("text", "rate", "five", "nostreamopts", "nokey"):
        return _c_text_done(h, body, "Hello", " world")
    if model == "cut":
        h.sse()
        _c(h, {"role": "assistant", "content": ""})
        return _c(h, {"content": "Hel"})                       # no finish_reason, no [DONE]
    if model == "stall":
        h.sse()
        _c(h, {"content": "thinking"})
        return fake.release.wait(30)
    if model == "maxtok":
        h.sse()
        _c(h, {"content": "A long ans"})
        _c(h, {}, "length")
        return h.ev("[DONE]")
    if model in ("tools", "budget", "edit"):
        h.sse()
        if body["messages"][-1].get("role") != "tool":                                                     # the first step
            _c(h, {"role": "assistant", "content": None, "reasoning_content": "plan"})
            _c(h, {"content": "Checking."})
            if model == "edit":
                _c(h, {"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "function": {"name": "sch_apply", "arguments": ""}}]})
                raw = json.dumps(EDIT_OPS)
                for i in range(0, len(raw), 9):
                    _c(h, {"tool_calls": [{"index": 0, "function": {"arguments": raw[i:i + 9]}}]})
            else:                                               # two parallel calls, their fragments interleaved
                _c(h, {"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "function": {"name": "copper_help", "arguments": ""}}]})
                _c(h, {"tool_calls": [{"index": 1, "id": "call_b", "type": "function", "function": {"name": "no_such_tool", "arguments": "{\"x\""}}]})
                _c(h, {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]})
                _c(h, {"tool_calls": [{"index": 1, "function": {"arguments": ": 1}"}}]})
            _c(h, {}, "tool_calls")
            if (body.get("stream_options") or {}).get("include_usage"):
                _c(h, usage={"prompt_tokens": 1000, "completion_tokens": 40, "total_tokens": 1040})
            return h.ev("[DONE]")
        results = [m for m in body["messages"] if m.get("role") == "tool"]
        _c(h, {"content": " Done: " + ",".join("error" if '"error"' in (m.get("content") or "") else "ok" for m in results) + "."})
        _c(h, {}, "stop")
        if (body.get("stream_options") or {}).get("include_usage"):
            _c(h, usage={"prompt_tokens": 1080, "completion_tokens": 12, "total_tokens": 1092,
                         "prompt_tokens_details": {"cached_tokens": 1000}})
        return h.ev("[DONE]")
    return h.json(404, {"error": {"message": f"The model `{model}` does not exist", "type": "invalid_request_error", "code": "model_not_found"}})
