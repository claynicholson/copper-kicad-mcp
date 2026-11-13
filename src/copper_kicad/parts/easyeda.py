"""EasyEDA (LCSC) → KiCad converter. Clean-room implementation written from
the EasyEDA Standard JSON data format; no code from easyeda2kicad (AGPL).

Fetch chain for a C-number: EasyEDA API (browser UA + Referer) → jlcsearch's
R2 mirror (/api/easyeda_components) → local raw-JSON cache.

Outputs (KiCad 9/10 formats, loadable by both):
  * symbol   — ``(symbol ...)`` S-expression list (multi-unit via subparts)
  * footprint— ``.kicad_mod`` text with pads/silk/fab/courtyard and a model ref
  * 3D       — STEP (as published by EasyEDA) + VRML converted from the OBJ,
               both in the model's native frame; the footprint's (model)
               offset/rotate place it exactly where EasyEDA does.

Units: EasyEDA canvas unit = 10 mil = 0.254 mm. Symbol Y is flipped (KiCad
symbols are Y-up); footprints keep Y-down like KiCad's PCB editor.

Attribution: every EasyEDA payload carries a licence notice requiring that
reproductions credit the "JLCEDA/EasyEDA Official Library" with links to
https://lceda.cn/ and https://easyeda.com — we stamp that into a ``Source``
property on every generated symbol and in every footprint's description.
Assets are generated on the user's machine, on demand, never redistributed.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .. import sexpr
from ..sexpr import Sym
from . import common as C
from .common import NetError

U = 0.254  # mm per EasyEDA unit
EE_URL = "https://easyeda.com/api/products/{lcsc}/components?version=6.4.19.5"
EE_MIRROR = "https://jlcsearch.tscircuit.com/api/easyeda_components/{lcsc}"
OBJ_URL = "https://modules.easyeda.com/3dmodel/{uuid}"
STEP_URL = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{uuid}"
ATTRIBUTION = ("JLCEDA/EasyEDA Official Library (https://lceda.cn/ , https://easyeda.com) — "
               "converted locally by Copper for KiCad")
SYM_VERSION = 20241209   # KiCad 9 format; KiCad 10 loads it natively
FP_VERSION = 20241229


class ConvertError(Exception):
    pass


# ── fetching ─────────────────────────────────────────────────────────────
def fetch(lcsc: str, refresh: bool = False) -> dict:
    """Raw EasyEDA component ``result`` dict for a C-number."""
    code = C.lcsc_norm(lcsc)
    if not code:
        raise ConvertError(f"not an LCSC number: {lcsc!r}")
    if not refresh:
        hit = C.cache_get("easyeda", code, ttl=60 * 86400)
        if hit:
            return hit
    errors = []
    try:
        d = C.http_json(EE_URL.format(lcsc=code), headers={"User-Agent": C.UA_BROWSER, "Referer": "https://easyeda.com/",
                                                         "Accept": "application/json"}, retries=2, timeout=30)
        if d.get("success") and d.get("result"):
            C.cache_put("easyeda", code, d["result"])
            return d["result"]
        errors.append(f"easyeda: {d.get('message') or d.get('code')}")
    except NetError as e:
        errors.append(f"easyeda: {e}")
    try:
        d = C.http_json(EE_MIRROR.format(lcsc=code), headers={"User-Agent": C.UA_BROWSER}, retries=2, timeout=30)
        res = ((d.get("easyeda_component_details") or {}).get("easyeda_json") or {})
        res = res.get("result", res) if isinstance(res, dict) else {}
        if res.get("dataStr"):
            C.cache_put("easyeda", code, res)
            return res
        errors.append("jlcsearch mirror: no data")
    except NetError as e:
        errors.append(f"jlcsearch mirror: {e}")
    stale = C.cache_get("easyeda", code, ttl=None)
    if stale:
        return stale
    raise NetError(f"{code}: no EasyEDA symbol/footprint available ({'; '.join(errors)})")


def fetch_3d(uuid: str) -> tuple[str | None, bytes | None]:
    """(obj_text, step_bytes) for an EasyEDA 3D model uuid (either may be None)."""
    obj = step = None
    cdir = C.cache_dir("easyeda3d")
    fo, fs = cdir / f"{uuid}.obj", cdir / f"{uuid}.step"
    if fo.exists():
        obj = fo.read_text(encoding="utf-8", errors="replace")
    else:
        try:
            obj = C.http(OBJ_URL.format(uuid=uuid), headers={"User-Agent": C.UA_BROWSER}, timeout=60, retries=1).decode("utf-8", "replace")
            if "v " not in obj[:20000]:
                obj = None
            else:
                fo.write_text(obj, encoding="utf-8")
        except NetError:
            obj = None
    if fs.exists():
        step = fs.read_bytes()
    else:
        try:
            step = C.http(STEP_URL.format(uuid=uuid), headers={"User-Agent": C.UA_BROWSER}, timeout=90, retries=1)
            if not step.startswith(b"ISO-10303"):
                step = None
            else:
                fs.write_bytes(step)
        except NetError:
            step = None
    return obj, step


# ── helpers ──────────────────────────────────────────────────────────────
def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _r(x: float, nd=4) -> float:
    v = round(x, nd)
    return 0.0 if v == 0 else v


def safe_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.+\-]+", "_", str(s or "").strip())
    return s.strip("_") or "part"


_PATH_TOK = re.compile(r"[MmLlHhVvAaCcQqZzSsTt]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def path_polylines(d: str, arc_seg=12) -> list[tuple[list[tuple[float, float]], bool]]:
    """SVG path → [(points, closed)] (arcs/curves flattened)."""
    toks = _PATH_TOK.findall(d or "")
    out, cur, closed = [], [], False
    x = y = sx = sy = 0.0
    i, cmd = 0, None

    def num():
        nonlocal i
        v = float(toks[i]); i += 1
        return v
    while i < len(toks):
        t = toks[i]
        if re.fullmatch(r"[A-Za-z]", t):
            cmd = t; i += 1
            if cmd in "Zz":
                if cur:
                    out.append((cur, True))
                cur = []
                x, y = sx, sy
                continue
        if cmd is None:
            i += 1
            continue
        try:
            if cmd in "Mm":
                nx, ny = num(), num()
                if cmd == "m":
                    nx += x; ny += y
                if cur:
                    out.append((cur, False))
                cur = [(nx, ny)]
                x, y, sx, sy = nx, ny, nx, ny
                cmd = "L" if cmd == "M" else "l"
            elif cmd in "Ll":
                nx, ny = num(), num()
                if cmd == "l":
                    nx += x; ny += y
                x, y = nx, ny; cur.append((x, y))
            elif cmd in "Hh":
                v = num(); x = v + (x if cmd == "h" else 0); cur.append((x, y))
            elif cmd in "Vv":
                v = num(); y = v + (y if cmd == "v" else 0); cur.append((x, y))
            elif cmd in "Aa":
                rx, ry, rot, fa, fs, nx, ny = num(), num(), num(), num(), num(), num(), num()
                if cmd == "a":
                    nx += x; ny += y
                pts = arc_points(x, y, rx, ry, rot, int(fa), int(fs), nx, ny, arc_seg)
                cur.extend(pts[1:])
                x, y = nx, ny
            elif cmd in "CcSsQqTt":
                n = {"C": 6, "S": 4, "Q": 4, "T": 2}[cmd.upper()]
                vals = [num() for _ in range(n)]
                if cmd.islower():
                    vals = [v + (x if k % 2 == 0 else y) for k, v in enumerate(vals)]
                x, y = vals[-2], vals[-1]
                cur.append((x, y))
            else:
                i += 1
        except (IndexError, ValueError):
            break
    if cur:
        out.append((cur, closed))
    return out


def arc_center(x1, y1, rx, ry, phi, fa, fs, x2, y2):
    """SVG endpoint arc → (cx, cy, r, theta1, dtheta) for circular arcs."""
    r = max(abs(rx), abs(ry))
    if r == 0 or (x1 == x2 and y1 == y2):
        return None
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    d2 = dx * dx + dy * dy
    if d2 > r * r:
        r = math.sqrt(d2)
    h = math.sqrt(max(0.0, r * r - d2))
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    L = math.sqrt(d2) or 1e-12
    ux, uy = -dy / L, dx / L   # unit perpendicular
    sign = 1 if fa != fs else -1
    cx, cy = mx + sign * h * ux, my + sign * h * uy
    t1 = math.atan2(y1 - cy, x1 - cx)
    t2 = math.atan2(y2 - cy, x2 - cx)
    dt = t2 - t1
    if fs and dt < 0:
        dt += 2 * math.pi
    elif not fs and dt > 0:
        dt -= 2 * math.pi
    return cx, cy, r, t1, dt


def arc_points(x1, y1, rx, ry, phi, fa, fs, x2, y2, n=12):
    a = arc_center(x1, y1, rx, ry, phi, fa, fs, x2, y2)
    if not a:
        return [(x1, y1), (x2, y2)]
    cx, cy, r, t1, dt = a
    return [(cx + r * math.cos(t1 + dt * k / n), cy + r * math.sin(t1 + dt * k / n)) for k in range(n + 1)]


def arc_mid(x1, y1, rx, ry, phi, fa, fs, x2, y2):
    a = arc_center(x1, y1, rx, ry, phi, fa, fs, x2, y2)
    if not a:
        return None
    cx, cy, r, t1, dt = a
    return cx + r * math.cos(t1 + dt / 2), cy + r * math.sin(t1 + dt / 2)


def _parse_arc_path(d: str):
    m = re.search(r"M\s*([-\d.eE]+)[\s,]+([-\d.eE]+)\s*A\s*([-\d.eE]+)[\s,]+([-\d.eE]+)[\s,]+([-\d.eE]+)[\s,]+([01])[\s,]*([01])[\s,]*([-\d.eE]+)[\s,]+([-\d.eE]+)", d or "")
    if not m:
        return None
    v = [float(g) for g in m.groups()]
    return v[0], v[1], v[2], v[3], v[4], int(v[5]), int(v[6]), v[7], v[8]


def _effects(size=1.27, hide=False, justify=None):
    e = [Sym("effects"), [Sym("font"), [Sym("size"), size, size]]]
    if justify:
        e.append([Sym("justify"), *[Sym(j) for j in justify]])
    if hide:
        e.append([Sym("hide"), Sym("yes")])
    return e


def _stroke(w=0.254, typ="default"):
    return [Sym("stroke"), [Sym("width"), _r(w)], [Sym("type"), Sym(typ)]]


def _fill(t="none"):
    return [Sym("fill"), [Sym("type"), Sym(t)]]


# ── symbol ───────────────────────────────────────────────────────────────
_ELEC = {"0": "passive", "1": "input", "2": "output", "3": "bidirectional", "4": "power_in",
         "5": "open_collector", "6": "open_emitter", "7": "tri_state"}


def _pin_from(shape: str, ox: float, oy: float, connector: bool):
    segs = shape.split("^^")
    h = segs[0].split("~")
    if len(h) < 7:
        return None
    elec = h[2] if len(h) > 2 else "0"
    x, y = _f(h[4]), _f(h[5])
    rot = _f(h[6]) if h[6] != "" else 0.0
    name_seg = segs[3].split("~") if len(segs) > 3 else []
    num_seg = segs[4].split("~") if len(segs) > 4 else []
    number = (num_seg[4] if len(num_seg) > 4 and num_seg[4] != "" else h[3]).strip()
    name = name_seg[4].strip() if len(name_seg) > 4 else ""
    name_vis = len(name_seg) > 0 and name_seg[0] == "1"
    num_vis = len(num_seg) > 0 and num_seg[0] == "1"
    dot = len(segs) > 5 and segs[5].split("~")[0] == "1"
    clock = len(segs) > 6 and segs[6].split("~")[0] == "1"
    # geometry from the pin line path (authoritative); rotation as fallback
    length = 10.0
    direction = None
    if len(segs) > 2:
        pl = path_polylines(segs[2].split("~")[0])
        if pl and len(pl[0][0]) >= 2:
            (ax, ay), (bx, by) = pl[0][0][0], pl[0][0][-1]
            # far end from the connection point is the body end
            da = math.hypot(ax - x, ay - y)
            db = math.hypot(bx - x, by - y)
            ex, ey = (bx, by) if db >= da else (ax, ay)
            length = math.hypot(ex - x, ey - y) or 10.0
            direction = (ex - x, ey - y)
    if direction is None:
        a = math.radians(rot)
        direction = (-math.cos(a), math.sin(a))  # EasyEDA rot points outward (body→pin end)
    dx, dy = direction
    # KiCad: angle points from connection point toward body; KiCad Y is up
    if abs(dx) >= abs(dy):
        ang = 0 if dx > 0 else 180
    else:
        ang = 270 if dy > 0 else 90
    etype = _ELEC.get(elec, "passive")
    if connector and etype in ("power_in", "input", "output"):
        etype = "passive"
    shape_k = "inverted_clock" if dot and clock else ("inverted" if dot else ("clock" if clock else "line"))
    if name == "" or name == number and not name_vis:
        pass
    return {"x": (x - ox) * U, "y": -(y - oy) * U, "angle": ang, "length": length * U, "number": number,
            "name": name, "name_vis": name_vis, "num_vis": num_vis, "type": etype, "shape": shape_k}


def _sym_graphics(shapes: list[str], ox: float, oy: float) -> tuple[list, list[dict]]:
    """→ (graphic items, raw rect list for fill choice)."""
    g = []
    rects = []

    def P(px, py):
        return [Sym("xy"), _r((px - ox) * U), _r(-(py - oy) * U)]
    for s in shapes:
        f = s.split("~")
        t = f[0]
        try:
            if t == "R":
                x, y, w, h = _f(f[1]), _f(f[2]), _f(f[5]), _f(f[6])
                sw = max(_f(f[8], 1), 0.5) * U if len(f) > 8 else 0.254
                fill = f[10] if len(f) > 10 else "none"
                item = [Sym("rectangle"), [Sym("start"), _r((x - ox) * U), _r(-(y - oy) * U)],
                        [Sym("end"), _r((x + w - ox) * U), _r(-(y + h - oy) * U)], _stroke(0.254 if sw < 0.3 else sw), None]
                rects.append({"item": item, "area": w * h, "fill": fill})
                g.append(item)
            elif t in ("PL", "PG"):
                nums = [_f(v) for v in re.split(r"[\s,]+", f[1].strip()) if v]
                pts = list(zip(nums[0::2], nums[1::2]))
                if len(pts) < 2:
                    continue
                if t == "PG" and pts[0] != pts[-1]:
                    pts.append(pts[0])
                fill = f[5] if len(f) > 5 else "none"
                ftype = "outline" if fill not in ("none", "", None) and (t == "PG" or pts[0] == pts[-1]) else "none"
                g.append([Sym("polyline"), [Sym("pts"), *[P(*p) for p in pts]], _stroke(), _fill(ftype)])
            elif t == "PT":
                fill = f[5] if len(f) > 5 else "none"
                for pts, closed in path_polylines(f[1]):
                    if closed and pts[0] != pts[-1]:
                        pts = pts + [pts[0]]
                    if len(pts) < 2:
                        continue
                    ftype = "outline" if closed and fill not in ("none", "") else "none"
                    g.append([Sym("polyline"), [Sym("pts"), *[P(*p) for p in pts]], _stroke(), _fill(ftype)])
            elif t == "C":
                cx, cy, r = _f(f[1]), _f(f[2]), _f(f[3])
                fill = f[7] if len(f) > 7 else "none"
                g.append([Sym("circle"), [Sym("center"), _r((cx - ox) * U), _r(-(cy - oy) * U)], [Sym("radius"), _r(r * U)],
                          _stroke(), _fill("outline" if fill not in ("none", "") else "none")])
            elif t == "E":
                cx, cy, rx, ry = _f(f[1]), _f(f[2]), _f(f[3]), _f(f[4])
                fill = f[8] if len(f) > 8 else "none"
                ft = "outline" if fill not in ("none", "") else "none"
                if abs(rx - ry) < 1e-6:
                    g.append([Sym("circle"), [Sym("center"), _r((cx - ox) * U), _r(-(cy - oy) * U)],
                              [Sym("radius"), _r(rx * U)], _stroke(), _fill(ft)])
                else:
                    pts = [(cx + rx * math.cos(2 * math.pi * k / 32), cy + ry * math.sin(2 * math.pi * k / 32)) for k in range(33)]
                    g.append([Sym("polyline"), [Sym("pts"), *[P(*p) for p in pts]], _stroke(), _fill(ft)])
            elif t == "A":
                a = _parse_arc_path(f[1])
                if not a:
                    for pts, closed in path_polylines(f[1]):
                        g.append([Sym("polyline"), [Sym("pts"), *[P(*p) for p in pts]], _stroke(), _fill()])
                    continue
                x1, y1, rx, ry, phi, fa, fs, x2, y2 = a
                mid = arc_mid(x1, y1, rx, ry, phi, fa, fs, x2, y2)
                if mid is None:
                    continue
                g.append([Sym("arc"), [Sym("start"), _r((x1 - ox) * U), _r(-(y1 - oy) * U)],
                          [Sym("mid"), _r((mid[0] - ox) * U), _r(-(mid[1] - oy) * U)],
                          [Sym("end"), _r((x2 - ox) * U), _r(-(y2 - oy) * U)], _stroke(), _fill()])
            elif t == "L":   # line: L~x1~y1~x2~y2~...
                x1, y1, x2, y2 = _f(f[1]), _f(f[2]), _f(f[3]), _f(f[4])
                g.append([Sym("polyline"), [Sym("pts"), P(x1, y1), P(x2, y2)], _stroke(), _fill()])
            elif t == "T":
                # T~mark~x~y~rot~color~font~size~weight~style~baseline~type~text~visible~anchor~id
                mark = f[1]
                if mark in ("P", "N") or len(f) < 14 or f[13] == "0":
                    continue
                text = f[12]
                if not text.strip():
                    continue
                x, y, rot = _f(f[2]), _f(f[3]), _f(f[4])
                size = 1.27
                m = re.match(r"([\d.]+)pt", f[7] or "")
                if m:
                    size = max(0.8, min(2.5, float(m.group(1)) * 0.1))
                anchor = f[14] if len(f) > 14 else "start"
                just = {"start": ["left"], "end": ["right"]}.get(anchor)
                g.append([Sym("text"), text, [Sym("at"), _r((x - ox) * U), _r(-(y - oy) * U), int((-rot) % 360) if rot in (90, 270) else 0],
                          _effects(size, justify=just)])
        except (IndexError, ValueError):
            continue
    return g, rects


def _snap_offset(pins: list[dict], grid=1.27) -> tuple[float, float]:
    """Shift so pin connection points land on the 50 mil grid."""
    if not pins:
        return 0.0, 0.0
    best = (0.0, 0.0)
    best_n = -1
    for p in pins[:12]:
        dx = round(p["x"] / grid) * grid - p["x"]
        dy = round(p["y"] / grid) * grid - p["y"]
        n = sum(1 for q in pins if abs((q["x"] + dx) / grid - round((q["x"] + dx) / grid)) < 0.02
                and abs((q["y"] + dy) / grid - round((q["y"] + dy) / grid)) < 0.02)
        if n > best_n:
            best_n, best = n, (dx, dy)
    return best


def _shift(item, dx, dy):
    if not isinstance(item, list) or not item:
        return
    if item[0] in ("xy", "start", "end", "mid", "center", "at") and len(item) >= 3 and \
            isinstance(item[1], (int, float)) and isinstance(item[2], (int, float)):
        item[1] = _r(item[1] + dx)
        item[2] = _r(item[2] + dy)
        return
    for e in item[1:]:
        _shift(e, dx, dy)


_KIND_PREFIX = {"header": "J", "connector": "J", "usb_connector": "J", "switch": "SW", "crystal": "Y",
                "oscillator": "Y", "led": "D", "diode": "D", "schottky": "D", "zener": "D", "rectifier": "D",
                "bridge": "D", "tvs": "D", "mosfet": "Q", "bjt": "Q", "fuse": "F", "resistor": "R", "capacitor": "C",
                "electrolytic": "C", "tantalum": "C", "inductor": "L", "ferrite": "FB"}
# ICs whose EasyEDA pins are all 'passive' (common): infer power pins from their names so ERC can
# flag an unpowered VDD. First name alias decides ('VDDA/DDA' → VDDA).
_PWR_IN = re.compile(r"^(?:\w*VDD\w*|\w*VCC\w*|\w*VSS\w*|[A-Z]?GND\w*|VBAT|VREG_VIN|V\d+V\d*|V\d+P\d+|V?IN)$", re.I)
_PWR_OUT = re.compile(r"^(?:VOUT\w*|VO|OUT)$", re.I)
_IC_KINDS = ("ic", "mcu", "memory", "opamp", "interface", "ldo", "dcdc", "module", "other", "esd", "oscillator")


_CONN_FOOTPRINT = re.compile(r"(?i)(HDR|CONN|USB[-_]|JST|FPC|FFC|IDC|PinHeader|PinSocket)")


def _infer_pin_types(pins: list[dict], kind: str, connector: bool) -> None:
    """In place: when EasyEDA typed every pin 'passive' on an IC, make supply/ground pins power_in (and a
    regulator's VOUT power_out). Pins EasyEDA did type are left alone; connectors/passives untouched;
    exposed pads, VBUS (often a sense input) and SW/LX nodes stay passive."""
    if connector or kind not in _IC_KINDS or any(p["type"] != "passive" for p in pins):
        return
    for p in pins:
        nm = re.split(r"[/\[(]", p["name"] or "")[0].strip().lstrip("~")
        if not nm:
            continue
        if kind in ("ldo", "dcdc") and _PWR_OUT.match(nm):
            p["type"] = "power_out"
        elif _PWR_IN.match(nm):
            if re.fullmatch(r"(?i)V?IN", nm) and kind not in ("ldo", "dcdc"):
                continue       # 'IN' on an op-amp/ESD part is a signal
            p["type"] = "power_in"


def build_symbol(comp: dict, name: str, fields: dict, footprint: str = "", kind: str = "") -> tuple[list, dict]:
    """EasyEDA component → KiCad (symbol ...) list + info {pins: {num: name}, units, ...}."""
    ds = comp.get("dataStr") or {}
    head = ds.get("head") or {}
    cpara = head.get("c_para") or {}
    units_src = []
    sub = comp.get("subparts") or []
    if sub:
        for sp in sub:
            d = sp.get("dataStr") or {}
            h = d.get("head") or {}
            units_src.append((d.get("shape") or [], _f(h.get("x")), _f(h.get("y"))))
    else:
        units_src.append((ds.get("shape") or [], _f(head.get("x")), _f(head.get("y"))))
    connector = kind in ("usb_connector", "header", "connector") or (cpara.get("pre") or "").upper().startswith(("J", "USB", "CN", "P"))
    prefix = re.sub(r"[?.\d]+$", "", (cpara.get("pre") or "U").split(".")[0]) or "U"
    prefix = {"LED": "D", "USB": "J", "CN": "J", "CON": "J", "X": "Y", "XTAL": "Y", "IC": "U", "K": "SW", "S": "SW",
              "KEY": "SW", "B": "FB", "H": "J", "HDR": "J", "P": "J", "PIN": "J"}.get(prefix.upper(), prefix)
    if prefix.upper().startswith(("USB", "TYPE")):
        prefix = "J"
    # the LCSC category is more reliable than EasyEDA's 'pre' (pin headers ship as 'U?')
    kp = _KIND_PREFIX.get(kind or "")
    if kp and (prefix.upper() in ("U", "IC", "") or kind in ("header", "connector", "usb_connector")):
        prefix = kp
    # last resort: the footprint name says connector (HDR-…, CONN-…, USB-…, JST…) while category and 'pre' say IC
    if prefix.upper() == "U" and _CONN_FOOTPRINT.match(str(footprint or "").split(":")[-1]):
        prefix = "J"
        connector = True

    unit_items, all_pins = [], []
    for shapes, ox, oy in units_src:
        pins = [p for p in (_pin_from(s, ox, oy, connector) for s in shapes if s.startswith("P~")) if p]
        g, rects = _sym_graphics([s for s in shapes if not s.startswith("P~")], ox, oy)
        unit_items.append((g, rects, pins))
        all_pins.extend(pins)
    if not all_pins:
        raise ConvertError("EasyEDA symbol has no pins")
    _infer_pin_types(all_pins, kind, connector)
    for p in all_pins:
        if p["name"] == p["number"]:
            p["name"] = ""          # a name that repeats the number is drawn twice ('1..5' twice on headers)
    dx, dy = _snap_offset(all_pins)
    names_hidden = all(not p["name_vis"] or not p["name"] for p in all_pins)
    nums_hidden = all(not p["num_vis"] for p in all_pins)

    sym = [Sym("symbol"), name]
    if nums_hidden:
        sym.append([Sym("pin_numbers"), [Sym("hide"), Sym("yes")]])
    pn = [Sym("pin_names"), [Sym("offset"), 0 if names_hidden else 1.016]]
    if names_hidden:
        pn.append([Sym("hide"), Sym("yes")])
    sym.append(pn)
    sym += [[Sym("exclude_from_sim"), Sym("no")], [Sym("in_bom"), Sym("yes")], [Sym("on_board"), Sym("yes")]]

    # bbox for property placement
    xs = [p["x"] + dx for p in all_pins]
    ys = [p["y"] + dy for p in all_pins]
    for g, _, _ in unit_items:
        for it in g:
            for xy in _iter_xy(it):
                xs.append(xy[0] + dx); ys.append(xy[1] + dy)
    top, bot = max(ys), min(ys)
    left = min(xs)

    def prop(k, v, at, hide=False, justify=None):
        return [Sym("property"), k, v, [Sym("at"), _r(at[0]), _r(at[1]), 0], _effects(1.27, hide, justify)]
    sym.append(prop("Reference", prefix, (left, top + 1.27), justify=["left"]))
    sym.append(prop("Value", fields.get("Value") or name, (left, bot - 1.27), justify=["left"]))
    sym.append(prop("Footprint", footprint, (0, bot - 3.81), hide=True))
    sym.append(prop("Datasheet", fields.get("Datasheet", "") or "", (0, bot - 6.35), hide=True))
    sym.append(prop("Description", fields.get("Description", "") or "", (0, bot - 8.89), hide=True))
    y = bot - 11.43
    for k in ("LCSC", "MPN", "Manufacturer", "JLC Class", "Source"):
        if fields.get(k):
            sym.append(prop(k, fields[k], (0, y), hide=True))
            y -= 2.54
    if fields.get("Keywords"):
        sym.append(prop("ki_keywords", fields["Keywords"], (0, 0), hide=True))

    pinmap = {}
    for ui, (g, rects, pins) in enumerate(unit_items, start=1):
        if rects:
            big = max(rects, key=lambda r: r["area"])
            for r in rects:
                r["item"][4] = _fill("background" if r is big and r["area"] >= 100 else "none")
        body = [Sym("symbol"), f"{name}_{ui}_1"]
        for it in g:
            if dx or dy:
                _shift(it, dx, dy)
            body.append(it)
        for p in pins:
            pin = [Sym("pin"), Sym(p["type"]), Sym(p["shape"]),
                   [Sym("at"), _r(p["x"] + dx), _r(p["y"] + dy), p["angle"]], [Sym("length"), _r(p["length"])]]
            pname = p["name"] if p["name"] else "~"
            pin.append([Sym("name"), pname, _effects(1.27)])
            pin.append([Sym("number"), p["number"], _effects(1.27)])
            body.append(pin)
            pinmap.setdefault(p["number"], p["name"])
        sym.append(body)
    info = {"pins": pinmap, "units": len(unit_items), "prefix": prefix,
            "duplicate_pin_numbers": len(all_pins) != len(pinmap)}
    return sym, info


def _iter_xy(it):
    if not isinstance(it, list) or not it:
        return
    if it[0] in ("xy", "start", "end", "mid", "center") and len(it) >= 3 and isinstance(it[1], (int, float)):
        yield (it[1], it[2])
        return
    for e in it[1:]:
        yield from _iter_xy(e)


# ── footprint ────────────────────────────────────────────────────────────
LAYERS = {"1": "F.Cu", "2": "B.Cu", "3": "F.SilkS", "4": "B.SilkS", "5": "F.Paste", "6": "B.Paste",
          "7": "F.Mask", "8": "B.Mask", "10": "Edge.Cuts", "12": "Dwgs.User", "13": "F.Fab", "14": "B.Fab",
          "15": "Cmts.User", "99": "F.Fab", "101": "F.Fab"}


def _pts(s: str) -> list[tuple[float, float]]:
    n = [_f(v) for v in re.split(r"[\s,]+", (s or "").strip()) if v]
    return list(zip(n[0::2], n[1::2]))


def _dedupe(pts, eps=1e-3):
    out = []
    for p in pts:
        if not out or abs(p[0] - out[-1][0]) > eps or abs(p[1] - out[-1][1]) > eps:
            out.append(p)
    if len(out) > 2 and abs(out[0][0] - out[-1][0]) <= eps and abs(out[0][1] - out[-1][1]) <= eps:
        out.pop()
    # drop collinear midpoints
    res = []
    n = len(out)
    for i in range(n):
        a, b, c = out[i - 1], out[i], out[(i + 1) % n]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) > 1e-6 or n <= 3:
            res.append(b)
    return res


def _rect_of(pts):
    """If pts is an axis-aligned rectangle → (cx, cy, w, h) else None."""
    q = _dedupe(pts)
    if len(q) != 4:
        return None
    xs = sorted({round(p[0], 3) for p in q})
    ys = sorted({round(p[1], 3) for p in q})
    if len(xs) == 2 and len(ys) == 2:
        return (xs[0] + xs[1]) / 2, (ys[0] + ys[1]) / 2, xs[1] - xs[0], ys[1] - ys[0]
    return None


def _rot(px, py, deg):
    """Rotate a vector by a KiCad angle (CCW as displayed, Y-down coordinates)."""
    a = math.radians(deg)
    return px * math.cos(a) + py * math.sin(a), -px * math.sin(a) + py * math.cos(a)


def _pad_angle(shape, w, h, rot, pts):
    """Pick the KiCad angle (±rot) that reproduces EasyEDA's own outline."""
    if not rot:
        return 0.0
    cands = [(-rot) % 360, rot % 360]
    if shape == "RECT" and len(pts) >= 4:
        cx = sum(p[0] for p in pts[:4]) / 4
        cy = sum(p[1] for p in pts[:4]) / 4
        best, err_best = cands[0], 1e9
        for a in cands:
            corners = [_rot(sx * w / 2, sy * h / 2, a) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
            err = sum(min(math.hypot(cx + c[0] - p[0], cy + c[1] - p[1]) for p in pts[:4]) for c in corners)
            if err < err_best:
                best, err_best = a, err
        return best
    if shape == "OVAL" and len(pts) >= 2:
        (ax, ay), (bx, by) = pts[0], pts[1]
        seg = math.degrees(math.atan2(-(by - ay), bx - ax))  # KiCad-style CCW angle of the long axis
        for a in cands:
            long_axis = a if w >= h else a + 90
            if abs(((seg - long_axis + 90) % 180) - 90) < 2:
                return a
    return cands[0]


class Footprint:
    def __init__(self, name: str):
        self.name = name
        self.items: list = []
        self.pads: list[dict] = []
        self.bbox_body: list[tuple[float, float]] = []
        self.model = None
        self.th = False


EDGE_LAYER = "Edge.Cuts"
EDGE_DOC_LAYER = "Dwgs.User"      # where footprint-owned board-edge graphics go by default


def _item_points(it) -> list[tuple[float, float]]:
    """Extent points of one fp_* graphic (a circle contributes its bounding box)."""
    if it and it[0] == "fp_circle":
        c, e = sexpr.find(it, "center"), sexpr.find(it, "end")
        if c is not None and e is not None:
            r = math.hypot(e[1] - c[1], e[2] - c[2])
            return [(c[1] - r, c[2] - r), (c[1] + r, c[2] + r)]
    return [(float(x), float(y)) for x, y in _iter_xy(it)]


def _demote_edge_cuts(items: list, to_layer: str) -> dict | None:
    """Move every graphic a converted footprint has on Edge.Cuts to `to_layer` (in place).

    KiCad treats footprint-owned Edge.Cuts graphics as part of the BOARD outline, so a connector's
    'the board edge goes here' marker (USB-C plugs, card-edge and mid-mount parts) becomes a stray
    open segment that breaks the outline of every board the part is placed on. EasyEDA draws them on
    its BoardOutLine layer as documentation; they belong on a documentation layer in KiCad.
    → {'count', 'moved_to', 'bbox_mm', 'board_edge'?} or None when the footprint has none."""
    pts, n = [], 0
    for it in items:
        for e in it:
            if isinstance(e, list) and e and e[0] == "layer" and len(e) > 1 and e[1] == EDGE_LAYER:
                e[1] = to_layer
                n += 1
                pts += _item_points(it)
    if not n:
        return None
    info = {"count": n, "moved_to": to_layer}
    if pts:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        info["bbox_mm"] = [_r(min(xs)), _r(min(ys)), _r(max(xs)), _r(max(ys))]
        # one straight axis-aligned run is a board-edge marker: say where the edge sits (footprint-local mm)
        if max(ys) - min(ys) < 0.01 <= max(xs) - min(xs):
            info["board_edge"] = {"axis": "y", "at_mm": _r(ys[0])}
        elif max(xs) - min(xs) < 0.01 <= max(ys) - min(ys):
            info["board_edge"] = {"axis": "x", "at_mm": _r(xs[0])}
    return info


def build_footprint(comp: dict, name: str, descr: str = "", tags: str = "",
                    edge_cuts: str = EDGE_DOC_LAYER) -> tuple[str, dict]:
    """EasyEDA package → (.kicad_mod text, meta).

    edge_cuts: layer for graphics EasyEDA drew on its board-outline layer (default Dwgs.User; pass
    'Edge.Cuts' to keep them as real board cuts). meta['edge_cuts'] reports what was moved."""
    pkg = comp.get("packageDetail") or {}
    ds = pkg.get("dataStr") or {}
    head = ds.get("head") or {}
    ox, oy = _f(head.get("x")), _f(head.get("y"))
    shapes = ds.get("shape") or []
    fp = Footprint(name)

    def X(x):
        return _r((x - ox) * U)

    def Y(y):
        return _r((y - oy) * U)
    pad_boxes = []   # in mm, for paste/mask region dedupe
    model = None
    for s in shapes:
        f = s.split("~")
        t = f[0]
        try:
            if t == "PAD":
                p = _pad(f, X, Y)
                if p:
                    fp.pads.append(p)
                    pad_boxes.append(p["bbox"])
            elif t == "HOLE":
                x, y, r = _f(f[1]), _f(f[2]), _f(f[3])
                d = _r(2 * r * U)
                fp.pads.append({"sexpr": [Sym("pad"), "", Sym("np_thru_hole"), Sym("circle"), [Sym("at"), X(x), Y(y)],
                                          [Sym("size"), d, d], [Sym("drill"), d], [Sym("layers"), "*.Cu", "*.Mask"]],
                                "number": "", "bbox": (X(x) - d / 2, Y(y) - d / 2, X(x) + d / 2, Y(y) + d / 2), "th": True})
            elif t == "VIA":
                x, y, dia, r = _f(f[1]), _f(f[2]), _f(f[3]), _f(f[5])
                fp.pads.append({"sexpr": [Sym("pad"), "", Sym("thru_hole"), Sym("circle"), [Sym("at"), X(x), Y(y)],
                                          [Sym("size"), _r(dia * U), _r(dia * U)], [Sym("drill"), _r(2 * r * U)],
                                          [Sym("layers"), "*.Cu", "*.Mask"]], "number": "",
                                "bbox": (X(x), Y(y), X(x), Y(y)), "th": True})
            elif t == "TRACK":
                w, layer = _f(f[1]), LAYERS.get(f[2])
                if not layer:
                    continue
                pts = _pts(f[4])
                for a, b in zip(pts, pts[1:]):
                    fp.items.append([Sym("fp_line"), [Sym("start"), X(a[0]), Y(a[1])], [Sym("end"), X(b[0]), Y(b[1])],
                                     _stroke(max(w * U, 0.05), "solid"), [Sym("layer"), layer]])
                    if layer in ("F.Fab", "F.SilkS"):
                        fp.bbox_body += [(X(a[0]), Y(a[1])), (X(b[0]), Y(b[1]))]
            elif t == "ARC":
                w, layer = _f(f[1]), LAYERS.get(f[2])
                a = _parse_arc_path(f[4])
                if not layer or not a:
                    continue
                x1, y1, rx, ry, phi, fa, fs, x2, y2 = a
                mid = arc_mid(x1, y1, rx, ry, phi, fa, fs, x2, y2)
                if mid is None:
                    continue
                fp.items.append([Sym("fp_arc"), [Sym("start"), X(x1), Y(y1)], [Sym("mid"), X(mid[0]), Y(mid[1])],
                                 [Sym("end"), X(x2), Y(y2)], _stroke(max(w * U, 0.05), "solid"), [Sym("layer"), layer]])
                if layer in ("F.Fab", "F.SilkS"):
                    fp.bbox_body += [(X(x1), Y(y1)), (X(x2), Y(y2)), (X(mid[0]), Y(mid[1]))]
            elif t == "CIRCLE":
                cx, cy, r, w, layer = _f(f[1]), _f(f[2]), _f(f[3]), _f(f[4]), LAYERS.get(f[5])
                if not layer or r <= 0:
                    continue
                fill = "yes" if r * U < 0.3 and layer in ("F.Fab", "F.SilkS") else "no"
                rr = max(r * U, 0.05)
                fp.items.append([Sym("fp_circle"), [Sym("center"), X(cx), Y(cy)], [Sym("end"), _r(X(cx) + rr), Y(cy)],
                                 _stroke(max(w * U, 0.05), "solid"), [Sym("fill"), Sym(fill)], [Sym("layer"), layer]])
            elif t == "RECT":
                x, y, w, h, layer = _f(f[1]), _f(f[2]), _f(f[3]), _f(f[4]), LAYERS.get(f[5])
                if not layer:
                    continue
                sw = _f(f[8], 0.5) * U if len(f) > 8 else 0.1
                fp.items.append([Sym("fp_rect"), [Sym("start"), X(x), Y(y)], [Sym("end"), X(x + w), Y(y + h)],
                                 _stroke(max(sw, 0.05), "solid"), [Sym("fill"), Sym("no")], [Sym("layer"), layer]])
                if layer in ("F.Fab", "F.SilkS"):
                    fp.bbox_body += [(X(x), Y(y)), (X(x + w), Y(y + h))]
            elif t == "SOLIDREGION":
                lid, path, kind = f[1], f[3], (f[4] if len(f) > 4 else "solid")
                if lid == "100" or kind in ("cutout", "npth"):
                    continue
                layer = LAYERS.get(lid)
                if not layer:
                    continue
                for pts, closed in path_polylines(path):
                    q = [(X(px), Y(py)) for px, py in pts]
                    if len(q) < 3:
                        continue
                    if layer in ("F.Paste", "B.Paste", "F.Mask", "B.Mask") and _inside_any(q, pad_boxes):
                        continue  # redundant: pads already carry paste/mask
                    if lid == "99":
                        fp.items.append([Sym("fp_poly"), [Sym("pts"), *[[Sym("xy"), a, b] for a, b in q]],
                                         _stroke(0.1, "solid"), [Sym("fill"), Sym("no")], [Sym("layer"), layer]])
                        fp.bbox_body += q
                    else:
                        fp.items.append([Sym("fp_poly"), [Sym("pts"), *[[Sym("xy"), a, b] for a, b in q]],
                                         _stroke(0, "solid"), [Sym("fill"), Sym("yes")], [Sym("layer"), layer]])
            elif t == "SVGNODE":
                try:
                    j = json.loads(s.split("~", 1)[1])
                except ValueError:
                    continue
                at = j.get("attrs") or {}
                if at.get("c_etype") == "outline3D" and at.get("uuid"):
                    co = [_f(v) for v in str(at.get("c_origin", "0,0")).split(",")]
                    # The projected outline drawn in the SVGNODE children is where
                    # EasyEDA actually shows the body; c_origin is sometimes stale.
                    ol = _svgnode_points(j)
                    if len(ol) >= 3:
                        co = [(min(p[0] for p in ol) + max(p[0] for p in ol)) / 2,
                              (min(p[1] for p in ol) + max(p[1] for p in ol)) / 2]
                    model = {"uuid": at["uuid"], "title": at.get("title") or name,
                             "origin_mm": ((co[0] - ox) * U, -((co[1] if len(co) > 1 else oy) - oy) * U),
                             "z_mm": _f(at.get("z")) * U,
                             "rotation": [(_f(v)) for v in str(at.get("c_rotation", "0,0,0")).split(",")][:3]}
        except (IndexError, ValueError):
            continue
    # still-redundant paste/mask polys placed before their pads were parsed
    fp.items = [it for it in fp.items if not (it[0] == "fp_poly" and _layer_of(it) in ("F.Paste", "B.Paste", "F.Mask", "B.Mask")
                                              and _inside_any([(xy[1], xy[2]) for xy in it[1][1:]], [p["bbox"] for p in fp.pads]))]
    if not fp.pads:
        raise ConvertError("EasyEDA footprint has no pads")
    edge = _demote_edge_cuts(fp.items, edge_cuts) if edge_cuts and edge_cuts != EDGE_LAYER else None
    fp.th = any(p.get("th") and p["number"] != "" for p in fp.pads)
    text, meta = _emit_fp(fp, descr, tags)
    meta["edge_cuts"] = edge
    meta["model"] = model
    meta["origin"] = (ox, oy)
    return text, meta


def _svgnode_points(node) -> list[tuple[float, float]]:
    pts = []
    for ch in node.get("childNodes") or []:
        at = ch.get("attrs") or {}
        for k in ("points", "d"):
            if at.get(k):
                nums = [float(x) for x in re.findall(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", str(at[k]))]
                pts += list(zip(nums[0::2], nums[1::2]))
        pts += _svgnode_points(ch)
    return pts


def _layer_of(it):
    for e in it:
        if isinstance(e, list) and e and e[0] == "layer":
            return e[1]
    return None


def _inside_any(q, boxes, tol=0.05):
    xs, ys = [p[0] for p in q], [p[1] for p in q]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return any(x0 >= b[0] - tol and y0 >= b[1] - tol and x1 <= b[2] + tol and y1 <= b[3] + tol for b in boxes)


def _pad(f, X, Y) -> dict | None:
    shape = f[1]
    x, y, w, h = _f(f[2]), _f(f[3]), _f(f[4]), _f(f[5])
    layer = f[6]
    number = f[8].strip() if len(f) > 8 else ""
    hole_r = _f(f[9]) if len(f) > 9 else 0
    pts = _pts(f[10]) if len(f) > 10 else []
    rot = _f(f[11]) if len(f) > 11 else 0
    hole_len = _f(f[13]) if len(f) > 13 else 0
    hole_pts = _pts(f[14]) if len(f) > 14 else []
    plated = (f[15] if len(f) > 15 else "Y") != "N"
    th = hole_r > 0
    if th:
        ptype = "thru_hole" if plated else "np_thru_hole"
        layers = ["*.Cu", "*.Mask"]
    elif layer == "2":
        ptype, layers = "smd", ["B.Cu", "B.Paste", "B.Mask"]
    else:
        ptype, layers = "smd", ["F.Cu", "F.Paste", "F.Mask"]
    W, H = max(w * U, 0.01), max(h * U, 0.01)
    cx, cy = X(x), Y(y)
    ang = 0.0
    prims = None
    kshape = "rect"
    if shape == "ELLIPSE":
        kshape = "circle"
        W = H = max(W, H)
    elif shape == "OVAL":
        kshape = "oval"
        ang = _pad_angle("OVAL", w, h, rot, pts)
    elif shape == "RECT":
        kshape = "rect"
        ang = _pad_angle("RECT", w, h, rot, pts)
    elif shape == "POLYGON":
        rect = _rect_of(pts)
        if rect:
            kshape = "rect"
            cx, cy = X(rect[0]), Y(rect[1])
            W, H = _r(rect[2] * U), _r(rect[3] * U)
        else:
            q = _dedupe(pts)
            if len(q) < 3:
                kshape = "rect"
            else:
                kshape = "custom"
                rel = [(_r((px - x) * U), _r((py - y) * U)) for px, py in q]
                prims = [Sym("primitives"), [Sym("gr_poly"), [Sym("pts"), *[[Sym("xy"), a, b] for a, b in rel]],
                                             [Sym("width"), 0], [Sym("fill"), Sym("yes")]]]
                ext = min(max(a for a, _ in rel) - min(a for a, _ in rel), max(b for _, b in rel) - min(b for _, b in rel))
                W = H = _r(max(0.05, min(ext * 0.4, 0.5)))
    at = [Sym("at"), cx, cy] + ([_r(ang)] if ang else [])
    p = [Sym("pad"), number, Sym(ptype), Sym(kshape), at, [Sym("size"), _r(W), _r(H)]]
    if th:
        d = 2 * hole_r * U
        if hole_len > 0 and abs(hole_len * U - d) > 1e-3:
            # slot: orientation from the hole centre-line, expressed in pad-local frame
            L = hole_len * U
            local_vertical = H >= W
            if len(hole_pts) >= 2:
                (ax, ay), (bx, by) = hole_pts[0], hole_pts[1]
                seg = math.degrees(math.atan2(-(by - ay), bx - ax))
                rel = ((seg - ang) % 180)
                local_vertical = 45 < rel < 135
            p.append([Sym("drill"), Sym("oval"), _r(d if local_vertical else L), _r(L if local_vertical else d)])
        else:
            p.append([Sym("drill"), _r(d)])
    p.append([Sym("layers"), *layers])
    if kshape == "custom":
        p.append([Sym("options"), [Sym("clearance"), Sym("outline")], [Sym("anchor"), Sym("circle")]])
        p.append(prims)
    # bbox (axis-aligned, rotation-aware)
    if kshape == "custom":
        xs = [cx + a for a, _ in rel]
        ys = [cy + b for _, b in rel]
    else:
        corners = [_rot(sx * W / 2, sy * H / 2, ang) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        xs = [cx + c[0] for c in corners]
        ys = [cy + c[1] for c in corners]
    return {"sexpr": p, "number": number, "th": th, "bbox": (min(xs), min(ys), max(xs), max(ys))}


def _emit_fp(fp: Footprint, descr: str, tags: str) -> tuple[str, dict]:
    xs, ys = [], []
    for p in fp.pads:
        b = p["bbox"]
        xs += [b[0], b[2]]; ys += [b[1], b[3]]
    pad_box = (min(xs), min(ys), max(xs), max(ys))
    for x, y in fp.bbox_body:
        xs.append(x); ys.append(y)
    m = 0.25
    cy0 = math.floor((min(ys) - m) * 100) / 100
    cy1 = math.ceil((max(ys) + m) * 100) / 100
    cx0 = math.floor((min(xs) - m) * 100) / 100
    cx1 = math.ceil((max(xs) + m) * 100) / 100
    tree = [Sym("footprint"), fp.name, [Sym("version"), FP_VERSION], [Sym("generator"), "copper_kicad"],
            [Sym("generator_version"), "1.0"], [Sym("layer"), "F.Cu"],
            [Sym("descr"), descr], [Sym("tags"), tags]]

    def prop(k, v, at, layer, hide=False, size=1.0):
        e = [Sym("property"), k, v, [Sym("at"), _r(at[0]), _r(at[1]), 0], [Sym("layer"), layer]]
        if hide:
            e.append([Sym("hide"), Sym("yes")])
        e.append([Sym("effects"), [Sym("font"), [Sym("size"), size, size], [Sym("thickness"), _r(size * 0.15)]]])
        return e
    tree.append(prop("Reference", "REF**", (0, cy0 - 0.8), "F.SilkS"))
    tree.append(prop("Value", fp.name, (0, cy1 + 0.8), "F.Fab"))
    tree.append(prop("Datasheet", "", (0, 0), "F.Fab", hide=True))
    tree.append(prop("Description", descr, (0, 0), "F.Fab", hide=True))
    tree.append([Sym("attr"), Sym("through_hole" if fp.th else "smd")])
    tree += fp.items
    tree.append([Sym("fp_rect"), [Sym("start"), _r(cx0), _r(cy0)], [Sym("end"), _r(cx1), _r(cy1)],
                 _stroke(0.05, "solid"), [Sym("fill"), Sym("no")], [Sym("layer"), "F.CrtYd"]])
    tree.append([Sym("fp_text"), Sym("user"), "${REFERENCE}", [Sym("at"), 0, 0, 0], [Sym("layer"), "F.Fab"],
                 [Sym("effects"), [Sym("font"), [Sym("size"), 0.8, 0.8], [Sym("thickness"), 0.12]]]])
    for p in sorted(fp.pads, key=_pad_sort):
        tree.append(p["sexpr"])
    meta = {"pads": sorted({p["number"] for p in fp.pads if p["number"] != ""}, key=_natkey),
            "pad_count": sum(1 for p in fp.pads if p["number"] != ""), "through_hole": fp.th,
            "courtyard_mm": [cx0, cy0, cx1, cy1], "pad_extent_mm": [round(v, 3) for v in pad_box], "tree": tree}
    return sexpr.dumps(tree) + "\n", meta


def _natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(s))]


def _pad_sort(p):
    return (p["number"] == "", _natkey(p["number"]))


def set_model(fp_text: str, path: str, offset, rotate) -> str:
    tree = sexpr.loads(fp_text)
    tree = [e for e in tree if not (isinstance(e, list) and e and e[0] == "model")]
    tree.append([Sym("model"), path, [Sym("offset"), [Sym("xyz"), *[_r(v, 4) for v in offset]]],
                 [Sym("scale"), [Sym("xyz"), 1, 1, 1]], [Sym("rotate"), [Sym("xyz"), *[_r(v, 3) for v in rotate]]]])
    return sexpr.dumps(tree) + "\n"


# ── 3D ───────────────────────────────────────────────────────────────────
def parse_obj(text: str):
    """→ (vertices [(x,y,z) mm], groups [(material(rgb, transparency), [faces])])"""
    mats: dict[str, list] = {}
    verts = []
    groups: list = []
    cur_mat = None
    cur = None
    mname = None
    for line in text.splitlines():
        p = line.split()
        if not p:
            continue
        k = p[0]
        if k == "newmtl":
            mname = p[1] if len(p) > 1 else str(len(mats))
            mats[mname] = [(0.7, 0.7, 0.7), 0.0]
        elif k == "Kd" and mname is not None and len(p) >= 4:
            mats[mname][0] = (_f(p[1]), _f(p[2]), _f(p[3]))
        elif k == "d" and mname is not None and len(p) >= 2:
            mats[mname][1] = max(0.0, min(1.0, _f(p[1])))
        elif k == "v" and len(p) >= 4:
            verts.append((_f(p[1]), _f(p[2]), _f(p[3])))
        elif k == "usemtl":
            cur_mat = p[1] if len(p) > 1 else None
            cur = None
        elif k == "f" and len(p) >= 4:
            if cur is None:
                cur = (cur_mat, [])
                groups.append(cur)
            idx = []
            for t in p[1:]:
                try:
                    i = int(t.split("/")[0])
                except ValueError:
                    continue
                idx.append(i - 1 if i > 0 else len(verts) + i)
            if len(idx) >= 3:
                cur[1].append(idx)
    out = []
    for mat, faces in groups:
        rgb, tr = mats.get(mat, [(0.7, 0.7, 0.7), 0.0])
        out.append(((rgb, tr), faces))
    return verts, out


def obj_to_wrl(text: str) -> tuple[str, tuple]:
    """OBJ (mm) → VRML 2.0 (KiCad unit 0.1 inch), native frame. Returns (wrl, bbox_mm)."""
    verts, groups = parse_obj(text)
    if not verts:
        raise ConvertError("empty OBJ")
    s = 1 / 2.54
    out = ["#VRML V2.0 utf8", "# Generated by Copper for KiCad from the " + ATTRIBUTION, ""]
    for (rgb, tr), faces in groups:
        used = sorted({i for f in faces for i in f if 0 <= i < len(verts)})
        remap = {v: k for k, v in enumerate(used)}
        pts = ",\n".join(f"{verts[i][0]*s:.5f} {verts[i][1]*s:.5f} {verts[i][2]*s:.5f}" for i in used)
        idx = ",\n".join(" ".join(str(remap[i]) for i in f if i in remap) + " -1" for f in faces)
        out.append("Shape {\n appearance Appearance { material Material {\n"
                   f"  diffuseColor {rgb[0]:.4f} {rgb[1]:.4f} {rgb[2]:.4f}\n  specularColor 0.2 0.2 0.2\n"
                   f"  ambientIntensity 0.3\n  shininess 0.4\n  transparency {tr:.3f}\n }} }}\n"
                   f" geometry IndexedFaceSet {{\n  solid FALSE\n  creaseAngle 0.5\n  coord Coordinate {{ point [\n{pts}\n  ] }}\n"
                   f"  coordIndex [\n{idx}\n  ]\n }}\n}}\n")
    xs, ys, zs = zip(*verts)
    return "\n".join(out), (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def _rotmat(rx, ry, rz):
    """Rotation matrix R = Rz·Ry·Rx (degrees, right-handed, CCW-positive)."""
    ax, ay, az = (math.radians(v) for v in (rx, ry, rz))
    cx, sx, cy, sy, cz, sz = math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay), math.cos(az), math.sin(az)
    Rx = [[1, 0, 0], [0, cx, -sx], [0, sx, cx]]
    Ry = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]
    Rz = [[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]

    def mul(A, B):
        return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return mul(Rz, mul(Ry, Rx))


def model_transform(model: dict, verts: list[tuple]) -> tuple[list[float], list[float]]:
    """KiCad (offset, rotate) placing the native-frame model like EasyEDA does:
    rotate by c_rotation, then move the rotated bbox centre (XY) to c_origin and
    the bottom to z."""
    rx, ry, rz = (model.get("rotation") or [0, 0, 0]) + [0] * (3 - len(model.get("rotation") or []))
    R = _rotmat(rx, ry, rz)
    xs, ys, zs = [], [], []
    for v in verts[:: max(1, len(verts) // 4000)]:
        x = R[0][0] * v[0] + R[0][1] * v[1] + R[0][2] * v[2]
        y = R[1][0] * v[0] + R[1][1] * v[1] + R[1][2] * v[2]
        z = R[2][0] * v[0] + R[2][1] * v[1] + R[2][2] * v[2]
        xs.append(x); ys.append(y); zs.append(z)
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    tx, ty = model["origin_mm"]
    off = [tx - cx, ty - cy, model.get("z_mm", 0.0) - min(zs)]
    # KiCad's file format stores rotations negated relative to a CCW-positive frame
    return off, [-rx, -ry, -rz]


def step_vertices(step: bytes) -> list[tuple]:
    t = step.decode("latin-1", "replace")
    cp = {m.group(1): (float(m.group(2)), float(m.group(3)), float(m.group(4))) for m in re.finditer(
        r"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-\d.Ee+]+)\s*,\s*([-\d.Ee+]+)\s*,\s*([-\d.Ee+]+)\s*\)\s*\)", t)}
    vp = [m.group(1) for m in re.finditer(r"VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*\)", t)]
    return [cp[i] for i in vp if i in cp]


def model_bbox(verts: list[tuple], offset, rotation) -> tuple:
    """Bounding box (x0, y0, z0, x1, y1, z1, mm, KiCad 3D frame: +Y up, board top at z=0) of a native-frame
    model after KiCad applies ``rotation`` (CCW degrees, i.e. model_transform's input) and ``offset``."""
    R = _rotmat(*(list(rotation) + [0, 0, 0])[:3])
    pts = []
    for v in verts[:: max(1, len(verts) // 4000)]:
        pts.append([sum(R[i][k] * v[k] for k in range(3)) + offset[i] for i in range(3)])
    if not pts:
        return None
    xs, ys, zs = zip(*pts)
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
