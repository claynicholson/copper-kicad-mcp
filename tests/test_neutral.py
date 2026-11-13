"""Neutrality tests: Copper is not locked to a parts vendor, a PCB fab, an MCP client or a model.

    python tests/test_neutral.py            (KiCad's Python or any Python 3.10+ with KiCad installed)

Covers the layered config (config.py), the parts-provider interface with its three built-ins and a third-party
provider loaded from a config path and from an entry point (docs/examples/example_provider.py is the provider
under test), the data-driven fab profiles and fab_export, and the wording the agent sees.
Everything runs in temp folders; the "design a board" test fails if anything touches the network.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

_TMP = Path(tempfile.mkdtemp(prefix="copper-test-neutral-"))
os.environ["COPPER_HOME"] = str(_TMP / "home0")          # project state (active project) lives here for the whole run
(_TMP / "home0").mkdir()

from copper_kicad import config  # noqa: E402
from copper_kicad import project as P  # noqa: E402
from copper_kicad.kicad import env  # noqa: E402
from copper_kicad.mcp import registry  # noqa: E402
from copper_kicad.mcp.registry import ToolError  # noqa: E402
from copper_kicad.parts import common as C  # noqa: E402
from copper_kicad.parts import providers as PV  # noqa: E402
from copper_kicad.review import design_review, fab, fab_profiles as FP, project_info  # noqa: E402
import copper_kicad.tools  # noqa: E402,F401  (registers the tools)

EXAMPLE_PROVIDER = ROOT / "docs" / "examples" / "example_provider.py"

CATALOGUE = """mpn,manufacturer,value,package,symbol,footprint,stock,Digi-Key,Bin
RC0603FR-07330RL,Yageo,330R,0603,,,2000,311-330HRCT-ND,A3
CL21A106KAYNNNE,Samsung,10uF,0805,,,750,1276-1096-1-ND,B1
AMS1117-3.3,Advanced Monolithic Systems,AMS1117-3.3,SOT-223,Regulator_Linear:AMS1117-3.3,Package_TO_SOT_SMD:SOT-223-3_TabPin2,40,,C7
LTST-C191KRKT,Lite-On,LED red,0603,,,300,160-1447-1-ND,A9
GONE-0603,Nobody,47k,0603,,,0,,Z0
"""

ACME = {
    "name": "acme", "label": "ACME Circuits", "short": "ACME", "extends": "generic",
    "notes": "Test profile; the numbers are invented.",
    "rules": {"standard": {"label": "ACME 8 mil", "min_track": 0.2, "min_clearance": 0.2, "edge_clearance": 0.4}},
    "gerber": {"x2": False, "protel_extensions": True, "job_file": False},
    "naming": {"zip": "ACME_{name}.zip", "bom": "ACME_{name}_bom.csv", "placement": "ACME_{name}_xy.csv"},
    "zip": {"folder": "{name}", "include_assembly": True},
    "bom": {"part_field": "Stock No", "part_label": "stockroom number",
            "columns": [["Line", "{item}"], ["Qty", "{qty}"], ["Stock No", "{part}"], ["MPN", "{mpn}"], ["Refs", "{refs}"],
                        ["Bin", "{field:Bin}"]]},
    "placement": {"format": "columns", "position": "anchor",
                  "columns": [["RefDes", "{ref}"], ["X_mm", "{x:.3f}"], ["Y_mm", "{y:.3f}"], ["Angle", "{rotation:.1f}"],
                              ["Side", "{layer}"]],
                  "layer_names": {"top": "T", "bottom": "B"}},
    "blocks": ["erc", "drc", "unrouted", "parity", "parts", "outline", "empty"],
}

_N = [0]


def new_home(cfg=None, files: dict | None = None) -> Path:
    """A fresh $COPPER_HOME for the config layer (config.py reads the variable on every call)."""
    _N[0] += 1
    h = _TMP / f"home{_N[0]}"
    h.mkdir()
    if cfg is not None:
        (h / "config.json").write_text(cfg if isinstance(cfg, str) else json.dumps(cfg, indent=1), encoding="utf-8")
    for rel, text in (files or {}).items():
        f = h / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text if isinstance(text, str) else json.dumps(text, indent=1), encoding="utf-8")
    os.environ["COPPER_HOME"] = str(h)
    _activate_clean()
    return h


_CLEAN: list = []


def _activate_clean():
    """Tools without a `project` argument read the ACTIVE project's config layer: start every test from a
    pristine active project so one test's project-level config never leaks into the next."""
    if not _CLEAN:
        r = project_info.create_project("clean-active", str(_TMP / "proj"), "blank")
        _CLEAN.append(P.resolve(r["project"]["dir"], activate=False))
    P.activate(_CLEAN[0])


def new_project(name: str, **kw) -> P.Project:
    r = project_info.create_project(name, str(_TMP / "proj"), kw.pop("template", None), kw.pop("board_size", [40, 25]), **kw)
    return P.resolve(r["project"]["dir"])


def demo(name: str) -> P.Project:
    dst = _TMP / "demos" / f"{name}-{_N[0]}"
    shutil.copytree(env.kicad().share / "demos" / name, dst)
    return P.resolve(str(dst), activate=False)


def call(tool: str, **args) -> dict:
    """structuredContent of a tool call made the way the server makes it; errors come back as {'error': …}."""
    return registry.call_tool(tool, args)["structuredContent"]


class no_network:
    """Any HTTP request inside the block is a test failure (not an 'offline' fallback)."""

    def __enter__(self):
        self.orig = C.http

        def boom(url, *a, **k):
            raise AssertionError(f"network access attempted: {url}")
        C.http = boom
        return self

    def __exit__(self, *exc):
        C.http = self.orig
        return False


def raises(code: str, fn, *a, **k) -> ToolError:
    try:
        fn(*a, **k)
    except ToolError as e:
        assert e.code == code, (e.code, e.message)
        return e
    raise AssertionError(f"expected ToolError {code}")


# ── config ───────────────────────────────────────────────────────────────
def test_config_layers_defaults_user_project_call():
    new_home()
    c = config.load(None)
    assert c.providers == ["lcsc"] and c.fab_profile == "jlcpcb" and c.sources == [], c.describe()   # zero-config default
    home = new_home({"parts": {"providers": ["local", "kicad"], "catalogue": "stock.csv", "policy": {"min_stock": 10}},
                     "fab": {"profile": "generic"}})
    c = config.load(None)
    assert c.providers == ["local", "kicad"] and c.fab_profile == "generic"
    assert c.policy["min_stock"] == 10 and c.policy["require_in_stock"] is True                       # objects merge key by key
    assert Path(c.provider_options("local")["catalogue"]) == home / "stock.csv"                       # relative to $COPPER_HOME
    pr = new_project("layers")
    assert config.load(pr).fab_profile == "generic"                                                   # user layer reaches projects
    config.set_project(pr, {"fab": {"profile": "pcbway"}, "parts": {"providers": ["kicad"], "catalogue": "mine.csv"}})
    c = config.load(pr)
    assert c.fab_profile == "pcbway" and c.providers == ["kicad"]                                     # project beats user
    assert Path(c.get("parts.catalogue")) == pr.dir / "mine.csv"                                      # relative to the project
    assert [Path(s).name for s in c.sources] == ["config.json", "config.json"] and str(pr.dir) in c.sources[1]
    c = config.load(pr, {"fab": {"profile": "oshpark"}, "parts": {"providers": None}})
    assert c.fab_profile == "oshpark" and c.providers == ["kicad"] and c.sources[-1] == "call arguments"   # call beats project
    assert config.load(None).fab_profile == "generic"                                                 # other projects untouched
    d = config.describe(pr)
    assert d["parts_providers"] == ["kicad"] and d["fab_profile"] == "pcbway" and len(d["config_files"]) == 2, d


def test_bad_config_is_a_tool_error_with_file_and_line_never_a_crash():
    home = new_home('{\n  "parts": {"providers": ["local"],\n  "fab": {"profile": "generic"}\n')            # unbalanced brace
    e = raises("BAD_CONFIG", config.load, None)
    assert str(home / "config.json") in e.message and e.data["file"].endswith("config.json") and e.data["line"] >= 3, e.message
    assert e.fix and "JSON" in e.fix
    new_home('{\n  "fab": {"profile": "generic"},\n  "parts": {"catalogue": "C:\\parts\\stock.csv"}\n}')     # Windows path, single \
    e = raises("BAD_CONFIG", config.load, None)
    assert e.data["line"] == 3 and "backslash" in e.fix, (e.message, e.fix)
    new_home('{\n "parts": {\n  "providers": "local"\n }\n}')                                                # wrong type
    e = raises("BAD_CONFIG", config.load, None)
    assert e.data["line"] == 3 and "parts.providers" in e.message and '["local", "kicad"]' in e.fix, (e.message, e.fix)
    new_home('{\n "fabs": {"profile": "generic"}\n}')                                                        # typo'd key
    e = raises("BAD_CONFIG", config.load, None)
    assert e.data["line"] == 2 and "unknown key 'fabs'" in e.message and "fab" in e.fix, (e.message, e.fix)
    # tools answer with the error and its fix; status / doctor keep working and show it
    out = registry.call_tool("parts_search", {"query": "10k 0603"})
    assert out["isError"] and out["structuredContent"]["error"] == "BAD_CONFIG" and out["structuredContent"]["fix"], out
    st = project_info.status()
    assert st["config"]["error"] == "BAD_CONFIG" and "line 2" in st["config"]["message"], st["config"]
    from copper_kicad.install import doctor
    chk = doctor.check_config()
    assert chk.status == "fail" and "fabs" in chk.detail, chk
    new_home({"fab": {"profile": "nope"}})                                                                   # valid file, unknown name
    e = raises("UNKNOWN_FAB_PROFILE", FP.active, None)
    assert "jlcpcb" in e.fix and "generic" in e.fix, e.fix
    new_home({"parts": {"providers": ["nope"]}})
    e = raises("UNKNOWN_PROVIDER", lambda: PV.active(config.load(None)))
    assert "local" in e.fix and "copper_kicad.parts" in e.fix, e.fix
    new_home()


def test_a_project_file_cannot_load_provider_code_unless_the_user_allows_it():
    new_home()
    pr = new_project("untrusted")
    (pr.dir / "evil.py").write_text("raise SystemExit('project code ran')\n", encoding="utf-8")
    config.project_file(pr).parent.mkdir(exist_ok=True)
    config.project_file(pr).write_text(json.dumps({"parts": {"providers": ["lcsc"], "provider_paths": ["evil.py"],
                                                             "allow_project_providers": True}}), encoding="utf-8")
    c = config.load(pr)
    assert c.get("parts.provider_paths") == [] and c.warnings and "ignored" in c.warnings[0], (c.data["parts"], c.warnings)
    assert "warnings" in config.describe(pr) and set(PV.available(c)) == {"lcsc", "local", "kicad"}
    new_home({"parts": {"allow_project_providers": True}})                 # the USER says yes → the path is honoured
    c = config.load(pr)
    assert [Path(p) for p in c.get("parts.provider_paths")] == [pr.dir / "evil.py"] and not c.warnings
    new_home()


# ── providers ────────────────────────────────────────────────────────────
def test_local_provider_reads_a_catalogue_file_offline():
    home = new_home({"parts": {"providers": ["local"], "catalogue": "stock.csv"}}, {"stock.csv": CATALOGUE})
    with no_network():
        cfg = config.load(None)
        (loc,) = PV.active(cfg)
        assert loc.name == "local" and loc.part_field == "MPN" and loc.status()["parts"] == 5
        r = loc.search("330R 0603", {"in_stock": True})["results"]
        assert [x["id"] for x in r] == ["RC0603FR-07330RL"] and r[0]["match"] == "exact" and r[0]["provider"] == "local", r
        assert r[0]["fields"] == {"Digi-Key": "311-330HRCT-ND", "Bin": "A3"} and r[0]["manufacturer"] == "Yageo"
        assert loc.search("330R 0402", {})["results"] == []                              # wrong package is not a match
        assert loc.search("47k 0603", {"in_stock": True})["results"] == []               # stock 0 is skipped…
        assert loc.search("47k 0603", {"in_stock": False})["results"][0]["id"] == "GONE-0603"   # …unless asked
        assert loc.pick("LED red 0603")["mpn"] == "LTST-C191KRKT" and loc.pick("LED green 0603") is None
        assert loc.get("ams1117-3.3")["lib_id"] == "Regulator_Linear:AMS1117-3.3" and loc.owns("AMS1117-3.3") and not loc.owns("C25804")
        f = loc.schematic_fields(loc.get("RC0603FR-07330RL"))
        assert f["MPN"] == "RC0603FR-07330RL" and f["Manufacturer"] == "Yageo" and f["Digi-Key"] == "311-330HRCT-ND", f
        # JSON catalogue, a different part-number field
        (home / "stock.json").write_text(json.dumps({"parts": [
            {"id": "BIN-7", "mpn": "X1", "value": "1k", "package": "0402", "fields": {"Mouser": "71-X1"}}]}), encoding="utf-8")
        new = config.load(None, {"parts": {"catalogue": str(home / "stock.json"), "options": {"local": {"part_field": "Mouser"}}}})
        (loc2,) = PV.active(new)
        assert loc2.part_field == "Mouser" and loc2.pick("1k 0402")["id"] == "BIN-7"
    # broken files are named with their line
    (home / "stock.csv").write_text("mpn,value,package,stock\nA,10k,0603,lots\n", encoding="utf-8")
    e = raises("BAD_CATALOGUE", loc.rows)
    assert e.data["line"] == 2 and "stock" in e.message, e.message
    (home / "stock.csv").write_text("name,qty\nfoo,1\n", encoding="utf-8")
    e = raises("BAD_CATALOGUE", loc.rows)
    assert "header" in e.message and "mpn" in e.fix, (e.message, e.fix)
    new_home({"parts": {"providers": ["local"]}})
    e = raises("NO_CATALOGUE", lambda: PV.active(config.load(None))[0].search("10k"))
    assert "parts.csv" in e.fix, e.fix
    new_home()


def test_kicad_provider_gives_generic_parts_with_no_supplier():
    new_home({"parts": {"providers": ["kicad"]}})
    with no_network():
        (k,) = PV.active(config.load(None))
        assert k.part_field is None and k.generic and PV.part_field(config.load(None)) is None
        for spec, lib, fp, val in (("10k 0603", "Device:R", "Resistor_SMD:R_0603_1608Metric", "10k"),
                                   ("100nF 0402", "Device:C", "Capacitor_SMD:C_0402_1005Metric", "100nF"),
                                   ("LED red 0805", "Device:LED", "LED_SMD:LED_0805_2012Metric", "LED red"),
                                   ("Header 1x4 2.54mm", "Connector_Generic:Conn_01x04",
                                    "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical", "Conn_01x04"),
                                   ("Header 2x5 female", "Connector_Generic:Conn_02x05_Odd_Even",
                                    "Connector_PinSocket_2.54mm:PinSocket_2x05_P2.54mm_Vertical", "Conn_02x05")):
            p = k.pick(spec)
            assert p and (p["lib_id"], p["footprint"], p["value"]) == (lib, fp, val), (spec, p)
            assert p["mpn"] == "" and p["provider"] == "kicad"
        ic = k.pick("AMS1117-3.3")
        assert ic["lib_id"] == "Regulator_Linear:AMS1117-3.3" and ic["footprint"].startswith("Package_TO_SOT_SMD:SOT-223"), ic
        assert k.pick("definitely-not-a-part-xyz") is None
        s = call("parts_search", query="10k 0603")
        assert s["provider"] == "kicad" and len(s["results"]) == 1 and '"lib_id":"Device:R"' in s["next"] and '"value":"10k"' in s["next"], s
    new_home()


def test_third_party_provider_from_a_config_path_is_the_documented_example():
    assert EXAMPLE_PROVIDER.exists(), "docs/PROVIDERS.md points at this file"
    assert "example_provider.py" in (ROOT / "docs" / "PROVIDERS.md").read_text(encoding="utf-8")
    new_home({"parts": {"providers": ["stockroom", "kicad"], "provider_paths": [str(EXAMPLE_PROVIDER)],
                        "options": {"stockroom": {"min_stock": 1}}}})
    with no_network():
        cfg = config.load(None)
        assert PV.available(cfg)["stockroom"] == str(EXAMPLE_PROVIDER)
        sr, k = PV.active(cfg)
        assert (sr.name, sr.part_field, k.name) == ("stockroom", "Stock No", "kicad") and sr.options == {"min_stock": 1}
        assert sr.pick("10k 0603")["id"] == "SR-0001" and sr.owns("sr-0003") and sr.get("SR-0003")["mpn"] == "AP2112K-3.3TRG1"
        # through the tools, exactly as an agent calls them
        s = call("parts_search", query="100nF 0603")
        assert s["provider"] == "stockroom" and s["results"][0]["id"] == "SR-0002" and "stockroom" in s["next"], s
        assert call("parts_search", query="LED red 0603")["provider"] == "kicad"          # stock 0 there → next provider answers
        g = call("parts_get", part="SR-0003")
        assert g["provider"] == "stockroom" and g["footprint"] == "Package_TO_SOT_SMD:SOT-23-5", g
        assert call("parts_search", query="10k 0603", provider="kicad")["results"][0]["id"] == "Device:R"   # per-call override
        a = call("parts_alternatives", target="100nF 0603")
        assert a["provider"] == "stockroom" and a["alternatives"][0]["id"] == "SR-0002", a
        pr = new_project("stockroomed")
        i = call("parts_import", part="SR-0001", project=str(pr.dir), preview=False)
        assert i["provider"] == "stockroom" and i["lib_id"] == "Device:R" and i["footprint"] == "Resistor_SMD:R_0603_1608Metric", i
        assert i["fields"]["Stock No"] == "SR-0001" and i["fields"]["MPN"] == "RC0603FR-0710KL" and i["fields"]["Shelf"] == "A1", i
        from copper_kicad.sch import ops
        r = ops.apply_ops(pr, [{"op": "place", "part": "SR-0003", "id": "u", "at": [100.33, 60.96]},
                               {"op": "place", "spec": "10k 0603", "id": "r", "at": [130.81, 60.96]},
                               {"op": "place", "part": "stockroom:SR-0002", "id": "c", "at": [152.4, 60.96]}])
        placed = [c for c in r["changed"] if c.startswith("placed")]
        assert any("Stock No SR-0003" in c for c in placed) and any("Stock No SR-0001" in c for c in placed), placed
        assert not [c for c in placed if "LCSC" in c or "JLC" in c], placed
    # a provider that cannot load is an error with its line, not a crash
    bad = _TMP / "broken_provider.py"
    bad.write_text("x = 1\nimport does_not_exist_anywhere\n", encoding="utf-8")
    new_home({"parts": {"providers": ["broken"], "provider_paths": [str(bad)]}})
    out = registry.call_tool("parts_search", {"query": "10k"})
    e = out["structuredContent"]
    assert out["isError"] and e["error"] == "PROVIDER_LOAD_FAILED" and e.get("line") == 2 and "PROVIDERS.md" in e["fix"], e
    empty = _TMP / "empty_provider.py"
    empty.write_text("class NotAProvider:\n    pass\n", encoding="utf-8")
    new_home({"parts": {"providers": ["x"], "provider_paths": [str(empty)]}})
    e = raises("PROVIDER_LOAD_FAILED", lambda: PV.active(config.load(None)))
    assert "defines no provider" in e.message and "search" in e.fix, (e.message, e.fix)
    assert project_info.status()["config"]["parts_providers"] == ["x"]                    # status still answers
    new_home()


def test_third_party_provider_from_an_entry_point():
    """An installed package advertises a provider in the entry-point group 'copper_kicad.parts'."""
    site = _TMP / "site"
    site.mkdir()
    (site / "acme_parts.py").write_text(
        "class Acme:\n"                                   # duck-typed on purpose: no Provider base class
        "    name = 'acmeparts'\n    label = 'ACME parts'\n    part_field = 'ACME PN'\n"
        "    def search(self, query, filters=None, limit=8, live=True):\n"
        "        return {'results': [{'id': 'A-1', 'mpn': 'ZZ1', 'value': '10k', 'package': '0603', 'kind': 'resistor'}]\n"
        "                if '10k' in query else []}\n"
        "    def get(self, part_id, live=True):\n"
        "        return {'id': 'A-1', 'mpn': 'ZZ1', 'value': '10k', 'package': '0603', 'kind': 'resistor'} if part_id == 'A-1' else None\n",
        encoding="utf-8")
    di = site / "acme_parts-1.0.dist-info"
    di.mkdir()
    (di / "METADATA").write_text("Metadata-Version: 2.1\nName: acme-parts\nVersion: 1.0\n", encoding="utf-8")
    (di / "entry_points.txt").write_text(f"[{PV.ENTRY_POINT_GROUP}]\nacmeparts = acme_parts:Acme\n", encoding="utf-8")
    sys.path.insert(0, str(site))
    PV._EP_CACHE = None
    try:
        new_home({"parts": {"providers": ["acmeparts"]}})
        cfg = config.load(None)
        assert "entry point" in PV.available(cfg)["acmeparts"]
        (p,) = PV.active(cfg)
        assert p.name == "acmeparts" and p.part_field == "ACME PN" and isinstance(p, PV.Provider)   # adapted, defaults filled in
        assert p.pick("10k 0603")["id"] == "A-1" and p.pick("1k") is None
        pr = new_project("entrypoint")
        r = PV.resolve_part(pr, "A-1", "acmeparts")                                                # default resolve(): KiCad stock fit
        assert r["lib_id"] == "Device:R" and r["fields"] == {"ACME PN": "A-1", "MPN": "ZZ1"} and r["part_field"] == "ACME PN", r
        assert call("parts_search", query="10k 0603")["provider"] == "acmeparts"
    finally:
        sys.path.remove(str(site))
        PV._EP_CACHE = None
        new_home()


def test_lcsc_parts_always_carry_the_vendor_neutral_fields():
    new_home()
    old = os.environ.get("COPPER_OFFLINE")
    os.environ["COPPER_OFFLINE"] = "1"                                   # offline table only: no network needed
    try:
        cfg = config.load(None)
        (lc,) = PV.active(cfg)
        assert lc.name == "lcsc" and lc.part_field == "LCSC" and lc.assembly_classes[0] == "Basic"
        assert PV.part_field(cfg) == "LCSC" and lc.owns("C25804") and not lc.owns("RC0603")
        pr = new_project("neutralfields")
        r = PV.resolve_part(pr, "C25804")
        f = r["fields"]
        assert r["provider"] == "lcsc" and f["LCSC"] == "C25804" and f["MPN"] and f["Manufacturer"], f
        from copper_kicad.sch import ops
        from copper_kicad.sch.netlist import export_netlist
        ops.apply_ops(pr, [{"op": "place", "spec": "10k 0603", "id": "r", "at": [100.33, 60.96]}])
        fields = export_netlist(pr)["components"]["R1"]["fields"]
        assert fields.get("LCSC") == "C25804" and fields.get("MPN") and fields.get("Manufacturer"), fields
        # …so the same design can be re-sourced: under a generic fab profile the BOM identifies the part by MPN
        assert PV.part_number(fields, "MPN") == fields["MPN"] and PV.part_number(fields, "LCSC") == "C25804"
        assert PV.part_number({"Manufacturer Part Number": "X"}, "MPN") == "X" and PV.part_number({"lcsc part #": "c5"}, "LCSC") == ""
    finally:
        if old is None:
            os.environ.pop("COPPER_OFFLINE", None)
        else:
            os.environ["COPPER_OFFLINE"] = old


def test_design_a_board_with_local_and_kicad_providers_without_any_network():
    """The whole parts path of a small board (regulator + LED + header) with providers [local, kicad] and the
    generic fab profile. Any HTTP request fails the test."""
    new_home({"parts": {"providers": ["local", "kicad"], "catalogue": "stock.csv"}, "fab": {"profile": "generic"}},
             {"stock.csv": CATALOGUE})
    from copper_kicad.sch import ops
    from copper_kicad.sch.netlist import export_netlist
    with no_network():
        pr = new_project("regled")
        assert config.load(pr).fab_profile == "generic"
        r = ops.apply_ops(pr, [
            {"op": "place", "spec": "AMS1117-3.3", "id": "u"},
            {"op": "place", "spec": "10uF 0805", "id": "cin"}, {"op": "place", "spec": "10uF 0805", "id": "cout"},
            {"op": "place", "spec": "330R 0603", "id": "r"}, {"op": "place", "spec": "LED red 0603", "id": "d"},
            {"op": "place", "spec": "Header 1x2 2.54mm", "id": "j"},
            {"op": "place", "spec": "1k 0603", "id": "r2"},                       # not in the catalogue → kicad generic
            {"op": "connect", "pins": ["j.1", "u.VI", "cin.1"], "net": "VIN"},
            {"op": "connect", "pins": ["j.2", "u.GND", "cin.2", "cout.2", "d.K", "r2.2"], "net": "GND"},
            {"op": "connect", "pins": ["u.VO", "cout.1", "r.1", "r2.1"], "net": "+3V3"},
            {"op": "connect", "pins": ["r.2", "d.A"], "net": "LED_A"},
            {"op": "pwr_flag", "net": "VIN"}, {"op": "pwr_flag", "net": "GND"}])
        assert not [w for w in r.get("warnings", []) if "generic" in w or "LCSC" in w], r.get("warnings")
        comps = export_netlist(pr)["components"]
        u, r1, j, r2 = (comps[r["refs"][k]] for k in ("u", "r", "j", "r2"))
        assert u["fields"]["MPN"] == "AMS1117-3.3" and u["footprint"] == "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
        assert r1["fields"]["MPN"] == "RC0603FR-07330RL" and r1["fields"]["Digi-Key"] == "311-330HRCT-ND" and r1["value"] == "330R"
        assert j["lib_id"] == "Connector_Generic:Conn_01x02" and "MPN" not in j["fields"]
        assert r2["lib_id"] == "Device:R" and r2["value"] == "1k" and "MPN" not in r2["fields"]
        assert not any("LCSC" in c["fields"] or "JLC Class" in c["fields"] for c in comps.values())
        b = call("parts_source_bom", project=str(pr.dir))
        s = b["summary"]
        assert b["providers"] == ["local", "kicad"] and b["part_field"] == "MPN", b
        assert (s["components"], s["sourced"], s["with_part_number"], s["generic"], s["need_manual_choice"]) == (7, 7, 5, 2, 0), s
        # a part placed by hand gets its number from the catalogue (value + footprint package), written with neutral fields
        ops.apply_ops(pr, [{"op": "place", "lib_id": "Device:C", "value": "10uF", "footprint": "Capacitor_SMD:C_0805_2012Metric",
                            "id": "c3", "at": [200.66, 100.33]}, {"op": "no_connect", "pins": ["c3.1", "c3.2"]}])
        b = call("parts_source_bom", project=str(pr.dir))
        assert b["summary"]["newly_assigned"] == 1 and b["written"]["fields_changed"] >= 1, (b["summary"], b["written"])
        c3 = export_netlist(pr)["components"]["C3"]["fields"]
        assert c3["MPN"] == "CL21A106KAYNNNE" and c3["Manufacturer"] == "Samsung" and c3["Digi-Key"] == "1276-1096-1-ND", c3
        # nothing the agent reads names the default vendor
        dr = design_review.review(pr, checks=["sourcing"], include_erc=False)
        assert dr["stats"]["part_field"] == "MPN" and dr["stats"]["sourced_pct"] == 100.0 and "basic" not in dr["stats"], dr["stats"]
        assert not [f for f in dr["findings"] if f["type"].startswith("missing_")], dr["findings"]
        sm = project_info.summary(pr)
        assert sm["config"]["parts_providers"] == ["local", "kicad"] and sm["config"]["fab_profile"] == "generic"
        assert sm["part_field"] == "MPN" and sm["sourced_pct"] == 100.0, sm
        text = json.dumps([b, sm["next"], dr["findings"], call("parts_search", query="330R 0603")], ensure_ascii=False)
        assert not re.search(r"LCSC|JLC|EasyEDA", text), re.findall(r".{40}(?:LCSC|JLC|EasyEDA).{40}", text)[:3]
    new_home()


# ── fab profiles ─────────────────────────────────────────────────────────
def test_builtin_fab_profiles_are_valid_and_say_what_was_not_verified():
    new_home()
    assert FP.names() == ["generic", "jlcpcb", "oshpark", "pcbway"], FP.names()
    for n in FP.names():
        p = FP.load(n)
        assert p["name"] == n and p["label"] and p["_scope"] == "built-in"
        assert "UNVERIFIED" in p["notes"], f"{n}: the notes must say which numbers were not confirmed"
        r = FP.rules(p)
        assert 0.05 <= r["min_track"] <= 0.3 and r["via"][0] > r["via"][1] >= r["min_via_drill"] - 1e-9 and r["min_via_diameter"] <= r["via"][0]
        assert set(p["blocks"]) <= set(FP.BLOCK_CODES) and {"erc", "drc", "unrouted", "outline", "empty"} <= set(p["blocks"])
        d = FP.describe(p)
        assert d["name"] == n and d["min_track_mm"] == p["rules"]["standard"]["min_track"]
    for n in ("jlcpcb", "pcbway", "oshpark"):
        assert "2026" in FP.load(n)["notes"], n                         # the date the fab's page was checked
    assert "parts" in FP.load("jlcpcb")["blocks"] and FP.load("jlcpcb")["bom"]["part_field"] == "LCSC"
    assert "parts" not in FP.load("generic")["blocks"] and FP.load("generic")["bom"]["part_field"] is None
    assert FP.load("generic")["gerber"]["x2"] and not FP.load("generic")["gerber"]["protel_extensions"]
    assert not FP.load("oshpark")["assembly"]["offered"] and not FP.load("oshpark")["drill"]["separate_th"]
    assert FP.rules(FP.load("oshpark"), layers=2)["min_track"] == 0.1524 and FP.rules(FP.load("oshpark"), layers=4)["min_track"] == 0.127
    assert FP.rotation_table_path(FP.load("jlcpcb")).name == "jlc_rotations.json" and FP.rotation_table_path(FP.load("pcbway")) is None
    lst = call("fab_export", profile="list")
    assert lst["active"] == "jlcpcb" and [p["name"] for p in lst["profiles"]] == FP.names() and "FAB_PROFILES.md" in lst["add_your_own"]
    assert all(p["notes"] for p in lst["profiles"])


def test_jlcpcb_profile_reproduces_the_previous_hard_coded_rules():
    """The default must behave exactly as before the rules became data."""
    new_home()
    from copper_kicad.pcb import rules
    legacy = {"jlc": {"min_track": 0.127, "min_clearance": 0.127, "min_via_diameter": 0.6, "min_via_drill": 0.3, "min_annular": 0.13,
                      "min_hole": 0.3, "hole_to_hole": 0.5, "edge_clearance": 0.3, "hole_clearance": 0.2, "silk_clearance": 0.0,
                      "min_text_height": 0.8, "min_text_thickness": 0.15, "via": (0.6, 0.3)},
              "jlc_advanced": {"min_track": 0.09, "min_clearance": 0.09, "min_via_diameter": 0.45, "min_via_drill": 0.2,
                               "min_annular": 0.1, "min_hole": 0.2, "hole_to_hole": 0.5, "edge_clearance": 0.3,
                               "hole_clearance": 0.2, "silk_clearance": 0.0, "min_text_height": 0.8, "min_text_thickness": 0.15,
                               "via": (0.45, 0.2)}}
    for name, want in legacy.items():
        got = rules.PRESETS[name]
        assert {k: got[k] for k in want} == want, (name, {k: (got[k], v) for k, v in want.items() if got[k] != v})
    assert rules.PRESETS["jlc"]["label"] == "JLCPCB standard (no surcharge)" and rules.PRESETS["jlc"]["power_via"] == (0.8, 0.4)
    assert rules.PRESETS["jlc_advanced"]["power_via"] == (0.6, 0.3)
    assert rules.PRESETS["relaxed"]["min_track"] == 0.2 and "nope" not in rules.PRESETS and rules.PRESETS.get("nope") is None
    assert rules.default_preset() == "jlc" and {"fab", "jlc", "jlc_advanced", "relaxed", "pcbway", "oshpark", "generic"} <= set(rules.preset_names())
    dru = rules.dru_rules("jlc")
    assert '(rule "Copper jlc: track width"' in dru and "(constraint track_width (min 0.127mm))" in dru and "annular_width (min 0.15mm)" in dru
    e = raises("BAD_ARGUMENT", rules.dru_rules, "nope")
    assert "fab" in e.fix and "pcbway" in e.fix
    new_home({"fab": {"profile": "pcbway"}})
    assert rules.default_preset() == "pcbway" and rules.preset_rules("fab")["min_track"] == 0.15
    assert '(rule "Copper pcbway: track width"' in rules.dru_rules("pcbway")
    assert rules.preset_rules("pcbway_advanced")["min_track"] == 0.1
    new_home()


def test_custom_fab_profiles_from_the_user_and_project_folders():
    home = new_home(None, {"fabs/acme.json": ACME})
    assert "acme" in FP.names() and FP.load("acme")["_scope"] == "user"
    p = FP.load("acme")
    assert p["rules"]["standard"]["min_track"] == 0.2 and p["rules"]["standard"]["min_via_drill"] == 0.3     # inherited from generic
    assert p["gerber"]["protel_extensions"] and not p["gerber"]["x2"] and p["drill"]["separate_th"]         # merged with the parent
    assert p["bom"]["part_field"] == "Stock No" and "parts" in p["blocks"] and p["label"] == "ACME Circuits"
    pr = new_project("fabscope")
    d = pr.dir / ".copper" / "fabs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "acme.json").write_text(json.dumps(dict(ACME, label="ACME (project copy)")), encoding="utf-8")
    (d / "jlcpcb.json").write_text(json.dumps({"name": "jlcpcb", "extends": "generic", "label": "my JLC"}), encoding="utf-8")
    assert FP.load("acme", pr)["label"] == "ACME (project copy)" and FP.load("acme", pr)["_scope"] == "project"    # nearest wins
    assert FP.load("jlcpcb", pr)["label"] == "my JLC" and FP.load("jlcpcb")["label"] == "JLCPCB"                 # a project may shadow a built-in
    # mistakes: file, line, fix — never a traceback
    (home / "fabs" / "broken.json").write_text('{\n "name": "broken",\n "rules": {"standard": {"min_track": "thin"}}\n}', encoding="utf-8")
    e = raises("BAD_FAB_PROFILE", FP.load, "broken")
    assert e.data["line"] == 3 and "min_track" in e.message and "broken.json" in e.message, e.message
    (home / "fabs" / "broken.json").write_text('{\n "name": "broken",\n "blocks": ["erc", "typo"]\n}', encoding="utf-8")
    e = raises("BAD_FAB_PROFILE", FP.load, "broken")
    assert e.data["line"] == 3 and "blocks" in e.message, e.message
    (home / "fabs" / "broken.json").write_text('{\n "name": "broken"\n "label": "x"\n}', encoding="utf-8")
    e = raises("BAD_FAB_PROFILE", FP.load, "broken")
    assert e.data["line"] == 3, e.message
    (home / "fabs" / "broken.json").write_text('{"name": "broken", "extends": "missing"}', encoding="utf-8")
    e = raises("BAD_FAB_PROFILE", FP.load, "broken")
    assert "extends" in e.message and "generic" in e.fix
    lst = call("fab_export", profile="list")                         # the listing survives a broken file and names it
    assert "acme" in [p["name"] for p in lst["profiles"]] and lst["broken"][0]["name"] == "broken", lst.get("broken")
    e = raises("UNKNOWN_FAB_PROFILE", FP.load, "nope")
    assert "acme" in e.fix and "FAB_PROFILES.md" in e.fix
    new_home()


def test_fab_export_follows_the_profile_on_a_real_board():
    new_home(None, {"fabs/acme.json": ACME})
    pr = demo("pic_programmer")                                       # a routed KiCad demo, no supplier numbers at all
    out = _TMP / "fabout"

    def export(profile, **kw):
        return fab.fab_export(pr, profile=profile, out_dir=str(out / (profile or "default")), run_checks=False, **kw)

    def header(path):
        with open(path, newline="", encoding="utf-8") as f:
            return next(csv.reader(f))

    # generic: Gerber X2 with .gbr names + job file, Excellon PTH/NPTH, KiCad's own position file, plain BOM; no part gate
    g = export("generic")
    names = zipfile.ZipFile(g["zip"]).namelist()
    assert Path(g["zip"]).name == "pic_programmer-fab.zip" and g["profile"] == "generic" and g["fab"] == "Generic (any fab)"
    assert len([n for n in names if n.endswith(".gbr")]) == 9 and any(n.endswith(".gbrjob") for n in names), names
    assert {n for n in names if n.endswith(".drl")} == {"pic_programmer-PTH.drl", "pic_programmer-NPTH.drl"}, names
    x2 = [f.name for f in Path(g["gerber_dir"]).glob("*.gbr")
          if "%TF.FileFunction,Copper,L1,Top*%" in f.read_text(encoding="ascii", errors="ignore")]
    assert len(x2) == 1, x2                                                                       # X2 attributes
    assert header(g["bom"]) == ["Refs", "Qty", "Value", "Footprint", "MPN", "Manufacturer", "Supplier Part #"]
    assert header(g["placement"]) == ["Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side"] and g["placement_source"] == "kicad-cli pos"
    assert g["rotation_corrections"] == [] and g["placements"] > 40 and g["part_field"] == "LCSC"     # the project's field (default provider)
    assert g["waived"] == [] and g["advisories"][0]["code"] == "parts" and g["summary"]["unsourced_parts"] == g["summary"]["assembled_parts"]
    assert "extended_parts" not in g["summary"] and not re.search(r"JLC", json.dumps(g["summary"]["order_notes"]))
    # pcbway: Protel names, no X2, its BOM columns, MPN as the part number
    w = export("pcbway")
    names = zipfile.ZipFile(w["zip"]).namelist()
    assert Path(w["zip"]).name == "pic_programmer-PCBWay.zip" and {Path(n).suffix for n in names} >= {".gtl", ".gbl", ".gm1", ".drl"}, names
    (gtl,) = Path(w["gerber_dir"]).glob("*.gtl")
    assert "%TF." not in gtl.read_text(encoding="ascii", errors="ignore")
    assert header(w["bom"])[:5] == ["Item #", "Designator", "Qty", "Manufacturer", "Mfg Part #"] and w["part_field"] == "MPN"
    assert Path(w["bom"]).name == "pic_programmer-BOM-PCBWay.csv" and Path(w["placement"]).name == "pic_programmer-centroid-PCBWay.csv"
    # oshpark: bare boards (no BOM unless asked), one merged drill file, 4-layer rules differ
    o = export("oshpark")
    names = zipfile.ZipFile(o["zip"]).namelist()
    assert "bom" not in o and [n for n in names if n.endswith(".drl")] == ["pic_programmer.drl"], (sorted(o), names)
    o2 = export("oshpark", assembly=True)
    assert Path(o2["bom"]).exists() and "does not assemble" in o2["summary"]["order_notes"][0]
    # a user's own profile: its part field gates the order, its names, columns and zip layout are used
    e = raises("FAB_BLOCKED", export, "acme")
    assert e.data["profile"] == "acme" and e.data["blockers"][0]["code"] == "parts" and "stockroom number" in e.message, e.message
    a = export("acme", waive=["parts"])
    names = zipfile.ZipFile(a["zip"]).namelist()
    assert Path(a["zip"]).name == "ACME_pic_programmer.zip" and all(n.startswith("pic_programmer/") for n in names), names
    assert {"pic_programmer/ACME_pic_programmer_bom.csv", "pic_programmer/ACME_pic_programmer_xy.csv"} <= set(names)
    assert header(a["bom"]) == ["Line", "Qty", "Stock No", "MPN", "Refs", "Bin"]
    assert header(a["placement"]) == ["RefDes", "X_mm", "Y_mm", "Angle", "Side"]
    rows = list(csv.DictReader(open(a["placement"], newline="", encoding="utf-8")))
    assert {r["Side"] for r in rows} <= {"T", "B"} and re.fullmatch(r"-?\d+\.\d{3}", rows[0]["X_mm"]) and re.fullmatch(r"\d+\.\d", rows[0]["Angle"])
    assert [w["code"] for w in a["waived"]] == ["parts"]
    # the default is still the JLCPCB pack, and the tool reports the profile it used
    j = export(None, waive=["parts"])
    assert j["profile"] == "jlcpcb" and Path(j["zip"]).name == "pic_programmer-JLCPCB.zip"
    assert header(j["bom"]) == ["Comment", "Designator", "Footprint", "LCSC Part #"]
    assert header(j["placement"]) == ["Designator", "Mid X", "Mid Y", "Layer", "Rotation"]
    t = registry.call_tool("fab_export", {"project": str(pr.dir), "profile": "generic", "waive": ["all"], "out_dir": str(out / "tool")})
    assert not t["isError"] and t["content"][0]["text"].startswith("Generic (any fab) pack for pic_programmer ready"), t["content"][0]["text"][:200]
    assert raises("BAD_ARGUMENT", export, "generic", waive=["lcsc"])           # the old vendor-named waive code is gone
    new_home()


def test_project_create_and_design_rules_derive_from_the_fab_profile():
    new_home()
    from copper_kicad.pcb import rules

    def pro(pr):
        return json.loads(pr.pro.read_text(encoding="utf-8"))["board"]["design_settings"]["rules"]
    a = project_info.create_project("default-a", str(_TMP / "proj"), None, [30, 20])
    b = project_info.create_project("named-b", str(_TMP / "proj"), "jlc-2layer", [30, 20])
    assert a["template"] == b["template"] == "jlc-2layer" and a["fab"] == b["fab"] == "jlcpcb" and a["rules"] == "JLCPCB standard (no surcharge)"
    pa, pb = P.resolve(a["project"]["dir"], activate=False), P.resolve(b["project"]["dir"], activate=False)
    ja, jb = (json.loads(p.pro.read_text(encoding="utf-8")) for p in (pa, pb))
    ja["meta"]["filename"] = jb["meta"]["filename"] = ""
    assert ja == jb and pro(pa)["min_track_width"] == 0.127                              # the old template name = the default
    assert not config.project_file(pa).exists()                                        # nothing to remember: it is the default
    assert (pa.dir / "default-a.kicad_dru").read_text(encoding="utf-8").count("JLC:") >= 3
    o = project_info.create_project("osh4", str(_TMP / "proj"), "4layer", [30, 20], fab="oshpark")
    po = P.resolve(o["project"]["dir"], activate=False)
    assert o["fab"] == "oshpark" and o["template"] == "4layer" and "stackup_note" in o and o["kicad_check"]["ok"], o
    r = pro(po)
    assert (r["min_track_width"], r["min_clearance"], r["min_copper_edge_clearance"], r["min_via_annular_width"]) == (0.127, 0.127, 0.381, 0.1016), r
    assert json.loads(config.project_file(po).read_text(encoding="utf-8")) == {"fab": {"profile": "oshpark"}}
    dru = (po.dir / "osh4.kicad_dru").read_text(encoding="utf-8")
    assert '(rule "Copper oshpark: track width"' in dru and "JLC" not in dru, dru[:300]
    assert rules.default_preset(po) == "oshpark" and FP.active(po)["name"] == "oshpark" and FP.active(pa)["name"] == "jlcpcb"
    new_home({"fab": {"profile": "generic"}})                                           # the user's default fab reaches new projects
    g = project_info.create_project("gen2", str(_TMP / "proj"), None, [30, 20])
    pg = P.resolve(g["project"]["dir"], activate=False)
    assert g["fab"] == "generic" and g["template"] == "2layer" and pro(pg)["min_track_width"] == 0.15
    assert not config.project_file(pg).exists()
    j = project_info.create_project("jlc-explicit", str(_TMP / "proj"), "jlc-2layer", [30, 20])   # the old name still means JLCPCB
    pj = P.resolve(j["project"]["dir"], activate=False)
    assert j["fab"] == "jlcpcb" and json.loads(config.project_file(pj).read_text(encoding="utf-8"))["fab"]["profile"] == "jlcpcb"
    e = raises("UNKNOWN_FAB_PROFILE", project_info.create_project, "nofab", str(_TMP / "proj"), None, None, fab="nope")
    assert not (_TMP / "proj" / "nofab").exists()                                       # refused before anything was written
    t = registry.TOOLS["project_create"]
    assert "fab" in t.schema["properties"] and {"2layer", "4layer", "jlc-2layer", "jlc-4layer", "blank"} == set(t.schema["properties"]["template"]["enum"])
    new_home()


def test_status_and_summary_show_the_active_providers_and_profile():
    new_home({"parts": {"providers": ["local", "kicad"], "catalogue": "stock.csv"}, "fab": {"profile": "pcbway"}},
             {"stock.csv": CATALOGUE})
    pr = new_project("statusproj")
    P.activate(pr)
    st = call("copper_status")
    assert st["config"]["parts_providers"] == ["local", "kicad"] and st["config"]["fab_profile"] == "pcbway", st["config"]
    line = registry.call_tool("copper_status", {})["content"][0]["text"].splitlines()[0]
    assert "parts: local+kicad" in line and "fab: pcbway" in line, line
    sm = call("project_summary", project=str(pr.dir))
    assert sm["config"]["fab_profile"] == "pcbway" and sm["config"]["config_files"], sm["config"]
    from copper_kicad.install import doctor
    c = doctor.check_config()
    assert c.status == "ok" and "local, kicad" in c.detail and "pcbway" in c.detail, c
    net = doctor.check_network(timeout=0.1)
    assert [x.status for x in net] == ["skip"] and "nothing to probe" in net[0].detail, net       # no LCSC probes without the lcsc provider
    d = PV.describe(pr)
    assert d["active"] == ["local", "kicad"] and d["part_field"] == "MPN" and d["providers"][0]["parts"] == 5, d
    new_home()
    c = call("copper_status")["config"]
    assert (c["parts_providers"], c["fab_profile"], c["config_files"]) == (["lcsc"], "jlcpcb", []), c


# ── what the agent reads ─────────────────────────────────────────────────
_PRODUCTS = re.compile(r"\b(Claude|Codex|Cursor|ChatGPT|GPT-?\d\w*|Gemini|Copilot|Anthropic|OpenAI|Windsurf|claude|codex|chatgpt|openai)\b")


def _agent_facing_texts() -> dict:
    from copper_kicad import resources, skills
    from copper_kicad.mcp import server
    texts = {"server instructions": server.instructions() if hasattr(server, "instructions") else server.INSTRUCTIONS,
             "guide": resources.GUIDE}
    for name, t in registry.TOOLS.items():
        d = t.describe()
        texts[f"tool {name}"] = json.dumps({k: v for k, v in d.items() if k != "_meta"}, ensure_ascii=False) + "\n" + t.description
        texts[f"help {name}"] = registry.help_text(name)
    for name, p in resources.PROMPTS.items():
        texts[f"prompt {name}"] = json.dumps(p) + resources._prompt_text(name, {"requirements": "x"})
    for r in resources.RESOURCES + resources.TEMPLATES:
        texts[f"resource {r['name']}"] = json.dumps(r)
    for s in skills.list_skills():
        texts[f"skill {s['name']}"] = s["description"] + "\n" + s["body"]
    return texts


def test_nothing_the_agent_reads_names_a_model_or_a_client():
    """The server never calls an LLM and must not assume one. Product names belong in the install/registration
    code and docs only (install/, link/, cli.py, plugin/, the README's install section)."""
    new_home()
    bad = {k: sorted(set(m.group(0) for m in _PRODUCTS.finditer(v))) for k, v in _agent_facing_texts().items() if _PRODUCTS.search(v)}
    assert not bad, bad
    # source: outside the registration code only the namespaced `_meta` hint may carry a vendor namespace
    hits = []
    for f in sorted((SRC / "copper_kicad").rglob("*.py")):
        rel = f.relative_to(SRC / "copper_kicad").as_posix()
        if rel.startswith(("install/", "link/")) or rel == "cli.py":
            continue
        if rel in ("agent/drivers/anthropic_api.py", "agent/drivers/openai_compat.py", "agent/drivers/claude_code.py", "agent/drivers/codex.py"):
            continue                                    # one provider's / one CLI's adapter each; their kinds stay neutral
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if _PRODUCTS.search(line) and "anthropic/alwaysLoad" not in line:
                hits.append(f"{rel}:{i}: {line.strip()[:100]}")
    assert not hits, hits
    # the `_meta` hint is documented as harmless, and only ever sits in `_meta`
    assert "anthropic/alwaysLoad" in (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for t in registry.TOOLS.values():
        d = t.describe()
        assert set(d) <= {"name", "title", "description", "inputSchema", "annotations", "_meta"}, set(d)
        assert set(d.get("_meta") or {}) <= {"anthropic/alwaysLoad"}, d.get("_meta")


def test_the_tool_surface_is_vendor_neutral():
    new_home()
    assert "fab_jlc" not in registry.TOOLS and "fab_export" in registry.TOOLS
    fx = registry.TOOLS["fab_export"]
    assert "profile" in fx.schema["properties"] and "parts" in fx.schema["properties"]["waive"]["items"]["enum"]
    assert "lcsc" not in fx.schema["properties"]["waive"]["items"]["enum"]
    for name in ("parts_search", "parts_get", "parts_import", "parts_source_bom", "parts_alternatives"):
        t = registry.TOOLS[name]
        assert "provider" in t.schema["properties"] and not re.search(r"LCSC|JLC", t.title or ""), (name, t.title)
    assert {"part", "lcsc", "provider"} <= set(registry.TOOLS["parts_get"].schema["properties"])
    from copper_kicad import resources
    from copper_kicad.sch import ops
    assert set(resources.PROMPTS) == {"design_board", "source_bom", "review_design", "release_to_fab"}
    assert {"part", "provider", "lcsc", "spec", "lib_id"} <= ops.OP_FIELDS["place"] and {"part", "provider"} <= ops.OP_FIELDS["swap"]
    # prompts follow the configuration instead of assuming the default vendor
    assert "JLCPCB" in resources._prompt_text("release_to_fab", {}) and "LCSC" in resources._prompt_text("source_bom", {})
    P.activate(new_project("promptproj"))
    new_home({"parts": {"providers": ["kicad"]}, "fab": {"profile": "generic"}})
    for name in resources.PROMPTS:
        txt = resources._prompt_text(name, {"requirements": "x"})
        assert not re.search(r"LCSC|JLC|EasyEDA", txt), (name, re.findall(r".{30}(?:LCSC|JLC).{30}", txt)[:2])
    assert "Generic (any fab)" in resources._prompt_text("release_to_fab", {})
    assert "PCBWay" in resources._prompt_text("release_to_fab", {"profile": "pcbway"})
    new_home()
    from copper_kicad import skills
    assert {s["name"] for s in skills.list_skills()} == {"kicad-design", "parts-sourcing", "design-review", "fab-release"}
    assert {p.name for p in (ROOT / "plugin" / "skills").iterdir()} == {s["name"] for s in skills.list_skills()}
    for d in ("CONFIG.md", "PROVIDERS.md", "FAB_PROFILES.md"):
        assert (ROOT / "docs" / d).exists(), d


# Files under src/copper_kicad that may name the default vendor, and why. Anything else must use a lookup
# (config.py, parts/providers, review/fab_profiles.py) instead of a vendor name.
VENDOR_FILES = {
    # the default parts provider and its engine
    "parts/providers/lcsc.py": "the lcsc provider", "parts/providers/__init__.py": "built-in registry + LCSC field aliases",
    "parts/providers/local.py": "docs: LCSC as an example supplier column", "parts/basic.py": "lcsc engine: offline Basic table",
    "parts/common.py": "lcsc engine: HTTP hosts, C-number parsing, JLC classes", "parts/detail.py": "lcsc engine: part detail",
    "parts/easyeda.py": "lcsc engine: EasyEDA converter", "parts/importer.py": "lcsc engine: symbol/footprint import",
    "parts/search.py": "lcsc engine: JLC/jlcsearch search", "parts/seed_build.py": "lcsc engine: builds the offline table",
    "parts/sourcing.py": "lcsc engine: LCSC BOM sourcing (source_bom_generic is the neutral pass)",
    # the default fab profile
    "data/fabs/jlcpcb.json": "the jlcpcb profile", "data/jlc_rotations.json": "the jlcpcb profile's rotation table",
    "data/templates/jlc.kicad_dru": "the jlcpcb profile's extra DRC rules", "review/fab_profiles.py": "names the default profile",
    "review/fab.py": "docstrings describing the default profile's output",
    "pcb/rules.py": "docs: the default profile's 'jlc' preset aliases", "review/project_info.py": "jlc-2layer/jlc-4layer template names",
    # defaults quoted in help text, hints and field-name fallbacks (each resolves through the config first)
    "config.py": "DEFAULTS + docs", "resources.py": "guide/prompts: wording for the default setup",
    "review/common.py": "lcsc_of(): the LCSC field parser used by the lcsc provider and the jlcpcb profile",
    "review/design_review.py": "JLC class/fee check, active only for profiles with part_classes; ESD example part",
    "review/verify.py": "_DEFAULT_FAB: hint numbers when no profile can be read",
    "sch/ops.py": "'lcsc' op key = shorthand for the default provider; LCSC as default part field",
    "sch/model.py": "shows LCSC/MPN fields in component listings", "pcb/workers/board.py": "copies LCSC/MPN fields to footprints",
    "pcb/route.py": "comments citing JLC numbers as examples", "kicad/libs.py": "a comment",
    "install/doctor.py": "network probes of the lcsc provider (skipped when it is not active)",
    "tools/parts_tools.py": "describes the default provider", "tools/fab_tools.py": "describes the default profile",
    "tools/project_tools.py": "jlc-* template names", "tools/pcb_tools.py": "'jlc' preset names; LCSC/MPN field display",
    "tools/sch_tools.py": "the 'lcsc' op key and field examples", "tools/verify_tools.py": "help text for the default setup",
    "mcp/catalogue.py": "slim tool texts (op keys, default preset name)", "mcp/compact.py": "digest: the (JLC Basic) class suffix of a placed line",
    "mcp/server.py": "one line: the default part-number field",
    "skills/kicad-design/SKILL.md": "the default setup, labelled as default", "skills/design-review/SKILL.md": "same",
    "skills/parts-sourcing/SKILL.md": "a section on the default provider", "skills/fab-release/SKILL.md": "the default profile's files",
}


def test_vendor_names_only_where_the_default_provider_and_profile_live():
    rx = re.compile(r"jlc|lcsc|easyeda", re.I)
    found = set()
    for f in (SRC / "copper_kicad").rglob("*"):
        if f.is_file() and f.suffix in (".py", ".json", ".md", ".kicad_dru", ".html", ".js", ".css") and "__pycache__" not in f.parts:
            if rx.search(f.read_text(encoding="utf-8", errors="ignore")):
                found.add(f.relative_to(SRC / "copper_kicad").as_posix())
    stale = sorted(set(VENDOR_FILES) - found)
    assert not stale, f"{stale} no longer name a vendor: remove them from VENDOR_FILES"
    extra = sorted(found - set(VENDOR_FILES))
    assert not extra, (f"{extra} name a parts vendor / fab. Use a lookup (config, parts.providers, fab_profiles), or add the "
                       f"file to VENDOR_FILES with the reason it belongs to the default provider/profile.")
    # the neutral core must stay free of them
    for rel in ("parts/providers/base.py", "parts/providers/kicad.py", "parts/providers/kicad_fit.py",
                "data/fabs/generic.json", "data/fabs/pcbway.json", "data/fabs/oshpark.json"):
        assert rel not in found, rel


# ── review regressions: things that parsed but could not work, and provider code that misbehaves ──────────
BOOM_PROVIDER = """
class Boom:
    name = "boom"
    def search(self, query, filters=None, limit=8, live=True):
        raise RuntimeError("API key missing")
    def get(self, part_id, live=True):
        raise KeyError(part_id)

class Weird:
    name = "weird"
    def search(self, query, filters=None, limit=8, live=True):
        return {"results": [{"mpn": None}, "junk"]}
    def get(self, part_id, live=True):
        return {"nothing": 1}
    def alternatives(self, target, **kw):
        return ["not", "a", "dict"]
"""


def test_config_that_parses_but_cannot_work_is_reported_not_papered_over():
    """An unknown provider / unknown fab profile / missing catalogue used to show as a normal status line, and
    project_summary silently fell back to 'LCSC 0.0% → parts_source_bom()' for a user who never chose LCSC."""
    from copper_kicad.install import doctor
    for cfg, word in (({"parts": {"providers": ["digikey", "kicad"]}}, "unknown parts provider 'digikey'"),
                      ({"parts": {"providers": ["kicad"]}, "fab": {"profile": "seeed"}}, "unknown fab profile 'seeed'"),
                      ({"parts": {"providers": ["local", "kicad"], "catalogue": "nope.csv"}}, "nope.csv"),
                      ('{"parts": {"providers": ["kicad"],}}', "bad Copper config")):
        new_home(cfg)
        pr = _CLEAN[0]
        d = config.describe(pr)
        msg = d.get("message") or d["problems"][0]["message"]
        assert word in msg, (cfg, d)
        st = registry.call_tool("copper_status", {})
        line, sc = st["content"][0]["text"].splitlines()[0], st["structuredContent"]
        assert sc["next"].startswith("fix the Copper config first") and word in sc["next"], sc["next"]
        assert "CONFIG " in line and line.count(word) == 1, line          # said once, not twice
        sm = registry.call_tool("project_summary", {"project": str(pr.dir)})
        text = json.dumps(sm["structuredContent"], ensure_ascii=False) + sm["content"][0]["text"]
        assert sm["structuredContent"]["next"][0].startswith("fix the Copper config first"), sm["structuredContent"]["next"]
        assert "sourced_pct" not in sm["structuredContent"] and "LCSC" not in text, text[:400]
        assert doctor.check_config().status in ("warn", "fail"), doctor.check_config()
        if word == "nope.csv":
            continue          # providers and profile resolve; only the catalogue file is missing
        dr = design_review.review(pr, checks=["sourcing"], include_erc=False)
        assert [f["type"] for f in dr["findings"]] == ["config_problem"] and "LCSC" not in json.dumps(dr), dr
    # a healthy config has no problems entry at all
    new_home({"parts": {"providers": ["local", "kicad"], "catalogue": "stock.csv"}, "fab": {"profile": "generic"}},
             {"stock.csv": CATALOGUE})
    assert "problems" not in config.describe(_CLEAN[0]) and "problems" not in call("copper_status")["config"]
    new_home()
    assert "problems" not in call("copper_status")["config"]


def test_ignored_project_provider_code_is_the_first_problem_shown():
    h = new_home({"parts": {"providers": ["lcsc"]}}, {"prov/boom.py": BOOM_PROVIDER})
    pr = new_project("foreign")
    (pr.dir / ".copper").mkdir(exist_ok=True)
    (pr.dir / ".copper" / "config.json").write_text(json.dumps(
        {"parts": {"providers": ["boom", "kicad"], "provider_paths": [str(h / "prov" / "boom.py")]}}), encoding="utf-8")
    d = config.describe(pr)
    assert "provider_paths ignored" in d["problems"][0]["message"], d
    assert "unknown parts provider 'boom'" in d["problems"][1]["message"], d
    new_home()


def test_a_misbehaving_third_party_provider_is_named_never_an_internal_error():
    """Provider code is not ours: an exception or a malformed record must come back as PROVIDER_FAILED (or be
    skipped with a note), never as INTERNAL 'this is a Copper bug'."""
    new_home({"parts": {"providers": ["weird", "boom", "kicad"], "provider_paths": ["prov/boom.py"]}},
             {"prov/boom.py": BOOM_PROVIDER})
    with no_network():
        r = call("parts_search", query="10uF 0805")
        assert r["provider"] == "kicad" and r["providers_asked"] == ["weird", "boom", "kicad"], r
        assert "malformed" in r["provider_errors"]["weird"] and "API key missing" in r["provider_errors"]["boom"], r["provider_errors"]
        for tool, args in (("parts_get", {"part": "X1", "provider": "boom"}), ("parts_get", {"part": "X1", "provider": "weird"}),
                           ("parts_alternatives", {"target": "X1", "provider": "boom"}),
                           ("parts_import", {"part": "X1", "provider": "boom", "project": str(_CLEAN[0].dir), "preview": False}),
                           ("sch_apply", {"ops": [{"op": "place", "part": "X1", "provider": "boom"}], "dry_run": True}),
                           ("sch_apply", {"ops": [{"op": "place", "part": "X1", "provider": "weird"}], "dry_run": True})):
            e = call(tool, **args)
            assert e.get("error") in ("PROVIDER_FAILED", "PART_NOT_FOUND", "NO_SYMBOL"), (tool, args, e)
            assert "Copper bug" not in json.dumps(e), e
        a = call("parts_alternatives", target="X1", provider="weird")
        assert a.get("alternatives") == [] and "error" not in a, a
        # a spec still gets placed by the next provider, with the failure in the warnings
        d = call("sch_apply", ops=[{"op": "place", "spec": "1uF 0603"}], dry_run=True)
        assert "error" not in d and any("boom" in w and "API key missing" in w for w in d["warnings"]), d
    new_home()


def test_generic_search_hint_keeps_value_and_footprint():
    """parts_search's `next` for a generic KiCad part used to be place part='Device:C', which drops the value
    and the footprint the search had just worked out."""
    new_home({"parts": {"providers": ["kicad"]}, "fab": {"profile": "generic"}})
    with no_network():
        r = call("parts_search", query="100nF 0402")
    op = json.loads(re.search(r"sch_apply ops=\[(\{.*?\})\]", r["next"]).group(1))
    assert op == {"op": "place", "lib_id": "Device:C", "footprint": "Capacitor_SMD:C_0402_1005Metric", "value": "100nF"}, r["next"]
    d = call("sch_apply", ops=[op], dry_run=True)
    assert "error" not in d and "100nF" in d["changed"][0] and not d.get("warnings"), d
    new_home()


def test_catalogue_in_a_folder_with_spaces_and_umlauts_and_an_ansi_spreadsheet_export():
    h = new_home({"parts": {"providers": ["local"], "catalogue": "Stock Lists/tëile.csv"}})
    f = h / "Stock Lists" / "tëile.csv"
    f.parent.mkdir()
    # what a spreadsheet's plain "CSV" export writes on a German PC: semicolons, the ANSI code page
    f.write_bytes("Mfr Part #;Manufacturer;Value;Package;Stock\r\nWE-744;Würth Elektronik;10uH;0805;12\r\n".encode("cp1252"))
    with no_network():
        r = call("parts_search", query="WE-744")
    assert r["results"][0]["manufacturer"] == "Würth Elektronik" and r["results"][0]["stock"] == 12, r
    f.write_text("foo,bar\n1,2\n", encoding="utf-8")
    e = call("parts_search", query="WE-744")
    assert e["error"] == "BAD_CATALOGUE" and e["line"] == 1 and "mpn / value / id" in e["message"], e
    new_home()


def test_unsourced_parts_advisory_does_not_offer_a_waive_when_nothing_blocks():
    part = [{"ref": "J1", "part": ""}]
    g = fab.gate(None, set(), part, {}, run_checks=False, profile=FP.load("generic"))
    assert not g["blockers"] and g["advisories"][0]["code"] == "parts" and "waive" not in g["advisories"][0]["fix"], g
    g = fab.gate(None, set(), part, {}, run_checks=False, profile=FP.load("jlcpcb"), part_label="LCSC number")
    assert g["blockers"][0]["code"] == "parts" and "waive=['parts']" in g["blockers"][0]["fix"], g


def test_placed_digest_and_tool_plumbing_carry_no_vendor_column_or_dead_tool():
    from copper_kicad.mcp import catalogue, compact
    lines = ["placed R1 Device:R 10k at (1.0, 2.0) LCSC C25804 (JLC Basic)",
             "placed C1 Device:C 10uF at (1, 2) rot 90 MPN CL21A106KAYNNNE",
             "placed U1 unit 1 Amplifier_Operational:LM358 LM358 at (3, 4) Mfr Part # LM358DR (other sheet instance(s): U2)",
             "placed J1 Connector_Generic:Conn_01x02 Conn_01x02 at (3, 4)"] + ["R1.1 → VIN"] * 6
    d = compact.digest_changed(lines)
    assert d["parts"]["cols"] == ["ref", "value", "part", "class"], d["parts"]["cols"]
    assert d["parts"]["rows"] == [["R1", "10k", "C25804", "B"], ["C1", "10uF", "CL21A106KAYNNNE", None],
                                  ["U1", "LM358", "LM358DR", None], ["J1", "Conn_01x02", None, None]], d["parts"]["rows"]
    dead = "fab_" + "jlc"
    hits = [f.relative_to(ROOT).as_posix() for base in (SRC, ROOT / "tests" / "budget", ROOT / "docs", ROOT / "plugin")
            for f in base.rglob("*") if f.is_file() and f.suffix in (".py", ".json", ".md")
            and dead in f.read_text(encoding="utf-8", errors="ignore")]
    assert not hits, hits
    assert "'jlc' rule preset" not in catalogue.SLIM["pcb_setup"][1]


if __name__ == "__main__":
    passed = failed = 0
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    only = sys.argv[1:]
    for name, fn in tests:
        if only and not any(o in name for o in only):
            continue
        try:
            fn()
            passed += 1
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {name}: {type(e).__name__}: {str(e)[:600]}")
            traceback.print_exc(limit=4)
        finally:
            os.environ["COPPER_HOME"] = str(_TMP / "home0")
    print(f"\n{passed}/{passed + failed} passed  (scratch: {_TMP})")
    sys.exit(1 if failed else 0)
