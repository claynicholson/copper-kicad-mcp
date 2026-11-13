"""Shared machinery for drivers that run the user's own agent CLI headless. Not a driver: it registers nothing.

The shape every CLI driver follows:

    argv = find(account, names)                 the program: account['command'] if set, else the first name on PATH
    with ToolGate(turn) as gate, Child(argv + flags, cwd, turn) as child:
        child.send(prompt_text); child.close_stdin()
        for line in child.lines():              JSON lines from the CLI's stdout
            ...                                 -> turn.text / turn.usage / tool rows for the CLI's own tools
        (no terminal marker by EOF)             -> raise child.truncated()

Rules these helpers enforce:
  * the CLI is the user's unmodified program, signed in by its own flow; Copper passes no credential to it and
    reads none from it;
  * its only MCP server is Copper's bridge (_gate.py / _bridge.py); its own file and shell tools are switched off
    by the driver's flags, and any such call that still shows up in the stream is shown to the user as a tool row
    named ``cli:<name>``, never hidden;
  * nothing that needs quoting goes on a command line: prompts travel on stdin, configuration in files. On Windows
    the CLIs are often ``.cmd`` shims run through cmd.exe, where quotes, newlines and ``% & | < > ^`` in an
    argument are not safe; ``safe_arg`` refuses such an argument instead of guessing;
  * cancel kills the whole process tree; a CLI that prints nothing for COPPER_AGENT_CLI_IDLE_S (default 300 s)
    while no Copper tool is running is killed and reported as error(network, CLI_SILENT).

    Child(argv, cwd, turn, env=None)   .send(text) .close_stdin() .lines() .wait(timeout) .kill() .stderr_tail()
    find(account, names) -> [str] | None;  available(names) -> (bool, reason)
    safe_arg(value, what) -> str;  work_dir(turn) -> str
    Totals(turn, kind)                 per-conversation running totals for CLIs that report cumulative usage
    classify(message) -> DriverError;  earlier_turns(turn) -> str;  cli_tool_name(name);  text_of_content(content)
"""
from __future__ import annotations

import collections
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .base import DriverError

IS_WIN = sys.platform == "win32"
_UNSAFE = re.compile(r'["\r\n%&|<>^]')
_EOF = object()


def _env_f(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.environ.get(name, default)))
    except ValueError:
        return default


def split_command(command: str) -> list[str]:
    """An account's ``command`` as argv: a path (may contain spaces if it exists as given) or a command line."""
    command = (command or "").strip()
    if not command:
        return []
    if os.path.exists(command):
        return [command]
    parts = shlex.split(command, posix=not IS_WIN)
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]


def find(account: dict | None, names: tuple) -> list[str] | None:
    """The CLI's argv prefix, or None when it is not installed. Never runs the program."""
    argv = split_command((account or {}).get("command") or "")
    if argv:
        exe = argv[0] if os.path.exists(argv[0]) else shutil.which(argv[0])
        return [exe] + argv[1:] if exe else None
    for n in names:
        exe = shutil.which(n)
        if exe:
            return [exe]
    return None


def available(names: tuple) -> tuple[bool, str]:
    if find(None, names):
        return True, ""
    return False, f"`{names[0]}` is not on PATH; install it and sign in with its own login, or set the program path"


def safe_arg(value: str, what: str) -> str:
    """``value`` if it can be passed as a command-line argument through any launcher, else a DriverError."""
    value = str(value)
    if _UNSAFE.search(value):
        raise DriverError("engine", f"{what} contains a character that cannot be passed to the CLI safely: {value[:80]!r}",
                          code="UNSAFE_ARGUMENT", retryable=False)
    return value


def work_dir(turn) -> str:
    """Where the CLI runs: the project folder (its tools are off; this only names the project for its session
    store), or a scratch folder without a project."""
    return str(turn.project_dir) if turn.project_dir else tempfile.gettempdir()


class Child:
    """The CLI process: stdin/stdout pipes, stderr kept as a short tail, whole-tree kill."""

    def __init__(self, argv: list[str], cwd: str, turn, env: dict | None = None, what: str = "the agent CLI"):
        self.turn, self.what = turn, what
        self.q: queue.Queue = queue.Queue()
        self._err = collections.deque(maxlen=40)
        self.idle_s = _env_f("COPPER_AGENT_CLI_IDLE_S", 300)
        flags = 0
        if IS_WIN:
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      cwd=cwd, env=dict(os.environ, **(env or {})), bufsize=0, creationflags=flags,
                                      start_new_session=not IS_WIN)
        except OSError as e:
            raise DriverError("engine", f"cannot start {what}: {type(e).__name__}: {e}", code="CLI_START",
                              retryable=False, fix="check that the program is installed and its path is right") from None
        threading.Thread(target=self._pump_out, name="copper-cli-out", daemon=True).start()
        threading.Thread(target=self._pump_err, name="copper-cli-err", daemon=True).start()

    def _pump_out(self):
        try:
            for raw in iter(self.p.stdout.readline, b""):
                self.q.put(raw)
        except (OSError, ValueError):
            pass
        self.q.put(_EOF)

    def _pump_err(self):
        try:
            for raw in iter(self.p.stderr.readline, b""):
                self._err.append(raw.decode("utf-8", errors="replace").rstrip())
        except (OSError, ValueError):
            pass

    def send(self, text: str) -> None:
        try:
            self.p.stdin.write(text.encode("utf-8"))
            self.p.stdin.flush()
        except (OSError, ValueError):
            pass                                    # the reader side reports a CLI that died

    def close_stdin(self) -> None:
        try:
            self.p.stdin.close()
        except (OSError, ValueError):
            pass

    def lines(self):
        """Lines of the CLI's stdout until EOF. Checks for cancel 10 times a second; while the CLI is alive and
        either printing or waiting for one of our tools, the watchdog is fed."""
        last = time.monotonic()
        while True:
            self.turn.check_cancel()
            try:
                raw = self.q.get(timeout=0.1)
            except queue.Empty:
                if self.turn.in_tool:
                    last = time.monotonic()
                elif time.monotonic() - last > self.idle_s:
                    self.kill()
                    raise DriverError("network", f"{self.what} printed nothing for {self.idle_s:.0f} s", code="CLI_SILENT",
                                      retryable=True) from None
                self.turn.touch()
                continue
            if raw is _EOF:
                return
            last = time.monotonic()
            self.turn.touch()
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                yield line

    def json_lines(self):
        """``lines()`` parsed as JSON objects; lines that are not JSON objects (a banner, a warning) are skipped."""
        for line in self.lines():
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                yield msg

    def wait(self, timeout: float = 5.0) -> int | None:
        try:
            return self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill(self) -> None:
        if self.p.poll() is not None:
            return
        try:
            if IS_WIN:                              # the shim's cmd.exe, node and our bridge: the whole tree
                subprocess.run(["taskkill", "/PID", str(self.p.pid), "/T", "/F"], capture_output=True, timeout=10,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                import signal
                os.killpg(self.p.pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            self.p.wait(2)
        except subprocess.TimeoutExpired:
            try:
                self.p.kill()
            except OSError:
                pass

    def stderr_tail(self, limit: int = 300) -> str:
        text = " | ".join(l for l in list(self._err)[-4:] if l.strip())
        return " ".join(text.split())[-limit:]

    def truncated(self) -> DriverError:
        """The error for "stdout ended before the CLI's terminal marker"."""
        code = self.wait(3)
        tail = self.stderr_tail()
        low = tail.lower()
        if any(w in low for w in ("not logged in", "log in", "login", "sign in", "unauthorized", "authenticat", "api key")):
            return DriverError("auth", f"{self.what} is not signed in: {tail}", code="CLI_AUTH", retryable=False,
                               fix="run the CLI once in a terminal and sign in with its own login")
        if code not in (None, 0):
            return DriverError("protocol", f"{self.what} exited with code {code}" + (f": {tail}" if tail else ""),
                               code="CLI_EXIT", retryable=False)
        return DriverError.truncated(f"{self.what}'s output")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.kill()
        self.close_stdin()
        for f in (self.p.stdout, self.p.stderr):
            try:
                f.close()
            except (OSError, ValueError):
                pass


class Totals:
    """Running totals of a CLI session that reports cumulative figures (so a turn can report its own share).
    Kept with the conversation as a 'resume' record of driver ``<kind>.totals``: {'session': id, name: total}."""

    def __init__(self, turn, kind: str):
        self.turn, self.key = turn, kind + ".totals"
        self.before: dict = {}
        self.now: dict = {}
        conv = turn.request.conversation
        if conv:
            try:
                self.before = json.loads(turn.sessions.resume_id(conv, self.key) or "{}")
            except (ValueError, TypeError):
                self.before = {}

    def delta(self, name: str, total, session_id: str | None):
        """This turn's share of the cumulative ``total`` of session ``session_id``."""
        total = total or 0
        self.now[name] = total
        prev = self.before.get(name) or 0
        return total - prev if session_id and self.before.get("session") == session_id and total >= prev else total

    def save(self, session_id: str | None) -> None:
        conv = self.turn.request.conversation
        if conv and self.now and session_id:
            try:
                self.turn.sessions.set_resume_id(conv, self.key, json.dumps(dict(self.now, session=session_id)))
            except Exception:  # noqa: BLE001
                pass


_AUTH = re.compile(r"not logged in|log ?in|sign ?in|unauthori[sz]ed|authenticat|api key|credential|\b401\b|\b403\b", re.I)
_RATE = re.compile(r"rate.?limit|usage limit|quota|too many requests|\b429\b|billing|credit|overloaded", re.I)
_NET = re.compile(r"network|connect|timed? ?out|unreachable|stream (closed|disconnected|error)|\b5\d\d\b|dns|reset", re.I)


def classify(message: str, what: str = "the agent CLI", code: str = "CLI_ERROR") -> DriverError:
    """A CLI's own failure text as a protocol error kind (the CLI talks to its provider; we only see its words)."""
    msg = " ".join(str(message or "").split())[:300] or f"{what} failed"
    if _AUTH.search(msg):
        return DriverError("auth", f"{what}: {msg}", code="CLI_AUTH", retryable=False,
                           fix="run the CLI once in a terminal and sign in with its own login")
    if _RATE.search(msg):
        return DriverError("rate", f"{what}: {msg}", code="CLI_RATE")
    if _NET.search(msg):
        return DriverError("network", f"{what}: {msg}", code="CLI_NETWORK", retryable=True)
    return DriverError("protocol", f"{what}: {msg}", code=code, retryable=False)


def earlier_turns(turn, limit: int = 12, chars: int = 6000) -> str:
    """Earlier turns of the conversation as text, for a CLI session that starts fresh (no resume id, or another
    account answered before). '' when there are none."""
    msgs = turn.history(limit)
    if not msgs:
        return ""
    lines = [("User: " if m["role"] == "user" else "You: ") + m["text"].strip() for m in msgs]
    text = "\n".join(lines)
    return "[earlier in this conversation]\n" + text[-chars:] + "\n[end of earlier conversation]\n\n"


def cli_tool_name(name: str) -> str:
    return "cli:" + str(name or "tool")[:60]


def text_of_content(content) -> str:
    """Plain text of an MCP-style content value (string, block, or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or "")
    if isinstance(content, list):
        return "\n".join(text_of_content(c) for c in content if c)
    return ""
