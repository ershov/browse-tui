# recipes/browse-jira

Open Jira tickets via the `jira` CLI (sketch).

**One-line summary:** lists open tickets assigned to the current user
through the `jira list` CLI, with `jira view` driving the preview
pane and an `o:Open` action that hands the ticket key to `$BROWSER`.
Environment-dependent — adapt the parser if your CLI's table layout
differs.

**Demonstrates:**

- An external CLI behind unreliable preconditions (auth, install,
  network) — degrades to a single friendly error Item rather than a
  traceback.
- Lazy preview fetch (`jira view <KEY>`) — no upfront cost for the
  list of tickets, only the cursor's description is fetched.
- A custom `Action` that punts to `$BROWSER` / `xdg-open` via
  `ctx.run_external`.

**Usage:**

```bash
./recipes/browse-jira
```

Requires the `jira` CLI on PATH (e.g. `go-jira`). If it's missing or
auth fails, the recipe shows a single error item explaining what's
wrong. Keys: `o` open ticket in `$BROWSER` / `xdg-open`.

**Source:** [`recipes/browse-jira`](../../recipes/browse-jira)

---

*[← All recipes](../recipes.md)*
