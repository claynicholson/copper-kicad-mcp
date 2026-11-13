"""Guided autorouter tools: pcb_route_plan (read-only analysis) and pcb_route_guided.

The idea: whichever model drives Copper writes the *heuristics* (a plan: net order,
widths, layer preferences, corridors, cost regions); a deterministic router turns the plan
into copper; KiCad's own DRC decides what counts. Copper never calls a model, and with no
plan at all the built-in heuristics are used, so the router also works with no AI.
"""
from __future__ import annotations

import json
import shutil

from .. import project as projmod
from ..mcp.registry import Image, Result, ToolError, add_help, tool
from ..pcb import guided

PROJECT = {"type": "string", "description": "Project folder or .kicad_pro/.kicad_pcb path. Omit to use the active project."}
WHILE_OPEN = {"type": "string", "enum": ["refuse", "revert"],
              "description": "If the board is open in KiCad: 'refuse' (default) or 'revert' = write anyway, then use File ▸ Revert in KiCad."}


# ── pcb_route_plan ────────────────────────────────────────────────────────
def _auto(pr, ripup, board=None) -> dict:
    """{'auto': ...} (+ 'cuts' from a global-stage preview: v2 is the default engine) for pcb_route_plan.
    Read-only; never fails the tool: what cannot be computed is left out with a one-line note."""
    try:
        from ..pcb.guided import plan as planmod, session
        from ..pcb.v2 import planx
        b = board if board is not None else session.load_board(pr, ripup)[0]
        cfg = planmod.resolve(b, None)
        out = {"auto": planx.auto_preview(b, cfg)}
        try:        # electrical constraints the router would work to with no plan (names, net classes, KiCad rules)
            from ..pcb.v2 import constraints as cmod
            pv = cmod.preview(cmod.resolve(b, cfg, cfg.x, cmod.read_project(pr, layers=b.L)))
            if pv:
                out["auto"]["constraints"] = pv
        except Exception:  # noqa: BLE001 — a preview must never break the read-only analysis
            pass
        if cfg.engine == "v2":
            cuts = _global_cuts(b, cfg)
            if cuts is not None:
                out["cuts"] = cuts
        return out
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        return {"auto": {"note": f"not available: {type(e).__name__}: {str(e)[:120]}"}}


def _global_cuts(board, cfg):
    """Cuts over capacity from the v2 classify + global stages alone (no copper, seconds). None when those stages
    are not built."""
    try:
        from ..pcb.v2 import classify, diag, globalroute, tiles as tilesmod
        from ..pcb.v2.oracle import Oracle
        from ..pcb.v2.pieces import RouteState
        from ..pcb.v2.types import WORK_PER_SECOND, Budget, Ctx
        if any(getattr(m, "STUB", False) for m in (classify, globalroute, tilesmod)):
            return None
        ctx = Ctx(board=board, cfg=cfg, x=cfg.x, oracle=Oracle(board))
        ctx.state = RouteState(ctx.oracle, ctx.conns)
        budget = Budget(int(10 * WORK_PER_SECOND))
        ctx.reports.append(classify.run(ctx, budget))
        ctx.reports.append(globalroute.run(ctx, budget))
        return diag.cuts(ctx)
    except Exception:  # noqa: BLE001 — a preview must never break the read-only analysis
        return None


@tool("pcb_route_plan",
      """Analyse the board for routing and return what you need to write a routing plan for pcb_route_guided.
Read-only. Call it once before pcb_route_guided when you want to steer the router; skip it to use the defaults.

Returns (compact, mm, KiCad coordinates: x right, y down):
  board      size, extent [x0,y0,x1,y1], copper layers (which carry a pour / act as a plane, SMD pad counts)
  rules      board minimums; net_classes: track / clearance / via per class
  nets       table [net, kind, pads, unrouted, est_mm, class, track, clearance, flags] — kind is a guess from
             names + topology: ground | power | diff | clock | hs | analog | signal; flags: crystal, "P of <net>",
             "pour:F.Cu+B.Cu" (a pour connects it — only pads the pour misses get routed)
  pours, keepouts (rule areas incl. copper-antenna-* antenna keep-outs, as rects)
  congestion text map (one char per cell: digit = connections that must cross it, # = no free copper) and
             hot_spots [{at:[x,y], rect, pads, connections_crossing, free, parts}] — put cost regions /
             route order around these
  suggested_plan   the built-in heuristics as an editable plan; plan_schema: every field
  auto       what the router (engine 'v2', the default) would decide by itself: roles {net: plane|power|pair|bus|critical} (signal nets
             left out), buses, power widths, layer directions — veto any of it in the plan (nets[..].role / width,
             buses, global.layer_directions)
heatmap=true attaches a picture of the congestion map with the unrouted connections drawn.
ripup='all' analyses the board as if every (unlocked) track were removed — use it before a full re-route.

Example: pcb_route_plan() → edit suggested_plan (e.g. add order, a corridor, a region) → pcb_route_guided(plan=...).""",
      {"project": PROJECT,
       "nets": {"type": "array", "items": {"type": "string"},
                "description": "Only list these nets in the table (names or globs, e.g. ['/USB*','VBUS'])"},
       "ripup": {"type": "string", "enum": ["none", "all"],
                 "description": "none (default) = as the board is; all = as if all unlocked tracks were ripped up"},
       "heatmap": {"type": "boolean", "description": "Attach the congestion heat-map PNG (default true)"}},
      read_only=True, idempotent=True, group="pcb", title="Routing plan analysis")
def pcb_route_plan(project=None, nets=None, ripup="none", heatmap=True):
    pr = projmod.resolve(project)
    hp = pr.out_dir / "pcb-route-plan.png" if heatmap else None
    d = guided.analyze(pr, ripup=ripup, nets=nets, heatmap_path=hp)
    img = d.pop("_heatmap", None)
    d.update(_auto(pr, ripup, d.pop("_board", None)))
    r = d["routing"]
    hs = d["congestion"]["hot_spots"]
    summary = (f"{pr.name}: {d['board']['size_mm'][0]}×{d['board']['size_mm'][1]} mm, {len(d['board']['layers'])} copper "
               f"layers, {r['unrouted_connections']} unrouted connections in {r['nets_to_route']} nets"
               + (f", tightest spot at {hs[0]['at']} ({', '.join(hs[0]['parts'][:3])})" if hs else ""))
    images = [Image(path=img, caption="congestion heat-map (red = many connections through little free copper; "
                                      "white lines = unrouted connections)")] if img else []
    return Result(d, images, summary=summary)


# ── pcb_route_guided ──────────────────────────────────────────────────────
def _findings(out: dict) -> list[dict]:
    f = []
    drc = out.get("drc") or {}
    failed = out.get("failed") or []
    if failed:
        blockers = {}
        for e in failed:
            for n in e.get("blocked_by") or []:
                blockers[n] = blockers.get(n, 0) + 1
        nets = sorted({e["net"] for e in failed})
        off = [e for e in failed if "outside the board outline" in (e.get("reason") or "")]
        if off:
            f.append({"severity": "warning", "code": "PARTS_OFF_BOARD", "message": f"{len(off)} of the unrouted "
                      f"connections end on a part that is not on the board",
                      "fix": "pcb_place(auto=true) or pcb_place(moves=[...]) to put every part inside the outline, "
                             "then pcb_route_guided() again"})
            failed = [e for e in failed if e not in off]
            nets = sorted({e["net"] for e in failed})
        late = sum(1 for e in failed if "time budget" in (e.get("reason") or "") or "effort" in (e.get("reason") or ""))
        if not failed:
            pass
        elif 2 * late >= len(failed):
            fix = (f"{late} of them were never tried or ran out of search budget: raise plan.global.time_budget_s "
                   "(max 600) / effort, use a coarser global.grid, or route in batches with nets=[...]")
        elif blockers:
            top = sorted(blockers, key=lambda k: (-blockers[k], k))[:4]
            fix = (f"route the failed nets first: plan.order={nets[:6] + ['*']} — or give {top} a detour "
                   f"(plan.nets['{top[0]}'].avoid / corridor), lower global.via_cost, or raise global.ripup_rounds")
        elif any("boxed in" in (e.get("reason") or "") for e in failed):
            fix = ("the pad has no room for a track at this width/clearance: pcb_place(moves=[...]) to open space, "
                   "plan.nets[<net>].width smaller, or allow more layers")
        elif any("max_" in (e.get("reason") or "") for e in failed):
            fix = "relax plan.nets[<net>].max_length / max_vias, or move the parts closer"
        elif any("time budget" in (e.get("reason") or "") or "effort" in (e.get("reason") or "") for e in failed):
            fix = "raise plan.global.time_budget_s / effort, or use a coarser global.grid"
        else:
            fix = ("check plan regions / avoid / layers for these nets (pcb_route_plan shows keep-outs and hot spots); "
                   "pcb_route(mode='auto') (Freerouting) or pcb_route(tracks=[...]) can finish single connections")
        # KiCad's ratsnest is the count that matters (a connection can be reported by more than one pass)
        n_un = len(failed)
        if not off and out.get("total") is not None and 0 < out["total"] - out.get("routed", 0) < n_un:
            n_un = out["total"] - out["routed"]
        if failed:
            sug = [s for s in out.get("suggest") or [] if isinstance(s, dict) and s.get("patch")]
            if sug:                                   # the router's own, specific advice goes first
                fix = f"suggest[0]: {sug[0].get('why')} → {_patch_text(sug[0]['patch'])}. Otherwise: {fix}"
            f.append({"severity": "warning", "code": "UNROUTED", "fix": fix, "message":
                      f"{n_un} connection(s) not routed in {len(nets)} net(s): {', '.join(nets[:8])}"})
    cf = _cons_findings(out)                  # after UNROUTED: its fix leads with the router's own suggest[0]
    f.extend(_v2_findings(out))
    f.extend(cf)
    prev = out.get("previous") or {}
    if prev and (out.get("unrouted_after") or 0) > (prev.get("unrouted") or 0):
        f.append({"severity": "warning", "code": "WORSE_THAN_BEFORE", "message":
                  f"the board had {prev.get('unrouted')} unrouted connection(s) before this rip-up and has "
                  f"{out['unrouted_after']} now",
                  "fix": f"{prev.get('restore')} brings the previous routing back; next time try the plan with "
                         f"dry_run=true or compare=true first, or re-route only part of it with ripup='nets'"})
    for s in out.get("skipped") or []:
        if "pour" in s["reason"]:
            f.append({"severity": "info", "code": "LEFT_FOR_POUR", "message": f"{s['net']}: {s['connections']} connections "
                      f"left for a pour", "fix": f"pcb_pour(net='{s['net']}') then pcb_route_guided() again to connect "
                      f"the pads the pour does not reach — or plan.nets['{s['net']}'].skip=false to route it as tracks"})
    for r in out.get("removed_after_drc") or []:
        f.append({"severity": "warning", "code": "REMOVED_AFTER_DRC", "message": f"{r['net']}: {r['why']}",
                  "fix": f"its copper was removed again (reported as unrouted). Give it another path: "
                         f"plan.nets['{r['net']}'].avoid / corridor / layers, then re-run"})
    if drc.get("errors"):
        types = ", ".join(f"{k.split(':', 1)[1]}×{v}" for k, v in sorted((drc.get("by_type") or {}).items())
                          if k.startswith("error:"))
        new = drc.get("new_errors")
        # length / skew / pair markers are constraint verdicts with their own findings (LENGTH_MISMATCH, PAIR_*)
        copper_new = max(0, (new or 0) - (drc.get("constraint_errors") or 0))
        if copper_new:
            f.append({"severity": "error", "code": "DRC_ERRORS_NEW", "message": f"{copper_new} DRC error(s) that were not on "
                      f"the board before this run (see drc.new_top) — the router could not repair or attribute them",
                      "fix": "checkpoint(action='restore', id=<checkpoint>) undoes the run; or give the nets near "
                             "drc.new_top[].at another path (plan.nets[..].avoid / plan.regions) and re-run. A "
                             "starved_thermal '… isolated island' is a pour pad listed under failed."})
        old = drc["errors"] - (new or 0)
        if old:
            f.append({"severity": "warning" if new is not None else "error", "code": "DRC_ERRORS", "message":
                      f"{old} DRC error(s) on the board" + (" that were there before routing" if new is not None else "")
                      + f" (all errors by type: {types})",
                      "fix": "verify() lists them with fixes (placement / footprint / outline issues are not "
                             "routing problems)"})
    if drc.get("parity"):
        f.append({"severity": "warning", "code": "SCHEMATIC_PARITY",
                  "message": f"{drc['parity']} board/schematic mismatches", "fix": "pcb_sync()"})
    limited = {(r.get("p"), r.get("n")) for r in (out.get("constraints") or {}).get("pairs") or []
               if r.get("max_skew") is not None}
    for p in out.get("diff_pairs") or []:
        if p["skew_mm"] > 1.0 and (p["p"], p["n"]) not in limited:
            f.append({"severity": "info", "code": "PAIR_SKEW", "message": f"{p['p']} / {p['n']}: length mismatch "
                      f"{p['skew_mm']} mm ({p['len_p']} vs {p['len_n']})",
                      "fix": "fine for USB 2.0 full/high speed on a short run; for tighter pairs route them by hand "
                             "in KiCad's differential-pair router"})
    for w in out.get("warnings") or []:
        f.append({"severity": "info", "code": "NOTE", "message": w})
    return f


def _patch_text(patch: dict) -> str:
    if "move" in patch:
        m = patch["move"]
        return f"pcb_place(moves=[{{'ref': '{m.get('ref')}', 'by': {m.get('by')}}}])"
    return "plan patch " + json.dumps(patch, separators=(",", ":"))


_QUALITY = {
    "vias_over_budget": "more vias than planned",
    "necked_share": "share of the net below its width",
    "detour": "routed length / direct length",
    "bus_unbundled": "bus members that left the bundle",
    "power_thinner_than_pad": "narrowest power track, mm (limit = what its pads allow)",
    "odd_angle_share": "share of segments that are neither axial nor 45°",
}


def _v2_findings(out: dict) -> list[dict]:
    """Findings from the v2 diagnostics (design §6.2): cuts over capacity named by failure certificates,
    the critique of what was routed, and the ready patches in suggest[]. Empty for the 'guided' engine."""
    f = []
    sug = [s for s in out.get("suggest") or [] if isinstance(s, dict) and s.get("patch")]
    seen = set()
    for e in out.get("failed") or []:
        c = e.get("cut")
        if not isinstance(c, dict) or (c.get("demand_mm") or 0) <= (c.get("capacity_mm") or 0):
            continue
        key = (c.get("a"), c.get("b"), c.get("layer"))
        if key in seen or len(seen) >= 3:
            continue
        seen.add(key)
        mine = next((s for s in sug if str(c.get("a")) in s.get("why", "") and str(c.get("b")) in s.get("why", "")), None)
        fix = (f"{mine['why']} → {_patch_text(mine['patch'])}" if mine else
               "open the gap (pcb_place(moves=[...])), let the net use another layer (plan.nets[..].layers / "
               "max_vias), or escape the part through vias (plan.fanout)")
        also = [n for n in c.get("nets") or [] if n != e.get("net")]
        f.append({"severity": "warning", "code": "CUT_OVER_CAPACITY", "fix": fix, "message":
                  f"{e.get('net')} ({e.get('from')}→{e.get('to')}): {c.get('demand_mm')} mm of track + clearance "
                  f"must pass " + (f"through {c.get('a')}" if c.get("a") == c.get("b") else
                                   f"between {c.get('a')} and {c.get('b')}")
                  + f" on {c.get('layer')}, {c.get('capacity_mm')} mm fits"
                  + (f" (also wanted there: {', '.join(also[:4])})" if also else "")})
    by_kind = {}
    for c in out.get("critique") or []:
        if isinstance(c, dict) and c.get("kind"):
            by_kind.setdefault(c["kind"], []).append(c)
    for kind in sorted(by_kind):
        rows = by_kind[kind]
        first = ", ".join(f"{r.get('net') or 'board'} {r.get('value')} (limit {r.get('limit')})" for r in rows[:3])
        total = len(rows) + sum(int(r.get("more") or 0) for r in rows)
        f.append({"severity": "info", "code": "ROUTE_QUALITY", "message":
                  f"{kind} — {_QUALITY.get(kind, kind)}: {first}" + (f" +{total - 3} more" if total > 3 else ""),
                  "fix": "critique[] has every case with its position; suggest[] has a patch for most of them"
                         if sug else "critique[] has every case with its position"})
    if sug:
        f.append({"severity": "info", "code": "SUGGESTED_PATCHES", "message":
                  f"{len(sug)} ready patch(es) in suggest[]: " + " | ".join(str(s.get("why"))[:110] for s in sug[:3]),
                  "fix": "merge a patch into the plan (objects merge key by key; regions / buses / reserve append; "
                         "order replaces) and run again with ripup='all' or ripup='nets'; a {'move': ...} patch is "
                         "for pcb_place(moves=[...])"})
    return f


CONS_MAX = 6              # rows per kind of the constraints block in the compact result (misses first)
CONS_FIND_MAX = 3         # cases named in a constraint finding (the block lists every case)


def _mm(v) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def _cons_findings(out: dict) -> list[dict]:
    """Findings from out['constraints'] (design §14.10): one per code, naming up to CONS_FIND_MAX cases with the
    amount in mm and a fix. A patch that adopts a name-detected length group is appended to out['suggest'].
    Length groups are tuned by the router (v2/tune.py): a group listed here is one the tuner could not close, and
    the case says why. Pairs are laid as coupled lanes (v2/pairs.py): a pair listed here is one that could not be
    (routed as two nets, with the reason) or that misses a limit, with the amount."""
    c = out.get("constraints")
    if not isinstance(c, dict):
        return []
    cases, sev, fixes = {}, {}, {}
    rank = {"info": 0, "warning": 1, "error": 2}

    def add(code, severity, case, fix):
        cases.setdefault(code, []).append(case)
        if rank[severity] >= rank.get(sev.get(code), -1):
            sev[code] = severity
        fixes.setdefault(code, fix)

    for g in c.get("groups") or []:
        if g.get("mode") == "report":
            if g.get("src") == "name" and g.get("nets"):
                patch = {"groups": [{"nets": list(g["nets"]), "tol": g.get("tol")}]}
                spread = f", routed {_mm(g['min'])}..{_mm(g['max'])} mm" if g.get("min") is not None else ""
                add("CONSTRAINT_GUESSED", "info", f"{g['name']} ({g['n']} nets{spread}, tol {_mm(g.get('tol'))} typical)",
                    "measured, not enforced. suggest[] has the plan patch that adopts each; "
                    "plan.groups=[{'nets': [...], 'off': true}] silences one, plan.groups=[] all")
                out.setdefault("suggest", []).append({"why": f"adopt the name-detected length group {g['name']}",
                                                      "patch": patch})
            continue
        if g.get("ok") is not False:
            continue
        level = "warning" if g.get("src") in ("plan", "dru") else "info"
        if g.get("open"):
            what = f"{', '.join(g['open'][:2])} not completely routed"
        else:
            what = " and ".join(([f"{g.get('worst')} {_mm(g['short_by'])} mm short"] if g.get("short_by") else [])
                                + ([f"a member {_mm(g['long_by'])} mm too long"] if g.get("long_by") else [])) \
                or str(g.get("why") or "flagged by KiCad")
        why = str(g.get("why") or "")
        add("LENGTH_MISMATCH", level,
            f"{g['name']}: {what}" + (f" of {_mm(g['target'])}" if g.get("target") is not None else "")
            + f" (tol {_mm(g.get('tol'))}" + (f"; {why[:90]}" if why and why not in what else "") + ")",
            "meanders could not close it (groups[].why, capacity): make room beside the short runs, raise "
            "groups[].amp or nets[..].max_vias, or relax groups[].tol / set len")
    for r in c.get("pairs") or []:
        name = str(r.get("p"))                # a pair is named by its P net
        if r.get("skew_over"):
            add("PAIR_SKEW", "warning", f"{name} {_mm(r.get('skew'))} mm, {_mm(r['skew_over'])} over the limit "
                f"{_mm(r.get('max_skew'))}", "skew bumps found no room (skew_why): make room beside it, or relax "
                "diff_pairs[].skew")
        kerr = [t for t in r.get("kicad_errors") or [] if t != "skew_out_of_range"]
        if r.get("uncoupled_over") or kerr or r.get("lanes") is False:
            # a pair that could not be laid as lanes is reported even when no limit is broken (information)
            add("PAIR_UNCOUPLED", "warning" if (r.get("uncoupled_over") or kerr) else "info",
                f"{name} {r.get('coupled', 0)} % at its gap, {_mm(r.get('uncoupled'))} mm uncoupled"
                + (f" (limit {_mm(r['max_uncoupled'])})" if r.get("max_uncoupled") is not None else "")
                + (f", lanes {_mm(r['gap_seen'])} apart not {_mm(r.get('gap'))}" if r.get("gap_seen") is not None else "")
                + (f": {str(r['why'])[:80]}" if r.get("lanes") is False and r.get("why") else ""),
                "pairs[].why says what stopped the lanes (swapped pads, no fan-in, no path): fix that, or relax "
                "diff_pairs[].uncoupled")
        if r.get("kicad") is False:
            add("PAIR_NOT_KICAD", "info", f"{name} / {r.get('n')}", "a pair by its names for the router, not "
                "for KiCad (it wants ..._P/_N or ...+/-): rename the nets to get KiCad's pair gap, rules and router")
    for r in c.get("short") or []:
        if r.get("ok") is False:
            add("KEEP_SHORT_LONG", "warning", f"{r['net']} {_mm(r.get('len'))} mm, {_mm(r.get('long_by'))} over its "
                f"maximum {_mm(r.get('max'))}", "put it first in plan.order, move its parts closer together, or relax "
                "max_length / the KiCad rule")
        elif r.get("detour"):
            add("KEEP_SHORT_LONG", "info", f"{r['net']} {_mm(r.get('len'))} mm, a {_mm(r['detour'])} mm detour over "
                f"its estimate", "put it first in plan.order, move its parts closer together, or relax "
                "max_length / the KiCad rule")
    for row in (c.get("widths") or {}).get("off") or []:
        n, lay, w, want, mm_ = row[:5]
        # a class width laid thinner away from a pad is information; a plan / KiCad width rule not held, a warning
        add("WIDTH_NOT_HELD", "info" if len(row) > 5 else "warning",
            f"{n} {_mm(mm_)} mm on {lay} is {_mm(w)} mm wide, not {_mm(want)}",
            "nets[..].neck=false keeps the width; where it does not fit, open the gap or change the layer")
    for name in c.get("unread") or []:
        add("RULE_NOT_READ", "info", f"'{name}'", "the condition is outside what the router reads; KiCad still "
            "checks the rule. Declare it in the plan (groups / diff_pairs) to have it measured")
    for t, n in c.get("kicad_other") or []:
        add("KICAD_CONSTRAINT_ERRORS", "error", f"{t} {n}", "see verify(); constraint errors are never repaired "
            "by ripping copper up")
    title = {"LENGTH_MISMATCH": "length group(s) outside their window", "PAIR_SKEW": "pair(s) over their skew limit",
             "PAIR_UNCOUPLED": "pair(s) not coupled as required", "PAIR_NOT_KICAD": "pair(s) KiCad does not see as pairs",
             "KEEP_SHORT_LONG": "keep-short net(s) too long", "WIDTH_NOT_HELD": "stretch(es) not at the ruled width",
             "CONSTRAINT_GUESSED": "length group(s) guessed from net names", "RULE_NOT_READ": "KiCad rule(s) not read",
             "KICAD_CONSTRAINT_ERRORS": "KiCad constraint error(s) not predicted"}
    f = []
    for code in title:
        if code not in cases:
            continue
        n = len(cases[code])
        f.append({"severity": sev[code], "code": code, "fix": fixes[code], "message":
                  f"{n} {title[code]}: " + "; ".join(cases[code][:CONS_FIND_MAX])
                  + (f" (+{n - CONS_FIND_MAX} more in constraints)" if n > CONS_FIND_MAX else "")})
    return f


def _compact_constraints(c: dict) -> int:
    """The constraints block as the default result carries it: misses first, at most CONS_MAX rows per kind,
    0.01 mm, no per-net figures. Returns how many rows were left out."""
    cut = 0
    c.pop("nets", None)
    c.pop("tuning", None)

    def order(r):
        return {False: 0, None: 1, True: 2}.get(r.get("ok"), 1)

    def rnd(v):
        if isinstance(v, float):
            return round(v, 2)
        if isinstance(v, list):
            return [rnd(x) for x in v]
        return v
    for key in ("groups", "pairs", "short"):
        rows = c.get(key)
        if not isinstance(rows, list):
            continue
        if key == "short":
            keep = [r for r in rows if r.get("max") is not None or r.get("detour")]   # other soft ones: nothing to judge
            cut += len(rows) - len(keep)
            rows = keep
        rows = sorted(rows, key=order)
        cut += max(0, len(rows) - CONS_MAX)
        # a missed row keeps the amount (skew_over, uncoupled_over); its limit is then value - amount
        rows = [{k: rnd(v) for k, v in r.items() if k not in ("nets", "window", "branched", "kicad_errors", "est")
                 and not (k == "lanes" and v is True) and not (k == "vias" and "open" not in r and r.get("ok") is not False)
                 and not (k == "max_skew" and "skew_over" in r) and not (k == "max_uncoupled" and "uncoupled_over" in r)}
                for r in rows[:CONS_MAX]]
        if key == "pairs":
            for r in rows:                              # the reason a pair is two nets: the head of it is enough here
                for k in ("why", "skew_why"):
                    if isinstance(r.get(k), str) and len(r[k]) > 90:
                        r[k] = r[k][:87] + "..."
        if rows:
            c[key] = rows
        else:
            c.pop(key, None)
    w = c.get("widths")
    if isinstance(w, dict) and len(w.get("off") or []) > CONS_MAX:
        cut += len(w["off"]) - CONS_MAX
        w["off"] = w["off"][:CONS_MAX]
    if isinstance(c.get("kicad_other"), list):
        c["kicad_other"] = c["kicad_other"][:CONS_MAX]
    return cut


NOTES_MAX = 4             # notes[] entries in the compact result
CRITIQUE_MAX = 4          # critique[] entries (the ROUTE_QUALITY findings count them all)
SUGGEST_MAX = 3           # suggest[] patches
_FAILED_DETAIL = ("conn", "tried", "probe", "mincut_nets")


def _compact_v2(out: dict) -> None:
    """The default result of the v2 engine: what a caller acts on (counts, failed with reasons, findings with
    fixes, the patches). The engine's working diagnostics — per-stage scores and notes, judge rounds, the
    portfolio, the congestion grid of a board that routed, what it decided alone — are left out and named in
    ``more``; ``detail=true`` returns everything. A dry run and a run with failures keep what steers the next
    plan (auto, cuts, congestion)."""
    steer = bool(out.get("failed") or out.get("cuts") or out.get("dry_run"))
    left = []
    for k in ("stages", "judge", "portfolio"):
        if out.pop(k, None) is not None:
            left.append(k)
    if not steer:
        for k in ("auto", "congestion"):
            if out.pop(k, None) is not None:
                left.append(k)
    else:
        auto = out.get("auto")
        if isinstance(auto, dict):
            out["auto"] = {k: v for k, v in auto.items() if v}
    if isinstance(out.get("passes"), list):
        out["passes"] = [{k: p[k] for k in ("pass", "routed", "seconds") if k in p}
                         for p in out["passes"] if isinstance(p, dict)]
    notes = out.get("notes")
    if isinstance(notes, list) and len(notes) > NOTES_MAX:
        # (a note that says the router added vias to a length-matched net is one the caller acts on: it stays)
        first = [n for n in notes if isinstance(n, str) and "stretch on another layer" in n][:1]
        out["notes"] = first + [n for n in notes if n not in first][:NOTES_MAX - len(first)]
        left.append(f"notes (+{len(notes) - NOTES_MAX})")
    for key, cap in (("critique", CRITIQUE_MAX), ("suggest", SUGGEST_MAX)):
        v = out.get(key)
        if isinstance(v, list) and len(v) > cap:
            out[key] = v[:cap]
            left.append(f"{key} (+{len(v) - cap})")
    c = out.get("constraints")
    if isinstance(c, dict):
        n = _compact_constraints(c)
        left.append(f"constraints ({f'+{n} rows, ' if n else ''}per-net lengths)")
    cut = False
    for e in out.get("failed") or []:
        for k in _FAILED_DETAIL:
            cut = (e.pop(k, None) is not None) or cut
    if cut:
        left.append("failed[].probe / tried / mincut_nets")
    if left:
        out["more"] = "pcb_route_guided(..., detail=true) adds: " + ", ".join(left)


def _diag_images(pr, board, ov: dict, out: dict) -> list:
    """Pictures of the v2 diagnostics (design §6.3): the global sketch of a stage-limited dry run, the congestion
    heat with the cuts labelled (only when something is over capacity or failed), one crop per failure (max 6)."""
    from ..pcb.v2 import diag
    images = []
    tag = "-dryrun" if out.get("dry_run") else ""
    if ov.get("sketch"):
        path = diag.render(board, ov["sketch"], pr.out_dir / f"pcb-route-sketch{tag}.png",
                           title="global sketch: one line per connection through its planned corridor · red = over capacity")
        images.append(Image(path=path, caption="global sketch (planned corridors, no copper yet; red = a border "
                                               "with more demand than room, see cuts)"))
    if ov.get("congestion") and (out.get("cuts") or out.get("failed")):
        path = diag.render(board, ov["congestion"], pr.out_dir / f"pcb-route-congestion{tag}.png",
                           title="congestion: track demand / capacity per cell (red = full) · red lines = cuts over capacity")
        images.append(Image(path=path, caption="congestion after routing, with the cuts over capacity labelled"))
    for i, fl in enumerate((ov.get("failures") or [])[:diag.MAX_FAILURE_IMAGES]):
        path = diag.render(board, fl["prims"], pr.out_dir / f"pcb-route-failure-{i + 1}{tag}.png",
                           title=f"{fl.get('net')}: yellow = the missing connection, X = where it got stuck, orange = "
                                 f"nets in the way, red = the cut")
        images.append(Image(path=path, caption=f"why {fl.get('net')} failed (crop)"))
    return images


@tool("pcb_route_guided",
      """Guided autorouter: YOU supply the heuristics (plan), a deterministic router does the routing, KiCad's DRC
judges the result. Same plan + same board always gives the same copper. With no plan it uses built-in heuristics.
(pcb_route(mode='auto') is the other router: Freerouting, no plan. Use compare=true to see which is better.)

plan (every field optional; mm; net names may be globs; exact names win over globs):
  order: ["/XTAL*", "/USB_D?", "*"]            routing order; unlisted nets go by priority, short first
  global: {grid:0.1|"auto", via_cost:5, bend_cost:0.3, style:"45"|"90", layer_directions:{"F.Cu":"h","B.Cu":"v"},
           direction_bias:0.5, layer_cost:{"B.Cu":1.2}, layers:[..], ripup_rounds:3, time_budget_s:90, effort:1}
  nets: {"<name|glob>": {priority:0..100, width, clearance, layers:[..], prefer:[..], layer_cost:{..},
           direction:{..}, via_cost, max_vias, max_length, keep_short, avoid:[[x0,y0,x1,y1]],
           corridor:[[x,y],..], corridor_width:3, corridor_strict:false, neck:true, skip:true}}
  diff_pairs: [{p, n, gap, width}]             routed back-to-back along the same path (not gap-coupled; skew reported)
  regions: [{rect:[x0,y0,x1,y1], cost:4 | forbid:true, layers:[..], nets:[..], except:[..], name}]
Engine: 'v2' is the default (staged: power reservations, fine-pitch fan-out, global corridors, detail, repair,
polish, KiCad as judge). global.engine:"guided" selects the classic single-pass router; both honour every field
above and the same safety rules (forbid / avoid are never routed through, a ground net with 6+ pads and no pour is
left for pcb_pour and listed in skipped, a run that routes nothing leaves the board file byte-identical).
v2 also reads — every field optional, the 'guided' engine validates and ignores them:
  global: {stages:[classify,power,fanout,global,detail,repair,polish,judge], tile_mm, negotiation_iters,
           power_width_factor:2, fanout:"auto"|"none", polish:true}
  nets: {role:"plane|power|pair|bus|critical|signal", protect:true, via_budget:2 (soft; max_vias is hard),
         waypoints:[[x,y,layer?]], between:[["U3.4","U3.5"]], side_of:[{ref,side:"cw"|"ccw"}], tree:"star|chain|mst"}
  buses: [{nets:[..], order:[..], layer, spacing}]   fanout: {"<ref>": {style:"out|in|via|auto", side:"n|e|s|w", layer}}
  reserve: [{at:[x,y], for:"<net>"}]                 regions[]: + capacity_scale:0.5, no_vias:true
Unknown fields / nets / layers are rejected with the valid choices — nothing is written.

It respects: board outline + edge clearance, holes, rule-area keep-outs (antenna keep-outs too), net classes,
SMD/THT pad layers, existing and locked tracks, pours (a net with a pour only gets the pads the pour misses
connected, by short stubs/vias, after the zones are refilled).
ripup: none (default: keep existing tracks, route what is missing) | nets (rip up + re-route `nets`) | all (re-route
everything unlocked; not together with `nets`). A rip-up that then routes nothing is undone (board unchanged).
A run that closes no connection writes nothing at all (no stubs, no vias): the board file stays byte-identical.
nets: only route these nets. dry_run=true routes a scratch copy: real numbers + picture, board untouched.
compare=true: scores the existing routing against a guided re-route of the whole board (scratch copy, board untouched).

Returns {routed, total, percent (confirmed by KiCad's ratsnest), failed:[{net, from, to, reason, where:[x,y],
blocked_by}], skipped, nets:{net:[length_mm, vias]}, added, drc:{errors, new_errors, warnings, unconnected, parity},
findings (with fixes), plan_used, checkpoint, previous.restore after a rip-up} + a picture of the routes (pours
hidden). KiCad's DRC (zones refilled) is the judge: new copper it rejects is removed again and reported as unrouted.
drc.errors counts everything on the board; drc.new_errors (given when errors remain) = those this run added, 0 = all
were there before. global.time_budget_s is counted in search work, not seconds, so a slow machine gives the same copper.

Iterate: read `failed` → blocked_by says who is in the way → put the failed net earlier in `order`, give the
blocker an `avoid`/`corridor`, change via_cost / layers → run again with ripup='all' (or ripup='nets').
v2 adds diagnostics to steer by: cuts [{a, b, layer, capacity_mm, demand_mm, nets}] (where more track wants
through than fits), per failed entry cut / static_share (1 = boxed in by fixed copper → move parts; 0 = by routed
nets → order / protect), critique [{kind, net, value, limit, where}] and suggest [{why, patch}] — each patch is a
plan fragment to merge (or {"move": {ref, by}} for pcb_place); with failures or a dry run also auto {roles, buses,
widths, layer_dirs} (what it decided alone) and congestion {tile_mm, layers:{name:[[util %]]}}.
The default result is compact; `more` names what detail=true adds: stages [{name, routed, failed, score, work,
notes}], judge rounds, the portfolio, every note / critique / patch, failed[].probe / tried / mincut_nets.
The v2 loop: pcb_route_plan() → pcb_route_guided(plan={"global":{"stages":["classify","power",
"fanout","global"]}}, dry_run=true) (seconds: cuts + auto + a corridor sketch, no copper) → patch the plan → full run
(ripup="all") → read critique / suggest → pcb_route_guided(nets=[...], ripup="nets", plan=<patched>).
Examples:
  pcb_route_guided()                                                         # defaults, keep existing tracks
  pcb_route_guided(ripup="all", plan={"order":["/USB_D?","*"],"global":{"layer_directions":{"F.Cu":"h","B.Cu":"v"}}})
  pcb_route_guided(nets=["VBUS"], ripup="nets", plan={"nets":{"VBUS":{"width":0.6,"prefer":["F.Cu"]}},
                   "regions":[{"rect":[118,125,127,133],"forbid":true,"name":"under antenna"}]})
  pcb_route_guided(ripup="all", plan={"nets":{"/SPI_*":{"via_budget":1}},
                   "buses":[{"nets":["/D?"],"layer":"F.Cu"}],"fanout":{"U3":{"style":"via"}}})
  pcb_route_guided(plan={"global":{"engine":"guided"}})                      # the classic router""",
      {"project": PROJECT,
       "plan": {"type": "object", "description": "Routing heuristics (see description; pcb_route_plan returns the "
                                                 "schema and a suggested plan). Omit for built-in defaults."},
       "ripup": {"type": "string", "enum": ["none", "nets", "all"],
                 "description": "none = keep existing tracks (default); nets = rip up and re-route `nets`; "
                                "all = rip up every unlocked track first"},
       "nets": {"type": "array", "items": {"type": "string"}, "description": "Only route these nets (names or globs)"},
       "dry_run": {"type": "boolean", "description": "Route a scratch copy: full report + picture, board not modified"},
       "compare": {"type": "boolean", "description": "Score the existing routing vs. a guided re-route (implies "
                                                     "dry_run + ripup='all'); returns both scores and which is better"},
       "render": {"type": "boolean", "description": "Attach a PNG of the routes (default true)"},
       "detail": {"type": "boolean", "description": "Full engine diagnostics (default: compact result)"},
       "while_open": WHILE_OPEN},
      destructive=True, group="pcb", title="Guided autoroute")
def pcb_route_guided(project=None, plan=None, ripup="none", nets=None, dry_run=False, compare=False, render=True,
                     detail=False, while_open="refuse", _progress=None):
    pr = projmod.resolve(project)
    if plan is not None and not isinstance(plan, dict):
        raise ToolError("plan must be an object", code="BAD_PLAN", fix="pcb_route_plan() returns the schema and a suggested plan")
    out = guided.route(pr, plan, ripup=ripup, nets=nets, dry_run=dry_run, compare=compare, while_open=while_open,
                       progress=_progress)
    board = out.pop("_board", None)
    cfg = out.pop("_cfg", None)
    scratch = out.pop("_scratch", None)
    overlays = out.pop("_overlays", None)
    if overlays is None:                    # v2: diag leaves the overlay set of its last diagnosis on cfg.x
        overlays = getattr(getattr(cfg, "x", None), "overlays", None)
    images = []
    try:
        if render and board is not None:
            from ..pcb.guided import analysis as an
            if _progress:
                _progress(0.97, "drawing the routes")
            regions = [{"rect": list(r.rect), "forbid": r.forbid} for r in (cfg.regions if cfg else []) if r.idx < 1000]
            corridors = [nc.corridor["points"] for nc in (cfg.nets.values() if cfg else []) if nc.corridor]
            name = "pcb-route-guided" + ("-dryrun" if out.get("dry_run") else "")
            path = an.route_map(board, str(pr.out_dir / f"{name}.png"), failed=out.get("failed"), regions=regions,
                                corridors=corridors, title=("DRY RUN (not written)" if out.get("dry_run") else ""))
            images.append(Image(path=path, caption="routes as KiCad has them now (pours hidden; yellow X = unrouted)"
                                if not out.get("dry_run") else "proposed routes — dry run, board not modified"))
            if isinstance(overlays, dict):
                try:
                    images.extend(_diag_images(pr, board, overlays, out))
                except Exception as e:  # noqa: BLE001 — a picture must never cost the routing result
                    out.setdefault("warnings", []).append(f"diagnostic pictures not drawn: {type(e).__name__}: {e}")
    finally:
        if scratch is not None:
            shutil.rmtree(scratch.dir, ignore_errors=True)
    for e in out.get("failed") or []:
        e.pop("_a", None)
        e.pop("_b", None)
        e.pop("_frontier", None)
    cg = out.get("congestion")
    if isinstance(cg, dict) and "layers" in cg and not (out.get("failed") or out.get("cuts") or out.get("dry_run")):
        out["congestion"] = {k: v for k, v in cg.items() if k not in ("layers", "legend")}     # nothing to steer: keep the summary
    out["findings"] = _findings(out)
    if out.get("engine") == "v2" and not detail:
        _compact_v2(out)
    drc = out.get("drc")
    if drc:
        brief = {k: drc[k] for k in ("errors", "new_errors", "constraint_errors", "warnings", "unconnected", "parity")
                 if k in drc}
        if drc.get("new_top"):
            brief["new_top"] = drc["new_top"]
        elif drc.get("errors") and "new_errors" not in drc:
            brief["top_errors"] = [{k: t[k] for k in ("type", "message", "items", "at") if k in t}
                                   for t in drc.get("top", []) if t.get("severity") == "error"][:5]
        out["drc"] = brief
    if len(out.get("failed") or []) > 12:
        by_net = {}
        for e in out["failed"]:
            by_net[e["net"]] = by_net.get(e["net"], 0) + 1
        out["failed_by_net"] = by_net
        out["failed_more"] = f"{len(out['failed']) - 12} more not listed (pcb_route_guided(nets=[...]) shows a net's own failures)"
        out["failed"] = out["failed"][:12]
    out.pop("warnings", None)
    if out.get("not_written"):
        summary = f"guided route: 0/{out['total']} connections — {out['not_written']}"
    elif out.get("nothing_to_route") and not out.get("added", {}).get("tracks"):
        summary = ("nothing to route: every connection the plan selects is already made "
                   "(ripup='all' or ripup='nets' re-routes existing copper; compare=true scores a re-route first)")
    else:
        summary = (f"guided route{' (dry run)' if out.get('dry_run') else ''}: {out['routed']}/{out['total']} connections "
                   f"({out['percent']} %), " + ((f"DRC {drc['errors']} errors" + (f" ({drc['new_errors']} new)" if drc.get("new_errors") is not None else "")
                       + ", ") if drc else "")
                   + f"{out['added']['vias']} vias, {out['added']['length_mm']} mm, {out['seconds']} s")
        if out.get("compare"):
            c = out["compare"]
            def sc(d):
                return f"{d['drc_errors']} DRC errors, {d['unrouted']} unrouted, {d['vias']} vias, {d['length_mm']} mm"
            summary += f" — better: {c['better']} (existing: {sc(c['existing'])}; guided: {sc(c['guided'])})"
    return Result(out, images, summary=summary)


add_help("pcb_route_guided", """
Electrical constraints (engine 'v2'): length-matched groups, differential pairs, width / impedance classes, keep-short nets

The server never calls a model: the plan carries your heuristics, and what the plan leaves out is detected from
net names, KiCad net classes and the project's custom rules (.kicad_dru). Everything found is listed with its
source in `auto.constraints` (pcb_route_plan, and in the route result with failures / a dry run / detail=true).

Plan fields (all optional; unknown keys are rejected with the nearest valid name):
  global: {constraints: "auto" | "plan" | "report" | "off"}
      auto   (default) the plan's declarations + detection
      plan   only what the plan declares (detection is still listed in auto.constraints)
      report resolve and measure everything, enforce nothing
      off    nothing resolved, no `constraints` block
  groups: [{nets: ["/SD_D?", "/SD_CLK"], tol: 0.5, len: 42.0, amp: 2.0, name: "sd"}]
      tol    allowed spread, mm: every member within [longest - tol, longest]; with len: within len +- tol
      len    explicit target length (default: the longest member after routing)
      amp    largest meander amplitude for this group, mm (default: the project's tuning-pattern maximum, else 1)
      {nets: [...], off: true} takes those nets out of every group; groups: [] = no length groups at all
      a net can be in one group only
  diff_pairs: [{p, n, gap, width, skew: 0.15, uncoupled: 5, coupled: false, z: 90}]
      skew       max length difference inside the pair, mm
      uncoupled  max length of each net that does not run beside its partner, mm
      coupled    false = two nets, one after the other. Default: true for a plan / KiCad-rule pair and for a
                 name-detected pair whose pads are neighbours on the same parts
      z          differential impedance label, ohm (reported; never changes a width)
      diff_pairs: [] = no pairs (name detection off)
  nets: {"<name|glob>": {widths: {"F.Cu": 0.15, "In2.Cu": 0.11}, z: 50}}
      widths     width per layer for a controlled-impedance net; `width` stays the value on unlisted layers.
                 Each layer's segments are laid at that layer's width (pad escapes and the fallback
                 search included) and such a net is never necked; a stretch that is not at its layer's
                 width is listed in widths.off (WIDTH_NOT_HELD)
      z          impedance label, ohm (reported; no width is computed from it)
  already there and reused: nets[..].width, max_length, keep_short, max_vias
      max_length is a contract: a net that comes out longer loses its longest-detour connection, which is
                 reported as failed with the excess in mm (never written longer). A KiCad `length (max ..)` rule
                 is not enforced that way: the net stays routed and `short` reports long_by
      keep_short, a KiCad max-length rule, a switch node, an antenna feed: routed first, with the critical nets
Cheapest useful plans:  {"groups":[{"nets":["/SD_*"],"tol":0.5}]}    {"diff_pairs":[{"p":"/USB_D_P","n":"/USB_D_N","skew":0.15}]}

Detection when the plan is silent (precedence per net: plan > KiCad rule > net class > net name)
  KiCad rules  length (min/opt/max), skew (max), diff_pair_gap, diff_pair_uncoupled, via_count, track_width (opt),
               with conditions A.NetName == '<glob>', A.NetClass == '<class>', A.hasNetclass('<class>'),
               A.inDiffPair('<base>'), joined by ||. A rule with another condition is listed under `unread`
               (finding RULE_NOT_READ): KiCad still checks it.
  net classes  pair width / gap / via gap; a class named like an impedance (90ohm, 100R, Z50) labels its nets
  names        pairs: _P/_N, +/-, DP/DM, D+/D-, _H/_L (confidence from whether the pads are neighbours);
               length groups: SD / eMMC, QSPI, RGMII, DDR byte lanes and address, ULPI, parallel video, and
               "4+ numbered lines + one clock on one stem" — these are `mode: "report"`: measured, never enforced,
               with a CONSTRAINT_GUESSED finding that carries the plan patch to adopt them;
               keep-short: crystals, switch nodes on an inductor, antenna / RF feeds
  USB_DP / USB_DM is a pair for the router but not for KiCad (it wants _P/_N or +/-): finding PAIR_NOT_KICAD.

What a length is: exactly what KiCad's DRC reports — all track copper of the net (a tee stub counts), minus the
in-pad shortcut (a track that ends on a pad's centre counts as the straight line from the centre to where it
leaves the pad), plus the stack height of each via between the layers the net uses there, plus pad-to-die length.

Result: `constraints` (absent on a board without constraints)
  {mode, ok, missed,
   groups: [{name, n, target, min, max, worst, dev, tol, ok, src, tuned, added, hops, short_by | long_by,
             capacity, open, why}],
            tuned = members that got meanders, added = mm those add; hops = members that carry a stretch on
            another layer (two vias each) because there was no room beside them on their own; on a miss: why
            the tuner stopped and capacity = mm the free runs of the worst member could hold
   pairs:  [{p, n, len: [p, n], skew, max_skew, skew_over, gap, gap_seen, coupled, uncoupled, lanes, why, tuned,
             added, skew_why, vias: [p, n], ok, src, kicad}],
            coupled = % of the longer net that runs beside the other at the gap the lanes were laid at (+- 0.02 mm);
            uncoupled = mm of a net outside the rule's gap window (what KiCad's diff_pair_uncoupled counts);
            gap_seen = the edge gap most of the side-by-side length really runs at, when it is not the pair's gap
            lanes = false: the pair could not be laid as coupled lanes and was routed as two nets; why = the reason
            (for a pair whose copper was on the board before the run: whether that copper runs side by side at
            its gap for at least half its length — a call that only measures reports what the routing call did)
            tuned / added = skew bumps laid on the shorter net and the mm they add; skew_why = why more did not fit
   short:  [{net, len, max, ok, long_by, detour}],   detour = mm over the estimate, for a net without a maximum
   widths: {n, off: [[net, layer, width laid, width wanted, mm, "class"?]], necked},
            off = stretches not at their rule's width; rows ending in "class" are a net-class width laid thinner
            away from a pad (information); necked = mm of class-width nets laid thinner at a pad that needs it
   kicad:  {<KiCad marker type>: errors}, len_model_err, unread}
  ok: true / false, or null for information without a limit (a name-detected group, a pair without limits).
  A miss always carries the amount in mm (short_by, long_by, skew_over, uncoupled_over). The default result lists
  misses first, at most 6 rows per kind, 0.01 mm; detail=true adds every row, `nets` {net: [total, track,
  via, die, vias]} and `tuning` {net: [mm before, mm after, bumps laid, why it still misses]}.
  drc.new_errors counts everything KiCad reports as new; drc.constraint_errors says how many of those are length /
  skew / pair / via-count markers. Those never cause a rip-up: a connection is not removed for being too short.
  A run that keeps none of its copper (not_written: nothing closed, or a rip-up undone) reports the block of the
  board as it is on disk, measured without a KiCad verdict: no `kicad`, no tuned / hops / tuning.
Findings: LENGTH_MISMATCH, PAIR_SKEW, PAIR_UNCOUPLED, PAIR_NOT_KICAD, KEEP_SHORT_LONG, WIDTH_NOT_HELD,
  CONSTRAINT_GUESSED, RULE_NOT_READ, KICAD_CONSTRAINT_ERRORS.

Length tuning (groups from the plan or from a KiCad rule; a name-detected group is only measured)
  After KiCad has judged the routed board, every member shorter than its window gets meanders on its own free
  straight runs: 45-degree chamfered bumps, leg spacing max(0.6 mm or the project's tuning spacing, 3 x width,
  width + clearance), amplitude up to groups[].amp. Each bump is checked against all other copper, holes, rule
  areas, the board edge and the plan's forbid / avoid areas, and keeps its own net's pads, vias and tracks at leg
  spacing. The target is the longest member (or groups[].len); a net is never made longer than its window.
  A member that stays short at every amplitude (a track boxed in by its neighbours) gets a stretch on another
  layer the net may use: two vias on one of its straight runs, and between them either the run itself with its
  bumps or, where the run is short, one loop at right angles to it. The vias' height counts as length (KiCad
  counts it); they keep the clearance from the net's own pads; at most two such stretches per net, none when
  the two vias alone would overshoot, when nets[..].max_vias or a KiCad via_count rule leaves no two vias, or
  where a region forbids vias. groups[].hops counts these members and a note names them.
  KiCad then judges the tuned board: meanders that would add a DRC error or open a connection are taken out
  again, net by net, and tried elsewhere (3 attempts). What cannot be met is reported with the missing mm
  (short_by), the reason (why) and what the free runs could hold (capacity) - never as ok.
  Only copper of this run is tuned: tracks that were on the board before are measured, not altered (ripup: "nets"
  with those nets lets the router re-lay and tune them). Polish and the via pass leave tuned connections alone.

Differential pairs (plan, KiCad rule, net class or net names; `coupled: false` or diff_pairs: [] switches it off)
  A coupled pair is routed as one object: at each end both tracks leave their pads on a diagonal straight to their
  lanes, the lanes run width + gap apart from pad breakout to pad entry, and a layer change is two vias side by
  side across the direction of travel. The centre line is searched like any connection at width 2 x width + gap;
  every lane segment and via is then checked as copper of its own net. The gap is the pair's, but never below
  what KiCad allows between the two nets: for names KiCad does not read as a pair (USB_DP / USB_DM) that is the
  ordinary clearance, and gap_seen says so. A pair whose pads are swapped between its ends, that has no legal
  fan-in, or that finds no path at the pair's width is routed as two nets as before: `lanes: false` with `why`.
  Skew: right after a pair is laid, the shorter net gets shallow bumps (at most 0.3 mm high, away from its
  partner, nearest its pads first; lower ones where a high one does not fit) until the pair is inside
  diff_pairs[].skew / the KiCad skew rule; what does not fit is reported as skew_over with skew_why, and the
  bumps that did fit stay: a limit that cannot be met never leaves the pair worse than a looser one would (the
  shorter net is brought to within 0.003 mm of the longer at best). Bumps that would cost the pair its KiCad
  gap or uncoupled-length rule are taken out again. Repair never rips a coupled pair up for another net, polish leaves its lanes
  as laid, and when KiCad rejects one lane both are taken out and laid together again.
  Completion first: when a board does not end complete and clean and pairs that were only detected (names /
  classes, not the plan or a KiCad rule) were laid as lanes, it is routed once more with those pairs as two
  nets and the result with fewer new errors / open connections is kept (`why: "completion first: ..."`).
  A pair declared in the plan with coupled: true, or backed by a KiCad pair rule, always stays coupled.

Not in this build (reported, never silently passed): serpentines of a coupled pair as a pair (a pair that is too
short for its length group is reported), impedance computed from the stackup (z is a label), per-layer widths
for the two nets of a pair, pairs teed onto a third pad, arcs, blind / buried vias.
""")
