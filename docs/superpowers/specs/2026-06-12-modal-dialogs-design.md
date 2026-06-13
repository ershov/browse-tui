# browse-tui — Modal Dialogs Design

**Date:** 2026-06-12
**Status:** Draft (pre-implementation, rev 2)

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
and receives all input until it is closed. Target dialogs:

- **Selection list** — fzf-style filtered selection (replaces the
  current preview-pane picker behind `ctx.pick`) and, with the filter
  off and anchored placement, a context menu (`ctx.menu`; machinery
  only — nothing populates it yet).
- **Choice** — message + button row for confirmation / single choice
  (Yes/No/Cancel-style; backs `ctx.confirm`) and, with a single
  button, a static notification (`ctx.alert`; later, the surface for
  recipe error reporting).
- **Input** — single-line text entry (backs `ctx.prompt`).

## Goals

- One blocking modal window at a time. No stacking, no z-order.
- While open: no pane repaints, all keys go to the dialog. Content
  channels (stdin ingest, `ctx.print` output) keep flowing.
- Flicker-free restore on close: the regular UI repaints differentially
  with no leftover dialog cells and no blank intermediate frame.
- Replace the implementations behind `ctx.pick` / `ctx.confirm` /
  `ctx.prompt` (no backward-compatibility constraint; `ctx.confirm`'s
  contract changes — see API section).
- New recipe-facing APIs for menus and alerts.
- Reuse the existing machinery throughout: `Rect`, the
  `begin_row`/`end_row` row cache, the preview ANSI pipeline, the
  cell-width helpers, sync brackets.

## Non-goals (this round)

- Non-modal / persistent floating windows (would require z-order and
  clipping in the pane renderers).
- Free user-driven scrolling inside dialogs. Message text is wrapped
  and vertically clipped; selection lists keep the selection in view
  by windowing.
- Mouse interaction inside dialogs (mouse events are swallowed).
- Wiring recipe-exception reporting into the alert dialog (follow-up
  once the API exists).
- Anything that populates the context menu (hooks, default entries).
- A settings page / widget toolkit / layout engine. The design merely
  avoids precluding it: a future content kind plugs into the same
  frame-and-loop machinery.

## Background: rendering constraints

The renderer has no compositor. Panes tile the screen; each pane
renderer composes rows through `begin_row`/`end_row`
(020-terminal.py), which capture the row's writes and emit them only
on a cache miss against the pane's `PaneCache`. Two `end_row` facts
shape this design:

1. **Cache hit = emit nothing.** A row whose captured bytes match the
   cached entry (same rect, not first paint) is skipped. Anything that
   overdraws the screen behind the renderer's back must defeat this,
   or the next repaint leaves the overdrawn cells in place.
2. **Padding follows the cached visible length.** On a steady-state
   miss where the new row's visible length is shorter than the cached
   one, `end_row` pads (or `\e[K`s) out to the cached length. The
   cache entry *is* the renderer's belief about what's on screen — a
   planted entry with a full-width visible length makes the next paint
   clear the full width.

Fact 2 is the lever for flicker-free restore (below). Because the
dialog is fully modal, input routing is free: the dialog runs a nested
`read_key` loop, so the main loop never paints while it is open.

## Approaches considered

**A. Independent loop functions per dialog kind.** Siblings of today's
`_pick_on_info_bar`, each owning its read-key loop, sharing only draw
helpers. Simple, but the subtle parts — channel events, resize,
close/restore, cache interaction — would be duplicated per dialog,
and they are exactly the parts that are easy to get wrong.

**B. One shared modal loop + small content objects (chosen).** A
single `run_modal` owns the lifecycle (geometry, frame drawing through
the row cache, key loop, channel events, resize, restore); each dialog
kind is a small content object (size, row composition, key handling).
The lifecycle logic exists once; adding a dialog kind means adding a
content class. This is also the seam a future settings page would
plug into.

**C. A widget/layout toolkit.** Rows of focusable widgets, layout
engine, focus management. Rejected: none of the target dialogs needs
it, and it can be layered on top of B later if the settings page ever
materializes.

## Design

### Module

New file `src-tui/055-modal.py`, concatenated between `050-render.py`
(whose `Rect`, cell-width helpers, preview wrap pipeline, and `style`
it uses) and `060-context.py` (whose `ctx` methods delegate to it).
Public surface:

- `run_modal(browser, content, *, placement='center', anchor=None,
  delay_interaction=False, _read_key=None) -> result`
- Content classes: `ListContent`, `ChoiceContent`, `InputContent`.
- Convenience wrappers used by `ctx`: `modal_pick`, `modal_menu`,
  `modal_confirm`, `modal_input`, `modal_alert`.

`_read_key` is the same test-injection seam the current picker has: a
callable returning decoded key names, defaulting to the terminal
layer's `read_key`. Calling `run_modal` while a dialog is already open
raises `RuntimeError` (one window at a time is a hard invariant, and
violating it is a programming error).

### Content protocol

A content object provides:

- `title` — optional string embedded in the top border.
- `measure(max_w, max_h) -> (w, h)` — content size, driven by the
  content itself (longest option, wrapped message line count, …) and
  at most `(max_w, max_h)`. The caps are supplied by `run_modal`:
  80% of the screen width and `rows - 4` height. Called at open and
  after a terminal resize; never between keystrokes (the frame does
  not jiggle while the user types).
- `draw_row(row, width)` — emit the writes for one content row.
  Called inside an active `begin_row` capture; the frame loop supplies
  the borders and inner padding around it. Rows are filled to `width`
  via the existing segment writer (`_write_segments` with `pad_to`),
  so a row's previous content cannot bleed through.
- `handle_key(key) -> (done, result)` — process one key. `(False, _)`
  continues the loop; `(True, result)` closes the dialog and makes
  `run_modal` return `result`.

The shared loop handles these before the content sees a key:

- `'esc'` / `'ctrl-c'` — uniform cancel: close and return `None`.
- `'_notify'` — drain background work (`browser.drain_main_queue()`,
  `browser.apply_children_results()`) **without rendering**. Redraw
  flags that drained work sets in `browser._needs_redraw` simply
  accumulate; the close-time `'all'` absorbs them.
- `'_writable'` / `'_stdin'` — content-channel events: call
  `browser._drain_output()` / `browser._pump_stdin()` and continue.
  The loop passes the channel fds to `read_key` under the same
  conditions as the main loop (output fd only while bytes are
  buffered, fd 0 only while the streaming-input hook is armed), so
  a dialog never stalls a streaming recipe.
- `g_resize_flag` / `g_screen_lost_flag` — checked after every
  `read_key` return, same as the main loop; both route to the
  resize path below.
- Mouse events (`mouse-click:*`, `scroll-*`) — swallowed.

Everything else goes to `handle_key`; unrecognized keys are ignored by
the content.

### Drawing through the row cache

The dialog paints through the same differential machinery as the
panes. `run_modal` holds a **private** `PaneCache` (never registered
in `browser._pane_cache`, so the regular renderer cannot see it) and
composes every frame row — top border, content rows wrapped in `│`
borders and one column of inner padding, bottom border — inside
`begin_row`/`end_row` captures, bracketed by `begin_sync`/`end_sync`.

Consequences, all free:

- The first paint emits the full frame; subsequent iterations re-emit
  only rows whose bytes changed (in the picker, a keystroke re-emits
  the filter row and the option rows that actually moved).
- The border style matches the pane separators (the dim/gray
  chrome); the title renders bold, embedded in the top border:

```
┌─ Confirm ────────────────────────────┐
│ Delete 3 items?                      │
│                                      │
│        [ Yes ]   [ No ]              │
└──────────────────────────────────────┘
```

### Placement and sizing

Sizing is automatic: `measure` reports the content-driven size, capped
at 80% of the screen width and `rows - 4` rows. The frame adds the
border and inner padding (`content_w + 4` × `content_h + 2`). Widths
are computed with the cell-width helpers so wide glyphs measure
correctly. Geometry is computed at open and recomputed only on
terminal resize.

Two placement strategies (the `placement` argument):

- `'center'` (default) — centered on the screen.
- `'anchor'` — `anchor=(row, col)` in 1-based screen coordinates. The
  frame's top-left lands at `(row + 1, col)` (just below the anchor so
  the anchor row stays visible); if the frame would overflow the
  bottom edge it flips above the anchor; it shifts left as needed and
  finally clamps to the screen on both axes.

**Tiny terminals:** below a usable minimum (`cols < 20 or rows < 8`,
constants to be tuned) the frame rect becomes the entire screen — no
centering math on a screen that cannot fit a centered box.

### Delayed interaction

`delay_interaction=False` distinguishes dialogs the user asked for
from dialogs that appear on their own (background errors, async
events). When `True`, the dialog must not swallow keystrokes the user
typed *at the previous screen*:

1. On open, drain all pending input: while `input_ready()` (the
   zero-timeout poll the burst coalescer uses), read and discard keys.
2. Ignore all input until a threshold (default 0.5 s,
   `time.monotonic`-based) has elapsed since the dialog's first
   paint. Keys arriving inside the window are read and discarded;
   `'_notify'` / channel events / resize are processed normally.

The threshold is a module constant and injectable for tests (so key
streams driven through `_read_key` can run with a zero threshold).
User-invoked dialogs (everything `ctx` exposes today) default to
`False`; the future error-reporting wiring passes `True`.

### Resize / screen-lost while open

When `g_resize_flag` or `g_screen_lost_flag` is set: clear the entire
screen, `browser._pane_cache.clear()`, recompute geometry (re-running
`measure` with the new caps), repaint the frame and content. The
regular UI underneath stays blank until the dialog closes — resizes
are rare, and with the caches cleared and the screen genuinely blank,
the close-time repaint's first-paint branch is valid. This is
deliberately the simplest correct behavior.

### Close and restore: cache poisoning

On close (content returned done, or uniform cancel), instead of
blanking the dialog and forcing a from-scratch repaint:

1. For every cache in `browser._pane_cache` whose rect intersects the
   frame rect, set each intersecting row's entry to
   `(pane_width, POISON)` — `POISON` being a marker string no renderer
   can produce (it contains `\x00`, which every sanitizer strips).
2. `browser._needs_redraw.add('all')`.

The next `render_full` then repaints *differentially*: poisoned rows
miss the cache (bytes differ) and — because the planted visible length
is the full pane width — pad or `\e[K` out to the full row, clearing
every cell the dialog touched; untouched rows cache-hit and emit
nothing. The whole repaint lands inside one sync bracket: no blank
intermediate frame, minimal bytes. Pane separators and the info bar
have row caches too, so walking `browser._pane_cache` covers every
cell the dialog can overdraw.

Restore runs in a `try/finally` around the dialog loop, so a buggy
content object cannot leave the screen corrupted with a stale cache.
(After a resize-while-open the caches are already cleared and the
poisoning pass is a no-op over empty caches — the blank screen plus
first-paint repaint handles it.)

### Text handling

Dialog body text gets exactly Preview's ANSI treatment, via the same
pipeline (`_sanitize_preview` + the wrap-aware SGR walker
`_wrap_preview_line`): embedded SGR colors render, all other CSI is
dropped, raw control characters are neutralized. Message bodies wrap
to the content width and clip vertically to the available height;
when clipped, the last visible row ends with a dim `…` marker.

List option rows follow the list pane's existing selection rule:
embedded SGR renders normally on unselected rows, while the selected
row renders as plain visible text in reverse video so the highlight
reads cleanly. Titles and button labels are plain text.

### Dialog kinds

**ListContent — selection list** (backs `ctx.pick` and `ctx.menu`)

One content kind covers both the fzf-style picker and the context
menu; the differences are arguments, not structure: `filter=True`
shows a filter row, placement comes from `run_modal`, the title is
optional.

```
┌─ Status ─────────────────────────────┐         ┌────────────────┐
│ > in                                 │         │▌Open           │
│ ──────────────────────────────────── │         │ Rename         │
│   open                               │         │ Delete         │
│ ▌ in-progress                        │         └────────────────┘
└──────────────────────────────────────┘
   filter=True, centered (pick)          filter=False, anchored (menu)
```

- Width: longest option, floor 8 cells, capped as above. Height:
  options (+ 2 for the filter row and its separator when `filter=True`),
  capped.
- Keys: with `filter=True`, printable / `space` edit the filter and
  `backspace` deletes; `up`/`ctrl-p`, `down`/`ctrl-n` move the
  selection (wrapping); `home`/`end` jump; `enter` returns the
  selected option. When options outnumber the visible rows, the
  window scrolls to keep the selection in view (an improvement over
  the current picker, which lets the selection walk off-screen).
  `enter` with an empty filtered list is a no-op.
- Returns the chosen option string, or `None` on cancel. Called with
  an empty options list it returns `None` without opening.

**ChoiceContent — message + button row** (backs `ctx.confirm` and
`ctx.alert`)

- Wrapped message body (ANSI per Text handling), blank spacer, one
  centered row of buttons, focused button in reverse video, first
  button focused initially.
- Button labels use the standard `&` hotkey convention: `'&Yes'`
  renders as `Yes` with the `Y` underlined (`set_style` already
  supports underline); pressing the hotkey letter (case-insensitive)
  activates that button immediately; `&&` is a literal ampersand; a
  label without `&` simply has no hotkey.
- Keys: `left`/`right`/`tab` move focus (wrapping); `enter` activates
  the focused button; hotkeys activate directly. With a single button
  (the alert case), `space` also activates it.
- Returns the chosen button's label with the `&` stripped, or `None`
  on cancel. An empty button tuple raises `ValueError` (a programming
  error, not a user condition).

**InputContent — single-line text entry** (backs `ctx.prompt`)

- Wrapped prompt text above a one-row entry field; the field shows the
  buffer's tail when it overflows (suffix-trimmed by cells), with a
  visible cursor cell at the end.
- Keys: printable / `space` append, `backspace` deletes, `enter`
  returns the buffer. End-only editing, same as the current info-bar
  prompt (no cursor movement within the buffer this round).
- Returns the string (possibly empty), or `None` on cancel. `default`
  pre-fills the buffer.

### `ctx` API surface

- `ctx.pick(label, options) -> str | None` — selection list with
  filter, centered; `label` becomes the title.
- `ctx.menu(items, *, anchor=None) -> str | None` — selection list
  without filter, anchored; `anchor` is an optional `(row, col)` in
  screen coordinates, defaulting to the list cursor's screen cell
  (derived from the list pane's rect, the cursor index, and the list
  scroll offset — the same math the renderer and the mouse dispatcher
  already use).
- `ctx.confirm(message, buttons=('&Yes', '&No'), *, title=None)
  -> str | None` — choice dialog. **Contract change:** returns the
  chosen label (`'Yes'`, `'No'`, …) or `None` on cancel, not a bool.
  Call sites compare explicitly (`== 'Yes'` — note `None != 'Yes'`
  handles cancel for free); the old truthiness idiom
  `if ctx.confirm(...)` would be silently wrong (`'No'` is a truthy
  string). The in-tree callers that use it — the browse-fs delete
  guard and the browse-procs SIGTERM guard — and the `ctx.confirm`
  tests are updated in the same change.
- `ctx.prompt(prompt, default='') -> str | None` — input dialog (shipped
  as the pre-existing method name `ctx.input`).
- `ctx.alert(text, *, title=None) -> None` — choice dialog with a
  single `('&OK',)` button; sugar for notifications.

All five accept `delay_interaction` as a keyword and pass it through.

### Removed code

The info-bar/preview-pane implementations are deleted outright (no
compatibility shims): `_draw_info_prompt`, `_read_line_on_info_bar`,
`_confirm_on_info_bar`, `_pick_on_info_bar`, and `_info_bar_geometry`
in `060-context.py`. Their only callers are the three `ctx` methods
and the unit tests; `test/unit/test_pick.py` is rewritten against the
modal list dialog.

## Error handling

- Empty `options` / `items`: return `None` immediately without
  opening. Empty `buttons`: `ValueError`.
- Tiny terminals: full-screen frame per the threshold rule; on an
  absurdly small screen the dialog draws clipped rather than raising.
- Exceptions from content callbacks propagate after the restore steps
  run (`try/finally`), so the screen and caches are never left stale.
- Nested `run_modal`: `RuntimeError`.

## Testing

Unit tests (no TTY; injected `_read_key` driving deterministic key
streams, the pattern `test_pick.py` already uses; zero
delay-interaction threshold):

- Placement math: centering, anchor below/flip-above/shift-left/clamp,
  full-screen tiny-terminal rule.
- Sizing and caps, the width floor, wide-glyph widths, message
  wrapping, the clipped-`…` marker, ANSI passthrough vs. control-char
  neutralization.
- Per-dialog key behavior: filter editing + selection windowing,
  button focus movement + `&` hotkeys + `&&` escaping, input editing,
  uniform cancel, empty-input short-circuits, single-button `space`.
- Delay interaction: pending keys drained at open, keys inside the
  threshold window discarded, keys after it dispatched.
- Restore: after close, intersecting cache rows are poisoned with
  full-width visible lengths, `'all'` is flagged, and a subsequent
  `end_row` pass re-emits exactly the poisoned rows; restore also
  runs when content raises.

UI tests (headless pty, existing harness):

- Open each dialog over a populated UI, close it, assert the regular
  UI repaints with no leftover dialog cells.
- Resize while a dialog is open: screen clears, dialog repaints at the
  new geometry, close restores the full UI.
- A streaming-input recipe keeps ingesting while a dialog is open
  (channel events serviced from the modal loop).

## Future extensions (recorded, not designed)

- Message-body flags: wrap vs. horizontal clip, vertical scrolling
  vs. clipping.
- More placement strategies (top, cursor-relative for non-menu kinds)
  and manual sizing/positioning.
- Mouse support inside dialogs (click to select, click-outside to
  dismiss).
- Recipe-exception reporting through the alert dialog
  (`delay_interaction=True`), with storm protection ("don't show
  again for this hook").
- Context-menu population (recipe hook + default entries).
- Settings page as a new content kind, if ever needed.
