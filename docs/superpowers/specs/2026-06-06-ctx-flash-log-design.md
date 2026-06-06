# Design: replace `ctx.message()` with `flash` / `log` / reworked `error`

Date: 2026-06-06

## Motivation

`ctx.message(text)` is dead. It sets `Browser._message_text`, which **no
renderer ever reads** — so every call (the `y` "show full id" action, all the
toggle acknowledgements, "No preview available", etc.) produces no visible
output. Separately, `ctx.error(text)` *works* but is **sticky**: it takes over
the preview pane and is only ever cleared in `__init__`, so an error from a
failed `get_children` lingers after the user navigates away.

We want two clearly separated facilities, plus a fixed `error`:

1. **Indication** — immediate, transient feedback that an action happened, so
   the UI doesn't look stale. OK to vanish after a short timeout.
2. **Logging** — a `console.log`-style record, viewable on demand.
3. **Error** — like indication but louder and stickier.

## The three primitives

All three live on `Context` (delegating to `Browser`), are thread-safe (route
through `Browser.post`), and are the complete replacement for `ctx.message`.

| Method | Visible surface | Auto-clear | Logs? | Style |
|--------|-----------------|-----------|-------|-------|
| `ctx.flash(text, log=False)` | info-bar notice | timer, ~1s | only if `log=True` | neutral/dim |
| `ctx.log(text)` | none (silent) | n/a | always | n/a |
| `ctx.error(text)` | info-bar notice | next keypress, **min 1s** | always | red |

`ctx.message` is **removed entirely** (no alias) — it has no behavior to
preserve, and every in-repo call site is migrated.

### Semantics

- **`flash(text, log=False)`** — set the info-bar notice (kind `flash`), arm a
  `threading.Timer(_FLASH_DURATION)` that clears it. `log=True` also appends to
  the log. Toggle/mode acks use the bare form; side-effecting or
  degradation messages opt into `log=True`.
- **`log(text)`** — append one entry to the in-memory log ring buffer. No
  display, no redraw.
- **`error(text)`** — set the info-bar notice (kind `error`, red), **always**
  append to the log, **no** timer. Cleared by the first keypress that lands
  `>= _ERROR_MIN_DISPLAY` (1s) after it appeared; the keystroke still performs
  its normal action (non-swallowing). Earlier keypresses leave it in place so
  an in-flight key can't instantly wipe an unread error.

## Data model (Browser)

Replaces `_message_text` and `_error_text`:

```python
self._notice = None        # Notice | None  — the single info-bar notice slot
self._notice_seq = 0       # monotonic id; a timer only clears "its own" notice
self._log = collections.deque(maxlen=_LOG_MAXLEN)   # _LOG_MAXLEN = 1000
```

`Notice` is a small dataclass: `text: str`, `kind: str` (`'flash'`/`'error'`),
`shown_at: float` (monotonic), `seq: int`.

There is one slot; the most recent of `flash`/`error` wins (last-write
replaces, bumping `_notice_seq`). An error followed by a flash is therefore
replaced by the flash — acceptable, since the error is still in the log.

Constants: `_FLASH_DURATION = 1.0`, `_ERROR_MIN_DISPLAY = 1.0`,
`_LOG_MAXLEN = 1000`.

Log entries are stored pre-formatted with a wall-clock prefix:
`"HH:MM:SS  <text>"`, chronological order (oldest first).

### Threading & lifecycle

- Every mutation is posted to the main thread via `Browser.post` (same pattern
  the old methods used), so worker-thread callers (e.g. the `get_children`
  failure path) are safe.
- The flash timer is a **daemon** `threading.Timer`. On fire it posts a clear
  callback (which checks `_notice.seq` against the captured seq so a stale
  timer never clears a newer notice) and calls `notify_wake()` so the blocked
  main loop repaints. The active timer is cancelled on `stop_workers` / quit.
- **Headless** browsers skip arming the timer (no render loop); they still set
  `_notice` and append to `_log` so unit tests can assert state.

## Rendering

### Info-bar notice

The notice shares the middle region of the rich info bar (the `info=True` bar —
there is exactly one per layout) with the search/filter prompt and the hint.
Priority in that region:

1. search/filter prompt (when editing) — unchanged, highest
2. **notice** (`_notice`, flash or error)
3. hint (`browser.hint`) — existing fallback

`render_info_bar` reads `browser._notice`; when present it writes the text in
place of the hint — red (e.g. `fg=9`/`fg=1`, bold) for `error`, a quiet
distinct style for `flash`. Truncated to fit, like the hint is today. Pane
headers (`info=False`) never show the notice.

### Remove the sticky-error preview takeover

`error` no longer hijacks the preview pane. Delete every `_error_text` read in
`050-render.py` (the preview content branch, the `not browser._error_text`
guards, and the `'Error'` case in `_preview_label`). The `get_children`
failure path in `040-state.py` switches from `self._error_text = ...` to
`self.error(...)` (thread-safe via post; now also logged).

Tradeoff (accepted): a `get_children` error is now a red info-bar line +
log entry rather than a full pane. It's less prominent but always recoverable
in the log and persists until acknowledged.

## Clear policies (where they live)

- **flash** — timer only (plus: superseded visually whenever a prompt opens;
  the timer still clears the underlying slot). Deliberately *not* cleared by
  ordinary keypresses, so a 1s flash is predictable during navigation.
- **error** — at the top of `_handle_one_key`, before dispatch: if `_notice`
  is an error and `monotonic() - shown_at >= _ERROR_MIN_DISPLAY`, clear it and
  flag the info bar for redraw. The key then dispatches normally regardless.

## Log viewer (framework default)

New default action in `default_actions()`:

```python
Action('~', 'View message log', _view_log, 'none', 'OTHER'),
```

`_view_log(ctx)` joins `browser._log` with newlines (or `"(log empty)"`) and
calls `ctx.page(text)` — reusing the existing pager (`bat`/`batcat`/`less`),
which is paste-friendly and needs no new rendering. Recipe `actions` override
defaults (last-write-wins in `build_keymap`), so any recipe can rebind `~`.

Note: `` ` `` is already taken by `browse-git` ("Switch view mode") and is the
worse choice; `~` is free at the framework level (only `browse-plan` binds it,
and that binding is removed below).

## Call-site migration

Remove `ctx.message` / `Browser.message` / `_message_text` and reassign:

| Location | Text | New call |
|----------|------|----------|
| browse-fs:133 | `md preview: …` | `flash` |
| browse-plan:240 | `preview: …` | `flash` |
| browse-git:1367 | `mode: …` | `flash` |
| browse-git:1403 | `message coloring: …` | `flash` |
| browse-git:1416 | `commit graph: …` | `flash` |
| browse-procs:121 | `sent SIGTERM to pid …` | `flash(log=True)` |
| browse-md:1347 | `md preview: …` | `flash` |
| browse-md:1387 | `no markdown renderer found …` | `flash(log=True)` |
| browse-md:1401 | `No preview available` | `flash` |
| 070-actions.py:406 | `No preview available` | `flash` |
| 070-actions.py:420 | `No preview available` | `flash` |
| browse-claude:5440 | `no markdown renderer found …` | `flash(log=True)` |
| browse-claude:5454 | `No preview available` | `flash` |
| browse-claude:5477 | `voice rendering: …` | `flash` |
| browse-claude:5484 | `id: …` | **removed** → see y/id below |
| browse-claude:5517 | `no more voice messages this way` | `flash` |
| browse-claude:6210 | `view: …` | `flash` |
| browse-claude:6261 | `voice-only filter: …` | `flash` |

Rule of thumb: bare `flash` for toggle/mode acks and "nothing to show"
notices; `flash(log=True)` for side effects (process kill) and degradation
warnings (missing renderer) worth an audit trail.

## y / id change (browse-claude)

`_action_show_id` becomes:

```python
def _action_show_id(ctx):
    if ctx.cursor is None:
        return
    ctx.page(ctx.cursor.id)
```

A 1s flash can't be read or copied; the pager is genuinely paste-friendly
(full screen, scrollable/selectable), matching the action's existing
"paste-friendly" label.

## browse-plan logging integration

`browse-plan` currently reinvents a log: `_action_command_log` (bound to `~`)
shells out to `plan project get` as a stand-in for the per-process audit log
it actually wanted. With a framework log it can do the real thing.

- **Remove** `_action_command_log` and its `~` binding (browse-plan:483); it
  inherits the framework `~` viewer.
- **Log at the `_run_plan` chokepoint** (every `plan` shell-out passes through
  it). Record exactly two kinds of call:
  * **any call that errors** — non-zero `returncode` (including the 30s
    timeout fallback), even read-only ones, as `[error] plan <argv>: <stderr>`;
  * **any database-mutating call** on success — every subcommand *except* the
    read set (`list` / `-r list`, `get`, `project get`), as `plan <argv>`.

  Successful reads are not logged, so expands/refreshes don't flood the log.
  The read set is recognized by the `list` / `get` verbs in the argv.
- **Plumbing:** `_run_plan` has no `ctx` (it's also called by the ctx-less
  `get_children` / `get_preview`). Wire a module-level sink in `main()` right
  after the Browser is built — `_log_sink = b.log` (the thread-safe
  `Browser.log`) — and have `_run_plan` call it when set. No `ctx` threading.
- Behavior change: `~` now shows the session's audit log (errors + mutations)
  instead of `plan project get`. (This restores the originally-intended
  surface.)

## Removal checklist

- `Context.message` (060-context.py)
- `Browser.message`, `Browser.message_text`, `Browser._message_text`
- `Browser._error_text`, `Browser.error_text` (replaced by `_notice` + `_log`)
- preview-pane error rendering + `'Error'` label (050-render.py)
- `browse-plan._action_command_log` + `~` binding

## Testing

- **Unit (Browser/Context):** `flash` sets a `flash` notice and (headless)
  skips the timer; `flash(log=True)` appends; `log` appends silently with no
  notice/redraw; `error` sets a red notice + always logs + arms no timer;
  ring buffer caps at `_LOG_MAXLEN`; a stale timer (seq mismatch) does not
  clear a newer notice; `ctx.message` no longer exists.
- **Render:** info bar shows the notice in the right region with prompt >
  notice > hint priority; error renders red; pane headers don't show it.
- **Key path:** an error notice clears on a keypress at `>=1s`, not before,
  and the key still acts.
- **browse-claude:** `y` invokes the pager with the cursor id (headless
  `page` is a no-op, so assert via the call / a pty UI test).
- **browse-plan:** a mutation logs its `plan` argv; `~` pages the log.
- Update existing tests that asserted `_message_text` / `_error_text`
  (`test_context.py`, `test_background.py`, any browse-claude render tests).

## Out of scope

- Clipboard / OSC-52 copy (none exists today; the pager covers paste-for-now).
- A dedicated in-TUI log pane/overlay (the pager is enough).
- Per-recipe log filtering or levels.
