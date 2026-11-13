#!/usr/bin/env node
'use strict';
require('../lib/launcher').main(process.argv.slice(2)).then(
  (code) => { process.exitCode = code; },
  (err) => { process.stderr.write(`copper-kicad: ${err && err.stack || err}\n`); process.exitCode = 1; },
);
