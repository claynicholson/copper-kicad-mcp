"""`copper link` — connect Copper to the things a user already has.

* :mod:`apps`    AI clients on this machine: detect, connect (write Copper's
                 server entry into the client's own config), and a real
                 connected / not-connected check that launches the registered
                 command and performs the MCP handshake.
* :mod:`github`  optional GitHub link: reuses the ``gh`` CLI's existing login
                 when ``gh`` is installed, otherwise GitHub's OAuth device flow.
* :mod:`store`   the OS credential store (Windows Credential Manager, macOS
                 Keychain, libsecret). The only secret Copper ever holds is a
                 GitHub device-flow token, and only when ``gh`` is not used.
* :mod:`redact`  scrubs every secret read by this process from text and JSON.

Rules that hold everywhere in this package:

* Copper never sees or stores a password. Sign-in happens in the user's
  browser (GitHub) or inside the AI client itself.
* A token is kept in the OS credential store only; never in ``links.json``,
  a client config, a project file, a log line or an MCP tool result.
* The MCP server never calls a language model, so there is no AI-vendor
  account to link: "connecting an AI client" means registering Copper in that
  client's config. Every client is treated the same way.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def copper_home() -> Path:
    from ..install import clients
    return clients.copper_home()


def links_path() -> Path:
    return copper_home() / "links.json"


def load_links() -> dict:
    """The non-secret index of linked accounts (states, user names, dates)."""
    try:
        d = json.loads(links_path().read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("links"), dict):
            return d
    except (OSError, ValueError):
        pass
    return {"version": 1, "links": {}}


def save_links(d: dict) -> None:
    p = links_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
