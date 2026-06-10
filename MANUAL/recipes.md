# browse-tui — Recipes

`browse-tui` ships with a set of single-file Python recipes — each carries a
`#!/usr/bin/env -S browse-tui --run-py` shebang, so you can make them
executable and run them directly, or invoke them as
`browse-tui --run-py recipes/<name>`. The richer ones (`browse-git`,
`browse-claude`, `browse-md`) double as worked examples of the full API; the
rest each demonstrate a different data-source pattern.

| Recipe | What it browses |
| --- | --- |
| [`browse-git`](recipes/browse-git.md) | A tig-like git browser — commits · status · reflog · branches · stash, a colored commit graph, and `delta`-powered diffs. |
| [`browse-claude`](recipes/browse-claude.md) | Claude Code history — projects → sessions → messages, with per-message JSON preview. |
| [`browse-md`](recipes/browse-md.md) | Markdown files as a navigable heading tree, previewed through md2ansi; `FILE.md#section` deep-links. |
| [`browse-fs`](recipes/browse-fs.md) | Filesystem browser with a live mtime watcher; edit / open / delete actions. |
| [`browse-plan`](recipes/browse-plan.md) | A project ticket tree over the `plan` CLI — status / edit / create / move flows. |
| [`browse-procs`](recipes/browse-procs.md) | Live process tree from `ps`, with a kill action. |
| [`browse-mcp`](recipes/browse-mcp.md) | The tools exposed by any MCP (stdio) server, with their JSON schemas. |
| [`browse-jira`](recipes/browse-jira.md) | Open Jira tickets via the `jira` CLI. |
| [`browse-jira-mcp`](recipes/browse-jira-mcp.md) | Open Jira tickets via the Atlassian MCP server. |
| [shell pickers](recipes/shell-recipes.md) | `browse-files` · `browse-find` · `browse-ls` — tiny pure-bash pickers built from CLI flags alone. |

**Writing your own:** [recipes/writing-recipes.md](recipes/writing-recipes.md)
walks through a recipe skeleton, common patterns, and the framework
constraints to keep in mind.

## See also

- [api.md](api.md) — full Python API.
- [cli.md](cli.md) — CLI flags (also runnable from a recipe via
  `browse-tui --run-py …`).
- [../README.md](../README.md) — quickstart.
