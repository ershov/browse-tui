# browse-tui — Cursor Pin (`PIN_FIRST` / `PIN_LAST`) Design

**Date:** 2026-05-17
**Status:** Draft (pre-implementation)
**Extends** the sticky cursor anchor with a positional-pin tier that
overrides id-based anchoring while the user has explicitly chosen
"go to first / last".

## Motivation

The existing sticky cursor anchor follows an *id*: the cursor stays on
the same logical row even when its index shifts. Excellent for keeping
the user oriented during streaming inserts that don't directly affect
their attention.

But there's a class of UX patterns where the user *wants* index-based
stickiness, not id-based:

- **Tail-follow.** "Show me the latest." Real-time log viewers, chat
  apps, build output. After pressing `End`, the user wants the cursor
  to follow new appends, not stick on the row that *used to be* last.
- **Top-follow.** Same pattern for "newest at top" recipes (chat
  history, search results re-ranked over time, undo stacks).

Today, pressing `End` and watching new items arrive at the bottom
leaves the cursor on what *was* the last row — the user has to press
`End` again every time data changes.

A *positional* pin captures the intent of `Home`/`End`: stay at the
edge, regardless of which id is at the edge.

## Goals

1. After `g`/`Home` the cursor sticks to row 0 across mutations.
2. After `End` the cursor sticks to the last row across mutations.
3. Any other navigation drops the pin and reverts to id-based anchoring.
4. Recipes can engage/disengage the pin programmatically (no need to
   simulate a key press).
5. The pin overrides hide-displacement (the row-walk-back). A hidden
   last row shouldn't strand the cursor on a non-last row.

## Non-goals

- Other positional pins (e.g., "row N", "follow cursor row index").
  Could be added later; not in scope.
- Persistence across runs. Each `browse-tui` invocation starts
  unpinned.

---

## API

### Sentinels

Two module-level singletons, exported alongside `KEEP_PARENT`:

```python
class _AnchorSentinel:
    __slots__ = ('_kind',)
    def __init__(self, kind): self._kind = kind
    def __repr__(self): return f'<PIN_{self._kind.upper()}>'

PIN_FIRST = _AnchorSentinel('first')
PIN_LAST  = _AnchorSentinel('last')
```

The sentinels live in the cursor anchor list at position 0 in place
of an id:

```python
browser._cursor_anchor = [PIN_FIRST]   # pinned to row 0
browser._cursor_anchor = [PIN_LAST]    # pinned to last row
```

A single-tier anchor — no fallback chain needed; the sentinel always
resolves to a position.

### `Browser.nav_home() / Browser.nav_end()`

Thread-safe public methods that move the cursor and engage the pin:

```python
def nav_home(self) -> None:
    """(thread-safe) Move cursor to row 0 and pin it there.

    Posts a callable that sets ``state.cursor = 0`` and
    ``_cursor_anchor = [PIN_FIRST]``. The cursor follows new arrivals
    at the top until any non-home/non-end navigation clears the pin.
    Returns None — there is nothing to await.
    """
    def _do():
        self._state.cursor = 0
        self._cursor_anchor = [PIN_FIRST]
        mark_cursor_changed(self)
        self._needs_redraw.add('list')
    self.post(_do)


def nav_end(self) -> None:
    """(thread-safe) Move cursor to the last visible row and pin it there.

    See ``nav_home``.
    """
    def _do():
        vis = visible_items(self._state)
        self._state.cursor = max(0, len(vis) - 1)
        self._cursor_anchor = [PIN_LAST]
        mark_cursor_changed(self)
        self._needs_redraw.add('list')
    self.post(_do)
```

### `Context.nav_home() / Context.nav_end()`

Pass-throughs:

```python
def nav_home(self) -> None:
    self._browser.nav_home()

def nav_end(self) -> None:
    self._browser.nav_end()
```

### Action-layer integration

`_nav_home` / `_nav_end` in `070-actions.py` are already wired to
`g`/`Home`/`End`. They run synchronously on the main thread, so they
call internal helpers `_do_nav_home` / `_do_nav_end` directly instead
of posting (which would defer to the next tick):

```python
def _nav_home(ctx):
    """Jump cursor to the first row and pin it there."""
    b = ctx._browser
    b._state.cursor = 0
    b._cursor_anchor = [PIN_FIRST]
    mark_cursor_changed(b)


def _nav_end(ctx):
    """Jump cursor to the last visible row and pin it there."""
    b = ctx._browser
    vis = visible_items(b._state)
    b._state.cursor = max(0, len(vis) - 1)
    b._cursor_anchor = [PIN_LAST]
    mark_cursor_changed(b)
```

The post-action `_reanchor_cursor` in `_handle_one_key` sees the
sentinel anchor and preserves it (see *Pin lifecycle*).

### Module exports

Added to the public surface:

| Name        | Kind     | Purpose                                          |
| ----------- | -------- | ------------------------------------------------ |
| `PIN_FIRST` | sentinel | Anchor pinned to row 0.                          |
| `PIN_LAST`  | sentinel | Anchor pinned to the last visible row.           |

---

## Semantics

### Pin lifecycle

The pin is engaged by `_nav_home`/`_nav_end` (key path) or
`Browser.nav_home()`/`Browser.nav_end()` (API path). Both set
`_cursor_anchor = [PIN_FIRST]` (or `[PIN_LAST]`) along with the
cursor move.

The pin is preserved by:

- **`_reanchor_cursor`** when the cursor is still at the pinned row
  (row 0 for `PIN_FIRST`, last row for `PIN_LAST`).
- **`_apply_cursor_anchor`** — the sentinel always wins; no walk to
  later tiers (since there are none). After resolving the cursor to
  the pinned row, the pin is kept.
- **`Browser.update_data._apply`** — the pin takes precedence over
  hide-displacement (see below).

The pin is cleared by:

- **`_reanchor_cursor`** when the cursor is *not* at the pinned row
  (the user moved away with `j`/`k`/`Click`/`PgUp`/`PgDn`/`cursor_to`).
  In that case `_reanchor_cursor` falls through to its normal
  id-based snapshot capture.
- **Explicit re-pin to the *other* edge** — pressing `End` while
  pinned to `PIN_FIRST` replaces with `[PIN_LAST]` (and vice versa).
- **Recipe `cursor_to(id)`** — sets an id-based anchor, displacing
  the pin.

### `_reanchor_cursor` update

```python
def _reanchor_cursor(self):
    cur = self._cursor_anchor
    if cur and isinstance(cur[0], _AnchorSentinel):
        pin = cur[0]
        vis = visible_items(self._state)
        c = self._state.cursor
        if vis:
            target = 0 if pin is PIN_FIRST else len(vis) - 1
            if c == target:
                return    # pin survives
        # cursor moved away from the pin → drop pin
    snap = self._compute_anchor_snapshot()
    if snap:
        self._cursor_anchor = snap
```

### `_apply_cursor_anchor` update

```python
def _apply_cursor_anchor(self) -> bool:
    if not self._cursor_anchor:
        return False
    vis = visible_items(self._state)
    if not vis:
        return False
    first = self._cursor_anchor[0]
    if isinstance(first, _AnchorSentinel):
        new_i = 0 if first is PIN_FIRST else len(vis) - 1
        if self._state.cursor != new_i:
            self._state.cursor = new_i
            mark_cursor_changed(self)
            self._snap_list_scroll_to_row(new_i)
        return True   # pin always counts as a primary hit
    # Existing id-based walk follows.
    ...
```

### Pin vs hide-displacement

In `Browser.update_data._apply`, the pin short-circuits
hide-displacement:

```python
if self._cursor_anchor and isinstance(self._cursor_anchor[0], _AnchorSentinel):
    # Pin owns cursor position — skip hide-displacement.
    self._apply_cursor_anchor()
else:
    self._apply_hide_displacement(pre_vis_ids, pre_cursor)
    self._apply_cursor_anchor()
```

Rationale: hide-displacement's walk-back finds the row just above the
hidden one — useful for id-based anchoring (preserving "the row the
user was on"). With a positional pin the user said "stay at the edge",
which means re-clamp to the new row 0 / row N-1.

### Interaction with empty visible list

If `visible_items` is empty, `_apply_cursor_anchor` returns False
without touching the cursor. The pin remains in `_cursor_anchor`. As
soon as a row arrives, the next `_apply_cursor_anchor` call lands the
cursor at the pinned edge.

### Search mode

Search-driven cursor motion (`/` query + Enter / `n` / `N`) sets an
id-based cursor via `cursor_to` — the pin clears naturally through
the existing reanchor path. No special-case needed.

### Mouse interaction

Mouse click on a list row goes through the cursor-to-row dispatcher
(`_dispatch_mouse` in `070-actions.py`); this performs an id-based
cursor change, so the pin clears via the standard reanchor path.

Wheel scroll doesn't move the cursor — it adjusts `_list_scroll` —
so it doesn't engage or clear the pin. Same as today's behavior for
the expand-goal: the wheel is a viewport gesture, not a cursor
gesture.

---

## Test plan

Tests live in `test/async_/test_browser.py` (anchor-style class) and
`test/unit/test_multiselect.py` (action wiring) is unaffected.

### Sentinel basics

- `PIN_FIRST is not PIN_LAST` (distinct singletons).
- `repr(PIN_FIRST) == '<PIN_FIRST>'`, ditto for `PIN_LAST`.
- Importable: `from browse_tui import PIN_FIRST, PIN_LAST`.

### `nav_home` / `nav_end` API

- `b.nav_home()` posts a main-thread callable; after `run_until_idle`,
  cursor is at 0 and anchor is `[PIN_FIRST]`.
- `b.nav_end()` posts → cursor at last and anchor is `[PIN_LAST]`.
- Empty list → cursor at 0, anchor still `[PIN_FIRST]` (waits for arrival).

### Pin follows arrivals

- Pinned to first; `update_data` inserts a new item at position 0 →
  cursor lands on the new first row.
- Pinned to last; `update_data` appends → cursor lands on the new
  last row.
- Pinned to first; existing first row is hidden (or removed) → cursor
  on the new first visible.
- Pinned to last; existing last row is hidden → cursor on the new
  last visible.
- Pinned to last; everything hidden → cursor parks; next arrival
  re-engages.

### Pin clears on other motion

- Pinned to first; `j` moves cursor down one → pin gone, anchor is
  id-based with the new cursor row's id at tier 0.
- Pinned to first; mouse click on row 2 → pin gone.
- Pinned to first; `PgDn` → pin gone.
- Pinned to first; `cursor_to('X')` → pin gone (X becomes anchor).
- Pinned to first; press `End` → pin replaced with `[PIN_LAST]`.

### Pin and hide-displacement

- Pinned to last; cursor on last row; last row is hidden in a batch →
  cursor lands on new last visible row (pin won, walk-back skipped).
- Not pinned; cursor on a middle row; that row is hidden →
  hide-displacement runs normally (walk-back finds previous row).

### Recipe-level API

- `Browser.nav_home` / `Browser.nav_end` are callable from a non-main
  thread; the action lands on the main thread after `drain_main_queue`.
- `Context.nav_home` / `Context.nav_end` forward correctly.

### Action-layer wiring (regression)

- `dispatch_key(b, ctx, 'g')` sets `_cursor_anchor = [PIN_FIRST]`.
- `dispatch_key(b, ctx, 'home')` does the same.
- `dispatch_key(b, ctx, 'end')` sets `[PIN_LAST]`.
- `dispatch_key(b, ctx, 'j')` after pin → pin cleared.

---

## Open questions

None.

---

## Implementation outline (informational)

1. **State layer** (`src-tui/040-state.py`)
   - Define `_AnchorSentinel` class + `PIN_FIRST` / `PIN_LAST`
     singletons near `KEEP_PARENT`.
   - Update `_apply_cursor_anchor` to short-circuit on sentinel.
   - Update `_reanchor_cursor` to preserve sentinel iff cursor at the
     pinned row.
   - Add `Browser.nav_home()` / `Browser.nav_end()` (thread-safe
     post-and-mutate).
   - Update `Browser.update_data._apply` to skip hide-displacement
     when a pin is active.

2. **Action layer** (`src-tui/070-actions.py`)
   - `_nav_home` / `_nav_end` set `_cursor_anchor = [PIN_FIRST/LAST]`
     directly (already on main thread; no `post`).

3. **Context** (`src-tui/060-context.py`)
   - `Context.nav_home()` / `Context.nav_end()` pass-throughs.

4. **Tests** (per plan above).

5. **Docs** (`docs/api.md`)
   - Add `Browser.nav_home` / `Browser.nav_end` and `Context` pass-throughs.
   - Add `PIN_FIRST` / `PIN_LAST` to module-exports table.
   - Add a paragraph on the pin's behavior (where to look when
     wondering why the cursor follows the edge).
