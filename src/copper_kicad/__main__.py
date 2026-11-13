"""``python -m copper_kicad`` → the ``copper`` CLI (MCP server when stdin is a pipe).

Also runnable as a plain script (``python src/copper_kicad/__main__.py``), which
is how an MCPB host may launch the bundle's ``entry_point`` when it does not use
``mcp_config``: then there is no parent package, so put ``src`` on sys.path and
import absolutely.
"""
import sys

if __package__ in (None, ""):
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copper_kicad.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
