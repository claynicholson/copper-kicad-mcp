"""``python -m copper_kicad.agent``: run the sidecar on stdio."""
from __future__ import annotations

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
