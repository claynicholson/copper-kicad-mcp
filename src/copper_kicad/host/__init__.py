"""The live host: the Copper app that owns the open document (docs/internal/HOST_PROTOCOL.md).

    client.py       Host (the interface), HostClient (typed calls over a transport), HostError, connect()
    rendezvous.py   $COPPER_HOME/hosts/<project-key>.json: where an engine process finds the host of a project
    relay.py        loopback listener inside copper-agent that forwards host.* calls from child processes
    live.py         LiveSession: runs an engine tool against a shadow checkout and lands the result with host.apply

Nothing reaches the user's open document except through ``Host.apply``.
"""
from .client import Host, HostClient, HostError, connect  # noqa: F401
