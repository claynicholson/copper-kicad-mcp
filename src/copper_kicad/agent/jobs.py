"""Long engine jobs the user can stop: one engine tool in a child process, and the board file-job sequence.

Engine tools are plain blocking functions (kicad-cli, pcbnew workers, an autorouter); most cannot be
interrupted from inside. ``Turn.call_tool`` runs them on the turn's thread, so Stop only mutes the turn while
the tool keeps running. For a job that takes minutes, or that writes the user's board file, that is not good
enough. This module runs the tool in a child Python process instead:

    run_tool(turn, name, args, *, project=None, title=None) -> outcome
        One engine tool in a child process, reported as tool_start / tool_progress / tool_end. On turn.cancel the
        whole process tree is killed and ``Cancelled`` is raised. Same outcome dict as ``Turn.call_tool``.
        For read-only work and exports (nothing to roll back).

    board_job(turn, name, args, label) -> outcome
        A board tool that writes (HOST_PROTOCOL.md 5.9): ask_permission, host.save, checkpoint, the tool on the
        saved files in a child process, host.reload. Stop kills the job and puts every board file back byte for
        byte, so a cancelled job changed nothing. When the job left the files as they were (dry run, refusal)
        the reload is skipped and the editor keeps its undo history.

    Job checkpoints   <project>/.copper/jobs/<id>/{job.json, <board files>}
        checkpoints(project_dir) -> [job]            newest first
        restore(turn, job_id=None) -> outcome        "/undo-checkpoint": put a checkpoint's files back and reload.
                                                     The state before the restore is itself checkpointed, so a
                                                     second restore is a redo (job['kind'] == 'redo').
        recover(project_dir) -> [id]                 roll back jobs whose agent died under them

On Windows the job process lives in a kernel job object: Stop ends its whole tree at once, and the system ends
it if the agent itself is killed. The next board job then finds the orphaned checkpoint and rolls it back.

Only board-side files are snapshotted and restored (``BOARD_FILES``). The schematic is never touched here: its
undo is the editor's own Ctrl+Z, because every schematic change goes through ``host.apply``.

The child speaks one JSON object per line on its stdout: ``{"p": fraction, "m": message}`` while it runs, then
``{"wire": ..., "data": ...}`` once. Everything else the engine or its tools print goes to stderr (the log).
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..host.client import E_DENIED, E_NO_DOCUMENT, E_UNSUPPORTED, HostError
from ..host.live import _fail, _outcome
from .drivers.base import Cancelled

BOARD_FILES = (".kicad_pcb", ".kicad_pro", ".kicad_dru")      # what a board job may change, and what restore puts back
KEEP_JOBS = 20
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_SRC = str(Path(__file__).resolve().parents[2])                 # the folder that holds the copper_kicad package
_CHILD = ("import sys; sys.path.insert(0, sys.argv[1]); "
          "from copper_kicad.agent import jobs; sys.exit(jobs.child_main())")
_SKIP = (E_NO_DOCUMENT, E_UNSUPPORTED)

# Jobs that hold a snapshot they may still have to put back. The agent's turn threads are daemon threads: when
# the app closes the channel mid-job the process would exit under the rollback, so exit waits for it here.
_active = 0
_idle = threading.Condition()


def _enter() -> None:
    global _active
    with _idle:
        _active += 1


def _leave() -> None:
    global _active
    with _idle:
        _active -= 1
        _idle.notify_all()


@atexit.register
def _drain(timeout: float = 20.0) -> None:
    end = time.monotonic() + timeout
    with _idle:
        while _active > 0 and time.monotonic() < end:
            _idle.wait(0.1)


# ── the child ─────────────────────────────────────────────────────────────
def child_main() -> int:
    """Entry of the job process: read one request from stdin, run the tool, write progress and the result."""
    out_fd = os.dup(1)
    os.dup2(2, 1)                                   # whatever the engine prints goes to the log, not to our pipe
    sys.stdout = sys.stderr
    if sys.platform == "win32":
        import msvcrt
        msvcrt.setmode(out_fd, os.O_BINARY)
    out = os.fdopen(out_fd, "wb")
    lock = threading.Lock()

    def emit(obj: dict) -> None:
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        with lock:
            try:
                out.write(data)
                out.flush()
            except (OSError, ValueError):
                pass

    try:
        req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except ValueError:
        emit({"wire": {"content": [{"type": "text", "text": "job request was not JSON"}], "isError": True}, "data": None})
        return 2

    def progress(fraction: float, message: str = "") -> None:
        emit({"p": fraction, "m": message})
    progress.has_token = True
    progress.cancelled = lambda: False              # a cancelled job is killed, not asked

    from .. import tools as _tools  # noqa: F401  (registers the engine tools)
    from ..host.live import _engine
    res = _engine(req["tool"], req.get("args") or {}, req.get("project_dir"), progress)
    raw = res["raw"] if isinstance(res.get("raw"), dict) else {}
    wire = res["wire"]
    emit({"wire": wire, "data": raw.get("structuredContent") if isinstance(raw.get("structuredContent"), dict) else None})
    return 0


# ── the parent ────────────────────────────────────────────────────────────
def _win_job(p: subprocess.Popen):
    """Windows: put the job process into a kernel job object so that its whole tree can be ended at once, and
    IS ended by the system if this agent dies (KILL_ON_JOB_CLOSE). -> (kernel32, handle), or None (then taskkill)."""
    try:
        import ctypes
        from ctypes import wintypes

        class _Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k.CreateJobObjectW(None, None)
        if not h:
            return None
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000                  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info))
        if not ok or not k.AssignProcessToJobObject(h, int(p._handle)):
            k.CloseHandle(h)
            return None
        return (k, h)
    except Exception:  # noqa: BLE001 - no job object: kill_tree falls back to taskkill
        return None


def kill_tree(p: subprocess.Popen) -> None:
    """End the job and everything it started (kicad-cli, the pcbnew worker, a router)."""
    job = getattr(p, "copper_job", None)
    if job is not None:
        p.copper_job = None
        try:
            job[0].TerminateJobObject(job[1], 1)                         # the whole tree, at once
            job[0].CloseHandle(job[1])
        except Exception:  # noqa: BLE001
            pass
    elif p.poll() is None:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=10,
                               stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
            else:
                os.killpg(p.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        if p.poll() is None:
            p.kill()
        p.wait(5)
    except (OSError, subprocess.SubprocessError):
        pass


def _pump(p: subprocess.Popen, q: "queue.Queue") -> None:
    try:
        for raw in iter(p.stdout.readline, b""):
            try:
                msg = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(msg, dict):
                q.put(msg)
    except (OSError, ValueError):
        pass
    q.put(None)


def _child(turn, call: str, name: str, args: dict, route: str) -> dict:
    """Run one engine tool in a child process under an already open tool call. -> outcome.
    Raises Cancelled (after killing the process tree) when the turn is cancelled or ends."""
    request = json.dumps({"tool": name, "args": args, "project_dir": turn.project_dir}, ensure_ascii=False, default=str)
    kw = {"creationflags": _NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}
    try:
        p = subprocess.Popen([sys.executable, "-X", "utf8", "-c", _CHILD, _SRC], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=None, env=dict(os.environ, PYTHONIOENCODING="utf-8"), **kw)
    except OSError as e:
        return _fail(route, "JOB_FAILED", f"could not start the job process: {e.strerror or e}")
    p.copper_job = _win_job(p) if sys.platform == "win32" else None
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_pump, args=(p, q), name="copper-job-reader", daemon=True).start()
    result = None
    turn.in_tool += 1
    try:
        try:
            p.stdin.write(request.encode("utf-8"))
            p.stdin.close()
        except OSError:
            pass
        while True:
            if turn.cancelled.is_set() or turn.finished:
                kill_tree(p)
                raise Cancelled()
            try:
                msg = q.get(timeout=0.05)
            except queue.Empty:
                continue
            if msg is None:
                break
            if "wire" in msg:
                result = msg
            elif "p" in msg:
                try:
                    turn.tool_progress(call, float(msg["p"]), str(msg.get("m") or "") or None)
                except (TypeError, ValueError):
                    pass
    finally:
        turn.in_tool -= 1
        turn.touch()
        if p.poll() is None and result is None:
            kill_tree(p)
        try:
            p.wait(10)
        except subprocess.SubprocessError:
            pass
        kill_tree(p)                                    # nothing the job started may outlive it
    if result is None or not isinstance(result.get("wire"), dict):
        return _fail(route, "JOB_CRASHED", f"the {name} job stopped without a result (exit code {p.returncode})",
                     "run it again; the agent log has the job's output")
    return _outcome(route, {"raw": {"structuredContent": result.get("data")}, "wire": result["wire"]})


def _end(turn, call: str, out: dict) -> dict:
    turn.tool_end(call, out["ok"], summary=(out["text"].splitlines() or [""])[0] if out["ok"] else None, error=out["error"])
    return out


def run_tool(turn, name: str, args: dict | None = None, *, project: str | None = None, title: str | None = None,
             route: str = "disk") -> dict:
    """One engine tool in a child process, as one tool call of the turn. ``project`` (default: the open project)
    is passed to tools that take one."""
    from ..mcp import registry
    from .. import tools as _tools  # noqa: F401  (registers the engine tools)
    args = dict(args or {})
    tool = registry.TOOLS.get(name)
    shown = dict(args)
    if tool is not None and "project" in (tool.schema.get("properties") or {}) and (project or turn.project_dir):
        args["project"] = str(project or turn.project_dir)
    call = turn.tool_start(name, shown, title)
    try:
        out = _child(turn, call, name, args, route)
    except Cancelled:
        turn.tool_end(call, False, error={"code": "CANCELLED", "message": "cancelled"})
        raise
    return _end(turn, call, out)


# ── job checkpoints ───────────────────────────────────────────────────────
def _jobs_dir(project_dir) -> Path:
    return Path(project_dir) / ".copper" / "jobs"


def board_files(project_dir) -> list[Path]:
    d = Path(project_dir)
    return sorted(f for f in d.iterdir() if f.is_file() and f.suffix in BOARD_FILES and not f.name.startswith(("~", ".")))


def snapshot(project_dir, label: str, tool: str, host_checkpoint: str | None = None) -> dict:
    """Copy the board-side files to a new job checkpoint. -> its job record (also written as job.json)."""
    root = _jobs_dir(project_dir)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in tool)[:32].strip("-") or "job"
    base = f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}"
    jid, n = base, 1
    while (root / jid).exists():
        n += 1
        jid = f"{base}-{n}"
    dest = root / jid
    dest.mkdir(parents=True)
    files = []
    try:
        for f in board_files(project_dir):
            shutil.copy2(f, dest / f.name)
            files.append(f.name)
    except OSError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    job = {"id": jid, "label": label[:120], "tool": tool, "created": round(time.time()), "files": files, "state": "open",
           "kind": "job", "pid": os.getpid()}
    if host_checkpoint:
        job["host_checkpoint"] = host_checkpoint
    _write_job(project_dir, job)
    for old in sorted(p for p in root.iterdir() if p.is_dir())[:-KEEP_JOBS]:
        shutil.rmtree(old, ignore_errors=True)
    return job


def _write_job(project_dir, job: dict) -> None:
    p = _jobs_dir(project_dir) / job["id"] / "job.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def checkpoints(project_dir, states=("done",)) -> list[dict]:
    """Job checkpoints that can be restored, newest first."""
    root = _jobs_dir(project_dir)
    out = []
    if not root.is_dir():
        return out
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        try:
            job = json.loads((d / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(job, dict) and job.get("id") == d.name and (states is None or job.get("state") in states):
            out.append(job)
    return out


def changed_since(project_dir, job: dict) -> list[str]:
    """Names of the board files that differ from the checkpoint (or appeared / disappeared since)."""
    d, snap = Path(project_dir), _jobs_dir(project_dir) / job["id"]
    now = {f.name for f in board_files(project_dir)}
    out = []
    for name in sorted(now | set(job.get("files") or [])):
        try:
            same = name in now and name in job["files"] and (d / name).read_bytes() == (snap / name).read_bytes()
        except OSError:
            same = False
        if not same:
            out.append(name)
    return out


def put_back(project_dir, job: dict) -> list[str]:
    """Make the board files byte-identical to the checkpoint. -> names written or removed. Each file is replaced
    atomically; files the checkpoint does not hold (created since) are removed."""
    d, snap = Path(project_dir), _jobs_dir(project_dir) / job["id"]
    touched = []
    for name in changed_since(project_dir, job):
        target = d / name
        if name in (job.get("files") or []):
            tmp = d / f".{name}.copper-restore"
            shutil.copy2(snap / name, tmp)
            for attempt in range(5):
                try:
                    os.replace(tmp, target)
                    break
                except PermissionError:
                    if attempt == 4:
                        tmp.unlink(missing_ok=True)
                        raise
                    time.sleep(0.2 * (attempt + 1))
        else:
            target.unlink(missing_ok=True)
        touched.append(name)
    return touched


def _drop(project_dir, job: dict) -> None:
    shutil.rmtree(_jobs_dir(project_dir) / job["id"], ignore_errors=True)


def recover(project_dir) -> list[str]:
    """Roll back jobs whose agent died under them (state 'open', owner process gone): the system killed the job
    with the agent and the editor never reloaded, so the checkpoint is what the editor still shows.
    -> ids rolled back. Called before every board job and restore."""
    from ..host.rendezvous import pid_alive
    done = []
    for job in checkpoints(project_dir, states=("open",)):
        if job.get("pid") == os.getpid() or pid_alive(job.get("pid")):
            continue                                    # running right now (here, or in the other editor's agent)
        try:
            put_back(project_dir, job)
            _drop(project_dir, job)
            done.append(job["id"])
        except OSError:
            pass
    return done


# ── the board file job ────────────────────────────────────────────────────
def _save(host, documents) -> list[str]:
    saved = []
    for doc in documents:
        try:
            host.save(doc)
            saved.append(doc)
        except HostError as e:
            if e.code not in _SKIP:              # not open in the app (or an older app): the file is the document
                raise
    return saved


def _reload(turn, host) -> None:
    try:
        res = host.reload("board")
    except HostError as e:
        if e.code not in _SKIP:
            raise
        return
    turn.server.live.invalidate("board")
    if isinstance(res.get("rev"), int):
        turn.server.live.revs["board"] = res["rev"]


def board_job(turn, name: str, args: dict | None = None, label: str | None = None) -> dict:
    """Run a board-writing engine tool as a file job the user can stop (see the module docstring). -> outcome,
    with ``checkpoint`` (a job id for /undo-checkpoint) when the board changed. Never raises for a refusal or a
    tool failure; raises Cancelled when the turn was cancelled (the board files are then as before)."""
    from ..mcp import registry
    from .. import tools as _tools  # noqa: F401
    args = dict(args or {})
    tool = registry.TOOLS.get(name)
    label = label or (tool.title if tool is not None and tool.title else name)
    call = turn.tool_start(name, args, label)
    job = None
    _enter()
    try:
        if not turn.project_dir:
            return _end(turn, call, _fail("board", "NO_PROJECT", "no project is open"))
        if tool is None:
            return _end(turn, call, _fail("board", "UNKNOWN_TOOL", f"no engine tool {name!r}"))
        host = turn.host
        props = tool.schema.get("properties") or {}
        try:
            ans = host.ask_permission("file_job", {"tool": name, "document": "board", "label": label,
                                                   "note": "runs on the saved board file; undo is /undo-checkpoint"})
            if not ans.get("granted"):
                raise HostError(E_DENIED, f"you declined to run {label} on the board file")
            recover(turn.project_dir)
            _save(host, ("schematic", "board"))
            cp = None
            if not args.get("dry_run"):                # a dry run writes nothing: no entry in the app's history
                try:
                    cp = host.checkpoint(label, "board").get("id")
                except HostError as e:
                    if e.code != E_UNSUPPORTED:
                        raise
            job = snapshot(turn.project_dir, label, name, cp)
        except HostError as e:
            return _end(turn, call, _fail("board", e.code, e.message, e.data.get("fix")))
        except OSError as e:
            return _end(turn, call, _fail("board", "CHECKPOINT_FAILED", f"could not snapshot the board files: {e.strerror or e}"))
        run = dict(args, project=turn.project_dir)
        if "while_open" in props:
            run.setdefault("while_open", "revert")
        if "render" in props:
            run.setdefault("render", False)
        try:
            out = _child(turn, call, name, run, "board")
        except Cancelled:
            put_back(turn.project_dir, job)             # the app saved right before the job: file == live document
            _drop(turn.project_dir, job)
            job = None
            raise
        changed = changed_since(turn.project_dir, job)
        if changed:
            job["state"], job["changed"] = "done", changed
            job["summary"] = (out["text"].splitlines() or [""])[0][:200]
            _write_job(turn.project_dir, job)
            out["checkpoint"] = job["id"]
            out["text"] += "\n" + json.dumps({"live": {"checkpoint": job["id"], "undo": "/undo-checkpoint"}},
                                             separators=(",", ":"))
            try:
                _reload(turn, host)
            except Cancelled:                          # stopped before the editor re-read the file: undo the job,
                put_back(turn.project_dir, job)        # so the file still is what the editor shows
                _drop(turn.project_dir, job)
                job = None
                raise
            except HostError as e:
                out["text"] +="\n" + json.dumps({"live": {"reload": e.code, "message": e.message}}, separators=(",", ":"))
                out["reload_error"] = {"code": e.code, "message": e.message}
        else:
            _drop(turn.project_dir, job)               # nothing changed: no reload, the undo history survives
        job = None
        return _end(turn, call, out)
    except Cancelled:
        turn.tool_end(call, False, error={"code": "CANCELLED", "message": "cancelled; the board files are as before"})
        raise
    except BaseException:
        turn.tool_end(call, False, error={"code": "INTERNAL", "message": "the job failed inside Copper"})
        if job is not None and job.get("state") == "open":
            try:
                put_back(turn.project_dir, job)
                _drop(turn.project_dir, job)
            except OSError:
                pass
        raise
    finally:
        _leave()


def restore(turn, job_id: str | None = None) -> dict:
    """Put a job checkpoint's board files back and reload the board. -> outcome with ``data``:
    {'restored': id, 'label', 'files': [names], 'redo': id of the checkpoint taken just before}."""
    call = turn.tool_start("undo-checkpoint", {"id": job_id} if job_id else None, "Restore checkpoint")
    _enter()
    try:
        if not turn.project_dir:
            return _end(turn, call, _fail("board", "NO_PROJECT", "no project is open"))
        recover(turn.project_dir)
        jobs = checkpoints(turn.project_dir)
        job = next((j for j in jobs if j["id"] == job_id), None) if job_id else (jobs[0] if jobs else None)
        if job is None:
            return _end(turn, call, _fail("board", "NO_CHECKPOINT",
                                          f"no checkpoint {job_id!r}" if job_id else "there is no board job to undo",
                                          "/undo-checkpoint list shows the checkpoints" if job_id else None))
        host = turn.host
        try:
            ans = host.ask_permission("file_job", {"tool": "undo-checkpoint", "document": "board",
                                                   "label": f"Restore the board from before: {job['label']}",
                                                   "note": "board edits made since are replaced; run it again to redo"})
            if not ans.get("granted"):
                raise HostError(E_DENIED, "you declined to restore the checkpoint")
            _save(host, ("board",))                    # so the redo checkpoint holds the board as it is now
            if not changed_since(turn.project_dir, job):
                return _end(turn, call, _ok("board", f"nothing to restore: the board is already as it was before {job['label']}",
                                            {"restored": None, "label": job["label"], "files": []}))
            was_redo = job.get("kind") == "redo"
            redo = snapshot(turn.project_dir, job["label"], "undo-checkpoint")
            files = put_back(turn.project_dir, job)
            redo["state"] = "done"
            if not was_redo:
                redo["kind"] = "redo"                  # restoring THIS one brings the job's result back
            _write_job(turn.project_dir, redo)
            job["state"] = "restored"
            _write_job(turn.project_dir, job)
            out = _ok("board", (f"redid {job['label']}" if was_redo else f"the board is as it was before {job['label']}")
                      + f" ({', '.join(files)})",
                      {"restored": job["id"], "label": job["label"], "files": files, "redo": redo["id"],
                       "redone": was_redo})
            try:
                _reload(turn, host)
            except Cancelled:                          # stopped before the editor re-read the file: take it back
                put_back(turn.project_dir, redo)
                _drop(turn.project_dir, redo)
                job["state"] = "done"
                _write_job(turn.project_dir, job)
                raise
            except HostError as e:
                out["reload_error"] ={"code": e.code, "message": e.message}
            return _end(turn, call, out)
        except HostError as e:
            return _end(turn, call, _fail("board", e.code, e.message, e.data.get("fix")))
        except OSError as e:
            return _end(turn, call, _fail("board", "RESTORE_FAILED", f"could not restore the board files: {e.strerror or e}",
                                          "close any program holding the board file and run /undo-checkpoint again"))
    except Cancelled:
        turn.tool_end(call, False, error={"code": "CANCELLED", "message": "cancelled"})
        raise
    finally:
        _leave()


def _ok(route: str, text: str, data: dict | None = None) -> dict:
    return {"ok": True, "text": text, "data": data, "images": [], "error": None, "proposal": None, "route": route}
