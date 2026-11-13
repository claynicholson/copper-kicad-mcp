"""Symbol geometry: lib → schematic transforms, pin world positions, pin lookup.

KiCad library coordinates have +Y pointing UP; schematic coordinates have +Y
pointing DOWN. eeschema maps a library point ``p`` to the sheet with a 2×2
TRANSFORM matrix ``(x1, y1, x2, y2)``::

    world = symbol_at + (x1*p.x + y1*p.y,  x2*p.x + y2*p.y)

The default (orientation 0) is ``(1, 0, 0, -1)`` — i.e. just the Y flip.
Rotations / mirrors are composed exactly like ``SCH_SYMBOL::SetOrientation``
does when it loads ``(at x y ANGLE)`` + ``(mirror x|y)`` from a file. This is
proven against kicad-cli netlist connectivity in tests/test_schematic.py for
all 8 orientations.
"""
from __future__ import annotations

import re

GRID = 1.27


def snap(v: float, grid: float = GRID) -> float:
    return round(round(v / grid) * grid, 4)


def snap_pt(p, grid: float = GRID):
    return [snap(p[0], grid), snap(p[1], grid)]


_ID = (1, 0, 0, -1)
_CCW = (0, 1, -1, 0)
_CW = (0, -1, 1, 0)
_MIRROR_Y = (-1, 0, 0, 1)
_MIRROR_X = (1, 0, 0, -1)


def _compose(m, t):
    x1, y1, x2, y2 = m
    a1, b1, a2, b2 = t
    return (x1 * a1 + x2 * b1, y1 * a1 + y2 * b1, x1 * a2 + x2 * b2, y1 * a2 + y2 * b2)


def matrix(rot: int = 0, mirror: str | None = None):
    """TRANSFORM for a symbol stored as ``(at x y rot)`` + optional ``(mirror x|y)``."""
    rot = int(rot) % 360
    m = _ID
    if rot == 90:
        m = _compose(m, _CCW)
    elif rot == 180:
        m = _compose(_compose(m, _CCW), _CCW)
    elif rot == 270:
        m = _compose(m, _CW)
    elif rot != 0:
        raise ValueError(f"rotation must be 0/90/180/270, got {rot}")
    if mirror == "x":
        m = _compose(m, _MIRROR_X)
    elif mirror == "y":
        m = _compose(m, _MIRROR_Y)
    return m


def apply(m, p):
    """Transform a library-relative point (Y up) to a sheet offset (Y down)."""
    x1, y1, x2, y2 = m
    return (x1 * p[0] + y1 * p[1], x2 * p[0] + y2 * p[1])


def to_world(at, m, p):
    dx, dy = apply(m, p)
    return [round(at[0] + dx, 4), round(at[1] + dy, 4)]


_LIBDIR = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}


def pin_tip(at, m, pin) -> list[float]:
    """World coordinate of a pin's connection point (the pin's outer end)."""
    return to_world(at, m, pin["at"])


def pin_outward(m, pin) -> tuple[int, int]:
    """Unit vector (sheet coords) pointing AWAY from the symbol body at this pin."""
    dx, dy = _LIBDIR.get(int(pin.get("angle", 0)) % 360, (1, 0))
    wx, wy = apply(m, (-dx, -dy))
    return (int(round(wx)), int(round(wy)))


def side_of(vec) -> str:
    return {(-1, 0): "left", (1, 0): "right", (0, -1): "top", (0, 1): "bottom"}.get(tuple(vec), "left")


def label_angle(out) -> int:
    """Angle for a label whose anchor is at a stub end, text running further outward."""
    return {(1, 0): 0, (0, -1): 90, (-1, 0): 180, (0, 1): 270}.get(tuple(out), 0)


def bbox_world(at, m, bb):
    """Transform a lib bbox [xmin,ymin,xmax,ymax] to sheet coords."""
    pts = [to_world(at, m, (bb[0], bb[1])), to_world(at, m, (bb[2], bb[3])),
           to_world(at, m, (bb[0], bb[3])), to_world(at, m, (bb[2], bb[1]))]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def rotation_for_direction(natural_lib_vec, want_world_vec) -> int:
    """Rotation (0/90/180/270) so a lib-space direction maps onto a sheet direction."""
    best, bestd = 0, -9
    for r in (0, 90, 180, 270):
        v = apply(matrix(r), natural_lib_vec)
        d = v[0] * want_world_vec[0] + v[1] * want_world_vec[1]
        if d > bestd + 1e-9:
            best, bestd = r, d
    return best


# ── pin lookup ──────────────────────────────────────────────────────────
def strip_decor(name: str) -> str:
    """'~{RESET}' → 'RESET', '~RST' → 'RST', '{slash}' entities, spaces removed, upper-cased."""
    s = re.sub(r"[~_^]\{([^}]*)\}", r"\1", name)
    s = s.replace("{slash}", "/").lstrip("~!")
    return s.strip().upper()


def _segments(name: str) -> list[str]:
    return [strip_decor(seg) for seg in re.split(r"[/,]", name) if seg.strip()]


_SYNONYMS = [
    {"VIN", "VI", "IN", "VIN+", "INPUT"},
    {"VOUT", "VO", "OUT", "OUTPUT"},
    {"GND", "VSS", "GROUND"},
    {"EN", "ENABLE"},
]


def match_pins(pins: list[dict], token: str) -> tuple[list[dict], str]:
    """Resolve a pin token (number or name) against a symbol's pins.

    Order: exact number → exact name → case-insensitive name → decoration-stripped
    name (~{CS} → CS) → '/'-alias segment (PA9/USART1_TX) → alternate function
    name → single-pin fallback. Returns (pins, how). All pins sharing the
    resolved name are returned (e.g. every GND pin)."""
    tok = str(token).strip()
    if not tok:
        return [], ""
    r = [p for p in pins if p["number"] == tok]
    if r:
        return r, "number"
    r = [p for p in pins if p["name"] == tok]
    if r:
        return r, "name"
    tl = tok.lower()
    r = [p for p in pins if p["name"].lower() == tl]
    if r:
        return r, "name-ci"
    r = [p for p in pins if p["number"].lower() == tl]
    if r:
        return r, "number-ci"
    td = strip_decor(tok)
    r = [p for p in pins if p["name"] and strip_decor(p["name"]) == td]
    if r:
        return r, "name-decor"
    r = [p for p in pins if p["name"] and td in _segments(p["name"])]
    if r:
        # 'RXD' matching both 'IO20/RXD' and 'PA3/RXD' is ambiguous; same full name on several pins is not
        return r, ("alias" if len({p["name"] for p in r}) == 1 else "ambiguous")
    r = [dict(p, alternate=a) for p in pins for a in p.get("alternates", []) if strip_decor(a) == td]
    if r:
        return r, ("alternate" if len({p["number"] for p in r}) == 1 else "ambiguous")
    # well-known power-pin synonyms (VIN ↔ VI ↔ IN, VOUT ↔ VO ↔ OUT, …) — only when exactly one name matches
    for group in _SYNONYMS:
        if td.upper() in group:
            names = {p["name"] for p in pins if p["name"] and strip_decor(p["name"]).upper() in group}
            if len(names) == 1:
                return [p for p in pins if p["name"] in names], "synonym"
            break
    # pins whose name differs only by '+'/'-' spacing or leading P (e.g. "D+" vs "DP") are NOT guessed.
    uniq = {p["number"] for p in pins}
    if len(uniq) == 1:
        return pins, "single-pin"
    return [], ""


def suggest_pins(pins: list[dict], token: str, n: int = 8) -> list[str]:
    td = strip_decor(str(token))
    scored = []
    for p in pins:
        nm = strip_decor(p["name"] or "")
        s = 0
        if td and (td in nm or nm in td) and nm:
            s = 2
        elif td[:2] and nm.startswith(td[:2]):
            s = 1
        scored.append((s, p))
    scored.sort(key=lambda t: -t[0])
    out = []
    for s, p in scored:
        lab = f"{p['number']}({p['name']})" if p["name"] else p["number"]
        if lab not in out:
            out.append(lab)
        if len(out) >= n:
            break
    return out
