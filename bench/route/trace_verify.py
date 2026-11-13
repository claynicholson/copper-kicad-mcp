"""Trace v1's Router._verify failures on a corpus board (router v2 W0 pre-work, design Appendix A1).

    PY bench/route/trace_verify.py [board]        default: one_air_max (about 2.5 minutes)

Runs the unchanged guided router on a scratch copy (bench/route/.work/trace_verify/<board>) with the suggested plan,
as the benchmark does, with Router._verify wrapped: for every connection it is about to drop it records the failing
segment, whether its ends are grid points, and every shape the segment violates (static or routed) with distances.
Writes results/oracle_diff/trace_<board>.json."""
import json
import math
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402

common.sandbox()
from copper_kicad import project as projmod  # noqa: E402
from copper_kicad.pcb import guided  # noqa: E402
from copper_kicad.pcb.guided import geom, router as routermod  # noqa: E402

board = sys.argv[1] if len(sys.argv) > 1 else "one_air_max"
work = common.WORK / "trace_verify" / board
if work.exists():
    shutil.rmtree(work)
work.mkdir(parents=True)
for f in (common.CORPUS / board).iterdir():
    if f.is_file() and f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_prl"):
        shutil.copy2(f, work / f.name)

events = []
passes = []


def offenders(r, c, lay, x1, y1, x2, y2, w):
    """Every shape the segment violates by v1's own test (need + 0.002), static and dynamic."""
    b = r.b
    nid = b.nets[c.net].id
    nc = r.cfg.net(c.net)
    hw, clr = w / 2, nc.clearance
    R = b.rules
    bb = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    out = []
    for name, idx in (("static", b.index), ("dyn", r.dyn)):
        for s in idx.near(lay, bb, hw + max(clr, R["edge_clearance"], R["hole_clearance"]) + 0.6):
            if s.cls == "cu":
                if s.net == nid and nid:
                    continue
                need = hw + max(clr, s.clr)
            elif s.cls == "hole":
                if s.net == nid and nid:
                    continue
                need = hw + R["hole_clearance"]
            elif s.cls == "kt":
                need = hw
            else:
                continue
            d = s.dist_seg(x1, y1, x2, y2)
            if d < need + 0.002:
                tag = s.tag
                e = {"index": name, "cls": s.cls, "kind": s.kind, "net": b.net_by_id[s.net].name if s.net in b.net_by_id else None,
                     "layers": list(s.layers), "dist": round(d, 5), "need": round(need, 5), "short_by": round(need - d, 5),
                     "g": [round(v, 4) for v in s.g] if s.kind == "box" else f"poly[{len(s.g)}] bbox {[round(v, 3) for v in s.bbox]}"}
                if name == "dyn" and tag is not None:
                    e["conn"] = tag.label()
                    e["conn_net"] = tag.net
                    e["routed_before"] = r.routed.index(tag) < r.routed.index(c) if tag in r.routed and c in r.routed else None
                    e["conn_pour"] = tag.pour
                elif tag is not None and hasattr(tag, "name"):
                    e["pad"] = tag.name
                out.append(e)
    return out


def on_grid(g, x, y):
    ix, iy = g.cell(x, y)
    return abs(g.mx(ix) - x) < 1e-6 and abs(g.my(iy) - y) < 1e-6


orig_verify = routermod.Router._verify


def traced(self):
    b = self.b
    bad_conns = 0
    for c in list(self.routed):
        nid = b.nets[c.net].id
        nc = self.cfg.net(c.net)
        for k, (lay, x1, y1, x2, y2, w) in enumerate(c.segs):
            if not b.seg_clear(lay, x1, y1, x2, y2, w / 2, nid, nc.clearance, self.dyn):
                bad_conns += 1
                if len(events) < 60:
                    events.append({"pass": len(passes), "mode": self.mode, "net": c.net, "conn": c.label(), "pour": c.pour,
                                   "seg_index": k, "n_segs": len(c.segs), "layer": lay,
                                   "seg": [round(v, 4) for v in (x1, y1, x2, y2, w)],
                                   "len": round(math.hypot(x2 - x1, y2 - y1), 4),
                                   "a_on_grid": on_grid(self.g, x1, y1), "b_on_grid": on_grid(self.g, x2, y2),
                                   "step": self.g.step, "clearance": nc.clearance, "notes": list(c.notes),
                                   "all_segs": [[l] + [round(v, 4) for v in s] for l, *s in c.segs],
                                   "vias": [list(v) for v in c.vias],
                                   "routed_on_net": sum(1 for r in self.routed if r.net == c.net),
                                   "offenders": offenders(self, c, lay, x1, y1, x2, y2, w)})
                break
    passes.append({"mode": self.mode, "routed": len(self.routed), "conns_failing_verify": bad_conns,
                   "nets_failing": sorted({e["net"] for e in events if e["pass"] == len(passes)})})
    return orig_verify(self)


routermod.Router._verify = traced
pr = projmod.Project(work, board)
facts = guided.analyze(pr, ripup="none")
out = guided.route(pr, facts["suggested_plan"], ripup="none")
failed = [e for e in out.get("failed") or [] if "self-check" in (e.get("reason") or "")]
res = {"board": board, "routed": out.get("routed"), "total": out.get("total"), "passes": passes, "events": events,
       "self_check_failed_reported": len(failed),
       "self_check_where": sorted({(e["net"], tuple(e.get("where") or [])) for e in failed})}
(common.RESULTS / "oracle_diff").mkdir(parents=True, exist_ok=True)
(common.RESULTS / "oracle_diff" / f"trace_{board}.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
print(json.dumps({k: v for k, v in res.items() if k != "events"}, indent=1, default=str))
print("events", len(events))
