"""Driver for any OpenAI-compatible chat-completions endpoint (``POST <base_url>/chat/completions``), streaming,
with tool calls. Hosted providers, routers and local servers (Ollama, LM Studio, llama.cpp, vLLM, ...) speak it.

Account kind ``api-chat-completions``.

    account: base_url (required, up to and including the version segment, e.g. http://localhost:11434/v1),
             secret (optional: local servers need none), default_model (the user's choice; nothing is assumed here)

What is sent: ``messages`` = system (prompt.system_prefix(mode), byte-stable so providers that cache prefixes can),
earlier turns as text, the user turn; ``tools`` = prompt.tool_catalogue(mode) as function tools; ``stream: true``
with ``stream_options.include_usage``. A server that rejects ``stream_options`` (HTTP 400 naming it) is asked
again without it, once per turn. No sampling parameters and no output limit are sent: provider defaults apply.

Streaming tool calls arrive as fragments keyed by ``index``; they are assembled, run in order through
``turn.call_tool`` and answered with one ``tool`` message each. A missing call id (some local servers) is filled in.
Reasoning text (``reasoning_content`` / ``reasoning`` deltas) goes out on the reasoning channel and is not sent back.

End of stream: a ``finish_reason`` must have arrived (``data: [DONE]`` alone is not enough, and some servers never
send it); otherwise error(network, TRUNCATED). finish_reason ``length`` -> error(engine, MAX_TOKENS),
``content_filter`` -> error(engine, REFUSED).

Local models are often weak at multi-step edits; that is said in the label of the account, not enforced here.
"""
from __future__ import annotations

import json
import os

from .. import accounts, prompt
from . import register
from ._http import Api, budget_left, run_with_retry, stream_error
from .base import Driver, DriverError


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


@register
class ChatCompletionsDriver(Driver):
    kind = "api-chat-completions"
    label = "OpenAI-compatible API (any base URL, including local servers)"
    needs = ("base_url", "secret")          # the secret may be left empty for a local server

    def _api(self, account: dict, turn=None) -> Api:
        if not account.get("base_url"):
            raise DriverError("protocol", "this account has no base URL", code="BAD_BASE_URL", retryable=False,
                              fix="set the base URL, for example http://localhost:11434/v1")
        key = accounts.secret(account.get("id") or "") if account.get("has_secret") else None
        return Api(account["base_url"], {"Authorization": f"Bearer {key}"} if key else {}, key, turn)

    def models(self, account: dict) -> list[dict]:
        data = self._api(account).get_json("/models").get("data") or []
        return [{"id": m["id"], "label": m["id"], "quick": False} for m in data if isinstance(m, dict) and m.get("id")]

    def test(self, account: dict) -> dict:
        try:
            models = self.models(account)
        except DriverError as e:
            if e.code == "HTTP_404":            # a server without a model list still serves completions
                return {"ok": True, "message": "reachable; this server has no model list, type the model id", "models": []}
            return {"ok": False, "message": e.message, "kind": e.kind, "code": e.code}
        return {"ok": True, "message": f"{len(models)} model(s) available", "models": models}

    def run(self, turn) -> None:
        req = turn.request
        if not req.model:
            raise DriverError("engine", "no model is chosen for this account", code="NO_MODEL", retryable=False,
                              fix="pick a model for this account (accounts.test lists them)")
        api = self._api(req.account, turn)
        tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                   "parameters": t["input_schema"]}}
                 for t in prompt.tool_catalogue(req.mode)]
        messages = [{"role": "system", "content": prompt.system_prefix(req.mode)}]
        messages += [{"role": m["role"], "content": m["text"]} for m in turn.history()]
        messages.append({"role": "user", "content": prompt.user_turn(req.text, req.focus, turn.notes())})
        used = {"in": 0, "out": 0, "cached": 0}
        opts = {"usage": True}
        max_steps = _int_env("COPPER_AGENT_MAX_STEPS", 40)

        for step in range(max_steps):
            turn.check_cancel()
            if step:
                budget_left(turn, used["in"] + used["out"])
            text, calls, finish, usage = run_with_retry(turn, lambda state: self._once(api, req.model, messages, tools,
                                                                                       opts, turn, state))
            for k in used:
                used[k] += usage[k]
            if any(usage.values()):
                turn.usage(used["in"], used["out"], used["cached"], model=req.model)
            if finish == "content_filter":
                raise DriverError("engine", "the provider's content filter stopped the answer", code="REFUSED", retryable=False)
            if finish == "length":
                raise DriverError("engine", "the answer was cut off at the output limit", code="MAX_TOKENS", retryable=False,
                                  fix="ask for a smaller step")
            if not calls:
                return
            messages.append({"role": "assistant", "content": text or None,
                             "tool_calls": [{"id": c["id"], "type": "function",
                                             "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                                            for c in calls]})
            for c in calls:
                turn.check_cancel()
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": _run_call(c, turn)})
        raise DriverError("engine", f"the turn needed more than {max_steps} model requests", code="MAX_STEPS",
                          retryable=False, fix="continue in a new message")

    def _once(self, api: Api, model: str, messages: list, tools: list, opts: dict, turn, state: dict):
        """One streamed request -> (answer text, [tool call], finish_reason, usage of this request)."""
        body = {"model": model, "messages": messages, "stream": True}
        if tools:
            body["tools"] = tools
        if opts["usage"]:
            body["stream_options"] = {"include_usage": True}
        try:
            stream = api.stream("/chat/completions", body)
        except DriverError as e:
            if opts["usage"] and e.code in ("HTTP_400", "HTTP_422") and "stream_options" in e.message:
                opts["usage"] = False                       # an older / smaller server: ask again without it
                return self._once(api, model, messages, tools, opts, turn, state)
            raise
        text: list[str] = []
        calls: dict = {}
        finish = None
        usage = {"in": 0, "out": 0, "cached": 0}
        with stream:
            for _name, data in stream.events():
                if data.strip() == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except ValueError:
                    raise DriverError("protocol", "the model stream carried a line that is not JSON", code="BAD_STREAM",
                                      retryable=False) from None
                if not isinstance(ev, dict):
                    continue
                if ev.get("error"):
                    err = ev["error"] if isinstance(ev["error"], dict) else {"message": str(ev["error"])}
                    raise stream_error(str(err.get("type") or err.get("code") or ""), str(err.get("message") or ""), api.secret)
                u = ev.get("usage")
                if isinstance(u, dict):
                    cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                    usage = {"in": int(u.get("prompt_tokens") or 0), "out": int(u.get("completion_tokens") or 0),
                             "cached": cached}
                for choice in ev.get("choices") or []:
                    if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta") or {}
                    thought = delta.get("reasoning_content") or delta.get("reasoning")
                    if isinstance(thought, str) and thought:
                        state["emitted"] = True
                        turn.text(thought, channel="reasoning")
                    if isinstance(delta.get("content"), str) and delta["content"]:
                        state["emitted"] = True
                        text.append(delta["content"])
                        turn.text(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        if not isinstance(tc, dict):
                            continue
                        slot = calls.setdefault(tc.get("index", len(calls) if tc.get("id") else max(len(calls) - 1, 0)),
                                                {"id": "", "name": "", "arguments": ""})
                        fn = tc.get("function") or {}
                        slot["id"] = tc.get("id") or slot["id"]
                        slot["name"] += fn.get("name") or ""
                        slot["arguments"] += fn.get("arguments") or ""
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        if finish is None:
            raise DriverError.truncated()
        out = []
        for n, index in enumerate(sorted(calls, key=lambda i: (not isinstance(i, int), i))):
            c = calls[index]
            if c["name"]:
                c["id"] = c["id"] or f"call_{n + 1}"
                out.append(c)
        return "".join(text), out, finish, usage


def _run_call(call: dict, turn) -> str:
    try:
        args = json.loads(call["arguments"]) if call["arguments"].strip() else {}
        if not isinstance(args, dict):
            raise ValueError("not an object")
    except ValueError:
        return json.dumps({"error": "INVALID_JSON", "message": "the tool arguments were not a JSON object",
                           "fix": "call the tool again with valid JSON arguments"})
    out = turn.call_tool(call["name"], args)
    text = out["text"] or ("ok" if out["ok"] else "failed")
    if out.get("images"):
        text += f"\n({len(out['images'])} image(s) were produced; this connection carries text only)"
    return text
