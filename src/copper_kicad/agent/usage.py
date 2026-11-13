"""Usage accounting: what every turn cost, per turn and per project.

    <project>/.copper/usage.jsonl          one JSON object per finished turn, append-only
    $COPPER_HOME/agent/usage.jsonl         the same for turns without a project

A line:
    {"ts": 1794268800, "turn": "t-5f0c9a1e", "kind": "model" | "command", "status": "ok" | "cancelled" | "error",
     "ms": 4120, "tools": 2, "in": 1200, "out": 80, "cached": 1000,
     "conversation"?: str, "mode"?: str, "account"?: id, "account_kind"?: str, "model"?: str, "cost"?: number,
     "command"?: name}

``in`` / ``out`` / ``cached`` are the turn's final token counts as its driver reported them (``turn.usage``);
a no-model command writes zeros, which is the point: the footer can show what the deterministic jobs saved.
``cost`` is present only when the driver reported one. No text, no arguments and no secrets are written.

    record_turn(project_dir, turn, status=None) -> dict | None
                                                        called once by the server, right before the turn's `done`
                                                        event goes out; never raises
    read(project_dir, limit=None) -> [line]             oldest first; torn or foreign lines are skipped
    summary(project_dir, conversation=None) -> dict     see ``summary``
    line(summary) -> str                                one line for the pane footer / the /usage command
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_TOKENS = ("in", "out", "cached")


def path(project_dir=None) -> Path:
    if project_dir:
        return Path(project_dir) / ".copper" / "usage.jsonl"
    return Path(os.environ.get("COPPER_HOME") or (Path.home() / ".copper")) / "agent" / "usage.jsonl"


def turn_record(turn, status: str | None = None) -> dict:
    req = turn.request
    u = getattr(turn, "_usage", None) or {}
    rec = {"ts": round(time.time()), "turn": turn.id, "kind": turn.kind, "status": status or turn.status or "ok",
           "ms": int((time.monotonic() - turn.started) * 1000), "tools": int(getattr(turn, "_calls", 0)),
           "in": int(u.get("in", 0)), "out": int(u.get("out", 0)), "cached": int(u.get("cached", 0))}
    if req.conversation:
        rec["conversation"] = req.conversation
    acct = req.account or {}
    if turn.kind == "command":
        rec["command"] = (turn.command or {}).get("name")
    elif acct.get("kind") in (None, "none"):            # typed on the no-account pane: no model was involved
        rec["kind"] = "command"
        text = (req.text or "").strip()
        if text.startswith("/"):
            rec["command"] = text[1:].split(None, 1)[0][:40] if len(text) > 1 else None
    else:
        rec.update({"mode": req.mode, "account": acct.get("id"), "account_kind": acct.get("kind")})
        model = u.get("model") or req.model
        if model:
            rec["model"] = model
    if isinstance(u.get("cost"), (int, float)) and not isinstance(u.get("cost"), bool):
        rec["cost"] = u["cost"]
    return {k: v for k, v in rec.items() if v is not None}


def record(project_dir, rec: dict) -> None:
    p = path(project_dir)
    data = (json.dumps(rec, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "ab") as f:                 # one write per line: two agents (schematic + board editor) may append
            f.write(data)


def record_turn(project_dir, turn, status: str | None = None) -> dict | None:
    try:
        rec = turn_record(turn, status)
        record(project_dir, rec)
        return rec
    except Exception:  # noqa: BLE001 — the meter must never cost a turn
        return None


def read(project_dir=None, limit: int | None = None) -> list[dict]:
    try:
        raw = path(project_dir).read_bytes()
    except OSError:
        return []
    out = []
    for ln in raw.decode("utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("turn"), str) and rec.get("kind") in ("model", "command"):
            out.append(rec)
    return out[-limit:] if limit else out


def _zero() -> dict:
    return {"turns": 0, "in": 0, "out": 0, "cached": 0, "cost": 0.0, "tools": 0}


def _add(tot: dict, rec: dict) -> None:
    tot["turns"] += 1
    for k in _TOKENS:
        v = rec.get(k)
        tot[k] += v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0
    c = rec.get("cost")
    if isinstance(c, (int, float)) and not isinstance(c, bool):
        tot["cost"] = round(tot["cost"] + c, 6)
    t = rec.get("tools")
    tot["tools"] += t if isinstance(t, int) and not isinstance(t, bool) else 0


def summary(project_dir=None, conversation: str | None = None, records: list[dict] | None = None) -> dict:
    """Totals of a project's usage file.

    {'file': str,
     'project': {'turns','in','out','cached','cost','tools','model_turns','command_turns','cost_complete': bool},
     'today': {...same counters, model turns of the local day},
     'by_account': {account id: {...}}, 'by_model': {model id: {...}},
     'by_day': {'YYYY-MM-DD': {...}}                     the 14 most recent days with model turns
     'conversation': {...} | absent                      when ``conversation`` is given
     'last': line | None}                                the most recent model turn

    Counters cover MODEL turns (commands cost nothing and are only counted in ``command_turns``).
    ``cost_complete`` is False when at least one model turn reported tokens but no cost."""
    recs = read(project_dir) if records is None else records
    proj, today, conv = _zero(), _zero(), _zero()
    by_account, by_model, by_day = {}, {}, {}
    commands, complete, last = 0, True, None
    day_now = time.strftime("%Y-%m-%d")
    for r in recs:
        if r["kind"] != "model":
            commands += 1
            continue
        last = r
        _add(proj, r)
        if "cost" not in r and (r.get("in") or r.get("out")):
            complete = False
        day = time.strftime("%Y-%m-%d", time.localtime(r["ts"])) if isinstance(r.get("ts"), (int, float)) else "?"
        _add(by_day.setdefault(day, _zero()), r)
        if day == day_now:
            _add(today, r)
        _add(by_account.setdefault(str(r.get("account") or "?"), _zero()), r)
        if r.get("model"):
            _add(by_model.setdefault(str(r["model"]), _zero()), r)
        if conversation and r.get("conversation") == conversation:
            _add(conv, r)
    out = {"file": str(path(project_dir)),
           "project": dict(proj, model_turns=proj["turns"], command_turns=commands, cost_complete=complete,
                           turns=proj["turns"] + commands),
           "today": today, "by_account": by_account, "by_model": by_model,
           "by_day": dict(sorted(by_day.items())[-14:]), "last": last}
    if conversation:
        out["conversation"] = conv
    return out


def _n(v: int) -> str:
    return f"{v / 1e6:.2f}M" if v >= 1e6 else f"{v / 1e3:.1f}k" if v >= 1e4 else str(v)


def _tokens(t: dict, complete: bool = True) -> str:
    s = f"{_n(t['in'])} in / {_n(t['out'])} out"
    if t["cached"]:
        s += f" ({_n(t['cached'])} cached)"
    if t["cost"]:
        s += f", {'' if complete else 'at least '}{t['cost']:.4f}".rstrip("0").rstrip(".") + " cost"
    return s


def line(s: dict) -> str:
    """'Project: 12 model turns, 48.1k in / 3.2k out (40.0k cached); 30 commands (no tokens). Today: ...'"""
    p = s["project"]
    if not p["model_turns"]:
        return (f"No model usage in this project yet; {p['command_turns']} command(s) run without a model."
                if p["command_turns"] else "No usage recorded yet.")
    out = f"Project: {p['model_turns']} model turn(s), {_tokens(p, p['cost_complete'])}"
    if p["command_turns"]:
        out += f"; {p['command_turns']} command(s) without a model"
    if s["today"]["turns"]:
        out += f". Today: {s['today']['turns']} turn(s), {_tokens(s['today'], p['cost_complete'])}"
    return out + "."
