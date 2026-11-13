"""Helpers shared by the command modules. Registers no command."""
from __future__ import annotations

import re

from ...host.client import E_NO_DOCUMENT, E_UNSUPPORTED, HostError

TEXT_ARG = {"type": "string", "description": "the words typed after the command"}
NOT_THERE = (E_NO_DOCUMENT, E_UNSUPPORTED, "METHOD_NOT_FOUND", "RPC-32601")     # "this app cannot do that here"
_FINDING_KEYS = ("severity", "type", "message", "at", "refs", "pins", "fix", "sheet", "count")


def document(turn, default: str = "schematic") -> str:
    """'schematic' or 'board': where the user is. The focus says it; else the frame the app named in hello."""
    f = turn.focus if isinstance(turn.focus, dict) else {}
    d = str(f.get("document") or (turn.server.hello or {}).get("frame") or default).lower()
    return "board" if d in ("board", "pcb", "pcbnew") else "schematic"


def focus_of(turn, doc: str | None = None) -> dict:
    """The turn's focus, or {} when there is none (or it belongs to another document than ``doc``)."""
    f = turn.focus if isinstance(turn.focus, dict) else {}
    if doc and f and document(turn) != doc:
        return {}
    return f


def flags(text, known: dict) -> tuple[dict, list[str]]:
    """Split the words typed after a command into flags and the rest.
    known: {word: (argument, value)}. -> ({argument: value}, [other words]). Commas separate words too."""
    out, rest = {}, []
    for w in re.split(r"[\s,]+", str(text or "").strip()):
        if not w:
            continue
        if w.lower() in known:
            k, v = known[w.lower()]
            out[k] = v
        else:
            rest.append(w)
    return out, rest


def first_line(out: dict, limit: int = 400) -> str:
    return ((out.get("text") or "").splitlines() or [""])[0][:limit]


def say_failure(turn, what: str, out: dict) -> None:
    """One or two plain lines for a tool that did not work."""
    err = out.get("error") or {}
    code, msg = err.get("code"), err.get("message") or "it failed"
    if code == "DENIED":
        turn.text(f"{what}: not run. {msg[:1].upper() + msg[1:]}.\n")
        return
    turn.text(f"{what} did not work: {msg}" + (f"\nWhat to do: {err['fix']}" if err.get("fix") else "") + "\n")


def sheet_arg(focus: dict) -> str | None:
    """The engine's ``sheet`` argument for the focus' sheet (None for the root sheet)."""
    path = str((focus or {}).get("sheet_path") or "/").strip()
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return name or None


def finding(raw) -> dict | None:
    """One finding in the protocol's shape (HOST_PROTOCOL.md 6.5) from the engine's or a host's."""
    if not isinstance(raw, dict):
        return None
    sev = str(raw.get("severity") or "warning").lower()
    if sev not in ("error", "warning"):
        return None                                        # 'info' summaries are not rows
    f = {k: raw[k] for k in _FINDING_KEYS if raw.get(k) not in (None, "", [])}
    f["severity"] = sev
    f["type"] = str(raw.get("type") or raw.get("code") or "finding")
    f["message"] = str(raw.get("message") or raw.get("description") or f["type"])
    names = [str(r) for r in raw.get("refs") or [] if isinstance(r, str)]
    for it in raw.get("items") or []:
        if isinstance(it, dict) and it.get("ref"):
            names.append(str(it["ref"]) + (f".{it['pin']}" if it.get("pin") else ""))
    pins = [n for n in names if "." in n] + [str(p) for p in raw.get("pins") or [] if isinstance(p, str)]
    refs = list(dict.fromkeys(n.split(".", 1)[0] for n in names))
    if refs:
        f["refs"] = refs
    if pins:
        f["pins"] = list(dict.fromkeys(pins))
    else:
        f.pop("pins", None)
    if not (isinstance(f.get("at"), list) and len(f["at"]) == 2):
        f.pop("at", None)
    return f


def findings(raws, limit: int = 100) -> list[dict]:
    out = [f for f in (finding(r) for r in raws or []) if f]
    out.sort(key=lambda f: f["severity"] != "error")
    return out[:limit]


def save_documents(turn, documents, label: str) -> str | None:
    """Before a job that reads the SAVED files: ask, then save the open documents. -> a note for the user when
    the job will not see unsaved edits (None when everything asked for was saved or is not open).
    The app answers the ``save`` permission itself when nothing is unsaved."""
    host = turn.host
    try:
        ans = host.ask_permission("save", {"documents": list(documents), "label": label,
                                           "note": "the job reads the saved files, so unsaved edits are saved first"})
    except HostError as e:
        if e.code in NOT_THERE:
            return "This used the files as last saved."
        raise
    if not ans.get("granted"):
        return "Not saved first, as you chose: this used the files as last saved."
    busy = []
    for doc in documents:
        try:
            host.save(doc)
        except HostError as e:
            if e.code in NOT_THERE:
                continue
            busy.append(f"{doc} ({e.message})")
    if busy:
        return "Could not save " + ", ".join(busy) + ": this used the files as last saved."
    return None
