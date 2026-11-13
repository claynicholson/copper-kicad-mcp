"""PCB engine for KiCad 9/10.

The server never imports ``pcbnew``: board edits run as short-lived worker
processes under KiCad's own Python (``pcb/workers/board.py``, driven by
``pcb/worker.py``). Server-side modules:

  board.py        run_ops / read / drc / netlist / fp_libs  (the public API other modules use)
  place.py        connectivity-driven auto placement + ratsnest score (pure Python)
  rules.py        design-rule presets from the fab profiles → .kicad_pro + .kicad_dru
  route.py        Freerouting autoroute (DSN → SES) with restore-on-failure
  freerouting.py  on-demand Java (Temurin) + pinned Freerouting jar in ~/.copper/tools
  live.py         optional IPC (kipy) capability check + live board read
"""
