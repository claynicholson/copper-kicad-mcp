"""Commands about the pane itself: /usage and /accounts. No engine tool, no model."""
from __future__ import annotations

from .. import accounts, drivers, usage as ledger
from . import command


@command("usage", "Show what this project's AI turns have used", usage="/usage", needs_project=False)
def usage(turn, args):
    s = ledger.summary(turn.project_dir, turn.request.conversation or None)
    lines = [ledger.line(s)]
    for name, t in sorted(s["by_model"].items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))[:6]:
        lines.append(f"  {name}: {t['turns']} turn(s), {t['in']:,} in / {t['out']:,} out"
                     + (f" ({t['cached']:,} cached)" if t["cached"] else "") + (f", cost {t['cost']:g}" if t["cost"] else ""))
    if s["project"]["model_turns"] and not s["project"]["cost_complete"]:
        lines.append("  (some turns reported tokens but no cost)")
    turn.text("\n".join(lines) + "\n")


@command("accounts", "List the connected AI accounts and the kinds that can be connected", usage="/accounts",
         needs_project=False)
def accounts_(turn, args):
    rows = [a for a in accounts.list_accounts() if a["id"] != "none"]
    lines = [f"{a.get('label') or a['id']}  ({a['kind']}" + (f", {a['base_url']}" if a.get("base_url") else "")
             + (f", model {a['default_model']}" if a.get("default_model") else "") + ")" for a in rows]
    if not rows:
        lines.append("No AI account is connected. The commands work without one; see /help.")
    kinds = [d for d in drivers.describe() if d["kind"] != "none"]
    if kinds:
        lines.append("Can be connected: " + "; ".join(
            d["label"] + ("" if d["available"] else f" (not available: {d['reason'] or 'missing'})") for d in kinds))
    turn.text("\n".join(lines) + "\n")
