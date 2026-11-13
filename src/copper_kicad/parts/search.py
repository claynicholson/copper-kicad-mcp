"""LCSC / JLCPCB part search.

Sources, combined and de-duplicated by LCSC number:

* ``seed``      — bundled offline table of JLC Basic + Preferred + curated
                  common parts (instant, always available; stock is a snapshot).
* ``jlc``       — JLCPCB's own parts-library API (authoritative *assembly*
                  stock, Basic/Extended class, Preferred flag, price ladder).
                  Rate-limited to ~1 req/s, honest User-Agent, cached 1 h.
* ``jlcsearch`` — jlcsearch.tscircuit.com (MIT, Cloudflare D1). Used as a
                  fallback / supplement; its intermittent "D1 overloaded"
                  errors are retried with backoff. Its free-text
                  ``/api/search`` route is known-broken (returns unrelated
                  parts), so we use ``/components/list.json?search=`` and the
                  typed category routes (SI numbers).

Descriptive queries ('alcohol gas sensor', 'wifi module', 'sd card socket') have no value or part
number to anchor on. They are matched word by word (whole words, plural-insensitive) against each
part's description and catalogue category, and — because JLC's keyword matcher only knows the exact
catalogue wording ('gas sensors' finds 81 parts, 'gas sensor' none) — the live sources are asked
with wording variants, then by the catalogue category the matching parts turn out to live in.
A part that confirms only some of the words comes back match='partial' with the rest listed as
`unconfirmed`.

Ranking: spec relevance first (a '10k 0603' query only returns 10 kΩ 0603
parts at the top), then Basic > Preferred > Extended, then in-stock, stock,
price.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Iterable

from . import common as C
from .common import NetError

JLC_LIST_URL = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"
JLCSEARCH = "https://jlcsearch.tscircuit.com"
LIVE_TTL = 3600          # JLC / jlcsearch results
NEG_TTL = 600


# ── record normalisation ─────────────────────────────────────────────────
def _rec(**kw) -> dict:
    r = {
        "lcsc": kw.get("lcsc"), "mfr": kw.get("mfr") or "", "manufacturer": kw.get("manufacturer") or "",
        "package": kw.get("package") or "", "category": kw.get("category") or "",
        "subcategory": kw.get("subcategory") or "", "description": (kw.get("description") or "").strip(),
        "basic": bool(kw.get("basic")), "preferred": bool(kw.get("preferred")),
        "stock": kw.get("stock"), "stock_source": kw.get("stock_source"),
        "price": kw.get("price") or [], "datasheet": kw.get("datasheet") or "",
        "attributes": kw.get("attributes") or {}, "source": kw.get("source"),
    }
    r["class"] = C.jlc_class(r["basic"], r["preferred"])
    r["kind"] = kw.get("kind") or C.classify(r["category"], r["subcategory"], r["description"], r["mfr"])
    r["typed"] = kw.get("typed") or C.typed_fields(r["kind"], r["attributes"], r["description"], r["mfr"])
    for k in ("min_order", "attrition", "lcsc_url", "image", "retail_stock"):
        if kw.get(k) is not None:
            r[k] = kw[k]
    return r


def from_jlc(it: dict) -> dict:
    attrs = {}
    for a in it.get("attributes") or []:
        n, v = a.get("attribute_name_en"), a.get("attribute_value_name")
        if n and v not in (None, "", "-"):
            attrs[n] = v
    lib = (it.get("componentLibraryType") or "").lower()
    return _rec(
        lcsc=it.get("componentCode"), mfr=it.get("componentModelEn"), manufacturer=it.get("componentBrandEn"),
        package=it.get("componentSpecificationEn"),
        # JLC's firstSortName is the *sub*category, secondSortName the parent
        category=it.get("secondSortName") or "", subcategory=it.get("firstSortName") or it.get("componentTypeEn") or "",
        description=it.get("describe") or it.get("erpComponentName") or "",
        basic=lib == "base", preferred=bool(it.get("preferredComponentFlag")),
        stock=it.get("stockCount"), stock_source="jlc-assembly",
        price=C.price_ladder(it.get("componentPrices") or []),
        datasheet=it.get("dataManualUrl") or "", attributes=attrs, source="jlc",
        min_order=it.get("leastPatchNumber") or it.get("minPurchaseNum"), attrition=it.get("lossNumber"),
        lcsc_url=it.get("lcscGoodsUrl"), image=it.get("componentImageUrl"))


def from_jlcsearch(it: dict) -> dict:
    attrs = it.get("attributes")
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except ValueError:
            attrs = {}
    price = it.get("price")
    if isinstance(price, (int, float)):
        price = [{"qty": 1, "usd": float(price)}]
    elif isinstance(price, str):
        price = C.price_ladder(price)
    elif it.get("price1") is not None:
        price = [{"qty": 1, "usd": float(it["price1"])}]
    return _rec(
        lcsc=C.lcsc_norm(it.get("lcsc")), mfr=it.get("mfr"), package=it.get("package"),
        category=it.get("category") or "", subcategory=it.get("subcategory") or "",
        description=it.get("description"), basic=it.get("is_basic"), preferred=it.get("is_preferred"),
        stock=it.get("stock"), stock_source="jlcsearch-snapshot" if it.get("stock") is not None else None,
        price=price or [], attributes=attrs or {}, source="jlcsearch")


# ── JLC live API ─────────────────────────────────────────────────────────
def jlc_query(keyword: str, *, page_size: int = 50, basic_only: bool = False, preferred_only: bool = False,
              in_stock: bool = True, ttl: float = LIVE_TTL, category: str | None = None) -> list[dict]:
    """category = an exact (case-sensitive) JLC catalogue sub-category name, e.g. 'Gas Sensors'."""
    body = {"keyword": keyword or "", "currentPage": 1, "pageSize": max(1, min(100, page_size))}
    if category:
        body["secondSortName"] = category      # JLC's request field for what its rows call firstSortName
    if basic_only:
        body["componentLibraryType"] = "base"
    if preferred_only:
        body["preferredComponentFlag"] = True
    if in_stock:
        body["stockFlag"] = True
    key = json.dumps(body, sort_keys=True)
    hit = C.cache_get("jlc", key, ttl)
    if hit is not None:
        return [from_jlc(x) for x in hit]
    d = C.http_json(JLC_LIST_URL, body, retries=2, backoff=2.0)
    if not isinstance(d, dict) or d.get("code") != 200:
        raise NetError(f"JLC API returned {str(d)[:160]}")
    lst = ((d.get("data") or {}).get("componentPageInfo") or {}).get("list") or []
    lst = [x for x in lst if not str(x.get("componentCode", "")).startswith("C99")]  # customer-consigned junk
    C.cache_put("jlc", key, lst)
    return [from_jlc(x) for x in lst]


def jlc_part(lcsc: str, ttl: float = LIVE_TTL) -> dict | None:
    """Authoritative assembly record for one C-number (or None if JLC doesn't list it)."""
    code = C.lcsc_norm(lcsc)
    for r in jlc_query(code, page_size=5, in_stock=False, ttl=ttl):
        if r["lcsc"] == code:
            return r
    return None


# ── jlcsearch ────────────────────────────────────────────────────────────
def _d1_overloaded(body: bytes) -> bool:
    return b"overloaded" in body[:400] or b"D1_ERROR" in body[:400]


def jlcsearch_get(path: str, params: dict, ttl: float = LIVE_TTL) -> dict:
    url = f"{JLCSEARCH}{path}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
    hit = C.cache_get("jlcsearch", url, ttl)
    if hit is not None:
        return hit
    d = C.http_json(url, headers={"User-Agent": C.UA_BROWSER}, retries=4, backoff=1.5, retry_if=_d1_overloaded)
    if isinstance(d, dict) and d.get("error"):
        raise NetError(f"jlcsearch: {d.get('message') or d.get('error')}")
    C.cache_put("jlcsearch", url, d)
    return d


def jlcsearch_components(search: str, limit: int = 30) -> list[dict]:
    d = jlcsearch_get("/components/list.json", {"search": search, "limit": limit})
    return [from_jlcsearch(x) for x in d.get("components", [])]


_JS_ROUTES = {"resistor": ("/resistors/list.json", "resistors", "resistance", "ohm"),
              "capacitor": ("/capacitors/list.json", "capacitors", "capacitance", "F")}


def jlcsearch_parametric(kind: str, value: float, package: str | None, basic_only=False) -> list[dict]:
    if kind not in _JS_ROUTES:
        return []
    path, key, field, _ = _JS_ROUTES[kind]
    params = {field: f"{value:.12g}", "package": package}
    if basic_only:
        params["is_basic"] = "true"
    d = jlcsearch_get(path, params)
    out = []
    for x in d.get(key, []):
        r = from_jlcsearch(x)
        if kind == "resistor":
            r["typed"].setdefault("ohm", x.get("resistance"))
        else:
            r["typed"].setdefault("F", x.get("capacitance_farads"))
            if x.get("voltage_rating") is not None:
                r["typed"].setdefault("V", x.get("voltage_rating"))
        r["kind"] = kind
        out.append(r)
    return out


# ── query parsing + matching ─────────────────────────────────────────────
def parse_query(q: str) -> dict:
    from .basic import parse_spec
    return parse_spec(q)


def match_score(r: dict, spec: dict) -> int:
    """0 = strong match, 1 = partial, 2 = token-only, 9 = contradicts spec."""
    from .basic import spec_matches
    return spec_matches(r, spec)


def rank_key(r: dict, score: int = 0, spec: dict | None = None):
    """Same ranking as spec placement and BOM sourcing (basic._rank)."""
    from .basic import _rank
    return _rank(dict(r, _score=score), spec or {})


def apply_filters(rows: Iterable[dict], f: dict) -> list[dict]:
    from .basic import pkg_match
    out = []
    pkg = C.norm_pkg(f.get("package")) if f.get("package") else None
    cat = (f.get("category") or "").lower()
    for r in rows:
        if f.get("basic_only") and not r.get("basic"):
            continue
        if f.get("basic_or_preferred") and not (r.get("basic") or r.get("preferred")):
            continue
        if pkg and not pkg_match(r.get("package"), pkg):
            continue
        if cat and not _category_match(cat, r):
            continue
        st = r.get("stock")
        if f.get("in_stock") and st is not None and st <= 0:
            continue
        if f.get("min_stock") and (st or 0) < int(f["min_stock"]):
            continue
        out.append(r)
    return out


# ── descriptive queries ──────────────────────────────────────────────────
JLC_VARIANT_CALLS = 5        # extra live JLC keyword queries one descriptive search may make (~1 s each, cached)
JLCSEARCH_VARIANT_CALLS = 3
JLC_CATEGORY_CALLS = 2
_STOP_WORDS = {"smd", "smt", "tht", "part", "parts", "the", "a", "an", "for", "with", "and", "or", "of", "to", "in", "on",
               "ic", "ics", "chip", "chips", "component", "components", "type", "pin", "pins"}
# query wording → catalogue wording (a hit on either counts)
_SYNONYMS = {"socket": ("connector", "holder", "receptacle"), "jack": ("connector",), "mic": ("microphone",),
             "pot": ("potentiometer",), "trimmer": ("potentiometer",), "ethanol": ("alcohol",),
             "display": ("lcd", "oled"), "screen": ("display", "lcd", "oled"), "opto": ("optocoupler", "optoisolator"),
             "optocoupler": ("optoisolator",), "ble": ("bluetooth",), "speaker": ("buzzer",), "wlan": ("wifi",)}
_JUNK_CATEGORIES = {"", "new arrivals", "global sourcing parts", "others", "other"}


def _singular(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("ches", "shes", "sses", "xes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def _toggle_plural(w: str) -> str:
    """'sensor' <-> 'sensors': JLC's keyword matcher needs the catalogue's own number."""
    sg = _singular(w)
    if sg != w:
        return sg
    if re.search(r"[^aeiou]y$", w):
        return w[:-1] + "ies"
    return w + ("es" if w.endswith(("s", "x", "ch", "sh")) else "s")


def _tokens(text: str) -> set:
    t = str(text or "").lower()
    toks = set(re.findall(r"[a-z0-9]+", t))
    toks.update(x.replace("-", "") for x in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)+", t))   # 'wi-fi' → 'wifi'
    return toks


def _record_tokens(r: dict) -> set:
    return _tokens(" ".join(str(r.get(k) or "") for k in ("mfr", "description", "package", "category", "subcategory")))


def _word_hit(w: str, toks: set) -> bool:
    """Whole-word match, plural-insensitive; a word of 4+ letters also matches as a prefix ('temp' ~
    'temperature'). Never a substring: 'gas' is not in 'CSBGA', 'sd' is not in 'X49SM8MSD2SC'."""
    def one(x):
        sx = _singular(x)
        for t in toks:
            if t == x or _singular(t) == sx or (len(x) >= 4 and t.startswith(x)):
                return True
        return False
    return one(w) or any(one(s) for s in _SYNONYMS.get(w, ()))


def word_hits(words: list, r: dict) -> tuple[int, list]:
    """(how many of `words` the record confirms, the ones it does not)."""
    toks = _record_tokens(r)
    missing = [w for w in words if not _word_hit(w, toks)]
    return len(words) - len(missing), missing


def raw_words(spec: dict, raw: str) -> list:
    """The describing words of a query: everything that is not the part number, a package or a number."""
    from .basic import _PKG_RE
    mpn = str(spec.get("mpn") or "").lower()
    kws = [str(k).lower() for k in spec.get("keywords") or []]
    out = []
    for t in re.split(r"[\s,;/]+", str(raw or "").lower().replace("wi-fi", "wifi")):
        t = t.strip("()[]\"'")
        if not t or t == mpn or _PKG_RE.match(t):
            continue
        if re.search(r"\d", t):
            if t in kws:
                out.append(t)                    # 'rs485', 'i2c' when another token is the part number
            continue
        out += [x for x in re.split(r"[^a-z]+", t) if x]
    seen, uniq = set(), []
    for w in out:
        if len(w) > 1 and w not in _STOP_WORDS and w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq


def _anchored(spec: dict) -> bool:
    return bool(spec.get("kind") or spec.get("value") is not None)


def query_words(spec: dict, raw: str) -> list:
    """The words a result must confirm. With a kind or value to anchor on ('SD card connector'), only the
    words the spec parser could not place; otherwise every describing word of the query."""
    if _anchored(spec):
        out = []
        for k in spec.get("keywords") or []:
            k = str(k).lower()
            if len(k) > 1 and k not in _STOP_WORDS and k not in out:
                out.append(k)
        return out
    return raw_words(spec, raw)


def keyword_variants(words: list) -> list:
    """Other ways to say the same thing to a keyword matcher, most specific first: the last word's
    number toggled, then (3+ words) one word dropped."""
    if not words:
        return []
    out = []

    def both(ws):
        out.append(" ".join(ws))
        out.append(" ".join(ws[:-1] + [_toggle_plural(ws[-1])]))

    both(words)
    if len(words) >= 3:
        drops = [words[1:], words[:-1]] + [words[:i] + words[i + 1:] for i in range(1, len(words) - 1)]
        out += [" ".join(ws) for ws in drops]    # 'gas sensor', 'alcohol gas', 'alcohol sensor'; 'sd card'
        out += [" ".join(ws[:-1] + [_toggle_plural(ws[-1])]) for ws in drops]
    seen, uniq = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def _mpn_refine(score: int, q_raw: str, m_raw: str) -> int:
    """A short part number found INSIDE a longer one is usually a coincidence: 'MQ-3' is not Samsung's
    'CL03A105MQ3CSNH', nor 'MQ360R0' (the number goes on). Longer or separator-bounded hits keep their score."""
    from .basic import _alnum, mpn_family
    q, m = _alnum(q_raw), _alnum(m_raw)
    if not q or q == m or q not in m or mpn_family(q, m):
        return score
    raw = str(m_raw or "").lower()
    idx = [j for j, ch in enumerate(raw) if re.match(r"[a-z0-9]", ch)]
    if len(idx) != len(m):
        return score
    i = m.find(q)
    after = raw[idx[i + len(q) - 1] + 1:]
    if q[-1].isdigit() and re.match(r"\.?\d", after):          # '…MQ+30A…': the number is 30, not 3
        return 9 if len(q) <= 4 else max(score, 1)
    if i == 0:
        return score
    bounded = idx[i - 1] != idx[i] - 1           # a separator right before the hit: 'SY-CO2-24BPT'
    if not bounded and len(q) <= 4:
        return 9 if len(q) <= 3 else max(score, 2)
    return score


def _described(words: list, r: dict) -> tuple[int, list]:
    """Score a record on describing words alone: all confirmed → 0; at least half (and two) → 1 partial;
    fewer → 9. One word matching by itself ('sensor') is never a partial match for 'alcohol gas sensor'."""
    if not words:
        return 9, []
    hits, missing = word_hits(words, r)
    if not missing:
        return 0, []
    if hits >= max(2, (len(words) + 1) // 2):
        return 1, missing
    return 9, missing


def soft_terms(spec: dict, raw: str) -> list:
    """Ratings in a plain description ('relay 5V', 'buzzer 12mm') that no structured check covers: a result
    that does not show them is 'partial', never rejected."""
    from .basic import _PKG_RE
    if _anchored(spec) or spec.get("mpn"):
        return []
    out = []
    for t in re.split(r"[\s,;/]+", str(raw or "").lower()):
        t = t.strip("()[]\"'")
        if re.fullmatch(r"\d+(?:\.\d+)?[a-z]+", t) and not _PKG_RE.match(t) and t not in out:
            out.append(t)
    return out


def describe_score(r: dict, spec: dict, words: list | None = None, soft: list | None = None) -> tuple[int, list]:
    """(score, unconfirmed words). The structured part of the spec (kind, value, package, part number …) is
    scored by basic.spec_matches; the describing words are matched here, as whole words."""
    from .basic import _alnum, spec_matches
    words = list(words or [])
    anchored = _anchored(spec)
    st = dict(spec, keywords=[])
    if not anchored:
        st.pop("gender", None)                   # 'socket' is a describing word when no connector kind was parsed
    mpn = st.get("mpn")
    s = spec_matches(r, st)
    if s < 9 and mpn:
        s = _mpn_refine(s, mpn, r.get("mfr"))
    if not words:
        return s, []
    if anchored or mpn:
        best = (9, [])
        if s < 9:
            hits, missing = word_hits(words, r)
            best = (s, []) if not missing else (max(s, 1 if hits else 2), missing)
        if mpn and not anchored and best[0] > 0:
            # the 'part number' may be one more describing word ('i2c temperature sensor', 'MQ-3 gas sensor'):
            # a part that confirms the description without carrying that number is still an answer
            st.pop("mpn")
            s2 = spec_matches(r, st) if st.get("package") else 0
            if s2 < 9:
                toks = _record_tokens(r)
                missing = [w for w in words if not _word_hit(w, toks)]
                if _alnum(mpn) not in toks:
                    missing.append(str(mpn))
                n = len(words) + 1
                d = 0 if not missing else (1 if n - len(missing) >= max(2, (n + 1) // 2) else 9)
                d = max(d, s2)
                if d < best[0]:
                    best = (d, missing)
        return best
    if s >= 9:
        return 9, []
    if not st.get("package"):
        s = 0                                    # nothing structured to match on: the words decide
    d, missing = _described(words, r)
    s = max(s, d)
    if s < 9 and soft:
        text = f"{r.get('mfr') or ''} {r.get('description') or ''} {r.get('package') or ''}".lower()
        unseen = [t for t in soft if not re.search(rf"(?<![0-9.]){re.escape(t)}", text)]
        if unseen:
            s, missing = max(s, 1), missing + unseen
    return s, missing


def _category_match(cat: str, r: dict) -> bool:
    """category filter: a kind ('mosfet'), or catalogue wording in any number ('sensor', 'gas sensors',
    'wifi module' ~ 'WiFi Modules')."""
    hay = f"{r.get('kind') or ''} {r.get('subcategory') or ''} {r.get('category') or ''}".lower()
    if cat in hay:
        return True
    ws = [w for w in re.findall(r"[a-z0-9]+", cat) if w not in _STOP_WORDS]
    toks = _tokens(hay)
    return bool(ws) and all(_word_hit(w, toks) for w in ws)


def _catalogue_category(r: dict) -> str | None:
    """The record's JLC sub-category in the catalogue's own spelling (jlcsearch re-capitalises it; JLC's
    category filter is case-sensitive, and every JLC description ends with the real name)."""
    sub = str(r.get("subcategory") or "").strip()
    if sub.lower() in _JUNK_CATEGORIES:
        return None
    m = re.search(re.escape(sub), r.get("description") or "", re.I)
    return m.group(0) if m else sub


def _dominant_categories(rows: list, words: list, n: int = JLC_CATEGORY_CALLS) -> list:
    """Catalogue categories most of the matching rows live in, whose NAME shares a word with the query."""
    count: dict = {}
    for r in rows:
        name = _catalogue_category(r)
        if name:
            count[name] = count.get(name, 0) + 1
    total = sum(count.values())
    out = []
    for name, c in sorted(count.items(), key=lambda kv: -kv[1]):
        toks = _tokens(name)
        if c * 5 >= total and any(_word_hit(w, toks) for w in words):
            out.append(name)
    return out[:n]


def _jlc_keyword(spec: dict, raw: str) -> str:
    """JLC's keyword matcher is token-based and works best with MPN-ish terms
    and canonical value strings ('10k 0603', '100nF 0402')."""
    if spec.get("mpn"):
        return spec["mpn"]
    toks = []
    v = spec.get("value")
    kind = spec.get("kind")
    if kind == "header" and spec.get("pins"):
        # JLC's matcher knows the catalogue wording '1x5P' / '2x10P', not 'Header 1x5'
        rows = spec.get("rows") or 1
        cols = spec["pins"] // rows if spec["pins"] % rows == 0 else spec["pins"]
        toks.append(f"{rows}x{cols}P")
        if spec.get("pitch"):
            toks.append(f"{spec['pitch']:g}mm")
        return " ".join(toks)
    if kind == "usb_connector" and spec.get("usb") == "a":
        return "Type-A" + (" Female" if spec.get("gender") == "female" else "")
    if v is not None:
        if kind == "resistor":
            toks.append(C.fmt_si(v, "ohm").replace("Ω", "") + ("Ω" if v < 1000 else ""))
        elif kind in ("capacitor", "electrolytic", "tantalum"):
            toks.append(C.fmt_si(v, "F"))
        elif kind in ("inductor",):
            toks.append(C.fmt_si(v, "H"))
        elif kind in ("crystal", "oscillator"):
            toks.append(C.fmt_si(v, "Hz"))
            m = re.fullmatch(r"(?:SMD-?)?(\d{4})", str(spec.get("package") or ""), re.I)
            if m:
                toks.append(m.group(1))           # '8MHz 3225' → the SMD3225-4P parts, not 100 HC-49s
    if spec.get("voltage") and kind in ("capacitor", "electrolytic", "tantalum"):
        toks.append(C.fmt_si(spec["voltage"], "V"))
    if spec.get("package") and spec["package"] in C.CHIP_SIZES:
        toks.append(spec["package"])
    if not toks:
        return raw
    if kind == "led":
        toks += [spec.get("color") or "", "LED"]
    return " ".join(t for t in toks if t)


def search(query: str = "", filters: dict | None = None, limit: int = 10, live: bool = True,
           sources: tuple = ("seed", "jlc", "jlcsearch"), raw: bool = False, expand: bool = False) -> dict:
    """Combined search. Returns {'results': [...], 'sources': {name: status}, 'spec': parsed}.
    raw=True also returns the full ranked records (with '_score') under 'records'.
    expand=True (parts_search) spends extra live queries on a descriptive query that found too little —
    wording variants, then the catalogue category — and lists out-of-stock matches under 'out_of_stock'
    when nothing in stock carries the asked part number."""
    from . import basic
    f = dict(filters or {})
    f.setdefault("in_stock", True)
    q = (query or "").strip()
    spec = parse_query(q) if q else {}
    if q and f.get("package") and not spec.get("package"):
        # parse with the package in view: '7805' + package TO-220 is a part number, not a 7805 Ω resistor
        spec = parse_query(f"{q} {f['package']}")
        spec["raw"] = q
    if f.get("package") and not spec.get("package"):
        spec["package"] = C.norm_pkg(f["package"])
    cat = str(f.get("category") or "").strip().lower()
    if q and cat in C.KINDS and not spec.get("kind"):
        spec["kind"] = cat                 # category='usb_connector' + 'USB-A' → kind-filtered ranking
    words = query_words(spec, q) if q else []
    phrase = raw_words(spec, q) if q else []
    descriptive = bool(words) and spec.get("value") is None
    soft = soft_terms(spec, q) if descriptive else []
    mpn = spec.get("mpn")
    status: dict[str, str] = {}
    pool: dict[str, dict] = {}

    def add(rows, prefer=False):
        for r in rows:
            if not r.get("lcsc"):
                continue
            old = pool.get(r["lcsc"])
            if old is None or prefer:
                if old is not None:
                    # keep richer typed/attrs from the other record
                    for k in ("typed", "attributes", "datasheet", "description"):
                        if not r.get(k) and old.get(k):
                            r[k] = old[k]
                pool[r["lcsc"]] = r

    def score_all(rows=None, flt=None, use_words=True):
        got = []
        for r in apply_filters(pool.values() if rows is None else rows, f if flt is None else flt):
            s, missing = describe_score(r, spec, words if use_words else [], soft) if spec else (0, [])
            if s < 9:
                got.append((r, s, missing))
        # among partial matches, the one that confirms the query's rarest word comes first: for 'alcohol gas
        # sensor' an alcohol sensor (no 'gas') beats the eighty gas sensors that do not say 'alcohol'
        seen: dict = {}
        for _r, _s, missing in got:
            for w in missing:
                seen[w] = seen.get(w, 0) + 1
        out = []
        for r, s, missing in got:
            cost = round(sum(1.0 / (1 + len(got) - seen[w]) for w in missing), 6)
            out.append(((s, cost, rank_key(r, s, spec)), r, s, missing))
        out.sort(key=lambda t: t[0])
        return out

    def has_mpn(t) -> bool:
        return not mpn or str(mpn) not in t[3]

    def enough() -> bool:
        """Enough full matches (carrying the asked part number, when there is one) to stop asking."""
        return sum(1 for t in score_all() if t[2] == 0 and has_mpn(t)) >= limit

    # 1. offline seed
    if "seed" in sources:
        if descriptive and not _anchored(spec):
            seed_rows = [dict(r) for r in basic.all_parts()]     # the describing words are matched below, as whole words
        else:
            seed_rows = basic.candidates(spec) if spec else basic.all_parts()
        add(seed_rows)
        status["seed"] = f"ok ({len(seed_rows)} candidates, snapshot {basic.snapshot_date()})"

    # 2. JLC live
    jlc_ok = False
    kw = _jlc_keyword(spec, q) if q else ""
    # a keyword rebuilt from a parsed value ('10k 0603', '1x5P 2.54mm') is already in catalogue wording
    widen = bool(expand and descriptive and (kw == q or mpn))
    stock_flag = bool(f.get("in_stock"))
    if live and "jlc" in sources and q:
        tried, n_rows = [], 0
        try:
            rows = jlc_query(kw, page_size=100, basic_only=bool(f.get("basic_only")), in_stock=stock_flag)
            if spec.get("value") is not None and not f.get("basic_only"):
                rows += jlc_query(kw, page_size=50, basic_only=True, in_stock=stock_flag)
            if not rows and kw != q:
                rows = jlc_query(q, page_size=100, in_stock=stock_flag)
            add(rows, prefer=True)
            jlc_ok = True
            n_rows = len(rows)
            status["jlc"] = f"ok ({n_rows} rows, live assembly stock)"
            if widen:
                variants = [v for v in keyword_variants(phrase) if v.lower() not in (kw.lower(), q.lower())]
                if cat and cat not in C.KINDS:
                    variants += [v for v in keyword_variants(re.findall(r"[a-z0-9]+", cat)) if v not in variants]
                for v in variants[:JLC_VARIANT_CALLS]:
                    if enough():
                        break
                    rows = jlc_query(v, page_size=100, basic_only=bool(f.get("basic_only")), in_stock=stock_flag)
                    tried.append(v)
                    add(rows, prefer=True)
                    n_rows += len(rows)
                status["jlc"] = f"ok ({n_rows} rows, live assembly stock)"
        except NetError as e:
            if not jlc_ok:
                status["jlc"] = f"unavailable: {e}"
        if tried:
            status["jlc"] += "; also asked " + ", ".join(f"'{v}'" for v in tried)

    # 3. jlcsearch (fallback / supplement). Its search reads descriptions, so it finds what JLC's keyword matcher misses.
    short = (not enough()) if widen else len(pool) < limit
    if live and "jlcsearch" in sources and q and (not jlc_ok or short):
        try:
            rows = []
            if spec.get("kind") in _JS_ROUTES and spec.get("value") is not None:
                rows = jlcsearch_parametric(spec["kind"], spec["value"], spec.get("package"), bool(f.get("basic_only")))
            else:
                rows = jlcsearch_components(mpn or q, limit=40)
            # jlcsearch stock can be stale/null: never let it override a JLC record
            add([r for r in rows if r["lcsc"] not in pool])
            n_rows = len(rows)
            if widen:
                asked = str(mpn or q).lower()
                variants = [" ".join(phrase)] + ([" ".join(phrase[1:]), " ".join(phrase[:-1])] if len(phrase) >= 3 else [])
                for v in [v for v in variants if v and v != asked][:JLCSEARCH_VARIANT_CALLS - 1]:
                    if enough():
                        break
                    rows = jlcsearch_components(v, limit=40)
                    add([r for r in rows if r["lcsc"] not in pool])
                    n_rows += len(rows)
            status["jlcsearch"] = f"ok ({n_rows} rows)"
        except NetError as e:
            status["jlcsearch"] = f"unavailable: {e}"

    # 4. the catalogue category the matching parts live in, asked from JLC for live assembly stock
    if live and jlc_ok and widen and not enough():
        try:
            matched = [t[1] for t in score_all() if t[2] <= 1]
            asked_cats = []
            for name in _dominant_categories(matched, phrase or words):
                have = sum(1 for r in pool.values() if r.get("source") == "jlc" and r.get("subcategory") == name)
                if have >= 20:
                    continue                      # a keyword query already brought this category in
                add(jlc_query("", page_size=100, basic_only=bool(f.get("basic_only")), in_stock=stock_flag, category=name),
                    prefer=True)
                asked_cats.append(name)
            if asked_cats:
                status["jlc"] += "; category " + ", ".join(f"'{c}'" for c in asked_cats)
        except NetError:
            pass

    n_considered = len(apply_filters(pool.values(), f))
    scored = score_all()
    best_score = scored[0][2] if scored else 0
    out = []
    for _, r, s, missing in scored[: max(1, limit)]:
        out.append(present(r, relevance=s, unconfirmed=missing))
    res = {"results": out, "sources": status,
           "spec": {k: v for k, v in spec.items() if k != "raw" and not k.startswith("_") and v not in (None, "", [])},
           "total_considered": n_considered, "best_relevance": best_score, "note": CLASS_NOTE}
    if descriptive and not _anchored(spec):
        res["words"] = words
    mpn_found = any(has_mpn(t) and t[2] <= 1 for t in scored)
    if mpn and scored and not mpn_found:
        res["match_note"] = (f"no {'in-stock ' if stock_flag else ''}part carries the part number '{mpn}': these match the "
                             f"description only (match='partial'; `unconfirmed` lists what each result does not confirm)")
    elif scored and best_score == 1 and scored[0][3]:
        res["match_note"] = ("no part confirms every word of the query: results are match='partial' and `unconfirmed` "
                             "lists the words each one does not confirm — check the datasheet before using one")

    # 5. nothing in stock answers the query: say what exists but is out of assembly stock (never as a result)
    if expand and live and jlc_ok and stock_flag and q and (not scored or (mpn and not mpn_found)):
        try:
            f2 = {k: v for k, v in f.items() if k not in ("in_stock", "min_stock")}
            rows = [r for r in jlc_query(kw, page_size=50, in_stock=False) if r.get("lcsc") not in pool]
            # a part that carries the asked number answers the query whatever its catalogue text says
            oos = [t for t in score_all(rows, f2, use_words=not mpn) if t[2] <= 1 and has_mpn(t)]
            if oos:
                res["out_of_stock"] = [present(r, relevance=s, unconfirmed=missing) for _, r, s, missing in oos[:3]]
        except NetError:
            pass
    if raw:
        res["records"] = [dict(r, _score=s) for _, r, s, _m in scored[: max(1, limit)]]
    return res


CLASS_NOTE = (f"class: Basic = no setup fee; Preferred = no fee on JLC Economic PCBA; Extended = ~${C.JLC_SETUP_FEE_USD:.0f} "
              "feeder setup per unique part per order. stock = JLC assembly stock.")


def _short_desc(s: str, n: int = 110) -> str:
    s = re.sub(r"\s*ROHS\s*$", "", re.sub(r"-?\d+℃~\+?\d+℃\s*", "", s)).strip()
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


def present(r: dict, relevance: int | None = None, unconfirmed: list | None = None) -> dict:
    """Compact, model-friendly view of a part record."""
    passive = r.get("kind") in ("resistor", "capacitor", "inductor", "ferrite", "led", "electrolytic", "tantalum")
    d = {
        "lcsc": r["lcsc"], "class": r.get("class"), "mfr_part": r.get("mfr"), "manufacturer": r.get("manufacturer") or None,
        "package": r.get("package"), "kind": r.get("kind"), "description": _short_desc(r.get("description") or ""),
        "stock": r.get("stock"),
        "price_usd": C.ladder_str(r.get("price") or [], 3) or None,
        # datasheets matter for ICs/connectors (pinout); for chip passives they are noise
        "datasheet": (r.get("datasheet") or None) if not passive else None,
    }
    if r.get("typed"):
        d["params"] = {k: (round(v, 15) if isinstance(v, float) else v) for k, v in r["typed"].items() if v is not None}
    if str(r.get("stock_source") or "").startswith(("snapshot", "jlcsearch")):
        d["stock_note"] = f"stock from {r['stock_source']} (not live)"
    if r.get("stock") is not None and r["stock"] <= 0:
        d["warning"] = "out of JLC assembly stock"
    if relevance is not None and relevance > 0:
        d["match"] = {1: "partial", 2: "keyword-only"}.get(relevance, "weak")
        if unconfirmed:
            d["unconfirmed"] = list(unconfirmed)       # query words this part's catalogue entry does not confirm
    return {k: v for k, v in d.items() if v is not None}
