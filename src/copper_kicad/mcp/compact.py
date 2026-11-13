"""Wire shaping: what a tool result looks like when it leaves the server.

Tools keep building full payloads and ``registry.call_tool`` keeps returning them unchanged (in-process
callers and tests read ``structuredContent``). ``Server._call`` passes that dict through
:func:`to_wire` just before it replies, and through :func:`prepare_args` just before the call:

``prepare_args``  the image policy and the digest-by-default arguments for calls that did not say
                  otherwise (``COPPER_IMAGES`` = never | final | auto; default ``final``: single-step
                  tools attach no picture, a ``render`` call gets the smallest size that is still legible).
``to_wire``       the compactor. Rules (R-numbers as in docs/ARCHITECTURE "wire envelope"):

  R1  a string VALUE that is a path inside the project becomes project-relative with forward slashes
      (never under the keys dir / project / fix / next; paths outside the project stay absolute)
  R2  derivable keys are dropped: revision, canonicalized, checks_run, section, a top-level `file` inside the
      project, `report` unless the same object has `more`
  R3  empty values are dropped: None, [], {}, "" ONLY. Zeros and booleans are kept; a count that is
      absent is not a count of zero. `false` is dropped only for the few keys in NOISE_FALSE.
  R4  `changed` longer than 8 lines becomes counts by verb, plus a ref/value/part/class table of what
      each schematic placement resolved to, plus the lines that fit no verb
  R5  identical findings (same type|code, message, fix) merge into one entry with `n`, merged refs and
      the first positions; `fixes` arrays go; `fixed_findings` becomes a count
  R6  the standing UNROUTED / DRC_SUMMARY findings go when the payload already carries the DRC counts
  R7  floats are rounded to 4 decimals (0.1 um; positions come back in later calls); lengths, scores,
      seconds and percentages to 1. Widths and clearances keep 4: 0.127 mm must not become 0.1
  R8  one channel: the text block is `summary + "\\n" + compact JSON`; `structuredContent` is sent only
      with COPPER_STRUCTURED=1 (and then carries the same compacted payload)
  R9  image captions are `[image] <what>`; the saved path appears once, in the JSON, relative
  cap the text block is held under COPPER_MAX_RESULT_BYTES (default 6000) by cutting the longest lists
      from the end; the full payload is spilled to <project>/.copper/results/ and `cut` says where

Never dropped: fix, next, refs, at, error, message, ok, fab_ready, and any key whose value is a number.
``COPPER_COMPACT=0`` switches all of this off (the raw registry result goes out) for debugging and for
before/after measurements. Pure functions of (payload, project dir); no imports from ``tools/``.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# ── knobs ────────────────────────────────────────────────────────────────
DEFAULT_MAX_BYTES = 6000
DETAIL_FACTOR = 4                      # a call that asks for detail (limit / what / refs / query …) may be this much larger
DETAIL_ARGS = ("limit", "what", "refs", "query", "detail", "net", "ref", "nets")
MAX_SPILL_FILES = 20
MIN_LIST = 3
IMAGE_MAX_EDGE = 1400                  # no image leaves above this unless the caller passed `width`
RENDER_WIDTH = {"schematic": 1000, "pcb": 800, "pcb3d": 800}
IMAGE_PALETTE_OVER = 300_000           # base64 characters: a larger flat-colour drawing is re-encoded with a 256-colour palette
IMAGE_MODES = ("never", "final", "auto")
# tools whose picture IS the answer: never switched off by the policy
IMAGE_EXEMPT = {"render", "focus", "export"}
# digest-by-default: arguments added when the call did not pass them
WIRE_DEFAULTS = {"pcb_read": {"what": ["summary"]}, "verify": {"limit": 8}}
WIRE_HINTS = {"pcb_read": "pcb_read(what=['footprints'|'nets'|'pads'|'tracks'|'zones'], refs=[...]) for detail"}

PROTECTED_PATH_KEYS = {"dir", "project", "fix", "next", "fixes", "hint", "help"}
DROP_KEYS = {"revision", "canonicalized", "checks_run", "section"}
NOISE_FALSE = {"dry_run", "created", "downloaded", "cached", "truncated"}
NEVER_DROP = {"fix", "next", "refs", "at", "error", "message", "ok", "fab_ready"}
FINDING_LISTS = {"findings", "violations", "new_issues", "top", "top_errors", "new_top", "blockers"}
STANDING = {"DRC_SUMMARY"}
ABS_DIR_TOOLS = {"project_create", "project_open", "project_summary", "copper_status", "fab_export"}
ONE_DECIMAL = re.compile(r"(length_mm$|^ratsnest|^score|^seconds$|^percent$|^skew_mm$)")


def enabled() -> bool:
    return os.environ.get("COPPER_COMPACT", "1").strip().lower() not in ("0", "false", "off", "no")


def image_mode() -> str:
    m = os.environ.get("COPPER_IMAGES", "final").strip().lower()
    return m if m in IMAGE_MODES else "final"


# ── before the call ──────────────────────────────────────────────────────
def prepare_args(tool, args: dict) -> tuple[dict, dict]:
    """Wire-level defaults for one call. Returns (arguments to run with, what was defaulted).

    An explicit argument always wins: nothing the caller passed is changed. ``tool`` is the registry
    Tool (or None for an unknown name, which is left for the registry to report)."""
    if tool is None or not isinstance(args, dict) or not enabled():
        return args, {}
    props = tool.schema.get("properties", {})
    out, defaulted = dict(args), {}
    if image_mode() != "auto" and tool.name not in IMAGE_EXEMPT:
        for k in ("preview", "render"):
            if (props.get(k) or {}).get("type") == "boolean" and out.get(k) is None:
                out[k] = False
                defaulted[k] = False
    if tool.name == "render" and "width" in props and out.get("width") is None:
        w = RENDER_WIDTH.get(str(out.get("target")))
        if w:
            out["width"] = w
            defaulted["width"] = w
    wire = dict(WIRE_DEFAULTS.get(tool.name) or {})
    if tool.name == "pcb_read" and (out.get("refs") or out.get("net")):
        # a question about some parts or one net: answer that, not the whole board's footprint and net lists
        # (the tool's own default adds both, which made pcb_read(refs=['U11']) a 24 kB result on a 589-net board)
        wire["what"] = ["footprints"] if out.get("refs") else ["nets"]
    for k, v in wire.items():
        if k in props and out.get(k) is None:
            out[k] = list(v) if isinstance(v, list) else v
            defaulted[k] = v
    return out, defaulted


# ── paths (R1) ───────────────────────────────────────────────────────────
def _variants(project_dir) -> list[str]:
    """Every spelling of the project folder a payload may contain: as given, resolved (long), and on
    Windows the 8.3 short form."""
    if not project_dir:
        return []
    seen, out = set(), []

    def add(p):
        s = str(p).rstrip("\\/")
        if s and s.lower() not in seen and len(s) > 3:
            seen.add(s.lower())
            out.append(s)
    add(project_dir)
    try:
        add(Path(project_dir).resolve())
    except OSError:
        pass
    if os.name == "nt":
        try:
            import ctypes
            for fn in (ctypes.windll.kernel32.GetShortPathNameW, ctypes.windll.kernel32.GetLongPathNameW):
                for base in list(out):
                    buf = ctypes.create_unicode_buffer(1024)
                    if fn(base, buf, 1024):
                        add(buf.value)
        except Exception:  # noqa: BLE001 — best effort
            pass
    return out


def _long_name(s: str) -> str:
    """Windows: expand 8.3 short components (C:\\Users\\USERNA~1\\...) to the long spelling. The path need not
    exist: the longest existing parent is expanded and the rest is appended."""
    if os.name != "nt":
        return s
    try:
        import ctypes
        head, tail = s.replace("/", "\\"), []
        for _ in range(40):
            buf = ctypes.create_unicode_buffer(2048)
            if ctypes.windll.kernel32.GetLongPathNameW(head, buf, 2048):
                return "\\".join([buf.value.rstrip("\\")] + tail[::-1])
            head, _sep, last = head.rpartition("\\")
            if not head or not last:
                break
            tail.append(last)
    except Exception:  # noqa: BLE001
        pass
    return s


class Paths:
    """Rewrites strings that point into one project folder."""

    def __init__(self, project_dir, windows: bool | None = None):
        self.windows = (os.name == "nt") if windows is None else windows
        vs = _variants(project_dir) if project_dir else []
        self.abs = None
        if vs:
            try:
                self.abs = str(Path(project_dir).resolve())
            except OSError:
                self.abs = vs[0]
        alts = []
        for v in sorted(vs, key=len, reverse=True):
            parts = [p for p in re.split(r"[\\/]+", v) if p]
            lead = "/" if v.startswith("/") else ""
            alts.append(re.escape(lead) + r"[\\/]+".join(re.escape(p) for p in parts))
        flags = re.I if self.windows else 0
        self.rx_whole = re.compile(r"^(?:%s)(?:[\\/]+(.*))?$" % "|".join(alts), flags | re.S) if alts else None
        self.rx_inner = re.compile(r"(?<![\w.~-])(?:%s)[\\/]+([^\s\"'<>|*?]+)" % "|".join(alts), flags) if alts else None
        self.rx_bare = re.compile(r"(?<![\w.~-])(?:%s)(?![\w\\/.~-])" % "|".join(alts), flags) if alts else None

    def value(self, s: str) -> str:
        """A whole string value: relative when it is the project dir or lies under it."""
        if not self.rx_whole or len(s) > 600 or "\n" in s:
            return s
        m = self.rx_whole.match(s)
        if not m and self.windows and "~" in s and re.match(r"^[A-Za-z]:[\\/]", s):
            m = self.rx_whole.match(_long_name(s))       # a path with 8.3 components somewhere in the middle
        if m:
            return (m.group(1) or ".").replace("\\", "/")
        return self.text(s)

    def text(self, s: str) -> str:
        """Free text (summary line, messages): paths INSIDE the project lose the prefix; the bare project
        dir itself is left alone (it is the one absolute location a result may state)."""
        if not self.rx_inner:
            return s
        return self.rx_inner.sub(lambda m: m.group(1).replace("\\", "/"), s)


# ── R4: change digest ────────────────────────────────────────────────────
_PLACED = re.compile(r"^placed (\S+)(?: unit \d+)? (\S+)(?: (.*?))? at \([^)]*\)(?: rot -?[\d.]+)?(?: mirror \w+)?(?: [^()]+? (\S+))?(?: \(JLC (\w)\w*\))?(?= \(|$)")
_ADDED = re.compile(r"^added (\S+) \(")
_MOVED = re.compile(r"^\S+ → \(-?[\d.]+, -?[\d.]+\)")


def digest_changed(lines: list) -> list | dict:
    if len(lines) <= 8 or not all(isinstance(x, str) for x in lines):
        return lines
    counts: dict = {}
    parts, notes = [], []

    def bump(k):
        counts[k] = counts.get(k, 0) + 1
    for line in lines:
        m = _PLACED.match(line)
        if m:
            bump("placed")
            value = m.group(3) or m.group(2).split(":")[-1]
            parts.append([m.group(1), value[:28], m.group(4), m.group(5)])
        elif _ADDED.match(line):
            bump("added")
        elif _MOVED.match(line):
            bump("moved")
        elif "no-connect" in line or "no connect" in line:
            bump("no_connect")
        elif line.startswith(("updated ", "replaced ", "removed ")):
            bump(line.split(" ", 1)[0])
        elif " → " in line or " wired " in line or "already wired" in line:
            bump("connected")
        elif line.startswith("… and "):
            notes.append(line)
        else:
            notes.append(line)
    d: dict = dict(counts)
    if parts:
        d["parts"] = {"cols": ["ref", "value", "part", "class"], "rows": parts}
    if notes:
        d["notes"] = notes[:6] + ([f"… {len(notes) - 6} more"] if len(notes) > 6 else [])
    return d


# ── R5/R6: findings ──────────────────────────────────────────────────────
def group_findings(fs: list, drop_standing: bool) -> list:
    out, seen = [], {}
    for f in fs:
        if not isinstance(f, dict):
            out.append(f)
            continue
        code = f.get("code")
        if drop_standing and (code in STANDING or (code == "UNROUTED" and f.get("severity") == "info")):
            continue
        key = (f.get("type") or code, f.get("message"), f.get("fix"))
        if key[0] is None and key[1] is None:
            out.append(f)
            continue
        first = seen.get(key)
        if first is None:
            f = dict(f)
            f.pop("fixes", None)
            seen[key] = f
            out.append(f)
            continue
        add = f.get("count") if isinstance(f.get("count"), int) else f.get("n") if isinstance(f.get("n"), int) else 1
        if isinstance(first.get("count"), int):
            first["count"] += add
        else:
            first["n"] = first.get("n", 1) + add
        for k in ("refs", "items"):
            if isinstance(f.get(k), list):
                have = first.setdefault(k, [])
                if isinstance(have, list):
                    have.extend(x for x in f[k] if x not in have)
        if f.get("at") is not None and first.get("at") is not None:
            more = first.setdefault("more_at", [])
            if len(more) < 2 and f["at"] != first["at"] and f["at"] not in more:
                more.append(f["at"])
    return out


# ── the walk (R1, R2, R3, R7) ────────────────────────────────────────────
def _empty(k, v) -> bool:
    if v is None or (isinstance(v, (list, dict, str)) and len(v) == 0):
        return k not in ("fix", "next", "message", "error") or v is None      # an empty refs / at list says nothing
    return v is False and k in NOISE_FALSE


def _round(k, v: float):
    return round(v, 1 if (isinstance(k, str) and ONE_DECIMAL.search(k)) else 4)


def _walk(o, paths: Paths, key=None, top=False, has_drc=False):
    if isinstance(o, dict):
        d = {}
        for k, v in o.items():
            if k in DROP_KEYS and (isinstance(v, bool) or not isinstance(v, (int, float))):
                continue
            if k == "report" and "more" not in o and isinstance(v, str):
                continue
            if top and k == "file" and isinstance(v, str) and paths.rx_whole and paths.rx_whole.match(v):
                continue
            if k == "fixes" and isinstance(v, list):
                continue
            if k == "fixed_findings" and isinstance(v, list):
                if v:
                    d[k] = len(v)
                continue
            if k == "changed" and isinstance(v, list):
                v = digest_changed(v)
            if k in FINDING_LISTS and isinstance(v, list):
                v = group_findings(v, has_drc and k == "findings")
            if k in ("top", "examples") and isinstance(v, list) and len(v) > 1 and all(isinstance(x, str) for x in v):
                seen: dict = {}
                for x in v:
                    seen[x] = seen.get(x, 0) + 1
                v = [x if n == 1 else f"{x} (x{n})" for x, n in seen.items()]
            if isinstance(v, str):
                v = v if k in PROTECTED_PATH_KEYS else paths.value(v)
            elif isinstance(v, bool) or isinstance(v, int):
                pass
            elif isinstance(v, float):
                v = _round(k, v)
            else:
                v = _walk(v, paths, k, False, has_drc) if k not in ("fix", "next") else v
            if _empty(k, v):
                continue
            d[k] = v
        return d
    if isinstance(o, list):
        out = []
        for x in o:
            if isinstance(x, str):
                out.append(x if key in PROTECTED_PATH_KEYS else paths.value(x))
            elif isinstance(x, float):
                out.append(_round(key, x))
            elif isinstance(x, (dict, list)):
                out.append(_walk(x, paths, key, False, has_drc))
            else:
                out.append(x)
        return out
    return o


def compact_payload(payload: dict, project_dir=None, paths: Paths | None = None) -> dict:
    """Rules R1 to R7 on one payload dict. Pure: the input is not modified."""
    paths = paths or Paths(project_dir)
    v = payload.get("verify")
    has_drc = (isinstance(v, dict) and isinstance(v.get("drc"), dict)) or \
              (isinstance(payload.get("drc"), dict) and "errors" in payload["drc"])
    return _walk(payload, paths, None, True, has_drc)


# ── the cap ──────────────────────────────────────────────────────────────
def _dumps(o) -> str:
    return json.dumps(o, separators=(",", ":"), default=str, ensure_ascii=False)


def _lists(o, path=()):
    """Every list with more than MIN_LIST entries, as (path, list)."""
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _lists(v, path + (k,))
    elif isinstance(o, list):
        if len(o) > MIN_LIST and path and path[-1] not in ("cols", "at", "refs", "bbox", "more_at"):
            yield path, o
        for i, v in enumerate(o):
            if isinstance(v, (dict, list)) and not (isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v)):
                yield from _lists(v, path + (i,))


def cap_payload(payload: dict, summary: str, max_bytes: int) -> tuple[dict, dict]:
    """Cut the longest lists from the end until `summary + JSON` fits. Returns (payload, {path: [shown, total]}).
    Lists never go below MIN_LIST entries; if the text still does not fit it is sent as it is."""
    def size(p):
        return len((summary + "\n" + _dumps(p)).encode("utf-8")) if summary else len(_dumps(p).encode("utf-8"))
    if size(payload) <= max_bytes:
        return payload, {}
    p = json.loads(_dumps(payload))
    cut: dict = {}
    for _ in range(400):
        over = size(p) - max_bytes
        if over <= 0:
            break
        cands = sorted(_lists(p), key=lambda pl: -len(_dumps(pl[1])))
        if not cands:
            break
        path, lst = cands[0]
        per = max(1, len(_dumps(lst)) // max(1, len(lst)))
        drop = min(len(lst) - MIN_LIST, max(1, -(-(over + 160) // per)))
        name = ".".join(str(x) for x in path)
        total = cut.get(name, [0, len(lst)])[1]
        del lst[len(lst) - drop:]
        cut[name] = [len(lst), total]
    return p, cut


def _spill(tool: str, payload: dict, project_dir, home=None) -> str | None:
    """Write the uncut payload next to erc.json / drc.json and return its (project-relative) path."""
    try:
        if project_dir and Path(project_dir).is_dir():
            d = Path(project_dir) / ".copper" / "results"
            rel = True
        else:
            d = Path(home or os.environ.get("COPPER_HOME") or (Path.home() / ".copper")) / "results"
            rel = False
        d.mkdir(parents=True, exist_ok=True)
        old = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime)
        nums = [int(m.group(1)) for f in old for m in [re.search(r"-(\d+)\.json$", f.name)] if m]
        n = (max(nums) + 1) if nums else 1
        for f in old[:max(0, len(old) - (MAX_SPILL_FILES - 1))]:
            f.unlink()
        f = d / f"{re.sub(r'[^A-Za-z0-9_]+', '_', tool or 'result')}-{n}.json"
        f.write_text(_dumps(payload), encoding="utf-8")
        return f".copper/results/{f.name}" if rel else str(f)
    except OSError:
        return None


# ── images ───────────────────────────────────────────────────────────────
def _shrink(b64: str, max_edge: int) -> str:
    """Downscale a PNG whose long edge exceeds max_edge. Returns the input on any problem."""
    from .ledger import b64_png_size
    w, h = b64_png_size(b64)
    if not w or max(w, h) <= max_edge:
        return b64
    try:
        import base64
        import io
        from PIL import Image as PILImage
        im = PILImage.open(io.BytesIO(base64.b64decode(b64)))
        s = max_edge / max(w, h)
        im = im.resize((max(1, round(w * s)), max(1, round(h * s))), PILImage.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001 — a picture that cannot be resized is still a picture
        return b64


def _fewer_bytes(b64: str) -> str:
    """Re-encode a large PNG with a 256-colour palette (no dithering, same pixels). Board and schematic
    drawings are flat colour plus anti-aliasing, so this cuts the bytes on the wire to about a third and
    reads the same; image TOKENS depend on pixels and do not change. It matters because clients cap the size
    of one tool result (often near 1 MB) and a 1000 px picture of a dense board was 0.5 to 0.7 MB of base64.
    Returns the input unless the result is clearly smaller; never raises."""
    if len(b64) <= IMAGE_PALETTE_OVER:
        return b64
    try:
        import base64
        import io
        from PIL import Image as PILImage
        im = PILImage.open(io.BytesIO(base64.b64decode(b64)))
        if im.mode == "P":
            return b64
        alpha = im.mode in ("RGBA", "LA")
        q = im.convert("RGBA" if alpha else "RGB").quantize(
            colors=256, method=PILImage.Quantize.FASTOCTREE if alpha else PILImage.Quantize.MEDIANCUT,
            dither=PILImage.Dither.NONE)
        buf = io.BytesIO()
        q.save(buf, format="PNG", optimize=True)
        out = base64.b64encode(buf.getvalue()).decode("ascii")
        return out if len(out) < 0.8 * len(b64) else b64
    except Exception:  # noqa: BLE001 — the original picture is always a valid answer
        return b64


# ── digests for a few large results ──────────────────────────────────────
# Wire-only shaping of payloads whose bulk is on disk anyway or repeats the request. Kept here (not in the
# tool modules) so in-process callers still get everything; each is a candidate to move into its tool.
def _shape_status(p: dict, args: dict) -> dict:
    k = p.get("kicad")
    if isinstance(k, dict):
        k = dict(k)
        slim = lambda i: {kk: v for kk, v in i.items() if kk not in ("kicad_cli", "python", "share")}             if isinstance(i, dict) else i                                    # all three live under `root`
        act, ins = k.get("active"), k.get("installs")
        if isinstance(ins, list):
            if len(ins) == 1 and isinstance(act, dict) and isinstance(ins[0], dict) and ins[0].get("root") == act.get("root"):
                k.pop("installs")                                            # the one install IS the active one
            else:
                k["installs"] = [slim(i) for i in ins]
        if isinstance(act, dict):
            k["active"] = slim(act)
        p["kicad"] = k
    c = p.get("capabilities")
    if isinstance(c, dict):
        c = dict(c)
        if isinstance(c.get("pcbnew_worker"), dict):
            c["pcbnew_worker"] = {kk: v for kk, v in c["pcbnew_worker"].items() if kk != "python"}
        if isinstance(c.get("routing"), dict):
            c["routing"] = {kk: v for kk, v in c["routing"].items() if kk not in ("tools_dir", "freerouting", "java")}
        p["capabilities"] = c
    return p


def _shape_fab(p: dict, args: dict) -> dict:
    for k in ("zip_files", "layers", "bom_preview", "cpl_preview", "placement_preview"):          # the files are on disk
        p.pop(k, None)
    return p


def _shape_guided(p: dict, args: dict) -> dict:
    if not args.get("plan"):
        p.pop("plan_used", None)                                             # the built-in defaults, not a choice
    p.pop("passes", None)
    return p


SHAPERS = {"copper_status": _shape_status, "fab_export": _shape_fab,
           "pcb_route_guided": _shape_guided}


# ── the wire ─────────────────────────────────────────────────────────────
def _split(result: dict) -> tuple[str, dict | None, list, list]:
    """(summary, payload or None, image blocks, captions) from a registry result."""
    content = result.get("content") or []
    text0 = content[0].get("text", "") if content and content[0].get("type") == "text" else ""
    payload = result.get("structuredContent")
    summary = ""
    if isinstance(payload, dict):
        body = _dumps(payload)
        if text0.endswith(body):
            summary = text0[:len(text0) - len(body)].rstrip("\n")
        else:
            i = text0.find("\n{")
            summary = text0[:i] if i > 0 else ""
    else:
        payload = None
        i = text0.find("\n{")
        head, body = (text0[:i], text0[i + 1:]) if i >= 0 else ("", text0)
        if body.startswith("{"):
            try:
                payload, summary = json.loads(body), head
            except ValueError:
                payload = None
    images, captions = [], []
    for block in content[1:] if content else []:
        if block.get("type") == "image":
            images.append(block)
            captions.append(None)
        elif block.get("type") == "text" and block.get("text", "").startswith("[image") and captions:
            captions[-1] = block["text"]
        elif block.get("type") == "text":
            images.append(block)             # some other text block: passed through in place
            captions.append(None)
    return summary, payload, images, captions


def _has_abs_dir(o, paths: Paths) -> bool:
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "dir" and isinstance(v, str) and paths.rx_whole and paths.rx_whole.match(v):
                return True
            if isinstance(v, (dict, list)) and _has_abs_dir(v, paths):
                return True
    elif isinstance(o, list):
        return any(_has_abs_dir(x, paths) for x in o)
    return False


def to_wire(result: dict, project_dir=None, *, tool: str | None = None, args: dict | None = None,
            defaulted: dict | None = None, max_bytes: int | None = None) -> dict:
    """Shape one registry result for the client. ``args`` are the caller's own arguments (before
    prepare_args), ``defaulted`` what prepare_args added. Never raises: on any problem the input goes out."""
    if not isinstance(result, dict):
        return result
    if not enabled():
        if isinstance(result.get("structuredContent"), dict):
            result["structuredContent"].pop("_raw", None)
        return result
    try:
        return _to_wire(result, project_dir, tool, args or {}, defaulted or {}, max_bytes)
    except Exception:  # noqa: BLE001 — shaping must never lose a result
        if os.environ.get("COPPER_DEBUG"):
            import sys
            import traceback
            traceback.print_exc(file=sys.stderr)
        return result


def _to_wire(result, project_dir, tool, args, defaulted, max_bytes):
    summary, payload, blocks, captions = _split(result)
    if payload is None:                       # plain text result (copper_help): nothing to compact
        return {k: v for k, v in result.items() if k != "structuredContent"}
    raw = bool(payload.pop("_raw", False)) if isinstance(payload, dict) else False
    is_error = bool(result.get("isError"))
    paths = Paths(project_dir)
    shaper = SHAPERS.get(tool or "")
    if shaper and not raw and not is_error:
        payload = shaper(dict(payload), args)
    out_payload = payload if raw else compact_payload(payload, paths=paths)
    if not raw:
        summary = paths.text(summary)
        if paths.abs and paths.rx_bare and _has_abs_dir(payload, paths):
            # the location is stated once, in the JSON `dir`: the summary line names the folder only
            summary = paths.rx_bare.sub(lambda m: Path(paths.abs).name + "/", summary)
        if is_error and not os.environ.get("COPPER_DEBUG"):
            out_payload.pop("trace", None)
        credits = tool == "copper_status" and args.get("detail") == "credits"        # the meter names no project
        if tool in ABS_DIR_TOOLS and paths.abs and not is_error and not credits and not _has_abs_dir(payload, paths):
            out_payload["dir"] = paths.abs
        hint = WIRE_HINTS.get(tool or "")
        if hint and defaulted and not is_error and "more" not in out_payload:
            out_payload["more"] = hint

    # images: at most what was asked for, never larger than needed, captions without paths
    content_tail, n_images, dropped = [], 0, 0
    explicit_many = tool in IMAGE_EXEMPT and (tool != "render" or args.get("sheet") or args.get("target") != "schematic")
    limit = 99 if (explicit_many or image_mode() == "auto") else 1
    for block, cap in zip(blocks, captions):
        if block.get("type") != "image":
            content_tail.append(block)
            continue
        if n_images >= limit:
            dropped += 1
            continue
        if not raw:
            data = block.get("data") or ""
            if args.get("width") is None:
                data = _shrink(data, IMAGE_MAX_EDGE)
            if args.get("target") != "pcb3d":            # a shaded 3D view is not flat colour: left as rendered
                data = _fewer_bytes(data)
            if data is not block.get("data"):
                block = dict(block, data=data)
        content_tail.append(block)
        n_images += 1
        if cap:
            what, sep, where = cap.partition(" — saved at ")
            content_tail.append({"type": "text", "text": what})
            if sep and not raw:
                rel = paths.value(where.strip())
                if rel not in _dumps(out_payload):
                    key = "image" if "image" not in out_payload else f"image_{n_images}"
                    out_payload[key] = rel
    if dropped and not raw:
        out_payload["images_not_attached"] = dropped
        if tool == "render":
            out_payload.setdefault("next", "render(target='schematic', sheet='<name>') shows one of the other sheets")

    # cap
    cap_b = max_bytes if max_bytes is not None else _max_bytes(tool, args)
    if cap_b and not raw:
        capped, cut = cap_payload(out_payload, summary, cap_b)
        if cut:
            where = _spill(tool or "result", out_payload, project_dir)
            capped["cut"] = {"lists": cut, **({"file": where} if where else {}),
                             "note": "lists shortened to fit; [shown, total] per list" + (", full result in file" if where else "")}
            out_payload = capped
    text = (summary + "\n" if summary else "") + _dumps(out_payload)
    out = {"content": [{"type": "text", "text": text}] + content_tail, "isError": is_error}
    if os.environ.get("COPPER_STRUCTURED", "").strip() == "1" and out_payload:
        out["structuredContent"] = json.loads(_dumps(out_payload))
    return out


def _max_bytes(tool: str | None, args: dict) -> int:
    try:
        base = int(os.environ.get("COPPER_MAX_RESULT_BYTES", DEFAULT_MAX_BYTES))
    except ValueError:
        base = DEFAULT_MAX_BYTES
    if base <= 0:
        return 0
    try:
        from . import registry
        t = registry.TOOLS.get(tool or "")
        declared = getattr(t, "max_bytes", None) if t else None
    except Exception:  # noqa: BLE001
        declared = None
    if declared:
        base = max(base, int(declared))
    if any(args.get(k) is not None for k in DETAIL_ARGS):
        base *= DETAIL_FACTOR
    return base
