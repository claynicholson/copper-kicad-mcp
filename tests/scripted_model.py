"""A scripted fake model behind a local chat-completions endpoint, for tests/test_agent_conversations.py.
Loopback only; nothing leaves the machine.

    srv = ScriptedModel(key).start();  srv.url         base URL including /v1
    srv.script(model, fn)                              fn(ctx) -> step, called once per request naming that model
    srv.requests                                       [{'model','n','headers','body'}] in arrival order

ctx:  {'n': 1-based request number for this model, 'body': the request, 'user': text of the last user message,
       'results': [str] the tool results that followed the last assistant message, 'all_results': every tool result}
step: {'text': str, 'calls': [(tool name, arguments dict)], 'finish': str | None,
       'hold': threading.Event | None (wait for it before sending the calls), 'cut': bool (close without a finish),
       'status': int, 'error': str (answer with an HTTP error instead)}
A step with calls ends with finish_reason 'tool_calls', one without ends with 'stop'. Text arrives in small pieces
and tool arguments in fragments, as real providers send them.

The server refuses any request that does not carry ``Authorization: Bearer <key>`` (401), so a turn that works
proves the secret went out in the header and nowhere else needs it.
"""
from __future__ import annotations

import http.server
import json
import threading


class ScriptedModel:
    def __init__(self, key: str):
        self.key = key
        self.requests: list = []
        self.scripts: dict = {}
        self.counts: dict = {}
        self.lock = threading.Lock()
        self.httpd = None
        self.url = ""

    def script(self, model: str, fn) -> str:
        self.scripts[model] = fn
        self.counts[model] = 0
        return model

    def of(self, model: str) -> list:
        return [r for r in self.requests if r["model"] == model]

    def start(self) -> "ScriptedModel":
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def json(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def chunk(self, delta=None, finish=None, usage=None):
                obj = {"id": "chatcmpl-s", "object": "chat.completion.chunk", "model": "scripted",
                       "choices": [] if delta is None and finish is None else
                       [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
                if usage:
                    obj["usage"] = usage
                self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
                self.wfile.flush()

            def do_GET(self):
                if self.headers.get("Authorization") != f"Bearer {fake.key}":
                    return self.json(401, {"error": {"message": "Incorrect API key provided", "code": "invalid_api_key"}})
                return self.json(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in fake.scripts]})

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                except ValueError:
                    return self.json(400, {"error": {"message": "bad json"}})
                model = body.get("model")
                with fake.lock:
                    n = fake.counts[model] = fake.counts.get(model, 0) + 1
                    fake.requests.append({"model": model, "n": n, "body": body,
                                          "headers": {k.lower(): v for k, v in self.headers.items()}})
                if self.headers.get("Authorization") != f"Bearer {fake.key}":
                    return self.json(401, {"error": {"message": "Incorrect API key provided", "code": "invalid_api_key"}})
                fn = fake.scripts.get(model)
                if fn is None:
                    return self.json(404, {"error": {"message": f"The model `{model}` does not exist", "code": "model_not_found"}})
                msgs = body.get("messages") or []
                results, i = [], len(msgs) - 1
                while i >= 0 and msgs[i].get("role") == "tool":
                    results.insert(0, msgs[i].get("content") or "")
                    i -= 1
                user = next((m.get("content") or "" for m in reversed(msgs) if m.get("role") == "user"), "")
                try:
                    step = fn({"n": n, "body": body, "user": user, "results": results,
                               "all_results": [m.get("content") or "" for m in msgs if m.get("role") == "tool"]}) or {}
                except Exception as e:  # noqa: BLE001 — a broken script must show up in the test, not hang it
                    return self.json(500, {"error": {"message": f"script failed: {type(e).__name__}: {e}"}})
                if step.get("status"):
                    return self.json(step["status"], {"error": {"message": step.get("error") or "refused"}})
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.chunk({"role": "assistant", "content": ""})
                    text = step.get("text") or ""
                    for k in range(0, len(text), 12):
                        self.chunk({"content": text[k:k + 12]})
                    if step.get("hold") is not None:
                        step["hold"].wait(60)
                    if step.get("cut"):
                        return None
                    calls = step.get("calls") or []
                    for idx, (name, args) in enumerate(calls):
                        self.chunk({"tool_calls": [{"index": idx, "id": f"call_{n}_{idx}", "type": "function",
                                                    "function": {"name": name, "arguments": ""}}]})
                        raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
                        for k in range(0, len(raw), 17):
                            self.chunk({"tool_calls": [{"index": idx, "function": {"arguments": raw[k:k + 17]}}]})
                    self.chunk({}, step.get("finish") or ("tool_calls" if calls else "stop"))
                    if (body.get("stream_options") or {}).get("include_usage"):
                        self.chunk(usage={"prompt_tokens": 900 + 40 * n, "completion_tokens": 30, "total_tokens": 930 + 40 * n})
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (OSError, ValueError):
                    pass                                   # the client went away (cancel / kill tests)
                return None

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        return self

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
