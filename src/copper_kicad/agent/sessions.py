"""Conversations on disk: ``<project>/.copper/conversations/<conversation>.jsonl``.

One JSON object per line, append-only, so a crash loses at most the line being written and a second process
(standalone schematic and board editors each run their own agent) can read a conversation the other is writing.
Without a project the folder is ``$COPPER_HOME/conversations``.

    Store(project_dir=None)
      .new_id() -> str                                   a fresh conversation id
      .append(conversation, record) -> None              adds 'ts'; writes the meta line first if the file is new
      .load(conversation) -> [record]                    [] if unknown; a torn last line is skipped
      .list() -> [{'id','title','created','updated','turns'}]        newest first
      .messages(conversation, limit=None) -> [{'role': 'user'|'assistant', 'text': str}]     for driver history
      .resume_id(conversation, driver) -> str | None     the newest id a driver stored (a CLI session id, ...)
      .set_resume_id(conversation, driver, value)
      .delete(conversation) -> bool

Record kinds (the 'kind' field):
    meta       {'v': 1, 'conversation', 'created', 'title'}                    always line 1
    user       {'turn', 'text', 'focus'?, 'mode', 'account'?, 'model'?, 'command'?: {'name','args'}}
    assistant  {'turn', 'text'}                                                the turn's answer text, coalesced
    tool       {'turn', 'call', 'name', 'args'?, 'ok', 'summary'?, 'error'?}
    proposal   {'turn', 'proposal_id', 'document', 'state', 'summary', 'counts'}
    outcome    {'proposal_id', 'outcome': 'kept'|'undone'|'stale'}
    findings   {'turn', 'document', 'counts'?, 'items'}
    done       {'turn', 'status', 'usage'?, 'error'?: {'kind','code'?,'message'}}
    resume     {'driver', 'id'}

Secrets never reach this file: account records are referenced by id only.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

VERSION = 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_lock = threading.Lock()


def valid_id(conversation) -> bool:
    return isinstance(conversation, str) and bool(_ID.fullmatch(conversation))


class Store:
    def __init__(self, project_dir=None):
        if project_dir:
            self.dir = Path(project_dir) / ".copper" / "conversations"
        else:
            self.dir = Path(os.environ.get("COPPER_HOME") or (Path.home() / ".copper")) / "conversations"

    def new_id(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)

    def path(self, conversation: str) -> Path:
        if not valid_id(conversation):
            raise ValueError(f"bad conversation id {conversation!r}")
        return self.dir / f"{conversation}.jsonl"

    def append(self, conversation: str, record: dict) -> None:
        p = self.path(conversation)
        now = round(time.time(), 3)
        lines = []
        with _lock:
            if not p.exists():
                self.dir.mkdir(parents=True, exist_ok=True)
                title = str(record.get("text") or (record.get("command") or {}).get("name") or "")[:80]
                lines.append({"kind": "meta", "v": VERSION, "conversation": conversation, "created": now, "title": title})
            lines.append(dict(record, ts=now))
            with open(p, "a", encoding="utf-8", newline="\n") as f:
                for rec in lines:
                    f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")

    def load(self, conversation: str) -> list[dict]:
        try:
            raw = self.path(conversation).read_text(encoding="utf-8")
        except (OSError, ValueError):
            return []
        out = []
        for line in raw.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue                                   # a torn line (crash mid-write)
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def list(self) -> list[dict]:
        out = []
        try:
            files = list(self.dir.glob("*.jsonl"))
        except OSError:
            files = []
        for p in files:
            if not valid_id(p.stem):
                continue
            recs = self.load(p.stem)
            meta = next((r for r in recs if r.get("kind") == "meta"), {})
            out.append({"id": p.stem, "title": meta.get("title") or "", "created": meta.get("created"),
                        "updated": max((r.get("ts") or 0 for r in recs), default=meta.get("created") or 0),
                        "turns": sum(1 for r in recs if r.get("kind") == "user")})
        return sorted(out, key=lambda c: c["updated"] or 0, reverse=True)

    def messages(self, conversation: str, limit: int | None = None) -> list[dict]:
        """The conversation as alternating user / assistant texts (tool traffic is not replayed).
        Command turns are left out: they never went through a model."""
        out = []
        for r in self.load(conversation):
            if r.get("kind") == "user" and not r.get("command") and r.get("text"):
                out.append({"role": "user", "text": r["text"], "turn": r.get("turn")})
            elif r.get("kind") == "assistant" and r.get("text") and out and out[-1].get("turn") == r.get("turn"):
                out.append({"role": "assistant", "text": r["text"], "turn": r.get("turn")})
        while out and out[-1]["role"] == "user":           # an unanswered turn (cancelled, failed) is not history
            out.pop()
        out = [{"role": m["role"], "text": m["text"]} for m in out]
        return out[-limit:] if limit else out

    def resume_id(self, conversation: str, driver: str) -> str | None:
        for r in reversed(self.load(conversation)):
            if r.get("kind") == "resume" and r.get("driver") == driver:
                return r.get("id")
        return None

    def set_resume_id(self, conversation: str, driver: str, value: str) -> None:
        self.append(conversation, {"kind": "resume", "driver": driver, "id": value})

    def delete(self, conversation: str) -> bool:
        try:
            self.path(conversation).unlink()
            return True
        except (OSError, ValueError):
            return False
