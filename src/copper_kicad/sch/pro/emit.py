"""Turn the abstract drawing (unit wires, power symbols, labels, frames) into schematic items.

Labels are MOVED (the original node keeps its fields, font and uuid); wires, junctions, no-connect flags, power
symbols and PWR_FLAGs are re-created. Block frames + titles are members of `copper:block:<id>` groups so the next
run replaces them instead of stacking them; `copper:layout:<digest>` records the geometry the engine produced.
"""
from __future__ import annotations

import copy
import hashlib
import time
import uuid as _uuid

from ... import sexpr
from ...sexpr import Sym
from .. import ops as O
from ..model import Schematic, _set_justify, autoplace_fields, new_uuid, num
from .build import set_pose
from .route import cell, seg_cells

U = 1.27
NS = _uuid.UUID("6f1c5f0e-2c58-4c53-9c1e-7b1f1f0c0a57")
BLOCK_GROUP = "copper:block:"
LAYOUT_GROUP = "copper:layout:"
CONNECTIVITY = ("wire", "junction", "label", "global_label", "hierarchical_label", "no_connect")
USER_GRAPHICS = ("text", "text_box", "polyline", "rectangle", "circle", "arc", "bezier", "image", "table")


SUMMARY_MARK = "Blocks: "
SUMMARY_MORE = "\u00b7 "                        # second line of the summary (comment 1 when comment 2 starts it)


def _uuid_of(node) -> str:
    return str(sexpr.value(node, "uuid", ""))


def copper_groups(sch: Schematic) -> list:
    return [g for g in sch.items("group") if len(g) > 1 and
            (str(g[1]).startswith(BLOCK_GROUP) or str(g[1]).startswith(LAYOUT_GROUP))]


def group_members(g) -> list[str]:
    m = sexpr.find(g, "members")
    return [str(x) for x in m[1:]] if m is not None else []


def copper_member_uuids(sch: Schematic) -> set:
    out = set()
    for g in copper_groups(sch):
        if str(g[1]).startswith(BLOCK_GROUP):
            out |= set(group_members(g))
    return out


def user_graphics(sch: Schematic) -> list:
    """Graphic / text items that are not the engine's own frames."""
    own = copper_member_uuids(sch)
    return [n for k in USER_GRAPHICS for n in sch.items(k) if _uuid_of(n) not in own]


def digest(sch: Schematic) -> str:
    """Fingerprint of the geometry that carries meaning: symbol poses, wire ends, label positions."""
    rows = []
    for s in sch.symbols(include_power=False):
        rows.append(("s", s.ref, s.unit, round(s.at[0], 2), round(s.at[1], 2), s.rot, s.mirror or ""))
    for w in sch.wires():
        a, b = sorted([(round(w["a"][0], 2), round(w["a"][1], 2)), (round(w["b"][0], 2), round(w["b"][1], 2))])
        rows.append(("w", a, b))
    for l in sch.labels():
        rows.append(("l", l["net"], round(l["at"][0], 2), round(l["at"][1], 2)))
    rows.sort(key=repr)
    return hashlib.sha256(repr(rows).encode()).hexdigest()[:12]


def stored_digest(sch: Schematic) -> str | None:
    for g in sch.items("group"):
        if len(g) > 1 and str(g[1]).startswith(LAYOUT_GROUP):
            return str(g[1])[len(LAYOUT_GROUP):]
    return None


def is_engine_layout(sch: Schematic) -> bool:
    d = stored_digest(sch)
    return d is not None and d == digest(sch)


def strip(sch: Schematic) -> dict:
    """Remove everything the engine re-creates. Returns what it removed (counts)."""
    own = copper_member_uuids(sch)
    n = {"frames": 0}
    for k in ("rectangle", "text"):
        for node in list(sch.items(k)):
            if _uuid_of(node) in own:
                sch.remove(node)
                n["frames"] += 1
    for g in copper_groups(sch):
        sch.remove(g)
    for kind in CONNECTIVITY:
        for node in list(sch.items(kind)):
            sch.remove(node)
    for s in list(sch.symbols()):
        if s.is_power:
            sch.remove(s.node)
    return n


def move_label(node, pt, angle: int):
    kind = str(node[0])
    angle = int(angle) % 360
    a = sexpr.find(node, "at")
    a[1:] = [num(pt[0]), num(pt[1]), angle]
    if kind == "label":
        just = "left bottom" if angle in (0, 90) else "right bottom"
    else:
        just = "left" if angle in (0, 90) else "right"
    _set_justify(node, just)
    for p in sexpr.find_all(node, "property"):
        pa = sexpr.find(p, "at")
        if pa is not None and len(pa) > 2:
            pa[1], pa[2] = num(pt[0]), num(pt[1])
        if sexpr.find(p, "effects") is not None:
            _set_justify(p, just.split()[0])


class LabelPool:
    """Hands out the sheet's original label nodes per (net, text, kind, shape); copies one when more are needed."""

    def __init__(self):
        self.used: dict = {}

    def take(self, sch: Schematic, entry, pt, angle):
        text, kind, shape, nodes = entry
        i = self.used.get(id(nodes), 0)
        if i == 0:
            # KiCad re-orders items when it saves: hand the nodes out in uuid order, so re-laying an already
            # re-laid sheet gives every label the same place again (a second run is a byte-identical no-op)
            nodes.sort(key=lambda n: str(sexpr.value(n, "uuid", "")))
        self.used[id(nodes)] = i + 1
        if i < len(nodes):
            node = nodes[i]
        elif nodes:
            node = copy.deepcopy(nodes[0])
            for e in node:
                if isinstance(e, list) and e and e[0] == "uuid":
                    e[1] = new_uuid()
        else:
            node = None
        if node is None:
            return sch.add_label(text, pt, angle, kind, shape or "bidirectional")
        move_label(node, pt, angle)
        return sch._insert(node)


def emit_wires(sch: Schematic, wires, keep: dict, pin_tips: dict) -> dict:
    """Unit-edge union per net -> prune dangling ends -> maximal segments split at tees -> junctions."""
    edges: dict = {}
    for n, a, b in wires:
        seq = seg_cells(a, b)
        for u, v in zip(seq, seq[1:]):
            edges.setdefault(n, set()).add((u, v))
    pruned = nw = nj = 0
    for n in sorted(edges, key=str):
        es = edges[n]
        kp = keep.get(n, set())
        while True:
            deg: dict = {}
            for u, v in es:
                deg[u] = deg.get(u, 0) + 1
                deg[v] = deg.get(v, 0) + 1
            dead = {(u, v) for u, v in es if (deg[u] == 1 and u not in kp) or (deg[v] == 1 and v not in kp)}
            if not dead:
                break
            es -= dead
            pruned += len(dead)
        deg = {}
        hor: dict = {}
        ver: dict = {}
        for u, v in es:
            deg[u] = deg.get(u, 0) + 1
            deg[v] = deg.get(v, 0) + 1
            if u[1] == v[1]:
                hor.setdefault(u[1], []).append((u, v))
            else:
                ver.setdefault(u[0], []).append((u, v))
        perp_h = {c for row in ver.values() for e in row for c in e}
        perp_v = {c for row in hor.values() for e in row for c in e}
        for table, perp, axis in ((hor, perp_h, 0), (ver, perp_v, 1)):
            for key in sorted(table):
                row = sorted(table[key], key=lambda e: e[0][axis])
                start, last = row[0][0], row[0][1]
                for u, v in row[1:] + [(None, None)]:
                    if u is not None and u == last and u not in perp and u not in kp:
                        last = v
                        continue
                    sch.add_wire((start[0] * U, start[1] * U), (last[0] * U, last[1] * U))
                    nw += 1
                    if u is not None:
                        start, last = u, v
        for c in sorted(deg):
            if deg[c] + (1 if pin_tips.get(c, 0) == n else 0) >= 3:
                sch.add_junction((c[0] * U, c[1] * U))
                nj += 1
    return {"wires": nw, "junctions": nj, "pruned": pruned}


_WANT = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


def put_power(sch: Schematic, src, ref: str, value: str, q, orient: str):
    s = sch.add_symbol(src[1], src[0], ref, q, 0, value=value)
    for rot in (0, 90, 180, 270):
        set_pose(s, q[0], q[1], rot)
        bb = s.body_bbox()
        c = ((bb[0] + bb[2]) / 2 - q[0], (bb[1] + bb[3]) / 2 - q[1])
        m = max(abs(c[0]), abs(c[1]), 1e-6)
        if (round(c[0] / m), round(c[1] / m)) == _WANT[orient]:
            break
    autoplace_fields(s)
    return s


NOTE_SIZE = 1.27
NOTES_ID = "~notes"                              # block id of the group that holds the sheet's design notes
MACHINE_SLOT = 9
MACHINE_LINE = "Layout: Copper sch_tidy - comments 1-2 and the frame notes are generated"


def add_note_lines(sch: Schematic, lines: list, at, pitch: float = 2.2) -> list:
    """Stack of note lines (text items, left bottom anchored) -> their uuids."""
    out = []
    for i, ln in enumerate(lines):
        t = sch.add_text(str(ln), (round(at[0], 2), round(at[1] + i * pitch, 2)), size=NOTE_SIZE)
        out.append(_uuid_of(t))
    return out


def add_notes_group(sch: Schematic, stacks: list) -> str | None:
    """The sheet's design notes outside the block frames: stacks = [(lines, at)] as one engine-owned group."""
    members = []
    for lines, at in stacks:
        members += add_note_lines(sch, lines, at)
    if not members:
        return None
    gu = new_uuid()
    sch._insert([Sym("group"), BLOCK_GROUP + NOTES_ID, [Sym("uuid"), gu], [Sym("members"), *members]])
    return gu


def add_frame(sch: Schematic, block_id: str, title: str, box, note: str | None = None, stacks: list | None = None) -> str:
    """Dashed frame + title (+ note, + stacks of design-note lines [(lines, at)]) as one named group. Returns the
    group's uuid."""
    ru = new_uuid()
    sch._insert([Sym("rectangle"), [Sym("start"), num(box[0]), num(box[1])], [Sym("end"), num(box[2]), num(box[3])],
                 [Sym("stroke"), [Sym("width"), 0], [Sym("type"), Sym("dash")]],
                 [Sym("fill"), [Sym("type"), Sym("none")]], [Sym("uuid"), ru]])
    members = [ru]
    t = sch.add_text(title, (box[0] + 1.5, box[1] + 3.6), size=2.0)
    members.append(_uuid_of(t))
    if note:
        t2 = sch.add_text(str(note)[:60], (box[0] + 1.5, box[1] + 6.2), size=1.27)
        members.append(_uuid_of(t2))
    for lines, at in stacks or []:
        members += add_note_lines(sch, lines, at)
    gu = new_uuid()
    sch._insert([Sym("group"), BLOCK_GROUP + block_id, [Sym("uuid"), gu], [Sym("members"), *members]])
    return gu


def add_layout_group(sch: Schematic, rect_uuids: list):
    if rect_uuids:
        sch._insert([Sym("group"), LAYOUT_GROUP + digest(sch), [Sym("uuid"), new_uuid()],
                     [Sym("members"), *rect_uuids]])


_ACRONYMS = {"afe", "usb", "mcu", "adc", "dac", "led", "pcb", "io", "gpio", "spi", "i2c", "uart", "can", "rf", "bms",
             "psu", "ldo", "dc", "ac", "pwm", "imu", "gps", "lcd", "oled", "ble", "nfc", "rtc", "poe", "hv", "lv"}


def pretty_title(name: str) -> str:
    """'li-ion-powerbank' -> 'Li-Ion Powerbank', 'rp2040_min' -> 'RP2040 Min': a file name is not a title."""
    name = str(name).strip()
    if not name or " " in name or any(c.isupper() for c in name[1:]):
        return name                                       # somebody already wrote it the way they want it
    words = [w for w in name.replace("_", "-").split("-") if w]
    out = []
    for w in words:
        if any(c.isdigit() for c in w) or w.lower() in _ACRONYMS:
            out.append(w.upper())
        else:
            out.append(w[:1].upper() + w[1:])
    if len(out) > 1 and len(words[0]) <= 2 and words[0].isalpha() and len(words[1]) <= 3 and words[1].isalpha() \
            and words[0].lower() not in _ACRONYMS:
        out[0:2] = [out[0] + "-" + out[1]]                # Li-Ion, Ni-MH
    return " ".join(out)


def fill_title_block(sch: Schematic, project_name: str, extra: dict | None = None, summary: str | None = None):
    """Title + date + revision where empty, and a one-line description of what is on the sheet as comment 1
    (only when the comment is empty or was written by an earlier re-layout); company / other comments only from
    the caller."""
    tb = sexpr.find(sch.tree, "title_block")
    if tb is None:
        tb = [Sym("title_block")]
        idx = next((i for i, e in enumerate(sch.tree) if isinstance(e, list) and e and e[0] == "paper"), 4)
        sch.tree.insert(idx + 1, tb)

    def setv(key, val, only_empty):
        cur = sexpr.find(tb, key)
        if cur is None:
            order = ["title", "date", "rev", "company"]
            pos = len(tb)
            for i, e in enumerate(tb):
                if isinstance(e, list) and e and (e[0] == "comment" or (str(e[0]) in order and key in order and
                                                                         order.index(str(e[0])) > order.index(key))):
                    pos = i
                    break
            tb.insert(pos, [Sym(key), str(val)])
        elif not only_empty or not str(cur[1] if len(cur) > 1 else ""):
            cur[1:] = [str(val)]
    cur_t = sexpr.find(tb, "title")
    setv("title", pretty_title(project_name), not (cur_t is not None and len(cur_t) > 1 and str(cur_t[1]) == project_name))
    setv("date", time.strftime("%Y-%m-%d"), True)
    setv("rev", "1", True)
    if summary:
        lines = [summary] if isinstance(summary, str) else [x for x in summary if x]
        # comment 2 sits above comment 1 on the sheet: a second line continues below the first
        want = {1: SUMMARY_MARK + lines[0]} if len(lines) == 1 else \
            {2: SUMMARY_MARK + lines[0], 1: SUMMARY_MORE + lines[1]}
        for n in (2, 1):
            cur = next((e for e in sexpr.find_all(tb, "comment") if len(e) > 2 and int(e[1]) == n), None)
            mine = cur is not None and (not str(cur[2]) or str(cur[2]).startswith((SUMMARY_MARK, SUMMARY_MORE)))
            if n in want:
                if cur is None:
                    tb.append([Sym("comment"), n, want[n]])
                elif mine:
                    cur[2] = want[n]
            elif cur is not None and mine and str(cur[2]):
                cur[2] = ""
    for k, v in (extra or {}).items():
        if k in ("title", "date", "rev", "company") and v is not None:
            setv(k, v, False)
        elif k.startswith("comment") and v is not None:
            n = int(k[7:] or 1)
            cur = next((e for e in sexpr.find_all(tb, "comment") if len(e) > 2 and int(e[1]) == n), None)
            if cur is None:
                tb.append([Sym("comment"), n, str(v)])
            else:
                cur[2] = str(v)


def fill_title_block_v2(sch: Schematic, project_name: str, extra: dict | None, function: str, rails: str):
    """pro2: title / date / rev as fill_title_block; comment 1 = what the sheet is, comment 2 = its supply rails,
    comment 4 = the line that says these were generated. Comments somebody else wrote stay: comment 1 / 2 are
    replaced only when they are empty, carry an earlier re-layout's summary, or comment 4 holds the engine's line."""
    fill_title_block(sch, project_name, None, None)
    tb = sexpr.find(sch.tree, "title_block")

    def get(n):
        return next((e for e in sexpr.find_all(tb, "comment") if len(e) > 2 and int(e[1]) == n), None)
    # the engine's own line lives in comment 9, which the standard drawing sheet does not print (a title block
    # carries what the sheet is, not which tool arranged it); a sheet of an earlier version has it in comment 4
    old4 = get(4)
    if old4 is not None and str(old4[2]) == MACHINE_LINE:
        tb.remove(old4)
        if get(MACHINE_SLOT) is None:
            tb.append([Sym("comment"), MACHINE_SLOT, MACHINE_LINE])
    c4 = get(MACHINE_SLOT)
    ours = c4 is not None and str(c4[2]) == MACHINE_LINE
    wrote = False
    for n, text in ((1, function), (2, rails)):
        cur = get(n)
        mine = cur is None or not str(cur[2]) or ours or str(cur[2]).startswith((SUMMARY_MARK, SUMMARY_MORE))
        if not mine:
            continue
        if cur is None:
            if text:
                tb.append([Sym("comment"), n, text])
                wrote = True
        else:
            cur[2] = text
            wrote = wrote or bool(text)
    if wrote and (c4 is None or not str(c4[2])):
        if c4 is None:
            tb.append([Sym("comment"), MACHINE_SLOT, MACHINE_LINE])
        else:
            c4[2] = MACHINE_LINE
    cm = sorted((e for e in tb if isinstance(e, list) and e and e[0] == "comment"), key=lambda e: int(e[1]))
    tb[:] = [e for e in tb if not (isinstance(e, list) and e and e[0] == "comment")] + cm
    for k, v in (extra or {}).items():
        if k in ("title", "date", "rev", "company") and v is not None:
            cur = sexpr.find(tb, k)
            if cur is None:
                tb.insert(1, [Sym(k), str(v)])
            else:
                cur[1:] = [str(v)]
        elif k.startswith("comment") and v is not None:
            n = int(k[7:] or 1)
            cur = get(n)
            if cur is None:
                tb.append([Sym("comment"), n, str(v)])
            else:
                cur[2] = str(v)


def deterministic_uuids(sch: Schematic, old_ids: set):
    """Give every node created by this run a uuid derived from the sheet uuid and its creation order, so the same
    input always produces the same bytes. Group member lists follow."""
    taken = set()
    for n in sch.tree:
        if isinstance(n, list) and id(n) in old_ids:
            for u in _all_uuids(n):
                taken.add(str(u[1]))
    remap = {}
    k = 0
    for n in sch.tree:
        if not isinstance(n, list) or id(n) in old_ids or not n or n[0] in ("lib_symbols", "title_block", "group"):
            continue
        for u in _all_uuids(n):
            while True:
                k += 1
                new = str(_uuid.uuid5(NS, f"{sch.uuid}/{k}"))
                if new not in taken:
                    break
            remap[str(u[1])] = new
            taken.add(new)
            u[1] = new
    for g in sch.items("group"):                      # block groups first: the layout group nests them
        if id(g) in old_ids:
            continue
        gu = sexpr.find(g, "uuid")
        old_u = str(gu[1])
        while True:
            k += 1
            new = str(_uuid.uuid5(NS, f"{sch.uuid}/{k}"))
            if new not in taken:
                break
        taken.add(new)
        gu[1] = new
        remap[old_u] = new
        m = sexpr.find(g, "members")
        if m is not None:
            m[1:] = [remap.get(str(x), str(x)) for x in m[1:]]


def _all_uuids(node) -> list:
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, list):
            if n and n[0] == "uuid" and len(n) > 1:
                out.append(n)
            else:
                stack.extend(reversed([e for e in n if isinstance(e, list)]))
    return out


def other_sheet_refs(pr, f) -> set:
    refs = set()
    for sf in O.sheet_files(pr):
        if not O._same(sf, f):
            info = O._scan_file(sf)
            if info:
                refs |= info["refs"]
    return refs
