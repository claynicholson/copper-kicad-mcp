#!/bin/sh
# Copper for KiCad — one-line installer for macOS and Linux.
#
#   curl -LsSf https://raw.githubusercontent.com/claynicholson/copper-kicad-mcp/main/installer/install.sh | sh
#
# Installs uv (if missing), then Copper into its own Python 3.12 environment
# (`uv tool install`, no sudo) from the wheel attached to the GitHub release,
# then runs `copper setup`. On a desktop that opens
# the setup page in your browser (KiCad found, what to bring along from it, AI
# apps, a test call); in a plain terminal it is the same thing as a wizard. It asks
# before it changes anything. Without a terminal (CI, docker) it runs
# `copper setup --yes`, which changes nothing in KiCad's own settings.
#
#   COPPER_VERSION=1.1.1     specific release (default: the latest GitHub release)
#   COPPER_SOURCE=<spec>     a wheel path or URL for `uv tool install` (skips the download and checksum)
#   COPPER_SETUP_ARGS="..."  arguments for `copper setup` (default: none = interactive; --yes without a terminal)
#   COPPER_NO_SETUP=1        install only
#   COPPER_NO_MODIFY_PATH=1  do not let uv edit your shell profile to put its tool folder on PATH
#   COPPER_NO_DESKTOP=1      Linux: do not add the "Copper setup" launcher to the applications menu
#   COPPER_ALLOW_ROOT=1      allow running as root (not recommended: Copper is a per-user tool)
#
# Hardening: https only (TLS 1.2+), nothing is written outside your home directory,
# no sudo, downloads go to a private temp dir that is removed on exit, and a wheel
# taken from a GitHub release is checked against that release's SHA256SUMS.txt
# before it is installed.
#
# STATUS: syntax-checked (sh -n) and reviewed on Windows only. It has NOT been run
# end to end on macOS or Linux in this build.
set -eu
umask 077

REPO="claynicholson/copper-kicad-mcp"
DEFAULT_VERSION="1.1.1"   # used when the GitHub API cannot be reached (rate limit, proxy)
if [ -t 1 ]; then A="$(printf '\033[38;2;200;120;60m')"; R="$(printf '\033[0m')"; RED="$(printf '\033[31m')"; else A=""; R=""; RED=""; fi
say() { printf '  %s━━●%s %s\n' "$A" "$R" "$1"; }
warn() { printf '  %s! %s%s\n' "$RED" "$1" "$R" >&2; }
fail() { printf '  %sx %s%s\n' "$RED" "$1" "$R" >&2; exit 1; }

echo
say "Copper for KiCad — installer"
echo

need() { command -v "$1" >/dev/null 2>&1; }

# 0. sanity
[ -n "${HOME:-}" ] && [ -d "$HOME" ] || fail "HOME is not set to a directory"
if [ "$(id -u)" = "0" ] && [ -z "${COPPER_ALLOW_ROOT:-}" ]; then
  fail "do not run this installer as root or with sudo: Copper installs for one user. (COPPER_ALLOW_ROOT=1 overrides.)"
fi
case "$(uname -s)" in
  Darwin) OS=macos ;;
  Linux)  OS=linux ;;
  *)      fail "this installer is for macOS and Linux; on Windows use the Setup.exe or install.ps1" ;;
esac
need curl || need wget || fail "curl or wget is required"
# https only, TLS 1.2 or newer, fail on HTTP errors
fetch() {
  if need curl; then curl --proto '=https' --tlsv1.2 -LsSf "$1"
  else wget --https-only --secure-protocol=TLSv1_2 -qO- "$1"; fi
}
fetch_to() {
  if need curl; then curl --proto '=https' --tlsv1.2 -LsSf -o "$2" "$1"
  else wget --https-only --secure-protocol=TLSv1_2 -qO "$2" "$1"; fi
}
sha256_of() {
  if need sha256sum; then sha256sum "$1" | cut -d' ' -f1
  elif need shasum; then shasum -a 256 "$1" | cut -d' ' -f1
  else echo ""; fi
}
TMP="$(mktemp -d "${TMPDIR:-/tmp}/copper-install.XXXXXX")" || fail "could not create a temp directory"
trap 'rm -rf "$TMP"' EXIT INT TERM HUP

# 1. uv
if ! need uv; then
  say "Installing uv (Astral's Python manager, per-user)…"
  fetch_to https://astral.sh/uv/install.sh "$TMP/uv-install.sh" || fail "could not download the uv installer"
  # COPPER_NO_MODIFY_PATH=1: neither uv's installer nor `uv tool update-shell` edits your shell profile
  if [ -n "${COPPER_NO_MODIFY_PATH:-}" ]; then UV_NO_MODIFY_PATH=1; export UV_NO_MODIFY_PATH; fi
  sh "$TMP/uv-install.sh" || fail "the uv installer failed — see https://docs.astral.sh/uv/"
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; export PATH
  need uv || fail "uv could not be installed — see https://docs.astral.sh/uv/"
fi
say "uv: $(command -v uv)"

# 2. what to install
SPEC="${COPPER_SOURCE:-}"
if [ -z "$SPEC" ]; then
  if [ -n "${COPPER_VERSION:-}" ]; then API="https://api.github.com/repos/$REPO/releases/tags/v$COPPER_VERSION";
  else API="https://api.github.com/repos/$REPO/releases/latest"; fi
  META="$(fetch "$API" 2>/dev/null || true)"
  URL="$(printf '%s' "$META" | grep -o '"browser_download_url": *"[^"]*copper_kicad-[^"]*\.whl"' | head -n1 | sed 's/.*"\(https[^"]*\)"/\1/' || true)"
  SUMS="$(printf '%s' "$META" | grep -o '"browser_download_url": *"[^"]*SHA256SUMS\.txt"' | head -n1 | sed 's/.*"\(https[^"]*\)"/\1/' || true)"
  if [ -z "$URL" ]; then
    V="${COPPER_VERSION:-$DEFAULT_VERSION}"
    URL="https://github.com/$REPO/releases/download/v$V/copper_kicad-$V-py3-none-any.whl"
    SUMS="https://github.com/$REPO/releases/download/v$V/SHA256SUMS.txt"
  fi
  if [ -n "$URL" ]; then
    WHL="$TMP/$(basename "$URL")"
    if fetch_to "$URL" "$WHL"; then
      # the wheel must match the checksum published with the same release
      if [ -n "$SUMS" ] && fetch_to "$SUMS" "$TMP/SHA256SUMS.txt"; then
        WANT="$(tr -d '\r' < "$TMP/SHA256SUMS.txt" | grep " [*]\{0,1\}$(basename "$URL")\$" | head -n1 | cut -d' ' -f1 || true)"
        GOT="$(sha256_of "$WHL")"
        if [ -z "$GOT" ]; then
          warn "no sha256sum/shasum on this machine: the download could not be verified"
        elif [ -z "$WANT" ]; then
          fail "the release's SHA256SUMS.txt does not list $(basename "$URL"); not installing an unverified file"
        elif [ "$WANT" != "$GOT" ]; then
          fail "checksum mismatch for $(basename "$URL") (expected $WANT, got $GOT); not installing it"
        else
          say "Checksum verified ($(basename "$URL"))"
        fi
      else
        fail "could not download the release's SHA256SUMS.txt; not installing an unverified file"
      fi
      SPEC="$WHL"
    else
      fail "could not download $URL"
    fi
  fi
fi
say "Installing Copper from $SPEC"

# 3. install
uv tool install --force --python 3.12 "$SPEC" || fail "uv tool install failed"
BIN="$(uv tool dir --bin)"
COPPER="$BIN/copper"
[ -x "$COPPER" ] || fail "copper not found in $BIN"
[ -n "${COPPER_NO_MODIFY_PATH:-}" ] || uv tool update-shell >/dev/null 2>&1 || true

"$COPPER" version >/dev/null 2>&1 || fail "copper was installed but does not run — re-run this installer"

# 3b. KiCad: say what was found before setup asks about it
if [ "$OS" = macos ]; then
  if [ -x "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli" ] || [ -x "/Applications/KiCad.app/Contents/MacOS/kicad-cli" ]; then
    say "KiCad found in /Applications"
  else
    warn "KiCad was not found in /Applications. Install it from https://www.kicad.org/download/macos/ and run 'copper setup' again."
  fi
else
  if need kicad-cli; then
    say "KiCad found: $(command -v kicad-cli)"
  elif need flatpak && flatpak info org.kicad.KiCad >/dev/null 2>&1; then
    warn "KiCad is installed as a Flatpak. Copper needs the kicad-cli program, which the Flatpak does not put on PATH; install KiCad from your distribution or the KiCad PPA as well."
  else
    warn "KiCad was not found. Install KiCad 10 (https://www.kicad.org/download/linux/) and run 'copper setup' again."
  fi
fi

# 3c. Linux: a launcher in the applications menu, so setup can be opened again without a terminal
if [ "$OS" = linux ] && [ -z "${COPPER_NO_DESKTOP:-}" ]; then
  APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  if mkdir -p "$APPS" 2>/dev/null; then
    {
      echo "[Desktop Entry]"
      echo "Type=Application"
      echo "Name=Copper setup"
      echo "Comment=Connect KiCad to your AI apps"
      echo "Exec=\"$COPPER\" setup --web"
      echo "Terminal=false"
      echo "Categories=Development;Electronics;"
    } > "$APPS/copper-setup.desktop" 2>/dev/null && chmod 644 "$APPS/copper-setup.desktop" 2>/dev/null || true
  fi
fi

# 4. setup. It asks before changing anything, so give it the terminal when there is
# one (stdin is the piped script under `curl | sh`). `[ -r /dev/tty ]` is true even
# without a controlling terminal (CI, docker), so actually try to open it; with no
# terminal there is nobody to ask, and --yes applies only the safe defaults.
rc=0
if [ -n "${COPPER_NO_SETUP:-}" ]; then
  say "Installed. Run 'copper setup' when you are ready."
  exit 0
fi
# shellcheck disable=SC2086
if (exec </dev/tty) 2>/dev/null; then
  "$COPPER" setup ${COPPER_SETUP_ARGS:-} </dev/tty || rc=$?
else
  "$COPPER" setup ${COPPER_SETUP_ARGS:---yes} </dev/null || rc=$?
fi
echo
if [ "$rc" -ne 0 ]; then
  say "Installed, but setup reported problems — run 'copper doctor' for the fixes."
else
  say "Done. Open a new terminal to use 'copper' (copper setup, copper link, copper doctor)."
fi
