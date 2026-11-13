"""The model-driver interface. SEAM: every way of reaching a model implements ``Driver`` (HOST_PROTOCOL.md 8).

A driver turns one user turn into calls on the ``Turn`` it is given. It never writes to stdout, never talks to
the app directly and never touches the document: text goes out through ``turn.text``, engine work through
``turn.call_tool`` (which emits the tool events, routes edits through the live host and returns what the model
should read). The server guarantees the turn ends with exactly one ``done`` whatever the driver does:

    driver returns normally            -> done(status='ok')        (or 'cancelled' if the turn was cancelled)
    raises Cancelled                   -> done(status='cancelled')
    raises DriverError(kind, ...)      -> error(kind, ...) then done(status='error')
    raises anything else               -> error(kind='engine', code='INTERNAL') then done(status='error')
    goes silent                        -> the watchdog ends the turn: error(kind='network', code='WATCHDOG')

So a driver's obligations are:
  1. call ``turn.check_cancel()`` (or look at ``turn.cancelled``) at least once per received chunk and before
     every tool call, and give every blocking read a timeout so a cancelled turn's thread can end;
  2. treat the end of a model stream WITHOUT that stream's own terminal marker as a failure:
     ``raise DriverError.truncated()``. Never report a cut-off answer as a finished one;
  3. map failures to an error kind: auth | rate | network | engine | protocol (see ``DriverError``);
  4. report usage with ``turn.usage(...)`` when the provider reports it (cumulative for the turn);
  5. keep secrets out of events, logs and exception texts (``accounts.secret(id)`` is the only way to read one).

Register a driver from its own module in this package (it is imported automatically):

    from . import register
    from .base import Driver

    @register
    class MyDriver(Driver):
        kind = "my-kind"
        ...
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

ERROR_KINDS = ("auth", "rate", "network", "engine", "protocol")


class Cancelled(Exception):
    """The turn was cancelled. Raised by ``Turn.check_cancel`` and by host calls abandoned on cancel."""


class DriverError(Exception):
    """A turn-ending failure with a kind the app can act on.

    kind       auth      no / bad / expired credentials, or no account connected       -> "Connect an account"
               rate      rate limit, quota or budget exhausted (``retry_after_s``)      -> "Try again in ..."
               network   unreachable, timeout, connection reset, stream cut short       -> "Retry"
               engine    a Copper bug or an engine failure outside a tool result        -> "Report"
               protocol  the provider / CLI / host spoke something unexpected           -> "Update"
    code       a short machine string (NO_ACCOUNT, TRUNCATED, HTTP_401, WATCHDOG, ...)
    """

    def __init__(self, kind: str, message: str, code: str | None = None, retryable: bool | None = None,
                 fix: str | None = None, retry_after_s: float | None = None):
        super().__init__(message)
        self.kind = kind if kind in ERROR_KINDS else "engine"
        self.message = message
        self.code = code
        self.retryable = (self.kind in ("rate", "network")) if retryable is None else retryable
        self.fix = fix
        self.retry_after_s = retry_after_s

    @classmethod
    def truncated(cls, what: str = "the model stream") -> "DriverError":
        return cls("network", f"{what} ended before it was complete", code="TRUNCATED", retryable=True)


@dataclass
class TurnRequest:
    """What the user asked for. Drivers read it from ``turn.request``."""
    conversation: str
    text: str
    focus: dict | None = None
    mode: str = "chat"                    # chat | quick | agent
    account: dict = field(default_factory=dict)      # the account record (no secret)
    model: str | None = None
    budget_tokens: int | None = None      # agent mode: stop when in + out for this run would exceed it


class Driver:
    """One way of reaching a model. Stateless between turns: per-conversation state goes through
    ``turn.sessions`` (history, resume ids)."""

    kind: str = ""                 # the account kind this driver serves; lower-case, [a-z0-9-]
    label: str = ""                # plain text for the account picker, e.g. "API key (messages format)"
    needs: tuple = ()              # what accounts.add must be given: any of 'secret', 'base_url', 'command'
    streaming: bool = True

    def available(self) -> tuple[bool, str]:
        """(usable on this machine?, reason when not). Cheap: no network. A CLI driver checks PATH here."""
        return True, ""

    def models(self, account: dict) -> list[dict]:
        """Models this account can use: [{'id', 'label', 'quick': bool}]. May be [] (free-text model id)."""
        return []

    def test(self, account: dict) -> dict:
        """Check the account works with one cheap request. -> {'ok': bool, 'message': str, 'models'?: [...]}"""
        return {"ok": True, "message": ""}

    def run(self, turn) -> None:
        """Run one turn to completion on the calling (worker) thread. See the module docstring for the contract.

        The usual shape of an API-style driver:

            req = turn.request
            system = prompt.system_prefix(req.mode)                  # stable -> mark it cacheable
            tools = prompt.tool_catalogue(req.mode)                  # stable, same order every turn
            messages = turn.history() + [user(prompt.user_turn(req.text, req.focus, turn.notes()))]
            while True:
                turn.check_cancel()
                for chunk in stream(system, tools, messages):        # must end with the provider's stop marker
                    turn.check_cancel()
                    turn.text(chunk.text)
                turn.usage(in_tokens, out_tokens, cached_tokens)
                if not tool_calls: return
                for call in tool_calls:
                    out = turn.call_tool(call.name, call.args)       # events + live routing happen inside
                    messages.append(tool_result(call.id, out['text'], out['images'], is_error=not out['ok']))
        """
        raise NotImplementedError


def stream_lines(fp, turn, what: str = "the model stream") -> Iterator[str]:
    """Text lines of a binary line stream (an HTTP response body, a child's stdout), one at a time.

    Between lines it checks for cancellation and feeds the watchdog. A read error or timeout becomes
    ``DriverError('network')``. The generator simply ends at EOF: the caller knows its protocol's terminal marker
    and must ``raise DriverError.truncated()`` if EOF arrives before it."""
    while True:
        turn.check_cancel()
        try:
            raw = fp.readline()
        except (OSError, ValueError, EOFError) as e:
            turn.check_cancel()
            raise DriverError("network", f"{what} failed: {type(e).__name__}: {e}", code="STREAM_READ") from None
        except Exception as e:  # noqa: BLE001 — http.client.IncompleteRead and friends
            turn.check_cancel()
            raise DriverError("network", f"{what} failed: {type(e).__name__}", code="TRUNCATED") from None
        if not raw:
            return
        turn.touch()
        yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
