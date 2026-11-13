#!/usr/bin/env node
// Copies the pure-Python copper_kicad package (../src/copper_kicad) into py/ so the
// npm tarball carries exactly the source the wheel is built from. Runs on `npm pack`
// and `npm publish` (prepack). Refuses to pack when the versions disagree.
'use strict';
const fs = require('fs');
const path = require('path');

const here = path.resolve(__dirname, '..');
const repo = path.resolve(here, '..');
const src = path.join(repo, 'src', 'copper_kicad');
const dst = path.join(here, 'py', 'copper_kicad');

function fail(msg) {
  console.error(`vendor: ${msg}`);
  process.exit(1);
}

if (!fs.existsSync(path.join(src, '__init__.py'))) fail(`no Python package at ${src}`);
const init = fs.readFileSync(path.join(src, '__init__.py'), 'utf8');
const m = /__version__\s*=\s*"([^"]+)"/.exec(init);
const pkg = JSON.parse(fs.readFileSync(path.join(here, 'package.json'), 'utf8'));
if (!m || m[1] !== pkg.version) {
  fail(`version mismatch: src/copper_kicad ${m ? m[1] : '?'} vs npm/package.json ${pkg.version}`);
}

const SKIP_DIRS = new Set(['__pycache__']);
const SKIP_EXT = new Set(['.pyc', '.pyo']);
let files = 0;
let bytes = 0;
function copy(from, to) {
  fs.mkdirSync(to, { recursive: true });
  for (const ent of fs.readdirSync(from, { withFileTypes: true })) {
    const a = path.join(from, ent.name);
    const b = path.join(to, ent.name);
    if (ent.isDirectory()) {
      if (!SKIP_DIRS.has(ent.name)) copy(a, b);
    } else if (ent.isFile() && !SKIP_EXT.has(path.extname(ent.name))) {
      fs.copyFileSync(a, b);
      files += 1;
      bytes += fs.statSync(b).size;
    }
  }
}

fs.rmSync(path.join(here, 'py'), { recursive: true, force: true });
copy(src, dst);
fs.writeFileSync(path.join(here, 'py', 'VERSION'), pkg.version + '\n');
fs.copyFileSync(path.join(repo, 'LICENSE'), path.join(here, 'LICENSE'));
console.error(`vendor: copper_kicad ${pkg.version}: ${files} files, ${(bytes / 1e6).toFixed(1)} MB -> npm/py/`);
