'use strict';
// npx -y copper-kicad [args]  ->  <python> -c "<boot>" [args]
//
// No args and no terminal on stdin = the MCP server over stdio (what AI apps start).
// Every other argument goes to the `copper` CLI unchanged (setup, link, doctor, ...).
// Nothing but the Python child ever writes to stdout: in server mode stdout is the
// MCP channel, so all launcher messages go to stderr.
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, spawnSync } = require('child_process');
const { candidates, choose, PROBE, MIN } = require('./python');

const PKG_DIR = path.resolve(__dirname, '..');
const PY_DIR = path.join(PKG_DIR, 'py');
const VERSION = require('../package.json').version;
// commands whose result names the Python package's location (they register Copper
// with AI apps, or print the entry to paste): run them from a stable copy, not from
// npx's cache, which npm may clear at any time
const STABLE = new Set(['setup', 'install', 'link', 'config']);
// commands that take --demo [DIR] (copper_kicad/cli.py DEMO_COMMANDS)
const DEMO = new Set(['setup', 'link', 'install', 'uninstall', 'doctor', 'status', 'config']);

function log(msg) {
  process.stderr.write(`copper-kicad: ${msg}\n`);
}

function copperHome(env) {
  if (env.COPPER_CONFIG_HOME_OVERRIDE) return path.join(env.COPPER_CONFIG_HOME_OVERRIDE, '.copper');
  return env.COPPER_HOME || path.join(os.homedir(), '.copper');
}

// `setup --demo` is a throw-away try-out: keep the launcher's own files (the stable copy of Copper,
// Pillow) out of the real home too, in a temporary folder.
function isDemo(args) {
  if (!args.length || !DEMO.has(args[0])) return false;
  for (const a of args) {
    if (a === '--') return false;
    if (a === '--demo' || a.startsWith('--demo=')) return true;
  }
  return false;
}

function probe(c) {
  const r = spawnSync(c.cmd, [...c.args, '-c', PROBE], {
    encoding: 'utf8', timeout: 20000, windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'],
  });
  if (r.error || r.status !== 0) return null;
  try {
    const line = r.stdout.trim().split(/\r?\n/).pop();
    return JSON.parse(line);
  } catch (_) {
    return null;
  }
}

function findPython(env) {
  const io = {
    exists: (p) => { try { return fs.statSync(p).isFile(); } catch (_) { return false; } },
    listDir: (p) => fs.readdirSync(p),
    homedir: os.homedir(),
  };
  const { pick, tried } = choose(process.platform, candidates(process.platform, env, io), probe);
  return { pick, tried };
}

function fingerprint(dir) {
  let n = 0;
  let size = 0;
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) { if (e.name !== '__pycache__') walk(p); } else if (e.isFile() && !p.endsWith('.pyc')) {
        n += 1;
        size += fs.statSync(p).size;
      }
    }
  };
  walk(dir);
  return `${VERSION} ${n} ${size}`;
}

// A copy of the Python package under $COPPER_HOME/runtime/npm-<version> that outlives npx's cache.
function stableRuntime(env) {
  const root = path.join(copperHome(env), 'runtime', `npm-${VERSION}`);
  const want = fingerprint(path.join(PY_DIR, 'copper_kicad'));
  const stamp = path.join(root, '.copper-npm');
  let have = '';
  try { have = fs.readFileSync(stamp, 'utf8').trim(); } catch (_) { /* first run */ }
  if (have !== want) {
    const tmp = `${root}.tmp-${process.pid}`;
    fs.rmSync(tmp, { recursive: true, force: true });
    fs.mkdirSync(tmp, { recursive: true });
    fs.cpSync(path.join(PY_DIR, 'copper_kicad'), path.join(tmp, 'copper_kicad'), {
      recursive: true, filter: (s) => !s.endsWith('.pyc') && path.basename(s) !== '__pycache__',
    });
    fs.writeFileSync(path.join(tmp, '.copper-npm'), want + '\n');
    // keep Pillow from an earlier bootstrap of this version
    for (const keep of fs.existsSync(root) ? fs.readdirSync(root) : []) {
      if (/^(PIL|pillow)/i.test(keep)) fs.renameSync(path.join(root, keep), path.join(tmp, keep));
    }
    fs.rmSync(root, { recursive: true, force: true });
    fs.renameSync(tmp, root);
  }
  return root;
}

// Pillow: Copper rasterises KiCad's SVG plots with it. KiCad's Python on Windows has it;
// elsewhere install it once into a folder Copper owns (never into KiCad's or the system Python).
function ensurePillow(py, depsDir, env) {
  if (py.info.pil) return true;
  if (fs.existsSync(path.join(depsDir, 'PIL'))) return true;
  if (env.COPPER_NPM_NO_BOOTSTRAP) {
    log(`Pillow is missing in ${py.info.exe} and COPPER_NPM_NO_BOOTSTRAP is set; renders will fail.`);
    return false;
  }
  const failed = path.join(depsDir, '.pillow-failed');
  try {
    if (Date.now() - fs.statSync(failed).mtimeMs < 6 * 3600 * 1000) return false;   // do not retry on every start
  } catch (_) { /* never failed */ }
  log(`Pillow is missing in ${py.info.exe}; installing it once into ${depsDir} ...`);
  fs.mkdirSync(depsDir, { recursive: true });
  const r = spawnSync(py.cmd, [...py.args, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
    '--quiet', '--retries', '1', '--timeout', '20', '--target', depsDir, 'pillow>=10'],
  { stdio: ['ignore', 2, 2], windowsHide: true, timeout: 600000 });
  if (r.status === 0) {
    fs.rmSync(failed, { force: true });
    return true;
  }
  try { fs.writeFileSync(failed, new Date().toISOString() + '\n'); } catch (_) { /* read-only */ }
  log('could not install Pillow (no pip, or offline). Install it for that Python ' +
      `(${py.info.exe} -m pip install pillow) or point COPPER_PYTHON at a Python 3.10+ that has it.`);
  return false;
}

const BOOT = 'import os, sys; sys.path[0:0] = [p for p in os.environ.pop("COPPER_NPM_PATH", "").split(os.pathsep) if p]; '
  + 'from copper_kicad.cli import main; sys.exit(main(sys.argv[1:]))';

function noPythonHelp(tried) {
  const lines = [
    `no Python ${MIN.join('.')}+ found to run Copper.`,
    'Copper runs on the Python that comes with KiCad. Install KiCad 10 from https://www.kicad.org/download/',
    'or set COPPER_PYTHON to a Python 3.10+ interpreter.',
  ];
  if (tried.length) {
    lines.push('Tried:');
    for (const t of tried) {
      const v = t.info ? `Python ${t.info.v.join('.')}` : 'not runnable';
      lines.push(`  ${[t.cmd, ...t.args].join(' ')}  (${t.source}: ${v})`);
    }
  }
  return lines.join('\n');
}

async function main(argv, env = process.env) {
  if (argv[0] === '--launcher-info') {
    const { pick, tried } = findPython(env);
    process.stdout.write(JSON.stringify({ version: VERSION, python: pick, tried, copperHome: copperHome(env) }, null, 1) + '\n');
    return pick ? 0 : 1;
  }
  if (!fs.existsSync(path.join(PY_DIR, 'copper_kicad', '__init__.py'))) {
    log(`this package has no Python sources at ${PY_DIR} (an unpacked checkout? run \`node scripts/vendor.js\`).`);
    return 1;
  }
  const { pick: py, tried } = findPython(env);
  if (!py) {
    log(noPythonHelp(tried));
    return 1;
  }

  let args = argv.slice();
  if (args.length === 0 && !process.stdin.isTTY) args = ['serve'];
  const stable = args.length > 0 && STABLE.has(args[0]);

  // where the launcher keeps its files; under --demo a temporary folder, never the real home
  const homeEnv = isDemo(args)
    ? { ...env, COPPER_CONFIG_HOME_OVERRIDE: '', COPPER_HOME: path.join(os.tmpdir(), 'copper-kicad-demo', '.copper') }
    : env;
  let pkgRoot = PY_DIR;
  let depsDir = path.join(copperHome(homeEnv), 'runtime', `pydeps-py${py.info.v.join('')}`);
  if (stable) {
    try {
      pkgRoot = stableRuntime(homeEnv);
      depsDir = pkgRoot;          // Pillow next to the package: the registered entry finds it too
    } catch (e) {
      log(`could not copy Copper to ${copperHome(homeEnv)} (${e.message}); running from the npm cache.`);
    }
  }
  ensurePillow(py, depsDir, env);

  const pyPath = [pkgRoot, depsDir].filter((p, i, a) => a.indexOf(p) === i && fs.existsSync(p));
  const childEnv = { ...env, COPPER_NPM_PATH: pyPath.join(path.delimiter), PYTHONUTF8: '1', COPPER_NPM_LAUNCHER: VERSION };
  childEnv.PYTHONPATH = [...pyPath, env.PYTHONPATH].filter(Boolean).join(path.delimiter);

  const child = spawn(py.cmd, [...py.args, '-X', 'utf8', '-c', BOOT, ...args], {
    stdio: 'inherit', env: childEnv, windowsHide: true,
  });
  const forward = (sig) => { try { child.kill(sig); } catch (_) { /* gone */ } };
  for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
    try { process.on(sig, () => forward(sig)); } catch (_) { /* not on this OS */ }
  }
  return new Promise((resolve) => {
    child.on('error', (e) => { log(`could not start ${py.cmd}: ${e.message}`); resolve(1); });
    child.on('exit', (code, signal) => resolve(code === null ? (signal ? 1 : 0) : code));
  });
}

module.exports = { main, copperHome, isDemo, BOOT, STABLE };
