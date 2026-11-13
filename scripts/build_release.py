"""Build every Copper release artifact from one source tree.

    python scripts/build_release.py [--skip-installer] [--skip-mcpb] [--skip-npm] [--skip-video] [--validate-mcpb]
    python scripts/build_release.py --check      # exit 1 if dist/ is stale vs src/ (CI gate)
    python scripts/build_release.py --after-signing   # re-record Setup.exe's digest after Authenticode signing

Outputs (dist/):
  copper_kicad-<v>-py3-none-any.whl, copper_kicad-<v>.tar.gz   GitHub release (one-line installers, Homebrew, AppImage)
  copper-<v>.mcpb                                             Claude Desktop extension (manifest 0.4, server.type uv)
  copper-kicad-<v>.tgz                                        npm launcher (`npm publish` this file; npx -y copper-kicad)
  Copper-for-KiCad-<v>-Setup.exe                               Windows installer (NSIS, per-user)
  Copper-for-KiCad-<v>-portable-win64.zip                      unzip + install.cmd (no installer)
  copper-release.mp4                                          the release video the README links to (copied, not built;
                                                              --release-video PATH, default ../copper-media/release-video/readme/)
  SHA256SUMS.txt
  BUILD-INFO.json    version + sha256 of the source tree each artifact was built from

Every artifact is built from the working tree as it is now (an editable
`uv tool install --editable` dev install does not matter: the wheel is built
by `uv build` from src/, and the wheel's files are compared byte for byte with
src/ before anything else is packaged). Changing src/ after a build makes
`--check` fail until the artifacts are rebuilt; bump the version in
pyproject.toml and src/copper_kicad/__init__.py before rebuilding anything
that was already published under the old version.

Also syncs src/copper_kicad/skills → plugin/skills, regenerates missing brand
assets, stamps the version into npm/package.json and plugin/.mcp.json and
the mcpb sha256 into server.json. There is no PyPI package: the release
assets on GitHub and the npm package are the distribution channels.

Tools are downloaded once into build/tools: NSIS (official zip from
SourceForge) and uv.exe (astral-sh GitHub release), plus Pillow wheels and the
python-build-standalone CPython 3.12 archive so the installer works offline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
BUILD = ROOT / "build"
TOOLS = BUILD / "tools"
NSIS_VERSION = os.environ.get("COPPER_NSIS_VERSION", "3.11")
UV_VERSION = os.environ.get("COPPER_UV_VERSION", "0.13.6")
PILLOW_PYTHONS = ["cp311", "cp312"]       # 3.12 = bundled Python; 3.11 = KiCad's python (offline fallback)
UA = "copper-kicad-build"
REPO = "claynicholson/copper-kicad-mcp"
IS_WIN = sys.platform == "win32"


def log(msg):
    print(f"[build] {msg}", flush=True)


def version() -> str:
    init = (ROOT / "src" / "copper_kicad" / "__init__.py").read_text(encoding="utf-8")
    v = re.search(r'__version__\s*=\s*"([^"]+)"', init).group(1)
    pp = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pv = re.search(r'^version\s*=\s*"([^"]+)"', pp, re.M).group(1)
    if v != pv:
        raise SystemExit(f"version mismatch: __init__ {v} vs pyproject {pv}")
    return v


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, expect_sha256: str | None = None) -> Path:
    if dest.exists() and (not expect_sha256 or sha256(dest) == expect_sha256):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    if expect_sha256 and sha256(tmp) != expect_sha256:
        tmp.unlink()
        raise SystemExit(f"checksum mismatch for {url}")
    os.replace(tmp, dest)
    return dest


def run(cmd, cwd=None, env=None):
    log("$ " + " ".join(str(c) for c in cmd))
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env)
    if r.returncode != 0:
        raise SystemExit(f"command failed ({r.returncode}): {cmd[0]}")


# ── provenance ───────────────────────────────────────────────────────────
_SKIP = ("__pycache__",)


def _source_files() -> list[Path]:
    pkg = ROOT / "src" / "copper_kicad"
    files = [p for p in pkg.rglob("*") if p.is_file() and not any(x in p.parts for x in _SKIP)
             and p.suffix not in (".pyc", ".pyo")]
    return sorted(files) + [ROOT / "pyproject.toml"]


def source_digest() -> str:
    """sha256 over every packaged source file (path + bytes)."""
    h = hashlib.sha256()
    for p in _source_files():
        h.update(p.relative_to(ROOT).as_posix().encode() + b"\0")
        h.update(p.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()


def check_wheel_matches_src(whl: Path):
    """The wheel must hold exactly the files in src/copper_kicad (no stale build cache)."""
    pkg = ROOT / "src" / "copper_kicad"
    want = {p.relative_to(pkg.parent).as_posix(): p for p in _source_files() if pkg in p.parents}
    with zipfile.ZipFile(whl) as z:
        got = {n: z.read(n) for n in z.namelist() if n.startswith("copper_kicad/") and not n.endswith("/")}
    missing = sorted(set(want) - set(got))
    extra = sorted(n for n in set(got) - set(want) if not any(x in n for x in _SKIP))
    diff = sorted(n for n in set(want) & set(got) if got[n] != want[n].read_bytes())
    if missing or extra or diff:
        raise SystemExit(f"wheel {whl.name} does not match src/: missing {missing[:5]}, extra {extra[:5]}, "
                         f"different {diff[:5]}")


def write_build_info(v: str, arts: list[Path], skipped: list[str] | None = None):
    info = {"version": v, "source_sha256": source_digest(),
            "built_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
            "artifacts": {p.name: sha256(p) for p in arts}}
    if skipped:
        info["skipped"] = skipped
    (DIST / "BUILD-INFO.json").write_text(json.dumps(info, indent=1) + "\n", encoding="utf-8")


def check(dist: Path = DIST, root: Path | None = None) -> list[str]:
    """Problems that make dist/ unfit to publish (empty list = OK)."""
    v = version()
    probs = []
    bi = dist / "BUILD-INFO.json"
    if not bi.exists():
        return [f"{bi} missing: artifacts were not built by this script — run scripts/build_release.py"]
    info = json.loads(bi.read_text(encoding="utf-8"))
    if info.get("version") != v:
        probs.append(f"dist/ was built for {info.get('version')}, source is {v}")
    if info.get("source_sha256") != source_digest():
        probs.append("src/ changed after dist/ was built — rebuild (and bump the version if "
                     f"{info.get('version')} was already published)")
    arts = info.get("artifacts") or {}
    if "release-video" in (info.get("skipped") or []):
        probs.append(f"built with --skip-video: {RELEASE_VIDEO} is not in dist/, but the README links to it "
                     "as a release asset — rebuild with the video")
    for name, digest in arts.items():
        f = dist / name
        if not f.exists():
            probs.append(f"{name} listed in BUILD-INFO.json but missing")
        elif sha256(f) != digest:
            probs.append(f"{name} changed after the build")
    for f in dist.iterdir():
        if (f.is_file() and f.name not in arts and f.name not in ("BUILD-INFO.json", "SHA256SUMS.txt")
                and not f.name.startswith(".")):        # uv build writes dist/.gitignore
            probs.append(f"{f.name} was not produced by the last build (stale)")
    srv = json.loads(((root or ROOT) / "server.json").read_text(encoding="utf-8"))
    for pkg in srv.get("packages", []):
        if pkg.get("registryType") == "mcpb":
            name = pkg["identifier"].rsplit("/", 1)[-1]
            if name in arts and pkg.get("fileSha256") != arts[name]:
                probs.append(f"server.json fileSha256 does not match {name}")
    return probs


def after_signing(dist: Path = DIST) -> int:
    """Signing Setup.exe changes its bytes. Re-record that one file's digest in BUILD-INFO.json and rewrite
    SHA256SUMS.txt, so --check passes again; every other artifact must still match the build."""
    bi = dist / "BUILD-INFO.json"
    if not bi.exists():
        raise SystemExit(f"{bi} missing — build first")
    info = json.loads(bi.read_text(encoding="utf-8"))
    arts = info.get("artifacts") or {}
    exe = [n for n in arts if n.endswith("-Setup.exe")]
    if not exe:
        raise SystemExit("no Setup.exe in the last build")
    for name, digest in arts.items():
        if name not in exe and sha256(dist / name) != digest:
            raise SystemExit(f"{name} changed after the build — rebuild instead")
    for name in exe:
        arts[name] = sha256(dist / name)
        log(f"re-recorded {name} after signing")
    info["signed"] = exe
    bi.write_text(json.dumps(info, indent=1) + "\n", encoding="utf-8")
    (dist / "SHA256SUMS.txt").write_bytes("".join(f"{d}  {n}\n" for n, d in arts.items()).encode("utf-8"))
    return 0


# ── steps ─────────────────────────────────────────────────────────────────
def sync_skills():
    src = ROOT / "src" / "copper_kicad" / "skills"
    dst = ROOT / "plugin" / "skills"
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    n = 0
    for d in sorted(src.iterdir()):
        if (d / "SKILL.md").exists():
            shutil.copytree(d, dst / d.name, ignore=shutil.ignore_patterns("__pycache__"))
            n += 1
    log(f"synced {n} skills → plugin/skills")


def brand_assets():
    need = [ROOT / "installer/windows/assets/welcome.bmp", ROOT / "installer/windows/assets/header.bmp",
            ROOT / "installer/windows/assets/copper.ico", ROOT / "mcpb/icon.png", ROOT / "plugin/assets/logo.png"]
    if all(p.exists() for p in need):
        return
    run([sys.executable, ROOT / "scripts" / "make_brand_assets.py"])


RELEASE_VIDEO = "copper-release.mp4"     # README: releases/download/v<v>/copper-release.mp4


def find_release_video(src: str | None) -> Path:
    """The release video, a release asset made in copper-media (not built from src/). The README's hero GIF
    links to it on the GitHub release, so a build without it is refused (--skip-video for a local test build;
    --check then reports the dist/ as unfit to publish)."""
    cands = [Path(src)] if src else [ROOT.parent / "copper-media" / "release-video" / "readme" / RELEASE_VIDEO]
    for c in cands:
        if c.is_file():
            return c
    raise SystemExit(f"{RELEASE_VIDEO} not found at {cands[0]} — pass --release-video PATH "
                     "(the README links to it as a release asset), or --skip-video for a build you will not publish")


def release_video(src: Path) -> Path:
    """Copy the release video into dist/ as a release asset."""
    out = DIST / RELEASE_VIDEO
    shutil.copyfile(src, out)
    log(f"release video {out.name} ({out.stat().st_size / 1e6:.1f} MB) from {src.name}")
    return out


def check_compiles():
    """Refuse to package a tree with a module that does not even compile."""
    bad = []
    for p in _source_files():
        if p.suffix == ".py":
            try:
                compile(p.read_bytes(), str(p), "exec", dont_inherit=True)
            except SyntaxError as e:
                bad.append(f"{p.relative_to(ROOT)}:{e.lineno}: {e.msg}")
    if bad:
        raise SystemExit("source does not compile:\n  " + "\n  ".join(bad))


def build_python(v: str) -> tuple[Path, Path]:
    DIST.mkdir(exist_ok=True)
    for old in DIST.glob("copper_kicad-*"):
        old.unlink()
    uv = shutil.which("uv")
    if uv:
        run([uv, "build", "--out-dir", DIST], cwd=ROOT)
    else:
        run([sys.executable, "-m", "build", "--outdir", DIST], cwd=ROOT)
    whl = next(DIST.glob(f"copper_kicad-{v}-*.whl"))
    sdist = next(DIST.glob(f"copper_kicad-{v}.tar.gz"))
    with zipfile.ZipFile(whl) as z:
        names = z.namelist()
    for must in ("copper_kicad/cli.py", "copper_kicad/__main__.py", "copper_kicad/skills/kicad-design/SKILL.md"):
        if must not in names:
            raise SystemExit(f"wheel is missing {must}")
    check_wheel_matches_src(whl)
    log(f"wheel ok ({len(names)} files): {whl.name}")
    return whl, sdist


def build_mcpb(v: str, validate=False) -> Path:
    stage = BUILD / "mcpb"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    man = json.loads((ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    man["version"] = v
    (stage / "manifest.json").write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(ROOT / "mcpb" / "icon.png", stage / "icon.png")   # .mcpbignore is for `mcpb pack`, not the bundle
    for f in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / f, stage / f)
    shutil.copytree(ROOT / "src" / "copper_kicad", stage / "src" / "copper_kicad",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if validate:
        npx = shutil.which("npx")
        if npx:
            run([npx, "-y", "@anthropic-ai/mcpb@2", "validate", stage / "manifest.json"])
        else:
            log("npx not found; skipping mcpb validate")
    out = DIST / f"copper-{v}.mcpb"
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(stage).as_posix())
    log(f"mcpb: {out.name} ({out.stat().st_size / 1e3:.0f} kB)")
    return out


def nsis() -> Path:
    if not IS_WIN:
        m = shutil.which("makensis")
        if not m:
            raise SystemExit("makensis not found (install nsis)")
        return Path(m)
    d = TOOLS / f"nsis-{NSIS_VERSION}"
    exe = d / "makensis.exe"
    if exe.exists():
        return exe
    z = download(f"https://downloads.sourceforge.net/project/nsis/NSIS%203/{NSIS_VERSION}/nsis-{NSIS_VERSION}.zip",
                 TOOLS / f"nsis-{NSIS_VERSION}.zip")
    with zipfile.ZipFile(z) as zz:
        zz.extractall(TOOLS)
    if not exe.exists():
        raise SystemExit(f"makensis.exe not found after extracting {z}")
    return exe


def uv_exe() -> Path:
    d = TOOLS / f"uv-{UV_VERSION}"
    exe = d / "uv.exe"
    if exe.exists():
        return exe
    base = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-x86_64-pc-windows-msvc.zip"
    want = None
    try:
        req = urllib.request.Request(base + ".sha256", headers={"User-Agent": UA})
        want = urllib.request.urlopen(req, timeout=60).read().decode().split()[0]
    except Exception:  # noqa: BLE001
        log("no published sha256 for uv; continuing without checksum")
    z = download(base, TOOLS / f"uv-{UV_VERSION}-win64.zip", want)
    d.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(z) as zz:
        for n in zz.namelist():
            if n.endswith("uv.exe"):
                exe.write_bytes(zz.read(n))
    return exe


def pillow_wheels() -> list[Path]:
    meta = json.loads(urllib.request.urlopen(urllib.request.Request(
        "https://pypi.org/pypi/pillow/json", headers={"User-Agent": UA}), timeout=60).read())
    out = []
    for cp in PILLOW_PYTHONS:
        cands = [u for u in meta["urls"] if u["filename"].endswith(f"-{cp}-{cp}-win_amd64.whl")]
        if not cands:
            log(f"no Pillow wheel for {cp}; skipping")
            continue
        u = cands[0]
        out.append(download(u["url"], TOOLS / "wheels" / u["filename"], u["digests"]["sha256"]))
    return out


def python_dist(uv: Path) -> tuple[str, Path, str]:
    """Download the python-build-standalone archive the bundled uv would fetch
    for CPython 3.12 so the installer can serve it as a local mirror.
    Returns (version, archive path, relative mirror path)."""
    from urllib.parse import unquote, urlparse
    r = subprocess.run([str(uv), "python", "list", "3.12", "--only-downloads", "--show-urls"],
                       capture_output=True, text=True, check=True)
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].startswith("cpython-3.12.") and "windows-x86_64-none" in parts[0]                 and "freethreaded" not in parts[0]:
            key, url = parts
            ver = key.split("-")[1]
            segs = urlparse(url).path.split("/")
            rel = f"{segs[-2]}/{unquote(segs[-1])}"          # <release>/<file> — what uv asks the mirror for
            dest = TOOLS / "python-dist" / rel
            download(url, dest)
            return ver, dest, rel
    raise SystemExit("uv did not list a CPython 3.12 download for windows-x86_64")


DOCTOR_CMD = """@echo off
rem Copper doctor - checks KiCad, Copper and your AI apps.
title Copper doctor
"%~dp0bin\\copper.exe" doctor --pause
"""

PORTABLE_INSTALL = r"""@echo off
rem Copper for KiCad - portable install (no installer, no admin).
rem Creates a private Python environment next to this file and registers Copper with your AI apps.
setlocal
set "HERE=%~dp0"
if not exist "%HERE%uv.exe" (
  echo.
  echo Extract the whole zip first ^(right-click - Extract All^), then run install.cmd from the extracted folder.
  pause
  exit /b 1
)
rem A Copper server started by an AI app locks this folder; upgrading it then leaves a broken environment.
set "COPPER_DIR=%HERE%"
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$d=(Get-Item -LiteralPath $env:COPPER_DIR).FullName.TrimEnd('\')+'\'; $p=@(Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($d, [StringComparison]::OrdinalIgnoreCase) }); if ($p.Count) { Write-Host ('Stopping ' + $p.Count + ' running Copper process(es) - restart your AI apps afterwards.'); $p | Stop-Process -Force; Start-Sleep 1 }"
set "UV_TOOL_DIR=%HERE%tools"
set "UV_TOOL_BIN_DIR=%HERE%bin"
set "UV_PYTHON_INSTALL_DIR=%HERE%python"
set "UV_LINK_MODE=copy"
set "UV_PYTHON_INSTALL_MIRROR=file:///%HERE%python-dist"
"%HERE%uv.exe" python install {pyver} --offline --no-bin --no-registry
"%HERE%uv.exe" tool install --force --offline --managed-python --python {pyver} --find-links "%HERE%wheels" "%HERE%wheels\{wheel}"
if errorlevel 1 "%HERE%uv.exe" tool install --force --python 3.12 --find-links "%HERE%wheels" "%HERE%wheels\{wheel}"
"%HERE%bin\copper.exe" version >nul 2>&1
if errorlevel 1 (
  echo.
  echo Copper could not be installed. Quit your AI apps, check your internet connection and try again.
  pause
  exit /b 1
)
"%HERE%bin\copper.exe" setup %*
pause
"""

PORTABLE_UNINSTALL = r"""@echo off
rem Removes Copper from your AI apps' configs. Delete this folder afterwards.
"%~dp0bin\copper.exe" uninstall --client all
pause
"""


def stage_installer(v: str, whl: Path, uv: Path, wheels: list[Path], pyarchive: Path, pyrel: str) -> Path:
    stage = BUILD / "installer-stage"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "wheels").mkdir(parents=True)
    shutil.copy2(uv, stage / "uv.exe")
    shutil.copy2(whl, stage / "wheels" / whl.name)
    for w in wheels:
        shutil.copy2(w, stage / "wheels" / w.name)
    (stage / "python-dist" / pyrel).parent.mkdir(parents=True)
    shutil.copy2(pyarchive, stage / "python-dist" / pyrel)
    lic = (ROOT / "LICENSE").read_text(encoding="utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
    (stage / "LICENSE.txt").write_bytes(lic.encode("utf-8"))
    shutil.copy2(ROOT / "installer" / "windows" / "QUICKSTART.html", stage / "QUICKSTART.html")
    (stage / "copper-doctor.cmd").write_bytes(DOCTOR_CMD.replace("\n", "\r\n").encode("ascii"))
    return stage


def build_installer(v: str, stage: Path, whl: Path, pyver: str) -> Path:
    mk = nsis()
    out = DIST / f"Copper-for-KiCad-{v}-Setup.exe"
    out.unlink(missing_ok=True)
    run([mk, "/V3", "/INPUTCHARSET", "UTF8", f"/DVERSION={v}", f"/DSTAGE={stage}", f"/DWHEEL={whl.name}",
         f"/DPYVER={pyver}", f"/DOUTFILE={out}", ROOT / "installer" / "windows" / "copper.nsi"], cwd=ROOT / "installer" / "windows")
    if not out.exists():
        raise SystemExit("makensis reported success but the installer is missing")
    log(f"installer: {out.name} ({out.stat().st_size / 1e6:.1f} MB)")
    return out


def build_portable(v: str, stage: Path, whl: Path, pyver: str) -> Path:
    out = DIST / f"Copper-for-KiCad-{v}-portable-win64.zip"
    out.unlink(missing_ok=True)
    top = f"Copper-for-KiCad-{v}"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, f"{top}/{p.relative_to(stage).as_posix()}")
        z.writestr(f"{top}/install.cmd", PORTABLE_INSTALL.replace("{wheel}", whl.name).replace("{pyver}", pyver).replace("\n", "\r\n"))
        z.writestr(f"{top}/uninstall.cmd", PORTABLE_UNINSTALL.replace("\n", "\r\n"))
        z.writestr(f"{top}/README.txt", (
            f"Copper for KiCad {v} - portable\r\n\r\n"
            "1. Unzip this folder somewhere permanent (e.g. %LOCALAPPDATA%\\Programs\\Copper).\r\n"
            "2. Double-click install.cmd. It creates a private Python environment and registers Copper with your AI apps.\r\n"
            "3. Restart your AI app. Run copper-doctor.cmd if anything looks wrong.\r\n"))
    log(f"portable: {out.name} ({out.stat().st_size / 1e6:.1f} MB)")
    return out


def stamp_versions(v: str):
    """npm/package.json and the plugin's pinned npx spec follow the Python package version."""
    pj = ROOT / "npm" / "package.json"
    if pj.exists():
        d = json.loads(pj.read_text(encoding="utf-8"))
        if d.get("version") != v:
            d["version"] = v
            pj.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
            log(f"npm/package.json -> {v}")
    mj = ROOT / "plugin" / ".mcp.json"
    if mj.exists():
        t = mj.read_text(encoding="utf-8")
        nt = re.sub(r'"copper-kicad@[^"]*"', f'"copper-kicad@{v}"', t)
        if nt != t:
            mj.write_text(nt, encoding="utf-8")
            log(f"plugin/.mcp.json -> copper-kicad@{v}")


def build_npm(v: str) -> Path | None:
    """`npm pack` the launcher (its prepack script vendors src/copper_kicad) into dist/."""
    npm = shutil.which("npm")
    if not npm:
        log("npm not found; skipping the npm package")
        return None
    out = DIST / f"copper-kicad-{v}.tgz"
    out.unlink(missing_ok=True)
    run([npm, "pack", "--pack-destination", DIST], cwd=ROOT / "npm")
    if not out.exists():
        raise SystemExit(f"npm pack did not write {out.name}")
    import tarfile
    with tarfile.open(out) as tf:
        names = set(tf.getnames())
    for must in ("package/bin/copper-kicad.js", "package/py/copper_kicad/cli.py", "package/package.json"):
        if must not in names:
            raise SystemExit(f"{out.name} is missing {must}")
    pkg = ROOT / "src" / "copper_kicad"
    want = {"package/py/" + p.relative_to(pkg.parent).as_posix() for p in _source_files() if pkg in p.parents}
    got = {n for n in names if n.startswith("package/py/copper_kicad/")}
    if want != got:
        raise SystemExit(f"{out.name} does not match src/: missing {sorted(want - got)[:5]}, extra {sorted(got - want)[:5]}")
    log(f"npm: {out.name} ({out.stat().st_size / 1e6:.1f} MB)")
    return out


def stamp_server_json(v: str, mcpb: Path | None):
    p = ROOT / "server.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["version"] = v
    for pkg in d.get("packages", []):
        pkg["version"] = v
        if pkg.get("registryType") == "mcpb":
            pkg["identifier"] = f"https://github.com/{REPO}/releases/download/v{v}/copper-{v}.mcpb"
            if mcpb:
                pkg["fileSha256"] = sha256(mcpb)
    p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-installer", action="store_true")
    ap.add_argument("--skip-mcpb", action="store_true")
    ap.add_argument("--skip-npm", action="store_true")
    ap.add_argument("--validate-mcpb", action="store_true", help="run `npx @anthropic-ai/mcpb validate`")
    ap.add_argument("--check", action="store_true", help="only verify dist/ matches src/ (exit 1 if stale)")
    ap.add_argument("--after-signing", action="store_true",
                    help="after signing Setup.exe: re-record its sha256 in BUILD-INFO.json and SHA256SUMS.txt")
    ap.add_argument("--release-video", help=f"path to {RELEASE_VIDEO} (default: ../copper-media/release-video/readme/)")
    ap.add_argument("--skip-video", action="store_true",
                    help=f"build without {RELEASE_VIDEO} (local tests only: --check then fails)")
    a = ap.parse_args(argv)
    if a.after_signing:
        return after_signing()
    if a.check:
        probs = check()
        for pr in probs:
            log(f"STALE: {pr}")
        log("dist/ is up to date" if not probs else f"{len(probs)} problem(s)")
        return 1 if probs else 0
    v = version()
    log(f"Copper for KiCad {v}")
    check_compiles()                   # before touching dist/
    video_src = None if a.skip_video else find_release_video(a.release_video)
    old = DIST / "BUILD-INFO.json"
    if old.exists():
        try:
            prev = json.loads(old.read_text(encoding="utf-8"))
            if prev.get("version") == v and prev.get("source_sha256") != source_digest():
                log(f"NOTE: rebuilding {v} from changed source — bump the version first if {v} was already published")
        except ValueError:
            pass
    if DIST.exists():
        for f in DIST.iterdir():       # never leave artifacts of an older build next to new ones
            if f.is_file():
                f.unlink()
    sync_skills()
    brand_assets()
    stamp_versions(v)
    whl, sdist = build_python(v)
    arts = [whl, sdist]
    mcpb = None
    if not a.skip_mcpb:
        mcpb = build_mcpb(v, a.validate_mcpb)
        arts.append(mcpb)
    if not a.skip_npm:
        tgz = build_npm(v)
        if tgz:
            arts.append(tgz)
    if not a.skip_installer:
        uv = uv_exe()
        wheels = pillow_wheels()
        pyver, pyarchive, pyrel = python_dist(uv)
        log(f"bundling CPython {pyver} ({pyarchive.stat().st_size / 1e6:.0f} MB) for offline installs")
        stage = stage_installer(v, whl, uv, wheels, pyarchive, pyrel)
        arts.append(build_installer(v, stage, whl, pyver))
        arts.append(build_portable(v, stage, whl, pyver))
    if video_src:
        arts.append(release_video(video_src))
    stamp_server_json(v, mcpb)
    sums = "".join(f"{sha256(p)}  {p.name}\n" for p in arts)
    # LF line endings on every OS: `sha256sum -c` and install.sh's grep run on Linux/macOS
    (DIST / "SHA256SUMS.txt").write_bytes(sums.encode("utf-8"))
    write_build_info(v, arts, None if video_src else ["release-video"])
    log("done:\n" + sums)
    return 0


if __name__ == "__main__":
    sys.exit(main())
