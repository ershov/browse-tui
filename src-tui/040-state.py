"""browse-tui: state layer (visible tree, cursor/scope, async workers, post queue, Pending)."""

import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


class Pending:
    """A handle for an async operation that may chain follow-up callbacks.

    Lifecycle:
      - Constructed: ``done`` is False, ``_chain`` is empty.
      - ``.then(cb)`` before resolve: appends cb to _chain; returns self.
      - ``.then(cb)`` after resolve: invokes cb synchronously; returns self.
      - ``._resolve()``: sets done=True (idempotent), snapshots and clears
        _chain, then runs each snapshotted callback in registration order.
        A callback may register further callbacks via ``.then()`` during
        its execution; because _done is already True, those callbacks fire
        synchronously inside the inner ``.then()`` call rather than being
        queued -- see the snapshot-and-clear pattern below.

    Cancellation: ``Pending.cancel()`` marks the handle cancelled. Chained
    callbacks registered via ``.then()`` will NOT fire after cancel --
    neither callbacks queued before cancel nor those registered after.
    The underlying worker fetch still runs to completion (we never kill
    in-flight subprocesses); cancellation only suppresses chain firing.
    Cancel is non-strict: caches may still be populated by the completing
    worker; only the user-visible chain is suppressed.
    """

    __slots__ = ('_done', '_cancelled', '_chain')

    def __init__(self) -> None:
        self._done = False
        self._cancelled = False
        self._chain: list = []

    @property
    def done(self) -> bool:
        """``True`` once the underlying op has resolved (or been resolved)."""
        return self._done

    @property
    def cancelled(self) -> bool:
        """``True`` once :meth:`cancel` has been called."""
        return self._cancelled

    def cancel(self) -> None:
        """Mark this Pending cancelled.

        Subsequent ``.then()`` calls become no-ops (still return self for
        ergonomic chaining). Any callbacks queued via ``.then()`` before
        cancel are dropped eagerly and will NOT fire when ``_resolve``
        eventually runs. Idempotent: calling ``cancel()`` on an already-
        cancelled or already-resolved Pending is harmless and does not
        undo any callbacks that already fired.
        """
        self._cancelled = True
        # Drop queued callbacks eagerly so they cannot leak references.
        self._chain.clear()

    def then(self, callback: Callable[[], None]) -> 'Pending':
        """Append ``callback`` to the resolve chain.

        If the Pending is already resolved, ``callback`` runs synchronously
        inside ``then()``. If cancelled, ``then()`` is a no-op (still
        returns self for ergonomics — chains can be built before or after
        ``cancel`` without branching).

        Callbacks always run on the main thread, after the worker's
        result has been applied to the cache.
        """
        if self._cancelled:
            # Silently no-op. Returning self preserves chain ergonomics:
            # ``p.then(a).then(b)`` keeps working even after cancel.
            return self
        if self._done:
            callback()
        else:
            self._chain.append(callback)
        return self

    def _resolve(self) -> None:
        if self._done:
            return  # idempotent -- second resolve is a no-op
        self._done = True
        if self._cancelled:
            # Worker completed (e.g. cache populated) but the chain was
            # cancelled -- don't fire any callbacks.
            return
        # Snapshot-and-clear before iterating: a callback that registers
        # further callbacks via .then() during iteration will see _done=True
        # and have its callback fire synchronously inside .then(), bypassing
        # this loop. Without the clear, those would also accumulate in the
        # iterated list and fire here, breaking the documented order.
        chain, self._chain = self._chain, []
        for cb in chain:
            cb()


# A single rendered row produced by ``visible_items``.
@dataclass
class VisibleEntry:
    """One entry in the rendered visible list.

    Fields:
      - ``item``: the Item being shown (or a synthetic placeholder).
      - ``depth``: 0-based depth relative to the current scope root.
      - ``kind``: one of:
          * ``'normal'`` — an ordinary tree row.
          * ``'scope_root'`` — the scope item itself, shown at depth 0
            when ``scope_stack`` is non-empty (mirrors plan-tui showing
            the scope ticket as the first row).
          * ``'pending'`` — synthetic ``loading…`` placeholder under an
            expanded parent whose children haven't loaded yet.
    """

    item: Any
    depth: int
    kind: str = 'normal'


@dataclass
class State:
    """Container for the state that ``visible_items`` reads.

    Lives on Browser; factored as a separate dataclass so unit tests can
    construct one directly without spinning up a full Browser. Threading
    concerns (workers, post-queue) are out of scope here — see ticket #7.
    """

    root_id: Any = None
    scope_stack: list = field(default_factory=list)
    expanded: set = field(default_factory=set)
    _expanded_by_scope: dict = field(default_factory=dict)
    _children: dict = field(default_factory=dict)
    _children_pending: set = field(default_factory=set)
    _visible_dirty: bool = True
    _visible_cache: list = field(default_factory=list)
    # Cursor index into the visible-tree list, and the user-selected ids
    # (rendered with a ``*`` marker by the renderer in ticket #10). Both
    # live on State so unit tests can construct one without spinning up
    # a Browser, and so the visible-tree builder can read ``selected``
    # later when it wires marker columns.
    cursor: int = 0
    selected: set = field(default_factory=set)


# ---- scope management ----------------------------------------------------


def current_scope(state: State) -> Any:
    """Return the id of the current scope root."""
    return state.scope_stack[-1] if state.scope_stack else state.root_id


def scope_into(state: State, item_id) -> None:
    """Push the current expanded set under its scope key, switch scope.

    The new scope's expanded set is restored from ``_expanded_by_scope``
    (empty if first visit). Marks the visible-tree dirty.
    """
    # Memoise the expanded set under the scope we're leaving.
    state._expanded_by_scope[current_scope(state)] = state.expanded
    state.scope_stack.append(item_id)
    # Restore (or default to empty) the expanded set for the new scope.
    state.expanded = state._expanded_by_scope.get(item_id, set())
    state._visible_dirty = True


def scope_out(state: State) -> Any:
    """Pop the top of ``scope_stack``, restoring the previous expanded set.

    Returns the id we were scoped *to* before popping (so the caller can
    move the cursor onto it). Returns None if the stack is already empty.
    """
    if not state.scope_stack:
        return None
    # Memoise the scope we're leaving.
    leaving = state.scope_stack.pop()
    state._expanded_by_scope[leaving] = state.expanded
    # Restore the previous scope's expanded set.
    state.expanded = state._expanded_by_scope.get(current_scope(state), set())
    state._visible_dirty = True
    return leaving


# ---- cache invalidation --------------------------------------------------


def mark_visible_dirty(state: State) -> None:
    """Flip the visible-tree dirty bit. Next ``visible_items`` rebuilds."""
    state._visible_dirty = True


def cache_invalidate_subtree(state: State, item_id) -> None:
    """Drop one parent's children entry and mark the visible-tree dirty.

    Safe to call for a never-cached id — the missing key is simply ignored.
    """
    state._children.pop(item_id, None)
    state._visible_dirty = True


def cache_invalidate_all(state: State) -> None:
    """Clear the entire children cache and mark the visible-tree dirty."""
    state._children.clear()
    state._visible_dirty = True


# ---- placeholder for pending state ---------------------------------------

# Module-level sentinel — the placeholder rows reuse a single id so callers
# never confuse it with a user-supplied id (``object()`` instances are only
# equal to themselves).
_PENDING_PLACEHOLDER_ID = object()


def _make_pending_placeholder():
    """Build a synthetic Item for the 'loading…' row.

    ``Item`` is resolved from this module's globals — production builds
    concatenate ``030-data.py`` ahead of this file so the name is bound
    naturally; the test harness injects ``_state.Item = _data.Item``
    after loading the module independently.
    """
    return Item(id=_PENDING_PLACEHOLDER_ID, title='⧗ loading…')


# ---- visible-tree builder ------------------------------------------------


def visible_items(state: State) -> list:
    """Return the flat list of ``VisibleEntry`` rows currently visible.

    Caches the result in ``state._visible_cache``; rebuilds when
    ``state._visible_dirty`` is True. The returned list is the cached one
    (identity-stable across repeated calls until a dirty mark forces a
    rebuild).

    Build:
      1. Determine scope via ``current_scope``.
      2. If ``scope_stack`` is non-empty, emit the scope item itself at
         depth 0 (kind ``'scope_root'``); subsequent children start at
         depth 1.
      3. Iterative DFS over ``_children``, honouring ``expanded``:
         - Cached non-empty list: emit each child; recurse into expanded
           parents with ``has_children``.
         - Cached empty list: emit nothing under this parent.
         - Not in cache + parent expanded: emit a single ``'pending'``
           placeholder row (whether or not the id is in
           ``_children_pending`` — the renderer kicks the worker on the
           next tick; ``visible_items`` only observes).
         - Not in cache + parent not expanded: skip (nothing to render).
    """
    if not state._visible_dirty:
        return state._visible_cache

    out: list = []

    scope_root_id = current_scope(state)
    if state.scope_stack:
        # Try to recover the actual Item for the scope row by scanning the
        # cache; fall back to a synthetic Item if not findable.
        scope_item = _find_item(state, scope_root_id)
        if scope_item is None:
            scope_item = Item(id=scope_root_id, title=str(scope_root_id))
        out.append(VisibleEntry(item=scope_item, depth=0, kind='scope_root'))
        base_depth = 1
    else:
        base_depth = 0

    # DFS using an explicit stack of child rows still to expand into. We
    # push children onto a worklist in reverse so popping yields them in
    # original insertion order.
    children = state._children.get(scope_root_id)
    if children is None:
        # Scope root not cached. If at root, this means we've never loaded
        # anything — render nothing. (At a nested scope, a placeholder
        # under the scope_root would be reasonable, but for now we mirror
        # plan-tui's behaviour and render an empty content area; the
        # worker will populate the cache.)
        if state.scope_stack and scope_root_id in state.expanded:
            out.append(VisibleEntry(
                item=_make_pending_placeholder(),
                depth=base_depth,
                kind='pending',
            ))
    else:
        _emit_children(state, children, base_depth, out)

    state._visible_cache = out
    state._visible_dirty = False
    return out


def _emit_children(state, children, depth, out):
    """Emit ``children`` at ``depth``, recursing into expanded items.

    Iterative DFS — uses an explicit worklist. Each frame is
    ``(siblings_iter, depth)``; we push deeper frames as we descend.
    """
    # Worklist holds iterators of (item, depth) pairs to consume. We use
    # iter() so resuming a parent frame after recursing into a child is
    # simply a matter of continuing the outer iterator on the next loop
    # turn.
    stack = [(iter([(c, depth) for c in children]),)]
    while stack:
        (siblings,) = stack[-1]
        try:
            child, d = next(siblings)
        except StopIteration:
            stack.pop()
            continue
        out.append(VisibleEntry(item=child, depth=d, kind='normal'))
        if not child.has_children or child.id not in state.expanded:
            continue
        sub = state._children.get(child.id)
        if sub is None:
            # Expanded but not cached — placeholder row; do not recurse.
            out.append(VisibleEntry(
                item=_make_pending_placeholder(),
                depth=d + 1,
                kind='pending',
            ))
            continue
        if not sub:
            # Cached empty list — nothing to recurse into.
            continue
        stack.append((iter([(c, d + 1) for c in sub]),))


def _find_item(state, item_id):
    """Look up an Item by id by scanning every cached children list.

    Used when scope_into needs to render the scope item itself — we rely
    on the user having seen it as a child somewhere. Returns ``None`` if
    not findable; the caller then synthesises a fallback Item.
    """
    for children in state._children.values():
        for child in children:
            if child.id == item_id:
                return child
    return None


# ---- search helpers (ticket #22) ----------------------------------------
#
# Mirrors plan-tui's ``_search_text`` / ``_search_matches`` / ``_search_next``
# / ``_search_jump_nearest`` (see plan-source/src-tui/070-main.py:6-46).
# These live in the state layer rather than the action layer so the
# renderer (050-render.py) can call ``_search_text`` / ``_search_matches``
# while highlighting non-cursor rows — both the renderer and the
# action-mode key dispatcher need the same matcher, and putting it next
# to ``visible_items`` keeps state-driven helpers in one module.


def _search_text(item):
    """Return the searchable haystack for an Item.

    Includes the id and (when present) the bracketed tag so a query like
    ``open`` matches ``#5 [open] foo`` even when ``open`` is the tag and
    not a substring of the title. Mirrors plan-tui's ``_search_text``
    (which embeds the status string the user sees on screen).
    """
    parts = [str(item.id)]
    if item.title:
        parts.append(item.title)
    if item.tag:
        parts.append('[{}]'.format(item.tag))
    return ' '.join(parts)


def _search_matches(text, query):
    """Fragment-AND match: every space-separated piece of ``query`` in ``text``.

    Case-insensitive. Empty query (or whitespace-only) does not match.
    Mirrors plan-tui's ``_search_matches`` logic with the search query as
    a parameter rather than a module global.
    """
    if not query:
        return False
    fragments = query.lower().split()
    if not fragments:
        return False
    low = text.lower()
    return all(f in low for f in fragments)


def _search_find(state, query, start_idx, direction=1):
    """Find the next/prev visible item matching ``query``.

    Walks the visible list starting from ``start_idx`` in ``direction``
    (1 forward, -1 backward), wrapping around. Skips non-``'normal'``
    entries (placeholders / scope_root) so search never lands on a
    synthetic row. Returns the visible-list index of the match, or
    ``None`` if no match exists.
    """
    vis = visible_items(state)
    if not vis or not query:
        return None
    n = len(vis)
    for step in range(1, n + 1):
        idx = (start_idx + step * direction) % n
        entry = vis[idx]
        if entry.kind == 'normal' and _search_matches(
                _search_text(entry.item), query):
            return idx
    return None


def _search_jump_nearest(browser):
    """Jump cursor to the nearest match (forward search from current pos).

    Used by the search-mode key dispatcher: each keystroke that mutates
    the query nudges the cursor onto the first match at-or-after the
    current cursor (passing ``cursor - 1`` so the cursor *itself* can
    match, mirroring plan-tui).
    """
    state = browser._state
    idx = _search_find(state, browser._search_query, state.cursor - 1, 1)
    if idx is not None:
        state.cursor = idx
        browser._needs_redraw.add('list')
        browser._needs_redraw.add('preview')
        browser._needs_redraw.add('children')


# ---- insert-mode placement helpers (ticket #21) -------------------------
#
# These mirror plan-tui's ``_auto_insert_depth`` and ``_resolve_insert``
# (see plan-source/src-tui/070-main.py:50-96). Pure functions: take a
# position + depth + visible list, return the resolved depth or
# ``(relation, dest_id)`` describing how to place the new item.
#
# Difference from plan-tui: we don't carry a ``parent`` field on
# ``VisibleEntry`` (browse-tui's lazy children cache doesn't track
# parent ids). To find an outdent ancestor we walk the visible list
# itself — by tree-DFS construction, the parent of any row at depth
# ``d`` is the most recent earlier row at depth ``d - 1`` (or smaller).


def auto_insert_depth(pos, vis):
    """Compute the natural depth for an insertion marker at gap position pos.

    Mirrors plan-tui's ``_auto_insert_depth``. ``pos`` is a *gap* in the
    visible list: gap 0 sits above the first row, gap ``len(vis)`` sits
    below the last row. The auto-depth is:

    * ``vis[0].depth`` (or 0) when the list is empty / pos at top.
    * The *below* row's depth when it's deeper than the *above* row —
      i.e. the marker "lands inside" the parent's subtree by default.
    * Otherwise the *above* row's depth — sibling-after.
    """
    if not vis:
        return 0
    if pos <= 0:
        return vis[0].depth if vis else 0
    above = vis[pos - 1]
    if pos < len(vis):
        below = vis[pos]
        if below.depth > above.depth:
            return below.depth
    return above.depth


def resolve_insert(pos, depth, vis, *, scope_root_id=None):
    """Convert (insert_pos, insert_depth) into ``(relation, dest_id)``.

    Mirrors plan-tui's ``_resolve_insert``. Returns ``(None, None)`` if
    the position is invalid.

    ``relation`` is one of ``'before'``, ``'after'``, ``'first'``;
    ``dest_id`` is the item id the relation references.

    plan-tui has an explicit ``parent`` field on every ticket and uses
    an id_map of all_tickets to walk up ancestors. browse-tui's lazy
    model doesn't carry ``parent`` on ``VisibleEntry``, so we walk the
    visible list itself to find the ancestor at the target depth — by
    tree-DFS construction the most-recent earlier row at depth ``d`` is
    the unique ancestor at that depth.
    """
    if not vis or pos <= 0:
        return (None, None)

    above = vis[pos - 1]

    # Skip the synthetic scope_root row — it isn't a valid insertion
    # reference. Mirror plan-tui's "id == 0" branch: if the row directly
    # above the gap is the scope root, fall through to a "before vis[pos]"
    # placement (or give up if there's no row below either).
    if above.kind == 'scope_root':
        if pos < len(vis):
            return ('before', vis[pos].item.id)
        return (None, None)

    if depth > above.depth:
        # Inserting as child of above.
        return ('first', above.item.id)
    if depth == above.depth:
        # Inserting as sibling after above.
        return ('after', above.item.id)

    # depth < above.depth — outdented. Walk back through ``vis`` until
    # we hit a row at the target depth; that's the ancestor we want to
    # become a sibling of (relation 'after').
    i = pos - 1
    while i >= 0 and vis[i].depth > depth:
        i -= 1
    if i >= 0 and vis[i].depth == depth and vis[i].kind != 'scope_root':
        return ('after', vis[i].item.id)
    return (None, None)


# ---- Browser engine ------------------------------------------------------

# Phase 1 (ticket #7) implements the threading core: workers, post queue,
# Pending registry, headless test mode. Subsequent tickets fill in the rest:
#   #8  — thread-safe public API (cursor_to, expand, select, watch),
#         from_flat_tree.
#   #9  — terminal layer + main loop wiring (real notify_wake self-pipe).
#   #10 — renderer (the headless flag becomes meaningful).
#   #11 — Context-vs-Browser surface split for actions.
#   #12 — actions / keymap.
#   #13 — CLI parsing + entry-point glue.


# Reasonable bounds for ``list_ratio``. The lower bound is 1/200 (≈0.5%)
# and upper bound 199/200 — outside this range the layout produces
# degenerate panes (zero list or zero preview) regardless of terminal
# size, which the user can't recover from with hotkey nudges. The
# layout itself enforces a minimum-1 list and minimum-2 preview when
# the terminal is large enough; this clamp is just a guardrail against
# bad CLI input.
_LIST_RATIO_MIN = 0.005
_LIST_RATIO_MAX = 0.995


def _clamp_list_ratio(r: float) -> float:
    """Pin ``r`` into the valid ratio range. NaN / non-numeric → default."""
    try:
        f = float(r)
    except (TypeError, ValueError):
        return 0.30
    if f != f:  # NaN
        return 0.30
    if f < _LIST_RATIO_MIN:
        return _LIST_RATIO_MIN
    if f > _LIST_RATIO_MAX:
        return _LIST_RATIO_MAX
    return f


# Valid split-layout codes recognised by ``layout_panes`` (050-render.py).
# Mirrors the four families described there:
#   'h'  — horizontal stack (list / children / preview, info bar in
#          the topmost active separator). Historic default.
#   'v'  — vertical side-by-side (list | children | preview).
#   'm'  — mixed: list+children stacked left, preview right.
#   'pc' — list left, children+preview stacked right.
# CLI parsing also accepts ``'a'`` (auto) but resolves it to one of the
# above before reaching Browser, so the runtime state is always one of
# these four.
_VALID_SPLITS = ('v', 'h', 'm', 'pc')


def _clamp_split(s) -> str:
    """Validate ``s`` against ``_VALID_SPLITS``. Unknown / non-string → 'h'.

    Resolves ``'auto'`` / ``'a'`` against the live terminal width via
    ``term_size``: ``>=230`` cols → ``'v'``, else ``'h'``. This makes
    Python recipes (which construct ``Browser`` directly without going
    through the CLI's ``_resolve_split_type``) get the same auto
    behaviour as ``--split-type=auto``.
    """
    if not isinstance(s, str):
        return 'h'
    if s in _VALID_SPLITS:
        return s
    if s.lower() in ('a', 'auto'):
        ts = globals().get('term_size')
        if ts is not None:
            try:
                cols, _ = ts()
                return 'v' if cols >= 230 else 'h'
            except Exception:
                pass
        return 'h'
    return 'h'


class Browser:
    """The TUI engine and async coordinator.

    Holds the data caches (``_state``), the cross-thread post queue
    (``_main_queue``), the children FIFO worker, and the latest-wins
    preview worker. All public mutation goes through ``post(fn)`` so
    background threads (workers, watchers, signal handlers) are safe to
    schedule work without taking locks -- the main thread drains the
    queue on every wake.

    Construction kwargs (full spec'd surface):
      title:              window title (renderer in #10).
      get_children:       (parent_id) -> Iterable[Item|str|tuple|dict].
      get_preview:        (item_id) -> str | None.
      actions:            list of Action objects (Action lands in #11;
                          phase 1 stores the list opaquely).
      on_enter:           default-action handler; #13 wires fall-back
                          print+exit when None.
      format_item:        (item, ctx) -> [(text, fg, bold), …]; renderer
                          consumes in #10.
      root_id:            Any (default None).
      initial_scope:      if set, pushed onto scope_stack at construction.
      show_preview:       enable the preview pane (renderer in #10).
      show_children_pane: enable the right-hand children-as-list pane.
      multi_select:       allow multi-selection (action layer in #12).
      print_format:       output format string used when on_enter is None
                          and the user picks the default action.
      help_intro:         optional prose shown at the top of the help
                          screen (and ``--help``); recipes use it to
                          describe what the tool does. ``None`` elides
                          the section.
      help_outro:         optional prose shown at the bottom of the help
                          screen (and ``--help``); good for examples or
                          links. ``None`` elides the section.
      show_ids:           one of 'always' / 'auto' (default) / 'never';
                          controls whether the per-row id segment is
                          shown. 'auto' suppresses id when it equals the
                          title.
      _headless:          skip terminal init (default False) -- observable
                          here for tests; the real terminal init/teardown
                          branches on it once #9 lands.
    """

    def __init__(self, *,
                 title: str = 'browse-tui',
                 get_children: Optional[Callable[[Any], Any]] = None,
                 get_preview: Optional[Callable[[Any], Optional[str]]] = None,
                 actions: Optional[list] = None,
                 on_enter: Any = None,
                 format_item: Optional[Callable] = None,
                 root_id: Any = None,
                 initial_scope: Any = None,
                 show_preview: bool = True,
                 show_children_pane: bool = True,
                 list_ratio: float = 0.30,
                 split: str = 'auto',
                 multi_select: bool = True,
                 print_format: str = '{id}',
                 help_intro: Optional[str] = None,
                 help_outro: Optional[str] = None,
                 show_ids: str = 'auto',
                 _headless: bool = False) -> None:
        """Construct a Browser.

        All keyword arguments are optional; sensible defaults yield a
        Browser that displays nothing (empty ``get_children``) but still
        boots cleanly for tests and smoke checks.

        Args:
            title: Window title shown in the header bar.
            get_children: ``(parent_id) -> Iterable[Item|str|tuple|dict]``
                Called per parent-being-expanded on a worker thread.
                Returned values are coerced via :func:`to_item`. Errors
                raised here are caught at the worker boundary: the
                parent's children become ``[]``, the error is surfaced
                via the info bar, and any ``Pending`` waiting on the
                fetch still resolves.
            get_preview: ``(item_id) -> str | None`` Optional preview
                callback; runs on a worker thread with latest-wins
                coalescing. ``None`` from the callback is treated as
                ``''``. Errors are rendered as
                ``[error] ExceptionName: message``.
            actions: List of :class:`Action` keybindings registered at
                construction. User-supplied actions override defaults
                bound to the same key.
            on_enter: What pressing ``Enter`` (outside search mode) does.
                ``None`` / ``'print-exit'`` formats ``ctx.targets`` via
                ``print_format`` and exits 0; ``'action:KEY'`` runs the
                bound action; ``'noop'`` does nothing; a callable is
                invoked with the Context.
            format_item: Optional ``(item, ctx) -> [(text, fg, bold)…]``
                hook overriding the default per-row layout.
            root_id: Initial id passed to the first ``get_children``
                call. ``None`` is the default.
            initial_scope: If set, pushed onto ``scope_stack`` at
                construction so the UI starts inside that scope.
            show_preview: Whether the preview pane starts visible.
            show_children_pane: Whether the children-grid pane starts
                visible.
            multi_select: Whether multi-selection is enabled. Phase 1
                stores this opaquely; the action layer reads it.
            print_format: ``str.format``-style template applied to each
                target when ``on_enter`` resolves to print-exit.
            help_intro: Optional prose shown at the top of ``--help``
                and the in-app help screen (``?``). Recipes use it to
                describe what their tool does.
            help_outro: Optional prose shown at the bottom of ``--help``
                and the in-app help screen.
            show_ids: One of ``'always'``, ``'auto'`` (default),
                ``'never'``. Controls whether the per-row id segment is
                rendered before the title. ``'auto'`` suppresses the id
                when ``str(item.id) == item.title`` (the common shape
                for line-based CLI input where showing both is pure
                duplication); ``'always'`` forces it; ``'never'`` hides
                it. Recipes can pin this value at construction; the CLI
                exposes ``--show-ids``.
            _headless: Skip terminal init/teardown — used by tests.
        """
        if show_ids not in ('always', 'auto', 'never'):
            raise ValueError(
                "show_ids must be one of 'always', 'auto', 'never'; "
                f"got {show_ids!r}"
            )
        # --- user-supplied data callbacks -------------------------------
        # Default get_children to "no children" so a Browser constructed
        # with no kwargs still works (tests, smoke checks). get_preview
        # stays None -- the preview worker treats None as "always returns
        # ''" rather than calling a no-op lambda needlessly.
        self.title = title
        self.get_children = get_children or (lambda _id: [])
        self.get_preview = get_preview
        # actions/on_enter/format_item are stored opaquely in phase 1;
        # tickets #11 (Context) and #12 (action keymap) read them.
        self.actions = list(actions) if actions is not None else []
        self.on_enter = on_enter
        self.format_item = format_item
        self.show_preview = show_preview
        self.show_children_pane = show_children_pane
        # Fraction of total terminal rows allocated to the list pane.
        # Stored as a float so it survives terminal resizes without
        # rounding drift; clamped to a usable range by ``set_list_ratio``.
        # The ratio covers list / (list + children-grid + preview) per
        # the model: children pane stays content-driven, preview gets
        # the remainder.
        self.list_ratio = _clamp_list_ratio(list_ratio)
        # Split-layout selector — controls which family of pane geometries
        # ``layout_panes`` produces. Default ``'auto'`` resolves at
        # construction time via ``_clamp_split`` (vertical at >=230 cols,
        # else horizontal) so Python recipes that construct Browser
        # directly get the same auto behaviour as ``--split-type=auto``.
        self.split = _clamp_split(split)
        self.multi_select = multi_select
        self.print_format = print_format
        # help_intro/help_outro are prose blurbs shown above/below the
        # auto-generated key list in --help and the in-app help screen
        # (?). Recipes set them to explain what their tool does;
        # ``None`` (the default) elides the corresponding section.
        self.help_intro = help_intro
        self.help_outro = help_outro
        self.show_ids = show_ids
        self._headless = _headless

        # --- domain state ------------------------------------------------
        # State stays a separate dataclass so unit tests can poke it
        # without spinning up a Browser. The preview cache lives on State
        # alongside _children for cohesion (one place to invalidate
        # everything per item id).
        self._state = State(root_id=root_id)
        self._state._preview = {}  # item_id -> preview text
        # Apply ``initial_scope`` after State is built so scope_into can
        # do its bookkeeping (saving the empty pre-scope expanded set
        # under the prior scope key).
        if initial_scope is not None:
            scope_into(self._state, initial_scope)

        # --- cross-thread plumbing --------------------------------------
        # main_queue: any thread -> main thread. Drained by drain_main_queue
        # on every wake. queue.Queue is thread-safe and cheap; we don't
        # need its blocking semantics, just FIFO + safe puts.
        self._main_queue = queue.Queue()

        # children worker: FIFO of parent ids to fetch. The worker pops
        # from the left, fetches, appends to _children_results. Both
        # deques are safe for single-producer / single-consumer use under
        # the GIL (deque ops are individually atomic).
        self._children_queue = deque()
        self._children_in_flight = {}     # id -> list[Pending] awaiting this fetch
        self._children_results = deque()  # FIFO of (id, items, error_or_none)
        self._children_event = threading.Event()

        # preview worker: single-slot latest-wins. The worker reads
        # _preview_req atomically, fetches, writes _preview_result, then
        # checks if _preview_req is still the same id. If not, loops to
        # serve the newer request. See _preview_worker for the snippet
        # we ported verbatim from plan-tui.
        self._preview_req = None
        self._preview_result = None  # (id, text)
        self._preview_event = threading.Event()

        # --- worker lifecycle bookkeeping --------------------------------
        self._stop = False
        self._workers_running = False
        self._children_thread = None
        self._preview_thread = None

        # --- surfaced state for the renderer (filled in by ticket #10) ---
        self._error_text = ''
        self._message_text = ''

        # --- render-layer bookkeeping (ticket #10) ----------------------
        # Selective-redraw flag set; values are strings: 'list', 'preview',
        # 'info', 'all'. ``render_full`` clears it; ``render_partial`` reads
        # it and clears as it goes. The render layer treats an empty set
        # as "nothing to do".
        self._needs_redraw = set()
        # Search state — phase 1 stores the strings; key handlers in #11
        # set them. The renderer reads ``_search_query`` for highlight
        # spans and ``_search_mode`` for the search prompt in the info
        # bar. Phase-2 ticket #22 wires the actual highlight pass.
        self._search_query = ''
        self._search_mode = False
        # Preview pane scroll offset (lines from top of preview content).
        # Reset whenever the preview content changes; nudged by the
        # shift-up/shift-down handlers in the action layer (#12).
        self._preview_scroll = 0
        # Help-mode toggle — when True, the preview pane shows the
        # composed help text (``compose_help_text(self)`` from the
        # render layer) instead of the per-item preview. The handler
        # lives in the action layer (#12); the renderer just observes
        # the flag.
        self._help_mode = False
        # Last cursor item id we drove the preview pane to. Set by
        # _update_preview_for_cursor; when the cursor lands on a
        # different (or no) item we treat that as a navigation event
        # and reset the preview pane: scroll back to the top and
        # dismiss the help overlay so the user sees the new item's
        # preview, not stale state from the previous one.
        self._preview_cursor_id = None
        # List-pane scroll offset (rows from top of the visible list).
        # Maintained by render_list to keep the cursor on-screen; lives
        # on Browser so partial redraws remember it across calls.
        self._list_scroll = 0

        # --- quit bookkeeping (read by the main loop in #13) ------------
        # quit() flips _quit_requested; the main loop watches the flag
        # and exits with _quit_code, printing _quit_output if non-empty.
        self._quit_requested = False
        self._quit_code = 0
        self._quit_output = ''

        # --- cursor_to deferred resolution -----------------------------
        # cursor_to(id) for an id not currently visible parks the request
        # here; whenever a future cache delivery rebuilds the visible
        # list, ``apply_children_results`` retries the placement and
        # resolves the parked Pending if the id appeared. Phase 1
        # acceptable simplification: we only retry once per delivery,
        # we don't walk the parent chain to expand ancestors (that would
        # need parent metadata on items). Production recipes that use
        # from_flat_tree() get the cache populated up front so cursor_to
        # always finds the id immediately.
        self._pending_cursor = None  # tuple (id, Pending) or None

        # --- insert-mode bookkeeping (ticket #21) ----------------------
        # Insert mode is entered by ``ctx.insert(label, on_confirm)``.
        # The user moves a placement marker through the visible tree and
        # confirms a position; on confirm, ``_insert_callback`` is invoked
        # with ``(relation, dest_id)`` describing how to place the new
        # item. While ``_insert_mode`` is True the main loop routes keys
        # through ``_handle_insert_key`` instead of the regular dispatch.
        #
        # ``_insert_pos`` is a *gap* position in the visible list: 1
        # means "insert before the first row after the scope_root",
        # ``len(vis)`` means "insert at the very end". ``_insert_depth``
        # is the indentation level for the placement marker (controlled
        # by the user via right/left).
        self._insert_mode = False
        self._insert_pos = 0
        self._insert_depth = 0
        self._insert_callback = None
        self._insert_label = ''

    # ---- action registration -------------------------------------------

    def add_action(self, action: 'Action') -> None:
        """Register a custom Action.

        If an existing entry (built-in or earlier custom) binds the
        same key, that entry is replaced — recipes can override one
        default keybinding without rebuilding the full list. Not
        thread-safe by design: recipes call this during construction
        before ``start_workers`` / the main loop start.
        """
        # Replace any existing entry for the same key, then append.
        self.actions = [a for a in self.actions if a.key != action.key]
        self.actions.append(action)

    # ---- public, thread-safe API ---------------------------------------

    def refresh(self, id: Any = None,
                on_complete: Optional[Callable[[], None]] = None) -> 'Pending':
        """Schedule a refetch of one parent's children (or the full root).

        Returns a Pending that resolves on the main thread once the worker
        has delivered the new children list. Safe to call from any thread
        -- the actual cache invalidation runs on the main thread (in
        ``_do_refresh``) so visible-tree state stays consistent.

        ``id=None`` invalidates the entire cache and refetches the root.
        ``on_complete`` is wired via ``.then`` so callers may chain in
        either style.
        """
        pending = Pending()
        if on_complete is not None:
            pending.then(on_complete)
        self.post(lambda: self._do_refresh(id, pending))
        return pending

    def post(self, fn: Callable[[], None]) -> None:
        """Schedule ``fn`` to run on the main thread on the next drain.

        The callable runs with no arguments and its return value is
        ignored. Exceptions inside ``fn`` propagate to the drain loop --
        callers should catch their own exceptions if they want to keep
        the drain going. (We may revisit and wrap in try/except once the
        renderer can surface a status line.)
        """
        self._main_queue.put(fn)
        notify_wake()

    def message(self, text: str) -> None:
        """Surface ``text`` as a transient status message.

        Stored on Browser; the renderer in ticket #10 picks it up. Safe
        to call from any thread (uses ``post`` under the hood so the
        write happens on the main thread).
        """
        self.post(lambda: setattr(self, '_message_text', text))

    def error(self, text: str) -> None:
        """Surface ``text`` as an error message. Same lane as ``message``."""
        self.post(lambda: setattr(self, '_error_text', text))

    @property
    def error_text(self) -> str:
        """Most recent error message surfaced via :meth:`error`."""
        return self._error_text

    @property
    def message_text(self) -> str:
        """Most recent transient status message surfaced via :meth:`message`."""
        return self._message_text

    def cursor_to(self, id: Any,
                  on_complete: Optional[Callable[[], None]] = None) -> 'Pending':
        """Move cursor to the item with the given id, expanding ancestors as needed.

        Asynchronous because we may need to fetch ancestor children. The
        returned Pending resolves once the cursor is positioned -- after
        all required fetches complete, or on the next drain if the id is
        already visible. For not-yet-visible ids, phase 1 best-effort:
        the Pending resolves with the cursor unmoved (full ancestor walk
        is phase-2 territory, requires parent metadata on items).
        """
        pending = Pending()
        if on_complete is not None:
            pending.then(on_complete)
        self.post(lambda: self._do_cursor_to(id, pending))
        return pending

    def expand(self, id: Any,
               on_complete: Optional[Callable[[], None]] = None) -> 'Pending':
        """Add ``id`` to expanded; trigger fetch if not cached.

        Pending resolves when children are cached (or immediately on the
        next drain if already cached). Safe to call from any thread.
        """
        pending = Pending()
        if on_complete is not None:
            pending.then(on_complete)
        self.post(lambda: self._do_expand(id, pending))
        return pending

    def cancel(self, *pendings: 'Pending') -> None:
        """Mark one or more Pendings cancelled (sugar for ``p.cancel()``).

        Idempotent on already-cancelled or already-resolved Pendings.
        Worker fetches are not killed -- cancellation is non-strict and
        only suppresses chained ``.then()`` callbacks from firing. Useful
        when the user has moved on and a stale chain (e.g. cursor-to a
        no-longer-relevant id) should not fire.
        """
        for p in pendings:
            p.cancel()

    def set_list_ratio(self, ratio: float) -> None:
        """Set the list pane's share of total terminal rows (clamped).

        The clamp range is ``[_LIST_RATIO_MIN, _LIST_RATIO_MAX]`` —
        outside that range the layout produces degenerate panes and
        the user can't recover with hotkey nudges. The layout
        independently enforces a minimum-1 list / minimum-2 preview
        when the terminal has room; this method's clamp is a sanity
        guardrail, not the live floor.
        """
        self.list_ratio = _clamp_list_ratio(ratio)
        self._needs_redraw.add('all')

    def set_split(self, s: str) -> None:
        """Set the split-layout selector (clamped to ``_VALID_SPLITS``).

        Invalid values (unknown codes, non-strings, ``None``) fall back
        to the historic default ``'h'``. Mirrors ``set_list_ratio``: the
        clamp is a guardrail, not a live floor — the layout helpers in
        050-render produce sane geometries even at degenerate sizes.
        Marks the full screen for redraw so the next render pass picks
        up the new layout family.
        """
        self.split = _clamp_split(s)
        self._needs_redraw.add('all')

    def select(self, ids, replace: bool = False) -> None:
        """Add ``ids`` to ``selected`` (or replace existing selection if ``replace``).

        Thread-safe; the actual mutation runs on the main thread so the
        renderer never sees a torn set. Phase 1 stores the ids verbatim;
        the renderer in #10 reads the set when emitting ``*`` markers.
        """
        # Snapshot the iterable on the calling thread so the lambda
        # doesn't capture a mutating live source.
        ids_list = list(ids)
        self.post(lambda: self._do_select(ids_list, replace))

    def quit(self, code: int = 0, output: str = '') -> None:
        """Request the main loop to exit with the given exit code.

        Thread-safe. Phase 1 stores ``_quit_requested``/``_quit_code``/
        ``_quit_output`` on Browser; the main loop in #13 reads these
        and shuts down once the current drain finishes.
        """
        self.post(lambda: self._do_quit(code, output))

    def watch(self, callback: Callable[['Browser'], None],
              interval: Optional[float] = None) -> threading.Thread:
        """Spawn a daemon thread that calls ``callback(self)`` repeatedly.

        If ``interval`` is set, sleep ``interval`` seconds between calls.
        If ``None``, callback is called once and the user is responsible
        for any internal loop. Either way the returned thread is daemon
        so the process exits cleanly.

        Uncaught exceptions in the callback don't crash the process --
        they're surfaced via ``self.error('watcher: ...')``. The watcher
        thread itself dies on the exception (no auto-restart) so authors
        learn about the bug quickly. We deliver the error via ``post``
        rather than writing ``self._error_text`` directly so the message
        lands on the main thread alongside any other errors.
        """
        def _runner():
            try:
                if interval is None:
                    callback(self)
                else:
                    while not self._stop:
                        callback(self)
                        # Use a short-poll sleep so stop_workers can wake
                        # us promptly; one big sleep would block exit by
                        # up to ``interval`` seconds.
                        end = time.monotonic() + interval
                        while time.monotonic() < end and not self._stop:
                            time.sleep(min(0.05, end - time.monotonic()))
            except Exception as e:
                self.error(f'watcher: {type(e).__name__}: {e}')
                # Fall through -- thread exits, no auto-restart.

        t = threading.Thread(
            target=_runner,
            daemon=True,
            name='browse-tui-watcher',
        )
        t.start()
        return t

    # ---- worker lifecycle ----------------------------------------------

    def start_workers(self) -> None:
        """Spawn the children + preview worker threads.

        Idempotent -- a second call while running is a no-op. Threads are
        daemons so the process exits cleanly even if a worker is mid-fetch
        (no atexit complications, no nondaemon shutdown hangs).
        """
        if self._workers_running:
            return
        self._stop = False
        self._workers_running = True
        self._children_thread = threading.Thread(
            target=self._children_worker,
            daemon=True,
            name='browse-tui-children',
        )
        self._preview_thread = threading.Thread(
            target=self._preview_worker,
            daemon=True,
            name='browse-tui-preview',
        )
        self._children_thread.start()
        self._preview_thread.start()

    def stop_workers(self, timeout: float = 1.0) -> None:
        """Signal both workers to exit and join with ``timeout``.

        Idempotent. Sets ``_stop``, then sets both events so each worker
        wakes from its outer ``wait()`` and observes the stop flag.
        """
        if not self._workers_running:
            return
        self._stop = True
        # Both workers wait on their own event; set both so neither
        # blocks indefinitely. The inner ``while`` in each worker also
        # checks ``_stop`` so any in-progress fetch finishes naturally.
        self._children_event.set()
        self._preview_event.set()
        if self._children_thread:
            self._children_thread.join(timeout=timeout)
        if self._preview_thread:
            self._preview_thread.join(timeout=timeout)
        self._workers_running = False

    # ---- main-loop drain (production main loop + tests) ----------------

    def drain_main_queue(self) -> int:
        """Run all currently posted callables; return how many ran.

        Runs only fns that were already in the queue when we started --
        callables that post further work end up running on the next
        drain (so there's no risk of a tight loop monopolising the main
        thread). queue.Queue's get_nowait gives us the right semantics.
        """
        n = 0
        while True:
            try:
                fn = self._main_queue.get_nowait()
            except queue.Empty:
                break
            fn()
            n += 1
        return n

    def apply_children_results(self) -> int:
        """Move worker-produced children results into the cache.

        Called on the main thread after a wake. For each delivered
        ``(id, items)``: store in cache, mark visible-tree dirty, drop
        the pending bit, resolve all in-flight Pendings registered for
        that id. Phase 3 (ticket #29) coalesces duplicate enqueues in
        ``_do_refresh``, so a single delivery resolves every Pending
        that asked for ``id_`` while the fetch was in flight.

        Also flags the list pane for redraw so the next render pass
        surfaces the freshly-delivered children without waiting for the
        next user keystroke -- otherwise the screen lags behind the
        cache when ``_notify`` is the only thing waking the loop.
        """
        n = 0
        while self._children_results:
            id_, items = self._children_results.popleft()
            self._state._children[id_] = items
            self._state._children_pending.discard(id_)
            mark_visible_dirty(self._state)
            for p in self._children_in_flight.pop(id_, []):
                p._resolve()
            n += 1
        if n:
            # If the apply shrank the visible list past the cursor,
            # clamp it so the cursor still indexes a real row. Without
            # this, a watcher-driven refresh that removes items can
            # leave state.cursor past len(visible) — the renderer
            # skips the row (no crash) but the cursor effectively
            # disappears until the user presses j/k.
            vis = visible_items(self._state)
            if vis and self._state.cursor >= len(vis):
                self._state.cursor = len(vis) - 1
            elif not vis:
                self._state.cursor = 0
            self._needs_redraw.add('list')
            # The cache may have just filled the cursor item's
            # children — flag the grid pane for redraw too. Render-time
            # checks gate the actual paint so this is harmless when the
            # grid is hidden / disabled.
            self._needs_redraw.add('children')
            # Layout depends on grid sizing: when the grid was hidden
            # waiting for children to arrive, the preview now needs to
            # shrink to make room. A full repaint is cheaper than
            # tracking that delta by hand.
            self._needs_redraw.add('all')
        return n

    def apply_preview_result(self) -> bool:
        """Move the worker-produced preview text into the cache.

        Returns True if a result was applied, False if the slot was empty.
        Also flags the preview pane dirty so the loop renders it on the
        next pass (without this the worker delivery only becomes visible
        after the next keystroke that triggers a ``_needs_redraw`` add).
        """
        if self._preview_result is None:
            return False
        id_, text = self._preview_result
        self._preview_result = None
        self._state._preview[id_] = text
        self._needs_redraw.add('preview')
        return True

    def run_until_idle(self, timeout: float = 2.0) -> None:
        """Test affordance: drain queues + wait for workers, until idle.

        Idle means: main_queue empty AND children_queue empty AND
        children_results empty AND _preview_req is None AND
        _preview_result is None AND no in-flight pendings. Polls every
        5ms and raises ``TimeoutError`` if not idle within ``timeout``.

        Safe to call repeatedly. Production code uses the real main loop
        (ticket #13); this exists only so tests don't have to invent
        their own pump.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain_main_queue()
            self.apply_children_results()
            self.apply_preview_result()
            if (self._main_queue.empty()
                    and not self._children_queue
                    and not self._children_results
                    and self._preview_req is None
                    and self._preview_result is None
                    and not self._children_in_flight):
                return
            time.sleep(0.005)
        raise TimeoutError(
            f'run_until_idle: still busy after {timeout}s '
            f'(queue={self._main_queue.qsize()}, '
            f'children_queue={len(self._children_queue)}, '
            f'children_results={len(self._children_results)}, '
            f'preview_req={self._preview_req!r}, '
            f'preview_result={self._preview_result!r}, '
            f'in_flight={list(self._children_in_flight)})'
        )

    # ---- internal: refresh dispatch (main thread) -----------------------

    def _do_refresh(self, id_, pending):
        """Run the cache-invalidate + enqueue step on the main thread.

        Posted by ``refresh`` so concurrent callers don't race on the
        cache or the in-flight registry. ``id=None`` means full refresh
        (clear all caches and refetch the root).

        Phase 3 (ticket #29) coalesces duplicate enqueues: if a fetch is
        already pending for ``id_``, the new ``pending`` is registered in
        ``_children_in_flight[id_]`` and resolves with the existing
        fetch's result -- no second worker fetch is triggered. Freshness
        note: a second ``refresh`` arriving while a fetch is in flight
        sees the in-flight result, even if data mutated between the two
        calls. Callers that need a guaranteed re-fetch can chain a
        further ``refresh`` after the first resolves (the
        ``_children_pending`` gate is cleared in ``apply_children_results``).
        """
        if id_ is None:
            cache_invalidate_all(self._state)
            id_ = self._state.root_id
        else:
            cache_invalidate_subtree(self._state, id_)
        # Force the next ``_update_preview_for_cursor`` to re-fetch.
        # The ``_preview_cursor_id`` gate (#126) skips re-requests when
        # the cursor stays on the same item, but a refresh just
        # invalidated the underlying data, so the cached preview text
        # is stale and a re-fetch is the correct action.
        self._preview_cursor_id = None
        # Always register the waiter so it resolves with the fetch result.
        self._children_in_flight.setdefault(id_, []).append(pending)
        # Only enqueue + flag pending the first time -- a fetch already in
        # flight for this id will deliver one result that resolves every
        # registered waiter together.
        if id_ not in self._state._children_pending:
            self._state._children_pending.add(id_)
            self._children_queue.append(id_)
            self._children_event.set()

    def _do_cursor_to(self, id_, pending):
        """Main-thread: position the cursor at ``id_`` and resolve ``pending``.

        Phase 1 simplification: we walk the current visible-tree list
        and, if ``id_`` is found, set ``self._state.cursor`` to its
        index. If not found, we resolve the Pending anyway (best-effort)
        so chained ``.then()`` callbacks don't strand. A future phase
        could expand the parent chain via parent metadata on Items;
        recipes that need it today should use ``from_flat_tree`` to
        pre-populate the cache, in which case every id is visible.

        Flags ``list`` / ``children`` / ``preview`` for redraw on a
        successful move so the next render pass surfaces the new
        cursor position -- without this, an external thread's
        ``cursor_to`` would silently move the cursor but the screen
        wouldn't update until the next user keystroke (regression
        guarded by ticket #77's stress tests).
        """
        # Build/refresh the visible list; visible_items honours the
        # dirty bit so this is a no-op if nothing changed.
        vis = visible_items(self._state)
        for i, entry in enumerate(vis):
            if entry.item.id == id_:
                self._state.cursor = i
                self._needs_redraw.add('list')
                self._needs_redraw.add('children')
                self._needs_redraw.add('preview')
                pending._resolve()
                return
        # Not visible. Best-effort: leave cursor alone and resolve.
        # (Documented in cursor_to's docstring.)
        pending._resolve()

    def _do_expand(self, id_, pending):
        """Main-thread: add ``id_`` to expanded; fetch if not cached.

        If children are already cached, mark visible-tree dirty and
        resolve immediately. Otherwise register the Pending as a waiter
        and enqueue the fetch -- ``apply_children_results`` resolves it
        once the worker delivers.

        In both cases mark the list pane dirty so the next render pass
        surfaces the new sub-tree (or the ``⧗ loading…`` placeholder
        while the fetch is in flight) -- otherwise the loop renders
        nothing until the worker completes and the placeholder is
        invisibly skipped.
        """
        self._state.expanded.add(id_)
        if id_ in self._state._children:
            mark_visible_dirty(self._state)
            self._needs_redraw.add('list')
            pending._resolve()
            return
        self._state._children_pending.add(id_)
        self._children_in_flight.setdefault(id_, []).append(pending)
        self._children_queue.append(id_)
        self._children_event.set()
        mark_visible_dirty(self._state)
        self._needs_redraw.add('list')

    def _do_select(self, ids, replace):
        """Main-thread: update ``selected`` set + flag list-pane redraw."""
        if replace:
            self._state.selected = set(ids)
        else:
            self._state.selected.update(ids)
        mark_visible_dirty(self._state)
        self._needs_redraw.add('list')

    def _do_quit(self, code, output):
        """Main-thread: flip the quit flag and stash exit code/output."""
        self._quit_code = code
        self._quit_output = output
        self._quit_requested = True
        # Wake the main loop so it observes _quit_requested promptly. In
        # headless mode notify_wake is a no-op; in production it writes
        # one byte to the self-pipe.
        notify_wake()

    # ---- workers --------------------------------------------------------

    def _children_worker(self):
        """FIFO worker thread: drain ``_children_queue`` one id at a time.

        Errors caught at the boundary -- a misbehaving ``get_children``
        must not crash the worker thread (which would leave Pendings
        unresolved forever). On error: cache becomes ``[]``, error_text
        is updated, the Pending still resolves so chains keep firing.
        """
        while not self._stop:
            self._children_event.wait()
            self._children_event.clear()
            while self._children_queue and not self._stop:
                id_ = self._children_queue.popleft()
                try:
                    raw = self.get_children(id_)
                    items = [to_item(x) for x in raw]
                except Exception as e:
                    items = []
                    # Cross-thread write to a Python str attribute is
                    # safe under the GIL; the renderer reads it later
                    # on the main thread.
                    self._error_text = (
                        f'get_children({id_!r}): {type(e).__name__}: {e}'
                    )
                self._children_results.append((id_, items))
                notify_wake()

    def _preview_worker(self):
        """Latest-wins single-slot worker (ported from plan-tui).

        Reads ``_preview_req`` atomically, fetches, writes the result.
        Only the main thread writes ``_preview_req`` -- the worker
        clears it only after confirming no newer request landed during
        the fetch. If a newer request did land, we loop immediately to
        serve it without blocking on the event.
        """
        while not self._stop:
            self._preview_event.wait()
            self._preview_event.clear()
            while not self._stop:
                req_id = self._preview_req
                if req_id is None:
                    break
                try:
                    if self.get_preview is not None:
                        text = self.get_preview(req_id)
                        if text is None:
                            text = ''
                    else:
                        text = ''
                except Exception as e:
                    text = f'[error] {type(e).__name__}: {e}'
                self._preview_result = (req_id, text)
                notify_wake()
                # Latest-wins: only clear the slot if no newer request
                # landed during the fetch. Otherwise loop immediately to
                # serve the newer one (we'll overwrite _preview_result
                # on the next iteration -- the main thread may have
                # already consumed the previous one, or it may not; either
                # way the latest result is what wins).
                if self._preview_req == req_id:
                    self._preview_req = None
                # else: keep looping with the new req_id

    # ---- internal: preview request slot ---------------------------------

    def request_preview(self, id_: Any) -> None:
        """Set the latest-wins preview request slot.

        Called on the main thread (typically by cursor-move handlers in
        ticket #8). Idempotent for the same id.
        """
        self._preview_req = id_
        self._preview_event.set()

    # ---- main loop ------------------------------------------------------

    def run(self) -> int:
        """Run the TUI main loop until quit. Returns exit code.

        Drives workers + post queue + render. Sets up terminal in
        non-headless mode; tears down at exit. Honours SIGTSTP/SIGCONT
        via the signal handlers in 020-terminal.

        Returns the exit code stored by ctx.quit() (or browser.quit()),
        plus prints any captured ``_quit_output`` to stdout after
        ``term_restore``.

        Auto-detects ``-h`` / ``--help`` in ``sys.argv[1:]`` so recipes
        that call ``Browser.run()`` without their own argparse get
        recipe-aware help (intro/outro + custom actions) for free,
        rather than dropping the user into the TUI with the help flag
        as a meaningless argv entry. Recipes that consume ``-h`` /
        ``--help`` themselves before calling ``run()`` are unaffected
        (their argparse strips the flag from sys.argv first).

        Cross-module symbols (``term_init``/``term_restore``/``read_key``/
        ``g_resize_flag``/``Context``/``dispatch_key``/``render_full``/
        ``render_partial``/``compose_help_text``) are resolved as bare
        globals — in the concatenated production build that's the
        unified namespace; in tests the loader injects them onto this
        module.
        """
        # Help flag short-circuit: print composed help (intro + sections
        # + CUSTOM ACTIONS + outro) and exit without entering the loop.
        # Honours -h and --help as exact tokens; ``--help=foo`` style
        # bundling is not relevant here (argparse-using recipes consume
        # it first; the auto-detect target is recipes that don't argparse).
        if any(arg in ('-h', '--help') for arg in sys.argv[1:]):
            sys.stdout.write(compose_help_text(self, include_usage=False))
            return 0

        self.start_workers()
        if not self._headless:
            term_init()
        ctx = Context(self)
        self._ctx = ctx

        # Initial fetch + render. We post a refresh of the root so the
        # children worker populates the cache (or leverages an already-
        # populated cache from from_flat_tree). Wait briefly for the
        # first results before painting so the user doesn't flash a
        # ``loading…`` placeholder for callbacks that resolve in <500ms.
        self.refresh()
        if not self._headless:
            try:
                self.run_until_idle(timeout=0.5)
            except TimeoutError:
                pass  # slow callback; render the loading state
            self._update_preview_for_cursor()
            self._update_children_for_cursor()
            try:
                self.run_until_idle(timeout=0.2)
            except TimeoutError:
                pass
            render_full(self)

        try:
            while not self._quit_requested:
                # Drain pending updates from any thread, then render if
                # something is dirty. Pre-key drain so a worker result
                # that landed before this iteration is visible by the
                # next read_key wake.
                self.drain_main_queue()
                self.apply_children_results()
                self.apply_preview_result()

                # Re-derive preview / children fetches for the current
                # cursor *after* applying worker deliveries — when a
                # slow get_children resolves long after the startup
                # wait, the cursor-on-row-0 finally points at a real
                # item and we need to kick the preview now (otherwise
                # the preview pane stays blank until the user presses
                # a key, since the bottom-of-loop call only fires
                # after key dispatch). Both helpers are idempotent.
                self._update_preview_for_cursor()
                self._update_children_for_cursor()

                # Resize flag — set by SIGWINCH handler in 020-terminal.
                # Bare-name access works in the concatenated build; in
                # tests this attribute is injected onto the module.
                if globals().get('g_resize_flag', False):
                    globals()['g_resize_flag'] = False
                    self._needs_redraw.add('all')

                if self._needs_redraw and not self._headless:
                    if 'all' in self._needs_redraw:
                        render_full(self)
                    else:
                        render_partial(self)

                if self._quit_requested:
                    break

                try:
                    key = read_key()
                except KeyboardInterrupt:
                    key = 'ctrl-c'

                if globals().get('g_resize_flag', False):
                    globals()['g_resize_flag'] = False
                    self._needs_redraw.add('all')

                if key == '_notify':
                    # Worker delivered something; loop and drain.
                    continue

                # Dispatch the key to the action layer. Insert-mode is
                # the most-special branch (placement marker movement),
                # then search-mode + normal-mode dispatch live in
                # dispatch_key.
                #
                # Snapshot the active list row before dispatch so we
                # can snap the viewport back to follow the cursor when
                # a key actually moved it. Wheel-scroll handlers leave
                # the cursor alone, so the comparison is False and
                # ``_list_scroll`` keeps the user's scrolled position.
                prev_row = self._active_list_row()
                prev_insert = self._insert_mode
                if self._insert_mode:
                    _handle_insert_key(self, ctx, key)
                else:
                    dispatch_key(self, ctx, key)
                new_row = self._active_list_row()
                if prev_insert != self._insert_mode or new_row != prev_row:
                    self._snap_list_scroll_to_row(new_row)

                # Trigger preview + children fetches for the (possibly
                # moved) cursor before the next render pass. The
                # children fetch populates the children-grid pane.
                self._update_preview_for_cursor()
                self._update_children_for_cursor()
        finally:
            if not self._headless:
                term_restore()
            self.stop_workers()

        # After teardown — print captured output (e.g. from on_enter
        # print-exit). Done outside the alternate screen so the user's
        # shell sees the result.
        if self._quit_output:
            sys.stdout.write(self._quit_output)
            sys.stdout.flush()

        return self._quit_code

    def _update_preview_for_cursor(self) -> None:
        """Request a preview fetch for the current cursor item.

        No-op when previews are disabled or when the cursor is on a
        non-normal entry (placeholder / scope-root). Called by the main
        loop at the top of every iteration (post-#124) so cursor moves
        and worker deliveries both trigger latest-wins preview fetches.

        When the cursor lands on a different item (or off any normal
        item) since the last call, the preview pane is reset: scroll
        offset returns to 0 and help mode (if active) is dismissed so
        the user sees the new item's preview, not stale state.

        Idempotency note: the gate is ``_preview_cursor_id`` (only
        updated when the cursor moves to a different item), NOT
        ``_preview_req`` (cleared by the worker after each fetch). A
        ``_preview_req`` gate would cause a hot fetch loop now that
        this runs every iteration — see ticket #126.
        """
        if not self.show_preview:
            return
        state = self._state
        vis = visible_items(state)

        new_id = None
        if 0 <= state.cursor < len(vis):
            entry = vis[state.cursor]
            if entry.kind == 'normal':
                new_id = entry.item.id

        if new_id == self._preview_cursor_id:
            # Cursor still on the same item — already requested in a
            # prior call, the worker either resolved or is in flight.
            # Re-firing now would kick a redundant fetch every iteration.
            return

        self._preview_cursor_id = new_id
        self._preview_scroll = 0
        if self._help_mode:
            self._help_mode = False
        self._needs_redraw.add('preview')

        if new_id is None:
            self._preview_req = None
            return
        self.request_preview(new_id)

    def _list_pane_height_safe(self) -> int:
        """Return the list pane's height, or 0 if it can't be determined.

        Mirrors ``_list_pane_height`` in 070-actions.py but lives here
        so it can be called from state-layer methods without crossing
        the module boundary. Falls back to 0 when ``term_size`` /
        ``layout_panes`` aren't available (headless tests, no tty); the
        snap helper treats 0 as "give up gracefully."
        """
        try:
            ts = globals().get('term_size')
            lp = globals().get('layout_panes')
            if ts is None or lp is None:
                return 0
            cols, rows = ts()
            layout = lp(
                cols, rows,
                split=self.split,
                show_preview=self.show_preview,
                show_children_pane=self.show_children_pane,
                list_ratio=self.list_ratio,
            )
            list_rect = layout.get('list')
            if list_rect is None:
                return 0
            h = list_rect.height
            return h if h > 0 else 0
        except Exception:
            return 0

    def _snap_list_scroll_to_row(self, row: int) -> None:
        """Adjust ``_list_scroll`` so visible-list ``row`` is on-screen.

        Called from the main loop whenever a key changes the active row
        (cursor in normal mode, ``_insert_pos`` in insert mode). Wheel
        scrolling does *not* call this — that's the whole point of the
        decoupling: a wheel scroll can move the viewport past the
        cursor and the next render keeps it there. The renderer's
        bounds-only clamp prevents nonsense (negative or past-end)
        offsets, but doesn't drag scroll back to the cursor.
        """
        height = self._list_pane_height_safe()
        if height <= 0:
            return
        if row < self._list_scroll:
            self._list_scroll = max(0, row)
            self._needs_redraw.add('list')
        elif row >= self._list_scroll + height:
            self._list_scroll = max(0, row - height + 1)
            self._needs_redraw.add('list')

    def _active_list_row(self) -> int:
        """Return the row currently considered the 'cursor' on-screen.

        In insert mode the marker (``_insert_pos``) is what the user
        controls and what we want to keep visible. Outside insert mode
        it's ``state.cursor``. The main loop uses this to detect
        active-row changes after a dispatched key and call
        ``_snap_list_scroll_to_row`` accordingly.
        """
        if self._insert_mode:
            return self._insert_pos
        return self._state.cursor

    def _update_children_for_cursor(self) -> None:
        """Kick a children fetch for the cursor item if it's an unfetched branch.

        The children-grid pane (ticket #19) wants the cursor item's
        direct children even when the cursor row itself isn't expanded
        in the list pane. Plan-tui drives this with a separate
        ``plan_children(tid)`` round-trip; browse-tui has only one
        children worker, so we re-use it: enqueue the cursor id without
        touching ``state.expanded`` (so the visible-tree builder doesn't
        descend into it). The cache fills, the grid renders, and the
        list pane stays unchanged.

        No-op when the children-grid pane is disabled, when the cursor
        is on a non-normal entry, when the cursor item has no children,
        or when its children are already cached / in flight.
        """
        if not self.show_children_pane:
            return
        state = self._state
        vis = visible_items(state)
        if not (0 <= state.cursor < len(vis)):
            return
        entry = vis[state.cursor]
        if entry.kind != 'normal':
            return
        item = entry.item
        if not item.has_children:
            return
        if item.id in state._children:
            return  # already cached
        if item.id in state._children_pending:
            return  # already in flight
        state._children_pending.add(item.id)
        self._children_queue.append(item.id)
        self._children_event.set()

    # ---- eager adapter -------------------------------------------------

    @classmethod
    def from_flat_tree(cls, rows, *, root_id: Any = None,
                       **browser_kwargs) -> 'Browser':
        """Build a Browser whose ``_children`` cache is pre-populated from ``rows``.

        Each row may be ``Item``, ``str``, ``tuple``, or ``dict`` (per
        ``to_item`` rules). Hierarchy detection looks at the coerced
        Items:

        * **parent-pointer mode** -- if any row has a ``parent`` field
          (or attribute) other than ``None``, every row is grouped under
          its parent's id; rows with no parent (or ``parent is None``)
          go under ``root_id``.
        * **depth-coded mode** -- otherwise, if any row has a ``depth``
          field, walk rows in iteration order maintaining a stack of
          ``(depth, item)``: a row at depth ``d+1`` is a child of the
          most recent row at depth ``d``; depth-0 rows go under
          ``root_id``.
        * **flat mode** -- if neither hint is present, all rows become
          direct children of ``root_id``.

        The synthesised ``get_children`` reads from the pre-populated
        cache, so no user callback runs at runtime. Recipes wanting
        true laziness should pass their own ``get_children`` instead.
        """
        items = [to_item(r) for r in rows]
        has_parent = any(
            getattr(it, 'parent', None) is not None for it in items
        )
        has_depth = (
            not has_parent
            and any(getattr(it, 'depth', None) is not None for it in items)
        )

        children_by_parent: dict = {}

        if has_parent:
            for it in items:
                p = getattr(it, 'parent', None)
                if p is None:
                    p = root_id
                children_by_parent.setdefault(p, []).append(it)
        elif has_depth:
            # Walk in iteration order, maintaining a stack of
            # (depth, item). For each row at depth d: pop frames with
            # depth >= d, then the parent is the top-of-stack item (or
            # root_id if the stack is empty).
            stack: list = []
            for it in items:
                d = getattr(it, 'depth', 0) or 0
                while stack and stack[-1][0] >= d:
                    stack.pop()
                parent = stack[-1][1].id if stack else root_id
                children_by_parent.setdefault(parent, []).append(it)
                stack.append((d, it))
        else:
            if items:
                children_by_parent[root_id] = list(items)

        # Synthesise the get_children callback to read from the cache.
        # Captures children_by_parent rather than self._state._children
        # so a later cache_invalidate_all() doesn't strand the recipe
        # (we still want eager reads to win).
        def _get_children_eager(pid):
            return children_by_parent.get(pid, [])

        browser_kwargs.setdefault('get_children', _get_children_eager)
        browser_kwargs.setdefault('root_id', root_id)
        b = cls(**browser_kwargs)
        # Pre-populate the cache so visible_items() and apply_*_results
        # see it immediately; no fetch happens at runtime unless the
        # caller invokes ``refresh`` explicitly.
        b._state._children.update(children_by_parent)
        return b
