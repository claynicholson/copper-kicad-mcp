"""Provenance stamp: every project Copper edits carries the text variable COPPER_MCP = <Copper version> in its
.kicad_pro (project.stamp, called from registry.call_tool after a non-read-only tool changed a design file).

Run:  "C:/Program Files/KiCad/10.0/bin/python.exe" tests/test_provenance.py
The KiCad re-save test needs KiCad 10's pcbnew module (skipped without it).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
_TMP = Path(tempfile.mkdtemp(prefix="copper-test-provenance-"))
os.environ["COPPER_HOME"] = str(_TMP / "home")
from copper_kicad import __version__, project as projmod  # noqa: E402
from copper_kicad.host import route  # noqa: E402
from copper_kicad.mcp import registry  # noqa: E402
import copper_kicad.tools.project_tools  # noqa: E402,F401
import copper_kicad.tools.pcb_tools  # noqa: E402,F401
import copper_kicad.tools.sch_tools  # noqa: E402,F401

VAR = projmod.STAMP_VAR


class Skip(Exception):
    pass


def _vars(pro: Path) -> dict:
    return json.loads(pro.read_text(encoding="utf-8")).get("text_variables", {})


def _bare(name: str, pro_data: dict | None = None) -> projmod.Project:
    d = _TMP / name
    d.mkdir(parents=True, exist_ok=True)
    pro = d / f"{name}.kicad_pro"
    pro.write_text(json.dumps(pro_data if pro_data is not None else
                              {"meta": {"filename": pro.name, "version": 3}, "text_variables": {}}, indent=2) + "\n",
                   encoding="utf-8")
    return projmod.Project(d, name)


def test_stamp_writes_the_version_and_keeps_everything_else():
    pr = _bare("keep", {"meta": {"version": 3}, "board": {"design_settings": {"x": 0.127}},
                        "text_variables": {"REV": "B", "COPPER": "1oz"}})
    assert projmod.stamp(pr) is True
    d = json.loads(pr.pro.read_text(encoding="utf-8"))
    assert d["text_variables"] == {"REV": "B", "COPPER": "1oz", VAR: __version__}, d["text_variables"]
    assert d["board"] == {"design_settings": {"x": 0.127}} and d["meta"] == {"version": 3}
    before = pr.pro.read_bytes()
    assert projmod.stamp(pr) is False, "a second stamp with the same version writes nothing"
    assert pr.pro.read_bytes() == before


def test_stamp_adds_text_variables_when_missing_and_updates_an_old_version():
    pr = _bare("old", {"meta": {"version": 3}})
    assert projmod.stamp(pr) is True and _vars(pr.pro) == {VAR: __version__}
    d = json.loads(pr.pro.read_text(encoding="utf-8"))
    d["text_variables"][VAR] = "0.9.0"
    pr.pro.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
    assert projmod.stamp(pr) is True and _vars(pr.pro)[VAR] == __version__


def test_since_only_stamps_when_a_design_file_changed():
    pr = _bare("since")
    (pr.dir / "since.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    old = time.time() - 3600
    for f in (pr.pro, pr.dir / "since.kicad_sch"):
        os.utime(f, (old, old))
    assert projmod.stamp(pr, since=time.time()) is False and _vars(pr.pro) == {}
    started = time.time()
    (pr.dir / "since.kicad_sch").write_text("(kicad_sch )", encoding="utf-8")
    assert projmod.stamp(pr, since=started) is True and _vars(pr.pro) == {VAR: __version__}


def test_shadow_checkouts_and_unreadable_projects_are_left_alone():
    sh = _TMP / "proj" / ".copper" / "shadow" / "abc"
    sh.mkdir(parents=True)
    pro = sh / "s.kicad_pro"
    pro.write_text('{"text_variables": {}}\n', encoding="utf-8")
    assert projmod.stamp(projmod.Project(sh, "s")) is False and _vars(pro) == {}
    outside = _TMP / "elsewhere-shadow"
    outside.mkdir()
    (outside / "o.kicad_pro").write_text('{"text_variables": {}}\n', encoding="utf-8")
    route.note_shadow(outside, _TMP / "proj")
    assert projmod.stamp(projmod.Project(outside, "o")) is False
    bad = _bare("bad")
    bad.pro.write_text("{not json", encoding="utf-8")
    assert projmod.stamp(bad) is False and bad.pro.read_text(encoding="utf-8") == "{not json"
    assert projmod.stamp(projmod.Project(_TMP / "nope", "nope")) is False


def test_subfolder_sheets_and_project_libraries_count_as_edits():
    pr = _bare("sub")
    (pr.dir / "sch").mkdir()
    (pr.dir / "sub.kicad_sch").write_text(
        '(kicad_sch (sheet (property "Sheetname" "pwr") (property "Sheetfile" "sch/pwr.kicad_sch")))', encoding="utf-8")
    sheet = pr.dir / "sch" / "pwr.kicad_sch"
    sheet.write_text("(kicad_sch)", encoding="utf-8")
    old = time.time() - 3600
    for f in (pr.pro, pr.dir / "sub.kicad_sch", sheet):
        os.utime(f, (old, old))
    started = time.time()
    sheet.write_text("(kicad_sch )", encoding="utf-8")
    assert projmod.stamp(pr, since=started) is True, "an edit of a sub-sheet kept in a subfolder is an edit"
    lib = _bare("lib")
    os.utime(lib.pro, (old, old))
    (lib.dir / "libs").mkdir()
    started = time.time()
    (lib.dir / "libs" / "copper-lcsc.kicad_sym").write_text("(kicad_symbol_lib)", encoding="utf-8")
    assert projmod.stamp(lib, since=started) is True, "a part imported into the project's library is an edit"


def test_projects_open_in_kicad_are_not_written():
    pr = _bare("open")
    lk = pr.dir / "~open.kicad_pcb.lck"
    lk.write_text("{}", encoding="utf-8")
    assert projmod.stamp(pr) is False and _vars(pr.pro) == {}
    lk.unlink()
    assert projmod.stamp(pr) is True


def test_non_ascii_text_is_kept_as_written():
    pr = _bare("utf", {"meta": {"version": 3}, "text_variables": {"NOTE": "Ω µ ±5%"}})
    assert projmod.stamp(pr) is True
    raw = pr.pro.read_text(encoding="utf-8")
    assert "Ω µ ±5%" in raw and "\\u" not in raw, raw


def test_tools_that_never_edit_a_design_never_stamp():
    for name in ("focus", "export", "fab_export", "library"):
        assert name in registry._NO_STAMP and name in registry.TOOLS, name


def test_a_project_switch_during_a_call_still_stamps_the_project_it_started_on():
    a, b = _bare("racea"), _bare("raceb")
    old = time.time() - 3600
    for pr in (a, b):
        os.utime(pr.pro, (old, old))
    projmod.activate(b)                       # another call switched the active project meanwhile
    started = time.time()
    (a.dir / "racea.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    registry._stamp({}, started, before=a)
    assert _vars(a.pro) == {VAR: __version__}, "the project the call edited is stamped"
    assert _vars(b.pro) == {}, "the newly active project changed nothing during the call"


def _ok(r):
    assert not r.get("isError"), json.dumps(r)[:600]
    return r


def test_tool_calls_stamp_only_when_they_change_the_design():
    _ok(registry.call_tool("project_create", {"name": "tc", "dir": str(_TMP), "board_size": [30, 20]}))
    pro = _TMP / "tc" / "tc.kicad_pro"
    assert _vars(pro).get(VAR) == __version__, "project_create stamps"
    d = json.loads(pro.read_text(encoding="utf-8"))
    d["text_variables"] = {}
    pro.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
    _ok(registry.call_tool("pcb_read", {}))
    assert _vars(pro) == {}, "a read-only tool never stamps"
    r = registry.call_tool("sch_apply", {"ops": [{"op": "nonsense"}], "verify": False, "preview": False})
    assert r.get("isError") and _vars(pro) == {}, "a refused call never stamps"
    _ok(registry.call_tool("sch_apply", {"ops": [{"op": "place", "spec": "10k 0603"}], "verify": False,
                                         "preview": False}))
    assert _vars(pro) == {VAR: __version__}, "an edit stamps"


def test_kicad_keeps_the_variable_when_it_saves_the_project():
    try:
        import pcbnew  # noqa: PLC0415
    except ImportError:
        raise Skip("needs KiCad 10's pcbnew")
    pcb = _TMP / "tc" / "tc.kicad_pcb"
    if not pcb.exists():
        raise Skip("needs the project from test_tool_calls_stamp_only_when_they_change_the_design")
    b = pcbnew.LoadBoard(str(pcb))
    pcbnew.SaveBoard(str(pcb), b)
    sm = pcbnew.GetSettingsManager()
    if hasattr(sm, "SaveProject"):
        sm.SaveProject(str(pcb.with_suffix(".kicad_pro")))
    assert _vars(pcb.with_suffix(".kicad_pro")).get(VAR) == __version__


if __name__ == "__main__":
    fails = skips = 0
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"PASS {name} ({time.time() - t0:.1f} s)")
        except Skip as e:
            skips += 1
            print(f"SKIP {name}: {e}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {type(e).__name__}: {str(e)[:400]}")
    print(f"{len(tests) - fails - skips} passed, {fails} failed, {skips} skipped")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(1 if fails else 0)
