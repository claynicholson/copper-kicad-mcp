"""Page composition for "pro2" (docs/internal/SCHEMATIC_PRO_V2_DESIGN.md section 9).

Frames stand in rank columns on a 2.54 mm grid: the frames of a column share its left edge and its width, columns
share the top line, the gap between two columns (the channel) is sized by the nets that cross it. The smallest
paper that holds the drawing is taken; what is left over goes into the channels first, then into the frames, and
the whole arrangement is centred. Nothing comes closer than 10.16 mm to the drawing border, and the title-block
corner stays free. engine.compose / _compose_paper remain the composer of the "pro" rung.

Class P (the sheet is used): the smallest paper still wins; on it the arrangement is chosen whose outline covers
the sheet and has its proportions (COVER_W, ASPECT_W; a column that ends over the title block is not "short"),
without drawing a signal right to left or up a column (FLOW_POS_W, the order inside a column) and without putting
two frames that could be wired into one column (LOST_WIRE_W). Every arrangement is tried with the rows of a
column filled strictly (narrow frames only) and packed (PACK). The spare width of the sheet goes into the
channels up to CH_EXTRA_MAX each, the rest into two equal margins.
"""
from __future__ import annotations

import itertools
import math

from ..model import PAPER

G = 2.54
# (package D) the smallest sheet the frames fit on: a timer and its supply input on A4 are two small frames in
# the middle of an empty sheet - the same drawing fills an A5 sheet
PAPERS = ["A5", "A4", "A3", "A2", "A1", "A0"]
# (fix round 7, class P) the ISO B sizes stand between two A sizes: a drawing that is too large for A4 by a few
# centimetres stood on A3 with half the sheet empty and its texts small at page scale. KiCad has no name for them,
# the sheet is written as (paper "User" w h) - see set_paper / paper_of. B_SERIES = False: A sizes only, as before
B_SERIES = True
SIZES = {"B5": (250.0, 176.0), "B4": (353.0, 250.0), "B3": (500.0, 353.0)}
if B_SERIES:
    PAPERS = ["A5", "B5", "A4", "B4", "A3", "B3", "A2", "A1", "A0"]


def dims(pp: str):
    """(width, height) in mm of a paper name: an A size, a B size, or 'User WxH' as paper_of names any other sheet."""
    if pp in SIZES:
        return SIZES[pp]
    if pp in PAPER:
        return PAPER[pp]
    m = pp.split()
    if len(m) == 2 and m[0] == "User" and "x" in m[1]:
        try:
            w, h = (float(v) for v in m[1].split("x", 1))
            return w, h
        except ValueError:
            pass
    return PAPER["A3"]


def paper_of(sch) -> str:
    """The paper name of a sheet as this module knows it ('A4', 'B4', 'User 300x200')."""
    name = sch.paper
    if name != "User":
        return name
    w, h = sch.page_size()
    for k, (kw, kh) in SIZES.items():
        if abs(kw - w) < 0.6 and abs(kh - h) < 0.6:
            return k
    return f"User {w:g}x{h:g}"


def set_paper(sch, pp: str):
    """Write the paper of a sheet: KiCad's own names as they are, every other size as a user size."""
    if pp in PAPER:
        sch.set_paper(pp)
    else:
        w, h = dims(pp)
        from ... import sexpr
        sexpr.set_value(sch.tree, "paper", "User", *(int(v) if float(v).is_integer() else v for v in (w, h)))
BORDER = 20.32            # sheet edge -> first frame: the 10 mm drawing border + 10.16 mm (+ rounding to the grid)
GUT = 5.08                # between two frames of a column; the least channel between two columns
TRACK = 2.54              # channel width per net that crosses it
TRACKS_MAX = 10           # a wider bus is drawn as label columns, not as wires in the channel
CH_EXTRA_MAX = 10.16      # (class P) what a channel may be wider than its tracks need when the sheet has the room: the
#                           columns stand apart until the drawing spans the sheet (was 0: a drawing 30 mm narrower than
#                           its sheet stood in the middle of it with all the air in the margins)
GROW_W, GROW_H = 1.0, 1.0    # (package D) a frame is not stretched beyond its drawing: a frame is as large as what
#                              it holds - a big frame around a small circuit reads as an empty sheet
EVEN = 0.9                # a frame at least this wide (tall) compared with its column (row) takes the column's width
#                           (the row's height): the edges line up, and the drawing stands in the middle of it
NARROW = 0.72             # a frame this much narrower than its column keeps its own width (aligned to the outer edge)
GUT_MAX = 2 * GUT
COLS_MAX = 5
RISE = 0.04               # how far above the sheet's middle a drawing may stand (fraction of the sheet height)
RANK_W = 0.4              # per rank a column spans beyond one (fix round 4: 0.2 stacked source - amplifier in one column)
COVER_W = 1.0             # weight of the sheet area the arrangement's outline leaves empty
SAME_COL_W = 0.3          # class L: cost of a signal edge between two (non-connector) frames of ONE column
KEEP_WIRED = True         # class L: a connector that is wired to its IC by two or more wires is not undocked
UNDOCK_NAMED = False      # class P (off, measured): a connector that names alone join to its IC gets a frame of its own
#                           wherever the IC stands. On: label_only_part_links motor-driver 4 -> 0, rs485-node 4 -> 0,
#                           rp2040-min 2 -> 0, but rp2040-min is a band of three columns again (sheet_fill 0.51 -> 0.45)
#                           and rs485-node falls from 0.65 to 0.55; motor-driver rises 0.57 -> 0.64
PACK = True               # class P: the frames of a column may also stand in rows that are filled from the left whatever
#                           their widths (the supply input beside its regulator OVER the wide MCU frame): the composer
#                           draws every arrangement both ways and keeps the cheaper one
ASPECT_W = 0.6            # class P: weight of |ln(outline aspect / sheet aspect)|: a band across a sheet loses to rows
LOST_WIRE_W = 0.1         # class P: per net (at most two per pair of frames) that two frames of one column could have wired
FLOW_POS_W = 3.0          # class P: a signal edge whose driver frame stands right of its sink frame (not above / under it)


def area(pp: str):
    """(x0, y0, x1, y1) frames may use, and the top-left corner of the reserved title-block rectangle."""
    pw, ph = dims(pp)
    x0 = y0 = BORDER
    x1 = math.floor((pw - BORDER + 0.001) / G) * G
    y1 = math.floor((ph - BORDER + 0.001) / G) * G
    tb = (math.floor((pw - 123.0) / G) * G, math.floor((ph - 46.5) / G) * G)
    return (x0, y0, x1, y1), tb


def notes_rect(pp: str, frames: list):
    """The largest free rectangle left of the title block under the frames (reserved for design notes, WP4), or
    None. frames = [[x0, y0, x1, y1]]."""
    (x0, _y0, x1, y1), tb = area(pp)
    best = None
    lefts = {f[0] for f in frames}
    xs = sorted({x0, x1, tb[0] - GUT} | lefts | {f[2] + GUT for f in frames})
    for a in xs:
        for b in xs:
            if b - a < 40.0 or b > tb[0] - GUT + 0.01:
                continue
            top = max([f[3] + GUT for f in frames if f[0] < b - 0.01 and a < f[2] - 0.01] or [BORDER])
            if y1 - top < 15.0:
                continue
            # the notes start on the left edge of a frame column (the leftmost one that has room), so the block
            # lines up with the drawing; only when no column has room under it: the largest free rectangle
            key = (0, a, -(b - a)) if a in lefts else (1, -(b - a) * (y1 - top), a)
            if best is None or key < best[0]:
                best = (key, [a, top, b, y1])
    return best[1] if best else None


def _fl(v: float) -> float:
    return math.floor(v / G + 1e-6) * G


def _shelve(c, W, cw, edge=None, last=False, pack=False):
    """Rows of one column: neighbouring frames that are much narrower than the column stand side by side (a small
    supply input beside a small regulator), each with its own width. A frame that holds a connector the design
    talks through keeps the outer end of its row (the sheet's edge): left in every column but the last, right
    there - so a row never holds two of them. -> [[block]]
    pack (class P): a row takes the next frame whenever it still fits the column's width - a frame need not be
    "narrow" for that."""
    rows = []
    for k in c:
        r = rows[-1] if rows else None
        ok = bool(r) and (pack or (W[k] < NARROW * cw and all(W[t] < NARROW * cw for t in r)))             and sum(W[t] for t in r) + GUT * len(r) + W[k] <= cw + 0.01
        if ok and edge:
            ok = not any(edge[t] for t in r) if last else not edge[k]
        if ok:
            r.append(k)
        else:
            rows.append([k])
    return rows


def _arrange(cols, W, H, nets_between, pp, vgut=GUT, edge=None, pack=False):
    """One arrangement on one paper -> dict or None when it does not fit.
    (package D) A frame is as large as its drawing: a column is as wide as its widest frame and is not stretched;
    a frame nearly as wide as its column (EVEN) takes the column's width so that the edges line up, every other
    frame keeps its own width on the column's left edge - in the last column a frame that holds a connector on
    the right edge (the connector stands at the sheet's edge)."""
    (ax0, ay0, ax1, ay1), tb = area(pp)
    aw = ax1 - ax0
    nc = len(cols)
    widths = [max(W[k] for k in c) for c in cols]
    need = sum(widths) + GUT * (nc - 1)
    if need > aw + 0.01:
        return None
    rows = [_shelve(c, W, widths[i], edge, nc > 1 and i == nc - 1, pack) for i, c in enumerate(cols)]
    RH = [[max(H[k] for k in r) for r in rs] for rs in rows]
    nat_h = [sum(rh) + vgut * (len(rh) - 1) for rh in RH]
    if any(h > ay1 - ay0 + 0.01 for h in nat_h):
        return None
    slack = aw - need
    chan = [GUT] * (nc - 1)
    if nc > 1:
        # every channel gets the tracks of the nets that cross it (as far as the sheet allows); what is left over
        # stays in the margins - a wider channel only makes the wires across it longer
        want = [min(TRACKS_MAX, n) * TRACK for n in nets_between]
        f = min(1.0, slack / sum(want)) if sum(want) > 0 else 0.0
        for i in range(nc - 1):
            add = _fl(want[i] * f)
            chan[i] += add
            slack -= add
        add = max(0.0, min(CH_EXTRA_MAX, _fl(slack / (nc - 1))))
        chan = [c + add for c in chan]
        slack -= add * (nc - 1)
    span = sum(widths) + sum(chan)
    x = ax0 + math.floor((aw - span) / 2 / G + 0.5) * G
    xs = []
    for i, w in enumerate(widths):
        xs.append(x)
        x += w + (chan[i] if i < nc - 1 else 0.0)
    lim = [tb[1] if xs[i] + widths[i] > tb[0] + 0.01 else ay1 for i in range(nc)]
    ph = dims(pp)[1]
    if any(nat_h[i] > lim[i] - ay0 + 0.01 for i in range(nc)):
        return None
    # columns stand on a common top line; a frame is as tall as its drawing. The gutters of a shorter column open
    # (up to GUT_MAX) until it ends level with the tallest one
    tall = max(nat_h)
    cells, colh, stretch = [], [], []
    for i in range(nc):
        full = lim[i] - ay0
        hs = list(RH[i])
        gut = vgut
        if len(hs) > 1:
            gut += max(0.0, min(GUT_MAX - vgut, _fl((min(tall, full) - nat_h[i]) / (len(hs) - 1))))
        cells.append((hs, gut))
        colh.append(sum(hs) + gut * (len(hs) - 1))
        stretch.append(1.0 - nat_h[i] / full)
    hall = max(colh)
    top = min(lim[i] - colh[i] for i in range(nc))
    y0 = min(max(ay0, _fl((ay0 + ay1 - hall) / 2 + G / 2)), _fl(top))
    # (fix round 4) the title block and the sheet notes fill the bottom of the sheet: a drawing that is centred to the
    # millimetre reads as hanging low under an empty band. It stands a little above the middle (at most RISE of the
    # sheet's height, so that it still counts as centred)
    y0 = max(ay0, min(y0, max(y0 - _fl((y0 - ay0) / 2), _fl(ph / 2 - RISE * ph - hall / 2 + G))))
    pos = {}
    for i in range(nc):
        hs, gut = cells[i]
        y = y0
        outer = nc > 1 and i == nc - 1                    # the last column ends at the sheet's right edge
        for r, h in zip(rows[i], hs):
            if len(r) == 1:
                k = r[0]
                wk, xk = widths[i], xs[i]
                if W[k] < EVEN * widths[i]:
                    wk = W[k]
                    # a frame with a connector in the last column: the connector stands at the sheet's edge
                    if outer and edge and edge[k]:
                        xk = xs[i] + widths[i] - wk
                pos[k] = (round(xk, 4), round(y, 4), round(wk, 4), round(h, 4))
            else:
                # a row of narrow frames: spread over the column's width when that takes little (both edges line
                # up), else side by side with the common gutter - from the right edge in the last column
                free = widths[i] - sum(W[k] for k in r) - GUT * (len(r) - 1)
                gap = GUT + (_fl(free / (len(r) - 1)) if free / (len(r) - 1) <= GUT_MAX - GUT + 0.01 else 0.0)
                xk = xs[i]
                if outer and edge and edge[r[-1]]:
                    xk = xs[i] + widths[i] - sum(W[k] for k in r) - gap * (len(r) - 1)
                for k in r:
                    hk = h if H[k] >= EVEN * h else H[k]
                    pos[k] = (round(xk, 4), round(y, 4), round(W[k], 4), round(hk, 4))
                    xk += W[k] + gap
            y += h + gut
    off = abs(y0 + hall / 2 - ph / 2) / ph
    return {"pos": pos, "xs": xs, "widths": widths, "chan": chan, "stretch": stretch, "need": need / aw, "off": off,
            "cover": span * hall / (aw * (ay1 - ay0)), "colh": colh, "ah": ay1 - ay0, "lim": lim, "y0": y0,
            "rows": rows, "RH": RH, "span": span, "hall": hall, "aspect": (span / max(hall, 1.0)) / (aw / (ay1 - ay0)),
            # (class P) a column that ends over the title block is not "short": the title block fills what is under it
            "colh_tb": [colh[i] + (ay1 - lim[i]) for i in range(nc)],
            "channels": [(round(xs[i] + widths[i], 4), round(xs[i + 1], 4)) for i in range(nc - 1)]}


def _row_cost(arr, cols, W, H, page, edge, rank, sig, k):
    """The terms of the composer's cost that are read off the rows and the places of the frames (class P: with
    packed rows a column is no longer a stack, so they are measured where the frames stand)."""
    cost = 0.0
    pos = arr["pos"]
    lo = min(p[0] for p in pos.values())
    hi = max(p[0] + p[2] for p in pos.values())
    for ci, c in enumerate(cols):
        cost += RANK_W * (max(rank[t] for t in c) - min(rank[t] for t in c))
        cw = arr["widths"][ci]
        # the holes beside the rows of a column (a row narrower than its column) and under a frame shorter than its row
        for r, rh in zip(arr["rows"][ci], arr["RH"][ci]):
            cost += 4.0 * max(0.0, cw - sum(W[t] for t in r) - GUT * (len(r) - 1)) * rh / page
            cost += 4.0 * sum(W[t] * (rh - H[t]) for t in r) / page
        for t in c:
            # a connector frame stands on the outline's left or right edge
            if edge[t] and (ci not in (0, k - 1) or (pos[t][0] > lo + 1.0 and pos[t][0] + pos[t][2] < hi - 1.0)):
                cost += 3.0
    # a signal reads left to right, or down a column: never from a frame to one on its left
    for a, b in sig:
        pa, pb = pos[a], pos[b]
        if pa[0] + pa[2] / 2 > pb[0] + pb[2] / 2 + 1.0 and \
                min(pa[0] + pa[2], pb[0] + pb[2]) - max(pa[0], pb[0]) <= 0.5 * min(pa[2], pb[2]):
            cost += FLOW_POS_W
    return cost


def _assignments(n, k, fixed, le, order_only):
    """Every way to put blocks 0..n-1 into k columns (none empty): fixed {block: column}, le [(a, b)] = block a not
    right of block b. order_only: the blocks keep their order (contiguous cuts) - for sheets with many blocks."""
    out = []
    col = [0] * n
    after = {}
    for a, b in le:
        after.setdefault(max(a, b), []).append((a, b))

    def rec(i, used):
        if i == n:
            if len(used) == k:
                out.append(tuple(col))
            return
        if k - len(used) > n - i:
            return
        lo = col[i - 1] if (order_only and i) else 0
        hi = min(k - 1, (col[i - 1] + 1) if (order_only and i) else k - 1)
        for c in ([fixed[i]] if i in fixed else range(lo, hi + 1)):
            if c < lo or c > hi:
                continue
            col[i] = c
            if all(col[a] <= col[b] for a, b in after.get(i, ())):
                rec(i + 1, used | {c})
    rec(0, frozenset())
    return out


def compose(blocks: list, sizes: dict, F, D, paper: str | None = None, tight: dict | None = None,
            last: dict | None = None, keep: list | None = None):
    """-> (paper, {id(block): (x, y, w, h)}, info) or (None, None, {}).
    info: columns [[block id]], col_of {block id: column}, channels [(x0, x1)], undock [connector refs that sit in a
    middle column with the part they serve].
    keep = [[block id]]: the columns (and the order in them) of an earlier drawing of the same blocks - the sheet is
    composed in exactly that arrangement (package D: the second drawing with measured frame sizes).
    Hard: supply inputs / signal sources in the first column, the connectors a design talks through in the last,
    no signal drawn right to left. Soft (cost): one rank per column, blocks of similar width share a column, the
    columns are equally tall, a block that holds a connector stands in an outer column."""
    by_id = {b.info.id: b for b in blocks}
    order = [i for i in F.order if i in by_id] + sorted(i for i in by_id if i not in F.order)
    n = len(order)
    if not n:
        return None, None, {}
    ix = {i: t for t, i in enumerate(order)}
    rank = [F.rank.get(i, 0) for i in order]
    pin = [F.pin.get(i) for i in order]
    # (fix round 4) only a block that IS a connector is tied to an outer column. A block that merely holds one (the
    # debug header drawn at its MCU) may stand in the middle: that connector is then undocked into a frame of its own
    # at the sheet's edge (info["undock"]) - tying the whole MCU to the edge left a third of the sheet empty
    is_conn = getattr(D, "is_conn", lambda r: True)
    docked_only = {root for c, root in getattr(D, "docked", {}).items() if not is_conn(root)}
    edge = [i in F.edge_blocks and i not in docked_only for i in order]
    has_conn = [2 if F.pin.get(i) == "last" else i in F.edge_blocks or any(D.is_conn(r) and D.npins(r) >= 2 for r in
                                          list(getattr(by_id[i].info, "anchors", ())) + list(getattr(by_id[i].info, "sats", ())))
                for i in order]
    supply = [getattr(by_id[i].info, "kind", None) in ("input", "power") for i in order]
    sig = [(ix[a], ix[b]) for (a, b), e in F.edges.items()
           if e["kind"] == "signal" and not e["feedback"] and a in ix and b in ix]
    pwr = [(ix[a], ix[b]) for (a, b), e in F.edges.items() if e["kind"] == "power" and a in ix and b in ix]
    wire_nets: dict = {}
    for pair, nets in F.shared.items():
        ks = [k for k in nets if len(D.view.nets[k].pins) <= 4]
        if ks and len(ks) < 8 and all(i in ix for i in pair):
            a, b = sorted(ix[i] for i in pair)
            wire_nets[(a, b)] = set(ks)

    def between(ca, cb):
        s = set()
        for a in ca:
            for b in cb:
                s |= wire_nets.get((min(a, b), max(a, b)), set())
        return len(s)
    cands = []
    for k in range(1, min(n, COLS_MAX) + 1):
        fixed = {}
        if k > 1:
            fixed = {t: (0 if pin[t] == "first" else k - 1) for t in range(n) if pin[t]}
        got = _assignments(n, k, fixed, sig, n > 8)
        if not got and sig:
            got = _assignments(n, k, fixed, [], n > 8)
        cands += [(k, a) for a in got]
    if keep and sorted(i for c in keep for i in c) == sorted(order) and all(keep):
        asg = [0] * n
        for ci, c in enumerate(keep):
            for i in c:
                asg[ix[i]] = ci
        cands = [(len(keep), tuple(asg))]
    else:
        keep = None
    best = None
    for pp in ([paper] if paper else PAPERS):
        (ax0, ay0, ax1, ay1), _tb = area(pp)
        page = (ax1 - ax0) * (ay1 - ay0)
        # `last` = sizes of last resort (the frames without room for their notes) before the next larger sheet
        for sz, vgut in ([(sizes, GUT)] + ([(tight, GUT), (tight, G)] if tight else []) + ([(last, G)] if last else [])):
            W = [sz[id(by_id[i])][0] for i in order]
            H = [sz[id(by_id[i])][1] for i in order]
            for k, asg in cands:
                cols = [[t for t in range(n) if asg[t] == c] for c in range(k)]
                if keep:
                    cols = [[ix[i] for i in c] for c in keep]
                if sum(max(W[t] for t in c) for c in cols) + GUT * (k - 1) > ax1 - ax0 + 0.01:
                    continue
                nb = [between(cols[i], cols[i + 1]) for i in range(k - 1)]
                seen_rows = None
                for pack in ((False, True) if PACK else (False,)):
                    arr = _arrange(cols, W, H, nb, pp, vgut, has_conn, pack)
                    if arr is None:
                        continue
                    if pack and arr["rows"] == seen_rows:
                        continue                          # nothing to pack: the same rows as the strict rule gives
                    seen_rows = arr["rows"]
                    cost = _row_cost(arr, cols, W, H, page, edge, rank, sig, k)
                    cost += 1.5 * sum(1 for a, b in pwr if asg[a] > asg[b])
                    # the supply chain (input - protection - regulator) reads down ONE column
                    cost += 0.6 * sum(abs(asg[a] - asg[b]) for a, b in pwr if asg[a] < asg[b] and supply[a] and supply[b])
                    cost += 0.6 * sum(1 for a in range(n) for b in range(n) if rank[a] < rank[b] and asg[a] > asg[b])
                    # (class L) a signal that goes from one IC to the next reads - and can be WIRED - from a column to the
                    # one on its right; stacked in one column the two frames are joined by a pair of names
                    cost += SAME_COL_W * sum(1 for a, b in sig if asg[a] == asg[b] and not pin[a] and not pin[b])
                    # (fix round 4) a column that holds nothing but the connector the design talks through (a speaker
                    # terminal, a GPIO header: pinned to the sheet's edge) is not an "empty column": it stands beside
                    # the block that drives it, and neither its free height nor its shortness counts against it
                    # (package D) only the connector a design talks THROUGH (pinned to the last column): a supply input
                    # alone in the first column with everything else stacked beside it is a sheet with two holes
                    solo = [k > 1 and len(c) == 1 and pin[c[0]] == "last" for c in cols]
                    cost += 2.0 * sum(st * (0.3 if solo[ci] else 1.0) for ci, st in enumerate(arr["stretch"])) / k                     + 0.5 * (1 - arr["need"])
                    hs_ = [h for ci, h in enumerate(arr["colh_tb"]) if not solo[ci]] or arr["colh_tb"]
                    arr["uneven"] = (max(hs_) - min(hs_)) / arr["ah"]
                    cost += 0.8 if (k == 1 and n > 1) else 0.0
                    # (fix round 4) the drawing as a whole covers the sheet: three frames in a thin band across a sheet
                    # that is two thirds empty lose to the same frames in two rows
                    # ... and its columns are about equally tall (no hole under a short column beside a long one)
                    cost += COVER_W * (1.0 - min(1.0, arr["cover"]) + arr["uneven"])
                    # (class P) ... and its outline has the proportions of the sheet
                    cost += ASPECT_W * abs(math.log(max(arr["aspect"], 1e-3)))
                    # ... but not at the price of a wire: a net that can be wired from a frame to the frame beside it
                    # (few pins, see wire_nets) is a pair of names when the two frames stand in ONE column
                    cost += LOST_WIRE_W * sum(min(2, len(ns)) for (a, b), ns in wire_nets.items() if asg[a] == asg[b])
                    cost += 30.0 * max(0.0, arr["off"] - 0.04)    # a tall column over the title block pushes all up
                    key = (round(cost, 6), k, asg, pack)
                    if best is None or key < best[0]:
                        best = (key, pp, cols, arr, sz, vgut, pack)
            if best is not None:
                break
        if best is not None:
            break
    if best is None:
        return None, None, {}
    _key, pp, cols, arr, _sz, vgut, pack = best
    W = [best[4][id(by_id[i])][0] for i in order]
    H = [best[4][id(by_id[i])][1] for i in order]
    # top to bottom inside a column: a block stands beside the blocks of the other columns it is wired to
    # (the reading order of the ranks is the tie-break)

    def pull(a_):
        yc = {t: q[1] + q[3] / 2 for t, q in a_["pos"].items()}
        xc = {t: q[0] for t, q in a_["pos"].items()}
        return sum(len(ns) * abs(yc[a] - yc[b]) for (a, b), ns in wire_nets.items() if xc[a] != xc[b])
    sig_set = set(sig)
    for _ in range(0 if keep else 2):
        for ci in range(len(cols)):
            if not 2 <= len(cols[ci]) <= 5:
                continue
            # the supply (input, regulator) stands above what it feeds: power enters a sheet at the top left, and
            # (package D) a debug header or another connector of that column goes under the supply chain
            def below(c_):
                # (class P) ... and a frame stands above the frames of its column it drives (a signal reads DOWN a column)
                return sum(1 for x in range(len(c_)) for y in range(x + 1, len(c_)) if supply[c_[y]] and not supply[c_[x]])                     + sum(1 for x in range(len(c_)) for y in range(x + 1, len(c_)) if (c_[y], c_[x]) in sig_set
                          and (c_[x], c_[y]) not in sig_set)
            pick = ((below(cols[ci]), pull(arr), 0), cols[ci], arr)
            for perm in itertools.permutations(cols[ci]):
                c2 = cols[:ci] + [list(perm)] + cols[ci + 1:]
                a2 = _arrange(c2, W, H, [between(c2[i], c2[i + 1]) for i in range(len(c2) - 1)], pp, vgut, has_conn, pack)
                if a2 is None or (pack and a2["hall"] > arr["hall"] + 0.01):
                    continue                              # (a packed column: another order may not cost a row)
                inv = sum(1 for x in range(len(perm)) for y in range(x + 1, len(perm)) if perm[x] > perm[y])
                key2 = (below(perm), round(pull(a2) + 15.0 * inv, 3), inv)
                if key2 < pick[0]:
                    pick = (key2, list(perm), a2)
            cols = cols[:ci] + [pick[1]] + cols[ci + 1:]
            arr = pick[2]
    # (fix round 4) an outer column that holds ONE connector frame which is wired to a frame of the neighbouring
    # column stands level with that frame (the speaker terminal beside the amplifier's output, not at the top of the
    # sheet). Every other column keeps the common top line.
    for ci, c in enumerate(cols):
        if len(c) != 1 or len(cols) < 2 or not pin[c[0]]:
            continue
        t = c[0]
        mates = [(len(ns), a if b == t else b) for (a, b), ns in wire_nets.items() if t in (a, b)]
        mates = [(w_, u) for w_, u in mates if u in arr["pos"] and abs(next(i for i, cc in enumerate(cols) if u in cc) - ci) == 1]
        if not mates:
            continue
        u = max(mates)[1]
        x_, y_, w_, h_ = arr["pos"][t]
        ux, uy, uw, uh = arr["pos"][u]
        (ax0_, ay0_, ax1_, ay1_), _tb = area(pp)
        yn = min(max(_fl(uy + (uh - h_) / 2), ay0_), _fl(arr["lim"][ci] - h_))
        if yn >= ay0_:
            arr["pos"][t] = (x_, round(yn, 4), w_, h_)
    col_of = {order[t]: ci for ci, c in enumerate(cols) for t in c}
    nc = len(cols)
    # (class L) ... unless it is WIRED to its IC there (a programming header joined to the MCU by a bundle of two
    # or more wires, `Blk.wired`): taken to the edge it would be a column of names again
    undock = sorted(c for c, root in getattr(D, "docked", {}).items()
                    if D.npins(c) >= 2 and (col_of.get(D.block_of.get(c)) not in (0, nc - 1) or UNDOCK_NAMED)
                    and not (KEEP_WIRED and len(getattr(by_id.get(D.block_of.get(c)), "wired", {}).get(c, ())) >= 2))
    pos = {id(by_id[order[t]]): p for t, p in arr["pos"].items()}
    (_ax0, ay0_, _ax1, _ay1), _tb = area(pp)
    info = {"columns": [[order[t] for t in c] for c in cols], "col_of": col_of, "channels": arr["channels"],
            "undock": undock, "cost": best[0][0], "sizes": best[4], "pack": pack,
            # {column: (top, bottom)} a frame of that column may stand between (the title block ends some columns)
            # - inside the outline of the drawing: a frame that slides does not make the drawing taller
            "room": {ci: (max(ay0_, arr["y0"]), min(arr["lim"][ci], arr["y0"] + max(arr["colh"]))) for ci in range(nc)}}
    return pp, pos, info
