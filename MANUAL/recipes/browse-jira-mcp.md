# recipes/browse-jira-mcp

Open Jira tickets via the Atlassian MCP server.

**One-line summary:** same UX as `browse-jira`, but instead of shelling
out to the `jira` CLI it speaks JSON-RPC to `mcp-atlassian` (launched
once per session via `uvx`), calling `jira_search` for the row list and
`jira_get_issue` for the preview pane. Credentials come from
`JIRA_URL` / `JIRA_USERNAME` / `JIRA_API_TOKEN`.

**Demonstrates:**

- A long-lived helper subprocess behind `get_children` /
  `get_preview` — the MCP server is started lazily, the
  `initialize` handshake runs once, and subsequent tool calls reuse
  the same stdio pipe (lock-guarded so concurrent callbacks don't
  interleave).
- Line-delimited JSON-RPC client — no extra dependencies, just
  `subprocess` and `json`.
- Graceful degradation when env vars are missing or the server fails
  to start / authenticate — surfaces a single error Item instead of
  a traceback.
- `atexit`-driven cleanup so the helper process is terminated when
  the recipe exits.

**Usage:**

```bash
export JIRA_URL=https://jira.example.com/
export JIRA_USERNAME=you@example.com
export JIRA_API_TOKEN=…
./recipes/browse-jira-mcp
```

Keys: `o` open ticket URL in `$BROWSER` / `xdg-open`.

**Source:** [`recipes/browse-jira-mcp`](../../recipes/browse-jira-mcp)

---

*[← All recipes](../recipes.md)*
