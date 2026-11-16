"""Harbor agent for EDA-bench: Harbor's own Claude Code agent with Copper's MCP server added.

Everything about Claude Code (install, auth, model, flags, trajectory) is Harbor's ``ClaudeCode``
unchanged; this class only

* installs Copper into the agent container (``/opt/copper``, a venv that sees the image's system
  Python so ``pcbnew`` and Pillow come from KiCad's install), and
* registers it as a stdio MCP server named ``copper``.

The task instruction is passed through untouched. Run from the repository root:

    PYTHONPATH=bench/edabench harbor run --path <dataset> \\
        --agent copper_agent:CopperClaudeCode --model anthropic/<model>

Agent kwargs (``--ak key=value``):

* ``copper_spec``: what to ``pip install`` instead of a wheel built from this checkout. Copper is
  not on PyPI, so this is a git URL, e.g.
  ``"copper-kicad @ git+https://github.com/claynicholson/copper-kicad-mcp@<tag-or-commit>"``.
* ``copper_wheel``: path to an already built wheel on the host.

Every Claude Code kwarg (``max_turns``, ``reasoning_effort``, ``append_system_prompt``, ...) still works.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import MCPServerConfig

REPO = Path(__file__).resolve().parents[2]
VENV = "/opt/copper"
COPPER_HOME = "/opt/copper-home"
LAUNCHER = "/usr/local/bin/copper-mcp"
LOGS = "/logs/agent"

_wheel_lock = threading.Lock()
_wheel: Path | None = None


def build_wheel() -> Path:
    """One wheel of this checkout per host process, shared by every concurrent trial."""
    global _wheel
    with _wheel_lock:
        if _wheel is None or not _wheel.exists():
            out = Path(tempfile.mkdtemp(prefix="copper-edabench-wheel-"))
            subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out), str(REPO)],
                           check=True, capture_output=True, text=True)
            _wheel = next(out.glob("copper_kicad-*.whl"))
        return _wheel


class CopperClaudeCode(ClaudeCode):
    @staticmethod
    def name() -> str:
        return "copper-claude-code"

    def __init__(self, logs_dir: Path, *args: Any, copper_spec: str | None = None,
                 copper_wheel: str | None = None, **kwargs: Any):
        super().__init__(logs_dir, *args, **kwargs)
        if copper_spec and copper_wheel:
            raise ValueError("pass copper_spec or copper_wheel, not both")
        self._copper_spec = copper_spec
        self._copper_wheel = Path(copper_wheel).expanduser().resolve() if copper_wheel else None
        if not any(s.name == "copper" for s in self.mcp_servers):
            self.mcp_servers.append(MCPServerConfig(name="copper", transport="stdio", command=LAUNCHER))

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)

        if self._copper_spec:
            target = self._copper_spec
        else:
            wheel = self._copper_wheel or await asyncio.to_thread(build_wheel)
            target = f"/tmp/{wheel.name}"
            await environment.upload_file(wheel, target)

        launcher = (
            "#!/bin/sh\n"
            f"export COPPER_HOME={COPPER_HOME} COPPER_NO_UPDATE_CHECK=1 PYTHONIOENCODING=utf-8\n"
            f'exec {VENV}/bin/copper serve "$@"\n'
        )
        await self.exec_as_root(
            environment,
            command=(
                "set -eu; "
                f"python3 -m venv --system-site-packages {VENV}; "
                f"{VENV}/bin/pip install --quiet --no-cache-dir --disable-pip-version-check {shlex.quote(target)}; "
                f"mkdir -p {COPPER_HOME} {LOGS}; chmod 0777 {COPPER_HOME}; "
                f"printf %s {shlex.quote(launcher)} > {LAUNCHER}; chmod 0755 {LAUNCHER}; "
                # Record what Copper sees (KiCad version, libraries, capabilities) next to the trajectory.
                f"COPPER_HOME={COPPER_HOME} COPPER_NO_UPDATE_CHECK=1 {VENV}/bin/copper doctor --json --fast --offline "
                f"> {LOGS}/copper-doctor.json 2>&1 || true"
            ),
        )

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        try:
            await super().run(instruction, environment, context)
        finally:
            # Copper's own per-session tool ledger, for failure analysis next to Claude Code's trajectory.
            await environment.exec(
                command=f"[ -d {COPPER_HOME}/sessions ] && cp -r {COPPER_HOME}/sessions {LOGS}/copper-sessions || true",
                user="root",
            )
