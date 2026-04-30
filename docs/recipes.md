# browse-tui — Recipes

`browse-tui` ships with three production-quality recipes. Each is a
single-file Python script with a `#!/usr/bin/env -S browse-tui --python`
shebang — make them executable and run them directly, or invoke as
`browse-tui --python recipes/<name>`.

This page is the recipe index plus a "writing your own" walkthrough.

---

## `recipes/browse-fs`

Filesystem browser with mtime watcher.

**One-line summary:** lazy `os.scandir` children, file/dir preview, edit /
open / delete actions, and a background mtime watcher that auto-refreshes
changed directories.

**Demonstrates:**

- Lazy `get_children` — `os.scandir` per parent, only when the user expands.
- `get_preview` branching on dir vs file (head of file, or `os.listdir`).
- Custom `Action` handlers using `ctx.run_external` for `$EDITOR` /
  `xdg-open`.
- `ctx.confirm` as a y/n prompt before destructive operations.
- `ctx.error` and `ctx.refresh` for error reporting and post-action UI
  refresh.
- Recipe-set Item attributes (`item.size`, `item.mode`, `item.mtime`) that
  survive the full pipeline.
- `browser.watch(callback)` — a daemon thread polling mtimes and calling
  `browser.refresh(d)` on change.

**Usage:**

```bash
./recipes/browse-fs            # current directory
./recipes/browse-fs ~          # any path
./recipes/browse-fs /etc
```

Keys (in addition to defaults): `e` edit (`$EDITOR`), `o` open (`xdg-open`),
`d` delete (with confirmation). Enter is wired to `action:e`.

**Source:** [`recipes/browse-fs`](../recipes/browse-fs) (~130 lines)

---

## `recipes/browse-plan`

Drop-in replacement for `plan-tui` on the `browse-tui` core.

**One-line summary:** a full plan-tui port — same keybindings, same
behaviour, same on-disk format. Doubles as the parity validator for the
abstraction.

**Demonstrates:**

- Subprocess-driven `get_children` — shells out to the `plan` CLI and parses
  tab-separated output.
- `ctx.pick(label, options)` — fzf-style picker for status changes.
- `ctx.run_external` + `ctx.page` — edit ticket via `$EDITOR`, view via
  bat/less.
- `ctx.insert(label, on_confirm)` — full insert mode for create / move
  flows.
- Synthetic root rows — a non-expandable "Project" entry above the real
  tree (mirrors plan-tui's UX).
- Mixed-type ids (integers for tickets, `0` for the synthetic project).
- Multi-target actions with target filtering (`ctx.targets` minus the
  synthetic id).

**Usage:**

```bash
./recipes/browse-plan          # full project tree
./recipes/browse-plan 5        # drill into ticket 5 (initial-scope)
```

Keys: `s` status (picker), `e`/`E` edit (recursive), `v`/`V` view
(recursive), `c`/`C` create (bulk), `m` move, `x` close, `o` reopen, `~`
project log.

**Source:** [`recipes/browse-plan`](../recipes/browse-plan) (~390 lines)

---

## `recipes/browse-claude`

Claude Code project / session / message browser.

**One-line summary:** three-level hierarchy walking
`~/.claude/projects/<encoded-path>/<session>.jsonl` files, with per-message
JSON pretty-print preview.

**Demonstrates:**

- Multi-level lazy hierarchy — id-shape dispatch (None →
  projects, dir-path → sessions, file-path → messages).
- JSON-line parsing with mixed record shapes (user, assistant, last-prompt,
  permission-mode).
- Compact summaries (one-line title with role + first 80 chars; full
  pretty-print in preview).
- `_human_time` style helpers — recipe-side formatting reaches the UI via
  the `tag` field plus `tag_style`.
- Truncation markers — `_MESSAGE_LIMIT` caps per-session enumeration; an
  explicit "(more — only first N shown)" row tells the user where the cliff
  is.
- Resolving message ids back to the source file via `ctx.run_external` to
  open the `.jsonl` in `$EDITOR`.

**Usage:**

```bash
./recipes/browse-claude                  # all projects
./recipes/browse-claude /home/me/work    # initial-scope
```

Keys: `e` / `o` open in `$EDITOR`, `y` show id (debugging).

**Source:** [`recipes/browse-claude`](../recipes/browse-claude) (~450 lines)

---

## Writing your own recipe

A useful recipe is around 30-100 lines. The pattern:

1. **Implement `get_children(parent_id)`** — return any iterable of
   `Item | str | tuple | dict`.
2. **Optionally implement `get_preview(item_id)`** — return a string.
3. **Define `Action`s** — each is `(key, label, handler, requires)`.
4. **Build a `Browser`**, call `.run()`, exit with the return code.

### Skeleton

```python
#!/usr/bin/env -S browse-tui --python
"""my-recipe — short docstring."""

import os
import sys
from browse_tui import Action, Browser, Item


def get_children(parent_id):
    """Return children of parent_id. parent_id is None for the root."""
    if parent_id is None:
        return [Item(id='a', title='Apple', has_children=True),
                Item(id='b', title='Banana')]
    if parent_id == 'a':
        return ['a1', 'a2', 'a3']
    return []


def get_preview(item_id):
    return f'preview of {item_id}'


def my_action(ctx):
    ctx.message(f'pressed e on {ctx.cursor.id}')


def main():
    sys.exit(Browser(
        title='my-recipe',
        get_children=get_children,
        get_preview=get_preview,
        actions=[Action('e', 'Echo', my_action, 'cursor')],
    ).run())


if __name__ == '__main__':
    main()
```

Make it executable (`chmod +x my-recipe`), drop the shebang, run it.

### Common patterns

#### Loading children from a subprocess

```python
import subprocess

def get_children(parent_id):
    result = subprocess.run(['plan', str(parent_id), 'list'],
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return []
    return [parse_line(ln) for ln in result.stdout.splitlines() if ln]
```

#### Custom attributes that survive to actions

```python
def get_children(_):
    items = [Item(id='a', title='Apple')]
    items[0].size = 1024
    items[0].mtime = time.time()
    return items

# When using a CLI --action, those attributes appear as $TUI_SIZE / $TUI_MTIME.
# When using a Python Action handler, they're plain attributes on ctx.cursor:
def show(ctx):
    ctx.message(f'size: {ctx.cursor.size}')
```

#### Background updates via a watcher

```python
def my_watcher(browser):
    while True:
        time.sleep(1.0)
        for changed_id in poll_for_changes():
            browser.refresh(changed_id)

b = Browser(get_children=...)
b.watch(my_watcher)
sys.exit(b.run())
```

The `browser.refresh(id)` call is thread-safe — the post queue funnels it
onto the main thread before mutation.

#### Eager mode (pre-populated tree)

```python
rows = [
    {'id': 'a', 'title': 'A', 'has_children': True},
    {'id': 'a1', 'parent': 'a'},
    {'id': 'a2', 'parent': 'a'},
    {'id': 'b', 'title': 'B'},
]
b = Browser.from_flat_tree(rows, root_id=None, title='demo')
```

`from_flat_tree` auto-detects parent-pointer / depth-coded / flat-list mode.
See [docs/api.md](api.md#browserfrom_flat_treerows--root_idnone-kwargs-class-method).

#### Confirm + refresh

```python
def delete(ctx):
    if not ctx.confirm(f'delete {len(ctx.targets)} items?'):
        return
    for it in ctx.targets:
        os.remove(it.id)
    ctx.refresh()
```

#### Picker (fzf-style sub-flow)

```python
def set_priority(ctx):
    chosen = ctx.pick('priority', ['low', 'medium', 'high', 'urgent'])
    if chosen is None:
        return
    save_priority(ctx.cursor.id, chosen)
    ctx.refresh()
```

#### Insert mode

```python
def add(ctx):
    def on_confirm(relation, dest_id):
        # relation in {'before', 'after', 'first'}
        new_id = create_record(parent_or_sibling=dest_id, where=relation)
        ctx.refresh()
        ctx.cursor_to(new_id)
    ctx.insert('add', on_confirm)
```

#### Quit with output

```python
def confirm_pick(ctx):
    if ctx.cursor:
        ctx.quit(code=0, output=ctx.cursor.id + '\n')
```

`ctx.quit(code, output)` exits the loop; `output` is printed to stdout
after terminal teardown so it integrates with shell pipelines.

### Tips

- **Errors in callbacks won't crash the UI** — `get_children` raising lands
  as `[]` for that parent, surfaces as an info-bar message; `get_preview`
  raising lands as `[error] ExceptionName: message` in the preview.
- **Keep ids hashable.** Strings, ints, tuples are all fine. Don't use
  mutable types (lists, sets, dicts) as ids.
- **Set `has_children=True` for branches.** Without it, the user can't
  press Right to expand and the browser won't fetch grandchildren.
- **The `tag` field is for short labels** (status, size, count) shown in
  brackets after the title. Use `tag_style` to colour it.
- **Shebang gotcha:** the `-S` flag in `#!/usr/bin/env -S browse-tui --python`
  is what lets `env` parse multiple args. Without it your shebang only
  resolves the first word. (Linux 4.18+, macOS 10.15+ have `-S`.)

---

## See also

- [docs/api.md](api.md) — full Python API.
- [docs/cli.md](cli.md) — CLI flags (also runnable from a recipe via
  `browse-tui --python …`).
- [README.md](../README.md) — quickstart.
