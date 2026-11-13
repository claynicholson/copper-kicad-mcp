"""Terminal front ends for `copper link` and the KiCad-transition step of `copper setup`.

Kept out of ``cli.py`` so that file stays a thin argument parser. Every function takes the ``UI`` object
from ``cli`` and returns a process exit code (or the data the wizard needs).
"""
from __future__ import annotations

import json
import sys

from .. import __version__

GITHUB = "github"
STATE_MARK = {"connected": "ok", "broken": "fail", "found": "warn", "not-found": "skip"}


# ── copper link ───────────────────────────────────────────────────────────
def _split_targets(names: list[str]) -> tuple[list[str], bool]:
    """(client ids, github?) from the words after `copper link`. Raises ValueError for unknown words."""
    from . import clients
    apps_, gh = [], False
    for raw in names:
        for n in [x for x in raw.replace(",", " ").split() if x]:
            k = n.lower()
            if k == GITHUB:
                gh = True
            elif k in ("apps", "all"):
                apps_ += [c.id for c in clients.resolve_names("all") if c.id not in apps_]
            else:
                k = clients.ALIASES.get(k, k)
                if k not in clients.BY_ID:
                    raise ValueError(f"unknown link target '{n}'. AI clients: {', '.join(clients.BY_ID)}, or `apps` "
                                     f"(every client found). Accounts: {GITHUB}.")
                if k not in apps_:
                    apps_.append(k)
    return apps_, gh


def link_status(verify: bool = False) -> dict:
    from ..link import apps, github, store
    g = github.gh_status()
    return {"version": __version__, "apps": apps.status(verify_launch=verify),
            "github": {**github.status(live=True, gh=g), "how": github.available(g)["how"]},
            "store": store.describe()}


def print_status(ui, st: dict):
    ui.section("AI apps")
    for a in st["apps"]:
        detail = a["detail"]
        if a["state"] == "connected" and not a["checked"] and not a.get("via_plugin"):
            detail += " (launch not tested; add --verify)"
        ui.line(STATE_MARK[a["state"]], a["name"], detail, a.get("fix"), width=20)
    ui.section("Accounts")
    g = st["github"]
    ui.line("ok" if g["linked"] else "skip", "GitHub", g["detail"], width=20)
    if not g["linked"]:
        ui.p(f"     {' ' * 20} {ui.dim('copper link github')}")
    s = st["store"]
    ui.p()
    ui.p("   " + ui.dim(f"Token store: {s['name']}" + ("" if s["available"] else " (GitHub can still be linked through the gh CLI)")))
    ui.p("   " + ui.dim("Copper never sees a password and does not link AI-vendor accounts: each app keeps its own login."))


def _print_connect(ui, res: dict) -> bool:
    from . import clients
    ok = True
    by_client: dict[str, list[dict]] = {}
    for v in res.get("verify", []):
        by_client.setdefault(v["client"], []).append(v)
    for ch in res["changes"]:
        name = clients.BY_ID[ch["client"]].name
        if ch["action"] == "error":
            ok = False
            ui.line("fail", name, f"could not write {ch['path']}: {ch.get('detail', '')}", width=20)
            continue
        vs = [v for v in by_client.get(ch["client"], []) if v["path"] == ch["path"]]
        if res.get("dry_run"):
            ui.line("info", name, f"would be {ch['action']} in {ch['path']}", width=20)
        elif vs and not vs[0]["ok"]:
            ok = False
            ui.line("fail", name, f"registered, but Copper does not start ({vs[0]['stage']}): {vs[0]['detail']}",
                    "copper doctor", width=20)
        else:
            verb = {"added": "connected", "updated": "reconnected", "unchanged": "already connected"}.get(ch["action"], ch["action"])
            ui.line("ok", name, verb + (f" · {vs[0]['detail']}" if vs else "") + f" — {ch['path']}", width=20)
    for s in res.get("skipped", []):
        ui.line("ok", clients.BY_ID[s["client"]].name, s["reason"], width=20)
    changed = {ch["client"] for ch in res["changes"] if ch["action"] in ("added", "updated")}
    hints = [f"{clients.BY_ID[c].name}: {h}" for c, h in res.get("restart", {}).items() if h and c in changed]
    if hints and not res.get("dry_run"):
        ui.section("Next")
        for h in hints:
            ui.p(f"   {ui.accent('•')} {h}")
    return ok


def link_github(ui, interactive: bool = True, allow_weak_store: bool = False) -> int:
    from ..link import github, store

    def on_code(info):
        ui.p()
        ui.p(f"   Open {ui.accent(info['verification_uri'])} and enter this code:")
        ui.p(f"       {ui.bold(info['user_code'])}")
        ui.p("   " + ui.dim("Waiting for you to finish on GitHub… (Ctrl+C cancels; Copper never sees your password)"))

    av = github.available()
    ui.p("   " + ui.dim(av["explain"]))
    try:
        r = github.link(interactive=interactive, on_code=on_code, allow_weak_store=allow_weak_store)
    except github.LinkError as e:
        ui.line("fail" if e.code not in ("no_route", "gh_logged_out") else "warn", "GitHub", e.message, e.fix, width=20)
        return 1
    except store.StoreError as e:
        ui.line("fail", "GitHub", str(e), width=20)
        return 1
    except KeyboardInterrupt:
        ui.p()
        ui.line("skip", "GitHub", "cancelled", width=20)
        return 130
    ui.line("ok", "GitHub", r["detail"], width=20)
    return 0


def cmd_link(args, ui, interactive: bool) -> int:
    from ..link import apps, github, store
    from . import clients
    if args.backend:
        s = store.describe()
        if args.json:
            print(json.dumps(s, indent=1))
        else:
            ui.line("ok" if s["available"] else "warn", "Token store", s["name"] + (f" — {s.get('detail')}" if s.get("detail") else ""))
        return 0
    try:
        ids, gh = _split_targets(args.targets or [])
    except ValueError as e:
        ui.p(f"  {ui.red('✗')} {e}")
        return 2

    if args.list or (args.verify and not args.unlink):
        only = set(ids)
        st = link_status(verify=args.verify)
        if only:
            st["apps"] = [a for a in st["apps"] if a["id"] in only]
        if args.json:
            print(json.dumps(st, indent=1))
        else:
            ui.banner("link")
            print_status(ui, st)
            ui.p()
        broken = [a for a in st["apps"] if a["state"] == "broken"]
        return 1 if (args.verify and broken) else 0

    if args.all and not args.unlink:
        # `copper link --all` means "connect everything found". It used to fall into the unlink branch
        # below and remove every connection (and the GitHub link) without being asked to.
        ids += [c.id for c in clients.resolve_names("all") if c.id not in ids]
        if not ids and not gh:
            ui.p("   " + ui.dim("No AI client found on this computer. Name one to connect it anyway: copper link cursor"))
            return 0
    if args.unlink:
        if args.all:
            ids, gh = [c.id for c in clients.CLIENTS], True
        if not ids and not gh:
            ui.p(f"  {ui.red('✗')} name what to unlink (a client id, `apps`, `github`) or pass --all")
            return 2
        out: dict = {}
        if ids:
            out["apps"] = apps.disconnect(ids, dry_run=args.dry_run)
        if gh and not args.dry_run:
            out["github"] = github.unlink()
        if args.json:
            print(json.dumps(out, indent=1))
            return 0
        ui.banner("unlink" + (" (dry run)" if args.dry_run else ""))
        for ch in (out.get("apps") or {}).get("changes", []):
            ui.line("ok" if ch["action"] in ("removed", "restored") else "fail" if ch["action"] == "error" else "skip",
                    clients.BY_ID[ch["client"]].name, f"{ch['action']} — {ch['path']}"
                    + (f": {ch['detail']}" if ch["action"] == "error" and ch.get("detail") else ""), width=20)
        if ids and not (out.get("apps") or {}).get("changes"):
            ui.p("   Copper was not connected to those clients.")
        for b in (out.get("apps") or {}).get("backups", []):
            ui.p(f"   {ui.dim('backup kept:')} {b}")
        if "github" in out:
            g = out["github"]
            ui.line("ok" if g["unlinked"] else "skip", "GitHub", ("unlinked. " + g["note"]) if g["unlinked"] else "was not linked", width=20)
        ui.p()
        return 0

    # ── link ──
    if not ids and not gh:
        # bare `copper link`: show everything, then connect every client found with one answer
        st = link_status()
        found = [a for a in st["apps"] if a["state"] in ("found", "broken")]
        if args.json and not args.yes:
            print(json.dumps(st, indent=1))
            return 0
        if not args.json:
            ui.banner("link")
            print_status(ui, st)
            ui.p()
        if not found:
            if not args.json:
                none_at_all = not any(a["state"] != "not-found" for a in st["apps"])
                ui.p("   " + (ui.dim("No AI client found. Install one, or name it: copper link claude-desktop") if none_at_all
                              else ui.dim("Every AI client found is connected. Check them with: copper link --verify")))
                ui.p()
            return 0
        names = ", ".join(a["name"] for a in found)
        # At a terminal Enter means yes. With piped or closed input nothing is connected unless the answer
        # really is "y" (or --yes was given): a script must not change client configs by accident.
        if not args.yes and not ui.ask(f"Connect Copper to {names}?", interactive):
            if not interactive:
                ui.p("   " + ui.dim("Nothing changed. To connect without being asked: ") + ui.accent("copper link --yes"))
            return 0
        ids = [a["id"] for a in found]
    rc = 0
    res = None
    if ids:
        try:
            res = apps.connect(ids, launcher=args.launcher, verify_launch=not args.no_verify, dry_run=args.dry_run)
        except ValueError as e:
            ui.p(f"  {ui.red('✗')} {e}")
            return 2
    if args.json:
        out = {"apps": res} if res else {}
        if gh:
            try:
                out["github"] = github.link(interactive=False, allow_weak_store=args.allow_weak_store)
            except (github.LinkError, store.StoreError) as e:
                out["github"] = {"linked": False, "error": str(getattr(e, "message", e)), "fix": getattr(e, "fix", None)}
        print(json.dumps(out, indent=1))
        return 0 if (not res or res["ok"]) and (not gh or out["github"].get("linked")) else 1
    if res:
        ui.section("AI apps")
        if not _print_connect(ui, res):
            rc = 1
    if gh:
        ui.section("GitHub")
        rc = link_github(ui, interactive, args.allow_weak_store) or rc
    ui.p()
    return rc


# ── copper setup: KiCad transition (terminal) ─────────────────────────────
def choose_kicad(ui, yes) -> None:
    """Several KiCad versions installed: ask which one Copper should drive."""
    from ..kicad import env
    from . import kicad_import, prefs
    lst = [i for i in env.installs() if i.cli]
    if len(lst) < 2:
        return
    cur = env.kicad()
    ui.p("   " + ui.dim(f"{len(lst)} KiCad versions are installed:"))
    for n, i in enumerate(lst, 1):
        ui.p(f"     {n}. KiCad {i.full_version or i.version}  {ui.dim(str(i.root))}" + ("  ← default" if i.root == cur.root else ""))
    if yes is not None:
        ui.line("info", "KiCad", f"using {cur.full_version or cur.version} (the default; change with `copper setup`)")
        return
    d = next((n for n, i in enumerate(lst, 1) if i.root == cur.root), 1)
    pick = lst[ui.choose(f"Which one should Copper use? [1-{len(lst)}, Enter = {d}]", len(lst), d) - 1]
    if pick.root != cur.root or prefs.get("kicad_version"):
        kicad_import.choose_install(pick.full_version or pick.version)
    ui.line("ok", "KiCad", f"Copper will use {pick.full_version or pick.version}")


def import_step(ui, yes, only: str | None = None, source: str | None = None, no_libs: bool = False,
                api: str = "ask") -> dict | None:
    """Show what the user's KiCad already has and ask, item by item, what to bring along.

    ``yes`` True = non-interactive: only items that leave KiCad's own files alone are applied, unless
    ``only`` (the ``--import`` list) or ``--api enable`` names more."""
    from . import kicad_import as K
    try:
        disc = K.discover()
    except Exception as e:  # noqa: BLE001
        ui.line("warn", "KiCad settings", f"could not be read: {e}")
        return None
    srcs = disc["sources"]
    src = source or disc["default_source"]
    if source is None and yes is None and len(srcs) > 1:
        ui.p("   " + ui.dim(f"KiCad settings were found for {len(srcs)} versions:"))
        for n, v in enumerate(srcs, 1):
            c = next(c for c in disc["configs"] if c["version"] == v)
            ui.p(f"     {n}. KiCad {v}  {ui.dim(str(c['sym']['libs']) + ' symbol / ' + str(c['fp']['libs']) + ' footprint libraries')}"
                 + ("  ← current" if v == disc["target"] else ""))
        d = srcs.index(src) + 1 if src in srcs else 1
        src = srcs[ui.choose(f"Which settings should Copper use? [1-{len(srcs)}, Enter = {d}]", len(srcs), d) - 1]
    try:
        pl = K.plan(src)
    except ValueError as e:
        ui.line("fail", "KiCad settings", str(e))
        return None
    if pl["source"] and pl["source"] != pl["target"]:
        ui.p("   " + ui.dim(f"From KiCad {pl['source']} into KiCad {pl['target']}. The KiCad {pl['source']} files are only read."))
    for w in pl["warnings"]:
        ui.say(w, indent=3, style=ui.dim)
    explicit = None
    if only is not None:
        words = [w for w in only.replace(",", " ").split() if w]
        explicit = set(K.ORDER) if "all" in words else set() if "none" in words else None
        if explicit is None:
            bad = [w for w in words if w not in K.ORDER and w != "safe"]
            if bad:
                ui.line("fail", "--import", f"unknown {'item' if len(bad) == 1 else 'items'} {', '.join(bad)}; known: {', '.join(K.ORDER)}, all, none, safe")
                return None
            explicit = set(words) - {"safe"}
            if "safe" in words:
                explicit |= {k for k, v in K.defaults(pl, yes=True).items() if v}
    auto = K.defaults(pl, yes=True)
    choices: dict[str, bool] = {}
    held: list[str] = []
    for it in pl["items"]:
        iid, label = it["id"], it["label"]
        if it["state"] == "in-use":
            ui.line("ok", label, it["found"] + (f" — {it['action']}" if it["action"] else ""))
            continue
        if it["state"] == "nothing":
            ui.line("skip", label, it["found"] + (f" — {it['action']}" if it["action"] else ""))
            continue
        if it["state"] == "blocked":
            ui.line("warn", label, it["found"], it["blocked"])
            continue
        if iid == "api" and api == "skip":
            continue
        if no_libs and iid in ("sym_libs", "fp_libs"):
            continue
        if explicit is not None:
            want = iid in explicit
        elif iid == "api" and api == "enable":
            want = True
        elif yes is not None:
            want = auto.get(iid, False)
            if not want:
                ui.line("skip", label, f"{it['found']} (left as it is: --yes changes nothing without being told)")
                held.append(iid)
        else:
            ui.p(f"   {ui.accent('•')} {ui.bold(label)}  {ui.dim(it['found'])}")
            tag = "KiCad setting, backed up first" if it["modifies_kicad"] else "Copper only"
            want = ui.ask(f"  {it['action'][0].upper() + it['action'][1:]}? {ui.dim('· ' + tag)}", it["default"])
        choices[iid] = want
    if held:
        frm = f" --from {pl['source']}" if pl["source"] and pl["source"] != pl["target"] else ""
        ui.p("   " + ui.dim("To bring those along: ") + ui.accent("copper setup") + ui.dim(" (asks about each), or ")
             + ui.accent(f"copper setup --yes{frm} --import " + ",".join(held)))
    if not any(choices.values()):
        return {"results": {}, "changed": [], "run": None}
    res = K.apply(choices, source=pl["source"])
    for iid, r in res["results"].items():
        label = next(it["label"] for it in pl["items"] if it["id"] == iid)
        if r.get("error"):
            ui.line("fail", label, r["error"])
        elif r.get("skipped"):
            ui.line("skip", label, r["skipped"])
        else:
            bits = []
            if r.get("created"):
                bits.append("created from KiCad's defaults")
            if r.get("added"):
                bits.append(f"added {len(r['added'])}: {K._some([str(x).replace(chr(92), '/').rsplit('/', 1)[-1] for x in r['added']])}")
            if r.get("copied"):
                bits.append(f"copied {r['copied']}")
            if r.get("enabled"):
                bits.append("enabled")
            if r.get("changed") and not bits:
                bits.append("done")
            if r.get("note"):
                bits.append(r["note"])
            ui.line("ok" if not r.get("unresolved") else "warn", label, "; ".join(bits) or "nothing needed changing")
    if res["changed"]:
        ui.p("   " + ui.dim(f"{len(res['changed'])} {'file' if len(res['changed']) == 1 else 'files'} changed; originals are saved. Put everything back with: ")
             + ui.accent("copper setup --undo"))
    return res


def cmd_undo(ui, run_id: str | None = None, force: bool = False, dry_run: bool = False, as_json: bool = False) -> int:
    from . import kicad_import as K
    r = K.undo(run_id, force=force, dry_run=dry_run)
    if as_json:
        print(json.dumps(r, indent=1))
        return 0 if not r.get("refused") else 1
    ui.banner("setup --undo" + (" (dry run)" if dry_run else ""))
    if r.get("refused"):
        ui.line("warn", "Undo", r["refused"])
        ui.p()
        return 1
    if not r["run"]:
        ui.p("   " + r["detail"])
        ui.p()
        return 0
    for p in r["restored"]:
        ui.line("ok", "restored", p, width=10)
    for p in r["removed"]:
        ui.line("ok", "removed", p, width=10)
    for n in r.get("notes") or []:
        ui.line("info", "note", n, width=10)
    for k in r["kept"]:
        ui.line("warn", "left alone", f"{k['path']} — {k['why']}", "copper setup --undo --force   (overwrites it)", width=10)
    ui.p()
    ui.p("   " + ui.dim(r["detail"]))
    ui.p()
    return 0 if not r["kept"] else 1


# ── copper uninstall --accounts / --purge ─────────────────────────────────
PURGE_NAMES = ("tools", "cache", "focus", "focus-cache", "logs", "kicad-import", "state.json", "clients.json",
               "links.json", "prefs.json", "update.json", "onboard.json", "credentials.bin", "credentials.key")


def purge(dry_run: bool = False) -> dict:
    """Delete only the names Copper creates inside its data folder; anything else stays and is listed."""
    import shutil
    from . import clients
    home = clients.copper_home()
    removed, left, held = [], [], []
    if home.is_dir():
        for p in sorted(home.iterdir()):
            if p.name == "kicad-import" and _open_imports():
                # the backups in there are the only copies of KiCad files an import replaced
                left.append(str(p))
                held.append(f"{p} holds the originals of KiCad settings that `copper setup` changed "
                            f"({_open_imports()} import(s) not undone). Run `copper setup --undo` first, or delete "
                            "the folder yourself if you want to keep the imported settings.")
                continue
            if p.name in PURGE_NAMES:
                if not dry_run:
                    shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
                removed.append(str(p))
            else:
                left.append(str(p))
        if not left and not dry_run:
            try:
                home.rmdir()
            except OSError:
                pass
    return {"home": str(home), "removed": removed, "left": left, "held": held}


def _open_imports() -> int:
    """Imports whose backups are still the way back (not undone, and a file was replaced)."""
    from . import kicad_import as K
    try:
        return sum(1 for r in K.history() if not r.get("undone")
                   and any(op.get("backup") and not op.get("undone") for op in r.get("ops") or []))
    except Exception:  # noqa: BLE001
        return 0


def is_windowless() -> bool:
    return sys.stdout is None or sys.stderr is None
