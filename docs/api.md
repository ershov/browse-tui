# browse-tui — Python API Reference

The `browse_tui` module exposes five public types and a handful of helpers.
Recipes import them directly:

```python
from browse_tui import Browser, Item, Action
```

The same import works whether the recipe is run via `browse-tui --run-py …`
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
    hidden: bool = False     # per-row visibility — skipped at render time
                             # (cascades over the subtree); see Visibility
                             # under `browser.update_data` below
```

`Item` is intentionally non-slotted: recipes can attach arbitrary
domain-specific attributes (`item.size`, `item.mtime`, `item.path` …) and
they survive across the full pipeline (rendering, search, action env vars).

Two such attributes are honoured by the list renderer when set:

| Attribute  | Type     | Effect |
| ---------- | -------- | ------ |
| `row_bg`   | int (256-color) | Background colour for the whole row (turns the row into a coloured stripe; extends across the trailing pad). |
| `row_fg`   | int (256-color) | Foreground colour for segments that don't specify their own `fg`. Segments with explicit colours keep theirs. Useful for "dim the whole row" / "red row for failed status" effects. |

Both default to `None` (no override). Set per-item with `item.row_bg = 1`
/ `item.row_fg = 8` after construction.

A framework-internal `_filter_hidden` field also lives on `Item` (written
by the interactive filter evaluator). It's `init=False, repr=False,
compare=False` — invisible to recipe code and never part of equality.
See the *Interactive filter* subsection under `browser.update_data`.

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
| `('a', 'B')`   | `Item('a', 'B')` — positional, 1-6 elements                         |
| `{'id': 'a'}`  | `Item(**{'id': 'a'})` — extras land as attributes on the result     |

Tuples shorter than 1 or longer than 6 raise `TypeError`. Dicts must contain
an `id` key. The 6-tuple form maps to `(id, title, tag, tag_style, has_children, hidden)`.

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

### Cache introspection

Read-only views into the framework's live item / children cache. Use
these to answer "what's currently loaded" without forcing a refetch.
They are stable public API; the underlying `_items_by_id` / `_children`
fields on `State` remain framework-private.

```python
ctx.items_by_id                       -> dict[id, Item]   # live read-only
ctx.get_item(id)                      -> Item | None      # O(1) lookup
ctx.cached_children(parent_id)        -> list[Item] | None
ctx.cached_parents()                  -> list[id]
ctx.all_items()                       -> Iterator[Item]   # snapshot
```

| Method            | What it does                                                                 |
| ----------------- | ---------------------------------------------------------------------------- |
| `items_by_id`     | Live dict; do not mutate. Identity stable across calls; contents stream.     |
| `get_item`        | O(1) lookup. Returns `None` if id is not loaded.                             |
| `cached_children` | `None` means "not yet fetched"; `[]` means "fetched, no children". Copy.     |
| `cached_parents`  | Ids of every parent whose children list is cached (insertion order).         |
| `all_items`       | Snapshot iterator — safe under concurrent cache mutation.                    |

Typical recipe patterns:

```python
# "Bulk visibility flip" — toggle hidden on every loaded item
for it in ctx.all_items():
    if condition(it.id):
        ctx.upsert(it.id, KEEP_PARENT, hidden=not it.hidden)

# "Diff and append" — tail-feed dedup before update_data
existing = {it.id for it in (ctx.cached_children(parent) or [])}
for fresh in incoming:
    if fresh.id not in existing:
        ctx.upsert(fresh.id, parent, **kwargs)

# "Iterate every loaded subtree" — directory mtime watcher etc.
for p in ctx.cached_parents():
    poll(p)
```

### Push-API pass-throughs

Mirror the `Browser` push surface so action handlers can mutate the tree
without keeping a separate `browser` reference:

```python
ctx.update_data(ops)                  -> None
ctx.upsert(id, parent_id, **fields)   -> None    # one-op convenience
ctx.set_item(id, parent_id, **fields) -> None    # one-op convenience
ctx.remove(id)                        -> None    # one-op convenience
ctx.set_preview(id, text)             -> None
ctx.append_preview(id, chunk)         -> None
ctx.clear_preview(id)                 -> None
ctx.invalidate_preview(id)            -> None    # drop cache + re-fetch
ctx.get_cached_preview(id)            -> str | None   # read without re-fetch
ctx.drop_preview_cache(id=None)       -> None    # drop one/all; auto-kicks cursor
ctx.preview_item_id                   -> id | None    # id whose preview is shown
ctx.preview_to_tail()                 -> None    # pin preview to bottom
ctx.nav_home()                        -> None    # cursor -> row 0 + PIN_FIRST
ctx.nav_end()                         -> None    # cursor -> last row + PIN_LAST
ctx.collapse_all()                    -> None    # clear all expanded
ctx.expand_subtree(id, lazy=True)     -> None    # expand id + cached descendants
ctx.select_all_visible()              -> None    # selection = every visible row
ctx.clear_selection()                 -> None    # drop every entry
ctx.invert_selection()                -> None    # flip visible rows' selection
ctx.scope                             -> id | None    # current scope (None at root)
ctx.scope_stack                       -> tuple[id, ...]
ctx.scope_into(id)                    -> None    # drill in; fires on_scope_change
ctx.scope_out()                       -> None    # drill out; fires on_scope_change
ctx.filters                           -> tuple[str, ...]   # active filter list
ctx.set_filters(filters)              -> None    # replace; drops empty strings
ctx.add_filter(text)                  -> None    # append (no-op if empty)
ctx.clear_filters()                   -> None    # alias for set_filters([])
ctx.mode                              -> Mode    # NORMAL / SEARCH_EDIT / FILTER_EDIT
ctx.search_query                      -> str     # active /-search query
ctx.set_search_query(text)            -> None    # replace; '' clears; forces NORMAL
ctx.clear_search()                    -> None    # alias for set_search_query('')
ctx.run_in_worker(fn)                 -> threading.Thread
```

`upsert` / `set_item` / `remove` are convenience wrappers for the single-op
case (each routes through `update_data` with a one-element list); for
multiple ops, prefer `update_data` directly so the batch stays atomic.

`run_in_worker(fn)` spawns a one-shot daemon thread, surfacing any
uncaught exception via `browser.error`. The thread handle is mostly
informational — synchronisation should be done via `Pending` or
`threading.Event` inside `fn`.

### Escape hatches (advanced; unstable surface)

When the documented Context surface doesn't cover what a recipe needs,
two read-only properties expose the underlying objects:

```python
ctx.browser   -> Browser   # the underlying Browser instance
ctx.state     -> State     # the underlying State dataclass
```

Anything reachable through them is **at-your-own-risk** — names and
shapes inside `Browser` and `State` may change between minor versions
where the typed Context methods do not. Prefer the typed methods
whenever they exist; if you find yourself reaching for an escape hatch
to do something common, please open an issue so the capability can be
promoted.

Common legitimate reads off `ctx.state`:

```python
ctx.state.expanded         # set of expanded ids in the current scope
ctx.state.scope_stack      # ancestor chain (root-first) of current scope
ctx.state.cursor           # cursor row index into the visible list
ctx.state.selected         # set of selected ids
ctx.state.root_id          # initial scope root id (None if unset)
```

Writing to fields directly is unsupported — route mutations through
Context / Browser methods so the framework sees them.

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
    show_ids='auto',                      # 'always' | 'auto' | 'never'
    preview_ansi=True,                    # honour SGR colour codes in preview
)
```

#### `show_ids`

Controls whether the per-row id segment is rendered in front of the title.

| Value      | Behaviour                                                              |
| ---------- | ---------------------------------------------------------------------- |
| `'always'` | Always emit `'<id> <title>'` (yellow id, then title).                  |
| `'auto'`   | Default. Emit the id only when `str(item.id) != item.title`. The line-based CLI shape (`Item(id='README.md')`) renders as just `'README.md'`; tracker-style sources (`Item(id=42, title='Implement feature')`) render as `'42 Implement feature'`. |
| `'never'`  | Never emit the id segment.                                             |

A `format_item` hook overrides this entirely — the hook's segments are
emitted verbatim.

### Lifecycle hooks

Three optional callback kwargs let recipes react to framework events
without polling. Each takes `(ctx) -> None`; recipes read what they
need off `ctx`. All three exist on `Browser.__init__`:

```python
Browser(...,
        on_cursor_change=cb,   # cursor row id changed (debounced)
        on_scope_change=cb,    # scope_into / scope_out completed
        on_quit=cb)            # shutdown, after screen restore
```

| Hook | When it fires | Notes |
| ---- | ------------- | ----- |
| `on_cursor_change` | At most once per main-loop tick; only when the row id under the cursor differs from the last fire. | Rapid moves coalesce. Re-anchor moves that land on the same id are silent. Exceptions surface via `Browser.error`. |
| `on_scope_change` | After a successful `scope_into` / `scope_out` transition. | Read `ctx.state.scope_stack` for the new scope. Exceptions surface via `Browser.error`. |
| `on_quit` | Once during shutdown, after the screen is restored, before `Browser.run` returns. | Use for worker / file-handle / temp-file cleanup. Exceptions are swallowed silently — a failing cleanup must not block exit. |

Typical patterns:

```python
def on_cursor_change(ctx):
    item = ctx.cursor
    if item is None:
        return
    log.info(f'cursor: {item.id}')

def on_scope_change(ctx):
    if ctx.state.scope_stack:
        ctx.message(f'in scope: {ctx.state.scope_stack[-1]}')

def on_quit(ctx):
    _STOP_EVENT.set()        # tell worker threads to wind down
    for f in _OPEN_HANDLES:
        f.close()
```

### Callbacks

#### `get_children(parent_id) -> Iterable[Item|str|tuple|dict] | Generator | None`

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

##### Return-shape contract

| Return                       | Behaviour                                                                                                                                              |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| any iterable (incl. `[]`)    | All items are upserted under `parent_id` in one atomic batch with a trailing `complete(parent_id)` op (clears the loading flag).                       |
| generator                    | Each `yield` is delivered as its own `update_data` batch — the UI keeps showing "loading…" between yields. On `StopIteration` a final `complete(parent_id)` clears it. A mid-stream raise surfaces via `browser.error(...)`; loading stays set so the recipe can clear it explicitly. |
| `None`                       | No batch is posted; the loading flag stays set. The recipe is expected to push from elsewhere (e.g. via `browser.update_data` from a watcher).         |

Per-yield, `isinstance(chunk, list)` is treated as a batch of items; anything
else (`Item`, `tuple`, `dict`, `str`) is a single item coerced via `to_item` —
same flexibility as today's mixed return lists, applied per-yield.

```python
def get_children(parent_id):
    page = 0
    while True:
        rows = jira_search(parent_id, offset=page * 100, limit=100)
        if not rows:
            return
        yield [Item(id=r.key, title=r.summary, tag=r.status) for r in rows]
        page += 1
```

The auto-clear on return-with-iterable is unconditional. A recipe that
returns initial data *and* expects later watcher pushes to keep showing
"loading…" must explicitly call
`browser.update_data([incomplete(parent_id)])` after returning.

#### `get_preview(item_id) -> str | Generator[str] | None`

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

##### Generator support

`get_preview` may also return a generator yielding string chunks. Each yield
is appended to the per-id preview cache via `append_preview`. The worker
eager-pulls until the buffered content reaches a configurable cap
(`Browser(preview_buffer_cap_chars=100_000, preview_buffer_cap_lines=1000)`),
then pauses without closing the generator. When the user scrolls within
~12 wrapped rows of the buffered tail, the renderer signals demand and the
worker resumes pulling for one more cap window.

A cursor-move closes the paused generator (firing the recipe's `finally`
block, useful for releasing file handles or sockets). A mid-stream raise
appends `[error] ExceptionName: message` to whatever is already buffered.

```python
def get_preview(item_id):
    with open(item_id) as f:
        try:
            for line in f:
                yield line
        finally:
            pass  # f's context manager closes on cursor-move
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
browser.nav_home()                         # cursor → row 0; engage PIN_FIRST
browser.nav_end()                          # cursor → last row; engage PIN_LAST
browser.select(ids, replace=False)
browser.message(text)
browser.error(text)
browser.quit(code=0, output='')
browser.cancel(*pendings)                  # sugar for p.cancel()
browser.post(callable_)                    # schedule fn on main thread
browser.update_data(ops)                   # batched tree mutations
browser.set_preview(id, text)
browser.append_preview(id, chunk)
browser.clear_preview(id)
browser.invalidate_preview(id)             # drop cache + re-fetch
browser.preview_to_tail()                  # pin preview scroll to bottom
```

`update_data`, `append_preview`, and `clear_preview` schedule a callable on
the post queue that mutates state on the main thread; their writes become
visible (and trigger a paint) on the next drain — no keystroke required.
`set_preview` routes through the single-slot preview-result lane instead of
the post queue. Both land on the main thread, but recipes mixing
`set_preview` with `append_preview`/`clear_preview` for the same id should
pick one path — see `browser.append_preview` for the ordering caveat.

#### `browser.update_data(ops) -> None`

Apply a batched list of tree-mutation ops on the main thread. Snapshots
`ops` to a list on the calling thread and posts a single callable that
runs `apply_ops`. The whole batch is atomic with respect to render; two
separate `update_data` calls are not — each is its own post-queue task.

| Op                                                  | Effect                                                                                                                                                                                          |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `("upsert", id, parent_id, fields[, where])`        | Insert under `parent_id` if new; **patch-merge** in place if known (matching keys override `Item` fields, others land as custom attrs). Reparents if `parent_id` differs from the existing parent. `parent_id=None` patches fields only (silent no-op if `id` is unknown and `state.root_id` is not None). Optional `where` carries a positioning descriptor — see *Positioning* below. |
| `("set", id, parent_id, fields[, where])`           | Insert-or-replace. `fields` is the entire record; unspecified `Item` fields revert to dataclass defaults; custom attrs are dropped. A new `Item` instance is constructed. Children stored under `_children[id]` are preserved. Optional `where` carries a positioning descriptor — see *Positioning* below.                                |
| `("mod", id, parent_id, fields[, where])`           | **Patch only — never inserts.** Silent no-op if `id` is unknown. `parent_id=KEEP_PARENT` (default) leaves parent untouched; any other value reparents. `where` always implies reposition (no `"reposition"` flag needed). See *Visibility* below for the main use case. |
| `("remove", id)`                                    | Remove the item. Cascades: `_children[id]` is also dropped along with all descendant index entries.                                                                                              |
| `("clear_children", parent_id)`                     | Drop all known children of `parent_id`; cache entry reverts to "no fetch yet"; `_loading[parent_id]` flips to False.                                                                             |
| `("complete", parent_id)`                           | Clear the loading flag (`_loading[parent_id] = False`).                                                                                                                                          |
| `("incomplete", parent_id)`                         | Set the loading flag (`_loading[parent_id] = True`).                                                                                                                                             |

Ops apply in list order; reparenting in one op is visible to subsequent
ops in the same batch. Unknown ops raise `ValueError` (no silent drop).

##### Positioning (`where` on `upsert`/`set`)

`upsert` and `set` accept an optional 5th tuple element — a positioning
descriptor — that controls where a new (or repositioned) row lands in
the parent's children list. The legacy 4-tuple form remains valid; when
`where` is absent, new ids append at the end and existing ids keep their
current position.

The descriptor is a 2- or 3-tuple `(TYPE, OPTIONS [, REFERENCE])`:

| TYPE       | OPTIONS                       | REFERENCE                                          | Effect                                                                  |
| ---------- | ----------------------------- | -------------------------------------------------- | ----------------------------------------------------------------------- |
| `"first"`  | `None` or `frozenset(...)`    | omitted (or silently ignored)                      | Insert at index 0.                                                      |
| `"last"`   | `None` or `frozenset(...)`    | omitted (or silently ignored)                      | Insert at end.                                                          |
| `"before"` | `None` or `frozenset(...)`    | `int` (child index) or `str` (child id) — required | Insert immediately before the referenced sibling.                       |
| `"after"`  | `None` or `frozenset(...)`    | `int` (child index) or `str` (child id) — required | Insert immediately after the referenced sibling.                        |

`OPTIONS` is a `frozenset` of string flags (or `None` for no flags). One
flag is defined:

- `"reposition"` — apply the position to an existing id. Without this
  flag, an `upsert`/`set` whose id is already present keeps its current
  position; with the flag, it moves to the computed position.

**Reference resolution.** Out-of-range and missing references collapse
to the nearest edge, regardless of direction:

- `int < 0` or missing `str` id → `"before"` becomes `"first"`,
  `"after"` becomes `"last"`.
- `int >= len(children)` → both `"before"` and `"after"` become `"last"`.

So `("before", None, -1)` and `("after", None, -1)` both insert first;
`("before", None, 999)` and `("after", None, 999)` both insert last.

**Same-id pivot.** If the reference resolves to the id being
upserted (e.g. `where=("before", None, "X")` while upserting `"X"`):

- New id (not yet a child) → pivot is missing → falls back to first/last
  by direction.
- Existing id without `"reposition"` → `where` is ignored; existing
  position kept.
- Existing id with `"reposition"` → same-id pivot is a no-op on
  position; fields are still patched (`upsert`) or the item replaced
  (`set`).

**Validation.** A malformed descriptor (wrong tuple length, unknown
keyword, unknown flag, non-int/str reference, missing reference for
`"before"`/`"after"`, etc.) raises `ValueError` and aborts the batch.

**Examples:**

```python
from browse_tui import upsert, set_item

# Prepend a row (newest-first chat history).
op = upsert('msg-42', 'session-9',
            where=('first', None),
            text=msg.text)

# Insert immediately before a known sibling.
op = upsert('log-99', 'log',
            where=('before', None, 'log-100'),
            text=line)

# Insert at a known index.
op = upsert('row-3', 'parent',
            where=('after', None, 2),
            text='…')

# Reposition an existing row to the top.
op = upsert('pinned', 'parent',
            where=('first', frozenset({'reposition'})),
            pinned=True)
```

Use the helper constructors (see *Helper functions* below) rather than
hand-rolling tuples:

```python
from browse_tui import upsert, remove

def cpu_pulse(browser):
    while True:
        time.sleep(1.0)
        ops = []
        for p in psutil.process_iter():
            try:
                cpu = p.cpu_percent()
            except psutil.NoSuchProcess:
                ops.append(remove(str(p.pid)))
                continue
            ops.append(upsert(str(p.pid), None,
                              tag=f'{cpu:.0f}%',
                              tag_style='red' if cpu > 50 else 'dim'))
        browser.update_data(ops)        # one batch, one render
```

##### Visibility (`hidden` flag on `Item` + `mod` op)

`Item.hidden` is a per-row visibility flag (default `False` = visible).
Hidden rows are skipped at render time, and a hidden expandable parent
cascades over its entire subtree — descendants of a hidden ancestor
render nothing, but their own `hidden` values are preserved (revealing
the parent reinstates descendants with their individual states).

Set at row creation via the `hidden=` kwarg on `upsert` / `set_item`:

```python
ctx.upsert('debug-row', 'parent', title='Debug', hidden=True)
```

Toggle dynamically via the **`mod` op** — patch-only, never inserts:

```python
from browse_tui import mod, KEEP_PARENT
ctx.update_data([
    mod('id-1', hidden=True),
    mod('id-2', hidden=True),
    mod('id-3', hidden=False),
])
```

`mod` is patch-only by design: out-of-order or speculative toggles
against ids that haven't arrived yet are silent no-ops, not inserts.
`parent_id` defaults to the `KEEP_PARENT` sentinel ("leave the parent
alone"); pass an explicit id (or `None`) to also reparent the row.

`Context` does **not** expose a `ctx.mod(...)` convenience method —
visibility toggles tend to come in batches, and the batched
`update_data` route encourages efficient composition.

**Cursor on a hidden row.** When `update_data` hides the row the
cursor was on, the framework walks back through the pre-mutation
visible list to find the first row still visible and parks the cursor
there. If no earlier row survives, the cursor lands on the new first
visible row. The walk-back is intentionally separate from the
cursor anchor's fallback chain (which handles deletions): hide
*expects* cursor movement; deletion *preserves* cursor identity.

**Search and hidden compose with AND.** Hidden is absolute — search
mode never elevates a hidden row, including hidden ancestors of
matching descendants. Recipes that want matches to override hide
should flip `hidden` themselves in response to search events.

**Selection and hidden.** Hiding a row does not change
`state.selected` — selection is identity-keyed and stable across
visibility toggles. Hidden rows can be selected via API
(`Browser.select`, `update_data` patches, etc.) regardless of
visibility. The "Select all" keybinding (`Ctrl-A`) is WYSIWYG: it
clears the existing selection and then adds every currently-visible
normal row, so selections of hidden, collapsed-child, or
out-of-scope rows are dropped. "Deselect all" (`Ctrl-N`) clears the
entire selection set.

##### Interactive filter (`&`) — user-driven, stacking

A user-facing filter complements `Item.hidden`. While `hidden` is
**recipe-owned** and cascades over subtrees, the filter is **user-owned**
(via the `&` keybinding), stacks predicates with AND semantics, and uses
*match-promotes-ancestors* — a non-matching parent with a matching
descendant stays visible as scaffolding.

- **`&`** opens the filter prompt and appends a placeholder entry.
- Typing extends the live entry; matches re-evaluate every keystroke.
- **Enter** commits the live entry; the next `&` stacks on top.
- **Enter** on an empty live entry **clears all** filters.
- **Ctrl-X** (within filter-edit) also clears all filters and exits.
- **Ctrl-C** / **Esc** cancel the in-progress edit, keeping committed
  filters.
- Non-overridden keys (arrows, page keys, scope-in/out, …) fall through
  to NORMAL dispatch so the user can navigate while the prompt is open.
  `Enter` is the one carve-out — recipe `on_enter` handlers do not fire
  during filter-edit.

Per-row evaluation is bottom-up: every reachable item gets a framework-
internal `_filter_hidden` flag (recipes never see it). The renderer skips
rows whose flag is `True` *and* `state._filter_active` is `True`. When
the filter is cleared, the flag becomes inert; no O(N) clear pass.

Composition with `Item.hidden`: a row appears iff **neither** layer hides
it. `Item.hidden` cascades over subtrees and is checked first, so a
recipe-hidden row stays hidden even if filter scaffolding wants it.

Recipes can read or replace the filter list:

```python
ctx.filters                    # tuple[str, ...] — committed + live, no empties
ctx.set_filters(['open', 'today'])
ctx.add_filter('high')         # no-op on empty
ctx.clear_filters()            # alias for set_filters([])
```

`set_filters` forces filter-edit exit if active; the in-progress
placeholder is discarded. Recipe writes are authoritative.

See `docs/superpowers/specs/2026-05-17-filter-design.md` for the design
details (mode enum, evaluator, lifecycle table, cursor / selection /
search interaction).

##### Behavioural change vs. pre-streaming API

If a recipe has a `get_children(p)` callback **and** a watcher pushing into
the same parent, today the `get_children` return *appends* (rather than
clobbers) the watcher's pushes. Items may interleave; none are lost. Recipes
needing atomic replace can use `update_data([clear_children(p), …upserts…])`.

#### `browser.nav_home() / browser.nav_end() -> None`

Move the cursor to row 0 (`nav_home`) or the last visible row
(`nav_end`) and **engage a positional pin** so the cursor follows
new arrivals at that edge.

The pin lives in `browser._cursor_anchor` as the `PIN_FIRST` or
`PIN_LAST` module-level sentinel. While pinned:

- Each `update_data` batch re-clamps the cursor to the pinned edge
  via `_apply_cursor_anchor` — new items at the top (or bottom)
  pull the cursor along.
- Hide-displacement is short-circuited; the pin's re-clamp covers
  the case naturally (last row hidden → cursor on new last row).
- The pin survives across mutations as long as the cursor stays at
  the pinned position.

The pin is cleared by any other cursor motion — `j`/`k`, `PgUp`/
`PgDn`, mouse click, `cursor_to(id)`. Re-pressing the opposite edge
(`End` after `Home`, or vice versa) swaps `PIN_FIRST` ↔ `PIN_LAST`.

Typical use case: tail-follow on a streaming log / chat. A recipe
can engage the pin programmatically without simulating a key press:

```python
ctx.nav_end()  # follow new arrivals at the bottom
```

Both methods return `None`; no fetches are required, so there's
nothing to await. They are thread-safe (post onto the main thread).

The matching keybinds (`g`, `Home` → `nav_home`; `End` → `nav_end`)
engage the pin too.

#### `browser.invalidate_preview(id) -> None`

Drop the cached preview text for `id` and re-request a fresh fetch.
Unlike a cursor move, this preserves view state — `_preview_scroll`,
`_preview_at_tail`, and `_help_mode` are all left intact, so a user
who pinned the view to the bottom keeps following the tail as the
re-fetched content arrives.

Use this when the *underlying data* feeding a preview changed but
the cursor stayed on the same row — for example, an umbrella whose
composed body depends on children that just streamed in via
`update_data`, or a file whose content changed on disk. The cursor-
move path (`_update_preview_for_cursor`) treats `_preview_cursor_id
= None` as a fresh-view signal and resets scroll + tail pin;
`invalidate_preview` is the right primitive when that reset is
unwanted.

```python
# Background watcher discovered new bytes in the cursor's file —
# refresh the preview without disrupting tail-follow.
def watcher(b):
    while True:
        time.sleep(1.0)
        if file_changed(path):
            cur = b._preview_cursor_id
            if cur and cur.startswith(path):
                b.invalidate_preview(cur)
```

Thread-safe — posts onto the main thread. Idempotent for the same id.

#### `browser.preview_to_tail() -> None`

Pin the preview view to the bottom of its content. Sets the
`_preview_at_tail` flag on the main thread; the renderer then forces
`_preview_scroll = max_scroll` on every pass while engaged, so the
view follows `append_preview` chunks and generator pulls without
further user input.

The flag is a **sticky user intent** — it only clears on explicit
upward scroll motion: Shift-Up / Alt-Up, Alt-PgUp, Shift-Home /
Alt-Home, or wheel-up over the preview pane.

It survives across:

- Cursor-item changes (the new item also opens at its tail; the
  cursor-change reset zeroes `_preview_scroll` and the renderer's
  pin override snaps to the new `max_scroll` on next paint).
- Help-mode toggle (returning from help leaves the view at the
  preview's tail).
- `invalidate_preview` and other recipe-driven content refreshes.
- Downward motions (Shift-Down, Alt-PgDn, wheel-down, repeat
  Shift/Alt-End) — the renderer clamps so they're no-ops at the
  tail.

```python
ctx.preview_to_tail()  # streaming log tail; new chunks stay visible
```

Symmetric to `nav_end` (which pins the list cursor to the bottom);
the matching keybinds Shift-End / Alt-End engage it too.
Thread-safe — posts onto the main thread.

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

### Op constructors for `update_data`

Six tagged-tuple constructors so recipes don't hand-roll op shapes:

```python
from browse_tui import upsert, set_item, remove, clear_children, complete, incomplete
```

| Helper                                                              | Returns                                                                 |
| ------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `upsert(id, parent_id, *, where=None, **fields)`                    | `("upsert", id, parent_id, fields)` or `(..., where)` if `where`        |
| `set_item(id, parent_id, *, where=None, **fields)`                  | `("set",    id, parent_id, fields)` or `(..., where)` if `where`        |
| `mod(id, parent_id=KEEP_PARENT, *, where=None, **fields)`           | `("mod",    id, parent_id, fields)` or `(..., where)` if `where`        |
| `remove(id)`                                                        | `("remove", id)`                                                        |
| `clear_children(parent_id)`                                         | `("clear_children", parent_id)`                                         |
| `complete(parent_id)`                                               | `("complete", parent_id)`                                               |
| `incomplete(parent_id)`                                             | `("incomplete", parent_id)`                                             |

`where` is keyword-only — it cannot collide positionally with a field
name. See *Positioning* under `browser.update_data` for the descriptor
shape and semantics.

`mod`'s `parent_id` defaults to the `KEEP_PARENT` module-level sentinel —
patch fields without reparenting. See *Visibility* under
`browser.update_data` for the main use case.

The `set` helper is named `set_item` because shadowing the built-in via
`from browse_tui import set` would be hostile to recipes.

See `browser.update_data` above for the per-op semantics.

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
| `upsert`                  | function    |
| `set_item`                | function    |
| `mod`                     | function    |
| `KEEP_PARENT`             | sentinel    |
| `PIN_FIRST`               | sentinel    |
| `PIN_LAST`                | sentinel    |
| `remove`                  | function    |
| `clear_children`          | function    |
| `complete`                | function    |
| `incomplete`              | function    |
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
#!/usr/bin/env -S browse-tui --run-py
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
- [docs/superpowers/specs/2026-05-08-streaming-push-api-design.md](superpowers/specs/2026-05-08-streaming-push-api-design.md) — streaming / push API design rationale.
