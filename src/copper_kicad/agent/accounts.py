"""Account records: which model access the user connected. Secrets live in the OS credential store only.

    $COPPER_HOME/agent/accounts.json     [{'id','kind','label','base_url'?,'command'?,'default_model'?,'has_secret'?,'created'}]
    link/store.py                        the secret of account <id>, under the name 'agent.<id>'

    list_accounts() -> [record]          always starts with the built-in {'id': 'none', 'kind': 'none'}
    get(account_id) -> record | None
    add(kind, label=None, base_url=None, secret=None, command=None, default_model=None) -> record
    remove(account_id) -> bool
    secret(account_id) -> str | None     for drivers only; never put it in an event, a log line or an exception
    public(record) -> dict               what the app sees: the record plus 'has_secret', never the secret

``kind`` is a driver kind (agent/drivers); the protocol and this file name no vendor.
"""
from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import threading
import time
from pathlib import Path

NONE = {"id": "none", "kind": "none", "label": "No account (commands only)"}
_lock = threading.Lock()
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,30}")


class AccountError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _file() -> Path:
    return Path(os.environ.get("COPPER_HOME") or (Path.home() / ".copper")) / "agent" / "accounts.json"


def _read() -> list[dict]:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [a for a in data if isinstance(a, dict) and _ID.fullmatch(str(a.get("id") or "")) and a.get("kind")] \
        if isinstance(data, list) else []


def _write(accounts: list[dict]) -> None:
    p = _file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(accounts, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _store():
    from ..link import store
    return store.get_store()


def _secret_name(account_id: str) -> str:
    return f"agent.{account_id}"


def list_accounts() -> list[dict]:
    return [dict(NONE)] + _read()


def get(account_id) -> dict | None:
    if account_id in (None, "", "none"):
        return dict(NONE)
    return next((a for a in _read() if a["id"] == account_id), None)


def add(kind: str, label: str | None = None, base_url: str | None = None, secret: str | None = None,
        command: str | None = None, default_model: str | None = None) -> dict:
    from . import drivers
    if kind == "none" or drivers.get(kind) is None:
        raise AccountError("UNKNOWN_DRIVER", f"no driver of kind {kind!r}; available: {sorted(drivers.DRIVERS)}")
    if base_url is not None and not re.match(r"https?://[^\s]+$", base_url):
        raise AccountError("BAD_ARGUMENT", "base_url must be an http(s) URL")
    if base_url is not None and "@" in base_url.split("://", 1)[1].split("/", 1)[0]:
        # the record (and so the URL) is stored and shown in the clear; only `secret` goes to the credential store
        raise AccountError("BAD_ARGUMENT", "base_url must not contain a user name or key; put the key in the secret field")
    with _lock:
        accounts = _read()
        aid = f"{re.sub(r'[^a-z0-9]+', '-', kind.lower()).strip('-')[:16] or 'acct'}-{_secrets.token_hex(3)}"
        rec = {"id": aid, "kind": kind, "label": (label or kind)[:80], "created": round(time.time())}
        for k, v in (("base_url", base_url), ("command", command), ("default_model", default_model)):
            if v:
                rec[k] = v
        if secret:
            try:
                _store().set(_secret_name(aid), secret)
                rec["has_secret"] = True
            except Exception as e:  # noqa: BLE001 — StoreError and backend failures; the text never holds the secret
                raise AccountError("STORE_UNAVAILABLE", f"cannot save the secret in the credential store: "
                                                        f"{type(e).__name__}") from None
        accounts.append(rec)
        _write(accounts)
    return rec


def remove(account_id: str) -> bool:
    with _lock:
        accounts = _read()
        keep = [a for a in accounts if a["id"] != account_id]
        if len(keep) == len(accounts):
            return False
        _write(keep)
    try:
        _store().delete(_secret_name(account_id))
    except Exception:  # noqa: BLE001
        pass
    return True


def secret(account_id: str) -> str | None:
    try:
        return _store().get(_secret_name(account_id))
    except Exception:  # noqa: BLE001
        return None


def public(rec: dict) -> dict:
    out = {k: v for k, v in rec.items() if k in ("id", "kind", "label", "base_url", "command", "default_model", "created")}
    out["has_secret"] = bool(rec.get("has_secret"))
    return out
