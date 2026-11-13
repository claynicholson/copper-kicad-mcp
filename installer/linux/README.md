# Installing Copper on Linux and macOS

Everything on this page installs Copper **for one user, without sudo**, and ends with the same step:

```sh
copper setup
```

On a desktop that opens Copper's setup page in your browser: the KiCad it found, what you want to bring
along from it (libraries, themes, hotkeys, plugins, recent projects), your AI apps, optional GitHub, the
autorouter download and a test call. In a plain terminal (SSH, no display) the same questions are asked
as a wizard. Nothing in KiCad's own settings is changed without asking, and `copper setup --undo` puts
it back.

> **Status.** The commands below follow the documented behaviour of uv, pipx and npm. They were written
> and syntax-checked on Windows. **None of them has been run end to end on macOS or Linux in this
> build.** The parts of Copper they install (the CLI, `copper setup`, `copper link`) are covered by the
> test suite on Windows only; the macOS Keychain and libsecret token stores are untested.

## 1. One line (recommended)

```sh
curl -LsSf https://raw.githubusercontent.com/claynicholson/copper-kicad-mcp/main/installer/install.sh | sh
```

It installs [uv](https://docs.astral.sh/uv/) if missing, downloads Copper's wheel from the latest
[GitHub release](https://github.com/claynicholson/copper-kicad-mcp/releases/latest), checks it against the
release's `SHA256SUMS.txt`, installs it into its own Python 3.12 environment, adds a "Copper setup" entry
to the applications menu on Linux, and runs `copper setup`. It refuses to run as root.

## 2. npx (Node.js 18+)

```sh
npx -y copper-kicad setup
```

The npm package runs Copper on the Python that comes with KiCad (macOS: inside `KiCad.app`; Linux: the
system `python3` that KiCad's packages install `pcbnew` into). `COPPER_PYTHON=/path/to/python3` picks
another Python 3.10+.

## 3. uv or pipx, from the release wheel

```sh
uv tool install --python 3.12 https://github.com/claynicholson/copper-kicad-mcp/releases/download/v1.1.0/copper_kicad-1.1.0-py3-none-any.whl
# or
pipx install https://github.com/claynicholson/copper-kicad-mcp/releases/download/v1.1.0/copper_kicad-1.1.0-py3-none-any.whl
copper setup
```

Update: run the same command with the newer release's wheel (`uv tool install --force ...` /
`pipx install --force ...`). Remove: `copper uninstall && uv tool uninstall copper-kicad` (or
`pipx uninstall copper-kicad`).

`copper setup` registers the absolute path of the `copper` launcher with each AI app, so the apps find it
even though they do not read your shell's PATH. If you later reinstall with a different tool, run
`copper link apps` once so the apps point at the new launcher; `copper link --verify` tells you whether
each app's entry still starts.

## KiCad

Copper needs KiCad 10 with `kicad-cli` on the machine (KiCad 9 can only view and check designs):

| System | Where Copper looks |
|---|---|
| macOS | `/Applications/KiCad/KiCad.app`, `/Applications/KiCad.app`, `~/Applications/KiCad/KiCad.app` |
| Linux | `kicad-cli` on `PATH`, `/usr/bin/kicad-cli`, `/usr/local/bin/kicad-cli` |
| Flatpak | detected, but the Flatpak does not expose `kicad-cli`; install a native KiCad as well |

`COPPER_KICAD_CLI=/path/to/kicad-cli` overrides the search. With several KiCad versions installed,
`copper setup` asks which one Copper should use.

## Linking accounts

```sh
copper link                 # every AI app found: connected or not; one answer connects them all
copper link --verify        # start Copper the way each app would and check that it answers
copper link github          # optional
copper link --unlink cursor
```

GitHub uses your existing GitHub CLI login (`gh auth login`) when `gh` is installed, and Copper then
stores nothing. Without `gh`, GitHub's device sign-in is used if an OAuth client id is configured
(`COPPER_GITHUB_CLIENT_ID`); the token is kept in the system keyring (libsecret via `secret-tool` on
Linux, the login Keychain on macOS). Copper never sees a password and never keeps a token in a plain
file. On a headless machine with no keyring, use `gh`.
