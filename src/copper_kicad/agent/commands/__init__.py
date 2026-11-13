"""No-model commands: deterministic jobs the pane can run without any account. SEAM (HOST_PROTOCOL.md 8.2).

A command is a function ``fn(turn, args) -> None`` that drives the same ``Turn`` a model driver gets: it emits
text with ``turn.text``, runs engine tools with ``turn.call_tool`` (events and live routing included) and may
use ``turn.findings`` / ``turn.reveal`` / ``turn.host``. It returns when done; errors follow the driver
contract (raise ``DriverError`` or let a tool result carry the failure).

Add a command in its own module in this package (imported automatically):

    from . import command

    @command("verify", "Run ERC / DRC and list what is wrong", usage="/verify [erc|drc|all]",
             args={"what": {"type": "string", "enum": ["erc", "drc", "all"]}}, positional=("what",))
    def verify(turn, args):
        out = turn.call_tool("verify", {"what": args.get("what", "all")})
        ...

    COMMANDS: {name: Command}
    command(name, title, *, usage='', args=None, required=(), positional=(), documents=(...), needs_project=True)
    describe() -> [{'name','title','usage','args','required','documents','needs_project'}]     hello.commands
    parse('/name rest of line') -> (name, args)        JSON object after the name, or positional words
    run(turn, name, args) -> None                      validates args, then calls the command
"""
from __future__ import annotations

import importlib
import json
import pkgutil
import traceback
from dataclasses import dataclass, field

from ..drivers.base import DriverError

DOCUMENTS = ("schematic", "board")
COMMANDS: dict = {}
LOAD_ERRORS: dict = {}
_loaded = False


@dataclass
class Command:
    name: str
    title: str
    fn: object
    usage: str = ""
    args: dict = field(default_factory=dict)        # {arg name: JSON-schema fragment}
    required: tuple = ()
    positional: tuple = ()                          # arg names filled from the words after '/name'; the last takes the rest
    documents: tuple = DOCUMENTS                    # where the pane offers it
    needs_project: bool = True

    def describe(self) -> dict:
        return {"name": self.name, "title": self.title, "usage": self.usage or f"/{self.name}", "args": self.args,
                "required": list(self.required), "documents": list(self.documents), "needs_project": self.needs_project}


def command(name: str, title: str, *, usage: str = "", args: dict | None = None, required=(), positional=(),
            documents=DOCUMENTS, needs_project: bool = True):
    def deco(fn):
        COMMANDS[name] = Command(name, title, fn, usage, dict(args or {}), tuple(required), tuple(positional),
                                 tuple(documents), needs_project)
        return fn
    return deco


def load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for m in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        try:
            importlib.import_module(f"{__name__}.{m.name}")
        except Exception:  # noqa: BLE001
            LOAD_ERRORS[f"commands.{m.name}"] = traceback.format_exc(limit=3).strip().splitlines()[-1]


def get(name):
    load()
    return COMMANDS.get(name)


def describe() -> list[dict]:
    load()
    return [c.describe() for c in COMMANDS.values()]


def parse(text: str) -> tuple[str, dict]:
    """'/verify drc' -> ('verify', {'what': 'drc'});  '/tool {"tool": "sch_read"}' -> ('tool', {...}).
    An unknown name is returned as is (run() reports it)."""
    body = (text or "").strip().lstrip("/")
    name, _, rest = body.partition(" ")
    rest = rest.strip()
    cmd = get(name)
    if not rest:
        return name, {}
    if rest.startswith("{"):
        try:
            obj = json.loads(rest)
            if isinstance(obj, dict):
                return name, obj
        except ValueError:
            pass
    if cmd is None or not cmd.positional:
        return name, {"text": rest}
    words = rest.split(None, len(cmd.positional) - 1)
    return name, dict(zip(cmd.positional, words))


def run(turn, name: str, args: dict | None = None) -> None:
    cmd = get(name)
    if cmd is None:
        raise DriverError("engine", f"unknown command /{name}. Commands: " + " ".join("/" + n for n in COMMANDS),
                          code="UNKNOWN_COMMAND", retryable=False)
    if cmd.needs_project and not turn.project_dir:
        raise DriverError("engine", f"/{name} needs an open project", code="NO_PROJECT", retryable=False)
    from ...mcp import registry
    spec = registry.Tool(f"/{name}", cmd.fn, cmd.title,
                         {"type": "object", "properties": cmd.args, "required": list(cmd.required)})
    try:
        clean = registry.validate_args(spec, dict(args or {}))
    except registry.ToolError as e:
        raise DriverError("engine", f"{e.message}. Usage: {cmd.usage or '/' + name}", code="BAD_ARGUMENT",
                          retryable=False, fix=e.fix) from None
    cmd.fn(turn, clean)
