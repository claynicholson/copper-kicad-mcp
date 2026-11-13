"""A test driver for tests/test_agent_commands.py, loaded through COPPER_AGENT_PLUGINS (never shipped).

    kind 'test-key'    an account with a secret and a base_url, served by the local fake HTTP server of the test:
        test(account)  GET <base_url>/models with the header 'X-Test-Key: <secret>'
                       200 -> {'ok': True, 'models': [...]}; 401 -> DriverError('auth'); unreachable -> 'network'
        models()       two fixed ids
        run(turn)      GET <base_url>/turn?q=<text>: the JSON answer {'text','in','out','cached','cost'?,'model'?}
                       becomes turn.text + turn.usage (what a real driver reports from its provider)
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from copper_kicad.agent import accounts
from copper_kicad.agent.drivers import register
from copper_kicad.agent.drivers.base import Driver, DriverError


def _get(account: dict, path: str) -> dict:
    req = urllib.request.Request(account["base_url"].rstrip("/") + path,
                                 headers={"X-Test-Key": accounts.secret(account["id"]) or ""})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise DriverError("auth", "the test server refused the key", code="HTTP_401") from None
        raise DriverError("protocol", f"the test server answered {e.code}", code=f"HTTP_{e.code}") from None
    except OSError as e:
        raise DriverError("network", f"cannot reach the test server: {type(e).__name__}", code="CONNECT") from None


@register
class KeyDriver(Driver):
    kind = "test-key"
    label = "Test account with a key"
    needs = ("secret", "base_url")
    streaming = False

    def models(self, account):
        return [{"id": "test-large", "label": "Large", "quick": False}, {"id": "test-small", "label": "Small", "quick": True}]

    def test(self, account):
        got = _get(account, "/models")
        return {"ok": True, "message": f"{len(got.get('models') or [])} model(s)"}

    def run(self, turn) -> None:
        got = _get(turn.request.account, "/turn?q=" + urllib.parse.quote(turn.request.text))
        turn.text(got.get("text") or "")
        turn.usage(got["in"], got["out"], got.get("cached", 0), cost=got.get("cost"),
                   model=got.get("model") or turn.request.model)
