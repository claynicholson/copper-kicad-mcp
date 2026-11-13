"""What can be pointed at: one *scene* per document (a schematic sheet instance
or the board), built from the design files alone.

    sheets(pr)                         -> every schematic sheet INSTANCE (root first)
    select_sheet(pr, sheet)            -> one of them (name / path / file / index)
    load(pr, document, sheet=None)     -> Scene (cached until the files change)
    stamp(pr, document)                -> cheap change token (mtime+size of the files)

A scene item is a plain dict::

    {'ref','value','footprint','x','y','rot','bbox':[x0,y0,x1,y1],
     'pins':[{'num','name','x','y','net', (schematic) 'ix','iy', (pcb) 'hw','hh' = pad half extents,
              'a' = pad angle in degrees when it is not axis-aligned (hw/hh are then in the pad's own frame)}],
     schematic: 'unit' (multi-unit parts), 'lib_id';   pcb: 'side': 'top'|'bottom'}

Schematic geometry comes from the S-expression model (symbol body + pin
boxes); nets from KiCad's own netlist export. Board geometry (courtyard boxes,
pad centres, pad nets, tracks) is parsed from the .kicad_pcb (`pcbfile`), with the
pcbnew read worker as the fallback for files the parser does not understand.
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .. import sexpr
from ..mcp.registry import ToolError
from . import geometry as G

DOCUMENTS = ("schematic", "pcb")
_lock = threading.Lock()
_cache: dict = {}
_net_cache: dict = {}


@dataclass
class Scene:
    document: str
    view: list                       # [x, y, w, h] mm — the region the canvas shows (SVG viewBox)
    items: list = field(default_factory=list)
    labels: list = field(default_factory=list)    # schematic: {'net','x','y','kind'}; power symbols too
    tracks: list = field(default_factory=list)    # pcb: {'net','a','b','w','layer'} / vias {'net','at','w'}
    subsheets: list = field(default_factory=list)  # schematic: {'name','file','bbox'} sheet symbols on this sheet
    sheet: dict | None = None        # schematic: the sheet instance (see sheets())
    stamp: str = ""
    warnings: list = field(default_factory=list)
    layers: int = 2                  # pcb copper layer count

    def by_ref(self) -> dict:
        out: dict = {}
        for it in self.items:
            out.setdefault(it["ref"], []).append(it)
        return out


# ── change tokens ─────────────────────────────────────────────────────────
def _sig(paths) -> str:
    h = hashlib.sha1()
    for p in paths:
        try:
            st = Path(p).stat()
            h.update(f"{p}|{st.st_mtime_ns}|{st.st_size};".encode())
        except OSError:
            h.update(f"{p}|missing;".encode())
    return h.hexdigest()[:12]


def _sheet_files(pr) -> list[Path]:
    from ..sch import ops
    return ops.sheet_files(pr) or [pr.sch]


def stamp(pr, document: str) -> str:
    """Changes whenever a file of that document changes on disk (stat only — cheap enough to poll)."""
    return _sig(_sheet_files(pr) if document == "schematic" else [pr.pcb])


# ── schematic sheets ──────────────────────────────────────────────────────
def _svg_safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def sheets(pr) -> list[dict]:
    """Every sheet instance: [{'index', 'name': '/' | '/amp/left', 'title', 'file': Path, 'path': '/uuid/…',
    'svg': file stem kicad-cli plots it to}] — root first, breadth-first."""
    from ..sch import ops
    out, names = [], {}
    for h in ops.hierarchy(pr):
        if not h["file"].exists():
            continue
        if h["parent"] is None:
            names[h["path"]] = []
        else:
            parent = h["path"].rsplit("/", 1)[0]
            if parent not in names:
                continue
            names[h["path"]] = names[parent] + [h["name"] or h["file"].stem]
        parts = names[h["path"]]
        out.append({"index": len(out), "name": "/" + "/".join(parts), "title": parts[-1] if parts else "Root",
                    "file": h["file"], "path": h["path"],
                    "svg": pr.name + "".join("-" + _svg_safe(p) for p in parts)})
    return out


def select_sheet(pr, sheet=None) -> dict:
    """`sheet` = None/'/' (root), an index, a sheet path ('/amp/left'), a sheet name, or a file name/stem
    (when only one instance uses that file)."""
    if not pr.sch.exists():
        raise ToolError(f"no schematic {pr.sch.name}", code="NO_SCHEMATIC",
                        fix="project_open(path=...) on a project that has a schematic, or create one with sch_apply")
    all_ = sheets(pr)
    if not all_:
        raise ToolError(f"{pr.sch.name} could not be read", code="PARSE_FAILED", fix="open it in KiCad and re-save")
    if sheet is None or str(sheet).strip() in ("", "/", "root", "Root"):
        return all_[0]
    s = str(sheet).strip()
    if s.isdigit() and int(s) < len(all_):
        return all_[int(s)]
    key = "/" + s.replace("\\", "/").strip("/").lower()
    hit = [e for e in all_ if e["name"].lower() == key]
    if not hit:
        last = key.rsplit("/", 1)[-1]
        hit = [e for e in all_[1:] if e["name"].lower().rsplit("/", 1)[-1] == last]
    if not hit:
        stem = Path(s).stem.lower()
        hit = [e for e in all_ if e["file"].stem.lower() == stem]
    if len(hit) == 1:
        return hit[0]
    listing = ", ".join(f"'{e['name']}'" + (f" ({e['file'].name})" if e["index"] else "") for e in all_[:24])
    if hit:
        raise ToolError(f"sheet '{sheet}' is ambiguous: {', '.join(e['name'] for e in hit)}", code="SHEET_AMBIGUOUS",
                        fix=f"pass the full sheet path — sheets: {listing}")
    raise ToolError(f"no sheet '{sheet}' in {pr.sch.name}", code="SHEET_NOT_FOUND",
                    fix=f"sheets: {listing} — omit `sheet` for the root")


# ── nets from KiCad's netlist ─────────────────────────────────────────────
def pin_nets(pr) -> tuple[dict, str | None]:
    """({(ref, pin number): net name}, warning) — cached until a sheet file changes."""
    key = (str(pr.dir).lower(), pr.name)
    sig = stamp(pr, "schematic")
    with _lock:
        c = _net_cache.get(key)
        if c and c[0] == sig:
            return c[1], c[2]
    from ..sch import netlist
    warn = None
    try:
        pn = netlist.pin_nets(netlist.export_netlist(pr))
    except ToolError as e:
        pn, warn = {}, f"net names unavailable ({e.message[:160]})"
    with _lock:
        _net_cache[key] = (sig, pn, warn)
    return pn, warn


def is_unconnected(net: str) -> bool:
    return (not net) or net.startswith("unconnected-(") or "unconnected" in net and net.startswith("Net-(")


# ── schematic scene ───────────────────────────────────────────────────────
def _schematic(pr, entry: dict) -> Scene:
    from ..sch.model import Schematic, text_width
    sch = Schematic.load(entry["file"], pr.name, sheet_path=entry["path"])
    pn, warn = pin_nets(pr)
    try:
        w, h = sch.page_size()
    except Exception:  # noqa: BLE001
        w, h = 297.0, 210.0
    sc = Scene("schematic", [0.0, 0.0, float(w), float(h)], sheet=entry, stamp=stamp(pr, "schematic"))
    if warn:
        sc.warnings.append(warn)
    for s in sch.symbols():
        try:
            ref, at, bbox = s.ref, s.at, s.body_bbox()
            pins = s.pins_world()
        except Exception:  # noqa: BLE001 — one odd symbol must not hide the sheet
            continue
        if s.is_power or ref.startswith("#"):
            sc.labels.append({"net": s.value, "x": at[0], "y": at[1], "kind": "power"})
            continue
        multi = len({p["unit"] for p in s.lib_pins() if p["unit"]}) > 1
        many = len(pins) > 4
        plist = []
        for p in pins:
            x, y = p["xy"]
            ox, oy = p.get("out", (0, 0))
            # the clickable extent of a pin: its line, plus (on ICs) the name printed inside the body
            inner = float(p.get("length") or 0) + (min(12.0, text_width(p.get("name") or "") + 1.0) if many else 0.0)
            d = {"num": p["number"], "name": p["name"] if p["name"] not in ("~", "") else "",
                 "x": G.r3(x), "y": G.r3(y), "ix": G.r3(x - ox * inner), "iy": G.r3(y - oy * inner),
                 "net": pn.get((ref, p["number"]), "")}
            if p.get("hidden"):
                d["hidden"] = True
            plist.append(d)
        it = {"ref": ref, "value": s.value, "footprint": s.field("Footprint"), "lib_id": s.lib_id,
              "x": G.r3(at[0]), "y": G.r3(at[1]), "rot": s.rot, "bbox": G.rbox(bbox), "pins": plist}
        if multi:
            it["unit"] = s.unit
        if s.dnp:
            it["dnp"] = True
        sc.items.append(it)
    for lb in sch.labels():
        sc.labels.append({"net": lb["net"], "x": lb["at"][0], "y": lb["at"][1], "kind": lb["kind"]})
    for sh in sch.items("sheet"):
        at, size = sexpr.find(sh, "at"), sexpr.find(sh, "size")
        if at is None or size is None:
            continue
        props = {str(p[1]): str(p[2]) for p in sexpr.find_all(sh, "property") if len(p) > 2}
        x, y, sw, shh = float(at[1]), float(at[2]), float(size[1]), float(size[2])
        sc.subsheets.append({"name": props.get("Sheetname") or props.get("Sheet name", ""),
                             "file": props.get("Sheetfile") or props.get("Sheet file", ""),
                             "bbox": G.rbox([x, y, x + sw, y + shh])})
    sc.items.sort(key=lambda i: (G.natural_key(i["ref"]), i.get("unit", 0)))
    return sc


# ── board scene ───────────────────────────────────────────────────────────
def read_board(pr, source: str = "auto") -> dict:
    """Footprints (+pads), tracks, outline of the board file. source: 'file' = parse the .kicad_pcb directly
    (fast, no pcbnew), 'pcbnew' = the read worker, 'auto' = file, falling back to pcbnew if the parser balks."""
    if source in ("auto", "file"):
        from . import pcbfile
        try:
            d = pcbfile.read(pr.pcb)
            d["source"] = "file"
            return d
        except Exception:  # noqa: BLE001 — unknown dialect: let KiCad itself read it
            if source == "file":
                raise
    from ..pcb import worker
    res = worker.run("board", {"pcb": str(pr.pcb), "save": False, "summary": False,
                               "ops": [{"op": "read", "what": ["footprints", "pads", "tracks"], "limit": 200000}]},
                     timeout=180)
    d = res["results"][0]
    d["source"] = "pcbnew"
    return d


def _pcb(pr) -> Scene:
    if not pr.pcb.exists():
        raise ToolError(f"{pr.pcb.name} does not exist yet", code="NO_BOARD",
                        fix="pcb_sync() to create the board from the schematic")
    st = stamp(pr, "pcb")
    d = read_board(pr)
    sc = Scene("pcb", [0, 0, 297, 210], stamp=st, layers=int(d.get("copper_layers") or 2))
    boxes = []
    ol = d.get("outline") or {}
    if ol.get("bbox"):
        boxes.append(ol["bbox"])
    for fp in d.get("footprints", []):
        ref = fp.get("ref") or ""
        bbox = G.rbox(fp["bbox"])
        boxes.append(bbox)
        if not ref or ref.upper().startswith("REF**"):
            continue
        sc.items.append({"ref": ref, "value": fp.get("value", ""), "footprint": fp.get("footprint", ""),
                         "x": G.r3(fp["x"]), "y": G.r3(fp["y"]), "rot": fp.get("rot", 0), "side": fp.get("side", "top"),
                         "bbox": bbox,
                         "pins": [dict({"num": p.get("num", ""), "name": "", "x": G.r3(p["x"]), "y": G.r3(p["y"]),
                                        "net": p.get("net", ""), "hw": p.get("hw", 0), "hh": p.get("hh", 0)},
                                       **({"a": p["a"]} if p.get("a") else {}))
                                  for p in fp.get("pad_list", []) if p.get("num")]})
    for t in d.get("track_list", []):
        if t.get("type") == "via":
            x, y = t["at"]
            sc.tracks.append({"net": t.get("net", ""), "at": [x, y], "w": t.get("size", 0.6)})
            boxes.append([x, y, x, y])
        elif "start" in t:
            a, b = t["start"], t["end"]
            sc.tracks.append({"net": t.get("net", ""), "a": a, "b": b, "w": t.get("width", 0.2), "layer": t.get("layer", "")})
            boxes.append([min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])])
    u = G.union(boxes)
    if u:
        m = max(3.0, 0.04 * max(u[2] - u[0], u[3] - u[1]))
        sc.view = G.rbox([u[0] - m, u[1] - m, (u[2] - u[0]) + 2 * m, (u[3] - u[1]) + 2 * m])
    sc.items.sort(key=lambda i: G.natural_key(i["ref"]))
    return sc


def load(pr, document: str, sheet=None) -> Scene:
    """The scene of a document, cached until its files change. Raises ToolError (NO_BOARD / NO_SCHEMATIC /
    SHEET_NOT_FOUND) with an exact fix."""
    if document not in DOCUMENTS:
        raise ToolError(f"document must be 'schematic' or 'pcb', got {document!r}", code="BAD_ARGUMENT",
                        fix="focus(action='resolve', document='pcb', box=[x0,y0,x1,y1])")
    entry = select_sheet(pr, sheet) if document == "schematic" else None
    key = (str(pr.dir).lower(), pr.name, document, entry["path"] if entry else "")
    st = stamp(pr, document)
    with _lock:
        c = _cache.get(key)
        if c and c.stamp == st:
            return c
    sc = _schematic(pr, entry) if document == "schematic" else _pcb(pr)
    with _lock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = sc
    return sc
