"""Netlist export through KiCad itself (the ground truth for connectivity)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from .. import sexpr
from ..kicad import env
from ..mcp.registry import ToolError


def export_netlist(pr, sch: Path | None = None) -> dict:
    """{'components': {ref: {'value','footprint','lib_id','fields':{..},'sheet'}},
        'nets': {net_name: [{'ref','pin','pin_name','pin_type'}]}} — from `kicad-cli sch export netlist`."""
    sch = Path(sch or pr.sch)
    if not sch.exists():
        raise ToolError(f"{sch.name} does not exist", code="NOT_FOUND", fix="sch_apply with place ops creates it")
    with tempfile.TemporaryDirectory(prefix="copper-net-") as td:
        out = Path(td) / "net.net"
        r = env.run(["sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(out), str(sch)], timeout=180)
        if not out.exists():
            raise ToolError(f"kicad-cli netlist export failed: {(r.stderr or r.stdout).strip()[-600:]}",
                            code="NETLIST_FAILED")
        tree = sexpr.loads(out.read_text(encoding="utf-8"))
    return parse_netlist(tree)


def parse_netlist(tree) -> dict:
    comps = {}
    for c in sexpr.find_all(sexpr.find(tree, "components") or [], "comp"):
        ref = str(sexpr.value(c, "ref", ""))
        ls = sexpr.find(c, "libsource")
        lib_id = ""
        if ls is not None:
            lib_id = f"{sexpr.value(ls, 'lib', '')}:{sexpr.value(ls, 'part', '')}"
        fields = {}
        fs = sexpr.find(c, "fields")
        for fl in sexpr.find_all(fs or [], "field"):
            name = sexpr.value(fl, "name")
            if name is not None:
                fields[str(name)] = str(fl[2]) if len(fl) > 2 and not isinstance(fl[2], list) else ""
        for p in sexpr.find_all(c, "property"):
            n, v = sexpr.value(p, "name"), sexpr.value(p, "value")
            if n is not None and str(n) not in fields and not str(n).startswith("ki_"):
                fields[str(n)] = str(v or "")
        sp = sexpr.find(c, "sheetpath")
        comps[ref] = {"value": str(sexpr.value(c, "value", "")), "footprint": str(sexpr.value(c, "footprint", "")),
                      "lib_id": lib_id, "fields": fields,
                      "sheet": str(sexpr.value(sp, "names", "/")) if sp is not None else "/"}
        if sexpr.find(c, "property") is not None:
            for p in sexpr.find_all(c, "property"):
                if str(sexpr.value(p, "name")) == "dnp":
                    comps[ref]["dnp"] = True
    nets = {}
    for n in sexpr.find_all(sexpr.find(tree, "nets") or [], "net"):
        name = str(sexpr.value(n, "name", ""))
        nodes = []
        for nd in sexpr.find_all(n, "node"):
            pin = str(sexpr.value(nd, "pin", ""))
            pf = str(sexpr.value(nd, "pinfunction", "") or "")
            if pf.endswith(f"_{pin}"):      # KiCad 10 suffixes the pin number to disambiguate stacked names
                pf = pf[: -len(pin) - 1]
            nodes.append({"ref": str(sexpr.value(nd, "ref", "")), "pin": pin,
                          "pin_name": pf,
                          "pin_type": str(sexpr.value(nd, "pintype", "") or "")})
        nets[name] = nodes
    return {"components": comps, "nets": nets}


def pin_nets(nl: dict) -> dict:
    """{(ref, pin): net_name}"""
    out = {}
    for name, nodes in nl["nets"].items():
        for nd in nodes:
            out[(nd["ref"], nd["pin"])] = name
    return out


def short_name(net: str) -> str:
    """'/USB_DP' → 'USB_DP' (root-sheet local label prefix)."""
    return net[1:] if net.startswith("/") and net.count("/") == 1 else net


def is_unconnected(net: str) -> bool:
    return net.startswith("unconnected-(") or net.startswith("Net-(") and "unconnected" in net


def check_expectations(nl: dict, expect: list[dict]) -> dict:
    """Verify Copper's intended connections against KiCad's netlist."""
    pn = pin_nets(nl)
    bad = []
    ok = 0
    for e in expect:
        if "same" in e:
            nets = {pn.get(tuple(k)) for k in e["same"]}
            if len(nets) == 1 and None not in nets:
                ok += 1
            else:
                bad.append({"pins": [".".join(k) for k in e["same"]], "expected": "same net", "got": sorted(map(str, nets))})
            continue
        for ref, pin in e["pins"]:
            got = pn.get((ref, pin))
            if got is not None and (short_name(got) == e["net"] or got == e["net"] or got.split("/")[-1] == e["net"]):
                ok += 1
            else:
                bad.append({"pin": f"{ref}.{pin}", "expected": e["net"], "got": got})
    return {"checked": ok + len(bad), "ok": ok, "mismatches": bad}
