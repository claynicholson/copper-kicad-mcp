"""Guided autorouter: AI-written heuristics, deterministic routing, KiCad-verified results.

The model that drives Copper never routes copper itself and Copper never calls
a model: a *plan* (plain JSON — net order, widths, layer preferences, corridors,
cost regions …) goes in, a deterministic rip-up-and-reroute engine turns it
into tracks, and KiCad's own DRC decides what counts as routed.

Public API::

    analyze(pr, ripup='none', nets=None, heatmap_path=None) -> dict
        read-only facts for writing a plan (nets, congestion, layers, keep-outs, suggested plan, schema)
    route(pr, plan=None, ripup='none', nets=None, dry_run=False, compare=False,
          while_open='refuse', progress=None) -> dict
        route + write in one transaction + DRC + repair; same plan + same board = same result
    SCHEMA_TEXT, PLAN_FIELDS         the plan schema (text / field tables)

Modules: geom (exact geometry), grid (clearance-aware occupancy maps), astar
(search), router (rip-up & reroute), model (board from the pcbnew worker),
plan (schema, validation, built-in heuristics), analysis (facts + pictures),
session (worker I/O, commit, DRC, repair).
"""
from .plan import PLAN_FIELDS, SCHEMA_TEXT  # noqa: F401
from .session import analyze, route  # noqa: F401
