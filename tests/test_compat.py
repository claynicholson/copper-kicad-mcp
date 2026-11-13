"""KiCad compatibility: versions, install selection, file-format guards,
kicad-cli capability gating, known-issue advisories, the update check, the
doctor table, unpacked symbol libraries and the pcbnew worker guard.

Two kinds of test live here:

* pure ones (no KiCad needed) — version parsing, selection, the update
  check, ERC scale, and everything about KiCad versions that are NOT
  installed, which runs against ``tests/data/fake_kicad_cli.py`` (a model of
  kicad-cli built from the real 10.0.6 command tree plus release notes);
* ones that use whatever KiCad ``env.kicad()`` finds and SKIP without it.

Every KiCad run uses a temporary KICAD_CONFIG_HOME and COPPER_HOME.

    python tests/test_compat.py            (no pytest needed)
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "tests" / "data"
sys.path.insert(0, str(ROOT / "src"))

_TMP = Path(tempfile.mkdtemp(prefix="copper-test-compat-"))
os.environ["COPPER_HOME"] = str(_TMP / "home")
os.environ["KICAD_CONFIG_HOME"] = str(_TMP / "kicad-config")       # unversioned: KiCad appends 10.0 itself
os.environ["COPPER_NO_UPDATE_CHECK"] = "1"
for _k in ("COPPER_KICAD_VERSION", "COPPER_KICAD_CLI", "KICAD_CLI", "COPPER_KICAD_ALLOW_PRERELEASE"):
    os.environ.pop(_k, None)
(_TMP / "kicad-config").mkdir(parents=True, exist_ok=True)

from copper_kicad import project as P  # noqa: E402
from copper_kicad.kicad import compat, compat_report, env  # noqa: E402
from copper_kicad.mcp import registry  # noqa: E402
from copper_kicad.mcp.registry import ToolError  # noqa: E402

V = compat.parse_version
SKIPPED: list[str] = []


class Skip(Exception):
    pass


def real_kicad():
    """The real active install, or skip the test."""
    try:
        with use_installs(None):
            k = env.kicad()
    except ToolError:
        raise Skip("no KiCad installed")
    if k.ver is None or k.ver.series != "10.0":
        raise Skip(f"needs a KiCad 10.0.x (found {k.full_version})")
    return k


def demos() -> Path:
    d = real_kicad().share / "demos"
    if not d.is_dir():
        raise Skip("KiCad demos are not installed")
    return d


def demo_copy(name: str, tag: str) -> P.Project:
    src = demos() / name
    dst = _TMP / f"{name}-{tag}"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    for f in dst.rglob("*"):
        if f.is_file():
            os.chmod(f, 0o666)
    pro = next(dst.glob("*.kicad_pro"))
    return P.Project(dst, pro.stem)


V9_DEMOS = ROOT / "tests" / "data" / "demo-v9"


def v9_copy(tag: str) -> P.Project:
    """A real KiCad 9-format design (complex_hierarchy as KiCad 9.0 saved it), frozen in tests/data/demo-v9.
    The installed demo cannot serve: KiCad 10.0.8 re-saved it in the 10.0 format."""
    dst = _TMP / f"complex_hierarchy-{tag}"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(V9_DEMOS / "complex_hierarchy", dst)
    for f in dst.rglob("*"):
        if f.is_file():
            os.chmod(f, 0o666)
    pr = P.Project(dst, "complex_hierarchy")
    for f, want in ((pr.sch, 20250114), (dst / "ampli_ht.kicad_sch", 20250114), (pr.pcb, 20241229)):
        assert compat.file_format(f)["version"] == want, f"tests/data/demo-v9 drifted: {f.name}"
    return pr


def sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def tree_hashes(d: Path) -> dict:
    return {str(f.relative_to(d)): sha(f) for f in sorted(d.rglob("*"))
            if f.is_file() and ".copper" not in f.parts and "copper-out" not in f.parts
            and f.suffix != ".kicad_prl" and f.name != "fp-info-cache"}        # KiCad's own local state files


def restamp(path: Path, stamp: int, gen: str | None = None):
    import re
    s = Path(path).read_text(encoding="utf-8")
    s = re.sub(r"\(version\s+\d{8}\)", f"(version {stamp})", s, count=1)
    if gen:
        s = re.sub(r'\(generator_version\s+"[^"]*"\)', f'(generator_version "{gen}")', s, count=1)
    Path(path).write_text(s, encoding="utf-8", newline="\n")


class use_installs:
    """Make env see exactly these installs (None = the real ones) with these environment variables."""

    def __init__(self, lst, **environ):
        self.lst, self.environ = lst, environ

    def __enter__(self):
        self.saved = env.installs
        self.saved_env = {k: os.environ.get(k) for k in self.environ}
        if self.lst is not None:
            lst = list(self.lst)
            env.installs = lambda: lst
        for k, v in self.environ.items():
            os.environ[k] = v
        env._select_memo.clear()
        env._config_memo.clear()
        env.set_project_format(None)
        return self

    def __exit__(self, *a):
        env.installs = self.saved
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        env._select_memo.clear()
        env._config_memo.clear()
        env.set_project_format(None)
        compat.take_notes()


_fake_n = [0]


def fake_install(version: str, remove: str = "", add: str = "", stamps: str = "", python=None, log: Path | None = None):
    """An Install backed by tests/data/fake_kicad_cli.py reporting ``version``."""
    _fake_n[0] += 1
    cfg = _TMP / f"fake-{_fake_n[0]}-{version}.json"
    d = {"FAKE_KICAD_VERSION": version, "FAKE_KICAD_REMOVE": remove, "FAKE_KICAD_ADD": add, "FAKE_KICAD_STAMPS": stamps}
    if log:
        d["FAKE_KICAD_LOG"] = str(log)
    cfg.write_text(json.dumps(d), encoding="utf-8")
    v = V(version)
    return env.Install(f"fake-{version}", _TMP, cfg, python, None, full_version=str(v), ver=v,
                       cli_argv=[sys.executable, str(DATA / "fake_kicad_cli.py"), "@" + str(cfg)])


# what the KiCad 9.0 CLI documentation does not list (unverified on a real 9.0 binary)
KICAD9_LACKS = ("sch upgrade;pcb upgrade;pcb export stats;pcb drc|--refill-zones;pcb drc|--save-board;"
                "pcb export gerbers|--check-zones;pcb export pdf|--check-zones;pcb export odb|--check-zones")
KICAD11_ADDS = "api-server;sch export png;pcb export png;pcb diff;sch diff;pcb export specctra"


def bare(version: str, python=None):
    """A selection-only Install (never launched)."""
    v = V(version)
    return env.Install(version, Path("/nowhere") / version, Path("/nowhere") / version / "kicad-cli", python, None,
                       full_version=str(v), ver=v)


def call(name, **args):
    r = registry.call_tool(name, args, lambda f, m: None)
    return r.get("structuredContent") or {}, r["isError"]


def _tools():
    from copper_kicad import tools  # noqa: F401  (registers every tool)


# ── T1 versions ───────────────────────────────────────────────────────────
def test_parse_version_fields_and_ordering():
    cases = {
        "10.0.6": (10, 0, 6, "", False, False, "10.0", 10),
        "10.0.8-rc1": (10, 0, 8, "-rc1", False, True, "10.0", 10),
        "10.99.0-3904-gabcdef12": (10, 99, 0, "-3904-gabcdef12", True, True, "10.99", 11),
        "10.99.0-unknown": (10, 99, 0, "-unknown", True, True, "10.99", 11),
        "11.0.0-rc1": (11, 0, 0, "-rc1", False, True, "11.0", 11),
        "Version: 10.0.6, release build": (10, 0, 6, "", False, False, "10.0", 10),
    }
    for text, (ma, mi, pa, extra, nightly, pre, series, eff) in cases.items():
        v = V(text)
        assert (v.major, v.minor, v.patch, v.extra) == (ma, mi, pa, extra), (text, v)
        assert (v.nightly, v.prerelease, v.series, v.effective_major) == (nightly, pre, series, eff), (text, v)
    assert V("") is None and V(None) is None and V("no digits here") is None
    order = sorted([V("10.0.8"), V("10.0.8-rc1"), V("10.0.7"), V("10.99.0-1")], key=compat.newest_key)
    assert [str(v) for v in order] == ["10.0.7", "10.0.8-rc1", "10.0.8", "10.99.0-1"], order
    assert str(V("9.0.9.1")) == "9.0.9.1" and V("9.0.9.1").t == (9, 0, 9)
    assert not hasattr(compat.KiVer, "__lt__") or compat.KiVer.__lt__ is object.__lt__     # no dataclass ordering


# ── T2 selection ──────────────────────────────────────────────────────────
F9 = (("pcb", 20241229), ("sch", 20250114))
F10 = (("pcb", 20260206), ("sch", 20260306))
F11 = (("pcb", 20261031), ("sch", 20261106))


def test_select_table():
    py = Path(sys.executable)                    # "has an engine"
    def mk(*versions):
        return [bare(v, python=py) for v in versions]

    def pick(installs, pin=None, project=None, allow=False, engine=compat.engine_available):
        i, why = compat.select(installs, pin, project, allow, engine)
        return str(i.ver), why
    rows = [
        (mk("10.0.8", "10.99.0-1"), None, None, "10.0.8", "newest stable"),
        (mk("10.0.8", "10.99.0-1"), None, F11, "10.99.0-1", "project is in a pre-release format"),
        (mk("10.0.8", "10.99.0-1"), None, F10, "10.0.8", "newest stable that reads the project"),
        (mk("9.0.9", "10.0.8"), None, F9, "10.0.8", "newest stable that reads the project"),
        (mk("9.0.9", "10.0.8"), None, F10, "10.0.8", "newest stable that reads the project"),
        (mk("9.0.9", "10.0.8"), "9", F10, "9.0.9", "pinned"),
        (mk("10.0.8", "10.99.0-1"), "nightly", F10, "10.99.0-1", "pinned"),
        (mk("10.99.0-1"), None, F10, "10.99.0-1", "only install"),
        (mk("10.0.8", "11.0.0-rc1"), None, None, "10.0.8", "newest stable"),
        (mk("9.0.9"), None, F9, "9.0.9", "only install"),
    ]
    for installs, pin, project, want, why in rows:
        got, reason = pick(installs, pin, project)
        assert got == want and reason.startswith(why), (
            [str(i.ver) for i in installs], pin, project, got, reason)
    # after 11.0.x is "stable": prefer the stable install that can still edit boards ...
    both = mk("10.0.8", "11.0.2")
    got, reason = pick(both, None, F10)
    assert got == "10.0.8" and "can edit boards" in reason, (got, reason)          # KiCad 11 has no pcbnew Python
    # ... and once an engine exists for 11, the newest stable wins
    got, reason = pick(both, None, F10, engine=lambda i: True)
    assert got == "11.0.2", (got, reason)
    # opt-in: a pre-release competes
    got, _ = pick(mk("10.0.8", "10.99.0-1"), None, None, allow=True, engine=lambda i: True)
    assert got == "10.99.0-1"
    # a pin that matches nothing is reported, not obeyed
    got, reason = pick(mk("10.0.8"), "12", None)
    assert got == "10.0.8" and "matches no install" in reason, reason
    # read-only modes say so
    assert "view/verify only" in pick(mk("9.0.9"), None, None)[1]
    assert "blocked" in pick(mk("10.99.0-1"), None, F10)[1]
    try:
        compat.select([], None, None)
        raise AssertionError("empty list must raise")
    except ToolError as e:
        assert e.code == "KICAD_NOT_FOUND"


# ── T3 a nightly next to a stable release ─────────────────────────────────
def test_nightly_never_outranks_stable_and_engine_follows_cli():
    stable = fake_install("10.0.8", python=Path(sys.executable))
    nightly = fake_install("10.99.0-3904-gabcdef12", add=KICAD11_ADDS, stamps="sch=20261106,pcb=20261031,sym=20261106")
    with use_installs([nightly, stable]):
        k = env.kicad()
        assert k.ver.series == "10.0" and k.reason == "newest stable", (k.full_version, k.reason)
        assert env.kicad_python() == Path(sys.executable)
        assert env.config_dir().name == "10.0"
    with use_installs([nightly, stable], COPPER_KICAD_VERSION="nightly"):
        k = env.kicad()
        assert k.ver.series == "10.99" and k.reason.startswith("pinned"), (k.full_version, k.reason)
        assert env.kicad_python() is None, "the stable install's Python must not serve a nightly kicad-cli"
        assert env.config_dir().name == "10.99"
        from copper_kicad.pcb import worker
        try:
            worker.python()
            raise AssertionError("expected PCB_ENGINE_UNAVAILABLE")
        except ToolError as e:
            assert e.code == "PCB_ENGINE_UNAVAILABLE" and "10.0.x" in e.fix, e.to_dict()
        pv = env.path_vars()                                                # T11
        assert "KICAD11_SYMBOL_DIR" not in pv or pv["KICAD11_SYMBOL_DIR"]   # share is None for the fake
    with use_installs([nightly, stable], COPPER_KICAD_ALLOW_PRERELEASE="1"):
        assert env.kicad().ver.series == "10.0", "the stable release can edit boards; the nightly cannot"


def test_path_vars_cover_current_and_next_major():
    share = _TMP / "share-nightly"
    (share / "symbols").mkdir(parents=True, exist_ok=True)
    n = fake_install("10.99.0-1", add=KICAD11_ADDS)
    n.share = share
    with use_installs([n]):
        pv = env.path_vars()
        assert pv["KICAD11_SYMBOL_DIR"] == str(share / "symbols") == pv["KICAD10_SYMBOL_DIR"], pv
        assert "KICAD11_3RD_PARTY" in pv and pv["KICAD11_3RD_PARTY"].replace("\\", "/").endswith("10.99/3rdparty"), pv
    other = _TMP / "stock"
    with use_installs([n], KICAD_STOCK_DATA_HOME=str(other), KICAD_DOCUMENTS_HOME=str(_TMP / "docs")):
        pv = env.path_vars()
        assert pv["KICAD10_FOOTPRINT_DIR"] == str(other / "footprints"), pv
        assert pv["KICAD10_3RD_PARTY"] == str(_TMP / "docs" / "10.99" / "3rdparty"), pv


def test_worker_refuses_a_pcbnew_from_another_series():
    k = real_kicad()
    if not k.python:
        raise Skip("KiCad Python not available")
    pr = demo_copy("complex_hierarchy", "mismatch")
    before = sha(pr.pcb)
    odd = dataclasses.replace(k, ver=V("10.99.0-test"), full_version="10.99.0-test", reason="")
    from copper_kicad.pcb import worker
    with use_installs([odd]):
        try:
            worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "add_text", "text": "X", "at": [100, 100]}],
                                 "save": True}, timeout=120)
            raise AssertionError("expected ENGINE_MISMATCH")
        except ToolError as e:
            assert e.code == "ENGINE_MISMATCH" and "10.99" in e.message and "10.0" in e.message, e.to_dict()
    assert sha(pr.pcb) == before, "board changed although the engine was refused"


# ── T4 file formats ───────────────────────────────────────────────────────
def test_file_format_sniff():
    f = _TMP / "sniff.kicad_sch"
    f.write_bytes(b'\xef\xbb\xbf  \n(kicad_sch (version 20260306) (generator "eeschema") (generator_version "10.0")\n'
                  b'(sheet (property "Sheetname" "A") (property "Sheetfile" "sub/a.kicad_sch"))\n'
                  b'(sheet (property "Sheet file" "b.kicad_sch"))(sheet (property "Sheetfile" "sub/a.kicad_sch")))')
    c = compat.file_format(f)
    assert c == {"kind": "sch", "version": 20260306, "generator_version": "10.0", "subs": ["sub/a.kicad_sch", "b.kicad_sch"]}, c
    g = _TMP / "sniff.kicad_pcb"
    g.write_text('(kicad_pcb\n\t(version 20241229)\n\t(generator "pcbnew")\n)', encoding="utf-8")
    assert compat.file_format(g)["kind"] == "pcb" and compat.file_format(g)["version"] == 20241229
    assert compat.series_of_stamp("pcb", 20241229) == "9.0" and compat.series_of_stamp("sch", 20260306) == "10.0"
    assert compat.series_of_stamp("sch", 20261106) == "newer"
    assert compat.write_stamp("sch") == 20260306 and compat.write_stamp("pcb", bare("11.0.2")) == 20260206


def test_too_new_project_is_refused_everywhere_and_stays_restorable():
    real_kicad()
    _tools()
    pr = demo_copy("complex_hierarchy", "future")
    orig = compat.file_format(pr.sch)["version"]      # 20250114 up to KiCad 10.0.7, 20260306 from 10.0.8
    P.checkpoint(pr, "before", dedupe=False)
    restamp(pr.sch, 20261106, "10.99")
    restamp(pr.pcb, 20261031, "10.99")
    before = tree_hashes(pr.dir)
    with use_installs(None):
        for tool, args in [("verify", {"what": ["erc"]}), ("verify", {"what": ["drc"]}), ("project_summary", {}),
                           ("sch_apply", {"ops": [{"op": "text", "text": "x", "at": [50, 50]}]}),
                           ("pcb_place", {}), ("render", {"target": "schematic"}), ("fab_export", {})]:
            if tool not in registry.TOOLS:
                continue
            d, err = call(tool, project=str(pr.dir), **args)
            assert err and d.get("error") == "FORMAT_TOO_NEW", (tool, d)
            text = json.dumps(d)
            assert "10.99" in text and "2026110" in text or "20261031" in text, (tool, d)
            assert "Copper bug" not in text, (tool, d)
            names = {Path(f["file"]).name for f in d["files"]}
            assert {pr.sch.name, pr.pcb.name} <= names, (tool, names)
        # low level: the launcher and the worker refuse too (non-project call sites)
        for fn in (lambda: env.run(["sch", "export", "netlist", "-o", str(_TMP / "n.net"), str(pr.sch)]),
                   lambda: __import__("copper_kicad.pcb.worker", fromlist=["x"]).run(
                       "board", {"pcb": str(pr.pcb), "ops": [], "save": False})):
            try:
                fn()
                raise AssertionError("expected FORMAT_TOO_NEW")
            except ToolError as e:
                assert e.code == "FORMAT_TOO_NEW", e.to_dict()
        # status / open / checkpoint report instead of failing
        d, err = call("project_open", path=str(pr.dir))
        assert not err and d["format_problem"]["error"] == "FORMAT_TOO_NEW" and d["format"]["series"] == "newer", d
        d, err = call("copper_status")
        assert not err and d["active_project"]["format_problem"]["error"] == "FORMAT_TOO_NEW", d.get("active_project")
        d, err = call("checkpoint", action="list", project=str(pr.dir))
        assert not err and d["total"] >= 1, d
        assert tree_hashes(pr.dir) == before, "a refused call modified a design file"
        cid = d["checkpoints"][-1] if isinstance(d["checkpoints"][-1], str) else d["checkpoints"][-1]["id"]
        d, err = call("checkpoint", action="restore", id=cid, project=str(pr.dir))
        assert not err, d
        assert compat.file_format(pr.sch)["version"] == orig, "restore must bring the readable file back"


def test_too_new_subsheet_is_caught_before_kicad_drops_it_silently():
    real_kicad()
    _tools()
    pr = demo_copy("complex_hierarchy", "subsheet")
    sub = pr.dir / "ampli_ht.kicad_sch"
    restamp(sub, 20261106, "10.99")
    import subprocess
    calls = []
    real_run = subprocess.run

    def counting(cmd, *a, **k):
        # count kicad-cli runs ON THE PROJECT (asking the binary which formats it writes, in a temp dir, is fine)
        if cmd and "kicad-cli" in str(cmd[0]).lower() and any(str(pr.dir) in str(a) for a in cmd[1:]):
            calls.append(cmd[1:4])
        return real_run(cmd, *a, **k)
    with use_installs(None):
        subprocess.run = counting
        try:
            d, err = call("verify", what=["erc"], project=str(pr.dir))
            assert err and d["error"] == "FORMAT_TOO_NEW" and "ampli_ht.kicad_sch" in d["message"], d
            assert [Path(f["file"]).name for f in d["files"]] == ["ampli_ht.kicad_sch"], d["files"]
            from copper_kicad.pcb import board
            try:
                board.netlist(pr)                       # no project preflight on this path: the launcher catches it
                raise AssertionError("expected FORMAT_TOO_NEW")
            except ToolError as e:
                assert e.code == "FORMAT_TOO_NEW" and "ampli_ht" in e.message
        finally:
            subprocess.run = real_run
    assert calls == [], f"kicad-cli was started on a project it would read incompletely: {calls}"


def test_multi_instance_sheet_listed_once_and_preflight_is_cheap():
    d = demos() / "multichannel"
    pr = P.Project(d, "multichannel_mixer")
    compat._fmt_cache.clear()
    t = time.perf_counter()
    st = compat.project_stamps(pr)
    cold = (time.perf_counter() - t) * 1000
    names = [Path(f).name for f, _k, _s in st]
    assert names.count("channel_strip.kicad_sch") == 1 and "multichannel_mixer.kicad_sch" in names, names
    best = min(_timed(lambda: compat.project_stamps(pr)) for _ in range(20))
    assert best < 2.0, f"warm project_stamps {best:.2f} ms"
    print(f"   NOTE multichannel: {len(st)} files, cold {cold:.1f} ms, warm {best:.3f} ms")


def _timed(fn) -> float:
    t = time.perf_counter()
    fn()
    return (time.perf_counter() - t) * 1000


def test_resolve_overhead_budget():
    """T15: the whole-project preflight on a 16-sheet design."""
    d = demos() / "jetson-agx-thor-baseboard"
    if not d.is_dir():
        raise Skip("jetson demo not installed")
    with use_installs(None):
        env.kicad()
        compat._fmt_cache.clear()
        cold = _timed(lambda: P.resolve(str(d), activate=False))
        warm = min(_timed(lambda: P.resolve(str(d), activate=False)) for _ in range(20))
        pr = P.resolve(str(d), activate=False)
        n = len(compat.project_stamps(pr))
        bare_ms = min(_timed(lambda: P._resolve(str(d), True)) for _ in range(20))
    print(f"   NOTE jetson: {n} files; resolve cold {cold:.0f} ms, warm {warm:.2f} ms (path resolution alone {bare_ms:.2f} ms)")
    assert n >= 16, n
    assert warm - bare_ms <= 2.0, f"warm preflight adds {warm - bare_ms:.2f} ms"
    assert cold <= 300, f"cold preflight {cold:.0f} ms"


def test_old_design_is_upgraded_once_with_a_checkpoint_and_identical_netlist():
    real_kicad()
    _tools()
    pr = v9_copy("v9")
    orig = {f.name: f.read_bytes() for f in (pr.sch, pr.pcb, pr.dir / "ampli_ht.kicad_sch")}
    assert compat.file_format(pr.sch)["version"] == 20250114 and compat.file_format(pr.pcb)["version"] == 20241229

    def netlist_tail():
        out = _TMP / "v9.net"
        r = env.run(["sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(out), str(pr.sch)])
        assert out.exists(), r.stderr
        s = out.read_text(encoding="utf-8")
        return s[s.index("(components"):]
    with use_installs(None):
        pr = P.resolve(str(pr.dir), activate=False)
        assert env.kicad().ver.series == "10.0"
        d, err = call("project_open", path=str(pr.dir))
        assert not err and d["format"]["series"] == "9.0" and d["format"]["sch"] == 20250114, d.get("format")
        before = netlist_tail()
        # 1. a no-op edit of the root sheet: guard, then KiCad's own canonical re-save
        from copper_kicad.sch import model
        compat.take_notes()
        P.guard_writable(pr.sch)
        assert model.canonicalize_file(pr.sch)["canonicalized"]
        notes = compat.take_notes()
        up = notes["format_upgraded"]
        assert len(up) == 1 and up[0]["files"] == [pr.sch.name] and up[0]["from"] == "9.0" and up[0]["to"] == "10.0", notes
        cid = up[0]["checkpoint"]
        assert cid and (pr.dir / ".copper" / "snapshots" / cid / pr.sch.name).read_bytes() == orig[pr.sch.name]
        assert compat.file_format(pr.sch)["version"] == 20260306
        assert compat.file_format(pr.dir / "ampli_ht.kicad_sch")["version"] == 20250114, "untouched sub-sheet was rewritten"
        assert netlist_tail() == before, "netlist changed across the format upgrade"
        # 2. a real schematic edit through the tool: no second note for the same file
        d, err = call("sch_apply", ops=[{"op": "text", "text": "compat test", "at": [40, 40]}], project=str(pr.dir))
        assert not err, d
        assert "format_upgraded" not in d, d.get("format_upgraded")
        # 3. a board edit: its own one-time note, attached to the tool result by the dispatcher
        from copper_kicad.pcb import board
        board.run_ops(pr, [{"op": "add_text", "text": "compat", "at": [100, 100]}])
        assert compat.file_format(pr.pcb)["version"] == 20260206
        d, err = call("verify", what=["erc"], project=str(pr.dir))
        assert not err, d
        fu = d.get("format_upgraded")
        assert fu and fu["files"] == [pr.pcb.name] and fu["checkpoint"], d.keys()
        d2, err = call("verify", what=["erc"], project=str(pr.dir))
        assert not err and "format_upgraded" not in d2
        board.run_ops(pr, [{"op": "add_text", "text": "again", "at": [110, 100]}])
        assert "format_upgraded" not in compat.take_notes(), "the note must appear once per file"
        st = json.loads((pr.dir / ".copper" / "state.json").read_text(encoding="utf-8"))
        assert set(st["format_upgraded"]) == {pr.sch.name, pr.pcb.name}, st
        # a sub-sheet kept in a sub-folder belongs to the project above it (state + snapshot live there)
        sub = pr.dir / "sheets"
        sub.mkdir()
        shutil.copy2(V9_DEMOS / "complex_hierarchy" / "ampli_ht.kicad_sch", sub / "amp2.kicad_sch")
        amp2_orig = (sub / "amp2.kicad_sch").read_bytes()
        P.guard_writable(sub / "amp2.kicad_sch")
        assert "format_upgraded" not in compat.take_notes(), "a guard without a write must not report an upgrade"
        assert "sheets/amp2.kicad_sch" not in json.loads((pr.dir / ".copper" / "state.json").read_text("utf-8"))["format_upgraded"]
        P.guard_writable(sub / "amp2.kicad_sch")
        assert model.canonicalize_file(sub / "amp2.kicad_sch")["canonicalized"]
        assert not list(sub.glob("*.kicad_pro")), "a project file was created next to a sub-sheet"
        n = compat.take_notes()["format_upgraded"][0]
        assert n["files"] == ["sheets/amp2.kicad_sch"] and n["checkpoint"] and not (sub / ".copper").exists(), n
        # no sheet of the project references amp2, so the checkpoint cannot hold it: the original is kept apart
        assert Path(n["backup"]).read_bytes() == amp2_orig and "original is kept at" in n["note"], n
        for f in sub.iterdir():
            f.unlink()
        sub.rmdir()
        # 4. the checkpoint taken before the first upgrade restores byte-identical KiCad 9 files
        P.restore_checkpoint(pr, cid)
        for name in (pr.sch.name, pr.pcb.name, "ampli_ht.kicad_sch"):
            assert (pr.dir / name).read_bytes() == orig[name], f"{name} differs after restore"


def test_drc_save_board_goes_through_the_write_guard():
    k = real_kicad()
    pr = v9_copy("drcsave")
    old = dataclasses.replace(k, ver=V("10.99.0-test"), full_version="10.99.0-test")
    before = sha(pr.pcb)
    from copper_kicad.pcb import board
    with use_installs([old]):
        pr = P.resolve(str(pr.dir), activate=False)
        try:
            board.drc(pr, save_refill=True, record=False)
            raise AssertionError("expected FORMAT_UPGRADE_BLOCKED")
        except ToolError as e:
            assert e.code == "FORMAT_UPGRADE_BLOCKED", e.to_dict()
    assert sha(pr.pcb) == before


def test_prerelease_never_upgrades_a_stable_format_file_without_consent():
    k = real_kicad()
    _tools()
    pr = v9_copy("prerelease")
    before = tree_hashes(pr.dir)
    pre = dataclasses.replace(k, ver=V("10.99.0-test"), full_version="10.99.0-test")
    with use_installs([pre]):
        d, err = call("project_open", path=str(pr.dir))
        assert not err and d["format"].get("prerelease") and d["format"]["allow_format_upgrade"] is False, d.get("format")
        d, err = call("sch_apply", ops=[{"op": "text", "text": "x", "at": [40, 40]}], project=str(pr.dir))
        assert err and d["error"] == "FORMAT_UPGRADE_BLOCKED" and "allow_format_upgrade" in d["fix"], d
        assert "unreadable in KiCad 9.0" in d["message"], d["message"]
        assert tree_hashes(pr.dir) == before, "a blocked write changed a file"
        d, err = call("project_open", path=str(pr.dir), allow_format_upgrade=True)
        assert not err and d["format"]["allow_format_upgrade"] is True, d.get("format")
        d, err = call("sch_apply", ops=[{"op": "text", "text": "x", "at": [40, 40]}], project=str(pr.dir))
        assert not err, d
        fu = d.get("format_upgraded")
        assert fu and fu["files"] == [pr.sch.name] and fu["to"] == "10.99" and fu["checkpoint"], d.keys()
        d, err = call("project_open", path=str(pr.dir), allow_format_upgrade=False)       # consent can be withdrawn
        assert d["format"]["allow_format_upgrade"] is False


# ── T5 worker config dir ──────────────────────────────────────────────────
def test_worker_uses_the_real_config_dir_not_a_nested_one():
    k = real_kicad()
    if not k.python:
        raise Skip("KiCad Python not available")
    cfg = _TMP / "cfg-t5"
    (cfg / "10.0").mkdir(parents=True, exist_ok=True)
    (cfg / "10.0" / "marker.json").write_text('{"keep": true}', encoding="utf-8")
    pr = demo_copy("complex_hierarchy", "t5")
    with use_installs(None, KICAD_CONFIG_HOME=str(cfg)):
        env.kicad()
        from copper_kicad.pcb import board
        board.run_ops(pr, [{"op": "add_text", "text": "t5", "at": [100, 100]}])
        board.drc(pr, record=False)
        assert env.config_dir() == cfg / "10.0"
        assert compat_report.nested_config_dir() is None
    assert (cfg / "10.0").is_dir() and not (cfg / "10.0" / "10.0").exists(), sorted(p.name for p in (cfg / "10.0").iterdir())
    assert (cfg / "10.0" / "marker.json").read_text(encoding="utf-8") == '{"keep": true}'
    # the leftover of the old bug is reported, and removed only when empty
    (cfg / "10.0" / "10.0" / "colors").mkdir(parents=True)
    with use_installs(None, KICAD_CONFIG_HOME=str(cfg)):
        assert compat_report.nested_config_dir() == str(cfg / "10.0" / "10.0")
        rows = {c["id"]: c for c in compat_report.checks(compat_report.table(probe=False))}
        assert rows["config_nested"]["status"] == "warn" and "safe to delete" in rows["config_nested"]["detail"]
        (cfg / "10.0" / "10.0" / "colors" / "user.json").write_text("{}", encoding="utf-8")
        r = compat_report.fix_nested_config_dir()
        assert not r["removed"] and "not empty" in r["reason"], r
        (cfg / "10.0" / "10.0" / "colors" / "user.json").unlink()
        assert compat_report.fix_nested_config_dir()["removed"]
        assert not (cfg / "10.0" / "10.0").exists() and (cfg / "10.0" / "marker.json").exists()


# ── worker watchdog ───────────────────────────────────────────────────────
def test_worker_does_not_hang_on_a_missing_font():
    """pcbnew raises a modal wxLog dialog ("Font not found") inside LoadBoard: without the guard the worker
    blocks until the server's timeout (measured: still blocked after 45 s; with the guard 0.4 s)."""
    k = real_kicad()
    if not k.python:
        raise Skip("KiCad Python not available")
    pr = demo_copy("complex_hierarchy", "font")
    s = pr.pcb.read_text(encoding="utf-8").rstrip()
    assert s.endswith(")")
    s = s[:-1] + ('\t(gr_text "FONT"\n\t\t(at 100 100 0)\n\t\t(layer "F.SilkS")\n'
                  '\t\t(uuid "11111111-2222-3333-4444-555555555555")\n\t\t(effects\n\t\t\t(font\n'
                  '\t\t\t\t(face "NoSuchFontXYZ123")\n\t\t\t\t(size 1.5 1.5)\n\t\t\t\t(thickness 0.3)\n\t\t\t)\n\t\t)\n\t)\n)\n')
    pr.pcb.write_text(s, encoding="utf-8", newline="\n")
    from copper_kicad.pcb import worker
    with use_installs(None):
        for script, req in (("board", {"pcb": str(pr.pcb), "ops": [{"op": "read", "what": ["summary"]}], "save": False}),
                            ("guided_board", {"pcb": str(pr.pcb), "ops": [{"op": "stats"}], "save": False})):
            t = time.perf_counter()
            worker.run(script, req, timeout=60)
            dt = time.perf_counter() - t
            assert dt < 30, f"{script} took {dt:.1f} s on a board with a missing font"
            print(f"   NOTE {script} worker loaded a board with a missing font in {dt:.1f} s")


def test_worker_watchdog_kills_a_stuck_worker_and_reports_it():
    k = real_kicad()
    if not k.python:
        raise Skip("KiCad Python not available")
    from copper_kicad.pcb import worker
    wd = _TMP / "stuck-workers"
    wd.mkdir(exist_ok=True)
    pidfile = wd / "pid.txt"
    (wd / "stuck.py").write_text(
        "import os, sys, time\nopen(%r, 'w').write(str(os.getpid()))\nsys.stdin.read()\ntime.sleep(600)\n" % str(pidfile),
        encoding="utf-8")
    (wd / "silent.py").write_text("import sys\nsys.stdin.read()\nprint('no result line')\n", encoding="utf-8")
    saved = worker.WORKERS
    worker.WORKERS = wd
    try:
        with use_installs(None):
            t = time.perf_counter()
            try:
                worker.run("stuck", {"ops": []}, timeout=3)
                raise AssertionError("expected WORKER_TIMEOUT")
            except ToolError as e:
                assert e.code == "WORKER_TIMEOUT" and "not modified" in e.fix, e.to_dict()
            dt = time.perf_counter() - t
            assert dt < 15, f"the watchdog took {dt:.1f} s for a 3 s limit"
            try:
                worker.run("silent", {"ops": []}, timeout=30)
                raise AssertionError("expected WORKER_CRASHED")
            except ToolError as e:
                assert e.code == "WORKER_CRASHED", e.to_dict()
    finally:
        worker.WORKERS = saved
    pid = int(pidfile.read_text())
    time.sleep(0.5)
    assert not _alive(pid), f"stuck worker {pid} is still running"


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import subprocess
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_both_workers_start_the_shared_guard_before_loading_a_board():
    wdir = ROOT / "src" / "copper_kicad" / "pcb" / "workers"
    for name in ("board.py", "guided_board.py"):
        s = (wdir / name).read_text(encoding="utf-8")
        assert "_kiguard.start(P, MARK)" in s, name
        assert s.index("_kiguard.start(P, MARK)") < s.index("LoadBoard("), f"{name}: guard must run before LoadBoard"
    sys.path.insert(0, str(wdir))
    try:
        import _kiguard

        class Fake:
            @staticmethod
            def Version():
                return "10.0.6"
        assert _kiguard.series_of(Fake) == "10.0"
        assert _kiguard.mismatch(Fake, "10.0") is None and _kiguard.mismatch(Fake, "") is None
        assert "10.99" in _kiguard.mismatch(Fake, "10.99")
    finally:
        sys.path.remove(str(wdir))


# ── T6 ERC coordinate scale ───────────────────────────────────────────────
def test_erc_scale_is_measured_first_and_versioned_second():
    from copper_kicad.review import verify

    def data(ver, items):
        d = {"coordinate_units": "mm", "sheets": [{"violations": [{"items": items}]}]}
        if ver:
            d["kicad_version"] = ver
        return d
    saved = verify._symbol_positions
    verify._symbol_positions = lambda pr: {"u1": (120.65, 80.01), "u2": (0.0, 55.88)}
    try:
        cases = [
            ("a: 100x small, uuid match", data("10.0.6", [{"uuid": "u1", "pos": {"x": 1.2065, "y": 0.8001}}]), 100.0),
            ("b: correct scale, uuid match, old version", data("10.0.6", [{"uuid": "u1", "pos": {"x": 120.65, "y": 80.01}}]), 1.0),
            ("b2: 100x small, uuid match, NEW version", data("10.0.8", [{"uuid": "u1", "pos": {"x": 1.2065, "y": 0.8001}}]), 100.0),
            ("y axis when x is 0", data("10.0.8", [{"uuid": "u2", "pos": {"x": 0, "y": 0.5588}}]), 100.0),
            ("c: no match, 10.0.8, max coord 8 mm", data("10.0.8", [{"uuid": "zz", "pos": {"x": 8.0, "y": 3.0}}]), 1.0),
            ("c2: no match, 10.0.7-rc1 has the fix number", data("10.0.7-rc1", [{"uuid": "zz", "pos": {"x": 8.0, "y": 3.0}}]), 1.0),
            ("d: no match, 10.0.6", data("10.0.6", [{"uuid": "zz", "pos": {"x": 8.0, "y": 3.0}}]), 100.0),
            ("e: version missing", data(None, [{"uuid": "zz", "pos": {"x": 150.0, "y": 90.0}}]), 100.0),
            ("no items", {"coordinate_units": "mm", "sheets": []}, 1.0),
        ]
        for name, d, want in cases:
            got = verify._erc_scale(d, None)
            assert got == want, (name, got, want)
        mils = data("10.0.8", [{"uuid": "zz", "pos": {"x": 300, "y": 100}}])
        mils["coordinate_units"] = "mils"
        assert verify._erc_scale(mils, None) == 0.0254
    finally:
        verify._symbol_positions = saved


def test_erc_positions_match_symbol_positions_on_the_installed_kicad():
    real_kicad()
    from copper_kicad.review import verify
    pr = demo_copy("pic_programmer", "erc")
    with use_installs(None):
        pr = P.resolve(str(pr.dir), activate=False)
        out = verify.work_dir(pr) / "erc-raw.json"
        env.run(["sch", "erc", "--format", "json", "--severity-all", "--units", "mm", "-o", str(out), str(pr.sch)])
        data = json.loads(out.read_text(encoding="utf-8"))
        scale = verify._erc_scale(data, pr)
        sym = verify._symbol_positions(pr)
        checked = 0
        for s in data.get("sheets", []):
            for v in s.get("violations", []):
                for i in v.get("items", []):
                    p = sym.get(i.get("uuid"))
                    if p and i.get("pos"):
                        assert abs(i["pos"]["x"] * scale - p[0]) < 0.01 and abs(i["pos"]["y"] * scale - p[1]) < 0.01, (i, p, scale)
                        checked += 1
        if not checked:
            raise Skip("the demo's ERC has no symbol-anchored marker")
        print(f"   NOTE KiCad {data.get('kicad_version')}: ERC scale x{scale:g}, {checked} marker(s) within 0.01 mm")


# ── T7 capabilities ───────────────────────────────────────────────────────
def test_caps_on_the_real_cli_are_probed_once_and_persisted():
    k = real_kicad()
    cache = _TMP / "caps-real"
    c = compat.Caps(k.cli, cache)
    assert c.has("pcb drc", "--refill-zones") and c.has("sch upgrade") and c.has("pcb export stats")
    assert c.has("sch export netlist", "--variant")
    assert not c.has("sch export png") and not c.has("api-server") and not c.has("pcb drc", "--no-such-flag")
    assert "netlist" in c.subs("sch export")
    assert c.spawns > 0
    c2 = compat.Caps(k.cli, cache)                               # "second process"
    assert c2.has("pcb drc", "--refill-zones") and not c2.has("sch export png") and c2.spawns == 0
    args, dropped = c2.filter("pcb drc", ["pcb", "drc", "--refill-zones", "--bogus", "v", "--bogus2=1", "x.kicad_pcb"],
                              {"--refill-zones": 0, "--bogus": 1, "--bogus2": 1})
    assert args == ["pcb", "drc", "--refill-zones", "x.kicad_pcb"] and dropped == ["--bogus", "--bogus2"], (args, dropped)
    # a different binary identity gets its own cache file; a corrupt cache is rebuilt
    other = _TMP / "other-cli.exe"
    shutil.copy2(k.cli, other) if False else other.write_bytes(b"x")
    assert compat.Caps(other, cache).key != c.key
    c.file.write_text("{not json", encoding="utf-8")
    c3 = compat.Caps(k.cli, cache)
    assert c3.has("pcb drc", "--save-board") and c3.spawns > 0
    with use_installs(None):
        got = compat.learned_stamps(env.kicad())
    assert got == compat.FORMATS["10.0"], got


def test_caps_fail_open_when_help_cannot_be_parsed():
    junk = _TMP / "junk-cli.py"
    junk.write_text("import sys\nprint('segfault')\nsys.exit(3)\n", encoding="utf-8")
    c = compat.Caps(junk, _TMP / "caps-junk", [sys.executable, str(junk)])
    assert c.has("pcb drc", "--refill-zones") and c.has("anything at all"), "a failed probe must never drop a flag"
    args, dropped = c.filter("pcb drc", ["pcb", "drc", "--refill-zones"], {"--refill-zones": 0})
    assert dropped == [] and args == ["pcb", "drc", "--refill-zones"]
    missing = compat.Caps(_TMP / "no-such-binary", _TMP / "caps-junk", [str(_TMP / "no-such-binary")])
    assert missing.has("pcb drc", "--refill-zones")


def test_every_kicad_cli_flag_copper_passes_is_known():
    """Each (command, --flag) literal in a kicad-cli argument list exists in the recorded 10.0 command tree
    (tests/data/cli-10.0.json) — so a typo or a flag from a newer KiCad cannot ship ungated."""
    tree = json.loads((DATA / "cli-10.0.json").read_text(encoding="utf-8"))["help"]
    roots = set(tree[""]["subs"])
    seen, problems = 0, []
    for f in (ROOT / "src" / "copper_kicad").rglob("*.py"):
        try:
            mod = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue                                   # a file another change is in the middle of
        for node in ast.walk(mod):
            if not isinstance(node, ast.List) or len(node.elts) < 2:
                continue
            lead = []
            for e in node.elts:
                if isinstance(e, ast.Constant) and isinstance(e.value, str) and not e.value.startswith("-"):
                    lead.append(e.value)
                else:
                    break
            if not lead or lead[0] not in roots or lead[0] == "version":
                continue
            cmd, bad = "", None
            for tok in lead:
                if tok in tree[cmd]["subs"]:
                    cmd = (cmd + " " + tok).strip()
                else:
                    bad = tok if tree[cmd]["subs"] else None       # a group was expecting a subcommand
                    break
            if bad is not None:
                if cmd.count(" ") >= 1:                             # e.g. ["sch", "export", "png", ...]
                    problems.append(f"{f.name}:{node.lineno}: KiCad 10.0 has no `{cmd} {bad}`")
                continue                                            # ["sym", "fp"]: not a kicad-cli argument list
            if tree[cmd]["subs"]:
                continue                                            # a prefix that is completed elsewhere
            for e in node.elts:
                if isinstance(e, ast.Constant) and isinstance(e.value, str) and e.value.startswith("--"):
                    seen += 1
                    flag = e.value.split("=", 1)[0]
                    if flag not in tree[cmd]["flags"]:
                        problems.append(f"{f.name}:{node.lineno}: `{cmd}` has no {flag} in KiCad {tree and '10.0'}")
    assert not problems, "\n".join(problems)
    assert seen >= 20, f"only {seen} flags found: the scan no longer sees the call sites"
    for cmd, flags in compat.OPTIONAL_FLAGS.items():
        for flag in flags:
            assert flag in tree[cmd]["flags"], f"OPTIONAL_FLAGS names {cmd} {flag}, which KiCad 10.0 does not have"


# ── T8 an older KiCad (modelled) ──────────────────────────────────────────
def test_kicad9_model_drops_missing_flags_and_refuses_edits():
    log = _TMP / "fake9.log"
    k9 = fake_install("9.0.9", remove=KICAD9_LACKS, log=log)
    pr = _v9_fixture("k9")
    with use_installs([k9]):
        k = env.kicad()
        assert k.ver.series == "9.0" and "view/verify only" in k.reason, k.reason
        pr = P.resolve(str(pr.dir), activate=False)                         # 9-format files: readable by 9
        c = compat.caps(k)
        assert not c.has("sch upgrade") and not c.has("pcb drc", "--refill-zones") and c.has("pcb drc", "--schematic-parity")
        assert not c.has("pcb export stats") and c.has("sch export netlist")
        from copper_kicad.pcb import board
        compat.take_notes()
        r = board.drc(pr, record=False)
        assert r["errors"] == 0 and r["unconnected"] == 0, r
        sent = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        drc = [a for a in sent if a[:2] == ["pcb", "drc"] and "--help" not in a][-1]
        assert "--refill-zones" not in drc and "--schematic-parity" in drc, drc
        notes = compat.take_notes()
        assert any("zones were not refilled before DRC" in n and "9.0.9" in n for n in notes.get("kicad_notes", [])), notes
        r = env.run(["pcb", "export", "gerbers", "--check-zones", "--no-x2", "-o", str(_TMP / "g"), str(pr.pcb)])
        assert r.returncode == 0 and r.copper_dropped == ["--check-zones"], (r.returncode, r.stdout, r.copper_dropped)
        compat.take_notes()
        for path in (pr.sch, pr.pcb):
            try:
                P.guard_writable(path)
                raise AssertionError("expected KICAD_TOO_OLD")
            except ToolError as e:
                assert e.code == "KICAD_TOO_OLD" and "KiCad 10" in e.fix, e.to_dict()
        assert compat.variant_args(k, "sch export netlist", None) == []
        assert compat.variant_args(k, "sch export netlist", "lite") == ["--variant", "lite"]
        try:
            compat.variant_args(k, "pcb drc", "lite")              # no such option on this command
            raise AssertionError("expected NO_VARIANTS")
        except ToolError as e:
            assert e.code == "NO_VARIANTS", e.to_dict()
        _tools()
        r = registry.call_tool("project_create", {"name": "nine", "dir": str(_TMP / "nine")}, None)
        assert r["isError"] and r["structuredContent"]["error"] == "KICAD_TOO_OLD" and not (_TMP / "nine").exists(), r
        rows = compat_report.checks(compat_report.table())
        row = next(r for r in rows if r["id"] == "kicad")
        assert row["status"] == "warn" and "view/verify only" in row["detail"] and "-sch-upgrade" in row["detail"], row
        assert "-refill-zones" in row["detail"] and "engine none" in row["detail"], row
    # a 10-format project next to it: KiCad 9 cannot read it → refused with the reason
    pr10 = _v9_fixture("k9-new", sch=20260306, pcb=20260206)
    with use_installs([k9]):
        try:
            P.resolve(str(pr10.dir), activate=False)
            raise AssertionError("expected FORMAT_TOO_NEW")
        except ToolError as e:
            assert e.code == "FORMAT_TOO_NEW" and "9.0.9" in e.message, e.to_dict()


def _v9_fixture(tag: str, sch: int = 20250114, pcb: int = 20241229) -> P.Project:
    d = _TMP / f"mini-{tag}"
    d.mkdir(exist_ok=True)
    (d / "mini.kicad_pro").write_text("{}", encoding="utf-8")
    (d / "mini.kicad_sch").write_text(
        f'(kicad_sch\n\t(version {sch})\n\t(generator "eeschema")\n\t(generator_version "x")\n\t(uuid "a")\n\t(paper "A4")\n)\n',
        encoding="utf-8")
    (d / "mini.kicad_pcb").write_text(
        f'(kicad_pcb\n\t(version {pcb})\n\t(generator "pcbnew")\n\t(generator_version "x")\n\t(zone (net 0))\n)\n', encoding="utf-8")
    return P.Project(d, "mini")


def test_newer_point_release_model_changes_nothing_but_the_advisories():
    """10.0.7 / 10.0.8 (modelled from the release notes; 10.0.8 since confirmed on the real binary in CI): same command tree,
    same formats, so no flag is dropped and no upgrade note appears — only the advisories differ."""
    pr = _v9_fixture("k1008", sch=20260306, pcb=20260206)
    for ver, ids in (("10.0.6", {"zone-fill-shorts", "erc-json-scale", "bom-preset-error", "via-padstack-crash"}),
                     ("10.0.7", {"bom-preset-error", "via-padstack-crash"}), ("10.0.8", set()), ("10.0.8-rc1", set())):
        assert {a["id"] for a in compat.advisories(V(ver))} == ids, ver
    assert {a["id"] for a in compat.advisories(V("10.99.0-1"))} == {"swig-removed"}
    assert {a["id"] for a in compat.advisories(V("11.0.0"))} == {"swig-removed"}
    assert "view-only" in {a["id"] for a in compat.advisories(V("9.0.9"))}
    log = _TMP / "fake1008.log"
    k = fake_install("10.0.8", log=log, python=Path(sys.executable))
    with use_installs([k]):
        P.resolve(str(pr.dir), activate=False)
        from copper_kicad.pcb import board
        compat.take_notes()
        board.drc(pr, record=False)
        sent = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        drc = [a for a in sent if a[:2] == ["pcb", "drc"] and "--help" not in a][-1]
        assert "--refill-zones" in drc, drc
        assert compat.take_notes() == {}, "10.0.8 needs no note"
        assert P.guard_writable(pr.sch) is None and P.guard_writable(pr.pcb) is None
        assert compat.take_notes() == {}
    # on 10.0.6 the same DRC adds the zone-fill advisory, once per project
    k6 = fake_install("10.0.6", python=Path(sys.executable))
    with use_installs([k6]):
        compat._advised.clear()
        board.drc(pr, record=False)
        n1 = compat.take_notes()
        board.drc(pr, record=False)
        n2 = compat.take_notes()
        assert len(n1.get("kicad_advisory", [])) == 1 and "10.0.7" in n1["kicad_advisory"][0] and len(n1["kicad_advisory"][0]) <= 200, n1
        assert n2 == {}, n2


def test_kicad11_model_is_detected_by_capability_not_by_version():
    k11 = fake_install("11.0.1", add=KICAD11_ADDS, stamps="sch=20270101,pcb=20270102,sym=20270103")
    k10 = fake_install("10.0.8", python=Path(sys.executable))
    with use_installs([k11, k10]):
        assert env.kicad().ver.series == "10.0" and "can edit boards" in env.kicad().reason, env.kicad().reason
        c = compat.caps(k11)
        assert c.has("api-server") and c.has("pcb export png") and c.has("pcb diff") and c.has("pcb drc", "--refill-zones")
        assert compat.max_stamp(k11, "sch") == 20270101 and compat.max_stamp(k11, "fp") == 20270102, "learned from the binary"
        t = compat_report.table()
        r11 = next(r for r in t["installs"] if r["version"] == "11.0.1")
        assert r11["caps"]["api-server"] and r11["caps"]["png"] and not r11["active"] and r11["engine"].startswith("none"), r11
        assert r11["formats"]["sch"] == 20270101 and any(a["id"] == "swig-removed" for a in r11["advisories"])
    with use_installs([k11]):
        from copper_kicad.pcb import worker
        try:
            worker.python()
            raise AssertionError("expected PCB_ENGINE_UNAVAILABLE")
        except ToolError as e:
            assert e.code == "PCB_ENGINE_UNAVAILABLE" and "removed in KiCad 11" in e.message


# ── T10 unpacked symbol libraries ─────────────────────────────────────────
def test_unpacked_symbol_library_is_indexed_and_read():
    k = real_kicad()
    from copper_kicad.kicad import libs
    pdir = _TMP / "sym dir project"                       # a path with a space
    pdir.mkdir(exist_ok=True)
    out = pdir / "TimerDir.kicad_symdir"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir()                                           # kicad-cli unpacks only into an existing directory
    with use_installs(None):
        shutil.copy2(k.share / "symbols" / "Timer.kicad_sym", pdir / "Timer.kicad_sym")
        r = env.run(["sym", "upgrade", "--force", "-o", str(out), str(pdir / "Timer.kicad_sym")])
        if not list(out.glob("*.kicad_sym")):
            raise Skip(f"this kicad-cli does not unpack libraries: {(r.stderr or r.stdout)[-200:]}")
        files = sorted(out.glob("*.kicad_sym"))
        (pdir / "sym-lib-table").write_text(
            '(sym_lib_table\n  (version 7)\n  (lib (name "TimerDir")(type "KiCad")(uri "${KIPRJMOD}/TimerDir.kicad_symdir")'
            '(options "")(descr ""))\n)\n', encoding="utf-8")
        (pdir / "p.kicad_pro").write_text("{}", encoding="utf-8")
        t = time.perf_counter()
        idx = [e for lib, e, _s in libs.symbol_index(pdir) if lib == "TimerDir"]
        ms = (time.perf_counter() - t) * 1000
        packed = libs._scan_sym_file(str(pdir / "Timer.kicad_sym"))
        assert len(idx) == len(packed) == len(files) and {e["n"] for e in idx} == {e["n"] for e in packed}, (len(idx), len(packed))
        hits = [h["lib_id"] for h in libs.search_symbols("NE555", pdir, limit=40, lib="TimerDir")]
        assert "TimerDir:NE555D" in hits, hits
        ls = libs.read_symbol("TimerDir:8253", pdir)        # derived: (extends "82C54"), the parent is a sibling file
        base = libs.read_symbol("TimerDir:82C54", pdir)
        assert len(libs.parse_pins(ls.tree)) == len(libs.parse_pins(base.tree)) > 10, len(libs.parse_pins(ls.tree))
        d = next(e for e in idx if e["n"] == "8253")
        assert d["p"] == next(e for e in packed if e["n"] == "8253")["p"] > 0, d
        scan = _timed(lambda: libs._scan_sym_file(str(out)))
        print(f"   NOTE unpacked Timer: {len(files)} files, first index {ms:.0f} ms, rescan {scan:.0f} ms")
        assert scan < 250, f"scan of {len(files)} files took {scan:.0f} ms"


# ── T12 doctor table ──────────────────────────────────────────────────────
def test_doctor_table_and_update_row():
    k6 = fake_install("10.0.6", python=Path(sys.executable))
    saved = compat.latest_release
    try:
        with use_installs([k6]):
            compat.latest_release = lambda *a, **k: ({"latest": V("10.0.8"), "newer_major": None} if k.get("detail") else V("10.0.8"))
            t = compat_report.table()
            assert len(t["installs"]) == 1
            row = t["installs"][0]
            for key in ("version", "series", "channel", "active", "reason", "formats", "engine", "caps", "advisories"):
                assert key in row, key
            assert row["version"] == "10.0.6" and row["series"] == "10.0" and row["channel"] == "stable" and row["active"]
            assert row["formats"] == {"sch": 20260306, "pcb": 20260206, "sym": 20251024} and row["engine"] == "swig", row
            assert any(a["id"] == "zone-fill-shorts" for a in row["advisories"])
            checks = compat_report.checks(t)
            assert checks[0]["id"] == "kicad" and checks[0]["status"] == "warn" and "ACTIVE (newest stable)" in checks[0]["detail"]
            up = compat_report.update_check(network=False)
            assert len(up) == 1 and up[0]["id"] == "kicad_update" and up[0]["status"] == "warn", up
            assert "10.0.6 installed, 10.0.8 available" in up[0]["detail"] and "zone fills" in up[0]["detail"], up
            from copper_kicad.install import doctor
            ids = {c.id: c for c in doctor.run(fast=True, network=False, clients=False)}
            assert ids["kicad"].status == "warn" and ids["kicad_update"].status == "warn", ids.keys()
            assert "kicad" in doctor.summarize(list(ids.values())) and doctor.summarize(list(ids.values()))["kicad"]["installs"]
            s = env.summary()
            assert s["active"]["reason"] == "newest stable" and any(a["id"] == "zone-fill-shorts" for a in s["advisories"]), s
            # unknown → no row; a newer MAJOR is information, never "please update"
            compat.latest_release = lambda *a, **k: ({"latest": None, "newer_major": None} if k.get("detail") else None)
            assert compat_report.update_check(network=False) == []
            compat.latest_release = lambda *a, **k: {"latest": V("10.0.6"), "newer_major": V("11.0.0")}
            up = compat_report.update_check(network=False)
            assert [u["id"] for u in up] == ["kicad_update", "kicad_next_major"] and up[0]["status"] == "ok", up
            assert up[1]["status"] == "skip" and "11.0.0 is out" in up[1]["detail"] and "10.0.x" in up[1]["detail"], up
        pre = fake_install("10.99.0-7", add=KICAD11_ADDS)
        with use_installs([pre]):
            compat.latest_release = lambda *a, **k: {"latest": V("10.0.8"), "newer_major": None}
            rows = compat_report.checks(compat_report.table(probe=False))
            assert rows[0]["status"] == "warn" and "nightly" in rows[0]["detail"] and "stable KiCad 10.0.x" in rows[0]["fix"], rows[0]
            assert compat_report.update_check(network=False) == [], "no update nagging for a nightly"
    finally:
        compat.latest_release = saved


# ── T13 update check ──────────────────────────────────────────────────────
def test_latest_release_picks_the_newest_supported_stable_and_caches():
    rows = json.loads((DATA / "kicad-tags.json").read_text(encoding="utf-8"))
    assert {"10.0.8-rc1", "9.0.9.1"} <= {r["name"] for r in rows}, "the recorded tag list lost its awkward entries"
    rows.insert(0, {"name": "11.0.0"})
    lat, newer = compat.pick_latest([r["name"] for r in rows])
    assert str(lat) == "10.0.8" and str(newer) == "11.0.0", (lat, newer)
    assert compat.pick_latest(["11.0.0", "12.0.1", None, 7, "junk"]) == (None, V("12.0.1"))
    home = _TMP / "home-latest"
    calls = []

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return json.dumps(rows).encode()
    saved_get, saved_home = compat._http_get, os.environ["COPPER_HOME"]
    os.environ["COPPER_HOME"] = str(home)
    os.environ.pop("COPPER_NO_UPDATE_CHECK", None)
    try:
        compat._http_get = fake_get
        assert str(compat.latest_release()) == "10.0.8" and len(calls) == 1
        assert calls[0][0] == compat.TAGS_URL and "?" in calls[0][0] and "user" not in calls[0][0].split("?")[1]
        d = compat.latest_release(detail=True)
        assert str(d["latest"]) == "10.0.8" and str(d["newer_major"]) == "11.0.0" and len(calls) == 1, "second call hit the network"
        assert json.loads((home / "cache" / "kicad-latest.json").read_text(encoding="utf-8"))["latest"] == "10.0.8"
        # a failure is cached for an hour: an offline machine pays the timeout once
        shutil.rmtree(home)

        def boom(url, timeout):
            calls.append((url, timeout))
            time.sleep(0.05)
            raise TimeoutError("timed out")
        compat._http_get = boom
        n = len(calls)
        t = time.perf_counter()
        assert compat.latest_release(timeout=3.0) is None
        assert time.perf_counter() - t <= 3.2 and len(calls) == n + 1
        assert compat.latest_release() is None and len(calls) == n + 1, "failure was not cached"
        # opt-out and cache-only never touch the network
        shutil.rmtree(home)
        os.environ["COPPER_NO_UPDATE_CHECK"] = "1"
        assert compat.latest_release() is None and len(calls) == n + 1
        os.environ.pop("COPPER_NO_UPDATE_CHECK")
        assert compat.latest_release(cached_only=True) is None and len(calls) == n + 1
        # an expired cache is refreshed
        compat._http_get = fake_get
        (home / "cache").mkdir(parents=True)
        (home / "cache" / "kicad-latest.json").write_text(json.dumps(
            {"at": time.time() - 90000, "ok": True, "max_major": 10, "latest": "10.0.7"}), encoding="utf-8")
        assert str(compat.latest_release()) == "10.0.8" and len(calls) == n + 2
    finally:
        compat._http_get = saved_get
        os.environ["COPPER_HOME"] = saved_home
        os.environ["COPPER_NO_UPDATE_CHECK"] = "1"


def test_update_check_is_not_reachable_from_the_mcp_request_path():
    """Only `copper doctor` / `copper status` may ask the network; the server-side status reads the cache."""
    src = ROOT / "src" / "copper_kicad"
    offenders = []
    for f in src.rglob("*.py"):
        s = f.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(s.splitlines(), 1):
            if "latest_release(" in line and "def latest_release" not in line and "cached_only" not in line:
                if f.name not in ("compat.py", "compat_report.py"):
                    offenders.append(f"{f.name}:{i}")
    assert not offenders, offenders
    s = (src / "kicad" / "compat_report.py").read_text(encoding="utf-8")
    assert "cached_only=not network" in s
    calls = []
    saved = compat._http_get
    compat._http_get = lambda *a, **k: calls.append(a) or b"[]"
    try:
        _tools()
        with use_installs([fake_install("10.0.6", python=Path(sys.executable))]):
            os.environ.pop("COPPER_NO_UPDATE_CHECK", None)
            d, err = call("copper_status")
            assert not err and d["kicad"]["active"]["version"] == "10.0.6", d.get("kicad")
            assert any(a["id"] == "zone-fill-shorts" for a in d["kicad"]["advisories"]), d["kicad"]
    finally:
        compat._http_get = saved
        os.environ["COPPER_NO_UPDATE_CHECK"] = "1"
    assert calls == [], "copper_status made a network request"


# ── T14 IPC socket discovery ──────────────────────────────────────────────
def test_socket_discovery_prefers_api_sock_and_lists_all():
    from copper_kicad.pcb import live
    d = _TMP / "sock" / "kicad"
    d.mkdir(parents=True, exist_ok=True)
    (d / "api-1234.sock").write_text("")
    (d / "api-99.sock").write_text("")
    os.utime(d / "api-99.sock", (time.time() - 500, time.time() - 500))
    assert [p.name for p in live.sockets([d])] == ["api-1234.sock", "api-99.sock"]
    (d / "api.sock").write_text("")
    os.utime(d / "api.sock", (time.time() - 900, time.time() - 900))
    assert [p.name for p in live.sockets([d])] == ["api.sock", "api-1234.sock", "api-99.sock"]
    assert live.sockets([_TMP / "nope"]) == []
    saved = live.socket_dirs
    live.socket_dirs = lambda: [d]
    old = os.environ.pop("KICAD_API_SOCKET", None)
    try:
        assert live.socket_path() == d / "api.sock"
        assert [Path(s).name for s in live.capability()["sockets"]] == ["api.sock", "api-1234.sock", "api-99.sock"]
        os.environ["KICAD_API_SOCKET"] = "ipc://" + str(d / "api-99.sock")
        assert live.socket_path() == d / "api-99.sock"
    finally:
        live.socket_dirs = saved
        os.environ.pop("KICAD_API_SOCKET", None)
        if old:
            os.environ["KICAD_API_SOCKET"] = old


# ── write-path coverage ───────────────────────────────────────────────────
def test_every_board_and_schematic_write_path_is_behind_the_write_guard():
    """guard_writable coverage is by convention; this keeps the convention honest: a module that saves a
    board through kicad-cli (--save-board) or starts a saving pcbnew worker must call guard_writable."""
    src = ROOT / "src" / "copper_kicad"
    missing = []
    for f in src.rglob("*.py"):
        if "workers" in f.parts or f.name in ("compat.py", "worker.py"):
            continue
        s = f.read_text(encoding="utf-8", errors="replace")
        saves = '"--save-board"' in s or ("worker.run(" in s and '"save": save' in s) or '"save": True' in s
        if saves and "guard_writable(" not in s:
            missing.append(f.name)
    assert not missing, f"write paths without guard_writable: {missing}"
    s = (src / "project.py").read_text(encoding="utf-8")
    assert "compat.guard_write(p, inst)" in s and "format_preflight(pr, check=format_check)" in s


# ── tester regressions ────────────────────────────────────────────────────
def test_subfolder_sheet_upgrade_is_checkpointed_and_restorable():
    """A sub-sheet kept in a sub-folder (royalblue54L demo: sch/*.kicad_sch) is upgraded one-way by its first
    edit. The checkpoint the note points at must really hold the original, restore must bring it back
    byte-identical, and no bogus sch/<sheet>.kicad_pro may appear next to it."""
    real_kicad()
    _tools()
    pr = demo_copy("royalblue54L_feather", "sub")
    sheet = pr.dir / "sch" / "Debugger.kicad_sch"
    orig = {f: f.read_bytes() for f in [pr.sch, pr.pcb, *sorted((pr.dir / "sch").glob("*.kicad_sch"))]}
    assert compat.file_format(sheet)["version"] == 20250114
    with use_installs(None):
        op = [{"op": "text", "text": "compat sub", "at": [30, 30]}]
        # a dry run and a failing batch write nothing: no note, no state entry, on this or the next call
        d, err = call("sch_apply", ops=op, sheet="Debugger", layout="keep", dry_run=True, project=str(pr.dir))
        assert not err and "format_upgraded" not in d, d
        d, err = call("sch_apply", ops=[{"op": "place", "lib_id": "Nope:Nothing", "id": "x"}], sheet="Debugger",
                      layout="keep", project=str(pr.dir))
        assert err, d
        d, err = call("copper_status")
        assert "format_upgraded" not in d, "an upgrade that never happened was reported on the next call"
        assert "format_upgraded" not in compat._pstate(pr.dir), compat._pstate(pr.dir)
        assert sheet.read_bytes() == orig[sheet]
        # the real edit
        d, err = call("sch_apply", ops=op, sheet="Debugger", layout="keep", verify=False, project=str(pr.dir))
        assert not err, d
        fu = d.get("format_upgraded")
        assert fu and fu["files"] == ["sch/Debugger.kicad_sch"] and "checkpoint holds the original" in fu["note"], d
        snap = pr.dir / ".copper" / "snapshots" / fu["checkpoint"]
        assert (snap / "sch" / "Debugger.kicad_sch").read_bytes() == orig[sheet], "the checkpoint does not hold the sub-sheet"
        assert compat.file_format(sheet)["version"] == 20260306
        assert not list((pr.dir / "sch").glob("*.kicad_pro")), list((pr.dir / "sch").iterdir())
        assert set(compat._pstate(pr.dir)["format_upgraded"]) == {"sch/Debugger.kicad_sch"}
        d, err = call("checkpoint", action="list", project=str(pr.dir))
        assert not err and "sch/Debugger.kicad_sch" in d["files"], d
        d, err = call("checkpoint", action="diff", id=fu["checkpoint"], project=str(pr.dir))
        assert not err and [f["file"] for f in d["changed_files"]] == ["sch/Debugger.kicad_sch"], d
        d, err = call("checkpoint", action="restore", id=fu["checkpoint"], project=str(pr.dir))
        assert not err and "sch/Debugger.kicad_sch" in d["restored"], d
        for f, b in orig.items():
            assert f.read_bytes() == b, f"{f.name} differs after restore"
        # dedupe still works with sub-folder files: an unchanged project adds no second snapshot
        a = P.checkpoint(pr, "same")
        assert P.checkpoint(pr, "same") == a


def test_a_kicad_cli_that_does_not_run_is_refused_before_anything_is_written():
    """COPPER_KICAD_CLI pointing at something that is not a working kicad-cli: no version, so no guard can
    decide anything. Edits used to be written unchecked ("KiCad rejected the written schematic") and
    project_open reported 0 components."""
    real_kicad()
    _tools()
    pr = demo_copy("complex_hierarchy", "broken")
    before = tree_hashes(pr.dir)
    dead = _TMP / "dead-cli.py"
    dead.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    broken = env.Install("env", _TMP, dead, None, None, cli_argv=[sys.executable, str(dead)])
    assert broken.ver is None
    with use_installs([broken]):
        try:
            env.run(["version"])
            raise AssertionError("expected KICAD_CLI_BROKEN")
        except ToolError as e:
            assert e.code == "KICAD_CLI_BROKEN", e.to_dict()
        d, err = call("sch_apply", ops=[{"op": "text", "text": "x", "at": [40, 40]}], project=str(pr.dir))
        assert err and d["error"] == "KICAD_CLI_BROKEN", d
        new = _TMP / "broken-new"
        r = registry.call_tool("project_create", {"name": "broken-new", "dir": str(new)}, None)
        d, err = r.get("structuredContent") or {}, r["isError"]
        assert err and d["error"] == "KICAD_CLI_BROKEN" and not new.exists(), d
        d, err = call("project_open", path=str(pr.dir))
        assert not err and "components" not in d and d["netlist_error"]["error"] == "KICAD_CLI_BROKEN", d
        d, err = call("copper_status")
        assert d["kicad"]["problem"]["error"] == "KICAD_CLI_BROKEN" and d["capabilities"]["kicad_cli"] is False, d["kicad"]
        assert tree_hashes(pr.dir) == before, "a file changed although kicad-cli does not run"


def test_board_counts_on_a_board_with_top_level_items_at_column_zero():
    """KiCad's own royalblue54L demo board has its segments, vias and footprints at column 0: project_open
    reported 0 tracks / 0 vias for a fully routed 8-layer board."""
    import re
    from copper_kicad.review.project_info import board_counts
    t = (demos() / "royalblue54L_feather" / "RoyalBlue54L-Feather.kicad_pcb").read_text(encoding="utf-8")
    c = board_counts(t)
    assert c["tracks"] == len(re.findall(r"\(segment[\s)]", t)) == 943 and c["vias"] == 183, c
    assert c["footprints_on_board"] == 71 and c.get("teardrop_zones") == 263, c
    t2 = (demos() / "complex_hierarchy" / "complex_hierarchy.kicad_pcb").read_text(encoding="utf-8")
    c2 = board_counts(t2)
    assert c2["tracks"] == len(re.findall(r"^\t\((?:segment|arc)[\s)]", t2, re.M)) > 0, c2


def test_tool_calls_do_not_inherit_the_previous_calls_project_format():
    """Tool calls run on pooled threads: the selector's per-call project format must be reset when a call
    starts, or a call without a project picks its KiCad by whatever project that thread touched last."""
    _tools()
    stable, nightly = bare("10.0.6", python=Path(sys.executable)), bare("10.99.0-1-gabc")
    with use_installs([nightly, stable]):
        learned = compat.learned_stamps
        compat.learned_stamps = lambda inst: {"sch": 20261106, "pcb": 20261031} if inst.ver.nightly else {}
        t = registry.TOOLS["copper_help"]
        saved = t.fn
        try:
            env.set_project_format((("sch", 20261106),))
            assert env.kicad().ver.nightly
            seen = {}
            t.fn = lambda **a: seen.update(v=str(env.kicad().ver)) or {"ok": 1}
            registry.call_tool("copper_help", {}, None)
            assert seen["v"] == "10.0.6", seen
        finally:
            t.fn = saved
            compat.learned_stamps = learned


def test_same_series_older_stamp_is_not_described_as_another_kicad():
    """A file saved by a 9.99 nightly has a stamp between the 9.0 and 10.0 releases: the note must not say
    'was in KiCad 10.0 format ... KiCad 10.0 can no longer open it'."""
    k = real_kicad()
    d = _TMP / "midstamp"
    d.mkdir(exist_ok=True)
    (d / "m.kicad_pro").write_text("{}", encoding="utf-8")
    f = d / "m.kicad_sch"
    f.write_text('(kicad_sch (version 20250610) (generator "eeschema") (generator_version "9.99") (paper "A4"))\n', encoding="utf-8")
    with use_installs(None):
        compat.take_notes()
        n = compat.guard_write(f, k)["format_upgraded"]
        assert "development format" in n["note"] and "KiCad 10.0 can no longer open it" not in n["note"], n
        compat.take_notes()


def test_doctor_does_not_call_a_nested_config_folder_with_files_empty():
    """`copper doctor` said "<config>/10.0/10.0 is an empty folder ... safe to delete" for a folder that held
    KiCad settings files; --fix (rightly) left it alone, so the row contradicted the fix."""
    cfg = _TMP / "nested-cfg"
    n = cfg / "10.0" / "10.0" / "colors"
    n.mkdir(parents=True, exist_ok=True)
    with use_installs([bare("10.0.6")], KICAD_CONFIG_HOME=str(cfg)):
        row = next(c for c in compat_report.checks({"installs": []}) if c["id"] == "config_nested")
        assert "empty folder" in row["detail"] and "--fix" in row["fix"], row
        (n / "user.json").write_text("{}", encoding="utf-8")
        row = next(c for c in compat_report.checks({"installs": []}) if c["id"] == "config_nested")
        assert "empty" not in row["detail"] and "1 file" in row["detail"] and "by hand" in row["fix"], row
        assert compat_report.fix_nested_config_dir()["removed"] is False and (n / "user.json").exists()
        (n / "user.json").unlink()
        assert compat_report.fix_nested_config_dir()["removed"] is True and not (cfg / "10.0" / "10.0").exists()


def test_a_major_version_pin_never_selects_the_nightly():
    """COPPER_KICAD_VERSION=10 picked the 10.99 nightly (KiCad 11 in the making) over 10.0.8."""
    lst = [bare("9.0.9"), bare("10.0.6"), bare("10.0.8-rc1"), bare("10.99.0-1-gabc"), bare("10.0.8")]
    for pin, want in (("10", "10.0.8"), (" 10.0 ", "10.0.8"), ("10.0.8", "10.0.8"), ("10.0.8-rc1", "10.0.8-rc1"),
                      ("nightly", "10.99.0-1-gabc"), ("10.99", "10.99.0-1-gabc"), ("9", "9.0.9")):
        i, why = compat.select(lst, pin)
        assert i.full_version == want and "pinned" in why, (pin, i.full_version, why)
    i, why = compat.select([bare("10.99.0-1-gabc"), bare("9.0.9")], "10")        # nothing else fits: the nightly
    assert i.full_version == "10.99.0-1-gabc", why


# ── runner ────────────────────────────────────────────────────────────────
def _run_all() -> int:
    only = sys.argv[1:]
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f) and (not only or n in only)]
    failed = 0
    for n, f in tests:
        t = time.perf_counter()
        try:
            f()
            print(f"PASS {n}  ({time.perf_counter() - t:.1f}s)")
        except Skip as e:
            SKIPPED.append(n)
            print(f"SKIP {n}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {n}: {type(e).__name__}: {e}")
            traceback.print_exc(limit=5)
    print(f"{len(tests) - failed - len(SKIPPED)}/{len(tests)} passed, {len(SKIPPED)} skipped  (scratch: {_TMP})")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
