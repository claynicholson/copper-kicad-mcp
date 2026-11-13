"""Lossless S-expression reader/writer for KiCad files.

Atoms: Sym (bare token) vs str (quoted). Lists are python lists.
Writer emits KiCad 9/10 house style (tab indent, compact leaf lists).
"""
import re


class Sym(str):
    """Bare (unquoted) token."""
    __slots__ = ()

    def __repr__(self):
        return f"Sym({str.__repr__(self)})"


_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)
_UNESC = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _unescape(s):
    if "\\" not in s:
        return s
    return re.sub(r"\\(.)", lambda m: _UNESC.get(m.group(1), "\\" + m.group(1)), s)


def loads(text):
    stack, cur = [], []
    pos, n = 0, len(text)
    m = _TOKEN.match
    while pos < n:
        mo = m(text, pos)
        if not mo:
            if text[pos:].strip() == "":
                break
            raise ValueError(f"bad token at {pos}: {text[pos:pos+40]!r}")
        pos = mo.end()
        o, c, q, a = mo.groups()
        if o:
            stack.append(cur); cur = []
        elif c:
            done = cur; cur = stack.pop(); cur.append(done)
        elif q is not None:
            cur.append(_unescape(q))
        elif a is not None:
            cur.append(Sym(a))
    if stack:
        raise ValueError("unbalanced parentheses")
    return cur[0] if len(cur) == 1 else cur


def _atom(x):
    if isinstance(x, Sym):
        return str(x)
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, (int,)):
        return str(x)
    if isinstance(x, float):
        s = f"{x:.6f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s
    s = str(x).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def _is_leaf(x):
    return not any(isinstance(e, list) for e in x)


# Heads KiCad always writes on one line even though they contain sublists
_INLINE = {"at", "xy", "pts", "start", "end", "mid", "center", "size", "offset",
           "color", "stroke", "font", "effects", "justify", "uuid", "length", "width",
           "type", "hide", "property_bool"}


def dumps(x, indent=0):
    if not isinstance(x, list):
        return _atom(x)
    if not x:
        return "()"
    head = x[0]
    if _is_leaf(x) or (isinstance(head, str) and head in ("xy", "at", "size", "start", "end", "mid", "center")):
        return "(" + " ".join(dumps(e) for e in x) + ")"
    tabs = "\t" * (indent + 1)
    parts = ["(" + _atom(head)]
    # keep leading atoms on the head line
    i = 1
    while i < len(x) and not isinstance(x[i], list):
        parts[0] += " " + _atom(x[i]); i += 1
    body = []
    for e in x[i:]:
        if isinstance(e, list):
            body.append(tabs + dumps(e, indent + 1))
        else:
            body.append(tabs + _atom(e))
    return parts[0] + "\n" + "\n".join(body) + "\n" + "\t" * indent + ")"


# ── navigation helpers ──────────────────────────────────────────────────────
def head(x):
    return x[0] if isinstance(x, list) and x else None


def find(x, name):
    for e in x:
        if isinstance(e, list) and e and e[0] == name:
            return e
    return None


def find_all(x, name):
    return [e for e in x if isinstance(e, list) and e and e[0] == name]


def value(x, name, default=None):
    e = find(x, name)
    return e[1] if e is not None and len(e) > 1 else default


def set_value(x, name, *vals):
    e = find(x, name)
    if e is None:
        x.append([Sym(name), *vals])
    else:
        e[1:] = list(vals)


if __name__ == "__main__":
    import sys, time
    t = time.time()
    src = open(sys.argv[1], encoding="utf-8").read()
    tree = loads(src)
    t1 = time.time()
    out = dumps(tree) + "\n"
    t2 = time.time()
    again = loads(out)
    print("parse", round(t1 - t, 3), "dump", round(t2 - t1, 3), "roundtrip-equal", again == tree,
          "len", len(src), len(out))
    if len(sys.argv) > 2:
        open(sys.argv[2], "w", encoding="utf-8").write(out)
