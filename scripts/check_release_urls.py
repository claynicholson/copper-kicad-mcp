"""Release smoke test: every public URL a new user (or a registry) follows must resolve.

    python scripts/check_release_urls.py [--npx] [--offline-list]

Collects the http(s) URLs in README.md, docs/INSTALL.md, server.json, the
plugin/marketplace manifests, mcpb/manifest.json and the one-line installers,
fetches each one and fails (exit 1) on anything that is not 2xx/3xx. It also
checks the npm package (registry metadata for the version in npm/package.json,
with the mcpName the MCP Registry verifies) and the GitHub release the
installers download from. ``--npx`` also runs ``npx -y copper-kicad@<version>
--version`` (what the Cursor/VS Code buttons, plugin/.mcp.json and server.json's
npm package launch). ``--offline-list`` only prints the URLs. Run it after
publishing and before announcing.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ["README.md", "docs/INSTALL.md", "server.json", ".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json",
           "plugin/.claude-plugin/plugin.json", "plugin/.codex-plugin/plugin.json", "plugin/.mcp.json",
           "plugin/README.md", "mcpb/manifest.json", "installer/install.ps1", "installer/install.sh"]
# placeholders, examples and endpoints that legitimately answer 4xx to a bare GET
SKIP = re.compile(r"[{}$<>]|example\.com|localhost|api\.adoptium\.net|astral\.sh/uv/install")
REPO = "claynicholson/copper-kicad-mcp"
import json as _json  # noqa: E402
VERSION = _json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))["version"]
# resolved by name rather than written as a URL: the npm package (npx, server.json, plugin)
# and the release the one-line installers download from
EXTRA = {f"https://registry.npmjs.org/copper-kicad/{VERSION}": ["server.json (npm package)", "plugin/.mcp.json (npx)"],
         f"https://api.github.com/repos/{REPO}/releases/latest": ["installer/install.ps1", "installer/install.sh"],
         f"https://github.com/{REPO}/releases/download/v{VERSION}/copper_kicad-{VERSION}-py3-none-any.whl":
             ["installer/install.ps1", "installer/install.sh"],
         f"https://github.com/{REPO}/releases/download/v{VERSION}/SHA256SUMS.txt": ["installer/install.sh"]}
URL = re.compile(r"https?://[^\s\"'<>)\]`|\\]+")


def urls() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {u: list(w) for u, w in EXTRA.items()}
    for rel in SOURCES:
        p = ROOT / rel
        if not p.exists():
            continue
        for u in URL.findall(p.read_text(encoding="utf-8", errors="replace")):
            u = u.rstrip(".,;:")
            if SKIP.search(u):
                continue
            found.setdefault(u, []).append(rel)
    return found


def fetch(u: str) -> tuple[str, int | None, str]:
    req = urllib.request.Request(u, headers={"User-Agent": "copper-release-check"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return u, r.status, ""
    except urllib.error.HTTPError as e:
        return u, e.code, ""
    except Exception as e:  # noqa: BLE001
        return u, None, str(getattr(e, "reason", e))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--npx", action="store_true", help="also run `npx -y copper-kicad@<version> --version`")
    ap.add_argument("--offline-list", action="store_true", help="only list the URLs")
    a = ap.parse_args(argv)
    found = urls()
    if a.offline_list:
        for u, where in sorted(found.items()):
            print(f"{u}    ({', '.join(sorted(set(where)))})")
        return 0
    bad = 0
    with ThreadPoolExecutor(8) as ex:
        for u, code, why in sorted(ex.map(fetch, found)):
            ok = code is not None and code < 400
            bad += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {code or '---'} {u}" + (f"  ({why})" if why else "")
                  + ("" if ok else f"  <- {', '.join(sorted(set(found[u])))}"))
    meta_ok = False
    try:
        req = urllib.request.Request(f"https://registry.npmjs.org/copper-kicad/{VERSION}",
                                     headers={"User-Agent": "copper-release-check"})
        with urllib.request.urlopen(req, timeout=20) as r:
            meta = _json.loads(r.read())
        meta_ok = meta.get("mcpName") == "io.github.claynicholson/copper"
    except Exception as e:  # noqa: BLE001
        meta = {"error": str(e)}
    bad += not meta_ok
    print(f"{'ok  ' if meta_ok else 'FAIL'} npm copper-kicad@{VERSION} carries mcpName io.github.claynicholson/copper")
    if a.npx:
        npx = shutil.which("npx")
        if not npx:
            print("FAIL npx not on PATH")
            bad += 1
        else:
            r = subprocess.run([npx, "-y", f"copper-kicad@{VERSION}", "--version"],
                               capture_output=True, text=True, timeout=600)
            ok = r.returncode == 0 and r.stdout.startswith(f"copper {VERSION}")
            bad += not ok
            print(f"{'ok  ' if ok else 'FAIL'} npx -y copper-kicad@{VERSION} --version -> "
                  f"{(r.stdout or r.stderr).strip().splitlines()[-1:] or ['(no output)']}")
    print(f"\n{len(found)} URLs checked, {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
