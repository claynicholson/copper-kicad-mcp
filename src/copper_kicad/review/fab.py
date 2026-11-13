"""Fabrication outputs: the order pack for a fab profile, and every other kicad-cli export.

fab_export(pr, profile=None, out_dir=None, waive=None, ...) produces, under <project>/copper-out/fab/,
whatever the active fab profile (review/fab_profiles.py, data/fabs/*.json) describes:
    gerbers/        Gerbers + Excellon drill, plotted with the profile's settings (X2 or not, Protel or .gbr
                    extensions, PTH/NPTH split or merged)
    <zip>           gerbers + drill (and BOM + placement when the profile wants them inside), named by the profile
    <bom>           BOM CSV with the profile's columns and part-number field
    <placement>     placement CSV: the profile's own columns with its rotation-correction table, or KiCad's
                    standard position file
and verifies every file exists, is non-empty and that the gerber layer set is complete.
The default profile is 'jlcpcb' (Protel gerbers, 'Comment,Designator,Footprint,LCSC Part #' BOM,
'Designator,Mid X,Mid Y,Layer,Rotation' CPL with the JLC rotation table).

export(pr, kind, out=None) wraps the other kicad-cli exporters with the same
existence checks.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import zipfile
from pathlib import Path

from .. import sexpr
from ..kicad import env
from ..mcp.registry import ToolError
from . import fab_profiles as FP
from .common import cli, field_ci, load_netlist, natural_key, work_dir
from .common import out_dir as _out_dir

DATA = Path(__file__).resolve().parent.parent / "data"

#: 'stock' is a WARNING, never a blocker (it depends on a live supplier answer): waive=['stock'] acknowledges it.
WARN_CODES = ("stock",)
WAIVABLE = set(FP.BLOCK_CODES) | set(WARN_CODES)

# ── board facts ──────────────────────────────────────────────────────────
def copper_layers(pcb: Path) -> list[str]:
    tree = sexpr.loads(Path(pcb).read_text(encoding="utf-8"))
    ls = sexpr.find(tree, "layers")
    out = []
    for e in (ls[1:] if ls else []):
        if isinstance(e, list) and len(e) >= 3 and str(e[1]).endswith(".Cu"):
            out.append(str(e[1]))
    order = {"F.Cu": -1, "B.Cu": 999}
    return sorted(out, key=lambda n: order.get(n, int(re.sub(r"\D", "", n) or 0)))


def board_stats(pr) -> dict:
    """kicad-cli pcb export stats (KiCad 10) → parsed numbers in mm."""
    out = work_dir(pr) / "stats.json"
    if out.exists():
        out.unlink()
    r = cli(["pcb", "export", "stats", "--format", "json", "--units", "mm", "-o", str(out), str(pr.pcb)], timeout=180)
    if not out.exists():
        return {"available": False, "error": (r.stderr or r.stdout).strip()[-300:]}
    try:
        d = json.loads(out.read_text(encoding="utf-8"))
    except ValueError:
        return {"available": False}

    def mm(v):
        try:
            return round(float(str(v).split()[0]), 4)
        except (ValueError, IndexError):
            return None
    b = d.get("board", {})
    res = {"available": True, "has_outline": b.get("has_outline"), "width_mm": mm(b.get("width")),
           "height_mm": mm(b.get("height")), "area_mm2": mm(b.get("area")), "thickness_mm": mm(b.get("board_thickness")),
           "min_track_width_mm": mm(b.get("min_track_width")), "min_track_clearance_mm": mm(b.get("min_track_clearance")),
           "min_drill_mm": mm(b.get("min_drill_diameter")), "pads": d.get("pads", {}), "vias": d.get("vias", {}),
           "components": (d.get("components") or {}).get("total", {})}
    holes = d.get("drill_holes") or []
    res["drill_sizes_mm"] = sorted({mm(h.get("x_size")) for h in holes if mm(h.get("x_size"))})
    res["slots"] = sum(h.get("count", 0) for h in holes if h.get("shape") == "Slot")
    return res


def board_footprints(pr) -> dict:
    """Placement data per footprint, read straight from the .kicad_pcb text:
    anchor, rotation, side, attributes, fields and the centre of the soldered
    pads' bounding box (what JLC's pick-and-place expects). No pcbnew needed —
    KiCad's Python can block on modal dialogs (missing fonts etc.) for minutes
    on some boards, so it is not used here."""
    tree = sexpr.loads(pr.pcb.read_text(encoding="utf-8"))
    fps = []
    for fp in sexpr.find_all(tree, "footprint"):
        fields = {str(p[1]): str(p[2]) for p in sexpr.find_all(fp, "property") if len(p) > 2}
        ref = fields.get("Reference", "")
        at = sexpr.find(fp, "at") or [None, 0, 0]
        x0, y0 = float(at[1]), float(at[2])
        rot = float(at[3]) if len(at) > 3 else 0.0
        attr = sexpr.find(fp, "attr") or []
        attrs = {str(a) for a in attr[1:]}
        lib, _, name = str(fp[1]).rpartition(":")
        pads = [p for p in sexpr.find_all(fp, "pad") if len(p) > 2 and str(p[2]) != "np_thru_hole" and _on_copper(p)]
        fps.append({"ref": ref, "value": fields.get("Value", ""), "lib": lib, "name": name,
                    "layer": "bottom" if sexpr.value(fp, "layer") == "B.Cu" else "top",
                    "rotation": rot, "pos": [x0, y0], "pads_center": _pads_center(pads, x0, y0, rot),
                    "pad_count": len(pads), "smd": "smd" in attrs, "tht": "through_hole" in attrs,
                    "board_only": "board_only" in attrs, "dnp": "dnp" in attrs or sexpr.value(fp, "dnp") in ("yes", True),
                    "exclude_from_bom": "exclude_from_bom" in attrs, "exclude_from_pos": "exclude_from_pos_files" in attrs,
                    "fields": fields})
    return {"copper_layers": len(copper_layers(pr.pcb)), "footprints": fps, "source": "kicad_pcb"}


def _on_copper(pad) -> bool:
    ly = sexpr.find(pad, "layers")
    return bool(ly) and any(str(x).endswith(".Cu") for x in ly[1:])


def _pads_center(pads: list, x0: float, y0: float, rot: float) -> list | None:
    """Centre of the bounding box of the copper pads, in board coordinates.
    Pad (at x y a) is in the footprint frame; `a` already includes the
    footprint rotation. KiCad rotates counter-clockwise on screen (Y down)."""
    if not pads:
        return None
    t = math.radians(rot)
    c, s = math.cos(t), math.sin(t)
    xs0, ys0, xs1, ys1 = [], [], [], []
    for p in pads:
        at = sexpr.find(p, "at")
        size = sexpr.find(p, "size")
        if at is None or size is None:
            continue
        px, py = float(at[1]), float(at[2])
        pa = math.radians(float(at[3])) if len(at) > 3 else t
        w, h = float(size[1]), float(size[2] if len(size) > 2 else size[1])
        ax, ay = x0 + px * c + py * s, y0 - px * s + py * c
        hx = abs(w / 2 * math.cos(pa)) + abs(h / 2 * math.sin(pa))
        hy = abs(w / 2 * math.sin(pa)) + abs(h / 2 * math.cos(pa))
        xs0.append(ax - hx); xs1.append(ax + hx); ys0.append(ay - hy); ys1.append(ay + hy)
    if not xs0:
        return None
    return [round((min(xs0) + max(xs1)) / 2, 4), round((min(ys0) + max(ys1)) / 2, 4)]


# ── rotation corrections ──────────────────────────────────────────────────
def _profile(profile=None, pr=None) -> dict:
    """A profile dict from a dict / a name / None (None = the project's active profile)."""
    if isinstance(profile, dict):
        return profile
    return FP.active(pr, profile)


def load_rotation_rules(pr=None, profile=None) -> list[dict]:
    """Footprint-name → rotation/offset corrections for the profile's assembly service: the project's own
    table(s) first (<project>/.copper/rotations.json), then the table the profile names. Empty when the
    profile has none (the placement file then carries KiCad's rotation unchanged)."""
    prof = _profile(profile, pr)
    rot = (prof.get("placement") or {}).get("rotation") or {}
    table = FP.rotation_table_path(prof)
    if table is None:
        return []
    rules = []
    if pr is not None:
        for n in rot.get("project_tables") or ["rotations.json"]:
            f = pr.dir / ".copper" / n
            if f.exists():
                try:
                    rules += json.loads(f.read_text(encoding="utf-8")).get("rules", [])
                except ValueError:
                    pass
    try:
        rules += json.loads(table.read_text(encoding="utf-8")).get("rules", [])
    except (OSError, ValueError):
        pass
    out = []
    for r in rules:
        try:
            out.append({**r, "_re": re.compile(r["pattern"])})
        except (re.error, KeyError):
            continue
    return out


def _num(s) -> float | None:
    try:
        return float(str(s).strip().replace("°", "").replace("deg", ""))
    except ValueError:
        return None


def native_orientation(fp: dict, fields: dict | None = None, profile=None) -> bool:
    """True when the footprint already uses the assembly service's own zero orientation, so the correction
    table (written for KiCad-stock footprints) must NOT be applied. A profile says which libraries / Source
    fields those are; for JLCPCB that is every EasyEDA/JLCEDA-derived footprint (Copper's parts_import,
    easyeda2kicad, JLC's own plugin) — the table rotated such a TSSOP/SOIC/SOT-23 by 90-270°."""
    rot = (_profile(profile).get("placement") or {}).get("rotation") or {}
    lib_rx, src_rx = rot.get("native_libraries"), rot.get("native_source")
    if lib_rx and re.match(lib_rx, str(fp.get("lib") or ""), re.I):
        return True
    if src_rx:
        src = field_ci({**(fields or {}), **(fp.get("fields") or {})}, "Source", "Footprint Source")
        return bool(re.search(src_rx, src, re.I))
    return False


def placement_row(fp: dict, rules: list[dict], extra_fields: dict | None = None, profile=None) -> dict:
    """KiCad footprint placement → placement-file values (mm, Y up, corrected rotation) for a profile.

    Order: an explicit rotation-offset field (profile.placement.rotation.offset_fields); else no correction for
    footprints that are native to the assembly service; else the first matching rule of the profile's table."""
    prof = _profile(profile)
    pl = prof.get("placement") or {}
    rcfg = pl.get("rotation") or {}
    fields = {**(extra_fields or {}), **(fp.get("fields") or {})}
    rot = float(fp.get("rotation") or 0.0)
    bottom = fp.get("layer") == "bottom"
    base = (180.0 - rot) % 360 if bottom else rot % 360
    corr, off, source = 0.0, (0.0, 0.0), None
    native = native_orientation(fp, fields, prof)
    ov = field_ci(fields, *rcfg["offset_fields"]) if rcfg.get("offset_fields") else ""
    if ov and _num(ov) is not None:
        corr, source = _num(ov), "field"
    elif native:
        source = None              # already in the service's orientation (reported separately as native)
    else:
        for r in rules:
            if r["_re"].search(fp.get("name", "")):
                corr, source = float(r.get("rotation", 0)), f"rule {r['pattern']}"
                o = r.get("offset") or [0, 0]
                off = (float(o[0]), float(o[1]))
                break
    pov = field_ci(fields, *rcfg["position_offset_fields"]) if rcfg.get("position_offset_fields") else ""
    if pov:
        nums = [x for x in (_num(v) for v in re.split(r"[,;\s]+", pov.strip())) if x is not None]
        if len(nums) >= 2:
            off = (nums[0], nums[1])
            source = (source + "+" if source else "") + "pos-field"
    final = (base + corr) % 360
    x, y = (fp.get("pos") if pl.get("position") == "anchor" else None) or fp.get("pads_center") or fp.get("pos")
    if off != (0.0, 0.0):
        a = math.radians(base)
        ox = off[0] * math.cos(a) + off[1] * math.sin(a)
        oy = -off[0] * math.sin(a) + off[1] * math.cos(a)
        if bottom:
            ox = -ox
        x, y = x + ox, y + oy
    names = pl.get("layer_names") or {}
    return {"x": round(x, 4), "y": round(-y, 4), "rotation": round(final, 2),
            "layer": names.get("bottom", "bottom") if bottom else names.get("top", "top"),
            "correction": corr, "offset": list(off), "correction_source": source,
            "native": source is None and native}


# ── assembly data ─────────────────────────────────────────────────────────
def part_field_for(pr, profile: dict) -> str:
    """The schematic field whose value is 'the part number' in this profile's BOM: the profile's own
    (jlcpcb → LCSC), else the project's parts policy / first provider, else MPN."""
    pf = (profile.get("bom") or {}).get("part_field")
    if pf:
        return pf
    try:
        from .. import config
        from ..parts import providers as PV
        return PV.part_field(config.load(pr)) or "MPN"
    except ToolError:
        return "MPN"


def assembly_parts(pr, fps: dict | None = None, part_field: str = "LCSC") -> dict:
    """Merge board footprints with schematic fields. Returns {'parts':[...], 'skipped':[...]}"""
    from ..parts.providers import part_number
    fps = fps or board_footprints(pr)
    sch = {}
    if pr.sch.exists():
        try:
            sch = load_netlist(pr)["components"]
        except ToolError:
            sch = {}
    parts, skipped = [], []
    for fp in fps["footprints"]:
        ref = fp["ref"]
        sc = sch.get(ref, {})
        fields = {**(sc.get("fields") or {}), **(fp.get("fields") or {})}
        why = None
        if not ref or ref.startswith("#") or ref.upper().startswith("REF**"):
            why = "no reference"
        elif fp.get("dnp") or sc.get("dnp"):
            why = "DNP"
        elif fp.get("exclude_from_bom") or sc.get("exclude_from_bom"):
            why = "excluded from BOM"
        elif fp.get("board_only"):
            why = "board-only (not in schematic)"
        elif fp.get("pad_count", 0) == 0:
            why = "no pads (graphic/logo)"
        if why:
            skipped.append({"ref": ref, "reason": why})
            continue
        parts.append({"ref": ref, "value": sc.get("value") or fp.get("value", ""), "footprint": fp.get("name", ""),
                      "part": part_number(fields, part_field), "mpn": part_number(fields, "MPN"),
                      "manufacturer": field_ci(fields, "Manufacturer", "Mfr", "Mfg", "MFR"),
                      "fields": fields, "fp": fp, "in_pos": not fp.get("exclude_from_pos")})
    parts.sort(key=lambda p: natural_key(p["ref"]))
    return {"parts": parts, "skipped": skipped, "source": fps.get("source"), "copper_layers": fps.get("copper_layers"),
            "part_field": part_field}


_TPL = re.compile(r"\{([^{}:]+)(?::([^{}]*))?\}")
_SUPPLIER_FIELDS = ("LCSC", "Digi-Key", "DigiKey", "Mouser", "Farnell", "Newark", "RS", "TME", "Arrow",
                    "Supplier Part Number", "Supplier PN", "Distributor PN")


def _render(tpl: str, ctx: dict, fields: dict | None = None) -> str:
    """'{value}' / '{x:.4f}' / '{field:Digi-Key}' → text. Unknown names render empty (never an exception)."""
    def sub(m):
        name, spec = m.group(1).strip(), m.group(2)
        if name == "field":
            return field_ci(fields or {}, spec or "")
        v = ctx.get(name, "")
        if spec and isinstance(v, (int, float)) and not isinstance(v, bool):
            try:
                return format(v, spec)
            except ValueError:
                return str(v)
        return "" if v is None else str(v)
    return _TPL.sub(sub, tpl)


def write_bom(parts: list[dict], path: Path, profile=None, part_field: str | None = None) -> list[dict]:
    """Grouped BOM with the profile's columns. Parts with the same part number are one line; parts without
    one group by value + footprint (+ MPN)."""
    prof = _profile(profile)
    cols = prof["bom"]["columns"]
    part_field = part_field or prof["bom"].get("part_field") or "MPN"
    groups: dict = {}
    for p in parts:
        key = ("L", p["part"]) if p.get("part") else ("V", p["value"], p["footprint"], p.get("mpn") or "")
        groups.setdefault(key, []).append(p)
    rows = []
    for ps in groups.values():
        refs = sorted((p["ref"] for p in ps), key=natural_key)
        p0 = ps[0]
        notes = "; ".join(f"{n} {field_ci(p0['fields'], n)}" for n in dict.fromkeys((part_field,) + _SUPPLIER_FIELDS)
                          if n.lower() != "mpn" and field_ci(p0["fields"], n))
        ctx = {"value": p0["value"], "refs": ",".join(refs), "qty": len(refs), "footprint": p0["footprint"],
               "part": p0.get("part") or "", "mpn": p0.get("mpn") or "", "manufacturer": p0.get("manufacturer") or "",
               "description": field_ci(p0["fields"], "Description"), "supplier_notes": notes,
               # the supplier's own number: the part field itself unless that IS the MPN, then any distributor field
               "supplier": (p0.get("part") or "") if part_field.upper() != "MPN" else notes,
               "mount": "THT" if p0["fp"].get("tht") else "SMD"}
        rows.append({"_ctx": ctx, "_fields": p0["fields"], "_qty": len(refs), "_refs": refs})
    rows.sort(key=lambda r: natural_key(r["_refs"][0]))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([h for h, _ in cols])
        for i, r in enumerate(rows, 1):
            r["_ctx"]["item"] = i
            vals = [_render(t, r["_ctx"], r["_fields"]) for _, t in cols]
            for (h, _), v in zip(cols, vals):
                r[h] = v
            w.writerow(vals)
    return rows


def write_placement(pr, parts: list[dict], path: Path, profile=None) -> list[dict]:
    """The placement / centroid / CPL file. format 'columns': Copper computes every row (pad-centre or anchor
    position, the profile's rotation corrections). format 'kicad': KiCad's own position file, untouched."""
    prof = _profile(profile, pr)
    pl = prof["placement"]
    if pl.get("format") == "kicad":
        if path.exists():
            path.unlink()
        cmd = ["pcb", "export", "pos", "--format", "csv", "--units", pl.get("units") or "mm", "--side", pl.get("side") or "both",
               "--exclude-dnp", "-o", str(path), str(pr.pcb)]
        r = cli(cmd, timeout=300)
        if not path.exists():
            raise ToolError(f"kicad-cli wrote no position file: {(r.stderr or r.stdout).strip()[-500:]}", code="POS_FAILED")
        rows = []
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append({"ref": row.get("Ref", ""), "x": _num(row.get("PosX")), "y": _num(row.get("PosY")),
                             "rotation": _num(row.get("Rot")), "layer": row.get("Side", ""), "correction": 0.0,
                             "correction_source": None, "native": False})
        return rows
    rules = load_rotation_rules(pr, prof)
    cols = pl["columns"]
    rows = []
    for p in parts:
        if not p["in_pos"]:
            continue
        rows.append({"ref": p["ref"], "value": p["value"], "footprint": p["footprint"],
                     **placement_row(p["fp"], rules, p["fields"], prof)})
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([h for h, _ in cols])
        for r in rows:
            w.writerow([_render(t, r, None) for _, t in cols])
    return rows


# ── gerbers / drill ───────────────────────────────────────────────────────
def gerber_layers(pr, profile=None) -> list[str]:
    prof = _profile(profile, pr)
    out = []
    for name in prof["gerber"]["layers"]:
        out += copper_layers(pr.pcb) if name == "copper" else [name]
    return list(dict.fromkeys(out))


def export_gerbers(pr, out: Path, layers: list[str] | None = None, profile=None) -> list[Path]:
    prof = _profile(profile, pr)
    g = prof["gerber"]
    out.mkdir(parents=True, exist_ok=True)
    layers = layers or gerber_layers(pr, prof)
    flags = []
    if not g.get("x2"):
        flags.append("--no-x2")
    if not g.get("netlist_attributes"):
        flags.append("--no-netlist")
    if g.get("subtract_soldermask"):
        flags.append("--subtract-soldermask")
    if not g.get("protel_extensions"):
        flags.append("--no-protel-ext")
    r = cli(["pcb", "export", "gerbers", "-o", str(out) + os.sep, "-l", ",".join(layers), *flags, "--check-zones",
             "--precision", str(int(g.get("precision") or 6)), str(pr.pcb)], timeout=600)
    files = sorted(p for p in out.iterdir() if p.is_file() and p.name.startswith(pr.name) and _FAB_EXT.match(p.suffix)
                   and p.suffix.lower() not in (".drl", ".gbrjob"))
    if not files:
        raise ToolError(f"kicad-cli wrote no gerbers: {(r.stderr or r.stdout).strip()[-600:]}", code="GERBER_FAILED")
    return files


def export_drill(pr, out: Path, profile=None) -> list[Path]:
    prof = _profile(profile, pr)
    d = prof["drill"]
    out.mkdir(parents=True, exist_ok=True)
    cmd = ["pcb", "export", "drill", "-o", str(out) + os.sep, "--format", "excellon",
           "--excellon-units", "in" if str(d.get("units")).lower().startswith("in") else "mm",
           "--excellon-zeros-format", str(d.get("zeros") or "decimal"),
           "--excellon-oval-format", str(d.get("oval") or "alternate"),
           "--drill-origin", str(d.get("origin") or "absolute")]
    if d.get("separate_th", True):
        cmd.append("--excellon-separate-th")
    r = cli(cmd + [str(pr.pcb)], timeout=300)
    files = sorted(out.glob(f"{pr.name}*.drl"))
    if not files:
        raise ToolError(f"kicad-cli wrote no drill files: {(r.stderr or r.stdout).strip()[-600:]}", code="DRILL_FAILED")
    return files


def _check_gerbers(files: list[Path], layers: list[str]) -> list[str]:
    problems = []
    for f in files:
        txt = f.read_text(encoding="ascii", errors="ignore")
        if f.stat().st_size < 80 or "M02*" not in txt:
            problems.append(f"{f.name} is empty or truncated")
    if len(files) < len(layers):
        problems.append(f"expected {len(layers)} gerber layers, got {len(files)}: {[f.name for f in files]}")
    return problems


_FAB_EXT = re.compile(r"^\.(g[tb][losp]|gm\d+|g\d+|gko|gbr|gbrjob|drl)$", re.I)


def _clean_outputs(d: Path, prefix: str):
    """Remove only the files a previous Copper/KiCad plot of this board wrote
    (so a stale layer can never sneak into the zip) — never anything else."""
    if not d.is_dir():
        return
    for f in d.iterdir():
        if f.is_file() and f.name.startswith(prefix) and _FAB_EXT.match(f.suffix):
            f.unlink()


# ── gate ─────────────────────────────────────────────────────────────────
def _bom_lines(parts: list[dict]) -> list[dict]:
    """One line per unique part number (what the fab loads one feeder for): [{'part', 'refs', 'value'}]."""
    lines: dict = {}
    for p in parts:
        if p.get("part"):
            ln = lines.setdefault(p["part"], {"part": p["part"], "refs": [], "value": p.get("value") or ""})
            ln["refs"].append(p["ref"])
    for ln in lines.values():
        ln["refs"].sort(key=natural_key)
    return sorted(lines.values(), key=lambda ln: natural_key(ln["refs"][0]))


def _refs_text(refs: list[str], n: int = 6) -> str:
    return ",".join(refs[:n]) + (f",… ({len(refs)})" if len(refs) > n else "")


_STOCK_WORDS = {"out_of_stock": "OUT OF STOCK (need {need})", "insufficient": "{stock} in stock, need {need}",
                "not_listed": "not listed by the supplier"}


def stock_finding(st: dict, short: str = "the fab") -> dict | None:
    """The gate's 'stock' warning from sourcing.stock_check(): affected refs, one entry per short part with its
    parts_alternatives fix. None when every checked part is in stock."""
    bad = st.get("short") or []
    if not bad:
        return None
    b = st.get("boards") or 1
    what = "; ".join(f"{x['part']} ({_refs_text(x['refs'], 4)}): " + _STOCK_WORDS[x["status"]].format(**x) for x in bad[:8])
    more = f" (+{len(bad) - 8} more)" if len(bad) > 8 else ""
    return {"code": "stock", "severity": "warning",
            "message": f"{len(bad)} BOM part(s) short of stock for {b} board(s) — {what}{more}",
            "refs": [r for x in bad for r in x["refs"]],
            "parts": [{k: x[k] for k in ("part", "refs", "value", "status", "stock", "need", "fix")} for x in bad[:40]],
            "source": st.get("source"),
            "fix": f"{bad[0]['fix']} → sch_apply set_field on {_refs_text(bad[0]['refs'], 4)} with the substitute, then pcb_sync"
                   f" — or waive=['stock'] if you supply / hand-solder these parts or {short} restocks before you order"}


def gate(pr, waive: set, parts: list[dict], stats: dict, run_checks: bool = True, profile=None,
         part_label: str = "part number", check_stock: bool = False, boards: int | None = None,
         part_field: str | None = None, progress=None) -> dict:
    """What refuses the export. A profile's `blocks` list decides which findings block; the rest are
    reported as `advisories` (e.g. unsourced parts for a fab that sources by description).
    check_stock adds the sourcing check (only when nothing blocks): parts with zero / too little stock or not
    listed at all become one `warnings` entry (code 'stock'; `waived` when acknowledged). It never blocks and
    never fails offline: `stock` then says what was not checked."""
    from .verify import run_drc, run_erc
    prof = _profile(profile, pr)
    blocks = set(prof.get("blocks") or [])
    blockers, waived, advisories, checks = [], [], [], {}

    def add(item):
        code = item["code"]
        if code not in blocks:
            if code == "parts":      # this fab does not need a number per part: nothing to waive
                item = dict(item, fix="optional for this fab profile: parts_source_bom() fills in what the parts providers know")
            advisories.append(item)
            return
        (waived if code in waive else blockers).append(item)

    if run_checks:
        if pr.sch.exists():
            e = run_erc(pr)
            checks["erc"] = {"errors": e["errors"], "warnings": e["warnings"]}
            if e["errors"]:
                add({"code": "erc", "message": f"ERC has {e['errors']} error(s)", "fix": "verify(what=['erc']) and fix the findings",
                     "top": [v["message"] for v in e["violations"] if v["severity"] == "error"][:5]})
        d = run_drc(pr, refill=True, parity=pr.sch.exists())
        checks["drc"] = {"errors": d["errors"], "warnings": d["warnings"], "unconnected": d["unconnected"], "parity": d["parity"]}
        if d["errors"]:
            add({"code": "drc", "message": f"DRC has {d['errors']} error(s)", "fix": "verify(what=['drc']) and fix the findings",
                 "top": [v["message"] for v in d["violations"] if v["severity"] == "error" and v.get("section") == "violations"][:5]})
        if d["unconnected"]:
            add({"code": "unrouted", "message": f"{d['unconnected']} unrouted connection(s) on nets {d['unrouted_nets'][:10]}",
                 "fix": "pcb_route() to autoroute the rest (pcb_pour(net='GND') for ground planes), then verify"})
        if d["parity"]:
            add({"code": "parity", "message": f"{d['parity']} schematic↔board parity issue(s)", "fix": "pcb_sync()"})
    missing = [p["ref"] for p in parts if not p.get("part")]
    if missing:
        add({"code": "parts", "message": f"{len(missing)} assembled part(s) with no {part_label}: {', '.join(missing[:20])}",
             "fix": "parts_source_bom() — or waive=['parts'] for a bare-PCB order / parts you hand-solder"})
    if stats.get("_soldered_footprints") == 0:
        add({"code": "empty", "message": "the board has no footprints with pads — nothing would be fabricated but a blank outline",
             "fix": "pcb_sync() to bring the schematic's parts onto the board"})
    if stats.get("available") and stats.get("has_outline") is False:
        add({"code": "outline", "message": "board has no closed Edge.Cuts outline", "fix": "pcb_setup(width=…, height=…)"})
    warnings, stock = [], None
    if check_stock and parts and not blockers:
        from ..parts import sourcing
        lines = _bom_lines(parts)
        if lines:
            try:
                stock = sourcing.stock_check(pr, lines, boards or sourcing.DEFAULT_BOARDS, part_field, progress=progress)
            except Exception as e:  # noqa: BLE001 — a sourcing problem must never cost the user the fab pack
                stock = {"boards": boards or sourcing.DEFAULT_BOARDS, "lines": len(lines), "checked": 0, "short": [],
                         "unknown": [], "classes": {}, "unchecked": [ln["part"] for ln in lines],
                         "not_checked": f"stock check failed ({type(e).__name__}: {e})"}
            f = stock_finding(stock, prof.get("short") or prof["label"])
            if f and "stock" in waive:
                waived.append({k: f[k] for k in ("code", "message", "refs")})
            elif f:
                warnings.append(f)
    return {"blockers": blockers, "waived": waived, "advisories": advisories, "warnings": warnings, "checks": checks,
            "stock": stock}


# ── fab_export ───────────────────────────────────────────────────────────
def fab_export(pr, profile: str | None = None, out_dir: str | None = None, waive: list[str] | None = None,
               layers: list[str] | None = None, assembly: bool | None = None, run_checks: bool = True, progress=None,
               check_stock: bool = True, boards: int | None = None) -> dict:
    """The order pack for one fab profile (default: the project's configured profile, 'jlcpcb' out of the box).
    check_stock: ask the parts provider whether every BOM part is in stock for `boards` boards (default 5) and
    report shortfalls as a warning; offline it says stock was not checked."""
    prof = _profile(profile, pr)
    if not pr.pcb.exists():
        raise ToolError(f"no board at {pr.pcb}", code="NO_PCB", fix="pcb_sync() to create the board from the schematic")
    wv = {w.strip().lower() for w in (waive or [])}
    if "all" in wv:
        wv = set(WAIVABLE)
    unknown = wv - WAIVABLE
    if unknown:
        raise ToolError(f"unknown waive code(s) {sorted(unknown)}; valid: {sorted(WAIVABLE)} or 'all'", code="BAD_ARGUMENT")
    offered = bool(prof["assembly"].get("offered", True))
    if assembly is None:
        assembly = offered
    _p = progress or (lambda f, m="": None)
    _p(0.05, "reading board")
    out = Path(out_dir) if out_dir else _out_dir(pr) / "fab"
    out.mkdir(parents=True, exist_ok=True)
    stats = board_stats(pr)
    fps = board_footprints(pr)
    stats["_soldered_footprints"] = sum(1 for f in fps["footprints"] if f.get("pad_count"))
    part_field = part_field_for(pr, prof)
    part_label = prof["bom"].get("part_label") or "part number"
    if not prof["bom"].get("part_field"):
        part_label = {"LCSC": "LCSC number", "MPN": "MPN"}.get(part_field.upper(), f"'{part_field}' field")
    asm = assembly_parts(pr, fps, part_field)
    parts = asm["parts"]
    _p(0.15, "running ERC/DRC gate")
    g = gate(pr, wv, parts if assembly else [], stats, run_checks, prof, part_label,
             check_stock=bool(check_stock and assembly), boards=boards, part_field=part_field,
             progress=lambda f, m="": _p(0.3 + 0.14 * f, m))
    if g["blockers"]:
        raise ToolError(f"fab_export ({prof['label']}) refused for project {pr.name} ({pr.dir}): the design is not ready to order — " +
                        "; ".join(b["message"] for b in g["blockers"]),
                        code="FAB_BLOCKED",
                        fix=g["blockers"][0]["fix"] + f"  (or waive={json.dumps([b['code'] for b in g['blockers']])} to export anyway)",
                        data={"project": pr.name, "profile": prof["name"], "blockers": g["blockers"], "checks": g["checks"]})
    naming = {k: v.replace("{name}", pr.name).replace("{profile}", prof["name"]) for k, v in prof["naming"].items()}
    _p(0.45, "plotting gerbers")
    gdir = out / naming["gerber_dir"]
    _clean_outputs(gdir, pr.name)
    lay = layers or gerber_layers(pr, prof)
    gfiles = export_gerbers(pr, gdir, lay, prof)
    _p(0.6, "drill files")
    dfiles = export_drill(pr, gdir, prof)
    problems = _check_gerbers(gfiles, lay)
    for f in dfiles:
        if f.stat().st_size < 20:
            problems.append(f"{f.name} is empty")
    pth = [f for f in dfiles if "NPTH" not in f.name.upper()]
    if not pth:
        problems.append("no PTH drill file")
    result = {"project": pr.name, "profile": prof["name"], "fab": prof["label"], "gerber_dir": str(gdir), "layers": lay,
              "copper_layers": len([x for x in lay if x.endswith(".Cu")])}
    extra_zip: list[Path] = []
    if prof["gerber"].get("job_file"):
        extra_zip += sorted(gdir.glob(f"{pr.name}*.gbrjob"))          # the Gerber X2 job file (stackup, file functions)
    if assembly:
        _p(0.75, "BOM + placement")
        bom_path = out / naming["bom"]
        pos_path = out / naming["placement"]
        bom = write_bom(parts, bom_path, prof, part_field)
        pos = write_placement(pr, parts, pos_path, prof)
        bom_refs = {r for row in bom for r in row["_refs"]}
        pos_refs = {r["ref"] for r in pos}
        if not bom_path.stat().st_size or (parts and not pos_path.stat().st_size):
            raise ToolError("BOM/placement file is empty", code="FAB_VERIFY_FAILED")
        corrected = [{"ref": r["ref"], "correction": r["correction"], "by": r["correction_source"]}
                     for r in pos if r["correction_source"]]
        unsourced = [p["ref"] for p in parts if not p.get("part")]
        result.update({
            "bom": str(bom_path), "placement": str(pos_path), "part_field": part_field,
            "bom_lines": len(bom), "placements": len(pos),
            "bom_preview": [{k: v for k, v in r.items() if not k.startswith("_")} for r in bom[:8]],
            "placement_preview": [{k: r[k] for k in ("ref", "x", "y", "layer", "rotation")} for r in pos[:6]],
            "rotation_corrections": corrected[:40],
            # footprints native to the assembly service: written with the board rotation unchanged
            "native_orientation": [r["ref"] for r in pos if r.get("native")][:60],
            "not_placed": asm["skipped"][:40],
            "bom_without_placement": sorted(bom_refs - pos_refs, key=natural_key),
            "placement_source": "kicad-cli pos" if prof["placement"].get("format") == "kicad" else asm["source"],
        })
        if unsourced:
            result["unsourced"] = unsourced[:60]
        if prof["zip"].get("include_assembly"):
            extra_zip += [bom_path, pos_path]
    zpath = out / naming["zip"]
    folder = str(prof["zip"].get("folder") or "").replace("{name}", pr.name).replace("{profile}", prof["name"]).strip("/\\")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in gfiles + dfiles + extra_zip:
            z.write(f, f"{folder}/{f.name}" if folder else f.name)
    with zipfile.ZipFile(zpath) as z:
        bad = z.testzip()
        names = z.namelist()
    if bad:
        problems.append(f"zip member {bad} is corrupt")
    if problems:
        raise ToolError("fab outputs failed verification: " + "; ".join(problems), code="FAB_VERIFY_FAILED",
                        data={"files": names})
    result.update({"zip": str(zpath), "zip_files": names})
    result["summary"] = _summary(pr, stats, parts if assembly else [], result, prof)
    if assembly and parts:
        st = _stock_report(g, parts, prof, result["summary"]["order_notes"], bool(check_stock))
        if st:
            result["stock"] = st
    if assembly and not offered:
        result["summary"]["order_notes"].insert(0, f"{prof['label']} does not assemble boards: the BOM and placement file "
                                                   f"are for your own use")
    from ..project import is_open_in_kicad
    if is_open_in_kicad(pr.pcb):
        result["summary"]["order_notes"].insert(0, f"{pr.pcb.name} is open in KiCad: this pack was made from the last SAVED "
                                                   f"board — save in KiCad and re-run fab_export if you changed anything there")
    result["checks"] = g["checks"]
    result["waived"] = g["waived"]
    if g["warnings"]:
        result["warnings"] = g["warnings"]
    if g["advisories"]:
        result["advisories"] = g["advisories"]
    _p(1.0, "done")
    return result


def _stock_report(g: dict, parts: list[dict], prof: dict, notes: list, asked: bool) -> dict | None:
    """The compact `stock` block of the result; puts the one sentence the user must read at the top of the order
    notes (short parts, or that stock was NOT checked and why)."""
    st = g.get("stock")
    short = prof.get("short") or prof["label"]
    if st is None:
        n = len(_bom_lines(parts))
        if not n:
            return None
        notes.insert(0, f"stock was NOT checked (check_stock=false): confirm every BOM part is in stock at {short} "
                        f"before ordering (parts_get shows live stock)")
        return {"lines": n, "checked": 0, "unchecked": n, "short": 0,
                "not_checked": "check_stock=false" if not asked else "not run"}
    n_short, n_un = len(st["short"]), len(st["unchecked"])
    if n_un:                # shortfalls are in `warnings` / `waived` and in the tool's first line: not repeated here
        notes.insert(0, f"stock was NOT checked for {n_un} of {st['lines']} BOM part(s) ({st['not_checked']}): confirm they "
                        f"are in stock at {short} before ordering (parts_get shows live stock)")
    out = {"boards": st["boards"], "lines": st["lines"], "checked": st["checked"], "unchecked": n_un, "short": n_short}
    if st["unknown"]:
        out["unknown"] = st["unknown"][:40]
    if st.get("source"):
        out["source"] = st["source"]
    if n_un:
        out.update(not_checked=st["not_checked"], unchecked_parts=st["unchecked"][:40])
    return out


def _summary(pr, stats: dict, parts: list[dict], res: dict, prof: dict) -> dict:
    lim = prof.get("limits") or {}
    short = prof.get("short") or prof["label"]
    rules = FP.rules(prof, "standard", res.get("copper_layers"))
    s = {"board_mm": [stats.get("width_mm"), stats.get("height_mm")] if stats.get("available") else None,
         "copper_layers": res.get("copper_layers"), "thickness_mm": stats.get("thickness_mm"),
         "min_track_width_mm": stats.get("min_track_width_mm"), "min_clearance_mm": stats.get("min_track_clearance_mm"),
         "min_drill_mm": stats.get("min_drill_mm"), "vias": stats.get("vias"),
         "slots": stats.get("slots")}
    notes = []
    w, h = (s["board_mm"] or [None, None])
    if w and h:
        tier = lim.get("price_tier_mm")
        if tier:
            if w <= tier[0] and h <= tier[1]:
                notes.append(f"≤{tier[0]:g}×{tier[1]:g} mm: qualifies for {short}'s cheapest prototype price tier")
            else:
                notes.append(f"{w:.1f}×{h:.1f} mm is larger than {tier[0]:g}×{tier[1]:g} mm — expect a higher board price")
        amin = lim.get("assembly_min_mm")
        if amin and (w < amin[0] or h < amin[1]):
            notes.append(f"very small board: {short} assembly needs ≥ {amin[0]:g}×{amin[1]:g} mm — panelize or add rails")
    mt = s.get("min_track_width_mm")
    if mt is not None and mt < rules["min_track"] - 1e-6:
        notes.append(f"min track {mt} mm is below {short}'s {rules['min_track']:g} mm standard capability")
    md = s.get("min_drill_mm")
    sd = lim.get("small_drill_surcharge_mm")
    if md is not None and sd and md < sd - 1e-6:
        notes.append(f"min drill {md} mm < {sd:g} mm adds {short}'s small-via surcharge")
    elif md is not None and not sd and md < rules["min_hole"] - 1e-6:
        notes.append(f"min drill {md} mm is below {short}'s {rules['min_hole']:g} mm minimum")
    n_cu = res.get("copper_layers") or 2
    if n_cu >= 4:
        sn = (lim.get("stackup_notes") or {}).get(str(n_cu))
        notes.append(f"{n_cu} layers: select the matching {short} impedance stackup at order time" + (f" ({sn})" if sn else ""))
    v = stats.get("vias") or {}
    exotic = {k: v.get(k) for k in ("blind", "buried", "micro") if v.get(k)}
    if exotic and lim.get("through_vias_only"):
        notes.append(f"board uses {exotic} vias — {short}'s standard service only drills through-vias (HDI is a special quote)")
    if parts:
        uniq: dict = {}
        for p in parts:
            if p.get("part"):
                uniq.setdefault(p["part"], p["fields"])
        by_part = {ln["part"]: ln for ln in _bom_lines(parts)}
        s.update({"assembled_parts": len(parts), "unique_parts": len(uniq), "part_field": res.get("part_field"),
                  "unsourced_parts": sum(1 for p in parts if not p.get("part")),
                  "tht_parts": sum(1 for p in parts if p["fp"].get("tht")),
                  "bottom_side_parts": sum(1 for p in parts if p["fp"].get("layer") == "bottom")})
        pc = prof.get("part_classes") or {}
        if pc.get("field"):
            from .design_review import part_class
            cls = {num: part_class(num, f, prof) for num, f in uniq.items()}
            fee = float((pc.get("fee_usd") or {}).get("extended") or 0)
            ext = sorted(n for n, c in cls.items() if c == "extended")
            unk = sorted(n for n, c in cls.items() if c == "unknown")
            s.update({"extended_parts": len(ext), "class_unknown": len(unk), "estimated_extended_fee_usd": len(ext) * fee})
            n_basic = sum(1 for c in cls.values() if c == "basic")
            n_pref = sum(1 for c in cls.values() if c == "preferred")
            label = pc.get("fee_label") or "Extended-part fee"
            # the fee picture in one place: what is free, what is charged, and which references cause it
            s["assembly_fees"] = {
                "basic": n_basic, "preferred": n_pref, "extended": len(ext), "unknown": len(unk),
                "fee_per_extended_usd": fee, "estimated_usd": len(ext) * fee,
                "extended_refs": {n: by_part[n]["refs"] for n in ext[:40]}}
            if uniq:
                line = (f"{len(uniq)} unique parts: {n_basic} Basic" + (f" + {n_pref} Preferred" if n_pref else "")
                        + f" (no per-part fee), {len(ext)} Extended")
                if ext:
                    line += (f" × ${fee:g} = ${len(ext) * fee:g} {label} per order; parts_alternatives(target=…) "
                             f"looks for a Basic substitute")
                notes.append(line)
            if unk:
                notes.append(f"Basic/Extended class unknown for {len(unk)} part(s) — parts_get shows the class; "
                             f"Extended costs ${fee:g} each")
        if s["unsourced_parts"] and "parts" not in (prof.get("blocks") or []):
            notes.append(f"{s['unsourced_parts']} part(s) have no {res.get('part_field')} — the BOM lists them by value + "
                         f"footprint; parts_source_bom() fills in what your parts providers know")
        if s["bottom_side_parts"]:
            notes.append(f"{s['bottom_side_parts']} parts on the bottom: needs double-sided assembly (or move them top)")
        if s["tht_parts"]:
            notes.append(f"{s['tht_parts']} through-hole part(s): " + (lim.get("tht_note") or "check how your assembler handles THT"))
    notes += list(prof.get("order_notes") or [])
    s["order_notes"] = notes
    return s


# ── generic export ────────────────────────────────────────────────────────
_DESIGN_SUFFIXES = {".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_prl", ".kicad_dru", ".kicad_sym", ".kicad_mod",
                    ".kicad_wks", ".kicad_jobset"}

EXPORT_KINDS = ["pdf_schematic", "svg", "pcb_pdf", "step", "glb", "vrml", "ipc2581", "odb", "netlist", "bom", "pos",
                "drill", "gerbers", "render3d", "stats", "ipcd356"]


def export(pr, kind: str, out: str | None = None, layers: list[str] | None = None, view: str = "iso") -> dict:
    if kind not in EXPORT_KINDS:
        raise ToolError(f"unknown export kind '{kind}'", code="BAD_ARGUMENT", fix=f"one of {EXPORT_KINDS}")
    needs_pcb = kind not in ("pdf_schematic", "svg", "netlist", "bom")
    if needs_pcb and not pr.pcb.exists():
        raise ToolError(f"{kind} needs a board and {pr.pcb.name} does not exist", code="NO_PCB", fix="pcb_sync()")
    if not needs_pcb and not pr.sch.exists():
        raise ToolError(f"{kind} needs a schematic", code="NO_SCHEMATIC", fix="project_open(path=...)")
    base = _out_dir(pr) / "export"
    base.mkdir(parents=True, exist_ok=True)
    ext = {"pdf_schematic": ".pdf", "pcb_pdf": ".pdf", "step": ".step", "glb": ".glb", "vrml": ".wrl", "ipc2581": ".xml",
           "odb": ".zip", "netlist": ".net", "bom": ".csv", "pos": "-pos.csv", "render3d": ".png", "stats": "-stats.json",
           "ipcd356": ".d356"}
    is_dir = kind in ("svg", "drill", "gerbers")
    default_name = f"{pr.name}{('-' + kind) if kind in ('pcb_pdf', 'odb') else ''}{ext.get(kind, '')}"
    target = Path(os.path.expanduser(out)) if out else (base / kind if is_dir else base / default_name)
    if out:
        if not target.is_absolute():
            target = pr.dir / target
        if target.suffix.lower() in _DESIGN_SUFFIXES or target.name.lower() in ("fp-lib-table", "sym-lib-table"):
            raise ToolError(f"refusing to write a {kind} export over the design file {target.name}", code="BAD_ARGUMENT",
                            fix=f"pass a file name like {default_name!r} (or omit out for {base})")
        if not is_dir and target.is_dir():
            target = target / default_name          # a folder was given for a single-file export
        if is_dir and target.exists() and not target.is_dir():
            raise ToolError(f"{kind} export needs a folder, but {target} is a file", code="BAD_ARGUMENT",
                            fix="pass a folder path (or omit out)")
    if not is_dir:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
    src_sch, src_pcb = str(pr.sch), str(pr.pcb)
    cmd = None
    if kind == "pdf_schematic":
        cmd = ["sch", "export", "pdf", "-o", str(target), src_sch]
    elif kind == "svg":
        target.mkdir(parents=True, exist_ok=True)
        cmd = ["sch", "export", "svg", "-o", str(target), src_sch]
    elif kind == "pcb_pdf":
        lay = layers or ["F.Cu", "B.Cu", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Edge.Cuts", "F.Fab"]
        cmd = ["pcb", "export", "pdf", "-l", ",".join(lay), "--mode-multipage", "--check-zones", "-o", str(target), src_pcb]
    elif kind == "step":
        cmd = ["pcb", "export", "step", "--subst-models", "-f", "-o", str(target), src_pcb]
    elif kind == "glb":
        cmd = ["pcb", "export", "glb", "--subst-models", "-f", "--include-tracks", "--include-pads", "--include-silkscreen",
               "--include-soldermask", "-o", str(target), src_pcb]
    elif kind == "vrml":
        cmd = ["pcb", "export", "vrml", "-f", "-o", str(target), src_pcb]
    elif kind == "ipc2581":
        pf = part_field_for(pr, FP.active(pr))
        cmd = ["pcb", "export", "ipc2581", "--bom-col-dist-pn", pf, "--bom-col-dist", pf, "--bom-col-mfg-pn", "MPN",
               "--bom-col-mfg", "Manufacturer", "-o", str(target), src_pcb]
    elif kind == "odb":
        cmd = ["pcb", "export", "odb", "--check-zones", "-o", str(target), src_pcb]
    elif kind == "netlist":
        cmd = ["sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(target), src_sch]
    elif kind == "bom":
        pf = part_field_for(pr, FP.active(pr))
        sup = [] if pf.upper() == "MPN" else [pf]          # the project's supplier part-number field (LCSC by default)
        cols = ["Reference", "Value", "Footprint", "${QUANTITY}", *sup, "MPN", "Manufacturer", "${DNP}"]
        cmd = ["sch", "export", "bom", "--fields", ",".join(cols),
               "--labels", ",".join(["Refs", "Value", "Footprint", "Qty", *sup, "MPN", "Manufacturer", "DNP"]),
               "--group-by", ",".join(["Value", "Footprint", *(sup or ["MPN"])]),
               "--ref-range-delimiter", "", "-o", str(target), src_sch]
    elif kind == "pos":
        cmd = ["pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both", "--exclude-dnp",
               "-o", str(target), src_pcb]
    elif kind == "stats":
        cmd = ["pcb", "export", "stats", "--format", "json", "--units", "mm", "-o", str(target), src_pcb]
    elif kind == "ipcd356":
        cmd = ["pcb", "export", "ipcd356", "-o", str(target), src_pcb]
    if kind == "gerbers":
        _clean_outputs(target, pr.name)
        files = export_gerbers(pr, target, layers)
        probs = _check_gerbers(files, layers or gerber_layers(pr))
        if probs:
            raise ToolError("; ".join(probs), code="EXPORT_VERIFY_FAILED")
        return _files_result(kind, target, files)
    if kind == "drill":
        files = export_drill(pr, target)
        return _files_result(kind, target, files)
    if kind == "render3d":
        from .. import render
        im = render.pcb_3d(pr.pcb, target.parent, view=view)
        p = Path(im.path)
        if out and p != target:
            shutil.move(str(p), str(target))
            p = target
        return _files_result(kind, p.parent, [p], image=str(p))
    r = cli(cmd, timeout=900)
    if is_dir:
        files = sorted(f for f in target.iterdir() if f.is_file())
    else:
        files = [target] if target.exists() else []
    files = [f for f in files if f.stat().st_size > 0]
    if not files:
        raise ToolError(f"kicad-cli {kind} export produced no output (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-700:]}",
                        code="EXPORT_FAILED")
    return _files_result(kind, target if is_dir else target.parent, files, log=(r.stderr or "").strip()[-300:] or None)


def _files_result(kind, where: Path, files: list[Path], **extra) -> dict:
    return {"kind": kind, "path": str(where if len(files) != 1 else files[0]),
            "files": [{"name": f.name, "bytes": f.stat().st_size} for f in files],
            "total_bytes": sum(f.stat().st_size for f in files), **{k: v for k, v in extra.items() if v}}
