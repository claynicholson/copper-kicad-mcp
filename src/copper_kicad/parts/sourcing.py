"""BOM sourcing: give every schematic component an LCSC part, Basic first.

``source_bom(pr, policy)`` reads the schematic's components (via
``sch.netlist.export_netlist``, falling back to kicad-cli directly), infers a
spec from Value + Footprint (+ symbol) for every component without an LCSC
number ('10k' + R_0603 → Basic C25804), writes LCSC / MPN / Manufacturer
fields through ``sch.ops.apply_ops`` and reports lock %, Basic/Preferred/
Extended counts, estimated Extended setup fees, stock risks and the parts
that need a human/agent decision.

``alternatives(lcsc|spec)`` → in-stock substitutes in the same package.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from .. import sexpr
from ..kicad import env
from ..mcp.registry import ToolError
from . import basic, search
from . import common as C

LCSC_FIELDS = ("LCSC", "LCSC Part", "LCSC Part #", "LCSC#", "LCSC_PART", "JLCPCB Part", "JLC", "JLCPCB", "LCSC Part Number")
NOT_ASSEMBLED = re.compile(r"^(MountingHole|TestPoint|Fiducial|Symbol|NetTie|Jumper:SolderJumper|kibuzzard|Logo)", re.I)

DEFAULT_POLICY = {
    "basic_only": False,        # refuse Extended parts entirely
    "allow_extended": True,     # allow Extended when no Basic/Preferred fits
    "min_stock": 500,           # flag parts below this JLC assembly stock
    "boards": 5,                # build quantity for stock-risk maths
    "overwrite": False,         # replace existing LCSC fields
    "live": True,               # confirm stock/class with the live JLC API
    "write": True,              # write fields (False = report only)
    "fields": ["LCSC", "MPN", "Manufacturer"],
}


# ── reading components ───────────────────────────────────────────────────
def _components(pr) -> dict:
    try:
        from ..sch.netlist import export_netlist  # contract (sch module)
        return export_netlist(pr)["components"]
    except ImportError:
        pass
    return _components_cli(pr)


def _components_cli(pr) -> dict:
    """Fallback reader: kicad-cli sch export netlist (kicadsexpr)."""
    if not pr.sch.exists():
        raise ToolError(f"no schematic at {pr.sch}", code="NOT_FOUND", fix="project_open(path=...)")
    with tempfile.TemporaryDirectory(prefix="copper-net-") as td:
        out = Path(td) / "net.net"
        r = env.run(["sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(out), str(pr.sch)], timeout=180)
        if not out.exists():
            raise ToolError(f"kicad-cli netlist export failed: {(r.stderr or r.stdout)[-400:]}", code="KICAD_CLI_FAILED")
        tree = sexpr.loads(out.read_text(encoding="utf-8"))
    comps = {}
    for c in sexpr.find_all(sexpr.find(tree, "components") or [], "comp"):
        ref = str(sexpr.value(c, "ref", ""))
        ls = sexpr.find(c, "libsource")
        fields = {}
        fs = sexpr.find(c, "fields")
        for f in sexpr.find_all(fs or [], "field"):
            nm = sexpr.value(f, "name")
            if nm is not None and len(f) > 2:
                fields[str(nm)] = str(f[2])
        for p in sexpr.find_all(c, "property"):
            nm, val = sexpr.value(p, "name"), sexpr.value(p, "value")
            if nm and val is not None:
                fields.setdefault(str(nm), str(val))
        sheet = sexpr.find(c, "sheetpath")
        comps[ref] = {"value": str(sexpr.value(c, "value", "")), "footprint": str(sexpr.value(c, "footprint", "") or ""),
                      "lib_id": f"{sexpr.value(ls, 'lib', '')}:{sexpr.value(ls, 'part', '')}" if ls else "",
                      "fields": fields, "sheet": str(sexpr.value(sheet, "names", "/")) if sheet else "/"}
    return comps


def existing_lcsc(fields: dict) -> str | None:
    low = {k.lower(): v for k, v in (fields or {}).items()}
    for f in LCSC_FIELDS:
        v = low.get(f.lower())
        code = C.lcsc_norm(v) if v else None
        if code and len(code) > 2:
            return code
    return None


MPN_FIELDS = ("MPN", "Manufacturer Part Number", "Manufacturer_Part_Number", "Mfr Part", "Mfr. Part #", "MFR_PN",
              "MFN", "Part Number", "PartNumber", "MP", "Manufacturer Part", "Mfr_PN")


def existing_mpn(fields: dict) -> str | None:
    low = {k.lower(): v for k, v in (fields or {}).items()}
    for f in MPN_FIELDS:
        v = str(low.get(f.lower()) or "").strip()
        if len(re.sub(r"[^A-Za-z0-9]", "", v)) >= 4 and v not in ("~", "-", "N/A", "n/a", "NA", "DNP"):
            return v
    return None


_NOT_ASSEMBLED_RX = re.compile(r"(?i)(^|[_:])(mounting_?hole|plain_?hole|testpoint|test_point|fiducial|logo|"
                               r"kibuzzard|graphic|nettie|net_tie|symbol)|creative_?commons|oshw|^Symbol:")
_NOT_ASSEMBLED_REF = re.compile(r"^(H|MH|LOGO|FID|TP|G|NT|REF\*\*)\d*$", re.I)


def skip_reason(ref: str, c: dict) -> str | None:
    """Why a component is not part of the assembled BOM (DNP, excluded, mechanical), or None."""
    low = {k.lower(): str(v).strip().lower() for k, v in (c.get("fields") or {}).items()}

    def flag(name):
        return name in low and low[name] not in ("no", "0", "false", "n")
    if c.get("dnp") or flag("dnp"):
        return "DNP"
    if flag("exclude_from_bom"):
        return "excluded from BOM"
    if flag("exclude_from_board"):
        return "not on board"
    fp = c.get("footprint") or ""
    if NOT_ASSEMBLED.match(fp.split(":", 1)[-1]) or NOT_ASSEMBLED.match(fp) or _NOT_ASSEMBLED_RX.search(fp) \
            or _NOT_ASSEMBLED_RX.search(c.get("lib_id") or "") or (_NOT_ASSEMBLED_REF.match(ref) and not fp.lower().startswith(("connector", "button"))):
        return "mechanical / not assembled"
    return None


# designator prefix → kinds a part on it may have (a 'J' must never get an ESD chip because its Value says 'USB6')
_REF_FAMILY = [
    (r"^(J|P|CN|CON|X?USB|JP)$", {"connector", "header", "usb_connector", "switch"}),
    (r"^(SW|S|BTN|K)$", {"switch", "connector", "other", "ic"}),
    (r"^(R|RN|RA)$", {"resistor"}),
    (r"^(C|CP|CE)$", {"capacitor", "electrolytic", "tantalum"}),
    (r"^(L)$", {"inductor", "ferrite"}),
    (r"^(FB|FL)$", {"ferrite", "inductor"}),
    (r"^(D|LED|ZD|TVS)$", {"diode", "schottky", "zener", "tvs", "esd", "rectifier", "bridge", "led"}),
    (r"^(Q|T|M)$", {"mosfet", "bjt", "ic", "other"}),
    (r"^(Y|X|XTAL|OSC)$", {"crystal", "oscillator"}),
    (r"^(F|PTC)$", {"fuse"}),
    (r"^(U|IC)$", {"ic", "mcu", "module", "memory", "opamp", "interface", "ldo", "dcdc", "esd", "tvs", "oscillator",
                   "other", "mosfet", "bjt", "connector", "led", "switch"}),
]


def ref_kinds(ref: str) -> set | None:
    pre = re.match(r"^[A-Za-z]+", ref or "")
    if not pre:
        return None
    for rx, kinds in _REF_FAMILY:
        if re.match(rx, pre.group(0), re.I):
            return kinds
    return None


_IMPERIAL = ("01005", "0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512")
_METRIC_ONLY = {"1005": "0402", "1608": "0603", "2012": "0805", "3216": "1206", "3225": "1210", "4532": "1812",
                "5025": "2010", "6332": "2512"}
_PKG_FAMILIES = re.compile(
    r"(?<![A-Za-z0-9])(SOT-?23(?:-\d)?|SOT-?223|SOT-?89|SOT-?323|SOT-?363|SOT-?563|SOT-?143|SOD-?123F?|SOD-?323F?|"
    r"SOD-?523|SOD-?923|SOIC-\d+|SOP-\d+|TSSOP-\d+|HTSSOP-\d+|MSOP-\d+|SSOP-\d+|QFN-\d+|DFN-\d+|TDFN-\d+|LQFP-\d+|"
    r"TQFP-\d+|TO-92|TO-220|TO-252|TO-263|DIP-\d+|DO-41|DO-35|DO-15|DO-201|MiniMELF|SMA|SMB|SMC)(?![0-9A-Za-z])", re.I)
_PASSIVE_KINDS = ("resistor", "capacitor", "inductor", "ferrite", "led", "fuse", "electrolytic", "tantalum")


def footprint_package(fp_name: str, kind: str | None = None) -> str | None:
    """Package implied by a footprint name: 'R_0603_1608Metric'/'C_0402'/'1005_C' → '0603'/'0402'/'0402',
    'D_DO-35_SOD27_…' → 'DO-35', 'DIP-8_W7.62mm' → 'DIP-8', 'Crystal_SMD_3225-4Pin…' → 'SMD3225-4P'."""
    n = fp_name.split(":", 1)[-1]
    m = re.match(r"^Crystal_SMD_(\d{4})-(\d)Pin", n, re.I)
    if m:
        return f"SMD{m.group(1)}-{m.group(2)}P"
    m = re.match(r"^Crystal_(\d)-SMD_(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)mm", n, re.I)
    if m:
        return f"SMD{round(float(m.group(2)) * 10):02d}{round(float(m.group(3)) * 10):02d}-{m.group(1)}P"
    if kind in _PASSIVE_KINDS or kind is None:
        m = re.search(r"(?:^|_)(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)_\d{4}Metric", n)
        if m:
            return m.group(1)
        if re.match(r"^(R|C|L|LED|D|FB|Fuse|CP|RES|CAP|IND)?_?(SMD_?)?(\d{4,5})(?:_|$|[A-Z]?$)", n, re.I) or \
                re.match(r"^\d{4,5}_[A-Z]+$", n, re.I):
            for t in re.findall(r"\d{4,5}", n)[:1]:
                if t in _IMPERIAL:
                    return t
                if t in _METRIC_ONLY:
                    return _METRIC_ONLY[t]
    m = _PKG_FAMILIES.search(n.replace("_", "-"))
    if m:
        return C.norm_pkg(m.group(1).upper())
    return None


# ── spec inference ───────────────────────────────────────────────────────
_LIB_KIND = [(r"^Device:R(_|$)|:R_Small|Resistor", "resistor"), (r"^Device:C_Polarized|:CP|C_Polarized", "electrolytic"),
             (r"^Device:C(_Small)?$|^Device:C$", "capacitor"), (r"^Device:L(_|$)|Inductor", "inductor"),
             (r"Ferrite", "ferrite"), (r"^Device:LED", "led"), (r"D_Schottky", "schottky"), (r"D_Zener", "zener"),
             (r"D_TVS", "tvs"), (r"^Device:D(_Small)?$", "diode"), (r"Crystal", "crystal"), (r"Fuse", "fuse"),
             (r"Q_NMOS|Q_PMOS", "mosfet"), (r"Q_NPN|Q_PNP", "bjt")]
_REF_KIND = {"R": "resistor", "C": "capacitor", "L": "inductor", "FB": "ferrite", "LED": "led", "Y": "crystal", "X": "crystal",
             "F": "fuse", "D": "diode"}


def infer_spec(comp: dict) -> dict:
    lib_id = comp.get("lib_id") or ""
    fp = comp.get("footprint") or ""
    value = (comp.get("value") or "").strip()
    ref = comp.get("ref", "")
    kind = None
    for rx, k in _LIB_KIND:
        if re.search(rx, lib_id, re.I):
            kind = k
            break
    fp_name = fp.split(":", 1)[-1]
    if kind is None:
        pre = re.match(r"^[A-Za-z]+", ref)
        kind = _REF_KIND.get(pre.group(0).upper()) if pre else None
        if kind == "diode" and re.search(r"LED", lib_id + fp_name, re.I):
            kind = "led"
    pkg = footprint_package(fp_name, kind) if fp_name else None
    spec = basic.parse_spec(value) if value else {"keywords": []}
    if kind:
        if spec.get("kind") in (None, "resistor", "capacitor", "inductor") or kind in ("led", "electrolytic"):
            spec["kind"] = kind if not (kind == "capacitor" and spec.get("kind") in ("electrolytic", "tantalum")) else spec["kind"]
        # bare '100n' parsed as capacitor but symbol says inductor etc.
        if kind == "inductor" and spec.get("unit") == "F":
            spec["unit"] = "H"
        want_unit = {"resistor": "ohm", "capacitor": "F", "electrolytic": "F", "tantalum": "F", "inductor": "H",
                     "crystal": "Hz", "oscillator": "Hz"}.get(spec.get("kind"))
        if want_unit and spec.get("value") is not None and spec.get("unit") != want_unit:
            # '470' on a capacitor symbol: ohms make no sense, pF is a guess → let a human decide
            spec.pop("value", None)
            spec.pop("unit", None)
            spec["unit_mismatch"] = True
        if kind == "led":
            spec.pop("mpn", None)
            spec.pop("value", None)
    if pkg:
        spec["package"] = pkg
    spec["_footprint"] = fp_name
    # generic symbol names in Value ('R', 'C', 'LED', 'D', 'Crystal') carry no part info
    if value.upper() in ("R", "C", "L", "D", "LED", "CRYSTAL", "FB", "FERRITEBEAD", "FUSE", "Q_NMOS", "Q_PMOS"):
        spec.pop("mpn", None)
    spec.setdefault("keywords", [])
    spec["keywords"] = [k for k in spec["keywords"] if k not in ("r", "c", "l", "d")]
    spec["_ref_kinds"] = ref_kinds(ref)
    spec["_ref"] = ref
    rks = spec["_ref_kinds"]
    if spec.get("kind") == "resistor" and spec.get("unit") == "ohm" and re.fullmatch(r"\d{3,}", value) \
            and rks and "resistor" not in rks:
        # Value '7805' on U3 (TO-220) is a part number, not a 7805 Ω resistor
        for k in ("value", "unit", "kind"):
            spec.pop(k, None)
        spec["mpn"] = value
    mpn = existing_mpn(comp.get("fields") or {})
    if mpn:
        spec["_mpn_field"] = mpn
    return spec


_ORDER_SUFFIX = re.compile(r"^(tr|t|r|rl|lf|sn|lfsn|pbf|nopb|g|g4|e3|ct|f|tf|ta|tb|hf|z|ztr|reel|tape|ldtr)*$")


def _kind_ok(r: dict, spec: dict) -> bool:
    ks = spec.get("_ref_kinds")
    return not ks or (r.get("kind") or "other") in ks


def _mpn_eq(a: str, b: str) -> bool:
    """MPN equality ignoring punctuation and packaging/ordering suffixes ('-TR', '(LF)(SN)', '/NOPB')."""
    x, y = basic._alnum(a), basic._alnum(b)
    if not x or not y:
        return False
    if x == y:
        return True
    lo, hi = (x, y) if len(x) < len(y) else (y, x)
    return len(lo) >= 5 and hi.startswith(lo) and bool(_ORDER_SUFFIX.match(hi[len(lo):]))


def by_mpn(mpn: str, live: bool = True, family: bool = False) -> list[dict]:
    """Catalogue records whose MPN equals ``mpn`` (offline table + live JLC), in-stock Basic first.
    family=True also accepts maker-prefixed generic numbers ('78L05' → L78L05ACZ, MC78L05ACP)."""
    q = basic._alnum(mpn)

    def ok(m):
        return _mpn_eq(m, mpn) or (family and basic.mpn_family(q, basic._alnum(m)))
    hits = {r["lcsc"]: r for r in basic.all_parts() if ok(r.get("mfr"))}
    if live:
        try:
            for r in search.jlc_query(mpn.strip(), page_size=50 if family else 25, in_stock=False):
                if ok(r.get("mfr")):
                    hits[r["lcsc"]] = r          # live record wins (fresh stock/class)
        except C.NetError:
            pass
    out = list(hits.values())
    out.sort(key=lambda r: (0 if (r.get("stock") or 0) > 0 else 1, C.CLASS_RANK.get(r.get("class"), 3),
                            -(r.get("stock") or 0)))
    return out


def _choose(spec: dict, policy: dict, live: bool) -> tuple[dict | None, str, list[dict], list[str]]:
    """→ (record, reason, other options, notes)."""
    notes: list[str] = []
    passive = spec.get("kind") in ("resistor", "capacitor", "inductor", "ferrite", "led", "crystal", "electrolytic")
    # 1. an explicit MPN field is the strongest signal
    mf = spec.get("_mpn_field")
    if mf:
        hits = [h for h in by_mpn(mf, live) if not policy.get("basic_only") or h["basic"]]
        pk = spec.get("package")
        fit = [h for h in hits if pk and h.get("package") and basic.pkg_match(h["package"], pk)]
        if pk and not fit and not passive:
            # a generic MPN field ('78L05', '1N4007') is made in several packages: the footprint decides
            fam = [h for h in by_mpn(mf, live, family=True) if h.get("package") and basic.pkg_match(h["package"], pk)
                   and (not policy.get("basic_only") or h["basic"])]
            fit = [h for h in fam if (h.get("stock") or 0) > 0] or fam
        if fit:
            hits = fit
        if hits:
            best = hits[0]
            if pk and best.get("package") and not basic.pkg_match(best["package"], pk):
                if not passive:
                    pkgs = sorted({str(h.get("package")) for h in hits})[:4]
                    return None, (f"MPN field '{mf}' exists at JLC only as {', '.join(pkgs)}, but the footprint is {pk}: "
                                  "change the footprint to match, or set LCSC to a part in this package "
                                  f"(parts_search(query='{mf}', package='{pk}'))"), hits[:3], notes
                notes.append(f"MPN field {mf} is {best['package']} but the footprint looks like {pk} "
                             "— check the footprint")
            if not _kind_ok(best, spec):
                notes.append(f"{best['lcsc']} is a {best.get('kind')} — unusual for this designator; check the MPN field")
            how = "found at JLC" if _mpn_eq(best.get("mfr") or "", mf) else f"matched maker part {best.get('mfr')}"
            return best, f"MPN field '{mf}' {how}" + (f" in {pk}" if pk and fit else ""), hits[1:4], notes
        if not passive or spec.get("value") is None:
            opts = []
            if live:
                try:
                    opts = search.search(mf, {"in_stock": True}, limit=6, live=True)["results"]
                except Exception:  # noqa: BLE001
                    opts = []
            return None, (f"MPN field '{mf}' is not in JLC's catalogue — pick an equivalent with parts_search "
                          "and set its LCSC field"), [dict(o, _presented=True) for o in opts[:3]], notes
        notes.append(f"MPN field '{mf}' is not stocked at JLC — chose an equivalent by value + package "
                     "(the MPN field is replaced)")
    has_info = spec.get("value") is not None or spec.get("mpn") or (spec.get("kind") == "led" and spec.get("color"))
    if not has_info:
        hit = [h for h in _mpn_in_footprint(spec) if _kind_ok(h, spec)]
        if hit:
            return hit[0], f"MPN {hit[0]['mfr']} found in the footprint name", hit[1:4], notes
        if spec.get("unit_mismatch"):
            return None, ("Value has no unit that fits this symbol (e.g. '470' on a capacitor) — "
                          "write '470pF' / '4.7uF'"), [], notes
        return None, "Value doesn't identify a part (set Value to e.g. '10k', '100nF 16V', 'AMS1117-3.3' or an MPN)", [], notes
    if passive and not spec.get("package") and spec.get("kind") != "crystal":
        fp = spec.get("_footprint") or ""
        if re.search(r"(?i)potentiometer|trimmer|^RV", fp) or re.match(r"(?i)^(RV|VR|POT)\d", spec.get("_ref") or ""):
            return None, (f"potentiometer footprint '{fp}': choose the trimmer by its body "
                          f"(parts_search(query='{_spec_query(spec)} potentiometer')) and set its LCSC field"), [], notes
        if tht_geometry(fp) is not None:
            return _choose_tht(spec, policy, live, notes)
        return None, (f"footprint doesn't identify a package (assign a footprint like "
                      f"{_SMD_EXAMPLE.get(spec.get('kind'), 'R_0603_1608Metric')} first)"), [], notes
    cands = [c for c in basic.candidates(spec) if _kind_ok(c, spec)]
    strong = [c for c in cands if c["_score"] == 0]
    if policy.get("basic_only"):
        strong = [c for c in strong if c["basic"]]
    if strong and (strong[0]["basic"] or strong[0]["preferred"]):
        return strong[0], "offline Basic/Preferred table", strong[1:4], notes
    # live search widens the pool (Extended parts, fresher stock)
    pool = list(strong)
    if live:
        try:
            q = spec.get("raw") or ""
            flt = {"in_stock": True, "basic_only": bool(policy.get("basic_only"))}
            if spec.get("package"):
                flt["package"] = spec["package"]      # '1N4148' on DO-35 must not return the SOD-123 '1N4148W'
            res = search.search(q if spec.get("mpn") else _spec_query(spec), flt, limit=8, live=True, raw=True)
            # full records come back with the search: no per-result catalogue lookups (each was a
            # rate-limited JLC request, the main cost of a first parts_source_bom run)
            for full in res.get("records", []):
                if basic.spec_matches(full, spec) == 0 and _kind_ok(full, spec):
                    pool.append(dict(full, _score=0))
        except Exception:  # noqa: BLE001 — sourcing must not die on a flaky source
            pass
    pool.sort(key=lambda r: basic._rank(r, spec))
    seen, uniq = set(), []
    for r in pool:
        if r["lcsc"] not in seen:
            seen.add(r["lcsc"])
            uniq.append(r)
    if not uniq:
        return None, "no in-stock JLC part matches this Value/footprint", [], notes
    best = uniq[0]
    if not (best["basic"] or best["preferred"]) and not policy.get("allow_extended", True):
        return None, "only Extended parts match and policy forbids Extended", uniq[:3], notes
    if spec.get("mpn") and not passive:
        q = basic._alnum(spec["mpn"])
        exact = [r for r in uniq if basic._alnum(r["mfr"]).startswith(q) or basic.mpn_family(q, basic._alnum(r["mfr"]))]
        if not exact:
            return None, f"no exact MPN match for '{spec['mpn']}'", uniq[:3], notes
        if len({C.norm_pkg(r.get("package")) for r in exact}) > 1 and not spec.get("package"):
            return None, f"'{spec['mpn']}' exists in several packages — choose one matching the footprint", exact[:4], notes
        best = exact[0]
    return best, "live JLC search" if best.get("source") == "jlc" else "offline table", \
        [r for r in uniq if r is not best][:3], notes


# ── through-hole passives ────────────────────────────────────────────────
_SMD_EXAMPLE = {"resistor": "R_0603_1608Metric", "capacitor": "C_0805_2012Metric", "inductor": "L_0805_2012Metric",
                "ferrite": "L_0603_1608Metric", "led": "LED_0603_1608Metric", "fuse": "Fuse_1206_3216Metric",
                "electrolytic": "Capacitor_SMD:CP_Elec_6.3x7.7", "tantalum": "CP_EIA-3528-21_Kemet-B"}
_THT_RX = re.compile(r"(?i)axial|radial|THT|disc|_P\d|DIP|TO-?92|C_Rect|^LED_D\d")


def tht_geometry(fp: str) -> dict | None:
    """Through-hole passive footprint → {'style', 'D', 'L', 'P', 'W'} (mm, when present), else None.
    'R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal' → axial D2.5 L6.3 P10.16;
    'CP_Radial_D6.3mm_P2.50mm' → radial D6.3 P2.5; 'C_Disc_D5.0mm_W2.5mm_P5.00mm' → disc P5."""
    n = (fp or "").split(":", 1)[-1]
    if not _THT_RX.search(n):
        return None
    g: dict = {"style": next((s for s in ("axial", "radial", "disc", "rect") if s in n.lower()), "tht")}
    for k in ("D", "L", "P", "W"):
        m = re.search(rf"(?:^|_){k}(\d+(?:\.\d+)?)mm", n)
        if m:
            g[k] = float(m.group(1))
    return g


def _rec_geometry(r: dict) -> tuple[bool, dict]:
    """(is through-hole, {'D','L','P','_mm'}) from a JLC record's package/description."""
    pkg = str(r.get("package") or "")
    sub = str(r.get("subcategory") or "")
    desc = str(r.get("description") or "")
    tht = bool(re.search(r"(?i)^plugin|插件|through hole|leaded|\bDIP\b", pkg) or
               re.search(r"(?i)through hole|leaded|plugin|插件", f"{sub} {desc}"))
    if re.search(r"(?i)\bSMD\b|surface mount|chip", f"{pkg} {sub}"):
        tht = False
    g: dict = {}
    txt = f"{pkg} {desc}"
    m = re.search(r"D(\d+(?:\.\d+)?)\s*[x*×]\s*L(\d+(?:\.\d+)?)", txt, re.I)
    if m:
        g["D"], g["L"] = float(m.group(1)), float(m.group(2))
    m = re.search(r"P\s*=\s*(\d+(?:\.\d+)?)\s*mm", txt, re.I)
    if m:
        g["P"] = float(m.group(1))
    g["_mm"] = [float(x) for x in re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*mm", desc)]
    return tht, g


def _geom_score(fpg: dict, r: dict) -> int | None:
    """0 = body/pitch confirmed against the footprint, 1 = not confirmable, None = not a THT part or
    a THT part whose body/pitch contradicts the footprint."""
    tht, g = _rec_geometry(r)
    if not tht:
        return None
    mm = g.get("_mm") or []
    txt = f"{r.get('package') or ''} {r.get('subcategory') or ''} {r.get('description') or ''}".lower()
    # lead style must match: a radial electrolytic does not fit an axial footprint (and vice versa).
    # LCSC's through-hole resistors are axial by construction; other kinds must say so.
    if fpg["style"] == "axial" and r.get("kind") != "resistor" and "axial" not in txt:
        return None
    if fpg["style"] in ("radial", "disc") and "axial" in txt:
        return None

    def near(v, tol, *cands):
        return any(c is not None and abs(c - v) <= tol for c in cands)
    if fpg["style"] == "axial" and fpg.get("D") and fpg.get("L"):
        if "D" in g:
            return 0 if near(fpg["D"], 0.7, g["D"]) and near(fpg["L"], 1.3, g.get("L")) else None
        return 1
    if fpg["style"] == "radial" and fpg.get("D"):
        if "D" in g or mm:
            if not near(fpg["D"], 0.35, g.get("D"), *mm):
                return None
            return 0 if not fpg.get("P") or near(fpg["P"], 0.15, g.get("P"), *mm) else 1
        return 1
    if fpg.get("P"):
        if "P" in g or mm:
            return 0 if near(fpg["P"], 0.15, g.get("P"), *mm) else None
        return 1
    if fpg.get("D"):          # round body only (LED_D5.0mm)
        if "D" in g or mm:
            return 0 if near(fpg["D"], 0.35, g.get("D"), *mm) else None
        return 1
    return 1


def _tht_query(spec: dict) -> str:
    k = spec.get("kind")
    v = _spec_query({kk: vv for kk, vv in spec.items() if kk not in ("package", "voltage")})
    if k == "resistor":
        return f"{v} Through Hole Resistors"
    if k == "electrolytic":
        return f"{v} {spec['voltage']:g}V Plugin" if spec.get("voltage") else f"{v} Aluminum Electrolytic Capacitors - Leaded"
    if k == "capacitor":
        return f"{v} Through Hole"
    return f"{v} Plugin"


def _choose_tht(spec: dict, policy: dict, live: bool, notes: list) -> tuple:
    """Through-hole passive footprint: pick an in-stock THT LCSC part whose value (and voltage) match and
    whose body/pitch fits the footprint. Same rule as THT semis (DO-35 / TO-92 / DIP matched by package):
    assign when the fit is confirmed, otherwise return options + an exact, kind-appropriate fix."""
    fp = spec.get("_footprint") or ""
    fpg = tht_geometry(fp) or {"style": "tht"}
    kind = spec.get("kind")
    smd = _SMD_EXAMPLE.get(kind, "R_0603_1608Metric")
    vtxt = f" rated ≥{spec['voltage']:g}V" if spec.get("voltage") else ""
    alt_hint = f"or switch to an SMD footprint ({smd}{vtxt}) and re-run parts_source_bom"
    if not live:
        return None, f"through-hole footprint '{fp}': THT parts need the live JLC search (live=true), {alt_hint}", [], notes
    try:
        rows = search.jlc_query(_tht_query(spec), page_size=100, in_stock=True)
    except C.NetError as e:
        return None, f"through-hole footprint '{fp}': live JLC search unavailable ({e}), {alt_hint}", [], notes
    sp = {k: v for k, v in spec.items() if k != "package"}
    good, maybe = [], []
    for r in rows:
        if re.search(r"(?i)potentiometer|variable|varistor|thermistor|trimmer|safety|array|network", r.get("subcategory") or ""):
            continue
        if not _kind_ok(r, spec) or basic.spec_matches(r, sp) != 0:
            continue
        if policy.get("basic_only") and not r.get("basic"):
            continue
        gs = _geom_score(fpg, r)
        if gs is None:
            continue
        (good if gs == 0 else maybe).append(dict(r, _score=gs))
    good.sort(key=lambda r: basic._rank(r, sp))
    maybe.sort(key=lambda r: basic._rank(r, sp))
    if good:
        best = good[0]
        if not (best["basic"] or best["preferred"]) and not policy.get("allow_extended", True):
            return None, "only Extended THT parts match and policy forbids Extended", good[:3], notes
        notes.append("through-hole part: JLC solders it in its THT step (extra per-board cost vs SMD)")
        return best, f"live JLC search: THT {kind} matching the footprint body/pitch", (good[1:] + maybe)[:3], notes
    why = (f"through-hole footprint '{fp}': no in-stock THT {kind} with this value{vtxt} and a confirmed body/pitch "
           f"for it; pick one of `options` after checking its size, {alt_hint}")
    return None, why, maybe[:3], notes


def _mpn_in_footprint(spec: dict) -> list[dict]:
    """Footprints often embed the MPN (USB_C_Receptacle_HRO_TYPE-C-31-M-12,
    SW_SPST_TS-1187A…): match offline-table MPNs (≥6 alnum chars) inside it."""
    fp = basic._alnum(spec.get("_footprint", ""))
    if len(fp) < 6:
        return []
    hits = [dict(r, _score=0) for r in basic.all_parts()
            if len(basic._alnum(r.get("mfr"))) >= 6 and basic._alnum(r.get("mfr")) in fp]
    if spec.get("kind"):
        hits = [h for h in hits if basic._same_family(spec["kind"], h.get("kind") or "")] or hits
    hits.sort(key=lambda r: (-len(basic._alnum(r["mfr"])), basic._rank(r, spec)))
    return hits


_REC_CACHE: dict = {}


def search_record(code: str) -> dict | None:
    if code in _REC_CACHE:
        return _REC_CACHE[code]
    r = basic.by_lcsc(code)
    if r is None:
        try:
            r = search.jlc_part(code)
        except C.NetError:
            r = None
    _REC_CACHE[code] = r
    return r


def _spec_query(spec: dict) -> str:
    parts = []
    k = spec.get("kind")
    if spec.get("value") is not None:
        parts.append(C.fmt_si(spec["value"], {"ohm": "ohm", "F": "F", "H": "H", "Hz": "Hz"}.get(spec.get("unit"), "")))
    if spec.get("voltage"):
        parts.append(f"{spec['voltage']:g}V")
    if spec.get("package"):
        parts.append(spec["package"])
    if k == "led":
        parts += [spec.get("color") or "", "LED"]
    return " ".join(p for p in parts if p)


def _sheet_target(pr, c: dict) -> tuple[str, str | None]:
    """(sheet file, sheet arg for apply_ops) for a netlist component. Root → (root file, None);
    sub-sheet → (its Sheetfile, its Sheetname — which also selects the right instance)."""
    f = c.get("fields") or {}
    sfile = str(f.get("Sheetfile") or "").strip()
    sname = str(f.get("Sheetname") or "").strip()
    path = str(c.get("sheet") or "/")
    if path in ("", "/") or not sfile or Path(sfile).name.lower() == pr.sch.name.lower() and path.count("/") <= 1:
        return pr.sch.name, None
    if not sname:
        sname = [x for x in path.split("/") if x][-1]
    return sfile, sname


def source_bom(pr, policy: dict | None = None) -> dict:
    pol = dict(DEFAULT_POLICY)
    pol.update(policy or {})
    if pol["write"] or pol.get("will_write"):
        # refuse BEFORE the (slow, rate-limited) searches when the schematic cannot be written anyway
        from .. import project as _P
        _P.guard_writable(pr.sch, pol.get("while_open", "refuse"))
    comps = _components(pr)
    groups: dict[tuple, list[str]] = {}
    skipped: dict[str, list[str]] = {}
    for ref, c in sorted(comps.items(), key=lambda kv: _natref(kv[0])):
        if ref.startswith("#"):
            continue
        why_skip = skip_reason(ref, c)
        if why_skip:
            skipped.setdefault(why_skip, []).append(ref)
            continue
        fpn = (c.get("footprint") or "")
        key = (c.get("value"), fpn, c.get("lib_id"), existing_lcsc(c.get("fields") or {}) or "",
               existing_mpn(c.get("fields") or {}) or "")
        groups.setdefault(key, []).append(ref)
    ops: list[tuple[str, dict]] = []      # (ref, op)
    lines, manual, risks = [], [], []
    usage: dict[str, list] = {}
    for (value, fp, lib_id, code, mpn_field), refs in groups.items():
        comp = dict(comps[refs[0]], ref=refs[0])
        line = {"refs": refs, "value": value, "footprint": fp}
        sheet = fix_sheet(pr, comps, refs[0])
        if sheet:
            line["sheet"] = sheet            # sch_apply(..., sheet=<this>) reaches these refs (sub-sheet / instance)
        rec = None
        if not fp:
            line.update(status="manual", reason="no footprint assigned — set one (sch_apply set_field Footprint) first")
            manual.append(line)
            continue
        if code and not pol["overwrite"]:
            rec = basic.by_lcsc(code)
            if pol["live"]:
                try:
                    live = search.jlc_part(code)
                    rec = live or rec
                    if live is None:
                        line["warning"] = "C-number not listed in JLC's assembly catalogue"
                except C.NetError:
                    pass
            line.update(status="locked", lcsc=code)
            if rec:
                fpk = footprint_package(fp, rec.get("kind"))
                if fpk and rec.get("package") and not basic.pkg_match(rec["package"], fpk) \
                        and C.norm_pkg(rec["package"]) in C.CHIP_SIZES + ["SOT-23", "SOT-223", "SOD-123", "SOD-323", "SMA", "SMB"]:
                    line["warning"] = (f"LCSC {code} is {rec['package']} but the footprint is {fpk} — "
                                       f"the part will not fit; fix the footprint or parts_alternatives(target='{code}')")
        else:
            spec = infer_spec(comp)
            rec, why, others, notes = _choose(spec, pol, pol["live"])
            if notes:
                line["notes"] = notes
            if rec is None:
                line.update(status="manual", reason=why,
                            spec={k: v for k, v in spec.items() if not k.startswith("_") and k != "raw" and v not in (None, [], "")},
                            options=[({k: v for k, v in o.items() if k != "_presented"} if o.get("_presented")
                                      else search.present(o)) for o in others][:3])
                manual.append(line)
                continue
            line.update(status="assigned", lcsc=rec["lcsc"], reason=why,
                        alternatives=[o["lcsc"] for o in others][:3])
            if mpn_field and not _mpn_eq(mpn_field, rec.get("mfr") or ""):
                line["replaces_mpn"] = mpn_field
            vals = {"LCSC": rec["lcsc"], "MPN": rec.get("mfr") or "", "Manufacturer": rec.get("manufacturer") or ""}
            for ref in refs:
                fields = {f: vals[f] for f in pol["fields"] if vals.get(f)}
                if fields:
                    ops.append((ref, {"op": "set_field", "ref": ref, "fields": fields}))
        if rec:
            line.update(mpn=rec.get("mfr"), jlc_class=rec.get("class"), package=rec.get("package"),
                        stock=rec.get("stock"),
                        unit_price=C.unit_price(rec.get("price") or [], max(10, len(refs) * pol["boards"])))
            u = usage.setdefault(line["lcsc"], [rec, []])
            u[1].extend(refs)
        lines.append(line)
    for code, (rec, refs) in usage.items():     # stock risk per LCSC number (all lines using it)
        need = len(refs) * pol["boards"]
        st = rec.get("stock")
        if st is None:
            risks.append({"lcsc": code, "refs": refs, "risk": "stock unknown"})
        elif st < need * 2 or st < pol["min_stock"]:
            risks.append({"lcsc": code, "refs": refs, "stock": st, "need": need,
                          "risk": "OUT OF STOCK" if st <= 0 else "low JLC assembly stock",
                          "fix": f"parts_alternatives(target='{code}')"})
    written = None
    if ops and pol["write"]:
        written = _write_ops(pr, comps, ops, pol.get("while_open", "refuse"))
    all_lines = lines + manual
    total_refs = sum(len(l["refs"]) for l in all_lines)
    locked_refs = sum(len(l["refs"]) for l in lines if l["status"] in ("locked", "assigned"))
    # unique *LCSC numbers* by class — J2..J8 with different Values but one connector is one feeder / one fee
    classes = {"Basic": 0, "Preferred": 0, "Extended": 0, "unknown": 0}
    seen_codes = set()
    for l in lines:
        if l["status"] in ("locked", "assigned") and l.get("lcsc") not in seen_codes:
            seen_codes.add(l.get("lcsc"))
            classes[l.get("jlc_class") or "unknown"] = classes.get(l.get("jlc_class") or "unknown", 0) + 1
    cost = sum(l["unit_price"] * len(l["refs"]) for l in lines if l.get("unit_price"))
    return {
        "summary": {
            "components": total_refs, "bom_lines": len(all_lines), "unique_lcsc": len(seen_codes),
            "locked_pct": round(100.0 * locked_refs / total_refs, 1) if total_refs else 100.0,
            "locked": locked_refs, "newly_assigned": sum(len(l["refs"]) for l in lines if l["status"] == "assigned"),
            "need_manual_choice": sum(len(l["refs"]) for l in manual),
            "unique_by_class": classes,
            "extended_setup_fees_usd": round(classes["Extended"] * C.JLC_SETUP_FEE_USD, 2),
            "parts_cost_per_board_usd": round(cost, 3),
            "stock_risks": len(risks),
            "skipped": {k: v for k, v in skipped.items()},
            "stock_source": "live JLC assembly stock" if pol["live"] else f"offline snapshot {basic.snapshot_date()}",
        },
        "lines": lines, "manual": manual, "stock_risks": risks,
        "written": written if written is not None else {
            "ops": len(ops), "note": ("not written (write=false)" if not (pol["write"] or pol.get("will_write"))
                                      else "nothing to write" if not ops else "not written yet")},
    }


def _write_ops(pr, comps: dict, ops: list[tuple[str, dict]], while_open: str = "refuse") -> dict:
    """Write set_field ops sheet by sheet. Symbols in a sheet used by several instances share their
    fields, so each sheet *file* is written once (through its first instance)."""
    try:
        from ..sch.ops import apply_ops, sheet_file  # contract (sch module)
        from .. import project as P
    except ImportError as e:
        raise ToolError(f"schematic writer unavailable ({e}); nothing written", code="SCH_OPS_UNAVAILABLE",
                        fix="parts_source_bom(write=false) to get the assignments")
    by_file: dict[str, tuple[str | None, list[dict]]] = {}
    for ref, op in ops:
        sfile, sheet = _sheet_target(pr, comps[ref])
        key = sfile.lower()
        if key not in by_file:
            by_file[key] = (sheet, [])
        if by_file[key][0] == sheet:
            by_file[key][1].append(op)
        # else: another instance of an already-covered sheet file → same symbols, same fields
    # refuse up front (before writing anything) if any target file is open in KiCad
    files = {}
    # KiCad locks only the ROOT sheet (~root.kicad_sch.lck) while the hierarchy is open, so a sub-sheet
    # write must also be refused when the root is open
    P.guard_writable(pr.sch, while_open)
    for key, (sheet, _) in by_file.items():
        f, _ = sheet_file(pr, sheet)
        P.guard_writable(f, while_open)
        files[key] = f
    done, changed = [], 0
    for key, (sheet, sops) in by_file.items():
        try:
            r = apply_ops(pr, sops, while_open=while_open, sheet=sheet)
        except ToolError as e:
            if done:
                e.message += f" — NOTE: {', '.join(done)} were already written (checkpoint taken before)"
            raise
        done.append(Path(r.get("file") or files[key]).name)
        changed += len(r.get("changed") or [])
    return {"ops": len(ops), "sheets_written": done, "fields_changed": changed}

def _natref(ref: str):
    m = re.match(r"([A-Za-z#]+)(\d*)", ref)
    return (m.group(1), int(m.group(2) or 0)) if m else (ref, 0)


# ── alternatives ─────────────────────────────────────────────────────────
def alternatives(target: str, limit: int = 8, live: bool = True, same_package: bool = True) -> dict:
    code = C.lcsc_norm(target) if re.fullmatch(r"\s*[Cc]?\d{2,10}\s*", target or "") else None
    if not code:
        res = search.search(target, {"in_stock": True}, limit=limit, live=live)
        return {"target": target, "spec": res["spec"], "alternatives": res["results"], "sources": res["sources"]}
    from .importer import part_record
    rec = part_record(code, live=live, fresh=live)
    if not rec:
        raise ToolError(f"{code} not found in JLC catalogue", code="PART_NOT_FOUND",
                        fix="parts_search(query=...) with the value/MPN instead")
    spec: dict = {"keywords": []}
    k = rec.get("kind")
    t = rec.get("typed") or {}
    passive_val = {"resistor": ("ohm", "ohm"), "capacitor": ("F", "F"), "electrolytic": ("F", "F"), "tantalum": ("F", "F"),
                   "inductor": ("H", "H"), "ferrite": ("ohm", "ohm"), "crystal": ("Hz", "Hz")}
    if k in passive_val and t.get(passive_val[k][0]) is not None:
        spec.update(kind=k, value=t[passive_val[k][0]], unit=passive_val[k][1])
        if t.get("V"):
            spec["voltage"] = t["V"]
    elif k == "led":
        spec.update(kind="led", color=t.get("color"))
    else:
        # ICs / discretes: same MPN family across makers (strip packaging/grade suffix)
        mpn = rec.get("mfr") or ""
        spec["mpn"] = re.sub(r"[-/#](TR|T|R|RL|PBF|NOPB|G4|E3|13|7)$", "", mpn, flags=re.I)
        spec["kind"] = k if k not in ("ic", "other") else None
    if same_package and rec.get("package"):
        spec["package"] = C.norm_pkg(rec["package"])
    pool = {r["lcsc"]: r for r in basic.candidates(spec)}
    status = {"seed": "ok"}
    if live:
        try:
            q = spec.get("mpn") or _spec_query(spec) or rec.get("mfr")
            for r in search.jlc_query(q, page_size=100, in_stock=True):
                pool.setdefault(r["lcsc"], r)
            if spec.get("value") is not None:
                for r in search.jlc_query(q, page_size=50, basic_only=True, in_stock=True):
                    pool.setdefault(r["lcsc"], r)
            status["jlc"] = "ok"
        except C.NetError as e:
            status["jlc"] = f"unavailable: {e}"
    out = []
    for r in pool.values():
        if r["lcsc"] == code:
            continue
        s = basic.spec_matches(r, spec)
        if s >= 2:
            continue
        if (r.get("stock") or 0) <= 0:
            continue
        out.append((basic._rank(dict(r, _score=s), spec), r, s))
    out.sort(key=lambda x: x[0])
    note_extra = ""
    if not out and k in _PARAMETRIC and (not same_package or rec.get("package")):
        # no same-MPN-family part: fall back to a parametric match (kind + package + key rating)
        spec = {"keywords": [], "kind": k}
        if same_package and rec.get("package"):
            spec["package"] = C.norm_pkg(rec["package"])
        for key, sk in _PARAMETRIC[k]:
            if t.get(key) is not None:
                spec[sk] = t[key]
        pool = {r["lcsc"]: r for r in basic.candidates(spec)}
        if live:
            words = {"esd": "ESD", "tvs": "TVS", "zener": "Zener", "diode": "Diode", "schottky": "Schottky",
                     "mosfet": "MOSFET", "bjt": "Transistor", "ldo": "LDO"}
            q = " ".join(x for x in (f"{t['V']:g}V" if t.get("V") and k in ("esd", "tvs", "zener") else "",
                                     words[k], spec.get("package") or "") if x)
            try:
                for r in search.jlc_query(q, page_size=100, in_stock=True):
                    pool.setdefault(r["lcsc"], r)
            except C.NetError as e:
                status["jlc"] = f"unavailable: {e}"
        for r in pool.values():
            s = basic.spec_matches(r, spec)
            if r["lcsc"] != code and s < 2 and (r.get("stock") or 0) > 0:
                out.append((basic._rank(dict(r, _score=s), spec), r, s))
        out.sort(key=lambda x: x[0])
        if out:
            note_extra = (" No same-MPN part is stocked, so these match by parameters (kind, package, "
                          f"{', '.join(sk for _, sk in _PARAMETRIC[k])}) — compare datasheets (polarity/pinout/capacitance) before swapping.")
    return {"target": code, "original": search.present(rec), "spec": {k: v for k, v in spec.items() if v not in (None, [], "")},
            "alternatives": [search.present(r, s) for _, r, s in out[:limit]], "sources": status,
            "note": "same package, in JLC assembly stock; Basic > Preferred > Extended, then stock. "
                    "For ICs, check the pinout/datasheet before swapping makers." + note_extra}


_PARAMETRIC = {"esd": [("V", "vz")], "tvs": [("V", "vz")], "zener": [("V", "vz")], "diode": [], "schottky": [],
               "mosfet": [("channel", "channel")], "bjt": [("polarity", "polarity")], "ldo": [("Vout", "vout")]}


def fix_sheet(pr, comps: dict, ref: str) -> str | None:
    """The `sheet` argument a sch_apply op needs to reach `ref` (None for the root sheet): the sub-sheet
    file, or the instance name when the sheet is used several times (sch.ops.ref_elsewhere)."""
    c = comps.get(ref) or {}
    sfile, sname = _sheet_target(pr, c)
    if sname is None:
        return None
    try:
        from ..sch.model import Schematic
        from ..sch.ops import ref_elsewhere
        return ref_elsewhere(pr, Schematic.load(pr.sch, pr.name), ref) or sname
    except Exception:  # noqa: BLE001 — a hint must never fail the sourcing pass
        return sname


# ── provider-neutral sourcing ─────────────────────────────────────────────
def source_bom_generic(pr, providers: list, policy: dict | None = None, part_field: str | None = None) -> dict:
    """BOM sourcing through any list of parts providers (parts/providers), asked in order per component.

    A component is *sourced* when it carries the project's part-number field (`part_field`), or — when a
    generic provider such as 'kicad' is active — when it simply has a Value and a Footprint. For the rest,
    each provider's ``match_component`` is asked; the first match writes that provider's part-number field
    plus the vendor-neutral MPN / Manufacturer (and any extra supplier fields the provider knows)."""
    from .providers import generic_ok, part_number
    pol = dict(DEFAULT_POLICY)
    pol.update(policy or {})
    if pol["write"] or pol.get("will_write"):
        from .. import project as _P
        _P.guard_writable(pr.sch, pol.get("while_open", "refuse"))
    comps = _components(pr)
    generic = generic_ok(providers)
    names = [p.name for p in providers]
    groups: dict[tuple, list[str]] = {}
    skipped: dict[str, list[str]] = {}
    for ref, c in sorted(comps.items(), key=lambda kv: _natref(kv[0])):
        if ref.startswith("#"):
            continue
        why_skip = skip_reason(ref, c)
        if why_skip:
            skipped.setdefault(why_skip, []).append(ref)
            continue
        f = c.get("fields") or {}
        key = (c.get("value"), c.get("footprint") or "", c.get("lib_id"), part_number(f, part_field), existing_mpn(f) or "")
        groups.setdefault(key, []).append(ref)
    ops: list[tuple[str, dict]] = []
    lines, manual = [], []
    by_provider: dict[str, int] = {}
    for (value, fp, lib_id, number, mpn_field), refs in groups.items():
        comp = dict(comps[refs[0]], ref=refs[0])
        line = {"refs": refs, "value": value, "footprint": fp}
        sheet = fix_sheet(pr, comps, refs[0])
        if sheet:
            line["sheet"] = sheet
        if not fp:
            line.update(status="manual", reason="no footprint assigned — set one (sch_apply set_field Footprint) first")
            manual.append(line)
            continue
        if number and not pol["overwrite"]:
            line.update(status="locked", part=number, part_field=part_field)
            lines.append(line)
            continue
        rec = why = prov = None
        options: list = []
        for pv in providers:
            try:
                r, w, others = pv.match_component(comp, pol, pol["live"])
            except ToolError:
                raise
            except Exception as e:  # noqa: BLE001 — one provider failing must not stop the pass
                line.setdefault("notes", []).append(f"provider '{pv.name}' failed: {e}")
                continue
            if r is not None:
                rec, why, prov = r, w, pv
                break
            if others and not options:
                options = others
            why = why or w
        if rec is None:
            line.update(status="manual", reason=why or "no active provider matched it",
                        options=[{k: v for k, v in o.items() if k in ("provider", "id", "mpn", "manufacturer", "value",
                                                                       "package", "stock", "class", "match")} for o in options][:3])
            manual.append(line)
            continue
        by_provider[prov.name] = by_provider.get(prov.name, 0) + len(refs)
        if rec.get("generic"):
            line.update(status="generic", provider=prov.name, reason=why)
            lines.append(line)
            continue
        fields = prov.schematic_fields(prov.normalize(dict(rec)))
        fields = {k: v for k, v in fields.items() if k not in ("Description", "Datasheet")}
        if mpn_field and fields.get("MPN") and not _mpn_eq(mpn_field, fields["MPN"]):
            line["replaces_mpn"] = mpn_field
        line.update(status="assigned", provider=prov.name, part=rec.get("id"), part_field=prov.part_field,
                    mpn=rec.get("mpn") or None, manufacturer=rec.get("manufacturer") or None, reason=why,
                    stock=rec.get("stock"), package=rec.get("package"), fields=fields)
        if rec.get("class"):
            line["class"] = rec["class"]
        for ref in refs:
            if fields:
                ops.append((ref, {"op": "set_field", "ref": ref, "fields": fields}))
        lines.append(line)
    written = None
    if ops and pol["write"]:
        written = _write_ops(pr, comps, ops, pol.get("while_open", "refuse"))
    all_lines = lines + manual
    total = sum(len(l["refs"]) for l in all_lines)
    numbered = sum(len(l["refs"]) for l in lines if l["status"] in ("locked", "assigned"))
    gen = sum(len(l["refs"]) for l in lines if l["status"] == "generic")
    sourced = numbered + (gen if generic else 0)
    return {
        "summary": {
            "providers": names, "part_field": part_field, "components": total, "bom_lines": len(all_lines),
            "sourced": sourced, "sourced_pct": round(100.0 * sourced / total, 1) if total else 100.0,
            "with_part_number": numbered, "generic": gen,
            "newly_assigned": sum(len(l["refs"]) for l in lines if l["status"] == "assigned"),
            "need_manual_choice": sum(len(l["refs"]) for l in manual),
            "by_provider": by_provider, "skipped": {k: v for k, v in skipped.items()},
        },
        "lines": lines, "manual": manual, "stock_risks": [],
        "written": written if written is not None else {
            "ops": len(ops), "note": ("not written (write=false)" if not (pol["write"] or pol.get("will_write"))
                                      else "nothing to write" if not ops else "not written yet")},
    }


# ── stock check for the fab gate ──────────────────────────────────────────
STOCK_BUDGET_S = 75.0     # live lookups are rate-limited (about one a second): stop here and say what was not checked
DEFAULT_BOARDS = 5        # boards an order is assumed to be for when the caller does not say


def _stock_record(prov, part: str) -> tuple[dict | None, str]:
    """(record | None when the supplier does not list the part, what the stock figure is). Raises C.NetError
    when the supplier cannot be reached: a snapshot is never passed off as today's stock."""
    if getattr(prov, "name", "") == "lcsc":
        rec = search.jlc_part(part)                      # the assembly pool, one cached request per part
        return (None if rec is None else {"stock": rec.get("stock"), "class": rec.get("class"), "mpn": rec.get("mfr")},
                "JLCPCB assembly stock (live)")
    if getattr(prov, "needs_network", False) and C.offline():
        raise C.NetError("offline mode (COPPER_OFFLINE=1)")
    from .providers import call
    rec = call(prov, "get", part, live=True)
    label = getattr(prov, "label", None) or getattr(prov, "name", "provider")
    if not isinstance(rec, dict):
        return None, f"{label} stock"
    return {"stock": rec.get("stock"), "class": rec.get("class"), "mpn": rec.get("mpn")}, f"{label} stock"


def stock_check(pr, lines: list[dict], boards: int = DEFAULT_BOARDS, part_field: str | None = None,
                budget_s: float = STOCK_BUDGET_S, progress=None) -> dict:
    """Is every BOM line in stock for `boards` boards? lines: [{'part', 'refs', 'value'?}] (one per unique part number).

    Asks the parts provider that owns each number (lcsc → live JLCPCB assembly stock). Never raises for a
    source problem: offline, a failing provider, no provider for the number or the time budget running out
    leave lines in `unchecked` with `not_checked` saying why — the caller must say stock was NOT checked.
    Returns {'boards', 'lines', 'checked', 'unchecked': [part…], 'not_checked'?, 'source'?, 'classes': {part: class},
    'short': [{'part','refs','value','status','stock','need','fix'}], 'unknown': [part…]}; status is
    'out_of_stock' | 'insufficient' | 'not_listed'."""
    import time
    boards = max(1, int(boards or DEFAULT_BOARDS))
    out: dict = {"boards": boards, "lines": len(lines), "checked": 0, "unchecked": [], "short": [], "unknown": [],
                 "classes": {}}
    if not lines:
        return out
    try:
        from . import providers as PV
        cfg, provs = PV.for_pr(pr)
    except ToolError as e:
        out.update(unchecked=[l["part"] for l in lines], not_checked=f"parts providers unavailable: {e.message}")
        return out
    why: list[str] = []
    dead: dict = {}                                       # provider name → why it stopped answering
    t0 = time.monotonic()
    for i, l in enumerate(lines):
        part, refs = str(l["part"]), sorted(l.get("refs") or [], key=_natref)
        prov = None
        try:
            prov = PV.owner_of(part, cfg, provs)
        except ToolError:
            prov = None
        if prov is None and part_field:
            prov = next((p for p in provs if str(getattr(p, "part_field", "") or "").lower() == part_field.lower()
                         and getattr(p, "name", "") != "lcsc"), None)
        if prov is None:
            out["unchecked"].append(part)
            msg = f"no active parts provider reports stock for {part_field or 'these'} numbers"
            if msg not in why:
                why.append(msg)
            continue
        name = getattr(prov, "name", "?")
        if name in dead:
            out["unchecked"].append(part)
            continue
        if time.monotonic() - t0 > budget_s:
            out["unchecked"].append(part)
            msg = f"time budget of {budget_s:g} s used up after {out['checked']} part(s)"
            if msg not in why:
                why.append(msg)
            continue
        if progress:
            progress(i / len(lines), f"stock {part}")
        try:
            rec, source = _stock_record(prov, part)
        except C.NetError as e:
            dead[name] = f"offline / supplier unreachable ({str(e)[:120]})"
            out["unchecked"].append(part)
            why.append(dead[name])
            continue
        except ToolError as e:
            dead[name] = f"provider '{name}' failed ({e.message[:120]})"
            out["unchecked"].append(part)
            why.append(dead[name])
            continue
        out["checked"] += 1
        out.setdefault("source", source)
        need = len(refs) * boards
        base = {"part": part, "refs": refs, "value": l.get("value") or "", "need": need,
                "fix": f"parts_alternatives(target='{part}')"}
        if rec is None:
            out["short"].append(dict(base, status="not_listed", stock=None))
            continue
        if rec.get("class"):
            out["classes"][part] = rec["class"]
        st = rec.get("stock")
        if st is None:
            out["unknown"].append(part)
        elif st <= 0:
            out["short"].append(dict(base, status="out_of_stock", stock=0))
        elif st < need:
            out["short"].append(dict(base, status="insufficient", stock=int(st)))
    if out["unchecked"]:
        out["not_checked"] = "; ".join(why) or "not checked"
    return out
