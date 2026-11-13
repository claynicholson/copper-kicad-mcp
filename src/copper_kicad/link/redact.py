"""Second line of defence for secrets: scrub them from anything printed or returned.

The first line is design: no MCP tool takes or returns a secret and nothing in
Copper puts one in a result object. This module exists so that an exception
message, a debug trace or a log line that happens to carry one is cleaned too.

    register(value)        remember a secret (and its URL-quoted / base64 / JSON-escaped forms)
    scrub(text) -> text    replace every registered form with ``[redacted]``
    scrub_obj(obj) -> obj  the same, recursively, for JSON-like data
    ScrubbingStream(f)     file-like wrapper that scrubs on write
    safe_url(url)          the URL with every query value replaced by ``…``
    alias(real, shown)     display a folder under another name (``copper setup --demo`` shows its throw-away
                           home as ``C:\\Users\\you``); applied by scrub() after the secrets
"""
from __future__ import annotations

import base64
import json
import re
import threading
import urllib.parse

REDACTED = "[redacted]"
MIN_LEN = 6            # shorter values would produce false positives

_lock = threading.Lock()
_forms: set[str] = set()
_rx: re.Pattern | None = None

# Token shapes that are secrets wherever they appear, registered or not
# (GitHub's documented prefixes: ghp_ gho_ ghu_ ghs_ ghr_ and github_pat_).
_SHAPES = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")


def register(value: str | None) -> None:
    global _rx
    if not value or len(value) < MIN_LEN:
        return
    forms = {value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value),
             base64.b64encode(value.encode("utf-8", "replace")).decode("ascii"),
             json.dumps(value)[1:-1]}
    with _lock:
        if forms <= _forms:
            return
        _forms.update(f for f in forms if len(f) >= MIN_LEN)
        _rx = re.compile("|".join(re.escape(f) for f in sorted(_forms, key=len, reverse=True)))


def clear() -> None:
    """Forget everything (tests)."""
    global _rx, _alias_rx
    with _lock:
        _forms.clear()
        _rx = None
        _aliases.clear()
        _alias_rx = None


_aliases: dict[str, str] = {}
_alias_rx: re.Pattern | None = None


_SEP = r"(?:\\+|/)"


def _parts(path: str) -> list[str]:
    return [p for p in re.split(r"[\\/]+", str(path)) if p]


def alias(real: str, shown: str) -> None:
    """Show ``real`` (a folder) as ``shown`` wherever text passes through :func:`scrub`. Any slash style
    (``\\``, ``/``, and the doubled ``\\\\`` of JSON or Python string literals) and any letter case of ``real``
    match; the separator of the original text is kept."""
    global _alias_rx
    parts = _parts(real)
    if len(parts) < 2 or len("".join(parts)) < 4:
        return
    with _lock:
        _aliases["\x00".join(p.lower() for p in parts)] = shown
        pats = sorted(_aliases, key=lambda k: len(k.split("\x00")), reverse=True)
        _alias_rx = re.compile("|".join(_SEP.join(re.escape(p) for p in k.split("\x00")) + r"(?=$|[\\/\"'\s,;:)\]}])"
                                        for k in pats), re.IGNORECASE)


def _show(m: re.Match) -> str:
    form = m.group(0)
    sep = re.search(_SEP, form).group(0)
    key = "\x00".join(p.lower() for p in _parts(form))
    shown = _aliases.get(key)
    if shown is None:
        return form
    return sep.join(_parts(shown))          # a leading "/" of a POSIX path stays outside the match


def scrub(text):
    if not isinstance(text, str) or not text:
        return text
    rx = _rx
    if rx is not None:
        text = rx.sub(REDACTED, text)
    text = _SHAPES.sub(REDACTED, text)
    arx = _alias_rx
    return arx.sub(_show, text) if arx is not None else text


def scrub_obj(obj):
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, dict):
        return {scrub(k) if isinstance(k, str) else k: scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(scrub_obj(v) for v in obj)
    return obj


def safe_url(url: str) -> str:
    """``https://h/p?apiKey=K&q=1`` → ``https://h/p?apiKey=…&q=…`` (and no userinfo)."""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return "<url>"
    host = u.hostname or ""
    if u.port:
        host += f":{u.port}"
    q = "&".join(f"{k}=…" for k, _ in urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    return urllib.parse.urlunsplit((u.scheme, host, u.path, q, ""))


class ScrubbingStream:
    """Wraps a text stream; everything written passes through :func:`scrub`."""

    def __init__(self, stream):
        self._s = stream

    def write(self, s):
        return self._s.write(scrub(s) if isinstance(s, str) else s)

    def writelines(self, lines):
        for ln in lines:
            self.write(ln)

    def __getattr__(self, name):
        return getattr(self._s, name)
