# EDA-bench with Copper

[EDA-bench](https://github.com/EDA-bench/eda-bench) (ICML 2026) asks an agent to build a complete KiCad project
(schematic and placed, routed PCB) from an engineering brief, for 35 public open-hardware boards. A separate
grader with no network access scores the result with ERC, DRC, connector pin matching, routed connectivity and
ngspice simulation of the board's I/O behaviour. It runs on [Harbor](https://www.harborframework.com/).

This folder runs Claude Code with Copper's MCP server on it.

## KiCad 10, not the stock images

EDA-bench's own images use Debian's KiCad 9, and Copper edits only with KiCad 10+ (`FLOOR` in
`kicad/compat.py`). KiCad 9 cannot open KiCad 10 files, so the grader has to move to KiCad 10 as well.
`prepare.sh` builds both images on Ubuntu 26.04 with KiCad 10.0.8 from KiCad's official PPA (the stock ones
are Debian trixie). The PPA builds for amd64 and arm64, so the images run natively on x86 and Apple Silicon.
It also writes a copy of the dataset whose `task.toml` files point at those images. Tasks, instructions,
grader code and scoring are unchanged.

So **scores are not directly comparable with the paper's KiCad 9 numbers**. To get a comparison, run a baseline
agent on the same images (below). The agent image contains no Copper. `copper-claude-code` installs it at setup
time, so plain `claude-code` on the same dataset differs from it only by Copper.

## Files

| file | what it is |
|---|---|
| `images/agent.Dockerfile` | the EDA-bench agent image: Ubuntu 26.04, KiCad 10.0.8 + stock symbol/footprint libraries (PPA, pinned), ngspice, Python, CLI tools |
| `images/verifier.Dockerfile` | EDA-bench's verifier Dockerfile with that image as its base |
| `prepare.sh` | clones EDA-bench at the pinned commit, builds both images, writes `.work/dataset-kicad10`, runs `harbor sync` |
| `copper_agent.py` | `CopperClaudeCode`: Harbor's own `ClaudeCode` agent, plus a Copper install and a `copper` stdio MCP server |

`CopperClaudeCode` changes nothing else about Claude Code. The prompt is the task instruction verbatim, and
the permission mode is Harbor's default (`bypassPermissions`). Copper is installed into `/opt/copper`, a venv
that uses the image's system Python so `pcbnew` and Pillow come from KiCad. By default it installs a wheel
built from **this checkout**, so the run measures your working tree.

## Run

Needs Docker (Docker Desktop, or `colima start --vm-type vz` on a Mac), [uv](https://docs.astral.sh/uv/),
Harbor 0.19.0 and an Anthropic API key. If your shell has `ANTHROPIC_BASE_URL` set (some Claude apps set it),
unset it for these runs: Harbor forwards it into the container.

```bash
uv tool install 'harbor==0.19.0'
```

```bash
bench/edabench/prepare.sh
```

First check that the grader works on KiCad 10. The oracle submits each task's reference design, so a task that
scores below 1 here is a KiCad 10 grading difference, not an agent failure:

```bash
harbor run --path bench/edabench/.work/dataset-kicad10 --agent oracle
```

One task with Copper (run from the repository root, so `copper_agent` is importable):

```bash
PYTHONPATH=bench/edabench harbor run --path bench/edabench/.work/dataset-kicad10/usb_c_female_breakout --agent copper_agent:CopperClaudeCode --model anthropic/claude-opus-5-5
```

The whole benchmark, then the same model without Copper as the baseline:

```bash
PYTHONPATH=bench/edabench harbor run --path bench/edabench/.work/dataset-kicad10 --agent copper_agent:CopperClaudeCode --model anthropic/claude-opus-5-5 --n-concurrent 4
```

```bash
harbor run --path bench/edabench/.work/dataset-kicad10 --agent claude-code --model anthropic/claude-opus-5-5 --n-concurrent 4
```

Inspect the results with `harbor view jobs`. Each trial's agent logs also hold `copper-doctor.json` (what Copper
saw: KiCad version, libraries, capabilities) and `copper-sessions/` (Copper's own tool ledger).

Useful agent options (`--ak key=value`):

- `copper_spec="copper-kicad @ git+https://github.com/claynicholson/copper-kicad-mcp@<ref>"`: test a pushed
  ref instead of the working tree.
- `copper_wheel=<path>`: use a wheel you built yourself.
- Any Claude Code option: `max_turns`, `reasoning_effort`, `max_budget_usd`, `append_system_prompt`. If you set
  `append_system_prompt` (for example to point the model at Copper), set the same one on the baseline.

## Things to know

- Each task allows the agent up to 3 hours. 35 tasks is a sizeable API bill, so start with single tasks.
- The agent phase has public network access (EDA-bench's design). Copper uses it for part lookups
  (LCSC/JLCPCB, EasyEDA). The grader runs offline and sees only `/workspace/final_project`.
- Copper never downloads Freerouting by itself, so routing uses Copper's built-in router.
- Report the EDA-bench commit (pinned in `prepare.sh`), Harbor version, KiCad 10.0.8, the model, the Copper
  ref and the baseline run with any score.
