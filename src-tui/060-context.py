"""browse-tui: Context — main-thread-only wrapper around Browser.

Action handlers receive a ``Context`` rather than the bare Browser. The
split is deliberate:

* **Browser** is the thread-safe surface — every public op is callable
  from any thread (``post()`` shuttles work onto the main thread).
* **Context** wraps Browser and adds main-thread-only sub-flows like
  ``input``, ``confirm``, ``run_external``, and ``page``. These read
  keys synchronously or suspend the terminal to launch external
  processes; they are *not* safe to call from a worker thread.

Phase 1 affordances exposed on Context: ``cursor``, ``selected``,
``targets``, plus pass-through versions of ``refresh / cursor_to /
expand / select / message / error / quit`` and the main-thread sub-flows
listed above. ``pick`` and ``insert`` are deferred to phase 2 (#20, #21).
"""

import os
import shutil
import subprocess


class Context:
    """The handle that action handlers receive.

    Construction is internal — the main loop creates one Context
    per dispatched action. Handlers never see the Browser directly,
    which keeps the surface small and steers recipes towards the
    main-thread-aware affordances.
    """

    def __init__(self, browser):
        self._browser = browser

    # ---- selection helpers --------------------------------------------

    @property
    def cursor(self):
        """Return the Item under the cursor, or None.

        ``None`` when the visible list is empty *or* when the row under
        the cursor is a non-normal entry (the ``loading…`` placeholder
        or the synthetic scope-root row). Recipes that operate on the
        cursor should branch on ``None`` to skip those cases.
        """
        state = self._browser._state
        vis = visible_items(state)
        if 0 <= state.cursor < len(vis):
            entry = vis[state.cursor]
            if entry.kind == 'normal':
                return entry.item
        return None

    @property
    def selected(self):
        """Return the list of Items currently in ``state.selected``.

        Walks the visible list first (cheapest) then the cached
        ``_children`` map to find Items by id. Items appear at most
        once in the result. Returns ``[]`` when nothing is selected.
        """
        state = self._browser._state
        if not state.selected:
            return []
        result = []
        seen_ids = set()
        for entry in visible_items(state):
            if entry.kind == 'normal' and entry.item.id in state.selected:
                if entry.item.id not in seen_ids:
                    result.append(entry.item)
                    seen_ids.add(entry.item.id)
        for items in state._children.values():
            for it in items:
                if it.id in state.selected and it.id not in seen_ids:
                    result.append(it)
                    seen_ids.add(it.id)
        return result

    @property
    def targets(self):
        """``selected`` if non-empty, else ``[cursor]`` if any, else ``[]``.

        Most actions operate on this — ``ctx.selected or [ctx.cursor]``
        with the empty-fallback handled. The ``targets`` shape lets
        recipes write ``for it in ctx.targets`` without separate
        single/multi branches.
        """
        sel = self.selected
        if sel:
            return sel
        c = self.cursor
        return [c] if c else []

    # ---- thread-safe pass-through -------------------------------------
    #
    # These delegate straight to Browser. We keep the wrappers because
    # (a) the surface is the documented one, and (b) changing Browser
    # later (e.g. routing through a different queue) only needs editing
    # one place.

    def refresh(self, id=None, on_complete=None):
        """Refetch one parent's children, or the full root if ``id`` is None."""
        return self._browser.refresh(id, on_complete)

    def cursor_to(self, id, on_complete=None):
        """Move the cursor onto the item with ``id``."""
        return self._browser.cursor_to(id, on_complete)

    def expand(self, id, on_complete=None):
        """Expand and fetch the children of ``id``."""
        return self._browser.expand(id, on_complete)

    def select(self, ids, replace=False):
        """Add ``ids`` to the selection set (or replace it)."""
        return self._browser.select(ids, replace)

    def message(self, text):
        """Surface ``text`` as a transient status message."""
        self._browser.message(text)

    def error(self, text):
        """Surface ``text`` as an error message."""
        self._browser.error(text)

    def quit(self, code=0, output=''):
        """Request the main loop to exit with ``code`` and stdout ``output``."""
        self._browser.quit(code, output)

    # ---- main-thread sub-flows ----------------------------------------

    def run_external(self, cmd, env=None) -> int:
        """Suspend the terminal, run ``cmd``, then resume.

        ``cmd`` is either a list of argv strings or a shell string (the
        latter triggers ``shell=True``). ``env`` is merged with the
        parent environment — pass ``None`` to inherit unchanged.

        Returns the subprocess exit code, or ``-1`` if launching the
        process raised. Errors are also surfaced via ``ctx.error``.

        Headless Browsers skip the suspend/resume calls (term layer is
        not initialised) so unit tests can exercise the run path
        without a real TTY. The ``_needs_redraw`` flag is still set so
        the next render pass repaints over whatever the external
        process drew.
        """
        if not self._browser._headless:
            term_suspend()
        try:
            full_env = None if env is None else {**os.environ, **env}
            shell = isinstance(cmd, str)
            result = subprocess.run(cmd, shell=shell, env=full_env)
            return result.returncode
        except Exception as e:
            self.error(f'run_external: {type(e).__name__}: {e}')
            return -1
        finally:
            if not self._browser._headless:
                term_resume()
            self._browser._needs_redraw.add('all')

    def page(self, text, lang=''):
        """Pipe ``text`` into bat/batcat/less, suspending the terminal first.

        Detects bat or batcat in PATH; falls back to ``$PAGER`` (or
        ``less -R``) otherwise. ``lang`` is forwarded to bat as
        ``--language=<lang>`` for syntax highlighting; ignored by less.

        Headless Browsers skip the suspend/resume calls, so this is
        callable from tests but the pager will inherit the test
        runner's stdin (which is usually fine — the pager will just
        exit immediately on EOF).
        """
        pager = None
        for cand in ('bat', 'batcat'):
            p = shutil.which(cand)
            if p:
                pager = [p, '--style=plain', '--paging=always']
                if lang:
                    pager.extend(['--language', lang])
                break
        if pager is None:
            pager = [os.environ.get('PAGER') or 'less', '-R']

        if not self._browser._headless:
            term_suspend()
        try:
            proc = subprocess.Popen(pager, stdin=subprocess.PIPE)
            try:
                proc.stdin.write(text.encode('utf-8', errors='replace'))
                proc.stdin.close()
            except BrokenPipeError:
                pass
            proc.wait()
        except Exception as e:
            self.error(f'page: {type(e).__name__}: {e}')
        finally:
            if not self._browser._headless:
                term_resume()
            self._browser._needs_redraw.add('all')

    def input(self, prompt, default=''):
        """Read a single-line string from the user on the info bar.

        Returns the text typed (empty string if the user just hit
        Enter), or ``None`` if the user cancelled with esc/ctrl-c.

        Headless Browsers return ``default`` immediately so unit tests
        can drive deterministic flows; the real TTY path defers to
        ``_read_line_on_info_bar`` and is exercised by UI tests in
        ticket #14.
        """
        if self._browser._headless:
            return default
        return _read_line_on_info_bar(self._browser, prompt, default)

    def confirm(self, prompt):
        """Show ``prompt`` and read y/n on the info bar.

        Returns ``True`` for ``y``/``Y``, ``False`` for ``n``/``N`` or
        cancel. Headless Browsers return ``False`` so unit tests can
        rely on the safe-default outcome.
        """
        if self._browser._headless:
            return False
        return _confirm_on_info_bar(self._browser, prompt)


# ---- info-bar prompt helpers ----------------------------------------------
#
# The implementations below mirror plan-tui's ``_read_string`` and
# ``_status_bar_message`` patterns (see plan-source/src-tui/060-actions.py).
# Phase 1 here implements the minimum necessary for production runs; full
# polish (cursor visibility, scroll-back, history) is out of scope. UI
# tests in ticket #14 exercise these by driving a real terminal.


def _info_bar_geometry(browser):
    """Return ``(row, cols)`` for the info bar, or ``(0, 0)`` if no room.

    The render layer owns ``layout_panes`` and the actual geometry; we
    re-derive it here so the prompt helpers don't have to thread layout
    state through every call. Returns ``(0, 0)`` when the terminal is
    too small to host the info bar.
    """
    cols, rows = term_size()
    layout = layout_panes(cols, rows, show_preview=browser.show_preview)
    return layout['info_row'], layout['cols']


def _draw_info_prompt(browser, prompt, buf):
    """Paint ``prompt + buf`` on the info bar.

    Mirrors plan-tui's _read_string drawing: prompt in bold yellow on
    blue, buf in normal text, fill rest of the row with separator
    characters in gray. Doesn't read input — callers loop with
    ``read_key`` and call this after each key.
    """
    row, cols = _info_bar_geometry(browser)
    if row <= 0 or cols <= 0:
        return
    move(row, 1)
    clear_line()
    set_style(fg=11, bg=4, bold=True)
    write(prompt[:cols])
    pos = len(prompt)
    if pos < cols:
        set_style()
        remaining = cols - pos
        write(buf[:remaining])
        pos += min(len(buf), remaining)
    if pos < cols:
        set_style(fg=8)
        write('─' * (cols - pos))
    set_style()
    flush()


def _read_line_on_info_bar(browser, prompt, default=''):
    """Drive a single-line text-entry prompt on the info bar.

    Returns the typed string on Enter, or None on esc/ctrl-c. ``default``
    pre-fills the input (so editors and rename flows can offer the
    current value).

    Resize events (``_notify`` after a SIGWINCH) cause a redraw of the
    info bar; the typed buffer is preserved across resizes.
    """
    buf = default
    while True:
        _draw_info_prompt(browser, prompt, buf)
        key = read_key()
        if key == '_notify':
            # SIGWINCH or other notification — repaint and continue.
            continue
        if key == 'enter':
            return buf
        if key in ('esc', 'ctrl-c'):
            return None
        if key == 'backspace':
            if buf:
                buf = buf[:-1]
            continue
        if key == 'space':
            buf += ' '
            continue
        if len(key) == 1 and key.isprintable():
            buf += key


def _confirm_on_info_bar(browser, prompt):
    """Drive a y/n prompt on the info bar.

    Returns True for y/Y, False for n/N or esc/ctrl-c. Other keys are
    ignored (the prompt re-paints and waits for a fresh key).
    """
    while True:
        _draw_info_prompt(browser, prompt + ' (y/n) ', '')
        key = read_key()
        if key == '_notify':
            continue
        if key in ('y', 'Y'):
            return True
        if key in ('n', 'N', 'esc', 'ctrl-c'):
            return False
