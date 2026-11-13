"""Steering demo (design §6.4, §12 stage 7): ONE plan revision taken from the router's own diagnostics.

    run 1   the board with the suggested plan (or an existing result: --base NAME)
    read    router_report.suggest[] of run 1 (what pcb_route_guided returns as `suggest`)
    revise  build one plan patch from it, mechanically (no judgement, no model call):
              --rule order   every {"order": [net, "*"]} patch merged into one order list (failed nets first)
              --rule first   suggest[0].patch as it is
              --rule budget  the {"global": {"time_budget_s": ...}} patch, when the router offers one
    run 2   the same board with that patch merged over the suggested plan (run.py --opt plan=...)
    report  KiCad's verdict on both (the harness's numbers, not the router's)

    python bench/route/steer_demo.py coldfire [--rule order] [--base v2_stage7] [--name steer] [--runs 1]

Patches of the form {"move": ...} are placement advice, not plan fields: they are skipped.
Nothing here is scored by the benchmark; it only calls run.py and reads its result files.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"


def load(name: str, board: str) -> dict:
    return json.loads((RESULTS / name / f"{board}.json").read_text(encoding="utf-8"))


def revision(suggest: list, rule: str) -> tuple[dict | None, list[str]]:
    why = []
    plans = [s for s in suggest if isinstance(s.get("patch"), dict) and "move" not in s["patch"]]
    if rule == "first":
        if not plans:
            return None, why
        return plans[0]["patch"], [plans[0].get("why", "")]
    if rule == "budget":
        for s in plans:
            if "time_budget_s" in (s["patch"].get("global") or {}):
                return s["patch"], [s.get("why", "")]
        return None, why
    nets = []
    for s in plans:
        for n in s["patch"].get("order") or []:
            if n != "*" and n not in nets:
                nets.append(n)
                why.append(s.get("why", ""))
    return ({"order": nets + ["*"]} if nets else None), why


def run(name: str, board: str, runs: int, plan: dict | None) -> None:
    cmd = [sys.executable, str(HERE / "run.py"), "--router", "v2", "--name", name, "--board", board, "--runs", str(runs)]
    if plan is not None:
        cmd += ["--opt", "plan=" + json.dumps(plan)]
    subprocess.run(cmd, check=True)


def row(d: dict) -> dict:
    r = d["runs"][0]
    same = len(d["runs"]) > 1 and len({x["signature"] for x in d["runs"]}) == 1
    return {"connections": r["connections"], "left": r["unconnected"], "done_pct": r["completion_pct"],
            "new_drc": r["drc_new_errors"], "vias": r["vias"], "wall_s": r["wall_s"],
            "same_twice": same if len(d["runs"]) > 1 else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("--rule", choices=("order", "first", "budget"), default="order")
    ap.add_argument("--base", default=None, help="existing results name to take run 1 from")
    ap.add_argument("--name", default="steer")
    ap.add_argument("--runs", type=int, default=1)
    a = ap.parse_args()
    base = a.base or f"{a.name}_base"
    if a.base is None:
        run(base, a.board, a.runs, None)
    d1 = load(base, a.board)
    suggest = d1["runs"][0]["router_report"].get("suggest") or []
    patch, why = revision(suggest, a.rule)
    out = {"board": a.board, "rule": a.rule, "base": base, "before": row(d1), "patch": patch, "why": why}
    if patch is None:
        out["note"] = "the router's suggest[] holds no patch of this kind"
    else:
        rev = f"{a.name}_{a.rule}"
        run(rev, a.board, a.runs, patch)
        out["revised"] = rev
        out["after"] = row(load(rev, a.board))
        out["improved"] = (out["after"]["new_drc"], out["after"]["left"]) < (out["before"]["new_drc"], out["before"]["left"])
    f = RESULTS / f"{a.name}_{a.rule}_{a.board}.demo.json"
    f.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
