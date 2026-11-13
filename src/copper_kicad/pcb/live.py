"""Optional live-board access through KiCad's IPC API (``kipy``).

Never required: everything works on files. This only detects whether a live
PCB editor is reachable and, if so, reads the board that is open in it.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def socket_dirs() -> list[Path]:
    """Where KiCad puts its API socket: <tmp>/kicad, /tmp/kicad (always on macOS) and the flatpak cache."""
    dirs = [Path(tempfile.gettempdir()) / "kicad"]
    if sys.platform != "win32":
        dirs += [Path("/tmp/kicad"), Path.home() / ".var/app/org.kicad.KiCad/cache/tmp/kicad"]
    out = []
    for d in dirs:
        if d not in out:
            out.append(d)
    return out


def sockets(dirs: list[Path] | None = None) -> list[Path]:
    """Every live-API socket found, best first. KiCad names it ``api.sock`` and
    falls back to ``api-<pid>.sock`` when another instance already holds that
    name, so ``api.sock`` is preferred and the rest are ordered newest first."""
    found: list[tuple[int, float, Path]] = []
    for d in (dirs if dirs is not None else socket_dirs()):
        try:
            names = [n for n in os.listdir(d) if n.startswith("api") and n.endswith(".sock")]
        except OSError:
            continue
        for n in names:
            p = Path(d) / n
            try:
                found.append((0 if n == "api.sock" else 1, -p.stat().st_mtime, p))
            except OSError:
                continue
    found.sort(key=lambda t: (t[0], t[1], str(t[2])))
    out = []
    for _a, _b, p in found:
        if p not in out:
            out.append(p)
    return out


def socket_path() -> Path | None:
    env = os.environ.get("KICAD_API_SOCKET")
    if env:
        p = env[len("ipc://"):] if env.startswith("ipc://") else env
        return Path(p) if os.path.exists(p) else None
    s = sockets()
    return s[0] if s else None


def capability() -> dict:
    try:
        import kipy  # noqa: F401
        has_kipy = True
        ver = getattr(kipy, "__version__", None)
    except Exception:  # noqa: BLE001
        has_kipy, ver = False, None
    sock = socket_path()
    return {"kipy": has_kipy, "kipy_version": ver, "socket": str(sock) if sock else None,
            "sockets": [str(s) for s in sockets()],
            "available": bool(has_kipy and sock),
            "hint": None if has_kipy and sock else
            ("enable Preferences ▸ Plugins ▸ 'Enable KiCad API' and open the PCB editor" if has_kipy else
             "live mode needs the optional `kicad-python` package (pip install kicad-python); file mode works without it")}


def read_live(limit: int = 500) -> dict:
    """Footprints/nets/track counts of the board open in the running PCB editor."""
    cap = capability()
    if not cap["available"]:
        raise RuntimeError(cap["hint"])
    from kipy import KiCad  # type: ignore
    k = KiCad(timeout_ms=5000)
    b = k.get_board()
    fps = []
    for fp in b.get_footprints()[:limit]:
        try:
            pos = fp.position
            fps.append({"ref": fp.reference_field.text.value, "value": fp.value_field.text.value,
                        "x": round(pos.x / 1e6, 4), "y": round(pos.y / 1e6, 4),
                        "rot": round(fp.orientation.degrees, 3),
                        "side": "bottom" if "B_" in str(fp.layer) or str(fp.layer).endswith("B_Cu") else "top"})
        except Exception:  # noqa: BLE001
            continue
    nets = [n.name for n in b.get_nets()]
    return {"source": "live", "document": getattr(b, "name", None), "footprints": fps,
            "footprint_count": len(fps), "nets": nets[:limit], "tracks": len(b.get_tracks()),
            "vias": len(b.get_vias()), "zones": len(b.get_zones())}
