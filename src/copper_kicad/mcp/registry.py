"""Tool registry + result types shared by every Copper tool.

A tool is a plain function ``fn(**args) -> Result | dict | str`` registered
with :func:`tool`. Arguments are validated against a light JSON-schema subset
before the call so tools can trust their inputs.

Conventions every tool follows (the "honest envelope"):
  * mutations report exactly what changed (``changed``) and re-verify with
    KiCad itself (``verify``) instead of claiming success;
  * problems come back as ``findings`` with a ``fix`` hint naming the tool
    call that resolves them;
  * failures raise :class:`ToolError` with a ``fix`` hint — never a silent
    fallback.

Keeping tools/list small (every client loads the whole catalogue up front):
a tool's ``description`` says what it does, its arguments and ONE short
example. Long examples, op catalogues, recipes and edge-case notes go in
``help=`` instead::

    @tool("sch_apply", "Edit the schematic in one batch … (short)", {...},
          help='''## Ops
          {"op":"place", ...}
          ## Examples
          sch_apply(ops=[...])''')

(or ``add_help("sch_apply", text)`` after registration — calling it again
appends). The text is served on demand as the MCP resource
``copper://help/<tool>`` and by the ``copper_help(tool=…)`` tool, the
description gets a one-line pointer to it automatically, and BAD_ARGUMENT
errors name it. ``copper://help`` lists every tool with its help size.
"""
from __future__ import annotations

import base64
import inspect
import json
import os
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable


class ToolError(Exception):
    """A user-facing, actionable failure."""

    def __init__(self, message: str, fix: str | None = None, code: str = "ERROR", data: dict | None = None):
        super().__init__(message)
        self.message = message
        self.fix = fix
        self.code = code
        self.data = data or {}

    def to_dict(self) -> dict:
        d = {"error": self.code, "message": self.message}
        if self.fix:
            d["fix"] = self.fix
        if self.data:
            d.update(self.data)
        return d


@dataclass
class Image:
    path: str | None = None
    data: bytes | None = None
    mime: str = "image/png"
    caption: str | None = None

    def b64(self) -> str:
        raw = self.data if self.data is not None else open(self.path, "rb").read()
        return base64.b64encode(raw).decode("ascii")


@dataclass
class Result:
    """Structured tool result. ``data`` is the JSON payload; ``images`` are
    returned as MCP image content so the model can *see* the design."""
    data: dict = field(default_factory=dict)
    images: list[Image] = field(default_factory=list)
    summary: str | None = None   # one-line human summary placed first
    raw: bool = False            # True = send as built: the wire compactor (mcp/compact.py) leaves it alone

    def to_mcp(self) -> dict:
        content = []
        payload = dict(self.data)
        if self.raw:
            payload["_raw"] = True
        text = json.dumps(payload, separators=(",", ":"), default=str, ensure_ascii=False)
        if self.summary:
            text = self.summary + "\n" + text
        content.append({"type": "text", "text": text})
        for im in self.images:
            try:
                content.append({"type": "image", "data": im.b64(), "mimeType": im.mime})
                if im.caption:
                    content.append({"type": "text", "text": f"[image] {im.caption}" + (f" — saved at {im.path}" if im.path else "")})
            except OSError as e:
                content.append({"type": "text", "text": f"[image unavailable: {e}]"})
        out = {"content": content, "isError": False}
        if payload:
            out["structuredContent"] = json.loads(json.dumps(payload, default=str))
        return out


@dataclass
class Tool:
    name: str
    fn: Callable
    description: str
    schema: dict
    title: str | None = None
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False
    core: bool = False
    group: str = "general"
    max_bytes: int | None = None            # a larger wire cap for explicit detail views (see mcp/compact.py)
    aliases: dict = field(default_factory=dict)   # {wrong argument name: right one}, accepted with a note

    def describe(self) -> dict:
        """The tools/list entry. mcp/catalogue.py keeps it small: shared parameter text, lean annotations,
        and a slim description where one is registered (the full text then lives on the help page)."""
        from . import catalogue
        return catalogue.shape(self, self.description.strip())

    def has_help(self) -> bool:
        from . import catalogue
        return self.name in HELP or catalogue.slim_for(self) is not None


TOOLS: dict[str, Tool] = {}
HELP: dict[str, str] = {}      # tool name -> long-form help (markdown), served as copper://help/<tool>


def add_help(name: str, text: str) -> None:
    """Register long-form help for a tool (examples, op catalogues, recipes) so it stays out of
    tools/list. Safe to call before or after the tool is registered; a second call appends."""
    import textwrap
    first, _, rest = (text or "").partition("\n")      # like a docstring: line 1 may sit right after the quotes
    text = (first.strip() + "\n" + textwrap.dedent(rest)).strip() if first.strip() else textwrap.dedent(rest).strip()
    if text:
        HELP[name] = (HELP[name] + "\n\n" + text) if name in HELP else text


def help_text(name: str) -> str:
    """Everything about one tool as markdown: description, registered help and argument schema."""
    t = TOOLS.get(name)
    if t is None:
        raise ToolError(f"unknown tool '{name}'", code="NOT_FOUND",
                        fix=f"one of: {', '.join(sorted(TOOLS))}")
    props = t.schema.get("properties", {})
    req = set(t.schema.get("required", []))
    lines = [f"# {name}", "", t.description.strip()]
    if name in HELP:
        lines += ["", HELP[name]]
    if props:
        lines += ["", "## Arguments"]
        for k, v in props.items():
            ty = v.get("type", "any")
            if ty == "array" and isinstance(v.get("items"), dict):
                ty = f"array of {v['items'].get('type', 'any')}"
            extra = f" — one of {json.dumps(v['enum'])}" if v.get("enum") else ""
            sub = (v.get("items") or {}).get("enum") if isinstance(v.get("items"), dict) else None
            if sub:
                extra = f" — any of {json.dumps(sub)}"
            lines.append(f"- `{k}` ({ty}{', required' if k in req else ''}){extra}"
                         + (f": {v['description']}" if v.get("description") else ""))
    return "\n".join(lines) + "\n"


def help_index() -> list[dict]:
    return [{"tool": n, "summary": t.description.strip().splitlines()[0][:160], "group": t.group,
             "help_chars": len(HELP.get(n, "")) + (len(t.description) if t.has_help() and n not in HELP else 0),
             "uri": f"copper://help/{n}"} for n, t in TOOLS.items()]


def tool(name: str, description: str, params: dict | None = None, required: list[str] | None = None, *,
         title: str | None = None, read_only=False, destructive=False, idempotent=False,
         open_world=False, core=False, group="general", help: str | None = None,  # noqa: A002
         max_bytes: int | None = None, aliases: dict | None = None):
    """Decorator registering an MCP tool.

    ``params`` maps argument name -> JSON-schema fragment. ``help`` is long-form
    markdown (examples, recipes) kept OUT of tools/list — see the module docstring.
    """
    def deco(fn):
        schema = {"type": "object", "properties": params or {}, "additionalProperties": False}
        if required:
            schema["required"] = required
        TOOLS[name] = Tool(name, fn, description, schema, title, read_only, destructive,
                           idempotent, open_world, core, group, max_bytes, dict(aliases or {}))
        if help:
            add_help(name, help)
        return fn
    return deco


# ── argument validation ───────────────────────────────────────────────────
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
          "array": list, "object": dict}


def _check(value, schema, path):
    t = schema.get("type")
    if t is None:
        return value
    types = t if isinstance(t, list) else [t]
    if "null" in types and value is None:
        return value
    ok = False
    for ty in types:
        py = _TYPES.get(ty)
        if py is None:
            continue
        if ty in ("integer", "number") and isinstance(value, bool):
            continue
        if ty == "integer" and isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, py):
            ok = True
            break
        # forgiving coercions for clients that stringify everything
        if ty in ("integer", "number") and isinstance(value, str):
            try:
                value = int(value) if ty == "integer" else float(value)
                ok = True
                break
            except ValueError:
                pass
        if ty == "boolean" and isinstance(value, str) and value.lower() in ("true", "false"):
            value = value.lower() == "true"
            ok = True
            break
        if ty in ("array", "object") and isinstance(value, str):
            try:
                v2 = json.loads(value)
                if isinstance(v2, py):
                    value, ok = v2, True
                    break
            except ValueError:
                pass
    if not ok:
        ex = {"string": '"text"', "integer": "3", "number": "1.5", "boolean": "true", "array": "[...]", "object": "{...}"}
        raise ToolError(f"argument '{path}' must be {t}, got {type(value).__name__}", code="BAD_ARGUMENT",
                        fix=f"pass '{path}' as a JSON {' or '.join(types)}, e.g. {ex.get(types[0], '…')}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError(f"argument '{path}' must be one of {schema['enum']}, got {value!r}", code="BAD_ARGUMENT",
                        fix=f"use one of {json.dumps(schema['enum'])} for '{path}'")
    if isinstance(value, list) and "items" in schema:
        value = [_check(v, schema["items"], f"{path}[{i}]") for i, v in enumerate(value)]
    return value


# Argument names models reach for by habit, per tool. Only names that can mean nothing else on that tool;
# never a global table (a global file->project would silently retarget a call to another project).
ALIASES: dict[str, dict[str, str]] = {
    "project_create": {"path": "dir"},
    "project_open": {"project": "path", "dir": "path"},
}


def validate_args(t: Tool, args: dict) -> dict:
    if args is not None and not isinstance(args, dict):
        raise ToolError(f"arguments for {t.name} must be a JSON object, got {type(args).__name__}", code="BAD_ARGUMENT",
                        fix=f"call {t.name} with arguments as an object, e.g. {{}}")
    args = dict(args or {})
    props = t.schema.get("properties", {})
    al = dict(ALIASES.get(t.name) or {}, **(t.aliases or {}))
    for k in [k for k in args if k not in props and k in al and al[k] in props]:
        if args.get(al[k]) is None:          # a declared alias: accept it (the result is the same call, spelled right)
            args[al[k]] = args[k]
        del args[k]
    unknown = [k for k in args if k not in props]
    if unknown:
        import difflib
        near = {k: m[0] for k in unknown for m in [difflib.get_close_matches(k, list(props), n=1, cutoff=0.6)] if m}
        hint = "; ".join(f"'{k}' -> did you mean '{v}'?" for k, v in near.items())
        raise ToolError(f"unknown argument(s) {unknown} for {t.name}" + (f" ({hint})" if hint else "")
                        + f"; valid: {sorted(props)}", code="BAD_ARGUMENT",
                        fix=(f"use {', '.join(repr(v) for v in near.values())} instead; " if near else "")
                        + f"call {t.name} with only these arguments: {sorted(props)}")
    for r in t.schema.get("required", []):
        if args.get(r) is None:
            raise ToolError(f"missing required argument '{r}' for {t.name}", code="BAD_ARGUMENT",
                            fix=f"pass '{r}' (see {t.name}'s inputSchema)")
    for k, v in list(args.items()):
        if v is None:
            del args[k]
            continue
        args[k] = _check(v, props[k], k)
    return args


def call_tool(name: str, args: dict, progress: Callable[[float, str], None] | None = None) -> dict:
    t = TOOLS.get(name)
    if t is None:
        return _error_payload(ToolError(f"unknown tool '{name}'", fix="call tools/list (or `copper_status`) to see available tools", code="UNKNOWN_TOOL"))
    try:
        a = validate_args(t, args)
        if progress is not None and "_progress" in inspect.signature(t.fn).parameters:
            a["_progress"] = progress
        _kicad_begin()
        out = t.fn(**a)
        out = _kicad_notes(out)
        if isinstance(out, Result):
            return out.to_mcp()
        if isinstance(out, dict):
            return Result(out).to_mcp()
        return {"content": [{"type": "text", "text": str(out)}], "isError": False}
    except ToolError as e:
        if e.code in ("BAD_ARGUMENT", "BAD_OP") and t.has_help() and "help" not in e.data:
            e.data["help"] = f"copper_help(tool='{name}') shows worked examples"
        return _error_payload(e)
    except Exception as e:  # noqa: BLE001 — surface everything, never crash the server
        tb = traceback.format_exc(limit=6)
        if os.environ.get("COPPER_DEBUG"):
            import sys
            print(tb, file=sys.stderr)
        return _error_payload(ToolError(f"{type(e).__name__}: {e}", code="INTERNAL",
                                        fix="this is a Copper bug: calling again with the same arguments will fail the "
                                            "same way. Try a different approach, or run `copper doctor` and report it "
                                            "with the trace (COPPER_DEBUG=1 prints it)",
                                        data={"retry": False, "trace": tb.splitlines()[-4:]}))


def _kicad_begin():
    """Forget the previous call's project formats: tool calls run on pooled threads, and the KiCad
    selector must never pick an install because of a project some earlier call on this thread touched."""
    try:
        from ..kicad import env
        env.set_project_format(None)
    except Exception:  # noqa: BLE001
        pass


def _kicad_notes(out):
    """Attach queued one-time KiCad compatibility notes (format upgrade, dropped
    kicad-cli flags, version advisories) to a successful tool result."""
    try:
        from ..kicad import compat
        return compat.attach_notes(out)
    except Exception:  # noqa: BLE001 — a note must never cost the result
        return out


def _error_payload(e: ToolError) -> dict:
    d = e.to_dict()
    text = json.dumps(d, separators=(",", ":"), default=str, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": json.loads(json.dumps(d, default=str)), "isError": True}
