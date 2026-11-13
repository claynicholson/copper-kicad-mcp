"""Format-preserving JSON / JSONC editing.

Client config files are owned by the user (and often by a running app), so
Copper never round-trips them through ``json.dumps``. It parses the text into
spans and splices only the member it owns. Comments, ordering, indentation,
line endings, BOM and other servers stay byte-identical.

Supports ``//`` and ``/* */`` comments and trailing commas (Zed, VS Code).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


class JsoncError(ValueError):
    pass


@dataclass
class Node:
    kind: str                 # obj | arr | str | num | lit
    start: int
    end: int                  # exclusive
    members: list = field(default_factory=list)   # obj: [Member]; arr: [Node]
    value: object = None      # scalars


@dataclass
class Member:
    key: str
    kstart: int
    kend: int
    val: Node


class _P:
    def __init__(self, s: str):
        self.s = s
        self.i = 0

    def ws(self):
        s, n = self.s, len(self.s)
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n﻿":
                self.i += 1
            elif s.startswith("//", self.i):
                j = s.find("\n", self.i)
                self.i = n if j < 0 else j + 1
            elif s.startswith("/*", self.i):
                j = s.find("*/", self.i + 2)
                if j < 0:
                    raise JsoncError("unterminated /* comment")
                self.i = j + 2
            else:
                break

    def err(self, msg, at: int | None = None):
        i = self.i if at is None else at
        line = self.s.count("\n", 0, i) + 1
        col = i - (self.s.rfind("\n", 0, i) + 1) + 1
        raise JsoncError(f"{msg} at line {line}, column {col}")

    def value(self) -> Node:
        self.ws()
        if self.i >= len(self.s):
            self.err("unexpected end of input")
        c = self.s[self.i]
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c == '"':
            st = self.i
            v = self.string()
            return Node("str", st, self.i, value=v)
        m = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?").match(self.s, self.i)
        if m and m.end() > self.i:
            st = self.i
            self.i = m.end()
            return Node("num", st, self.i, value=json.loads(m.group(0)))
        for lit, v in (("true", True), ("false", False), ("null", None)):
            if self.s.startswith(lit, self.i):
                st = self.i
                self.i += len(lit)
                return Node("lit", st, self.i, value=v)
        self.err(f"unexpected character {c!r}")

    def string(self) -> str:
        m = re.compile(r'"(?:[^"\\\n]|\\.)*"').match(self.s, self.i)
        if not m:
            self.err("unterminated string")
        try:
            v = json.loads(m.group(0))
        except ValueError as e:
            # json reports a position inside the token; report it in the file instead,
            # with the hint every hand-edited Windows path needs.
            pos = self.i + int(getattr(e, "pos", 0) or 0)
            msg = getattr(e, "msg", None) or str(e)
            if "escape" in msg.lower():
                msg = (f"{msg} {self.s[pos:pos + 2]} (backslashes in JSON strings must be doubled, "
                       r"C:\\Users\\me, or written as forward slashes, C:/Users/me)")
            self.err(msg, at=pos)
        self.i = m.end()
        return v

    def obj(self) -> Node:
        st = self.i
        self.i += 1
        node = Node("obj", st, st)
        while True:
            self.ws()
            if self.i >= len(self.s):
                self.err("unterminated object")
            if self.s[self.i] == "}":
                self.i += 1
                node.end = self.i
                return node
            if self.s[self.i] != '"':
                self.err("expected key")
            ks = self.i
            key = self.string()
            ke = self.i
            self.ws()
            if self.i >= len(self.s) or self.s[self.i] != ":":
                self.err("expected ':'")
            self.i += 1
            val = self.value()
            node.members.append(Member(key, ks, ke, val))
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1
            elif self.i < len(self.s) and self.s[self.i] == "}":
                continue
            else:
                self.err("expected ',' or '}'")

    def arr(self) -> Node:
        st = self.i
        self.i += 1
        node = Node("arr", st, st)
        while True:
            self.ws()
            if self.i >= len(self.s):
                self.err("unterminated array")
            if self.s[self.i] == "]":
                self.i += 1
                node.end = self.i
                return node
            node.members.append(self.value())
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1
            elif self.i < len(self.s) and self.s[self.i] == "]":
                continue
            else:
                self.err("expected ',' or ']'")


def parse(text: str) -> Node:
    p = _P(text)
    p.ws()
    if p.i >= len(text):
        return Node("obj", -1, -1)    # empty file == {}
    n = p.value()
    p.ws()
    if p.i != len(text):
        p.err("trailing content")
    return n


def to_py(text: str, node: Node | None = None):
    """Plain Python value of a JSONC text (comments/trailing commas allowed)."""
    node = node or parse(text)
    if node.start < 0:
        return {}
    if node.kind == "obj":
        return {m.key: to_py(text, m.val) for m in node.members}
    if node.kind == "arr":
        return [to_py(text, v) for v in node.members]
    return node.value


loads = to_py


def _find(text: str, root: Node, path: list[str]) -> Node | None:
    n = root
    for k in path:
        if n is None or n.kind != "obj":
            return None
        # duplicate keys: the LAST one wins, as in every real JSON parser
        n = next((m.val for m in reversed(n.members) if m.key == k), None)
    return n


def count(text: str, path: list[str]) -> int:
    """How many members named ``path[-1]`` the parent object holds. JSON allows
    duplicate keys and every real parser keeps the LAST one, so a config with
    two ``"copper"`` members must be collapsed before editing it."""
    if not path:
        return 0
    root = parse(text)
    if root.start < 0:
        return 0
    parent = _find(text, root, path[:-1]) if len(path) > 1 else root
    if parent is None or parent.kind != "obj":
        return 0
    return sum(1 for m in parent.members if m.key == path[-1])


def get(text: str, path: list[str]):
    root = parse(text)
    n = _find(text, root, path) if root.start >= 0 else None
    return None if n is None else to_py(text, n)


def _line_indent(text: str, pos: int) -> str:
    ls = text.rfind("\n", 0, pos) + 1
    m = re.match(r"[ \t]*", text[ls:pos])
    return m.group(0) if m else ""


def _nl(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _unit(text: str) -> str:
    m = re.search(r"\n([ \t]+)\S", text)
    if m and m.group(1).startswith("\t"):
        return "\t"
    if m:
        return m.group(1) if len(m.group(1)) <= 4 else "  "
    return "  "


def _dump(value, indent: str, unit: str, nl: str) -> str:
    raw = json.dumps(value, indent=unit if unit == "\t" else len(unit), ensure_ascii=False)
    return raw.replace("\n", nl + indent)


def set_value(text: str, path: list[str], value) -> str:
    """Return ``text`` with the member at ``path`` set to ``value`` (creating
    parent objects as needed). Everything else is preserved verbatim."""
    if not path:
        raise JsoncError("empty path")
    if not text.strip():
        text = "{}" + ("\n" if not text else text)
    root = parse(text)
    if root.start < 0:
        text, root = "{}\n", parse("{}\n")
    if root.kind != "obj":
        raise JsoncError("top-level JSON value is not an object")
    nl, unit = _nl(text), _unit(text)
    # walk down, creating what is missing in a single splice
    node = root
    depth = 0
    for depth, k in enumerate(path):
        nxt = next((m for m in node.members if m.key == k), None)
        if nxt is None:
            break
        if depth == len(path) - 1:
            # replace existing value
            ind = _line_indent(text, nxt.kstart)
            new = _dump(value, ind, unit, nl)
            return text[:nxt.val.start] + new + text[nxt.val.end:]
        if nxt.val.kind != "obj":
            # replace non-object with an object holding the rest of the path
            rest = value
            for kk in reversed(path[depth + 1:]):
                rest = {kk: rest}
            ind = _line_indent(text, nxt.kstart)
            return text[:nxt.val.start] + _dump(rest, ind, unit, nl) + text[nxt.val.end:]
        node = nxt.val
    else:  # pragma: no cover - loop always breaks or returns
        pass
    # node is an object missing key path[depth]
    rest = value
    for kk in reversed(path[depth + 1:]):
        rest = {kk: rest}
    key = path[depth]
    if node.members:
        last = node.members[-1]
        ind = _line_indent(text, last.kstart)
        if text.rfind("\n", node.start, last.kstart) < 0:   # single-line object
            member = f", {json.dumps(key, ensure_ascii=False)}: {json.dumps(rest, ensure_ascii=False)}"
            return text[:last.val.end] + member + text[last.val.end:]
        member = f",{nl}{ind}{json.dumps(key, ensure_ascii=False)}: {_dump(rest, ind, unit, nl)}"
        return text[:last.val.end] + member + text[last.val.end:]
    # empty object: insert right after "{" so a trailing // comment can't swallow it
    outer = _line_indent(text, node.start)
    ind = outer + unit
    inner = text[node.start + 1:node.end - 1]
    member = f"{nl}{ind}{json.dumps(key, ensure_ascii=False)}: {_dump(rest, ind, unit, nl)}"
    tail = inner if inner.strip() else f"{nl}{outer}"
    return text[:node.start + 1] + member + tail + text[node.end - 1:]


def remove(text: str, path: list[str]) -> str:
    """Return ``text`` without the member at ``path`` (no-op if absent)."""
    root = parse(text)
    if root.start < 0:
        return text
    parent = _find(text, root, path[:-1]) if len(path) > 1 else root
    if parent is None or parent.kind != "obj":
        return text
    idx = next((i for i, m in enumerate(parent.members) if m.key == path[-1]), None)
    if idx is None:
        return text
    mem = parent.members[idx]
    n = len(parent.members)
    if n == 1:
        # Keep the object, drop the member. Comments around it stay; with none
        # left the object becomes "{}" again, so `{}` → install → uninstall is
        # byte-identical (set_value inserts into an empty object the same way).
        before = text[parent.start + 1:mem.kstart]
        after = re.sub(r"^[ \t]*,", "", text[mem.val.end:parent.end - 1])
        keep = (before.rstrip(" \t") if before.strip() else "") + (after if after.strip() else "")
        if keep and not keep.strip(" \t\r\n,"):
            keep = ""
        if before.strip() and not after.strip() and "\n" in before:
            keep = keep.rstrip("\r\n") + _nl(text) + _line_indent(text, parent.start)
        return text[:parent.start + 1] + keep + text[parent.end - 1:]
    if idx < n - 1:
        # delete from start of this member's line through the start of the next member's line
        ls = text.rfind("\n", 0, mem.kstart) + 1
        start = ls if not text[ls:mem.kstart].strip() else mem.kstart
        nxt = parent.members[idx + 1]
        ls2 = text.rfind("\n", 0, nxt.kstart) + 1
        end = ls2 if (ls2 > mem.val.end and not text[ls2:nxt.kstart].strip()) else nxt.kstart
        return text[:start] + text[end:]
    prev = parent.members[idx - 1]
    # last member: drop the comma after prev through the end of this value
    tail = mem.val.end
    m = re.compile(r"[ \t]*,").match(text, tail)   # trailing comma (JSONC)
    if m:
        tail = m.end()
    return text[:prev.val.end] + text[tail:]


def dumps_new(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"
