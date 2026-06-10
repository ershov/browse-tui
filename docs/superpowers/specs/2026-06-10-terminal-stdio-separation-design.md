# Terminal / stdin-stdout separation — stage 1 design

**Status:** PROPOSED (2026-06-10)
**Date:** 2026-06-10
**Worktree/branch:** `.claude/worktrees/stdio` / `worktree-stdio`

> **Stage 1 of two.** This effort makes `browse-tui` usable as a Unix filter —
> content on `stdin`/`stdout`, the UI on the controlling terminal. It is split
> into two deliverable epics:
>
> - **Stage 1 (this doc): separate the terminal from `stdin`/`stdout`.** Route
>   *all* UI I/O through a dedicated terminal fd so the terminal layer no longer
>   rides on `sys.stdin`/`sys.stdout`. Stage 1 includes the refactor and every
>   *natural consequence* of it — most visibly, the existing print-exit result
>   becomes cleanly capturable (`sel=$(browse-tui …)`).
> - **Stage 2 (separate spec): add stdin/stdout content capabilities.** New
>   features that *use* the now-freed std streams — recipes reading their
>   dataset from `stdin`, mid-session streaming, programmatic-output APIs beyond
>   the existing `ctx.quit(output=…)`, and a reserved path toward a structured
>   control-mode (`tmux -CC`-style). None of stage 2 is built here.

## 1. Goal & scope

### 1.1 Goal

Decouple the terminal I/O substrate (`src-tui/020-terminal.py`) from the
process's standard streams. After stage 1 the UI is painted to, and keystrokes
are read from, a single **terminal device fd** (`/dev/tty` by default), while
`sys.stdin`/`sys.stdout` are left untouched for content and results.

The observable payoff: `sel=$(browse-tui --root-cmd cat …)` works — the UI
renders on the terminal, and `stdout` carries only the print-exit selection,
with zero escape-sequence contamination.

### 1.2 In scope (stage 1)

1. A terminal-device abstraction in `020-terminal.py` (`_term_in_fd` +
   `_term_out`) that every output/input/lifecycle helper uses instead of
   `sys.stdin`/`sys.stdout`.
2. Terminal-fd **resolution policy** + a new `--tty PATH` flag (with the
   sentinel `--tty -` meaning "use the std streams").
3. **Clean-error** behaviour when no terminal is available (replacing today's
   `termios` traceback).
4. Retargeting of raw-mode setup, the `read_key`/`select` loop, the
   SIGWINCH/SIGTSTP/SIGCONT handlers, `term_suspend`/`term_resume`, and
   `term_size` to the terminal fd.
5. **Interactive shell-out handoff**: `run_external` / `page` (and any other
   interactive spawn) hand the terminal fd to the child as its `stdin`/
   `stdout`/`stderr`.
6. Removal of the now-redundant `_reopen_stdin_from_tty` dup2 hack.
7. Tests proving the separation (result-capture, shell-out, no-tty error,
   `--tty -`).

### 1.3 Out of scope (stage 2 and beyond)

- Any API for recipes to *read* content from `stdin` (beyond the pre-existing
  `--root-cmd cat`).
- Mid-session streaming of `stdin`/`stdout`.
- New programmatic-output APIs beyond today's `ctx.quit(output=…)` /
  `--on-enter print-exit`.
- Control-mode / structured protocol on `stdout`.
- Supporting genuinely *split* terminals (fd 0 and fd 1 on different ttys) —
  explicitly rejected (§7, D3).

### 1.4 Governing principles

- **The terminal is a deliberate choice, not "whichever std fd is a tty."**
  Default to `/dev/tty`; never silently probe the std fds. This is what keeps
  the result-capture guarantee a contract instead of an accident of how the
  caller wired their fds.
- **One terminal device, used for both read and write.** Mirrors curses / fzf /
  every TUI. No split-terminal support.
- **`sys.stdin`/`sys.stdout` are never repointed.** The terminal layer stops
  conflating "the controlling terminal" with "the process's std streams." (This
  is the conceptual debt the size-probe code already works around — see §2.)
- **No new behaviour beyond the refactor + its natural outcomes.** New features
  are stage 2.
- **Additive at the call sites.** Recipe authors calling `ctx.run_external` /
  `ctx.page` see no change; the framework wires the terminal into children.

## 2. Background — how terminal I/O works today

Everything funnels through one module, `020-terminal.py`. The conflation to
remove: that module writes the UI to `sys.stdout` and reads keys from
`sys.stdin`, while those same streams are also the content/result channels.

### 2.1 Output → `sys.stdout`

`write()` calls `sys.stdout.write` (`020-terminal.py:69-79`). The alt-screen /
cursor-hide / mouse-enable and their teardown use `sys.stdout.buffer.write` +
flush (`_enter_raw` `:445-446`, `_leave_raw` `:474-475`). 256-colour SGR,
scroll regions, sync-output bracketing (DEC 2026) all go through `write()`. The
differential renderer batches per-row writes into `_row_buf` and emits once on
a cache miss (`end_row` `:258-342`); the final emit is `write()` → `sys.stdout`.
There is **no** `/dev/tty` output path and **no** `isatty` check anywhere in the
source.

### 2.2 Input ← fd 0

`read_key` (`:533`), `input_ready` (`:510`), and the raw-mode setup all use
`sys.stdin.fileno()` (`tty.setraw`/`termios.tcgetattr` at `:442-443`,
`select` sets at `:522`, `:541-549`). The async self-pipe (`_notify_r`) is
`select`ed alongside fd 0 so worker threads can wake the read loop (`:448-454`,
`:541-562`).

### 2.3 Lifecycle, signals, suspend/resume

`term_init` saves termios, enters raw + alt screen, registers SIGWINCH/SIGTSTP/
SIGCONT (`:456-469`). `_handle_sigtstp`/`_handle_sigcont` (`:386-434`) restore
and re-enter raw mode — with the documented bytecode-checkpoint subtlety that a
trailing call after `os.kill` is load-bearing. `term_suspend`/`term_resume`
(`:493-506`) wrap shelling out to editors/pagers. `term_size` (`:356-377`)
already probes the three std fds *and then* `/dev/tty`, so it works even when
both std streams are pipes — the one place the code already acknowledges that
the terminal ≠ the std streams.

### 2.4 The existing stdin-pipe handling (CLI only)

`--root-cmd cat` reads `sys.stdin.buffer.read()` then calls
`_reopen_stdin_from_tty()` (`080-cli.py:1561-1567`), which `os.dup2`'s
`/dev/tty` onto fd 0 and rebuilds `sys.stdin` (`:1216-1235`) so `read_key` has a
keyboard again. This is wired **only** into the eager-CLI builder; the Python
recipe path (`cmd_run_py` → `runpy.run_path`, `:1124-1140`) does nothing with
stdin or the tty.

### 2.5 Where the result and help go

The print-exit result is stashed via `ctx.quit(code, output=…)`
(`040-state.py:5245` → `_do_quit` `:6048`) and written to `sys.stdout` **after**
teardown (`:7169-7171`). Help text is written to `sys.stdout` (`:6962-6964`).
Both are *intentional* content-on-stdout — they stay there in stage 1; they're
just no longer co-mingled with UI bytes.

### 2.6 Shell-out

`run_external` runs `subprocess.run(cmd, shell=shell, env=full_env)`
(`060-context.py:689`) — inheriting all three std fds. `page` runs
`Popen(pager, stdin=subprocess.PIPE)` (`:725`) — stdin is the text pipe,
stdout/stderr inherited. Today fd 1 is the terminal, so `less`/`$EDITOR` paint
to the screen by inheritance. Both are gated by the same `term_suspend`/
`term_resume` choke point.

### 2.7 The crash on piped stdin

Confirmed empirically: `termios.tcgetattr` on a piped fd 0 raises
`error (25) Inappropriate ioctl for device`. So a recipe that reads its dataset
from `stdin` and then calls `Browser.run()` → `term_init` → `_enter_raw`
crashes today. Stage 1's resolution policy (§3.2/§3.8) replaces that crash with
either a working `/dev/tty` session or a clean error.

## 3. Design

### 3.1 The terminal device

Two module-level globals in `020-terminal.py`, set by `term_init`:

| Name | Meaning |
|------|---------|
| `_term_in_fd` | int fd read by `os.read` / `select`; the target of raw-mode `termios`. |
| `_term_out` | a text writer (`.write(str)`, `.flush()`, `.buffer.write(bytes)`) for all UI output. |

Every current use of `sys.stdout` in the module becomes `_term_out`; every
`sys.stdin.fileno()` becomes `_term_in_fd`. The self-pipe `_notify_r` is still
`select`ed alongside `_term_in_fd`.

In the single-device case (`/dev/tty` or an explicit `--tty PATH`) both views
are one underlying fd opened `O_RDWR`; reads use raw `os.read(_term_in_fd, …)`
(as today — the text wrapper's read buffer is never used), writes use
`_term_out`. In `--tty -` mode they are fd 0 (in) and fd 1 (out).

### 3.2 Resolution order

`term_init(tty_path=None)` resolves the device in this strict order:

1. **`tty_path == '-'`** → `_term_in_fd = 0`, `_term_out = sys.stdout` (reuse the
   existing stream; no second wrapper). Asserts the std streams are a tty
   (§3.8).
2. **`tty_path` is a device path** → `open(tty_path, O_RDWR)`.
3. **`tty_path is None`** → `open('/dev/tty', O_RDWR)`.
4. **open fails** → raise a clean error (§3.8); **never** fall back to probing
   the std fds.

The opened fd (cases 2–3) is marked `O_CLOEXEC` and is *owned* (closed on
`term_restore`). In case 1 the writer is `sys.stdout` — not opened by us, and
not closed by us.

> This supersedes the auto-fallback floated during brainstorming ("use the std
> fds if they happen to be ttys"). Probing the std fds first would put the UI
> back on `stdout` in the common case and make behaviour shift the moment
> `stdout` is piped — the opposite of the contract stage 1 is establishing.

### 3.3 Output retargeting + buffering

`_term_out` is built once in `term_init`:

- single-device: `_term_out = os.fdopen(out_fd, 'w', encoding='utf-8',
  newline='')` (a `TextIOWrapper` — `.buffer` exposes the `BufferedWriter` for
  the byte-level alt-screen/mouse sequences, exactly mirroring today's
  `sys.stdout` / `sys.stdout.buffer` split).
- `--tty -`: `_term_out = sys.stdout`.

The batch-writes-then-one-flush-per-frame discipline (and the DEC 2026
`begin_sync`/`end_sync` bracketing the renderer relies on) is preserved
verbatim — only the destination object changes. `flush()` calls
`_term_out.flush()`.

### 3.4 Input retargeting

`read_key` / `input_ready` read and `select` on `_term_in_fd`. Raw-mode setup
(`tty.setraw`, `termios.tcgetattr`/`tcsetattr`) targets `_term_in_fd`. Setting
raw on the readable side affects the underlying terminal device, which is shared
with the writable side in every supported configuration, so this is correct for
both the single-device and `--tty -` cases.

### 3.5 Signals, suspend/resume, size

- SIGWINCH/SIGTSTP/SIGCONT handlers and `term_suspend`/`term_resume` write
  through `_term_out` and operate termios on `_term_in_fd`. The
  bytecode-checkpoint subtlety in `_handle_sigtstp` is preserved unchanged.
- `term_size` queries `os.get_terminal_size(_term_in_fd)` first, then falls
  back to `/dev/tty`, then `(80, 24)`. The 3-std-fd probe is dropped (the
  terminal fd is now the authoritative source).
- `_terminal_cols_for_auto` (`080-cli.py`, runs *before* `term_init` to resolve
  `--split-type=auto`) is aligned to the same policy: prefer the resolved
  `--tty` target / `/dev/tty`, not a std-fd-first probe.

### 3.6 Interactive shell-out handoff

The child gets the terminal on fd 0/1/2 by **passing the terminal fds to
subprocess**, not by closing/reopening fds in the parent. `020-terminal.py`
exposes a small accessor returning the child's `(in_fd, out_fd)` — the single
`O_RDWR` device fd for both in single-device mode, or `(0, 1)` in `--tty -`
mode (i.e. `_term_in_fd` and `_term_out.fileno()`):

- `run_external`: `subprocess.run(cmd, stdin=in_fd, stdout=out_fd,
  stderr=out_fd, …)`. subprocess `dup2`s them onto the child's 0/1/2 after
  fork, before exec.
- `page`: `Popen(pager, stdin=PIPE, stdout=out_fd, stderr=out_fd)` — stdin
  stays the text pipe; the pager reads keys from `/dev/tty` itself (as
  `cmd | less` always has).

Rationale (chosen over close-and-reopen-onto-0/1 in the parent):

- The parent's fd 0/1 are **never touched** — essential for stage 2, where they
  are the content channels.
- The parent keeps `term_fd` open for `term_resume` (termios continuity; no
  reopen). `O_CLOEXEC` + the explicit pass means no stray fd leaks into the
  child beyond 0/1/2.
- Uniform across modes and needs no `preexec_fn`. In `--tty -` mode the terminal
  fd already *is* the std streams, so the pass is effectively today's
  inherit-the-std-fds behaviour.

`stderr` → terminal (fd 2 included): during the suspend window the child owns
the screen, so its diagnostics should be visible there and must not leak into a
redirected `2>` on the parent.

Captured subprocesses (`--preview-cmd` / `--children-cmd` / `--root-cmd`,
already on `subprocess.PIPE`) are non-interactive and untouched. An audit item:
confirm the CLI `--action` runner routes through `run_external` (or wire it the
same way).

**Known limitation — `--tty -` + pager.** When the terminal *is* the std
streams there is no separate `/dev/tty` for the pager to read keys from while
its stdin carries the text. `page` degrades in that mode: write the text to
`_term_out` without interactive paging. Acceptable for a narrow fallback.

### 3.7 Result & help routing (std streams freed)

- `_quit_output` and help text continue to go to the real `sys.stdout`
  (`040-state.py:6962-6964`, `:7169-7171`) — now uncontaminated by UI bytes in
  the default `/dev/tty` mode.
- `--root-cmd cat` keeps reading `sys.stdin`, but `_reopen_stdin_from_tty` is
  **deleted**: keys now come from `_term_in_fd` (`/dev/tty`) directly, so fd 0
  is left as-is after the read.

### 3.8 No-terminal policy & errors

- Default (no `--tty`, `/dev/tty` unopenable — daemon/cron/detached): print
  `browse-tui: no controlling terminal; pass --tty - to run over stdin/stdout`
  to stderr and exit non-zero. No traceback.
- `--tty -` where the std streams are **not** a tty (someone piped them): the
  raw-mode `termios` call would fail; catch it and emit the same clean
  `not a terminal` error rather than a traceback.

`_headless` (the internal test mode that skips `term_init` and all rendering;
`040-state.py:6967`, `:6983`, `:7150`) is unrelated and unchanged — it is *not*
the `--tty -` mode (which runs the full terminal lifecycle on the std fds).

## 4. The `--tty` flag

### 4.1 CLI

`--tty PATH` (metavar `PATH`, default `/dev/tty`). The sentinel value `-`
selects the std-streams mode. `run_tui` resolves it and passes it to
`term_init(tty_path=…)`.

### 4.2 Recipes

`Browser.run()` already auto-detects `-h`/`--help` in `sys.argv[1:]`
(`040-state.py:6962`). It is extended to also recognise `--tty PATH` / `--tty -`
the same way, so a recipe (`./my-recipe --tty -`) gets the behaviour without
wiring its own argparse. Recipes that consume `--tty` via their own argparse
first are unaffected (they strip it from `sys.argv`). Default remains
`/dev/tty`.

## 5. Testing

- **Existing tmux UI tests** (`test/ui/`) drive the binary under a pane pty. The
  UI moves from `stdout` to `/dev/tty`, which under the fixture *is* that pty —
  so the snapshots should be unchanged. **Verify** `/dev/tty` resolves to the
  pane pty in `TmuxFixture` before relying on this; if not, the fixture passes
  `--tty -` (the pane's std streams are the pty) as a fallback.
- **Unit tests** using `_headless` are unaffected (they skip `term_init`).
- **New: result-capture separation.** Pipe content via `--root-cmd cat`,
  capture `stdout` (a pipe), script a selection + Enter over the pty, assert
  captured `stdout` contains **only** the formatted result — zero `\033[`
  bytes.
- **New: shell-out with piped stdout.** With `stdout` captured, trigger an
  action that runs an external command; assert its output landed on the
  terminal (pty), not in the captured `stdout`.
- **New: no-tty clean error.** Run with `/dev/tty` unavailable and no `--tty -`;
  assert the `no controlling terminal` message on stderr and a non-zero exit
  (not a traceback).
- **New: `--tty -` smoke.** Run with std streams on a pty and `--tty -`; assert
  a normal interactive session (render + key input) works.

## 6. Files touched

| File | Change |
|------|--------|
| `src-tui/020-terminal.py` | `_term_in_fd`/`_term_out` globals; resolution in `term_init(tty_path)`; retarget all `sys.stdout`/`sys.stdin` uses, signals, suspend/resume, `term_size`; delete dependence on std streams. |
| `src-tui/080-cli.py` | `--tty` arg; pass to `term_init`; delete `_reopen_stdin_from_tty`; `--root-cmd cat` no longer reopens; align `_terminal_cols_for_auto`; no-tty error. |
| `src-tui/040-state.py` | `run()` auto-detect of `--tty`; pass `tty_path` into `term_init`; (result/help stdout writes unchanged). |
| `src-tui/060-context.py` | `run_external` / `page` pass the terminal fd as child 0/1/2; `page` degrade under `--tty -`. |
| `test/ui/…`, `test/unit/…` | New separation/shell-out/no-tty/`--tty -` tests. |
| `docs/cli.md` | Document `--tty`, the capturable-result contract, the no-tty error. |

## 7. Decisions (final)

- **D1.** Stage 1 = the refactor + all natural outcomes (capturable print-exit
  result, UI on the terminal). New features → stage 2.
- **D2.** No terminal → clean error by default; `--tty -` is an explicit opt-in
  that still runs the full `term_init` on the std streams.
- **D3.** One terminal device used for both read and write. No split-terminal
  support; no auto-probe of the std fds; `/dev/tty` is the default.
- **D4.** Interactive children get the terminal via `subprocess`
  `stdin=stdout=stderr=term_fd` (parent fds untouched, parent keeps `term_fd`),
  not close-and-reopen. `stderr` → terminal too.
- **D5.** Flag spelling: `--tty PATH`, default `/dev/tty`, sentinel `-` = std
  streams.
- **D6.** `_reopen_stdin_from_tty` is removed.
- **D7.** `--tty -` mode degrades the interactive pager (no `/dev/tty` for keys).

## 8. Risks / open implementation notes

- **tmux fixture `/dev/tty` resolution** — the one thing that could ripple
  through the whole UI suite. Verify early (§5).
- **Double-buffering in `--tty -`** — `_term_out = sys.stdout` shares fd 1 with
  the result write; flush `_term_out` before the post-teardown result write.
  (Accepted that UI + result co-mingle in this mode.)
- **fd ownership** — close the opened `/dev/tty` fd on `term_restore` only when
  we own it (cases 2–3), never the std fds.
- **`O_CLOEXEC` + subprocess** — confirm the passed `term_fd` reaches the child
  as 0/1/2 while not leaking as a higher fd (subprocess `close_fds=True` default
  + explicit pass).
