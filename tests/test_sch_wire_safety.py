"""sch_apply must never join two nets the batch did not ask to join — run against the real KiCad 10 install.

    python -m pytest tests/test_sch_wire_safety.py      (pytest optional)
    python tests/test_sch_wire_safety.py                (plain runner)

Covers the pin-to-pin `wire` router (it used to run through a neighbour's label stub and get a junction there),
the net-merge and net-split checks that refuse such a batch before anything is written, `label` on a pin that
already has a wire (it used to fail with NO_ROOM) and `move` carrying such a label along. Every connectivity claim is checked with KiCad's own netlist.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("COPPER_HOME", tempfile.mkdtemp(prefix="copper-home-"))

from copper_kicad import sexpr  # noqa: E402
from copper_kicad.mcp.registry import ToolError  # noqa: E402
from copper_kicad.project import Project  # noqa: E402
from copper_kicad.sch import netlist as N  # noqa: E402
from copper_kicad.sch import ops as O  # noqa: E402
from copper_kicad.sch.model import Schematic  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="copper-wire-safety-"))


def _project(name: str) -> Project:
    d = TMP / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return Project(d, name)


def _nets(pr) -> dict:
    """{(ref, pin): short net name}"""
    return {k: N.short_name(v) for k, v in N.pin_nets(N.export_netlist(pr)).items()}


def _err(fn) -> ToolError:
    try:
        fn()
    except ToolError as e:
        return e
    raise AssertionError("expected a ToolError")


def _tip(pr, ref, pin):
    for p in Schematic.load(pr.sch).find_ref(ref)[0].pins_world():
        if p["number"] == pin:
            return p["xy"], p["out"]
    raise AssertionError(f"{ref}.{pin} not found")


def _neighbour_stub(name: str) -> Project:
    """R1.2 and R2.1 face each other on one row; R3.1's label stub OTHER ends right on that row, between them."""
    pr = _project(name)
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R1", "at": [100.33, 100.33], "rot": 90},
                     {"op": "place", "lib_id": "Device:R", "ref": "R2", "at": [130.81, 100.33], "rot": 90},
                     {"op": "place", "lib_id": "Device:R", "ref": "R3", "at": [115.57, 106.68]},
                     {"op": "connect", "pin": "R3.1", "net": "OTHER"}])
    a, b = _tip(pr, "R1", "2")[0], _tip(pr, "R2", "1")[0]
    assert abs(a[1] - b[1]) < 0.01 and a[0] < 115.57 < b[0], (a, b)
    sch = Schematic.load(pr.sch)
    assert any(l["net"] == "OTHER" and abs(l["at"][0] - 115.57) < 0.01 and abs(l["at"][1] - a[1]) < 0.01
               for l in sch.labels()), "the OTHER stub must end on the R1-R2 row for this test to mean anything"
    return pr


# ── unit: the merge rule itself (no KiCad) ────────────────────────────────
def test_unintended_merges_rule():
    def nl(**nets):
        return {"nets": {k.replace("_SL_", "/"): [{"ref": p.split(".")[0], "pin": p.split(".")[1]} for p in v]
                         for k, v in nets.items()}}
    before = nl(_SL_VUSB=["J1.A4"], **{"unconnected-(R1-Pad1)": ["R1.1"], "unconnected-(J1-A5)": ["J1.A5"]})
    after = nl(_SL_VUSB=["J1.A4", "J1.A5", "R1.1"])
    asked = [{"same": [("J1", "A5"), ("R1", "1")]}]
    bad = O.unintended_merges(before, after, asked)
    assert bad and bad[0]["net"] == "/VUSB" and ["J1.A5", "R1.1"] in bad[0]["groups"], bad
    # asked for by name: fine
    assert not O.unintended_merges(before, after, [{"pins": [("J1", "A5"), ("R1", "1")], "net": "VUSB"}])
    # a pin that did not exist before and was never mentioned (a new part's hidden power pin) is not judged
    assert not O.unintended_merges(nl(GND=["U1.4"]), nl(GND=["U1.4", "U9.7"]), [])
    # pins placed in this very batch are judged once the batch names them
    bad = O.unintended_merges({"nets": {}}, nl(_SL_SDA=["U1.5", "U1.6", "R2.1"]),
                              [{"pins": [("U1", "5")], "net": "SDA"}, {"same": [("U1", "6"), ("R2", "1")]}])
    assert bad and bad[0]["net"] == "/SDA", bad


# ── the reviewer's minimal repro: wire two resistors, then name the wire ──
def test_wire_then_label_two_resistors():
    pr = _project("rr_label")
    r = O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R1"},
                         {"op": "place", "lib_id": "Device:R", "ref": "R2", "near": "R1"},
                         {"op": "wire", "from": "R1.2", "to": "R2.1"},
                         {"op": "label", "net": "MID", "pin": "R1.2"}])
    assert any("MID" in c and "on its wire" in c for c in r["changed"]), r["changed"]
    n = _nets(pr)
    assert n[("R1", "2")] == n[("R2", "1")] == "MID", n
    assert n[("R1", "1")] != "MID" and n[("R2", "2")] != "MID", n
    # the other end is on MID already; naming it again is a no-op
    r = O.apply_ops(pr, [{"op": "label", "net": "MID", "pin": "R2.1"}])
    assert "already on MID" in r["changed"][0], r["changed"]
    # and a different name on the same wire is refused, not a second net on one wire
    assert _err(lambda: O.apply_ops(pr, [{"op": "label", "net": "OTHER", "pin": "R2.1"}])).code == "NET_CONFLICT"


def test_label_after_wire_in_a_later_batch_and_at_either_end():
    for end in ("R1.1", "R2.2"):
        pr = _project("rr_later_" + end.replace(".", "_"))
        O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R1", "at": [100.33, 100.33], "rot": 90},
                         {"op": "place", "lib_id": "Device:R", "ref": "R2", "at": [129.54, 100.33], "rot": 90},
                         {"op": "wire", "from": "R1.1", "to": "R2.2"}])
        O.apply_ops(pr, [{"op": "label", "net": "LOOP", "pin": end}])
        n = _nets(pr)
        assert n[("R1", "1")] == n[("R2", "2")] == "LOOP", n
        assert n[("R1", "2")] != "LOOP" and n[("R2", "1")] != "LOOP", n


# ── a wire must detour around a neighbour's connect stub, not join it ─────
def test_wire_detours_around_neighbour_label_stub():
    pr = _neighbour_stub("detour")
    r = O.apply_ops(pr, [{"op": "wire", "from": "R1.2", "to": "R2.1"}])
    n = _nets(pr)
    assert n[("R1", "2")] == n[("R2", "1")], n
    assert n[("R3", "1")] == "OTHER" and n[("R1", "2")] != "OTHER", n
    stub_end = (115.57, _tip(pr, "R1", "2")[0][1])
    sch = Schematic.load(pr.sch)
    for j in sch.items("junction"):
        at = sexpr.find(j, "at")
        assert abs(float(at[1]) - stub_end[0]) > 0.01 or abs(float(at[2]) - stub_end[1]) > 0.01, r["changed"]
    # the detour is clean wiring: no wire of the new run touches the stub end
    for w in sch.wires():
        if any(abs(e[0] - stub_end[0]) < 0.01 and abs(e[1] - stub_end[1]) < 0.01 for e in (w["a"], w["b"])):
            assert abs(w["a"][0] - w["b"][0]) < 0.01, f"only R3's own vertical stub may end there: {w}"


def test_reviewer_ldo_enable_pullup_not_shorted():
    """EN pull-up wired to the regulator: the route's escape column used to pass the VUSB label stub of R1.1."""
    pr = _project("ldo")
    O.apply_ops(pr, [{"op": "place", "lib_id": "Regulator_Linear:AP2112K-3.3", "ref": "U1"},
                     {"op": "place", "lib_id": "Device:R", "value": "100k", "ref": "R1", "near": "U1"},
                     {"op": "place", "lib_id": "Device:C", "value": "1uF", "ref": "C1", "near": "U1"},
                     {"op": "connect", "pins": ["U1.VIN", "C1.1", "R1.1"], "net": "VUSB"},
                     {"op": "connect", "pins": ["U1.GND", "C1.2"], "net": "GND"},
                     {"op": "connect", "pin": "U1.VOUT", "net": "+3V3"},
                     {"op": "wire", "from": "R1.2", "to": "U1.EN"}])
    n = _nets(pr)
    assert n[("R1", "1")] == n[("U1", "1")] == "VUSB", n
    assert n[("R1", "2")] == n[("U1", "3")] and n[("U1", "3")] != "VUSB", n


def test_usb_c_cc_wires_labels_and_ground():
    """The skill's USB-C example shape: wire CC pins to their resistors, name them, ground the far ends."""
    pr = _project("usbc")
    O.apply_ops(pr, [{"op": "place", "lib_id": "Connector:USB_C_Receptacle_USB2.0_16P", "ref": "J1", "group": "connector"},
                     {"op": "place", "lib_id": "Device:R", "value": "5.1k", "ref": "R1", "near": "J1"},
                     {"op": "place", "lib_id": "Device:R", "value": "5.1k", "ref": "R2", "near": "J1"},
                     {"op": "connect", "pins": ["J1.A4", "J1.B4", "J1.A9", "J1.B9"], "net": "VUSB"},
                     {"op": "wire", "from": "J1.A5", "to": "R1.1"}, {"op": "label", "net": "CC1", "pin": "R1.1"},
                     {"op": "wire", "from": "J1.B5", "to": "R2.1"}, {"op": "label", "net": "CC2", "pin": "R2.1"},
                     {"op": "connect", "pins": ["R1.2", "R2.2", "J1.A1", "J1.B12", "J1.SH"], "net": "GND"},
                     {"op": "no_connect", "pins": ["J1.A8", "J1.B8"]}])
    n = _nets(pr)
    assert n[("J1", "A5")] == n[("R1", "1")] == "CC1", n
    assert n[("J1", "B5")] == n[("R2", "1")] == "CC2", n
    assert n[("J1", "A4")] == "VUSB" and n[("R1", "2")] == n[("R2", "2")] == n[("J1", "A1")] == "GND", n
    assert sorted(k for k, v in n.items() if v == "VUSB") == [("J1", "A4"), ("J1", "A9"), ("J1", "B4"), ("J1", "B9")], n


# ── refusals are atomic ───────────────────────────────────────────────────
def test_explicit_wire_over_a_foreign_stub_is_refused_and_writes_nothing():
    pr = _neighbour_stub("merge_refused")
    a, b = _tip(pr, "R1", "2")[0], _tip(pr, "R2", "1")[0]
    before = pr.sch.read_bytes()
    batch = [{"op": "set_field", "ref": "R1", "field": "Value", "value": "1k"},
             {"op": "wire", "points": [a, b]}]
    for dry in (True, False):
        e = _err(lambda: O.apply_ops(pr, batch, dry_run=dry))
        assert e.code == "NET_MERGE" and "nothing was written" in e.message, (e.code, e.message)
        assert "R3.1" in e.message and (e.data or {}).get("applied") is False, (e.message, e.data)
        assert pr.sch.read_bytes() == before
    # drawing to the stub on purpose (a vertex on it) is a join the caller asked for
    O.apply_ops(pr, [{"op": "wire", "points": [a, [115.57, a[1]]]}])
    n = _nets(pr)
    assert n[("R1", "2")] == n[("R3", "1")] == "OTHER", n


def test_no_clean_route_is_refused_and_writes_nothing():
    """Every way into R2.1 runs through another net's wire end: refuse, name that net, write nothing."""
    pr = _project("no_route")
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R1", "at": [100.33, 100.33], "rot": 90},
                     {"op": "place", "lib_id": "Device:R", "ref": "R2", "at": [130.81, 100.33], "rot": 90}])
    (bx, by), out = _tip(pr, "R2", "1")
    assert tuple(out) == (-1, 0), out
    left, up, down = [bx - 1.27, by], [bx, by - 1.27], [bx, by + 1.27]
    O.apply_ops(pr, [{"op": "wire", "points": [left, [bx - 1.27, by - 7.62]]},
                     {"op": "wire", "points": [up, [bx + 7.62, by - 1.27]]},
                     {"op": "wire", "points": [down, [bx + 7.62, by + 1.27]]},
                     {"op": "label", "net": "FENCE", "at": [bx - 1.27, by - 7.62]},
                     {"op": "label", "net": "FENCE", "at": [bx + 7.62, by - 1.27]},
                     {"op": "label", "net": "FENCE", "at": [bx + 7.62, by + 1.27]}])
    before = pr.sch.read_bytes()
    for dry in (True, False):
        e = _err(lambda: O.apply_ops(pr, [{"op": "wire", "from": "R1.2", "to": "R2.1"}], dry_run=dry))
        assert e.code == "NO_ROUTE" and "FENCE" in e.message and "nothing was written" in e.message, (e.code, e.message)
        assert '"op":"connect"' in (e.fix or ""), e.fix
        assert pr.sch.read_bytes() == before


def test_tool_refusal_is_an_error_result_and_writes_nothing():
    from copper_kicad.mcp import registry
    import copper_kicad.tools  # noqa: F401
    pr = _neighbour_stub("tool_refused")
    a, b = _tip(pr, "R1", "2")[0], _tip(pr, "R2", "1")[0]
    before = pr.sch.read_bytes()
    r = registry.call_tool("sch_apply", {"project": str(pr.dir), "ops": [{"op": "wire", "points": [a, b]}],
                                         "preview": False})
    text = "\n".join(c["text"] for c in r["content"] if c["type"] == "text")
    assert r.get("isError") and "NET_MERGE" in text, text[:400]
    assert pr.sch.read_bytes() == before


# ── unit: the split rule ──────────────────────────────────────────────────
def test_unintended_splits_rule():
    def nl(**nets):
        return {"nets": {k: [{"ref": p.split(".")[0], "pin": p.split(".")[1]} for p in v] for k, v in nets.items()}}
    before = nl(X=["R1.2", "R2.1", "R3.1"])
    assert O.unintended_splits(before, nl(X=["R1.2", "R2.1", "R3.1"])) == []
    bad = O.unintended_splits(before, nl(**{"X": ["R3.1"], "Net-(R1-Pad2)": ["R1.2", "R2.1"]}))
    assert bad and bad[0]["net"] == "X" and len(bad[0]["groups"]) == 2, bad
    assert O.unintended_splits(before, nl(X=["R1.2", "R3.1"])) == []          # a deleted pin is not a split


def _wired_and_labelled(name: str) -> Project:
    """R1.2 wired to R2.1, R3.1 on X, then X named on the wire through `label pin=R2.1` (label sits on the wire)."""
    pr = _project(name)
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R1", "at": [100.33, 100.33], "rot": 90},
                     {"op": "place", "lib_id": "Device:R", "ref": "R2", "at": [130.81, 100.33], "rot": 90},
                     {"op": "place", "lib_id": "Device:R", "ref": "R3", "at": [160.02, 120.65]},
                     {"op": "connect", "pin": "R3.1", "net": "X"}])
    O.apply_ops(pr, [{"op": "wire", "from": "R1.2", "to": "R2.1"}])
    r = O.apply_ops(pr, [{"op": "label", "net": "X", "pin": "R2.1"}])
    assert any("on its wire" in c for c in r["changed"]), r["changed"]
    return pr


def test_wire_label_then_move_keeps_the_net():
    """A move re-draws the moved pin's wire; a label on that wire must come along (it used to be left behind and
    split the net while sch_apply reported success)."""
    for i, (ref, by) in enumerate((("R2", [7.62, 7.62]), ("R1", [0, 7.62]), ("R2", [0, -5.08]), ("R1", [-7.62, 0]))):
        pr = _wired_and_labelled(f"move_{i}")
        O.apply_ops(pr, [{"op": "move", "ref": ref, "by": by}])
        n = _nets(pr)
        assert n[("R1", "2")] == n[("R2", "1")] == n[("R3", "1")] == "X", (ref, by, n)


def test_net_split_is_refused_and_writes_nothing():
    """Backstop: a batch that deletes nothing must not split a net. Simulated by a label the move cannot know
    about (placed at explicit coordinates on the wire, then the wire's end pin moved off the row)."""
    pr = _project("split")
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R1", "at": [100.33, 100.33], "rot": 90},
                     {"op": "place", "lib_id": "Device:R", "ref": "R2", "at": [130.81, 100.33], "rot": 90},
                     {"op": "place", "lib_id": "Device:R", "ref": "R3", "at": [160.02, 120.65]},
                     {"op": "connect", "pin": "R3.1", "net": "X"}])
    O.apply_ops(pr, [{"op": "wire", "from": "R1.2", "to": "R2.1"}])
    O.apply_ops(pr, [{"op": "label", "net": "X", "at": [124.46, 100.33]}])
    orig = O._reseat_labels
    O._reseat_labels = lambda *a, **k: None                    # what old code did: leave the label behind
    try:
        before = pr.sch.read_bytes()
        e = _err(lambda: O.apply_ops(pr, [{"op": "move", "ref": "R2", "by": [7.62, 7.62]}]))
        assert e.code == "NET_SPLIT" and "'X'" in e.message, (e.code, e.message)
        assert pr.sch.read_bytes() == before
    finally:
        O._reseat_labels = orig


def test_label_on_wire_skips_a_crossing_wire():
    """A foreign wire crossing the pin's wire right where the label would go: the label picks another point."""
    pr = _project("xlabel")
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R1", "at": [100.33, 100.33], "rot": 90},
                     {"op": "place", "lib_id": "Device:R", "ref": "R2", "at": [130.81, 100.33], "rot": 90}])
    O.apply_ops(pr, [{"op": "wire", "from": "R1.2", "to": "R2.1"}])
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R3", "at": [106.68, 85.09]},
                     {"op": "place", "lib_id": "Device:R", "ref": "R4", "at": [106.68, 115.57]}])
    O.apply_ops(pr, [{"op": "wire", "points": [[106.68, 88.9], [106.68, 111.76]]}])
    O.apply_ops(pr, [{"op": "connect", "pin": "R3.1", "net": "FOREIGN"}])
    O.apply_ops(pr, [{"op": "label", "net": "MID", "pin": "R1.2"}])
    n = _nets(pr)
    assert n[("R1", "2")] == n[("R2", "1")] == "MID", n
    assert n[("R3", "2")] == n[("R4", "1")] != "MID", n


def test_route_never_falls_back_to_a_diagonal():
    """12 R over 12 C, wired crosswise: every wire is orthogonal or refused with NO_ROUTE; nets stay apart."""
    pr = _project("chain")
    ops = []
    for i in range(12):
        ops += [{"op": "place", "lib_id": "Device:R", "ref": f"R{i + 1}", "at": [78.74 + 5.08 * i, 100.33]},
                {"op": "place", "lib_id": "Device:C", "ref": f"C{i + 1}", "at": [78.74 + 5.08 * i, 115.57]}]
    O.apply_ops(pr, ops + [{"op": "connect", "pin": f"C{i + 1}.2", "net": "GND"} for i in range(12)])
    wired = []
    for i in range(11):
        a, b = f"R{i + 1}.2", f"C{12 - i}.1"
        try:
            O.apply_ops(pr, [{"op": "wire", "from": a, "to": b}])
            wired.append((a, b))
        except ToolError as e:
            assert e.code == "NO_ROUTE", (e.code, e.message)
    for w in Schematic.load(pr.sch).wires():
        assert abs(w["a"][0] - w["b"][0]) < 0.01 or abs(w["a"][1] - w["b"][1]) < 0.01, w
    n = _nets(pr)
    for a, b in wired:
        ka, kb = tuple(a.split(".")), tuple(b.split("."))
        assert n[ka] == n[kb] and sum(1 for v in n.values() if v == n[ka]) == 2, (a, b, n[ka])


def test_rename_and_connect_to_the_new_name_in_one_batch():
    """rename_net A→NEWA plus a connect to NEWA is one net, not an accidental merge."""
    pr = _project("rename")
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R1", "at": [100.33, 100.33], "rot": 90},
                     {"op": "place", "lib_id": "Device:R", "ref": "R2", "at": [130.81, 100.33], "rot": 90},
                     {"op": "connect", "pin": "R1.2", "net": "A"}])
    O.apply_ops(pr, [{"op": "rename_net", "from": "A", "to": "NEWA"}, {"op": "connect", "pin": "R2.1", "net": "NEWA"}])
    n = _nets(pr)
    assert n[("R1", "2")] == n[("R2", "1")] == "NEWA", n


def test_explicit_wire_joining_two_named_nets_warns():
    pr = _project("explicit_names")
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R1", "at": [100.33, 100.33], "rot": 90},
                     {"op": "place", "lib_id": "Device:R", "ref": "R2", "at": [130.81, 100.33], "rot": 90},
                     {"op": "connect", "pin": "R1.2", "net": "A"}, {"op": "connect", "pin": "R2.1", "net": "B"}])
    labs = {l["net"]: l["at"] for l in Schematic.load(pr.sch).labels()}
    r = O.apply_ops(pr, [{"op": "wire", "points": [labs["A"], labs["B"]]}], dry_run=True)
    assert any("joins the nets A, B" in w for w in r["warnings"]), r["warnings"]


def test_split_check_still_runs_beside_an_unrelated_delete():
    """A delete in the batch excuses splits of the nets it touches, not a split elsewhere caused by a move."""
    pr = _project("split_scope")
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R1", "at": [100.33, 100.33], "rot": 90},
                     {"op": "place", "lib_id": "Device:R", "ref": "R2", "at": [130.81, 100.33], "rot": 90},
                     {"op": "place", "lib_id": "Device:R", "ref": "R3", "at": [160.02, 120.65]},
                     {"op": "place", "lib_id": "Device:R", "ref": "R9", "at": [190.5, 120.65]},
                     {"op": "connect", "pin": "R3.1", "net": "X"}, {"op": "connect", "pin": "R9.1", "net": "Y"}])
    O.apply_ops(pr, [{"op": "wire", "from": "R1.2", "to": "R2.1"}])
    O.apply_ops(pr, [{"op": "label", "net": "X", "at": [124.46, 100.33]}])
    orig = O._reseat_labels
    O._reseat_labels = lambda *a, **k: None
    try:
        before = pr.sch.read_bytes()
        e = _err(lambda: O.apply_ops(pr, [{"op": "delete", "ref": "R9"}, {"op": "move", "ref": "R2", "by": [7.62, 7.62]}]))
        assert e.code == "NET_SPLIT" and "'X'" in e.message, (e.code, e.message)
        assert pr.sch.read_bytes() == before
    finally:
        O._reseat_labels = orig


def _shared_sheet_demo() -> Project | None:
    """KiCad's complex_hierarchy demo: ampli_ht.kicad_sch is used twice (R2xx and R3xx)."""
    from copper_kicad.kicad import env
    try:
        src = Path(env.kicad().share) / "demos" / "complex_hierarchy"
    except Exception:  # noqa: BLE001
        return None
    if not src.is_dir():
        return None
    d = TMP / "shared_sheet"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    for f in list(src.glob("*.kicad_sch")) + list(src.glob("*.kicad_pro")):
        shutil.copy2(f, d / f.name)
    for f in sorted(d.glob("*.kicad_sch")):
        env.run(["sch", "upgrade", "--force", str(f)], timeout=180)
    return Project(d, "complex_hierarchy")


def test_second_instance_of_a_shared_sheet_is_judged_by_its_own_references():
    """Edits through the second instance (R3xx) used to be refused with NET_MERGE naming first-instance refs."""
    pr = _shared_sheet_demo()
    if pr is None:
        print("SKIP: KiCad demos not installed")
        return
    for ops in ([{"op": "wire", "from": "R303.1", "to": "R313.1"}],
                [{"op": "connect", "pins": ["R303.1", "R303.2"], "net": "SHORTX"}],
                [{"op": "connect", "pins": ["R203.1", "R203.2"], "net": "SHORTX"}]):
        r = O.apply_ops(pr, ops, dry_run=True, sheet="ampli_ht.kicad_sch")
        assert r["changed"], (ops, r)


def _run_all():
    import time
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in tests:
        t = time.time()
        try:
            f()
            print(f"PASS {n} ({time.time() - t:.1f}s)")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {n}: {type(e).__name__}: {e}")
            traceback.print_exc(limit=4)
    print(f"\n{len(tests) - failed}/{len(tests)} passed — artifacts in {TMP}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
