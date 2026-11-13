"""The no-account driver: the pane as a command surface, no model anywhere.

A turn whose text starts with '/' runs that command (agent/commands). Any other text gets one fixed answer
telling the user how to connect an account and which commands exist. Nothing here costs a token.
"""
from __future__ import annotations

from . import register
from .base import Driver


@register
class NoneDriver(Driver):
    kind = "none"
    label = "No account (commands only)"
    needs = ()
    streaming = False

    def run(self, turn) -> None:
        from .. import commands
        text = (turn.request.text or "").strip()
        if text.startswith("/"):
            name, args = commands.parse(text)
            commands.run(turn, name, args)
            return
        names = " ".join("/" + c["name"] for c in commands.describe())
        turn.text("Connect an AI account to describe changes in words.\n"
                  f"Without one, these commands work: {names}\n")
