"""Differential test of router v2's oracle against kicad-cli DRC (design §4.1, W0 gate).

    PY bench/route/oracle_diff.py refs [board ...]     corpus references: every human track / via, oracle vs KiCad
    PY bench/route/oracle_diff.py seeded               at-the-rule cases on a generated board: KiCad's real epsilon
    PY bench/route/oracle_diff.py refs --tol 0.0005    judge with another tolerance (default: types.TOL_MM)

``refs``: the human routing (reference.json) is committed to a scratch copy of the unrouted corpus board,
KiCad judges it (``pcb drc --all-track-errors --refill-zones``), and the oracle judges every committed item
against the board + all the other reference items. Reported per board: agreement, and every disagreement
with the rule, the distances, the KiCad message and (for items only the oracle flags) the known reason.

``seeded``: a small generated board with every obstacle kind (pads of several shapes, local / footprint
clearance overrides, a mask margin, net classes, a coupled differential pair, tracks, vias, an NPTH hole,
the board edge, a keep-out) gets probe tracks / vias at gaps of need + delta for a ladder of deltas (one
board per delta); KiCad says which it flags. The oracle is evaluated at several tolerances on the same
boards, which is how TOL_MM is chosen (largest with zero false-legal).

Everything is written under bench/route/.work (scratch KiCad config, scratch COPPER_HOME) and
bench/route/results/oracle_diff/.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402

# DRC types the oracle claims to predict for a single new track / via
MODELLED = {"clearance", "shorting_items", "tracks_crossing", "copper_edge_clearance", "hole_clearance",
            "hole_to_hole", "holes_co_located", "items_not_allowed", "solder_mask_bridge"}
OUT = common.RESULTS / "oracle_diff"
KICAD_EPS = 0.0005          # mm, measured by ``seeded`` (KiCad 10.0): flags iff gap < need - 0.5 um
TOLS = (0.0, 0.0003, 0.0004, 0.0005, 0.001)


def _imports():
    common.sandbox()
    from copper_kicad.pcb.guided import model, session
    from copper_kicad.pcb.v2 import oracle as oracle_mod, types
    return model, session, oracle_mod, types


def kicad_drc(pcb: Path, timeout: int = 900) -> list[dict]:
    out = pcb.parent / "drc.json"
    if out.exists():
        out.unlink()
    p = subprocess.run([str(common.KICAD_CLI), "pcb", "drc", "--format", "json", "--units", "mm", "--severity-all",
                        "--all-track-errors", "--refill-zones", "-o", str(out), str(pcb)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                       creationflags=common._NO_WINDOW)
    if not out.exists():
        raise RuntimeError(f"kicad-cli drc wrote no report (exit {p.returncode}): {(p.stderr or p.stdout)[-400:]}")
    return json.loads(out.read_text(encoding="utf-8")).get("violations", [])


def scratch(board: str, tag: str) -> Path:
    """Fresh copy of corpus/<board> under .work/oracle_diff/<tag>/<board>; returns the .kicad_pcb path."""
    d = common.WORK / "oracle_diff" / tag / board
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    for f in (common.CORPUS / board).iterdir():
        if f.is_file() and f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_prl"):
            shutil.copy2(f, d / f.name)
    return d / f"{board}.kicad_pcb"


def _why(orc, b, has_dru: bool) -> str:
    """Known reason an item is flagged by the oracle only ('UNEXPLAINED' when there is none)."""
    if b.dist - b.need >= -KICAD_EPS - 2e-6 and b.rule != "keepout" and b.tag != "mask":
        return "inside KiCad's 0.5 um epsilon"
    if b.tag == "mask":
        return "policy: graphic on a mask layer treated as a keep-out"
    if b.tag == "copper":
        return "approximation: copper graphic taken as its bounding box"
    if b.tag == "zone":
        return "approximation: teardrop / zone polygon"
    if b.static and b.tag in orc._pads_by_name:
        for pad in orc._pads_by_name[b.tag]:
            if any(s.kind == "poly" for shs in pad.shapes.values() for s in shs):
                return "approximation: custom pad shape as an outside polygon (worker, 5 um arc error)"
    if has_dru:
        return "custom .kicad_dru rule overrides the net class (dru_minimums is conservative)"
    return "UNEXPLAINED"


def judge(pcb: Path, tracks: list, vias: list, tols=(None,)) -> dict:
    """Commit ``tracks`` ([net, layer name, x1, y1, x2, y2, w]) and ``vias`` ([net, x, y, size, drill]) to the
    board file, let KiCad and the oracle (once per tolerance in ``tols``; None = types.TOL_MM) judge every item."""
    model, session, oracle_mod, types = _imports()
    res = session._run(pcb, [{"op": "extract"}, {"op": "commit", "tracks": tracks, "vias": vias}], save=True)
    ex, cm = res[0], res[1]
    uu = list(cm["track_uuids"]) + list(cm["via_uuids"])
    t0 = time.time()
    raw = kicad_drc(pcb)
    t_drc = time.time() - t0
    dru = pcb.with_suffix(".kicad_dru")
    board = model.Board(ex, model.dru_minimums(dru))
    ov = oracle_mod.local_overrides_from_file(pcb)
    lay = {n: i for i, n in enumerate(board.layers)}
    pieces = []
    for i, (net, ln, x1, y1, x2, y2, w) in enumerate(tracks):
        pieces.append(types.Piece(id=i + 1, net=net, conn=-1, kind="seg", layer=lay[ln], x1=x1, y1=y1, x2=x2, y2=y2,
                                  w=w, stage="legacy"))
    for j, (net, x, y, size, drill) in enumerate(vias):
        pieces.append(types.Piece(id=len(tracks) + j + 1, net=net, conn=-1, kind="via", layer=-1, x1=x, y1=y, x2=x,
                                  y2=y, w=size, drill=drill, stage="legacy"))
    verdicts = {}
    t_or = 0.0
    orc = None
    for tol in tols:
        orc = oracle_mod.Oracle(board, types.TOL_MM if tol is None else tol, overrides=ov)
        for p in pieces:
            orc.add_piece(p)
        t0 = time.time()
        verdicts[tol] = [orc.piece_ok(p) for p in pieces]
        t_or = time.time() - t0
    idx = {u: i for i, u in enumerate(uu)}
    kflag = {}          # item index -> [violation]
    other = {}          # types on our items the oracle does not model, by type
    for v in raw:
        ids = [idx[i["uuid"]] for i in v.get("items", []) if i.get("uuid") in idx]
        if not ids:
            continue
        if v.get("type") in MODELLED:
            if any(str(i.get("description", "")).startswith("Zone") for i in v.get("items", [])):
                other["zone:" + v["type"]] = other.get("zone:" + v["type"], 0) + 1
                continue
            for i in ids:
                kflag.setdefault(i, []).append(v)
        else:
            other[v.get("type")] = other.get(v.get("type"), 0) + 1
    items = []
    first = tols[0]
    for i, p in enumerate(pieces):
        b = verdicts[first][i]
        it = {"i": i, "kind": p.kind, "net": p.net, "layer": p.layer, "geom": [p.x1, p.y1, p.x2, p.y2, p.w, p.drill],
              "oracle": None if b is None else {"rule": b.rule, "dist": b.dist, "need": b.need, "tag": b.tag,
                                               "net": b.net, "at": list(b.at), "static": b.static},
              "kicad": [{"type": v["type"], "msg": v.get("description"), "sev": v.get("severity"),
                         "items": [x.get("description") for x in v.get("items", [])]} for v in kflag.get(i, [])]}
        if b is not None and not it["kicad"]:
            it["why"] = _why(orc, b, dru.exists())
        if len(tols) > 1:
            it["by_tol"] = {str(t): (verdicts[t][i].rule if verdicts[t][i] is not None else None) for t in tols}
        items.append(it)
    return {"items": items, "other_types": other, "n": len(pieces), "drc_s": round(t_drc, 1),
            "oracle_us_per_check": round(1e6 * t_or / max(1, len(pieces)), 1), "layers": board.layers,
            "rules": {k: v for k, v in board.rules.items() if k != "default"},
            "overrides": {k: (len(v) if isinstance(v, dict) else v) for k, v in ov.items()},
            "violations_total": len(raw)}


def summarize(res: dict) -> dict:
    both_ok = both_bad = k_only = o_only = 0
    dis, why = [], {}
    for it in res["items"]:
        o, k = it["oracle"] is not None, bool(it["kicad"])
        if o and k:
            both_bad += 1
        elif not o and not k:
            both_ok += 1
        elif k:
            k_only += 1
            dis.append(it)
        else:
            o_only += 1
            why[it["why"]] = why.get(it["why"], 0) + 1
            dis.append(it)
    return {"items": res["n"], "both_legal": both_ok, "both_flagged": both_bad, "false_legal": k_only,
            "false_illegal": o_only, "false_illegal_why": dict(sorted(why.items())),
            "unexplained": why.get("UNEXPLAINED", 0), "disagreements": dis}


def reference(board: str) -> tuple[list, list]:
    ref = json.loads((common.CORPUS / board / "reference.json").read_text(encoding="utf-8"))
    tracks = [[s[0], s[1], s[2], s[3], s[4], s[5], s[6]] for s in ref["segments"]
              if s[0] and math.hypot(s[4] - s[2], s[5] - s[3]) > 0]
    vias = [[v[0], v[1], v[2], v[3], v[4]] for v in ref["via_list"] if v[0] and (len(v) < 6 or v[5] == "through")]
    return tracks, vias


def run_refs(boards: list[str], tol: float | None = None, verbose: bool = True) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    table = {}
    for b in boards:
        if not (common.CORPUS / b / "reference.json").exists():
            if verbose:
                print(f"{b}: no reference routing (synthetic board) — skipped")
            continue
        pcb = scratch(b, "refs")
        tracks, vias = reference(b)
        res = judge(pcb, tracks, vias, (tol,))
        s = summarize(res)
        table[b] = {k: v for k, v in s.items() if k != "disagreements"}
        table[b].update(other_types=res["other_types"], oracle_us_per_check=res["oracle_us_per_check"],
                        drc_s=res["drc_s"], overrides=res["overrides"])
        (OUT / f"refs_{b}.json").write_text(json.dumps({"summary": table[b], "disagreements": s["disagreements"],
                                                        "rules": res["rules"], "layers": res["layers"]}, indent=1),
                                            encoding="utf-8")
        if verbose:
            print(f"{b}: {s['items']} items  legal/legal {s['both_legal']}  flagged/flagged {s['both_flagged']}  "
                  f"FALSE-LEGAL {s['false_legal']}  false-illegal {s['false_illegal']} {s['false_illegal_why']}  "
                  f"({res['oracle_us_per_check']} us/check, other KiCad types on items: {res['other_types']})")
            shown = 0
            for it in s["disagreements"]:
                if it["kicad"] or it.get("why") == "UNEXPLAINED":
                    print("    ", it["kind"], it["net"], "L%s" % it["layer"], it["geom"], "ORACLE", it["oracle"], "KICAD",
                          [(k["type"], k["msg"], k["items"]) for k in it["kicad"]][:2])
                    shown += 1
                    if shown >= 12:
                        break
    return table


# ── seeded at-the-rule cases ──────────────────────────────────────────────
DELTAS_UM = (6.0, 2.0, 1.0, 0.5, 0.4, 0.3, 0.2, 0.1, 0.0, -0.1, -0.2, -0.3, -0.4, -0.5, -0.6, -0.7, -0.9, -1.0, -1.1,
             -2.0, -6.0)

_BUILD = r'''
import json, sys
import pcbnew as P
out, spec = sys.argv[1], json.loads(open(sys.argv[2], encoding="utf-8").read())
b = P.BOARD()
def mm(v): return int(round(v * 1e6))
nets = {}
for n in spec["nets"]:
    ni = P.NETINFO_ITEM(b, n); b.Add(ni); nets[n] = ni
def edge(x1, y1, x2, y2, w):
    s = P.PCB_SHAPE(b); s.SetShape(P.SHAPE_T_SEGMENT); s.SetStart(P.VECTOR2I(mm(x1), mm(y1))); s.SetEnd(P.VECTOR2I(mm(x2), mm(y2)))
    s.SetLayer(P.Edge_Cuts); s.SetWidth(mm(w)); b.Add(s)
x0, y0, x1, y1 = spec["outline"]
for a in ((x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)):
    edge(*a, spec["edge_width"])
SH = {"rect": P.PAD_SHAPE_RECT, "circle": P.PAD_SHAPE_CIRCLE, "oval": P.PAD_SHAPE_OVAL, "roundrect": P.PAD_SHAPE_ROUNDRECT}
def call(fn, *a):
    try:
        return fn(P.F_Cu, *a)
    except Exception:
        return fn(*a)
for i, pd in enumerate(spec["pads"]):
    fp = P.FOOTPRINT(b); fp.SetReference(pd["ref"]); fp.SetPosition(P.VECTOR2I(mm(pd["x"]), mm(pd["y"])))
    fp.SetLayer(P.F_Cu)
    p = P.PAD(fp); p.SetNumber("1")
    kind = pd.get("kind", "smd")
    if kind == "smd":
        p.SetAttribute(P.PAD_ATTRIB_SMD); p.SetLayerSet(P.PAD.SMDMask())
    elif kind == "tht":
        p.SetAttribute(P.PAD_ATTRIB_PTH); p.SetLayerSet(P.PAD.PTHMask()); p.SetDrillSize(P.VECTOR2I(mm(pd["drill"]), mm(pd["drill"])))
    else:
        p.SetAttribute(P.PAD_ATTRIB_NPTH); p.SetLayerSet(P.PAD.UnplatedHoleMask()); p.SetDrillSize(P.VECTOR2I(mm(pd["drill"]), mm(pd["drill"])))
    call(p.SetShape, SH[pd["shape"]])
    call(p.SetSize, P.VECTOR2I(mm(pd["w"]), mm(pd["h"])))
    if pd["shape"] == "roundrect":
        call(p.SetRoundRectRadiusRatio, pd.get("rr", 0.25))
    p.SetPosition(P.VECTOR2I(mm(pd["x"]), mm(pd["y"])))
    if pd.get("rot"):
        p.SetOrientationDegrees(pd["rot"])
    if pd.get("net"):
        p.SetNet(nets[pd["net"]])
    if pd.get("clearance"):
        p.SetLocalClearance(mm(pd["clearance"]))
    if pd.get("fp_clearance"):
        fp.SetLocalClearance(mm(pd["fp_clearance"]))
    if pd.get("mask_margin"):
        p.SetLocalSolderMaskMargin(mm(pd["mask_margin"]))
    fp.Add(p); b.Add(fp)
for z in spec.get("keepouts", []):
    zn = P.ZONE(b); zn.SetIsRuleArea(True); zn.SetDoNotAllowTracks(True); zn.SetDoNotAllowVias(True)
    for name in ("SetDoNotAllowZoneFills", "SetDoNotAllowCopperPour"):
        try: getattr(zn, name)(False)
        except Exception: pass
    zn.SetDoNotAllowPads(False); zn.SetDoNotAllowFootprints(False)
    ls = P.LSET(); ls.AddLayer(P.F_Cu); ls.AddLayer(P.B_Cu); zn.SetLayerSet(ls)
    o = zn.Outline(); o.NewOutline()
    for x, y in z: o.Append(mm(x), mm(y))
    b.Add(zn)
P.SaveBoard(out, b)
print("OK")
'''

SEED_RULES = {"clearance": 0.2, "min_clearance": 0.15, "edge_clearance": 0.3, "hole_clearance": 0.25,
              "hole_to_hole": 0.5}


def _seed_spec():
    """The seeded board: every obstacle on a spot of its own, far from everything else."""
    pads = [
        {"ref": "PR", "shape": "rect", "x": 10, "y": 10, "w": 1.0, "h": 0.6, "net": "A"},
        {"ref": "PC", "shape": "circle", "x": 10, "y": 16, "w": 0.9, "h": 0.9, "net": "A"},
        {"ref": "PO", "shape": "oval", "x": 10, "y": 22, "w": 1.4, "h": 0.6, "net": "A"},
        {"ref": "PRR", "shape": "roundrect", "x": 10, "y": 28, "w": 1.0, "h": 0.8, "net": "A", "rr": 0.25},
        {"ref": "PX", "shape": "rect", "x": 10, "y": 34, "w": 1.0, "h": 0.6, "net": "A", "rot": 30},
        {"ref": "PL", "shape": "rect", "x": 10, "y": 40, "w": 1.0, "h": 0.6, "net": "A", "clearance": 0.35},
        {"ref": "PS", "shape": "rect", "x": 10, "y": 46, "w": 1.0, "h": 0.6, "net": "A", "clearance": 0.12},
        {"ref": "PT", "shape": "circle", "x": 10, "y": 52, "w": 1.6, "h": 1.6, "net": "A", "kind": "tht", "drill": 0.8},
        {"ref": "PN", "shape": "circle", "x": 10, "y": 58, "w": 1.0, "h": 1.0, "net": "", "kind": "npth", "drill": 1.0},
        {"ref": "PB", "shape": "rect", "x": 3, "y": 3, "w": 0.6, "h": 0.6, "net": "B"},
        {"ref": "PCN", "shape": "rect", "x": 50, "y": 70, "w": 0.6, "h": 0.6, "net": "C"},
        {"ref": "PF", "shape": "rect", "x": 40, "y": 10, "w": 1.0, "h": 0.6, "net": "A", "fp_clearance": 0.3},
        {"ref": "PM", "shape": "rect", "x": 40, "y": 16, "w": 1.0, "h": 0.6, "net": "A", "mask_margin": 0.3},
        {"ref": "PDP", "shape": "rect", "x": 40, "y": 22, "w": 1.0, "h": 0.6, "net": "DP_P"},
        {"ref": "PDN", "shape": "rect", "x": 55, "y": 76, "w": 0.6, "h": 0.6, "net": "DP_N"},
        {"ref": "PS2", "shape": "rect", "x": 40, "y": 28, "w": 1.0, "h": 0.6, "net": "A", "clearance": 0.17},
        # off the 0.1 um grid the worker's extract rounds to (true lower edge at y = 34.30009)
        {"ref": "PQ", "shape": "rect", "x": 40.00005, "y": 34.00005, "w": 1.0, "h": 0.60008, "net": "A"},
    ]
    return {"nets": ["A", "B", "C", "DP_P", "DP_N"], "outline": [0.0, 0.0, 60.0, 80.0], "edge_width": 0.1,
            "pads": pads, "keepouts": [[[30.0, 60.0], [36.0, 60.0], [36.0, 64.0], [30.0, 64.0]]]}


def _seed_cases():
    """[(case name, expected oracle rule, probe factory(delta_mm) -> ('seg'|'via', item))], fixed tracks, fixed vias.
    Probes are net B (class clearance 0.2), width 0.2 / via 0.6-0.3 unless the case says otherwise."""
    W, HW, CL = 0.2, 0.1, SEED_RULES["clearance"]
    VS, VD = 0.6, 0.3
    HC, HH, EC = SEED_RULES["hole_clearance"], SEED_RULES["hole_to_hole"], SEED_RULES["edge_clearance"]
    cases = []

    def seg_below(name, rule, y_edge, need, x=10.0, half_len=2.0, net="B"):
        # horizontal probe below an obstacle whose lower edge is at y_edge
        cases.append((name, rule, lambda d: ("seg", [net, "F.Cu", x - half_len, y_edge + need + d + HW, x + half_len,
                                                     y_edge + need + d + HW, W])))

    seg_below("seg|rect pad edge", "clearance", 10 + 0.3, CL)
    seg_below("seg|circle pad", "clearance", 16 + 0.45, CL)
    seg_below("seg|oval pad flat", "clearance", 22 + 0.3, CL)
    seg_below("seg|roundrect pad edge", "clearance", 28 + 0.4, CL)
    seg_below("seg|pad clearance override 0.35 (above class)", "clearance", 40 + 0.3, 0.35)
    seg_below("seg|pad clearance override 0.12 (below board min 0.15)", "clearance", 46 + 0.3, 0.15)
    seg_below("seg|pad clearance override 0.17 (below class 0.2)", "clearance", 28 + 0.3, 0.17, x=40.0)
    seg_below("seg|footprint clearance override 0.3", "clearance", 10 + 0.3, 0.3, x=40.0)
    seg_below("seg|pad solder-mask margin 0.3 (bridge)", "mask", 16 + 0.3, 0.3, x=40.0)
    seg_below("seg|tht pad copper", "clearance", 52 + 0.8, CL)
    seg_below("seg|pad off the extract's 0.1 um grid", "clearance", 34.30009, CL, x=40.0)
    seg_below("seg|npth hole", "hole", 58 + 0.5, HC)

    # 45-degree probe whose flank passes the corner (3.3, 3.3) of PB (net B; probe net C, class clearance 0.3)
    def diag(d):
        g = (0.3 + d + HW) * math.sqrt(2)
        cx, cy = 3.3 + g, 3.3
        return ("seg", ["C", "F.Cu", cx + 1.5, cy - 1.5, cx - 1.5, cy + 1.5, W])
    cases.append(("seg 45deg|rect pad corner (class 0.3 probe)", "clearance", diag))
    # rotated pad (30 deg): vertical probe to the right of its rightmost corner
    c30, s30 = math.cos(math.radians(30)), math.sin(math.radians(30))
    xr = 10 + 0.5 * c30 + 0.3 * s30
    cases.append(("seg|rotated rect pad corner", "clearance",
                  lambda d: ("seg", ["B", "F.Cu", xr + CL + d + HW, 32.0, xr + CL + d + HW, 36.0, W])))
    # track to track, parallel (fixed track net A width 0.25 at y = 5, x 20..45)
    cases.append(("seg|parallel track", "clearance",
                  lambda d: ("seg", ["B", "F.Cu", 20.0, 5.0 + 0.125 + CL + d + HW, 30.0, 5.0 + 0.125 + CL + d + HW, W])))
    # track end (round cap) to track: probe is vertical, ends above the fixed track at y = 12
    cases.append(("seg end cap|track", "clearance",
                  lambda d: ("seg", ["B", "F.Cu", 25.0, 12.0 - 0.125 - CL - d - HW, 25.0, 8.0, W])))

    # 45-degree fixed track (net A, width 0.25) from (20, 20) to (30, 30): parallel 45-degree probe
    def par45(d):
        off = (0.125 + CL + d + HW) * math.sqrt(2)
        return ("seg", ["B", "F.Cu", 20.0 + off, 20.0, 30.0 + off, 30.0, W])
    cases.append(("seg 45deg|parallel 45deg track", "clearance", par45))
    # net classes: fixed track of net C (class Wide, clearance 0.3) at y = 70
    cases.append(("seg|track of a class with clearance 0.3", "clearance",
                  lambda d: ("seg", ["B", "F.Cu", 20.0, 70.0 + 0.125 + 0.3 + d + HW, 30.0, 70.0 + 0.125 + 0.3 + d + HW, W])))
    # coupled differential pair (class Pair: clearance 0.25, pair gap 0.16): fixed DP_P track at y = 74
    cases.append(("seg DP_N|coupled DP_P track (pair gap 0.16)", "clearance",
                  lambda d: ("seg", ["DP_N", "F.Cu", 20.0, 74.0 + 0.125 + 0.16 + d + HW, 30.0, 74.0 + 0.125 + 0.16 + d + HW, W])))
    cases.append(("seg DP_N|coupled DP_P pad (pair gap 0.16)", "clearance",
                  lambda d: ("seg", ["DP_N", "F.Cu", 38.0, 22.3 + 0.16 + d + HW, 42.0, 22.3 + 0.16 + d + HW, W])))
    # other net next to a pair member: the class clearance 0.25 (second fixed DP_P track at y = 78)
    cases.append(("seg|DP_P track, not its partner (class 0.25)", "clearance",
                  lambda d: ("seg", ["B", "F.Cu", 20.0, 78.0 - 0.125 - 0.25 - d - HW, 30.0, 78.0 - 0.125 - 0.25 - d - HW, W])))
    # track to via copper (fixed via net A 0.8/0.4 at (25, 40))
    cases.append(("seg|via copper", "clearance",
                  lambda d: ("seg", ["B", "F.Cu", 22.0, 40.0 + 0.4 + CL + d + HW, 28.0, 40.0 + 0.4 + CL + d + HW, W])))
    # track to the hole of a via with a thin annulus (fixed via net A 0.5/0.45 at (25, 46)): the hole rule binds
    cases.append(("seg|via hole (annulus 0.025)", "hole",
                  lambda d: ("seg", ["B", "B.Cu", 22.0, 46.0 + 0.225 + HC + d + HW, 28.0, 46.0 + 0.225 + HC + d + HW, W])))
    # board edge (left edge x = 0, drawn 0.1 wide): vertical probe
    cases.append(("seg|board edge (line 0.1 wide)", "edge",
                  lambda d: ("seg", ["B", "F.Cu", EC + d + HW, 20.0, EC + d + HW, 30.0, W])))
    # keep-out (x 30..36, y 60..64): horizontal probe below it, need 0
    cases.append(("seg|keep-out", "keepout",
                  lambda d: ("seg", ["B", "F.Cu", 31.0, 64.0 + d + HW, 35.0, 64.0 + d + HW, W])))
    # vias
    cases.append(("via|rect pad edge", "clearance", lambda d: ("via", ["B", 10.0, 10.3 + CL + d + VS / 2, VS, VD])))
    cases.append(("via|track", "clearance", lambda d: ("via", ["B", 40.0, 5.0 + 0.125 + CL + d + VS / 2, VS, VD])))
    cases.append(("via|via copper", "clearance", lambda d: ("via", ["B", 40.0, 40.0 + 0.4 + CL + d + VS / 2, VS, VD])))
    cases.append(("via|board edge", "edge", lambda d: ("via", ["B", EC + d + VS / 2, 40.0, VS, VD])))
    # via with a thin annulus (0.6 / 0.56) next to a foreign track: its own hole binds (0.28 + 0.25 > 0.3 + 0.2)
    cases.append(("via own hole|foreign track", "hole",
                  lambda d: ("via", ["B", 44.0, 5.0 - 0.125 - HC - d - 0.28, VS, 0.56])))
    # via to NPTH hole (r 0.5): hole-to-hole binds (0.5 + 0.15 > 0.25 + 0.3)
    cases.append(("via|npth hole (hole to hole)", "hole",
                  lambda d: ("via", ["B", 10.0, 58.0 - 0.5 - HH - d - VD / 2, VS, VD])))
    # same net: only hole-to-hole applies (tht pad drill 0.8; fixed via 0.8/0.4 at (50, 40))
    cases.append(("via|same-net tht hole (hole to hole)", "hole",
                  lambda d: ("via", ["A", 10.0, 52.0 - 0.4 - HH - d - VD / 2, VS, VD])))
    cases.append(("via|same-net via (hole to hole)", "hole",
                  lambda d: ("via", ["A", 50.0, 40.0 + 0.2 + HH + d + VD / 2, VS, VD])))
    cases.append(("via|keep-out", "keepout", lambda d: ("via", ["B", 33.0, 60.0 - d - VS / 2, VS, VD])))
    fixed_t = [["A", "F.Cu", 20.0, 5.0, 45.0, 5.0, 0.25], ["A", "F.Cu", 20.0, 12.0, 30.0, 12.0, 0.25],
               ["A", "F.Cu", 20.0, 20.0, 30.0, 30.0, 0.25], ["C", "F.Cu", 20.0, 70.0, 30.0, 70.0, 0.25],
               ["DP_P", "F.Cu", 20.0, 74.0, 30.0, 74.0, 0.25], ["DP_P", "F.Cu", 20.0, 78.0, 30.0, 78.0, 0.25]]
    fixed_v = [["A", 25.0, 40.0, 0.8, 0.4], ["A", 25.0, 46.0, 0.5, 0.45], ["A", 40.0, 40.0, 0.8, 0.4],
               ["A", 50.0, 40.0, 0.8, 0.4]]
    return cases, fixed_t, fixed_v


def build_seed_board(dst: Path) -> Path:
    """Board (pcbnew, in KiCad's Python) + project file with the design rules (they live in the .kicad_pro)."""
    common.sandbox()
    dst.mkdir(parents=True, exist_ok=True)
    pcb = dst / "seed.kicad_pcb"
    script, spec = dst / "_build.py", dst / "spec.json"
    script.write_text(_BUILD, encoding="utf-8")
    spec.write_text(json.dumps(_seed_spec()), encoding="utf-8")
    p = subprocess.run([str(common.KICAD_PY), "-X", "utf8", str(script), str(pcb), str(spec)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                       creationflags=common._NO_WINDOW)
    if "OK" not in (p.stdout or "") or not pcb.exists():
        raise RuntimeError(f"seed board build failed: {(p.stderr or p.stdout)[-800:]}")
    pro = json.loads((common.CORPUS / "tinytapeout" / "tinytapeout.kicad_pro").read_text(encoding="utf-8"))
    R = SEED_RULES
    pro["board"]["design_settings"].setdefault("rules", {}).update(
        min_clearance=R["min_clearance"], min_copper_edge_clearance=R["edge_clearance"],
        min_hole_clearance=R["hole_clearance"], min_hole_to_hole=R["hole_to_hole"], min_track_width=0.1,
        min_via_diameter=0.3, min_through_hole_diameter=0.2, min_via_annular_width=0.0)
    sev = pro["board"]["design_settings"].setdefault("rule_severities", {})
    for k in MODELLED:
        sev[k] = "error"
    base = dict(pro["net_settings"]["classes"][0])
    base.update(name="Default", clearance=R["clearance"], track_width=0.2, via_diameter=0.6, via_drill=0.3,
                diff_pair_gap=0.25, diff_pair_width=0.2)
    wide = dict(base, name="Wide", clearance=0.3, priority=0)
    pair = dict(base, name="Pair", clearance=0.25, diff_pair_gap=0.16, priority=1)
    pro["net_settings"]["classes"] = [base, wide, pair]
    pro["net_settings"]["netclass_patterns"] = [{"netclass": "Wide", "pattern": "C"}, {"netclass": "Pair", "pattern": "DP_*"}]
    pro["net_settings"]["netclass_assignments"] = None
    pro.setdefault("meta", {})["filename"] = "seed.kicad_pro"
    (dst / "seed.kicad_pro").write_text(json.dumps(pro, indent=2), encoding="utf-8")
    return pcb


def run_seeded(deltas=DELTAS_UM, tols=TOLS, verbose: bool = True) -> dict:
    """One board per delta (so probes never see each other).
    -> {cases: {case: {delta_um: {kicad: [types], oracle: {tol: rule | None}}}}, epsilon: {...}, tols: {...}}"""
    OUT.mkdir(parents=True, exist_ok=True)
    root = common.WORK / "oracle_diff" / "seeded"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    base = build_seed_board(root / "base")
    cases, fixed_t, fixed_v = _seed_cases()
    table = {c[0]: {} for c in cases}
    rules_seen = {}
    fixed_flags = 0
    for du in deltas:
        d = root / f"d{du:+.1f}"
        d.mkdir(parents=True, exist_ok=True)
        pcb = d / "seed.kicad_pcb"
        shutil.copy2(base, pcb)
        shutil.copy2(base.with_suffix(".kicad_pro"), pcb.with_suffix(".kicad_pro"))
        tracks, vias, where = list(fixed_t), list(fixed_v), []
        for name, _rule, make in cases:
            kind, item = make(du / 1000.0)
            item = [round(v, 6) if isinstance(v, float) else v for v in item]
            if kind == "seg":
                where.append((name, "t", len(tracks)))
                tracks.append(item)
            else:
                where.append((name, "v", len(vias)))
                vias.append(item)
        res = judge(pcb, tracks, vias, tols)
        rules_seen = res["rules"]
        for name, tv, k in where:
            it = res["items"][k if tv == "t" else len(tracks) + k]
            table[name][du] = {"kicad": sorted({v["type"] for v in it["kicad"]}), "oracle": it["by_tol"],
                               "msg": it["kicad"][0]["msg"] if it["kicad"] else None}
        for k in list(range(len(fixed_t))) + [len(tracks) + j for j in range(len(fixed_v))]:
            it = res["items"][k]
            # fixed copper is only ever flagged together with a probe; alone it must be clean for both judges
            if du == max(deltas) and (it["kicad"] or it["oracle"]):
                fixed_flags += 1
    out = {"deltas_um": list(deltas), "cases": table, "expected_rule": {c[0]: c[1] for c in cases},
           "board_rules": rules_seen, "fixed_copper_flagged_at_max_delta": fixed_flags}
    eps = {}
    for name, row in table.items():
        ok = [du for du, r in row.items() if not r["kicad"]]
        bad = [du for du, r in row.items() if r["kicad"]]
        eps[name] = {"kicad_accepts_down_to_um": min(ok) if ok else None, "kicad_flags_from_um": max(bad) if bad else None,
                     "kicad_type": sorted({t for r in row.values() for t in r["kicad"]})}
    out["epsilon"] = eps
    by_tol = {}
    for t in tols:
        fl, fi, wrong = [], [], []
        for name, row in table.items():
            for du, r in row.items():
                o = r["oracle"][str(t)]
                if r["kicad"] and not o:
                    fl.append([name, du])
                elif o and not r["kicad"]:
                    fi.append([name, du])
                if o and o not in (out["expected_rule"][name], "short"):
                    wrong.append([name, du, o])
        by_tol[str(t)] = {"false_legal": fl, "false_illegal": fi, "unexpected_rule": wrong}
    out["tols"] = by_tol
    (OUT / "seeded.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    if verbose:
        print("board rules:", rules_seen)
        print(f"{'case':58s} KiCad accepts down to / flags from (um)   KiCad type")
        for name, e in eps.items():
            print(f"{name:58s} {str(e['kicad_accepts_down_to_um']):>6s} / {str(e['kicad_flags_from_um']):>6s}   {e['kicad_type']}")
        for t, r in by_tol.items():
            print(f"tol {t} mm: false-legal {len(r['false_legal'])} {r['false_legal'][:6]}  false-illegal "
                  f"{len(r['false_illegal'])}  unexpected rule {r['unexpected_rule'][:4]}")
    return out


def main(argv):
    tol = None
    if "--tol" in argv:
        i = argv.index("--tol")
        tol = float(argv[i + 1])
        del argv[i:i + 2]
    if not argv or argv[0] not in ("refs", "seeded"):
        print(__doc__)
        return 2
    if argv[0] == "seeded":
        run_seeded()
        return 0
    boards = argv[1:] or sorted(p.name for p in common.CORPUS.iterdir() if p.is_dir())
    table = run_refs(boards, tol)
    if not argv[1:]:
        (OUT / "refs.json").write_text(json.dumps(table, indent=1), encoding="utf-8")
    keys = ("items", "both_legal", "both_flagged", "false_legal", "false_illegal", "unexplained")
    tot = {k: sum(v[k] for v in table.values()) for k in keys}
    why = {}
    for v in table.values():
        for k, n in v["false_illegal_why"].items():
            why[k] = why.get(k, 0) + n
    print("TOTAL", tot)
    print("false-illegal by reason:", why)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
