"""Regression tests for bugs found while attacking the install / link / KiCad-transition feature.

Each test names the failure it guards against. Same sandbox as tests/test_install.py
(COPPER_CONFIG_HOME_OVERRIDE + COPPER_HOME + an unversioned KICAD_CONFIG_HOME; nothing real is touched).
Runs with pytest or plainly:  python tests/test_install_hardening.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
os.environ.setdefault("COPPER_HOME", tempfile.mkdtemp(prefix="copper-install-home-"))

import test_install as T  # noqa: E402  (sets COPPER_HOME to a temp dir before importing Copper)
from test_install import K, Sandbox, _cli, _has_kicad, _tree, apps, env, linkcli  # noqa: E402

ALL = {k: True for k in K.ORDER}


def _used10(sb, sym_table: str | None = None):
    """The user has already run the new KiCad and customised it."""
    k10 = sb.k10
    k10.mkdir(parents=True, exist_ok=True)
    (k10 / "sym-lib-table").write_text(sym_table or (
        '(sym_lib_table\n\t(version 7)\n'
        '\t(lib (name "Device") (type "KiCad") (uri "${KICAD10_SYMBOL_DIR}/Device.kicad_sym") (options "") (descr "x"))\n'
        '\t(lib (name "Ten") (type "KiCad") (uri "C:/ten/Ten.kicad_sym") (options "") (descr ""))\n)\n'), encoding="utf-8")
    (k10 / "kicad_common.json").write_text(json.dumps(
        {"environment": {"vars": {"TENVAR": "C:/ten"}}, "api": {"enable_server": False}}, indent=2), encoding="utf-8")
    (k10 / "user.hotkeys").write_text("common.Control.zoomIn\tCtrl+Shift+=\n", encoding="utf-8")
    (k10 / "eeschema.json").write_text('{"ten": 1}', encoding="utf-8")
    env.refresh()
    return k10


# ── data loss ─────────────────────────────────────────────────────────────
def test_two_imports_in_one_second_do_not_share_a_backup_folder():
    """Run ids were second-resolution: a second import in the same second reused the first run's backup
    folder and overwrote the user's ORIGINAL file with an already-changed one."""
    with Sandbox(kicad9=True) as sb:
        k10 = _used10(sb)
        original = (k10 / "user.hotkeys").read_bytes()
        r1 = K.apply({"hotkeys": True}, source="9.0")
        (k10 / "user.hotkeys").write_text("edited after the first import\n", encoding="utf-8")
        r2 = K.apply({"hotkeys": True}, source="9.0")
        assert r1["run"] and r2["run"] and r1["run"] != r2["run"], (r1["run"], r2["run"])
        runs = K.history()
        assert len({r["id"] for r in runs}) == 2
        first_backup = Path(runs[0]["ops"][0]["backup"])
        assert first_backup.read_bytes() == original, "the first run's backup was overwritten"
        assert Path(runs[1]["ops"][0]["backup"]) != first_backup


def test_undo_never_rewrites_a_settings_file_it_cannot_parse():
    """Undo of a key-level edit loaded an unparsable kicad_common.json as {} and wrote '{}' over it."""
    with Sandbox(kicad9=True) as sb:
        k10 = _used10(sb)
        K.apply({"api": True, "env_vars": True}, source="9.0")
        broken = '{"api": {"enable_server": true,  <<< half-saved by something else'
        (k10 / "kicad_common.json").write_text(broken, encoding="utf-8")
        for dry in (True, False):
            u = K.undo(dry_run=dry)
            assert (k10 / "kicad_common.json").read_text(encoding="utf-8") == broken, "file was clobbered"
            assert u["kept"] and "not valid JSON" in u["kept"][0]["why"] and "backups" in u["kept"][0]["why"], u
        assert not K.history()[-1]["undone"], "a run with files left alone must stay undoable"
        assert not [p for p in k10.iterdir() if "copper-tmp" in p.name]


def test_existing_table_in_another_layout_keeps_the_users_libraries():
    """The merge looked for the substring '(lib ' to decide whether a table had libraries; a table laid
    out differently (one form per line) was replaced by KiCad's default table and the user's own entries
    were dropped from it."""
    multi = ('(sym_lib_table\n  (version 7)\n  (lib\n    (name "Ten")\n    (type "KiCad")\n'
             '    (uri "C:/ten/Ten.kicad_sym")\n    (options "")\n    (descr "")\n  )\n)\n')
    with Sandbox(kicad9=True) as sb:
        k10 = _used10(sb, multi)
        r = K.apply({"sym_libs": True}, source="9.0")["results"]["sym_libs"]
        assert r.get("changed") and not r.get("created"), r
        names = [x["name"] for x in K._libs(k10 / "sym-lib-table")]
        assert names[0] == "Ten" and set(r["added"]) <= set(names) and len(names) == 1 + len(r["added"]), names


def test_damaged_tables_are_reported_and_left_alone():
    """A truncated / binary library table was reported as "0 libraries … already lists all of them"."""
    with Sandbox(kicad9=True) as sb:
        k10 = _used10(sb)
        (sb.k9 / "sym-lib-table").write_bytes(b'(sym_lib_table (version 7) (lib (name "X")(type "KiCad")(uri "C:/x")')
        pl = K.plan("9.0")
        it = next(i for i in pl["items"] if i["id"] == "sym_libs")
        assert it["state"] == "nothing" and "not a library table" in it["found"], it
        # a damaged TARGET table is never replaced by the default table
        (sb.k9 / "sym-lib-table").write_text('(sym_lib_table\n\t(version 7)\n\t(lib (name "Mine") (type "KiCad") '
                                             '(uri "C:/m.kicad_sym") (options "") (descr ""))\n)\n', encoding="utf-8")
        garbage = b"\xff\xfe\x00\x00 not a table (((("
        (k10 / "sym-lib-table").write_bytes(garbage)
        it = next(i for i in K.plan("9.0")["items"] if i["id"] == "sym_libs")
        assert it["state"] == "nothing", it
        K.apply({"sym_libs": True}, source="9.0")
        assert (k10 / "sym-lib-table").read_bytes() == garbage
        # and the merge itself refuses, should it ever be reached
        run = K._Run("9.0", "10.0")
        out = K._merge_table(run, "sym", sb.k9, k10, False)
        assert "error" in out and (k10 / "sym-lib-table").read_bytes() == garbage and not run.ops, out


def test_a_file_that_cannot_be_replaced_leaves_no_litter_and_says_why():
    """A read-only (or locked) target raised a raw PermissionError and left '<name>.copper-tmp' files in
    the user's KiCad settings folder."""
    with Sandbox(kicad9=True) as sb:
        k10 = _used10(sb)
        before = (k10 / "user.hotkeys").read_bytes()
        os.chmod(k10 / "user.hotkeys", 0o444)
        try:
            res = K.apply({"hotkeys": True, "themes": True}, source="9.0")
        finally:
            os.chmod(k10 / "user.hotkeys", 0o666)
        if (k10 / "user.hotkeys").read_bytes() != before:
            return                    # this platform replaces read-only files (POSIX): nothing to check
        r = res["results"]
        assert "could not be written" in r["hotkeys"].get("error", "") and "copper-tmp" not in r["hotkeys"]["error"], r
        assert r["themes"].get("copied") == 1, "one item failing must not stop the others"
        assert not [p.name for p in k10.rglob("*") if "copper-tmp" in p.name]
        assert all(op["item"] != "hotkeys" for op in K.history()[-1]["ops"])


def test_a_damaged_journal_is_set_aside_not_overwritten():
    """With an unreadable journal.json the next import wrote a new journal holding only itself: the
    record of earlier runs (which names the backups) was gone."""
    with Sandbox(kicad9=True) as sb:
        _used10(sb)
        K.apply({"hotkeys": True}, source="9.0")
        raw = K.journal_path().read_text(encoding="utf-8")
        K.journal_path().write_text(raw[: len(raw) // 2], encoding="utf-8")          # truncated
        assert K.history() == []
        K.apply({"themes": True}, source="9.0")
        aside = [p for p in K.work_dir().iterdir() if p.name.startswith("journal.damaged-")]
        assert len(aside) == 1 and aside[0].read_text(encoding="utf-8") == raw[: len(raw) // 2]
        assert len(K.history()) == 1
        assert len(list((K.work_dir() / "backups").iterdir())) >= 1, "the earlier run's backups must survive"


def test_undo_takes_only_its_own_entries_out_of_a_table_changed_since():
    """After KiCad (or the user) saved the library table again, undo refused the whole file. It now
    removes exactly the entries Copper added and keeps everything else, including an entry of Copper's
    that the user has re-pointed."""
    with Sandbox(kicad9=True) as sb:
        k10 = _used10(sb)
        r = K.apply({"sym_libs": True}, source="9.0")["results"]["sym_libs"]
        assert len(r["added"]) >= 2, r
        t = k10 / "sym-lib-table"
        s = t.read_text(encoding="utf-8")
        s = s.replace("\n)", '\n\t(lib (name "AddedLater") (type "KiCad") (uri "C:/later.kicad_sym") (options "") (descr "a (b) \\"c\\""))\n)')
        moved = r["added"][-1]
        old_uri = next(x["uri"] for x in K._libs(t) if x["name"] == moved)
        s = s.replace(f'(uri "{old_uri}")', '(uri "D:/moved/by/user.kicad_sym")')
        t.write_text(s, encoding="utf-8")
        d = K.undo(dry_run=True)
        assert t.read_text(encoding="utf-8") == s and d["notes"], d
        u = K.undo()
        names = [x["name"] for x in K._libs(t)]
        assert names == ["Device", "Ten", moved, "AddedLater"], names
        assert u["notes"] and "kept your later changes" in u["notes"][0] and str(t) in u["restored"], u
        assert K._table_ok(t.read_text(encoding="utf-8"), "sym")
        assert K.history()[-1]["undone"]


def test_lib_spans_handle_quotes_parens_and_layouts():
    text = ('(sym_lib_table\r\n\t(version 7)\r\n'
            '\t(lib (name "A)(")(type "KiCad")(uri "C:/a \\"q\\" (x).kicad_sym")(options "")(descr ")"))\r\n'
            '  (lib\r\n    (name "B")\r\n    (uri "${V}/b.kicad_sym")\r\n  )\r\n'
            '\t(lib (name "C") (type "KiCad") (uri "C:\\\\c.lib") (options "") (descr "") (disabled))\r\n)\r\n')
    spans = K._lib_spans(text)
    assert [(n, u) for _, _, n, u in spans] == [("A)(", 'C:/a "q" (x).kicad_sym'), ("B", "${V}/b.kicad_sym"), ("C", "C:\\c.lib")], spans
    out, n = K._remove_libs(text, [{"name": "B", "uri": "${V}/b.kicad_sym"}, {"name": "C", "uri": "somewhere else"}])
    assert n == 1 and [x[2] for x in K._lib_spans(out)] == ["A)(", "C"] and K._table_ok(out, "sym") and "\r\n\r\n" not in out


def test_purge_keeps_the_only_copies_of_replaced_kicad_files():
    """`copper uninstall --purge` deleted kicad-import/ and with it the user's original KiCad files of
    an import that was never undone."""
    with Sandbox(kicad9=True) as sb:
        k10 = _used10(sb)
        original = (k10 / "user.hotkeys").read_bytes()
        K.apply({"hotkeys": True}, source="9.0")
        backup = Path(K.history()[-1]["ops"][0]["backup"])
        d = linkcli.purge()
        assert backup.read_bytes() == original and K.work_dir().is_dir(), "the backup was purged"
        assert d["held"] and "copper setup --undo" in d["held"][0] and str(K.work_dir()) in d["left"], d
        r = _cli("uninstall", "--purge", "--dry-run")
        assert "copper setup --undo" in r.stdout, r.stdout
        K.undo()
        assert (k10 / "user.hotkeys").read_bytes() == original
        d = linkcli.purge()
        assert not d["held"] and not K.work_dir().exists(), d


# ── honest reporting ──────────────────────────────────────────────────────
def test_older_versions_own_libraries_are_mentioned_when_the_new_kicad_has_settings():
    """Someone who had opened the new KiCad once was shown only its settings: nothing said that the
    older version still had libraries of their own to bring along."""
    with Sandbox(kicad9=True) as sb:
        _used10(sb)
        pl = K.plan()
        assert pl["source"] == pl["target"] and pl["same_version"]
        assert [a["version"] for a in pl["also"]] == ["9.0"], pl["also"]
        assert "Mine" in pl["also"][0]["summary"] and "copper setup --from 9.0" in pl["warnings"][0], pl
        r = _cli("setup", "--tty", "--yes", "--clients", "none", "--no-doctor")
        assert "KiCad 9.0 also has" in r.stdout and "--from 9.0" in r.stdout, r.stdout
        # once they are brought along the hint goes away
        K.apply({k: True for k in ("sym_libs", "fp_libs", "themes", "plugins", "env_vars")}, source="9.0")
        assert K.plan()["also"] == [], K.plan()["also"]


def test_setup_banner_does_not_claim_safety_when_told_to_change_kicad():
    with Sandbox(kicad9=True) as sb:
        _used10(sb)
        r = _cli("setup", "--tty", "--yes", "--clients", "none", "--no-doctor", "--from", "9.0", "--import", "hotkeys")
        assert "nothing in KiCad's own settings is changed" not in r.stdout and "exactly what was named" in r.stdout, r.stdout
        r = _cli("setup", "--tty", "--yes", "--clients", "none", "--no-doctor")
        assert "nothing in KiCad's own settings is changed" in r.stdout


def test_link_all_connects_and_never_unlinks():
    """`copper link --all` fell into the unlink branch: it removed every connection and the GitHub link."""
    with Sandbox() as sb:
        r = _cli("link", "--yes", "--no-verify")
        assert r.returncode == 0, r.stdout + r.stderr
        before = _tree(sb.home)
        r = _cli("link", "--all", "--no-verify")
        assert "unlink" not in r.stdout.lower() and "removed" not in r.stdout, r.stdout
        st = {a["id"]: a["state"] for a in json.loads(_cli("link", "--list", "--json").stdout)["apps"]}
        assert st["cursor"] == "connected" and st["codex"] == "connected", st
        after = _tree(sb.home)
        assert {k: v for k, v in before.items() if "clients.json" not in k} == {k: v for k, v in after.items() if "clients.json" not in k}
        r = _cli("link", "--unlink", "--all")
        st = {a["id"]: a["state"] for a in json.loads(_cli("link", "--list", "--json").stdout)["apps"]}
        assert st["cursor"] == "found" and st["codex"] == "found", st


def test_a_dying_servers_last_words_are_scrubbed_and_its_exit_code_reported():
    """The launch check quoted the child's last stderr line verbatim (into the terminal, the setup page
    and doctor) and reported 'code None' because it asked for the exit code too early."""
    token = "ghp_" + "a1B2c3D4e5" * 4
    code = f"import sys; sys.stderr.write('boom {token}\\n'); sys.exit(7)  # from copper_kicad.cli import main; main(['serve'])"
    r = apps.verify_entry({"command": sys.executable, "args": ["-c", code]}, timeout=20)
    assert r["ok"] is False and r["stage"] == "initialize", r
    assert token not in json.dumps(r) and "[redacted]" in r["detail"], r
    assert "code 7" in r["detail"] and "None" not in r["detail"], r


def test_freerouting_line_names_the_folder_the_router_is_really_in():
    from copper_kicad.install import freerouting
    f = {"ok": True, "version": "2.2.3", "java_version": "25", "tools_dir": r"C:\sandbox\tools",
         "jar": str(Path("C:/shared/tools/freerouting-2.2.3.jar"))}
    line = freerouting.describe(f)
    assert str(Path("C:/shared/tools")) in line and "sandbox" not in line, line


# ── KiCad's own first-run import must survive Copper ───────────────────────
def test_probing_kicad_does_not_create_the_users_config_folder():
    """`kicad-cli version` writes <config>/<ver>/kicad.json + kicad_common.json. Copper's version probe
    and doctor ran it against the user's real config root, so installing Copper before first starting a
    new KiCad made KiCad skip its own 'import settings from a previous version' offer."""
    if not _has_kicad():
        print("SKIP no KiCad installed")
        return
    with Sandbox() as sb:
        cfg = sb.home / "kicad"
        env.refresh()
        k = env.kicad()
        assert k.full_version or k.version
        assert not cfg.exists() or not any(cfg.rglob("*")), list(cfg.rglob("*"))
        for args in (("doctor", "--offline"), ("status",), ("link", "--list"),
                     ("setup", "--tty", "--yes", "--clients", "all", "--no-doctor")):
            r = _cli(*args)
            assert r.returncode == 0, (args, r.stdout[-400:], r.stderr[-400:])
            assert not cfg.exists() or not any(cfg.rglob("*")), (args, [str(p) for p in cfg.rglob("*")])


def test_probing_kicad_does_not_resave_existing_settings():
    if not _has_kicad():
        print("SKIP no KiCad installed")
        return
    with Sandbox(kicad9=True) as sb:
        k10 = _used10(sb)
        before = _tree(sb.home / "kicad")
        for args in (("doctor", "--offline"), ("setup", "--tty", "--yes", "--clients", "none", "--no-doctor")):
            _cli(*args)
        assert _tree(sb.home / "kicad") == before, "a health check / --yes run changed KiCad's settings"
        assert k10.is_dir()


def test_import_into_kicad_cli_default_files_then_undo_restores_them():
    """The other "fresh" case: the new KiCad's folder holds only what a kicad-cli run leaves there. After
    an import of everything, a KiCad re-save and an undo, exactly those files are back."""
    if not _has_kicad():
        print("SKIP no KiCad installed")
        return
    with Sandbox(kicad9=True) as sb:
        cli = env.kicad().cli
        run = lambda: subprocess.run([str(cli), "version"], capture_output=True, timeout=60,   # noqa: E731
                                     env=dict(os.environ, KICAD_CONFIG_HOME=str(sb.home / "kicad")))
        run()
        assert sorted(p.name for p in sb.k10.iterdir() if p.is_file()) == ["kicad.json", "kicad_common.json"]
        pl = K.plan()
        assert pl["source"] == "9.0" and pl["target_fresh"], pl
        before = {k: json.loads((sb.k10 / k).read_text(encoding="utf-8")) for k in ("kicad.json", "kicad_common.json")}
        res = K.apply(ALL)
        assert not any("error" in v for v in res["results"].values()), res["results"]
        run()                                                   # KiCad saves its settings again
        u = K.undo()
        assert u["kept"] == [], u["kept"]
        assert sorted(str(p.relative_to(sb.k10)) for p in sb.k10.rglob("*") if p.is_file()) == ["kicad.json", "kicad_common.json"]
        after = {k: json.loads((sb.k10 / k).read_text(encoding="utf-8")) for k in before}
        assert after == before, "the files kicad-cli had written are not back as they were"


def test_undo_removes_a_settings_file_copper_created_but_keeps_one_kicad_filled():
    with Sandbox(kicad9=True) as sb:
        assert not sb.k10.exists()
        K.apply({"api": True}, source="9.0")
        assert json.loads((sb.k10 / "kicad_common.json").read_text(encoding="utf-8"))["api"]["enable_server"] is True
        d = K.undo(dry_run=True)
        assert (sb.k10 / "kicad_common.json").exists() and str(sb.k10 / "kicad_common.json") in d["removed"], d
        K.undo()
        assert not sb.k10.exists(), sorted(_tree(sb.k10))
        # … but when KiCad has saved its own keys into that file since, only Copper's keys go
        K.apply({"api": True}, source="9.0")
        c = json.loads((sb.k10 / "kicad_common.json").read_text(encoding="utf-8"))
        c["appearance"] = {"icon_theme": 1}
        (sb.k10 / "kicad_common.json").write_text(json.dumps(c), encoding="utf-8")
        K.undo()
        c = json.loads((sb.k10 / "kicad_common.json").read_text(encoding="utf-8"))
        assert c["appearance"] == {"icon_theme": 1} and "enable_server" not in c.get("api", {}), c


def _main() -> int:
    only = sys.argv[1:]
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)
             and (not only or any(o in n for o in only))]
    failed = 0
    for n, f in tests:
        t = time.perf_counter()
        try:
            f()
            print(f"PASS {n}  ({time.perf_counter() - t:.1f}s)")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {n}\n{traceback.format_exc()}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
