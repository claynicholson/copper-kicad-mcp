"""Engine tools for tests/test_agent_conversations.py, loaded through COPPER_AGENT_PLUGINS (never shipped).

They are named ``sch_zz_*`` so host/live.py routes them like every schematic tool (shadow checkout -> patch ->
host.apply), and they misbehave in the two ways a real tool can:

    sch_zz_write_then_fail   edits the sheet in the shadow, THEN fails (a crash after the save: layout, render, ...)
    sch_zz_slow              cannot be interrupted for `seconds`, then edits the sheet (a long engine job)
    sch_zz_exit              calls sys.exit() (a library that gives up that way must not take the turn with it)
"""
from __future__ import annotations

import time

from copper_kicad import project as P
from copper_kicad.mcp.registry import Result, ToolError, tool
from copper_kicad.sch import ops as O

_PROJECT = {"type": "string"}


@tool("sch_zz_write_then_fail", "test: write a text item, then fail", {"project": _PROJECT, "text": {"type": "string"}},
      [], title="test", destructive=True, group="schematic")
def sch_zz_write_then_fail(project=None, text="LEAKED-BY-A-FAILED-TOOL"):
    pr = P.resolve(project, must_exist=False)
    O.apply_ops(pr, [{"op": "text", "text": text, "at": [25.4, 25.4]}])
    raise ToolError("the tool broke after it had written the sheet", code="BROKE_AFTER_WRITE")


@tool("sch_zz_slow", "test: block, then set a value", {"project": _PROJECT, "seconds": {"type": "number"},
                                                       "ref": {"type": "string"}, "value": {"type": "string"}},
      [], title="test", destructive=True, group="schematic")
def sch_zz_slow(project=None, seconds=3.0, ref="R3", value="SLOW"):
    pr = P.resolve(project, must_exist=False)
    time.sleep(float(seconds))
    r = O.apply_ops(pr, [{"op": "set_field", "ref": ref, "field": "Value", "value": value}])
    return Result({"changed": r["changed"]}, summary="slow edit written")


@tool("sch_zz_exit", "test: a tool that calls sys.exit()", {"project": _PROJECT}, [], title="test", group="schematic")
def sch_zz_exit(project=None):
    raise SystemExit(3)
