"""parts_search speed: batched queries, exact C-number / part-number fast paths, concurrent sources, per-source
time budget, circuit breaker and the 24 h cache. No network: every request goes through a fake
``common._urlopen`` that answers like JLC / jlcsearch after a simulated latency.

Run: "C:/Program Files/KiCad/10.0/bin/python.exe" tests/test_parts_speed.py
"""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
os.environ.setdefault("COPPER_HOME", tempfile.mkdtemp(prefix="copper-test-home-"))
os.environ.pop("COPPER_OFFLINE", None)
from copper_kicad.parts import common as C, search as S  # noqa: E402


# ── a fake catalogue behind a fake HTTP layer ───────────────────────────────────────────────────────────────
def _jlc_item(code, mfr, pkg, desc, stock=1000, basic=False, sub="Microcontrollers (MCU/MPU/SOC)", cat="ICs"):
    return {"componentCode": code, "componentModelEn": mfr, "componentBrandEn": "ACME", "componentSpecificationEn": pkg,
            "secondSortName": cat, "firstSortName": sub, "describe": desc, "stockCount": stock,
            "componentLibraryType": "base" if basic else "expand", "componentPrices": [{"startNumber": 1, "productPrice": 1.5}]}


CAT = {
    "C25804": _jlc_item("C25804", "0603WAF1002T5E", "0603", "10kΩ ±1% 100mW 0603 Chip Resistor - Surface Mount", 900000, True,
                        "Chip Resistor - Surface Mount", "Resistors"),
    "C529355": _jlc_item("C529355", "STM32G431CBT6", "LQFP-48(7x7)", "170MHz 128KB LQFP-48 Microcontrollers (MCU/MPU/SOC)", 4000),
    "C7777": _jlc_item("C7777", "OLDPART123", "SOT-23", "obsolete part", 0),
    # 'C1815' is both an LCSC code (a 330 pF capacitor) and the common name of the 2SC1815 transistor
    "C1815": _jlc_item("C1815", "0805CG331J500NT", "0805", "330pF 50V C0G 0805 Multilayer Ceramic Capacitors MLCC - SMD/SMT",
                       50000, True, "Multilayer Ceramic Capacitors MLCC - SMD/SMT", "Capacitors"),
    "C55127495": _jlc_item("C55127495", "2SC1815-Y", "SOT-23", "NPN 50V 150mA SOT-23 Bipolar (BJT)", 3000, False,
                           "Bipolar (BJT)", "Transistors"),
}
for i in range(8):
    code = f"C90{i}01"
    CAT[code] = _jlc_item(code, f"FASTMCU{i}X7", "QFN-32", f"fake MCU number {i} Microcontrollers (MCU/MPU/SOC)", 500 + i)


class _Resp(io.BytesIO):
    headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeNet:
    """Replaces common._urlopen. latency: seconds per request; fail: {host_part: 'timeout' | 503}."""

    def __init__(self, latency=0.0, fail=None, hang=None):
        self.latency, self.fail, self.hang = latency, dict(fail or {}), dict(hang or {})
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self.active = self.peak = 0

    def __call__(self, req, timeout):
        url = req.full_url
        body = json.loads(req.data) if req.data else None
        with self.lock:
            self.calls.append((url, json.dumps(body, sort_keys=True) if body else ""))
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            for host, secs in self.hang.items():
                if host in url:
                    time.sleep(min(secs, timeout))
                    if secs > timeout:
                        raise socket.timeout("timed out")
            time.sleep(self.latency)
            for host, how in self.fail.items():
                if host in url:
                    if how == "timeout":
                        raise urllib.error.URLError(socket.timeout("timed out"))
                    raise urllib.error.HTTPError(url, int(how), "fail", {}, None)
            return _Resp(json.dumps(self.answer(url, body)).encode())
        finally:
            with self.lock:
                self.active -= 1

    def answer(self, url, body):
        if "jlcpcb.com" in url:
            kw = (body.get("keyword") or "").lower()
            rows = [it for it in CAT.values()
                    if kw and (kw == it["componentCode"].lower() or kw in it["componentModelEn"].lower()
                               or all(w in it["describe"].lower() + " " + it["componentSpecificationEn"].lower()
                                      for w in kw.replace("ω", "").split()))]
            if body.get("stockFlag"):
                rows = [r for r in rows if r["stockCount"] > 0]
            if body.get("componentLibraryType") == "base":
                rows = [r for r in rows if r["componentLibraryType"] == "base"]
            return {"code": 200, "data": {"componentPageInfo": {"list": rows}}}
        if "jlcsearch" in url:
            return {"components": []}
        return {}

    def hosts(self):
        return [("jlc" if "jlcpcb.com" in u else "jlcsearch" if "jlcsearch" in u else u) for u, _b in self.calls]

    def __enter__(self):
        self._orig = C._urlopen
        C._urlopen = self
        return self

    def __exit__(self, *exc):
        C._urlopen = self._orig


def fresh():
    """A clean cache, breaker and rate-limit state for one test (never the real ~/.copper)."""
    os.environ["COPPER_HOME"] = tempfile.mkdtemp(prefix="copper-test-home-")
    assert ".copper" not in os.environ["COPPER_HOME"].replace("copper-test", "")
    C.breaker_reset()
    with C._host_lock:
        C._host_next.clear()


def _tool(args):
    import copper_kicad.tools  # noqa: F401
    from copper_kicad.mcp import registry as R
    return R.call_tool("parts_search", args)


# ── tests ────────────────────────────────────────────────────────────────────────────────────────────────
def test_exact_identifiers():
    assert S.exact_code("C25804") == "C25804" and S.exact_code(" c25804 ") == "C25804"
    assert S.exact_code("LCSC C2040") == "C2040"
    for q in ("10k 0603", "C0603", "25804", "STM32G431CBT6", "C25804 0603", "100nF"):
        assert S.exact_code(q) is None, q
    from copper_kicad.parts import basic
    for q, want in (("STM32G431CBT6", True), ("AMS1117-3.3", True), ("USBLC6-2SC6", True),
                    ("10k 0603", False), ("MQ-3", False), ("ESP32 module", False), ("LED red 0603", False)):
        assert S.confident_mpn(basic.parse_spec(q), q) is want, q


def test_c_number_is_one_lookup_and_cached_24h():
    fresh()
    with FakeNet() as net:
        r = S.search("C25804", limit=5)
        assert [x["lcsc"] for x in r["results"]] == ["C25804"] and r["exact"] == "lcsc", r
        assert net.hosts() == ["jlc"], net.calls                      # one request, no broad search, no jlcsearch
        f = list((Path(os.environ["COPPER_HOME"]) / "cache" / "parts" / "jlc").glob("*.json"))
        assert len(f) == 1, "the JLC answer cached under COPPER_HOME"
        S.search("C25804", limit=5)
        assert len(net.calls) == 1, net.calls                          # second ask: from the cache
        # an answer 3 h old still answers, says its stock is cached, and is NOT re-stored as fresh
        old = time.time() - 3 * 3600
        os.utime(f[0], (old, old))
        r = S.search("C25804")
        x = r["results"][0]
        assert len(net.calls) == 1 and "cached" in x.get("stock_note", ""), x
        assert "cached 3 h ago" in r["sources"]["jlc"] and "live" not in r["sources"]["jlc"], r["sources"]
        assert abs(f[0].stat().st_mtime - old) < 2, "cached stock re-saved with a new date"
        assert not (Path(os.environ["COPPER_HOME"]) / "cache" / "parts" / "lcsc_rec").exists()
        # 25 h old: asked again
        old = time.time() - 25 * 3600
        os.utime(f[0], (old, old))
        S.search("C25804")
        assert len(net.calls) == 2, net.calls


def test_c_number_out_of_stock_and_unknown():
    fresh()
    with FakeNet():
        r = S.search("C7777")
        assert r["results"] == [] and r["out_of_stock"][0]["lcsc"] == "C7777", r
        r = S.search("C7777", {"in_stock": False})
        assert r["results"][0]["lcsc"] == "C7777"
        r = S.search("C25804", {"package": "0402"})
        assert r["results"] == [] and r["filtered_out"][0]["lcsc"] == "C25804" and "filters" in r["match_note"], r
        r = S.search("C123456789")
        assert r["results"] == [] and "not found" in r["match_note"], r


def test_exact_part_number_skips_jlcsearch():
    fresh()
    with FakeNet() as net:
        r = S.search("STM32G431CBT6", limit=5, expand=True)
        assert r["results"][0]["lcsc"] == "C529355", r
        assert net.hosts() == ["jlc"], net.calls
        assert r["sources"]["jlcsearch"].startswith("skipped"), r["sources"]
    fresh()
    with FakeNet() as net:                                             # not an exact hit: jlcsearch still asked
        S.search("STM32G431", limit=5, expand=True)
        assert "jlcsearch" in net.hosts(), net.calls


def test_value_queries_ask_jlc_concurrently():
    fresh()
    from copper_kicad.parts import basic
    basic.candidates(basic.parse_spec("10k 0603"))                    # load the offline table outside the clock
    with FakeNet(latency=0.6) as net:
        t = time.time()
        r = S.search("10k 0603", limit=1)                               # enough rows: no jlcsearch fallback
        dt = time.time() - t
        assert r["results"][0]["lcsc"] == "C25804", r
        assert net.hosts() == ["jlc", "jlc"] and net.peak == 2, (net.calls, net.peak)
        assert dt < 1.15, dt                                           # two 0.6 s requests side by side (0.25 s apart), not 1.2 s+


def test_breaker_skips_a_failing_host_for_ten_minutes():
    fresh()
    with FakeNet(fail={"jlcpcb.com": "timeout"}) as net:
        r = S.search("C25804")
        assert "unavailable" in r["sources"]["jlc"], r["sources"]
        assert C.breaker_state() == {}, C.breaker_state()               # one failed call never opens it
        S.search("C25805")                                              # the second in a row does
        n = net.hosts().count("jlc")
        assert "jlcpcb.com" in C.breaker_state() and C.breaker_state()["jlcpcb.com"]["seconds_left"] > 590
        r = S.search("STM32G431CBT6")
        assert net.hosts().count("jlc") == n, net.calls                # skipped at once, no new request
        assert "skipped" in r["sources"]["jlc"], r["sources"]
        assert "jlcsearch" in r["sources"], r["sources"]                # the other source still answers
    fresh()
    with FakeNet(fail={"jlcsearch": 503}) as net:
        S.search("C99999999")                                           # JLC: not listed -> jlcsearch -> 503
        S.search("C99999998")
        assert "jlcsearch.tscircuit.com" in C.breaker_state(), C.breaker_state()
        n = len(net.calls)
        S.search("C88888888")
        assert net.hosts()[n:] == ["jlc"], net.calls[n:]
    fresh()
    with FakeNet(fail={"jlcsearch": 404}):                              # a 4xx is not an outage
        S.search("C99999999")
        assert C.breaker_state() == {}


def test_a_hanging_source_costs_one_budget():
    fresh()
    old = S.SOURCE_TIMEOUT
    S.SOURCE_TIMEOUT = 1.0
    try:
        with FakeNet(hang={"jlcpcb.com": 30}):
            t = time.time()
            r = S.search("STM32G431CBT6")
            dt = time.time() - t
            assert dt < 3.0, dt
            assert "unavailable" in r["sources"]["jlc"], r["sources"]
            S.search("FASTMCU1X7")                                     # the second timeout in a row opens the breaker
            t = time.time()
            S.search("FASTMCU2X7")
            assert time.time() - t < 0.5                                # breaker: no third wait
    finally:
        S.SOURCE_TIMEOUT = old


def test_short_c_number_is_also_a_part_number():
    fresh()
    from copper_kicad.parts import basic
    assert S.exact_code("C1815") == "C1815" and S.code_is_ambiguous("C1815") and S.code_is_ambiguous("c945")
    assert not S.code_is_ambiguous("C25804") and not S.code_is_ambiguous("LCSC C1815")
    with FakeNet():
        r = S.search("C1815", {"in_stock": True}, limit=3, expand=True)
        assert r["results"] and all(x["kind"] == "bjt" and "C1815" in x["mfr_part"] for x in r["results"]), r  # as before
        assert r["lcsc_code"]["lcsc"] == "C1815" and "LCSC C1815" in r["match_note"], r
        r = S.search("LCSC C1815")                                       # explicitly a code: the capacitor only
        assert [x["lcsc"] for x in r["results"]] == ["C1815"] and r["exact"] == "lcsc", r
        p = basic.pick("C1815", live=True)                              # sch_apply place spec='C1815'
        assert p and p["kind"] == "bjt" and p["lcsc"] != "C1815", p
        r = S.search("C945")                                             # neither a code nor a part number here
        assert r["results"] == [] and "not found" in r["match_note"], r
        r = S.search("C7777")                                            # a code, no part number: the record answers
        assert r["out_of_stock"][0]["lcsc"] == "C7777", r


def test_empty_answers_live_one_hour():
    fresh()
    with FakeNet() as net:
        S.search("C123456789")
        n = len(net.calls)
        S.search("C123456789")
        assert len(net.calls) == n, net.calls                          # 1 h: still from the cache
        old = time.time() - 2 * 3600
        for g in (Path(os.environ["COPPER_HOME"]) / "cache" / "parts").rglob("*.json"):
            os.utime(g, (old, old))
        S.search("C123456789")
        assert len(net.calls) == 2 * n, net.calls                      # 2 h old and empty: asked again
        S.search("C25804")
        f = [g for g in (Path(os.environ["COPPER_HOME"]) / "cache" / "parts" / "jlc").glob("*.json")
             if "0603WAF1002T5E" in g.read_text(encoding="utf-8")]
        os.utime(f[0], (old, old))
        m = len(net.calls)
        S.search("C25804")
        assert len(net.calls) == m, net.calls                          # a non-empty answer keeps its 24 h


def test_discarded_prefetch_never_trips_the_breaker():
    fresh()
    url = S.JLCSEARCH + "/components/list.json?search=x"
    with FakeNet(fail={"jlcsearch": 503}):
        for _ in range(3):
            try:
                C.http(url, retries=0, breaker="check")
            except C.NetError as e:
                assert e.trip == "HTTP 503", e.trip
        assert C.breaker_state() == {}, C.breaker_state()
        # a description query: jlcsearch is prefetched alongside JLC, JLC alone is enough, the failure is ignored
        S.search("fake MCU number", limit=1, expand=True)
        S.search("fake MCU number 3", limit=1, expand=True)
        assert C.breaker_state() == {}, C.breaker_state()
    fresh()
    with FakeNet():                                                      # a success clears the failure count
        C.breaker_fail("jlcpcb.com", "a timeout")
        S.search("C25804")
        C.breaker_fail("jlcpcb.com", "a timeout")
        assert C.breaker_state() == {}, C.breaker_state()


def test_open_jlcsearch_breaker_never_retrips_itself():
    # C14: the breaker's own 'skipped' refusal, a 4xx and an {'error': ...} body are not outages
    host = "jlcsearch.tscircuit.com"
    fresh()
    with FakeNet() as net:                                               # jlcsearch is healthy again
        C.breaker_trip(host, "old outage", seconds=30)
        for q in ("LM358 opamp", "NE555 timer", "TL431 shunt reference"):
            r = S.search(q, expand=True)
            assert "skipped" in r["sources"].get("jlcsearch", ""), r["sources"]
        st = C.breaker_state().get(host)
        assert st and st["seconds_left"] <= 30 and "old outage" in st["why"], C.breaker_state()
        assert host not in C._fails, C._fails
        C.breaker_reset()
        n = net.hosts().count("jlcsearch")
        S.search("LM358 opamp", expand=True)
        assert net.hosts().count("jlcsearch") > n, net.calls            # closed again: asked at once
    for fail in ({"jlcsearch": 404}, None):
        fresh()
        with FakeNet(fail=fail) as net:
            if fail is None:                                             # an {'error': ...} body
                net.answer = lambda url, body, _a=net.answer: {"error": "bad query"} if "jlcsearch" in url else _a(url, body)
            for q in ("LM358 opamp", "NE555 timer", "TL431 shunt reference"):
                r = S.search(q, expand=True)
                assert "unavailable" in r["sources"]["jlcsearch"], r["sources"]
            assert C.breaker_state() == {} and host not in C._fails, (C.breaker_state(), C._fails)
    fresh()
    with FakeNet(fail={"jlcsearch": 503}):                               # a used prefetch that hits a real outage counts
        S.search("LM358 opamp", expand=True)
        S.search("NE555 timer", expand=True)
        assert host in C.breaker_state(), C.breaker_state()


def test_check_mode_success_closes_the_failure_count():
    # C15: a good answer in breaker='check' mode proves the host is up, like any other success
    host = "jlcsearch.tscircuit.com"
    url = S.JLCSEARCH + "/components/list.json?search=y"
    fresh()
    with FakeNet():
        C.breaker_fail(host, "a timeout")
        C.http(url, retries=0, breaker="check")
        assert host not in C._fails, C._fails
        C.breaker_fail(host, "a timeout")
        assert C.breaker_state() == {}, C.breaker_state()
    fresh()
    with FakeNet():                                                      # same through a search's jlcsearch prefetch
        C.breaker_fail(host, "a timeout")
        r = S.search("LM358 opamp", expand=True)
        assert r["sources"]["jlcsearch"].startswith("ok"), r["sources"]
        C.breaker_fail(host, "a timeout")
        assert C.breaker_state() == {}, C.breaker_state()

def test_batch_of_8_with_1s_latency_is_concurrent():
    fresh()
    qs = [f"FASTMCU{i}X7" for i in range(8)]
    with FakeNet(latency=1.0) as net:
        t = time.time()
        out = _tool({"queries": qs, "limit": 2})
        dt = time.time() - t
    sc = out["structuredContent"]
    assert not out.get("isError"), out
    assert sc["order"] == qs and set(sc["queries"]) == set(qs), sc
    for i, q in enumerate(qs):
        assert sc["queries"][q]["results"][0]["id"] == f"C90{i}01", sc["queries"][q]
    assert len(net.calls) == 8 and net.hosts() == ["jlc"] * 8, net.calls   # one exact lookup per part number
    assert net.peak >= 4, net.peak
    print(f"    batch of 8 x 1 s latency: {dt:.2f} s (sequential would be >= 8 s), peak {net.peak} in flight")
    assert dt < 4.0, dt


def test_batch_shapes_overrides_dedupe_and_isolated_errors():
    fresh()
    with FakeNet() as net:
        out = _tool({"queries": ["C25804", {"query": "10k 0603", "package": "0402"}, "10k 0603", "C25804",
                                 {"query": "C25804", "provider": "no_such_provider"}], "limit": 2})
    sc = out["structuredContent"]
    assert not out.get("isError"), out
    assert sc["order"] == ["C25804", "10k 0603", "10k 0603 #2", "C25804 #2"], sc["order"]   # exact duplicate answered once
    assert sc["queries"]["C25804"]["results"][0]["id"] == "C25804"
    assert sc["queries"]["10k 0603"]["results"] == [] or all("0402" in x["package"] for x in sc["queries"]["10k 0603"]["results"])
    assert sc["queries"]["10k 0603 #2"]["results"][0]["id"] == "C25804"
    assert "error" in sc["queries"]["C25804 #2"], sc["queries"]["C25804 #2"]     # one bad query, the rest answered
    assert "note" not in sc["queries"]["C25804"] and sc["note"], sc              # the class note once, not per query
    assert len(sc["queries"]["10k 0603 #2"]["results"]) <= 2
    assert "queries answered" in out["content"][0]["text"].splitlines()[0]
    bad = _tool({"queries": ["10k 0603", {"query": "x", "colour": "red"}]})
    assert bad.get("isError") and "colour" in bad["content"][0]["text"], bad
    none = _tool({})
    assert none.get("isError") and "query" in none["content"][0]["text"], none
    with FakeNet():                                                      # query + queries: query is not dropped
        sc = _tool({"query": "C25804", "queries": ["10k 0603", "C25804"]})["structuredContent"]
    assert sc["order"] == ["C25804", "10k 0603"], sc["order"]


def test_filtered_out_records_are_normalized():
    fresh()
    with FakeNet():
        sc = _tool({"query": "C25804", "package": "0402"})["structuredContent"]
        assert sc["results"] == [] and sc["filtered_out"][0]["id"] == "C25804", sc
        assert sc["filtered_out"][0]["provider"] == "lcsc", sc
        sc = _tool({"query": "C1815"})["structuredContent"]
        assert sc["lcsc_code"]["id"] == "C1815" and sc["lcsc_code"]["provider"] == "lcsc", sc


def test_no_type_unions_in_tool_schemas():
    """Gemini function declarations and OpenAI strict schemas reject `type: [..]`: one would block the whole list."""
    import copper_kicad.tools  # noqa: F401
    from copper_kicad.mcp import registry as R

    def walk(node, path):
        if isinstance(node, dict):
            assert not isinstance(node.get("type"), list), f"{path}: type union {node['type']}"
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
    for name, t in R.TOOLS.items():
        walk(t.schema, name)
    q = R.TOOLS["parts_search"].schema["properties"]["queries"]["items"]
    assert {"type": "string"} in q["anyOf"] and {"type": "object"} in q["anyOf"], q


def test_single_query_unchanged():
    fresh()
    with FakeNet():
        out = _tool({"query": "C25804"})
        sc = out["structuredContent"]
        assert not out.get("isError") and sc["results"][0]["id"] == "C25804" and "queries" not in sc, sc
        assert sc["next"].startswith('sch_apply ops=[{"op":"place","lcsc":"C25804"}]'), sc["next"]
        out = _tool({"query": "STM32G431CBT6", "limit": 5})
        assert out["structuredContent"]["results"][0]["id"] == "C529355"


def test_concurrent_cache_writes_do_not_collide():
    fresh()
    errs = []

    def w(i):
        try:
            for _ in range(30):
                C.cache_put("t", "same-key", {"i": i, "pad": "x" * 2000})
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=w, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and C.cache_get("t", "same-key", None)["pad"] == "x" * 2000
    assert not list(C.cache_dir("t").glob("*.tmp")), list(C.cache_dir("t").glob("*"))


def test_tool_text_mentions_batch():
    import copper_kicad.tools  # noqa: F401
    from copper_kicad.mcp import catalogue, registry as R
    t = R.TOOLS["parts_search"]
    assert "queries" in t.schema["properties"] and "query" not in t.schema.get("required", [])
    assert "queries=[" in t.description
    assert catalogue.slim_for(t) and "queries:" in catalogue.slim_for(t)          # slim text still current


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted((k, v) for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)):
        try:
            fn()
            passed += 1
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            traceback.print_exc(limit=4)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
