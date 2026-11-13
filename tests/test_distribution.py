"""Distribution & onboarding tests: config merging, client registration,
CLI, doctor, skills, manifests, brand assets, installer script.

Runs with pytest or plainly:  python tests/test_distribution.py
Never touches real client configs: everything happens under a temp
COPPER_CONFIG_HOME_OVERRIDE / KICAD_CONFIG_HOME.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
# Copper's own state (active project, recent list) must never land in the real ~/.copper
os.environ.setdefault("COPPER_HOME", tempfile.mkdtemp(prefix="copper-dist-home-"))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from copper_kicad.install import clients, jsonc  # noqa: E402


class Sandbox:
    """Temp fake HOME for client configs + KiCad config dir."""

    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="copper-dist-test-"))
        self.home = self.dir / "home"
        self.home.mkdir()
        self._old = {k: os.environ.get(k) for k in ("COPPER_CONFIG_HOME_OVERRIDE", "COPPER_LAUNCHER", "KICAD_CONFIG_HOME")}
        os.environ["COPPER_CONFIG_HOME_OVERRIDE"] = str(self.home)
        os.environ.pop("COPPER_LAUNCHER", None)
        return self

    def __exit__(self, *a):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, rel, text, bom=False):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
        return p


# ── jsonc ─────────────────────────────────────────────────────────────────
JSONC_CASES = [
    "",
    "{}",
    '{\n  "mcpServers": {\n    "other": {"command": "x"}\n  },\n  "foo": 1\n}\n',
    '// zed\n{\n  "theme": "One Dark", // trailing comment\n  "context_servers": {\n    // empty\n  },\n}\n',
    '{"a":1}',
    '{\r\n\t"mcpServers": {\r\n\t\t"x": {}\r\n\t}\r\n}\r\n',
    '{\n  /* block */ "mcpServers": {"a": {"command": "b", "args": ["/*not a comment*/", "//nor this"]}}\n}',
]


def test_jsonc_set_get_remove_roundtrip():
    entry = {"command": "C:\\Program Files\\Copper\\copper.exe", "args": ["serve"], "env": {"K": "ü"}}
    for case in JSONC_CASES:
        key = ["context_servers" if "context_servers" in case else "mcpServers", "copper"]
        t1 = jsonc.set_value(case, key, entry)
        assert jsonc.get(t1, key) == entry, case
        before = jsonc.loads(case) if case.strip() else {}
        after = jsonc.loads(t1)
        # everything that was there is still there
        for k, v in before.items():
            if k != key[0]:
                assert after[k] == v
            else:
                for kk, vv in v.items():
                    assert after[k][kk] == vv
        t2 = jsonc.set_value(t1, key, {"command": "y"})
        assert jsonc.get(t2, key) == {"command": "y"}
        t3 = jsonc.remove(t1, key)
        assert jsonc.get(t3, key) is None
        assert jsonc.loads(t3).get(key[0], {}) == before.get(key[0], {}), case


def test_jsonc_append_then_remove_is_byte_identical():
    for case in [JSONC_CASES[2], JSONC_CASES[5], JSONC_CASES[6], JSONC_CASES[4]]:
        key = ["mcpServers", "copper"]
        if jsonc.get(case, ["mcpServers"]) is None:
            continue
        t = jsonc.set_value(case, key, {"command": "c", "args": []})
        assert jsonc.remove(t, key) == case, repr(case)


def test_jsonc_preserves_comments_and_strings():
    src = JSONC_CASES[3]
    t = jsonc.set_value(src, ["context_servers", "copper"], {"command": "c"})
    assert "// zed" in t and "// trailing comment" in t
    s = JSONC_CASES[6]
    assert jsonc.get(s, ["mcpServers", "a", "args"]) == ["/*not a comment*/", "//nor this"]


def test_jsonc_rejects_garbage():
    for bad in ['{"a": }', '{"a" 1}', "[1,2", '{"a": "unterminated}']:
        try:
            jsonc.parse(bad)
        except jsonc.JsoncError:
            continue
        raise AssertionError(f"accepted invalid JSON: {bad!r}")


# ── TOML (Codex) ──────────────────────────────────────────────────────────
def test_toml_block_and_remove():
    spec = clients.ServerSpec("C:\\Copper\\copper.exe", ["serve"], {"PYTHONPATH": "C:\\src", "A B": "x\"y"})
    block = clients.toml_block("mcp_servers.copper", spec, {"startup_timeout_sec": 60})
    text = 'model = "o3"\n\n[mcp_servers.other]\ncommand = "o"\n\n' + block + "\n[profiles.x]\nmodel = \"y\"\n"
    got = clients.toml_get(text, "mcp_servers.copper")
    assert got["command"] == spec.command and got["args"] == ["serve"] and got["startup_timeout_sec"] == 60
    assert got["env"]["PYTHONPATH"] == "C:\\src"
    removed = clients.toml_remove(text, "mcp_servers.copper")
    assert "copper" not in removed
    assert "[mcp_servers.other]" in removed and "[profiles.x]" in removed and 'model = "o3"' in removed
    # quoted table names are recognised too
    assert clients.toml_get('[mcp_servers."copper"]\ncommand = "z"\n', "mcp_servers.copper") == {"command": "z"}


# ── client registration ───────────────────────────────────────────────────
def _claude_cfg() -> Path:
    """Claude Desktop's primary config for THIS platform inside the active sandbox (call inside a Sandbox):
    %APPDATA%/Claude on Windows, ~/Library/Application Support/Claude on macOS, $XDG_CONFIG_HOME/Claude on Linux."""
    return clients._claude_desktop_dirs()[0] / "claude_desktop_config.json"


def _seed_clients(sb: Sandbox):
    claude = _claude_cfg()
    claude.parent.mkdir(parents=True)
    orig_claude = '{\n  "mcpServers": {\n    "fs": {"command": "npx", "args": ["-y", "fs"]}\n  },\n  "globalShortcut": ""\n}\n'
    claude.write_text(orig_claude, encoding="utf-8")
    orig_codex = 'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "foo"\n'
    sb.write(".codex/config.toml", orig_codex)
    (sb.home / ".cursor").mkdir()
    (sb.home / ".gemini").mkdir()
    zed = clients._zed_settings()
    zed.parent.mkdir(parents=True, exist_ok=True)
    zed.write_text('// Zed settings\n{\n  "theme": "One Dark",\n}\n', encoding="utf-8")
    return {"claude": (claude, orig_claude), "codex": (sb.home / ".codex/config.toml", orig_codex), "zed": (zed, zed.read_text())}


def test_detection_is_sandboxed():
    with Sandbox() as sb:
        assert not any(c["detected"] for c in clients.detect()), "empty fake HOME must detect nothing"
        _seed_clients(sb)
        det = {c["id"] for c in clients.detect() if c["detected"]}
        assert {"claude-desktop", "codex", "cursor", "gemini", "zed"} <= det, det
        assert "vscode" not in det and "windsurf" not in det


def test_install_dry_run_writes_nothing():
    with Sandbox() as sb:
        seeds = _seed_clients(sb)
        before = {p: p.read_bytes() for p in sb.home.rglob("*") if p.is_file()}
        res = clients.install("all", dry_run=True, launcher=str(sb.dir / "copper.exe"))
        after = {p: p.read_bytes() for p in sb.home.rglob("*") if p.is_file()}
        assert before == after
        assert res["diffs"] and all(c["action"] == "added" for c in res["changes"]), res["changes"]
        assert any("copper" in d for d in res["diffs"])
        assert seeds


def test_install_idempotent_backup_uninstall():
    with Sandbox() as sb:
        seeds = _seed_clients(sb)
        exe = sb.dir / "Copper" / "copper.exe"
        exe.parent.mkdir()
        exe.write_bytes(b"MZ")
        res = clients.install("all", launcher=str(exe))
        acts = {(c["client"], c["action"]) for c in res["changes"]}
        assert all(a == "added" for _, a in acts), acts
        # each client config now has a correct entry
        claude, orig = seeds["claude"]
        cfg = json.loads(claude.read_text(encoding="utf-8"))
        assert cfg["mcpServers"]["copper"] == {"command": str(exe.resolve()), "args": ["serve"]}
        assert cfg["mcpServers"]["fs"] == {"command": "npx", "args": ["-y", "fs"]}
        assert cfg["globalShortcut"] == ""
        assert Path(str(claude) + clients.BACKUP_SUFFIX).read_text(encoding="utf-8") == orig
        codex = clients.toml_get(seeds["codex"][0].read_text(), "mcp_servers.copper")
        assert codex["command"] == str(exe.resolve()) and codex["tool_timeout_sec"] == 600 and codex["startup_timeout_sec"] == 60
        zed = jsonc.loads(seeds["zed"][0].read_text())
        assert zed["context_servers"]["copper"]["source"] == "custom"
        assert seeds["zed"][0].read_text().startswith("// Zed settings")
        gem = json.loads((sb.home / ".gemini/settings.json").read_text())
        assert gem["mcpServers"]["copper"]["timeout"] >= 60000
        # doctor/status view
        ents = [e for e in clients.entries() if e["status"] != "absent"]
        assert {e["client"] for e in ents} >= {"claude-desktop", "codex", "cursor", "zed", "gemini"}
        assert all(e["status"] == "ok" for e in ents), ents
        # idempotent
        res2 = clients.install("all", launcher=str(exe))
        assert all(c["action"] == "unchanged" for c in res2["changes"]), res2["changes"]
        # launcher moved → updated, still exactly one entry
        exe2 = sb.dir / "Copper2" / "copper.exe"
        exe2.parent.mkdir()
        exe2.write_bytes(b"MZ")
        res3 = clients.install(["codex", "claude-desktop"], launcher=str(exe2))
        assert all(c["action"] == "updated" for c in res3["changes"]), res3["changes"]
        assert seeds["codex"][0].read_text().count("[mcp_servers.copper]") == 1
        # backup is the pre-Copper original, not overwritten by later installs
        assert Path(str(claude) + clients.BACKUP_SUFFIX).read_text(encoding="utf-8") == orig
        # uninstall restores the user's content exactly (Claude Desktop + Codex)
        un = clients.uninstall("all")
        assert {c["client"] for c in un["changes"]} >= {"claude-desktop", "codex", "zed"}
        assert claude.read_text(encoding="utf-8") == orig
        assert seeds["codex"][0].read_text().strip() == seeds["codex"][1].strip()
        assert "copper" not in seeds["zed"][0].read_text()
        assert not [e for e in clients.entries() if e["status"] != "absent"]


def test_restore_backups():
    with Sandbox() as sb:
        seeds = _seed_clients(sb)
        clients.install(["claude-desktop"], launcher=sys.executable)
        claude, orig = seeds["claude"]
        cfg = json.loads(claude.read_text())
        cfg["mcpServers"]["added_later"] = {"command": "z"}
        claude.write_text(json.dumps(cfg))
        clients.uninstall(["claude-desktop"], restore_backups=True)
        assert claude.read_text(encoding="utf-8") == orig
        assert not Path(str(claude) + clients.BACKUP_SUFFIX).exists()


def test_explicit_client_creates_config_and_bom_preserved():
    with Sandbox() as sb:
        p = sb.write(".cursor/mcp.json", '{"mcpServers": {}}', bom=True)
        clients.install(["cursor", "windsurf"], launcher=sys.executable)
        raw = p.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert "copper" in jsonc.loads(raw[3:].decode())["mcpServers"]
        ws = sb.home / ".codeium/windsurf/mcp_config.json"
        assert json.loads(ws.read_text())["mcpServers"]["copper"]["args"][-1] == "serve"


def test_invalid_config_is_not_clobbered():
    with Sandbox() as sb:
        p = sb.write(".cursor/mcp.json", '{"mcpServers": {"x": 1,,}}')
        res = clients.install(["cursor"], launcher=sys.executable)
        assert res["changes"][0]["action"] == "error"
        assert p.read_text() == '{"mcpServers": {"x": 1,,}}'


def test_unknown_client_name():
    try:
        clients.resolve_names("claude-desktop,notaclient")
    except ValueError as e:
        assert "notaclient" in str(e)
    else:
        raise AssertionError("unknown client accepted")
    assert [c.id for c in clients.resolve_names("claude,code")] == ["claude-desktop", "vscode"]


def test_server_spec_variants():
    old = os.environ.pop("COPPER_LAUNCHER", None)
    try:
        spec = clients.server_spec()
        assert Path(spec.command).is_absolute()
        assert "serve" in spec.args[-1]
        # the registered command really starts the server (answers ping) from any cwd
        r = subprocess.run(spec.argv(), input=json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n",
                           capture_output=True, text=True, timeout=60, cwd=tempfile.gettempdir(),
                           env={**os.environ, **spec.env})
        assert '"id": 7' in r.stdout, r.stdout + r.stderr
        os.environ["COPPER_LAUNCHER"] = sys.executable
        assert clients.server_spec().args == ["-m", "copper_kicad", "serve"]
    finally:
        os.environ.pop("COPPER_LAUNCHER", None)
        if old:
            os.environ["COPPER_LAUNCHER"] = old


def test_deeplinks():
    import base64
    from urllib.parse import parse_qs, urlparse
    link = clients.cursor_deeplink()
    q = parse_qs(urlparse(link).query)
    assert q["name"] == ["copper"]
    assert json.loads(base64.b64decode(q["config"][0]))["args"][-1] == "serve"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"cursor\.com/install-mcp\?name=copper&config=([A-Za-z0-9%=+/]+)", readme)
    assert m, "README Cursor badge missing"
    from urllib.parse import unquote
    assert json.loads(base64.b64decode(unquote(m.group(1))))["command"] == "npx"     # the npm launcher; no PyPI package


# ── KiCad setup ───────────────────────────────────────────────────────────
def test_kicad_api_toggle_in_sandbox():
    from copper_kicad.install import kicad_setup
    from copper_kicad.kicad import env
    with Sandbox() as sb:
        os.environ["KICAD_CONFIG_HOME"] = str(sb.dir / "kicad" / "10.0")
        env.refresh()
        cfg = env.config_dir()     # wherever env.py resolves KICAD_CONFIG_HOME to
        assert str(cfg).startswith(str(sb.dir)), cfg
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "kicad_common.json").write_text(json.dumps({"api": {"enable_server": False}, "keep": 1}), encoding="utf-8")
        try:
            r = kicad_setup.set_api(True, force=True)
            assert r["enabled"] and r["changed"], r
            d = json.loads((cfg / "kicad_common.json").read_text())
            assert d["api"]["enable_server"] is True and d["keep"] == 1
            assert (cfg / "kicad_common.json.copper-backup").exists()
            assert kicad_setup.set_api(True, force=True)["changed"] is False
            # lib tables get created from KiCad's templates when KiCad is installed
            try:
                env.kicad()
                has_kicad = True
            except Exception:  # noqa: BLE001
                has_kicad = False
            if has_kicad:
                res = kicad_setup.ensure_lib_tables()
                st = kicad_setup.lib_tables_status()
                assert st["sym"]["exists"] and st["sym"]["libs"] > 50, res
                assert st["fp"]["exists"] and st["fp"]["libs"] > 50, res
        finally:
            os.environ.pop("KICAD_CONFIG_HOME", None)
            env.refresh()


# ── doctor + CLI ──────────────────────────────────────────────────────────
def test_doctor_shape():
    from copper_kicad.install import doctor
    with Sandbox():
        checks = doctor.run(fast=True, network=False)
        ids = {c.id for c in checks}
        assert {"python", "pillow", "kicad", "tools", "lib_tables"} <= ids, ids
        assert all(c.status in ("ok", "warn", "fail", "skip") for c in checks)
        assert all(c.fix for c in checks if c.status == "fail"), [c for c in checks if c.status == "fail" and not c.fix]
        s = doctor.summarize(checks)
        json.dumps(s)
        assert s["counts"]["ok"] >= 3
        b = doctor.brief()
        assert "healthy" in b and isinstance(b["problems"], list)


def _cli(*args, input=None, env_extra=None, timeout=120):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["NO_COLOR"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "copper_kicad", *args], input=input, capture_output=True,
                          text=True, encoding="utf-8", timeout=timeout, env=env, cwd=str(SRC))


def test_cli_version_config_install_json():
    with Sandbox() as sb:
        r = _cli("version")
        assert r.returncode == 0 and re.match(r"copper \d+\.\d+\.\d+", r.stdout), r.stdout + r.stderr
        r = _cli("config", "--client", "codex")
        assert "[mcp_servers.copper]" in r.stdout and "tool_timeout_sec = 600" in r.stdout
        r = _cli("config")
        assert "copper" in json.loads(r.stdout)["mcpServers"]
        r = _cli("install", "--client", "cursor", "--dry-run", "--json")
        assert r.returncode == 0, r.stderr
        d = json.loads(r.stdout)
        assert d["dry_run"] and d["changes"][0]["action"] == "added"
        assert not (sb.home / ".cursor" / "mcp.json").exists()
        r = _cli("install", "--client", "cursor")
        assert r.returncode == 0 and (sb.home / ".cursor" / "mcp.json").exists(), r.stdout + r.stderr
        r = _cli("status", "--json")
        st = json.loads(r.stdout)
        # an interpreter inside uv's cache (`uv run --with …`) is correctly flagged as throw-away
        ok = ("ok", "warn") if clients._ephemeral(sys.executable) else ("ok",)
        assert any(c["client"] == "cursor" and c["status"] in ok for c in st["clients"]), st
        r = _cli("uninstall", "--client", "cursor")
        assert r.returncode == 0
        cur = sb.home / ".cursor" / "mcp.json"
        assert not cur.exists() or "copper" not in cur.read_text()   # Copper created it, so it may be deleted
        r = _cli("install", "--client", "bogus")
        assert r.returncode == 2 and "bogus" in r.stdout


def test_cli_doctor_json():
    with Sandbox():
        r = _cli("doctor", "--json", "--fast", "--offline")
        d = json.loads(r.stdout)
        assert d["version"] and isinstance(d["checks"], list) and d["checks"]
        assert r.returncode == (0 if d["healthy"] else 1)


def test_bare_copper_with_pipe_serves_mcp():
    with Sandbox():
        msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
        r = _cli(input=msg, timeout=60)
        line = next(ln for ln in r.stdout.splitlines() if ln.strip())
        assert json.loads(line) == {"jsonrpc": "2.0", "id": 1, "result": {}}, r.stdout + r.stderr


# ── skills, plugin, manifests ─────────────────────────────────────────────
def test_skills_frontmatter():
    from copper_kicad import skills
    sk = {s["name"]: s for s in skills.list_skills()}
    assert set(sk) >= {"kicad-design", "parts-sourcing", "design-review", "fab-release"}, set(sk)
    for name, s in sk.items():
        assert Path(s["path"]).parent.name == name
        assert 60 <= len(s["description"]) <= 1024, name
        assert re.fullmatch(r"[a-z0-9-]{1,64}", name)
        assert len(s["body"]) > 1500, f"{name} is too thin"
    body = sk["kicad-design"]["body"]
    for must in ("5.1", "100 nF", "sch_apply", "verify", "LCSC"):
        assert must in body, must
    plug = ROOT / "plugin" / "skills"
    if plug.exists():
        for name, s in sk.items():
            assert (plug / name / "SKILL.md").read_text(encoding="utf-8") == Path(s["path"]).read_text(encoding="utf-8"), \
                f"plugin/skills/{name} out of sync — run scripts/build_release.py"


def test_manifests():
    from copper_kicad import __version__
    man = json.loads((ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    for k in ("manifest_version", "name", "version", "description", "author", "server"):
        assert k in man, k
    assert man["manifest_version"] == "0.4" and man["server"]["type"] == "uv"
    assert "mcp_config" in man["server"] and "${__dirname}" in man["server"]["mcp_config"]["args"]
    assert (ROOT / "mcpb" / man["icon"]).exists()
    srv = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert srv["name"] == "io.github.claynicholson/copper" and len(srv["description"]) <= 100
    assert {p["registryType"] for p in srv["packages"]} == {"npm", "mcpb"}
    npm = json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))
    assert npm["name"] == "copper-kicad" and npm["version"] == __version__ and npm["mcpName"] == srv["name"], npm
    pk = next(p for p in srv["packages"] if p["registryType"] == "npm")
    assert pk["identifier"] == npm["name"] and pk["version"] == __version__ == srv["version"], pk
    assert "copper-kicad-mcp" in srv["repository"]["url"]
    assert "mcp-name: io.github.claynicholson/copper" in (ROOT / "README.md").read_text(encoding="utf-8")
    mk = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    src = mk["plugins"][0]["source"]
    assert (ROOT / src / ".claude-plugin" / "plugin.json").exists()
    pj = json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert pj["name"] == "copper" and pj["version"] == __version__ and mk["plugins"][0]["version"] == __version__
    mcp = json.loads((ROOT / "plugin" / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["copper"]["args"][-1] == "serve"
    assert mcp["mcpServers"]["copper"]["command"] == "npx" and f"copper-kicad@{__version__}" in mcp["mcpServers"]["copper"]["args"]
    cx = json.loads((ROOT / "plugin" / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert len(cx["interface"]["defaultPrompt"]) <= 3


def test_brand_assets():
    from PIL import Image
    a = ROOT / "installer" / "windows" / "assets"
    with Image.open(a / "welcome.bmp") as im:
        assert im.size == (164, 314) and im.mode == "RGB"
    with Image.open(a / "header.bmp") as im:
        assert im.size == (150, 57)
    with Image.open(a / "copper.ico") as im:
        sizes = im.info.get("sizes") or set()
        assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= set(sizes), sizes
    with Image.open(ROOT / "mcpb" / "icon.png") as im:
        assert im.size == (512, 512)


def test_nsis_script_flow():
    nsi = (ROOT / "installer" / "windows" / "copper.nsi").read_text(encoding="utf-8")
    pages = re.findall(r"!insertmacro MUI_PAGE_(\w+)", nsi)
    assert pages == ["WELCOME", "LICENSE", "COMPONENTS", "DIRECTORY", "INSTFILES", "FINISH"], pages
    assert "RequestExecutionLevel user" in nsi and '$LOCALAPPDATA\\Programs\\Copper' in nsi
    for client in ("claude-desktop", "claude-code", "codex", "cursor", "vscode", "windsurf", "zed", "gemini",
                   "copilot-cli"):
        assert f'AddClient "{client}"' in nsi, client
        assert client in clients.BY_ID
    assert "copper.exe\" uninstall" in nsi and "Uninstall.exe" in nsi
    assert "COPPER_CONFIG_HOME_OVERRIDE" in nsi


def test_one_line_installers():
    ps = (ROOT / "installer" / "install.ps1").read_bytes()
    assert all(b < 128 for b in ps), "install.ps1 must be ASCII (PowerShell 5.1 reads BOM-less files as ANSI)"
    assert b"setup" in ps and b"tool install" in ps
    sh = (ROOT / "installer" / "install.sh").read_text(encoding="utf-8")
    assert sh.startswith("#!/bin/sh") and "set -eu" in sh and "uv tool install" in sh
    bash = shutil.which("bash")
    if bash:
        r = subprocess.run([bash, "-n", str(ROOT / "installer" / "install.sh")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


# ── regressions found by the skeptical-tester pass ───────────────────────
LAUNCHER = r"C:\Program Files\Copper\copper.exe"


def test_utf16_config_does_not_crash_doctor_or_status():
    # PowerShell 5.1 `Out-File`/`Set-Content -Encoding Unicode` writes UTF-16.
    from copper_kicad.install import doctor
    with Sandbox() as sb:
        p = _claude_cfg()
        p.parent.mkdir(parents=True)
        p.write_text('{"mcpServers": {}}', encoding="utf-16")
        before = p.read_bytes()
        res = clients.install("claude-desktop", launcher=LAUNCHER)
        ch = res["changes"][0]
        assert ch["action"] == "error" and "UTF-16" in ch["detail"], ch
        assert p.read_bytes() == before
        ent = [e for e in clients.entries() if e["client"] == "claude-desktop"]
        assert ent and ent[0]["status"] == "fail" and "UTF-16" in ent[0]["detail"]
        chk = [c for c in doctor.check_clients() if c.id == "client_claude-desktop"][0]
        assert chk.status == "fail" and "repair" in chk.fix, chk
        r = _cli("status", "--json")
        assert r.returncode == 0, r.stderr


def test_non_utf8_bytes_survive_round_trip():
    # A cp1252 "é" typed into the file in Notepad must come back byte-identical.
    with Sandbox() as sb:
        p = sb.home / ".cursor" / "mcp.json"
        p.parent.mkdir(parents=True)
        orig = '{\n  "note": "caf\xe9",\n  "mcpServers": {}\n}\n'.encode("cp1252")
        p.write_bytes(orig)
        assert clients.install("cursor", launcher=LAUNCHER)["changes"][0]["action"] == "added"
        assert b"caf\xe9" in p.read_bytes()
        clients.uninstall("cursor")
        assert p.read_bytes() == orig


def test_duplicate_copper_keys_are_collapsed():
    # JSON parsers keep the LAST duplicate: editing only the first one was a silent no-op.
    with Sandbox() as sb:
        p = sb.write(_claude_cfg(), '{"mcpServers": {"copper": {"command": "old"}, "x": {"command": "y"}, '
                                 '"copper": {"command": "old2"}}}')
        res = clients.install("claude-desktop", launcher=LAUNCHER)
        assert res["changes"][0]["action"] == "updated"
        d = json.loads(p.read_text(encoding="utf-8"))
        assert d["mcpServers"]["copper"]["command"] == LAUNCHER
        assert d["mcpServers"]["x"] == {"command": "y"}
        assert p.read_text(encoding="utf-8").count('"copper"') == 1
        assert clients.install("claude-desktop", launcher=LAUNCHER)["changes"][0]["action"] == "unchanged"
        sb.write(_claude_cfg(), '{"mcpServers": {"copper": {"command": "a"}, "copper": {"command": "b"}}}')
        clients.uninstall("claude-desktop")
        assert "copper" not in json.loads(p.read_text(encoding="utf-8"))["mcpServers"]


def test_readonly_config_reports_and_leaves_no_temp_file():
    with Sandbox() as sb:
        p = sb.write(".cursor/mcp.json", "{}")
        os.chmod(p, 0o444)
        try:
            ch = clients.install("cursor", launcher=LAUNCHER)["changes"][0]
        finally:
            os.chmod(p, 0o666)
        # refused on every OS: a POSIX rename ignores the target's mode, so clients._write checks it itself
        assert ch["action"] == "error" and "read-only" in ch["detail"], ch
        assert not list(p.parent.glob("*.copper-tmp"))


def test_claude_cli_style_entry_is_idempotent():
    # `claude mcp add-json` stores "env": {} — a second install must not rewrite it.
    with Sandbox() as sb:
        entry = {"type": "stdio", "command": LAUNCHER, "args": ["serve"], "env": {}}
        p = sb.write(".claude.json", json.dumps({"mcpServers": {"copper": entry}, "numStartups": 3}))
        before = p.read_bytes()
        ch = clients.install("claude-code", launcher=LAUNCHER)["changes"][0]
        assert ch["action"] == "unchanged", ch
        assert p.read_bytes() == before


def test_codex_keeps_user_keys_and_is_idempotent():
    tomllib = __import__("tomllib") if sys.version_info >= (3, 11) else None
    with Sandbox() as sb:
        p = sb.write(".codex/config.toml",
                     'model = "o3"\n\n[mcp_servers.copper]\ncommand = "copper"\nargs = ["serve"]\nenabled = true\n'
                     'env_vars = [\n  "HTTPS_PROXY",\n]\n\n[mcp_servers.copper.env]\nHTTPS_PROXY = "http://p:8080"\n\n'
                     '[mcp_servers.other]\ncommand = "x"\n')
        assert clients.install("codex", launcher=LAUNCHER)["changes"][0]["action"] == "updated"
        text = p.read_text(encoding="utf-8")
        if tomllib:
            d = tomllib.loads(text)
            cu = d["mcp_servers"]["copper"]
            assert cu["command"] == LAUNCHER and cu["args"] == ["serve"] and cu["tool_timeout_sec"] == 600
            assert cu["enabled"] is True and cu["env_vars"] == ["HTTPS_PROXY"]
            assert cu["env"] == {"HTTPS_PROXY": "http://p:8080"}
            assert d["mcp_servers"]["other"] == {"command": "x"} and d["model"] == "o3"
        assert clients.install("codex", launcher=LAUNCHER)["changes"][0]["action"] == "unchanged"
        clients.uninstall("codex")
        text = p.read_text(encoding="utf-8")
        assert "copper" not in text and "[mcp_servers.other]" in text and 'model = "o3"' in text


def test_codex_inline_and_dotted_definitions_are_replaced():
    # Appending [mcp_servers.copper] next to `copper = {…}` made config.toml unparseable for Codex.
    tomllib = __import__("tomllib") if sys.version_info >= (3, 11) else None
    for src in ('[mcp_servers]\ncopper = { command = "old", args = ["serve"] }\nother = { command = "x" }\n',
                'mcp_servers.copper.command = "old"\nmcp_servers.copper.args = ["serve"]\nmodel = "o3"\n'):
        with Sandbox() as sb:
            p = sb.write(".codex/config.toml", src)
            ent = [e for e in clients.entries() if e["client"] == "codex"][0]
            assert ent["status"] == "fail", ent
            ch = clients.install("codex", launcher=LAUNCHER)["changes"][0]
            assert ch["action"] == "updated", ch
            text = p.read_text(encoding="utf-8")
            assert text.count("[mcp_servers.copper]") == 1 and "copper =" not in text, text
            assert "copper.command" not in text and "copper.args" not in text, text
            if tomllib:
                d = tomllib.loads(text)
                assert d["mcp_servers"]["copper"]["command"] == LAUNCHER
            assert clients.install("codex", launcher=LAUNCHER)["changes"][0]["action"] == "unchanged"


def test_invalid_toml_is_not_clobbered():
    if sys.version_info < (3, 11):
        return
    with Sandbox() as sb:
        p = sb.write(".codex/config.toml", 'model = "o3\n[mcp_servers\n')
        before = p.read_bytes()
        ch = clients.install("codex", launcher=LAUNCHER)["changes"][0]
        assert ch["action"] == "error" and "TOML" in ch["detail"], ch
        assert p.read_bytes() == before


def test_symlinked_config_is_written_through():
    with Sandbox() as sb:
        real = sb.write("dotfiles/mcp.json", '{"mcpServers": {}}')
        link = sb.home / ".cursor" / "mcp.json"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            return                       # Windows without Developer Mode
        clients.install("cursor", launcher=LAUNCHER)
        assert link.is_symlink(), "the symlink was replaced by a regular file"
        assert "copper" in json.loads(real.read_text(encoding="utf-8"))["mcpServers"]


def test_claude_config_dir_is_honoured():
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    try:
        os.environ["CLAUDE_CONFIG_DIR"] = r"C:\cfg\claude" if os.name == "nt" else "/cfg/claude"
        with Sandbox():                  # sandbox mode ignores it, never touches the real dir
            assert clients._claude_code_json().parent != Path(os.environ["CLAUDE_CONFIG_DIR"])
        saved = os.environ.pop("COPPER_CONFIG_HOME_OVERRIDE", None)
        try:
            assert clients._claude_code_json() == Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
        finally:
            if saved is not None:
                os.environ["COPPER_CONFIG_HOME_OVERRIDE"] = saved
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


def test_uvx_cache_launch_is_not_registered():
    # `uvx copper-kicad setup` runs from uv's cache; that path vanishes on `uv cache clean`.
    assert clients._ephemeral(r"C:\Users\a\AppData\Local\uv\cache\archive-v0\Xy12\Lib\site-packages\copper_kicad")
    assert clients._ephemeral("/home/a/.cache/uv/archive-v0/abc/lib/python3.12/site-packages/copper_kicad")
    assert not clients._ephemeral(r"C:\Users\a\AppData\Roaming\uv\tools\copper-kicad\Lib\site-packages\copper_kicad")
    assert not clients._ephemeral(r"C:\Users\a\AppData\Local\Programs\Copper\tools\copper-kicad\Lib\site-packages")
    st, detail = clients.inspect_entry({"command": sys.executable, "args": ["-c", "import sys; sys.path.insert(0, "
                                        "'C:\\\\nowhere\\\\gone\\\\src'); from copper_kicad.cli import main"]})
    assert st == "fail" and "moved" in detail, (st, detail)


def test_install_hints_only_for_changed_clients():
    with Sandbox() as sb:
        (sb.home / ".cursor").mkdir()
        _cli("install", "--client", "cursor")
        r = _cli("install", "--client", "cursor")
        assert "picks it up automatically" not in r.stdout and "nothing to restart" in r.stdout, r.stdout


def test_cli_config_claude_code_prints_command():
    r = _cli("config", "--client", "claude-code")
    assert r.returncode == 0 and "claude mcp add-json copper -s user" in r.stdout, r.stdout


def test_setup_does_not_preempt_kicad_settings_import():
    # Creating <config>/10.0 before KiCad 10's first start silently skips its
    # "Import settings from KiCad 9" step (custom libraries lost).
    from copper_kicad.install import kicad_setup
    from copper_kicad.kicad import env
    with Sandbox() as sb:
        root = sb.dir / "kicad"
        (root / "9.0").mkdir(parents=True)
        (root / "9.0" / "kicad_common.json").write_text("{}", encoding="utf-8")
        os.environ["KICAD_CONFIG_HOME"] = str(root / "10.0")
        env.refresh()
        try:
            cfg = env.config_dir()
            if cfg.name != "10.0" or cfg.parent != root:
                return                   # env.py resolves KICAD_CONFIG_HOME differently; nothing to check
            assert kicad_setup.pending_migration() == root / "9.0"
            r = kicad_setup.set_api(True)
            assert r.get("refused") and "Import settings" in r["refused"]
            r = kicad_setup.ensure_lib_tables()
            assert r.get("error") and not r["created"]
            assert not cfg.exists(), "Copper created the new config dir before KiCad's first start"
            cfg.mkdir()
            assert kicad_setup.pending_migration() is None
        finally:
            env.refresh()


def test_installers_survive_running_server_and_stderr():
    nsi = (ROOT / "installer" / "windows" / "copper.nsi").read_text(encoding="utf-8")
    core = nsi[nsi.index('Section "Copper core'):nsi.index("SectionEnd", nsi.index('Section "Copper core'))]
    assert core.index("Call StopRunningCopper") < core.index("tool install"), "stop the running server before uv"
    assert '\\bin\\copper.exe" version' in core, "verify the environment runs, not just that copper.exe exists"
    un = nsi[nsi.index('Section "Uninstall"'):]
    assert "Call un.StopRunningCopper" in un
    # Setup is 32-bit: WOW64 PowerShell cannot see 64-bit process paths
    assert "Sysnative\\WindowsPowerShell" in nsi
    # test mode (COPPER_CONFIG_HOME_OVERRIDE) must not edit the real KiCad config either
    assert '${If} $NoKiCadCfg != "1"' in core and "setup --yes" in core
    api = nsi[nsi.index('Section "Enable KiCad scripting API"'):]
    assert api.index('$NoKiCadCfg == "1"') < api.index("kicad-api enable")
    assert 'ReadEnvStr $R3 "KICAD_CONFIG_HOME"' in nsi
    ps = (ROOT / "installer" / "install.ps1").read_text(encoding="ascii")
    # PS 5.1 + ErrorActionPreference=Stop + a redirected native stderr line = terminating error
    for ln in ps.splitlines():
        if re.search(r"&\s*\$uv\b.*(2>|\*>)", ln):
            pre = ps[:ps.index(ln)]
            assert pre.rfind("$ErrorActionPreference = 'Continue'") > pre.rfind("$ErrorActionPreference = 'Stop'"), ln
    assert "Stop-Process" in ps and "version" in ps
    sh = (ROOT / "installer" / "install.sh").read_text(encoding="utf-8")
    assert "setup --yes < /dev/tty" not in sh and "(exec </dev/tty)" in sh
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_release
    assert 'if not exist "%HERE%uv.exe"' in build_release.PORTABLE_INSTALL
    assert "Stop-Process" in build_release.PORTABLE_INSTALL and 'copper.exe" version' in build_release.PORTABLE_INSTALL


# ── regressions from the fresh-user / dogfood pass ───────────────────────
def _env_without(*names, **extra):
    env = {k: v for k, v in os.environ.items() if k not in names}
    env.update(extra)
    return env


def _cli_env(env, *args, input=None, timeout=120):
    env = dict(env)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["NO_COLOR"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, "-m", "copper_kicad", *args], input=input, capture_output=True,
                          text=True, encoding="utf-8", timeout=timeout, env=env, cwd=str(SRC))


BAD_ESCAPE = '{"mcpServers":{"filesystem":{"command":"npx","args":["C:\\Users\\me"]}}}'


def test_invalid_escape_config_is_reported_not_a_traceback():
    # a Windows path typed with single backslashes: json raises JSONDecodeError, not JsoncError
    try:
        jsonc.parse('{\n  "a": "C:\\Users\\me"\n}')
        raise AssertionError("invalid escape accepted")
    except jsonc.JsoncError as e:
        msg = str(e)
        assert "line 2, column 11" in msg and "doubled" in msg, msg
    with Sandbox() as sb:
        p = sb.write(_claude_cfg(), BAD_ESCAPE)
        es = [e for e in clients.entries() if e["client"] == "claude-desktop"]
        assert es and es[0]["status"] == "fail" and "not valid JSON" in es[0]["detail"], es
        for args in (("doctor", "--fast", "--offline"), ("status",), ("doctor", "--json", "--fast", "--offline")):
            r = _cli(*args)
            assert "Traceback" not in r.stderr and "Traceback" not in r.stdout, (args, r.stderr[-2000:])
            assert "not valid JSON" in r.stdout and "line 1, column" in r.stdout or args == ("status",), r.stdout
        r = _cli("install", "--client", "claude-desktop", "--json")
        d = json.loads(r.stdout)
        assert d["changes"][0]["action"] == "error" and "not valid JSON" in d["changes"][0]["detail"], d
        assert "line 1, column 57" in d["changes"][0]["detail"], d   # the file position, not the token's
        assert p.read_text(encoding="utf-8") == BAD_ESCAPE, "an unparseable file must never be rewritten"


def test_doctor_network_blocked_is_not_green():
    from copper_kicad.install import doctor
    orig = doctor._http_code
    try:
        doctor._http_code = lambda name, url, timeout: (403, "", 5)
        c = doctor._probe("jlcpcb", "https://x", "stock", 1)
        assert c.status == "warn" and "blocked" in c.detail and c.fix, c
        c = doctor._probe_easyeda(1)
        assert c.status == "warn" and "mirror" in c.detail, c
        doctor._http_code = lambda name, url, timeout: ((403, "", 5) if name == "easyeda" else (200, "", 7))
        c = doctor._probe_easyeda(1)
        assert c.status == "ok" and "via mirror" in c.detail and "403" in c.detail, c
        doctor._http_code = lambda name, url, timeout: (None, "timed out", 4000)
        assert doctor._probe("jlcsearch", "https://x", "search", 1).status == "warn"
    finally:
        doctor._http_code = orig
    urls = dict(doctor.EASYEDA_SOURCES)
    from copper_kicad.parts import easyeda
    assert urls["easyeda"] == easyeda.EE_URL.format(lcsc="C25804")
    assert urls["mirror"] == easyeda.EE_MIRROR.format(lcsc="C25804")


def test_doctor_says_autorouting_is_missing():
    import io
    from copper_kicad import cli
    from copper_kicad.install import doctor, freerouting
    orig = freerouting.find
    try:
        freerouting.find = lambda: {"ok": False, "problem": "Freerouting 2.2.3 is not installed",
                                    "fix": freerouting._fix("/x/tools"), "tools_dir": "/x/tools"}
        c = doctor.check_freerouting()
    finally:
        freerouting.find = orig
    assert c.status == "warn" and c.group == "pcb" and "/x/tools" in c.fix and "COPPER_HOME" in c.fix, c
    buf = io.StringIO()
    cli._print_doctor(cli.UI(buf, color=False), [doctor.Check("python", "Python", "ok"), c])
    out = buf.getvalue()
    assert "Copper is ready" in out and "copper freerouting --install" in out.split("Copper is ready")[1], out
    assert "Optional" not in out, out


def test_freerouting_reuses_shared_tools_dir():
    from copper_kicad.install import freerouting

    class FakeFr:
        FREEROUTING_VERSION, JAVA_MAJOR = "9.9.9", 99

        def __init__(self, d):
            self.d = d

        def tools_dir(self):
            return self.d

        def find_java(self):
            return None

    with Sandbox() as sb:
        shared, mine = sb.dir / "shared", sb.dir / "mine"
        (shared / "jre-99" / "bin").mkdir(parents=True)
        (shared / "jre-99" / "bin" / "java.exe").write_bytes(b"j")
        (shared / "freerouting-9.9.9.jar").write_bytes(b"x" * 10)
        mine.mkdir()
        orig = freerouting.shared_tools_dir
        freerouting.shared_tools_dir = lambda: shared
        try:
            got = freerouting.reuse_shared(FakeFr(mine))
            assert set(got) == {"freerouting-9.9.9.jar", "jre-99"}, got
            assert (mine / "jre-99" / "bin" / "java.exe").exists() and (mine / "freerouting-9.9.9.jar").exists()
            assert freerouting.reuse_shared(FakeFr(mine)) == []            # nothing left to copy
            assert freerouting.reuse_shared(FakeFr(shared)) == []          # same dir: no-op
        finally:
            freerouting.shared_tools_dir = orig


def test_help_hides_internal_winpath():
    from copper_kicad import cli
    r = _cli("--help")
    assert r.returncode == 0 and "SUPPRESS" not in r.stdout and "_winpath" not in r.stdout, r.stdout
    a = cli._winpath_parser().parse_args(["add", "C:\\x"])
    assert a.action == "add" and a.dir == "C:\\x"


def test_bare_copper_explains_itself_and_detects_mintty():
    import re as _re
    from copper_kicad import cli
    with Sandbox():
        msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
        r = _cli(input=msg, timeout=60)
        assert "serving MCP on stdio" in r.stderr and "copper --help" in r.stderr, r.stderr
        assert all(json.loads(ln) for ln in r.stdout.splitlines() if ln.strip())   # stdout stays pure JSON-RPC
    assert _re.search(cli._PTY_PIPE, r"\msys-1888ae32e00d56aa-pty0-from-master")
    assert _re.search(cli._PTY_PIPE, r"\cygwin-e022582115c10879-pty3-to-master")
    assert not _re.search(cli._PTY_PIPE, r"\Device\NamedPipe\anthropic-claude-mcp-1234")
    assert cli._mintty_pipe(None) is False


def test_config_override_sandboxes_kicad_config_and_copper_home():
    with Sandbox() as sb:
        env = _env_without("KICAD_CONFIG_HOME", "COPPER_HOME")
        r = _cli_env(env, "kicad-api", "status")
        assert r.returncode == 0 and str(sb.home) in r.stdout, r.stdout + r.stderr
        r = _cli_env(env, "freerouting", "--json")
        assert json.loads(r.stdout)["tools_dir"].startswith(str(sb.home)), r.stdout
        # an explicit KICAD_CONFIG_HOME still wins
        r = _cli_env({**env, "KICAD_CONFIG_HOME": str(sb.dir / "kc")}, "kicad-api", "status")
        assert str(sb.dir / "kc") in r.stdout, r.stdout


def test_setup_yes_banner_is_honest():
    with Sandbox() as sb:
        r = _cli_env(dict(os.environ, KICAD_CONFIG_HOME=str(sb.dir / "kc")), "setup", "--yes", "--clients", "none",
                     "--api", "skip", "--no-libs", "--no-doctor")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Nothing is changed without asking" not in r.stdout and "--yes" in r.stdout, r.stdout


def test_config_claude_code_works_in_both_powershells():
    r = _cli("config", "--client", "claude-code")
    lines = r.stdout.splitlines()
    i7 = next(i for i, ln in enumerate(lines) if "PowerShell 7.3+" in ln)
    i5 = next(i for i, ln in enumerate(lines) if "PowerShell 5.1" in ln)
    ps7, ps5 = lines[i7 + 1], lines[i5 + 1]
    j7 = ps7[ps7.index("'") + 1:ps7.rindex("'")].replace("''", "'")
    assert json.loads(j7)["args"][-1] in ("serve",) or json.loads(j7)["command"], ps7    # verbatim, valid JSON
    assert '\\"' in ps5 and '\\"' not in ps7, (ps5, ps7)
    assert "copper install --client claude-code" in r.stdout


def test_install_with_nothing_detected_prints_exact_commands():
    with Sandbox():
        r = _cli("install", "--client", "all")
        assert r.returncode == 0 and "copper install --client claude-desktop" in r.stdout, r.stdout


def test_uninstall_removes_files_copper_created_and_lists_backups():
    with Sandbox() as sb:
        sb.write(".gemini/settings.json", '{"theme": "x"}\n')
        assert _cli("install", "--client", "cursor,gemini,codex").returncode == 0
        assert _cli("install", "--client", "cursor").returncode == 0           # repeat keeps "created"
        man = json.loads((sb.home / ".copper" / "clients.json").read_text(encoding="utf-8"))
        assert man["cursor"][str(sb.home / ".cursor" / "mcp.json")]["created"] == "dir", man
        assert "created" not in man["gemini"][str(sb.home / ".gemini" / "settings.json")]
        r = _cli("uninstall", "--json")
        d = json.loads(r.stdout)
        assert not (sb.home / ".cursor" / "mcp.json").exists() and not (sb.home / ".cursor").exists()
        assert not (sb.home / ".codex" / "config.toml").exists()
        g = sb.home / ".gemini" / "settings.json"
        assert g.exists() and '"theme": "x"' in g.read_text(encoding="utf-8")        # the user's own file stays
        assert d["backups"] == [str(g) + clients.BACKUP_SUFFIX], d
        r = _cli("uninstall")
        assert "nothing" in r.stdout.lower() or "not registered" in r.stdout


def test_uninstall_keeps_created_file_the_user_added_to():
    with Sandbox() as sb:
        _cli("install", "--client", "cursor")
        p = sb.home / ".cursor" / "mcp.json"
        p.write_text(jsonc.set_value(p.read_text(encoding="utf-8"), ["mcpServers", "mine"], {"command": "x"}),
                     encoding="utf-8")
        _cli("uninstall", "--client", "cursor")
        assert p.exists() and '"mine"' in p.read_text(encoding="utf-8") and "copper" not in p.read_text(encoding="utf-8")


def test_doctor_warns_about_plugin_plus_user_server():
    from copper_kicad.install import doctor
    with Sandbox() as sb:
        clients.install(["claude-code"])
        assert not any(c.id == "client_claude-code_dup" for c in doctor.check_clients())
        sb.write(".claude/plugins/installed_plugins.json", json.dumps({"version": 2, "plugins": {"copper@copper": [{}]}}))
        dup = [c for c in doctor.check_clients() if c.id == "client_claude-code_dup"]
        assert dup and dup[0].status == "warn" and "claude plugin uninstall copper@copper" in dup[0].fix, dup


def test_mcpb_entry_point_runs_as_plain_script():
    man = json.loads((ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    ep = ROOT / man["server"]["entry_point"]
    env = _env_without("PYTHONPATH", NO_COLOR="1", PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(ep), "version"], capture_output=True, text=True, env=env,
                       cwd=tempfile.gettempdir(), timeout=60)
    assert r.returncode == 0 and r.stdout.startswith("copper "), r.stdout + r.stderr
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_release
    src = Path(build_release.__file__).read_text(encoding="utf-8")
    assert '".mcpbignore")' not in src, "the bundle must not ship .mcpbignore"


def test_build_release_detects_stale_dist():
    import zipfile
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_release as br
    with Sandbox() as sb:
        dist = sb.dir / "dist"
        dist.mkdir()
        assert br.check(dist) and "BUILD-INFO" in br.check(dist)[0]
        # a wheel built from exactly src/ passes, one stale file fails
        whl = dist / "copper_kicad-0-py3-none-any.whl"
        pkg = SRC / "copper_kicad"
        files = [p for p in br._source_files() if pkg in p.parents]
        with zipfile.ZipFile(whl, "w") as z:
            for p in files:
                z.write(p, p.relative_to(SRC).as_posix())
        br.check_wheel_matches_src(whl)
        stale = dist / "stale.whl"
        with zipfile.ZipFile(stale, "w") as z:
            for p in files:
                data = p.read_bytes() + (b"\n# old" if p.name == "cli.py" else b"")
                z.writestr(p.relative_to(SRC).as_posix(), data)
        try:
            br.check_wheel_matches_src(stale)
            raise AssertionError("stale wheel accepted")
        except SystemExit as e:
            assert "cli.py" in str(e), e
        stale.unlink()
        info = {"version": br.version(), "source_sha256": br.source_digest(), "artifacts": {whl.name: br.sha256(whl)}}
        (dist / "BUILD-INFO.json").write_text(json.dumps(info), encoding="utf-8")
        assert br.check(dist) == [], br.check(dist)
        info["source_sha256"] = "0" * 64
        (dist / "BUILD-INFO.json").write_text(json.dumps(info), encoding="utf-8")
        (dist / "Copper-for-KiCad-0.9-Setup.exe").write_bytes(b"old")
        probs = " | ".join(br.check(dist))
        assert "src/ changed" in probs and "Setup.exe was not produced" in probs, probs


def test_installer_offers_every_client_and_autorouting():
    nsi = (ROOT / "installer" / "windows" / "copper.nsi").read_text(encoding="utf-8")
    added = set(re.findall(r'AddClient "([\w-]+)"', nsi))
    assert added == set(clients.BY_ID), set(clients.BY_ID) ^ added          # incl. copilot-cli
    assert 'SectionSetText ${SEC} ""' not in nsi, "undetected apps must stay visible (unticked)"
    for sec in re.findall(r"Section \"[^\"]+\" (Sec\w+)\n\s+!insertmacro AddClient", nsi):
        assert re.search(r"!insertmacro ShowIf \$\{" + sec + r"\} \$R4 \"[^\"]+\"", nsi), sec
    fr = re.search(r'Section (/o )?"[^"]*Freerouting[^"]*" SecFreerouting', nsi)
    assert fr and not fr.group(1), "Freerouting must be ticked by default"
    init = nsi[nsi.index("Function .onInit"):]
    assert 'ReadEnvStr $R3 "COPPER_HOME"' in init and "UnselectSection ${SecFreerouting}" in init
    assert "copper install --client" in nsi[nsi.index("MUI_FINISHPAGE_TEXT"):nsi.index("MUI_FINISHPAGE_RUN")]


def test_one_liners_ask_before_changing_things():
    ps = (ROOT / "installer" / "install.ps1").read_text(encoding="ascii")
    assert "[Console]::IsInputRedirected" in ps and "else { @('--yes') }" not in ps
    sh = (ROOT / "installer" / "install.sh").read_text(encoding="utf-8")
    assert '"$COPPER" setup ${COPPER_SETUP_ARGS:-} </dev/tty' in sh
    assert '"$COPPER" setup ${COPPER_SETUP_ARGS:---yes} </dev/null' in sh


def test_codex_plugin_metadata_follows_docs():
    cx = json.loads((ROOT / "plugin" / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert "defaultPrompts" not in cx["interface"] and isinstance(cx["interface"]["defaultPrompt"], list)
    for f in ("README.md", "plugin/README.md"):
        assert "codex plugin add" not in (ROOT / f).read_text(encoding="utf-8"), f


def test_readme_commands_match_cli():
    """Every `copper …` command shown in the README and docs/INSTALL.md parses with the real CLI."""
    import shlex
    from copper_kicad import cli
    text = "\n".join((ROOT / f).read_text(encoding="utf-8") for f in ("README.md", "docs/INSTALL.md"))
    # `npx -y copper-kicad <args>` passes <args> to the same CLI
    text += "\n".join("`copper " + a + "`" for a in re.findall(r"npx -y copper-kicad ([a-z][^`|\n]*)", text))
    cmds = set(re.findall(r"`(copper [^`<]+)`", text))
    cmds |= {ln.split("#")[0].strip() for ln in re.findall(r"(?m)^(copper .+)$", text)}
    assert len(cmds) >= 8, cmds
    p = cli.build_parser()
    for c in sorted(cmds):
        argv = shlex.split(c.replace("\\|", "|"))[1:]
        try:
            a = p.parse_args(argv)
        except SystemExit as e:
            if e.code == 0:          # --help
                continue
            raise AssertionError(f"README shows a command the CLI rejects: {c}")
        assert getattr(a, "fn", None) or a.version, c
    for cid in re.findall(r"--client ([\w,-]+)", text):
        for n in cid.split(","):
            if n not in ("all", "json", "generic"):
                assert clients.ALIASES.get(n, n) in clients.BY_ID, n


def test_config_prints_a_plain_stdio_entry_for_any_client_and_rejects_unknown_names_cleanly():
    """`copper config --client generic` used to die with a ValueError traceback: a client Copper has no installer
    for must still get the plain stdio JSON, and a typo must be one line on stderr with exit code 2."""
    plain = _cli("config")
    assert plain.returncode == 0 and "mcpServers" in json.loads(plain.stdout), plain.stderr
    for name in ("generic", "json", "mcp", "stdio"):
        r = _cli("config", "--client", name)
        assert r.returncode == 0 and json.loads(r.stdout) == json.loads(plain.stdout), (name, r.stderr)
    bad = _cli("config", "--client", "no-such-client")
    assert bad.returncode == 2 and "Traceback" not in bad.stderr and "--client generic" in bad.stderr, bad.stderr


def test_canvas_command_parses_as_documented_and_calls_serve():
    """`copper canvas [project] [--port N] [--no-open]` (README / docs/FOCUS.md) → focus.canvas.serve(project, port, launch)."""
    import shlex
    import types
    from copper_kicad import cli
    p = cli.build_parser()
    docs = "".join((ROOT / f).read_text(encoding="utf-8") for f in ("README.md", "docs/FOCUS.md") if (ROOT / f).exists())
    shown = {c.strip() for c in re.findall(r"`(copper canvas[^`\[<]*)`", docs)}
    assert "copper canvas" in shown, shown                       # the docs do tell people to run it
    for c in sorted(shown | {"copper canvas", "copper canvas --no-open", "copper canvas C:/boards/amp --port 8765 --no-open",
                             "copper canvas ./amp/amp.kicad_pro"}):
        a = p.parse_args(shlex.split(c)[1:])
        assert a.fn is cli.cmd_canvas, c
    a = p.parse_args(["canvas"])
    assert (a.project, a.port, a.no_open) == (None, 0, False), a
    for bad in (["canvas", "--port", "abc"], ["canvas", "a", "b"]):
        import contextlib
        import io
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                p.parse_args(bad)
            raise AssertionError(f"accepted {bad}")
        except SystemExit as e:
            assert e.code == 2
    assert "copper canvas [project] [--port N] [--no-open]" in cli.__doc__
    assert "canvas" in _cli("--help").stdout
    # the command hands exactly (project, port, launch) to the canvas; the focus package is imported only then
    calls = []
    fake = types.ModuleType("copper_kicad.focus.canvas")
    fake.serve = lambda project=None, port=0, launch=True: calls.append((project, port, launch)) or 0
    import copper_kicad.focus as focus_pkg
    saved_mod, saved_attr = sys.modules.get("copper_kicad.focus.canvas"), getattr(focus_pkg, "canvas", None)
    sys.modules["copper_kicad.focus.canvas"] = fake
    focus_pkg.canvas = fake
    try:
        assert cli.main(["canvas", "C:/boards/amp", "--port", "8765", "--no-open"]) == 0
        assert cli.main(["canvas"]) == 0
    finally:
        if saved_mod is not None:
            sys.modules["copper_kicad.focus.canvas"] = saved_mod
        else:
            sys.modules.pop("copper_kicad.focus.canvas", None)
        if saved_attr is not None:
            focus_pkg.canvas = saved_attr
        else:
            delattr(focus_pkg, "canvas")
    assert calls == [("C:/boards/amp", 8765, False), (None, 0, True)], calls
    from copper_kicad.focus import canvas as real          # the real entry point has the signature the CLI relies on
    import inspect
    assert list(inspect.signature(real.serve).parameters)[:3] == ["project", "port", "launch"]
    r = _cli("canvas", str(ROOT / "no-such-project-dir-xyz"), "--no-open")
    assert r.returncode == 1 and "Traceback" not in (r.stdout + r.stderr), (r.returncode, r.stderr[-400:])


def test_first_use_download_counts_as_autorouting_available():
    import io
    from copper_kicad import cli
    from copper_kicad.install import doctor, freerouting

    class FakeFr:
        JAVA_MAJOR = 25

        def __init__(self, st):
            self.st = st

        def status(self):
            return self.st

        def tools_dir(self):
            return "/x/tools"
    first = ["Java 25 runtime (~60 MB)", "Freerouting 2.2.3 (~60 MB)"]
    orig = freerouting._backend
    try:
        freerouting._backend = lambda: FakeFr({"java": None, "java_version": None, "freerouting_jar": None,
                                               "freerouting_version": "2.2.3", "ready": False, "first_route_downloads": first})
        f = freerouting.find()
        assert f["ok"] is False and f["available"] is True and f["first_use_downloads"] == first, f
        assert "downloads on first use" in freerouting.describe(f) and "Freerouting 2.2.3" in freerouting.describe(f)
        c = doctor.check_freerouting()
        assert c.status == "ok" and c.group == "pcb" and "downloads on first use" in c.detail, c
        assert "copper freerouting --install" in c.detail, c.detail          # still says how to pre-download
        buf = io.StringIO()
        cli._print_doctor(cli.UI(buf, color=False), [doctor.Check("python", "Python", "ok"), c])
        out = buf.getvalue()
        assert "Copper is ready" in out and "not installed yet" not in out and "downloads on first use" in out, out
        freerouting._backend = lambda: FakeFr({"java": "/j", "java_version": "25", "freerouting_jar": "/x/fr.jar",
                                               "freerouting_version": "2.2.3", "ready": True})
        f = freerouting.find()
        assert f["ok"] and f["available"] and f["first_use_downloads"] == [] and f["fix"] is None, f
        # a backend that reports neither ready nor a first-use download is still a warning
        freerouting._backend = lambda: FakeFr({"java": None, "java_version": None, "freerouting_jar": "/x/fr.jar",
                                               "freerouting_version": "2.2.3", "ready": False})
        f = freerouting.find()
        assert not f["ok"] and not f["available"] and "Java" in f["problem"], f
        assert doctor.check_freerouting().status == "warn"
    finally:
        freerouting._backend = orig
    r = _cli("freerouting", "--json")
    live = json.loads(r.stdout)
    assert live["available"] is True and r.returncode == 0, (r.returncode, live)


def test_marketplaces_carry_codex_policy():
    from copper_kicad import __version__
    cl = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    ag = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
    assert ag["interface"] == {"displayName": "Copper for KiCad"} and ag["name"] == cl["name"] == "copper"
    for mk in (cl, ag):
        pl = mk["plugins"][0]
        assert pl["policy"] == {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, pl
        assert pl["category"] == "Development" and pl["version"] == __version__ and pl["source"] == "./plugin", pl
        assert (ROOT / pl["source"] / ".codex-plugin" / "plugin.json").exists()
    assert {k: v for k, v in ag.items() if k != "interface"} == cl          # one source of truth, two locations


# ── runner ────────────────────────────────────────────────────────────────
def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in tests:
        try:
            f()
            print(f"PASS {n}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {n}\n{traceback.format_exc()}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
