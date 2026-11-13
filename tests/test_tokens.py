"""Credit efficiency: what Copper costs the agent that drives it, and proof that it stays cheap.

    python tests/test_tokens.py            (or: python -m pytest tests/test_tokens.py)

Unit tests (no KiCad needed): the wire compactor (mcp/compact.py), the image policy, the credit meter
(mcp/ledger.py), the slim catalogue (mcp/catalogue.py) and argument-name help.
Replay tests (need KiCad; about four minutes): a reference board is built end to end through the stdio
server from the scripts in tests/budget/, with byte and image budgets asserted per stage, then changed
and rebuilt to prove board_build leaves existing copper, placement and the outline alone.
Set COPPER_TEST_NO_REPLAY=1 to run only the unit tests.

Budgets carry headroom over the largest run measured (routing and placement are not byte-reproducible);
the hard guarantees are the per-result cap and the call and image counts.
"""
from __future__ import annotations

import base64
import json
import os
import re
import struct
import sys
import tempfile
import threading
import time
import traceback
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "budget"))
os.environ.setdefault("COPPER_HOME", tempfile.mkdtemp(prefix="copper-home-"))
os.environ.setdefault("COPPER_CONFIG_HOME_OVERRIDE", tempfile.mkdtemp(prefix="copper-cfg-"))

from copper_kicad.mcp import catalogue, compact, ledger, registry  # noqa: E402
import replay as RP  # noqa: E402

NOTES: list[str] = []
FIX = json.loads((ROOT / "tests" / "fixtures" / "wire" / "flow_full.json").read_text(encoding="utf-8"))
PROJ = FIX["project_dir"]

# ── budgets (bytes of result text) ───────────────────────────────────────────────────────────────
CATALOGUE_MAX = 58_000            # tools/list; 67,404 B (30 tools) before this work, about 54,400 B (31 tools) after
SLIM_DESCRIPTION_MAX = 2_300      # characters of the longest slim description (sch_apply, with its op list)
CORE_MAX = 16_000                 # the always-loaded subset
INSTRUCTIONS_MAX = 1_200
RESULT_CAP = 6_000                # any single result, unless the caller asked for detail
SHORT = {"0 orient": 900, "2 schematic": 2_400, "3 board": 2_800, "5 fab": 3_000, "6 status": 3_400, "total": 11_500}
DEFAULTS_TOTAL = 27_000           # the 18-call flow; 47,803 B + 10 images before


def png(w: int, h: int) -> bytes:
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x00" * (w * 3) for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


def mcp(payload: dict, summary: str = "", images: list | None = None, captions: list | None = None, error=False) -> dict:
    """A registry-style result, as Result.to_mcp builds it."""
    text = json.dumps(payload, separators=(",", ":"), default=str, ensure_ascii=False)
    content = [{"type": "text", "text": (summary + "\n" + text) if summary else text}]
    for i, raw in enumerate(images or []):
        content.append({"type": "image", "data": base64.b64encode(raw).decode(), "mimeType": "image/png"})
        if captions and captions[i]:
            content.append({"type": "text", "text": captions[i]})
    out = {"content": content, "isError": error}
    if payload:
        out["structuredContent"] = json.loads(json.dumps(payload, default=str))
    return out


def wire(payload, summary="", project=PROJ, tool="t", args=None, **kw) -> tuple[str, dict, dict]:
    out = compact.to_wire(mcp(payload, summary, **{k: kw.pop(k) for k in ("images", "captions", "error") if k in kw}),
                          project, tool=tool, args=args or {}, **kw)
    text = out["content"][0]["text"]
    return text, RP.payload_of(out), out


class Env:
    def __init__(self, **kv):
        self.kv = kv

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.kv}
        for k, v in self.kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def leaves(o, names, path=""):
    """{path: value} of every numeric leaf whose key is in `names`."""
    out = {}
    if isinstance(o, dict):
        for k, v in o.items():
            if k in names and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[f"{path}.{k}"] = v
            out.update(leaves(v, names, f"{path}.{k}"))
    return out


# ── compactor ────────────────────────────────────────────────────────────────────────────────────
def test_zeros_and_false_survive():
    """A count that is absent is not a count of zero: 0, 0.0 and false are never 'empty'."""
    p = {"ok": False, "fab_ready": False, "errors": 0, "unrouted": 0, "parity": 0.0, "stale": False, "timed_out": False,
         "verify": {"drc": {"errors": 0, "warnings": 0, "unconnected": 0, "parity": 0}},
         "empty_list": [], "empty_dict": {}, "empty_str": "", "none": None, "dry_run": False, "created": False}
    _t, d, _o = wire(p)
    for k in ("ok", "fab_ready", "errors", "unrouted", "parity", "stale", "timed_out"):
        assert k in d and d[k] == p[k] and type(d[k]) is type(p[k]), (k, d)
    assert d["verify"]["drc"] == {"errors": 0, "warnings": 0, "unconnected": 0, "parity": 0}, d
    for k in ("empty_list", "empty_dict", "empty_str", "none", "dry_run", "created"):
        assert k not in d, (k, d)
    assert wire({"dry_run": True, "created": True})[1] == {"dry_run": True, "created": True}


def test_never_drops_fix_next_refs_at_and_numbers():
    p = {"next": "verify()", "fix": "pcb_sync()", "refs": ["U1.3"], "at": [1.0, 2.0], "error": "X", "message": "m", "ok": True,
         "section": "violations",
         "revision": "abc", "canonicalized": True, "checks_run": ["a"], "count": 0, "n": 3,
         "findings": [{"severity": "error", "type": "t", "message": "m", "fix": "sch_apply(ops=[])", "refs": ["U1.1"], "at": [5, 6]}]}
    _t, d, _o = wire(p)
    for k in ("next", "fix", "refs", "at", "error", "message", "ok", "count", "n"):
        assert d[k] == p[k], (k, d)
    for k in ("revision", "canonicalized", "checks_run", "section"):
        assert k not in d, k
    f = d["findings"][0]
    assert f["fix"] == "sch_apply(ops=[])" and f["refs"] == ["U1.1"] and f["at"] == [5, 6], f


def test_paths_are_project_relative():
    win = "C:\\Users\\dev\\Documents\\Copper\\proj dir\\c3ref"
    paths = compact.Paths(win, windows=True)
    assert paths.value(win + "\\copper-out\\fab\\x.zip") == "copper-out/fab/x.zip"
    assert paths.value("c:/users/DEV/documents/copper/proj dir/c3ref/c3ref.kicad_pcb") == "c3ref.kicad_pcb"   # case, slashes
    assert paths.value(win) == "."
    assert paths.value("C:\\Users\\dev\\Documents\\Copper\\proj dir\\c3ref2\\x") == "C:\\Users\\dev\\Documents\\Copper\\proj dir\\c3ref2\\x"
    assert paths.value("C:\\Program Files\\KiCad\\10.0\\bin\\kicad-cli.exe").startswith("C:\\Program Files")     # outside: absolute
    assert paths.text(f"pack ready: {win}\\copper-out\\fab\\a.zip — 2 layers") == "pack ready: copper-out/fab/a.zip — 2 layers"
    # never inside fix / next / dir / project (a relative path in a suggested call would resolve against the server's cwd)
    p = {"zip": win + "\\out\\a.zip", "dir": win, "fix": f"project_open(path='{win}')", "next": f"export(out='{win}\\x')",
         "nested": {"report": win + "\\.copper\\erc.json", "more": "3 more", "files": [win + "\\a.kicad_sch", "D:\\other\\b"]}}
    _t, d, _o = wire(p, f"saved {win}\\out\\a.zip", project=win)
    assert d["zip"] == "out/a.zip" and d["dir"] == win and d["fix"] == p["fix"] and d["next"] == p["next"], d
    assert d["nested"] == {"report": ".copper/erc.json", "more": "3 more", "files": ["a.kicad_sch", "D:\\other\\b"]}, d
    assert _t.startswith("saved out/a.zip\n"), _t[:80]
    # POSIX spelling, a folder with a space
    posix = compact.Paths("/home/u/my board", windows=False)
    assert posix.value("/home/u/my board/x.kicad_pcb") == "x.kicad_pcb"
    assert posix.value("/home/u/my board2/x.kicad_pcb") == "/home/u/my board2/x.kicad_pcb"
    assert posix.value("/HOME/u/my board/x.kicad_pcb") == "/HOME/u/my board/x.kicad_pcb"          # case matters there
    assert posix.text("wrote /home/u/my board/copper-out/a.png ok") == "wrote copper-out/a.png ok"


def test_windows_short_names_are_the_same_folder():
    if os.name != "nt":
        NOTES.append("8.3 short-name test skipped (not Windows)")
        return
    import ctypes
    d = Path(tempfile.mkdtemp(prefix="copper long name ")) / "my project folder"
    d.mkdir()
    buf = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(d), buf, 1024)
    short = buf.value
    if short.lower() == str(d).lower():
        NOTES.append("8.3 names are disabled on this volume: short-name spelling not exercised")
    p = {"a": short + "\\x.kicad_pcb", "b": str(d.resolve()) + "\\sub\\y.png", "c": str(d).upper().replace("\\", "/") + "/z"}
    for given in (short, str(d)):
        _t, out, _o = wire(p, project=given)
        assert out == {"a": "x.kicad_pcb", "b": "sub/y.png", "c": "z"}, (given, out)


def test_change_digest_keeps_what_each_part_resolved_to():
    ch = FIX["calls"][2]["payload"]["changed"]
    assert len(ch) > 8
    d = compact.digest_changed(ch)
    assert d["placed"] == 17 and d["connected"] >= 40, d
    rows = {r[0]: r for r in d["parts"]["rows"]}
    assert d["parts"]["cols"] == ["ref", "value", "part", "class"]
    assert rows["R1"] == ["R1", "5.1k", "C25905", "B"] and rows["D1"][2:] == ["C965807", "E"], rows   # Basic vs Extended is visible
    assert rows["U1"][:3] == ["U1", "AMS1117-3.3", "C6186"] and rows["U1"][3] is None, rows["U1"]
    assert compact.digest_changed(ch[:8]) == ch[:8]                                                # short lists are left alone
    sync = FIX["calls"][7]["payload"]["changed"]
    ds = compact.digest_changed(sync)
    assert ds["added"] == 17 and any("pad net assignments" in n for n in ds["notes"]), ds        # lines that fit no verb are kept


def test_findings_are_grouped_and_standing_ones_dropped():
    p = FIX["calls"][8]["payload"]                     # pcb_setup: 8 identical drill findings + UNROUTED + DRC_SUMMARY
    codes = [f.get("code") for f in p["findings"]]
    assert codes.count("drill_out_of_range") == 8 and "UNROUTED" in codes and "DRC_SUMMARY" in codes
    _t, d, _o = wire(p, tool="pcb_setup")
    got = {f["code"]: f for f in d["findings"]}
    assert "UNROUTED" not in got and "DRC_SUMMARY" not in got, list(got)                          # verify.drc has the counts
    g = got["drill_out_of_range"]
    assert g["n"] == 8 and g["at"] == p["findings"][1]["at"] and len(g["more_at"]) == 2 and g["fix"], g
    assert d["verify"]["drc"] == p["verify"]["drc"]
    # a specific UNROUTED warning (the router's own, with nets and a fix) is NOT a standing finding
    q = {"verify": {"drc": {"errors": 0, "unconnected": 2}},
         "findings": [{"severity": "warning", "code": "UNROUTED", "message": "could not finish 2: /A×2", "fix": "pcb_route(tracks=[])"}]}
    assert wire(q)[1]["findings"] == q["findings"]
    # `fixes` arrays go, `fixed_findings` becomes a count, repeated strings in `top` collapse
    r = {"drc": {"errors": 1, "fixed_findings": ["a", "b", "c"],
                 "findings": [{"type": "x", "message": "m", "fix": "verify()", "fixes": ["verify()", "render(target='pcb')"]}]},
         "waived": [{"code": "drc", "top": ["same", "same", "same", "other"]}]}
    d = wire(r)[1]
    assert d["drc"]["fixed_findings"] == 3 and "fixes" not in d["drc"]["findings"][0], d
    assert d["waived"][0]["top"] == ["same (x3)", "other"], d["waived"]


def test_rounding_keeps_widths_and_positions():
    p = {"at": [148.61790000001, 100.00229999], "min_track_width_mm": 0.127, "min_clearance_mm": 0.2243,
         "track_length_mm": 437.0849, "seconds": 8.6412, "percent": 94.2857, "score": {"ratsnest_mm_before": 933.5391}}
    d = wire(p)[1]
    assert d["at"] == [148.6179, 100.0023], d["at"]                       # 0.1 um: positions come back in later calls
    assert d["min_track_width_mm"] == 0.127 and d["min_clearance_mm"] == 0.2243, d
    assert d["track_length_mm"] == 437.1 and d["seconds"] == 8.6 and d["percent"] == 94.3, d


def test_single_channel_and_captions():
    img = png(40, 30)
    cap = f"[image] routes as KiCad has them now — saved at {PROJ}\\copper-out\\pcb-route-guided.png"
    t, d, out = wire({"routed": 3}, "guided route: 3/3", tool="pcb_route_guided", images=[img], captions=[cap])
    assert "structuredContent" not in out
    kinds = [c["type"] for c in out["content"]]
    assert kinds == ["text", "image", "text"], kinds
    assert out["content"][2]["text"] == "[image] routes as KiCad has them now", out["content"][2]
    assert d == {"routed": 3, "image": "copper-out/pcb-route-guided.png"}, d
    assert t.split("\n")[0] == "guided route: 3/3"
    with Env(COPPER_STRUCTURED="1"):
        out = compact.to_wire(mcp({"a": 1, "revision": "x", "e": []}), PROJ, tool="t")
        assert out["structuredContent"] == {"a": 1} == RP.payload_of(out), out
    # errors travel the same way and keep error / message / fix; the trace is for COPPER_DEBUG only
    e = {"error": "INTERNAL", "message": "KeyError: 'x'", "fix": "copper_status()", "retry": False, "trace": ["a", "b"]}
    with Env(COPPER_DEBUG=None):
        t, d, out = wire(e, error=True)
    assert out["isError"] is True and d == {k: v for k, v in e.items() if k != "trace"}, d
    # plain-text results (copper_help) and COPPER_COMPACT=0 pass through untouched
    plain = {"content": [{"type": "text", "text": "# sch_apply\n\n{\"op\":\"place\"}\nmore"}], "isError": False}
    assert compact.to_wire(dict(plain), PROJ, tool="copper_help") == plain
    with Env(COPPER_COMPACT="0"):
        raw = mcp({"revision": "x", "e": []}, "s")
        assert compact.to_wire(json.loads(json.dumps(raw)), PROJ, tool="t") == raw
    # Result(raw=True) is sent as built
    r = registry.Result({"revision": "x", "empty": []}, summary="s", raw=True).to_mcp()
    assert RP.payload_of(compact.to_wire(r, PROJ, tool="t")) == {"revision": "x", "empty": []}


def test_result_cap_cuts_lists_and_spills_the_rest():
    d = Path(tempfile.mkdtemp(prefix="copper cap "))
    p = {"ok": True, "fab_ready": False, "errors": 0, "next": "verify()",
         "footprints": [{"ref": f"R{i}", "value": "10k", "x": 100.5 + i, "y": 120.25, "note": "x" * 40} for i in range(200)],
         "nets": [{"name": f"/NET{i}", "pads": i} for i in range(150)], "small": [1, 2, 3]}
    text, got, out = wire(p, "board: 200 footprints", project=str(d), tool="pcb_read")
    assert len(text.encode()) <= compact.DEFAULT_MAX_BYTES, len(text.encode())
    assert got["ok"] is True and got["fab_ready"] is False and got["errors"] == 0 and got["next"] == "verify()", got
    assert compact.MIN_LIST <= len(got["footprints"]) < 200 and len(got["nets"]) <= 150 and got["small"] == [1, 2, 3]
    cut = got["cut"]
    assert cut["lists"]["footprints"] == [len(got["footprints"]), 200], cut
    if "nets" in cut["lists"]:
        assert cut["lists"]["nets"] == [len(got["nets"]), 150], cut
    full = json.loads((d / cut["file"]).read_text(encoding="utf-8"))
    assert len(full["footprints"]) == 200 and len(full["nets"]) == 150, "the spill file must hold the uncut payload"
    assert got["footprints"] == full["footprints"][:len(got["footprints"])], "lists are cut from the END"
    # a call that asks for detail gets a larger cap; nothing is ever cut below MIN_LIST, and at most 20 files are kept
    t2 = wire(p, "s", project=str(d), tool="pcb_read", args={"what": ["footprints"]})[0]
    assert len(t2.encode()) > compact.DEFAULT_MAX_BYTES
    tiny = wire({"rows": [{"k": "y" * 900} for _ in range(12)]}, project=str(d), tool="x", max_bytes=500)[1]
    assert len(tiny["rows"]) == compact.MIN_LIST, len(tiny["rows"])
    for _ in range(30):
        wire(p, project=str(d), tool="pcb_read")
    assert len(list((d / ".copper" / "results").glob("*.json"))) <= compact.MAX_SPILL_FILES
    with Env(COPPER_MAX_RESULT_BYTES="0"):
        assert "cut" not in wire(p, project=str(d), tool="pcb_read")[1]


def test_captured_flow_shrinks_and_keeps_every_count():
    """The 18 results of a real build, captured before this work, through the compactor rules alone (no cut, no
    digest defaults, and with a short project path): under 60% of the bytes, every numeric errors/warnings/unrouted/unconnected/parity leaf still there with the same value."""
    names = {"errors", "warnings", "unrouted", "unconnected", "parity", "new_errors", "erc_violations", "drc_violations"}
    before = after = 0
    worst = (0, "")
    d = Path(tempfile.mkdtemp(prefix="copper flow "))
    for c in FIX["calls"]:
        res = mcp(c["payload"], c["summary"])
        before += len(res["content"][0]["text"].encode())
        with Env(COPPER_MAX_RESULT_BYTES="0"):          # the rules alone, no cut
            out = compact.to_wire(json.loads(json.dumps(res)), PROJ, tool=c["tool"], args=c["args"])
        text = out["content"][0]["text"]
        after += len(text.encode())
        got = RP.payload_of(out)
        want = leaves(c["payload"], names)
        have = leaves(got, names)
        assert want == have, (c["tool"], {k: v for k, v in want.items() if have.get(k) != v})
        assert "structuredContent" not in out
        if c["tool"] not in compact.ABS_DIR_TOOLS:
            assert "Users\\\\dev\\\\Documents" not in text and "Users/dev/Documents" not in text, (c["tool"], text[:300])
        else:
            assert text.count("proj dir") == 1, (c["tool"], text.count("proj dir"))      # one absolute `dir`, nothing else
        capped = compact.to_wire(json.loads(json.dumps(res)), str(d), tool=c["tool"], args=c["args"])
        n = len(capped["content"][0]["text"].encode())
        worst = max(worst, (n, c["tool"]))
    NOTES.append(f"captured flow through the compactor: {before} -> {after} B ({100 * after // before}%); largest capped result "
                 f"{worst[0]} B ({worst[1]})")
    assert after <= 0.60 * before, (before, after)
    assert worst[0] <= RESULT_CAP, worst


# ── image policy and wire defaults ───────────────────────────────────────────────────────────────
def _tool(name, **props):
    return registry.Tool(name, lambda **k: {}, "d" * 50, {"type": "object", "properties": props})


def test_images_only_on_request():
    b = {"type": "boolean"}
    place = _tool("pcb_place", render=b, auto=b)
    apply_ = _tool("sch_apply", preview=b, ops={"type": "array"})
    with Env(COPPER_IMAGES=None, COPPER_COMPACT=None):
        assert compact.image_mode() == "final"
        assert compact.prepare_args(place, {"auto": True}) == ({"auto": True, "render": False}, {"render": False})
        assert compact.prepare_args(apply_, {"ops": []})[0]["preview"] is False
        assert compact.prepare_args(place, {"render": True})[0] == {"render": True}, "an explicit argument always wins"
        assert compact.prepare_args(_tool("focus", render=b), {})[0] == {}, "focus: the picture is the answer"
        r = _tool("render", target={"type": "string"}, width={"type": "integer"})
        assert compact.prepare_args(r, {"target": "pcb"})[0]["width"] == 800
        assert compact.prepare_args(r, {"target": "schematic"})[0]["width"] == 1000
        assert compact.prepare_args(r, {"target": "pcb", "width": 2000})[0]["width"] == 2000
        assert compact.prepare_args(_tool("pcb_read", what={"type": "array"}), {})[0] == {"what": ["summary"]}
        assert compact.prepare_args(_tool("pcb_read", what={"type": "array"}, refs={"type": "array"}), {"refs": ["U1"]})[0] == {"refs": ["U1"], "what": ["footprints"]}
        assert compact.prepare_args(None, {"x": 1}) == ({"x": 1}, {})
    with Env(COPPER_IMAGES="never"):
        assert compact.prepare_args(place, {})[0] == {"render": False}
    with Env(COPPER_IMAGES="auto"):
        assert compact.prepare_args(place, {})[0] == {}, "auto = the tools' own defaults"
    with Env(COPPER_IMAGES="bogus"):
        assert compact.image_mode() == "final"
    with Env(COPPER_COMPACT="0"):
        assert compact.prepare_args(place, {})[0] == {}


def test_images_are_capped_in_size_and_count():
    big, small = png(1800, 1272), png(800, 511)
    with Env(COPPER_IMAGES=None):
        out = wire({"a": 1}, tool="sch_tidy", images=[big])[2]
        w, h = ledger.b64_png_size(out["content"][1]["data"])
        assert max(w, h) == compact.IMAGE_MAX_EDGE and abs(w / h - 1800 / 1272) < 0.01, (w, h)
        out = wire({"a": 1}, tool="render", args={"target": "pcb", "width": 1800}, images=[big])[2]
        assert ledger.b64_png_size(out["content"][1]["data"]) == (1800, 1272), "an explicit width is honoured"
        out = wire({"a": 1}, tool="render", args={"target": "pcb"}, images=[small])[2]
        assert ledger.b64_png_size(out["content"][1]["data"]) == (800, 511)
        # one picture per result unless more were asked for
        t, d, out = wire({"images": ["a.png", "b.png", "c.png"]}, tool="render", args={"target": "schematic"}, images=[small] * 3)
        assert [c["type"] for c in out["content"]].count("image") == 1 and d["images_not_attached"] == 2 and "sheet=" in d["next"], d
        out = wire({}, tool="render", args={"target": "schematic", "sheet": "power"}, images=[small] * 3)[2]
        assert [c["type"] for c in out["content"]].count("image") == 3
        out = wire({"x": 1}, tool="parts_import", images=[small, small])[2]
        assert [c["type"] for c in out["content"]].count("image") == 1


def test_large_drawings_are_palette_encoded_same_pixels_fewer_bytes():
    """A 1000 px picture of a dense board was 0.5 to 0.7 MB of base64, close to the result-size limit of some
    clients. Large flat-colour drawings leave with a 256-colour palette: same width and height (so the same
    image tokens), about a third of the bytes. Small pictures and 3D views are not touched."""
    import io
    import random
    try:
        from PIL import Image as PILImage, ImageDraw
    except ImportError:
        NOTES.append("palette test skipped (no Pillow)")
        return
    rnd = random.Random(7)
    im = PILImage.new("RGB", (1000, 700), (20, 60, 150))
    dr = ImageDraw.Draw(im)
    for _ in range(2500):                                 # tracks and pads: a few colours, many anti-aliased edges
        x, y = rnd.randrange(1000), rnd.randrange(700)
        dr.line([x, y, x + rnd.randrange(-80, 80), y + rnd.randrange(-80, 80)], fill=rnd.choice([(200, 40, 40), (0, 0, 0), (240, 230, 140)]), width=3)
    im = im.resize((2000, 1400), PILImage.LANCZOS).resize((1000, 700), PILImage.LANCZOS)   # the anti-aliasing a renderer adds
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    raw_png = buf.getvalue()
    b64 = base64.b64encode(raw_png).decode()
    assert len(b64) > compact.IMAGE_PALETTE_OVER, len(b64)
    with Env(COPPER_COMPACT=None, COPPER_IMAGES=None):
        _, _, out = wire({"ok": True}, "s", tool="board_build", args={}, images=[raw_png], captions=["[image] the board"])
        data = next(c["data"] for c in out["content"] if c["type"] == "image")
        assert ledger.b64_png_size(data) == (1000, 700), "same pixels"
        assert len(data) < 0.7 * len(b64), (len(data), len(b64))
        got = PILImage.open(io.BytesIO(base64.b64decode(data))).convert("RGB")
        diff = sum(abs(a - b) for a, b in zip(got.resize((100, 70)).tobytes(), im.resize((100, 70)).tobytes())) / (100 * 70 * 3)
        assert diff < 6, f"the palette picture drifted from the original (mean channel error {diff:.1f})"
        # a 3D view keeps its shading; a small picture is left alone
        _, _, out3 = wire({"ok": True}, "s", tool="render", args={"target": "pcb3d"}, images=[raw_png], captions=["[image] 3D"])
        assert next(c["data"] for c in out3["content"] if c["type"] == "image") == b64
        small = png(400, 300)
        _, _, outs = wire({"ok": True}, "s", tool="render", args={"target": "pcb"}, images=[small], captions=["[image] x"])
        assert next(c["data"] for c in outs["content"] if c["type"] == "image") == base64.b64encode(small).decode()
    NOTES.append(f"palette encoding: 1000x700 drawing {len(b64)} -> {len(data)} base64 characters, same pixels")


# ── credit meter ─────────────────────────────────────────────────────────────────────────────────
def test_ledger_counts_bytes_pixels_and_tokens():
    led = ledger.Ledger()
    assert ledger.png_size(png(800, 511)) == (800, 511) and ledger.png_size(b"nope") == (0, 0)
    assert ledger.image_tokens(800, 511) == 545 and ledger.image_tokens(1800, 1272) == 2316 and ledger.image_tokens(0, 0) == 0
    res = mcp({"k": "v" * 96}, "sum", images=[png(800, 511)], captions=["[image] cap"])
    text_bytes = sum(len(c["text"].encode()) for c in res["content"] if c["type"] == "text")
    led.record("pcb_place", {"auto": True}, res, 1.25)
    led.record("pcb_place", {"auto": True}, mcp({"error": "X", "message": "é"}, error=True), 0.5)
    led.record("verify", None, mcp({}), 0.1)
    s = led.tools["pcb_place"]
    assert (s.calls, s.errors, s.images, s.image_pixels, s.image_tokens) == (2, 1, 1, 800 * 511, 545), s
    assert s.arg_bytes == 2 * len('{"auto":true}') and s.text_bytes == text_bytes + len('{"error":"X","message":"é"}'.encode()), s
    assert abs(s.seconds - 1.75) < 1e-9
    led.catalogue_bytes, led.instructions_bytes = 40_000, 1_200
    est = led.est_tokens()
    assert est["catalogue"] == 10_300 and est["images"] == 545 and est["total"] == sum(v for k, v in est.items() if k != "total"), est
    dg = led.digest()
    assert dg["calls"] == 3 and dg["errors"] == 1 and dg["images"] == 1 and dg["top"][0].startswith("pcb_place "), dg
    assert dg["reread_per_turn"] == est["catalogue"] + est["results"] + est["images"]
    assert len(json.dumps(dg, separators=(",", ":"))) <= 420, len(json.dumps(dg))
    tb = led.table()
    assert tb["cols"][0] == "tool" and tb["rows"][0][0] == "pcb_place" and tb["rows"][0][1] == 2 and "lower bound" in tb["not_counted"]


def test_ledger_is_thread_safe_and_private():
    home = Path(tempfile.mkdtemp(prefix="copper ledger "))
    with Env(COPPER_HOME=str(home), COPPER_LEDGER=None):
        led = ledger.Ledger()
        led.set_client({"name": "some-client", "version": "1.2", "secret": "no"})
        secret = "C:/Users/someone/private-board/top-secret-value"
        res = mcp({"path": secret, "text": "result text that must never be stored"})

        def work(i):
            for _ in range(250):
                led.record(f"tool_{i % 4}", {"project": secret}, res, 0.001)
        ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sum(s.calls for s in led.tools.values()) == 2000 and all(s.calls == 500 for s in led.tools.values())
        one = len(res["content"][0]["text"].encode())
        assert sum(s.text_bytes for s in led.tools.values()) == 2000 * one
        led.flush()
        files = list((home / "sessions").glob("*.json"))
        assert len(files) == 1, files                              # written during the run (every 10 calls) and at the end
        text = files[0].read_text(encoding="utf-8")
        assert "secret" not in text and "private-board" not in text and "result text" not in text, "counters only"
        data = json.loads(text)
        assert data["client"] == "some-client 1.2" and data["tools"]["tool_0"]["calls"] == 500

        def strings(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    yield k
                    yield from strings(v)
            elif isinstance(o, str):
                yield o
        assert max(len(s) for s in strings(data)) <= 40, "nothing longer than a tool name or a timestamp"
        rep = ledger.report(last=3)
        assert rep.startswith("Copper tool traffic only (estimated tokens)") and "tool_0" in rep and "2000 calls" in rep, rep[:300]
        # retention and opt-out
        for i in range(70):
            (home / "sessions" / f"20200101-0000{i:02d}-1.json").write_text("{}", encoding="utf-8")
        ledger.prune(home / "sessions")
        assert len(list((home / "sessions").glob("*.json"))) == ledger.KEEP_SESSIONS
    home2 = Path(tempfile.mkdtemp(prefix="copper ledger off "))
    with Env(COPPER_HOME=str(home2), COPPER_LEDGER="0"):
        led = ledger.Ledger()
        led.record("x", {}, mcp({"a": 1}), 0)
        led.flush()
        assert not (home2 / "sessions").exists() and led.digest()["calls"] == 1, "no file, counters still there"


# ── found by the skeptical test pass ─────────────────────────────────────────────────────────────
def test_fix_hints_are_never_cut_in_the_middle_of_a_call():
    """board_build cut every fix at 160 characters: `sch_apply(ops=[{"op":"p` cannot be run, so `next` fell
    back to verify() and the agent paid a second call to read the same finding again."""
    from copper_kicad import tools as _t  # noqa: F401
    from copper_kicad.tools import flow_tools as F
    long_fix = ('rail "GND" has no power source in the schematic. If it comes from a connector/battery/regulator not '
                'modelled as a power output, flag it: sch_apply(ops=[{"op":"pwr_flag","net":"GND"}])')
    assert len(long_fix) > 160 and F._short_fix(long_fix) == long_fix, F._short_fix(long_fix)
    huge = "x " * 300 + 'then pcb_place(moves=[{"ref":"P102","x":1,"y":2}]) so the courtyards no longer overlap ' + "y " * 300
    got = F._short_fix(huge)
    assert got == 'pcb_place(moves=[{"ref":"P102","x":1,"y":2}])', got          # only the call survives, whole
    prose = F._short_fix("word " * 200)
    assert len(prose) <= F.FIX_MAX + 2 and prose.endswith(" …"), prose[-20:]
    assert F._short_fix("pcb_sync()  [same fix for each of the 68 refs]") == "pcb_sync()"
    errs, warn = F._groups([{"severity": "error", "type": "power_pin_not_driven", "message": "m", "fix": long_fix},
                            {"severity": "warning", "type": "w", "count": 3}])
    assert errs[0]["fix"] == long_fix and F._call_in(errs[0]["fix"]) == 'sch_apply(ops=[{"op":"pwr_flag","net":"GND"}])', errs
    assert warn == {"w": 3}


def test_board_build_refuses_a_nonsense_size_before_touching_anything():
    from copper_kicad import tools as _t  # noqa: F401
    for args in ({"width": -5, "height": 0}, {"width": 0, "height": 20}, {"width": 30}, {"width": 1e9, "height": 20}):
        r = registry.call_tool("board_build", dict(args, project="Z:/no/such/project"))
        d = RP.payload_of(r)
        assert r["isError"] and d["error"] == "BAD_ARGUMENT", (args, d)       # not NO_PROJECT: the size is judged first


def test_pcb_read_about_some_parts_does_not_return_the_whole_board():
    """pcb_read(refs=['U11']) used the tool's own default (summary + ALL footprints + ALL nets): 24 kB on a
    589-net board. A question about some parts or one net now gets only that."""
    from copper_kicad import tools as _t  # noqa: F401
    t = registry.TOOLS["pcb_read"]
    with Env(COPPER_COMPACT=None, COPPER_IMAGES=None):
        assert compact.prepare_args(t, {})[0]["what"] == ["summary"]
        assert compact.prepare_args(t, {"refs": ["U11"]})[0]["what"] == ["footprints"]
        assert compact.prepare_args(t, {"net": "GND"})[0]["what"] == ["nets"]
        a, d = compact.prepare_args(t, {"refs": ["U11"], "what": ["pads"]})
        assert a["what"] == ["pads"] and "what" not in d, "an explicit `what` always wins"


def test_meter_rows_are_registered_tools_only_and_credits_name_no_folder():
    """A made-up tool name is client text of any length: it must not become a row of the session file.
    And the per-tool table is about the session, not a project: no absolute folder in it."""
    import io
    from copper_kicad import tools as _t  # noqa: F401
    from copper_kicad.mcp import server
    home = Path(tempfile.mkdtemp(prefix="copper meter "))
    with Env(COPPER_HOME=str(home), COPPER_LEDGER=None):
        led = ledger.reset()
        try:
            out = io.StringIO()
            s = server.Server(stdin=io.StringIO(""), stdout=out)
            s._call(1, {"name": "S3CRET-" + "x" * 500, "arguments": {"token": "hunter2"}})
            s._call(2, {"name": 123, "arguments": {}})
            s._call(3, {"name": "copper_status", "arguments": {"detail": "credits"}})
            assert set(led.tools) == {ledger.UNKNOWN, "copper_status"}, set(led.tools)
            assert led.tools[ledger.UNKNOWN].calls == 2 and led.tools[ledger.UNKNOWN].errors == 2
            led.flush()
            text = "".join(f.read_text(encoding="utf-8") for f in (home / "sessions").glob("*.json"))
            assert text and "S3CRET" not in text and "hunter2" not in text, text[:300]
            credits = json.loads(out.getvalue().strip().splitlines()[-1])["result"]
            d = RP.payload_of(credits)
            assert d["cols"][0] == "tool" and "dir" not in d, list(d)
        finally:
            ledger.reset()



# ── arguments: prevent the second failed call ────────────────────────────────────────────────────
def test_unknown_arguments_name_the_right_one():
    saved = dict(registry.TOOLS)
    try:
        @registry.tool("zz_open", "Open a thing by path. Example: zz_open(path='x')", {"path": {"type": "string"}, "limit": {"type": "integer"}},
                       ["path"], aliases={"file": "path"})
        def zz_open(path, limit=5):
            return {"path": path, "limit": limit}
        assert registry.TOOLS["zz_open"].aliases == {"file": "path"}
        ok = registry.call_tool("zz_open", {"file": "a.kicad_pro"})
        assert ok["isError"] is False and ok["structuredContent"]["path"] == "a.kicad_pro", ok      # a declared alias is accepted
        bad = registry.call_tool("zz_open", {"path": "a", "limt": 3})["structuredContent"]
        assert bad["error"] == "BAD_ARGUMENT" and "did you mean 'limit'" in bad["message"] and "'limit'" in bad["fix"], bad
        far = registry.call_tool("zz_open", {"path": "a", "zzzzzz": 3})["structuredContent"]
        assert far["error"] == "BAD_ARGUMENT" and "did you mean" not in far["message"] and "valid:" in far["message"], far
        assert registry.ALIASES["project_create"] == {"path": "dir"} and registry.ALIASES["project_open"]["project"] == "path"

        @registry.tool("zz_boom", "Always fails internally, to test the error text of bugs.", {})
        def zz_boom():
            raise KeyError("missing")
        e = registry.call_tool("zz_boom", {})["structuredContent"]
        assert e["error"] == "INTERNAL" and e["retry"] is False and "same way" in e["fix"], e
    finally:
        registry.TOOLS.clear()
        registry.TOOLS.update(saved)


# ── catalogue ────────────────────────────────────────────────────────────────────────────────────
def test_verify_next_is_a_call_when_only_drc_errors_remain():
    """A board that is fully connected but has DRC errors (errors of a footprint that were there before routing,
    for one) got the prose `next` 'fix DRC errors (see findings[].fix)': nothing a client can run. It is a call now,
    like every other `next` — the first error's own fix, or a look at the board."""
    import copper_kicad.tools as T   # noqa: F401
    from copper_kicad.tools import verify_tools as VT
    call = re.compile(r"\s*(?:[^()]*?\b)?([a-z][a-z0-9_]+)\(")
    drill = {"severity": "error", "type": "drill_out_of_range", "message": "Hole size out of range",
             "fix": "use JLC-capable holes: via ≥ 0.6/0.3 mm — pcb_setup(rules='jlc') then fix the offending vias/pads"}
    prose = {"severity": "error", "type": "clearance", "message": "Clearance violation (netclass 'Default' 0.2 mm)",
             "fix": "move the track (see at) or lower the clearance"}
    warn = {"severity": "warning", "type": "silk_edge_clearance", "fix": "pcb_place(moves=[...])"}
    for findings, tool_name in (([warn, drill], "pcb_setup"), ([prose, drill], "render"), ([], "render")):
        d = {"erc": {"errors": 0}, "drc": {"errors": 2, "unconnected": 0, "parity": 0, "findings": findings}}
        nxt = VT._next(d)
        m = call.match(nxt)
        assert m and m.group(1) == tool_name and m.group(1) in registry.TOOLS, nxt
        assert "DRC errors" in nxt, nxt
    # unrouted connections still come first
    d = {"erc": {}, "drc": {"errors": 2, "unconnected": 1, "unrouted_nets": ["GND"], "findings": [drill]}}
    assert VT._next(d).startswith("pcb_route()"), VT._next(d)


def _catalogue():
    import copper_kicad.tools as T   # noqa: F401
    return {"tools": [t.describe() for t in registry.TOOLS.values()]}


def test_catalogue_budget_and_shared_parameters():
    import copper_kicad.tools as T
    with Env(COPPER_CATALOGUE=None, COPPER_IMAGES=None, COPPER_COMPACT=None):
        tl = _catalogue()
        total = len(json.dumps(tl, ensure_ascii=False).encode())
        sizes = {t["name"]: len(json.dumps(t, ensure_ascii=False).encode()) for t in tl["tools"]}
        core = [t for t in tl["tools"] if (t.get("_meta") or {}).get("anthropic/alwaysLoad")]
        core_b = len(json.dumps(core, ensure_ascii=False).encode())
        with Env(COPPER_CATALOGUE="full"):
            full = len(json.dumps(_catalogue(), ensure_ascii=False).encode())
        NOTES.append(f"tools/list: {total} B for {len(sizes)} tools (owners' full text: {full} B); always-loaded {core_b} B "
                     f"({', '.join(t['name'] for t in core)}); largest {max(sizes.values())} B ({max(sizes, key=sizes.get)})")
        if T.LOAD_ERRORS:
            NOTES.append(f"tool modules failing to load (someone else's edit in progress?): {list(T.LOAD_ERRORS)}")
        stale = catalogue.stale()
        if stale:
            NOTES.append(f"slim descriptions that no longer match their tool (full text is served instead): {sorted(stale)}")
        assert total <= CATALOGUE_MAX, f"tools/list grew to {total} B (budget {CATALOGUE_MAX}): move text to help= or raise the budget on purpose"
        assert total < full
        for n in catalogue.SLIM:
            if n in sizes and n not in stale:
                assert len(catalogue.SLIM[n][1]) <= SLIM_DESCRIPTION_MAX, (n, len(catalogue.SLIM[n][1]))
                assert len(catalogue.SLIM[n][1]) < len(registry.TOOLS[n].description), n
        assert len(core) <= 8 and core_b <= CORE_MAX, (len(core), core_b)
        for t in tl["tools"]:
            ps = t["inputSchema"]["properties"]
            if isinstance(ps.get("project"), dict) and ps["project"].get("type") == "string":
                assert ps["project"]["description"] == catalogue.SHARED_PARAMS["project"], t["name"]
            if "while_open" in ps:
                assert ps["while_open"]["description"] == catalogue.SHARED_PARAMS["while_open"], t["name"]
            for k in ("render", "preview"):
                if (ps.get(k) or {}).get("type") == "boolean" and t["name"] not in compact.IMAGE_EXEMPT:
                    assert ps[k]["description"] == "attach a PNG (default false)", (t["name"], k)
            a = t["annotations"]
            assert "title" not in a and t["title"], t["name"]
            assert ("readOnlyHint" in a) != ("destructiveHint" in a), (t["name"], a)     # one or the other, never both
            assert isinstance(a["openWorldHint"], bool)
        assert all(len(v) <= 40 for v in catalogue.SHARED_PARAMS.values())
        assert _catalogue() == tl, "tools/list must be stable"
    with Env(COPPER_IMAGES="auto"):
        d = registry.TOOLS["pcb_place"].describe() if "pcb_place" in registry.TOOLS else None
        if d:
            assert d["inputSchema"]["properties"]["render"]["description"] == "attach a PNG (default true)"


def test_slim_descriptions_never_hide_a_changed_tool():
    """A slim text is tied to the tool's argument names. When the owner adds an argument, the owner's full
    description is served again (and stale() says so) rather than a text that does not know the argument."""
    saved, saved_slim = dict(registry.TOOLS), dict(catalogue.SLIM)
    try:
        long_text = "Does a demo thing with a very long explanation. " * 12 + "Example: zz_demo(x=1)"

        @registry.tool("zz_demo", long_text, {"x": {"type": "integer"}, "project": {"type": "string", "description": "long " * 20}})
        def zz_demo(x=1, project=None):
            return {"x": x}
        t = registry.TOOLS["zz_demo"]
        catalogue.slim("zz_demo", "project,x", "Demo thing. Example: zz_demo(x=1)")
        d = t.describe()
        assert d["description"] == "Demo thing. Example: zz_demo(x=1)" and "copper_help" not in d["description"], d
        assert d["inputSchema"]["properties"]["project"]["description"] == "project path (default: active)"
        assert t.schema["properties"]["project"]["description"].startswith("long"), "the registered schema is not modified"
        assert long_text in registry.help_text("zz_demo"), "the owner's full text is on the help page"
        assert t.has_help() and next(r for r in registry.help_index() if r["tool"] == "zz_demo")["help_chars"] >= len(long_text)
        bad = registry.call_tool("zz_demo", {"y": 1})["structuredContent"]
        assert "copper_help(tool='zz_demo')" in bad["help"], bad
        t.schema["properties"]["mode"] = {"type": "string"}               # the owner adds an argument
        assert t.describe()["description"] == long_text and catalogue.stale().get("zz_demo") == "mode,project,x"
        del t.schema["properties"]["mode"]
        with Env(COPPER_CATALOGUE="full"):
            f = t.describe()
            assert f["description"] == long_text and f["annotations"]["title"] and f["inputSchema"] is t.schema
    finally:
        registry.TOOLS.clear()
        registry.TOOLS.update(saved)
        catalogue.SLIM.clear()
        catalogue.SLIM.update(saved_slim)


def test_every_slimmed_tool_keeps_its_first_call():
    """Proxy for 'the slim text still lets a model make its first call': every example call in a slim text
    names the tool itself and only arguments the tool really has."""
    import copper_kicad.tools as T   # noqa: F401
    checked = 0
    for name, (_sig, text) in catalogue.SLIM.items():
        t = registry.TOOLS.get(name)
        if t is None:
            NOTES.append(f"slim text for '{name}' has no tool (renamed?)")
            continue
        props = set(t.schema["properties"])
        for m in re.finditer(r"\b%s\(([^()]*(?:\([^()]*\)[^()]*)*)\)" % re.escape(name), text):
            for kw in re.findall(r"(?:^|,\s*)([a-z_]+)=", m.group(1)):
                assert kw in props, f"{name}: slim text uses unknown argument '{kw}'"
                checked += 1
        for other in re.findall(r"\b([a-z]+_[a-z_]+)\(", text):
            assert other in registry.TOOLS, f"{name}: slim text names unknown tool '{other}'"
    assert checked >= 20, checked


# ── replays through the stdio server (need KiCad) ────────────────────────────────────────────────
def _kicad_ok() -> bool:
    if os.environ.get("COPPER_TEST_NO_REPLAY"):
        NOTES.append("replay tests skipped (COPPER_TEST_NO_REPLAY)")
        return False
    try:
        from copper_kicad.kicad import env as kenv
        return bool(kenv.kicad().cli)
    except Exception as e:  # noqa: BLE001
        NOTES.append(f"replay tests skipped: KiCad not found ({e})")
        return False


def _table(tag: str, rep: dict):
    print(f"   {tag}: per call")
    for r in rep["calls"]:
        if r.get("skipped"):
            continue
        print(f"     {r['tool']:18s} {r['stage']:12s} {r['text_bytes']:6d} B  {len(r['images'])} img (~{r['img_tokens']} tok)  {r['secs']:5.1f} s"
              + ("  ERROR" if r["is_error"] else ""))
    t = rep["total"]
    print(f"     TOTAL {t['calls']} calls, {t['text_bytes']} B text (~{t['text_tokens']} tok), {t['images']} images (~{t['img_tokens']} tok), "
          f"args {t['arg_bytes']} B, {t['seconds']} s; tools/list {rep['tools_list_bytes']} B, instructions {rep['instructions_bytes']} B")


def _common(rep: dict, script: list[dict], sb: dict):
    """Assertions every replay must pass."""
    proj = str(sb["proj"]).lower()
    variants = {proj, proj.replace("\\", "/"), proj.replace("\\", "\\\\")}
    registered = set(rep["tools"])
    for row, res in zip(rep["calls"], rep["results"]):
        if row.get("skipped"):
            continue
        assert not row["is_error"], (row["tool"], row["head"])
        assert "structuredContent" not in res, row["tool"]
        assert row["text_bytes"] <= RESULT_CAP, (row["tool"], row["text_bytes"])
        text = RP.text_of(res)
        low = text.lower()
        hits = sum(low.count(v) for v in variants)
        allowed = 1 if row["tool"] in compact.ABS_DIR_TOOLS else 0
        if row["tool"] == "copper_status":
            allowed = 4                                    # active project dir + the sandbox's own config / tools paths
        assert hits <= allowed, f"{row['tool']}: the project path appears {hits}x (allowed {allowed}): {text[:400]}"
        d = RP.payload_of(res)
        nxt = d.get("next")
        if isinstance(nxt, str) and nxt:
            m = re.match(r"\s*(?:[^()]*?\b)?([a-z][a-z0-9_]+)\(", nxt)
            assert m and m.group(1) in registered, f"{row['tool']}: next is not a call to a registered tool: {nxt!r}"


def _build_done(c, args: dict, max_calls: int = 8):
    """board_build until it no longer says `pending`, as a client does. The time box (45 s) is reached on a
    loaded machine; what these tests judge is the finished board, not how many calls the machine needed."""
    r = c.call("board_build", args)
    d = RP.payload_of(r)
    n = 1
    while not r["isError"] and d.get("pending") and n < max_calls:
        r = c.call("board_build", args)
        d = RP.payload_of(r)
        n += 1
    return r, d


_SHORT: dict = {}


def test_replay_short_path_budgets():
    """project_create, sch_apply, sch_tidy, board_build, fab pack, copper_status: six calls (seven when board_build
    hits its time box on a slow machine and is resumed), budgets per stage."""
    if not _kicad_ok():
        return
    sb = RP.sandbox()
    if not sb["lib_tables"]:
        NOTES.append("KiCad template library tables not found: byte budgets may be exceeded by library warnings")
    script = RP.load("c3_short")
    rep = RP.run(script, sb, quiet=True)
    _table("short path", rep)
    _SHORT.update(sb=sb, rep=rep)
    _common(rep, script, sb)
    t, st = rep["total"], rep["stages"]
    builds = [i for i, r in enumerate(rep["calls"]) if r["tool"] == "board_build"]
    if len(builds) > 1:
        NOTES.append(f"board_build hit its 45 s time box and was resumed: {len(builds)} calls "
                     f"({', '.join(str(rep['calls'][i]['secs']) + ' s' for i in builds)})")
    assert t["calls"] == 5 + len(builds) and t["errors"] == 0, t
    build_s = sum(rep["calls"][i]["secs"] for i in builds)
    extra = 0                            # bytes of the `pending` reports a loaded machine adds (each is under 900 B)
    if build_s <= 90:                    # the reference machine builds this board in about 40 s
        assert len(builds) <= 2, (len(builds), build_s)
    else:
        extra = 900 * max(0, len(builds) - 2)
        assert all(rep["calls"][i]["text_bytes"] <= 900 for i in builds[:-1]), [rep["calls"][i]["text_bytes"] for i in builds]
        NOTES.append(f"machine too loaded to judge the board_build call count ({build_s:.0f} s of board_build for a 40 s board)")
    for stage, cap in SHORT.items():
        if stage != "total":
            cap += extra if stage == "3 board" else 0
            assert st[stage]["text_bytes"] <= cap, (stage, st[stage]["text_bytes"], cap)
    assert t["text_bytes"] <= SHORT["total"] + extra, t
    assert t["images"] == 1 and t["img_tokens"] <= 700, (t["images"], t["img_tokens"])       # the one picture board_build attaches
    assert rep["tools_list_bytes"] <= CATALOGUE_MAX and rep["instructions_bytes"] <= INSTRUCTIONS_MAX, rep["instructions_bytes"]
    bb = RP.payload_of(rep["results"][builds[-1]])
    assert bb["ok"] is True and isinstance(bb["fab_ready"], bool) and bb["state"] == "new", bb
    assert set(bb["stages"]) == {"sync", "setup", "place", "route", "pour"}, bb["stages"]
    assert set(bb["drc"]) == {"errors", "warnings", "unrouted", "parity"} and len(bb["erc"]) == 2 and bb["next"], bb
    assert bb["drc"]["parity"] == 0 and "guided" in bb["stages"]["route"] and bb["checkpoint"], bb
    assert len(bb.get("findings") or []) <= 8 and all(len(f.get("fix", "")) <= 200 for f in bb.get("findings") or [])
    NOTES.append(f"board_build on the reference board: fab_ready={bb['fab_ready']}, DRC {bb['drc']}, {bb['seconds']} s"
                 + (f", blockers {bb.get('blockers')}" if bb.get("blockers") else ""))
    # the meter agrees with what the client counted (the status call reports the five calls before it)
    status = RP.payload_of(rep["results"][-1])
    s = status["session"]
    client_bytes = sum(r["text_bytes"] for r in rep["calls"][:-1])
    assert s["calls"] == t["calls"] - 1 and s["errors"] == 0 and s["images"] == 1, s
    assert abs(s["result_kb"] * 1000 - client_bytes) <= max(60, 0.02 * client_bytes), (s["result_kb"], client_bytes)
    assert s["est_tokens"]["catalogue"] == (rep["tools_list_bytes"] + rep["instructions_bytes"]) // 4, s
    assert s["est_tokens"]["images"] == rep["total"]["img_tokens"], s
    files = list((sb["home"] / "sessions").glob("*.json"))
    assert len(files) == 1, "the session file must exist after EOF"
    led = json.loads(files[0].read_text(encoding="utf-8"))
    assert sum(v["calls"] for v in led["tools"].values()) == t["calls"] and led["client"].startswith("budget-replay"), led
    assert str(sb["proj"]).split(os.sep)[-1] not in files[0].read_text(encoding="utf-8"), "no paths in the session file"


def test_replay_board_build_keeps_existing_work_and_resumes():
    """On the board the short path just built: add two parts and rebuild. Every existing track, via, footprint
    position and the (rounded) outline must be untouched; then the time box: a 1 s budget returns `pending`
    without error and the next call finishes without running any step twice."""
    if not _SHORT:
        NOTES.append("existing-board test skipped (the short-path replay did not run)")
        return
    from copper_kicad import project as P
    from copper_kicad.pcb import board
    sb = _SHORT["sb"]
    pr = P.Project(sb["proj"] / "c3ref", "c3ref")
    saved_env = {k: os.environ.get(k) for k in ("COPPER_HOME", "KICAD_CONFIG_HOME")}
    os.environ.update(COPPER_HOME=str(sb["home"]), KICAD_CONFIG_HOME=str(sb["kcfg"]))
    try:
        def snap():
            s = board.read(pr, what=["geom", "tracks"])
            return ({t["uuid"]: json.dumps(t, sort_keys=True) for t in s.get("track_list", [])},
                    {f["ref"]: (f["x"], f["y"], f["rot"], f["side"]) for f in s["footprints"]}, json.dumps(s["outline"], sort_keys=True))
        tracks0, fps0, outline0 = snap()
        assert len(tracks0) > 50 and len(fps0) == 17, (len(tracks0), len(fps0))
        c = RP.Client(sb["env"], stderr_path=sb["base"] / "server2.err")
        try:
            r = c.call("sch_apply", {"ops": [{"op": "place", "spec": "1k 0402", "id": "r9"}, {"op": "place", "spec": "LED red 0603", "id": "d9"},
                                             {"op": "connect", "pins": ["r9.1"], "net": "+3V3"},
                                             {"op": "connect", "pins": ["r9.2", "d9.A"], "net": "LED2_A"},
                                             {"op": "connect", "pins": ["d9.K"], "net": "GND"}]})
            assert not r["isError"], RP.text_of(r)[:400]
            assert not any(b["type"] == "image" for b in r["content"]), "sch_apply attaches no picture unless asked"
            # asks for a different size on purpose: the existing outline must win
            r, d = _build_done(c, {"width": 40, "height": 30, "image": "none"})
            assert not r["isError"] and d["state"] == "existing", RP.text_of(r)[:600]
            assert "kept the existing 60x36 mm outline" in d["stages"]["setup"] and "outline='replace'" in d["stages"]["setup"], d["stages"]
            assert "after pour" not in d["stages"]["route"], d["stages"]       # no new zone, so no second router pass
            assert d["stages"]["sync"].startswith("+2 parts") and "2 new parts placed" in d["stages"]["place"], d["stages"]
            assert not any(b["type"] == "image" for b in r["content"]) and len(RP.text_of(r).encode()) <= 2_800
            tracks1, fps1, outline1 = snap()
            assert outline1 == outline0, "the outline was redrawn"
            lost = [u for u in tracks0 if u not in tracks1]
            moved = [u for u in tracks0 if u in tracks1 and tracks1[u] != tracks0[u]]
            assert not lost and not moved, f"{len(lost)} existing tracks/vias deleted, {len(moved)} changed"
            assert all(fps1[k] == v for k, v in fps0.items()), "an existing footprint moved"
            assert set(fps1) - set(fps0) == {"R6", "D2"}, set(fps1) - set(fps0)
            x0, y0, x1, y1 = json.loads(outline1)["bbox"]
            assert all(x0 <= fps1[k][0] <= x1 and y0 <= fps1[k][1] <= y1 for k in ("R6", "D2")), "new parts must be on the board"
            NOTES.append(f"rebuild after adding 2 parts: {len(tracks0)} existing tracks/vias untouched, +{len(tracks1) - len(tracks0)} new; "
                         f"DRC {d.get('drc')}")
            # nothing changed since: sync is skipped, nothing is placed, and the board file stays byte-identical
            before = pr.pcb.read_bytes()
            d2 = RP.payload_of(c.call("board_build", {"steps": ["sync", "setup", "place"], "image": "none"}))
            assert "skipped" in d2["stages"]["sync"] and "kept" in d2["stages"]["place"], d2["stages"]
            assert pr.pcb.read_bytes() == before, "a no-op board_build rewrote the board"
            # a second writer on the same project is refused at once (in-process lock)
            import copper_kicad.tools.flow_tools as FT
            lock, _info = FT._BUSY.setdefault(str(P.resolve(str(pr.dir), activate=False).dir).lower(), (threading.Lock(), {}))
            lock.acquire()
            try:
                t0 = time.time()
                busy = registry.call_tool("board_build", {"project": str(pr.dir)})
                assert busy["isError"] and busy["structuredContent"]["error"] == "BUSY" and time.time() - t0 < 2, busy
            finally:
                lock.release()
        finally:
            c.close()
        # time box + resume, on a fresh copy of the schematic (new board)
        sb2 = RP.sandbox()
        script = RP.load("c3_short")[:3]
        c2 = RP.Client(sb2["env"], stderr_path=sb2["base"] / "server.err")
        try:
            for step in script:
                a = json.loads(json.dumps(step["args"]).replace("$DIR", str(sb2["proj"]).replace("\\", "/")))
                assert not c2.call(step["tool"], a)["isError"]
            seen: list[str] = []
            calls = 0
            d = {"pending": ["x"]}
            while d.get("pending") and calls < 8:
                r = c2.call("board_build", {"width": 60, "height": 36, "time_budget": 1} if calls == 0 else {"time_budget": 1})
                d = RP.payload_of(r)
                assert not r["isError"], RP.text_of(r)[:500]
                calls += 1
                if d.get("pending"):
                    assert d["ok"] is False and d["next"].startswith("board_build(") and len(d["done"]) == calls, d
                    assert not any(b["type"] == "image" for b in r["content"]), "no picture until the build is finished"
                    # a pending report names the step this call ran, not the ones earlier calls already reported
                    assert len(d["stages"]) == 1 and len(RP.text_of(r).encode()) <= 900, (d["stages"], len(RP.text_of(r).encode()))
            assert calls == 6 and not d.get("pending") and d["ok"] is True and d["state"] == "new", (calls, d)
            assert set(d["stages"]) == {"sync", "setup", "place", "route", "pour"} and "drc" in d, d
            assert "+17 parts" in d["stages"]["sync"] and "17 placed" in d["stages"]["place"], d["stages"]      # each step ran once
            NOTES.append(f"time box: time_budget=1 finished the board in {calls} calls, one step each, no step repeated")
        finally:
            c2.close()
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_replay_hand_routed_demo_is_left_alone():
    """A copy of KiCad's hand-routed pic_programmer demo: board_build (called with a width and height on purpose)
    must not delete or change one track or via, move one footprint, or touch the outline."""
    if not _kicad_ok():
        return
    import shutil
    from copper_kicad import project as P
    from copper_kicad.kicad import env as kenv
    from copper_kicad.pcb import board
    demo = next((d for d in (Path(kenv.kicad().root) / "share" / "kicad" / "demos" / "pic_programmer",
                             Path("/usr/share/kicad/demos/pic_programmer")) if d.is_dir()), None)
    if demo is None:
        NOTES.append("hand-routed demo test skipped (KiCad demos not installed)")
        return
    sb = RP.sandbox()
    dst = sb["proj"] / "pic_programmer"
    shutil.copytree(demo, dst)                                   # never the original
    for f in dst.rglob("*"):
        if f.is_file():
            os.chmod(f, 0o666)
    pr = P.Project(dst, "pic_programmer")
    saved_env = {k: os.environ.get(k) for k in ("COPPER_HOME", "KICAD_CONFIG_HOME")}
    os.environ.update(COPPER_HOME=str(sb["home"]), KICAD_CONFIG_HOME=str(sb["kcfg"]))
    try:
        def snap():
            s = board.read(pr, what=["geom", "tracks"])
            return ({t["uuid"]: json.dumps(t, sort_keys=True) for t in s.get("track_list", [])},
                    {f["ref"]: (f["x"], f["y"], f["rot"], f["side"], f.get("footprint")) for f in s["footprints"]},
                    json.dumps(s.get("outline"), sort_keys=True))
        tracks0, fps0, outline0 = snap()
        assert len(tracks0) > 200 and len(fps0) > 40, (len(tracks0), len(fps0))
        c = RP.Client(sb["env"], stderr_path=sb["base"] / "server.err")
        try:
            assert not c.call("project_open", {"path": str(dst / "pic_programmer.kicad_pro")})["isError"]
            r, d = _build_done(c, {"width": 40, "height": 30})
            assert not r["isError"] and d["state"] == "existing" and not d.get("pending"), RP.text_of(r)[:600]
            assert "kept the existing" in d["stages"]["setup"] and "kept" in d["stages"]["place"], d["stages"]
            assert len(RP.text_of(r).encode()) <= 2_800, len(RP.text_of(r).encode())
        finally:
            c.close()
        tracks1, fps1, outline1 = snap()
        lost = [u for u in tracks0 if u not in tracks1]
        changed = [u for u in tracks0 if u in tracks1 and tracks1[u] != tracks0[u]]
        assert not lost and not changed, f"{len(lost)} hand-routed tracks/vias deleted, {len(changed)} changed"
        assert fps1 == fps0, [k for k in fps0 if fps1.get(k) != fps0[k]][:8]
        assert outline1 == outline0, "the outline was redrawn"
        NOTES.append(f"hand-routed demo (pic_programmer): {len(tracks0)} tracks/vias, {len(fps0)} footprints and the outline untouched; "
                     f"DRC {d.get('drc')}, fab_ready={d.get('fab_ready')}")
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_replay_foreign_board_keeps_its_footprints_and_hand_placement():
    """Two things board_build did to work it did not make, found on boards the builder had not tried:
    1. KiCad's complex_hierarchy demo (hand-routed, two instances of one sheet): the schematic names every
       footprint through the stock libraries, the board through a project-local copy, and a plain board_build()
       swapped all 68 footprints (DRC went from 0 to 2 errors). A board with copper that this tool did not build
       keeps its footprints; the difference is reported.
    2. A board without copper where two parts had been placed by hand: the auto-placer moved them. Parts already
       inside the outline, without overlaps, stay where they were put."""
    if not _kicad_ok():
        return
    import shutil
    from copper_kicad import project as P
    from copper_kicad.kicad import env as kenv
    from copper_kicad.pcb import board
    sb = RP.sandbox()
    saved_env = {k: os.environ.get(k) for k in ("COPPER_HOME", "KICAD_CONFIG_HOME")}
    os.environ.update(COPPER_HOME=str(sb["home"]), KICAD_CONFIG_HOME=str(sb["kcfg"]))
    c = None
    try:
        c = RP.Client(sb["env"], stderr_path=sb["base"] / "server.err")
        demo = next((d for d in (Path(kenv.kicad().root) / "share" / "kicad" / "demos" / "complex_hierarchy",
                                 Path("/usr/share/kicad/demos/complex_hierarchy")) if d.is_dir()), None)
        if demo is None:
            NOTES.append("foreign-board footprint test skipped (KiCad demos not installed)")
        else:
            dst = sb["proj"] / "hier dém"                           # a space and a non-ASCII letter on purpose
            shutil.copytree(demo, dst)
            for f in dst.rglob("*"):
                if f.is_file():
                    os.chmod(f, 0o666)
            pr = P.Project(dst, "complex_hierarchy")

            def snap():
                s = board.read(pr, what=["geom", "tracks"])
                return ({f["ref"]: (f["x"], f["y"], f["rot"], f["side"], f.get("footprint")) for f in s["footprints"]},
                        {t["uuid"]: json.dumps(t, sort_keys=True) for t in s.get("track_list", [])})
            fps0, tracks0 = snap()
            assert len(fps0) > 60 and len(tracks0) > 300, (len(fps0), len(tracks0))
            assert not c.call("project_open", {"path": str(dst)})["isError"]
            r, d = _build_done(c, {})
            assert not r["isError"] and d["state"] == "existing" and not d.get("pending"), RP.text_of(r)[:600]
            fps1, tracks1 = snap()
            assert fps1 == fps0, f"{sum(1 for k in fps0 if fps1.get(k) != fps0[k])} footprints of a hand-made board were replaced or moved"
            assert tracks1 == tracks0, "tracks of a hand-made board changed"
            assert "kept as drawn" in d["stages"]["sync"] and "swapped" not in d["stages"]["sync"], d["stages"]
            assert d["drc"]["errors"] == 0, d["drc"]
            assert d.get("blockers") == ["parity"] and d["fab_ready"] is False and "verify(what=['drc'])" in d["next"], d
            assert len(RP.text_of(r).encode()) <= 2_800, len(RP.text_of(r).encode())
            NOTES.append(f"foreign board (complex_hierarchy): {len(fps0)} footprints and {len(tracks0)} tracks untouched, "
                         f"{d['drc']['parity']} footprint differences reported, not swapped")

        # 2. hand-placed parts on a board without copper
        ops = [{"op": "place", "lib_id": "MCU_Microchip_ATtiny:ATtiny85-20S", "id": "mcu"},
               {"op": "place", "spec": "100nF 0402", "id": "c1", "near": "mcu"},
               {"op": "place", "spec": "1k 0402", "id": "r1"},
               {"op": "place", "spec": "LED red 0603", "id": "d1", "near": "r1"},
               {"op": "place", "lib_id": "Connector_Generic:Conn_01x04", "id": "j1",
                "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"},
               {"op": "connect", "pins": ["mcu.8", "c1.1", "j1.1"], "net": "+5V"},
               {"op": "connect", "pins": ["mcu.4", "c1.2", "d1.K", "j1.2"], "net": "GND"},
               {"op": "connect", "pins": ["mcu.5", "r1.1"], "net": "LED"},
               {"op": "connect", "pins": ["r1.2", "d1.A"], "net": "LED_A"},
               {"op": "connect", "pins": ["mcu.6", "j1.4"], "net": "SIG"}]
        r = c.call("project_create", {"name": "handplaced", "dir": str(sb["proj"]), "board_size": [40, 30]})
        assert not r["isError"], RP.text_of(r)[:400]
        r = c.call("sch_apply", {"ops": ops})
        assert not r["isError"], RP.text_of(r)[:600]
        assert not c.call("pcb_sync", {})["isError"]
        x0, y0, x1, y1 = RP.payload_of(c.call("pcb_read", {}))["outline"]["bbox"]
        want = {"U1": ((x0 + x1) / 2, (y0 + y1) / 2, 90.0), "J1": (x0 + 4, (y0 + y1) / 2 - 4, 0.0)}
        r = c.call("pcb_place", {"moves": [{"ref": k, "x": v[0], "y": v[1], "rot": v[2]} for k, v in want.items()]})
        assert not r["isError"], RP.text_of(r)[:400]
        r, d = _build_done(c, {})
        assert not r["isError"] and d["state"] == "new" and not d.get("pending"), RP.text_of(r)[:600]
        assert "2 already on the board kept" in d["stages"]["place"], d["stages"]
        got = {f["ref"]: (f["x"], f["y"], f["rot"]) for f in RP.payload_of(c.call("pcb_read", {"refs": ["U1", "J1"]}))["footprints"]}
        for ref, (x, y, rot) in want.items():
            assert abs(got[ref][0] - x) < 0.01 and abs(got[ref][1] - y) < 0.01 and abs(got[ref][2] - rot) < 0.01, (ref, got[ref], want[ref])
        assert d["drc"]["unrouted"] == 0 and d["drc"]["errors"] == 0, d["drc"]
        # the ERC errors of this little schematic come back with a fix that can be run as it stands
        assert d.get("findings") and d["next"].startswith("sch_apply(ops=[") and d["next"].endswith("])"), d.get("next")
        NOTES.append(f"hand-placed parts: U1 and J1 stayed where they were put; {d['stages']['place']}; next = {d['next'][:70]}")
    finally:
        if c is not None:
            c.close()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v



def test_replay_long_flow_has_no_unrequested_images():
    """The 18-call flow with no image flags at all: single-step tools attach nothing, the three explicit
    render calls return three small pictures, and the text total is under budget."""
    if not _kicad_ok():
        return
    sb = RP.sandbox()
    script = RP.load("c3_defaults")
    rep = RP.run(script, sb, quiet=True)
    _table("long flow, no image flags", rep)
    _common(rep, script, sb)
    for row in rep["calls"]:
        if row["tool"] != "render":
            assert not row["images"], f"{row['tool']} attached a picture nobody asked for"
        else:
            assert len(row["images"]) == 1 and max(row["images"][0]["w"], row["images"][0]["h"]) <= 1000, row["images"]
    t = rep["total"]
    assert t["calls"] == 18 and t["images"] == 3 and t["img_tokens"] <= 2_600, t
    assert t["text_bytes"] <= DEFAULTS_TOTAL, t
    rd = RP.payload_of(next(r for row, r in zip(rep["calls"], rep["results"]) if row["tool"] == "pcb_read"))
    assert "footprints" not in rd and rd.get("footprint_count") == 17 and "pcb_read(what=" in rd["more"], rd
    NOTES.append(f"long flow: {t['text_bytes']} B text + {t['images']} images (~{t['img_tokens']} tok) in {t['calls']} calls "
                 f"(before this work: 47,803 B + 10 images, ~15,659 tok)")


def test_two_servers_serve_identical_catalogues():
    if os.environ.get("COPPER_TEST_NO_REPLAY"):
        return
    sb = RP.sandbox() if _kicad_ok() else None
    if sb is None:
        return
    outs = []
    for _ in range(2):
        c = RP.Client(sb["env"])
        try:
            outs.append((json.dumps(c.req("tools/list", {})[0], sort_keys=False), c.init.get("instructions")))
        finally:
            c.close()
    assert outs[0] == outs[1], "tools/list and instructions must be byte-identical across processes (prompt caches key on them)"
    assert "{fab}" not in outs[0][1] and len(outs[0][1].encode()) <= INSTRUCTIONS_MAX, len(outs[0][1].encode())


def _route_result_with_constraints(groups: int, pairs: int, extras: bool = True) -> dict:
    """A v2 route result as the engine + session hand it to the tool, with a constraints block of the given size
    (design §14.10): half of the rows are misses, per-net figures; with ``extras`` also a name-detected group,
    keep-short nets, a width rule, an unread rule and a KiCad error nothing explains (every finding code at once)."""
    g_rows, p_rows, nets = [], [], {}
    for i in range(groups):
        miss = i % 2 == 1
        members = [f"/BUS{i}_D{k}" for k in range(8)] + [f"/BUS{i}_CLK"]
        row = {"name": f"bus{i}", "n": 9, "target": 41.2034 + i, "min": 38.7012 + i if miss else 40.9811 + i,
               "max": 41.2034 + i, "tol": 0.5, "src": "dru" if i % 3 else "plan", "worst": members[2],
               "dev": -2.5022 if miss else -0.2223, "ok": not miss}
        if miss:
            row["short_by"] = 2.0022
        g_rows.append(row)
        for n in members:
            nets[n] = [41.2034, 39.6234, 1.58, 0.0, 1]
    if extras:
        g_rows.append({"name": "sd:/SD_", "n": 6, "target": 30.1, "min": 22.4, "max": 30.1, "tol": 2.0, "src": "name",
                       "worst": "/SD_D3", "dev": -7.7, "ok": None, "mode": "report",
                       "nets": ["/SD_CLK", "/SD_CMD", "/SD_D0", "/SD_D1", "/SD_D2", "/SD_D3"]})
    for i in range(pairs):
        miss = i % 2 == 0
        row = {"p": f"/LANE{i}_P", "n": f"/LANE{i}_N", "src": "dru", "gap": 0.15, "len": [30.1234 + i, 30.9876 + i],
               "skew": 0.8642, "coupled": 0, "uncoupled": 30.9876, "gap_seen": 0.2, "max_skew": 0.15,
               "max_uncoupled": 6.0, "ok": not miss}
        if miss:
            row.update({"skew_over": 0.7142, "uncoupled_over": 24.9876,
                        "kicad_errors": ["diff_pair_uncoupled_length_too_long", "skew_out_of_range"]})
        else:
            row.update({"skew": 0.1021, "coupled": 88, "uncoupled": 3.2})
        p_rows.append(row)
        nets[row["p"]] = nets[row["n"]] = [30.1234, 30.1234, 0.0, 0.0, 0]
    cons = {"mode": "auto", "ok": groups // 2 + pairs // 2 + 1, "missed": groups - groups // 2 + pairs - pairs // 2 + 1,
            "groups": g_rows, "pairs": p_rows,
            "short": [{"net": "/XI", "len": 9.1234, "max": 8.0, "ok": False, "long_by": 1.1234},
                      {"net": "/XO", "len": 4.2, "max": 8.0, "ok": True}] + [{"net": f"/SW{k}", "len": 6.0 + k} for k in range(9)],
            "widths": {"n": 3, "off": [["/CLK50", "In2.Cu", 0.11, 0.15, 3.2]]},
            "unread": ["odd_rule"], "kicad": {"skew_out_of_range": 12, "diff_pair_uncoupled_length_too_long": 6},
            "len_model_err": 0.0012, "kicad_other": [["too_many_vias", "/W"]], "nets": nets}
    if not extras:
        for k in ("short", "widths", "unread", "kicad_other"):
            cons.pop(k)
        cons["ok"], cons["missed"] = groups // 2 + pairs // 2, groups - groups // 2 + pairs - pairs // 2
    return {"routed": 240, "total": 240, "percent": 100.0, "failed": [], "skipped": [], "engine": "v2",
            "nets": {n: v[:2] for n, v in list(nets.items())[:12]},
            "added": {"tracks": 1800, "vias": 240, "length_mm": 6400.5},
            "drc": {"errors": 18, "new_errors": 18, "constraint_errors": 18, "warnings": 4, "unconnected": 0, "parity": 0,
                    "by_type": {"error:skew_out_of_range": 12, "error:diff_pair_uncoupled_length_too_long": 6}},
            "passes": [{"pass": "classify", "connections": 240, "routed": 0, "seconds": 0.0, "work": 0},
                       {"pass": "detail", "connections": 240, "routed": 240, "seconds": 20.1, "work": 9_000_000}],
            "plan_used": {"global": {"grid": 0.1, "via_cost": 5, "bend_cost": 0.3, "ripup_rounds": 3}},
            "warnings": [], "stages": [{"name": "classify"}], "judge": {"rounds": [], "commits": 1},
            "auto": {"roles": {}, "constraints": {"mode": "auto", "groups": [{"name": "bus0"}]}},
            "seconds": 31.2, "written": True, "checkpoint": "before-guided-route-1", "revision": "abc",
            "unrouted_before": 240, "unrouted_after": 0, "ripped_up": 0, "totals": {"tracks": 1800, "vias": 240}, "constraints": cons}


def test_constraints_block_stays_inside_the_result_cap():
    """12 length groups + 12 pairs (design §14.10): the default result lists the misses first, at most 6 rows per
    kind, and fits the 6 000 B cap without the wire layer having to cut anything; every miss keeps its amount in
    mm. With every other kind of constraint finding on top, the cap still holds and what gives way is the list of
    findings, never a constraint row."""
    from copper_kicad.tools import route_tools as R

    def brief(o):
        o["drc"] = {k: o["drc"][k] for k in ("errors", "new_errors", "constraint_errors", "warnings", "unconnected", "parity")}

    plain = _route_result_with_constraints(12, 12, extras=False)
    full = len(json.dumps(plain["constraints"], separators=(",", ":")).encode())
    plain["findings"] = R._findings(plain)
    R._compact_v2(plain)
    brief(plain)
    text, got, _w = wire(plain, "guided route: 240/240 connections", tool="pcb_route_guided")
    NOTES.append(f"constraints: 12 groups + 12 pairs = {full} B in full, the default result is {len(text.encode())} B in all")
    assert len(text.encode()) <= RESULT_CAP and "cut" not in got, (len(text.encode()), got.get("cut"))
    assert len(got["constraints"]["groups"]) == len(got["constraints"]["pairs"]) == 6
    assert [f["code"] for f in got["findings"]] == ["LENGTH_MISMATCH", "PAIR_SKEW", "PAIR_UNCOUPLED"]

    out = _route_result_with_constraints(12, 12)
    out["findings"] = R._findings(out)
    R._compact_v2(out)
    c = out["constraints"]
    assert "nets" not in c and len(c["groups"]) == len(c["pairs"]) == R.CONS_MAX == 6
    assert all(r["ok"] is False for r in c["groups"]) and all(r["ok"] is False for r in c["pairs"])   # misses first
    assert all("short_by" in r for r in c["groups"]) and all("skew_over" in r and "uncoupled_over" in r for r in c["pairs"])
    assert all("max_skew" not in r and abs(r["skew"] - r["skew_over"] - 0.15) < 1e-9 for r in c["pairs"])   # limit = value - amount
    assert c["groups"][0]["short_by"] == 2.0 and c["groups"][0]["target"] == 42.2 and c["pairs"][0]["len"] == [30.12, 30.99]
    assert [r["net"] for r in c["short"]] == ["/XI", "/XO"] and c["short"][0]["long_by"] == 1.12       # soft ones: counted
    assert (c["ok"], c["missed"]) == (13, 13) and c["kicad"]["skew_out_of_range"] == 12                 # counts are whole
    assert "constraints (+22 rows, per-net lengths)" in out["more"]
    brief(out)
    text, got, _w = wire(out, "guided route: 240/240 connections", tool="pcb_route_guided")
    assert len(text.encode()) <= RESULT_CAP, len(text.encode())
    assert set((got.get("cut") or {}).get("lists") or {}) <= {"findings"}, got.get("cut")
    assert got["constraints"]["groups"] == c["groups"] and got["constraints"]["pairs"] == c["pairs"]
    assert got["constraints"]["short"] == c["short"]
    # findings: one per code, naming the first CONS_FIND_MAX cases with their amounts and how many more there
    # are; none of them is the "could not repair or attribute" DRC error: every new error is a constraint marker
    codes = [f["code"] for f in out["findings"]]
    for code in ("LENGTH_MISMATCH", "PAIR_SKEW", "PAIR_UNCOUPLED", "KEEP_SHORT_LONG", "WIDTH_NOT_HELD",
                 "CONSTRAINT_GUESSED", "RULE_NOT_READ", "KICAD_CONSTRAINT_ERRORS"):
        assert codes.count(code) == 1, (code, codes)
    assert "DRC_ERRORS_NEW" not in codes
    by = {f["code"]: f for f in out["findings"]}
    assert by["LENGTH_MISMATCH"]["message"].startswith("6 length group(s) outside their window: bus1: /BUS1_D2 2 mm short")
    assert "(+3 more in constraints)" in by["LENGTH_MISMATCH"]["message"] and by["LENGTH_MISMATCH"]["severity"] == "warning"
    assert "0.71 over the limit 0.15" in by["PAIR_SKEW"]["message"] and "6 pair(s)" in by["PAIR_SKEW"]["message"]
    assert "lanes 0.2 apart not 0.15" in by["PAIR_UNCOUPLED"]["message"] and "(limit 6)" in by["PAIR_UNCOUPLED"]["message"]
    assert "1.12 over its maximum 8" in by["KEEP_SHORT_LONG"]["message"] and "0.11 mm wide" in by["WIDTH_NOT_HELD"]["message"]
    assert by["KICAD_CONSTRAINT_ERRORS"]["severity"] == "error" and "too_many_vias /W" in by["KICAD_CONSTRAINT_ERRORS"]["message"]
    assert all(f.get("fix") for f in out["findings"] if f["code"] in by and f["code"] != "NOTE")
    assert "sd:/SD_ (6 nets" in by["CONSTRAINT_GUESSED"]["message"] and "suggest[]" in by["CONSTRAINT_GUESSED"]["fix"]
    assert out["suggest"][0]["patch"] == {"groups": [{"nets": ["/SD_CLK", "/SD_CMD", "/SD_D0", "/SD_D1", "/SD_D2", "/SD_D3"],
                                                      "tol": 2.0}]}
    # a copper error beside them is still reported as one
    out2 = _route_result_with_constraints(1, 1)
    out2["drc"]["new_errors"] = 20
    assert [f["message"][:2] for f in R._findings(out2) if f["code"] == "DRC_ERRORS_NEW"] == ["2 "]
    # a result without constraints is untouched by all of this
    out3 = _route_result_with_constraints(0, 0)
    del out3["constraints"]
    out3["drc"].pop("constraint_errors")
    out3["drc"]["new_errors"] = 0
    f3 = R._findings(out3)
    R._compact_v2(out3)
    assert "constraints" not in out3 and "constraints" not in out3.get("more", "")
    assert not [f for f in f3 if f["code"] in ("LENGTH_MISMATCH", "PAIR_SKEW", "CONSTRAINT_GUESSED")]


def test_tuned_groups_report_their_shortfall_inside_the_result_cap():
    """Length tuning (design §14.7): a group the tuner could not close says how many members got meanders, how
    many mm they add, why it stopped and what the free runs could hold. 12 such groups + 12 pairs still fit the
    6 000 B cap with nothing cut; the per-net tuning table goes with detail only."""
    from copper_kicad.tools import route_tools as R
    why = "no free straight run: 5.53 mm of the 11.47 mm needed could be added"
    out = _route_result_with_constraints(12, 12, extras=False)
    for i, g in enumerate(out["constraints"]["groups"]):
        if g.get("ok") is False:
            g.update(tuned=3, added=12.3456, why=why, capacity=5.53)
    out["constraints"]["tuning"] = {f"/BUS{i}_D{k}": [30.1234, 41.5678, 7] + ([why] if k == 2 else [])
                                    for i in range(1, 13) for k in range(5)}
    out["findings"] = R._findings(out)
    f = {x["code"]: x for x in out["findings"]}["LENGTH_MISMATCH"]
    assert "no free straight run: 5.53 mm of the 11.47 mm needed could be added)" in f["message"]
    assert "groups[].amp" in f["fix"] and "capacity" in f["fix"] and "not laid" not in f["fix"]
    assert "no meanders are laid" not in json.dumps(out["findings"])
    R._compact_v2(out)
    c = out["constraints"]
    assert "tuning" not in c and "nets" not in c and len(c["groups"]) == 6
    assert all(r["ok"] is False and r["why"] == why and r["capacity"] == 5.53 and r["tuned"] == 3 and r["added"] == 12.35
               and "short_by" in r for r in c["groups"])
    out["drc"] = {k: out["drc"][k] for k in ("errors", "new_errors", "constraint_errors", "warnings", "unconnected", "parity")}
    text, got, _w = wire(out, "guided route: 240/240 connections", tool="pcb_route_guided")
    NOTES.append(f"constraints: 12 tuned-but-short groups + 12 pairs: the default result is {len(text.encode())} B in all")
    assert len(text.encode()) <= RESULT_CAP and "cut" not in got, (len(text.encode()), got.get("cut"))
    assert got["constraints"]["groups"] == c["groups"]


def test_constraint_help_is_in_copper_help_not_in_the_catalogue():
    """The plan syntax and the report of the constraints stage are documented where they cost nothing up front."""
    import copper_kicad.tools  # noqa: F401
    from copper_kicad.mcp import registry
    text = registry.HELP.get("pcb_route_guided", "")
    for word in ("groups", "tol", "diff_pairs", "skew", "uncoupled", "widths", "global: {constraints", "LENGTH_MISMATCH",
                 "PAIR_NOT_KICAD", "CONSTRAINT_GUESSED", "in-pad shortcut", "Not in this build"):
        assert word in text, word
    with Env(COPPER_CATALOGUE=None, COPPER_IMAGES=None, COPPER_COMPACT=None):
        tl = _catalogue()
    listed = json.dumps(tl, ensure_ascii=False)
    assert len(listed.encode()) <= CATALOGUE_MAX
    for word in ("LENGTH_MISMATCH", "CONSTRAINT_GUESSED", "uncoupled"):
        assert word not in listed, word


def _main() -> int:
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in tests:
        t0 = time.time()
        try:
            f()
            print(f"PASS {n} ({time.time() - t0:.1f}s)")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {n}\n{traceback.format_exc()}")
    for note in NOTES:
        print(f"NOTE {note}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
