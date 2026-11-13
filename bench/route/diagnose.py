"""Failure-mode digest of a benchmark result: python bench/route/diagnose.py <name> [board ...]

Reads results/<name>/<board>.json and prints, per board: why connections are missing (router's own reasons,
grouped), which nets, what was skipped by the plan, where quality is worse than the reference (detours,
extra vias, thinner tracks) and the geometry counters."""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def bucket(reason: str) -> str:
    r = reason or "?"
    for pat, name in ((r"time budget", "time budget used up (never tried)"), (r"effort limit", "search effort limit"),
                      (r"blocked by routed nets", "blocked by routed nets"), (r"boxed in", "pad boxed in (no start cell)"),
                      (r"walled in", "walled in (no path at all)"), (r"routed, then removed", "routed, then removed after KiCad DRC"),
                      (r"self-check", "internal clearance self-check dropped it"), (r"nothing reachable", "no reachable target"),
                      (r"outside the board", "pad outside outline"), (r"max_vias|max_length", "plan constraint (max_vias/max_length)"),
                      (r"left unrouted by Freerouting", "left unrouted by Freerouting")):
        if re.search(pat, r):
            return name
    return r[:60]


def main():
    name = sys.argv[1]
    only = sys.argv[2:]
    for f in sorted((HERE / "results" / name).glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if only and d["board"] not in only:
            continue
        r = d["runs"][0]
        rep = r.get("router_report") or {}
        print(f"\n=== {d['board']} ({d['layers']}L): {r.get('completion_pct')} % — {r.get('unconnected')} of "
              f"{r.get('connections')} left, DRC new {r.get('drc_new_errors')} {r.get('drc_new_by_type')}, "
              f"wall {r['wall_s']} s, phases {rep.get('phases')}")
        if r.get("router_error"):
            print("  ROUTER ERROR:", r["router_error"][:400])
        print(f"  router's own count: {rep.get('routed')}/{rep.get('total')}; passes: "
              + "; ".join(f"{p.get('pass')}: {p.get('routed')}/{p.get('connections')} in {p.get('seconds')} s, "
                          f"{p.get('ripups')} rip-ups" for p in rep.get("passes") or []))
        pu = (rep.get("plan_used") or {}).get("global") or {}
        print(f"  plan.global: {pu}")
        for s in rep.get("skipped") or []:
            print(f"  skipped by plan: {s['net']} ({s['connections']} connections): {s['reason'][:70]}")
        b = {}
        for e in rep.get("failed") or []:
            k = bucket(e.get("reason"))
            b.setdefault(k, []).append(e)
        for k, es in sorted(b.items(), key=lambda kv: -len(kv[1])):
            nets = {}
            for e in es:
                nets[e["net"]] = nets.get(e["net"], 0) + int(e.get("connections") or 1)
            top = ", ".join(f"{n}×{c}" for n, c in sorted(nets.items(), key=lambda kv: -kv[1])[:8])
            print(f"  failed [{sum(nets.values())}] {k}: {top}")
            blk = {}
            for e in es:
                for n in e.get("blocked_by") or []:
                    blk[n] = blk.get(n, 0) + 1
            if blk:
                print("      blockers: " + ", ".join(f"{n}×{c}" for n, c in sorted(blk.items(), key=lambda kv: -kv[1])[:8]))
        inc = r.get("incomplete_nets") or {}
        if inc:
            print("  incomplete nets (KiCad): " + ", ".join(f"{n}×{c}" for n, c in list(inc.items())[:12])
                  + (f" … {len(inc)} nets" if len(inc) > 12 else ""))
        for x in rep.get("removed_after_drc") or []:
            print(f"  removed after DRC: {x['net']}: {x['why'][:110]}")
        for x in (r.get("drc_new_top") or [])[:4]:
            print(f"  new DRC: {x['type']}: {x['message']} {x['items']} at {x['at']}")
        notes = rep.get("notes") or []
        if notes:
            print(f"  necking notes: {len(notes)} e.g. {notes[:3]}")
        c = r.get("common")
        if c:
            print(f"  vs reference over {c['nets']} common nets: length {c['length_mm']} vs {c['ref_length_mm']} "
                  f"({c['length_ratio']}×), vias {c['vias']} vs {c['ref_vias']}, median net ratio {c['net_ratio_median']}, "
                  f"{c['nets_over_1p5x']} nets > 1.5×; thinner than ref: {c['nets_thinner_than_ref']} nets, wider: {c['nets_wider_than_ref']}")
            for w in c["worst_detours"][:4]:
                print(f"      detour {w['net']}: {w['mm']} mm vs {w['ref_mm']} ({w['ratio']}×), vias {w['vias']} vs {w['ref_vias']}")
            for w in c["most_extra_vias"][:3]:
                print(f"      vias {w['net']}: {w['vias']} vs {w['ref_vias']}")
            for w in c["thinner_top"][:4]:
                print(f"      thinner {w['net']}: {w['width']} vs {w['ref_width']} mm over {w['ref_mm']} mm")
        print(f"  whole board: length {r.get('length_mm')} (ref {r.get('ref_length_mm')}), vias {r.get('vias')} "
              f"(ref {r.get('ref_vias')}), length/MST over finished nets {r.get('length_over_mst_done_nets')}, geometry {r.get('geometry')}")
        print(f"  deterministic: {d.get('deterministic')}")


if __name__ == "__main__":
    main()
