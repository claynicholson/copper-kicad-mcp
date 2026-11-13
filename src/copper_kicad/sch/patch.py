"""uuid patches for schematic sheets: the form in which an engine edit reaches the live document.

SEAM (docs/internal/HOST_PROTOCOL.md section 7). The signatures below are the contract; change them only together with
HOST_PROTOCOL.md, host/live.py and tests/fakehost.py.

    diff_sheet(before, after) -> SheetDiff dict                 one sheet file, text before / after an engine edit
    diff_project(before, after, sheets) -> ProjectPatch dict    every sheet of a checkout; feeds host.apply
    parse_add(add) -> [item node]                               raises PatchError
    fragment_problems(add) -> [str]                             why `add` is not a paste-parser fragment ([] = fine)
    clipboard_text(add, lib_symbols=()) -> str                  the text KiCad's Copy would have produced
    wrap_sheet(add, lib_symbols=(), ...) -> str                 a minimal complete sheet around `add` (parser check)
    apply_sheet(text, patch, lib_symbols=()) -> str             REFERENCE APPLIER, one sheet file; raises PatchError
    apply_project(texts, patches, lib_symbols=()) -> {file: text}    REFERENCE APPLIER, a whole host.apply
    netlist_hash(netlist) -> 'sha256:<hex>'     netlist_lines(netlist) -> [str]     expect(netlist) -> dict

The model
---------
A sheet file is ``(kicad_sch <header nodes> (lib_symbols ...) <items> (sheet_instances ...) ...)``.
An ITEM is a top-level node of one of the kinds in ITEM_HEADS that carries a uuid (``item_uuid``): a direct
``(uuid X)`` child, or for ``rule_area`` the uuid of its ``polyline``. Items are keyed by that uuid, nothing else:

  * an item only in ``after``            -> its text goes to ``add``
  * an item only in ``before``           -> its uuid goes to ``remove``
  * an item in both whose trees differ   -> uuid in ``remove`` AND its new text in ``add`` (same uuid: this is how
                                            a changed symbol keeps its link to the board)
  * a ``lib_symbols`` entry that is new or different -> its full ``(symbol "Lib:Name" ...)`` text in ``lib_symbols``

Two trees "differ" when they are not equal after reading numbers as numbers (``1.27`` == ``1.2700``). White
space, the quoting of a token and the order of top-level items are never a difference.

``add`` is plain KiCad s-expression text, the items one after another with no enclosing node: exactly the item
part of what eeschema puts on the clipboard (SCH_IO_KICAD_SEXPR::Format(SCH_SELECTION*, ...): an optional
``(lib_symbols ...)`` node followed by the items) and what SCH_IO_KICAD_SEXPR::LoadContent ->
SCH_IO_KICAD_SEXPR_PARSER::ParseSchematic(sheet, aIsCopyableOnly = true) reads. See HOST_PROTOCOL.md 7.5 for
what was read in the KiCad 10.0 sources and what is still unproven.

What a patch does NOT carry (each is reported in ``skipped``, the rest of the change still applies):
  * header nodes: paper, title_block, the sheet's own uuid, bus_alias, and the trailing sheet_instances /
    symbol_instances / embedded_fonts / embedded_files. The paste parser rejects or cannot place them;
  * top-level items without a uuid (files older than KiCad 7; tables before KiCad 9): they cannot be addressed;
  * items whose uuid occurs twice in one sheet (a corrupt file): they cannot be addressed either;
  * sheet files that did not exist before (``new_sheets``): host/live.py sends those through ``files``.

Instance data is carried verbatim. A symbol's ``(instances (project "p" (path "/root/sheet" (reference "R1")
(unit 1))))`` and a sheet's ``(instances ... (page "2"))`` hold FULL paths, as in a file, not the
selection-relative paths eeschema writes for its own clipboard. For a sheet file that is used several times the
item therefore lists one path per use and each use keeps its own reference.
"""
from __future__ import annotations

import hashlib

from .. import sexpr

# top-level kinds the paste parser accepts as items (ParseSchematic's switch, minus the header-only tokens)
ITEM_HEADS = frozenset((
    "symbol", "sheet", "junction", "no_connect", "bus_entry", "wire", "bus", "polyline", "arc", "circle",
    "rectangle", "bezier", "rule_area", "text", "label", "global_label", "hierarchical_label", "directive_label",
    "netclass_flag", "text_box", "table", "image", "group"))
_TAIL = ("sheet_instances", "symbol_instances", "embedded_fonts", "embedded_files")   # nodes that follow the items
_HEADER_IGNORE = ("version", "generator", "generator_version", "lib_symbols")        # never reported as skipped
FILE_VERSION = 20260306         # KiCad 10.0 schematic format: the version `add` text is written in


class PatchError(Exception):
    """A patch cannot be applied. ``code``: PARSE | UNKNOWN_ITEM | DUPLICATE_ITEM | NO_SHEET. Nothing was changed."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ── reading ───────────────────────────────────────────────────────────────
def _parse(text: str, what: str) -> list:
    try:
        tree = sexpr.loads(text)
    except (ValueError, IndexError) as e:
        raise PatchError("PARSE", f"{what}: {e}") from None
    if not isinstance(tree, list) or not tree or tree[0] != "kicad_sch":
        raise PatchError("PARSE", f"{what}: not a kicad_sch tree")
    return tree


def _head(node):
    return node[0] if isinstance(node, list) and node and isinstance(node[0], str) else None


def item_uuid(node) -> str | None:
    """The uuid of a top-level item node, or None for header nodes and uuid-less items."""
    h = _head(node)
    if h is None or h not in ITEM_HEADS:
        return None
    u = sexpr.value(node, "uuid")
    if u is None and h == "rule_area":                       # (rule_area (polyline (pts ...) ... (uuid X)))
        poly = sexpr.find(node, "polyline")
        u = sexpr.value(poly, "uuid") if poly is not None else None
    return str(u) if u is not None and not isinstance(u, list) else None


def _index(tree: list) -> tuple[dict, list, list]:
    """(items by uuid in document order, anonymous item nodes, uuids that occur more than once)."""
    items, anon, dup = {}, [], []
    for n in tree[1:]:
        if _head(n) not in ITEM_HEADS:
            continue
        u = item_uuid(n)
        if u is None:
            anon.append(n)
        elif u in items:
            dup.append(u)
        else:
            items[u] = n
    for u in dup:                                            # ambiguous: not addressable, treated as anonymous
        items.pop(u, None)
    return items, anon, sorted(set(dup))


def _items(tree: list) -> dict:
    """{uuid: node} of a sheet tree's addressable items, in document order."""
    return _index(tree)[0]


def _lib_symbols(tree: list) -> dict:
    ls = sexpr.find(tree, "lib_symbols")
    return {str(n[1]): n for n in (ls or [])[1:] if isinstance(n, list) and len(n) > 1 and n[0] == "symbol"}


def _key(x):
    """A tree with numbers read as numbers, for comparing what two writers produced."""
    if isinstance(x, list):
        return tuple(_key(e) for e in x)
    if isinstance(x, sexpr.Sym):
        try:
            return ("n", float(x))
        except ValueError:
            return ("s", str(x))
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return ("n", float(x))
    return ("q", str(x))


def _same(a, b) -> bool:
    return a is b or a == b or _key(a) == _key(b)


def _ref(node) -> str | None:
    """The Reference property of a symbol item (the default instance's designator), else None."""
    if _head(node) == "symbol":
        for p in sexpr.find_all(node, "property"):
            if len(p) > 2 and p[1] == "Reference":
                return str(p[2])
    return None


def _refs(node) -> list:
    """Reference designators a symbol item answers to: its Reference property and every instance reference."""
    if _head(node) != "symbol":
        return []
    out = [str(p[2]) for p in sexpr.find_all(node, "property") if len(p) > 2 and p[1] == "Reference"]
    for proj in sexpr.find_all(sexpr.find(node, "instances") or [], "project"):
        for path in sexpr.find_all(proj, "path"):
            r = sexpr.value(path, "reference")
            if r is not None:
                out.append(str(r))
    return out


def _lib_names(node) -> list:
    """Names under which a symbol item looks up its definition in lib_symbols (lib_name wins in KiCad)."""
    if _head(node) != "symbol":
        return []
    return [str(v) for v in (sexpr.value(node, "lib_name"), sexpr.value(node, "lib_id")) if v is not None]


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _points(node) -> list:
    pts = []
    at = sexpr.find(node, "at")
    if at is not None and len(at) > 2 and _num(at[1]) is not None and _num(at[2]) is not None:
        pts.append((_num(at[1]), _num(at[2])))
        size = sexpr.find(node, "size")
        if _head(node) in ("sheet", "text_box", "image") and size is not None and len(size) > 2 \
                and _num(size[1]) is not None and _num(size[2]) is not None:
            pts.append((pts[0][0] + _num(size[1]), pts[0][1] + _num(size[2])))
    holders = [node] + ([sexpr.find(node, "polyline")] if _head(node) == "rule_area" else [])
    for holder in holders:
        for xy in sexpr.find_all(sexpr.find(holder or [], "pts") or [], "xy"):
            if len(xy) > 2 and _num(xy[1]) is not None and _num(xy[2]) is not None:
                pts.append((_num(xy[1]), _num(xy[2])))
        for name in ("start", "end", "mid", "center"):
            p = sexpr.find(holder or [], name)
            if p is not None and len(p) > 2 and _num(p[1]) is not None and _num(p[2]) is not None:
                pts.append((_num(p[1]), _num(p[2])))
    return pts


def _box(nodes) -> list | None:
    pts = [p for n in nodes for p in _points(n)]
    if not pts:
        return None
    return [round(min(p[0] for p in pts), 4), round(min(p[1] for p in pts), 4),
            round(max(p[0] for p in pts), 4), round(max(p[1] for p in pts), 4)]


def _multiset(nodes) -> dict:
    out = {}
    for n in nodes:
        k = _key(n)
        out[k] = out.get(k, 0) + 1
    return out


# ── diff ──────────────────────────────────────────────────────────────────
def diff_sheet(before: str, after: str) -> dict:
    """Diff one sheet file's text before and after an engine edit.

    -> {'remove': [uuid],            removed + changed items, in document order of `before`
        'add': str,                  s-expression text of added + changed items, in document order of `after`
        'lib_symbols': [str],        new or changed library symbol definitions, each a complete (symbol ...)
        'lib_names': [str],          their names, same order
        'lib_unused': [str],         those of them that no item on the sheet uses (diff_project leaves them out)
        'added': [uuid], 'changed': [uuid], 'removed': [uuid],
        'kinds': {kind: n},          item kinds touched ('symbol', 'wire', ...), added + changed + removed
        'refs': [str],               reference designators of added / changed / removed symbols (no #PWR)
        'region': [x0, y0, x1, y1] | None,     mm, bounding box of everything touched
        'skipped': [str]}            what the patch does NOT carry (see the module docstring)
    An unchanged sheet gives remove == [] and add == ''. ``before`` may be '' for a sheet that did not exist."""
    a_tree = _parse(after, "after")
    existed = bool(before.strip())
    b_tree = _parse(before, "before") if existed else ["kicad_sch"]
    (b, b_anon, b_dup), (a, a_anon, a_dup) = _index(b_tree), _index(a_tree)
    removed = [u for u in b if u not in a]
    added = [u for u in a if u not in b]
    changed = [u for u in a if u in b and not _same(a[u], b[u])]
    bl, al = _lib_symbols(b_tree), _lib_symbols(a_tree)
    lib_names = [name for name, n in al.items() if name not in bl or not _same(bl[name], n)]
    touched = [a[u] for u in added + changed] + [b[u] for u in removed + changed]
    kinds: dict = {}
    for n in [a[u] for u in added + changed] + [b[u] for u in removed]:
        kinds[str(n[0])] = kinds.get(str(n[0]), 0) + 1
    refs = sorted({r for n in touched for r in _refs(n) if r and not r.startswith("#")})

    skipped = []
    if existed:
        bh, ah = ({str(n[0]): n for n in t[1:] if _head(n) and _head(n) not in ITEM_HEADS} for t in (b_tree, a_tree))
        for name in list(ah) + [k for k in bh if k not in ah]:
            if name in _HEADER_IGNORE or name in skipped:
                continue
            if name not in bh or name not in ah or not _same(bh[name], ah[name]):
                skipped.append(name)
        skipped = [f"header node '{name}' differs" for name in skipped]
        bv, av = sexpr.value(b_tree, "version"), sexpr.value(a_tree, "version")
        if bv != av:
            skipped.append(f"file format version changed {bv} -> {av}")
    if _multiset(b_anon) != _multiset(a_anon):
        skipped.append(f"{max(len(a_anon), len(b_anon))} item(s) without a uuid differ")
    for u in sorted(set(a_dup) | set(b_dup)):
        skipped.append(f"uuid {u} occurs more than once")
    used = {name for n in a.values() for name in _lib_names(n)}
    for u in added + changed:
        missing = [nm for nm in _lib_names(a[u])[:1] if nm not in al]
        if missing and _head(a[u]) == "symbol":
            skipped.append(f"symbol {u} uses '{missing[0]}', which has no lib_symbols entry")
    rm, ad = set(removed) | set(changed), set(added) | set(changed)
    return {"remove": [u for u in b if u in rm],
            "add": "\n".join(sexpr.dumps(a[u]) for u in a if u in ad),
            "lib_symbols": [sexpr.dumps(al[name]) for name in lib_names], "lib_names": lib_names,
            "lib_unused": [name for name in lib_names if name not in used],
            "added": added, "changed": changed, "removed": removed, "kinds": kinds,
            "refs": refs, "region": _box(touched), "skipped": skipped}


def diff_project(before: dict, after: dict, sheets: list[dict] | None = None) -> dict:
    """Diff every sheet of a checkout.

    before / after: {project-relative file name (forward slashes): text}. ``sheets`` is host.checkout's list
    ``[{'sheet_path', 'file'}]`` and supplies the sheet_path of each file (default '/'; for a file that is used
    several times, the first path listed).

    -> {'patches': [{'sheet_path', 'file', 'remove': [uuid], 'add': str, 'libs'?: [name]}],
                                             only sheets that changed. 'libs' names the definitions (from the
                                             top-level 'lib_symbols') this sheet's cache must receive
        'lib_symbols': [str],                union over the sheets, one definition per symbol name
        'counts': {'added', 'changed', 'removed'},
        'refs': [str], 'region': [x0, y0, x1, y1] | None, 'skipped': [str],
        'new_sheets': [file],                files in `after` that are not in `before`: not patchable, the
                                             caller sends them as side files (host.apply `files`)
        'empty': bool}                       True when there is nothing to apply
    The 'patches' / 'lib_symbols' values go into host.apply unchanged."""
    paths: dict = {}
    for s in sheets or []:
        if isinstance(s, dict) and s.get("file"):
            paths.setdefault(str(s["file"]), s.get("sheet_path") or "/")
    out = {"patches": [], "lib_symbols": [], "counts": {"added": 0, "changed": 0, "removed": 0}, "refs": [],
           "region": None, "skipped": [], "new_sheets": [], "empty": True}
    libs, boxes = {}, []
    for f in sorted(after):
        if f not in before:
            out["new_sheets"].append(f)
            continue
        if before[f] == after[f]:
            continue
        d = diff_sheet(before[f], after[f])
        for k in ("added", "changed", "removed"):
            out["counts"][k] += len(d[k])
        out["refs"] = sorted(set(out["refs"]) | set(d["refs"]))
        out["skipped"] += [f"{f}: {s}" for s in d["skipped"]]
        if d["region"]:
            boxes.append(d["region"])
        names = []
        for name, text in zip(d["lib_names"], d["lib_symbols"]):
            if name in d["lib_unused"]:
                continue                              # a cache entry nothing on the sheet uses: not worth a patch
            if name in libs and libs[name] != text and not _same(sexpr.loads(libs[name]), sexpr.loads(text)):
                out["skipped"].append(f"{f}: library symbol '{name}' differs from the one sent for another sheet")
                continue
            libs.setdefault(name, text)
            names.append(name)
        if d["remove"] or d["add"] or names:
            p = {"sheet_path": paths.get(f, "/"), "file": f, "remove": d["remove"], "add": d["add"]}
            if names:
                p["libs"] = names
            out["patches"].append(p)
    out["lib_symbols"] = list(libs.values())
    if boxes:
        out["region"] = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
    out["empty"] = not out["patches"]
    return out


# ── the `add` fragment ────────────────────────────────────────────────────
def _loads_many(text: str) -> list:
    """Every top-level node of a text that holds several s-expressions one after another."""
    return sexpr.loads("(" + (text or "") + "\n)")


def parse_add(add: str) -> list:
    """The item nodes of a patch's ``add`` text. Raises PatchError('PARSE')."""
    try:
        nodes = _loads_many(add)
    except (ValueError, IndexError) as e:
        raise PatchError("PARSE", f"add: {e}") from None
    for n in nodes:
        if _head(n) not in ITEM_HEADS:
            raise PatchError("PARSE", f"add: every top-level node must be a schematic item, got {str(n)[:60]!r}")
        if item_uuid(n) is None:
            raise PatchError("PARSE", f"add: {n[0]} item without a uuid")
    return nodes


def fragment_problems(add: str, lib_names=None) -> list[str]:
    """Reasons why ``add`` is not a standalone fragment the paste parser takes; [] when it is fine.

    Checked: the text is a sequence of complete lists (nothing before, between or after them but white space,
    parentheses balanced with quotes respected); every list is an item kind ParseSchematic(aIsCopyableOnly) has
    a case for and none of the kinds it refuses (paper, page, title_block, bus_alias) or cannot place without a
    SCHEMATIC (embedded_fonts, embedded_files); every item has a uuid, and no uuid twice; symbols have lib_id
    and at; with ``lib_names`` (the names available in lib_symbols), every symbol's definition is among them."""
    probs = []
    depth, in_str, esc, started = 0, False, False, False
    for i, ch in enumerate(add or ""):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            if depth == 0:
                probs.append(f"text outside a list at offset {i}")
                break
            in_str = True
        elif ch == "(":
            depth += 1
            started = True
        elif ch == ")":
            depth -= 1
            if depth < 0:
                probs.append(f"unbalanced ')' at offset {i}")
                break
        elif depth == 0 and not ch.isspace():
            probs.append(f"text outside a list at offset {i}")
            break
    if not probs and (depth != 0 or in_str):
        probs.append("the text ends inside a list or a string")
    if probs:
        return probs
    if not started:
        return []
    try:
        nodes = _loads_many(add)
    except (ValueError, IndexError) as e:
        return [f"not an s-expression: {e}"]
    seen = set()
    for n in nodes:
        h = _head(n)
        if h not in ITEM_HEADS:
            probs.append(f"'{h}' is not an item the paste parser accepts")
            continue
        u = item_uuid(n)
        if u is None:
            probs.append(f"{h} without a uuid")
        elif u in seen:
            probs.append(f"uuid {u} twice")
        seen.add(u)
        if h == "symbol":
            if sexpr.value(n, "lib_id") is None or sexpr.find(n, "at") is None:
                probs.append(f"symbol {u} lacks lib_id or at")
            names = _lib_names(n)
            if lib_names is not None and names and names[0] not in lib_names:
                probs.append(f"symbol {u}: no library symbol '{names[0]}' available")
    return probs


def clipboard_text(add: str, lib_symbols=()) -> str:
    """``add`` as eeschema's Copy would have written it: ``(lib_symbols ...)`` first when there are definitions,
    then the items. This is the text to paste into a stock KiCad by hand, and what a host hands to LoadContent."""
    head = ""
    if lib_symbols:
        head = "(lib_symbols\n" + "\n".join("\t" + t.replace("\n", "\n\t") for t in lib_symbols) + "\n)\n"
    return head + (add or "") + ("\n" if add and not add.endswith("\n") else "")


def wrap_sheet(add: str, lib_symbols=(), uuid: str = "00000000-0000-4000-8000-000000000001",
               version: int = FILE_VERSION, generator_version: str = "10.0") -> str:
    """A minimal complete sheet file around a fragment, so KiCad's file parser (the same item parsers as the paste
    parser) can be run on it with kicad-cli. Not a patch target: the instance paths inside `add` are kept as is."""
    libs = "\n".join("\t\t" + t.replace("\n", "\n\t\t") for t in lib_symbols)
    body = "\n".join("\t" + line for line in (add or "").split("\n")) if add else ""
    return (f'(kicad_sch\n\t(version {version})\n\t(generator "eeschema")\n\t(generator_version "{generator_version}")\n'
            f'\t(uuid "{uuid}")\n\t(paper "A3")\n\t(lib_symbols' + ("\n" + libs + "\n\t" if libs else "") + ")\n"
            + (body + "\n" if body else "")
            + '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n\t(embedded_fonts no)\n)\n')


# ── apply (the reference a host must match; used by tests/fakehost.py and the fixture tests) ──
def _parse_libs(lib_symbols) -> dict:
    out = {}
    for t in lib_symbols or ():
        try:
            n = sexpr.loads(t)
        except (ValueError, IndexError) as e:
            raise PatchError("PARSE", f"lib_symbols: {e}") from None
        if not isinstance(n, list) or len(n) < 2 or n[0] != "symbol" or isinstance(n[1], list):
            raise PatchError("PARSE", "lib_symbols: every entry must be one (symbol \"Lib:Name\" ...)")
        out[str(n[1])] = n
    return out


def apply_sheet(text: str, patch: dict, lib_symbols=()) -> str:
    """Apply one sheet patch to a sheet file's text and return the new text. All or nothing.

    patch: {'remove': [uuid], 'add': str, 'libs'?: [name]} (a diff_sheet result is one).
    lib_symbols: [(symbol "Lib:Name" ...) text]. A definition is merged into this sheet's lib_symbols (replacing
    the entry of that name, else appended) when the patch names it in 'libs', or, when the patch has no 'libs',
    when a symbol on the resulting sheet uses that name.
    A uuid in both ``remove`` and ``add`` is replaced in place (document order kept); other added items go after
    the last existing item, before sheet_instances. Raises PatchError: PARSE (bad s-expression or not an item),
    UNKNOWN_ITEM (a uuid to remove is not in the sheet), DUPLICATE_ITEM (an added uuid would exist twice)."""
    tree = _parse(text, "sheet")
    new = parse_add(patch.get("add") or "")
    libs = _parse_libs(lib_symbols)
    have, _, dup = _index(tree)
    remove = [str(u) for u in patch.get("remove") or []]
    if len(set(remove)) != len(remove):
        raise PatchError("PARSE", "remove: a uuid is listed twice")
    missing = [u for u in remove if u not in have]
    if missing:
        raise PatchError("UNKNOWN_ITEM", f"uuid(s) to remove are not in the sheet: {missing[:5]}")
    rm = set(remove)
    new_by = {}
    for n in new:
        u = item_uuid(n)
        if u in new_by or (u in have and u not in rm) or u in dup:
            raise PatchError("DUPLICATE_ITEM", f"uuid {u} would exist twice")
        new_by[u] = n
    out, placed = [tree[0]], set()
    for n in tree[1:]:
        u = item_uuid(n)
        if u in rm and have.get(u) is n:
            if u in new_by:
                out.append(new_by[u])
                placed.add(u)
            continue
        out.append(n)
    rest = [n for u, n in new_by.items() if u not in placed]
    if rest:
        idx = next((i for i, n in enumerate(out) if _head(n) in _TAIL), len(out))
        out[idx:idx] = rest
    names = patch.get("libs")
    if names is None:
        used = {nm for n in out[1:] for nm in _lib_names(n)}
        names = [nm for nm in libs if nm in used]
    else:
        unknown = [nm for nm in names if nm not in libs]
        if unknown:
            raise PatchError("PARSE", f"libs: the patch names {unknown[:3]} but no such definition was sent")
    if names:
        ls = sexpr.find(out, "lib_symbols")
        if ls is None:
            ls = [sexpr.Sym("lib_symbols")]
            idx = next((i for i, n in enumerate(out) if i and (_head(n) in ITEM_HEADS or _head(n) in _TAIL)), len(out))
            out.insert(idx, ls)
        for nm in names:
            for i, old in enumerate(ls):
                if isinstance(old, list) and len(old) > 1 and old[0] == "symbol" and old[1] == nm:
                    ls[i] = libs[nm]
                    break
            else:
                ls.append(libs[nm])
    return sexpr.dumps(out) + "\n"


def apply_project(texts: dict, patches, lib_symbols=()) -> dict:
    """Apply a whole host.apply to sheet texts: {file: text} in, {file: new text} out for the files that changed.

    All or nothing: every patch is applied in memory and any PatchError leaves the caller with nothing to write.
    Several patches for the same file are applied in order. A patch for a file that is not in ``texts`` raises
    PatchError('NO_SHEET')."""
    out: dict = {}
    for p in patches or []:
        f = str(p.get("file") or "")
        if f not in texts:
            raise PatchError("NO_SHEET", f"no sheet file {f!r} in the open schematic")
        out[f] = apply_sheet(out.get(f, texts[f]), p, lib_symbols)
    return out


def patch_counts(patches) -> dict:
    """{'added', 'changed', 'removed'} of a patch list, the way a host counts them (uuid in both = changed)."""
    c = {"added": 0, "changed": 0, "removed": 0}
    for p in patches or []:
        added = {item_uuid(n) for n in parse_add(p.get("add") or "")}
        removed = {str(u) for u in p.get("remove") or []}
        c["changed"] += len(added & removed)
        c["added"] += len(added - removed)
        c["removed"] += len(removed - added)
    return c


# ── netlist identity ──────────────────────────────────────────────────────
def netlist_lines(netlist: dict) -> list[str]:
    """The sorted lines netlist_hash hashes: one per net with at least one real pin, ``REF.PIN,REF.PIN,...``."""
    lines = []
    for nodes in (netlist.get("nets") or {}).values():
        pins = sorted({f"{n['ref']}.{n['pin']}" for n in nodes if not str(n.get("ref", "")).startswith("#")})
        if pins:
            lines.append(",".join(pins))
    return sorted(lines)


def netlist_hash(netlist: dict) -> str:
    """Canonical hash of a netlist in sch/netlist.export_netlist()'s shape ({'nets': {name: [{'ref','pin'}]}}).

    One line per net that has at least one real pin: the pins as ``REF.PIN`` sorted and joined by ','. Pins of
    references starting with '#' (power symbols, flags) are left out. Net NAMES are not part of the hash (an
    unnamed net's generated name depends on which pin KiCad picks). Lines are sorted, joined with '\\n', UTF-8
    encoded and hashed with SHA-256. Two schematics with the same hash connect the same pins. A multi-unit
    symbol contributes the pins of all its units under its one reference, by pin NUMBER."""
    return "sha256:" + hashlib.sha256("\n".join(netlist_lines(netlist)).encode("utf-8")).hexdigest()


def expect(netlist: dict) -> dict:
    """The ``expect`` object of host.apply for a netlist exported from the edited shadow."""
    return {"netlist_hash": netlist_hash(netlist)}
