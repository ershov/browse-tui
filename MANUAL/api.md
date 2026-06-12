# browse-tui — Python API Reference

The `browse_tui` module exposes a handful of public types — `Browser`,
`Item`, `Action`, `Context`, `BrowserConfig`, `Pending` — plus helper
functions. Recipes import what they need directly:

```python
from browse_tui import Browser, Item, Action
```

The same import works whether the recipe is run via `browse-tui --run-py …`
(in which case `browse_tui` is the running binary, self-injected at startup)
or as part of a regular Python project that has the binary on `sys.path`.

This document is a cross-reference for every public surface. For tutorials
see [recipes.md](recipes.md); for the CLI surface see
[cli.md](cli.md); for the underlying threading model see
[docs/internals.md](../docs/internals.md).

---

## Contents

- [`Item`](#item)
  - [Title fallback](#title-fallback)
  - [Example](#example)
  - [Coercion: `to_item(x)`](#coercion-to_itemx)
- [`Action`](#action)
  - [`requires` gating](#requires-gating)
  - [Example](#example-1)
  - [Key names](#key-names)
- [`Context`](#context)
  - [Selection helpers](#selection-helpers)
  - [Thread-safe ops (pass-through to Browser)](#thread-safe-ops-pass-through-to-browser)
  - [Cache introspection](#cache-introspection)
  - [Push-API pass-throughs](#push-api-pass-throughs)
  - [Worker supersede: `ctx.run_in_slot(name, fn)`](#worker-supersede-ctxrun_in_slotname-fn)
  - [Escape hatches (advanced; unstable surface)](#escape-hatches-advanced-unstable-surface)
  - [Main-thread sub-flows](#main-thread-sub-flows)
- [`Browser`](#browser)
  - [Constructor](#constructor)
  - [Lifecycle hooks](#lifecycle-hooks)
  - [Content channels (stdin / stdout)](#content-channels-stdin--stdout)
  - [Callbacks](#callbacks)
  - [Lifecycle](#lifecycle)
  - [Thread-safe public ops](#thread-safe-public-ops)
- [Plugin system](#plugin-system)
  - [`BrowserConfig`](#browserconfig)
  - [`PluginConfig`](#pluginconfig)
  - [`register_plugin(cfg)` and `registered_plugins`](#register_plugincfg-and-registered_plugins)
  - [Lifecycle hooks](#lifecycle-hooks-1)
  - [Hooking patterns](#hooking-patterns)
  - [`--plugin` CLI flag](#--plugin-cli-flag)
  - [Module discovery (`sys.path`)](#module-discovery-syspath)
- [`Pending`](#pending)
  - [`then(cb)`](#thencb)
  - [`cancel()`](#cancel)
  - [Three usage styles](#three-usage-styles)
- [Helper functions](#helper-functions)
  - [Op constructors for `update_data`](#op-constructors-for-update_data)
  - [`to_item(x) -> Item`](#to_itemx---item)
  - [`parse_input(data, *, fmt, fields=None, record_sep=b'\n', strict=False)`](#parse_inputdata--fmt-fieldsnone-record_sepbn-strictfalse)
  - [`coerce_has_children(raw) -> bool`](#coerce_has_childrenraw---bool)
  - [Cell-accurate string helpers](#cell-accurate-string-helpers)
  - [Styles](#styles)
  - [Module exports](#module-exports)
- [Putting it together](#putting-it-together)
- [See also](#see-also)

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

Three such attributes are honoured by the list renderer when set:

| Attribute  | Type     | Effect |
| ---------- | -------- | ------ |
| `row_bg`   | int (256-color) | Background colour for the whole row (turns the row into a coloured stripe; extends across the trailing pad). |
| `row_fg`   | int (256-color) | Foreground colour for segments that don't specify their own `fg`. Segments with explicit colours keep theirs. Useful for "dim the whole row" / "red row for failed status" effects. |
| `chips`    | `list[(text, style)]` | Trailing coloured chips rendered after the title as ` [text]` segments, each coloured by `style` through the same palette as `tag_style` (`'green'`, `'red'`, … or `''` for plain — see `style(name)` below). Unlike a single `tag`, several chips can follow one title; the colour rides the segment foreground (never embedded in the text) so width math stays correct. Honoured only by the default content handler — a `format_row_content` / `format_row` override is responsible for its own chip layout. |

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
def get_children(_, *, reload=False):
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

Key strings come from the terminal layer (`020-terminal.read_key`), which
decodes raw escape sequences into canonical lowercase names. An `Action.key`
must match the canonical name exactly — dispatch is case-sensitive string
equality, with no aliases, normalization, or patterns (`'Ctrl-R'`, `'C-r'`,
and `'F1'` match nothing; spell them `'ctrl-r'` and `'f1'`).

**Plain keys**

- Single printable characters, as typed: `'a'`, `'A'`, `'/'`, `'?'` —
  Shift+letter is just the uppercase character, there is no `'shift-a'`.
- Named specials: `'enter'`, `'esc'`, `'space'`, `'tab'`,
  `'btab'` (Shift-Tab), `'backspace'`, `'up'`, `'down'`, `'left'`,
  `'right'`, `'home'`, `'end'`, `'pgup'`, `'pgdn'`, `'insert'`,
  `'delete'`, `'f1'` … `'f12'`.

**Modifier prefixes**

Exactly five prefixes exist, in these fixed spellings — the framework never
emits `'shift-ctrl-'`, `'ctrl-alt-'`, or any other permutation:

- single: `'shift-'`, `'alt-'`, `'ctrl-'`
- combined: `'ctrl-shift-'`, `'alt-ctrl-'`

(Alt-Shift and Alt-Ctrl-Shift combinations are not decoded: depending on the
terminal they arrive as the bare key name or are silently ignored.)

What composes with what:

- `'ctrl-a'` … `'ctrl-z'` — note Ctrl-I / Ctrl-M / Ctrl-H arrive as
  `'tab'` / `'enter'` / `'backspace'` (same control byte).
- `'alt-'` + any printable character: `'alt-x'`, `'alt-X'`, `'alt-1'`,
  `'alt-*'`. Alt-Space arrives as the literal `'alt- '` (alt-prefix +
  space character).
- `'alt-ctrl-a'` … `'alt-ctrl-z'`.
- Named specials (arrows, `home`, `end`, `pgup`, `pgdn`, `insert`,
  `delete`, `f1`-`f12`) take all five prefixes, terminal permitting:
  `'shift-up'`, `'ctrl-pgdn'`, `'alt-f6'`, `'ctrl-shift-home'`,
  `'alt-ctrl-left'`, …
- Enter only takes `'shift-enter'` and `'alt-enter'`; Shift-Tab is
  `'btab'`.

**Internal pseudo-keys** (not bindable)

`read_key` also returns names for events the framework consumes itself,
before the action keymap is consulted. They are listed here so you can
recognize them in code and logs — an `Action` bound to one never fires:

- `'mouse-click:ROW:COL'`, `'scroll-up:ROW:COL'`, `'scroll-down:ROW:COL'` —
  mouse reports with the coordinates baked into the name, routed to the
  built-in mouse dispatch (cursor placement, pane scrolling).
- `'_notify'` — wakeup from the internal notification pipe (background ops).
- `'_writable'` — stdout content channel can take more bytes; drains the
  `ctx.print` buffer (see [Content channels](#content-channels-stdin--stdout)).
- `'_stdin'` — stdin content channel has data or hit EOF; pumps the
  `on_stdin` stream.
- `'_unknown'` — unrecognized escape sequence; silently ignored.
- `'_mouse'` — ignored mouse event (button release, right-click, drag, …).

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
ctx.collapse(id)                      -> None
ctx.select(ids, replace=False)        -> None
ctx.flash(text, log=False)            -> None
ctx.log(text)                         -> None
ctx.error(text)                       -> None
ctx.print(text, end='\n')             -> None
ctx.quit(code=0, output='')           -> None
```

| Method       | What it does                                                         |
| ------------ | -------------------------------------------------------------------- |
| `refresh`    | Refetch one parent's children (or full root if id is None).          |
| `cursor_to`  | Move cursor to id; resolves once positioned (best-effort).           |
| `expand`     | Add id to expanded; trigger fetch if not cached.                     |
| `collapse`   | Remove id from expanded; fold its subtree (no-op if not expanded).   |
| `select`     | Add ids to selection (or replace).                                   |
| `flash`      | Transient info-bar notice; `log=True` also records it in the log.    |
| `log`        | Append to the message log silently (no on-screen notice).           |
| `error`      | Red, sticky info-bar notice; always logged; cleared by next keypress.|
| `print`      | Write to the stdout content channel (see *Output — `ctx.print`*).    |
| `quit`       | Exit the main loop with `code`; `output` joins the stdout channel after any `print` (strict FIFO). |

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
ctx.preview_width                     -> int    # preview pane cols (0 if hidden / no tty)
ctx.preview_to_tail()                 -> None    # pin preview to bottom
ctx.nav_home()                        -> None    # cursor -> row 0 + PIN_FIRST
ctx.nav_end()                         -> None    # cursor -> last row + PIN_LAST
ctx.collapse(id)                      -> None    # remove one id from expanded
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
ctx.hint                              -> str     # info-bar hint line
ctx.set_hint(text)                    -> None    # replace info-bar hint; repaints
ctx.run_in_worker(fn)                 -> threading.Thread
```

`upsert` / `set_item` / `remove` are convenience wrappers for the single-op
case (each routes through `update_data` with a one-element list); for
multiple ops, prefer `update_data` directly so the batch stays atomic.

`run_in_worker(fn)` spawns a one-shot daemon thread, surfacing any
uncaught exception via `browser.error`. The thread handle is mostly
informational — synchronisation should be done via `Pending` or
`threading.Event` inside `fn`.

### Worker supersede: `ctx.run_in_slot(name, fn)`

For workers whose latest call should replace any in-flight earlier
call (live-as-you-type recompute, cancellable tail refresh), use
`run_in_slot` instead. Each call returns a `CancellationToken`;
re-submitting the same `name` cancels the prior token.

```python
ctx.run_in_slot(name: str, fn) -> CancellationToken

class CancellationToken:
    def is_cancelled() -> bool
    def cancel() -> None
```

The function signature is `fn(token)` — `fn` receives the token
and must poll `token.is_cancelled()` cooperatively at safe points.
The framework does **not** kill threads.

```python
def slow_search(token):
    for row in scan():
        if token.is_cancelled():
            return                       # bail out promptly
        ctx.append_preview(cursor_id, render(row))

# Each keystroke replaces the running worker (wire via on_search_change).
def on_search_change(ctx, query):
    ctx.run_in_slot('preview-search', slow_search)
```

Exceptions inside `fn` are routed to `browser.error` exactly like
`run_in_worker`.

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

A `Browser` takes a single `BrowserConfig` dataclass holding every
construction parameter:

```python
from browse_tui import Browser, BrowserConfig

b = Browser(BrowserConfig(
    title='browse-tui',
    get_children=lambda _, *, reload=False: [],            # (parent_id) -> Iterable[Item|str|tuple|dict]
    get_preview=None,                     # (item_id) -> str  (optional)
    actions=None,                         # list[Action]
    on_enter=None,                        # default Enter handler; see below
    format_row=None,                      # advanced: whole-row display override
    format_row_chrome=None,               # advanced: selection marker + indent + expander
    format_row_content=None,              # advanced: content region (id + tag + title + chips)
    root_id=None,
    initial_scope=None,
    show_preview=True,
    show_children_pane=True,
    multi_select=True,
    print_format='{id}',
    show_ids='auto',                      # 'always' | 'auto' | 'never'
    preview_ansi=True,                    # honour SGR colour codes in preview
))
```

`BrowserConfig` is a dataclass — adding a new construction parameter
means adding a field there. Plugins that need to influence
construction read or mutate the same dataclass in their
`on_before_init` hook (see *Plugin system* below).

#### `show_ids`

Controls whether the per-row id segment is rendered in front of the title.

| Value      | Behaviour                                                              |
| ---------- | ---------------------------------------------------------------------- |
| `'always'` | Always emit `'<id> <title>'` (yellow id, then title).                  |
| `'auto'`   | Default. Emit the id only when `str(item.id) != item.title`. The line-based CLI shape (`Item(id='README.md')`) renders as just `'README.md'`; tracker-style sources (`Item(id=42, title='Implement feature')`) render as `'42 Implement feature'`. |
| `'never'`  | Never emit the id segment.                                             |

A `format_row` or `format_row_content` hook that doesn't emit the id segment
overrides this entirely — a set hook's segments are emitted verbatim (the
default content handler is the one that consults `show_ids`).

### Lifecycle hooks

These optional callback kwargs let recipes react to framework events
without polling. Every hook takes `ctx` followed by **the subject of
the change** — there is no `(ctx)`-only hook. Recipes can still read
the same data off `ctx` (`ctx.cursor`, `ctx.selected`, `ctx.filters`,
…); the payload is the convenient, uniform path. All are optional
`BrowserConfig` fields, mirrored as `Browser(...)` kwargs, `None` by
default and a no-op when unset:

```python
Browser(...,
        # observed state
        on_cursor_change=cb,     # (ctx, id)     cursor row id changed (debounced)
        on_selection_change=cb,  # (ctx, ids)    state.selected changed
        on_scope_change=cb,      # (ctx, scope_id, prev_scope_id, direction)
        on_quit=cb,              # (ctx, code)   shutdown, after screen restore
        # tree structure
        on_expand=cb,            # (ctx, ids)    ids newly expanded this drain
        on_collapse=cb,          # (ctx, ids)    ids newly collapsed this drain
        on_children_loaded=cb,   # (ctx, parent_ids)  fetches that settled this drain
        # input state / geometry
        on_search_change=cb,     # (ctx, query)  effective search query changed
        on_filter_change=cb,     # (ctx, filters)  active filter tuple changed
        on_resize=cb,            # (ctx, cols, rows)  terminal resized
        # content channel (NOT drain-time; see Content channels below)
        on_stdin=cb)             # (ctx, data, *, delimiter, is_eof, errno)
```

| Hook | Signature | When it fires | Notes |
| ---- | --------- | ------------- | ----- |
| `on_cursor_change` | `(ctx, id)` | At most once per main-loop tick; only when the row id under the cursor differs from the last fire. | `id` is the cursor row id, or `None` on a placeholder / empty list. Rapid moves coalesce. Re-anchor moves that land on the same id are silent. |
| `on_selection_change` | `(ctx, ids)` | After every change to `state.selected` (Space, alt-Space, Ctrl-A, Ctrl-N, `select_all_visible` / `clear_selection` / `invert_selection` / `select`). No-op calls (e.g. `clear` on an already-empty set) are silent. | `ids` is the resulting selected id list, in selection (insertion) order. |
| `on_scope_change` | `(ctx, scope_id, prev_scope_id, direction)` | After a successful `scope_into` / `scope_out` transition. | `scope_id` is the new scope, `prev_scope_id` the one just left — either is `None` at the root. `direction` is `'in'` (scope_into) or `'out'` (scope_out). See *scope direction* below. |
| `on_quit` | `(ctx, code)` | Once during shutdown, after the screen is restored, before `Browser.run` returns. | `code` is the exit code stashed by `quit()`. Use for worker / file-handle / temp-file cleanup. Exceptions are swallowed silently — a failing cleanup must not block exit. |
| `on_expand` | `(ctx, ids)` | When one or more ids newly enter `state.expanded` (the collapse→expand transition). | `ids` is the list expanded this drain (≥1): length 1 for a single `→`/`l`, longer for Alt-Right / `expand_subtree` / a recursive expand. Re-pressing `→` on an already-expanded node fires nothing. Children may or may not be cached at fire time — see *cached vs. uncached* below. |
| `on_collapse` | `(ctx, ids)` | When one or more ids newly leave `state.expanded`. Symmetric with `on_expand`. | `ids` batches the burst: `←`/`h` (length 1), a recursive collapse, or `collapse_all` (the whole expanded set in one call). |
| `on_children_loaded` | `(ctx, parent_ids)` | When one or more `get_children` fetches **settle** (a parent's loading flag flips True→False). | `parent_ids` batches every parent that settled this drain (≥1). Per id, `ctx.cached_children(pid)` returns the full list (possibly `[]`). The source-agnostic counterpart to `ctx.expand(id).then(cb)`. See firing rules below. |
| `on_search_change` | `(ctx, query)` | When the effective search query changes — live per keystroke in search-edit mode, and on `set_search_query` / `clear_search`. | `query` is the new string (also `ctx.search_query`). Debounced to one fire per drain on the final value; clearing to `''` fires once. |
| `on_filter_change` | `(ctx, filters)` | When the committed-plus-live filter list changes (`&` typing/commit/clear, `set_filters` / `add_filter` / `clear_filters`). | `filters` is the `tuple[str, ...]` also returned by `ctx.filters`. Debounced per drain; an identical re-set is a no-op; `add_filter('')` is a no-op. |
| `on_resize` | `(ctx, cols, rows)` | When the terminal dimensions change (SIGWINCH path). | `cols` / `rows` are the new dimensions. Lets recipes drop width-dependent caches up front rather than lazily on the next `get_preview`. |
| `on_stdin` | `(ctx, data, *, delimiter, is_eof, errno)` | As bytes arrive on the stdin content channel during the run (not a state transition). | The streaming-input channel — a different mechanism from the observed-state hooks above. Full contract in [Content channels](#content-channels-stdin--stdout) below. |

The observed-state hooks (everything except `on_stdin`) are
**source-agnostic** (they fire on the state transition, not the
keystroke — keyboard, mouse, programmatic call, and startup
auto-expands all count) and **drain-time / debounced** (coalesced to
one fire per main-loop tick; set / burst events deliver the whole
burst as one list). `on_stdin` is neither: it is driven by the select
loop as input arrives, one delivery per chunk / record — see
[Content channels](#content-channels-stdin--stdout). Every hook except
`on_quit` routes exceptions to `Browser.error` (a red info-bar message)
and never crashes the loop; `on_quit` swallows silently so a failing
cleanup can't block exit.

> **Note:** `on_expand` / `on_collapse` are source-agnostic about the
> *gesture* too — insert-mode marker movement that re-roots the tree
> can land or remove ids from `state.expanded` and therefore fire
> them. Treat the payload as "what changed", not "what the user pressed".

Typical patterns:

```python
def on_cursor_change(ctx, id):
    if id is None:
        return
    log.info(f'cursor: {id}')

def on_scope_change(ctx, scope_id, prev_scope_id, direction):
    if direction == 'in':
        ctx.flash(f'scoped into {scope_id} (from {prev_scope_id})')

def on_expand(ctx, ids):
    log.debug('expanded %s', ids)

def on_quit(ctx, code):
    _STOP_EVENT.set()        # tell worker threads to wind down
    for f in _OPEN_HANDLES:
        f.close()
```

#### `on_scope_change` direction and `prev_scope_id`

A scope transition carries both endpoints and a direction, so a recipe
can branch on scope-*in* vs scope-*out* without overriding keys or
tracking the prior scope itself:

- `scope_into(child)` → `direction == 'in'`; `scope_id` is the new
  scope, `prev_scope_id` is the scope left. The initial scope-in from
  the root passes `prev_scope_id is None`.
- `scope_out()` → `direction == 'out'`; scoping out to the root passes
  `scope_id is None`.

```python
def on_scope_change(ctx, scope_id, prev_scope_id, direction):
    if direction == 'in':
        ctx.invalidate_preview(scope_id)   # cross-file preview refresh
```

#### Cached vs. uncached expansion (composition pattern)

`on_expand` and `on_children_loaded` are orthogonal events. At
`on_expand` fire time a node's children **may or may not be cached** —
an expand of an uncached node kicks an async fetch, and only that
fetch later fires `on_children_loaded`. Expanding an **already-cached**
node fires `on_expand` but **not** `on_children_loaded` (no fetch ran).

A recipe that wants to "act once an expanded node's children are
visible" composes the two — `on_expand` stashes the intent,
`on_children_loaded` fulfils it. Unlike `ctx.expand(id).then(cb)`
(which only fires for an expand *the recipe itself* issued), this works
for user-driven expansion and correctly excludes refresh / prefetch
arrivals:

```python
_awaiting = set()

def on_expand(ctx, ids):
    for id in ids:
        if ctx.cached_children(id) is not None:
            _react(ctx, id)            # children already cached: act now
        else:
            _awaiting.add(id)          # fetch in flight: defer

def on_children_loaded(ctx, parent_ids):
    for pid in parent_ids:
        if pid in _awaiting:
            _awaiting.discard(pid)
            _react(ctx, pid)
```

`on_children_loaded` firing rules:

- **Settles via fetch only.** A cached expand does not fire it.
- **Empty result fires** with `cached_children(pid) == []`; a `None`
  return does **not** fire until the recipe later clears loading.
- **Errors fire** — a `get_children` exception becomes `[]` at the
  worker boundary, loading clears, the hook fires with an empty list.
- **Refresh refires.** `ctx.refresh(parent)` invalidates then
  refetches → fires again on the new completion. A full `ctx.refresh()`
  preserves `state.expanded` (so no expand/collapse fires) but
  refetches every expanded parent, batching them as each settles.

#### Scope transitions do not fire expand/collapse

`scope_into` / `scope_out` save and restore per-scope expanded sets. A
scope transition is an `on_scope_change` event, **not** a burst of
expansions: immediately after the transition restores `state.expanded`
the framework re-baselines its expand/collapse diff, so the restored
ids are not reported as fresh expands or collapses. `on_scope_change`
fires (with its direction); `on_expand` / `on_collapse` stay silent.
The next genuine expand after a transition fires normally.

#### Firing order within a drain

The only cross-hook ordering guarantee is that **`on_cursor_change`
fires last, after expansion has settled** — so a cursor-change handler
sees the post-expansion tree with freshly-delivered children accounted
for. Beyond that, do not depend on the relative order of hooks:
`on_scope_change` and `on_selection_change` fire synchronously at their
mutation sites (as the scope / selection change is applied), while
`on_resize`, `on_search_change`, `on_filter_change`, `on_collapse`,
`on_expand`, `on_children_loaded`, and `on_cursor_change` fire in a
post-drain settle pass. Each hook still fires at most once per logical
change per drain.

### Content channels (stdin / stdout)

With the UI on its own terminal device, the process's `stdin` and
`stdout` are free to carry content while the UI runs. `stderr` is left
untouched — an escape hatch for diagnostics; tracebacks still reach it.
The CLI-user-facing side of this is in
[cli.md](cli.md#content-channels-stdin-in-stdout-out); the recipe-author
APIs are below.

**Initial input** is plain `sys.stdin` read *before* `Browser.run()` —
there is no framework API, and none is possible (`ctx` does not exist
yet). Slurp (`sys.stdin.read()`), iterate lines (`for line in
sys.stdin`), or split records (`sys.stdin.buffer.read().split(b'\0')`)
in the recipe's setup, then build the tree. On a tty the read blocks
until `^D` (standard `cat` behaviour). stdin is one-shot — `Ctrl-R`
reload re-serves the parsed in-memory data, so no re-read is needed.

#### Output — `ctx.print`

```python
ctx.print(text, end='\n')             -> None
```

Mirrors builtin `print` (`text` is `str()`-coerced and newline-terminated
unless `end` overrides it) and appends to a single output buffer shared
with `ctx.quit`'s `output`, so everything is delivered in strict **FIFO**
order — prints first, then the quit output. It **never blocks the UI**;
delivery depends on what `stdout` is:

- **pipe / file:** drained live by the event loop as the consumer keeps
  up — a slow / backpressuring reader does not stall the UI.
- **tty:** held for the whole session and written to normal scrollback at
  exit, after the UI closes (the `fzf` model — a bare interactive run
  still prints its result, with the selection last).
- **consumer gone (`EPIPE`):** the channel is dead for the rest of the
  session — the buffer is dropped and later `ctx.print` calls are no-ops.
  The UI stays alive (it is on the terminal device, independent of stdout).

`ctx.print` is thread-safe (callable from worker threads).

#### Streaming input — `on_stdin`

To consume stdin **live while the UI runs** (a tail-feed, a record
stream), opt in via three `BrowserConfig` fields:

```python
Browser(...,
        on_stdin=handle,                 # the hook
        stdin_delimiter=None,            # None = raw chunks; or a delimiter
        stdin_want_bytes=False)           # False = decoded str; True = bytes
```

The hook signature — the framework **always** passes every keyword:

```python
def on_stdin(ctx, data, *, delimiter, is_eof, errno):
    ...
```

- **`data`** — `str` (or `bytes` when `stdin_want_bytes=True`), **never
  `None`**; may be empty (`''` / `b''`, e.g. an empty record). `str` is
  decoded with an **incremental utf-8 decoder** (`errors='replace'`), so a
  multibyte sequence split across chunk boundaries decodes correctly and
  invalid bytes become U+FFFD. Empty `data` flows through typical
  processing as zero records, so a hook that ignores the flags never
  crashes on it.
- **`stdin_delimiter`** selects the framing:
  - **`None` (raw-chunk mode):** one call per chunk as it arrives;
    `delimiter` is always `''` (or `b''`).
  - **a delimiter** (`'\n'` for lines, `'\0'` for NUL records,
    multi-char supported): the framework owns partial-record buffering and
    delivers **one complete record per call**, delimiter stripped, with the
    stripped delimiter passed back in `delimiter`. **Empty records are
    preserved** — `"a\n\n"` delivers record `"a"`, then `""`, then the EOF
    call. The delimiter's **type must match the data mode** — `str` in the
    default mode, `bytes` when `stdin_want_bytes=True`; a type mismatch (or
    an empty delimiter of either type) raises **`ValueError` at
    construction**.
- **`is_eof`** — `True` on the **final** call; its `data` is the trailing
  *unterminated* record (or empty if the stream ended on a delimiter), with
  `delimiter` empty. Record mode is therefore unambiguous about whether a
  trailing partial existed.
- **`errno`** — `0` on every normal call; on a read error the stream ends
  with a final call carrying the numeric errno **and** `is_eof=True`
  (error implies no more data, so a hook that only checks `is_eof` handles
  error-end correctly for free).

After the EOF / error call the stream **never resumes** — fd 0 leaves the
select set for good. The hook typically folds incoming data into the tree
via `ctx.update_data` / `ctx.upsert`. Exceptions are caught and routed to
`ctx.error` (like the observed-state hooks).

**Fairness:** the keyboard outranks the stream — a saturating producer
(a `yes`-style firehose) still leaves the session quittable, because
`read_key` reads the terminal before the loop pumps another stdin chunk.

**Cost when unset:** zero. With no `on_stdin` the select read-set is
exactly as without the feature; an ended stream costs one attribute check
per loop iteration and then drops out entirely.

**Composing initial reads with streaming.** Streaming begins *where the
synchronous ingest left off*. The framework reads via `sys.stdin.buffer`,
so a pre-run read on the **bytes** layer (`sys.stdin.buffer`) composes
losslessly — read a header synchronously, then stream the records, with no
bytes lost across the hand-off. A partial **text-layer** read
(`sys.stdin.read(n)` / `sys.stdin`) does **not** compose: the text wrapper
hides its residue. Read to EOF, or use the bytes layer, when combining a
synchronous read with `on_stdin`.

> `on_stdin` is unavailable in headless runs and under `--tty -` (there
> fd 0 *is* the UI device). A tty stdin in normal mode is detached to
> `/dev/null` at startup, so the hook simply receives an immediate EOF
> call through the same code path as a closed pipe.

> **Signature note:** as with every hook, the framework does not validate
> the signature in this release — a wrong-arity `on_stdin` is the recipe
> author's bug (caught the moment the hook first fires, surfaced via
> `ctx.error`).

### Callbacks

#### `get_children(parent_id, *, reload=False) -> Iterable[Item|str|tuple|dict] | Generator | None`

Required (in practice). Called per parent-being-expanded, on a worker thread.
Return any iterable of items in any of the four shapes accepted by `to_item`.
Mixed lists are fine. The result is cached until `ctx.refresh(parent_id)` or
`browser.refresh(parent_id)` invalidates it.

For the very first call, `parent_id` is `root_id`.

`reload` is `True` only on the root refresh enqueue (Ctrl-R / `ctx.refresh()`
with no id) — a single signal that means "drop any recipe-internal caches
and rebuild from scratch." Every other call (expand, auto-prefetch,
re-dispatched expanded ids during the same refresh) passes `reload=False`;
the framework's own `cache_invalidate_all` already wiped `state._children`
so per-id re-signalling would be redundant.

Errors raised inside `get_children` are caught at the worker boundary: the
parent's children become `[]` (preventing retry storms), the error is
surfaced via the info bar, and any `Pending` waiting on the fetch still
resolves (callback chains keep firing).

```python
def get_children(parent_id, *, reload=False):
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
def get_children(parent_id, *, reload=False):
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

#### Row-format hooks

Each item row is a list of `(text, fg, bold)` segments. Three optional,
individually-overridable hooks let a recipe shape that list — each
`(item, ctx) -> [(text, fg, bold), …]`, where `ctx` is a `RowContext`
(below):

| Hook                 | Region it owns                                            |
| -------------------- | --------------------------------------------------------- |
| `format_row`         | The **whole row** (total control).                        |
| `format_row_chrome`  | The structural prefix: selection marker + indent + expander. |
| `format_row_content` | The content region: id + tag + title + chips (or arbitrary columns). |

The default composition is `format_row = format_row_chrome +
format_row_content`. Chrome stays framework-owned unless explicitly
overridden, so a recipe overrides **only `format_row_content`** to render
its own columns *while keeping the tree* (indent + `▼`/`▶`). Most recipes
leave all three alone.

**Resolution is by config, not by return value, and bound once.** A hook
left unset (`None`) uses the framework default for that part; a hook that
*is* set owns its return completely. There is no magic `None`-return
sentinel — a set hook always returns real segments. The hooks are resolved
once in `Browser.__init__` (after `on_before_init` plugin hooks fire), so
the per-row render path never tests a hook against `None`.

To build "the default, plus a tweak" — or to column-format the common rows
and fall back for the odd ones (an error row, a "working tree clean" row) —
call the matching **public default handler**, edit the list it returns, and
return that:

```python
from browse_tui import default_row_content, cell_ljust, style

def fs_row_content(item, ctx):
    if getattr(item, 'col_perms', None) is None:
        return default_row_content(item, ctx)   # error / synthetic row
    dfg, dbold = style('dim')
    return [
        (cell_ljust(item.col_perms, 10) + '  ', dfg, dbold),
        (item.title, None, False),               # flexible column, last
    ]

Browser(BrowserConfig(get_children=…, format_row_content=fs_row_content))
```

Put the *flexible* column (the name / subject) **last** in the segment
list: the renderer truncates left-to-right, so a narrow pane trims that
column and leaves the fixed metadata columns intact.

#### Public default handlers

The framework's stock builders, exported so a hook can wrap them rather
than reimplement them (importable from `browse_tui`):

- **`default_row_chrome(item, ctx) -> [(text, fg, bold), …]`** — the
  selection marker (`'* '`/`'  '`), indentation, and expander
  (`'▼ '`/`'▶ '`/`'  '`) segments.
- **`default_row_content(item, ctx) -> [(text, fg, bold), …]`** — the id
  segment (gated by `show_ids`), the `tag` chip, the title (with the
  `is_current_scope` → `scope_title` override), and the trailing `chips`.
- **`default_row(item, ctx) -> [(text, fg, bold), …]`** — returns
  `default_row_chrome(item, ctx) + default_row_content(item, ctx)` and sets
  `ctx.content_width` along the way, so a whole-row `format_row` override
  can call it, tweak the result, and return it. It composes the *framework*
  defaults (not any other resolved hook).

#### `RowContext`

The per-row handle passed to all three hooks (distinct from the action
`Context`). Built fresh per painted row; carries read-only per-row state and
the live pane geometry:

| Field              | Meaning                                                    |
| ------------------ | ---------------------------------------------------------- |
| `depth`            | tree depth of the row.                                     |
| `selected`         | `bool` — row is in the selection.                          |
| `expanded`         | `bool` — row is expanded.                                  |
| `is_current_scope` | `bool` — this item *is* the current scope root.            |
| `kind`             | the visible-entry kind (`'normal'` for hook rows).         |
| `parent_id`        | the id of this row's parent (or `None`).                   |
| `list_width`       | content width of the list pane in cells.                   |
| `content_width`    | cells left for `format_row_content` after the chrome on this row. |

`content_width` starts equal to `list_width` and is lowered to
`list_width − cells(chrome)` once the default composer has measured the
chrome (so a `format_row_content` hook reads the room left after the
prefix). Under a whole-row `format_row` override it stays equal to
`list_width` (the chrome split is unknown). Both dimensions are `0` before
the first paint / in headless tests (matching the `preview_width` contract);
pick a fallback explicitly (`ctx.list_width or 80`).

Advanced escape hatch (mirrors `Context.browser`, unstable surface):

- **`ctx.browser`** — the underlying `Browser`, for capabilities not yet on
  `RowContext`.

### Lifecycle

#### `Browser.run() -> int`

Start workers, set up the terminal, run the main loop, tear down. Blocks
until `ctx.quit()` (or `q`/`Esc`). Returns the exit code stashed via `quit`
(or the cancel code 1 from a default-quit).

#### `recipe_argv(argv=None) -> list`

Return `argv` (default `sys.argv[1:]`) with the framework-owned terminal-device
flag removed — `--tty VALUE` (value is the following token, consumed too) and
`--tty=VALUE`. `run()` auto-detects that flag but leaves it in `sys.argv`, so a
recipe scanning its own positionals should read from this instead, or it would
misread `--tty` / its value (`-` or a `/dev/pts/N` path) as one of its own
arguments. Returns a fresh list; `sys.argv` is left untouched on purpose — `run()`
still reads `--tty` from it to resolve the device.

```python
from browse_tui import recipe_argv

args = recipe_argv()            # this recipe's own positionals, --tty dropped
root = args[0] if args else '.'
```

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
browser.collapse(id)                       -> None   # remove id from expanded; fold subtree
browser.nav_home()                         # cursor → row 0; engage PIN_FIRST
browser.nav_end()                          # cursor → last row; engage PIN_LAST
browser.select(ids, replace=False)
browser.flash(text, log=False)
browser.log(text)
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

`update_data`, `set_preview`, `append_preview`, and `clear_preview` all
schedule a callable on the post queue that mutates state on the main
thread; their writes become visible (and trigger a paint) on the next
drain — no keystroke required. Since #431 `set_preview` shares this FIFO
lane: multiple writes accumulate (every call lands), and ordering with
`append_preview` / `clear_preview` is the simple FIFO of the post queue.
The framework's preview worker still delivers its own `get_preview`
results via a separate single-slot lane (`_preview_result`); see
`browser.set_preview` for worker-vs-recipe race semantics.

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

**What the filter sees.** Evaluation is scoped to the **currently
visible tree** — the rows that would be on screen if the filter were
off. A node passes iff it self-matches against every active entry, *or*
it has at least one visible matching descendant (the scaffold rule).
Two consequences worth knowing:

- The filter never looks inside collapsed nodes. A collapsed parent is
  judged on its own text alone; the framework does not eagerly fetch
  uncached subtrees to find matches. Expand a parent to evaluate its
  children.
- Newly revealed rows (streamed in via `update_data` under a visible
  expanded parent, or revealed by an `expand` the user just performed)
  are evaluated as they appear, with the change propagated up through
  visible ancestors only.

**Recompute triggers.** The filter recomputes on:

- filter change (typing, `set_filters`, `clear_filters`);
- scope change (`scope_into` / `scope_out`);
- `expand` of a previously-collapsed parent (newly-revealed subtree
  only — the expanded parent's own flag is preserved);
- each `update_data` op landing under a visible expanded parent
  (per-op walk-up, early-terminating at the first stable ancestor).

It does **not** recompute on collapse, cursor movement, or `update_data`
ops landing under a collapsed / uncached parent. As a result, a
scaffold-visible parent stays visible after the user collapses it
(stale-scaffold contract); the flag catches up on the next filter
change.

Per-row evaluation writes a framework-internal `_filter_hidden` flag
(recipes never see it). The renderer skips rows whose flag is `True`
*and* `state._filter_active` is `True`. When the filter is cleared, the
flag becomes inert; no O(N) clear pass.

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

See `docs/superpowers/specs/2026-05-27-filter-visible-tree-only-design.md`
for the evaluator and recompute-trigger rationale, and
`docs/superpowers/specs/2026-05-17-filter-design.md` for the keybindings,
mode enum, and recipe API (its evaluator / triggers sections are
superseded by the 05-27 design).

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

#### `browser.preview_width -> int`

Width of the preview pane in terminal columns, refreshed on every
render pass. Reads a cached value the renderer updates from the live
layout — so reads are O(1) and don't recompute geometry, but resizes,
split / ratio changes, and `show_preview` toggles take effect on the
next paint (and are visible to the next `get_preview` fetch).

Returns `0` until the first paint, while the preview pane is hidden,
or when terminal geometry can't be read (headless tests, no tty). Pick
an explicit fallback when a non-zero value is required:

```python
def get_preview(item_id):
    text = read_markdown(item_id)
    width = ctx.preview_width or 80
    return md2ansi(text, line_width=width)
```

Also accessible as `ctx.preview_width`. Safe to read from any thread.

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

## Plugin system

Plugins are ordinary Python modules that extend the framework or a
specific recipe. Two loading channels:

* **Recipe-driven:** a recipe `import`s a plugin module like any
  other helper. Standard Python — no framework cooperation needed
  beyond `BROWSE_TUI_PLUGIN_PATH`-style `sys.path` setup (covered
  automatically — see *Module discovery* below).
* **User-driven:** `browse-tui --plugin SPEC [--plugin SPEC ...]`
  loads plugins at launch time regardless of what the main recipe
  expects. SPEC is either a module name or a filesystem path.

A plugin module can optionally call `register_plugin(PluginConfig(...))`
to receive lifecycle callbacks. A plugin that only writes into a
shared registry at module-body time (`preview_formatters['markdown']
= my_format`, etc.) needs no `register_plugin` call at all.

### `BrowserConfig`

```python
@dataclass
class BrowserConfig:
    title: str = 'browse-tui'
    hint: str = ' /:search  ?:help  q:quit '   # info-bar hint line
    get_children: Callable | None = None
    get_preview:  Callable | None = None
    actions: list | None = None
    on_enter: Any = None
    format_row: Callable | None = None             # (item, ctx) -> segments
    format_row_chrome: Callable | None = None      # (item, ctx) -> segments
    format_row_content: Callable | None = None     # (item, ctx) -> segments
    root_id: Any = None
    initial_scope: Any = None
    show_preview: bool = True
    show_children_pane: bool = True
    preview_ansi: bool = True
    list_ratio: float = 0.30
    split: str = 'auto'
    multi_select: bool = True
    print_format: str = '{id}'
    help_intro: str | None = None
    help_outro: str | None = None
    show_ids: str = 'auto'
    show_scope_crumb: bool = False
    preview_buffer_cap_chars: int = 100_000
    preview_buffer_cap_lines: int = 1000
    on_cursor_change: Callable | None = None      # (ctx, id)
    on_scope_change: Callable | None = None        # (ctx, scope_id, prev_scope_id, direction)
    on_selection_change: Callable | None = None    # (ctx, ids)
    on_expand: Callable | None = None              # (ctx, ids)
    on_collapse: Callable | None = None            # (ctx, ids)
    on_children_loaded: Callable | None = None     # (ctx, parent_ids)
    on_search_change: Callable | None = None       # (ctx, query)
    on_filter_change: Callable | None = None       # (ctx, filters)
    on_resize: Callable | None = None              # (ctx, cols, rows)
    on_quit: Callable | None = None                # (ctx, code)
    on_stdin: Callable | None = None               # (ctx, data, *, delimiter, is_eof, errno)
    stdin_delimiter: str | bytes | None = None     # None = raw chunks; else record delimiter
    stdin_want_bytes: bool = False                  # True = bytes (data + delimiter), not str
    _headless: bool = False
```

Every field corresponds to a Browser construction parameter — see
the *Constructor* section for the per-field semantics.

### `PluginConfig`

```python
@dataclass
class PluginConfig:
    name: str | None = None
    on_before_init: Callable[[Browser, BrowserConfig], None] | None = None
    on_after_init:  Callable[[Browser], None] | None = None
    on_before_run:  Callable[[Browser], None] | None = None
    on_after_run:   Callable[[Browser], None] | None = None
```

All fields optional. If `name` is `None` at `register_plugin` time,
it's filled from the calling module's `__name__`.

### `register_plugin(cfg)` and `registered_plugins`

```python
from browse_tui import PluginConfig, register_plugin, registered_plugins

def _setup(browser):
    browser.bind('M', show_markdown_help)

register_plugin(PluginConfig(name='markdown-preview', on_before_run=_setup))
```

`registered_plugins` is the live list of registrations, in
registration order. Public and fully mutable — plugins may inspect,
reorder, remove, or replace entries to compose with each other.
Multiple calls from the same module are allowed; each appends a
separate entry.

`Context.register_plugin(cfg)` is the pass-through. Calling it
during a Browser's lifetime registers for *future* Browser
constructions — the current Browser's `__init__` hooks have already
fired.

### Lifecycle hooks

| Hook | When | What you get |
| ---- | ---- | ------------ |
| `on_before_init` | Top of `Browser.__init__`, after defaults are loaded into `BrowserConfig` and before any construction. | `(browser, config)`. `browser` is essentially empty — treat it as identity only. Mutate `config` to override defaults the recipe set. |
| `on_after_init`  | Bottom of `Browser.__init__`, after the Browser is fully built. | `(browser)`. Read or monkey-patch the live Browser. |
| `on_before_run`  | Top of `Browser.run`, right before the event loop starts. | `(browser)`. Workers are already running; final-mile setup. |
| `on_after_run`   | Inside a `finally` at the end of `Browser.run`, even when the loop exits via exception. | `(browser)`. Cleanup. May replace an in-flight exception per Python's `finally` semantics. |

Hooks fire in registration order on each pass. Missing hooks (`None`)
are skipped silently. Exceptions propagate unchanged — the framework
does not catch / log / continue.

### Hooking patterns

Several patterns cover most extension needs:

1. **Populate a shared registry at module-body time.** Cheapest for
   simple "add a thing" plugins:

   ```python
   from browse_tui import preview_formatters
   preview_formatters['markdown'] = render_markdown
   ```

   No hooks, no `register_plugin` call.

2. **Override `BrowserConfig` defaults in `on_before_init`.** When
   the plugin wants to set defaults the recipe will then read:

   ```python
   def _set_defaults(browser, config):
       if config.preview_formatter is None:
           config.preview_formatter = render_markdown
   register_plugin(PluginConfig(on_before_init=_set_defaults))
   ```

3. **Wrap callable fields on `BrowserConfig`.** Compose instead of
   replace:

   ```python
   def _wrap(browser, config):
       prev = config.preview_formatter
       def chained(item):
           text = prev(item) if prev else item.body
           return decorate_with_markdown(text)
       config.preview_formatter = chained
   ```

4. **Monkey-patch the Browser instance in `on_after_init`.** Works
   for replacing methods, adding new methods, or attaching plain
   attribute data:

   ```python
   def _patch(browser):
       orig = browser.set_preview
       def wrapped(id_, text):
           return orig(id_, render_markdown(text))
       browser.set_preview = wrapped
       browser.markdown = MarkdownState()
   ```

5. **Monkey-patch the `Browser` class at module-body time.** Apply
   to every Browser; useful for tests and global rewrites. Class-
   level attributes are a convenient namespace for cross-Browser
   registries:

   ```python
   Browser.preview_formatters = {}
   ```

6. **Compose with another plugin via `registered_plugins`.** Find
   the target by name and wrap its hooks:

   ```python
   for cfg in registered_plugins:
       if cfg.name == 'syntax-highlight':
           orig = cfg.on_after_init
           def wrapped(browser):
               if orig: orig(browser)
               attach_extra_lexer(browser)
           cfg.on_after_init = wrapped
   ```

### `--plugin` CLI flag

```
browse-tui --plugin foo --plugin bar --run-py recipe.py
browse-tui --plugin /opt/tools/markdown_helper.py --run recipe.py
browse-tui -c cmd -p cmd --plugin foo            # CLI mode (no recipe file)
```

* Repeatable. Order is preserved.
* `SPEC` is either a module name (no `/`, no `.py`) or a filesystem
  path. Paths are loaded via `importlib.util.spec_from_file_location`
  and named after their basename without the `.py` suffix; module
  names go through `importlib.import_module`.
* Plugins load before the main recipe / TUI mode starts.
* Combining `--plugin` with `--run-cli` (or `--run` auto-detected as
  `cli`) exits with a hard error before any import or `execvpe`.
  External CLI recipes replace the Python process; plugins would be
  discarded. Pass `--plugin` to the inner `browse-tui` invocation
  inside the recipe script instead.

### Module discovery (`sys.path`)

At process start, the launcher prepends three locations to
`sys.path`, in order:

1. The directory containing the running `browse-tui` binary.
2. The directory of the main Python recipe (when there is one).
3. For each path-form `--plugin SPEC`: that file's parent directory.

This makes the natural "drop file in directory" distribution work
three ways: alongside the binary, alongside the recipe, or wherever
a path-form plugin lives.

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
        lambda: ctx.flash('navigated')))
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

### Cell-accurate string helpers

For recipes assembling their own row segments (via the row-format hooks
above), these format **plain text** measured in **display cells** — wide
characters (CJK / emoji) count as 2. Carry colour via the segment
`fg`/`bold`, never embedded SGR, so the width math stays exact.

```python
from browse_tui import cell_width, cell_fit, cell_ljust, cell_rjust, cell_center, cell_trim
```

| Helper                                                              | Returns                                                                       |
| ------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `cell_width(s) -> int`                                              | Display-cell width of `s`.                                                    |
| `cell_ljust(s, width, fill=' ') -> str`                            | Pad right to `width` cells; `s` unchanged if already wider.                   |
| `cell_rjust(s, width, fill=' ') -> str`                            | Pad left to `width` cells.                                                    |
| `cell_center(s, width, fill=' ') -> str`                           | Pad both sides to `width` cells.                                              |
| `cell_trim(s, width, *, where='end', ellipsis='…', word_boundary=False) -> str` | Trim to `width` cells (no-op if it fits); ellipsis placed at the `'end'` (`'abc…'`), `'start'` (`'…xyz'`), or `'middle'` (`'ab…yz'`). |
| `cell_fit(s, width, *, justify='left', trim='end', ellipsis='…', fill=' ', word_boundary=False) -> str` | The one-call column formatter: `cell_trim` if too wide, else pad to **exactly** `width` cells per `justify`. |

`cell_ljust`/`cell_rjust`/`cell_center` and `cell_trim` are the primitives;
`cell_fit` is the combinator recipes reach for most — it always returns
exactly `width` cells. The `ellipsis` defaults to `'…'` (1 cell); pass
`'...'` for three dots. `word_boundary` (middle trim only) prefers a space
near the cut. `fill` and `ellipsis` must each be a single cell.

```python
cell_fit('a long title', 8)                      # 'a long …'
cell_fit('42', 6, justify='right')               # '    42'
cell_fit('mid', 9, justify='center')             # '   mid   '
cell_trim('/very/long/path', 10, where='start')  # '…long/path'
```

### Styles

A segment's colour *is* a raw `(fg, bold)` pair — `fg` a 256-colour palette
index (or `None` for the terminal default), `bold` a bool. The `tag_style` /
chip vocabulary is the set of *named* styles mapping onto those pairs;
recipes building their own segments resolve them through `style()`:

```python
from browse_tui import style, STYLE_NAMES, MARKER_FG, ID_FG, DIM_FG
```

- **`style(name) -> (fg, bold)`** — resolve a named style
  (`'green'`/`'red'`/`'yellow'`/`'gray'`/`'cyan'`/`'blue'`/`'magenta'`/
  `'dim'`/`''`) to the raw `(fg, bold)` pair `tag_style` / chips use. An
  unknown name (or `''`) returns `(None, False)` (plain), matching tag
  rendering's fallback.
- **`STYLE_NAMES`** — a `frozenset` of the valid named-style keys (mirrors
  the vocabulary documented on `Item.tag_style`); use it to validate or
  enumerate.
- **`MARKER_FG`** (`4`, blue `▼`/`▶`), **`ID_FG`** (`3`, yellow `#id`),
  **`DIM_FG`** (`242`, the `'dim'` fg) — the semantic palette constants the
  default chrome uses, exposed so columns can match it without magic
  numbers.

```python
dfg, dbold = style('dim')                  # (242, False)
seg = (cell_ljust('-rw-r--r--', 10), dfg, dbold)
```

A segment author writes either a named style (`fg, bold = style('dim')`) or
a raw value directly (`(text, DIM_FG, False)`, or any 256-colour int). The
named vocabulary is the recommended, stable colour API; raw ints are the
escape hatch for colours outside the palette.

### Module exports

What actually lives at `browse_tui.<name>`:

| Name                      | Kind        |
| ------------------------- | ----------- |
| `Browser`                 | class       |
| `Item`                    | dataclass   |
| `Action`                  | dataclass   |
| `Context`                 | class       |
| `RowContext`              | class       |
| `Pending`                 | class       |
| `to_item`                 | function    |
| `parse_input`             | function    |
| `coerce_has_children`     | function    |
| `default_row`             | function    |
| `default_row_chrome`      | function    |
| `default_row_content`     | function    |
| `cell_width`              | function    |
| `cell_ljust`              | function    |
| `cell_rjust`              | function    |
| `cell_center`             | function    |
| `cell_trim`               | function    |
| `cell_fit`                | function    |
| `style`                   | function    |
| `STYLE_NAMES`             | frozenset   |
| `MARKER_FG`               | int         |
| `ID_FG`                   | int         |
| `DIM_FG`                  | int         |
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

def get_children(path, *, reload=False):
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
- [cli.md](cli.md) — CLI surface.
- [recipes.md](recipes.md) — shipped recipes.
- [docs/internals.md](../docs/internals.md) — module layout, threading model.
- [docs/superpowers/specs/2026-04-30-browse-tui-design.md](../docs/superpowers/specs/2026-04-30-browse-tui-design.md) — original design spec.
- [docs/superpowers/specs/2026-05-08-streaming-push-api-design.md](../docs/superpowers/specs/2026-05-08-streaming-push-api-design.md) — streaming / push API design rationale.
