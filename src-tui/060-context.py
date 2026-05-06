"""browse-tui: Context — main-thread-only wrapper around Browser.

Action handlers receive a ``Context`` rather than the bare Browser. The
split is deliberate:

* **Browser** is the thread-safe surface — every public op is callable
  from any thread (``post()`` shuttles work onto the main thread).
* **Context** wraps Browser and adds main-thread-only sub-flows like
  ``input``, ``confirm``, ``run_external``, and ``page``. These read
  keys synchronously or suspend the terminal to launch external
  processes; they are *not* safe to call from a worker thread.

Affordances exposed on Context: ``cursor``, ``selected``, ``targets``,
plus pass-through versions of ``refresh / cursor_to / expand / select /
message / error / quit`` and the main-thread sub-flows ``run_external``,
``page``, ``input``, ``confirm``, ``pick``, ``insert``.
"""

import os
import shutil
import subprocess
from typing import Any, Callable, Optional


class Context:
    """The handle that action handlers receive.

    Construction is internal — the main loop creates one Context
    per dispatched action. Handlers never see the Browser directly,
    which keeps the surface small and steers recipes towards the
    main-thread-aware affordances.
    """

    def __init__(self, browser) -> None:
        self._browser = browser

    # ---- selection helpers --------------------------------------------

    @property
    def cursor(self) -> Optional['Item']:
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
    def selected(self) -> list:
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
    def targets(self) -> list:
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

    def refresh(self, id: Any = None,
                on_complete: Optional[Callable[[], None]] = None) -> 'Pending':
        """Refetch one parent's children, or the full root if ``id`` is None.

        Returns a :class:`Pending` that resolves once the worker has
        delivered the new children list. Safe to call from any thread.
        """
        return self._browser.refresh(id, on_complete)

    def cursor_to(self, id: Any,
                  on_complete: Optional[Callable[[], None]] = None) -> 'Pending':
        """Move the cursor onto the item with ``id``.

        Returns a :class:`Pending` that resolves once the cursor is
        positioned. Best-effort for ids not currently visible — see
        :meth:`Browser.cursor_to`.
        """
        return self._browser.cursor_to(id, on_complete)

    def expand(self, id: Any,
               on_complete: Optional[Callable[[], None]] = None) -> 'Pending':
        """Expand and fetch the children of ``id``.

        Returns a :class:`Pending` that resolves once children are
        cached (or immediately if already cached).
        """
        return self._browser.expand(id, on_complete)

    def select(self, ids, replace: bool = False) -> None:
        """Add ``ids`` to the selection set (or replace it)."""
        return self._browser.select(ids, replace)

    def message(self, text: str) -> None:
        """Surface ``text`` as a transient status message."""
        self._browser.message(text)

    def error(self, text: str) -> None:
        """Surface ``text`` as an error message."""
        self._browser.error(text)

    def quit(self, code: int = 0, output: str = '') -> None:
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

    def page(self, text: str, lang: str = '') -> None:
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

    def input(self, prompt: str, default: str = '') -> Optional[str]:
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

    def confirm(self, prompt: str) -> bool:
        """Show ``prompt`` and read y/n on the info bar.

        Returns ``True`` for ``y``/``Y``, ``False`` for ``n``/``N`` or
        cancel. Headless Browsers return ``False`` so unit tests can
        rely on the safe-default outcome.
        """
        if self._browser._headless:
            return False
        return _confirm_on_info_bar(self._browser, prompt)

    def insert(self, label: str,
               on_confirm: Callable[[str, Any], None]) -> None:
        """Enter insert mode for placing a new item. (ticket #21)

        The user moves a placement marker through the visible tree:

          * ``up/k``, ``down/j``: move marker up/down by one row
          * ``home/g``, ``end/G``: jump to top/bottom (within scope)
          * ``pgup``, ``pgdn``: page-sized jumps
          * ``right``: indent — make child of entry above (expanding it
                       if it has un-shown children)
          * ``left``: outdent — collapse a sibling-above-with-children,
                     or move marker before the parent ancestor
          * ``enter``: confirm — invokes ``on_confirm(relation, dest_id)``
                       where ``relation`` is one of ``'before'``,
                       ``'after'``, ``'first'``
          * ``esc/ctrl-c/q``: cancel — does *not* invoke the callback

        ``label`` is shown on the marker row (``-- {label} --``) so the
        user can see what they're placing (e.g. ``'create'``, ``'move'``).

        ``ctx.insert`` returns immediately after configuring insert
        state; the actual key handling happens in the main loop's
        dispatch (which routes through ``_handle_insert_key`` while
        ``_insert_mode`` is True).

        Headless Browsers are a no-op (state stays unmodified, callback
        never fires) — unit tests exercise the key handler directly.
        """
        if self._browser._headless:
            return
        state = self._browser._state
        vis = visible_items(state)
        if not vis:
            return
        # Default placement: gap right after the cursor item. visible_items
        # builds the list with the scope_root row at index 0 when scoped,
        # so cursor + 1 always lands at a real-row gap.
        pos = state.cursor + 1
        # Clamp to [min_pos, len(vis)]; min_pos is 1 (skip the
        # scope_root gap at index 0 when present).
        max_pos = len(vis)
        min_pos = 1 if vis and vis[0].kind == 'scope_root' else 1
        if pos > max_pos:
            pos = max_pos
        if pos < min_pos:
            pos = min_pos
        self._browser._insert_mode = True
        self._browser._insert_pos = pos
        self._browser._insert_depth = auto_insert_depth(pos, vis)
        self._browser._insert_label = label
        self._browser._insert_callback = on_confirm
        self._browser._needs_redraw.add('all')

    def pick(self, label: str, options) -> Optional[str]:
        """fzf-style filterable picker overlaid on the preview pane.

        Renders a ``label> `` prompt on the info bar and the filtered
        list of ``options`` in the preview pane area. The user can:

          * type to filter (case-insensitive substring match);
          * up / down / ctrl-p / ctrl-n to move the picker cursor;
          * home / end to jump to the first / last filtered match;
          * enter to select the highlighted option;
          * esc / ctrl-c to cancel;
          * backspace to edit the filter.

        Returns the selected option string, or ``None`` if cancelled.
        Headless Browsers return ``None`` immediately so unit tests can
        rely on the cancel outcome without driving a key stream.

        The picker is **not re-entrant** — calling ``ctx.pick`` from
        inside another ``ctx.pick`` handler is unsupported and will
        produce undefined screen state.
        """
        if self._browser._headless:
            return None
        return _pick_on_info_bar(self._browser, label, list(options))


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
    layout = layout_panes(cols, rows, show_preview=browser.show_preview,
                          list_ratio=browser.list_ratio)
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


# ---- pick / picker overlay ------------------------------------------------
#
# fzf-style sub-flow: prompt 'label> ' on the info bar, filtered list of
# options overlaid in the preview pane area. Mirrors plan-tui's
# ``action_status`` (see plan-source/src-tui/060-actions.py:134) — same
# key dispatch, simpler cancel/return contract. Resize handling inside
# the picker is deferred to phase 3 (the helper just continues the loop
# on ``_notify`` / SIGWINCH; the next iteration re-reads geometry and
# repaints).


def _pick_on_info_bar(browser, label, options, *, _read_key=None):
    """Run the fzf-style picker loop. Returns the chosen string or None.

    ``_read_key`` is an injection seam for unit tests — when None we
    defer to the module-level ``read_key`` from ``020-terminal``. Tests
    pass an iterator-backed callable to drive a deterministic key stream
    without a real TTY.

    Layout:
      * filter prompt on ``info_row`` (yellow-on-blue label, then the
        current filter string, then dim filler ─);
      * filtered options overlaid on the preview-pane area starting at
        ``prev_top`` (note: ``prev_top`` is the preview's *separator*
        row in the regular renderer; the picker repurposes it as the
        first option row, which is fine because exiting the picker
        always sets ``_needs_redraw = {'all'}`` so the next render
        repaints the separator over the leftover row).

    On exit (enter or esc) we mark the layout dirty so the main loop
    repaints the regular UI on its next pass.
    """
    rk = _read_key if _read_key is not None else read_key

    filter_query = ''
    cursor = 0

    def _filtered():
        if not filter_query:
            return list(options)
        q = filter_query.lower()
        return [o for o in options if q in o.lower()]

    while True:
        # Re-derive layout each iteration so a SIGWINCH-triggered redraw
        # picks up the new terminal size on the next paint.
        cols, rows_total = term_size()
        layout = layout_panes(
            cols, rows_total,
            show_preview=browser.show_preview,
            list_ratio=browser.list_ratio,
        )
        cols = layout['cols']
        prev_top = layout['prev_top']
        prev_height = layout['prev_height']
        info_row = layout['info_row']

        # When the info row coincides with the preview's top (i.e. no
        # children-grid pane), the filter prompt and the options list
        # would otherwise overdraw the same row. Reserve the top row
        # for the prompt and slide the options down by one.
        if info_row > 0 and info_row == prev_top and prev_height > 0:
            options_top = prev_top + 1
            options_height = prev_height - 1
        else:
            options_top = prev_top
            options_height = prev_height

        visible = _filtered()
        if cursor >= len(visible):
            cursor = max(0, len(visible) - 1)

        # ---- filter prompt on the info row ------------------------
        if info_row > 0:
            move(info_row, 1)
            clear_line()
            S = '─'
            prompt = ' {}> '.format(label)
            set_style(fg=11, bg=4, bold=True)
            write(prompt[:cols])
            pos = min(len(prompt), cols)
            if pos < cols:
                set_style(fg=252, bg=236)
                write(filter_query[:cols - pos])
                pos += min(len(filter_query), cols - pos)
            if pos < cols:
                set_style(fg=8)
                write(S * (cols - pos))
            reset_style()

        # ---- options list in the preview-pane area ----------------
        if options_height > 0:
            for i in range(options_height):
                move(options_top + i, 1)
                clear_line()
                if i < len(visible):
                    label_text = visible[i]
                    if i == cursor:
                        set_style(reverse=True)
                        line = ('  ' + label_text).ljust(cols)[:cols]
                        write(line)
                        reset_style()
                    else:
                        write(('  ' + label_text)[:cols])

        flush()

        key = rk()

        if key == '_notify':
            # Background workers nudged us — drain main-thread work and
            # re-render on the next iteration. Resize is handled
            # implicitly by re-deriving the layout above.
            browser.drain_main_queue()
            browser.apply_children_results()
            browser.apply_preview_result()
            continue

        if key in ('down', 'ctrl-n'):
            if visible:
                cursor = (cursor + 1) % len(visible)
        elif key in ('up', 'ctrl-p'):
            if visible:
                cursor = (cursor - 1) % len(visible)
        elif key == 'home':
            cursor = 0
        elif key == 'end':
            if visible:
                cursor = len(visible) - 1
        elif key == 'enter':
            if visible:
                browser._needs_redraw.add('all')
                return visible[cursor]
            # No matches; ignore enter.
        elif key in ('esc', 'ctrl-c'):
            browser._needs_redraw.add('all')
            return None
        elif key == 'backspace':
            if filter_query:
                filter_query = filter_query[:-1]
                cursor = 0
        elif key == 'space':
            filter_query += ' '
            cursor = 0
        elif len(key) == 1 and key.isprintable():
            filter_query += key
            cursor = 0
        # Other keys (alt-*, ctrl-* not handled, mouse, function keys) —
        # silently ignored, loop continues.
