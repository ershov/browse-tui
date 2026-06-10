"""browse-tui: terminal layer (raw mode, key reader, signals, mouse, self-pipe).

Provides the low-level I/O substrate the UI builds on:

* VT100 output helpers (cursor movement, scroll regions, 256-colour styling)
* Raw-mode lifecycle: ``term_init`` / ``term_restore`` plus
  ``term_suspend`` / ``term_resume`` for shelling out to editors and pagers
* Signal handlers for SIGWINCH (resize), SIGTSTP (clean stop) and SIGCONT
  (resume into raw mode again)
* A self-pipe (``_notify_r`` / ``_notify_w``) so background threads can
  wake the main loop's ``select`` via ``notify_wake``
* ``read_key`` -- a VT100/SGR/CSI-u escape-sequence parser that returns
  string keynames like ``'up'``, ``'ctrl-r'``, ``'shift-enter'``,
  ``'mouse-click:R:C'``, ``'_notify'`` and ``'esc'``

This module is pure I/O and contains nothing application-specific. It has
no tests of its own; coverage comes from the Layer 3 UI tests that drive
the full Browser through scripted key streams.

**Terminal device.** All UI I/O rides on a dedicated *terminal device*,
never on ``sys.stdin`` / ``sys.stdout``. ``term_init`` resolves it (see
:func:`term_init` for the policy) and sets three module globals:

* ``_tty_fd_in``  -- the fd read by ``os.read`` / ``select``; the target
  of the raw-mode ``termios`` calls.
* ``_tty_fd_out`` -- the fd the UI is written to.
* ``_tty_writer`` -- a buffered text writer over ``_tty_fd_out``
  (``.write(str)`` / ``.flush()`` / ``.buffer.write(bytes)``).

A terminal is full-duplex, so in single-device mode (``/dev/tty`` or an
explicit ``--tty TTY_PATH``) ``_tty_fd_in == _tty_fd_out`` -- one fd
opened ``O_RDWR``. The two names exist for the ``--tty -`` case, where
input is fd 0 and output is fd 1 (the std streams carry read and write on
separate fds). Decoupling the terminal from the std streams is what makes
the print-exit result cleanly capturable (``sel=$(browse-tui …)``): UI
bytes go to the device, ``stdout`` carries only the result.
"""

import errno
import os
import re
import select
import signal
import sys
import termios
import tty
import unicodedata


# ---------------------------------------------------------------------------
# Terminal layer: raw mode, VT100 output helpers, keystroke reader
# ---------------------------------------------------------------------------

_saved_termios = None
_orig_sigtstp_handler = None

# ---- terminal device (set by term_init; see module docstring) ------------
#
# Every UI read/write goes through these instead of sys.stdin/sys.stdout.
# Unset (term_init not yet run / after term_restore): fds are -1 and the
# writer is None.
_tty_fd_in = -1        # int fd: os.read / select / raw-mode termios target
_tty_fd_out = -1       # int fd: UI output destination
_tty_writer = None     # buffered text writer over _tty_fd_out (.write/.flush/.buffer)
# True when term_init opened the device fd itself (a path or /dev/tty) and
# therefore owns it -- term_restore closes it. False for --tty - (the fds
# are the std streams: not ours to close).
_tty_owns_fd = False

g_resize_flag = False
# Set when the alt-screen content has been blown away externally —
# e.g. resume from SIGTSTP+SIGCONT (the kernel/shell re-enters the alt
# screen with a blank canvas). The main loop observes this flag and
# clears ``Browser._pane_cache`` so the next ``render_full`` actually
# emits content (cache-hit short-circuits in ``end_row`` would
# otherwise leave the screen blank because the cache still holds the
# pre-suspend bytes).
g_screen_lost_flag = False
_notify_r = -1    # read end of self-pipe for waking up read_key
_notify_w = -1    # write end

# ---- row-buffer shim state ------------------------------------------------
#
# The row-buffer shim lets renderers stay nearly unchanged while their
# per-row writes get diffed against a per-pane line cache. While a row
# capture is active (``_row_capture_active=True``), every ``write()``
# (including indirect calls via ``set_style`` / ``move`` / ``clear_line``
# / ``clear_columns``) is appended to ``_row_buf`` instead of flushed
# to stdout. ``end_row`` then compares the accumulated bytes against
# ``pane_cache.lines[rel_row]`` and emits only on a cache miss.
#
# Captures cannot nest: ``begin_row`` asserts the flag is False. Pair
# every ``begin_row`` with exactly one ``end_row``.

_row_capture_active = False
_row_buf = []          # list[str], appended to by write()
_row_meta = None       # dict | None: pane_cache, rel_row, abs_row, left, right, rightmost

# ---- output helpers -------------------------------------------------------

def write(s):
    """Write string to the terminal device without flushing.

    When a row capture is active (see :func:`begin_row`), appends to the
    capture buffer instead; the captured bytes are diffed against the
    pane's line cache by :func:`end_row`. Otherwise the bytes are batched
    in ``_tty_writer``'s buffer and emitted on the next :func:`flush`
    (one flush per frame -- see :func:`begin_sync`).
    """
    if _row_capture_active:
        _row_buf.append(s)
    else:
        _tty_writer.write(s)

def flush():
    """Flush the terminal device (one call per rendered frame)."""
    _tty_writer.flush()

def move(row, col):
    """Move cursor to 1-based (row, col) position."""
    write(f'\033[{row};{col}H')

def clear_line():
    """Erase the entire current line."""
    write('\033[2K')

def clear_columns(row, left, right):
    """Clear columns ``left`` through ``right - 1`` (inclusive/exclusive) on ``row``.

    Mirrors the Rect convention used by the render layer (``right`` is
    exclusive). Emits a cursor move and a single space-fill — that's
    cheaper than scoped ``\\033[K`` variants when the column range is
    small and avoids clobbering content in adjacent panes.

    No-op when the range is empty (``right <= left``).
    """
    width = right - left
    if width <= 0:
        return
    move(row, left)
    write(' ' * width)

def set_scroll_region(top, bottom):
    """Set the scrolling region to rows top..bottom (1-based, inclusive)."""
    write(f'\033[{top};{bottom}r')

def scroll_up():
    """Scroll the contents of the scroll region up by one line."""
    write('\033D')

def scroll_down():
    """Scroll the contents of the scroll region down by one line."""
    write('\033M')

def set_style(fg=None, bg=None, bold=False, reverse=False, underline=False):
    """Apply 256-color style. fg/bg are ints 0-255 or None."""
    parts = ['0']  # reset first
    if bold:
        parts.append('1')
    if underline:
        parts.append('4')
    if reverse:
        parts.append('7')
    if fg is not None:
        parts.append(f'38;5;{fg}')
    if bg is not None:
        parts.append(f'48;5;{bg}')
    write(f'\033[{";".join(parts)}m')

def reset_style():
    """Reset all text attributes."""
    write('\033[0m')

# ---- row-buffer shim: capture + diff against per-pane line cache ---------

# Match any ANSI CSI sequence: ESC '[' <intermediate bytes> <final letter>.
# Final byte is any ASCII letter (A-Z / a-z), which covers SGR ('m'), cursor
# moves ('H', 'A', etc.), erase ('J', 'K'), and other CSI commands. We use
# the broader CSI form (rather than SGR-only) so non-SGR sequences embedded
# in captured row content don't leak into width math.
_ANSI_CSI_RE = re.compile(r'\x1b\[[^a-zA-Z]*[a-zA-Z]')


class SgrState:
    """Accumulates SGR sequences seen in a stream; renders the active state.

    First iteration: concatenates all fed sequences verbatim, dropping
    on ``\\e[m`` / ``\\e[0m``. Future improvement (TODO): track separate
    fg / bg / attrs slots so :meth:`render` can emit a single minimal
    combined sequence (e.g. fg overwrite drops the previous fg code).
    """

    def __init__(self):
        # Concatenated SGR sequences seen since last reset.
        self._buf = ''

    def feed(self, sgr_seq):
        """Apply one ``\\e[...m`` sequence. ``\\e[m`` or ``\\e[0m`` clears.

        Reset detection: extract the parameter portion (between ``[`` and
        ``m``), split on ``;``, and treat as a reset iff every component
        is empty or ``'0'``. This handles ``\\e[m``, ``\\e[0m``,
        ``\\e[0;0m``, ``\\e[;m`` as resets while leaving ``\\e[10m`` (a
        font-selection code, NOT a reset) alone.
        """
        # Defensive: only handle SGR sequences (ending in 'm' with the
        # CSI prefix). Anything else is a no-op.
        if not (sgr_seq.startswith('\033[') and sgr_seq.endswith('m')):
            return
        params = sgr_seq[2:-1].split(';')
        if all(p == '' or p == '0' for p in params):
            self._buf = ''
            return
        self._buf += sgr_seq

    def render(self):
        """Return the active state as bytes (or ``''`` if empty)."""
        return self._buf

    def is_empty(self):
        return not self._buf

    def reset(self):
        self._buf = ''


def _char_width(ch):
    """Return display columns for one character: 2 for wide/fullwidth, else 1.

    Shared definition of "one char's column count" used by the visible-
    length helpers and the preview-line wrap walker. Mirrors the same
    East Asian Width classification :func:`_visible_len` applies, hoisted
    out as a single-char primitive so the wrap walker (050-render.py
    :func:`_wrap_preview_line`) can fall through to a per-char column
    fit on cuts that contain wide characters.
    """
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


def _visible_len(s):
    """Count visible cells in ``s``, ignoring ANSI CSI escape sequences.

    Mirrors the escape-skipping logic in :func:`_truncate_visible` (in
    050-render.py). Strips any CSI sequence (``\\033[...<final>``) before
    counting -- not just SGR -- so cursor moves, erase commands, etc.
    embedded in captured row output don't leak into width math.

    Returns display columns rather than code points: characters whose
    East Asian Width is Wide (``W``) or Fullwidth (``F``) -- e.g. CJK
    ideographs -- count as 2 cells. Other characters count as 1. This
    keeps the steady-state pad math in :func:`end_row` correct for
    rows containing non-ASCII content; otherwise wide chars would
    under-count and trailing ghost cells could remain on screen when
    content shrinks in a non-rightmost pane.
    """
    stripped = _ANSI_CSI_RE.sub('', s)
    visible = 0
    for ch in stripped:
        visible += _char_width(ch)
    return visible


def begin_row(pane_cache, rel_row, abs_row, left, right, *, rightmost):
    """Start capturing writes into the row buffer.

    Subsequent ``write()`` / ``set_style()`` / ``reset_style()`` /
    ``move()`` / ``clear_line()`` / ``clear_columns()`` calls accumulate
    into the module-level ``_row_buf`` instead of going to stdout. Pair
    with :func:`end_row`. Captures cannot nest.

    ``pane_cache`` is duck-typed: ``end_row`` reads ``pane_cache.rect``,
    ``pane_cache.prev_rect`` and reads/writes ``pane_cache.lines[rel_row]``.
    The state-layer ``PaneCache`` type defined in 040-state.py is the
    expected concrete shape; tests can pass a ``SimpleNamespace`` with
    the same attrs.
    """
    global _row_capture_active, _row_buf, _row_meta
    if _row_capture_active:
        raise RuntimeError('begin_row: row capture already active (no nesting)')
    _row_capture_active = True
    _row_buf = []
    _row_meta = {
        'pane_cache': pane_cache,
        'rel_row': rel_row,
        'abs_row': abs_row,
        'left': left,
        'right': right,
        'rightmost': rightmost,
    }


def end_row():
    """Finish a row capture; emit only on a cache miss.

    Cache HIT (same captured bytes as ``pane_cache.lines[rel_row]``,
    same rect, prev_rect != None): emit nothing.

    Cache MISS: emit ``\\e[<abs_row>;<left>H`` + buffered bytes +
    ``\\e[m`` + (pad-or-``\\e[K``), then update the cache. Padding rules:

      * ``prev_rect is None`` (first paint in this rect): no padding.
      * ``prev_rect != rect`` (rect just changed): pad to pane width
        (or ``\\e[K`` when ``rightmost``); cache stores
        ``visible_len = pane_width``.
      * Steady state, new visible_len < cached visible_len: pad to
        cached visible_len (or ``\\e[K`` when rightmost); cache stores
        the displayed visible_len.
      * Else: no padding; cache stores new visible_len.
    """
    global _row_capture_active, _row_buf, _row_meta
    if not _row_capture_active:
        raise RuntimeError('end_row: no active row capture')

    meta = _row_meta
    buf = ''.join(_row_buf)
    pane_cache = meta['pane_cache']
    rel_row = meta['rel_row']
    abs_row = meta['abs_row']
    left = meta['left']
    right = meta['right']
    rightmost = meta['rightmost']
    pane_width = right - left

    # Reset capture state BEFORE emitting so direct stdout writes below
    # actually go to stdout (they call write(), which checks the flag).
    _row_capture_active = False
    _row_buf = []
    _row_meta = None

    rect = pane_cache.rect
    prev_rect = pane_cache.prev_rect
    cached = pane_cache.lines[rel_row] if rel_row < len(pane_cache.lines) else None

    new_visible = _visible_len(buf)

    # Cache hit: same content + same rect + not first paint → emit nothing.
    if (cached is not None
            and prev_rect is not None
            and prev_rect == rect
            and cached[1] == buf):
        return

    # Cache miss path — emit.
    write('\033[{};{}H'.format(abs_row, left))
    write(buf)
    write('\033[m')

    if prev_rect is None:
        # First paint in this rect — no padding.
        stored_visible = new_visible
    elif prev_rect != rect:
        # Rect changed — pad to full pane width (or \e[K if rightmost).
        if rightmost:
            write('\033[K')
        else:
            pad = pane_width - new_visible
            if pad > 0:
                write(' ' * pad)
        stored_visible = pane_width
    else:
        # Steady state, same rect.
        cached_visible = cached[0] if cached is not None else 0
        if new_visible < cached_visible:
            if rightmost:
                write('\033[K')
                # \e[K clears to end-of-line; the displayed visible
                # length is effectively new_visible (rest is blank).
                stored_visible = new_visible
            else:
                pad = cached_visible - new_visible
                write(' ' * pad)
                stored_visible = cached_visible
        else:
            stored_visible = new_visible

    pane_cache.lines[rel_row] = (stored_visible, buf)


def begin_sync():
    """Begin a synchronized output region (DEC mode 2026)."""
    write('\033[?2026h')


def end_sync():
    """End a synchronized output region (DEC mode 2026)."""
    write('\033[?2026l')

# ---- terminal size --------------------------------------------------------

def term_size():
    """Return (cols, rows) tuple for the current terminal.

    Queries the resolved terminal device (``_tty_fd_in``) first -- the
    authoritative source now that the layer no longer rides on the std
    streams -- then falls back to ``/dev/tty``, then ``(80, 24)``. The
    ``/dev/tty`` fallback covers the window before ``term_init`` has set
    ``_tty_fd_in`` (e.g. an early size probe).
    """
    if _tty_fd_in >= 0:
        try:
            sz = os.get_terminal_size(_tty_fd_in)
            if sz.columns > 0 and sz.lines > 0:
                return (sz.columns, sz.lines)
        except OSError:
            pass
    try:
        with open('/dev/tty') as _tty:
            sz = os.get_terminal_size(_tty.fileno())
            if sz.columns > 0 and sz.lines > 0:
                return (sz.columns, sz.lines)
    except OSError:
        pass
    return (80, 24)

# ---- signal handlers ------------------------------------------------------

def _handle_sigwinch(signum, frame):
    global g_resize_flag
    g_resize_flag = True
    notify_wake()

def _handle_sigtstp(signum, frame):
    """Restore terminal, then re-raise SIGTSTP with the default handler.

    The trailing ``signal.signal(SIGCONT, ...)`` after ``os.kill`` is
    deliberate, NOT redundant. Python's signal handler dispatch only
    runs Python-level handlers at certain bytecode checkpoints — bare
    ``RETURN_VALUE`` after the ``os.kill`` call does not always trigger
    the check, so on resume from SIGTSTP the queued ``_handle_sigcont``
    handler can be deferred until the main loop's next ``read_key``
    select wakes for some other reason. By the time it does run, the
    test fixture has often already captured a stale (bash-prompt)
    screen.
    A trailing function call after ``os.kill`` reliably yields a
    bytecode checkpoint, so SIGCONT runs synchronously on the resume
    path, ``_enter_raw`` re-paints the alt screen, and the next render
    pass shows the TUI state.
    """
    _leave_raw()
    # Temporarily set default handler so re-raise actually stops the process
    signal.signal(signal.SIGTSTP, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTSTP)
    # Trailing CALL bytecode — see docstring above. Re-asserting the
    # SIGCONT handler is also harmless and self-documents the intent
    # ("on resume, this is the handler we want").
    signal.signal(signal.SIGCONT, _handle_sigcont)

def _handle_sigcont(signum, frame):
    """Re-enter raw mode after being resumed from a SIGTSTP stop.

    Note: ``_enter_raw`` performs buffered stdio (``_tty_writer.buffer.write``
    + ``flush``), which is safe here BECAUSE Python dispatches signal
    handlers only at bytecode checkpoints between interpreter ops — not
    inside arbitrary C calls — so we cannot re-enter stdio mid-write. See
    ``_handle_sigtstp``'s docstring for the fuller discussion of Python's
    bytecode-checkpoint signal dispatch model. A future port to a stricter
    signal-safety regime (ctypes trampolines, asyncio signal integration,
    etc.) would need to revisit this.
    """
    global g_resize_flag, g_screen_lost_flag
    _enter_raw()
    # Re-register SIGTSTP handler (it was set to SIG_DFL before stop)
    signal.signal(signal.SIGTSTP, _handle_sigtstp)
    # Force a full redraw. ``g_screen_lost_flag`` tells the main loop
    # to also drop the per-pane row cache: the alt-screen content was
    # destroyed while we were stopped, so cache-hit short-circuits in
    # ``end_row`` would otherwise emit nothing and leave the screen
    # blank.
    g_resize_flag = True
    g_screen_lost_flag = True

# ---- raw mode / alternate screen -----------------------------------------

def _enter_raw():
    """Enter raw mode and switch to the alternate screen.

    Raw-mode ``termios`` is applied to ``_tty_fd_in``; the alt-screen /
    cursor-hide / mouse-enable bytes go out the device writer's byte
    buffer. Setting raw on the readable side affects the underlying
    terminal device, which is shared with the writable side in every
    supported configuration (single-device, and ``--tty -`` where both
    fds are the std streams of one pty).
    """
    global _saved_termios
    if _saved_termios is None:
        _saved_termios = termios.tcgetattr(_tty_fd_in)
    tty.setraw(_tty_fd_in)
    # Alternate screen buffer, hide cursor, enable SGR mouse tracking
    _tty_writer.buffer.write(b'\033[?1049h\033[?25l\033[?1000h\033[?1006h')
    _tty_writer.buffer.flush()

def notify_wake():
    """Wake up read_key() from another thread (e.g. after async preview load)."""
    if _notify_w >= 0:
        try:
            os.write(_notify_w, b'\x00')
        except OSError:
            pass

def _resolve_terminal(tty_path):
    """Resolve the terminal device and set the device globals.

    Implements the strict resolution order (no std-fd auto-probe -- the
    terminal is a deliberate choice, which is what keeps result capture a
    contract rather than an accident of how the caller wired their fds):

    1. ``tty_path == '-'`` -> use the std streams: ``_tty_fd_in = 0``,
       ``_tty_fd_out = 1``, ``_tty_writer = sys.stdout`` (reuse the
       existing stream; no second wrapper). Not owned -- the std fds are
       never closed by us.
    2. ``tty_path`` is a device path -> ``os.open(path, O_RDWR)``.
    3. ``tty_path is None`` -> ``os.open('/dev/tty', O_RDWR)``.

    Cases 2-3 open a single full-duplex fd (``_tty_fd_in == _tty_fd_out``),
    mark it ``O_CLOEXEC`` (so it does not leak into children -- shell-out
    passes it explicitly), build a UTF-8 text writer over it, and take
    ownership (``term_restore`` closes it). An open failure raises a clean
    ``SystemExit`` (no traceback), never falling back to the std fds.
    """
    global _tty_fd_in, _tty_fd_out, _tty_writer, _tty_owns_fd

    if tty_path == '-':
        _tty_fd_in = 0
        _tty_fd_out = 1
        _tty_writer = sys.stdout
        _tty_owns_fd = False
        return

    device = tty_path if tty_path is not None else '/dev/tty'
    try:
        fd = os.open(device, os.O_RDWR | os.O_CLOEXEC)
    except OSError:
        if tty_path is None:
            raise SystemExit(
                'browse-tui: no controlling terminal; '
                'pass --tty - to run over stdin/stdout')
        raise SystemExit(f'browse-tui: cannot open terminal {device!r}')
    _tty_fd_in = fd
    _tty_fd_out = fd
    # TextIOWrapper with newline='' (no translation) over the device, with
    # .buffer exposing the BufferedWriter for the byte-level alt-screen /
    # mouse sequences -- mirroring the old sys.stdout / sys.stdout.buffer
    # split exactly.
    _tty_writer = os.fdopen(fd, 'w', encoding='utf-8', newline='')
    _tty_owns_fd = True


def term_init(tty_path=None):
    """Resolve the terminal device, save termios, enter raw mode + alt screen.

    ``tty_path`` selects the device (see :func:`_resolve_terminal` for the
    full policy): ``None`` (default) opens ``/dev/tty``; an explicit path
    opens that device; the sentinel ``'-'`` uses the process's std streams
    (fd 0 in, fd 1 out). All subsequent UI I/O rides on the resolved
    device, never on ``sys.stdin`` / ``sys.stdout``.

    Also registers signal handlers for SIGWINCH, SIGTSTP, and SIGCONT.

    Raises a clean ``SystemExit`` (no traceback) when no terminal is
    available -- either the device cannot be opened, or it is not a tty
    (e.g. ``--tty -`` with piped std streams, where the raw-mode
    ``termios`` call fails).
    """
    global _orig_sigtstp_handler, _notify_r, _notify_w
    _resolve_terminal(tty_path)
    _orig_sigtstp_handler = signal.getsignal(signal.SIGTSTP)
    # Create self-pipe for async notification
    _notify_r, _notify_w = os.pipe()
    os.set_blocking(_notify_r, False)
    os.set_blocking(_notify_w, False)
    try:
        _enter_raw()
    except termios.error:
        # The resolved device is not a tty (the --tty - piped case). Tear
        # down the half-built state and surface a clean error rather than
        # a termios traceback.
        term_restore()
        raise SystemExit('browse-tui: not a terminal')
    signal.signal(signal.SIGWINCH, _handle_sigwinch)
    signal.signal(signal.SIGTSTP, _handle_sigtstp)
    signal.signal(signal.SIGCONT, _handle_sigcont)


def term_child_fds():
    """Return ``(in_fd, out_fd)`` for handing the terminal to a child.

    The shell-out path (``run_external`` / ``page``) passes these to
    ``subprocess`` as the child's stdin/stdout/stderr so an interactive
    editor/pager talks to the same terminal -- without the parent ever
    touching its own fd 0/1. In single-device mode both values are the one
    ``O_RDWR`` device fd; in ``--tty -`` mode they are ``(0, 1)``.
    """
    return (_tty_fd_in, _tty_fd_out)

def _leave_raw():
    """Restore termios and leave alternate screen, but keep the notification pipe.

    No-op once the terminal is already torn down (``_tty_writer is None``)
    so ``term_restore`` stays idempotent: a second ``term_restore`` (or any
    ``_leave_raw`` after teardown) cleanly returns instead of dereferencing
    the nulled writer / writing to the closed device fd. ``_tty_writer`` and
    ``_tty_fd_in`` are nulled together in ``term_restore``, so this one guard
    covers both the byte-write and the ``termios`` call below.
    """
    if _tty_writer is None:
        return
    # Disable mouse tracking, show cursor, leave alternate screen
    _tty_writer.buffer.write(b'\033[?1006l\033[?1000l\033[?25h\033[?1049l')
    _tty_writer.buffer.flush()
    if _saved_termios is not None:
        termios.tcsetattr(_tty_fd_in, termios.TCSAFLUSH, _saved_termios)

def term_restore():
    """Full cleanup: restore termios, leave alternate screen, close fds.

    Closes the notification pipe and -- when we opened the terminal device
    ourselves (a path or ``/dev/tty``) -- the device fd too, flushing it
    via the owning writer. The std streams (``--tty -`` mode) are never
    closed. Resets the device globals so a later ``term_init`` starts
    clean.
    """
    global _notify_r, _notify_w
    global _tty_fd_in, _tty_fd_out, _tty_writer, _tty_owns_fd
    _leave_raw()
    for fd in (_notify_r, _notify_w):
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    _notify_r = _notify_w = -1
    if _tty_owns_fd and _tty_writer is not None:
        # Closing the owning TextIOWrapper flushes it and closes the
        # underlying O_RDWR device fd exactly once.
        try:
            _tty_writer.close()
        except OSError:
            pass
    _tty_fd_in = _tty_fd_out = -1
    _tty_writer = None
    _tty_owns_fd = False

# ---- suspend / resume for shelling out -----------------------------------

def term_suspend():
    """Restore terminal for an external command (editor, pager, etc.)."""
    _leave_raw()

def term_resume():
    """Re-enter raw mode and alternate screen after an external command."""
    global g_resize_flag, g_screen_lost_flag
    _enter_raw()
    g_resize_flag = True
    # Shelling out to an editor/pager scrolled the user's content into
    # the primary screen and left the alt screen blank on re-entry —
    # same as resume from SIGTSTP. Drop the row cache so the next
    # ``render_full`` actually re-emits every pane.
    g_screen_lost_flag = True

# ---- keystroke reader -----------------------------------------------------

def input_ready():
    """Return True iff more keyboard input is buffered on the device right now.

    Non-blocking poll (``select`` with a zero timeout). Used by the main
    loop to coalesce a burst of keystrokes (e.g. a held-down arrow or a
    pasted command) into a single render — dispatch the queued keys
    back-to-back, paint once at the end.

    Watches the terminal device only — the notification pipe is
    intentionally NOT polled here so async worker deliveries break the
    coalescing loop and the next outer iteration sees their state changes.
    """
    fd = _tty_fd_in
    while True:
        try:
            r, _, _ = select.select([fd], [], [], 0)
            return bool(r)
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            raise


def read_key():
    """Read one keystroke and return a string name for it.

    Handles multi-byte escape sequences, alt-combos, and bare ESC
    (disambiguated via a 50 ms timeout after the initial ESC byte).
    Retries on EINTR (e.g. from SIGWINCH).
    Also wakes up on the notification pipe and returns '_notify'.
    """
    fd = _tty_fd_in

    # Wait for the terminal device or the notification pipe
    watch_fds = [fd]
    if _notify_r >= 0:
        watch_fds.append(_notify_r)
    while True:
        try:
            ready, _, _ = select.select(watch_fds, [], [])
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            raise
        if _notify_r >= 0 and _notify_r in ready:
            # Drain the notification pipe
            try:
                os.read(_notify_r, 1024)
            except OSError:
                pass
            # Always deliver _notify first; the device stays buffered for next call
            return '_notify'
        break  # the terminal device is ready

    def _read1():
        """Read a single byte from the terminal device, retrying on EINTR."""
        while True:
            try:
                b = os.read(fd, 1)
                if not b:
                    return ''
                return b.decode('utf-8', errors='replace')
            except OSError as e:
                if e.errno == errno.EINTR:
                    continue
                raise

    def _peek(timeout=0.05):
        """Return True if more input is available within *timeout* seconds."""
        while True:
            try:
                r, _, _ = select.select([fd], [], [], timeout)
                return bool(r)
            except OSError as e:
                if e.errno == errno.EINTR:
                    continue
                raise

    ch = _read1()
    if ch == '':
        return 'esc'  # EOF treated as esc

    o = ord(ch)

    # ---- Ctrl combos (0x01-0x1a excluding special cases) ------------------
    if ch == '\r' or ch == '\n':
        return 'enter'
    if ch == '\t':
        return 'tab'
    if ch == '\x7f' or ch == '\x08':
        return 'backspace'
    if ch == ' ':
        return 'space'

    if ch == '\x1b':
        # ESC received — could be bare Esc, Alt-combo, or escape sequence
        if not _peek():
            return 'esc'

        ch2 = _read1()
        if ch2 == '':
            return 'esc'

        # ESC ESC ... — Alt prefix before another escape sequence
        # Some terminals send Alt+Up as ESC ESC [ A instead of ESC [ 1;3 A
        if ch2 == '\x1b':
            if not _peek():
                return 'esc'  # double-Esc with no follow-up — keep as Esc
            ch3 = _read1()
            if ch3 == '[':
                inner = _read_csi(fd, _read1, _peek)
                if inner == '_unknown':
                    return '_unknown'
                # Prepend alt- if not already modified
                if inner.startswith(('shift-', 'ctrl-', 'alt-', 'ctrl-shift-')):
                    return inner  # already has a modifier
                return 'alt-' + inner
            if ch3 == 'O':
                ch4 = _read1()
                ss3_map = {'P': 'f1', 'Q': 'f2', 'R': 'f3', 'S': 'f4'}
                inner = ss3_map.get(ch4)
                if inner is not None:
                    return 'alt-' + inner
                return '_unknown'
            return '_unknown'

        # CSI sequence: ESC [
        if ch2 == '[':
            return _read_csi(fd, _read1, _peek)

        # SS3 sequence: ESC O  (commonly used for F1-F4)
        if ch2 == 'O':
            ch3 = _read1()
            if ch3 == 'P':
                return 'f1'
            if ch3 == 'Q':
                return 'f2'
            if ch3 == 'R':
                return 'f3'
            if ch3 == 'S':
                return 'f4'
            # Unknown SS3 — silently ignore (don't fall through to
            # 'esc' which would tear down the app via _quit).
            return '_unknown'

        # Alt-combo: ESC + printable char
        if ' ' <= ch2 <= '~':
            return 'alt-' + ch2

        # Alt + Enter (ESC + CR / LF) is the conventional shape; named
        # without the explicit ``ctrl-`` prefix because every terminal
        # sends it via this path.
        if ch2 == '\r' or ch2 == '\n':
            return 'alt-enter'

        # Alt + Ctrl-X (e.g. Alt-Ctrl-P = ESC + 0x10). Without this
        # branch, the bare ``return 'esc'`` below would fire and
        # ``'esc'`` is bound to _quit — pressing Alt-Ctrl-anything in
        # certain terminals would tear the app down. Return the
        # ``alt-ctrl-x`` form so action bindings can match it (or
        # silently ignore if no binding exists).
        ch2_o = ord(ch2)
        if 1 <= ch2_o <= 26:
            return 'alt-ctrl-' + chr(ch2_o + 96)

        # Truly unknown ESC + byte: return a sentinel that the dispatch
        # layer ignores, NOT 'esc'. 'esc' is bound to quit, so hitting
        # an unrecognised escape sequence used to teardown the app.
        return '_unknown'

    # ---- Ctrl-A through Ctrl-Z (except those handled above) ---------------
    if 1 <= o <= 26:
        return 'ctrl-' + chr(o + 96)  # 0x01 -> 'ctrl-a', etc.

    # ---- Regular printable character --------------------------------------
    return ch


def _read_csi(fd, _read1, _peek):
    """Parse a CSI (ESC [) escape sequence and return a key name."""
    buf = ''
    while True:
        ch = _read1()
        if ch == '':
            break
        # CSI parameters and intermediates are in 0x20-0x3F range
        # Final byte is in 0x40-0x7E range
        buf += ch
        if '@' <= ch <= '~':
            break

    # Arrow keys
    if buf == 'A':
        return 'up'
    if buf == 'B':
        return 'down'
    if buf == 'C':
        return 'right'
    if buf == 'D':
        return 'left'

    # Bare Home / End (some terminals send these without a tilde).
    if buf == 'H':
        return 'home'
    if buf == 'F':
        return 'end'

    # Shift-Tab: ESC [ Z
    if buf == 'Z':
        return 'btab'

    # Home / End (rxvt, xterm without application mode)
    if buf == 'H':
        return 'home'
    if buf == 'F':
        return 'end'

    # Tilde sequences: ESC [ <number> ~ or ESC [ <number> ; <mod> ~
    if buf.endswith('~'):
        raw = buf[:-1]
        mod_prefix = ''
        if ';' in raw:
            parts = raw.split(';')
            num = parts[0]
            try:
                mod = int(parts[1])
            except ValueError:
                mod = 0
            if mod == 2:
                mod_prefix = 'shift-'
            elif mod == 3:
                mod_prefix = 'alt-'
            elif mod == 5:
                mod_prefix = 'ctrl-'
        else:
            num = raw
        if num == '1' or num == '7':
            return mod_prefix + 'home'
        if num == '4' or num == '8':
            return mod_prefix + 'end'
        if num == '5':
            return mod_prefix + 'pgup'
        if num == '6':
            return mod_prefix + 'pgdn'
        if num == '2':
            return 'insert'
        if num == '3':
            return 'delete'
        if num == '11':
            return 'f1'
        if num == '12':
            return 'f2'
        if num == '13':
            return 'f3'
        if num == '14':
            return 'f4'
        if num == '15':
            return 'f5'
        if num == '17':
            return 'f6'
        if num == '18':
            return 'f7'
        if num == '19':
            return 'f8'
        if num == '20':
            return 'f9'
        if num == '21':
            return 'f10'
        if num == '23':
            return 'f11'
        if num == '24':
            return 'f12'

    # Shift/Ctrl/Alt modified arrows + Home/End: ESC [ 1 ; <mod> <A-D|H|F>
    # Same encoding for arrows and Home/End — modifier in the second
    # parameter, terminal letter selects the key.
    if len(buf) >= 3 and buf[-1] in 'ABCDHF' and ';' in buf:
        key_name = {
            'A': 'up', 'B': 'down', 'C': 'right', 'D': 'left',
            'H': 'home', 'F': 'end',
        }[buf[-1]]
        # Extract modifier: 2=shift, 3=alt, 5=ctrl, etc.
        parts = buf[:-1].split(';')
        if len(parts) == 2:
            try:
                mod = int(parts[1])
            except ValueError:
                return key_name
            if mod == 2:
                return 'shift-' + key_name
            if mod == 3:
                return 'alt-' + key_name
            if mod == 5:
                return 'ctrl-' + key_name
            if mod == 6:
                return 'ctrl-shift-' + key_name
            if mod == 7:
                return 'alt-ctrl-' + key_name
        return key_name

    # SGR mouse: ESC [ < Cb ; Cx ; Cy M/m
    if buf.startswith('<') and buf[-1] in ('M', 'm'):
        parts = buf[1:-1].split(';')
        if len(parts) == 3:
            try:
                cb, cx, cy = int(parts[0]), int(parts[1]), int(parts[2])
            except ValueError:
                return '_mouse'
            if cb == 64:
                return 'scroll-up:{}:{}'.format(cy, cx)
            if cb == 65:
                return 'scroll-down:{}:{}'.format(cy, cx)
            if cb == 0 and buf[-1] == 'M':  # left press only
                return 'mouse-click:{}:{}'.format(cy, cx)
            return '_mouse'  # ignore release, right-click, etc.
        return '_mouse'

    # CSI u encoding (kitty keyboard protocol): ESC [ <keycode> ; <mod> u
    if buf.endswith('u') and ';' in buf:
        parts = buf[:-1].split(';')
        if len(parts) == 2:
            try:
                keycode = int(parts[0])
                mod = int(parts[1])
            except ValueError:
                pass
            else:
                if keycode == 13:  # Enter
                    if mod == 2:
                        return 'shift-enter'
                    if mod == 3:
                        return 'alt-enter'

    # Fallback: unknown CSI sequence. Return a sentinel the action
    # dispatcher ignores rather than 'esc', which is bound to _quit —
    # an unrecognised modifier-key combination must not tear the app
    # down.
    return '_unknown'
