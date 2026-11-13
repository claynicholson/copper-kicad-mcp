"""The latest focus, shared between processes.

The canvas may run inside the MCP server or as its own process (`copper canvas`),
so the focus lives in a small JSON file under ``<COPPER_HOME>/focus/`` — one per
project, never inside the project folder:

    <key>.json          {'seq': n, 'focus': {...} | None, 'kicad': {'sig', 'ts'}}
    <key>.canvas.json   {'port', 'token', 'pid', 'started'}     (who serves the canvas right now)

    get(pr) -> (focus | None, seq)     put(pr, focus) -> seq      clear(pr) -> seq
    wait(pr, after_seq, timeout) -> (focus | None, seq, changed)
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from .. import project as projmod

_lock = threading.Lock()


def key(pr) -> str:
    return hashlib.sha1(f"{str(pr.dir.resolve()).lower()}|{pr.name}".encode()).hexdigest()[:16]


def _dir() -> Path:
    d = Path(projmod.STATE_DIR) / "focus"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(pr) -> Path:
    return _dir() / f"{key(pr)}.json"


def canvas_path(pr) -> Path:
    return _dir() / f"{key(pr)}.canvas.json"


def _read(p: Path) -> dict:
    for _ in range(3):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            time.sleep(0.02)        # another process is mid-replace
    return {}


def _write(p: Path, d: dict):
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, separators=(",", ":"))
        for attempt in range(6):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.03 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def get(pr) -> tuple[dict | None, int]:
    d = _read(state_path(pr))
    return d.get("focus") or None, int(d.get("seq") or 0)


def put(pr, focus: dict | None) -> int:
    with _lock:
        p = state_path(pr)
        d = _read(p)
        d["seq"] = int(d.get("seq") or 0) + 1
        d["focus"] = focus
        _write(p, d)
        return d["seq"]


def clear(pr) -> int:
    return put(pr, None)


def wait(pr, after_seq: int, timeout: float, poll: float = 0.2) -> tuple[dict | None, int, bool]:
    """Block until the focus changes (seq > after_seq) or `timeout` seconds pass."""
    end = time.time() + max(0.0, timeout)
    while True:
        f, seq = get(pr)
        if seq > after_seq and f:
            return f, seq, True
        if time.time() >= end:
            return f, seq, False
        time.sleep(poll)


def kicad_mark(pr, sig: str) -> float:
    """Remember when a given KiCad selection was first seen, so it can be compared with canvas focus by age."""
    with _lock:
        p = state_path(pr)
        d = _read(p)
        k = d.get("kicad") or {}
        if k.get("sig") != sig:
            k = {"sig": sig, "ts": round(time.time(), 3)}
            d["kicad"] = k
            _write(p, d)
        return float(k["ts"])


# ── canvas registry ───────────────────────────────────────────────────────
def canvas_info(pr) -> dict:
    return _read(canvas_path(pr))


def set_canvas(pr, info: dict | None):
    p = canvas_path(pr)
    if info is None:
        try:
            p.unlink()
        except OSError:
            pass
        return
    _write(p, info)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
