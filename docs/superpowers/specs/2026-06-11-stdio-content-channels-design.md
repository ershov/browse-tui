# Stage 2 — stdin/stdout content channels (design)

- **Date:** 2026-06-11
- **Status:** Draft for review
- **Branch:** `worktree-stdio`
- **Epic:** #849 (terminal separation + content channels); this is its stage-2 deliverable **#835**.
- **Builds on:** stage 1 (#850) — `docs/superpowers/specs/2026-06-10-terminal-stdio-separation-design.md`

## 1. Background & goal

Stage 1 moved all UI I/O onto a dedicated terminal device (`/dev/tty`, or `--tty PATH`),
leaving the process's `stdin` / `stdout` free. Stage 2 turns that freedom into first-class
**content channels**: recipes (and the bare framework) read input from `stdin` and write
results to `stdout`, with the live cases (streaming reads, non-blocking writes) integrated
into the existing `select()` event loop so the UI never blocks.

Reads and writes ship **together** because they are one mechanism: the loop already
`select()`s on the terminal-input fd and the `notify_wake` self-pipe; stage 2 adds the
content fds to that same loop — fd 0 readable → deliver input, fd 1 writable → drain
buffered output.

## 2. Scope

**In scope**
- fd hygiene at startup: fd 0 / fd 1 → `/dev/null` when they are ttys; `stderr` untouched.
- Buffered output: `ctx.print()`.
- Synchronous initial input: `ctx.stdin` / `ctx.read_stdin()`.
- Streaming input: opt-in `on_stdin` recipe hook.
- CLI: `--root-cmd -` (direct stdin list); `--root-cmd cat` retained.
- Reference recipe: `browse-md` reads its document from `stdin`. `on_stdin` validated via tests.

**Out of scope**
- Any change to `stderr` (left as an escape hatch).
- A dedicated `--root-file` flag (covered by `< file`, `--root-cmd -`, recipe path args).
- Converting `browse-git` / `browse-fs` to stdin (fast-follow tickets once the pattern is proven).
- Structured bidirectional / control-mode protocols on the streams.

## 3. Design

### 3.1 fd hygiene at startup (normal mode only)

In normal mode (UI on `/dev/tty`), during `term_init`:
- if `os.isatty(0)`: `dup2(/dev/null, 0)`
- if `os.isatty(1)`: `dup2(/dev/null, 1)`

This guarantees the content channels are never the controlling terminal. A recipe that
reads `stdin` when it is interactive gets immediate EOF instead of blocking on — or stealing
keystrokes from — the UI; a write to `stdout` when it is the terminal goes to `/dev/null`
instead of painting over the alt-screen. When `stdin` / `stdout` are pipes or files (the
content cases), they are left as-is.

`stderr` (fd 2) is **left untouched** — an escape hatch for ad-hoc recipe diagnostics, and
uncaught-exception tracebacks continue to reach the real `stderr` with no save/restore. The
framework's own user-facing messages go through the existing `ctx.flash` / `ctx.error` /
`ctx.log` (the `~` view), not `stderr`.

`--tty -` mode is **excluded**: there fd 0/1 *are* the UI, so they are never redirected and
the content channels are unavailable in that mode.

### 3.2 Output — `ctx.print()`

`ctx.print(text, end='\n')` writes to `stdout`, mirroring builtin `print` (newline-terminated,
`end` overridable). Writes append to an output buffer; the event loop drains the buffer to
fd 1 **non-blocking** whenever fd 1 is writable, so a slow / backpressuring consumer never
blocks the UI. fd 1 is set non-blocking while buffered output is in use and restored at
teardown; the final drain at teardown is a blocking write (the UI is gone, so waiting on the
consumer is correct backpressure).

`ctx.quit(code, output)` is unchanged and shares the same buffer, so mid-session `print()`
output and the final quit output preserve FIFO order on the stream.

Because fd 1 is `/dev/null` when `stdout` is a tty (§3.1), `print()` and the quit / selection
output are delivered **only when `stdout` is captured** (a pipe / file). **Behavior note (D3):**
running the tool bare on an interactive terminal no longer echoes the final selection to the
terminal — the result is a capture-only channel. Capture via `$(…)` and the test suite pipe
`stdout`, so they are unaffected.

### 3.3 Input — initial ingest (synchronous, before run)

`ctx.stdin` exposes a prepared file-like over fd 0; `ctx.read_stdin()` is a memoized full
slurp. A recipe reads whatever it needs **before** `browser.run()`:
- slurp → `ctx.read_stdin()` / `ctx.stdin.read()`
- lines → `for line in ctx.stdin`
- NUL / custom records → `ctx.stdin.buffer.read().split(b'\0')`
- partial → read some and stop

When `stdin` was a tty, fd 0 is `/dev/null` (§3.1), so these reads return EOF immediately — a
recipe uniformly does "read; if empty, fall back to args / path."

### 3.4 Input — streaming (during run)

A recipe opts into live input by defining `on_stdin`. After the recipe's synchronous reads
(§3.3), `browser.run()` continues reading fd 0 from **where the recipe left off** — slurping
and streaming **compose**, they are not mutually exclusive. The loop reads fd 0 non-blocking
when readable and calls the hook:

- `on_stdin(ctx, data)` — `data` is a **utf-8-decoded `str` by default**; a recipe that wants
  raw bytes sets an opt-in flag (recipe-level `stdin_bytes = True` / a `raw=True` registration
  option — exact spelling settled in the plan). For `str` mode the framework uses an
  **incremental utf-8 decoder**, so a multibyte sequence split across chunk boundaries is
  handled correctly.
- **EOF** is delivered as a final call with empty `data` (`''` / `b''`).

The hook folds incoming data into the tree via the existing `ctx.update_data` / `ctx.upsert`.
Record framing (lines, NUL, JSON-lines) is the recipe's responsibility — it buffers partial
records between calls.

*Implementation note:* the synchronous phase (§3.3) and the streaming phase share one fd and
one read position. Any bytes the sync phase read-ahead-buffered but did not hand to the recipe
must be delivered to `on_stdin` before the loop reads more from fd 0, so no bytes are lost at
the hand-off.

### 3.5 Event-loop integration

The existing `select()` loop (terminal-input fd + `notify_wake` self-pipe) gains, **conditionally**:
- fd 1 in the write-set **only while output is buffered** (an always-writable fd otherwise busy-loops);
- fd 0 in the read-set **only when a recipe registered `on_stdin`**.

When neither is in use there is no added work — no cost for recipes that don't use the channels.
Wakeups reuse the stage-1 `notify_wake` self-pipe.

### 3.6 CLI

- `--root-cmd -` reads the root list directly from `stdin` (no `cat` subprocess), parsed per
  `--input` / `--fields` as today.
- `--root-cmd cat` continues to work unchanged (back-compat).

### 3.7 Reference recipe

- `browse-md`: read the document from `stdin` when no path arg is given (initial ingest, §3.3).
- `on_stdin` is validated end-to-end via a headless test harness (no shipped streaming recipe
  in this stage; one can be added when a concrete streaming use case appears).

## 4. Decisions & behavior notes

- **D1 — reads + writes ship together.** One `select()` integration; splitting would touch the loop twice.
- **D2 — fd 0 and fd 1 → `/dev/null` when ttys** (normal mode). Cheap, symmetric safety; content channels are strictly "pipe / file only."
- **D3 — `stdout` is capture-only.** Consequence of D2: a bare interactive run no longer echoes the selection to the terminal. Accepted to avoid held-until-teardown tty machinery; capture (`$(…)`, tests) is unaffected.
- **D4 — `stderr` untouched.** Escape hatch; avoids traceback save/restore; user messages use `flash` / `error` / `log`.
- **D5 — slurp + stream compose.** Recipe reads synchronously before `run()`; streaming continues from the unread remainder.
- **D6 — `on_stdin` delivers `str` (utf-8, incremental decode) by default; opt-in bytes.** EOF = final empty chunk.
- **D7 — `ctx.print` mirrors builtin `print`** (newline default, `end` overridable).
- **D8 — no `--root-file`.** Covered by existing mechanisms; revisit only if streaming reserves stdin and static initial data is also needed.

## 5. Testing

- **fd hygiene:** tty stdin / stdout (pty) → fd 0/1 are `/dev/null`; piped stdin / stdout left intact; `--tty -` excluded.
- **`ctx.print`:** captured stdout receives buffered output in order; a slow / backpressuring reader does not block the UI (loop-drain); tty stdout → output discarded, no screen corruption.
- **ingest:** slurp / lines / NUL / partial via `ctx.stdin`; tty stdin → EOF.
- **streaming:** `on_stdin` delivers chunks as they arrive; `str` vs bytes; multibyte split across chunks; EOF; compose-with-ingest (read header, then stream).
- **CLI:** `--root-cmd -` builds the list from stdin; `--root-cmd cat` unchanged.
- **regression:** existing capture / result tests stay green (stdout piped in tests).

## 6. Out of scope / future

- `--root-file` (if streaming later reserves stdin and static initial data is also needed).
- `browse-git` / `browse-fs` stdin conversions (fast-follow).
- Structured bidirectional / control-mode protocol on the freed streams.
