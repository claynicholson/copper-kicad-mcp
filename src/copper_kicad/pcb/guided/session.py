"""Guided routing sessions: talk to the pcbnew worker, run the pure-Python
router, write the result in one transaction, then let KiCad judge it.

    analyze(pr)                         -> facts for a model to write a plan (read-only)
    route(pr, plan, ripup=..., ...)     -> routes, verifies with KiCad DRC, repairs or removes
                                           anything KiCad rejects, restores the board on failure

The server process never imports pcbnew; every board access is a short worker
subprocess (``workers/guided_board.py``).
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from ... import project as projmod
from ...kicad import env
from ...mcp.registry import ToolError
from .. import board as boardmod
from .. import worker
from . import model, plan as planmod, router as routermod

WORKER = "guided_board"
# DRC error types that copper we add can cause; anything else is not ours to fix
COPPER_TYPES = {"clearance", "shorting_items", "tracks_crossing", "copper_edge_clearance", "hole_clearance",
                "hole_to_hole", "via_dangling", "track_dangling", "annular_width", "via_diameter", "track_width",
                "drill_out_of_range", "items_not_allowed", "zones_intersect", "holes_co_located",
                "diff_pair_gap_out_of_range", "length_out_of_range", "track_angle", "track_segment_length",
                "connection_width", "solder_mask_bridge"}


def _run(pcb: Path, ops: list[dict], save: bool, timeout: int = 600) -> list[dict]:
    return worker.run(WORKER, {"pcb": str(pcb), "ops": ops, "save": save}, timeout=timeout)["results"]


def _need_board(pr):
    if not pr.pcb.exists():
        raise ToolError(f"{pr.pcb.name} does not exist yet", code="NO_BOARD",
                        fix="pcb_sync() to create the board from the schematic, then pcb_setup() and pcb_place(auto=true)")


def load_board(pr, ripup: str = "none", nets: list[str] | None = None, with_before: bool = False):
    """-> (model.Board, extract dict, before-stats | None, ripped count). Nothing is saved."""
    _need_board(pr)
    ops = []
    if with_before:
        ops.append({"op": "stats"})
    ops.append({"op": "ripup", "mode": ripup, "nets": nets or []})
    ops.append({"op": "extract"})
    res = _run(pr.pcb, ops, save=False)
    ex = res[-1]
    b = model.Board(ex, model.dru_minimums(pr.dir / f"{pr.name}.kicad_dru"))
    if not b.pads:
        raise ToolError("the board has no footprints yet", code="EMPTY_BOARD",
                        fix="pcb_sync() to pull the parts from the schematic, then pcb_place(auto=true)")
    return b, ex, (res[0] if with_before else None), res[-2]["removed"]


def _scratch(pr):
    """Copy the design files to a temp project so a trial run gets real KiCad numbers without touching the board."""
    tmp = Path(tempfile.mkdtemp(prefix="copper-guided-"))
    for f in pr.dir.iterdir():
        if f.is_file() and (f.suffix in (".kicad_pro", ".kicad_pcb", ".kicad_dru", ".kicad_prl")
                            or f.name in ("fp-lib-table", "sym-lib-table")):
            shutil.copy2(f, tmp / f.name)
    try:
        for sh in pr.sheets():
            rel = sh.resolve().relative_to(pr.dir.resolve())
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sh, tmp / rel)
    except (OSError, ValueError):
        pass
    return projmod.Project(tmp, pr.name)


def _raw_drc(pr) -> list[dict]:
    tmp = Path(tempfile.mkdtemp(prefix="copper-drc-"))
    try:
        rep = tmp / "drc.json"
        env.run(["pcb", "drc", "--format", "json", "--units", "mm", "--severity-all", "--refill-zones",
                 "-o", str(rep), str(pr.pcb)], timeout=600)
        if not rep.exists():
            return []
        return json.loads(rep.read_text(encoding="utf-8")).get("violations", [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _select(cfg, board, nets):
    """Restrict routing to the named nets (names / globs). Returns the selected set or None."""
    if not nets:
        return None
    names = planmod._Names(board)
    sel = set()
    for pat in nets:
        sel.update(names.match(pat, "argument nets"))
    for n, nc in cfg.nets.items():
        if n not in sel:
            nc.skip, nc.skip_reason = True, "not selected"
        elif nc.skip and not nc.explicit:
            nc.skip, nc.skip_reason = False, None      # naming a net explicitly beats the default "leave it for the pour"
    return sel


def _score(drc: dict, totals: dict) -> dict:
    return {"drc_errors": drc.get("errors"), "unrouted": drc.get("unconnected"), "vias": totals.get("vias"),
            "tracks": totals.get("tracks"), "length_mm": totals.get("track_length_mm")}


def _better(a: dict, b: dict) -> bool:
    """Is score a better than score b? fewer DRC errors, then fewer unrouted, then fewer vias, then shorter."""
    ka = (a["drc_errors"] or 0, a["unrouted"] or 0, a["vias"] or 0, a["length_mm"] or 0)
    kb = (b["drc_errors"] or 0, b["unrouted"] or 0, b["vias"] or 0, b["length_mm"] or 0)
    return ka < kb


def _protect(cfg, board, rects):
    """Keep other nets out of the neighbourhood of pads that must reach their pour / keep their thermal
    spokes. Nets with a pad of their own inside the rectangle may still enter, at a price."""
    for x0, y0, x1, y1, net in rects:
        inside = set()
        for p in board.pads:
            if p.net and x0 <= p.x <= x1 and y0 <= p.y <= y1:
                inside.add(board.net_by_id[p.net].name)
        inside.discard(net)
        r = planmod.Region()
        r.idx = 1000 + len(cfg.regions)
        r.rect, r.forbid, r.cost, r.layers, r.nets, r.exc = (x0, y0, x1, y1), True, 1.0, None, None, inside | {net}
        r.name = "protect"
        cfg.regions.append(r)
        if inside:
            r = planmod.Region()
            r.idx = 1000 + len(cfg.regions)
            r.rect, r.forbid, r.cost, r.layers, r.nets, r.exc = (x0, y0, x1, y1), False, 8.0, None, inside, set()
            r.name = "protect"
            cfg.regions.append(r)


def route(pr, plan: dict | None = None, *, ripup: str = "none", nets: list[str] | None = None,
          dry_run: bool = False, compare: bool = False, while_open: str = "refuse", progress=None) -> dict:
    """Route the board with the guided router. See tools/route_tools.py for the user-facing contract."""
    t0 = time.time()
    _need_board(pr)
    if compare:
        dry_run, ripup = True, "all"
    if ripup == "nets" and not nets:
        raise ToolError("ripup='nets' needs nets=[...] (which nets to rip up and re-route)", code="BAD_ARGUMENT",
                        fix="pass nets=['/USB_DP','/USB_DN'] — or ripup='all' to re-route everything")
    if nets is not None and (not isinstance(nets, (list, tuple)) or not all(isinstance(n, str) for n in nets)):
        raise ToolError("nets must be a list of net names / globs", code="BAD_ARGUMENT", fix="e.g. nets=['/USB_D?','VBUS']")
    if nets and compare:
        raise ToolError("compare=true scores the whole board; it cannot be combined with nets=[...]", code="BAD_ARGUMENT",
                        fix="drop nets (compare re-routes everything on a scratch copy), or use dry_run=true with "
                            "ripup='nets' to try a re-route of just those nets")
    if nets and ripup == "all":
        raise ToolError("ripup='all' with nets=[...] would delete every track on the board but only re-route "
                        f"{len(nets)} net pattern(s)", code="BAD_ARGUMENT",
                        fix="use ripup='nets' to re-route just those nets, or drop nets to re-route the whole board")
    if not dry_run:
        projmod.guard_writable(pr.pcb, while_open)

    def prog(f, msg):
        if progress:
            try:
                progress(f, msg)
            except Exception:  # noqa: BLE001
                pass
    dru = model.dru_minimums(pr.dir / f"{pr.name}.kicad_dru")
    prog(0.02, "reading the board")
    b, ex, before, _ = load_board(pr, "none", with_before=True)
    cfg = planmod.resolve(b, plan)              # validates the plan before anything is touched
    sel = _select(cfg, b, nets)
    rip_nets = sorted(sel) if (ripup == "nets" and sel) else []
    ripped = 0
    if ripup != "none":
        b, ex, _, ripped = load_board(pr, ripup, rip_nets)
        cfg = planmod.resolve(b, plan)
        sel = _select(cfg, b, nets)
    warnings = list(b.warnings) + list(cfg.warnings)

    def targeted(unc_by_net):
        return sum(v for n, v in (unc_by_net or {}).items() if n in cfg.nets and not cfg.nets[n].skip)
    total = targeted(ex.get("unconnected_by_net"))
    existing = None
    if compare:
        prog(0.04, "scoring the existing routing with KiCad DRC")
        existing = _score(boardmod.drc(pr, parity=False, refill=True), before["totals"])

    if getattr(cfg, "engine", "v2") != "guided":
        # router v2 (pcb/v2, the default): its engine searches and lets KiCad judge on a scratch copy; the write
        # stays here. plan.global.engine = 'guided' selects the classic router below
        return _route_v2(pr, cfg, ex, before, ripup=ripup, rip_nets=rip_nets, ripped=ripped, dry_run=dry_run,
                         compare=compare, existing=existing, warnings=warnings, prog=prog, t0=t0, dru=dru)

    # the budget is counted in router work units (deterministic), the wall clock is only a far safety net
    budget = cfg.time_budget * routermod.WORK_PER_SECOND
    hard = time.time() + 5 * cfg.time_budget + 60
    spent = {"work": 0, "clock_hit": False}
    passes, results = [], []
    owners = {}        # uuid -> ('t', net, layer, x1, y1, x2, y2, w, key) | ('v', net, x, y, size, drill, key)
    #                    key = (pass index, connection index): what gets removed together
    protect = []       # [(x0, y0, x1, y1, net)]
    where = {}         # protect rect -> (x, y) of the pad it was made for
    state = {"ex": ex, "pour_failed": []}

    def make(ex_):
        b_ = model.Board(ex_, dru)
        cfg_ = planmod.resolve(b_, plan)
        _select(cfg_, b_, nets)
        _protect(cfg_, b_, protect)
        b_.make_grid(cfg_.step)
        return b_, cfg_

    def run_pass(b_, cfg_, mode, f0, f1, grace=0.0):
        # pads a pour misses are cheap to connect: they always get a slice of the budget, even after a long signal pass
        left = budget - spent["work"]
        if mode != "signals":
            left = max(left, 0.2 * budget)
        if grace:
            left = max(left, grace)
        r = routermod.Router(b_, cfg_, mode=mode, deadline=hard, work=max(0.0, left),
                             progress=lambda f, m: prog(f0 + (f1 - f0) * f, m))
        res = r.run()
        spent["work"] += res["work"]
        spent["clock_hit"] = spent["clock_hit"] or res["clock_hit"]
        n_ok = sum(1 for c in res["conns"] if c.state == "routed")
        passes.append({"pass": mode, "connections": len(res["conns"]), "routed": n_ok, "seconds": res["seconds"],
                       "ripups": res["stats"]["ripups"], "_work": res["work"]})
        results.append(res)
        return res

    def commit(target, r, pre_ops=()):
        ops = list(pre_ops) + [{"op": "commit", "tracks": r["tracks"], "vias": r["vias"]}, {"op": "extract"}]
        res = _run(target.pcb, ops, save=True)
        cm = res[-2]
        pi = results.index(r)
        for u, t, ci in zip(cm["track_uuids"], r["tracks"], r["owners_t"]):
            owners[u] = ("t",) + tuple(t) + ((pi, ci),)
        for u, v, ci in zip(cm["via_uuids"], r["vias"], r["owners_v"]):
            owners[u] = ("v",) + tuple(v) + ((pi, ci),)
        state["ex"] = res[-1]

    def pour_passes(target, f0):
        """Connect what the refilled zones do not reach (two rounds: a stub can split a pour)."""
        state["pour_failed"] = []
        for rnd in range(2):
            b2, cfg2 = make(state["ex"])
            todo = [n for n, net in b2.nets.items() if net.has_pour and n in cfg2.nets and not cfg2.nets[n].skip
                    and len(b2.clusters.get(n, {})) > 1]
            if not todo or spent["work"] > 1.4 * budget:
                return
            prog(f0 + 0.04 * rnd, f"connecting {', '.join(todo[:4])} pads the pour does not reach")
            r2 = run_pass(b2, cfg2, "pours", f0 + 0.04 * rnd, f0 + 0.04 * (rnd + 1))
            state["pour_failed"] = [c for c in r2["conns"] if c.state == "failed"]
            if not r2["tracks"] and not r2["vias"]:
                return
            commit(target, r2)

    prog(0.06, "routing signal nets")
    b.make_grid(cfg.step)
    r1 = run_pass(b, cfg, "signals", 0.06, 0.6)
    has_pour_work = any(net.has_pour and len(b.clusters.get(n, {})) > 1 and not cfg.nets[n].skip
                        for n, net in b.nets.items() if n in cfg.nets)
    if not r1["tracks"] and not r1["vias"] and not ripped and not has_pour_work:
        # nothing to write
        out = {"routed": 0, "total": total, "percent": 100.0 if total == 0 else 0.0,
               "unrouted_before": before["unconnected"], "unrouted_after": before["unconnected"],
               "failed": _failed(results), "skipped": _skipped(results),
               "added": {"tracks": 0, "vias": 0, "length_mm": 0},
               "ripped_up": 0, "passes": [{k: v for k, v in p_.items() if k != "_work"} for p_ in passes],
               "plan_used": cfg.used, "warnings": warnings,
               "seconds": round(time.time() - t0, 1), "written": False, "nothing_to_route": total == 0,
               "revision": projmod.revision(pr.pcb), "_board": b, "_cfg": cfg}
        if dry_run:
            out["dry_run"] = True
        return out

    # ── write (to the board, or to a scratch copy for dry_run / compare) ──
    target = _scratch(pr) if dry_run else pr
    cp = None
    backup = None
    if not dry_run:
        cp = projmod.checkpoint(pr, "before-guided-route")
        backup = Path(tempfile.mkdtemp(prefix="copper-guided-bak-"))
        shutil.copy2(pr.pcb, backup / pr.pcb.name)
    removed = {}       # connection key -> (net, why)
    reverted = False
    try:
        prog(0.62, "writing tracks and refilling zones")
        commit(target, r1, [{"op": "ripup", "mode": ripup, "nets": rip_nets}])
        pour_passes(target, 0.66)
        # ── KiCad is the judge ────────────────────────────────────────────
        prog(0.8, "verifying with KiCad DRC")
        drc = boardmod.drc(target, parity=True, refill=True)
        for attempt in range(4):
            raw = _raw_drc(target) if drc.get("errors") else []
            # 1. new copper that takes part in a violation is removed (and reported as unrouted)
            blame = _blame(raw, owners)
            # 2. pads cut off from their pour / starved thermal reliefs: give the pad room and re-route the
            #    nets that crowd it (twice at most). If a thermal relief is still starved after that, the
            #    crowding connections go; a pad that merely cannot reach its pour is reported as unrouted.
            starved = _starved(raw, state["ex"])
            if starved and "base" not in state:
                # thermal reliefs that are starved without any of our copper (footprint / placement) are not ours
                # (judged on the board as it was BEFORE this run: by now pr.pcb already holds the new copper)
                state["base"] = _baseline_starved(pr, ripup, rip_nets, backup / pr.pcb.name if backup else None)
            starved = [s for s in starved if (s[0], s[1]) not in state.get("base", ())]
            need = ([(x, y, n, not isolated, True) for x, y, n, isolated in starved]
                    + [(c.pa.x, c.pa.y, c.net, False, False) for c in state["pour_failed"]])
            rects, redo = [], set()
            # a re-route is only started when there is budget to finish it (never rip up and then run out)
            est = max(2.0 * routermod.WORK_PER_SECOND, 1.5 * passes[0]["_work"])
            can_redo = attempt < 2 and spent["work"] + est < 1.5 * budget
            for widen in (False, True):
                for x, y, net, is_error, in_drc in need:
                    rect = _pad_rect(state["ex"], x, y, net)
                    if widen:
                        # nothing close to any starved pad explains the errors: a track can cut the pour off a
                        # little further out (KiCad then counts the spokes that end on the cut-off island as
                        # missing). Look wider, and treat every remaining starved_thermal error as ours to clear.
                        if not in_drc:
                            continue
                        rect = (rect[0] - 1.5, rect[1] - 1.5, rect[2] + 1.5, rect[3] + 1.5, net)
                        is_error = True
                    crowd = _intruders(owners, rect)
                    if not crowd:
                        continue            # not caused by copper we added
                    where[rect] = (x, y)
                    if rect in protect or not can_redo:
                        if is_error:
                            for key, n in crowd.items():
                                blame.setdefault(key, (n, f"crowds the {net} pad at ({x}, {y}): KiCad reports its "
                                                          f"thermal relief as starved with this connection routed here"))
                    elif rect not in rects:
                        rects.append(rect)
                        redo |= set(crowd.values())
                if blame or redo:
                    break
            redo -= {n for n, _w in blame.values()}
            if not blame and not redo:
                break
            removed.update(blame)
            # re-routing a few nets between everything else gives poor detours: when the first pass was
            # quick, start the signal pass again from scratch with the protected areas in place
            # (decided by board size, not by the clock: the same plan must give the same copper)
            full = bool(redo) and not removed and passes[0]["connections"] <= 250
            uu = [u for u, it in owners.items() if full or it[-1] in blame or it[1] in redo]
            for u in uu:
                del owners[u]
            prog(0.84 + 0.03 * attempt, f"repair: re-routing {len(redo)} net(s), removing {len(blame)} connection(s)")
            state["ex"] = _run(target.pcb, [{"op": "remove", "uuids": uu}, {"op": "extract"}], save=True)[1]
            if redo:
                protect.extend(rects)
                b3, cfg3 = make(state["ex"])
                gone = {n for n, _w in removed.values()}
                for n in cfg3.nets:
                    if n in gone or (not full and n not in redo):
                        cfg3.nets[n].skip, cfg3.nets[n].skip_reason = True, "not selected"
                r3 = run_pass(b3, cfg3, "signals", 0.86, 0.9, grace=2 * est)
                if r3["tracks"] or r3["vias"]:
                    commit(target, r3)
            pour_passes(target, 0.9)
            drc = boardmod.drc(target, parity=True, refill=True)
        st = _run(target.pcb, [{"op": "stats"}], save=False)[0]
        if not dry_run and ripped and not owners:
            # the rip-up bought nothing: no connection could be routed. Leave the board exactly as it was.
            shutil.copy2(backup / pr.pcb.name, pr.pcb)
            reverted = True
            st, state["ex"] = _run(pr.pcb, [{"op": "stats"}, {"op": "extract"}], save=False)
            drc = None
        elif drc.get("errors"):
            # which of the remaining errors were on the board before this run? (honest accounting)
            try:
                if dry_run:
                    base_raw = _raw_drc(pr)
                else:
                    sc0 = _scratch(pr)
                    try:
                        shutil.copy2(backup / pr.pcb.name, sc0.pcb)
                        base_raw = _raw_drc(sc0)
                    finally:
                        shutil.rmtree(sc0.dir, ignore_errors=True)
                new = _new_errors(_raw_drc(target), base_raw)
                drc["new_errors"] = len(new)
                drc["new_top"] = new[:5]
            except (ToolError, OSError, ValueError):
                pass
    except BaseException:
        if backup is not None:
            shutil.copy2(backup / pr.pcb.name, pr.pcb)      # all-or-nothing
        if dry_run:
            shutil.rmtree(target.dir, ignore_errors=True)   # no scratch copies left behind
        raise
    finally:
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)

    for p_ in passes:
        p_.pop("_work", None)
    if spent["clock_hit"]:
        warnings.append("the wall-clock safety limit stopped the router before its work budget was used up (slow or "
                        "busy machine): this result may not be reproducible — re-run, or lower global.time_budget_s")
    left = total if reverted else targeted(st["unconnected_by_net"])
    failed = _failed(results, removed, None if reverted else st["unconnected_by_net"])
    items = list(owners.values())
    touched = sorted({it[1] for it in items})
    add_t = sum(1 for it in items if it[0] == "t")
    add_v = sum(1 for it in items if it[0] == "v")
    add_len = sum(((it[5] - it[3]) ** 2 + (it[6] - it[4]) ** 2) ** 0.5 for it in items if it[0] == "t")
    fb, fcfg = make(state["ex"])
    out = {
        "routed": max(0, total - left), "total": total,
        "percent": round(100.0 * (total - left) / total, 1) if total else 100.0,
        "unrouted_before": before["unconnected"], "unrouted_after": st["unconnected"],
        "failed": failed, "skipped": _skipped(results),
        "nets": {n: st["net_stats"][n][:2] for n in touched if n in st["net_stats"]},
        "added": {"tracks": add_t, "vias": add_v, "length_mm": round(add_len, 1)},
        "ripped_up": ripped, "totals": st["totals"], "drc": drc, "passes": passes, "plan_used": cfg.used,
        "warnings": warnings, "seconds": round(time.time() - t0, 1),
        "written": (not dry_run) and not reverted and bool(ripped or add_t or add_v or removed),
        "checkpoint": cp, "revision": projmod.revision(pr.pcb), "_board": fb, "_cfg": fcfg,
    }
    pairs = _pair_report(cfg, st["net_stats"], {n for n, _w in removed.values()})
    if pairs:
        out["diff_pairs"] = pairs
    if removed:
        seen = {}
        for n, w in removed.values():
            seen[(n, w)] = seen.get((n, w), 0) + 1
        out["removed_after_drc"] = [{"net": n, "connections": k, "why": w} for (n, w), k in sorted(seen.items())]
    notes = sorted({f"{c.net}: {n}" for r in results for c in r["conns"] if c.state == "routed" for n in c.notes})
    if notes:
        out["notes"] = notes[:20]
    if protect:
        out["repairs"] = [{"gave_room_to": f"{r[4]} pad at ({round(where[r][0], 2)}, {round(where[r][1], 2)})"}
                          for r in protect]
    if not total and not add_t and not add_v:
        out["nothing_to_route"] = True
    if reverted:
        out["ripped_up"] = 0
        out["not_written"] = ("no connection could be routed with this plan, so the rip-up was undone: the board is "
                              "unchanged (see failed / findings)")
        out.pop("drc", None)
    if ripped and not dry_run and not reverted:
        out["previous"] = {"unrouted": before["unconnected"], **before["totals"],
                           "restore": f"checkpoint(action='restore', id='{cp}')"}
    if dry_run:
        out["dry_run"] = True
        out["_scratch"] = target
    if compare:
        guided = _score(drc, st["totals"])
        win = "guided" if _better(guided, existing) else ("existing" if _better(existing, guided) else "tie")
        out["compare"] = {"existing": existing, "guided": guided, "better": win,
                          "ranking": "fewer DRC errors, then fewer unrouted connections, then fewer vias, then shorter",
                          "adopt": "pcb_route_guided(ripup='all', plan=<the same plan>) writes the guided result "
                                   "(deterministic: same plan + same board = same routing)"}
    return out


def _route_v2(pr, cfg, ex, before, *, ripup, rip_nets, ripped, dry_run, compare, existing, warnings, prog, t0, dru):
    """Engine 'v2' (the default): the search and the KiCad judge run in ``pcb/v2/engine.py`` on a scratch copy
    of the project; this function owns the write. The board file is replaced once, by the scratch board KiCad
    judged, after a checkpoint — or not at all: dry_run / compare, plan-only stages, and every run that closed no
    connection (whatever copper it laid on the scratch copy: escape stubs, a rip-up). The file is then
    byte-identical to what it was."""
    from ..v2 import engine as v2engine
    scratch = _scratch(pr)
    keep_scratch = False
    try:
        if ripup != "none":
            _run(scratch.pcb, [{"op": "ripup", "mode": ripup, "nets": rip_nets}, {"op": "fill"}], save=True)
        prog(0.04, "KiCad DRC of the board before routing (new errors are counted against it)")
        baseline_raw = _raw_drc(scratch)
        out = v2engine.route(pr, cfg, cfg.x, ex, scratch=scratch, baseline_raw=baseline_raw,
                             budget_units=int(cfg.time_budget * routermod.WORK_PER_SECOND), progress=prog)
        judged = bool(out.pop("_judged", False))
        targets = out.pop("_targets", None) or {n for n, nc in cfg.nets.items() if not nc.skip}
        touched = out.pop("_touched", None) or []
        for k in ("_unconnected", "_stats", "_commit", "_ctx", "_scratch"):
            out.pop(k, None)
        add = out["added"]
        new_copper = bool(add["tracks"] or add["vias"])
        removed = out.get("removed_after_drc") or []
        cp, reverted, written = None, False, False
        # a run that closed no connection (KiCad's ratsnest of the judged scratch board) writes nothing: copper that
        # connects nothing (fan-out stubs, reserved vias) is no result, and a rip-up that bought nothing is undone
        closed = int(out.get("routed") or 0) > 0 and new_copper
        discarded = judged and not dry_run and not closed and (new_copper or bool(ripped))
        # ... and so is a rip-up the engine had nothing to commit for (every connection failed in the search: the
        # judge never ran) — the rip-up only ever happened on the scratch copy, and the report must say so
        unjudged_rip = (not judged) and not dry_run and bool(ripped)
        if unjudged_rip and not new_copper:
            reverted = True
        if judged and not dry_run:
            if discarded:
                reverted = True             # leave the board exactly as it was
                new_copper = False
                out["added"] = {"tracks": 0, "vias": 0, "length_mm": 0}
            elif new_copper:
                prog(0.96, "writing the routed board")
                cp = projmod.checkpoint(pr, "before-guided-route")
                tmp = pr.pcb.with_name(pr.pcb.name + ".copper-tmp")
                try:
                    shutil.copy2(scratch.pcb, tmp)
                    tmp.replace(pr.pcb)     # all-or-nothing: one atomic replace by the board KiCad judged
                finally:
                    tmp.unlink(missing_ok=True)
                written = True
        target = scratch if (judged and dry_run) else pr
        st, exf = _run(target.pcb, [{"op": "stats"}, {"op": "extract"}], save=False)
        drc = None
        if judged and not reverted:
            prog(0.97, "final KiCad DRC")
            drc = boardmod.drc(target, parity=True, refill=True)
            drc.update({k: v for k, v in (out.get("drc") or {}).items()
                        if k in ("new_errors", "new_top", "constraint_errors")})
            if not drc.get("new_top"):
                drc.pop("new_top", None)
        total = sum(v for n, v in (ex.get("unconnected_by_net") or {}).items() if n in targets)
        if judged:
            left = total if reverted else sum(v for n, v in st["unconnected_by_net"].items() if n in targets)
        else:
            left = total - out["routed"] if dry_run else total      # plan-only: proposed (dry run) / nothing written
        left = max(0, min(total, left))
        fb = model.Board(exf, dru)
        fb.make_grid(cfg.step)
        out.update({
            "routed": total - left, "total": total,
            "percent": round(100.0 * (total - left) / total, 1) if total else 100.0,
            "unrouted_before": before["unconnected"], "unrouted_after": st["unconnected"],
            "nets": {n: st["net_stats"][n][:2] for n in touched if n in st["net_stats"]} if judged else {},
            "ripped_up": ripped, "totals": st["totals"], "drc": drc,
            "warnings": list(warnings) + [w for w in out.get("warnings") or [] if w not in warnings],
            "seconds": round(time.time() - t0, 1), "written": written, "checkpoint": cp,
            "revision": projmod.revision(pr.pcb), "_board": fb, "_cfg": cfg,
        })
        if drc is None:
            out.pop("drc", None)
        if reverted:
            # nothing of this run is on the board: the constraints block of its (discarded) copper goes, and the
            # one of the board AS IT IS takes its place — measured on the file, with no KiCad verdict claimed
            _constraints_as_is(out, cfg, fb, st)
        pairs = _pair_report(cfg, st["net_stats"], {r["net"] for r in removed}, out.get("constraints"))
        if pairs and judged:
            out["diff_pairs"] = pairs
        if not total and not new_copper:
            out["nothing_to_route"] = True
        if reverted:
            out["ripped_up"] = 0
            if ripped:
                out["not_written"] = ("no connection could be routed with this plan, so the rip-up was undone: the "
                                      "board is unchanged (see failed / findings)")
            elif not total:
                # nothing was open: the copper proposed on top (an escape stub, a reserved via) connects nothing
                out["not_written"] = ("every connection the plan selects is already made, and the copper proposed "
                                      "on top of it connects nothing: nothing was written, the board is unchanged")
            else:
                out["not_written"] = ("no connection could be routed with this plan: nothing was written, the board "
                                      "is unchanged (see failed / findings)")
            for k in ("removed_after_drc", "repairs", "critique"):
                out.pop(k, None)            # they describe copper that was not kept (constraints: see above)
        elif not judged and not dry_run and out.get("not_committed") and (new_copper or total):
            out["not_written"] = out["not_committed"]
            if ripped:                      # plan-only stages after a rip-up: proposed copper, nothing written
                out["ripped_up"] = 0
                out["not_written"] += " — the rip-up was undone: the board is unchanged"
        if ripped and written:
            out["previous"] = {"unrouted": before["unconnected"], **before["totals"],
                               "restore": f"checkpoint(action='restore', id='{cp}')"}
        if dry_run:
            out["dry_run"] = True
            if judged:
                out["_scratch"] = scratch
                keep_scratch = True         # the caller renders from it, then deletes it
        if compare and drc is not None:
            guided = _score(drc, st["totals"])
            win = "guided" if _better(guided, existing) else ("existing" if _better(existing, guided) else "tie")
            out["compare"] = {"existing": existing, "guided": guided, "better": win,
                              "ranking": "fewer DRC errors, then fewer unrouted connections, then fewer vias, then shorter",
                              "adopt": "pcb_route_guided(ripup='all', plan=<the same plan>) writes the guided result "
                                       "(deterministic: same plan + same board = same routing)"}
        return out
    finally:
        if not keep_scratch:
            shutil.rmtree(scratch.dir, ignore_errors=True)


def _constraints_as_is(out: dict, cfg, board, stats: dict) -> None:
    """out['constraints'] of a run that kept none of its copper: the block of the unchanged board (existing copper
    only, KiCad's open connections of that file), or no block when the board has no constraints. A failure to
    measure is a warning, never the loss of the routing result."""
    out.pop("constraints", None)
    x = getattr(cfg, "x", None)
    cons = getattr(x, "cons", None)
    if cons is None or not cons.any():
        return
    try:
        from ..v2 import constraints as cmod
        rep = cmod.measure_board(board, x, (stats or {}).get("unconnected_by_net"))
    except Exception as e:  # noqa: BLE001
        out.setdefault("warnings", []).append(f"electrical constraints could not be measured ({type(e).__name__}: {e})")
        return
    if rep:
        out["constraints"] = rep


def _pad_rect(ex, x, y, net):
    """The neighbourhood of a pad that other nets must leave free so it can reach its pour: the pad's own
    extent plus room for the thermal gap and a spoke."""
    ex_, ey_ = 0.25, 0.25
    for p in ex["pads"]:
        if abs(p["x"] - x) < 0.01 and abs(p["y"] - y) < 0.01:
            for _layers, kind, g in p.get("shapes") or []:
                if kind == "box":
                    ux, uy, hw, hh = abs(g[2]), abs(g[3]), g[4], g[5]
                    ex_, ey_ = max(ex_, ux * hw + uy * hh), max(ey_, uy * hw + ux * hh)
                else:
                    for poly in g:
                        for qx, qy in poly:
                            ex_, ey_ = max(ex_, abs(qx - x)), max(ey_, abs(qy - y))
            break
    # 1.3 mm around the centre is enough for ordinary SMD pads (more only pushes neighbours into detours);
    # big pads (connector shields, THT) need the margin measured from their edge
    mx = 1.3 if ex_ <= 0.8 else min(ex_, 3.0) + 0.75
    my = 1.3 if ey_ <= 0.8 else min(ey_, 3.0) + 0.75
    return (round(x - mx, 3), round(y - my, 3), round(x + mx, 3), round(y + my, 3), net)


def _sig(v):
    return (v.get("type"), v.get("description"), tuple(sorted(str(i.get("description")) for i in v.get("items", []))))


def _new_errors(raw, base_raw) -> list[dict]:
    """DRC errors in ``raw`` that were not already in ``base_raw`` (multiset difference on type + text + items)."""
    have = {}
    for v in base_raw:
        if v.get("severity") == "error":
            have[_sig(v)] = have.get(_sig(v), 0) + 1
    out = []
    for v in raw:
        if v.get("severity") != "error":
            continue
        k = _sig(v)
        if have.get(k):
            have[k] -= 1
            continue
        items = v.get("items", [])
        e = {"type": v.get("type"), "message": v.get("description"), "items": [i.get("description") for i in items][:2]}
        # where to look: a pad / track / via rather than a zone (whose position is a corner of its outline)
        its = sorted(items, key=lambda i: str(i.get("description", "")).startswith("Zone"))
        pos = (its[0].get("pos") if its else None) or {}
        if "x" in pos:
            e["at"] = [pos["x"], pos["y"]]
        out.append(e)
    return out


def _baseline_starved(pr, ripup, rip_nets, original=None) -> set:
    """{(x, y)} of pads whose thermal relief is starved on the board *before* any new copper.
    ``original``: the untouched board file when ``pr.pcb`` has already been written to."""
    sc = _scratch(pr)
    try:
        if original is not None:
            shutil.copy2(original, sc.pcb)
        ex0 = _run(sc.pcb, [{"op": "ripup", "mode": ripup, "nets": rip_nets}, {"op": "fill"}, {"op": "extract", "fills": False}],
                   save=True)[-1]
        return {(x, y) for x, y, _n, _i in _starved(_raw_drc(sc), ex0)}
    except ToolError:
        return set()
    finally:
        shutil.rmtree(sc.dir, ignore_errors=True)


def _starved(raw, ex):
    """[(x, y, net, isolated)] pads KiCad reports with a starved thermal relief. ``isolated`` = the
    spokes are fine but end on a pour island that is cut off (an unrouted pour connection, not a crowded pad)."""
    out = []
    for v in raw:
        if v.get("type") != "starved_thermal":
            continue
        for it in v.get("items", []):
            pos = it.get("pos") or {}
            if "pad" not in (it.get("description") or "").lower() or "x" not in pos:
                continue
            for p in ex["pads"]:
                if abs(p["x"] - pos["x"]) < 0.01 and abs(p["y"] - pos["y"]) < 0.01 and p.get("net"):
                    e = (p["x"], p["y"], p["net"], "isolated island" in (v.get("description") or ""))
                    if e[:3] not in [o[:3] for o in out]:
                        out.append(e)
                    break
    return out


def _intruders(owners, rect) -> dict:
    """{connection key: net} for new copper of other nets that enters the rectangle."""
    x0, y0, x1, y1, net = rect
    out = {}
    for it in owners.values():
        n = it[1]
        if n == net or it[-1] in out:
            continue
        if it[0] == "t":
            xa, ya, xb, yb = it[3], it[4], it[5], it[6]
            if max(xa, xb) < x0 or min(xa, xb) > x1 or max(ya, yb) < y0 or min(ya, yb) > y1:
                continue
            steps = max(1, int((abs(xb - xa) + abs(yb - ya)) / 0.2))
            for k in range(steps + 1):
                x, y = xa + (xb - xa) * k / steps, ya + (yb - ya) * k / steps
                if x0 <= x <= x1 and y0 <= y <= y1:
                    out[it[-1]] = n
                    break
        elif x0 <= it[2] <= x1 and y0 <= it[3] <= y1:
            out[it[-1]] = n
    return out


def _failed(results, removed=None, unc=None):
    out = []
    removed = removed or {}
    for pi, r in enumerate(results):
        for c in r["conns"]:
            e = {"net": c.net, "from": c.pa.name, "to": c.pb.name if c.pb else "pour",
                 "_a": (c.pa.x, c.pa.y), "_b": (c.pb.x, c.pb.y) if c.pb else None}
            if c.state == "failed":
                e["reason"] = c.reason
                if c.where:
                    e["where"] = [round(c.where[0], 2), round(c.where[1], 2)]
                if c.blocked_by:
                    e["blocked_by"] = c.blocked_by[:6]
                # a later pass may have connected what an earlier one could not
                if unc is None or unc.get(c.net):
                    out.append(e)
            elif (pi, c.idx) in removed:
                e["reason"] = "routed, then removed: " + removed[(pi, c.idx)][1]
                e["where"] = [round(c.pa.x, 2), round(c.pa.y, 2)]
                out.append(e)
    # the same connection can fail in several passes: keep the last report
    uniq = {}
    for e in out:
        uniq[(e["net"], e["from"], e["to"])] = e
    return list(uniq.values())


def _skipped(results):
    out, seen = [], set()
    for r in results:
        for s in r["skipped"]:
            if s["reason"] != "not selected" and s["net"] not in seen:
                seen.add(s["net"])
                out.append(s)
    return out


def _blame(violations, owners) -> dict:
    """{connection key: (net, why)} for new copper that takes part in a KiCad DRC error."""
    out = {}
    for v in violations:
        if v.get("severity") != "error" or v.get("type") not in COPPER_TYPES:
            continue
        mine = [owners[i["uuid"]] for i in v.get("items", []) if i.get("uuid") in owners]
        if not mine:
            continue
        pos = (v.get("items") or [{}])[0].get("pos") or {}
        why = f"KiCad DRC {v.get('type')}: {v.get('description')}"
        if pos:
            why += f" at ({pos.get('x')}, {pos.get('y')})"
        it = max(mine, key=lambda m: m[-1])      # of two new items, drop the one routed later
        out.setdefault(it[-1], (it[1], why))
    return out


def _pair_report(cfg, net_stats, removed, cons=None):
    """out['diff_pairs']. ``cons``: the v2 engine's ``constraints`` block of the same result — a pair it reports
    as coupled lanes is described as that here, with the same lengths (KiCad's figure: via height included, the
    stretch inside a pad not), so the two parts of one result never contradict each other."""
    rows = {(r.get("p"), r.get("n")): r for r in (cons or {}).get("pairs") or [] if isinstance(r, dict)}
    out = []
    for n, nc in cfg.nets.items():
        if nc.dp_role != "p" or nc.skip:
            continue
        m = nc.dp_partner
        if n in removed or m in removed or n not in net_stats or m not in net_stats:
            continue
        lp, ln = net_stats[n][0], net_stats[m][0]
        coupling = "sequential: N routed right after P along P's path, spacing = clearance (not gap-controlled)"
        skew = round(abs(lp - ln), 2)
        row = rows.get((n, m))
        if row is not None and isinstance(row.get("len"), list) and len(row["len"]) == 2 and None not in row["len"]:
            lp, ln = round(row["len"][0], 2), round(row["len"][1], 2)
            skew = round(float(row.get("skew", abs(row["len"][0] - row["len"][1]))), 2)
            if row.get("lanes") is True:
                coupling = (f"coupled lanes: {row.get('coupled')} % of the pair runs side by side at "
                            f"{row.get('gap_seen', row.get('gap'))} mm (figures: constraints.pairs)")
        out.append({"p": n, "n": m, "coupling": coupling, "len_p": lp, "len_n": ln, "skew_mm": skew})
    return out


def analyze(pr, *, ripup: str = "none", nets: list[str] | None = None, heatmap_path=None, limit: int = 100) -> dict:
    """Everything a model needs to write a routing plan — read-only."""
    from . import analysis as an
    b, ex, _before, ripped = load_board(pr, ripup)
    cfg = planmod.resolve(b, None)
    b.make_grid(cfg.step)
    facts = planmod.classify(b)
    if nets:
        names = planmod._Names(b)
        keep = set()
        for pat in nets:
            keep.update(names.match(pat, "argument nets"))
        facts_view = {n: f for n, f in facts.items() if n in keep}
    else:
        facts_view = facts
    rows, more = an.net_table(b, facts_view, limit)
    cong = an.congestion(b, cfg)
    x0, y0, x1, y1 = b.bbox
    R = b.rules
    classes = {}
    for net in b.nets.values():
        if len(net.pads) >= 2:
            classes.setdefault(net.cls, [net.width, net.clearance, net.via, net.drill])
    out = {
        "board": {"size_mm": [round(x1 - x0, 2), round(y1 - y0, 2)], "origin": [round(x0, 2), round(y0, 2)],
                  "extent": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
                  "outline_closed": b.outline_closed, "layers": an.layer_stack(b), "auto_grid_mm": b.auto_step()},
        "rules": {"min_clearance": R["clearance"], "min_track": R["track_width"], "min_via": [R["via"], R["drill"]],
                  "edge_clearance": R["edge_clearance"], "hole_clearance": R["hole_clearance"],
                  "hole_to_hole": R["hole_to_hole"]},
        "net_classes": {"columns": ["track", "clearance", "via", "drill"], **classes},
        "routing": {"unrouted_connections": ex.get("unconnected"), **ex.get("totals", {}),
                    "nets_to_route": sum(1 for f in facts.values() if f["unrouted"])},
        "nets": {"columns": ["net", "kind", "pads", "unrouted", "est_mm", "class", "track", "clearance", "flags"],
                 "rows": rows},
        "pours": b.pours, "keepouts": b.keepouts,
        "congestion": {k: v for k, v in cong.items() if not k.startswith("_")},
        "suggested_plan": planmod.default_plan(b, facts),
        "plan_schema": planmod.SCHEMA_TEXT,
    }
    if more:
        out["nets"]["more"] = f"{more} more nets not listed — pass nets=['<glob>'] to see them"
    if ripup != "none":
        out["routing"]["as_if_ripped_up"] = ripped
    if b.warnings:
        out["warnings"] = b.warnings
    if heatmap_path:
        out["_heatmap"] = an.heatmap(b, cong, str(heatmap_path))
    return out
