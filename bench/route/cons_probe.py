"""What KiCad's own DRC says about length, skew and differential-pair rules (design §14.9, "measured").

    "C:/Program Files/KiCad/10.0/bin/python.exe" bench/route/cons_probe.py [case ...]
    cases: hops pair pair45 via unrouted padentry fanin midpad viastub (default: all)

Builds tiny boards with hand-placed copper of known length under bench/route/.work/cons_probe/, writes a
.kicad_pro + .kicad_dru next to each, runs kicad-cli DRC and prints every error. Nothing here imports the
router: these are the facts the electrical-constraints stage is designed against, and the script is how to
re-measure them on another KiCad version. Run it with KiCad's Python (needs pcbnew).

Measured with KiCad 10.0 (2026-11-11), see the design section for the list:
  hops      4 layers, no stackup in the file: a through via counts F.Cu-In1.Cu 0.5325, F.Cu-In2.Cu 1.0475,
            F.Cu-B.Cu 1.58 mm (only the layers the net's tracks use); pad-to-die length is added; a net's
            length is the sum of ALL its track copper (a tee stub counts); an unrouted net reports actual 0
  pair      lanes at the gap, begun clear of the pads, are coupled; the straight parallel exit from the pads at pad
            pitch IS a `diff_pair_gap_out_of_range` (max) error; the chamfered U bump adds 2A - 4c(2 - sqrt 2) mm
            exactly; skew is measured against the LONGEST net the rule matches
  pair45    the same pair leaving the pad centres on a diagonal, lanes beginning ON the pad edges: uncoupled over
            its whole length (see fanin)
  via       a pair's layer change: the parallel jog at via pitch is a max-gap error and counts as uncoupled
  unrouted  skew, pair and `length (max ...)` rules give no error on a board without tracks; a
            `length (min ...)` rule does (actual 0)
  fanin     which fan-in KiCad accepts as coupled. A lane SEGMENT counts as coupled only when the two "rungs" joining
            its ends to the partner's are clear of other copper, the pair's own pads included: lanes that start on
            the pad edges are uncoupled over their whole length (c: 30.2 mm) although the gap is right; a straight
            parallel exit to past the pads is a max-gap error (d); a diagonal straight out of the pad centres with
            each lane BROKEN where the rung is clear of the pads gives no error at all (e)
  padentry  a chain that ENDS ON A PAD'S POSITION is counted as the straight line from the pad centre to the point
            where it leaves the pad (a dogleg inside the pad is cut short: -0.05 mm per end on a 1.2 x 0.6 pad);
            a track that ends elsewhere inside a pad, on its edge, or runs through the centre is counted in full,
            and nothing is added to reach the centre. A lane lying exactly ON a pad edge is a numerical toss-up
            (/f 30.1617 against its mirror image /g 30.2485)
  midpad    (added in step A) two chains that END ON THE SAME PAD CENTRE, each with a dogleg inside the pad, are one
            line through the pad: counted in full (30.3234), no shortcut; a via in the middle of a straight track
            with a branch on another layer counts both layers (33.0 + 1.58); F.Cu - In1.Cu - In2.Cu - F.Cu adds
            0.5325 + 0.515 + 1.0475 (inner to inner = half + dielectric + half)
  viastub   (added in step A) a VIA gets the same shortcut as a pad: a track that turns s mm before its via, inside
            the via's copper circle (radius r), is counted as the straight line from where it enters the circle to
            the via centre, i.e. s + sqrt(r^2 - s^2) - r shorter; a stub of r or more is counted in full
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE / ".work" / "cons_probe"
KICAD_BIN = Path(os.environ.get("COPPER_BENCH_KICAD_BIN", "C:/Program Files/KiCad/10.0/bin"))
CLI = KICAD_BIN / ("kicad-cli.exe" if sys.platform == "win32" else "kicad-cli")
OX, OY = 100.0, 100.0
PRO = {"board": {"design_settings": {"rules": {"use_height_for_length_calcs": True}}},
       "net_settings": {"classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6,
                                     "via_drill": 0.3, "diff_pair_width": 0.2, "diff_pair_gap": 0.2,
                                     "diff_pair_via_gap": 0.25}], "netclass_patterns": []}}


def mm(v):
    return int(round(v * 1e6))


class Probe:
    """One scratch board: two (or three) parts with one SMD pad per net at the given y, tracks added by hand."""

    def __init__(self, name: str, nets: dict, layers: int = 2, size=(40.0, 30.0), tee: str | None = None):
        import pcbnew as P  # noqa: N812
        self.P = P
        self.name, self.dir = name, WORK / name
        self.dir.mkdir(parents=True, exist_ok=True)
        b = self.b = P.BOARD()
        b.SetCopperLayerCount(layers)
        self.nets = {}
        w, h = size
        for x1, y1, x2, y2 in ((0, 0, w, 0), (w, 0, w, h), (w, h, 0, h), (0, h, 0, 0)):
            s = P.PCB_SHAPE(b)
            s.SetShape(P.SHAPE_T_SEGMENT)
            s.SetStart(P.VECTOR2I(mm(OX + x1), mm(OY + y1)))
            s.SetEnd(P.VECTOR2I(mm(OX + x2), mm(OY + y2)))
            s.SetLayer(P.Edge_Cuts)
            s.SetWidth(mm(0.1))
            b.Add(s)
        self.pads = {}
        for ref, x in (("J1", 5.0), ("U1", 35.0), ("R1", 20.0)):
            if ref == "R1" and not tee:
                continue
            fp = P.FOOTPRINT(b)
            fp.SetReference(ref)
            fp.SetValue(ref)
            fp.SetFPID(P.LIB_ID("bench", "FP_" + ref))
            fp.SetPosition(P.VECTOR2I(mm(OX + x), mm(OY)))
            b.Add(fp)
            for i, (n, y) in enumerate(nets.items()):
                if ref == "R1" and n != tee:
                    continue
                p = P.PAD(fp)
                p.SetNumber(str(i + 1))
                p.SetPosition(P.VECTOR2I(mm(OX + x), mm(OY + y + (2.0 if ref == "R1" else 0.0))))
                p.SetSize(P.F_Cu, P.VECTOR2I(mm(1.2), mm(0.6)))
                p.SetAttribute(P.PAD_ATTRIB_SMD)
                p.SetShape(P.F_Cu, P.PAD_SHAPE_RECT)
                p.SetLayerSet(P.PAD.SMDMask())
                p.SetNet(self.net(n))
                fp.Add(p)
                self.pads[(ref, n)] = p

    def net(self, n):
        if n not in self.nets:
            ni = self.P.NETINFO_ITEM(self.b, n)
            self.b.Add(ni)
            self.nets[n] = ni
        return self.nets[n]

    def track(self, n, pts, layer="F.Cu", w=0.2):
        P = self.P
        for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
            t = P.PCB_TRACK(self.b)
            t.SetStart(P.VECTOR2I(mm(OX + xa), mm(OY + ya)))
            t.SetEnd(P.VECTOR2I(mm(OX + xb), mm(OY + yb)))
            t.SetWidth(mm(w))
            t.SetLayer(self.b.GetLayerID(layer))
            t.SetNet(self.net(n))
            self.b.Add(t)

    def via(self, n, x, y):
        P = self.P
        v = P.PCB_VIA(self.b)
        v.SetPosition(P.VECTOR2I(mm(OX + x), mm(OY + y)))
        v.SetWidth(P.F_Cu, mm(0.6))
        v.SetDrill(mm(0.3))
        v.SetLayerPair(P.F_Cu, P.B_Cu)
        v.SetNet(self.net(n))
        self.b.Add(v)

    def drc(self, rules: str) -> list[dict]:
        pcb = self.dir / f"{self.name}.kicad_pcb"
        self.P.SaveBoard(str(pcb), self.b)
        (self.dir / f"{self.name}.kicad_pro").write_text(json.dumps(PRO), encoding="utf-8")
        (self.dir / f"{self.name}.kicad_dru").write_text("(version 1)\n" + rules, encoding="utf-8")
        out = self.dir / "drc.json"
        subprocess.run([str(CLI), "pcb", "drc", "--format", "json", "--units", "mm", "--severity-all", "--refill-zones",
                        "-o", str(out), str(pcb)], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)
        d = json.loads(out.read_text(encoding="utf-8"))
        errs = [v for v in d.get("violations", []) if v.get("severity") == "error"]
        print(f"== {self.name}: {len(errs)} error(s), {len(d.get('unconnected_items', []))} unconnected")
        for v in errs:
            print("  ", v["type"], "|", v["description"], "|", [i.get("description") for i in v.get("items", [])][:2])
        return errs


PROBE_ALL = '(rule "probe" (condition "A.NetName == \'/*\'") (constraint length (min 2000mm)))\n'


def case_hops():
    """Via length per layer hop, pad-to-die, a tee net, an unrouted net: one always-failing length rule prints
    KiCad's own length of every net ('actual')."""
    nets = {"/A": 4.0, "/B": 8.0, "/C": 12.0, "/E": 16.0, "/T": 20.0, "/U": 24.0}
    p = Probe("hops", nets, layers=4, tee="/T")
    for n, y, lay in (("/A", 4.0, "In1.Cu"), ("/B", 8.0, "In2.Cu"), ("/C", 12.0, "B.Cu")):
        p.track(n, [(5, y), (15, y)])
        p.via(n, 15, y)
        p.track(n, [(15, y), (25, y)], lay)
        p.via(n, 25, y)
        p.track(n, [(25, y), (35, y)])
    p.track("/E", [(5, 16), (35, 16)])
    p.pads[("U1", "/E")].SetPadToDieLength(mm(2.5))
    p.track("/T", [(5, 20), (20, 20), (35, 20)])
    p.track("/T", [(20, 20), (20, 22)])
    p.drc(PROBE_ALL)
    print("   expect: /A 31.0650  /B 32.0950  /C 33.1600  /E 32.5000  /T 32.0000  /U 0.0000")


PAIR_RULES = '''(rule "usb_skew" (condition "A.inDiffPair('/USB_D_')") (constraint skew (max 0.1mm)))
(rule "usb_gap" (condition "A.inDiffPair('/USB_D_')") (constraint diff_pair_gap (min 0.15mm) (max 0.25mm)) (constraint diff_pair_uncoupled (max 3mm)))
(rule "sd_match" (condition "A.NetName == '/SD_*'") (constraint skew (max 0.3mm)))
'''
PAIR_NETS = {"/USB_D_P": 4.0, "/USB_D_N": 5.0, "/SD_CLK": 12.0, "/SD_D0": 16.0}


def _singles(p: Probe):
    """/SD_CLK straight (30 mm); /SD_D0 with two chamfered U bumps: amplitude A, chamfer c, top F, spacing S."""
    p.track("/SD_CLK", [(5, 12), (35, 12)])
    A, c, F, S = 1.0, 0.2, 0.6, 0.6
    pts, s = [(5, 16)], 10.0
    for _k in range(2):
        pts += [(s - c, 16), (s, 16 - c), (s, 16 - A + c), (s + c, 16 - A), (s + F - c, 16 - A), (s + F, 16 - A + c),
                (s + F, 16 - c), (s + F + c, 16)]
        s += F + S
    p.track("/SD_D0", pts + [(35, 16)])
    return 30 + 2 * (2 * A - 4 * c * (2 - math.sqrt(2)))


def case_pair(straight_exit: bool = True):
    """A coupled pair on one layer (pads 1.0 mm apart, lanes 0.4 mm apart = gap 0.2). ``straight_exit``: the
    tracks leave the pads in parallel for 1 mm before the 45-degree fan-in (what a max-gap rule flags)."""
    p = Probe("pair" if straight_exit else "pair45", PAIR_NETS)
    if straight_exit:
        p.track("/USB_D_P", [(5, 4), (6, 4), (6.3, 4.3), (33.7, 4.3), (34, 4), (35, 4)])
        p.track("/USB_D_N", [(5, 5), (6, 5), (6.3, 4.7), (33.7, 4.7), (34, 5), (35, 5)])
    else:
        p.track("/USB_D_P", [(5, 4), (5.3, 4.3), (34.7, 4.3), (35, 4)])
        p.track("/USB_D_N", [(5, 5), (5.3, 4.7), (34.7, 4.7), (35, 5)])
    want = _singles(p)
    p.drc(PAIR_RULES + PROBE_ALL)
    print(f"   expect: /SD_D0 {want:.4f} (bump formula), sd_match skew = /SD_CLK against the longest (/SD_D0)")


def case_via():
    """The same pair with a layer change in the middle: vias 0.9 mm apart, 45-degree jogs, 0.75 mm parallel at
    via pitch on both layers."""
    p = Probe("via", PAIR_NETS)
    for n, y0, yl, yv in (("/USB_D_P", 4.0, 4.3, 4.05), ("/USB_D_N", 5.0, 4.7, 4.95)):
        p.track(n, [(5, y0), (6, y0), (6.3, yl), (18, yl), (18.25, yv), (19, yv)])
        p.via(n, 19, yv)
        p.track(n, [(19, yv), (19.75, yv), (20, yl), (33.7, yl), (34, y0), (35, y0)], "B.Cu")
        p.via(n, 35, y0)
    _singles(p)
    p.drc(PAIR_RULES + PROBE_ALL)


def case_unrouted():
    """No tracks at all: which constraint rules already fail on an unrouted board (the benchmark's baseline)."""
    Probe("unrouted", PAIR_NETS).drc(PAIR_RULES + '(rule "minlen" (condition "A.NetName == \'/SD_D0\'") '
                                                  '(constraint length (min 10mm)))\n'
                                                  '(rule "maxlen" (condition "A.NetName == \'/SD_CLK\'") '
                                                  '(constraint length (max 8mm)))\n')
    print("   expect: 1 error (minlen, actual 0); the skew, pair and max-length rules are silent")


def case_fanin():
    """Three fan-in shapes for the same pair and the same rules (gap 0.15 .. 0.25 mm, uncoupled <= 3 mm)."""
    shapes = {
        "fanin_c": ([(5, 4), (5.2, 4), (5.5, 4.3), (34.5, 4.3), (34.8, 4), (35, 4)],
                    [(5, 5), (5.2, 5), (5.5, 4.7), (34.5, 4.7), (34.8, 5), (35, 5)]),
        "fanin_d": ([(5, 4), (5.7, 4), (6.0, 4.3), (34.0, 4.3), (34.3, 4), (35, 4)],
                    [(5, 5), (5.7, 5), (6.0, 4.7), (34.0, 4.7), (34.3, 5), (35, 5)]),
        "fanin_e": ([(5, 4), (5.3, 4.3), (6, 4.3), (34, 4.3), (34.7, 4.3), (35, 4)],
                    [(5, 5), (5.3, 4.7), (6, 4.7), (34, 4.7), (34.7, 4.7), (35, 5)]),
    }
    rules = "".join(line + "\n" for line in PAIR_RULES.splitlines() if "sd_match" not in line)
    for name, (pp, nn) in shapes.items():
        p = Probe(name, {"/USB_D_P": 4.0, "/USB_D_N": 5.0})
        p.track("/USB_D_P", pp)
        p.track("/USB_D_N", nn)
        p.drc(rules)
    print("   expect: c = 1 error (uncoupled 30.2485), d = 2 errors (max gap, actual 0.8), e = 0 errors")


def case_padentry():
    """How much of a track inside its own pad counts (pads 1.2 x 0.6 mm at x = 5 and x = 35)."""
    cases = {
        "/a": [(5, 3), (35, 3)],                                        # centre to centre
        "/b": [(4.5, 6), (35.5, 6)],                                    # runs 0.5 mm past both centres, inside the pads
        "/c": [(5, 9), (5.2, 9.2), (34.8, 9.2), (35, 9)],               # dogleg inside the pad
        "/d": [(5.6, 12), (34.4, 12)],                                  # ends on the pad edges
        "/e": [(5.3, 15), (34.7, 15)],                                  # ends inside the pads, 0.3 mm from the centres
        "/f": [(5, 18), (5.3, 18.3), (34.7, 18.3), (35, 18)],           # 45 degrees out, lane ON the pad's long edge
        "/g": [(5, 21), (5.3, 20.7), (34.7, 20.7), (35, 21)],           # ... and its mirror image
        "/h": [(5, 24), (5, 24.6), (35, 24.6), (35, 24)],               # straight out through the long side
        "/i": [(5, 27), (5.25, 27.25), (34.75, 27.25), (35, 27)],       # 45 degrees out, lane leaves through the end
    }
    p = Probe("padentry", {n: pts[0][1] for n, pts in cases.items()}, size=(40.0, 32.0))
    for n, pts in cases.items():
        p.track(n, pts)
    p.drc(PROBE_ALL)
    print("   plain sums:", {n: round(sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])), 4)
                             for n, pts in cases.items()})
    print("   expect: /a 30.0000  /b 31.0000  /c 30.0649  /d 28.8000  /e 29.4000  /f 30.1617  /g 30.2485  "
          "/h 31.2000  /i 30.1000")


def case_midpad():
    """Chains meeting on a pad centre, a via tee, inner-layer hops."""
    nets = {"/T": 20.0, "/V": 26.0, "/W": 10.0}
    p = Probe("midpad", nets, layers=4, tee="/T", size=(40.0, 34.0))
    c1 = [(5, 20), (18, 21.9), (19.8, 21.9), (20, 22)]
    c2 = [(20, 22), (20.2, 21.9), (22, 21.9), (35, 20)]
    p.track("/T", c1)
    p.track("/T", c2)
    plain = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for pts in (c1, c2) for a, b in zip(pts, pts[1:]))
    p.track("/V", [(5, 26), (20, 26)])
    p.track("/V", [(20, 26), (35, 26)])
    p.via("/V", 20, 26)
    p.track("/V", [(20, 26), (20, 29)], "B.Cu")
    p.track("/W", [(5, 10), (12, 10)])
    p.via("/W", 12, 10)
    p.track("/W", [(12, 10), (20, 10)], "In1.Cu")
    p.via("/W", 20, 10)
    p.track("/W", [(20, 10), (28, 10)], "In2.Cu")
    p.via("/W", 28, 10)
    p.track("/W", [(28, 10), (35, 10)])
    p.drc(PROBE_ALL)
    print(f"   expect: /T {plain:.4f} (the plain sum: no in-pad shortcut at the shared pad)  /V 34.5800  /W 32.0950")


def case_viastub():
    """A track that turns s mm before its via (0.6 mm via pad, r = 0.3)."""
    stubs = (0.035, 0.1, 0.2, 0.3, 0.5)
    nets = {f"/S{k}": 4.0 + 5.0 * k for k in range(len(stubs))}
    p = Probe("viastub", nets, size=(40.0, 30.0))
    want = []
    for (n, y), s_ in zip(nets.items(), stubs):
        p.track(n, [(5, y), (20, y), (20, y + s_)])
        p.via(n, 20, y + s_)
        p.track(n, [(20, y + s_), (33, y + s_)], "B.Cu")
        p.via(n, 33, y + s_)
        p.track(n, [(33, y + s_), (35, y)])
        plain = 15 + s_ + 13 + math.hypot(2, s_) + 2 * 1.58
        cut = s_ + math.sqrt(0.09 - s_ * s_) - 0.3 if s_ < 0.3 else 0.0
        want.append(f"{n} (s {s_}) {plain - cut:.4f}")
    p.drc(PROBE_ALL)
    print("   expect: " + "  ".join(want))


CASES = {"hops": case_hops, "pair": case_pair, "pair45": lambda: case_pair(False), "via": case_via,
         "unrouted": case_unrouted, "padentry": case_padentry, "fanin": case_fanin, "midpad": case_midpad,
         "viastub": case_viastub}


def main():
    cfg = WORK / "kc"
    home = WORK / "ch"
    for d in (cfg, home):
        d.mkdir(parents=True, exist_ok=True)
    os.environ["KICAD_CONFIG_HOME"] = str(cfg)          # never the user's KiCad config
    os.environ["COPPER_HOME"] = str(home)
    try:
        import wx
        wx.Log.EnableLogging(False)
    except Exception:  # noqa: BLE001
        pass
    for name in (sys.argv[1:] or list(CASES)):
        if name not in CASES:
            print(f"unknown case {name!r}; cases: {', '.join(CASES)}")
            return 2
        CASES[name]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
