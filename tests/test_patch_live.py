"""sch/patch.py unit tests and the live-host seam: live mode, the guard_writable hook, host/route.py.

    python tests/test_patch_live.py [name-fragment ...]        (KiCad's Python; the seam tests need kicad-cli 10)

The host here is FileHost: the smallest correct host, on files, in this process. It answers host.checkout with a
copy of its "open document" and applies host.apply with the reference applier (patch.apply_project). Engines reach
it the way an external MCP server would: through host/relay.py and the rendezvous file, with a token.
Nothing outside a temp folder is touched; no model, no network beyond loopback.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_BASE = os.environ.get("COPPER_TEST_TMP")
if _BASE:
    Path(_BASE).mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="copper-test-live-", dir=_BASE))
for _k, _d in (("COPPER_HOME", "home"), ("COPPER_CONFIG_HOME_OVERRIDE", "cfg"), ("KICAD_CONFIG_HOME", "kicad-config")):
    (TMP / _d).mkdir()
    os.environ[_k] = str(TMP / _d)
os.environ["COPPER_HOME"] = str(TMP / "home")        # spelled out: test_install.py checks every module for it
for _k in ("COPPER_HOST_PORT", "COPPER_HOST_TOKEN", "COPPER_HOST_PROJECT", "COPPER_LIVE", "COPPER_LIVE_CANON"):
    os.environ.pop(_k, None)

from copper_kicad import project as P  # noqa: E402
from copper_kicad import sexpr  # noqa: E402
from copper_kicad.host import client as hostclient, rendezvous, route  # noqa: E402
from copper_kicad.host.live import LiveSession  # noqa: E402
from copper_kicad.host.relay import Relay  # noqa: E402
from copper_kicad.kicad import env  # noqa: E402
from copper_kicad.mcp import registry  # noqa: E402
from copper_kicad.mcp.registry import ToolError  # noqa: E402
from copper_kicad.project import Project  # noqa: E402
from copper_kicad.sch import model as M  # noqa: E402
from copper_kicad.sch import netlist as N  # noqa: E402
from copper_kicad.sch import ops as O  # noqa: E402
from copper_kicad.sch import patch as SP  # noqa: E402
import copper_kicad.tools  # noqa: E402,F401

_STATE: dict = {}
_N = [0]


def workdir(name: str) -> Path:
    _N[0] += 1
    d = TMP / f"{_N[0]:02d}-{name}"
    d.mkdir(parents=True)
    return d


# ── synthetic sheets (no KiCad needed) ────────────────────────────────────
def U(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def sym(n: int, ref: str, value: str, x=100, lib="Device:R", extra="") -> str:
    return (f'(symbol (lib_id "{lib}") (at {x} 50 0) (unit 1) (uuid "{U(n)}") '
            f'(property "Reference" "{ref}" (at {x} 48 0)) (property "Value" "{value}" (at {x} 52 0)) {extra}'
            f'(instances (project "p" (path "/{U(900)}" (reference "{ref}") (unit 1)))))')


def wire(n: int, x0=10, x1=20) -> str:
    return f'(wire (pts (xy {x0} 10) (xy {x1} 10)) (stroke (width 0) (type default)) (uuid "{U(n)}"))'


def lib(name="Device:R", pins=2) -> str:
    body = " ".join(f'(pin passive line (at 0 {i} 0) (length 1.27) (name "~") (number "{i + 1}"))' for i in range(pins))
    return f'(symbol "{name}" (symbol "{name.split(":")[-1]}_1_1" {body}))'


def sheet(*items, libs=(), header='(paper "A4")', tail='(sheet_instances (path "/" (page "1")))') -> str:
    return (f'(kicad_sch (version 20260306) (generator "eeschema") (generator_version "10.0") (uuid "{U(900)}") {header} '
            f'(lib_symbols {" ".join(libs)}) {" ".join(items)} {tail} (embedded_fonts no))')


def uuids(text: str) -> list:
    return list(SP._index(sexpr.loads(text))[0])


def expect_error(code: str, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except SP.PatchError as e:
        assert e.code == code, f"expected {code}, got {e.code}: {e.message}"
        return e
    raise AssertionError(f"expected PatchError {code}")


def test_diff_and_apply_round_trip_keeps_uuids_and_order():
    before = sheet(sym(1, "R1", "10k"), wire(2), sym(3, "R2", "4k7", x=120), wire(4, 30, 40), libs=[lib()])
    after = sheet(sym(1, "R1", "22k"), sym(3, "R2", "4k7", x=120), wire(4, 30, 40), wire(5, 50, 60),
                  sym(6, "C1", "100n", x=140, lib="Device:C"), libs=[lib(), lib("Device:C")])
    d = SP.diff_sheet(before, after)
    assert (d["changed"], d["added"], d["removed"]) == ([U(1)], [U(5), U(6)], [U(2)]), d
    assert d["remove"] == [U(1), U(2)] and d["refs"] == ["C1", "R1"] and d["lib_names"] == ["Device:C"]
    assert d["kinds"] == {"symbol": 2, "wire": 2} and d["region"] == [10.0, 10.0, 140.0, 50.0], (d["kinds"], d["region"])
    assert [SP.item_uuid(n) for n in SP.parse_add(d["add"])] == [U(1), U(5), U(6)]
    assert d["skipped"] == [] and SP.fragment_problems(d["add"], lib_names={"Device:R", "Device:C"}) == []
    out = SP.apply_sheet(before, d, d["lib_symbols"])
    assert uuids(out) == [U(1), U(3), U(4), U(5), U(6)], uuids(out)         # R1 replaced in place, new items at the end
    again = SP.diff_sheet(out, after)
    assert not again["remove"] and not again["add"] and not again["lib_symbols"], again
    tree = sexpr.loads(out)
    assert [n[0] for n in tree[-2:]] == ["sheet_instances", "embedded_fonts"]       # new items went before the tail
    assert set(SP._lib_symbols(tree)) == {"Device:R", "Device:C"}
    assert SP.diff_sheet(before, before)["add"] == "" and SP.diff_sheet("", before)["added"] == [U(1), U(2), U(3), U(4)]


def test_formatting_number_spelling_and_item_order_are_not_changes():
    a = sheet(sym(1, "R1", "10k"), wire(2), libs=[lib()])
    b = sheet(wire(2), sym(1, "R1", "10k"), libs=[lib()]).replace("(at 100 50 0)", "(at 100.0 50.00 0)").replace(") (", ")\n\t(")
    d = SP.diff_sheet(a, b)
    assert not d["remove"] and not d["add"] and not d["lib_symbols"] and not d["skipped"], d
    c = b.replace('"10k"', '"10"')
    assert SP.diff_sheet(a, c)["changed"] == [U(1)]
    quoted = a.replace("(unit 1) (uuid", '(unit "1") (uuid')            # quoting alone is formatting
    assert SP.diff_sheet(a, quoted)["changed"] == []


def test_apply_refuses_whole_patch_and_changes_nothing():
    text = sheet(sym(1, "R1", "10k"), wire(2), libs=[lib()])
    expect_error("UNKNOWN_ITEM", SP.apply_sheet, text, {"remove": [U(77)], "add": ""})
    expect_error("DUPLICATE_ITEM", SP.apply_sheet, text, {"remove": [], "add": wire(2)})
    expect_error("DUPLICATE_ITEM", SP.apply_sheet, text, {"remove": [], "add": wire(5) + wire(5)})
    expect_error("PARSE", SP.apply_sheet, text, {"remove": [], "add": "(wire (pts"})
    expect_error("PARSE", SP.apply_sheet, text, {"remove": [], "add": '(paper "A3")'})
    expect_error("PARSE", SP.apply_sheet, text, {"remove": [], "add": "(wire (pts (xy 1 1) (xy 2 2)))"})      # no uuid
    expect_error("PARSE", SP.apply_sheet, text, {"remove": [U(2), U(2)], "add": ""})
    expect_error("PARSE", SP.apply_sheet, text, {"remove": [], "add": wire(5)}, ["(not-a-symbol)"])
    expect_error("PARSE", SP.apply_sheet, text, {"remove": [], "add": wire(5), "libs": ["Device:Q"]}, [lib()])
    expect_error("PARSE", SP.apply_sheet, "(kicad_pcb)", {"remove": [], "add": ""})
    texts = {"a.kicad_sch": text, "b.kicad_sch": text}
    good = {"file": "a.kicad_sch", "remove": [U(2)], "add": ""}
    expect_error("UNKNOWN_ITEM", SP.apply_project, texts, [good, {"file": "b.kicad_sch", "remove": [U(9)], "add": ""}])
    expect_error("NO_SHEET", SP.apply_project, texts, [good, {"file": "c.kicad_sch", "remove": [], "add": ""}])
    assert texts == {"a.kicad_sch": text, "b.kicad_sch": text}
    out = SP.apply_project(texts, [good, {"file": "a.kicad_sch", "remove": [], "add": wire(8)}])     # in order, same file
    assert list(out) == ["a.kicad_sch"] and uuids(out["a.kicad_sch"]) == [U(1), U(8)]
    assert SP.patch_counts([{"remove": [U(1), U(2)], "add": sym(1, "R1", "1k") + wire(9)}]) == {"added": 1, "changed": 1, "removed": 1}


def test_item_kinds_rule_area_sheet_group_and_what_is_skipped():
    rule = f'(rule_area (polyline (pts (xy 1 1) (xy 9 1) (xy 9 9)) (stroke (width 0)) (uuid "{U(20)}")))'
    sh = (f'(sheet (at 50 60) (size 20 10) (uuid "{U(21)}") (property "Sheetname" "psu") (property "Sheetfile" "psu.kicad_sch") '
          f'(pin "VIN" input (at 50 62 180) (uuid "{U(22)}")) (instances (project "p" (path "/{U(900)}" (page "2")))))')
    grp = f'(group "g" (uuid "{U(23)}") (members "{U(1)}" "{U(20)}"))'
    old_text = '(text "legacy note" (at 5 5 0))'                                  # an item without a uuid
    before = sheet(sym(1, "R1", "10k"), old_text, libs=[lib()])
    after = sheet(sym(1, "R1", "10k"), rule, sh, grp, old_text.replace("legacy", "changed"), libs=[lib()],
                  header='(paper "A3") (title_block (title "x"))', tail='(sheet_instances (path "/" (page "7")))')
    d = SP.diff_sheet(before, after)
    assert d["added"] == [U(20), U(21), U(23)] and d["kinds"] == {"rule_area": 1, "sheet": 1, "group": 1}, d
    assert d["region"] == [1.0, 1.0, 70.0, 70.0], d["region"]                     # the sheet symbol's far corner counts
    sk = " | ".join(d["skipped"])
    for word in ("'paper'", "'title_block'", "'sheet_instances'", "without a uuid"):
        assert word in sk, (word, d["skipped"])
    out = SP.apply_sheet(before, d)
    assert uuids(out) == [U(1), U(20), U(21), U(23)] and '"legacy note"' in out and '(paper "A4")' in out
    back = SP.diff_sheet(out, after)
    assert not back["add"] and not back["remove"]
    # a rule area is addressed by the uuid of its polyline
    assert uuids(SP.apply_sheet(out, {"remove": [U(20)], "add": ""})) == [U(1), U(21), U(23)]
    dup = sheet(wire(2), wire(2, 30, 40), wire(3))
    dd = SP.diff_sheet(dup, sheet(wire(3)))
    assert dd["removed"] == [] and any("more than once" in s for s in dd["skipped"]), dd
    expect_error("DUPLICATE_ITEM", SP.apply_sheet, dup, {"remove": [], "add": wire(2)})


def test_library_symbols_go_only_to_the_sheets_that_use_them():
    a0 = sheet(sym(1, "R1", "10k"), libs=[lib()])
    b0 = sheet(sym(11, "R9", "1k"), libs=[lib()])
    a1 = sheet(sym(1, "R1", "10k"), sym(2, "Q1", "BC547", lib="Device:Q", extra='(lib_name "Q_1") '),
               libs=[lib(), lib("Q_1", 3), lib("Device:Unused")])
    b1 = sheet(sym(11, "R9", "1k"), libs=[lib("Device:R", 3)])                      # same name, changed definition
    info = SP.diff_project({"a.kicad_sch": a0, "sub/b.kicad_sch": b0}, {"a.kicad_sch": a1, "sub/b.kicad_sch": b1},
                           [{"sheet_path": "/", "file": "a.kicad_sch"}, {"sheet_path": "/b/", "file": "sub/b.kicad_sch"},
                            {"sheet_path": "/b2/", "file": "sub/b.kicad_sch"}])
    by = {p["file"]: p for p in info["patches"]}
    assert by["a.kicad_sch"]["libs"] == ["Q_1"] and by["sub/b.kicad_sch"]["libs"] == ["Device:R"], by
    assert by["sub/b.kicad_sch"]["sheet_path"] == "/b/" and by["sub/b.kicad_sch"]["add"] == ""    # a library-only patch
    assert len(info["lib_symbols"]) == 2 and not info["empty"] and info["counts"] == {"added": 1, "changed": 0, "removed": 0}
    out = SP.apply_project({"a.kicad_sch": a0, "sub/b.kicad_sch": b0}, info["patches"], info["lib_symbols"])
    la, lb = (SP._lib_symbols(sexpr.loads(out[f])) for f in ("a.kicad_sch", "sub/b.kicad_sch"))
    assert set(la) == {"Device:R", "Q_1"} and SP._same(la["Device:R"], sexpr.loads(lib()))      # a's R stays the old one
    assert SP._same(lb["Device:R"], sexpr.loads(lib("Device:R", 3)))
    # without the per-patch list a host merges what the resulting sheet uses, and nothing else
    loose = [{k: v for k, v in p.items() if k != "libs"} for p in info["patches"] if p["file"] == "a.kicad_sch"]
    la2 = SP._lib_symbols(sexpr.loads(SP.apply_project({"a.kicad_sch": a0}, loose, [lib("Q_1", 3), lib("Device:Zed")])["a.kicad_sch"]))
    assert set(la2) == {"Device:R", "Q_1"}
    # the same name with two different new definitions in one edit: one is sent, the other is reported
    c0, c1 = sheet(sym(21, "R5", "1"), libs=[]), sheet(sym(21, "R5", "1"), libs=[lib("Device:R", 4)])
    clash = SP.diff_project({"b.kicad_sch": b0, "c.kicad_sch": c0}, {"b.kicad_sch": b1, "c.kicad_sch": c1})
    assert len(clash["lib_symbols"]) == 1 and any("differs from the one sent" in s for s in clash["skipped"]), clash
    missing = SP.diff_sheet(a0, sheet(sym(1, "R1", "10k"), sym(3, "D1", "x", lib="Device:D"), libs=[lib()]))
    assert any("no lib_symbols entry" in s for s in missing["skipped"])
    new = SP.diff_project({"a.kicad_sch": a0}, {"a.kicad_sch": a0, "new.kicad_sch": b0})
    assert new["new_sheets"] == ["new.kicad_sch"] and new["empty"] and not new["patches"]


def test_fragment_checker_and_clipboard_shape():
    good = sym(1, "R1", 'a \\")\\" in a (string') + "\n" + wire(2)
    assert SP.fragment_problems(good) == [] and SP.fragment_problems("") == [] and SP.fragment_problems("  \n") == []
    bad = {
        "trailing text": good + " stray",
        "leading atom": "x " + good,
        "unbalanced close": good + ")",
        "unterminated": good[:-1],
        "open string": wire(2)[:-3],
        "file wrapper": f"(kicad_sch (version 20260306) {wire(2)})",
        "header node": '(paper "A4")' + wire(2),
        "title block": "(title_block)",
        "bus alias": '(bus_alias "x" (members a b))',
        "embedded": "(embedded_fonts no)",
        "lib_symbols inside add": "(lib_symbols)" + wire(2),
        "no uuid": "(junction (at 1 1))",
        "uuid twice": wire(2) + wire(2),
        "symbol without lib_id": f'(symbol (at 1 1 0) (uuid "{U(4)}"))',
    }
    for what, text in bad.items():
        assert SP.fragment_problems(text), f"not caught: {what}"
        try:
            SP.parse_add(text)
            ok = what in ("symbol without lib_id", "uuid twice")       # parse_add checks shape; apply checks uuids
        except SP.PatchError:
            ok = True
        assert ok, f"parse_add accepted: {what}"
    assert SP.fragment_problems(sym(1, "R1", "1"), lib_names={"Device:C"}) and not SP.fragment_problems(sym(1, "R1", "1"), lib_names={"Device:R"})
    clip = SP.clipboard_text(good, [lib()])
    nodes = SP._loads_many(clip)
    assert [n[0] for n in nodes] == ["lib_symbols", "symbol", "wire"] and nodes[0][1][1] == "Device:R"
    assert SP.clipboard_text(good).startswith("(symbol") and SP.clipboard_text("") == ""
    w = sexpr.loads(SP.wrap_sheet(good, [lib()]))
    assert w[0] == "kicad_sch" and uuids(SP.wrap_sheet(good, [lib()])) == [U(1), U(2)] and "Device:R" in SP._lib_symbols(w)
    assert sexpr.value(w, "version") == str(SP.FILE_VERSION) == str(M.FILE_VERSION)


def test_netlist_hash_ignores_names_order_and_power_symbols():
    a = {"nets": {"/A": [{"ref": "R1", "pin": "1"}, {"ref": "U1", "pin": "3"}, {"ref": "#PWR01", "pin": "1"}],
                  "GND": [{"ref": "R1", "pin": "2"}, {"ref": "U1", "pin": "4"}, {"ref": "U1", "pin": "11"}],
                  "unconnected-(#FLG1)": [{"ref": "#FLG01", "pin": "1"}]}}
    b = {"nets": {"GND": [{"ref": "U1", "pin": "11"}, {"ref": "U1", "pin": "4"}, {"ref": "R1", "pin": "2"}],
                  "Net-(R1-Pad1)": [{"ref": "U1", "pin": "3"}, {"ref": "R1", "pin": "1"}]}}
    assert SP.netlist_lines(a) == ["R1.1,U1.3", "R1.2,U1.11,U1.4"] == SP.netlist_lines(b)
    assert SP.netlist_hash(a) == SP.netlist_hash(b) and SP.netlist_hash(a).startswith("sha256:") and len(SP.netlist_hash(a)) == 71
    import hashlib
    assert SP.netlist_hash(a) == "sha256:" + hashlib.sha256(b"R1.1,U1.3\nR1.2,U1.11,U1.4").hexdigest()
    c = {"nets": {"GND": b["nets"]["GND"][:2], "x": b["nets"]["Net-(R1-Pad1)"]}}
    assert SP.netlist_hash(c) != SP.netlist_hash(a) and SP.expect(a) == {"netlist_hash": SP.netlist_hash(a)}
    assert SP.netlist_hash({}) == "sha256:" + hashlib.sha256(b"").hexdigest()


def test_shared_sheet_items_carry_every_instance_path():
    two = ('(instances (project "p" (path "/root/a" (reference "R201") (unit 1)) (path "/root/b" (reference "R301") (unit 1))))')
    s0 = f'(symbol (lib_id "Device:R") (at 1 1 0) (uuid "{U(1)}") (property "Reference" "R201") (property "Value" "1k") {two})'
    d = SP.diff_sheet(sheet(s0, libs=[lib()]), sheet(s0.replace('"1k"', '"2k"'), libs=[lib()]))
    assert d["refs"] == ["R201", "R301"] and d["add"].count("(path") == 2 and '"/root/b"' in d["add"], d


# ── the live seam (needs KiCad) ───────────────────────────────────────────
class FileHost(hostclient.Host):
    """A host on files: `live` is the open document; checkout copies it, apply patches it with the reference applier."""

    def __init__(self, live: Path, check_netlist: bool = True):
        self.live = Path(live)
        self.rev = 1
        self.calls: list = []
        self.undo: list = []
        self.busy = False
        self.check_netlist = check_netlist
        self.granted = True

    def _pr(self) -> Project:
        return _project(self.live)

    def sheets(self) -> list:
        pr, names, out, seen = self._pr(), {}, [], set()
        for h in O.hierarchy(pr):
            parent = h["path"].rsplit("/", 1)[0]
            names[h["path"]] = (names.get(parent, "").rstrip("/") + "/" + h["name"]) if h["name"] else "/"
            rel = os.path.relpath(h["file"], self.live).replace(os.sep, "/")
            if rel not in seen and Path(h["file"]).exists():
                seen.add(rel)
                out.append({"sheet_path": names[h["path"]], "file": rel})
        return out

    def checkout(self, document="schematic"):
        self.calls.append(("checkout", document))
        shadow = self.live / ".copper" / "shadow" / str(self.rev)
        shutil.rmtree(shadow, ignore_errors=True)
        shutil.copytree(self.live, shadow, ignore=lambda _d, names: [n for n in names if n in (".copper", "copper-out")
                                                                     or n.startswith("~") or n.endswith(".lck")])
        return {"shadow_dir": str(shadow), "rev": self.rev, "document": document, "sheets": self.sheets(),
                "project": self._pr().name, "dirty": False}

    def apply(self, *, base_rev, document, patches, lib_symbols=None, files=None, summary="", expect=None):
        self.calls.append(("apply", {"base_rev": base_rev, "patches": patches, "lib_symbols": lib_symbols or [],
                                     "files": files or [], "summary": summary, "expect": expect}))
        if self.busy:
            raise hostclient.HostError("BUSY", "the editor is busy")
        if base_rev != self.rev:
            raise hostclient.HostError("STALE_BASE", "the document changed since the checkout", {"rev": self.rev})
        texts = {}
        for p in patches:
            f = self.live / str(p.get("file") or "")
            if not f.is_file():
                raise hostclient.HostError("NO_SHEET", f"no sheet {p.get('file')!r}")
            texts[p["file"]] = f.read_text(encoding="utf-8")
        try:
            new = SP.apply_project(texts, patches, lib_symbols or [])
            counts = SP.patch_counts(patches)
        except SP.PatchError as e:
            raise hostclient.HostError(e.code, e.message) from None
        old = dict(texts)
        for f in files or []:
            dst = self.live / f["path"]
            old[f["path"]] = dst.read_text(encoding="utf-8") if dst.exists() else None
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f["shadow_path"], dst)
        for rel, text in new.items():
            (self.live / rel).write_text(text, encoding="utf-8", newline="\n")
        self.undo.append(old)
        self.rev += 1
        ok = None
        if self.check_netlist and expect and expect.get("netlist_hash"):
            ok = SP.netlist_hash(N.export_netlist(self._pr())) == expect["netlist_hash"]
        return {"proposal_id": f"p{len(self.undo)}", "state": "review", "rev": self.rev, "counts": counts,
                "erc_new": [], "netlist_ok": ok}

    def undo_last(self):
        for rel, text in self.undo.pop().items():
            if text is None:
                (self.live / rel).unlink()
            else:
                (self.live / rel).write_text(text, encoding="utf-8", newline="\n")
        self.rev += 1

    def user_edit(self):
        self.rev += 1

    def reveal(self, **kw):
        return {}

    def checkpoint(self, label, document=None):
        self.calls.append(("checkpoint", label))
        return {"id": "cp1"}

    def save(self, document):
        self.calls.append(("save", document))
        return {"rev": self.rev}

    def reload(self, document):
        self.calls.append(("reload", document))
        self.rev += 1
        return {"rev": self.rev}

    def ask_permission(self, kind, detail):
        self.calls.append(("ask_permission", kind))
        return {"granted": self.granted, "remember": False}

    def n(self, what: str) -> int:
        return sum(1 for c in self.calls if c[0] == what)


def _project(d: Path) -> Project:
    pros = sorted(p.stem for p in Path(d).glob("*.kicad_pro"))
    return Project(Path(d), Path(d).name if Path(d).name in pros or not pros else pros[0])


def base_project() -> Path:
    """Two resistors and a capacitor on three nets, built once by the engine (normal mode, KiCad re-saved)."""
    if "base" not in _STATE:
        d = TMP / "base" / "seam"
        d.mkdir(parents=True)
        O.apply_ops(Project(d, "seam"), [
            {"op": "place", "lib_id": "Device:R", "value": "10k", "ref": "R1"},
            {"op": "place", "lib_id": "Device:R", "value": "4k7", "ref": "R2"},
            {"op": "place", "lib_id": "Device:C", "value": "100n", "ref": "C1"},
            {"op": "connect", "pin": "R1.1", "net": "+3V3"}, {"op": "connect", "pins": ["R1.2", "R2.1", "C1.1"], "net": "MID"},
            {"op": "connect", "pins": ["R2.2", "C1.2"], "net": "GND"}])
        _STATE["base"] = d
    return _STATE["base"]


def open_copy(name: str, src: Path | None = None) -> Path:
    d = workdir(name) / "seam" if src is None else workdir(name) / src.name
    shutil.copytree(src or base_project(), d, ignore=lambda _d, names: [n for n in names if n in (".copper", "copper-out", "libs")
                                                                        or n.endswith((".kicad_pcb", ".kicad_prl", ".pretty"))])
    return d


def lock(d: Path) -> Path:
    pr = _project(d)
    lk = d / f"~{pr.sch.name}.lck"
    lk.write_text(json.dumps({"hostname": "test", "username": "test"}), encoding="utf-8")
    return lk


def symbol_uuid(d: Path, ref: str, rel: str | None = None) -> str:
    pr = _project(d)
    for s in M.Schematic.load(d / rel if rel else pr.sch, pr.name).symbols():
        if s.ref == ref:
            return str(sexpr.value(s.node, "uuid"))
    raise AssertionError(f"no {ref}")


def value_of(d: Path, ref: str, rel: str | None = None) -> str:
    pr = _project(d)
    return next(s.value for s in M.Schematic.load(d / rel if rel else pr.sch, pr.name).symbols() if s.ref == ref)


def payload(wire: dict) -> dict:
    text = "\n".join(c.get("text", "") for c in wire.get("content") or [] if c.get("type") == "text")
    out = {}
    for line in text.splitlines():
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.update(v)
    out["_text"] = text
    return out


def test_live_mode_skips_the_kicad_resave_and_is_scoped():
    assert not M.live()
    with M.live_mode():
        assert M.live()
        with M.live_mode(False):
            assert not M.live()
        assert M.live()
    assert not M.live()
    os.environ["COPPER_LIVE"] = "1"
    try:
        assert M.live()
        with M.live_mode(False):
            assert not M.live()
    finally:
        del os.environ["COPPER_LIVE"]
    d = open_copy("livemode")
    pr = _project(d)
    before = pr.sch.read_text(encoding="utf-8")
    calls = []
    real = env.run
    env.run = lambda args, **kw: (calls.append(list(args[:2])), real(args, **kw))[1]
    try:
        with M.live_mode():
            res = O.apply_ops(pr, [{"op": "set_field", "ref": "R1", "field": "Value", "value": "33k"}])
        assert res.get("live") is True and res.get("canonicalized") is False and calls == [], (res, calls)
        res2 = O.apply_ops(pr, [{"op": "set_field", "ref": "R2", "field": "Value", "value": "1k"}])
        assert res2.get("canonicalized") is True and ["sch", "upgrade"] in calls, (res2, calls)
    finally:
        env.run = real
    d1 = SP.diff_sheet(before, pr.sch.read_text(encoding="utf-8"))
    assert len(d1["changed"]) == 2 and not d1["added"] and not d1["removed"], d1     # KiCad's re-save touched nothing else


def test_guard_writable_routes_to_the_live_host_instead_of_refusing():
    d = open_copy("guard")
    pr = _project(d)
    P.guard_writable(pr.sch)                                             # no lock, no host: writable
    lk = lock(d)
    try:
        P.guard_writable(pr.sch)
        raise AssertionError("an open document was writable")
    except ToolError as e:
        assert e.code == "FILE_OPEN_IN_KICAD", e.code
    assert P.live_host(pr.sch) is None and route.registered_for(d) is None
    relay = Relay(FileHost(d), d)
    relay.start()
    try:
        info = P.live_host(pr.sch)
        assert info and info["via"] == "rendezvous" and info["pid"] == os.getpid() and Path(info["project_dir"]) == d, info
        for target in (pr.sch, pr.pro, d / "sub" / "child.kicad_sch"):
            (d / "sub").mkdir(exist_ok=True)
            try:
                P.guard_writable(target)
                raise AssertionError(f"{target.name}: a direct write to a hosted project was allowed")
            except ToolError as e:
                assert e.code == "LIVE_HOST" and e.data["live_host"]["via"] == "rendezvous" and "token" not in json.dumps(e.to_dict()), e.to_dict()
        lk.unlink()
        try:                                                             # the host counts even without a lock file
            P.guard_writable(pr.sch)
            raise AssertionError("no redirect without a lock")
        except ToolError as e:
            assert e.code == "LIVE_HOST"
        P.guard_writable(pr.sch, "revert")                               # the file-job route: the app reloads afterwards
        shadow = d / ".copper" / "shadow" / "3"
        shadow.mkdir(parents=True)
        (shadow / "seam.kicad_pro").write_text("{}", encoding="utf-8")
        P.guard_writable(shadow / "seam.kicad_sch")                      # a shadow checkout is never redirected
        with route.inside():
            P.guard_writable(pr.sch)                                     # nor is the routed call itself
        other = open_copy("guard-other")
        P.guard_writable(_project(other).sch)                            # another project is not affected
        os.environ.update(relay.env())                                   # a child of the agent: host given by env
        try:
            assert P.live_host(pr.sch)["via"] == "env" and P.live_host(_project(other).sch) is None
            del os.environ["COPPER_HOST_PROJECT"]
            assert P.live_host(_project(other).sch)["via"] == "env"      # without the project name the env host is THE host
        finally:
            for k in ("COPPER_HOST_PORT", "COPPER_HOST_TOKEN", "COPPER_HOST_PROJECT"):
                os.environ.pop(k, None)
    finally:
        relay.stop()
    assert P.live_host(pr.sch) is None
    P.guard_writable(pr.sch)
    rendezvous.publish(d, port=1, token="t", pid=2 ** 22 + 12345)       # a host that died: stale entry, plain project again
    assert P.live_host(pr.sch) is None and not rendezvous.path(d).exists()


def test_route_call_tool_lands_an_edit_in_the_open_document():
    d = open_copy("route")
    pr = _project(d)
    lock(d)
    host = FileHost(d)
    relay = Relay(host, d)
    relay.start()
    try:
        before = pr.sch.read_bytes()
        r1_uuid = symbol_uuid(d, "R1")
        args = {"project": str(d), "ops": [{"op": "set_field", "ref": "R1", "field": "Value", "value": "22k"}], "preview": False}
        raw = registry.call_tool("sch_apply", dict(args))                 # the plain pipeline: refused, nothing written
        assert raw["isError"] and raw["structuredContent"]["error"] == "LIVE_HOST", raw["structuredContent"]
        assert pr.sch.read_bytes() == before and host.calls == []
        t0 = time.time()
        res = payload(route.call_tool("sch_apply", dict(args)))
        took = time.time() - t0
        assert host.n("checkout") == 1 and host.n("apply") == 1, host.calls
        ap = host.calls[-1][1]
        assert ap["base_rev"] == 1 and len(ap["patches"]) == 1 and ap["patches"][0]["remove"] == [r1_uuid], ap["patches"]
        assert ap["patches"][0]["file"] == "seam.kicad_sch" and ap["patches"][0]["sheet_path"] == "/"
        assert ap["expect"]["netlist_hash"].startswith("sha256:") and ap["lib_symbols"] == []
        assert res["live"]["state"] == "review" and res["live"]["netlist_ok"] is True, res
        assert "erc" not in res["_text"].lower() or '"skipped"' in res["_text"], res["_text"]      # no file ERC in live mode
        assert '"connectivity"' in res["_text"] or "connectivity" in res["_text"], res["_text"]
        assert value_of(d, "R1") == "22k" and symbol_uuid(d, "R1") == r1_uuid
        # a second edit continues from the rev the host returned: no new checkout, and a place gets its library symbol
        res2 = payload(route.call_tool("sch_apply", {"project": str(d), "preview": False, "ops": [
            {"op": "place", "lib_id": "Device:LED", "id": "d"}, {"op": "connect", "pin": "d.1", "net": "MID"},
            {"op": "connect", "pin": "d.2", "net": "GND"}]}))
        assert host.n("checkout") == 1 and host.n("apply") == 2 and res2["live"]["netlist_ok"] is True, (host.calls[-1], res2)
        ap2 = host.calls[-1][1]
        assert ap2["base_rev"] == 2 and len(ap2["lib_symbols"]) == 1 and ap2["patches"][0]["libs"] == ["Device:LED"], ap2["patches"][0].keys()
        assert SP.fragment_problems(ap2["patches"][0]["add"]) == []
        nl = N.export_netlist(pr)
        assert {f"{n['ref']}.{n['pin']}" for n in nl["nets"]["/MID"]} >= {"D1.1", "R1.2"}, nl["nets"].keys()
        # reads go through the shadow too and do not apply anything
        rd = route.call_tool("sch_read", {"project": str(d)})
        assert not rd["isError"] and host.n("apply") == 2
        # the user edits in the app: the next apply is refused once (STALE_BASE), nothing lands, the retry works
        host.user_edit()
        snap = pr.sch.read_bytes()
        st = route.call_tool("sch_apply", {"project": str(d), "preview": False,
                                           "ops": [{"op": "set_field", "ref": "R2", "field": "Value", "value": "1k"}]})
        assert st["isError"] and payload(st)["error"] == "STALE_BASE" and pr.sch.read_bytes() == snap, payload(st)
        ok = payload(route.call_tool("sch_apply", {"project": str(d), "preview": False,
                                                   "ops": [{"op": "set_field", "ref": "R2", "field": "Value", "value": "1k"}]}))
        assert ok["live"]["netlist_ok"] is True and value_of(d, "R2") == "1k" and host.n("checkout") == 2
        host.undo_last()
        assert value_of(d, "R2") == "4k7"
        _STATE["route_s"] = round(took, 1)
    finally:
        relay.stop()
        route.forget()
    # the host is gone: the lock is still there, so the plain refusal is back
    gone = route.call_tool("sch_apply", {"project": str(d), "preview": False,
                                         "ops": [{"op": "set_field", "ref": "R2", "field": "Value", "value": "9k"}]})
    assert gone["isError"] and "FILE_OPEN_IN_KICAD" in payload(gone)["_text"], payload(gone)


def test_route_without_a_host_is_the_plain_file_pipeline():
    d = open_copy("plain")
    res = route.call_tool("sch_apply", {"project": str(d), "preview": False, "verify": False,
                                        "ops": [{"op": "set_field", "ref": "R1", "field": "Value", "value": "1M"}]})
    assert not res["isError"] and value_of(d, "R1") == "1M", payload(res)
    assert route.try_live("sch_apply", {"project": str(d), "ops": []}) is None
    assert route.try_live("copper_status", {}) is None and route.try_live("no_such_tool", {}) is None


def test_live_session_canonical_mode_and_host_refusals():
    d = open_copy("session")
    host = FileHost(d)
    sess = LiveSession(host, str(d))
    assert sess.live_mode is True
    sess.live_mode = False                                              # COPPER_LIVE_CANON=1: kicad-cli re-saves the shadow
    out = sess.call_tool("sch_apply", {"ops": [{"op": "move", "ref": "C1", "at": [180.34, 100.33]}], "preview": False})
    assert out["ok"] and out["proposal"]["netlist_ok"] is True and out["proposal"]["counts"]["changed"] >= 1, out
    assert "verify" in (out["data"] or {}) and "skipped" not in json.dumps(out["data"]["verify"]), out["data"].get("verify")
    host.busy = True
    snap = _project(d).sch.read_bytes()
    busy = sess.call_tool("sch_apply", {"ops": [{"op": "delete", "ref": "C1"}], "preview": False})
    assert not busy["ok"] and busy["error"]["code"] == "BUSY" and _project(d).sch.read_bytes() == snap
    host.busy = False
    again = sess.call_tool("sch_apply", {"ops": [{"op": "delete", "ref": "C1"}], "preview": False})
    assert again["ok"] and again["proposal"]["counts"]["removed"] >= 1 and again["proposal"]["netlist_ok"] is True, again
    dry = sess.call_tool("sch_apply", {"ops": [{"op": "delete", "ref": "R2"}], "dry_run": True})
    assert dry["ok"] and dry["proposal"] is None and value_of(d, "R2") == "4k7"
    bad = sess.call_tool("sch_apply", {"ops": [{"op": "delete", "ref": "R99"}], "preview": False})
    assert not bad["ok"] and bad["proposal"] is None


def test_sub_sheet_and_shared_sheet_edits_patch_the_right_file():
    demos = Path(env.kicad().share) / "demos"
    for folder, sheet_arg, ref, n_paths in (("pic_programmer", "pic_sockets.kicad_sch", "C6", 1),
                                            ("complex_hierarchy", "ampli_ht.kicad_sch", "R201", 2)):
        d = open_copy("sub", demos / folder)
        for f in sorted(d.rglob("*.kicad_sch")):
            if ".copper" not in f.parts:
                assert env.run(["sch", "upgrade", "--force", str(f)], timeout=180).returncode == 0
        host = FileHost(d)
        sess = LiveSession(host, str(d))
        root = _project(d).sch
        root_before = root.read_bytes()
        uid = symbol_uuid(d, ref, sheet_arg)
        out = sess.call_tool("sch_apply", {"sheet": sheet_arg, "preview": False,
                                           "ops": [{"op": "set_field", "ref": ref, "field": "Value", "value": "123n"}]})
        assert out["ok"] and out["proposal"]["netlist_ok"] is True, out["text"][:600]
        ap = host.calls[-1][1]
        assert [p["file"] for p in ap["patches"]] == [sheet_arg] and ap["patches"][0]["remove"] == [uid], ap["patches"][0]["remove"]
        assert ap["patches"][0]["sheet_path"] != "/" and root.read_bytes() == root_before, ap["patches"][0]["sheet_path"]
        node = SP.parse_add(ap["patches"][0]["add"])[0]
        paths = [p for proj in sexpr.find_all(sexpr.find(node, "instances"), "project") for p in sexpr.find_all(proj, "path")]
        assert len(paths) == n_paths, [p[1] for p in paths]                # a shared sheet's item names every use
        assert value_of(d, ref, sheet_arg) == "123n" and symbol_uuid(d, ref, sheet_arg) == uid


def _main() -> int:
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
             and (not only or any(o in n for o in only))]
    failed = 0
    for n, f in tests:
        t0 = time.time()
        try:
            f()
            print(f"PASS {n} ({time.time() - t0:.1f}s)", flush=True)
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {n}\n{traceback.format_exc()}", flush=True)
    route.forget()
    if "route_s" in _STATE:
        print(f"NOTE one live set_field through route.call_tool (checkout, engine, netlist x2, apply): {_STATE['route_s']} s")
    if not failed and not os.environ.get("COPPER_TEST_KEEP") and not _BASE:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
