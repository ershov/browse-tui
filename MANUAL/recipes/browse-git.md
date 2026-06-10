# recipes/browse-git

A tig-like browser over a git repository, with five view modes.

**One-line summary:** five switchable modes — commits (default), status,
reflog, branches, stash — over a mandatory `KIND:`-prefixed id scheme;
colorized diff / message previews; ref-decoration and author·date chips
on commit rows.

**Modes:**

- **commits** — recent `git log`; drill commit → changed files →
  per-file diff. Scoped by positional revs / paths, or spanned over every
  local branch with `--all` (git log's `--branches`). An unscoped log is
  preceded by up to four synthetic working-change rows — `Untracked
  changes` / `Tracked changes` / `Staged changes` / `Conflicts` — each
  shown only when non-empty and drillable into its files.
- **status** — `git status` working-tree entries; the leaf preview is
  the staged and/or worktree diff chosen by the porcelain `XY` code
  (`MM` shows both sections; `??` renders the file as an addition).
- **reflog** — `git reflog` entries with their `HEAD@{n}` selector;
  same commit → file → diff drill-down.
- **branches** — branches / remotes / tags via `git for-each-ref`,
  tagged by kind; drill a ref into its commits.
- **stash** — `git stash list`; drill into a stash's files.

Switch modes with `--mode NAME` at launch, the backtick (`` ` ``) picker
at runtime, or auto-selection from positional args.

**Demonstrates:**

- A tagged-tuple id scheme (`('commit', sha)`, `('file', sha, path)`,
  `('ref', name)`, `('status', xy, path)`, `('stash', n)`, `('reflog', n,
  sha)`) — structured, hashable ids with no string parsing; `get_children`
  / `get_preview` dispatch on the tag (`id[0]`).
- Colorized previews: `git -c color.ui=always` for diffs / stat, piped
  through [`delta`](https://github.com/dandavison/delta) when it is on
  PATH (`--no-gitconfig --paging never --width <preview_width>`), with a
  silent fallback to the git-colored diff when `delta` is absent.
- Optional md2ansi commit-message coloring via the soft `md2ansi_lib`
  import, toggled with `m` (the same pattern as `browse-fs` /
  `browse-plan`).
- `item.chips` — ref/tag decorations parsed from `%D` plus an
  `author · relative-date` chip render as colored trailing chips on
  commit rows; file rows carry an A/M/D/R status-letter tag.
- `on_enter` as a callable that flips expand/collapse via `ctx.expand` /
  `ctx.collapse` (Enter never quits in long-running browse mode).
- `ctx.pick` for the runtime mode picker; `ctx.run_external` for the `E`
  working-tree edit; positional rev-vs-path classification via
  `git rev-parse`.
- Fail-fast in `main()` — `git` missing or not inside a work tree exits
  with a stderr message before the TUI launches.

**Usage:**

```bash
cd /path/to/your/repo
./recipes/browse-git                    # commits mode
./recipes/browse-git --all              # log spanning every local branch
./recipes/browse-git --mode status      # working-tree changes
./recipes/browse-git -n 200 HEAD~50     # cap + root the log at a rev
./recipes/browse-git -- src/            # filter the log to a path
```

Keys: `` ` `` mode picker, `Enter` flip expand/collapse, `E` edit the
working-tree file in `$EDITOR` (file/status rows), `m` toggle md2ansi
message coloring (when available); the built-in `v` pages the colored
diff in `$PAGER`, `e` edits the preview text.

**Source:** [`recipes/browse-git`](../../recipes/browse-git)

---

*[← All recipes](../recipes.md)*
