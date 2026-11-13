"""The normalised event model: what every model driver and every no-model command is reduced to.

One turn is one ordered stream of ``event`` notifications (agent -> app). docs/internal/HOST_PROTOCOL.md section 6 is
the authority; this module is its executable form and is what tests/fakehost.py checks streams against.

    make(type, **fields) -> dict            one event body (no turn / seq yet; Turn.emit adds them)
    check(event) -> [problem]               field-level validation of one event
    check_stream(events) -> [problem]       ordering rules for one whole turn

Ordering rules (check_stream):
  * ``seq`` starts at 1 and increases by exactly 1;
  * ``tool_progress`` / ``tool_end`` name a ``call`` opened by an earlier ``tool_start`` and not yet ended;
  * at most one ``error``, and the event right after it is ``done`` with ``status: "error"``;
  * exactly one ``done``, and it is the last event; every tool call is ended before it.
"""
from __future__ import annotations

PROTOCOL = 1          # highest protocol version this agent speaks
PROTOCOL_MIN = 1      # lowest it still accepts

TYPES = ("text_delta", "tool_start", "tool_progress", "tool_end", "proposal", "findings", "reveal",
         "usage", "done", "error")
ERROR_KINDS = ("auth", "rate", "network", "engine", "protocol")
DONE_STATUS = ("ok", "cancelled", "error")
TEXT_CHANNELS = ("answer", "reasoning")

# type -> ((required field, python type(s)), ...)
REQUIRED = {
    "text_delta": (("text", str),),
    "tool_start": (("call", str), ("name", str)),
    "tool_progress": (("call", str), ("fraction", (int, float))),
    "tool_end": (("call", str), ("ok", bool)),
    "proposal": (("proposal_id", str), ("document", str), ("state", str), ("counts", dict)),
    "findings": (("document", str), ("items", list)),
    "reveal": (),
    "usage": (("in", int), ("out", int), ("cached", int)),
    "done": (("status", str),),
    "error": (("kind", str), ("message", str)),
}


def make(type_: str, **fields) -> dict:
    """An event body. Fields that are None are left out (absent means "not given" everywhere in the protocol)."""
    if type_ not in TYPES:
        raise ValueError(f"unknown event type {type_!r}")
    ev = {"type": type_}
    ev.update({k: v for k, v in fields.items() if v is not None})
    return ev


def check(ev: dict) -> list[str]:
    """Problems with one event (empty list = valid)."""
    if not isinstance(ev, dict):
        return ["event is not an object"]
    t = ev.get("type")
    if t not in TYPES:
        return [f"unknown event type {t!r}"]
    out = []
    for name, ty in REQUIRED[t]:
        v = ev.get(name)
        if v is None or not isinstance(v, ty) or (ty is int and isinstance(v, bool)):
            out.append(f"{t}.{name} missing or not {getattr(ty, '__name__', 'number')}")
    if t == "error" and ev.get("kind") not in ERROR_KINDS:
        out.append(f"error.kind {ev.get('kind')!r} not one of {ERROR_KINDS}")
    if t == "done" and ev.get("status") not in DONE_STATUS:
        out.append(f"done.status {ev.get('status')!r} not one of {DONE_STATUS}")
    if t == "text_delta" and ev.get("channel", "answer") not in TEXT_CHANNELS:
        out.append(f"text_delta.channel {ev.get('channel')!r} not one of {TEXT_CHANNELS}")
    if t == "tool_progress" and isinstance(ev.get("fraction"), (int, float)) and not 0 <= ev["fraction"] <= 1:
        out.append("tool_progress.fraction outside 0..1")
    if t == "reveal" and not any(k in ev for k in ("refs", "uuids", "net", "box")):
        out.append("reveal names no target (refs / uuids / net / box)")
    return out


def check_stream(events: list[dict]) -> list[str]:
    """Problems with the complete event stream of ONE turn (empty list = conforming)."""
    out, open_calls, seen_calls = [], set(), set()
    done_at = error_at = None
    for i, ev in enumerate(events):
        out += [f"#{i + 1}: {p}" for p in check(ev)]
        if ev.get("seq") != i + 1:
            out.append(f"#{i + 1}: seq is {ev.get('seq')!r}, expected {i + 1}")
        if done_at is not None:
            out.append(f"#{i + 1}: {ev.get('type')} after done")
        t = ev.get("type")
        if error_at is not None and i == error_at + 1 and not (t == "done" and ev.get("status") == "error"):
            out.append(f"#{i + 1}: the event after error must be done(status=error), got {t}")
        if t == "tool_start":
            if ev.get("call") in seen_calls:
                out.append(f"#{i + 1}: call id {ev.get('call')!r} reused")
            open_calls.add(ev.get("call"))
            seen_calls.add(ev.get("call"))
        elif t in ("tool_progress", "tool_end"):
            if ev.get("call") not in open_calls:
                out.append(f"#{i + 1}: {t} for a call that is not open: {ev.get('call')!r}")
            if t == "tool_end":
                open_calls.discard(ev.get("call"))
        elif t == "error":
            if error_at is not None:
                out.append(f"#{i + 1}: second error in one turn")
            error_at = i
        elif t == "done":
            done_at = i
            if open_calls:
                out.append(f"#{i + 1}: done while tool call(s) {sorted(open_calls)} are still open")
            if (ev.get("status") == "error") != (error_at is not None):
                out.append(f"#{i + 1}: done.status={ev.get('status')!r} does not match the presence of an error event")
    if done_at is None:
        out.append("no done event")
    return out
