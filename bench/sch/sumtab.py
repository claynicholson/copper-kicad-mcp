"""Print one line of key numbers per design of a bench output directory (debugging aid): sumtab.py <out dir>"""
import json
import sys
for r in json.load(open(sys.argv[1] + "/summary.json")):
    for s in r["sheets"]:
        m = s["metrics"]
        print(f"{r['design']:20s} {s['style_used']:6s} {m['paper']} fill {m['sheet_fill']:.3f} nc {m['nc_cross_on_own_pin_number']:2d} "
              f"lab {m['labels']:2d} x {m['wire_crossings']} tc {m['text_collisions']} score {m['score']} wired {m['wired_local_ratio']}"
              f" ragged {m.get('frame_ragged')}")
