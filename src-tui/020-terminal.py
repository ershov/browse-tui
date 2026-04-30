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
"""

import errno
import os
import select
import signal
import sys
import termios
import tty


# ---------------------------------------------------------------------------
# Terminal layer: raw mode, VT100 output helpers, keystroke reader
# ---------------------------------------------------------------------------

_saved_termios = None
_orig_sigtstp_handler = None
g_resize_flag = False
_notify_r = -1    # read end of self-pipe for waking up read_key
_notify_w = -1    # write end

# ---- output helpers -------------------------------------------------------

def write(s):
    """Write string to stdout without flushing."""
    sys.stdout.write(s)

def flush():
    """Flush stdout."""
    sys.stdout.flush()

def move(row, col):
    """Move cursor to 1-based (row, col) position."""
    write(f'\033[{row};{col}H')

def clear_line():
    """Erase the entire current line."""
    write('\033[2K')

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

# ---- terminal size --------------------------------------------------------

def term_size():
    """Return (cols, rows) tuple for the current terminal."""
    sz = os.get_terminal_size()
    return (sz.columns, sz.lines)

# ---- signal handlers ------------------------------------------------------

def _handle_sigwinch(signum, frame):
    global g_resize_flag
    g_resize_flag = True
    notify_wake()

def _handle_sigtstp(signum, frame):
    """Restore terminal, then re-raise SIGTSTP with the default handler."""
    _leave_raw()
    # Temporarily set default handler so re-raise actually stops the process
    signal.signal(signal.SIGTSTP, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTSTP)

def _handle_sigcont(signum, frame):
    """Re-enter raw mode after being resumed from a SIGTSTP stop."""
    global g_resize_flag
    _enter_raw()
    # Re-register SIGTSTP handler (it was set to SIG_DFL before stop)
    signal.signal(signal.SIGTSTP, _handle_sigtstp)
    # Force a full redraw
    g_resize_flag = True

# ---- raw mode / alternate screen -----------------------------------------

def _enter_raw():
    """Enter raw mode and switch to the alternate screen."""
    global _saved_termios
    if _saved_termios is None:
        _saved_termios = termios.tcgetattr(sys.stdin.fileno())
    tty.setraw(sys.stdin.fileno())
    # Alternate screen buffer, hide cursor, enable SGR mouse tracking
    sys.stdout.buffer.write(b'\033[?1049h\033[?25l\033[?1000h\033[?1006h')
    sys.stdout.buffer.flush()

def notify_wake():
    """Wake up read_key() from another thread (e.g. after async preview load)."""
    if _notify_w >= 0:
        try:
            os.write(_notify_w, b'\x00')
        except OSError:
            pass

def term_init():
    """Save termios, enter raw mode, alternate screen, hide cursor.
    Also registers signal handlers for SIGWINCH, SIGTSTP, and SIGCONT.
    """
    global _orig_sigtstp_handler, _notify_r, _notify_w
    _orig_sigtstp_handler = signal.getsignal(signal.SIGTSTP)
    # Create self-pipe for async notification
    _notify_r, _notify_w = os.pipe()
    os.set_blocking(_notify_r, False)
    os.set_blocking(_notify_w, False)
    _enter_raw()
    signal.signal(signal.SIGWINCH, _handle_sigwinch)
    signal.signal(signal.SIGTSTP, _handle_sigtstp)
    signal.signal(signal.SIGCONT, _handle_sigcont)

def _leave_raw():
    """Restore termios and leave alternate screen, but keep the notification pipe."""
    # Disable mouse tracking, show cursor, leave alternate screen
    sys.stdout.buffer.write(b'\033[?1006l\033[?1000l\033[?25h\033[?1049l')
    sys.stdout.buffer.flush()
    if _saved_termios is not None:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, _saved_termios)

def term_restore():
    """Full cleanup: restore termios, leave alternate screen, close notification pipe."""
    global _notify_r, _notify_w
    _leave_raw()
    for fd in (_notify_r, _notify_w):
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    _notify_r = _notify_w = -1

# ---- suspend / resume for shelling out -----------------------------------

def term_suspend():
    """Restore terminal for an external command (editor, pager, etc.)."""
    _leave_raw()

def term_resume():
    """Re-enter raw mode and alternate screen after an external command."""
    global g_resize_flag
    _enter_raw()
    g_resize_flag = True

# ---- keystroke reader -----------------------------------------------------

def read_key():
    """Read one keystroke and return a string name for it.

    Handles multi-byte escape sequences, alt-combos, and bare ESC
    (disambiguated via a 50 ms timeout after the initial ESC byte).
    Retries on EINTR (e.g. from SIGWINCH).
    Also wakes up on the notification pipe and returns '_notify'.
    """
    fd = sys.stdin.fileno()

    # Wait for stdin or notification pipe
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
            # Always deliver _notify first; stdin stays buffered for next call
            return '_notify'
        break  # stdin is ready

    def _read1():
        """Read a single byte from stdin, retrying on EINTR."""
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
                return 'esc'
            ch3 = _read1()
            if ch3 == '[':
                inner = _read_csi(fd, _read1, _peek)
                # Prepend alt- if not already modified
                if inner.startswith(('shift-', 'ctrl-', 'alt-', 'ctrl-shift-')):
                    return inner  # already has a modifier
                return 'alt-' + inner
            if ch3 == 'O':
                ch4 = _read1()
                ss3_map = {'P': 'f1', 'Q': 'f2', 'R': 'f3', 'S': 'f4'}
                inner = ss3_map.get(ch4, 'esc')
                if inner != 'esc':
                    return 'alt-' + inner
                return 'esc'
            return 'esc'

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
            # Unknown SS3 — return what we can
            return 'esc'

        # Alt-combo: ESC + printable char
        if ' ' <= ch2 <= '~':
            return 'alt-' + ch2

        # Alt + control char (e.g. Alt-Enter = ESC + CR)
        if ch2 == '\r' or ch2 == '\n':
            return 'alt-enter'

        return 'esc'

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

    # Shift/Ctrl modified arrows: ESC [ 1 ; <mod> <A-D>
    if len(buf) >= 3 and buf[-1] in 'ABCD' and ';' in buf:
        direction = {'A': 'up', 'B': 'down', 'C': 'right', 'D': 'left'}[buf[-1]]
        # Extract modifier: 2=shift, 3=alt, 5=ctrl, etc.
        parts = buf[:-1].split(';')
        if len(parts) == 2:
            try:
                mod = int(parts[1])
            except ValueError:
                return direction
            if mod == 2:
                return 'shift-' + direction
            if mod == 3:
                return 'alt-' + direction
            if mod == 5:
                return 'ctrl-' + direction
            if mod == 6:
                return 'ctrl-shift-' + direction
        return direction

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

    # Fallback: unknown CSI sequence
    return 'esc'
