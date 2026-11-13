"""On-demand Freerouting + Java runtime management.

The tools folder is ``$COPPER_HOME/tools`` (default ``~/.copper/tools``). When COPPER_HOME points
somewhere else and has no install yet, an install already in the shared ``~/.copper/tools`` is used
as it is (read-only) instead of downloading ~120 MB again.

Freerouting 2.2.x needs Java 25+. We use, in order:
  1. ``COPPER_JAVA`` (path to a java executable),
  2. a Temurin JRE previously downloaded into ``$COPPER_HOME/tools/jre-<major>``,
  3. the same in the shared ``~/.copper/tools``,
  4. a ``java`` on PATH if new enough,
  5. otherwise download Temurin JRE from the Adoptium API (sha256 verified).

The Freerouting jar is pinned (``FREEROUTING_VERSION``, override with
``COPPER_FREEROUTING_VERSION`` or point ``COPPER_FREEROUTING_JAR`` at a jar)
and downloaded from GitHub releases into ``$COPPER_HOME/tools`` (the shared folder's jar is used
when it is already there).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from ..mcp.registry import ToolError

# 2.2.3 is the release other projects report as deterministic/stable for CLI use
# (2.1.0 had CLI bugs, 2.4.x is much slower).
FREEROUTING_VERSION = os.environ.get("COPPER_FREEROUTING_VERSION", "2.2.3")
JAVA_MAJOR = 25  # freerouting 2.2.x jars are class-file 69 (Java 25)
UA = {"User-Agent": "copper-kicad (+https://github.com/claynicholson/copper-kicad-mcp)"}
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def tools_dir() -> Path:
    d = Path(os.environ.get("COPPER_HOME", Path.home() / ".copper")) / "tools"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _shared_tools() -> Path:
    """The default tools folder, used as a read-only fallback when COPPER_HOME points elsewhere."""
    return Path.home() / ".copper" / "tools"


def _os_arch() -> tuple[str, str]:
    osn = {"win32": "windows", "darwin": "mac"}.get(sys.platform, "linux")
    m = platform.machine().lower()
    arch = "aarch64" if m in ("arm64", "aarch64") else "x64"
    return osn, arch


def _java_version(java: str) -> int:
    try:
        r = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=30,
                           creationflags=_NO_WINDOW)
        m = re.search(r'version "(\d+)(?:\.(\d+))?', (r.stderr or "") + (r.stdout or ""))
        if not m:
            return 0
        major = int(m.group(1))
        return int(m.group(2) or 0) if major == 1 else major
    except (OSError, subprocess.SubprocessError):
        return 0


def _find_java_in(d: Path) -> Path | None:
    exe = "java.exe" if sys.platform == "win32" else "java"
    for p in d.rglob(exe):
        if p.parent.name == "bin":
            return p
    return None


def _download(url: str, dest: Path, sha256: str | None = None, progress=None, label=""):
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=UA)
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got, last = 0, 0.0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if progress and total and time.time() - last > 1:
                    last = time.time()
                    progress(got / total, f"downloading {label} {got >> 20}/{total >> 20} MB")
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise ToolError(f"download failed for {label or url}: {e}", code="DOWNLOAD_FAILED",
                        fix="check the network connection (or set COPPER_JAVA / COPPER_FREEROUTING_JAR to local files) and retry")
    if sha256 and h.hexdigest().lower() != sha256.lower():
        tmp.unlink(missing_ok=True)
        raise ToolError(f"checksum mismatch for {label}", code="DOWNLOAD_CORRUPT", fix="retry the call")
    os.replace(tmp, dest)


def _get_json(url: str):
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def find_java() -> Path | None:
    env = os.environ.get("COPPER_JAVA")
    if env and Path(env).exists():
        return Path(env)
    local = tools_dir() / f"jre-{JAVA_MAJOR}"
    if local.is_dir():
        j = _find_java_in(local)
        if j:
            return j
    shared = _shared_tools() / f"jre-{JAVA_MAJOR}"
    if shared.is_dir() and shared != local:
        j = _find_java_in(shared)
        if j:
            return j
    w = shutil.which("java")
    if w and _java_version(w) >= JAVA_MAJOR:
        return Path(w)
    return None


def ensure_java(progress=None) -> Path:
    j = find_java()
    if j:
        return j
    osn, arch = _os_arch()
    try:
        assets = _get_json(f"https://api.adoptium.net/v3/assets/latest/{JAVA_MAJOR}/hotspot?architecture={arch}"
                           f"&image_type=jre&os={osn}&vendor=eclipse")
        pkg = assets[0]["binary"]["package"]
        link, checksum, name = pkg["link"], pkg.get("checksum"), pkg["name"]
    except (OSError, ValueError, KeyError, IndexError):
        link = f"https://api.adoptium.net/v3/binary/latest/{JAVA_MAJOR}/ga/{osn}/{arch}/jre/hotspot/normal/eclipse"
        checksum, name = None, f"jre-{JAVA_MAJOR}.{'zip' if osn == 'windows' else 'tar.gz'}"
    work = Path(tempfile.mkdtemp(prefix="copper-jre-", dir=str(tools_dir())))
    try:
        arc = work / name
        _download(link, arc, checksum, progress, f"Java {JAVA_MAJOR} runtime (Temurin)")
        out = work / "x"
        out.mkdir()
        if name.endswith(".zip"):
            with zipfile.ZipFile(arc) as z:
                z.extractall(out)
        else:
            with tarfile.open(arc) as t:
                t.extractall(out)  # trusted source (Adoptium, sha256-checked)
        dest = tools_dir() / f"jre-{JAVA_MAJOR}"
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        os.replace(out, dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    j = _find_java_in(dest)
    if not j:
        raise ToolError("downloaded Java runtime has no bin/java", code="JAVA_MISSING",
                        fix="install Java 25+ and set COPPER_JAVA to its java executable")
    if sys.platform != "win32":
        j.chmod(0o755)
    return j


def jar_path() -> Path:
    env = os.environ.get("COPPER_FREEROUTING_JAR")
    if env:
        return Path(env)
    p = tools_dir() / f"freerouting-{FREEROUTING_VERSION}.jar"
    alt = _shared_tools() / p.name
    return alt if not _jar_ok(p) and _jar_ok(alt) else p


def _jar_ok(p: Path) -> bool:
    try:
        return p.stat().st_size > 1_000_000
    except OSError:
        return False


def ensure_jar(progress=None) -> Path:
    jp = jar_path()
    if _jar_ok(jp):
        return jp
    if os.environ.get("COPPER_FREEROUTING_JAR"):
        raise ToolError(f"COPPER_FREEROUTING_JAR points to a missing file: {jp}", code="FREEROUTING_MISSING")
    v = FREEROUTING_VERSION
    url = f"https://github.com/freerouting/freerouting/releases/download/v{v}/freerouting-{v}.jar"
    sha = None
    try:
        rel = _get_json(f"https://api.github.com/repos/freerouting/freerouting/releases/tags/v{v}")
        for a in rel.get("assets", []):
            if a.get("name") == f"freerouting-{v}.jar":
                url = a.get("browser_download_url", url)
                dg = a.get("digest") or ""
                if dg.startswith("sha256:"):
                    sha = dg.split(":", 1)[1]
    except (OSError, ValueError):
        pass  # rate-limited API — fall back to the direct URL
    _download(url, jp, sha, progress, f"Freerouting {v}")
    return jp


def status() -> dict:
    j = find_java()
    jp = jar_path()
    ready = bool(j and jp.exists())
    out = {"java": str(j) if j else None, "java_version": _java_version(str(j)) if j else None,
           "freerouting_jar": str(jp) if jp.exists() else None, "freerouting_version": FREEROUTING_VERSION,
           "ready": ready, "tools_dir": str(tools_dir())}
    if not ready:
        # not an error: the first pcb_route downloads what is missing (and says so in its result)
        out["first_route_downloads"] = [x for x, miss in ((f"Java {JAVA_MAJOR} runtime (~60 MB)", not j),
                                                          (f"Freerouting {FREEROUTING_VERSION} (~60 MB)", not jp.exists()))
                                        if miss]
    return out


_FATAL = re.compile(r"StackOverflowError|OutOfMemoryError|HeadlessException|UncaughtExceptionHandler|"
                    r"Exception in thread")


def run(dsn: Path, ses: Path, passes: int = 30, timeout: int = 600, threads: int | None = None,
        progress=None, log_file: Path | None = None, stall: float = 240.0) -> dict:
    """Run Freerouting headless on ``dsn`` writing ``ses``. Returns run info."""
    java = ensure_java(progress)
    jar = ensure_jar(progress)
    # one thread keeps Freerouting deterministic run-to-run (multi-threaded optimisation is not)
    threads = threads or 1
    cmd = [str(java), "-Djava.awt.headless=true", "-jar", str(jar),
           "-de", str(dsn), "-do", str(ses), "-mp", str(int(passes)), "-mt", str(threads),
           "--gui.enabled=false"]
    # telemetry / update checks off where the version supports these settings
    cmd += ["--usage_and_diagnostic_data.disable_analytics=true", "--feature_flags.save_jobs=0"]
    t0 = time.time()
    out_lines: list[str] = []
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
                             cwd=str(Path(dsn).parent))
    except OSError as e:
        raise ToolError(f"could not start Java/Freerouting: {e}", code="FREEROUTING_FAILED")
    import threading

    def pump():
        for line in p.stdout:  # type: ignore[union-attr]
            out_lines.append(line.rstrip())
            if progress:
                m = re.search(r"pass\s*(?:#|no\.?)?\s*(\d+)", line, re.I)
                if m:
                    progress(min(0.95, int(m.group(1)) / max(1, passes)), line.strip()[:120])
    th = threading.Thread(target=pump, daemon=True)
    th.start()
    # Freerouting sometimes loses its routing thread (StackOverflowError in PolylineTrace.combine, then a
    # HeadlessException from the uncaught-exception handler) and the JVM idles until killed: watch the
    # log and stop it a few seconds after such a fatal line, or when it has been silent too long.
    timed_out, crashed, fatal_at = False, None, None
    last_n, last_change = 0, time.time()
    while True:
        try:
            p.wait(timeout=1.0)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        if len(out_lines) != last_n:
            last_n, last_change = len(out_lines), now
            if fatal_at is None and any(_FATAL.search(x) for x in out_lines[-20:]):
                fatal_at = now
        if now - t0 > timeout:
            timed_out = True
        elif fatal_at is not None and now - last_change > 8:
            crashed = "router crashed: " + next((x.strip()[:160] for x in out_lines if _FATAL.search(x)), "fatal error")
        elif now - last_change > stall:
            crashed = f"router stalled: no output for {int(stall)} s"
        elif Path(ses).exists() and Path(ses).stat().st_size > 0 and now - last_change > 15:
            break   # session written and the JVM is just not exiting
        else:
            continue
        p.kill()
        break
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    th.join(5)
    if log_file:
        try:
            log_file.write_text(chr(10).join(out_lines), encoding="utf-8")
        except OSError:
            pass
    done = [int(m.group(1)) for x in out_lines for m in [re.search(r"pass #(\d+)\b.*was completed", x)] if m]
    return {"returncode": p.returncode, "seconds": round(time.time() - t0, 1), "timed_out": timed_out,
            "crashed": crashed, "passes_done": max(done) if done else None,
            "ses_written": Path(ses).exists() and Path(ses).stat().st_size > 0,
            "log_tail": out_lines[-15:], "command": " ".join(cmd[3:])}
