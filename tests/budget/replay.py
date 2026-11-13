"""Replay a scripted board build through the stdio MCP server and account for every byte.

Used by tests/test_tokens.py; also runnable on its own to print the per-call table:

    python tests/budget/replay.py tests/budget/c3_legacy.json [report.json]

A script is a JSON list of {"tool", "args", "stage", "alt"?, "expect_error"?}. A board_build that returns
`pending` (its time box) is called again with no arguments, as a client would. "$DIR" inside any string
argument is replaced by the scratch project folder; "alt" names a tool to call when "tool" is not
registered (a tool that is being renamed). Everything runs in throw-away COPPER_HOME / KICAD_CONFIG_HOME
folders (the latter seeded with KiCad's stock library tables), so no user file is read or written.
The scripts only use parts that resolve offline (JLC Basic passives by `spec`, KiCad stock symbols by lib_id).
"""
from __future__ import annotations

import base64
import itertools
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
HERE = Path(__file__).resolve().parent
ENV_KNOBS = ("COPPER_IMAGES", "COPPER_STRUCTURED", "COPPER_MAX_RESULT_BYTES", "COPPER_LEDGER", "COPPER_DEBUG",
             "COPPER_COMPACT", "COPPER_CALL_BUDGET_S")


def png_dims(raw: bytes) -> tuple[int, int]:
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
        return struct.unpack(">II", raw[16:24])
    return (0, 0)


def img_tokens(w: int, h: int) -> int:
    """Neutral yardstick: w*h/750 after the long edge is scaled down to 1568 px."""
    if not w or not h:
        return 0
    s = min(1.0, 1568 / max(w, h))
    return int(w * s * h * s / 750)


def sandbox(prefix: str = "copper budget ") -> dict:
    """Scratch dirs + environment for one replay. The folder names contain a space on purpose."""
    base = Path(tempfile.mkdtemp(prefix=prefix))
    home, kcfg, proj, cfg = base / "home", base / "kcfg", base / "proj dir", base / "cfg"
    for d in (home, kcfg, proj, cfg):
        d.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(SRC))
    try:
        from copper_kicad.kicad import env as kenv
        inst = kenv.kicad()
    finally:
        sys.path.remove(str(SRC))
    ver = ".".join(str(inst.version).split(".")[:2])
    vdir = kcfg / ver
    vdir.mkdir(exist_ok=True)
    tpl = None
    for cand in (Path(inst.root) / "share" / "kicad" / "template", Path(inst.root) / "template",
                 Path("/usr/share/kicad/template")):
        if (cand / "sym-lib-table").exists():
            tpl = cand
            break
    if tpl:
        for n in ("sym-lib-table", "fp-lib-table"):
            shutil.copyfile(tpl / n, vdir / n)
    env = dict(os.environ)
    env.update({"COPPER_HOME": str(home), "KICAD_CONFIG_HOME": str(kcfg), "COPPER_CONFIG_HOME_OVERRIDE": str(cfg),
                "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
    for k in ENV_KNOBS:
        env.pop(k, None)
    return {"base": base, "home": home, "kcfg": kcfg, "proj": proj, "env": env, "lib_tables": bool(tpl)}


class Client:
    def __init__(self, env: dict, stderr_path: Path | None = None):
        launcher = os.environ.get("COPPER_TEST_LAUNCHER")
        cmd = [launcher, "serve"] if launcher else [sys.executable, "-m", "copper_kicad", "serve"]
        self._err = open(stderr_path, "ab") if stderr_path else subprocess.DEVNULL
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._err,
                                  cwd=str(SRC), env=env, text=True, encoding="utf-8", bufsize=1)
        self.ids = itertools.count(1)
        self.init, _ = self.req("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                "clientInfo": {"name": "budget-replay", "version": "1"}})
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.p.stdin.flush()

    def req(self, method: str, params: dict):
        i = next(self.ids)
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("server exited")
            m = json.loads(line)
            if m.get("id") == i and "method" not in m:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m["result"], len(line.encode("utf-8"))

    def call(self, tool: str, args: dict | None = None) -> dict:
        return self.req("tools/call", {"name": tool, "arguments": args or {}})[0]

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=60)
        except Exception:  # noqa: BLE001
            self.p.kill()
        if self._err is not subprocess.DEVNULL:
            self._err.close()


def text_of(result: dict) -> str:
    return "\n".join(c["text"] for c in result.get("content", []) if c.get("type") == "text")


def payload_of(result: dict) -> dict:
    """The JSON payload of a tool result, from the text block (works with or without structuredContent)."""
    txt = next((c["text"] for c in result.get("content", []) if c.get("type") == "text"), "")
    for line in txt.split("\n"):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    return {}


def run(script: list[dict], sb: dict | None = None, dump: Path | None = None, quiet: bool = False,
        env_extra: dict | None = None) -> dict:
    sb = sb or sandbox()
    env = dict(sb["env"], **(env_extra or {}))
    c = Client(env, stderr_path=sb["base"] / "server.err")
    try:
        tl, _ = c.req("tools/list", {})
        rep = {"instructions_bytes": len(c.init.get("instructions", "").encode()),
               "tools_list_bytes": len(json.dumps(tl, ensure_ascii=False).encode()),
               "tools": {t["name"]: len(json.dumps(t, ensure_ascii=False).encode()) for t in tl["tools"]},
               "calls": [], "sandbox": str(sb["base"]), "results": []}
        proj = str(sb["proj"]).replace("\\", "/")
        todo = list(script)
        n = -1
        while todo:
            step = todo.pop(0)
            n += 1
            tool = step["tool"]
            if tool not in rep["tools"] and step.get("alt") in rep["tools"]:
                tool = step["alt"]
            if tool not in rep["tools"]:
                rep["calls"].append({"n": n, "tool": tool, "stage": step.get("stage", ""), "skipped": "tool not registered",
                                     "arg_bytes": 0, "text_bytes": 0, "struct_bytes": 0, "images": [], "is_error": False,
                                     "secs": 0, "img_tokens": 0})
                rep["results"].append({})
                if not quiet:
                    print(f"{n:2d} {tool:18s} SKIPPED (not registered)")
                continue
            a = json.loads(json.dumps(step.get("args", {})).replace("$DIR", proj))
            t0 = time.time()
            r, wire = c.req("tools/call", {"name": tool, "arguments": a})
            row = {"n": n, "tool": tool, "stage": step.get("stage", ""), "secs": round(time.time() - t0, 1),
                   "arg_bytes": len(json.dumps(a, separators=(",", ":")).encode()), "text_bytes": 0, "struct_bytes": 0,
                   "images": [], "is_error": bool(r.get("isError")), "wire_bytes": wire}
            txt = ""
            for b in r.get("content", []):
                if b["type"] == "text":
                    row["text_bytes"] += len(b["text"].encode())
                    txt += b["text"] + "\n"
                elif b["type"] == "image":
                    raw = base64.b64decode(b["data"])
                    w, h = png_dims(raw)
                    row["images"].append({"w": w, "h": h, "png_bytes": len(raw), "est_tokens": img_tokens(w, h)})
                    if dump:
                        (dump / f"{n:02d}-{tool}-{len(row['images'])}.png").write_bytes(raw)
            if "structuredContent" in r:
                row["struct_bytes"] = len(json.dumps(r["structuredContent"], separators=(",", ":"), ensure_ascii=False).encode())
            if dump:
                (dump / f"{n:02d}-{tool}.txt").write_text(json.dumps(a) + "\n\n" + txt, encoding="utf-8")
            row["img_tokens"] = sum(i["est_tokens"] for i in row["images"])
            row["head"] = txt[:160]
            rep["calls"].append(row)
            rep["results"].append(r)
            if not quiet:
                print(f"{n:2d} {tool:18s} {'ERR' if row['is_error'] else 'ok ':3s} {row['secs']:6.1f}s "
                      f"args={row['arg_bytes']:5d}B text={row['text_bytes']:6d}B struct={row['struct_bytes']:6d}B "
                      f"imgs={[(i['w'], i['h'], i['est_tokens']) for i in row['images']]}  {txt[:110]!r}", flush=True)
            if row["is_error"] and not step.get("expect_error") and step.get("stop_on_error", True):
                if not quiet:
                    print(txt[:3000])
                break
            # a time-boxed tool that returned `pending` is called again, as its `next` says (at most 3 times)
            again = step.get("resume", 3 if tool == "board_build" else 0)
            if again and payload_of(r).get("pending"):
                todo.insert(0, {"tool": tool, "stage": step.get("stage", ""), "args": {}, "resume": again - 1})
    finally:
        c.close()

    def total(k):
        return sum(r.get(k, 0) for r in rep["calls"])
    rep["total"] = {"calls": len([r for r in rep["calls"] if not r.get("skipped")]), "arg_bytes": total("arg_bytes"),
                    "text_bytes": total("text_bytes"), "struct_bytes": total("struct_bytes"),
                    "images": sum(len(r["images"]) for r in rep["calls"]), "img_tokens": total("img_tokens"),
                    "text_tokens": total("text_bytes") // 4,
                    "errors": sum(1 for r in rep["calls"] if r["is_error"]), "seconds": round(total("secs"), 1)}
    stages: dict = {}
    for r in rep["calls"]:
        s = stages.setdefault(r["stage"], {"calls": 0, "text_bytes": 0, "images": 0, "img_tokens": 0})
        s["calls"] += 0 if r.get("skipped") else 1
        s["text_bytes"] += r["text_bytes"]
        s["images"] += len(r["images"])
        s["img_tokens"] += r["img_tokens"]
    rep["stages"] = stages
    if not quiet:
        for k, s in stages.items():
            print(f"  stage {k:14s} calls={s['calls']:2d} text={s['text_bytes']:6d}B images={s['images']} (~{s['img_tokens']} tok)")
        print("TOTAL", rep["total"], "tools/list", rep["tools_list_bytes"], "instructions", rep["instructions_bytes"])
    return rep


def load(name: str) -> list[dict]:
    p = Path(name)
    if not p.exists():
        p = HERE / (name if name.endswith(".json") else name + ".json")
    return json.loads(p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    sb_ = sandbox()
    d_ = sb_["base"] / "dump"
    d_.mkdir()
    extra_ = dict(kv.split("=", 1) for kv in sys.argv[3:])
    rep_ = run(load(sys.argv[1]), sb_, dump=d_, env_extra=extra_)
    rep_.pop("results")
    if len(sys.argv) > 2 and sys.argv[2] != "-":
        Path(sys.argv[2]).write_text(json.dumps(rep_, indent=1), encoding="utf-8")
    print("sandbox:", sb_["base"])
