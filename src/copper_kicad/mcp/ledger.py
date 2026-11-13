"""Per-session credit meter: what Copper's own traffic costs the agent that drives it.

One :class:`Ledger` lives for the life of the server process. ``Server._call`` records every tool call
AFTER the result was shaped for the wire, so the byte counts are what the client actually receives:
calls, errors, seconds, argument bytes, result text bytes and image pixels per tool. Token figures are
ESTIMATES from two neutral yardsticks (text bytes / 4; image pixels / 750 after scaling the long edge
to 1568 px); bytes and pixels are the facts.

What the meter cannot see: the agent's own prompt and reasoning, and the fact that every later turn
re-reads the whole conversation. Its total is a lower bound on what a session costs, never the bill.

Privacy: counters only (tool names, byte, pixel and second counts, the client's name). No argument
values, no result text, no paths. ``COPPER_LEDGER=0`` switches the session file off; the in-memory
counters behind ``copper_status`` stay.

    python -m copper_kicad.mcp.ledger [--last N]      # print past sessions without spending tokens
"""
from __future__ import annotations

import json
import os
import struct
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

TEXT_BYTES_PER_TOKEN = 4
IMAGE_PX_PER_TOKEN = 750
IMAGE_LONG_EDGE = 1568
FLUSH_EVERY = 10
KEEP_SESSIONS = 50
HEADER = ("Copper tool traffic only (estimated tokens). Your AI bill also includes the assistant's own prompt, "
          "its reasoning, and re-reading the conversation on every turn.")


def png_size(raw: bytes) -> tuple[int, int]:
    """(width, height) from a PNG's IHDR; (0, 0) for anything else. No imaging library needed."""
    if len(raw) >= 24 and raw[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", raw[16:24])
    return (0, 0)


def b64_png_size(data: str) -> tuple[int, int]:
    """Same, from base64 text: only the first 32 characters (24 bytes) are decoded."""
    import base64
    try:
        return png_size(base64.b64decode(data[:32]))
    except (ValueError, TypeError):
        return (0, 0)


def image_tokens(w: int, h: int) -> int:
    if not w or not h:
        return 0
    s = min(1.0, IMAGE_LONG_EDGE / max(w, h))
    return int(w * s * h * s / IMAGE_PX_PER_TOKEN)


def text_tokens(n_bytes: int) -> int:
    return n_bytes // TEXT_BYTES_PER_TOKEN


@dataclass
class ToolStats:
    calls: int = 0
    errors: int = 0
    seconds: float = 0.0
    arg_bytes: int = 0
    text_bytes: int = 0
    images: int = 0
    image_pixels: int = 0
    image_tokens: int = 0

    def est_tokens(self) -> int:
        return text_tokens(self.arg_bytes) + text_tokens(self.text_bytes) + self.image_tokens


@dataclass
class Ledger:
    started: float = field(default_factory=time.time)
    client: str = ""
    catalogue_bytes: int = 0
    instructions_bytes: int = 0
    tools: dict = field(default_factory=dict)

    def __post_init__(self):
        self._lock = threading.Lock()
        self._since_flush = 0
        self._file: Path | None = None

    # ── recording ─────────────────────────────────────────────────────────
    def set_client(self, info: dict | None):
        info = info or {}
        name, ver = str(info.get("name") or "")[:40], str(info.get("version") or "")[:20]
        self.client = (name + (" " + ver if ver else "")).strip()

    def record(self, name: str, args, mcp_result: dict, seconds: float) -> None:
        """Count one finished call. ``mcp_result`` is the dict that goes on the wire."""
        try:
            arg_bytes = len(json.dumps(args if args is not None else {}, separators=(",", ":"),
                                       ensure_ascii=False, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            arg_bytes = 0
        text = images = pixels = itok = 0
        for block in (mcp_result or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text += len(str(block.get("text", "")).encode("utf-8"))
            elif block.get("type") == "image":
                w, h = b64_png_size(block.get("data") or "")
                images += 1
                pixels += w * h
                itok += image_tokens(w, h)
        flush = False
        with self._lock:
            st = self.tools.get(name)
            if st is None:
                st = self.tools[name] = ToolStats()
            st.calls += 1
            st.errors += 1 if (mcp_result or {}).get("isError") else 0
            st.seconds += float(seconds or 0)
            st.arg_bytes += arg_bytes
            st.text_bytes += text
            st.images += images
            st.image_pixels += pixels
            st.image_tokens += itok
            self._since_flush += 1
            if self._since_flush >= FLUSH_EVERY:
                self._since_flush = 0
                flush = True
        if flush:
            self.flush()

    # ── views ─────────────────────────────────────────────────────────────
    def _snapshot(self) -> dict:
        with self._lock:
            return {k: ToolStats(**asdict(v)) for k, v in self.tools.items()}

    def totals(self) -> ToolStats:
        t = ToolStats()
        for s in self._snapshot().values():
            for k, v in asdict(s).items():
                setattr(t, k, getattr(t, k) + v)
        return t

    def est_tokens(self) -> dict:
        t = self.totals()
        d = {"catalogue": text_tokens(self.catalogue_bytes + self.instructions_bytes),
             "args": text_tokens(t.arg_bytes), "results": text_tokens(t.text_bytes), "images": t.image_tokens}
        d["total"] = sum(d.values())
        return d

    def digest(self) -> dict:
        """Small summary for copper_status (about 250 bytes)."""
        t = self.totals()
        snap = self._snapshot()
        top = sorted(snap.items(), key=lambda kv: -kv[1].est_tokens())[:3]
        est = self.est_tokens()
        return {"calls": t.calls, "errors": t.errors, "result_kb": round(t.text_bytes / 1000, 1), "images": t.images,
                "est_tokens": est,
                "reread_per_turn": est["catalogue"] + est["results"] + est["images"],
                "top": [f"{k} {v.est_tokens()}" for k, v in top],
                "note": "estimated tokens of Copper traffic only; copper_status(detail='credits') lists every tool"}

    def table(self) -> dict:
        """Per-tool table, most expensive first."""
        snap = self._snapshot()
        rows = [[k, s.calls, s.errors, round(s.seconds, 1), s.arg_bytes, s.text_bytes, s.images, s.image_tokens, s.est_tokens()]
                for k, s in sorted(snap.items(), key=lambda kv: -kv[1].est_tokens())]
        return {"cols": ["tool", "calls", "errors", "seconds", "arg_bytes", "result_bytes", "images", "image_tokens",
                         "est_tokens"],
                "rows": rows, "est_tokens": self.est_tokens(),
                "catalogue_bytes": self.catalogue_bytes, "instructions_bytes": self.instructions_bytes,
                "how_estimated": "text bytes / 4; image pixels / 750 after scaling the long edge to 1568 px",
                "not_counted": "the assistant's own prompt and reasoning, and re-reading the conversation on every "
                               "turn: this is a lower bound, not the bill"}

    # ── session file ──────────────────────────────────────────────────────
    def to_json(self) -> dict:
        snap = self._snapshot()
        return {"started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started)),
                "seconds": round(time.time() - self.started, 1), "client": self.client,
                "catalogue_bytes": self.catalogue_bytes, "instructions_bytes": self.instructions_bytes,
                "est_tokens": self.est_tokens(),
                "tools": {k: {kk: (round(vv, 1) if isinstance(vv, float) else vv) for kk, vv in asdict(v).items()}
                          for k, v in snap.items()}}

    def flush(self) -> None:
        """Rewrite this session's file (temp file + replace). Never raises."""
        if os.environ.get("COPPER_LEDGER", "1").strip().lower() in ("0", "false", "off", "no"):
            return
        if not self.tools:
            return
        try:
            d = sessions_dir()
            d.mkdir(parents=True, exist_ok=True)
            if self._file is None:
                stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started))
                self._file = d / f"{stamp}-{os.getpid()}.json"
                prune(d, keep=KEEP_SESSIONS - 1)
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.to_json(), separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self._file)
        except OSError:
            pass


# The row for calls to a name that is not a registered tool. Only registered names become rows: a made-up
# name is client text (any length, any content) and the meter stores counters, never client text.
UNKNOWN = "(unknown tool)"


def sessions_dir() -> Path:
    home = os.environ.get("COPPER_HOME")
    return (Path(home) if home else Path.home() / ".copper") / "sessions"


def prune(d: Path, keep: int = KEEP_SESSIONS) -> None:
    try:
        files = sorted(d.glob("*.json"))
        for f in files[:max(0, len(files) - keep)]:
            f.unlink()
    except OSError:
        pass


CURRENT = Ledger()


def current() -> Ledger:
    return CURRENT


def reset() -> Ledger:
    """Start a fresh ledger (a new server, or a test)."""
    global CURRENT
    CURRENT = Ledger()
    return CURRENT


# ── `python -m copper_kicad.mcp.ledger` / `copper credits` ─────────────────────
def report(last: int = 5) -> str:
    d = sessions_dir()
    files = sorted(d.glob("*.json"))[-max(1, last):] if d.is_dir() else []
    lines = [HEADER, ""]
    if not files:
        return "\n".join(lines + [f"no recorded sessions in {d}"])
    for f in files:
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        est = s.get("est_tokens") or {}
        tools = s.get("tools") or {}
        calls = sum(t.get("calls", 0) for t in tools.values())
        lines.append(f"{s.get('started', f.stem)}  {s.get('client') or 'unknown client'}: {calls} calls, "
                     f"~{est.get('total', 0)} tokens (catalogue {est.get('catalogue', 0)}, arguments {est.get('args', 0)}, "
                     f"results {est.get('results', 0)}, images {est.get('images', 0)})")
        rows = sorted(tools.items(), key=lambda kv: -(kv[1].get("text_bytes", 0) // 4 + kv[1].get("arg_bytes", 0) // 4
                                                      + kv[1].get("image_tokens", 0)))
        for name, t in rows[:12]:
            tok = t.get("text_bytes", 0) // 4 + t.get("arg_bytes", 0) // 4 + t.get("image_tokens", 0)
            lines.append(f"    {name:20s} {t.get('calls', 0):3d} calls  {t.get('errors', 0):2d} errors  "
                         f"{t.get('text_bytes', 0):7d} B text  {t.get('images', 0):2d} images  ~{tok} tokens")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="copper credits", description="Estimated cost of Copper's tool traffic per session.")
    ap.add_argument("--last", type=int, default=5, help="how many recent sessions to print (default 5)")
    a = ap.parse_args(argv)
    print(report(a.last))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
