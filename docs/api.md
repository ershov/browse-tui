# browse-tui — Python API Reference

The `browse_tui` module exposes five public types and a handful of helpers.
Recipes import them directly:

```python
from browse_tui import Browser, Item, Action
```

The same import works whether the recipe is run via `browse-tui --python …`
(in which case `browse_tui` is the running binary, self-injected at startup)
or as part of a regular Python project that has the binary on `sys.path`.

This document is a cross-reference for every public surface. For tutorials
see [docs/recipes.md](recipes.md); for the CLI surface see
[docs/cli.md](cli.md); for the underlying threading model see
[docs/internals.md](internals.md).

---

## `Item`

The basic record type — one node in the hierarchy.

```python
@dataclass
class Item:
    id: Any                  # any hashable; what get_children/preview receive
    title: str = ''          # falls back to str(id)
    tag: str = ''            # rendered as [tag] after id/title
    tag_style: str = ''      # 'green'|'red'|'yellow'|'gray'|'cyan'
                             # |'blue'|'magenta'|'dim'
    has_children: bool = False  # controls ▼/▶ marker, drives expansion
```

`Item` is intentionally non-slotted: recipes can attach arbitrary
domain-specific attributes (`item.size`, `item.mtime`, `item.path` …) and
they survive across the full pipeline (rendering, search, action env vars).

### Title fallback

If `title` is empty, `__post_init__` sets it to `str(id)`. This is what makes
`Item('a')` produce a one-column row showing `a`.

### Example

```python
from browse_tui import Item

items = [
    Item(id='/tmp/a.txt', title='a.txt', tag='4K', tag_style='dim'),
    Item(id='/tmp/dir',   title='dir/',  has_children=True),
    # arbitrary extra attrs are fine:
    Item(id='/tmp/b.txt', title='b.txt'),
]
items[2].size = 1024     # exported as TUI_SIZE if used by a CLI action
items[2].mtime = 1700000000
```

### Coercion: `to_item(x)`

When a `get_children` callback returns a list, each element is run through
`to_item(x)`. Any of these shapes is valid:

| Input          | Result                                                              |
| -------------- | ------------------------------------------------------------------- |
| `Item(...)`    | identity                                                            |
| `'hello'`      | `Item(id='hello')` (title defaults to `'hello'`)                    |
| `('a', 'B')`   | `Item('a', 'B')` — positional, 1-5 elements                         |
| `{'id': 'a'}`  | `Item(**{'id': 'a'})` — extras land as attributes on the result     |

Tuples shorter than 1 or longer than 5 raise `TypeError`. Dicts must contain
an `id` key.

```python
from browse_tui import to_item

to_item('foo')                              # Item(id='foo', title='foo')
to_item(('a', 'Apple'))                     # Item(id='a', title='Apple')
to_item({'id': 'a', 'tag': 'NEW'})          # Item with tag='NEW'
to_item({'id': 'a', 'size': 42}).size       # 42 — extras attach
```

Mixed lists are valid:

```python
def get_children(_):
    return ['plain', ('id2', 'titled'), {'id': 'rich', 'tag': 'NEW'}]
```

---

## `Action`

A keybinding: key string → handler. Recipes pass a list of these to
`Browser(actions=[…])` or call `browser.add_action(action)`.

```python
@dataclass
class Action:
    key: str                          # 'e', 'ctrl-r', 'alt-down', 'space', …
    label: str = ''                   # short text for help screens
    handler: Callable[[Context], None] = None
    requires: str = 'none'            # gating: see below
```

### `requires` gating

The dispatcher silently skips a handler whose precondition is unmet:

| `requires`    | Fires when                                      |
| ------------- | ----------------------------------------------- |
| `'none'`      | always (default)                                |
| `'cursor'`    | `ctx.cursor` is not None                        |
| `'selection'` | `ctx.selected` is non-empty                     |
| `'targets'`   | either selection or cursor is non-empty         |

An unknown gate name behaves like `'none'` (so a typo doesn't silently
disable the action).

### Example

```python
from browse_tui import Action

def edit(ctx):
    ctx.run_external([os.environ.get('EDITOR', 'vi'), ctx.cursor.id])

actions = [
    Action('e', 'Edit',   edit,                   'cursor'),
    Action('q', 'Quit',   lambda ctx: ctx.quit(), 'none'),
    Action('d', 'Delete', delete,                 'targets'),
]
```

User-supplied actions override defaults: binding `q` to a custom handler
replaces the built-in quit on the same key.

### Key names

Key strings come from the terminal layer (`020-terminal.read_key`):

- single chars: `'a'`, `'A'`, `'/'`, `'?'`
- arrows: `'up'`, `'down'`, `'left'`, `'right'`
- modifiers: `'ctrl-a'`, `'alt-down'`, `'shift-up'`, `'shift-enter'`
- specials: `'enter'`, `'esc'`, `'space'`, `'backspace'`, `'home'`, `'end'`,
  `'pgup'`, `'pgdn'`, `'tab'`, `'f1'`, `'ctrl-c'`, `'ctrl-z'`, `'ctrl-p'`, …
- `'alt-space'` arrives as the literal `'alt- '` (alt-prefix + space).

---

## `Context`

What action handlers receive (one argument). `Context` is the main-thread-only
surface — it adds blocking sub-flows (`pick`, `input`, `confirm`, `insert`,
`run_external`, `page`) that read keys synchronously, plus pass-throughs for
the thread-safe Browser ops.

You never construct a `Context` yourself; the main loop builds one per
dispatched action.

### Selection helpers

```python
ctx.cursor      # -> Item | None  (None on placeholders or empty list)
ctx.selected    # -> list[Item]    (every Item in the selection set)
ctx.targets     # -> list[Item]    (selected if non-empty, else [cursor])
```

`ctx.targets` is the most-used: it gives you the right set whether the user
hit space-mark or not.

```python
def delete(ctx):
    targets = ctx.targets
    if not targets:
        return
    if not ctx.confirm(f'delete {len(targets)} item(s)?'):
        return
    for it in targets:
        os.remove(it.id)
    ctx.refresh()
```

### Thread-safe ops (pass-through to Browser)

Each returns a `Pending` (where applicable) — see `Pending` below.

```python
ctx.refresh(id=None, on_complete=None) -> Pending
ctx.cursor_to(id, on_complete=None)   -> Pending
ctx.expand(id, on_complete=None)      -> Pending
ctx.select(ids, replace=False)        -> None
ctx.message(text)                     -> None
ctx.error(text)                       -> None
ctx.quit(code=0, output='')           -> None
```

| Method       | What it does                                                         |
| ------------ | -------------------------------------------------------------------- |
| `refresh`    | Refetch one parent's children (or full root if id is None).          |
| `cursor_to`  | Move cursor to id; resolves once positioned (best-effort).           |
| `expand`     | Add id to expanded; trigger fetch if not cached.                     |
| `select`     | Add ids to selection (or replace).                                   |
| `message`    | Surface a transient status message in the info bar.                  |
| `error`      | Surface an error message (red, sticks until next message).           |
| `quit`       | Exit the main loop with `code`; print `output` to stdout afterwards. |

### Main-thread sub-flows

These read keystrokes synchronously and must only be called from a handler
running on the main thread (which is the normal case — the dispatcher invokes
your handler on the main thread).

#### `ctx.run_external(cmd, env=None) -> int`

Suspend the terminal, run `cmd`, then resume. `cmd` is a list of argv strings
or a shell string (the latter triggers `shell=True`). `env` is merged with
the parent environment; pass `None` to inherit unchanged.

Returns the subprocess exit code, or `-1` if launching raised.

```python
def edit(ctx):
    ctx.run_external([os.environ.get('EDITOR', 'vi'), ctx.cursor.id])
```

#### `ctx.page(text, lang='') -> None`

Pipe `text` into `bat`/`batcat`/`less` (in that order; first one found on
PATH wins). `lang` is forwarded to bat as `--language=<lang>` for syntax
highlighting; ignored by less.

```python
def view(ctx):
    with open(ctx.cursor.id) as f:
        ctx.page(f.read(), lang='py')
```

#### `ctx.input(prompt, default='') -> str | None`

Read a single-line string from the user on the info bar. Returns the typed
text (empty string if the user just hit Enter), or `None` if cancelled with
Esc / Ctrl-C.

```python
def rename(ctx):
    new = ctx.input('rename: ', default=ctx.cursor.title)
    if new is None or new == ctx.cursor.title:
        return
    os.rename(ctx.cursor.id, os.path.join(os.path.dirname(ctx.cursor.id), new))
    ctx.refresh()
```

#### `ctx.confirm(prompt) -> bool`

Show `prompt` followed by `(y/n)` on the info bar. Returns `True` for `y`/`Y`,
`False` for `n`/`N` / Esc / Ctrl-C.

#### `ctx.pick(label, options) -> str | None`

fzf-style filterable picker overlaid on the preview pane. The user types to
filter (case-insensitive substring match), Up/Down/Ctrl-P/Ctrl-N to move,
Enter to choose, Esc to cancel.

Returns the chosen option string, or `None` if cancelled.

```python
def set_status(ctx):
    chosen = ctx.pick('status', ['open', 'in-progress', 'done', 'wontfix'])
    if chosen is None:
        return
    set_status_on(ctx.cursor.id, chosen)
    ctx.refresh()
```

The picker is **not re-entrant** — calling `ctx.pick` from inside another
`ctx.pick`'s handler is undefined behaviour.

#### `ctx.insert(label, on_confirm)`

Enter insert mode. The user moves a placement marker through the visible
tree:

| Key                 | Effect                                                  |
| ------------------- | ------------------------------------------------------- |
| Up/Down/`j`/`k`     | Move marker up/down by one row                          |
| Home/`g`, End/`G`   | Jump to top/bottom (within scope)                       |
| PgUp/PgDn           | Page-sized jumps                                        |
| Right               | Indent — make child of entry above (auto-expanding)     |
| Left                | Outdent — collapse a sibling-above-with-children, or move marker before parent |
| Enter               | Confirm — invokes `on_confirm(relation, dest_id)`       |
| Esc / Ctrl-C / `q`  | Cancel — does not invoke the callback                   |

`relation` is one of `'before'`, `'after'`, `'first'`; `dest_id` is the id
the relation references.

```python
def create(ctx):
    def on_confirm(relation, dest_id):
        new_id = make_new_record()
        place(new_id, relation=relation, near=dest_id)
        ctx.refresh()
    ctx.insert('create', on_confirm)
```

`label` is shown on the marker row (`-- {label} --`) so the user can see
what they're placing.

---

## `Browser`

The engine. Construct one, set callbacks, call `run()`. Public ops are
thread-safe — every mutation routes through an internal post queue and is
applied on the main thread.

### Constructor

```python
Browser(
    *,
    title='browse-tui',
    get_children=lambda _: [],            # (parent_id) -> Iterable[Item|str|tuple|dict]
    get_preview=None,                     # (item_id) -> str  (optional)
    actions=None,                         # list[Action]
    on_enter=None,                        # default Enter handler; see below
    format_item=None,                     # advanced: per-item display override
    root_id=None,
    initial_scope=None,
    show_preview=True,
    show_children_pane=True,
    multi_select=True,
    print_format='{id}',
)
```

### Callbacks

#### `get_children(parent_id) -> Iterable[Item|str|tuple|dict]`

Required (in practice). Called per parent-being-expanded, on a worker thread.
Return any iterable of items in any of the four shapes accepted by `to_item`.
Mixed lists are fine. The result is cached until `ctx.refresh(parent_id)` or
`browser.refresh(parent_id)` invalidates it.

For the very first call, `parent_id` is `root_id`.

Errors raised inside `get_children` are caught at the worker boundary: the
parent's children become `[]` (preventing retry storms), the error is
surfaced via the info bar, and any `Pending` waiting on the fetch still
resolves (callback chains keep firing).

```python
def get_children(parent_id):
    if parent_id is None:
        parent_id = '/'
    return [Item(id=os.path.join(parent_id, n), title=n,
                 has_children=os.path.isdir(os.path.join(parent_id, n)))
            for n in sorted(os.listdir(parent_id))]
```

#### `get_preview(item_id) -> str`

Optional. Called per cursor-move, on a worker thread (latest-wins:
rapid moves coalesce to one in-flight fetch). Return any string; it's shown
verbatim in the preview pane.

`None` from the callback is treated as `''`. Errors are caught and rendered
as `[error] ExceptionName: message`.

```python
def get_preview(item_id):
    try:
        with open(item_id, 'rb') as f:
            return f.read(4096).decode('utf-8', errors='replace')
    except OSError as e:
        return f'[error] {e}'
```

#### `on_enter` modes

What pressing Enter (outside search mode) does:

| Value             | Behaviour                                                      |
| ----------------- | -------------------------------------------------------------- |
| `None` / `'print-exit'` | Format `ctx.targets` via `print_format`, exit with code 0. |
| `'action:KEY'`    | Run the action bound to `KEY` (built-in or registered).        |
| `'noop'`          | Do nothing — long-running browse mode.                         |
| `callable(ctx)`   | Direct callable; invoked with the Context.                     |

```python
Browser(get_children=…, on_enter='action:e')
Browser(get_children=…, on_enter=lambda ctx: print(ctx.cursor.id))
```

#### `format_item(item, ctx) -> [(text, fg, bold), …]`

Optional per-item display override. The renderer falls back to the default
formatter (id + tag) when this is `None`. Most recipes leave it alone.

### Lifecycle

#### `Browser.run() -> int`

Start workers, set up the terminal, run the main loop, tear down. Blocks
until `ctx.quit()` (or `q`/`Esc`). Returns the exit code stashed via `quit`
(or the cancel code 1 from a default-quit).

#### `Browser.add_action(action) -> None`

Register an `Action` after construction. If an existing entry binds the same
key, that entry is replaced — recipes can override one default keybinding
without rebuilding the full list.

```python
b = Browser(get_children=…)
b.add_action(Action('s', 'Stat', stat_handler, 'cursor'))
sys.exit(b.run())
```

`add_action` is **not** thread-safe; call it during construction, before
`run()`.

#### `Browser.from_flat_tree(rows, *, root_id=None, **kwargs)` (class method)

Build a Browser whose `_children` cache is pre-populated from `rows`. Each
row may be `Item`, `str`, `tuple`, or `dict`. Hierarchy detection:

- **Parent-pointer mode** — if any row has a `parent` field other than
  None, every row is grouped under its parent's id.
- **Depth-coded mode** — otherwise, if any row has a `depth` field, walk
  rows in order maintaining a stack: a row at depth `d+1` is a child of
  the most recent row at depth `d`.
- **Flat mode** — neither hint present → all rows are direct children of
  `root_id`.

```python
rows = [
    {'id': 'a', 'title': 'A', 'has_children': True},
    {'id': 'a1', 'title': 'A.1', 'parent': 'a'},
    {'id': 'a2', 'title': 'A.2', 'parent': 'a'},
    {'id': 'b', 'title': 'B'},
]
b = Browser.from_flat_tree(rows, root_id=None, title='demo')
sys.exit(b.run())
```

The synthesised `get_children` reads from the pre-populated cache, so no
user callback runs at runtime. Recipes wanting true laziness should pass
their own `get_children` instead.

### Thread-safe public ops

All callable from any thread. Mutations are funnelled onto the main thread
via the post queue so the renderer never sees a torn state.

```python
browser.refresh(id=None, on_complete=None) -> Pending
browser.cursor_to(id, on_complete=None)    -> Pending
browser.expand(id, on_complete=None)       -> Pending
browser.select(ids, replace=False)
browser.message(text)
browser.error(text)
browser.quit(code=0, output='')
browser.cancel(*pendings)                  # sugar for p.cancel()
browser.post(callable_)                    # schedule fn on main thread
```

#### `browser.watch(callback, interval=None) -> threading.Thread`

Spawn a daemon thread invoking `callback(browser)` either once (`interval=None`)
or in a loop with `time.sleep(interval)` between calls.

Watchers update the UI by calling `browser.refresh(id)` — the post queue
funnels everything onto the main thread.

```python
def mtime_watcher(browser):
    """Poll every 1s; refresh changed dirs."""
    last = {}
    while True:
        time.sleep(1.0)
        for d in list(browser._state._children.keys()):
            try:
                m = os.stat(d).st_mtime
            except OSError:
                continue
            if d in last and last[d] != m:
                browser.refresh(d)
            last[d] = m

b = Browser(get_children=…)
b.watch(mtime_watcher)
sys.exit(b.run())
```

Uncaught exceptions in the callback are surfaced via `browser.error(...)` and
the watcher thread dies (no auto-restart).

---

## `Pending`

A handle for an async operation that may chain follow-up callbacks.
`refresh`, `cursor_to`, and `expand` return one.

```python
class Pending:
    @property
    def done(self) -> bool: ...
    @property
    def cancelled(self) -> bool: ...

    def cancel(self) -> None: ...
    def then(self, callback) -> 'Pending': ...
```

### `then(cb)`

Append `cb` to the chain. If the Pending is already resolved, `cb` runs
synchronously inside `then()`. If cancelled, `then()` is a no-op (still
returns self for ergonomics).

Callbacks always run on the main thread, after the worker's result has been
applied to the cache.

```python
ctx.expand('a').then(
    lambda: ctx.cursor_to('a-1').then(
        lambda: ctx.message('navigated')))
```

### `cancel()`

Mark the Pending cancelled. Any callback registered before or after `cancel`
will not fire. Idempotent. Worker fetches are not killed — only the
user-visible chain is suppressed.

```python
p = ctx.refresh('expensive-id')
# ... user moved on ...
p.cancel()    # chain won't fire even when the fetch finishes
```

### Three usage styles

```python
# 1. Fire and forget:
ctx.refresh()

# 2. on_complete kwarg:
ctx.refresh(id='a', on_complete=lambda: ctx.cursor_to('a-1'))

# 3. Chained .then():
ctx.refresh(id='a').then(lambda: ctx.cursor_to('a-1'))
```

All three are equivalent.

---

## Helper functions

A short tour of the helpers worth knowing.

### `to_item(x) -> Item`

Coerces `Item | str | tuple | dict` to an `Item`. Raises `TypeError` on
unsupported shapes. See `Item` above for the rules.

### `parse_input(data, *, fmt, fields=None, record_sep=b'\n', strict=False)`

Parse raw bytes into an iterator of dicts (Item-kwargs). Used by the CLI's
`--root-cmd` / `--children-cmd` glue, but exposed for recipes that want to
handle arbitrary text formats too.

`fmt` accepts the same values as `--input`:
`tsv | csv | json | json-array | ifs:CHARS | split:REGEX | match:REGEX`.

```python
from browse_tui import parse_input

rows = list(parse_input(b'a\tApple\nb\tBanana\n', fmt='tsv'))
# [{'id': 'a', 'title': 'Apple'}, {'id': 'b', 'title': 'Banana'}]

rows = list(parse_input(b'[{"id":"a"},{"id":"b"}]', fmt='json-array'))
# [{'id': 'a'}, {'id': 'b'}]
```

`strict=False` (the default) skips malformed records silently; `strict=True`
raises on the first malformed record.

### `coerce_has_children(raw) -> bool`

Coerce a string/None/bool to bool for the `has_children` field.

| Input                                       | Result |
| ------------------------------------------- | ------ |
| `True` / `'1'` / `'true'` / `'yes'` / `'y'` / `'on'` | `True`  |
| `False` / `None` / `''` / anything else     | `False` |

(Case-insensitive. Phase 1 is tolerant — unknown strings return False rather
than raising.)

```python
from browse_tui import coerce_has_children

coerce_has_children('1')      # True
coerce_has_children('false')  # False
coerce_has_children(None)     # False
```

### Module exports

What actually lives at `browse_tui.<name>`:

| Name                      | Kind        |
| ------------------------- | ----------- |
| `Browser`                 | class       |
| `Item`                    | dataclass   |
| `Action`                  | dataclass   |
| `Context`                 | class       |
| `Pending`                 | class       |
| `to_item`                 | function    |
| `parse_input`             | function    |
| `coerce_has_children`     | function    |
| `__version__`             | str         |

The internal terminal layer, renderer, and state-builder helpers are also in
the module namespace (single-file build), but are not part of the documented
API and may change without notice. A handful — `parse_tsv`, `parse_csv`,
`parse_json_lines`, `parse_json_array`, `parse_ifs`, `parse_split`,
`parse_match`, `decode_record_sep`, `make_cli_action`, `item_env` — are
stable enough that recipes occasionally use them directly.

---

## Putting it together

Minimal but full-featured recipe:

```python
#!/usr/bin/env -S browse-tui --python
"""mini — a 30-line filesystem browser."""
import os, sys
from browse_tui import Action, Browser, Item

def get_children(path):
    if not path:
        path = os.getcwd()
    out = []
    try:
        for n in sorted(os.listdir(path)):
            full = os.path.join(path, n)
            out.append(Item(id=full, title=n, has_children=os.path.isdir(full)))
    except OSError as e:
        return [Item(id='__err__', title=f'[error] {e}', tag='err', tag_style='red')]
    return out

def get_preview(item_id):
    if os.path.isdir(item_id):
        return '\n'.join(sorted(os.listdir(item_id))[:200])
    try:
        with open(item_id, 'rb') as f:
            return f.read(4096).decode('utf-8', errors='replace')
    except OSError as e:
        return f'[error] {e}'

def edit(ctx):
    ctx.run_external([os.environ.get('EDITOR', 'vi'), ctx.cursor.id])

if __name__ == '__main__':
    root = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.getcwd()
    sys.exit(Browser(
        title='mini',
        get_children=get_children,
        get_preview=get_preview,
        root_id=root,
        actions=[Action('e', 'Edit', edit, 'cursor')],
        on_enter='action:e',
    ).run())
```

For a full-featured filesystem browser see `recipes/browse-fs`; for a
plan-tui-equivalent ticket browser see `recipes/browse-plan`; for a
multi-level Claude Code session browser see `recipes/browse-claude` — and
the [recipes index](recipes.md) walks through each.

---

## See also

- [README.md](../README.md) — quickstart.
- [docs/cli.md](cli.md) — CLI surface.
- [docs/recipes.md](recipes.md) — shipped recipes.
- [docs/internals.md](internals.md) — module layout, threading model.
- [docs/superpowers/specs/2026-04-30-browse-tui-design.md](superpowers/specs/2026-04-30-browse-tui-design.md) — original design spec.
