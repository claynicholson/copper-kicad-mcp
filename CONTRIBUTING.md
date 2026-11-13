# Contributing

Thanks for helping. Copper is plain Python (3.10+, Pillow) driving KiCad 10; there is nothing to build.

```bash
git clone https://github.com/claynicholson/copper-kicad-mcp && cd copper-kicad-mcp
PYTHONPATH=src python -m copper_kicad doctor      # KiCad's own python works too
python tests/test_protocol.py                     # drives the real MCP server over stdio
```

- **Tests are plain scripts** (`python tests/test_<area>.py`, pytest also works). Most need KiCad 10 installed;
  `test_protocol`, `test_compat` and `test_tokens` (with `COPPER_TEST_NO_REPLAY=1`) do not. Run the suite for the area
  you touched, with `COPPER_HOME` and `KICAD_CONFIG_HOME` pointed at throw-away folders.
- **Every change is verified.** Tools report what they changed and re-run KiCad's checks; a PR should do the same:
  say which tests ran, on which KiCad, and what could not be checked.
- **Keep the tool surface small.** New behaviour usually belongs in an existing tool's ops or arguments, with
  worked examples in `copper_help`.
- **No vendor lock-in.** Parts vendors and fabs are providers and profiles ([docs/PROVIDERS.md](docs/PROVIDERS.md),
  [docs/FAB_PROFILES.md](docs/FAB_PROFILES.md)); the server never calls a language model.

Architecture and module contracts: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Releases: [docs/RELEASING.md](docs/RELEASING.md).
By contributing you agree that your work is released under the [MIT license](LICENSE).
