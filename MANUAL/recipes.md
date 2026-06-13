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

## Reading from stdin (`-`)

Three recipes accept a lone `-` argument and take their data from stdin
instead of the filesystem / repo. The read happens once, before the UI
opens (a pipe is slurped, a tty blocks until `^D` like `cat`); stdin is
one-shot, so `Ctrl-R` re-serves the same parse. In every case `-` is the
whole data source — it combines with no other positional argument, and the
bare form of each recipe on a terminal is unchanged (still browses the cwd
/ current repo):

**Piping auto-engages stdin mode — `-` is optional.** When stdin is a pipe
or redirect (not a tty), each recipe detects it and behaves as if `-` were
passed, so `cmd | browse-md` is equivalent to `cmd | browse-md -`. The `-`
remains the *explicit* form, and is required only to read stdin when stdin
is a terminal (type/paste the input, then `^D`). Auto-detect changes
nothing else: piped data **plus** a positional path is still an error (the
two name the data source in conflicting ways), and a bare invocation on an
interactive terminal still browses the cwd / repo without touching stdin.

- **`browse-md -`** — read **one** Markdown document from stdin and browse
  it as a heading tree. The root row is titled `-` (matching the invocation); it has no on-disk
  source, so the `V` / `E` (page / edit the source file) actions have
  nothing to open and flash instead of acting. Empty input is an empty
  document (an empty tree), not an error. Cross-file reference following
  is off for the piped doc unless you supply candidate base directories
  with the repeatable `--root DIR` flag — which also adds extra resolution
  bases (tried after the file's own directory) in file mode, for refs that
  live outside the file's tree.

  ```bash
  glow-flavoured-render | browse-md            # '-' optional when piping
  git show HEAD:README.md | browse-md --root "$(git rev-parse --show-toplevel)"
  ```

- **`browse-fs -`** — read a **newline-separated list of paths** (one per
  line; blank lines skipped, duplicates dropped) and show them as the root
  rows, in order. Each path is shown verbatim as the row title; existing
  directories expand and files preview as usual, and a path that **does not
  exist** shows a dim **`[missing]`** tag instead of a tree. Empty input is
  a clean empty list.

  ```bash
  fd -e py | browse-fs              # '-' optional; or: git ls-files | browse-fs
  ```

- **`browse-git -`** — read **git output** from stdin and sniff its kind
  from the first non-blank line (color-agnostic), building the matching
  view:

  | Sniffed kind | Recognised by | Tree |
  | --- | --- | --- |
  | **diff** | a `diff --git` header or a bare `--- a/` hunk | an auto-expanded umbrella row (`diff: N files +X -Y`) over one row per file; its preview is a per-file `path \| +N -M` table with green adds / red removes plus the `N files changed …` summary, and each file row previews its delta-rendered block |
  | **log** | `commit <sha>` blocks (`--stat` / `-p` included) | one row per commit block; the whole block as the preview |
  | **status** | porcelain `XY path` lines (the `-z` form too) or the human `On branch …` / `HEAD detached …` prose | the status view's leaf rows |

  The slurped text is the only data source — git is never run, no repo is
  required, and the actions that would re-run git (the `` ` `` mode picker,
  the `t` graph toggle) flash instead of acting. Empty or unrecognised
  input is a clean error on stderr (exit 2) before the UI starts.

  ```bash
  git diff | browse-git             # '-' optional; also: git log -p | browse-git
  ```

> **`--tty -` interplay.** The framework's `--tty -` flag (run the UI over
> the std streams — see [cli.md](cli.md#--tty-tty_path)) consumes the `-`
> as *its own value*, so it is never read as the content-mode `-`. The `-`
> content modes are therefore unavailable under `--tty -`: there fd 0/1
> *are* the UI, leaving no free stdin to ingest. `browse-md --tty -` (etc.)
> falls back to the bare form (browse the cwd / repo).

## Content channels in your own recipe

A recipe can read stdin and write results to stdout while the UI runs on
its own terminal — the same mechanism the `-` modes above use:

- **Initial input** is plain `sys.stdin`, read in the recipe's setup
  *before* `Browser.run()` (slurp, iterate lines, or split records on the
  bytes layer), then build the tree from it. There is no framework API for
  this — and none is needed.
- **`ctx.print(text, end='\n')`** writes to the stdout content channel
  (mirrors builtin `print`): streamed live to a pipe, or delivered to
  scrollback at exit on a tty, never blocking the UI. `ctx.quit(code,
  output)` appends `output` to the same channel (strict FIFO — prints
  first).
- **`on_stdin`** (a `BrowserConfig` hook) consumes stdin **live** while the
  UI runs — raw chunks or framed records (`stdin_delimiter`), `str` or
  `bytes` (`stdin_want_bytes`) — folding each delivery into the tree via
  `ctx.update_data`.

The full contract (the `on_stdin` signature, record framing, EOF / error
shape, and how a synchronous read composes with streaming) is in
[api.md](api.md#content-channels-stdin--stdout).

## See also

- [api.md](api.md) — full Python API.
- [cli.md](cli.md) — CLI flags (also runnable from a recipe via
  `browse-tui --run-py …`).
- [../README.md](../README.md) — quickstart.
