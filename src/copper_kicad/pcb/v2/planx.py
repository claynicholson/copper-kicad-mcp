"""v2 plan fields: schema extension and resolution into ``V2Cfg`` (design §6.1, §9).

The plan stays one JSON object. ``guided.plan`` owns the v1 fields and the validation helpers;
this module adds the v2 fields to its field tables (``extend_schema``, called once when
``guided.plan`` is imported, so unknown fields still raise ``BAD_PLAN``) and turns their values into
a ``V2Cfg`` (``resolve``, called at the end of ``guided.plan.resolve``; the result is ``cfg.x``).

Reconciliation with the v1 schema (design §6.1): width, clearance, corridor, avoid, layer
directions, neck and max_vias already exist and are reused unchanged; nothing here renames or
shadows a v1 field. Every v2 field is optional and its default reproduces un-steered behaviour.

Imports from v2: ``types`` only (``classify`` lazily and optionally, for ``auto_preview``).
"""
from __future__ import annotations

import difflib
import fnmatch
import re

from .types import ROLES

ENGINES = ("guided", "v2")
DEFAULT_ENGINE = "v2"              # owner decision 2026-11-11; "guided" (v1) stays selectable via global.engine
STAGES = ("classify", "power", "fanout", "global", "detail", "repair", "polish", "judge")
TREES = ("star", "chain", "mst")
FANOUT_STYLES = ("out", "in", "via", "auto")
SIDES = ("n", "e", "s", "w")
_SIDE_ALIAS = {"north": "n", "east": "e", "south": "s", "west": "w", "up": "n", "right": "e", "down": "s", "left": "w",
               "-y": "n", "+x": "e", "+y": "s", "-x": "w"}

# ── schema (name -> help text; also what a model reads) ───────────────────
V2_GLOBAL: dict = {
    "engine": "'v2' (default; staged router: reservations, fan-out, global corridors, repair, polish) or 'guided' "
              "(the classic single-pass router)",
    "stages": "v2: which stages run, any of classify, power, fanout, global, detail, repair, polish, judge "
              "(default all). With dry_run, stages ending at 'global' returns cuts + the corridor sketch in seconds",
    "tile_mm": "v2: global-route tile size 0.25..5 mm (default: 1, or 0.5 with pad pitch <= 0.5 mm)",
    "negotiation_iters": "v2: 1..100 global negotiation rounds (default 30)",
    "power_width_factor": "v2: 1..10, auto width of power nets = class width x this, capped by the pads (default 2)",
    "fanout": "v2: 'auto' (default) or 'none' = no fine-pitch escape stage",
    "polish": "v2: true (default) = pull tight, remove vias, re-widen after routing",
    "plane_rescue": "v2: true (default) = a connection with no way on the signal layers may cross a plane layer "
                    "(inner layer left out of global.layers because a pour covers it); false keeps planes untouched",
    "via_frugal": "v2: true (default) = a board without plane layers is first routed at 3 x via_cost and that result "
                  "kept when it is complete and clean (fewer vias, longer detours); false = the plain run only",
    "constraints": "v2: 'auto' (default: the plan's length groups / pairs / widths plus what KiCad's rules, net classes "
                   "and net names say) | 'plan' (only what the plan declares) | 'report' (measure only) | 'off'",
}
CONSTRAINT_MODES = ("auto", "plan", "report", "off")
V2_NET: dict = {
    "role": "v2: plane | power | pair | bus | critical | signal (overrides the automatic role)",
    "protect": "v2: true = this net's copper is never ripped up to let another net through",
    "via_budget": "v2: vias per connection the layer planner should aim for (soft; max_vias stays the hard limit)",
    "waypoints": "v2: [[x, y] or [x, y, layer], ...] points (mm) the route must visit in order",
    "between": "v2: [['U3.4','U3.5'], ...] route through the gap between these two pads",
    "side_of": "v2: [{ref:'U3', side:'cw'|'ccw'}] pass that part on this side",
    "tree": "v2: star | chain | mst — how a net with more than two pads is split into connections",
    "widths": "v2: {layer: mm} track width per layer (controlled impedance); `width` stays the value elsewhere",
    "z": "v2: 10..300 ohm, impedance label of the net (reported with the width; never changes a width)",
}
V2_TOP: dict = {
    "buses": "v2: [{nets:[...], order:[...], layer, spacing}] nets that travel together, side by side in `order`",
    "fanout": "v2: {'<ref or glob>': {style: out|in|via|auto, side: n|e|s|w, layer}} escape style per part",
    "reserve": "v2: [{at:[x,y], for:'<net>'}] a via site kept free for that net",
    "groups": "v2: [{nets:[...], tol, len, amp, name, off}] nets whose lengths must match within tol mm",
}
V2_REGION: dict = {
    "capacity_scale": "v2: 0.05..4 multiplier on how many tracks the global router plans through the rectangle",
    "no_vias": "v2: true = no vias inside the rectangle",
}
BUS_FIELDS = {"nets": "[net or glob, ...] at least two nets (required)",
              "order": "[net, ...] the same nets in lateral order (default: natural sort)",
              "layer": "layer the bundle should run on", "spacing": "centre-to-centre pitch mm (default width + clearance)"}
FANOUT_FIELDS = {"style": "out | in | via | auto (default auto)", "side": "n | e | s | w: where the escapes head "
                 "(board directions, y grows downwards so n = smaller y)", "layer": "layer the escapes should end on"}
RESERVE_FIELDS = {"at": "[x, y] mm (required)", "for": "net name (required)"}
GROUP_FIELDS = {"nets": "[net or glob, ...] the members (required; one net is enough with len)",
                "tol": "0.01..100 mm allowed spread: every member within [longest - tol, longest], or within "
                       "len +- tol when len is given (required unless off)",
                "len": "0.1..1e5 mm explicit target length (default: the longest member after routing)",
                "amp": "0.3..20 mm largest meander amplitude for this group",
                "name": "label in reports (default G<n>)",
                "off": "true = these nets are in no length group, whatever the rules or names say"}
PAIR_X_FIELDS = {"skew": "0.005..100 mm max length difference inside the pair",
                 "uncoupled": "0..1e4 mm max length of each net that does not run beside its partner",
                 "coupled": "true (default when the pads allow it) = one object at the gap; false = two nets, one "
                            "after the other",
                 "z": "10..300 ohm differential impedance label (reported; never changes a width)"}

# other spellings a caller is likely to try (the BAD_PLAN hint then names the real field)
FIELD_ALIASES = {"tolerance": "tol", "tolerance_mm": "tol", "target": "len", "target_mm": "len", "length": "len",
                 "target_length": "len", "amplitude": "amp", "max_skew": "skew", "max_skew_mm": "skew",
                 "skew_mm": "skew", "impedance": "z", "impedance_ohm": "z", "ohm": "z", "max_uncoupled": "uncoupled"}

SCHEMA_V2 = """
// v2 fields (engine 'v2', the default; ignored by global.engine:"guided", still validated):
 global: {engine:"v2"|"guided", stages:["classify","power","fanout","global","detail","repair","polish","judge"],
          tile_mm, negotiation_iters:30, power_width_factor:2, fanout:"auto"|"none", polish:true, plane_rescue:true,
          via_frugal:true}
 nets: {"<name|glob>": {role:"plane"|"power"|"pair"|"bus"|"critical"|"signal", protect:true, via_budget:2,
          waypoints:[[x,y,layer?]], between:[["U3.4","U3.5"]], side_of:[{ref,side:"cw"|"ccw"}], tree:"star"|"chain"|"mst"}}
 buses: [{nets:[..], order:[..], layer, spacing}], fanout: {"<ref>": {style:"out"|"in"|"via"|"auto", side:"n|e|s|w", layer}},
 reserve: [{at:[x,y], for:"<net>"}], regions[]: + capacity_scale:0.5, no_vias:true (cost/forbid then optional)
 global.constraints:"auto"|"plan"|"report"|"off", groups: [{nets:[..], tol:0.5, len, amp, name, off}] (length match),
 diff_pairs[]: + skew, uncoupled, coupled, z; nets: + widths:{"F.Cu":0.15}, z:50"""
_MARK = "// v2 fields"


def v2_plan(suggested: dict) -> dict:
    """The suggested plan of ``pcb_route_plan`` (v1's ``default_plan``) turned into a plan that routes the whole
    board with engine 'v2' (what the benchmark sends): ``global.engine = 'v2'``, and every automatic ``skip: true``
    (a ground net without a pour, left for a later ``pcb_pour``) becomes an explicit ``skip: false`` — the net is
    then routed as a power net (design §4.3). Without that entry both engines leave such a net for the pour, as the
    tool promises. Everything else is copied unchanged; idempotent."""
    import copy
    plan = copy.deepcopy(suggested or {})
    plan.setdefault("global", {})["engine"] = "v2"
    nets = plan.get("nets") or {}
    for name in sorted(nets):
        d = nets[name]
        if isinstance(d, dict) and d.get("skip") is True:
            d["skip"] = False
    return plan


def extend_schema(plan_module) -> None:
    """Add the v2 fields to guided.plan's field tables and schema text. Idempotent."""
    for k, v in V2_GLOBAL.items():
        plan_module.GLOBAL_FIELDS.setdefault(k, v)
    for k, v in V2_NET.items():
        plan_module.NET_FIELDS.setdefault(k, v)
    for k, v in V2_REGION.items():
        plan_module.REGION_FIELDS.setdefault(k, v)
    pf = plan_module.PLAN_FIELDS
    pf.setdefault("buses", [BUS_FIELDS])
    pf.setdefault("fanout", {"<footprint reference or glob>": FANOUT_FIELDS})
    pf.setdefault("reserve", [RESERVE_FIELDS])
    pf.setdefault("groups", [GROUP_FIELDS])
    for k, v in PAIR_X_FIELDS.items():
        plan_module.PAIR_FIELDS.setdefault(k, v)
    if hasattr(plan_module, "FIELD_ALIASES"):
        plan_module.FIELD_ALIASES.update(FIELD_ALIASES)
    if _MARK not in plan_module.SCHEMA_TEXT:
        plan_module.SCHEMA_TEXT = plan_module.SCHEMA_TEXT + SCHEMA_V2


# ── resolved configuration ────────────────────────────────────────────────
class NetX:
    """Resolved v2 hints of one net."""
    __slots__ = ("role", "protect", "via_budget", "waypoints", "between", "side_of", "tree")

    def __init__(self):
        self.role = None          # None = decide automatically (classify)
        self.protect = False
        self.via_budget = None    # None = no soft target
        self.waypoints = ()       # ((x, y, layer index | None), ...)
        self.between = ()         # (('U3.4', 'U3.5'), ...)
        self.side_of = ()         # (('U3', 'cw'|'ccw'), ...)
        self.tree = None          # None = incremental Steiner order

    def as_dict(self) -> dict:
        d = {}
        for k in self.__slots__:
            v = getattr(self, k)
            if v not in (None, False, ()):
                d[k] = [list(t) for t in v] if isinstance(v, tuple) else v
        return d


class RegionX:
    """v2 part of one plan region (same index as cfg.regions[idx])."""
    __slots__ = ("idx", "rect", "layers", "nets", "exc", "name", "capacity_scale", "no_vias")

    def applies(self, net) -> bool:
        if self.exc and net in self.exc:
            return False
        return self.nets is None or net in self.nets

    def contains(self, x, y, layer=None) -> bool:
        x0, y0, x1, y1 = self.rect
        return x0 <= x <= x1 and y0 <= y <= y1 and (layer is None or self.layers is None or layer in self.layers)


_NO_NET = NetX()


class V2Cfg:
    """Resolved v2 hints + auto{} (everything the router decided on its own, filled by the stages)."""

    def __init__(self):
        self.engine = DEFAULT_ENGINE
        self.stages = STAGES                 # canonical order, always contains 'classify'
        self.tile_mm = None                  # None = automatic (design §4.6)
        self.negotiation_iters = 30
        self.power_width_factor = 2.0
        self.fanout = "auto"                 # 'auto' | 'none'
        self.polish = True
        self.plane_rescue = True             # failed connections may cross a plane layer (engine, after the ladder)
        self.via_frugal = True               # an easy board is routed at 3 x the via cost (engine._frugal)
        self.nets = {}                       # net name -> NetX (only nets with a v2 hint)
        self.buses = []                      # [{'nets': [...], 'order': [...], 'layer': int | None, 'spacing': float | None}]
        self.fanout_parts = {}               # ref -> {'style', 'side': 'n'|'e'|'s'|'w'|None, 'layer': int | None}
        self.reserve = []                    # [{'at': (x, y), 'net': name}]
        self.regions = []                    # [RegionX] regions that carry capacity_scale / no_vias
        self.given = []                      # 'scope.field' of every v2 field the plan set (sorted)
        self.warnings = []
        self.auto = {"roles": {}, "buses": [], "widths": {}, "layer_dirs": {}}
        self.overlays = None                 # diag.overlay_set() of the last diagnosis (the tool draws it)
        self.cons_plan = cons_plan_default() # the plan's constraint declarations, validated (design §14.3)
        self.cons = None                     # constraints.Constraints once the engine resolved them (design §14.2)
        self.cons_env = None                 # {'stack', 'die', 'use_height'} for constraints.net_length
        self._bus_of = {}

    # ── lookups the stages use ────────────────────────────────────────────
    def net(self, name) -> NetX:
        return self.nets.get(name, _NO_NET)

    def has(self, stage: str) -> bool:
        return stage in self.stages

    @property
    def last_stage(self) -> str:
        return self.stages[-1]

    def bus_of(self, net):
        """The declared bus this net belongs to (dict) or None."""
        i = self._bus_of.get(net)
        return None if i is None else self.buses[i]

    def fanout_for(self, ref) -> dict:
        return self.fanout_parts.get(ref) or {"style": "auto" if self.fanout == "auto" else "none", "side": None,
                                              "layer": None}

    def capacity_scale(self, x, y, layer=None, net=None) -> float:
        """Product of region.capacity_scale of every region covering the point."""
        s = 1.0
        for r in self.regions:
            if r.capacity_scale != 1.0 and r.contains(x, y, layer) and (net is None or r.applies(net)):
                s *= r.capacity_scale
        return s

    def no_vias(self, x, y, net=None) -> bool:
        for r in self.regions:
            if r.no_vias and r.contains(x, y) and (net is None or r.applies(net)):
                return True
        return False

    def as_dict(self) -> dict:
        """Everything resolved, as plain data (auto{} excluded): equal dicts = equal steering."""
        return {
            "engine": self.engine, "stages": list(self.stages), "tile_mm": self.tile_mm,
            "negotiation_iters": self.negotiation_iters, "power_width_factor": self.power_width_factor,
            "fanout": self.fanout, "polish": self.polish, "plane_rescue": self.plane_rescue,
            "via_frugal": self.via_frugal,
            "nets": {n: self.nets[n].as_dict() for n in sorted(self.nets) if self.nets[n].as_dict()},
            "buses": [dict(b) for b in self.buses],
            "fanout_parts": {r: dict(self.fanout_parts[r]) for r in sorted(self.fanout_parts)},
            "reserve": [{"at": list(r["at"]), "net": r["net"]} for r in self.reserve],
            "regions": [{"idx": r.idx, "rect": list(r.rect), "capacity_scale": r.capacity_scale, "no_vias": r.no_vias}
                        for r in self.regions],
            "constraints": cons_plan_dict(self.cons_plan),
        }


def cons_plan_default() -> dict:
    """V2Cfg.cons_plan of a plan that says nothing about constraints.
      mode         'auto' | 'plan' | 'report' | 'off'
      groups       [{'name' | None, 'nets': (sorted names), 'tol', 'len' | None, 'amp' | None}]
      off          (nets vetoed by groups[].off)          no_groups   True for ``groups: []``
      pairs        {p net: {'n', 'gap' | None, 'width' | None, 'skew', 'uncoupled', 'coupled', 'z'}} (as declared)
      pairs_given  the plan has a diff_pairs list (then it is the whole list: name detection is off)
      widths       {net: ((layer index, mm), ...)}         z   {net: ohm}"""
    return {"mode": "auto", "groups": [], "off": (), "no_groups": False, "pairs": {}, "pairs_given": False,
            "widths": {}, "z": {}}


def cons_plan_dict(cp: dict) -> dict:
    """cons_plan as plain, comparable data (for as_dict)."""
    cp = cp or cons_plan_default()
    return {"mode": cp["mode"], "groups": [{k: (list(v) if isinstance(v, tuple) else v) for k, v in g.items()}
                                           for g in cp["groups"]],
            "off": sorted(cp["off"]), "no_groups": cp["no_groups"],
            "pairs": {p: {k: v for k, v in d.items() if v is not None} for p, d in sorted(cp["pairs"].items())
                      if any(d.get(k) is not None for k in PAIR_X_FIELDS)},
            "widths": {n: [list(t) for t in w] for n, w in sorted(cp["widths"].items())},
            "z": dict(sorted(cp["z"].items()))}


def _natural(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e7


def resolve(board, plan: dict | None, cfg) -> V2Cfg:
    """v2 part of the plan -> V2Cfg. Raises ToolError(code='BAD_PLAN') via guided.plan._bad.

    Called by guided.plan.resolve after the v1 part is validated (so plan / global / nets / regions
    have the right shapes and only known fields). Adds the top-level v2 fields to cfg.used and its
    own notes to cfg.warnings."""
    from ..guided import plan as P
    bad, num = P._bad, P._num
    plan = plan or {}
    x = V2Cfg()
    given = set()
    names = P._Names(board)

    # ── global ────────────────────────────────────────────────────────────
    g = plan.get("global") or {}
    for k in V2_GLOBAL:
        if g.get(k) is not None and k != "engine":
            given.add(f"global.{k}")
    if g.get("engine") is not None:
        if g["engine"] not in ENGINES:
            bad(f"plan.global.engine must be one of {list(ENGINES)}, got {g['engine']!r}",
                fix="'v2' is the staged router (the default), 'guided' the classic one")
        x.engine = g["engine"]
    if g.get("stages") is not None:
        st = g["stages"]
        if not isinstance(st, (list, tuple)) or not st or not all(isinstance(s, str) for s in st):
            bad(f"plan.global.stages must be a non-empty list of stage names, got {st!r}", fix=f"stages: {list(STAGES)}")
        unknown = [s for s in st if s not in STAGES]
        if unknown:
            hint = [difflib.get_close_matches(s, STAGES, 1) for s in unknown]
            bad(f"plan.global.stages: unknown stage(s) {unknown}; valid: {list(STAGES)}",
                fix="; ".join(f"'{s}' → '{h[0]}'" for s, h in zip(unknown, hint) if h) or None)
        x.stages = tuple(s for s in STAGES if s in st or s == "classify")
        if "classify" not in st:
            x.warnings.append("global.stages: 'classify' always runs (every other stage needs its connections)")
    if g.get("tile_mm") is not None:
        x.tile_mm = num(g["tile_mm"], "global.tile_mm", 0.25, 5.0)
    if g.get("negotiation_iters") is not None:
        x.negotiation_iters = int(num(g["negotiation_iters"], "global.negotiation_iters", 1, 100))
    if g.get("power_width_factor") is not None:
        x.power_width_factor = num(g["power_width_factor"], "global.power_width_factor", 1.0, 10.0)
    if g.get("fanout") is not None:
        if g["fanout"] not in ("auto", "none"):
            bad(f"plan.global.fanout must be 'auto' or 'none', got {g['fanout']!r}",
                fix="per-part styles go in the top-level plan.fanout: {'U3': {'style': 'via'}}")
        x.fanout = g["fanout"]
    if g.get("polish") is not None:
        if not isinstance(g["polish"], bool):
            bad(f"plan.global.polish must be true or false, got {g['polish']!r}")
        x.polish = g["polish"]
    if g.get("plane_rescue") is not None:
        if not isinstance(g["plane_rescue"], bool):
            bad(f"plan.global.plane_rescue must be true or false, got {g['plane_rescue']!r}")
        x.plane_rescue = g["plane_rescue"]
    if g.get("via_frugal") is not None:
        if not isinstance(g["via_frugal"], bool):
            bad(f"plan.global.via_frugal must be true or false, got {g['via_frugal']!r}")
        x.via_frugal = g["via_frugal"]
    cp = x.cons_plan
    if g.get("constraints") is not None:
        if g["constraints"] not in CONSTRAINT_MODES:
            close = difflib.get_close_matches(str(g["constraints"]), CONSTRAINT_MODES, 1)
            hint = f"did you mean '{close[0]}'? " if close else ""
            bad(f"plan.global.constraints must be one of {list(CONSTRAINT_MODES)}, got {g['constraints']!r}",
                fix=hint + "'auto' = the plan plus what KiCad's rules, net classes and net names say; 'plan' = only "
                "what the plan declares; 'report' = measure, lay no extra copper; 'off' = ignore constraints")
        cp["mode"] = g["constraints"]

    # ── nets: globs in plan order first, exact names last (same precedence as the v1 fields) ──
    pad_names = {}
    for p in board.pads:
        pad_names.setdefault(p.name, p)
    refs = sorted({p.ref for p in board.pads})
    keyed = []
    for pat, d in (plan.get("nets") or {}).items():
        v2 = {k: d[k] for k in V2_NET if d.get(k) is not None}
        if not v2:
            continue
        hits = names.match(pat, "nets")
        keyed.append((0 if hits == [pat] else -1, pat, hits, v2))
    merged = {}
    for _exact, pat, hits, v2 in sorted(keyed, key=lambda t: t[0]):
        for n in hits:
            merged.setdefault(n, {}).update(v2)
    for n in sorted(merged):
        d = merged[n]
        if n not in cfg.nets:
            continue                      # fewer than two pads: nothing to route, nothing to steer
        nc = cfg.nets[n]
        where = f"nets['{n}']"
        nx = NetX()
        for k in d:
            given.add(f"nets.{k}")
        if "role" in d:
            if d["role"] not in ROLES:
                bad(f"plan.{where}.role must be one of {list(ROLES)}, got {d['role']!r}")
            if d["role"] == "pair" and not nc.dp_partner:
                bad(f"plan.{where}.role='pair' but {n} is in no differential pair",
                    fix=f"declare it: plan.diff_pairs=[{{'p': '{n}', 'n': '<its partner>'}}]")
            if d["role"] == "plane" and not board.nets[n].has_pour:
                bad(f"plan.{where}.role='plane' but {n} has no pour to connect to",
                    fix=f"pcb_pour(net='{n}') first, or use role 'power' to route it as wide tracks")
            nx.role = d["role"]
        if "protect" in d:
            if not isinstance(d["protect"], bool):
                bad(f"plan.{where}.protect must be true or false, got {d['protect']!r}")
            nx.protect = d["protect"]
        if "via_budget" in d:
            nx.via_budget = int(num(d["via_budget"], f"{where}.via_budget", 0, 50))
            if nc.max_vias is not None and nx.via_budget > nc.max_vias:
                x.warnings.append(f"{n}: via_budget {nx.via_budget} is above max_vias {nc.max_vias} — using {nc.max_vias}")
                nx.via_budget = nc.max_vias
        if "tree" in d:
            if d["tree"] not in TREES:
                bad(f"plan.{where}.tree must be one of {list(TREES)}, got {d['tree']!r}")
            nx.tree = d["tree"]
        if "waypoints" in d:
            wp = d["waypoints"]
            if not isinstance(wp, (list, tuple)) or not wp:
                bad(f"plan.{where}.waypoints must be a list of [x, y] or [x, y, layer] points in mm")
            out = []
            for i, pt in enumerate(wp):
                if not (isinstance(pt, (list, tuple)) and len(pt) in (2, 3) and _is_num(pt[0]) and _is_num(pt[1])):
                    bad(f"plan.{where}.waypoints[{i}] must be [x, y] or [x, y, layer] in mm, got {pt!r}")
                px, py = float(pt[0]), float(pt[1])
                if not board.inside(px, py):
                    bx = board.bbox
                    bad(f"plan.{where}.waypoints[{i}]=({px}, {py}) is not on the board",
                        fix=f"the board spans x {round(bx[0], 2)}..{round(bx[2], 2)}, y {round(bx[1], 2)}..{round(bx[3], 2)} mm")
                lay = None
                if len(pt) == 3 and pt[2] is not None:
                    lay = P._layer(board, pt[2], f"{where}.waypoints[{i}]")
                    if lay not in nc.layers_ok:
                        bad(f"plan.{where}.waypoints[{i}]: layer {board.layers[lay]} is not allowed for {n}",
                            fix=f"allowed here: {[board.layers[l] for l in nc.layers_ok]} (plan.{where}.layers / global.layers)")
                out.append((px, py, lay))
            nx.waypoints = tuple(out)
        if "between" in d:
            bt = d["between"]
            if isinstance(bt, (list, tuple)) and len(bt) == 2 and all(isinstance(s, str) for s in bt):
                bt = [bt]                                    # one pair written without the outer list
            if not isinstance(bt, (list, tuple)) or not bt:
                bad(f"plan.{where}.between must be a list of pad pairs like [['U3.4', 'U3.5']]")
            out = []
            for i, pr_ in enumerate(bt):
                if not (isinstance(pr_, (list, tuple)) and len(pr_) == 2 and all(isinstance(s, str) for s in pr_)):
                    bad(f"plan.{where}.between[{i}] must be two pad names like ['U3.4', 'U3.5'], got {pr_!r}")
                for s in pr_:
                    if s not in pad_names:
                        close = difflib.get_close_matches(s, list(pad_names), 4, 0.5)
                        bad(f"plan.{where}.between[{i}]: there is no pad '{s}' on the board",
                            fix=(f"did you mean {', '.join(close)}? " if close else "") + "pads are named <ref>.<number>")
                if pr_[0] == pr_[1]:
                    bad(f"plan.{where}.between[{i}] names the same pad twice ({pr_[0]})")
                out.append((pr_[0], pr_[1]))
            nx.between = tuple(out)
        if "side_of" in d:
            so = d["side_of"]
            if isinstance(so, dict):
                so = [so]
            if not isinstance(so, (list, tuple)) or not so:
                bad(f"plan.{where}.side_of must be a list like [{{'ref': 'U3', 'side': 'cw'}}]")
            out = []
            for i, e in enumerate(so):
                if not isinstance(e, dict) or set(e) - {"ref", "side"} or "ref" not in e or "side" not in e:
                    bad(f"plan.{where}.side_of[{i}] must be {{ref, side}}, got {e!r}")
                if e["ref"] not in refs:
                    close = difflib.get_close_matches(str(e["ref"]), refs, 4, 0.5)
                    bad(f"plan.{where}.side_of[{i}]: there is no part '{e['ref']}' on the board",
                        fix=f"did you mean {', '.join(close)}?" if close else None)
                if e["side"] not in ("cw", "ccw"):
                    bad(f"plan.{where}.side_of[{i}].side must be 'cw' or 'ccw', got {e['side']!r}",
                        fix="cw = pass the part clockwise as seen from the route's first pad, ccw = the other way")
                out.append((e["ref"], e["side"]))
            nx.side_of = tuple(out)
        if "widths" in d:
            wd = d["widths"]
            if not isinstance(wd, dict) or not wd:
                bad(f"plan.{where}.widths must be an object like {{'F.Cu': 0.15, 'In2.Cu': 0.11}} (mm per layer)",
                    fix=f"one width on every layer is plan.{where}.width")
            lo_w = board.rules["track_width"]
            by = {}
            for lk, wv in wd.items():
                lay = P._layer(board, lk, f"{where}.widths")
                w_ = num(wv, f"{where}.widths.{lk}", 0.01, 20.0)
                if w_ < lo_w - 1e-9:
                    bad(f"plan.{where}.widths.{lk}={w_} is below the board's minimum track width {lo_w} mm",
                        fix=f"use a width >= {lo_w}")
                by[lay] = w_
            cp["widths"][n] = tuple(sorted(by.items()))
        if "z" in d:
            cp["z"][n] = num(d["z"], f"{where}.z", 10.0, 300.0)
        if nx.as_dict():
            x.nets[n] = nx

    # ── constraints: differential-pair extras (the v1 part validated p / n / gap / width) ──
    cp["pairs_given"] = plan.get("diff_pairs") is not None
    for i, d in enumerate(plan.get("diff_pairs") or []):
        pn, nn = names.match(d["p"], f"diff_pairs[{i}].p")[0], names.match(d["n"], f"diff_pairs[{i}].n")[0]
        e = {"n": nn, "gap": float(d["gap"]) if d.get("gap") is not None else None,
             "width": float(d["width"]) if d.get("width") is not None else None,
             "skew": None, "uncoupled": None, "coupled": None, "z": None}
        if d.get("skew") is not None:
            e["skew"] = num(d["skew"], f"diff_pairs[{i}].skew", 0.005, 100.0)
        if d.get("uncoupled") is not None:
            e["uncoupled"] = num(d["uncoupled"], f"diff_pairs[{i}].uncoupled", 0.0, 1e4)
        if d.get("coupled") is not None:
            if not isinstance(d["coupled"], bool):
                bad(f"plan.diff_pairs[{i}].coupled must be true or false, got {d['coupled']!r}",
                    fix="false = route the two nets one after the other (not gap-coupled)")
            e["coupled"] = d["coupled"]
        if d.get("z") is not None:
            e["z"] = num(d["z"], f"diff_pairs[{i}].z", 10.0, 300.0)
        cp["pairs"][pn] = e
        extra = {k: e[k] for k in PAIR_X_FIELDS if e[k] is not None}
        if extra:
            given.update(f"diff_pairs.{k}" for k in extra)
            for u in cfg.used.get("diff_pairs") or []:
                if u.get("p") == pn:
                    u.update(extra)

    # ── constraints: length groups ────────────────────────────────────────
    groups = plan.get("groups")
    if groups is not None:
        given.add("groups")
        if not isinstance(groups, (list, tuple)):
            bad("plan.groups must be a list of {nets, tol, len, amp, name, off}",
                fix='e.g. groups: [{"nets": ["/SD_*"], "tol": 0.5}]; groups: [] = no length groups at all')
        cp["no_groups"] = not groups
        in_group, off, used_g = {}, [], []
        for i, d in enumerate(groups):
            P._fields(d, GROUP_FIELDS, f"groups[{i}]")
            if not isinstance(d.get("nets"), (list, tuple)) or not d["nets"]:
                bad(f"plan.groups[{i}] needs nets: [net or glob, ...]")
            members = []
            for pat in d["nets"]:
                for n in names.match(pat, f"groups[{i}].nets"):
                    if n not in members and n in cfg.nets:
                        members.append(n)
            if d.get("off") is not None and not isinstance(d["off"], bool):
                bad(f"plan.groups[{i}].off must be true or false, got {d['off']!r}")
            if d.get("off"):
                off.extend(members)
                used_g.append({"nets": sorted(members), "off": True})
                continue
            if d.get("tol") is None:
                bad(f"plan.groups[{i}] needs tol: the allowed length spread in mm",
                    fix='e.g. {"nets": [...], "tol": 0.5}; {"nets": [...], "off": true} takes the nets out of every group')
            tol = num(d["tol"], f"groups[{i}].tol", 0.01, 100.0)
            ln = num(d["len"], f"groups[{i}].len", 0.1, 1e5) if d.get("len") is not None else None
            amp = num(d["amp"], f"groups[{i}].amp", 0.3, 20.0) if d.get("amp") is not None else None
            if d.get("name") is not None and (not isinstance(d["name"], str) or not d["name"].strip()):
                bad(f"plan.groups[{i}].name must be a non-empty string, got {d['name']!r}")
            if len(members) < (1 if ln is not None else 2):
                bad(f"plan.groups[{i}] needs at least two routable nets (one is enough with len), got {members}",
                    fix="a single net to a fixed length: add len; nets with fewer than two pads do not count")
            for n in members:
                if n in in_group:
                    bad(f"plan.groups[{i}]: {n} is already in groups[{in_group[n]}] — a net can be in one length group",
                        fix=f"drop it from one of the two, or merge groups[{in_group[n]}] and groups[{i}]")
                in_group[n] = i
            e = {"name": d["name"].strip() if d.get("name") else None, "nets": tuple(sorted(members)), "tol": tol,
                 "len": ln, "amp": amp}
            cp["groups"].append(e)
            used_g.append({k: (list(v) if isinstance(v, tuple) else v) for k, v in e.items() if v is not None})
        cp["off"] = tuple(sorted(set(off) - set(in_group)))
        cfg.used["groups"] = used_g

    # ── buses ─────────────────────────────────────────────────────────────
    buses = plan.get("buses")
    if buses is not None:
        if buses:
            given.add("buses")
        if not isinstance(buses, (list, tuple)):
            bad("plan.buses must be a list of {nets, order, layer, spacing}")
        used_b = []
        for i, d in enumerate(buses):
            P._fields(d, BUS_FIELDS, f"buses[{i}]")
            if not isinstance(d.get("nets"), (list, tuple)) or not d["nets"]:
                bad(f"plan.buses[{i}] needs nets: [net or glob, ...]")
            members = []
            for pat in d["nets"]:
                for n in names.match(pat, f"buses[{i}].nets"):
                    if n not in members:
                        members.append(n)
            members = [n for n in members if n in cfg.nets]
            if len(members) < 2:
                bad(f"plan.buses[{i}] needs at least two routable nets, got {members}")
            if d.get("order") is not None:
                if not isinstance(d["order"], (list, tuple)):
                    bad(f"plan.buses[{i}].order must be a list of net names")
                order = []
                for pat in d["order"]:
                    for n in names.match(pat, f"buses[{i}].order"):
                        if n not in order:
                            order.append(n)
                if sorted(order) != sorted(members):
                    miss = sorted(set(members) - set(order))
                    extra = sorted(set(order) - set(members))
                    bad(f"plan.buses[{i}].order must list exactly the bus nets"
                        + (f"; missing {miss}" if miss else "") + (f"; not in nets {extra}" if extra else ""))
            else:
                order = sorted(members, key=_natural)
            for n in order:
                if n in x._bus_of:
                    bad(f"plan.buses[{i}]: {n} is already in buses[{x._bus_of[n]}] — a net can be in one bus")
                x._bus_of[n] = i
            lay = P._layer(board, d["layer"], f"buses[{i}].layer") if d.get("layer") is not None else None
            sp = num(d["spacing"], f"buses[{i}].spacing", 0.05, 10.0) if d.get("spacing") is not None else None
            x.buses.append({"nets": sorted(members), "order": order, "layer": lay, "spacing": sp})
            used_b.append({"nets": list(order), **({"layer": board.layers[lay]} if lay is not None else {}),
                           **({"spacing": sp} if sp is not None else {})})
        if used_b:
            cfg.used["buses"] = used_b

    # ── per-part fan-out ──────────────────────────────────────────────────
    fo = plan.get("fanout")
    if fo is not None:
        if fo:
            given.add("fanout")
        if not isinstance(fo, dict):
            bad("plan.fanout must be an object {'<ref>': {style, side, layer}}",
                fix="the on/off switch is plan.global.fanout: 'auto' | 'none'")
        used_f = {}
        for pat, d in fo.items():
            P._fields(d, FANOUT_FIELDS, f"fanout['{pat}']")
            if pat in refs:
                hits = [pat]
            else:
                hits = [r for r in refs if fnmatch.fnmatchcase(r, pat)] if any(c in pat for c in "*?[") else []
            if not hits:
                close = difflib.get_close_matches(str(pat), refs, 4, 0.5)
                bad(f"plan.fanout: there is no part '{pat}' on the board",
                    fix=f"did you mean {', '.join(close)}?" if close else f"parts: {', '.join(refs[:40])}")
            style = d.get("style", "auto")
            if style not in FANOUT_STYLES:
                bad(f"plan.fanout['{pat}'].style must be one of {list(FANOUT_STYLES)}, got {style!r}")
            side = d.get("side")
            if side is not None:
                side = _SIDE_ALIAS.get(side, side)
                if side not in SIDES:
                    bad(f"plan.fanout['{pat}'].side must be 'n', 'e', 's' or 'w', got {d['side']!r}",
                        fix="board directions: n = towards smaller y (up on screen), e = larger x")
            lay = P._layer(board, d["layer"], f"fanout['{pat}'].layer") if d.get("layer") is not None else None
            for r in hits:
                x.fanout_parts[r] = {"style": style, "side": side, "layer": lay}
            used_f[pat] = dict(d)
        if used_f:
            cfg.used["fanout"] = used_f

    # ── reserved via sites ────────────────────────────────────────────────
    rs = plan.get("reserve")
    if rs is not None:
        if rs:
            given.add("reserve")
        if not isinstance(rs, (list, tuple)):
            bad("plan.reserve must be a list of {at: [x, y], for: net}")
        for i, d in enumerate(rs):
            P._fields(d, RESERVE_FIELDS, f"reserve[{i}]")
            at = d.get("at")
            if not (isinstance(at, (list, tuple)) and len(at) == 2 and _is_num(at[0]) and _is_num(at[1])):
                bad(f"plan.reserve[{i}] needs at: [x, y] in mm, got {at!r}")
            if not d.get("for"):
                bad(f"plan.reserve[{i}] needs for: '<net name>'")
            hit = names.match(d["for"], f"reserve[{i}].for")
            if len(hit) != 1:
                bad(f"plan.reserve[{i}].for must name exactly one net, '{d['for']}' matches {len(hit)}")
            px, py = float(at[0]), float(at[1])
            if not board.inside(px, py):
                bad(f"plan.reserve[{i}].at=({px}, {py}) is not on the board")
            x.reserve.append({"at": (px, py), "net": hit[0]})
        if x.reserve:
            cfg.used["reserve"] = [{"at": list(r["at"]), "for": r["net"]} for r in x.reserve]

    # ── regions: capacity_scale / no_vias ride on the v1 region of the same index ──
    for i, d in enumerate(plan.get("regions") or []):
        if d.get("capacity_scale") is None and d.get("no_vias") is None:
            continue
        base = cfg.regions[i]
        r = RegionX()
        r.idx, r.rect, r.layers, r.nets, r.exc, r.name = base.idx, base.rect, base.layers, base.nets, base.exc, base.name
        r.capacity_scale = 1.0
        if d.get("capacity_scale") is not None:
            r.capacity_scale = num(d["capacity_scale"], f"regions[{i}].capacity_scale", 0.05, 4.0)
            given.add("regions.capacity_scale")
        r.no_vias = False
        if d.get("no_vias") is not None:
            if not isinstance(d["no_vias"], bool):
                bad(f"plan.regions[{i}].no_vias must be true or false, got {d['no_vias']!r}")
            r.no_vias = d["no_vias"]
            given.add("regions.no_vias")
        x.regions.append(r)

    x.given = sorted(given)
    if x.engine != "v2" and x.given:
        x.warnings.append(f"the plan sets v2-only field(s) {x.given}, which the 'guided' engine ignores — "
                          f"drop global.engine='guided' (engine 'v2' is the default) to use them")
    cfg.warnings.extend(x.warnings)
    return x


# ── helpers for the engine, the tools and the tests ───────────────────────
def apply_clearances(oracle, board, cfg) -> int:
    """Tell the oracle about plan clearances above the net class (W0 note 8). Returns how many nets."""
    n = 0
    for name in sorted(cfg.nets):
        nc = cfg.nets[name]
        if nc.clearance > board.nets[name].clearance + 1e-9:
            oracle.set_clearance(name, nc.clearance)
            n += 1
    return n


_APPEND = ("regions", "buses", "reserve", "diff_pairs", "groups")


def merge_patch(plan: dict | None, patch: dict) -> dict:
    """Plan + a suggest[] patch -> new plan (inputs untouched). Objects merge key by key, the lists
    regions / buses / reserve / diff_pairs / groups are appended to, everything else (order, values) is replaced.
    A placement patch ({'move': ...}) is not a plan fragment and raises ValueError."""
    if "move" in patch:
        raise ValueError("a 'move' patch is for pcb_place, not for the plan")

    def merge(a, b, key=None):
        if isinstance(a, dict) and isinstance(b, dict):
            out = dict(a)
            for k, v in b.items():
                out[k] = merge(a[k], v, k) if k in a else _copy(v)
            return out
        if key in _APPEND and isinstance(a, list) and isinstance(b, list):
            return [_copy(v) for v in a] + [_copy(v) for v in b if v not in a]
        return _copy(b)
    return merge(plan or {}, patch)


def _copy(v):
    if isinstance(v, dict):
        return {k: _copy(w) for k, w in v.items()}
    if isinstance(v, (list, tuple)):
        return [_copy(w) for w in v]
    return v


def pad_minor(pad) -> float:
    """Smallest copper dimension of a pad, mm (0.0 when it has no copper)."""
    best = 0.0
    for shapes in pad.shapes.values():
        for s in shapes:
            if s.kind == "box":
                m = 2.0 * (min(s.g[4], s.g[5]) + s.g[6])
            else:
                m = min(s.bbox[2] - s.bbox[0], s.bbox[3] - s.bbox[1])
            if m > 0 and (best == 0.0 or m < best):
                best = m
    return best


def auto_preview(board, cfg) -> dict:
    """What v2 would decide on its own for this board + plan, for pcb_route_plan's `auto` (veto with
    plan.nets[..].role / width, plan.buses, global.layer_directions). Uses classify when that stage is
    built; otherwise the documented §4.3 rules on guided.plan's net facts. Compact: nets whose role is
    'signal' and widths equal to the class width are left out."""
    x = cfg.x if getattr(cfg, "x", None) is not None else V2Cfg()
    roles, buses, source = None, None, "rules"
    try:
        from . import classify
        if not getattr(classify, "STUB", False):
            roles = dict(classify.roles(board, cfg, x))
            buses = list(classify.detect_buses(board))
            source = "classify"
    except Exception:  # noqa: BLE001 — a sibling stage that is not ready must not break a read-only tool
        roles, buses = None, None
    if roles is None:
        from ..guided import plan as P
        facts = P.classify(board)
        roles = {}
        for n in sorted(cfg.nets):
            f = facts.get(n) or {}
            nx = x.net(n)
            if nx.role:
                roles[n] = nx.role
            elif board.nets[n].has_pour:
                roles[n] = "plane"
            elif f.get("kind") in ("ground", "power"):
                roles[n] = "power"
            elif cfg.nets[n].dp_partner:
                roles[n] = "pair"
            elif cfg.nets[n].keep_short or "crystal" in (f.get("tags") or []):
                roles[n] = "critical"
            else:
                roles[n] = "signal"
        buses = []
    widths = {}
    for n in sorted(roles):
        if roles[n] != "power" or n not in cfg.nets:
            continue
        nc, net = cfg.nets[n], board.nets[n]
        if abs(nc.width - net.width) > 1e-9:
            continue                                  # the plan (or a pair) chose the width
        minor = [m for m in (pad_minor(p) for p in net.pads) if m > 0]
        w = min(net.width * x.power_width_factor, 0.8 * min(minor) if minor else 1.0, 1.0)
        w = round(max(w, net.width), 3)
        if w > net.width + 1e-9:
            widths[n] = w
    for b in buses:
        for n in b.get("nets") or []:
            if roles.get(n) == "signal":
                roles[n] = "bus"
    out = {"roles": {n: r for n, r in sorted(roles.items()) if r != "signal"},
           "buses": [{k: b[k] for k in ("nets", "refs", "order", "layer") if b.get(k) is not None} for b in buses
                     if isinstance(b, dict)],
           "widths": widths, "layer_dirs": dict(x.auto.get("layer_dirs") or {}), "source": source}
    return out
