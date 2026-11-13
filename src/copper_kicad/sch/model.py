"""In-memory KiCad 10 schematic on top of the lossless S-expression tree.

* :meth:`Schematic.load` / :meth:`Schematic.new` / :meth:`Schematic.save`
* placed symbols as :class:`SymInst` views (ref, value, lib_id, at, rot, mirror,
  unit, fields, pins in world coordinates)
* builders for wires, junctions, labels (local/global/hierarchical), power
  symbols, no-connects and text in KiCad 10 house format
* ``save(canonicalize=True)`` writes atomically, then lets KiCad itself re-save
  the file (``kicad-cli sch upgrade --force``), which is byte-for-byte lossless
  on KiCad-written files and normalises anything we wrote into eeschema's
  canonical form.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy
import json
import os
import re
import shutil
import uuid as _uuid
from pathlib import Path

from .. import sexpr
from ..sexpr import Sym
from ..mcp.registry import ToolError
from ..kicad import env
from ..kicad.libs import parse_pins, unit_bbox
from . import symbols as G

FILE_VERSION = 20260306
GENERATOR_VERSION = "10.0"
PAPER = {"A5": (210, 148), "A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594),
         "A0": (1189, 841), "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8),
         "D": (863.6, 558.8), "E": (1117.6, 863.6), "USLetter": (279.4, 215.9), "USLegal": (355.6, 215.9)}
FONT = 1.27


def new_uuid() -> str:
    return str(_uuid.uuid4())


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def num(v: float):
    """Float → tidy value for the writer (ints stay ints)."""
    v = round(float(v), 4)
    return int(v) if v == int(v) else v


def effects(justify: str | None = None, hide=False, size=FONT) -> list:
    e = [Sym("effects"), [Sym("font"), [Sym("size"), num(size), num(size)]]]
    if justify:
        e.append([Sym("justify"), *[Sym(j) for j in justify.split()]])
    if hide:
        e.append([Sym("hide"), Sym("yes")])
    return e


# Advance widths of KiCad's stroke font in units of the text size, MEASURED from kicad-cli 10 plots (labels of
# repeated glyphs). The old guess (0.78 per capital) was ~20 % short, so every box that reserves room for a net
# label / value text was too small and neighbouring labels ended up end to end ("BOOTSEL USB_DP").
_ADV = {"A": .82, "B": .97, "C": .97, "D": .97, "E": .87, "F": .82, "G": .97, "H": 1.01, "I": .44, "J": .73, "K": .97,
        "L": .78, "M": 1.11, "N": 1.01, "O": 1.01, "P": .97, "Q": 1.01, "R": .97, "S": .92, "T": .73, "U": 1.01,
        "V": .82, "W": 1.11, "X": .92, "Y": .82, "Z": .92,
        "a": .87, "b": .87, "c": .82, "d": .87, "e": .82, "f": .54, "g": .87, "h": .87, "i": .44, "j": .44, "k": .78,
        "l": .49, "m": 1.30, "n": .87, "o": .87, "p": .87, "q": .87, "r": .59, "s": .78, "t": .54, "u": .87, "v": .73,
        "w": 1.01, "x": .78, "y": .73, "z": .78,
        "+": 1.20, "-": 1.20, "=": 1.20, "&": 1.20, "<": 1.20, ">": 1.20, "@": 1.25, "%": 1.11, "/": 1.01, "#": .97,
        "$": .92, "|": .92, "_": .73, "*": .73, "~": .68, "(": .63, ")": .63, "[": .63, "]": .63, "{": .63, "}": .63,
        ".": .44, ",": .44, ":": .44, ";": .44, "'": .44, "!": .44, " ": .73}
LABEL_OFFSET = 0.35     # gap KiCad leaves between a label's anchor and the first glyph


def text_width(s: str, size: float = FONT) -> float:
    """Rendered width of KiCad stroke-font text (digits 0.92, unknown glyphs 0.97 × size). Overbar markup
    '~{RESET}' draws only the name."""
    s = re.sub(r"~\{([^}]*)\}", lambda m: m.group(1), s)
    return sum(_ADV.get(ch, 0.92 if ch.isdigit() else 0.97) for ch in s) * size


class SymInst:
    """View of a placed ``(symbol ...)`` node."""

    def __init__(self, sch: "Schematic", node: list):
        self.sch = sch
        self.node = node

    # basic fields
    @property
    def lib_id(self) -> str:
        return str(sexpr.value(self.node, "lib_id", ""))

    @property
    def lib_name(self) -> str:
        """Key into lib_symbols (lib_name overrides lib_id when present)."""
        return str(sexpr.value(self.node, "lib_name", "") or self.lib_id)

    @property
    def uuid(self) -> str:
        return str(sexpr.value(self.node, "uuid", ""))

    @property
    def at(self) -> list[float]:
        a = sexpr.find(self.node, "at")
        return [_f(a[1]), _f(a[2])]

    @property
    def rot(self) -> int:
        a = sexpr.find(self.node, "at")
        return int(round(_f(a[3]))) % 360 if len(a) > 3 else 0

    @property
    def mirror(self) -> str | None:
        m = sexpr.value(self.node, "mirror")
        return str(m) if m else None

    @property
    def unit(self) -> int:
        return int(_f(sexpr.value(self.node, "unit", 1), 1))

    @property
    def body_style(self) -> int:
        return int(_f(sexpr.value(self.node, "body_style", sexpr.value(self.node, "convert", 1)), 1))

    @property
    def matrix(self):
        return G.matrix(self.rot, self.mirror)

    def field_node(self, name: str):
        for p in sexpr.find_all(self.node, "property"):
            if len(p) > 2 and str(p[1]) == name:
                return p
        return None

    def field(self, name: str, default: str = "") -> str:
        p = self.field_node(name)
        return str(p[2]) if p is not None else default

    @property
    def fields(self) -> dict:
        return {str(p[1]): str(p[2]) for p in sexpr.find_all(self.node, "property") if len(p) > 2}

    @property
    def ref(self) -> str:
        inst = self._instance_path()
        if inst is not None:
            r = sexpr.value(inst, "reference")
            if r:
                return str(r)
        return self.field("Reference")

    @property
    def value(self) -> str:
        return self.field("Value")

    @property
    def is_power(self) -> bool:
        ls = self.sch.lib_symbol(self.lib_name)
        return bool(ls is not None and sexpr.find(ls, "power") is not None) or self.ref.startswith("#")

    @property
    def dnp(self) -> bool:
        return str(sexpr.value(self.node, "dnp", "no")) == "yes"

    def _instance_path(self):
        insts = sexpr.find(self.node, "instances")
        if insts is None:
            return None
        want = self.sch.sheet_path
        first = None
        for proj in sexpr.find_all(insts, "project"):
            for p in sexpr.find_all(proj, "path"):
                if first is None:
                    first = p
                if want and str(p[1]) == want:
                    return p
        return first

    def set_ref(self, ref: str):
        p = self.field_node("Reference")
        if p is not None:
            p[2] = ref
        inst = self._instance_path()
        if inst is not None:
            sexpr.set_value(inst, "reference", ref)

    def set_field(self, name: str, value: str, hide: bool | None = None):
        if name == "Reference":
            return self.set_ref(value)
        p = self.field_node(name)
        if p is None:
            at = self.at
            p = [Sym("property"), name, value, [Sym("at"), num(at[0]), num(at[1]), 0],
                 [Sym("show_name"), Sym("no")], [Sym("do_not_autoplace"), Sym("no")],
                 effects(hide=True if hide is None else hide)]
            # insert after the last property
            idx = max(i for i, e in enumerate(self.node) if isinstance(e, list) and e and e[0] in ("property", "uuid", "dnp"))
            self.node.insert(idx + 1, p)
        else:
            p[2] = value
            if hide is not None:
                _set_hidden(p, hide)

    # geometry
    def lib_pins(self) -> list[dict]:
        return self.sch.lib_pins(self.lib_name)

    def unit_pins(self) -> list[dict]:
        u, s = self.unit, self.body_style
        return [p for p in self.lib_pins() if p["unit"] in (0, u) and p["body_style"] in (0, s)]

    def pins_world(self) -> list[dict]:
        """Pins of the placed unit with world tip + outward direction."""
        at, m = self.at, self.matrix
        out = []
        for p in self.unit_pins():
            q = dict(p)
            q["xy"] = G.pin_tip(at, m, p)
            q["out"] = G.pin_outward(m, p)
            out.append(q)
        return out

    def body_bbox(self) -> list[float]:
        ls = self.sch.lib_symbol(self.lib_name)
        bb = unit_bbox(ls, self.unit, self.body_style) if ls is not None else [-1.27, -1.27, 1.27, 1.27]
        return G.bbox_world(self.at, self.matrix, bb)

    def field_boxes(self) -> list[list[float]]:
        out = []
        for p in sexpr.find_all(self.node, "property"):
            if len(p) < 3 or _is_hidden(p) or not str(p[2]):
                continue
            a = sexpr.find(p, "at")
            if a is None:
                continue
            x, y = _f(a[1]), _f(a[2])
            ang = int(round(_f(a[3]))) % 180 if len(a) > 3 else 0
            vertical = (ang == 90) != (self.rot in (90, 270))
            w = text_width(str(p[2])) + 0.4
            h = FONT + 0.4
            if vertical:
                w, h = h, w
            just = _justify(p)
            if vertical:
                x0, x1 = x - w / 2, x + w / 2
                y0, y1 = (y - h, y) if "left" in just else (y, y + h) if "right" in just else (y - h / 2, y + h / 2)
            else:
                x0, x1 = (x, x + w) if "left" in just else (x - w, x) if "right" in just else (x - w / 2, x + w / 2)
                y0, y1 = (y - h, y) if "bottom" in just else (y, y + h) if "top" in just else (y - h / 2, y + h / 2)
            out.append([x0, y0, x1, y1])
        return out

    def describe(self) -> dict:
        d = {"ref": self.ref, "value": self.value, "lib_id": self.lib_id,
             "footprint": self.field("Footprint"), "at": [num(v) for v in self.at], "rot": self.rot}
        if self.mirror:
            d["mirror"] = self.mirror
        if len({p["unit"] for p in self.lib_pins() if p["unit"]}) > 1:
            d["unit"] = self.unit
        for k in ("LCSC", "MPN"):
            v = self.field(k)
            if v:
                d[k.lower()] = v
        if self.dnp:
            d["dnp"] = True
        return d


def _is_hidden(p) -> bool:
    for e in p[3:]:
        if isinstance(e, list) and e and e[0] == "hide":
            return len(e) == 1 or str(e[1]) == "yes"
        if isinstance(e, list) and e and e[0] == "effects":
            for x in e[1:]:
                if (isinstance(x, list) and x and x[0] == "hide" and (len(x) == 1 or str(x[1]) == "yes")) or x == "hide":
                    return True
    return False


def _set_hidden(p, hide: bool):
    for i, e in enumerate(list(p)):
        if isinstance(e, list) and e and e[0] == "hide":
            p.remove(e)
    eff = sexpr.find(p, "effects")
    if eff is not None:
        for e in list(eff):
            if (isinstance(e, list) and e and e[0] == "hide") or e == "hide":
                eff.remove(e)
    if hide:
        idx = p.index(eff) if eff is not None else len(p)
        p.insert(idx, [Sym("hide"), Sym("yes")])


def _justify(p) -> str:
    eff = sexpr.find(p, "effects")
    j = sexpr.find(eff, "justify") if eff is not None else None
    return " ".join(str(x) for x in j[1:]) if j is not None else ""


class Schematic:
    def __init__(self, tree: list, path: Path | None = None, project_name: str = "", sheet_path: str | None = None):
        self.tree = tree
        self.path = Path(path) if path else None
        self.project_name = project_name
        self._pin_cache: dict[str, list] = {}
        self.sheet_path = sheet_path or f"/{self.uuid}"

    # ── io ────────────────────────────────────────────────────────────────
    @classmethod
    def load(cls, path: Path, project_name: str = "", sheet_path: str | None = None) -> "Schematic":
        path = Path(path)
        try:
            tree = sexpr.loads(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise ToolError(f"cannot read {path}: {e}", code="READ_FAILED")
        except ValueError as e:
            raise ToolError(f"{path.name} is not a valid KiCad schematic: {e}", code="PARSE_FAILED",
                            fix="restore a checkpoint: checkpoint(action='list') then checkpoint(action='restore', id=...)")
        if not tree or tree[0] != "kicad_sch":
            raise ToolError(f"{path.name} is not a .kicad_sch file", code="PARSE_FAILED")
        s = cls(tree, path, project_name or path.stem, sheet_path)
        if sheet_path is None:
            s.sheet_path = s._guess_sheet_path()
        return s

    @classmethod
    def new(cls, path: Path | None = None, project_name: str = "", paper: str = "A4", title: str = "") -> "Schematic":
        u = new_uuid()
        tree = [Sym("kicad_sch"), [Sym("version"), FILE_VERSION], [Sym("generator"), "eeschema"],
                [Sym("generator_version"), GENERATOR_VERSION], [Sym("uuid"), u], [Sym("paper"), paper]]
        if title:
            tree.append([Sym("title_block"), [Sym("title"), title]])
        tree += [[Sym("lib_symbols")],
                 [Sym("sheet_instances"), [Sym("path"), "/", [Sym("page"), "1"]]],
                 [Sym("embedded_fonts"), Sym("no")]]
        return cls(tree, path, project_name or (Path(path).stem if path else ""), f"/{u}")

    def _guess_sheet_path(self) -> str:
        """Instance path used by symbols in this file (root: /<uuid>; sub-sheet: from its symbols)."""
        own = f"/{self.uuid}"
        counts: dict[str, int] = {}
        for n in sexpr.find_all(self.tree, "symbol"):
            insts = sexpr.find(n, "instances")
            if insts is None:
                continue
            for proj in sexpr.find_all(insts, "project"):
                for p in sexpr.find_all(proj, "path"):
                    counts[str(p[1])] = counts.get(str(p[1]), 0) + 1
        if not counts or own in counts:
            return own
        return max(counts, key=counts.get)

    def text(self) -> str:
        return sexpr.dumps(self.tree) + "\n"

    def save(self, path: Path | None = None, canonicalize: bool = True, base_revision: str | None = None) -> dict:
        from .. import project as P
        path = Path(path or self.path)
        try:
            P.atomic_write(path, self.text(), base_revision)
        except PermissionError as e:
            raise ToolError(f"cannot write {path.name}: {e.strerror or e} — the file is read-only or locked by another "
                            f"program (KiCad, a sync client like OneDrive/Dropbox, antivirus)", code="WRITE_FAILED",
                            fix="clear the read-only attribute / pause syncing, then retry the same call") from None
        except OSError as e:
            raise ToolError(f"cannot write {path.name}: {e}", code="WRITE_FAILED",
                            fix="check free disk space and folder permissions, then retry") from None
        info = {"canonicalized": False}
        if canonicalize and live():
            info["live"] = True                      # the app that owns the document re-serialises it
        elif canonicalize:
            info.update(canonicalize_file(path))
        return info

    # ── header ───────────────────────────────────────────────────────────
    @property
    def uuid(self) -> str:
        return str(sexpr.value(self.tree, "uuid", ""))

    @property
    def paper(self) -> str:
        return str(sexpr.value(self.tree, "paper", "A4"))

    def set_paper(self, paper: str):
        sexpr.set_value(self.tree, "paper", paper)

    def page_size(self) -> tuple[float, float]:
        p = sexpr.find(self.tree, "paper")
        if p is not None and len(p) >= 4 and str(p[1]) == "User":
            return _f(p[2]), _f(p[3])
        w, h = PAPER.get(self.paper, (297, 210))
        if p is not None and any(str(x) == "portrait" for x in p[1:]):
            w, h = h, w
        return w, h

    # ── lib symbols ──────────────────────────────────────────────────────
    def _lib_symbols(self) -> list:
        ls = sexpr.find(self.tree, "lib_symbols")
        if ls is None:
            ls = [Sym("lib_symbols")]
            idx = next((i for i, e in enumerate(self.tree) if isinstance(e, list) and e and e[0] in ("title_block", "paper")), 4)
            self.tree.insert(idx + 1, ls)
        return ls

    def lib_symbol(self, name: str):
        idx = self.__dict__.get("_ls_idx")
        if idx is None:
            idx = {}
            for s in sexpr.find_all(self._lib_symbols(), "symbol"):
                idx.setdefault(str(s[1]), s)
            self._ls_idx = idx
        return idx.get(name)

    def lib_pins(self, name: str) -> list[dict]:
        if name not in self._pin_cache:
            ls = self.lib_symbol(name)
            self._pin_cache[name] = parse_pins(ls) if ls is not None else []
        return self._pin_cache[name]

    def embed(self, libsym, replace: bool = False) -> str:
        """Embed a kicad.libs.LibSymbol under its lib_id. Returns the lib_symbols key."""
        key = libsym.lib_id
        cur = self.lib_symbol(key)
        if cur is not None and not replace:
            return key
        node = libsym.embedded()
        ls = self._lib_symbols()
        if cur is not None:
            ls[ls.index(cur)] = node
        else:
            ls.append(node)
        self._pin_cache.pop(key, None)
        self._ls_idx = None
        return key

    def embed_tree(self, key: str, tree: list):
        ls = self._lib_symbols()
        cur = self.lib_symbol(key)
        t = copy.deepcopy(tree)
        t[1] = key
        if cur is not None:
            ls[ls.index(cur)] = t
        else:
            ls.append(t)
        self._pin_cache.pop(key, None)
        self._ls_idx = None

    def prune_lib_symbols(self) -> list[str]:
        used = {s.lib_name for s in self.symbols(include_power=True)}
        ls = self._lib_symbols()
        removed = []
        for s in list(sexpr.find_all(ls, "symbol")):
            if str(s[1]) not in used:
                ls.remove(s)
                removed.append(str(s[1]))
                self._pin_cache.pop(str(s[1]), None)
        self._ls_idx = None
        return removed

    # ── items ─────────────────────────────────────────────────────────────
    def symbols(self, include_power: bool = True) -> list[SymInst]:
        out = [SymInst(self, n) for n in sexpr.find_all(self.tree, "symbol") if sexpr.find(n, "lib_id") is not None]
        if not include_power:
            out = [s for s in out if not s.ref.startswith("#")]
        return out

    def find_ref(self, ref: str) -> list[SymInst]:
        return [s for s in self.symbols() if s.ref == ref]

    def items(self, kind: str) -> list[list]:
        return sexpr.find_all(self.tree, kind)

    def labels(self) -> list[dict]:
        out = []
        for kind in ("label", "global_label", "hierarchical_label"):
            for n in self.items(kind):
                a = sexpr.find(n, "at")
                out.append({"kind": {"label": "local", "global_label": "global", "hierarchical_label": "hierarchical"}[kind],
                            "net": str(n[1]), "at": [_f(a[1]), _f(a[2])], "rot": int(_f(a[3])) if len(a) > 3 else 0,
                            "node": n})
        return out

    def wires(self) -> list[dict]:
        out = []
        for n in self.items("wire"):
            pts = [[_f(xy[1]), _f(xy[2])] for xy in sexpr.find_all(sexpr.find(n, "pts") or [], "xy")]
            if len(pts) >= 2:
                out.append({"a": pts[0], "b": pts[-1], "node": n})
        return out

    def _insert(self, node: list):
        idx = next((i for i, e in enumerate(self.tree) if isinstance(e, list) and e and e[0] in ("sheet_instances", "embedded_fonts")),
                   len(self.tree))
        self.tree.insert(idx, node)
        return node

    def remove(self, node: list) -> bool:
        for i, e in enumerate(self.tree):
            if e is node:
                del self.tree[i]
                return True
        return False

    # ── builders ─────────────────────────────────────────────────────────
    def add_wire(self, a, b) -> list:
        n = [Sym("wire"), [Sym("pts"), [Sym("xy"), num(a[0]), num(a[1])], [Sym("xy"), num(b[0]), num(b[1])]],
             [Sym("stroke"), [Sym("width"), 0], [Sym("type"), Sym("default")]], [Sym("uuid"), new_uuid()]]
        return self._insert(n)

    def add_junction(self, p) -> list:
        n = [Sym("junction"), [Sym("at"), num(p[0]), num(p[1])], [Sym("diameter"), 0],
             [Sym("color"), 0, 0, 0, 0], [Sym("uuid"), new_uuid()]]
        return self._insert(n)

    def add_no_connect(self, p) -> list:
        return self._insert([Sym("no_connect"), [Sym("at"), num(p[0]), num(p[1])], [Sym("uuid"), new_uuid()]])

    def add_label(self, net: str, p, angle: int = 0, kind: str = "local", shape: str = "bidirectional") -> list:
        angle = int(angle) % 360
        if kind == "local":
            just = "left bottom" if angle in (0, 90) else "right bottom"
            n = [Sym("label"), net, [Sym("at"), num(p[0]), num(p[1]), angle],
                 effects(just), [Sym("uuid"), new_uuid()]]
        elif kind == "global":
            just = "left" if angle in (0, 90) else "right"
            n = [Sym("global_label"), net, [Sym("shape"), Sym(shape)], [Sym("at"), num(p[0]), num(p[1]), angle],
                 [Sym("fields_autoplaced"), Sym("yes")], effects(just), [Sym("uuid"), new_uuid()],
                 [Sym("property"), "Intersheetrefs", "${INTERSHEET_REFS}", [Sym("at"), num(p[0]), num(p[1]), 0],
                  [Sym("hide"), Sym("yes")], effects(just)]]
        elif kind == "hierarchical":
            just = "left" if angle in (0, 90) else "right"
            n = [Sym("hierarchical_label"), net, [Sym("shape"), Sym(shape)],
                 [Sym("at"), num(p[0]), num(p[1]), angle], effects(just), [Sym("uuid"), new_uuid()]]
        else:
            raise ToolError(f"label kind must be local/global/hierarchical, got {kind!r}", code="BAD_ARGUMENT")
        return self._insert(n)

    def add_text(self, text: str, p, size: float = FONT, angle: int = 0) -> list:
        n = [Sym("text"), text, [Sym("exclude_from_sim"), Sym("no")], [Sym("at"), num(p[0]), num(p[1]), angle],
             effects("left bottom", size=size), [Sym("uuid"), new_uuid()]]
        return self._insert(n)

    def add_symbol(self, key: str, lib_id: str, ref: str, at, rot: int = 0, mirror: str | None = None,
                   unit: int = 1, value: str | None = None, fields: dict | None = None, dnp: bool = False,
                   body_style: int = 1, lib_name: str | None = None) -> SymInst:
        """Place an instance of an embedded lib symbol (``key`` in lib_symbols)."""
        ls = self.lib_symbol(key)
        if ls is None:
            raise ToolError(f"internal: lib symbol {key} not embedded", code="INTERNAL")
        n = [Sym("symbol")]
        if lib_name and lib_name != lib_id:
            n.append([Sym("lib_name"), lib_name])
        n += [[Sym("lib_id"), lib_id], [Sym("at"), num(at[0]), num(at[1]), int(rot) % 360]]
        if mirror in ("x", "y"):
            n.append([Sym("mirror"), Sym(mirror)])
        is_power = sexpr.find(ls, "power") is not None
        n += [[Sym("unit"), int(unit)], [Sym("body_style"), int(body_style)],
              [Sym("exclude_from_sim"), Sym("no")],
              [Sym("in_bom"), Sym("no" if is_power else str(sexpr.value(ls, "in_bom", "yes")))],
              [Sym("on_board"), Sym("no" if is_power else str(sexpr.value(ls, "on_board", "yes")))],
              [Sym("in_pos_files"), Sym("no" if is_power else str(sexpr.value(ls, "in_pos_files", "yes")))],
              [Sym("dnp"), Sym("yes" if dnp else "no")],
              [Sym("fields_autoplaced"), Sym("yes")],
              [Sym("uuid"), new_uuid()]]
        props = [(str(p[1]), p) for p in sexpr.find_all(ls, "property") if len(p) > 2]
        fields = dict(fields or {})
        seen = set()
        for k, p in props:
            if k.startswith("ki_"):
                continue
            seen.add(k)
            val = ref if k == "Reference" else (value if (k == "Value" and value is not None) else fields.pop(k, str(p[2])))
            if k == "Value" and value is not None:
                fields.pop("Value", None)
            node = [Sym("property"), k, val, [Sym("at"), num(at[0]), num(at[1]), 0],
                    [Sym("show_name"), Sym("no")], [Sym("do_not_autoplace"), Sym("no")]]
            hidden = _is_hidden(p) if k not in ("Reference", "Value") else (is_power and k == "Reference")
            if k == "Value" and is_power:
                hidden = False
            if k in ("Footprint", "Datasheet", "Description"):
                hidden = True
            if hidden:
                node.append([Sym("hide"), Sym("yes")])
            node.append(effects())
            n.append(node)
        for k, v in fields.items():
            if k in seen:
                continue
            n.append([Sym("property"), k, str(v), [Sym("at"), num(at[0]), num(at[1]), 0],
                      [Sym("show_name"), Sym("no")], [Sym("do_not_autoplace"), Sym("no")], [Sym("hide"), Sym("yes")],
                      effects()])
        pins = parse_pins(ls)
        numbers = []
        for p in pins:
            if p["unit"] in (0, unit) and p["body_style"] in (0, body_style) and p["number"] not in numbers:
                numbers.append(p["number"])
        for num_ in numbers:
            n.append([Sym("pin"), num_, [Sym("uuid"), new_uuid()]])
        proj = [Sym("project"), self.project_name,
                [Sym("path"), self.sheet_path, [Sym("reference"), ref], [Sym("unit"), int(unit)]]]
        # a sheet instantiated several times needs a reference per instance path
        taken = self.used_refs() | {ref}
        prefix = re.sub(r"\d+$", "", ref)
        for other in getattr(self, "other_paths", []):
            r2 = ref if ref.startswith("#") else self.next_ref(prefix, taken, path=other)
            taken.add(r2)
            proj.append([Sym("path"), other, [Sym("reference"), r2], [Sym("unit"), int(unit)]])
        n.append([Sym("instances"), proj])
        self._insert(n)
        si = SymInst(self, n)
        autoplace_fields(si)
        return si

    # ── references ───────────────────────────────────────────────────────
    def used_refs(self) -> set[str]:
        """References in use on this sheet plus anywhere else in the hierarchy (``extra_refs``)."""
        return {s.ref for s in self.symbols()} | set(getattr(self, "extra_refs", set()))

    def path_refs(self, path: str | None = None) -> set[str]:
        """References used on this sheet under one instance path (default: the instance being edited)."""
        path = path or self.sheet_path
        out = set()
        for n in sexpr.find_all(self.tree, "symbol"):
            insts = sexpr.find(n, "instances")
            if insts is None:
                if path == self.sheet_path:
                    p = [e for e in sexpr.find_all(n, "property") if len(e) > 2 and str(e[1]) == "Reference"]
                    if p:
                        out.add(str(p[0][2]))
                continue
            for proj in sexpr.find_all(insts, "project"):
                for p in sexpr.find_all(proj, "path"):
                    if str(p[1]) == path:
                        out.add(str(sexpr.value(p, "reference", "")))
        return out

    def ref_block(self, path: str | None = None) -> int | None:
        """Per-sheet annotation block in use (KiCad 'sheet number x 100': R201, C203 … → 2), or None."""
        nums = []
        for r in self.path_refs(path):
            m = re.fullmatch(r"[A-Za-z_]+(\d+)", r)
            if m and not r.startswith("#"):
                nums.append(int(m.group(1)))
        if len(nums) < 3:
            return None
        blocks = [n // 100 for n in nums]
        b = max(set(blocks), key=blocks.count)
        return b if b >= 1 and blocks.count(b) >= 0.75 * len(nums) else None

    def next_ref(self, prefix: str, taken: set[str] | None = None, path: str | None = None) -> str:
        """Next free reference, continuing the sheet's annotation scheme (R214 after R201…R213 on a sheet numbered
        in the 200 block; #PWR0205 after #PWR0201…)."""
        taken = taken if taken is not None else self.used_refs()
        nums = set()
        widths = []
        for r in taken:
            m = re.fullmatch(re.escape(prefix) + r"(\d+)", r)
            if m:
                nums.add(int(m.group(1)))
                widths.append((int(m.group(1)), len(m.group(1))))
        i = 1
        block = self.ref_block(path)
        if block:
            i = block * 100 + 1
            while i in nums and i < (block + 1) * 100:
                i += 1
            if i >= (block + 1) * 100:
                i = 1
        while i in nums:
            i += 1
        if prefix in ("#PWR", "#FLG"):
            w = max((wd for n, wd in widths if block and n // 100 == block), default=2)
            return f"{prefix}{i:0{w}d}"
        return f"{prefix}{i}"


def autoplace_fields(si: SymInst):
    """Place visible Reference/Value (+ other visible fields) beside the body, horizontal,
    like eeschema's autoplacer: right of tall/narrow parts, above/below wide parts."""
    bb = si.body_bbox()
    pins = si.pins_world()
    vis = []
    for p in sexpr.find_all(si.node, "property"):
        if len(p) < 3:
            continue
        if not _is_hidden(p) and str(p[2]):
            vis.append(p)
    # field angle such that text is drawn horizontally on the sheet
    fang = 90 if si.rot in (90, 270) else 0
    w = bb[2] - bb[0]
    h = bb[3] - bb[1]
    occupied_sides = {G.side_of(p["out"]) for p in pins}
    is_power = si.is_power
    if is_power:
        # power symbols: value text beyond the graphic, opposite the pin
        for p in vis:
            a = sexpr.find(p, "at")
            if not pins:
                continue
            o = pins[0]["out"]  # outward from pin = towards the wire; text goes opposite
            cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            tip = pins[0]["xy"]
            dx, dy = cx - tip[0], cy - tip[1]
            if abs(dx) >= abs(dy) and abs(dx) > 0.01:
                tw = text_width(str(p[2]))
                x = (bb[2] + 0.6 + tw / 2) if dx > 0 else (bb[0] - 0.6 - tw / 2)
                y = tip[1]
            else:
                x = tip[0]
                y = (bb[3] + 1.4) if dy > 0 else (bb[1] - 1.4)
            a[1:] = [num(G.snap(x, 0.0254)), num(G.snap(y, 0.0254)), fang]
            _set_justify(p, None)
        return
    lines = [p for p in vis]
    if not lines:
        return
    widths = [text_width(str(p[2])) for p in lines]

    def put(p, x, y):
        a = sexpr.find(p, "at")
        a[1:] = [num(G.snap(x, 0.0254)), num(G.snap(y, 0.0254)), fang]
        _set_justify(p, None)

    if {"left", "right", "top", "bottom"} <= occupied_sides or (
            "top" in occupied_sides and "bottom" in occupied_sides and "right" in occupied_sides and w > 12):
        # pins on every side: use the free corners (outside the pin rows)
        ls = si.sch.lib_symbol(si.lib_name)
        gb = G.bbox_world(si.at, si.matrix, unit_bbox(ls, si.unit, si.body_style, include_pins=False)) if ls is not None else bb
        tops = [p for p in pins if G.side_of(p["out"]) == "top"]
        bots = [p for p in pins if G.side_of(p["out"]) == "bottom"]
        xr = max([gb[2]] + [p["xy"][0] for p in tops + bots]) + 1.0
        put(lines[0], xr + widths[0] / 2, gb[1] - 1.3)
        y = gb[3] + 1.3
        for p, tw in zip(lines[1:], widths[1:]):
            put(p, xr + tw / 2, y)
            y += 1.9
    elif ("right" not in occupied_sides) and (h >= w * 0.6 or "top" in occupied_sides and "bottom" in occupied_sides):
        # stack to the right of the body, vertically centred
        gb_w = (lambda g: g[2] - g[0])(unit_bbox(si.sch.lib_symbol(si.lib_name), si.unit, si.body_style, include_pins=False))             if si.sch.lib_symbol(si.lib_name) is not None else 9
        x0 = bb[2] + (2.4 if gb_w < 2.6 else 0.8)   # bare pin-only units (op-amp power unit): clear the pin names
        total = len(lines) * 1.9
        y = (bb[1] + bb[3]) / 2 - total / 2 + 0.95
        for p, tw in zip(lines, widths):
            a = sexpr.find(p, "at")
            a[1:] = [num(G.snap(x0 + tw / 2, 0.0254)), num(G.snap(y, 0.0254)), fang]
            _set_justify(p, None)
            y += 1.9
    else:
        # reference above the body, the rest below (or all above if bottom is busy)
        cx = (bb[0] + bb[2]) / 2
        above = [lines[0]] if "top" not in occupied_sides or "bottom" in occupied_sides else []
        below = lines[1:] if above else lines
        if "top" in occupied_sides and "bottom" not in occupied_sides:
            above, below = [], lines
        elif "bottom" in occupied_sides and "top" not in occupied_sides:
            above, below = lines, []
        y = bb[1] - 1.2 - (len(above) - 1) * 1.9
        for p in above:
            a = sexpr.find(p, "at")
            a[1:] = [num(G.snap(cx, 0.0254)), num(G.snap(y, 0.0254)), fang]
            _set_justify(p, None)
            y += 1.9
        y = bb[3] + 1.4
        for p in below:
            a = sexpr.find(p, "at")
            a[1:] = [num(G.snap(cx, 0.0254)), num(G.snap(y, 0.0254)), fang]
            _set_justify(p, None)
            y += 1.9
    # hidden fields sit on the symbol origin
    for p in sexpr.find_all(si.node, "property"):
        if len(p) > 2 and (_is_hidden(p) or not str(p[2])):
            a = sexpr.find(p, "at")
            a[1:] = [num(si.at[0]), num(si.at[1]), fang]


def _set_justify(p, just: str | None):
    eff = sexpr.find(p, "effects")
    if eff is None:
        return
    for e in list(eff):
        if isinstance(e, list) and e and e[0] == "justify":
            eff.remove(e)
    if just:
        eff.append([Sym("justify"), *[Sym(j) for j in just.split()]])


# ── live mode ─────────────────────────────────────────────────────────────
# An edit made for a LIVE HOST (the app has the document open; host/live.py runs the engine on a shadow copy and
# sends the app a uuid patch) needs neither the kicad-cli re-save nor a file ERC: the app is the canonicaliser and
# the verifier. In live mode Schematic.save() writes Copper's own s-expression text and stops, and the schematic
# tools skip ERC (tools/sch_tools._erc). The netlist connectivity check still runs: it is the engine's proof that
# the ops did what was asked. Scope: the current thread / context (host/live.py), or the whole process with
# COPPER_LIVE=1.
_LIVE = contextvars.ContextVar("copper_live", default=None)


def live() -> bool:
    """True while the engine is editing a shadow checkout for a live host."""
    v = _LIVE.get()
    return bool(v) if v is not None else os.environ.get("COPPER_LIVE") == "1"


@contextlib.contextmanager
def live_mode(on: bool = True):
    """``with live_mode(): ...``: saves inside skip the kicad-cli re-save and ERC (see above)."""
    token = _LIVE.set(bool(on))
    try:
        yield
    finally:
        _LIVE.reset(token)


# ── canonical save via KiCad ──────────────────────────────────────────────
def canonicalize_file(path: Path) -> dict:
    """Let eeschema (kicad-cli) re-save the file in its canonical KiCad 10 format.

    The project file must sit next to the root sheet so instance data keeps the
    project name; if it is missing we create it from KiCad's template first."""
    path = Path(path)
    pro = path.with_suffix(".kicad_pro")
    if not pro.exists() and not any(path.parent.glob("*.kicad_pro")):
        # A sub-sheet kept in a sub-folder (sch/power.kicad_sch) belongs to the project in a folder above:
        # creating sch/power.kicad_pro there would litter the user's project with a second, bogus project.
        from ..kicad import compat as _compat
        owner = _compat._project_dir_of(str(path))
        if not any(Path(owner).glob("*.kicad_pro")):
            ensure_project_file(pro)
    before = path.read_text(encoding="utf-8")
    try:
        r = env.run(["sch", "upgrade", "--force", str(path)], timeout=120)
    except ToolError as e:
        return {"canonicalized": False, "canonicalize_error": e.message}
    if r.returncode != 0:
        # KiCad refused the file — keep ours but report it loudly: it means our output is invalid
        path.write_text(before, encoding="utf-8", newline="\n")
        raise ToolError(f"KiCad rejected the written schematic (kicad-cli exit {r.returncode}): {(r.stderr or r.stdout).strip()[-600:]}",
                        code="KICAD_REJECTED", fix="this is a Copper bug — restore the last checkpoint and report it")
    return {"canonicalized": True}


def ensure_project_file(pro: Path) -> bool:
    if pro.exists():
        return False
    try:
        inst = env.kicad()
        tmpl = inst.share / "template" / "kicad.kicad_pro" if inst.share else None
    except ToolError:
        tmpl = None
    if tmpl and tmpl.exists():
        data = json.loads(tmpl.read_text(encoding="utf-8"))
    else:
        data = {"meta": {"filename": pro.name, "version": 3}, "schematic": {}, "board": {}}
    data.setdefault("meta", {})["filename"] = pro.name
    pro.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True
