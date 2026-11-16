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


def _finish_guided(pr, sig: list[str], ignore: set, budget: float, while_open: str, only=None,
                   all_nets=()) -> dict:
    """Hand what Freerouting left to the guided router (rip-up-and-reroute on exact pad geometry; it
    removes any copper of its own that KiCad DRC objects to). Existing tracks stay as they are."""
    from . import guided
    plan = {"global": {"layers": list(sig), "time_budget_s": int(max(15, budget)), "ripup_rounds": 6, "via_cost": 2}}
    skip = set(ignore)
    if only:   # an ECO route: the finisher must not add copper (e.g. plane vias) to any other net either
        skip |= {n for n in all_nets if n not in only}
    if skip:
        plan["nets"] = {n: {"skip": True} for n in sorted(skip)}
    return guided.route(pr, plan=plan, ripup="none", while_open=while_open,
                        **({"nets": sorted(only)} if only else {}))


RIPUP_MAX_NETS = 16      # a finisher rip-up re-routes at most this many nets
RIPUP_MAX_PADS = 12      # blockers with more pads than this (power rails) are never ripped up


def _ripup_nets(pr, failed: list[dict], keep_out: set, limit: int = RIPUP_MAX_NETS, radius: float = 1.0) -> list[str]:
    """The nets a targeted rip-up should re-route for the connections the finisher left open: each failed
    connection's own net, the nets it reports as blocking it, and every net whose tracks / vias cross the escape
    region (pad box + ``radius`` mm) of its pins. Ground, pour, frozen and ignored nets (``keep_out``) and big
    multi-pad rails are never ripped up; failed nets come first, then the most frequent blockers."""
    from .place import is_ground
    own = [f["net"] for f in failed if f.get("net")]
    pins = [p for f in failed for p in (f.get("from"), f.get("to")) if p and p != "pour"]
    pts = [f["where"] for f in failed if isinstance(f.get("where"), (list, tuple)) and len(f["where"]) == 2]
    esc = worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "escape_nets", "pins": pins[:60], "points": pts[:60],
                                                           "radius": radius}], "save": False})["results"][0]
    pads = esc.get("pad_count") or {}
    real = set(esc.get("board_nets") or ())
    score = {}
    for f in failed:
        for n in f.get("blocked_by") or []:
            score[n] = score.get(n, 0) + 2
    for ns in (esc.get("by_pin") or {}).values():
        for n in ns:
            score[n] = score.get(n, 0) + 1
    out = []
    for n in own:
        if n not in out and n not in keep_out and not is_ground(n):
            out.append(n)
    for n, _ in sorted(score.items(), key=lambda kv: (-kv[1], kv[0])):
        if len(out) >= limit:
            break
        if n in out or n in keep_out or is_ground(n) or pads.get(n, 0) > RIPUP_MAX_PADS:
            continue
        if real and n not in real:
            continue          # a blocker label ('track', 'via', 'edge'), not a net: nothing to rip
        out.append(n)
    return out


def _ripup_guided(pr, sig: list[str], budget: float, while_open: str, rip: list[str]) -> dict:
    """Guided router with ripup='nets': rip up ``rip`` and re-route them (and what they were blocking)."""
    from . import guided
    plan = {"global": {"layers": list(sig), "time_budget_s": int(max(15, budget)), "ripup_rounds": 6, "via_cost": 2}}
    return guided.route(pr, plan=plan, ripup="nets", nets=sorted(rip), while_open=while_open)


def _ripup_keep_out(ignore, others, zone_nets, only, all_nets, pre_copper) -> set:
    """Nets the finisher rip-up must never rip: ignored, frozen (ECO), pour nets, every net outside an ECO's
    ``only`` and every net that had copper before this call (``pre_copper``) — only copper this call created
    is ever ripped up and re-routed."""
    out = set(ignore) | set(others) | set(zone_nets) | set(pre_copper)
    if only:
        out |= {n for n in all_nets if n not in only}
    return out


def finisher_ripup(pr, failed: list[dict], keep_out: set, sig: list[str], unc0: int, ignore: set, budget: float,
                   while_open: str = "refuse", err0: int | None = None, open_nets: set | None = None) -> tuple[dict, dict]:
    """Targeted rip-up after the guided finisher left ``unc0`` connections open (``failed``: its failed list).
    Rips up the failed nets plus the nets crossing their pins' escape regions (_ripup_nets) and re-routes them
    with the guided router (ripup='nets'). The result stays on the board only when unrouted (not counting
    ``ignore``) drops below ``unc0`` AND KiCad DRC has no more errors than before (``err0``, measured when None);
    otherwise the board file is put back byte for byte. Returns (log entry, board stats)."""
    t0 = time.time()
    pcb = pr.pcb
    backup = Path(tempfile.mkdtemp(prefix="copper-ripup-"))
    shutil.copy2(pcb, backup / pcb.name)
    entry, stats = {}, {}
    try:
        if open_nets:
            failed = [f for f in failed if f.get("net") in open_nets]
        rip = _ripup_nets(pr, failed, keep_out)
        if not rip:
            return {"skipped": "no net to rip up (the failed nets are pour / ground / frozen nets)"}, {}
        entry["nets"] = rip
        if err0 is None:
            err0 = board.drc(pr, parity=False, record=False)["errors"]
        _ripup_guided(pr, sig, budget, while_open, rip)
        stats = _board_stats(pcb)
        unc = _unrouted(stats, ignore)
        entry["unrouted"] = unc
        why = "no gain" if unc >= unc0 else None
        if why is None:
            err = board.drc(pr, parity=False, record=False)["errors"]
            if err > err0:
                why = f"added DRC errors ({err0} -> {err})"
        if why:
            entry["discarded"] = why
        else:
            entry["kept"] = True
    except Exception as e:  # noqa: BLE001 — optional: the board before it stands
        entry["failed"] = (getattr(e, "message", None) or str(e))[:200]
    finally:
        if not entry.get("kept"):
            shutil.copy2(backup / pcb.name, pcb)      # the previous board, exactly
        shutil.rmtree(backup, ignore_errors=True)
        entry["seconds"] = round(time.time() - t0, 1)
    return entry, stats


def _shrink_margins(m: float) -> list[float]:
    """Margins tried, in order, when a full-board pour is shrunk to the hull of what it must reach."""
    m = max(0.2, float(m))
    return [m, m + 2.0, m + 5.0]


def _openable_layers(layers: list[str], sig: list[str], inner_zones: dict) -> list[str]:
    """Inner layers layer_policy='auto' may open to signals: not already signal layers, and every pour on
    them is on a named, non-ground net (a GND plane is never opened)."""
    from .place import is_ground
    return [x for x in layers if x not in ("F.Cu", "B.Cu") and x not in sig
            and not any(is_ground(z.get("net") or "") or not z.get("net") for z in inner_zones.get(x, []))]


def _pour_watch(cands: list[str], inner_zones: dict) -> set:
    """Pour nets on the layers a layers round opens. They are checked even when ignored or frozen: shrinking
    their pour, or routing across it, can cut the plane in two."""
    return {z["net"] for lay in cands for z in inner_zones.get(lay, []) if z.get("net")}


def _split_nets(before: dict, after: dict, watch) -> list[str]:
    """Nets of ``watch`` with more unconnected items in ``after`` than in ``before`` (e.g. a pour cut in two)."""
    b0, b1 = before.get("unconnected_by_net"), after.get("unconnected_by_net")
    if b0 is None or b1 is None:
        return []
    return sorted(n for n in watch if (b1.get(n) or 0) > (b0.get(n) or 0))


# what one Freerouting round writes into the run state for the final report: put back when a round is discarded
_ROUND_KEYS = ("patched", "fan", "dropped", "keepouts", "ex_info", "info")

CONTINUE_MIN = 20   # a 'continue' round is only worth its time when at least this many connections are left


def _preflight(pr, ignore, only, cap_s: float) -> dict | None:
    """Routability preflight (pcb.preflight). A crash in it never blocks routing: it is reported as skipped."""
    from . import preflight
    try:
        return preflight.check(pr, ignore=ignore, nets=only, cap_s=cap_s)
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        return {"ok": True, "skipped": f"preflight could not run: {str(e)[:160]}"}


def autoroute(pr, passes: int = 30, timeout: int = 600, clear_existing: bool = False,
              ignore_nets: list[str] | None = None, while_open: str = "refuse", progress=None,
              rounds: int = 3, signal_layers: list[str] | None = None, keep_worse: bool = False,
              fanout: bool = True, finish: bool = True, nets: list[str] | None = None, preflight: bool = True,
              force: bool = False, preflight_cap: float = 15.0, continue_min: int | None = None,
              layer_policy: str = "planes_only", finish_ripup: bool = True, plane_margin: float = 1.0) -> dict:
    """Route with Freerouting, then finish, within one overall ``timeout`` budget:

    1. route — Freerouting on everything unrouted (existing copper is exported as fixed, so only new
       wiring comes back);
    2. continue — Freerouting again on what is left, with the finished nets frozen;
    3. guided — the guided router (``pcb.guided.route``) on what Freerouting could not finish
       (``finish``); its result is kept only if it routes more without adding DRC errors;
    4. ripup — only on boards a full route takes under a minute, and only when no hand-made tracks
       would be lost: rip everything up and route again with more passes (then 3 again).

    The stage with the fewest unrouted connections is kept. If the result has more DRC errors and no
    fewer unrouted connections than the board had before, the board is restored (unless keep_worse).

    nets: route only these nets (an ECO). Every other net leaves the DSN network and its copper becomes
    a keep-out, so it is never moved; the unrouted counts that steer the rounds are of these nets only.
    Round 2 ('continue') is skipped when round 1 left fewer than ``continue_min`` connections or made no
    progress: those go straight to the guided finisher (Freerouting 'continue' rounds on a nearly finished
    board cost minutes and close a handful). preflight: before anything is written, prove routability
    (pcb.preflight, capped at ``preflight_cap`` s); walled-in pads raise ROUTE_PREFLIGHT_FAILED unless
    ``force``, congestion is only reported. Each Freerouting round logs to .copper/freerouting-round<N>.log
    (freerouting.log = the latest).

    layer_policy: 'planes_only' (default) keeps every inner plane layer free of traces. 'auto': when
    connections are still open after the main pass, each inner layer whose pours are all non-ground (a
    GND plane is never opened) becomes a signal layer for one more Freerouting round on just the leftover
    nets; a pour covering (nearly) the whole board on it is first shrunk to the convex hull of the copper it
    must reach (+ ``plane_margin`` mm). That round (and the board change) is kept only if unrouted drops, DRC
    errors do not rise and no pour net of an opened layer (ignored or frozen ones too) loses a connection. A pour already restricted (pcb_pour(region=...)) is used as it is.
    finish_ripup: when the guided finisher leaves connections open, retry once with ripup='nets' on the
    failed nets plus the nets crossing their pins' escape regions — never a net that had copper before this
    call (unless clear_existing), a pour net or a frozen net; kept only if unrouted drops and DRC errors do
    not rise, else the previous board is restored."""
    t_start = time.time()
    pcb = pr.pcb
    if layer_policy not in ("planes_only", "auto"):
        raise ToolError(f"layer_policy must be 'planes_only' or 'auto', not {layer_policy!r}", code="BAD_ARGUMENT",
                        fix="layer_policy='auto' opens non-ground inner pours to signals when connections remain")
    if not pcb.exists():
        raise ToolError("no board yet", code="NO_BOARD", fix="pcb_sync() then pcb_setup() and pcb_place(auto=true)")
    projmod.guard_writable(pcb, while_open)
    if progress:
        progress(0.02, "checking unrouted connections")
    before = board.drc(pr, parity=False)
    snap0 = board.read(pr, what=["summary", "copper_nets"])
    ignore = set(ignore_nets or [])
    only = (set(nets or []) - ignore) or None
    if nets and not only:
        raise ToolError("every net in nets=[...] is also in ignore_nets: nothing to route", code="BAD_ARGUMENT",
                        fix="drop those nets from ignore_nets, or leave nets out to route the whole board")
    # nets with copper from before this call: the finisher rip-up never touches them (existing tracks stay as they are)
    pre_copper = set(snap0.get("copper_nets") or ())
    if clear_existing:
        pre_copper = pre_copper - only if only else set()
    zone_nets = {z.get("net") for z in snap0.get("zones") or [] if z.get("net") and not z.get("rule_area")}
    continue_min = CONTINUE_MIN if continue_min is None else continue_min
    if not clear_existing and before["unconnected"] == 0:
        return {"checkpoint": None, "rounds": [], "router": None, "seconds": 0, "timed_out": False,
                "nets_left_to_pour": 0, "copper_graphic_keepouts": 0, "nothing_to_route": True,
                "unrouted_before": 0, "unrouted_after": 0, "tracks_added": 0, "vias_added": 0,
                "track_length_mm": snap0.get("track_length_mm"), "drc": board.drc(pr, parity=True),
                "revision": projmod.revision(pcb)}
    if only and not clear_existing:
        by_net = _board_stats(pcb).get("unconnected_by_net")
        if by_net is not None and not any(by_net.get(n) for n in only):
            return {"checkpoint": None, "rounds": [], "router": None, "seconds": round(time.time() - t_start, 1),
                    "timed_out": False, "nets_left_to_pour": 0, "copper_graphic_keepouts": 0, "nothing_to_route": True,
                    "nets_routed": sorted(only), "unrouted_before": before["unconnected"],
                    "unrouted_after": before["unconnected"], "tracks_added": 0, "vias_added": 0,
                    "track_length_mm": snap0.get("track_length_mm"), "drc": board.drc(pr, parity=True),
                    "revision": projmod.revision(pcb)}
    pre = None
    if preflight and not clear_existing:
        if progress:
            progress(0.03, "preflight: can every pad get out?")
        pre = _preflight(pr, ignore, only, preflight_cap)
        if pre and not pre.get("ok") and not force:
            w = pre["walled"]
            raise ToolError(
                f"routing preflight: {pre['walled_total']} pad(s) are walled in and cannot be connected by any router in "
                f"this placement (first: {w[0]['pad']} on {w[0]['net']} at {w[0]['at']}). Freerouting was not started; "
                "the board is unchanged.", code="ROUTE_PREFLIGHT_FAILED",
                fix=(pre["suggested_moves"][0]["try"] if pre.get("suggested_moves") else "move the parts around those pads")
                + "; pcb_route(force=true) routes anyway",
                data={"preflight": {k: pre[k] for k in ("walled", "walled_total", "hot_spots", "over_capacity",
                                                        "suggested_moves", "seconds") if k in pre}})
    cp = projmod.checkpoint(pr, "before-autoroute")
    backup = Path(tempfile.mkdtemp(prefix="copper-route-"))
    shutil.copy2(pcb, backup / pcb.name)
    work = Path(tempfile.mkdtemp(prefix="copper-fr-"))
    rounds_log, downloads = [], []
    st = {"info": {}, "dropped": 0, "keepouts": 0, "patched": {}, "fan": None, "first": None, "dups": 0,
          "sig": None, "n": 0, "guided": None, "others": set(), "pre_unc": None, "frozen": 0,
          "round_only": None, "round_sig": None, "ex_info": {}, "opened": None, "ripup": None, "failed": None}
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
        if clear_existing and rnd == 1 and only:
            # nets=[...] + clear_existing: re-route just those nets from scratch; every other net keeps its copper
            ops.append({"op": "delete", "nets": sorted(only)})
        elif (clear_existing and rnd == 1) or strategy == "ripup":
            ops.append({"op": "delete", "all_tracks": True})
        # nets with an inner plane: a stub + via per SMD pad (Freerouting will not drop vias into a plane)
        r_only = st["round_only"] or only     # a layers round re-routes only the leftover nets
        if fanout:
            ops.append({"op": "fanout", "planes": True, **({"only": sorted(r_only)} if r_only else {})})
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
        if r_only:
            # an ECO route: every net outside `nets` is frozen like a finished one (its copper a keep-out)
            others = [n for n in ex_info.get("nets", []) if n not in r_only and n not in ignore and n not in done]
            if not st["round_only"]:
                st["others"] = set(others)
                st["all_nets"] = list(ex_info.get("nets", []))
                st["frozen"] = len(others)
            done = done + others
        if st["pre_unc"] is None and by0 is not None:
            st["pre_unc"] = _unrouted(aft, ignore | st["others"])
        if fanout:
            fo = ex["results"][-2]
            if fo.get("fanout_vias") or fo.get("thermal_vias") or fo.get("failed"):
                st["fan"] = {k: fo[k] for k in ("fanout_vias", "thermal_vias", "failed") if fo.get(k)}
        st["dropped"] = _patch_dsn(dsn, sorted(ignore))
        _patch_dsn(dsn, done)
        _wiring_to_keepouts(dsn, done)
        st["keepouts"] = _add_keepouts(dsn, ex_info.get("copper_graphics") or [])
        st["ex_info"] = {k: ex_info.get(k) for k in ("copper_layers", "planes", "inner_zones")}
        planes, sig = _signal_layers(ex_info, st["round_sig"] or signal_layers)
        patched = _patch_rules(dsn, clearance=ex_info.get("max_clearance"),
                               neck={k: v for k, v in (ex_info.get("neck") or {}).items() if k not in ignore},
                               power_layers=planes)
        patched["signal_layers"] = st["sig"] = sig
        _edge_keepouts(dsn, (ex_info.get("edge_clearance") or 0) - (ex_info.get("max_clearance") or 0) + 0.01
                       if (ex_info.get("edge_clearance") or 0) > (ex_info.get("max_clearance") or 0) else 0, sig)
        st["patched"] = patched
        log = pr.copper_dir / f"freerouting-round{rnd}.log"
        info = st["info"] = freerouting.run(dsn, ses, passes=npass, timeout=int(max(10, cap)),
                                            progress=progress if rnd == 1 else None, log_file=log)
        try:   # freerouting.log stays the latest round's log, as before
            shutil.copy2(log, pr.copper_dir / "freerouting.log")
        except OSError:
            pass
        if not info["ses_written"]:
            why = info.get("crashed") or ("timed out" if info["timed_out"] else f"exit {info['returncode']}")
            raise ToolError(f"Freerouting produced no session file ({why}). Last output: "
                            f"{' | '.join(info['log_tail'][-3:])[:400]}", code="ROUTE_FAILED",
                            fix="pcb_route(passes=10) or pcb_place(auto=true, spacing=1.0) to give the router "
                                f"more room; full log: {log}")
        if st["first"] is None:
            st["first"] = (info["seconds"], max(1, int(info.get("passes_done") or npass)))
        ops = [{"op": "import_ses", "path": str(ses)}, {"op": "heal", **({"nets": sorted(r_only)} if r_only else {})}]
        if snap0.get("zones"):
            ops.append({"op": "fill"})
        imp = worker.run("board", {"pcb": str(pcb), "ops": ops, "save": True, "by_net": True})
        r0 = imp["results"][0]
        st["dups"] += int(r0.get("stale_duplicates") or 0)
        unc = _unrouted(imp["after"], ignore | st["others"])
        rounds_log.append({"round": rnd, "strategy": strategy, "seconds": info["seconds"],
                           "unrouted": unc, "timed_out": info["timed_out"], "log": log.name}
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
        st["failed"] = None
        shutil.copy2(work / "best.kicad_pcb", pcb)
        t0 = time.time()
        err0 = None
        entry = {"round": st["n"], "strategy": "guided"}
        try:
            say(0.8, f"guided router: finishing {best[0]} connections Freerouting left")
            err0 = board.drc(pr, parity=False, record=False)["errors"]
            g = _finish_guided(pr, st["sig"] or ["F.Cu", "B.Cu"], ignore, min(120.0, left - 20), while_open,
                               **({"only": only, "all_nets": st.get("all_nets") or ()} if only else {}))
            stats = _board_stats(pcb)
            unc = _unrouted(stats, ignore | st["others"])
            entry |= {"seconds": round(time.time() - t0, 1), "unrouted": unc}
            st["failed"] = list(g.get("failed") or [])
            if unc < best[0] and (g.get("drc") or {}).get("errors", 0) <= err0:
                st["guided"] = {"routed": best[0] - unc,
                                "failed": [{k: f[k] for k in ("net", "from", "to", "reason", "where") if k in f}
                                           for f in (g.get("failed") or [])][:20]}
                keep(unc, stats)
                err0 = None       # the board changed: the rip-up measures its own baseline
            else:
                entry["discarded"] = "no gain" if unc >= best[0] else "added DRC errors"
        except Exception as e:  # noqa: BLE001 — the finisher is optional: Freerouting's result stands
            entry["failed"] = (getattr(e, "message", None) or str(e))[:200]
        rounds_log.append(entry)
        if finish_ripup and st["failed"] and best[0]:
            ripup_stage(err0)

    def ripup_stage(err0):
        """The finisher left connections open: a targeted rip-up (finisher_ripup). Kept only when it is better."""
        left = deadline - time.time()
        if left < 40:
            rounds_log.append({"round": st["n"] + 1, "strategy": "guided-ripup", "skipped": "time budget used up"})
            return
        shutil.copy2(work / "best.kicad_pcb", pcb)
        keep_out = _ripup_keep_out(ignore, st["others"], zone_nets, only, st.get("all_nets") or (), pre_copper)
        say(0.85, "guided router: targeted rip-up around the open connections")
        entry, stats = finisher_ripup(pr, st["failed"], keep_out, st["sig"] or ["F.Cu", "B.Cu"], best[0],
                                      ignore | st["others"], min(90.0, left - 30), while_open, err0=err0,
                                      open_nets={n for n, v in (best[2].get("unconnected_by_net") or {}).items() if v})
        if entry.get("nets"):
            st["n"] += 1
        entry = {"round": st["n"], "strategy": "guided-ripup"} | entry
        if entry.get("kept"):
            st["ripup"] = {"nets": entry["nets"], "routed": best[0] - entry["unrouted"]}
            keep(entry["unrouted"], stats)
        rounds_log.append(entry)

    def layers_stage():
        """layer_policy='auto': open the non-ground inner pour layers to signals for the leftover nets."""
        nonlocal best
        left = deadline - time.time()
        entry = {"strategy": "open_layers"}
        exi = st["ex_info"] or {}
        layers = exi.get("copper_layers") or []
        zones = exi.get("inner_zones") or {}
        sig0 = list(st["sig"] or [])
        cands = _openable_layers(layers, sig0, zones)
        leftover = sorted(n for n, v in (best[2].get("unconnected_by_net") or {}).items()
                          if v and n not in ignore and n not in st["others"] and n not in zone_nets)
        if not cands:
            rounds_log.append(dict(entry, skipped="no inner layer can be opened (GND planes are never opened)"))
            return
        if not leftover:
            rounds_log.append(dict(entry, skipped="only pour nets are left open"))
            return
        if left < 60:
            rounds_log.append(dict(entry, skipped="time budget used up"))
            return
        rounds_log.append(entry)
        prev_best = best
        saved = {k: st[k] for k in _ROUND_KEYS}
        # the opened layers' pour nets count here even when ignored / frozen: a split plane is no gain
        watch = _pour_watch(cands, zones)
        skip = (ignore | st["others"]) - watch
        shutil.copy2(work / "best.kicad_pcb", pcb)
        shutil.copy2(work / "best.kicad_pcb", work / "before-layers.kicad_pcb")
        t0 = time.time()
        kept = False
        try:
            err0 = board.drc(pr, parity=False, record=False)["errors"]
            shrunk = []
            full = [(lay, net) for lay in cands for net in sorted({z["net"] for z in zones.get(lay, [])
                                                                   if z.get("single_layer") and (z.get("frac") or 0) >= 0.9})]
            if full:
                # the shrunk pour must still join every pad / via of its net: copper of other nets already on that
                # layer can cut a tight hull in two, so a wider margin is tried before giving up
                base = _unrouted(best[2], skip)
                for k, margin in enumerate(_shrink_margins(plane_margin)):
                    if k:
                        shutil.copy2(work / "before-layers.kicad_pcb", pcb)
                    ops = [{"op": "zone_region", "net": net, "layer": lay, "region": "auto", "margin": margin,
                            "min_shrink": 0.95} for lay, net in full]
                    res = worker.run("board", {"pcb": str(pcb), "ops": ops + [{"op": "fill"}], "save": True, "by_net": True})
                    unc_shrunk = _unrouted(res["after"], skip)
                    if unc_shrunk <= base and not _split_nets(best[2], res["after"], watch):
                        break
                else:
                    raise ToolError(f"shrinking the pour opened {max(1, unc_shrunk - base)} connections of its own net",
                                    code="PLANE_SHRINK")
                for r in res["results"][:-1]:
                    if r.get("zones"):
                        shrunk.append({"layer": r["layer"], "net": r["net"], "area_before_mm2": r.get("area_before_mm2"),
                                       "area_after_mm2": r["area_mm2"], "margin_mm": margin, "outline": r["polygon"]})
            entry["opened"] = [{"layer": lay, "pour_nets": sorted({z["net"] for z in zones.get(lay, [])})}
                               | ({"shrunk": [s for s in shrunk if s["layer"] == lay]}
                                  if any(s["layer"] == lay for s in shrunk) else {}) for lay in cands]
            entry["leftover_nets"] = len(leftover)
            st["round_only"] = set(leftover)
            st["round_sig"] = [x for x in layers if x in sig0 or x in cands]
            say(0.82, f"Freerouting (inner layers {', '.join(cands)} opened): {best[0]} connections")
            was = best[1]
            try:
                ok_ = stage("layers", min(left - 30, max(90.0, 2 * (st["first"] or (60, 1))[0] + 30)), passes)
            finally:
                st["round_only"] = None
            if ok_ and best[1] != was:
                split = _split_nets(prev_best[2], best[2], watch)
                if split:
                    raise ToolError(f"split the pour of {', '.join(split)}", code="LAYERS_SPLIT")
                err = board.drc(pr, parity=False, record=False)["errors"]
                if err > err0:
                    raise ToolError(f"added DRC errors ({err0} -> {err})", code="LAYERS_DRC")
                kept = True
                st["opened"] = entry["opened"]
                mid, mid_st = best, {k: st[k] for k in ("guided", "ripup", "failed")}
                shutil.copy2(work / "best.kicad_pcb", work / "after-layers.kicad_pcb")
                guided_stage()        # the finisher again, now with the opened layers
                split = _split_nets(mid[2], best[2], watch) if best is not mid else []
                if split:             # the finisher cut a pour: back to the layers round's board
                    best = mid
                    st.update(mid_st)
                    shutil.copy2(work / "after-layers.kicad_pcb", work / "best.kicad_pcb")
                    shutil.copy2(work / "after-layers.kicad_pcb", pcb)
                    entry["finisher_discarded"] = f"split the pour of {', '.join(split)}"
            else:
                entry["discarded"] = "no gain"
        except Exception as e:  # noqa: BLE001 — optional: the board before it stands
            entry["discarded" if isinstance(e, ToolError) else "failed"] = (getattr(e, "message", None) or str(e))[:200]
        if not kept:
            best = prev_best
            shutil.copy2(work / "before-layers.kicad_pcb", work / "best.kicad_pcb")
            shutil.copy2(work / "before-layers.kicad_pcb", pcb)
            st["sig"] = sig0
            st.update(saved)      # the report describes the board that was kept, not the discarded round
        st["round_sig"] = None
        entry["kept"] = kept
        entry["total_seconds"] = round(time.time() - t0, 1)

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
            pre_unc = st["pre_unc"]
            if best[0] < continue_min or (pre_unc is not None and best[0] >= pre_unc):
                st["n"] += 1          # the skipped round keeps its number: round numbers mean the same on every run
            if best[0] < continue_min:
                rounds_log.append({"round": st["n"], "strategy": "continue",
                                   "skipped": f"only {best[0]} connections left (< {continue_min}): straight to the "
                                              "guided finisher"})
            elif pre_unc is not None and best[0] >= pre_unc:
                rounds_log.append({"round": st["n"], "strategy": "continue",
                                   "skipped": f"round 1 made no progress ({pre_unc} -> {best[0]})"})
            elif left < 20:
                rounds_log.append({"round": st["n"] + 1, "strategy": "continue", "skipped": "time budget used up"})
            else:
                # later rounds get a share of the budget, not all of it: a router that hangs after a
                # crash must not eat the whole timeout
                say(0.4, "Freerouting round 2 (continue): routing")
                ok = stage("continue", min(left, max(60.0, 2 * st["first"][0] + 30)), passes)
        guided_stage()
        if (layer_policy == "auto" and not signal_layers and best is not None and best[0]
                and not st["info"].get("crashed")):
            layers_stage()
        # rip-up-and-retry: a second opinion that is only cheap on small boards, and never when it would
        # throw away tracks that were on the board before this call
        info = st["info"]
        if (ok and best[0] and rounds >= 3 and not only and not st["opened"] and (clear_existing or not snap0.get("tracks"))
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
            "nets_routed": sorted(only) if only else None, "nets_frozen": st["frozen"] or None,
            "preflight": _pre_brief(pre),
            "layer_policy": layer_policy if layer_policy != "planes_only" else None,
            "planes_opened": (st["opened"] if not reverted else None),
            "finish_ripup": (st["ripup"] if not reverted else None),
            "revision": projmod.revision(pcb)}


def _pre_brief(pre: dict | None) -> dict | None:
    if not pre:
        return None
    if pre.get("skipped"):
        return {"skipped": pre["skipped"]}
    out = {"ok": pre["ok"], "seconds": pre.get("seconds"), "pads_checked": pre.get("checked_pads")}
    if not pre.get("complete", True):
        out["partial"] = "time cap reached: not every pad was checked"
    if pre.get("walled_total"):
        out["walled"] = pre["walled"][:5]
        out["forced"] = True
    if pre.get("over_capacity"):
        out["congested"] = pre["over_capacity"][:3]
    return out
