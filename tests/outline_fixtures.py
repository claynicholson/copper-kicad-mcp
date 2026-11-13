"""Generated geometry for the outline tests (no downloads, nothing checked in but text).

    fancy_board(path)          a .kicad_pcb with arcs, a chamfer, a notch, a Bezier edge, a cutout, a slot, 4 holes
    enclosure(path, ...)       synthetic STEP tray (planar faces): cavity, bosses with pilot holes, rib;
                               flags: 'walls' 'lid' 'window' 'asm' (lid placed by an assembly transform) 'pcb'
    poly_of(pcb) / compare(a, b)   board polygon via pcbnew, XOR area + max boundary deviation
"""
from __future__ import annotations

import math


# ── a board with every kind of edge ───────────────────────────────────────
def fancy_board(path, thickness=1.2):
    import pcbnew as P
    mm = P.FromMM
    b = P.CreateEmptyBoard()
    V = lambda x, y: P.VECTOR2I(mm(x), mm(y))

    def seg(a, c):
        s = P.PCB_SHAPE(b)
        s.SetShape(P.SHAPE_T_SEGMENT)
        s.SetStart(V(*a))
        s.SetEnd(V(*c))
        s.SetLayer(P.Edge_Cuts)
        s.SetWidth(mm(0.05))
        b.Add(s)

    def arc(a, m, c):
        s = P.PCB_SHAPE(b)
        s.SetShape(P.SHAPE_T_ARC)
        s.SetArcGeometry(V(*a), V(*m), V(*c))
        s.SetLayer(P.Edge_Cuts)
        s.SetWidth(mm(0.05))
        b.Add(s)

    def circ(c, r):
        s = P.PCB_SHAPE(b)
        s.SetShape(P.SHAPE_T_CIRCLE)
        s.SetCenter(V(*c))
        s.SetEnd(V(c[0] + r, c[1]))
        s.SetLayer(P.Edge_Cuts)
        s.SetWidth(mm(0.05))
        b.Add(s)
    x0, y0, x1, y1 = 100.0, 100.0, 160.0, 140.0
    k = lambda r: r * (1 - math.sqrt(0.5))
    # top edge with a semicircular notch (r=4) at x=130
    seg((x0 + 3, y0), (126, y0))
    arc((126, y0), (130, y0 + 4), (134, y0))
    seg((134, y0), (x1 - 3, y0))
    arc((x1 - 3, y0), (x1 - k(3), y0 + k(3)), (x1, y0 + 3))          # top-right r=3
    seg((x1, y0 + 3), (x1, y1 - 5))
    seg((x1, y1 - 5), (x1 - 5, y1))                                   # 5 mm chamfer
    # bottom edge: cubic Bezier bulging down between x=150 and x=110
    s = P.PCB_SHAPE(b)
    s.SetShape(P.SHAPE_T_BEZIER)
    s.SetStart(V(x1 - 5, y1))
    s.SetBezierC1(V(145, y1 + 2.5))
    s.SetBezierC2(V(120, y1 + 2.5))
    s.SetEnd(V(x0 + 6, y1))
    s.SetLayer(P.Edge_Cuts)
    s.SetWidth(mm(0.05))
    try:
        s.RebuildBezierToSegmentsPointsList(mm(0.005))
    except Exception:  # noqa: BLE001
        pass
    b.Add(s)
    arc((x0 + 6, y1), (x0 + k(6), y1 - k(6)), (x0, y1 - 6))           # bottom-left r=6
    seg((x0, y1 - 6), (x0, y0 + 3))
    arc((x0, y0 + 3), (x0 + k(3), y0 + k(3)), (x0 + 3, y0))           # top-left r=3
    # rectangular cutout 10 x 6
    cx0, cy0, cx1, cy1 = 125.0, 117.0, 135.0, 123.0
    for a, c in (((cx0, cy0), (cx1, cy0)), ((cx1, cy0), (cx1, cy1)), ((cx1, cy1), (cx0, cy1)), ((cx0, cy1), (cx0, cy0))):
        seg(a, c)
    # slot 8 x 2 (two lines + two semicircles)
    seg((141, 126), (147, 126))
    arc((147, 126), (148, 127), (147, 128))
    seg((147, 128), (141, 128))
    arc((141, 128), (140, 127), (141, 126))
    for c in ((104, 104), (156, 104), (104, 134), (150, 134)):
        circ(c, 1.6)
    b.GetDesignSettings().SetBoardThickness(mm(thickness))
    P.SaveBoard(str(path), b)
    return [(104, 104), (156, 104), (104, 134), (150, 134)]


def rounded_board(path, w=45.0, h=32.0, r=3.0):
    import pcbnew as P
    mm = P.FromMM
    b = P.CreateEmptyBoard()
    V = lambda x, y: P.VECTOR2I(mm(x), mm(y))
    x0, y0, x1, y1 = 100.0, 100.0, 100.0 + w, 100.0 + h
    k = r * (1 - math.sqrt(0.5))

    def add(shape, *pts):
        s = P.PCB_SHAPE(b)
        s.SetShape(shape)
        if shape == P.SHAPE_T_ARC:
            s.SetArcGeometry(V(*pts[0]), V(*pts[1]), V(*pts[2]))
        else:
            s.SetStart(V(*pts[0]))
            s.SetEnd(V(*pts[1]))
        s.SetLayer(P.Edge_Cuts)
        s.SetWidth(mm(0.05))
        b.Add(s)
    add(P.SHAPE_T_SEGMENT, (x0 + r, y0), (x1 - r, y0))
    add(P.SHAPE_T_SEGMENT, (x1, y0 + r), (x1, y1 - r))
    add(P.SHAPE_T_SEGMENT, (x1 - r, y1), (x0 + r, y1))
    add(P.SHAPE_T_SEGMENT, (x0, y1 - r), (x0, y0 + r))
    add(P.SHAPE_T_ARC, (x1 - r, y0), (x1 - k, y0 + k), (x1, y0 + r))
    add(P.SHAPE_T_ARC, (x1, y1 - r), (x1 - k, y1 - k), (x1 - r, y1))
    add(P.SHAPE_T_ARC, (x0 + r, y1), (x0 + k, y1 - k), (x0, y1 - r))
    add(P.SHAPE_T_ARC, (x0, y0 + r), (x0 + k, y0 + k), (x0 + r, y0))
    P.SaveBoard(str(path), b)


def poly_of(pcb):
    import pcbnew as P
    b = P.LoadBoard(str(pcb))
    ps = P.SHAPE_POLY_SET()
    ok = b.GetBoardPolygonOutlines(ps, False)
    return ok, ps, b


def _rings(ps):
    out = []
    for o in range(ps.OutlineCount()):
        for c in [ps.Outline(o)] + [ps.Hole(o, h) for h in range(ps.HoleCount(o))]:
            out.append([(c.CPoint(i).x / 1e6, c.CPoint(i).y / 1e6) for i in range(c.PointCount())])
    return out


def _seg_d(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    t = 0 if l2 == 0 else max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def compare(pcb_a, pcb_b, shift=(0.0, 0.0), holes=()):
    """-> (xor area mm2, area of a, max boundary deviation mm) with b moved by ``shift`` first.
    holes = [(x, y, r)]: Edge.Cuts circles of a that b carries as mounting-hole footprints instead; they
    are taken out of the comparison (their area from the XOR, their rings from the deviation)."""
    import pcbnew as P
    ok_a, pa, _ = poly_of(pcb_a)
    ok_b, pb, _ = poly_of(pcb_b)
    assert ok_a and ok_b, "an outline is not closed"
    if shift != (0.0, 0.0):
        pb.Move(P.VECTOR2I(int(shift[0] * 1e6), int(shift[1] * 1e6)))
    ra, rb = _rings(pa), _rings(pb)
    is_hole = lambda ring: any(all(abs(math.hypot(px - hx, py - hy) - hr) < 0.02 for px, py in ring) for hx, hy, hr in holes)
    n0 = len(ra)
    ra = [r for r in ra if not is_hole(r)]
    x = P.SHAPE_POLY_SET(pa)
    x.BooleanXor(pb)

    def dev(A, B):
        segs = [(c[i], c[(i + 1) % len(c)]) for c in B for i in range(len(c))]
        return max(min(_seg_d(p, a, b) for a, b in segs) for c in A for p in c)
    skipped = sum(math.pi * hr * hr for _, _, hr in holes) if n0 != len(ra) else 0.0
    return max(0.0, x.Area() / 1e12 - skipped), pa.Area() / 1e12, max(dev(ra, rb), dev(rb, ra))


# ── synthetic STEP enclosure ──────────────────────────────────────────────
class _Step:
    def __init__(self, k=1.0):
        self.E, self.K = [], k

    def add(self, s):
        self.E.append(s)
        return "#%d" % len(self.E)

    def pt(self, x, y, z):
        return self.add("CARTESIAN_POINT('',(%.9g,%.9g,%.9g))" % (x * self.K, y * self.K, z * self.K))

    def dr(self, x, y, z):
        return self.add("DIRECTION('',(%.9g,%.9g,%.9g))" % (x, y, z))

    def ax(self, o, z, x):
        return self.add("AXIS2_PLACEMENT_3D('',%s,%s,%s)" % (self.pt(*o), self.dr(*z), self.dr(*x)))

    def vx(self, p):
        return self.add("VERTEX_POINT('',%s)" % self.pt(*p))

    def line_edge(self, a, b):
        d = [b[i] - a[i] for i in range(3)]
        l = math.sqrt(sum(v * v for v in d))
        ln = self.add("LINE('',%s,%s)" % (self.pt(*a), self.add("VECTOR('',%s,%.9g)" % (self.dr(*[v / l for v in d]), l * self.K))))
        ec = self.add("EDGE_CURVE('',%s,%s,%s,.T.)" % (self.vx(a), self.vx(b), ln))
        return self.add("ORIENTED_EDGE('',*,*,%s,.T.)" % ec)

    def arc_edge(self, c, r, a0, a1, z):
        p = lambda t: (c[0] + r * math.cos(t), c[1] + r * math.sin(t), z)
        ci = self.add("CIRCLE('',%s,%.9g)" % (self.ax((c[0], c[1], z), (0, 0, 1), (1, 0, 0)), r * self.K))
        if abs(a1 - a0 - 2 * math.pi) < 1e-9:
            v = self.vx(p(a0))
            ec = self.add("EDGE_CURVE('',%s,%s,%s,.T.)" % (v, v, ci))
        else:
            ec = self.add("EDGE_CURVE('',%s,%s,%s,.T.)" % (self.vx(p(a0)), self.vx(p(a1)), ci))
        return self.add("ORIENTED_EDGE('',*,*,%s,.T.)" % ec)

    def rect_loop(self, x0, y0, x1, y1, z, r=0):
        if r <= 0:
            c = [(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)]
            es = [self.line_edge(c[i], c[(i + 1) % 4]) for i in range(4)]
        else:
            h = math.pi / 2
            es = [self.line_edge((x0 + r, y0, z), (x1 - r, y0, z)), self.arc_edge((x1 - r, y0 + r), r, -h, 0, z),
                  self.line_edge((x1, y0 + r, z), (x1, y1 - r, z)), self.arc_edge((x1 - r, y1 - r), r, 0, h, z),
                  self.line_edge((x1 - r, y1, z), (x0 + r, y1, z)), self.arc_edge((x0 + r, y1 - r), r, h, 2 * h, z),
                  self.line_edge((x0, y1 - r, z), (x0, y0 + r, z)), self.arc_edge((x0 + r, y0 + r), r, 2 * h, 3 * h, z)]
        return self.add("EDGE_LOOP('',(%s))" % ",".join(es))

    def circ_loop(self, cx, cy, r, z):
        return self.add("EDGE_LOOP('',(%s))" % self.arc_edge((cx, cy), r, 0, 2 * math.pi, z))

    def wall_loop(self, c):
        return self.add("EDGE_LOOP('',(%s))" % ",".join(self.line_edge(c[i], c[(i + 1) % 4]) for i in range(4)))

    def face(self, faces, o, n, x, outer, inner=()):
        b = [self.add("FACE_OUTER_BOUND('',%s,.T.)" % outer)] + [self.add("FACE_BOUND('',%s,.T.)" % i) for i in inner]
        faces.append(self.add("ADVANCED_FACE('',(%s),%s,.T.)" % (",".join(b), self.add("PLANE('',%s)" % self.ax(o, n, x)))))

    def solid(self, name, faces):
        sh = self.add("CLOSED_SHELL('',(%s))" % ",".join(faces))
        return self.add("MANIFOLD_SOLID_BREP('%s',%s)" % (name, sh))

    def plate(self, name, x0, y0, x1, y1, z0, z1, r=0):
        f = []
        self.face(f, (0, 0, z1), (0, 0, 1), (1, 0, 0), self.rect_loop(x0, y0, x1, y1, z1, r))
        self.face(f, (0, 0, z0), (0, 0, -1), (1, 0, 0), self.rect_loop(x0, y0, x1, y1, z0, r))
        return self.solid(name, f)


def enclosure(path, unit="mm", zh=20.0, flags="", header_extra="", data_kw="DATA;"):
    """80 x 50 x zh tray, 2 mm floor, cavity 76 x 46 with r=3 corners, 4 bosses d=7 h=4 with 2.5 mm pilot
    holes, one 4 x 10 rib 10 mm tall. flags (space separated): walls, lid, window, asm, pcb."""
    fl = set(flags.split())
    s = _Step(1.0 if unit == "mm" else 1 / 25.4)
    X, Y, Z, FL = 80.0, 50.0, float(zh), 2.0
    bosses = [(8, 8), (72, 8), (8, 42), (72, 42)]
    f = []
    win = [s.rect_loop(50, 15, 65, 35, 0)] if "window" in fl else []
    s.face(f, (0, 0, 0), (0, 0, -1), (1, 0, 0), s.rect_loop(0, 0, X, Y, 0), win)
    s.face(f, (0, 0, Z), (0, 0, 1), (1, 0, 0), s.rect_loop(0, 0, X, Y, Z), [s.rect_loop(2, 2, X - 2, Y - 2, Z, 3)])
    s.face(f, (0, 0, FL), (0, 0, 1), (1, 0, 0), s.rect_loop(2, 2, X - 2, Y - 2, FL, 3),
           [s.circ_loop(bx, by, 3.5, FL) for bx, by in bosses] + [s.rect_loop(38, 20, 42, 30, FL)]
           + ([s.rect_loop(50, 15, 65, 35, FL)] if "window" in fl else []))
    for bx, by in bosses:
        s.face(f, (bx, by, FL + 4), (0, 0, 1), (1, 0, 0), s.circ_loop(bx, by, 3.5, FL + 4), [s.circ_loop(bx, by, 1.25, FL + 4)])
    s.face(f, (0, 0, FL + 10), (0, 0, 1), (1, 0, 0), s.rect_loop(38, 20, 42, 30, FL + 10))
    s.face(f, (0, 0, 0), (0, -1, 0), (1, 0, 0), s.wall_loop([(0, 0, 0), (X, 0, 0), (X, 0, Z), (0, 0, Z)]))
    s.face(f, (0, Y, 0), (0, 1, 0), (1, 0, 0), s.wall_loop([(0, Y, 0), (X, Y, 0), (X, Y, Z), (0, Y, Z)]))
    s.face(f, (0, 0, 0), (-1, 0, 0), (0, 1, 0), s.wall_loop([(0, 0, 0), (0, Y, 0), (0, Y, Z), (0, 0, Z)]))
    s.face(f, (X, 0, 0), (1, 0, 0), (0, 1, 0), s.wall_loop([(X, 0, 0), (X, Y, 0), (X, Y, Z), (X, 0, Z)]))
    if "walls" in fl:
        a, b, c, d = 2, 2, X - 2, Y - 2
        s.face(f, (a, b, FL), (0, 1, 0), (1, 0, 0), s.wall_loop([(a, b, FL), (c, b, FL), (c, b, Z), (a, b, Z)]))
        s.face(f, (a, d, FL), (0, -1, 0), (1, 0, 0), s.wall_loop([(a, d, FL), (c, d, FL), (c, d, Z), (a, d, Z)]))
        s.face(f, (a, b, FL), (1, 0, 0), (0, 1, 0), s.wall_loop([(a, b, FL), (a, d, FL), (a, d, Z), (a, b, Z)]))
        s.face(f, (c, b, FL), (-1, 0, 0), (0, 1, 0), s.wall_loop([(c, b, FL), (c, d, FL), (c, d, Z), (c, b, Z)]))
    tray = s.solid("enclosure", f)
    if "asm" in fl:
        # tray, lid (and optionally a PCB) as separate parts of an assembly; the lid and the PCB are modelled
        # at their own origin and put in place by ITEM_DEFINED_TRANSFORMATION
        ident = s.ax((0, 0, 0), (0, 0, 1), (1, 0, 0))
        lid = s.plate("lid", 0, 0, X, Y, 0, 2, 3)
        lid_at = s.ax((0, 0, Z), (0, 0, 1), (1, 0, 0))
        asm_items = [ident, lid_at]
        reps = [(tray, ident, None), (lid, s.ax((0, 0, 0), (0, 0, 1), (1, 0, 0)), lid_at)]
        if "pcb" in fl:
            pcb = s.plate("pcb", 0, 0, 60, 40, 0, 1.6, 2)
            pcb_at = s.ax((10, 5, FL + 4), (0, 0, 1), (1, 0, 0))
            asm_items.append(pcb_at)
            reps.append((pcb, s.ax((0, 0, 0), (0, 0, 1), (1, 0, 0)), pcb_at))
        asm = s.add("SHAPE_REPRESENTATION('assembly',(%s),$)" % ",".join(asm_items))
        for sol, local, target in reps:
            rep = s.add("ADVANCED_BREP_SHAPE_REPRESENTATION('',(%s,%s),$)" % (local, sol))
            idt = s.add("ITEM_DEFINED_TRANSFORMATION('','',%s,%s)" % (local, target or ident))
            rr = s.add("( REPRESENTATION_RELATIONSHIP('','',%s,%s) REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION(%s) "
                       "SHAPE_REPRESENTATION_RELATIONSHIP() )" % (rep, asm, idt))
            s.add("CONTEXT_DEPENDENT_SHAPE_REPRESENTATION(%s,$)" % rr)
    elif "lid" in fl:
        s.plate("lid", 0, 0, X, Y, Z, Z + 2, 3)
    if unit == "mm":
        s.add("( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) )")
    else:
        s.add("( NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) )")
        s.add("( CONVERSION_BASED_UNIT('INCH',#%d) LENGTH_UNIT() NAMED_UNIT(#%d) )" % (len(s.E), len(s.E)))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('synthetic enclosure%s'),'2;1');\nFILE_NAME('enc','',(''),(''),'','','');\n"
                 "FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\nENDSEC;\n%s\n" % (header_extra, data_kw))
        for i, e in enumerate(s.E):
            fh.write("#%d = %s;\n" % (i + 1, e))
        fh.write("ENDSEC;\nEND-ISO-10303-21;\n")
    return len(s.E)
