"""Server-side PCB API: runs pcbnew worker ops safely, reads boards, runs
DRC through kicad-cli and pulls the schematic netlist.

Public functions other modules call::

    run_ops(pr, ops, ...)        -> worker result (guarded + checkpointed write)
    read(pr, what=..., refs=...) -> board snapshot dict (no write)
    drc(pr, parity=True, refill=True) -> {'errors','warnings','unconnected','parity',...}
    netlist(pr)                  -> {'components': [...], 'nets': [...]}
    fp_libs(pr)                  -> {nickname: path} from global + project fp-lib-tables
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from .. import project as projmod
from .. import sexpr
from ..kicad import env
from ..mcp.registry import ToolError
from . import worker


def board_path(pr) -> Path:
    return pr.pcb


def fp_libs(pr) -> dict:
    out = {}
    for lib in env.lib_tables("fp", pr.dir):
        if lib.exists:
            out[lib.name] = lib.path
    return out


def run_ops(pr, ops: list[dict], *, save: bool = True, create: bool = False, while_open: str = "refuse",
            label: str = "pcb", timeout: int = 300, refill: bool = True) -> dict:
    """Run worker ops on the project's board. Writes are guarded (board open in
    KiCad → refuse) and checkpointed first."""
    pcb = board_path(pr)
    if save:
        if pcb.exists():
            projmod.guard_writable(pcb, while_open)
            projmod.checkpoint(pr, label)
        elif not create:
            raise ToolError(f"{pcb.name} does not exist yet", code="NO_BOARD",
                            fix="pcb_sync() to create the board from the schematic")
    before_rev = projmod.revision(pcb)
    req = {"pcb": str(pcb), "ops": ops, "save": save, "create": create, "refill": refill}
    res = worker.run("board", req, timeout=timeout)
    res["revision"] = projmod.revision(pcb)
    res["revision_before"] = before_rev
    return res


def read(pr, what=("summary", "footprints", "nets"), refs=None, timeout: int = 120) -> dict:
    pcb = board_path(pr)
    if not pcb.exists():
        raise ToolError(f"{pcb.name} does not exist yet", code="NO_BOARD",
                        fix="pcb_sync() to create the board from the schematic")
    op = {"op": "read", "what": list(what)}
    if refs:
        op["refs"] = list(refs)
    res = worker.run("board", {"pcb": str(pcb), "ops": [op], "save": False, "summary": False}, timeout=timeout)
    out = res["results"][0]
    out["revision"] = projmod.revision(pcb)
    return out


# ── netlist from the schematic ────────────────────────────────────────────
def _props(comp) -> dict:
    d = {}
    for p in sexpr.find_all(comp, "property"):
        n, v = sexpr.value(p, "name"), sexpr.value(p, "value", "")
        if n is not None:
            d[str(n)] = "" if v is None else str(v)
    return d


SKIP_FIELDS = {"Footprint", "Reference", "Value"}
ALWAYS_FIELDS = {"Datasheet", "Description"}  # synced even when empty (parity checks them)


def netlist(pr) -> dict:
    """Schematic netlist via ``kicad-cli sch export netlist --format kicadsexpr``.

    components: [{ref,value,footprint,fields,path,sheetname,sheetfile,dnp,exclude_bom,exclude_board,lib_id}]
    nets: [{name, nodes:[[ref,pin],...], pins:[{ref,pin,fn,type}]}]"""
    if not pr.sch.exists():
        raise ToolError(f"no schematic {pr.sch.name}", code="NO_SCHEMATIC",
                        fix="create the schematic first (sch_apply) — the board is generated from it")
    tmp = Path(tempfile.mkdtemp(prefix="copper-net-"))
    try:
        out = tmp / "net.net"
        r = env.run(["sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(out), str(pr.sch)], timeout=180)
        if not out.exists():
            raise ToolError(f"netlist export failed: {(r.stderr or r.stdout).strip()[-600:]}", code="NETLIST_FAILED",
                            fix="run verify() / ERC to find the schematic problem")
        tree = sexpr.loads(out.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    comps = []
    cs = sexpr.find(tree, "components") or []
    for c in sexpr.find_all(cs, "comp"):
        ref = str(sexpr.value(c, "ref", ""))
        fields = {}
        fl = sexpr.find(c, "fields")
        if fl:
            for f in sexpr.find_all(fl, "field"):
                n = sexpr.value(f, "name")
                v = f[2] if len(f) > 2 and not isinstance(f[2], list) else ""
                if n and str(n) not in SKIP_FIELDS and (v or str(n) in ALWAYS_FIELDS):
                    fields[str(n)] = str(v)
        props = _props(c)
        sp = sexpr.find(c, "sheetpath")
        sheet_ts = str(sexpr.value(sp, "tstamps", "/")) if sp else "/"
        ts = sexpr.find(c, "tstamps")
        uid = str(ts[1]) if ts and len(ts) > 1 else ""
        if not sheet_ts.endswith("/"):
            sheet_ts += "/"
        ls = sexpr.find(c, "libsource")
        comps.append({
            "ref": ref, "value": str(sexpr.value(c, "value", "")), "footprint": str(sexpr.value(c, "footprint", "") or ""),
            "fields": fields, "path": (sheet_ts + uid) if uid else "",
            "sheetname": props.get("Sheetname", ""), "sheetfile": props.get("Sheetfile", ""),
            "dnp": "dnp" in props, "exclude_bom": "exclude_from_bom" in props,
            "exclude_board": "exclude_from_board" in props,
            "lib_id": (f"{sexpr.value(ls, 'lib', '')}:{sexpr.value(ls, 'part', '')}" if ls else ""),
        })
    nets = []
    ns = sexpr.find(tree, "nets") or []
    for n in sexpr.find_all(ns, "net"):
        name = str(sexpr.value(n, "name", ""))
        nodes, pins = [], []
        for nd in sexpr.find_all(n, "node"):
            ref, pin = str(sexpr.value(nd, "ref", "")), str(sexpr.value(nd, "pin", ""))
            nodes.append([ref, pin])
            pins.append({"ref": ref, "pin": pin, "fn": str(sexpr.value(nd, "pinfunction", "") or ""),
                         "type": str(sexpr.value(nd, "pintype", "") or "")})
        nets.append({"name": name, "nodes": nodes, "pins": pins, "class": str(sexpr.value(n, "class", "") or "")})
    return {"components": comps, "nets": nets}


# ── DRC via kicad-cli ─────────────────────────────────────────────────────
def drc(pr, parity: bool = True, refill: bool = True, limit: int = 12, save_refill: bool = False,
        record: bool = True) -> dict:
    """KiCad DRC. Zones are refilled in memory first by default (``refill``; the file is only rewritten
    with ``save_refill``), so every pcb_* tool, verify and fab report the same counts whatever the
    state of the fills saved in the file. The result is recorded as the project's latest DRC
    (``record``), which is what verify / project_summary show as last_verify."""
    pcb = board_path(pr)
    if refill and save_refill and pcb.exists():
        projmod.guard_writable(pcb)     # --save-board rewrites the file: same lock + file-format policy as any board edit
    tmp = Path(tempfile.mkdtemp(prefix="copper-drc-"))
    try:
        rep = tmp / "drc.json"
        args = ["pcb", "drc", "--format", "json", "--units", "mm", "--severity-all", "-o", str(rep)]
        if parity and pr.sch.exists():
            args.append("--schematic-parity")
        if refill:
            args.append("--refill-zones")
            if save_refill:
                args.append("--save-board")
        r = env.run(args + [str(pcb)], timeout=600)
        if not rep.exists():
            raise ToolError(f"kicad-cli pcb drc failed: {(r.stderr or r.stdout).strip()[-600:]}", code="DRC_FAILED")
        d = json.loads(rep.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    viol = d.get("violations", [])
    unc = d.get("unconnected_items", [])
    par = d.get("schematic_parity", [])
    out = {
        "errors": sum(1 for v in viol if v.get("severity") == "error"),
        "warnings": sum(1 for v in viol if v.get("severity") == "warning"),
        "unconnected": len(unc),
        "parity": len(par),
        "by_type": {},
    }
    for v in viol:
        k = f"{v.get('severity')}:{v.get('type')}"
        out["by_type"][k] = out["by_type"].get(k, 0) + 1

    def brief(v):
        items = v.get("items", [])
        e = {"severity": v.get("severity"), "type": v.get("type"), "message": v.get("description"),
             "items": [i.get("description") for i in items][:3]}
        uu = [i.get("uuid") for i in items[:3] if i.get("uuid")]
        if uu:
            e["uuids"] = uu
        if items and items[0].get("pos"):
            e["at"] = [items[0]["pos"].get("x"), items[0]["pos"].get("y")]
        return e
    errs = sorted(viol, key=lambda v: 0 if v.get("severity") == "error" else 1)
    out["top"] = [brief(v) for v in errs[:limit]]
    if unc:
        out["unconnected_top"] = [brief(v) for v in unc[:limit]]
    if par:
        out["parity_top"] = [brief(v) for v in par[:limit]]
    if record:
        try:   # a run without the parity check must not overwrite the recorded parity count with 0
            from ..review.verify import record_external
            record_external(pr, "drc", out if parity and pr.sch.exists() else {k: v for k, v in out.items() if k != "parity"},
                            source="pcb")
        except Exception:  # noqa: BLE001 — bookkeeping only; never fail a DRC over it
            pass
    return out
