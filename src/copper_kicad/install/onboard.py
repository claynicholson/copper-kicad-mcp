"""First-run onboarding in the browser: ``copper setup`` / ``copper setup --web``.

A local page in Copper's colours that walks through: KiCad found → what to bring along from it →
AI clients to connect → GitHub (optional) → autorouter download → a real test call.

    serve(open_browser=True, port=0, idle_exit_s=600) -> int       blocking; returns an exit code
    Onboard(port=0)                                                the server object (tests drive it directly)

Lifetime: the server stops on **Done**, 5 s after the tab is closed (``/api/bye`` via ``sendBeacon``,
cancelled by any later request, e.g. a reload), after ``idle_exit_s`` without any request, or after
60 minutes. A second ``copper setup --web`` re-opens the browser on the running instance.

Nothing here returns a secret: the GitHub routes return states, the account name and (during device
sign-in) the short user code the user must type on github.com, never a token.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

from .. import BRAND_ACCENT, BRAND_BG, DISPLAY_NAME, __version__
from .. import invocation, localweb
from ..link import redact
from . import clients

STATIC = Path(__file__).with_name("static")
HARD_CAP_S = 3600
BYE_GRACE_S = 5
EXAMPLE_PROMPT = "Use Copper to design a USB-C powered ESP32-C3 board with a status LED."


def page_html(nonce: str) -> bytes:
    html = (STATIC / "onboard.html").read_text(encoding="utf-8")
    css = (STATIC / "onboard.css").read_text(encoding="utf-8")
    js = (STATIC / "onboard.js").read_text(encoding="utf-8")
    page = (html.replace("/*__CSS__*/", css).replace("/*__JS__*/", js).replace("__NONCE__", nonce)
            .replace("__VERSION__", __version__).replace("__NAME__", DISPLAY_NAME)
            .replace("__ACCENT__", BRAND_ACCENT).replace("__BG__", BRAND_BG))
    return invocation.cmds(page).encode("utf-8")      # through npx, hints read `npx -y copper-kicad setup`


# ── single instance ───────────────────────────────────────────────────────
def info_path() -> Path:
    return clients.copper_home() / "onboard.json"


def _alive(info: dict) -> bool:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{int(info['port'])}/api/ping",
                                     headers={"X-Copper-Token": str(info["token"])})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=1.5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def running() -> dict | None:
    try:
        info = json.loads(info_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(info, dict) and info.get("port") and info.get("token") and _alive(info):
        return {**info, "url": f"http://127.0.0.1:{info['port']}/?t={info['token']}"}
    return None


# ── jobs (long actions the page polls) ────────────────────────────────────

def download_mb(fr: dict) -> int | None:
    """Total of the "~N MB" sizes the first route would download (None when unknown or nothing is missing)."""
    import re
    sizes = [int(m) for x in (fr.get("first_use_downloads") or []) for m in re.findall(r"~(\d+) MB", str(x))]
    return sum(sizes) or None


def router_summary(fr: dict) -> str:
    """The autorouter's state in one plain sentence for the setup page (doctor keeps the technical line)."""
    if fr.get("ok"):
        java = f" with Java {fr['java_version']}" if fr.get("java_version") else ""
        return f"Freerouting {fr.get('version') or ''}{java} is ready.".replace("  ", " ")
    if fr.get("available"):
        mb = download_mb(fr)
        return ("Not downloaded yet. Copper fetches it" + (f" (about {mb} MB)" if mb else "") +
                " the first time your AI routes a board.")
    return (fr.get("problem") or "Not installed") + "."

class Job:
    def __init__(self, kind: str):
        self.id = f"{kind}-{int(time.time() * 1000) % 10 ** 9}"
        self.kind = kind
        self.done = False
        self.error: str | None = None
        self.fix: str | None = None
        self.message = ""
        self.lines: list[str] = []
        self.result = None
        self.extra: dict = {}
        self.cancel = False

    def say(self, m: str):
        self.message = redact.scrub(str(m))
        self.lines = (self.lines + [self.message])[-12:]

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "done": self.done, "error": self.error, "fix": self.fix,
                "message": self.message, "lines": self.lines, "result": self.result, **self.extra}


class Onboard:
    def __init__(self, port: int = 0, idle_exit_s: int = 600, launcher: str | None = None):
        self.httpd, self.token = localweb.make_server(_Handler, port)
        self.httpd.app = self  # type: ignore[attr-defined]
        self.port = self.httpd.port
        self.idle_exit_s = idle_exit_s
        self.launcher = launcher
        self.started = time.monotonic()
        self.last = time.monotonic()
        self.bye_at: float | None = None
        self.finished = threading.Event()
        self.reason = ""
        self.jobs: dict[str, Job] = {}
        self.source: str | None = None
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    # ── lifetime ──────────────────────────────────────────────────────────
    def touch(self):
        self.last = time.monotonic()
        self.bye_at = None

    def start(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="copper-onboard", daemon=True)
        self.thread.start()
        p = info_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"port": self.port, "token": self.token, "pid": os.getpid()}, f)
        except OSError:
            pass

    def stop(self, reason: str = ""):
        self.reason = self.reason or reason
        self.finished.set()

    def close(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        finally:
            try:
                cur = json.loads(info_path().read_text(encoding="utf-8"))
                if cur.get("token") == self.token:
                    info_path().unlink()
            except (OSError, ValueError):
                pass

    def wait(self) -> str:
        """Block until one of the lifetime rules fires; returns the reason."""
        while not self.finished.wait(0.25):
            now = time.monotonic()
            busy = any(not j.done for j in self.jobs.values())
            if self.bye_at is not None and now >= self.bye_at and not busy:
                self.stop("tab closed")
            elif now - self.last > self.idle_exit_s and not busy:
                self.stop("idle")
            elif now - self.started > HARD_CAP_S:
                self.stop("time limit")
        return self.reason

    # ── state ─────────────────────────────────────────────────────────────
    def state(self) -> dict:
        from ..link import apps, github, store
        from . import freerouting, kicad_import
        from . import demo
        out: dict = {"version": __version__, "example": EXAMPLE_PROMPT, "demo": demo.active() is not None}
        try:
            disc = kicad_import.discover()
            out["kicad"] = {"found": bool(disc["active"]), "active": disc["active"], "installs": disc["installs"],
                            "preferred": disc["preferred_version"], "running": disc["kicad_running"],
                            "configs": [{"version": c["version"], "sym": c["sym"]["libs"], "fp": c["fp"]["libs"],
                                         "custom": len(c["sym"]["custom"]) + len(c["fp"]["custom"]),
                                         "fresh": c["fresh"]} for c in disc["configs"]],
                            "sources": disc["sources"], "target": disc["target"]}
            src = self.source if self.source in disc["sources"] else None
            pl = kicad_import.plan(src)
            pl["defaults"] = kicad_import.defaults(pl)
            out["import"] = pl
            out["undo"] = [{"id": r["id"], "at": r["at"], "items": r.get("items", [])}
                           for r in kicad_import.history() if not r.get("undone")][-1:]
        except Exception as e:  # noqa: BLE001
            out["kicad"] = {"found": False, "error": str(getattr(e, "message", e))}
            out["import"] = None
        out["apps"] = apps.status(verify_launch=False)
        out["snippet"] = apps.generic_snippet(self.launcher)
        g = github.gh_status()
        gh = github.status(live=True, gh=g)
        av = github.available(g)
        out["github"] = {"linked": gh["linked"], "via": gh["via"], "user": gh["user"], "detail": gh["detail"],
                         "how": av["how"], "explain": av["explain"]}
        try:
            fr = freerouting.find()
            out["freerouting"] = {"ok": bool(fr.get("ok")), "available": bool(fr.get("available")),
                                  "detail": freerouting.describe(fr), "summary": router_summary(fr),
                                  "mb": download_mb(fr)}
        except Exception as e:  # noqa: BLE001
            out["freerouting"] = {"ok": False, "available": False, "detail": f"{type(e).__name__}: {e}"}
        out["store"] = store.describe()
        out["jobs"] = [j.to_dict() for j in self.jobs.values() if not j.done]
        return redact.scrub_obj(out)

    # ── jobs ──────────────────────────────────────────────────────────────
    def _spawn(self, kind: str, fn) -> Job:
        for j in self.jobs.values():
            if j.kind == kind and not j.done:
                return j
        job = Job(kind)
        self.jobs[job.id] = job

        def run():
            try:
                job.result = redact.scrub_obj(fn(job))
            except Exception as e:  # noqa: BLE001
                job.error = redact.scrub(str(getattr(e, "message", None) or f"{type(e).__name__}: {e}"))
                job.fix = getattr(e, "fix", None)
            finally:
                job.done = True
        threading.Thread(target=run, name=f"copper-onboard-{kind}", daemon=True).start()
        return job

    def job_freerouting(self) -> Job:
        from . import freerouting

        def fn(job):
            r = freerouting.install(progress=job.say)
            return {"ok": bool(r.get("ok")), "detail": router_summary(r)}
        return self._spawn("freerouting", fn)

    def job_test(self) -> Job:
        from ..link import apps

        def fn(job):
            job.say("Starting Copper the way your AI app does…")
            r = apps.test_call(self.launcher)
            if not r["ok"]:
                raise RuntimeError(f"the test call failed at {r['stage']}: {r['detail']}")
            return {"ok": True, "detail": r["detail"], "tools": r["tools"], "reply": (r.get("call") or {}).get("text", "")}
        return self._spawn("test", fn)

    def job_verify(self) -> Job:
        from ..link import apps
        return self._spawn("verify", lambda job: {"apps": apps.status(verify_launch=True)})

    def job_connect(self, ids: list[str]) -> Job:
        from ..link import apps

        def fn(job):
            job.say("Connecting…")
            r = apps.connect(ids, launcher=self.launcher)
            return {"ok": r["ok"], "changes": r["changes"], "verify": r["verify"], "skipped": r["skipped"],
                    "restart": {k: v for k, v in r["restart"].items() if v}, "apps": apps.status()}
        return self._spawn("connect", fn)

    def job_github(self) -> Job:
        from ..link import github, store

        def fn(job):
            av = github.available()
            if av["how"] == "gh":
                return github.link(interactive=False)
            if av["how"] != "device":
                raise github.LinkError("GitHub is not linked.", "no_route", av["explain"])
            cid = github.client_id()
            st = store.get_store()
            info = github.device_start(cid)
            job.extra = {"user_code": info["user_code"], "verification_uri": info["verification_uri"]}
            job.say(f"Enter the code {info['user_code']} at {info['verification_uri']}")
            token = github.device_poll(cid, info, should_stop=lambda: job.cancel or self.finished.is_set())
            return github._finish_device(st, token, github.DEFAULT_SCOPE)
        return self._spawn("github", fn)


class _Handler(localweb.GatedHandler):
    server_version = "CopperSetup"

    @property
    def app(self) -> Onboard:
        return self.server.app  # type: ignore[attr-defined]

    def send_json(self, obj, code: int = 200):
        super().send_json(invocation.cmds_obj(obj), code)

    def route_get(self, path: str, q: dict):
        app = self.app
        if path == "/api/ping":
            return {"ok": True}
        app.touch()
        if path == "/":
            self.send_page(page_html)
            return None
        if path == "/api/state":
            return app.state()
        if path.startswith("/api/jobs/"):
            job = app.jobs.get(path.rsplit("/", 1)[1])
            if not job:
                raise localweb.HttpError(404, "no such job")
            return job.to_dict()
        raise localweb.HttpError(404, "not found")

    def route_post(self, path: str, body: dict):
        from ..link import apps, github
        from . import kicad_import
        app = self.app
        if path == "/api/bye":
            app.bye_at = time.monotonic() + BYE_GRACE_S
            return {}
        app.touch()
        if path == "/api/heartbeat":
            return {}
        if path == "/api/done":
            app.stop("done")
            return {"bye": True}
        if path == "/api/kicad/choose":
            v = body.get("version")
            try:
                kicad_import.choose_install(str(v) if v else None)
            except ValueError as e:
                raise localweb.HttpError(400, str(e)) from None
            return app.state()
        if path == "/api/import/plan":
            src = body.get("source")
            app.source = str(src) if src else None
            return app.state()
        if path == "/api/import/apply":
            ch = body.get("choices")
            if not isinstance(ch, dict):
                raise localweb.HttpError(400, "choices must be an object of item id → true/false")
            choices = {str(k): v is True for k, v in ch.items() if k in kicad_import.ORDER}
            res = kicad_import.apply(choices, source=app.source)
            return {"result": res, "state": app.state()}
        if path == "/api/import/undo":
            return {"result": kicad_import.undo(), "state": app.state()}
        if path == "/api/apps/connect":
            ids = body.get("ids")
            if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i in clients.BY_ID for i in ids):
                raise localweb.HttpError(400, "ids must be a list of client ids")
            return {"job": app.job_connect(ids).id}
        if path == "/api/apps/disconnect":
            ids = body.get("ids")
            if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i in clients.BY_ID for i in ids):
                raise localweb.HttpError(400, "ids must be a list of client ids")
            return {"result": apps.disconnect(ids), "state": app.state()}
        if path == "/api/apps/verify":
            return {"job": app.job_verify().id}
        if path == "/api/github/link":
            return {"job": app.job_github().id}
        if path == "/api/github/cancel":
            for j in app.jobs.values():
                if j.kind == "github":
                    j.cancel = True
            return {}
        if path == "/api/github/unlink":
            return {"result": github.unlink(), "state": app.state()}
        if path == "/api/freerouting":
            return {"job": app.job_freerouting().id}
        if path == "/api/test":
            return {"job": app.job_test().id}
        if path == "/api/doctor":
            from . import doctor
            cs = doctor.run(fast=False, network=bool(body.get("network", True)))
            return redact.scrub_obj(doctor.summarize(cs))
        raise localweb.HttpError(404, "not found")


# ── entry point ───────────────────────────────────────────────────────────
def open_browser(url: str) -> bool:
    if os.environ.get("COPPER_NO_BROWSER"):
        return False
    try:
        import webbrowser
        return bool(webbrowser.open(url, new=2))
    except Exception:  # noqa: BLE001
        return False


def _message_box(text: str, title: str = DISPLAY_NAME):
    if sys.platform == "win32" and not os.environ.get("COPPER_NO_BROWSER"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
        except Exception:  # noqa: BLE001
            pass


def serve(open_browser_: bool = True, port: int = 0, idle_exit_s: int = 600, launcher: str | None = None,
          out=None, windowless: bool = False) -> int:
    """`copper setup --web`. Blocks until the page is done; prints (or shows) the URL."""
    out = out or sys.stdout
    live = running()
    if live:
        print(f"Copper setup is already open:\n  {live['url']}", file=out, flush=True)
        if open_browser_:
            open_browser(live["url"])
        return 0
    try:
        app = Onboard(port, idle_exit_s, launcher)
    except OSError as e:
        print(invocation.cmds(f"Copper setup could not open a local port ({e}). Use `copper setup --tty` instead."),
              file=sys.stderr)
        return 1
    app.start()
    opened = open_browser(app.url) if open_browser_ else False
    print((f"{DISPLAY_NAME} setup is open in your browser:" if opened else
           f"{DISPLAY_NAME} setup is ready. Open this address in your browser:") + f"\n  {app.url}\n"
          "  (this window can stay in the background; it closes when you press Close setup on the page)",
          file=out, flush=True)
    if open_browser_ and not opened and windowless:
        _message_box(f"Open this address in your browser to finish setting up Copper:\n\n{app.url}")
    try:
        reason = app.wait()
    except KeyboardInterrupt:
        reason = "interrupted"
    finally:
        app.close()
    print(invocation.cmds(f"Copper setup closed ({reason}). Run `copper setup` any time to open it again."),
          file=out, flush=True)
    return 0
