# recipes/browse-claude

Claude Code project / session / message browser.

**One-line summary:** three-level hierarchy walking
`~/.claude/projects/<encoded-path>/<session>.jsonl` files, with per-message
JSON pretty-print preview.

**Demonstrates:**

- Multi-level lazy hierarchy — id-shape dispatch (None →
  projects, dir-path → sessions, file-path → messages).
- JSON-line parsing with mixed record shapes (user, assistant, last-prompt,
  permission-mode).
- Compact summaries (one-line title with role + first 80 chars; full
  pretty-print in preview).
- `_human_time` style helpers — recipe-side formatting reaches the UI via
  the `tag` field plus `tag_style`.
- Truncation markers — `_MESSAGE_LIMIT` caps per-session enumeration; an
  explicit "(more — only first N shown)" row tells the user where the cliff
  is.
- Resolving message ids back to the source file via `ctx.run_external` to
  open the `.jsonl` in `$EDITOR`.

**Usage:**

```bash
./recipes/browse-claude                  # all projects
./recipes/browse-claude /home/me/work    # initial-scope
./recipes/browse-claude --detail tools   # boot at detail level 2
```

**Detail levels.** A transcript carries far more than speech, so rows are
gated by a detail level — each record has a *minimum* level at or above
which it shows:

- `1` `voice` — speech only: user prompts and assistant text replies
  (`AskUserQuestion` / `SendMessage` / task-notifications count as
  dialog). The default.
- `2` `tools` — the conversation as lived: voice plus every other
  user/assistant record (tool calls, tool results, thinking) and the
  inline turn-duration / api-error framing.
- `3` `detailed` — adds a curated set of useful metadata (summaries,
  prompts, PR links, worktree state, tags, local commands, attachments…).
- `4` `all` — every record, including bookkeeping and unknown kinds.

Set the boot level with `--detail LEVEL` (a number `1`-`4` or the word
`voice` / `tools` / `detailed` / `all`); change it live with the `1`-`4`
keys.

Keys: `1`-`4` set the detail level, `e` / `o` open in `$EDITOR`, `y` show
id (debugging).

**Source:** [`recipes/browse-claude`](../../recipes/browse-claude)

---

*[← All recipes](../recipes.md)*
