# recipes/browse-mcp

Inspect the tools exposed by any MCP (stdio) server.

**One-line summary:** spawns the given command as an MCP server, runs the
`initialize` handshake, calls `tools/list`, and presents each tool as an
Item; the preview pane shows the tool's description plus its JSON input
schema, pretty-printed.

**Demonstrates:**

- A generic line-delimited JSON-RPC client behind `get_children` /
  `get_preview` — no dependencies beyond `subprocess` and `json`; the
  server is started lazily and the `initialize` handshake runs once.
- Construction via `BrowserConfig` — the config-object form of the
  `Browser(...)` constructor.
- Graceful degradation — a sentinel error Item (not a traceback) when the
  command is missing or the handshake / `tools/list` call fails.
- An `a:About` action that pages the server's `serverInfo` (name +
  version) for orientation, plus `atexit` cleanup of the helper process.

**Usage:**

```bash
./recipes/browse-mcp uvx mcp-atlassian@v0.21.1
./recipes/browse-mcp npx -y @modelcontextprotocol/server-filesystem /tmp
./recipes/browse-mcp python -m my_mcp_server
```

Credentials / configuration are read from the parent environment — export
whatever the chosen server expects (e.g. `JIRA_API_TOKEN`) before
launching. Keys: `a` page the server's `serverInfo`.

**Source:** [`recipes/browse-mcp`](../../recipes/browse-mcp)

---

*[← All recipes](../recipes.md)*
