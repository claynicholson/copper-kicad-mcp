# Releasing Copper for KiCad

Every install path in the README depends on public resources. None of them work until **all** of these exist, so
publish them together and run the checks below before announcing anything. There is no PyPI package.

| Resource | Used by |
|---|---|
| Public GitHub repo `claynicholson/copper-kicad-mcp` | README links and images, plugin marketplaces, `server.json` repository |
| GitHub release `v<version>` with the release assets listed in step 7 | Download links, `install.ps1` / `install.sh` (wheel + `SHA256SUMS.txt`), `.mcpb`, `server.json` mcpb package, the README's video link (`copper-release.mp4`) |
| npm package `copper-kicad@<version>` (`dist/copper-kicad-<version>.tgz`) | `npx -y copper-kicad`, Add to Cursor / Install in VS Code, `plugin/.mcp.json`, `server.json` npm package |
| `main` branch containing `installer/install.ps1`, `installer/install.sh` | the one-liners (raw.githubusercontent.com) |
| MCP Registry entry `io.github.claynicholson/copper` | registry listings; validated against the npm package's `mcpName` |

## Steps

1. Bump the version in `pyproject.toml` **and** `src/copper_kicad/__init__.py` (the build refuses a mismatch), and in
   `server.json`, `mcpb/manifest.json`, both plugin manifests and both marketplace files
   (`tests/test_distribution.py` checks them). The build stamps `npm/package.json` and the `copper-kicad@<version>`
   pin in `plugin/.mcp.json` itself. Never re-publish different bytes under the same version.
2. Update `CHANGELOG.md` and the `DEFAULT_VERSION` fallback in `installer/install.ps1` and `installer/install.sh`.
3. Build everything from a clean tree: `python scripts/build_release.py --validate-mcpb`. It clears `dist/`, checks
   the wheel and the npm tarball against `src/` file for file, stamps `server.json` (version, mcpb URL,
   `fileSha256`) and writes `dist/SHA256SUMS.txt` and `dist/BUILD-INFO.json`. It also copies the release video
   `copper-release.mp4` into `dist/` (from `../copper-media/release-video/readme/`, or `--release-video PATH`); the
   README's preview GIF links to it as a release asset, so the build stops when it is missing (`--skip-video` makes
   a local test build that `--check` then refuses).
4. Sign `Copper-for-KiCad-<version>-Setup.exe` with Authenticode if a certificate is available, then run
   `python scripts/build_release.py --after-signing`: it re-records that file's sha256 in `BUILD-INFO.json` and
   rewrites `SHA256SUMS.txt` (every other artifact must still match the build). Unsigned, Windows SmartScreen warns
   every new user.
5. `python scripts/build_release.py --check` must pass (after signing too).
6. Make the repo public (first release only), set the description, topics, homepage and social preview from
   [`docs/internal/REPO_SETTINGS.md`](internal/REPO_SETTINGS.md).
7. Tag `v<version>`, push, create the GitHub release and attach exactly these files from `dist/` (not
   `BUILD-INFO.json` or `.gitignore`):
   - `copper_kicad-<version>-py3-none-any.whl` and `copper_kicad-<version>.tar.gz`
   - `copper-<version>.mcpb`
   - `copper-kicad-<version>.tgz`
   - `Copper-for-KiCad-<version>-Setup.exe` and `Copper-for-KiCad-<version>-portable-win64.zip`
   - `copper-release.mp4`
   - `SHA256SUMS.txt`

   Optional: for an inline player in the README, edit it in GitHub's web editor and drop
   `copper-release.mp4` into the comment's spot above the preview GIF (GitHub uploads it as an attachment).
8. `npm publish dist/copper-kicad-<version>.tgz` (the exact tarball the build checked).
9. Publish `server.json` to the MCP Registry: `mcp-publisher login github` then `mcp-publisher publish`.
10. `python scripts/check_release_urls.py --npx` must report 0 problems: every URL in the README, docs/INSTALL.md,
    `server.json`, the plugin and marketplace manifests, `mcpb/manifest.json` and the installers resolves, the npm
    package carries the `mcpName`, and `npx -y copper-kicad@<version> --version` runs.

## When KiCad releases a new version

KiCad point releases arrive every few weeks and do not wait for a Copper release. For each one: install it, run
`python tests/test_compat.py`, regenerate and diff `tests/data/cli-10.0.json`, read the release notes, then update
`KNOWN_ISSUES` (and `FORMATS` for a new series) in `src/copper_kicad/kicad/compat.py` and the evidence marks in
`docs/COMPATIBILITY.md`. `.github/workflows/kicad-matrix.yml` runs the suites on the KiCad Docker images.

## Codex plugin metadata

Codex reads `.agents/plugins/marketplace.json` (the repo root `.claude-plugin/marketplace.json` is its
legacy-compatible location). Each entry needs `policy.installation` (`AVAILABLE`), `policy.authentication`
(`ON_INSTALL`) and `category`. Users add the marketplace with `codex plugin marketplace add claynicholson/copper-kicad-mcp`
and install the plugin from the Plugins directory in the Codex app; the CLI has no install command. Re-check the
current documentation at https://developers.openai.com/codex/plugins/build.md before each release.
