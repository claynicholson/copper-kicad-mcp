"""sch_tidy end to end, through the TOOL (registry.call_tool, the path an MCP client takes), with the tool's own
defaults - no style argument, so whatever sch_tidy defaults to is what is measured.

    "<KiCad>/bin/python.exe" bench/sch/tool_e2e.py                      the 10 bench designs (corpus + unseen)
    "<KiCad>/bin/python.exe" bench/sch/tool_e2e.py --project <folder>   plus a COPY of a project from elsewhere
    "<KiCad>/bin/python.exe" bench/sch/tool_e2e.py c3-devboard --style pro

Every design is copied to bench/sch/.work/p<pid>/e2e/<name> (originals are only read; lock files, backups and
output folders are not copied). The proof is the independent witness of run.py (kicad-cli netlist canonicalised by
its own parser + kicad-cli ERC) taken before and after: netlist IDENTICAL, no new ERC. Writes
<out>/<name>/after/*.png, the re-laid sheets and <out>/summary.json (default out: bench/sch/out/_tool_e2e).
Exit code 1 when any design changed its netlist, gained an ERC violation or the tool answered an error.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as B  # noqa: E402  (sets up the private work directory: COPPER_HOME, KICAD_CONFIG_HOME, TEMP)
from run import _env  # noqa: E402

import copper_kicad.tools  # noqa: E402,F401
from copper_kicad.mcp import registry  # noqa: E402
from copper_kicad.project import Project  # noqa: E402
from copper_kicad.sch import ops as O  # noqa: E402

SKIP = shutil.ignore_patterns("~*", "*.lck", "_restore_backup*", "copper-out", "*-backups", "*.kicad_pcb", "*.kicad_prl",
                              "fp-info-cache", "__pycache__")


def one(name: str, src: Path, a) -> dict:
    work = _env.WORK / "e2e" / name
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(src, work, ignore=SKIP)
    pros = sorted(work.glob("*.kicad_pro"))
    pr = Project(work, pros[0].stem if pros else name)
    tmp = _env.WORK / "tmp"
    rep = {"design": name, "source": str(src), "sheets": [], "problems": []}
    t0 = time.time()
    nl0, erc0 = B.witness_netlist(pr.sch, tmp), B.witness_erc(pr.sch, tmp)
    for f in O.sheet_files(pr):
        rel = str(f.relative_to(work)).replace("\\", "/")
        args = {"project": str(pr.pro), "sheet": rel, "force": True, "preview": False}
        if a.style:
            args["style"] = a.style
        out = registry.call_tool("sch_tidy", args)
        d = out.get("structuredContent") or {}
        row = {"sheet": rel, "is_error": bool(out.get("isError")), "style_used": d.get("style_used"),
               "fallback": d.get("fallback"), "fallbacks": d.get("fallbacks"), "paper": d.get("paper"),
               "connectivity": d.get("connectivity"), "readability": d.get("readability"), "hard": d.get("hard"),
               "reason": d.get("reason"), "tool_erc": (d.get("verify") or {}).get("erc"),
               "summary": (out.get("content") or [{}])[0].get("text", "")[:400],
               "result_bytes": len(json.dumps({k: v for k, v in d.items() if k != "verify"}))}
        if row["is_error"]:
            code = (d.get("error") or {}).get("code") if isinstance(d.get("error"), dict) else d.get("code")
            row["code"] = code
            if code != "LAYOUT_UNSUPPORTED" and "LAYOUT_UNSUPPORTED" not in row["summary"]:   # a root of sheet symbols is fine
                rep["problems"].append(f"{rel}: tool error {row['summary'][:200]}")
        rep["sheets"].append(row)
    nl1, erc1 = B.witness_netlist(pr.sch, tmp), B.witness_erc(pr.sch, tmp)
    diff = B.netlist_diff(nl0, nl1)
    new = {k: n - erc0["counts"].get(k, 0) for k, n in erc1["counts"].items() if n > erc0["counts"].get(k, 0)}
    rep.update(netlist_identical=not diff, netlist_diff=diff[:10], erc_before=erc0["counts"], erc_after=erc1["counts"],
               erc_new=new, components=len(nl0["components"]), nets=len(nl0["nets"]))
    if diff:
        rep["problems"].append(f"NETLIST CHANGED: {diff[:4]}")
    if new:
        rep["problems"].append(f"NEW ERC: {new}")
    final = a.out_dir / name
    shutil.rmtree(final, ignore_errors=True)
    final.mkdir(parents=True)
    try:
        rep["after_png"] = [e["png"] for e in B.render(pr.sch, final / "after", a.px_per_mm, None, 0)]
    except Exception as e:  # noqa: BLE001 - the pictures are for looking; the proof is above
        rep["render_error"] = str(e)[:200]
    for f in O.sheet_files(pr):
        shutil.copyfile(f, final / f.name)
    rep["seconds"] = round(time.time() - t0, 1)
    rep["ok"] = not rep["problems"]
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("designs", nargs="*", help="bench design names (default: all of corpus + corpus_unseen)")
    ap.add_argument("--project", action="append", default=[], help="folder of another KiCad project (a copy is tidied)")
    ap.add_argument("--only-projects", action="store_true", help="skip the bench designs")
    ap.add_argument("--style", default=None, help="pass style= to the tool (default: the tool's own default)")
    ap.add_argument("--px-per-mm", type=float, default=11.0)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    a.out_dir = Path(a.out).resolve() if a.out else _env.OUT / "_tool_e2e"
    a.out_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    if not a.only_projects:
        for base in (_env.CORPUS, _env.UNSEEN):
            for d in sorted(p for p in base.iterdir() if p.is_dir()):
                if not a.designs or d.name in a.designs:
                    jobs.append((d.name, d))
    for p in a.project:
        jobs.append(("ext-" + Path(p).name, Path(p)))
    reps = []
    for name, src in jobs:
        r = one(name, src, a)
        reps.append(r)
        s = r["sheets"][0] if r["sheets"] else {}
        print(f"{'ok  ' if r['ok'] else 'FAIL'} {name:26s} style={s.get('style_used')} paper={s.get('paper')} "
              f"netlist={'identical' if r['netlist_identical'] else 'CHANGED'} new_erc={r['erc_new'] or 'none'} "
              f"readability={(s.get('readability') or {}).get('before')}->{(s.get('readability') or {}).get('after')} "
              f"{r['seconds']}s" + (f"  fallback: {s['fallback']}" if s.get("fallback") else ""), flush=True)
        for p in r["problems"]:
            print("     " + p, flush=True)
    (a.out_dir / "summary.json").write_text(json.dumps(reps, indent=1), encoding="utf-8")
    n = len(reps)
    print(f"netlist identical {sum(r['netlist_identical'] for r in reps)}/{n}; no new ERC "
          f"{sum(not r['erc_new'] for r in reps)}/{n}; ok {sum(r['ok'] for r in reps)}/{n}")
    sys.exit(0 if all(r["ok"] for r in reps) else 1)


if __name__ == "__main__":
    main()
