# Host protocol v1: the Copper app and `copper-agent`

This is the authoritative contract between the Copper desktop app (the KiCad fork: C++, owns the open
document and the UI) and `copper-agent` (Python: owns the model loop and runs the engine). The executable
forms are `src/copper_kicad/agent/` (the agent), `src/copper_kicad/host/` (the engine-side host client) and
`tests/fakehost.py` (a Python stand-in for the app). `tests/test_agent_protocol.py` exercises everything on
this page that does not need a compiled app.

The protocol names no model vendor, no AI client and no parts vendor. Whatever the agent can drive is
listed at run time in `hello` (`drivers`, `accounts`, `commands`); the app renders those lists.

Contents: 1 transport, 2 lifecycle and ordering, 3 errors, 4 app -> agent, 5 agent -> app requests
(`host.*`), 6 events, 7 patches, 8 seams for implementers, 9 engines outside the agent, 10 versioning,
11 a worked example, 12 what is verified.

---

## 1. Transport

| | |
|---|---|
| Process | The app starts one agent per app process: `<python> -m copper_kicad.agent`, working directory = the folder that contains the `copper_kicad` package (KiCad's bundled Python ignores `PYTHONPATH`). |
| Channel | The child's stdin (app -> agent) and stdout (agent -> app). |
| Framing | One JSON-RPC 2.0 object per line, UTF-8, terminated by `\n`. No embedded newlines (JSON escapes them). No length prefix, no batches (a JSON array is answered with `-32600`). A leading UTF-8 BOM and blank lines are ignored. |
| stderr | The agent's log. The app redirects it to `<user data>/copper/logs/agent.log`. It never carries protocol data, request params or secrets. |
| Line size | Both sides must accept lines of at least 32 MiB (a patch for a large sheet is a few hundred kB). |
| Direction | Both sides send requests, notifications and responses. Ids are per direction: the app's request ids are its own (string or integer); the agent's are strings `"a1"`, `"a2"`, ... |
| Units | All coordinates are millimetres in the document's own frame. Boxes are `[x0, y0, x1, y1]`. |
| Unknown fields | Receivers ignore fields they do not know. Absent and `null` mean the same: "not given". |

The agent guarantees that nothing except protocol lines reaches its stdout: at start it re-points file
descriptor 1 at stderr and file descriptor 0 at the null device for the engine and every child process it
starts (kicad-cli, board workers), and keeps private handles for the protocol.

Threading in the app: the reader thread parses lines and posts them to the UI thread; every `host.*`
request is executed on the UI thread. The reader thread never touches the document.

## 2. Lifecycle and ordering rules

1. **`hello` first.** Before a successful `hello` the agent answers only `hello`, `ping` and `shutdown`;
   anything else gets `NOT_READY`. `hello` may be sent again later (project changed) when no turn is running.
2. **One turn at a time** (v1, `hello.limits.concurrent_turns`). `turn.start` or `command.run` while a turn
   is active is refused with `BUSY`. The app queues or disables the composer.
3. **Answer before events.** The response to `turn.start` / `command.run` (carrying the turn id) is written
   before the first `event` of that turn.
4. **Events of a turn are totally ordered** by `seq`, starting at 1, no gaps.
5. **Every accepted turn ends with exactly one `done` event**, and `done` is its last event. This holds for
   normal completion, cancellation, driver failure, driver crash, a silent driver (watchdog) and loss of the
   channel. An `error` event, if any, is immediately followed by `done` with `status: "error"`.
6. **Cancel.** After `turn.cancel` the turn ends with `done` (`status: "cancelled"`) within
   `limits.cancel_grace_s` seconds even if the driver does not cooperate. Work that cannot be interrupted
   (a running engine tool) finishes in the background; its result is discarded and it can no longer reach
   the document (rule 8).
7. **Host requests belong to a turn.** While the agent waits for a `host.*` answer the turn's idle watchdog
   is paused; the per-method timeout in section 5 applies instead. When the agent stops waiting (timeout,
   cancel, turn ended) it sends the notification `host.cancelled`. The app may still answer; the answer is
   ignored.
8. **Nothing reaches the document except through `host.apply`** (schematic) or the file-job sequence of
   section 5.9 (board). The agent refuses to send `host.*` requests for a turn that is cancelled or ended.
9. **EOF.**
   * Agent sees EOF on stdin (the app went away): pending `host.*` requests fail with `HOST_GONE`, every open
     turn gets `error` (`kind: "protocol"`, `code: "HOST_GONE"`) and `done`, conversations are flushed, the
     agent exits with code 0.
   * App sees EOF on the agent's stdout (the agent crashed or was killed): the app's RPC layer must
     **synthesise** for every open turn a `tool_end` (`ok: false`, `error.code: "ABORTED"`) for each tool call
     still open, then an `error` event (`kind: "protocol"`, `code: "AGENT_EXITED"`) followed by `done`
     (`status: "error"`), fail its own pending requests, and offer Restart. The pane never waits on
     a dead agent.
10. **Watchdog** (agent side, values in `hello.limits`): a turn with no event, no running tool and no pending
    host request for `idle_timeout_s` ends with `error` (`kind: "network"`, `code: "WATCHDOG"`); a tool with no
    progress for `tool_timeout_s` ends the turn with `kind: "engine"`, `code: "WATCHDOG_TOOL"`; a turn longer
    than `turn_timeout_s` ends with `code: "TURN_TIMEOUT"`.
11. **Shutdown.** The app either sends `shutdown` or simply closes the agent's stdin. Both are clean.

## 3. Errors

A failed request is answered with a JSON-RPC error object:

```json
{"jsonrpc":"2.0","id":4,"error":{"code":-32000,"message":"another turn is running","data":{"code":"BUSY","turn":"t-5f0c9a1e"}}}
```

`error.code` is one of the JSON-RPC codes below. For every application error it is `-32000` and
**`error.data.code` (a string) is what programs switch on**. `message` is for logs and for the user.

| `error.code` | `data.code` | Meaning |
|---|---|---|
| -32700 | `PARSE_ERROR` | The line was not JSON. Answered with `id: null`. The connection stays up. |
| -32600 | `INVALID_REQUEST` | Not an object, a batch, a non-string method, non-object params, or neither request nor response. |
| -32601 | `METHOD_NOT_FOUND` | Unknown request method. Unknown notifications are ignored silently. |
| -32602 | `INVALID_PARAMS` | A parameter is missing or has the wrong type. |
| -32603 | `INTERNAL` | A bug in the receiver. |
| -32000 | see below | Application error. |

Application codes answered by the **agent**: `NOT_READY`, `UNSUPPORTED_PROTOCOL` (`data.min`, `data.max`),
`BUSY`, `UNKNOWN_TURN`, `UNKNOWN_COMMAND`, `UNKNOWN_ACCOUNT`, `UNKNOWN_DRIVER`, `NO_DRIVER`,
`UNKNOWN_CONVERSATION`, `BAD_ARGUMENT`, `STORE_UNAVAILABLE` (no OS credential store).

Application codes answered by the **app** to `host.*` requests:

| `data.code` | Meaning | What the agent does |
|---|---|---|
| `STALE_BASE` | `base_rev` is not the document's current revision. Nothing was applied. `data.rev` = current. | Drops its checkout; the tool result tells the model to call again. |
| `BUSY` | The editor cannot accept a change now (modal dialog, interactive tool running, review in progress). Nothing was applied. | Reports it in the tool result. |
| `PARSE` | A patch's s-expression did not parse, or a side file path is invalid. Nothing was applied. | Tool failure. |
| `UNKNOWN_ITEM` | A uuid in `remove` is not in the document. Nothing was applied. | Tool failure. |
| `DUPLICATE_ITEM` | An added uuid already exists and is not being removed. Nothing was applied. | Tool failure. |
| `NO_SHEET` | A patch names a sheet file that is not part of the open schematic. Nothing was applied. | Tool failure. |
| `NO_DOCUMENT` | That document is not open in this app process. | Skips optional steps (saving a schematic that is not open). |
| `DENIED` | The user refused. | Tool failure with `DENIED`. |
| `UNSUPPORTED` | The host does not implement the method (see `hello.host_methods`). | Skips optional steps. |
| `INTERNAL` | A bug in the app. | Tool failure. |

Codes the agent synthesises for its own callers (never on the wire from the app): `TIMEOUT`, `HOST_GONE`,
`CANCELLED`, `NO_HOST`.

## 4. App -> agent

### 4.1 `hello` (request)

```json
{"jsonrpc":"2.0","id":1,"method":"hello","params":{"protocol":1,"app_version":"10.0.8-copper.1","kicad_cli":"C:/Program Files/Copper/bin/kicad-cli.exe","python":"C:/Program Files/Copper/bin/python.exe","project_dir":"D:/boards/sensor","frame":"schematic","host_methods":["host.checkout","host.apply","host.reveal","host.checkpoint","host.save","host.reload","host.verify","host.ask_permission"]}}
```

| Param | Type | |
|---|---|---|
| `protocol` | integer, required | The highest protocol version the app speaks. |
| `app_version` | string | For the log. |
| `kicad_cli` | string or null | The app's own `kicad-cli`. When it is an existing file the agent pins the engine to it ("the host is the fork"). |
| `python` | string or null | The app's Python (the one that can `import pcbnew`). Informational in v1. |
| `project_dir` | string or null | Folder of the open project; null in the project manager with no project. Must exist. |
| `frame` | string | `"schematic"`, `"board"`, `"manager"`, ...: which frame started the agent. Informational. |
| `host_methods` | array of strings, optional | The `host.*` methods this app build implements. Default: all eight. The agent fails calls to others locally with `UNSUPPORTED`. |

Result:

```json
{"protocol":1,"engine_version":"1.0.0","agent":{"pid":48572,"python":"3.11.5"},
 "accounts":[{"id":"none","kind":"none","label":"No account (commands only)","has_secret":false}],
 "drivers":[{"kind":"none","label":"No account (commands only)","needs":[],"available":true,"reason":""}],
 "commands":[{"name":"help","title":"List the commands","usage":"/help","args":{},"required":[],"documents":["schematic","board"],"needs_project":false}],
 "limits":{"concurrent_turns":1,"idle_timeout_s":60,"tool_timeout_s":1800,"turn_timeout_s":3600,"cancel_grace_s":2},
 "load_errors":{}}
```

| Result field | |
|---|---|
| `protocol` | The version both sides use from now on: `min(app's, agent's)`. |
| `accounts` | Account records (section 4.8). The first is always the built-in `none`. |
| `drivers` | Kinds of account the agent can drive: `kind` (opaque string), `label` (plain text for the picker), `needs` (subset of `"secret"`, `"base_url"`, `"command"`: what `accounts.add` must be given), `available` + `reason` (for example a CLI that is not installed). |
| `commands` | No-model commands: `name`, `title`, `usage`, `args` (JSON-schema fragments by argument name), `required`, `documents` (where to offer it), `needs_project`. |
| `limits` | The agent's timing rules (section 2). |
| `load_errors` | `{module: reason}` for drivers / commands that failed to load. Empty in a healthy install. |

Errors: `INVALID_PARAMS`, `UNSUPPORTED_PROTOCOL`, `BUSY`.

### 4.2 `turn.start` (request)

```json
{"jsonrpc":"2.0","id":7,"method":"turn.start","params":{"conversation":"20261110-104500-a1b2c3","text":"add a 100n cap on each VDD pin of this","focus":{"document":"schematic","sheet_path":"/","mode":"selection","refs":["U3"],"pins":[],"nets":["+3V3","GND"],"items":[{"uuid":"6f0c0f1e-5a3b-4a53-9d0e-2f3f1c1f7a10","type":"symbol"}],"source":"kicad","seq":41,"rev":12},"mode":"chat","account":"none","model":null}}
```

| Param | Type | |
|---|---|---|
| `conversation` | string or null | Conversation id, `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`. Null: the agent creates one and returns it. |
| `text` | string, required, non-empty | The user's words. With the `none` account, text starting with `/` runs a command. |
| `focus` | focus object (4.7) or null | Null: the last `focus.changed` is used. |
| `mode` | `"chat"` (default), `"quick"` (one-line quick edit: small tool set, no history), `"agent"` (multi-step run with `budget_tokens`) | |
| `account` | string | An account id from `accounts`. Default `"none"`. |
| `model` | string or null | Model id for that account; null = the account's default. |
| `budget_tokens` | integer, optional | Agent mode: the run stops when it would exceed this. |

Result: `{"turn": "t-5f0c9a1e", "conversation": "20261110-104500-a1b2c3"}`. Events follow (section 6).
Errors: `NOT_READY`, `BUSY`, `INVALID_PARAMS`, `UNKNOWN_ACCOUNT`, `NO_DRIVER`.

### 4.3 `turn.cancel` (request)

Params `{"turn": "t-5f0c9a1e"}`. Result `{"cancelled": true}`; for a turn that had already ended
`{"cancelled": false, "status": "ok" | "cancelled" | "error"}` (a late Stop is not an error). Error:
`UNKNOWN_TURN`. Idempotent. The turn's `done` event follows (rule 6).

### 4.4 `command.run` (request)

```json
{"jsonrpc":"2.0","id":9,"method":"command.run","params":{"name":"tool","args":{"tool":"sch_apply","arguments":{"ops":[{"op":"set_field","ref":"R1","field":"Value","value":"4k7"}]}},"focus":null,"conversation":null}}
```

Runs a no-model command (`hello.commands`) as a turn: same event stream, same cancel, same `done`.
`conversation` null means the command is not recorded in any conversation. Result `{"turn": "..."}` (plus
`conversation` when one was given). Errors: `UNKNOWN_COMMAND`, `BUSY`, `INVALID_PARAMS`. Bad command
arguments are reported inside the turn as an `error` event with `code: "BAD_ARGUMENT"`.

### 4.5 Notifications from the app

| Method | Params | Meaning |
|---|---|---|
| `focus.changed` | focus object (4.7), or `{}` for "nothing selected" | The selection or pointed place changed. The agent keeps the latest as the default focus of the next turn. Send on change, coalesced to at most a few per second. |
| `doc.changed` | `{"document": "schematic" \| "board", "rev": integer}` | The document's revision moved (user edit, undo, redo, apply, reload). The agent drops a checkout made at another revision. Must be sent after every successful `host.apply` and `host.reload` too. |
| `proposal.resolved` | `{"proposal_id": string, "outcome": "kept" \| "undone" \| "stale"}` | The review of a change ended. `kept`: the user accepted it (or left review). `undone`: the user undid it. `stale`: it was dropped because the document changed under it. The agent records the outcome and, unless `kept`, tells the model in the next turn. |

### 4.6 `ping`, `shutdown` (requests)

`ping` -> `{}` at any time (also before `hello`). `shutdown` -> `{}`, then the agent ends open turns as in
rule 9 and exits 0.

### 4.7 The focus object

What the user is pointing at, built by the app from the live selection and connectivity. It extends the
focus object of [FOCUS.md](../FOCUS.md) (same meaning of `refs`, `pins`, `nets`, `point_mm`, `box_mm`).

```json
{"document":"schematic","sheet_path":"/power","mode":"selection",
 "refs":["U3","C4"],"pins":[{"ref":"U3","number":"12","name":"PA5"}],"nets":["/SWDIO","GND"],
 "items":[{"uuid":"6f0c0f1e-5a3b-4a53-9d0e-2f3f1c1f7a10","type":"symbol"},{"uuid":"0b7c5b06-11b5-45a5-8a53-1f5e7e0f3c22","type":"wire"}],
 "source":"kicad","seq":41,"rev":12}
```

| Field | Type | |
|---|---|---|
| `document` | `"schematic"` \| `"board"` | Required. |
| `sheet_path` | string | Schematic: human-readable sheet path, `/` for the root, `/power/buck` below it. |
| `mode` | `"selection"` \| `"point"` \| `"box"` | Required. `selection`: items the user selected. `point`: the cursor position captured at invoke time. `box`: a dragged region. |
| `point_mm` | `[x, y]` | For `point`. |
| `box_mm` | `[x0, y0, x1, y1]` | For `box`; also allowed with `selection` (its bounding box). |
| `refs` | array of strings | Reference designators of the parts involved, natural order. Empty with a point or box means "that area". |
| `pins` | array of `{"ref", "number", "name"}` | Pins / pads pointed at directly. |
| `nets` | array of strings | Net names of those pins and of selected wires / tracks. |
| `items` | array of `{"uuid", "type"}` | Every selected item: `symbol`, `wire`, `label`, `junction`, `sheet`, `footprint`, `pad`, `track`, `via`, `zone`, ... |
| `source` | `"kicad"` | Constant for focus built by the app. |
| `seq` | integer | Increases with every focus change in this app run. |
| `rev` | integer | The document revision the focus was read from. |

The agent turns it into one line at the top of the user turn (`agent/prompt.py`, `focus_line`), about
60-150 tokens; it is never part of the cached prompt prefix.

### 4.8 `accounts.*` (requests)

An account record: `{"id", "kind", "label", "base_url"?, "command"?, "default_model"?, "has_secret", "created"}`.
Secrets go from the app's password field straight to the agent in `accounts.add`, from there into the OS
credential store (`link/store.py`), and never come back: no result, event, log line or file contains one.

| Method | Params | Result |
|---|---|---|
| `accounts.list` | `{}` | `{"accounts": [record], "drivers": [driver]}` |
| `accounts.add` | `{"kind", "label"?, "base_url"?, "secret"?, "command"?, "default_model"?}` | `{"account": record}`. Errors: `UNKNOWN_DRIVER`, `BAD_ARGUMENT`, `STORE_UNAVAILABLE`. A `base_url` with a user name or key in it (`https://user:key@host`) is refused with `BAD_ARGUMENT`: the record is stored and shown in the clear, only `secret` goes to the credential store. |
| `accounts.remove` | `{"id"}` | `{}`. Error: `UNKNOWN_ACCOUNT`. |
| `accounts.test` | `{"id"}` | `{"ok": bool, "message": string, "models"?: [{"id","label","quick"}], "kind"?, "code"?}`. May take seconds (one cheap request to the provider). |

### 4.9 `conversations.*` (requests)

Conversations live in `<project>/.copper/conversations/<id>.jsonl` (append-only JSON lines; record kinds in
`agent/sessions.py`). Standalone schematic and board editors each run their own agent and share these files.

| Method | Params | Result |
|---|---|---|
| `conversations.list` | `{}` | `{"conversations": [{"id","title","created","updated","turns"}]}`, newest first |
| `conversations.load` | `{"conversation"}` | `{"conversation", "records": [record]}` for re-rendering a transcript. Error: `UNKNOWN_CONVERSATION`. |
| `conversations.delete` | `{"conversation"}` | `{}` |

### 4.10 `usage.get` (request)

Every finished turn appends one line to `<project>/.copper/usage.jsonl` (`$COPPER_HOME/agent/usage.jsonl`
without a project), written before the turn's `done` goes out, so a `usage.get` sent on `done` already
counts it. A line holds numbers only, never text or secrets (`agent/usage.py`):

```json
{"ts":1794268800,"turn":"t-5f0c9a1e","kind":"model","status":"ok","ms":4120,"tools":2,"in":1200,"out":80,"cached":1000,
 "conversation":"20261110-013455-652206","mode":"chat","account":"key-3fa2c1","account_kind":"key","model":"m-large","cost":0.0125}
```

`kind` is `"model"` or `"command"` (a `command.run`, or text typed on the `none` account; always zero
tokens). `in` / `out` / `cached` are the turn's final counts as its driver reported them; `cost` is present
only when the driver reported one.

Params `{"conversation"?: string, "limit"?: 0..1000}`. Result:

| Field | |
|---|---|
| `project` | `{"turns","model_turns","command_turns","in","out","cached","cost","tools","cost_complete"}`. Token and cost counters cover model turns; `cost_complete` is false when a model turn reported tokens but no cost. |
| `today` | The same counters for model turns of the local day. |
| `by_account`, `by_model` | `{id: counters}`. |
| `by_day` | `{"YYYY-MM-DD": counters}`, the 14 most recent days with model turns. |
| `conversation` | Counters of that conversation (only when asked for). |
| `last` | The most recent model turn's line, or null. |
| `line` | One plain sentence for the usage footer. |
| `turns` | The last `limit` lines (`[]` without `limit`). |
| `file` | Path of the ledger. |

The per-turn numbers are also on the wire as the `usage` event and in `done.usage`.

## 5. Agent -> app: `host.*` requests

All are answered on the UI thread. Timeouts are how long the agent waits before giving up (`TIMEOUT`) and
sending `host.cancelled`.

| Method | Timeout | Purpose |
|---|---|---|
| `host.checkout` | 60 s | Write the live document to a shadow project folder. |
| `host.apply` | 120 s | Land a patch as one undoable commit and enter review. |
| `host.reveal` | 15 s | Select, centre, highlight. |
| `host.checkpoint` | 60 s | Take a restorable project snapshot. |
| `host.save` | 120 s | Save a document to its file. |
| `host.reload` | 120 s | Re-read a document from its file without prompting. |
| `host.verify` | 300 s | Run ERC / DRC on the live document. |
| `host.ask_permission` | 900 s | Ask the user. |

`document` is `"schematic"` or `"board"` everywhere. `rev` is a non-negative integer the app increments on
every change of that document (edit, undo, redo, apply, reload); it is compared for equality only. The app
clears `<project>/.copper/shadow/` when it opens a project.

### 5.1 `host.checkout`

Params `{"document": "schematic"}`. The app serialises the **live** document, unsaved edits included, into
`<project>/.copper/shadow/<rev>/` and answers:

```json
{"shadow_dir":"D:/boards/sensor/.copper/shadow/12","rev":12,"document":"schematic","project":"sensor","dirty":true,
 "sheets":[{"sheet_path":"/","file":"sensor.kicad_sch"},{"sheet_path":"/power","file":"power.kicad_sch"}]}
```

The shadow folder must be a project the engine can open on its own:

* `<project>.kicad_pro` (copied), every sheet file of the hierarchy at its project-relative path (written
  from memory with the normal file writer), `sym-lib-table` and `fp-lib-table` (copied), and every
  project-local library those tables reach through `${KIPRJMOD}` (copied; 3D models may be skipped);
* for `"board"`: additionally the `.kicad_pcb` written from memory;
* no lock files.

`sheets` lists each sheet FILE once, root first, with a human-readable `sheet_path` of one of its instances
and its path relative to the project folder (forward slashes). `dirty` says whether the live document
differs from its saved file. Errors: `NO_DOCUMENT`, `BUSY`, `INTERNAL`.

Where the shadow lives is the app's choice; the agent treats whatever `shadow_dir` names as a shadow
(`host/route.note_shadow`). `<project>/.copper/shadow/<rev>/` is the default shape, but:

* **Path length.** On Windows without long-path support a shadow under a deep project folder crosses 260
  characters (`<project>/.copper/shadow/<rev>/libs/<lib>.pretty/<a 100 character footprint name>.kicad_mod`).
  kicad-cli then cannot read those libraries and file copies fail. An app should use long-path APIs or a short
  location (for example `%LOCALAPPDATA%/copper/shadow/<key>/<rev>/`).
* **Two apps, one project.** A standalone schematic editor and a standalone board editor each run their own
  agent and count their own `rev`. `<rev>` alone is not unique between them: put the process id (or any token of
  the app instance) in the path.
* **Cleanup is the app's.** The agent never deletes a shadow. The app removes shadows of older revisions (keep
  the one it handed out last) and all of its shadows on exit.

The agent edits the shadow with the engine in live mode (no kicad-cli re-save, no file ERC: the app is the
canonicaliser and the verifier; a shadow has no editor lock and no host of its own) and never asks the app
to read it back. The sheets must be in the app's CURRENT file format: `add` text is parsed at that version (7.5). It keeps one checkout per document and reuses it until `doc.changed` reports another `rev`.

### 5.2 `host.apply`

```json
{"jsonrpc":"2.0","id":"a2","method":"host.apply","params":{"base_rev":12,"document":"schematic",
 "patches":[{"sheet_path":"/","file":"sensor.kicad_sch","remove":["635743e0-96b3-4858-b080-c4407ad3314a"],"add":"(symbol\n\t(lib_id \"Device:R\")\n\t(at 66.04 58.42 0)\n\t...\n\t(uuid \"635743e0-96b3-4858-b080-c4407ad3314a\")\n\t...)"}],
 "lib_symbols":[],"summary":"R1.Value = '4k7'","expect":{"netlist_hash":"sha256:5b0e..."}}}
```

| Param | Type | |
|---|---|---|
| `base_rev` | integer | The `rev` of the checkout the patch was computed from. |
| `document` | string | `"schematic"` in v1 (board patches are a later version; see 5.9). |
| `patches` | array | One entry per changed sheet file: `{"sheet_path", "file", "remove": [uuid], "add": "<s-expr>", "libs"?: [name]}` (section 7). |
| `lib_symbols` | array of strings | Library symbol definitions that are new or changed, each one complete `(symbol "Lib:Name" ...)`, once per name; a patch's `libs` says which sheet gets which (7.3). |
| `files` | array, optional | `[{"path": "<project-relative>", "shadow_path": "<absolute, inside the shadow>"}]`: non-document files the edit created or changed (a symbol or footprint the engine imported into a project library, the lib tables). The app copies them into the project as part of the same apply and reloads its library tables. |
| `summary` | string | One line for the review bar and the undo menu ("Copper: R1.Value = '4k7'"). |
| `expect` | object, optional | `{"netlist_hash": "sha256:..."}`: the netlist the edited shadow has (section 7.4). |

What the app does, in this order, on the UI thread:

1. `base_rev` != current `rev` -> `STALE_BASE`. Editor not idle -> `BUSY`. "Not idle" means the USER's
   modal states (a move in progress, a dialog). Review mode of an earlier proposal of the same agent is NOT
   busy: a turn regularly applies twice (a change, then the repair of what ERC found), and the second apply
   must be accepted on top of the first. The review bar then covers both; each stays its own undo step and Undo
   reverts them newest first. An app that answered `BUSY` here would make every multi-step turn fail.
2. Parse every `add` and every `lib_symbols` entry (the clipboard parser, section 7.5). Check every `remove` uuid exists on
   its sheet and no added uuid would be duplicated. Any failure -> `PARSE` / `UNKNOWN_ITEM` /
   `DUPLICATE_ITEM` / `NO_SHEET`, **nothing applied**.
3. One commit across all sheets: remove, add (a uuid in both `remove` and `add` of a sheet is a modification:
   the item keeps its uuid), add library symbols, copy `files`. Push it. One apply is one undo step.
4. Increment `rev`. Tint the changed items, enter review mode, and compute the review facts: counts, the ERC
   findings that are new compared with before the commit, and whether the live netlist hash equals
   `expect.netlist_hash`.
5. Answer, then send `doc.changed` with the new `rev`.

Result:

```json
{"proposal_id":"p17","state":"review","rev":13,"counts":{"added":0,"changed":1,"removed":0},"erc_new":[],"netlist_ok":true}
```

| Field | |
|---|---|
| `proposal_id` | The app's id for this change; used in `proposal.resolved`. |
| `state` | `"review"`: the change is in the document and the Keep / Undo bar is up. `"applied"`: applied without review (a setting). |
| `rev` | The document's revision after the commit. Required: the agent continues from it without a new checkout. |
| `counts` | `{"added", "changed", "removed"}` in items. |
| `erc_new` | Findings (section 6.5 shape) that did not exist before the commit. |
| `netlist_ok` | `true` / `false` when `expect.netlist_hash` was given and compared, else `null`. `false` turns the review bar amber and pre-selects Undo. |

### 5.3 `host.reveal`

Params: any of `{"refs": ["U3"], "uuids": ["..."], "net": "/SWDIO", "box": [x0, y0, x1, y1], "document": "schematic"}`.
Selects and brightens those items, pans only if they are off-screen. Result `{}`. Unknown refs are ignored.

### 5.4 `host.checkpoint`

Params `{"label": "Route USB pair", "document": "board"}`. Takes a restorable snapshot of the project files
(the app's local history), named "Before Copper: <label>". Result `{"id": "<opaque>"}`.

### 5.5 `host.save` / 5.6 `host.reload`

Params `{"document"}`. `host.save` writes the live document to its file (no dialog). `host.reload` re-reads it
from the file without prompting, keeping the view; the undo history of that document is lost. Result
`{"rev": integer}` for both (`host.reload` always increments `rev` and is followed by `doc.changed`).
Errors: `NO_DOCUMENT`, `BUSY`.

### 5.7 `host.verify`

Params `{"document", "baseline": string?}`. Runs ERC (schematic) or DRC (board) on the live document.

```json
{"document":"schematic","counts":{"errors":1,"warnings":3},"baseline":"b9",
 "findings":[{"severity":"error","type":"pin_not_connected","message":"Pin not connected","at":[66.04,54.61],"refs":["R1"],"pins":["R1.1"]}],
 "new":[]}
```

`baseline` in the result is an opaque token for "the findings as of now"; passing it back later makes the
app also return `new` (findings absent at the baseline).

### 5.8 `host.ask_permission`

Params `{"kind": string, "detail": object}`. Kinds in v1: `"file_job"` (`detail`: `{"tool", "document",
"label", "note"}`; a board job that runs on the saved file and ends with a reload). Result
`{"granted": bool, "remember": bool}`; with `remember` the app answers the same kind itself next time.

A second kind, `"save"` (`detail`: `{"documents": ["schematic", "board"], "label", "note"}`), is asked by
commands that read the SAVED files without changing them (`/export`): granted -> the agent calls `host.save`
for each listed document and then runs; not granted -> it runs on the files as last saved and says so. The
app should answer `{"granted": true}` by itself when none of the listed documents has unsaved changes. A host
that does not know the kind may answer `UNSUPPORTED`; the command then also uses the files as last saved.

### 5.9 Board changes in v1: the file-job sequence

Board tools that write (`pcb_*`, `board_build`) do not produce patches yet. The agent runs them as:

1. `host.ask_permission` `{"kind": "file_job", ...}`; not granted -> the tool fails with `DENIED`.
2. `host.save` for `"schematic"` (skipped on `NO_DOCUMENT`) and `"board"`.
3. `host.checkpoint`.
4. The engine tool on the real project files.
5. `host.reload` `"board"` (always, also when step 4 failed).

The tool result carries the checkpoint id; undo is "Restore checkpoint". The app shows the `tool_progress`
events of step 4 as a progress row with Stop.

The board commands (`/route`, `/pour`; `agent/jobs.py: board_job`) run the same sequence with three
differences that make Stop safe. `LiveSession._board`, which serves a model's board tool calls, does not have
them yet.

* Step 4 runs in a child process. On `turn.cancel`, on stdin EOF and on any other end of the turn the whole
  process tree is killed (on Windows a kernel job object, which also ends it if the agent itself is killed).
* Before step 4 the agent copies the board-side files (`.kicad_pcb`, `.kicad_pro`, `.kicad_dru`) to
  `<project>/.copper/jobs/<id>/`. A stopped job has them put back byte for byte and sends no `host.reload`:
  the app saved in step 2, so the file again equals what the editor shows. A job whose agent died is rolled
  back the same way before the next board job (`jobs.recover`).
* Step 5 is sent only when a board file actually changed (a dry run or a refusal keeps the editor's undo
  history). `host.save` and `host.reload` answering `NO_DOCUMENT` are skipped: the board is then not open in
  the app and the file is the document. A dry run skips step 3.

`/undo-checkpoint` restores the newest such copy: `host.ask_permission` (`file_job`, `tool:
"undo-checkpoint"`), `host.save` `"board"`, a copy of the current files (so running it again is a redo),
the files put back, `host.reload` `"board"`. It never touches schematic files; their undo is the editor's
own, because every schematic change is a `host.apply`.

### 5.10 `host.cancelled` (notification, agent -> app)

`{"id": "a7", "reason": "timeout" | "turn_cancelled"}`: the agent no longer waits for its request `a7`. The
app dismisses any dialog it opened for it. If the request was a `host.apply` that the app has already
executed, the change stays in review as usual.

## 6. Events (agent -> app notifications)

```json
{"jsonrpc":"2.0","method":"event","params":{"turn":"t-5f0c9a1e","seq":3,"type":"tool_end","call":"c1","ok":true,"summary":"1 change(s) written","ms":4120}}
```

Every event has `turn`, `seq` and `type`. Ordering rules are in section 2 (rules 3-5) and are implemented as
`agent/events.py: check_stream`.

| `type` | Fields | Meaning |
|---|---|---|
| `text_delta` | `text`; `channel`? = `"reasoning"` (absent = answer) | A piece of the model's (or a command's) text. Concatenate in `seq` order. |
| `tool_start` | `call`, `name`; `title`?, `args`? (omitted above 4 kB: then `args_bytes`, `args_keys`) | A tool call began. `call` is unique within the turn. |
| `tool_progress` | `call`, `fraction` (0..1); `message`? | Progress of an open call. |
| `tool_end` | `call`, `ok`, `ms`; `summary`?, `error`? = `{"code","message","fix"?}` | The call ended. A failed tool does not end the turn: the model reads the failure and continues. |
| `proposal` | `proposal_id`, `document`, `state`, `counts`; `summary`, `erc_new`, `netlist_ok`, `refs`, `region`, `rev`, `skipped`? | A change landed through `host.apply` (the app already shows the review bar; this is for the transcript card). |
| `findings` | `document`, `items` (6.5); `source`? (`"erc"`, `"drc"`, `"engine"`), `counts`? | Check results to list; each row can be clicked. |
| `reveal` | any of `refs`, `uuids`, `net`, `box`; `document`? | Advisory: the transcript now talks about these; the app may highlight them. No answer. |
| `usage` | `in`, `out`, `cached` (tokens, cumulative for the turn); `cost`?, `model`? | For the usage footer. The last one wins. |
| `error` | `kind`, `message`; `code`?, `retryable`?, `fix`?, `retry_after_s`? | The turn failed. Always followed by `done`. At most one per turn. |
| `done` | `status` = `"ok"` \| `"cancelled"` \| `"error"`; `usage`? | The turn is over. Always the last event. |

### 6.1 `error.kind`

| `kind` | Means | The pane offers |
|---|---|---|
| `auth` | No, wrong or expired credentials; no account connected. | "Connect an account" |
| `rate` | Rate limit, quota or run budget exhausted (`retry_after_s`). | "Try again in ..." |
| `network` | Unreachable, timeout, connection reset, stream cut short (`code: "TRUNCATED"`), silent stream (`code: "WATCHDOG"`). | "Retry" |
| `engine` | A failure inside Copper outside a tool result (`INTERNAL`, `BAD_ARGUMENT`, `UNKNOWN_COMMAND`, `NO_PROJECT`, `WATCHDOG_TOOL`, `TURN_TIMEOUT`). | "Report" / the message |
| `protocol` | A peer spoke something unexpected or went away (`HOST_GONE`, `TIMEOUT`, `AGENT_EXITED`). | "Restart" |

### 6.5 Finding

`{"severity": "error" | "warning", "type": string, "message": string, "at": [x, y]?, "refs": [string]?, "pins": ["REF.PIN"]?, "fix": string?}`.
The engine's own findings (`review/verify.py`) additionally carry `items`.

## 7. Patches (`sch/patch.py`)

### 7.1 Items

A sheet file is `(kicad_sch <header> (lib_symbols ...) <items> (sheet_instances ...) ...)`. An **item** is
a top-level node of one of these kinds that carries a uuid: `symbol`, `sheet`, `junction`, `no_connect`,
`bus_entry`, `wire`, `bus`, `polyline`, `arc`, `circle`, `rectangle`, `bezier`, `rule_area`, `text`, `label`,
`global_label`, `hierarchical_label`, `directive_label`, `netclass_flag`, `text_box`, `table`, `image`, `group`.
The uuid is the node's direct `(uuid X)` child; for `rule_area` it is the uuid of its `polyline` (that is the
object's uuid in the app). The sheet's own `(uuid ...)` is a header node, not an item. Items are identified by
uuid only. Each unit of a multi-unit symbol is its own item with its own uuid. A sheet symbol is one item; its
pins are part of it.

Two versions of an item are the same when their trees are equal after reading numbers as numbers (`1.27` =
`1.2700`). White space, quoting and the order of items in the file are not differences.

### 7.2 A sheet patch

`{"sheet_path": "/", "file": "sensor.kicad_sch", "remove": ["<uuid>", ...], "add": "<s-expression text>", "libs": ["Device:LED"]}`

* `file` identifies the sheet (one screen in the app, even when the file is instantiated several times);
  `sheet_path` is one of its instance paths, for display.
* `remove`: uuids of items that are gone or changed, in the order they had in the file.
* `add`: the complete text of the items that are new or changed, one after another, in KiCad's current file
  syntax (format 20260306 for KiCad 10.0). No enclosing node, no `lib_symbols`, no header nodes. May be `""`.
* A changed item is in both: same uuid in `remove` and inside `add`. The app must treat that as a
  modification of the same object (same uuid afterwards), so a symbol keeps its link to its footprint.
* `libs` (optional): names of the definitions in the top-level `lib_symbols` that THIS sheet's symbol cache
  must receive. A patch can consist of `libs` alone (`remove` empty, `add` empty). When `libs` is absent the
  app merges every sent definition that a symbol on the resulting sheet uses.
* Instance data is carried verbatim, with FULL paths as in a file:
  `(instances (project "p" (path "/<root uuid>/<sheet uuid>" (reference "R201") (unit 1)) (path ... )))`.
  An item on a sheet that is used twice lists one path per use, each with its own reference. These are not the
  selection-relative paths eeschema writes for its own clipboard (7.5).
* Not carried, by design: header nodes (`paper`, `title_block`, the sheet uuid, `bus_alias`), the trailing
  `sheet_instances` / `symbol_instances` / `embedded_fonts` / `embedded_files`, top-level items without a uuid
  (files older than KiCad 7), items whose uuid occurs twice in the sheet. The engine names each such difference
  in the proposal's `skipped` list; the rest of the change still applies.
* A sheet FILE the engine created is not a patch: it is sent in `files` (5.2) and referenced by the `sheet`
  item added to its parent. No engine tool creates sheets today.

### 7.3 Library symbols and side files

`lib_symbols` holds each `(symbol "Name" ...)` definition that is new or changed in the `lib_symbols` section
of a patched sheet and is used by an item there, once per name. `Name` is what the symbol items look up:
their `lib_name` when they have one, else their `lib_id`. Definitions a sheet no longer uses are not removed
by a patch (the app prunes its cache when it saves). If two sheets get different new definitions under one name
in a single edit, one is sent and the other is reported in `skipped`. `files` (5.2) carries what the engine
wrote next to the sheets: the project's library tables and libraries when a part was imported.

### 7.4 `netlist_hash`

Defined on a netlist as a set of nets, each a set of pins `REF.PIN`:

1. drop pins whose reference starts with `#` (power symbols, flags);
2. drop nets left without pins;
3. per net: sort its `REF.PIN` strings (byte order) and join with `,`;
4. sort those lines, join with `\n`, encode as UTF-8, SHA-256;
5. the hash is `"sha256:" + lowercase hex`.

Net names are not hashed (generated names of unnamed nets are not stable across writers). Multi-unit symbols
contribute the pins of all their units under the one reference, by pin NUMBER. The engine computes it from
`kicad-cli sch export netlist` of the edited shadow; the app computes it from its connection graph after the
commit. Equal hashes mean the same pins are connected to each other. `patch.netlist_lines()` returns the lines
of step 4 for debugging a mismatch.

A mismatch does not always mean a wrong patch. Measured with kicad-cli 10.0.6: when an edit leaves two
collinear wires that partly overlap (an end of each inside the other, no junction), KiCad's netlist for the
SAME set of items depends on their order in the file (`tests/test_patch_fixtures.py`, `ORDER_SENSITIVE`). The
app's connection graph may therefore disagree with the shadow's netlist for such a drawing. `netlist_ok:
false` (amber bar, Undo preselected) is the right outcome there: the drawing is ambiguous.

### 7.5 `add` and KiCad's paste parser

What was read in the KiCad 10.0 sources (`upstream/10.0`, `eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr.cpp`,
`sch_io_kicad_sexpr_parser.cpp`, `eeschema/tools/sch_editor_control.cpp`):

* **Copy** (`SCH_IO_KICAD_SEXPR::Format( SCH_SELECTION*, SCH_SHEET_PATH*, SCHEMATIC&, OUTPUTFORMATTER*, bool
  aForClipboard )`) writes, with no enclosing node: one `(lib_symbols (symbol ...) ...)` holding the cached
  definition of every selected symbol (omitted when no symbol is selected), then each selected item with the
  same writer functions the file writer uses. `patch.clipboard_text(add, lib_symbols)` produces this shape.
* **Paste** reads the clipboard text with `SCH_IO_KICAD_SEXPR::LoadContent( LINE_READER&, SCH_SHEET*, int
  aFileVersion = SEXPR_SCHEMATIC_FILE_VERSION )`, which is `ParseSchematic( sheet, aIsCopyableOnly = true,
  aFileVersion )` on a temporary sheet with a temporary screen. In that mode the parser reads no `(kicad_sch`
  header, takes `aFileVersion` as the format version of the text, loops over top-level lists until end of
  input, and has a case for: `lib_symbols`, `symbol`, `sheet`, `junction`, `no_connect`, `bus_entry`,
  `polyline`, `bus`, `wire`, `arc`, `circle`, `rectangle`, `bezier`, `rule_area`, `text`, `label`,
  `global_label`, `hierarchical_label`, `directive_label`, `netclass_flag`, `text_box`, `table`, `image`,
  `group`, and also `generator`, `generator_version`, `uuid`, `sheet_instances`, `symbol_instances`. It throws
  on `paper`, `page`, `title_block` and `bus_alias`, and `embedded_fonts` / `embedded_files` act on the
  whole schematic (or throw when the temporary screen has none), so they must not be in `add`. At the end it calls `screen->UpdateLocalLibSymbolLinks()`, which binds
  each parsed symbol to a definition in the TEMPORARY screen's `lib_symbols`.
* Because the text is parsed at the current format version, `add` must be in current syntax. It is: the engine
  writes KiCad 10 syntax, and `host.checkout` gives it sheets the app serialised (current format). A checkout
  made by copying an old file from disk would put old-syntax items into `add`; fakehost-style hosts must
  upgrade first.

What the adapter should do with a patch (recommendation; nothing here has been compiled):

1. Build the text `(lib_symbols <definitions>)` + `add`. For `<definitions>` use the sent `lib_symbols` and,
   for every other name the items use, the destination screen's own cached definition. Without a definition
   in the temporary screen a parsed symbol has no library symbol and no pins.
2. `LoadContent` into a temporary sheet whose screen was created with the frame's `SCHEMATIC` as parent (as
   `Paste` does).
3. Take the items off the temporary screen and commit them WITHOUT the rest of `Paste`: `Paste` replaces
   uuids with new ones (`const_cast<KIID&>( item->m_Uuid ) = KIID()`), rewrites symbol instances relative to
   the paste path (`updatePastedSymbol`, which looks instances up by a path ending in the clipboard path) and
   may re-annotate. A patch needs the opposite: uuids kept, instances kept as sent (they are full paths that
   already name every use of the sheet).
4. For a uuid that is in both `remove` and `add`: either `Modify` the existing object and copy the parsed
   state into it, or remove + add in the same commit. Which of the two keeps undo, selection and the board
   link intact is the first thing to test in Phase C.

Checked here without the app: for 36 fixture edits, in both writer modes, every `add` passes
`patch.fragment_problems` (complete lists only, allowed kinds only, uuids present and unique) and a minimal
sheet wrapped around it (`patch.wrap_sheet`) is read and re-saved by `kicad-cli sch upgrade` with every item
still present under its uuid and kind. kicad-cli uses the same item parser functions; it does not run the
`aIsCopyableOnly` branch itself.

Not proven, needs the app or a hand test:

* the `aIsCopyableOnly` path end to end (paste the files written by `COPPER_PATCH_SAMPLES=<dir>
  python tests/test_patch_fixtures.py` into a stock KiCad 10 schematic: Phase A test 3);
* that a symbol parsed this way and committed with its sent `instances` shows the right reference on every use
  of a shared sheet, and the right unit for multi-unit symbols;
* `sheet` items (the adapter must load or create the sheet's screen; `Paste` has extra code for this),
  `group` items (members are uuids that must already exist), `table`, `image`, `rule_area`: the engine does
  not produce these today, the patch format merely does not exclude them;
* same-uuid remove + add in one `SCH_COMMIT`.

### 7.6 The reference applier

`patch.apply_sheet(text, patch, lib_symbols)` and `patch.apply_project(texts, patches, lib_symbols)` apply a
`host.apply` to sheet FILE text. `tests/fakehost.py` uses them, and the app's result must be equivalent:

* all or nothing across all patches: `PARSE` (bad s-expression, a top-level node that is not an item kind, an
  item without uuid, a `libs` name without a definition, a uuid twice in `remove`), `UNKNOWN_ITEM` (a `remove`
  uuid not on that sheet), `DUPLICATE_ITEM` (an added uuid already on the sheet and not removed, or twice in
  `add`), `NO_SHEET` (no such file);
* a uuid in both lists is replaced where it was; other added items are appended after the existing items;
* definitions named in `libs` replace the cache entry of that name or are appended;
* everything else in the file (header, other items, unused definitions) is left as it was.

"Equivalent" is defined by the acceptance test: the patched project has the same kicad-cli netlist
(components with value, footprint and lib id; nets by name with their pins) as the project the engine wrote
directly, and the same items and used definitions by uuid / name.

## 8. Seams for implementers

Three places are designed to be filled in without touching the spine.

### 8.1 Patch (`src/copper_kicad/sch/patch.py`)

```python
ITEM_HEADS; FILE_VERSION
class PatchError(Exception):            # .code: PARSE | UNKNOWN_ITEM | DUPLICATE_ITEM | NO_SHEET, .message
def item_uuid(node) -> str | None
def diff_sheet(before: str, after: str) -> dict
    # {'remove': [uuid], 'add': str, 'lib_symbols': [str], 'lib_names': [str], 'lib_unused': [str],
    #  'added': [uuid], 'changed': [uuid], 'removed': [uuid], 'kinds': {kind: n},
    #  'refs': [str], 'region': [x0, y0, x1, y1] | None, 'skipped': [str]}
def diff_project(before: dict, after: dict, sheets: list[dict] | None = None) -> dict
    # before/after: {project-relative file: text}; sheets: host.checkout's list
    # {'patches': [{'sheet_path','file','remove','add','libs'?}], 'lib_symbols': [str],
    #  'counts': {'added','changed','removed'}, 'refs': [str], 'region': [...] | None, 'skipped': [str],
    #  'new_sheets': [file], 'empty': bool}
def parse_add(add: str) -> list                         # item nodes; raises PatchError('PARSE')
def fragment_problems(add: str, lib_names=None) -> list[str]     # [] = a clean paste-parser fragment
def clipboard_text(add: str, lib_symbols=()) -> str     # (lib_symbols ...) + items, as eeschema's Copy writes it
def wrap_sheet(add: str, lib_symbols=(), ...) -> str    # a minimal complete sheet around a fragment
def apply_sheet(text: str, patch: dict, lib_symbols=()) -> str           # reference applier, one sheet
def apply_project(texts: dict, patches, lib_symbols=()) -> dict          # reference applier, one host.apply
def patch_counts(patches) -> dict                       # {'added','changed','removed'} as a host counts them
def netlist_lines(netlist: dict) -> list[str];  def netlist_hash(netlist: dict) -> str;  def expect(netlist) -> dict
```

Callers: `host/live.py: LiveSession._schematic` (diff, expect), `tests/fakehost.py: h_apply` (apply).
Tests: `tests/test_patch_fixtures.py` (the Phase A acceptance test, 36 edits), `tests/test_patch_live.py`.

Live mode (`sch/model.py`): `with model.live_mode(): ...` or `COPPER_LIVE=1`. Inside, `Schematic.save()` does
not run `kicad-cli sch upgrade` and the schematic tools do not run ERC; `model.live()` tells. `LiveSession`
runs schematic tools this way unless `COPPER_LIVE_CANON=1`.

### 8.2 Model drivers (`src/copper_kicad/agent/drivers/`)

One module per driver; it is imported automatically and registers itself.

```python
from copper_kicad.agent.drivers import register
from copper_kicad.agent.drivers.base import Driver, DriverError, Cancelled, TurnRequest, stream_lines

@register
class MyDriver(Driver):
    kind = "my-kind"            # the account kind; lower-case [a-z0-9-]
    label = "..."               # plain text for the picker
    needs = ("secret", "base_url")       # of 'secret', 'base_url', 'command'
    def available(self) -> tuple[bool, str]: ...        # cheap, no network
    def models(self, account: dict) -> list[dict]: ...  # [{'id','label','quick': bool}]
    def test(self, account: dict) -> dict: ...          # {'ok': bool, 'message': str}
    def run(self, turn) -> None: ...                    # blocking, on the turn's thread
```

What `run` gets (`agent/turn.py`):

```python
turn.request            # TurnRequest(conversation, text, focus, mode, account, model, budget_tokens)
turn.focus, turn.project_dir, turn.host, turn.sessions, turn.cancelled
turn.check_cancel()                              # raises Cancelled
turn.touch()                                     # feeds the watchdog without an event
turn.text(s, channel=None)                       # text_delta
turn.call_tool(name, args, summary=None) -> {'ok','text','data','images','error','proposal','route'}
turn.tool_start(name, args=None, title=None) -> call;  turn.tool_progress(call, fraction, message=None)
turn.tool_end(call, ok=True, summary=None, error=None)
turn.findings(document, items, source=None, counts=None);  turn.reveal(refs=..., uuids=..., net=..., box=..., document=...)
turn.usage(in_tokens, out_tokens, cached=0, cost=None, model=None)
turn.history(limit=None) -> [{'role': 'user'|'assistant', 'text'}];  turn.notes() -> [str]
```

and from `agent/prompt.py`: `system_prefix(mode)`, `tool_catalogue(mode)` (`[{'name','description','input_schema'}]`),
`user_turn(text, focus, notes)`, `prefix_digest(mode)`; from `agent/accounts.py`: `secret(account_id)`.

The contract (enforced by the server, see `drivers/base.py`): return = `done(ok)`; `raise Cancelled` =
`done(cancelled)`; `raise DriverError(kind, message, code=..., retryable=..., fix=..., retry_after_s=...)` =
`error` + `done(error)`; any other exception = `error(engine, INTERNAL)`. A stream that ends without its
terminal marker must `raise DriverError.truncated()`. Blocking reads need timeouts. Secrets never appear in
events, logs or exception texts. Driver modules may name a vendor in their own file (they are that vendor's
adapter); the account `kind`, the protocol and everything else stay neutral, and `tests/test_neutral.py`
needs the `agent/drivers/` adapters added to its list of files that may carry product names.

A driver that spawns an agent CLI attaches exactly one MCP server to it: the tool gate's relay
(`agent/drivers/_gate.py`, `_bridge.py`). The relay has no engine; each `tools/call` is run inside the agent
through `turn.call_tool`, so a CLI-driven edit takes the same road as any other (tool events, checkout, patch,
`host.apply`, cancel). The shipped drivers, their account kinds, command lines and what is verified are in
`docs/internal/AGENT_DRIVERS.md`. (`turn.server.start_relay()` and section 9 remain for engines that run in another
process, such as an external MCP client.)

### 8.3 No-model commands (`src/copper_kicad/agent/commands/`)

One module per command group; imported automatically.

```python
from copper_kicad.agent.commands import command

@command("verify", "Run ERC / DRC and list what is wrong", usage="/verify [erc|drc|all]",
         args={"what": {"type": "string", "enum": ["erc", "drc", "all"]}}, positional=("what",),
         documents=("schematic", "board"), needs_project=True)
def verify(turn, args: dict) -> None:
    out = turn.call_tool("verify", {"what": args.get("what", "all")})
    ...
```

A command drives the same `turn` as a driver and follows the same error contract. It is reachable three
ways: `command.run`, `/name ...` typed with the `none` account, and from other drivers. Built in: `/help`,
`/focus`, `/tool` (any engine tool by name, routed like a model's tool call).

The commands that make the pane useful without an account (`commands/design.py`, `board.py`, `account.py`).
Each takes structured `args` from `command.run`, or the words typed after its name (argument `text`):

| Command | Engine work | How it reaches the document |
|---|---|---|
| `/verify [erc\|drc\|all]` | `host.verify` on the live document; without it the engine's `verify` in a child process, ERC on a shadow checkout, DRC on the saved board | Read-only. One `findings` event per check; findings on the selected parts come first. |
| `/tidy [pro\|labels] [force] [preview]` | `sch_tidy` on the sheet in focus | One `host.apply` (one undo step), like any schematic edit. |
| `/parts <query>` | `parts_search`; with no query, the one selected part is looked up first | Read-only. |
| `/export <kind>` | `export`, or `fab_export` for `fab`, in a child process | Asks to save first (`save` permission, 5.8); writes under `<project>/copper-out`. |
| `/focus [detail]` | none; with `detail`: `sch_read` / `pcb_read` for parts and nets, the engine's focus resolver for a point or box | Read-only; `reveal`. |
| `/route [net ...] [reroute] [freerouting] [preview]` | `pcb_route_guided` (or `pcb_route`); nets default to the nets in focus | Board file job (5.9), stoppable. |
| `/pour [net] [remove\|refill] [stitch]` | `pcb_pour`; net defaults to the one net in focus, else GND | Board file job (5.9). |
| `/undo-checkpoint [list\|<id>]` | none | Restores the board files from before the last board job and reloads (5.9); again = redo. |
| `/usage`, `/accounts` | none | Text only. |

`agent/jobs.py` is the second door to the engine, for work the user must be able to stop:
`run_tool(turn, name, args, project=None, title=None)` runs one engine tool in a child process as one tool
call of the turn, `board_job(turn, name, args, label)` wraps it in the file-job sequence. Both return the
outcome dict of `turn.call_tool` and raise `Cancelled` after killing the process tree.

### 8.4 Where engine tools run (`host/live.py`)

`turn.call_tool` is the only door to the engine. `LiveSession.call_tool` routes by tool:

| Route | Tools | What happens |
|---|---|---|
| `schematic` | `sch_*`, `library`, `parts_import`, `parts_source_bom` | Checkout once per revision; the tool runs with `project=<shadow_dir>`; writers are diffed and applied with `host.apply`. |
| `board` | `pcb_*` writers, `board_build` | The file-job sequence (5.9), as a job the user can stop: `turn.call_tool` hands these to `agent/jobs.board_job` (child process, board files put back on Stop), for a model's tool call exactly as for `/route`. |
| `disk`, live read | `project_summary`, `verify`, `design_review`, `render` | Read-only. With a schematic open they run on its shadow checkout, so they see the LIVE document (unsaved edits and changes still in review), not the file as last saved. The board in that shadow is the saved board file, copied in by the agent. `NO_DOCUMENT` / `UNSUPPORTED` from `host.checkout` -> the project files. |
| `disk` | every other tool with a `project` argument (`pcb_read`, `export`, `fab_export`, ...) | Runs on the project files as last saved. |
| `none` | tools without `project` | Runs as is. |
| `blocked` | `project_create`, `project_open`, `focus`, `checkpoint` | Refused with `NOT_IN_APP`: the app owns the project, pushes the focus and owns undo (a file checkpoint restore would overwrite files under the editor). |

The model never passes `project`; `prompt.tool_catalogue` removes that argument and leaves the blocked tools out.

Rules of this door that the conversation tests (`tests/test_agent_conversations.py`) pin down:

* **A failed tool cannot leak.** When a schematic tool fails after it wrote its shadow (a crash in a late
  stage), the agent drops that checkout. The next edit starts from a fresh checkout, so the failed edit cannot
  ride along in the next patch.
* **A stopped tool cannot leak.** An engine tool that could not be interrupted finishes in the background on
  its shadow; its `host.apply` is never sent (rule 8) and its checkout is dropped. A turn started meanwhile
  waits for it and then checks out again.
* **What the model reads after `host.apply`.** The tool result ends with one line
  `{"live":{"proposal_id","state","netlist_ok","erc_new":<count>,"findings":[...]?,"not_applied":[...]?,"warning"?}}`. `findings`
  are the app's `erc_new` entries cut to `severity`, `type`, `message`, `at`, `refs`, `pins`, `net`, `fix` (at
  most 8, then `findings_more`). Without them a model sees only a count and cannot repair what the app's ERC found.
  `not_applied` lists what the patch could not carry (7.2: a changed paper size or title block), so the model
  can say so instead of claiming it.
* **No engine snapshots in a shadow.** `project.checkpoint` returns `""` inside a shadow checkout: the app's undo
  is the checkpoint, and `.copper/snapshots` under a shadow only deepens paths.
* **A small edit stays small.** `sch_apply` on a sheet that already holds symbols runs with `layout: "keep"`
  unless the caller names a layout. The engine's automatic re-draw of a sheet it arranged itself is meant for
  file workflows; through the editor it turned "add one capacitor" into 8 added, 37 changed and 2 removed items.
  An empty sheet is still laid out, and `/tidy` re-draws on request.
* **A tool cannot end the turn's thread.** A tool that raises `SystemExit` is one failed tool call.
* **The focus line is inert.** Names in a focus come from the design file. `prompt.focus_line` keeps every
  value on the one `[focus]` line (control characters and line breaks become spaces, each value is cut at 80
  characters, values of the wrong type are dropped), so a reference or net name cannot start a `[note]` line.

## 9. Engines outside the agent (loopback relay and rendezvous)

An agent CLI started by a driver runs Copper's MCP server as its own child. That engine reaches the live
host through the agent:

```
app <== stdio ==> copper-agent <== 127.0.0.1:<port>, token ==> engine (MCP server)
```

* `host/relay.py`: `Relay(host, project_dir).start()` listens on `127.0.0.1` on a random port. The wire is this
  protocol's framing; the first request must be `auth` `{"token": "<token>"}`; after that only `host.*`
  requests are accepted. A wrong token closes the connection without an answer.
* `host/rendezvous.py`: `$COPPER_HOME/hosts/<key>.json` = `{"port", "token", "pid", "protocol", "project_dir",
  "name", "started"}`, `key` = the focus store's project key. Stale entries (dead pid) are removed on lookup.
* `host/client.py: connect(project_dir)` returns a `HostClient` from the environment (`COPPER_HOST_PORT`,
  `COPPER_HOST_TOKEN`, set by the agent for its children) or from the rendezvous file, else `None`.

`host/route.py` is the tool-call boundary for such an engine:

* `project.guard_writable` (every writer calls it) asks `route.registered_for(path)`. When the project has a
  live host, a direct write is not refused with `FILE_OPEN_IN_KICAD` ("close KiCad") but with `LIVE_HOST`:
  "this edit belongs to the app". Nothing is written. Shadow checkouts, the routed call itself and
  `while_open='revert'` file jobs are exempt. The agent adds `COPPER_HOST_PROJECT` to its child's environment
  so the environment-given host applies to that project only.
* `route.call_tool(name, args, progress)` is a drop-in for `prepare_args -> registry.call_tool -> to_wire`:
  schematic and board-writing tools of a hosted project go through a cached `LiveSession` (checkout, engine on
  the shadow in live mode, uuid patch, `host.apply`); everything else, and every project without a host, runs
  the plain file pipeline. A `LIVE_HOST` refusal of the plain pipeline is retried through the host once. A host
  that went away falls back to the plain pipeline (which then refuses on the editor lock as before).
* `route.try_live(...)` returns `None` instead of falling back, for callers that keep their own pipeline.

Not wired yet: `mcp/server.py` still calls `registry.call_tool` directly, so an external MCP client editing a
hosted project gets the `LIVE_HOST` refusal instead of a landed change. The edit is one call at the server's
tool-call site: `result = route.try_live(name, args, progress)` and the existing pipeline when that is `None`.

## 10. Versioning

* `protocol` is one integer. v1 is this page.
* Negotiation: the app sends the highest version it speaks; the agent answers `min(app, agent)` or
  `UNSUPPORTED_PROTOCOL` with `data.min` / `data.max` if the app is older than the agent's minimum. The app
  disconnects if the answer is below its own minimum.
* Within a version, additions are allowed and must be ignored by old receivers: new optional params, new
  result fields, new event fields, new `data.code` strings, new notifications, new `error.code` values under
  an existing `kind`, new entries in `hello` lists.
* A new version is needed for: a new event `type`, a new `error.kind`, a new `done.status`, a changed
  meaning or type of an existing field, a new required param, a new ordering rule.
* Optional host methods are negotiated by `hello.host_methods`, not by version.

## 11. A complete exchange

A no-account command that edits a value. `>` is app to agent, `<` is agent to app. Long strings are cut.

```
> {"jsonrpc":"2.0","id":1,"method":"hello","params":{"protocol":1,"app_version":"fakehost/1","kicad_cli":null,"python":"...","project_dir":"D:/w/live","frame":"schematic"}}
< {"jsonrpc":"2.0","id":1,"result":{"protocol":1,"engine_version":"1.0.0","agent":{"pid":48572,"python":"3.11.5"},"accounts":[{"id":"none","kind":"none","label":"No account (commands only)","has_secret":false}],"drivers":[...],"commands":[...],"limits":{...},"load_errors":{}}}
> {"jsonrpc":"2.0","id":2,"method":"command.run","params":{"name":"tool","args":{"tool":"sch_apply","arguments":{"ops":[{"op":"set_field","ref":"R1","field":"Value","value":"4k7"}]}},"focus":null}}
< {"jsonrpc":"2.0","id":2,"result":{"turn":"t-8a5795f5"}}
< {"jsonrpc":"2.0","method":"event","params":{"turn":"t-8a5795f5","seq":1,"type":"tool_start","call":"c1","name":"sch_apply","args":{"ops":[{"op":"set_field","ref":"R1","field":"Value","value":"4k7"}]}}}
< {"jsonrpc":"2.0","id":"a1","method":"host.checkout","params":{"document":"schematic"}}
> {"jsonrpc":"2.0","id":"a1","result":{"shadow_dir":"D:/w/live/.copper/shadow/1","rev":1,"document":"schematic","project":"c3-devboard","sheets":[{"sheet_path":"/","file":"c3-devboard.kicad_sch"}],"dirty":false}}
< {"jsonrpc":"2.0","method":"event","params":{"turn":"t-8a5795f5","seq":2,"type":"tool_progress","call":"c1","fraction":0.6,"message":"netlist check"}}
< {"jsonrpc":"2.0","id":"a2","method":"host.apply","params":{"base_rev":1,"document":"schematic","patches":[{"sheet_path":"/","file":"c3-devboard.kicad_sch","remove":["635743e0-96b3-4858-b080-c4407ad3314a"],"add":"(symbol\n\t(lib_id \"Device:R\")..."}],"lib_symbols":[],"summary":"R1.Value = '4k7'","expect":{"netlist_hash":"sha256:..."}}}
> {"jsonrpc":"2.0","id":"a2","result":{"proposal_id":"p1","state":"review","rev":2,"counts":{"added":0,"changed":1,"removed":0},"erc_new":[],"netlist_ok":true}}
> {"jsonrpc":"2.0","method":"doc.changed","params":{"document":"schematic","rev":2}}
< {"jsonrpc":"2.0","method":"event","params":{"turn":"t-8a5795f5","seq":3,"type":"tool_end","call":"c1","ok":true,"summary":"1 change(s) written to c3-devboard.kicad_sch; connectivity 0/0 ok","ms":22359}}
< {"jsonrpc":"2.0","method":"event","params":{"turn":"t-8a5795f5","seq":4,"type":"proposal","proposal_id":"p1","document":"schematic","state":"review","summary":"R1.Value = '4k7'","counts":{"added":0,"changed":1,"removed":0},"erc_new":[],"netlist_ok":true,"refs":["R1"],"region":[66.04,58.42,66.04,58.42],"rev":2}}
< {"jsonrpc":"2.0","method":"event","params":{"turn":"t-8a5795f5","seq":5,"type":"text_delta","text":"1 change(s) written to c3-devboard.kicad_sch; ...\n"}}
< {"jsonrpc":"2.0","method":"event","params":{"turn":"t-8a5795f5","seq":6,"type":"done","status":"ok"}}
> {"jsonrpc":"2.0","method":"proposal.resolved","params":{"proposal_id":"p1","outcome":"kept"}}
```

A cancelled turn that was waiting for the user:

```
> {"jsonrpc":"2.0","id":12,"method":"turn.start","params":{"conversation":"c-7","text":"route this net class","account":"acct-1a2b3c","mode":"chat"}}
< {"jsonrpc":"2.0","id":12,"result":{"turn":"t-0c1d2e3f","conversation":"c-7"}}
< {"jsonrpc":"2.0","method":"event","params":{"turn":"t-0c1d2e3f","seq":1,"type":"tool_start","call":"c1","name":"pcb_route"}}
< {"jsonrpc":"2.0","id":"a9","method":"host.ask_permission","params":{"kind":"file_job","detail":{"tool":"pcb_route","document":"board","label":"pcb_route","note":"runs on the saved board file; undo is Restore checkpoint"}}}
> {"jsonrpc":"2.0","id":13,"method":"turn.cancel","params":{"turn":"t-0c1d2e3f"}}
< {"jsonrpc":"2.0","method":"host.cancelled","params":{"id":"a9","reason":"turn_cancelled"}}
< {"jsonrpc":"2.0","id":13,"result":{"cancelled":true}}
< {"jsonrpc":"2.0","method":"event","params":{"turn":"t-0c1d2e3f","seq":2,"type":"tool_end","call":"c1","ok":false,"error":{"code":"CANCELLED","message":"cancelled"},"ms":2210}}
< {"jsonrpc":"2.0","method":"event","params":{"turn":"t-0c1d2e3f","seq":3,"type":"done","status":"cancelled"}}
```

A model stream that was cut off, and a line that was not JSON:

```
< {"jsonrpc":"2.0","method":"event","params":{"turn":"t-77aa01bc","seq":2,"type":"text_delta","text":" world"}}
< {"jsonrpc":"2.0","method":"event","params":{"turn":"t-77aa01bc","seq":3,"type":"error","kind":"network","message":"the model stream ended before it was complete","code":"TRUNCATED","retryable":true}}
< {"jsonrpc":"2.0","method":"event","params":{"turn":"t-77aa01bc","seq":4,"type":"done","status":"error"}}
> this is not json
< {"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"parse error: the line is not JSON","data":{"code":"PARSE_ERROR"}}}
```

## 12. What is verified, and what is not

Verified by `tests/test_agent_protocol.py` against a real agent process and `tests/fakehost.py`: sections 1-4
and 6 as the agent implements them; `host.checkout`, `host.apply` (including `STALE_BASE`, `BUSY`, all-or-nothing
and one undo step on file copies), `host.ask_permission`, `host.cancelled`; the relay and rendezvous; the
baseline patch round trip.

Verified by `tests/test_patch_fixtures.py` (kicad-cli 10.0.6): on 36 fixture edits (place, value and field
changes, move, delete, swap, wires, labels, power symbols, no-connects, net rename, text, PWR_FLAG, a batch,
multi-unit symbols, a hierarchical sub-sheet, a sheet file used twice, a 219 kB sheet) the original sheets plus
the patch, applied by the reference applier, have the same kicad-cli netlist as the engine's direct write, in
both writer modes (KiCad re-saved, and live mode); every `add` is a clean fragment that kicad-cli reads back
item for item. 35 of the 36 pass; one (`hier_root_move_overlap`) has a result whose netlist KiCad itself reports
differently depending on item order (7.4); it is reported as KNOWN, not as a pass, in the runs where that shows.
Verified by `tests/test_patch_live.py`: patch refusals, library-symbol routing per sheet, live mode, the
`guard_writable` hook (`LIVE_HOST`), and `host/route.call_tool` landing edits through a relay and the rendezvous
file in a file-backed host, including `STALE_BASE` recovery and sub-sheet / shared-sheet patches.

Not verified, because it needs the compiled app: everything the app does inside `host.*` (serialising live
sheets, the clipboard parser's copy-only branch accepting `add` (7.5), one commit across sheets, same-uuid modification, review mode,
in-process ERC delta, the app-side `netlist_hash`), the board file-job sequence end to end (`host.save`,
`host.checkpoint`, `host.reload` are only stubbed in fakehost), and `focus.changed` from a real selection.
`host.verify` and `files` in `host.apply` are implemented on both Python sides but have no test yet.

Verified by `tests/test_agent_conversations.py` (whole conversations: a scripted model behind a local
chat-completions server issues tool calls to a real agent process; fakehost stands in for the app on copies of
a real dev board, KiCad's `complex_hierarchy` and `video` demos, and a project in a folder with spaces and
non-ASCII letters): add a part and wire it, change a value, delete parts, rename a net, a refused step and a new
ERC finding repaired in the same turn, an edit on a sheet file used twice, a multi-unit symbol, a user edit
during and between turns (`STALE_BASE`, retry builds on the user's edit), Stop during an engine tool and during
`host.apply`, the agent killed mid-turn and restarted, 150 `focus.changed` notifications around a turn and a
hostile focus object, a mute / busy / failing app, model-side failures, a model-driven board job (denied, run,
stopped), and a host whose document differs from the saved files. Each scenario checks that the document's
bytes change only at `host.apply` (or a board job's reload), that `netlist_ok` is true and the named pins are
on the named nets by kicad-cli, that the turn ends, that every patch passes `fragment_problems` against the
definitions sent or cached on the destination sheet, and at the end that the account secret is in no protocol
line, log or file other than the credential store.

Bugs these conversations found and that are fixed (each has a regression test there): the model was told only
the COUNT of the app's new ERC findings; read tools ran on the files as last saved instead of the open document;
the file `checkpoint` tool could restore files under the editor; a tool that failed after writing its shadow had
its edit applied with the NEXT change; a model-driven board tool could not be stopped and a stopped one left the
board file different from the editor; every edit of an engine-arranged sheet re-drew the whole sheet; engine
snapshots were written inside shadows (and pushed paths over the Windows limit); a shadow outside
`<project>/.copper/shadow` was not recognised as one; a line break in a focus value could start a `[note]` line;
credentials inside a `base_url` were stored and echoed in the clear; the "engine never imports the agent" test
had been dead since it was written (a control character in its pattern).

fakehost conflates two things the app keeps apart, and tests that need the difference use the `UnsavedHost`
of that file: the open document (memory) and the project files (as last saved). fakehost also re-saves an
older-format project in the current format when it "opens" it, as the app does.
