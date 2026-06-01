# browse-git improvements — design

**Date:** 2026-06-01
**Status:** approved-pending-spec-review
**Recipe:** `recipes/browse-git`

## Goal

Bring the `browse-git` recipe closer to `tig`'s usefulness without
reaching for curses or a heavy redesign. Four threads:

1. **Colorize every git preview.** The preview pane already honors ANSI
   (`BrowserConfig.preview_ansi=True`); the recipe currently pipes
   *uncolored* `git show`, so the fix is making git/`delta` emit color.
2. **Branch/tag decorations** surfaced in the commit list.
3. **Use `delta`** for per-file diffs when available, configured to look
   terminal-native.
4. **View modes** beyond the default commit log: `status`, `reflog`,
   `branches`, `stash`, switchable by flag and in-app key.

Non-goals (explicitly dropped during brainstorming): ASCII commit-graph
art, copy-SHA-to-clipboard, and any framework-level renderer change.

## Constraints discovered (ground truth from the codebase)

- **Preview ANSI works** end to end: `preview_ansi` defaults `True`, and
  the wrap walker (`_wrap_preview_line` / `SgrState`) re-emits SGR
  verbatim, including 256-color and truecolor. Non-CSI escapes (OSC-8
  hyperlinks) are **not** handled — they would leak as text.
- **`git show --color=always`** emits SGR even when stdout is a pipe.
- **`delta`** colorizes when piped too. With `--no-gitconfig` the OSC-8
  hyperlink emission (driven by the user's `[delta] hyperlinks=true`)
  disappears. `--color=always` is **not** a delta flag.
- **List rows cannot embed ANSI in `title`.** `_truncate_by_cells` is
  plain-text-only (counts escape bytes as visible cells), so width math
  breaks if a title carries SGR.
- **`format_item` is called with `ctx=None`** and without
  `depth`/`selected`/`expanded`, so a full override loses the ▼/▶
  expand marker, the multi-select `*` marker, and indentation. Therefore
  the recipe stays on the **default formatter** and uses the single
  `tag`/`tag_style` chip for color.
- `delta` and `batcat` are both on PATH in the dev environment; git is
  2.53. The recipe must still degrade gracefully when `delta` is absent
  and when `git` is absent / cwd isn't a repo (current behavior).

## Decisions locked during brainstorming

- **Modes:** ship all five — `commits` (default), `status`, `reflog`,
  `branches`, `stash`.
- **Row layout:** ref decorations only (no graph art).
- **Mode switching:** both a `--mode NAME` launch flag **and** an in-app
  `m` key opening a `ctx.pick` chooser.
- **Niceties:** status-colored file list, author + relative date on
  commit rows, "open diff in pager" key. (No copy-SHA.)
- **Row coloring (decision D):** single `tag` chip only — **no**
  framework change. Multi-colored chips were considered and rejected to
  keep the change recipe-local.

## Architecture

The recipe keeps its shape: a `get_children` that lists rows and a
`get_preview` that renders text, both dispatched by **id shape**. Two
module-level knobs drive behavior, set once in `main()` from argv and
mutated by the mode picker:

- `_log_limit` — existing `-n NUMBER` cap.
- `_mode` — one of `commits` / `status` / `reflog` / `branches` / `stash`.

A module-level `_browser` reference is stashed in `main()` so
`get_preview` (which receives no `ctx`) can read `_browser.preview_width`
for `delta --width`.

### ID scheme (string, dispatched by prefix then shape)

A single `_parse_id(item_id) -> tuple` helper classifies every id.
Dispatch order (a real git sha is hex, so it never collides with the
English prefixes):

| Kind | id form | Children | Preview |
| ---- | ------- | -------- | ------- |
| commit | `<sha>` (bare hex) | changed files | colored `show --stat` |
| file | `<sha>:<path>` (first colon splits) | — (leaf) | colored per-file diff |
| ref | `ref:<refname>` | commits on that ref | `show --stat <ref>` |
| status file | `status:<XY>:<path>` | — (leaf) | staged and/or worktree diff |
| stash | `stash:<n>` | files in the stash | `stash show -p <n>` |
| stash file | `stash:<n>:<path>` | — (leaf) | per-file stash diff |
| reflog | `reflog:<n>:<sha>` | changed files (`<sha>:<path>`) | `show --stat <sha>` |

`reflog` entries carry the index `n` because the reflog frequently
revisits the same sha; without `n` the global id index would collapse
duplicates into one row. `status` and `stash` parse with
`split(':', 2)` so a path containing colons survives intact; `ref`
keeps everything after the first `:` as the refname.

The root listing is chosen by `_mode`. Drill-down for `commit` /
`file` ids is shared across `commits`, `reflog`, and `branches` modes —
a sha is a sha.

### Root listing per mode

- **commits:** `git log --format='%H%x00%D%x00%an%x00%ar%x00%s' -n LIMIT`.
  NUL-delimited fields → sha, decoration (`%D`), author, relative date,
  subject.
- **status:** `git status --porcelain` → one row per entry; `XY`
  porcelain code encoded in the id and shown as the tag.
- **reflog:** `git reflog --format='%H%x00%gd%x00%gs' -n LIMIT` → sha,
  selector (`HEAD@{n}`), action subject.
- **branches:** `git for-each-ref --format=…` over
  `refs/heads`, `refs/remotes`, `refs/tags` → `ref:<name>` rows tagged
  by kind. Children = that ref's `git log` (commit rows).
- **stash:** `git stash list --format='%gd%x00%s'` → `stash:<n>` rows.

### Drill-down

- commit/reflog/stash file lists use `--name-status` so each file row
  carries its A/M/D/R status letter in the `tag` chip.
- file leaves render a per-file unified diff; status-mode leaves choose
  `git diff --cached -- path` (staged) and/or `git diff -- path`
  (worktree) based on the `XY` code; untracked (`??`) renders the file
  as an addition.

## Row rendering (single-tag, default formatter)

`show_ids='never'`; the full sha lives in `item.id` for git operations.

| Row kind | `tag` (colored chip) | `title` (plain) |
| -------- | -------------------- | --------------- |
| commit | `sha[:7]` yellow | `subject` + optional ` ‹decorations›` + ` · author, reldate` |
| file | status letter (`A`/`M`/`D`/`R`) — green/yellow/red/cyan | path (new path for renames) |
| ref | ref kind (`branch`/`remote`/`tag`) colored | refname |
| reflog | `sha[:7]` yellow | `HEAD@{n}  action subject` |
| stash | `stash@{n}` yellow | stash subject |

Decorations are parsed from `%D` (`HEAD -> main, origin/x, tag: v1`),
delimited inline with `‹…›`. They are not individually colored (the
single-tag constraint); the chip stays on the sha. Author/date are
plain-text in the title. **This exact layout is the main thing to
confirm at spec review** — e.g. whether the colored chip should be the
decoration instead of the sha when a commit is a branch head.

## Colorization helpers

Two small, unit-testable helpers centralize all color:

- `_git_color(*args) -> str` — runs `git -c color.ui=always <args>` and
  returns stdout (used for `show --stat`, ref summaries, etc.).
- `_colorize_diff(raw: str) -> str` — if `delta` is on PATH, pipes
  `raw` through:

  ```
  delta --no-gitconfig --paging never --width <preview_width or 80>
  ```

  Otherwise returns the diff already colored by git
  (`git show --color=always …`). `--no-gitconfig` guarantees no OSC-8
  hyperlink leak and deterministic styling regardless of the user's
  delta config; `--paging never` stops delta from spawning a pager when
  its output is captured; `--width` matches the preview pane.

  "Terminal-like style" is realized by `--no-gitconfig` (drops the
  user's theme) — delta then uses its built-in defaults over the
  terminal palette. If a flatter look is wanted we can add
  `--syntax-theme none`; that is a spec-review knob, not a hard
  requirement.

## Mode switching

- **Flag:** `--mode NAME` is popped from argv in `main()` (same pattern
  as `-n`), validated against the five names; unknown → falls back to
  `commits` with an info-bar note.
- **Key:** an `Action('m', 'Mode', …)` handler calls
  `ctx.pick('mode', [...])`; on a choice it sets `_mode`, updates the
  title, and `ctx.refresh()`es the root.
- **Pager:** an `Action('p', 'Pager', …)` handler renders the cursor's
  colored diff and hands it to `ctx.page(text)` (delta/bat/less).

The window/help title reflects the active mode
(`browse-git [mode]`).

## Error handling

Preserved from today and extended:

- `git` missing → single red error row (existing `_err_item`).
- cwd not a repo → existing `rev-parse --is-inside-work-tree` guard.
- Any git subcommand failure surfaces its stderr as the error row or
  preview text (existing `returncode` branching).
- `delta` absent → silent fallback to `git --color=always` (no error).
- Empty mode listings (no stashes, clean working tree) → a single
  dim informational row ("no stashes", "working tree clean").

## Testing

**Unit (no tmux, fast — load the recipe module directly):**

- `_parse_id` classifies each id form correctly, including paths with
  colons and refnames with slashes.
- `%D` decoration parsing → expected `‹…›` string and per-token kinds.
- porcelain `XY` parsing → status letter + correct diff command choice.
- status-letter → tag_style map.
- `_colorize_diff` returns `\x1b[`-bearing text (delta path and the
  git-fallback path, the latter forced by monkeypatching `shutil.which`).

**tmux smoke (extend `test/ui/test_recipe_browse_git.py`):**

- existing three tests stay green (commits list, drill into files,
  rapid-scroll prefetch).
- `--mode reflog` lists reflog entries.
- `--mode status` lists a modified file with its status tag.
- `m` picker switches commits → reflog and reflog entries appear.
- a commit that is a branch head shows its decoration text in the row.

Color in previews is asserted at the unit level (`_colorize_diff`),
not via tmux capture (tmux strips SGR by default and escape-level
assertions are brittle).

## Documentation

- Update the recipe's module docstring and `_HELP_INTRO_TMPL` to cover
  modes, `--mode`, the `m`/`p` keys, and decorations.
- Update `docs/recipes.md`'s browse-git section to mention modes and
  delta usage.

## Logistics

- All work in the `worktree-browse-git-improvements` worktree.
- Tracked with the `plan` CLI; non-trivial tickets implemented by
  Opus / high-effort subagents, each followed by a review subagent
  (goal met? overengineered? stale code?). Leader commits per ticket.
