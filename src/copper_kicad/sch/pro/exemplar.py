"""Reference numbers measured on human-drawn sheets - constants only, nothing is read at run time.

Source: the 84 sheets with at least 8 parts of the KiCad 10.0 demo projects (share/kicad/demos), measured by
bench/sch/exemplars.py (re-run it to refresh). The score (score.py) is calibrated against these; they describe how
people draw, they are not a quality guarantee for any single sheet.
"""
SHEETS = 84

LABELS_PER_PART = {"p25": 0.292, "p50": 0.847, "p75": 2.294}     # all label kinds / parts
NEIGHBOUR_MM = {"p25": 8.89, "p50": 11.43, "p75": 15.24}        # median Manhattan distance to the nearest part
SHEET_FILL = {"p25": 0.312, "p50": 0.537, "p75": 0.77}          # bounding box of the symbols / drawable area
NOTES = {"p25": 0.0, "p50": 2.0, "p75": 6.25}                   # text items per sheet
WIRES_PER_PART = {"p25": 3.161, "p50": 3.943, "p75": 5.44}
POWER_UPRIGHT = {"p25": 0.692, "p50": 0.886, "p75": 1.0}        # share of supply-up / ground-down symbols
PWR_FLAGS = {"p50": 0.0, "p90": 3.0}

# thresholds the score uses (see docs/internal/SCHEMATIC_PRO_V2_DESIGN.md section 11)
WIRED_LOCAL_MIN = 0.85
LABEL_IQR = LABELS_PER_PART["p75"] - LABELS_PER_PART["p50"]
SAT_DIST_MM = 17.8          # design value kept: satellite-to-pin distance cannot be measured without a netlist
DECAP_DIST_MM = 8.0
FILL_MIN = 0.60
BORDER_MIN_MM = 10.0
MAX_POWER_JUNCTIONS = 4
