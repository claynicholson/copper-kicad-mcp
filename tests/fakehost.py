"""A stand-in for the Copper app (the C++ side of docs/internal/HOST_PROTOCOL.md), for tests without a compiled fork.

    host = FakeHost(project_dir, workdir).start()
    host.hello()
    events = host.run_command("tool", {"tool": "sch_apply", "arguments": {...}})
    host.undo(); host.close()

What it models:
  * the "open document" is a COPY of ``project_dir`` under ``workdir/live`` (the caller's project is never touched),
    re-saved in the installed KiCad's file format when it is older, as opening a project in the app does (the app
    serialises shadows and parses patches in the current format only; ``upgrade=False`` turns this off);
    ``rev`` is an integer per document, bumped on every change;
  * ``host.checkout`` copies the live files to ``live/.copper/shadow/<rev>/`` and lists the sheets;
  * ``host.apply`` refuses on a wrong ``base_rev`` (STALE_BASE) or when ``busy`` (BUSY), applies every patch in
    memory first (sch/patch.apply_sheet) and writes only if all of them applied: one apply = one undo entry;
    with ``expect.netlist_hash`` it exports the netlist of the result with kicad-cli and reports ``netlist_ok``;
  * ``host.save`` / ``host.reload`` / ``host.checkpoint`` / ``host.reveal`` / ``host.verify`` / ``host.ask_permission``;
  * the reader: when the agent's stdout ends while a turn is open it SYNTHESISES ``error`` + ``done`` for that turn,
    exactly as the app's RPC layer must (a dead sidecar never leaves the pane waiting).

Everything that crosses the pipe is recorded in ``host.transcript`` ({'t','dir','msg'}; dir is 'app>agent' or
'agent>app') and can be written with ``save_transcript(path)`` as JSON lines. Lines from the agent that are not
JSON-RPC objects are kept in ``host.garbage`` (there must be none).

Knobs for failure tests: ``busy``, ``permission``, ``mute`` (methods never answered), ``delay`` (seconds per
method), ``fail`` ({method: (code, message)}), ``edit_after_checkout`` (the "user" edits during the turn).
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SKIP_DIRS = {".copper", "copper-out", ".git", ".history", "__pycache__"}
PROTOCOL = 1


class RpcFailure(Exception):
    """The agent answered a request with a JSON-RPC error."""

    def __init__(self, error: dict):
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        super().__init__(f"{error.get('code')} {data.get('code')}: {error.get('message')}")
        self.rpc_code = error.get("code")
        self.code = data.get("code")
        self.message = error.get("message")
        self.data = data


class HostRefusal(Exception):
    def __init__(self, code: str, message: str, **data):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def long_path(p) -> str:
    """A path the Windows file API accepts beyond 260 characters (demo libraries have 100-character file names
    and a shadow lives several folders deep). Elsewhere: the path as is."""
    s = os.path.abspath(str(p))
    if sys.platform == "win32" and not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + s
    return s


def copy_project(src: Path, dst: Path) -> Path:
    def ignore(d, names):
        return [n for n in names if n in SKIP_DIRS or n.endswith(("-backups", ".lck")) or n.startswith("~")]
    shutil.copytree(long_path(src), long_path(dst), ignore=ignore)
    return Path(dst)


def upgrade_sheets(d: Path) -> list[str]:
    """Bring every sheet file of a project copy to the installed KiCad's file format, as opening it in the app
    does: the app holds the document in memory and writes shadows (and takes patches) in the CURRENT format only.
    -> the files that were re-saved."""
    from copper_kicad.kicad import env
    from copper_kicad.sch import patch as schpatch
    done = []
    for f in sorted(Path(d).rglob("*.kicad_sch")):
        if ".copper" in f.relative_to(d).parts:
            continue
        try:
            with open(long_path(f), "r", encoding="utf-8") as fh:
                head = fh.read(400)
        except OSError:
            continue
        v = head.partition("(version ")[2].partition(")")[0].strip()
        if v.isdigit() and int(v) < schpatch.FILE_VERSION:
            r = env.run(["sch", "upgrade", "--force", str(f)], timeout=300)
            if r.returncode != 0:
                raise RuntimeError(f"kicad-cli could not upgrade {f.name}: {(r.stderr or r.stdout)[-300:]}")
            done.append(f.relative_to(d).as_posix())
    return done


class FakeHost:
    def __init__(self, project_dir, workdir, *, env: dict | None = None, plugins=(), check_netlist: bool = True,
                 app_version: str = "fakehost/1", upgrade: bool = True):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.live = copy_project(Path(project_dir), self.workdir / "live") if project_dir else None
        self.upgraded = upgrade_sheets(self.live) if self.live is not None and upgrade else []
        self.env = dict(os.environ, PYTHONIOENCODING="utf-8", **(env or {}))
        if plugins:
            self.env["COPPER_AGENT_PLUGINS"] = os.pathsep.join(str(p) for p in plugins)
        self.check_netlist = check_netlist
        self.app_version = app_version
        self.rev = {"schematic": 1, "board": 1}
        self.undo_stack: list[dict] = []
        self.proposals: dict = {}
        self.checkpoints: list[str] = []
        self.transcript: list[dict] = []
        self.garbage: list[str] = []
        self.stderr: list[str] = []
        self.events: dict = {}
        self.started: dict = {}                  # turn id -> request method
        self.calls: list[tuple] = []             # host.* requests received: (method, params)
        self.cancelled_requests: list[dict] = []
        self.reveals: list[dict] = []
        self.busy = False
        self.permission = {"granted": True, "remember": False}
        self.mute: set = set()
        self.delay: dict = {}
        self.fail: dict = {}
        self.edit_after_checkout = False
        self.eof = threading.Event()
        self.p = None
        self._t0 = time.time()
        self._lock = threading.Lock()
        self._wlock = threading.Lock()
        self._next = 0
        self._answers: dict = {}                 # request id -> Queue
        self._methods: dict = {}                 # request id -> method
        self._orphans: queue.Queue = queue.Queue()      # answers with id null (parse errors)
        self._cv = threading.Condition()

    # ── process ───────────────────────────────────────────────────────────
    def start(self) -> "FakeHost":
        self.p = subprocess.Popen([sys.executable, "-m", "copper_kicad.agent"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(SRC), env=self.env, bufsize=0)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        return self

    def _note(self, direction: str, msg) -> None:
        with self._lock:
            self.transcript.append({"t": round(time.time() - self._t0, 3), "dir": direction, "msg": msg})

    def send_raw(self, text: str) -> None:
        self._note("app>agent", text)
        with self._wlock:
            self.p.stdin.write((text + "\n").encode("utf-8"))
            self.p.stdin.flush()

    def _send(self, msg: dict) -> None:
        self._note("app>agent", msg)
        with self._wlock:
            try:
                self.p.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
                self.p.stdin.flush()
            except (OSError, ValueError):
                pass

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request_async(self, method: str, params: dict | None = None) -> int:
        with self._lock:
            self._next += 1
            mid = self._next
            self._answers[mid] = queue.Queue()
            self._methods[mid] = method
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        return mid

    def answer(self, mid: int, timeout: float = 30) -> dict:
        try:
            msg = self._answers[mid].get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"no answer to request {mid} ({self._methods.get(mid)}) in {timeout} s\n"
                               f"stderr tail: {''.join(self.stderr[-15:])}") from None
        if msg.get("error") is not None:
            raise RpcFailure(msg["error"])
        return msg.get("result")

    def request(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        return self.answer(self.request_async(method, params), timeout)

    def orphan_error(self, timeout: float = 10) -> dict:
        """The next answer with id null (what a parse error produces)."""
        return self._orphans.get(timeout=timeout)

    def _read(self) -> None:
        for raw in iter(self.p.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                assert isinstance(msg, dict) and msg.get("jsonrpc") == "2.0"
            except (ValueError, AssertionError):
                self.garbage.append(line)
                continue
            self._note("agent>app", msg)
            if "method" in msg and "id" in msg:
                threading.Thread(target=self._serve, args=(msg,), daemon=True).start()
            elif "method" in msg:
                self._notification(msg["method"], msg.get("params") or {})
            elif msg.get("id") is None:
                self._orphans.put(msg)
            else:
                method = self._methods.get(msg["id"])
                res = msg.get("result")
                if method in ("turn.start", "command.run") and isinstance(res, dict) and res.get("turn"):
                    with self._cv:
                        self.started[res["turn"]] = method
                        self.events.setdefault(res["turn"], [])
                q = self._answers.get(msg["id"])
                if q is not None:
                    q.put(msg)
        # EOF: the sidecar is gone. An app must never leave a turn open: synthesise its end.
        with self._cv:
            for turn, evs in self.events.items():
                if not any(e.get("type") == "done" for e in evs):
                    ended = {e.get("call") for e in evs if e.get("type") == "tool_end"}
                    for call in [e["call"] for e in evs if e.get("type") == "tool_start" and e["call"] not in ended]:
                        evs.append({"turn": turn, "seq": len(evs) + 1, "type": "tool_end", "call": call, "ok": False,
                                    "error": {"code": "ABORTED", "message": "the Copper agent stopped"}, "synthetic": True})
                    evs.append({"turn": turn, "seq": len(evs) + 1, "type": "error", "kind": "protocol", "code": "AGENT_EXITED",
                                "message": "the Copper agent stopped", "synthetic": True})
                    evs.append({"turn": turn, "seq": len(evs) + 1, "type": "done", "status": "error", "synthetic": True})
            self.eof.set()
            self._cv.notify_all()

    def _read_err(self) -> None:
        for raw in iter(self.p.stderr.readline, b""):
            self.stderr.append(raw.decode("utf-8", errors="replace"))

    def _notification(self, method: str, params: dict) -> None:
        if method == "event":
            with self._cv:
                self.events.setdefault(params.get("turn"), []).append(params)
                self._cv.notify_all()
        elif method == "host.cancelled":
            self.cancelled_requests.append(params)

    # ── turns ─────────────────────────────────────────────────────────────
    def hello(self, **over) -> dict:
        params = {"protocol": PROTOCOL, "app_version": self.app_version, "kicad_cli": None, "python": sys.executable,
                  "project_dir": str(self.live) if self.live else None, "frame": "schematic"}
        params.update(over)
        return self.request("hello", params)

    def turn(self, text: str, **kw) -> str:
        return self.request("turn.start", dict({"conversation": None, "text": text, "focus": None, "mode": "chat",
                                                "account": "none", "model": None}, **kw))["turn"]

    def command(self, name: str, args: dict | None = None, **kw) -> str:
        return self.request("command.run", dict({"name": name, "args": args or {}, "focus": None}, **kw))["turn"]

    def wait_done(self, turn: str, timeout: float = 120) -> list[dict]:
        end = time.time() + timeout
        with self._cv:
            while not any(e.get("type") == "done" for e in self.events.get(turn, [])):
                left = end - time.time()
                if left <= 0:
                    raise TimeoutError(f"turn {turn} did not end in {timeout} s; events so far: "
                                       f"{[e.get('type') for e in self.events.get(turn, [])]}\n"
                                       f"stderr tail: {''.join(self.stderr[-15:])}")
                self._cv.wait(min(left, 0.5))
            return list(self.events[turn])

    def wait_event(self, turn: str, type_: str, timeout: float = 30) -> dict:
        end = time.time() + timeout
        with self._cv:
            while True:
                for e in self.events.get(turn, []):
                    if e.get("type") == type_:
                        return e
                if time.time() >= end:
                    raise TimeoutError(f"no {type_} event for {turn} in {timeout} s")
                self._cv.wait(0.2)

    def wait_call(self, method: str, timeout: float = 30) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            for m, p in list(self.calls):
                if m == method:
                    return p
            time.sleep(0.02)
        raise TimeoutError(f"the agent never called {method}")

    def run_turn(self, text: str, timeout: float = 120, **kw) -> list[dict]:
        return self.wait_done(self.turn(text, **kw), timeout)

    def run_command(self, name: str, args: dict | None = None, timeout: float = 120, **kw) -> list[dict]:
        return self.wait_done(self.command(name, args, **kw), timeout)

    def cancel(self, turn: str) -> dict:
        return self.request("turn.cancel", {"turn": turn})

    def text_of(self, events: list[dict]) -> str:
        return "".join(e.get("text", "") for e in events if e.get("type") == "text_delta")

    # ── the app's side of the document ────────────────────────────────────
    def user_edit(self, document: str = "schematic") -> int:
        """The user changed the document (here: nothing but the revision moves)."""
        self.rev[document] += 1
        self.notify("doc.changed", {"document": document, "rev": self.rev[document]})
        return self.rev[document]

    def keep(self, proposal_id: str) -> None:
        self.proposals[proposal_id]["outcome"] = "kept"
        self.notify("proposal.resolved", {"proposal_id": proposal_id, "outcome": "kept"})

    def undo(self) -> str | None:
        """Ctrl+Z: revert the last apply as one step."""
        if not self.undo_stack:
            return None
        entry = self.undo_stack.pop()
        for rel, text in entry["files"].items():
            f = self.live / rel
            if text is None:
                f.unlink(missing_ok=True)
            else:
                f.write_bytes(text)
        self.rev["schematic"] += 1
        self.proposals[entry["proposal_id"]]["outcome"] = "undone"
        self.notify("doc.changed", {"document": "schematic", "rev": self.rev["schematic"]})
        self.notify("proposal.resolved", {"proposal_id": entry["proposal_id"], "outcome": "undone"})
        return entry["proposal_id"]

    def _serve(self, msg: dict) -> None:
        method, params, mid = msg["method"], msg.get("params") or {}, msg["id"]
        self.calls.append((method, params))
        if method in self.mute:
            return
        if self.delay.get(method):
            time.sleep(self.delay[method])
        try:
            if method in self.fail:
                raise HostRefusal(*self.fail[method])
            fn = getattr(self, "h_" + method.replace("host.", "", 1).replace(".", "_"), None)
            if not method.startswith("host.") or fn is None:
                self._send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}",
                                                                   "data": {"code": "METHOD_NOT_FOUND"}}})
                return
            result = fn(params)
        except HostRefusal as e:
            self._send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": e.message,
                                                               "data": dict(e.data, code=e.code)}})
            return
        except Exception as e:  # noqa: BLE001
            self._send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}",
                                                               "data": {"code": "INTERNAL"}}})
            return
        self._send({"jsonrpc": "2.0", "id": mid, "result": result})
        after = getattr(self, "_after_" + method.replace("host.", "", 1), None)
        if after:
            after(params, result)

    def _project(self):
        from copper_kicad import project as P
        return P.resolve(str(self.live), activate=False)

    def sheets(self) -> list[dict]:
        """[{'sheet_path': '/' | '/name/...', 'file': project-relative}] of the live schematic, root first."""
        from copper_kicad.sch import ops
        pr = self._project()
        names, out, seen = {}, [], set()
        for h in ops.hierarchy(pr):
            parent = h["path"].rsplit("/", 1)[0]
            names[h["path"]] = (names.get(parent, "").rstrip("/") + "/" + h["name"]) if h["name"] else "/"
            # realpath on both sides: a temp folder may be spelled 8.3 (USERNA~1) on one and long on the other
            rel = os.path.relpath(os.path.realpath(h["file"]), os.path.realpath(self.live)).replace(os.sep, "/")
            if rel not in seen and Path(h["file"]).exists():
                seen.add(rel)
                out.append({"sheet_path": names[h["path"]], "file": rel})
        return out

    def h_checkout(self, p: dict) -> dict:
        doc = p.get("document")
        if doc not in ("schematic", "board"):
            raise HostRefusal("NO_DOCUMENT", f"no document {doc!r}")
        rev = self.rev[doc]
        shadow = self.live / ".copper" / "shadow" / str(rev)
        if shadow.exists():
            shutil.rmtree(long_path(shadow))
        shadow.parent.mkdir(parents=True, exist_ok=True)
        copy_project(self.live, shadow)
        return {"shadow_dir": str(shadow), "rev": rev, "document": doc, "project": self._project().name,
                "sheets": self.sheets() if doc == "schematic" else [], "dirty": False}

    def _after_checkout(self, p, result):
        if self.edit_after_checkout:
            self.edit_after_checkout = False
            self.user_edit(p.get("document"))

    def h_apply(self, p: dict) -> dict:
        from copper_kicad.sch import patch as schpatch
        doc = p.get("document")
        if doc != "schematic":
            raise HostRefusal("UNSUPPORTED", "fakehost applies schematic patches only")
        if self.busy:
            raise HostRefusal("BUSY", "the editor is busy")
        if p.get("base_rev") != self.rev[doc]:
            raise HostRefusal("STALE_BASE", "the document changed since the checkout", rev=self.rev[doc])
        libs = [t for t in p.get("lib_symbols") or []]
        old, texts = {}, {}
        for pt in p.get("patches") or []:
            rel = str(pt.get("file") or "")
            f = (self.live / rel).resolve()
            if not rel or self.live.resolve() not in f.parents or not f.is_file():
                raise HostRefusal("NO_SHEET", f"no sheet file {rel!r} in the open schematic")
            old.setdefault(rel, f.read_bytes())
            texts.setdefault(rel, old[rel].decode("utf-8"))
        try:                                             # the reference applier: all patches in memory, or nothing
            new = schpatch.apply_project(texts, p.get("patches") or [], libs)
            counts = schpatch.patch_counts(p.get("patches") or [])
        except schpatch.PatchError as e:
            raise HostRefusal(e.code, e.message) from None
        side = {}
        for f in p.get("files") or []:
            rel, src = str(f.get("path") or ""), Path(str(f.get("shadow_path") or ""))
            dst = (self.live / rel).resolve()
            if not rel or self.live.resolve() not in dst.parents or not src.is_file():
                raise HostRefusal("PARSE", f"bad side file {rel!r}")
            side[rel] = (src, dst)
        # commit: all or nothing, one undo entry
        for rel, (src, dst) in side.items():
            old[rel] = dst.read_bytes() if dst.exists() else None
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        for rel, text in new.items():
            (self.live / rel).write_bytes(text.encode("utf-8"))
        self.rev[doc] += 1
        pid = f"p{len(self.proposals) + 1}"
        self.undo_stack.append({"proposal_id": pid, "files": old})
        netlist_ok = None
        want = (p.get("expect") or {}).get("netlist_hash")
        if want and self.check_netlist:
            from copper_kicad.sch import netlist
            netlist_ok = schpatch.netlist_hash(netlist.export_netlist(self._project())) == want
        self.proposals[pid] = {"summary": p.get("summary"), "counts": counts, "outcome": None, "netlist_ok": netlist_ok}
        return {"proposal_id": pid, "state": "review", "rev": self.rev[doc], "counts": counts, "erc_new": [],
                "netlist_ok": netlist_ok}

    def _after_apply(self, p, result):
        self.notify("doc.changed", {"document": "schematic", "rev": result["rev"]})

    def h_reveal(self, p: dict) -> dict:
        self.reveals.append(p)
        return {}

    def h_checkpoint(self, p: dict) -> dict:
        cid = f"cp{len(self.checkpoints) + 1}"
        copy_project(self.live, self.live / ".copper" / "checkpoints" / cid)
        self.checkpoints.append(cid)
        return {"id": cid}

    def h_save(self, p: dict) -> dict:
        if p.get("document") not in self.rev:
            raise HostRefusal("NO_DOCUMENT", f"no document {p.get('document')!r}")
        return {"rev": self.rev[p["document"]]}

    def h_reload(self, p: dict) -> dict:
        if p.get("document") not in self.rev:
            raise HostRefusal("NO_DOCUMENT", f"no document {p.get('document')!r}")
        self.rev[p["document"]] += 1
        self.undo_stack.clear()
        return {"rev": self.rev[p["document"]]}

    def h_verify(self, p: dict) -> dict:
        from copper_kicad.review import verify
        doc = p.get("document")
        r = verify.run_erc(self._project()) if doc == "schematic" else verify.run_drc(self._project())
        return {"document": doc, "findings": r.get("violations") or [],
                "counts": {"errors": r.get("errors", 0), "warnings": r.get("warnings", 0)}, "baseline": f"rev{self.rev[doc]}"}

    def h_ask_permission(self, p: dict) -> dict:
        ans = self.permission(p) if callable(self.permission) else self.permission
        return dict(ans)

    # ── end ───────────────────────────────────────────────────────────────
    def close(self, timeout: float = 15) -> int | None:
        """Close the agent's stdin (what the app does on exit) and wait for it to leave."""
        if self.p is None:
            return None
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(5)
            return None
        self.eof.wait(5)
        return self.p.returncode

    def kill(self) -> None:
        self.p.kill()
        self.p.wait(5)
        self.eof.wait(5)

    def save_transcript(self, path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for rec in self.transcript:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return path


def _lib_name(text: str) -> str | None:
    from copper_kicad import sexpr
    try:
        n = sexpr.loads(text)
        return str(n[1])
    except (ValueError, IndexError):
        return None
