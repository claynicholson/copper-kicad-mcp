"""Project creation from templates, status, summaries and checkpoint diffs.

Shared by tools/project_tools.py and resources.py.
"""
from __future__ import annotations

import collections
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .. import __version__, sexpr
from .. import project as P
from ..kicad import env
from ..mcp.registry import ToolError
from .common import cli, comp_kind, is_auto_net, is_ground_net, is_power_net, lcsc_of, load_netlist, natural_key

TEMPLATES = Path(__file__).resolve().parent.parent / "data" / "templates"
# 'jlc-*' are the default fab profile's own files; '2layer' / '4layer' are the same stackup skeletons with the
# rules of whatever fab profile the project uses (project_create(fab=…) or fab.profile in the Copper config)
TEMPLATE_NAMES = ["blank", "jlc-2layer", "jlc-4layer", "2layer", "4layer"]
_SKELETON = {2: "jlc-2layer", 4: "jlc-4layer"}


# ── capabilities / status ────────────────────────────────────────────────
_PCBNEW_OK: dict = {}


def pcbnew_ok() -> dict:
    py = env.kicad_python()
    if not py:
        return {"ok": False, "python": None}
    key = str(py)
    if key not in _PCBNEW_OK:
        try:
            r = subprocess.run([str(py), "-c", "import pcbnew,sys;sys.stdout.write(pcbnew.Version())"],
                               capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                               creationflags=env._NO_WINDOW)
            _PCBNEW_OK[key] = {"ok": r.returncode == 0, "python": key, "version": (r.stdout or "").strip()[-20:] or None}
        except (OSError, subprocess.TimeoutExpired) as e:
            _PCBNEW_OK[key] = {"ok": False, "python": key, "error": str(e)}
    return _PCBNEW_OK[key]


def ipc_status() -> dict:
    cands = []
    if os.environ.get("KICAD_API_SOCKET"):
        cands.append(Path(os.environ["KICAD_API_SOCKET"].replace("ipc://", "")))
    cands.append(Path(tempfile.gettempdir()) / "kicad" / "api.sock")
    cands.append(Path("/tmp/kicad/api.sock"))
    sock = next((str(c) for c in cands if c.exists()), None)
    return {"socket": sock, "kicad_running_with_api": bool(sock),
            "kipy_installed": importlib.util.find_spec("kipy") is not None}


def tools_status() -> dict:
    """Autorouter state, from the same code pcb_route uses (install.freerouting.find → pcb.freerouting.status).
    Not being downloaded yet is NOT "unavailable": the first pcb_route fetches what is missing."""
    from ..install import freerouting
    f = freerouting.find()
    out = {"tools_dir": f.get("tools_dir"), "freerouting": f.get("jar"), "java": f.get("java"),
           "autorouting_available": bool(f.get("available")), "autorouting_ready": bool(f.get("ok"))}
    if f.get("ok"):
        out["autorouting"] = "ready"
    elif f.get("available"):
        out["autorouting"] = "downloads on first use"
        out["first_route_downloads"] = f.get("first_use_downloads")
        out["note"] = ("pcb_route() works: its first run downloads " + " + ".join(f.get("first_use_downloads") or ["the router"])
                       + f" into {f.get('tools_dir')} (needs network once; `copper freerouting --install` does it ahead of time)")
    else:
        out["autorouting"] = "unavailable"
        out["problem"], out["fix"] = f.get("problem"), f.get("fix")
    return out


def status() -> dict:
    from .. import tools as _tools
    from ..mcp import registry
    fmt_problem = None
    try:    # first, so that "kicad.active" below is the install that reads the active project
        _a = P._state().get("active")
        if _a and Path(_a).parent.exists():
            fmt_problem = P.format_preflight(P.Project(Path(_a).parent, Path(_a).stem), check=False)
    except Exception:  # noqa: BLE001
        pass
    ks = env.summary()
    caps = {"kicad_cli": bool(ks.get("active") and ks["active"].get("kicad_cli")) and not ks.get("problem"),
            "pcbnew_worker": pcbnew_ok(), "ipc": ipc_status(), "routing": tools_status()}
    st = P._state()
    active = st.get("active")
    act = None
    if active and Path(active).parent.exists():
        try:
            pr = P.Project(Path(active).parent, Path(active).stem)
            act = {**pr.to_dict(), "open_in_kicad": {"schematic": P.is_open_in_kicad(pr.sch), "pcb": P.is_open_in_kicad(pr.pcb)}}
            from .verify import last_results
            act["last_verify"] = last_results(pr)
            if fmt_problem:
                act["format_problem"] = fmt_problem
        except Exception:  # noqa: BLE001
            act = {"path": active}
    from .. import config as _config
    _cpr = None
    try:
        _cpr = P.Project(Path(active).parent, Path(active).stem) if act else None
    except Exception:  # noqa: BLE001
        _cpr = None
    d = {"copper": __version__, "kicad": ks, "capabilities": caps, "active_project": act,
         "config": _config.describe(_cpr),     # active parts providers + fab profile for the active project
         "recent_projects": P.recent()[:6], "tools_loaded": len(registry.TOOLS),
         "tool_load_errors": {k: v.splitlines()[0] for k, v in getattr(_tools, "LOAD_ERRORS", {}).items()}}
    d["next"] = _config_fix(d["config"]) or next_step(d)
    return d


def _config_fix(c: dict) -> str | None:
    """The next step when the Copper config is broken or names things that do not exist: nothing else is
    trustworthy until it is fixed (which part number counts, which fab rules apply)."""
    if c.get("error"):
        return f"fix the Copper config first: {c.get('message')} — {c.get('fix')}"
    if c.get("problems"):
        p = c["problems"][0]
        return f"fix the Copper config first: {p['message']}" + (f" — {p['fix']}" if p.get("fix") else "")
    return None


def next_step(d: dict) -> str:
    if not d["capabilities"]["kicad_cli"]:
        return "Install KiCad 10 (https://www.kicad.org/download/), then copper_status again"
    a = d.get("active_project")
    if not a or not a.get("schematic"):
        return "project_create(name='my-board') or project_open(path='…/board.kicad_pro')"
    lv = a.get("last_verify") or {}
    if not lv.get("erc"):
        return "project_summary() to see the design, then verify()"
    if lv["erc"].get("errors"):
        return "verify(what=['erc']) and fix the errors (each finding has a fix)"
    if not a.get("pcb"):
        return "pcb_sync() to create the board from the schematic"
    drc = lv.get("drc") or {}
    if not drc:
        return "verify(what=['erc','drc'])"
    if drc.get("unconnected"):
        return "pcb_route() to finish routing, then verify()"
    if drc.get("errors"):
        return "verify(what=['drc']) and fix the errors"
    return "design_review() then fab_export() to produce the fab order pack"


# ── project_create ───────────────────────────────────────────────────────
def _sanitize(name: str) -> str:
    n = re.sub(r"[^\w.\-]+", "-", name.strip()).strip("-.")
    if not n:
        raise ToolError("project name is empty after removing unsafe characters", code="BAD_ARGUMENT")
    return n


def _fill(text: str, values: dict) -> str:
    # KiCad omits empty title-block entries; do the same so files are byte-identical to what KiCad writes
    for k, v in values.items():
        if v in ("", None):
            text = re.sub(r"^[ \t]*\(\w+ \"\{\{" + k + r"\}\}\"\)\n", "", text, flags=re.M)
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v).replace("\\", "\\\\").replace('"', '\\"'))
    return text


def outline_sexpr(w: float, h: float, x0: float = 100.0, y0: float = 100.0) -> str:
    return (f'\t(gr_rect\n\t\t(start {x0:g} {y0:g})\n\t\t(end {x0 + w:g} {y0 + h:g})\n'
            f'\t\t(stroke\n\t\t\t(width 0.05)\n\t\t\t(type solid)\n\t\t)\n\t\t(fill no)\n'
            f'\t\t(layer "Edge.Cuts")\n\t\t(uuid "{uuid.uuid4()}")\n\t)\n')


def create_project(name: str, directory: str | None = None, template: str | None = None,
                   board_size: list | None = None, title: str | None = None, company: str = "", rev: str = "1",
                   fab: str | None = None) -> dict:
    """template None = 2 layers for the configured fab profile ('jlc-2layer' out of the box). The design rules of a
    non-blank project always come from a fab profile: the template's own files when the profile ships them
    (jlcpcb), else the layer-count skeleton with the profile's rules applied."""
    from .. import config as _config
    from . import fab_profiles as FP
    if template is not None and template not in TEMPLATE_NAMES:
        raise ToolError(f"unknown template '{template}'", code="BAD_ARGUMENT", fix=f"one of {TEMPLATE_NAMES}")
    profile = None
    layers = 4 if template in ("jlc-4layer", "4layer") else 2
    if template != "blank" or fab:
        fab_name = fab or (FP.DEFAULT_PROFILE if str(template or "").startswith("jlc") else _config.load(None).fab_profile)
        profile = FP.load(fab_name)               # unknown / broken profile → ToolError before anything is written
    if template != "blank":
        native = (profile.get("templates") or {}).get(str(layers))
        template = native if native in TEMPLATE_NAMES else _SKELETON[layers]
    else:
        native = "blank"
    from ..kicad import compat
    compat.require_working(env.kicad())         # a kicad-cli that does not run cannot check the new files
    compat.require_authoring(env.kicad())       # new projects are KiCad 10 files: refuse before writing anything on KiCad 9
    raw = name.strip()
    name = _sanitize(name)
    base = Path(os.path.expanduser(os.path.expandvars(directory))) if directory else env.documents_dir() / "Copper"
    # `dir` is the project folder itself when it already ends in the name (as typed or as saved), else its parent
    d = base if base.name.lower() in (name.lower(), raw.lower()) else base / name
    d = d.resolve()
    if d.exists() and any(d.glob("*.kicad_pro")):
        raise ToolError(f"{d} already contains a KiCad project", code="EXISTS",
                        fix=f"project_open(path={json.dumps(str(d))}) — or choose another name/dir")
    clash = [f"{name}{ext}" for ext in (".kicad_sch", ".kicad_pcb", ".kicad_dru") if (d / f"{name}{ext}").exists()]
    if clash:
        # e.g. a folder holding a lone schematic: never overwrite a user's design file
        raise ToolError(f"{d} already has {', '.join(clash)} — refusing to overwrite it", code="EXISTS",
                        fix=f"project_open(path={json.dumps(str(d / clash[0]))}) to work on it, or choose another name/dir")
    if board_size is not None:
        if len(board_size) != 2 or not all(isinstance(v, (int, float)) and 3 <= v <= 1000 for v in board_size):
            raise ToolError("board_size must be [width_mm, height_mm], each 3–1000", code="BAD_ARGUMENT")
    d.mkdir(parents=True, exist_ok=True)
    vals = {"TITLE": title or name, "DATE": time.strftime("%Y-%m-%d"), "REV": rev, "COMPANY": company or "",
            "UUID": str(uuid.uuid4())}
    written = []
    pro = json.loads((TEMPLATES / f"{template}.kicad_pro").read_text(encoding="utf-8"))
    pro["meta"]["filename"] = f"{name}.kicad_pro"
    (d / f"{name}.kicad_pro").write_text(json.dumps(pro, indent=2) + "\n", encoding="utf-8", newline="\n")
    written.append(f"{name}.kicad_pro")
    sch = _fill((TEMPLATES / "default.kicad_sch").read_text(encoding="utf-8"), vals)
    (d / f"{name}.kicad_sch").write_text(sch, encoding="utf-8", newline="\n")
    written.append(f"{name}.kicad_sch")
    pcb_tpl = "blank" if template == "blank" else template
    pcb = _fill((TEMPLATES / f"{pcb_tpl}.kicad_pcb").read_text(encoding="utf-8"), vals)
    pcb = pcb.replace("{{OUTLINE}}", outline_sexpr(*board_size) if board_size else "")
    (d / f"{name}.kicad_pcb").write_text(pcb, encoding="utf-8", newline="\n")
    written.append(f"{name}.kicad_pcb")
    if native and template.startswith("jlc"):
        shutil.copy2(TEMPLATES / "jlc.kicad_dru", d / f"{name}.kicad_dru")
        written.append(f"{name}.kicad_dru")
    pr = P.resolve(str(d / f"{name}.kicad_pro"), activate=True)
    rules_label = None
    if profile is not None:
        if not native:
            # the skeleton carries the default profile's numbers: overwrite rules, net-class minimums and the .kicad_dru
            from ..pcb import rules as _rules
            rules_label = _rules.apply(pr, preset=profile["name"])["label"]
            written.append(f"{name}.kicad_dru")
        if profile["name"] != _config.load(pr).fab_profile:
            _config.set_project(pr, {"fab": {"profile": profile["name"]}})     # the project remembers its fab
            written.append(".copper/config.json")
    # honest verification: KiCad itself must load every file cleanly
    check = load_check(pr)
    if not check["ok"]:
        raise ToolError(f"KiCad could not load the new project cleanly: {check}", code="TEMPLATE_INVALID",
                        data={"check": check, "dir": str(d)})
    cp = P.checkpoint(pr, "created")
    out = {"project": pr.to_dict(), "template": template if native else f"{layers}layer", "written": written,
           "board_size_mm": board_size,
           "kicad_check": check, "checkpoint": cp}
    if profile is not None:
        out["fab"] = profile["name"]
        out["rules"] = rules_label or FP.rules(profile, "standard", layers)["label"]
        if not native and layers >= 4:
            out["stackup_note"] = ("the 4-layer stackup thicknesses are a generic 1.6 mm build — set your fab's own in "
                                   "KiCad's Board Setup ▸ Physical Stackup before impedance-critical routing")
    if name != raw:
        out["note"] = f"name {raw!r} was saved as {name!r} (spaces/special characters replaced in file names)"
    return out


def load_check(pr) -> dict:
    out = {"ok": True}
    tmp = Path(tempfile.mkdtemp(prefix="copper-chk-"))
    try:
        r = cli(["sch", "erc", "--format", "json", "-o", str(tmp / "e.json"), str(pr.sch)], timeout=120)
        if not (tmp / "e.json").exists():
            out.update(ok=False, sch=(r.stderr or r.stdout).strip()[-400:])
        else:
            e = json.loads((tmp / "e.json").read_text(encoding="utf-8"))
            out["erc_violations"] = sum(len(s.get("violations", [])) for s in e.get("sheets", []))
        if pr.pcb.exists():
            r = cli(["pcb", "drc", "--format", "json", "-o", str(tmp / "d.json"), str(pr.pcb)], timeout=120)
            if not (tmp / "d.json").exists():
                out.update(ok=False, pcb=(r.stderr or r.stdout).strip()[-400:])
            else:
                dd = json.loads((tmp / "d.json").read_text(encoding="utf-8"))
                out["drc_violations"] = len(dd.get("violations", []))
                out["drc_types"] = sorted({v.get("type") for v in dd.get("violations", [])})
            if "rror" in (r.stderr or "") and "rule" in (r.stderr or "").lower():
                out.update(ok=False, rules=(r.stderr or "").strip()[-400:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


# ── summary ──────────────────────────────────────────────────────────────
def sheet_list(pr) -> list[dict]:
    out = []
    for f in pr.sheets():
        try:
            tree = sexpr.loads(f.read_text(encoding="utf-8"))
            nsym = len([s for s in sexpr.find_all(tree, "symbol") if not str(sexpr.value(s, "lib_id", "")).startswith("power:")])
        except (OSError, ValueError):
            nsym = None
        out.append({"file": f.name, "symbols": nsym})
    return out


def board_outline_mm(pcb: Path) -> list | None:
    bb = board_bbox_mm(pcb)
    return [round(bb[2] - bb[0], 3), round(bb[3] - bb[1], 3)] if bb else None


def board_bbox_mm(pcb: Path) -> list | None:
    """[x0, y0, x1, y1] of the Edge.Cuts graphics (board coordinates, mm)."""
    try:
        tree = sexpr.loads(pcb.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    xs, ys = [], []
    for kind in ("gr_line", "gr_rect", "gr_arc", "gr_poly", "gr_circle"):
        for g in sexpr.find_all(tree, kind):
            if sexpr.value(g, "layer") != "Edge.Cuts":
                continue
            for k in ("start", "end", "mid", "center"):
                e = sexpr.find(g, k)
                if e is not None and len(e) >= 3:
                    xs.append(float(e[1]))
                    ys.append(float(e[2]))
            pts = sexpr.find(g, "pts")
            for xy in (sexpr.find_all(pts, "xy") if pts else []):
                xs.append(float(xy[1]))
                ys.append(float(xy[2]))
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def summary(pr, with_netlist: bool = True) -> dict:
    from .verify import last_results
    d: dict = {"project": pr.to_dict(), "sheets": sheet_list(pr) if pr.sch.exists() else []}
    if with_netlist and pr.sch.exists():
        try:
            nl = load_netlist(pr)
            comps = {r: c for r, c in nl["components"].items() if not r.startswith("#")}
            kinds = collections.Counter(comp_kind(r, c) for r, c in comps.items())
            placed = [c for c in comps.values() if not c.get("dnp") and not c.get("exclude_from_bom")]
            from ..parts.providers import part_number
            from .design_review import _sourcing_context
            src = _sourcing_context(pr)          # which field is 'the part number' here, and is one required at all

            def _sourced(c):
                return bool(part_number(c.get("fields", {}), src["field"])) or not src["required"]
            locked = [c for c in placed if _sourced(c)]
            nets = nl["nets"]
            unconn = sum(1 for n in nets if n.startswith("unconnected-("))
            missing_lcsc = sorted((r for r, c in comps.items() if c in placed and not _sourced(c)), key=natural_key)
            missing_fp = sorted((r for r, c in comps.items() if not c.get("footprint")), key=natural_key)
            ics = sorted((f"{r} {c.get('value', '')}" for r, c in comps.items() if comp_kind(r, c) == "ic"), key=natural_key)
            d.update({
                "components": len(comps), "by_kind": dict(kinds.most_common()),
                "dnp": sum(1 for c in comps.values() if c.get("dnp")),
                # KiCad's full net names (as on the board, e.g. '/SDA'); every net incl. KiCad's per-pin 'unconnected-(…)' nets
                "nets": len(nets), "connected_nets": sum(1 for v in nets.values() if len(v) >= 2),
                "unconnected_pin_nets": unconn, "named_nets": sum(1 for n in nets if not is_auto_net(n)),
                "power_rails": sorted(n for n, v in nets.items() if is_power_net(n, v) and not is_auto_net(n)),
                "ground_nets": sorted(n for n, v in nets.items() if is_ground_net(n, v)),
                "part_field": src["field"], "sourced_pct": round(100 * len(locked) / max(1, len(placed)), 1),
                "missing_part_number": missing_lcsc[:30], "missing_footprint": missing_fp[:30], "ics": ics[:30],
            })
            for k, full in (("missing_part_number", missing_lcsc), ("missing_footprint", missing_fp), ("ics", ics)):
                if len(full) > 30:
                    d[k + "_total"] = len(full)
        except ToolError as e:
            d["netlist_error"] = e.to_dict()
    if pr.pcb.exists():
        d["board_mm"] = board_outline_mm(pr.pcb)
        try:
            from .fab import copper_layers
            d["copper_layers"] = len(copper_layers(pr.pcb))
            tree_txt = pr.pcb.read_text(encoding="utf-8")
            d.update(board_counts(tree_txt))
        except Exception:  # noqa: BLE001
            pass
    d["last_verify"] = last_results(pr)
    from .. import config as _config
    d["config"] = _config.describe(pr)      # active parts providers + fab profile (+ which config files set them)
    d["open_in_kicad"] = {"schematic": P.is_open_in_kicad(pr.sch), "pcb": P.is_open_in_kicad(pr.pcb)}
    try:
        d["checkpoints"] = len(P.list_checkpoints(pr))
    except OSError:
        d["checkpoints"] = 0
        d["read_only"] = ("the project folder is not writable (e.g. a KiCad demo under Program Files) — checks still work, "
                          "but copy it somewhere writable before editing: project_create can't, so copy the folder and project_open the copy")
    d["next"] = _summary_next(d)
    fix = _config_fix(d["config"])
    if fix:
        # the sourcing numbers above were computed from fallback defaults: do not present them as this project's
        for k in ("part_field", "sourced_pct", "missing_part_number", "missing_part_number_total"):
            d.pop(k, None)
        d["next"] = [fix] + [s for s in d["next"] if not s.startswith("parts_source_bom()")]
    return d


def board_counts(txt: str) -> dict:
    """Top-level board item counts. KiCad 9/10 write '(segment' / '(via' / '(zone' alone on their line
    (children on the next lines), older versions '(segment (start …'; teardrop zones are counted apart."""
    def n(head):
        # '\t?': some real boards (KiCad's own royalblue54L demo) carry their top-level items at column 0
        return len(re.findall(rf"^\t?\({head}(?=[\s)])", txt, re.M))
    zones = n("zone")
    # KiCad 10 marks teardrops '(attr (teardrop …' (older boards name them '$teardrop…'); look only inside each zone's head
    tear = sum(1 for m in re.finditer(r"^\t?\(zone(?=[\s)])", txt, re.M)
               if re.search(r"\(teardrop\b|\(name \"\$teardrop", re.split(r"\n\t?\((?=zone|segment|via|footprint|arc|gr_|group|embedded)", txt[m.end():m.end() + 400], 1)[0]))
    out = {"footprints_on_board": n("footprint"), "tracks": n("segment") + n("arc"), "vias": n("via"), "zones": zones - tear}
    if tear:
        out["teardrop_zones"] = tear
    return out


def _summary_next(d: dict) -> list[str]:
    s = []
    if d.get("components", 0) == 0:
        s.append("add parts: parts_search → sch_apply(ops=[{op:'place',…},{op:'connect',…}])")
        return s
    lv = d.get("last_verify") or {}
    erc = lv.get("erc")
    if not erc or erc.get("stale"):
        s.append("verify() — ERC results are missing or older than the schematic")
    elif erc.get("errors"):
        s.append(f"fix {erc['errors']} ERC error(s): verify(what=['erc'])")
    if d.get("missing_footprint"):
        s.append(f"assign footprints to {', '.join(d['missing_footprint'][:5])}")
    if d.get("sourced_pct", 100) < 100:
        s.append(f"parts_source_bom() — {d.get('sourced_pct')}% of parts have "
                 + (f"an LCSC number" if d.get("part_field") == "LCSC" else f"a {d.get('part_field') or 'part'} number"))
    if not d["project"].get("pcb") or not d.get("footprints_on_board"):
        s.append("pcb_sync() → pcb_setup() → pcb_place() to start the board")
    else:
        drc = lv.get("drc")
        if not drc or drc.get("stale"):
            s.append("verify(what=['erc','drc'])")
        elif drc.get("unconnected"):
            s.append(f"pcb_route() — {drc['unconnected']} unrouted connection(s)")
        elif drc.get("errors"):
            s.append(f"fix {drc['errors']} DRC error(s)")
        else:
            s.append("design_review() then fab_export()")
    return s


# ── checkpoint diff ──────────────────────────────────────────────────────
def _symbols(path: Path) -> dict:
    out = {}
    try:
        tree = sexpr.loads(open(P._long(path), encoding="utf-8").read())
    except (OSError, ValueError):
        return out
    for s in sexpr.find_all(tree, "symbol"):
        props = {str(p[1]): str(p[2]) for p in sexpr.find_all(s, "property") if len(p) > 2}
        ref = props.get("Reference", "?")
        at = sexpr.find(s, "at")
        out.setdefault(ref, []).append({"lib_id": str(sexpr.value(s, "lib_id", "")), "value": props.get("Value", ""),
                                        "footprint": props.get("Footprint", ""),
                                        "lcsc": lcsc_of(props), "at": [str(x) for x in (at[1:] if at else [])]})
    return out


def _footprints(path: Path) -> dict:
    out = {}
    try:
        tree = sexpr.loads(open(P._long(path), encoding="utf-8").read())
    except (OSError, ValueError):
        return out
    for fp in sexpr.find_all(tree, "footprint"):
        props = {str(p[1]): str(p[2]) for p in sexpr.find_all(fp, "property") if len(p) > 2}
        at = sexpr.find(fp, "at")
        out[props.get("Reference", "?")] = {"fp": str(fp[1]), "at": [str(x) for x in (at[1:] if at else [])],
                                            "layer": str(sexpr.value(fp, "layer", ""))}
    return out


def _hier_labels(path: Path) -> "collections.Counter":
    """Names of the hierarchical labels on a sheet (a Counter: one name can be placed several times)."""
    try:
        t = open(P._long(path), encoding="utf-8").read()
    except OSError:
        return collections.Counter()
    return collections.Counter(m.replace('\\"', '"')
                               for m in re.findall(r'^\t\(hierarchical_label\s+"((?:[^"\\]|\\.)*)"', t, re.M))


def _counts(tree_path: Path, heads=("segment", "via", "zone", "wire", "label", "global_label", "hierarchical_label", "junction",
                                    "no_connect", "arc")) -> dict:
    try:
        t = open(P._long(tree_path), encoding="utf-8").read()
    except OSError:
        return {}
    return {h: len(re.findall(rf"^\t\({h}\b", t, re.M)) for h in heads}


def diff_checkpoint(pr, cid: str | None = None) -> dict:
    snaps = P.list_checkpoints(pr)
    if not snaps:
        raise ToolError("no checkpoints yet", code="NOT_FOUND", fix="checkpoint(action='create', label='...')")
    cid = cid or snaps[0]["id"]
    src = pr.copper_dir / "snapshots" / cid
    if not os.path.isdir(P._long(src)):
        raise ToolError(f"no checkpoint '{cid}'", code="NOT_FOUND", fix="checkpoint(action='list')")
    files = []
    names = set(P.snapshot_files(src)) | set(P.design_files(pr))      # sub-folder sheets included ('sch/x.kicad_sch')
    for n in sorted(names):
        a, b = src / n, pr.dir / n
        if not os.path.exists(P._long(a)):
            files.append({"file": n, "status": "added since checkpoint"})
            continue
        if not b.exists():
            files.append({"file": n, "status": "deleted since checkpoint"})
            continue
        ta, tb = open(P._long(a), "rb").read(), b.read_bytes()
        if ta == tb:
            continue
        la = collections.Counter(ta.decode("utf-8", "replace").splitlines())
        lb = collections.Counter(tb.decode("utf-8", "replace").splitlines())
        entry = {"file": n, "status": "modified", "lines_added": sum((lb - la).values()), "lines_removed": sum((la - lb).values())}
        if n.endswith(".kicad_sch"):
            sa, sb = _symbols(a), _symbols(b)
            entry["symbols_added"] = sorted(set(sb) - set(sa), key=natural_key)
            entry["symbols_removed"] = sorted(set(sa) - set(sb), key=natural_key)
            changed = []
            for r in sorted(set(sa) & set(sb), key=natural_key):
                if sa[r] != sb[r]:
                    x, y = sa[r][0], sb[r][0]
                    what = [k for k in ("lib_id", "value", "footprint", "lcsc", "at") if x.get(k) != y.get(k)]
                    changed.append({"ref": r, "changed": what or ["units"]})
            entry["symbols_changed"] = changed[:50]
            ca, cb = _counts(a), _counts(b)
            entry["items_delta"] = {k: cb.get(k, 0) - ca.get(k, 0) for k in ca if cb.get(k, 0) != ca.get(k, 0)}
            ha, hb = _hier_labels(a), _hier_labels(b)
            if ha != hb:
                # a sheet's hierarchical labels are its interface to the parent: losing one silently cuts a net
                entry["hier_labels_added"] = sorted((hb - ha).elements(), key=natural_key)
                entry["hier_labels_removed"] = sorted((ha - hb).elements(), key=natural_key)
                if entry["hier_labels_removed"]:
                    entry["note"] = (f"hierarchical label(s) {', '.join(entry['hier_labels_removed'][:6])} removed — the parent's "
                                     f"sheet pin of that name no longer connects into this sheet; verify(what=['erc'])")
        elif n.endswith(".kicad_pcb"):
            fa, fb = _footprints(a), _footprints(b)
            entry["footprints_added"] = sorted(set(fb) - set(fa), key=natural_key)
            entry["footprints_removed"] = sorted(set(fa) - set(fb), key=natural_key)
            entry["footprints_moved"] = sorted((r for r in set(fa) & set(fb) if fa[r]["at"] != fb[r]["at"] or fa[r]["layer"] != fb[r]["layer"]), key=natural_key)[:80]
            entry["footprints_swapped"] = sorted((r for r in set(fa) & set(fb) if fa[r]["fp"] != fb[r]["fp"]), key=natural_key)
            ca, cb = _counts(a), _counts(b)
            entry["items_delta"] = {k: cb.get(k, 0) - ca.get(k, 0) for k in ca if cb.get(k, 0) != ca.get(k, 0)}
        files.append(entry)
    return {"checkpoint": cid, "changed_files": files, "identical": not files}
