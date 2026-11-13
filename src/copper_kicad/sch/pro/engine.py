"""Professional schematic re-layout with a netlist-identity guarantee.

relayout() = baseline (staged copy of the whole project) -> candidate layouts, best first:
    1 "pro"            blocks + stems + banks + in-block wires
    2 "pro-plain"      the same without the optional in-block router
    3 "labels"         the older label-style tidy (only for sheets a machine drew)
-> each candidate must pass the geometric pre-check and the gate (G0-G4, sch/pro/gate.py) -> the first that passes is
written. If none passes, nothing is written. Deterministic: no randomness, no model calls, only local kicad-cli.
"""
from __future__ import annotations

import itertools
import math
import time
from pathlib import Path

from ... import project as P
from ... import sexpr
from ...kicad import env
from ...mcp.registry import ToolError
from .. import ops as O
from ..model import PAPER, Schematic, text_width
from . import emit as E
from . import flow as FLOW
from . import gate, notes as NOTES, page as PAGE, route, score
from . import symfix as SYMFIX
from . import text as TEXT
from .blocks import Design, _refkey, prefix, sheet_view
from . import build as BUILD
from .build import Blk, Cell, U, hit, lbl_box, power_box, power_boxes, snap, strip_cell, wbox

PAPERS = ["A4", "A3", "A2", "A1", "A0"]
MARGIN = 12.7             # 10 grid units: the first frame may start on the grid line next to the sheet border
PADF = 3 * U              # air between a block's content and its frame
PADV = 2 * U + 0.6        # ... above and below (the title strip already adds air on top)
TITLE_H = 5.08            # title strip inside the frame
BLOCK_GAP = 5.08
# package D (page composition): a frame hugs what is drawn in it. The size of a block is first an estimate (boxes
# reserved while it is built); the finished drawing is measured (`_fit_measure`) and the sheet is composed once more
# with the measured sizes (`relayout`: FIT_PASSES more drawings at most)
FIT_PASSES = 2
FIT_PAD = 3.3             # air between the ink of a block and its frame: left, right, bottom, and under the title strip
FIT_TOP = 2.2
FIT_TIGHT = 2.54          # ... on a sheet that would otherwise have to be the next larger paper
FIT_MIN = 1.2             # a frame drawn closer than this to its content (or through it) is not taken
# fix round 3 (docs/internal/SCHEMATIC_PRO_V2_DESIGN.md 17)
SWITCH_NAMES = True       # the doubled contact of a tactile switch: a name at the far pin, not a wire box around the part
BUS_ALL_OR_NONE = True     # a bus between two parts is wired completely or named completely
NOTES_AT_TITLE_BLOCK = True   # the sheet's NOTES block stands left of the title block, level with its top edge
PAPER_GUARD = True        # class L: a sheet that needs a larger paper because of its bundles is drawn without them (_paper_guard)
GUARD_FROM = "A4"         # ... checked when the sheet came out larger than this
REHOME = True             # class L: an island pull-up / filter cap is drawn at another block's pin of its net (_rehome)
SETTLE_WIRED_ONLY = True  # fix round 8: a frame is only moved level with rows it can be WIRED to (_settle.reach)
SIDE_SYM_TIGHT = True     # a sideways supply symbol next to a pin no longer excuses a wire around the part


class LayoutError(Exception):
    pass


class Undock(Exception):
    """pro2: these connectors were drawn at the part they serve, in a block that ends up in a middle column. The
    sheet is drawn again with each of them as a block of its own at the sheet's edge."""

    def __init__(self, refs):
        super().__init__("undock " + ", ".join(refs))
        self.refs = list(refs)


# ───────────────────────── sheet composition ─────────────────────────
COL_GAP_MAX = 5 * BLOCK_GAP


def _free_merge(a: float, b: float) -> bool:
    """Stages that may share a column without hurting the reading order."""
    a, b = sorted((a, b))
    return a == b or (a < 3 and b < 3) or (a >= 90 and b >= 90) or (3 <= a < 90 and 3 <= b < 90 and b - a < 1)


def _affinity(blocks: list) -> dict:
    """{(i, j): number of signal nets blocks i and j share} - which frames belong next to each other."""
    if not blocks:
        return {}
    D = blocks[0].D
    of = {}
    for i, b in enumerate(blocks):
        for r in list(b.info.anchors) + list(b.info.sats):
            of[r] = i
    aff: dict = {}
    for k, ln in D.view.nets.items():
        if k in D.rails or not D.conn(k):
            continue
        bs = sorted({of[p[0]] for p in ln.pins if p[0] in of})
        for a in range(len(bs)):
            for c in range(a + 1, len(bs)):
                aff[(bs[a], bs[c])] = aff.get((bs[a], bs[c]), 0) + 1
    return aff


def compose(blocks: list, sizes: dict, paper: str | None, tight: dict | None = None):
    """Place block frames: signal flow left to right in columns (supply in | sources | ICs in driving order | I/O),
    on the smallest paper that holds them, clear of the title block. Every way of cutting the flow-ordered block
    list into columns is tried; the connectors the design talks through may form the last column or sit under the
    column of the IC they serve. The arrangement that mixes the fewest stages and uses the page most evenly wins.
    Frames of a column share its width, columns share the top line (and the bottom line when little is missing)
    and are spread across the sheet.  -> (paper, {id(block): (x, y, w, h)})."""
    order = sorted(blocks, key=lambda b: (getattr(b.info, "stage", b.info.rank), b.info.rank, _refkey(b.info.id)))
    if not order:
        return None, None
    aff0 = _affinity(order)
    aff = {}
    for (a, b), n in aff0.items():
        aff[(a, b)] = aff[(b, a)] = n
    fallback = None
    for pp in ([paper] if paper else PAPERS):
        best = _compose_paper(order, sizes, pp, aff)
        if best is None and tight:                        # a few millimetres short: less air inside the frames
            best = _compose_paper(order, tight, pp, aff)
        if best:
            if fallback is not None:
                # the bigger sheet only when it really untangles the flow - a half-empty A3 is no improvement
                return (pp, best[1]) if (fallback[2] > 2.6 and best[2] < fallback[2] - 0.75) else fallback[:2]
            if best[2] <= 1.6 or paper or pp == PAPERS[-1]:
                return pp, best[1]
            fallback = (pp, best[1], best[2])             # crowded: one size up may keep the flow in clean columns
    return fallback[:2] if fallback else (None, None)


def _compose_paper(order: list, sizes: dict, pp: str, aff: dict):
    """Best arrangement of the flow-ordered blocks on one paper -> (cost, positions, stage-mixing) or None."""
    n = len(order)
    stage = [getattr(b.info, "stage", b.info.rank) for b in order]
    W = [sizes[id(b)][0] for b in order]
    H = [sizes[id(b)][1] for b in order]
    core = [k for k in range(n) if stage[k] < 90]
    tail = [k for k in range(n) if stage[k] >= 90]
    if not core:
        core, tail = tail, []
    pw, ph = PAPER.get(pp, PAPER["A3"])
    ax0, ay0, ax1, ay1 = MARGIN, MARGIN, pw - MARGIN, ph - MARGIN
    tb = [pw - 10 - 112 - 4, ph - 10 - 36 - 4, pw, ph]
    aw, ah = ax1 - ax0, ay1 - ay0
    if sum(W[k] * H[k] for k in range(n)) > aw * ah or max(W) > aw + 0.01 or max(H) > ah + 0.01:
        return None
    best = None
    tailset = set(tail)
    base_pen = 0.0
    vgap = BLOCK_GAP

    def shelve(c, wide):
        """Rows of one column: neighbours that fit side by side within the column's width share a row."""
        if not wide:
            return [[k] for k in c]
        lim_w = max(W[k] for k in c)
        rows = []
        for k in c:
            r = rows[-1] if rows else None
            fits = bool(r) and sum(W[t] for t in r) + BLOCK_GAP * len(r) + W[k] <= lim_w + 0.01
            if fits and (k in tailset or _free_merge(stage[r[-1]], stage[k])):
                r.append(k)
            else:
                rows.append([k])
        return rows

    def evaluate(cols, own_tail, key2, wide=False):
        nonlocal best
        rows = [shelve(c, wide) for c in cols]
        if wide and all(len(r) == 1 for rs in rows for r in rs):
            return                                        # nothing shares a row: the plain variant covers it
        widths = [max(sum(W[k] for k in r) + BLOCK_GAP * (len(r) - 1) for r in rs) for rs in rows]
        rh = [[max(H[k] for k in r) for r in rs] for rs in rows]
        heights = [sum(h) + vgap * (len(h) - 1) for h in rh]
        total_w = sum(widths) + BLOCK_GAP * (len(cols) - 1)
        hall = max(heights)
        if total_w > aw + 0.01 or hall > ah + 0.01:
            return
        gap0 = BLOCK_GAP if len(cols) == 1 else min(COL_GAP_MAX, BLOCK_GAP + (aw - total_w) / (len(cols) - 1))
        gap0 = math.floor(gap0 / (2 * U)) * 2 * U
        y0 = None
        left0 = math.ceil(ax0 / (2 * U)) * 2 * U
        # spread and centred when that works; a tall column that would end up over the title block is the reason
        # to pack the columns to the left instead
        for gap, centred in ((gap0, True), (gap0, False), (BLOCK_GAP, True), (BLOCK_GAP, False)):
            span = sum(widths) + gap * (len(cols) - 1)
            x0 = snap(ax0 + (aw - span) / 2, 2 * U) if centred else left0
            if x0 + span > ax1 + 0.01:
                continue
            xs, x = [], x0
            for w in widths:
                xs.append(x)
                x += w + gap
            # columns under which the title block sits must end above it
            lim = [(tb[1] - BLOCK_GAP / 2 if xs[c] + widths[c] > tb[0] else ay1) for c in range(len(cols))]
            low = min([lim[c] - heights[c] for c in range(len(cols))] + [ay0 + (ah - hall) / 2])
            over_tb = any(l < ay1 for l in lim)            # then the free area ends at the title block
            c_page, c_tb = snap(ay0 + (ah - hall) / 2, 2 * U), snap(ay0 + max(0.0, (tb[1] - ay0 - hall) / 2), 2 * U)
            for cand in ((c_tb, c_page) if over_tb else (c_page, c_tb)) + (
                         math.floor(low / (2 * U)) * 2 * U, snap(ay0, 2 * U) + 2 * U,
                         snap(ay0, 2 * U)):
                if cand >= ay0 - 0.01 and all(cand + heights[c] <= lim[c] + 0.01 for c in range(len(cols))):
                    y0 = cand
                    break
            if y0 is not None:
                break
        if y0 is None:
            return
        mix = 0.0
        lonely = 0.0
        for ci, c in enumerate(cols):
            cc = [k for k in c if k not in tailset or own_tail]
            for a, b in zip(cc, cc[1:]):
                if not _free_merge(stage[a], stage[b]):
                    # a connector that only feeds this very block (sensor input over its amplifier) is no mixing
                    mix += 0.25 if (stage[a] == 2.0 and aff.get((a, b), 0) > 0) else 1.0
                elif 3 <= stage[a] < 90 and 3 <= stage[b] < 90:
                    mix += 0.2                            # two ICs read better side by side than stacked
            if not own_tail:
                for k in c:
                    if k in tailset:
                        here = sum(aff.get((k, o), 0) for o in c if o != k)
                        near = max([sum(aff.get((k, o), 0) for o in c2) for c2 in cols if c2 is not c] or [0])
                        if here == 0 and near > 0:
                            lonely += 0.35                # a connector parked under a block it has nothing to do with
                        elif here < near:
                            lonely += 0.15
                        if ci == 0 and len(cols) > 1 and stage[c[0]] < 3:
                            lonely += 0.2                 # outputs do not belong under the supply input
        rag = sum(hall - h for h in heights) / (hall * len(cols))          # 0 = all columns equally tall
        fill_w, fill_h = span / aw, hall / ah
        aspect = abs(math.log((span / hall) / (aw / ah)))
        cost = (base_pen + mix + lonely + rag * 1.2 + aspect * 0.6 + (1 - min(fill_w, fill_h)) * 0.4
                + (0.8 if len(cols) == 1 and n > 2 else 0.0))
        key = (round(cost, 6), wide, key2)
        if best is None or key < best[0]:
            pos = {}
            for ci, (rs, cx, cw) in enumerate(zip(rows, xs, widths)):
                # the column ends on the common bottom line when it only lacks a little
                target = min(y0 + hall, lim[ci])
                extra = target - y0 - heights[ci]
                grow = extra / sum(rh[ci]) if 0 < extra <= 0.45 * heights[ci] else 0.0
                y = y0
                for r, h0 in zip(rs, rh[ci]):
                    hk = max(h0, math.floor(h0 * (1 + grow) / (2 * U)) * 2 * U) if grow else h0
                    # the frames of a row share its height and fill the column's width between them
                    spare = cw - sum(W[k] for k in r) - BLOCK_GAP * (len(r) - 1)
                    x = cx
                    for t, k in enumerate(r):
                        wk = W[k] + math.floor(spare * W[k] / sum(W[q] for q in r) / (2 * U)) * 2 * U
                        if t == len(r) - 1:
                            wk = cx + cw - x
                            if len(r) == 1 and wk > 1.6 * W[k]:
                                wk = W[k]                 # a small block is not blown up into a mostly empty frame
                        pos[id(order[k])] = (x, y, wk, hk)
                        x += wk + BLOCK_GAP
                    y += hk + vgap
            best = (key, pos, base_pen + mix + lonely)

    _ev = evaluate

    def evaluate(cols, own_tail, key2):                  # noqa: F811 - both variants of every arrangement
        _ev(cols, own_tail, key2, False)
        _ev(cols, own_tail, key2, True)

    # the plain reading order first; then with the small IC blocks free to sit under any column (a sensor under
    # the supply column keeps a design on the smaller sheet) - at a price, so it only wins when it must
    movable = [k for k in core if order[k].info.kind == "ic" and W[k] * H[k] <= 0.5 * max(W[q] * H[q] for q in core)]
    variants = [(core, tail, 0.0, BLOCK_GAP)]
    if movable and len(movable) < len(core) and len(tail) + len(movable) <= 4:
        variants.append(([k for k in core if k not in movable], tail + movable, 0.3, BLOCK_GAP))
    variants += [(c, t, bp + 0.1, 3 * U) for c, t, bp, _g in list(variants)]
    for core, tail, base_pen, vgap in variants:
        if best is not None and vgap < BLOCK_GAP and best[2] < 0.75:
            break                                         # frames only move closer when that untangles the flow
        tailset = set(tail)
        nc = len(core)
        if nc <= 11:
            cuts_all = range(1 << (nc - 1))
        else:                                                # many blocks: only cut where the stage changes
            allowed = [k for k in range(nc - 1) if not _free_merge(stage[core[k]], stage[core[k + 1]])] or list(range(nc - 1))
            allowed = allowed[:11]
            cuts_all = [sum(1 << allowed[t] for t in range(len(allowed)) if m >> t & 1) for m in range(1 << len(allowed))]
        for mask in cuts_all:
            cols, cur = [], [core[0]]
            for t in range(1, nc):
                if mask >> (t - 1) & 1:
                    cols.append(cur)
                    cur = []
                cur.append(core[t])
            cols.append(cur)
            if not tail:
                evaluate(cols, True, (mask, 0))
                continue
            # (a) the connectors as columns of their own on the right
            for tmask in (range(1 << (len(tail) - 1)) if len(tail) <= 6 else (0, (1 << (len(tail) - 1)) - 1)):
                tc, cur = [], [tail[0]]
                for t in range(1, len(tail)):
                    if tmask >> (t - 1) & 1:
                        tc.append(cur)
                        cur = []
                    cur.append(tail[t])
                tc.append(cur)
                evaluate(cols + tc, True, (mask, tmask))
            # (b) each connector under one of the columns
            if len(cols) ** len(tail) <= 1300:
                for assign in itertools.product(range(len(cols)), repeat=len(tail)):
                    c2 = [list(c) for c in cols]
                    for k, ci in zip(tail, assign):
                        c2[ci].append(k)
                    evaluate(c2, False, (mask, 1000 + sum(a * (len(cols) ** e) for e, a in enumerate(assign))))
    return (best[0][0], best[1], best[2]) if best else None


# ───────────────────────── the "pro" layout ─────────────────────────
REFIT_BOTH = True         # fix round 6: both second drawings are compared, not the first one that passes
REFIT_KEEPS_TEXT = True   # class P: ... nor may it leave a text touching something that the first drawing kept clear
REFIT_KEEPS_WIRES = True  # fix round 6: the second drawing (measured frame sizes) may not turn a cross-frame wire into names
FLAG_HOME = True          # fix round 6: a PWR_FLAG parked on a strip of its own moves onto the net's wire once that is routed


def _flags_home(D, wires, labels, flags, fixed, hard, pin_tips, frame_boxes) -> int:
    """A PWR_FLAG of a signal net that found no room on the net's stubs was parked on a strip of its own (a name, a
    short wire, the flag): an island beside the circuit. After the router has drawn the net there is usually a
    straight run with room: the flag moves there - upright on the wire, clear of every corner, junction, part, name
    and other wire - and the strip (its wire and its second copy of the name) goes. Only when the net keeps its name
    somewhere else. The lists are changed in place. -> number of flags moved."""
    DIR = {0: (1, 0), 180: (-1, 0), 90: (0, -1), 270: (0, 1)}
    ftw = max(2.6, text_width("PWR_FLAG") + 0.6)
    moved = 0
    for lab in list(fixed):
        k, e, q0, ang = lab
        if ang != 180 or k in D.rails or lab not in labels:
            continue
        q0 = tuple(q0)
        mine = [w for w in wires if w[0] == k]
        strip = [w for w in mine if q0 in (tuple(w[1]), tuple(w[2]))]
        if len(strip) != 1 or abs(strip[0][1][1] - strip[0][2][1]) > 0.01:
            continue
        w0 = strip[0]
        q1 = tuple(w0[2]) if tuple(w0[1]) == q0 else tuple(w0[1])
        fl = [f for f in flags if f[0] == k and tuple(f[1]) == q1]
        if len(fl) != 1 or any(w is not w0 and q1 in (tuple(w[1]), tuple(w[2])) for w in mine):
            continue
        if not any(l is not lab and l[0] == k and l[1][0] == e[0] for l in labels):
            continue                                      # the strip carries the net's only name: it stays
        obst = [h_ for h_ in hard if not (h_[0] - 0.01 <= q1[0] <= h_[2] + 0.01 and h_[1] - 6.0 <= q1[1] <= h_[3] + 6.0)]
        obst += [lbl_box(l[1][0], l[2], DIR[l[3]], l[1][1]) for l in labels if l is not lab]
        ends = {(round(q[0], 2), round(q[1], 2)) for w in wires for q in (w[1], w[2])}
        done = False
        runs = sorted((w for w in mine if w is not w0 and abs(w[1][1] - w[2][1]) < 0.01 and abs(w[1][0] - w[2][0]) >= 4 * U - 0.01),
                      key=lambda w: (-abs(w[1][0] - w[2][0]), w[1][1], w[1][0]))
        for w in runs:
            x0, x1 = sorted((w[1][0], w[2][0]))
            y = w[1][1]
            n_ = int(round((x1 - x0) / U))
            for i in sorted(range(2, n_ - 1), key=lambda i: (abs(i - n_ / 2), i)):
                pt = (snap(x0 + i * U), y)
                if (round(pt[0], 2), round(pt[1], 2)) in ends or route.cell(pt) in pin_tips:
                    continue
                fb = next((f for f in frame_boxes if f[0] < pt[0] < f[2] and f[1] < pt[1] < f[3]), None)
                for fo, sg in (("up", -1), ("down", 1)):
                    box = [pt[0] - ftw / 2, min(pt[1] + sg * 0.45, pt[1] + sg * 5.4), pt[0] + ftw / 2, max(pt[1] + sg * 0.45, pt[1] + sg * 5.4)]
                    if fb is not None and not (fb[0] + 1.0 < box[0] and box[2] < fb[2] - 1.0 and fb[1] + TITLE_H < box[1] and box[3] < fb[3] - 1.0):
                        continue
                    if any(hit(box, o_, 0.0) for o_ in obst):
                        continue
                    if any(w2 is not w and w2 is not w0 and hit(box, wbox(w2[1], w2[2], 0.3), 0.0) for w2 in wires):
                        continue
                    flags[flags.index(fl[0])] = (k, pt, fo)
                    wires.remove(w0)
                    labels.remove(lab)
                    hard.append(box)
                    moved += 1
                    done = True
                    break
                if done:
                    break
            if done:
                break
    return moved


NC_AIR = 0.5              # fix round 6: a cross closer than this to its pin's number ("27x") also gets a lead
NC_ONE_LINE = True        # fix round 8: fields and names on the next row do not stop a lead (see _nc_leads)
NC_LANE = 0.75            # half width of the lane a lead needs (the cross is 1.27 mm wide; was 1.1: a name on the next row blocked it)


def _nc_leads(sch, D, ncs, wires, hard, labels, pin_tips, frame_boxes) -> list:
    """A no-connect cross that would stand on its own pin's number (a short pin carries the number right at its
    tip) goes half a grid step (1.27 mm) out, on a lead of its own - when that step is free of every wire, part, name and other
    cross. Decided after the router: the lead takes nothing a wire needed. `wires` and `pin_tips` are extended."""
    from . import textbox as TB
    at = {route.cell(q): tuple(q) for q in ncs}
    DIR = {0: (1, 0), 180: (-1, 0), 90: (0, -1), 270: (0, 1)}
    lbls = [lbl_box(e[0], q, DIR[ang], e[1]) for _k, e, q, ang in labels]
    rnd = lambda b: tuple(round(v, 2) for v in b)             # noqa: E731
    soft = set()
    if NC_ONE_LINE:
        # the crosses of a part stand in ONE line: a Reference / Value that stands in the way of a lead is re-seated by
        # the text solver afterwards (it keeps fields off crosses), and a name on the next row is no obstacle as long
        # as the cross itself stays clear of it - one cross left on its pin number beside a row of led-out ones
        # read as a mistake
        soft = {rnd(fb) for s_ in sch.symbols(include_power=False) for fb in s_.field_boxes()} | {rnd(b) for b in lbls}
    obst = [o for o in hard if rnd(o) not in soft] + [wbox(a, b, 0.1) for _n, a, b in wires]
    if not NC_ONE_LINE:
        obst += lbls
    out = dict(at)
    h = TB.NC_HALF
    for s in sch.symbols(include_power=False):
        mine = [p for p in s.pins_world() if route.cell(p["xy"]) in at and (s.ref, p["number"]) in D.view.nc
                and not D.conn(D.pins_of[s.ref].get(p["number"]))]
        if not mine:
            continue
        nums = {n_: bx for kind, n_, _t, bx in TB.pin_texts(s) if kind == "pinnum"}
        if not any(nums.get(p["number"]) is not None
                   and hit(nums[p["number"]], [p["xy"][0] - h - NC_AIR, p["xy"][1] - h, p["xy"][0] + h + NC_AIR, p["xy"][1] + h], 0.0)
                   for p in mine):
            continue
        # one part, one way: when one cross of a part would touch its number, every cross of that part gets a lead
        for p in mine:
            c = route.cell(p["xy"])
            k = D.pins_of[s.ref].get(p["number"])
            x, y = p["xy"]
            ox, oy = p["out"]
            end = (snap(x + ox * U), snap(y + oy * U))
            a_, b_ = (x + ox * 0.7, y + oy * 0.7), (end[0] + ox * NC_LANE, end[1] + oy * NC_LANE)
            lane = [min(a_[0], b_[0]) - abs(oy) * NC_LANE, min(a_[1], b_[1]) - abs(ox) * NC_LANE,
                    max(a_[0], b_[0]) + abs(oy) * NC_LANE, max(a_[1], b_[1]) + abs(ox) * NC_LANE]
            if NC_ONE_LINE and any(hit([end[0] - h, end[1] - h, end[0] + h, end[1] + h], b_, 0.0) for b_ in lbls):
                continue
            if route.cell(end) in pin_tips or route.cell(end) in out or any(hit(lane, o_, 0.1) for o_ in obst)                     or any(max(abs(q[0] - end[0]), abs(q[1] - end[1])) < 2 * U - 0.01 for c_, q in out.items() if c_ != c):
                continue
            fb = next((f for f in frame_boxes if f[0] < x < f[2] and f[1] < y < f[3]), None)
            if fb is not None and not (fb[0] + U < end[0] < fb[2] - U and fb[1] + U < end[1] < fb[3] - U):
                continue
            wires.append((k if k is not None else ("nc", s.ref, str(p["number"])), (x, y), end))
            del out[c]
            out[route.cell(end)] = end
            pin_tips.setdefault(route.cell(end), None)
            obst.append(wbox((x, y), end, 0.1))
    return list(out.values())


def _rehome(D: Design, blocks: list, make) -> list:
    """(class L) No island where a pin is free elsewhere. A pull-up / pull-down / filter cap that found no room at
    the pins of its own IC (I2C pull-ups behind a crystal) used to stand beside it, joined by the net's name. When
    another block of the sheet holds a pin of the same net (the sensor on that bus), the part is drawn THERE, on
    the wire of that pin - if that block can take it without losing anything. The choice is written into the design
    (`BlockInfo.sats`, `Design.block_of`, the part's role), so every later drawing of the sheet keeps it.
    -> [(ref, from block, to block)]"""
    from . import dock as DOCK
    moved = []
    by_id = {b.info.id: i for i, b in enumerate(blocks)}
    for bi in range(len(blocks)):
        src = blocks[bi]
        if getattr(src, "sm", None) is None or not src.info.sats:
            continue
        isl = DOCK.islands(src)
        if not isl:
            continue
        # the blocks that hold another pin of the island's net, the one with the fewest parts first
        want: dict = {}
        for r, k in isl:
            for ci, c in enumerate(blocks):
                if ci != bi and getattr(c, "sm", None) is not None and c.info.kind != "misc" and \
                        any(k in D.pins_of.get(a, {}).values() for a in c.info.anchors):
                    want.setdefault(ci, []).append(r)
        for ci in sorted(want, key=lambda i: (-len(want[i]), len(blocks[i].info.anchors) + len(blocks[i].info.sats), i)):
            refs = [r for r in want[ci] if r in src.info.sats]
            for group in ([refs] if len(refs) > 1 else []) + [[r] for r in refs]:
                group = [r for r in group if r in src.info.sats]
                if not group:
                    continue
                dst = blocks[ci]
                s0, d0 = list(src.info.sats), list(dst.info.sats)
                src.info.sats[:] = [r for r in s0 if r not in group]
                dst.info.sats[:] = sorted(d0 + group, key=_refkey)
                ok = False
                try:
                    nd = make(dst.info)
                    isl_d = {r for r, _k in DOCK.islands(nd)}
                    if nd.sm is not None and not (isl_d & set(group)) and len(isl_d) <= len(DOCK.islands(dst)) and \
                            DOCK.block_cost(nd) <= DOCK.block_cost(dst):
                        ns = make(src.info)
                        if ns.sm is not None and DOCK.block_cost(ns) < DOCK.block_cost(src) and \
                                len(DOCK.islands(ns)) < len(DOCK.islands(src)):
                            ok = True
                except LayoutError:
                    ok = False
                if not ok:
                    src.info.sats[:] = s0
                    dst.info.sats[:] = d0
                    continue
                blocks[bi], blocks[ci] = ns, nd
                src = ns
                for r in group:
                    D.block_of[r] = dst.info.id
                    k = next(k for r2, k in isl if r2 == r)
                    home = next((a for a in dst.info.anchors if k in D.pins_of.get(a, {}).values()), dst.info.id)
                    if r in D.role:
                        D.role[r]["anchor"] = home
                    moved.append((r, src.info.id, dst.info.id))
    D.rehomed = list(getattr(D, "rehomed", ())) + moved
    return moved


def layout_pro(sch: Schematic, D: Design, pr_name: str, use_router: bool = True, frames: bool = True,
               paper: str | None = None, titles: dict | None = None, notes: dict | None = None,
               title_block: dict | None = None, other_refs: set | None = None, keep_paper: bool = False,
               v2: bool = False, undock_ok: bool = False, flag_on_wire: bool = True, fit: dict | None = None,
               columns: list | None = None) -> dict:
    """Re-draw `sch` in place. Raises LayoutError when the sheet cannot be drawn in this style.
    fit = {block id: (left, top, right, bottom)}: how far the finished drawing of a block reached beyond the box the
    block reserved (negative: it stayed inside), measured on an earlier drawing of this sheet (the result's "fit").
    columns = the result's page.columns of that drawing: the blocks keep that arrangement."""
    t0 = time.time()
    v = D.view
    old_nodes = [n for n in sch.tree if isinstance(n, list)]
    old_ids = {id(n) for n in old_nodes}
    for s in sch.symbols(include_power=False):
        if sch.lib_symbol(s.lib_name) is None:
            raise LayoutError(f"symbol {s.ref} has no embedded library symbol")
    for k, ln in v.nets.items():
        if ln.flags and not ln.named:
            raise LayoutError(f"a PWR_FLAG sits on a net without a name ({k})")
    E.strip(sch)
    # ── blocks ──
    blocks = []
    F0 = FLOW.analyse(D) if v2 else None
    def make(info):
        b = Blk(D, info)
        b.flag_on_wire = flag_on_wire
        if v2:
            # a connector looks INTO the sheet: one whose frame the page pins to the first column has its pins on
            # the right, one in the last column on the left (a DC jack that feeds the rail through a diode is not a
            # "supply input" by its nets, but it stands on the left edge all the same)
            b.face_hint = {"first": "right", "last": "left"}.get(F0.pin.get(info.id))
            b.enable_v2()
            try:
                b.build()
            except LayoutError:
                raise
            except Exception as e:  # noqa: BLE001 - a block the docking cannot build is drawn the legacy way
                why = f"{type(e).__name__}: {str(e)[:100]}"
                b = Blk(D, info)
                b.flag_on_wire = flag_on_wire
                b.face_hint = {"first": "right", "last": "left"}.get(F0.pin.get(info.id))
                b.enable_v2()
                b.used -= b.stage_claim
                b.sm = None
                b.stats["dropped"].append(("block", why))
                b.build()
        else:
            b.build()
        return b
    for info in D.blocks:
        blocks.append(make(info))
    if v2 and REHOME:
        _rehome(D, blocks, make)
    # ── strips: PWR_FLAGs and second names, parked in the power block ──
    items = []
    for k in sorted(v.nets, key=lambda k: (not D.is_gnd(k), D.text(k), k)):
        ln = v.nets[k]
        rail = k in D.rails
        aliases = ln.names if rail else ln.names[1:]
        if not (ln.flags or aliases or ln.power[1:]):
            continue
        first = ("power", ln.power[0]) if rail else ("label", ln.names[0])
        items.append({"net": k, "first": first, "power": ln.power[1:], "labels": list(aliases), "flags": ln.flags})
    for o in v.orphans:
        items.append({"net": None, "first": None, "power": list(o["power"]), "labels": list(o["names"]), "flags": o["flags"]})
    # a flag belongs on the net it marks, where the supply comes in - not in a pile of its own
    by_src = sorted(blocks, key=lambda b: ({"input": 0, "power": 1}.get(b.info.kind, 2), getattr(b.info, "stage", 9)))
    for it in items:
        if it["net"] is None or not it["flags"] or it["power"] or it["labels"]:
            continue
        while it["flags"]:
            if not any(b.add_flag(it["net"]) for b in by_src):
                break
            it["flags"] -= 1
    items = [it for it in items if it["flags"] or it["power"] or it["labels"] or it["net"] is None]
    if items:
        host = next((b for b in blocks if b.info.kind == "power"), None) or \
            next((b for b in blocks if b.info.kind == "input"), None) or (blocks[0] if blocks else None)
        if host is None:
            raise LayoutError("nothing to lay out")
        # what is left (a second name, a flag that found no room on its net) is parked under the block the net
        # lives in; rails and pass-through names go to the supply block
        homes: dict = {}
        for it in items:
            home = host
            if it["net"] is not None and it["net"] not in D.rails:
                cnt = {}
                for r, _p in v.nets[it["net"]].pins:
                    bi = D.block_of.get(r)
                    cnt[bi] = cnt.get(bi, 0) + 1
                if cnt:
                    top = sorted(cnt, key=lambda k: (-cnt[k], str(k)))[0]
                    home = next((b for b in blocks if b.info.id == top), host)
            homes.setdefault(id(home), (home, []))[1].append(it)
        for home, its in homes.values():
            sc = strip_cell(D, its)
            hb = home.bbox()
            sb = sc.bbox()
            home.attach(sc, "B", pref=(hb[0], hb[3]), port=(sb[0], sb[1]), gap=4 * U)
    if not blocks:
        raise LayoutError("nothing to lay out")
    # ── compose ──
    fit = {k: tuple(e) + (True,) * (5 - len(e)) for k, e in (fit or {}).items()} if (v2 and frames) else {}
    drawn = {id(b): _fit_box(b, fit.get(b.info.id)) for b in blocks}      # what the block draws (bbox, corrected)
    ref0 = {id(b): (b.bbox(), _fit_ref(b)) for b in blocks}
    sizes = {}
    for b in blocks:
        bb = drawn[id(b)]
        tw = text_width((titles or {}).get(b.info.id, b.info.title), 2.0) + 5.0 if frames else 0.0
        if b.info.id in fit:
            # measured: the same air on every side (one grid step of play: the drawing stands on the 2.54 mm grid)
            sizes[id(b)] = (math.ceil((max(bb[2] - bb[0] + 2 * FIT_PAD, tw) - 0.01) / (2 * U)) * 2 * U,
                            math.ceil((bb[3] - bb[1] + FIT_TOP + FIT_PAD + U + TITLE_H - 0.01) / (2 * U)) * 2 * U)
            continue
        sizes[id(b)] = (math.ceil((max(bb[2] - bb[0] + 2 * PADF, tw) - 0.01) / (2 * U)) * 2 * U,
                        math.ceil((bb[3] - bb[1] + 2 * PADV + TITLE_H - 0.01) / (2 * U)) * 2 * U)
    tight = {}
    for b in blocks:
        bb = drawn[id(b)]
        tw = text_width((titles or {}).get(b.info.id, b.info.title), 2.0) + 5.0 if frames else 0.0
        if b.info.id in fit:
            tight[id(b)] = (math.ceil((max(bb[2] - bb[0] + 2 * FIT_TIGHT, tw) - 0.01) / (2 * U)) * 2 * U,
                            math.ceil((bb[3] - bb[1] + 2 * FIT_TIGHT + TITLE_H - 0.01) / (2 * U)) * 2 * U)
            continue
        tight[id(b)] = (math.ceil((max(bb[2] - bb[0] + (4 * U if v2 else 2 * U), tw) - 0.01) / (2 * U)) * 2 * U,
                        math.ceil((bb[3] - bb[1] + 2 * U + TITLE_H - 0.01) / (2 * U)) * 2 * U)
    page_info: dict = {}
    leans: dict = {}
    note_room: dict = {}
    auto_notes = NOTES.build(D) if (v2 and frames) else {}
    bare = dict(tight)
    for b in blocks:
        # the notes of a frame stand under its drawing: the frame is that much taller (and at least as wide)
        nw, nh = NOTES.box_of(auto_notes.get(b.info.id, []))
        if nh and b.info.id in fit and not fit[b.info.id][4]:
            continue                                      # (measured) its notes stood beside their parts, not under the drawing
        if nh:
            add = math.ceil((nh + 2.2) / (2 * U)) * 2 * U
            note_room[id(b)] = add
            for sz in (sizes, tight):
                sz[id(b)] = (max(sz[id(b)][0], math.ceil((nw + 2 * PADF) / (2 * U)) * 2 * U), sz[id(b)][1] + add)
    if v2:
        F = FLOW.analyse(D)
        pp, pos, page_info = PAGE.compose(blocks, sizes, F, D, PAGE.paper_of(sch) if keep_paper else paper, tight,
                                          bare if note_room else None, keep=columns)
        if pp is not None and page_info.get("sizes") is bare:
            note_room = {}                                # too tight for that: the notes go where room is left
        if pp is not None and page_info["undock"] and undock_ok:
            raise Undock(page_info["undock"])
    else:
        pp, pos = compose(blocks, sizes, sch.paper if keep_paper else paper, tight)
    if pp is None:
        raise LayoutError("the drawing does not fit on " + (paper or "an A0 sheet")
                          + " - split the design into hierarchical sheets")
    PAGE.set_paper(sch, pp)
    pw, ph = PAGE.dims(pp)
    frame_boxes = []
    if v2 and page_info:
        leans = _settle(blocks, pos, page_info["col_of"], D, note_room, drawn if fit else None, page_info.get("room"))
        frame_boxes = [[snap(pos[id(b)][0]), snap(pos[id(b)][1]), snap(pos[id(b)][0] + pos[id(b)][2]),
                        snap(pos[id(b)][1] + pos[id(b)][3])] for b in blocks]
    for b in ([] if frame_boxes else blocks):
        x, y, cw, ch = pos[id(b)]
        bb = b.bbox()
        w, h = sizes[id(b)]
        # the drawing sits in the middle of its frame; frames of a column share both edges
        b.shift(snap(x + (cw - (bb[2] - bb[0])) / 2 - bb[0], 2 * U),
                snap(y + TITLE_H + (ch - TITLE_H - (bb[3] - bb[1])) / 2 - bb[1], 2 * U))
        frame_boxes.append([snap(x), snap(y), snap(x + cw), snap(y + ch)])
    t_place = time.time() - t0
    bus_named: set = set()
    for _try in (0, 1, 2, 3):
        # ── registries ──
        wires = [w for b in blocks for w in b.wires]
        terms = [t for b in blocks for t in b.terms]
        powers = [p for b in blocks for p in b.powers]
        ncs = [p for b in blocks for p in b.ncs]
        fixed = [l for b in blocks for l in b.labels]
        flags = [f for b in blocks for f in b.flags]
        blk_of_term = {(t[0], t[1]): i for i, b in enumerate(blocks) for t in b.terms}
        name_at = {n: (q, o) for b in blocks for n, q, o in getattr(b, "name_at", ())}
        pin_tips = {}
        hard = []
        for s in sch.symbols(include_power=False):
            gb = score.graphic_box(sch, s)
            hard.append(gb)
            hard += s.field_boxes()
            for p in s.pins_world():
                pin_tips[route.cell(p["xy"])] = D.pins_of[s.ref].get(p["number"])
                ln = float(p.get("length", 2.54) or 2.54)
                inner = (p["xy"][0] - p["out"][0] * ln, p["xy"][1] - p["out"][1] * ln)
                hard.append(wbox((p["xy"][0] - p["out"][0] * U, p["xy"][1] - p["out"][1] * U), inner, 0.1))
                nm = str(p.get("name") or "")
                if v2 and nm not in ("", "~"):
                    # a unit without a body around its pin names (the supply unit of an op-amp): the names stand in
                    # the open, and a wire may not run over them
                    far = (inner[0] - p["out"][0] * (text_width(nm) + 1.0), inner[1] - p["out"][1] * (text_width(nm) + 1.0))
                    mx, my = (inner[0] + far[0]) / 2, (inner[1] + far[1]) / 2
                    if not (gb[0] - 0.3 <= mx <= gb[2] + 0.3 and gb[1] - 0.3 <= my <= gb[3] + 0.3):
                        hard.append(wbox(inner, far, 0.7))
        if v2:
            hard += _twin_rows(sch, D)
        for n, q, o, val in powers:
            hard.append(power_box(val, q, o))
        for n, q, fo in flags:
            hard.append(power_box("PWR_FLAG", q, fo))
        for n, e, q, ang in fixed:
            hard.append(lbl_box(e[0], q, {0: (1, 0), 180: (-1, 0), 90: (0, -1), 270: (0, 1)}[ang], e[1]))
        for fb in frame_boxes:                               # the title strip of every frame
            hard.append([fb[0], fb[1], fb[2], fb[1] + TITLE_H])
        for q in ncs:
            pin_tips.setdefault(route.cell(q), None)
        resv = [x for b in blocks for x in b.resv]
        R = route.Router(hard, resv, pin_tips, wires, (4, 4, int(pw / U) - 4, int(ph / U) - 4))
        for t in terms:                  # a label may be anchored here: no other net may cross this point (C6)
            R.node[route.cell(t[1])] = t[0]
        for n, q, o, val in powers:
            R.node[route.cell(q)] = n
        for n, e, q, ang in fixed:
            R.node[route.cell(q)] = n
        for n, q, fo in flags:
            R.node[route.cell(q)] = n
        signal = sorted({t[0] for t in terms})
        must = {k for k in signal if not D.named(k)}
        t1 = time.time()
        pin_ref = {}
        for s in sch.symbols(include_power=False):
            for p in s.pins_world():
                pin_ref[route.cell(p["xy"])] = s.ref
        big = {a for b in blocks for a in b.info.anchors}

        pin_out = {}
        for s in sch.symbols(include_power=False):
            for p in s.pins_world():
                pin_out[route.cell(p["xy"])] = (s.ref, tuple(p["out"]))
        blk_of_ref = {r: i for i, b in enumerate(blocks) for r in list(b.info.anchors) + list(b.info.sats)}

        docked = {a for b in blocks for a in b.info.anchors if a != b.info.id and D.is_conn(a)}

        def skip_pair(ci, cj):
            ri = {pin_ref[c] for c in ci if c in pin_ref}
            rj = {pin_ref[c] for c in cj if c in pin_ref}
            if (ri and all(D.npins(r) == 1 for r in ri)) or (rj and all(D.npins(r) == 1 for r in rj)):
                return True                                   # a test point / pad carries the net's name, not a wire
            for r in ri & rj:
                # two pins of ONE part that face different ways (switch contacts, a TVS array's in / out): a wire
                # between them would loop around the part and its texts - a label on each side reads better
                oi = {pin_out[c][1] for c in ci if c in pin_out and pin_out[c][0] == r}
                oj = {pin_out[c][1] for c in cj if c in pin_out and pin_out[c][0] == r}
                if oi and oj and not (oi & oj):
                    return True
            # two different ICs / connectors: labels at both ends read better than a wire threaded between them -
            # unless they sit next to each other in one block (an MCU and its flash, a sensor on its bus)
            if bool(ri) and bool(rj) and not (ri & rj) and (ri <= docked or rj <= docked) and len(ci) * len(cj) <= 4000:
                # a connector docked at the pins it serves is wired row on row; one that had to move off its row keeps
                # its names - a wire that winds around the part to reach it reads worse than two labels
                d = min(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a in ci for b in cj)
                if not any(a[1] == b[1] and abs(a[0] - b[0]) == d for a in ci for b in cj):
                    return True
            if bool(ri) and bool(rj) and ri <= big and rj <= big and not (ri & rj):
                if {blk_of_ref.get(r) for r in ri} != {blk_of_ref.get(r) for r in rj}:
                    return True
                d = min(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a in ci for b in cj) if len(ci) * len(cj) <= 4000 else 99
                return d > 16
            return False

        def skip_pair_v2(ci, cj):
            # pro2: inside a block a wire is the rule (docs 5.1) - the router's bend / crossing limits decide, not the
            # kind of part. Only a wire that would loop around ONE part to join two of its sides stays a pair of names
            # (the in / out pins of a TVS array, the doubled contacts of a tactile switch).
            ri = {pin_ref[c] for c in ci if c in pin_ref}
            rj = {pin_ref[c] for c in cj if c in pin_ref}
            for r in ri & rj:
                if D.npins(r) < 5 and not SWITCH_NAMES:
                    continue                              # a strap across a switch's doubled contacts is fine
                oi = {pin_out[c][1] for c in ci if c in pin_out and pin_out[c][0] == r}
                oj = {pin_out[c][1] for c in cj if c in pin_out and pin_out[c][0] == r}
                if oi and oj and not (oi & oj):
                    return True
            # ... and a wire from ONE pin of an IC to something that lies behind that pin: it would have to run
            # around the part's body (a reset line under the whole MCU) - two names read better
            # (a group that reaches around its part already - the bar over a pass-through TVS - is judged by the
            # pin that looks at the other group)
            for a_, b_ in ((ci, cj), (cj, ci)):
                behind = []
                for t in a_:
                    if t not in pin_out or D.npins(pin_out[t][0]) < 6:
                        continue
                    o = pin_out[t][1]
                    behind.append(all((b[0] - t[0]) * o[0] + (b[1] - t[1]) * o[1] < -4 for b in b_))
                if behind and all(behind):
                    return True
            return False
        span = None
        if v2:
            R.v2 = True
            # a name that stands ON its wire (the stub of a pin with a series part behind it): no other wire of the
            # net may end under the letters - the junction dot and the wire would run through the name
            R.no_tee = set()
            for n_, pt_, o_ in terms:
                if o_[1] != 0 or not D.named(n_):
                    continue
                if not any(n2 == n_ and a[1] == b[1] == pt_[1] and min(a[0], b[0]) - 0.01 <= pt_[0] + o_[0] * U <= max(a[0], b[0]) + 0.01
                           for n2, a, b in wires):
                    continue                              # the name hangs off the wire's end: nothing under it
                for i_ in range(0, int(math.ceil((text_width(D.text(n_)) + 1.5) / U)) + 1):
                    R.no_tee.add(route.cell((pt_[0] + o_[0] * i_ * U, pt_[1])))
            diag = {i: int(math.hypot(fb[2] - fb[0], fb[3] - fb[1]) / U) for i, fb in enumerate(frame_boxes)}
            span = lambda bs: max([diag.get(i, route.MAX_SPAN) for i in bs if i is not None] or [route.MAX_SPAN])  # noqa: E731
        side_syms = [route.cell(q) for _n, q, o, _v in powers if o not in ("up", "down")]

        def tight_pair(ci, cj):
            # both groups hold a pin of a part with three or more pins (IC, header, switch): a wire between them is
            # only worth drawing when it goes there directly. (A two-pin part would be left as an island of two
            # names - there the longer wire is the lesser evil.)
            ri, rj = ({pin_ref[c] for c in cs if c in pin_ref and D.npins(pin_ref[c]) >= 3} for cs in (ci, cj))
            if not ri or not rj or (ri & rj):                 # (a strap across ONE part's contacts is no detour)
                return False
            # ... unless a name would have no room at its pin: a supply symbol that had to be drawn sideways in the
            # next row stands where the name would
            return SIDE_SYM_TIGHT or not any(abs(c[0] - s_[0]) <= 4 and abs(c[1] - s_[1]) <= 2 for cs in (ci, cj)
                                             for c in cs for s_ in side_syms)
        # (fix round 3) a bus between two parts is drawn ONE way: when only some of its members could be wired (the
        # flash pins that look at the MCU) the sheet is wired once more with the whole bus named at both ends
        named_only = set(bus_named)
        routed = route.route_nets(R, wires, terms, [k for k in signal if k not in named_only] if use_router else sorted(must),
                                  blk_of_term, must,
                                  skip_pair=skip_pair_v2 if v2 else skip_pair, span=span, limits=(3, 1) if v2 else None,
                                  tight=tight_pair if v2 else None)
        crossed = 0
        if v2 and use_router and page_info:
            # cross-block wiring by cost (docs 5.1): a named net between two blocks of the same or of neighbouring
            # columns is wired when the wire is short and plain enough; bundles go in pin-row order
            col_of = page_info["col_of"]
            bcol = {i: col_of.get(b.info.id) for i, b in enumerate(blocks)}

            def adjacent(bi, bj):
                ca, cb = bcol.get(bi), bcol.get(bj)
                return ca is not None and cb is not None and abs(ca - cb) <= 1
            joined = route.route_cross(R, wires, terms, [k for k in signal if k not in must and k not in named_only],
                                       blk_of_term, adjacent,
                                       lambda k: len(v.nets[k].pins))
            crossed = len(joined)
            routed += crossed
            # a drawing that leaned towards a neighbour and got no wire for it goes back to the middle of its
            # frame, and the sheet is wired once more
            back = [i for i, d in leans.items() if (d[0] or d[1]) and not any(t[0] in joined for t in blocks[i].terms)]
            if _try == 0 and back:
                for i in back:
                    blocks[i].shift(-leans[i][0], -leans[i][1])
                continue
        t_route = time.time() - t1
        # ── labels: one per wire group of a named net ──
        ANG = {(1, 0): 0, (-1, 0): 180, (0, -1): 90, (0, 1): 270}
        DIR = {a: o for o, a in ANG.items()}

        def upright(k, ent, t, placed):
            """pro2: a name at the end of a VERTICAL wire reads horizontally - at the wire end when there is room
            beside it, else the wire runs on (up to 12 steps) until there is. -> (point, direction, steps) or None
            (then the name stays vertical, which the score lists as a hard violation)."""
            if not v2 or t[2][0] != 0:
                return None
            lbs = [lbl_box(e[0], q, DIR[a], e[1]) for _k, e, q, a in placed]
            for kk in (0, 2, 4, 6, 8, 10, 12):
                end = (t[1][0], snap(t[1][1] + t[2][1] * kk * U))
                if kk:
                    ext = wbox(t[1], end, 0.05)
                    if any(hit(ext, o, 0.1) for o in hard) or any(hit(ext, x, 0.1) for x in lbs) or \
                            any(n2 != k and hit(ext, wbox(a, b, 0.05), -1.0) for n2, a, b in wires) or \
                            any(n2 != k and hit(ext, rb, 0.1) for n2, rb in resv):
                        break
                for d in ((1, 0), (-1, 0)):
                    lb = lbl_box(ent[0], end, d, ent[1])
                    if any(hit(lb, o, 0.1) for o in hard) or any(hit(lb, x, 0.1) for x in lbs) or \
                            any(hit(lb, wbox(a, b, 0.05), 0.15) for _n2, a, b in wires) or \
                            any(n2 != k and hit(lb, rb, 0.1) for n2, rb in resv):
                        continue
                    return end, d, kk
            return None

        def name_net(k, comps, placed):
            """The labels of one named net: on its wire when it is in one piece, else one per wire group."""
            ent = v.nets[k].names[0]
            if sum(1 for c in comps if c[1]) == 1 and len(comps) == 1:
                spot = _name_on_wire(k, ent, wires, hard, placed,
                                     [(c[0] * route.U, c[1] * route.U) for c in pin_tips], frame_boxes if v2 else (),
                                     prefer=name_at.get(k))
                if spot is not None:
                    return [(k, ent, spot, 0)]
            out = []
            for cs, ts in comps:
                if not ts:
                    continue

                def clean(t):
                    lb = lbl_box(ent[0], t[1], t[2], ent[1])
                    return sum(1 for n2, a, b in wires if hit(lb, wbox(a, b, 0.05), 0.15))
                t = min(ts, key=lambda t: (clean(t), -t[1][0] * t[2][0], t[1][1], t[1][0]))
                up = upright(k, ent, t, placed + out)
                if up is not None:
                    if up[2]:
                        wires.append((k, t[1], up[0]))
                    out.append((k, ent, up[0], ANG[up[1]]))
                    continue
                at = t[1]
                if v2 and t[2][1] == 0:
                    # a name that starts on a junction, or with a wire of its own net running up through its first
                    # letter, moves along its wire until it stands free
                    def dirty(pt):
                        lb = lbl_box(ent[0], pt, t[2], ent[1])
                        g = [lb[0] - 0.4, lb[1], lb[2] + 0.4, lb[3]]
                        for _n2, a, b in wires:
                            if a[0] != b[0]:
                                continue
                            if hit(g, wbox(a, b, 0.05), 0.0):
                                return True
                            if any(abs(e[1] - pt[1]) < 0.01 and abs(e[0] - pt[0]) < 1.0 for e in (a, b)):
                                return True
                        return False
                    if dirty(at):
                        lbs = [lbl_box(e[0], q, DIR[a_], e[1]) for _k, e, q, a_ in placed + out]
                        for j in (2, 4, 6, 8, 10):
                            pt = (snap(at[0] + t[2][0] * j * U), at[1])
                            nxt = (snap(pt[0] + t[2][0] * U), pt[1])
                            if not all(any(n2 == k and a[1] == b[1] == q_[1] and min(a[0], b[0]) - 0.01 <= q_[0] <= max(a[0], b[0]) + 0.01
                                           for n2, a, b in wires) for q_ in (pt, nxt)):
                                break                     # the wire ends here
                            lb = lbl_box(ent[0], pt, t[2], ent[1])
                            lb = [lb[0], lb[1], lb[2], min(lb[3], pt[1] - 0.2)]
                            if dirty(pt) or any(hit(lb, o, -0.3) for o in hard) or any(hit(lb, x, 0.1) for x in lbs) or                                     any(n2 != k and hit(lb, wbox(a, b, 0.05), 0.1) for n2, a, b in wires):
                                continue
                            at = pt
                            break
                if v2 and t[2][1] == 0 and at == t[1]:
                    # a name that would stand on a symbol or on another text (a supply symbol that had to be drawn
                    # in the row next to it): the stub runs on until the name stands free
                    lbs = [lbl_box(e[0], q, DIR[a_], e[1]) for _k, e, q, a_ in placed + out]
                    lbs += [bx for _n, q, o, val in powers for bx in power_boxes(val, q, o) if o in ("up", "down")]
                    side_pb = []
                    for _n, q, o, val in powers:
                        if o in ("up", "down"):
                            continue
                        # a sideways symbol (the last resort of section 14): its triangle is a row high, its text not
                        sx = 1 if o == "right" else -1
                        side_pb.append(power_box(val, q, o))
                        far = q[0] + sx * (3.2 + max(2.6, text_width(val) + 0.6))
                        lbs.append([min(q[0], q[0] + sx * 2.7), q[1] - 1.3, max(q[0], q[0] + sx * 2.7), q[1] + 1.3])
                        lbs.append([min(q[0] + sx * 2.7, far), q[1] - 0.75, max(q[0] + sx * 2.7, far), q[1] + 0.75])
                    hard_f = [o for o in hard if o not in side_pb]

                    def free_at(pt):
                        lb = lbl_box(ent[0], pt, t[2], ent[1])
                        return not (any(hit(lb, o, 0.1) for o in hard_f) or any(hit(lb, x, 0.1) for x in lbs)
                                    or any(n2 != k and hit(lb, wbox(a, b, 0.05), 0.1) for n2, a, b in wires)
                                    or any(n2 != k and hit(lb, rb, 0.1) for n2, rb in resv))
                    if not free_at(at):
                        for j in (2, 4, 6, 8, 10, 12):
                            pt = (snap(at[0] + t[2][0] * j * U), at[1])
                            ext = wbox(at, pt, 0.05)
                            if (any(hit(ext, o, 0.1) for o in hard_f) or any(hit(ext, x, 0.1) for x in lbs)
                                    or any(n2 != k and hit(ext, wbox(a, b, 0.05), -0.3) for n2, a, b in wires)
                                    or any(n2 != k and hit(ext, rb, 0.1) for n2, rb in resv)
                                    or any(pin_tips.get(route.cell((at[0] + t[2][0] * i * U, at[1])), k) != k
                                           for i in range(1, j + 1))):
                                break
                            if free_at(pt):
                                wires.append((k, at, pt))
                                at = pt
                                break
                out.append((k, ent, at, ANG[tuple(t[2])]))
            return out
        labels = list(fixed)
        settled: dict = {}
        if v2 and use_router:
            # second pass (docs 5.1 "retry once"): the nets that are in one piece now get their ONE name; the label
            # reservations of all their other wire ends are stale and no longer block the nets that are still split
            split = []
            for k in signal:
                if k in must:
                    continue
                comps = route.components(wires, k, terms)
                blks = [{blk_of_term.get((t[0], t[1])) for t in c[1]} for c in comps if c[1]]
                if k not in named_only and any(blks[i] & blks[j] for i in range(len(blks)) for j in range(i + 1, len(blks))):
                    split.append(k)
                    continue
                settled[k] = name_net(k, comps, labels + [x for lst in settled.values() for x in lst])
            if split:
                took = [x for lst in settled.values() for x in lst]
                hard2 = hard + [lbl_box(e[0], q, DIR[ang], e[1]) for _k, e, q, ang in took]
                R2 = route.Router(hard2, [(n, b) for n, b in resv if n in split], pin_tips, wires, R.bounds)
                R2.v2 = True
                R2.no_tee = R.no_tee
                R2.node.update(R.node)
                for k_, _e, q, _a in took:
                    R2.node[route.cell(q)] = k_
                routed += route.route_nets(R2, wires, terms, split, blk_of_term, must, skip_pair=skip_pair_v2, span=span,
                                           limits=(3, 1), tight=tight_pair)
                for key in ("routes", "crossings", "expanded", "rejected"):
                    R.stats[key] += R2.stats[key]
        if v2 and use_router and BUS_ALL_OR_NONE and not bus_named and _try < 3:
            bus_named = _half_wired_buses(D, v, wires, terms, [k for k in signal if k not in must])
            if bus_named:
                continue
        for k in signal:
            comps = route.components(wires, k, terms)
            if k in must:
                real = [c for c in comps if c[1]]
                if len(real) > 1:
                    raise LayoutError(f"net {k} has no name on this sheet and could not be wired in one piece")
                continue
            labels += settled[k] if k in settled else name_net(k, comps, labels)
        break
    if v2 and FLAG_HOME and use_router:
        _flags_home(D, wires, labels, flags, fixed, hard, pin_tips, frame_boxes)
    # ── emit ──
    keep: dict = {}
    for c, k in pin_tips.items():
        keep.setdefault(k, set()).add(c)
    for k, e, q, ang in labels:
        keep.setdefault(k, set()).add(route.cell(q))
    for k, q, o, val in powers:
        keep.setdefault(k, set()).add(route.cell(q))
    for k, q, fo in flags:
        keep.setdefault(k, set()).add(route.cell(q))
    if v2 and use_router and BUILD.NC_LEAD:      # (the pro2-safe rung keeps every cross on its pin)
        ncs = _nc_leads(sch, D, ncs, wires, hard, labels, pin_tips, frame_boxes)
    nc_cells = {route.cell(q) for q in ncs}
    for n, a, b_ in wires:                                  # the open end of a no-connect lead is not a dangling end
        for q in (a, b_):
            if route.cell(q) in nc_cells:
                keep.setdefault(n, set()).add(route.cell(q))
    em = E.emit_wires(sch, wires, keep, pin_tips)
    pool = E.LabelPool()
    for k, e, q, ang in labels:
        pool.take(sch, e, q, ang)
    for q in sorted(set(ncs)):
        sch.add_no_connect(q)
    taken = set(other_refs or ()) | {s.ref for s in sch.symbols()}
    sch.extra_refs = set(other_refs or ())

    def next_ref(prefix):
        r = sch.next_ref(prefix, taken)
        taken.add(r)
        return r
    for k, q, o, val in powers:
        src = v.power_src.get(val)
        if src is None:
            raise LayoutError(f"no power symbol for {val}")
        E.put_power(sch, src, next_ref("#PWR"), val, q, o)
    for k, q, fo in flags:
        if v.flag_src is None:
            raise LayoutError("no PWR_FLAG symbol on the sheet")
        E.put_power(sch, v.flag_src, next_ref("#FLG"), "PWR_FLAG", q, fo)
    rect_uuids = []
    note_stats = {"placed": 0, "left_out": 0}
    if frames:
        # pro2: deterministic design notes in the free corners of the frames they belong to (notes.py); what
        # concerns the whole sheet (rails and their sources) goes to the free strip beside the title block, or to
        # the frame with the most room
        stacks: dict = {}
        loose_stacks = []
        if v2:
            auto = auto_notes
            obst = TEXT.flat(TEXT.scene(sch))
            for b, fb in zip(blocks, frame_boxes):
                obst.append([fb[0], fb[1], fb[2], fb[1] + TITLE_H + (2.6 if (notes or {}).get(b.info.id) else 0.0)])

            def seat(lines, fb, title_h=TITLE_H):
                lines = list(lines)
                while lines:
                    at = NOTES.place(lines, fb, obst, title_h)
                    if at is not None:
                        w, h = NOTES.box_of(lines)
                        top = at[1] - NOTES.NOTE_SIZE - 0.3
                        obst.append([at[0] - 0.8, top - 0.8, at[0] + w + 0.8, top + h + 0.8])
                        return lines, at
                    lines.pop()
                return None
            ids = {b.info.id for b in blocks}
            sheet_lines = list(auto.get(None, []))
            for k in sorted((k for k in auto if k is not None and k not in ids), key=str):
                sheet_lines += auto[k]
            part_box: dict = {}
            for s_ in sch.symbols(include_power=False):
                bb_ = s_.body_bbox()
                for fb_ in s_.field_boxes():
                    bb_ = [min(bb_[0], fb_[0]), min(bb_[1], fb_[1]), max(bb_[2], fb_[2]), max(bb_[3], fb_[3])]
                o_ = part_box.get(s_.ref)
                part_box[s_.ref] = bb_ if o_ is None else [min(o_[0], bb_[0]), min(o_[1], bb_[1]), max(o_[2], bb_[2]), max(o_[3], bb_[3])]
            note_stats["near"] = 0
            for b, fb in zip(blocks, frame_boxes):
                want = list(auto.get(b.info.id, []))
                # (text hygiene) a note that names a part stands next to that part: lines about one subject form
                # one stack; what finds no room near its subject goes to the frame's free corner as before
                rest = []
                if NOTES.NEAR:
                    by_subj: dict = {}
                    # notes of one kind stay together (the legend rows of two connectors, the current of two LEDs):
                    # one stack next to the parts it is about, taken as one
                    kinds: dict = {}
                    for ln_ in want:
                        sj = tuple(r_ for r_ in NOTES.subject(ln_) if r_ in part_box)
                        kinds.setdefault((prefix(sj[0]), NOTES.is_legend(ln_)) if sj else None, []).append((sj, ln_))
                    for kd, rows_ in kinds.items():
                        sj = tuple(sorted({r_ for s2, _l in rows_ for r_ in s2}, key=_refkey)) if kd else ()
                        by_subj.setdefault(sj, []).extend(l_ for _s, l_ in rows_)
                    for sj, lns in by_subj.items():
                        at = None
                        if sj:
                            sb = [min(part_box[r_][0] for r_ in sj), min(part_box[r_][1] for r_ in sj),
                                  max(part_box[r_][2] for r_ in sj), max(part_box[r_][3] for r_ in sj)]
                            if fb[0] <= sb[0] and sb[2] <= fb[2] and fb[1] <= sb[1] and sb[3] <= fb[3]:
                                at = NOTES.place_near(lns, sb, fb, obst, TITLE_H + (2.6 if (notes or {}).get(b.info.id) else 0.0))
                        if at is None:
                            rest += lns
                            continue
                        w_, h_ = NOTES.box_of(lns)
                        top_ = at[1] - NOTES.NOTE_SIZE - 0.3
                        obst.append([at[0] - 0.8, top_ - 0.8, at[0] + w_ + 0.8, top_ + h_ + 0.8])
                        stacks.setdefault(b.info.id, []).append((lns, at))
                        note_stats["placed"] += len(lns)
                        note_stats["near"] += len(lns)
                    rest = [ln_ for ln_ in want if ln_ in rest]
                else:
                    rest = want
                got = seat(rest, fb) if rest else None
                if got:
                    stacks.setdefault(b.info.id, []).append(got)
                note_stats["placed"] += len(got[0]) if got else 0
                note_stats["left_out"] += len(rest) - (len(got[0]) if got else 0)
            if sheet_lines:
                sheet_lines = ["NOTES"] + sheet_lines
                nr = PAGE.notes_rect(pp, frame_boxes)
                got = None
                if NOTES_AT_TITLE_BLOCK:
                    # (text hygiene) the sheet's notes belong to the title block: directly left of it, first line
                    # level with its top edge - not somewhere under a frame column
                    (_ax0, _ay0, _ax1, ay1_), tb_ = PAGE.area(pp)
                    nw_, nh_ = NOTES.box_of(sheet_lines)
                    y_ = tb_[1] + 1.0
                    while got is None and y_ + nh_ <= ay1_:
                        # ... or as far below that as a frame standing in the way asks for
                        bx = [tb_[0] - 2 * 2 * U - nw_, y_, tb_[0] - 2 * 2 * U, y_ + nh_]
                        if bx[0] >= MARGIN and not any(hit(bx, [fb[0] - 2 * U, fb[1] - 2 * U, fb[2] + 2 * U, fb[3] + 2 * U], 0) for fb in frame_boxes)                                 and not any(hit(bx, o, 0) for o in obst):
                            got = (list(sheet_lines), (round(bx[0], 2), round(bx[1] + NOTES.NOTE_SIZE + 0.3, 2)))
                            obst.append([bx[0] - 0.8, bx[1] - 0.8, bx[2] + 0.8, bx[3] + 0.8])
                        y_ += 2 * U
                    if got is None:
                        # a frame stands left of the title block: the notes sit ON the title block, flush with its
                        # left edge (as far from the frame above as the gap allows)
                        bx = [tb_[0] + 1.5, tb_[1] - 1.5 - nh_, tb_[0] + 1.5 + nw_, tb_[1] - 1.5]
                        if not any(hit(bx, [fb[0] - U, fb[1] - U, fb[2] + U, fb[3] + U], 0) for fb in frame_boxes)                                 and not any(hit(bx, o, 0) for o in obst):
                            got = (list(sheet_lines), (round(bx[0], 2), round(bx[1] + NOTES.NOTE_SIZE + 0.3, 2)))
                            obst.append([bx[0] - 0.8, bx[1] - 0.8, bx[2] + 0.8, bx[3] + 0.8])
                if got is None:
                    got = seat(sheet_lines, nr, 0.0) if nr else None
                if got and len(got[0]) > 1:
                    loose_stacks.append(got)
                else:
                    got = None
                    # (fix round 4) no strip under the frames: any free spot of the SHEET that holds the block (under
                    # a short frame, above the title block) before the notes go into a frame they do not belong to
                    (ax0, ay0, ax1, ay1), tb = PAGE.area(pp)
                    nw_, nh_ = NOTES.box_of(sheet_lines)
                    spots = sorted({(fb[0], fb[3] + 1.5 * U) for fb in frame_boxes}, key=lambda q: (-q[1], q[0]))                         + sorted({(fb[2] + 2 * 2 * U, fb[1]) for fb in frame_boxes}, key=lambda q: (-q[1], q[0]))
                    for sx, sy in spots:
                        box = [sx, sy, sx + nw_, sy + nh_]
                        if box[2] > ax1 or box[3] > ay1 or (box[2] > tb[0] - 2 * U and box[3] > tb[1] + 2.0):
                            continue
                        if any(hit(box, [fb[0] - U, fb[1] - U, fb[2] + U, fb[3] + U], 0) for fb in frame_boxes)                                 or any(hit(box, o, 0) for o in obst):
                            continue
                        got = (list(sheet_lines), (round(sx, 2), round(sy + NOTES.NOTE_SIZE + 0.3, 2)))
                        obst.append([box[0] - 0.8, box[1] - 0.8, box[2] + 0.8, box[3] + 0.8])
                        loose_stacks.append(got)
                        break
                    for b, fb in ([] if got else sorted(zip(blocks, frame_boxes), key=lambda t: -(t[1][2] - t[1][0]) * (t[1][3] - t[1][1]))):
                        got = seat(sheet_lines, fb)
                        if got and len(got[0]) > 1:
                            stacks.setdefault(b.info.id, []).append(got)
                            break
                        got = None
                note_stats["placed"] += len(got[0]) if got else 0
                note_stats["left_out"] += len(sheet_lines) - (len(got[0]) if got else 0)
        for b, fb in zip(blocks, frame_boxes):
            title = (titles or {}).get(b.info.id, b.info.title)
            rect_uuids.append(E.add_frame(sch, b.info.id, title, fb, (notes or {}).get(b.info.id), stacks.get(b.info.id)))
        if loose_stacks:
            E.add_notes_group(sch, loose_stacks)
    # the sheet in one line: what each frame is for (the part numbers are in the frames)
    names = []
    for b in sorted(blocks, key=lambda b: (getattr(b.info, "stage", 0), _refkey(b.info.id))):
        t = str((titles or {}).get(b.info.id, b.info.title))
        t = " ".join(t.split("  ")[0].split()) if "  " in t and b.info.id not in (titles or {}) \
            and b.info.kind in ("power", "main", "ic") else " ".join(t.split())
        if t and t not in names:
            names.append(t)
    summ = [""]
    for t in names:
        if summ[-1] and len(summ[-1]) + len(t) > 60:
            if len(summ) == 2:
                summ[-1] += " \u2026"
                break
            summ.append("")
        summ[-1] += (" \u00b7 " if summ[-1] else "") + t
    if v2:
        sm = NOTES.summary(D, [b.info for b in sorted(blocks, key=lambda b: (getattr(b.info, "stage", 0), _refkey(b.info.id)))], titles)
        E.fill_title_block_v2(sch, pr_name, title_block, sm["function"], sm["rails"])
    else:
        E.fill_title_block(sch, pr_name, title_block, summ)
    user_notes = _park_notes(sch, frame_boxes, pw, ph)
    # pro2: the Reference / Value texts that touch something are re-seated on the finished drawing (text.py)
    if v2 and BUILD.FIELDS_TOGETHER:
        for s_ in sch.symbols(include_power=False):
            if D.npins(s_.ref) > 2:
                BUILD.together_fields(s_)
    text_stats = TEXT.solve(sch, _strap_boxes(sch, D), frames=frame_boxes if frames else None) if v2 else None
    if v2:
        # ... and the exact pass: whatever still touches by KiCad's own drawing rules (the metric text_collisions)
        text_stats["exact"] = TEXT.refine(sch, frames=frame_boxes if frames else None)
    sym_hints = SYMFIX.hints(sch) if v2 else []
    fit_now = _fit_measure(sch, blocks, frame_boxes, ref0) if (v2 and frames and page_info) else None
    sch.prune_lib_symbols()
    E.add_layout_group(sch, rect_uuids)
    E.deterministic_uuids(sch, old_ids)
    del old_nodes
    loose = []
    grp = {}
    for gi, g in enumerate(O.geo_groups(sch)):
        for p in g["pins"]:
            grp.setdefault(p[0], set()).add(gi)
    sizes_g = {}
    for gi, g in enumerate(O.geo_groups(sch)):
        sizes_g[gi] = len({p[0] for p in g["pins"]})
    for b in blocks:
        for r in b.fallback:
            if all(sizes_g.get(gi, 1) <= 1 for gi in grp.get(r, ())):
                loose.append(r)
    return {"paper": pp, "blocks": [{"title": (titles or {}).get(b.info.id, b.info.title),
                                     "parts": len(b.info.anchors) + len(b.info.sats)} for b in blocks],
            "loose_parts": sorted(loose, key=_refkey), "emit": em, "labels": len(labels), "power_symbols": len(powers),
            "user_notes": user_notes, "idioms": _idiom_stats(blocks, D) if v2 else None,
            "notes": note_stats if v2 else None, "text": text_stats, "symbol_hints": sym_hints,
            "fit": fit_now["fit"] if fit_now else None, "fit_pad": fit_now["pad"] if fit_now else None,
            "fit_used": bool(fit),
            "page": ({"columns": page_info["columns"], "channels": page_info["channels"],
                      "packed": bool(page_info.get("pack")),
                      "flow_violations": FLOW.violations(F, page_info["col_of"]), "cross_block_wires": crossed,
                      "notes_rect": PAGE.notes_rect(pp, frame_boxes)} if page_info else None),
            "route": {"requests": R.stats["routes"], "routed": routed, "crossings": R.stats["crossings"],
                      "expanded": R.stats["expanded"], "seconds": round(t_route, 3)},
            "place_seconds": round(t_place, 3)}


def _fit_ref(b):
    """A point that moves with the block (its first symbol)."""
    return tuple(b.syms[0].at[:2]) if getattr(b, "syms", None) else None


def _fit_box(b, e=None) -> list:
    """The box a block draws in: its bbox, corrected by what an earlier drawing measured (layout_pro `fit`)."""
    bb = b.bbox()
    return [bb[0] - e[0], bb[1] - e[1], bb[2] + e[2], bb[3] + e[3]] if e else bb


def _fit_measure(sch: Schematic, blocks: list, frame_boxes: list, ref0: dict) -> dict:
    """(package D) What every block really drew - symbols, pins, wires, names, supply symbols, Reference / Value
    texts as KiCad draws them (textbox.scene), without the notes - against the box the block had reserved when the
    sheet was composed. -> {"fit": {block id: (left, top, right, bottom)} for the next drawing,
    "pad": {block id: (left, under the title, right, bottom)} free millimetres inside the frame now}."""
    from . import textbox as TB
    sc = TB.scene(sch)
    items = [t["box"] for t in sc["texts"] if t["kind"] != "note"]
    notes = [t["box"] for t in sc["texts"] if t["kind"] == "note" and not t.get("title")]
    items += [[min(g[0], g[2]), min(g[1], g[3]), max(g[0], g[2]), max(g[1], g[3])] for _o, g in sc["segs"]]
    items += [[q[0] - 0.5, q[1] - 0.5, q[0] + 0.5, q[1] + 0.5] for q in sc["dots"]]
    items += [[n["at"][0] - 0.64, n["at"][1] - 0.64, n["at"][0] + 0.64, n["at"][1] + 0.64] for n in sc["ncs"]]
    out = {"fit": {}, "pad": {}}
    for b, fb in zip(blocks, frame_boxes):
        bb0, r0 = ref0.get(id(b), (None, None))
        r1 = _fit_ref(b)
        if bb0 is None or r0 is None or r1 is None:
            continue
        got = [q for q in items if fb[0] <= (q[0] + q[2]) / 2 <= fb[2] and fb[1] <= (q[1] + q[3]) / 2 <= fb[3]]
        # (fix round 6) the Reference / Value of the block's own parts count wherever they stand: a text that was
        # pushed out over the frame line is what the frame has to grow for
        mine = {s_.ref for s_ in getattr(b, "syms", ())}
        got += [t["box"] for t in sc["texts"] if t["kind"] == "field" and t["owner"] in mine and t["box"] not in got]
        for w in sc["wires"]:
            # a wire that leaves the frame (to a neighbour) is not what the frame has to hold
            if fb[0] - 0.01 <= min(w[0], w[2]) and max(w[0], w[2]) <= fb[2] + 0.01 \
                    and fb[1] - 0.01 <= min(w[1], w[3]) and max(w[1], w[3]) <= fb[3] + 0.01:
                got.append([min(w[0], w[2]), min(w[1], w[3]), max(w[0], w[2]), max(w[1], w[3])])
        if not got:
            continue
        c = [min(q[0] for q in got), min(q[1] for q in got), max(q[2] for q in got), max(q[3] for q in got)]
        # the notes of the frame: a stack that stands under the whole drawing has the strip that was kept for it
        # (and will have it again); a note beside the part it explains is part of the drawing
        strip = False
        for q in notes:
            if not (fb[0] <= (q[0] + q[2]) / 2 <= fb[2] and fb[1] + TITLE_H <= (q[1] + q[3]) / 2 <= fb[3]):
                continue
            if q[1] >= c[3] - 0.3 or q[1] < fb[1] + TITLE_H + 3.5:
                # (a note that found room only in the top corner of its frame, away from what it explains, gets
                # the strip under the drawing in the next one)
                strip = True
            else:
                got.append(q)
        c = [min(q[0] for q in got), min(q[1] for q in got), max(q[2] for q in got), max(q[3] for q in got)]
        dx, dy = r1[0] - r0[0], r1[1] - r0[1]              # the block has moved since it was measured for the page
        bb = [bb0[0] + dx, bb0[1] + dy, bb0[2] + dx, bb0[3] + dy]
        out["fit"][b.info.id] = (round(bb[0] - c[0], 2), round(bb[1] - c[1], 2), round(c[2] - bb[2], 2), round(c[3] - bb[3], 2),
                                 strip)
        out["pad"][b.info.id] = (round(c[0] - fb[0], 2), round(c[1] - fb[1] - TITLE_H, 2), round(fb[2] - c[2], 2),
                                 round(fb[3] - c[3], 2))
    return out


def _settle(blocks: list, pos: dict, col_of: dict, D: Design, note_room: dict | None = None, drawn: dict | None = None,
            room: dict | None = None):
    """pro2: where each block's drawing sits inside its frame. A drawing leans towards the column its nets go to
    (so that the wire across the channel is short) and is moved up or down until its terminals stand on the rows
    of the terminals they connect to; a block without such nets sits in the middle of its frame."""
    v = D.view

    def wirable(k):
        ln = v.nets.get(k)
        return ln is not None and k not in D.rails and len(ln.pins) <= route.X_MAX_TERMS
    tnets = [{} for _ in blocks]
    for i, b in enumerate(blocks):
        for k, p, _o in b.terms:
            if wirable(k):
                tnets[i].setdefault(k, []).append(p)
    owners: dict = {}
    for i in range(len(blocks)):
        for k in tnets[i]:
            owners.setdefault(k, []).append(i)
    col = [col_of.get(b.info.id, 0) for b in blocks]
    ncol = max(col) + 1 if col else 1
    placed: dict = {}
    rng: dict = {}

    def reach(i, j, k) -> bool:
        """Can net k be a WIRE between the frames i and j? Not when the two share a whole bus (two name columns,
        route.BUS_MIN) or stand further apart than a wire across frames may be long: a frame that goes up or down
        to meet rows it is only joined to by names stands at an arbitrary height for no reason."""
        if sum(1 for k2 in tnets[i] if j in owners[k2]) >= route.BUS_MIN:
            return False
        xi = [p[0] + rng[i][0] for p in tnets[i][k]]
        xj = [p[0] + rng[j][0] for p in tnets[j][k]]
        return min(abs(a - b) for a in xi for b in xj) <= route.X_MAX_LEN_MM
    mid: dict = {}
    slide: dict = {}
    seq = sorted(range(len(blocks)), key=lambda i: (col[i], pos[id(blocks[i])][1]))
    G2 = U if drawn else 2 * U                            # (a measured drawing is seated to the pin grid)
    for rnd in (0, 1):                                    # second round: the first columns follow the later ones
        for i in seq:
            b = blocks[i]
            if rnd == 0:
                x, y, cw, ch = pos[id(b)]
                ch -= (note_room or {}).get(id(b), 0.0)   # the strip under the drawing belongs to the frame's notes
                bb = drawn[id(b)] if drawn else b.bbox()
                bw, bh = bb[2] - bb[0], bb[3] - bb[1]
                padx = PADF if cw - bw >= 2 * PADF else max(0.0, (cw - bw) / 2)
                pady = pady_b = PADV if ch - TITLE_H - bh >= 2 * PADV else max(0.0, (ch - TITLE_H - bh) / 2)
                lo_x, hi_x = x + padx - bb[0], x + cw - padx - bw - bb[0]
                lo_y, hi_y = y + TITLE_H + pady - bb[1], y + ch - pady_b - bh - bb[1]
                if drawn:
                    # (package D) a measured drawing stands in the middle of its frame: the same air left and right,
                    # and under it what is over it plus the difference FIT_PAD - FIT_TOP (the title strip is air
                    # too). It keeps to the grid step next to that place
                    mx = x + (cw - bw) / 2 - bb[0]
                    my = y + TITLE_H + max(0.0, (ch - TITLE_H - bh - (FIT_PAD - FIT_TOP)) / 2) - bb[1]
                    lo_x, hi_x = mx - U / 2, mx + U / 2
                    lo_y, hi_y = my - U, my + U
                    if room and sum(1 for c_ in col if c_ == col[i]) == 1 and col[i] in room:
                        # a frame that has its column to itself goes up or down WITH its drawing (on the frame
                        # grid) until its terminals stand on the rows they are wired to
                        slide[i] = (my, y)
                        lo_y, hi_y = my - (y - room[col[i]][0]), my + (room[col[i]][1] - (y + pos[id(b)][3]))
                right = sum(1 for k in tnets[i] for j in owners[k] if col[j] > col[i])
                left = sum(1 for k in tnets[i] for j in owners[k] if col[j] < col[i])
                dx = hi_x if right > left else lo_x if left > right else None
                if drawn:
                    dx = None                             # a frame that hugs its drawing leaves nothing to lean in
                clampx = lambda t: min(max(snap(t, G2), math.ceil(lo_x / G2 - 1e-6) * G2), math.floor(hi_x / G2 + 1e-6) * G2)  # noqa: E731
                cy = lambda t: min(max(snap(t, G2), math.ceil(lo_y / G2 - 1e-6) * G2), math.floor(hi_y / G2 + 1e-6) * G2)  # noqa: E731
                # at home a drawing of the first column stands at the sheet's left edge, one of the last column at
                # the right edge (connectors at the edges, facing inward); the columns between are centred
                home = lo_x if (ncol > 1 and col[i] == 0) else hi_x if (ncol > 1 and col[i] == ncol - 1) else (lo_x + hi_x) / 2
                if drawn:
                    home = (lo_x + hi_x) / 2
                mid[i] = (clampx(home), cy((lo_y + hi_y) / 2))
                rng[i] = (mid[i][0] if dx is None else clampx(dx), lo_y, hi_y)
            dx, lo_y, hi_y = rng[i]
            want = []
            for k, pts in tnets[i].items():
                for j in owners[k]:
                    if j != i and j in placed and col[j] != col[i]:
                        if SETTLE_WIRED_ONLY and not reach(i, j, k):
                            continue
                        oy = sum(p[1] for p in tnets[j][k]) / len(tnets[j][k]) + placed[j]
                        want.append(oy - sum(p[1] for p in pts) / len(pts))
            if rnd and not want:
                continue
            dy = min(hi_y, max(lo_y, sorted(want)[len(want) // 2])) if want else slide[i][0] if i in slide else (lo_y + hi_y) / 2
            dy = min(max(snap(dy, G2), math.ceil(lo_y / G2 - 1e-6) * G2), math.floor(hi_y / G2 + 1e-6) * G2)
            placed[i] = dy
    for i, (my, y) in slide.items():
        # the frame follows in steps of its own grid; what is left over stays inside the play of the drawing
        step = round((placed[i] - my) / (2 * U)) * 2 * U
        x_, _y, w_, h_ = pos[id(blocks[i])]
        pos[id(blocks[i])] = (x_, round(y + step, 4), w_, h_)
        placed[i] = snap(min(max(placed[i], my + step - U), my + step + U), G2)
        mid[i] = (mid[i][0], snap(my + step, G2))         # "the middle of its frame" is where the frame now stands
    out = {}
    for i in seq:
        blocks[i].shift(rng[i][0], placed[i])
        out[i] = (round(rng[i][0] - mid[i][0], 4), round(placed[i] - mid[i][1], 4))
    return out


def _twin_rows(sch: Schematic, D: Design) -> list:
    """(package B) The contact row of a part with doubled pins (pins 1 / 2 of a tactile switch, joined by a straight
    link along the row): the strip with the two pin numbers above the link. No net name stands there."""
    out = []
    for s in sch.symbols(include_power=False):
        if not 3 <= D.npins(s.ref) <= 4:
            continue
        ps = s.pins_world()
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                a, b = ps[i], ps[j]
                k = D.pins_of.get(s.ref, {}).get(a["number"])
                if k is None or k != D.pins_of[s.ref].get(b["number"]) or abs(a["xy"][1] - b["xy"][1]) > 0.01                         or a["out"][0] * b["out"][0] >= 0:
                    continue
                x0, x1 = sorted((a["xy"][0], b["xy"][0]))
                out.append([x0 - 0.6, a["xy"][1] - 2.2, x1 + 0.6, a["xy"][1] - 0.4])
    return out


def _strap_boxes(sch: Schematic, D: Design) -> list:
    """The inside of a strap that joins two contacts of ONE small part (the doubled pins of a tactile switch): a
    Reference left in there reads as a text with a box drawn around it, so the text solver keeps the fields out."""
    hz = [(min(w["a"][0], w["b"][0]), max(w["a"][0], w["b"][0]), w["a"][1]) for w in sch.wires()
          if abs(w["a"][1] - w["b"][1]) < 0.01]
    out = []
    for s in sch.symbols(include_power=False):
        if not 3 <= D.npins(s.ref) <= 4:
            continue
        ps = s.pins_world()
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                a, b = ps[i], ps[j]
                k = D.pins_of.get(s.ref, {}).get(a["number"])
                if k is None or k != D.pins_of[s.ref].get(b["number"]) or abs(a["xy"][1] - b["xy"][1]) > 0.01                         or a["out"][0] * b["out"][0] >= 0:
                    continue
                x0, x1 = sorted((a["xy"][0], b["xy"][0]))
                y = a["xy"][1]
                for wx0, wx1, wy in hz:
                    if wx0 <= x0 + 0.01 and wx1 >= x1 - 0.01 and 0.5 < abs(wy - y) <= 6 * U + 0.01:
                        out.append([wx0, min(y, wy) - 0.2, wx1, max(y, wy) + 0.2])
    return out


def _idiom_stats(blocks: list, D: Design) -> dict:
    """What the idiom templates drew: matches per idiom, dropped matches (with the reason), coverage = parts drawn
    from a proven template / all parts of the sheet."""
    by, dropped, claimed = {}, [], set()
    for b in blocks:
        st = b.stats or {}
        for i in st.get("matches", []):
            by[i] = by.get(i, 0) + 1
        dropped += [f"{i}: {w}" for i, w in st.get("dropped", [])]
        claimed |= st.get("claimed", set())
    return {"matches": by, "dropped": dropped[:12], "dropped_count": len(dropped), "claimed": len(claimed),
            "parts": len(D.units), "coverage": round(len(claimed) / max(1, len(D.units)), 3),
            "rehomed": [f"{r}: {a} -> {b}" for r, a, b in getattr(D, "rehomed", ())]}


BUS_MIN = 3


def _half_wired_buses(D, v, wires, terms, nets) -> set:
    """Nets of a bus (three or more named nets with one prefix - QSPI_SS, QSPI_SD0 ... - between the same two
    parts of five or more pins) of which some, but not all, are wired in one piece. -> those nets."""
    groups: dict = {}
    for k in nets:
        if not D.named(k):
            continue
        big = frozenset(r for r, _p in v.nets[k].pins if D.npins(r) >= 5)
        if len(big) != 2:
            continue
        pre = route.bus_prefix(D.text(k))
        if pre:
            groups.setdefault((pre, big), []).append(k)
    out: set = set()
    for ks in groups.values():
        if len(ks) < BUS_MIN:
            continue
        whole = [k for k in ks if sum(1 for c in route.components(wires, k, terms) if c[1]) <= 1]
        if 0 < len(whole) < len(ks):
            out |= set(ks)
    return out


def _name_on_wire(k, ent, wires, hard, labels, tips=(), frames=(), prefer=None):
    """Where the name of a net that is wired in one piece reads best: ON its longest free horizontal wire (the
    way a person names a net), not on a stub that dangles off it. -> point or None.
    prefer = (wire end, direction): the name stands right there when it can - at the host's end of a bundle wire, so
    that the names of a bundle form one column beside the pins (bundle.py)."""
    rows: dict = {}
    for n, a, b in wires:
        if n == k and a[1] == b[1] and a[0] != b[0]:
            rows.setdefault(round(a[1], 4), []).append(tuple(sorted((a[0], b[0]))))
    runs = []
    for y, segs in rows.items():
        segs.sort()
        cur = list(segs[0])
        for x0, x1 in segs[1:]:
            if x0 <= cur[1] + 0.01:
                cur[1] = max(cur[1], x1)
            else:
                runs.append((cur[0], cur[1], y))
                cur = [x0, x1]
        runs.append((cur[0], cur[1], y))
    tw = text_width(ent[0]) + (4.0 if ent[1] != "local" else 0.0)
    others = [wbox(a, b, 0.05) for n, a, b in wires if not (n == k and a[1] == b[1])]
    placed = [lbl_box(e[0], q, {0: (1, 0), 180: (-1, 0), 90: (0, -1), 270: (0, 1)}[ang], e[1]) for _, e, q, ang in labels]
    tees = [a for n, a, b in wires if n == k and a[0] == b[0]] + [b for n, a, b in wires if n == k and a[0] == b[0]]
    # a part pin that ends ON the run (a filter cap hanging from the wire) makes a junction dot just like a wire
    # does: the name may not stand over it either
    for x0, x1, y in runs:
        tees += [q for q in tips if abs(q[1] - y) < 0.01 and x0 + 0.01 < q[0] < x1 - 0.01]

    def free(p, y):
        lb = lbl_box(ent[0], p, (1, 0), ent[1])
        lb = [lb[0], lb[1], lb[2], min(lb[3], y - 0.2)]
        if any(hit(lb, o, -0.5) for o in hard) or any(hit(lb, o, 0.1) for o in others) or \
                any(hit(lb, o, -0.3) for o in placed):
            return False
        if any(abs(t[1] - y) < 0.01 and p[0] - 1.0 < t[0] < p[0] + tw + 0.6 for t in tees):
            return False                                  # not across a junction of its own net
        if any(fb[1] - 0.01 <= y <= fb[3] + 0.01 and p[0] - 1.5 < fx < p[0] + tw + 1.5
               for fb in frames for fx in (fb[0], fb[2])):
            return False                                  # not on / across the border of a frame (fix round 3)
        if any(n != k and min(a[0], b[0]) - 0.01 <= p[0] <= max(a[0], b[0]) + 0.01
               and min(a[1], b[1]) - 0.01 <= y <= max(a[1], b[1]) + 0.01 for n, a, b in wires):
            return False                                  # a label on a crossing would join both wires
        return True
    if prefer is not None:
        (hx, hy), o = prefer
        for x0, x1, y in runs:
            if abs(y - hy) > 0.01 or not (x0 - 0.01 <= hx <= x1 + 0.01):
                continue
            for j in (1, 2, 3, 4, 0):
                px = snap(hx + j * U) if o[0] > 0 else snap(math.floor((hx - j * U - tw) / U) * U)
                if px >= x0 - 0.01 and px + tw <= x1 + 0.01 and free((px, y), y):
                    return (px, y)
    for x0, x1, y in sorted(runs, key=lambda r: (-(r[1] - r[0]), r[2], r[0])):
        if x1 - x0 < tw + U - 0.01:
            break
        n_ = int(round((x1 - x0 - tw) / U))
        for i in sorted(range(1, max(2, n_)), key=lambda i: (abs(i - n_ / 2), i)):
            p = (snap(x0 + i * U), y)
            if free(p, y):
                return p
    return None


def _park_notes(sch: Schematic, frame_boxes: list, pw: float, ph: float) -> dict:
    """The user's own notes and graphics survive a re-layout, but where they sat means nothing in the new drawing
    (a note ended up on top of a regulator). Plain text notes are stacked in the free strip under the blocks;
    everything else (lines, boxes, images, rotated text) stays where it is and is reported.
    -> {"moved": n, "left_in_place": n}"""
    items = E.user_graphics(sch)
    if not items:
        return {}
    bottom = max((fb[3] for fb in frame_boxes), default=MARGIN)
    left = min((fb[0] for fb in frame_boxes), default=MARGIN)
    tb_left, tb_top = pw - 10 - 112 - 4, ph - 10 - 36 - 4
    y = snap(bottom + 3 * U)
    moved = stay = 0
    for n in sorted((n for n in items), key=lambda n: (str(n[0]) != "text", str(sexpr.value(n, "uuid", "")))):
        at = sexpr.find(n, "at")
        if str(n[0]) != "text" or at is None or len(at) < 3 or (len(at) > 3 and float(at[3] or 0) % 360 != 0):
            stay += 1
            continue
        eff = sexpr.find(n, "effects") or []
        size = 1.27
        font = sexpr.find(eff, "font")
        sz = sexpr.find(font, "size") if font else None
        if sz and len(sz) > 1:
            try:
                size = float(sz[1])
            except (TypeError, ValueError):
                pass
        lines = str(n[1]).replace("\n", "\n").split("\n")
        w = max(text_width(ln, size) for ln in lines)
        h = size * 1.62 * len(lines)
        just = [str(x) for x in (sexpr.find(eff, "justify") or [])[1:]]
        x_right = left + w
        if y + h > ph - MARGIN or (x_right > tb_left and y + h > tb_top) or x_right > pw - MARGIN:
            stay += 1
            continue
        ax = left if "left" in just else left + w if "right" in just else left + w / 2
        ay = y + h if "bottom" in just else y if "top" in just else y + h / 2
        at[1], at[2] = E.num(ax), E.num(ay)
        y = snap(y + h + U, U)
        moved += 1
    return {"moved": moved, "left_in_place": stay}


# ───────────────────────── orchestration ─────────────────────────
def sheet_state(sch: Schematic, nl: dict | None = None) -> str:
    """'empty' | 'engine' (untouched engine output) | 'machine' (label-style, no hand-drawn wiring) | 'hand'."""
    if not sch.symbols(include_power=False):
        return "empty"
    if E.user_graphics(sch):
        return "hand"
    if E.is_engine_layout(sch):
        return "engine"
    # Copper's label style never wires three different parts together (it draws 2-pin chains and labels)
    if sum(1 for g in O.geo_groups(sch) if len({p[0] for p in g["pins"]}) >= 3) >= 2:
        return "hand"
    # label style: (almost) every wire starts at a pin; a hand-drawn sheet routes wires between wires
    tips = set()
    for s in sch.symbols():
        for p in s.pins_world():
            tips.add((round(p["xy"][0], 2), round(p["xy"][1], 2)))
    ws = sch.wires()
    free = sum(1 for w in ws if (round(w["a"][0], 2), round(w["a"][1], 2)) not in tips
               and (round(w["b"][0], 2), round(w["b"][1], 2)) not in tips)
    if ws and (free > 0.25 * len(ws) or len(sch.items("junction")) > max(4, 0.25 * len(ws))):
        return "hand"
    return "machine"


def _load_text(text: str, f: Path, pr, spath) -> Schematic:
    return Schematic(sexpr.loads(text), f, pr.name, spath)


def _brief(m: dict) -> dict:
    return {k: m[k] for k in ("score", "score_v1", "labels", "overlaps", "wire_crossings", "wired_local_ratio", "paper",
                              "text_collisions")}


def relayout(pr, sheet: str | None = None, *, dry_run: bool = False, while_open: str = "refuse", style: str = "pro",
             route_mode: str = "auto", blocks: list | None = None, frames: bool = True, title_block: dict | None = None,
             paper: str | None = None, force: bool = False, implicit: bool = False,
             near: dict | None = None, symfix: str = "hint") -> dict:
    """Re-lay one sheet. Writes only what passed the identity gate; otherwise raises LAYOUT_REFUSED /
    LAYOUT_UNSUPPORTED or answers style_used 'kept' / 'unchanged' - in all those cases the file is untouched."""
    T0 = time.time()
    f, spath = O.sheet_file(pr, sheet)
    if not f.exists():
        raise ToolError(f"{f.name} does not exist", code="NOT_FOUND")
    if style not in ("pro", "pro2", "labels"):
        raise ToolError("style must be 'pro', 'pro2' or 'labels'", code="BAD_ARGUMENT")
    if symfix not in ("off", "hint", "auto"):
        raise ToolError("symfix must be 'off', 'hint' or 'auto'", code="BAD_ARGUMENT")
    if paper and paper not in PAPER and paper not in PAGE.SIZES:
        raise ToolError(f"unknown paper {paper!r}", code="BAD_ARGUMENT", fix="one of " + ", ".join(PAPERS))
    if not dry_run:
        O._guard(pr, f, while_open)
    base_rev = P.revision(f)
    sch0 = O.load_sheet(pr, sheet, create=False)
    spath = sch0.sheet_path
    if sch0.items("bus") or sch0.items("bus_entry"):
        raise gate.unsupported("this sheet has buses; re-layout does not re-route buses")
    if sch0.items("sheet"):
        raise gate.unsupported("this sheet contains hierarchical sheet symbols; only leaf sheets are re-laid",
                               "run sch_tidy(sheet='<child>.kicad_sch') on each sub-sheet")
    for k in ("directive_label", "netclass_flag", "rule_area"):
        if sch0.items(k):
            raise gate.unsupported(f"this sheet has {k.replace('_', ' ')}s, which a re-layout cannot re-attach yet")
    if getattr(sch0, "other_paths", None):
        raise gate.unsupported(f"{f.name} is used by {len(sch0.other_paths) + 1} sheet instances; a shared sheet is not "
                               f"re-laid", "edit this sheet with sch_apply (move / connect / disconnect) instead")
    if not sch0.symbols(include_power=False):
        return {"file": str(f), "style_used": "kept", "reason": "the sheet has no components", "dry_run": dry_run}
    try:
        kmajor = env.kicad().major
    except ToolError:
        kmajor = 0
    b = gate.baseline(pr, f, spath, with_erc=False)
    try:
        state = sheet_state(sch0)
        D0 = Design(sch0, b.nl)
        before = score.measure(sch0, b.nl, D0)
        res = {"file": str(f), "dry_run": dry_run, "readability": {"before": _brief(before)}}
        if state == "hand" and not force:
            res.update(style_used="kept", reason="this sheet was wired by hand (or carries notes / graphics); "
                       "re-laying it would discard that drawing. Pass force=true to re-lay it anyway.",
                       orphan_graphics=len(E.user_graphics(sch0)))
            return res
        hints, titles, notes = {}, {}, {}
        for h in blocks or []:
            if not isinstance(h, dict) or not h.get("id"):
                continue
            if h.get("title"):
                titles[str(h["id"])] = " ".join(str(h["title"]).split())[:60]      # one line: it sits in the title strip
            if h.get("note"):
                notes[str(h["id"])] = str(h["note"])
            for m in h.get("members") or []:
                hints[str(m)] = str(h["id"])
        # style ladder, best first: pro2 -> pro2-safe -> pro -> pro-plain -> labels. At most three candidates reach
        # the kicad-cli gate (each costs several round trips); the union-find pre-gate is free and runs on all.
        rungs = []
        if style == "pro2":
            if route_mode != "off":
                # symfix Level 2 is a candidate of its own in front of the ladder: the gate decides (with the
                # library-symbol ERC checks NOT exempt for the parts it touches); refused -> plain pro2, Level 1
                if symfix == "auto" and any(SYMFIX.may_redraw(k) and SYMFIX.derive(sch0.lib_symbol(k)) is not None
                                            for k in SYMFIX.scan(sch0)):
                    rungs.append("pro2+symfix")
                rungs.append("pro2")
            rungs.append("pro2-safe")
        if style in ("pro", "pro2"):
            if route_mode != "off":
                rungs.append("pro")
            rungs.append("pro-plain")
        if state != "hand":
            rungs.append("labels")
        sig0 = route.signature(sch0, False), route.signature(sch0, True)
        gated = 0
        other_refs = E.other_sheet_refs(pr, f)
        tried = []
        winner = None
        for rung in rungs:
            info = {}
            try:
                if gated >= 3 and rung != rungs[-1]:
                    tried.append({"style": rung, "refused": "skipped (three candidates were already gated)"})
                    continue
                if rung == "labels":
                    text = _labels_candidate(pr, f, sheet)
                else:
                    hx = dict(hints)

                    def draw(flag_on_wire=True, fit=None, columns=None, on_paper=None):
                        for attempt in (0, 1):
                            sch_ = O.load_sheet(pr, sheet, create=False)
                            fx = SYMFIX.apply(sch_) if rung == "pro2+symfix" else {}
                            D_ = Design(sch_, b.nl, hints=hx, near=near)
                            try:
                                inf = layout_pro(sch_, D_, pr.name, use_router=rung in ("pro", "pro2", "pro2+symfix"),
                                                 v2=rung.startswith("pro2"), frames=frames and kmajor >= 10 or
                                                 (frames and kmajor == 0), paper=paper or on_paper, titles=titles, notes=notes,
                                                 title_block=title_block, other_refs=other_refs,
                                                 undock_ok=attempt == 0, flag_on_wire=flag_on_wire, fit=fit,
                                                 columns=columns)
                                return sch_, fx, D_, inf
                            except Undock as u:           # a connector in a middle column: a block of its own
                                hx.update({c: c for c in u.refs if c not in hx})
                    both = rung in ("pro2", "pro2+symfix") and BUILD.FLAG_ON_RAIL_WIRE
                    on_wire = True
                    try:
                        sch, fixed_syms, D, info = draw()
                    except LayoutError:
                        if not both:
                            raise
                        both = False
                        on_wire = False
                        sch, fixed_syms, D, info = draw(False)
                    if both and any(ln.flags and k in D.rails for k, ln in D.view.nets.items()):
                        # a PWR_FLAG on its rail's own wire may stand in the lane a signal wire needs (flags are
                        # placed before the router runs): the drawing with the flags beside their supply symbols
                        # is built too, and wins when it has fewer names or fewer crossings
                        try:
                            alt = draw(False)
                        except Exception:  # noqa: BLE001 - the alternative is optional
                            alt = None
                        if alt is not None and (alt[3]["labels"], alt[3]["route"]["crossings"]) <                                 (info["labels"], info["route"]["crossings"]):
                            sch, fixed_syms, D, info = alt
                            on_wire = False
                    if rung.startswith("pro2"):
                        sch, fixed_syms, D, info = _refit(draw, on_wire, (sch, fixed_syms, D, info), sig0)
                        got = _paper_guard(draw, on_wire, (sch, fixed_syms, D, info), sig0, paper)
                        if got is not None:
                            sch, fixed_syms, D, info = got
                    errs = route.verify(sch, D)
                    if errs:
                        raise LayoutError("pre-check: " + "; ".join(errs[:4]))
                    if sig0[0] == sig0[1]:              # the original itself must read the same both ways
                        errs = route.verify_partition(sch, D, sig0[0])
                        if errs:
                            raise LayoutError("pre-gate: " + "; ".join(errs[:4]))
                    text = sch.text()
                    if fixed_syms:
                        info["symfix"] = {k: {x: v_[x] for x in ("refs", "fanned", "offset")} for k, v_ in fixed_syms.items()}
            except LayoutError as e:
                tried.append({"style": rung, "refused": str(e)})
                continue
            except ToolError as e:
                tried.append({"style": rung, "refused": e.message})
                continue
            except Exception as e:  # noqa: BLE001 - a layout bug must cost one candidate, never the user's sheet
                tried.append({"style": rung, "refused": f"layout engine error ({type(e).__name__}: {str(e)[:120]})"})
                continue
            gated += 1
            g = gate.check(pr, b, text, spath, symfix=rung == "pro2+symfix")
            if g.ok:
                winner = (rung.split("+")[0], g, info)
                break
            tried.append({"style": rung, "refused": g.reasons[:6]})
        if winner is None:
            raise ToolError("no re-layout of this sheet passed the identity checks (netlist, net names, flags, ERC) - nothing was written",
                            code="LAYOUT_REFUSED", data={"tried": tried, "readability": res["readability"]},
                            fix="leave the sheet as it is, or edit it with sch_apply (move / connect)")
        rung, g, info = winner
        sch1 = _load_text(g.text, f, pr, spath)
        after = score.measure(sch1, b.nl, idioms=(info.get("idioms") or {}).get("coverage") if info else None)
        res["readability"]["after"] = _brief(after)
        res["connectivity"] = (f"identical (kicad-cli netlist of the whole project {g.hash_before} = {g.hash_after}"
                               + (f"; kept {g.kept}" if g.kept else "") + ")")
        res["erc"] = {"before": g.erc_before, "after": g.erc_after}
        if tried:
            res["tried"] = tried
        worse = (after["overlaps"] > before["overlaps"] or after["wire_through_text_or_body"] > before["wire_through_text_or_body"]
                 or after["wired_local_ratio"] < before["wired_local_ratio"])
        if g.text == f.read_text(encoding="utf-8") or (state == "engine" and worse):
            res.update(style_used="unchanged", paper=PAGE.paper_of(sch0))
            res["readability"]["after"] = res["readability"]["before"]
            return res
        res.update(style_used=rung, paper=PAGE.paper_of(sch1))
        if info:
            res["blocks"] = info["blocks"]
            if info["loose_parts"]:
                res["loose_parts"] = info["loose_parts"]
            res["route"] = info["route"]
            if info.get("idioms"):
                res["idioms"] = info["idioms"]
            if info.get("page"):
                res["page"] = info["page"]
            if info.get("user_notes"):
                res["user_notes"] = info["user_notes"]
            for k in ("notes", "text", "symfix"):
                if info.get(k):
                    res[k] = info[k]
        hints_ = []
        if len(D0.units) > 40 or len(D0.blocks) > 6:
            hints_.append("a design this size reads better split into hierarchical sheets (one per function)")
        if symfix != "off" and info:
            hints_ += info.get("symbol_hints") or []
            if (info.get("text") or {}).get("unplaced"):
                hints_.append("no free place for the texts of " + ", ".join(info["text"]["unplaced"][:6])
                              + " - they overlap something: look at the render")
        if hints_:
            res["hint"] = "; ".join(hints_)[:600]
        res["seconds"] = round(time.time() - T0, 2)
        res["cli_calls"] = b.cli_calls
        res["metrics"] = {k: v for k, v in after.items() if k != "overlap_list"}
        if dry_run:
            return res
        res["checkpoint"] = P.checkpoint(pr, "sch_layout" if implicit else "sch_tidy")
        P.atomic_write(f, g.text, base_rev)
        res["revision"] = P.revision(f)
        return res
    finally:
        b.close()


def _touching(inf: dict) -> int:
    """Texts of a drawing that still touch something after the text solver (the metric text_collisions)."""
    return int((((inf.get("text") or {}).get("exact") or {}).get("after")) or 0)


def _refit(draw, on_wire: bool, first: tuple, sig0: tuple) -> tuple:
    """(package D) The sheet is drawn again with the sizes its blocks really have (`layout_pro(fit=...)`): frames
    that hug their drawings, and the smallest paper those frames fit on. A later drawing replaces the earlier one
    only when it passes the same pre-checks, keeps every frame clear of its content and has no more names and
    crossings; `first` = (sch, fixed symbols, Design, info) of the drawing made from the estimates.
    Tried in this order: the sheet composed anew for the measured sizes; the arrangement of the first drawing with
    the measured sizes (when the new arrangement cost a wire)."""
    best = first
    trace = []
    taken = []

    def key(inf):
        return (inf["labels"], inf["route"]["crossings"])

    def attempt(fit, columns, on_paper=None):
        try:
            cand = draw(on_wire, fit, columns, on_paper) if on_paper else draw(on_wire, fit, columns)
        except Exception as e:  # noqa: BLE001 - the second drawing is optional
            trace.append(f"not drawn ({type(e).__name__}: {str(e)[:80]})")
            return None, None
        inf = cand[3]
        errs = route.verify(cand[0], cand[2]) or (route.verify_partition(cand[0], cand[2], sig0[0]) if sig0[0] == sig0[1] else [])
        if errs:
            trace.append("pre-check: " + str(errs[0])[:80])
            return None, None
        pads = [v for p_ in (inf.get("fit_pad") or {}).values() for v in p_]
        fits = bool(pads) and min(pads) >= FIT_MIN
        ok = fits and key(inf) <= key(first[3])
        if ok and REFIT_KEEPS_TEXT:
            # (class P) ... and no text that touches something (a net name across the border of a frame that now hugs
            # its drawing): the smaller sheet is not worth a name one cannot read
            ok = _touching(inf) <= _touching(first[3])
        if ok and REFIT_KEEPS_WIRES:
            # (fix round 6) ... and no wire between two frames becomes a pair of names (the measured sizes may suggest
            # stacking the amplifier under the converter it feeds; a name saved elsewhere must not pay for that)
            ok = ((inf.get("page") or {}).get("cross_block_wires") or 0) >= ((first[3].get("page") or {}).get("cross_block_wires") or 0)
        trace.append({"paper": inf["paper"], "taken": ok, "pad_min": min(pads) if pads else None,
                      "names_crossings": [list(key(first[3])), list(key(inf))], "kept_columns": bool(columns)})
        return cand, (ok, fits)
    for columns in (None, (first[3].get("page") or {}).get("columns")):
        fit = dict(first[3].get("fit") or {})
        done = False
        for _ in range(FIT_PASSES):
            if not fit:
                break
            cand, st = attempt(fit, columns)
            if cand is None:
                break
            if st[0]:
                # (fix round 6) a drawing that was taken may still hold room it no longer needs (a name of the first
                # drawing became a wire): drawn once more with its own measured sizes, kept when that passes too
                new = dict(cand[3].get("fit") or {})
                # (class L) ... or the strip that was kept under a drawing for its notes: the notes found room beside
                # their parts this time (a name column became wires), the frame would stand 9 mm under its drawing
                if REFIT_BOTH and any(k in fit and (max(abs(a - b) for a, b in zip(new[k][:4], fit[k][:4])) >= 2.54
                                                    or (len(fit[k]) > 4 and fit[k][4] and not new[k][4])) for k in new):
                    cand2, st2 = attempt(new, columns)
                    if REFIT_KEEPS_TEXT and cand2 is not None and not st2[0] and st2[1] and cand2[3].get("paper") != cand[3].get("paper")                             and _touching(cand2[3]) > _touching(first[3]):
                        # (class P) the smaller sheet left a name across a frame border: the same sizes on the sheet
                        # the drawing has (the room a name no longer needs is still given back)
                        cand2, st2 = attempt(new, columns, cand[3].get("paper"))
                    if cand2 is not None and st2[0] and key(cand2[3]) <= key(cand[3]) and                             ((cand2[3].get("page") or {}).get("cross_block_wires") or 0) >= ((cand[3].get("page") or {}).get("cross_block_wires") or 0):
                        cand = cand2
                taken.append(cand)
                done = True
                break
            if st[1]:
                break                                     # it fits, but it cost a name or a crossing
            new = dict(cand[3].get("fit") or {})
            if all(k in fit and max(abs(a - b) for a, b in zip(new[k], fit[k])) < 0.7 for k in new):
                break                                     # measured as before: one more drawing changes nothing
            fit = new
        if done and not REFIT_BOTH:
            break
    if taken:
        # (fix round 6) both second drawings are made - the sheet composed anew and the first arrangement with the
        # measured sizes - and the one with fewer names, fewer crossings, more wires between frames is kept (the new
        # composition on a draw)
        def key2(c):
            return (c[3]["labels"], c[3]["route"]["crossings"], -((c[3].get("page") or {}).get("cross_block_wires") or 0))
        best = min(taken, key=key2)
    if best[3].get("page") is not None:
        best[3]["page"]["fit"] = trace
    return best


def _paper_guard(draw, on_wire: bool, cur: tuple, sig0: tuple, paper):
    """(class L) A bundle may not cost a paper size. Wires between a header and its IC make the block larger than
    two columns of names did; when the sheet as drawn needs a paper beyond GUARD_FROM (A3 and up: the drawing is
    then a third of its sheet), it is drawn once more without bundles, and that drawing is taken when it fits a
    smaller paper. -> the drawing to use instead, or None."""
    from . import bundle as BUNDLE
    inf = cur[3]
    if not (PAPER_GUARD and BUNDLE.ENABLED and paper is None and ((inf.get("idioms") or {}).get("matches") or {}).get("bundle")):
        return None
    order = PAGE.PAPERS
    if inf.get("paper") not in order or order.index(inf["paper"]) <= order.index(GUARD_FROM):
        return None
    BUNDLE.ENABLED = False
    try:
        alt = draw(on_wire)
        fit = dict(alt[3].get("fit") or {})
        probe = draw(on_wire, fit, None) if fit else alt     # the paper the measured frames fit on
        # (fix round 7) with the B sizes between the A sizes a step is half a paper size. The guard keeps a sheet ON
        # the paper GUARD_FROM (A4): a drawing that needs more than that either way keeps its wires (the flash of an
        # RP2040 sheet on A3 is not un-wired to reach B4)
        got = min(order.index(x[3]["paper"]) if x[3].get("paper") in order else 99 for x in (alt, probe))
        if got >= order.index(inf["paper"]) or (PAGE.B_SERIES and got > order.index(GUARD_FROM)):
            return None
        alt = _refit(draw, on_wire, alt, sig0)
        if route.verify(alt[0], alt[2]) or (sig0[0] == sig0[1] and route.verify_partition(alt[0], alt[2], sig0[0])):
            return None
    except Exception:  # noqa: BLE001 - the alternative is optional
        return None
    finally:
        BUNDLE.ENABLED = True
    a_inf = alt[3]
    if a_inf.get("paper") in order and order.index(a_inf["paper"]) < order.index(inf["paper"]) and             not (PAGE.B_SERIES and order.index(a_inf["paper"]) > order.index(GUARD_FROM)):
        if a_inf.get("page") is not None:
            a_inf["page"]["paper_guard"] = f"drawn without bundles: {a_inf['paper']} instead of {inf['paper']}"
        return alt
    return None


def _labels_candidate(pr, f: Path, sheet) -> str:
    """The label-style tidy, run on a staged copy of the project; returns the text it produced."""
    import shutil
    from ...project import Project
    td = gate.stage(pr)
    try:
        spr = Project(td, pr.name)
        rel = f.resolve().relative_to(pr.dir.resolve())
        O.tidy(spr, None if O._same(f, pr.sch) else str(rel).replace("\\", "/"))
        return (td / rel).read_text(encoding="utf-8")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def readability(pr, sheet: str | None = None) -> dict:
    """Metrics of a sheet as it is (one netlist export)."""
    from ..netlist import export_netlist
    f, spath = O.sheet_file(pr, sheet)
    if not f.exists():
        raise ToolError(f"{f.name} does not exist", code="NOT_FOUND")
    sch = Schematic.load(f, pr.name, spath)
    m = score.measure(sch, export_netlist(pr))
    m["state"] = sheet_state(sch)
    return m
