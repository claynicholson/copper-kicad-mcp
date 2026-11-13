"""Shared start-up guard for the pcbnew workers (runs inside KiCad's Python).

Both workers call ``_kiguard.start(pcbnew, MARK)`` right after importing
pcbnew, before any board is loaded:

* **No modal dialogs.** pcbnew reports e.g. a missing font ("Font 'X' not
  found; substituting ...") through wxLog, which pops up a MODAL dialog in a
  headless worker: ``LoadBoard`` then blocks until the server kills the worker
  minutes later. wx logging is switched off, and wx assertion dialogs too.
* **Engine handshake.** ``COPPER_EXPECT_KICAD_SERIES`` is the series of the
  kicad-cli Copper is using. A pcbnew from another KiCad series would save
  the board in a different file format than the one kicad-cli checks, so the
  worker refuses (ENGINE_MISMATCH) before it touches the board. On Linux every
  KiCad shares the system python3, so this cannot be decided by path.

A dialog that still gets through cannot be handled from inside the worker
(pcbnew holds the interpreter lock while it waits), so the server's
subprocess timeout in ``pcb/worker.py`` stays the backstop: it kills the
worker and reports WORKER_TIMEOUT with the board untouched.

Stdlib + pcbnew only; must never raise.
"""
import json
import os
import sys

def quiet():
    try:
        import wx
        wx.Log.EnableLogging(False)
        try:
            wx.DisableAsserts()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def series_of(pcbnew):
    try:
        import re
        m = re.match(r"\s*(\d+)\.(\d+)", str(pcbnew.Version()))
        return "%s.%s" % (m.group(1), m.group(2)) if m else ""
    except Exception:  # noqa: BLE001
        return ""


def mismatch(pcbnew, expect=None):
    """Error text when pcbnew's series differs from the expected one, else None."""
    expect = expect or os.environ.get("COPPER_EXPECT_KICAD_SERIES") or ""
    have = series_of(pcbnew)
    if expect and have and expect != have:
        return "pcbnew is KiCad %s but kicad-cli is KiCad %s" % (have, expect)
    return None


def _emit(mark, payload):
    try:
        sys.stdout.write("\n" + mark + json.dumps(payload) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def start(pcbnew, mark):
    """Call once, right after ``import pcbnew``. Exits the worker (with a
    result line) on an engine mismatch."""
    quiet()
    msg = mismatch(pcbnew)
    if msg:
        _emit(mark, {"ok": False, "code": "ENGINE_MISMATCH", "error": msg + ". The board file was not modified.",
                     "fix": "install the python3/pcbnew package of the same KiCad as kicad-cli, or pin one KiCad "
                            "with COPPER_KICAD_VERSION; `copper doctor` shows both"})
        sys.exit(0)
