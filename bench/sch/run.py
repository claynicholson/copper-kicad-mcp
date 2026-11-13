"""Schematic quality benchmark for the professional layout engine (copper_kicad.sch.pro).

    "<KiCad>/bin/python.exe" bench/sch/run.py                 all designs in bench/sch/corpus
    "<KiCad>/bin/python.exe" bench/sch/run.py opamp-afe rp2040-min --px-per-mm 14
    "<KiCad>/bin/python.exe" bench/sch/run.py --style labels  --no-tiles --no-before

For every design the harness
  1. copies the corpus project to bench/sch/.work/p<pid>/run/<design> (the corpus is never modified),
  2. takes an INDEPENDENT witness of the circuit: `kicad-cli sch export netlist` run directly (not through the
     package's gate) and canonicalised by this file's own parser, plus `kicad-cli sch erc`,
  3. runs the engine (engine.relayout(force=True), the code behind sch_tidy) on every leaf sheet; the engine's own
     identity gate (G0-G4) decides whether anything is written,
  4. repeats step 2 and compares: netlist must be IDENTICAL, ERC must report nothing new,
  5. exports every sheet as PNG to bench/sch/out/<design>/ (after/, before/, tiles/ = overlapping zoomed crops
     small enough to read every label), and writes report.json. bench/sch/out/summary.json collects all designs.

Exit code 1 when any design changed its netlist, gained an ERC violation, or the engine raised an unexpected error.
Everything (temp, staged copies, COPPER_HOME, KICAD_CONFIG_HOME) lives under bench/sch/.work/p<pid>, one directory
per process, so concurrent runs cannot read each other's files; results are swapped into the output directory per
design when they are complete (--out DIR gives a run its own output directory).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

_env.setup()
from copper_kicad import render as R  # noqa: E402
from copper_kicad.kicad import env  # noqa: E402
from copper_kicad.mcp.registry import ToolError  # noqa: E402
from copper_kicad.project import Project  # noqa: E402
from copper_kicad.sch import ops as O  # noqa: E402
from copper_kicad.sch.model import Schematic  # noqa: E402
from copper_kicad.sch.netlist import export_netlist  # noqa: E402
from copper_kicad.sch.pro import engine, route, symfix  # noqa: E402
from copper_kicad.sch.pro.blocks import Design  # noqa: E402

# label-only links between different parts of one frame before the bundle planner / the re-homing of island parts
# (class L; measured with bench/sch/links.py on bench/sch/out/_L_base). Designs not listed: 0
L_BEFORE = {"li-ion-powerbank": 1, "motor-driver": 4, "opamp-afe": 3, "rp2040-min": 6, "stm32-sensor": 2, "rs485-node": 6}
# class D before the rail bars (bench/sch/out/_D_base, measured with bench/sch/decaps.py): (caps tied to their IC by a
# power symbol only, the groups they form, repeated supply symbols on the decoupling of one IC rail). Not listed: 0
D_BEFORE = {"rp2040-min": (7, 3, 4), "stm32-sensor": (1, 1, 1)}
# class I before the idiom templates (bench/sch/out/_I_base, measured with bench/sch/idioms.py):
# (crystal_crossings + usbc_crossings, power_x_signal, sense_hidden). Not listed: 0
I_BEFORE = {"c3-devboard": (3, 2, 0), "li-ion-powerbank": (3, 4, 0), "motor-driver": (0, 0, 1),
            "phone-breathalyzer": (0, 2, 0), "rp2040-min": (4, 2, 0), "stm32-sensor": (2, 0, 0)}
# class P before the packed rows / the channel slack (bench/sch/out/_P_base): sheet_fill per design
P_BEFORE = {"c3-devboard": 0.417, "li-ion-powerbank": 0.641, "motor-driver": 0.547, "opamp-afe": 0.556,
            "phone-breathalyzer": 0.422, "rp2040-min": 0.454, "stm32-sensor": 0.561, "audio-amp": 0.221,
            "ne555-blinker": 0.331, "rs485-node": 0.618}
ENV_ERC = {"lib_symbol_issues", "lib_symbol_mismatch", "footprint_link_issues"}   # this machine's library tables


# ───────────────────────── independent witness (no package code) ─────────────────────────
def _cli(args: list, timeout=300) -> subprocess.CompletedProcess:
    inst = env.kicad()
    return subprocess.run([str(inst.cli), *[str(a) for a in args]], capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")


def _sx(text: str):
    """Minimal s-expression reader (strings keep their content, atoms are str)."""
    i, n = 0, len(text)
    stack, cur = [], []
    while i < n:
        c = text[i]
        if c == "(":
            stack.append(cur)
            cur = []
            i += 1
        elif c == ")":
            done = cur
            cur = stack.pop()
            cur.append(done)
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while text[j] != '"':
                if text[j] == "\\":
                    j += 1
                buf.append(text[j])
                j += 1
            cur.append("".join(buf))
            i = j + 1
        elif c.isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()":
                j += 1
            cur.append(text[i:j])
            i = j
    return cur[0] if cur else []


def _kids(node, name):
    return [x for x in node if isinstance(x, list) and x and x[0] == name]


def _val(node, name, default=""):
    k = _kids(node, name)
    return k[0][1] if k and len(k[0]) > 1 else default


def witness_netlist(root_sch: Path, tmp: Path) -> dict:
    """{'components': {ref: [value, footprint, sorted fields]}, 'nets': {name: sorted ['REF.PIN:type']}}"""
    out = tmp / "witness.net"
    out.unlink(missing_ok=True)
    r = _cli(["sch", "export", "netlist", "--format", "kicadsexpr", "-o", out, root_sch])
    if not out.exists():
        raise RuntimeError("kicad-cli netlist export failed: " + (r.stderr or r.stdout)[-400:])
    t = _sx(out.read_text(encoding="utf-8"))
    comps = {}
    for c in _kids((_kids(t, "components") or [[]])[0], "comp"):
        fields = sorted((f[1][1] if isinstance(f[1], list) else "", f[2] if len(f) > 2 else "")
                        for f in _kids((_kids(c, "fields") or [[]])[0], "field"))
        comps[_val(c, "ref")] = [_val(c, "value"), _val(c, "footprint"), fields]
    nets = {}
    for nn in _kids((_kids(t, "nets") or [[]])[0], "net"):
        nodes = sorted(f"{_val(x, 'ref')}.{_val(x, 'pin')}:{_val(x, 'pintype')}" for x in _kids(nn, "node"))
        nets[_val(nn, "name")] = nodes
    return {"components": comps, "nets": nets}


def netlist_diff(a: dict, b: dict) -> list:
    d = []
    for k in ("components", "nets"):
        for name in sorted(set(a[k]) | set(b[k])):
            if a[k].get(name) != b[k].get(name):
                d.append(f"{k[:-1]} {name}: {'missing after' if name not in b[k] else 'new after' if name not in a[k] else 'differs'}")
    return d


def witness_erc(root_sch: Path, tmp: Path) -> dict:
    """{'counts': {'severity type': n}, 'environment': n, 'items': [..]} from kicad-cli sch erc."""
    out = tmp / "witness-erc.json"
    out.unlink(missing_ok=True)
    _cli(["sch", "erc", "--format", "json", "--severity-all", "-o", out, root_sch])
    if not out.exists():
        return {"counts": {"error erc_did_not_run": 1}, "environment": 0, "items": []}
    data = json.loads(out.read_text(encoding="utf-8"))
    counts, items, envn = {}, [], 0
    for sh in data.get("sheets", []):
        for v in sh.get("violations", []):
            if v.get("type") in ENV_ERC:
                envn += 1
                continue
            k = f"{v.get('severity')} {v.get('type')}"
            counts[k] = counts.get(k, 0) + 1
            items.append({"type": v.get("type"), "severity": v.get("severity"), "sheet": sh.get("path"),
                          "what": v.get("description"),
                          "items": [i.get("description") for i in v.get("items", [])][:3]})
    return {"counts": counts, "environment": envn, "items": items}


# ───────────────────────── rendering ─────────────────────────
def render(root_sch: Path, out_dir: Path, px_per_mm: float, tiles_dir: Path | None, tile_px: int) -> list:
    """PNG of every sheet at `px_per_mm`; optional overlapping tiles of the drawn area (blank tiles skipped)."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    widths = {}
    for f in sorted(root_sch.parent.rglob("*.kicad_sch")):
        try:
            widths[f.stem] = Schematic.load(f, root_sch.stem, None).page_size()[0]
        except Exception:  # noqa: BLE001
            pass
    wmm = max(widths.values() or [297.0])
    imgs = R.schematic(root_sch, out_dir, width=int(wmm * px_per_mm), max_sheets=64)
    res = []
    if tiles_dir is not None:
        shutil.rmtree(tiles_dir, ignore_errors=True)
        tiles_dir.mkdir(parents=True)
    for im in imgs:
        p = Path(im.path)
        entry = {"png": str(p.relative_to(out_dir.parent.parent)).replace("\\", "/"), "tiles": []}
        img = Image.open(p).convert("RGB")
        entry["size"] = list(img.size)
        if tiles_dir is not None:
            W, H = img.size
            step = int(tile_px * 0.88)
            nx = max(1, -(-(W - tile_px) // step) + 1) if W > tile_px else 1
            ny = max(1, -(-(H - tile_px) // step) + 1) if H > tile_px else 1
            for iy in range(ny):
                for ix in range(nx):
                    x0 = min(ix * step, max(0, W - tile_px))
                    y0 = min(iy * step, max(0, H - tile_px))
                    t = img.crop((x0, y0, min(W, x0 + tile_px), min(H, y0 + tile_px)))
                    small = t.convert("L").resize((max(1, t.width // 8), max(1, t.height // 8)))
                    hist = small.histogram()
                    ink = sum(hist[:200]) / max(1, small.width * small.height)
                    if ink < 0.004:                           # nothing but the sheet border
                        continue
                    tp = tiles_dir / f"{p.stem}_r{iy}c{ix}.png"
                    t.save(tp)
                    entry["tiles"].append(str(tp.relative_to(out_dir.parent.parent)).replace("\\", "/"))
        res.append(entry)
    return res


# ───────────────────────── one design ─────────────────────────
def run_design(name: str, a, unseen: bool = False) -> dict:
    src = (_env.UNSEEN if unseen else _env.CORPUS) / name
    work = _env.WORK / "run" / name
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(src, work)
    pros = sorted(work.glob("*.kicad_pro"))
    pr = Project(work, pros[0].stem if pros else name)
    final = (a.out_dir / "unseen" / name) if unseen else (a.out_dir / name)
    out = _env.WORK / "out" / name          # rendered privately, then swapped in: readers never see a half-written run
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    tmp = _env.WORK / "tmp"
    rep = {"design": name, "set": "unseen" if unseen else "corpus", "sheets": [], "ok": True, "problems": []}
    t0 = time.time()
    nl0 = witness_netlist(pr.sch, tmp)
    erc0 = witness_erc(pr.sch, tmp)
    rep["size"] = {"components": len(nl0["components"]), "nets": len(nl0["nets"])}
    if not a.no_before:
        rep["before_png"] = render(pr.sch, out / "before", a.px_per_mm, None, a.tile)
    for f in O.sheet_files(pr):
        rel = str(f.relative_to(work)).replace("\\", "/")
        s = {"sheet": rel}
        ts = time.time()
        pre = None
        try:
            # the union-find pre-gate must agree with the kicad-cli gate: what the gate let through (and the engine
            # wrote) is checked again below against the sheet as it was
            sname = None if f.resolve() == pr.sch.resolve() else f.name
            sch0 = O.load_sheet(pr, sname, create=False)
            pre = (Design(sch0, export_netlist(pr)), route.signature(sch0, False), route.signature(sch0, True))
        except Exception as e:  # noqa: BLE001
            s["pregate"] = {"error": f"{type(e).__name__}: {e}"}
        try:
            # root sheet -> None, sub-sheets by file name (an ABSOLUTE path makes the engine's last-resort "labels"
            # candidate fail: it re-resolves the sheet inside a staged copy of the project)
            overprint0 = symfix.scan(sch0) if pre is not None else {}
            r = engine.relayout(pr, None if f.resolve() == pr.sch.resolve() else f.name, force=True, style=a.style,
                                route_mode=a.route, symfix=a.symfix)
            s.update(style_used=r.get("style_used"), paper=r.get("paper"), reason=r.get("reason"),
                     gate=r.get("connectivity"), gate_erc=r.get("erc"), tried=r.get("tried"),
                     blocks=r.get("blocks"), loose_parts=r.get("loose_parts"), route=r.get("route"),
                     readability=r.get("readability"), metrics=r.get("metrics"), hint=r.get("hint"),
                     idioms=r.get("idioms"), page=r.get("page"), wrote=bool(r.get("revision")),
                     notes=r.get("notes"), text=r.get("text"), symfix_applied=r.get("symfix"))
            # pin texts that overprint (stacked pins, numbers wider than their pins): measured on the sheet as it was
            # and as it is now, by symfix.scan - which level of the fix the engine ended up with is stated in words
            left = symfix.scan(O.load_sheet(pr, sname, create=False))

            def _n(d):
                return sum(len(x["long"]) + sum(len(st) for st in x["stacks"]) for x in d.values())
            s["pin_text_overprint"] = {
                "before": _n(overprint0), "after": _n(left),
                "state": ("none on this sheet" if not overprint0 else
                          "level 2 applied (derived embedded symbol; gate and ERC clean)" if r.get("symfix") and not left else
                          "level 2 applied in part; level 1 hint for the rest" if r.get("symfix") else
                          "level 1 (hint only)" if any("overprint" in h for h in [str(r.get("hint") or "")]) else
                          "overprint left WITHOUT a hint")}
            if pre is not None and r.get("revision"):
                sch1 = O.load_sheet(pr, sname, create=False)
                errs = route.verify_partition(sch1, pre[0], pre[1]) if pre[1] == pre[2] else []
                refused = [str(t.get("refused")) for t in (r.get("tried") or []) if str(t.get("refused")).startswith("pre-gate")]
                s["pregate"] = {"false_reject_of_written_sheet": errs[:6], "agrees_with_gate": not errs,
                                "candidates_refused": refused, "original_unambiguous": pre[1] == pre[2]}
                if errs:
                    rep["problems"].append(f"{rel}: pre-gate rejects a sheet the gate accepted: {errs[0][:120]}")
            if a.style in ("pro", "pro2") and r.get("style_used") not in (a.style, "unchanged"):
                rep["problems"].append(f"{rel}: engine fell back to '{r.get('style_used')}'"
                                       + (f" ({r.get('reason')})" if r.get("reason") else ""))
        except ToolError as e:
            s.update(style_used="refused", code=e.code, reason=e.message, tried=(e.data or {}).get("tried"))
            rep["problems"].append(f"{rel}: {e.code}: {e.message[:160]}")
            if e.code not in ("LAYOUT_UNSUPPORTED",):
                rep["ok"] = False
        except Exception as e:  # noqa: BLE001
            s.update(style_used="crashed", reason=f"{type(e).__name__}: {e}")
            rep["problems"].append(f"{rel}: crashed: {type(e).__name__}: {e}")
            rep["ok"] = False
        s["seconds"] = round(time.time() - ts, 2)
        rep["sheets"].append(s)
    nl1 = witness_netlist(pr.sch, tmp)
    erc1 = witness_erc(pr.sch, tmp)
    diff = netlist_diff(nl0, nl1)
    rep["netlist_identical"] = not diff
    if diff:
        rep["ok"] = False
        rep["netlist_diff"] = diff[:40]
        rep["problems"].append(f"NETLIST CHANGED ({len(diff)} differences)")
    new = {k: n - erc0["counts"].get(k, 0) for k, n in erc1["counts"].items() if n > erc0["counts"].get(k, 0)}
    rep["erc"] = {"before": erc0["counts"], "after": erc1["counts"], "new": new,
                  "environment_only": [erc0["environment"], erc1["environment"]], "after_items": erc1["items"][:30]}
    if new:
        rep["ok"] = False
        rep["problems"].append(f"NEW ERC: {new}")
    rep["after_png"] = render(pr.sch, out / "after", a.px_per_mm, None if a.no_tiles else out / "tiles", a.tile)
    for f in O.sheet_files(pr):
        shutil.copyfile(f, out / f.name)
    rep["seconds"] = round(time.time() - t0, 1)
    (out / "report.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    final.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        shutil.rmtree(final, ignore_errors=True)
        try:
            shutil.move(str(out), str(final))
            break
        except OSError:
            time.sleep(0.5)
    return rep


def acceptance(reps: list, style: str) -> dict:
    """The measurable acceptance checks of work packages 1 to 4, over every sheet of the run. Numbers, not verdicts on
    how the sheets look - that is what the PNGs and tiles are for."""
    rows = [(r["design"], s) for r in reps for s in r.get("sheets", []) if s.get("metrics")]
    n = len(rows)

    def cnt(f):
        return sum(1 for _d, s in rows if f(s["metrics"]))

    def val(key):
        return {d: s["metrics"].get(key) for d, s in rows}
    cov = [s["metrics"].get("idiom_coverage", 0.0) for _d, s in rows]
    return {
        "sheets": n,
        "netlist_identical": f"{sum(1 for r in reps if r.get('netlist_identical'))}/{len(reps)}",
        "erc_new_empty": f"{sum(1 for r in reps if not (r.get('erc') or {}).get('new'))}/{len(reps)}",
        "style_used": {d: s.get("style_used") for d, s in rows},
        f"style_used == {style}": f"{sum(1 for _d, s in rows if s.get('style_used') == style)}/{n}",
        "power_sideways == 0": f"{cnt(lambda m: m.get('power_sideways') == 0)}/{n}",
        "power_sideways": val("power_sideways"),
        # package A (power conventions): hooks / arches to a power symbol, ground above or supply below its side pin
        "power_hooks == 0": f"{cnt(lambda m: m.get('power_hooks') == 0)}/{n}",
        "power_hooks": val("power_hooks"),
        "power_wrong_side == 0": f"{cnt(lambda m: m.get('power_wrong_side') == 0)}/{n}",
        "power_wrong_side": val("power_wrong_side"),
        "power_top_stubs": val("power_top_stubs"),
        "decap_dist_mean_mm <= 10": f"{cnt(lambda m: m.get('decap_dist_mean_mm', 99) <= 10)}/{n}",
        "decap_dist_mean_mm <= 8": f"{cnt(lambda m: m.get('decap_dist_mean_mm', 99) <= 8)}/{n}",
        "decap_dist_mean_mm": val("decap_dist_mean_mm"),
        "max_junctions_on_one_power_net <= 4": f"{cnt(lambda m: m.get('max_junctions_on_one_power_net', 99) <= 4)}/{n}",
        "max_junctions_on_one_power_net": val("max_junctions_on_one_power_net"),
        # class D (decoupling): every cap visibly belongs to a supply pin (score.decap_metrics; bench/sch/decaps.py
        # compares two runs). decap_detached = caps tied to their IC by a power symbol only; decap_rail_symbol_repeats
        # = supply symbols beyond the first on the decoupling of one IC rail; decap_rows_above = cap rows over one part
        "decap_detached == 0": f"{cnt(lambda m: m.get('decap_detached') == 0)}/{n}",
        "decap_detached": val("decap_detached"),
        "decap_detached now/before class D": "%d/%d" % (
            sum(s["metrics"].get("decap_detached") or 0 for _d, s in rows), sum(D_BEFORE.get(d, (0, 0, 0))[0] for d, _s in rows)),
        "decap_groups_detached now/before class D": "%d/%d" % (
            sum(s["metrics"].get("decap_groups_detached") or 0 for _d, s in rows), sum(D_BEFORE.get(d, (0, 0, 0))[1] for d, _s in rows)),
        "decap_detached_list": {d: s["metrics"].get("decap_detached_list") for d, s in rows if s["metrics"].get("decap_detached")},
        "decap_rail_symbol_repeats": val("decap_rail_symbol_repeats"),
        "decap_rail_symbol_repeats now/before class D": "%d/%d" % (
            sum(s["metrics"].get("decap_rail_symbol_repeats") or 0 for _d, s in rows), sum(D_BEFORE.get(d, (0, 0, 0))[2] for d, _s in rows)),
        "decap_rows_above <= 2": f"{cnt(lambda m: (m.get('decap_rows_above') or 0) <= 2)}/{n}",
        "decap_rows_above": val("decap_rows_above"),
        "rail_symbol_repeats": val("rail_symbol_repeats"), "power_symbols": val("power_symbols"),
        "decap_dist_score_mm": val("decap_dist_score_mm"),
        # class I (idioms; score.idiom_metrics, bench/sch/idioms.py compares two runs and crops the drawings):
        # crossings inside the crystal and USB-receptacle drawings, a supply / ground wire over a signal wire next to
        # the pins of an IC, a regulator feedback pin hidden behind a supply symbol
        "idiom_crossings == 0": f"{cnt(lambda m: m.get('idiom_crossings') == 0)}/{n}",
        "idiom_crossings": val("idiom_crossings"),
        "idiom_crossings now/before class I": "%d/%d" % (
            sum(s["metrics"].get("idiom_crossings") or 0 for _d, s in rows), sum(I_BEFORE.get(d, (0, 0, 0))[0] for d, _s in rows)),
        "crystal_crossings": val("crystal_crossings"), "crystal_grounds_max": val("crystal_grounds_max"),
        "usbc_crossings": val("usbc_crossings"), "usbc_pair_crossings": val("usbc_pair_crossings"),
        "power_x_signal": val("power_x_signal"),
        "power_x_signal now/before class I": "%d/%d" % (
            sum(s["metrics"].get("power_x_signal") or 0 for _d, s in rows), sum(I_BEFORE.get(d, (0, 0, 0))[1] for d, _s in rows)),
        "sense_hidden == 0": f"{cnt(lambda m: m.get('sense_hidden') == 0)}/{n}",
        "sense_hidden": val("sense_hidden"),
        "idiom_crossing_list": {d: s["metrics"].get("idiom_crossing_list") for d, s in rows if s["metrics"].get("idiom_crossing_list")},
        "rail_bars": {d: (s.get("idioms") or {}).get("matches", {}).get("railbus", 0) for d, s in rows},
        "led_against_flow == 0": f"{cnt(lambda m: m.get('led_against_flow') == 0)}/{n}",
        "idiom_coverage_mean": round(sum(cov) / max(1, len(cov)), 3),
        "idiom_coverage": val("idiom_coverage"),
        "idiom_dropped": {d: (s.get("idioms") or {}).get("dropped_count") for d, s in rows},
        "pregate_false_rejects": sum(1 for _d, s in rows if not (s.get("pregate") or {}).get("agrees_with_gate", True)),
        "pregate_candidates_refused": sum(len((s.get("pregate") or {}).get("candidates_refused") or []) for _d, s in rows),
        # work package 2: in-block wiring, labels, router rules
        "wired_local_ratio >= 0.85": f"{cnt(lambda m: m.get('wired_local_ratio', 0) >= 0.85)}/{n}",
        "wired_local_ratio": val("wired_local_ratio"),
        # class L: links between parts drawn in ONE frame that are joined by nothing but a net name
        # (score.measure: label_only_links = wires missing inside frames, label_only_pairs = part pairs of one frame
        # that share signal nets of which none is wired between them)
        "label_only_links": val("label_only_links"),
        "label_only_links_total": sum(s["metrics"].get("label_only_links") or 0 for _d, s in rows),
        # the acceptance number of class L: links between DIFFERENT parts (a second pin of one part - the far side of a
        # pass-through TVS array - is not a link between two parts). Baseline before the bundle planner: 22 over the
        # ten designs (bench/sch/out/_L_base); target -70 %
        "label_only_part_links": val("label_only_part_links"),
        "label_only_part_links_total": sum(s["metrics"].get("label_only_part_links") or 0 for _d, s in rows),
        "label_only_part_links now/before class L": "%d/%d" % (
            sum(s["metrics"].get("label_only_part_links") or 0 for _d, s in rows),
            sum(L_BEFORE.get(d, 0) for d, _s in rows)),
        "label_only_link_list": val("label_only_link_list"),
        "rehomed": {d: (s.get("idioms") or {}).get("rehomed") for d, s in rows if (s.get("idioms") or {}).get("rehomed")},
        "label_only_pairs": val("label_only_pairs"),
        "label_only_pair_list": val("label_only_pair_list"),
        "bundles": {d: (s.get("idioms") or {}).get("matches", {}).get("bundle", 0) for d, s in rows},
        "labels_redundant == 0": f"{cnt(lambda m: m.get('labels_redundant') == 0)}/{n}",
        "labels": val("labels"), "labels_floor": val("labels_floor"),
        "wire_through_text_or_body == 0": f"{cnt(lambda m: m.get('wire_through_text_or_body') == 0)}/{n}",
        "crossing_near_junction == 0": f"{cnt(lambda m: m.get('crossing_near_junction') == 0)}/{n}",
        "staggered_pairs == 0": f"{cnt(lambda m: m.get('staggered_pairs') == 0)}/{n}",
        "staggered_pairs": val("staggered_pairs"),
        "wire_crossings": val("wire_crossings"), "overlaps": val("overlaps"),
        "idiom_matches": {d: (s.get("idioms") or {}).get("matches") for d, s in rows},
        # work package 3: flow ranks, page composition, cross-block wiring
        "flow_violations == 0": f"{cnt(lambda m: m.get('flow_violations') == 0)}/{n}",
        "connector_edge_ratio == 1.0": f"{cnt(lambda m: m.get('connector_edge_ratio') == 1.0)}/{n}",
        "connector_edge_ratio": val("connector_edge_ratio"),
        "sheet_fill >= 0.60": f"{cnt(lambda m: m.get('sheet_fill', 0) >= 0.60)}/{n}",
        "sheet_fill": val("sheet_fill"), "frame_fill": val("frame_fill"), "paper": val("paper"),
        # class P: the sheet is used (acceptance: sheet_fill >= 0.45 on 8 of 10 designs, none below 0.35, no third
        # of the sheet empty, no paper beyond A4 unless the frames do not fit it)
        "sheet_fill >= 0.45": f"{cnt(lambda m: m.get('sheet_fill', 0) >= 0.45)}/{n}",
        "sheet_fill >= 0.35": f"{cnt(lambda m: m.get('sheet_fill', 0) >= 0.35)}/{n}",
        "sheet_fill now/before class P": {d: [s["metrics"].get("sheet_fill"), P_BEFORE.get(d)] for d, s in rows},
        "page_fill": val("page_fill"), "page_span": val("page_span"),
        "page_empty_thirds == 0": f"{cnt(lambda m: m.get('page_empty_thirds') == 0)}/{n}",
        "page_empty_thirds": val("page_empty_thirds"),
        "page_packed": {d: (s.get("page") or {}).get("packed") for d, s in rows},
        "frame_ragged == 0": f"{cnt(lambda m: m.get('frame_ragged') == 0)}/{n}",
        "border_min_mm >= 10": f"{cnt(lambda m: m.get('border_min_mm', 0) >= 10)}/{n}",
        "border_min_mm": val("border_min_mm"),
        "off_centre <= 0.05": f"{cnt(lambda m: m.get('off_centre', 1) <= 0.05)}/{n}",
        "off_centre": val("off_centre"),
        "flag_islands == 0": f"{cnt(lambda m: m.get('flag_islands') == 0)}/{n}",
        "labels_per_signal_net <= 1.0": f"{cnt(lambda m: m.get('labels_per_signal_net', 9) <= 1.0)}/{n}",
        "labels_per_signal_net": val("labels_per_signal_net"),
        "wire_crossings <= WP2": f"{sum(1 for d, s in rows if d in WP2_CROSSINGS and s['metrics'].get('wire_crossings', 99) <= WP2_CROSSINGS[d])}/{sum(1 for d, _s in rows if d in WP2_CROSSINGS)}",
        "wire_crossings_wp2": {d: WP2_CROSSINGS.get(d) for d, _s in rows},
        "columns": {d: (s.get("page") or {}).get("columns") for d, s in rows},
        "cross_block_wires": {d: (s.get("page") or {}).get("cross_block_wires") for d, s in rows},
        "score_v2": val("score"), "score_v1": val("score_v1"), "hard": val("hard"),
        # work package 4: text, symbols, notes, title block
        "hard empty": f"{cnt(lambda m: not m.get('hard'))}/{n}",
        "overlaps == 0": f"{cnt(lambda m: m.get('overlaps') == 0)}/{n}",
        "notes_count >= 2": f"{cnt(lambda m: m.get('notes_count', 0) >= 2)}/{n}",
        "notes_count": val("notes_count"),
        "title_fields >= 3": f"{cnt(lambda m: m.get('title_fields', 0) >= 3)}/{n}",
        "title_fields": val("title_fields"),
        "score_v2 >= 90": f"{cnt(lambda m: m.get('score', 0) >= 90)}/{n}",
        "penalties": val("penalties"),
        # package C: text hygiene (copper_kicad.sch.pro.textbox: ink boxes from KiCad's drawing rules)
        "text_collisions == 0": f"{cnt(lambda m: m.get('text_collisions') == 0)}/{n}",
        "text_collisions": val("text_collisions"),
        "text_collision_list": {d: s["metrics"].get("text_collision_list") for d, s in rows if s["metrics"].get("text_collisions")},
        "lib_pin_text_collisions": val("lib_pin_text_collisions"),
        "nc_cross_on_own_pin_number": val("nc_cross_on_own_pin_number"),
        "pin_text_overprint": {d: s.get("pin_text_overprint") for d, s in rows},
        "text_solver": {d: s.get("text") for d, s in rows},
        "notes_placed": {d: s.get("notes") for d, s in rows},
        "hints": {d: s.get("hint") for d, s in rows if s.get("hint")},
    }


# wire crossings measured at the end of work package 2 (bench/sch/out/acceptance.json of that run): the reference for
# "cross-block wiring must not add crossings"
# c3 / breathalyzer / rp2040: +3 / +2 / +3 since the pass-through bars of the TVS arrays (the supply and ground pin
# cross their bar squarely, and a doubled D+ / D- pair hands its wire out across the other pair's jumper)
WP2_CROSSINGS = {"c3-devboard": 6, "li-ion-powerbank": 0, "motor-driver": 0, "opamp-afe": 3, "phone-breathalyzer": 3,
                 "rp2040-min": 7, "stm32-sensor": 2}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("designs", nargs="*", help="folder names under bench/sch/corpus (default: all)")
    ap.add_argument("--style", default="pro2", choices=["pro2", "pro", "labels"])
    ap.add_argument("--route", default="auto", choices=["auto", "off"])
    ap.add_argument("--symfix", default="auto", choices=["off", "hint", "auto"],
                    help="pin-text overprint: auto = derived embedded symbol when the gate accepts it (default here), "
                         "hint = report only (the tool's default)")
    ap.add_argument("--px-per-mm", type=float, default=11.0, help="raster resolution of the sheet PNGs")
    ap.add_argument("--tile", type=int, default=1500, help="tile edge in px (zoomed crops for reading labels)")
    ap.add_argument("--no-tiles", action="store_true")
    ap.add_argument("--no-before", action="store_true")
    ap.add_argument("--no-unseen", action="store_true", help="skip the designs of bench/sch/corpus_unseen")
    ap.add_argument("--out", default=None, help="output directory (default bench/sch/out); give every concurrent "
                    "run its own to keep the images of one run together")
    a = ap.parse_args()
    a.out_dir = Path(a.out).resolve() if a.out else _env.OUT
    def _all(root):
        return sorted(d.name for d in root.iterdir() if d.is_dir() and list(d.glob("*.kicad_sch"))) if root.is_dir() else []
    todo = [(n, False) for n in _all(_env.CORPUS)] + ([] if a.no_unseen else [(n, True) for n in _all(_env.UNSEEN)])
    if a.designs:
        todo = [(n, u) for n, u in todo if n in a.designs]
        for n in a.designs:
            if n not in [t[0] for t in todo]:
                print(f"{n}: not in the corpus")
    a.out_dir.mkdir(parents=True, exist_ok=True)
    reps = []
    for n, unseen in todo:
        try:
            rep = run_design(n, a, unseen)
        except Exception as e:  # noqa: BLE001
            rep = {"design": n, "set": "unseen" if unseen else "corpus", "ok": False,
                   "problems": [f"harness error: {type(e).__name__}: {e}"], "sheets": []}
        reps.append(rep)
        for s in rep["sheets"]:
            rb = s.get("readability") or {}
            b_, a_ = rb.get("before") or {}, rb.get("after") or {}
            print(f"{n:22s} {s['sheet'][:28]:28s} {str(s.get('style_used')):9s} {str(s.get('paper') or ''):3s} "
                  f"netlist={'SAME' if rep.get('netlist_identical') else 'DIFF'} "
                  f"erc_new={sum((rep.get('erc') or {}).get('new', {}).values())} "
                  f"score {b_.get('score')}->{a_.get('score')} labels {b_.get('labels')}->{a_.get('labels')} "
                  f"overlaps {b_.get('overlaps')}->{a_.get('overlaps')} textcol {b_.get('text_collisions')}->{a_.get('text_collisions')} cross {b_.get('wire_crossings')}->{a_.get('wire_crossings')} "
                  f"wired {b_.get('wired_local_ratio')}->{a_.get('wired_local_ratio')} {s.get('seconds')}s")
            m = s.get("metrics") or {}
            idi = s.get("idioms") or {}
            if m:
                print(f"    v2: score {m.get('score')} (v1 {m.get('score_v1')})  power_sideways {m.get('power_sideways')}  "
                      f"decap_dist {m.get('decap_dist_mean_mm')}  max_power_junctions {m.get('max_junctions_on_one_power_net')}  "
                      f"led_against_flow {m.get('led_against_flow')}  idiom_coverage {m.get('idiom_coverage')} "
                      f"({m.get('idiom_coverage_kind')})  labels {m.get('labels')}/floor {m.get('labels_floor')}  "
                      f"hard {m.get('hard')}")
            if idi:
                print(f"    idioms: {idi.get('matches')} dropped {idi.get('dropped_count')}"
                      + (f" {idi.get('dropped')[:3]}" if idi.get('dropped') else ""))
            pg = s.get("pregate") or {}
            if pg and (not pg.get("agrees_with_gate", True) or pg.get("candidates_refused")):
                print(f"    pre-gate: {pg}")
        for p in rep["problems"]:
            print(f"    ! {p}")
    (a.out_dir / "summary.json").write_text(json.dumps(reps, indent=1), encoding="utf-8")
    acc = acceptance([r for r in reps if r.get("set") != "unseen"], a.style)
    unseen = [r for r in reps if r.get("set") == "unseen"]
    if unseen:
        # designs the engine was not developed against: the same checks, reported on their own
        acc["unseen"] = acceptance(unseen, a.style)
    (a.out_dir / "acceptance.json").write_text(json.dumps(acc, indent=1), encoding="utf-8")
    print("\nacceptance (work packages 1-4 of docs/SCHEMATIC_PRO_V2_DESIGN.md):")
    for k, v in acc.items():
        if k == "unseen":
            print("  unseen designs (bench/sch/corpus_unseen):")
            for k2, v2 in v.items():
                print(f"    {k2}: {v2}")
        else:
            print(f"  {k}: {v}")
    bad = [r["design"] for r in reps if not r["ok"]]
    print(f"\n{len(reps) - len(bad)}/{len(reps)} designs passed (netlist identical, no new ERC, engine produced a layout)"
          + (f"; FAILED: {', '.join(bad)}" if bad else ""))
    print(f"images: {a.out_dir}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
