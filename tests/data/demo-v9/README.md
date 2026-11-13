# KiCad 9-format demo (test data)

`complex_hierarchy/` is KiCad's own `demos/complex_hierarchy` project, copied from a KiCad 10.0.6 install with
LF line endings as in KiCad's source tree (the same files as KiCad tag 10.0.7; `.gitattributes` keeps them
byte-exact). Its schematics are format 20250114 and its board 20241229, written by KiCad 9.0.

`tests/test_compat.py` needs a real KiCad 9 design to check the format-upgrade guard. It used to take this demo
from the installed KiCad, but KiCad 10.0.8 re-saved the demo in the 10.0 format (upstream commit d1a5ae8b), so
the installed copy is no longer a KiCad 9 design. This frozen copy keeps the tests meaningful on every 10.0.x.

The files are part of KiCad (https://gitlab.com/kicad/code/kicad), distributed under KiCad's licence
(GPL-3.0-or-later, see KiCad's LICENSE.README). They are used only as test data and are not part of any
Copper package.
