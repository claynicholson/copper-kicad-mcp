'use strict';
// Finding a Python that can run Copper. Copper itself is pure Python (3.10+) plus
// Pillow; KiCad's own Python is preferred because it is always there when KiCad is
// and (on Windows) already carries Pillow. The candidate list is a pure function of
// (platform, env, filesystem) so it can be unit-tested for every OS on any OS.
const path = require('path');

const MIN = [3, 10];

function versionKey(name) {
  // "10.0" -> [10, 0]; "9.0" -> [9, 0]; anything else sorts last
  const m = /^(\d+)(?:\.(\d+))?/.exec(name);
  return m ? [Number(m[1]), Number(m[2] || 0)] : [-1, -1];
}

function byVersionDesc(a, b) {
  const ka = versionKey(a);
  const kb = versionKey(b);
  return kb[0] - ka[0] || kb[1] - ka[1];
}

/**
 * Ordered list of {cmd, args, source} to try.
 * io = {exists(p): bool, listDir(p): string[], homedir: string}
 */
function candidates(platform, env, io) {
  const out = [];
  const seen = new Set();
  const pathMod = platform === 'win32' ? path.win32 : path.posix;
  const add = (cmd, source, args = [], mustExist = true) => {
    if (!cmd) return;
    const key = [cmd, ...args].join('\u0000').toLowerCase();
    if (seen.has(key)) return;
    if (mustExist && !io.exists(cmd)) return;
    seen.add(key);
    out.push({ cmd, args, source });
  };

  if (env.COPPER_PYTHON) add(env.COPPER_PYTHON, 'COPPER_PYTHON', [], false);

  const cli = env.COPPER_KICAD_CLI;
  if (platform === 'win32') {
    if (cli) add(pathMod.join(pathMod.dirname(cli), 'python.exe'), 'KiCad (COPPER_KICAD_CLI)');
    const roots = [env.ProgramFiles, env.ProgramW6432, 'C:\\Program Files',
      env.LOCALAPPDATA && pathMod.join(env.LOCALAPPDATA, 'Programs')].filter(Boolean);
    for (const root of roots) {
      const kicad = pathMod.join(root, 'KiCad');
      let vers = [];
      try { vers = io.listDir(kicad); } catch (_) { vers = []; }
      for (const v of vers.slice().sort(byVersionDesc)) {
        add(pathMod.join(kicad, v, 'bin', 'python.exe'), `KiCad ${v}`);
      }
    }
    add('py', 'py launcher', ['-3'], false);
    add('python', 'PATH', [], false);
    add('python3', 'PATH', [], false);
  } else if (platform === 'darwin') {
    const fw = (app) => pathMod.join(app, 'Contents', 'Frameworks', 'Python.framework', 'Versions', 'Current', 'bin', 'python3');
    if (cli) {
      const i = cli.indexOf('.app/');
      if (i > 0) add(fw(cli.slice(0, i + 4)), 'KiCad (COPPER_KICAD_CLI)');
    }
    for (const app of ['/Applications/KiCad/KiCad.app', '/Applications/KiCad.app',
      io.homedir && pathMod.join(io.homedir, 'Applications', 'KiCad', 'KiCad.app'),
      io.homedir && pathMod.join(io.homedir, 'Applications', 'KiCad.app')].filter(Boolean)) {
      add(fw(app), 'KiCad.app');
    }
    add('/opt/homebrew/bin/python3', 'Homebrew');
    add('/usr/local/bin/python3', '/usr/local');
    add('python3', 'PATH', [], false);
    add('/usr/bin/python3', 'system');
  } else {
    // Linux / BSD: distribution KiCad packages put pcbnew into the system python3
    add('/usr/bin/python3', 'system');
    add('python3', 'PATH', [], false);
    add('/usr/local/bin/python3', '/usr/local');
    add('python', 'PATH', [], false);
  }
  return out;
}

const PROBE = [
  'import sys, json, importlib.util as u',
  'print(json.dumps({"v": list(sys.version_info[:2]), "exe": sys.executable,',
  ' "pil": u.find_spec("PIL") is not None, "pcbnew": u.find_spec("pcbnew") is not None}))',
].join('\n');

function versionOk(v) {
  return Array.isArray(v) && (v[0] > MIN[0] || (v[0] === MIN[0] && v[1] >= MIN[1]));
}

/**
 * Pick the interpreter. probe(c) -> {v:[maj,min], exe, pil, pcbnew} | null.
 * On Linux one that can import pcbnew wins over an earlier one that cannot.
 */
function choose(platform, cands, probe) {
  const tried = [];
  let firstOk = null;
  for (const c of cands) {
    const info = probe(c);
    tried.push({ ...c, info });
    if (!info || !versionOk(info.v)) continue;
    const hit = { ...c, info };
    if (platform !== 'linux' && platform !== 'freebsd' && platform !== 'openbsd') return { pick: hit, tried };
    if (info.pcbnew) return { pick: hit, tried };
    if (!firstOk) firstOk = hit;
  }
  return { pick: firstOk, tried };
}

module.exports = { candidates, choose, versionOk, versionKey, PROBE, MIN };
