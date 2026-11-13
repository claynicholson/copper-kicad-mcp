# Model drivers of copper-agent

How the agent (`src/copper_kicad/agent/`) reaches a model. The engine never calls a model; the loop lives
only in these drivers. The app and the protocol (`HOST_PROTOCOL.md`) see an opaque account `kind`, a plain-text
label and the normalised events; they never see a vendor name, a model id or a secret.

Status on 2026-11-10: everything below runs against local fakes (`tests/test_agent_drivers.py`, 17 tests).
No real provider was called and no real agent CLI is installed on the build machine. Section 7 lists
what that leaves unverified.

## 1. The drivers

| Account kind | File | Reaches | The account needs |
|---|---|---|---|
| `none` | `drivers/none.py` | no model: slash commands only | nothing |
| `api-messages` | `drivers/anthropic_api.py` | Anthropic Messages API, the user's key | `secret`; optional `base_url` (a gateway speaking the same API) |
| `api-chat-completions` | `drivers/openai_compat.py` | any OpenAI-compatible `/chat/completions` endpoint, hosted or local | `base_url` (up to the version segment, e.g. `http://localhost:11434/v1`); `secret` unless the server needs none |
| `cli-stream-json` | `drivers/claude_code.py` | the user's own Claude Code CLI, headless | nothing if `claude` is on PATH, else `command` = its path |
| `cli-exec-json` | `drivers/codex.py` | the user's own Codex CLI, headless | nothing if `codex` is on PATH, else `command` = its path |
| `cli-acp` | `drivers/acp.py` | any agent CLI that speaks the Agent Client Protocol | `command` = the command line that starts it in ACP mode |

Shared, not drivers: `_http.py` (requests, SSE, retries, error mapping, cancel), `_cli.py` (child process,
tree kill, safe arguments, error wording), `_gate.py` + `_bridge.py` (the one MCP server a CLI gets).

**Model ids are configuration.** No file names a model. `accounts.test` asks the provider for its list
(`GET /v1/models`, or `GET <base_url>/models`) and returns it; the user picks one when connecting and it is
stored as the account's `default_model`; `turn.start.model` overrides it per turn. An API account with no model
ends the turn with `error(engine, NO_MODEL)` before any request is sent. For the CLI kinds a missing model
means "the CLI's own default". `quick` is false for every listed model: which model is cheap is the user's call.

## 2. API-key drivers

Both run the tool loop in the agent: request, stream, run the requested engine tools through
`turn.call_tool` (in order, one at a time: the engine has process-wide state), send every result back, repeat.

| | `api-messages` | `api-chat-completions` |
|---|---|---|
| Stable prefix | `system` = one text block with `cache_control: ephemeral`. Tools render before it, so this one breakpoint caches catalogue + system prompt. A second breakpoint sits on the newest message and moves forward each step | The system message and tool list are byte-identical between turns, which is all automatic prefix caching needs |
| Volatile part | focus line, notes and the user's words, in the last user message | same |
| Not sent | `thinking`, sampling parameters, `tool_choice` (the accepted values differ per model generation) | sampling parameters, output limit |
| Assistant blocks | sent back unchanged during the loop, thinking blocks with their signature included | text + `tool_calls`; reasoning text is shown, not sent back |
| Parallel tool calls | all `tool_result` blocks in ONE user message, `is_error` on failures | one `tool` message per call id; a missing id is filled in |
| Terminal marker | `message_stop` | a `finish_reason` (a bare `[DONE]` is not enough; a missing `[DONE]` is tolerated) |
| Usage | `in` = input + cache-creation + cache-read, `cached` = cache-read, summed over the loop | `prompt_tokens` / `completion_tokens` / `cached_tokens` via `stream_options.include_usage`; a server that rejects the option is asked again without it |

Failure mapping (`_http.failure`, `_http.stream_error`):

| What happened | Event |
|---|---|
| 401, 403 | `error(auth, HTTP_401)`, not retried |
| 402 | `error(rate, HTTP_402)`, not retryable |
| 429 | retried after `retry-after` / `retry-after-ms`; longer than 20 s: `error(rate, HTTP_429, retry_after_s)` at once |
| 408, 409, 5xx, connection refused, timeout | retried with backoff (0.5 s doubling, 3 retries), then `error(network, ...)` |
| 400, 404, 413, 422 | `error(protocol, HTTP_4xx)`; 404 carries the fix "check the model id and the base URL" |
| error object inside a stream | by its type: auth / rate / network / protocol |
| stream ends without the terminal marker | `error(network, TRUNCATED)` |
| output limit | `error(engine, MAX_TOKENS)`; refusal / content filter: `error(engine, REFUSED)` |
| `budget_tokens` spent | `error(rate, BUDGET)` before the next request; the response size is capped to what is left |
| more than 40 model requests in one turn | `error(engine, MAX_STEPS)` |

A request is retried only while nothing of it reached the user: text that was shown is never asked for twice.

Cancel: a watcher closes the response's socket the moment the turn is cancelled, so the blocked read returns
at once (measured: under 0.1 s on Windows; `shutdown()` alone does not wake a blocked `recv` there).

Secrets: read with `accounts.secret(id)` (`link/store.py`), put in one request header, scrubbed from provider
error texts. A key is refused over plain `http://` to anything but this machine (`error(auth, INSECURE_URL)`).

Environment: `COPPER_AGENT_MAX_TOKENS` (16000), `COPPER_AGENT_MAX_STEPS` (40), `COPPER_AGENT_RETRIES` (3),
`COPPER_AGENT_BACKOFF_S` (0.5), `COPPER_AGENT_RETRY_MAX_WAIT_S` (20), `COPPER_AGENT_HTTP_TIMEOUT_S` (120),
`COPPER_AGENT_API_VERSION` (the messages API version header).

## 3. Agent-CLI drivers: the common shape

The CLI is the user's unmodified program, signed in by its own flow. Copper passes it no credential and reads
none from it.

```
app <== stdio ==> copper-agent <== 127.0.0.1 + token ==> _bridge.py (MCP stdio) <== stdio ==> the CLI
```

* **One MCP server, no engine in it.** Per turn the driver opens a tool gate (`_gate.py`): a loopback listener
  with a fresh token, written to a file in a private temp folder that is deleted when the turn ends. The CLI is
  given `python _bridge.py <gate file>` as its only MCP server. The bridge lists the catalogue
  (`prompt.tool_catalogue(mode)`) and forwards each `tools/call` to the gate, where it runs through
  `turn.call_tool`. So a CLI-driven edit emits the same tool events, uses the same shadow checkout and lands
  through the same `host.apply` as an API-driven one, and cancel stops it the same way.
* **Its own tools are off.** Each driver switches the CLI's file, shell and web tools off with that CLI's flags.
  If such a call still appears in the stream it is shown as a tool row named `cli:<name>`, never hidden.
* **Nothing that needs quoting is on the command line.** Prompts go in on stdin, configuration in files. On
  Windows these CLIs are often `.cmd` shims run by `cmd.exe`; an argument containing `" % & | < > ^` or a line
  break is refused (`error(engine, UNSAFE_ARGUMENT)`) rather than guessed at.
* **Sessions.** The CLI's session id is stored with the conversation (`sessions.set_resume_id`) and passed
  back on the next turn. A fresh session gets the earlier turns as text at the top of the message.
* **Cancel** kills the whole process tree. A CLI that prints nothing for `COPPER_AGENT_CLI_IDLE_S` (300 s) while
  no Copper tool is running is killed: `error(network, CLI_SILENT)`.
* **Errors** are worded by the CLI, so the kind comes from its text or exit code: sign-in words give
  `auth` (fix: "run the CLI once in a terminal and sign in"), limit words `rate`, connection words `network`,
  anything else `protocol`. Output that ends before the terminal marker is `error(network, TRUNCATED)`.

## 4. `cli-stream-json` (Claude Code)

```
claude -p --output-format stream-json --verbose --include-partial-messages
       --mcp-config <file> --strict-mcp-config
       --tools "" --disallowedTools Bash,Edit,Write,Read,Glob,Grep,NotebookEdit,WebFetch,WebSearch,Agent,Task
       --allowedTools mcp__copper --permission-mode dontAsk
       --append-system-prompt-file <file>
       [--model <id>] [--resume <session id>] [--no-session-persistence]
```

`--bare` is never passed: bare mode does not use the subscription login. The user turn is written to stdin.
Environment added for the child: `MCP_TOOL_TIMEOUT=1800000`, `MCP_TIMEOUT=30000`.

| Stream line | Becomes |
|---|---|
| `system` / `init` | `session_id` stored; a failed `copper` MCP server ends the turn (`protocol`, `CLI_MCP_FAILED`) |
| `stream_event` `content_block_delta` | `text_delta` (answer or reasoning) |
| `assistant` | text blocks only if no delta carried them; usage per message id (also the budget check) |
| `assistant` `tool_use` not named `mcp__copper__*` | a `cli:<name>` tool row, closed by the matching `user` `tool_result` |
| `system` / `api_retry` | remembered: its `error` category names the kind if the run fails |
| `result` | terminal marker. `usage` -> `usage`; `total_cost_usd` is the conversation's total, so the turn's share is reported; `is_error` -> `error` |

A `--resume` that the CLI no longer knows ("No conversation found") is retried once as a fresh session.

## 5. `cli-exec-json` (Codex)

```
codex exec --json --skip-git-repo-check --sandbox read-only --ignore-user-config
      -c features.shell_tool=false -c features.view_image=false -c features.multi_agent=false
      -c features.apps=false -c tools.web_search=false -c approval_policy=never
      -c mcp_servers.copper.command='<python>' -c mcp_servers.copper.args=['<bridge>','<gate file>']
      -c mcp_servers.copper.default_tools_approval_mode=approve
      -c mcp_servers.copper.tool_timeout_sec=1800 -c mcp_servers.copper.startup_timeout_sec=30
      -c mcp_servers.copper.required=true
      [-m <model>] [resume <thread id>] -
```

`--ignore-user-config` keeps the user's own MCP servers out; `COPPER_CLI_USER_CONFIG=1` drops that flag for
users whose provider is set up in their `config.toml`. Paths are TOML literal strings (single quotes). There
is no safe flag for extra system text, so Copper's instructions open the first message of a session.

| Stream line | Becomes |
|---|---|
| `thread.started` | `thread_id` stored as the resume id |
| `item.completed` `agent_message` / `reasoning` | whole-message `text_delta` (this CLI does not stream deltas) |
| `item.*` `mcp_tool_call` on server `copper` | nothing: the gate already reported it |
| `item.*` `command_execution`, `file_change`, `web_search`, other MCP calls | a `cli:<kind>` tool row |
| `error` | remembered ("Reconnecting..." is transient) |
| `turn.completed` | terminal marker. Its usage is the thread's running total; the turn reports total minus the previous total |
| `turn.failed` | `error`, kind from the message |

`budget_tokens` cannot stop this CLI mid-run (usage arrives only at the end); it is checked when the turn ends.

## 6. `cli-acp` (generic)

Copper is the ACP client: `initialize` (protocol version 1, **no** `fs` and **no** `terminal` capability),
`session/new` or `session/resume` with `mcpServers = [copper bridge]`, `session/prompt`, then `session/update`
notifications until the prompt's result arrives with a `stopReason`.

* `agent_message_chunk` / `agent_thought_chunk` -> answer / reasoning text.
* `session/request_permission` is answered at once: allow-once for a Copper tool, the reject option for
  anything else. A call counts as Copper's when its title or name carries a catalogue tool name, or names the
  `copper` server and is not classed `edit` / `delete` / `move` / `execute` by the agent. In doubt: rejected.
* `fs/*`, `terminal/*` and any other request from the agent: JSON-RPC error `-32601`.
* `stopReason`: `end_turn` ok, `cancelled` cancelled, `refusal` `error(engine, REFUSED)`, `max_tokens` /
  `max_turn_requests` `error(engine)`.
* Cancel sends `session/cancel`, waits 0.3 s, then kills the tree.
* ACP reports context size, not billed tokens: no `usage` event.

## 7. Verified and not verified

Verified by running (`tests/test_agent_drivers.py`, Windows 11, KiCad 10's Python 3.11, fakes only):

* both API shapes: text streaming, usage, the cacheable prefix (same bytes on the next turn), history replay,
  quick mode's tool subset, parallel tool calls with one failing, thinking blocks returned unchanged, an edit
  landing through `host.checkout` / `host.apply` with one undo, 429 with retry, 429 with a long wait, 5xx retry,
  401, 404, mid-stream cut (after text: no retry; before text: retry), in-stream error object, output limit,
  cancel during a blocked read, budget, no key over plain http, a dead port, a keyless local server;
* the three CLI drivers against fake CLIs that speak the documented stream formats and really start the bridge
  and call tools through MCP: the exact command lines above, through a Windows `.cmd` shim and as a plain
  program; resume ids; cumulative usage and cost turned into per-turn figures; a CLI-driven `sch_apply`
  arriving as a normal proposal with one undo; own-tool rows; cut stream; sign-in failure; cancel with the
  process tree gone; a missing program;
* the bridge alone with no gate behind it (answers, never hangs), and the gate refusing a wrong token.

Not verified, needs the real thing:

* any real provider: real model behaviour, real cache hits, real rate-limit headers, TLS (the cancel path
  closes the raw socket under a TLS stream, which was only exercised on plain loopback HTTP);
* the real Claude Code and Codex CLIs: that every flag above is accepted by the installed version, that
  `--tools ""` survives a real shim, that `mcp__copper` allows the bridge's tools without a prompt, that
  `exec resume` accepts the same flags, that `default_tools_approval_mode=approve` lets MCP calls run in a
  non-interactive run (an open upstream report says some versions cancel them), that `--ignore-user-config`
  keeps the sign-in, the exact shape of `result.usage` on a resumed session, and that their built-in tools
  are fully gone with these flags;
* a real ACP agent: how it titles MCP tool calls in permission requests (the allow rule depends on it),
  `session/resume` availability, behaviour on `session/cancel`;
* macOS and Linux (process-group kill, `shlex` splitting of `command`);
* the terms under which each vendor allows its CLI to be driven by another program: re-read before release.

Known gaps: images in tool results are dropped for `api-chat-completions` (a note says so to the model);
server-side refusal fallbacks of the messages API are not requested; no model is marked `quick`.
