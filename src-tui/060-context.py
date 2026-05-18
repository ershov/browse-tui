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
import threading
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

    # ---- escape hatches (advanced; unstable surface) ------------------
    #
    # These expose the underlying Browser and State for recipes that
    # need something not covered by Context's documented API.
    # Everything reachable through them is "advanced / at your own
    # risk" — names and shapes here may change between minor versions
    # where the Context surface itself does not. Prefer the typed
    # Context methods whenever they cover the use case.

    @property
    def browser(self):
        """The underlying :class:`Browser` instance (advanced; unstable).

        Use Context methods first. Reach for this only when there is
        a Browser-level capability that has not yet been promoted to
        Context (please file an issue when that happens).
        """
        return self._browser

    @property
    def state(self):
        """The underlying :class:`State` dataclass (advanced; read-only).

        Useful for inspecting state fields that have no dedicated
        accessor — ``state.expanded``, ``state.scope_stack``,
        ``state.cursor``, ``state.selected`` etc. Mutating fields
        directly is unsupported; route writes through Context /
        Browser methods.
        """
        return self._browser._state

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
    def visible_items(self) -> list:
        """Visible rows in display order, as ``(Item, kind)`` tuples.

        ``kind`` is one of ``'normal'``, ``'scope_root'``, ``'pending'``.
        Reflects the current state of expanded / scoped subtrees, so a
        row expanded by an earlier ``Right`` press shows up with its
        children inline. ``ctx.cursor_index`` indexes this same list.

        Useful for cross-subtree navigation primitives — e.g. an action
        that wants to "jump to the next voice message anywhere visible"
        walks this list rather than re-fetching a particular parent's
        children.
        """
        state = self._browser._state
        return [(e.item, e.kind) for e in visible_items(state)]

    @property
    def cursor_index(self) -> int:
        """Index of the cursor row in ``visible_items``.

        Returns the raw ``state.cursor`` value. Out-of-range or
        on-a-placeholder cases are not filtered here — pair with
        ``visible_items`` and check ``kind`` if you care.
        """
        return self._browser._state.cursor

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
               on_complete: Optional[Callable[[], None]] = None,
               autoscroll: bool = False) -> 'Pending':
        """Expand and fetch the children of ``id``.

        Returns a :class:`Pending` that resolves once children are
        cached (or immediately if already cached).

        ``autoscroll`` (default ``False``): pass ``True`` to park a
        scroll-to-fit goal that adjusts the list viewport to show the
        parent row plus its newly-revealed subtree (re-applied as
        async children stream in). User-driven navigation passes
        ``True``; recipes default to ``False`` so bulk-expand setup
        doesn't surprise the user.
        """
        return self._browser.expand(id, on_complete, autoscroll=autoscroll)

    def select(self, ids, replace: bool = False) -> None:
        """Add ``ids`` to the selection set (or replace it)."""
        return self._browser.select(ids, replace)

    @property
    def mode(self) -> 'Mode':
        """Current input-dispatch mode (``Mode`` enum).

        Pass-through to :attr:`Browser.mode`. Useful for action
        handlers that want to short-circuit when a prompt is open.
        """
        return self._browser.mode

    @property
    def search_query(self) -> str:
        """Active search query string (``''`` when none).

        Pass-through to :attr:`Browser.search_query`. Reflects the
        live entry during ``Mode.SEARCH_EDIT``.
        """
        return self._browser.search_query

    def set_search_query(self, text: str) -> None:
        """Replace the search query.

        Pass-through to :meth:`Browser.set_search_query`. Empty
        ``text`` clears the search. Forces ``Mode.NORMAL`` (exits
        any in-progress prompt).
        """
        return self._browser.set_search_query(text)

    def clear_search(self) -> None:
        """Drop the search query; alias for ``set_search_query('')``."""
        return self._browser.clear_search()

    @property
    def scope(self):
        """Current scope id, or ``None`` at the root.

        Pass-through to :attr:`Browser.scope`. Recipes can use this
        from any action handler that needs to branch on "am I in a
        scope?" without poking ``ctx.state.scope_stack`` directly.
        """
        return self._browser.scope

    @property
    def scope_stack(self) -> tuple:
        """Ancestor chain (root-first) of the current scope, as a tuple.

        Pass-through to :attr:`Browser.scope_stack`. Read-only — use
        :meth:`scope_into` / :meth:`scope_out` to change scope.
        """
        return self._browser.scope_stack

    def scope_into(self, id) -> None:
        """Drill into the item with ``id``.

        Pass-through to :meth:`Browser.scope_into`. Pushes ``id``
        onto the scope stack, lands the cursor on the new view's
        row 0, and fires ``on_scope_change``. No-op if already
        scoped into ``id``.
        """
        return self._browser.scope_into(id)

    def scope_out(self) -> None:
        """Pop the top of the scope stack.

        Pass-through to :meth:`Browser.scope_out`. Lands the cursor
        on the row we drilled into (or 0 if not found). Fires
        ``on_scope_change``. No-op at the root.
        """
        return self._browser.scope_out()

    def collapse_all(self) -> None:
        """Clear every entry from ``state.expanded`` for the current scope.

        Pass-through to :meth:`Browser.collapse_all`. Cursor identity
        is preserved when possible; if the cursor sat inside a now-
        collapsed subtree the framework walks back to the nearest
        still-visible ancestor.
        """
        return self._browser.collapse_all()

    def expand_subtree(self, id, lazy: bool = True) -> None:
        """Expand every cached descendant of ``id`` (including ``id``).

        Pass-through to :meth:`Browser.expand_subtree`. ``lazy=True``
        (default) only walks the cached part of the tree —
        un-fetched branches stay collapsed.
        """
        return self._browser.expand_subtree(id, lazy=lazy)

    def nav_home(self) -> None:
        """Move cursor to row 0 and engage the ``PIN_FIRST`` cursor pin.

        The cursor follows new arrivals at the top until any
        non-home/non-end navigation clears the pin. See
        :meth:`Browser.nav_home` and
        ``docs/superpowers/specs/2026-05-17-cursor-pin-design.md``.
        """
        self._browser.nav_home()

    def nav_end(self) -> None:
        """Move cursor to the last visible row and engage ``PIN_LAST``.

        Symmetric to :meth:`nav_home`.
        """
        self._browser.nav_end()

    @property
    def filters(self) -> tuple:
        """Currently-active filter strings (committed + live), in order.

        Returns a tuple of non-empty strings — the empty placeholder
        slot used by the filter-edit prompt before the user has typed
        anything is excluded. The live (in-progress) entry IS included
        as soon as the user has typed even one character, because it
        already affects what the user sees on screen. See
        ``docs/superpowers/specs/2026-05-17-filter-design.md``.
        """
        return self._browser.filters

    def set_filters(self, filters) -> None:
        """Replace the filter list with the given iterable of strings.

        Empty strings are dropped. If the user is in FILTER_EDIT, the
        mode is forced to NORMAL (the in-progress placeholder is
        discarded). Recipe writes are authoritative.
        """
        self._browser.set_filters(filters)

    def add_filter(self, text: str) -> None:
        """Append ``text`` to the filter stack (no-op if empty).

        Forces FILTER_EDIT exit if active before appending.
        """
        self._browser.add_filter(text)

    def clear_filters(self) -> None:
        """Drop all filters; alias for ``set_filters([])``."""
        self._browser.clear_filters()

    def message(self, text: str) -> None:
        """Surface ``text`` as a transient status message."""
        self._browser.message(text)

    def error(self, text: str) -> None:
        """Surface ``text`` as an error message."""
        self._browser.error(text)

    def quit(self, code: int = 0, output: str = '') -> None:
        """Request the main loop to exit with ``code`` and stdout ``output``."""
        self._browser.quit(code, output)

    # ---- cache introspection ------------------------------------------
    #
    # Read-only views into the framework's live item / children cache.
    # See :meth:`Browser.items_by_id` for invariants and lifecycle
    # notes.

    @property
    def items_by_id(self) -> dict:
        """All currently-loaded items keyed by id (live read-only view).

        Pass-through to :meth:`Browser.items_by_id`. The returned
        dict is the framework's live cache; do not mutate. Use
        :meth:`update_data` to add / remove items.
        """
        return self._browser.items_by_id

    def get_item(self, id_) -> Optional['Item']:
        """Return the loaded Item with ``id`` or ``None``.

        Pass-through to :meth:`Browser.get_item`. O(1) lookup over
        the item cache.
        """
        return self._browser.get_item(id_)

    def cached_children(self, parent_id) -> Optional[list]:
        """Loaded children of ``parent_id`` as a list, or ``None`` if not cached.

        Pass-through to :meth:`Browser.cached_children`. Returns a
        shallow copy; ``None`` vs ``[]`` distinguishes "not fetched"
        from "fetched, no children".
        """
        return self._browser.cached_children(parent_id)

    def cached_parents(self) -> list:
        """Ids of every parent whose children list is currently cached.

        Pass-through to :meth:`Browser.cached_parents`. Useful for
        recipes iterating every loaded subtree (mtime watchers,
        tail-feed diffs, bulk visibility flips).
        """
        return self._browser.cached_parents()

    def all_items(self):
        """Iterator over every currently-loaded Item.

        Pass-through to :meth:`Browser.all_items`. Snapshot iterator
        — safe under concurrent cache mutation.
        """
        return self._browser.all_items()

    # ---- push API pass-throughs / convenience -------------------------
    #
    # These mirror the streaming-push surface (Section 3 of the design
    # doc): ``update_data`` for batched ops, plus single-op convenience
    # wrappers (``upsert`` / ``set_item`` / ``remove``) for the common
    # case of one mutation at a time. The preview methods forward to
    # Browser; ``run_in_worker`` spawns a one-shot daemon thread.

    def update_data(self, ops) -> None:
        """Apply a batched list of tree-mutation ops on the main thread.

        Pass-through to :meth:`Browser.update_data`. ``ops`` is an
        iterable of op tuples produced by the module-level helpers
        (``upsert`` / ``set_item`` / ``remove`` / ``clear_children`` /
        ``complete`` / ``incomplete``). Returns ``None``.
        """
        return self._browser.update_data(ops)

    def upsert(self, id, parent_id, *, where=None, **fields) -> None:
        """Single-op convenience: ``update_data([upsert(id, parent_id, **fields)])``.

        Routes through ``Browser.update_data`` so the mutation lands on
        the main thread atomically with respect to render. Returns
        ``None``. For multiple ops, prefer ``update_data`` directly to
        keep them in one batch.

        ``where`` (optional, keyword-only) is a positioning descriptor;
        see ``upsert`` helper / ``apply_ops`` semantics for details.
        """
        return self._browser.update_data(
            [upsert(id, parent_id, where=where, **fields)]
        )

    def set_item(self, id, parent_id, *, where=None, **fields) -> None:
        """Single-op convenience: ``update_data([set_item(id, parent_id, **fields)])``.

        Insert-or-replace shape — see ``apply_ops`` semantics for ``set``.
        Returns ``None``. ``where`` (optional, keyword-only) carries an
        optional positioning descriptor.
        """
        return self._browser.update_data(
            [set_item(id, parent_id, where=where, **fields)]
        )

    def remove(self, id) -> None:
        """Single-op convenience: ``update_data([remove(id)])``.

        Removes the item with this id (cascades to its cached children).
        Returns ``None``.
        """
        return self._browser.update_data([remove(id)])

    def set_preview(self, id, text) -> None:
        """Pass-through to :meth:`Browser.set_preview`.

        Replaces the preview content for ``id``. ``None`` is coerced
        to ``''``.
        """
        return self._browser.set_preview(id, text)

    def append_preview(self, id, chunk) -> None:
        """Pass-through to :meth:`Browser.append_preview`.

        Appends ``chunk`` to the per-id preview cache. See the Browser
        method's docstring for ordering caveats versus ``set_preview``.
        """
        return self._browser.append_preview(id, chunk)

    def clear_preview(self, id) -> None:
        """Pass-through to :meth:`Browser.clear_preview`.

        Drops the cached preview text for ``id``.
        """
        return self._browser.clear_preview(id)

    def preview_to_tail(self) -> None:
        """Pass-through to :meth:`Browser.preview_to_tail`.

        Pins the preview view to the bottom of its content; subsequent
        ``append_preview`` chunks keep the view on the new tail until
        the user scrolls up.
        """
        return self._browser.preview_to_tail()

    def invalidate_preview(self, id) -> None:
        """Pass-through to :meth:`Browser.invalidate_preview`.

        Drops cached preview text for ``id`` and re-fetches without
        resetting view state (scroll, tail pin, help mode). Used when
        the underlying data feeding a preview has changed but the
        cursor hasn't moved.
        """
        return self._browser.invalidate_preview(id)

    def get_cached_preview(self, id) -> Optional[str]:
        """Cached preview text for ``id`` or ``None``.

        Pass-through to :meth:`Browser.get_cached_preview`. Read-only,
        no callback fire. Returns ``None`` for ids with no cached entry.
        """
        return self._browser.get_cached_preview(id)

    def drop_preview_cache(self, id=None) -> None:
        """Drop cached preview text.

        Pass-through to :meth:`Browser.drop_preview_cache`. ``id=None``
        drops every entry; when the dropped id matches the current
        preview cursor, the worker is auto-kicked and the preview pane
        is redrawn.
        """
        return self._browser.drop_preview_cache(id)

    @property
    def preview_item_id(self):
        """Id whose preview is currently displayed (or ``None``).

        Pass-through to :meth:`Browser.preview_item_id`. May lag behind
        the row cursor during rapid navigation.
        """
        return self._browser.preview_item_id

    def run_in_worker(self, fn: Callable[[], Any]) -> threading.Thread:
        """Run ``fn()`` on a fresh daemon thread, surfacing exceptions.

        The function takes no arguments and its return value is ignored.
        Uncaught exceptions are routed to ``browser.error`` (matching
        :meth:`Browser.watch`'s pattern) so a failing one-shot doesn't
        crash the process — the thread dies on the exception and the
        message lands on the main thread alongside other errors.

        The returned thread handle is mostly informational; recipes
        that need synchronisation should use ``threading.Event`` /
        ``Pending`` inside ``fn`` itself.

        Design note: the existing ``_children_worker`` is a FIFO of
        parent-ids, not a callable runner, so reusing it would conflate
        unrelated traffic. A dedicated daemon thread per submission
        keeps the surface small and matches ``Browser.watch``'s
        approach for arbitrary callables.
        """
        browser = self._browser

        def _runner():
            try:
                fn()
            except Exception as e:
                browser.error(f'run_in_worker: {type(e).__name__}: {e}')

        t = threading.Thread(
            target=_runner,
            daemon=True,
            name='browse-tui-ctx-worker',
        )
        t.start()
        return t

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
    """Return ``(row, left, width)`` for the info bar, or ``(0, 0, 0)`` if no room.

    The render layer owns ``layout_panes`` and the actual geometry; we
    re-derive it here so the prompt helpers don't have to thread layout
    state through every call. Returns ``(0, 0, 0)`` when the terminal is
    too small to host the info bar. ``left`` and ``width`` come from the
    info-bar Rect so non-'h' layouts (v/m/pc) — where the info bar is a
    standalone bottom row, currently full-width — still resolve to the
    right span; if a future layout makes the info bar narrower this
    helper will track that automatically.
    """
    cols, rows = term_size()
    layout = layout_panes(cols, rows,
                          split=getattr(browser, 'split', 'h'),
                          show_preview=browser.show_preview,
                          list_ratio=browser.list_ratio)
    info_bar = layout.get('info_bar')
    if info_bar is None:
        return 0, 0, 0
    return info_bar.top, info_bar.left, info_bar.width


def _draw_info_prompt(browser, prompt, buf):
    """Paint ``prompt + buf`` on the info bar.

    Mirrors plan-tui's _read_string drawing: prompt in bold yellow on
    blue, buf in normal text, fill rest of the row with separator
    characters in gray. Doesn't read input — callers loop with
    ``read_key`` and call this after each key.

    Uses the info-bar Rect (``left``..``left+width``) rather than the
    full screen width so the prompt overlays exactly the info-bar row,
    leaving the rest of the screen alone.
    """
    row, left, width = _info_bar_geometry(browser)
    if row <= 0 or width <= 0:
        return
    move(row, left)
    clear_columns(row, left, left + width)
    move(row, left)
    set_style(fg=11, bg=4, bold=True)
    write(prompt[:width])
    pos = len(prompt) if len(prompt) < width else width
    if pos < width:
        set_style()
        remaining = width - pos
        write(buf[:remaining])
        pos += min(len(buf), remaining)
    if pos < width:
        set_style(fg=8)
        write('─' * (width - pos))
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
            split=getattr(browser, 'split', 'h'),
            show_preview=browser.show_preview,
            list_ratio=browser.list_ratio,
        )
        cols = layout['cols']
        preview_rect = layout.get('preview')
        info_bar = layout.get('info_bar')
        prev_top = preview_rect.top if preview_rect is not None else 0
        prev_left = preview_rect.left if preview_rect is not None else 1
        prev_right = preview_rect.right if preview_rect is not None else cols + 1
        prev_height = preview_rect.height if preview_rect is not None else 0
        info_row = info_bar.top if info_bar is not None else 0
        info_left = info_bar.left if info_bar is not None else 1
        info_width = info_bar.width if info_bar is not None else 0

        # When the info row coincides with the preview's top (i.e. no
        # children-grid pane, layout 'h'), the filter prompt and the
        # options list would otherwise overdraw the same row. Reserve
        # the top row for the prompt and slide the options down by
        # one. In v/m/pc layouts the info bar is a standalone bottom
        # row and never overlaps the preview rect.
        if info_row > 0 and info_row == prev_top and prev_height > 0:
            options_top = prev_top + 1
            options_height = prev_height - 1
        else:
            options_top = prev_top
            options_height = prev_height

        visible = _filtered()
        if cursor >= len(visible):
            cursor = max(0, len(visible) - 1)

        # ---- filter prompt on the info bar ------------------------
        # Use the info-bar Rect's left/width so the prompt overlays
        # exactly the info bar in any layout (today the info bar is
        # full-width in every layout, but using the rect keeps this
        # robust to future narrower info bars).
        if info_row > 0 and info_width > 0:
            move(info_row, info_left)
            clear_columns(info_row, info_left, info_left + info_width)
            move(info_row, info_left)
            S = '─'
            prompt = ' {}> '.format(label)
            set_style(fg=11, bg=4, bold=True)
            write(prompt[:info_width])
            pos = min(len(prompt), info_width)
            if pos < info_width:
                set_style(fg=252, bg=236)
                write(filter_query[:info_width - pos])
                pos += min(len(filter_query), info_width - pos)
            if pos < info_width:
                set_style(fg=8)
                write(S * (info_width - pos))
            reset_style()

        # ---- options list in the preview-pane area ----------------
        # Use the preview rect's left/right so options overlay only the
        # preview pane (not the whole row) in v/m/pc layouts where the
        # preview pane is narrower than the screen.
        prev_width = max(0, prev_right - prev_left)
        if options_height > 0 and prev_width > 0:
            for i in range(options_height):
                move(options_top + i, prev_left)
                clear_columns(options_top + i, prev_left, prev_right)
                if i < len(visible):
                    move(options_top + i, prev_left)
                    label_text = visible[i]
                    if i == cursor:
                        set_style(reverse=True)
                        line = ('  ' + label_text).ljust(prev_width)[:prev_width]
                        write(line)
                        reset_style()
                    else:
                        write(('  ' + label_text)[:prev_width])

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
