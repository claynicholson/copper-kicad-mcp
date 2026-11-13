"""C-number → KiCad symbol + footprint (+3D) usable in this project.

Policy (``prefer='auto'``): **KiCad's own symbol whenever a correct one exists**, a symbol converted from the
supplier drawing only when none does. The choice is made by :mod:`copper_kicad.parts.stock`:

  1. two-terminal chip parts by kind + package (Device:R/C/L/LED/D… + the IPC footprint) — right by construction;
  2. a stock symbol named after the MPN (exact, packaging suffix, ``x`` wildcard, family prefix, ``extends``
     children) or a generic one picked from the part's real pinout (Q_NMOS_GSD, Crystal_GND24, Conn_01x06 …),
     accepted only after a pin-by-pin check against the supplier's pin data: numbers cover the footprint pads
     exactly, names agree, the footprint's pads lie on the supplier's pads with the same numbering;
  3. otherwise **EasyEDA** → project-local ``copper-lcsc`` libraries (``libs/copper-lcsc.kicad_sym``,
     ``libs/copper-lcsc.pretty``, ``libs/copper-lcsc.3dshapes``) registered in the project's sym/fp-lib-table
     with ``${KIPRJMOD}`` URIs, then verified by loading them with kicad-cli.

A stock symbol may be paired with the converted footprint when no stock footprint fits the package (multi-package
symbols such as LM358, generic connectors). Every result says which symbol was chosen, whether symbol and
footprint are stock or converted, and why (``symbol_source``, ``footprint_source``, ``why``, ``selection``).
"""
from __future__ import annotations

import functools
import json
import re
import shutil
import tempfile
from pathlib import Path

from .. import sexpr
from ..sexpr import Sym
from ..kicad import env
from ..mcp.registry import ToolError
from ..project import Project, atomic_write
from . import common as C
from . import easyeda as E

LIB = "copper-lcsc"
SETUP_FEE = C.JLC_SETUP_FEE_USD

# ── stock mapping for passives / discretes by package ────────────────────
_CHIP = {"0201": "0603", "0402": "1005", "0603": "1608", "0805": "2012", "1206": "3216", "1210": "3225",
         "1812": "4532", "2010": "5025", "2512": "6332"}
_DIODE_FP = {"SOD-123": "D_SOD-123", "SOD-123F": "D_SOD-123F", "SOD-123FL": "D_SOD-123F", "SOD-323": "D_SOD-323",
             "SOD-323F": "D_SOD-323F", "SOD-523": "D_SOD-523", "SOD-923": "D_SOD-923", "SMA": "D_SMA", "SMB": "D_SMB",
             "SMC": "D_SMC", "SMAF": "D_SMA", "MINIMELF": "D_MiniMELF", "LL-34": "D_MiniMELF", "SOD-80": "D_MiniMELF"}
_DIODE_SYM = {"diode": "D", "rectifier": "D", "schottky": "D_Schottky", "zener": "D_Zener", "tvs": "D_TVS", "esd": "D_TVS"}


def stock_candidate(rec: dict) -> dict | None:
    """Package/category rule → {'lib_id','footprint','why'} (unverified)."""
    kind = rec.get("kind")
    pk = C.norm_pkg(rec.get("package"))
    mfr = rec.get("mfr") or ""
    desc = (rec.get("description") or "").lower()
    if pk in _CHIP:
        metric = _CHIP[pk]
        if kind == "resistor" and "network" not in (rec.get("subcategory") or "").lower():
            return {"lib_id": "Device:R", "footprint": f"Resistor_SMD:R_{pk}_{metric}Metric", "why": "chip resistor"}
        if kind == "capacitor":
            return {"lib_id": "Device:C", "footprint": f"Capacitor_SMD:C_{pk}_{metric}Metric", "why": "chip MLCC"}
        if kind == "inductor":
            return {"lib_id": "Device:L", "footprint": f"Inductor_SMD:L_{pk}_{metric}Metric", "why": "chip inductor"}
        if kind == "ferrite":
            return {"lib_id": "Device:FerriteBead_Small", "footprint": f"Inductor_SMD:L_{pk}_{metric}Metric", "why": "chip ferrite bead"}
        if kind == "led" and "rgb" not in desc and not re.search(r"\b(bi-?color|dual)\b", desc):
            return {"lib_id": "Device:LED", "footprint": f"LED_SMD:LED_{pk}_{metric}Metric", "why": "chip LED (pin 1 = K)"}
        if kind == "fuse":
            return {"lib_id": "Device:Polyfuse" if "ptc" in desc or "resettable" in desc else "Device:Fuse",
                    "footprint": f"Fuse:Fuse_{pk}_{metric}Metric", "why": "chip fuse"}
        if kind in ("diode", "zener", "schottky", "tvs", "esd") and pk in ("0402", "0603", "0805"):
            return None
    if kind in _DIODE_SYM and pk in _DIODE_FP:
        # single-diode two-terminal packages only (SOT-23 diodes have varied pinouts)
        if re.search(r"\b(dual|series|common|array|bidirectional)\b", desc) and kind not in ("tvs", "esd"):
            return None
        sym = _DIODE_SYM[kind]
        if kind in ("tvs", "esd"):
            # Device:D_TVS is the BIDIRECTIONAL symbol (pins A1/A2, no polarity). A unidirectional part needs a
            # symbol that shows its cathode; a part whose direction is not stated gets no rule candidate at all.
            uni = tvs_unidirectional(rec)
            if uni is None:
                return None
            if uni:
                return {"lib_id": "Device:D_Zener", "footprint": f"Diode_SMD:{_DIODE_FP[pk]}",
                        "why": f"unidirectional TVS in {pk}: drawn as Device:D_Zener (pin 1 = K) — Device:D_TVS is the "
                               f"bidirectional symbol and would not show the polarity"}
            return {"lib_id": "Device:D_TVS", "footprint": f"Diode_SMD:{_DIODE_FP[pk]}", "why": f"bidirectional TVS in {pk}"}
        return {"lib_id": f"Device:{sym}", "footprint": f"Diode_SMD:{_DIODE_FP[pk]}", "why": f"{kind} in {pk} (pin 1 = K)"}
    if kind == "tantalum":
        # 'CASE-A-3216-18(mm)' → Capacitor_Tantalum_SMD:CP_EIA-3216-18_Kemet-A (pad 1 = +, as Device:C_Polarized pin 1)
        m = re.search(r"(?<!\d)(\d{4})-(\d{2})(?!\d)", rec.get("package") or "")
        if m:
            fp = _tantalum_fp(m.group(1), m.group(2))
            if fp:
                return {"lib_id": "Device:C_Polarized", "footprint": fp, "why": f"tantalum, EIA {m.group(1)}-{m.group(2)} (pin 1 = +)"}
    if kind == "electrolytic":
        m = re.search(r"D(\d+(?:\.\d+)?)\s*x\s*L(\d+(?:\.\d+)?)", rec.get("package") or "", re.I)
        if m and "smd" in (rec.get("package") or "").lower():
            d, l = m.group(1), m.group(2)
            return {"lib_id": "Device:C_Polarized", "footprint": f"Capacitor_SMD:CP_Elec_{_mmfmt(d)}x{_mmfmt(l)}",
                    "why": "SMD aluminium electrolytic"}
    return None


def tvs_unidirectional(rec: dict) -> bool | None:
    """TVS / ESD diode direction: catalogue text first, else the usual MPN convention (SMAJ5.0A uni, SMAJ5.0CA bi).
    None = not stated."""
    t = (rec.get("description") or "").lower()
    if re.search(r"\bbi-?directional\b", t):
        return False
    if re.search(r"\buni-?directional\b", t):
        return True
    m = str(rec.get("mfr") or "").upper()
    if re.fullmatch(r"(SMF|SMAJ|SMBJ|SMCJ|P4SM[AB]|P6SM[AB]|1\.5SM[CB]|SMA6J|SMB6J)[\d.]+(C?A)(?:[-/].*)?", m):
        return not re.search(r"CA(?:[-/].*)?$", m)
    return None


@functools.lru_cache(maxsize=64)
def _tantalum_fp(size: str, height: str) -> str | None:
    try:
        L = env.find_lib("fp", "Capacitor_Tantalum_SMD")
    except ToolError:
        return None
    hits = sorted(f.stem for f in Path(L.path).glob(f"CP_EIA-{size}-{height}_*.kicad_mod") if "HandSolder" not in f.stem)
    return f"Capacitor_Tantalum_SMD:{hits[0]}" if len(hits) == 1 else None


def _mmfmt(v: str) -> str:
    f = float(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


# ── stock symbol index (MPN → Library:Name) ──────────────────────────────
@functools.lru_cache(maxsize=1)
def _sym_index() -> dict:
    try:
        inst = env.kicad()
        ver = inst.full_version or inst.version
    except ToolError:
        return {"exact": {}, "wild": []}
    hit = C.cache_get("symidx", f"v2-{ver}", ttl=30 * 86400)
    if hit:
        return hit
    exact: dict[str, list] = {}
    wild = []
    for lib in env.lib_tables("sym"):
        if lib.scope == "project" or not lib.exists or not lib.path.endswith(".kicad_sym"):
            continue
        try:
            txt = Path(lib.path).read_text(encoding="utf-8")
        except OSError:
            continue
        for m in re.finditer(r'^\t\(symbol "([^"]+)"', txt, re.M):
            n = m.group(1)
            exact.setdefault(_alnum(n), []).append(f"{lib.name}:{n}")
            if re.search(r"[A-Z0-9]x", n) and "x" in n:
                wild.append([f"{lib.name}:{n}", "^" + re.escape(n.upper()).replace("X", "[A-Z0-9]") + "$", n])
    d = {"exact": exact, "wild": [w for w in wild if w[2].count("x") <= 6]}
    C.cache_put("symidx", f"v2-{ver}", d)
    return d


def _alnum(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


_SUFFIX_OK = re.compile(r"^(?:[-/#]?(?:TR|T|R|RL|PBF|NOPB|G4|E3|13|7|TA|TB|CT|ND|HF)|[-/#]G)*$", re.I)


def find_stock_symbols(mpn: str) -> list[str]:
    idx = _sym_index()
    key = _alnum(mpn)
    out = list(idx["exact"].get(key, []))
    if not out:
        # trailing packaging/grade suffixes: '…-TR', '/NOPB', 'RL' …
        raw = str(mpn or "").upper()
        for cut in range(1, 6):
            if len(raw) - cut < 4:
                break
            head, tail = raw[:-cut], raw[-cut:]
            if _SUFFIX_OK.match(tail):
                out = list(idx["exact"].get(_alnum(head), []))
                if out:
                    break
    if not out:
        up = re.sub(r"[^A-Z0-9_.+-]", "", str(mpn or "").upper())
        for lib_id, rx, _ in idx["wild"]:
            if re.match(rx, up) or re.match(rx, re.sub(r"[-/#].*$", "", up)):
                out.append(lib_id)
    return out


# ── reading stock libs ───────────────────────────────────────────────────
@functools.lru_cache(maxsize=64)
def _sym_file(path: str):
    return sexpr.loads(Path(path).read_text(encoding="utf-8"))


def expand_pin_number(num: str) -> list[str]:
    """KiCad 10 stacked pin numbers: one pin numbered '[1,15,38,39]' or '[1-4]' stands for all of those pads."""
    m = re.fullmatch(r"\[(.+)\]", str(num))
    if not m:
        return [str(num)]
    out = []
    for part in m.group(1).split(","):
        r = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", part)
        if r:
            out += [str(k) for k in range(int(r.group(1)), int(r.group(2)) + 1)]
        elif part.strip():
            out.append(part.strip())
    return out


def stock_symbol(lib_id: str, project_dir: Path | None = None) -> dict | None:
    """{'pins': {num: name}, 'types': {num: electrical type}, 'units': {unit: [nums]}, 'footprint': default,
    'fp_filters': [...], 'ref': prefix, …} resolving `extends`. Stacked pin numbers are expanded, so 'pins' has one
    entry per footprint pad number. Unit 0 holds the pins common to every unit."""
    lib, _, name = lib_id.partition(":")
    try:
        L = env.find_lib("sym", lib, project_dir)
    except ToolError:
        return None
    tree = _sym_file(L.path)
    syms = {s[1]: s for s in sexpr.find_all(tree, "symbol")}
    s = syms.get(name)
    if s is None:
        return None
    props = {p[1]: p[2] for p in sexpr.find_all(s, "property") if len(p) > 2}
    chain = [s]
    while sexpr.find(chain[-1], "extends") is not None and len(chain) < 5:
        parent = syms.get(sexpr.value(chain[-1], "extends"))
        if parent is None:
            break
        chain.append(parent)
        for p in sexpr.find_all(parent, "property"):
            if len(p) > 2:
                props.setdefault(p[1], p[2])
    pins, types, units = {}, {}, {}
    stacked = False
    for sub in sexpr.find_all(chain[-1], "symbol"):
        m = re.fullmatch(r".*_(\d+)_(\d+)", str(sub[1]))
        unit, style = (int(m.group(1)), int(m.group(2))) if m else (1, 1)
        for pin in sexpr.find_all(sub, "pin"):
            nm = str(sexpr.value(pin, "name", ""))
            nm = "" if nm == "~" else nm
            raw = str(sexpr.value(pin, "number", ""))
            nums = expand_pin_number(raw)
            stacked = stacked or nums != [raw]
            for num in nums:
                if num not in pins or pins[num] == "":
                    pins[num] = nm
                types.setdefault(num, str(pin[1]) if len(pin) > 1 else "")
                if style <= 1 and num not in units.setdefault(unit, []):
                    units[unit].append(num)
    return {"pins": pins, "types": types, "units": units, "unit_count": max([u for u in units if u > 0] or [1]),
            "stacked": stacked, "footprint": props.get("Footprint", ""),
            "fp_filters": str(props.get("ki_fp_filters", "")).split(), "ref": props.get("Reference", "U"),
            "description": props.get("Description", ""), "datasheet": props.get("Datasheet", "")}


def footprint_pads(fp_id: str, project_dir: Path | None = None) -> set[str] | None:
    lib, _, name = fp_id.partition(":")
    try:
        L = env.find_lib("fp", lib, project_dir)
    except ToolError:
        return None
    f = Path(L.path) / f"{name}.kicad_mod"
    if not f.exists():
        return None
    txt = f.read_text(encoding="utf-8", errors="replace")
    return {m.group(1) for m in re.finditer(r'\(pad "([^"]*)"', txt) if m.group(1)}


def _pkg_pin_count(pkg: str) -> int | None:
    s = (pkg or "").upper()
    s = re.sub(r"SOT-?(23|223|89|323|363|353|563|143|343|523|723|883|25)", "SOTX", s)
    s = re.sub(r"(SOD|TO|DO)-?\d+\w*", "X", s)
    m = re.search(r"(?:SOTX|SOIC|SOP|SO|SSOP|TSSOP|MSOP|QFN|DFN|LQFP|TQFP|QFP|LQFN|WQFN|VQFN|UQFN|HTSSOP|ESOP|BGA|LGA|WSON|SON|DIP)-?(\d+)", s)
    return int(m.group(1)) if m else None


def verify_stock(lib_id: str, fp_id: str, rec: dict, project_dir: Path | None) -> tuple[bool, list[str], dict]:
    """Structural check of a stock symbol + footprint pair: both exist, the symbol's pin numbers cover the
    footprint's pad numbers EXACTLY (no pin without a pad, no numbered pad without a pin), and the package
    pin-count agrees with the catalogue's. The pinout itself is checked by parts.stock against supplier data."""
    notes = []
    sym = stock_symbol(lib_id, project_dir)
    if sym is None:
        return False, [f"stock symbol {lib_id} not found in symbol libraries"], {}
    if not fp_id:
        return False, [f"{lib_id} has no default footprint"], sym
    pads = footprint_pads(fp_id, project_dir)
    if pads is None:
        return False, [f"footprint {fp_id} not found in footprint libraries"], sym
    missing = sorted(set(sym["pins"]) - pads, key=E._natkey)
    if missing:
        return False, [f"{lib_id} pins {missing} have no pad in {fp_id}"], sym
    extra = sorted(pads - set(sym["pins"]), key=E._natkey)
    if extra:
        return False, [f"{fp_id} pads {extra} have no pin in {lib_id}"], sym
    n = _pkg_pin_count(rec.get("package") or "")
    if n and n not in (len(pads), len(pads) - 1):
        return False, [f"LCSC package {rec.get('package')} has {n} pins but {fp_id} has {len(pads)} pads"], sym
    return True, notes, sym


# ── project libraries ────────────────────────────────────────────────────
def lib_paths(pr: Project) -> dict:
    d = pr.dir / "libs"
    return {"dir": d, "sym": d / f"{LIB}.kicad_sym", "pretty": d / f"{LIB}.pretty", "3d": d / f"{LIB}.3dshapes"}


def ensure_lib_tables(pr: Project) -> list[str]:
    """Register copper-lcsc in the project's sym-lib-table and fp-lib-table."""
    changed = []
    for kind, fname, uri in (("sym", "sym-lib-table", "${KIPRJMOD}/libs/" + LIB + ".kicad_sym"),
                             ("fp", "fp-lib-table", "${KIPRJMOD}/libs/" + LIB + ".pretty")):
        f = pr.dir / fname
        head = "sym_lib_table" if kind == "sym" else "fp_lib_table"
        if f.exists():
            txt = f.read_text(encoding="utf-8")
            try:
                tree = sexpr.loads(txt)
            except ValueError:
                raise ToolError(f"{fname} is not valid S-expression", code="BAD_LIB_TABLE",
                                fix=f"fix or delete {f} and retry")
            libs = sexpr.find_all(tree, "lib")
            mine = [l for l in libs if sexpr.value(l, "name") == LIB]
            if mine and sexpr.value(mine[0], "uri") == uri and sexpr.find(mine[0], "disabled") is None:
                continue
            tree = [e for e in tree if not (isinstance(e, list) and e and e[0] == "lib" and sexpr.value(e, "name") == LIB)]
        else:
            tree = [Sym(head), [Sym("version"), 7]]
        tree.append([Sym("lib"), [Sym("name"), LIB], [Sym("type"), "KiCad"], [Sym("uri"), uri], [Sym("options"), ""],
                     [Sym("descr"), "LCSC/JLCPCB parts imported by Copper (source: JLCEDA/EasyEDA Official Library)"]])
        atomic_write(f, _dump_table(tree))
        changed.append(fname)
    return changed


def _dump_table(tree) -> str:
    lines = [f"({tree[0]}"]
    for e in tree[1:]:
        if isinstance(e, list) and e and e[0] == "lib":
            lines.append("  (lib " + "".join(sexpr.dumps(x) for x in e[1:]) + ")")
        else:
            lines.append("  " + sexpr.dumps(e))
    lines.append(")")
    return "\n".join(lines) + "\n"


def _load_symlib(path: Path) -> list:
    if path.exists():
        try:
            t = sexpr.loads(path.read_text(encoding="utf-8"))
            if isinstance(t, list) and t and t[0] == "kicad_symbol_lib":
                return t
        except ValueError:
            pass
    return [Sym("kicad_symbol_lib"), [Sym("version"), E.SYM_VERSION], [Sym("generator"), "copper_kicad"],
            [Sym("generator_version"), "1.0"]]


def _existing_symbol(lib_tree, name):
    for s in sexpr.find_all(lib_tree, "symbol"):
        if s[1] == name:
            return s
    return None


# ── public API ───────────────────────────────────────────────────────────
def part_record(lcsc: str, live: bool = True, fresh: bool = False) -> dict:
    """Best available catalogue record for a C-number. The offline table is
    used when it knows the part (fast, class rarely changes) unless
    ``fresh=True``; otherwise the live JLC API."""
    from . import basic, search
    code = C.lcsc_norm(lcsc)
    rec = None if fresh else basic.by_lcsc(code)
    if rec is None and live:
        try:
            rec = search.jlc_part(code)
        except C.NetError:
            rec = None
    if rec is None and fresh:
        rec = basic.by_lcsc(code)
    return rec or {}


def resolve_lcsc(pr: Project, lcsc: str, prefer: str = "auto", with_3d: bool = True, live: bool = True,
                 overwrite: bool = False, fresh: bool = False) -> dict:
    """C-number → {'lcsc','lib_id','footprint','value','fields':{…},'basic','source', …}.

    prefer: 'auto' (KiCad's stock symbol when one verifiably fits, else the converted EasyEDA symbol), 'stock'
    (fail if no stock symbol fits), 'easyeda' (always import the LCSC/EasyEDA drawing).

    What was chosen and why is always in the result:
      source            'kicad-stock' (the symbol is KiCad's) | 'easyeda' (the symbol was converted)
      symbol_source     'stock' | 'converted'
      footprint_source  'stock' | 'converted' (a stock symbol on the converted footprint when no stock footprint fits)
      why               one sentence: the match and the checks it passed, or why no stock symbol could be used
      selection         {'chosen', 'match', 'verified', 'checks': [...], 'reference', 'tried': [{lib_id, ok, notes}],
                         'fallback': reason the converted symbol was used (only then)}"""
    code = C.lcsc_norm(lcsc)
    if not code:
        raise ToolError(f"'{lcsc}' is not an LCSC part number", code="BAD_ARGUMENT",
                        fix="pass a C-number like 'C25804' (find one with parts_search)")
    # fresh=True (parts_import): live JLC class/stock, same numbers parts_search shows; placement keeps the
    # instant offline record (stock is re-checked by parts_source_bom / fab anyway)
    rec = part_record(code, live=live, fresh=fresh and live)
    comp = None
    warnings: list[str] = []
    if not rec:
        try:
            comp = E.fetch(code)
            cp = (comp.get("dataStr") or {}).get("head", {}).get("c_para", {})
            rec = {"lcsc": code, "mfr": cp.get("Manufacturer Part") or comp.get("title"), "manufacturer": cp.get("Manufacturer"),
                   "package": cp.get("package"), "description": comp.get("description") or "",
                   "basic": "Basic" in (cp.get("JLCPCB Part Class") or ""), "preferred": False,
                   "kind": "other", "typed": {}, "datasheet": ""}
            rec["class"] = C.jlc_class(rec["basic"], False)
            warnings.append("part not found in the JLC catalogue — data from EasyEDA only; check JLC assembly availability")
        except C.NetError as e:
            raise ToolError(f"{code}: not found in JLC catalogue or EasyEDA ({e})", code="PART_NOT_FOUND",
                            fix="check the C-number on lcsc.com, or parts_search(query=...) for an alternative")
    from .basic import canonical_value
    fields = {
        "LCSC": code, "MPN": rec.get("mfr") or "", "Manufacturer": rec.get("manufacturer") or "",
        "Datasheet": rec.get("datasheet") or "", "Description": (rec.get("description") or "")[:250],
        "JLC Class": rec.get("class") or C.jlc_class(rec.get("basic"), rec.get("preferred")),
    }
    value = canonical_value(rec) or rec.get("mfr") or code
    base = {"lcsc": code, "value": value, "basic": bool(rec.get("basic")), "preferred": bool(rec.get("preferred")),
            "class": fields["JLC Class"], "mpn": fields["MPN"], "package": rec.get("package"),
            "stock": rec.get("stock"), "stock_source": rec.get("stock_source")}
    if rec.get("stock") is not None and rec["stock"] <= 0:
        warnings.append("JLC assembly stock is 0 — consider parts_alternatives")

    # 1. KiCad stock symbol, when one is verifiably right (parts.stock)
    sel = None
    if prefer in ("auto", "stock"):
        from . import stock as S

        def load_ref():
            nonlocal comp
            if comp is None:
                comp = E.fetch(code)
            return supplier_ref(comp, rec.get("kind") or "")
        sel = S.select(rec, pr.dir, load_ref)
        ch = sel["choice"]
        tried = [{"lib_id": t["lib_id"], "footprint": t.get("footprint") or "", "ok": t["ok"], "notes": t["notes"]} for t in sel["tried"]]
        if ch:
            selection = {"chosen": ch["lib_id"], "symbol": "stock", "footprint": ch["footprint_source"], "match": ch["match"],
                         "verified": ch.get("verified"), "checks": ch["notes"], "reference": sel["reference"], "tried": tried}
            out = dict(base, lib_id=ch["lib_id"], footprint=ch["footprint"], fields=fields, source="kicad-stock",
                       symbol_source="stock", footprint_source=ch["footprint_source"], pins=ch.get("pins", {}),
                       units=ch.get("units", 1), warnings=warnings, files_written=[], selection=selection)
            sym = stock_symbol(ch["lib_id"], pr.dir) or {}
            if not fields["Datasheet"] and sym.get("datasheet"):
                fields["Datasheet"] = sym["datasheet"]
            if ch["footprint_source"] == "converted":
                # stock symbol on the supplier's footprint: write that footprint into the project library
                fp = _write_footprint(pr, code, comp, rec, fields, warnings, with_3d, overwrite)
                if set(fp["meta"]["pads"]) != set(out["pins"]):          # re-checked on what was actually written
                    raise ToolError(f"{code}: converted footprint pads {fp['meta']['pads']} do not match {ch['lib_id']}",
                                    code="CONVERT_FAILED", fix=f"parts_import(lcsc='{code}', prefer='easyeda')")
                out.update(footprint=fp["id"], files_written=fp["files"] + [str(pr.dir / t) for t in ensure_lib_tables(pr)],
                           pads=fp["meta"]["pad_count"], through_hole=fp["meta"]["through_hole"],
                           courtyard_mm=fp["meta"]["courtyard_mm"], model=(fp["model"] or {}).get("path"),
                           libs={"fp": str(lib_paths(pr)["pretty"])}, **({"edge_cuts_moved": fp["edge"]} if fp["edge"] else {}))
                fields["Footprint Source"] = E.ATTRIBUTION
            out["why"] = (f"KiCad stock symbol {ch['lib_id']}: {ch['why']}; "
                          + ("KiCad stock footprint" if ch["footprint_source"] == "stock" else "footprint converted from the supplier drawing")
                          + f" — verified: {ch.get('verified')}")
            return out
        if prefer == "stock":
            raise ToolError(f"{code} ({rec.get('mfr')}, {rec.get('package')}): no verified KiCad stock symbol — {sel['fallback']}"[:600],
                            code="NO_STOCK_FIT", fix=f"parts_import(lcsc='{code}', prefer='easyeda')", data={"tried": tried})
        warnings += [f"stock candidate rejected: {t['lib_id']} / {t['footprint'] or '(no footprint)'}: {'; '.join(t['notes'])}"[:400]
                     for t in tried if not t["ok"]][:4]

    # 2. EasyEDA import — the explicit, reported fallback
    if comp is None:
        try:
            comp = E.fetch(code)
        except C.NetError as e:
            raise ToolError(f"{code}: EasyEDA symbol/footprint unavailable ({e})", code="EASYEDA_UNAVAILABLE",
                            fix=f"retry later (network), or pick an alternative with parts_alternatives(target='{code}')")
    out = _import_easyeda(pr, code, comp, rec, fields, base, warnings, with_3d, overwrite)
    if sel is None:
        reason = "prefer='easyeda' was requested"
        selection = {"chosen": out["lib_id"], "symbol": "converted", "footprint": "converted", "fallback": reason, "tried": []}
    else:
        reason = sel["fallback"]
        selection = {"chosen": out["lib_id"], "symbol": "converted", "footprint": "converted", "fallback": reason,
                     "reference": sel["reference"],
                     "tried": [{"lib_id": t["lib_id"], "footprint": t.get("footprint") or "", "ok": t["ok"], "notes": t["notes"]}
                               for t in sel["tried"]]}
    out.update(symbol_source="converted", footprint_source="converted", selection=selection,
               why=f"symbol converted from the supplier (EasyEDA) drawing — no KiCad stock symbol could be used: {reason}"[:700])
    return out


def supplier_ref(comp: dict, kind: str = "") -> dict | None:
    """The supplier (EasyEDA) drawing as the pin data parts.stock verifies against: {'pins': {num: name}, 'units',
    'pads': [...], 'pad_names': set, 'package'}. None when the drawing cannot be converted (nothing to verify with)."""
    from . import stock as S
    try:
        _, sinfo = E.build_symbol(comp, "REF", {"Value": "REF"}, footprint="", kind=kind or "")
        text, meta = E.build_footprint(comp, "REF")
    except Exception:  # noqa: BLE001 — an unreadable drawing is 'no reference', never a crash
        return None
    pkg = comp.get("packageDetail") or {}
    return {"pins": {str(k): str(v or "") for k, v in sinfo["pins"].items()}, "units": sinfo.get("units") or 1,
            "duplicate_pin_numbers": bool(sinfo.get("duplicate_pin_numbers")),
            "pads": S.pads_of(text), "pad_names": {str(p) for p in meta.get("pads") or []}, "package": pkg.get("title") or ""}


def _write_footprint(pr, code, comp, rec, fields, warnings, with_3d, overwrite) -> dict:
    """Convert the supplier footprint (+3D) into libs/copper-lcsc.pretty. → {'id', 'name', 'meta', 'files', 'model', 'edge'}"""
    paths = lib_paths(pr)
    paths["pretty"].mkdir(parents=True, exist_ok=True)
    cpara = ((comp.get("dataStr") or {}).get("head") or {}).get("c_para") or {}
    mpn = fields["MPN"] or cpara.get("Manufacturer Part") or comp.get("title") or code
    pkg = comp.get("packageDetail") or {}
    fp_name = E.safe_name(pkg.get("title") or ((pkg.get("dataStr") or {}).get("head") or {}).get("c_para", {}).get("package") or code)
    pkg_uuid = pkg.get("uuid") or ""
    # name collision: same footprint name, different EasyEDA package → suffix with C-number
    fp_file = paths["pretty"] / f"{fp_name}.kicad_mod"
    if fp_file.exists() and pkg_uuid and f"easyeda:{pkg_uuid}" not in fp_file.read_text(encoding="utf-8", errors="ignore"):
        fp_name = f"{fp_name}_{code}"
        fp_file = paths["pretty"] / f"{fp_name}.kicad_mod"
    if not fields["Datasheet"]:
        link = (((pkg.get("dataStr") or {}).get("head") or {}).get("c_para") or {}).get("link") or ""
        if link.startswith("http"):
            fields["Datasheet"] = link.split("?")[0]
    files = []
    descr = (f"{mpn} ({code}) {rec.get('package') or ''} — {fields['Description'][:120]} | "
             f"Source: {E.ATTRIBUTION} | easyeda:{pkg_uuid}")
    try:
        fp_text, fmeta = E.build_footprint(comp, fp_name, descr=descr, tags=f"{code} {mpn} LCSC JLCPCB")
    except E.ConvertError as e:
        raise ToolError(f"{code}: footprint conversion failed: {e}", code="CONVERT_FAILED",
                        fix="use a KiCad stock footprint via sch_apply set_field Footprint, or choose another part")
    edge = fmeta.get("edge_cuts")
    if edge:
        warnings.append(edge_cuts_note(edge))
    model_info = None
    if with_3d and fmeta.get("model"):
        model_info = _write_3d(paths, fp_name, fmeta["model"], warnings)
        if model_info:
            fp_text = E.set_model(fp_text, model_info["path"], model_info["offset"], model_info["rotate"])
            files += model_info.get("files", [])
            w = _model_sanity(model_info.get("bbox"), fmeta)
            if w:
                warnings.append(w)
    if overwrite or not fp_file.exists() or fp_file.read_text(encoding="utf-8", errors="ignore") != fp_text:
        atomic_write(fp_file, fp_text)
        files.append(str(fp_file))
    return {"id": f"{LIB}:{fp_name}", "name": fp_name, "meta": fmeta, "files": files, "model": model_info, "edge": edge, "mpn": mpn}


def _import_easyeda(pr, code, comp, rec, fields, base, warnings, with_3d, overwrite) -> dict:
    paths = lib_paths(pr)
    fp = _write_footprint(pr, code, comp, rec, fields, warnings, with_3d, overwrite)
    mpn, fmeta, files, model_info, edge = fp["mpn"], fp["meta"], fp["files"], fp["model"], fp["edge"]
    sym_name = f"{E.safe_name(mpn)}_{code}"
    fields["Source"] = E.ATTRIBUTION
    footprint_id = fp["id"]
    # symbol
    try:
        sym, sinfo = E.build_symbol(comp, sym_name, dict(fields, Value=mpn, Keywords=f"{code} {mpn} {rec.get('kind') or ''}"),
                                    footprint=footprint_id, kind=rec.get("kind") or "")
    except E.ConvertError as e:
        raise ToolError(f"{code}: symbol conversion failed: {e}", code="CONVERT_FAILED",
                        fix="choose another part or draw the symbol manually")
    lib_tree = _load_symlib(paths["sym"])
    old = _existing_symbol(lib_tree, sym_name)
    if old is not None:
        lib_tree.remove(old)
    lib_tree.append(sym)
    new_text = sexpr.dumps(lib_tree) + "\n"
    if not paths["sym"].exists() or paths["sym"].read_text(encoding="utf-8") != new_text:
        atomic_write(paths["sym"], new_text)
        files.append(str(paths["sym"]))
    tables = ensure_lib_tables(pr)
    files += [str(pr.dir / t) for t in tables]
    # validation: pads ⊇ pins
    pads = set(fmeta["pads"])
    pins = sinfo["pins"]
    missing = sorted(set(pins) - pads, key=E._natkey)
    unused = sorted(pads - set(pins), key=E._natkey)
    if missing:
        warnings.append(f"symbol pins {missing} have NO matching footprint pad — the part will not connect on the PCB")
    if unused:
        warnings.append(f"footprint pads {unused} have no symbol pin (mechanical/shield/EP?) — they will be unconnected")
    return dict(base, lib_id=f"{LIB}:{sym_name}", footprint=footprint_id, fields=fields, source="easyeda",
                pins=pins, units=sinfo["units"], pads=fmeta["pad_count"], pin_pad_mismatch=missing,
                unconnected_pads=unused, through_hole=fmeta["through_hole"], courtyard_mm=fmeta["courtyard_mm"],
                model=(model_info or {}).get("path"), warnings=warnings, files_written=files,
                libs={"sym": str(paths["sym"]), "fp": str(paths["pretty"])},
                **({"edge_cuts_moved": edge} if edge else {}))


def edge_cuts_note(edge: dict) -> str:
    """One line for the import result: what was moved off Edge.Cuts and what the lines usually mean."""
    n = edge["count"]
    s = (f"footprint had {n} graphic{'s' if n != 1 else ''} on Edge.Cuts (EasyEDA board-outline layer) — moved to "
         f"{edge['moved_to']} so placing the part cannot corrupt the board outline")
    be = edge.get("board_edge")
    if be:
        s += (f"; hint: the line marks where the board edge should be — put the board edge at {be['axis']} = "
              f"{be['at_mm']:g} mm from the footprint origin (footprint-local, before rotation)")
    elif edge.get("bbox_mm"):
        s += (f"; hint: these usually mark the board edge or a cutout the part needs (footprint-local bbox "
              f"{edge['bbox_mm']} mm) — draw it in the real board outline with pcb_setup if so")
    return s


def _write_3d(paths, fp_name, model, warnings) -> dict | None:
    obj, step = E.fetch_3d(model["uuid"])
    if not obj and not step:
        warnings.append("no 3D model available from EasyEDA")
        return None
    paths["3d"].mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    verts = None
    rel = None
    if obj:
        try:
            wrl, _ = E.obj_to_wrl(obj)
            files += _write_if_changed(paths["3d"] / f"{fp_name}.wrl", wrl)
            verts = E.parse_obj(obj)[0]
            rel = f"{fp_name}.wrl"
        except E.ConvertError as e:
            warnings.append(f"3D OBJ conversion failed: {e}")
    if step:
        files += _write_if_changed(paths["3d"] / f"{fp_name}.step", step)
        # the transform must come from the file KiCad will load: EasyEDA's OBJ and STEP of one part can
        # use different native origins (C5383109: OBJ z -7.0..4.6, STEP z -3.0..8.5 → a header 4 mm in the air)
        sv = E.step_vertices(step)
        if sv:
            rel, verts = f"{fp_name}.step", sv
        elif verts is None:
            warnings.append("STEP model has no readable vertices; not referenced")
    if not verts:
        warnings.append("3D model has no geometry; not referenced")
        return None
    off, rot = E.model_transform(model, verts)
    return {"path": "${KIPRJMOD}/libs/" + LIB + ".3dshapes/" + rel, "offset": off, "rotate": rot, "files": files,
            "bbox": E.model_bbox(verts, off, [-r for r in rot])}


def _model_sanity(bbox, fmeta: dict) -> str | None:
    """3D model placed as KiCad will show it vs the footprint: it must sit on the board (bottom near
    z=0 or pins through it) and cover the pads' centre. → warning text or None."""
    if not bbox:
        return None
    x0, y0, z0, x1, y1, z1 = bbox
    px0, py0, px1, py1 = fmeta.get("pad_extent_mm") or fmeta.get("courtyard_mm") or (0, 0, 0, 0)
    cx, cy = (px0 + px1) / 2, -(py0 + py1) / 2          # KiCad 3D frame: +Y up
    msgs = []
    if z0 > 0.5:
        msgs.append(f"floats {z0:.1f} mm above the board")
    if z1 < 0:
        msgs.append("is entirely below the board")
    if not (x0 - 0.3 <= cx <= x1 + 0.3 and y0 - 0.3 <= cy <= y1 + 0.3):
        msgs.append(f"does not cover the pad centre (model x {x0:.1f}..{x1:.1f}, y {y0:.1f}..{y1:.1f}; pads at {cx:.2f},{cy:.2f})")
    if msgs:
        return ("3D model " + " and ".join(msgs) + " — check render(target='pcb3d'); the footprint itself is fine")
    return None


def _write_if_changed(path: Path, data) -> list[str]:
    """Write text/bytes only when different; → [path] if written (honest change reporting)."""
    try:
        old = path.read_bytes() if isinstance(data, bytes) else path.read_text(encoding="utf-8")
        if old == data:
            return []
    except OSError:
        pass
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    return [str(path)]


def verify_import(pr: Project, result: dict, render_dir: Path | None = None, width: int = 700) -> dict:
    """Load the imported symbol/footprint with kicad-cli (proves KiCad parses
    them) and optionally rasterize previews. → {'ok', 'errors', 'images'}"""
    from .. import svgraster
    out = {"ok": True, "errors": [], "images": []}
    stock = result.get("source") == "kicad-stock"
    fp_converted = result.get("footprint_source") == "converted" or result.get("source") == "easyeda"
    if not fp_converted and not (stock and render_dir is not None):
        return out
    paths = lib_paths(pr)
    tmp = Path(tempfile.mkdtemp(prefix="copper-parts-"))
    try:
        sym_lib, _, sym_name = result["lib_id"].partition(":")
        fp_lib, _, fp_name = result["footprint"].partition(":")
        sym_file, fp_dir = str(paths["sym"]), str(paths["pretty"])
        if stock:      # KiCad's own libraries: preview only (they load by definition)
            try:
                sym_file = env.find_lib("sym", sym_lib, pr.dir).path
                if not fp_converted:
                    fp_dir = env.find_lib("fp", fp_lib, pr.dir).path
            except ToolError as e:
                out["errors"].append(f"preview unavailable: {e}")
                return out
        (tmp / "sym").mkdir()
        (tmp / "fp").mkdir()
        r1 = env.run(["sym", "export", "svg", "--symbol", sym_name, "-o", str(tmp / "sym"), sym_file], timeout=120)
        r2 = env.run(["fp", "export", "svg", "--footprint", fp_name, "-o", str(tmp / "fp"), fp_dir], timeout=120)
        ssvg = sorted((tmp / "sym").glob("*.svg")) if (tmp / "sym").exists() else []
        fsvg = sorted((tmp / "fp").glob("*.svg")) if (tmp / "fp").exists() else []
        if not ssvg:
            out["ok"] = stock
            out["errors"].append(f"kicad-cli could not load symbol {sym_name}: {(r1.stderr or r1.stdout).strip()[-300:]}")
        if not fsvg:
            out["ok"] = stock and not fp_converted and out["ok"]
            out["errors"].append(f"kicad-cli could not load footprint {fp_name}: {(r2.stderr or r2.stdout).strip()[-300:]}")
        if render_dir is not None:
            render_dir.mkdir(parents=True, exist_ok=True)
            from ..mcp.registry import Image
            for svg in ssvg[:4]:
                png = render_dir / f"{svg.stem}.png"
                svgraster.rasterize(str(svg), str(png), width=width, background=(255, 255, 255, 255))
                out["images"].append(Image(path=str(png), caption=f"symbol {svg.stem}"))
            for svg in fsvg[:1]:
                png = render_dir / f"{svg.stem}_fp.png"
                svgraster.rasterize(str(svg), str(png), width=width, background=(20, 20, 24, 255))
                out["images"].append(Image(path=str(png), caption=f"footprint {fp_name} (pads, silk, courtyard)"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out
