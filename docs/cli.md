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
preview worker is latest-wins: rapid cursor moves coalesce to one in-flight
fetch.

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
- Else the rows are flat children of `--root-id`.

Special case: `--root-cmd cat` reads stdin verbatim (no subprocess). After
consuming stdin, the binary reopens stdin from `/dev/tty` so keyboard input
still works.

```bash
# Pipe-in friendly:
ls | browse-tui --root-cmd cat --input tsv --fields id

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
| `--no-multi-select`   | Disable the selection set (Space/Alt-Space/Ctrl-A become no-ops). |
| `--show-ids MODE`     | Whether to render the id segment in front of each row's title: `always` / `auto` (default) / `never`. In `auto` mode the id is suppressed when `str(item.id) == item.title` — useful for line-based CLI sources (filenames, `seq`, `xargs`) where showing both is duplication. |
| `--title TITLE`       | Window title shown in the info bar.                          |
| `--initial-scope ID`  | Start scoped to this id (Alt-Up to leave).                   |

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
| Shift-Down     | Scroll preview down by 1 line                               |
| Shift-Up       | Scroll preview up by 1 line                                 |
| Alt-PgDn       | Scroll preview down by a page                               |
| Alt-PgUp       | Scroll preview up by a page                                 |
| Space          | Toggle selection of cursor; advance cursor                  |
| Alt-Space      | Toggle selection of cursor; move cursor up                  |
| Ctrl-A         | Select all visible normal rows                              |
| Ctrl-N         | Clear the selection                                         |
| Ctrl-R         | Reload (refresh entire tree)                                |
| Ctrl-L         | Force redraw                                                |
| `/`            | Enter search mode                                           |
| Enter          | In search mode → next match; otherwise → `--on-enter`       |
| Shift-Enter    | In search mode → previous match                             |
| Esc            | Exit search mode (in search), else quit                     |
| `?` / F1       | Toggle help screen                                          |
| `q`            | Quit (exit code 1)                                          |
| Ctrl-C         | Quit (exit code 1)                                          |
| Ctrl-Z         | Suspend (resume with `fg`)                                  |

Recipes can override any of these via `--action` (CLI) or
`Action(key, …)` (Python). The custom binding wins.

In **search mode** (after `/`), every printable key extends the query and the
cursor jumps to the nearest match in real time. Backspace trims; Esc cancels.

---

## Environment variables

| Variable | Read by                                                                         |
| -------- | ------------------------------------------------------------------------------- |
| `EDITOR` | Recipes that template `$EDITOR` in actions; `Context.run_external` callers.     |
| `PAGER`  | Fallback when neither `bat` nor `batcat` is on PATH (used by `Context.page`).   |
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

- [docs/api.md](api.md) — Python API for richer recipes.
- [docs/recipes.md](recipes.md) — `browse-fs`, `browse-plan`, `browse-claude`.
- [docs/internals.md](internals.md) — module layout and threading model.
- [README.md](../README.md) — quickstart.
