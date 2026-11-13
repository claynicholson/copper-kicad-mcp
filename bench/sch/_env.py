"""Shared bootstrap for the schematic bench: everything (temp files, staged copies, Copper home, KiCad config)
is redirected into bench/sch/.work so a run never touches the user's real KiCad / Copper configuration."""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WORK = HERE / ".work"
CORPUS = HERE / "corpus"
UNSEEN = HERE / "corpus_unseen"      # designs the engine was not developed against; reported separately
OUT = HERE / "out"


def setup():
    """Every process gets its OWN work directory (.work/p<pid>): two benchmark runs at the same time used to share
    witness files and staged copies, and reported another design's netlist as a difference."""
    global WORK
    import atexit
    import shutil
    import tempfile
    base = WORK
    base.mkdir(parents=True, exist_ok=True)
    WORK = base / f"p{os.getpid()}"
    shutil.rmtree(WORK, ignore_errors=True)
    for k, d in (("COPPER_HOME", "home"), ("COPPER_CONFIG_HOME_OVERRIDE", "cfg"), ("KICAD_CONFIG_HOME", "kicad-config"),
                 ("COPPER_STAGE_DIR", "stage"), ("TEMP", "tmp"), ("TMP", "tmp"), ("TMPDIR", "tmp")):
        p = WORK / d
        p.mkdir(parents=True, exist_ok=True)
        os.environ[k] = str(p)
    tempfile.tempdir = str(WORK / "tmp")
    atexit.register(shutil.rmtree, WORK, True)
    sys.path.insert(0, str(ROOT / "src"))
