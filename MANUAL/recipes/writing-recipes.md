# Writing your own recipe

A useful recipe is usually short. The pattern:

1. **Implement `get_children(parent_id)`** — return any iterable of
   `Item | str | tuple | dict`.
2. **Optionally implement `get_preview(item_id)`** — return a string.
3. **Define `Action`s** — each is `(key, label, handler, requires)`.
4. **Build a `Browser`**, call `.run()`, exit with the return code.

## Skeleton

```python
#!/usr/bin/env -S browse-tui --run-py
"""my-recipe — short docstring."""

import os
import sys
from browse_tui import Action, Browser, Item


def get_children(parent_id, *, reload=False):
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
    ctx.flash(f'pressed e on {ctx.cursor.id}')


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

## Common patterns

### Loading children from a subprocess

```python
import subprocess

def get_children(parent_id, *, reload=False):
    result = subprocess.run(['plan', str(parent_id), 'list'],
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return []
    return [parse_line(ln) for ln in result.stdout.splitlines() if ln]
```

### Custom attributes that survive to actions

```python
def get_children(_, *, reload=False):
    items = [Item(id='a', title='Apple')]
    items[0].size = 1024
    items[0].mtime = time.time()
    return items

# When using a CLI --action, those attributes appear as $TUI_SIZE / $TUI_MTIME.
# When using a Python Action handler, they're plain attributes on ctx.cursor:
def show(ctx):
    ctx.flash(f'size: {ctx.cursor.size}')
```

### Background updates via a watcher

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

### Eager mode (pre-populated tree)

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
See [api.md](../api.md#browserfrom_flat_treerows--root_idnone-kwargs-class-method).

### Confirm + refresh

```python
def delete(ctx):
    if ctx.confirm(f'delete {len(ctx.targets)} items?') != 'Yes':
        return
    for it in ctx.targets:
        os.remove(it.id)
    ctx.refresh()
```

### Picker (fzf-style sub-flow)

```python
def set_priority(ctx):
    chosen = ctx.pick('priority', ['low', 'medium', 'high', 'urgent'])
    if chosen is None:
        return
    save_priority(ctx.cursor.id, chosen)
    ctx.refresh()
```

### Insert mode

```python
def add(ctx):
    def on_confirm(relation, dest_id):
        # relation in {'before', 'after', 'first'}
        new_id = create_record(parent_or_sibling=dest_id, where=relation)
        ctx.refresh()
        ctx.cursor_to(new_id)
    ctx.insert('add', on_confirm)
```

### Quit with output

```python
def confirm_pick(ctx):
    if ctx.cursor:
        ctx.quit(code=0, output=ctx.cursor.id + '\n')
```

`ctx.quit(code, output)` exits the loop; `output` is printed to stdout
after terminal teardown so it integrates with shell pipelines.

## Framework constraints when pushing data

The eager-push surface (`update_data`, `set_preview`, `append_preview`,
`clear_preview`, `invalidate_preview`) is fast, thread-safe, and
silently forgiving in places that can bite you. Two rules to keep in
mind:

### Children-list authority

Once `_state._children[parent]` is non-None — populated by a
`get_children` delivery or any `update_data` upsert — the framework
treats whatever's there as the parent's children list. There is no
"loading more" indicator after the initial population: tree expansion
paints exactly what's in the list at paint time.

**Implication:** if you push children for a parent via
`update_data(upsert(...))`, you must *eventually* push all siblings.
Partial lists are valid as transient states (the tail-worker pattern
streams children over time, and the user sees them appear as they
arrive), but a permanently-incomplete list means tree expansion
permanently hides the missing siblings. The framework can't tell
"still streaming" from "forgot to push the rest" — that's a
recipe-author responsibility.

### Preview-API registration prerequisite

`set_preview`, `append_preview`, `clear_preview`, and
`invalidate_preview` all no-op when the id isn't present in
`_items_by_id`. Preview storage lives on the Item (`Item.preview` /
`Item.preview_render`), so without a registered Item there's nowhere
to write. To cache preview text for an id, register the Item first.

The cheapest idiom is an idempotent upsert with no field changes:

```python
b.update_data([upsert(id_, parent_id)])
b.set_preview(id_, text)
```

For an existing id this is patch-merge-with-no-fields (no-op); for a
missing id it creates a minimal Item under `parent_id` with default
fields (`title` backfilled from `str(id)`, `tag=''`,
`has_children=False`, etc.) unless you pass them explicitly. Pair
with the children-list authority rule above — registering one item
via upsert puts it in the parent's children list, so you must
eventually push the full sibling set under that parent.

**The framework registers cursor-reachable Items.** `visible_items`
synthesises and registers a `scope_root` Item when one doesn't
already exist (see the `state.scope_stack` branch in 040-state.py),
so the per-Item preview cache always has somewhere to land for the
cursor's current row. Recipes only need to enforce the registration
constraint above for their *own* pushes — not for cursor navigation.

## Tips

- **Errors in callbacks won't crash the UI** — `get_children` raising lands
  as `[]` for that parent, surfaces as an info-bar message; `get_preview`
  raising lands as `[error] ExceptionName: message` in the preview.
- **Keep ids hashable.** Strings, ints, tuples are all fine. Don't use
  mutable types (lists, sets, dicts) as ids.
- **Set `has_children=True` for branches.** Without it, the user can't
  press Right to expand and the browser won't fetch grandchildren.
- **The `tag` field is for short labels** (status, size, count) shown in
  brackets after the title. Use `tag_style` to colour it.
- **Shebang gotcha:** the `-S` flag in `#!/usr/bin/env -S browse-tui --run-py`
  is what lets `env` parse multiple args. Without it your shebang only
  resolves the first word. (Linux 4.18+, macOS 10.15+ have `-S`.)

## See also

- [api.md](../api.md) — full Python API.
- [cli.md](../cli.md) — CLI flags (also runnable from a recipe via
  `browse-tui --run-py …`).
- [Recipe index](../recipes.md) — all shipped recipes.
- [README.md](../../README.md) — quickstart.
