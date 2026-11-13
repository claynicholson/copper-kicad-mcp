"""Build the routing-benchmark corpus under bench/route/corpus/.

    python bench/route/prepare.py [--only NAME ...] [--force]

For every real board: copy the human-routed original into a work dir, record the HUMAN reference
(total length, vias, per-net length / vias / widths / layers, KiCad DRC of the original), then strip all
tracks + vias (zones and footprints stay, zones refilled) and save the unrouted board + its project
rules (.kicad_pro, .kicad_dru) as corpus/<name>/. The originals are only ever read.

Two synthetic stress boards are generated from scratch (no reference routing), and four constraint boards
(suite "cons": length-matched groups, differential pairs, a keep-short crystal) with their KiCad net classes
(.kicad_pro) and custom rules (.kicad_dru); meta.json of those carries the expected constraint set.
Run with KiCad's Python or any Python 3.10+ (the pcbnew work happens in a KiCad-Python subprocess).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

DEMOS = Path("C:/Program Files/KiCad/10.0/share/kicad/demos")
EXTERNAL = Path(os.environ.get("COPPER_BENCH_EXTERNAL", "phone-breathalyzer/phone-breathalyzer.kicad_pcb"))

# (name, source .kicad_pcb, difficulty 1..5, reference kind, note) — in increasing difficulty
REAL = [
    ("ecc83", DEMOS / "ecc83/ecc83-pp.kicad_pcb", 1, "human", "2-layer THT tube amp, 9 nets, GND pour"),
    ("sonde_xilinx", DEMOS / "sonde xilinx/sonde xilinx.kicad_pcb", 1, "human", "2-layer, 26 nets, mixed THT/SMD"),
    ("complex_hierarchy", DEMOS / "complex_hierarchy/complex_hierarchy.kicad_pcb", 2, "human",
     "2-layer THT, 50 nets, human used 0 vias"),
    ("pic_programmer", DEMOS / "pic_programmer/pic_programmer.kicad_pcb", 2, "human", "2-layer THT, 34 nets, large board"),
    ("phone_breathalyzer", EXTERNAL, 2, "existing",
     "2-layer SMD 22x48 mm, 0.5 mm pitch; reference = the routing currently on the user's board (not hand-made)"),
    ("multichannel_mixer", DEMOS / "multichannel/multichannel_mixer.kicad_pcb", 3, "human",
     "2-layer mixed SMD/THT, 79 nets, two pours"),
    ("interf_u", DEMOS / "interf_u/interf_u.kicad_pcb", 3, "human", "2-layer THT bus board, 110 nets, 84 vias by hand"),
    ("stickhub", DEMOS / "stickhub/StickHub.kicad_pcb", 4, "human",
     "2-layer dense SMD USB hub 16.6x40 mm, 0.5 mm pitch, 5 pours, arcs"),
    ("one_air_max", DEMOS / "openair-max/One-Air-Max.kicad_pcb", 4, "human", "4-layer, 120 nets, 0.39 mm pitch, 34 pours"),
    ("tinytapeout", DEMOS / "tiny_tapeout/tinytapeout-demo.kicad_pcb", 4, "human", "4-layer, 108 nets, 757 pads"),
    ("coldfire", DEMOS / "kit-dev-coldfire-xilinx_5213/kit-dev-coldfire-xilinx_5213.kicad_pcb", 5, "human",
     "4-layer, 209 nets, 825 pads, QFP buses"),
    ("cm5_minima", DEMOS / "cm5_minima/CM5_MINIMA_3.kicad_pcb", 5, "human",
     "6-layer, 0.4 mm pitch, diff pairs, 13 pours (stretch case)"),
]


def _summ(ref: dict) -> dict:
    return {k: ref[k] for k in ("tracks", "vias", "arcs", "blind_or_micro_vias", "length_mm", "unconnected")}


def prepare_real(name, src: Path, difficulty, kind, note, force=False):
    dst = common.CORPUS / name
    if (dst / "meta.json").exists() and not force:
        print(f"{name}: exists (use --force to rebuild)")
        return
    if not src.exists():
        print(f"{name}: source not found: {src} — skipped")
        return
    work = common.WORK / "prepare" / name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    # work on copies only: the originals are never opened for writing
    orig = work / f"{name}.kicad_pcb"
    shutil.copy2(src, orig)
    for ext in (".kicad_pro", ".kicad_dru"):
        s = src.with_suffix(ext)
        if s.exists():
            shutil.copy2(s, work / f"{name}{ext}")
            shutil.copy2(s, dst / f"{name}{ext}")
    if not (dst / f"{name}.kicad_pro").exists():
        (dst / f"{name}.kicad_pro").write_text("{}", encoding="utf-8")
    print(f"{name}: measuring the reference + stripping copper …", flush=True)
    r = common.pcb_op({"op": "prepare", "src": str(orig), "dst": str(dst / f"{name}.kicad_pcb")})
    print(f"{name}: KiCad DRC of the original and of the unrouted copy …", flush=True)
    d_ref = common.drc(orig)
    d_un = common.drc(dst / f"{name}.kicad_pcb")
    ref = r["reference"]
    (dst / "reference.json").write_text(json.dumps(
        {"segments": ref.pop("segments"), "via_list": ref.pop("via_list")}), encoding="utf-8")
    meta = {
        "name": name, "kind": "real", "difficulty": difficulty, "note": note,
        "source": str(src).replace("\\", "/"), "reference_kind": kind,
        "facts": r["facts"],
        "reference": {**_summ(ref), "nets": ref["nets"], "unconnected_by_net": ref["unconnected_by_net"],
                      "drc": {k: d_ref[k] for k in ("errors", "warnings", "unconnected", "by_type")},
                      "drc_error_list": d_ref["error_list"]},
        "unrouted": {**r["unrouted"],
                     "drc": {k: d_un[k] for k in ("errors", "warnings", "unconnected", "by_type")},
                     "drc_error_list": d_un["error_list"]},
        "stripped": {"tracks_and_vias": r["removed"], "locked": r["removed_locked"], "teardrops": r["teardrops_removed"]},
    }
    (dst / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    print(f"{name}: {len(r['facts']['layers'])} layers, {r['unrouted']['connections']} connections "
          f"(kicad-cli: {d_un['unconnected']}), reference {ref['length_mm']} mm / {ref['vias']} vias, "
          f"reference DRC {d_ref['errors']} errors / {d_ref['unconnected']} unconnected, "
          f"unrouted DRC {d_un['errors']} errors", flush=True)


# ── synthetic stress boards ───────────────────────────────────────────────
def synth_bus_cross() -> dict:
    """Two 24-pin SMD connectors facing each other with the pin order REVERSED: every net crosses every
    other one, on SMD pads (so each needs a layer change), through a 28 mm channel."""
    n, pitch = 24, 1.0
    y0 = 6.0
    j1 = {"ref": "J1", "x": 4.0, "y": 0.0, "pads": []}
    j2 = {"ref": "J2", "x": 36.0, "y": 0.0, "pads": []}
    for i in range(n):
        j1["pads"].append({"n": i + 1, "x": 0.0, "y": y0 + i * pitch, "w": 1.6, "h": 0.6, "net": f"/B{i:02d}"})
        j2["pads"].append({"n": i + 1, "x": 0.0, "y": y0 + i * pitch, "w": 1.6, "h": 0.6, "net": f"/B{n - 1 - i:02d}"})
    return {"size": [40.0, 35.0], "layers": 2, "parts": [j1, j2]}


def synth_qfp_fanout() -> dict:
    """QFP-64 (0.5 mm pitch) in the middle, four 1x16 2.54 mm headers round the edge. Top and left sides
    go straight to their header, bottom and right sides go to their header in REVERSED order (crossings).
    One pin per side is GND with a GND pour on B.Cu (SMD pad -> via -> pour)."""
    cx = cy = 25.0
    n = 16
    u = {"ref": "U1", "x": cx, "y": cy, "pads": []}
    hdr = {s: {"ref": f"J{k + 1}", "x": 0.0, "y": 0.0, "pads": []} for k, s in enumerate("TRBL")}
    num = 0
    for side in "TRBL":
        for k in range(n):
            num += 1
            off = (k - (n - 1) / 2) * 0.5
            if side == "T":
                px, py, w, h = off, -5.7, 0.3, 1.5
            elif side == "B":
                px, py, w, h = off, 5.7, 0.3, 1.5
            elif side == "L":
                px, py, w, h = -5.7, off, 1.5, 0.3
            else:
                px, py, w, h = 5.7, off, 1.5, 0.3
            net = "GND" if k == 7 else f"/{side}{k:02d}"
            u["pads"].append({"n": num, "x": px, "y": py, "w": w, "h": h, "net": net})
            hk = (n - 1 - k) if side in "BR" else k            # reversed order on two sides
            hoff = (hk - (n - 1) / 2) * 2.54
            if side == "T":
                hx, hy = cx + hoff, 3.0
            elif side == "B":
                hx, hy = cx + hoff, 47.0
            elif side == "L":
                hx, hy = 3.0, cy + hoff
            else:
                hx, hy = 47.0, cy + hoff
            hdr[side]["pads"].append({"n": hk + 1, "x": hx, "y": hy, "w": 1.7, "h": 1.7, "net": net, "tht": 1.0})
    return {"size": [50.0, 50.0], "layers": 2, "parts": [u] + list(hdr.values()),
            "zones": [{"net": "GND", "layer": "B.Cu", "rect": [0.5, 0.5, 49.5, 49.5]}]}


SYNTH = [
    ("synth_bus_cross", synth_bus_cross, 3, "synthetic: 24-net reversed SMD bus (every net crosses every other), 2 layers"),
    ("synth_qfp_fanout", synth_qfp_fanout, 4, "synthetic: QFP-64 0.5 mm fan-out to 4 headers, 2 sides reversed, GND pour on B.Cu"),
]


def prepare_synth(name, fn, difficulty, note, force=False):
    dst = common.CORPUS / name
    if (dst / "meta.json").exists() and not force:
        print(f"{name}: exists (use --force to rebuild)")
        return
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    spec = fn()
    r = common.pcb_op({"op": "synth", "dst": str(dst / f"{name}.kicad_pcb"), **spec})
    (dst / f"{name}.kicad_pro").write_text("{}", encoding="utf-8")
    d_un = common.drc(dst / f"{name}.kicad_pcb")
    meta = {"name": name, "kind": "synthetic", "difficulty": difficulty, "note": note, "source": "prepare.py",
            "reference_kind": None, "facts": r["facts"], "reference": None,
            "unrouted": {**r["unrouted"], "drc": {k: d_un[k] for k in ("errors", "warnings", "unconnected", "by_type")},
                         "drc_error_list": d_un["error_list"]}}
    (dst / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"{name}: {r['unrouted']['connections']} connections (kicad-cli: {d_un['unconnected']}), "
          f"unrouted DRC {d_un['errors']} errors {d_un['by_type']}", flush=True)


# ── constraint boards (suite "cons", design §14.11): synthetic boards with KiCad rules a router must work to ──
def _class(name, width=0.2, clearance=0.2, dp_width=0.2, dp_gap=0.2):
    return {"bus_width": 12, "clearance": clearance, "diff_pair_gap": dp_gap, "diff_pair_via_gap": 0.25,
            "diff_pair_width": dp_width, "line_style": 0, "microvia_diameter": 0.3, "microvia_drill": 0.1, "name": name,
            "pcb_color": "rgba(0, 0, 0, 0.000)", "priority": 2147483647 if name == "Default" else 0,
            "schematic_color": "rgba(0, 0, 0, 0.000)", "track_width": width, "tuning_profile": "", "via_diameter": 0.6,
            "via_drill": 0.3, "wire_width": 6}


def _pro(classes=(), patterns=()):
    """A .kicad_pro in the shape KiCad 10 writes (it ignores net settings without their schema version): the
    Default class, extra classes, and which nets are in them. Length / skew / pair rules keep KiCad's default
    severity (error); via height counts in a net's length (KiCad's default). ``meta.filename`` is filled in by
    prepare_cons."""
    extra = [dict(c, priority=i) for i, c in enumerate(classes)]
    return {"board": {"design_settings": {"rules": {"use_height_for_length_calcs": True}}},
            "meta": {"filename": "", "version": 3},
            "net_settings": {"classes": [_class("Default")] + extra, "meta": {"version": 5}, "net_colors": None,
                             "netclass_assignments": None,
                             "netclass_patterns": [{"netclass": c, "pattern": p} for c, p in patterns]}}


USB_RULES = '''(rule "usb_skew" (condition "A.inDiffPair('/USB_D_')") (constraint skew (max 0.15mm)))
(rule "usb_gap" (condition "A.inDiffPair('/USB_D_')") (constraint diff_pair_gap (min 0.15mm) (max 0.9mm)) (constraint diff_pair_uncoupled (max 6mm)))
'''


def cons_usb_pair() -> dict:
    """A USB pair from a connector on the west edge to an MCU row on the south side, turned 90 degrees to it (the
    inner lane of the turn is shorter: skew), neighbouring pads at both ends, four ordinary nets round it (one of
    them 0.5 mm wide). The pair's class gap (0.15) is below the clearance (0.2): only a pair-aware router gets it."""
    j1 = {"ref": "J1", "x": 3.0, "y": 0.0, "pads": []}
    for i, net in enumerate(["/A0", "/USB_D_P", "/USB_D_N", "/A1", "/A2", "/DRV"]):
        j1["pads"].append({"n": i + 1, "x": 0.0, "y": 8.0 + i * 1.0, "w": 1.6, "h": 0.6, "net": net})
    u1 = {"ref": "U1", "x": 0.0, "y": 20.0, "pads": []}
    for i, net in enumerate(["/A2", "/A1", "/USB_D_N", "/USB_D_P", "/A0", "/DRV", "", ""]):
        u1["pads"].append({"n": i + 1, "x": 18.0 + i * 0.65, "y": 0.0, "w": 0.35, "h": 1.4, "net": net})
    return {"size": [30.0, 24.0], "layers": 2, "parts": [j1, u1],
            "pro": _pro([_class("USB", dp_width=0.2, dp_gap=0.15), _class("WIDE", width=0.5)],
                        [("USB", "/USB_D_*"), ("WIDE", "/DRV")]),
            "dru": "(version 1)\n" + USB_RULES,
            "constraints": {"groups": [], "short": [],
                            "pairs": [{"p": "/USB_D_P", "n": "/USB_D_N", "skew": 0.15, "gap": 0.15, "width": 0.2,
                                       "coupled_pct": 70}]}}


def cons_bus_match() -> dict:
    """An SD bus (4 data lines + clock) from a column on the west (2.0 mm pitch) to a row on the south (0.8 mm
    pitch): each line further out in the turn is naturally about 2 mm longer, 8 mm from the shortest to the longest. One unconstrained net
    beside it, open board to the north and east for whatever a router adds."""
    nets = ["/SD_D0", "/SD_D1", "/SD_D2", "/SD_D3", "/SD_CLK", "/IRQ"]
    j1 = {"ref": "J1", "x": 4.0, "y": 0.0, "pads": []}
    u1 = {"ref": "U1", "x": 0.0, "y": 24.0, "pads": []}
    for i, net in enumerate(nets):
        j1["pads"].append({"n": i + 1, "x": 0.0, "y": 7.0 + i * 2.0, "w": 1.6, "h": 0.7, "net": net})
        u1["pads"].append({"n": i + 1, "x": 22.0 + (len(nets) - 1 - i) * 0.8, "y": 0.0, "w": 0.4, "h": 1.4, "net": net})
    return {"size": [40.0, 30.0], "layers": 2, "parts": [j1, u1], "pro": _pro(),
            "dru": '(version 1)\n(rule "sd_match" (condition "A.NetName == \'/SD_D?\' || A.NetName == \'/SD_CLK\'") '
                   '(constraint skew (max 0.5mm)))\n',
            "min_natural_spread": 6.0,
            "constraints": {"groups": [{"nets": ["/SD_CLK", "/SD_D0", "/SD_D1", "/SD_D2", "/SD_D3"], "tol": 0.5}],
                            "pairs": [], "short": []}}


def cons_pairs_cross() -> dict:
    """Two pairs on SMD pads that must cross: A runs west to east, B north to south. One of them has to change
    layer through a pair of vias twice, and stay a pair while doing it."""
    parts = []
    # the pads sit 0.6 mm from the board edge: no track fits behind a connector, the pairs really have to cross
    for ref, x, y, vert, nets in (("J1", 1.4, 15.0, True, ("/A_P", "/A_N")), ("J2", 28.6, 15.0, True, ("/A_P", "/A_N")),
                                  ("J3", 15.0, 1.4, False, ("/B_P", "/B_N")), ("J4", 15.0, 28.6, False, ("/B_P", "/B_N"))):
        pads = []
        for i, net in enumerate(nets):
            off = (i - 0.5) * 1.0
            pads.append({"n": i + 1, "x": 0.0 if vert else off, "y": off if vert else 0.0,
                         "w": 1.6 if vert else 0.6, "h": 0.6 if vert else 1.6, "net": net})
        parts.append({"ref": ref, "x": x, "y": y, "pads": pads})
    dru = "(version 1)\n"
    for k in "ab":
        dru += (f'(rule "{k}_skew" (condition "A.inDiffPair(\'/{k.upper()}_\')") (constraint skew (max 0.2mm)))\n'
                f'(rule "{k}_gap" (condition "A.inDiffPair(\'/{k.upper()}_\')") (constraint diff_pair_gap (min 0.15mm) '
                f'(max 1.2mm)) (constraint diff_pair_uncoupled (max 8mm)))\n')
    return {"size": [30.0, 30.0], "layers": 2, "parts": parts,
            "pro": _pro([_class("DP", dp_width=0.2, dp_gap=0.15)], [("DP", "/A_*"), ("DP", "/B_*")]), "dru": dru,
            "constraints": {"groups": [], "short": [],
                            "pairs": [{"p": "/A_P", "n": "/A_N", "skew": 0.2, "gap": 0.15, "width": 0.2, "coupled_pct": 60},
                                      {"p": "/B_P", "n": "/B_N", "skew": 0.2, "gap": 0.15, "width": 0.2, "coupled_pct": 60}]}}


def cons_mixed() -> dict:
    """4 layers, GND plane on In1.Cu. A QFP-32 MCU with: a QSPI flash (IO0..3 + CLK matched to 0.3 mm), a USB pair to
    a connector, a crystal (each leg at most 8 mm), 12 ordinary nets to a 2.54 mm header in reversed order, 3V3.
    Constrained and ordinary nets compete for the same room; vias count in the lengths."""
    cx, cy, off = 25.0, 20.0, 4.5
    u1 = {"ref": "U1", "x": cx, "y": cy, "pads": []}
    side_nets = {
        "W": ["/Q_IO0", "/Q_CLK", "/Q_IO3", "+3V3", "/Q_CS", "/Q_IO1", "/Q_IO2", "GND"],
        "N": ["/USB_D_P", "/USB_D_N", "GND", "/XI", "/XO", "/P0", "/P1", "/P2"],
        "E": ["/P3", "/P4", "/P5", "/P6", "/P7", "/P8", "/P9", "/P10"],
        "S": ["/P11", "+3V3", "GND", "", "", "", "", ""],
    }
    num = 0
    for side in "WNES":
        for k, net in enumerate(side_nets[side]):
            num += 1
            t = (k - 3.5) * 0.8
            if side == "W":
                px, py, w, h = -off, t, 1.5, 0.45
            elif side == "E":
                px, py, w, h = off, t, 1.5, 0.45
            elif side == "N":
                px, py, w, h = t, -off, 0.45, 1.5
            else:
                px, py, w, h = t, off, 0.45, 1.5
            u1["pads"].append({"n": num, "x": px, "y": py, "w": w, "h": h, "net": net})
    u2 = {"ref": "U2", "x": 9.0, "y": 20.0, "pads": []}                      # SO-8 flash, 1.27 mm pitch
    for k, net in enumerate(["/Q_CS", "/Q_IO1", "/Q_IO2", "GND"]):
        u2["pads"].append({"n": k + 1, "x": -2.7, "y": (k - 1.5) * 1.27, "w": 1.5, "h": 0.6, "net": net})
    for k, net in enumerate(["/Q_IO0", "/Q_CLK", "/Q_IO3", "+3V3"]):
        u2["pads"].append({"n": 8 - k, "x": 2.7, "y": (k - 1.5) * 1.27, "w": 1.5, "h": 0.6, "net": net})
    j1 = {"ref": "J1", "x": 21.0, "y": 4.0, "pads": []}                      # USB connector, north edge
    for k, net in enumerate(["+5V", "/USB_D_P", "/USB_D_N", "GND"]):
        j1["pads"].append({"n": k + 1, "x": (k - 1.5) * 1.0, "y": 0.0, "w": 0.6, "h": 1.6, "net": net})
    y1 = {"ref": "Y1", "x": 26.0, "y": 10.5, "pads": [
        {"n": 1, "x": -1.1, "y": 0.0, "w": 1.0, "h": 1.4, "net": "/XI"},
        {"n": 2, "x": 1.1, "y": 0.0, "w": 1.0, "h": 1.4, "net": "/XO"}]}
    j2 = {"ref": "J2", "x": 45.0, "y": 0.0, "pads": []}                      # header, east edge, reversed order
    for k in range(12):
        j2["pads"].append({"n": k + 1, "x": 0.0, "y": 6.0 + k * 2.54, "w": 1.7, "h": 1.7, "net": f"/P{11 - k}", "tht": 1.0})
    c1 = {"ref": "C1", "x": 17.0, "y": 27.5, "pads": [
        {"n": 1, "x": -0.8, "y": 0.0, "w": 0.9, "h": 1.0, "net": "+3V3"},
        {"n": 2, "x": 0.8, "y": 0.0, "w": 0.9, "h": 1.0, "net": "GND"}]}
    dru = ('(version 1)\n(rule "qspi_match" (condition "A.NetName == \'/Q_IO?\' || A.NetName == \'/Q_CLK\'") '
           '(constraint skew (max 0.3mm)))\n' + USB_RULES +
           '(rule "xtal_short" (condition "A.NetName == \'/XI\' || A.NetName == \'/XO\'") (constraint length (max 8mm)))\n')
    return {"size": [50.0, 40.0], "layers": 4, "parts": [u1, u2, j1, y1, j2, c1],
            "zones": [{"net": "GND", "layer": "In1.Cu", "rect": [0.5, 0.5, 49.5, 39.5]}],
            "pro": _pro([_class("USB", dp_width=0.2, dp_gap=0.15)], [("USB", "/USB_D_*")]), "dru": dru,
            "constraints": {"groups": [{"nets": ["/Q_CLK", "/Q_IO0", "/Q_IO1", "/Q_IO2", "/Q_IO3"], "tol": 0.3}],
                            "pairs": [{"p": "/USB_D_P", "n": "/USB_D_N", "skew": 0.15, "gap": 0.15, "width": 0.2,
                                       "coupled_pct": 50}],
                            "short": [{"net": "/XI", "max": 8.0}, {"net": "/XO", "max": 8.0}]}}


CONS = [
    ("cons_usb_pair", cons_usb_pair, 2, "constraints: USB pair connector -> MCU with a 90-degree turn (skew 0.15, gap 0.15, uncoupled 6), 2 layers"),
    ("cons_bus_match", cons_bus_match, 2, "constraints: SD bus, 5 nets matched to 0.5 mm (natural spread about 8 mm), 2 layers"),
    ("cons_pairs_cross", cons_pairs_cross, 3, "constraints: two pairs that must cross on SMD pads (skew 0.2, uncoupled 8), 2 layers"),
    ("cons_mixed", cons_mixed, 3, "constraints: QFP-32 with a QSPI group (0.3 mm), a USB pair, a crystal (max 8 mm), 12 plain nets, 4 layers"),
]
CONS_TYPES = ("length_out_of_range", "skew_out_of_range", "diff_pair_gap_out_of_range",
              "diff_pair_uncoupled_length_too_long", "too_many_vias")


def prepare_cons(name, fn, difficulty, note, force=False):
    """Like prepare_synth, plus the project file (net classes) and the custom rules next to the board, and the
    expected constraint set in meta.json. The unrouted board must carry no constraint error: the rules are skew /
    pair / length-max rules, which KiCad leaves silent on a net without tracks."""
    dst = common.CORPUS / name
    if (dst / "meta.json").exists() and not force:
        print(f"{name}: exists (use --force to rebuild)")
        return
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    spec = fn()
    pro, dru, expect = spec.pop("pro"), spec.pop("dru"), spec.pop("constraints")
    want_spread = spec.pop("min_natural_spread", None)
    r = common.pcb_op({"op": "synth", "dst": str(dst / f"{name}.kicad_pcb"), **spec})
    # pcbnew's SaveBoard writes a project file of its own next to the board: the net classes go INTO that file
    # (written first, ours would be overwritten), the custom rules beside it
    pro_path = dst / f"{name}.kicad_pro"
    try:
        base = json.loads(pro_path.read_text(encoding="utf-8")) if pro_path.exists() else {}
    except ValueError:
        base = {}
    base["net_settings"] = pro["net_settings"]
    base.setdefault("meta", pro["meta"])
    base["meta"]["filename"] = f"{name}.kicad_pro"
    rules = base.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})
    rules.update(pro["board"]["design_settings"]["rules"])
    pro_path.write_text(json.dumps(base, indent=2), encoding="utf-8")
    (dst / f"{name}.kicad_dru").write_text(dru, encoding="utf-8")
    d_un = common.drc(dst / f"{name}.kicad_pcb")
    bad = {k: v for k, v in d_un["by_type"].items() if k.split(":", 1)[-1] in CONS_TYPES}
    if bad:
        raise SystemExit(f"{name}: the unrouted board already has constraint errors {bad}: the rule file is wrong")
    mst = r["facts"]["mst_mm"]
    for g in expect["groups"]:
        nat = [mst[n] for n in g["nets"]]
        g["natural_spread"] = round(max(nat) - min(nat), 3)
        if want_spread is not None and g["natural_spread"] < want_spread:
            raise SystemExit(f"{name}: the natural lengths of {g['nets']} differ by {g['natural_spread']} mm, "
                             f"less than the {want_spread} mm the board is meant to have")
    meta = {"name": name, "kind": "synthetic", "suite": "cons", "difficulty": difficulty, "note": note,
            "source": "prepare.py", "reference_kind": None, "facts": r["facts"], "reference": None,
            "constraints": expect,
            "unrouted": {**r["unrouted"], "drc": {k: d_un[k] for k in ("errors", "warnings", "unconnected", "by_type")},
                         "drc_error_list": d_un["error_list"]}}
    (dst / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"{name}: {r['unrouted']['connections']} connections (kicad-cli: {d_un['unconnected']}), "
          f"unrouted DRC {d_un['errors']} errors {d_un['by_type']}, constraint errors: none", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="board names to (re)build")
    ap.add_argument("--force", action="store_true", help="rebuild even when corpus/<name>/meta.json exists")
    a = ap.parse_args()
    common.sandbox()
    common.CORPUS.mkdir(parents=True, exist_ok=True)
    for name, src, diff, kind, note in REAL:
        if not a.only or name in a.only:
            prepare_real(name, src, diff, kind, note, a.force)
    for name, fn, diff, note in SYNTH:
        if not a.only or name in a.only:
            prepare_synth(name, fn, diff, note, a.force)
    for name, fn, diff, note in CONS:
        if not a.only or name in a.only:
            prepare_cons(name, fn, diff, note, a.force)


if __name__ == "__main__":
    main()
