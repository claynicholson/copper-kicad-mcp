# Security

Please report vulnerabilities **privately** through GitHub:
[Report a vulnerability](https://github.com/claynicholson/copper-kicad-mcp/security/advisories/new).
Do not open a public issue for them.

Useful to include: the Copper version (`copper --version`), how it was installed, and the steps or file that
trigger the problem. You will get an answer in the advisory thread.

In scope: the MCP server, the `copper` CLI and setup page, the installers and the npm launcher. Copper runs locally,
binds its setup page and canvas to 127.0.0.1, never calls a language model and stores no credentials in plain files.
