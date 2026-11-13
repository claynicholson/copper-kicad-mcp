"""Optional GitHub link.

Order of preference:

1. **The GitHub CLI.** When ``gh`` is installed and logged in, Copper uses that login as-is. Copper
   stores no token at all and never asks ``gh`` for one; ``links.json`` records only "linked through gh"
   and the account name. When ``gh`` is installed but logged out, ``copper link github`` runs
   ``gh auth login --web`` (GitHub's own browser sign-in) for the user.
2. **OAuth device flow**, when ``gh`` is not installed and a client id is configured
   (``COPPER_GITHUB_CLIENT_ID`` or ``github_client_id`` in ``$COPPER_HOME/prefs.json``). Copper ships no
   client id: the project owner has to register an OAuth app with device flow enabled first. The user
   types a short code on github.com; Copper never sees a password. The resulting token goes into the OS
   credential store (:mod:`store`) and nowhere else.
3. Otherwise :func:`link` explains both options.

No function here returns a token to a caller outside this module except :func:`_token`, which exists for
a future ``copper share`` and is never reachable from an MCP tool.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import load_links, now, redact, save_links, store as storemod

IS_WIN = sys.platform == "win32"
_NO_WINDOW = 0x08000000 if IS_WIN else 0
NAME = "github"
HOST = "github.com"
DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
VERIFY_PAGE = "https://github.com/login/device"
API_USER = "https://api.github.com/user"
DEFAULT_SCOPE = "public_repo"
UA = "copper-kicad (link)"


class LinkError(Exception):
    def __init__(self, message: str, code: str = "error", fix: str | None = None):
        super().__init__(message)
        self.message, self.code, self.fix = message, code, fix


# ── gh CLI ────────────────────────────────────────────────────────────────
def gh_path() -> str | None:
    """Absolute path of ``gh``. In the test sandbox only an explicit ``COPPER_GH`` is used, so tests
    never read the real ``gh`` login."""
    explicit = os.environ.get("COPPER_GH")
    if explicit:
        return explicit if os.path.exists(explicit) else None
    if os.environ.get("COPPER_CONFIG_HOME_OVERRIDE"):
        return None
    return shutil.which("gh")


def _gh(args: list[str], timeout: float = 20, interactive: bool = False) -> subprocess.CompletedProcess | None:
    exe = gh_path()
    if not exe:
        return None
    cmd = [exe, *args]
    if IS_WIN and exe.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c", *cmd]
    env = dict(os.environ, GH_PROMPT_DISABLED="1", NO_COLOR="1") if not interactive else None
    try:
        if interactive:
            return subprocess.run(cmd, timeout=timeout)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL,
                              creationflags=_NO_WINDOW, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None


def gh_status() -> dict:
    """``{'installed', 'logged_in', 'user', 'detail'}``. Never asks gh for the token."""
    exe = gh_path()
    if not exe:
        return {"installed": False, "logged_in": False, "user": None, "detail": "the GitHub CLI (gh) is not installed"}
    r = _gh(["auth", "status", "--hostname", HOST])
    if r is None:
        return {"installed": True, "logged_in": False, "user": None, "detail": "gh did not answer"}
    if r.returncode != 0:
        return {"installed": True, "logged_in": False, "user": None,
                "detail": "gh is installed but not logged in to github.com"}
    user = None
    u = _gh(["api", "user", "--jq", ".login"])
    if u is not None and u.returncode == 0:
        user = (u.stdout or "").strip().splitlines()[0] if (u.stdout or "").strip() else None
    if not user:
        import re
        m = re.search(r"(?:account|as)\s+([A-Za-z0-9-]+)", (r.stdout or "") + (r.stderr or ""))
        user = m.group(1) if m else None
    return {"installed": True, "logged_in": True, "user": user, "detail": "logged in through gh"}


# ── configuration ─────────────────────────────────────────────────────────
def client_id() -> str | None:
    """The OAuth app's public client id, from the environment or prefs.json. Never hard-coded."""
    v = (os.environ.get("COPPER_GITHUB_CLIENT_ID") or "").strip()
    if v:
        return v
    try:
        from . import copper_home
        d = json.loads((copper_home() / "prefs.json").read_text(encoding="utf-8"))
        v = str(d.get("github_client_id") or "").strip()
        return v or None
    except (OSError, ValueError):
        return None


# ── status ────────────────────────────────────────────────────────────────
def status(live: bool = True, gh: dict | None = None) -> dict:
    """``{'linked', 'via', 'user', 'detail', 'fix'}``.

    ``live`` asks ``gh`` (about 0.3 s); without it only the index is read. The credential store is read
    for presence only when the index says a device-flow token was stored."""
    rec = (load_links()["links"].get(NAME) or {})
    via = rec.get("via")
    out = {"linked": False, "via": None, "user": None, "detail": "not linked (optional)", "fix": "copper link github",
           "linked_at": rec.get("linked_at")}
    if via == "gh":
        if not live:
            return {**out, "linked": True, "via": "gh", "user": rec.get("user"),
                    "detail": f"linked through the GitHub CLI as {rec.get('user') or '?'}", "fix": None}
        g = gh or gh_status()
        if g["logged_in"]:
            return {**out, "linked": True, "via": "gh", "user": g["user"] or rec.get("user"),
                    "detail": f"linked through the GitHub CLI as {g['user'] or rec.get('user') or '?'}", "fix": None}
        return {**out, "via": "gh", "detail": f"was linked through gh, but {g['detail']}",
                "fix": "gh auth login, then copper link github"}
    if via == "device":
        try:
            have = storemod.get_store().get(NAME) is not None
        except storemod.StoreError as e:
            return {**out, "via": "device", "detail": f"credential store unavailable: {e}", "code": "STORE_UNAVAILABLE"}
        if have:
            return {**out, "linked": True, "via": "device", "user": rec.get("user"),
                    "detail": f"linked as {rec.get('user') or '?'} (token in {storemod.get_store().name})", "fix": None}
        return {**out, "via": "device", "detail": "the stored token is gone; link again"}
    return out


def available(gh: dict | None = None) -> dict:
    """What `copper link github` would do on this machine, without doing it."""
    g = gh or gh_status()
    cid = client_id()
    if g["logged_in"]:
        how = "gh"
    elif g["installed"]:
        how = "gh-login"
    elif cid:
        how = "device"
    else:
        how = "none"
    return {"how": how, "gh": g, "client_id_configured": bool(cid), "explain": explain(how)}


def explain(how: str) -> str:
    return {
        "gh": "Uses your existing GitHub CLI login. Copper stores no token.",
        "gh-login": "The GitHub CLI is installed but logged out. Copper runs `gh auth login --web`: "
                    "you sign in on github.com in your browser; Copper never sees your password.",
        "device": "Opens github.com/login/device where you type a short code. The token is kept in your "
                  "operating system's credential store.",
        "none": "Install the GitHub CLI (https://cli.github.com), run `gh auth login`, then `copper link github`. "
                "(Device sign-in without gh needs an OAuth client id that this build does not ship; set "
                "COPPER_GITHUB_CLIENT_ID if your organisation has one.)",
    }[how]


# ── link / unlink ─────────────────────────────────────────────────────────
def _record(via: str, user: str | None, scope: str | None = None) -> dict:
    d = load_links()
    d["links"][NAME] = {"via": via, "user": user, "linked_at": now(), **({"scope": scope} if scope else {})}
    save_links(d)
    return d["links"][NAME]


def link(interactive: bool = True, on_code=None, open_browser: bool = True, scope: str = DEFAULT_SCOPE,
         allow_weak_store: bool = False) -> dict:
    """Link GitHub by the best available route. ``on_code(info)`` is called with the device-flow code."""
    g = gh_status()
    if g["installed"] and not g["logged_in"]:
        if not interactive:
            raise LinkError("The GitHub CLI is installed but not logged in.", "gh_logged_out",
                            "run `gh auth login` in a terminal, then `copper link github`")
        _gh(["auth", "login", "--hostname", HOST, "--web", "--git-protocol", "https"], timeout=600, interactive=True)
        g = gh_status()
        if not g["logged_in"]:
            raise LinkError("GitHub CLI login did not complete.", "gh_login_failed", "run `gh auth login` and try again")
    if g["logged_in"]:
        _forget_token()
        rec = _record("gh", g["user"])
        return {"linked": True, "via": "gh", "user": g["user"],
                "detail": f"linked through the GitHub CLI as {g['user'] or '?'}; Copper stores no token", **rec}
    cid = client_id()
    if not cid:
        raise LinkError("GitHub cannot be linked yet on this computer.", "no_route", explain("none"))
    st = storemod.get_store(allow_weak=allow_weak_store)       # fail before the user types a code
    info = device_start(cid, scope)
    if on_code:
        on_code(info)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(info["verification_uri"])
        except Exception:  # noqa: BLE001
            pass
    token = device_poll(cid, info)
    return _finish_device(st, token, scope)


def _finish_device(st, token: str, scope: str | None, api_user: str = API_USER) -> dict:
    redact.register(token)
    user = _whoami(token, api_user)
    st.set(NAME, token)
    rec = _record("device", user, scope)
    return {"linked": True, "via": "device", "user": user,
            "detail": f"linked as {user or '?'}; token kept in {st.name}", **rec}


def unlink() -> dict:
    d = load_links()
    had = d["links"].pop(NAME, None)
    save_links(d)
    removed = _forget_token()
    note = ""
    if had and had.get("via") == "gh":
        note = "Your GitHub CLI login is untouched (`gh auth logout` signs gh out)."
    elif removed:
        note = ("The token was removed from this computer. To revoke it on GitHub as well: "
                "github.com → Settings → Applications → Authorized OAuth Apps.")
    return {"unlinked": bool(had) or removed, "token_removed": removed, "note": note}


def _forget_token() -> bool:
    try:
        return bool(storemod.get_store().delete(NAME))
    except storemod.StoreError:
        return False


def _token() -> str | None:
    """The device-flow token (None when linked through gh). For in-process use by a future
    `copper share` only; registered with the redactor on every read."""
    try:
        t = storemod.get_store().get(NAME)
    except storemod.StoreError:
        return None
    redact.register(t)
    return t


# ── device flow ───────────────────────────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):          # a redirect would replay the body to another host
        return None


def _post(url: str, fields: dict, timeout: float = 15) -> dict:
    if os.environ.get("COPPER_OFFLINE") == "1":
        raise LinkError("offline mode (COPPER_OFFLINE=1): GitHub cannot be reached", "network")
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode("ascii"), method="POST",
                                 headers={"Accept": "application/json", "User-Agent": UA,
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace") or "{}")
        except ValueError:
            body = {}
        if body.get("error"):
            return body
        raise LinkError(f"GitHub answered HTTP {e.code} at {redact.safe_url(url)}", "http") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LinkError(f"GitHub could not be reached ({getattr(e, 'reason', e)})", "network",
                        "check your internet connection and try again") from None


def device_start(cid: str, scope: str = DEFAULT_SCOPE, url: str = DEVICE_CODE_URL) -> dict:
    """Step 1: ask GitHub for a user code. Returns ``{'device_code','user_code','verification_uri',
    'expires_in','interval'}`` (``device_code`` is a secret until the flow ends)."""
    d = _post(url, {"client_id": cid, "scope": scope})
    if d.get("error"):
        fix = None
        if d["error"] in ("device_flow_disabled", "unauthorized_client", "incorrect_client_credentials", "not_found"):
            fix = ("the configured client id is not an OAuth app with device flow enabled; use the GitHub CLI "
                   "instead (`gh auth login`, then `copper link github`)")
        raise LinkError(f"GitHub refused to start sign-in: {d.get('error_description') or d['error']}", d["error"], fix)
    if not d.get("device_code") or not d.get("user_code"):
        raise LinkError("GitHub's reply did not contain a sign-in code", "bad_reply")
    redact.register(d["device_code"])
    return {"device_code": d["device_code"], "user_code": d["user_code"],
            "verification_uri": d.get("verification_uri") or VERIFY_PAGE,
            "expires_in": int(d.get("expires_in") or 900), "interval": max(1, int(d.get("interval") or 5))}


def device_poll(cid: str, info: dict, url: str = TOKEN_URL, sleep=time.sleep, should_stop=None) -> str:
    """Step 2: wait until the user has typed the code. Honours ``interval`` and ``slow_down``."""
    interval = info["interval"]
    deadline = time.monotonic() + info["expires_in"]
    while time.monotonic() < deadline:
        if should_stop and should_stop():
            raise LinkError("sign-in was cancelled", "cancelled")
        sleep(interval)
        d = _post(url, {"client_id": cid, "device_code": info["device_code"],
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if d.get("access_token"):
            redact.register(d["access_token"])
            return d["access_token"]
        err = d.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval = int(d.get("interval") or interval + 5)
            continue
        if err == "expired_token":
            break
        if err == "access_denied":
            raise LinkError("You cancelled the sign-in on GitHub.", "access_denied")
        raise LinkError(f"GitHub sign-in failed: {d.get('error_description') or err or 'unknown reply'}", err or "error")
    raise LinkError("The sign-in code expired before it was entered.", "expired_token", "run `copper link github` again")


def _whoami(token: str, url: str = API_USER) -> str | None:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": UA,
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("login")
    except Exception:  # noqa: BLE001 — the account name is a nicety; the link itself succeeded
        return None
