"""Driver for an API key on the Anthropic Messages API (``POST /v1/messages``), with tool use and streaming.

Account kind ``api-messages``. The agent runs the tool loop: the model asks for engine tools, ``turn.call_tool``
runs them in-process (live routing and tool events happen there), the results go back, until the model stops.

    account: secret (the API key, from the credential store), base_url (optional; a gateway that speaks the same
             API), default_model (chosen by the user when connecting; this file names no model)

What is sent:
    system    one text block = prompt.system_prefix(mode), marked ``cache_control: ephemeral``. Tools render before
              the system prompt, so this one breakpoint caches the tool catalogue and the system prompt together.
    tools     prompt.tool_catalogue(mode), same order every turn (a changed list would invalidate the cache)
    messages  earlier turns as plain text, then the user turn (focus line + notes + words). The newest message
              carries a second breakpoint so each step of the tool loop reads the previous step from the cache.
    Thinking is left at the model's own default (no ``thinking`` field, no sampling parameters): the accepted
    values differ per model generation and the model id is the user's choice.

The assistant's content blocks (text, thinking with its signature, tool_use) are sent back exactly as received
while the tool loop runs; they are never edited. Across turns only the answer text is kept (agent/sessions.py).

Stop reasons: end_turn -> done; tool_use -> run the tools, continue; pause_turn -> continue; max_tokens ->
error(engine, MAX_TOKENS); refusal -> error(engine, REFUSED). A stream that ends without ``message_stop`` is
error(network, TRUNCATED).

Environment: COPPER_AGENT_MAX_TOKENS (16000) per response, COPPER_AGENT_MAX_STEPS (40) model requests per turn,
COPPER_AGENT_API_VERSION (the ``anthropic-version`` header, default 2023-06-01), and the retry settings of _http.py.
"""
from __future__ import annotations

import json
import os

from .. import accounts, prompt
from . import register
from ._http import Api, budget_left, run_with_retry, stream_error
from .base import Driver, DriverError

DEFAULT_BASE = "https://api.anthropic.com"
API_VERSION = "2023-06-01"


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


@register
class MessagesApiDriver(Driver):
    kind = "api-messages"
    label = "Anthropic API key"
    needs = ("secret",)

    # ── account ───────────────────────────────────────────────────────────
    def _api(self, account: dict, turn=None) -> Api:
        key = accounts.secret(account.get("id") or "")
        if not key:
            raise DriverError("auth", "this account has no API key stored", code="NO_KEY", retryable=False,
                              fix="connect the account again and enter the key")
        headers = {"x-api-key": key, "anthropic-version": os.environ.get("COPPER_AGENT_API_VERSION") or API_VERSION}
        return Api(account.get("base_url") or DEFAULT_BASE, headers, key, turn)

    def models(self, account: dict) -> list[dict]:
        """The models this key can use, asked from the provider (newest first, as the API lists them)."""
        api, out, after = self._api(account), [], None
        for _ in range(5):
            page = api.get_json("/v1/models?limit=1000" + (f"&after_id={after}" if after else ""))
            for m in page.get("data") or []:
                if isinstance(m, dict) and m.get("id"):
                    out.append({"id": m["id"], "label": m.get("display_name") or m["id"], "quick": False})
            after = page.get("last_id")
            if not page.get("has_more") or not after:
                break
        return out

    def test(self, account: dict) -> dict:
        try:
            models = self.models(account)
        except DriverError as e:
            return {"ok": False, "message": e.message, "kind": e.kind, "code": e.code}
        return {"ok": True, "message": f"{len(models)} model(s) available", "models": models}

    # ── one turn ──────────────────────────────────────────────────────────
    def run(self, turn) -> None:
        req = turn.request
        if not req.model:
            raise DriverError("engine", "no model is chosen for this account", code="NO_MODEL", retryable=False,
                              fix="pick a model for this account (accounts.test lists them)")
        api = self._api(req.account, turn)
        system = [{"type": "text", "text": prompt.system_prefix(req.mode), "cache_control": {"type": "ephemeral"}}]
        tools = prompt.tool_catalogue(req.mode)
        messages = [{"role": m["role"], "content": m["text"]} for m in turn.history()]
        messages.append({"role": "user", "content": [{"type": "text", "text": prompt.user_turn(req.text, req.focus, turn.notes())}]})
        used = {"in": 0, "out": 0, "cached": 0}
        max_tokens, max_steps = _int_env("COPPER_AGENT_MAX_TOKENS", 16000), _int_env("COPPER_AGENT_MAX_STEPS", 40)

        for step in range(max_steps):
            turn.check_cancel()
            left = budget_left(turn, used["in"] + used["out"]) if step else None
            body = {"model": req.model, "max_tokens": max_tokens if left is None else max(256, min(max_tokens, left)),
                    "system": system, "messages": _with_breakpoint(messages), "stream": True}
            if tools:
                body["tools"] = tools
            blocks, stop, usage = run_with_retry(turn, lambda state: self._once(api, body, turn, state))
            used["in"] += usage["in"]
            used["out"] += usage["out"]
            used["cached"] += usage["cached"]
            turn.usage(used["in"], used["out"], used["cached"], model=req.model)
            calls = [b for b in blocks if b.get("type") == "tool_use"]
            if stop == "refusal":
                raise DriverError("engine", "the model declined this request", code="REFUSED", retryable=False)
            if stop == "max_tokens":
                raise DriverError("engine", "the answer was cut off at the output limit", code="MAX_TOKENS", retryable=False,
                                  fix="ask for a smaller step, or raise COPPER_AGENT_MAX_TOKENS")
            if stop == "pause_turn":
                messages.append({"role": "assistant", "content": blocks})
                continue
            if stop != "tool_use" or not calls:
                return
            messages.append({"role": "assistant", "content": blocks})
            results = []
            for call in calls:                                # in order; all results go back in ONE user message
                turn.check_cancel()
                results.append(_tool_result(call, turn))
            messages.append({"role": "user", "content": results})
        raise DriverError("engine", f"the turn needed more than {max_steps} model requests", code="MAX_STEPS",
                          retryable=False, fix="continue in a new message")

    def _once(self, api: Api, body: dict, turn, state: dict):
        """One streamed request -> (content blocks, stop_reason, usage of this request)."""
        blocks: dict = {}
        partial: dict = {}
        usage = {"in": 0, "out": 0, "cached": 0}
        stop, complete = None, False
        with api.stream("/v1/messages", body) as stream:
            for name, data in stream.events():
                try:
                    ev = json.loads(data)
                except ValueError:
                    raise DriverError("protocol", "the model stream carried a line that is not JSON", code="BAD_STREAM",
                                      retryable=False) from None
                kind = ev.get("type") or name
                if kind == "message_start":
                    _add_usage(usage, (ev.get("message") or {}).get("usage"))
                elif kind == "content_block_start":
                    block = dict(ev.get("content_block") or {})
                    blocks[ev.get("index")] = block
                    if block.get("type") == "tool_use":
                        partial[ev.get("index")] = []
                    elif block.get("type") == "text" and block.get("text"):
                        state["emitted"] = True
                        turn.text(block["text"])
                elif kind == "content_block_delta":
                    block, d = blocks.get(ev.get("index")), ev.get("delta") or {}
                    if block is None:
                        continue
                    t = d.get("type")
                    if t == "text_delta":
                        block["text"] = block.get("text", "") + d.get("text", "")
                        state["emitted"] = True
                        turn.text(d.get("text", ""))
                    elif t == "input_json_delta":
                        partial.setdefault(ev.get("index"), []).append(d.get("partial_json", ""))
                    elif t == "thinking_delta":
                        block["thinking"] = block.get("thinking", "") + d.get("thinking", "")
                        if d.get("thinking"):
                            state["emitted"] = True
                            turn.text(d["thinking"], channel="reasoning")
                    elif t == "signature_delta":
                        block["signature"] = block.get("signature", "") + d.get("signature", "")
                    elif t == "citations_delta":
                        block.setdefault("citations", []).append(d.get("citation"))
                elif kind == "message_delta":
                    stop = (ev.get("delta") or {}).get("stop_reason") or stop
                    _add_usage(usage, ev.get("usage"), final=True)
                elif kind == "message_stop":
                    complete = True
                    break
                elif kind == "error":
                    err = ev.get("error") or {}
                    raise stream_error(err.get("type", ""), err.get("message", ""), api.secret)
                # ping and event types added later: ignored
        if not complete:
            raise DriverError.truncated()
        out = []
        for index in sorted(blocks, key=lambda i: (i is None, i)):
            block = blocks[index]
            if block.get("type") == "tool_use":
                raw = "".join(partial.get(index) or [])
                try:
                    block["input"] = json.loads(raw) if raw.strip() else (block.get("input") or {})
                    if not isinstance(block["input"], dict):
                        raise ValueError("not an object")
                except ValueError:
                    block["input"] = {}
                    block["_bad_input"] = raw[:200]
            out.append(block)
        return out, stop, usage


def _add_usage(total: dict, u, final: bool = False) -> None:
    """message_start carries the input side; message_delta the (cumulative) output side of one request."""
    if not isinstance(u, dict):
        return
    if not final:
        read = int(u.get("cache_read_input_tokens") or 0)
        total["in"] = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + read
        total["cached"] = read
    if u.get("output_tokens") is not None:
        total["out"] = int(u.get("output_tokens") or 0)


def _with_breakpoint(messages: list) -> list:
    """A copy of ``messages`` whose newest message ends in a cache breakpoint (the stored list stays unmarked, so the
    marker moves forward with the conversation instead of piling up: at most two breakpoints per request)."""
    if not messages:
        return messages
    last = messages[-1]
    content = last["content"]
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not content:
        return messages
    marked = list(content)
    marked[-1] = dict(marked[-1], cache_control={"type": "ephemeral"})
    return messages[:-1] + [dict(last, content=marked)]


def _tool_result(call: dict, turn) -> dict:
    """Run one requested tool and shape its outcome as a tool_result block."""
    bad = call.pop("_bad_input", None)
    if bad is not None:
        text = json.dumps({"error": "INVALID_JSON", "message": "the tool arguments were not a JSON object",
                           "fix": "call the tool again with valid JSON arguments"})
        return {"type": "tool_result", "tool_use_id": call.get("id"), "content": text, "is_error": True}
    out = turn.call_tool(str(call.get("name") or ""), call.get("input") or {})
    content = [{"type": "text", "text": out["text"] or ("ok" if out["ok"] else "failed")}]
    for img in out.get("images") or []:
        if img.get("data"):
            content.append({"type": "image", "source": {"type": "base64", "media_type": img.get("mime") or "image/png",
                                                        "data": img["data"]}})
    res = {"type": "tool_result", "tool_use_id": call.get("id"), "content": content}
    if not out["ok"]:
        res["is_error"] = True
    return res
