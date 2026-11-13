"""AI clients: detect, connect, and check that the connection really works.

    status(verify=False)            one row per client: connected / broken / found / not found
    connect(ids='all')              register Copper in each client's config, then verify by launching it
    disconnect(ids)                 remove Copper from the client's config
    verify(client_id=None)          launch handshake for every registered target
    verify_entry(entry)             the handshake itself

"Connected" is decided by evidence, not by a file existing: the exact command, arguments and environment
stored in the client's config are launched, and the child must answer MCP ``initialize`` and
``tools/list`` over stdio. That proves the registered command starts and speaks MCP on this machine. It
does not prove the client has loaded it yet (most clients read their config at start), so the restart hint
is still shown.

Only entries that Copper recognises as its own are launched: a config file is user-editable and
``--verify`` must not become a way to run whatever command it holds.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path, PureWindowsPath

from .. import SERVER_NAME, __version__
from ..install import clients
from . import redact

IS_WIN = sys.platform == "win32"
_NO_WINDOW = 0x08000000 if IS_WIN else 0
PROTOCOL = "2025-06-18"


# ── recognising our own entry ─────────────────────────────────────────────
def _cmd_args_env(entry: dict) -> tuple[str | None, list[str], dict]:
    cmd = entry.get("command")
    args = list(entry.get("args") or [])
    env = dict(entry.get("env") or {})
    if isinstance(cmd, dict):                      # legacy Zed shape {"path": …, "args": […], "env": {…}}
        args = list(cmd.get("args") or args)
        env = dict(cmd.get("env") or env)
        cmd = cmd.get("path")
    return (str(cmd) if cmd else None), [str(a) for a in args], {str(k): str(v) for k, v in env.items()}


def is_copper_entry(entry: dict | None) -> tuple[bool, str]:
    """(True, '') when ``entry`` is a launch line Copper itself would write."""
    if not isinstance(entry, dict):
        return False, "no entry"
    cmd, args, _env = _cmd_args_env(entry)
    if not cmd:
        return False, "entry has no command"
    stem = PureWindowsPath(cmd).stem.lower()     # splits on / and \: a Windows launch line reads right on any OS
    joined = " ".join(args)
    if stem.startswith("copper") and args[-1:] == ["serve"]:      # copper, copper-kicad, an AppImage named Copper-…
        return True, ""
    if stem.startswith("python") and (args[:3] == ["-m", "copper_kicad", "serve"] or (
            args[:1] == ["-c"] and "from copper_kicad.cli import main" in joined and "main(['serve'])" in joined)):
        return True, ""
    if stem in ("uvx", "uv") and "copper-kicad" in args and args[-2:] == ["copper", "serve"]:
        return True, ""
    # the npm launcher (README / server.json / plugin): npx -y copper-kicad[@x.y.z] [serve]
    pkg = [a for a in args if a == "copper-kicad" or a.startswith("copper-kicad@")]
    if stem == "npx" and pkg and args[-1] in ("serve", pkg[-1]):
        return True, ""
    return False, "not a command Copper wrote (edited by hand?); run `copper link` to rewrite it"


# ── the handshake ─────────────────────────────────────────────────────────
def verify_entry(entry: dict, timeout: float = 20.0, call_tool: str | None = None) -> dict:
    """Launch ``entry`` and perform ``initialize`` + ``tools/list`` (and one ``tools/call`` of
    ``call_tool`` when given: the "test call" of the onboarding page).

    Returns ``{'ok', 'stage', 'detail', 'initialize_ms', 'tools', 'server'}`` where ``stage`` names the
    step that failed (``recognise`` | ``spawn`` | ``initialize`` | ``tools/list`` | ``tools/call``) or is ``None``."""
    ok, why = is_copper_entry(entry)
    if not ok:
        return {"ok": False, "stage": "recognise", "detail": why, "initialize_ms": None, "tools": 0}
    cmd, args, extra = _cmd_args_env(entry)
    env = dict(os.environ)
    env.update(extra)
    env.update(COPPER_NO_UPDATE_CHECK="1", PYTHONIOENCODING="utf-8")
    t0 = time.perf_counter()
    try:
        p = subprocess.Popen([cmd, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=env, creationflags=_NO_WINDOW, bufsize=0)
    except OSError as e:
        return {"ok": False, "stage": "spawn", "detail": f"{type(e).__name__}: {e.strerror or e}",
                "initialize_ms": None, "tools": 0}
    q: queue.Queue = queue.Queue()
    err: list[bytes] = []

    def pump():
        for raw in iter(p.stdout.readline, b""):
            q.put(raw)
        q.put(None)

    def drain():
        for raw in iter(p.stderr.readline, b""):
            if len(err) < 40:
                err.append(raw)

    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=drain, daemon=True).start()
    deadline = t0 + timeout

    def send(obj):
        p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        p.stdin.flush()

    def reply(want_id, stage):
        while True:
            left = deadline - time.perf_counter()
            if left <= 0:
                raise _Fail(stage, f"no reply within {timeout:.0f} s")
            try:
                raw = q.get(timeout=left)
            except queue.Empty:
                raise _Fail(stage, f"no reply within {timeout:.0f} s") from None
            if raw is None:
                try:
                    code = p.wait(timeout=2)       # stdout closed a moment before the exit code exists
                except subprocess.TimeoutExpired:
                    code = None
                time.sleep(0.05)                   # let the stderr reader catch the last line
                # the child's last words go to a terminal, the setup page and doctor: scrub them first
                tail = redact.scrub(b"".join(err).decode("utf-8", "replace")).strip().splitlines()[-1:] or [""]
                raise _Fail(stage, ("the server exited" + (f" (code {code})" if code is not None else "")
                                    + " before answering") + (f": {tail[0][:200]}" if tail[0] else ""))
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                raise _Fail(stage, f"printed something that is not MCP on stdout: {redact.scrub(line)[:120]!r}") from None
            if isinstance(msg, dict) and msg.get("id") == want_id:
                if "error" in msg:
                    raise _Fail(stage, f"error reply: {str(msg['error'].get('message', msg['error']))[:200]}")
                return msg.get("result") or {}

    out = {"ok": False, "stage": None, "detail": "", "initialize_ms": None, "tools": 0}
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                         "clientInfo": {"name": "copper-link-check", "version": __version__}}})
        init = reply(1, "initialize")
        out["initialize_ms"] = int((time.perf_counter() - t0) * 1000)
        out["server"] = (init.get("serverInfo") or {}).get("version")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tl = reply(2, "tools/list")
        out["tools"] = len(tl.get("tools") or [])
        out["total_ms"] = int((time.perf_counter() - t0) * 1000)
        if not out["tools"]:
            raise _Fail("tools/list", "the server answered but lists no tools")
        out["detail"] = f"launches in {out['initialize_ms'] / 1000:.1f} s, {out['tools']} tools"
        if call_tool:
            send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": call_tool, "arguments": {}}})
            res = reply(3, "tools/call")
            text = next((c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text"), "")
            out["call"] = {"tool": call_tool, "is_error": bool(res.get("isError")),
                           "ms": int((time.perf_counter() - t0) * 1000) - out["total_ms"],
                           "text": text.strip()[:400]}
            if res.get("isError"):
                raise _Fail("tools/call", f"{call_tool} returned an error: {text.strip()[:200]}")
            out["detail"] += f"; {call_tool} answered in {out['call']['ms'] / 1000:.1f} s"
        out["ok"] = True
    except _Fail as f:
        out.update(stage=f.stage, detail=f.detail)
    except OSError as e:                       # broken pipe: the child died while we wrote
        out.update(stage="initialize", detail=f"the server closed its input ({e.strerror or e})")
    finally:
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for s in (p.stdout, p.stderr):
            try:
                s.close()
            except OSError:
                pass
    return out


class _Fail(Exception):
    def __init__(self, stage, detail):
        super().__init__(detail)
        self.stage, self.detail = stage, detail


def test_call(launcher: str | None = None, tool: str = "copper_status", timeout: float = 60.0) -> dict:
    """Start Copper exactly as a client would and make one real tool call."""
    return verify_entry(clients.server_spec(launcher).entry(), timeout, call_tool=tool)


def _key(entry: dict) -> str:
    return json.dumps(_cmd_args_env(entry), sort_keys=True)


def verify(client_id: str | list[str] | None = None, timeout: float = 20.0) -> list[dict]:
    """Handshake for every registered target (of ``client_id`` when given). Identical launch lines are
    launched once."""
    want = None
    if client_id:
        want = {c.id for c in clients.resolve_names(client_id, detected_only_for_all=False)}
    targets = [e for e in clients.entries() if e.get("entry") and (want is None or e["client"] in want)]
    uniq: dict[str, dict] = {}
    for e in targets:
        uniq.setdefault(_key(e["entry"]), e["entry"])
    results: dict[str, dict] = {}
    if uniq:
        with cf.ThreadPoolExecutor(min(4, len(uniq))) as ex:
            for k, r in zip(uniq, ex.map(lambda en: verify_entry(en, timeout), uniq.values())):
                results[k] = r
    return [{"client": e["client"], "name": e["name"], "path": e["path"], **results[_key(e["entry"])]} for e in targets]


# ── status ────────────────────────────────────────────────────────────────
def plugin_installed() -> str | None:
    """Name of the Copper plugin when Claude Code already loads Copper through it."""
    f = clients.home() / ".claude" / "plugins" / "installed_plugins.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plugins = d.get("plugins", d) if isinstance(d, dict) else {}
    for k in (plugins if isinstance(plugins, dict) else {}):
        if str(k).split("@")[0] == "copper":
            return str(k)
    return None


def status(verify_launch: bool = False, timeout: float = 20.0) -> list[dict]:
    """One row per client.

    ``state``: ``connected`` (registered and, with ``verify_launch``, the handshake passed) ·
    ``broken`` (registered but the entry is wrong or does not start) · ``found`` (client installed, Copper
    not registered) · ``not-found``. Without ``verify_launch`` a registered entry whose command exists is
    reported as ``connected`` with ``checked: False``."""
    by: dict[str, list[dict]] = {}
    for e in clients.entries():
        by.setdefault(e["client"], []).append(e)
    checks: dict[str, dict] = {}
    if verify_launch:
        for r in verify(None, timeout):
            checks[r["path"]] = r
    plug = plugin_installed()
    rows = []
    for c in clients.CLIENTS:
        es = by.get(c.id, [])
        reg = [e for e in es if e["status"] != "absent"]
        detected = any(e["detected"] for e in es)
        row = {"id": c.id, "name": c.name, "detected": detected, "paths": [e["path"] for e in (reg or es)],
               "restart": c.restart_hint, "checked": False, "detail": "", "fix": None}
        if c.id == "claude-code" and plug:
            row["via_plugin"] = plug
        if not reg:
            if row.get("via_plugin"):
                row.update(state="connected", detail=f"connected through the {plug} plugin")
            elif detected:
                row.update(state="found", detail="found, not connected", fix=f"copper link {c.id}")
            else:
                row.update(state="not-found", detail="not found on this computer")
            rows.append(row)
            continue
        bad = [e for e in reg if e["status"] == "fail"]
        if bad:
            row.update(state="broken", detail=bad[0]["detail"], fix=f"copper link {c.id}")
        else:
            row.update(state="connected", detail="registered")
            warn = [e for e in reg if e["status"] == "warn"]
            if warn:
                row["detail"] = warn[0]["detail"]
        if verify_launch and not bad:
            rs = [checks[e["path"]] for e in reg if e["path"] in checks]
            row["checked"] = bool(rs)
            failed = [r for r in rs if not r["ok"]]
            if failed:
                row.update(state="broken", detail=f"does not start ({failed[0]['stage']}): {failed[0]['detail']}",
                           fix=f"copper link {c.id}")
            elif rs:
                row["detail"] = rs[0]["detail"]
                row["tools"] = rs[0]["tools"]
                row["initialize_ms"] = rs[0]["initialize_ms"]
        rows.append(row)
    return rows


# ── connect / disconnect ──────────────────────────────────────────────────
def connect(ids="all", launcher: str | None = None, verify_launch: bool = True, timeout: float = 20.0,
            dry_run: bool = False) -> dict:
    """Register Copper with ``ids`` ('all' = every detected client) in one action, then verify each."""
    chosen = clients.resolve_names(ids)
    plug = plugin_installed()
    skipped = []
    if plug and (ids in ("all", ["all"], None)):
        # Claude Code already loads Copper through the plugin: a second registration lists every tool twice
        skipped = [c.id for c in chosen if c.id == "claude-code"]
        chosen = [c for c in chosen if c.id != "claude-code"]
    res = clients.install([c.id for c in chosen], dry_run=dry_run, launcher=launcher) if chosen else {
        "server": clients.server_spec(launcher).entry(), "clients": [], "dry_run": dry_run, "changes": [],
        "diffs": [], "restart": {}}
    res["skipped"] = [{"client": s, "reason": f"already connected through the {plug} plugin"} for s in skipped]
    res["verify"] = []
    if verify_launch and not dry_run and chosen:
        res["verify"] = verify([c.id for c in chosen], timeout)
    res["ok"] = (not any(ch["action"] == "error" for ch in res["changes"])
                 and all(v["ok"] for v in res["verify"]))
    return res


def disconnect(ids="all", dry_run: bool = False) -> dict:
    return clients.uninstall(ids, dry_run=dry_run)


def generic_snippet(launcher: str | None = None) -> str:
    """The JSON to paste into any other MCP client."""
    return json.dumps({"mcpServers": {SERVER_NAME: clients.server_spec(launcher).entry()}}, indent=2)
