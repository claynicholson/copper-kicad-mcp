"""parts_search · parts_get · parts_import · parts_source_bom · parts_alternatives

Parts come from *providers* (parts/providers): the project's Copper config lists which ones are active
and in what order (``parts.providers``), and every tool takes an optional ``provider=`` to ask one
directly. Out of the box the only active provider is ``lcsc`` — LCSC parts for JLCPCB assembly, JLC
*Basic* parts first (no setup fee), then *Preferred*, then *Extended* (~$3 feeder setup per unique part).
``local`` is a user catalogue file, ``kicad`` is generic library parts; third parties add their own.
Whatever the provider, a placed part also carries the vendor-neutral MPN + Manufacturer when known.
"""
from __future__ import annotations

from .. import project as P
from ..mcp.registry import Result, ToolError, tool
from ..parts import common as C

PROJECT = {"type": "string", "description": "Project folder or .kicad_pro/.kicad_sch path. Omit to use the active project."}
PROVIDER = {"type": "string", "description": "parts provider to ask (default: the project's parts.providers, in order): "
                                             "lcsc | local | kicad | a third-party name"}


def _providers(project=None, provider=None):
    from ..parts import providers as PV
    cfg, provs = PV.for_project(project, provider)
    return PV, cfg, provs


def _one(part, provider, project=None):
    """(PV, cfg, provider object, id) for a tool that takes one part id."""
    PV, cfg, provs = _providers(project, None)
    pref, pid = PV.split_id(part, cfg)
    name = provider or pref
    if name:
        return PV, cfg, PV.get(name, cfg), pid
    prov = PV.owner_of(pid, cfg, provs)
    if prov is None and C.lcsc_norm(pid) and str(pid).strip()[:1] in "Cc0123456789":
        prov = PV.get("lcsc", cfg)
    if prov is None:
        raise ToolError(f"no active parts provider knows part '{part}' (active: {[p.name for p in provs]})",
                        code="PART_NOT_FOUND", fix="parts_search(query=...) to find it, or pass provider='<name>'")
    return PV, cfg, prov, pid


def _place_hint(prov, pid: str, rec: dict | None = None) -> str:
    if prov.name == "lcsc":
        return f'sch_apply ops=[{{"op":"place","lcsc":"{pid}"}}]'
    if getattr(prov, "generic", False) and rec and rec.get("lib_id"):
        # a generic part IS its symbol + footprint + value: its id (the bare symbol) would lose the other two
        import json
        op = {"op": "place", "lib_id": rec["lib_id"], "footprint": rec.get("footprint") or "", "value": rec.get("value") or ""}
        return "sch_apply ops=[" + json.dumps({k: v for k, v in op.items() if v}, separators=(",", ":")) + "]"
    return f'sch_apply ops=[{{"op":"place","part":"{pid}","provider":"{prov.name}"}}]'


@tool("parts_search", """Find parts — use this BEFORE placing any part. The project's parts providers are asked in order and the first one
with results answers; `provider` in the result says which. Default provider 'lcsc': LCSC parts for JLCPCB assembly, ranked by spec
relevance, then JLC **Basic** (no setup fee) > **Preferred** > Extended (~$3 setup per unique part), then live JLC assembly stock, then price.
Other providers: 'local' (the user's own catalogue file — Digi-Key/Mouser/stockroom numbers, offline), 'kicad' (generic KiCad library
parts, value + footprint only), plus any third-party provider the config names.
query: free text — a value spec ('10k 0603', '100nF 16V 0402 X7R', '4.7uF 0805', 'LED red 0603', '12MHz crystal', 'LDO 3.3V SOT-223'),
an MPN ('AMS1117-3.3', 'USBLC6-2SC6', 'ESP32-C3', '7805 TO-220'), or a category phrase ('USB-C 16P', 'USB-A female',
'Header 1x4 2.54mm' (male pins; add 'female' for sockets), 'Header 2x10', '8MHz crystal 3225', '22uH inductor 3A').
A plain description works too for long-tail parts ('alcohol gas sensor', 'wifi module', 'sd card socket', 'rotary encoder'): every
word is matched as a whole word against the description and catalogue category, and lcsc asks the live catalogue in its own
wording and by category. A short family name ('MQ-3') only matches part numbers that start with it; add what the part is
('MQ-3 gas sensor') to also get parts that fit the description.
Current/voltage terms filter: an inductor whose current can't be confirmed comes back match='partial'.
Each result: provider, id (the provider's part number), mpn, manufacturer, package, stock, price, match ('partial' = not every spec
term confirmed; `unconfirmed` then lists the query words that result does not confirm, and `match_note` says so for the whole
answer) — lcsc results also carry lcsc, class, params, datasheet and a stock_note when stock is not live. `out_of_stock` lists
parts that carry the asked part number but have no assembly stock (never mixed into `results`).
Next: place with sch_apply (the result's `next` gives the exact op; it auto-imports symbol+footprint); parts_get for full detail.
Examples: parts_search(query='10k 0603') → C25804 Basic first; parts_search(query='USB-C 16P', limit=5); parts_search(query='10k 0603', provider='local')""",
      {"query": {"type": "string", "description": "value spec, MPN, or a description of the part"},
       "category": {"type": "string", "description": "optional kind/category filter: resistor, capacitor, led, diode, mosfet, ldo, crystal, usb_connector, header, mcu, module, … "
                                                      "or catalogue wording ('sensor', 'gas sensors', 'wifi module')"},
       "package": {"type": "string", "description": "optional package filter, e.g. '0603', 'SOT-23', 'SOIC-8'"},
       "basic_only": {"type": "boolean", "description": "only the provider's cheapest assembly class (JLC Basic)"},
       "in_stock": {"type": "boolean", "description": "only parts in stock (default: the config policy, true)"},
       "min_stock": {"type": "integer"},
       "limit": {"type": "integer", "description": "max results (default 8)"},
       "live": {"type": "boolean", "description": "query live supplier data (default true); false = offline data only"},
       "provider": PROVIDER},
      ["query"], title="Search parts", read_only=True, idempotent=True, open_world=True, core=True, group="parts")
def parts_search(query, category=None, package=None, basic_only=False, in_stock=None, min_stock=None, limit=8, live=True,
                 provider=None):
    PV, cfg, provs = _providers(None, provider)
    if in_stock is None:
        in_stock = bool(cfg.policy.get("require_in_stock", True))
    f = {"category": category, "package": package, "basic_only": basic_only, "in_stock": in_stock, "min_stock": min_stock}
    f = {k: v for k, v in f.items() if v not in (None, "")}
    limit = max(1, min(int(limit), 40))
    asked, r, prov, failed = [], None, None, {}
    for p in provs:
        asked.append(p.name)
        try:
            cur = PV.clean_results(p, p.search(query, f, limit=limit, live=live))
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 — a third-party provider failing must not hide the others
            failed[p.name] = f"{type(e).__name__}: {e}"
            continue
        if cur.get("dropped"):
            failed[p.name] = cur.pop("dropped")
        if r is None or cur.get("results"):
            r, prov = cur, p
        if cur.get("results"):
            break
    r = dict(r or {"results": []}, provider=prov.name if prov else None, providers_asked=asked)
    if failed:
        r["provider_errors"] = failed
    res = r["results"]
    oos = r.get("out_of_stock") or []
    if not res and oos:
        o = oos[0]
        oid = o.get("lcsc") or o.get("id")
        hint = (f"{oid} {o.get('mfr_part') or o.get('mpn') or ''} matches but has no assembly stock: "
                f"parts_alternatives(target='{oid}') for substitutes, describe the part instead of naming it "
                f"(e.g. parts_search(query='<what it is> sensor / module / connector')), or parts_search(query='{query}', "
                f"in_stock=false) to list it anyway (hand-solder or consign it)")
        return Result(dict(r, next=hint), summary=f"nothing in stock for '{query}' — {len(oos)} out-of-stock match(es), "
                                                  f"best {oid} {o.get('mfr_part') or o.get('mpn') or ''}".rstrip())
    if not res:
        hint = "drop the package/category filter, or search by MPN" if (package or category) else \
            "rephrase as a value spec ('10k 0603'), an MPN, or a plainer description ('gas sensor', 'wifi module'); " \
            "or parts_get(part=...) if you know the part number"
        if len(asked) == 1 and not provider:
            others = sorted(set(PV.available(cfg)) - set(asked))
            if others:
                hint += f"; other providers: {others} (provider=…)"
        what = "in-stock JLC parts" if asked == ["lcsc"] else f"parts in {asked}"
        return Result(dict(r, next=hint), summary=f"no {what} for '{query}'")
    top = res[0]
    if prov.name == "lcsc":
        summ = (f"{len(res)} result(s) for '{query}' — best: {top['lcsc']} {top.get('class')} {top.get('mfr_part')} "
                f"{top.get('package')} stock {top.get('stock')}")
        r["next"] = f"sch_apply ops=[{{\"op\":\"place\",\"lcsc\":\"{top['lcsc']}\"}}] or parts_get(lcsc='{top['lcsc']}') for full detail"
    else:
        summ = (f"{len(res)} result(s) for '{query}' from {prov.name} — best: {top['id']} "
                f"{top.get('mpn') or top.get('value') or ''} {top.get('package') or ''}".rstrip()
                + (f" stock {top['stock']}" if top.get("stock") is not None else ""))
        r["next"] = f"{_place_hint(prov, top['id'], top)} or parts_get(part='{top['id']}', provider='{prov.name}')"
    if top.get("match") and top.get("match") != "exact":
        # never let a partial answer read like a hit: say it in the one line a model is sure to read
        un = top.get("unconfirmed") or []
        summ += f" — {top['match'].upper()} match" + (f" (not confirmed: {', '.join(map(str, un))})" if un else "")
        tid = top.get("lcsc") or top.get("id")
        r["next"] = f"check it first: parts_get(part='{tid}') (datasheet), then " + r["next"].split(" or parts_get(")[0]
    return Result(r, summary=summ)


@tool("parts_get", """Full detail for one part by its provider part number. The provider is found from the id (a C-number is LCSC;
'name:id' names one) or `provider`.
lcsc: JLC **assembly** stock + Basic/Preferred/Extended class + price ladder + attrition (authoritative for JLC PCBA), LCSC **retail**
stock (a different pool — labelled), parameters, datasheet URL, whether EasyEDA has a symbol/footprint/3D model, and whether
parts_import would use a KiCad stock symbol+footprint or import from EasyEDA.
Other providers: the catalogue record (mpn, manufacturer, value, package, stock, symbol/footprint, extra supplier fields).
Use before committing to an unfamiliar part, or to check stock/class of a part already on the schematic.
Example: parts_get(part='C2040')""",
      {"part": {"type": "string", "description": "the provider's part number, e.g. 'C25804' (LCSC) or an MPN from your catalogue"},
       "lcsc": {"type": "string", "description": "shorthand for part=<C-number>, provider='lcsc' (digits alone are accepted)"},
       "provider": PROVIDER,
       "easyeda": {"type": "boolean", "description": "lcsc: also check EasyEDA symbol/footprint availability (default true)"}},
      [], title="Part detail", read_only=True, idempotent=True, open_world=True, group="parts")
def parts_get(part=None, lcsc=None, provider=None, easyeda=True):
    pid = str(part or lcsc or "").strip()
    if not pid:
        raise ToolError("pass part='<part number>'", code="BAD_ARGUMENT", fix="parts_get(part='C25804')")
    if lcsc and not part and not provider:
        provider = "lcsc"
    PV, cfg, prov, pid = _one(pid, provider)
    if prov.name == "lcsc":
        code = C.lcsc_norm(pid)
        if not code:
            raise ToolError(f"'{pid}' is not an LCSC part number", code="BAD_ARGUMENT", fix="pass e.g. part='C25804'")
        d = prov.detail(code, live=True, easyeda=easyeda)
        if not d.get("found"):
            raise ToolError(f"{code} not found at JLC or LCSC", code="PART_NOT_FOUND", data=d,
                            fix="check the number, or parts_search(query=<value or MPN>)")
        ja = d.get("jlc_assembly") or {}
        summ = (f"{code} {d.get('mfr_part')} ({d.get('package')}) — JLC {ja.get('class', 'not assemblable?')}, "
                f"assembly stock {ja.get('stock')}" + (f", LCSC retail {d['lcsc_retail']['stock']}" if d.get("lcsc_retail") else ""))
        return Result(d, summary=summ)
    d = PV.call(prov, "get", pid)
    if d and (not isinstance(d, dict) or not str(d.get("id") or d.get("mpn") or "").strip()):
        raise ToolError(f"parts provider '{prov.name}' returned a malformed record for '{pid}' (no id)", code="PROVIDER_FAILED",
                        fix="fix the provider's get(): it must return a part record with an 'id' (docs/PROVIDERS.md), or None")
    if not d:
        raise ToolError(f"'{pid}' not found in provider '{prov.name}'", code="PART_NOT_FOUND",
                        fix=f"parts_search(query=<value or MPN>, provider='{prov.name}')")
    prov.normalize(d)
    summ = (f"{d['id']} {d.get('mpn') or d.get('value') or ''} ({d.get('package') or 'no package'}) — {prov.name}"
            + (f", stock {d['stock']}" if d.get("stock") is not None else ""))
    return Result(d, summary=summ)


@tool("parts_import", """Make a provider's part usable in this project: returns the `lib_id` + `footprint` to use in sch_apply place
(sch_apply {"op":"place","lcsc":…} / {"op":"place","part":…} calls this automatically — use parts_import directly to inspect/preview
first, or to force a source).
lcsc — prefer='auto' (default): KiCad's own stock symbol whenever one passes a pin-by-pin check against the supplier's pin data
(chip R/C/L/LED/diodes; MPN/family names: AMS1117-3.3, LM358, STM32F103C8Tx; generics by pinout: Q_NMOS_GSD, Conn_01x06) — else
the converted LCSC/EasyEDA symbol, footprint and 3D model in project libraries libs/copper-lcsc.kicad_sym / .pretty / .3dshapes.
The result says which and why: symbol_source / footprint_source, `why`, `selection`. prefer='easyeda' forces the LCSC
drawing; prefer='stock' fails if no stock symbol fits. kicad-cli loads every converted symbol/footprint.
Other providers: the symbol + footprint the catalogue names, else KiCad's stock libraries (chip passives by package, pin headers,
symbols named after the MPN); a provider may also ship its own importer.
preview=true returns PNG renders of the symbol and footprint so you can check pin names/numbers and pad geometry.
Fields stamped on placement: the provider's part number field (LCSC for lcsc) plus MPN, Manufacturer, Datasheet, Description.
Example: parts_import(part='C165948') → copper-lcsc:TYPE-C-31-M-12_C165948 + copper-lcsc:USB-C_SMD-TYPE-C-31-M-12_1""",
      {"part": {"type": "string", "description": "the provider's part number"},
       "lcsc": {"type": "string", "description": "shorthand for part=<C-number>, provider='lcsc'"},
       "provider": PROVIDER, "project": PROJECT,
       "prefer": {"type": "string", "enum": ["auto", "stock", "easyeda"], "description": "lcsc only"},
       "with_3d": {"type": "boolean", "description": "download/convert the 3D model (default true)"},
       "preview": {"type": "boolean", "description": "return symbol/footprint images (default true)"}},
      [], title="Import part", idempotent=True, open_world=True, group="parts")
def parts_import(part=None, lcsc=None, provider=None, project=None, prefer="auto", with_3d=True, preview=True):
    from ..parts import importer
    pid = str(part or lcsc or "").strip()
    if not pid:
        raise ToolError("pass part='<part number>'", code="BAD_ARGUMENT", fix="parts_import(part='C165948')")
    if lcsc and not part and not provider:
        provider = "lcsc"
    pr = P.resolve(project)
    PV, cfg, prov, pid = _one(pid, provider, str(pr.pro))
    if prov.name == "lcsc":       # the EasyEDA route (and a stock symbol on a converted footprint) writes project library files
        try:
            P.checkpoint(pr, f"parts_import-{pid}"[:60])
        except OSError:
            pass
    if prov.name == "lcsc":
        r = prov.resolve(pr, pid, prefer=prefer, with_3d=with_3d, fresh=True)
    else:
        r = PV.call(prov, "resolve", pr, pid)
        if not isinstance(r, dict) or not r.get("lib_id"):
            raise ToolError(f"parts provider '{prov.name}' returned no symbol for part '{pid}'", code="NO_SYMBOL",
                            fix="place it with an explicit lib_id + footprint and set_field the part number")
        r.setdefault("source", "kicad-stock")
    r["provider"] = prov.name
    r["part_field"] = prov.part_field
    images = []
    converted = r.get("source") == "easyeda" or r.get("footprint_source") == "converted"
    if converted or preview:
        try:
            v = importer.verify_import(pr, r, render_dir=(pr.out_dir / "parts") if preview else None)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 — a preview must never fail an import that worked
            v = {"images": [], "ok": True, "errors": [f"preview unavailable: {e}"]}
        images = v["images"]
        if converted:
            r["kicad_load_check"] = {"ok": v["ok"], "errors": v["errors"]}
            if not v["ok"]:
                r.setdefault("warnings", []).append("KiCad could not load the generated library item — do not place it; report this")
        elif v["errors"]:
            r["preview_note"] = "; ".join(v["errors"])[:300]
    r["changed"] = r.pop("files_written", [])
    if str(r.get("stock_source") or "").startswith(("snapshot", "jlcsearch")):
        r["stock_note"] = f"stock from {r['stock_source']} (not live)"       # same label parts_search uses
    pid = r.get("lcsc") or r.get("id") or pid
    number = {prov.part_field: pid} if prov.part_field and prov.part_field not in ("MPN",) else {}
    fld = (",\"fields\":" + __import__("json").dumps(number, separators=(",", ":"))) if number else ""
    r["next"] = (f"sch_apply ops=[{{\"op\":\"place\",\"lib_id\":\"{r['lib_id']}\",\"footprint\":\"{r.get('footprint') or ''}\","
                 f"\"value\":\"{r.get('value') or ''}\"{fld}}}]  (or simply {_place_hint(prov, pid)[len('sch_apply ops=['):-1]})")
    warn = f" — {len(r['warnings'])} warning(s)" if r.get("warnings") else ""
    cls = f", JLC {r['class']}" if prov.name == "lcsc" and r.get("class") else (f", {r['class']}" if r.get("class") else "")
    how = r.get("source")
    if r.get("symbol_source"):
        how = (f"stock symbol + {r.get('footprint_source')} footprint" if r["symbol_source"] == "stock" else "converted from EasyEDA")
    summ = f"{pid} → {r['lib_id']} + {r.get('footprint') or '(no footprint)'} ({how}{cls}){warn}"
    return Result(r, images=images, summary=summ)


@tool("parts_source_bom", """BOM sourcing pass: give every schematic component (all sheets, incl. hierarchical/multi-instance) a part from
the project's parts providers. Run it once the schematic has Values + Footprints, before fab.
Default provider 'lcsc': every component gets an LCSC part JLC can assemble, Basic first. Per component without an LCSC field it
decides from, in order: an existing MPN field ('MPN', 'Manufacturer Part Number', …) → Value + Footprint package ('10k' on R_0402 →
Basic C25744; '100nF' on C_0603 → C14663; '1N4148' on D_DO-35 → a DO-35 part, never the SOD-123 1N4148W) → an MPN embedded in the
footprint name. The designator is a sanity check (a J is never given an IC). Through-hole passives get an in-stock THT part whose
value and body/pitch match the footprint; if none is confirmed the line is `manual` with options and the SMD footprint to switch to.
Existing LCSC fields are kept (overwrite=true re-picks) and checked: live stock, and LCSC package vs footprint.
Other providers ('local', 'kicad', third-party; several are asked in order): match by MPN field, then Value + package; a generic
provider ('kicad') accepts any part that has a Value and a Footprint.
Writes the provider's part-number field plus MPN / Manufacturer (write=false → report only).
Skips DNP, exclude_from_bom/board, mounting holes, logos, fiducials, test points (listed in summary.skipped).
Returns: summary (sourced %, per-provider counts; for lcsc unique parts by class, ≈ Extended setup fees, cost/board), `lines`
(decided; `sheet` = the sch_apply sheet argument for sub-sheet refs), `manual` (could not decide: reason + options) and `stock_risks`.
Example: parts_source_bom(write=false) to preview, then parts_source_bom() to apply.""",
      {"project": PROJECT, "write": {"type": "boolean", "description": "write fields into the schematic (default true)"},
       "basic_only": {"type": "boolean", "description": "only a provider's cheapest assembly class, e.g. JLC Basic (default: the config policy, false)"},
       "allow_extended": {"type": "boolean", "description": "allow them when nothing cheaper fits (default: the config policy, true)"},
       "overwrite": {"type": "boolean", "description": "re-pick parts that already have a part number (default false)"},
       "boards": {"type": "integer", "description": "build quantity for stock-risk maths (default 5)"},
       "live": {"type": "boolean", "description": "use live stock/search (default true; false = offline data only)"},
       "provider": PROVIDER,
       "while_open": {"type": "string", "enum": ["refuse", "revert"],
                      "description": "if a schematic sheet is open in KiCad: 'refuse' (default) or 'revert' (write anyway; then File ▸ Revert in KiCad)"}},
      title="Source BOM", idempotent=True, open_world=True, group="parts")
def parts_source_bom(project=None, write=True, basic_only=None, allow_extended=None, overwrite=False, boards=5, live=True,
                     provider=None, while_open="refuse"):
    from ..parts import sourcing
    pr = P.resolve(project)
    PV, cfg, provs = _providers(str(pr.pro), provider)
    cpol = cfg.policy
    pol = {"write": False, "will_write": bool(write),
           "basic_only": cpol.get("basic_only", False) if basic_only is None else basic_only,
           "allow_extended": cpol.get("allow_extended", True) if allow_extended is None else allow_extended,
           "min_stock": cpol.get("min_stock", 500), "require_in_stock": cpol.get("require_in_stock", True),
           "overwrite": overwrite, "boards": max(1, int(boards)), "live": live, "while_open": while_open}
    lcsc_only = len(provs) == 1 and provs[0].name == "lcsc"
    field = "LCSC" if lcsc_only else PV.part_field(cfg, provs)

    def run():
        if lcsc_only:
            return provs[0].source_bom(pr, pol)
        return sourcing.source_bom_generic(pr, provs, pol, field)

    r = run()                                     # decide first; checkpoint only if something will be written
    if write and r["written"].get("ops"):
        try:
            P.checkpoint(pr, "parts_source_bom")
        except OSError:
            pass
        pol["write"] = True
        r = run()                                 # cached lookups: fast; re-reads the schematic it writes to
    s = r["summary"]
    r["providers"] = [p.name for p in provs]
    r["part_field"] = field
    if lcsc_only:
        cls = s["unique_by_class"]
        summ = (f"BOM {s['locked_pct']}% LCSC-locked ({s['locked']}/{s['components']} parts), +{s['newly_assigned']} "
                f"{'assigned' if write else 'would be assigned (write=false)'}, {s['need_manual_choice']} need a choice · "
                f"unique LCSC: Basic {cls['Basic']} / Preferred {cls['Preferred']} / Extended {cls['Extended']} "
                f"(≈${s['extended_setup_fees_usd']:.0f} setup) · {s['stock_risks']} stock risk(s)")
    else:
        summ = (f"BOM {s['sourced_pct']}% sourced ({s['sourced']}/{s['components']} parts) via {', '.join(r['providers'])}: "
                f"{s['with_part_number']} with {field or 'a part number'}, {s['generic']} generic, +{s['newly_assigned']} "
                f"{'assigned' if write else 'would be assigned (write=false)'}, {s['need_manual_choice']} need a choice")
    if r["manual"]:
        m = r["manual"][0]
        o0 = (m.get("options") or [{}])[0]
        opt = o0.get("lcsc") or o0.get("id")
        sheet = f", sheet=\"{m['sheet']}\"" if m.get("sheet") else ""
        r["next"] = ("resolve each `manual` line: pick from its options (or parts_search(query=<value + package>)), then "
                     "sch_apply(ops=[{\"op\":\"set_field\",\"ref\":\"%s\",\"fields\":{\"%s\":\"%s\"}}]%s) (one op per ref; a line's "
                     "`sheet` is the sheet argument for refs on a sub-sheet); then re-run parts_source_bom"
                     % (m["refs"][0], field or "MPN", opt or "…", sheet))
    elif r.get("stock_risks"):
        r["next"] = r["stock_risks"][0].get("fix") or "parts_alternatives(target=<part number>)"
    elif not write and r["written"].get("ops"):
        r["next"] = "parts_source_bom() to write these fields"
    else:
        r["next"] = ("BOM is fully LCSC-locked" if lcsc_only else "every part is sourced") + " — run the verify/fab tools next"
    return Result(r, summary=summ)


@tool("parts_alternatives", """In-stock substitutes for a part — use when a part is out of stock, in a dear assembly class (JLC Extended:
~$3 setup fee), or too expensive. target: a part number (→ same package, same value / same MPN family; for lcsc Basic > Preferred >
Extended, then stock) or a free-text spec ('100nF 0402'). For ICs the alternatives are the same MPN family from other
makers/packagings — check the datasheet pinout before swapping. Apply with sch_apply set_field (the result's `next` gives the op).
Example: parts_alternatives(target='C15401')""",
      {"target": {"type": "string", "description": "part number (e.g. 'C15401') or spec ('100nF 0402')"},
       "lcsc": {"type": "string", "description": "alias of target for an LCSC C-number"},
       "limit": {"type": "integer", "description": "max results (default 8)"},
       "same_package": {"type": "boolean", "description": "default true"},
       "provider": PROVIDER},
      [], title="Part alternatives", read_only=True, idempotent=True, open_world=True, group="parts")
def parts_alternatives(target=None, lcsc=None, limit=8, same_package=True, provider=None):
    target = (target or lcsc or "").strip()
    if not target:
        raise ToolError("pass target='C15401' (a part number) or a spec like '100nF 0402'", code="BAD_ARGUMENT",
                        fix="parts_alternatives(target='C15401')")
    PV, cfg, provs = _providers(None, provider)
    pref, tid = PV.split_id(target, cfg)
    if pref and not provider:
        provs, target = [PV.get(pref, cfg)], tid
    prov = PV.owner_of(target, cfg, provs) or provs[0]
    limit = max(1, min(int(limit), 30))
    r = None
    for p in [prov] + [x for x in provs if x is not prov]:
        r = PV.call(p, "alternatives", target, limit=limit, same_package=same_package)
        if not isinstance(r, dict):
            r = {"target": target, "alternatives": []}
        r["alternatives"] = PV.clean_results(p, {"results": r.get("alternatives") or []})["results"]
        prov = p
        if r["alternatives"]:
            break
    r["provider"] = prov.name
    alts = r["alternatives"]
    if prov.name == "lcsc":
        summ = (f"{len(alts)} alternative(s) for {target}" + (f" — best {alts[0]['lcsc']} {alts[0].get('class')} "
                                                               f"{alts[0].get('mfr_part')} stock {alts[0].get('stock')}" if alts else ""))
    else:
        summ = f"{len(alts)} alternative(s) for {target} from {prov.name}" + (f" — best {alts[0]['id']}" if alts else "")
    if alts:
        a0 = alts[0]
        fields = {}
        if prov.part_field and prov.part_field != "MPN":
            fields[prov.part_field] = a0["id"]
        fields["MPN"] = a0.get("mpn") or ""
        r["next"] = ("sch_apply ops=[{\"op\":\"set_field\",\"ref\":\"<ref>\",\"fields\":%s}] for each ref using %s"
                     % (__import__("json").dumps(fields, separators=(",", ":")), r.get("target")))
    else:
        r["next"] = "widen: parts_alternatives(target=..., same_package=false) or parts_search(query=<spec>)"
    return Result(r, summary=summ)
