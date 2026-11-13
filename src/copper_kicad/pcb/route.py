"""Autorouting: Freerouting (Specctra DSN → SES), leftovers handed to the guided router, with the
board restored if anything fails.

Diagnosis: set COPPER_ROUTE_DEBUG_DIR to a folder to keep every round's DSN / SES files."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .. import project as projmod
from ..mcp.registry import ToolError
from . import board, freerouting, worker

_WS = " \t\r\n"


def _block_end(txt: str, k: int) -> int:
    """Index of the ')' closing the '(' at ``k``. A '"' only opens/closes a string at a token
    boundary, because KiCad writes net names containing '"' unquoted (e.g. LED"Q)."""
    depth, j, inq = 0, k, False
    while j < len(txt):
        ch = txt[j]
        if ch == '"' and ((inq and (j + 1 >= len(txt) or txt[j + 1] in _WS + ")"))
                          or (not inq and txt[j - 1] in _WS + "(")):
            inq = not inq
        elif not inq:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return j
        j += 1
    return len(txt) - 1


_TOK = re.compile(r'(?<![^\s(])"[^"]*"(?=[\s)]|$)|[^\s()]+')


def _unq(t: str) -> str:
    return t[1:-1] if len(t) >= 2 and t[0] == '"' and t[-1] == '"' else t


def _patch_dsn(dsn: Path, ignore_nets: list[str]):
    """Drop nets Freerouting should leave alone (e.g. GND that a pour will join): their
    ``(net ...)`` blocks in the network and their names in every ``(class ...)`` net list
    (KiCad wraps long class lists over several lines, so this works on whole blocks)."""
    if not ignore_nets:
        return 0
    want = set(ignore_nets) | {n.replace('"', "''") for n in ignore_nets}   # KiCad writes " as '' in (net ...)
    txt = dsn.read_text(encoding="utf-8", errors="replace")
    out, i, n = [], 0, 0
    for m in re.finditer(r"\((net|class)\s", txt):
        k = m.start()
        if k < i:
            continue
        e = _block_end(txt, k)
        block = txt[k:e + 1]
        if m.group(1) == "net":
            head = _TOK.findall(block[4:].lstrip()[:400])
            name = _unq(head[0]) if head else ""
            # only network nets (they own a (pins ...) list); a wire's (net X) reference stays
            if name in want and "(pins" in block:
                out.append(txt[i:k])
                i = e + 1
                n += 1
            continue
        # (class <id> net net ... (circuit ...) (rule ...)) — filter the names before the first sub-list
        sub = block.find("(", 1)
        names_part = block[: sub if sub > 0 else len(block) - 1]
        toks = _TOK.findall(names_part[len("(class"):])
        if not any(_unq(t) in want for t in toks[1:]):
            continue
        kept = [toks[0]] + [t for t in toks[1:] if _unq(t) not in want]
        rest = block[sub:] if sub > 0 else ")"
        out.append(txt[i:k])
        out.append("(class " + " ".join(kept) + "\n      " + rest)
        i = e + 1
    out.append(txt[i:])
    dsn.write_text("".join(out), encoding="utf-8")
    return n


NL = chr(10)


def _add_keepouts(dsn: Path, items: list[dict], margin: float = 0.25) -> int:
    """Turn board-level copper text/graphics into Specctra keepouts (DSN units: um, y up)."""
    if not items:
        return 0
    txt = dsn.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"\(unit\s+(\w+)\)", txt)
    scale = {"um": 1000.0, "mm": 1.0, "mil": 1 / 0.0254, "inch": 1 / 25.4}.get(m.group(1) if m else "um", 1000.0)
    lines = []
    for i, it in enumerate(items):
        x0, y0, x1, y1 = it["bbox"]
        x0, y0, x1, y1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
        lay = it["layer"].replace('"', "")
        lay = f'"{lay}"' if " " in lay else lay
        lines.append(f'    (keepout "copper_graphic_{i + 1}" (rect {lay} {x0 * scale:.0f} {-y1 * scale:.0f} '
                     f'{x1 * scale:.0f} {-y0 * scale:.0f}))')
    # insert right after the (boundary ...) block inside (structure ...)
    k = txt.find("(boundary")
    if k < 0:
        return 0
    depth, j = 0, k
    while j < len(txt):
        if txt[j] == "(":
            depth += 1
        elif txt[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    txt = txt[:j + 1] + NL + NL.join(lines) + txt[j + 1:]
    dsn.write_text(txt, encoding="utf-8")
    return len(lines)


def _path_keepouts(name: str, layer: str, width: float, pts: list[float]) -> list[str]:
    """One polygon keepout per track segment (square ends). Freerouting does not reliably honour
    path-shaped keepouts — new traces crossed the kept copper of finished nets — while polygons hold."""
    out, h = [], width / 2
    for i in range(0, len(pts) - 3, 2):
        x1, y1, x2, y2 = pts[i:i + 4]
        ln = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        ux, uy = ((x2 - x1) / ln, (y2 - y1) / ln) if ln else (1.0, 0.0)
        ax, ay, bx, by = x1 - ux * h, y1 - uy * h, x2 + ux * h, y2 + uy * h
        nx, ny = -uy * h, ux * h
        c = [ax + nx, ay + ny, bx + nx, by + ny, bx - nx, by - ny, ax - nx, ay - ny]
        out.append(f'    (keepout "{name}_{i // 2}" (polygon {layer} 0 ' + " ".join(f"{v:.1f}" for v in c) + "))")
    return out


def _edge_keepouts(dsn: Path, extra: float, layers: list[str]) -> int:
    """Freerouting keeps only the ordinary clearance to the board edge; KiCad's copper-to-edge rule is
    usually larger (JLC: 0.3 mm against 0.2). A keepout strip of ``extra`` mm either side of the outline
    on every signal layer makes up the difference (a via landed 0.22 mm from the edge without it)."""
    if extra <= 0 or not layers:
        return 0
    txt = dsn.read_text(encoding="utf-8", errors="replace")
    k = txt.find("(boundary")
    if k < 0:
        return 0
    sc = _scale(txt)
    keep, i = [], 0
    while k >= 0:
        e = _block_end(txt, k)
        for m in re.finditer(r"\(path\s+\S+\s+[\d.]+((?:\s+-?[\d.]+)+)\s*\)", txt[k:e + 1]):
            pts = [float(v) for v in m.group(1).split()]
            for lay in layers:
                q = f'"{lay}"' if " " in lay else lay
                keep += _path_keepouts(f"copper_edge_{i}", q, 2 * extra * sc, pts)
                i += 1
        last = e
        k = txt.find("(boundary", e)
    if not keep:
        return 0
    txt = txt[:last + 1] + NL + NL.join(keep) + txt[last + 1:]
    dsn.write_text(txt, encoding="utf-8")
    return len(keep)


def _wiring_to_keepouts(dsn: Path, nets) -> int:
    """Turn the wires and vias of ``nets`` (complete nets taken out of the network) into keepouts, so
    Freerouting still routes around that copper but never has to merge those traces."""
    nets = set(nets or [])
    if not nets:
        return 0
    want = nets | {n.replace('"', "''") for n in nets}
    txt = dsn.read_text(encoding="utf-8", errors="replace")
    st = txt.find("(structure")
    sig_layers = re.findall(r'\(layer\s+("[^"]+"|\S+)\s*\(type\s+signal\)', txt[st:_block_end(txt, st)] if st >= 0 else "")
    w0 = txt.find("(wiring")
    if w0 < 0:
        return 0
    w1 = _block_end(txt, w0)
    keep, out, i, n = [], [], 0, 0
    for m in re.finditer(r"\((wire|via)\s", txt[w0:w1]):
        k = w0 + m.start()
        if k < i:
            continue
        e = _block_end(txt, k)
        block = txt[k:e + 1]
        nm = re.search(r'\(net\s+("[^"]*"|[^\s()]+)\s*\)', block)
        if not nm or _unq(nm.group(1)) not in want:
            continue
        if m.group(1) == "wire":
            pm = re.search(r"\(path\s+(\"[^\"]+\"|\S+)\s+([\d.]+)((?:\s+-?[\d.]+)+)\s*\)", block)
            if pm:
                keep += _path_keepouts(f"copper_done_{n}", pm.group(1), float(pm.group(2)),
                                       [float(v) for v in pm.group(3).split()])
        else:
            vm = re.search(r'\(via\s+("[^"]+"|\S+)\s+(-?[\d.]+)\s+(-?[\d.]+)', block)
            dm = re.search(r"_(\d+(?:\.\d+)?):", vm.group(1)) if vm else None
            if vm and dm:
                dia = float(dm.group(1))
                for lay in sig_layers:
                    keep.append(f'    (keepout "copper_done_{n}" (circle {lay} {dia:g} {vm.group(2)} {vm.group(3)}))')
        out.append(txt[i:k])
        i = e + 1
        n += 1
    if not n:
        return 0
    out.append(txt[i:])
    txt = "".join(out)
    k = txt.find("(boundary")
    if k >= 0 and keep:
        e = _block_end(txt, k)
        txt = txt[:e + 1] + NL + NL.join(keep) + txt[e + 1:]
    dsn.write_text(txt, encoding="utf-8")
    return n


def _scale(txt: str) -> float:
    m = re.search(r"\(unit\s+(\w+)\)", txt)
    return {"um": 1000.0, "mm": 1.0, "mil": 1 / 0.0254, "inch": 1 / 25.4}.get(m.group(1) if m else "um", 1000.0)


def _patch_rules(dsn: Path, clearance: float | None = None, neck: dict | None = None,
                 power_layers: list[str] | None = None) -> dict:
    """Make the DSN agree with KiCad DRC:

    * clearance — KiCad checks two items against the LARGER of their classes' clearances. Freerouting
      uses a class's own clearance only between two items of that class and the board default
      (structure rule) for every mixed pair, for pads whose net left the network and for keepouts — so
      a Default track passed a Power pad at the Default 0.15 mm. The board default becomes the largest
      class clearance; each class keeps its own, so two signal tracks may still pass at the signal
      clearance (raising every class to the maximum made 0.4 mm-pitch QFN pads nearly impossible to leave).
    * neck — nets whose class width cannot leave a fine-pitch pad legally move into an extra class
      with the widest legal width (Freerouting has no per-pad neck-down).
    * power_layers — reserved plane layers become (type power): Freerouting routes no traces there
      and reaches the plane net with vias."""
    txt = dsn.read_text(encoding="utf-8", errors="replace")
    sc = _scale(txt)
    out = {"clearance": None, "necked": {}, "plane_layers": []}
    if clearance:
        v = f"{clearance * sc:.6g}"
        st = txt.find("(structure")
        if st >= 0:
            se = _block_end(txt, st)
            head, n = re.subn(r"\(clearance\s+[\d.]+\)", f"(clearance {v})", txt[st:se])
            txt = txt[:st] + head + txt[se:]
            out["clearance"] = clearance if n else None
    for lay in power_layers or []:
        q = re.escape(lay)
        txt, n = re.subn(r'(\(layer\s+"?' + q + r'"?\s*\(type\s+)signal(\))', r"\1power\2", txt)
        if n:
            out["plane_layers"].append(lay)
    if neck:
        want = {}
        for n_, w in neck.items():
            want[n_] = w
            want[n_.replace('"', "''")] = w
        pieces, i, circuits = [], 0, {}
        moved = {}
        for m in re.finditer(r"\(class\s", txt):
            k = m.start()
            if k < i:
                continue
            e = _block_end(txt, k)
            block = txt[k:e + 1]
            sub = block.find("(", 1)
            names_part = block[: sub if sub > 0 else len(block) - 1]
            toks = _TOK.findall(names_part[len("(class"):])
            hit = [t for t in toks[1:] if _unq(t) in want]
            if not hit:
                continue
            c = re.search(r"\(circuit", block)
            circ = block[c.start():_block_end(block, c.start()) + 1] if c else ""
            cm = re.search(r"\(clearance\s+[\d.]+\)", block[sub:] if sub > 0 else "")
            for t in hit:
                moved[t] = (want[_unq(t)], circ, cm.group(0) if cm else "")
            kept = [toks[0]] + [t for t in toks[1:] if _unq(t) not in want]
            pieces.append(txt[i:k])
            pieces.append("(class " + " ".join(kept) + "\n      " + (block[sub:] if sub > 0 else ")"))
            i = e + 1
        pieces.append(txt[i:])
        txt = "".join(pieces)
        if moved:
            groups = {}
            for t, (w, circ, clr) in moved.items():
                groups.setdefault((w, circ, clr), []).append(t)
            blocks = []
            for i, ((w, circ, clr), nets) in enumerate(sorted(groups.items(), key=lambda kv: kv[0])):
                name = f"copper_neck_{int(round(w * 1000))}" + (f"_{i}" if sum(1 for g in groups if g[0] == w) > 1 else "")
                blocks.append(f"    (class {name} {' '.join(nets)}\n      {circ}\n"
                              f"      (rule (width {w * sc:.6g}) {clr})\n    )")
                for t in nets:
                    out["necked"][_unq(t)] = w
            k = txt.find("(network")
            if k >= 0:
                e = _block_end(txt, k)
                txt = txt[:e] + NL.join(blocks) + NL + "  " + txt[e:]
    dsn.write_text(txt, encoding="utf-8")
    return out


def _signal_layers(info: dict, signal_layers: list[str] | None) -> tuple[list[str], list[str]]:
    """(reserved plane layers, signal layers). Default: inner layers carrying a board-wide pour are
    planes; on a 4-layer board both inner layers are reserved (JLC SIG/GND/PWR/SIG stack-up)."""
    layers = info.get("copper_layers") or ["F.Cu", "B.Cu"]
    if signal_layers:
        bad = [x for x in signal_layers if x not in layers]
        if bad:
            raise ToolError(f"signal_layers {bad} are not copper layers of this board ({', '.join(layers)})",
                            code="BAD_LAYER", fix=f"pick from {layers}")
        sig = [x for x in layers if x in signal_layers]
    else:
        planes = set(info.get("planes") or {})
        inner = [x for x in layers if x not in ("F.Cu", "B.Cu")]
        sig = [x for x in layers if x not in planes and not (len(layers) == 4 and x in inner)]
    return [x for x in layers if x not in sig], sig


def _board_stats(pcb: Path) -> dict:
    """Track/via counts and {net: unrouted} of the board file as it is now (read-only)."""
    return worker.run("board", {"pcb": str(pcb), "ops": [], "save": False, "by_net": True}).get("after") or {}


def _unrouted(stats: dict, ignore: set) -> int:
    by_net = stats.get("unconnected_by_net")
    # nets left for a pour do not count: otherwise every extra round is wasted on them
    return (sum(v for k, v in by_net.items() if k not in ignore) if by_net is not None
            else int(stats.get("unconnected") or 0))


def _finish_guided(pr, sig: list[str], ignore: set, budget: float, while_open: str) -> dict:
    """Hand what Freerouting left to the guided router (rip-up-and-reroute on exact pad geometry; it
    removes any copper of its own that KiCad DRC objects to). Existing tracks stay as they are."""
    from . import guided
    plan = {"global": {"layers": list(sig), "time_budget_s": int(max(15, budget)), "ripup_rounds": 6, "via_cost": 2}}
    if ignore:
        plan["nets"] = {n: {"skip": True} for n in sorted(ignore)}
    return guided.route(pr, plan=plan, ripup="none", while_open=while_open)


def autoroute(pr, passes: int = 30, timeout: int = 600, clear_existing: bool = False,
              ignore_nets: list[str] | None = None, while_open: str = "refuse", progress=None,
              rounds: int = 3, signal_layers: list[str] | None = None, keep_worse: bool = False,
              fanout: bool = True, finish: bool = True) -> dict:
    """Route with Freerouting, then finish, within one overall ``timeout`` budget:

    1. route — Freerouting on everything unrouted (existing copper is exported as fixed, so only new
       wiring comes back);
    2. continue — Freerouting again on what is left, with the finished nets frozen;
    3. guided — the guided router (``pcb.guided.route``) on what Freerouting could not finish
       (``finish``); its result is kept only if it routes more without adding DRC errors;
    4. ripup — only on boards a full route takes under a minute, and only when no hand-made tracks
       would be lost: rip everything up and route again with more passes (then 3 again).

    The stage with the fewest unrouted connections is kept. If the result has more DRC errors and no
    fewer unrouted connections than the board had before, the board is restored (unless keep_worse)."""
    t_start = time.time()
    pcb = pr.pcb
    if not pcb.exists():
        raise ToolError("no board yet", code="NO_BOARD", fix="pcb_sync() then pcb_setup() and pcb_place(auto=true)")
    projmod.guard_writable(pcb, while_open)
    if progress:
        progress(0.02, "checking unrouted connections")
    before = board.drc(pr, parity=False)
    snap0 = board.read(pr, what=["summary"])
    ignore = set(ignore_nets or [])
    if not clear_existing and before["unconnected"] == 0:
        return {"checkpoint": None, "rounds": [], "router": None, "seconds": 0, "timed_out": False,
                "nets_left_to_pour": 0, "copper_graphic_keepouts": 0, "nothing_to_route": True,
                "unrouted_before": 0, "unrouted_after": 0, "tracks_added": 0, "vias_added": 0,
                "track_length_mm": snap0.get("track_length_mm"), "drc": board.drc(pr, parity=True),
                "revision": projmod.revision(pcb)}
    cp = projmod.checkpoint(pr, "before-autoroute")
    backup = Path(tempfile.mkdtemp(prefix="copper-route-"))
    shutil.copy2(pcb, backup / pcb.name)
    work = Path(tempfile.mkdtemp(prefix="copper-fr-"))
    rounds_log, downloads = [], []
    st = {"info": {}, "dropped": 0, "keepouts": 0, "patched": {}, "fan": None, "first": None, "dups": 0,
          "sig": None, "n": 0, "guided": None}
    best = None   # (unrouted, stage number, after-stats, duplicates removed up to that stage)
    deadline = time.time() + max(30, int(timeout))
    reverted = None

    def say(frac, msg):
        if progress:
            progress(frac, msg)

    def keep(unc, stats):
        nonlocal best
        if best is None or unc < best[0]:
            best = (unc, st["n"], stats, st["dups"])
            shutil.copy2(pcb, work / "best.kicad_pcb")
            return True
        return False

    def freeroute(strategy: str, cap: float, npass: int):
        """One Freerouting run on the board as it is on disk. Returns (unrouted, after-stats)."""
        rnd = st["n"]
        dsn, ses = work / f"board{rnd}.dsn", work / f"board{rnd}.ses"
        ops = []
        if (clear_existing and rnd == 1) or strategy == "ripup":
            ops.append({"op": "delete", "all_tracks": True})
        # nets with an inner plane: a stub + via per SMD pad (Freerouting will not drop vias into a plane)
        if fanout:
            ops.append({"op": "fanout", "planes": True})
        ops.append({"op": "export_dsn", "path": str(dsn)})
        ex = worker.run("board", {"pcb": str(pcb), "ops": ops, "save": len(ops) > 1, "by_net": True})
        ex_info = ex["results"][-1]
        # nets that are already complete leave the DSN network (their copper stays as obstacles):
        # Freerouting 2.2 crashes merging existing traces (NPE / StackOverflow in PolylineTrace.combine)
        # and stops after pass 1, so it should only see the wiring of nets it has to finish
        aft = ex.get("after") or {}
        by0 = aft.get("unconnected_by_net")
        done = ([n for n in ex_info.get("nets", []) if n not in by0 and n not in ignore]
                if by0 is not None and aft.get("tracks") else [])
        if fanout:
            fo = ex["results"][-2]
            if fo.get("fanout_vias") or fo.get("thermal_vias") or fo.get("failed"):
                st["fan"] = {k: fo[k] for k in ("fanout_vias", "thermal_vias", "failed") if fo.get(k)}
        st["dropped"] = _patch_dsn(dsn, sorted(ignore))
        _patch_dsn(dsn, done)
        _wiring_to_keepouts(dsn, done)
        st["keepouts"] = _add_keepouts(dsn, ex_info.get("copper_graphics") or [])
        planes, sig = _signal_layers(ex_info, signal_layers)
        patched = _patch_rules(dsn, clearance=ex_info.get("max_clearance"),
                               neck={k: v for k, v in (ex_info.get("neck") or {}).items() if k not in ignore},
                               power_layers=planes)
        patched["signal_layers"] = st["sig"] = sig
        _edge_keepouts(dsn, (ex_info.get("edge_clearance") or 0) - (ex_info.get("max_clearance") or 0) + 0.01
                       if (ex_info.get("edge_clearance") or 0) > (ex_info.get("max_clearance") or 0) else 0, sig)
        st["patched"] = patched
        info = st["info"] = freerouting.run(dsn, ses, passes=npass, timeout=int(max(10, cap)),
                                            progress=progress if rnd == 1 else None,
                                            log_file=pr.copper_dir / "freerouting.log")
        if not info["ses_written"]:
            why = info.get("crashed") or ("timed out" if info["timed_out"] else f"exit {info['returncode']}")
            raise ToolError(f"Freerouting produced no session file ({why}). Last output: "
                            f"{' | '.join(info['log_tail'][-3:])[:400]}", code="ROUTE_FAILED",
                            fix="pcb_route(passes=10) or pcb_place(auto=true, spacing=1.0) to give the router "
                                f"more room; full log: {pr.copper_dir / 'freerouting.log'}")
        if st["first"] is None:
            st["first"] = (info["seconds"], max(1, int(info.get("passes_done") or npass)))
        ops = [{"op": "import_ses", "path": str(ses)}, {"op": "heal"}]
        if snap0.get("zones"):
            ops.append({"op": "fill"})
        imp = worker.run("board", {"pcb": str(pcb), "ops": ops, "save": True, "by_net": True})
        r0 = imp["results"][0]
        st["dups"] += int(r0.get("stale_duplicates") or 0)
        unc = _unrouted(imp["after"], ignore)
        rounds_log.append({"round": rnd, "strategy": strategy, "seconds": info["seconds"],
                           "unrouted": unc, "timed_out": info["timed_out"]}
                          | ({"crashed": info["crashed"]} if info.get("crashed") else {})
                          | ({"duplicates_removed": r0["stale_duplicates"]} if r0.get("stale_duplicates") else {})
                          | ({"clashing_nets_removed": sorted(r0["clashes_removed"])} if r0.get("clashes_removed") else {}))
        return unc, imp["after"]

    def stage(strategy: str, cap: float, npass: int) -> bool:
        """Run one Freerouting stage; False when it failed (the best earlier stage is kept)."""
        st["n"] += 1
        try:
            unc, stats = freeroute(strategy, cap, npass)
        except ToolError as e:
            if best is None:
                raise
            rounds_log.append({"round": st["n"], "strategy": strategy, "failed": e.message[:200]})
            return False
        keep(unc, stats)
        return True

    def guided_stage():
        """Leftovers → guided router. Kept only when it routes more and adds no DRC errors."""
        left = deadline - time.time()
        if not finish or best is None or not best[0] or left < 30:
            return
        st["n"] += 1
        shutil.copy2(work / "best.kicad_pcb", pcb)
        t0 = time.time()
        entry = {"round": st["n"], "strategy": "guided"}
        try:
            say(0.8, f"guided router: finishing {best[0]} connections Freerouting left")
            err0 = board.drc(pr, parity=False, record=False)["errors"]
            g = _finish_guided(pr, st["sig"] or ["F.Cu", "B.Cu"], ignore, min(120.0, left - 20), while_open)
            stats = _board_stats(pcb)
            unc = _unrouted(stats, ignore)
            entry |= {"seconds": round(time.time() - t0, 1), "unrouted": unc}
            if unc < best[0] and (g.get("drc") or {}).get("errors", 0) <= err0:
                st["guided"] = {"routed": best[0] - unc,
                                "failed": [{k: f[k] for k in ("net", "from", "to", "reason", "where") if k in f}
                                           for f in (g.get("failed") or [])][:20]}
                keep(unc, stats)
            else:
                entry["discarded"] = "no gain" if unc >= best[0] else "added DRC errors"
        except Exception as e:  # noqa: BLE001 — the finisher is optional: Freerouting's result stands
            entry["failed"] = (getattr(e, "message", None) or str(e))[:200]
        rounds_log.append(entry)

    try:
        say(0.05, "Freerouting: preparing Java + router")
        had_java, had_jar = freerouting.find_java() is not None, freerouting.jar_path().exists()
        freerouting.ensure_java(progress)
        freerouting.ensure_jar(progress)
        if not had_java:
            downloads.append(f"Java {freerouting.JAVA_MAJOR} runtime → {freerouting.tools_dir()}")
        if not had_jar:
            downloads.append(f"Freerouting {freerouting.FREEROUTING_VERSION} → {freerouting.jar_path()}")
        say(0.12, "Freerouting round 1 (route): routing")
        ok = stage("route", deadline - time.time(), passes)
        info = st["info"]
        if ok and best[0] and rounds >= 2 and not info["timed_out"] and not info.get("crashed"):
            left = deadline - time.time()
            if left < 20:
                rounds_log.append({"round": st["n"] + 1, "strategy": "continue", "skipped": "time budget used up"})
            else:
                # later rounds get a share of the budget, not all of it: a router that hangs after a
                # crash must not eat the whole timeout
                say(0.4, "Freerouting round 2 (continue): routing")
                ok = stage("continue", min(left, max(60.0, 2 * st["first"][0] + 30)), passes)
        guided_stage()
        # rip-up-and-retry: a second opinion that is only cheap on small boards, and never when it would
        # throw away tracks that were on the board before this call
        info = st["info"]
        if (ok and best[0] and rounds >= 3 and (clear_existing or not snap0.get("tracks"))
                and not info["timed_out"] and not info.get("crashed")):
            first_secs, first_passes = st["first"]
            left = deadline - time.time()
            cap = min(left - 10, max(60.0, 2 * first_secs + 30))
            fit = int((cap - 10) / max(0.2, first_secs / first_passes))
            if first_secs > 60:
                rounds_log.append({"round": st["n"] + 1, "strategy": "ripup",
                                   "skipped": f"a full route of this board takes {first_secs:.0f} s — not repeated"})
            elif left < 20 or fit < min(passes, 8):
                rounds_log.append({"round": st["n"] + 1, "strategy": "ripup", "skipped": "time budget used up"})
            else:
                say(0.85, "Freerouting (rip-up and retry): routing")
                was = best[1]
                if stage("ripup", cap, min(passes * 2, fit)) and best[1] != was:
                    guided_stage()
        # the board on disk is whatever the last attempted stage left (a rip-up round may have deleted
        # every track and then failed): always put the best stage back
        shutil.copy2(work / "best.kicad_pcb", pcb)
        rounds_log.append({"kept_round": best[1]})
        say(0.95, "verifying with KiCad DRC")
        after = board.drc(pr, parity=True)
        worse = after["errors"] > before["errors"] and after["unconnected"] >= before["unconnected"]
        if worse and not keep_worse:
            shutil.copy2(backup / pcb.name, pcb)
            reverted = (f"routing added DRC errors ({before['errors']} → {after['errors']}) without reducing "
                        f"unrouted ({before['unconnected']} → {after['unconnected']}); board restored")
            after = board.drc(pr, parity=True)
    except BaseException:
        shutil.copy2(backup / pcb.name, pcb)   # restore the untouched board
        raise
    finally:
        dbg = os.environ.get("COPPER_ROUTE_DEBUG_DIR")   # keep the DSN / SES files of every round for diagnosis
        if dbg:
            shutil.copytree(work, Path(dbg) / time.strftime("route-%H%M%S"), dirs_exist_ok=True)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
    last = best[2] if not reverted else dict(snap0, unconnected_by_net=None)
    dups = 0 if reverted else best[3]
    patched, info = st["patched"], st["info"]
    kept_guided = st["guided"] if (st["guided"] and not reverted) else None
    return {"checkpoint": cp, "rounds": rounds_log,
            "router": f"Freerouting {freerouting.FREEROUTING_VERSION}" + (" + guided router" if kept_guided else ""),
            "seconds": round(time.time() - t_start, 1),
            "router_seconds": round(sum(r.get("seconds", 0) for r in rounds_log), 1),
            "timed_out": info.get("timed_out"), "reverted": reverted, "downloaded": downloads or None,
            "nets_left_to_pour": st["dropped"], "copper_graphic_keepouts": st["keepouts"],
            "plane_layers": patched.get("plane_layers") or None, "signal_layers": patched.get("signal_layers"),
            "necked_nets": patched.get("necked") or None, "clearance_mm": patched.get("clearance"),
            "plane_fanout": st["fan"], "guided_finish": kept_guided,
            "unrouted_before": before["unconnected"], "unrouted_after": after["unconnected"],
            "drc_errors_before": before["errors"],
            # duplicates that were already on the board were deleted: they are not "fewer tracks routed"
            "tracks_added": last.get("tracks", 0) - snap0.get("tracks", 0) + dups,
            "duplicates_removed": dups or None,
            "vias_added": last.get("vias", 0) - snap0.get("vias", 0),
            "track_length_mm": last.get("track_length_mm"), "drc": after,
            "unrouted_by_net": last.get("unconnected_by_net"),
            "revision": projmod.revision(pcb)}
