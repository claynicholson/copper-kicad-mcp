#!/bin/sh
# Build Copper-for-KiCad-<version>-<arch>.AppImage.
#
#   installer/linux/appimage/build-appimage.sh dist/copper_kicad-1.1.1-py3-none-any.whl [out-dir]
#
# STATUS: NOT PUBLISHED, UNTESTED (no AppImage is attached to releases). Written on Windows; it has not been run on Linux, so treat it as a recipe to be
# debugged on a real machine (or an ubuntu CI runner), not as a working build.
#
# What it makes: an AppDir holding a relocatable CPython (python-build-standalone, fetched by uv),
# Copper's wheel and Pillow, with AppRun calling `python -m copper_kicad "$@"`.
#
# Read this before shipping it. An AppImage is the weakest way to install Copper on Linux:
#   * AI clients start Copper many times a day by absolute path. Each start of an AppImage mounts a
#     squashfs through FUSE (libfuse2, which newer distributions no longer install by default).
#   * Copper drives the *system's* KiCad (kicad-cli, and KiCad's Python for pcbnew). Neither can live
#     inside the AppImage, so it is not self-contained anyway.
#   * `uv tool install <release wheel URL>` or `pipx install <release wheel URL>` (see ../README.md) give the same
#     private environment with no FUSE and with `uv tool upgrade` / `pipx upgrade` for updates.
# Use it for the one case it serves well: a machine where nothing may be installed and one file has
# to be copied over.
#
# Needs: uv, curl, appimagetool on PATH (https://github.com/AppImage/appimagetool).
set -eu
umask 022

WHL="${1:?usage: build-appimage.sh <copper_kicad-*.whl> [out-dir]}"
OUT="${2:-dist}"
[ -f "$WHL" ] || { echo "no such wheel: $WHL" >&2; exit 1; }
for t in uv appimagetool; do command -v "$t" >/dev/null 2>&1 || { echo "$t is required" >&2; exit 1; }; done

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
VERSION="$(basename "$WHL" | sed 's/^copper_kicad-\([^-]*\)-.*/\1/')"
ARCH="$(uname -m)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/copper-appimage.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT INT TERM HUP
APPDIR="$WORK/Copper.AppDir"
mkdir -p "$APPDIR/usr" "$OUT"

# 1. a relocatable Python inside the AppDir
UV_PYTHON_INSTALL_DIR="$WORK/py" uv python install 3.12
PYHOME="$(find "$WORK/py" -maxdepth 1 -type d -name 'cpython-3.12*' | head -n1)"
[ -n "$PYHOME" ] || { echo "uv did not install a Python" >&2; exit 1; }
cp -a "$PYHOME/." "$APPDIR/usr/"

# 2. Copper and Pillow into that Python's site-packages (no venv: a venv hard-codes absolute paths)
uv pip install --python "$APPDIR/usr/bin/python3.12" --break-system-packages "$WHL"

# 3. AppRun, desktop entry, icon
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
# `Copper.AppImage` with no arguments behaves like `copper`: an MCP server when stdin is a pipe
# (an AI client started it), the command list in a terminal. `Copper.AppImage setup` opens setup.
HERE="$(dirname "$(readlink -f "$0")")"
export PYTHONNOUSERSITE=1
# Register the AppImage file itself with AI clients, not the temporary mount point.
[ -n "${APPIMAGE:-}" ] && export COPPER_LAUNCHER="$APPIMAGE"
exec "$HERE/usr/bin/python3.12" -I -m copper_kicad "$@"
EOF
chmod 755 "$APPDIR/AppRun"
cat > "$APPDIR/copper.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Copper for KiCad
Comment=Connect KiCad to your AI apps
Exec=AppRun setup --web
Icon=copper
Terminal=false
Categories=Development;Electronics;
EOF
cp "$ROOT/plugin/assets/logo.png" "$APPDIR/copper.png"

# 4. pack
ARCH="$ARCH" appimagetool "$APPDIR" "$OUT/Copper-for-KiCad-$VERSION-$ARCH.AppImage"
echo "built $OUT/Copper-for-KiCad-$VERSION-$ARCH.AppImage"
