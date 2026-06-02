# browse-git improvements — design

**Date:** 2026-06-01
**Status:** approved-pending-spec-review
**Recipe:** `recipes/browse-git`

## Goal

Bring the `browse-git` recipe closer to `tig`'s usefulness without
reaching for curses or a heavy redesign. Threads:

1. **Colorize every git preview.** The preview pane already honors ANSI
   (`BrowserConfig.preview_ansi=True`); the recipe currently pipes
   *uncolored* `git show`, so the fix is making git/`delta` emit color.
2. **Branch/tag decorations** surfaced as colored tags in the commit
   and reflog lists.
3. **Use `delta`** for per-file diffs when available, configured to look
   terminal-native.
4. **View modes** beyond the default commit log: `status`, `reflog`,
   `branches`, `stash`, switchable by flag, by key, and auto-selected
   from positional arguments.
5. **md2ansi-color the commit message** in the preview, toggleable.

Non-goals this round: ASCII commit-graph art (see "Graph" below for why
it needs framework work), copy-SHA-to-clipboard.

## Constraints discovered (ground truth from the codebase)

- **Preview ANSI works** end to end: `preview_ansi` defaults `True`; the
  wrap walker (`_wrap_preview_line` / `SgrState`) re-emits SGR verbatim,
  including 256-color and truecolor. Non-CSI escapes (OSC-8 hyperlinks)
  are **not** handled — they leak as text.
- **`git show --color=always`** emits SGR even when stdout is a pipe.
- **`delta`** colorizes when piped too. With `--no-gitconfig` the OSC-8
  hyperlink emission (driven by the user's `[delta] hyperlinks=true`)
  disappears. `--color=always` is **not** a delta flag.
- **List rows cannot embed ANSI in `title`.** `_truncate_by_cells` is
  plain-text-only (counts escape bytes as visible cells); SGR in a title
  breaks width math. → md2ansi colors the *preview*, never list titles.
- **`format_item` is called with `ctx=None`** and without
  `depth`/`selected`/`expanded`, so a full override loses the ▼/▶
  marker, the multi-select `*` marker, and indentation. → we stay on the
  **default formatter** and extend it with `item.chips` (below).
- **Built-in keys:** `v` pages the cursor *preview* in `$PAGER`
  (`less -R`, ANSI-aware) — so it pages our colored diff for free; `e`
  edits the preview text in `$EDITOR`; `R` toggles preview ANSI. `m`,
  backtick, `tab` are unbound. `m` is the established **md2ansi toggle**
  key across recipes.
- **No single-node collapse** in the public Context API (only
  `expand(id)` and `collapse_all()`); Left collapses via private state
  mutation + repaint. → add `ctx.collapse(id)` (below).
- `delta` and `batcat` are on PATH in dev; git is 2.53. The recipe must
  degrade when `delta` / `md2ansi_lib` are absent.

## Decisions (locked during brainstorming)

| Topic | Decision |
| ----- | -------- |
| Modes | all five: `commits` (default), `status`, `reflog`, `branches`, `stash` |
| Row layout | ref decorations as colored tags; **no** graph art |
| Ref chips | add framework `item.chips` (reverses earlier single-chip call) |
| Mode switch | `--mode NAME` flag **+** backtick (`` ` ``) opens a picker **+** auto-select from positional args. No cycle key. |
| Enter | flips expand/collapse of the cursor row; never quits, never prints |
| Pager / editor | rely on built-in `v` / `e`; add `E` to edit the real working-tree file for file/status rows |
| md toggle | `m` toggles md2ansi coloring of the commit message in the preview |
| Niceties | status-colored file list; author + relative date chip; (no copy-SHA) |
| Errors | git-missing / not-a-repo → stderr message + `sys.exit(2)` before launch |
| ID scheme | mandatory kind prefix on every id |

## Framework prerequisites (small, reusable, separately reviewed)

Two additions land first, each with its own ticket + review and
`docs/api.md` updates:

1. **`item.chips`** — an optional `list[(text, style)]` on `Item`. The
   **default** `format_item_segments` path renders them as trailing
   colored chips after the title (same `_TAG_STYLE` palette as `tag`).
   ~6 lines; preserves selection/▼▶/indent chrome; width-correct (color
   is the segment `fg`, never embedded in text). This is also the
   primitive a future commit-graph would extend.

2. **`ctx.collapse(id)` / `browser.collapse(id)`** — symmetric
   counterpart to the existing `expand(id)`: discards `id` from
   `state.expanded` and triggers the same repaint path Left uses. The
   missing half of the expand/collapse API; needed for Enter's toggle.

## Why the graph is out of scope (answer to "what would it take")

A faithful, *colored* `git log --graph` is **not** a recipe-only change:

- **No colored left-gutter.** Rows are
  `[sel][indent][▼/▶][id][tag][title][chips]`. A branch-colored spine
  (`│ ├─┐ ┘`) needs *leading* colored segments; titles can't carry ANSI
  and `format_item` loses chrome. New framework concept required.
- **Connector rows.** `--graph` emits non-commit rows (`|\`, `| |`) that
  map to no `Item` — they'd need a new synthetic, non-selectable row
  kind.
- **Gutter collision.** Depth-indentation (drill into files) and the
  graph spine both want the left margin.

A monochrome hack (graph prefix shoved into the plain title, connector
rows dropped) needs no framework change but looks rough and breaks the
file drill-down. Deferred; `item.chips` is a step toward the colored
version.

## ID scheme (mandatory prefix, unambiguous)

Every id is `KIND:REST`. `_parse_id` does `kind, _, rest =
item_id.partition(':')` then parses `rest` per kind. No hex-vs-word
heuristics.

| Kind | id | Children | Preview |
| ---- | -- | -------- | ------- |
| `commit` | `commit:<sha>` | files | md2ansi message + colored `show --stat` |
| `file` | `file:<sha>:<path>` | — (leaf) | colored per-file diff |
| `ref` | `ref:<refname>` | commits on that ref | `show --stat <ref>` |
| `status` | `status:<XY>:<path>` | — (leaf) | staged and/or worktree diff |
| `stash` | `stash:<n>` | files in the stash | `stash show -p <n>` |
| `stash` file | `stash:<n>:<path>` | — (leaf) | per-file stash diff |
| `reflog` | `reflog:<n>:<sha>` | files (`file:<sha>:<path>`) | md2ansi message + `show --stat <sha>` |

`rest` parsing: `file`/`status` use `rest.split(':', 1)` (sha/XY then a
path that may contain colons); `reflog` uses `rest.split(':', 1)` →
`n`, `sha`; `stash` uses `rest.split(':', 1)` → `n`[, path]; `ref` keeps
all of `rest` (refnames may contain `/` and rarely `:`). Files are
**always** `file:<sha>:<path>`, so commit/reflog/branch drill-down share
one code path — a sha is a sha. `reflog` carries index `n` because the
reflog revisits the same sha and the global id index must not collapse
duplicate rows.

## Root listing per mode (newest-first)

- **commits:** `git log --format='%H%x00%D%x00%an%x00%ar%x00%s' -n LIMIT
  [<revs>] [-- <paths>]` → sha, decoration (`%D`), author, relative
  date, subject. Revs/paths come from positional args (below).
- **status:** `git status --porcelain` → one row per entry; the `XY`
  porcelain code is encoded in the id and shown as the tag. Clean tree →
  single dim "working tree clean" row.
- **reflog:** `git reflog --format='%H%x00%gd%x00%cr%x00%D%x00%gs'
  -n LIMIT` → sha, selector (`HEAD@{n}`), **relative date**,
  **decorations**, action subject. Reverse-chronological already.
- **branches:** `git for-each-ref --sort=-committerdate
  --format=…` over `refs/heads`, `refs/remotes`, `refs/tags` →
  `ref:<name>` rows tagged by kind. Children = that ref's `git log`.
- **stash:** `git stash list --format='%gd%x00%cr%x00%gs'` → `stash:<n>`
  rows. Empty → single dim "no stashes" row.

## Drill-down

- commit/reflog/stash file lists use `--name-status` so each file row
  carries its A/M/D/R status letter in the `tag` chip.
- file leaves render a per-file unified diff. status-mode leaves pick
  `git diff --cached -- path` (staged) and/or `git diff -- path`
  (worktree) by the `XY` code; untracked (`??`) renders the file as an
  addition.

## Row rendering (default formatter + chips)

`show_ids='never'`; the full sha lives in `item.id`.

| Row kind | `tag` (chip) | `chips` (trailing, colored) | `title` (plain) |
| -------- | ------------ | --------------------------- | --------------- |
| commit | `sha[:7]` yellow | refs: HEAD green-bold, branch cyan, remote blue, `tag:` yellow; then `author · 3d ago` dim | subject |
| reflog | `sha[:7]` yellow | `HEAD@{n}` dim, `3d ago` dim, ref decorations | action subject |
| file | status letter A/M/D/R (green/yellow/red/cyan) | — | path (new path for renames) |
| ref | kind `branch`/`remote`/`tag` colored | — | refname |
| stash | `stash@{n}` yellow | `3d ago` dim | stash subject |

Decorations parsed from `%D` (`HEAD -> main, origin/x, tag: v1`) → one
chip per ref, colored by kind. Author/date is a dim chip. (Possible
courtesy: when no md2ansi, nothing else changes.)

## Colorization helpers

Two small, unit-testable helpers centralize color:

- `_git_color(*args) -> str` — `git -c color.ui=always <args>`, returns
  stdout. Used for `show --stat`, ref summaries.
- `_colorize_diff(raw: str) -> str` — pipes `raw` through
  `delta --no-gitconfig --paging never --width <preview_width or 80>`
  when `delta` is on PATH; else returns git-colored diff
  (`git show --color=always …`). `--no-gitconfig` kills the OSC-8 leak
  and ignores the user's theme ("terminal-like"); `--paging never`
  stops delta spawning a pager off-tty; `--width` matches the pane
  (read from a module-level `_browser` ref since `get_preview` gets no
  `ctx`).

Commit/reflog node preview = md2ansi(message) (when `_MD_COLOR`) +
colored `show --stat`. The full per-file diff lives at the file leaf.

## md2ansi message coloring (`m` toggle)

Soft dependency, matching `browse-fs` / `browse-plan`:

```python
try:
    from md2ansi_lib import md2ansi as _md2ansi_fn
    if not callable(_md2ansi_fn): _md2ansi_fn = None
except Exception:
    _md2ansi_fn = None
_MD_COLOR = _md2ansi_fn is not None   # default on when available
```

`m` (bound only when `_md2ansi_fn` is present) flips `_MD_COLOR` and
invalidates the current preview. Off → raw message text.

## Positional args → auto mode (minimal heuristic)

`main()` pops `-n` and `--mode`, then classifies remaining positionals:

- everything after a literal `--` is a pathspec (no classification);
- else `os.path.exists(arg)` → pathspec;
- else `git rev-parse --verify --quiet '<arg>^{commit}'` succeeds → rev;
- else → stderr "unknown path or revision: <arg>", `sys.exit(2)`.

Positionals scope **commits** mode: `git log [revs] -- [paths]`, and the
title notes the filter. An explicit `--mode` other than `commits` wins
for the mode and ignores positionals with an info-bar note (reflog /
status / stash / branches don't take a pathspec here).

## Keys

| Key | Action |
| --- | ------ |
| Enter | flip expand/collapse of the cursor row (no quit) |
| `` ` `` | open mode picker (`ctx.pick`) → set `_mode`, retitle, refresh root |
| `m` | toggle md2ansi message coloring (when available) |
| `v` | (built-in) page colored preview/diff in `$PAGER` |
| `e` | (built-in) edit preview text in `$EDITOR` |
| `E` | edit the real working-tree file in `$EDITOR` (file/status rows) |

The window/help title reflects the active mode and any path/rev filter.

## Error handling

- **Fail-fast in `main()`** before launching: `git` missing → stderr
  + `sys.exit(2)`; cwd not a work tree (`rev-parse
  --is-inside-work-tree`) → stderr + `sys.exit(2)`. (Removes the
  in-TUI error row for these two fatal cases.)
- Per-subcommand failures mid-browse still surface their stderr as the
  error row / preview text (existing `returncode` branching).
- `delta` / `md2ansi_lib` absent → silent fallback, no error.

## Testing

**Unit (no tmux; import the recipe module directly):**

- `_parse_id` classifies every prefixed id, including paths and
  refnames containing colons/slashes.
- `%D` decoration parsing → chip list with correct (text, style) per
  ref kind.
- porcelain `XY` parsing → status letter + correct diff-command choice.
- positional classification: path vs rev vs `--` vs unknown(→exit).
- `_colorize_diff` returns `\x1b[`-bearing text on both the delta path
  and the git-fallback path (force the latter by monkeypatching
  `shutil.which`).
- chips render: `format_item_segments` emits the extra colored segments
  for an item with `chips` (framework unit test).
- `ctx.collapse(id)` removes `id` from `expanded` (framework unit test).

**tmux smoke (extend `test/ui/test_recipe_browse_git.py`):**

- existing three tests stay green.
- `--mode reflog` lists reflog entries.
- `--mode status` lists a modified file with its status tag.
- backtick opens the picker; choosing reflog switches the list.
- a branch-head commit shows its decoration chip text in the row.
- Enter toggles a commit's file list open/closed.

Preview color is asserted at the unit level (`_colorize_diff`), not via
tmux capture (tmux strips SGR; escape-level assertions are brittle).

## Documentation

- Recipe module docstring + `_HELP_INTRO_TMPL`: modes, `--mode`,
  positional args, the `` ` `` / `m` / `E` keys, Enter behavior,
  decorations.
- `docs/recipes.md` browse-git section: modes + delta usage.
- `docs/api.md`: `item.chips` and `ctx.collapse(id)` /
  `browser.collapse(id)`.

## Logistics

- All work in the `worktree-browse-git-improvements` worktree.
- Tracked with the `plan` CLI. Framework prereqs (chips, collapse) land
  first with their own review. Non-trivial tickets implemented by
  Opus / high-effort subagents, each followed by a review subagent
  (goal met? overengineered? stale code?). Leader commits per ticket.
