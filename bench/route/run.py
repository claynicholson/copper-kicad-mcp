"""Routing benchmark harness.

    python bench/route/run.py --router guided                 # every board in bench/route/corpus
    python bench/route/run.py --router guided --board stickhub --board ecc83
    python bench/route/run.py --router freerouting --runs 1 --name freerouting
    python bench/route/run.py --router v2 --opt plan='{"global":{"time_budget_s":300}}'
    python bench/route/run.py --router guided --summarize     # rebuild the table from results/<name>/*.json

Run it with KiCad's Python (it has Pillow; pcbnew itself is only used in subprocesses):
    "C:/Program Files/KiCad/10.0/bin/python.exe" bench/route/run.py --router guided

For each board the router gets a fresh copy of the UNROUTED corpus board (in bench/route/.work) and the
harness then measures, from the board file alone:

    completion %     (connections of the unrouted board - KiCad's unconnected count afterwards) / connections
    DRC              kicad-cli errors (zones refilled), and how many of them were not on the unrouted board
    length, vias     total, and against the human reference (whole board, and only over nets both finished)
    wall time        of the router call
    determinism      --runs N (default 2) routes N fresh copies and compares the copper item by item

Output: results/<name>/<board>.json (+ .png, + .human.png), results/<name>.json, results/<name>.md.
A router is a module in bench/route/routers/ (see routers/__init__.py); --router v2 loads routers/v2.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import statistics
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402

SHORT_SEG = 0.2      # mm: shorter segments are counted as "stubby"


SUITES = ("route", "cons", "all")     # route = the 14-board corpus (the default, so its table and totals stay
#                                       comparable from run to run); cons = the constraint boards (design §14.11)


def corpus_boards(suite: str = "all") -> list[dict]:
    out = []
    for d in sorted(common.CORPUS.iterdir()) if common.CORPUS.exists() else []:
        m = d / "meta.json"
        if m.exists():
            meta = json.loads(m.read_text(encoding="utf-8"))
            if suite == "all" or meta.get("suite", "route") == suite:
                out.append(meta)
    return sorted(out, key=lambda m: (m["difficulty"], m["unrouted"]["connections"], m["name"]))


def signature(cu: dict) -> str:
    """Order-independent fingerprint of the copper (1 µm resolution)."""
    items = []
    for n, lay, x1, y1, x2, y2, w, arc in cu["segments"]:
        a, b = (round(x1, 3), round(y1, 3)), (round(x2, 3), round(y2, 3))
        if b < a:
            a, b = b, a
        items.append(("t", n, lay, a, b, round(w, 3), arc))
    for n, x, y, size, drill, kind in cu["via_list"]:
        items.append(("v", n, round(x, 3), round(y, 3), round(size, 3), round(drill, 3), kind))
    items.sort()
    return hashlib.sha1(repr(items).encode()).hexdigest()[:16]


def geometry(cu: dict) -> dict:
    odd = short = 0
    for _n, _lay, x1, y1, x2, y2, _w, arc in cu["segments"]:
        ln = math.hypot(x2 - x1, y2 - y1)
        if ln < SHORT_SEG:
            short += 1
        if arc or ln < 1e-6:
            continue
        ang = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 45.0
        if min(ang, 45.0 - ang) > 0.5:
            odd += 1
    n = max(1, len(cu["segments"]))
    return {"segments": len(cu["segments"]), "odd_angle_segments": odd, "odd_angle_pct": round(100.0 * odd / n, 1),
            "short_segments": short, "short_pct": round(100.0 * short / n, 1)}


def _dominant(widths: dict):
    return float(max(widths.items(), key=lambda kv: kv[1])[0]) if widths else None


def score(meta: dict, ms: dict, drc: dict) -> dict:
    cu = ms["copper"]
    total = meta["unrouted"]["drc"]["unconnected"]
    left = drc["unconnected"]
    new = common.new_errors(drc["error_list"], meta["unrouted"].get("drc_error_list") or [])
    new_by = {}
    for e in new:
        new_by[e["type"]] = new_by.get(e["type"], 0) + 1
    out = {
        "connections": total, "unconnected": left,
        "completion_pct": round(100.0 * (total - left) / total, 1) if total else 100.0,
        "drc_errors": drc["errors"], "drc_errors_unrouted_board": meta["unrouted"]["drc"]["errors"],
        "drc_new_errors": len(new), "drc_new_by_type": new_by, "drc_new_top": new[:8],
        "length_mm": cu["length_mm"], "vias": cu["vias"], "tracks": cu["tracks"],
        "incomplete_nets": dict(sorted(ms["unconnected_by_net"].items(), key=lambda kv: (-kv[1], kv[0]))),
        "geometry": geometry(cu), "signature": signature(cu),
    }
    mst = meta["facts"].get("mst_mm") or {}
    done = [n for n in mst if n not in ms["unconnected_by_net"] and n in cu["nets"]]
    m = sum(mst[n] for n in done)
    out["length_over_mst_done_nets"] = round(sum(cu["nets"][n]["length"] for n in done) / m, 3) if m > 0 else None
    ref = meta.get("reference")
    if not ref:
        return out
    out["ref_length_mm"], out["ref_vias"] = ref["length_mm"], ref["vias"]
    out["length_ratio"] = round(cu["length_mm"] / ref["length_mm"], 3) if ref["length_mm"] else None
    out["via_ratio"] = round(cu["vias"] / ref["vias"], 3) if ref["vias"] else None
    # like for like: only nets that are complete in the reference AND in this result, and carry tracks in both
    rn, mn = ref["nets"], cu["nets"]
    common_nets = [n for n in rn if n in mn and n not in ref["unconnected_by_net"] and n not in ms["unconnected_by_net"]
                   and rn[n]["length"] > 0 and mn[n]["length"] > 0]
    lr, lm = sum(rn[n]["length"] for n in common_nets), sum(mn[n]["length"] for n in common_nets)
    vr, vm = sum(rn[n]["vias"] for n in common_nets), sum(mn[n]["vias"] for n in common_nets)
    ratios = sorted(((mn[n]["length"] / rn[n]["length"], n) for n in common_nets if rn[n]["length"] >= 5.0), reverse=True)
    thinner, wider = [], []
    for n in common_nets:
        wr, wm = _dominant(rn[n]["widths"]), _dominant(mn[n]["widths"])
        if wr and wm and wm < wr - 1e-3:
            thinner.append([n, wm, wr, rn[n]["length"]])
        elif wr and wm and wm > wr + 1e-3:
            wider.append([n, wm, wr, rn[n]["length"]])
    thinner.sort(key=lambda t: -t[3])
    out["common"] = {
        "nets": len(common_nets), "length_mm": round(lm, 1), "ref_length_mm": round(lr, 1),
        "length_ratio": round(lm / lr, 3) if lr else None, "vias": vm, "ref_vias": vr,
        "net_ratio_median": round(statistics.median(r for r, _n in ratios), 3) if ratios else None,
        "nets_over_1p5x": sum(1 for r, _n in ratios if r > 1.5),
        "worst_detours": [{"net": n, "ratio": round(r, 2), "mm": mn[n]["length"], "ref_mm": rn[n]["length"],
                           "vias": mn[n]["vias"], "ref_vias": rn[n]["vias"]} for r, n in ratios[:6]],
        "most_extra_vias": [{"net": n, "vias": mn[n]["vias"], "ref_vias": rn[n]["vias"]} for n in
                            sorted(common_nets, key=lambda n: rn[n]["vias"] - mn[n]["vias"])[:5]
                            if mn[n]["vias"] > rn[n]["vias"]],
        "nets_thinner_than_ref": len(thinner), "nets_wider_than_ref": len(wider),
        "thinner_top": [{"net": n, "width": wm, "ref_width": wr, "ref_mm": ln} for n, wm, wr, ln in thinner[:8]],
    }
    return out


def run_board(router, meta: dict, name: str, runs: int, options: dict, render: bool) -> dict:
    board = meta["name"]
    src = common.CORPUS / board
    res_dir = common.RESULTS / name
    res_dir.mkdir(parents=True, exist_ok=True)
    out = {"board": board, "router": router.NAME, "difficulty": meta["difficulty"], "note": meta["note"],
           "layers": len(meta["facts"]["layers"]), "reference_kind": meta.get("reference_kind"), "runs": []}
    first_ms = first_drc = None
    for k in range(runs):
        work = common.WORK / "runs" / name / board / f"run{k + 1}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        for f in src.iterdir():
            if f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
                shutil.copy2(f, work / f.name)
        pcb = work / f"{board}.kicad_pcb"
        run = {"run": k + 1}
        t0 = time.time()
        try:
            rep = router.route(work, pcb, dict(options))
            run["wall_s"] = round(time.time() - t0, 1)
            run["router_report"] = rep
        except Exception as e:  # noqa: BLE001 — a crashing router is a result, not a harness failure
            run["wall_s"] = round(time.time() - t0, 1)
            run["router_error"] = f"{type(e).__name__}: {getattr(e, 'message', None) or e}"[:1500]
            run["router_trace"] = traceback.format_exc()[-1500:]
        try:
            ms = common.pcb_op({"op": "measure", "pcb": str(pcb), "drawing": k == 0})
            drc = common.drc(pcb)
            run.update(score(meta, ms, drc))
            run["drc_by_type"] = drc["by_type"]
            if meta.get("constraints"):
                import cons as consmod
                run["cons"] = consmod.evaluate(meta, pcb, ms, drc, run.get("router_report"))
            if k == 0:
                first_ms, first_drc = ms, drc
        except Exception as e:  # noqa: BLE001
            run["measure_error"] = f"{type(e).__name__}: {e}"[:1500]
        out["runs"].append(run)
        print(f"  {board} run {k + 1}: " + (f"{run.get('completion_pct')} % ({run.get('unconnected')} left of "
              f"{run.get('connections')}), DRC {run.get('drc_errors')} ({run.get('drc_new_errors')} new), "
              f"{run.get('length_mm')} mm, {run.get('vias')} vias, {run['wall_s']} s"
              + (f"  ROUTER ERROR: {run['router_error'][:200]}" if run.get("router_error") else "")), flush=True)
        if k > 0 or runs == 1:
            shutil.rmtree(work, ignore_errors=True)
    sigs = [r.get("signature") for r in out["runs"]]
    out["deterministic"] = (None if len(sigs) < 2 or None in sigs else len(set(sigs)) == 1)
    if meta.get("constraints"):
        try:        # what the router detects on the unrouted board with no plan at all
            import cons as consmod
            out["detect"] = consmod.detect(src, board)
        except Exception as e:  # noqa: BLE001
            out["detect_error"] = f"{type(e).__name__}: {e}"[:400]
    out["wall_s_mean"] = round(sum(r["wall_s"] for r in out["runs"]) / len(out["runs"]), 1)
    if render and first_ms is not None:
        try:
            import render as rendermod
            r0 = out["runs"][0]
            cu = first_ms["copper"]
            title = (f"{board} — {name}: {r0.get('completion_pct')} % of {r0.get('connections')}, DRC "
                     f"{r0.get('drc_errors')} ({r0.get('drc_new_errors')} new), {cu['length_mm']} mm, {cu['vias']} vias")
            order = [l for l in reversed(meta["facts"]["layers"])]
            rendermod.draw(str(res_dir / f"{board}.png"), first_ms["bbox"], first_ms["pads"], first_ms["outline"],
                           cu["segments"], cu["via_list"], first_drc["unconnected_lines"], title, layer_order=order)
            out["render"] = f"{name}/{board}.png"
            refp = src / "reference.json"
            if refp.exists() and meta.get("reference"):
                ref = json.loads(refp.read_text(encoding="utf-8"))
                kind = "human" if meta.get("reference_kind") == "human" else "existing"
                rendermod.draw(str(res_dir / f"{board}.human.png"), first_ms["bbox"], first_ms["pads"],
                               first_ms["outline"], ref["segments"], ref["via_list"], (),
                               f"{board} — {kind} reference: {meta['reference']['length_mm']} mm, "
                               f"{meta['reference']['vias']} vias", layer_order=order)
                out["render_reference"] = f"{name}/{board}.human.png"
        except Exception as e:  # noqa: BLE001
            out["render_error"] = f"{type(e).__name__}: {e}"[:400]
    # keep the routed board of run 1 for inspection (small; inside .work)
    (res_dir / f"{board}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def _x(v, digits=2):
    return "–" if v is None else f"{v:.{digits}f}×"


def summarize(name: str) -> str:
    res_dir = common.RESULTS / name
    metas = {m["name"]: m for m in corpus_boards()}
    rows, cons_rows = [], []
    for f in sorted(res_dir.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("board") not in metas:
            continue
        if metas[d["board"]].get("suite", "route") == "cons":
            cons_rows.append(d)         # constraint boards have their own table: the corpus totals stay comparable
        else:
            rows.append(d)
    rows.sort(key=lambda d: (d["difficulty"], metas[d["board"]]["unrouted"]["connections"], d["board"]))
    lines = [f"# Routing benchmark — `{name}`", "",
             f"Router: {rows[0]['router'] if rows else '?'}", "",
             "Completion and DRC are KiCad's own verdict (kicad-cli, zones refilled) on the routed board. "
             "`new` DRC errors = errors that are not on the unrouted board. `ref` = the human routing of the "
             "original demo board (for `phone_breathalyzer`: the routing currently on that board). "
             "`common ×` compares track length only over nets that both the reference and the router finished. "
             "Wall time is the router call alone (run 1), on a machine that was running other benchmark jobs "
             "in parallel.", "",
             "| board | diff | L | conns | done % | left | DRC err (new) | length mm | ref mm | len × | common × | vias | ref vias | "
             "odd-angle % | wall s | same twice |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tot = {"conns": 0, "left": 0, "new": 0, "wall": 0.0}
    for d in rows:
        r = d["runs"][0]
        if "completion_pct" not in r:
            lines.append(f"| {d['board']} | {d['difficulty']} | {d['layers']} | – | measure failed | | | | | | | | | | "
                         f"{r['wall_s']} | |")
            continue
        det = {True: "yes", False: "NO", None: "n/a"}[d.get("deterministic")]
        err = " (router error)" if r.get("router_error") else ""
        c = r.get("common") or {}
        lines.append(
            f"| {d['board']} | {d['difficulty']} | {d['layers']} | {r['connections']} | {r['completion_pct']}{err} | "
            f"{r['unconnected']} | {r['drc_errors']} ({r['drc_new_errors']}) | {r['length_mm']} | "
            f"{r.get('ref_length_mm', '–')} | {_x(r.get('length_ratio'))} | {_x(c.get('length_ratio'))} | {r['vias']} | "
            f"{r.get('ref_vias', '–')} | {r['geometry']['odd_angle_pct']} | {r['wall_s']} | {det} |")
        tot["conns"] += r["connections"]
        tot["left"] += r["unconnected"]
        tot["new"] += r["drc_new_errors"]
        tot["wall"] += r["wall_s"]
    if tot["conns"]:
        full = sum(1 for d in rows if d["runs"][0].get("unconnected") == 0 and d["runs"][0].get("drc_new_errors") == 0)
        lines += ["", f"**Total:** {tot['conns'] - tot['left']}/{tot['conns']} connections "
                  f"({100.0 * (tot['conns'] - tot['left']) / tot['conns']:.1f} %), {tot['new']} new DRC errors, "
                  f"{full}/{len(rows)} boards fully routed and clean, {tot['wall']:.0f} s router wall time."]
    if cons_rows:
        import cons as consmod
        cons_rows.sort(key=lambda d: (d["difficulty"], metas[d["board"]]["unrouted"]["connections"], d["board"]))
        trip = []
        for d in cons_rows:
            off = common.RESULTS / f"{name}_off" / f"{d['board']}.json"
            trip.append((metas[d["board"]], d, json.loads(off.read_text(encoding="utf-8")) if off.exists() else None))
        if not rows:
            lines = [f"# Routing benchmark — `{name}`", "", f"Router: {cons_rows[0]['router']}"]
        lines += consmod.table(name, trip)
        for meta, d, off in trip:
            d["verdict"] = consmod.verdict(meta, d, off)
    md = "\n".join(lines) + "\n"
    (common.RESULTS / f"{name}.md").write_text(md, encoding="utf-8")
    (common.RESULTS / f"{name}.json").write_text(json.dumps({"name": name, "boards": rows, "cons_boards": cons_rows},
                                                            indent=1), encoding="utf-8")
    return md


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--router", required=True, help="module name in bench/route/routers/ (guided, freerouting, none, …)")
    ap.add_argument("--board", action="append", help="corpus board name (repeatable); default: all")
    ap.add_argument("--max-difficulty", type=int, default=5)
    ap.add_argument("--runs", type=int, default=2, help="routes per board (2 = determinism check)")
    ap.add_argument("--name", help="results name (default: the router name)")
    ap.add_argument("--opt", action="append", default=[], help="router option key=JSON (repeatable)")
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--summarize", action="store_true", help="only rebuild results/<name>.md/.json")
    ap.add_argument("--list", action="store_true", help="list the corpus and exit")
    ap.add_argument("--suite", "--group", dest="suite", default="route",
                    help="route (default: the 14-board corpus) | cons (alias: constraints; the constraint boards, "
                         "docs/ROUTER_V2_DESIGN.md 14.11) | all. A --board named explicitly is run whatever its suite")
    a = ap.parse_args()
    a.suite = {"constraints": "cons"}.get(a.suite, a.suite)
    if a.suite not in SUITES:
        ap.error(f"--suite must be one of {SUITES} (or 'constraints'), got {a.suite!r}")
    common.sandbox()
    name = a.name or a.router
    boards = corpus_boards("all" if a.board else a.suite)
    if a.list:
        for m in boards:
            ref = m.get("reference") or {}
            print(f"{m['name']:20s} {m.get('suite', 'route'):5s} diff {m['difficulty']}  {len(m['facts']['layers'])}L  "
                  f"{m['unrouted']['connections']:4d} conns  ref {ref.get('length_mm', '-')} mm / {ref.get('vias', '-')} vias"
                  f"  — {m['note']}")
        return 0
    if a.summarize:
        print(summarize(name))
        return 0
    import routers
    router = routers.load(a.router)
    ok, why = router.available()
    if not ok:
        print(f"router '{a.router}' is not available: {why}")
        return 2
    options = {}
    for o in a.opt:
        k, _, v = o.partition("=")
        try:
            options[k] = json.loads(v)
        except ValueError:
            options[k] = v
    if a.board:
        unknown = [b for b in a.board if b not in {m["name"] for m in boards}]
        if unknown:
            print(f"unknown board(s) {unknown}; corpus: {[m['name'] for m in boards]}")
            return 2
    todo = [m for m in boards if (not a.board or m["name"] in a.board) and m["difficulty"] <= a.max_difficulty]
    print(f"router: {router.NAME}; boards: {[m['name'] for m in todo]}; runs: {a.runs}", flush=True)
    for m in todo:
        run_board(router, m, name, a.runs, options, not a.no_render)
    print(summarize(name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
