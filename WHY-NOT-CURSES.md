# Why `browse-tui` doesn't use `curses`

A comparative analysis of the current hand-rolled VT100 terminal layer against
a hypothetical `curses`-based implementation.

## What this project actually does at the I/O layer

`src-tui/020-terminal.py` (740 lines) is a hand-rolled VT100/xterm I/O substrate.
It bypasses `curses` entirely:

- **Output** — raw `\033[…` bytes via `sys.stdout.write`
  (`write/move/clear_line/clear_columns/set_style/set_scroll_region/scroll_up/scroll_down/begin_sync/end_sync`
  at `020-terminal.py:68-302`). 256-color SGR is hand-built; synchronized output
  uses DEC mode 2026 (`\033[?2026h/l`, `020-terminal.py:295-302`), which is a
  recent xterm/kitty/wezterm feature `curses` doesn't speak.
- **Input** — a custom escape-sequence parser (`read_key` + `_read_csi`,
  `020-terminal.py:460-740`). It decodes CSI, SS3, SGR mouse (`?1006`), and
  CSI-u (kitty keyboard protocol for `shift-enter`/`alt-enter`), with a 50 ms
  peek to disambiguate bare ESC from Alt-combos.
- **Terminal lifecycle** — `termios.tcgetattr` + `tty.setraw`, plus
  alt-screen/cursor-hide/mouse-tracking enable as raw bytes (`_enter_raw`,
  `020-terminal.py:388-396`).
- **Signals** — `SIGWINCH` flag, plus a `SIGTSTP`/`SIGCONT` round-trip
  (`020-terminal.py:336-385`) that lets Ctrl-Z to a job-control stop work
  cleanly. The docstrings call out a non-obvious Python signal-dispatch quirk
  that affected the design.
- **Async wakeup** — a self-pipe (`_notify_r`/`_notify_w`) selected next to
  stdin so worker threads can interrupt `read_key` (`020-terminal.py:46-47,
  398-404, 471-489`).
- **Differential rendering** — a row-buffer shim (`begin_row`/`end_row`,
  `020-terminal.py:179-292`) that captures per-row writes, computes display
  width with East Asian Width handling (`_visible_len` at
  `020-terminal.py:141-176`), diffs against `PaneCache.lines`
  (`040-state.py:118-188`), and emits only on cache miss with surgical pad
  logic.

Above the terminal layer: state (`040-state.py`), pure-ish renderer
(`050-render.py`), context sub-flows, action dispatch, CLI — no terminal
coupling beyond the helpers above.

## What `curses` would change

`curses` (the stdlib `_curses` binding to ncurses) would replace roughly all
of `020-terminal.py` and parts of the diffing logic in
`040-state.py`/`end_row`:

| Concern | Current implementation | With `curses` |
|---|---|---|
| Raw mode, alt screen, cursor hide, termios save/restore | Hand-rolled (`_enter_raw`, `_leave_raw`) | `curses.wrapper` / `initscr` + `cbreak`/`noecho`/`curs_set` |
| Output bytes | Direct VT100 escapes | `addstr`/`addch` + `attron`/`attrset`, ncurses emits the right escapes for `$TERM` |
| Color | Hand-built `\033[38;5;Nm` | `init_pair` + `color_pair`; you get 8/16/256 depending on `$TERM` |
| Differential rendering | Bespoke row cache + `_visible_len` + sync-output bracketing | `wnoutrefresh` + `doupdate`; ncurses computes the minimal diff against its internal screen model |
| Resize | `SIGWINCH` flag → `'all'` redraw | `KEY_RESIZE` from `getch`, plus `resizeterm`/`update_lines_cols` |
| Key parsing | Hand-rolled CSI/SS3/CSI-u parser, ~280 lines | `keypad(True)` + symbolic constants (`KEY_UP`, `KEY_PPAGE`, …); ncurses owns the terminfo lookup |
| Mouse | SGR `?1006` parser | `mousemask` + `getmouse` (uses xterm protocols ncurses knows about) |
| Synchronized output (DEC 2026) | `begin_sync`/`end_sync` around every paint | Not exposed by ncurses; you'd lose the explicit BSU/ESU bracketing and rely on `doupdate`'s buffered emit |
| `SIGTSTP`/`SIGCONT` | Hand-coded round-trip with the bytecode-checkpoint comment | Mostly works out of the box; ncurses re-paints on resume, but the alt-screen/scroll-region behaviors differ subtly |

## Comparative analysis

### What `curses` would clearly improve

1. **Less I/O code to own.** Roughly the entire 740-line `020-terminal.py`
   plus `_visible_len` (CJK width) and the row-cache machinery (`PaneCache`,
   `begin_row`/`end_row`) collapses to maybe 100-200 lines of `curses.window`
   calls. ncurses already implements the diff, the width tables, the terminfo
   lookup, the signal handling.
2. **Portability across `$TERM`.** The current code hard-codes xterm-flavored
   sequences. Anything emitting `\033[?1049h` or `\033[38;5;Nm` works on every
   modern terminal but is technically lying about what `linux`,
   `screen-256color`, or older entries actually accept. ncurses checks
   terminfo and downgrades. (In practice this rarely matters — modern targets
   all speak xterm — but it's a real gap.)
3. **CJK width.** ncurses (with `ncursesw` and a UTF-8 locale) already knows
   East Asian Width; `_visible_len` is a re-implementation done in commit
   `1935365`.
4. **Key-name table.** ncurses ships hundreds of function-key/keypad mappings
   via terminfo. The current parser handles ~30 keys plus the kitty CSI-u
   extension for `shift-enter`/`alt-enter`.

### What `curses` would make worse — and why the project chose against it

1. **Single-file, dependency-free is a stated requirement.** The README and
   `010-prelude.py` both emphasize "single executable Python file, Python 3.8+
   the only requirement." `curses` itself is stdlib so that's fine — but
   `curses` is a thin wrapper around the system `libncurses`, and on minimal
   Linux containers / Alpine / some BSDs the `_curses` extension is not built
   or `libncursesw.so.6` is missing. The current implementation needs only
   `termios`/`tty`/`select`/`signal`, all guaranteed-present on POSIX.
2. **DEC 2026 synchronized output.** `begin_sync`/`end_sync` brackets the
   whole frame so modern terminals swap atomically (no tearing on resize, no
   flicker on partial repaints). ncurses doesn't expose this — `doupdate`
   buffers then flushes, but doesn't emit BSU/ESU. For a 3-pane layout that
   frequently repaints partial regions during async preview/children
   resolution, this is visible.
3. **Self-pipe wakeup.** The post-queue + self-pipe pattern
   (`040-state.py:1284`, `020-terminal.py:398-404, 471-489`) lets background
   threads (`_children_worker`, `_preview_worker`, recipe-spawned watchers)
   wake the main loop's `select`. With `curses.getch` (blocking) you'd have
   to either: (a) put `getch` in nodelay mode and poll, burning CPU;
   (b) use `halfdelay` (deciseconds resolution, sloppy); or (c) drop into
   `select` on stdin yourself, which means most of the current I/O design
   comes back. The explicit `select(stdin, pipe)` model is hard to give up
   cleanly.
4. **Kitty CSI-u.** `_read_csi` decodes `\033[13;2u` as `shift-enter`.
   ncurses's terminfo doesn't have these — you'd lose the shift-Enter "search
   backwards" binding the README documents.
5. **Synchronized SIGTSTP/SIGCONT semantics.** `_handle_sigtstp` has a
   comment-load (`020-terminal.py:336-385`) about Python's bytecode-checkpoint
   signal dispatch and why a trailing function call after `os.kill` matters.
   The same constraints exist in a `curses` build, but `curses` adds its own
   state machine on top (`def_prog_mode`/`reset_prog_mode`/`endwin`/`refresh`)
   that interacts with this. The `test_suspend.py` UI test exists precisely
   because this is fiddly; rewriting it on top of `curses` would mean
   re-deriving the working sequence.
6. **The differential renderer is *more* aggressive than ncurses's.**
   `end_row` (`020-terminal.py:208-292`) only diffs visible bytes after
   stripping SGR — and stores them in a per-pane cache that's geometry-aware
   (`PaneCache` invalidates on `prev_rect != rect`). ncurses does cell-level
   diffing, which is cheaper per-row but loses the "if the same pre-formatted
   row produced identical bytes, skip it entirely" short-circuit. For the
   common case where typing `j`/`k` only swaps which row carries the cursor
   highlight, the current cache hits ~all unchanged rows in O(1)
   compare-and-skip. ncurses would walk every cell.
7. **256-color directly.** The renderer assumes 256 colors and emits indices
   0-255 via `_TAG_STYLE`. `curses.init_pair` requires pre-allocating (fg, bg)
   pairs (`COLORS * COLOR_PAIRS` matrix), which is awkward for a
   recipe-defined palette. Doable but more boilerplate.

### Other tradeoffs worth naming

- **Complexity vs control.** The current code is ~5,700 lines total, of which
  ~740 are terminal I/O. A `curses` port would shrink the I/O layer but
  introduce a `curses` dependency surface that's notoriously underdocumented
  at the edges (resize, mouse, color-pair lifecycle, sub-windows). The
  existing layer is direct: a one-page mental model of "writes go to
  `_row_buf`, diffed in `end_row`, flushed once" beats a multi-page mental
  model of "ncurses internal state + Python wrapper + your `Window`s."
- **Testability.** UI tests use a `TmuxFixture` driving the binary and
  snapshotting the screen (`test/ui/`). Both implementations would be
  testable the same way. But the unit-test loader (`test/_loader.py`) cleanly
  substitutes module-level globals like `_notify_r`, `read_key`,
  `g_resize_flag`. Stubbing ncurses per-test is harder (it's an extension
  module).
- **Threading.** All cross-thread mutation goes through `Browser.post(fn)` →
  self-pipe → main loop drain (`docs/internals.md:151-184`). This pattern
  *requires* an awakeable `select`; it doesn't translate cleanly to `getch`.

## Conclusion

For this codebase specifically, **`curses` would be a net loss**, even though
it's the textbook answer for "build a Python TUI." Three reasons:

1. The async/threading model (worker threads → self-pipe → main `select`) is
   the spine of the architecture. Replacing it for `curses` either requires
   keeping `select(stdin)` yourself (in which case you've kept ~half of
   `020-terminal.py`) or polling (CPU cost + latency).
2. The differential renderer is genuinely better than ncurses for this UI —
   it caches at the row-bytes level after SGR, which beats cell-level diffing
   for the dominant repaint patterns (cursor move, async preview/children
   resolve).
3. Synchronized output (DEC 2026) and CSI-u keyboard protocol are not in
   ncurses; the project uses both.

`curses` would be the right call if (a) you needed to support older/exotic
terminals beyond the xterm family, (b) the threading model were synchronous,
or (c) the project budget didn't include 740 lines of terminal code. None of
those holds here. The hand-rolled VT100 path is paying for itself.

Where `curses` would still win: a smaller, simpler TUI that doesn't have
async workers, doesn't need synchronized output, and doesn't care about CJK
width or kitty extensions. Then `curses.wrapper` + a few `addstr`/`refresh`
calls is plainly easier than reinventing termios setup. This is just not
that project.
