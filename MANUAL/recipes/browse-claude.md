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
```

Keys: `e` / `o` open in `$EDITOR`, `y` show id (debugging).

**Source:** [`recipes/browse-claude`](../../recipes/browse-claude)

---

*[← All recipes](../recipes.md)*
