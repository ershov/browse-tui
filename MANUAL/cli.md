# browse-tui — CLI Reference

Every flag, every input format, with worked examples.

```
USAGE
  browse-tui [OPTIONS]                         # TUI mode
  browse-tui SCRIPT [args…]                    # auto-detect recipe (same as --run)
  browse-tui --run     SCRIPT [args…]          # auto-detect by shebang/+x
  browse-tui --run-py  SCRIPT [args…]          # run as a Python recipe (in-process)
  browse-tui --run-cli SCRIPT [args…]          # exec the script (TUI_BIN exported,
                                               # browse-tui dir prepended to PATH)
```

`browse-tui` has three top-level modes:

1. **TUI mode** — `--children-cmd CMD` (lazy) or `--root-cmd CMD` (eager).
   Exactly one of these must be present.
2. **Recipe mode** — `--run SCRIPT`, `--run-py SCRIPT`, `--run-cli SCRIPT`,
   or a bare positional. The recipe must be the first argument; every arg
   after `SCRIPT` is forwarded to the recipe as its `sys.argv`.
3. **`--install`/`--uninstall`** — copy the binary in/out of standard paths;
   never enters TUI mode.

`--version`, `-h`/`--help`, `--command-log` are orthogonal — but only in TUI
mode. In recipe mode, no other browse-tui flags are accepted.

---

## Contents

- [Data sources](#data-sources)
  - [`-c, --children-cmd CMD`](#-c---children-cmd-cmd)
  - [`--root-id ID`](#--root-id-id)
  - [`-p, --preview-cmd CMD`](#-p---preview-cmd-cmd)
  - [`--root-cmd CMD`](#--root-cmd-cmd)
- [Input formats](#input-formats)
  - [Bare formats](#bare-formats)
  - [Prefix formats (parameterised)](#prefix-formats-parameterised)
  - [Fields](#fields)
  - [Record separator](#record-separator)
  - [Path separator](#path-separator)
  - [Worked examples per format](#worked-examples-per-format)
  - [Coercion](#coercion)
- [Actions](#actions)
  - [`-a, --action 'KEY:LABEL:CMD'`](#-a---action-keylabelcmd)
  - [`--action-timeout SECS`](#--action-timeout-secs)
  - [`--on-enter MODE`](#--on-enter-mode)
  - [`--print-format FMT`](#--print-format-fmt)
- [Layout / display](#layout--display)
  - [Layouts](#layouts)
- [Install / uninstall](#install--uninstall)
- [Recipe mode](#recipe-mode)
  - [Auto-detection rules (`--run` and bare positional)](#auto-detection-rules---run-and-bare-positional)
  - [Python recipes (`--run-py`)](#python-recipes---run-py)
  - [Binary recipes (`--run-cli`)](#binary-recipes---run-cli)
  - [Shebang trick](#shebang-trick)
- [Plugins](#plugins)
- [Default keybindings](#default-keybindings)
- [Environment variables](#environment-variables)
- [Debug / ops](#debug--ops)
- [Worked examples](#worked-examples)
  - [1. fzf-style flat selector](#1-fzf-style-flat-selector)
  - [2. Filesystem tree (lazy)](#2-filesystem-tree-lazy)
  - [3. /etc/passwd browser (eager, IFS-split)](#3-etcpasswd-browser-eager-ifs-split)
  - [4. find with NUL safety](#4-find-with-nul-safety)
  - [5. ls -l with named-regex](#5-ls--l-with-named-regex)
  - [6. Run a recipe directly](#6-run-a-recipe-directly)
  - [7. Install to ~/.local/bin](#7-install-to-localbin)
  - [8. Action that re-invokes browse-tui (recursive)](#8-action-that-re-invokes-browse-tui-recursive)
- [See also](#see-also)

## Data sources

### `-c, --children-cmd CMD`

Lazy mode. `CMD` is a bash command run for every parent the user expands; its
stdout is parsed per `--input` and the resulting rows become the children of
that parent. The id of the parent being expanded is exposed as `$TUI_ID`.

```bash
browse-tui \
  --children-cmd 'find "$TUI_ID" -mindepth 1 -maxdepth 1 -printf "%p\t%f\n"' \
  --root-id "$PWD"
```

For the very first call `$TUI_ID` is `--root-id` (default: empty string).
When the command exits non-zero, the parent is treated as having no children
(no error popup; details surface in the preview pane only if `--preview-cmd`
is also wired).

### `--root-id ID`

Initial id passed as `$TUI_ID` to `--children-cmd`. Defaults to the empty
string. Pick whatever your `--children-cmd` knows how to handle when fed the
root.

### `-p, --preview-cmd CMD`

Bash command run for the preview pane. The id of the cursor item is `$TUI_ID`.
Stdout (utf-8, errors replaced) is shown verbatim in the preview pane. The
preview worker is latest-wins and waits for the cursor to settle (~0.2s)
before running `CMD`, so a held `j`/`k` coalesces to one run for the row the
cursor lands on. While that run is outstanding the pane keeps the previously
shown preview rather than blanking, and the pane label reads `⧗ Preview`;
both revert once the new output arrives. Already-visited rows swap the same
way — only once the cursor settles — but from the cached output, without
re-running `CMD`. (The settle delay is the `preview_debounce` knob — see
[api.md](api.md); recipes can tune or disable it, there is no CLI flag.)

```bash
--preview-cmd '[[ -d "$TUI_ID" ]] && ls -lA "$TUI_ID" || head -200 "$TUI_ID"'
```

### `--root-cmd CMD`

Eager mode. `CMD` runs once at startup; its stdout is parsed per `--input` and
the entire result becomes the tree. Hierarchy is detected from the parsed
records:

- If any record has a `parent` field, it's used as a parent pointer (rows
  with no `parent` go under `--root-id`).
- Else if any record has a `depth` field, the tree is built by depth-coding
  (each row at depth `d+1` is a child of the most recent row at depth `d`).
- Else if `--path-sep CHARS` is given, each row's `id` is split on `CHARS`
  to synthesize a tree (see `--path-sep` below).
- Else the rows are flat children of `--root-id`.

Precedence is `parent > depth > path-split > flat` — a more explicit
structural column always wins. If `--path-sep` is given but the rows also
carry an explicit `parent`/`depth` column, the flag is ignored and
`browse-tui: --path-sep ignored: rows carry explicit parent/depth` is
printed to stderr.

Special case: `--root-cmd -` reads stdin verbatim (no subprocess) — the
canonical spelling for "the root list comes straight from stdin". Bare
`--root-cmd cat` is kept as an alias for it (exactly `cat`; `--root-cmd
'cat file'` still runs as a command). The UI is painted to the terminal
device (`--tty`, default `/dev/tty`), not to `stdin`/`stdout`, so keyboard
input keeps working after stdin is consumed and `stdout` stays free for the
selection — see [Terminal & capturable result](#terminal--capturable-result).

```bash
# Pipe-in friendly:
ls | browse-tui --root-cmd - --input tsv --fields id

# Or run a real command:
browse-tui --root-cmd 'cat /etc/passwd' --input ifs:: \
           --fields user,_,uid,gid,gecos,home,shell
```

`--children-cmd` and `--root-cmd` are mutually exclusive — pick one. With
neither, `browse-tui` prints `error: --children-cmd or --root-cmd is required`
and exits with code 2.

---

## Input formats

`--input FMT` selects the parser. Default is `tsv`.

### Bare formats

| `FMT`        | What it parses                                                  |
| ------------ | --------------------------------------------------------------- |
| `tsv`        | Tab-separated, one record per line. Fields by `--fields`.       |
| `csv`        | RFC 4180 CSV, one record per line. Fields by `--fields`.        |
| `json`       | One JSON object per record. Keys = fields directly.             |
| `json-array` | Whole input is one JSON array. `--record-sep` is ignored.       |

### Prefix formats (parameterised)

| `FMT`                | Meaning                                                          |
| -------------------- | ---------------------------------------------------------------- |
| `ifs:CHARS`          | Bash-IFS-style split. Whitespace IFS collapses runs.             |
| `split:REGEX`        | `re.split(REGEX, line)` — awk-style, e.g. `split:\s+`.           |
| `match:REGEX`        | `re.match(REGEX, line)`; named groups become fields.             |

### Fields

`--fields LIST` is a comma-separated list of column names for `tsv`, `csv`,
`ifs`, and `split`. Default is `id,title`. Standard names: `id`, `title`,
`tag`, `tag_style`, `has_children`. Any other name lands as an arbitrary
attribute on the resulting `Item` (and is exported as `TUI_<NAME>` to action
commands; see Environment below). Columns past `len(fields)` are dropped.

`match:REGEX` ignores `--fields` — the named groups define the field mapping
directly.

### Record separator

`--record-sep nl` (default), `null`, or any literal byte sequence (UTF-8
encoded).

```bash
# find -print0 → NUL records:
find . -maxdepth 3 -print0 | browse-tui --root-cmd cat \
  --record-sep null --fields id
```

`json-array` ignores `--record-sep` (the whole input is a single value).

### Path separator

`--path-sep CHARS` splits each parsed row's `id` on `CHARS` and synthesizes
a tree, creating intermediate prefix nodes as needed. Eager `--root-cmd`
only; combining it with `--children-cmd` is an error
(`error: --path-sep requires --root-cmd (eager mode)`, exit 2). It is
orthogonal to `--input`/`--fields` — the parser produces rows as usual,
then the `id` column is split. `CHARS` may be multiple characters (e.g.
`::`). Node ids are full prefix paths; a leaf's title is its last segment
(an explicit `title` column wins), and metadata columns ride onto the leaf.

Empty segments are handled path-aware: a leading separator is preserved
(`/etc/passwd` → `/etc` › `/etc/passwd`, so absolute ≠ relative), doubled
separators collapse (`a//b` → `a` › `a/b`), a trailing separator is ignored
(`a/b/` → `a` › `a/b`), and empty / all-separator entries are skipped.

```bash
# plain path list → tree
find . | browse-tui --root-cmd cat --path-sep /

# metadata columns ride onto leaves
find . -printf '%p\t%s\n' | browse-tui --root-cmd cat --fields id,size --path-sep /

# non-/ separator (qualified names)
printf 'os.path.join\nos.path.split\n' | browse-tui --root-cmd cat --path-sep .
```

See `--root-cmd` above for how `--path-sep` slots into hierarchy detection
(`parent > depth > path-split > flat`).

### Worked examples per format

```bash
# tsv (the default)
printf 'a\tApple\nb\tBanana\n' | browse-tui --root-cmd cat

# csv
printf 'id,title\n"a,b","quoted, comma"\n' | \
  browse-tui --root-cmd cat --input csv

# json (ndjson)
printf '{"id": "a", "title": "Apple"}\n{"id": "b"}\n' | \
  browse-tui --root-cmd cat --input json

# json-array
printf '[{"id":"a"},{"id":"b","tag":"t","tag_style":"green"}]' | \
  browse-tui --root-cmd cat --input json-array

# ifs::  — colon-only IFS, like /etc/passwd
browse-tui --root-cmd 'cat /etc/passwd' --input ifs:: \
           --fields user,_,uid,gid,gecos,home,shell

# ifs:" \t"  — whitespace IFS (collapses runs)
browse-tui --root-cmd 'echo "  alpha  beta   gamma  "' --input 'ifs: \t'

# split:\s+ — awk-style
browse-tui --root-cmd 'echo "  alpha  beta   gamma "' --input 'split:\s+'

# match:^\s*(?P<id>\S+)\s+(?P<size>\d+)\s+(?P<title>.+)$
ls -l | browse-tui --root-cmd cat \
  --input 'match:^\S+\s+\d+\s+\S+\s+\S+\s+(?P<size>\d+)\s+\S+\s+\S+\s+\S+\s+(?P<id>.+)$'
```

### Coercion

The `has_children` field is coerced from string to bool (`'1'`, `'true'`,
`'yes'`, `'y'`, `'on'` → True; anything else → False; case-insensitive). All
other fields stay strings unless your recipe overrides via the API.

---

## Actions

### `-a, --action 'KEY:LABEL:CMD'`

Register a custom keybinding. Repeatable. Splits on the first two colons —
the `CMD` may contain colons freely (paths, sed expressions, URLs).

```bash
--action 'e:Edit:$EDITOR "$TUI_ID"'
--action 'd:Delete:rm -ri "$TUI_ID"'
--action 'g:git log:git log --oneline "$TUI_ID" | head -50 | less'
```

`CMD` runs as `bash -c CMD` with the parent environment inherited. The
following extra variables are set:

| Variable          | Value                                                   |
| ----------------- | ------------------------------------------------------- |
| `TUI_ID`          | id of the primary target (cursor or first selected)     |
| `TUI_TITLE`       | title of primary target                                 |
| `TUI_TAG`         | tag of primary target                                   |
| `TUI_TAG_STYLE`   | tag style of primary target                             |
| `TUI_HAS_CHILDREN`| `1` or `0`                                              |
| `TUI_<ATTR>`      | every recipe-set attribute (uppercased identifier)      |
| `TUI_IDS_FILE`    | path to a NUL-separated file with every target id       |
| `TUI_IDS_COUNT`   | number of target ids                                    |
| `TUI_TARGETS`     | `cursor` or `selection` (which set drove the action)    |
| `TUI_BIN`         | absolute path of the running `browse-tui` binary        |

`TUI_BIN`, `TUI_IDS_FILE`, `TUI_IDS_COUNT`, and `TUI_TARGETS` are reserved —
recipe-set item attributes with those names cannot clobber them.

After the command exits, the affected subtree is refreshed automatically.
Non-zero exit codes surface in the info bar via `ctx.error`.

```bash
# Read all selected ids in bash (NUL-safe):
--action 'b:Bulk delete:while IFS= read -rd $"\0" id; do rm "$id"; done < "$TUI_IDS_FILE"'

# Or use xargs -0:
--action 'b:Bulk:xargs -0 -I{} echo {} < "$TUI_IDS_FILE"'
```

### `--action-timeout SECS`

Per-action timeout in seconds; default 600. On timeout the action returns the
GNU-timeout convention (124).

### `--on-enter MODE`

What pressing Enter does:

| `MODE`        | Behaviour                                                    |
| ------------- | ------------------------------------------------------------ |
| `print-exit`  | (default) Print the formatted target ids to stdout, exit 0.  |
| `action:KEY`  | Invoke the action bound to `KEY` (must be registered).       |
| `noop`        | Do nothing — long-running browse mode.                       |

### `--print-format FMT`

Format string used by `print-exit` mode. Uses `str.format`-style placeholders
over Item attributes (the dataclass fields plus any extras). Default is
`{id}`. One target = one line.

```bash
--print-format '{id} {title}'
--print-format '{id}\t{tag}'      # (you'll need shell quoting)
```

When a placeholder doesn't resolve, `browse-tui` falls back to the bare id.

---

## Layout / display

| Flag                  | Effect                                                       |
| --------------------- | ------------------------------------------------------------ |
| `--no-preview`        | Start with the preview pane hidden. Toggle live with Ctrl-P. |
| `--no-children-pane`  | Start with the children-grid pane hidden.                    |
| `--preview-ansi` / `--no-preview-ansi` | Honour ANSI SGR colour codes in the preview pane (default on). Other escape sequences are stripped either way. Toggle live with capital `R`. |
| `--no-multi-select`   | Disable the selection set (Space/Alt-Space/Ctrl-A become no-ops). |
| `--list-size N\|N%`    | Initial list pane size. `N` is a line count (proportional to startup terminal — scales on resize); `N%` locks the proportion. Default `30%`. Adjust live with `-`/`_` (shrink) and `=`/`+` (grow); the ratio is preserved across terminal resizes. |
| `--split-type TYPE`   | Initial pane layout. Accepts `h`/`horizontal`, `v`/`vertical`, `m`/`mixed`, `pc`/`preview-children`, or `a`/`auto` (default). `auto` picks `v` when terminal width >= 230 columns, else `h`; resolved once at startup and not recomputed on resize. Switch live with `\` (cycles `v`→`h`→`m`→`pc`) or Alt-1/2/3/4. See [Layouts](#layouts) below. |
| `--show-ids MODE`     | Whether to render the id segment in front of each row's title: `always` / `auto` (default) / `never`. In `auto` mode the id is shown only when it is a scalar (`str`/`int`) differing from the title — for line-based CLI sources (filenames, `seq`, `xargs`) an id equal to the title would just be duplication and is suppressed; a `--python` recipe's structured ids (tuples/objects) are routing state and are never shown. |
| `--title TITLE`       | Window title shown in the info bar.                          |
| `--initial-scope ID`  | Start scoped to this id (Alt-Up to leave).                   |
| `--scope-crumb`       | Show the scope drill-down crumb (`▸ a ▸ b …`) in the info bar. Off by default — ids can be long (file paths, jsonl paths) and the crumb eats horizontal space. |

### Layouts

Four pane layouts are available; switch live with `\` (cycle) or Alt-1/2/3/4
(direct select). All layouts include a single-line info bar across the bottom.

```
Vertical (`v`, Alt-1):           Horizontal (`h`, Alt-2):
+------+--------------+          +----------------+
|      |   children   |          |      list      |
| list +--------------+          +----------------+
|      |              |          |    children    |
|      |   preview    |          +----------------+
+------+--------------+          |    preview     |
|      info bar       |          +----------------+
+---------------------+          |    info bar    |
                                 +----------------+

Mixed (`m`, Alt-3):              Preview-children (`pc`, Alt-4):
+----------+----------+          +------+--------------+
|   list   |          |          |      |   children   |
+----------+ preview  |          | list +--------------+
| children |          |          |      |   preview    |
+----------+----------+          +------+--------------+
|      info bar       |          |      info bar       |
+---------------------+          +---------------------+
```

* **Horizontal** — list, children, preview stacked vertically. Best for
  narrow terminals; the historical default.
* **Vertical** — list on the left; children-above-preview on the right
  (children gets ~25% of the right-side height). Best for wide terminals.
* **Mixed** — list and children stacked on the left; preview spans the
  full height on the right. Children gets ~25% of the left-side height.
* **Preview-children** — currently the same shape as Vertical; reserved
  as a distinct slot for future tweaks (and may be consolidated later).

---

## Terminal & capturable result

`browse-tui` paints the UI to — and reads keys from — a dedicated **terminal
device**, keeping the process's `stdin`/`stdout` free for content and results.
That separation is what makes the selection cleanly capturable.

### `--tty TTY_PATH`

Terminal device for the UI. Default `/dev/tty` (the controlling terminal).
The UI is drawn to, and keys are read from, this device while `stdin`/`stdout`
stay free for piped content and the printed result.

| `TTY_PATH`     | Behaviour                                                       |
| -------------- | --------------------------------------------------------------- |
| `/dev/tty`     | (default) Open the controlling terminal `O_RDWR`. `stdin`/`stdout` untouched. |
| *device path*  | A specific terminal, e.g. `--tty /dev/pts/3`. Opened the same way. |
| `-`            | Run the UI over the process's std streams (fd 0 in, fd 1 out), which must themselves be a terminal. The escape hatch for environments with no openable `/dev/tty`. |

The flag works for **both** the CLI and Python recipes. Recipes get it for
free: `Browser.run()` auto-detects `--tty TTY_PATH` / `--tty=TTY_PATH` in
`sys.argv` and forwards the value to the terminal layer, so
`./my-recipe --tty -` works without the recipe wiring its own argparse. A
recipe that does argparse `--tty` itself (stripping it before `run()`) is
unaffected.

### Capturable result

Because the UI lives on the terminal device (default `/dev/tty`) and never on
`stdout`, `stdout` carries **only** the `print-exit` result — zero escape
sequences. A command substitution captures just the selection:

```bash
# $sel is exactly the chosen id(s) — no UI bytes leak into the capture:
sel=$(ls | browse-tui --root-cmd cat --input tsv --fields id)

# Same for a redirect:
ls | browse-tui --root-cmd cat --input tsv --fields id > picked.txt
```

The UI is fully interactive on the terminal the whole time; the result is
delivered on `stdout` only when Enter fires `print-exit` (the default
`--on-enter`; see [`--on-enter`](#--on-enter-mode) and
[`--print-format`](#--print-format-fmt) for the result formatting).

### Content channels: stdin in, stdout out

With the UI on its own terminal device, the process's `stdin` and `stdout`
are free to carry **content** — input piped in, results printed out — while
the UI runs. `stderr` is left untouched throughout (an escape hatch for
ad-hoc recipe diagnostics; uncaught tracebacks still reach it).

**Reading stdin.** `--root-cmd -` (and the `cat` alias) reads the root list
directly from stdin, parsed per `--input` / `--fields` exactly as a command's
output would be — see [`--root-cmd`](#--root-cmd-cmd). The read happens once,
before the UI opens:

- A **pipe or file** is slurped and parsed up front.
- A **tty** blocks until you end input with `^D`, exactly like `cat` —
  standard unix, not special-cased. (Type your rows, then `^D`.)
- stdin is **one-shot**: it cannot be re-read. `Ctrl-R` reload re-serves the
  already-parsed in-memory data — the same as eager `--root-cmd`, which does
  not re-run its command on reload either.

Once the UI is up, a tty stdin is detached (reads return immediate EOF) so a
mid-session read can never steal your keystrokes.

**Printing to stdout.** The `print-exit` selection is written to `stdout`
when Enter fires (above). A recipe may also emit its own text through the
`Context.print` API; from the CLI user's side the delivery is what matters:

- **Piped / redirected stdout** (`| consumer`, `> file`): output is
  **streamed live** as the consumer keeps up — a slow or backpressuring
  reader never blocks the UI.
- **A terminal stdout** (a bare interactive run): output is **held for the
  whole session and delivered to normal scrollback at exit**, after the UI
  closes — the `fzf` model, so a bare run still prints its result where you'd
  expect. (browse-tui's own selection prints last.)
- **Consumer gone** (it closed its read end — `EPIPE`): output simply stops;
  the UI stays alive on its terminal and later prints are dropped.

In every case the order is strict FIFO: anything printed during the session
arrives before the final `print-exit` / quit output.

(The Python-side contract — the `Context.print` and streaming `on_stdin`
APIs a recipe author wires up — is in
[api.md](api.md#output--ctxprint) and [api.md](api.md#streaming-input--on_stdin).)

### No controlling terminal

With no openable `/dev/tty` and no `--tty -` (cron, daemons, detached
sessions), `browse-tui` exits non-zero with a clean one-line error on stderr —
no Python traceback:

```
browse-tui: no controlling terminal; pass --tty - to run over stdin/stdout
```

`--tty -` is the escape hatch. But the std streams it adopts must be a real
terminal: pointing `--tty -` at piped/redirected std streams exits 1 with

```
browse-tui: not a terminal
```

(An explicit device path that cannot be opened fails the same clean way:
`browse-tui: cannot open terminal '<path>'`.)

### `--tty -` limitation: no interactive pager

In `--tty -` mode the UI rides on `stdin`/`stdout`, so there is **no separate
terminal** for a pager or editor to read keys from while its stdin carries
text — the pager paths (`v` view preview, `~` view the message log, the
`Context.page` API) and shell-outs to `$EDITOR` have no private `/dev/tty`.
browse-tui does not special-case this: it runs `$PAGER`/`$EDITOR` normally
(bracketed by suspend/resume so the screen stays safe), so configure them for
non-interactive use if needed. Prefer the default `/dev/tty` (or an explicit
device path) whenever one is available.

---

## Install / uninstall

`--install TARGET` copies the running binary; `--uninstall TARGET` removes it.

| `TARGET`  | Path                              |
| --------- | --------------------------------- |
| `local`   | `./browse-tui`                    |
| `user`    | `~/.local/bin/browse-tui`         |
| `system`  | `/usr/local/bin/browse-tui`       |
| `env`     | `$VIRTUAL_ENV/bin/browse-tui`     |

Behaviour:

- Identical destination → silent no-op (exit 0).
- Different destination → refused unless `--force` (exit 2).
- `system` target without root → prints a `sudo cp …` hint and exits 3.
- `env` target without `$VIRTUAL_ENV` set → exits 1 with an error.

Install / uninstall mode never enters the TUI; the action runs and exits.

```bash
./browse-tui --install user
./browse-tui --uninstall user
./browse-tui --install user --force   # overwrite differing binary
```

---

## Recipe mode

`browse-tui` can run a recipe — either a Python script that builds a
`Browser` directly, or any executable file (typically a shell script
that wraps `browse-tui` with a particular configuration). Pick the
right form for the job:

| Form                          | Use                                                  |
| ----------------------------- | ---------------------------------------------------- |
| `browse-tui SCRIPT [args…]`   | Auto-detect (same as `--run`).                       |
| `browse-tui --run SCRIPT …`   | Auto-detect by shebang / executable bit.             |
| `browse-tui --run-py SCRIPT …`| Force Python; runs in-process via `runpy`.           |
| `browse-tui --run-cli SCRIPT …`| Force exec; replaces the process with the script.   |

**Recipe must be the first argument.** No other browse-tui flag is
accepted in recipe mode — every arg after `SCRIPT` becomes the recipe's
`sys.argv`. If you need `browse-tui`-level configuration, put it inside
the recipe code (Python recipes pass kwargs to `Browser`; binary recipes
build their own command line for the `browse-tui` they exec).

```bash
browse-tui recipes/browse-fs ~                 # bare positional → --run
browse-tui --run-py ./my-recipe.py --my-flag v # explicit Python recipe
browse-tui --run-cli ./wrapper.sh ~/projects   # explicit exec
```

### Auto-detection rules (`--run` and bare positional)

1. If the file's first line is a shebang containing the word `python`
   (matched at word boundaries — `/opt/cpython/...` does not match),
   it's run as a Python recipe via `--run-py`. Executable bit is not
   required: `runpy` runs the file in-process.
2. Otherwise, if the file is executable, it's `exec`'d via `--run-cli`.
3. Otherwise — error. Use `chmod +x SCRIPT`, or pass `--run-py` /
   `--run-cli` explicitly.

### Python recipes (`--run-py`)

The running binary self-injects into `sys.modules['browse_tui']` before
the recipe executes, so the recipe can `from browse_tui import Browser,
Item, Action` with no install. `sys.argv` is rewritten to
`[script, *recipe_args]` and the recipe runs in the same Python process
as the binary itself.

### Binary recipes (`--run-cli`)

The recipe is `exec`'d, replacing the browse-tui process. Two
environment knobs are set up first:

* `TUI_BIN` is set to the absolute path of the running binary.
* The directory containing the binary is prepended to `PATH` so a
  recipe that calls `browse-tui` by name resolves to *this* build —
  handy when running from a build tree without installing.

A typical bash recipe uses `${TUI_BIN:-$(command -v browse-tui)}` to
invoke the binary deterministically.

### Shebang trick

Recipes can be made directly executable with the env-trick shebang:

```python
#!/usr/bin/env -S browse-tui --run-py
from browse_tui import Browser, Item, Action
…
```

`-S` lets `env` parse multiple args; the kernel runs
`browse-tui --run-py /path/to/script ARGS…`. Make the file `chmod +x`
and run it directly. This is how the shipped `recipes/browse-fs`,
`recipes/browse-plan`, `recipes/browse-claude`, `recipes/browse-git`,
`recipes/browse-jira`, and `recipes/browse-procs` work.

In recipe mode the data-source flags (`--children-cmd`, `--root-cmd`,
`--input`, …) are not accepted on the binary — the recipe is fully in
charge of how the Browser is configured.

---

## Plugins

`--plugin SPEC` loads a plugin at launch — SPEC is a module name or a
filesystem path, and the flag is repeatable. Plugins are ordinary Python
modules that extend the framework or a recipe (registering a preview
formatter, taking lifecycle callbacks via `register_plugin(PluginConfig(...))`,
and so on); a recipe can also load one with a plain `import`. See the
[Plugin system](api.md#plugin-system) section of the API reference for the
hook surface and module-discovery rules.

---

## Default keybindings

| Key            | Action                                                      |
| -------------- | ----------------------------------------------------------- |
| `j` / Down     | Cursor down                                                 |
| `k` / Up       | Cursor up                                                   |
| `g` / Home     | First item                                                  |
| `G` / End      | Last item                                                   |
| PgUp           | Page up                                                     |
| PgDn           | Page down                                                   |
| Right          | Expand node, or step into first child if already expanded   |
| Left           | Collapse node, or jump to parent                            |
| Alt-Right      | Expand siblings recursively                                 |
| Alt-Left       | Collapse siblings recursively                               |
| Alt-Down       | Scope into cursor item                                      |
| Alt-Up         | Scope out                                                   |
| Ctrl-P         | Toggle preview pane                                         |
| Alt-P          | Toggle children-grid pane                                   |
| `R`            | Toggle preview ANSI colours (override `--preview-ansi`)     |
| Shift-Down     | Scroll preview down by 1 line                               |
| Shift-Up       | Scroll preview up by 1 line                                 |
| Alt-PgDn       | Scroll preview down by a page                               |
| Alt-PgUp       | Scroll preview up by a page                                 |
| Shift-Home     | Scroll preview to top                                       |
| Shift-End      | Scroll preview to bottom                                    |
| `-` / `_`      | Shrink list pane (no-op when preview hidden)                |
| `=` / `+`      | Grow list pane (no-op when preview hidden)                  |
| `\`            | Cycle pane layout (`v`→`h`→`m`→`pc`→`v`)                    |
| Alt-1          | Switch to vertical layout (`v`)                             |
| Alt-2          | Switch to horizontal layout (`h`)                           |
| Alt-3          | Switch to mixed layout (`m`)                                |
| Alt-4          | Switch to preview-children layout (`pc`)                    |
| Space          | Toggle selection of cursor; advance cursor                  |
| Alt-Space      | Toggle selection of cursor; move cursor up                  |
| Ctrl-A         | Select all visible normal rows                              |
| Ctrl-N         | Clear the selection                                         |
| Ctrl-R         | Reload (refresh entire tree)                                |
| Ctrl-L         | Force redraw                                                |
| `v`            | View cursor item's preview in `$PAGER` (default `less -R`)  |
| `e`            | Edit cursor item's preview in `$EDITOR` (default `vi`)      |
| `~`            | Page the session message log in `$PAGER`                    |
| `/`            | Enter search mode                                           |
| `&`            | Enter filter mode (stacking; less-style)                    |
| Enter          | In search mode → next match; in filter mode → commit (or clear-all on empty); otherwise → `--on-enter` |
| Shift-Enter    | In search mode → previous match                             |
| Ctrl-X         | In filter mode → clear all filters and exit                 |
| Esc            | Exit search/filter mode (cancel current edit), else quit    |
| `?` / F1       | Toggle help screen                                          |
| `q`            | Quit (exit code 1)                                          |
| Ctrl-C         | Quit (exit code 1)                                          |
| Ctrl-Z         | Suspend (resume with `fg`)                                  |

Recipes can override any of these via `--action` (CLI) or
`Action(key, …)` (Python). The custom binding wins.

The default `e` opens the preview in `$EDITOR` against a tempfile and
**discards** any changes when the editor exits — there is no
cross-cutting save hook because the storage model varies per recipe
(filesystem path, MCP tool call, plan ticket id, …). Recipes that want
edits to persist override `e` with a handler that writes the buffer
back to its data source. Both `v` and `e` write the preview as UTF-8
with `surrogateescape` so non-printable bytes (control chars, lone
surrogates from filesystem reads) round-trip faithfully — no
question-mark replacements.

`v` and `e` work whether the preview pane is visible or not. The
preview text comes from the cache when present; on cache miss the
recipe's `--preview-cmd` (or `Browser.get_preview` callable) is
invoked synchronously and the result is cached. If the recipe has no
preview source at all, an info message is shown and no external
process is launched.

In **search mode** (after `/`), every printable key extends the query and the
cursor jumps to the nearest match in real time. Backspace trims; Esc cancels.

In **filter mode** (after `&`), every printable key extends the live filter
and the visible list narrows in real time using match-promotes-ancestors
semantics (a non-matching parent stays visible if any descendant matches).
Enter commits the live filter onto the stack; the next `&` adds another
predicate (AND-stacked). Enter on an empty filter clears all filters.
Ctrl-X clears all filters from inside the prompt. Backspace trims the
in-progress entry; Ctrl-U clears just the in-progress entry; Ctrl-W kills
the last word. Non-overridden keys (arrows, page keys, scope, …) fall
through to normal navigation while the prompt stays open.

The filter narrows the list to rows that match across what's currently
visible. Collapsed parents are evaluated on their own text alone — the
filter does not look inside collapsed subtrees, so it never triggers a
deep fetch. Expand a parent and the newly-revealed rows are filtered as
they appear. See
`docs/superpowers/specs/2026-05-27-filter-visible-tree-only-design.md`
for the evaluator rationale, and
`docs/superpowers/specs/2026-05-17-filter-design.md` for the keybindings
and recipe API.

**Mouse:** when running in a terminal that supports SGR mouse reporting,
left-click on a list row positions the cursor there. The wheel scrolls
the pane under the mouse: 3 lines per notch on the list (cursor stays
put — viewport-decoupled), or on the preview pane (same channel as
Shift-Up / Shift-Down). The next cursor-moving key snaps the list
viewport so the cursor is visible again. Click on the preview while the
help screen is up dismisses it. Mouse events are ignored in search /
insert / picker modes.

---

## Environment variables

| Variable | Read by                                                                         |
| -------- | ------------------------------------------------------------------------------- |
| `EDITOR` | Default `e` action (edit preview); recipes that template `$EDITOR` in actions; `Context.run_external` callers. |
| `PAGER`  | Default `v` action (view preview); fallback when neither `bat` nor `batcat` is on PATH (used by `Context.page`). |
| `VIRTUAL_ENV` | Required for `--install env`.                                              |

`browse-tui` does not read or write any other environment variables.
Action commands inherit the parent environment unchanged, plus the `TUI_*`
overlay listed above.

---

## Debug / ops

| Flag             | Effect                                                                 |
| ---------------- | ---------------------------------------------------------------------- |
| `--command-log`  | Show command log on quit (informational; no behavioural change).       |
| `--version`      | Print version (`0.1.0`) and exit 0.                                    |
| `-h`/`--help`    | Print the argparse help summary and exit 0.                            |

---

## Worked examples

### 1. fzf-style flat selector

```bash
ls | browse-tui --root-cmd cat --input tsv --fields id | xargs cat
```

`browse-tui` reads stdin (via `--root-cmd cat`), shows a one-column flat list,
and prints the chosen id on Enter.

### 2. Filesystem tree (lazy)

```bash
browse-tui \
  --children-cmd 'find "$TUI_ID" -mindepth 1 -maxdepth 1 -printf "%p\t%f\t%y\n"' \
  --fields id,title,kind \
  --root-id "$PWD" \
  --preview-cmd '[[ -d "$TUI_ID" ]] && ls -lA "$TUI_ID" || head -200 "$TUI_ID"' \
  --action 'e:Edit:$EDITOR "$TUI_ID"' \
  --action 'd:Delete:rm -ri "$TUI_ID"'
```

### 3. /etc/passwd browser (eager, IFS-split)

```bash
browse-tui --root-cmd 'cat /etc/passwd' \
           --input ifs:: \
           --fields user,_,uid,gid,gecos,home,shell
```

### 4. find with NUL safety

```bash
find . -maxdepth 3 -print0 \
  | browse-tui --root-cmd cat --record-sep null --fields id
```

### 5. ls -l with named-regex

```bash
browse-tui --root-cmd 'ls -lA' \
           --input 'match:^(?P<mode>\S+)\s+\d+\s+(?P<owner>\S+)\s+\S+\s+(?P<size>\d+)\s+(?P<date>\S+\s+\S+\s+\S+)\s+(?P<id>.+)$'
```

### 6. Run a recipe directly

```bash
# Auto-detect by shebang (Python recipe runs in-process via runpy):
browse-tui recipes/browse-fs ~
# Force mode (in-process Python):
browse-tui --run-py recipes/browse-fs ~
# Or via the recipe's own shebang:
./recipes/browse-fs ~
```

### 7. Install to ~/.local/bin

```bash
browse-tui --install user
```

### 8. Action that re-invokes browse-tui (recursive)

```bash
browse-tui \
  --children-cmd 'ls "$TUI_ID"' \
  --root-id /tmp \
  --action 'd:Drill:"$TUI_BIN" --children-cmd "ls \"\$TUI_ID\"" --root-id "$TUI_ID"'
```

`$TUI_BIN` always points at the currently-running binary, so recipes can spawn
sub-views with no install dependency.

---

## See also

- [api.md](api.md) — Python API for richer recipes.
- [recipes.md](recipes.md) — shipped recipes and how to write your own.
- [docs/internals.md](../docs/internals.md) — module layout and threading model.
- [README.md](../README.md) — quickstart.
