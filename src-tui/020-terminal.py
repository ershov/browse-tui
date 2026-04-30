"""browse-tui: terminal layer (raw mode, key reader, signals, mouse, self-pipe).

Phase 1 stub: only the cross-thread main-loop wakeup primitive lives here for
now. ``notify_wake`` is a no-op until the full terminal layer (raw mode,
key reader, signals, mouse, self-pipe init/teardown) lands in ticket #9 and
fills in ``_notify_w`` with the write end of a real self-pipe. Workers and
``Browser.post`` already call ``notify_wake`` -- those calls become no-ops in
headless mode and silently start writing real bytes once the pipe is wired.
"""

import os


# Write end of the self-pipe used by the production main loop's ``select``.
# Negative means "not initialised yet" -- ``notify_wake`` short-circuits.
# Ticket #9's ``term_init`` will assign this; ``term_restore`` resets it.
_notify_w = -1


def notify_wake():
    """Wake the main loop's ``select``. No-op until the self-pipe is wired.

    Safe to call from any thread (workers, watchers, post handlers). The
    write is non-blocking; if the pipe buffer is somehow full we drop the
    byte -- the main loop only needs *one* readable byte to wake up, and
    the buffer being non-empty already implies that condition.
    """
    if _notify_w >= 0:
        try:
            os.write(_notify_w, b'\x00')
        except OSError:
            # Closed, full (non-blocking), or otherwise wedged. The main loop
            # is either already woken (buffer non-empty) or in the middle of
            # teardown (fd closed); either way, dropping is correct.
            pass
