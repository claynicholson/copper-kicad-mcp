"""Run pcbnew worker scripts with KiCad's own Python interpreter.

The server process never imports ``pcbnew``. Each board operation is a short
subprocess: ``<kicad python> -I workers/board.py`` with a JSON request on
stdin. The worker prints exactly one result line prefixed with ``MARK`` on
stdout (pcbnew itself may print noise), so parsing stays robust.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ..kicad import compat, env
from ..mcp.registry import ToolError

WORKERS = Path(__file__).with_name("workers")
MARK = "@@COPPER_RESULT@@"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def python() -> Path:
    """The active KiCad's own interpreter (never another install's: the board
    must be edited by the KiCad whose kicad-cli checks it)."""
    py = env.kicad_python()
    if not py:
        try:
            inst = env.kicad()
        except ToolError:
            inst = None
        if inst is not None and inst.ver is not None and inst.ver.effective_major >= 11:
            raise ToolError(f"KiCad {inst.ver} has no pcbnew Python (removed in KiCad 11).",
                            code="PCB_ENGINE_UNAVAILABLE",
                            fix="install KiCad 10.0.x alongside; Copper uses it automatically for 10.0-format projects")
        raise ToolError("KiCad's Python (with pcbnew) was not found — PCB editing needs KiCad 10's.",
                        code="NO_KICAD_PYTHON",
                        fix="install KiCad 10 (the Windows/macOS installers include it; on Linux install the "
                            "python3-kicad / kicad package) and run `copper doctor`")
    return py


def run(script: str, request: dict, timeout: int = 300) -> dict:
    """Run ``workers/<script>.py`` with ``request`` as JSON on stdin.

    Returns the worker's result dict. Worker-side failures come back as
    ToolError with the worker's message (and a short trace in ``data``)."""
    py = python()
    path = WORKERS / f"{script}.py"
    environ = dict(os.environ)
    # KICAD_CONFIG_HOME is deliberately NOT set: KiCad appends "<major.minor>" to it itself, so exporting
    # env.config_dir() made every worker run on (and create) an empty <config>/10.0/10.0.
    environ["PYTHONIOENCODING"] = "utf-8"
    try:
        inst = env.kicad()
    except ToolError:
        inst = None
    if inst is not None:
        if request.get("pcb") and os.path.isfile(request["pcb"]):
            compat.check_readable(request["pcb"], inst)      # pcbnew.LoadBoard just returns None for a too-new board
        if inst.ver is not None:
            # handshake: workers/_kiguard.py refuses (ENGINE_MISMATCH) before LoadBoard when the interpreter's
            # pcbnew belongs to another KiCad series (on Linux every install shares the system python3)
            request = dict(request, expect_series=inst.ver.series)
            environ["COPPER_EXPECT_KICAD_SERIES"] = inst.ver.series
    # path vars so footprint 3D paths etc. resolve the same way KiCad does
    try:
        for k, v in env.path_vars(Path(request["pcb"]).parent if request.get("pcb") else None).items():
            environ.setdefault(k, v)
    except Exception:  # noqa: BLE001
        pass
    try:
        r = subprocess.run([str(py), "-X", "utf8", str(path)], input=json.dumps(request), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout, env=environ,
                           cwd=str(WORKERS), creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise ToolError(f"pcbnew worker '{script}' timed out after {timeout}s", code="WORKER_TIMEOUT",
                        fix="retry with a smaller batch; the board file was not modified")
    payload = None
    for line in reversed((r.stdout or "").splitlines()):
        if line.startswith(MARK):
            try:
                payload = json.loads(line[len(MARK):])
            except ValueError:
                pass
            break
    if payload is None:
        tail = ((r.stderr or "") + "\n" + (r.stdout or "")).strip()[-1500:]
        raise ToolError(f"pcbnew worker '{script}' crashed (exit {r.returncode}). The board file was not modified. "
                        f"Output: {tail}", code="WORKER_CRASHED",
                        fix="run `copper doctor`; if the board is corrupt restore it with checkpoint(action='restore')")
    if not payload.get("ok"):
        raise ToolError(payload.get("error", "worker failed"), code=payload.get("code", "WORKER_ERROR"),
                        fix=payload.get("fix"), data={"trace": payload.get("trace", [])[-4:]} if payload.get("trace") else None)
    return payload.get("result", {})
