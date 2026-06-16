# Cross-thread modal dialog control — design

Status: approved, ready to implement
Date: 2026-06-16
Builds on: `docs/superpowers/specs/2026-06-12-modal-dialogs-design.md` (the
modal facility — `run_modal`, the content protocol, the convenience wrappers).

## Problem

Today every modal dialog is opened through a Context sub-flow
(`ctx.confirm` / `alert` / `pick` / `menu` / `input`) that runs `run_modal`,
a **blocking nested key loop on the main thread**. That is correct for an
action handler (which the main loop dispatches on the main thread) but leaves
two gaps:

1. A recipe's **background thread** (`ctx.run_in_worker` / `run_in_slot`)
   cannot interact with the user at all — the dialog sub-flows are
   main-thread-only and unsafe to call from a worker.
2. Code has no way to **query** whether a dialog is open, or to
   **dismiss** an open dialog programmatically (e.g. "the operation the
   dialog was guarding just finished in the background — take the dialog
   down").

This design adds three capabilities, all consistent with the existing
main-thread/any-thread split (`Context` is main-thread-only; `Browser` is the
thread-safe surface; `Browser.post` shuttles work onto the main thread):

* `is_dialog_open()` — query whether a dialog is currently displayed.
* `close_dialog(value)` — dismiss the open dialog, delivering `value`.
* an **async variant of every dialog function**, callable from any thread,
  that opens the dialog on the main thread and delivers the result through a
  callback.

## Background: why everything funnels through the main thread

`run_modal` owns the terminal and the input loop while it runs. Only the main
loop may own terminal I/O and the differential render cache. So:

* The blocking dialog runs on the main thread; while it is open the main
  thread is **parked inside `run_modal`**.
* Any cross-thread interaction therefore cannot touch dialog state directly.
  It must hand a request to the main thread via `Browser.post`, which enqueues
  onto the thread-safe `_main_queue` (`queue.Queue`) and wakes the loop. The
  main thread drains that queue (`drain_main_queue`) one callable at a time,
  FIFO — including inside an open dialog's `_notify` handler.

The invariant that makes the whole feature lock-free: **all dialog state is
read and mutated only on the main thread; other threads merely `post` requests
and (for the async variants) receive their result via a main-thread callback.**
The two genuinely cross-thread entry points (`close_dialog`, `open_dialog_async`)
either post, or perform a single atomic attribute write plus a wake.

## Result contract — one rule

> A dialog yields the chosen value, or **`None`** if it closed without a user
> answer **for any reason**: esc/ctrl-c cancel, programmatic `close_dialog`,
> being overridden by a later dialog, being displaced while still pending, or
> running headless.

There is **no separate "superseded" sentinel.** `None` already means "no user
answer," and that is the right meaning for every no-answer path. This keeps the
blocking API's existing contract (value or `None`) unchanged and keeps the
common guard idiom safe — `if not ctx.confirm('delete?', [('&Yes', True),
('&No', False)]): return` does the safe thing whether the user cancelled or a
background dialog overrode it, because `None` is falsy.

## Override — unconditional, single slot, no queue

When a new async dialog request arrives:

* If a dialog (blocking **or** async) is currently displayed, it is
  **dismissed** (its result becomes `None` via `close_dialog(None)`) and the
  new dialog is shown. Override is **unconditional** — there is no conflict
  policy parameter and no "discard" mode.
* At most **one** dialog is ever pending. A second request that arrives before
  the first has been shown **replaces** it; the displaced request's callback
  fires immediately with `None` (it was never shown).

There is no multi-dialog queue and no sequence-number arbitration. A single
`_pending_dialog` slot with "last-processed-wins" is sufficient:

* Because `drain_main_queue` empties the **entire** `_main_queue` before the
  main loop's servicing step runs `run_modal`, two requests that are both
  already queued collapse during one drain — the earlier one is displaced
  while pending (callback `None`, **never shown, no flash**) and only the
  survivor is displayed.
* Which of two genuinely-simultaneous requests survives is not specified
  (it depends on `queue.Queue` ordering). This is an accepted, documented
  non-determinism: either may win, and the loser's callback still fires `None`.
* A narrow consequence of unconditional override: a later-*arriving* request
  that was issued *earlier* can replace a newer dialog that is already on
  screen. That is acceptable — override is unconditional and the displaced
  dialog's callback still fires exactly once (`None`).

## Callback contract

> Every async dialog's `on_result` callback fires **exactly once**, **on the
> main thread**, with **exceptions caught** and routed to `browser.error`.

* **Exactly once.** Each resolution path fires through exactly one site:
  user choice / cancel / override-close all fire when `run_modal` returns at
  the main-loop servicing step; a displaced-while-pending request fires inline
  in `_enqueue_dialog` (which clears the slot so it cannot re-fire); headless
  fires `None` immediately.
* **Main thread.** The firing sites are all on the main thread (the servicing
  step and `_enqueue_dialog`, which runs during a drain). Recipes may freely
  touch `ctx` from the callback.
* **Exceptions caught.** Callbacks are invoked through a single helper that
  wraps them in `try/except` and routes failures to `browser.error`, matching
  the existing recipe-hook fire pattern (e.g. `_fire_cursor_change_if_pending`).
  Callbacks must **never** be delivered as a bare `post`ed closure, because
  `drain_main_queue` runs queued callables without catching — an exception
  there would escape into the loop.

## Quit contract

> On quit, the framework guarantees **only** that the `on_quit` hook is called.
> After `on_quit` returns, any pending / active / displaced dialog callbacks
> may be **dropped** — they are not guaranteed to fire.

`on_quit` is the existing shutdown hook (`Browser._fire_on_quit`, fired once in
teardown after the main loop exits). Consequence for the engine: `run_modal`
must also break when `_quit_requested` is set (today it does not), so a
worker's `ctx.quit()` can tear down an open dialog and let the app exit. On a
quit-break the servicing step does **not** fire the dialog's callback (dropped,
per the contract).

## Headless

Headless Browsers have no render loop, so there is nothing to open. The async
variants therefore **fire their callback with `None` immediately** (inline,
wrapped), consistent with the blocking `ctx.confirm` returning `None` in
headless. `is_dialog_open()` returns `False`; `close_dialog()` is a no-op.

## API surface

New `Browser` methods (thread-safe surface):

* `is_dialog_open() -> bool` — whether a dialog is currently displayed
  (the `_modal_open` flag). Cross-thread it is a best-effort snapshot.
* `close_dialog(value=None) -> None` — request the displayed dialog close,
  delivering `value` to whoever is waiting on it (the blocking return, or the
  async callback). No-op if no dialog is open. Thread-safe.
* `open_dialog_async(content, *, on_result=None, placement='center', anchor=None) -> None`
  — internal/general entry: queue `content` to open on the main thread; call
  `on_result(value)` there when it resolves. The per-kind `ctx` methods below
  are the public surface.

New `Context` methods — an async sibling for **every** dialog function,
callable **from any thread** (they post; the dialog opens on the main thread
and `on_result` fires there). These are the documented exception to "`Context`
is main-thread-only"; the `Context` module docstring is amended to say so.

* `confirm_async(message, buttons=('&Yes', '&No'), *, title=None, on_result=None)`
* `alert_async(text, *, title=None, on_result=None)`
* `pick_async(label, options, *, on_result=None)`
* `menu_async(items, *, anchor=None, on_result=None)`
* `input_async(prompt, *, default='', on_result=None)`

Each returns `None` immediately. `on_result` receives the chosen value or
`None` (see the result contract). For `alert_async`, the result is always
`None` (an alert conveys nothing back) — the callback signals "dismissed."
Empty `options`/`items` for `pick_async`/`menu_async` fire `on_result(None)`
without opening, mirroring the blocking wrappers' empty short-circuit.

Also exposed on `Context`: `is_dialog_open()` (pass-through; useful from the
main thread or as a cross-thread hint) and `close_dialog(value=None)`.

## Engine state and changes

State (on `Browser`/`State`, main-thread-owned except where noted):

* `_modal_open: bool` — exists; set/cleared by `run_modal`.
* `_modal_force: Optional[tuple]` — **new.** `(value,)` when an open dialog
  should force-close returning `value`; `None` when not armed. The 1-tuple
  encoding distinguishes "force-close with `None`" from "not armed." Written by
  `close_dialog` (a single atomic write + `notify_wake`); read and cleared by
  `run_modal`.
* `_pending_dialog: Optional[tuple]` — **new.** `(content, on_result,
  placement, anchor)` for the next dialog to show, or `None`. Main-thread-only.

`run_modal` changes (small):

* On entry, clear `_modal_force` (so a stale force armed while no dialog was
  open does not leak into this one).
* Honor `_modal_force` and `_quit_requested`: after the `_notify` drain (and as
  a top-of-loop check), if `_modal_force is not None`, break returning
  `_modal_force[0]` and clear it; if `_quit_requested`, break returning `None`.

`Browser.run` main loop change: right after the existing
`drain_main_queue()` / `apply_children_results()` at the top of the loop, add a
servicing step:

```
while self._pending_dialog is not None and not self._modal_open \
        and not self._quit_requested:
    content, on_result, placement, anchor = self._pending_dialog
    self._pending_dialog = None
    result = run_modal(self, content, placement=placement, anchor=anchor,
                       delay_interaction=True)
    if not self._quit_requested:
        self._fire_dialog_cb(on_result, result)   # dropped on quit
```

`_enqueue_dialog(content, on_result, placement, anchor)` (main thread, run
during a drain):

```
if self._pending_dialog is not None:
    self._fire_dialog_cb(self._pending_dialog[1], None)   # displaced, unshown
self._pending_dialog = (content, on_result, placement, anchor)
if self._modal_open:
    self.close_dialog(None)        # override the active dialog -> it returns None
```

`_fire_dialog_cb(cb, value)` — the one callback-firing helper (enforces the
callback contract):

```
if cb is None:
    return
try:
    cb(value)
except Exception as e:
    self.error(f'dialog callback: {type(e).__name__}: {e}')
```

`delay_interaction=True` is used for every async-opened dialog (it drains
in-flight keystrokes and grace-gates, so a dialog appearing under the user's
fingers is not instantly dismissed — exactly the self-appearing-dialog case the
flag was built for).

## Non-goals (explicitly out of scope)

* **No `SUPERSEDED` sentinel** — `None` covers every no-answer path.
* **No conflict policy / discard mode** — override is unconditional.
* **No synchronous-wait variant** — callbacks only. (A worker that wants to
  block can build its own `threading.Event` around the callback; we do not
  provide or advise it, to avoid main-thread deadlocks.)
* **No multi-dialog queue / backlog** — a single pending slot, last wins.
* **No sequence-number arbitration** — which of two simultaneous requests wins
  is unspecified, by design.

## Testing notes

* Unit-test the engine pieces against a headless `Browser` and by driving
  `run_modal` with the existing scripted-key seam (`_read_key`) plus the new
  `_modal_force` / `_quit_requested` breaks.
* Verify the callback contract directly: exactly-once on each path
  (choice / cancel / override / displaced-pending / headless), main-thread,
  and that a throwing callback is caught and routed to `error`.
* Verify cross-thread end-to-end via a real worker thread (`run_in_worker`)
  calling a `*_async` method against a headless or pty-driven Browser — not a
  fake `ctx` — so the `post` → drain → servicing → callback path is exercised
  for real.
* Rendering/ctx surface changed → run the **full** `./run-tests-parallel.sh`.
