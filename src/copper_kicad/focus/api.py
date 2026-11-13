"""The focus service: one place where every channel ends.

    point(pr, document, …)            canvas click / drag  -> stored focus
    latest(pr=None)                   newest stored focus + age_s (cheap; for MCP resources)
    current(pr, source, wait)         newest focus from the canvas or KiCad, optionally waiting for the user
    detail(pr, focus, render)         refs with value/footprint/position/nets (+ a cropped picture)
    clear(pr)
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .. import project as projmod
from ..mcp.registry import Image, ToolError
from . import geometry as G
from . import resolver as R
from . import store

MAX_WAIT = 300.0
PIN_CAP = 12          # pins listed per item / 2x that in focus.pins — a box over a 100-pin part stays readable


def _with_age(f: dict | None) -> dict | None:
    if not f:
        return None
    f = dict(f)
    f["age_s"] = round(max(0.0, time.time() - float(f.get("ts") or 0)), 1)
    return f


def point(pr, document: str, sheet=None, mode: str = "point", point=None, box=None, radius=None,
          add: bool = False, side: str | None = None, source: str = "canvas",
          prefer_side: str | None = None) -> dict | None:
    """Record what the user pointed at. `add` (shift) merges into the current focus: a click toggles one
    part/pin, a box adds everything it touches. Returns the stored focus (None when the merge emptied it)."""
    res = (R.resolve(pr, document, box=box, sheet=sheet, side=side) if mode == "box"
           else R.resolve(pr, document, near=point, radius=radius, sheet=sheet, side=side, prefer_side=prefer_side))
    old, _ = store.get(pr)
    if add and old and old.get("document") == res["document"] and old.get("sheet") == res.get("sheet"):
        # pins listed by a box only describe partial coverage — a selection keeps pins the user clicked
        refs = list(old.get("refs", []))
        pins = [] if old.get("mode") == "box" else list(old.get("pins", []))
        nrefs, npins = res["refs"], ([] if mode == "box" else res.get("pins", []))
        if mode != "box" and len(nrefs) == 1:
            r = nrefs[0]
            if npins:
                for p in npins:
                    if p in pins:
                        pins.remove(p)
                        if not any(q.startswith(r + ".") for q in pins) and r in refs:
                            refs.remove(r)
                    else:
                        pins.append(p)
                        if r not in refs:
                            refs.append(r)
            elif r in refs:
                refs.remove(r)
                pins = [q for q in pins if not q.startswith(r + ".")]
            else:
                refs.append(r)
        else:
            refs += [r for r in nrefs if r not in refs]
            pins += [p for p in npins if p not in pins]
        if not refs and not pins:
            store.put(pr, None)
            return None
        if nrefs or npins:
            sel = R.select(pr, document, sorted(refs, key=G.natural_key), pins, sheet=sheet)
            f = R.to_focus(sel, source)
            store.put(pr, f)
            return _with_age(f)
        return _with_age(old)            # shift-click on nothing keeps the selection
    f = R.to_focus(res, source)
    store.put(pr, f)
    return _with_age(f)


def clear(pr) -> int:
    return store.clear(pr)


def latest(pr=None) -> dict | None:
    """The newest stored focus of the project (default: active project) with `age_s`, or None.
    File read only — no KiCad calls — so it is safe to attach to every MCP resource read."""
    try:
        pr = pr or projmod.resolve(None)
    except ToolError:
        return None
    return _with_age(store.get(pr)[0])


def current(pr, source: str = "auto", wait: float = 0.0, progress=None) -> tuple[dict | None, dict]:
    """(focus | None, info). source: 'canvas' | 'kicad' | 'auto' (whichever the user touched last; a KiCad
    selection only when the IPC API is up). wait > 0 blocks until the user points on the canvas (or selects
    in KiCad) or the time runs out; info = {'waited_s', 'timed_out', 'kicad': hint when unavailable}."""
    from . import live
    info: dict = {}
    wait = max(0.0, min(float(wait or 0), MAX_WAIT))

    def kicad_focus(strict: bool):
        try:
            return live.selection(pr)
        except ToolError as e:
            if strict:
                raise
            info["kicad"] = e.message
            return None

    def pick():
        cf = store.get(pr)[0] if source in ("auto", "canvas") else None
        kf = kicad_focus(source == "kicad") if source in ("auto", "kicad") else None
        if cf and kf:
            return cf if float(cf.get("ts") or 0) >= float(kf.get("ts") or 0) else kf
        return cf or kf

    t0 = time.time()
    f = pick()
    if wait > 0:
        base_ts = float(f.get("ts") or 0) if f else 0.0
        _, seq0 = store.get(pr)
        live_ok = source in ("auto", "kicad") and "kicad" not in info
        n = 0
        while time.time() - t0 < wait:
            if progress and n % 10 == 0:
                try:
                    progress(min(0.99, (time.time() - t0) / wait),
                             "waiting for the user to point (Copper Canvas or a KiCad selection)…")
                except Exception:  # noqa: BLE001
                    pass
            n += 1
            time.sleep(0.25)
            if source in ("auto", "canvas"):
                cf, seq = store.get(pr)
                if seq > seq0 and cf:
                    f = cf
                    break
            if live_ok and n % 4 == 0:
                kf = kicad_focus(False)
                if kf and float(kf.get("ts") or 0) > base_ts:
                    f = kf
                    break
        else:
            info["timed_out"] = True
        info["waited_s"] = round(time.time() - t0, 1)
    return _with_age(f), info


def _compact_item(it: dict) -> dict:
    d = {"ref": it["ref"], "value": it.get("value", "")}
    if it.get("footprint"):
        d["footprint"] = it["footprint"]
    d["at"] = [it["x"], it["y"]]
    if it.get("rot"):
        d["rot"] = it["rot"]
    for k in ("side", "unit", "dnp", "inside"):
        if k in it and not (k == "inside" and it[k] == "full"):
            d[k] = it[k]
    nets = it.get("nets") or []
    d["nets"] = nets[:16]
    if len(nets) > 16:
        d["nets_total"] = len(nets)
    if it.get("pins"):
        d["pins"] = [{k: p[k] for k in ("num", "name", "net")
                      if p.get(k) and not (k == "net" and p[k].startswith("unconnected-("))}
                     for p in it["pins"][:PIN_CAP]]
        if len(it["pins"]) > PIN_CAP:
            d["pins_total"] = len(it["pins"])
    return d


def public(focus: dict | None) -> dict | None:
    """The focus object as handed to a model: internal bookkeeping (ts, stamp) removed, age_s present."""
    if not focus:
        return None
    f = {k: v for k, v in focus.items() if k not in ("ts", "stamp")}
    if len(f.get("pins") or []) > 2 * PIN_CAP:
        f["pins_total"] = len(f["pins"])
        f["pins"] = f["pins"][:2 * PIN_CAP]
    f.setdefault("age_s", round(max(0.0, time.time() - float(focus.get("ts") or 0)), 1))
    return f


def detail(pr, focus: dict, render: bool = True, out_dir: Path | None = None, width: int = 900,
           limit: int = 40, live_items: list | None = None) -> tuple[dict, list[Image]]:
    """Everything a model needs to act on a focus: ({'focus', 'chip', 'items', 'missing'?, 'stale'?}, [Image]).
    Items are re-read from the files as they are NOW, so positions/nets are current even for an old focus.
    `live_items` (a fresh resolve result's items) skips that re-read and keeps 'inside'/'pins' detail."""
    d = R.describe(pr, focus)
    items = live_items if live_items is not None else d["items"]
    out = {"focus": public(focus), "chip": R.chip(focus)}
    out["items"] = [_compact_item(i) for i in items[:limit]]
    if len(items) > limit:
        out["items_total"] = len(items)
        out["items_note"] = f"first {limit} of {len(items)} parts detailed — refs lists them all; use a smaller box for the rest"
    if d["missing"]:
        out["missing"] = d["missing"]
    if d["stale"]:
        out["stale"] = True
    images: list[Image] = []
    if render:
        from . import svg
        try:
            name = "focus-" + ("sch" if focus["document"] == "schematic" else "pcb") + ".png"
            images.append(svg.crop_png(pr, focus, items, d["scene"], d["region"],
                                       (out_dir or (pr.out_dir / "focus")) / name, width=width))
        except ToolError as e:
            out["render_error"] = e.message
        except Exception as e:  # noqa: BLE001 — the data is still good without the picture
            out["render_error"] = f"{type(e).__name__}: {e}"
    return out, images


def as_resource(pr=None) -> dict:
    """MCP resource content for the current focus (cheap; no rendering, no KiCad calls):
    {'uri': 'copper://focus', 'mimeType': 'application/json', 'text': '{"focus": {...}|null, "chip": "R3, U2.EN"}'}"""
    f = latest(pr)
    return {"uri": "copper://focus", "mimeType": "application/json",
            "text": json.dumps({"focus": public(f), "chip": R.chip(f)}, separators=(",", ":"), ensure_ascii=False)}
