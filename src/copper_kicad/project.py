"""Project resolution, active-project state, atomic writes, lock detection
and checkpoints. Every tool that touches a design goes through here."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .mcp.registry import ToolError

STATE_DIR = Path(os.environ.get("COPPER_HOME", Path.home() / ".copper"))
STATE_FILE = STATE_DIR / "state.json"


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(d: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(d, indent=1), encoding="utf-8")


@dataclass
class Project:
    dir: Path
    name: str

    @property
    def pro(self) -> Path:
        return self.dir / f"{self.name}.kicad_pro"

    @property
    def sch(self) -> Path:
        return self.dir / f"{self.name}.kicad_sch"

    @property
    def pcb(self) -> Path:
        return self.dir / f"{self.name}.kicad_pcb"

    @property
    def copper_dir(self) -> Path:
        d = self.dir / ".copper"
        d.mkdir(exist_ok=True)
        return d

    @property
    def out_dir(self) -> Path:
        d = self.dir / "copper-out"
        d.mkdir(exist_ok=True)
        return d

    def sheets(self) -> list[Path]:
        """Root sheet + every sub-sheet file referenced (recursively)."""
        from . import sexpr
        out, todo, seen = [], [self.sch], set()
        while todo:
            f = todo.pop(0)
            if not f.exists() or str(f).lower() in seen:
                continue
            seen.add(str(f).lower())
            out.append(f)
            try:
                tree = sexpr.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for sh in sexpr.find_all(tree, "sheet"):
                for p in sexpr.find_all(sh, "property"):
                    if len(p) > 2 and p[1] in ("Sheetfile", "Sheet file"):
                        todo.append(f.parent / str(p[2]))
        return out

    def to_dict(self) -> dict:
        return {"name": self.name, "dir": str(self.dir), "schematic": str(self.sch) if self.sch.exists() else None,
                "pcb": str(self.pcb) if self.pcb.exists() else None}


def resolve(project: str | None = None, must_exist: bool = True, activate: bool | None = None,
            format_check: bool = True) -> Project:
    """Accepts a project dir, .kicad_pro/.kicad_sch/.kicad_pcb path, or None
    (→ the active project).

    A per-call `project` argument does NOT change the active project (a read
    of another design must never redirect the next fab_export/verify) — except
    when no usable project is active yet, so the first call still "opens" it.
    project_open / project_create pass activate=True.

    Every resolve also runs the KiCad format preflight (format_preflight):
    format_check=False reports instead of refusing (status / open / checkpoint)."""
    pr = _resolve(project, must_exist)
    format_preflight(pr, check=format_check)
    if activate or (activate is None and project and not _active_ok()):
        _remember(pr)
    return pr


def activate(pr: Project) -> Project:
    return _remember(pr)


def format_preflight(pr: Project, check: bool = True) -> dict | None:
    """Tell the KiCad selector which file formats this project is in (so the
    install that reads it becomes the active one for this call), then refuse
    — or with check=False return as a dict — any sheet or board written by a
    KiCad newer than the active one reads. kicad-cli silently DROPS a too-new
    sub-sheet from netlists and ERC, so this must run before anything else."""
    try:
        from .kicad import compat, env
        stamps = compat.project_stamps(pr)            # one walk: root sheet, every sub-sheet, the board
        env.set_project_format(compat.project_format(pr, stamps))
        compat.check_project(pr, env.kicad(), stamps)
    except ToolError as e:
        if e.code != "FORMAT_TOO_NEW":
            return None                      # e.g. KiCad not installed: not this check's business
        if check:
            raise
        return e.to_dict()
    except Exception:  # noqa: BLE001 — a preflight must never be the reason a project cannot be resolved
        return None
    return None


def _active_ok() -> bool:
    a = _state().get("active")
    return bool(a) and Path(a).parent.is_dir()


def is_active(pr: Project) -> bool:
    a = _state().get("active")
    return bool(a) and Path(a) == pr.dir / f"{pr.name}.kicad_pro"


def _resolve(project: str | None, must_exist: bool) -> Project:
    if not project:
        st = _state()
        project = st.get("active")
        if not project:
            raise ToolError("No project selected.", code="NO_PROJECT",
                            fix="project_open(path=...) for an existing project, or project_create(name=..., dir=...) for a new one")
    p = Path(os.path.expandvars(os.path.expanduser(str(project)))).resolve()
    if p.is_dir():
        pros = sorted(p.glob("*.kicad_pro"))
        if not pros:
            schs = sorted(p.glob("*.kicad_sch"))
            if schs:
                return Project(p, schs[0].stem)
            raise ToolError(f"no .kicad_pro in {p}", code="NO_PROJECT",
                            fix="project_create(name=..., dir=...) to start one here")
        return Project(p, pros[0].stem)
    if p.suffix in (".kicad_pro", ".kicad_sch", ".kicad_pcb", ".kicad_prl"):
        if must_exist and not p.exists() and p.suffix != ".kicad_pro":
            raise ToolError(f"file not found: {p}", code="NOT_FOUND")
        return Project(p.parent, p.stem)
    raise ToolError(f"not a KiCad project path: {p}", code="BAD_PATH",
                    fix="pass a project folder or a .kicad_pro / .kicad_sch / .kicad_pcb file")


def _remember(pr: Project) -> Project:
    st = _state()
    if st.get("active") != str(pr.dir / f"{pr.name}.kicad_pro"):
        st["active"] = str(pr.dir / f"{pr.name}.kicad_pro")
        recent = [r for r in st.get("recent", []) if r != st["active"]]
        st["recent"] = [st["active"]] + recent[:9]
        try:
            _save_state(st)
        except OSError:
            pass
    return pr


def recent() -> list[str]:
    return _state().get("recent", [])


# ── file safety ──────────────────────────────────────────────────────────
def revision(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def lock_file(path: Path) -> Path | None:
    """KiCad writes ~<name>.<ext>.lck next to an open document."""
    p = Path(path)
    for cand in (p.parent / f"~{p.name}.lck", p.parent / f".~lock.{p.name}#", p.parent / f"{p.name}.lck"):
        if cand.exists():
            return cand
    return None


def is_open_in_kicad(path: Path) -> bool:
    lk = lock_file(path)
    if not lk:
        return False
    # stale locks (KiCad crashed) older than 3 days are ignored
    try:
        return time.time() - lk.stat().st_mtime < 3 * 86400
    except OSError:
        return False


def lock_owner(lk: Path | None) -> str:
    """'user@HOST' from KiCad's JSON lock file (best effort)."""
    if not lk:
        return ""
    try:
        d = json.loads(Path(lk).read_text(encoding="utf-8", errors="replace") or "{}")
    except (OSError, ValueError):
        return ""
    if not isinstance(d, dict):
        return ""
    user, host = str(d.get("username") or "").strip(), str(d.get("hostname") or "").strip()
    return f"{user}@{host}" if user and host else (user or host)


def _lock_desc(lk: Path) -> str:
    who = lock_owner(lk)
    try:
        age_h = (time.time() - lk.stat().st_mtime) / 3600
    except OSError:
        age_h = 0.0
    bits = [f"lock {lk.name}"]
    if who:
        bits.append(f"held by {who}")
    bits.append(f"{age_h:.1f} h old" if age_h >= 1 else "fresh")
    return ", ".join(bits)


def live_host(path) -> dict | None:
    """The live host registered for the project that holds ``path`` (host/route.registered_for): an app has the
    document open and applies edits itself (docs/internal/HOST_PROTOCOL.md). None for a plain project, for a shadow
    checkout, and while the live route itself is running the tool."""
    try:
        from .host import route
        return route.registered_for(path)
    except Exception:  # noqa: BLE001  (the guard must never fail because of the host lookup)
        return None


def guard_writable(path: Path, while_open: str = "refuse", *, format_check: bool = True):
    """Refuse to clobber a document open in the KiCad GUI (KiCad 9/10 have no
    live schematic API and would overwrite our edits on save), and apply the
    file-format policy (kicad/compat.guard_write): never save over a file the
    active KiCad cannot read, never silently move a file to a pre-release
    format, snapshot once before an old design is upgraded."""
    p = Path(path)
    if format_check and p.suffix in (".kicad_sch", ".kicad_pcb"):
        from .kicad import compat, env
        try:
            inst = env.kicad()
        except ToolError:
            inst = None
        if inst is not None:
            compat.guard_write(p, inst)
    lk = lock_file(p) if is_open_in_kicad(p) else None
    if lk is None and p.suffix == ".kicad_sch":
        # eeschema locks only the root sheet; a sub-sheet is open if any root next to it is
        for cand in p.parent.glob("~*.kicad_sch.lck"):
            try:
                if time.time() - cand.stat().st_mtime < 3 * 86400:
                    lk = cand
                    break
            except OSError:
                pass
    if while_open == "refuse":
        live = live_host(p)
        if live is not None:
            # An app with a live host owns this document: the edit is not refused for good, it is ROUTED. The
            # tool-call boundary (host/route.call_tool, or copper-agent's LiveSession) runs the same tool on a
            # shadow checkout and lands the result in the open document as one undoable change.
            raise ToolError(
                f"{p.name} is open in the app, which takes edits through its live host as one undoable change; "
                f"this call tried to write the file directly.", code="LIVE_HOST",
                fix="run the tool through the live route (copper_kicad.host.route.call_tool does it; so does the "
                    "app's own Copper pane). Nothing was written.",
                data={"live_host": live, "retry": "live"})
    if while_open == "refuse" and lk is not None:
        raise ToolError(
            f"{p.name} is open in KiCad ({_lock_desc(lk)}). Editing it now would be overwritten when KiCad saves.",
            code="FILE_OPEN_IN_KICAD",
            fix=("Save & close that editor in KiCad, then retry — or retry with while_open='revert' and use File ▸ Revert in KiCad "
                 f"afterwards. If KiCad is not running (stale lock after a crash), delete {lk}."))


def atomic_write(path: Path, text: str, base_revision: str | None = None):
    path = Path(path)
    if base_revision and path.exists() and revision(path) != base_revision:
        raise ToolError(f"{path.name} changed since revision {base_revision} (now {revision(path)})",
                        code="STALE_REVISION", fix="re-read the design and retry with the new revision")
    try:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    except OSError as e:
        raise ToolError(f"could not write {path.name}: {e.strerror or e}", code="WRITE_FAILED",
                        fix="the project folder is not writable — copy it somewhere writable and project_open the copy") from e
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError as e:
                if attempt == 4:
                    raise ToolError(f"could not write {path.name}: {e}", code="WRITE_FAILED",
                                    fix="close any program holding the file (or clear its read-only flag) and retry") from e
                time.sleep(0.2 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ── checkpoints ──────────────────────────────────────────────────────────
DESIGN_EXT = (".kicad_pro", ".kicad_sch", ".kicad_pcb", ".kicad_dru", "sym-lib-table", "fp-lib-table")


def _long(p: Path) -> str:
    """Windows: extended-length path so snapshots deep in long project paths
    never hit MAX_PATH (260)."""
    s = os.path.abspath(str(p))
    if os.name == "nt" and len(s) >= 240 and not s.startswith("\\\\?\\"):
        return "\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s
    return s


def _design_files(d: Path) -> list[Path]:
    """Design files of the project folder, plus every sub-sheet its schematics
    reference that is kept in a SUB-FOLDER of the project (``sch/power.kicad_sch``):
    an edit or a format upgrade of such a sheet must be as restorable as one of
    the root sheet. Sheets outside the project folder are not snapshotted."""
    top = sorted(f for f in d.iterdir() if f.is_file() and (f.suffix in DESIGN_EXT or f.name in DESIGN_EXT))
    out, seen = list(top), {os.path.normcase(os.path.abspath(f)) for f in top}
    try:
        from .kicad import compat
        base = os.path.abspath(d)
        extra = []
        for root in top:
            if root.suffix != ".kicad_sch":
                continue
            for f in compat._sheet_tree(root):
                k = os.path.normcase(os.path.abspath(f))
                rel = os.path.relpath(os.path.abspath(f), base)
                if k in seen or rel.startswith("..") or os.path.isabs(rel) or rel.split(os.sep)[0] == ".copper":
                    continue
                seen.add(k)
                extra.append(Path(base) / rel)
        out += sorted(extra)
    except Exception:  # noqa: BLE001 — a snapshot of the top-level files is better than none
        pass
    return out


def _rel(d: Path, f: Path) -> str:
    """Snapshot-relative name of a design file: 'board.kicad_sch', 'sch/power.kicad_sch'."""
    return os.path.relpath(os.path.abspath(f), os.path.abspath(d)).replace(os.sep, "/")


def snapshot_files(snap: Path) -> list[str]:
    """Relative names ('/'-separated) of every file in one snapshot folder, sub-folders included."""
    base = _long(snap)
    out = []
    for root, _dirs, names in os.walk(base):
        for n in names:
            out.append(os.path.relpath(os.path.join(root, n), base).replace(os.sep, "/"))
    return sorted(out)


def design_files(pr: "Project") -> list[str]:
    """Relative names of the files a checkpoint of ``pr`` would hold right now."""
    return sorted(_rel(pr.dir, f) for f in _design_files(pr.dir))


def _same_as(snap: Path, files: list[Path], d: Path | None = None) -> bool:
    try:
        d = d or (files[0].parent if files else snap)
        rels = sorted(_rel(d, f) for f in files)
        if snapshot_files(snap) != rels:
            return False
        return all(Path(_long(snap / _rel(d, f))).read_bytes() == f.read_bytes() for f in files)
    except OSError:
        return False


def checkpoint(pr: Project, label: str = "", dedupe: bool = True) -> str:
    """Snapshot design files to .copper/snapshots/<ts>-<label>. Cheap (text files).

    dedupe=True (every internal safety snapshot): when the newest snapshot is
    byte-identical to the current files, return its id instead of writing
    another copy — so one tool call that snapshots several times, or a
    parts_import that changes no design file, adds no clutter."""
    from .host import route
    if route.is_shadow(pr.dir):
        return ""        # a live host's shadow checkout: a throwaway copy whose undo is the app's (host/live.py)
    root = pr.copper_dir / "snapshots"
    files = _design_files(pr.dir)
    if dedupe and root.is_dir():
        snaps = sorted(root.iterdir())
        if snaps and _same_as(snaps[-1], files, pr.dir):
            return snaps[-1].name
    ts = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40]
    dest = root / (f"{ts}-{safe}" if safe else ts)
    n = 1
    base = dest
    while dest.exists():
        n += 1
        dest = Path(f"{base}-{n}")
    os.makedirs(_long(dest))
    try:
        for f in files:
            to = dest / _rel(pr.dir, f)
            if to.parent != dest:
                os.makedirs(_long(to.parent), exist_ok=True)
            shutil.copy2(_long(f), _long(to))
    except OSError as e:
        shutil.rmtree(_long(dest), ignore_errors=True)       # never leave a partial snapshot behind
        raise ToolError(f"could not snapshot {pr.name}: {e.strerror or e} ({getattr(e, 'filename', '') or dest})",
                        code="CHECKPOINT_FAILED",
                        fix="free disk space / close programs locking the project files, or move the project to a shorter path") from e
    # prune: keep newest 60
    snaps = sorted(root.iterdir())
    for old in snaps[:-60]:
        shutil.rmtree(_long(old), ignore_errors=True)
    return dest.name


def list_checkpoints(pr: Project) -> list[dict]:
    d = pr.copper_dir / "snapshots"
    if not d.exists():
        return []
    return [{"id": s.name, "files": snapshot_files(s)} for s in sorted(d.iterdir(), reverse=True)]


def restore_checkpoint(pr: Project, cid: str) -> list[str]:
    src = pr.copper_dir / "snapshots" / str(cid)
    if not cid or not os.path.isdir(_long(src)) or src.parent != pr.copper_dir / "snapshots":
        raise ToolError(f"no checkpoint '{cid}'", code="NOT_FOUND", fix="checkpoint(action='list')")
    files = snapshot_files(src)
    if not files:
        raise ToolError(f"checkpoint '{cid}' is empty (an interrupted snapshot)", code="NOT_FOUND",
                        fix="checkpoint(action='list') and pick another id")
    for f in files:  # check everything first so a restore never stops half-way
        guard_writable(pr.dir / f, format_check=False)
    guard_writable(pr.sch, format_check=False)  # KiCad locks only the root sheet
    # short label: the id is already in the snapshot list, repeating it doubled the path length (MAX_PATH)
    before = checkpoint(pr, "before-restore")
    restored = []
    try:
        for f in files:
            to = pr.dir / f
            if to.parent != pr.dir:
                os.makedirs(_long(to.parent), exist_ok=True)
            shutil.copy2(_long(src / f), _long(to))
            restored.append(f)
    except OSError as e:
        raise ToolError(f"restore of {cid} failed after {restored}: {e.strerror or e}", code="RESTORE_FAILED",
                        fix=f"checkpoint(action='restore', id='{before}') puts back the state from just before this restore") from e
    return restored
