"""Constraint metrics of the routing benchmark (suite "cons", docs/ROUTER_V2_DESIGN.md §14.11).

Like everything in the harness these numbers come from the routed board file and from KiCad, not from what a
router says about itself:

    kicad_len      KiCad's own length of every constrained net (common.kicad_lengths: an always-failing length
                   rule on a throw-away copy of the routed board; the DRC message carries the "actual" length)
    groups         spread = longest - shortest member, against the rule's tolerance
    pairs          skew = |len P - len N|, vias of P and N, coupled_pct = share of the longer net's track length that
                   has a parallel segment of the other net on the same layer at the pair's gap +- 0.02 mm
    short          length against the rule's maximum
    cons_drc       KiCad's five constraint error types in the board's own DRC
    len_model_err  largest |router's length figure - kicad_len| (the one number that reads the router's report)
    detect         what the router detects with NO plan against the expected set in meta.json

``verdict`` turns them into pass / fail rows against the thresholds of §14.11. A miss is a result to report with
its amount in mm, never a reason to loosen the board's rule file.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import common

CONS_TYPES = ("length_out_of_range", "skew_out_of_range", "diff_pair_gap_out_of_range",
              "diff_pair_uncoupled_length_too_long", "too_many_vias")
GAP_TOL = 0.02            # mm
LEN_MODEL_MAX = 0.005     # mm
WALL_FACTOR, WALL_SLACK = 1.5, 10.0


def coupled_pct(segs_p: list, segs_n: list, gap: float, tol: float = GAP_TOL) -> float:
    """Share (%) of the longer net's track length that runs beside the other net: a parallel segment on the same
    layer whose copper edge is ``gap`` +- ``tol`` away. ``segs_*``: [net, layer, x1, y1, x2, y2, w, arc]."""
    def one(mine, theirs):
        total = cov = 0.0
        for _n, lay, x1, y1, x2, y2, w, _arc in mine:
            ln = math.hypot(x2 - x1, y2 - y1)
            total += ln
            if ln < 1e-9:
                continue
            ux, uy = (x2 - x1) / ln, (y2 - y1) / ln
            spans = []
            for _m, lay2, a1, b1, a2, b2, w2, _arc2 in theirs:
                l2 = math.hypot(a2 - a1, b2 - b1)
                if lay2 != lay or l2 < 1e-9 or abs(ux * (b2 - b1) - uy * (a2 - a1)) > 1e-6 * l2:
                    continue
                edge = abs((a1 - x1) * uy - (b1 - y1) * ux) - (w + w2) / 2.0
                if abs(edge - gap) > tol + 1e-9:
                    continue
                t1, t2 = (a1 - x1) * ux + (b1 - y1) * uy, (a2 - x1) * ux + (b2 - y1) * uy
                s0, s1 = max(0.0, min(t1, t2)), min(ln, max(t1, t2))
                if s1 - s0 > 1e-9:
                    spans.append((s0, s1))
            end = 0.0
            for s0, s1 in sorted(spans):
                if s1 > end:
                    cov += s1 - max(s0, end)
                    end = s1
        return total, cov
    tp, cp = one(segs_p, segs_n)
    tn, cn = one(segs_n, segs_p)
    longer = max(tp, tn)
    if longer < 1e-9:
        return 0.0
    return round(100.0 * (cp if tp >= tn else cn) / longer, 1)


def expected(meta: dict) -> dict:
    c = meta.get("constraints") or {}
    return {"groups": sorted(sorted(g["nets"]) for g in c.get("groups") or []),
            "pairs": sorted([p["p"], p["n"]] for p in c.get("pairs") or []),
            "short": sorted(s["net"] for s in c.get("short") or [])}


def detect(project_dir: Path, name: str) -> dict:
    """What the router's detection finds on the UNROUTED corpus board with no plan (names only)."""
    from copper_kicad import project as projmod
    from copper_kicad.pcb.guided import plan as planmod, session
    from copper_kicad.pcb.v2 import constraints
    pr = projmod.Project(Path(project_dir), name)
    board = session.load_board(pr)[0]
    cfg = planmod.resolve(board, None)
    cons = constraints.resolve(board, cfg, cfg.x, constraints.read_project(pr, layers=board.L))
    out = constraints.expected_set(cons)
    out["unread"] = list(cons.unread)
    out["modes"] = {g.name: [g.mode, g.src] for g in cons.groups}
    return out


def evaluate(meta: dict, pcb: Path, ms: dict, drc: dict, report: dict | None) -> dict:
    """Every constraint metric of one routed board. ``ms``: the harness's own measure of the board; ``drc``: its
    kicad-cli DRC; ``report``: the router's report (only for len_model_err)."""
    c = meta.get("constraints") or {}
    nets = sorted({n for g in c.get("groups") or [] for n in g["nets"]}
                  | {q for p in c.get("pairs") or [] for q in (p["p"], p["n"])}
                  | {s["net"] for s in c.get("short") or []})
    klen = common.kicad_lengths(pcb, nets)
    cu = ms["copper"]
    by_net = {}
    for s in cu["segments"]:
        by_net.setdefault(s[0], []).append(s)
    out = {"kicad_len": {n: klen.get(n) for n in nets}, "groups": [], "pairs": [], "short": []}
    for g in c.get("groups") or []:
        ls = [klen[n] for n in g["nets"] if klen.get(n)]
        full = len(ls) == len(g["nets"])
        spread = round(max(ls) - min(ls), 4) if ls else None
        out["groups"].append({"nets": g["nets"], "tol": g["tol"], "min": min(ls) if ls else None,
                              "max": max(ls) if ls else None, "spread": spread, "routed": len(ls),
                              "ok": bool(full and spread is not None and spread <= g["tol"] + 1e-9),
                              "over": round(spread - g["tol"], 4) if spread is not None and spread > g["tol"] else 0.0})
    for p in c.get("pairs") or []:
        lp, ln = klen.get(p["p"]), klen.get(p["n"])
        skew = round(abs(lp - ln), 4) if lp and ln else None
        vp = (cu["nets"].get(p["p"]) or {}).get("vias", 0)
        vn = (cu["nets"].get(p["n"]) or {}).get("vias", 0)
        pct = coupled_pct(by_net.get(p["p"], []), by_net.get(p["n"], []), p["gap"])
        out["pairs"].append({"p": p["p"], "n": p["n"], "len": [lp, ln], "skew": skew, "max_skew": p["skew"],
                             "skew_ok": bool(skew is not None and skew <= p["skew"] + 1e-9),
                             "vias": [vp, vn], "vias_ok": vp == vn,
                             "coupled_pct": pct, "min_coupled_pct": p.get("coupled_pct"),
                             "coupled_ok": p.get("coupled_pct") is None or pct >= p["coupled_pct"]})
    for s in c.get("short") or []:
        ln = klen.get(s["net"])
        out["short"].append({"net": s["net"], "len": ln, "max": s["max"], "ok": bool(ln and ln <= s["max"] + 1e-9),
                             "over": round(ln - s["max"], 4) if ln and ln > s["max"] else 0.0})
    out["cons_drc"] = {k.split(":", 1)[1]: v for k, v in (drc.get("by_type") or {}).items()
                       if k.startswith("error:") and k.split(":", 1)[1] in CONS_TYPES}
    rn = ((report or {}).get("constraints") or {}).get("nets") or {}
    errs = [abs(rn[n][0] - klen[n]) for n in nets if n in rn and rn[n] and rn[n][0] is not None and klen.get(n)]
    out["len_model_err"] = round(max(errs), 4) if errs else None
    out["len_model_nets"] = len(errs)
    return out


def verdict(meta: dict, d: dict, off: dict | None = None) -> list[dict]:
    """Pass / fail rows of one board result (results/<name>/<board>.json) against §14.11.
    ``off``: the result of the same board with plan.global.constraints = "off", for the wall-time and
    completion comparison."""
    rows = []

    def row(metric, value, limit, ok, note=""):
        rows.append({"metric": metric, "value": value, "limit": limit, "ok": bool(ok), "note": note})
    r = d["runs"][0]
    cons = r.get("cons") or {}
    want = 100.0
    if off is not None and meta["name"] == "cons_mixed":
        want = off["runs"][0].get("completion_pct", 100.0)
    row("done %", r.get("completion_pct"), f">= {want}", (r.get("completion_pct") or 0) >= want)
    row("new DRC errors (constraint types included)", r.get("drc_new_errors"), 0, r.get("drc_new_errors") == 0,
        ", ".join(f"{k} x{v}" for k, v in sorted((r.get("drc_new_by_type") or {}).items())))
    row("same twice", d.get("deterministic"), True, d.get("deterministic") is True)
    det = d.get("detect")
    if det is not None:
        exp = expected(meta)
        miss = {k: [x for x in exp[k] if x not in det.get(k, [])] for k in exp}
        extra = {k: [x for x in det.get(k, []) if x not in exp[k]] for k in exp}
        ok = not any(miss.values()) and not any(extra.values()) and not det.get("unread")
        row("detection with no plan", "exact" if ok else "differs", "nothing missing, nothing extra", ok,
            "" if ok else f"missing {json.dumps({k: v for k, v in miss.items() if v})} extra "
                          f"{json.dumps({k: v for k, v in extra.items() if v})} unread {det.get('unread')}")
    if cons.get("len_model_err") is not None:
        row("len_model_err mm", cons["len_model_err"], f"<= {LEN_MODEL_MAX}", cons["len_model_err"] <= LEN_MODEL_MAX,
            f"over {cons.get('len_model_nets')} nets")
    else:
        row("len_model_err mm", None, f"<= {LEN_MODEL_MAX}", False, "the router reported no lengths to compare")
    for g in cons.get("groups") or []:
        row(f"group spread mm ({len(g['nets'])} nets, {g['nets'][0]} ..)", g["spread"], f"<= {g['tol']}", g["ok"],
            f"{g['over']} mm over" if g["over"] else ("not every member is routed" if not g["ok"] else ""))
    for p in cons.get("pairs") or []:
        tag = f"{p['p']} / {p['n']}"
        row(f"pair skew mm ({tag})", p["skew"], f"<= {p['max_skew']}", p["skew_ok"],
            f"{round(p['skew'] - p['max_skew'], 4)} mm over" if p["skew"] is not None and not p["skew_ok"] else "")
        if p.get("min_coupled_pct") is not None:
            row(f"coupled % ({tag})", p["coupled_pct"], f">= {p['min_coupled_pct']}", p["coupled_ok"])
        row(f"vias P = N ({tag})", f"{p['vias'][0]} / {p['vias'][1]}", "equal", p["vias_ok"])
    for s in cons.get("short") or []:
        row(f"keep-short mm ({s['net']})", s["len"], f"<= {s['max']}", s["ok"], f"{s['over']} mm over" if s["over"] else "")
    if off is not None:
        lim = round(WALL_FACTOR * off["runs"][0]["wall_s"] + WALL_SLACK, 1)
        row("wall s (limit from the constraints:off run)", r.get("wall_s"), f"<= {lim}", r.get("wall_s", 1e9) <= lim,
            f"off run {off['runs'][0]['wall_s']} s")
    return rows


def table(name: str, rows: list[tuple]) -> list[str]:
    """Markdown for results/<name>.md. rows: [(meta, board result dict, off result | None)]."""
    lines = ["", "## Constraint boards (suite `cons`)", "",
             "Lengths are KiCad's own (an always-failing length rule on a copy of the routed board). A board passes "
             "only when every row of its list holds; a miss is given with its amount.", "",
             "| board | done % | new DRC (constraint types) | same twice | detection | len model err mm | "
             "group spread mm (tol) | pair skew mm (max) | coupled % (min) | vias P/N | keep-short mm (max) | wall s "
             "(off) | verdict |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    detail = []
    for meta, d, off in rows:
        r = d["runs"][0]
        cons = r.get("cons") or {}
        v = verdict(meta, d, off)
        bad = [x for x in v if not x["ok"]]
        cd = cons.get("cons_drc") or {}
        det = next((x for x in v if x["metric"].startswith("detection")), None)
        lines.append(
            f"| {meta['name']} | {r.get('completion_pct')} | {r.get('drc_new_errors')} "
            f"({', '.join(f'{k} x{n}' for k, n in sorted(cd.items())) or 'none'}) | "
            f"{ {True: 'yes', False: 'NO', None: 'n/a'}[d.get('deterministic')] } | {det['value'] if det else 'n/a'} | "
            f"{cons.get('len_model_err')} | "
            + ("; ".join(f"{g['spread']} ({g['tol']})" for g in cons.get('groups') or []) or "–") + " | "
            + ("; ".join(f"{p['skew']} ({p['max_skew']})" for p in cons.get('pairs') or []) or "–") + " | "
            + ("; ".join(f"{p['coupled_pct']} ({p['min_coupled_pct']})" for p in cons.get('pairs') or []) or "–") + " | "
            + ("; ".join(f"{p['vias'][0]}/{p['vias'][1]}" for p in cons.get('pairs') or []) or "–") + " | "
            + ("; ".join(f"{s['len']} ({s['max']})" for s in cons.get('short') or []) or "–") + " | "
            + f"{r.get('wall_s')} ({off['runs'][0]['wall_s'] if off else '–'}) | "
            + ("PASS" if not bad else f"FAIL ({len(bad)} of {len(v)})") + " |")
        if bad:
            detail.append(f"**{meta['name']}** fails:")
            for x in bad:
                detail.append(f"- {x['metric']}: {x['value']} (needs {x['limit']})" + (f" — {x['note']}" if x["note"] else ""))
            detail.append("")
        kl = cons.get("kicad_len") or {}
        if kl:
            detail.append(f"{meta['name']} — KiCad lengths, mm: " + ", ".join(f"{n} {kl[n]}" for n in sorted(kl)))
            detail.append("")
    if detail:
        lines += [""] + detail
    return lines
