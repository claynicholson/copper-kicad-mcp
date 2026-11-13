"""Deterministic design notes and the title-block summary for "pro2" (docs/internal/SCHEMATIC_PRO_V2_DESIGN.md section 9).

Everything here is derived from the netlist and the part values alone - no model call, no guessing: a note whose
inputs cannot be parsed is left out. Notes are plain text items; they are members of the block groups the engine
owns, so the next re-layout replaces them, and they are not part of the netlist (the gate compares fields and
connectivity, never free text).
"""
from __future__ import annotations

import math
import re

from .. import layout as LAY
from ..model import text_width
from .blocks import Design, _refkey, _reg_kind, prefix

NOTE_SIZE = 1.27
NOTE_PITCH = 2.2          # line to line
DECAP_NOTES = False        # the per-part "Place C.. close to U.." lines
MAX_PER_BLOCK = 5
# fix round 5: the rails and their sources are said ONCE, in the title block ("Rails: +3V3 (U1 from VBUS), VBUS (J1)");
# the free-standing NOTES block that repeated them beside the title block is not written
RAILS_IN_TITLE_BLOCK = True
# ... and a connector legend that cannot list its signals ("J2 = 1x10 pin header (10 pins)") only repeats the value
BARE_LEGEND_OFF = True
MAX_CHARS = 64
GENERIC_PINOUT = True      # fix round 8: a generic 1xN / 2xN connector of three or more pins gets its pinout note too

_MULT = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0, "r": 1.0, "k": 1e3,
         "meg": 1e6, "g": 1e9}

# feedback reference voltages by part family (data sheets); a regulator that is not listed gets the formula only
VREF = (("MT3608", 0.6), ("TPS5430", 1.221), ("TPS54331", 0.8), ("MP1584", 0.8), ("MP2307", 0.925), ("XL6009", 1.25),
        ("LM2596", 1.23), ("LM317", 1.25), ("AMS1117-ADJ", 1.25), ("TPS563200", 0.768), ("SY8089", 0.6),
        ("TLV62569", 0.6), ("AP3012", 1.25), ("SX1308", 0.6), ("MP2359", 0.81), ("AP63203", 0.8), ("TPS61023", 0.595))
# typical forward voltages at a few mA, by the colour written in the LED's value
VF = (("red", 2.0), ("yellow", 2.1), ("orange", 2.1), ("amber", 2.1), ("green", 2.2), ("blue", 3.0), ("white", 3.0),
      ("ir", 1.3))


def parse_value(text: str, kind: str):
    """'4.7k' / '4k7' / '100nF' / '22uH' / '12MHz' -> float in ohm / farad / henry / hertz, or None."""
    t = str(text).strip().split()[0] if str(text).strip() else ""
    t = t.replace(",", ".")
    unit = {"R": r"(?:ohms?|Ω|r)?", "C": r"f?", "L": r"h?", "F": r"hz"}[kind]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(meg|[pnuµμmkMGR]?)" + unit, t, re.I)
    if m is None:
        m2 = re.fullmatch(r"(\d+)([pnuµμmkMRr])(\d+)", t)            # 4k7, 2R2, 4n7
        if m2 is None:
            return None
        num, mult = float(m2.group(1) + "." + m2.group(3)), m2.group(2)
    else:
        num, mult = float(m.group(1)), m.group(2)
    if mult == "M":
        f = 1e6 if kind in ("R", "F") else 1e-3
    elif mult == "m":
        f = 1e-3
    else:
        f = _MULT.get(mult.lower())
    if f is None:
        return None
    if kind == "R" and f < 1:
        return None
    if kind in ("C", "L") and f >= 1 and not (kind == "C" and mult == ""):
        return None
    if kind == "C" and mult == "":
        return None                                       # a bare number is not a capacitance
    return num * f


def eng(v: float, unit: str) -> str:
    """1234.5 -> '1.23 k<unit>'."""
    if v == 0:
        return f"0 {unit}"
    for f, p in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
        if abs(v) >= f:
            x = v / f
            s = f"{x:.3g}"
            return f"{s} {p}{unit}"
    return f"{v:.3g} {unit}"


def rail_volts(name: str):
    """Nominal voltage a rail's NAME states ('+3V3', '5V', 'VCC_1V8', 'VBUS' = USB 5 V), or None."""
    n = str(name).upper().lstrip("+")
    m = re.search(r"(\d+)V(\d+)", n)
    if m:
        return float(m.group(1) + "." + m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)V\b", n)
    if m:
        return float(m.group(1))
    if n in ("VBUS", "USB_VBUS", "VUSB"):
        return 5.0
    return None


def _two(D: Design, r: str):
    ps = D.pins_of.get(r, {})
    if len(ps) != 2:
        return None
    a, b = [ps[k] for k in sorted(ps)]
    return (a, b) if a != b and a is not None and b is not None else None


def _other(D, r, k):
    t = _two(D, r)
    if t is None or k not in t:
        return None
    return t[1] if t[0] == k else t[0]


def _on(D, k, pfx):
    ln = D.view.nets.get(k)
    return sorted({r for r, _p in ln.pins if prefix(r) in pfx and _two(D, r)}, key=_refkey) if ln else []


def _val(D, r):
    return " ".join(str(D.comps[r]["value"]).split())


def _is_led(D, r):
    c = D.comps[r]
    return prefix(r) in ("D", "LED") and ("LED" in str(c["lib_id"]).upper() or str(c["value"]).upper().startswith("LED"))


def connector_name(D: Design, c: str, generic: bool = False) -> str:
    """What a person would call the connector ('USB Type-C receptacle', '1x10 pin header') - '' when nothing in
    its value / library name says. generic: a stock generic symbol (Conn_01x06) is called '1x6 header'."""
    blob = (str(D.comps[c]["value"]) + " " + str(D.comps[c]["lib_id"])).upper()
    n = D.npins(c)
    if "TYPE-C" in blob or "TYPE_C" in blob or "USB_C" in blob or "USB-C" in blob:
        return "USB Type-C receptacle"
    if "MICRO" in blob and "USB" in blob:
        return "USB Micro-B receptacle"
    if "USB" in blob:
        return "USB connector"
    m = re.search(r"(?<!\d)([12])\s*[X*]\s*(\d{1,2})(?!\d)", blob)
    if m:
        kind = "socket" if re.search(r"SOCKET|FEMALE", blob) else "pin header"
        return f"{m.group(1)}x{int(m.group(2))} {kind}"
    if re.search(r"\bB\d+B-PH|JST.?PH|\bPH\b", blob):
        return f"JST PH {n}-pin"
    if re.search(r"\bXH\b|B\d+B-XH", blob):
        return f"JST XH {n}-pin"
    if re.search(r"SCREW|TERMINAL|KF\d|\bDG\d", blob):
        return f"{n}-pin terminal block"
    if "JACK" in blob or "BARREL" in blob:
        return "DC jack"
    # the stock generic symbols (Conn_01x06, Conn_02x05_Odd_Even): rows x pins with a leading zero. The pinout of a
    # programming / SPI header is what its note is for - without it the pin order is read off the wires
    m = re.search(r"CONN_0?([12])X(\d{1,2})(?!\d)", blob) if (GENERIC_PINOUT and generic) else None
    if m and int(m.group(2)) >= 3:
        return f"{int(m.group(1))}x{int(m.group(2))} " + ("socket" if re.search(r"SOCKET|FEMALE", blob) else "header")
    return ""


def build(D: Design) -> dict:
    """{block id | None: [note line]} - None holds the notes about the whole sheet (rails and their sources).
    Deterministic; every line is at most MAX_CHARS long."""
    v = D.view
    out: dict = {}

    def add(block, line):
        line = " ".join(str(line).split())
        if len(line) > MAX_CHARS:
            line = line[:MAX_CHARS - 1].rstrip() + "…"
        lst = out.setdefault(block, [])
        if line not in lst:
            lst.append(line)

    def blk(r):
        return D.block_of.get(r)
    gnd = {k for k in D.rails if D.is_gnd(k)}
    # ── rails and where they come from ──
    for k in sorted((k for k in D.rails if k not in gnd), key=lambda k: D.text(k)):
        if RAILS_IN_TITLE_BLOCK:
            break                                         # said once, in the title block (summary())
        name = D.text(k)
        regs = sorted((a for a, o in D.reg_out.items() if k in o), key=_refkey)
        feeds = sorted((c for c, f in D.feed_of.items() if f == k), key=_refkey)
        if regs:
            a = regs[0]
            ins = sorted(D.text(i) for i in D.reg_in.get(a, ()) if D.text(i) and i != k)
            add(None, f"{name}: from {a} ({_val(D, a)})" + (f", input {ins[0]}" if ins else ""))
        elif feeds:
            c = feeds[0]
            nm = connector_name(D, c)
            add(None, f"{name}: from {c}" + (f" ({nm})" if nm else ""))
    # ── regulators with a feedback divider ──
    for a in sorted(D.reg_out, key=_refkey):
        fb = [k for p, k in sorted(D.pins_of[a].items()) if re.fullmatch(r"V?FB|ADJ|VSENSE|VSEN", D.pin_name.get((a, p), ""), re.I)]
        if not fb or fb[0] in D.rails:
            continue
        rs = _on(D, fb[0], ("R",))
        top = [r for r in rs if _other(D, r, fb[0]) in D.reg_out[a] or (_other(D, r, fb[0]) in D.rails and _other(D, r, fb[0]) not in gnd)]
        bot = [r for r in rs if _other(D, r, fb[0]) in gnd]
        if len(top) != 1 or len(bot) != 1:
            continue
        rt, rb = parse_value(_val(D, top[0]), "R"), parse_value(_val(D, bot[0]), "R")
        outn = D.text(_other(D, top[0], fb[0]))
        line = f"{a}: Vout = Vref x (1 + {top[0]}/{bot[0]})"
        vref = next((vr for fam, vr in VREF if fam in (_val(D, a) + " " + str(D.comps[a]["lib_id"])).upper()), None)
        if vref is not None and rt and rb:
            line += f" = {vref:g} V x {1 + rt / rb:.3g} = {vref * (1 + rt / rb):.2f} V"
        add(blk(a), line + (f" ({outn})" if outn and vref is None else ""))
    # ── LEDs: current at the rail's nominal voltage ──
    for d in sorted((r for r in D.units if _is_led(D, r) and _two(D, r)), key=_refkey):
        na, nb = _two(D, d)
        for near in (na, nb):
            far = nb if near == na else na
            rs = [r for r in _on(D, near, ("R",)) if len(v.nets[near].pins) == 2]
            if len(rs) != 1:
                continue
            end = _other(D, rs[0], near)
            rails_ = [x for x in (far, end) if x in D.rails and x not in gnd]
            if len(rails_) != 1:
                continue
            vr = rail_volts(D.text(rails_[0]))
            rv = parse_value(_val(D, rs[0]), "R")
            if vr is None or not rv:
                continue
            col = next(((c, vf) for c, vf in VF if re.search(rf"\b{c}\b", _val(D, d).lower())), None)
            vf = col[1] if col else 2.0
            i = (vr - vf) / rv
            if i <= 0:
                add(blk(d), f"{d}: {D.text(rails_[0])} is below the assumed Vf {vf:g} V - check {rs[0]}")
            else:
                add(blk(d), f"{d}: I = ({vr:g} V - Vf {vf:g} V) / {_val(D, rs[0])} = {eng(i, 'A')}"
                    + ("" if col else " (Vf assumed)"))
            break
    # ── RC low-pass: series R, shunt C to ground, into a pin ──
    seen_rc = set()
    for r in sorted((r for r in D.units if prefix(r) == "R" and _two(D, r)), key=_refkey):
        for a_, b_ in (_two(D, r), _two(D, r)[::-1]):
            if a_ in D.rails or b_ in D.rails or b_ in seen_rc:
                continue
            pins_b = v.nets[b_].pins
            cs = [c for c in _on(D, b_, ("C",)) if _other(D, c, b_) in gnd]
            others = [p for p in pins_b if p[0] not in (r, *cs)]
            if len(cs) != 1 or not others or any(prefix(p[0]) in ("R", "C", "L", "D", "Y", "X", "SW", "LED") for p in others):
                continue
            if _on(D, a_, ("C",)) and any(_other(D, c, a_) in gnd for c in _on(D, a_, ("C",))) and len(v.nets[a_].pins) <= 3:
                continue                                  # symmetric pi: the direction is not clear
            rv, cv = parse_value(_val(D, r), "R"), parse_value(_val(D, cs[0]), "C")
            if not rv or not cv:
                continue
            if {p[0] for p in others} & {p[0] for p in v.nets[a_].pins if prefix(p[0]) not in ("R", "C", "L", "D")}:
                continue                                  # both ends on one IC: a timing / feedback network, no filter
            seen_rc.add(b_)
            add(blk(r), f"{r}/{cs[0]} low-pass: fc = 1/(2 pi R C) = {eng(1 / (2 * math.pi * rv * cv), 'Hz')}")
            break
    # ── 555-style astable: RA from the supply to DISCH, RB from DISCH to THRES = TRIG, C from there to ground ──
    for a in sorted(D.units, key=_refkey):
        nm_ = {str(D.pin_name.get((a, p), "")).upper(): k for p, k in D.pins_of[a].items()}
        dis = next((k for n_, k in nm_.items() if re.fullmatch(r"DIS(CH(ARGE)?)?", n_)), None)
        thr = next((k for n_, k in nm_.items() if re.fullmatch(r"THR(ES(HOLD)?)?", n_)), None)
        trg = next((k for n_, k in nm_.items() if re.fullmatch(r"TR(IG(GER)?)?", n_)), None)
        if dis is None or thr is None or thr != trg or dis == thr or dis in D.rails or thr in D.rails:
            continue
        ra = [r for r in _on(D, dis, ("R",)) if _other(D, r, dis) in D.rails and _other(D, r, dis) not in gnd]
        rb = [r for r in _on(D, dis, ("R",)) if _other(D, r, dis) == thr]
        ct = [c for c in _on(D, thr, ("C",)) if _other(D, c, thr) in gnd]
        if len(ra) != 1 or len(rb) != 1 or len(ct) != 1:
            continue
        va, vb, vc = parse_value(_val(D, ra[0]), "R"), parse_value(_val(D, rb[0]), "R"), parse_value(_val(D, ct[0]), "C")
        line = f"{a} astable: f = 1.44 / (({ra[0]} + 2 {rb[0]}) {ct[0]})"
        if va and vb and vc:
            line += f" = {eng(1.44 / ((va + 2 * vb) * vc), 'Hz')}"
        add(blk(a), line)
    # ── crystals ──
    for y in sorted((r for r in D.units if prefix(r) in ("Y", "X") and not D.is_conn(r)), key=_refkey):
        nets = sorted({k for k in D.pins_of[y].values() if k is not None and k not in D.rails})
        caps = sorted({c for k in nets for c in _on(D, k, ("C",)) if _other(D, c, k) in gnd}, key=_refkey)
        f = parse_value(_val(D, y), "F")
        if f is None or len(nets) != 2:
            continue
        line = f"{y}: {_val(D, y)}"
        vals = {_val(D, c) for c in caps}
        if len(caps) == 2 and len(vals) == 1:
            cv = parse_value(vals.pop(), "C")
            line += f", load caps {caps[0]}/{caps[1]} {_val(D, caps[0])}"
            if cv:
                line += f" (CL = {eng(cv / 2, 'F')} + stray)"
        add(blk(y), line)
    # ── I2C pull-ups ──
    pu: dict = {}
    for k, ln in v.nets.items():
        nm = D.text(k).upper()
        if k in D.rails or not re.search(r"SDA|SCL", nm):
            continue
        for r in _on(D, k, ("R",)):
            o = _other(D, r, k)
            if o in D.rails and o not in gnd:
                pu.setdefault((D.text(o), _val(D, r)), []).append(r)
    for (rail, val), rs in sorted(pu.items()):
        rs = sorted(set(rs), key=_refkey)
        add(blk(rs[0]), f"I2C pull-ups {', '.join(rs)} = {val} to {rail}")
    # ── connectors: the friendly name (the Value field keeps the order code) ──
    for c in sorted((r for r in D.units if D.is_conn(r) and D.npins(r) >= 2), key=_refkey):
        # (fix round 8) a generic header drawn at its IC (a programming / SPI header, wired pin to pin): its pinout is
        # said in words. One that has a frame of its own carries the names right at its pins - no note
        nm = connector_name(D, c, generic=blk(c) not in (None, c))
        if not nm or nm.upper() == _val(D, c).upper():
            continue
        sig = []
        for p in sorted(D.pins_of[c], key=_refkey):
            t = D.text(D.pins_of[c][p])
            if t and t not in sig:
                sig.append(t)
        # the signals are listed when ALL of them fit on the line - a list cut off half way says nothing
        tail = ": " + ", ".join(sig) if sig else ""
        if len(f"{c} = {nm}{tail}") <= MAX_CHARS and (sig or not BARE_LEGEND_OFF):
            add(blk(c), f"{c} = {nm}{tail}")
        elif not BARE_LEGEND_OFF:
            add(blk(c), f"{c} = {nm} ({D.npins(c)} pins)")
    # ── decoupling ──
    dec: dict = {}
    for s, role in D.role.items():
        if role.get("kind") == "decap" and role.get("anchor") in D.units and prefix(s) == "C":
            dec.setdefault((role["anchor"], D.text(role.get("via")) or ""), []).append(s)
    for (a, rail), cs in sorted(dec.items(), key=lambda t: (_refkey(t[0][0]), t[0][1])):
        if not DECAP_NOTES:
            break                 # fix round 3: "Place Cx close to Uy" under every frame read as boilerplate clutter
        cs = sorted(cs, key=_refkey)
        line = f"Place {', '.join(cs)} close to {a}" + (f" ({rail})" if rail else "")
        if len(line) > MAX_CHARS:
            line = f"Place the {len(cs)} {rail + ' ' if rail else ''}decoupling caps close to {a}"
        add(blk(a), line)
    for k in list(out):
        if k is not None and len(out[k]) > MAX_PER_BLOCK:
            out[k] = out[k][:MAX_PER_BLOCK]
    return out


def summary(D: Design, blocks: list, titles: dict | None = None) -> dict:
    """Title-block lines: {'function': one line saying what the sheet is, 'rails': the supplies and their sources}.
    blocks = [BlockInfo] in reading order."""
    words, shorts = [], []
    # what the sheet IS comes first (its ICs), then how it is supplied, then what it is plugged into
    blocks = sorted(blocks, key=lambda b: {"main": 0, "ic": 1, "power": 2}.get(b.kind, 3))
    for b in blocks:
        t = str((titles or {}).get(b.id, b.title))
        head, _, detail = t.partition("  ")
        head, detail = " ".join(head.split()), " ".join(detail.split())
        # function words in lower case, abbreviations and part numbers as they are
        w = " ".join(x.lower() if (len(x) > 4 and x.isalpha() and x.isupper()) else x for x in f"{head} {detail}".split())
        if w and w not in words:
            words.append(w)
            shorts.append(" ".join(x.lower() if (len(x) > 4 and x.isalpha() and x.isupper()) else x for x in head.split()) or w)
            if shorts[-1] in ("connector", "sensor", "driver"):
                shorts[-1] = w                            # the bare word says nothing without its detail
    # the whole sheet in one line: when the part numbers make it too long they go (they are in the frame titles),
    # block by block from the end - never a list that stops half way
    heads = shorts
    full = list(words)
    for i in range(len(full) - 1, -1, -1):
        if len(", ".join(full)) <= 66:
            break
        full[i] = heads[i]
    uniq = []
    for w in full:
        if w not in uniq:
            uniq.append(w)
    line = ", ".join(uniq)
    if len(line) > 78:
        line = line[:77].rsplit(", ", 1)[0]
    line = line[:1].upper() + line[1:]
    gnd = {k for k in D.rails if D.is_gnd(k)}
    rl = []
    for k in sorted((k for k in D.rails if k not in gnd), key=lambda k: D.text(k)):
        src = sorted((a for a, o in D.reg_out.items() if k in o), key=_refkey) or \
            sorted((c for c, f in D.feed_of.items() if f == k), key=_refkey)
        ins = []
        if RAILS_IN_TITLE_BLOCK and src and src[0] in D.reg_out:
            ins = sorted(D.text(i) for i in D.reg_in.get(src[0], ()) if D.text(i) and i != k)
        rl.append(D.text(k) + (f" ({src[0]}" + (f" from {ins[0]}" if ins else "") + ")" if src else ""))
    rails = ""
    for i, w in enumerate(rl):
        nxt = (", " if rails else "") + w
        if len(rails) + len(nxt) > 58:
            rails += f" + {len(rl) - i} more"
            break
        rails += nxt
    return {"function": line, "rails": ("Rails: " + rails) if rails else ""}


def box_of(lines: list) -> tuple:
    """(width, height) in mm of a stack of note lines."""
    if not lines:
        return 0.0, 0.0
    # sentences run up to 3.6 % wider in KiCad than model.text_width says (bench/sch/textcheck.py): 5 % on top
    return max(text_width(s, NOTE_SIZE) for s in lines) * 1.05 + 1.0, NOTE_PITCH * len(lines)


def place(lines: list, frame: list, obstacles: list, title_h: float = 5.08, pad: float = 1.9):
    """Where a stack of note lines fits inside `frame` without touching anything drawn: the free corners, bottom
    first. -> (x, y of the first line's baseline) or None."""
    w, h = box_of(lines)
    if not lines or w > frame[2] - frame[0] - 2 * pad or h > frame[3] - frame[1] - title_h - 2 * pad:
        return None
    xs = (frame[0] + pad, frame[2] - pad - w)
    ys = (frame[3] - pad - h, frame[1] + title_h + pad)
    for y in ys:
        for x in xs:
            b = [x - 0.8, y - 0.8, x + w + 0.8, y + h + 0.8]
            if not any(b[0] < o[2] and o[0] < b[2] and b[1] < o[3] and o[1] < b[3] for o in obstacles):
                return (round(x, 2), round(y + NOTE_SIZE + 0.3, 2))
    return None


NEAR = True               # a note that names a part stands next to that part when there is room
NEAR_MM = 24.0            # ... at most this far from it (else: the free corner of the frame, as before)
_SUBJ = re.compile(r"^([A-Z]{1,3}\d+)((?:\s*[/,+]\s*[A-Z]{1,3}\d+)*)(?=\s*[:=]|\s|$)")


def subject(line: str) -> tuple:
    """The parts a note line is about: 'D3: I = ..' -> ('D3',), 'R2/C4 low-pass ..' -> ('R2', 'C4'), else ()."""
    m = _SUBJ.match(str(line))
    if not m:
        return ()
    return tuple(re.findall(r"[A-Z]{1,3}\d+", m.group(0)))


def is_legend(line: str) -> bool:
    """'J2 = 2-pin terminal block: ..' - one row of a block's connector legend (the rows stay together)."""
    return bool(re.match(r"^[A-Z]{1,3}\d+\s=\s", str(line)))


def place_near(lines: list, box: list, frame: list, obstacles: list, title_h: float = 5.08, pad: float = 1.9):
    """A stack of note lines beside the thing it describes (`box` = that part with its texts): under it first, then
    right of it, above, left - the nearest free place inside the frame, at most NEAR_MM away. -> baseline of the
    first line or None."""
    w, h = box_of(lines)
    if not lines:
        return None
    lim = [frame[0] + pad, frame[1] + title_h + pad, frame[2] - pad, frame[3] - pad]
    cands = []
    step = 1.27
    n = int(NEAR_MM / step) + 1
    for k in range(n):
        d = 2.0 + k * step
        # a line wider than the room beside its part is pushed back into the frame (still under / over the part)
        for x in (box[0], (box[0] + box[2]) / 2 - w / 2, box[2] - w, min(max(box[0], lim[0]), lim[2] - w)):
            cands.append((d, 0, x, box[3] + d))                     # under
            cands.append((d + 2.0, 2, x, box[1] - d - h))           # above
        for y in ((box[1] + box[3]) / 2 - h / 2, box[1], box[3] - h):
            cands.append((d + 1.0, 1, box[2] + d + 0.5, y))         # right
            cands.append((d + 3.0, 3, box[0] - d - 0.5 - w, y))     # left
    cands.sort(key=lambda c: (c[0], c[1]))
    for _c, _s, x, y in cands:
        if x < lim[0] or y < lim[1] or x + w > lim[2] or y + h > lim[3]:
            continue
        b = [x - 1.0, y - 1.0, x + w + 1.0, y + h + 1.0]
        if not any(b[0] < o[2] and o[0] < b[2] and b[1] < o[3] and o[1] < b[3] for o in obstacles):
            return (round(x, 2), round(y + NOTE_SIZE + 0.3, 2))
    return None


def is_ground_name(name: str) -> bool:
    return LAY.is_ground(name)
