"""The only orchestrator of router v2 (design §2, §9).

    route(pr, cfg, x, ex, *, scratch, baseline_raw, budget_units, progress=None) -> dict

runs the stage pipeline on one ``Ctx`` and leaves the judged result in ``scratch.pcb``; the session-level write
(checkpoint, all-or-nothing, dry_run, compare) stays in ``guided/session.py``::

    classify → power → fanout → global → detail → repair → polish → judge (+ pours pass)

Rules this file enforces:

* **Stage selection**: ``plan.global.stages`` (``x.stages``). A stage runs only when it is selected *and* its module
  is built (``available_stages()``); a missing ``detail`` is replaced by ``legacy.route_all`` (v1's search), a missing
  ``classify`` by v1's ``plan_connections``. Without ``judge`` nothing is committed (plan-only run).
* **Keep-best gate**: after every search stage the state score is compared with the score before it; a stage that
  made the result worse is undone. The reservation stages (power, fanout, global) are gated on their copper being
  legal by the oracle instead (they do not close connections). The judge keeps its own best round.
* **Budget** in deterministic work units (``time_budget_s × 200 000``): each stage gets ``STAGE_SHARE`` of it, the
  detail slot also gets the shares of the search stages that are not built. No wall clock in any decision.
* **Determinism**: stages run in a fixed order on canonical data; the commit list comes from ``RouteState.export``.
* **No silent loss**: every connection ends as routed, or in ``failed[]`` with a reason, or in ``skipped[]`` /
  ``removed_after_drc[]``.

A stage that raises is undone and reported in ``warnings`` and in its ``stages[]`` entry (``COPPER_V2_STRICT=1``
re-raises instead: tests and the benchmark use it); KiCad I/O errors (``ToolError``) always propagate.
"""
from __future__ import annotations

import dataclasses
import importlib
import math
import os
import sys
import time
from pathlib import Path

from ...mcp.registry import ToolError
from ..guided import geom, model, plan as planmod
from .types import STAGE_SHARE, WORK_PER_SECOND, Budget, ConnV2, Ctx, StageReport, Terminal

STAGES = ("classify", "power", "fanout", "global", "detail", "repair", "polish", "judge")
RESERVING = ("power", "fanout", "global")          # gated on legality, exempt from the score gate (design §2)
SEARCHING = ("detail", "repair", "polish")         # gated on the score
_MODULE = {"global": "globalroute"}
# where each stage sits on the 0..1 progress bar the session shows (session uses 0..0.06 and the tail itself)
_SPAN = {"classify": (0.06, 0.08), "power": (0.08, 0.10), "fanout": (0.10, 0.13), "global": (0.13, 0.18),
         "detail": (0.18, 0.60), "repair": (0.60, 0.70), "polish": (0.70, 0.76), "judge": (0.76, 0.92),
         "pours": (0.92, 0.95), "tune": (0.95, 0.96)}
JUDGE_FLOOR = 0.10           # ... and at least this share of the whole budget, whatever the search stages used
JUDGE_FLOOR_S = 2.0          # the judge's re-routes always get at least this much search (seconds of budget)
POURS_SHARE = 0.2            # pads a pour misses are cheap to connect: they always get a slice (as v1)
TUNE_SHARE = 0.05            # length tuning (v2/tune.py, judged by judge.tune_pass): its slice, a floor (design §14.6)
WIDE_MAX_CONNS = 250         # build stage 1: boards up to this size may be re-routed from scratch by the judge (as v1)
WIDE_MAX = 2                 # ... at most this often per run
POLISH_SLACK = 0.02          # polish may add this share of the routed length for clean-up edits (odd angles, stubs)
POLISH_SLACK_MIN = 1.0       # ... and at least this many mm
ALT_MAX_CONNS = 150          # second opinion (v1's search for the whole board, judged the same way): boards up to this size
ALT_MAX_WORK = 2.0           # ... and only while both runs together stay under this many budgets
LOCAL_FILL_SHARE = 0.5       # a poured net whose pour joins less than this share of its pads is routed like a signal
LOCAL_FILL_MIN_CLUSTERS = 3  # ... when the pour leaves it in at least this many pieces (minimal classify, x.local_fill)
PLANE_COST = 2.5             # plane rescue: cost multiplier of a plane layer for a connection that failed without it
RETREE = True                # connections that all end on one unreachable pad are re-aimed at each other's far ends
RETREE_LEGACY = False        # ... v1's search as well for a re-aimed pair (measured: +1 on one board, -5 on another
#                              where its extra copper cut pour pads off: off)
RETREE_SHARE = 0.15          # ... with this share of the budget (a floor)
PLANE_LADDER = True          # ... then the repair ladder for what is still open, planes open to every unpoured net
PLANE_LADDER_SHARE = 0.10    # ... with this share of the budget (a floor as well)
LADDER_FLOOR = 0.0           # the repair ladder's guaranteed share on a board without plane layers when the fallback
                             # used the search budget up. Off: measured at 0.10 on the one board it applies to
                             # (2 layers, 164 connections): 0 of 7 rips accepted, 0 repaired, one connection lost
PLANE_SHARE = 0.15           # ... and its share of the budget (a floor: it runs after the fallback used the rest)
POUR_WINDOW = True           # pours pass: pads v1's search cannot connect get the window search to the nearest pad of the net
POUR_WINDOW_REACH = 25.0     # ... when that pad is within this many mm (Manhattan)
POUR_BRIDGE = True           # ... then a bridge from any copper of the pad's side (pads, tracks, pour island) to any
#                              copper of the rest of the net (detail.route_conn(bridge=...)): stitch vias, island joins
BRIDGE_POINTS = 400          # ... points sampled per side to find where the two sides are nearest
POUR_RIP = True              # ... and last a bridge that moves routed connections out of its way (KiCad judges each)
POUR_RIP_VICTIMS = 3         # ... at most this many connections moved for one bridge
POUR_RIP_PRICE = 4.0         # ... mm a bridge may detour to spare one routed connection
POUR_RIP_SHARE = 0.2         # ... its own share of the budget (a floor: victims are re-routed with it)
POUR_RIP_ALTS = 4            # ... bridges tried per pad (a connection that cannot move is fixed for the next)
DIAG_MAX = 40                # failed[] records completed by a priced search when no blocker is named
DIAG_WORK = 80_000           # ... work units for each (not charged to the run's budget)
REVIA = True                 # before polish: connections with vias are re-laid via-frugally (polish.revia; COPPER_V2_REVIA=0)
REVIA_SHARE = 0.10           # ... with at most this share of the budget, when that much is left
VIA_FRUGAL = 3.0             # an easy board is routed with this multiple of the plan's via cost (humans keep such a
#                              board on one layer and detour instead; measured on the corpus: 0 - 0.7 x the vias)
VIA_FRUGAL_LADDER = (3.0, 8.0)  # the multiples tried in turn while each ends complete and clean; fewest vias wins
VIA_FRUGAL_FAILS = 0.03      # "easy": the frugal detail stage fails at most this share of the pad-to-pad connections
VIA_FRUGAL_FAILS_MIN = 2     # ... or this many, and the frugal run ends complete and clean; else the normal run, fresh
HALO_MM = 0.0                # fan-out halo: mm round a fanned-out part's pads where passing nets pay HALO_COST (0 = off)
HALO_COST = 3.0
HALO_MIN_ESCAPES = 8         # ... for parts with at least this many escapes
FALLBACK_FIRST = True       # legacy fallback before the repair ladder (COPPER_V2_ORDER = fallback | repair)


def _strict() -> bool:
    return os.environ.get("COPPER_V2_STRICT") == "1"


def _module(name: str):
    """The built module of a stage / helper, or None when it is missing or still a stub."""
    try:
        m = importlib.import_module(f"{__package__}.{_MODULE.get(name, name)}")
    except Exception:  # noqa: BLE001
        return None
    return None if getattr(m, "STUB", False) else m


def available_stages() -> dict[str, bool]:
    """Which stage modules are importable and not stubs (drives fallbacks)."""
    return {name: _module(name) is not None for name in STAGES}


# ── S0 fallback: v1's connections ─────────────────────────────────────────
def local_fill_nets(board, cfg) -> list[str]:
    """Poured nets whose pour is a local fill, not a plane: it joins less than ``LOCAL_FILL_SHARE`` of the net's
    pads on the unrouted board and lies only on layers the signals are routed on. Such a net needs real routing
    (a human routes it first, as tracks); left to the pours pass it is laid one stub at a time between the finished
    signals."""
    out = []
    for name in sorted(board.clusters):
        net, cl = board.nets[name], board.clusters[name]
        if not net.has_pour or len(cl) < LOCAL_FILL_MIN_CLUSTERS or cfg.net(name).skip:
            continue
        sizes = [len(c["pads"]) for c in cl.values()]
        pads = sum(sizes)
        if not pads or max(sizes) >= LOCAL_FILL_SHARE * pads:
            continue
        ok = cfg.net(name).layers_ok
        sig = set(ok) if ok else set(range(board.L))
        if any(l not in sig for l in net.pour_layers):
            continue                        # it has a plane of its own: a via per pad is the connection
        out.append(name)
    return out


def minimal_connections(board, cfg, local_fill: bool = False) -> list[ConnV2]:
    """Stage-1 classify: v1's ``plan_connections`` (signals mode) as ConnV2 records. ``ConnV2.idx`` is v1's
    ``Conn.idx`` and ``rank`` is v1's order key, so ``legacy`` and the state agree on what a connection is.
    ``local_fill``: the nets of ``local_fill_nets`` are planned like unpoured nets (an MST over their pad
    clusters, routed with the signals); what the search leaves open of them still goes to the pours pass."""
    legacy = _module("legacy")
    if legacy is not None and hasattr(legacy, "plan_connections"):
        flip = local_fill_nets(board, cfg) if local_fill else []
        for n in flip:
            board.nets[n].has_pour = False  # v1's planner decides "MST or pad-to-pour" on this flag alone
        try:
            return legacy.plan_connections(board, cfg, "signals")
        finally:
            for n in flip:
                board.nets[n].has_pour = True
    from ..guided import router as routermod
    if board.grid is None:
        board.make_grid(cfg.step)
    r = routermod.Router(board, cfg, mode="signals")
    out = []
    for c in r.plan_connections():
        nc = cfg.net(c.net)
        if nc.dp_partner:
            role = "pair"
        elif nc.keep_short:
            role = "critical"
        elif planmod._POWER.match(c.net) or planmod._GROUND.match(c.net):
            role = "power"
        else:
            role = "signal"
        a = Terminal(c.pa.x, c.pa.y, tuple(c.pa.layers), pad=c.pa.name, cluster=c.a)
        b = Terminal(c.pb.x, c.pb.y, tuple(c.pb.layers), pad=c.pb.name, cluster=c.b)
        out.append(ConnV2(idx=c.idx, net=c.net, a=a, b=b, est=c.est, role=role, width=nc.width,
                          neck=(nc.neck_widths[0] if nc.neck_widths else nc.width), clearance=nc.clearance,
                          via=(nc.via, nc.drill), rank=tuple(c.key), tree_target=True))
    return out


def skipped_nets(board, cfg) -> list[dict]:
    """Nets the plan leaves alone (same entries as v1's Router.skipped, minus 'not selected')."""
    out = []
    for name in sorted(board.clusters):
        cl = board.clusters[name]
        nc = cfg.nets.get(name)
        if nc is None or len(cl) < 2 or not nc.skip or nc.skip_reason == "not selected":
            continue
        out.append({"net": name, "connections": len(cl) - 1, "reason": nc.skip_reason or "plan: skip"})
    return out


def targeted(targets, unconnected) -> int:
    return sum(int(v) for n, v in (unconnected or {}).items() if n in targets)


# ── the pipeline ──────────────────────────────────────────────────────────
class _NotEasy(Exception):
    """The via-frugal trial gave up: the board is routed by the normal run."""


class _Run:
    """One engine run: the context, the budget and the stage loop."""

    def __init__(self, pr, cfg, x, ex, scratch, baseline_raw, budget_units, progress):
        self.pr, self.cfg, self.scratch, self.baseline_raw = pr, cfg, scratch, baseline_raw
        self.x = x
        self.ex = ex
        self.progress = progress
        self.total = Budget(int(budget_units))
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.passes: list[dict] = []
        self.failed_seen: dict = {}          # (net, from, to) -> v1-format failed dict reported by a stage
        self.judged = False
        self.frugal = None                   # via-frugal trial (engine.route): hard detail failures allowed
        self.wide = 0                        # full re-routes done by _wide_reroute
        self.bridged = 0                     # pad-to-pour connections closed by a bridge (stitch via / island join)
        self.pour_rips = 0                   # bridge proposals that moved routed connections (handed to KiCad)
        self._diagnosed: set = set()         # connections whose failed[] record got a priced-search diagnosis
        self.targets: set = set()
        self.avail = available_stages()
        self.selected = tuple(s for s in STAGES if self._has(s))
        self.ctx: Ctx | None = None

    def _has(self, stage: str) -> bool:
        st = getattr(self.x, "stages", None) or getattr(self.cfg, "stages", None) or STAGES
        if stage == "classify":
            return True
        if stage == "polish" and getattr(self.x, "polish", True) is False:
            return False
        return stage in st

    def full_classify(self) -> bool:
        """S0 as designed (roles, power widths, Steiner order, plane connections) comes in with the power stage
        (design §12, build stage 2): it is used when 'power' is selected and built, or when the plan asks for it
        (``x.classify == 'full'``). Otherwise S0 is build stage 1's: v1's connection list (``x.classify ==
        'minimal'`` forces that)."""
        want = getattr(self.x, "classify", None) or os.environ.get("COPPER_V2_CLASSIFY")
        if want in ("full", "minimal"):
            return want == "full"
        return "power" in self.selected and self.avail.get("power", False)

    def detail_engine(self) -> str:
        """'window' (S4, design §4.8) when it is built, else 'legacy' (v1's search on the v2 state, build stage 1).
        ``x.detail`` / the environment variable COPPER_V2_DETAIL = 'legacy' keeps build stage 1's search selectable
        after S4 has landed (design §1: a stage that misses its gate must not become the only path)."""
        want = getattr(self.x, "detail", None) or os.environ.get("COPPER_V2_DETAIL")
        if want == "legacy" and _module("legacy") is not None:
            return "legacy"
        if self.avail.get("detail", False):
            return "window"
        return "legacy" if _module("legacy") is not None else "none"

    def fallback_on(self) -> bool:
        """v1's search for the connections the window search leaves open (``x.legacy_fallback`` / the environment
        variable COPPER_V2_FALLBACK = '0' turns it off: diagnostics of the window search alone)."""
        if os.environ.get("COPPER_V2_FALLBACK") == "0":
            return False
        return bool(getattr(self.x, "legacy_fallback", True))

    def fallback_first(self) -> bool:
        want = os.environ.get("COPPER_V2_ORDER")
        if want in ("fallback", "repair"):
            return want == "fallback"
        return FALLBACK_FIRST

    # ── progress: a stage reports 0..1, the session bar gets its span ─────
    def _progress_for(self, stage: str):
        if not self.progress:
            return None
        a, b = _SPAN.get(stage, (0.0, 1.0))
        cb = self.progress

        def f(frac, msg):
            try:
                cb(a + (b - a) * max(0.0, min(1.0, float(frac))), msg)
            except Exception:  # noqa: BLE001
                pass
        return f

    # ── build the context ─────────────────────────────────────────────────
    def setup(self):
        from .oracle import Oracle, local_overrides_from_file
        pieces = _module("pieces")
        if pieces is None:
            raise ToolError("router v2 is not available in this build (its route state module is missing)",
                            code="V2_UNAVAILABLE", fix="use plan.global.engine='guided' (the classic router)")
        pr, cfg = self.pr, self.cfg
        board = model.Board(self.ex, model.dru_minimums(Path(pr.dir) / f"{pr.name}.kicad_dru"))
        try:
            overrides = local_overrides_from_file(pr.pcb)
        except OSError:
            overrides = None
            self.warnings.append("could not read footprint clearance overrides / mask margins from the board file: "
                                 "the exact checker runs without them (KiCad DRC still judges the result)")
        self.overrides = overrides
        oracle = Oracle(board, overrides=overrides)
        for name in sorted(cfg.nets):                      # plan clearances above the class (W0 note 8)
            if name in board.nets and cfg.nets[name].clearance > board.nets[name].clearance + 1e-9:
                oracle.set_clearance(name, cfg.nets[name].clearance)
        # the plan's forbidden areas (regions[].forbid, nets[..].avoid, strict corridors) are rules of the exact
        # checker: no stage can place copper there, whatever its own search looked at
        oracle.set_plan(cfg)
        if board.grid is None:
            board.make_grid(cfg.step)                      # v1 search (legacy, pours) and minimal classify need it
        x = self.x
        if x is None:
            px = _module("planx")
            x = self.x = px.V2Cfg() if px else _NoX()
        self.ctx = ctx = Ctx(board=board, cfg=cfg, x=x, oracle=oracle, state=None, conns=[], corridors={}, tiles=None,
                             history={}, reports=[], progress=None)
        self.constraints(board, x)          # before S0: classify orders the members of tuned groups early
        # S0: connections (the state needs them before anything else can run)
        rep = None
        cl = _module("classify") if self.full_classify() else None
        if cl is not None:
            ctx.progress = self._progress_for("classify")
            try:
                ctx.state = pieces.RouteState(oracle, ctx.conns)       # classify may look at the (empty) state
                rep = cl.run(ctx, self.total.sub(0.01))
            except ToolError:
                raise
            except Exception as e:  # noqa: BLE001
                if _strict():
                    raise
                self.warnings.append(f"v2 stage 'classify' failed ({type(e).__name__}: {e}): using v1's connection list")
                rep, ctx.conns = None, []
        if rep is None:
            lf = bool(getattr(x, "local_fill", False)) or os.environ.get("COPPER_V2_LOCAL_FILL") == "1"
            ctx.conns = minimal_connections(board, cfg, local_fill=lf)
            rep = StageReport(name="classify", notes=["connections from v1 plan_connections (minimal classify)"])
            rep.data = {"minimal": True}
            if lf:
                nets = sorted({c.net for c in ctx.conns if board.nets[c.net].has_pour})
                if nets:
                    rep.data["local_fill"] = nets
                    rep.notes.append("poured net(s) whose pour joins few of their pads are routed with the signals: "
                                     + ", ".join(nets[:8]))
        ctx.state = pieces.RouteState(oracle, ctx.conns)
        # the nets this run routes: not skipped by the plan, or given connections by classify all the same
        self.targets = {n for n, nc in cfg.nets.items() if not nc.skip} | {c.net for c in ctx.conns}
        ctx.history["targets"] = self.targets
        rep.score = ctx.state.score()
        ctx.reports.append(rep)
        self.passes.append({"pass": "classify", "connections": len(ctx.conns), "routed": 0, "seconds": 0.0, "work": 0})
        return ctx

    def constraints(self, board, x) -> None:
        """Electrical constraints (design §14): the plan's declarations plus what KiCad's rules, net classes and
        net names say, resolved once per plan object (``x.cons``; a copy of ``x`` made for another pipeline run
        carries it) and listed in ``auto.constraints``. Nothing here changes copper."""
        cmod = _module("constraints")
        if cmod is None or not hasattr(x, "cons_plan"):
            return
        try:
            if getattr(x, "cons", None) is None:
                proj = cmod.read_project(self.pr, layers=board.L)
                x.cons = cmod.resolve(board, self.cfg, x, proj)
                x.cons_env = {"stack": proj.stack, "die": proj.die, "use_height": proj.use_height,
                              "tuning": proj.tuning}
            pv = cmod.preview(x.cons)
            if pv and isinstance(getattr(x, "auto", None), dict):
                x.auto["constraints"] = pv
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            if _strict():
                raise
            x.cons = None
            self.warnings.append(f"electrical constraints could not be resolved ({type(e).__name__}: {e}): "
                                 "routed without them")

    # ── one stage with its gate ───────────────────────────────────────────
    def stage(self, name: str, fn, budget: Budget, gate: str) -> StageReport:
        """gate: 'score' (undo when the score got worse) | 'legal' (undo when new copper is illegal) | 'none'."""
        ctx, state = self.ctx, self.ctx.state
        ctx.progress = self._progress_for(name if name in _SPAN else "detail")
        snap = state.snapshot()
        score0 = state.score()
        ids0 = {p.id for p in state.pieces()} if gate == "legal" else None
        t0 = time.time()
        used0 = budget.used
        try:
            rep = fn(budget)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 — another work package's stage must not take the route down
            if _strict():
                raise
            state.restore(snap)
            rep = StageReport(name=name, notes=[f"stage failed and was undone: {type(e).__name__}: {e}"])
            self.warnings.append(f"v2 stage '{name}' failed ({type(e).__name__}: {e}); its work was undone and the "
                                 "pipeline went on without it")
        if not isinstance(rep, StageReport):
            rep = StageReport(name=name)
        rep.name = name
        score1 = state.score()
        if gate == "tidy":
            # polish: nothing but length may get worse, and length by no more than the clean-up allowance
            slack = float(getattr(self.x, "polish_slack_mm", 0.0) or 0.0)
            k0, k1 = score0.key(), score1.key()
            worse = k1[:4] > k0[:4] or (k1[:4] == k0[:4] and score1.length_mm > score0.length_mm + slack + 1e-6)
            if worse:
                state.restore(snap)
                rep.notes.append("made the result worse (score " + _fmt(score1) + " vs " + _fmt(score0) + "): undone")
                rep.data = dict(rep.data or {}, undone=True)
        elif gate == "score" and score1 > score0:
            state.restore(snap)
            rep.notes.append("made the result worse (score " + _fmt(score1) + " vs " + _fmt(score0) + "): undone")
            rep.data = dict(rep.data or {}, undone=True)
        elif gate == "legal":
            bad = None
            for p in state.pieces():
                if p.id not in ids0:
                    bad = ctx.oracle.piece_ok(p)
                    if bad:
                        break
            if bad:
                state.restore(snap)
                rep.notes.append(f"placed copper the exact checker rejects ({bad.rule} to {bad.tag or bad.net} at "
                                 f"({round(bad.at[0], 3)}, {round(bad.at[1], 3)})): the stage was undone")
                rep.data = dict(rep.data or {}, undone=True)
        rep.score = state.score()
        if not rep.work:
            rep.work = budget.used - used0
        if not (rep.data or {}).get("undone"):
            for e in (rep.data or {}).get("failed") or []:
                if isinstance(e, dict) and e.get("net"):
                    self.failed_seen[(e["net"], e.get("from"), e.get("to"))] = e
        ctx.reports.append(rep)
        n_ok = sum(1 for c in ctx.conns if state.status(c.idx) == "routed")
        self.passes.append({"pass": name, "connections": len(ctx.conns), "routed": n_ok,
                            "seconds": round(time.time() - t0, 2), "work": rep.work})
        return rep

    def plan_audit(self) -> int:
        """The plan's forbidden areas are a contract, not a cost: before KiCad sees the copper, every piece of this
        run is checked against them once more (``oracle.plan_hit``, the rule every search already obeys). A piece
        that enters one — a stage that added copper without asking the exact checker — is taken out with its whole
        connection, which is reported as failed. Returns how many pieces still offend (0 = the state is clean)."""
        ctx, state = self.ctx, self.ctx.state
        orc = ctx.oracle
        if not getattr(orc, "plan_on", False):
            return 0

        def hits():
            out = []
            for p in sorted(state.pieces(), key=lambda p: p.id):
                tag = orc.plan_hit(p.net, None if p.kind == "via" else p.layer, p.x1, p.y1, p.x2, p.y2)
                if tag:
                    out.append((p, tag))
            return out
        bad = hits()
        if not bad:
            return 0
        conns = {}
        for p, tag in bad:
            if p.conn >= 0:
                conns.setdefault(p.conn, (p, tag))
            else:
                state.remove(p.id)
        for conn in sorted(conns):
            p, tag = conns[conn]
            state.rip_conn(conn, protected=True)
            state.set_status(conn, "failed", {"reason": f"routed through {tag}: removed (the plan forbids copper of "
                                                        f"{p.net} there)", "where": (p.x1, p.y1)})
        self.warnings.append(f"{len(bad)} piece(s) of new copper entered a forbidden area of the plan and were "
                             f"removed with their connection(s) before the board was checked")
        return len(hits())

    def drop_idle_escapes(self) -> int:
        """Fan-out escape vias the search then did not use: the pad's connections left on the pad's own layer and
        the via has copper on one layer only — KiCad reports it as ``via_dangling`` and it blocks the other layer
        for nothing. Such a via is removed (its stub on the pad layer stays: other copper may tee onto it) unless
        that would open a routed connection. Vias of a net with a pour, or left for one, stay: the pour is their
        other side. Returns how many were removed."""
        ctx, state, board = self.ctx, self.ctx.state, self.ctx.board
        power = _module("power")
        n = 0
        for p in sorted(state.pieces(), key=lambda p: p.id):
            if p.kind != "via" or p.stage != "fanout":
                continue
            net = board.nets.get(p.net)
            if net is None or net.has_pour or (power is not None and hasattr(power, "left_for_pour")
                                               and power.left_for_pour(self.cfg, p.net)):
                continue
            if any(len(pad.layers) > 1 and math.hypot(pad.x - p.x1, pad.y - p.y1) <= 1.0 for pad in net.pads):
                continue                    # at a through-hole pad of its net: not an escape to judge here
            reach = p.w / 2
            layers = set()
            for s in state.pieces(net=p.net):
                if s.kind == "seg" and geom.pt_seg_dist(p.x1, p.y1, s.x1, s.y1, s.x2, s.y2) <= reach + s.w / 2:
                    layers.add(s.layer)
            for t in getattr(board, "kept_tracks", None) or ():
                if t[0] == p.net and geom.pt_seg_dist(p.x1, p.y1, t[2], t[3], t[4], t[5]) <= reach + t[6] / 2:
                    layers.add(t[1])
            if len(layers) != 1:
                continue                    # used on both sides — or touched by nothing at all (not ours to judge)
            before = (state.dangling(p.net), state.dangling(p.net, True))
            snap = state.snapshot()
            state.remove(p.id)
            if (state.dangling(p.net), state.dangling(p.net, True)) != before:
                state.restore(snap)
                continue
            n += 1
        if n:
            self.notes.append(f"{n} fan-out escape via(s) the routes did not use were removed")
        return n

    def _failed_ids(self) -> list[int]:
        st = self.ctx.state
        return [c.idx for c in sorted(self.ctx.conns, key=lambda c: (c.rank, c.idx)) if st.status(c.idx) != "routed"]

    def reroute(self, ids: list[int], budget: Budget) -> None:
        """What the judge calls for the connections it removed: S4/S5 (or the legacy search) for exactly those."""
        ctx, st = self.ctx, self.ctx.state
        ids = [i for i in ids if st.status(i) != "routed"]
        pmod = _module("pairs")
        if pmod is not None and ids and ctx.history.get("pairs"):
            # a lane of a coupled pair is never re-routed alone: its partner's lane comes out too and the two
            # are laid together again (or, when that is refused, each on its own — reported as uncoupled)
            ids = [i for i in pmod.with_partners(ctx, ids) if st.status(i) != "routed"]
        chain = []
        detail, repair, legacy = (_module(n) for n in ("detail", "repair", "legacy"))
        if detail is not None and "detail" in self.selected and self.detail_engine() == "window":
            chain.append(lambda b, only: detail.run(ctx, b, only=only))
            if repair is not None and "repair" in self.selected:
                chain.append(lambda b, only: repair.run(ctx, b, only=only))
            if legacy is not None and self.fallback_on():
                chain.append(lambda b, only: legacy.route_all(ctx, b, only=only))
        elif legacy is not None:
            chain.append(lambda b, only: legacy.route_all(ctx, b, only=only))
        saved = ctx.progress
        ctx.progress = None
        try:
            for fn in chain:
                ids = [i for i in ids if st.status(i) != "routed"]
                if not ids or budget.spent_out():
                    break
                try:
                    rep = fn(budget, list(ids))
                    for e in (getattr(rep, "data", None) or {}).get("failed") or []:
                        if isinstance(e, dict) and e.get("net"):
                            self.failed_seen[(e["net"], e.get("from"), e.get("to"))] = e
                except ToolError:
                    raise
                except Exception as e:  # noqa: BLE001
                    if _strict():
                        raise
                    self.warnings.append(f"v2 re-route after DRC failed ({type(e).__name__}: {e}); the connections "
                                         "stay removed and are reported")
            left = [i for i in ids if st.status(i) != "routed"]
            if left and legacy is not None and self.detail_engine() == "legacy":
                self._wide_reroute(legacy, left)
        finally:
            ctx.progress = saved

    def _wide_reroute(self, legacy, left: list[int]) -> None:
        """Build stage 1 only (the legacy search cannot move copper that is already in the state, and S5 is not
        there to do it): when a local re-route found no way for some of the connections the judge removed, route
        the board's connections again from scratch with the judge's regions in place — v1's remedy, here under the
        keep-best rule: the attempt stays only if the state's score is better afterwards. Small boards only, at
        most twice, and only while the run stays under 1.5 x its budget (v1's rule for repair work)."""
        ctx, st = self.ctx, self.ctx.state
        first = next((p["work"] for p in self.passes if p["pass"] == "legacy"), 0)
        est = max(int(JUDGE_FLOOR_S * WORK_PER_SECOND), int(1.5 * first))
        if len(ctx.conns) > WIDE_MAX_CONNS or self.wide >= WIDE_MAX or self.total.used + est > 1.5 * self.total.total:
            return
        self.wide += 1
        snap, score0 = st.snapshot(), st.score()
        for c in ctx.conns:
            if st.pieces(conn=c.idx):
                st.rip_conn(c.idx)
            if st.status(c.idx) != "todo":
                st.set_status(c.idx, "todo")
        try:
            rep = legacy.route_all(ctx, Budget(2 * est, parent=self.total))
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            if _strict():
                raise
            st.restore(snap)
            self.warnings.append(f"v2 re-route after DRC failed ({type(e).__name__}: {e}); undone")
            return
        if st.score() < score0:
            for e in (rep.data or {}).get("failed") or []:
                if isinstance(e, dict) and e.get("net"):
                    self.failed_seen[(e["net"], e.get("from"), e.get("to"))] = e
            self.notes.append(f"{len(left)} connection(s) KiCad rejected had no other way between the routed copper: "
                              "the connections were routed again from scratch around the protected places")
        else:
            st.restore(snap)

    # ── the stage loop ────────────────────────────────────────────────────
    def run(self):
        ctx = self.setup()
        if getattr(self, "_frugal_auto", False):
            n_sig = sum(1 for c in ctx.conns if c.b.pad or c.b.cluster or c.b.layers)
            self.frugal = max(VIA_FRUGAL_FAILS_MIN, int(VIA_FRUGAL_FAILS * n_sig))
            if not n_sig:
                # nothing but pads for the pours pass (a board that is routed already, run again after pcb_pour):
                # there is no search a dearer via could change, and every trial costs KiCad round trips
                raise _NotEasy("no pad-to-pad connection to route")
            if self.plane_layers():
                # a board with plane layers: its pads reach power through vias and its signal layers are the
                # dense ones - not the kind of board a human keeps on one layer; no trial (it costs a detail stage)
                raise _NotEasy("the board has plane layers")
        total, sel, avail = self.total, self.selected, self.avail
        state = ctx.state
        legacy = _module("legacy")
        built = {s: (s in sel and avail.get(s, False)) for s in STAGES}
        if self.detail_engine() != "window":
            built["detail"] = False          # the detail slot is the legacy search; S5 needs S4's windows
            built["repair"] = False
        searched = False
        for name in ("power", "fanout", "global"):
            if not built[name]:
                continue
            mod = _module(name)
            self.stage(name, lambda b, m=mod: m.run(ctx, b), total.sub(STAGE_SHARE[name]), "legal")
            if name == "fanout":
                self.halos()
        if "detail" in sel:
            if built["detail"]:
                mod = _module("detail")
                if self.frugal is not None:
                    rep = self.stage("detail", lambda b: mod.run(ctx, b, max_failed=self.frugal),
                                     total.sub(STAGE_SHARE["detail"]), "score")
                    hard = (rep.data or {}).get("hard_failed")
                    if hard is None:
                        raise _NotEasy("the detail stage did not report (it failed, or it is not the window search)")
                    if (rep.data or {}).get("aborted") or (rep.data or {}).get("undone") or hard > self.frugal:
                        raise _NotEasy(f"{hard} connection(s) found no way at the frugal via cost")
                else:
                    self.stage("detail", lambda b: mod.run(ctx, b), total.sub(STAGE_SHARE["detail"]), "score")
                searched = True
            elif legacy is not None:
                # the detail slot also gets the shares of the search stages that are not built (stage 1: everything)
                share = 1.0 - sum(STAGE_SHARE[s] for s in ("power", "fanout", "global", "repair", "polish") if built[s])
                self.stage("legacy", lambda b: legacy.route_all(ctx, b), total.sub(share), "score")
                searched = True
                if getattr(self.x, "window_fallback", False) or os.environ.get("COPPER_V2_WINDOW_FALLBACK") == "1":
                    # the second opinion (engine.route), mirror image of the staged pipeline: what v1's search
                    # leaves open goes to the window search (box corridors) and then to the repair ladder
                    keep = int(sum(STAGE_SHARE[s] for s in ("polish", "judge")) * total.total)
                    for name in ("detail", "repair"):
                        mod = _module(name)
                        left = self._failed_ids()
                        room = total.left() - keep
                        if mod is None or not left or room <= 0:
                            continue
                        self.stage(name, lambda b, m=mod, ids=left: m.run(ctx, b, only=ids),
                                   Budget(min(room, int(STAGE_SHARE["repair"] * total.total)), parent=total), "score")
            else:
                self.warnings.append("router v2 has no detail search in this build: nothing was routed")
        # what the window search left open: v1's search per connection (design §12, build stage 3) and the repair
        # ladder. Neither is capped at a fixed share: they get what the search budget still holds, minus what
        # polish and the judge's re-routes are promised (an unused share rolls forward anyway). Measured (interface
        # notes, "Integration — build stage 3"): the fallback first closes more per work unit than the ladder first.
        reserve = sum(STAGE_SHARE[s] for s in ("polish", "judge") if s in sel)

        def rest(cap: float = 1.0) -> Budget:
            room = max(0, total.left() - int(reserve * total.total))
            return Budget(min(room, int(cap * total.total)), parent=total)

        def fallback():
            left = self._failed_ids()
            if left and searched and built["detail"] and legacy is not None and self.fallback_on():
                b = rest()
                if b.total > 0:
                    self.stage("legacy", lambda b: legacy.route_all(ctx, b, only=left, fair=True), b, "score")

        def ladder():
            if "repair" in sel and built["repair"] and searched and self._failed_ids():
                b = rest(STAGE_SHARE["repair"])
                floor = int(LADDER_FLOOR * total.total)
                if 0 < floor and b.total < floor and not self.plane_layers():
                    # the fallback used the search budget up and no plane ladder follows (a board without plane
                    # layers): the ladder still gets a turn — a floor, the run may go over by this much
                    b = Budget(floor, parent=total)
                if b.total > 0:
                    mod = _module("repair")
                    self.stage("repair", lambda b: mod.run(ctx, b), b, "score")

        for step in ((fallback, ladder) if self.fallback_first() else (ladder, fallback)):
            step()
        if searched:
            self.plane_rescue()
            self.retree()
            self.plane_ladder()
        if "polish" in sel and built["polish"] and state.pieces():
            mod = _module("polish")
            # clean-up edits (an odd-angle or stubby pad entry replaced by a 45-degree one) may cost a little
            # length: Score has no term for them. The allowance is the plan's (x.polish_slack_mm) or
            # POLISH_SLACK of the routed length, and the stage's gate allows exactly that much
            if getattr(self.x, "polish_slack_mm", None) is None:
                try:
                    self.x.polish_slack_mm = round(max(POLISH_SLACK_MIN, POLISH_SLACK * state.score().length_mm), 3)
                    self._slack_auto = True
                except Exception:  # noqa: BLE001 — a plan object without attributes: the strict rule
                    pass
            revia_on = REVIA and os.environ.get("COPPER_V2_REVIA") != "0" and getattr(self.x, "revia", True)
            if revia_on and self.avail.get("detail", False) and hasattr(mod, "revia"):
                # only on a board the search finished (no pad-to-pad connection open), and no floor: where
                # connections are still open a routed one cannot be re-laid without moving its neighbours
                # (measured: 2 - 3 vias for 5 - 10 s on each of the four largest boards)
                vb = total.sub(REVIA_SHARE)
                if vb.total >= 200_000 and not any(not _is_open_conn(ctx, i) for i in self._failed_ids()):
                    self.stage("vias", lambda b: mod.revia(ctx, b), vb, "score")
            # (a floor: the rescue rungs before it may have used the budget up, and an unpolished board is not
            # what "out of budget" should mean)
            self.stage("polish", lambda b: mod.run(ctx, b),
                       total.sub(STAGE_SHARE["polish"], floor=int(STAGE_SHARE["polish"] * total.total)), "tidy")
            if getattr(self, "_slack_auto", False):
                self.x.polish_slack_mm = None
        self.enforce_max_length()
        # what the search pass cost: the judge's estimate for "can a re-route be finished?"
        work = sum(p["work"] for p in self.passes if p["pass"] in ("detail", "legacy", "repair"))
        ctx.history["search"] = {"work": work, "conns": sum(1 for c in ctx.conns if state.status(c.idx) == "routed")}
        # ── S7: KiCad is the judge ────────────────────────────────────────
        beat = getattr(self.x, "alt_beat", None)
        if beat is not None and sum(1 for i in self._failed_ids()
                                    if not _is_open_conn(ctx, i)) > beat:
            # a second opinion (engine.route) that already has more open signal connections than the result it
            # would have to beat: not worth KiCad's time
            self.notes.append("second opinion abandoned: more connections open than the first result")
            return self.result()
        if searched:
            self.drop_idle_escapes()
        self.plan_audit()
        if "judge" in sel:
            judge, kio = _module("judge"), _module("kio")
            if judge is None or kio is None:
                self.warnings.append("router v2 cannot commit in this build (judge / KiCad I/O missing): nothing written")
            elif state.pieces() or self._pour_work():
                ctx.progress = self._progress_for("judge")
                t0 = time.time()
                # the judge's re-routes: their own share, plus the repair stage's when that stage does not run
                # (build stage 1: nothing else re-routes what KiCad rejects). A floor: only what is used is spent
                jshare = STAGE_SHARE["judge"] + (0.0 if built["repair"] else STAGE_SHARE["repair"])
                jb = total.sub(jshare, floor=max(int(JUDGE_FLOOR_S * WORK_PER_SECOND),
                                                 int(max(jshare, JUDGE_FLOOR) * total.total)))
                rep = judge.run(ctx, jb, self.pr, self.scratch, self.baseline_raw, self.reroute,
                                connect=self.pour_connect)
                ctx.reports.append(rep)
                self.passes.append({"pass": "judge", "connections": len(ctx.conns), "routed": rep.routed,
                                    "seconds": round(time.time() - t0, 2), "work": rep.work})
                self.judged = True
                if self._pour_nets():
                    ctx.progress = self._progress_for("pours")
                    t0 = time.time()
                    pb = total.sub(POURS_SHARE, floor=int(POURS_SHARE * total.total))
                    rb = total.sub(POUR_RIP_SHARE, floor=int(POUR_RIP_SHARE * total.total))
                    rep = judge.pours_pass(ctx, self.pr, self.scratch, pb, connect=self.pour_connect,
                                           reroute=self.reroute, dogbones=built["power"], rip=self.pour_rip,
                                           rip_budget=rb)
                    ctx.reports.append(rep)
                    self.passes.append({"pass": "pours", "connections": rep.routed + rep.failed, "routed": rep.routed,
                                        "seconds": round(time.time() - t0, 2), "work": rep.work})
                self.tune(judge)
                judge.cleanup(ctx)
                if self.plan_audit():
                    # the judge's own re-routes put copper into a forbidden area and it is on the scratch board
                    # already: that board is not a result. Nothing is committed (the caller then writes nothing)
                    self.judged = False
                    self.plan_violated = True
        return self.result()

    # ── plan max_length: a longer net is reported as failed, not written ──
    def enforce_max_length(self) -> int:
        """``nets[..].max_length`` is the plan's contract ("a longer result is reported as failed, not written"):
        a net that came out longer — measured as KiCad measures it — loses the connection with the largest
        detour over its estimate until it fits, and that connection is reported as failed with the excess in mm.
        A KiCad ``length (max ..)`` rule is NOT enforced this way: its net stays routed and the constraints
        block reports the excess (an open connection is worse than a long one unless the plan asked for it).
        Returns how many connections were taken out."""
        cmod = _module("constraints")
        cons = getattr(self.x, "cons", None)
        if cmod is None or cons is None or getattr(cons, "mode", None) not in ("auto", "plan"):
            return 0
        ctx, st = self.ctx, self.ctx.state
        cut = 0
        for net in sorted(n for n, nc in self.cfg.nets.items() if getattr(nc, "max_length", None) is not None):
            mx = float(self.cfg.nets[net].max_length)
            for _ in range(64):
                L = cmod.net_length(ctx, net).total
                if L <= mx + 1e-6:
                    break
                cand = []
                for c in ctx.conns:
                    if c.net != net or st.status(c.idx) != "routed":
                        continue
                    ps = [p for p in st.pieces(conn=c.idx) if not p.protected]
                    if ps:
                        cand.append((-(sum(p.length for p in ps) - float(c.est or 0.0)), c.idx, c))
                if not cand:
                    break
                c = sorted(cand, key=lambda e: e[:2])[0][2]
                st.rip_conn(c.idx)
                over = round(L - mx, 3)
                st.set_status(c.idx, "failed", {
                    "reason": f"the shortest route found makes the net {round(L, 3)} mm long, {over} mm over "
                              f"max_length={mx:g} (not written)", "code": "max_length", "where": [c.a.x, c.a.y],
                    "blocked_by": [], "blockers": [], "static_share": 1.0, "tried": [], "probe": {"enclosed": "none"},
                    "frontier": [], "width": c.width, "over_mm": over})
                self.failed_seen[(c.net, c.a.pad, c.b.pad)] = {
                    "conn": c.idx, "net": c.net, "from": c.a.pad, "to": c.b.pad, "code": "max_length",
                    "reason": f"the shortest route found makes the net {round(L, 3)} mm long, {over} mm over "
                              f"max_length={mx:g} (not written)", "where": [c.a.x, c.a.y], "blocked_by": [],
                    "static_share": 1.0, "tried": [], "probe": {"enclosed": "none"}}
                cut += 1
        if cut:
            self.notes.append(f"{cut} connection(s) taken out again: their net was longer than the plan's max_length "
                              "(reported as failed with the excess)")
        return cut

    # ── length tuning: the last judged pass ───────────────────────────────
    def tune(self, judge) -> None:
        """Meanders for the short members of the tuned length groups (design §14.6 / §14.7), after the judge and
        the pours pass so that the board KiCad accepted is the fixed point to fall back to. Runs only when the
        constraints name a group that may get copper: a board without one is not touched and KiCad is not asked
        again."""
        tmod = _module("tune")
        cons = getattr(self.x, "cons", None)
        if tmod is None or cons is None or not hasattr(judge, "tune_pass"):
            return
        if not (cons.tuned() or (hasattr(cons, "skew_pairs") and cons.skew_pairs())):
            return
        ctx, total = self.ctx, self.total
        ctx.progress = self._progress_for("tune")
        t0 = time.time()
        tb = total.sub(TUNE_SHARE, floor=int(TUNE_SHARE * total.total))
        try:
            rep = judge.tune_pass(ctx, self.pr, self.scratch, tb, tune=tmod)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 — the judged board from before the pass is still the result
            if _strict():
                raise
            self.warnings.append(f"length tuning failed ({type(e).__name__}: {e}); the board is as routed, the "
                                 "constraints block says what is missing")
            self.tune_failed = True
            return
        ctx.reports.append(rep)
        self.passes.append({"pass": "tune", "connections": rep.routed + rep.failed, "routed": rep.routed,
                            "seconds": round(time.time() - t0, 2), "work": rep.work})

    # ── through traffic stays out of the escape area of a fanned-out part ─
    def halos(self) -> None:
        """A cost region round every part the fan-out stage gave ``HALO_MIN_ESCAPES`` or more escapes: the part's
        pad box grown by ``HALO_MM``, at ``HALO_COST`` x for every net that has no pad on the part. The escapes
        end there and each still needs its way out; a net that only passes by pays for using that room (it is
        what a human keeps clear round a fine-pitch part). Global routing, the window search and v1's search all
        read plan regions. The regions are named 'fanout-halo' and listed in ``auto['halos']``."""
        mm, cost = _halo()
        if mm <= 0 or cost <= 1.0:
            return
        ctx = self.ctx
        fan = _module("fanout")
        if fan is None or not hasattr(fan, "registry"):
            return
        from ..guided import plan as planmod
        names = {n.id: name for name, n in ctx.board.nets.items()}
        count = {}
        for rec in fan.registry(ctx)["by_pad"].values():
            count[rec.get("ref")] = count.get(rec.get("ref"), 0) + 1
        made = []
        for ref in sorted(r for r, n in count.items() if r and n >= HALO_MIN_ESCAPES):
            pads = [p for p in ctx.board.pads if p.ref == ref]
            if not pads:
                continue
            x0, y0 = min(p.x for p in pads) - mm, min(p.y for p in pads) - mm
            x1, y1 = max(p.x for p in pads) + mm, max(p.y for p in pads) + mm
            nets = {names[p.net] for p in pads if p.net in names}
            r = planmod.Region()
            r.idx = 2000 + len(self.cfg.regions)
            r.rect, r.forbid, r.cost, r.layers = (x0, y0, x1, y1), False, cost, None
            r.nets, r.exc, r.name = None, nets, "fanout-halo"
            self.cfg.regions.append(r)
            made.append({"ref": ref, "rect": [round(v, 3) for v in (x0, y0, x1, y1)], "cost": cost})
        if made:
            auto = getattr(self.x, "auto", None)
            if isinstance(auto, dict):
                auto["halos"] = made
            self.notes.append(f"{len(made)} fanned-out part(s) keep a {mm:g} mm halo at {cost:g} x cost for nets "
                              "that only pass by: " + ", ".join(m["ref"] for m in made[:8]))

    # ── last rung of the search: across a plane layer ────────────────────
    def plane_layers(self) -> list[int]:
        """Inner layers a pour covers (v1: >= 40 % of the board) that the plan's ``layers`` leave out."""
        b = self.ctx.board
        try:
            planes = planmod.plane_layers(b)
        except Exception:  # noqa: BLE001
            return []
        return [l for l in planes if 0 < l < b.L - 1]

    def plane_rescue(self) -> None:
        """Connections that found no way on the signal layers get one more search in which the plane layers are
        open at ``PLANE_COST`` (a human does the same on a 4-layer board: the planes are crossed where the outer
        layers are full, and nowhere else). Only for connections between copper terminals of nets without a pour;
        the corridor is the widened one (the global stage planned without these layers). Whether a plane survives
        the cut is KiCad's verdict after the refill: the judge sees an island a track cut off as open connections
        and as starved thermals, and removes the copper it blames. ``plan.global.plane_rescue = false`` turns the
        rung off; COPPER_V2_PLANES=0 too."""
        ctx, st = self.ctx, self.ctx.state
        detail = _module("detail")
        if detail is None or getattr(self.x, "plane_rescue", True) is False or os.environ.get("COPPER_V2_PLANES") == "0":
            return
        planes = self.plane_layers()
        ids = [i for i in self._failed_ids() if not _is_open_conn(ctx, i)]
        ids = [i for i in ids if not ctx.board.nets[st.conn(i).net].has_pour]
        if not planes or not ids:
            return
        total = self.total
        b = Budget(max(int(PLANE_SHARE * total.total), 0), parent=total)      # a floor: the run may go over

        def run(budget):
            rep = StageReport(name="planes")
            got = []
            for i in ids:
                if budget.spent_out():
                    break
                conn = st.conn(i)
                nc = self.cfg.net(conn.net)
                ok0, lc0 = nc.layers_ok, nc.layer_cost
                extra = [l for l in planes if l not in (ok0 or ())]
                if not extra:
                    continue
                fail0 = st.fail(i) if hasattr(st, "fail") else None
                nc.layers_ok = tuple(sorted(set(ok0 or range(ctx.board.L)) | set(extra)))
                nc.layer_cost = dict(lc0 or {})
                for l in extra:
                    nc.layer_cost[l] = max(nc.layer_cost.get(l, 1.0), PLANE_COST)
                try:
                    r = detail.route_conn(ctx, conn, budget, widen=2, extra_vias=2)
                finally:
                    nc.layers_ok, nc.layer_cost = ok0, lc0
                if r.get("ok"):
                    got.append(i)
                    rep.routed += 1
                else:
                    rep.failed += 1
                    if st.status(i) != "failed" or fail0 is None:
                        st.set_status(i, "failed", fail0 or r.get("fail"))
            if got:
                names = sorted({st.conn(i).net for i in got})
                rep.notes.append(f"{len(got)} connection(s) had no way on the signal layers and cross a plane layer "
                                 f"({', '.join(ctx.board.layers[l] for l in planes)}): {', '.join(names[:8])}"
                                 + (" ..." if len(names) > 8 else ""))
            rep.data = {"planes": got}
            return rep
        self.stage("planes", run, b, "score")

    def plane_ladder(self) -> None:
        """What is still open after the plane rescue is blocked by routed copper more often than by pads: the
        ladder once more, with the planes open to every unpoured net — a victim of a rip that had no other way
        on the signal layers has one across a plane, which is what makes a rip pay (interface notes, W7: "the
        victim cannot be re-routed"). Boards with plane layers only; COPPER_V2_PLANE_LADDER=0 turns it off."""
        ctx, st = self.ctx, self.ctx.state
        total = self.total
        repair = _module("repair")
        if getattr(self.x, "plane_rescue", True) is False or os.environ.get("COPPER_V2_PLANES") == "0":
            return
        planes = self.plane_layers()
        left = [i for i in self._failed_ids() if not _is_open_conn(ctx, i)
                and not ctx.board.nets[st.conn(i).net].has_pour]
        if repair is None or not left or not planes or "repair" not in self.selected or not PLANE_LADDER \
                or os.environ.get("COPPER_V2_PLANE_LADDER") == "0":
            return
        saved = []
        for name in sorted(self.cfg.nets):
            nc = self.cfg.nets[name]
            net = ctx.board.nets.get(name)
            if net is None or net.has_pour:
                continue
            extra = [l for l in planes if l not in (nc.layers_ok or ())]
            if not extra:
                continue
            saved.append((nc, nc.layers_ok, nc.layer_cost))
            nc.layers_ok = tuple(sorted(set(nc.layers_ok or range(ctx.board.L)) | set(extra)))
            nc.layer_cost = dict(nc.layer_cost or {})
            for l in extra:
                nc.layer_cost[l] = max(nc.layer_cost.get(l, 1.0), PLANE_COST)
        try:
            b2 = Budget(int(PLANE_LADDER_SHARE * total.total), parent=total)
            self.stage("planes-ladder", lambda bb: repair.run(ctx, bb, only=left), b2, "score")
        finally:
            for nc, ok0, lc0 in saved:
                nc.layers_ok, nc.layer_cost = ok0, lc0

    # ── a net whose tree hangs on a pad nobody can reach ──────────────────
    def retree(self) -> None:
        """S0 fixes which pad pairs a net's connections join. When a pad cannot be reached (boxed in at a fine-pitch
        row), every connection that ends on it fails — although the net's other pads could be joined to each
        other, leaving one open connection instead of several (KiCad counts groups, not pairs). For each pad
        that ends two or more failed connections of a net: one of them stays as it is (it stands for the pad),
        the others are re-aimed at one another's far ends and searched again (window search, box corridor,
        planes open where the board has them). A re-aimed connection that still finds no way gets its pads back.
        COPPER_V2_RETREE=0 turns the step off."""
        ctx, st = self.ctx, self.ctx.state
        detail, legacy = _module("detail"), _module("legacy")
        if detail is None or not RETREE or os.environ.get("COPPER_V2_RETREE") == "0":
            return
        groups: dict = {}
        for c in sorted(ctx.conns, key=lambda c: (c.rank, c.idx)):
            if st.status(c.idx) == "routed" or _is_open_conn(ctx, c.idx) or not c.a.pad or not c.b.pad:
                continue
            groups.setdefault(c.net, []).append(c)
        jobs = []
        for net in sorted(groups):
            cs = groups[net]
            if len(cs) < 2:
                continue
            count: dict = {}
            for c in cs:
                for t in (c.a, c.b):
                    count[t.pad] = count.get(t.pad, 0) + 1
            taken = set()
            for hub in sorted((p for p, n in count.items() if n >= 2), key=lambda p: (-count[p], p)):
                grp = [c for c in cs if c.idx not in taken and hub in (c.a.pad, c.b.pad)]
                if len(grp) < 2:
                    continue
                grp.sort(key=lambda c: (c.est, c.idx))          # the shortest one keeps the pad
                taken.update(c.idx for c in grp)
                jobs.append((hub, grp))
        if not jobs:
            return
        planes = self.plane_layers() if (getattr(self.x, "plane_rescue", True) is not False
                                         and os.environ.get("COPPER_V2_PLANES") != "0") else []
        total = self.total

        def run(budget):
            rep = StageReport(name="retree")
            done = []
            for hub, grp in jobs:
                def far(c):
                    return c.b if c.a.pad == hub else c.a
                tree = [far(grp[0])]
                for c in grp[1:]:
                    if budget.spent_out():
                        break
                    t = far(c)
                    cand = [u for u in tree if u.pad != t.pad]
                    if not cand:
                        continue
                    u = min(cand, key=lambda u: (abs(u.x - t.x) + abs(u.y - t.y), u.pad))
                    a0, b0, est0 = c.a, c.b, c.est
                    cor0 = ctx.corridors.pop(c.idx, None) if ctx.corridors else None
                    fail0 = st.fail(c.idx) if hasattr(st, "fail") else None
                    dx, dy = abs(u.x - t.x), abs(u.y - t.y)
                    c.a, c.b, c.est = t, u, max(dx, dy) + 0.41421356 * min(dx, dy)
                    nc = self.cfg.net(c.net)
                    ok0, lc0 = nc.layers_ok, nc.layer_cost
                    extra = [] if ctx.board.nets[c.net].has_pour else [l for l in planes if l not in (ok0 or ())]
                    if extra:
                        nc.layers_ok = tuple(sorted(set(ok0 or range(ctx.board.L)) | set(extra)))
                        nc.layer_cost = dict(lc0 or {})
                        for l in extra:
                            nc.layer_cost[l] = max(nc.layer_cost.get(l, 1.0), PLANE_COST)
                    try:
                        r = detail.route_conn(ctx, c, budget, extra_vias=2 if extra else 0)
                    finally:
                        nc.layers_ok, nc.layer_cost = ok0, lc0
                    if RETREE_LEGACY and not r.get("ok") and legacy is not None and self.fallback_on() \
                            and not budget.spent_out():
                        # v1's search (board-wide grid, its own rip-up) for the re-aimed pair, capped like the
                        # fair fallback
                        st.set_status(c.idx, "todo")
                        try:
                            legacy.route_all(ctx, budget, only=[c.idx], fair=True)
                        except ToolError:
                            raise
                        except Exception:  # noqa: BLE001
                            if _strict():
                                raise
                        r = {"ok": st.status(c.idx) == "routed", "fail": st.fail(c.idx) if hasattr(st, "fail") else None}
                    if r.get("ok"):
                        rep.routed += 1
                        tree.append(t)
                        done.append((c.net, t.pad, u.pad, hub))
                    else:
                        rep.failed += 1
                        c.a, c.b, c.est = a0, b0, est0
                        if cor0 is not None:
                            ctx.corridors[c.idx] = cor0
                        st.set_status(c.idx, "failed", fail0 or r.get("fail"))
            if done:
                pads = sorted({h for _n, _a, _b, h in done})
                rep.notes.append(f"{len(done)} connection(s) re-aimed past a pad that could not be reached "
                                 f"({', '.join(pads[:8])}{' ...' if len(pads) > 8 else ''}): the rest of each net is "
                                 "joined, the pad itself stays open and is reported")
            rep.data = {"retree": [list(d) for d in done]}
            return rep
        self.stage("retree", run, Budget(int(RETREE_SHARE * total.total), parent=total), "score")

    def pour_connect(self, b2, budget: Budget, pads=None) -> tuple[list[int], list[dict]]:
        """The pours search of build stage 1: v1's pours mode (through ``legacy``, so with connection-level,
        oracle-exact verify) on the refilled board ``b2``, whose static copper already holds this run's tracks.
        The stubs it finds are added to the run's state as ``conn == -1`` pieces. -> (piece ids, failed dicts)"""
        from .oracle import Oracle
        legacy, pieces = _module("legacy"), _module("pieces")
        if legacy is None or not hasattr(legacy, "plan_connections"):
            return [], []
        ctx, cfg = self.ctx, self.cfg
        conns2 = [c for c in legacy.plan_connections(b2, cfg, "pours") if c.net in self.targets]
        if pads is not None:                # only the clusters of these pads (the judge: pads on a cut-off pour island)
            conns2 = [c for c in conns2
                      if any(p.name in pads for p in b2.clusters[c.net].get(c.a.cluster, {}).get("pads", ()))]
        if not conns2:
            return [], []
        o2 = self._pour_oracle(b2)
        ctx2 = Ctx(board=b2, cfg=cfg, x=ctx.x, oracle=o2, state=None, conns=conns2, corridors={}, tiles=None,
                   history={}, reports=[], progress=None)
        ctx2.state = pieces.RouteState(o2, conns2)
        rep = legacy.route_all(ctx2, budget)
        added, failed = [], list((rep.data or {}).get("failed") or [])
        # what v1's search (a board-wide grid) cannot reach gets the window search: exact at pad pitch, so a
        # pad next to another pad of its net, or one whose only way out is a neck between two tracks, is
        # found. The window search needs a copper target: the nearest pad of another cluster of the net
        # (joining any two clusters closes one open connection)
        detail = _module("detail")
        if detail is not None and POUR_WINDOW and "detail" in self.selected:
            got = set()

            def accept(c, own):
                got.add((c.net, c.a.pad))
                st2 = ctx2.state
                on_plane = [p for p in st2.pieces(conn=c.idx) if p.kind == "seg" and p.layer in own]
                if on_plane:
                    tok = st2.begin()
                    for p in on_plane:
                        st2.remove(p.id)
                    vias = [p for p in st2.pieces(conn=c.idx) if p.kind == "via"]
                    # ... and a via that only led back up from the plane goes with them
                    ends = [(p.x1, p.y1) for p in st2.pieces(conn=c.idx) if p.kind == "seg"] + \
                           [(p.x2, p.y2) for p in st2.pieces(conn=c.idx) if p.kind == "seg"]
                    for v in vias:
                        if not any(abs(v.x1 - x) < 1e-6 and abs(v.y1 - y) < 1e-6 for x, y in ends) and \
                                not (abs(v.x1 - c.a.x) < 1e-6 and abs(v.y1 - c.a.y) < 1e-6):
                            st2.remove(v.id)
                    if st2.connected(c.idx):
                        st2.commit(tok)
                    else:
                        st2.rollback(tok)
            for c in conns2:
                if ctx2.state.status(c.idx) == "routed" or budget.spent_out():
                    continue
                best = None
                for cid, cl in sorted(b2.clusters.get(c.net, {}).items()):
                    if cid == c.a.cluster:
                        continue
                    for p in cl["pads"]:
                        d = abs(p.x - c.a.x) + abs(p.y - c.a.y)
                        if p.layers and (best is None or (d, p.name) < best[:2]):
                            best = (d, p.name, p, cid)
                if best is None or best[0] > POUR_WINDOW_REACH:
                    continue
                p = best[2]
                b0, est0 = c.b, c.est
                c.b = Terminal(p.x, p.y, tuple(p.layers), pad=p.name, cluster=best[3])
                c.est = best[0]
                # the net's own plane (a pour layer signals do not use) is open to this search: a stub and a via
                # into the plane is the shortest way for a pad that has a plane under it. What the search lays
                # ON the plane to reach the other pad is taken out again when the via alone holds the connection
                nc = cfg.net(c.net)
                ok0 = nc.layers_ok
                own = [l for l in b2.nets[c.net].pour_layers if l not in (ok0 or ()) and 0 < l < b2.L - 1]
                if own:
                    nc.layers_ok = tuple(sorted(set(ok0 or range(b2.L)) | set(own)))
                try:
                    r = detail.route_conn(ctx2, c, budget, widen=1, extra_vias=1 if own else 0)
                except ToolError:
                    raise
                except Exception:  # noqa: BLE001 — the second chance must not take the pours pass down
                    if _strict():
                        raise
                    r = {"ok": False}
                finally:
                    nc.layers_ok = ok0
                if r.get("ok"):
                    accept(c, own)
                else:
                    c.b, c.est = b0, est0
            # ... and what is still open gets a bridge: from ANY copper of the pad's side (its pads, tracks, the
            # inside of its pour island) to ANY copper of the rest of the net — a stitch via where two islands
            # of the net overlap on different layers, a short track between two islands, a tee onto a track
            for c in conns2:
                if not POUR_BRIDGE or ctx2.state.status(c.idx) == "routed" or budget.spent_out():
                    continue
                spec = _bridge_spec(b2, ctx2.state, c)
                if spec is None:
                    continue
                nc = cfg.net(c.net)
                ok0 = nc.layers_ok
                own = [l for l in b2.nets[c.net].pour_layers if l not in (ok0 or ()) and 0 < l < b2.L - 1]
                if own:
                    nc.layers_ok = tuple(sorted(set(ok0 or range(b2.L)) | set(own)))
                try:
                    r = detail.route_conn(ctx2, c, budget, extra_vias=2, bridge=spec)
                    if not r.get("ok") and not budget.spent_out():      # ... once more on a lattice twice as fine
                        r2 = detail.route_conn(ctx2, c, budget, extra_vias=2, bridge=spec, pitch_scale=0.5)
                        if r2.get("ok"):
                            r = r2
                except ToolError:
                    raise
                except Exception:  # noqa: BLE001 — the second chance must not take the pours pass down
                    if _strict():
                        raise
                    r = {"ok": False}
                finally:
                    nc.layers_ok = ok0
                if r.get("ok"):
                    self.bridged += 1
                    accept(c, own)
                else:
                    _bridge_diag(ctx, failed, c, spec, r.get("fail"))
            if got:
                failed = [e for e in failed if (e.get("net"), e.get("from")) not in got]
        o = ctx.oracle
        for c in conns2:
            if ctx2.state.status(c.idx) != "routed":
                continue
            ps = ctx2.state.pieces(conn=c.idx)
            clr = cfg.nets[c.net].clearance if c.net in cfg.nets else None
            bad = None
            for p in ps:
                bad = (o.via_ok(p.x1, p.y1, p.w, p.drill, p.net, clearance=clr) if p.kind == "via"
                       else o.seg_ok(p.layer, p.x1, p.y1, p.x2, p.y2, p.w, p.net, clearance=clr))
                if bad:
                    break
            if bad:     # the fresh model and the run's own disagree (should not happen): never write it
                failed.append({"net": c.net, "from": c.a.pad or "point", "to": "pour", "_a": (c.a.x, c.a.y), "_b": None,
                               "where": [round(bad.at[0], 2), round(bad.at[1], 2)],
                               "reason": f"pad-to-pour stub rejected by the exact check ({bad.rule} to "
                                         f"{bad.tag or bad.net or 'copper'}) — dropped rather than written"})
                continue
            for p in ps:
                n = ctx.state.add(net=p.net, conn=-1, kind=p.kind, layer=p.layer, x1=p.x1, y1=p.y1, x2=p.x2, y2=p.y2,
                                  w=p.w, drill=p.drill, stage="legacy")
                added.append(n.id)
        return added, failed

    def _pour_oracle(self, b2):
        """The exact checker for a refilled board ``b2``, with the plan's clearances and what the judge closed for
        good (hardened spoke corridors)."""
        from .oracle import Oracle
        ctx, cfg = self.ctx, self.cfg
        o2 = Oracle(b2, overrides=self.overrides)
        for name in sorted(cfg.nets):
            if name in b2.nets and cfg.nets[name].clearance > b2.nets[name].clearance + 1e-9:
                o2.set_clearance(name, cfg.nets[name].clearance)
        o2.set_plan(cfg)
        seen = set()
        for lay in range(ctx.board.L):
            for sh in ctx.oracle.near(lay, ctx.board.bbox, 0.0):
                if getattr(sh, "cls", None) == "soft" and id(sh) not in seen and isinstance(sh.tag, tuple):
                    seen.add(id(sh))
                    owner = ctx.board.net_by_id.get(sh.net)
                    if owner is not None and owner.name in b2.nets:
                        for l2 in sh.layers:
                            o2.harden(o2.add_soft(sh.tag[0], owner.name, l2, sh))
        return o2

    def pour_rip(self, b2, ex2: dict, budget: Budget):
        """Generator for the judge's last pours step (``judge._rip_phase``): bridges that need routed connections
        moved. The refilled board is modelled a second time with this run's routed connections of unpoured nets
        as movable copper (everything else static, the fill islands as KiCad left them); for each pour pad still
        cut off the bridge search runs "soft" there and names the connections its cheapest bridge crosses. Those
        are ripped in the run's state, the bridge is laid, the connections are re-routed round it — and the
        proposal is yielded for KiCad's verdict ({added, victims, net, pad}). A proposal that cannot be completed
        (a victim finds no other way, the bridge fails the exact check) is undone here and never yielded; the
        judge undoes a yielded one it does not keep."""
        from ..guided import model
        from pathlib import Path
        legacy, pieces, detail = _module("legacy"), _module("pieces"), _module("detail")
        if not POUR_RIP or legacy is None or detail is None or "detail" not in self.selected \
                or not hasattr(legacy, "plan_connections"):
            return
        ctx, cfg, st = self.ctx, self.cfg, self.ctx.state
        layer_of = {n: i for i, n in enumerate(b2.layers)}

        def lay(v):
            return v if isinstance(v, int) else layer_of.get(v, -1)

        def k4(v):
            return round(v, 4)
        movable = {}
        # ... and the stubs earlier pours rounds laid (a group of joined pieces = one victim; it is not re-routed
        # here: the judge's follow-up pours round connects that pad again, round the bridge)
        from . import judge as judgemod
        stubs = set((ctx.history.get("judge") or {}).get("pour_pieces") or ())
        group_of, groups = {}, {}
        by_id = {pc.id: pc for pc in st.pieces()}
        for pid in sorted(stubs):
            pc = by_id.get(pid)
            if pc is None or pc.conn >= 0 or pc.protected or pid in group_of:
                continue
            grp = [g for g in judgemod._group(st, pc, by_id) if g.conn < 0 and not g.protected]
            gid = -(1000 + min(g.id for g in grp))
            groups[gid] = sorted(g.id for g in grp)
            for g in grp:
                group_of[g.id] = gid
        for pc in st.pieces():
            net = ctx.board.nets.get(pc.net)
            if pc.id in group_of:
                pass
            elif pc.conn < 0 or pc.protected or net is None or net.has_pour:
                continue
            if pc.kind == "via":
                movable[("v", pc.net, k4(pc.x1), k4(pc.y1))] = pc
            else:
                movable[("s", pc.net, pc.layer, k4(pc.x1), k4(pc.y1), k4(pc.x2), k4(pc.y2))] = pc
                movable[("s", pc.net, pc.layer, k4(pc.x2), k4(pc.y2), k4(pc.x1), k4(pc.y1))] = pc
        _dbg("pour_rip movable", len(movable))
        if not movable:
            return
        ex3 = dict(ex2)
        taken = {}
        tracks = []
        for t in ex2.get("tracks") or []:
            pc = movable.get(("s", t[0], lay(t[1]), k4(t[2]), k4(t[3]), k4(t[4]), k4(t[5])))
            if pc is None:
                tracks.append(t)
            else:
                taken[pc.id] = pc
        vias = []
        for v in ex2.get("vias") or []:
            pc = movable.get(("v", v[0], k4(v[1]), k4(v[2])))
            if pc is None:
                vias.append(v)
            else:
                taken[pc.id] = pc
        _dbg("pour_rip taken", len(taken), "of ex tracks", len(ex2.get("tracks") or []))
        if not taken:
            return
        ex3["tracks"], ex3["vias"] = tracks, vias
        dru = model.dru_minimums(Path(self.pr.dir) / f"{self.pr.name}.kicad_dru") if getattr(self.pr, "dir", None) else {}
        b3 = model.Board(ex3, dru)
        conns3 = [c for c in legacy.plan_connections(b3, cfg, "pours") if c.net in self.targets]
        if not conns3:
            return
        o3 = self._pour_oracle(b3)
        ctx3 = Ctx(board=b3, cfg=cfg, x=ctx.x, oracle=o3, state=None, conns=conns3, corridors={}, tiles=None,
                   history={}, reports=[], progress=None)
        st3 = ctx3.state = pieces.RouteState(o3, conns3)
        owner = {}                      # piece id in the second model -> connection idx in the run's state
        for pid in sorted(taken):
            pc = taken[pid]
            n = st3.add(net=pc.net, conn=-1, kind=pc.kind, layer=pc.layer, x1=pc.x1, y1=pc.y1, x2=pc.x2, y2=pc.y2,
                        w=pc.w, drill=pc.drill, stage="legacy")
            owner[n.id] = group_of.get(pc.id, pc.conn)

        o = ctx.oracle
        by_idx = {cn.idx: cn for cn in ctx.conns}
        for c in conns3:
            if st3.connected(c.idx):
                continue
            spec = _bridge_spec(b3, st3, c)
            if spec is None:
                continue
            nc = cfg.net(c.net)
            own = [l for l in b3.nets[c.net].pour_layers if l not in (nc.layers_ok or ()) and 0 < l < b3.L - 1]
            fixed = set()               # connections that could not be moved / whose move KiCad refused: not again
            for _alt in range(POUR_RIP_ALTS):
                if budget.spent_out():
                    return

                def price(pc):
                    k = owner.get(pc.id)
                    return None if (k is None or k in fixed) else POUR_RIP_PRICE
                ok0 = nc.layers_ok
                if own:
                    nc.layers_ok = tuple(sorted(set(ok0 or range(b3.L)) | set(own)))
                tok3 = st3.begin()
                try:
                    victims = []
                    try:
                        r = detail.route_conn(ctx3, c, budget, extra_vias=2, bridge=spec, soft_cost=price)
                        _dbg("pour_rip", c.net, c.a.pad, "soft ->", r.get("ok"), r.get("plan"), r.get("crossed"),
                             (r.get("fail") or {}).get("code"), "left", budget.left())
                        if r.get("plan"):
                            crossed = r.get("crossed_pieces") or {}
                            if any(pid not in owner for pid in crossed):
                                break
                            victims = sorted({owner[pid] for pid in crossed})
                            if not victims:
                                break
                            if len(victims) > POUR_RIP_VICTIMS:
                                # too many in the way of the cheapest bridge: the costliest to cross first is fixed
                                fixed.add(victims[-1])
                                continue
                            for pid in sorted(owner):
                                if owner[pid] in victims and st3.piece(pid) is not None:
                                    st3.remove(pid)
                            r = detail.route_conn(ctx3, c, budget, extra_vias=2, bridge=spec)
                            _dbg("pour_rip   hard after rip of", victims, "->", r.get("ok"),
                                 (r.get("fail") or {}).get("code"))
                        if not r.get("ok"):
                            break
                    except ToolError:
                        raise
                    except Exception:  # noqa: BLE001 — a proposal that cannot be built is no proposal
                        if _strict():
                            raise
                        break
                    finally:
                        nc.layers_ok = ok0
                    new = [p for p in st3.pieces(conn=c.idx) if not (p.kind == "seg" and p.layer in own)]
                    if not new:
                        break
                    snap = st.snapshot()
                    routed0 = [cn.idx for cn in ctx.conns if st.status(cn.idx) == "routed"]
                    gone = [v for v in victims if v < 0]          # stub groups: removed, re-connected by the judge
                    victims = [v for v in victims if v >= 0]
                    for v in victims:
                        st.rip_conn(v)
                    for gid in gone:
                        for pid in groups.get(gid, ()):
                            if st.piece(pid) is not None:
                                st.remove(pid)
                    clr = cfg.nets[c.net].clearance if c.net in cfg.nets else None
                    added = []
                    bad = None
                    for p in new:
                        bad = (o.via_ok(p.x1, p.y1, p.w, p.drill, p.net, clearance=clr) if p.kind == "via"
                               else o.seg_ok(p.layer, p.x1, p.y1, p.x2, p.y2, p.w, p.net, clearance=clr))
                        if bad:
                            break
                        n = st.add(net=p.net, conn=-1, kind=p.kind, layer=p.layer, x1=p.x1, y1=p.y1, x2=p.x2, y2=p.y2,
                                   w=p.w, drill=p.drill, stage="legacy")
                        added.append(n.id)
                    if bad:
                        st.restore(snap)
                        break
                    stuck = []
                    if victims:
                        # the exact window search first (wide, two extra vias), then whatever the run re-routes with
                        for v in victims:
                            cn = by_idx.get(v)
                            if cn is None or budget.spent_out():
                                continue
                            try:
                                detail.route_conn(ctx, cn, budget, widen=2, extra_vias=2)
                            except ToolError:
                                raise
                            except Exception:  # noqa: BLE001
                                if _strict():
                                    raise
                        self.reroute([v for v in victims if st.status(v) != "routed"], budget)
                        stuck = [v for v in victims if st.status(v) != "routed"]
                        vnets = sorted({by_idx[v].net for v in victims if v in by_idx})
                        if not stuck and any(st.dangling(n, True) for n in vnets):
                            stuck = list(victims)
                        if not stuck and any(st.status(i) != "routed" for i in routed0):
                            stuck = list(victims)   # the re-route pushed another connection out and lost it
                    _dbg("pour_rip   main:", "stuck" if stuck else "ok", stuck)
                    if stuck:
                        st.restore(snap)
                        fixed.update(stuck)
                        continue
                    self.pour_rips += 1
                    yield {"added": added, "victims": victims + gone, "net": c.net, "pad": c.a.pad}
                    if added and st.piece(added[0]) is not None:
                        return                      # kept: the board has changed, the judge starts a new pass
                    fixed.update(victims + gone)    # KiCad refused it (the judge restored the state)
                    if not victims and not gone:
                        break
                finally:
                    st3.rollback(tok3)

    def _pour_nets(self) -> bool:
        """Is there a poured net this run routes? (whether the refill reaches its pads is only known after it)"""
        b = self.ctx.board
        return any(net.has_pour and n in self.targets and len(net.pads) > 1 for n, net in b.nets.items())

    def _pour_work(self) -> bool:
        b = self.ctx.board
        return any(net.has_pour and n in self.targets and len(b.clusters.get(n, {})) > 1 for n, net in b.nets.items())

    # ── the result dict ───────────────────────────────────────────────────
    def result(self) -> dict:
        ctx, cfg, state = self.ctx, self.cfg, self.ctx.state
        j = ctx.history.get("judge") or {}
        last = j.get("last") or {}
        unc = last.get("unconnected") if self.judged else None
        if not isinstance(unc, dict):
            unc = None
        total = targeted(self.targets, self.ex.get("unconnected_by_net"))
        if unc is not None:
            left = targeted(self.targets, unc)          # KiCad's ratsnest after the refill: the count that matters
        else:
            left = total - sum(1 for c in ctx.conns if state.status(c.idx) == "routed")      # proposed, not judged
        left = max(0, min(total, left))
        failed = self._failed(unc)
        pieces = state.pieces()
        add_t = sum(1 for p in pieces if p.kind == "seg")
        add_v = sum(1 for p in pieces if p.kind == "via")
        add_len = sum(p.length for p in pieces)
        touched = sorted({p.net for p in pieces})
        st = last.get("stats") or {}
        net_stats = st.get("net_stats") if isinstance(st, dict) else None
        out = {
            "routed": total - left, "total": total,
            "percent": round(100.0 * (total - left) / total, 1) if total else 100.0,
            "failed": failed, "skipped": self._skipped(),
            "nets": {n: net_stats[n][:2] for n in touched if n in net_stats} if isinstance(net_stats, dict) else {},
            "added": {"tracks": add_t, "vias": add_v, "length_mm": round(add_len, 1)},
            "drc": None, "passes": self.passes, "plan_used": cfg.used,
            "warnings": list(self.warnings), "engine": "v2",
            "stages": [{"name": r.name, "routed": r.routed, "failed": r.failed, "score": dataclasses.asdict(r.score),
                        "work": r.work, "notes": list(r.notes)} for r in ctx.reports],
            "auto": getattr(ctx.x, "auto", None) or {},
            "_board": ctx.board, "_cfg": cfg, "_scratch": self.scratch, "_judged": self.judged,
            "_unconnected": unc, "_stats": st if isinstance(st, dict) else {}, "_targets": set(self.targets),
            "_commit": commit_list(ctx), "_ctx": ctx, "_touched": touched,
        }
        if self.judged:
            new = last.get("new") or []
            raw = last.get("raw") or []
            # new_errors is KiCad's truth (copper + constraint errors); constraint_errors says how many of them are
            # length / skew / pair markers, which the judge reports per group and never repairs by ripping (§14.6)
            cn = int(last.get("cons_new") or 0)
            out["drc"] = {"errors": sum(1 for v in raw if isinstance(v, dict) and v.get("severity") == "error"),
                          "new_errors": len(new) + cn, "new_top": [_brief(m) for m in new[:5]]}
            if cn:
                out["drc"]["constraint_errors"] = cn
            data = ctx.reports[-1].data or {}
            if data.get("removed_after_drc"):
                out["removed_after_drc"] = data["removed_after_drc"]
            if data.get("repairs"):
                out["repairs"] = [{"gave_room_to": r.replace("gave room to the ", "")} for r in data["repairs"]]
            out["judge"] = {"rounds": data.get("rounds") or [], "commits": data.get("commits", 0)}
            if data.get("spoke_blame"):
                out["judge"]["spoke_blame"] = data["spoke_blame"]
            if data.get("thermal_copper"):
                out["judge"]["thermal_copper"] = data["thermal_copper"]
        else:
            why = ("plan.global.stages does not include 'judge'" if "judge" not in self.selected
                   else "the routed board would have copper inside a forbidden area of the plan"
                   if getattr(self, "plan_violated", False) else "there was nothing to commit")
            out["not_committed"] = f"{why}: no copper was committed or checked by KiCad (plan-only result)"
        if self.bridged:
            self.notes.append(f"{self.bridged} bridge(s) tried by KiCad between cut-off pour copper and the rest "
                              "of its net (stitch via / short track between pour islands)")
        notes = sorted({n for r in ctx.reports for n in r.notes} | set(self.notes))
        if notes:
            out["notes"] = notes[:20]
        self._constraint_report(out, last if self.judged else None)
        self._diagnostics(out)
        return out

    def _constraint_report(self, out: dict, last: dict | None) -> None:
        """out['constraints']: every length group, pair, keep-short net and width rule measured on the copper of
        this result, with KiCad's own verdict when the board was judged (design §14.10). Absent on a board
        without constraints."""
        cmod = _module("constraints")
        cons = getattr(self.ctx.x, "cons", None)
        if cmod is None or cons is None or not cons.any():
            return
        try:
            rep = cmod.measure(self.ctx, kicad=last)
        except Exception as e:  # noqa: BLE001
            if _strict():
                raise
            out["warnings"].append(f"electrical constraints could not be measured ({type(e).__name__}: {e})")
            return
        if rep:
            out["constraints"] = rep

    def _skipped(self) -> list[dict]:
        rep = self.ctx.reports[0] if self.ctx.reports else None
        sk = (rep.data or {}).get("skipped") if rep is not None else None
        if isinstance(sk, list):            # classify's own list (it may route a net the plan would skip)
            return [dict(e) for e in sk if e.get("reason") != "not selected"]
        return skipped_nets(self.ctx.board, self.cfg)

    def _failed(self, unc) -> list[dict]:
        """failed[]: every connection that is not routed, with a reason (no silent loss)."""
        from . import judge as judgemod
        ctx, state = self.ctx, self.ctx.state
        diag = _module("diag")
        searched = any(p["pass"] in ("detail", "legacy", "repair") for p in self.passes)
        out = {}
        still = (ctx.history.get("judge") or {}).get("open_pads") if unc is not None else None
        pour_seen = set()
        for c in sorted(ctx.conns, key=lambda c: c.idx):
            status = state.status(c.idx)
            if status == "routed":
                continue
            frm = c.a.pad or f"({round(c.a.x, 2)}, {round(c.a.y, 2)})"
            to = c.b.pad or f"({round(c.b.x, 2)}, {round(c.b.y, 2)})"
            fail = dict(judgemod.fail_of(state, c.idx) or self.failed_seen.get((c.net, frm, to)) or {})
            if still is not None and not pieces_is_open(c.b) and ctx.board.nets[c.net].has_pour:
                # a pad-to-pad connection of a poured net (local fill routed with the signals): whether its pads
                # are apart is the refilled board's verdict, and the pours pass's records list exactly those
                continue
            if still is not None and pieces_is_open(c.b):
                # a pad-to-pour connection: open only if the accepted, refilled board holds the pad apart from the
                # rest of its net (one record per cut-off cluster; the pours pass's own record is the detailed one)
                k = still.get(c.a.pad)
                if k is None or k[1] in pour_seen:
                    continue
                pour_seen.add(k[1])
            if not fail.get("reason"):
                if not searched:
                    fail["reason"] = "not routed: no search stage ran (plan.global.stages / this build)"
                elif status == "todo":
                    fail["reason"] = "time budget used up before this connection was tried"
                else:
                    fail["reason"] = "no path found"
            e = None
            if diag is not None:
                try:
                    e = diag.failure(ctx, c, fail)
                except Exception:  # noqa: BLE001 — diagnostics must never hide the failure itself
                    e = None
            if not isinstance(e, dict):
                e = {"net": c.net, "from": frm, "to": to, "reason": fail["reason"]}
                w = fail.get("where")
                if w:
                    e["where"] = [round(w[0], 2), round(w[1], 2)]
                if fail.get("blocked_by"):
                    e["blocked_by"] = list(fail["blocked_by"])[:6]
                for k in ("cut", "static_share", "mincut_nets", "probe", "tried"):
                    if fail.get(k) is not None:
                        e[k] = fail[k]
            if not e.get("blocked_by") and not e.get("mincut_nets") and not pieces_is_open(c.b) \
                    and float(e.get("static_share") or 0.0) < 0.999 and len(self._diagnosed) < DIAG_MAX:
                self._diagnose(c, e)
            e.setdefault("net", c.net)
            e.setdefault("from", frm)
            e.setdefault("to", to)
            self._plan_reason(e)
            e.setdefault("_a", (c.a.x, c.a.y))
            e.setdefault("_b", (c.b.x, c.b.y))
            if still is not None and pieces_is_open(c.b) and still.get(c.a.pad) is not None:
                e["_pour_key"] = still[c.a.pad][1]
            removed = str(e.get("reason") or "").startswith("routed, then removed")
            # KiCad's ratsnest decides: a connection another route closed is not a failure (v1 does the same)
            if unc is None or unc.get(c.net) or removed:
                out[(e["net"], e["from"], e["to"])] = e
        for e in (ctx.history.get("judge") or {}).get("pour_failed") or []:
            if unc is None or unc.get(e.get("net")):
                k = still.get(e.get("from")) if still is not None else None
                if k is not None:
                    # the same cut-off cluster as a connection listed above: the pours pass's record replaces it
                    for key in [key for key, o in out.items() if o.get("to") in ("pour", None) or o.get("_pour_key")]:
                        if out[key].get("_pour_key") == k[1]:
                            del out[key]
                e = dict(e)
                self._plan_reason(e)
                out[(e.get("net"), e.get("from"), e.get("to"))] = e
        if unc is not None:
            # no silent loss: KiCad's ratsnest of the judged board is the count the result reports. A net it still
            # shows open while the router's own model has every connection of it laid (so no record above names
            # it) is listed as exactly that, with the pads KiCad holds apart. Poured nets: the pours pass's records
            listed = {e.get("net") for e in out.values()}
            for n in sorted(unc):
                net = ctx.board.nets.get(n)
                if not unc.get(n) or n not in self.targets or n in listed or net is None or net.has_pour:
                    continue
                e = self._unexplained_open(n, int(unc[n]))
                out[(e["net"], e["from"], e["to"])] = e
        return list(out.values())

    def _unexplained_open(self, name: str, count: int) -> dict:
        """failed[] record of a net KiCad shows open although every connection of it is routed in the model."""
        ctx = self.ctx
        ex = ((ctx.history.get("judge") or {}).get("last") or {}).get("ex")
        groups: dict = {}
        for p in (ex.get("pads") if isinstance(ex, dict) else None) or ():
            if p.get("net") == name and p.get("layers"):
                groups.setdefault(p.get("c") or id(p), []).append((f"{p.get('ref')}.{p.get('num')}", p["x"], p["y"]))
        sides = sorted(min(g) for g in groups.values())
        if len(sides) < 2:                  # no judged extract at hand: the net's first connection stands for it
            c = next((c for c in sorted(ctx.conns, key=lambda c: c.idx) if c.net == name), None)
            sides = [(c.a.pad or "?", c.a.x, c.a.y), (c.b.pad or "?", c.b.x, c.b.y)] if c is not None else []
        a = sides[0] if sides else ("?", 0.0, 0.0)
        b = sides[1] if len(sides) > 1 else ("?", a[1], a[2])
        e = {"net": name, "from": a[0], "to": b[0],
             "reason": f"KiCad still shows {count} open connection(s) on this net although the router had every "
                       "connection of it laid: its copper does not join these pads (ripup: 'nets' with this net "
                       "routes it again)",
             "where": [round(a[1], 2), round(a[2], 2)], "_a": (a[1], a[2]), "_b": (b[1], b[2])}
        return e

    def _plan_reason(self, e: dict) -> None:
        """A failed[] record whose pad lies inside a forbidden area of the plan says so, in the plan's words (the
        searches only see a pad with no way out), and no record lists a plan area as a net or part in the way."""
        orc = self.ctx.oracle
        if not getattr(orc, "plan_on", False):
            return
        pads = getattr(self, "_pad_by_name", None)
        if pads is None:
            pads = self._pad_by_name = {}
            for p in self.ctx.board.pads:
                pads.setdefault(p.name, p)
        net = e.get("net")
        for name in (e.get("from"), e.get("to")):
            p = pads.get(name)
            if p is None:
                continue
            tag = next((t for t in (orc.plan_hit(net, l, p.x, p.y, p.x, p.y) for l in p.layers) if t), None)
            if tag is None:
                continue
            if tag.startswith("outside"):
                e["reason"] = (f"pad {name} lies {tag}: the net cannot leave it — widen the corridor or drop "
                               "corridor_strict")
            else:
                e["reason"] = (f"pad {name} lies inside a forbidden region of the plan ({tag}): {net} cannot leave "
                               "it — add the net to regions[].except, or use cost instead of forbid")
            e["where"] = [round(p.x, 2), round(p.y, 2)]
            e["static_share"] = 1.0
            for k in ("cut", "mincut_nets", "probe", "note"):
                e.pop(k, None)
            e["blocked_by"] = []
            return
        mine = [b for b in e.get("blocked_by") or [] if isinstance(b, str) and b.startswith(("plan ", "outside the strict"))]
        if mine:
            e["blocked_by"] = [b for b in e["blocked_by"] if b not in mine]
            if "forbidden region" not in str(e.get("reason")):
                e["reason"] = f"{e.get('reason')} (a forbidden region of the plan is in the way: {', '.join(mine[:3])})"
            cut = e.get("cut")
            if isinstance(cut, dict) and (cut.get("a") in mine or cut.get("b") in mine):
                e.pop("cut", None)

    def _diagnose(self, c, e: dict) -> None:
        """A failed[] record without a named blocker (the ladder ran out of budget before it looked): one priced
        search that may cross routed copper names the connections in the way. Nothing is routed (the state is
        rolled back whatever the search returns); the work is charged to no stage."""
        detail = _module("detail")
        self._diagnosed.add(c.idx)
        if detail is None or "detail" not in self.selected:
            return
        ctx, st = self.ctx, self.ctx.state
        tok = st.begin()
        status, fail0 = st.status(c.idx), None
        try:
            from . import judge as judgemod
            fail0 = judgemod.fail_of(st, c.idx)
            r = detail.route_conn(ctx, c, Budget(DIAG_WORK), widen=2, extra_vias=2, soft_cost=1.0)
        except ToolError:
            raise
        except Exception:  # noqa: BLE001 — diagnostics must never hide the failure itself
            r = {}
        finally:
            st.rollback(tok)
            if st.status(c.idx) != status:
                st.set_status(c.idx, status, fail0)
        by_idx = getattr(self, "_by_idx", None)
        if by_idx is None:
            by_idx = self._by_idx = {cn.idx: cn for cn in ctx.conns}
        nets = []
        for k in sorted((r.get("crossed") or {}), key=lambda k: (-(r["crossed"][k]), k)):
            cn = by_idx.get(k)
            if cn is not None and cn.net != c.net and cn.net not in nets:
                nets.append(cn.net)
        if nets:
            e["blocked_by"] = nets[:6]
            e["mincut_nets"] = nets[:6]
            e.setdefault("tried", [])
            if isinstance(e["tried"], list) and "priced search (diagnosis)" not in e["tried"]:
                e["tried"] = list(e["tried"]) + ["priced search (diagnosis)"]
        elif r.get("ok"):
            e["note"] = "a later search found a way: run the router again or raise global.time_budget_s"

    def _diagnostics(self, out: dict) -> None:
        ctx = self.ctx
        diag = _module("diag")
        glob = next((r for r in ctx.reports if r.name == "global"), None)
        if diag is None:
            if glob is not None and (glob.data or {}).get("cuts"):
                out["cuts"] = glob.data["cuts"][:10]
            return
        crit = []
        for key, fn in (("congestion", lambda: diag.congestion(ctx)), ("cuts", lambda: diag.cuts(ctx)),
                        ("critique", lambda: diag.critique(ctx))):
            try:
                v = fn()
            except Exception as e:  # noqa: BLE001
                if _strict():
                    raise
                self.warnings.append(f"v2 diagnostics '{key}' failed ({type(e).__name__}: {e})")
                out["warnings"] = list(self.warnings)
                continue
            if v:
                out[key] = v
                if key == "critique":
                    crit = v
        try:
            sug = diag.suggest(ctx, out["failed"], crit)
            if sug:
                out["suggest"] = sug
        except Exception as e:  # noqa: BLE001
            if _strict():
                raise
            self.warnings.append(f"v2 diagnostics 'suggest' failed ({type(e).__name__}: {e})")
            out["warnings"] = list(self.warnings)


class _NoX:
    """Stand-in for planx.V2Cfg when the plan module is not built: every default."""
    stages = STAGES
    polish = True

    def __init__(self):
        self.auto = {"roles": {}, "buses": [], "widths": {}, "layer_dirs": {}}


def _fmt(score) -> str:
    return "/".join(str(round(v, 2)) if isinstance(v, float) else str(v) for v in score.key())


def _brief(m: dict) -> dict:
    from .judge import marker_at
    e = {"type": m.get("type"), "message": m.get("message") or m.get("description")}
    items = [i.get("description") if isinstance(i, dict) else i for i in (m.get("items") or [])][:2]
    if items:
        e["items"] = items
    at = marker_at(m)
    if at:
        e["at"] = [at[0], at[1]]
    return e


def route(pr, cfg, x, ex: dict, *, scratch, baseline_raw, budget_units: int, progress=None) -> dict:
    """Run the v2 pipeline for a board extract. The judged copper ends up in ``scratch.pcb`` (never in ``pr.pcb``:
    the caller writes). Returns the dict shape session.route builds from v1 results (routed, total, percent, failed,
    skipped, nets, added, drc, passes, plan_used, _board, _cfg, _scratch) plus the §6.2 keys (stages, auto,
    congestion, cuts, critique, suggest) and, for the session hook, ``_judged`` (scratch.pcb holds a committed and
    DRC-checked result), ``_unconnected`` and ``_stats`` of that result.

    **Second opinion** (keep-best across pipelines, design §1: a stage that has not passed its gate on every board
    must not be the only path): when the staged pipeline (window search first) leaves connections open on a board
    of at most ``ALT_MAX_CONNS`` connections, build stage 1's pipeline (v1's search for the whole board, same
    judge) routes the same board from scratch, and the result KiCad judges better — fewer new DRC errors, then
    fewer open connections — is the one returned. ``out['portfolio']`` lists both. COPPER_V2_ALT=0 or an explicit
    ``x.detail`` / ``stages`` choice in the plan turns it off."""
    import copy
    import shutil
    regions0 = list(getattr(cfg, "regions", None) or [])
    try:
        x_alt = copy.deepcopy(x) if x is not None else None
    except Exception:  # noqa: BLE001 — a plan object that cannot be copied: no second opinion
        x_alt = None
    pcb = Path(scratch.pcb) if getattr(scratch, "pcb", None) else None
    pre = pcb.with_name(pcb.name + ".v2pre") if pcb is not None and pcb.exists() else None
    if pre is not None:
        shutil.copy2(pcb, pre)
    try:
        # the via-frugal runs, cheapest multiple first: each one that ends complete and clean is a candidate and the
        # next, dearer one is tried; the candidate with the fewest vias (then the shortest copper, then the lower
        # multiple) is returned. A board that is not easy at the first multiple pays for that one trial only
        best = None
        fkeep = pcb.with_name(pcb.name + ".v2frugal") if pre is not None else None
        ladder = _frugal_ladder()
        for mult in ladder:
            out_f = _frugal(pr, cfg, x, ex, scratch, baseline_raw, budget_units, progress, pre, regions0, mult)
            if out_f is None:
                break               # not easy at this multiple: a dearer via will not make it easier
            if len(ladder) == 1:
                return out_f
            add = out_f.get("added") or {}
            tot = (out_f.get("_stats") or {}).get("totals") or {}       # KiCad's count of the judged board
            key = (_misses(out_f),
                   int(tot.get("vias") if tot.get("vias") is not None else add.get("vias") or 0),
                   float(tot.get("track_length_mm") if tot.get("track_length_mm") is not None else add.get("length_mm") or 0.0),
                   mult)
            if best is None or key < best[0]:
                best = (key, out_f, list(cfg.regions))
                shutil.copy2(pcb, fkeep)
            # back to the unrouted board for the next candidate (and for the normal run)
            cfg.regions[:] = regions0
            shutil.copy2(pre, pcb)
        if best is not None:
            try:
                shutil.copy2(fkeep, pcb)
                cfg.regions[:] = best[2]
                return best[1]
            finally:
                try:
                    fkeep.unlink()
                except OSError:
                    pass
        run_a = _Run(pr, cfg, x, ex, scratch, baseline_raw, budget_units, progress)
        out = run_a.run()
        if pre is not None and _pairs_wanted(run_a, out):
            got = _pairs_opinion(pr, cfg, run_a, out, ex, scratch, baseline_raw, budget_units, pcb, pre, regions0)
            if got is not None:
                run_a, out = got
        if pre is None or x_alt is None or not _alt_wanted(run_a, out, budget_units):
            return out
        kept = pcb.with_name(pcb.name + ".v2alt")
        shutil.copy2(pcb, kept)
        regions_a = list(cfg.regions)
        try:
            shutil.copy2(pre, pcb)
            cfg.regions[:] = regions0
            x_alt.stages = ("classify", "detail", "polish", "judge")
            x_alt.detail, x_alt.classify = "legacy", "minimal"
            x_alt.window_fallback = True
            x_alt.local_fill = True         # a poured net whose pour is a local fill is routed with the signals
            x_alt.v1_trees = True           # the ladder's rules for trees the v1 search laid (repair._v1_trees)
            left_units = int(min(budget_units, _alt_work() * budget_units - run_a.total.used))
            if _key(out)[0] == 0:
                x_alt.alt_beat = _key(out)[1]       # what the second opinion has to beat (it stops when it cannot)
            run_b = _Run(pr, cfg, x_alt, ex, scratch, baseline_raw, left_units, None)
            try:
                out_b = run_b.run()
            except ToolError:
                raise
            except Exception as e:  # noqa: BLE001
                if _strict():
                    raise
                out_b = None
                out.setdefault("warnings", []).append(f"v2 second opinion failed ({type(e).__name__}: {e}); ignored")
            rows = [_verdict("staged (window search first)", out)]
            if out_b is not None:
                rows.append(_verdict("build stage 1 (v1 search, whole board)", out_b))
            if out_b is not None and out_b.get("_judged") and _key(out_b) + (_misses(out_b),) < _key(out) + (_misses(out),):
                rows[1]["kept"] = True
                out_b["portfolio"] = rows
                out_b.setdefault("notes", []).append(
                    "the staged pipeline left more open than v1's search for the whole board: the latter result was kept")
                out_b["passes"] = [dict(p, pass_="first attempt (not kept): " + str(p.get("pass")))
                                   for p in out.get("passes") or []] + list(out_b.get("passes") or [])
                for p in out_b["passes"]:
                    if "pass_" in p:
                        p["pass"] = p.pop("pass_")
                return out_b
            rows[0]["kept"] = True
            out["portfolio"] = rows
            shutil.copy2(kept, pcb)
            cfg.regions[:] = regions_a
            return out
        finally:
            try:
                kept.unlink()
            except OSError:
                pass
    finally:
        if pre is not None:
            try:
                pre.unlink()
            except OSError:
                pass


PAIRS_FIRST = "completion first"      # Pair.why prefix of a detected pair the pairs opinion un-coupled


def _pairs_wanted(run: "_Run", out: dict) -> bool:
    """Completion first: a judged result that is NOT complete and clean, on a board where pairs the router only
    DETECTED (net names / net classes — not the plan, not a KiCad rule) were laid as coupled lanes, is routed
    once more with those pairs as two nets (``_pairs_opinion``). COPPER_V2_PAIRS_ALT=0 turns it off."""
    if os.environ.get("COPPER_V2_PAIRS_ALT") == "0" or not out.get("_judged") or run.ctx is None:
        return False
    cons = getattr(run.x, "cons", None)
    if cons is None or not hasattr(cons, "coupled_pairs") or _key(out) == (0, 0):
        return False
    auto = {p.p for p in cons.coupled_pairs() if p.src in ("name", "class")}
    hist = run.ctx.history.get("pairs") or {}
    return any(r.get("coupled") for p, rec in hist.items() if p in auto for r in (rec.get("conns") or {}).values())


def _pairs_opinion(pr, cfg, run_a, out, ex, scratch, baseline_raw, budget_units, pcb, pre, regions0):
    """The same board again with the detected pairs routed as two nets (as before design §14.8). Kept only when
    KiCad judges it strictly better — fewer new copper errors, then fewer open connections: a coupled pair is
    worth less than a connection. -> (run, out) of the kept second run, or None (the first result stands)."""
    import copy
    import dataclasses
    import shutil
    cons = run_a.x.cons
    k = _key(out)
    try:
        x_p = copy.deepcopy(run_a.x)
    except Exception:  # noqa: BLE001
        return None
    why = (f"{PAIRS_FIRST}: laid as coupled lanes the board ended with {k[1]} open connection(s)"
           + (f" and {k[0]} new error(s)" if k[0] else "") + "; diff_pairs[].coupled: true lays the pair regardless")
    x_p.cons = dataclasses.replace(cons, pairs=tuple(
        dataclasses.replace(p, coupled=False, why=why) if (p.coupled and p.src in ("name", "class")) else p
        for p in cons.pairs))
    if k[0] == 0:
        x_p.alt_beat = k[1] - 1                 # it stops once it cannot end with fewer open connections
    kept = pcb.with_name(pcb.name + ".v2pairs")
    shutil.copy2(pcb, kept)
    regions_a = list(cfg.regions)
    try:
        shutil.copy2(pre, pcb)
        cfg.regions[:] = regions0
        run_p = _Run(pr, cfg, x_p, ex, scratch, baseline_raw, budget_units, None)
        try:
            out_p = run_p.run()
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            if _strict():
                raise
            out_p = None
            out.setdefault("warnings", []).append(f"v2 pairs-as-two-nets run failed ({type(e).__name__}: {e}); ignored")
        if out_p is not None and out_p.get("_judged") and _key(out_p) < k:
            kp = _key(out_p)
            out_p.setdefault("notes", []).append(
                f"the differential pairs detected from net names / classes were routed as two nets: laid as coupled "
                f"lanes the board ended with {k[1]} open connection(s), as two nets with {kp[1]}"
                + (f" ({k[0]} against {kp[0]} new error(s))" if k[0] or kp[0] else "")
                + ". diff_pairs[].coupled: true in the plan lays a pair as lanes regardless")
            out_p["passes"] = [dict(p, **{"pass": "coupled pairs (not kept): " + str(p.get("pass"))})
                               for p in out.get("passes") or []] + list(out_p.get("passes") or [])
            return run_p, out_p
        shutil.copy2(kept, pcb)
        cfg.regions[:] = regions_a
        return None
    finally:
        try:
            kept.unlink()
        except OSError:
            pass


def _halo() -> tuple:
    env = os.environ.get("COPPER_V2_HALO")
    if env:
        try:
            a, b = env.split(",")
            return float(a), float(b)
        except ValueError:
            pass
    return HALO_MM, HALO_COST


def _frugal_ladder() -> tuple:
    """The via-cost multiples the via-frugal run tries, in order (COPPER_V2_FRUGAL_X="3,8" overrides)."""
    env = os.environ.get("COPPER_V2_FRUGAL_X")
    if env:
        try:
            return tuple(float(v) for v in env.split(",") if v.strip())
        except ValueError:
            pass
    return tuple(VIA_FRUGAL_LADDER)


def _frugal(pr, cfg, x, ex, scratch, baseline_raw, budget_units, progress, pre, regions0, mult=None):
    """The via-frugal run (stage 6): the whole staged pipeline with ``VIA_FRUGAL`` x the plan's via costs. Its
    result is returned only when the board proves easy — the frugal detail stage fails next to nothing and the
    run ends complete with no new DRC error; otherwise None, with ``cfg`` and the scratch board as they were (the
    caller then runs the normal pipeline from scratch: its result is what it was without this function).
    Off: COPPER_V2_FRUGAL=0, ``x.via_frugal = False``, an explicit stage / detail choice, or no judge stage."""
    import copy
    import shutil
    mult = VIA_FRUGAL if mult is None else float(mult)
    off = (mult <= 1.0 or os.environ.get("COPPER_V2_FRUGAL") == "0" or x is None or pre is None
           or getattr(x, "via_frugal", True) is False or getattr(x, "detail", None)
           or os.environ.get("COPPER_V2_DETAIL"))
    if off:
        return None
    try:
        x_f = copy.deepcopy(x)
    except Exception:  # noqa: BLE001
        return None
    probe = _Run(pr, cfg, x_f, ex, scratch, baseline_raw, budget_units, progress)
    need = ("global", "detail", "judge")
    if any(s not in probe.selected or not probe.avail.get(s, False) for s in need) or probe.detail_engine() != "window":
        return None
    costs = (cfg.via_cost, {n: nc.via_cost for n, nc in cfg.nets.items()})
    cfg.via_cost = costs[0] * mult
    for nc in cfg.nets.values():
        nc.via_cost = nc.via_cost * mult
    out = None
    try:
        probe._frugal_auto = True    # run() sets the threshold once the connections are known
        try:
            out = probe.run()
        except _NotEasy:
            out = None
        except ToolError:
            raise
        except Exception:  # noqa: BLE001
            if _strict():
                raise
            out = None
        if out is not None and not (out.get("_judged") and _key(out) == (0, 0) and not out.get("failed")):
            out = None
        if out is not None:
            out.setdefault("notes", []).append(
                f"an easy board: routed at {mult:g} x the plan's via cost (fewer layer changes, longer "
                "detours); plan.global.via_frugal: false gives the plain run")
        return out
    finally:
        cfg.via_cost = costs[0]
        for n, nc in cfg.nets.items():
            if n in costs[1]:
                nc.via_cost = costs[1][n]
        if out is None:
            cfg.regions[:] = regions0
            try:
                shutil.copy2(pre, Path(scratch.pcb))
            except OSError:
                pass


def _dbg(*a) -> None:
    if os.environ.get("COPPER_V2_DEBUG"):
        import sys
        print("[v2]", *a, file=sys.stderr, flush=True)


def pieces_is_open(t) -> bool:
    """pieces.is_open without importing the module at load time (an open terminal: no pad, no cluster, no layers)."""
    return not getattr(t, "pad", None) and not getattr(t, "cluster", None) and not getattr(t, "layers", None)


def _bridge_spec(b2, st2, c, to=None) -> dict | None:
    """The two sides of a pad-to-pour connection for ``detail.route_conn(bridge=...)``: everything joined to the
    pad's cluster on the refilled board ``b2`` (static clusters + this pass's stubs) against the rest of the net.
    'box' spans the nearest pair of points of the two sides, 'all' the whole source side. None = no other side.
    ``to`` (a cluster id): the other side is only what is joined to that cluster (a pad-to-pad connection: the
    repair ladder's bridge between the two parts of a tree)."""
    cl = b2.clusters.get(c.net) or {}
    if c.a.cluster not in cl or (to is not None and to not in cl):
        return None
    src_c, src_p = {c.a.cluster}, set()
    held = set()                    # pieces that hang on some cluster (a loose piece is no target)
    tgt_c = None if to is None else {to}
    for cs, ps in st2.net_groups(c.net, True):
        if c.a.cluster in cs:
            src_c, src_p = set(cs), set(ps)
            if to is not None and to in cs:
                return None         # one group already
        elif to is not None:
            if to in cs:
                tgt_c = set(cs)
                held = set(ps)
        elif cs:
            held.update(ps)
    layer_of = {n: i for i, n in enumerate(b2.layers)}

    def side(cids, pieces):
        sp = {"pads": [], "segs": [], "vias": [], "islands": []}
        pts = []
        for cid in sorted(cids):
            k = cl[cid]
            for p in sorted(k["pads"], key=lambda p: (p.name, p.x, p.y)):
                if p.layers:
                    sp["pads"].append(Terminal(p.x, p.y, tuple(p.layers), pad=p.name, cluster=cid))
                    pts.append((p.x, p.y))
            for t in k["tracks"]:
                l = t[1] if isinstance(t[1], int) else layer_of.get(t[1], -1)
                sp["segs"].append((l, t[2], t[3], t[4], t[5]))
                pts += [(t[2], t[3]), (t[4], t[5])]
            for v in k["vias"]:
                sp["vias"].append((v[1], v[2]))
                pts.append((v[1], v[2]))
            for l, poly in k["islands"]:
                sp["islands"].append((l, poly))
                pts += list(poly)
        for p in pieces:
            if p.kind == "via":
                sp["vias"].append((p.x1, p.y1))
                pts.append((p.x1, p.y1))
            else:
                sp["segs"].append((p.layer, p.x1, p.y1, p.x2, p.y2))
                pts += [(p.x1, p.y1), (p.x2, p.y2)]
        return sp, pts
    mine = list(st2.pieces(net=c.net))
    src, sp = side(src_c, [p for p in mine if p.id in src_p])
    tgt, tp = side([cid for cid in cl if cid not in src_c and (tgt_c is None or cid in tgt_c)],
                   [p for p in mine if p.id in held])
    if not sp or not tp:
        return None
    x0, y0 = min(x for x, _y in sp), min(y for _x, y in sp)
    x1, y1 = max(x for x, _y in sp), max(y for _x, y in sp)

    def thin(pts):
        pts = sorted(set(pts))
        if len(pts) > BRIDGE_POINTS:
            n = len(pts)
            pts = [pts[(k * (n - 1)) // (BRIDGE_POINTS - 1)] for k in range(BRIDGE_POINTS)]
        return pts
    near = [t for t in tp if x0 - 10 <= t[0] <= x1 + 10 and y0 - 10 <= t[1] <= y1 + 10] or tp
    best = None
    tpl = thin(near)
    for ax, ay in thin(sp):
        for bx, by in tpl:
            d = abs(ax - bx) + abs(ay - by)
            if best is None or (d, ax, ay, bx, by) < best:
                best = (d, ax, ay, bx, by)
    _d, ax, ay, bx, by = best
    return {"src": src, "tgt": tgt, "box": (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)),
            "all": (x0, y0, x1, y1), "gap": round(best[0], 3), "at": [round((ax + bx) / 2, 2), round((ay + by) / 2, 2)]}


def _bridge_diag(ctx, failed: list, c, spec: dict, fail: dict | None) -> None:
    """Complete the failed[] record of a pad-to-pour connection with what the bridge search found (the pours
    search's own record is a reason and a place only): what closes the pad's side in, how far the rest of the
    net is, what was tried."""
    e = next((e for e in failed if e.get("net") == c.net and e.get("from") == (c.a.pad or "point")), None)
    if e is None or not isinstance(fail, dict):
        return
    nets, tags = [], []
    for b in fail.get("blockers") or []:
        n = b.get("net")
        if isinstance(n, int):
            net = ctx.board.net_by_id.get(n)
            n = net.name if net is not None else None
        if n and n != c.net and n not in nets:
            nets.append(n)
        if b.get("tag") and b["tag"] not in tags:
            tags.append(b["tag"])
    e["blocked_by"] = nets[:6]
    e["mincut_nets"] = nets[:6]
    e["static_share"] = fail.get("static_share", 1.0)
    e["probe"] = fail.get("probe") or {"enclosed": "none"}
    e["tried"] = ["pours search"] + list(fail.get("tried") or [])
    e["cut"] = {"a": c.a.pad or "point", "b": f"nearest other {c.net} copper", "gap_mm": spec.get("gap"),
                "nets": nets[:6], "at": spec.get("at")}
    what = ", ".join(str(v) for v in (nets or tags)[:4])
    e["reason"] = (f"pour pad cut off: no legal track or stitch via from its side of {c.net} to the rest of the net "
                   f"({spec.get('gap')} mm away)" + (f"; closed in by {what}" if what else "")
                   + f" [{fail.get('code') or 'no path'}]")


def _is_open_conn(ctx, idx: int) -> bool:
    """A pad-to-pour connection (the refill / the pours pass closes those, not the search)."""
    c = ctx.state.conn(idx)
    return c is not None and not c.b.pad and not c.b.cluster and not c.b.layers


def _key(out: dict) -> tuple:
    """(new copper errors, open connections). KiCad's constraint markers (length, skew, pair gap ...) are not in
    it: no other pipeline or via cost repairs those, they are reported per group (design §14.6)."""
    drc = out.get("drc") or {}
    return (max(0, int(drc.get("new_errors") or 0) - int(drc.get("constraint_errors") or 0)),
            int(out.get("total") or 0) - int(out.get("routed") or 0))


def _misses(out: dict) -> int:
    """Constraint rows of a result that miss their target (length groups, pairs, keep-short, widths): what
    breaks a tie between two results KiCad judges equal in copper errors and open connections (design §14.6)."""
    return int((out.get("constraints") or {}).get("missed") or 0)


def _verdict(name: str, out: dict) -> dict:
    k = _key(out)
    return {"pipeline": name, "new_errors": k[0], "open": k[1], "kept": False}


def _alt_wanted(run: "_Run", out: dict, budget_units: int) -> bool:
    if os.environ.get("COPPER_V2_ALT") == "0" or getattr(run.x, "alt", True) is False:
        return False
    if not run.judged or run.ctx is None or _module("legacy") is None:
        return False
    if run.detail_engine() != "window" or "detail" not in run.selected or "judge" not in run.selected:
        return False                        # the run already was v1's search / a stage-limited run
    if getattr(run.x, "detail", None) or os.environ.get("COPPER_V2_DETAIL"):
        return False
    if any(st not in run.selected for st in STAGES):
        return False                        # the plan chose its stages: no other pipeline behind its back
    if len(run.ctx.conns) > ALT_MAX_CONNS or _key(out) == (0, 0):
        return False
    return _alt_work() * budget_units - run.total.used >= 0.5 * budget_units


def _alt_work() -> float:
    try:
        return float(os.environ.get("COPPER_V2_ALT_WORK") or ALT_MAX_WORK)
    except ValueError:
        return ALT_MAX_WORK


def commit_list(out_or_ctx) -> tuple:
    """(tracks, vias) of a run in commit order — what the determinism tests compare (same board + same plan ⇒ identical)."""
    state = getattr(out_or_ctx, "state", None)
    tracks, vias, _ids = state.export()
    return tuple(map(tuple, tracks)), tuple(map(tuple, vias))
