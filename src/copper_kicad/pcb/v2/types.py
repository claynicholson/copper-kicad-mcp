"""Shared data structures of router v2 (design §3). Frozen: change the design doc first.

Units are millimetres as Python floats, exactly as v1. Every coordinate v2 emits goes
through ``q()`` (1 nm, KiCad's internal unit) *before* the oracle checks it, so the
checked geometry is the geometry KiCad stores.

No clocks and no randomness anywhere in this module: ``Budget`` counts deterministic
work units on the same scale as v1 (``time_budget_s`` x 200 000).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# ── tolerances (calibrated against kicad-cli DRC in W0, see design Appendix A2) ──
TOL_MM = 0.0003       # legal iff dist >= need - TOL_MM. Measured (KiCad 10.0): DRC flags iff gap < need - 0.5 um;
#                       0.4 um is the largest tolerance with zero false-legal on the seeded ladder, and the
#                       worker's extract rounds static geometry to 0.1 um. Keep-outs and mask rules get none.
GUARD_MM = 0.002      # extra inflation used only when rasterising a window grid
PROTECT_FACTOR = 8.0  # rip price multiplier of protected pieces (escape stubs, dog-bones)
WORK_PER_SECOND = 200_000   # same scale as guided.router.WORK_PER_SECOND

# stage shares of the search budget (unused share rolls forward)
STAGE_SHARE = {"power": 0.03, "fanout": 0.05, "global": 0.10, "detail": 0.45, "repair": 0.22, "polish": 0.10,
               "judge": 0.05}
# work-unit charges
CHARGE_EXPANSION = 1          # per A* expansion (tile or cell)
CHARGE_SEARCH = 4000          # per window search started
CHARGE_WINDOW = 2000          # per window build
CHARGE_PATTERN = 200          # per pattern attempt
ORACLE_CHECKS_PER_UNIT = 8    # 1 unit per 8 oracle checks (Oracle.checks counts them)

ROLES = ("plane", "power", "pair", "bus", "critical", "signal")
ROLE_ORDER = {"critical": 0, "pair": 1, "bus": 2, "signal": 3, "power": 4, "plane": 5}
STAGES_OF_PIECE = ("power", "fanout", "detail", "repair", "polish", "legacy", "tune")
BLOCKER_RULES = ("clearance", "hole", "edge", "keepout", "mask", "short", "thermal", "reserve")


def q(v: float) -> float:
    """Quantise a coordinate / size to 1 nm. Idempotent: q(q(v)) == q(v)."""
    return round(v * 1e6) / 1e6


@dataclass(frozen=True)
class Piece:
    """One straight segment or one through via."""
    id: int               # monotone, assigned by RouteState
    net: str
    conn: int             # -1 for reservations not tied to a connection
    kind: str             # 'seg' | 'via'
    layer: int            # seg only (board layer index); -1 for via
    x1: float
    y1: float
    x2: float             # via: x1, y1 only (x2 == x1, y2 == y1)
    y2: float
    w: float              # seg width | via size
    drill: float = 0.0    # via only
    stage: str = "detail"       # 'power'|'fanout'|'detail'|'repair'|'polish'|'legacy'|'tune'
    protected: bool = False     # escape stub / dog-bone: rip price x PROTECT_FACTOR
    target_w: float = 0.0       # width polish should try to restore (== w when not necked; 0.0 means w)

    def __post_init__(self):
        if self.kind not in ("seg", "via"):
            raise ValueError(f"Piece.kind must be 'seg' or 'via', not {self.kind!r}")
        if not self.target_w:
            object.__setattr__(self, "target_w", self.w)

    @property
    def length(self) -> float:
        if self.kind == "via":
            return 0.0
        return ((self.x2 - self.x1) ** 2 + (self.y2 - self.y1) ** 2) ** 0.5


@dataclass(frozen=True)
class Terminal:
    """One end of a connection."""
    x: float
    y: float
    layers: tuple[int, ...]
    pad: str | None = None        # 'U3.4' when the terminal is a pad
    cluster: int = 0              # connectivity cluster id (guided.model)
    escape: bool = False          # True once fan-out moved the endpoint to the end of an escape stub


@dataclass
class ConnV2:
    idx: int
    net: str
    a: Terminal
    b: Terminal
    est: float = 0.0                         # octile estimate, mm
    role: str = "signal"                     # 'plane'|'power'|'pair'|'bus'|'critical'|'signal'
    width: float = 0.0
    neck: float = 0.0
    clearance: float = 0.0
    via: tuple[float, float] = (0.0, 0.0)    # (size, drill)
    rank: tuple = ()                         # canonical order key
    tree_target: bool = True                 # may tee onto any same-net piece


@dataclass
class Corridor:
    conn: int
    tiles: frozenset                         # {(tx, ty)}: 2-D tile coords, already dilated by 1 ring
    layers: tuple[int, ...] = ()
    layer_seq: tuple[tuple[int, int, int], ...] = ()   # (tile_i, from_layer, to_layer) via hints
    lateral: dict = field(default_factory=dict)        # (tile_edge) -> rank among crossings (planarity hint)
    overflow: float = 0.0                    # residual overflow on its path (0 = feasible by the model)


@dataclass
class Blocker:
    rule: str                    # 'clearance'|'hole'|'edge'|'keepout'|'mask'|'short'|'thermal'|'reserve'
    net: str | None              # net name of the blocking shape (None: no net)
    tag: str | None              # 'U3.4', 'via', 'track', 'edge', 'hole', 'keepout', 'zone', ...
    static: bool                 # False: v2 copper that could be ripped
    piece: int | None            # piece id when dynamic copper
    dist: float                  # actual gap, copper edge to shape (negative = overlap), mm
    need: float                  # required gap, mm
    at: tuple[float, float]      # point on the checked item closest to the blocker
    layer: int


@dataclass
class Cut:
    a: str                       # pad refs or 'edge' / 'tile(x,y)|E'
    b: str
    layer: str
    capacity_mm: float
    demand_mm: float
    nets: list[str] = field(default_factory=list)


@dataclass(order=True)
class Score:
    """Compared lexicographically, smaller is better."""
    new_errors: int = 0
    opens: int = 0
    necked_mm: float = 0.0
    vias: int = 0
    length_mm: float = 0.0
    bends: int = 0

    def key(self) -> tuple:
        return (self.new_errors, self.opens, self.necked_mm, self.vias, self.length_mm, self.bends)


@dataclass
class StageReport:
    name: str
    routed: int = 0
    failed: int = 0
    score: Score = field(default_factory=Score)
    work: int = 0
    notes: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)


class Budget:
    """Deterministic work units, same scale as v1 (time_budget_s x 200 000). No wall clock.

    Charges: 1 per A* expansion (tile or cell); 4000 per window search started; 2000 per window
    build; 1 per 8 oracle checks; 200 per pattern attempt.

    ``sub(fraction, floor)`` hands out a child budget of ``max(floor, min(fraction x total, left()))``
    units; everything charged to the child is charged to the parent as well, so an unused share
    rolls forward to whoever asks next. ``floor`` is a guaranteed minimum: the parent may go over.
    """
    __slots__ = ("total", "used", "parent")

    def __init__(self, total: int, parent: "Budget | None" = None):
        self.total = max(0, int(total))
        self.used = 0
        self.parent = parent

    def charge(self, n: int) -> None:
        n = int(n)
        if n <= 0:
            return
        b = self
        while b is not None:
            b.used += n
            b = b.parent

    def left(self) -> int:
        return max(0, self.total - self.used)

    def sub(self, fraction: float, floor: int = 0) -> "Budget":
        share = min(int(self.total * fraction), self.left())
        return Budget(max(int(floor), share), parent=self)

    def spent_out(self) -> bool:
        return self.used >= self.total

    def __repr__(self):
        return f"Budget({self.used}/{self.total})"


@dataclass
class Ctx:
    """The one context every stage works on: ``run(ctx, budget) -> StageReport``."""
    board: Any                              # guided.model.Board
    cfg: Any                                # guided.plan.Config
    x: Any = None                           # planx.V2Cfg
    oracle: Any = None                      # oracle.Oracle
    state: Any = None                       # pieces.RouteState
    conns: list = field(default_factory=list)          # list[ConnV2]
    corridors: dict = field(default_factory=dict)      # conn idx -> Corridor
    tiles: Any = None                       # tiles.Tiles | None
    history: dict = field(default_factory=dict)
    reports: list = field(default_factory=list)        # list[StageReport]
    progress: Callable | None = None
