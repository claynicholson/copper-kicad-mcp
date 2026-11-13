"""Install / link / KiCad-transition tests: `copper link`, the credential store, GitHub linking,
bringing a KiCad setup along (with undo), the onboarding page, the windowless launcher and the
installer files.

Runs with pytest or plainly:  python tests/test_install.py
Everything happens in temp sandboxes (COPPER_CONFIG_HOME_OVERRIDE + COPPER_HOME + an unversioned
KICAD_CONFIG_HOME). The real KiCad config, ~/.copper, AI-client configs and the OS credential store are
never touched; set COPPER_TEST_REAL_KEYCHAIN=1 to add one round trip through the real OS store under a
throw-away name.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
# Copper's own state must never land in the real ~/.copper, whatever a test forgets to sandbox
os.environ.setdefault("COPPER_HOME", tempfile.mkdtemp(prefix="copper-install-home-"))
os.environ["COPPER_NO_BROWSER"] = "1"          # never open a browser tab or a message box from a test

from copper_kicad import project as P  # noqa: E402
from copper_kicad.install import clients, kicad_import as K, kicad_setup, linkcli, onboard, prefs  # noqa: E402
from copper_kicad.kicad import env  # noqa: E402
from copper_kicad.link import apps, github, redact, store  # noqa: E402

_ENV = ("COPPER_CONFIG_HOME_OVERRIDE", "COPPER_HOME", "KICAD_CONFIG_HOME", "KICAD_DOCUMENTS_HOME", "COPPER_LAUNCHER",
        "COPPER_GITHUB_CLIENT_ID", "COPPER_GH", "COPPER_SECRET_BACKEND", "COPPER_KICAD_VERSION",
        "COPPER_KICAD_VERSION_FROM_PREFS", "COPPER_OFFLINE")


class Sandbox:
    """A fake home: AI-client configs, Copper's data folder, KiCad's config root and Documents/KiCad."""

    def __init__(self, kicad9: bool = False, clients_found=(".cursor", ".codex")):
        self.kicad9, self.found = kicad9, clients_found

    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="copper-install-test-"))
        self.home = self.dir / "home"
        self.home.mkdir()
        self._old = {k: os.environ.get(k) for k in _ENV}
        for k in _ENV:
            os.environ.pop(k, None)
        os.environ["COPPER_CONFIG_HOME_OVERRIDE"] = str(self.home)
        os.environ["COPPER_HOME"] = str(self.home / ".copper")
        os.environ["KICAD_CONFIG_HOME"] = str(self.home / "kicad")
        self._state = (P.STATE_DIR, P.STATE_FILE)
        P.STATE_DIR, P.STATE_FILE = self.home / ".copper", self.home / ".copper" / "state.json"
        self._running = kicad_setup.running_kicad
        kicad_setup.running_kicad = lambda: []          # a KiCad open on the dev machine must not fail tests
        for c in self.found:
            (self.home / c).mkdir()
        if self.kicad9:
            self.make_kicad9()
        env.refresh()
        store.reset_cache()
        redact.clear()
        return self

    def __exit__(self, *a):
        kicad_setup.running_kicad = self._running
        P.STATE_DIR, P.STATE_FILE = self._state
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        env.refresh()
        store.reset_cache()
        redact.clear()
        shutil.rmtree(self.dir, ignore_errors=True)

    @property
    def k9(self) -> Path:
        return self.home / "kicad" / "9.0"

    @property
    def k10(self) -> Path:
        return self.home / "kicad" / env.config_dir().name

    def make_kicad9(self):
        """Someone coming from KiCad 9: own libraries (one through a path variable, one whose folder is
        gone), a theme, hotkeys, a plugin, two recent projects (one deleted), the IPC API off."""
        k9 = self.k9
        k9.mkdir(parents=True)
        lib = self.home / "mylibs"
        lib.mkdir()
        (lib / "Mine.kicad_sym").write_text("(kicad_symbol_lib)", encoding="utf-8")
        q = str(lib).replace("\\", "/")
        (k9 / "sym-lib-table").write_text(
            '(sym_lib_table\n\t(version 7)\n'
            '\t(lib (name "Device") (type "KiCad") (uri "${KICAD9_SYMBOL_DIR}/Device.kicad_sym") (options "") (descr "d"))\n'
            f'\t(lib (name "Mine") (type "KiCad") (uri "{q}/Mine.kicad_sym") (options "") (descr "my \\"own\\" parts"))\n'
            '\t(lib (name "Var") (type "KiCad") (uri "${MYLIBS}/Mine.kicad_sym") (options "") (descr ""))\n)\n', encoding="utf-8")
        (k9 / "fp-lib-table").write_text(
            '(fp_lib_table\n\t(version 7)\n'
            '\t(lib (name "Gone") (type "KiCad") (uri "C:/nope/Gone.pretty") (options "") (descr ""))\n)\n', encoding="utf-8")
        (k9 / "colors").mkdir()
        (k9 / "colors" / "solar.json").write_text('{"meta": {"name": "Solar"}}', encoding="utf-8")
        (k9 / "user.hotkeys").write_text("common.Control.zoomIn\tCtrl+=\n", encoding="utf-8")
        self.proj = self.home / "proj" / "amp.kicad_pro"
        self.proj.parent.mkdir()
        self.proj.write_text(json.dumps({"board": {"design_settings": {"rules": {"min_clearance": 0.15}}},
                                         "net_settings": {"classes": [{"name": "Default", "clearance": 0.2},
                                                                      {"name": "Power", "track_width": 0.5}]}}), encoding="utf-8")
        (k9 / "kicad.json").write_text(json.dumps({"system": {"file_history": [str(self.proj), "C:/gone/x.kicad_pro"]}}), encoding="utf-8")
        (k9 / "kicad_common.json").write_text(json.dumps({"environment": {"vars": {"MYLIBS": q}},
                                                          "api": {"enable_server": False}}), encoding="utf-8")
        (k9 / "eeschema.json").write_text('{"appearance": {"x": 1}}', encoding="utf-8")
        d = self.home / "Documents" / "KiCad" / "9.0" / "scripting" / "plugins" / "myplug"
        d.mkdir(parents=True)
        (d / "__init__.py").write_text("x = 1\n", encoding="utf-8")


def _tree(d: Path) -> dict:
    return {str(p.relative_to(d)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*")) if p.is_file()}


def _has_kicad() -> bool:
    try:
        env.kicad()
        return True
    except Exception:  # noqa: BLE001
        return False


def _cli(*args, input=None, env_extra=None, timeout=180):
    e = dict(os.environ)
    e.update(NO_COLOR="1", PYTHONIOENCODING="utf-8", COPPER_NO_BROWSER="1", COPPER_TEST_KICAD_CLOSED="1")
    e.update(env_extra or {})
    boot = f"import sys; sys.path.insert(0, {str(SRC)!r}); from copper_kicad.cli import main; sys.exit(main(sys.argv[1:]))"
    return subprocess.run([sys.executable, "-c", boot, *args], input=input, capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout, env=e, cwd=tempfile.gettempdir())


# ── redaction ─────────────────────────────────────────────────────────────
def test_redactor_scrubs_every_form_and_token_shapes():
    redact.clear()
    try:
        secret = "s3cr3t/with+odd=chars&more"
        redact.register(secret)
        import base64
        forms = [secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret),
                 base64.b64encode(secret.encode()).decode(), json.dumps(secret)[1:-1]]
        text = " | ".join(forms)
        out = redact.scrub(text)
        assert all(f not in out for f in forms) and out.count(redact.REDACTED) >= 4, out
        nested = redact.scrub_obj({"a": [secret, {"b": f"x{secret}y"}], secret: 1})
        assert secret not in json.dumps(nested)
        redact.register("abc")                                  # too short: would shred ordinary text
        assert redact.scrub("abcabc") == "abcabc"
        # GitHub token shapes are scrubbed even when never registered
        assert "gho_" not in redact.scrub("token gho_" + "A1b2" * 9 + " end")
        assert "github_pat_" not in redact.scrub("x github_pat_" + "Z9_" * 10)
        assert redact.safe_url("https://u:p@h.example/p?apiKey=K&q=1") == "https://h.example/p?apiKey=…&q=…"
    finally:
        redact.clear()


# ── credential store ──────────────────────────────────────────────────────
def _conformance(st):
    assert st.get("nope") is None and st.delete("nope") is False
    st.set("github", "gho_" + "x" * 36)
    assert st.get("github") == "gho_" + "x" * 36
    st.set("uni", "ключ-鍵-key")
    assert st.get("uni") == "ключ-鍵-key"
    st.set("github", "second")
    assert st.get("github") == "second"
    if st.id != "keychain":
        assert st.names() == ["github", "uni"], st.names()
    assert st.delete("github") is True and st.get("github") is None
    assert st.delete("uni") is True
    for bad in ("", "A", "../x", "a b"):
        try:
            st.set(bad, "v")
            raise AssertionError(f"name {bad!r} accepted")
        except ValueError:
            pass


def test_store_memory_and_keyfile_conformance_and_tamper():
    _conformance(store.MemoryStore())
    d = Path(tempfile.mkdtemp(prefix="copper-kf-"))
    try:
        kf = store.KeyFile(d)
        _conformance(kf)
        kf.set("github", "CANARY-0123456789")
        raw = kf.data.read_bytes()
        assert b"CANARY" not in raw and b"github" not in raw, "the key file must not hold values in clear"
        kf.data.write_bytes(raw[:30] + bytes([raw[30] ^ 1]) + raw[31:])           # one flipped bit
        try:
            kf.get("github")
            raise AssertionError("tampered file accepted")
        except store.StoreError:
            pass
        kf.data.write_bytes(raw)
        assert kf.get("github") == "CANARY-0123456789"
        kf.keyf.unlink()
        try:
            kf.get("github")
            raise AssertionError("read without the key file")
        except store.StoreError as e:
            assert "missing" in str(e)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_store_sandbox_never_loads_the_os_store():
    with Sandbox() as sb:
        st = store.get_store()
        assert st.id == "keyfile" and Path(st.dir) == sb.home / ".copper", (st.id, getattr(st, "dir", None))
        assert store.describe()["strength"] == "file-permissions"
        os.environ["COPPER_SECRET_BACKEND"] = "memory"
        assert store.get_store().id == "memory"


def test_store_refuses_to_fall_back_silently():
    saved = store._candidates
    old = {k: os.environ.pop(k, None) for k in ("COPPER_CONFIG_HOME_OVERRIDE", "COPPER_SECRET_BACKEND")}
    home = tempfile.mkdtemp(prefix="copper-nostore-")
    old["COPPER_HOME"] = os.environ.get("COPPER_HOME")
    os.environ["COPPER_HOME"] = home

    class Never(store.SecretStore):
        name = "pretend keyring"

        def available(self):
            return False, "locked"
    try:
        store._candidates = lambda: [Never()]
        store.reset_cache()
        try:
            store.get_store()
            raise AssertionError("fell back to a file without being asked")
        except store.StoreError as e:
            assert "gh auth login" in str(e) and "--allow-weak-store" in str(e), e
        assert store.describe()["available"] is False
        assert store.get_store(allow_weak=True).id == "keyfile"       # explicit opt-in only
        assert not (Path(home) / "credentials.bin").exists()          # and nothing is written until used
    finally:
        store._candidates = saved
        store.reset_cache()
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)


def test_store_real_os_keychain_roundtrip_opt_in():
    if os.environ.get("COPPER_TEST_REAL_KEYCHAIN") != "1":
        return
    import secrets
    if sys.platform == "win32":
        st = store.WinCred(prefix="CopperTest-" + secrets.token_hex(4) + ":")
        assert st.available()[0] and st.names() == []
        try:
            _conformance(st)
            st.set("big", "a" * store.MAX_BLOB)
            assert len(st.get("big")) == store.MAX_BLOB
            try:
                st.set("big", "a" * (store.MAX_BLOB + 1))
                raise AssertionError("oversized blob accepted")
            except store.StoreError:
                pass
        finally:
            for n in st.names():
                st.delete(n)
        assert st.names() == []
    else:
        st = store._os_store()
        if st is None:
            return
        name = "selftest-" + secrets.token_hex(4)
        try:
            st.set(name, "v1")
            assert st.get(name) == "v1"
        finally:
            st.delete(name)
        assert st.get(name) is None


# ── AI clients ────────────────────────────────────────────────────────────
def test_only_copper_entries_are_launched():
    py = sys.executable
    good = [{"command": r"C:\x\bin\copper.exe", "args": ["serve"]},
            {"command": "/home/u/.local/bin/copper-kicad", "args": ["serve"]},
            {"command": py, "args": ["-m", "copper_kicad", "serve"]},
            {"command": py, "args": ["-c", "import sys; sys.path.insert(0, 'x'); from copper_kicad.cli import main; sys.exit(main(['serve']))"]},
            {"command": "/usr/bin/uvx", "args": ["--from", "copper-kicad", "copper", "serve"]},
            {"command": {"path": "/opt/copper", "args": ["serve"]}},                       # legacy Zed shape
            {"command": "/home/u/Copper-for-KiCad-1.0.0-x86_64.AppImage", "args": ["serve"]}]
    bad = [None, {}, {"command": "cmd", "args": ["/c", "calc"]}, {"command": py, "args": ["-c", "import os"]},
           {"command": "/bin/sh", "args": ["-c", "copper serve; rm -rf ~"]}, {"command": "copper", "args": ["doctor"]}]
    for e in good:
        assert apps.is_copper_entry(e)[0], e
    for e in bad:
        assert not apps.is_copper_entry(e)[0], e
        if e:
            r = apps.verify_entry(e)
            assert r["ok"] is False and r["stage"] == "recognise", r      # refused before anything is started


def test_verify_entry_reports_the_failing_stage():
    r = apps.verify_entry({"command": str(Path(tempfile.gettempdir()) / "nope" / "copper.exe"), "args": ["serve"]})
    assert r["ok"] is False and r["stage"] == "spawn", r
    d = Path(tempfile.mkdtemp(prefix="copper-fake-"))
    try:
        # a "copper" that prints a banner on stdout instead of speaking MCP
        (d / "copper_kicad").mkdir()
        (d / "copper_kicad" / "__init__.py").write_text("", encoding="utf-8")
        (d / "copper_kicad" / "__main__.py").write_text("print('Welcome to not-MCP', flush=True)\nimport sys; sys.stdin.read()\n", encoding="utf-8")
        r = apps.verify_entry({"command": sys.executable, "args": ["-m", "copper_kicad", "serve"], "env": {"PYTHONPATH": str(d)}})
        if sys.executable.lower().replace("\\", "/").find("/kicad/") < 0:      # KiCad's python ignores PYTHONPATH
            assert r["ok"] is False and r["stage"] == "initialize" and "not MCP" in r["detail"], r
        # one that exits at once
        (d / "copper_kicad" / "__main__.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
        r = apps.verify_entry({"command": sys.executable, "args": ["-m", "copper_kicad", "serve"], "env": {"PYTHONPATH": str(d)}})
        assert r["ok"] is False and r["stage"] == "initialize", r
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_link_apps_connect_verify_status_unlink():
    with Sandbox() as sb:
        st = {a["id"]: a for a in apps.status()}
        assert st["cursor"]["state"] == "found" and st["codex"]["state"] == "found" and st["zed"]["state"] == "not-found"
        assert st["cursor"]["fix"] == "copper link cursor"
        res = apps.connect("all")                                     # one action, every client found
        assert res["ok"] and sorted(c["client"] for c in res["changes"]) == ["codex", "cursor"], res
        assert all(v["ok"] and v["tools"] >= 20 and v["initialize_ms"] is not None for v in res["verify"]), res["verify"]
        st = {a["id"]: a for a in apps.status(verify_launch=True)}
        assert st["cursor"]["state"] == "connected" and st["cursor"]["checked"] and st["cursor"]["tools"] >= 20
        assert "launches in" in st["codex"]["detail"]
        # a real test call through the registered command
        t = apps.test_call()
        assert t["ok"] and t["call"]["tool"] == "copper_status" and not t["call"]["is_error"] and "Copper" in t["call"]["text"], t
        # break Cursor's entry the way a moved install does: registered, command gone
        cfg = sb.home / ".cursor" / "mcp.json"
        d = json.loads(cfg.read_text(encoding="utf-8"))
        d["mcpServers"]["copper"]["command"] = str(sb.home / "gone" / "copper.exe")
        cfg.write_text(json.dumps(d), encoding="utf-8")
        st = {a["id"]: a for a in apps.status(verify_launch=True)}
        assert st["cursor"]["state"] == "broken" and st["cursor"]["fix"] == "copper link cursor", st["cursor"]
        assert st["codex"]["state"] == "connected"
        # …and an entry that exists on disk but is not Copper's is never started
        d["mcpServers"]["copper"] = {"command": sys.executable, "args": ["-c", "open('pwned','w')"]}
        cfg.write_text(json.dumps(d), encoding="utf-8")
        r = [v for v in apps.verify("cursor")][0]
        assert r["ok"] is False and r["stage"] == "recognise" and not Path("pwned").exists(), r
        assert apps.connect("cursor")["ok"]                           # relinking repairs it
        un = apps.disconnect(["cursor", "codex"])
        assert sorted(c["client"] for c in un["changes"]) == ["codex", "cursor"]
        assert all(a["state"] in ("found", "not-found") for a in apps.status())


def test_plugin_install_is_reported_as_connected_and_not_registered_twice():
    with Sandbox(clients_found=(".cursor", ".claude")) as sb:
        f = sb.home / ".claude" / "plugins" / "installed_plugins.json"
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps({"plugins": {"copper@copper": [{"version": "1.0.0"}]}}), encoding="utf-8")
        row = {a["id"]: a for a in apps.status()}["claude-code"]
        assert row["state"] == "connected" and row["via_plugin"] == "copper@copper", row
        res = apps.connect("all", verify_launch=False)
        assert [c["client"] for c in res["changes"]] == ["cursor"] and res["skipped"][0]["client"] == "claude-code", res
        assert not (sb.home / ".claude.json").exists()


# ── GitHub ────────────────────────────────────────────────────────────────
def test_no_client_id_is_shipped_and_no_route_is_explained():
    src = "".join(p.read_text(encoding="utf-8") for p in (SRC / "copper_kicad").rglob("*.py"))
    assert not re.search(r"\b(?:Iv1\.[0-9a-f]{16}|Ov23[A-Za-z0-9]{16}|[0-9a-f]{20})\b", src), "a GitHub client id is hard-coded"
    with Sandbox():
        assert github.client_id() is None and github.gh_path() is None
        assert github.status()["linked"] is False and github.available()["how"] == "none"
        try:
            github.link(interactive=False)
            raise AssertionError("linked with no route")
        except github.LinkError as e:
            assert e.code == "no_route" and "gh auth login" in e.fix and "COPPER_GITHUB_CLIENT_ID" in e.fix, (e.code, e.fix)
        prefs.set("github_client_id", "from-prefs")
        assert github.client_id() == "from-prefs"
        os.environ["COPPER_GITHUB_CLIENT_ID"] = "from-env"
        assert github.client_id() == "from-env" and github.available()["how"] == "device"


def test_github_through_gh_stores_no_token():
    with Sandbox() as sb:
        calls = []
        saved = (github.gh_path, github._gh)

        class R:
            def __init__(self, rc, out=""):
                self.returncode, self.stdout, self.stderr = rc, out, ""
        state = {"logged_in": False}

        def fake_gh(args, timeout=20, interactive=False):
            calls.append(list(args))
            if args[:2] == ["auth", "status"]:
                return R(0 if state["logged_in"] else 1)
            if args[:2] == ["api", "user"]:
                return R(0, "octo-cat\n")
            if args[:2] == ["auth", "login"]:
                state["logged_in"] = True
                return R(0)
            return R(1)
        try:
            github.gh_path = lambda: "gh"
            github._gh = fake_gh
            assert github.available()["how"] == "gh-login"
            try:
                github.link(interactive=False)             # no terminal: Copper cannot run gh's login for the user
                raise AssertionError("ran a login without a terminal")
            except github.LinkError as e:
                assert e.code == "gh_logged_out"
            r = github.link(interactive=True)              # gh's own browser sign-in, then linked
            assert r["linked"] and r["via"] == "gh" and r["user"] == "octo-cat", r
            assert ["auth", "login", "--hostname", "github.com", "--web", "--git-protocol", "https"] in calls
            assert not any("token" in " ".join(c) for c in calls), "Copper must never ask gh for the token"
            assert json.loads((sb.home / ".copper" / "links.json").read_text(encoding="utf-8"))["links"]["github"]["via"] == "gh"
            assert not (sb.home / ".copper" / "credentials.bin").exists(), "a token was stored although gh holds the login"
            assert github.status()["linked"] and "octo-cat" in github.status()["detail"]
            state["logged_in"] = False                     # the user ran `gh auth logout`
            s = github.status()
            assert s["linked"] is False and "gh auth login" in s["fix"], s
            state["logged_in"] = True
            u = github.unlink()
            assert u["unlinked"] and "gh auth logout" in u["note"] and github.status(live=False)["linked"] is False
        finally:
            github.gh_path, github._gh = saved


class _GitHubStub(BaseHTTPRequestHandler):
    """github.com/login/device/code, /login/oauth/access_token and api.github.com/user in one server."""
    script: list = []
    seen: list = []
    token = "gho_" + "Tk9" * 12

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):  # noqa: N802
        body = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()))
        type(self).seen.append((self.path, body))
        if self.path == "/login/device/code":
            if body.get("client_id") == "disabled":
                return self._send({"error": "device_flow_disabled", "error_description": "Device Flow must be enabled"}, 400)
            return self._send({"device_code": "DEV-" + "c0de" * 8, "user_code": "WDJB-MJHT", "interval": 1,
                               "verification_uri": "https://github.com/login/device", "expires_in": 30})
        if self.path == "/login/oauth/access_token":
            step = type(self).script.pop(0) if type(self).script else "token"
            if step == "token":
                return self._send({"access_token": type(self).token, "token_type": "bearer", "scope": "public_repo"})
            if step == "redirect":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            return self._send({"error": step, "interval": 2} if step == "slow_down" else {"error": step})
        self._send({}, 404)

    def do_GET(self):  # noqa: N802
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        self._send({"login": "octo-device"})


def _stub():
    _GitHubStub.script, _GitHubStub.seen = [], []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _GitHubStub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_github_device_flow_token_goes_to_the_store_only():
    httpd, base = _stub()
    try:
        with Sandbox() as sb:
            waits = []
            _GitHubStub.script = ["authorization_pending", "slow_down", "authorization_pending", "token"]
            info = github.device_start("cid-123", url=base + "/login/device/code")
            assert info["user_code"] == "WDJB-MJHT" and info["interval"] == 1
            token = github.device_poll("cid-123", info, url=base + "/login/oauth/access_token", sleep=waits.append)
            assert token == _GitHubStub.token
            assert waits == [1, 1, 2, 2], f"interval and slow_down not honoured: {waits}"
            assert all("client_secret" not in b for p, b in _GitHubStub.seen if isinstance(b, dict))
            r = github._finish_device(store.get_store(), token, "public_repo", api_user=base + "/user")
            assert r["linked"] and r["via"] == "device" and r["user"] == "octo-device"
            assert token not in json.dumps(r), "the link result carries the token"
            assert ("/user", f"Bearer {token}") in _GitHubStub.seen
            # where it is, and where it is not
            assert store.get_store().get("github") == token
            home = sb.home / ".copper"
            for f in home.rglob("*"):
                if f.is_file():
                    assert token.encode() not in f.read_bytes(), f"token in clear in {f}"
            links = json.loads((home / "links.json").read_text(encoding="utf-8"))
            assert links["links"]["github"] == {**links["links"]["github"], "via": "device", "user": "octo-device"}
            s = github.status()
            assert s["linked"] and "octo-device" in s["detail"] and token not in json.dumps(s)
            assert token not in json.dumps(linkcli.link_status())
            assert redact.scrub(f"boom {token}") == f"boom {redact.REDACTED}"
            # the store being unreadable is reported as such, not as "not linked"
            saved = store.get_store
            try:
                def boom(*a, **k):
                    raise store.StoreError("keyring locked")
                store.get_store = boom
                assert github.status().get("code") == "STORE_UNAVAILABLE"
            finally:
                store.get_store = saved
            u = github.unlink()
            assert u["unlinked"] and u["token_removed"] and "Authorized OAuth Apps" in u["note"]
            assert store.get_store().get("github") is None and github.status()["linked"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_github_device_flow_errors_and_no_redirects():
    httpd, base = _stub()
    try:
        with Sandbox():
            try:
                github.device_start("disabled", url=base + "/login/device/code")
                raise AssertionError("started")
            except github.LinkError as e:
                assert e.code == "device_flow_disabled" and "gh auth login" in e.fix
            info = github.device_start("cid", url=base + "/login/device/code")
            for script, code in ((["access_denied"], "access_denied"), (["expired_token"], "expired_token")):
                _GitHubStub.script = list(script)
                try:
                    github.device_poll("cid", info, url=base + "/login/oauth/access_token", sleep=lambda s: None)
                    raise AssertionError(code)
                except github.LinkError as e:
                    assert e.code == code, (e.code, code)
            _GitHubStub.script = ["redirect"]                  # a redirect would replay the device code elsewhere
            try:
                github.device_poll("cid", info, url=base + "/login/oauth/access_token", sleep=lambda s: None)
                raise AssertionError("followed a redirect")
            except github.LinkError as e:
                assert e.code == "http" and "elsewhere" not in e.message, e.message
            _GitHubStub.script = ["authorization_pending"] * 5
            try:
                github.device_poll("cid", info, url=base + "/login/oauth/access_token", sleep=lambda s: None,
                                   should_stop=lambda: True)
                raise AssertionError("not cancelled")
            except github.LinkError as e:
                assert e.code == "cancelled"
            os.environ["COPPER_OFFLINE"] = "1"
            try:
                github.device_start("cid", url=base + "/login/device/code")
                raise AssertionError("went online in offline mode")
            except github.LinkError as e:
                assert e.code == "network"
    finally:
        httpd.shutdown()
        httpd.server_close()


# ── KiCad transition ──────────────────────────────────────────────────────
def test_kicad_cli_default_config_is_not_mistaken_for_user_settings():
    """`kicad-cli version` creates <config>/<ver>/kicad.json + kicad_common.json. That folder holds no
    settings of the user's: the plan must still offer the import from the older version."""
    with Sandbox(kicad9=True) as sb:
        tgt = sb.home / "kicad" / "10.0"
        tgt.mkdir()
        (tgt / "kicad.json").write_text("{}", encoding="utf-8")
        (tgt / "kicad_common.json").write_text('{"api": {"enable_server": false}}', encoding="utf-8")
        (tgt / "colors").mkdir()
        s = K.scan(tgt)
        assert s["fresh"] is True and K.scan(sb.k9)["fresh"] is False
        assert K.default_source([K.scan(tgt), K.scan(sb.k9)], tgt) == "9.0"


def test_import_plan_defaults_and_yes_never_touches_kicad():
    if not _has_kicad():
        return
    with Sandbox(kicad9=True) as sb:
        disc = K.discover()
        assert disc["sources"] == ["9.0"] and disc["default_source"] == "9.0" and disc["target_fresh"], disc
        pl = K.plan()
        items = {i["id"]: i for i in pl["items"]}
        assert set(items) == set(K.ORDER), set(K.ORDER) - set(items)
        assert all(i["state"] == "importable" for i in items.values()), {k: v["state"] for k, v in items.items()}
        assert items["sym_libs"]["names"] == ["Mine", "Var"] and items["fp_libs"]["names"] == ["Gone"]
        assert items["recent"]["names"] == [str(sb.proj)] and items["recent"]["modifies_kicad"] is False
        assert items["design_defaults"]["default"] is False and "Power" in items["design_defaults"]["found"]
        assert pl["warnings"] and "Editor preferences" in pl["warnings"][0]
        # --yes: only what leaves KiCad's own files alone
        y = K.defaults(pl, yes=True)
        assert [k for k, v in y.items() if v] == ["recent"], y
        assert all(not v for k, v in y.items() if items[k]["modifies_kicad"])
        d = K.defaults(pl)
        assert d["sym_libs"] and d["api"] and d["hotkeys"] and not d["design_defaults"]
        before9 = _tree(sb.k9)
        before10 = _tree(sb.k10) if sb.k10.exists() else {}
        res = K.apply(y)
        assert list(res["results"]) == ["recent"] and res["results"]["recent"]["added"] == [str(sb.proj)]
        assert _tree(sb.k9) == before9 and (_tree(sb.k10) if sb.k10.exists() else {}) == before10
        assert P.recent() == [str(sb.proj)]
        assert K.undo()["removed"] and P.recent() == []


def test_import_everything_then_undo_restores_byte_for_byte():
    if not _has_kicad():
        return
    with Sandbox(kicad9=True) as sb:
        K.discover()
        assert not sb.k10.exists(), "looking at KiCad must not create its settings folder"
        before9, before10 = _tree(sb.k9), _tree(sb.k10)
        docs = sb.home / "Documents" / "KiCad"
        before_docs = _tree(docs)
        res = K.apply({k: True for k in K.ORDER})
        r = res["results"]
        assert not any("error" in v for v in r.values()), r
        assert _tree(sb.k9) == before9 and _tree(docs / "9.0") == _tree(docs / "9.0"), "the source must only be read"
        # library tables: KiCad's defaults for the new version + the user's own libraries
        sym = (sb.k10 / "sym-lib-table").read_text(encoding="utf-8")
        assert r["sym_libs"]["created"] and r["sym_libs"]["added"] == ["Mine", "Var"]
        assert sym.count("(lib ") > 100 and "KICAD9_SYMBOL_DIR" not in sym and 'my \\"own\\" parts' in sym
        libs = {lib.name: lib for lib in env.lib_tables("sym")}
        assert libs["Mine"].exists and libs["Var"].exists, "the path variable did not come along"
        assert r["fp_libs"]["unresolved"] == ["Gone"] and "does not exist" in r["fp_libs"]["note"]
        # the rest
        assert (sb.k10 / "colors" / "solar.json").exists() and (sb.k10 / "user.hotkeys").read_bytes() == (sb.k9 / "user.hotkeys").read_bytes()
        assert (sb.k10 / "eeschema.json").read_bytes() == (sb.k9 / "eeschema.json").read_bytes()
        assert (docs / sb.k10.name / "scripting" / "plugins" / "myplug" / "__init__.py").exists()
        common = json.loads((sb.k10 / "kicad_common.json").read_text(encoding="utf-8"))
        assert common["api"]["enable_server"] is True and common["environment"]["vars"]["MYLIBS"]
        dd = K.design_defaults()
        assert dd["project"] == str(sb.proj) and dd["rules"]["min_clearance"] == 0.15 and len(dd["net_classes"]) == 2
        assert P.recent() == [str(sb.proj)]
        # a second look: nothing left to do, nothing asked again
        again = K.plan()
        assert again["same_version"] and not [i["id"] for i in again["items"] if i["state"] == "importable"], again["items"]
        assert K.apply({k: True for k in K.ORDER})["changed"] == []
        # KiCad (kicad-cli included) re-saves its settings with every default key on exit: still undoable
        common.update({"appearance": {"icon_scale": 0}, "system": {"local_history": True}})
        (sb.k10 / "kicad_common.json").write_text(json.dumps(common, indent=2), encoding="utf-8")
        dry = K.undo(dry_run=True)
        assert dry["kept"] == [] and len(dry["removed"]) >= 7, dry
        assert (sb.k10 / "sym-lib-table").exists(), "a dry run changed something"
        u = K.undo()
        assert u["kept"] == [], u["kept"]
        assert _tree(sb.k10) == before10, {k for k in set(_tree(sb.k10)) ^ set(before10)}
        assert not sb.k10.exists(), "undo must leave no empty settings folder behind"
        assert _tree(docs) == before_docs and not (docs / sb.k10.name).exists()
        assert P.recent() == [] and K.design_defaults() is None
        assert K.undo()["run"] is None and K.history()[-1]["undone"] is True


def test_undo_leaves_files_the_user_changed_since():
    if not _has_kicad():
        return
    with Sandbox(kicad9=True) as sb:
        K.discover()
        K.apply({"other_settings": True, "sym_libs": True, "hotkeys": True, "api": True})
        t = sb.k10 / "sym-lib-table"
        t.write_text(t.read_text(encoding="utf-8").replace('(name "Mine")', '(name "Mine2")'), encoding="utf-8")     # edited in KiCad
        common = json.loads((sb.k10 / "kicad_common.json").read_text(encoding="utf-8"))
        common["api"]["enable_server"] = False                                    # the user turned the API off again
        (sb.k10 / "kicad_common.json").write_text(json.dumps(common), encoding="utf-8")
        u = K.undo()
        kept = {Path(k["path"]).name: k["why"] for k in u["kept"]}
        assert "sym-lib-table" in kept and "changed since the import" in kept["sym-lib-table"], u
        assert t.exists() and "Mine2" in t.read_text(encoding="utf-8")
        assert not (sb.k10 / "user.hotkeys").exists()
        assert K.history()[-1]["undone"] is False
        u2 = K.undo(force=True)                                                   # explicit: overwrite it
        assert u2["kept"] == [] and not t.exists() and K.history()[-1]["undone"] is True


def test_same_version_settings_are_in_use_and_only_copper_items_are_offered():
    if not _has_kicad():
        return
    with Sandbox() as sb:
        ver = env.config_dir().name
        cfg = sb.home / "kicad" / ver
        cfg.mkdir(parents=True, exist_ok=True)          # (kicad-cli may have created it already)
        share = env.kicad().share
        for kind in ("sym", "fp"):
            shutil.copy2(share / "template" / f"{kind}-lib-table", cfg / f"{kind}-lib-table")
        proj = sb.home / "p" / "b.kicad_pro"
        proj.parent.mkdir()
        proj.write_text(json.dumps({"net_settings": {"classes": [{"name": "Default"}]}}), encoding="utf-8")
        (cfg / "kicad.json").write_text(json.dumps({"system": {"file_history": [str(proj)]}}), encoding="utf-8")
        (cfg / "kicad_common.json").write_text(json.dumps({"api": {"enable_server": True}}), encoding="utf-8")
        (cfg / "pcbnew.json").write_text("{}", encoding="utf-8")
        env.refresh()
        pl = K.plan()
        st = {i["id"]: i["state"] for i in pl["items"]}
        assert pl["same_version"] and st["sym_libs"] == "in-use" and st["fp_libs"] == "in-use" and st["api"] == "in-use", st
        assert [k for k, v in st.items() if v == "importable"] == ["recent", "design_defaults"], st
        before = _tree(cfg)
        K.apply({k: True for k in K.ORDER})
        assert _tree(cfg) == before, "nothing in KiCad's settings may change when they are already in use"
        assert P.recent() == [str(proj)] and K.design_defaults()["project"] == str(proj)


def test_running_kicad_blocks_changes_to_its_settings():
    if not _has_kicad():
        return
    with Sandbox(kicad9=True) as sb:
        K.discover()
        before10 = _tree(sb.k10)
        kicad_setup.running_kicad = lambda: ["kicad", "pcbnew"]
        pl = K.plan()
        st = {i["id"]: i for i in pl["items"]}
        assert st["sym_libs"]["state"] == "blocked" and "close it first" in st["sym_libs"]["blocked"]
        assert st["recent"]["state"] == "importable"                      # Copper's own items are not blocked
        res = K.apply({k: True for k in K.ORDER})
        assert "skipped" in res["results"]["sym_libs"] and res["results"]["recent"]["added"]
        assert _tree(sb.k10) == before10
        kicad_setup.running_kicad = lambda: []
        K.apply({"api": True})
        kicad_setup.running_kicad = lambda: ["kicad"]
        assert "refused" in K.undo() and json.loads((sb.k10 / "kicad_common.json").read_text(encoding="utf-8"))["api"]["enable_server"]


def test_running_kicad_detection_is_fast():
    if sys.platform != "win32":
        return
    t = time.perf_counter()
    names = kicad_setup._win_process_names()
    ms = (time.perf_counter() - t) * 1000
    assert names and any(n.endswith(".exe") for n in names) and ms < 1500, (len(names or []), ms)


def test_choose_install_is_remembered_and_exported():
    with Sandbox() as sb:
        prefs.set("kicad_version", "9.0")
        prefs.export_env()
        assert os.environ.get("COPPER_KICAD_VERSION") == "9.0"
        os.environ["COPPER_KICAD_VERSION"] = "10.0"            # an explicit variable wins over the remembered choice
        os.environ.pop("COPPER_KICAD_VERSION_FROM_PREFS", None)
        prefs.export_env()
        assert os.environ["COPPER_KICAD_VERSION"] == "10.0"
        try:
            K.choose_install("99.9")
            raise AssertionError("accepted a KiCad that is not installed")
        except ValueError:
            pass
        assert json.loads((sb.home / ".copper" / "prefs.json").read_text(encoding="utf-8"))["kicad_version"] == "9.0"


# ── CLI ───────────────────────────────────────────────────────────────────
def test_cli_link_list_connect_verify_unlink():
    with Sandbox() as sb:
        r = _cli("link", "--list", "--json")
        d = json.loads(r.stdout)
        assert {a["id"]: a["state"] for a in d["apps"]}["cursor"] == "found" and d["github"]["linked"] is False, r.stdout
        r = _cli("link", "--yes")                                # one action: everything found
        assert r.returncode == 0 and "connected" in r.stdout and "launches in" in r.stdout, r.stdout + r.stderr
        assert (sb.home / ".cursor" / "mcp.json").exists() and (sb.home / ".codex" / "config.toml").exists()
        r = _cli("link", "--verify", "--json")
        d = {a["id"]: a for a in json.loads(r.stdout)["apps"]}
        assert r.returncode == 0 and d["cursor"]["checked"] and d["codex"]["state"] == "connected"
        r = _cli("link", "zed")                                  # a named client that is not installed: still registered
        assert r.returncode == 0 and "Zed" in r.stdout, r.stdout
        r = _cli("link", "nosuch")
        assert r.returncode == 2 and "unknown link target" in r.stdout and "github" in r.stdout
        r = _cli("link", "github")
        assert r.returncode == 1 and "gh auth login" in r.stdout, r.stdout
        r = _cli("link", "--backend", "--json")
        assert json.loads(r.stdout)["id"] == "keyfile"
        r = _cli("link", "--unlink", "--all")
        assert r.returncode == 0 and "removed" in r.stdout
        assert all(a["state"] != "connected" for a in json.loads(_cli("link", "--list", "--json").stdout)["apps"])
        # bare `copper link` with piped "n": shows the state, changes nothing
        r = _cli("link", input="n\n")
        assert r.returncode == 0 and "Connect Copper to" in r.stdout and not (sb.home / ".cursor" / "mcp.json").exists()
        # closed input (a script, CI): nothing is connected by accident, and the way to do it is named
        r = _cli("link", input="")
        assert r.returncode == 0 and "copper link --yes" in r.stdout and not (sb.home / ".cursor" / "mcp.json").exists()
        r = _cli("link", input="y\n")
        assert r.returncode == 0 and (sb.home / ".cursor" / "mcp.json").exists(), r.stdout


def test_cli_setup_yes_is_safe_and_import_is_explicit_and_undoable():
    if not _has_kicad():
        return
    with Sandbox(kicad9=True) as sb:
        before9 = _tree(sb.k9)
        r = _cli("setup", "--yes", "--clients", "none", "--no-doctor")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "nothing in KiCad's own settings is changed" in r.stdout and "Nothing is changed without asking" not in r.stdout
        assert "copper setup --yes --from 9.0 --import" in r.stdout and "Recent projects" in r.stdout
        assert not sb.k10.exists(), "--yes created the new KiCad's settings folder: " + str(sorted(_tree(sb.k10)))
        assert _tree(sb.k9) == before9
        r = _cli("setup", "--yes", "--import", "sym_libs,api", "--clients", "none", "--no-doctor")
        assert r.returncode == 0 and "copper setup --undo" in r.stdout, r.stdout + r.stderr
        assert (sb.k10 / "sym-lib-table").exists() and not (sb.k10 / "fp-lib-table").exists()
        assert json.loads((sb.k10 / "kicad_common.json").read_text(encoding="utf-8"))["api"]["enable_server"] is True
        r = _cli("setup", "--undo", "--json")
        u = json.loads(r.stdout)
        assert r.returncode == 0 and u["kept"] == [] and not (sb.k10 / "sym-lib-table").exists(), u
        assert not sb.k10.exists(), "undo left files Copper created: " + str(sorted(_tree(sb.k10)))
        outs = [_cli("setup", "--undo").stdout for _ in range(3)]       # (the first run's recent-projects import, then nothing)
        assert "recent project" in outs[0] and "nothing to undo" in outs[-1], outs
        assert P.recent() == []
        r = _cli("setup", "--yes", "--import", "bogus", "--clients", "none", "--no-doctor")
        assert "unknown item" in r.stdout


def test_cli_setup_wizard_asks_per_item_with_defaults_shown():
    if not _has_kicad():
        return
    with Sandbox(kicad9=True, clients_found=()) as sb:
        # answers: editor prefs n, path vars y, symbols (Enter = default yes), footprints n, then EOF = defaults
        r = _cli("setup", "--no-doctor", input="n\ny\n\nn\n")
        out = r.stdout
        assert r.returncode == 0, out + r.stderr
        for label in ("Editor preferences", "Path variables", "Symbol libraries", "Footprint libraries", "Colour themes",
                      "Hotkeys", "Plugins", "Recent projects", "Design rules and net classes", "KiCad IPC API"):
            assert label in out, label
        assert out.count("[Y/n]") >= 8 and "[y/N]" in out, "each question must show its default"
        assert "KiCad setting, backed up first" in out and "Copper only" in out
        assert (sb.k10 / "sym-lib-table").exists() and not (sb.k10 / "fp-lib-table").exists()
        assert not (sb.k10 / "eeschema.json").exists(), "answered n"
        assert K.design_defaults() is None, "its default is no"
        assert "▍GitHub\n" in out and "not linked" in out
        assert not re.search(r"\w\((s|es)\)", out), "counts are written out properly, not as theme(s) / class(es)"


def test_cli_uninstall_purge_keeps_foreign_files():
    with Sandbox() as sb:
        home = sb.home / ".copper"
        (home / "cache").mkdir(parents=True)
        (home / "cache" / "x").write_text("1", encoding="utf-8")
        (home / "state.json").write_text("{}", encoding="utf-8")
        (home / "my-notes.txt").write_text("mine", encoding="utf-8")
        r = _cli("uninstall", "--purge", "--dry-run", "--json")
        assert (home / "cache").exists() and str(home / "my-notes.txt") in json.loads(r.stdout)["purge"]["left"]
        r = _cli("uninstall", "--purge", "--json")
        d = json.loads(r.stdout)["purge"]
        assert not (home / "cache").exists() and not (home / "state.json").exists()
        assert (home / "my-notes.txt").read_text(encoding="utf-8") == "mine" and d["left"] == [str(home / "my-notes.txt")]


def test_sandboxed_server_keeps_its_state_in_the_sandbox():
    """Regression: `copper serve` under COPPER_CONFIG_HOME_OVERRIDE (what tests/test_protocol.py starts)
    wrote the active/recent project into the real ~/.copper/state.json."""
    with Sandbox() as sb:
        e = {k: v for k, v in os.environ.items() if k != "COPPER_HOME"}
        code = (f"import sys, os; sys.path.insert(0, {str(SRC)!r})\n"
                "import copper_kicad.mcp.server as s, copper_kicad.cli as c\n"
                "s.main = lambda: print('HOME=' + os.environ.get('COPPER_HOME', '<unset>'))\n"
                "c.main(['serve'])\n"
                "from copper_kicad import project\nprint('STATE=' + str(project.STATE_FILE))\n")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=e, timeout=60)
        want = str(sb.home / ".copper")
        assert f"HOME={want}" in r.stdout and f"STATE={Path(want) / 'state.json'}" in r.stdout, r.stdout + r.stderr


def test_test_modules_isolate_copper_home():
    """Every test module that imports Copper in-process must set COPPER_HOME before the import
    (project.STATE_FILE is fixed at import time), or start servers under the sandbox override."""
    missing = []
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        s = f.read_text(encoding="utf-8")
        m = re.search(r"(?m)^(?:from|import) copper_kicad", s)
        if m is None:
            # subprocess-only (test_protocol): its servers run under the sandbox override, which the
            # sandboxed-server rule above covers
            assert "COPPER_CONFIG_HOME_OVERRIDE" in s or "copper_kicad" not in s, f.name
            continue
        head = s[:m.start()]
        if 'environ.setdefault("COPPER_HOME"' in head or 'environ["COPPER_HOME"]' in head:
            continue
        missing.append(f.name)
    # Every module is isolated now; a new one must add, before its first copper_kicad import:
    #     os.environ.setdefault("COPPER_HOME", tempfile.mkdtemp(prefix="copper-test-home-"))
    assert missing == [], missing


def test_windowless_launcher_logs_instead_of_crashing():
    with Sandbox() as sb:
        boot = (f"import sys; sys.path.insert(0, {str(SRC)!r}); sys.stdout = sys.stderr = sys.stdin = None\n"
                "from copper_kicad.cli import main\nrc = main(sys.argv[1:])\nimport os; os._exit(rc)\n")
        for args, want in ((["status"], 0), (["doctor", "--offline", "--fast"], 0), (["install", "--client", "all", "--dry-run"], 0),
                           (["link", "--list"], 0), (["setup", "--undo"], 0)):
            r = subprocess.run([sys.executable, "-c", boot, *args], capture_output=True, text=True, timeout=120,
                               env=dict(os.environ, COPPER_NO_BROWSER="1"))
            assert r.returncode == want, (args, r.returncode, r.stdout, r.stderr)
        log = (sb.home / ".copper" / "logs" / "copperw.log").read_text(encoding="utf-8")
        assert "copperw status" in log and "AI apps" in log and len(log) > 500, log[:300]
        # no console and no command must never become an MCP server: it serves the setup page
        p = subprocess.Popen([sys.executable, "-c", boot], env=dict(os.environ, COPPER_NO_BROWSER="1"))
        try:
            info = None
            for _ in range(80):
                info = onboard.running()
                if info:
                    break
                time.sleep(0.25)
            assert info, "bare windowless launch did not serve the setup page"
            req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/done", data=b"{}", method="POST",
                                         headers={"X-Copper-Token": info["token"], "Content-Type": "application/json"})
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5).read()
            assert p.wait(timeout=20) == 0
        finally:
            if p.poll() is None:
                p.kill()


# ── onboarding page ───────────────────────────────────────────────────────
class _Page:
    def __init__(self, app):
        self.app = app
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, body=None, token="good", headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if token == "good":
            h["X-Copper-Token"] = self.app.token
        elif token:
            h["X-Copper-Token"] = token
        req = urllib.request.Request(f"http://127.0.0.1:{self.app.port}{path}", method=method, headers=h,
                                     data=json.dumps(body or {}).encode() if method == "POST" else None)
        try:
            with self.opener.open(req, timeout=120) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def json(self, method, path, body=None):
        code, raw, _ = self.call(method, path, body)
        assert code == 200, (path, code, raw[:300])
        return json.loads(raw)

    def job(self, job_id, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            j = self.json("GET", f"/api/jobs/{job_id}")
            if j["done"]:
                return j
            time.sleep(0.2)
        raise AssertionError(f"job {job_id} did not finish")


def test_onboarding_gate_and_page():
    with Sandbox():
        app = onboard.Onboard()
        app.start()
        try:
            pg = _Page(app)
            assert pg.call("GET", "/", token=None)[0] == 403                                   # no token
            assert pg.call("GET", "/", token="wrong")[0] == 403                                # wrong token
            code, raw, hdr = pg.call("GET", "/")
            assert code == 200                                                                   # good token
            assert pg.call("GET", "/", headers={"Host": "evil.example"})[0] == 403             # DNS rebinding
            assert pg.call("POST", "/api/heartbeat", headers={"Origin": "http://evil.example"})[0] == 403   # cross-origin
            assert pg.call("POST", "/api/heartbeat")[0] == 200
            assert pg.call("GET", f"/?t={app.token}", token=None)[0] == 200                    # the URL the browser opens
            big = urllib.request.Request(f"http://127.0.0.1:{app.port}/api/heartbeat", method="POST", data=b"x" * 20000,
                                         headers={"X-Copper-Token": app.token})
            try:
                pg.opener.open(big, timeout=10)
                raise AssertionError("oversized body accepted")
            except urllib.error.HTTPError as e:
                assert e.code == 413
            html = raw.decode("utf-8")
            csp = hdr["Content-Security-Policy"]
            nonce = re.search(r"style-src 'nonce-([^']+)'", csp).group(1)
            assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
            assert html.count(f'nonce="{nonce}"') == 2 and "__NONCE__" not in html and "/*__JS__*/" not in html
            assert hdr["X-Frame-Options"] == "DENY" and hdr["Cache-Control"] == "no-store"
            assert not re.search(r"\sstyle\s*=", html), "inline style attributes are blocked by the CSP"
            assert "#C8783C" in html and "#F7F5F0" in html, "brand colours: copper accent on warm paper"
            assert not re.search(r"""(?:src|href)\s*=\s*["']https?://""", html.split("<script")[0]), "the page must load nothing remote"
            assert "innerHTML" not in html, "page text must be set as text, never as HTML"
            for sec in ("s-kicad", "s-import", "s-apps", "s-github", "s-router", "s-test"):
                assert f'id="{sec}"' in html
            assert app.token not in html
        finally:
            app.close()
        assert not onboard.info_path().exists()


def test_onboarding_walkthrough_import_connect_test_undo():
    if not _has_kicad():
        return
    with Sandbox(kicad9=True) as sb:
        assert onboard.running() is None
        app = onboard.Onboard()
        app.start()
        try:
            assert onboard.running()["port"] == app.port                      # a second `copper setup` re-uses it
            pg = _Page(app)
            t = time.perf_counter()
            st = pg.json("GET", "/api/state")
            first = time.perf_counter() - t
            assert st["kicad"]["found"] and st["kicad"]["sources"] == ["9.0"] and st["import"]["source"] == "9.0"
            assert {a["id"]: a["state"] for a in st["apps"]}["cursor"] == "found"
            assert st["github"]["linked"] is False and st["github"]["how"] == "none" and "gh auth login" in st["github"]["explain"]
            assert st["import"]["defaults"]["sym_libs"] is True and st["import"]["defaults"]["design_defaults"] is False
            t = time.perf_counter()
            pg.json("GET", "/api/state")
            assert time.perf_counter() - t < 2.5, f"state took {time.perf_counter() - t:.2f}s (first {first:.2f}s)"
            # import: only what was ticked
            code, raw, _ = pg.call("POST", "/api/import/apply", {"choices": "all"})
            assert code == 400
            r = pg.json("POST", "/api/import/apply", {"choices": {"sym_libs": True, "themes": True, "hotkeys": False, "bogus": True}})
            assert sorted(r["result"]["results"]) == ["sym_libs", "themes"], r["result"]["results"]
            assert (sb.k10 / "sym-lib-table").exists() and not (sb.k10 / "user.hotkeys").exists()
            assert r["state"]["undo"] and r["state"]["undo"][0]["items"] == ["sym_libs", "themes"]
            # connect + launch check
            assert pg.call("POST", "/api/apps/connect", {"ids": ["cursor", "rm -rf"]})[0] == 400
            j = pg.job(pg.json("POST", "/api/apps/connect", {"ids": ["cursor", "codex"]})["job"])
            assert j["error"] is None and j["result"]["ok"] and len(j["result"]["verify"]) == 2, j
            assert all(v["ok"] for v in j["result"]["verify"]) and j["result"]["restart"]["cursor"]
            # the test call
            j = pg.job(pg.json("POST", "/api/test")["job"])
            assert j["error"] is None and j["result"]["tools"] >= 20 and "Copper" in j["result"]["reply"], j
            # GitHub with no route: an explanation, never a crash
            j = pg.job(pg.json("POST", "/api/github/link")["job"])
            assert j["error"] and "gh auth login" in (j["fix"] or ""), j
            # undo and disconnect
            r = pg.json("POST", "/api/import/undo")
            assert r["result"]["kept"] == [] and not (sb.k10 / "sym-lib-table").exists() and r["state"]["undo"] == []
            r = pg.json("POST", "/api/apps/disconnect", {"ids": ["cursor"]})
            assert {a["id"]: a["state"] for a in r["state"]["apps"]}["cursor"] == "found"
            assert pg.call("GET", "/api/nope")[0] == 404 and pg.call("GET", "/api/jobs/zzz")[0] == 404
        finally:
            app.close()


def test_onboarding_device_sign_in_never_returns_the_token():
    httpd, base = _stub()
    saved = (github.DEVICE_CODE_URL, github.TOKEN_URL, github.API_USER, time.sleep)
    try:
        with Sandbox() as sb:
            os.environ["COPPER_GITHUB_CLIENT_ID"] = "cid-xyz"
            github.device_start.__defaults__ = (github.DEFAULT_SCOPE, base + "/login/device/code")
            poll_defaults = github.device_poll.__defaults__
            github.device_poll.__defaults__ = (base + "/login/oauth/access_token", lambda s: time_sleep_short(), None)
            fin_defaults = github._finish_device.__defaults__
            github._finish_device.__defaults__ = (base + "/user",)
            _GitHubStub.script = ["authorization_pending", "token"]
            app = onboard.Onboard()
            app.start()
            try:
                pg = _Page(app)
                assert pg.json("GET", "/api/state")["github"]["how"] == "device"
                jid = pg.json("POST", "/api/github/link")["job"]
                seen = []
                j = None
                end = time.time() + 30
                while time.time() < end:
                    code, raw, _ = pg.call("GET", f"/api/jobs/{jid}")
                    seen.append(raw)
                    j = json.loads(raw)
                    if j["done"]:
                        break
                    time.sleep(0.05)
                assert j and j["done"] and j["error"] is None, j
                assert any(b"WDJB-MJHT" in s for s in seen), "the page never got the code to show"
                assert j["result"]["via"] == "device" and j["result"]["user"] == "octo-device"
                st_raw = pg.call("GET", "/api/state")[1]
                tok = _GitHubStub.token.encode()
                assert all(tok not in s for s in seen) and tok not in st_raw, "a route returned the token"
                assert b"DEV-c0de" not in b"".join(seen), "a route returned the device code"
                assert json.loads(st_raw)["github"]["linked"] is True
                assert store.get_store().get("github") == _GitHubStub.token
                r = pg.json("POST", "/api/github/unlink")
                assert r["state"]["github"]["linked"] is False and store.get_store().get("github") is None
            finally:
                app.close()
                github.device_start.__defaults__ = (github.DEFAULT_SCOPE, saved[0])
                github.device_poll.__defaults__ = poll_defaults
                github._finish_device.__defaults__ = fin_defaults
            assert not any(tok in f.read_bytes() for f in (sb.home / ".copper").rglob("*") if f.is_file())
    finally:
        httpd.shutdown()
        httpd.server_close()


def time_sleep_short():
    time.sleep(0.05)


def test_onboarding_lifetime_rules():
    with Sandbox():
        saved = onboard.BYE_GRACE_S
        onboard.BYE_GRACE_S = 0.6
        try:
            # Done
            app = onboard.Onboard()
            app.start()
            pg = _Page(app)
            assert json.loads(pg.call("POST", "/api/done")[1]) == {"bye": True}
            assert app.wait() == "done"
            app.close()
            # tab closed: stops after the grace period …
            app = onboard.Onboard()
            app.start()
            pg = _Page(app)
            pg.call("POST", f"/api/bye?t={app.token}", token=None)
            t = time.monotonic()
            assert app.wait() == "tab closed" and 0.4 <= time.monotonic() - t < 3
            app.close()
            # … unless the page comes back (a reload)
            app = onboard.Onboard(idle_exit_s=2)
            app.start()
            pg = _Page(app)
            pg.call("POST", "/api/bye")
            pg.call("POST", "/api/heartbeat")
            t = time.monotonic()
            assert app.wait() == "idle" and time.monotonic() - t >= 1.5, "a reload must cancel the goodbye"
            app.close()
            assert not onboard.info_path().exists() and onboard.running() is None
        finally:
            onboard.BYE_GRACE_S = saved


# ── demo mode (`copper setup --demo`) ─────────────────────────────────────
_DEMO_PROBE = r"""
import json, os, sys, tempfile
sys.path.insert(0, SRC)
from copper_kicad.install import demo
d = demo.activate(DEMO)
from pathlib import Path
from copper_kicad import project as P
from copper_kicad.install import clients, freerouting, kicad_import, kicad_setup, onboard, prefs
from copper_kicad.kicad import env
from copper_kicad.link import store
paths = {"home": Path.home(), "clients.home": clients.home(), "appdata": clients.appdata(),
         "localappdata": clients.localappdata(), "xdg": clients.xdg_config(), "copper_home": clients.copper_home(),
         "kicad_config": env.config_root(), "documents": env.documents_dir(), "kicad_user_data": env.user_data_dir(),
         "import_docs": kicad_import.documents_root(), "tools": freerouting.default_tools_dir(),
         "shared_tools": freerouting.shared_tools_dir(), "temp": tempfile.gettempdir(), "prefs": prefs.path(),
         "state": P.STATE_DIR, "onboard": onboard.info_path()}
try:
    freerouting.install(progress=None)
    refused = False
except demo.DownloadsOff:
    refused = True
app = onboard.Onboard()
try:
    st = json.dumps(app.state())
finally:
    app.httpd.server_close()
print(json.dumps({"demo": str(d), "paths": {k: str(v) for k, v in paths.items()}, "store": type(store.get_store()).__name__,
                  "refused": refused, "running": kicad_setup.running_kicad(), "state": st,
                  "seeded": sorted(p.name for p in d.iterdir())}))
"""


def _demo_probe(demo_dir: str) -> dict:
    code = _DEMO_PROBE.replace("SRC", repr(str(SRC))).replace("DEMO", repr(demo_dir))
    e = {k: v for k, v in os.environ.items() if k not in _ENV}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", timeout=240,
                       env=dict(e, COPPER_NO_BROWSER="1", PYTHONIOENCODING="utf-8"), cwd=tempfile.gettempdir())
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_demo_mode_keeps_every_location_inside_the_demo_home():
    root = Path(tempfile.mkdtemp(prefix="copper-demo-test-"))
    try:
        d = root / "demo home"
        out = _demo_probe(str(d))
        home = Path(out["demo"])
        assert home == d.resolve()
        for k, v in out["paths"].items():
            assert Path(v).resolve().is_relative_to(home), (k, v)           # nothing outside the demo home
        assert out["store"] == "MemoryStore" and out["refused"] is True and out["running"] == []
        # an empty folder is seeded: AI apps installed but not connected, an earlier KiCad's settings
        assert {".claude.json", ".codex", ".cursor", "AppData", "Documents"} <= set(out["seeded"]), out["seeded"]
        st = json.loads(out["state"])
        assert st["demo"] is True
        if st["kicad"]["found"]:
            assert st["kicad"]["sources"] == ["9.0"] and st["import"]["source"] == "9.0"
        found = {a["id"] for a in st["apps"] if a["state"] == "found"}
        assert {"claude-desktop", "claude-code", "codex", "cursor"} <= found, found
        # shown as a generic home: neither the demo folder nor the real home appears on the page
        raw = out["state"]
        for real in (str(home), str(home).replace("\\", "/"), str(Path.home())):
            assert real.lower() not in raw.lower() and json.dumps(real)[1:-1].lower() not in raw.lower(), real
        assert ("C:\\\\Users\\\\you" if sys.platform == "win32" else "/you") in raw
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_demo_cli_setup_writes_only_inside_the_demo_home():
    root = Path(tempfile.mkdtemp(prefix="copper-demo-cli-"))
    try:
        d = root / "h"
        e = {k: "" for k in _ENV}
        r = _cli("setup", "--demo", str(d), "--yes", "--clients", "cursor,codex", "--no-doctor", env_extra=e)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "demo mode" in r.stderr and str(d) not in r.stdout, r.stdout
        cfg = json.loads((d / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
        assert "copper" in cfg["mcpServers"]
        assert "[mcp_servers.copper]" in (d / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert (d / ".cursor" / "mcp.json.copper-backup").exists()
        assert (d / ".copper" / "clients.json").exists()
        # --demo is listed in --help for the commands that take it
        h = _cli("setup", "--help")
        assert "--demo [DIR]" in h.stdout and "C:\\Users\\you" in h.stdout
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_demo_dir_argument_parsing():
    from copper_kicad import cli
    assert cli._demo_dir(["setup", "--web"]) is None
    assert cli._demo_dir(["setup", "--demo"]) == "" and cli._demo_dir(["setup", "--demo", "--web"]) == ""
    assert cli._demo_dir(["setup", "--demo", "D:/x", "--web"]) == "D:/x" and cli._demo_dir(["link", "--demo=D:/y"]) == "D:/y"
    assert cli._demo_dir(["serve", "--demo", "x"]) is None and cli._demo_dir(["render", "--demo", "x"]) is None
    assert cli._demo_dir(["setup", "--", "--demo", "x"]) is None


def test_path_alias_keeps_separators_and_whole_names():
    try:
        redact.clear()
        redact.alias(r"C:\t\demo\home", r"C:\Users\you")
        assert redact.scrub(r"at C:\t\demo\home\.cursor\mcp.json.") == r"at C:\Users\you\.cursor\mcp.json."
        assert redact.scrub("c:/T/demo/home/x") == "C:/Users/you/x"
        assert redact.scrub(json.dumps(r"C:\t\demo\home\a")) == json.dumps(r"C:\Users\you\a")
        assert redact.scrub(r"C:\t\demo\homework\a") == r"C:\t\demo\homework\a"          # whole folder names only
        redact.register("ghp_" + "x" * 30)
        assert redact.scrub("ghp_" + "x" * 30 + r" C:\t\demo\home") == r"[redacted] C:\Users\you"
        redact.clear()
        redact.alias("/home/clay", "/home/you")
        assert redact.scrub("/home/clay/.config and /home/clayton") == "/home/you/.config and /home/clayton"
    finally:
        redact.clear()


def test_documents_folder_follows_the_sandbox():
    """SHGetFolderPathW ignores USERPROFILE: in the sandbox the Documents folder must come from the fake home."""
    with Sandbox() as sb:
        os.environ.pop("KICAD_DOCUMENTS_HOME", None)
        assert env.documents_dir() == sb.home / "Documents"
        assert env.user_data_dir() == sb.home / "Documents" / "KiCad"


def test_onboarding_page_is_a_stepper_with_a_ready_screen():
    html = onboard.page_html("n0nce").decode("utf-8")
    for sec in ("s-kicad", "s-import", "s-apps", "s-github", "s-router", "s-test", "s-done"):
        assert f'id="{sec}"' in html
    assert 'aria-live="polite"' in html and 'role="alert"' in html, "progress and errors are announced"
    assert "prefers-reduced-motion" in html and "max-width: 860px" in html
    js = (SRC / "copper_kicad" / "install" / "static" / "onboard.js").read_text(encoding="utf-8")
    assert "S.example" in js and "Copy prompt" in js, "the ready screen shows what to try first"
    for state in ("KiCad isn't installed yet", "can view and check designs, but not edit them", "No AI app found on this computer"):
        assert state in js, state
    assert ".innerHTML" not in js and "insertAdjacentHTML" not in js
    # light, plain page: no eyebrow/kicker labels, no uppercase tags, no "01 / 06" counters
    assert 'content="light"' in html and "color-scheme:light" in html
    assert "kicker" not in html and "text-transform:uppercase" not in html and "Try this first" not in js
    assert not re.search(r'"0" \+ \(', js), "no zero-padded step numbers"
    # two animations (progress line, the trace drawn on the ready screen) plus the loading spinner; nothing else
    assert set(re.findall(r"@keyframes (\w+)", html)) == {"draw", "land", "spin"}, re.findall(r"@keyframes (\w+)", html)
    assert "AI client" not in js and "AI client" not in html and "stopped answering (" not in js
    for fr, want in (({"ok": False, "available": True, "first_use_downloads": ["Java 25 runtime (~50 MB)", "Freerouting 2.2.3 (~20 MB)"]},
                      "about 70 MB"), ({"ok": True, "version": "2.2.3", "java_version": "25"}, "is ready")):
        line = onboard.router_summary(fr)
        assert want in line and "pcb_route" not in line and line.endswith("."), line


# ── doctor ────────────────────────────────────────────────────────────────
def test_doctor_reports_links_and_clients_that_do_not_start():
    from copper_kicad.install import doctor
    with Sandbox() as sb:
        cs = {c.id: c for c in doctor.check_links(launch=False)}
        assert cs["link_github"].status == "skip" and cs["link_github"].group == "links"
        apps.connect("cursor", verify_launch=False)
        cs = {c.id: c for c in doctor.check_links(launch=True)}
        assert cs["client_launch"].status == "ok" and "tools" in cs["client_launch"].detail, cs
        cfg = sb.home / ".cursor" / "mcp.json"
        d = json.loads(cfg.read_text(encoding="utf-8"))
        d["mcpServers"]["copper"]["args"] = ["-c", "import sys; from copper_kicad.cli import main; sys.exit(main(['serve']))  # sys.path.insert(0, 'x')"]
        d["mcpServers"]["copper"]["command"] = sys.executable
        cfg.write_text(json.dumps(d), encoding="utf-8")
        if "kicad" not in sys.executable.lower():
            return                                   # only KiCad's python is guaranteed to lack copper_kicad on its path
        cs = {c.id: c for c in doctor.check_links(launch=True)}
        assert cs["launch_cursor"].status == "fail" and cs["launch_cursor"].fix == "copper link cursor", cs


# ── installer files ───────────────────────────────────────────────────────
def test_windows_installer_opens_the_setup_page():
    nsi = (ROOT / "installer" / "windows" / "copper.nsi").read_text(encoding="utf-8")
    assert "MUI_FINISHPAGE_RUN_FUNCTION RunSetup" in nsi
    fn = nsi[nsi.index("Function RunSetup"):]
    fn = fn[:fn.index("FunctionEnd")]
    assert '\\bin\\copperw.exe" setup --web' in fn and "copper-doctor.cmd" in fn, "windowless launcher, with a fallback"
    assert 'Copper setup.lnk" "$INSTDIR\\bin\\copperw.exe" "setup --web"' in nsi
    assert "setup --yes --clients none --api skip --no-doctor --quiet" in nsi
    py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'\[project\.gui-scripts\][^\[]*copperw\s*=\s*"copper_kicad\.cli:main"', py)
    for f in ("onboard.html", "onboard.css", "onboard.js"):
        assert (SRC / "copper_kicad" / "install" / "static" / f).exists()


def test_unix_install_paths_exist_and_say_what_is_untested():
    sh = (ROOT / "installer" / "install.sh").read_text(encoding="utf-8")
    assert sh.startswith("#!/bin/sh") and "set -eu" in sh and "umask 077" in sh
    assert "--proto '=https'" in sh and "SHA256SUMS.txt" in sh and "checksum mismatch" in sh
    assert 'id -u' in sh and "COPPER_ALLOW_ROOT" in sh and "mktemp -d" in sh and "trap " in sh
    code = "\n".join(ln for ln in sh.split("set -eu", 1)[1].splitlines() if not ln.lstrip().startswith("#"))
    assert "| sh" not in code, "nothing downloaded is piped straight into a shell"
    assert "sudo " not in code.replace("with sudo", "")
    assert "copper-setup.desktop" in sh and "setup --web" in sh
    assert "has NOT been run" in sh and "macOS or Linux" in sh
    rb = (ROOT / "installer" / "macos" / "copper-kicad.rb").read_text(encoding="utf-8")
    assert "class CopperKicad < Formula" in rb and "UNTESTED" in rb and 'depends_on "pillow"' in rb and "copper setup" in rb
    ai = (ROOT / "installer" / "linux" / "appimage" / "build-appimage.sh").read_text(encoding="utf-8")
    assert ai.startswith("#!/bin/sh") and "UNTESTED" in ai and "appimagetool" in ai and "COPPER_LAUNCHER" in ai
    doc = (ROOT / "installer" / "linux" / "README.md").read_text(encoding="utf-8")
    assert "pipx install" in doc and "uv tool install" in doc and "None of them has been run end to end" in doc
    bash = shutil.which("bash") or shutil.which("sh")
    if bash:
        for f in (ROOT / "installer" / "install.sh", ROOT / "installer" / "linux" / "appimage" / "build-appimage.sh"):
            r = subprocess.run([bash, "-n", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, (f.name, r.stderr)


def test_user_facing_text_is_vendor_neutral_about_accounts():
    """Copper links AI *clients* (their config files), never an AI vendor's account or API key."""
    src = "".join((SRC / "copper_kicad" / p).read_text(encoding="utf-8")
                  for p in ("link/apps.py", "link/github.py", "link/store.py", "install/linkcli.py", "install/onboard.py",
                            "install/static/onboard.js", "install/static/onboard.html"))
    assert not re.search(r"(?i)anthropic_api_key|openai_api_key|gemini_api_key|model api key", src)
    assert "does not link AI-vendor accounts" in src


def test_hints_name_npx_when_started_through_the_npm_launcher():
    """Under `npx -y copper-kicad` there is no `copper` on PATH: every hint must name the npx form."""
    import io
    from copper_kicad import cli, invocation
    old = os.environ.pop("COPPER_NPM_LAUNCHER", None)
    try:
        assert invocation.cmds("run `copper doctor`") == "run `copper doctor`"      # normal install: unchanged
        os.environ["COPPER_NPM_LAUNCHER"] = "1.1.0"
        c = invocation.cmds
        assert c("run `copper doctor`") == "run `npx -y copper-kicad doctor`"
        assert c("Put everything back with: copper setup --undo") == \
            "Put everything back with: npx -y copper-kicad setup --undo"
        assert c("fix: copper install --client claude-desktop") == "fix: npx -y copper-kicad install --client claude-desktop"
        assert c("\x1b[38;2;1;2;3mcopper link github\x1b[0m") == "\x1b[38;2;1;2;3mnpx -y copper-kicad link github\x1b[0m"
        for same in ("Copper setup closed", "copper pour on B.Cu", "C:/x/copper setup.txt", "the copper-kicad setup",
                     "npx -y copper-kicad setup", "copper_kicad.cli", "copper serves"):
            assert c(same) == same, same
        assert invocation.cmds_obj({"a": ["copper doctor", 3], "b": None}) == {"a": ["npx -y copper-kicad doctor", 3], "b": None}
        buf = io.StringIO()
        cli.UI(buf, color=False).line("fail", "Claude Desktop", "not connected", "copper install --client claude-desktop")
        assert "npx -y copper-kicad install --client claude-desktop" in buf.getvalue(), buf.getvalue()
        html = onboard.page_html("n0nce").decode("utf-8")
        assert "<code>npx -y copper-kicad setup</code>" in html and "copper setup<" not in html
    finally:
        os.environ.pop("COPPER_NPM_LAUNCHER", None)
        if old is not None:
            os.environ["COPPER_NPM_LAUNCHER"] = old


# ── runner ────────────────────────────────────────────────────────────────
def _main() -> int:
    only = sys.argv[1:]
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)
             and (not only or any(o in n for o in only))]
    failed = 0
    for n, f in tests:
        t = time.perf_counter()
        try:
            f()
            print(f"PASS {n}  ({time.perf_counter() - t:.1f}s)")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {n}\n{traceback.format_exc()}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
