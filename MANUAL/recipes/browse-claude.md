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
./recipes/browse-claude --detail edits   # prompts, replies + file edits
```

**Detail levels.** A transcript carries far more than speech, so rows are
gated by a detail level — each record has a *minimum* level at or above
which it shows:

- `1` `summary` — the skeleton: user prompts (including ones you queued
  while it worked) and the agent's `end_turn` responses. The default —
  what you asked and what it concluded.
- `2` `voice` — all speech: adds intermediate assistant text, readable
  thinking (running commentary, marked `🧠 thinking:`),
  `AskUserQuestion`, inter-agent messages, and the compaction boundary.
- `3` `edits` — adds the file-mutating tool calls (`Edit` / `Write` /
  `NotebookEdit` / `MultiEdit`): review what changed without the noise.
- `4` `tools` — adds every other tool call / result and the inline
  turn-duration / api-error framing.
- `5` `detailed` — adds a curated set of useful metadata (summaries,
  prompts, PR links, worktree state, tags, local commands, attachments…).
- `6` `all` — every record kind the recipe knows about, including
  bookkeeping.
- `7` `unknown` — adds records whose type / subtype / attachment type
  the recipe does not recognise: a maintenance view for spotting new
  record kinds.

Set the boot level with `--detail LEVEL` (a number `1`-`7` or the word
`summary` / `voice` / `edits` / `tools` / `detailed` / `all` /
`unknown`); change it live with the `1`-`7` keys.

**Subagents.** In tree mode (the default) every subagent the session
dispatched — Agent/Task subagents and in-process teammates alike — lists
in one top block above the turn umbrellas, bracketed by `--- Subagents:` /
`--- Session:` dividers, ordered by dispatch position; a transcript with
no dispatch site in the main thread sorts last, tagged `orphan` and
dimmed. Expand a subagent row to browse its own transcript as a nested
tree.

**Cross-link jumps.** Rows that participate in a leader↔teammate
interaction carry a `[↗]` marker in their title, on both sides of the
interaction — every link is one half of a two-way pair, so `Enter` jumps
across it in either direction. At the session level the spawn site
(`Agent`/`Task` call) pairs with the subagent group row. At the message
level each `SendMessage` the leader sent pairs with the teammate's
matching inbound message, each inbound teammate reply pairs with the
teammate's `SendMessage` that produced it, and the teammate's first
inbound message (the spawn prompt delivery) jumps back to the spawn
record. When one row carries several links (an assistant record spawning
or messaging several teammates at once), `Enter` opens a menu of targets;
the same jump rows also lead the row's context menu (`\` / F1 /
right-click). A target hidden by the active detail level lands on its
nearest visible row instead. `Enter` on an unmarked row does nothing.

Keys: `Enter` follow a `[↗]` cross-link, `1`-`7` set the detail level,
`e` / `o` open in `$EDITOR`, `y` show id (debugging).

**Source:** [`recipes/browse-claude`](../../recipes/browse-claude)

---

*[← All recipes](../recipes.md)*
