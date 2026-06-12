# browse-tui — Modal Dialogs Design

**Date:** 2026-06-12
**Status:** Draft (pre-implementation)

## Motivation

The framework's interactive sub-flows today live on borrowed screen
real estate: `ctx.prompt` / `ctx.confirm` draw on the info bar, and
`ctx.pick` overlays its option list on the preview pane. That works,
but the picker's look and feel are poor (options render where preview
content belongs, geometry depends on which panes happen to be visible),
and there is no primitive at all for context menus, alerts, or
free-form notifications.

This design adds a single **modal dialog** facility: one overlay window
at a time, drawn over the regular UI, that blocks all other repaints
and receives all input until it is closed. Target dialog kinds:

- **Picker** — fzf-style filtered selection (replaces the current
  preview-pane picker behind `ctx.pick`).
- **Choice** — button row for confirmation / single choice
  (Yes/No/Cancel-style; backs `ctx.confirm`).
- **Input** — single-line text entry (backs `ctx.prompt`).
- **Menu** — context menu anchored near the cursor (machinery only;
  nothing populates it yet).
- **Message** — static free-form text with a single dismiss (recipe
  notifications; later, the surface for recipe error reporting).

## Goals

- One blocking modal window at a time. No stacking, no z-order.
- While open: no pane repaints, all keys go to the dialog.
- Clean restore on close: the regular UI repaints fully with no
  leftover dialog cells.
- Replace the implementations behind `ctx.pick` / `ctx.confirm` /
  `ctx.prompt` (contracts kept; no backward-compatibility constraint
  on internals).
- New recipe-facing APIs for choice, menu, and message dialogs.

## Non-goals (this round)

- Non-modal / persistent floating windows (would require z-order and
  clipping in the pane renderers).
- Scrolling inside dialogs. Message text is wrapped and vertically
  clipped; pickers/menus keep the selection in view by windowing, but
  there is no free user-driven scrolling.
- Mouse interaction inside dialogs (mouse events are swallowed).
- Wiring recipe-exception reporting into the message dialog
  (follow-up once the API exists).
- Anything that populates the context menu (hooks, default entries).
- A settings page / widget toolkit / layout engine. The design merely
  avoids precluding it: a future content kind plugs into the same
  frame-and-loop machinery.

## Background: rendering constraints

The renderer has no compositor. Panes tile the screen; each pane
renderer writes escape sequences directly into its `Rect`, and a
per-pane row cache (`PaneCache`) makes repaints differential. Two
consequences for any overlay (both already proven out by the current
preview-pane picker, whose `_restore` documents them):

1. **Direct writes bypass the row cache.** After the overlay closes, a
   plain `render_full` would cache-hit on the overdrawn rows and emit
   nothing — the overlay would survive on screen. The cache must be
   dropped on close.
2. **A fresh cache means no trailing clears.** With cleared caches the
   renderer takes its "first paint" branch, which assumes cells are
   already blank. The overlay's cells must therefore be explicitly
   blanked before requesting the full repaint.

Because the dialog is fully modal, everything else is easy: the dialog
runs a nested `read_key` loop, so the main loop never paints while it
is open, and input routing needs no dispatcher changes.

## Approaches considered

**A. Independent loop functions per dialog kind.** Five siblings of
today's `_pick_on_info_bar`, each owning its read-key loop, sharing
only draw helpers. Simple, but the subtle parts — `_notify` handling,
resize, close/restore, cache interaction — would be duplicated five
times, and they are exactly the parts that are easy to get wrong.

**B. One shared modal loop + small content objects (chosen).** A
single `run_modal` owns the lifecycle (geometry, frame drawing, key
loop, `_notify`, resize, restore); each dialog kind is a small content
object with a three-method surface (measure, draw, handle key). The
lifecycle logic exists once; adding a dialog kind means adding a
content class. This is also the seam a future settings page would
plug into.

**C. A widget/layout toolkit.** Rows of focusable widgets, layout
engine, focus management. Rejected: none of the target dialogs needs
it, and it can be layered on top of B later if the settings page ever
materializes.

## Design

### Module

New file `src-tui/055-modal.py`, concatenated between `050-render.py`
(whose `Rect`, cell-width helpers, and `style` it uses) and
`060-context.py` (whose `ctx` methods delegate to it). Public surface:

- `run_modal(browser, content, *, placement='center', anchor=None,
  _read_key=None) -> result`
- Content classes: `PickerContent`, `ChoiceContent`, `InputContent`,
  `MenuContent`, `MessageContent`.
- Convenience wrappers used by `ctx`: `modal_pick`, `modal_choice`,
  `modal_input`, `modal_menu`, `modal_message`.

`_read_key` is the same test-injection seam the current picker has: a
callable returning decoded key names, defaulting to the terminal
layer's `read_key`.

### Content protocol

A content object provides:

- `title` — optional string embedded in the top border.
- `measure(max_w, max_h) -> (w, h)` — desired content size given the
  maximum available content area. Called at open and after a terminal
  resize; never between keystrokes (the frame does not jiggle while
  the user types).
- `draw(rect)` — paint the content rows into the given content `Rect`
  with direct terminal writes. Called after every loop iteration.
  Every row must be fully painted (content + fill to width) so stale
  cells from the previous iteration cannot survive; no diffing.
- `handle_key(key) -> (done, result)` — process one key. `(False, _)`
  continues the loop; `(True, result)` closes the dialog and makes
  `run_modal` return `result`.

The shared loop handles three things before the content sees a key:

- `'esc'` / `'ctrl-c'` — uniform cancel: close and return `None`
  (for `ChoiceContent` the `ctx.confirm` wrapper maps `None` to
  `False`).
- `'_notify'` — drain background work (`browser.drain_main_queue()`,
  `browser.apply_children_results()`) **without rendering**, then
  re-check geometry. Redraw flags that drained work sets in
  `browser._needs_redraw` simply accumulate; the close-time `'all'`
  absorbs them.
- Mouse events (`mouse-click:*`, `scroll-*`) — swallowed.

Everything else goes to `handle_key`; unrecognized keys are ignored by
the content.

### Frame, placement, sizing

The frame is a single-line box (`┌─┐│└┘`) in the same dim/gray styling
as the pane separators, with the title (when present) embedded in the
top border and rendered bold:

```
┌─ Confirm ────────────────────────────┐
│ Delete 3 items?                      │
│                                      │
│          [ Yes ]   [ No ]            │
└──────────────────────────────────────┘
```

The content area is the interior minus a one-column pad on each side.
Total frame size is therefore `content_w + 4` × `content_h + 2`.

Two placement strategies (the `placement` argument):

- `'center'` (default) — centered on the screen.
- `'anchor'` — `anchor=(row, col)` in 1-based screen coordinates. The
  frame's top-left lands at `(row + 1, col)` (just below the anchor so
  the anchor row stays visible); if the frame would overflow the
  bottom edge it flips above the anchor; it shifts left as needed and
  finally clamps to the screen on both axes.

Sizing: `measure` receives `max_w = cols - 4`, `max_h = rows - 2` and
returns its preferred size; the frame clamps the result to those
maxima. Content widths are computed with the existing cell-width
helpers (`cell_width`, `cell_trim`, …) so wide glyphs measure
correctly. There is no minimum-size fallback: on an absurdly small
terminal the dialog degrades to a clipped sliver, same as the rest of
the UI.

Geometry (placement + size) is computed at open and recomputed only on
terminal resize.

### Resize while open

Each loop iteration re-reads `term_size()`. When it differs from the
size last drawn against: clear the entire screen, recompute geometry,
repaint the frame and content. The regular UI underneath stays blank
until the dialog closes — resizes are rare and the full repaint on
close makes the UI whole again. This is deliberately the simplest
correct behavior; repainting the underlying panes mid-dialog is not
worth the machinery.

### Close and restore

On close (`handle_key` returned done, or uniform cancel):

1. Blank every screen row the frame occupied (border included).
2. `browser._pane_cache.clear()` — force the differential renderer to
   re-emit everything (constraint 1 above).
3. `browser._needs_redraw.add('all')` — the main loop's next pass runs
   `render_full`.

Step 1 satisfies the fresh-cache "cells are already blank" assumption
(constraint 2) for the cells the dialog touched; after a
resize-while-open the whole screen is already blank, which satisfies
it trivially.

### Text handling

Dialog text (messages, options, titles) is plain text. Control
characters and ESC are replaced with `?` before drawing — modal
content is often recipe- or data-supplied and must not be able to
inject escape sequences. No embedded-SGR support in this round.

Message text is wrapped to the content width (reusing the renderer's
wrapping helpers) and vertically clipped to the available height; when
clipped, the last visible row ends with a dim `…` marker so truncation
is visible.

### Dialog kinds

**PickerContent — fzf-style selection** (backs `ctx.pick`)

```
┌─ Status ─────────────────────────────┐
│ > in                                 │
│ ──────────────────────────────────── │
│   open                               │
│ ▌ in-progress                        │
│   pinned                             │
└──────────────────────────────────────┘
```

- Row 1: filter input (`> ` + query); row 2: separator; rest: the
  filtered options, selection in reverse video.
- Width: longest option (or a floor of 24 cells), clamped to ~2/3 of
  the screen. Height: all options + 2, clamped to `max_h`.
- Keys: printable / `space` append to the filter, `backspace` deletes;
  `up`/`ctrl-p`, `down`/`ctrl-n` move the selection (wrapping);
  `home`/`end` jump; `enter` returns the selected option. When options
  outnumber the visible rows, the option window scrolls to keep the
  selection in view (an improvement over the current picker, which
  lets the selection walk off-screen). `enter` with an empty filtered
  list is a no-op.
- Returns the chosen option string, or `None` on cancel. Called with
  an empty options list it returns `None` without opening.

**ChoiceContent — message + button row** (backs `ctx.confirm`)

- Wrapped message text, blank spacer, one centered row of buttons:
  `[ Yes ]   [ No ]   [ Cancel ]`, focused button in reverse video,
  first button focused initially.
- Keys: `left`/`right`/`tab` move focus (wrapping); `enter` activates;
  a button's first letter (case-insensitive) activates it directly
  when unique among the buttons.
- Returns the chosen button's label string, or `None` on cancel.

**InputContent — single-line text entry** (backs `ctx.prompt`)

- Wrapped prompt text above a one-row entry field; the field shows the
  buffer's tail when it overflows (suffix-trimmed by cells), with a
  visible cursor cell at the end.
- Keys: printable / `space` append, `backspace` deletes, `enter`
  returns the buffer. End-only editing, same as the current info-bar
  prompt (no cursor movement within the buffer this round).
- Returns the string (possibly empty), or `None` on cancel. `default`
  pre-fills the buffer.

**MenuContent — anchored context menu**

```
   ▸ deploy-staging
     ┌────────────────┐
     │▌Open           │
     │ Rename         │
     │ Delete         │
     └────────────────┘
```

- A vertical item list: no filter row, no title, minimal width
  (longest item, clamped). Selection in reverse video.
- Keys: `up`/`down` move (wrapping), `enter` returns the item.
- Placement `'anchor'`. The `ctx.menu` wrapper defaults the anchor to
  the list cursor's screen cell, derived from the list pane's rect,
  the cursor index, and the list scroll offset — the same math the
  renderer and the mouse dispatcher already use.
- Returns the chosen item string, or `None` on cancel; empty items
  returns `None` without opening.

**MessageContent — static text + dismiss**

- Wrapped, vertically clipped text (per Text handling above) with an
  optional title.
- Keys: `enter`, `space`, or `q` dismiss (besides the uniform
  esc/ctrl-c).
- Returns `None` always.

### `ctx` API surface

Existing methods keep their contracts, re-backed by modals:

- `ctx.pick(label, options) -> str | None` — picker; `label` becomes
  the title.
- `ctx.confirm(prompt) -> bool` — choice dialog with Yes/No;
  `True` only for Yes.
- `ctx.prompt(prompt, default='') -> str | None` — input dialog.

New methods:

- `ctx.choice(message, choices, *, title=None) -> str | None` —
  general button dialog.
- `ctx.menu(items, *, anchor=None) -> str | None` — context menu;
  `anchor` is an optional `(row, col)` in screen coordinates,
  defaulting to the list cursor's position.
- `ctx.alert(text, *, title=None) -> None` — message dialog.

### Removed code

The info-bar/preview-pane implementations are deleted outright (no
compatibility shims): `_draw_info_prompt`, `_read_line_on_info_bar`,
`_confirm_on_info_bar`, `_pick_on_info_bar`, and `_info_bar_geometry`
in `060-context.py`. Their only callers are the three `ctx` methods
and the unit tests; `test/unit/test_pick.py` is rewritten against the
modal picker.

## Error handling

- Empty `options` / `items` / `choices`: picker and menu return `None`
  immediately; `modal_choice` raises `ValueError` (an empty button row
  is a programming error, not a user condition).
- Tiny terminals: geometry clamps; the dialog draws clipped rather
  than raising.
- Exceptions raised by content callbacks propagate after the restore
  steps run (restore is wrapped in `try/finally`), so a buggy dialog
  cannot leave the screen corrupted with a stale cache.

## Testing

Unit tests (no TTY; injected `_read_key` driving deterministic key
streams, the pattern `test_pick.py` already uses):

- Placement math: centering, anchor below/flip-above/shift-left/clamp.
- Sizing and clamping, wide-glyph widths, message wrapping and the
  clipped-`…` marker.
- Per-dialog key behavior: picker filtering + selection windowing,
  choice focus movement + hotkeys, input editing, menu navigation,
  dismiss keys, uniform cancel, empty-input short-circuits.
- Restore: after close, `_pane_cache` is empty and `'all'` is in
  `_needs_redraw`; restore also runs when content raises.

UI tests (headless pty, existing harness):

- Open each dialog over a populated UI, close it, assert the regular
  UI repaints with no leftover dialog cells.
- Resize while a dialog is open: screen clears, dialog repaints at the
  new geometry, close restores the full UI.

## Future extensions (recorded, not designed)

- Message dialog flags: wrap vs. horizontal clip, vertical scrolling
  vs. clipping.
- More placement strategies (top, cursor-relative for non-menu kinds)
  and manual sizing/positioning.
- Mouse support inside dialogs (click to select, click-outside to
  dismiss).
- Recipe-exception reporting through the message dialog, with storm
  protection ("don't show again for this hook").
- Context-menu population (recipe hook + default entries).
- Settings page as a new content kind, if ever needed.
