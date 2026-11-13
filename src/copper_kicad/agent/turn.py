"""One turn: the object a model driver or a command drives.

    turn.request            TurnRequest (conversation, text, focus, mode, account, model, budget_tokens)
    turn.focus              the focus object given with the turn (or None)
    turn.project_dir        the open project's folder (or None)
    turn.host               HostClient bound to this turn (checkout / apply / reveal / ...)
    turn.sessions           sessions.Store of the project
    turn.cancelled          threading.Event, set by turn.cancel and by the watchdog

    turn.check_cancel()                          raises Cancelled once the turn is cancelled or already ended
    turn.touch()                                 "still alive" for the watchdog, without an event
    turn.text(s, channel=None)                   text_delta   (channel 'reasoning' for thinking text)
    turn.call_tool(name, args, summary=None)     run an engine tool: events + live routing -> outcome dict
    turn.tool_start(name, args=None, title=None) -> call id       for work that is not an engine tool
    turn.tool_progress(call, fraction, message=None)
    turn.tool_end(call, ok=True, summary=None, error=None)
    turn.proposal(p) / turn.findings(document, items, source=None, counts=None) / turn.reveal(**target)
    turn.usage(in_tokens, out_tokens, cached=0, cost=None, model=None)      cumulative for the turn
    turn.history(limit=None) -> [{'role','text'}]       earlier turns of this conversation
    turn.notes() -> [str]                                volatile facts for the user turn (review outcomes, ...)

Every emitting method first calls ``check_cancel``: a driver that keeps emitting stops by itself when the user
presses Stop. Events after the turn ended are dropped. ``seq`` is assigned here, under a lock, so events of one
turn are totally ordered on the wire.
"""
from __future__ import annotations

import json
import threading
import time

from . import events
from .drivers.base import Cancelled, TurnRequest

_ARGS_MAX = 4000        # bytes of JSON; larger tool arguments are summarised in tool_start


class Turn:
    def __init__(self, server, tid: str, request: TurnRequest, kind: str = "model", command: dict | None = None,
                 record: bool = True):
        self.server = server
        self.id = tid
        self.request = request
        self.kind = kind                       # model | command
        self.command = command
        self.record = record and bool(request.conversation)
        self.focus = request.focus
        self.project_dir = server.project_dir
        self.sessions = server.sessions
        self.host = server.host.for_turn(tid)
        self.cancelled = threading.Event()
        self.cancel_at: float | None = None
        self.abort_error: dict | None = None      # set by the server when the channel closes under the turn
        self.finished = False
        self.status: str | None = None
        self.started = self.last_activity = time.monotonic()
        self.in_tool = 0
        self.waiting_host = 0
        self.thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._seq = 0
        self._calls = 0
        self._open: dict = {}
        self._text: list[str] = []
        self._usage: dict | None = None

    # ── liveness ──────────────────────────────────────────────────────────
    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def check_cancel(self) -> None:
        if self.cancelled.is_set() or self.finished:
            raise Cancelled()

    # ── the wire ──────────────────────────────────────────────────────────
    def _emit(self, type_: str, **fields) -> bool:
        ev = events.make(type_, **fields)
        with self._lock:
            if self.finished:
                return False
            self._seq += 1
            self.last_activity = time.monotonic()
            self.server.notify("event", dict({"turn": self.id, "seq": self._seq}, **ev))
        return True

    def text(self, s: str, channel: str | None = None) -> None:
        self.check_cancel()
        if not s:
            return
        if channel in (None, "answer"):
            self._text.append(s)
        self._emit("text_delta", text=s, channel=None if channel == "answer" else channel)

    def tool_start(self, name: str, args: dict | None = None, title: str | None = None) -> str:
        self.check_cancel()
        with self._lock:
            self._calls += 1
            call = f"c{self._calls}"
            self._open[call] = {"name": name, "args": args, "t0": time.monotonic()}
        blob = json.dumps(args or {}, ensure_ascii=False, default=str)
        if len(blob.encode("utf-8")) <= _ARGS_MAX:
            self._emit("tool_start", call=call, name=name, title=title, args=json.loads(blob) if args else None)
        else:
            self._emit("tool_start", call=call, name=name, title=title, args_bytes=len(blob.encode("utf-8")),
                       args_keys=sorted(args or {}))
        return call

    def tool_progress(self, call: str, fraction: float, message: str | None = None) -> None:
        if self.finished or call not in self._open:
            return
        self._emit("tool_progress", call=call, fraction=round(max(0.0, min(1.0, float(fraction))), 3),
                   message=message or None)

    def tool_end(self, call: str, ok: bool = True, summary: str | None = None, error: dict | None = None) -> None:
        with self._lock:
            info = self._open.pop(call, None)
            if info is None:
                return
            ms = int((time.monotonic() - info["t0"]) * 1000)
            self._emit("tool_end", call=call, ok=bool(ok), summary=(summary or None) and summary[:400],
                       error=None if ok else (error or {"code": "ERROR", "message": "failed"}), ms=ms)
        self._record({"kind": "tool", "call": call, "name": info["name"], "ok": bool(ok),
                      "summary": (summary or "")[:400] or None, "error": None if ok else error,
                      "args": info["args"] if len(json.dumps(info["args"] or {}, default=str)) <= _ARGS_MAX else None})

    def proposal(self, p: dict) -> None:
        keep = ("proposal_id", "document", "state", "summary", "counts", "erc_new", "netlist_ok", "refs", "region",
                "rev", "skipped", "checkpoint")
        body = {k: p[k] for k in keep if p.get(k) is not None}
        body.setdefault("counts", {"added": 0, "changed": 0, "removed": 0})
        body.setdefault("state", "review")
        self._emit("proposal", **body)
        self.server.note_proposal(body.get("proposal_id"), self.request.conversation)
        self._record({"kind": "proposal", **{k: body.get(k) for k in ("proposal_id", "document", "state", "summary", "counts")}})

    def findings(self, document: str, items: list, source: str | None = None, counts: dict | None = None) -> None:
        self.check_cancel()
        self._emit("findings", document=document, items=items, source=source, counts=counts)
        self._record({"kind": "findings", "document": document, "counts": counts, "items": items[:50]})

    def reveal(self, **target) -> None:
        self.check_cancel()
        self._emit("reveal", **{k: v for k, v in target.items() if k in ("refs", "uuids", "net", "box", "document")})

    def usage(self, in_tokens: int, out_tokens: int, cached: int = 0, cost: float | None = None,
              model: str | None = None) -> None:
        self._usage = {"in": int(in_tokens), "out": int(out_tokens), "cached": int(cached)}
        if cost is not None:
            self._usage["cost"] = cost
        if model:
            self._usage["model"] = model
        self._emit("usage", **self._usage)

    # ── engine tools ──────────────────────────────────────────────────────
    def call_tool(self, name: str, args: dict | None = None, summary: str | None = None,
                  title: str | None = None) -> dict:
        """Run one engine tool the way the app needs it run (host/live.py) and report it as tool events.
        -> outcome {'ok','text','data','images','error','proposal','route'}; never raises for a tool failure
        (the model reads the failure), raises Cancelled when the turn was cancelled.

        A board tool that writes is a FILE JOB (agent/jobs.board_job): permission, save, checkpoint, the tool in a
        child process, reload. Stop kills it and puts the board files back, so a model-driven route or pour can be
        stopped like the /route command, and a stopped one never leaves the file different from the editor."""
        from ..host.live import route_of
        from ..mcp import registry
        from .. import tools as _tools  # noqa: F401  (registers the engine tools)
        if route_of(registry.TOOLS.get(name)) == "board":
            from . import jobs
            out = jobs.board_job(self, name, args, title)
            if self.cancelled.is_set() or self.finished:
                raise Cancelled()
            return out
        call = self.tool_start(name, args, title)

        def progress(fraction: float, message: str = ""):
            self.tool_progress(call, fraction, message)
        progress.has_token = True
        progress.cancelled = self.cancelled.is_set
        self.in_tool += 1
        try:
            out = self.server.live.call_tool(name, args, progress=progress, summary=summary, host=self.host)
        except Cancelled:
            self.tool_end(call, False, error={"code": "CANCELLED", "message": "cancelled"})
            raise
        except Exception as e:  # noqa: BLE001
            self.tool_end(call, False, error={"code": "INTERNAL", "message": f"{type(e).__name__}: {e}"})
            raise
        finally:
            self.in_tool -= 1
            self.touch()
        if self.cancelled.is_set() or self.finished:
            self.tool_end(call, False, error={"code": "CANCELLED", "message": "cancelled"})
            raise Cancelled()
        self.tool_end(call, out["ok"], summary=(out["text"].splitlines() or [""])[0], error=out["error"])
        if out.get("proposal"):
            self.proposal(out["proposal"])
        return out

    # ── conversation ──────────────────────────────────────────────────────
    def history(self, limit: int | None = None) -> list[dict]:
        if not self.request.conversation or self.request.mode == "quick":
            return []
        return self.sessions.messages(self.request.conversation, limit)

    def notes(self) -> list[str]:
        return self.server.take_notes(self.request.conversation)

    def _record(self, rec: dict) -> None:
        if not self.record:
            return
        try:
            self.sessions.append(self.request.conversation, dict({k: v for k, v in rec.items() if v is not None}, turn=self.id))
        except Exception as e:  # noqa: BLE001 — a full disk must not end a turn
            self.server.log("warning", f"session write failed: {type(e).__name__}: {e}")

    # ── the end (server only) ─────────────────────────────────────────────
    def finish(self, status: str, error: dict | None = None) -> bool:
        """Emit the terminal events exactly once. Returns False if the turn had already ended."""
        with self._lock:
            if self.finished:
                return False
            for call in list(self._open):
                self.tool_end(call, False, error={"code": "CANCELLED" if status == "cancelled" else "ABORTED",
                                                  "message": "the turn ended before the tool finished"})
            if error is not None:
                status = "error"
                self._emit("error", **error)
            self._emit("done", status=status, usage=self._usage)
            self.finished = True
            self.status = status
            self.cancelled.set()
        text = "".join(self._text)
        if text:
            self._record({"kind": "assistant", "text": text})
        self._record({"kind": "done", "status": status, "usage": self._usage,
                      "error": {k: error[k] for k in ("kind", "code", "message") if k in error} if error else None})
        return True
