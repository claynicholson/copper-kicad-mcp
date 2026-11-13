"""KiCad compatibility layer: versions, install selection, file-format stamps,
kicad-cli capabilities and known issues.

Stdlib only. Imports nothing from Copper except ``ToolError`` so that
``kicad/env.py`` can import it without a cycle.

What lives here
---------------
``KiVer`` / ``parse_version``     version of one KiCad binary
``select``                        which install Copper uses, and why
``file_format`` / ``project_stamps`` / ``check_readable`` / ``check_project``
                                  never hand KiCad a file it would silently drop
``guard_write``                   never move a file to a format the user's KiCad cannot open
``Caps`` / ``caps`` / ``OPTIONAL_FLAGS``
                                  ask the binary which commands and flags it has
``KNOWN_ISSUES`` / ``advisories`` what is wrong with a given KiCad version
``latest_release``                newest stable KiCad Copper supports (doctor / CLI only)

Everything about KiCad versions other than the ones Copper's CI runs is
gated on what the *binary* says (``--help`` output, a real ``upgrade`` run),
not on a hard-coded version table; see docs/COMPATIBILITY.md for what has
and has not been verified against a real binary.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..mcp.registry import ToolError

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Oldest series Copper authors with. Below it: view/verify only.
FLOOR = (10, 0)
# Newest major Copper's PCB engine supports. KiCad 11 removes the pcbnew Python module that every
# pcb_* edit runs on, so the update check must not recommend it until Copper has an engine for it.
SUPPORTED_MAX_MAJOR = 10
DOWNLOAD_URL = "https://www.kicad.org/download/"


# ── versions ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class KiVer:
    """One KiCad version. Deliberately NOT ``order=True``: comparing ``extra``
    lexically would sort 10.0.8-rc1 above 10.0.8. Compare ``.t``; sort with
    :func:`newest_key`."""
    major: int
    minor: int
    patch: int = 0
    extra: str = ""                      # "-rc1", "-3904-gabcdef12", ""

    @property
    def t(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    @property
    def nightly(self) -> bool:
        return self.minor == 99

    @property
    def prerelease(self) -> bool:
        return self.nightly or bool(re.search(r"(?i)(?:^|[-~.+])rc\d+", self.extra))

    @property
    def series(self) -> str:
        return f"{self.major}.{self.minor}"

    @property
    def effective_major(self) -> int:
        """Major the build is heading for: 10.99 is KiCad 11 development."""
        return self.major + 1 if self.nightly else self.major

    @property
    def channel(self) -> str:
        if self.nightly:
            return "nightly"
        if self.prerelease:
            return "rc"
        return "stable" if (self.major, self.minor) >= FLOOR else "old"

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}{self.extra}"


_VER_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?((?:[-~]rc\d+)?(?:[-+.~][0-9A-Za-z.+~-]+)?)")


def parse_version(text) -> KiVer | None:
    m = _VER_RE.search(str(text or ""))
    if not m:
        return None
    return KiVer(int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), (m.group(4) or "").rstrip(".,"))


def newest_key(v: KiVer | None):
    """Sort key: higher = newer; a pre-release sorts below the release of the same number."""
    return (v.t, not v.prerelease) if v else ((0, 0, 0), False)


def _ver(inst) -> KiVer | None:
    v = getattr(inst, "ver", None)
    if v is None:
        v = parse_version(getattr(inst, "full_version", "") or getattr(inst, "version", ""))
    return v


# ── file formats ──────────────────────────────────────────────────────────
# Offline fallback only: for a series not listed here (or a file newer than the table says the
# series reads) the binary itself is asked once (learned stamps, cached with the capabilities).
FORMATS: dict[str, dict[str, int]] = {
    "9.0": {"sch": 20250114, "sym": 20241209, "pcb": 20241229, "fp": 20241229},
    "10.0": {"sch": 20260306, "sym": 20251024, "pcb": 20260206, "fp": 20260206},
}
AUTHOR_SERIES = "10.0"          # dialect Copper's own writers emit on every KiCad >= 10
_HEAD = re.compile(rb"\s*\((kicad_sch|kicad_pcb|kicad_symbol_lib|footprint|module)\b")
_KIND = {b"kicad_sch": "sch", b"kicad_pcb": "pcb", b"kicad_symbol_lib": "sym", b"footprint": "fp", b"module": "fp"}
_VERSION = re.compile(rb"\(version\s+(\d{8})\)")
_GEN = re.compile(rb'\(generator_version\s+"([^"]*)"\)')
_SHEETFILE = re.compile(rb'\(property\s+"Sheet ?file"\s+"((?:[^"\\]|\\.)*)"')
DESIGN_SUFFIXES = (".kicad_sch", ".kicad_pcb", ".kicad_sym", ".kicad_mod")
_SUFFIX_KIND = {".kicad_sch": "sch", ".kicad_pcb": "pcb", ".kicad_sym": "sym", ".kicad_mod": "fp"}

_fmt_cache: dict[tuple, dict] = {}
_fmt_lock = threading.Lock()
_norm_cache: dict[str, str] = {}


def _norm(p: str) -> str:
    """normcase(abspath(p)), memoised: abspath is the expensive part of a warm preflight on Windows."""
    n = _norm_cache.get(p)
    if n is None:
        if len(_norm_cache) > 8000:
            _norm_cache.clear()
        n = _norm_cache[p] = os.path.normcase(os.path.abspath(p))
    return n


def file_format(path) -> dict:
    """``{"kind": sch|pcb|sym|fp|None, "version": int|None, "generator_version": str, "subs": [str]}``.

    Reads the first 600 bytes for the header (a UTF-8 BOM and leading
    whitespace are tolerated); a schematic is read whole once to collect its
    sub-sheet file names (bytes regex, no s-expression parse). Cached per
    ``(path, mtime_ns, size)``. Raises OSError when the file cannot be read."""
    p = os.fspath(path)
    st = os.stat(p)
    key = (_norm(p), st.st_mtime_ns, st.st_size)
    c = _fmt_cache.get(key)
    if c is not None:
        return c
    with open(p, "rb") as f:
        head = f.read(600)
        m = _HEAD.match(head[3:] if head.startswith(b"\xef\xbb\xbf") else head)
        kind = _KIND.get(m.group(1)) if m else None
        subs: list[str] = []
        if kind == "sch":
            data = head + f.read()
            for s in _SHEETFILE.findall(data):
                t = s.decode("utf-8", "replace").replace('\\"', '"').replace("\\\\", "\\")
                if t and t not in subs:
                    subs.append(t)
    v, g = _VERSION.search(head), _GEN.search(head)
    c = {"kind": kind, "version": int(v.group(1)) if v else None,
         "generator_version": g.group(1).decode("utf-8", "replace") if g else "", "subs": subs}
    with _fmt_lock:
        if len(_fmt_cache) > 4000:
            _fmt_cache.clear()
        _fmt_cache[key] = c
    return c


def _sheet_tree(root) -> list[str]:
    """Root sheet + every sub-sheet file it references (recursively), each
    file once however many sheet symbols point at it. Missing files and sheet
    names containing text variables are skipped (the guard fails open there)."""
    out, todo, seen = [], [os.fspath(root)], set()
    while todo:
        f = todo.pop()
        k = _norm(f)
        if k in seen:
            continue
        seen.add(k)
        try:
            c = file_format(f)
        except OSError:
            continue
        out.append(f)
        base = os.path.dirname(f)
        todo += [os.path.normpath(os.path.join(base, s)) for s in reversed(c["subs"]) if "${" not in s]
    return out


def project_stamps(pr) -> tuple[tuple[str, str, int], ...]:
    """``(path, kind, stamp)`` for the root sheet, every sub-sheet and the board.
    ``pr`` is a Project (``.sch`` / ``.pcb``). Files without a readable stamp are left out."""
    out = []
    files = _sheet_tree(pr.sch) if os.path.exists(pr.sch) else []
    if os.path.exists(pr.pcb):
        files.append(os.fspath(pr.pcb))
    for f in files:
        try:
            c = file_format(f)
        except OSError:
            continue
        if c["kind"] in ("sch", "pcb") and c["version"]:
            out.append((f, c["kind"], c["version"]))
    return tuple(out)


def project_format(pr, stamps=None) -> tuple[tuple[str, int], ...] | None:
    """Newest stamp per kind, e.g. ``(("pcb", 20241229), ("sch", 20250114))`` — the selector's input."""
    best: dict[str, int] = {}
    for _f, kind, stamp in (project_stamps(pr) if stamps is None else stamps):
        best[kind] = max(best.get(kind, 0), stamp)
    return tuple(sorted(best.items())) or None


def series_of_stamp(kind: str, stamp: int) -> str:
    """Oldest known series whose format covers ``stamp`` ("9.0", "10.0", or "newer")."""
    for s in sorted(FORMATS, key=lambda x: [int(n) for n in x.split(".")]):
        if stamp <= FORMATS[s].get(kind, 0):
            return s
    return "newer"


def max_stamp(inst, kind: str, need: int | None = None) -> int | None:
    """Newest ``kind`` stamp this install reads; None = unknown (callers allow).

    The series table answers the normal case with no subprocess. The binary is
    asked (three ``upgrade`` runs in a temp dir, cached per binary) only when
    the series is not in the table, or ``need`` is newer than the table says."""
    v = _ver(inst)
    table = FORMATS.get(v.series) if v else None
    tv = table.get(kind) if table else None
    if tv is not None and (need is None or need <= tv):
        return tv
    learned = learned_stamps(inst).get(kind)
    if learned:
        return max(learned, tv or 0)
    return tv


def learned_stamps(inst) -> dict:
    """Format stamps the binary itself writes, measured once per binary: three
    one-line files in a temp dir run through ``sch|pcb|sym upgrade --force``."""
    cli = getattr(inst, "cli", None)
    if not cli or not os.path.isfile(cli):
        return {}
    c = caps(inst)
    got = c.extra("formats")
    if isinstance(got, dict):
        return got
    out: dict[str, int] = {}
    tmp = tempfile.mkdtemp(prefix="copper-fmt-")
    try:
        for kind, name, text, cmd in (
                ("sch", "a.kicad_sch", '(kicad_sch (version 20230121) (generator "x"))', "sch"),
                ("pcb", "a.kicad_pcb", '(kicad_pcb (version 20221018) (generator "x"))', "pcb"),
                ("sym", "a.kicad_sym", '(kicad_symbol_lib (version 20220914) (generator "x"))', "sym")):
            f = os.path.join(tmp, name)
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            try:
                subprocess.run([*_argv(inst), cmd, "upgrade", "--force", f], capture_output=True, timeout=60,
                               stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, cwd=tmp)
                with open(f, "rb") as fh:
                    m = _VERSION.search(fh.read(600))
            except (OSError, subprocess.SubprocessError):
                m = None
            if m and int(m.group(1)) > 20230121:
                out[kind] = int(m.group(1))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if "pcb" in out:
        out["fp"] = out["pcb"]
    c.set_extra("formats", out)
    return out


def write_stamp(kind: str, inst=None) -> int:
    """Stamp Copper's own writers put on a NEW file: the 10.0 dialect on every
    KiCad >= 10 (newer KiCads read it and canonicalise it on save). Never a
    newer stamp over 10.0-dialect content."""
    mine = FORMATS[AUTHOR_SERIES][kind]
    mx = max_stamp(inst, kind) if inst is not None else None
    return min(mine, mx) if mx else mine


def _too_new(path: str, c: dict, inst, mx: int) -> dict:
    return {"file": path, "kind": c["kind"], "format": c["version"], "saved_by": c["generator_version"] or "?",
            "active_kicad": str(_ver(inst) or "?"), "reads_up_to": mx}


def _raise_too_new(bad: list[dict]):
    b = bad[0]
    more = f" (+{len(bad) - 1} more file(s))" if len(bad) > 1 else ""
    sv = b["saved_by"]
    raise ToolError(
        f"{os.path.basename(b['file'])} was saved by KiCad {sv} (format {b['format']}); the active KiCad "
        f"{b['active_kicad']} reads up to {b['reads_up_to']}{more}. Nothing was changed.",
        code="FORMAT_TOO_NEW",
        fix=f"open the project with the KiCad that saved it, or install that KiCad next to this one "
            f"(Copper picks it automatically; COPPER_KICAD_VERSION={sv if sv != '?' else '<series>'} pins it)",
        data={"files": bad})


def _unreadable(path: str, inst) -> dict | None:
    try:
        c = file_format(path)
    except OSError:
        return None
    if not c["kind"] or not c["version"]:
        return None
    mx = max_stamp(inst, c["kind"], need=c["version"])
    if mx is not None and c["version"] > mx:
        return _too_new(path, c, inst, mx)
    return None


def check_readable(path, inst) -> None:
    """Raise FORMAT_TOO_NEW when ``path`` (for a schematic: it or any of its
    sub-sheets) was written by a KiCad newer than ``inst`` reads. kicad-cli
    gives no reason for the former and silently drops the latter."""
    p = os.fspath(path)
    files = _sheet_tree(p) if p.lower().endswith(".kicad_sch") else [p]
    bad = [b for b in (_unreadable(f, inst) for f in files) if b]
    if bad:
        _raise_too_new(bad)


def check_project(pr, inst, stamps=None) -> None:
    """Raise FORMAT_TOO_NEW naming every project file ``inst`` cannot read."""
    bad = []
    for f, kind, stamp in (project_stamps(pr) if stamps is None else stamps):
        mx = max_stamp(inst, kind, need=stamp)
        if mx is not None and stamp > mx:
            bad.append(_too_new(f, file_format(f), inst, mx))
    if bad:
        _raise_too_new(bad)


def format_report(pr, inst=None) -> dict:
    """``{"sch": stamp, "pcb": stamp, "series": "10.0", "mixed": bool, "active_kicad": ...}`` for project_open."""
    stamps = project_stamps(pr)
    d: dict = dict(project_format(pr) or ())
    ser = {series_of_stamp(k, s) for _f, k, s in stamps}
    if ser:
        d["series"] = "newer" if "newer" in ser else max(ser, key=lambda x: [int(n) for n in x.split(".")])
        d["mixed"] = len(ser) > 1
    if inst is not None:
        d["active_kicad"] = str(_ver(inst) or "?")
    return d


# ── per-project state (.copper/state.json) + one-time notes ───────────────
def _pstate_file(project_dir) -> Path:
    return Path(project_dir) / ".copper" / "state.json"


def _pstate(project_dir) -> dict:
    try:
        d = json.loads(_pstate_file(project_dir).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _pstate_update(project_dir, fn) -> bool:
    d = _pstate(project_dir)
    fn(d)
    f = _pstate_file(project_dir)
    try:
        f.parent.mkdir(exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
        os.replace(tmp, f)
        return True
    except OSError:
        return False


def allow_format_upgrade(project_dir, series: str | None) -> None:
    """Record (or with None: clear) the user's consent to a one-way upgrade to a pre-release format."""
    def fn(d):
        if series:
            d["allow_format"] = series
        else:
            d.pop("allow_format", None)
    _pstate_update(project_dir, fn)


_notes: dict[str, list] = {}
_notes_lock = threading.Lock()
_advised: set[str] = set()


def add_note(key: str, value) -> None:
    """Queue a one-time note; the tool dispatcher attaches it to the next tool result."""
    with _notes_lock:
        lst = _notes.setdefault(key, [])
        if value not in lst:
            lst.append(value)


def take_notes() -> dict:
    settle_pending()
    with _notes_lock:
        d = dict(_notes)
        _notes.clear()
    return d


def attach_notes(out):
    """Merge queued notes into a tool result (a ``Result`` with ``.data`` or a plain dict)."""
    notes = take_notes()
    if not notes:
        return out
    data = out if isinstance(out, dict) else getattr(out, "data", None)
    if not isinstance(data, dict):
        with _notes_lock:                      # nothing to attach to: keep them for the next call
            for k, v in notes.items():
                _notes.setdefault(k, []).extend(x for x in v if x not in _notes.get(k, []))
        return out
    for k, v in notes.items():
        if k == "format_upgraded" and len(v) == 1:
            data.setdefault(k, v[0])
        else:
            data.setdefault(k, v)
    return out


def _project_dir_of(path: str) -> str:
    """The project folder a design file belongs to: the nearest folder (the file's own, or up to three
    levels up for sub-sheets kept in sub-folders) that holds a .kicad_pro; else the file's own folder."""
    d0 = d = os.path.dirname(os.path.abspath(path))
    for _ in range(4):
        try:
            if any(n.endswith(".kicad_pro") for n in os.listdir(d)):
                return d
        except OSError:
            break
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    return d0


def _project_name(pdir: str, path: str) -> str:
    try:
        pros = sorted(n for n in os.listdir(pdir) if n.endswith(".kicad_pro"))
    except OSError:
        pros = []
    return pros[0][:-len(".kicad_pro")] if pros else Path(path).stem


def require_working(inst) -> None:
    """Raise KICAD_CLI_BROKEN when the active kicad-cli did not even answer ``version``.

    Without a version none of the format guards can decide anything, and every later kicad-cli call fails
    with whatever the shell says: an edit would then be written without KiCad ever having checked it, and
    reads would come back empty. Refuse up front, before any file is touched."""
    if _ver(inst) is not None:
        return
    cli = getattr(inst, "cli", None)
    pinned = os.environ.get("COPPER_KICAD_CLI") or os.environ.get("KICAD_CLI")
    raise ToolError(f"kicad-cli does not run ({cli}): `kicad-cli version` gave no answer. Nothing was changed.",
                    code="KICAD_CLI_BROKEN",
                    fix=("COPPER_KICAD_CLI points at it: set it to a working kicad-cli or unset it, then restart "
                         "the server" if pinned else f"repair or reinstall KiCad ({DOWNLOAD_URL}), then run `copper doctor`"))


def require_authoring(inst) -> None:
    """Raise KICAD_TOO_OLD when the active KiCad cannot be used to create or edit designs."""
    v = _ver(inst)
    if v is not None and (v.major, v.minor) < FLOOR:
        raise ToolError(f"KiCad {v} can view and check this project, but Copper edits need KiCad 10.",
                        code="KICAD_TOO_OLD",
                        fix=f"install KiCad 10 from {DOWNLOAD_URL} (it can sit next to KiCad {v.major})")


def guard_write(path, inst) -> dict:
    """Format policy for a write to ``path`` (.kicad_sch / .kicad_pcb) with ``inst``.

    ====================================================  ==========================================
    file's format vs the active install                   action
    ====================================================  ==========================================
    active install older than KiCad 10                    KICAD_TOO_OLD
    file missing, or same format                          write
    file newer than the install reads                     FORMAT_TOO_NEW
    file older, install is a stable release               checkpoint once, write, one-time note
    file older, install is a pre-release (nightly / rc)   FORMAT_UPGRADE_BLOCKED unless the user
                                                          agreed (project_open allow_format_upgrade)
    ====================================================  ==========================================
    """
    p = os.fspath(path)
    kind = _SUFFIX_KIND.get(os.path.splitext(p)[1].lower())
    if kind not in ("sch", "pcb"):
        return {}
    require_working(inst)
    v = _ver(inst)
    require_authoring(inst)
    try:
        c = file_format(p)
    except OSError:
        return {}
    stamp = c["version"]
    if not stamp or c["kind"] != kind:
        return {}
    mx = max_stamp(inst, kind, need=stamp)
    if mx is not None and stamp > mx:
        _raise_too_new([_too_new(p, c, inst, mx)])
    stable_top = max(f[kind] for f in FORMATS.values())
    if v.prerelease and v.series not in FORMATS:
        older = stamp <= stable_top and (mx is None or stamp < mx)
    else:
        older = mx is not None and stamp < mx
    if not older:
        return {}
    pdir = _project_dir_of(p)
    rel = os.path.relpath(os.path.abspath(p), pdir).replace(os.sep, "/")
    st = _pstate(pdir)
    frm = series_of_stamp(kind, stamp)
    if v.prerelease and v.series not in FORMATS and st.get("allow_format") != v.series:
        raise ToolError(
            f"{rel} is KiCad {frm} format; the KiCad Copper uses for this project is the {v} pre-release, and saving "
            f"would make the file unreadable in KiCad {frm}.", code="FORMAT_UPGRADE_BLOCKED",
            fix=f"install a stable KiCad 10.0.x from {DOWNLOAD_URL}, or project_open(path, allow_format_upgrade=true) "
                "to accept a one-way upgrade")
    # Reaching this point means the file on disk is still in the old format (after the upgrade its stamp is
    # current and the function returned above), so the snapshot and the note are due: once per real upgrade.
    note = {"files": [rel], "from": frm, "to": v.series}
    held = False
    try:                                         # snapshot the old-format files before the first upgrade
        from .. import project as _project
        note["checkpoint"] = _project.checkpoint(_project.Project(Path(pdir), _project_name(pdir, p)),
                                                 f"before KiCad {v.series} format upgrade")
        held = os.path.isfile(_project._long(Path(pdir) / ".copper" / "snapshots" / note["checkpoint"] / rel))
    except Exception as e:  # noqa: BLE001 — a read-only project fails on the write itself, with a better message
        note["checkpoint_error"] = str(getattr(e, "message", e))[:160]
    if not held:
        # The checkpoint does not hold this file (a sheet kept outside the project folder, or the snapshot
        # failed): keep a copy of the original next to the snapshots, or refuse. A one-way format change
        # must never be the only copy.
        try:
            bdir = Path(pdir) / ".copper" / "format-originals"
            bdir.mkdir(parents=True, exist_ok=True)
            tag = hashlib.sha1(_norm(p).encode("utf-8", "replace")).hexdigest()[:8]
            b = bdir / f"{tag}-{stamp}-{os.path.basename(p)}"
            if not b.exists():
                shutil.copy2(p, b)
            note["backup"] = str(b)
        except OSError as e:
            raise ToolError(
                f"{rel} is in KiCad {frm} format and saving would upgrade it one-way, but no backup of the original "
                f"could be made ({e.strerror or e}). Nothing was changed.", code="FORMAT_UPGRADE_BLOCKED",
                fix="make the project folder writable (or copy the project somewhere writable) and retry") from e
    same = frm == v.series                       # e.g. a file saved by a 9.99 nightly: older stamp, same series name
    was = f"an older KiCad {frm} development format (stamp {stamp})" if same else f"KiCad {frm} format"
    lost = "KiCad builds older than this one may not open it" if same else f"KiCad {frm} can no longer open it"
    keep = "the checkpoint holds the original" if held else f"the original is kept at {note['backup']}"
    note["note"] = f"{rel} was in {was} and is saved in KiCad {v.series} format from now on ({lost}); {keep}."

    # The guard runs BEFORE the write, and the write may never happen (dry run, a failing op, a file that turns
    # out to be open in KiCad, a flow that stops earlier). So nothing is reported or recorded here: the upgrade
    # is confirmed against the file itself when the tool call ends (settle_pending).
    with _notes_lock:
        _pending[_norm(p)] = {"path": p, "stamp": stamp, "pdir": pdir, "rel": rel, "from": frm, "to": v.series,
                              "note": note}
    return {"format_upgraded": note}


_pending: dict[str, dict] = {}


def settle_pending() -> None:
    """Turn the format upgrades announced by :func:`guard_write` into notes and
    ``.copper/state.json`` entries, but only for files whose stamp on disk
    really is newer now. An upgrade that did not happen leaves no trace."""
    with _notes_lock:
        items = list(_pending.values())
        _pending.clear()
    for it in items:
        try:
            now = file_format(it["path"])["version"]
        except OSError:
            now = None
        if not now or now <= it["stamp"]:
            continue

        def fn(d, it=it):
            d.setdefault("format_upgraded", {})[it["rel"]] = {
                "from": it["from"], "to": it["to"], "stamp": it["stamp"],
                "checkpoint": it["note"].get("checkpoint"), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            if it["note"].get("backup"):
                d["format_upgraded"][it["rel"]]["backup"] = it["note"]["backup"]
        _pstate_update(it["pdir"], fn)
        add_note("format_upgraded", it["note"])


# ── known issues ──────────────────────────────────────────────────────────
_ISSUE = "https://gitlab.com/kicad/code/kicad/-/issues/"
KNOWN_ISSUES = [
    # (id, applies(KiVer) -> bool, severity, text, url)
    ("zone-fill-shorts", lambda v: (10, 0, 0) <= v.t < (10, 0, 7), "warn",
     "zone fills can short through-hole pins and other-net pads; fixed in KiCad 10.0.7", _ISSUE + "25141"),
    ("erc-json-scale", lambda v: v.t < (10, 0, 7), "info",
     "ERC JSON positions are 100x too small; Copper measures and corrects the scale", _ISSUE + "25582"),
    ("bom-preset-error", lambda v: (10, 0, 0) <= v.t < (10, 0, 8), "info",
     "spurious error from `sch export bom --preset`; fixed in KiCad 10.0.8", _ISSUE + "25834"),
    ("via-padstack-crash", lambda v: (10, 0, 0) <= v.t < (10, 0, 8), "info",
     "crash opening a board that has a via padstack; fixed in KiCad 10.0.8", _ISSUE + "25683"),
    ("swig-removed", lambda v: v.effective_major >= 11, "warn",
     "no pcbnew Python module (removed in KiCad 11): PCB edits need a KiCad 10.0.x installed alongside",
     "https://dev-docs.kicad.org/en/apis-and-binding/pcbnew/"),
    ("view-only", lambda v: (v.major, v.minor) < FLOOR, "warn",
     "older than KiCad 10: Copper can view and check designs with it but not edit them", DOWNLOAD_URL),
]


def advisories(v: KiVer | None) -> list[dict]:
    if v is None:
        return []
    return [{"id": i, "severity": sev, "text": text, "url": url} for i, fn, sev, text, url in KNOWN_ISSUES if fn(v)]


def advise_zone_fill(inst, board_path) -> str | None:
    """One line, once per project per server process, when DRC runs on a board
    with zones under a KiCad that has the zone-fill bug."""
    v = _ver(inst)
    if v is None or not any(a["id"] == "zone-fill-shorts" for a in advisories(v)):
        return None
    key = os.path.normcase(os.path.dirname(os.path.abspath(os.fspath(board_path))))
    if key in _advised:
        return None
    try:
        with open(board_path, "rb") as f:
            if b"(zone" not in f.read():
                return None
    except OSError:
        return None
    _advised.add(key)
    text = (f"KiCad {v} can fill zones so that they short through-hole pins or other-net pads (fixed in 10.0.7); "
            "update KiCad before ordering boards with copper pours.")
    add_note("kicad_advisory", text)
    return text


# ── newest release (doctor / CLI only; never on the MCP request path) ─────
TAGS_URL = "https://gitlab.com/api/v4/projects/kicad%2Fcode%2Fkicad/repository/tags?per_page=40&order_by=updated"
_TAG_RE = re.compile(r"^\d+\.\d+\.\d+$")


def copper_home() -> Path:
    return Path(os.environ.get("COPPER_HOME") or (Path.home() / ".copper"))


def _http_get(url: str, timeout: float) -> bytes:
    import urllib.request
    try:
        from .. import __version__ as _v
    except Exception:  # noqa: BLE001
        _v = "0"
    req = urllib.request.Request(url, headers={"User-Agent": f"copper-kicad/{_v}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # honours HTTPS_PROXY etc.
        return r.read()


def pick_latest(tags, max_major: int = SUPPORTED_MAX_MAJOR) -> tuple[KiVer | None, KiVer | None]:
    """``(newest supported stable, newest stable of a newer major or None)`` from tag names.
    Keeps only ``N.N.N`` (drops ``-rcN`` and four-part tags such as 9.0.9.1)."""
    vs = [parse_version(t) for t in tags if isinstance(t, str) and _TAG_RE.match(t)]
    vs = [v for v in vs if v and not v.nightly]
    ok = [v for v in vs if v.major <= max_major]
    newer = [v for v in vs if v.major > max_major]
    return (max(ok, key=lambda v: v.t) if ok else None, max(newer, key=lambda v: v.t) if newer else None)


def latest_release(timeout: float = 3.0, max_major: int = SUPPORTED_MAX_MAJOR, *, cached_only: bool = False,
                   detail: bool = False):
    """Newest stable KiCad with ``major <= max_major`` (or None: offline, opted
    out, unknown). One fixed GET with no user data; the answer is cached 24 h
    and a failure 1 h in ``$COPPER_HOME/cache/kicad-latest.json``.
    ``COPPER_NO_UPDATE_CHECK=1`` disables the request. ``detail=True`` returns
    ``{"latest": KiVer|None, "newer_major": KiVer|None}``."""
    def ret(d):
        lat = parse_version(d.get("latest")) if d.get("latest") else None
        nm = parse_version(d.get("newer_major")) if d.get("newer_major") else None
        return {"latest": lat, "newer_major": nm} if detail else lat
    f = copper_home() / "cache" / "kicad-latest.json"
    try:
        c = json.loads(f.read_text(encoding="utf-8"))
        age = time.time() - float(c.get("at", 0))
        if c.get("max_major") == max_major and 0 <= age < (86400 if c.get("ok") else 3600):
            return ret(c)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    if cached_only or os.environ.get("COPPER_NO_UPDATE_CHECK", "").strip() not in ("", "0"):
        return ret({})
    rec: dict = {"at": time.time(), "ok": False, "max_major": max_major}
    try:
        rows = json.loads(_http_get(TAGS_URL, timeout).decode("utf-8", "replace"))
        lat, nm = pick_latest([r.get("name") for r in rows if isinstance(r, dict)], max_major)
        rec.update(ok=lat is not None, latest=str(lat) if lat else None, newer_major=str(nm) if nm else None)
    except Exception:  # noqa: BLE001 — offline, proxy, bad JSON: no warning, no retry for an hour
        pass
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        os.replace(tmp, f)
    except OSError:
        pass
    return ret(rec)


# ── kicad-cli capabilities ────────────────────────────────────────────────
# Flags Copper passes that an older kicad-cli may lack: {command: {flag: number of values it takes}}.
# env.run() drops the ones the active binary does not list in its --help and says so in a note.
OPTIONAL_FLAGS: dict[str, dict[str, int]] = {
    "pcb drc": {"--refill-zones": 0, "--save-board": 0},
    "pcb export gerbers": {"--check-zones": 0},
    "pcb export pdf": {"--check-zones": 0},
    "pcb export odb": {"--check-zones": 0},
    "pcb export svg": {"--check-zones": 0, "--fit-page-to-board": 0},
}
DROP_NOTES = {
    "--refill-zones": "zones were not refilled before DRC",
    "--save-board": "the refilled board was not saved",
    "--check-zones": "zone fills were not re-checked before the export",
    "--fit-page-to-board": "the plot was not cropped to the board",
}


def _argv(inst) -> list[str]:
    a = getattr(inst, "cli_argv", None)
    return list(a) if a else [str(inst.cli)]


class Caps:
    """What one kicad-cli binary can do, learned from its ``--help`` output.

    * a subcommand exists only if its parent's ``{a,b,c}`` usage list names it
      (``kicad-cli <unknown> --help`` prints the parent help and exits 0);
    * flags are read from the command's own help text;
    * probes are lazy and persisted in ``cli-caps-<key>.json`` where the key
      covers the binary's path, size and mtime (an in-place KiCad update
      re-probes by itself);
    * if help cannot be parsed, ``has()`` answers True: a probe failure must
      never silently drop a flag such as ``--refill-zones``.
    """

    def __init__(self, cli, cache_dir, argv: list[str] | None = None):
        self.cli = str(cli)
        self.argv = list(argv) if argv else [self.cli]
        try:
            st = os.stat(self.cli)
            ident = f"{self.cli}|{st.st_size}|{st.st_mtime_ns}"
        except OSError:
            ident = f"{self.cli}|?"
        self.key = hashlib.sha1(ident.encode("utf-8", "replace")).hexdigest()[:16]
        self.file = Path(cache_dir) / f"cli-caps-{self.key}.json"
        self.spawns = 0
        self._lock = threading.Lock()
        try:
            d = json.loads(self.file.read_text(encoding="utf-8"))
            self.d = d if isinstance(d, dict) and isinstance(d.get("help", {}), dict) else {}
        except (OSError, ValueError):
            self.d = {}                       # missing or corrupt: rebuild
        self.d.setdefault("help", {})

    # persistence ---------------------------------------------------------
    def _save(self):
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_name(f"{self.file.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.d, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.file)
        except OSError:
            pass

    def extra(self, key: str):
        return self.d.get(key)

    def set_extra(self, key: str, value):
        with self._lock:
            self.d[key] = value
            self._save()

    # probing -------------------------------------------------------------
    def _run_help(self, parts: list[str]) -> tuple[int, str]:
        self.spawns += 1
        # Even `--help` makes kicad-cli create <config>/<ver>/ (kicad.json, kicad_common.json, colors/). A
        # capability probe must not create or re-save the user's KiCad settings (a config folder made by
        # Copper stops KiCad offering to import the previous version's settings on first start), so it
        # gets a throw-away config root.
        cfg = tempfile.mkdtemp(prefix="copper-kicad-probe-")
        try:
            r = subprocess.run([*self.argv, *parts, "--help"], capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
                               env=dict(os.environ, KICAD_CONFIG_HOME=cfg))
            return r.returncode, (r.stdout or "") + "\n" + (r.stderr or "")
        except (OSError, subprocess.SubprocessError):
            return -1, ""
        finally:
            shutil.rmtree(cfg, ignore_errors=True)

    def _help(self, cmd: str) -> dict:
        with self._lock:
            h = self.d["help"].get(cmd)
            if h is not None:
                return h
            code, txt = self._run_help(cmd.split())
            usage = re.search(r"(?im)^\s*usage:.*(?:\n[ \t]+\S.*)*", txt)
            utxt = usage.group(0) if usage else ""
            subs = re.search(r"\{([A-Za-z0-9_,-]+)\}", utxt or txt)
            flags = sorted(set(re.findall(r"(?<![\w-])(--[A-Za-z0-9][A-Za-z0-9-]*)", txt)))
            parsed = code == 0 and bool(utxt) and "Failed to parse" not in txt and "Unknown argument" not in txt
            h = {"ok": parsed, "flags": flags if parsed else [],
                 "subs": subs.group(1).split(",") if parsed and subs else []}
            if code != -1:                    # do not persist "could not even start the binary"
                self.d["help"][cmd] = h
                self._save()
            return h

    def subs(self, cmd: str) -> list[str]:
        return list(self._help(cmd)["subs"])

    def has(self, cmd: str, flag: str | None = None) -> bool:
        parts = cmd.split()
        for n in range(len(parts)):
            parent = self._help(" ".join(parts[:n]))
            if not parent["ok"]:
                return True                   # cannot tell: assume present, let KiCad report the real error
            if parts[n] not in parent["subs"]:
                return False
        if flag is None:
            return True
        h = self._help(cmd)
        return True if not h["ok"] else flag in h["flags"]

    def filter(self, cmd: str, args: list[str], optional: dict[str, int]) -> tuple[list[str], list[str]]:
        """Drop the ``optional`` flags (and their values) this binary lacks → ``(args, dropped)``."""
        out, dropped, i = [], [], 0
        while i < len(args):
            a = args[i]
            base = a.split("=", 1)[0]
            if base in optional and not self.has(cmd, base):
                dropped.append(base)
                i += 1 + (optional[base] if "=" not in a else 0)
                continue
            out.append(a)
            i += 1
        return out, dropped

    def count(self) -> int:
        """Leaf commands found so far (walks the whole tree: ~60 help calls cold, CLI/doctor use only)."""
        n, todo = 0, [""]
        while todo:
            c = todo.pop()
            s = self._help(c)["subs"]
            if not s and c:
                n += 1
            todo += [(c + " " + x).strip() for x in s]
        return n


_caps: dict[str, Caps] = {}


def caps(inst) -> Caps:
    """The (cached) capability probe of one install."""
    cli = str(inst.cli)
    c = _caps.get(cli)
    cache = copper_home() / "cache"
    if c is None or c.file.parent != cache:
        c = _caps[cli] = Caps(cli, cache, _argv(inst))
    return c


def command_of(args: list[str], table=OPTIONAL_FLAGS) -> str | None:
    """Longest leading command of ``args`` that has an entry in ``table``."""
    lead = []
    for a in args[:3]:
        if a.startswith("-"):
            break
        lead.append(a)
    for n in range(len(lead), 0, -1):
        c = " ".join(lead[:n])
        if c in table:
            return c
    return None


def variant_args(inst, cmd: str, variant: str | None) -> list[str]:
    """``["--variant", name]`` for a KiCad 10 design variant, when ``cmd`` (e.g. "sch export bom") of the
    active kicad-cli supports it. A variant is never dropped silently: exporting the default variant in
    its place would produce a wrong BOM, so an unsupported command is an error."""
    if not variant:
        return []
    if not caps(inst).has(cmd, "--variant"):
        raise ToolError(f"`kicad-cli {cmd}` of KiCad {_ver(inst) or '?'} has no --variant option.", code="NO_VARIANTS",
                        fix="design variants need KiCad 10; omit `variant` to export the default variant")
    return ["--variant", str(variant)]


def prepare_args(inst, args: list[str]) -> tuple[list[str], list[str]]:
    """The single pre-flight for every kicad-cli call: refuse input files the
    binary cannot read, and drop optional flags it does not have."""
    prev = ""
    for a in args:
        if prev not in ("-o", "--output") and a.lower().endswith(DESIGN_SUFFIXES) and os.path.isfile(a):
            check_readable(a, inst)
        prev = a
    cmd = command_of(args)
    if cmd is None:
        return args, []
    opt = OPTIONAL_FLAGS[cmd]
    if not any(a.split("=", 1)[0] in opt for a in args):
        return args, []
    out, dropped = caps(inst).filter(cmd, list(args), opt)
    if dropped:
        v = _ver(inst)
        add_note("kicad_notes", "; ".join(DROP_NOTES.get(f, f"{f} was not used") for f in dropped)
                 + f": KiCad {v or '?'} has no {', '.join(dropped)}")
    return out, dropped


# ── install selection ─────────────────────────────────────────────────────
def engine_available(inst) -> bool:
    """Can Copper edit boards with this install? Today that means KiCad's own
    Python with pcbnew (KiCad <= 10); KiCad 11 has neither."""
    py = getattr(inst, "python", None)
    v = _ver(inst)
    if v is not None and v.effective_major >= 11:
        return False
    try:
        return bool(py) and os.path.exists(py)
    except (OSError, TypeError):
        return False


def _reads(inst, project) -> bool:
    for kind, stamp in project or ():
        mx = max_stamp(inst, kind, need=stamp)
        if mx is not None and stamp > mx:
            return False
    return True


def select(installs: list, pin: str | None = None, project: tuple | None = None, allow_prerelease: bool = False,
           engine=engine_available):
    """Choose the install Copper uses → ``(install, human reason)``.

    1. ``pin`` (COPPER_KICAD_VERSION: a version prefix, or ``nightly``) wins.
    2. Stable installs of KiCad >= 10 that read the project's newest format:
       the newest one, preferring installs that can edit boards.
    3. Otherwise a pre-release that reads it ("project is in a pre-release format").
    4. No project: the newest stable >= 10.
    5. Only installs older than 10: the newest (view/verify only). Only a
       pre-release: that one (writes still pass the write guard).
    6. ``allow_prerelease`` lets pre-releases compete in 2 and 4.
    """
    cands = [i for i in installs if getattr(i, "cli", None) and _ver(i)]
    if not cands:
        cands = [i for i in installs if getattr(i, "cli", None)]
        if not cands:
            raise ToolError("KiCad was not found on this computer.", code="KICAD_NOT_FOUND",
                            fix=f"Install KiCad 10 from {DOWNLOAD_URL} (or set COPPER_KICAD_CLI to kicad-cli's path), "
                                "then run `copper doctor`.")
        return cands[0], "only install (version unknown)"

    def newest(lst, prefer_engine=True):
        return max(lst, key=lambda i: ((bool(engine(i)) if prefer_engine else False), newest_key(_ver(i))))

    note = ""
    if pin:
        p = pin.strip().lower()
        hit = [i for i in cands if (_ver(i).nightly if p == "nightly" else
                                    (str(_ver(i)) == p or str(_ver(i)).startswith(p + ".") or str(_ver(i)).startswith(p + "-")
                                     or _ver(i).series == p))]
        if hit:
            # "10" means KiCad 10, not the 10.99 nightly (which is KiCad 11 in the making): a prefix pin prefers a
            # release; a pre-release is chosen only when the pin names it (exact version, "nightly") or nothing else fits
            exact = [i for i in hit if str(_ver(i)) == p]
            hit = exact or [i for i in hit if not _ver(i).prerelease] or hit
            return newest(hit, False), f"pinned by COPPER_KICAD_VERSION={pin.strip()}"
        note = f" (COPPER_KICAD_VERSION={pin} matches no install)"
    current = [i for i in cands if (_ver(i).major, _ver(i).minor) >= FLOOR]
    stable = [i for i in current if not _ver(i).prerelease]
    pre = [i for i in current if _ver(i).prerelease]
    pool = stable + (pre if allow_prerelease else [])
    if project:
        ok = [i for i in pool if _reads(i, project)]
        if ok:
            best = newest(ok)
            why = "newest stable that reads the project" if not _ver(best).prerelease else "newest that reads the project (pre-releases allowed)"
            if len({_ver(i).series for i in ok}) > 1 and engine(best) and not engine(max(ok, key=lambda i: newest_key(_ver(i)))):
                why = "newest stable that reads the project and can edit boards"
            return best, why + note
        if any(series_of_stamp(kind, stamp) == "newer" for kind, stamp in project):
            ok = [i for i in pre if _reads(i, project)]
            if ok:
                return newest(ok, False), "project is in a pre-release format" + note
    if pool:
        best = newest(pool)
        why = "newest stable" if not _ver(best).prerelease else "newest (pre-releases allowed)"
        if engine(best) and not engine(max(pool, key=lambda i: newest_key(_ver(i)))):
            why = "newest stable that can edit boards"
        if project and len(pool) == 1 and len(cands) == 1:
            why = "only install"
        return best, why + note
    if pre:
        return newest(pre, False), ("only install" if len(cands) == 1 else "only pre-releases installed") + \
            " (pre-release: writes to stable-format files are blocked until allowed)" + note
    return newest(cands, False), ("only install" if len(cands) == 1 else "newest install") + \
        " (older than KiCad 10: view/verify only)" + note
