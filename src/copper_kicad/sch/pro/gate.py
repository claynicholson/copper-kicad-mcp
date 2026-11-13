"""Identity gate for schematic re-layout: "same netlist, or nothing is written".

A candidate text for ONE sheet file is accepted iff, on staged copies of the whole project,

* G0  every non-power symbol node is unchanged except for its pose and field positions (one narrow exception,
      only when the caller asks for it: a symfix-derived embedded library symbol, under the SAME key, whose pins,
      properties and flags equal the original's - sch/pro/symfix.py; the library-symbol ERC checks of the parts
      that use it are then NOT exempt),
* G1  ``kicad-cli sch export netlist`` of the ROOT sheet is identical (components, fields, net names, net
      classes, every node with pin function / pin type) - :func:`canon`,
* G2  hierarchical / global labels and PWR_FLAGs sit on the same pins as before,
* G3  every net carries exactly the same set of names (labels and power symbols) as before,
* G4  ERC reports nothing it did not report before.

Nothing in here runs inside the user's project directory: kicad-cli creates ``<name>.kicad_prl`` next to the root
sheet when it is missing, so both sides are exported from a staged tree that keeps every file's relative path.
The staged candidate is re-saved by KiCad itself (``sch upgrade --force``) BEFORE it is checked, so the bytes that
are proven are exactly the bytes the caller writes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ... import sexpr
from ...kicad import env
from ...mcp.registry import ToolError
from .. import ops as O
from ..model import FILE_VERSION, Schematic
from ..netlist import parse_netlist
from . import symfix as SYMFIX

# ERC types that depend on this machine's library tables or on geometry the layout cleaned up, not on the circuit
ERC_GEOMETRY_TYPES = {"lib_symbol_issues", "lib_symbol_mismatch", "footprint_link_issues", "endpoint_off_grid"}
# reported once per net at a pin KiCad picks: which pin is named may change with the drawing order
ERC_PER_NET_TYPES = {"power_pin_not_driven", "pin_not_driven"}


def unsupported(msg: str, fix: str | None = None):
    return ToolError(msg, code="LAYOUT_UNSUPPORTED", fix=fix)


# ───────────────────────── staging ─────────────────────────
def stage_root() -> Path:
    """Where staged copies live. COPPER_STAGE_DIR overrides (sandboxed kicad-cli that cannot see the temp dir)."""
    d = os.environ.get("COPPER_STAGE_DIR")
    if d:
        Path(d).mkdir(parents=True, exist_ok=True)
        return Path(d)
    return Path(tempfile.gettempdir())


def _inside(f: Path, d: Path) -> Path | None:
    try:
        return f.resolve().relative_to(d.resolve())
    except (ValueError, OSError):
        return None


def project_files(pr) -> list[Path]:
    """Every file the netlist / ERC of the project depends on: all sheet files, the project file, the project
    symbol table and the symbol libraries it points to inside the project folder. Not .kicad_prl / .kicad_pcb."""
    out = list(O.sheet_files(pr))
    for h in O.hierarchy(pr):
        if h["file"].exists() and _inside(h["file"], pr.dir) is None:
            raise unsupported(f"sheet file {h['file'].name} lies outside the project folder; it cannot be checked "
                              f"in isolation", "move the sheet file into the project folder")
    for f in (pr.pro, pr.dir / "sym-lib-table"):
        if f.exists():
            out.append(f)
    out += sorted(pr.dir.glob("*.kicad_sym"))
    tbl = pr.dir / "sym-lib-table"
    if tbl.exists():
        try:
            for uri in re.findall(r'\(uri "((?:[^"\\]|\\.)*)"\)', tbl.read_text(encoding="utf-8")):
                if uri.startswith("${KIPRJMOD}/"):
                    f = pr.dir / uri[len("${KIPRJMOD}/"):]
                    if f.is_file() and _inside(f, pr.dir) is not None:
                        out.append(f)
        except OSError:
            pass
    seen, uniq = set(), []
    for f in out:
        k = str(f.resolve()).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def stage(pr, replace: dict | None = None) -> Path:
    """Copy the project into a fresh temp dir, keeping relative paths; `replace` maps a file to new text."""
    td = Path(tempfile.mkdtemp(prefix="copper-gate-", dir=str(stage_root())))
    rep = {str(Path(k).resolve()).lower(): v for k, v in (replace or {}).items()}
    for f in project_files(pr):
        rel = _inside(f, pr.dir)
        dst = td / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        txt = rep.get(str(f.resolve()).lower())
        if txt is not None:
            dst.write_text(txt, encoding="utf-8", newline="\n")
        else:
            shutil.copyfile(f, dst)
    return td


# ───────────────────────── G1: netlist canon ─────────────────────────
def _plain(x):
    if isinstance(x, list):
        return [_plain(e) for e in x]
    return str(x)


def export_tree(root_sch: Path) -> list:
    out = root_sch.parent / ".copper-gate.net"
    try:
        r = env.run(["sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(out), str(root_sch)], timeout=300)
        if not out.exists():
            raise ToolError(f"kicad-cli netlist export failed: {(r.stderr or r.stdout).strip()[-400:]}",
                            code="NETLIST_FAILED")
        return sexpr.loads(out.read_text(encoding="utf-8"))
    finally:
        try:
            out.unlink()
        except OSError:
            pass


def canon(tree) -> dict:
    """Everything electrical in a kicadsexpr netlist, order-independent, without the (design ...) header
    (source path, date, tool, title blocks) and without net 'code' numbers (an enumeration)."""
    comps: dict = {}
    for c in sexpr.find_all(sexpr.find(tree, "components") or [], "comp"):
        ref = str(sexpr.value(c, "ref", ""))
        if ref in comps:
            raise unsupported(f"two components share the reference {ref}; a netlist comparison would merge them",
                              "annotate the schematic in KiCad (Tools > Annotate Schematic) first")
        if ref.endswith("?"):
            raise unsupported(f"the schematic has un-annotated references ({ref})",
                              "annotate the schematic in KiCad (Tools > Annotate Schematic) first")
        # (tstamps u1 u2 ..) of a multi-unit part lists its units in the order KiCad met them on the sheet, which
        # follows their position: the SET is the identity (a footprint matches on any of them), not the order
        comps[ref] = json.dumps(sorted(json.dumps([str(e[0])] + sorted(_plain(e[1:])) if e and e[0] == "tstamps"
                                                  else _plain(e)) for e in c[1:] if isinstance(e, list)))
    nets: dict = {}
    seen_nodes: dict = {}
    for n in sexpr.find_all(sexpr.find(tree, "nets") or [], "net"):
        name = str(sexpr.value(n, "name", ""))
        cls = str(sexpr.value(n, "class", ""))
        nodes = []
        for nd in sexpr.find_all(n, "node"):
            key = (str(sexpr.value(nd, "ref", "")), str(sexpr.value(nd, "pin", "")))
            if seen_nodes.setdefault(key, name) != name:
                raise unsupported(f"pin {key[0]}.{key[1]} appears in two nets ({seen_nodes[key]}, {name}): "
                                  f"duplicate references, or a multi-unit part whose shared pin is wired "
                                  f"differently per unit", "annotate the schematic in KiCad first")
            nodes.append(json.dumps(sorted(json.dumps(_plain(e)) for e in nd[1:])))
        nets[name] = json.dumps([cls, sorted(nodes)])
    libparts = sorted(json.dumps(_plain(e)) for e in (sexpr.find(tree, "libparts") or [])[1:])
    return {"components": comps, "nets": nets, "libparts": libparts}


def canon_hash(c: dict) -> str:
    return hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()[:16]


def canon_diff(a: dict, b: dict) -> list[str]:
    out = []
    for sec in ("components", "nets"):
        ka, kb = set(a[sec]), set(b[sec])
        out += [f"G1 {sec}: only before: {k}" for k in sorted(ka - kb)]
        out += [f"G1 {sec}: only after: {k}" for k in sorted(kb - ka)]
        out += [f"G1 {sec}: changed: {k}" for k in sorted(ka & kb) if a[sec][k] != b[sec][k]]
    if a["libparts"] != b["libparts"]:
        out.append("G1 libparts changed")
    return out


# ───────────────────────── G0: symbol payload ─────────────────────────
def _strip_prop(p):
    out = []
    for e in p:
        if isinstance(e, list) and e:
            if e[0] in ("at", "hide"):
                continue
            if e[0] == "effects":
                e = [x for x in e if not (isinstance(x, list) and x and x[0] in ("justify", "hide")) and x != "hide"]
        out.append(_plain(e))
    return out


def symbol_payload(sch: Schematic) -> dict:
    """uuid -> everything of a non-power symbol that a re-layout must not touch."""
    out = {}
    for s in sch.symbols():
        if s.is_power:
            continue
        body = []
        for e in s.node[1:]:
            if isinstance(e, list) and e:
                if e[0] in ("at", "mirror", "fields_autoplaced", "lib_name"):
                    continue                 # lib_name is compared on its own (4th element)
                if e[0] == "property":
                    body.append(_strip_prop(e))
                    continue
            body.append(_plain(e))
        ls = sch.lib_symbol(s.lib_name)
        out[s.uuid] = (s.ref, json.dumps(body), hashlib.sha1(sexpr.dumps(ls).encode()).hexdigest() if ls is not None else "",
                       s.lib_name)
    return out


def payload_diff(a: dict, b: dict, derived_ok: set | None = None) -> list[str]:
    """derived_ok = {lib_symbols key} whose changed embedded symbol symfix_ok() proved equivalent; without it any
    change of a symbol's embedded library symbol is refused. A changed lib_name (which embedded symbol the part
    selects) is always refused: KiCad writes it into the netlist."""
    out = []
    for u in sorted(set(a) | set(b)):
        if u not in b:
            out.append(f"G0 symbol {a[u][0]} would be removed")
        elif u not in a:
            out.append(f"G0 symbol {b[u][0]} would be added")
        elif a[u][1] != b[u][1]:
            out.append(f"G0 symbol {a[u][0]}: data other than its position would change")
        elif a[u][3] != b[u][3]:
            out.append(f"G0 symbol {a[u][0]}: it would point to another embedded library symbol ({b[u][3]})")
        elif a[u][2] != b[u][2] and not (derived_ok and a[u][3] in derived_ok):
            out.append(f"G0 symbol {a[u][0]}: its embedded library symbol would change")
    return out


def symfix_ok(before_syms: dict, sch: Schematic) -> tuple[set, list]:
    """The G0 symfix clause. -> ({lib_symbols key} whose embedded symbol changed admissibly, [reasons for the
    others]). A changed embedded symbol is admissible iff symfix.equivalent() finds its name, every pin (number,
    name, electrical type, shape, unit, body style, hidden flag, alternates, length, direction, text style) and
    every property / flag equal to the original's. Pin positions, the body rectangle and the pin-name offset are
    the only things that may differ."""
    ok, why = set(), []
    for key in sorted({s.lib_name for s in sch.symbols(include_power=False)}):
        old, new = before_syms.get(key), sch.lib_symbol(key)
        if old is None or new is None or sexpr.dumps(old) == sexpr.dumps(new):
            continue
        if not SYMFIX.may_redraw(key):
            # owner rule "default symbols first": a stock / user library symbol is never redrawn, equivalent or not
            why.append(f"G0 symfix: {key} is not a Copper-generated symbol - its embedded copy may not change")
            continue
        bad = SYMFIX.equivalent(old, new)
        if bad:
            why.append(f"G0 symfix: the changed embedded symbol {key} is not pin-equivalent: " + "; ".join(bad))
        else:
            ok.add(key)
    return ok, why


def symfix_refs(sch: Schematic, keys: set) -> set:
    return {s.ref for s in sch.symbols(include_power=False) if s.lib_name in keys}


# ───────────────────────── G2 / G3: what a netlist does not show ─────────────────────────
def label_meta(sch: Schematic) -> dict:
    """uuid -> everything of a label except where it sits (user fields such as Netclass, font, shape)."""
    out = {}
    for l in sch.labels():
        body = []
        for e in l["node"][2:]:              # [1] is the text: renames are G1 / G3's business
            if isinstance(e, list) and e:
                if e[0] in ("at", "fields_autoplaced"):
                    continue
                if e[0] == "effects":
                    e = [x for x in e if not (isinstance(x, list) and x and x[0] == "justify")]
                if e[0] == "property":
                    e = _strip_prop(e)
            body.append(_plain(e))
        out[str(sexpr.value(l["node"], "uuid", ""))] = (l["net"], l["kind"], json.dumps(body))
    return out


def inventory(sch: Schematic, nl: dict) -> tuple[dict, set]:
    """({net: {(text, kind, shape)} incl. ('<value>', 'power', '')}, {names on wire groups without any pin})."""
    inv: dict = {}
    orphans: set = set()
    for g in O._group_nets(sch, nl):
        names = {(l["net"], l["kind"], str(sexpr.value(l["node"], "shape", "")) if l["kind"] != "local" else "")
                 for l in g["labels"]}
        names |= {(s.value, "power", "") for s in g["power"]}
        if g["net"] is None:                 # joined to no pin, not even by name
            orphans |= names
            if g["flags"]:
                orphans.add(("PWR_FLAG", "flag", ""))
        if g["net"] is not None and names:
            inv.setdefault(g["net"], set()).update(names)
    return inv, orphans


def special_items(sch: Schematic) -> dict:
    return {k: len(sch.items(k)) for k in ("bus", "bus_entry", "sheet", "directive_label", "netclass_flag", "rule_area")
            if sch.items(k)}


def flag_counts(sch: Schematic, nl: dict) -> dict:
    out: dict = {}
    for g in O._group_nets(sch, nl):
        if g["flags"] and g["net"] is not None:
            out[g["net"]] = out.get(g["net"], 0) + len(g["flags"])
    return out


DROP_HIDDEN_NC = True      # a no-connect cross on a hidden pin of type no_connect is not kept (it floats beside the body)


def redundant_nc(pin: dict) -> bool:
    """A no-connect flag on this pin says nothing: the pin is hidden (the reader sees a cross on empty paper or on
    the body's edge - DW01A pin 4, MT3608 pin 6, LM4040 pin 3) and its electrical type is already `no_connect`, so
    ERC never asks for a flag and the netlist names the pin 'unconnected-(..)' with or without it."""
    return DROP_HIDDEN_NC and bool(pin.get("hidden")) and str(pin.get("type")) == "no_connect"


def nc_marked(sch: Schematic) -> set:
    """Pin-tip points (rounded to 0.01 mm) that carry a no-connect flag: the cross stands on the tip itself, or at
    the open end of ONE straight wire whose other end is the tip (the engine draws that lead when the cross would
    cover the pin's own number: a short pin has its number exactly where the cross goes). The lead may touch
    nothing else: no second wire, no other pin, at either of its ends."""
    pts = set()
    for n in sch.items("no_connect"):
        a = sexpr.find(n, "at")
        pts.add((round(float(a[1]), 2), round(float(a[2]), 2)))
    if not pts:
        return set()
    tips = set()
    for s in sch.symbols():
        for p in s.pins_world():
            tips.add((round(p["xy"][0], 2), round(p["xy"][1], 2)))
    ends: dict = {}
    for w in sch.wires():
        a = (round(float(w["a"][0]), 2), round(float(w["a"][1]), 2))
        b = (round(float(w["b"][0]), 2), round(float(w["b"][1]), 2))
        ends.setdefault(a, []).append(b)
        ends.setdefault(b, []).append(a)
    out = set()
    for q in pts:
        if q in tips:
            out.add(q)
            continue
        far = ends.get(q, [])
        if len(far) == 1 and far[0] in tips and len(ends.get(far[0], [])) == 1 and far[0] not in pts:
            out.add(far[0])
    return out


def nc_pins(sch: Schematic) -> set:
    pts = nc_marked(sch)
    out = set()
    for s in sch.symbols(include_power=False):
        for p in s.pins_world():
            # a cross on a HIDDEN pin of type no_connect marks nothing the reader can see and nothing ERC asks for:
            # it is not part of what must stay (blocks.redundant_nc; the engine does not draw it again)
            if (round(p["xy"][0], 2), round(p["xy"][1], 2)) in pts and not redundant_nc(p):
                out.add((s.ref, p["number"]))
    return out


# ───────────────────────── G4: ERC ─────────────────────────
def erc_keys(root_sch: Path) -> dict:
    """{key: count} with key = (severity, type, items...) - positions and uuids dropped, #PWR refs normalised."""
    out = root_sch.parent / ".copper-gate-erc.json"
    try:
        env.run(["sch", "erc", "--format", "json", "--severity-all", "-o", str(out), str(root_sch)], timeout=300)
        if not out.exists():
            return {"error": 1}
        data = json.loads(out.read_text(encoding="utf-8"))
    finally:
        try:
            out.unlink()
        except OSError:
            pass
    keys: dict = {}
    for sh in data.get("sheets", []):
        for v in sh.get("violations", []):
            t, sev = str(v.get("type")), str(v.get("severity"))
            if t in ERC_PER_NET_TYPES:
                items = []
            else:
                items = []
                for it in v.get("items", []):
                    d = re.sub(r"#(PWR|FLG)\w*", r"#\1", str(it.get("description", "")))
                    if re.match(r"\s*(horizontal |vertical )?(wire|junction|bus)\b", d, re.I):
                        d = re.sub(r"[^A-Za-z ].*", "", d).strip().split(",")[0]
                    items.append(d)
            k = json.dumps([sev, t, sorted(items)])
            keys[k] = keys.get(k, 0) + 1
    return keys


ERC_LIB_TYPES = ("lib_symbol_issues", "lib_symbol_mismatch")


def erc_new(before: dict, after: dict, strict_refs: set | None = None) -> list[str]:
    """strict_refs (the parts of a symfix candidate): the library-symbol checks that name one of these parts are
    NOT exempt - a new or more frequent item refuses the candidate."""
    out = []
    for k, n in sorted(after.items()):
        if k == "error":
            out.append("G4 ERC did not run on the candidate")
            continue
        sev, t, items = json.loads(k)
        if t in ERC_GEOMETRY_TYPES and not (strict_refs and t in ERC_LIB_TYPES and any(
                re.search(r"\b" + re.escape(r) + r"\b", d) for r in strict_refs for d in items)):
            continue
        if n > before.get(k, 0):
            out.append(f"G4 new ERC {sev} {t}" + (f": {'; '.join(items)[:140]}" if items else "") +
                       (f" (x{n - before.get(k, 0)})" if n - before.get(k, 0) > 1 else ""))
    return out


def erc_counts(keys: dict) -> dict:
    out = {"error": 0, "warning": 0}
    for k, n in keys.items():
        if k == "error":
            continue
        sev, t, _ = json.loads(k)
        if t in ERC_GEOMETRY_TYPES:
            continue
        out[sev] = out.get(sev, 0) + n
    return out


# ───────────────────────── baseline / check ─────────────────────────
@dataclass
class Baseline:
    stage_dir: Path
    root: Path                     # staged root sheet
    sheet: Path                    # staged copy of the edited sheet
    tree: list
    canon: dict
    nl: dict
    payload: dict
    labels: dict
    ext: tuple
    inv: tuple
    special: dict
    flags: dict
    ncs: set
    erc: dict | None = None
    seconds: dict = field(default_factory=dict)
    cli_calls: int = 0
    libsyms: dict = field(default_factory=dict)      # lib_symbols key -> tree of the original sheet (symfix clause)

    def close(self):
        shutil.rmtree(self.stage_dir, ignore_errors=True)


@dataclass
class GateResult:
    ok: bool
    reasons: list
    hash_before: str
    hash_after: str
    text: str = ""                 # the KiCad-canonical candidate that was proven (write exactly this)
    nl: dict | None = None
    erc_before: dict = field(default_factory=dict)
    erc_after: dict = field(default_factory=dict)
    kept: str = ""


def baseline(pr, sheet_file: Path, sheet_path: str | None = None, with_erc: bool = True) -> Baseline:
    """Stage the unmodified project and record everything the candidate is compared against."""
    rel = _inside(Path(sheet_file), pr.dir)
    if rel is None:
        raise unsupported(f"{Path(sheet_file).name} lies outside the project folder")
    td = stage(pr)
    try:
        root = td / pr.sch.name
        t = time.time()
        tree = export_tree(root)
        dt = time.time() - t
        c = canon(tree)
        nl = parse_netlist(tree)
        sch = Schematic.load(td / rel, pr.name, sheet_path)
        calls = 1
        if str(sexpr.value(sch.tree, "version", "")) != str(FILE_VERSION):
            # an older file format: KiCad's re-save adds attributes to every symbol, so G0 compares against the
            # re-saved original (the netlist baseline above is still the untouched file)
            raw = (td / rel).read_bytes()
            r = env.run(["sch", "upgrade", "--force", str(td / rel)], timeout=180)
            calls += 1
            if r.returncode == 0:
                sch_up = Schematic.load(td / rel, pr.name, sheet_path)
            else:
                sch_up = sch
            (td / rel).write_bytes(raw)
        else:
            sch_up = sch
        b = Baseline(td, root, td / rel, tree, c, nl, symbol_payload(sch_up), label_meta(sch_up),
                     O._external_signature(sch, nl), inventory(sch, nl), special_items(sch), flag_counts(sch, nl),
                     nc_pins(sch), seconds={"netlist": round(dt, 2)}, cli_calls=calls,
                     libsyms={str(x[1]): x for x in sexpr.find_all(sexpr.find(sch_up.tree, "lib_symbols") or [], "symbol")})
        if with_erc:
            ensure_erc(b)
        return b
    except BaseException:
        shutil.rmtree(td, ignore_errors=True)
        raise


def ensure_erc(b: Baseline):
    if b.erc is None:
        t = time.time()
        b.erc = erc_keys(b.root)
        b.seconds["erc"] = round(time.time() - t, 2)
        b.cli_calls += 1


def check(pr, b: Baseline, text: str, sheet_path: str | None = None, canonicalize: bool = True,
          symfix: bool = False) -> GateResult:
    """Prove `text` (new content of the baseline's sheet file) against the baseline. The staged tree is restored
    afterwards, so several candidates can be checked against one baseline. symfix=True admits symfix-derived
    embedded symbols (symfix_pairs) and compares ERC without the library-symbol exemption."""
    original = b.sheet.read_bytes()
    reasons: list = []
    hb = canon_hash(b.canon)
    try:
        b.sheet.write_text(text, encoding="utf-8", newline="\n")
        if canonicalize:
            r = env.run(["sch", "upgrade", "--force", str(b.sheet)], timeout=180)
            b.cli_calls += 1
            if r.returncode != 0:
                return GateResult(False, [f"KiCad rejected the candidate file: {(r.stderr or r.stdout).strip()[-300:]}"], hb, "")
        final = b.sheet.read_text(encoding="utf-8")
        sch = Schematic.load(b.sheet, pr.name, sheet_path)
        derived_ok = None
        if symfix:
            derived_ok, why = symfix_ok(b.libsyms, sch)
            reasons += why
        reasons += payload_diff(b.payload, symbol_payload(sch), derived_ok)
        lm = label_meta(sch)
        for u, v in b.labels.items():
            if u in lm and lm[u][1:] != v[1:]:
                reasons.append(f"G0 label {v[0]}: its fields or style would change")
        if special_items(sch) != b.special:
            reasons.append(f"G2 buses / sheet symbols / directive labels would change: {b.special} -> {special_items(sch)}")
        if reasons:
            return GateResult(False, reasons[:12], hb, "")
        tree = export_tree(b.root)
        b.cli_calls += 1
        try:
            c = canon(tree)
        except ToolError as e:
            return GateResult(False, ["G1 " + e.message], hb, "")
        ha = canon_hash(c)
        reasons += canon_diff(b.canon, c)
        nl = parse_netlist(tree)
        ext = O._external_signature(sch, nl)
        if ext[0] != b.ext[0]:
            for n, k, _ in sorted(b.ext[0] - ext[0], key=str)[:6]:
                reasons.append(f"G2 {k} label {n} would move to other pins or be dropped")
            for n, k, _ in sorted(ext[0] - b.ext[0], key=str)[:6]:
                reasons.append(f"G2 {k} label {n} would be added or moved")
        if ext[1] != b.ext[1] or flag_counts(sch, nl) != b.flags:
            reasons.append("G2 a PWR_FLAG would move to another net or be dropped")
        if ext[2] != b.ext[2]:
            reasons.append(f"G2 items not wired to any pin would change: {b.ext[2][:3]} -> {ext[2][:3]}")
        inv = inventory(sch, nl)
        for net in sorted(set(inv[0]) | set(b.inv[0])):
            x, y = b.inv[0].get(net, set()), inv[0].get(net, set())
            if x != y:
                reasons.append(f"G3 names on net {net}: lost {sorted(n for n, _, _ in x - y)}, new {sorted(n for n, _, _ in y - x)}")
        if inv[1] != b.inv[1]:
            reasons.append(f"G3 names on wires without pins would change: {sorted(str(x[0]) for x in inv[1] ^ b.inv[1])[:6]}")
        if nc_pins(sch) != b.ncs:
            reasons.append("G2 no-connect flags would move")
        if reasons:
            return GateResult(False, reasons[:12], hb, ha)
        if b.erc is None:
            # the "before" ERC must see the ORIGINAL sheet: the staged file holds the candidate right now
            cand = b.sheet.read_bytes()
            b.sheet.write_bytes(original)
            try:
                ensure_erc(b)
            finally:
                b.sheet.write_bytes(cand)
        after = erc_keys(b.root)
        b.cli_calls += 1
        reasons += erc_new(b.erc, after, strict_refs=symfix_refs(sch, derived_ok) if derived_ok else None)
        n_h = len({x for x in b.ext[0] if x[1] == "hierarchical"})
        n_g = len({x for x in b.ext[0] if x[1] == "global"})
        kept = ", ".join(x for x in (f"{sum(b.flags.values())} PWR_FLAG" if b.flags else "",
                                     f"{n_h} hierarchical label(s)" if n_h else "",
                                     f"{n_g} global label(s)" if n_g else "") if x)
        return GateResult(not reasons, reasons[:12], hb, ha, final, nl, erc_counts(b.erc), erc_counts(after), kept)
    finally:
        try:
            b.sheet.write_bytes(original)
        except OSError:
            pass
