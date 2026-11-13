"""Test drivers for copper-agent, loaded through COPPER_AGENT_PLUGINS (never shipped, never imported by the engine).

    kind 'test-script'   the turn's text is a JSON list of steps, executed in order:
        {"text": "..."}                 emit text
        {"sleep": s}                    wait cooperatively (checks for cancel every 20 ms)
        {"block": s}                    wait WITHOUT checking for cancel (a badly behaved driver)
        {"fail": kind, "code"?: str}    raise DriverError(kind)
        {"crash": "message"}            raise RuntimeError
        {"tool": name, "args": {...}}   turn.call_tool; emits the first line of the result as text
        {"ask": {...}}                  host.ask_permission; emits "granted=<bool>"
        {"usage": [in, out, cached]}    turn.usage
        {"history": true}               emit the conversation history as JSON
        {"notes": true}                 emit turn.notes() as JSON
        {"prompt": true}                emit prompt.user_turn(...) for this turn

    kind 'test-stream'   GET <account.base_url>/<turn text> from a local test server and relay "data: ..." lines as
                         text until "data: [DONE]". The stream ending without that marker is a truncated stream.
"""
from __future__ import annotations

import json
import time
import urllib.request

from copper_kicad.agent import prompt
from copper_kicad.agent.drivers import register
from copper_kicad.agent.drivers.base import Driver, DriverError, stream_lines


@register
class ScriptDriver(Driver):
    kind = "test-script"
    label = "Scripted test driver"

    def run(self, turn) -> None:
        for step in json.loads(turn.request.text):
            turn.check_cancel()
            if "text" in step:
                turn.text(step["text"])
            elif "sleep" in step:
                end = time.time() + step["sleep"]
                while time.time() < end:
                    turn.check_cancel()
                    turn.touch()
                    time.sleep(0.02)
            elif "block" in step:
                time.sleep(step["block"])
            elif "fail" in step:
                raise DriverError(step["fail"], f"scripted {step['fail']} failure", code=step.get("code"))
            elif "crash" in step:
                raise RuntimeError(step["crash"])
            elif "tool" in step:
                out = turn.call_tool(step["tool"], step.get("args") or {})
                turn.text(("ok: " if out["ok"] else "failed: ") + (out["text"].splitlines() or [""])[0] + "\n")
            elif "ask" in step:
                turn.text(f"granted={turn.host.ask_permission('test', step['ask']).get('granted')}\n")
            elif "usage" in step:
                turn.usage(*step["usage"])
            elif "history" in step:
                turn.text(json.dumps(turn.history()))
            elif "notes" in step:
                turn.text(json.dumps(turn.notes()))
            elif "prompt" in step:
                turn.text(prompt.user_turn("<text>", turn.focus, turn.notes()))


@register
class StreamDriver(Driver):
    kind = "test-stream"
    label = "Streaming test driver"
    needs = ("base_url",)

    def test(self, account):
        return {"ok": bool(account.get("base_url")), "message": "" if account.get("base_url") else "no base_url"}

    def run(self, turn) -> None:
        url = turn.request.account["base_url"].rstrip("/") + "/" + turn.request.text.strip()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            resp = opener.open(url, timeout=20)
        except OSError as e:
            raise DriverError("network", f"cannot reach the test server: {type(e).__name__}", code="CONNECT") from None
        complete = False
        with resp:
            for line in stream_lines(resp, turn):
                if line == "data: [DONE]":
                    complete = True
                    break
                if line.startswith("data: "):
                    turn.text(line[6:])
        if not complete:
            raise DriverError.truncated()
