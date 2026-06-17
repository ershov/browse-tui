"""browse-tui: Context — main-thread-only wrapper around Browser.

Action handlers receive a ``Context`` rather than the bare Browser. The
split is deliberate:

* **Browser** is the thread-safe surface — every public op is callable
  from any thread (``post()`` shuttles work onto the main thread).
* **Context** wraps Browser and adds main-thread-only sub-flows like
  ``input``, ``confirm``, ``run_external``, and ``page``. These open
  modal dialogs (a nested key loop) or suspend the terminal to launch
  external processes; they are *not* safe to call from a worker thread.

The thread-safe exception: the ``*_async`` dialog variants
(``confirm_async`` / ``alert_async`` / ``pick_async`` / ``menu_async`` /
``input_async``) and the dialog-control pass-throughs
(``is_dialog_open`` / ``close_dialog``) ARE callable from any thread. The
``*_async`` methods delegate to :meth:`Browser.open_dialog_async`, which
posts the open onto the main thread; the dialog opens there and its
``on_result`` callback fires there too (exactly once, exceptions caught).
A worker that needs to interact with the user therefore uses the async
variants — the blocking ``input`` / ``confirm`` / … remain main-thread-only.

Affordances exposed on Context: ``cursor``, ``selected``, ``targets``,
plus pass-through versions of ``refresh / cursor_to / expand / select /
flash / log / error / print / quit`` and the main-thread sub-flows
``run_external``, ``page``, ``input``, ``confirm``, ``alert``, ``pick``,
``menu``, ``insert`` (each with a thread-safe ``*_async`` sibling for the
dialog flows).
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

        ``kind`` is one of ``'normal'``, ``'pending'``. The scope row
        at depth 0 (when scoped) is emitted as ``'normal'`` — recipes
        identify it via ``item.id == ctx.scope`` rather than a row-role
        discriminator.
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

    def collapse(self, id: Any) -> None:
        """Collapse ``id`` — remove it from the expanded set.

        Pass-through to :meth:`Browser.collapse`. The single-node
        counterpart to :meth:`expand`: folds away ``id``'s children
        and repaints. Collapsing an id that isn't expanded is a no-op.
        Returns ``None`` — no fetch, nothing to await.
        """
        return self._browser.collapse(id)

    def select(self, ids, replace: bool = False) -> None:
        """Add ``ids`` to the selection set (or replace it)."""
        return self._browser.select(ids, replace)

    def select_all_visible(self) -> None:
        """Set the selection to every visible normal row (WYSIWYG).

        Pass-through to :meth:`Browser.select_all_visible`. Items
        previously selected that are not currently visible are
        dropped from the selection.
        """
        return self._browser.select_all_visible()

    def clear_selection(self) -> None:
        """Drop every entry from the selection set.

        Pass-through to :meth:`Browser.clear_selection`. No-op when
        nothing is selected.
        """
        return self._browser.clear_selection()

    def invert_selection(self) -> None:
        """Flip selection across visible normal rows.

        Pass-through to :meth:`Browser.invert_selection`. Selection
        state of non-visible rows is preserved as-is.
        """
        return self._browser.invert_selection()

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
    def hint(self) -> str:
        """The info-bar hint line.

        Pass-through to :attr:`Browser.hint`. Defaults to
        ``' /:search  ?:help  q:quit '``.
        """
        return self._browser.hint

    def set_hint(self, text: str) -> None:
        """Replace the info-bar hint line.

        Pass-through to :meth:`Browser.set_hint`. Repaints the info bar.
        """
        return self._browser.set_hint(text)

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
        un-fetched branches stay collapsed. A ``boundary`` descendant is
        revealed but not expanded (only ``id`` itself joins when it is a
        boundary) — see ``Item.boundary``.
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

    def flash(self, text: str, log: bool = False) -> None:
        """Surface ``text`` as a transient info-bar notice.

        Pass-through to :meth:`Browser.flash`. Use for toggle / mode
        acks and "nothing to show" notices; pass ``log=True`` to also
        record it in the message log (side effects, degradation
        warnings). Auto-clears after a short timeout.
        """
        self._browser.flash(text, log)

    def log(self, text: str) -> None:
        """Append ``text`` to the message log (no on-screen notice).

        Pass-through to :meth:`Browser.log`. The ``console.log``-style
        record; view it on demand via the framework log pager.
        """
        self._browser.log(text)

    def error(self, text: str) -> None:
        """Surface ``text`` as a red, sticky info-bar notice.

        Pass-through to :meth:`Browser.error`. Always logged; cleared by
        the next keypress (after a brief minimum-display window).
        """
        self._browser.error(text)

    def print(self, text, end: str = '\n') -> None:
        """Append ``text`` + ``end`` to the stdout content channel.

        Pass-through to :meth:`Browser.print`. Mirrors builtin ``print``
        (newline-terminated; ``end`` overridable) and never blocks the
        UI: a pipe/file stdout is drained by the event loop as the
        consumer keeps up, a tty stdout is held and delivered to normal
        scrollback after the UI exits — in strict FIFO order, ahead of
        any ``quit`` output. After the consumer goes away (``EPIPE``)
        calls become no-ops for the rest of the session.
        """
        self._browser.print(text, end)

    def quit(self, code: int = 0, output: str = '') -> None:
        """Request the main loop to exit with ``code`` and stdout ``output``.

        ``output`` joins the stdout content channel at teardown, after
        anything written via :meth:`print`.
        """
        self._browser.quit(code, output)

    def is_dialog_open(self) -> bool:
        """Whether a modal dialog is currently displayed.

        Pass-through to :meth:`Browser.is_dialog_open`. Useful from the
        main thread, or as a cross-thread hint (a best-effort snapshot).
        """
        return self._browser.is_dialog_open()

    def close_dialog(self, value=None) -> None:
        """Dismiss the open modal dialog, delivering ``value`` to its waiter.

        Pass-through to :meth:`Browser.close_dialog`. Whoever is waiting on
        the open dialog (the blocking return, or a future async callback)
        receives ``value``; ``None`` means "no answer." A no-op if no dialog
        is open. Thread-safe.
        """
        self._browser.close_dialog(value)

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

    def register_plugin(self, cfg) -> None:
        """Pass-through to the module-level ``register_plugin``.

        Appends ``cfg`` (a :class:`PluginConfig`) to the global
        ``registered_plugins`` list. Note that calling this from a
        live Context registers for *future* Browser constructions —
        the current Browser's ``__init__`` hooks have already fired.
        """
        return register_plugin(cfg)

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

    @property
    def preview_width(self) -> int:
        """Current width of the preview pane in terminal columns.

        Pass-through to :meth:`Browser.preview_width`. Recomputed from
        live geometry each call; returns ``0`` when the preview pane
        isn't shown or terminal geometry can't be read. Callers wanting
        a non-zero fallback should pick one explicitly, e.g.
        ``ctx.preview_width or 80``.
        """
        return self._browser.preview_width

    def run_in_slot(self, name: str, fn) -> 'CancellationToken':
        """Run ``fn(token)`` in a daemon thread; supersede prior in slot.

        Pass-through to :meth:`Browser.run_in_slot`. ``name``
        identifies the slot; the previous worker (if any) for the
        same name has its token cancelled before the new one starts.
        ``fn`` receives a :class:`CancellationToken` and must
        cooperatively check ``token.is_cancelled()`` at safe points.
        Exceptions inside ``fn`` route to :attr:`Browser.error`.
        """
        return self._browser.run_in_slot(name, fn)

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

    def run_external(self, cmd, env=None, *, keep_screen=False,
                     stdin_text=None) -> int:
        """Suspend the terminal, run ``cmd``, then resume.

        ``cmd`` is either a list of argv strings or a shell string (the
        latter triggers ``shell=True``). ``env`` is merged with the
        parent environment — pass ``None`` to inherit unchanged.

        ``stdin_text`` (a ``str``, default ``None``) feeds the child a
        document on its **stdin via a real pipe** while its stdout/stderr stay
        the terminal — for launching a child that reads a document from stdin
        and runs its own UI on the terminal device (e.g. ``browse-md -``, which
        slurps stdin then opens ``/dev/tty`` for keys). Unlike argv/env, a pipe
        has no size limit, so this is the correct channel for arbitrarily large
        content; passing it via env would hit ``MAX_ARG_STRLEN`` → ``E2BIG``.
        ``cmd`` must be an argv list when ``stdin_text`` is given. When ``None``
        (the default) the child inherits the terminal on stdin too, as an
        interactive editor needs.

        ``keep_screen=True`` hands the terminal to a child that owns the
        alternate screen itself (e.g. launching another browse-tui recipe):
        the pre-launch suspend stays on the alt screen and emits no clear, so
        there is no flash of the primary screen before the child paints over
        our buffer. Resume is unchanged (re-enters the alt screen and forces a
        full repaint once the child exits). The default fully leaves the alt
        screen, as a plain editor/pager needs.

        Returns the subprocess exit code, or ``-1`` if launching the
        process raised. Errors are also surfaced via ``ctx.error``.

        The child is handed the terminal on its stdin/stdout/stderr (via
        the terminal layer's child-fd accessor) so an interactive editor
        runs on the terminal regardless of how the parent's own fd 0/1
        are wired -- crucially without touching them, so a piped/captured
        ``stdout`` stays clean. In ``--tty -`` mode those fds already are
        the std streams, so this is the usual inherit behaviour.

        Headless Browsers skip the suspend/resume + fd-passing (term layer
        is not initialised) so unit tests can exercise the run path
        without a real TTY -- the child then inherits the test runner's
        fds. The ``_needs_redraw`` flag is still set so the next render
        pass repaints over whatever the external process drew.
        """
        child_fds = {}
        if not self._browser._headless:
            term_suspend(keep_screen=keep_screen)
            in_fd, out_fd = term_child_fds()
            # With piped stdin the child reads its document from the pipe and
            # runs its UI on the terminal device itself, so we hand it only
            # stdout/stderr; otherwise it inherits the terminal on stdin too.
            if stdin_text is None:
                child_fds = {'stdin': in_fd, 'stdout': out_fd, 'stderr': out_fd}
            else:
                child_fds = {'stdout': out_fd, 'stderr': out_fd}
        try:
            full_env = None if env is None else {**os.environ, **env}
            if stdin_text is not None:
                # ``input=`` opens stdin as a pipe and writes the document
                # to it — no argv/env size limit.
                result = subprocess.run(
                    cmd, input=stdin_text.encode('utf-8', errors='replace'),
                    env=full_env, **child_fds)
            else:
                shell = isinstance(cmd, str)
                result = subprocess.run(cmd, shell=shell, env=full_env,
                                        **child_fds)
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

        The pager's stdout/stderr are pointed at the terminal (via the
        terminal layer's child-fd accessor) while its stdin carries the
        text -- so it paints to the terminal even when the parent's
        ``stdout`` is piped, and reads keys from the terminal device
        itself (as ``cmd | less`` always has). In ``--tty -`` mode the
        terminal rides on the std streams, so there is no separate device
        for the pager to read keys from while its stdin carries the text;
        whatever the pager does without a private ``/dev/tty`` is up to it
        (configure ``$PAGER``/``$EDITOR`` for non-interactive use if
        needed) -- the suspend/resume bracket keeps the screen safe either
        way.

        Headless Browsers skip the suspend/resume + fd-passing (no term
        layer); the pager then inherits the test runner's stdin and just
        exits on EOF.
        """
        non_headless = not self._browser._headless

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

        out_fds = {}
        if non_headless:
            term_suspend()
            _, out_fd = term_child_fds()
            out_fds = {'stdout': out_fd, 'stderr': out_fd}
        try:
            proc = subprocess.Popen(pager, stdin=subprocess.PIPE, **out_fds)
            try:
                proc.stdin.write(text.encode('utf-8', errors='replace'))
                proc.stdin.close()
            except BrokenPipeError:
                pass
            proc.wait()
        except Exception as e:
            self.error(f'page: {type(e).__name__}: {e}')
        finally:
            if non_headless:
                term_resume()
            self._browser._needs_redraw.add('all')

    def input(self, prompt: str, default: str = '',
              *, delay_interaction: bool = False) -> Optional[str]:
        """Prompt for a single-line string in a modal input dialog.

        Opens a centered modal with the wrapped ``prompt`` above a one-row
        entry field pre-filled with ``default``. Returns the text typed
        (empty string if the user just hit Enter), or ``None`` if the user
        cancelled with esc/ctrl-c.

        ``delay_interaction`` (forwarded to the modal engine) ignores keys
        the user was typing at the previous screen for a short grace window
        — for dialogs that appear on their own rather than on request.

        Headless Browsers return ``default`` immediately so unit tests can
        drive deterministic flows without opening a dialog.
        """
        if self._browser._headless:
            return default
        return modal_input(self._browser, prompt, default=default,
                           delay_interaction=delay_interaction)

    def confirm(self, message: str, buttons=('&Yes', '&No'), *,
                title: Optional[str] = None,
                delay_interaction: bool = False) -> Optional[Any]:
        """Ask the user to choose a button in a modal choice dialog.

        Opens a centered modal showing ``message`` above a row of
        ``buttons``. Each item is a label ``str`` OR a ``(label, value)``
        2-tuple; the label uses the ``&`` hotkey convention (``'&Yes'``
        shows ``Yes`` with ``Y`` underlined; pressing ``y`` activates it).

        Returns the chosen button's VALUE: the supplied ``value`` for a
        tuple (any type), or the resolved label for a bare string
        (``'Yes'`` / ``'No'`` / …, the ``&`` stripped). ``None`` on
        esc/ctrl-c cancel.

        Map buttons to values so the result is used directly, sidestepping
        string comparison::

            if ctx.confirm(msg, [('&Yes', True), ('&No', False)]):
                ...

        With bare-string buttons, compare explicitly
        (``ctx.confirm(...) == 'Yes'``) — the truthiness idiom
        ``if ctx.confirm(...)`` is wrong because ``'No'`` is a truthy
        string. (Cancel returns ``None``, so mapping a button to ``None``
        makes a confirmed-``None`` indistinguishable from a cancel.)

        ``delay_interaction`` is forwarded to the modal engine. Headless
        Browsers return ``None`` immediately (the safe-default, no-open
        outcome) so unit tests don't drive a key stream.
        """
        if self._browser._headless:
            return None
        return modal_confirm(self._browser, message, buttons, title=title,
                             delay_interaction=delay_interaction)

    def alert(self, text: str, *, title: Optional[str] = None,
              delay_interaction: bool = False) -> None:
        """Show ``text`` in a modal notification with a single OK button.

        Opens a centered modal showing ``text`` above one ``[ OK ]`` button;
        the user dismisses it with enter/space/esc. Returns ``None`` always
        — an alert conveys nothing back to the caller.

        ``delay_interaction`` is forwarded to the modal engine. Headless
        Browsers are a no-op (nothing is drawn, nothing is read).
        """
        if self._browser._headless:
            return None
        return modal_alert(self._browser, text, title=title,
                           delay_interaction=delay_interaction)

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
        # Default placement: gap right after the cursor item. When
        # scoped, ``visible_items`` emits the scope row at index 0 as a
        # normal row at depth 0; ``cursor + 1`` lands at a real-row gap
        # below it (or below the cursor when the cursor is on a child).
        pos = state.cursor + 1
        # Clamp to [1, len(vis)]. The pos=0 gap (above the scope row
        # when scoped, or above the first root row otherwise) is
        # rejected by ``resolve_insert`` so we never park there.
        max_pos = len(vis)
        if pos > max_pos:
            pos = max_pos
        if pos < 1:
            pos = 1
        self._browser._insert_mode = True
        self._browser._insert_pos = pos
        self._browser._insert_depth = auto_insert_depth(pos, vis)
        self._browser._insert_label = label
        self._browser._insert_callback = on_confirm
        self._browser._needs_redraw.add('all')

    def pick(self, label: str, options,
             *, delay_interaction: bool = False) -> Optional[Any]:
        """fzf-style filterable picker in a centered modal dialog.

        Opens a centered modal with a ``> `` filter row above the list of
        ``options`` (``label`` becomes the dialog title). Each item is a
        display ``str`` OR a ``(display, value)`` 2-tuple; the filter matches
        the DISPLAY half (case-insensitive substring). The user can:

          * type to filter (case-insensitive substring match on the display);
          * up / down / ctrl-p / ctrl-n to move the selection (wrapping);
          * home / end to jump to the first / last filtered match;
          * enter to select the highlighted option;
          * esc / ctrl-c to cancel;
          * backspace to edit the filter.

        Returns the chosen option's VALUE — the supplied ``value`` for a tuple
        (any type), or the string itself for a bare option — or ``None`` if
        cancelled. An empty ``options`` returns ``None`` without opening.
        Headless Browsers return ``None`` immediately so unit tests can rely
        on the cancel outcome without driving a key stream.
        ``delay_interaction`` is forwarded to the modal engine.
        """
        if self._browser._headless:
            return None
        return modal_pick(self._browser, label, list(options),
                          delay_interaction=delay_interaction)

    def menu(self, items, *, anchor=None,
             delay_interaction: bool = False) -> Optional[Any]:
        """Anchored, unfiltered selection list — a context menu.

        Opens a modal selection list WITHOUT a filter row. Each item is a
        display ``str`` OR a ``(display, value)`` 2-tuple. ``anchor`` is an
        optional ``(row, col)`` 1-based screen cell the menu drops below;
        when ``None`` it defaults to the list cursor's screen cell so a
        menu reads as attached to the current row. Horizontally an anchored
        menu leans toward screen center but keeps its footprint within the
        list pane's columns, so it hangs off its row rather than drifting onto
        a neighbouring pane (#1051). The user moves with up/down (wrapping),
        jumps with home/end, picks with enter, cancels with esc/ctrl-c.

        Returns the chosen item's VALUE — the supplied ``value`` for a tuple
        (any type), or the string itself for a bare item — or ``None`` on
        cancel. An empty ``items`` returns ``None`` without opening. Headless
        Browsers return ``None`` immediately. ``delay_interaction`` is
        forwarded to the modal engine.
        """
        if self._browser._headless:
            return None
        if anchor is None:
            # A right-click context-menu trigger stashes the click cell on
            # the Browser for the duration of the ``on_context_menu`` fire
            # (see ``Browser._fire_context_menu``); prefer it so the menu
            # drops under the pointer. Otherwise default to the list
            # cursor cell so the menu reads as attached to the current row.
            anchor = (self._browser._context_menu_anchor
                      or _list_cursor_cell(self._browser))
        # An anchored menu leans toward screen center but keeps its footprint
        # within the LIST pane's columns (#1051) — derive that span here, where
        # the layout is already known. ``None`` (no resolvable list pane) lets
        # the menu keep the full-screen #1040 centering. A centered menu (no
        # anchor) passes no bound either.
        bounds = (_list_pane_bounds(self._browser)
                  if anchor is not None else None)
        return modal_menu(self._browser, list(items), anchor=anchor,
                          bounds=bounds, delay_interaction=delay_interaction)

    # ---- thread-safe async dialog variants ----------------------------
    #
    # An async sibling for every dialog flow, callable FROM ANY THREAD (the
    # documented exception to "Context is main-thread-only"). Each builds the
    # SAME content object its blocking sibling builds, then delegates to
    # ``Browser.open_dialog_async`` — which posts the open onto the main
    # thread and fires ``on_result`` THERE, exactly once, with exceptions
    # caught (see ``_fire_dialog_cb``). All return ``None`` immediately. The
    # result is the chosen value, or ``None`` for any no-answer path (cancel,
    # programmatic ``close_dialog``, override by a later dialog, displacement
    # while still pending, or headless). They open the same content as the
    # blocking methods so async dialogs look and behave identically.

    def confirm_async(self, message: str, buttons=('&Yes', '&No'), *,
                      title: Optional[str] = None,
                      on_result: Optional[Callable[[Any], None]] = None) -> None:
        """(thread-safe) Async :meth:`confirm` — open from any thread.

        Builds the same :class:`ChoiceContent` (``message`` above a row of
        ``buttons``) as :meth:`confirm` and opens it centered via
        :meth:`Browser.open_dialog_async`. ``on_result`` receives the chosen
        button's VALUE (the tuple's value, or the resolved label for a bare
        string) or ``None`` for any no-answer path. Returns ``None`` at once.
        """
        content = ChoiceContent(message, buttons, title=title)
        self._browser.open_dialog_async(content, on_result=on_result,
                                        placement='center')

    def alert_async(self, text: str, *, title: Optional[str] = None,
                    on_result: Optional[Callable[[Any], None]] = None) -> None:
        """(thread-safe) Async :meth:`alert` — open from any thread.

        Builds the same single-``&OK`` :class:`ChoiceContent` as :meth:`alert`
        and opens it centered. An alert conveys nothing back, so ``on_result``
        always receives ``None`` (the callback merely signals "dismissed"):
        the activation value is dropped in a wrapper, mirroring the blocking
        :meth:`alert` returning ``None``. Returns ``None`` at once.
        """
        content = ChoiceContent(text, ('&OK',), title=title)
        # Drop whatever the &OK dialog resolves to ('OK', or None on
        # cancel/override) and hand the callback None — an alert's result is
        # always "dismissed". Only wrap when a callback is registered so a
        # bare alert posts nothing extra.
        cb = None
        if on_result is not None:
            cb = lambda _value, _f=on_result: _f(None)
        self._browser.open_dialog_async(content, on_result=cb,
                                        placement='center')

    def pick_async(self, label: str, options, *,
                   on_result: Optional[Callable[[Any], None]] = None) -> None:
        """(thread-safe) Async :meth:`pick` — open from any thread.

        Builds the same filterable :class:`ListContent` (``label`` as the
        title) as :meth:`pick` and opens it centered. ``on_result`` receives
        the chosen option's VALUE or ``None``. An empty ``options`` fires
        ``on_result(None)`` WITHOUT opening (mirroring the blocking
        short-circuit) — once, on the main thread, caught. Returns ``None``.
        """
        options = list(options)
        if not options:
            self._deliver_none(on_result)
            return
        content = ListContent(options, filter=True, title=label)
        self._browser.open_dialog_async(content, on_result=on_result,
                                        placement='center')

    def menu_async(self, items, *, anchor=None,
                   on_result: Optional[Callable[[Any], None]] = None) -> None:
        """(thread-safe) Async :meth:`menu` — open from any thread.

        Builds the same unfiltered :class:`ListContent` as :meth:`menu`.
        ``anchor`` is an optional ``(row, col)`` 1-based screen cell the menu
        drops below; with ``anchor`` the dialog is anchor-placed, else
        centered. Unlike the blocking :meth:`menu`, ``anchor`` is NOT
        defaulted to the list cursor's cell — that derivation reads live
        geometry on the main thread and is meaningless from a worker. An empty
        ``items`` fires ``on_result(None)`` WITHOUT opening (once, main thread,
        caught). ``on_result`` receives the chosen item's VALUE or ``None``.
        Returns ``None``.
        """
        items = list(items)
        if not items:
            self._deliver_none(on_result)
            return
        content = ListContent(items, filter=False)
        placement = 'anchor' if anchor is not None else 'center'
        self._browser.open_dialog_async(content, on_result=on_result,
                                        placement=placement, anchor=anchor)

    def input_async(self, prompt: str, *, default: str = '',
                    on_result: Optional[Callable[[Any], None]] = None) -> None:
        """(thread-safe) Async :meth:`input` — open from any thread.

        Builds the same :class:`InputContent` (``prompt`` above a field
        pre-filled with ``default``) as :meth:`input` and opens it centered.
        ``on_result`` receives the entered string (possibly empty), or ``None``
        on cancel / any no-answer path. Returns ``None`` at once.
        """
        content = InputContent(prompt, default=default)
        self._browser.open_dialog_async(content, on_result=on_result,
                                        placement='center')

    def _deliver_none(self, on_result) -> None:
        """Fire ``on_result(None)`` on the main thread, once, caught.

        The empty-``pick``/``menu`` short-circuit: deliver ``None`` the same
        way :meth:`Browser.open_dialog_async` does, WITHOUT opening a dialog.
        Headless fires inline (no loop to post into); otherwise the firing is
        posted as a closure over the engine's one caught callback site
        (:meth:`Browser._fire_dialog_cb`) so it lands on the main thread and a
        throwing callback can't escape the drain — never as a bare posted
        ``on_result`` closure (the drain runs queued callables uncaught).
        Safe to call from any thread (``post`` is thread-safe).
        """
        b = self._browser
        if on_result is None:
            return
        if b._headless:
            b._fire_dialog_cb(on_result, None)
        else:
            b.post(lambda: b._fire_dialog_cb(on_result, None))


# ---- modal helpers --------------------------------------------------------
#
# Geometry derivation for ``ctx.menu``. The info-bar / preview-pane prompt
# loops (``_draw_info_prompt``, ``_read_line_on_info_bar``,
# ``_confirm_on_info_bar``, ``_pick_on_info_bar``, ``_info_bar_geometry``)
# that used to live here are gone — those sub-flows are modal dialogs now
# (see ``055-modal.py`` and the ``ctx`` methods above).


def _list_pane_rect(browser):
    """The list pane's :class:`Rect` in the current layout, or ``None``.

    Re-runs :func:`layout_panes` against the live terminal size and split /
    preview / ratio state — the single spot both :func:`_list_cursor_cell` (the
    keyboard anchor) and :func:`_list_pane_bounds` (the #1051 horizontal clamp
    span) read the list pane from. Returns ``None`` (no list pane, or a
    zero-area one) so callers fall back to their full-screen default.
    """
    cols, rows = term_size()
    layout = layout_panes(cols, rows,
                          split=getattr(browser, 'split', 'h'),
                          show_preview=browser.show_preview,
                          list_ratio=browser.list_ratio)
    list_rect = layout.get('list')
    if list_rect is None or list_rect.height <= 0 or list_rect.width <= 0:
        return None
    return list_rect


def _list_pane_bounds(browser):
    """List pane's inclusive 1-based column span ``(L, R)``, or ``None``.

    The horizontal bound an anchored ``ctx.menu`` clamps its footprint within
    (#1051): ``L`` is the pane's first column (``rect.left``) and ``R`` its last
    (``rect.right - 1`` — ``Rect.right`` is exclusive). ``None`` when there's no
    resolvable list pane (or it's zero-area), so ``ctx.menu`` passes no bound
    and the menu keeps the full-screen #1040 centering — the safe headless
    default.
    """
    list_rect = _list_pane_rect(browser)
    if list_rect is None:
        return None
    return (list_rect.left, list_rect.right - 1)


def _list_cursor_cell(browser):
    """Screen cell ``(row, col)`` of the list cursor, or ``None``.

    ``ctx.menu`` drops a context menu just below the active list row when no
    explicit anchor is given, so it reads as attached to the cursor. This
    re-derives that cell the same way ``render_list`` (050-render.py) paints
    it: the list pane's :class:`Rect` from :func:`layout_panes`, then the
    cursor's visible-row index ``state.cursor - browser._list_scroll`` added
    to the pane top, with the column at the pane's left edge. There is no
    header offset — the list pane paints rows starting at ``rect.top``.

    Returns ``None`` (caller falls back to a centered dialog) when the
    geometry can't be derived: no list pane in the current layout, or the
    cursor row scrolled out of the pane's visible span (so an anchor cell
    would point off the list).
    """
    list_rect = _list_pane_rect(browser)
    if list_rect is None:
        return None
    rel = browser._state.cursor - browser._list_scroll
    if not (0 <= rel < list_rect.height):
        # Cursor isn't on screen in the list pane — no sensible anchor.
        return None
    return (list_rect.top + rel, list_rect.left)
