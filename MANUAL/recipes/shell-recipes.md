# Lightweight shell-script recipes

These recipes are pure bash — each invoking the
`browse-tui` binary with `--root-cmd` / `--input` / `--preview-cmd` and
no Python. They are the minimum-viable demonstration that a useful TUI
can be built from CLI flags alone, and a starting point you can copy
and tweak for similar one-off pickers.

## `recipes/browse-files`

Single-directory file picker with preview. On Enter, prints the chosen
path on stdout (suitable for command-substitution: `cat "$(./recipes/browse-files /tmp)"`).

**Demonstrates:** `--root-cmd 'ls -1A DIR'` + `--input tsv --fields id`
+ `--preview-cmd` branching on file vs dir + `--print-format` shaping
the stdout result.

**Usage:**

```bash
./recipes/browse-files            # current directory
./recipes/browse-files /tmp       # any path
```

**Source:** [`recipes/browse-files`](../../recipes/browse-files)

## `recipes/browse-find`

Recursive directory-tree picker over `find -print0`. Each full path is
split on `/` so the flat `find` output nests into a real directory
tree. NUL-safe — handles paths with spaces or newlines correctly.
Extra arguments after the root path are forwarded straight to `find`.

**Demonstrates:** `--path-sep /` to build a tree from full paths +
`--record-sep null` for NUL-separated input + safe positional-argument
quoting via `printf %q` + a preview that branches on file vs dir.

**Usage:**

```bash
./recipes/browse-find                            # recurse from .
./recipes/browse-find /etc                       # recurse from /etc
./recipes/browse-find . -type f -name '*.py'     # extra args forwarded to find
```

**Source:** [`recipes/browse-find`](../../recipes/browse-find)

## `recipes/browse-ls`

`ls -lA` browser with mode / owner / size / date / name columns parsed
via a named-group regex.

**Demonstrates:** `--input 'match:REGEX'` with named groups becoming
Item attributes (the captured `mode`, `owner`, `size`, `date` are
exported as `$TUI_MODE`, `$TUI_OWNER`, etc. to any action commands).

**Usage:**

```bash
./recipes/browse-ls            # current directory
./recipes/browse-ls /etc       # any path
```

The regex is tuned for GNU `ls -lA`. BSD `ls` output may differ
slightly (link count column width, date format) — adapt the regex if
your `ls` produces different output.

**Source:** [`recipes/browse-ls`](../../recipes/browse-ls)

---

*[← All recipes](../recipes.md)*
