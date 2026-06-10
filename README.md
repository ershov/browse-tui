# browse-tui

Generic hierarchical browser TUI for shell pipelines and Python recipes.

`browse-tui` is a single-file, dependency-free Python 3 program that turns any
tree-shaped data source into a fast, keyboard-driven terminal UI — search,
filter, preview, multi-select, custom actions. Think `fzf`, but for hierarchies.

It's an *engine*: point it at a shell command, a Python callback, or a flat
list, and it handles the lazy tree, the preview pane, and the keymap. The
recipes below are what that engine looks like in practice.

## Shipped recipes

Run `./install-bin.sh` and a set of ready-made browsers lands on your `$PATH`,
each with a short `b-*` alias:

- **`b-git`** — `tig`, but generic. Browse commits, working-tree status,
  reflog, branches, and stashes; drill a commit into its changed files and into
  a colored diff. Native commit graph, ref/author/date chips on every row, and
  [`delta`](https://github.com/dandavison/delta)-powered diffs when `delta` is
  on your `$PATH`.
- **`b-claude`** — your Claude Code history as a tree: projects → sessions →
  individual messages, each one previewed as pretty-printed JSON. Jump straight
  to the source `.jsonl` in `$EDITOR`.
- **`b-md`** — any Markdown file as a navigable heading tree, previewed through
  md2ansi. `b-md README.md#install` deep-links straight to a section; point it
  at a directory to browse every `.md` inside.

…plus `b-fs` (filesystem browser with a live mtime watcher), `b-procs` (process
tree with a kill action), `b-mcp` (the tools exposed by any MCP server),
`b-plan`, and `b-jira` / `b-jira-mcp` — plus a few tiny pure-shell pickers
(`browse-find`, `browse-files`, `browse-ls`) to copy and tweak.

→ **[MANUAL/recipes.md](MANUAL/recipes.md)** documents every recipe and walks
through writing your own.

## Install

`browse-tui` is one executable Python file — no `pip`, no virtualenv. Python
3.8+ is the only requirement.

```bash
# Just the engine — copy the single file anywhere on your $PATH:
cp browse-tui ~/.local/bin/

# Batteries included — engine + the recipes + mdless/mdcat tools + b-* aliases:
./install-bin.sh     # copies into the first of ~/.local/bin, ~/bin, /usr/local/bin
./install-link.sh    # same, but symlinks back to this checkout (handy while hacking)
./uninstall.sh       # removes everything the installers placed
```

## Build your own

### From the shell — drill into a tree (lazy):

```bash
browse-tui \
  --children-cmd 'find "$TUI_ID" -mindepth 1 -maxdepth 1 -printf "%p\t%f\t%y\n"' \
  --fields id,title,kind \
  --root-id "$PWD" \
  --preview-cmd '[[ -d "$TUI_ID" ]] && ls -lA "$TUI_ID" || head -200 "$TUI_ID"' \
  --action 'e:Edit:$EDITOR "$TUI_ID"'
```

`$TUI_ID` is set per row; `$EDITOR` and the rest of the parent environment are
inherited unchanged. For a flat `fzf`-style list, skip the tree entirely:
`ls | browse-tui --root-cmd cat --input tsv --fields id` prints the selection to
stdout (exit 0; `q`/`Esc` cancels with exit 1).

### In Python — same idea, full API:

```python
#!/usr/bin/env -S browse-tui --run-py
import os
from browse_tui import Browser, Item, Action

def get_children(path, *, reload=False):
    path = path or os.getcwd()
    return [
        Item(id=os.path.join(path, n), title=n,
             has_children=os.path.isdir(os.path.join(path, n)))
        for n in sorted(os.listdir(path))
    ]

def edit(ctx):
    ctx.run_external([os.environ.get('EDITOR', 'vi'), ctx.cursor.id])

Browser(
    title='files',
    get_children=get_children,
    root_id=os.getcwd(),
    actions=[Action('e', 'Edit', edit, 'cursor')],
).run()
```

Make it executable and run it directly: `chmod +x my-recipe && ./my-recipe`.

## Default keys

| Key                        | Action                                  |
| -------------------------- | --------------------------------------- |
| `j`/`k`, arrows            | Cursor down/up                          |
| `g`/`G`, Home/End          | First/last item                         |
| PgDn/PgUp                  | Page down/up                            |
| Right/Left                 | Expand/collapse (or step in/out)        |
| Alt-Right/Left             | Recursive expand/collapse               |
| Alt-Down/Up                | Scope into / out of item                |
| `/` , `&`                  | Search · filter (stackable predicates)  |
| Space, Alt-Space           | Toggle select (down / up)               |
| Ctrl-A / Ctrl-N            | Select all / clear                      |
| Ctrl-P, Alt-P              | Toggle preview / children pane          |
| `R`                        | Toggle preview ANSI colours             |
| Shift-Up/Down, Alt-PgUp/Dn | Scroll preview (Shift-Home/End = ends)  |
| `-`/`=` , `\`              | Resize split · cycle layout (v/h/m/pc)  |
| `v` , `~`                  | Page preview in `$PAGER` · message log  |
| Ctrl-R / Ctrl-L            | Reload / redraw                         |
| `?` / F1                   | Help (full key list)                    |
| Enter                      | `--on-enter` (default: print + exit)    |
| `q`, Esc, Ctrl-C           | Quit (Ctrl-Z suspends)                  |

## Documentation

The user manual lives in **[`MANUAL/`](MANUAL/)**:

- **[cli.md](MANUAL/cli.md)** — every flag, every input format, worked examples.
- **[api.md](MANUAL/api.md)** — Python API: `Browser`, `Item`, `Action`, `Context`, `BrowserConfig`.
- **[recipes.md](MANUAL/recipes.md)** — every shipped recipe (`browse-git`, `browse-claude`, `browse-md`, …) and how to write your own.

For contributors, `docs/` holds [internals.md](docs/internals.md) (module
layout, threading model) and the [design
specs](docs/superpowers/specs/2026-04-30-browse-tui-design.md).

## License

Licensed under the Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
