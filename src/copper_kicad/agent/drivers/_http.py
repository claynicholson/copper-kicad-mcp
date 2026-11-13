"""HTTP plumbing shared by the API-key drivers (stdlib only). Not a driver: it registers nothing.

    Api(base_url, headers, secret, turn=None)
      .stream(path, body) -> Stream           POST JSON, expect a server-sent-event body (one attempt)
      .get_json(path, timeout=20) -> dict     GET JSON (model lists, account tests)
    Stream.events() -> Iterator[(event_name | None, data_text)]      one SSE event at a time; ends at EOF
    Stream.close()
    failure(status, headers, text, secret) -> DriverError              HTTP status -> protocol error kind
    run_with_retry(turn, attempt_fn)                                   the retry loop both API drivers use
    pause(turn, seconds)                                               cancellable sleep that feeds the watchdog
    budget_left(turn, used) -> int | None                              raises DriverError('rate', BUDGET) when spent

Retry policy (``run_with_retry``): a request is retried only while NOTHING of it reached the user, on
connection failures, HTTP 408 / 409 / 429 / 5xx. The wait is the provider's ``retry-after`` when given, else
exponential (COPPER_AGENT_BACKOFF_S, default 0.5 s, doubled per attempt, capped at 8 s). A provider asking for more
than COPPER_AGENT_RETRY_MAX_WAIT_S (default 20 s) is not waited for: the turn ends with ``rate`` and
``retry_after_s`` so the pane can say when. COPPER_AGENT_RETRIES (default 3) bounds the attempts.

Status -> kind:   401, 403 -> auth        402 -> rate (quota; not retryable)        429 -> rate
                  408, 409, 5xx -> network          400, 404, 413, 422, other 4xx -> protocol

Cancel: every open response is registered with a watcher that closes its socket the moment the turn is
cancelled, so a read blocked in the kernel returns at once and the turn's thread ends.

A secret travels in a request header and nowhere else. Provider error texts are shortened and scrubbed of the
secret before they become an event; plain-http URLs to anything but this machine are refused when a secret is set.
"""
from __future__ import annotations

import json
import os
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .base import Cancelled, DriverError

USER_AGENT = "copper-agent"
LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")


def _env_f(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


def retries() -> int:
    return int(_env_f("COPPER_AGENT_RETRIES", 3))


def scrub(text: str, secret: str | None, limit: int = 300) -> str:
    text = " ".join(str(text or "").split())
    if secret and len(secret) >= 6:
        text = text.replace(secret, "[secret]")
    return text[:limit]


def _provider_message(body: str) -> tuple[str, str]:
    """(message, type) out of a provider's JSON error body; ('', '') when it is not one."""
    try:
        d = json.loads(body)
    except ValueError:
        return (body or "").strip()[:300], ""
    err = d.get("error") if isinstance(d, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or ""), str(err.get("type") or err.get("code") or "")
    if isinstance(err, str):
        return err, ""
    if isinstance(d, dict) and d.get("message"):
        return str(d["message"]), str(d.get("type") or "")
    return "", ""


def retry_after(headers) -> float | None:
    if headers is None:
        return None
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        v = headers.get(name)
        if v:
            try:
                return max(0.0, float(v) * scale)
            except ValueError:
                pass
    return None


def failure(status: int, headers, body: str, secret: str | None = None) -> DriverError:
    """The DriverError for an HTTP error response."""
    msg, etype = _provider_message(body)
    msg = scrub(msg, secret) or f"HTTP {status}"
    code = f"HTTP_{status}"
    if status in (401, 403):
        return DriverError("auth", f"the provider rejected the credentials ({status}): {msg}", code=code,
                           fix="check the key of this account, or connect the account again")
    if status == 402:
        return DriverError("rate", f"the provider reports no credit or quota: {msg}", code=code, retryable=False,
                           fix="check billing with the provider")
    if status == 429:
        return DriverError("rate", f"rate limited by the provider: {msg}", code=code, retry_after_s=retry_after(headers))
    if status in (408, 409) or status >= 500:
        return DriverError("network", f"the provider is unavailable ({status}): {msg}", code=code, retryable=True,
                           retry_after_s=retry_after(headers))
    if status == 404:
        return DriverError("protocol", f"not found (404): {msg}", code=code, retryable=False,
                           fix="check the model id and the base URL of this account")
    return DriverError("protocol", f"the provider refused the request ({status}{', ' + etype if etype else ''}): {msg}",
                       code=code, retryable=False)


def stream_error(etype: str, message: str, secret: str | None = None) -> DriverError:
    """The DriverError for an error OBJECT delivered inside a stream (HTTP 200 already sent)."""
    t = (etype or "").lower()
    msg = scrub(message, secret) or t or "the provider reported an error"
    if "auth" in t or "permission" in t:
        return DriverError("auth", msg, code="STREAM_AUTH")
    if "rate" in t or "quota" in t or "billing" in t:
        return DriverError("rate", msg, code="STREAM_RATE")
    if "overload" in t or "api_error" in t or "server" in t or "timeout" in t or "unavailable" in t:
        return DriverError("network", msg, code="STREAM_ERROR", retryable=True)
    if "invalid" in t or "not_found" in t or "too_large" in t:
        return DriverError("protocol", msg, code="STREAM_REFUSED", retryable=False)
    return DriverError("network", msg, code="STREAM_ERROR", retryable=True)


def pause(turn, seconds: float) -> None:
    """Sleep in small steps: stops at once on cancel and keeps the watchdog fed (we are waiting on purpose)."""
    end = time.monotonic() + max(0.0, seconds)
    while True:
        if turn is not None:
            turn.check_cancel()
            turn.touch()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.05, left))


def budget_left(turn, used: int) -> int | None:
    """Tokens the run may still spend, None without a budget. Raises once the budget is spent."""
    budget = turn.request.budget_tokens
    if not budget:
        return None
    if used >= budget:
        raise DriverError("rate", f"the run reached its budget of {budget} tokens ({used} used)", code="BUDGET",
                          retryable=False, fix="raise the budget or continue in a new message")
    return budget - used


class _Watch:
    """Shuts a response's socket down when the turn is cancelled (a blocked read then returns immediately)."""

    def __init__(self, resp, turn):
        self.resp, self.turn = resp, turn
        self.done = threading.Event()
        if turn is not None:
            threading.Thread(target=self._run, name="copper-http-watch", daemon=True).start()

    def _run(self):
        while not self.done.is_set():
            if self.turn.cancelled.wait(0.1):
                if not self.done.is_set():
                    _shutdown(self.resp)
                return

    def stop(self):
        self.done.set()


def _shutdown(resp) -> None:
    """Make a read that is blocked on this response return NOW. ``shutdown`` wakes it on POSIX; on Windows only
    closing the handle does, so the descriptor is detached from the socket object (which would otherwise close a
    recycled handle later) and closed directly."""
    sock = None
    try:
        sock = resp.fp.raw._sock            # http.client.HTTPResponse -> BufferedReader -> SocketIO
    except AttributeError:
        pass
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (OSError, ValueError):
        pass
    try:
        fd = sock.detach()
        if fd is not None and fd >= 0:
            socket.close(fd)
    except (OSError, ValueError, AttributeError):
        pass


class Stream:
    """An open server-sent-event response."""

    def __init__(self, resp, turn, secret=None, what="the model stream"):
        self.resp, self.turn, self.secret, self.what = resp, turn, secret, what
        self.headers = resp.headers
        self._watch = _Watch(resp, turn)

    def lines(self):
        while True:
            if self.turn is not None:
                self.turn.check_cancel()
            try:
                raw = self.resp.readline()
            except Exception as e:  # noqa: BLE001 — OSError, timeout, http.client.IncompleteRead, ...
                if self.turn is not None:
                    self.turn.check_cancel()
                raise DriverError("network", f"{self.what} broke off: {type(e).__name__}", code="TRUNCATED",
                                  retryable=True) from None
            if self.turn is not None:
                self.turn.check_cancel()            # a socket shut down by the watcher reads as EOF
            if not raw:
                return
            if self.turn is not None:
                self.turn.touch()
            yield raw.decode("utf-8", errors="replace").rstrip("\r\n")

    def events(self):
        """(event name or None, data) per SSE event. Comment lines (': ping') only feed the watchdog."""
        name, data = None, []
        for line in self.lines():
            if not line:
                if data:
                    yield name, "\n".join(data)
                name, data = None, []
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                name = value
            elif field == "data":
                data.append(value)
        if data:                                     # a final event without the blank line after it
            yield name, "\n".join(data)

    def close(self):
        self._watch.stop()
        try:
            self.resp.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Api:
    def __init__(self, base_url: str, headers: dict | None = None, secret: str | None = None, turn=None,
                 timeout: float | None = None):
        self.base = (base_url or "").rstrip("/")
        self.headers = dict(headers or {})
        self.secret = secret
        self.turn = turn
        self.timeout = timeout or _env_f("COPPER_AGENT_HTTP_TIMEOUT_S", 120) or 120
        u = urllib.parse.urlsplit(self.base)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise DriverError("protocol", "this account has no usable base URL", code="BAD_BASE_URL", retryable=False,
                              fix="set the account's base URL (https://...)")
        if secret and u.scheme == "http" and (u.hostname or "") not in LOOPBACK:
            raise DriverError("auth", "refusing to send a key over plain http to another machine", code="INSECURE_URL",
                              retryable=False, fix="use an https:// base URL")

    def _request(self, method: str, path: str, body: dict | None, accept: str):
        data = None if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Accept", accept)
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in self.headers.items():
            req.add_header(k, v)
        return req

    def _open(self, req):
        """-> response. Raises DriverError for HTTP errors and for connection failures."""
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)   # honours the system proxy settings
        except urllib.error.HTTPError as e:
            try:
                text = e.read(20000).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                text = ""
            finally:
                e.close()
            raise failure(e.code, e.headers, text, self.secret) from None
        except Exception as e:  # noqa: BLE001 — URLError, timeout, ssl, connection reset
            if self.turn is not None:
                self.turn.check_cancel()
            reason = getattr(e, "reason", None)
            detail = type(reason).__name__ if isinstance(reason, BaseException) else type(e).__name__
            raise DriverError("network", f"cannot reach {urllib.parse.urlsplit(self.base).netloc}: {detail}",
                              code="CONNECT", retryable=True) from None

    def get_json(self, path: str, timeout: float | None = None) -> dict:
        saved, self.timeout = self.timeout, timeout or 20
        try:
            with self._open(self._request("GET", path, None, "application/json")) as resp:
                raw = resp.read(8_000_000)
        finally:
            self.timeout = saved
        try:
            out = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            raise DriverError("protocol", f"{path}: the answer is not JSON", code="BAD_JSON", retryable=False) from None
        return out if isinstance(out, dict) else {"data": out}

    def stream(self, path: str, body: dict, what: str = "the model stream") -> Stream:
        """POST and return the open event stream (one attempt; ``run_with_retry`` owns the retries)."""
        if self.turn is not None:
            self.turn.check_cancel()
        return Stream(self._open(self._request("POST", path, body, "text/event-stream")), self.turn, self.secret, what)

    @staticmethod
    def wait_for(err: DriverError, attempt: int, limit: int | None = None) -> float | None:
        """Seconds to wait before retrying after ``err`` on the given (1-based) failed attempt; None = give up."""
        limit = retries() if limit is None else limit
        if not err.retryable or attempt > limit:
            return None
        if err.retry_after_s is not None:
            if err.retry_after_s > _env_f("COPPER_AGENT_RETRY_MAX_WAIT_S", 20):
                return None
            return err.retry_after_s
        base = _env_f("COPPER_AGENT_BACKOFF_S", 0.5)
        return min(8.0, base * (2 ** (attempt - 1))) * (0.75 + random.random() / 2)


def run_with_retry(turn, attempt_fn):
    """Call ``attempt_fn(state)`` until it returns; ``state['emitted']`` set by the callee means "the user already
    saw part of this request", after which a failure is final (a retry would repeat text or tool calls)."""
    failed = 0
    while True:
        state = {"emitted": False}
        try:
            return attempt_fn(state)
        except Cancelled:
            raise
        except DriverError as e:
            failed += 1
            wait = None if state["emitted"] else Api.wait_for(e, failed)
            if wait is None:
                raise
            pause(turn, wait)
