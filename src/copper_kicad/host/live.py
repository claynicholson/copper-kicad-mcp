"""Run engine tools for a live host: shadow checkout -> engine (unchanged) -> uuid patch -> host.apply.

    LiveSession(host, project_dir).call_tool(name, args, progress=None, summary=None) -> outcome dict

This is the ONE place that decides where an engine tool runs when an app owns the document:

    route         tools                                   what happens
    ------------  --------------------------------------  ---------------------------------------------------
    'schematic'   sch_*, library, parts_import,           host.checkout('schematic') once per document revision;
                  parts_source_bom                        the tool runs with project=<shadow_dir>; if it wrote,
                                                          sch/patch.diff_project -> host.apply (one undo step)
    'board'       pcb_*, board_build (writers)            file job: ask_permission, host.save, host.checkpoint,
                                                          tool on the real files (while_open='revert'), host.reload
    'disk'        everything else that takes `project`    runs on the project files as last saved
    'none'        tools without a `project` argument      runs as is
    'disk'        project_summary, verify,                read-only: run on the schematic shadow too (the live
     (live read)  design_review, render                   document, not the file as last saved), board = saved file
    'blocked'     project_create, project_open, focus,    refused: the app owns the project, pushes the focus, and
                  checkpoint                              owns undo

The engine never learns about the host: a shadow folder has no KiCad lock and no host of its own, so
``project.guard_writable`` passes there. Results are shaped exactly like the MCP server's
(compact.prepare_args -> registry.call_tool -> compact.to_wire).

Schematic tools run in LIVE MODE (sch/model.live_mode): no kicad-cli re-save of the shadow and no file ERC, because
the app re-serialises and verifies the real document itself. The netlist connectivity check of the tool and the
``expect.netlist_hash`` export still run on the shadow. ``COPPER_LIVE_CANON=1`` (or ``LiveSession.live_mode =
False``) turns live mode off: the shadow is then re-saved by kicad-cli and ERC runs on it, as for a file edit.

``sch_apply`` on a sheet that already has parts runs with ``layout='keep'`` unless the caller names a layout: the
engine's automatic re-draw of a sheet it arranged is for file workflows; in an open editor a small edit must stay
small (the user's eyes are on the sheet). An empty sheet is still laid out.

Engines outside copper-agent reach the same door through ``host/route.call_tool`` (the ``guard_writable`` hook).

outcome = {'ok': bool,
           'text': str,                 what a model reads as the tool result (already compacted)
           'data': dict | None,         the tool's full structured payload (in-process callers, commands)
           'images': [{'data': b64, 'mime': str}],
           'error': {'code', 'message', 'fix'?} | None,
           'proposal': dict | None,     host.apply's result plus {'document', 'summary', 'refs', 'region'}
           'route': str}
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from pathlib import Path

from ..sch import patch as schpatch
from .client import E_DENIED, E_NO_DOCUMENT, E_UNSUPPORTED, Host, HostError

BLOCKED = {"project_create": "the app owns the project: open or create projects from the app",
           "project_open": "the app owns the project: open or create projects from the app",
           "focus": "the focus is given with the user's message (the [focus] line); there is nothing to fetch",
           "checkpoint": "the editor's own undo history and the Keep / Undo bar replace file checkpoints while the "
                         "design is open (a restore would overwrite files under the editor)"}
# read-only tools that would otherwise read the files AS LAST SAVED: with a schematic open they read a shadow
# checkout of the live document instead (unsaved edits and changes still in review included)
SHADOW_READS = ("project_summary", "verify", "design_review", "render")
_BOARD_SUFFIX = (".kicad_pcb", ".kicad_dru")
_PLACED = re.compile(r"\(symbol\s*\(lib_id\b")       # a placed symbol; a library definition is (symbol "Lib:Name"
SCHEMATIC_EXTRA = ("library", "parts_import", "parts_source_bom")
BOARD_EXTRA = ("board_build",)
# side files an engine tool may create in a shadow project and that must follow the patch into the real project
_SIDE_NAMES = ("sym-lib-table", "fp-lib-table")
_SIDE_SUFFIX = (".kicad_sym", ".kicad_mod", ".step", ".stp", ".wrl")


def route_of(tool) -> str:
    """Where a registered engine tool runs when a live host owns the document (see the module docstring)."""
    if tool is None:
        return "none"
    if tool.name in BLOCKED:
        return "blocked"
    has_project = "project" in (tool.schema.get("properties") or {})
    if tool.name.startswith("sch_") or tool.name in SCHEMATIC_EXTRA:
        return "schematic"
    if (tool.name.startswith("pcb_") or tool.name in BOARD_EXTRA) and not tool.read_only:
        return "board"
    return "disk" if has_project else "none"


class _Checkout:
    def __init__(self, res: dict):
        self.dir = Path(res["shadow_dir"])
        from . import route
        route.note_shadow(self.dir)
        self.rev = res["rev"]
        self.sheets = [s for s in res.get("sheets") or [] if isinstance(s, dict) and s.get("file")]
        self.before = self.read_sheets()
        self.side = self.side_files()

    def sheet_files(self) -> list[str]:
        files = [s["file"] for s in self.sheets]
        return files or sorted(p.relative_to(self.dir).as_posix() for p in self.dir.glob("*.kicad_sch"))

    def read_sheets(self) -> dict:
        out = {}
        for rel in self.sheet_files():
            try:
                out[rel] = (self.dir / rel).read_text(encoding="utf-8")
            except OSError:
                pass
        for p in self.dir.rglob("*.kicad_sch"):                      # sheets the tool created
            rel = p.relative_to(self.dir).as_posix()
            if rel not in out and not rel.startswith((".copper/", "copper-out/")):
                out[rel] = p.read_text(encoding="utf-8")
        return out

    def has_parts(self, sheet=None) -> bool:
        """Does the sheet an edit targets (file or sheet name; default: the root sheet) already hold symbols?"""
        files = self.sheet_files()
        want = str(sheet or "").replace("\\", "/").strip("/").lower()
        pick = [f for f in files if want and want in (f.lower(), Path(f).name.lower(), Path(f).stem.lower())]
        if not pick and want:
            pick = [s["file"] for s in self.sheets
                    if str(s.get("sheet_path") or "").strip("/").lower().rsplit("/", 1)[-1] == want]
        for rel in (pick or (files if want else files[:1])):
            if _PLACED.search(self.before.get(rel) or ""):
                return True
        return False

    def borrow_board(self, project_dir) -> None:
        """A schematic checkout has no board file (HOST_PROTOCOL.md 5.1). Tools that read both (verify, the
        summary, renders) get the board as last saved, copied in once; it is never read back from here."""
        if not project_dir or any(self.dir.glob("*.kicad_pcb")):
            return
        try:
            for p in Path(project_dir).iterdir():
                if p.is_file() and p.suffix.lower() in _BOARD_SUFFIX and not (self.dir / p.name).exists():
                    shutil.copyfile(p, self.dir / p.name)
        except OSError:
            pass

    def side_files(self) -> dict:
        out = {}
        for p in self.dir.rglob("*"):
            rel = p.relative_to(self.dir).as_posix()
            if p.is_file() and (p.name in _SIDE_NAMES or p.suffix.lower() in _SIDE_SUFFIX) \
                    and not rel.startswith((".copper/", "copper-out/")):
                st = p.stat()
                out[rel] = (st.st_size, st.st_mtime_ns)
        return out


class LiveSession:
    """Per-agent state of the live documents: the current checkout of each and the newest rev the host announced."""

    def __init__(self, host: Host, project_dir=None):
        self.host = host
        self.project_dir = str(project_dir) if project_dir else None
        self.lock = threading.RLock()
        self.checkouts: dict = {}
        self.revs: dict = {}
        self.live_mode = os.environ.get("COPPER_LIVE_CANON", "0") != "1"

    # ── what the app tells us ─────────────────────────────────────────────
    def doc_changed(self, document: str, rev) -> None:
        """`doc.changed`: remember the newest rev; a checkout at another rev is stale and is dropped lazily."""
        self.revs[document] = rev            # no lock: called on the reader thread while a tool may hold it

    def invalidate(self, document: str | None = None) -> None:
        if document is None:
            self.checkouts.clear()
        else:
            self.checkouts.pop(document, None)

    def checkout(self, document: str = "schematic", host: Host | None = None) -> _Checkout:
        with self.lock:
            co = self.checkouts.get(document)
            if co is not None and self.revs.get(document) in (None, co.rev) and co.dir.is_dir():
                return co
            co = _Checkout((host or self.host).checkout(document))
            self.checkouts[document] = co
            self.revs[document] = co.rev
            return co

    # ── the door ──────────────────────────────────────────────────────────
    def call_tool(self, name: str, args: dict | None = None, progress=None, summary: str | None = None,
                  host: Host | None = None) -> dict:
        from ..mcp import registry
        from .. import tools as _tools  # noqa: F401  (registers the engine tools)
        host = host or self.host
        args = dict(args or {})
        tool = registry.TOOLS.get(name)
        route = route_of(tool)
        try:
            if route == "blocked":
                return _fail(route, "NOT_IN_APP", f"{name} is not available inside the app: {BLOCKED[name]}")
            if route == "schematic":
                return self._schematic(host, tool, args, progress, summary)
            if route == "board":
                return self._board(host, tool, args, progress)
            if route == "disk" and tool.read_only and tool.name in SHADOW_READS:
                return self._read_live(host, tool, args, progress)
            if route == "disk" and self.project_dir:
                args["project"] = self.project_dir
            return _outcome(route, _engine(name, args, self.project_dir, progress))
        except HostError as e:
            if e.code == "STALE_BASE":
                self.invalidate()
                return _fail(route, e.code, "the document changed while the edit was being prepared; nothing was applied",
                             "call the tool again: it will start from the document as it is now")
            return _fail(route, e.code, e.message, e.data.get("fix"))

    def _read_live(self, host, tool, args, progress):
        """A read-only project tool: on the shadow of the live schematic when the app has one open, else on the
        files. The board in that shadow is the one last saved (board changes are file jobs, saved before they run)."""
        with self.lock:
            try:
                co = self.checkout("schematic", host)
            except HostError as e:
                if e.code not in (E_NO_DOCUMENT, E_UNSUPPORTED):
                    raise
                co = None                                        # no schematic open in this app: the files are it
            if co is None:
                if self.project_dir:
                    args["project"] = self.project_dir
                return _outcome("disk", _engine(tool.name, args, self.project_dir, progress))
            co.borrow_board(self.project_dir)
            args["project"] = str(co.dir)
            return _outcome("disk", _engine(tool.name, args, str(co.dir), progress))

    def _schematic(self, host, tool, args, progress, summary):
        with self.lock:
            co = self.checkout("schematic", host)
            args["project"] = str(co.dir)
            if tool.name == "sch_apply" and args.get("layout") is None and co.has_parts(args.get("sheet")):
                # In a file workflow the engine re-draws a sheet it arranged itself after every batch. In an open
                # editor that turns "add one capacitor" into dozens of moved items under the user's eyes: an edit
                # of a sheet that already has parts changes what was asked and nothing else (/tidy re-draws).
                args["layout"] = "keep"
            from ..sch import model as schmodel
            with schmodel.live_mode(self.live_mode):
                result = _engine(tool.name, args, str(co.dir), progress)
            out = _outcome("schematic", result)
            if tool.read_only or args.get("dry_run"):
                return out
            after = co.read_sheets()
            if not out["ok"]:
                # A tool that failed AFTER it wrote (a crash in layout or render, a late refusal) leaves an edit in
                # the shadow that the document does not have. That shadow must not be the base of the next patch.
                if after != co.before or co.side_files() != co.side:
                    self.checkouts.pop("schematic", None)
                return out
            info = schpatch.diff_project(co.before, after, co.sheets)
            side_now = co.side_files()
            files = [{"path": rel, "shadow_path": str(co.dir / rel)} for rel, sig in sorted(side_now.items())
                     if co.side.get(rel) != sig]
            # a sheet file the tool created cannot be a patch (there is nothing to patch): it travels as a file
            files += [{"path": rel, "shadow_path": str(co.dir / rel)} for rel in info["new_sheets"]]
            if info["empty"] and not files:
                co.before, co.side = after, side_now
                return out
            expect = _expect(co.dir)
            data = out.get("data") or {}
            text = summary or _summary(tool.name, data, info)
            try:
                res = host.apply(base_rev=co.rev, document="schematic", patches=info["patches"],
                                 lib_symbols=info["lib_symbols"], files=files, summary=text, expect=expect)
            except BaseException:                             # HostError, or Cancelled while waiting
                self.checkouts.pop("schematic", None)        # the shadow now holds an edit the document does not
                raise
            co.before, co.side = after, side_now
            if isinstance(res.get("rev"), int):
                co.rev = res["rev"]
                self.revs["schematic"] = co.rev
            else:
                self.checkouts.pop("schematic", None)
            out["proposal"] = dict(res, document="schematic", summary=text, refs=info["refs"], region=info["region"])
            out["proposal"].setdefault("counts", info["counts"])
            if info["skipped"]:
                out["proposal"]["skipped"] = info["skipped"]
            live = {k: res[k] for k in ("proposal_id", "state", "netlist_ok") if k in res}
            new = [f for f in res.get("erc_new") or [] if isinstance(f, dict)]
            live["erc_new"] = len(res.get("erc_new") or [])
            if new:                                           # what the app's own ERC found, so the model can fix it
                live["findings"] = [_brief(f) for f in new[:_FINDINGS_MAX]]
                if len(new) > _FINDINGS_MAX:
                    live["findings_more"] = len(new) - _FINDINGS_MAX
            if info["skipped"]:                               # e.g. a paper size the patch format cannot carry
                live["not_applied"] = [str(x)[:120] for x in info["skipped"][:6]]
            if res.get("netlist_ok") is False:
                live["warning"] = "the document's netlist differs from the one this edit computed; check the result"
            out["text"] += "\n" + json.dumps({"live": live}, separators=(",", ":"))
            return out

    def _board(self, host, tool, args, progress):
        if not self.project_dir:
            return _fail("board", "NO_PROJECT", "no project is open")
        label = tool.title or tool.name
        ans = host.ask_permission("file_job", {"tool": tool.name, "document": "board", "label": label,
                                               "note": "runs on the saved board file; undo is Restore checkpoint"})
        if not ans.get("granted"):
            raise HostError(E_DENIED, f"the user declined to run {tool.name} on the board file")
        for doc in ("schematic", "board"):
            try:
                host.save(doc)
            except HostError as e:
                if e.code not in (E_NO_DOCUMENT, E_UNSUPPORTED) or doc == "board":
                    raise
        cp = {}
        try:
            cp = host.checkpoint(label, "board")
        except HostError as e:
            if e.code != E_UNSUPPORTED:
                raise
        args["project"] = self.project_dir
        if "while_open" in (tool.schema.get("properties") or {}):
            args.setdefault("while_open", "revert")
        try:
            out = _outcome("board", _engine(tool.name, args, self.project_dir, progress))
        finally:
            res = host.reload("board")
            self.invalidate("board")
            if isinstance(res.get("rev"), int):
                self.revs["board"] = res["rev"]
        if cp.get("id"):
            out["checkpoint"] = cp["id"]
            out["text"] += "\n" + json.dumps({"live": {"checkpoint": cp["id"], "undo": "restore checkpoint"}},
                                             separators=(",", ":"))
        return out


# ── helpers ───────────────────────────────────────────────────────────────
def _engine(name: str, args: dict, project_dir, progress) -> dict:
    """One engine tool call, shaped like the MCP server shapes it. Never raises."""
    from ..mcp import compact, registry
    tool = registry.TOOLS.get(name)
    from . import route
    try:
        run_args, defaulted = compact.prepare_args(tool, args)
        with route.inside():                                  # this IS the routed call: no second redirect
            raw = registry.call_tool(name, run_args, progress)
        wire = compact.to_wire(raw, project_dir, tool=name, args=args, defaulted=defaulted)
    except (Exception, SystemExit) as e:  # noqa: BLE001 — SystemExit: a tool or library that calls sys.exit()
        raw = wire = {"content": [{"type": "text", "text": f"internal error: {type(e).__name__}: {e}"}], "isError": True}
    return {"raw": raw, "wire": wire}


def _outcome(route: str, res: dict) -> dict:
    raw, wire = res["raw"], res["wire"]
    text = "\n".join(c.get("text", "") for c in wire.get("content") or [] if c.get("type") == "text")
    images = [{"data": c.get("data"), "mime": c.get("mimeType", "image/png")}
              for c in wire.get("content") or [] if c.get("type") == "image"]
    data = raw.get("structuredContent") if isinstance(raw.get("structuredContent"), dict) else None
    ok = not wire.get("isError")
    err = None
    if not ok:
        d = data or {}
        err = {"code": str(d.get("error") or "ERROR"), "message": str(d.get("message") or text[:400])}
        if d.get("fix"):
            err["fix"] = str(d["fix"])
    return {"ok": ok, "text": text, "data": data, "images": images, "error": err, "proposal": None, "route": route}


def _fail(route: str, code: str, message: str, fix: str | None = None) -> dict:
    err = {"code": code, "message": message}
    if fix:
        err["fix"] = fix
    return {"ok": False, "text": json.dumps(dict({"error": code, "message": message}, **({"fix": fix} if fix else {})),
                                             ensure_ascii=False),
            "data": None, "images": [], "error": err, "proposal": None, "route": route}


_FINDINGS_MAX = 8


def _brief(f: dict) -> dict:
    """One host finding, cut down to what a model needs to repair it (and to a bounded size)."""
    out = {}
    for k in ("severity", "type", "message", "at", "refs", "pins", "net", "fix"):
        v = f.get(k)
        if v in (None, "", [], {}):
            continue
        out[k] = v[:200] if isinstance(v, str) else v[:12] if isinstance(v, list) else v
    return out


def _summary(name: str, data: dict, info: dict) -> str:
    changed = [c for c in (data.get("changed") or []) if isinstance(c, str)]
    if changed:
        return changed[0][:100] + (f" (+{len(changed) - 1} more)" if len(changed) > 1 else "")
    c = info["counts"]
    return f"{name}: {c['added']} added, {c['changed']} changed, {c['removed']} removed"


def _expect(shadow_dir: Path) -> dict | None:
    """The netlist the edited shadow has, for the host to compare with the live document after the commit.
    None when it cannot be exported (the host then reports netlist_ok: null). COPPER_LIVE_EXPECT=0 skips it."""
    if os.environ.get("COPPER_LIVE_EXPECT", "1") == "0":
        return None
    try:
        from .. import project as P
        from ..sch import netlist
        return schpatch.expect(netlist.export_netlist(P.resolve(str(shadow_dir), activate=False)))
    except Exception:  # noqa: BLE001
        return None
