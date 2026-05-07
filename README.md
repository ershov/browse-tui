# browse-tui

Generic hierarchical browser TUI for shell pipelines and Python recipes.

`browse-tui` is a single-file, dependency-free Python 3 program that turns any
tree-shaped data source into a fast, keyboard-driven terminal UI — search,
preview, multi-select, custom actions. Think `fzf` for hierarchies.

## Install

```bash
# Quick start — just copy the file:
cp browse-tui ~/.local/bin/

# Or use the built-in installer:
./browse-tui --install user        # ~/.local/bin/browse-tui
./browse-tui --install local       # ./browse-tui
./browse-tui --install env         # $VIRTUAL_ENV/bin/browse-tui
./browse-tui --install system      # prints a sudo cp hint
```

No `pip`, no virtualenv: `browse-tui` is a single executable Python file.
Python 3.8+ is the only requirement.

## Quick examples

### CLI — pipe in a flat list (fzf-style):

```bash
ls | browse-tui --root-cmd cat --input tsv --fields id
```

Selection is printed to stdout, exit code 0. Cancel with `q`/`Esc` (exit 1).

### CLI — drill into a tree (lazy):

```bash
browse-tui \
  --children-cmd 'find "$TUI_ID" -mindepth 1 -maxdepth 1 -printf "%p\t%f\t%y\n"' \
  --fields id,title,kind \
  --root-id "$PWD" \
  --preview-cmd '[[ -d "$TUI_ID" ]] && ls -lA "$TUI_ID" || head -200 "$TUI_ID"' \
  --action 'e:Edit:$EDITOR "$TUI_ID"'
```

`$TUI_ID` is set per row; `$EDITOR` and the rest of the parent environment are
inherited unchanged.

### Python recipe — same idea, full API:

```python
#!/usr/bin/env -S browse-tui --run-py
import os
from browse_tui import Browser, Item, Action

def get_children(path):
    if not path:
        path = os.getcwd()
    return [
        Item(id=os.path.join(path, n), title=n, has_children=os.path.isdir(os.path.join(path, n)))
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

Make it executable, run it directly: `chmod +x my-recipe && ./my-recipe`.

## Default keys

| Key                       | Action                              |
| ------------------------- | ----------------------------------- |
| `j`/`k`, arrows           | Cursor down/up                      |
| `g`/`G`, Home/End         | First/last item                     |
| PgUp/PgDn                 | Page up/down                        |
| Right/Left                | Expand/collapse (or step in/out)    |
| Alt-Right/Left            | Recursive expand/collapse           |
| Alt-Down/Up               | Scope into / out                    |
| Space, Alt-Space          | Toggle select (down / up)           |
| Ctrl-A / Ctrl-N           | Select all / clear                  |
| `/`                       | Search; Enter=next, Shift-Enter=prev|
| Ctrl-P                    | Toggle preview pane                 |
| `R`                       | Toggle preview ANSI colours         |
| Shift-Up/Down, Alt-PgUp/Dn| Scroll preview                      |
| Ctrl-R / Ctrl-L           | Reload / redraw                     |
| `?` / F1                  | Help                                |
| Enter                     | `--on-enter` (default: print + exit)|
| `q`, Esc, Ctrl-C          | Quit                                |

## Documentation

- **[docs/cli.md](docs/cli.md)** — every flag, every input format, worked examples.
- **[docs/api.md](docs/api.md)** — Python API: `Browser`, `Item`, `Action`, `Context`, `Pending`.
- **[docs/recipes.md](docs/recipes.md)** — shipped recipes (`browse-fs`, `browse-plan`, `browse-claude`) and how to write your own.
- **[docs/internals.md](docs/internals.md)** — module layout, threading model, contributor docs.
- **[docs/superpowers/specs/2026-04-30-browse-tui-design.md](docs/superpowers/specs/2026-04-30-browse-tui-design.md)** — original design spec.

## License

Same license as the parent repo.
