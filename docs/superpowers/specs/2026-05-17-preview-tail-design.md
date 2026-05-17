# browse-tui — Preview Tail-Follow (`_preview_at_tail`) Design

**Date:** 2026-05-17
**Status:** Draft (pre-implementation)
**Companion to** the cursor pin (`PIN_FIRST` / `PIN_LAST`) — same
intent ("stay at the edge as new content arrives"), applied to the
preview pane instead of the list.

## Motivation

The cursor pin makes the list follow the top/bottom edge as items
stream in. The preview pane has an analogous need but only on the
bottom edge: streaming previews (generator-driven, `append_preview`
from watcher threads, log tails) keep growing, and the user who
pressed "end" wants the latest content visible without re-pressing
End on every chunk.

The top edge needs no equivalent: preview content doesn't get
prepended in a way that pushes line 0 down. A scroll of 0 already
sticks to the start as content grows below.

## Goals

1. After Shift/Alt-End the preview view sticks to the new bottom as
   `wrapped` grows.
2. Any upward scroll motion drops the tail-follow.
3. Downward motions (already no-ops at the bottom) leave tail-follow
   engaged.
4. Item-change and help-toggle resets clear tail-follow alongside
   `_preview_scroll`.
5. Recipes can engage tail-follow programmatically.

## Non-goals

- Top-edge follow. No need; line 0 is stable.
- Tail-follow for the children grid pane. Different geometry; out of
  scope.
- Persistence across runs.

---

## API

### State

One boolean field on `Browser`, defaulting `False`:

```python
self._preview_at_tail = False
```

No module export, no sentinel class. The flag is purely an internal
viewport intent.

### `Browser.preview_to_tail()`

Thread-safe public method mirroring `Browser.nav_end`:

```python
def preview_to_tail(self) -> None:
    """(thread-safe) Pin the preview view to the bottom of its content.

    Posts a callable that sets ``_preview_at_tail = True`` and flags
    the preview pane dirty. The renderer overrides ``_preview_scroll``
    to ``max_scroll`` on every pass while the flag is set, so the
    view follows ``append_preview`` chunks and generator pulls
    without further user input.

    The flag clears automatically on any upward scroll motion
    (Shift/Alt-Up, page-up, Home, wheel-up), on cursor-item change,
    and on help-mode toggle. Returns None.
    """
    def _do():
        self._preview_at_tail = True
        self._needs_redraw.add('preview')
    self.post(_do)
```

### `Context.preview_to_tail()`

Pass-through:

```python
def preview_to_tail(self) -> None:
    self._browser.preview_to_tail()
```

---

## Semantics

### Engaging the flag

`_preview_end` (key path, bound to Shift-End / Alt-End) sets the
flag directly on the main thread:

```python
def _preview_end(ctx):
    b = ctx._browser
    b._preview_at_tail = True
    b._needs_redraw.add('preview')
```

The `10**9` scroll sentinel from the prior implementation goes away
— the renderer's pin override handles the "snap to bottom" semantics.

### Renderer pin override

In `render_preview` (`050-render.py`), after computing
`max_scroll = max(0, len(wrapped) - content_lines)`:

```python
if browser._preview_at_tail:
    scroll = max_scroll
    if browser._preview_scroll != max_scroll:
        browser._preview_scroll = max_scroll
else:
    scroll = max(0, min(browser._preview_scroll, max_scroll))
    if scroll != browser._preview_scroll:
        browser._preview_scroll = scroll
```

The write-back is essential: subsequent `_preview_scroll_up` reads
`_preview_scroll` and decrements, expecting a sensible value. With
tail-follow engaged the renderer keeps `_preview_scroll` synchronized
to `max_scroll` on every pass.

### Clearing the flag

Tail-follow is a *sticky* user intent. Once engaged it survives
across every kind of content/cursor change until the user explicitly
scrolls up.

The flag is cleared by:

| Site                       | Trigger                                       |
| -------------------------- | --------------------------------------------- |
| `_preview_scroll_up`       | Shift-Up — explicit upward motion             |
| `_preview_page_up`         | Alt-PgUp — explicit upward motion             |
| `_preview_home`            | Shift/Alt-Home — explicit top jump            |
| Wheel-up handler           | Wheel-up over the preview pane                |

The flag is *not* cleared by:

- `_preview_scroll_down` / `_preview_page_down` — downward motions
  at the tail are no-ops; the flag's intent matches.
- Wheel-down — same reasoning as page-down.
- `_preview_end` (re-engagement is idempotent).
- `append_preview`, `set_preview`, `clear_preview` — these update
  content; the flag survives so the view follows.
- **Cursor-item change**: the new item also opens at its tail (the
  cursor-change reset zeroes `_preview_scroll`, but the renderer's
  pin override snaps it to the new `max_scroll` on next paint).
- **Help-mode toggle**: returning from help shows the preview at
  its tail.
- **`Browser.invalidate_preview(id)`** (recipe-driven cache
  invalidation): the cache is dropped and re-fetched while the pin
  is preserved.

### Clearing logic shape

Each clearing action looks like:

```python
def _preview_scroll_up(ctx):
    b = ctx._browser
    b._preview_at_tail = False
    if b._preview_scroll > 0:
        b._preview_scroll -= 1
        b._needs_redraw.add('preview')
```

The flag is cleared unconditionally before the scroll-position
mutation. If the flag was engaged, the prior render wrote
`_preview_scroll = max_scroll`, so the decrement steps one row above
the bottom — the user's expected "step back one row from the tail"
behavior.

The cursor-change site in `040-state.py` (around the `_preview_scroll
= 0` assignment) adds `self._preview_at_tail = False` next to it.
`_toggle_help` in `070-actions.py` does the same.

### Interaction with `set_preview` / `append_preview`

Streaming pushes only mutate `_state._preview[id_]` and mark the
preview dirty. They don't touch the flag. On the next render the
recomputed `wrapped` length feeds a new `max_scroll`, and the flag
makes `scroll` track it.

### Recipe-driven cache invalidation: `Browser.invalidate_preview(id)`

When the *underlying data* feeding a preview changes but the cursor
hasn't moved — e.g., an umbrella row whose composed body depends on
children that just streamed in via `update_data` — recipes use
`Browser.invalidate_preview(id)`. That primitive drops the cached
text and re-requests a fetch while **preserving** view state
(`_preview_scroll`, `_preview_at_tail`, `_help_mode`).

Recipes must *not* clear the cache by setting `_preview_cursor_id =
None`. The cursor-move reset path (`_update_preview_for_cursor`)
treats that as "cursor moved → fresh view" and clobbers the tail
pin alongside scroll/help. `invalidate_preview` is the cursor-stays-
still alternative.

### Empty preview / no item

If `wrapped` is empty, `max_scroll = 0` and `scroll = 0` regardless
of the flag. The flag stays set; subsequent appends will engage it
once content arrives.

If the cursor leaves all items (`new_id is None`), the cursor-change
reset clears the flag.

### Search-mode / scope-mode

Neither affects the preview pane's scroll axis. The flag survives
search query edits and scope push/pop.

### Mouse interactions

Wheel-up over preview → clear (via the wheel handler). Wheel-down →
no change. Click on a preview row doesn't exist (preview is
read-only). Click on a list row drives an id-based cursor move,
which goes through the cursor-change reset and clears the flag.

---

## Test plan

Tests live in `test/async_/test_preview_tail.py` (new file). UI test
optional (`test/ui/test_preview_tail.py`).

### Flag basics

- `Browser._preview_at_tail` defaults `False`.
- `b.preview_to_tail()` posts → after `run_until_idle`, flag is
  `True` and preview pane is flagged dirty.

### Engagement

- `dispatch_key(b, ctx, 'shift-end')` sets the flag and renders the
  last lines of a multi-line preview.
- `dispatch_key(b, ctx, 'alt-end')` same.
- `_preview_scroll` and `max_scroll` agree after render with the
  flag set.

### Tail-follow

- Flag engaged; `append_preview(id, 'extra\n' * 50)` posts → after
  `run_until_idle` + render, the view scrolls to the new bottom.
- Flag engaged; preview generator yields more chunks → view follows.
- Flag engaged; `set_preview(id, '')` (preview emptied) → scroll
  goes to 0, flag remains, next append re-engages.

### Clearing — upward motions

- Flag engaged; Shift-Up → flag clears, scroll decremented.
- Flag engaged; Alt-PgUp → flag clears, scroll decremented by a page.
- Flag engaged; Shift-Home → flag clears, scroll at 0.
- Flag engaged; wheel-up over preview → flag clears.

### Clearing — downward motions don't clear

- Flag engaged; Shift-Down → flag *stays* engaged.
- Flag engaged; Alt-PgDn → flag stays engaged.
- Flag engaged; wheel-down → flag stays engaged.
- Flag engaged; Shift-End again → flag stays engaged (idempotent).

### Clearing — cursor / help

- Flag engaged; cursor moves to another item → flag clears,
  `_preview_scroll` resets to 0.
- Flag engaged; `_toggle_help` → flag clears, scroll resets.

### Recipe-level API

- `Browser.preview_to_tail` is callable from a non-main thread; the
  flag lands on the main thread after `drain_main_queue`.
- `Context.preview_to_tail` forwards correctly.

### Render-side invariant

- Flag engaged; render twice with growing content → `_preview_scroll`
  reads back as the latest `max_scroll` value on each pass.

---

## Open questions

None.

---

## Implementation outline (informational)

1. **State layer** (`src-tui/040-state.py`)
   - Initialize `self._preview_at_tail = False` in `Browser.__init__`.
   - Clear it in the cursor-change reset (next to `_preview_scroll = 0`).
   - Add `Browser.preview_to_tail()` thread-safe method.

2. **Renderer** (`src-tui/050-render.py`)
   - Apply the pin override after computing `max_scroll`.

3. **Action layer** (`src-tui/070-actions.py`)
   - `_preview_end`: set flag, drop the `10**9` write.
   - `_preview_scroll_up`, `_preview_page_up`, `_preview_home`:
     clear flag.
   - `_toggle_help`: clear flag.
   - Wheel-up branch in `_dispatch_mouse`: clear flag.

4. **Context** (`src-tui/060-context.py`)
   - `Context.preview_to_tail` pass-through.

5. **Tests** (`test/async_/test_preview_tail.py`, optional UI test).

6. **Docs** (`docs/api.md`)
   - Document `Browser.preview_to_tail` / `Context.preview_to_tail`
     in the public API section.
   - Note the auto-clearing behavior alongside the existing preview
     scroll actions.
