"""`copper` — the Copper for KiCad command line.

    copper                      MCP stdio server when launched by an AI client (stdin is a pipe)
    copper serve                MCP stdio server (explicit)
    copper setup [--yes]        first-run setup in your browser (terminal wizard with --tty or without a desktop):
                                KiCad found, what to bring along from it, AI clients, GitHub, autorouter, test call
    copper setup --undo         put back everything the last KiCad import changed
    copper link [target…]       connect Copper to AI clients (and optionally GitHub); --list, --verify, --unlink
    copper install --client all|claude-desktop,codex,…  [--dry-run]
    copper uninstall [--client …] [--restore-backups] [--dry-run]
    copper doctor [--json]      health checks with fixes
    copper status               what is installed and registered where
    copper config [--client X]  print the JSON/TOML snippet to paste into any client by hand
    copper render <file>        quick PNG of a schematic / board for humans
    copper kicad-api enable|disable|status
    copper freerouting [--install]
    copper canvas [project] [--port N] [--no-open]   Copper Canvas: point at parts in the browser for the AI
    copper version
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import BRAND_ACCENT, DISPLAY_NAME, SERVER_NAME, __version__

IS_WIN = sys.platform == "win32"


# ── terminal styling ──────────────────────────────────────────────────────
def _enable_vt() -> bool:
    if not IS_WIN:
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:  # noqa: BLE001
        return False


class UI:
    def __init__(self, stream=None, color: bool | None = None):
        self.out = stream or sys.stdout
        try:
            self.out.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        if os.environ.get("COPPER_DEMO_HOME"):
            from .link import redact          # --demo: paths read as a generic home (C:\Users\you)
            self.out = redact.ScrubbingStream(self.out)
        from . import invocation
        if invocation.via_npx():              # no `copper` on PATH: hints name `npx -y copper-kicad <cmd>`
            self.out = invocation.CommandStream(self.out)
        tty =hasattr(self.out, "isatty") and self.out.isatty()
        if color is None:
            color = tty and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb" and _enable_vt()
        self.color = bool(color)
        r, g, b = (int(BRAND_ACCENT[i:i + 2], 16) for i in (1, 3, 5))
        self._accent = f"\x1b[38;2;{r};{g};{b}m"

    def _c(self, code, s):
        return f"{code}{s}\x1b[0m" if self.color else s

    def accent(self, s):
        return self._c(self._accent, s)

    def bold(self, s):
        return self._c("\x1b[1m", s)

    def dim(self, s):
        return self._c("\x1b[2m", s)

    def green(self, s):
        return self._c("\x1b[38;2;94;186;125m", s)

    def yellow(self, s):
        return self._c("\x1b[38;2;230;180;80m", s)

    def red(self, s):
        return self._c("\x1b[38;2;232;98;92m", s)

    def p(self, *a, end="\n"):
        print(*a, file=self.out, end=end)
        self.out.flush()

    def banner(self, subtitle=""):
        mark = self.accent("━━●━━")
        self.p()
        self.p(f"  {mark}  {self.bold(DISPLAY_NAME)} {self.dim(__version__)}" + (f"  {self.dim('·')}  {subtitle}" if subtitle else ""))
        self.p()

    def section(self, title):
        self.p(f"\n  {self.accent('▍')}{self.bold(title)}")

    def mark(self, status):
        return {"ok": self.green("✓"), "warn": self.yellow("!"), "fail": self.red("✗"),
                "skip": self.dim("–"), "info": self.accent("•")}.get(status, "•")

    @property
    def cols(self) -> int:
        """Terminal width for wrapping; piped output (logs, tests, scripts) is never wrapped."""
        try:
            if not (hasattr(self.out, "isatty") and self.out.isatty()):
                return 100000
        except (ValueError, OSError):
            return 100000
        try:
            import shutil
            return max(60, min(shutil.get_terminal_size((100, 24)).columns, 120))
        except Exception:  # noqa: BLE001
            return 100

    def _wrap(self, text: str, indent: int) -> list[str]:
        """``text`` broken to the terminal width; continuation lines are indented by the caller."""
        import textwrap
        room = max(30, self.cols - indent - 1)
        out = []
        for para in str(text).splitlines() or [""]:
            out += textwrap.wrap(para, room, break_long_words=False, break_on_hyphens=False) or [""]
        return out

    def line(self, status, label, detail="", fix=None, width=26):
        lab = f"{label:<{width}}"
        if len(label) > width:                       # a long label gets its own line
            self.p(f"   {self.mark(status)} {label}")
            lab = " " * width
        pad = 5 + width + 1
        parts = self._wrap(detail, pad) if detail else [""]
        style = self.dim if status in ("ok", "skip") else (lambda s: s)
        self.p(f"   {self.mark(status)} {lab} {style(parts[0])}".rstrip())
        for ln in parts[1:]:
            self.p(" " * pad + style(ln))
        if fix and status in ("warn", "fail"):
            for n, ln in enumerate(self._wrap(fix, pad + 2)):
                self.p(" " * pad + (f"{self.accent('→')} " if n == 0 else "  ") + ln)

    def say(self, text, indent=3, style=None):
        """A paragraph wrapped to the terminal."""
        for ln in self._wrap(text, indent):
            self.p(" " * indent + (style(ln) if style else ln))

    def ask(self, question, default=True, assume=None) -> bool:
        if assume is not None:
            return assume
        hint = "[Y/n]" if default else "[y/N]"
        try:
            ans = input(f"   {self.accent('?')} {question} {self.dim(hint)} ").strip().lower()
        except EOFError:
            return default
        if not ans:
            return default
        return ans in ("y", "yes")

    def choose(self, question, n: int, default: int = 1) -> int:
        """A number 1..n (Enter or anything unreadable = ``default``)."""
        try:
            ans = input(f"   {self.accent('?')} {question} ").strip()
        except EOFError:
            return default
        try:
            v = int(ans)
        except ValueError:
            return default
        return v if 1 <= v <= n else default


# ── commands ──────────────────────────────────────────────────────────────
def cmd_serve(args) -> int:
    from .mcp import server
    server.main()
    return 0


def cmd_credits(args) -> int:
    """What recent sessions' Copper tool traffic cost (estimated tokens), without spending any."""
    from .mcp import ledger
    print(ledger.report(max(1, args.last)), end="")
    return 0


def cmd_version(args) -> int:
    print(f"copper {__version__}")
    if getattr(args, "verbose", False):
        print(f"python {sys.version.split()[0]} at {sys.executable}")
        print(f"package {Path(__file__).resolve().parent}")
    return 0


def _print_doctor(ui: UI, checks, compact: bool = False) -> int:
    """Every check grouped (``copper doctor``), or with ``compact`` only what needs attention plus one
    line for everything that passed (the end of ``copper setup``)."""
    groups = [("core", "Core"), ("pcb", "PCB engine"), ("clients", "AI clients"), ("links", "Linked accounts"),
              ("network", "Network (parts)"), ("optional", "Optional")]
    fails = [c for c in checks if c.status == "fail"]
    warns = [c for c in checks if c.status == "warn"]
    route = next((c for c in checks if c.id == "freerouting"), None)
    if compact:
        passed = [c for c in checks if c.status in ("ok", "skip")]
        ui.line("ok", f"{len(passed)} checks passed", ", ".join(c.label for c in passed[:6]) + (" …" if len(passed) > 6 else ""))
        for c in fails + warns:
            ui.line(c.status, c.label, c.detail, c.fix)
        return 1 if fails else 0
    for gid, title in groups:
        cs = [c for c in checks if c.group == gid]
        if not cs:
            continue
        ui.section(title)
        for c in cs:
            ui.line(c.status, c.label, c.detail, c.fix)
    ui.p()
    if fails:
        ui.p(f"  {ui.red('✗')} {len(fails)} problem{'s' if len(fails) != 1 else ''} to fix"
             + (f", {len(warns)} warning{'s' if len(warns) != 1 else ''}" if warns else "") + ".")
    else:
        ui.p(f"  {ui.green('✓')} {ui.bold('Copper is ready.')}" + (f" {ui.dim(f'({len(warns)} warning' + ('' if len(warns) == 1 else 's') + ')')}" if warns else ""))
    if route is not None and route.status != "ok":
        # not optional in practice: the loop pcb_place → pcb_route → pcb_pour → fab_export needs it
        ui.p(f"  {ui.yellow('!')} Autorouting (pcb_route) is not installed yet. Run: "
             f"{ui.accent('copper freerouting --install')} {ui.dim('(about 120 MB, once)')}")
    ui.p()
    return 1 if fails else 0


def cmd_doctor(args) -> int:
    from .install import doctor
    if getattr(args, "fix", False):
        from .kicad import compat_report
        r = compat_report.fix_nested_config_dir()
        if not args.json and (r.get("removed") or r.get("path")):
            print(("removed " + r["path"]) if r.get("removed") else f"left {r['path']} alone: {r.get('reason')}")
    checks = doctor.run(fast=args.fast, network=not args.offline)
    if args.json:
        print(json.dumps(doctor.summarize(checks), indent=1))
        return 0 if not any(c.status == "fail" for c in checks) else 1
    ui = UI()
    ui.banner("doctor")
    rc = _print_doctor(ui, checks)
    if args.pause:
        try:
            input("  Press Enter to close… ")
        except EOFError:
            pass
    return rc


def _print_changes(ui: UI, res: dict, show_diff: bool):
    if not res["clients"]:
        ui.line("warn", "No clients", "no AI app found on this computer. Installed one since? Name it explicitly:")
        for cid in ("claude-desktop", "claude-code", "codex", "cursor"):
            ui.p(f"     {ui.accent('→')} copper install --client {cid}")
        return
    for ch in res["changes"]:
        st = {"added": "ok", "updated": "ok", "unchanged": "skip", "removed": "ok", "restored": "ok",
              "error": "fail", "absent": "skip"}.get(ch["action"], "info")
        verb = ch["action"] if not res.get("dry_run") else f"would be {ch['action']}" if ch["action"] not in ("unchanged",) else "unchanged"
        ui.line(st, ch["client"], f"{verb} — {ch['path']}" + (f" ({ch['detail']})" if ch.get("detail") else ""), width=16)
    if show_diff and res.get("diffs"):
        for d in res["diffs"]:
            ui.p()
            for ln in d.splitlines():
                ui.p("     " + (ui.green(ln) if ln.startswith("+") and not ln.startswith("+++") else
                               ui.red(ln) if ln.startswith("-") and not ln.startswith("---") else ui.dim(ln)))


def cmd_install(args) -> int:
    from .install import clients
    ui = UI()
    try:
        res = clients.install(args.client, dry_run=args.dry_run, launcher=args.launcher)
    except ValueError as e:
        ui.p(f"  {ui.red('✗')} {e}")
        return 2
    if args.json:
        print(json.dumps(res, indent=1))
        return 0 if not any(c["action"] == "error" for c in res["changes"]) else 1
    ui.banner("install" + (" (dry run)" if args.dry_run else ""))
    ui.p(f"   {ui.dim('server:')} {' '.join(json.dumps(a) if ' ' in a else a for a in [res['server']['command'], *res['server']['args']])}")
    ui.section("Clients")
    _print_changes(ui, res, show_diff=args.dry_run or args.diff)
    changed = {ch["client"] for ch in res["changes"] if ch["action"] in ("added", "updated")}
    hints = [f"{clients.BY_ID[c].name}: {h}" for c, h in res["restart"].items() if h and c in changed]
    if res["changes"] and not changed and not any(ch["action"] == "error" for ch in res["changes"]):
        ui.p(f"\n   {ui.dim('Already registered everywhere — nothing to restart.')}")
    if hints and not args.dry_run:
        ui.section("Next")
        for h in hints:
            ui.p(f"   {ui.accent('•')} {h}")
    ui.p()
    return 0 if not any(c["action"] == "error" for c in res["changes"]) else 1


def cmd_uninstall(args) -> int:
    from .install import clients
    ui = UI()
    try:
        res = clients.uninstall(args.client, dry_run=args.dry_run, restore_backups=args.restore_backups)
    except ValueError as e:
        ui.p(f"  {ui.red('✗')} {e}")
        return 2
    extra: dict = {}
    if getattr(args, "accounts", False) or getattr(args, "purge", False):
        from .link import github
        extra["accounts"] = {"github": {"unlinked": False}} if args.dry_run else {"github": github.unlink()}
    if getattr(args, "purge", False):
        from .install import linkcli
        extra["purge"] = linkcli.purge(dry_run=args.dry_run)
    if args.json:
        print(json.dumps({**res, **extra}, indent=1))
        return 0
    ui.banner("uninstall" + (" (dry run)" if args.dry_run else ""))
    if not res["changes"]:
        ui.p("   Copper was not registered with any client.")
    else:
        _print_changes(ui, res, show_diff=args.dry_run)
    if "accounts" in extra:
        g = extra["accounts"]["github"]
        ui.line("ok" if g.get("unlinked") else "skip", "GitHub link", "removed" if g.get("unlinked") else "was not linked", width=16)
    if "purge" in extra:
        pg = extra["purge"]
        ui.section("Copper's data folder")
        ui.p(f"   {ui.dim(pg['home'])}: {len(pg['removed'])} item(s) {'would be ' if args.dry_run else ''}removed")
        kept_why = {h.split(" holds ", 1)[0]: h for h in pg.get("held") or []}
        for left in pg["left"]:
            if left in kept_why:
                ui.line("warn", "kept", kept_why[left], "copper setup --undo", width=6)
            else:
                ui.p(f"   {ui.accent('•')} left alone (not Copper's): {left}")
    if res.get("backups") and not args.restore_backups:
        ui.section("Backups kept")
        ui.p(f"   {ui.dim('Your configs as they were before Copper first edited them (delete them when no longer needed):')}")
        for b in res["backups"]:
            ui.p(f"   {ui.accent('•')} {b}")
    ui.p()
    return 0


def cmd_config(args) -> int:
    from .install import clients
    spec = clients.server_spec(args.launcher)
    if args.client in (None, "json", "generic", "mcp", "stdio"):       # any MCP client: the plain stdio entry
        print(json.dumps({"mcpServers": {SERVER_NAME: spec.entry()}}, indent=2))
    elif args.client == "codex":
        print(clients.toml_block(f"mcp_servers.{SERVER_NAME}", spec, clients.CODEX_EXTRA), end="")
    elif clients.ALIASES.get(args.client, args.client) == "claude-code":
        # the supported way to add a user-scope server; quoting differs per shell, so show both
        js = json.dumps(clients.BY_ID["claude-code"].shape(spec))
        print("# bash / zsh / Git Bash")
        sq = "'"
        print(f"claude mcp add-json {SERVER_NAME} -s user '{js.replace(sq, sq + chr(92) + sq + sq)}'")
        # PowerShell 7.3+ passes native arguments verbatim (PSNativeCommandArgumentPassing=Standard);
        # Windows PowerShell 5.1 strips unescaped inner double quotes.
        print("# PowerShell 7.3+ (pwsh)")
        print(f"claude mcp add-json {SERVER_NAME} -s user '{js.replace(sq, sq + sq)}'")
        print("# Windows PowerShell 5.1 (inner quotes escaped)")
        ps = js.replace(chr(34), chr(92) + chr(34)).replace(sq, sq + sq)
        print(f"claude mcp add-json {SERVER_NAME} -s user '{ps}'")
        print("# easiest, any shell: copper install --client claude-code")
    elif args.client == "cursor-link":
        print(clients.cursor_deeplink(spec))
    elif args.client == "vscode-link":
        print(clients.vscode_link(spec))
    else:
        try:
            c = clients.resolve_names([args.client])[0]
        except ValueError as e:
            print(f"copper config: {e}. Use --client generic for a plain stdio config any MCP client accepts.", file=sys.stderr)
            return 2
        t = c.targets()[0]
        print(f"// {t.path}")
        print(json.dumps({t.key_path[0]: {SERVER_NAME: c.shape(spec)}}, indent=2))
    return 0


def cmd_status(args) -> int:
    from .install import clients
    ui = UI()
    data: dict = {"version": __version__, "python": sys.executable, "server": clients.server_spec().entry()}
    try:
        from .kicad import env
        k = env.kicad()
        data["kicad"] = {"version": k.full_version, "cli": str(k.cli), "reason": k.reason}
        from .kicad import compat, compat_report
        adv = [a["text"] for a in compat.advisories(k.ver) if a["severity"] == "warn"]
        adv += [r["detail"] for r in compat_report.update_check(network=True) if r["status"] == "warn"]
        if adv:
            data["kicad"]["advisories"] = adv
    except Exception as e:  # noqa: BLE001
        data["kicad"] = {"error": str(getattr(e, "message", e))}
    data["clients"] = [{k: e[k] for k in ("client", "path", "status", "detail")}
                       for e in clients.entries() if e["status"] != "absent"]
    try:
        from . import tools  # noqa: F401
        from .mcp import registry
        data["tools"] = len(registry.TOOLS)
        data["tool_load_errors"] = list(getattr(tools, "LOAD_ERRORS", {}) or {})
    except Exception as e:  # noqa: BLE001
        data["tools"] = 0
        data["tool_load_errors"] = [str(e)]
    try:
        from . import project
        st = project._state()
        data["active_project"] = st.get("active") or st.get("current")
    except Exception:  # noqa: BLE001
        pass
    if args.json:
        print(json.dumps(data, indent=1, default=str))
        return 0
    ui.banner("status")
    ui.line("ok", "Copper", f"{__version__} — {sys.executable}", width=16)
    kd = data["kicad"]
    ui.line("ok" if "version" in kd else "fail", "KiCad", kd.get("version") and f"{kd['version']} — {kd['cli']}" or kd.get("error", ""), width=16)
    ui.line("ok" if data["tools"] and not data["tool_load_errors"] else "warn", "Tools",
            f"{data['tools']} loaded" + (f"; failed: {', '.join(data['tool_load_errors'])}" if data["tool_load_errors"] else ""), width=16)
    if data.get("active_project"):
        ui.line("info", "Project", str(data["active_project"]), width=16)
    ui.section("Registered with")
    if not data["clients"]:
        ui.p(f"   {ui.dim('nothing yet —')} copper install --client all")
    for e in data["clients"]:
        ui.line(e["status"], e["client"], e["path"], width=16)
    ui.p()
    return 0


def cmd_render(args) -> int:
    from . import render
    ui = UI()
    f = Path(args.file).expanduser().resolve()
    if f.suffix == ".kicad_pro":
        cand = f.with_suffix(".kicad_pcb") if args.pcb or args.three_d else f.with_suffix(".kicad_sch")
        f = cand
    if not f.exists():
        ui.p(f"  {ui.red('✗')} not found: {f}")
        return 2
    out = Path(args.out).expanduser().resolve() if args.out else f.parent / "copper-render"
    try:
        if f.suffix == ".kicad_sch":
            imgs = render.schematic(f, out, width=args.width)
        elif f.suffix == ".kicad_pcb":
            imgs = [render.pcb_3d(f, out, view=args.view) if args.three_d
                    else render.pcb_layers(f, out, layers=args.layers.split(",") if args.layers else None,
                                           width=args.width, name=f"{f.stem}-pcb")]
        else:
            ui.p(f"  {ui.red('✗')} expected a .kicad_sch, .kicad_pcb or .kicad_pro file")
            return 2
    except Exception as e:  # noqa: BLE001
        ui.p(f"  {ui.red('✗')} {getattr(e, 'message', e)}")
        if getattr(e, "fix", None):
            ui.p(f"    {ui.accent('→')} {e.fix}")
        return 1
    for im in imgs:
        ui.p(f"  {ui.green('✓')} {im.path}")
        if args.open:
            _open(im.path)
    return 0


def _open(path: str):
    try:
        if IS_WIN:
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.spawnlp(os.P_NOWAIT, "open", "open", path)
        else:
            os.spawnlp(os.P_NOWAIT, "xdg-open", "xdg-open", path)
    except Exception:  # noqa: BLE001
        pass


def cmd_kicad_api(args) -> int:
    from .install import kicad_setup
    ui = UI()
    if args.action == "status":
        s = kicad_setup.api_status()
        ui.line("ok" if s.get("enabled") else "warn", "KiCad IPC API", f"{'enabled' if s.get('enabled') else 'disabled'} — {s['path']}")
        return 0
    r = kicad_setup.set_api(args.action == "enable", force=args.force)
    if r.get("refused"):
        ui.line("fail", "KiCad IPC API", r["refused"])
        return 1
    ui.line("ok", "KiCad IPC API", f"{'enabled' if r.get('enabled') else 'disabled'}"
            f"{'' if r.get('changed') else ' (already)'} — {r['path']}")
    return 0


def cmd_freerouting(args) -> int:
    from .install import freerouting
    ui = UI()
    if args.install:
        try:
            r = freerouting.install(progress=lambda m: ui.p("   " + ui.dim(m)))
        except Exception as e:  # noqa: BLE001
            ui.line("fail", "Freerouting", f"{type(e).__name__}: {e}")
            return 1
    else:
        r = freerouting.find()
    if args.json:
        print(json.dumps(r, indent=1))
    else:
        if r["ok"]:
            ui.line("ok", "Freerouting", f"{r.get('jar') or '-'} · java {r.get('java_version') or '-'}")
        elif r.get("available"):
            ui.line("ok", "Freerouting", freerouting.describe(r))
            ui.p("     " + ui.dim("to download it now (e.g. before going offline): copper freerouting --install"))
        else:
            ui.line("warn", "Freerouting", freerouting.describe(r), r.get("fix"))
    return 0 if (r["ok"] or r.get("available")) else 1


def cmd_canvas(args) -> int:
    # imported lazily: the canvas (http server, SVG plotting) is only needed by this command
    from .focus import canvas
    return int(canvas.serve(args.project, args.port, not args.no_open) or 0)


def _can_browse() -> bool:
    """A desktop session where opening a browser tab is plausible."""
    if os.environ.get("COPPER_NO_BROWSER"):
        return False
    if IS_WIN or sys.platform == "darwin":
        return not os.environ.get("SSH_CONNECTION")
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and not os.environ.get("SSH_CONNECTION")


def cmd_link(args) -> int:
    from .install import linkcli
    if _WINDOWLESS and not (args.targets or args.list or args.verify or args.unlink or args.all or args.backend or args.yes):
        from .install import onboard
        return onboard.serve(launcher=args.launcher, windowless=True)
    return linkcli.cmd_link(args, UI(), _interactive_stdin())


def cmd_setup(args) -> int:
    from .install import clients, doctor, linkcli
    if args.undo:
        return linkcli.cmd_undo(UI(), force=args.force, dry_run=args.dry_run, as_json=args.json)
    # Front end: the browser page when someone is there to click (or there is no console at all);
    # the terminal wizard with --tty, with --yes, when input is piped, or without a desktop.
    scripted = (args.yes or args.quiet or args.tty or args.clients != "ask" or args.api != "ask"
                or args.imports is not None or args.no_libs or args.no_doctor or args.freerouting)
    if args.web or _WINDOWLESS or (not scripted and _interactive_stdin() and _can_browse()):
        from .install import onboard
        if not (args.web or _WINDOWLESS):
            UI().p("   Opening Copper setup in your browser. Prefer the terminal? Run: copper setup --tty")
        return onboard.serve(open_browser_=not args.no_open, port=args.port, launcher=args.launcher,
                             windowless=_WINDOWLESS)
    ui = UI()
    yes = True if args.yes else None
    quiet = args.quiet
    if not quiet:
        ui.banner("setup")
        told = args.imports is not None or args.api == "enable"
        ui.p("   " + ui.dim("Connects KiCad to your AI apps. " + (
            "--yes with --import/--api: applying exactly what was named" if args.yes and told else
            "--yes: applying only the safe defaults (nothing in KiCad's own settings is changed)" if args.yes else
            "Nothing is changed without asking") + "; every file we touch is backed up first."))

    # 1. KiCad (several installed: ask which)
    if not quiet:
        ui.section("KiCad")
    try:
        from .kicad import env
        k = env.kicad()
        if not quiet:
            ui.line("ok", "KiCad", f"{k.full_version or k.version} at {k.root}")
            linkcli.choose_kicad(ui, yes)
            k = env.kicad()
    except Exception as e:  # noqa: BLE001
        ui.line("fail", "KiCad", str(getattr(e, "message", e)), getattr(e, "fix", None))
        k = None

    # 2. coming from KiCad: libraries, themes, hotkeys, plugins, recent projects, design rules, IPC API.
    #    Asked item by item; --yes applies only what leaves KiCad's own files alone.
    if k is not None:
        if not quiet:
            ui.section("Your KiCad setup")
        if quiet:
            import io
            sink = UI(io.StringIO(), color=False)
            linkcli.import_step(sink, yes, args.imports, args.source, args.no_libs, args.api)
        else:
            linkcli.import_step(ui, yes, args.imports, args.source, args.no_libs, args.api)

    # 3. AI clients: one answer connects every client found, then each is launch-tested
    if args.clients != "none":
        if not quiet:
            ui.section("AI apps")
        if args.clients and args.clients != "ask":
            chosen = clients.resolve_names(args.clients)
        else:
            found = [c for c in clients.CLIENTS if c.detected()]
            if not found:
                ui.line("warn", "None detected", "install Claude Desktop, Claude Code, Codex or Cursor, then `copper link apps`")
                chosen = []
            else:
                for c in found:
                    ui.line("info", c.name, ", ".join(str(t.path) for t in c.targets()), width=20)
                if ui.ask(f"Connect Copper to {'these' if len(found) > 1 else 'this'} {len(found)} app{'s' if len(found) > 1 else ''}?", True, yes):
                    chosen = found
                elif yes is None:
                    chosen = [c for c in found if ui.ask(f"  … {c.name}?", False)]
                else:
                    chosen = []
        if chosen:
            from .link import apps
            res = apps.connect([c.id for c in chosen], launcher=args.launcher)
            linkcli._print_connect(ui, res)

    # 4. GitHub (optional, never under --yes)
    if yes is None and not quiet:
        from .link import github
        ui.section("GitHub")
        g = github.status()
        if g["linked"]:
            ui.line("ok", "GitHub", g["detail"], width=20)
        elif ui.ask("Link GitHub? Only needed to publish designs from Copper; you can do it later with `copper link github`", False):
            linkcli.link_github(ui, _interactive_stdin())
        else:
            ui.line("skip", "GitHub", "not linked (copper link github)", width=20)

    # 5. Freerouting
    from .install import freerouting
    ask_fr = not args.yes and not quiet and k is not None and not freerouting.find()["ok"]
    if ask_fr or (args.freerouting and not quiet):
        ui.section("Autorouter")
    if args.freerouting or (ask_fr and
                            ui.ask("Download the Freerouting autorouter + Java runtime (about 120 MB) now? "
                                   "(otherwise the first pcb_route downloads it)", True)):
        try:
            r = freerouting.install(progress=lambda m: ui.p("   " + ui.dim(m)))
            ui.line("ok" if r["ok"] else "warn", "Freerouting", r.get("jar") or r.get("problem") or "")
        except Exception as e:  # noqa: BLE001
            ui.line("warn", "Freerouting", f"download failed: {e}", "retry later with `copper freerouting --install`")

    # 6. doctor, then one real call through the registered command
    if args.no_doctor:
        return 0
    if not quiet:
        ui.section("Checking everything")
    checks = doctor.run(fast=False, network=True)
    if quiet:
        return _print_doctor(ui, checks)
    rc = _print_doctor(ui, checks, compact=True)
    t = None
    if rc == 0:
        from .link import apps
        t = apps.test_call(args.launcher)
        ui.line("ok" if t["ok"] else "fail", "Test call", t["detail"] if t["ok"] else f"failed at {t['stage']}: {t['detail']}",
                None if t["ok"] else "copper doctor")
        if not t["ok"]:
            rc = 1
    ui.p()
    if rc == 0:
        from .install.onboard import EXAMPLE_PROMPT
        from .link import apps as _apps
        con = [a for a in _apps.status() if a["state"] == "connected"]
        ui.p(f"  {ui.accent('━━●')} {ui.bold('Copper is ready.')}")
        ui.p()
        if con:
            ui.say("Restart " + (con[0]["name"] if len(con) == 1 else "your AI apps") + ", then ask:")
        else:
            ui.say("Connect an AI app (" + ui.accent("copper link") + "), then ask it:")
        ui.say("“" + EXAMPLE_PROMPT + "”", indent=5, style=ui.accent)
        ui.p()
    else:
        ui.p(f"  {ui.red('✗')} {ui.bold('Not ready yet.')} Fix what is marked above, then run {ui.accent('copper doctor')}.")
        ui.p()
    return rc


def cmd_winpath(args) -> int:
    """Add/remove a directory on the per-user PATH (Windows installer helper)."""
    if not IS_WIN:
        return 0
    import ctypes
    import winreg
    d = os.path.normpath(args.dir)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            cur, typ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            cur, typ = "", winreg.REG_EXPAND_SZ
        parts = [p for p in cur.split(";") if p]
        norm = [os.path.normcase(os.path.normpath(p)) for p in parts]
        changed = False
        if args.action == "add" and os.path.normcase(d) not in norm:
            parts.append(d)
            changed = True
        elif args.action == "remove" and os.path.normcase(d) in norm:
            parts = [p for p, n in zip(parts, norm) if n != os.path.normcase(d)]
            changed = True
        if changed:
            winreg.SetValueEx(key, "Path", 0, typ if typ in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else winreg.REG_EXPAND_SZ,
                              ";".join(parts))
            try:
                ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)
            except Exception:  # noqa: BLE001
                pass
    print(f"PATH {'updated' if changed else 'unchanged'}: {args.action} {d}")
    return 0


# ── parser ────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="copper", description=f"{DISPLAY_NAME} — KiCad for AI agents (MCP server + tools).")
    p.add_argument("-V", "--version", action="store_true", help="print version and exit")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    s = sub.add_parser("serve", help="run the MCP server on stdio (what AI clients launch)")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("setup", help="first-run setup: opens in your browser (--tty: terminal wizard; --undo: put a KiCad import back)")
    s.add_argument("-y", "--yes", action="store_true", help="accept all defaults (non-interactive)")
    s.add_argument("--clients", default="ask", help="ask | none | all | comma list of client ids")
    s.add_argument("--api", choices=["ask", "enable", "skip"], default="ask", help="KiCad IPC API handling")
    s.add_argument("--no-libs", action="store_true", help="don't create missing global library tables")
    s.add_argument("--freerouting", action="store_true", help="also download Freerouting + Java")
    s.add_argument("--no-doctor", action="store_true")
    s.add_argument("--quiet", action="store_true", help="only print changes and problems")
    s.add_argument("--web", action="store_true", help="open the setup page in your browser (the default on a desktop)")
    s.add_argument("--tty", action="store_true", help="use the terminal wizard instead of the browser page")
    s.add_argument("--no-open", action="store_true", help="with --web: print the address instead of opening the browser")
    s.add_argument("--port", type=int, default=0, help="with --web: port on 127.0.0.1 (default: a free one)")
    s.add_argument("--import", dest="imports", metavar="ITEMS",
                   help="what to bring along from KiCad without asking: safe (default with --yes) | all | none | comma list of "
                        "sym_libs,fp_libs,env_vars,themes,hotkeys,plugins,other_settings,recent,design_defaults,api")
    s.add_argument("--from", dest="source", metavar="VERSION", help="KiCad settings version to import from, e.g. 9.0")
    s.add_argument("--undo", action="store_true", help="put back everything the last KiCad import changed")
    s.add_argument("--force", action="store_true", help="with --undo: also restore files that changed since the import")
    s.add_argument("--dry-run", "-n", action="store_true", help="with --undo: show what would be put back")
    s.add_argument("--json", action="store_true", help="with --undo: machine-readable result")
    s.add_argument("--launcher", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("link", help="connect Copper to your AI clients (and optionally GitHub)",
                       description="Connect Copper to AI clients found on this computer, and optionally to GitHub. "
                                   "Targets: a client id, `apps` (every client found), `github`. "
                                   "Copper never asks for or stores a password.")
    s.add_argument("targets", nargs="*", help="client ids, `apps`, `github` (none: show status and offer to connect everything found)")
    s.add_argument("--list", "-l", action="store_true", help="show what is connected")
    s.add_argument("--verify", action="store_true", help="start Copper the way each client would and check that it answers")
    s.add_argument("--unlink", "-u", action="store_true", help="disconnect the named targets")
    s.add_argument("--all", action="store_true", help="every AI client found (with --unlink: every connection and the GitHub link)")
    s.add_argument("--yes", "-y", action="store_true", help="do not ask before connecting")
    s.add_argument("--no-verify", action="store_true", help="skip the launch check after connecting")
    s.add_argument("--dry-run", "-n", action="store_true", help="show what would change, change nothing")
    s.add_argument("--backend", action="store_true", help="which credential store would hold a GitHub token")
    s.add_argument("--allow-weak-store", action="store_true",
                   help="without an OS credential store, keep the GitHub token in a file protected by file permissions only")
    s.add_argument("--json", action="store_true")
    s.add_argument("--launcher", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_link)

    s = sub.add_parser("install", help="register Copper with AI clients")
    s.add_argument("--client", "-c", default="all", help="all (detected) or comma list: " + ", ".join(
        ["claude-desktop", "claude-code", "codex", "cursor", "vscode", "windsurf", "zed", "gemini", "copilot-cli"]))
    s.add_argument("--dry-run", "-n", action="store_true", help="show the diff, change nothing")
    s.add_argument("--diff", action="store_true", help="show diffs of what changed")
    s.add_argument("--launcher", help="absolute path of the copper executable to register (default: this one)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_install)

    s = sub.add_parser("uninstall", help="remove Copper from AI client configs")
    s.add_argument("--client", "-c", default="all")
    s.add_argument("--restore-backups", action="store_true", help="put back the *.copper-backup taken when Copper first edited each file (DISCARDS any other change made since; preview with --dry-run)")
    s.add_argument("--dry-run", "-n", action="store_true")
    s.add_argument("--yes", "-y", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--accounts", action="store_true", help="also remove the GitHub link (and its token) from this computer")
    s.add_argument("--purge", action="store_true",
                   help="--accounts, plus delete Copper's own data (autorouter download, cache, state); your files there are left alone")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("doctor", help="check everything Copper depends on")
    s.add_argument("--json", action="store_true")
    s.add_argument("--fast", action="store_true", help="skip the pcbnew import check")
    s.add_argument("--offline", action="store_true", help="skip network checks")
    s.add_argument("--fix", action="store_true", help="remove the empty KiCad config folder an old Copper left behind")
    s.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("status", help="installed version, KiCad and registered clients")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("config", help="print a config snippet for manual setup")
    s.add_argument("--client", "-c", help="generic (default; plain stdio JSON for any MCP client) | codex | cursor-link | vscode-link | any client id")
    s.add_argument("--launcher", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("render", help="render a schematic or board to PNG")
    s.add_argument("file")
    s.add_argument("--out", "-o", help="output directory (default: <file dir>/copper-render)")
    s.add_argument("--pcb", action="store_true", help="for a .kicad_pro, render the board")
    s.add_argument("--3d", dest="three_d", action="store_true", help="raytraced 3D view of a board")
    s.add_argument("--view", default="iso", choices=["iso", "top", "bottom", "iso_bottom", "front"])
    s.add_argument("--layers", help="comma list of PCB layers")
    s.add_argument("--width", type=int, default=1600)
    s.add_argument("--open", action="store_true", help="open the PNG afterwards")
    s.set_defaults(fn=cmd_render)

    s = sub.add_parser("kicad-api", help="enable/disable KiCad's IPC scripting API")
    s.add_argument("action", choices=["enable", "disable", "status"])
    s.add_argument("--force", action="store_true", help="even while KiCad is running (KiCad may undo it on exit)")
    s.set_defaults(fn=cmd_kicad_api)

    s = sub.add_parser("freerouting", help="locate or install the Freerouting autorouter")
    s.add_argument("--install", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_freerouting)

    s = sub.add_parser("canvas", help="open Copper Canvas: click or box-select parts so your AI knows what you mean")
    s.add_argument("project", nargs="?", help="project folder or .kicad_pro/.kicad_sch/.kicad_pcb (default: the active project)")
    s.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: a free one)")
    s.add_argument("--no-open", action="store_true", help="print the URL instead of opening the browser")
    s.set_defaults(fn=cmd_canvas)

    s = sub.add_parser("credits", help="estimated AI-credit cost of Copper's tool traffic in recent sessions")
    s.add_argument("--last", type=int, default=5, help="how many recent sessions to print (default 5)")
    s.set_defaults(fn=cmd_credits)

    s = sub.add_parser("version", help="print version")
    s.add_argument("--verbose", "-v", action="store_true")
    s.set_defaults(fn=cmd_version)

    for name in DEMO_COMMANDS:       # applied by _enter_demo() before parsing; listed here for --help
        sub.choices[name].add_argument("--demo", nargs="?", const="", metavar="DIR", help=DEMO_HELP)
    return p


def _winpath_parser() -> argparse.ArgumentParser:
    # Installer helper, deliberately not a subcommand: argparse lists subparsers
    # created with help=SUPPRESS as "==SUPPRESS==" in `copper --help`.
    p = argparse.ArgumentParser(prog="copper _winpath")
    p.add_argument("action", choices=["add", "remove"])
    p.add_argument("dir")
    return p


_PTY_PIPE = r"\\(?:cygwin|msys)-[0-9a-f]+-pty\d+-(?:from|to)-master"


def _mintty_pipe(stream) -> bool:
    """True when ``stream`` is a Cygwin/MSYS pty pipe (Git Bash's mintty, MSYS2):
    an interactive terminal that ``isatty()`` reports as a plain pipe on Windows."""
    if not IS_WIN or stream is None:
        return False
    try:
        import ctypes
        import msvcrt
        import re
        from ctypes import wintypes
        h = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
        k32 = ctypes.windll.kernel32
        if k32.GetFileType(h) != 3:                      # FILE_TYPE_PIPE
            return False

        class _NameInfo(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("name", wintypes.WCHAR * 512)]

        info = _NameInfo()
        if not k32.GetFileInformationByHandleEx(h, 2, ctypes.byref(info), ctypes.sizeof(info)):  # FileNameInfo
            return False
        return bool(re.search(_PTY_PIPE, info.name[:info.length // 2]))
    except Exception:  # noqa: BLE001
        return False


def _interactive_stdin() -> bool:
    try:
        return sys.stdin is not None and (sys.stdin.isatty() or _mintty_pipe(sys.stdin))
    except (AttributeError, ValueError, OSError):
        return False


def _sandbox_kicad_config():
    """COPPER_CONFIG_HOME_OVERRIDE is the test/sandbox switch for every user
    config Copper writes. KiCad's config (library tables, kicad_common.json for
    the IPC API) and COPPER_HOME (the ~120 MB Freerouting download) follow it
    too, or `copper setup` in "test mode" would edit the real KiCad settings.
    An explicit KICAD_CONFIG_HOME / COPPER_HOME still wins."""
    o = os.environ.get("COPPER_CONFIG_HOME_OVERRIDE")
    if not o:
        return
    from .install import clients
    # Copper's own data (Freerouting download, state) too, matching clients.copper_home()
    os.environ.setdefault("COPPER_HOME", str(clients.copper_home()))
    if os.environ.get("KICAD_CONFIG_HOME"):
        return
    if IS_WIN:
        root = clients.appdata() / "kicad"
    elif sys.platform == "darwin":
        root = clients.home() / "Library" / "Preferences" / "kicad"
    else:
        root = clients.xdg_config() / "kicad"
    os.environ["KICAD_CONFIG_HOME"] = str(root)


_WINDOWLESS = False


def _windowless_guard() -> bool:
    """`copperw.exe` / `pythonw` have no console: stdout, stderr and stdin are None and every print
    would crash where nobody can see it. Send output to ``$COPPER_HOME/logs/copperw.log`` instead."""
    global _WINDOWLESS
    if sys.stdout is not None and sys.stderr is not None:
        if sys.stdin is None:
            sys.stdin = open(os.devnull, encoding="utf-8")
        return False
    _WINDOWLESS = True
    try:
        from .install import clients
        d = clients.copper_home() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        log = d / "copperw.log"
        if log.exists() and log.stat().st_size > 256 * 1024:
            log.replace(d / "copperw.log.1")
        f = open(log, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        f = open(os.devnull, "w", encoding="utf-8")
    from .link import redact
    f = redact.ScrubbingStream(f)
    sys.stdout = sys.stdout or f
    sys.stderr = sys.stderr or f
    if sys.stdin is None:
        sys.stdin = open(os.devnull, encoding="utf-8")
    import time
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] copperw {' '.join(sys.argv[1:])}", flush=True)
    return True


def _windowless_fail(what: str):
    """A failure nobody can see is worse than a dialog: show one message box with the log path."""
    try:
        from .install import clients
        log = clients.copper_home() / "logs" / "copperw.log"
        if IS_WIN and not os.environ.get("COPPER_NO_BROWSER"):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"Copper could not finish: {what}\n\nDetails: {log}", DISPLAY_NAME, 0x10)
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    _enter_demo(argv)                    # before the windowless log: it lives in Copper's data folder
    if not _windowless_guard():
        return _main(argv)
    try:
        rc = _main(argv or ["setup", "--web"])      # no console and no command: never become an MCP server
    except SystemExit as e:                          # argparse error
        rc = e.code if isinstance(e.code, int) else 2
        if rc:
            _windowless_fail("the command line was not understood")
        raise
    except BaseException as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        _windowless_fail(f"{type(e).__name__}: {e}")
        return 1
    if rc:
        _windowless_fail(f"`copper {' '.join(argv)}` ended with code {rc}")
    return rc


DEMO_COMMANDS = ("setup", "link", "install", "uninstall", "doctor", "status", "config")
DEMO_HELP = ("try it without touching this computer: every config file, KiCad setting and download goes to DIR "
             "(default: a new temporary folder, filled with sample AI apps and KiCad 9 settings); "
             "paths are shown as a generic home such as C:\\Users\\you")


def _demo_dir(argv: list[str]) -> str | None:
    """The DIR of ``--demo [DIR]`` / ``--demo=DIR`` ("" = no DIR given), or None. Read before argparse
    because the demo home must be in place before anything computes a path (prefs, KiCad's config root)."""
    if not argv or argv[0] not in DEMO_COMMANDS:
        return None
    for n, a in enumerate(argv):
        if a == "--":
            break
        if a.startswith("--demo="):
            return a.split("=", 1)[1]
        if a == "--demo":
            nxt = argv[n + 1] if n + 1 < len(argv) else ""
            return "" if (not nxt or nxt.startswith("-")) else nxt
    return None


def _enter_demo(argv: list[str]) -> None:
    d = _demo_dir(argv)
    if d is None:
        return
    from .install import demo
    before = demo.active()
    home = demo.activate(d or None)
    if before != home and sys.stderr is not None:
        print(f"copper: demo mode. Everything stays in {home} (shown as {demo.shown_home()}).",
              file=sys.stderr, flush=True)


def _main(argv: list[str]) -> int:
    if argv and argv[0] == "_winpath":
        return cmd_winpath(_winpath_parser().parse_args(argv[1:]))
    _enter_demo(argv)
    try:
        from .install import prefs
        prefs.export_env()          # the KiCad chosen in setup (COPPER_KICAD_VERSION), for the server too
    except Exception:  # noqa: BLE001
        pass
    if argv and argv[0] != "serve":
        _sandbox_kicad_config()
    elif os.environ.get("COPPER_CONFIG_HOME_OVERRIDE"):
        # A sandboxed server (tests, the installer's test mode) must keep its state (active project,
        # recent list) inside the sandbox too; KiCad's own config is left as the caller set it.
        from .install import clients
        os.environ.setdefault("COPPER_HOME", str(clients.copper_home()))
    if not argv:
        # Launched by an MCP client (stdin is a pipe) → be the server. Someone who typed
        # bare `copper` in Git Bash/mintty also has a pipe, so look closer, and always say
        # on stderr what is happening (stdout belongs to JSON-RPC).
        if not _interactive_stdin():
            print(f"copper {__version__}: no command and stdin is not a terminal, so serving MCP on stdio "
                  "(Ctrl+C to quit; `copper --help` lists the commands).", file=sys.stderr, flush=True)
            return cmd_serve(None) or 0
        ui = UI()
        ui.banner()
        ui.p("   An MCP server that lets Claude, Codex and Cursor design real KiCad boards.")
        ui.p()
        for c, d in [("copper setup", "first-run setup (recommended)"),
                     ("copper doctor", "check that everything works"),
                     ("copper link", "connect every AI client found, and optionally GitHub"),
                     ("copper freerouting --install", "enable autorouting (pcb_route)"),
                     ("copper status", "what is registered where"),
                     ("copper render board.kicad_pcb", "quick PNG of a design"),
                     ("copper canvas", "point at parts in the browser for your AI"),
                     ("copper --help", "all commands")]:
            from . import invocation
            c = invocation.cmds(c)
            w = max(32, len(invocation.prog()) + 26)
            ui.p(f"   {ui.accent(c):<{w + 12}} {ui.dim(d)}" if ui.color else f"   {c:<{w}} {d}")
        ui.p()
        return 0
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version and not args.cmd:
        return cmd_version(argparse.Namespace(verbose=False))
    if not getattr(args, "fn", None):
        parser.print_help()
        return 0
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
