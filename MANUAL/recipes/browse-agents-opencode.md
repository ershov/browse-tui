# recipes/browse-agents-opencode

OpenCode project / session / record browser.

**One-line summary:** reads the OpenCode SQLite store read-only and folds
each session's messages into turns with tool umbrellas and rendered
previews.

**Parts build.** A generated file: `./build-agents.sh` concatenates
`recipes/browse-agents-src/common/` (the view code shared by every agent
flavour) and `recipes/browse-agents-src/opencode/` (the OpenCode store,
parser and poll tail) into `recipes/browse-agents-opencode`. Edit the parts,
rebuild with `./build-agents.sh` (or `./build-all.sh`), and commit the
generated recipe together with the parts. See
`recipes/browse-agents-src/DESIGN.md`.

**Hierarchy:**

- projects — the `project` rows (a git root, or `global`), most recently
  active first;
- sessions — `session_v2` rows (or `session` rows on a 1.x store), titled
  by the session title and tagged with the agent, model, message count,
  child-session count and age;
- records — one row per content element of each message (reasoning, tool
  call, tool result, text), folded into `<prompt>` turns opened by the user
  message and closed by the `idle` message (1.x: `step-start` /
  `step-finish`). Sessions spawned by this one (`parent_id`) list in a top
  block, expandable into their own tree.

**Usage:**

```bash
./recipes/browse-agents-opencode                      # all projects
./recipes/browse-agents-opencode /home/me/work        # one project worktree
./recipes/browse-agents-opencode --session ses_01ab   # a session (unique id prefix)
./recipes/browse-agents-opencode --db ~/backup/opencode.db --detail tools
```

Flags: `--db PATH`, `--session ID`, `--item N`, `--tree` / `--no-tree`,
`--detail LEVEL`, `-n NUMBER`. The store is `$OPENCODE_DB` when set, else
the newest `opencode*.db` under `$XDG_DATA_HOME/opencode` (default
`~/.local/share/opencode`).

**Detail levels** (`--detail`, or the `1`-`7` keys live):

- `1` `summary` — user prompts and the reply that ended each turn.
- `2` `voice` — adds intermediate assistant text and reasoning.
- `3` `edits` — adds `edit` / `write` / `patch` calls with their diffs.
- `4` `tools` — adds every other tool call and result, the `idle` turn end
  (1.x: `step-finish` with cost and tokens) and assistant errors.
- `5` `detailed` — adds system text, synthetic context, skill and shell
  messages, agent / model / location selections and retries.
- `6` `all` — adds snapshots, patches and empty assistant messages.
- `7` `unknown` — adds record kinds the recipe does not recognise.

**Format notes.** The transcript lives in DB rows, not files: `E` / `V` on a
session open a pretty-printed JSON dump of its message rows, and on
selected records a dump of just those records. The tail polls the
session's row count, max `seq` and max `time_updated` every second, so new
messages appear as they land; a running tool shows a `(running)` result
placeholder that refreshes on Ctrl-R or when its turn is re-expanded
(live in-place refresh is tracked in #1303). Tool inputs are
normalised to the shared names (`path` → `file_path`, `oldString` →
`old_string`) so the common formatters render them; an `edit` result
carries the patch from its metadata as the diff.

Keys: `Enter` follow a `[↗]` link to a child session, `1`-`7` set the
detail level, `J` / `K` jump between voice rows, `t` toggle tree / flat,
`e` / `E` open the preview / the JSON dump in `$EDITOR`, `y` show the row id.

**Source:** [`recipes/browse-agents-src/`](../../recipes/browse-agents-src/) (generated: [`recipes/browse-agents-opencode`](../../recipes/browse-agents-opencode))

---

*[← All recipes](../recipes.md)*
