# Stage 2 — stdin/stdout content channels (design)

- **Date:** 2026-06-11 (rev 2 — same day; rev 1 feedback folded in)
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
- fd hygiene at startup: fd 0 / fd 1 detached from the terminal when they are ttys;
  `stderr` untouched.
- Buffered output: `ctx.print()`; tty-stdout output delivered at exit (D3).
- Synchronous initial input: `ctx.stdin`.
- Streaming input: opt-in `on_stdin` hook with raw-chunk and delimited-record modes.
- CLI: `--root-cmd -` (canonical direct stdin list); bare `--root-cmd cat` kept as an alias.
- Recipe conversions (explicit `-` argument): `browse-md -`, `browse-fs -`, `browse-git -`.

**Out of scope**
- Any change to `stderr` (left as an escape hatch; tracebacks reach the user).
- A dedicated `--root-file` flag (covered by `< file`, `--root-cmd -`, recipe path args).
- Auto-detection of piped stdin (explicit `-` only — see D8).
- Hook signature validation / adaptive passing / error surfacing — deferred to the
  framework-wide hook-contract epic **#855**; this stage assumes user-supplied hooks
  have the correct signature.
- Structured bidirectional / control-mode protocols on the streams.

## 3. Design

### 3.1 fd hygiene at startup (normal mode only)

In normal mode (UI on `/dev/tty`), during `term_init`:
- if `os.isatty(0)`: `dup2(/dev/null, 0)` — a recipe read gets immediate EOF instead of
  blocking on, or stealing keystrokes from, the UI.
- if `os.isatty(1)`: save the real stdout fd (`os.dup`), then `dup2(/dev/null, 1)` — a
  stray raw `print()` mid-session vanishes harmlessly instead of painting over the
  alt-screen. Buffered output (`ctx.print`, quit output) is written to the **saved** fd at
  teardown (§3.2).

When `stdin` / `stdout` are pipes or files (the content cases), they are left as-is.

`stderr` (fd 2) is **left untouched** — an escape hatch for ad-hoc recipe diagnostics, and
uncaught-exception tracebacks reach the real `stderr` with no save/restore machinery. The
framework's own user-facing messages go through the existing `ctx.flash` / `ctx.error` /
`ctx.log` (the `~` view), not `stderr`.

`--tty -` mode is **excluded**: there fd 0/1 *are* the UI, so they are never redirected and
the content channels are unavailable in that mode.

### 3.2 Output — `ctx.print()`

`ctx.print(text, end='\n')` mirrors builtin `print` (newline-terminated, `end` overridable)
and appends to a single output buffer shared with the quit output, so everything written is
delivered in strict FIFO order. Delivery depends on what stdout is:

- **pipe / file:** the event loop drains the buffer to fd 1 **non-blocking** whenever fd 1
  is writable (§3.5), so a slow / backpressuring consumer never blocks the UI. The final
  drain at teardown is a blocking write (the UI is gone; waiting on the consumer is then
  correct backpressure).
- **tty:** the buffer is held for the whole session and written at teardown to the saved
  real stdout fd, **after** the terminal is restored (alt-screen exited) — so prints and the
  selection land in normal scrollback exactly where an fzf result does. A bare interactive
  run keeps echoing its result (D3).
- **write error (`EPIPE` etc.):** the channel is marked dead permanently — the buffer is
  dropped, fd 1 leaves the loop forever, and subsequent `ctx.print()` calls are no-ops. The
  UI stays alive (it is on the terminal device, independent of stdout).

`ctx.quit(code, output)` is unchanged and appends to the same buffer (FIFO: prints first,
then quit output).

### 3.3 Input — initial ingest (synchronous, before run)

`ctx.stdin` exposes a prepared text stream over fd 0 (`ctx.stdin.buffer` for bytes). A
recipe reads whatever it needs **before** `browser.run()`:
- slurp → `ctx.stdin.read()`
- lines → `for line in ctx.stdin`
- NUL / custom records → `ctx.stdin.buffer.read().split(b'\0')`
- partial → read some and stop

When `stdin` was a tty, fd 0 is `/dev/null` (§3.1), so these reads return EOF immediately —
a recipe uniformly does "read; if empty, fall back to args / path." (No `read_stdin()`
helper: plain reads on the prepared stream suffice.)

**One-shot + reload semantics.** stdin cannot be re-read; whoever parses it keeps the parse.
`ctrl-r` reload re-invokes `get_children`, which serves the parsed in-memory data — identical
to today's eager `--root-cmd` semantics (the command is not re-run on reload either). No
memoization machinery is needed.

### 3.4 Input — streaming (during run)

A recipe opts in via `BrowserConfig` fields (the established hook surface):
- `on_stdin` — the hook (callable);
- `stdin_delimiter` — `None` (default): raw chunks as they arrive; or any string
  (`'\n'` for lines, `'\0'` for NUL records, …): the framework owns partial-record
  buffering and delivers **one complete record per call**, delimiter stripped;
- `stdin_bytes` — `False` (default): `data` is `str`, decoded with an **incremental
  utf-8 decoder** (multibyte sequences split across chunk boundaries are handled
  correctly); `True`: raw `bytes`.

The hook signature (the framework always passes all keyword arguments):

```python
on_stdin(ctx, data, *, delimiter, is_eof, errno)
```

- `data` — `str` (or `bytes`), **never `None`**; may be `''`/`b''` (e.g. an empty record).
  Empty data flows through typical processing as zero records, so recipes that don't
  inspect the flags never crash.
- `delimiter` — exactly what was stripped after `data`: the configured delimiter in record
  mode; `''` in raw-chunk mode and on the final unterminated record. (A future regex-based
  delimiter would pass the actual matched text — the signature already accommodates it.)
- `is_eof` — `True` on the final call; its `data` is the trailing unterminated record, or
  empty if none. Record mode is therefore unambiguous: `"a\n\n"` delivers record `"a"`,
  record `""`, then the EOF call.
- `errno` — `0` normally; on a read error the stream ends with a final call carrying the
  numeric errno **and** `is_eof=True` (error implies no more data, so recipes that only
  check `is_eof` handle error-end correctly for free).

Streaming begins **where ingest left off** — synchronous reads (§3.3) happen first, and the
loop streams the unread remainder; slurp-then-stream compose (e.g. read a header, then
stream records). Any bytes the sync phase read-ahead-buffered but did not hand to the recipe
are delivered to `on_stdin` before the loop reads more from fd 0, so no bytes are lost at
the hand-off.

The hook folds incoming data into the tree via the existing `ctx.update_data` / `ctx.upsert`.

Per **#855** (deferred): no signature validation in this stage — a wrong-arity hook is the
recipe author's bug, with the known silent-swallow caveat, until the hook-contract epic
retrofits checking framework-wide.

### 3.5 Event-loop integration

The existing `select()` loop (terminal-input fd + `notify_wake` self-pipe) gains,
**conditionally**:
- **fd 1 in the write-set** only while stdout is a pipe/file **and** the buffer is
  non-empty **and** the channel is alive. On write error: dead forever (buffer dropped,
  fd removed, never re-added).
- **fd 0 in the read-set** only when `on_stdin` is registered **and** the stream has not
  ended. After the EOF / error delivery, the fd is removed forever.

The tty-stdin case needs no special-casing: fd 0 is `/dev/null`, the first read returns
EOF, the hook receives its EOF call, the fd leaves the set — the same code path as a real
pipe ending.

When neither channel is in use, the select sets are exactly as today — zero added work for
recipes that don't use the feature. `O_NONBLOCK` is applied to fd 0 only for the streaming
phase (synchronous ingest stays blocking) and to fd 1 only while pipe-draining (restored at
teardown). Wakeups reuse the stage-1 `notify_wake` self-pipe.

### 3.6 CLI

- `--root-cmd -` — canonical spelling for "read the root list directly from stdin" (no
  subprocess), parsed per `--input` / `--fields` as today. The direct read already exists
  as stage 1's `--root-cmd cat` special case; `-` becomes the canonical name for it.
- Bare `--root-cmd cat` — kept as an alias for `-` (exactly `cat` only; `--root-cmd
  'cat file'` still runs as a command).

### 3.7 Recipe conversions

All via an explicit `-` argument (no auto-detection — D8):
- `browse-md -` — read the document from stdin. Bare `browse-md` keeps browsing the
  current directory.
- `browse-fs -` — display the files/directories listed on stdin (one path per line).
- `browse-git -` — sniff the git output type from the stream — diff / log / status — and
  build the matching tree (e.g. diff → file tree with per-file diff previews). The meatiest
  conversion; exact sniffing rules settled in its ticket.

## 4. Decisions

- **D1 — reads + writes ship together.** One `select()` integration; splitting would touch the loop twice.
- **D2 — fd 0 and fd 1 are detached from the terminal when ttys** (normal mode): fd 0 → `/dev/null`; fd 1 → `/dev/null` with the real stdout saved for the teardown dump. `stderr` untouched (D4).
- **D3 — tty stdout output is delivered at exit** (rev 1 reversed): `ctx.print` output + selection are written to the terminal's normal scrollback after the UI exits — the fzf model, matching user expectation that a bare run still prints its result. Pipe stdout streams live during the session instead.
- **D4 — `stderr` untouched.** Escape hatch; no save/restore; tracebacks stay visible. User-facing messages use `flash` / `error` / `log`.
- **D5 — slurp + stream compose; stdin is one-shot.** Sync reads pre-run, streaming continues from the unread remainder; reload re-serves the in-memory parse (matches eager `--root-cmd` semantics).
- **D6 — `on_stdin(ctx, data, *, delimiter, is_eof, errno)`.** `data` never `None`; `str` via incremental utf-8 by default, `bytes` opt-in (`stdin_bytes`); record mode via `stdin_delimiter` delivers one stripped record per call with the stripped text in `delimiter`; EOF = final call with `is_eof=True` (data = trailing partial or empty); error end = numeric `errno` + `is_eof=True`.
- **D7 — `ctx.print` mirrors builtin `print`** (newline default, `end` overridable); single FIFO buffer shared with quit output; never blocks the UI; permanently no-ops after a dead-pipe error.
- **D8 — no auto-detection of piped stdin; explicit `-` only.** "stdin is not a tty" ≠ "content was piped": in scripted contexts (ssh, make, git hooks, CI) stdin is routinely a pipe or `/dev/null` with nothing in it, and auto mode would shadow the recipes' meaningful bare forms (browse cwd / repo log) with an empty UI; "pipe with data" cannot be probed without blocking or racing the producer.
- **D9 — no `--root-file`.** Covered by existing mechanisms; revisit only if a real need appears.
- **D10 — hook signature validation deferred to #855.** This stage assumes correct signatures; the hook-contract epic retrofits checking / adaptive passing / error surfacing framework-wide.

## 5. Testing

- **fd hygiene:** tty stdin / stdout (pty) → fd 0 reads EOF, stray writes vanish; piped
  stdin / stdout left intact; `--tty -` excluded from redirection.
- **`ctx.print`:** piped stdout receives prints + quit output in FIFO order; a slow /
  backpressuring reader does not block the UI (loop-drain); tty stdout → output appears in
  scrollback after exit (pty test: prints, then selection, after alt-screen teardown);
  consumer closes early (`EPIPE`) → UI stays alive, buffer dropped, later prints no-op.
- **ingest:** slurp / lines / NUL / partial via `ctx.stdin`; tty stdin → immediate EOF;
  reload after stdin ingest re-serves the parsed data.
- **streaming:** raw chunks arrive as read; record mode framing (records split exactly on
  the delimiter, empty records preserved, trailing unterminated record delivered on EOF);
  multibyte utf-8 split across chunk boundaries; bytes mode; EOF call shape; read-error →
  `errno` + permanent removal; ingest-then-stream hand-off loses no bytes.
- **CLI:** `--root-cmd -` builds the list from stdin; bare `cat` alias unchanged behavior;
  `--root-cmd 'cat file'` still runs as a command.
- **recipes:** `browse-md -` / `browse-fs -` / `browse-git -` end-to-end (headless);
  bare forms unchanged (`browse-md` still browses cwd).
- **regression:** stage-1 capture / result tests stay green.

## 6. Out of scope / future

- `--root-file` (only if a concrete need appears).
- Further streaming recipes once a real live-feed use case shows up.
- Structured bidirectional / control-mode protocol on the freed streams.
- Hook-contract epic **#855**: signature checking, adaptive passing, hook-error surfacing.
