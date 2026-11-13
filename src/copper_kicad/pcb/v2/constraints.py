"""Electrical constraints of router v2 (design §14): model, detection, measurement, report.

    read_project(pr)                       .kicad_dru rules + .kicad_pro settings + stackup / pad-to-die of the board file
    resolve(board, cfg, x, proj)           plan > KiCad rule > net class > net name  ->  Constraints
    preview(cons)                          what was found, for ``auto.constraints`` (pcb_route_plan, route result)
    net_length(ctx, net)                   a net's length exactly as KiCad's DRC counts it (§14.5, measured §14.9)
    coupled_stats(segs_p, segs_n, ...)     how much of a pair runs side by side at its gap (geometry)
    kicad_markers(raw)                     KiCad's five constraint marker types, parsed
    measure(ctx, kicad)                    the ``constraints`` block of a route result (§14.10)

Nothing here lays, moves or removes copper, and nothing calls a model: a plan carries the caller's heuristics
(``planx`` validates them into ``V2Cfg.cons_plan``); what the plan leaves out is detected deterministically from
net names, KiCad net classes and the project's custom rules. No clock, no randomness.

Imports from v2: ``types``; ``oracle.dp_complement`` (KiCad's pair-name rule). From v1: ``guided.plan`` (names).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

KICAD_TYPES = ("length_out_of_range", "skew_out_of_range", "diff_pair_gap_out_of_range",
               "diff_pair_uncoupled_length_too_long", "too_many_vias")
SOURCES = ("plan", "dru", "class", "name")          # precedence, strongest first
MODES = ("auto", "plan", "report", "off")
OURS = ("length", "skew", "diff_pair_gap", "diff_pair_uncoupled", "via_count", "track_width")
COUPLE_CONF = 0.5         # a name-detected pair is treated as one object from this confidence up
GAP_TOL = 0.02            # mm: a lane counts as coupled within the gap window widened by this much
PAR_DRIFT = 0.0005        # mm: two segments are parallel when they drift apart by less than this over the shorter one
PAR_MAX = 0.02            # ... and never when the sine of the angle between them is above this
LANES_PCT = 50.0          # copper from before the run counts as coupled lanes from this share of the longer net
PREVIEW_MAX = 12          # rows per kind in auto.constraints
VIA_GAP = 0.25            # KiCad's default diff_pair_via_gap, mm
COPPER_T, MASK_T, BOARD_T = 0.035, 0.01, 1.6      # KiCad's default stackup figures, mm


# ── model (design §14.2) ──────────────────────────────────────────────────
@dataclass(frozen=True)
class LenGroup:
    """Nets whose lengths belong together. Checks, all of which must hold for a member of length L:
       tol > 0 and target is None : L >= longest member - tol            (KiCad's skew semantics)
       tol > 0 and target given   : target - tol <= L <= target + tol    (plan ``len``)
       lo / hi                    : lo <= L <= hi                        (a KiCad ``length (min ..)(max ..)`` rule)
    ``tol == 0`` marks a pure window group (only lo / hi count; ``target`` is then the rule's ``opt``)."""
    name: str
    nets: tuple
    tol: float
    target: float | None = None
    lo: float | None = None
    hi: float | None = None
    amp: float | None = None
    mode: str = "tune"            # "tune" | "report"
    src: str = "plan"
    conf: float = 1.0
    why: str = ""


@dataclass(frozen=True)
class Pair:
    p: str
    n: str
    width: float
    gap: float                    # copper edge to copper edge
    via_gap: float = VIA_GAP
    skew: float | None = None     # max |len(p) - len(n)|; None = measured and reported only
    uncoupled: float | None = None
    gap_lo: float | None = None   # KiCad diff_pair_gap rule window, when one exists
    gap_hi: float | None = None
    coupled: bool = False         # True = to be routed as one object; False = sequential (today's behaviour)
    kicad: bool = True            # KiCad sees the names as a pair (oracle.dp_complement): its pair rules apply
    z: float | None = None
    src: str = "name"
    conf: float = 1.0
    why: str = ""


@dataclass(frozen=True)
class WidthRule:
    net: str
    by_layer: tuple = ()          # ((layer index, mm), ...); () = one width on every layer
    width: float = 0.0            # the width where by_layer says nothing
    neck: bool = False
    z: float | None = None
    src: str = "plan"
    why: str = ""


@dataclass(frozen=True)
class Short:
    net: str
    max_len: float | None = None
    hard: bool = False
    src: str = "name"
    why: str = ""


@dataclass(frozen=True)
class Constraints:
    mode: str = "auto"
    groups: tuple = ()
    pairs: tuple = ()
    widths: dict = field(default_factory=dict)       # net -> WidthRule
    short: dict = field(default_factory=dict)        # net -> Short
    max_vias: dict = field(default_factory=dict)     # net -> int (KiCad via_count rule)
    unread: tuple = ()            # KiCad rules with a constraint of ours and a condition we cannot read
    notes: tuple = ()

    def group_of(self, net) -> LenGroup | None:
        for g in self.groups:
            if net in g.nets:
                return g
        return None

    def pair_of(self, net) -> Pair | None:
        for p in self.pairs:
            if net == p.p or net == p.n:
                return p
        return None

    def width_on(self, net, layer) -> float | None:
        r = self.widths.get(net)
        if r is None:
            return None
        for l, w in r.by_layer:
            if l == layer:
                return w
        return r.width or None

    def layered(self, net) -> bool:
        """Does ``net`` carry a per-layer width rule the router lays (not a class width it only reports)?"""
        r = self.widths.get(net)
        return bool(r is not None and not r.neck and r.by_layer and self.mode in ("auto", "plan"))

    def lay_width(self, net, layer, full: float) -> float:
        """The width copper of ``net`` is LAID at on ``layer`` when the connection's full width is ``full``:
        the layer's own width under a per-layer width rule (plan ``nets[..].widths``, a KiCad ``track_width``
        rule with a layer) — the rule's base width on a layer it does not list — else ``full``. Every stage
        that lays full-width copper asks this (fan-out stubs, detail, the fallback search), so a layer the rule
        does not name never carries the widest layer's width."""
        if not self.layered(net):
            return full
        r = self.widths[net]
        if abs(max([r.width] + [w for _l, w in r.by_layer]) - full) > 1e-9:
            return full                   # not this rule's width (a pair's centre line, a necked rung)
        for l, w in r.by_layer:
            if l == layer:
                return w
        return r.width if r.width else full

    def tuned(self) -> tuple:
        """Groups that may get copper: mode 'tune' and the plan did not switch to report / off."""
        if self.mode not in ("auto", "plan"):
            return ()
        return tuple(g for g in self.groups if g.mode == "tune")

    def coupled_pairs(self) -> tuple:
        if self.mode not in ("auto", "plan"):
            return ()
        return tuple(p for p in self.pairs if p.coupled)

    def skew_pairs(self) -> tuple:
        """Pairs whose skew limit the tuner may work to (shallow bumps on the shorter net)."""
        if self.mode not in ("auto", "plan"):
            return ()
        return tuple(p for p in self.pairs if p.skew is not None and (self.mode == "auto" or p.src == "plan"))

    def any(self) -> bool:
        return bool(self.groups or self.pairs or self.widths or self.short or self.max_vias or self.unread)


# ── KiCad custom rules (.kicad_dru), the subset §14.4 reads ───────────────
@dataclass(frozen=True)
class DruRule:
    name: str
    cond: str | None              # the condition text; None = no condition (every item)
    layer: str | None
    severity: str | None
    cons: tuple                   # ((kind, {'min': mm, 'opt': mm, 'max': mm}, (flag, ...)), ...)
    time_domain: bool = False     # a value carries a time unit (KiCad 10 delay rules): not read


@dataclass
class ProjectRules:
    rules: list = field(default_factory=list)
    classes: dict = field(default_factory=dict)      # class name -> {'width', 'dp_width', 'dp_gap', 'dp_via_gap'}
    use_height: bool = True
    tuning: dict = field(default_factory=dict)       # {'single': {min_amplitude, max_amplitude, spacing}, 'skew': {...}}
    severities: dict = field(default_factory=dict)
    profiles: bool = False        # KiCad 10 tuning profiles (time-domain tuning) are in use: not read
    stack: "Stackup | None" = None
    die: dict = field(default_factory=dict)          # net -> pad-to-die mm (sum over its pads)
    notes: list = field(default_factory=list)


_UNIT = {"mm": 1.0, "": 1.0, "mil": 0.0254, "in": 25.4, "um": 0.001, "µm": 0.001, "thou": 0.0254}
_VAL = re.compile(r"^(-?\d+(?:\.\d*)?|-?\.\d+)\s*([A-Za-zµ]*)$")


def _sexpr(text: str) -> list:
    """A small s-expression reader: nested lists of strings. ``#`` starts a comment line."""
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    out, stack = [], []
    cur = out
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "(":
            new = []
            cur.append(new)
            stack.append(cur)
            cur = new
            i += 1
        elif ch == ")":
            if stack:
                cur = stack.pop()
            i += 1
        elif ch == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(text[j])
                j += 1
            cur.append("".join(buf))
            i = j + 1
        elif ch.isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            cur.append(text[i:j])
            i = j
    return out


def _value(tok) -> tuple:
    """'0.5mm' -> (0.5, False); a time value -> (None, True); unreadable -> (None, False)."""
    m = _VAL.match(str(tok).strip())
    if not m:
        return None, False
    unit = m.group(2).lower()
    if unit in ("ps", "fs", "ns"):
        return None, True
    if unit not in _UNIT:
        return None, False
    return float(m.group(1)) * _UNIT[unit], False


def parse_dru(text: str) -> list[DruRule]:
    """Every ``(rule ...)`` of a .kicad_dru text, in file order (later rules win in KiCad)."""
    rules = []
    for node in _sexpr(text or ""):
        if not (isinstance(node, list) and node and node[0] == "rule" and len(node) >= 2):
            continue
        name = node[1] if isinstance(node[1], str) else "?"
        cond = layer = sev = None
        cons, timed = [], False
        for part in node[2:]:
            if not (isinstance(part, list) and part):
                continue
            if part[0] == "condition" and len(part) > 1 and isinstance(part[1], str):
                cond = part[1]
            elif part[0] == "layer" and len(part) > 1 and isinstance(part[1], str):
                layer = part[1]
            elif part[0] == "severity" and len(part) > 1 and isinstance(part[1], str):
                sev = part[1]
            elif part[0] == "constraint" and len(part) > 1 and isinstance(part[1], str):
                vals, flags = {}, []
                for a in part[2:]:
                    if isinstance(a, list) and len(a) == 2 and a[0] in ("min", "opt", "max") and isinstance(a[1], str):
                        v, t = _value(a[1])
                        timed = timed or t
                        if v is not None:
                            vals[a[0]] = v
                    elif isinstance(a, list) and a and isinstance(a[0], str):
                        flags.append(a[0])
                    elif isinstance(a, str):
                        flags.append(a)
                cons.append((part[1], vals, tuple(flags)))
        rules.append(DruRule(name=name, cond=cond, layer=layer, severity=sev, cons=tuple(cons), time_domain=timed))
    return rules


_T_NAME = re.compile(r"^A\.NetName\s*==\s*'([^']*)'$")
_T_CLASS = re.compile(r"^A\.NetClass\s*==\s*'([^']*)'$")
_T_HAS = re.compile(r"^A\.hasNetclass\(\s*'([^']*)'\s*\)$", re.I)
_T_PAIR = re.compile(r"^A\.inDiffPair\(\s*'([^']*)'\s*\)$", re.I)
_T_TYPE = re.compile(r"^A\.Type\s*[!=]=\s*'[^']*'$", re.I)


def _strip_parens(s: str) -> str:
    s = s.strip()
    while s.startswith("(") and s.endswith(")"):
        depth, ok = 0, True
        for i, ch in enumerate(s):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(s) - 1:
                ok = False
                break
        if not ok:
            break
        s = s[1:-1].strip()
    return s


def _split_top(s: str, op: str) -> list[str]:
    out, depth, quote, start, i = [], 0, False, 0, 0
    while i < len(s):
        ch = s[i]
        if ch == "'":
            quote = not quote
        elif not quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and s.startswith(op, i):
                out.append(s[start:i])
                i += len(op)
                start = i
                continue
        i += 1
    out.append(s[start:])
    return [t.strip() for t in out]


def parse_condition(cond: str | None):
    """-> [(kind, pattern), ...] with kind in name | class | pair | all, or None when the condition uses anything
    outside the subset (the caller then lists the rule as not read; KiCad still checks it)."""
    if cond is None or not cond.strip():
        return [("all", "*")]
    sels = []
    for term in _split_top(_strip_parens(cond), "||"):
        sel = None
        for part in _split_top(_strip_parens(term), "&&"):
            part = _strip_parens(part)
            if _T_TYPE.match(part):
                continue
            hit = None
            for kind, rx in (("name", _T_NAME), ("class", _T_CLASS), ("class", _T_HAS), ("pair", _T_PAIR)):
                m = rx.match(part)
                if m:
                    hit = (kind, m.group(1))
                    break
            if hit is None or sel is not None:
                return None                   # an unknown test, or two net tests and-ed together
            sel = hit
        sels.append(sel or ("all", "*"))
    return sels


_WILD: dict = {}


def wild(pat: str, s: str) -> bool:
    """KiCad's string match: ``*`` and ``?`` wildcards, case-insensitive."""
    rx = _WILD.get(pat)
    if rx is None:
        rx = _WILD[pat] = re.compile("".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pat) + r"\Z",
                                     re.I | re.S)
    return rx.match(s) is not None


def dp_base(name: str):
    """(base name, polarity +1 | -1) by KiCad's suffix rule, or (None, 0)."""
    i = len(name) - 1
    while i >= 0:
        ch = name[i]
        if ch in "0123456789_":
            i -= 1
            continue
        if ch in "+P":
            return name[:i], 1
        if ch in "-N":
            return name[:i], -1
        return None, 0
    return None, 0


def select(sels, classes: dict) -> set:
    """Nets (keys of ``classes``: net name -> class string) a parsed condition matches."""
    from .oracle import dp_complement
    out = set()
    for kind, pat in sels:
        for n, cls in classes.items():
            if n in out:
                continue
            if kind == "all":
                out.add(n)
            elif kind == "name":
                if wild(pat, n):
                    out.add(n)
            elif kind == "class":
                if wild(pat, cls or "") or any(wild(pat, c.strip()) for c in (cls or "").split(",")):
                    out.add(n)
            elif kind == "pair":
                base, pol = dp_base(n)
                if pol and dp_complement(n) in classes and (
                        wild(pat, base) or (base.endswith("_") and wild(pat, base[:-1]))):
                    out.add(n)
    return out


# ── stackup and pad-to-die, from the board file text ──────────────────────
class Stackup:
    """Copper and dielectric thicknesses from top to bottom. ``height(a, b)`` is what KiCad adds to a net's
    length for a via between copper layers a and b (board layer indexes): everything strictly between, plus the
    full thickness of an end layer that is an outer layer and half of one that is an inner layer (measured on
    KiCad 10.0: F-In1 0.5325, F-In2 1.0475, F-B 1.58, In1-In2 0.515 mm on the default 4-layer stack)."""

    def __init__(self, items: list, explicit: bool = False):
        self.items = items                 # [('cu', thickness, None) | ('di', thickness, epsilon_r | None)], top first
        self.explicit = explicit
        self.n = sum(1 for it in items if it[0] == "cu")

    @classmethod
    def default(cls, layers: int, thickness: float = BOARD_T) -> "Stackup":
        layers = max(1, int(layers))
        if layers == 1:
            return cls([("cu", COPPER_T, None)])
        di = max(0.0, thickness - 2 * MASK_T - layers * COPPER_T) / (layers - 1)
        items = []
        for i in range(layers):
            items.append(("cu", COPPER_T, None))
            if i < layers - 1:
                items.append(("di", di, None))
        return cls(items)

    @classmethod
    def from_text(cls, txt: str, layers: int) -> "Stackup":
        """The board file's ``(setup (stackup ...))`` when it lists exactly ``layers`` copper layers, else KiCad's
        default for the board thickness in ``(general (thickness ...))``."""
        m = re.search(r"\(general\b[^()]*(?:\([^()]*\)[^()]*)*?\(thickness\s+([\d.]+)\)", txt[:4000])
        thick = float(m.group(1)) if m else BOARD_T
        i = txt.find("(stackup")
        if i < 0:
            return cls.default(layers, thick)
        depth, j = 0, i
        while j < len(txt):
            if txt[j] == "(":
                depth += 1
            elif txt[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        body = txt[i:j]
        items = []
        for lm in re.finditer(r'\(layer\s+"([^"]*)"', body):
            name = lm.group(1)
            k, d = lm.start(), 0
            while k < len(body):
                if body[k] == "(":
                    d += 1
                elif body[k] == ")":
                    d -= 1
                    if d == 0:
                        break
                k += 1
            blk = body[lm.start():k]
            th = [float(v) for v in re.findall(r"\(thickness\s+([\d.]+)", blk)]
            if name.endswith(".Cu"):
                items.append(("cu", th[0] if th else COPPER_T, None))
            elif name.lower().startswith("dielectric"):
                er = re.search(r"\(epsilon_r\s+([\d.]+)", blk)
                items.append(("di", sum(th), float(er.group(1)) if er else None))
        st = cls(items, explicit=True)
        if st.n != layers or not items or items[0][0] != "cu" or items[-1][0] != "cu":
            return cls.default(layers, thick)
        return st

    def height(self, a: int, b: int) -> float:
        if a == b:
            return 0.0
        a, b = (a, b) if a < b else (b, a)
        total, cu = 0.0, -1
        for kind, t, _er in self.items:
            if kind == "cu":
                cu += 1
                if cu < a or cu > b:
                    continue
                if cu in (a, b):
                    total += t if cu in (0, self.n - 1) else t / 2.0
                else:
                    total += t
            elif a <= cu < b:
                total += t
        return total


_PAD_NET = re.compile(r'\(net\s+(?:\d+\s+)?"((?:[^"\\]|\\.)*)"\)')


def die_lengths(txt: str) -> dict:
    """{net: pad-to-die length mm, summed over its pads} from ``(die_length ...)`` in the board file text."""
    out = {}
    for m in re.finditer(r"\(die_length\s+(-?[\d.]+)\)", txt):
        v = float(m.group(1))
        if v <= 0:
            continue
        i = txt.rfind("(pad ", 0, m.start())
        if i < 0:
            continue
        depth, j = 0, i
        while j < len(txt):
            if txt[j] == "(":
                depth += 1
            elif txt[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j < m.start():
            continue                         # the die length is not inside that pad
        nm = _PAD_NET.search(txt, i, j)
        if nm:
            name = nm.group(1).replace('\\"', '"').replace("\\\\", "\\")
            out[name] = round(out.get(name, 0.0) + v, 6)
    return out


def _impedance(cls: str | None) -> float | None:
    """90ohm, 100R, Z50, 50R0 ... -> ohms; None when the class name is not an impedance."""
    if not cls:
        return None
    m = re.search(r"(?<![A-Za-z0-9])(\d{2,3})\s*(?:ohms?|Ω|R)(?![A-Za-z1-9])", cls, re.I) or re.search(
        r"(?<![A-Za-z0-9])Z0?[_=]?(\d{2,3})(?![0-9])", cls, re.I)
    if not m:
        return None
    z = float(m.group(1))
    return z if 10.0 <= z <= 300.0 else None


def read_project_files(pro: Path | None, dru: Path | None, pcb: Path | None, layers: int = 2) -> ProjectRules:
    out = ProjectRules()
    try:
        out.rules = parse_dru(Path(dru).read_text(encoding="utf-8", errors="replace")) if dru else []
    except OSError:
        out.rules = []
    d = {}
    try:
        if pro:
            d = json.loads(Path(pro).read_text(encoding="utf-8", errors="replace") or "{}")
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    ns = d.get("net_settings") if isinstance(d.get("net_settings"), dict) else {}
    for c in ns.get("classes") or []:
        if isinstance(c, dict) and c.get("name"):
            out.classes[str(c["name"])] = {"width": c.get("track_width"), "dp_width": c.get("diff_pair_width"),
                                           "dp_gap": c.get("diff_pair_gap"), "dp_via_gap": c.get("diff_pair_via_gap")}
            if c.get("tuning_profile"):
                out.profiles = True
    ds = ((d.get("board") or {}).get("design_settings") or {}) if isinstance(d.get("board"), dict) else {}
    rules = ds.get("rules") if isinstance(ds.get("rules"), dict) else {}
    if rules.get("use_height_for_length_calcs") is False:
        out.use_height = False
    tp = ds.get("tuning_pattern_settings") if isinstance(ds.get("tuning_pattern_settings"), dict) else {}
    for key, src in (("single", "single_track_defaults"), ("skew", "diff_pair_skew_defaults")):
        t = tp.get(src)
        if isinstance(t, dict):
            out.tuning[key] = {k: t[k] for k in ("min_amplitude", "max_amplitude", "spacing")
                               if isinstance(t.get(k), (int, float))}
    sev = ds.get("rule_severities")
    if isinstance(sev, dict):
        out.severities = {k: v for k, v in sev.items() if k in KICAD_TYPES}
    prof = d.get("tuning_profiles")
    if isinstance(prof, dict) and any(isinstance(v, list) and v for v in prof.values()):
        out.profiles = True
    if out.profiles:
        out.notes.append("the project uses KiCad tuning profiles (time-domain tuning): not read, lengths are in mm")
    txt = ""
    try:
        if pcb:
            txt = Path(pcb).read_text(encoding="utf-8", errors="replace")
    except OSError:
        out.notes.append("could not read the board file for its stackup / pad-to-die lengths: KiCad's defaults are used")
    out.stack = Stackup.from_text(txt, layers) if txt else Stackup.default(layers)
    out.die = die_lengths(txt) if txt else {}
    return out


def read_project(pr, layers: int = 2) -> ProjectRules:
    """Everything detection and measurement need from the project directory (``pr.dir``, ``pr.name``, ``pr.pcb``).
    A project without a directory (tests) gives KiCad's defaults and no rules."""
    d, name = getattr(pr, "dir", None), getattr(pr, "name", None)
    if d is None or not name:
        return read_project_files(None, None, getattr(pr, "pcb", None), layers)
    d = Path(d)
    return read_project_files(d / f"{name}.kicad_pro", d / f"{name}.kicad_dru", getattr(pr, "pcb", None), layers)


# ── detection (design §14.4) ──────────────────────────────────────────────
# kind, stem must contain (regex | None = any non-empty stem | "" = any stem), member, clock, other members,
# fewest members, suggested tolerance mm (a typical value, not taken from an interface standard), confidence
NECK_REACH = 3.0          # mm: a stretch thinner than its class width counts as a neck when both its ends are this near a pad
DETOUR = (1.3, 1.0)       # a soft keep-short net is flagged as a detour above 1.3 x its estimate + 1 mm


NAME_GROUPS = (
    ("sd", r"(SD|MMC)", r"(D|DAT|DATA)[0-7]", r"(CLK|CK|SCK)", r"CMD", 4, 2.0, 0.6),
    ("qspi", None, r"(IO|SIO|DQ)[0-7]", r"(CLK|SCK|SCLK)", None, 4, 2.0, 0.6),
    ("qspi", r"(QSPI|OSPI|QUADSPI|FLASH)", r"D[0-7]", r"(CLK|SCK|SCLK)", None, 4, 2.0, 0.6),
    ("rgmii_tx", "", r"TXD[0-3]", r"(TXC|TX_CLK|TXCLK|GTX_CLK|GTXCLK)", r"(TX_CTL|TXCTL|TX_EN|TXEN)", 2, 1.0, 0.6),
    ("rgmii_rx", "", r"RXD[0-3]", r"(RXC|RX_CLK|RXCLK)", r"(RX_CTL|RXCTL|RX_DV|RXDV)", 2, 1.0, 0.6),
    ("ddr_addr", r"(DDR|SDRAM|DRAM|MEM)", r"(A\d{1,2}|BA[0-2])", r"(CK|CLK)(_?[PNTC])?", r"(RAS|CAS|WE|CS|CKE|ODT)N?",
     8, 1.0, 0.5),
    ("ulpi", r"ULPI", r"(D|DATA)[0-7]", r"CLK", r"(DIR|NXT|STP)", 4, 2.0, 0.6),
    ("video", None, r"[RGBD]\d{1,2}", r"(PCLK|DCLK|PIXCLK|DOTCLK)", r"(HSYNC|VSYNC|DE)", 4, 2.5, 0.5),
)
BUS_TOL, BUS_CONF, BUS_MIN = 2.5, 0.4, 4          # generic: >= 4 numbered members + exactly one clock on one stem
_BUS_MEMBER = re.compile(r"([A-Z]{1,4})(\d{1,2})\Z")
_BUS_CLOCK = re.compile(r"(CLK|CK|SCK|SCLK|CLOCK)\Z")
_DDR_DQ = re.compile(r"DQ(\d{1,2})\Z")
_DDR_STROBE = re.compile(r"DQS(\d?)(?:_?[PNTC])?\Z")
_DDR_MASK = re.compile(r"(?:DM|DQM)(\d?)\Z")
DDR_TOL, DDR_CONF = 0.5, 0.6
_SWITCH = re.compile(r"(?<![A-Za-z0-9])(SW|LX|PH\d?)(?![A-Za-z])", re.I)
_RF = re.compile(r"(?<![A-Za-z0-9])(ANT|RF)(?![A-Za-z])", re.I)


def _stem_tokens(name: str) -> list[tuple]:
    """Ways to read a net name as (stem, member token): the last one or two parts after ``_ . /``."""
    cut = name.rfind("/") + 1
    path, base = name[:cut], name[cut:]
    parts = re.split(r"([_.])", base)
    out = [(path, base.upper())]
    if len(parts) >= 3:
        out.append((path + "".join(parts[:-1]), parts[-1].upper()))
    if len(parts) >= 5:
        out.append((path + "".join(parts[:-3]), "".join(parts[-3:]).upper()))
    return [(s, t) for s, t in out if t]


def _stem_empty(stem: str) -> bool:
    return not stem.strip("/_.")


def name_groups(nets: list[str]) -> list[dict]:
    """Length groups the net names suggest: [{'kind', 'stem', 'nets' (sorted), 'tol', 'conf', 'why'}].
    A guess for the report (``mode: 'report'``), never a reason to lay copper."""
    by_stem = {}
    for n in sorted(nets):
        for stem, tok in _stem_tokens(n):
            by_stem.setdefault(stem, {}).setdefault(tok, n)
    out, used = [], set()

    def take(kind, stem, members, tol, conf, why):
        members = sorted(set(members))
        if any(m in used for m in members):
            return
        used.update(members)
        out.append({"kind": kind, "stem": stem, "nets": members, "tol": tol, "conf": conf, "why": why})

    for stem in sorted(by_stem, key=lambda s: (-len(s), s)):
        toks = by_stem[stem]
        free = {t: n for t, n in toks.items() if n not in used}
        # DDR byte lanes: DQ<n> with their strobe and mask
        lanes = {}
        for t, n in free.items():
            m = _DDR_DQ.match(t)
            if m:
                lanes.setdefault(int(m.group(1)) // 8, []).append(n)
        for lane in sorted(lanes):
            if len(lanes[lane]) < 4:
                continue
            extra = [n for t, n in free.items() for rx in (_DDR_STROBE, _DDR_MASK)
                     for m in [rx.match(t)] if m and (m.group(1) == "" or int(m.group(1)) == lane)]
            if any(_DDR_STROBE.match(t) for t, n in free.items() if n in extra):
                take("ddr_lane", stem, lanes[lane] + extra, DDR_TOL, DDR_CONF,
                     f"names: {len(lanes[lane])} DQ lines with their strobe on '{stem}' (lane {lane})")
        free = {t: n for t, n in toks.items() if n not in used}
        for kind, want, member, clock, other, fewest, tol, conf in NAME_GROUPS:
            if want is None and _stem_empty(stem):
                continue
            if want and not re.search(want, stem.upper()):
                continue
            mem = [n for t, n in free.items() if re.fullmatch(member, t)]
            clk = [n for t, n in free.items() if re.fullmatch(clock, t)]
            if len(mem) < fewest or not clk:
                continue
            oth = [n for t, n in free.items() if other and re.fullmatch(other, t)]
            take(kind, stem, mem + clk + oth, tol, conf,
                 f"names: {len(mem)} data lines + clock on '{stem}' look like {kind.split('_')[0].upper()}")
            free = {t: n for t, n in toks.items() if n not in used}
        if _stem_empty(stem):
            continue
        clk = [n for t, n in free.items() if _BUS_CLOCK.match(t)]
        if len(clk) != 1:
            continue
        fam = {}
        for t, n in free.items():
            m = _BUS_MEMBER.match(t)
            if m and n not in clk:
                fam.setdefault(m.group(1), []).append(n)
        for pre in sorted(fam):
            if len(fam[pre]) >= BUS_MIN:
                take("bus", stem, fam[pre] + clk, BUS_TOL, BUS_CONF,
                     f"names: {len(fam[pre])} numbered lines {pre}n + one clock on '{stem}'")
                break
    out.sort(key=lambda g: (g["stem"], g["kind"], g["nets"]))
    return out


def _is_supply(board, name: str) -> bool:
    from ..guided import plan as P
    net = board.nets[name]
    s = P.short(name)
    cls = (net.cls or "").lower()
    return bool(net.has_pour or P._GROUND.match(s) or P._POWER.match(s) or P._POWER.match(s.rsplit(".", 1)[-1])
                or "gnd" in cls or "ground" in cls or "power" in cls or "pwr" in cls
                or any(getattr(p, "pt", "") == "power_out" for p in net.pads))


def _crystal(board, name: str) -> bool:
    """guided.plan.classify's crystal tag (what the suggested plan turns into keep_short)."""
    from ..guided import plan as P
    fns = " ".join(p.fn for p in board.nets[name].pads if getattr(p, "fn", ""))
    return bool(re.search(rf"XTAL|OSC|XIN|XOUT|{P._W0}HSE|{P._W0}LSE|{P._W0}XC[12]{P._W1}|{P._W0}XL[12]{P._W1}",
                          P.short(name) + " " + fns, re.I))


def _pitch(pads: list) -> float:
    best = 0.0
    for i, a in enumerate(pads):
        for b in pads[i + 1:]:
            d = math.hypot(a.x - b.x, a.y - b.y)
            if d > 0.02 and (best == 0.0 or d < best):
                best = d
    return best


def pair_confidence(board, p: str, n: str, _cache: dict | None = None) -> tuple[float, str]:
    """How sure the geometry makes a name-detected pair: 0.9 = the two nets sit on neighbouring pads of the same
    parts, 0.6 = same parts but pads apart, 0.3 = different parts (then it is routed as two nets, as before)."""
    pp, pn = board.nets[p].pads, board.nets[n].pads
    rp, rn = sorted({a.ref for a in pp}), sorted({a.ref for a in pn})
    if rp != rn:
        return 0.3, "the two nets are on different parts"
    cache = _cache if _cache is not None else {}
    apart = []
    for ref in rp:
        if ref not in cache:
            cache[ref] = _pitch([a for a in board.pads if a.ref == ref])
        reach = 3.0 * cache[ref] if cache[ref] > 0 else 0.0
        mine = [a for a in pp if a.ref == ref]
        theirs = [a for a in pn if a.ref == ref]
        if any(min(math.hypot(a.x - b.x, a.y - b.y) for b in theirs) > reach + 1e-9 for a in mine):
            apart.append(ref)
    if apart or len(pp) != len(pn):
        return 0.6, ("pads apart on " + ", ".join(apart[:3])) if apart else "different pad counts"
    return 0.9, "pads adjacent on " + ", ".join(rp[:3])


def _fmt(v) -> str:
    return f"{v:g}"


def resolve(board, cfg, x, proj: ProjectRules | None = None) -> Constraints:
    """The constraints of this board + plan. Precedence per net: plan > KiCad rule > net class > name; a weaker
    source never overrides a number of a stronger one, it may fill a field the stronger one left empty."""
    from ..guided import plan as P
    from .oracle import dp_complement
    cp = getattr(x, "cons_plan", None) or {}
    mode = cp.get("mode", "auto")
    if mode == "off":
        return Constraints(mode="off")
    proj = proj if proj is not None else ProjectRules()
    routable = sorted(n for n in board.nets if len(board.nets[n].pads) >= 2)
    rset = set(routable)
    classes = {n: board.nets[n].cls for n in board.nets}
    notes, unread = list(proj.notes), []
    plan_only = mode == "plan"

    # ── KiCad custom rules ────────────────────────────────────────────────
    g_skew, g_len = [], []          # (rule name, nets, tol) / (rule name, nets, lo, opt, hi)
    p_dru, short_dru, vias_dru, width_dru = {}, {}, {}, {}
    pair_rule_nets = set()
    for r in proj.rules:
        mine = [c for c in r.cons if c[0] in OURS and (c[0] != "track_width" or "opt" in c[1])]
        if not mine:
            continue
        if r.time_domain:
            unread.append(r.name)
            notes.append(f"rule '{r.name}' is a time-domain (delay) rule: not read")
            continue
        sels = parse_condition(r.cond)
        if sels is None:
            unread.append(r.name)
            continue
        if (r.severity or "").lower() == "ignore":
            notes.append(f"rule '{r.name}' has severity ignore: listed, not enforced")
            continue
        hit = select(sels, classes) & rset
        pair_only = all(k == "pair" for k, _p in sels)
        for kind, v, flags in mine:
            sev = {"length": "length_out_of_range", "skew": "skew_out_of_range",
                   "diff_pair_gap": "diff_pair_gap_out_of_range",
                   "diff_pair_uncoupled": "diff_pair_uncoupled_length_too_long", "via_count": "too_many_vias"}.get(kind)
            if sev and proj.severities.get(sev) == "ignore":
                notes.append(f"rule '{r.name}': {sev} is set to ignore in the project: listed, not enforced")
                continue
            if kind == "skew" and v.get("max") is not None:
                two = sorted(hit)
                is_pair = len(two) == 2 and dp_complement(two[0]) == two[1]
                if pair_only or "within_diff_pairs" in flags or is_pair:
                    for n in hit:
                        p_dru.setdefault(n, {})["skew"] = (v["max"], r.name)
                    pair_rule_nets |= hit
                else:
                    g_skew.append((r.name, hit, v["max"]))
            elif kind == "length":
                if v.get("min") is None and v.get("opt") is None and v.get("max") is not None:
                    for n in hit:
                        short_dru[n] = (v["max"], r.name)
                elif v:
                    g_len.append((r.name, hit, v.get("min"), v.get("opt"), v.get("max")))
            elif kind == "diff_pair_gap":
                for n in hit:
                    p_dru.setdefault(n, {})["gap"] = (v.get("min"), v.get("opt"), v.get("max"), r.name)
                pair_rule_nets |= hit
            elif kind == "diff_pair_uncoupled" and v.get("max") is not None:
                for n in hit:
                    p_dru.setdefault(n, {})["uncoupled"] = (v["max"], r.name)
                pair_rule_nets |= hit
            elif kind == "via_count" and v.get("max") is not None:
                for n in hit:
                    vias_dru[n] = int(round(v["max"]))
            elif kind == "track_width":
                lay = None
                if r.layer:
                    lay = tuple(i for i, name in enumerate(board.layers) if wild(r.layer, name))
                for n in hit:
                    width_dru[n] = (v["opt"], lay, r.name)

    # ── pairs ─────────────────────────────────────────────────────────────
    df = proj.classes.get("Default") or {}
    dfr = board.rules.get("default") or {}
    df_dims = (dfr.get("dp_width"), dfr.get("dp_gap"))       # the Default class's pair, as the board carries it
    plan_pairs = cp.get("pairs") or {}
    cand = []                        # (p, n, source)
    if cp.get("pairs_given"):
        named = P.find_pairs(routable)
        for p, d in sorted(plan_pairs.items()):
            if p not in rset or d.get("n") not in rset:
                continue
            # pcb_route_plan's suggested plan sent back unchanged is still a guess from the names, not a declaration
            echo = (named.get(p) == d["n"] and not any(d.get(k) is not None for k in ("skew", "uncoupled", "coupled", "z"))
                    and (d.get("gap") is None or abs(d["gap"] - board.nets[p].dp_gap) < 1e-9)
                    and (d.get("width") is None or abs(d["width"] - board.nets[p].dp_width) < 1e-9)
                    and not _is_supply(board, p) and not _is_supply(board, d["n"]))
            cand.append((p, d["n"], "name" if echo else "plan"))
    else:
        seen = set()
        for p, n in sorted(P.find_pairs(routable).items()):
            if _is_supply(board, p) or _is_supply(board, n):
                continue
            cand.append((p, n, "name"))
            seen |= {p, n}
        for a in sorted(pair_rule_nets - seen):
            b = dp_complement(a)
            if b in rset and b not in seen and dp_base(a)[1] > 0:
                cand.append((a, b, "dru"))
                seen |= {a, b}
    pairs, pitch_cache = [], {}
    for p, n, src in cand:
        net = board.nets[p]
        pc = cfg.nets.get(p) if cfg is not None else None
        d = plan_pairs.get(p) or {}
        rule = dict(p_dru.get(p) or {})
        for k, v in (p_dru.get(n) or {}).items():
            rule.setdefault(k, v)
        cls = proj.classes.get(net.cls) or {}
        z_cls = _impedance(net.cls)
        special = net.cls != "Default" and (z_cls is not None or (
            df_dims[1] is not None and (abs(net.dp_gap - df_dims[1]) > 1e-9
                                        or abs(net.dp_width - (df_dims[0] or net.dp_width)) > 1e-9)))
        if src == "name" and rule:
            src = "dru"
        elif src == "name" and special:
            src = "class"
        gap_rule = rule.get("gap")
        width = d.get("width") or (pc.width if pc is not None and pc.dp_partner == n else net.dp_width)
        gap = d.get("gap") or (gap_rule[1] if gap_rule and gap_rule[1] is not None else None) or (
            pc.dp_gap if pc is not None and pc.dp_partner == n and pc.dp_gap else net.dp_gap)
        via_gap = cls.get("dp_via_gap") or df.get("dp_via_gap") or VIA_GAP
        skew = d.get("skew") if d.get("skew") is not None else (rule["skew"][0] if rule.get("skew") else None)
        unc = d.get("uncoupled") if d.get("uncoupled") is not None else (
            rule["uncoupled"][0] if rule.get("uncoupled") else None)
        kicad = dp_complement(p) == n
        if src in ("plan", "dru"):
            conf, geo = 1.0, ""
        else:
            conf, geo = pair_confidence(board, p, n, pitch_cache)
            if src == "class":
                conf = min(1.0, conf + 0.1)
        if src == "plan":
            why = "plan.diff_pairs"
        elif src == "dru":
            names = sorted({v[-1] for v in rule.values()})
            why = "rule " + ", ".join(f"'{r_}'" for r_ in names)
        elif src == "class":
            why = f"class '{net.cls}' (pair {_fmt(net.dp_width)} / {_fmt(net.dp_gap)} mm); {geo}"
        else:
            why = f"names; {geo}"
        coupled = d["coupled"] if d.get("coupled") is not None else (True if src in ("plan", "dru") else conf >= COUPLE_CONF)
        if plan_only and src != "plan":
            coupled = False
        if not kicad:
            notes.append(f"{p} + {n} is not a pair by KiCad's name rule (it wants ..._P/_N or ...+/-): KiCad applies "
                         "no pair gap and no inDiffPair rule to it")
        pairs.append(Pair(p=p, n=n, width=float(width), gap=float(gap), via_gap=float(via_gap), skew=skew,
                          uncoupled=unc, gap_lo=gap_rule[0] if gap_rule else None,
                          gap_hi=gap_rule[2] if gap_rule else None, coupled=bool(coupled), kicad=kicad,
                          z=d.get("z") if d.get("z") is not None else z_cls, src=src, conf=round(conf, 2), why=why))

    # ── length groups ─────────────────────────────────────────────────────
    groups, claimed = [], {}
    off = set(cp.get("off") or ())
    for i, d in enumerate(cp.get("groups") or []):
        nets = tuple(sorted(n for n in d["nets"] if n in rset))
        if not nets:
            continue
        name = d.get("name") or f"G{i + 1}"
        groups.append(LenGroup(name=name, nets=nets, tol=float(d["tol"]), target=d.get("len"), amp=d.get("amp"),
                               mode="tune", src="plan", conf=1.0, why=f"plan.groups[{i}]"))
        for n in nets:
            claimed[n] = name
    if not cp.get("no_groups"):
        dru_groups = []
        for name, hit, tol in reversed(g_skew):                  # a later rule wins in KiCad
            nets = tuple(sorted(n for n in hit if n not in claimed and n not in off))
            if len(nets) < 2:
                continue
            dru_groups.insert(0, LenGroup(name=name, nets=nets, tol=float(tol), mode="report" if plan_only else "tune",
                                          src="dru", conf=1.0, why=f"rule '{name}': skew max {_fmt(tol)} mm"))
            for n in nets:
                claimed[n] = name
        n_skew = len(dru_groups)
        for name, hit, lo, opt, hi in reversed(g_len):
            why = f"rule '{name}': length" + (f" min {_fmt(lo)}" if lo is not None else "") + (
                f" opt {_fmt(opt)}" if opt is not None else "") + (f" max {_fmt(hi)}" if hi is not None else "") + " mm"
            for k, g in enumerate(dru_groups):                   # the same nets under a skew rule: one group, both checks
                if g.lo is None and g.hi is None and set(g.nets) <= hit:
                    dru_groups[k] = replace(g, lo=lo, hi=hi, why=g.why + "; " + why)
            nets = tuple(sorted(n for n in hit if n not in claimed and n not in off))
            if not nets:
                continue
            dru_groups.insert(n_skew, LenGroup(name=name, nets=nets, tol=0.0, target=opt, lo=lo, hi=hi,
                                               mode="report" if plan_only else "tune", src="dru", conf=1.0, why=why))
            for n in nets:
                claimed[n] = name
        groups += dru_groups
        free = [n for n in routable if n not in claimed and n not in off and not _is_supply(board, n)]
        for g in name_groups(free):
            name = f"{g['kind']}:{g['stem']}"
            groups.append(LenGroup(name=name, nets=tuple(g["nets"]), tol=g["tol"], mode="report", src="name",
                                   conf=g["conf"], why=g["why"] + f"; tol {_fmt(g['tol'])} mm is a typical value"))
            for n in g["nets"]:
                claimed[n] = name
    seen_names = {}
    for k, g in enumerate(groups):                               # names are the report's keys: keep them unique
        c = seen_names.get(g.name, 0)
        seen_names[g.name] = c + 1
        if c:
            groups[k] = replace(g, name=f"{g.name}#{c + 1}")

    # ── widths / impedance labels ─────────────────────────────────────────
    widths = {}
    pw, pz = cp.get("widths") or {}, cp.get("z") or {}
    for n in sorted(set(pw) | set(pz)):
        if n not in rset:
            continue
        nc = cfg.nets.get(n) if cfg is not None else None
        base = nc.width if nc is not None else board.nets[n].width
        widths[n] = WidthRule(net=n, by_layer=tuple(pw.get(n) or ()), width=float(base), neck=False, z=pz.get(n),
                              src="plan", why="plan.nets[..]." + ("widths" if n in pw else "z"))
    if not plan_only:
        for n in sorted(width_dru):
            if n in widths:
                continue
            w, lay, rname = width_dru[n]
            nc = cfg.nets.get(n) if cfg is not None else None
            base = nc.width if nc is not None else board.nets[n].width
            widths[n] = WidthRule(net=n, by_layer=tuple((l, float(w)) for l in lay) if lay else (),
                                  width=float(base if lay else w), neck=False, src="dru",
                                  why=f"rule '{rname}': track_width opt {_fmt(w)} mm")
        paired = {q_ for pr_ in pairs for q_ in (pr_.p, pr_.n)}
        for n in routable:
            z = _impedance(board.nets[n].cls)
            if z is None or n in widths or n in paired or _is_supply(board, n):
                continue
            nc = cfg.nets.get(n) if cfg is not None else None
            widths[n] = WidthRule(net=n, width=float(nc.width if nc is not None else board.nets[n].width), neck=True,
                                  z=z, src="class", why=f"class '{board.nets[n].cls}'")
        # a net class with its own track width (not the Default class's): the class width is what the router lays;
        # the report says where it is not held. It may neck at a pad that needs it (neck=True), nowhere else
        dw = dfr.get("width")
        for n in routable:
            net = board.nets[n]
            if n in widths or n in paired or net.cls in (None, "", "Default") or _is_supply(board, n) or not dw:
                continue
            nc = cfg.nets.get(n) if cfg is not None else None
            if abs(net.width - dw) < 1e-9 or (nc is not None and abs(nc.width - net.width) > 1e-9):
                continue
            widths[n] = WidthRule(net=n, width=float(net.width), neck=True, src="class",
                                  why=f"class '{net.cls}' (track {_fmt(net.width)} mm)")

    # ── keep-short ────────────────────────────────────────────────────────
    short = {}
    for n in routable:
        nc = cfg.nets.get(n) if cfg is not None else None
        if nc is not None and (nc.keep_short or nc.max_length is not None):
            src = "plan" if (nc.max_length is not None or (nc.explicit and not _crystal(board, n))) else "name"
            why = ("plan.nets[..]." + ("max_length" if nc.max_length is not None else "keep_short")) if src == "plan" \
                else "name: crystal / oscillator net"
            short[n] = Short(net=n, max_len=nc.max_length, hard=nc.max_length is not None, src=src, why=why)
    if not plan_only:
        for n in sorted(short_dru):
            mx, rname = short_dru[n]
            if n in short and short[n].max_len is not None:
                continue
            short[n] = Short(net=n, max_len=float(mx), hard=True, src=short[n].src if n in short else "dru",
                             why=f"rule '{rname}': length max {_fmt(mx)} mm")
        for n in routable:
            if n in short or _is_supply(board, n):
                continue
            s = P.short(n)
            if _SWITCH.search(s) and any(re.match(r"L\d", a.ref or "") for a in board.nets[n].pads):
                short[n] = Short(net=n, src="name", why="name: switch node (on an inductor)")
            elif _RF.search(s):
                short[n] = Short(net=n, src="name", why="name: antenna / RF feed")
    max_vias = {} if plan_only else {n: v for n, v in sorted(vias_dru.items())}
    return Constraints(mode=mode, groups=tuple(groups), pairs=tuple(pairs), widths=widths, short=short,
                       max_vias=max_vias, unread=tuple(dict.fromkeys(unread)), notes=tuple(dict.fromkeys(notes)))


def preview(cons: Constraints | None, limit: int = PREVIEW_MAX) -> dict:
    """What was found, as plain data for ``auto.constraints`` ({} when there is nothing). Every row carries
    ``src`` (plan | dru | class | name), ``conf`` and ``why``; veto with ``groups: []``, ``{off: true}``,
    ``diff_pairs: []``, ``coupled: false`` or ``global.constraints``."""
    if cons is None or not cons.any():
        return {}
    out = {"mode": cons.mode}
    if cons.groups:
        rows = []
        for g in cons.groups[:limit]:
            r = {"name": g.name, "nets": list(g.nets), "tol": g.tol, "mode": g.mode, "src": g.src, "conf": g.conf,
                 "why": g.why}
            for k, v in (("len", g.target), ("min", g.lo), ("max", g.hi)):
                if v is not None:
                    r[k] = v
            rows.append(r)
        out["groups"] = rows
    if cons.pairs:
        rows = []
        for p in cons.pairs[:limit]:
            r = {"p": p.p, "n": p.n, "width": p.width, "gap": p.gap, "coupled": p.coupled, "kicad": p.kicad,
                 "src": p.src, "conf": p.conf, "why": p.why}
            for k, v in (("skew", p.skew), ("uncoupled", p.uncoupled), ("z", p.z)):
                if v is not None:
                    r[k] = v
            rows.append(r)
        out["pairs"] = rows
    if cons.short:
        out["short"] = [{"net": s.net, **({"max": s.max_len} if s.max_len is not None else {}), "src": s.src,
                         "why": s.why} for s in list(cons.short.values())[:limit]]
    if cons.widths:
        out["widths"] = [{"net": w.net, "width": w.width, **({"layers": [list(t) for t in w.by_layer]} if w.by_layer else {}),
                          **({"z": w.z} if w.z is not None else {}), "src": w.src}
                         for w in list(cons.widths.values())[:limit]]
    if cons.max_vias:
        out["max_vias"] = dict(list(cons.max_vias.items())[:limit])
    if cons.unread:
        out["unread"] = list(cons.unread)
    if cons.notes:
        out["notes"] = list(cons.notes[:6])
    more = sum(max(0, len(v) - limit) for v in (cons.groups, cons.pairs, cons.short, cons.widths, cons.max_vias))
    if more:
        out["more"] = more
    return out


def expected_set(cons: Constraints | None) -> dict:
    """The detected set in the shape the benchmark's ``meta.constraints`` uses (names only)."""
    if cons is None:
        return {"groups": [], "pairs": [], "short": []}
    return {"groups": sorted(sorted(g.nets) for g in cons.groups),
            "pairs": sorted([p.p, p.n] for p in cons.pairs),
            "short": sorted(cons.short)}


# ── length, exactly as KiCad's DRC counts it (design §14.5) ───────────────
@dataclass(frozen=True)
class LenParts:
    track: float = 0.0            # sum of every track segment of the net (tee stubs included)
    inpad: float = 0.0            # what the in-pad / in-via shortcut takes off
    via: float = 0.0              # stack height of the vias, between the layers the net uses at each
    die: float = 0.0              # pad-to-die lengths
    total: float = 0.0
    vias: int = 0
    branched: bool = False        # more than two pads or a tee: the figure is the net's total copper


def _key(x: float, y: float) -> tuple:
    return (int(round(x * 1e6)), int(round(y * 1e6)))


def _inside(shapes, x: float, y: float) -> bool:
    return any(s.dist_pt(x, y) < -1e-7 for s in shapes)


def measure_net(segs, vias, pads, stack: Stackup | None = None, use_height: bool = True, die: float = 0.0) -> LenParts:
    """``segs``: [(layer, x1, y1, x2, y2), ...]; ``vias``: [(x, y) or (x, y, size), ...]; ``pads``: objects with
    ``x``, ``y`` and ``shapes`` {layer: [geom.Shape]} (guided.model.Pad). Rules, each measured on KiCad 10.0
    (design §14.9 and the engineer-A probes in docs/internal/v2-interface-notes.md):

    * every segment counts in full, a tee stub too;
    * a chain that ENDS on a pad's position (exactly one segment end there on that layer) is counted as the
      straight line from the pad centre to the point where it leaves the pad's copper; two chains ending on the
      same pad centre on one layer are one line through the pad for KiCad and are counted in full;
    * the same shortcut applies at a via: a chain that ends on the via's position is counted as the straight line
      from the via centre to where it leaves the via's copper circle (a dogleg inside the via pad is cut short);
    * a via adds the stack height between the top-most and bottom-most layer on which the net has a segment end
      or a pad at the via's position (nothing when only one layer meets there);
    * pad-to-die length is added."""
    track = 0.0
    ends = {}
    for i, (lay, x1, y1, x2, y2) in enumerate(segs):
        track += math.hypot(x2 - x1, y2 - y1)
        ends.setdefault((lay,) + _key(x1, y1), []).append((i, 0))
        ends.setdefault((lay,) + _key(x2, y2), []).append((i, 1))

    def shortcut(lay, px, py, inside) -> float:
        at = ends.get((lay,) + _key(px, py)) or []
        if len(at) != 1 or not inside(px, py):
            return 0.0
        i, e = at[0]
        walked, seen, exit_pt = 0.0, set(), None
        while i not in seen and len(seen) < 64:
            seen.add(i)
            _l, x1, y1, x2, y2 = segs[i]
            ax, ay, bx, by = (x1, y1, x2, y2) if e == 0 else (x2, y2, x1, y1)
            ln = math.hypot(bx - ax, by - ay)
            if inside(bx, by):
                walked += ln
                exit_pt = (bx, by)
                nxt = [t for t in ends.get((lay,) + _key(bx, by)) or [] if t[0] != i]
                if len(nxt) != 1:
                    break
                i, e = nxt[0]
                continue
            lo, hi = 0.0, 1.0
            for _ in range(48):
                mid = (lo + hi) / 2.0
                if inside(ax + (bx - ax) * mid, ay + (by - ay) * mid):
                    lo = mid
                else:
                    hi = mid
            t = (lo + hi) / 2.0
            walked += ln * t
            exit_pt = (ax + (bx - ax) * t, ay + (by - ay) * t)
            break
        if exit_pt is None:
            return 0.0
        return max(0.0, walked - math.hypot(exit_pt[0] - px, exit_pt[1] - py))

    inpad = 0.0
    done = set()
    for pad in pads:
        for lay, shapes in sorted((getattr(pad, "shapes", None) or {}).items()):
            if shapes:
                done.add((lay,) + _key(pad.x, pad.y))
                inpad += shortcut(lay, pad.x, pad.y, lambda x, y, sh=shapes: _inside(sh, x, y))
    layers = sorted({k[0] for k in ends})
    for v in vias:
        r = (v[2] / 2.0) if len(v) > 2 and v[2] else 0.0
        if r <= 0:
            continue
        for lay in layers:
            k = (lay,) + _key(v[0], v[1])
            if k in done:
                continue                       # a via on a pad centre: the pad's copper is the larger shape
            done.add(k)
            inpad += shortcut(lay, v[0], v[1],
                              lambda x, y, vx=v[0], vy=v[1], rr=r: math.hypot(x - vx, y - vy) < rr - 1e-7)
    via_len = 0.0
    if use_height and stack is not None:
        pad_at = {}
        for pad in pads:
            for lay in (getattr(pad, "shapes", None) or {}):
                pad_at.setdefault(_key(pad.x, pad.y), set()).add(lay)
        lay_at = {}
        for (lay, kx, ky) in ends:
            lay_at.setdefault((kx, ky), set()).add(lay)
        for v in vias:
            k = _key(v[0], v[1])
            used = set(lay_at.get(k) or ()) | set(pad_at.get(k) or ())
            if len(used) >= 2:
                via_len += stack.height(min(used), max(used))
    branched = len(pads) > 2 or any(len(v) >= 3 for v in ends.values())
    total = track - inpad + via_len + die
    return LenParts(track=track, inpad=inpad, via=via_len, die=die, total=total, vias=len(vias), branched=branched)


def env_of(ctx) -> dict:
    """{'stack', 'die', 'use_height'} of this run (``ctx.x.cons_env``, set by the engine), with KiCad's defaults."""
    env = getattr(getattr(ctx, "x", None), "cons_env", None)
    if not isinstance(env, dict):
        env = ctx.history.get("cons_env") if isinstance(getattr(ctx, "history", None), dict) else None
    if not isinstance(env, dict):
        env = {}
    stack = env.get("stack") or Stackup.default(getattr(ctx.board, "L", 2))
    return {"stack": stack, "die": env.get("die") or {}, "use_height": env.get("use_height", True)}


def net_copper(ctx, net: str) -> tuple[list, list]:
    """(segments [(layer, x1, y1, x2, y2, w)], vias [(x, y, size)]) of a net: this run's pieces + the copper that
    was on the board before it."""
    segs, vias = [], []
    for t in getattr(ctx.board, "kept_tracks", None) or ():
        if t[0] == net:
            segs.append((t[1], t[2], t[3], t[4], t[5], t[6]))
    for v in getattr(ctx.board, "kept_vias", None) or ():
        if v[0] == net:
            vias.append((v[1], v[2], v[3]))
    state = getattr(ctx, "state", None)
    if state is not None:
        for p in state.pieces(net=net):
            if p.kind == "seg":
                segs.append((p.layer, p.x1, p.y1, p.x2, p.y2, p.w))
            else:
                vias.append((p.x1, p.y1, p.w))
    return segs, vias


def net_length(ctx, net: str) -> LenParts:
    """The length of a net as KiCad's DRC will report it for the copper the run holds now."""
    env = env_of(ctx)
    segs, vias = net_copper(ctx, net)
    n = ctx.board.nets.get(net)
    return measure_net([s[:5] for s in segs], vias, n.pads if n is not None else [], env["stack"], env["use_height"],
                       float(env["die"].get(net, 0.0)))


def coupled_stats(segs_p, segs_n, width: float, gap: float, gap_lo: float | None = None,
                  gap_hi: float | None = None, tol: float = GAP_TOL) -> dict:
    """How much of a pair runs side by side. ``segs_*``: [(layer, x1, y1, x2, y2, w), ...]. A stretch of a
    segment is *coupled* when the other net has a parallel segment on the same layer beside it whose copper
    edge is ``gap`` away (inside [gap_lo, gap_hi] when a rule gives a window), +- ``tol``.

    -> {'coupled_mm': [p, n], 'uncoupled_mm': [p, n], 'coupled_pct': share of the longer net, 'gap_seen': the
        edge gap most of the side-by-side length runs at (None when the two never run in parallel within 1 mm)}"""
    lo = (gap_lo if gap_lo is not None else gap) - tol
    hi = (gap_hi if gap_hi is not None else gap) + tol
    seen = {}

    def one(mine, theirs, tally):
        total = cov = 0.0
        for lay, x1, y1, x2, y2, w in mine:
            ln = math.hypot(x2 - x1, y2 - y1)
            total += ln
            if ln < 1e-9:
                continue
            ux, uy = (x2 - x1) / ln, (y2 - y1) / ln
            spans = []
            for lay2, a1, b1, a2, b2, w2 in theirs:
                if lay2 != lay:
                    continue
                l2 = math.hypot(a2 - a1, b2 - b1)
                if l2 < 1e-9:
                    continue
                # parallel: the two directions drift apart by less than PAR_DRIFT over the shorter segment.
                # (Copper read back from a board file has its coordinates rounded to 0.1 um: a 0.4 mm piece of a
                # skew bump is then 2e-4 rad off its neighbour. An exact test made the figures of one and the
                # same copper differ between the run that laid it and a later call that only measures it.)
                if abs(ux * (b2 - b1) - uy * (a2 - a1)) / l2 > min(PAR_MAX, max(1e-6, PAR_DRIFT / min(ln, l2))):
                    continue
                dist = abs((a1 - x1) * uy - (b1 - y1) * ux)
                edge = dist - (w + w2) / 2.0
                t1 = (a1 - x1) * ux + (b1 - y1) * uy
                t2 = (a2 - x1) * ux + (b2 - y1) * uy
                s0, s1 = max(0.0, min(t1, t2)), min(ln, max(t1, t2))
                if s1 - s0 <= 1e-9:
                    continue
                if tally and -1e-6 <= edge <= 1.0:
                    k = round(edge, 3)
                    seen[k] = seen.get(k, 0.0) + (s1 - s0)
                if lo - 1e-9 <= edge <= hi + 1e-9:
                    spans.append((s0, s1))
            spans.sort()
            end = 0.0
            for s0, s1 in spans:
                if s1 > end:
                    cov += s1 - max(s0, end)
                    end = s1
        return total, cov
    tp, cpl = one(segs_p, segs_n, True)
    tn, cnl = one(segs_n, segs_p, False)
    longer = max(tp, tn)
    return {"coupled_mm": [cpl, cnl], "uncoupled_mm": [tp - cpl, tn - cnl],
            "coupled_pct": 100.0 * (cpl if tp >= tn else cnl) / longer if longer > 1e-9 else 0.0,
            "gap_seen": max(sorted(seen), key=lambda k: seen[k]) if seen else None}


# ── KiCad's own verdict (design §14.9 message formats, KiCad 10.0, English) ──
_M = {
    "length_out_of_range": re.compile(
        r"\(rule '(?P<rule>.*)' (?P<side>min|max|opt) length (?P<lim>-?[\d.]+) mm; actual (?P<len>-?[\d.]+) mm\)"),
    "skew_out_of_range": re.compile(
        r"\(rule '(?P<rule>.*)' max skew (?P<lim>-?[\d.]+) mm; actual (?P<act>-?[\d.]+) mm; target net length "
        r"(?P<tgt>-?[\d.]+) mm \(from (?P<from>.*)\); actual (?P<len>-?[\d.]+) mm\)"),
    "diff_pair_gap_out_of_range": re.compile(
        r"\((?P<rule>.*) (?P<side>minimum|maximum) gap (?P<lim>-?[\d.]+) mm; actual (?P<act>-?[\d.]+) mm\)"),
    "diff_pair_uncoupled_length_too_long": re.compile(
        r"\((?P<rule>.*) maximum uncoupled length (?P<lim>-?[\d.]+) mm; actual (?P<act>-?[\d.]+) mm\)"),
    "too_many_vias": re.compile(r"\(rule '(?P<rule>.*)' max count (?P<lim>\d+); actual (?P<act>\d+)\)"),
}
_ITEM_NET = re.compile(r"\[([^\]]*)\]")


def kicad_markers(raw, nets=None) -> dict:
    """KiCad's constraint markers out of a DRC result (raw kicad-cli violations, or kio markers).

    -> {'counts': {type: errors}, 'warnings': {type: n}, 'unparsed': n,
        'markers': [{'type', 'nets', 'rule', 'limit', 'actual', 'len', 'target', 'from', 'side', 'severity'}],
        'len': {net: KiCad's own length of the net, mm}, 'nets': {net: [type, ...]}}

    ``nets``: only markers that name one of these nets. A message that does not parse (another KiCad language or
    version) still counts with its type and nets; ``unparsed`` says how many."""
    out = {"counts": {}, "warnings": {}, "unparsed": 0, "markers": [], "len": {}, "nets": {}}
    for v in raw or []:
        if not isinstance(v, dict) or v.get("type") not in KICAD_TYPES:
            continue
        typ = v["type"]
        sev = v.get("severity") or "error"
        if sev not in ("error", "warning"):
            continue
        msg = str(v.get("description") or v.get("message") or "")
        names = []
        for it in v.get("items") or []:
            text = it.get("description") if isinstance(it, dict) else it
            for n in _ITEM_NET.findall(str(text or "")):
                if n and n not in names:
                    names.append(n)
        if nets is not None and not any(n in nets for n in names):
            continue
        m = {"type": typ, "nets": names, "severity": sev}
        hit = _M[typ].search(msg)
        if hit:
            g = hit.groupdict()
            m["rule"] = g.get("rule")
            m["limit"] = float(g["lim"])
            if g.get("act") is not None:
                m["actual"] = float(g["act"])
            if g.get("side"):
                m["side"] = g["side"][:3]
            if g.get("len") is not None:
                m["len"] = float(g["len"])
                if typ == "length_out_of_range":
                    m["actual"] = m["len"]
                for n in names[:1] if typ == "skew_out_of_range" else names:
                    out["len"][n] = m["len"]
            if g.get("tgt") is not None:
                m["target"], m["from"] = float(g["tgt"]), g.get("from")
                if m["from"]:
                    out["len"].setdefault(m["from"], m["target"])
        else:
            m["unparsed"] = True
            out["unparsed"] += 1
        out["markers"].append(m)
        key = "counts" if sev == "error" else "warnings"
        out[key][typ] = out[key].get(typ, 0) + 1
        for n in names:
            if typ not in out["nets"].setdefault(n, []):
                out["nets"][n].append(typ)
    out["markers"].sort(key=lambda m: (m["type"], m["nets"], m.get("rule") or "", m.get("actual") or 0.0))
    out["counts"] = dict(sorted(out["counts"].items()))
    return out


# ── the report (design §14.10) ────────────────────────────────────────────
def window(g: LenGroup, lengths: dict) -> tuple:
    """(low, high, reference) a member's length must lie in; reference = the explicit target, else the longest
    routed member. ``lengths``: {net: mm} of the members that are completely routed."""
    vals = [lengths[n] for n in g.nets if n in lengths]
    ref = g.target if g.target is not None else (max(vals) if vals else None)
    lo, hi = -math.inf, math.inf
    if g.tol > 0 and ref is not None:
        lo = ref - g.tol
        hi = ref + g.tol if g.target is not None else ref
    if g.lo is not None:
        lo = max(lo, g.lo)
    if g.hi is not None:
        hi = min(hi, g.hi)
    return lo, hi, ref


def _r(v, nd: int = 3):
    return None if v is None else round(float(v), nd)


def _kicad(kicad) -> dict | None:
    if kicad is None:
        return None
    if isinstance(kicad, dict) and "counts" in kicad and "markers" in kicad:
        return kicad
    if isinstance(kicad, dict):
        if isinstance(kicad.get("cons"), dict):
            return kicad["cons"]
        return kicad_markers(kicad.get("raw") or [])
    return kicad_markers(kicad)


class _AsIs:
    """What ``measure`` reads of a run, for a board no run holds copper on (``measure_board``)."""

    def __init__(self, board, x):
        self.board, self.x, self.state, self.conns, self.history = board, x, None, (), {}


def measure_board(board, x, unconnected: dict | None = None) -> dict:
    """The ``constraints`` block of a board AS IT IS: the copper in ``board`` (a ``model.Board`` of the file),
    no piece of any run, no KiCad verdict (none was asked about this copper). ``x``: the run's plan extension
    (its resolved constraints and length environment); ``unconnected``: KiCad's open connections per net for
    that file. This is what a run reports whose own copper was not kept (nothing closed, rip-up undone): the
    figures of the unchanged board, never those of the discarded copper."""
    return measure(_AsIs(board, x), unconnected=unconnected)


def measure(ctx, kicad=None, cons: Constraints | None = None, unconnected: dict | None = None) -> dict:
    """The ``constraints`` block of a result: every group, pair, keep-short net and width rule measured on the
    copper the run holds, with KiCad's verdict where a judged check is at hand (``kicad``: the judge's check
    record, a ``kicad_markers`` dict or raw violations). A missed target always carries the amount in mm; a
    group in report mode has ``ok: None`` (information, not a pass). {} when there is nothing to report.
    ``unconnected``: KiCad's open connections per net when no check record carries them (``measure_board``)."""
    cons = cons if cons is not None else getattr(getattr(ctx, "x", None), "cons", None)
    if cons is None or not cons.any():
        return {}
    k = _kicad(kicad)
    klen = (k or {}).get("len") or {}
    knets = (k or {}).get("nets") or {}
    state = getattr(ctx, "state", None)
    unc = kicad.get("unconnected") if isinstance(kicad, dict) and isinstance(kicad.get("unconnected"), dict) else None
    if unc is None and isinstance(unconnected, dict):
        unc = unconnected
    open_nets = set()
    for c in getattr(ctx, "conns", None) or ():
        if state is not None and state.status(c.idx) != "routed":
            open_nets.add(c.net)
    if unc is not None:
        open_nets = {n for n in open_nets if unc.get(n)} | {n for n, v in unc.items() if v}
    cache, model_err = {}, 0.0

    def parts(n) -> LenParts:
        if n not in cache:
            cache[n] = net_length(ctx, n)
        return cache[n]

    def length(n) -> float:
        nonlocal model_err
        mine = parts(n).total
        if n in klen:
            model_err = max(model_err, abs(klen[n] - mine))
            return klen[n]
        return mine

    out = {"mode": cons.mode, "ok": 0, "missed": 0}
    hist = getattr(ctx, "history", None)
    tune_recs = ((hist.get("tune") or {}).get("nets") or {}) if isinstance(hist, dict) else {}
    pair_recs = (hist.get("pairs") or {}) if isinstance(hist, dict) else {}
    lanes_on = cons.mode in ("auto", "plan")
    bad_nets = set()                  # nets of every row that is reported as missed
    rows = []
    for g in cons.groups:
        routed = {n: length(n) for n in g.nets if n not in open_nets and parts(n).track > 0}
        lo, hi, ref = window(g, routed)
        row = {"name": g.name, "n": len(g.nets), "target": _r(ref), "min": _r(min(routed.values())) if routed else None,
               "max": _r(max(routed.values())) if routed else None, "tol": g.tol, "src": g.src}
        if g.lo is not None or g.hi is not None:
            row["window"] = [g.lo, g.hi]
        worst, worst_key, under, over = None, None, 0.0, 0.0
        for n in sorted(routed):
            u, o = max(0.0, lo - routed[n]), max(0.0, routed[n] - hi)
            under, over = max(under, u), max(over, o)
            key = (round(max(u, o), 9), round(abs(routed[n] - ref), 9) if ref is not None else 0.0)
            if worst_key is None or key > worst_key:
                worst, worst_key = n, key
        missing = sorted(n for n in g.nets if n not in routed)
        if worst is not None:
            row["worst"] = worst
            row["dev"] = _r(routed[worst] - ref) if ref is not None else 0.0
        flagged = sorted(n for n in g.nets if any(t in ("skew_out_of_range", "length_out_of_range")
                                                  for t in knets.get(n) or ()))
        mine_ok = not missing and under <= 1e-9 and over <= 1e-9
        if g.mode == "report":
            ok = None
            row["mode"] = "report"
            row["nets"] = list(g.nets)                     # what a patch that adopts the group has to name
        elif k is not None and g.src == "dru":
            ok = not flagged and not missing              # KiCad checks this very rule: its verdict
        else:
            ok = mine_ok and not flagged
        row["ok"] = ok
        if under > 1e-9:
            row["short_by"] = _r(under)
        if over > 1e-9:
            row["long_by"] = _r(over)
        if missing:
            row["open"] = missing[:4]
            row["why"] = "open connection"
        elif ok is False and mine_ok and flagged:
            row["why"] = "KiCad flags " + ", ".join(flagged[:3]) + " although the router's figures fit"
        if any(parts(n).branched for n in routed):
            row["branched"] = True                         # multi-pad members: the figure is the net's total copper
        # length tuning (v2/tune.py): how many members got meanders and how many mm they add; for a group that
        # still misses, why the tuner could not close it and how much the free runs could hold
        recs = [tune_recs[n] for n in g.nets if n in tune_recs]
        laid = [r for r in recs if r.get("bumps")]
        if laid:
            row["tuned"] = len(laid)
            row["added"] = _r(sum(float(r.get("added") or 0.0) for r in laid))
            hopped = sum(1 for r in laid if r.get("hops"))
            if hopped:
                row["hops"] = hopped                       # members with a stretch on another layer (two vias each)
        if ok is False and not missing:
            r = tune_recs.get(worst) if worst is not None else None
            if not (r and r.get("why")):
                r = next((x for x in recs if x.get("why")), None)
            if r and r.get("why"):
                row["why"] = r["why"]
                if r.get("capacity") is not None:
                    row["capacity"] = r["capacity"]            # mm the free runs of that member could add
        rows.append(row)
        if ok is not None:
            out["ok" if ok else "missed"] += 1
        if ok is False:
            bad_nets.update(g.nets)
    if rows:
        out["groups"] = rows
    rows = []
    for p in cons.pairs:
        sp, _vp = net_copper(ctx, p.p)
        sn, _vn = net_copper(ctx, p.n)
        row = {"p": p.p, "n": p.n, "src": p.src, "gap": p.gap}
        prec = pair_recs.get(p.p) or {}
        laid_gap = float(prec.get("gap") or p.gap)       # the gap the lanes were laid at (>= the pair's own)
        if not p.kicad:
            row["kicad"] = False
        opened = [n for n in (p.p, p.n) if n in open_nets or not parts(n).track > 0]
        ok = None
        if opened:
            row["open"] = opened
            if p.skew is not None or p.uncoupled is not None:
                ok = False
        else:
            lp, ln = length(p.p), length(p.n)
            # coupled: share that runs at the pair's own gap; uncoupled: mm outside the rule's gap window (what
            # KiCad's diff_pair_uncoupled counts) — the same thing when no rule gives a window
            tight = coupled_stats(sp, sn, p.width, laid_gap)
            cs = coupled_stats(sp, sn, p.width, laid_gap, p.gap_lo, p.gap_hi) if (
                p.gap_lo is not None or p.gap_hi is not None) else tight
            if _vp or _vn:
                row["vias"] = [len(_vp), len(_vn)]
            row.update({"len": [_r(lp), _r(ln)], "skew": _r(abs(lp - ln)), "coupled": int(round(tight["coupled_pct"])),
                        "uncoupled": _r(max(cs["uncoupled_mm"]))})
            if tight["gap_seen"] is not None and abs(tight["gap_seen"] - p.gap) > GAP_TOL:
                row["gap_seen"] = tight["gap_seen"]
            if p.skew is not None:
                row["max_skew"] = p.skew
                ok = abs(lp - ln) <= p.skew + 1e-9
                if not ok:
                    row["skew_over"] = _r(abs(lp - ln) - p.skew)
            if p.uncoupled is not None:
                row["max_uncoupled"] = p.uncoupled
                fit = max(cs["uncoupled_mm"]) <= p.uncoupled + 1e-9
                ok = fit if ok is None else (ok and fit)
                if not fit:
                    row["uncoupled_over"] = _r(max(cs["uncoupled_mm"]) - p.uncoupled)
        # how it was laid: as coupled lanes (pairs.py), or as two nets — then with the reason
        cs_recs = prec.get("conns") or {}
        n_lanes = sum(1 for r_ in cs_recs.values() if r_.get("coupled"))
        if p.coupled and lanes_on and not opened and not cs_recs and not any(
                c.net in (p.p, p.n) for c in getattr(ctx, "conns", None) or ()):
            # nothing of this pair was routed in this run (its copper was on the board before): `lanes` says
            # what that copper IS, from its geometry, so a call that only measures a board reports the same
            # as the call that laid it
            row["lanes"] = tight["coupled_pct"] >= LANES_PCT
            if not row["lanes"]:
                row["why"] = (f"copper from before this run: side by side at its gap for "
                              f"{int(round(tight['coupled_pct']))} % only (ripup: 'nets' lets the router re-lay it)")
        elif p.coupled and lanes_on and not opened and n_lanes < max(1, len(cs_recs)):
            row["lanes"] = False
            row["why"] = next((str(r_["why"]) for _i, r_ in sorted(cs_recs.items())
                               if not r_.get("coupled") and r_.get("why")),
                              "its connections do not pair up end for end (a tee, or copper from before the run)")
        elif n_lanes:
            row["lanes"] = True
        elif not p.coupled and lanes_on and not opened:
            # never attempted as a pair: say which decision that was (detection confidence, the plan, the mode)
            row["lanes"] = False
            if str(p.why).startswith("completion first"):
                row["why"] = str(p.why)             # engine._pairs_opinion: as lanes it cost connections
            elif cons.mode == "plan" and p.src != "plan":
                row["why"] = "not declared in the plan (global.constraints: 'plan'): routed as two nets"
            elif p.src in ("name", "class") and p.conf < COUPLE_CONF:
                row["why"] = (f"routed as two nets: {str(p.why).split('; ')[-1]} (confidence {p.conf}); "
                              "diff_pairs[].coupled: true lays it as a pair")
            else:
                row["why"] = "plan: coupled false (routed as two nets)"
        for n_ in (p.p, p.n):                              # skew bumps (tune.skew_pair) on the shorter net
            tr = tune_recs.get(n_)
            if tr and str(tr.get("group", "")).startswith("pair:"):
                if tr.get("bumps"):
                    row["tuned"] = tr["bumps"]
                    row["added"] = _r(tr.get("added"))
                if tr.get("why") and "skew_over" in row:
                    row["skew_why"] = tr["why"]
        flagged = sorted({t for n in (p.p, p.n) for t in knets.get(n) or ()
                          if t in ("skew_out_of_range", "diff_pair_gap_out_of_range",
                                   "diff_pair_uncoupled_length_too_long")})
        if flagged:
            row["kicad_errors"] = flagged
            ok = False
        elif ok is not None and k is not None and p.src == "dru" and p.kicad and not opened:
            ok = True                                      # KiCad checks these very rules and is silent
        row["ok"] = ok
        rows.append(row)
        if ok is not None:
            out["ok" if ok else "missed"] += 1
        if ok is False:
            bad_nets.update((p.p, p.n))
    if rows:
        out["pairs"] = rows
    rows = []
    for n in sorted(cons.short):
        s = cons.short[n]
        if not parts(n).track > 0 and n not in klen:
            continue
        ln = length(n)
        row = {"net": n, "len": _r(ln)}
        est = sum(float(c.est or 0.0) for c in getattr(ctx, "conns", None) or () if c.net == n)
        if s.max_len is None and est > 0 and ln > DETOUR[0] * est + DETOUR[1]:
            row["est"] = _r(est, 2)                        # a soft keep-short net that took a detour
            row["detour"] = _r(ln - est, 2)
        if s.max_len is not None:
            row["max"] = s.max_len
            row["ok"] = ln <= s.max_len + 1e-9 and "length_out_of_range" not in (knets.get(n) or ())
            if ln > s.max_len + 1e-9:
                row["long_by"] = _r(ln - s.max_len)
            out["ok" if row["ok"] else "missed"] += 1
            if not row["ok"]:
                bad_nets.add(n)
        rows.append(row)
    if rows:
        out["short"] = rows
    if cons.widths:
        off, necked = {}, 0.0
        for n in sorted(cons.widths):
            rule = cons.widths[n]
            net = ctx.board.nets.get(n)
            pads = [(pd.x, pd.y) for pd in (net.pads if net is not None else ())]
            for lay, x1, y1, x2, y2, w in net_copper(ctx, n)[0]:
                want = cons.width_on(n, lay)
                if want is None or abs(w - want) <= 1e-6:
                    continue
                mm = math.hypot(x2 - x1, y2 - y1)
                if rule.neck and w < want and any(math.hypot(px - x1, py - y1) <= NECK_REACH
                                                  and math.hypot(px - x2, py - y2) <= NECK_REACH for px, py in pads):
                    necked += mm                           # a neck at a pad of a class-width net: allowed
                    continue
                key = (n, lay, round(w, 4), round(want, 4))
                off[key] = off.get(key, 0.0) + mm
        layers = getattr(ctx.board, "layers", None) or []
        strict = {n for n, r_ in cons.widths.items() if not r_.neck}
        wbad = {key[0] for key in off} & strict
        # rows of a class width (necking allowed at pads) end with "class": information, not a missed rule
        out["widths"] = {"n": len(cons.widths),
                         "off": [[n, layers[lay] if 0 <= lay < len(layers) else lay, w, want, _r(mm, 2)]
                                 + ([] if n in strict else ["class"]) for (n, lay, w, want), mm in sorted(off.items())]}
        if necked > 1e-9:
            out["widths"]["necked"] = _r(necked, 2)        # mm laid thinner at pads that need it
        out["ok"] += len(strict) - len(wbad)
        out["missed"] += len(wbad)
        bad_nets |= wbad
    if cons.unread:
        out["unread"] = list(cons.unread)
    if k is not None:
        out["kicad"] = dict(k.get("counts") or {})
        if k.get("warnings"):
            out["kicad_warnings"] = dict(k["warnings"])
        if k.get("unparsed"):
            out["kicad_unparsed"] = k["unparsed"]          # another KiCad language / version: the router's figures
        if klen:
            out["len_model_err"] = round(model_err, 4)     # largest |router figure - KiCad's own length|, mm
        other = sorted({(m["type"], (m["nets"] or ["?"])[0]) for m in k.get("markers") or []
                        if m.get("severity") == "error" and not any(n in bad_nets for n in m["nets"])})
        if other:
            out["kicad_other"] = [list(t) for t in other]  # KiCad errors no missed row above explains
    constrained = sorted({n for g in cons.groups for n in g.nets} | {q for p in cons.pairs for q in (p.p, p.n)}
                         | set(cons.short))
    # per net [total, track, via, die, vias]: the router's OWN figures (never KiCad's), to 0.1 um like KiCad prints
    out["nets"] = {n: [_r(parts(n).total, 4), _r(parts(n).track, 4), _r(parts(n).via, 4), _r(parts(n).die, 4),
                       parts(n).vias] for n in constrained if n in cache}
    if tune_recs:
        # per tuned / untunable net: [length before, after, bumps laid] (+ the reason when it still misses)
        out["tuning"] = {n: [r.get("before"), r.get("after"), r.get("bumps") or 0] + ([r["why"]] if r.get("why") else [])
                         for n, r in sorted(tune_recs.items())}
    return out
