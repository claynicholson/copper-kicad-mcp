"""copper-agent: the sidecar the Copper app talks to (docs/internal/HOST_PROTOCOL.md).

    python -m copper_kicad.agent          newline-delimited JSON-RPC 2.0 on stdio, logs on stderr

The engine (everything else in copper_kicad) never calls a model. The model loop lives here and only here:

    server.py      the stdio JSON-RPC peer: hello, turns, cancel, watchdog, host requests
    turn.py        one turn: the event emitter and the single door to engine tools (Turn.call_tool)
    events.py      the normalised event model and its conformance checks
    sessions.py    <project>/.copper/conversations/*.jsonl
    prompt.py      the stable (cacheable) prefix and the per-turn focus line
    accounts.py    account records (secrets go to link/store.py, never to disk in clear, never to the app)
    drivers/       model access: base.py is the interface, none.py the no-account driver
    commands/      no-model commands (/help, /focus, /tool, /verify, /tidy, /route, /pour, /export, /parts ...)
    jobs.py        engine tools in a child process the user can stop; board file jobs and their checkpoints
    usage.py       <project>/.copper/usage.jsonl: what every turn used, summed per project
"""
