"""Router v2, electrical constraints end to end: the constraint bench boards routed through
``guided.session.route`` with KiCad 10, and KiCad's own DRC — each board's own rule file included — asked about
the written file (design §14.11). These are the cases an independent verifier found broken after step C; before
this file no test routed ``cons_mixed`` at all, so the gate passed while that board failed.

    cons_mixed      the QSPI group (skew 0.3 mm) has members boxed in by their neighbours: they take a stretch
                    on another layer through two vias (v2/tune.py "hop"); every rule of the board is met
    cons_bus_match  an explicit group length above the natural one (len 45, tol 0.3) on a half-empty board
    cons_usb_pair   the tightest skew limit the plan accepts (0.005 mm) never ends worse than a looser one
    cons_mixed      per-layer widths (nets[..].widths) reach every layer, the unlisted one included
    cons_usb_pair / cons_pairs_cross   a call that only measures reports what the routing call reported, and
                    ``diff_pairs`` says the same as ``constraints.pairs``

Every figure asserted here is KiCad's (``common.drc``, ``common.kicad_lengths``) or read from the written board
file, not the router's word. Skipped without KiCad 10 or the bench boards.

Run:  python tests/test_v2_consboards.py             all
      python tests/test_v2_consboards.py test_name   one test
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench" / "route"))
import common  # noqa: E402  (bench/route/common.py)

common.sandbox()       # KICAD_CONFIG_HOME, COPPER_HOME and the temp dir -> bench/route/.work, before copper_kicad loads
import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.v2 import constraints as C  # noqa: E402

_TMP = None
QSPI = ["/Q_CLK", "/Q_IO0", "/Q_IO1", "/Q_IO2", "/Q_IO3"]
SD = ["/SD_CLK", "/SD_D0", "/SD_D1", "/SD_D2", "/SD_D3"]
USB = ("/USB_D_P", "/USB_D_N")


def have_kicad(*boards) -> bool:
    return common.KICAD_CLI.exists() and common.KICAD_PY.exists() and all(
        (common.CORPUS / b / f"{b}.kicad_pcb").exists() for b in boards)


def project(board: str, tag: str):
    from copper_kicad import project as projmod
    global _TMP
    if _TMP is None:
        _TMP = Path(tempfile.mkdtemp(prefix="cb-", dir=str(common.WORK)))
    work = _TMP / f"{board}-{tag}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for f in (common.CORPUS / board).iterdir():
        if f.is_file() and f.name not in ("meta.json", "reference.json"):
            shutil.copy2(f, work / f.name)
    return projmod.Project(work, board)


def merged(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = merged(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def route(board: str, tag: str, patch: dict | None = None, **kw):
    """Route a scratch copy with the plan ``pcb_route_plan`` suggests (+ ``patch``). -> (project, result)."""
    from copper_kicad.pcb import guided
    from copper_kicad.pcb.v2 import planx
    pr = project(board, tag)
    plan = planx.v2_plan(guided.analyze(pr, ripup="none")["suggested_plan"])
    if patch:
        plan = merged(plan, patch)
    out = guided.route(pr, plan, **kw)
    return pr, out, plan


def copper_of(pr):
    from copper_kicad.pcb.v2 import kio
    ex = kio.extract(pr)
    return (sorted(tuple(round(v, 6) if isinstance(v, float) else v for v in t[:7]) for t in ex["tracks"]),
            sorted(tuple(round(v, 6) if isinstance(v, float) else v for v in t[:5]) for t in ex["vias"]))


def files_of(pr) -> dict:
    return {f.name: f.read_bytes() for f in sorted(pr.dir.iterdir()) if f.is_file() and f.suffix.startswith(".kicad")}


def clean(pr) -> dict:
    """KiCad's own DRC of the written board: no error of any kind (the five constraint types included), nothing
    unconnected."""
    d = common.drc(pr.pcb)
    assert d["errors"] == 0 and d["unconnected"] == 0, d.get("by_type")
    assert not any(t in str(d.get("by_type")) for t in C.KICAD_TYPES), d.get("by_type")
    return d


def skip(*boards) -> bool:
    if have_kicad(*boards):
        return False
    print("  (skipped: needs KiCad 10 and the constraint bench boards)")
    return True


# ── cons_mixed: every rule of the board, the boxed-in group members included ──
def test_e2e_cons_mixed_meets_every_rule_by_kicads_own_drc_and_is_deterministic():
    """The board the gate never routed. /Q_CLK and /Q_IO3 run 0.8 mm from their neighbours and need 11.5 mm
    more: no bump fits beside them. They get a stretch on another layer (two vias each, counted as length), and
    KiCad's DRC with the board's own rules (QSPI skew 0.3, USB skew 0.15 / gap / uncoupled 6, crystal max 8) has
    no error. A dry run writes nothing and says the same; a second project gets the same copper."""
    if skip("cons_mixed"):
        return
    pr0 = project("cons_mixed", "dry")
    from copper_kicad.pcb import guided
    from copper_kicad.pcb.v2 import planx
    before = files_of(pr0)
    plan0 = planx.v2_plan(guided.analyze(pr0, ripup="none")["suggested_plan"])
    dry = guided.route(pr0, plan0, dry_run=True)
    sc = dry.pop("_scratch", None)
    if sc is not None:
        shutil.rmtree(sc.dir, ignore_errors=True)
    assert files_of(pr0) == before and dry["written"] is False and dry["dry_run"] is True

    pr, out, _plan = route("cons_mixed", "a")
    assert out["written"] is True and out["routed"] == out["total"] == 30
    assert out["drc"]["new_errors"] == 0 and "constraint_errors" not in out["drc"], out["drc"]
    cons = out["constraints"]
    assert cons["missed"] == 0 and cons["kicad"] == {} and cons.get("len_model_err", 0.0) <= 0.005
    g = cons["groups"][0]
    assert g["ok"] is True and g["src"] == "dru" and g["max"] - g["min"] <= 0.3 and "short_by" not in g
    assert g["tuned"] >= 3 and g["hops"] >= 1                          # said, not hidden: vias were added
    assert any("stretch on another layer" in n for n in out["notes"])
    # the dry run reported the same block
    assert dry["constraints"]["groups"] == cons["groups"] and dry["constraints"]["pairs"] == cons["pairs"]
    # KiCad's verdict and KiCad's own lengths
    clean(pr)
    kl = common.kicad_lengths(pr.pcb, QSPI + list(USB) + ["/XI", "/XO"])
    assert set(QSPI) <= set(kl) and max(kl[n] for n in QSPI) - min(kl[n] for n in QSPI) <= 0.3 + 1e-9, kl
    assert abs(kl[USB[0]] - kl[USB[1]]) <= 0.15 + 1e-9 and kl["/XI"] <= 8.0 and kl["/XO"] <= 8.0, kl
    for n in QSPI + list(USB):                                         # the router's figure is KiCad's
        assert abs(cons["nets"][n][0] - kl[n]) <= 0.005, (n, cons["nets"][n], kl[n])
    # a member that changed layer did so through exactly two vias, and its via height is in its length
    _tracks, vias = copper_of(pr)
    hopped = [n for n in QSPI if cons["nets"][n][4]]
    assert len(hopped) == g["hops"] and all(cons["nets"][n][4] == 2 and cons["nets"][n][2] > 0 for n in hopped)
    assert all(sum(1 for v in vias if v[0] == n) == cons["nets"][n][4] for n in QSPI)
    # one result, one story about the pair
    row = cons["pairs"][0]
    dp = out["diff_pairs"][0]
    assert row["lanes"] is True and dp["coupling"].startswith("coupled lanes") and "sequential" not in dp["coupling"]
    assert dp["len_p"] == round(row["len"][0], 2) and dp["len_n"] == round(row["len"][1], 2)
    assert dp["skew_mm"] == round(row["skew"], 2)
    # the same board again: the same copper
    pr2, out2, _ = route("cons_mixed", "b")
    assert out2["written"] is True and copper_of(pr2) == copper_of(pr)


def test_e2e_cons_mixed_without_vias_reports_the_shortfall_and_never_a_pass():
    """max_vias: 0 on the group keeps every member on its layer: the boxed-in ones cannot be met then, and the
    result says by how much, why, and that KiCad's skew rule is broken — routed, complete, and not ok."""
    if skip("cons_mixed"):
        return
    pr, out, _ = route("cons_mixed", "nv", {"nets": {"/Q_*": {"max_vias": 0}}})
    assert out["routed"] == out["total"] == 30
    g = out["constraints"]["groups"][0]
    assert g["ok"] is False and g["short_by"] > 1.0 and g["why"] and "hops" not in g
    assert "max_vias" in g["why"] or "no free straight run" in g["why"]
    assert out["constraints"]["missed"] >= 1 and out["drc"]["constraint_errors"] >= 1
    assert out["constraints"]["kicad"].get("skew_out_of_range", 0) >= 1
    _tracks, vias = copper_of(pr)
    assert not [v for v in vias if v[0] in QSPI]
    d = common.drc(pr.pcb)
    kl = common.kicad_lengths(pr.pcb, QSPI)
    short = max(kl.values()) - min(kl.values()) - 0.3
    assert d["unconnected"] == 0 and abs(g["short_by"] - short) <= 0.01, (g, kl)
    assert sum(v for k, v in (d.get("by_type") or {}).items() if k.startswith("error:")) \
        == d["by_type"].get("error:skew_out_of_range", 0) >= 1, d["by_type"]      # nothing but the honest miss


# ── an explicit group length above the natural one ────────────────────────
def test_e2e_explicit_group_length_a_little_above_the_natural_one_is_met():
    """cons_bus_match with groups len 45, tol 0.3 (the natural longest is 36.7 mm): the inner bus members can
    add only 12 .. 17 mm between their neighbours. Before, three of them ended 3 .. 5 mm short and KiCad raised
    three skew errors against the board's own 0.5 mm rule on a board whose other layer is empty."""
    if skip("cons_bus_match"):
        return
    patch = {"groups": [{"nets": list(SD), "len": 45.0, "tol": 0.3}]}
    pr, out, _ = route("cons_bus_match", "l45", patch)
    assert out["written"] is True and out["routed"] == out["total"] == 6 and out["drc"]["new_errors"] == 0
    g = out["constraints"]["groups"][0]
    assert g["ok"] is True and g["src"] == "plan" and g["target"] == 45.0 and "short_by" not in g, g
    clean(pr)
    kl = common.kicad_lengths(pr.pcb, SD)
    assert set(kl) == set(SD) and all(abs(v - 45.0) <= 0.3 + 1e-9 for v in kl.values()), kl
    for n in SD:
        assert abs(out["constraints"]["nets"][n][0] - kl[n]) <= 0.005, (n, kl[n])
    pr2, out2, _ = route("cons_bus_match", "l45b", patch)
    assert copper_of(pr2) == copper_of(pr)
    # a target nothing can reach is still a miss with its amount (and the copper stays legal)
    pr3, out3, _ = route("cons_bus_match", "l400", {"groups": [{"nets": list(SD), "len": 400.0, "tol": 0.3}]})
    g3 = out3["constraints"]["groups"][0]
    assert g3["ok"] is False and g3["short_by"] > 100 and g3["why"] and out3["routed"] == out3["total"]
    d3 = common.drc(pr3.pcb)
    assert d3["unconnected"] == 0 and all(k.split(":", 1)[1] in C.KICAD_TYPES for k in d3["by_type"]
                                          if k.startswith("error:")), d3["by_type"]


# ── the tightest skew limit ───────────────────────────────────────────────
def test_e2e_the_tightest_skew_limit_never_ends_worse_than_a_looser_one():
    """cons_usb_pair with diff_pairs[].skew = 0.005 (the plan's minimum). Before: 0.387 mm and a KiCad skew
    error against the board's own 0.15 mm rule, where a limit of 0.15 ends at 0.05 mm with none."""
    if skip("cons_usb_pair"):
        return
    seen = {}
    for lim in (0.15, 0.005):
        pr, out, _ = route("cons_usb_pair", f"s{int(lim * 1000)}",
                           {"diff_pairs": [{"p": USB[0], "n": USB[1], "gap": 0.15, "width": 0.2, "skew": lim}]})
        assert out["routed"] == out["total"] == 6 and out["drc"]["new_errors"] == 0, out["drc"]
        clean(pr)                                                      # the board's own 0.15 mm rule included
        kl = common.kicad_lengths(pr.pcb, list(USB))
        seen[lim] = abs(kl[USB[0]] - kl[USB[1]])
        row = out["constraints"]["pairs"][0]
        assert row["src"] == "plan" and row["lanes"] is True and row.get("tuned", 0) >= 1
        assert abs(row["skew"] - seen[lim]) <= 0.001 and row["max_skew"] == lim
        assert row["ok"] is (seen[lim] <= lim + 1e-9), (lim, row, kl)
    assert seen[0.005] <= seen[0.15] + 1e-9 and seen[0.005] <= 0.005 + 1e-9, seen


# ── per-layer widths ──────────────────────────────────────────────────────
def test_e2e_per_layer_widths_are_held_on_every_layer_the_unlisted_one_too():
    """cons_mixed with nets '/P*': widths {In2.Cu 0.3, B.Cu 0.25}. Before, all twelve nets carried 1.4 .. 2.3 mm
    of F.Cu copper at 0.3 mm (their pad escapes), where the unlisted layer keeps the net's own 0.2 mm."""
    if skip("cons_mixed"):
        return
    pr, out, _ = route("cons_mixed", "w", {"nets": {"/P*": {"widths": {"In2.Cu": 0.3, "B.Cu": 0.25}}}})
    assert out["written"] is True and out["routed"] == out["total"] == 30 and out["drc"]["new_errors"] == 0
    w = out["constraints"]["widths"]
    assert w["n"] == 12 and w["off"] == [], w
    clean(pr)
    from copper_kicad.pcb.v2 import kio
    ex = kio.extract(pr)
    layers = ex["layers"]
    want = {"F.Cu": 0.2, "In1.Cu": 0.2, "In2.Cu": 0.3, "B.Cu": 0.25}
    seen = {}
    for t in ex["tracks"]:
        if str(t[0]).startswith("/P"):
            lay = layers[t[1]] if isinstance(t[1], int) else t[1]
            assert abs(t[6] - want[lay]) < 1e-6, t
            seen[lay] = seen.get(lay, 0) + 1
    assert seen.get("F.Cu", 0) >= 12 and seen.get("In2.Cu", 0) + seen.get("B.Cu", 0) >= 12, seen


# ── measuring the same copper again ───────────────────────────────────────
def test_e2e_a_call_that_only_measures_reports_what_the_routing_call_reported():
    """pcb_route_guided on a board that is already routed lays nothing and measures. Before, it said 82 % coupled,
    3.8 mm uncoupled, lanes false about copper the routing call had reported as 84 %, 3.4 mm, lanes true (KiCad:
    3.4008 mm). Also: ``diff_pairs`` in one result must not contradict ``constraints.pairs``."""
    if skip("cons_usb_pair", "cons_pairs_cross"):
        return
    from copper_kicad.pcb import guided
    for board in ("cons_usb_pair", "cons_pairs_cross"):
        pr, out, plan = route(board, "m")
        assert out["written"] is True and out["drc"]["new_errors"] == 0
        rows = {r["p"]: r for r in out["constraints"]["pairs"]}
        assert all(r["lanes"] is True for r in rows.values())
        for dp in out["diff_pairs"]:
            r = rows[dp["p"]]
            assert dp["coupling"].startswith("coupled lanes") and str(r["coupled"]) in dp["coupling"], dp
            assert (dp["len_p"], dp["len_n"]) == (round(r["len"][0], 2), round(r["len"][1], 2)), (dp, r)
            assert dp["skew_mm"] == round(r["skew"], 2)
        file_before = pr.pcb.read_bytes()
        again = guided.route(pr, plan)
        assert again.get("nothing_to_route") is True and pr.pcb.read_bytes() == file_before
        for r2 in again["constraints"]["pairs"]:
            r = rows[r2["p"]]
            assert r2["lanes"] is True and "why" not in r2, r2
            assert r2["coupled"] == r["coupled"], (board, r2["coupled"], r["coupled"])
            assert abs(r2["uncoupled"] - r["uncoupled"]) <= 0.002, (board, r2["uncoupled"], r["uncoupled"])
            assert abs(r2["len"][0] - r["len"][0]) <= 0.001 and abs(r2["len"][1] - r["len"][1]) <= 0.001
            assert abs(r2["skew"] - r["skew"]) <= 0.001 and r2["ok"] is r["ok"]
            assert r2.get("vias") == r.get("vias")


def _same_block(a: dict, b: dict, tol: float = 0.001) -> None:
    """Two constraints blocks tell the same figures (to ``tol`` mm): groups, pairs, keep-short nets."""
    assert [g["name"] for g in a.get("groups") or []] == [g["name"] for g in b.get("groups") or []]
    for ga, gb in zip(a.get("groups") or [], b.get("groups") or []):
        assert ga["ok"] is gb["ok"] and ga["n"] == gb["n"] and ga["tol"] == gb["tol"], (ga, gb)
        assert abs(ga["min"] - gb["min"]) <= tol and abs(ga["max"] - gb["max"]) <= tol, (ga, gb)
    assert [r["p"] for r in a.get("pairs") or []] == [r["p"] for r in b.get("pairs") or []]
    for ra, rb in zip(a.get("pairs") or [], b.get("pairs") or []):
        assert ra["lanes"] is rb["lanes"] and ra["coupled"] == rb["coupled"] and ra["ok"] is rb["ok"], (ra, rb)
        assert abs(ra["uncoupled"] - rb["uncoupled"]) <= 2 * tol and abs(ra["skew"] - rb["skew"]) <= tol, (ra, rb)
        assert all(abs(x - y) <= tol for x, y in zip(ra["len"], rb["len"])), (ra, rb)
    assert [(r["net"], r.get("ok")) for r in a.get("short") or []] == [(r["net"], r.get("ok")) for r in b.get("short") or []]
    for ra, rb in zip(a.get("short") or [], b.get("short") or []):
        assert abs(ra["len"] - rb["len"]) <= tol, (ra, rb)
    assert a["ok"] == b["ok"] and a["missed"] == b["missed"], (a["ok"], a["missed"], b["ok"], b["missed"])


def test_e2e_measuring_a_routed_cons_mixed_lays_nothing_and_keeps_the_block():
    """The verifier's second failure: a second pcb_route_guided call on the routed cons_mixed (4 layers, a GND
    plane, every connection made) proposed one dog-bone, was reverted, and returned NO constraints block, the
    summary "0/0 connections - no connection could be routed with this plan" and a diff_pairs row pointing at
    "constraints.pairs". Cause: the engine's Board (the second one built from the session's extract) gave the
    routed GND copper the wrong cluster, so a pad that hangs on the plane counted as cut off (guided/model.py,
    ``canonical``). Now: nothing proposed, nothing written, the same figures as the routing call."""
    if skip("cons_mixed"):
        return
    from copper_kicad.pcb import guided
    from copper_kicad.tools import route_tools
    pr, out, plan = route("cons_mixed", "re")
    assert out["written"] is True and out["routed"] == out["total"] == 30 and out["drc"]["new_errors"] == 0
    file_before = files_of(pr)
    again = guided.route(pr, plan)
    assert again.get("nothing_to_route") is True and "not_written" not in again, again.get("not_written")
    assert again["total"] == 0 and again["failed"] == [] and again["written"] is False
    assert again["added"]["tracks"] == 0 and again["added"]["vias"] == 0, again["added"]     # no dog-bone on a pad that is joined
    assert files_of(pr) == file_before
    _same_block(again["constraints"], out["constraints"])
    assert again["constraints"]["groups"][0]["ok"] is True and again["constraints"]["missed"] == 0
    # ... and through the tool layer: the summary says what happened, the block is in the (compact) result
    res = route_tools.pcb_route_guided(project=str(pr.dir), plan=plan, render=False)
    assert res.summary.startswith("nothing to route"), res.summary
    assert "could be routed" not in res.summary and files_of(pr) == file_before
    cons = res.data["constraints"]
    assert cons["missed"] == 0 and [g["ok"] for g in cons["groups"]] == [True] and [r["ok"] for r in cons["pairs"]] == [True]


def test_e2e_a_run_that_keeps_none_of_its_copper_reports_the_board_as_it_is():
    """Whenever a run's copper is discarded (it closed nothing, or its rip-up bought nothing and was undone), the
    board is unchanged — and the result must describe THAT board: its constraints block (measured on the file, no
    KiCad verdict claimed for it), a diff_pairs row that agrees with it, and a summary that does not say "no
    connection could be routed" when there was none to route. Before, the block was dropped while diff_pairs
    still pointed at "constraints.pairs"."""
    if skip("cons_usb_pair"):
        return
    from copper_kicad.pcb import guided
    from copper_kicad.pcb.v2 import engine as v2engine
    pr, out, plan = route("cons_usb_pair", "asis")
    assert out["written"] is True and out["drc"]["new_errors"] == 0
    file_before = files_of(pr)
    plain = guided.route(pr, plan)                                     # the measuring call: nothing to route
    assert plain.get("nothing_to_route") is True and plain["constraints"]["pairs"][0]["lanes"] is True

    def check(res, what):
        assert files_of(pr) == file_before, what
        assert res["written"] is False and res["routed"] == 0 and res["added"]["tracks"] == 0 and res["added"]["vias"] == 0, what
        cons = res.get("constraints")
        assert cons, f"{what}: the constraints block of the unchanged board is missing"
        _same_block(cons, plain["constraints"])
        assert "kicad" not in cons and "tuning" not in cons, what     # no verdict and no tuning record of discarded copper
        row = cons["pairs"][0]
        for dp in res.get("diff_pairs") or []:
            assert dp["coupling"].startswith("coupled lanes") and str(row["coupled"]) in dp["coupling"], (what, dp)
            assert (dp["len_p"], dp["len_n"]) == (round(row["len"][0], 2), round(row["len"][1], 2)), (what, dp, row)

    # 1. a rip-up with a plan that can route nothing: undone, and the still-routed board is what is reported
    everything = merged(plan, {"regions": [{"rect": [95, 95, 135, 130], "forbid": True, "name": "everything"}]})
    res = guided.route(pr, everything, ripup="all")
    assert "rip-up was undone" in res["not_written"] and res["total"] > 0 and res["failed"] and res["ripped_up"] == 0
    check(res, "rip-up undone")
    # 2. copper proposed on a board with nothing open (the verifier's run: a judged dog-bone that closes nothing)
    real = v2engine.route

    def proposing(*a, **kw):
        o = real(*a, **kw)
        o["_judged"] = True
        o["added"] = {"tracks": 0, "vias": 1, "length_mm": 0.0}
        return o
    v2engine.route = proposing
    try:
        res = guided.route(pr, plan)
    finally:
        v2engine.route = real
    assert res["total"] == 0 and "already made" in res["not_written"] and "board is unchanged" in res["not_written"]
    assert "could be routed" not in res["not_written"], res["not_written"]
    check(res, "copper that connects nothing")


if __name__ == "__main__":
    import time
    import traceback
    fails = 0
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v) and (not only or k in only)]
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"PASS {name} ({time.time() - t0:.1f}s)")
        except Exception as e:  # noqa: BLE001
            fails += 1
            traceback.print_exc()
            print(f"FAIL {name}: {e}")
    if _TMP is not None:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)
