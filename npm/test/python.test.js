'use strict';
// Path logic for every OS, run on any OS: `npm test` (node --test).
const test = require('node:test');
const assert = require('node:assert');
const { candidates, choose, versionOk } = require('../lib/python');

function fakeIo(files, dirs = {}, homedir = '/home/u') {
  const set = new Set(files.map((f) => f.toLowerCase()));
  return {
    exists: (p) => set.has(p.toLowerCase()),
    listDir: (p) => { if (dirs[p]) return dirs[p]; throw new Error('ENOENT'); },
    homedir,
  };
}
const cmds = (list) => list.map((c) => [c.cmd, ...c.args].join(' '));

test('windows: newest KiCad first, then per-user KiCad, then PATH pythons', () => {
  const env = { ProgramFiles: 'C:\\Program Files', LOCALAPPDATA: 'C:\\Users\\u\\AppData\\Local' };
  const io = fakeIo([
    'C:\\Program Files\\KiCad\\9.0\\bin\\python.exe',
    'C:\\Program Files\\KiCad\\10.0\\bin\\python.exe',
    'C:\\Users\\u\\AppData\\Local\\Programs\\KiCad\\10.0\\bin\\python.exe',
  ], {
    'C:\\Program Files\\KiCad': ['9.0', '10.0', 'readme.txt'],
    'C:\\Users\\u\\AppData\\Local\\Programs\\KiCad': ['10.0'],
  });
  assert.deepStrictEqual(cmds(candidates('win32', env, io)), [
    'C:\\Program Files\\KiCad\\10.0\\bin\\python.exe',
    'C:\\Program Files\\KiCad\\9.0\\bin\\python.exe',
    'C:\\Users\\u\\AppData\\Local\\Programs\\KiCad\\10.0\\bin\\python.exe',
    'py -3', 'python', 'python3',
  ]);
});

test('windows: COPPER_PYTHON and COPPER_KICAD_CLI come first', () => {
  const env = { COPPER_PYTHON: 'D:\\py\\python.exe', COPPER_KICAD_CLI: 'E:\\KiCad\\bin\\kicad-cli.exe' };
  const io = fakeIo(['E:\\KiCad\\bin\\python.exe']);
  const c = cmds(candidates('win32', env, io));
  assert.deepStrictEqual(c.slice(0, 2), ['D:\\py\\python.exe', 'E:\\KiCad\\bin\\python.exe']);
});

test('macOS: KiCad.app framework python, both install layouts', () => {
  const fw = 'Contents/Frameworks/Python.framework/Versions/Current/bin/python3';
  const io = fakeIo([`/Applications/KiCad/KiCad.app/${fw}`, `/Applications/KiCad.app/${fw}`, '/opt/homebrew/bin/python3'],
    {}, '/Users/u');
  assert.deepStrictEqual(cmds(candidates('darwin', {}, io)), [
    `/Applications/KiCad/KiCad.app/${fw}`, `/Applications/KiCad.app/${fw}`, '/opt/homebrew/bin/python3', 'python3',
  ]);
  const viaCli = candidates('darwin', { COPPER_KICAD_CLI: `/Volumes/x/KiCad.app/Contents/MacOS/kicad-cli` },
    fakeIo([`/Volumes/x/KiCad.app/${fw}`]));
  assert.strictEqual(viaCli[0].cmd, `/Volumes/x/KiCad.app/${fw}`);
});

test('linux: system python3 first; one with pcbnew wins', () => {
  const io = fakeIo(['/usr/bin/python3', '/usr/local/bin/python3']);
  const c = candidates('linux', {}, io);
  assert.deepStrictEqual(cmds(c), ['/usr/bin/python3', 'python3', '/usr/local/bin/python3', 'python']);
  const info = {
    '/usr/bin/python3': { v: [3, 12], pcbnew: false, pil: false },
    python3: { v: [3, 9], pcbnew: true, pil: true },
    '/usr/local/bin/python3': { v: [3, 11], pcbnew: true, pil: true },
  };
  const { pick } = choose('linux', c, (x) => info[x.cmd] || null);
  assert.strictEqual(pick.cmd, '/usr/local/bin/python3');
  const none = choose('linux', c, (x) => (x.cmd === '/usr/bin/python3' ? { v: [3, 12], pcbnew: false } : null));
  assert.strictEqual(none.pick.cmd, '/usr/bin/python3', 'falls back to the first usable python');
});

test('version gate is 3.10+; windows picks the first usable', () => {
  assert.ok(versionOk([3, 10]) && versionOk([3, 13]) && versionOk([4, 0]));
  assert.ok(!versionOk([3, 9]) && !versionOk(null));
  const c = [{ cmd: 'a', args: [] }, { cmd: 'b', args: [] }];
  const { pick, tried } = choose('win32', c, (x) => (x.cmd === 'a' ? { v: [3, 9] } : { v: [3, 11] }));
  assert.strictEqual(pick.cmd, 'b');
  assert.strictEqual(tried.length, 2);
  assert.strictEqual(choose('win32', c, () => null).pick, null);
});
