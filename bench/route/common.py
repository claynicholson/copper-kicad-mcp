"""Shared plumbing of the routing benchmark: sandboxed environment, the pcbnew subprocess, kicad-cli DRC.

Everything the benchmark writes stays under bench/route/ (work dirs, a scratch KiCad config, a scratch
COPPER_HOME, temp files) — the user's real KiCad config and ~/.copper are never written.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CORPUS = HERE / "corpus"
RESULTS = HERE / "results"
WORK = HERE / ".work"
KICAD_BIN = Path(os.environ.get("COPPER_BENCH_KICAD_BIN", "C:/Program Files/KiCad/10.0/bin"))
KICAD_PY = KICAD_BIN / ("python.exe" if sys.platform == "win32" else "python3")
KICAD_CLI = KICAD_BIN / ("kicad-cli.exe" if sys.platform == "win32" else "kicad-cli")
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def sandbox() -> dict:
    """Point every piece of state at bench/route/.work. Call before importing copper_kicad.
    KiCad appends '<major>.<minor>' to KICAD_CONFIG_HOME itself."""
    cfg = WORK / "kicad-config"
    home = WORK / "copper-home"
    tmp = WORK / "tmp"
    for d in (cfg, home, tmp):
        d.mkdir(parents=True, exist_ok=True)
    # Freerouting 2.2.3 cannot read a DSN whose path contains a space ("Couldn't read the input file"): Copper puts
    # the DSN in the temp dir, so hand out the 8.3 short form of ours (the normal %TEMP% is already one)
    tmp = _short(tmp)
    os.environ["KICAD_CONFIG_HOME"] = str(cfg)
    os.environ["COPPER_HOME"] = str(home)
    os.environ["TMP"] = os.environ["TEMP"] = os.environ["TMPDIR"] = str(tmp)
    tempfile.tempdir = str(tmp)
    src = str(REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    return {"kicad_config": str(cfg), "copper_home": str(home), "tmp": str(tmp)}


def _short(p: Path) -> Path:
    if sys.platform != "win32":
        return p
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024)
        return Path(buf.value) if 0 < n < 1024 else p
    except Exception:  # noqa: BLE001
        return p


def pcb_op(req: dict, timeout: int = 900) -> dict:
    """Run one _pcb.py op in KiCad's Python."""
    WORK.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix="pcbop-", dir=str(WORK)))
    rq, rs = d / "req.json", d / "res.json"
    rq.write_text(json.dumps(req), encoding="utf-8")
    try:
        p = subprocess.run([str(KICAD_PY), "-X", "utf8", str(HERE / "_pcb.py"), str(rq), str(rs)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=_NO_WINDOW, env=dict(os.environ))
        if not rs.exists():
            raise RuntimeError(f"_pcb.py {req.get('op')} produced no result (exit {p.returncode}): "
                               f"{(p.stderr or p.stdout)[-600:]}")
        res = json.loads(rs.read_text(encoding="utf-8"))
        if not res.get("ok"):
            raise RuntimeError(f"_pcb.py {req.get('op')}: {res.get('error')}\n{res.get('trace', '')}")
        return res["result"]
    finally:
        for f in (rq, rs):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            d.rmdir()
        except OSError:
            pass


def drc(pcb: Path, timeout: int = 900) -> dict:
    """KiCad's verdict via kicad-cli (zones refilled by KiCad): errors, warnings, unconnected."""
    WORK.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix="drc-", dir=str(WORK))) / "drc.json"
    t0 = time.time()
    try:
        p = subprocess.run([str(KICAD_CLI), "pcb", "drc", "--format", "json", "--units", "mm", "--severity-all",
                            "--refill-zones", "-o", str(out), str(pcb)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=_NO_WINDOW, env=dict(os.environ))
        if not out.exists():
            raise RuntimeError(f"kicad-cli drc wrote no report (exit {p.returncode}): {(p.stderr or p.stdout)[-400:]}")
        d = json.loads(out.read_text(encoding="utf-8"))
    finally:
        try:
            out.unlink()
            out.parent.rmdir()
        except OSError:
            pass
    by = {}
    errs = []
    for v in d.get("violations", []):
        k = f"{v.get('severity')}:{v.get('type')}"
        by[k] = by.get(k, 0) + 1
        if v.get("severity") == "error":
            pos = ((v.get("items") or [{}])[0].get("pos") or {})
            errs.append({"type": v.get("type"), "message": v.get("description"),
                         "items": [i.get("description") for i in v.get("items", [])][:2],
                         "at": [pos.get("x"), pos.get("y")] if pos else None})
    unc = []
    for u in d.get("unconnected_items", []):
        its = u.get("items") or []
        if len(its) >= 2 and its[0].get("pos") and its[1].get("pos"):
            unc.append([its[0]["pos"]["x"], its[0]["pos"]["y"], its[1]["pos"]["x"], its[1]["pos"]["y"]])
    return {"errors": sum(v for k, v in by.items() if k.startswith("error:")),
            "warnings": sum(v for k, v in by.items() if k.startswith("warning:")),
            "unconnected": len(d.get("unconnected_items", [])), "by_type": dict(sorted(by.items())),
            "error_list": errs, "unconnected_lines": unc, "seconds": round(time.time() - t0, 1)}


def kicad_lengths(pcb: Path, nets=None, timeout: int = 600) -> dict:
    """{net: KiCad's own length of the net, mm} for the board as it is: a throw-away copy of the board and its
    project file gets one custom rule that no net can meet (`length (min 100000mm)`), and the "actual" of every
    resulting DRC marker is KiCad's figure — tracks, the in-pad shortcut, via height and pad-to-die as its length
    tuner and its length rules count them. A net without tracks reports 0. ``nets``: only these."""
    import re
    import shutil
    pcb = Path(pcb)
    WORK.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix="klen-", dir=str(WORK)))
    try:
        shutil.copy2(pcb, d / "probe.kicad_pcb")
        pro = pcb.with_suffix(".kicad_pro")
        if pro.exists():
            shutil.copy2(pro, d / "probe.kicad_pro")
        (d / "probe.kicad_dru").write_text(
            '(version 1)\n(rule "copper_len_probe" (severity error) (condition "A.NetName == \'*\'") '
            '(constraint length (min 100000mm)))\n', encoding="utf-8")
        out = d / "drc.json"
        p = subprocess.run([str(KICAD_CLI), "pcb", "drc", "--format", "json", "--units", "mm", "--severity-all",
                            "--refill-zones", "-o", str(out), str(d / "probe.kicad_pcb")], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=_NO_WINDOW, env=dict(os.environ))
        if not out.exists():
            raise RuntimeError(f"kicad-cli drc wrote no report (exit {p.returncode}): {(p.stderr or p.stdout)[-400:]}")
        rep = json.loads(out.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    res = {}
    for v in rep.get("violations", []):
        if v.get("type") != "length_out_of_range" or "copper_len_probe" not in str(v.get("description")):
            continue
        m = re.search(r"actual (-?[\d.]+) mm", v["description"])
        if not m:
            continue
        for it in v.get("items") or []:
            for n in re.findall(r"\[([^\]]*)\]", str(it.get("description") or "")):
                if n and (nets is None or n in nets):
                    res[n] = float(m.group(1))
    return res


def error_signature(e: dict) -> tuple:
    return (e.get("type"), e.get("message"), tuple(sorted(str(i) for i in e.get("items") or [])))


def new_errors(after: list[dict], before: list[dict]) -> list[dict]:
    """Errors in ``after`` that were not already in ``before`` (multiset difference)."""
    have = {}
    for e in before:
        have[error_signature(e)] = have.get(error_signature(e), 0) + 1
    out = []
    for e in after:
        k = error_signature(e)
        if have.get(k):
            have[k] -= 1
        else:
            out.append(e)
    return out
