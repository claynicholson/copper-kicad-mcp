"""`python -m copper_kicad.focus [project] [--port N] [--no-open]` — run the Copper Canvas on its own.

Same thing `copper canvas` does once cli.py is wired to `focus.canvas.serve`."""
import argparse
import sys

from .canvas import serve


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="copper canvas", description="Copper Canvas — point at your design for your AI")
    ap.add_argument("project", nargs="?", help="project folder or .kicad_pro (default: the active Copper project)")
    ap.add_argument("--port", type=int, default=0, help="loopback port (default: a random free one)")
    ap.add_argument("--no-open", action="store_true", help="print the URL instead of opening the browser")
    a = ap.parse_args(argv)
    return serve(a.project, port=a.port, launch=not a.no_open)


if __name__ == "__main__":
    sys.exit(main())
