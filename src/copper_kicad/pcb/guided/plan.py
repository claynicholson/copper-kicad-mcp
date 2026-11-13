"""The routing *plan*: the heuristics a model (or nobody) hands to the guided router.

``classify``      net facts from names + topology (ground / power / diff pair / clock / high-speed …)
``default_plan``  the built-in heuristics written out as an editable plan
``resolve``       plan (possibly partial / None) + board -> validated ``Config``; every unknown
                  field, net, layer or bad value raises ToolError(code='BAD_PLAN') with the fix
"""
from __future__ import annotations

import difflib
import fnmatch
import re

from ...mcp.registry import ToolError
from ..v2 import planx as _planx          # v2 plan fields (imports nothing from this package at load time)

# ── schema (also the documentation a model sees) ──────────────────────────
GLOBAL_FIELDS = {
    "grid": "routing grid in mm, or 'auto' (0.1 mm on small boards, coarser on big ones)",
    "via_cost": "how many mm of extra track one via is worth (default 5; lower = more layer changes)",
    "bend_cost": "mm of extra track one 45° bend is worth (default 0.3; higher = straighter)",
    "style": "'45' (default) or '90' (orthogonal only)",
    "layer_directions": "{layer: 'h'|'v'|'any'} preferred direction per layer, e.g. {'F.Cu':'h','B.Cu':'v'}",
    "direction_bias": "0..3: cost factor for running against a layer's direction (default 0.5)",
    "layer_cost": "{layer: multiplier >= 1} make a layer less attractive (e.g. keep a ground-plane layer clean)",
    "layers": "[layer, ...] copper layers signals may use (default: all; inner plane layers excluded)",
    "ripup_rounds": "0..10 how often one connection may rip up others to get through (default 3)",
    "time_budget_s": "5..600 routing budget in seconds of a typical desktop CPU (counted as search work, so the "
                     "result does not depend on machine speed); what is left when it runs out is reported as failed "
                     "(default 90)",
    "effort": "0.2..5 multiplier on the per-connection search limit (default 1)",
    "engine": "'v2' (default: the staged router; reads the v2 fields below) or 'guided' (the classic router)",
    "stages": "v2 only: [stage, ...] of classify, power, fanout, global, detail, repair, polish, judge (default all)",
}
NET_FIELDS = {
    "priority": "0..100, higher routes earlier (ignored for nets matched by 'order')",
    "width": "track width mm (default: the net class width)",
    "clearance": "clearance mm (never below the net class / board minimum)",
    "layers": "[layer, ...] the only layers this net may use",
    "prefer": "[layer, ...] preferred layers (others cost 2.5x)",
    "layer_cost": "{layer: multiplier >= 1}",
    "direction": "{layer: 'h'|'v'|'any'} per-net override of global.layer_directions",
    "via_cost": "mm per via for this net",
    "max_vias": "max vias per connection (0 = stay on one layer)",
    "max_length": "max total routed length of the net, mm (a longer result is reported as failed, not written)",
    "keep_short": "true = route early and as directly as possible (crystals, RF, switch nodes)",
    "avoid": "[[x0,y0,x1,y1], ...] rectangles (mm) this net must not enter",
    "corridor": "[[x,y], ...] guide path (mm) the route should follow between its pads",
    "corridor_width": "mm, full width of the corridor (default 3)",
    "corridor_strict": "true = leaving the corridor is forbidden (default false: it only costs 3x)",
    "neck": "true (default) = a wide track may fall back to the default width where it does not fit",
    "skip": "true = do not route (e.g. leave GND for the pour)",
}
PAIR_FIELDS = {"p": "positive net", "n": "negative net", "gap": "mm between the two tracks", "width": "track width mm"}
REGION_FIELDS = {
    "rect": "[x0,y0,x1,y1] mm (required)",
    "cost": "multiplier > 1: tracks may cross but it is expensive",
    "forbid": "true: no track cell inside (nets with a pad inside cannot leave it — use 'except')",
    "layers": "[layer, ...] (default: all copper layers)",
    "nets": "[name or glob, ...] nets it applies to (default: all)",
    "except": "[name or glob, ...] nets it does not apply to",
    "name": "label echoed in reports",
}
PLAN_FIELDS = {
    "order": "[net name or glob, ...] routing order; nets not listed follow by priority, then short-first",
    "global": GLOBAL_FIELDS,
    "nets": {"<net name or glob>": NET_FIELDS},
    "diff_pairs": [PAIR_FIELDS],
    "regions": [REGION_FIELDS],
}

SCHEMA_TEXT = """plan = {
 order: ["/XTAL*", "/USB_D?", "*"],            // routing order (names/globs); unlisted nets: by priority, short first
 global: {grid:0.1|"auto", via_cost:5, bend_cost:0.3, style:"45"|"90",
          layer_directions:{"F.Cu":"h","B.Cu":"v"}, direction_bias:0.5, layer_cost:{"B.Cu":1.5},
          layers:["F.Cu","B.Cu"], ripup_rounds:3, time_budget_s:90, effort:1},
 nets: {"<name|glob>": {priority:0..100, width, clearance, layers:[..], prefer:[..], layer_cost:{..},
          direction:{layer:"h"|"v"|"any"}, via_cost, max_vias, max_length, keep_short:true,
          avoid:[[x0,y0,x1,y1]], corridor:[[x,y],..], corridor_width:3, corridor_strict:false,
          neck:true, skip:true}},
 diff_pairs: [{p:"/USB_DP", n:"/USB_DN", gap:0.2, width:0.2}],
 regions: [{rect:[x0,y0,x1,y1], cost:4 | forbid:true, layers:[..], nets:[..], except:[..], name:"antenna"}]
}  // every field optional; mm; exact net names win over globs; later globs win over earlier ones"""

_GROUND = re.compile(r"^/?([AaDdPp]?GND\w*|GND|VSS\w*|EARTH|0V|GROUND)$")
_POWER = re.compile(r"^/?(\+?\d+V\d*\w*|[+-]\d+V?\d*|V(CC|DD|BUS|IN|BAT|SYS|OUT|PP|REF|M|S)\w*|\w*(_PWR|_VCC|_VDD|PWR)|"
                    r"\+?(3V3|5V|12V|1V8|2V5|1V2|24V|48V|9V)\w*|[+]?V?BATT?\w*|VUSB\w*|AVDD\w*|DVDD\w*)$", re.I)
_W0, _W1 = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"          # word edges that treat '_' and '/' as separators
_CLOCK = re.compile(rf"(CLK|CLOCK|XTAL|OSC|XIN|XOUT|{_W0}XI{_W1}|{_W0}XO{_W1}|SCLK|{_W0}SCK{_W1}|MCLK|BCLK|LRCK|"
                    rf"{_W0}HSE|{_W0}LSE)", re.I)
_HS = re.compile(r"(USB|HDMI|ETH|RGMII|RMII|MIPI|LVDS|SDIO|SDMMC|QSPI|DDR|PCIE|SATA|TMDS|CSI|DSI|CAN[HL]|"
                 rf"{_W0}RF{_W1}|{_W0}ANT|TX[PN]{_W1}|RX[PN]{_W1})", re.I)
_ANALOG = re.compile(rf"(ADC|AIN|VREF|SENSE|{_W0}AN\d|AUDIO|{_W0}MIC|{_W0}FB{_W1})", re.I)
_PAIR_SUFFIX = (("_P", "_N"), ("+", "-"), ("P", "N"), ("_DP", "_DM"), ("DP", "DM"), ("DP", "DN"), ("_DP", "_DN"),
                ("D+", "D-"), ("_H", "_L"), ("H", "L"))


def short(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def find_pairs(names) -> dict:
    """{p_net: n_net} from name suffixes (USB_DP/USB_DN, D+/D-, TX_P/TX_N, CANH/CANL …)."""
    s = set(names)
    out = {}
    for n in sorted(s):
        for ps, ns in _PAIR_SUFFIX:
            if n.endswith(ps) and len(n) > len(ps):
                m = n[:len(n) - len(ps)] + ns
                if m in s and m != n and n not in out and n not in out.values() and m not in out:
                    # single letters P/N, H/L are only trusted next to a high-speed looking stem
                    if len(ps) == 1 and ps in "PH" and not _HS.search(n):
                        continue
                    out[n] = m
                    break
    return out


def _mst_len(pts):
    if len(pts) < 2:
        return 0.0
    used = [False] * len(pts)
    dist = [1e18] * len(pts)
    dist[0] = 0.0
    total = 0.0
    for _ in range(len(pts)):
        k = -1
        for i in range(len(pts)):
            if not used[i] and (k < 0 or dist[i] < dist[k]):
                k = i
        used[k] = True
        total += dist[k]
        x, y = pts[k]
        for i in range(len(pts)):
            if not used[i]:
                dx, dy = abs(pts[i][0] - x), abs(pts[i][1] - y)
                d = max(dx, dy) + 0.41421356 * min(dx, dy)
                if d < dist[i]:
                    dist[i] = d
    return total


def classify(board) -> dict:
    """{net: {'kind', 'pads', 'est', 'unrouted', 'tags': [...], 'pair': partner|None, 'role': 'p'|'n'|None}}"""
    names = [n for n, net in board.nets.items() if len(net.pads) >= 2]
    pairs = find_pairs(names)
    rev = {v: k for k, v in pairs.items()}
    out = {}
    for n in names:
        net = board.nets[n]
        s = short(n)
        tags = []
        fns = " ".join(p.fn for p in net.pads if p.fn)
        pts = {p.pt for p in net.pads if p.pt}
        cls = net.cls.lower()
        if _GROUND.match(s) or "gnd" in cls or "ground" in cls:
            kind = "ground"
        elif _POWER.match(s) or _POWER.match(s.rsplit(".", 1)[-1]) or "power" in cls or "pwr" in cls or "power_out" in pts:
            kind = "power"
        elif n in pairs or n in rev:
            kind = "diff"
        elif _CLOCK.search(s) or _CLOCK.search(fns):
            kind = "clock"
        elif _HS.search(s):
            kind = "hs"
        elif _ANALOG.search(s):
            kind = "analog"
        else:
            kind = "signal"
        if kind != "diff" and (n in pairs or n in rev):
            tags.append("diff")
        if _HS.search(s) and kind not in ("hs",):
            tags.append("hs")
        if re.search(rf"XTAL|OSC|XIN|XOUT|{_W0}HSE|{_W0}LSE|{_W0}XC[12]{_W1}|{_W0}XL[12]{_W1}", s + " " + fns, re.I):
            tags.append("crystal")
        cl = board.clusters.get(n, {})
        out[n] = {"kind": kind, "pads": len(net.pads), "est": round(_mst_len([(p.x, p.y) for p in net.pads]), 1),
                  "unrouted": max(0, len(cl) - 1), "tags": tags,
                  "pair": pairs.get(n) or rev.get(n), "role": "p" if n in pairs else ("n" if n in rev else None)}
    return out


def plane_layers(board) -> list[int]:
    """Layers mostly covered by one pour (>= 40 % of the board area)."""
    area = {}
    for z in board.pours:
        for l in z["layers"]:
            area[l] = area.get(l, 0.0) + z["area_mm2"]
    return sorted(board.layers.index(l) for l, a in area.items() if board.area > 0 and a >= 0.4 * board.area)


_PRIO = {"diff": 90, "clock": 85, "hs": 80, "analog": 60, "signal": 50, "power": 40, "ground": 20}


def default_plan(board, facts=None) -> dict:
    """The built-in heuristics as an explicit plan a model can edit and send back."""
    facts = facts or classify(board)
    plan = {"global": {"grid": board.auto_step(), "via_cost": 5, "bend_cost": 0.3, "ripup_rounds": 3}, "nets": {}}
    planes = plane_layers(board)
    outer = {0, board.L - 1}
    if board.L > 2:
        sig = [l for l in range(board.L) if l in outer or l not in planes]
        if len(sig) < board.L:
            plan["global"]["layers"] = [board.layers[l] for l in sig]
    if board.L == 2 and planes:
        # keep one plane as intact as possible: the side with fewer SMD pads (usually the bottom)
        smd = [0, 0]
        for p in board.pads:
            if p.kind == "smd" and len(p.layers) == 1:
                smd[0 if p.layers[0] == 0 else 1] += 1
        quiet = planes[0] if len(planes) == 1 else (1 if smd[1] <= smd[0] else 0)
        plan["global"]["layer_cost"] = {board.layers[quiet]: 1.2}
    pairs = []
    for n in sorted(facts):
        f = facts[n]
        d = {}
        net = board.nets[n]
        if f["kind"] == "ground" and not net.has_pour and f["pads"] >= 6:
            d["skip"] = True
        if "crystal" in f["tags"]:
            d["keep_short"] = True
            d["max_vias"] = 0
        elif f["kind"] in ("clock", "hs"):
            d["priority"] = _PRIO[f["kind"]]
        if f["role"] == "p":
            pairs.append({"p": n, "n": f["pair"], "gap": net.dp_gap, "width": net.dp_width})
        if d:
            plan["nets"][n] = d
    if pairs:
        plan["diff_pairs"] = pairs
    if not plan["nets"]:
        del plan["nets"]
    return plan


# ── resolved configuration ────────────────────────────────────────────────
class NetCfg:
    __slots__ = ("name", "rank", "priority", "skip", "skip_reason", "width", "clearance", "via", "drill",
                 "neck_widths", "layers_ok", "layer_cost", "layer_dir", "via_cost", "max_vias", "max_length",
                 "keep_short", "avoid", "corridor", "dp_partner", "dp_role", "dp_gap", "moves_key", "explicit")


class Region:
    __slots__ = ("idx", "rect", "cost", "forbid", "layers", "nets", "exc", "name")

    def applies(self, net):
        if self.exc and net in self.exc:
            return False
        return self.nets is None or net in self.nets


class Config:
    def __init__(self):
        self.step = 0.1
        self.via_cost = 5.0
        self.bend_cost = 0.3
        self.style = "45"
        self.direction_bias = 0.5
        self.ripup_rounds = 3
        self.ripup_cost = 4.0       # mm: price of crossing an already routed net when planning a rip-up
        self.history_cost = 0.3     # mm added to a cell each time a fight happens there
        self.time_budget = 90.0
        self.effort = 1.0
        self.hweight = 1.15
        self.regions = []
        self.nets = {}
        self.warnings = []
        self.used = {}              # the plan actually applied (defaults merged with the caller's plan)
        self.engine = "v2"          # 'v2' | 'guided' (plan.global.engine; resolve() sets it from planx)
        self.stages = None          # v2 stage subset (plan.global.stages), canonical order
        self.x = None               # v2.planx.V2Cfg: the resolved v2 fields

    def net(self, name) -> NetCfg:
        return self.nets[name]


def _bad(msg, fix=None, **data):
    raise ToolError(msg, code="BAD_PLAN", fix=fix or "pcb_route_plan() returns the schema, the net list and a "
                    "default plan to edit", data=data or None)


FIELD_ALIASES: dict = {}     # a common other spelling -> the field it means (only used for the BAD_PLAN hint)


def _fields(d, valid, where):
    if not isinstance(d, dict):
        _bad(f"plan.{where} must be an object, got {type(d).__name__}")
    unknown = [k for k in d if k not in valid]
    if unknown:
        hint = {k: difflib.get_close_matches(k, list(valid), 1) or [a for a in (FIELD_ALIASES.get(k),) if a in valid]
                for k in unknown}
        _bad(f"unknown field(s) {unknown} in {('plan.' + where) if where else 'plan'}; valid fields: {sorted(valid)}",
             fix="; ".join(f"'{k}' → '{v[0]}'" for k, v in hint.items() if v) or None)


def _num(v, where, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        _bad(f"plan.{where} must be a number, got {v!r}")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        _bad(f"plan.{where}={v} is out of range ({lo}..{hi})")
    return float(v)


def _rect(v, where):
    if not (isinstance(v, (list, tuple)) and len(v) == 4 and all(
            isinstance(x, (int, float)) and not isinstance(x, bool) and abs(x) < 1e7 for x in v)):
        _bad(f"plan.{where} must be [x0, y0, x1, y1] in mm, got {v!r}")
    x0, y0, x1, y1 = (float(x) for x in v)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


class _Names:
    def __init__(self, board):
        self.all = sorted(board.nets)
        self.routable = sorted(n for n, net in board.nets.items() if len(net.pads) >= 2)
        self.set = set(self.all)

    def match(self, pat, where) -> list[str]:
        where = where if where.startswith("argument ") else f"plan.{where}"
        if not isinstance(pat, str) or not pat:
            _bad(f"{where}: net names must be non-empty strings, got {pat!r}")
        if pat in self.set:
            return [pat]
        if any(ch in pat for ch in "*?["):
            hits = [n for n in self.routable if fnmatch.fnmatchcase(n, pat) or fnmatch.fnmatchcase(n.lstrip("/"), pat)
                    or fnmatch.fnmatchcase(short(n), pat)]
            if not hits:
                _bad(f"{where}: pattern '{pat}' matches no net",
                     fix=f"routable nets: {', '.join(self.routable[:60])}")
            return hits
        cands = [n for n in self.all if short(n) == short(pat)]
        if len(cands) == 1:
            return cands
        close = difflib.get_close_matches(pat, self.all, 4, 0.5) or difflib.get_close_matches(
            pat, [short(n) for n in self.all], 4, 0.5)
        _bad(f"{where}: net '{pat}' is not on the board" + (f" (ambiguous: {', '.join(cands)})" if cands else ""),
             fix=(f"did you mean {', '.join(repr(c) for c in close)}? " if close else "")
             + f"Routable nets: {', '.join(self.routable[:60])}")


def _layer(board, v, where):
    alias = {"top": "F.Cu", "bottom": "B.Cu", "F": "F.Cu", "B": "B.Cu", "front": "F.Cu", "back": "B.Cu"}
    n = alias.get(v, v)
    if n not in board.layers:
        _bad(f"plan.{where}: '{v}' is not a copper layer of this {board.L}-layer board",
             fix=f"use one of {board.layers}")
    return board.layers.index(n)


def _layers(board, v, where):
    if not isinstance(v, (list, tuple)) or not v:
        _bad(f"plan.{where} must be a non-empty list of layer names, e.g. {board.layers[:2]}")
    return tuple(sorted({_layer(board, x, where) for x in v}))


def _dirs(board, v, where):
    if not isinstance(v, dict):
        _bad(f"plan.{where} must be an object like {{'F.Cu':'h','B.Cu':'v'}}")
    out = {}
    for k, d in v.items():
        d = {"horizontal": "h", "vertical": "v", "x": "h", "y": "v", "none": "any"}.get(d, d)
        if d not in ("h", "v", "any"):
            _bad(f"plan.{where}.{k} must be 'h', 'v' or 'any', got {d!r}")
        out[_layer(board, k, where)] = d
    return out


def _lcost(board, v, where):
    if not isinstance(v, dict):
        _bad(f"plan.{where} must be an object like {{'B.Cu': 2}}")
    return {_layer(board, k, where): _num(x, f"{where}.{k}", 1.0, 50.0) for k, x in v.items()}


def resolve(board, plan: dict | None) -> Config:
    plan = plan or {}
    _fields(plan, PLAN_FIELDS, "")
    cfg = Config()
    names = _Names(board)
    facts = classify(board)
    base = default_plan(board, facts)
    used = {"global": dict(base.get("global") or {})}

    # ── global ────────────────────────────────────────────────────────────
    gl = plan.get("global") or {}
    _fields(gl, GLOBAL_FIELDS, "global")
    g = dict(base.get("global") or {})
    g.update(gl)
    used["global"].update(gl)
    step = g.get("grid", "auto")
    if step in ("auto", None):
        step = board.auto_step()
    step = _num(step, "global.grid", 0.02, 2.54)
    w, h = board.bbox[2] - board.bbox[0], board.bbox[3] - board.bbox[1]
    if (w / step + 3) * (h / step + 3) * board.L > 6_000_000:
        _bad(f"global.grid={step} mm gives {int((w / step) * (h / step) * board.L):,} cells on this "
             f"{w:.0f}×{h:.0f} mm {board.L}-layer board (limit 6,000,000)",
             fix=f"use global.grid='auto' (= {board.auto_step()} mm here) or a coarser grid")
    cfg.step = step
    used["global"]["grid"] = step
    cfg.via_cost = _num(g.get("via_cost", 5), "global.via_cost", 0.0, 200.0)
    cfg.bend_cost = _num(g.get("bend_cost", 0.3), "global.bend_cost", 0.0, 20.0)
    cfg.style = str(g.get("style", "45"))
    if cfg.style not in ("45", "90"):
        _bad(f"plan.global.style must be '45' or '90', got {g.get('style')!r}")
    cfg.direction_bias = _num(g.get("direction_bias", 0.5), "global.direction_bias", 0.0, 3.0)
    cfg.ripup_rounds = int(_num(g.get("ripup_rounds", 3), "global.ripup_rounds", 0, 10))
    cfg.time_budget = _num(g.get("time_budget_s", 90), "global.time_budget_s", 5, 600)
    cfg.effort = _num(g.get("effort", 1.0), "global.effort", 0.2, 5.0)
    gdir = _dirs(board, g["layer_directions"], "global.layer_directions") if g.get("layer_directions") else {}
    gcost = _lcost(board, g["layer_cost"], "global.layer_cost") if g.get("layer_cost") else {}
    glayers = _layers(board, g["layers"], "global.layers") if g.get("layers") else tuple(range(board.L))

    # ── per-net entries: defaults, then globs in order, then exact names ──
    entries = {}
    for n, d in (base.get("nets") or {}).items():
        entries[n] = dict(d)
    user_nets = plan.get("nets") or {}
    if not isinstance(user_nets, dict):
        _bad("plan.nets must be an object {net name or glob: {...}}")
    explicit = set()
    keyed = []
    for pat, d in user_nets.items():
        _fields(d, NET_FIELDS, f"nets['{pat}']")
        hits = names.match(pat, "nets")
        keyed.append((0 if hits == [pat] else -1, pat, hits, d))
    for _exact, pat, hits, d in sorted(keyed, key=lambda t: t[0]):
        for n in hits:
            entries.setdefault(n, {}).update(d)
            explicit.add(n)
    used_nets = {n: dict(d) for n, d in entries.items() if d}

    # ── order ─────────────────────────────────────────────────────────────
    rank = {}
    order = plan.get("order") or []
    if not isinstance(order, (list, tuple)):
        _bad("plan.order must be a list of net names / globs")
    for i, pat in enumerate(order):
        for n in names.match(pat, "order"):
            rank.setdefault(n, i)
    if order:
        used["order"] = list(order)

    # ── diff pairs ────────────────────────────────────────────────────────
    pairs = plan.get("diff_pairs")
    if pairs is None:
        pairs = base.get("diff_pairs") or []
    if not isinstance(pairs, (list, tuple)):
        _bad("plan.diff_pairs must be a list of {p, n, gap, width}")
    dp = {}
    used_pairs = []
    for i, d in enumerate(pairs):
        _fields(d, PAIR_FIELDS, f"diff_pairs[{i}]")
        if "p" not in d or "n" not in d:
            _bad(f"plan.diff_pairs[{i}] needs both 'p' and 'n' net names")
        p, n = names.match(d["p"], f"diff_pairs[{i}].p"), names.match(d["n"], f"diff_pairs[{i}].n")
        if len(p) != 1 or len(n) != 1 or p == n:
            _bad(f"plan.diff_pairs[{i}]: 'p' and 'n' must each name exactly one, different net")
        p, n = p[0], n[0]
        gap = _num(d["gap"], f"diff_pairs[{i}].gap", 0.05, 5.0) if d.get("gap") is not None else board.nets[p].dp_gap
        width = _num(d["width"], f"diff_pairs[{i}].width", 0.05, 5.0) if d.get("width") is not None else None
        dp[p] = (n, "p", gap, width)
        dp[n] = (p, "n", gap, width)
        used_pairs.append({"p": p, "n": n, "gap": gap, **({"width": width} if width else {})})
    if used_pairs:
        used["diff_pairs"] = used_pairs

    # ── regions ───────────────────────────────────────────────────────────
    regs = plan.get("regions") or []
    if not isinstance(regs, (list, tuple)):
        _bad("plan.regions must be a list of {rect, cost | forbid, ...}")
    for i, d in enumerate(regs):
        _fields(d, REGION_FIELDS, f"regions[{i}]")
        if "rect" not in d:
            _bad(f"plan.regions[{i}] needs rect: [x0, y0, x1, y1] (mm)")
        r = Region()
        r.idx = i
        r.rect = _rect(d["rect"], f"regions[{i}].rect")
        r.forbid = bool(d.get("forbid"))
        if not r.forbid and d.get("cost") is None and not any(d.get(k) is not None for k in _planx.V2_REGION):
            _bad(f"plan.regions[{i}] needs either cost: <multiplier> or forbid: true"
                 f" (or, for engine 'v2', {' / '.join(_planx.V2_REGION)})")
        r.cost = _num(d.get("cost", 1.0), f"regions[{i}].cost", 1.0, 1000.0)
        r.layers = _layers(board, d["layers"], f"regions[{i}].layers") if d.get("layers") else None
        r.nets = None
        if d.get("nets"):
            r.nets = set()
            for pat in d["nets"]:
                r.nets.update(names.match(pat, f"regions[{i}].nets"))
        r.exc = set()
        for pat in d.get("except") or []:
            r.exc.update(names.match(pat, f"regions[{i}].except"))
        r.name = str(d.get("name") or f"region {i}")
        cfg.regions.append(r)
        if r.forbid:
            x0, y0, x1, y1 = r.rect
            trapped = sorted({board.net_by_id[p.net].name for p in board.pads
                              if p.net and x0 <= p.x <= x1 and y0 <= p.y <= y1
                              and (r.layers is None or any(l in r.layers for l in p.layers))
                              and len(board.net_by_id[p.net].pads) >= 2 and r.applies(board.net_by_id[p.net].name)})
            if trapped:
                cfg.warnings.append(
                    f"regions[{i}] ('{r.name}') forbids an area that contains pads of {len(trapped)} net(s) "
                    f"({', '.join(trapped[:8])}{'…' if len(trapped) > 8 else ''}): those nets cannot leave their pads. "
                    f"List them in regions[{i}].except, or use cost instead of forbid.")
    if regs:
        used["regions"] = [dict(d) for d in regs]

    # ── build NetCfg for every net with >= 2 pads ─────────────────────────
    df_w = max(board.rules["default"].get("width") or 0.2, board.rules["track_width"])
    for n in names.routable:
        net = board.nets[n]
        f = facts[n]
        d = entries.get(n, {})
        where = f"nets['{n}']"
        nc = NetCfg()
        nc.name = n
        nc.explicit = n in explicit
        nc.rank = rank.get(n, len(order))
        nc.keep_short = bool(d.get("keep_short", False))
        nc.priority = _num(d["priority"], f"{where}.priority", 0, 100) if d.get("priority") is not None else \
            float(_PRIO.get(f["kind"], 50) + (8 if nc.keep_short else 0))
        nc.skip = bool(d.get("skip", False))
        nc.skip_reason = None
        if nc.skip and n not in explicit:
            nc.skip_reason = (f"ground net with {f['pads']} pads and no pour: left for pcb_pour(net='{n}') "
                              f"(plan.nets['{n}'].skip=false routes it as tracks)")
        nc.width = net.width
        nc.clearance = net.clearance
        nc.dp_partner = nc.dp_role = None
        nc.dp_gap = 0.0
        if n in dp:
            nc.dp_partner, nc.dp_role, nc.dp_gap, w_ = dp[n]
            nc.width = w_ or net.dp_width or net.width
        if d.get("width") is not None:
            nc.width = _num(d["width"], f"{where}.width", 0.01, 20.0)
        if nc.width < board.rules["track_width"] - 1e-9:
            _bad(f"plan.{where}.width={nc.width} is below the board's minimum track width "
                 f"{board.rules['track_width']} mm", fix=f"use width >= {board.rules['track_width']}")
        if d.get("clearance") is not None:
            c = _num(d["clearance"], f"{where}.clearance", 0.0, 20.0)
            if c < net.clearance - 1e-9:
                cfg.warnings.append(f"{n}: clearance {c} is below its net class clearance {net.clearance} mm — "
                                    f"using {net.clearance} (KiCad DRC would reject less)")
            nc.clearance = max(c, net.clearance)
        nc.via, nc.drill = net.via, net.drill
        nc.neck_widths = []
        if d.get("neck", True) and df_w < nc.width - 1e-9:
            nc.neck_widths = [df_w]
        nc.layers_ok = _layers(board, d["layers"], f"{where}.layers") if d.get("layers") else glayers
        nc.layer_cost = dict(gcost)
        if d.get("prefer"):
            pref = _layers(board, d["prefer"], f"{where}.prefer")
            for l in nc.layers_ok:
                if l not in pref:
                    nc.layer_cost[l] = max(nc.layer_cost.get(l, 1.0), 2.5)
        if d.get("layer_cost"):
            nc.layer_cost.update(_lcost(board, d["layer_cost"], f"{where}.layer_cost"))
        nc.layer_dir = dict(gdir)
        if d.get("direction"):
            nc.layer_dir.update(_dirs(board, d["direction"], f"{where}.direction"))
        nc.via_cost = _num(d["via_cost"], f"{where}.via_cost", 0.0, 200.0) if d.get("via_cost") is not None else cfg.via_cost
        nc.max_vias = int(_num(d["max_vias"], f"{where}.max_vias", 0, 50)) if d.get("max_vias") is not None else None
        nc.max_length = _num(d["max_length"], f"{where}.max_length", 0.1, 1e5) if d.get("max_length") is not None else None
        av = d.get("avoid") or []
        if not isinstance(av, (list, tuple)):
            _bad(f"plan.{where}.avoid must be a list of [x0,y0,x1,y1] rectangles")
        nc.avoid = [_rect(r, f"{where}.avoid[{i}]") for i, r in enumerate(av)]
        nc.corridor = None
        if d.get("corridor"):
            pts = d["corridor"]
            if not (isinstance(pts, (list, tuple)) and pts and all(
                    isinstance(p, (list, tuple)) and len(p) == 2 and all(
                        isinstance(x, (int, float)) and not isinstance(x, bool) and abs(x) < 1e7 for x in p) for p in pts)):
                _bad(f"plan.{where}.corridor must be a list of [x, y] points in mm")
            nc.corridor = {"points": [(float(p[0]), float(p[1])) for p in pts],
                           "width": _num(d.get("corridor_width", 3.0), f"{where}.corridor_width", 0.2, 500.0),
                           "strict": bool(d.get("corridor_strict", False)), "outside_cost": 3.0}
        nc.moves_key = (nc.layers_ok, tuple(sorted(nc.layer_cost.items())), tuple(sorted(nc.layer_dir.items())),
                        nc.keep_short)
        cfg.nets[n] = nc
    if used_nets:
        used["nets"] = used_nets
    cfg.used = used
    cfg.x = _planx.resolve(board, plan, cfg)      # v2 fields: validated for every engine, used by 'v2'
    cfg.engine, cfg.stages = cfg.x.engine, cfg.x.stages
    return cfg


_planx.extend_schema(__import__("sys").modules[__name__])      # v2 fields join the tables above (unknown → BAD_PLAN)
