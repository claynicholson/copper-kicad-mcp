"""Symbol / footprint library index, search and readers.

* A fast regex scanner indexes every ``.kicad_sym`` in the active lib tables
  (project → global → KiCad template) without a full S-expression parse. The
  index is cached in memory and on disk (``~/.copper/cache``) keyed by file
  mtime+size, so a warm search over KiCad's ~230 stock libraries takes a few ms.
* :func:`read_symbol` parses one symbol (plus its ``extends`` chain), flattens
  derived symbols exactly like eeschema does when it embeds them into a
  schematic, and returns pins / units / properties / bounding boxes.
* :func:`read_footprint` parses a ``.kicad_mod`` into pads + attributes.
* :func:`repair_tables` restores missing global lib tables from KiCad's
  templates (what KiCad's first-run wizard does).
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .. import sexpr
from ..sexpr import Sym
from ..mcp.registry import ToolError
from . import env

CACHE_VERSION = 3
_LOCK = threading.RLock()


def _cache_dir() -> Path:
    base = Path(os.environ.get("COPPER_HOME", Path.home() / ".copper")) / "cache"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base


# ── symbol index ──────────────────────────────────────────────────────────
_TOP_SYM = re.compile(r'^(?:\t|  )\(symbol "((?:[^"\\]|\\.)*)"', re.M)
_PROP = re.compile(r'\(property "((?:[^"\\]|\\.)*)" "((?:[^"\\]|\\.)*)"')
_EXTENDS = re.compile(r'\(extends "((?:[^"\\]|\\.)*)"\)')
_PIN = re.compile(r'\(pin (?:input|output|bidirectional|tri_state|passive|free|unspecified|power_in|power_out|'
                  r'open_collector|open_emitter|no_connect)\b')
_POWER = re.compile(r'^\s*\(power(?:\s+(?:global|local))?\)', re.M)
_UNIT = re.compile(r'\(symbol "(?:[^"\\]|\\.)*_(\d+)_(\d+)"')

_mem_sym: dict[str, dict] = {}      # path -> {"key": (mtime,size), "entries": [...]}
_mem_fp: dict[str, dict] = {}
_disk_loaded = {"sym": False, "fp": False}


def _unesc(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\") if "\\" in s else s


_dir_keys: dict[str, tuple] = {}     # dir -> (checked_at, key): a 2 s memo so one search does not rescan every directory library
_DIR_KEY_TTL = 2.0


def _is_symdir(p: str) -> bool:
    """An unpacked symbol library: a directory of one-symbol ``.kicad_sym`` files
    (KiCad names it ``<Lib>.kicad_symdir``; any directory in a sym-lib-table counts)."""
    return os.path.isdir(p)


def _symdir_files(p: str) -> list[str]:
    try:
        return sorted(e.path for e in os.scandir(p) if e.name.endswith(".kicad_sym") and e.is_file())
    except OSError:
        return []


def _file_key(p: str):
    try:
        st = os.stat(p)
    except OSError:
        return None
    import stat as _stat
    if not _stat.S_ISDIR(st.st_mode):
        return [int(st.st_mtime), st.st_size]
    now = time.monotonic()
    c = _dir_keys.get(p)
    if c and now - c[0] < _DIR_KEY_TTL:
        return c[1]
    n = mt = size = 0
    try:
        for e in os.scandir(p):
            if e.name.endswith(".kicad_sym"):
                s = e.stat()
                n += 1
                mt = max(mt, s.st_mtime_ns)
                size += s.st_size
    except OSError:
        return None
    key = [n, mt, size]
    _dir_keys[p] = (now, key)
    return key


def _scan_sym_file(path: str) -> list[dict]:
    """Regex-scan a .kicad_sym (or an unpacked library directory) into lightweight index entries."""
    if _is_symdir(path):
        out = []
        for f in _symdir_files(path):
            out += _scan_sym_file(f)
        return _resolve_derived(out)      # a derived symbol's parent is a sibling file
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    starts = [(m.start(), _unesc(m.group(1))) for m in _TOP_SYM.finditer(text)]
    if not starts and "(symbol" in text:
        # unusual formatting → full parse
        try:
            tree = sexpr.loads(text)
        except ValueError:
            return []
        out = []
        for s in sexpr.find_all(tree, "symbol"):
            chunk = sexpr.dumps(s)
            out.append(_entry_from_chunk(str(s[1]), chunk))
        return _resolve_derived(out)
    out = []
    for i, (pos, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        out.append(_entry_from_chunk(name, text[pos:end]))
    return _resolve_derived(out)


def _entry_from_chunk(name: str, chunk: str) -> dict:
    props = {}
    for m in _PROP.finditer(chunk):
        k = _unesc(m.group(1))
        if k not in props:
            props[k] = _unesc(m.group(2))
    ext = _EXTENDS.search(chunk[:400])
    units = {int(m.group(1)) for m in _UNIT.finditer(chunk) if int(m.group(1)) > 0}
    return {
        "n": name,
        "x": _unesc(ext.group(1)) if ext else "",
        "d": props.get("Description", props.get("ki_description", "")),
        "k": props.get("ki_keywords", ""),
        "f": props.get("Footprint", ""),
        "r": props.get("Reference", ""),
        "v": props.get("Value", name),
        "ff": props.get("ki_fp_filters", ""),
        "p": len(_PIN.findall(chunk)),
        "u": max(units) if units else 1,
        "pw": bool(_POWER.search(chunk[:600])),
    }


def _resolve_derived(entries: list[dict]) -> list[dict]:
    by = {e["n"]: e for e in entries}
    for e in entries:
        seen = 0
        cur = e
        while cur.get("x") and seen < 8:
            par = by.get(cur["x"])
            if par is None:
                break
            if not e["p"]:
                e["p"] = par["p"]
            if e["u"] <= 1 < par["u"]:
                e["u"] = par["u"]
            e["pw"] = e["pw"] or par["pw"]
            for k in ("d", "k", "f", "r", "ff"):
                if not e[k]:
                    e[k] = par[k]
            cur = par
            seen += 1
    return entries


def _load_disk(kind: str):
    if _disk_loaded[kind]:
        return
    _disk_loaded[kind] = True
    f = _cache_dir() / f"{kind}index-v{CACHE_VERSION}.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        target = _mem_sym if kind == "sym" else _mem_fp
        for p, v in data.items():
            target.setdefault(p, v)
    except (OSError, ValueError):
        pass


def _save_disk(kind: str):
    target = _mem_sym if kind == "sym" else _mem_fp
    f = _cache_dir() / f"{kind}index-v{CACHE_VERSION}.json"
    try:
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(target, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, f)
    except OSError:
        pass


def symbol_index(project_dir: Path | None = None) -> list[tuple]:
    """[(lib_name, entry_dict, lib_scope)] for every symbol in the active tables."""
    with _LOCK:
        _load_disk("sym")
        dirty = False
        out = []
        for lib in env.lib_tables("sym", project_dir):
            if not lib.exists or str(lib.type).lower() not in ("kicad", "legacy") or not (
                    lib.path.endswith(".kicad_sym") or _is_symdir(lib.path)):
                continue
            key = _file_key(lib.path)
            c = _mem_sym.get(lib.path)
            if c is None or c.get("key") != key:
                c = {"key": key, "entries": _scan_sym_file(lib.path)}
                _mem_sym[lib.path] = c
                dirty = True
            for e in c["entries"]:
                out.append((lib.name, e, lib.scope))
        if dirty:
            _save_disk("sym")
        return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+.]", "", s.lower())


def _tokens(q: str) -> list[str]:
    return [t for t in re.split(r"[\s,;]+", q.lower()) if t]


def search_symbols(query: str, project_dir: Path | None = None, limit: int = 20, lib: str | None = None,
                   include_power: bool | None = None) -> list[dict]:
    """Rank symbols: exact name > name prefix > name contains > keyword/description match.

    ``query`` may be "Lib:Name", a part number ("AMS1117-3.3"), or keywords ("ldo 3.3v")."""
    q = (query or "").strip()
    if not q:
        raise ToolError("empty query", code="BAD_ARGUMENT", fix="library(action='search_symbols', query='AMS1117')")
    qlib = None
    if ":" in q and " " not in q:
        qlib, q = q.split(":", 1)
    ql = q.lower()
    qn = _norm(q)
    toks = _tokens(q)
    ntoks = [_norm(t) for t in toks if _norm(t)]
    want_power = include_power if include_power is not None else any(
        t in ("power", "gnd", "vcc", "pwr") for t in toks)
    res = []
    for lname, e, scope in symbol_index(project_dir):
        if lib and lname != lib:
            continue
        if qlib and lname.lower() != qlib.lower():
            continue
        name = e["n"]
        nl = name.lower()
        nn = _norm(name)
        score = 0.0
        if nl == ql:
            score = 1000
        elif nn == qn and qn:
            score = 900
        elif nl.startswith(ql) or (qn and nn.startswith(qn)):
            score = 600 - min(len(nn) - len(qn), 100)
        elif qn and qn in nn:
            score = 400 - min(len(nn) - len(qn), 100)
        else:
            hay = f"{nl} {e['d'].lower()} {e['k'].lower()} {lname.lower()} {e['v'].lower()}"
            hayn = _norm(hay)
            hits = 0
            for t, tn in zip(toks, [_norm(t) for t in toks]):
                if t in hay or (tn and tn in hayn):
                    hits += 1
            if hits == len(toks) and toks:
                score = 150 + 10 * sum(1 for t in ntoks if t in nn)
            elif len(toks) > 1 and hits >= len(toks) - 1 and hits >= 2:
                score = 60 + hits
        if score <= 0:
            continue
        if e["pw"] and not want_power and score < 900:
            score -= 300
        if scope == "project":
            score += 5
        if e["x"]:
            score -= 0.5  # tiny preference for the base part on ties
        res.append((score, lname, e))
    res.sort(key=lambda r: (-r[0], r[1], r[2]["n"]))
    out = []
    for score, lname, e in res[:limit]:
        out.append({"lib_id": f"{lname}:{e['n']}", "description": e["d"], "keywords": e["k"],
                    "footprint": e["f"], "reference": e["r"], "pins": e["p"], "units": e["u"],
                    "power": e["pw"], "fp_filters": e["ff"], "score": round(score, 1)})
    return out


# ── footprint index ──────────────────────────────────────────────────────
_FP_DESCR = re.compile(r'\(descr "((?:[^"\\]|\\.)*)"')
_FP_TAGS = re.compile(r'\(tags "((?:[^"\\]|\\.)*)"')


def _scan_fp_dir(path: str) -> list[list]:
    out = []
    try:
        names = sorted(f for f in os.listdir(path) if f.endswith(".kicad_mod"))
    except OSError:
        return out
    for f in names:
        d = t = ""
        try:
            with open(os.path.join(path, f), "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(2500)
            m = _FP_DESCR.search(head)
            d = _unesc(m.group(1)) if m else ""
            m = _FP_TAGS.search(head)
            t = _unesc(m.group(1)) if m else ""
        except OSError:
            pass
        out.append([f[:-10], d, t])
    return out


def footprint_index(project_dir: Path | None = None) -> list[tuple]:
    with _LOCK:
        _load_disk("fp")
        dirty = False
        out = []
        for lib in env.lib_tables("fp", project_dir):
            if not lib.exists or not os.path.isdir(lib.path):
                continue
            try:
                key = [int(os.stat(lib.path).st_mtime), len(os.listdir(lib.path))]
            except OSError:
                continue
            c = _mem_fp.get(lib.path)
            if c is None or c.get("key") != key:
                c = {"key": key, "entries": _scan_fp_dir(lib.path)}
                _mem_fp[lib.path] = c
                dirty = True
            for e in c["entries"]:
                out.append((lib.name, e, lib.scope))
        if dirty:
            _save_disk("fp")
        return out


def _fp_glob_to_re(pat: str):
    return re.compile("^" + re.escape(pat).replace(r"\*", ".*").replace(r"\?", ".") + "$", re.I)


def search_footprints(query: str, project_dir: Path | None = None, limit: int = 20, lib: str | None = None,
                      filters: str | None = None) -> list[dict]:
    """Search footprints by name/description/tags. ``filters`` are ki_fp_filters globs
    (space separated, e.g. "R_* SOT?23*") which restrict candidates."""
    q = (query or "").strip()
    qlib = None
    if ":" in q and " " not in q:
        qlib, q = q.split(":", 1)
    ql, qn, toks = q.lower(), _norm(q), _tokens(q)
    fres = [_fp_glob_to_re(f) for f in (filters or "").split() if f]
    res = []
    for lname, (name, descr, tags), scope in footprint_index(project_dir):
        if (lib and lname != lib) or (qlib and lname.lower() != qlib.lower()):
            continue
        if fres and not any(r.match(name) for r in fres):
            continue
        nl, nn = name.lower(), _norm(name)
        if not q:
            score = 1
        elif nl == ql:
            score = 1000
        elif nl.startswith(ql):
            score = 600 - min(len(nl) - len(ql), 200)
        elif qn and qn in nn:
            score = 400 - min(len(nn) - len(qn), 200)
        else:
            hay = f"{nl} {descr.lower()} {tags.lower()} {lname.lower()}"
            hits = sum(1 for t in toks if t in hay or (_norm(t) and _norm(t) in _norm(hay)))
            score = (150 + hits) if toks and hits == len(toks) else 0
        if score <= 0:
            continue
        if scope == "project":
            score += 5
        res.append((score, lname, name, descr, tags))
    res.sort(key=lambda r: (-r[0], r[1], r[2]))
    return [{"fp_id": f"{l}:{n}", "description": d, "tags": t, "score": round(s, 1)}
            for s, l, n, d, t in res[:limit]]


# ── symbol reader ────────────────────────────────────────────────────────
_tree_cache: dict[str, tuple] = {}   # path -> (key, {name: chunk_text})


def _sym_chunks(path: str) -> dict[str, str]:
    key = _file_key(path)
    c = _tree_cache.get(path)
    if c and c[0] == key:
        return c[1]
    if _is_symdir(path):
        # unpacked library: one symbol per file. Keyed by the symbol name INSIDE each file, never by the
        # file name (KiCad escapes characters that are illegal in file names).
        chunks = {}
        for f in _symdir_files(path):
            try:
                chunks.update(_sym_chunks(f))
            except (OSError, ValueError):
                continue
        _tree_cache[path] = (key, chunks)
        return chunks
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    starts = [(m.start(), _unesc(m.group(1))) for m in _TOP_SYM.finditer(text)]
    chunks = {}
    if starts:
        for i, (pos, name) in enumerate(starts):
            end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
            ch = text[pos:end].rstrip()
            # trailing text may include the library's closing ')' → trim to balance
            chunks[name] = ch
    else:
        tree = sexpr.loads(text)
        for s in sexpr.find_all(tree, "symbol"):
            chunks[str(s[1])] = sexpr.dumps(s)
    if len(_tree_cache) > 24:
        _tree_cache.pop(next(iter(_tree_cache)))
    _tree_cache[path] = (key, chunks)
    return chunks


def _parse_chunk(ch: str) -> list:
    # a chunk is "(symbol ... )" possibly followed by the file's final ")" — parse the first form
    depth = 0
    in_str = False
    esc = False
    for i, c in enumerate(ch):
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return sexpr.loads(ch[: i + 1])
    raise ValueError("unbalanced symbol definition")


def split_lib_id(lib_id: str) -> tuple[str, str]:
    if ":" not in lib_id:
        raise ToolError(f"lib_id '{lib_id}' must look like 'Library:Symbol' (e.g. 'Device:R')", code="BAD_LIB_ID",
                        fix=f"library(action='search_symbols', query='{lib_id}')")
    lib, name = lib_id.split(":", 1)
    return lib, name


def _raw_symbol(lib_path: str, name: str) -> list | None:
    ch = None
    if _is_symdir(lib_path):
        direct = os.path.join(lib_path, name + ".kicad_sym")     # the usual case: no need to read the whole directory
        if os.path.isfile(direct):
            try:
                ch = _sym_chunks(direct).get(name)
            except (OSError, ValueError):
                ch = None
    if ch is None:
        ch = _sym_chunks(lib_path).get(name)
    return _parse_chunk(ch) if ch else None


def flatten_symbol(lib_path: str, name: str, depth: int = 0) -> list:
    """Return the symbol S-expr with any ``extends`` chain merged (KiCad's LIB_SYMBOL::Flatten)."""
    raw = _raw_symbol(lib_path, name)
    if raw is None:
        raise KeyError(name)
    ext = sexpr.value(raw, "extends")
    if not ext or depth > 8:
        return copy.deepcopy(raw)
    parent = flatten_symbol(lib_path, str(ext), depth + 1)
    out = [Sym("symbol"), name]
    child_heads = {e[0] for e in raw[2:] if isinstance(e, list) and e}
    child_props = {str(p[1]): p for p in sexpr.find_all(raw, "property")}
    used = set()
    for e in parent[2:]:
        if not isinstance(e, list) or not e:
            out.append(e)
            continue
        h = e[0]
        if h == "property":
            k = str(e[1])
            if k in child_props:
                out.append(copy.deepcopy(child_props[k]))
                used.add(k)
            else:
                out.append(e)
        elif h == "symbol":
            sub = copy.deepcopy(e)
            m = re.match(r".*_(\d+)_(\d+)$", str(sub[1]))
            if m:
                sub[1] = f"{name}_{m.group(1)}_{m.group(2)}"
            out.append(sub)
        elif h in child_heads and h not in ("extends",):
            out.append(copy.deepcopy(sexpr.find(raw, h)))
        else:
            out.append(e)
    # child-only properties (e.g. LCSC fields on a derived part) go after the parent's
    insert_at = max([i for i, e in enumerate(out) if isinstance(e, list) and e and e[0] == "property"] or [1]) + 1
    extra = [copy.deepcopy(p) for k, p in child_props.items() if k not in used]
    out[insert_at:insert_at] = extra
    for h in child_heads:
        if h in ("property", "extends", "symbol") or sexpr.find(out, h) is not None:
            continue
        out.insert(2, copy.deepcopy(sexpr.find(raw, h)))
    return out


def _f(x, d=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _yes(node, name) -> bool:
    """`(hide yes)` / bare `hide` flag inside node."""
    for e in node[1:]:
        if isinstance(e, list) and e and e[0] == name:
            return len(e) == 1 or str(e[1]) == "yes"
        if isinstance(e, Sym) and e == name:
            return True
    return False


def _effects_hidden(node) -> bool:
    if _yes(node, "hide"):
        return True
    eff = sexpr.find(node, "effects")
    return bool(eff is not None and _yes(eff, "hide"))


def parse_pins(sym: list) -> list[dict]:
    pins = []
    for sub in sexpr.find_all(sym, "symbol"):
        m = re.match(r".*_(\d+)_(\d+)$", str(sub[1]))
        unit, style = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        for p in sexpr.find_all(sub, "pin"):
            at = sexpr.find(p, "at") or [Sym("at"), 0, 0, 0]
            name_n = sexpr.find(p, "name")
            num_n = sexpr.find(p, "number")
            alts = [str(a[1]) for a in sexpr.find_all(p, "alternate") if len(a) > 1]
            pins.append({
                "number": str(num_n[1]) if num_n is not None and len(num_n) > 1 else "",
                "name": str(name_n[1]) if name_n is not None and len(name_n) > 1 else "",
                "type": str(p[1]) if len(p) > 1 else "unspecified",
                "shape": str(p[2]) if len(p) > 2 and not isinstance(p[2], list) else "line",
                "at": [_f(at[1]), _f(at[2])],
                "angle": int(round(_f(at[3]) if len(at) > 3 else 0)) % 360,
                "length": _f(sexpr.value(p, "length", 2.54)),
                "unit": unit,
                "body_style": style,
                "hidden": _yes(p, "hide"),
                "alternates": alts,
            })
    return pins


def _bbox_add(bb, x, y):
    if bb[0] is None:
        bb[:] = [x, y, x, y]
    else:
        bb[0] = min(bb[0], x); bb[1] = min(bb[1], y); bb[2] = max(bb[2], x); bb[3] = max(bb[3], y)


_DIRS = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}


def unit_bbox(sym: list, unit: int = 1, body_style: int = 1, include_pins=True) -> list[float]:
    """Bounding box (lib coords, Y up) of a unit's graphics (+pins). [xmin, ymin, xmax, ymax]."""
    bb = [None, None, None, None]
    for sub in sexpr.find_all(sym, "symbol"):
        m = re.match(r".*_(\d+)_(\d+)$", str(sub[1]))
        u, s = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        if u not in (0, unit) or s not in (0, body_style):
            continue
        for g in sub[2:]:
            if not isinstance(g, list) or not g:
                continue
            h = g[0]
            if h == "rectangle":
                for k in ("start", "end"):
                    n = sexpr.find(g, k)
                    if n is not None:
                        _bbox_add(bb, _f(n[1]), _f(n[2]))
            elif h in ("polyline", "bezier"):
                pts = sexpr.find(g, "pts")
                for xy in sexpr.find_all(pts or [], "xy"):
                    _bbox_add(bb, _f(xy[1]), _f(xy[2]))
            elif h == "circle":
                c = sexpr.find(g, "center")
                r = _f(sexpr.value(g, "radius", 0))
                if c is not None:
                    _bbox_add(bb, _f(c[1]) - r, _f(c[2]) - r)
                    _bbox_add(bb, _f(c[1]) + r, _f(c[2]) + r)
            elif h == "arc":
                pts = [sexpr.find(g, k) for k in ("start", "mid", "end")]
                pts = [p for p in pts if p is not None]
                for p in pts:
                    _bbox_add(bb, _f(p[1]), _f(p[2]))
                if len(pts) == 3:
                    c = _circle_center(*[(_f(p[1]), _f(p[2])) for p in pts])
                    if c:
                        # conservative: include the full circle if the arc is large
                        r = math.dist(c, (_f(pts[0][1]), _f(pts[0][2])))
                        if r < 20:
                            _bbox_add(bb, c[0] - r, c[1] - r)
                            _bbox_add(bb, c[0] + r, c[1] + r)
            elif h == "text":
                at = sexpr.find(g, "at")
                if at is not None:
                    _bbox_add(bb, _f(at[1]), _f(at[2]))
            elif h == "pin" and include_pins:
                at = sexpr.find(g, "at")
                if at is None:
                    continue
                x, y = _f(at[1]), _f(at[2])
                ang = int(round(_f(at[3]) if len(at) > 3 else 0)) % 360
                ln = _f(sexpr.value(g, "length", 2.54))
                dx, dy = _DIRS.get(ang, (1, 0))
                _bbox_add(bb, x, y)
                _bbox_add(bb, x + dx * ln, y + dy * ln)
    if bb[0] is None:
        return [-1.27, -1.27, 1.27, 1.27]
    return [float(v) for v in bb]


def _circle_center(a, b, c):
    ax, ay = a; bx, by = b; cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return (ux, uy)


@dataclass
class LibSymbol:
    lib_id: str
    lib: str
    name: str
    tree: list            # flattened S-expr, head name = item name (rename via embedded())
    path: str

    @property
    def properties(self) -> dict:
        return {str(p[1]): str(p[2]) for p in sexpr.find_all(self.tree, "property") if len(p) > 2}

    @property
    def is_power(self) -> bool:
        return sexpr.find(self.tree, "power") is not None

    @property
    def pins(self) -> list[dict]:
        if not hasattr(self, "_pins"):
            self._pins = parse_pins(self.tree)
        return self._pins

    @property
    def unit_count(self) -> int:
        us = [p["unit"] for p in self.pins] + [
            int(m.group(1)) for s in sexpr.find_all(self.tree, "symbol")
            if (m := re.match(r".*_(\d+)_(\d+)$", str(s[1])))]
        return max([u for u in us if u > 0] or [1])

    @property
    def has_body_styles(self) -> bool:
        return any(p["body_style"] == 2 for p in self.pins)

    def unit_pins(self, unit: int, body_style: int = 1) -> list[dict]:
        return [p for p in self.pins if p["unit"] in (0, unit) and p["body_style"] in (0, body_style)]

    def bbox(self, unit: int = 1, body_style: int = 1) -> list[float]:
        return unit_bbox(self.tree, unit, body_style)

    def embedded(self) -> list:
        """S-expr ready for a schematic's lib_symbols: named 'Lib:Name'."""
        t = copy.deepcopy(self.tree)
        t[1] = self.lib_id
        return t

    def to_dict(self, pins=True, max_pins: int = 400) -> dict:
        props = self.properties
        d = {"lib_id": self.lib_id, "description": props.get("Description", ""),
             "keywords": props.get("ki_keywords", ""), "reference": props.get("Reference", ""),
             "value": props.get("Value", ""), "footprint": props.get("Footprint", ""),
             "datasheet": props.get("Datasheet", ""), "fp_filters": props.get("ki_fp_filters", ""),
             "power": self.is_power, "units": self.unit_count,
             "properties": {k: v for k, v in props.items() if not k.startswith("ki_")}}
        raw_ext = None
        try:
            raw = _raw_symbol(self.path, self.name)
            raw_ext = sexpr.value(raw, "extends") if raw else None
        except (OSError, ValueError):
            pass
        if raw_ext:
            d["extends"] = f"{self.lib}:{raw_ext}"
        if pins:
            ps = self.pins
            d["pin_count"] = len({(p["number"]) for p in ps})
            d["pins"] = [{"number": p["number"], "name": p["name"], "type": p["type"],
                          **({"unit": p["unit"]} if self.unit_count > 1 else {}),
                          **({"body_style": p["body_style"]} if p["body_style"] > 1 else {}),
                          **({"hidden": True} if p["hidden"] else {}),
                          **({"alternates": p["alternates"]} if p["alternates"] else {}),
                          "at": p["at"], "angle": p["angle"], "length": p["length"]}
                         for p in ps[:max_pins]]
            if len(ps) > max_pins:
                d["pins_truncated"] = len(ps) - max_pins
        return d


_lsym_cache: dict[tuple, tuple] = {}


def read_symbol(lib_id: str, project_dir: Path | None = None) -> LibSymbol:
    """Load a symbol by 'Lib:Name' with `extends` flattened. Raises ToolError with a fix hint."""
    lib, name = split_lib_id(lib_id)
    L = env.find_lib("sym", lib, project_dir)
    key = (L.path, name)
    fk = _file_key(L.path)
    c = _lsym_cache.get(key)
    if c and c[0] == fk:
        return LibSymbol(lib_id, lib, name, copy.deepcopy(c[1]), L.path)
    try:
        tree = flatten_symbol(L.path, name)
    except KeyError:
        near = search_symbols(name, project_dir, limit=5)
        raise ToolError(f"symbol '{name}' not found in library '{lib}'", code="SYMBOL_NOT_FOUND",
                        fix="use one of: " + ", ".join(r["lib_id"] for r in near) if near else
                        f"library(action='search_symbols', query='{name}')",
                        data={"suggestions": [r["lib_id"] for r in near]})
    if len(_lsym_cache) > 200:
        _lsym_cache.pop(next(iter(_lsym_cache)))
    _lsym_cache[key] = (fk, tree)
    return LibSymbol(lib_id, lib, name, copy.deepcopy(tree), L.path)


# ── footprint reader ─────────────────────────────────────────────────────
def read_footprint(fp_id: str, project_dir: Path | None = None) -> dict:
    if ":" not in fp_id:
        raise ToolError(f"footprint id '{fp_id}' must look like 'Library:Footprint'", code="BAD_FP_ID",
                        fix=f"library(action='search_footprints', query='{fp_id}')")
    lib, name = fp_id.split(":", 1)
    L = env.find_lib("fp", lib, project_dir)
    f = Path(L.path) / f"{name}.kicad_mod"
    if not f.exists():
        near = search_footprints(name, project_dir, limit=5)
        raise ToolError(f"footprint '{name}' not found in '{lib}'", code="FOOTPRINT_NOT_FOUND",
                        fix="use one of: " + ", ".join(r["fp_id"] for r in near) if near else
                        f"library(action='search_footprints', query='{name}')",
                        data={"suggestions": [r["fp_id"] for r in near]})
    tree = sexpr.loads(f.read_text(encoding="utf-8", errors="replace"))
    pads = []
    bb = [None] * 4
    for p in sexpr.find_all(tree, "pad"):
        at = sexpr.find(p, "at") or [0, 0, 0]
        size = sexpr.find(p, "size") or [0, 0, 0]
        layers = sexpr.find(p, "layers") or ["layers"]
        drill = sexpr.find(p, "drill")
        pd = {"number": str(p[1]), "type": str(p[2]), "shape": str(p[3]),
              "at": [_f(at[1]), _f(at[2])] + ([_f(at[3])] if len(at) > 3 else []),
              "size": [_f(size[1]), _f(size[2])], "layers": [str(x) for x in layers[1:]]}
        if drill is not None:
            pd["drill"] = [_f(x) for x in drill[1:] if not isinstance(x, list) and str(x) != "oval"]
        pads.append(pd)
        _bbox_add(bb, pd["at"][0] - pd["size"][0] / 2, pd["at"][1] - pd["size"][1] / 2)
        _bbox_add(bb, pd["at"][0] + pd["size"][0] / 2, pd["at"][1] + pd["size"][1] / 2)
    crt = [None] * 4
    for g in tree[1:]:
        if isinstance(g, list) and g and str(g[0]).startswith("fp_") and sexpr.value(g, "layer") in ("F.CrtYd", "B.CrtYd"):
            for k in ("start", "end", "center", "mid"):
                n = sexpr.find(g, k)
                if n is not None:
                    _bbox_add(crt, _f(n[1]), _f(n[2]))
            pts = sexpr.find(g, "pts")
            for xy in sexpr.find_all(pts or [], "xy"):
                _bbox_add(crt, _f(xy[1]), _f(xy[2]))
    attrs = sexpr.find(tree, "attr")
    props = {str(p[1]): str(p[2]) for p in sexpr.find_all(tree, "property") if len(p) > 2}
    model = sexpr.find(tree, "model")
    numbers = [p["number"] for p in pads if p["number"]]
    return {"fp_id": fp_id, "description": str(sexpr.value(tree, "descr", "") or ""),
            "tags": str(sexpr.value(tree, "tags", "") or ""),
            "attributes": [str(a) for a in attrs[1:]] if attrs is not None else [],
            "pad_count": len(set(numbers)), "pads": pads,
            "pad_bbox": [round(v, 3) for v in bb] if bb[0] is not None else None,
            "courtyard": [round(v, 3) for v in crt] if crt[0] is not None else None,
            "model": str(model[1]) if model is not None else None,
            "properties": {k: v for k, v in props.items() if k not in ("Reference", "Value")},
            "path": str(f)}


# ── listing + repair ─────────────────────────────────────────────────────
def list_libraries(kind: str = "sym", project_dir: Path | None = None) -> list[dict]:
    return [{"name": l.name, "scope": l.scope, "type": l.type, "uri": l.uri, "path": l.path,
             "exists": l.exists, **({"descr": l.descr} if l.descr else {})}
            for l in env.lib_tables(kind, project_dir)]


def repair_tables(dry_run: bool = True, project_dir: Path | None = None) -> dict:
    """Make the global sym/fp lib tables usable.

    * missing global table → copy KiCad's template table into the config dir (KiCad first-run behaviour);
    * table present but most entries point at missing files (e.g. an older/uninstalled KiCad)
      → back it up and replace it with the template;
    * project tables are only reported (never rewritten): broken rows are listed with their paths.
    """
    inst = env.kicad()
    cfg = env.config_dir()
    report = {"config_dir": str(cfg), "kicad": inst.full_version or inst.version, "dry_run": dry_run,
              "actions": [], "problems": []}
    tmpl_dir = inst.share / "template" if inst.share else None
    variables = env.path_vars(project_dir)
    for kind in ("sym", "fp"):
        fname = f"{kind}-lib-table"
        g = cfg / fname
        tmpl = tmpl_dir / fname if tmpl_dir else None
        if not g.exists():
            if tmpl and tmpl.exists():
                report["actions"].append({"table": fname, "action": "create-from-template",
                                          "from": str(tmpl), "to": str(g)})
                if not dry_run:
                    cfg.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(tmpl, g)
            else:
                report["problems"].append(f"global {fname} missing and no KiCad template found")
            continue
        libs = env._parse_table(g, "global", variables)
        missing = [l for l in libs if not l.exists]
        if libs and len(missing) > max(5, 0.5 * len(libs)) and tmpl and tmpl.exists():
            bak = g.with_name(fname + ".copper-backup")
            report["actions"].append({"table": fname, "action": "replace-with-template",
                                      "reason": f"{len(missing)}/{len(libs)} libraries point at missing files",
                                      "backup": str(bak), "from": str(tmpl)})
            if not dry_run:
                shutil.copy2(g, bak)
                # keep user-added rows whose files exist and that aren't in the template
                tlibs = {l.name for l in env._parse_table(tmpl, "template", variables)}
                keep = [l for l in libs if l.exists and l.name not in tlibs]
                text = tmpl.read_text(encoding="utf-8").rstrip()
                if keep and text.endswith(")"):
                    rows = "".join(f'\n\t(lib (name "{l.name}") (type "{l.type}") (uri "{l.uri}") (options "") (descr "{l.descr}"))'
                                   for l in keep)
                    text = text[:-1].rstrip() + rows + "\n)"
                g.write_text(text + "\n", encoding="utf-8")
        elif missing:
            report["problems"].append({"table": fname, "scope": "global",
                                       "missing": [{"name": l.name, "path": l.path} for l in missing[:30]]})
        if project_dir and (Path(project_dir) / fname).exists():
            pl = env._parse_table(Path(project_dir) / fname, "project", variables)
            pm = [l for l in pl if not l.exists]
            if pm:
                report["problems"].append({"table": fname, "scope": "project",
                                           "missing": [{"name": l.name, "uri": l.uri, "path": l.path} for l in pm]})
    if not dry_run and report["actions"]:
        _mem_sym.clear(); _mem_fp.clear()
        _disk_loaded["sym"] = _disk_loaded["fp"] = False
        report["note"] = "Restart KiCad (or re-open the Symbol/Footprint chooser) to pick up the new tables."
    if not report["actions"] and not report["problems"]:
        report["ok"] = True
    return report


def warm(project_dir: Path | None = None) -> dict:
    t = time.time()
    n = len(symbol_index(project_dir))
    t1 = time.time()
    m = len(footprint_index(project_dir))
    return {"symbols": n, "footprints": m, "sym_s": round(t1 - t, 2), "fp_s": round(time.time() - t1, 2)}
