"""A stand-in kicad-cli for compatibility tests: no KiCad needed.

It answers ``version`` and ``--help`` the way the real binary does (usage
line with the ``{a,b,c}`` subcommand list; ``<unknown sub> --help`` prints
the PARENT help and exits 0; an unknown flag prints ``Unknown argument`` and
exits 1; an unknown subcommand prints ``Failed to parse`` and exits 1), from
the command tree recorded off a real KiCad 10.0.6 (``cli-10.0.json``).

Environment:
  FAKE_KICAD_VERSION   version to report (default 9.0.9)
  FAKE_KICAD_TREE      path of cli-10.0.json (default: next to this file)
  FAKE_KICAD_REMOVE    ';'-separated ``command`` or ``command|--flag`` entries this build lacks
  FAKE_KICAD_ADD       ';'-separated ``command`` or ``command|--flag`` entries this build has in addition
  FAKE_KICAD_STAMPS    ``sch=20250114,pcb=20241229,sym=20241209`` written by ``upgrade``
  FAKE_KICAD_LOG       file that receives one JSON line per invocation (argv)
A first argument ``@settings.json`` supplies the same variables per install,
so two fake installs with different versions can exist in one process.

This is a model of other KiCad versions built from release notes and docs,
not a recording of them: tests that use it prove Copper's gating logic, not
the behaviour of those KiCad binaries.
"""
import json
import os
import re
import sys


def load():
    f = os.environ.get("FAKE_KICAD_TREE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli-10.0.json")
    with open(f, encoding="utf-8") as fh:
        tree = {k: {"flags": list(v["flags"]), "subs": list(v["subs"])} for k, v in json.load(fh)["help"].items()}
    for ent in filter(None, (os.environ.get("FAKE_KICAD_REMOVE") or "").split(";")):
        cmd, _, flag = ent.partition("|")
        if flag:
            if cmd in tree and flag in tree[cmd]["flags"]:
                tree[cmd]["flags"].remove(flag)
        else:
            for k in [k for k in tree if k == cmd or k.startswith(cmd + " ")]:
                del tree[k]
            parent, _, leaf = cmd.rpartition(" ")
            if parent in tree and leaf in tree[parent]["subs"]:
                tree[parent]["subs"].remove(leaf)
    for ent in filter(None, (os.environ.get("FAKE_KICAD_ADD") or "").split(";")):
        cmd, _, flag = ent.partition("|")
        parts = cmd.split()
        for n in range(1, len(parts) + 1):
            c, parent = " ".join(parts[:n]), " ".join(parts[:n - 1])
            tree.setdefault(c, {"flags": ["--help"], "subs": []})
            if parts[n - 1] not in tree[parent]["subs"]:
                tree[parent]["subs"].append(parts[n - 1])
        if flag and flag not in tree[cmd]["flags"]:
            tree[cmd]["flags"].append(flag)
    return tree


def usage(tree, cmd):
    node = tree[cmd]
    name = ("kicad-cli " + cmd).strip() if node["subs"] or not cmd else cmd
    text = "Usage: " + name + " " + " ".join("[%s]" % f for f in node["flags"])
    if node["subs"]:
        text += " {" + ",".join(node["subs"]) + "}"
    else:
        text += " INPUT_FILE"
    text += "\n\nOptional arguments:\n" + "".join("  %s\n" % f for f in node["flags"])
    if node["subs"]:
        text += "\nSubcommands:\n" + "".join("  %s\n" % s for s in node["subs"])
    return text


def main(argv):
    if argv and argv[0].startswith("@"):      # per-install settings: a JSON object of the variables above
        with open(argv[0][1:], encoding="utf-8") as fh:
            os.environ.update({k: str(v) for k, v in json.load(fh).items()})
        argv = argv[1:]
    log = os.environ.get("FAKE_KICAD_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\n")
    ver = os.environ.get("FAKE_KICAD_VERSION") or "9.0.9"
    tree = load()
    cmd, i = "", 0
    while i < len(argv) and not argv[i].startswith("-"):
        nxt = (cmd + " " + argv[i]).strip()
        if nxt not in tree:
            break
        cmd, i = nxt, i + 1
    rest = argv[i:]
    if cmd == "version":
        print(ver)
        return 0
    if "--help" in rest or "-h" in rest:
        print(usage(tree, cmd))            # an unknown subcommand + --help prints the parent help, exit 0
        return 0
    if tree[cmd]["subs"]:
        bad = rest[0] if rest else ""
        print("Failed to parse '%s'" % bad)
        print(usage(tree, cmd))
        return 1
    out, files, j = None, [], 0
    while j < len(rest):
        a = rest[j]
        if a in ("-o", "--output"):
            out, j = (rest[j + 1] if j + 1 < len(rest) else None), j + 2
            continue
        if a.startswith("--"):
            if a.split("=", 1)[0] not in tree[cmd]["flags"]:
                print("Unknown argument: %s" % a)
                print(usage(tree, cmd))
                return 1
        elif not a.startswith("-") and re.search(r"\.kicad_(sch|pcb|sym|mod)$", a):
            files.append(a)
        j += 1
    if cmd == "pcb drc" and out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"kicad_version": ver, "coordinate_units": "mm", "violations": [], "unconnected_items": [],
                       "schematic_parity": []}, fh)
    elif cmd == "sch erc" and out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"kicad_version": ver, "coordinate_units": "mm", "sheets": []}, fh)
    elif cmd.endswith(" upgrade") and files:
        stamps = dict(kv.split("=") for kv in filter(None, (os.environ.get("FAKE_KICAD_STAMPS") or "").split(",")))
        kind = cmd.split()[0]
        if kind in stamps:
            with open(files[0], encoding="utf-8") as fh:
                s = fh.read()
            s = re.sub(r"\(version\s+\d{8}\)", "(version %s)" % stamps[kind], s, count=1)
            with open(files[0], "w", encoding="utf-8", newline="\n") as fh:
                fh.write(s)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
