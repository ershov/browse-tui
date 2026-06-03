"""browse-tui: state layer (visible tree, cursor/scope, async workers, post queue, Pending)."""

import enum
import inspect
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field, fields as _dc_fields
from typing import Any, Callable, Optional


# Cross-module symbol: ``registered_plugins`` is defined by
# ``035-plugins.py`` in the concatenated build. When this file is
# loaded standalone (tests), ``setdefault`` installs an empty list
# so Browser construction with no plugins behaves correctly. Tests
# that need plugin behaviour rewire the attribute explicitly.
globals().setdefault('registered_plugins', [])


class Mode(enum.Enum):
    """User-input dispatch mode for the Browser key handler.

    ``NORMAL`` — keystrokes dispatch through the action keymap.
    ``SEARCH_EDIT`` — ``/`` prompt open; typed chars extend the
    search query, navigation keys still fall through.
    ``FILTER_EDIT`` — ``&`` prompt open; typed chars extend the
    last entry of ``Browser._filters``, with similar fall-through.
    """
    NORMAL = 'normal'
    SEARCH_EDIT = 'search-edit'
    FILTER_EDIT = 'filter-edit'


class CancellationToken:
    """Cooperative-cancellation handle returned by ``Browser.run_in_slot``.

    Each call to ``run_in_slot(name, fn)`` returns a fresh
    ``CancellationToken``. Workers receive the token and must poll
    ``is_cancelled()`` themselves at safe points — the framework
    does **not** kill threads. Calling ``cancel()`` from any
    thread sets the flag.

    When the same slot is reused (next call to
    ``run_in_slot(same_name, ...)``), the prior token is cancelled
    automatically before the new worker starts.
    """

    __slots__ = ('_cancelled',)

    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def is_cancelled(self) -> bool:
        """Return ``True`` after :meth:`cancel` (or supersede) ran."""
        return self._cancelled.is_set()

    def cancel(self) -> None:
        """Set the cancelled flag. Idempotent."""
        self._cancelled.set()


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


# Per-pane row cache for the differential renderer (#185–#188).
#
# Holds the geometry of the pane during the most recent paint plus a
# parallel list of cached rows. Each ``lines`` slot is either ``None``
# (never painted, or invalidated and not yet repainted) or a tuple
# ``(visible_len, bytes)`` produced by ``end_row`` in 020-terminal.
#
# The cache is duck-typed against ``begin_row`` / ``end_row``: those
# helpers read ``.rect`` / ``.prev_rect`` and read/write ``.lines[i]``.
# We deliberately leave ``rect`` / ``prev_rect`` un-typed because the
# concatenated build order puts ``Rect`` (in 050-render.py) AFTER this
# file at module load time — a forward annotation would dangle when
# 040-state is loaded in isolation by the unit-test loader.
@dataclass
class PaneCache:
    """Per-pane row cache used by the synchronized-output renderer.

    Fields:
      * ``rect`` — geometry of the pane during the most recent paint.
      * ``prev_rect`` — geometry of the pane during the prior paint.
        ``begin_row`` / ``end_row`` consult this to decide whether a
        cached row from the previous paint is still positionally valid.
      * ``lines`` — list of length ``rect.height`` of cached row
        entries; each entry is ``None`` or ``(visible_len, bytes)``.

    The renderer migrations in #187/#188 will drive the cache; this
    ticket (#186) only wires the structure onto Browser.
    """

    rect: Any = None
    prev_rect: Any = None
    lines: list = field(default_factory=list)

    def invalidate(self, new_rect) -> None:
        """Rotate ``rect`` -> ``prev_rect``, install ``new_rect``, reset lines.

        The line buffer is sized to the new rect's height so per-row
        access is straight-line indexing.
        """
        self.prev_rect = self.rect
        self.rect = new_rect
        self.lines = [None] * new_rect.height

    def update_rect(self, rect) -> None:
        """Reconcile cache state with the pane's geometry for this frame.

        Single per-frame entry point covering both the per-pane
        cache rotation AND the orchestrator-level "disappeared pane"
        stamp (formerly ``_mark_disappeared_panes`` in 050-render.py).
        See ticket #228 for the motivation.

        Three branches:

          * ``rect is None`` — the pane is hidden this frame. If the
            cache currently holds a real geometry (not the sentinel and
            not unpainted), stamp the sentinel so the next ``update_rect``
            with a real rect is forced through the rect-changed path
            (full pad on reappear, clearing cells overwritten by
            neighbouring panes while the pane was hidden).
          * ``rect == self.rect`` — steady state. On the second call
            after an ``invalidate`` (where ``prev_rect != rect``), roll
            ``prev_rect`` forward so subsequent paints take the
            steady-state branch in ``end_row``. Otherwise no-op.
          * ``rect != self.rect`` — geometry changed (resize, move, or
            reappear after hide). Invalidate to the new rect.

        Critical invariant: this method must be called EXACTLY ONCE per
        cache per frame. Calling it twice on a fresh-rect frame would
        roll ``prev_rect`` forward inside that same frame, putting
        ``end_row`` into steady-state regime and under-padding the row.
        """
        if rect is None:
            if self.rect is not None and self.rect != _SENTINEL_RECT:
                self.rect = _SENTINEL_RECT
                self.lines = []
            return
        if self.rect != rect:
            self.invalidate(rect)
        elif self.prev_rect != self.rect:
            # The rect-change signal was consumed by the prior paint's
            # padding pass. Roll ``prev_rect`` forward so subsequent paints
            # take the steady-state branch in ``end_row``.
            self.prev_rect = self.rect


# Sentinel rect used by :meth:`PaneCache.update_rect` to mark a cache
# whose pane was hidden between paints (e.g. layout 'v'/'m'/'pc'
# children/sep_inner panes when the cursor moves onto a no-children
# item). Stored in ``cache.rect`` so the next ``update_rect(real_rect)``
# sees a mismatch and runs ``invalidate``, routing ``end_row`` through
# the "rect changed → full pad" path and clearing cells the neighbour
# overwrote while the pane was hidden. The negative-coordinate values
# can never collide with a real screen rect (which uses 1-based positive
# coords). The sentinel is constructed from a duck-typed shim with the
# same surface area as ``Rect`` (``__eq__`` / ``__hash__`` / ``height``)
# because the concatenated build order puts ``Rect`` (in 050-render.py)
# AFTER this file at module load time — depending on it here would
# crash the unit-test loader, which loads 040-state.py standalone.
class _SentinelRect:
    """Duck-typed Rect stand-in for the disappeared-pane sentinel.

    Implements the minimal surface PaneCache needs (``__eq__`` /
    ``__hash__`` / ``height``). Compares unequal to any real ``Rect``
    so ``update_rect`` always takes the rect-changed branch on the
    next reappear.
    """

    __slots__ = ('left', 'top', 'right', 'bottom')

    def __init__(self, left, top, right, bottom):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    @property
    def height(self):
        return self.bottom - self.top

    def __eq__(self, other):
        return self is other

    def __hash__(self):
        return id(self)

    def __repr__(self):
        return '_SENTINEL_RECT'


_SENTINEL_RECT = _SentinelRect(-1, -1, -1, -1)


# A single rendered row produced by ``visible_items``.
@dataclass
class VisibleEntry:
    """One entry in the rendered visible list.

    Fields:
      - ``item``: the Item being shown (or a synthetic placeholder).
      - ``depth``: 0-based depth relative to the current scope root.
      - ``kind``: one of:
          * ``'normal'`` — an ordinary tree row. The scope row at
            depth 0 (when scoped) is also ``'normal'``; recipes and
            actions identify it via ``item.id == current_scope(state)``
            rather than a row-role discriminator.
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
    # Bookkeeping indexes maintained alongside ``_children``. Foundation
    # for the upcoming ``update_data`` push API (see
    # docs/superpowers/specs/2026-05-08-streaming-push-api-design.md).
    # Today they are write-only — no public API reads from them yet —
    # but every mutation site of ``_children`` keeps them in lockstep so
    # later tickets can layer push-mode ops on top without retrofitting
    # invariants.
    #
    # ``_items_by_id``: primary id -> Item. Every Item that lives in any
    #     ``_children[parent].list`` is reachable here.
    # ``_parent_of_id``: child id -> parent id. Reverse index used by
    #     ``update_data`` to detect reparenting.
    # ``_loading``: parent id -> bool. Explicit "fetch in flight" flag.
    #     Today's UX hint is implied by ``_children_pending`` membership;
    #     ``_loading`` makes it addressable so ops like
    #     ``("complete", parent_id)`` / ``("incomplete", parent_id)``
    #     can flip it directly. ``_children_pending`` stays as the
    #     dispatch tracker.
    _items_by_id: dict = field(default_factory=dict)
    _parent_of_id: dict = field(default_factory=dict)
    _loading: dict = field(default_factory=dict)
    _visible_dirty: bool = True
    _visible_cache: list = field(default_factory=list)
    # Cursor index into the visible-tree list, and the user-selected ids
    # (rendered with a ``*`` marker by the renderer in ticket #10). Both
    # live on State so unit tests can construct one without spinning up
    # a Browser, and so the visible-tree builder can read ``selected``
    # later when it wires marker columns.
    cursor: int = 0
    selected: set = field(default_factory=set)
    # Filter-active flag — derived state set by ``_recompute_filter_hidden``
    # in lockstep with the per-Item ``_filter_hidden`` flags. The
    # renderer checks this before consulting ``_filter_hidden`` so the
    # check short-circuits when no filter is active. See
    # ``docs/superpowers/specs/2026-05-17-filter-design.md``.
    _filter_active: bool = False
    # Side-effect signals populated by ``apply_ops`` when preview-cache
    # ops run in a batch (#446). ``apply_ops`` is pure state mutation,
    # but preview ops have Browser-level fallout: a preview-pane redraw
    # signal and (for invalidate / cursor-affecting drops) a preview
    # worker kick. We collect those signals on State so ``apply_ops``
    # stays argument-pure; ``update_data._apply`` reads and translates
    # them after the batch lands. Each call of ``apply_ops`` resets
    # these slots before processing the batch.
    _preview_dirty: bool = False
    _preview_kicks: list = field(default_factory=list)
    # Parents whose children fetch SETTLED during the current
    # ``apply_ops`` / ``apply_children_results`` pass — recorded by
    # ``_set_loading(..., settled=True)`` (the ``complete`` op tail and
    # the legacy delivery deque). Collected on State so ``apply_ops``
    # stays argument-pure; the Browser-level callers move these into
    # ``_children_loaded_pending`` after the pass and reset the list
    # before populating it. NOT touched by ``clear_children`` (which
    # drops the cache) — see ``_set_loading``.
    _settled_parents: list = field(default_factory=list)


# ---- scope management ----------------------------------------------------


def current_scope(state: State) -> Any:
    """Return the id of the current scope root."""
    return state.scope_stack[-1] if state.scope_stack else state.root_id


def scope_into(state: State, item_id) -> None:
    """Push the current expanded set under its scope key, switch scope.

    The new scope's expanded set is restored from ``_expanded_by_scope``
    (empty if first visit). The scope id itself is auto-added to the
    new expanded set so the scope row paints expanded by default — the
    scope row is now a normal row at depth 0, and without the auto-add
    it would render collapsed (▶) until the user pressed Right. Marks
    the visible-tree dirty.
    """
    # Memoise the expanded set under the scope we're leaving.
    state._expanded_by_scope[current_scope(state)] = state.expanded
    state.scope_stack.append(item_id)
    # Restore (or default to empty) the expanded set for the new scope.
    # Copy on restore so the auto-add below doesn't leak into the
    # memoised entry; scope_out writes state.expanded back to
    # _expanded_by_scope when leaving, preserving any explicit
    # collapses the user made while in scope.
    state.expanded = set(state._expanded_by_scope.get(item_id, ()))
    # Auto-expand the scope row itself (see docstring).
    state.expanded.add(item_id)
    state._visible_dirty = True


def scope_out(state: State) -> Any:
    """Pop the top of ``scope_stack``, restoring the previous expanded set.

    Returns the id we were scoped *to* before popping (so the caller can
    move the cursor onto it). Returns None if the stack is already empty.

    Mirrors ``scope_into``'s auto-expand invariant: when we land in a
    still-scoped state, the new current scope id is added to
    ``state.expanded`` so the scope row paints expanded by default.
    Without this, popping into a scope whose memoised expanded set
    doesn't include itself (e.g. a recipe-pushed deep stack) would
    paint the scope row collapsed and hide the very row the caller
    intends to land the cursor on.
    """
    if not state.scope_stack:
        return None
    leaving = state.scope_stack.pop()
    state._expanded_by_scope[leaving] = state.expanded
    state.expanded = set(state._expanded_by_scope.get(current_scope(state), ()))
    if state.scope_stack:
        state.expanded.add(state.scope_stack[-1])
    state._visible_dirty = True
    return leaving


# ---- cache invalidation --------------------------------------------------


def mark_visible_dirty(state: State) -> None:
    """Flip the visible-tree dirty bit. Next ``visible_items`` rebuilds."""
    state._visible_dirty = True


def _index_drop_children(state: State, parent_id) -> None:
    """Drop ``_items_by_id`` / ``_parent_of_id`` entries for one parent's children.

    Called whenever ``_children[parent_id]`` is being dropped or replaced
    (cache invalidation, fresh worker delivery). Idempotent: safe to call
    for a parent with no cache entry. Doesn't touch ``_loading`` — that
    flag is owned by dispatch / delivery, not by index maintenance.
    """
    items = state._children.get(parent_id)
    if not items:
        return
    for child in items:
        # Only drop the reverse index if it still points at this parent
        # — the item may have been reparented in-batch by a later op
        # (forward-compat with ``update_data``); today's mutation paths
        # always satisfy the guard.
        if state._parent_of_id.get(child.id) == parent_id:
            state._parent_of_id.pop(child.id, None)
            state._items_by_id.pop(child.id, None)


def _index_add_children(state: State, parent_id, items) -> None:
    """Add ``_items_by_id`` / ``_parent_of_id`` entries for one parent's children.

    Called whenever a list is being installed under ``_children[parent_id]``
    (worker delivery, eager pre-population). Doesn't touch ``_loading``
    — that flag is owned by dispatch / delivery.
    """
    for child in items:
        state._items_by_id[child.id] = child
        state._parent_of_id[child.id] = parent_id


_PROMOTION_DATA_FIELDS = (
    'title', 'tag', 'tag_style', 'has_children', 'hidden', 'scope_title',
)


def _promote_synthetic(synthetic, real) -> None:
    """Copy data from ``real`` onto ``synthetic`` in place; clear flag.

    Used when a children-fetch delivery brings a real Item whose id
    matches an existing synthetic stub in ``_items_by_id`` (typically
    fabricated by ``visible_items`` for a scope-root id with no cached
    Item). Identity-preserving: callers can keep their reference to
    the synthetic and observe the promotion via attribute reads.

    Merge policy:
      - Item data fields (``title``, ``tag``, ``tag_style``,
        ``has_children``, ``hidden``, ``scope_title``): take ``real``.
      - Recipe-attached extras (``__dict__`` keys not in the dataclass
        field set): copied from ``real``. Extras on ``synthetic`` that
        aren't on ``real`` are not removed — synthetics rarely carry
        extras, but conservative-merge preserves them if present.
      - Framework cache slots (``preview``, ``preview_render``):
        ``real``'s values win when non-None; otherwise the synthetic's
        cached values are preserved. The common case is
        ``real.preview is None`` (the preview cache is filled lazily
        on first paint), so any preview already cached on the
        synthetic survives the promotion.
      - ``_filter_hidden``: ignored here; the framework recomputes it
        in ``apply_children_results`` after promotion.
      - ``synthetic`` flag: cleared.
    """
    for name in _PROMOTION_DATA_FIELDS:
        setattr(synthetic, name, getattr(real, name))
    # Recipe extras: anything on real.__dict__ not in the dataclass
    # field set.
    known = {f.name for f in _dc_fields(synthetic)}
    for k, v in real.__dict__.items():
        if k not in known:
            setattr(synthetic, k, v)
    # Cache slots: real wins when populated.
    if real.preview is not None:
        synthetic.preview = real.preview
        synthetic.preview_render = real.preview_render
    synthetic.synthetic = False


def _promote_synthetics(state: State, items):
    """Promote any synthetic stubs in ``_items_by_id`` matched by ``items``.

    Returns a new list (same length as ``items``) where each entry is
    either the original ``Item`` from ``items`` (no synthetic match)
    or the promoted synthetic (after in-place mutation). Substituting
    the promoted synthetic preserves identity in ``_items_by_id`` and
    on the per-Item preview cache.

    Called from ``apply_children_results`` between
    ``_index_drop_children`` and ``_state._children[id_] = items``.
    """
    out = []
    for incoming in items:
        existing = state._items_by_id.get(incoming.id)
        if existing is not None and getattr(existing, 'synthetic', False):
            _promote_synthetic(existing, incoming)
            out.append(existing)
        else:
            out.append(incoming)
    return out


def _set_loading(state: State, parent_id, value, *, settled=False) -> None:
    """Write ``state._loading[parent_id] = value``; the loading choke point.

    ``settled=True`` marks a genuine fetch SETTLEMENT — children for
    ``parent_id`` became available (the worker's ``complete`` op tail,
    ``apply_children_results``) — and records the id on
    ``state._settled_parents`` so the caller can move it into the
    Browser's ``_children_loaded_pending`` set (drained once per tick by
    ``_fire_children_loaded_if_pending``).

    ``settled`` is deliberately NOT passed by ``clear_children`` /
    ``incomplete`` / cache-invalidation: those clear loading but DROP (or
    never had) the children, so firing ``on_children_loaded`` there would
    violate the contract that ``ctx.cached_children(parent_id)`` is
    populated at fire time. Callers that collect settlements reset
    ``state._settled_parents`` before populating it (see ``apply_ops`` /
    ``apply_children_results``).
    """
    state._loading[parent_id] = value
    if settled:
        state._settled_parents.append(parent_id)


def cache_invalidate_subtree(state: State, item_id) -> None:
    """Drop one parent's children entry and mark the visible-tree dirty.

    Safe to call for a never-cached id — the missing key is simply ignored.
    Also drops the corresponding ``_items_by_id`` / ``_parent_of_id``
    entries for the children being evicted, and clears the ``_loading``
    flag for the parent (a fresh dispatch will re-set it).
    """
    _index_drop_children(state, item_id)
    state._children.pop(item_id, None)
    state._loading.pop(item_id, None)
    state._visible_dirty = True


def cache_invalidate_all(state: State) -> None:
    """Clear the entire children cache and mark the visible-tree dirty.

    Also clears the auxiliary indexes (``_items_by_id``,
    ``_parent_of_id``, ``_loading``) so they stay in lockstep with
    ``_children``.
    """
    state._children.clear()
    state._items_by_id.clear()
    state._parent_of_id.clear()
    state._loading.clear()
    state._visible_dirty = True


# ---- update_data ops (push API) ------------------------------------------
#
# The six tuple-op apply functions plus their module-level helper
# constructors. See
# ``docs/superpowers/specs/2026-05-08-streaming-push-api-design.md`` —
# Section 2 for full semantics.
#
# These are pure state-mutation helpers: no threading, no public API on
# ``Browser`` yet (that lands in ticket #269). ``apply_ops(state, ops)``
# walks the list and mutates ``state`` in place, in order. Reparenting in
# one op is visible to subsequent ops in the same batch — within-batch
# atomicity is the contract.


# Module-level helper constructors. These are exported from ``browse_tui``
# in #269; for now they are simply available next to ``apply_ops`` so unit
# tests and the eventual public-API wiring can both reach them. ``set_item``
# is named that way (not ``set``) because shadowing the builtin via
# ``from browse_tui import set`` would be hostile to recipes.

def upsert(id, parent_id, *, where=None, **fields):
    """Construct an ``("upsert", id, parent_id, fields[, where])`` op tuple.

    ``where`` is an optional positioning descriptor — see
    ``_resolve_where_index`` for the tuple shape and semantics. When
    omitted, the legacy 4-tuple form is emitted; with ``where`` set,
    a 5-tuple is emitted that ``apply_ops`` recognises.
    """
    if where is None:
        return ('upsert', id, parent_id, fields)
    return ('upsert', id, parent_id, fields, where)


def set_item(id, parent_id, *, where=None, **fields):
    """Construct a ``("set", id, parent_id, fields[, where])`` op tuple.

    See ``upsert`` for the ``where`` argument.
    """
    if where is None:
        return ('set', id, parent_id, fields)
    return ('set', id, parent_id, fields, where)


# Sentinel for ``mod``'s ``parent_id`` argument meaning "don't touch
# the parent". Distinct from ``None`` (which means root for
# ``state.root_id is None`` setups, or an explicit None-parent
# otherwise) and from any string id.
class _KeepParent:
    __slots__ = ()

    def __repr__(self):
        return 'KEEP_PARENT'


KEEP_PARENT = _KeepParent()


# Cursor-anchor sentinels — positional pins for tail-follow UX.
#
# When the user presses "go to first" or "go to last", the anchor
# becomes ``[PIN_FIRST]`` or ``[PIN_LAST]`` instead of a tier-list of
# ids. ``_apply_cursor_anchor`` resolves the sentinel to row 0 or
# ``len(visible) - 1`` on every background mutation, so the cursor
# follows the edge as new items arrive. Any non-home/non-end cursor
# movement drops the pin. See
# ``docs/superpowers/specs/2026-05-17-cursor-pin-design.md``.
class _AnchorSentinel:
    __slots__ = ('_kind',)

    def __init__(self, kind):
        self._kind = kind

    def __repr__(self):
        return f'<PIN_{self._kind.upper()}>'


PIN_FIRST = _AnchorSentinel('first')
PIN_LAST = _AnchorSentinel('last')


def mod(id, parent_id=KEEP_PARENT, *, where=None, **fields):
    """Construct a ``("mod", id, parent_id, fields[, where])`` op tuple.

    ``mod`` is the patch-only counterpart to ``upsert`` / ``set``:
    if the id is unknown the op is a silent no-op (no insert). Use
    it for safe field updates when the id might not yet exist (e.g.
    streaming sources).

    ``parent_id`` defaults to ``KEEP_PARENT`` — don't change the
    parent. An explicit value (str id or ``None``) triggers a reparent
    of the existing row.

    ``where`` is honoured when the id exists; since ``mod`` never
    inserts, positioning always implies repositioning (the
    ``"reposition"`` flag in the options slot is unnecessary).
    """
    if where is None:
        return ('mod', id, parent_id, fields)
    return ('mod', id, parent_id, fields, where)


def remove(id):
    """Construct a ``("remove", id)`` op tuple."""
    return ('remove', id)


def clear_children(parent_id):
    """Construct a ``("clear_children", parent_id)`` op tuple."""
    return ('clear_children', parent_id)


def complete(parent_id):
    """Construct a ``("complete", parent_id)`` op tuple."""
    return ('complete', parent_id)


def incomplete(parent_id):
    """Construct an ``("incomplete", parent_id)`` op tuple."""
    return ('incomplete', parent_id)


# Preview-cache op constructors (#446). These mirror the corresponding
# ``Browser`` single-call methods but produce op tuples that can be
# batched alongside tree-mutation ops in a single ``update_data`` call.
# Helper names take an ``_op`` suffix to keep them from colliding with
# the Browser method names when both are re-exported from ``browse_tui``.

def set_preview_op(id, text):
    """Construct a ``("set_preview", id, text)`` op tuple.

    Effect on apply: ``item.preview = text or ''`` and the wrap-cache
    ``item.preview_render`` is dropped. No-op if ``id`` is not in
    ``state._items_by_id`` (registration prerequisite — same as
    :meth:`Browser.set_preview`).
    """
    return ('set_preview', id, text)


def append_preview_op(id, chunk):
    """Construct a ``("append_preview", id, chunk)`` op tuple.

    Effect on apply: read-modify-write on ``item.preview`` (None ->
    ``''`` -> append). The wrap-cache is extended in place via the
    recorded tail offsets when the cached geometry matches, otherwise
    dropped. No-op if ``id`` is unknown.
    """
    return ('append_preview', id, chunk)


def clear_preview_op(id):
    """Construct a ``("clear_preview", id)`` op tuple.

    Effect on apply: ``item.preview = None`` and the wrap-cache is
    dropped. No-op if ``id`` is unknown.
    """
    return ('clear_preview', id)


def invalidate_preview_op(id):
    """Construct an ``("invalidate_preview", id)`` op tuple.

    Effect on apply: ``item.preview = None`` and the wrap-cache is
    dropped (when ``id`` is known), then a worker re-fetch is kicked
    via ``request_preview(id)`` regardless of whether the id was
    known — mirrors :meth:`Browser.invalidate_preview`.
    """
    return ('invalidate_preview', id)


def drop_preview_cache_op(id=None):
    """Construct a ``("drop_preview_cache", id_or_none)`` op tuple.

    Effect on apply: drop ``item.preview`` and the wrap-cache for the
    named ``id`` (one item) or for every loaded Item (``id=None``).
    When the dropped id matches the preview-cursor (or all are dropped),
    a worker kick is staged for the cursor id — mirrors
    :meth:`Browser.drop_preview_cache`.
    """
    return ('drop_preview_cache', id)


def _item_field_names():
    """Return the set of declared dataclass field names on ``Item``.

    ``Item`` is resolved from this module's globals at call time —
    production builds concatenate ``030-data.py`` ahead of this file so
    the name is bound; standalone test loads inject it after import via
    ``_state.Item = _data.Item``. Computing this each call keeps the
    helper safe to call at any point in the load sequence.
    """
    return {f.name for f in _dc_fields(Item)}


def _drop_subtree_indexes(state: State, item_id) -> None:
    """Recursively drop ``_items_by_id`` / ``_parent_of_id`` for a subtree.

    Used by ``remove`` and ``clear_children``: when an item is being
    discarded we also drop every descendant whose subtree is going with
    it. Each level's ``_children`` entry (if any) is popped along the
    way. Safe for unknown ids — recursion bottoms out at ids without
    a ``_children`` entry.
    """
    children = state._children.pop(item_id, None)
    state._loading.pop(item_id, None)
    if not children:
        return
    for child in children:
        cid = child.id
        # Drop the reverse index only if it still points at this parent
        # — guards against stale rows after a reparent earlier in the
        # same batch.
        if state._parent_of_id.get(cid) == item_id:
            state._parent_of_id.pop(cid, None)
            state._items_by_id.pop(cid, None)
        _drop_subtree_indexes(state, cid)


# Positioning descriptor for ``upsert`` / ``set`` ops.
#
# Shape: ``(TYPE, OPTIONS [, REFERENCE])``
#   TYPE:      "first" | "last" | "before" | "after"
#   OPTIONS:   None  | frozenset of strings  (currently only "reposition")
#   REFERENCE: required for "before"/"after"; int (child index, clamped)
#              or str (child id, falls back to first/last when missing)
#
# See ``docs/superpowers/specs/2026-05-15-update-data-positioning-design.md``.

_VALID_WHERE_KEYWORDS = frozenset({'first', 'last', 'before', 'after'})
_VALID_WHERE_OPTIONS = frozenset({'reposition'})


def _validate_where(where) -> None:
    """Validate a ``where`` descriptor. Raises ``ValueError`` if malformed."""
    if not isinstance(where, tuple):
        raise ValueError(
            f'where must be a tuple, got {type(where).__name__}'
        )
    if len(where) not in (2, 3):
        raise ValueError(
            f'where tuple must have 2 or 3 elements, got {len(where)}'
        )
    type_ = where[0]
    if type_ not in _VALID_WHERE_KEYWORDS:
        raise ValueError(
            f'unknown where keyword: {type_!r} '
            f'(expected one of {sorted(_VALID_WHERE_KEYWORDS)})'
        )
    opts = where[1]
    if opts is not None and not isinstance(opts, (set, frozenset)):
        raise ValueError(
            f'where options must be None or a (frozen)set, '
            f'got {type(opts).__name__}'
        )
    if opts:
        unknown = set(opts) - _VALID_WHERE_OPTIONS
        if unknown:
            raise ValueError(
                f'unknown where option(s): {sorted(unknown)} '
                f'(known: {sorted(_VALID_WHERE_OPTIONS)})'
            )
    if type_ in ('before', 'after'):
        if len(where) != 3:
            raise ValueError(
                f'{type_!r} requires a reference (3-tuple)'
            )
        ref = where[2]
        if not isinstance(ref, (int, str)):
            raise ValueError(
                f'where reference must be int or str, '
                f'got {type(ref).__name__}'
            )
    # For "first"/"last" a 3-tuple is tolerated; the reference slot is
    # silently ignored (forgiving — the parser dispatches on TYPE).


def _resolve_where_index(children_list, where, self_id=None):
    """Compute the insertion index for ``where`` given current ``children_list``.

    Returns the target index (an int in ``[0, len(children_list)]``), or
    ``None`` if ``self_id`` resolves to the pivot — the caller should
    treat that as a no-op on position (per spec same-id rule).

    Resolution rules:
      * "first" -> 0
      * "last"  -> len(children_list)
      * "before"/"after" with int reference:
          - ref < 0          -> collapse to first (0)
          - ref >= len       -> collapse to last (len)
          - 0 <= ref < len   -> use children_list[ref] as pivot
      * "before"/"after" with str reference:
          - id present       -> use that child as pivot
          - id missing       -> "before"->first, "after"->last
      * Same-id pivot (pivot resolves to ``self_id``) -> None

    The collapse rule is intentionally direction-independent: out-of-range
    int and missing str both go to the nearest edge, not direction-shifted
    by one position.
    """
    type_ = where[0]
    if type_ == 'first':
        return 0
    if type_ == 'last':
        return len(children_list)
    # before / after
    ref = where[2]
    if isinstance(ref, int):
        if ref < 0:
            return 0
        if ref >= len(children_list):
            return len(children_list)
        idx = ref
    else:
        idx = None
        for i, child in enumerate(children_list):
            if child.id == ref:
                idx = i
                break
        if idx is None:
            return 0 if type_ == 'before' else len(children_list)
    if self_id is not None and children_list[idx].id == self_id:
        return None
    return idx if type_ == 'before' else idx + 1


def _apply_upsert(state: State, id_, parent_id, fields, where=None) -> bool:
    """Apply one ``upsert`` op. Returns True if structure changed.

    ``where`` (optional) is a positioning descriptor — see
    ``_resolve_where_index`` for shape and semantics. When omitted, new
    ids append at the end and existing ids keep their current position
    (legacy behaviour).
    """
    if where is not None:
        _validate_where(where)
    known = _item_field_names()
    existing = state._items_by_id.get(id_)

    if existing is None:
        # New id. ``parent_id is None`` with an unknown id is a silent
        # debug-level drop per spec — patch-only upserts targeting
        # unknown ids are tolerated (out-of-order pushes from background
        # sources hit this naturally).
        #
        # Exception: when ``state.root_id is None`` (the framework
        # default), ``parent_id=None`` *is* the root and the upsert is
        # meant to insert under root — used by the children worker
        # delivering for a None-rooted Browser (#271). Disambiguate via
        # ``state.root_id``: only treat ``parent_id=None`` as patch-only
        # when the root id is something else.
        if parent_id is None and state.root_id is not None:
            return False
        # Construct a fresh Item with the known fields, then attach
        # any unknown keys as custom attrs (mirrors ``to_item`` for
        # dicts).
        item_kwargs = {k: v for k, v in fields.items() if k in known}
        extras = {k: v for k, v in fields.items() if k not in known}
        # ``id`` always comes from the op tuple, not from ``fields``.
        item_kwargs['id'] = id_
        item = Item(**item_kwargs)
        for k, v in extras.items():
            setattr(item, k, v)
        # Insert under parent. Orphan upserts (unknown parent) are
        # allowed — the cache entry is created on demand.
        children_list = state._children.setdefault(parent_id, [])
        if where is None:
            children_list.append(item)
        else:
            # ``self_id`` lets same-id pivot resolve to None; for new
            # ids it can only happen via str-pivot that doesn't match
            # anything (handled by the missing-id fallback) so we never
            # actually see ``None`` here in practice.
            idx = _resolve_where_index(children_list, where, self_id=id_)
            if idx is None:
                idx = len(children_list)
            children_list.insert(idx, item)
        state._items_by_id[id_] = item
        state._parent_of_id[id_] = parent_id
        return True

    # Existing id. Patch-merge: matching keys override Item fields,
    # unmatched keys land as custom attrs. Mutate in place so other
    # references (visible cache, selection set) keep working.
    for k, v in fields.items():
        setattr(existing, k, v)
    # Wrap cache (#422): the Item is being mutated. Drop both the raw
    # preview cache and the wrapped render — the displayed body may
    # depend on any patched field (title in composed umbrellas, tag in
    # status-driven summaries, etc.). Recipes that compose previews
    # from sibling Items lean on this invalidation.
    #
    # Gate (#445): skip the drop for a no-op patch. The documented
    # idempotent-ensure idiom (``upsert(id, parent)`` with no fields,
    # used to register an Item before ``set_preview``) and reposition-
    # only calls (``where=`` only) should not nuke a cached preview.
    if fields:
        existing.preview = None
        existing.preview_render = None

    if parent_id is None:
        # Patch-only: leave parent unchanged. Mutating fields like
        # ``has_children`` / ``title`` / ``hidden`` affects what the
        # visible-tree builder emits, so this is structural.
        return True

    reposition = (
        where is not None and 'reposition' in (where[1] or frozenset())
    )
    old_parent = state._parent_of_id.get(id_)
    if old_parent != parent_id:
        # Reparent: remove from old parent's child list (if cached),
        # insert into the new parent's list at ``where`` (or append if
        # ``where`` is None). The reposition flag is irrelevant for
        # reparent — moving to a new parent always places the row.
        old_list = state._children.get(old_parent)
        if old_list:
            for i, child in enumerate(old_list):
                if child.id == id_:
                    del old_list[i]
                    break
        new_list = state._children.setdefault(parent_id, [])
        if where is None:
            new_list.append(existing)
        else:
            idx = _resolve_where_index(new_list, where, self_id=id_)
            if idx is None:
                idx = len(new_list)
            new_list.insert(idx, existing)
        state._parent_of_id[id_] = parent_id
    elif reposition:
        # Reposition within the same parent. Compute the target index
        # against the original list (including the existing item), then
        # move atomically.
        children_list = state._children.get(parent_id)
        if children_list is None:
            # Defensive — existing item but no children list? Recreate.
            state._children[parent_id] = [existing]
            return True
        target_idx = _resolve_where_index(
            children_list, where, self_id=id_
        )
        if target_idx is None:
            # Same-id pivot — leave position unchanged.
            return True
        cur_idx = None
        for i, child in enumerate(children_list):
            if child.id == id_:
                cur_idx = i
                break
        if cur_idx is None:
            # Out-of-sync state — existing recorded but not in parent's
            # children list. Insert defensively at the target index.
            if target_idx > len(children_list):
                target_idx = len(children_list)
            children_list.insert(target_idx, existing)
            return True
        del children_list[cur_idx]
        if target_idx > cur_idx:
            target_idx -= 1
        children_list.insert(target_idx, existing)
    # else: existing same-parent, no reposition -> keep current position
    return True


def _apply_set(state: State, id_, parent_id, fields, where=None) -> bool:
    """Apply one ``set`` op. Returns True if structure changed.

    ``where`` (optional) is a positioning descriptor — see
    ``_resolve_where_index``. Like ``upsert``, new ids honour ``where``;
    existing ids in the same parent keep their position unless the
    ``"reposition"`` flag is set.
    """
    if where is not None:
        _validate_where(where)
    known = _item_field_names()
    # Full replace: ``fields`` is the entire record. Unspecified Item
    # fields revert to dataclass defaults; custom attrs are dropped. A
    # NEW Item instance is constructed (identity changes).
    item_kwargs = {k: v for k, v in fields.items() if k in known}
    extras = {k: v for k, v in fields.items() if k not in known}
    item_kwargs['id'] = id_
    item = Item(**item_kwargs)
    for k, v in extras.items():
        setattr(item, k, v)

    reposition = (
        where is not None and 'reposition' in (where[1] or frozenset())
    )
    old_parent = state._parent_of_id.get(id_)
    if old_parent is None and id_ not in state._items_by_id:
        # New id under the supplied parent_id.
        children_list = state._children.setdefault(parent_id, [])
        if where is None:
            children_list.append(item)
        else:
            idx = _resolve_where_index(children_list, where, self_id=id_)
            if idx is None:
                idx = len(children_list)
            children_list.insert(idx, item)
        state._items_by_id[id_] = item
        state._parent_of_id[id_] = parent_id
        return True

    # Replace existing. The id keeps its place in the parent's child
    # list when the parent is unchanged (or moves to the computed
    # position when reposition is requested); on reparent it inserts
    # at ``where`` (or appends if ``where`` is None).
    if old_parent == parent_id:
        old_list = state._children.get(old_parent)
        if old_list is None:
            state._children[parent_id] = [item]
        elif reposition:
            target_idx = _resolve_where_index(
                old_list, where, self_id=id_
            )
            cur_idx = None
            for i, child in enumerate(old_list):
                if child.id == id_:
                    cur_idx = i
                    break
            if target_idx is None:
                # Same-id pivot — replace in place.
                if cur_idx is not None:
                    old_list[cur_idx] = item
                else:
                    old_list.append(item)
            else:
                if cur_idx is None:
                    if target_idx > len(old_list):
                        target_idx = len(old_list)
                    old_list.insert(target_idx, item)
                else:
                    del old_list[cur_idx]
                    if target_idx > cur_idx:
                        target_idx -= 1
                    old_list.insert(target_idx, item)
        else:
            # No reposition: replace in place at current index.
            for i, child in enumerate(old_list):
                if child.id == id_:
                    old_list[i] = item
                    break
            else:
                old_list.append(item)
    else:
        # Reparent.
        old_list = state._children.get(old_parent)
        if old_list:
            for i, child in enumerate(old_list):
                if child.id == id_:
                    del old_list[i]
                    break
        new_list = state._children.setdefault(parent_id, [])
        if where is None:
            new_list.append(item)
        else:
            idx = _resolve_where_index(new_list, where, self_id=id_)
            if idx is None:
                idx = len(new_list)
            new_list.insert(idx, item)
        state._parent_of_id[id_] = parent_id

    state._items_by_id[id_] = item
    # ``_children[id_]`` (the children OF this id, as a parent) is
    # preserved — they belong to the id, not to the Item instance.
    return True


def _apply_mod(state: State, id_, parent_id, fields, where=None) -> bool:
    """Apply one ``mod`` op. Returns True if structure changed.

    Patch-only: never inserts. Unknown id is a silent no-op. When
    ``parent_id is KEEP_PARENT`` the parent is left untouched;
    otherwise the row is reparented (``None`` means root for
    ``state.root_id is None`` setups). ``where`` (optional) repositions
    the row in its target parent's children list; reposition is
    implicit (no need for the ``"reposition"`` options flag).
    """
    if where is not None:
        _validate_where(where)
    # Validate parent_id shape — KEEP_PARENT sentinel, str, or None.
    if (
        parent_id is not KEEP_PARENT
        and parent_id is not None
        and not isinstance(parent_id, str)
    ):
        raise ValueError(
            f'mod parent_id must be a str, None, or KEEP_PARENT '
            f'(got {type(parent_id).__name__})'
        )
    existing = state._items_by_id.get(id_)
    if existing is None:
        # Unknown id — silent no-op. Streaming tolerance.
        return False
    # Patch-merge fields. Drop ``id`` from the patch — the op's id is
    # authoritative (matches the upsert convention). Field patches on
    # an existing row are treated as structural (same posture as
    # ``_apply_upsert`` existing-id branch — title/tag/hidden all
    # affect the visible cache and the rendered output).
    fields_no_id = {k: v for k, v in fields.items() if k != 'id'}
    for k, v in fields_no_id.items():
        setattr(existing, k, v)
    # Wrap cache (#422): the Item is being mutated. Drop both the raw
    # preview cache and the wrapped render. See ``_apply_upsert`` for
    # the rationale.
    #
    # Gate (#445): skip the drop for a no-op patch (no real fields,
    # ignoring the discarded ``id`` key). Mirrors ``_apply_upsert``:
    # mutate -> invalidate; no-op -> preserve cache.
    if fields_no_id:
        existing.preview = None
        existing.preview_render = None
    structural = True

    if parent_id is KEEP_PARENT:
        # No reparent; no repositioning unless ``where`` was given.
        if where is None:
            return structural
        # Reposition within current parent.
        cur_parent = state._parent_of_id.get(id_)
        children_list = state._children.get(cur_parent)
        if children_list is None:
            return structural
        target_idx = _resolve_where_index(
            children_list, where, self_id=id_
        )
        if target_idx is None:
            # Same-id pivot — leave position unchanged.
            return structural
        cur_idx = None
        for i, child in enumerate(children_list):
            if child.id == id_:
                cur_idx = i
                break
        if cur_idx is None:
            return structural
        del children_list[cur_idx]
        if target_idx > cur_idx:
            target_idx -= 1
        children_list.insert(target_idx, existing)
        return True

    # Reparent path. ``parent_id`` is a real id or ``None``.
    old_parent = state._parent_of_id.get(id_)
    if old_parent != parent_id:
        old_list = state._children.get(old_parent)
        if old_list:
            for i, child in enumerate(old_list):
                if child.id == id_:
                    del old_list[i]
                    break
        new_list = state._children.setdefault(parent_id, [])
        if where is None:
            new_list.append(existing)
        else:
            idx = _resolve_where_index(new_list, where, self_id=id_)
            if idx is None:
                idx = len(new_list)
            new_list.insert(idx, existing)
        state._parent_of_id[id_] = parent_id
        return True
    # Same parent — reposition only if ``where`` given.
    if where is not None:
        children_list = state._children.get(parent_id)
        if children_list is None:
            state._children[parent_id] = [existing]
            return True
        target_idx = _resolve_where_index(
            children_list, where, self_id=id_
        )
        if target_idx is None:
            return structural
        cur_idx = None
        for i, child in enumerate(children_list):
            if child.id == id_:
                cur_idx = i
                break
        if cur_idx is None:
            return structural
        del children_list[cur_idx]
        if target_idx > cur_idx:
            target_idx -= 1
        children_list.insert(target_idx, existing)
        return True
    return structural


def _apply_remove(state: State, id_) -> bool:
    """Apply one ``remove`` op. Returns True if structure changed."""
    if id_ not in state._items_by_id:
        # Unknown id — silent no-op. ``remove`` is the natural way for
        # streaming sources to retract items, so out-of-order pushes
        # may target ids we've already dropped.
        return False
    parent_id = state._parent_of_id.pop(id_, None)
    state._items_by_id.pop(id_, None)
    parent_list = state._children.get(parent_id)
    if parent_list:
        for i, child in enumerate(parent_list):
            if child.id == id_:
                del parent_list[i]
                break
    # Cascade: drop the item's own subtree (children of ``id_`` as a
    # parent), recursively cleaning up indexes for descendants.
    _drop_subtree_indexes(state, id_)
    return True


def _apply_clear_children(state: State, parent_id) -> bool:
    """Apply one ``clear_children`` op. Returns True if structure changed."""
    children = state._children.get(parent_id)
    if children:
        # Recursively drop indexes for each child's subtree. We can't
        # rely on ``_index_drop_children`` here because we also need to
        # cascade into grandchildren (their subtrees go away too).
        for child in list(children):
            cid = child.id
            if state._parent_of_id.get(cid) == parent_id:
                state._parent_of_id.pop(cid, None)
                state._items_by_id.pop(cid, None)
            _drop_subtree_indexes(state, cid)
    # Cache entry reverts to "no fetch yet" — drop the dict entry
    # entirely so the visible-tree builder shows a placeholder if the
    # parent is expanded (matches the pre-fetch state).
    had_entry = parent_id in state._children
    state._children.pop(parent_id, None)
    # Spec: "loading flag is reset accordingly" — we set False (the
    # parent is in a known not-loading state; any future fetch will
    # flip it back to True via dispatch). NOT a settlement: the cache
    # entry was just dropped above, so ``cached_children`` would be
    # ``None`` — firing ``on_children_loaded`` here would break its
    # "children present at fire time" contract. Hence the bare
    # assignment (no ``_set_loading(..., settled=True)``).
    state._loading[parent_id] = False
    return had_entry or bool(children)


def _apply_set_preview(state: State, id_, text) -> None:
    """Apply one ``set_preview`` op — body of :meth:`Browser.set_preview`.

    Coerces ``text=None`` to ``''``; writes ``item.preview`` and drops
    the wrap cache. No-op when the id is not registered.
    """
    item = state._items_by_id.get(id_)
    if item is None:
        return
    item.preview = text if text is not None else ''
    item.preview_render = None


def _apply_append_preview(state: State, id_, chunk, preview_ansi) -> None:
    """Apply one ``append_preview`` op — body of :meth:`Browser.append_preview`.

    Read-modify-write on ``item.preview``; tries to extend the wrap
    cache in place via ``_extend_or_drop_preview_render``. No-op when
    the id is not registered. ``chunk=None`` coerces to ``''``.
    """
    item = state._items_by_id.get(id_)
    if item is None:
        return
    if chunk is None:
        chunk = ''
    cur = item.preview if item.preview is not None else ''
    item.preview = cur + chunk
    _extend_or_drop_preview_render(item, chunk, preview_ansi)


def _apply_clear_preview(state: State, id_) -> None:
    """Apply one ``clear_preview`` op — body of :meth:`Browser.clear_preview`.

    Drops the raw preview text and the wrap cache. No-op when the id
    is not registered.
    """
    item = state._items_by_id.get(id_)
    if item is None:
        return
    item.preview = None
    item.preview_render = None


def _apply_drop_preview_cache(state: State, id_) -> None:
    """Apply one ``drop_preview_cache`` op — body of :meth:`Browser.drop_preview_cache`.

    When ``id_`` is None, drops cache on every loaded Item; otherwise
    drops the named one (silent no-op when unknown). The post-batch
    "kick the cursor if its preview was dropped" decision lives on
    Browser — ``apply_ops`` records the intent on
    ``state._preview_kicks`` and ``update_data._apply`` resolves it.
    """
    if id_ is None:
        for item in state._items_by_id.values():
            item.preview = None
            item.preview_render = None
        return
    item = state._items_by_id.get(id_)
    if item is None:
        return
    item.preview = None
    item.preview_render = None


def apply_ops(state: State, ops, *, preview_ansi: bool = True) -> None:
    """Apply a list of ``update_data`` ops to ``state`` in order.

    Pure state mutation — no threading, no rendering. Each op is a
    tagged tuple; see Section 2 of the streaming-push design doc for
    the vocabulary. Ops apply in list order; reparenting in one op is
    visible to subsequent ops in the same batch. Flips
    ``_visible_dirty`` if any op affected the visible structure
    (anything that touched ``_children``).

    Side-effect signals for preview ops (#446) are written to
    ``state._preview_dirty`` (bool) and ``state._preview_kicks``
    (list). Both slots are reset at the start of every call so the
    caller can read the per-batch outcome without manual bookkeeping.
    ``state._preview_kicks`` carries entries that ``update_data._apply``
    translates into ``request_preview`` calls:
      * ``('id', cid)``           — invalidate_preview kick (unconditional).
      * ``('cursor', None)``      — drop_preview_cache with id=None.
      * ``('cursor_if', cid)``    — drop_preview_cache for a specific id;
                                    Browser kicks only when ``cid`` matches
                                    the current preview cursor.

    ``preview_ansi`` is forwarded to ``_extend_or_drop_preview_render``
    for ``append_preview`` ops. The default matches
    :attr:`BrowserConfig.preview_ansi` so standalone unit tests don't
    need to thread the value through.

    Unknown ops raise ``ValueError`` — silent drops would mask recipe
    typos.
    """
    structural = False
    preview_dirty = False
    preview_kicks = []
    # Reset the per-batch settlement list before processing so the
    # caller reads only this batch's settled parents (mirrors the
    # ``_preview_kicks`` reset above). ``_set_loading(..., settled=True)``
    # — fired by the ``complete`` op below — appends to it.
    state._settled_parents = []
    for op in ops:
        kind = op[0]
        if kind == 'upsert':
            # 4-tuple (legacy) or 5-tuple (with positioning descriptor).
            _, id_, parent_id, fields, *rest = op
            where = rest[0] if rest else None
            if _apply_upsert(state, id_, parent_id, fields, where):
                structural = True
        elif kind == 'set':
            _, id_, parent_id, fields, *rest = op
            where = rest[0] if rest else None
            if _apply_set(state, id_, parent_id, fields, where):
                structural = True
        elif kind == 'mod':
            _, id_, parent_id, fields, *rest = op
            where = rest[0] if rest else None
            if _apply_mod(state, id_, parent_id, fields, where):
                structural = True
        elif kind == 'remove':
            _, id_ = op
            if _apply_remove(state, id_):
                structural = True
        elif kind == 'clear_children':
            _, parent_id = op
            if _apply_clear_children(state, parent_id):
                structural = True
        elif kind == 'complete':
            _, parent_id = op
            # Genuine settlement: children are now available (the upserts
            # in this same batch, or an empty list). Record so
            # ``on_children_loaded`` fires for ``parent_id``.
            _set_loading(state, parent_id, False, settled=True)
        elif kind == 'incomplete':
            _, parent_id = op
            state._loading[parent_id] = True
        elif kind == 'set_preview':
            _, id_, text = op
            _apply_set_preview(state, id_, text)
            preview_dirty = True
        elif kind == 'append_preview':
            _, id_, chunk = op
            _apply_append_preview(state, id_, chunk, preview_ansi)
            preview_dirty = True
        elif kind == 'clear_preview':
            _, id_ = op
            _apply_clear_preview(state, id_)
            preview_dirty = True
        elif kind == 'invalidate_preview':
            _, id_ = op
            _apply_clear_preview(state, id_)
            # Worker kick fires regardless of whether the id was
            # registered — mirrors :meth:`Browser.invalidate_preview`.
            preview_kicks.append(('id', id_))
            preview_dirty = True
        elif kind == 'drop_preview_cache':
            _, id_ = op
            _apply_drop_preview_cache(state, id_)
            # Cursor-kick decision belongs to Browser (it owns
            # ``_preview_cursor_id``). Record the intent for the caller
            # to resolve.
            if id_ is None:
                preview_kicks.append(('cursor', None))
            else:
                preview_kicks.append(('cursor_if', id_))
            preview_dirty = True
        else:
            raise ValueError(f'apply_ops: unknown op kind {kind!r}')
    if structural:
        state._visible_dirty = True
    state._preview_dirty = preview_dirty
    state._preview_kicks = preview_kicks


# ---- helpers for the children-worker → update_data delivery (#271) -------


def _fields_of_item(item) -> dict:
    """Materialise the patchable fields of an Item for an ``upsert`` op.

    Returns a dict of every dataclass field on Item PLUS any custom
    attributes the recipe attached (recipes commonly stick ``size`` /
    ``mtime`` / ``path`` / ``parent`` / ``depth`` etc. on Items via
    ``to_item`` dict-mode or direct ``setattr``). ``id`` is excluded
    (it's the op key, not a patched field). ``parent_id`` is also
    excluded if present as a custom attr — the tuple-op carries the
    parent separately, and a stray ``parent`` field would shadow the
    op's parent_id and confuse downstream readers.

    Reads ``item.__dict__`` directly so non-slotted Items (the default)
    surface both dataclass fields and recipe-attached extras in one
    pass.
    """
    out = {}
    for k, v in item.__dict__.items():
        if k == 'id':
            continue
        out[k] = v
    return out


def _build_children_batch(parent_id, items) -> list:
    """Build the worker delivery batch: upserts followed by ``complete``.

    Per the streaming-push design (Section 3), the children worker's
    delivery is one atomic batch: every item is upserted under
    ``parent_id`` (preserving custom attrs), then a trailing
    ``complete(parent_id)`` op flips ``_loading[parent_id]`` to False
    in the same drain. An empty ``items`` yields just
    ``[complete(parent_id)]`` — the trailing clear is unconditional.
    """
    batch = [
        ('upsert', it.id, parent_id, _fields_of_item(it))
        for it in items
    ]
    batch.append(('complete', parent_id))
    return batch


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
         depth 0 as a normal row; subsequent children start at depth 1.
         The scope row is always expanded (``scope_into`` auto-adds the
         scope id to ``state.expanded``) so children render inline
         beneath it.
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

    if state.scope_stack:
        # Treat the scope row as a normal expanded parent. _emit_children
        # uniformly handles: emit the row at depth 0, check
        # ``has_children`` + ``id in state.expanded``, recurse into the
        # cached children list at depth 1 (or emit a pending placeholder
        # at depth 1 when the children fetch is still in flight). No
        # special-cased ``base_depth`` arithmetic and no separate
        # expansion / pending logic for the scope row — the scope row
        # IS just a parent that happens to sit at depth 0.
        #
        # Recover the actual Item via _find_item; fall back to a
        # synthetic stub if not findable. The synthetic is registered
        # in ``_items_by_id`` (so the per-Item preview cache has a
        # place to land — see #422 / #442) and flagged ``synthetic=True``
        # so ``_promote_synthetics`` can promote it in place when the
        # parent's children fetch later arrives. ``has_children=True``
        # on the stub keeps the expand glyph rendered — you can only
        # scope INTO an item with children, and recipes that scope to a
        # pre-known id (``initial_scope``) honour the same invariant.
        scope_root_id = current_scope(state)
        scope_item = _find_item(state, scope_root_id)
        if scope_item is None:
            scope_item = state._items_by_id.get(scope_root_id)
        if scope_item is None:
            scope_item = Item(
                id=scope_root_id,
                title=str(scope_root_id),
                has_children=True,
            )
            scope_item.synthetic = True
            state._items_by_id[scope_root_id] = scope_item
        _emit_children(state, [scope_item], 0, out)
    else:
        # Unscoped: emit root's children at depth 0. ``_emit_children``
        # handles None gracefully via its caller — at root we just
        # render an empty content area when the cache is empty.
        children = state._children.get(state.root_id)
        if children is not None:
            _emit_children(state, children, 0, out)

    state._visible_cache = out
    state._visible_dirty = False
    return out


def _emit_children(state, children, depth, out):
    """Emit ``children`` at ``depth``, recursing into expanded items.

    Iterative DFS — uses an explicit worklist. Each frame is
    ``(siblings_iter, depth)``; we push deeper frames as we descend.

    Items with ``hidden=True`` are skipped entirely: no row is
    emitted, and no recursion into their subtree happens. This is
    the render-only cascade for the per-row visibility flag — see
    ``docs/superpowers/specs/2026-05-16-row-visibility-design.md``.
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
        if getattr(child, 'hidden', False):
            # Hidden row: emit nothing and skip its subtree. The flag
            # is per-row; descendants' own ``hidden`` values are
            # preserved and take effect once the ancestor is shown.
            continue
        if state._filter_active and getattr(child, '_filter_hidden', False):
            # Filter-hidden: a row whose subtree contributes no
            # filter match. Per-row skip, not a subtree cascade — the
            # bottom-up evaluator only flags this on rows whose entire
            # subtree fails, so any matching descendant has its own
            # ``_filter_hidden=False`` and stays reachable via its
            # (now visible) ancestor. See
            # ``docs/superpowers/specs/2026-05-17-filter-design.md``.
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


def _search_text(item, *, show_ids='auto', is_current_scope=False):
    """Return the searchable haystack for an Item, aligned with the display.

    Includes the same segments the renderer would emit so search /
    filter matches match what the user actually sees:

      * id segment — only when it would be rendered (``show_ids`` resolves
        to visible per ``_id_visible``: ``'always'`` always, ``'auto'``
        when ``str(id) != title``, ``'never'`` never). Without this gate
        a recipe like browse-claude (``show_ids='never'``, voice ids
        carry the full file path) would match every row when the user
        searches for a path fragment that's only visible on the scope
        header — see scope-root unification design.
      * title segment — replaced by ``item.scope_title`` for the
        current-scope row, mirroring the renderer's label override.
      * bracketed tag — included so ``open`` matches ``[open]`` even
        when it isn't in the title.
    """
    parts = []
    id_visible = (
        show_ids == 'always'
        or (show_ids != 'never' and str(item.id) != item.title)
    )
    if id_visible:
        parts.append(str(item.id))
    label = item.title
    if is_current_scope:
        scope_title = getattr(item, 'scope_title', None)
        if scope_title:
            label = scope_title
    if label:
        parts.append(label)
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


def _search_find(state, query, start_idx, direction=1, *, show_ids='auto'):
    """Find the next/prev visible item matching ``query``.

    Walks the visible list starting from ``start_idx`` in ``direction``
    (1 forward, -1 backward), wrapping around. Skips non-``'normal'``
    entries (``pending`` placeholders) so search never lands on a row
    without a real id. The scope row at depth 0 (when scoped) is a
    ``normal`` row and is searchable like any other. ``show_ids`` is
    plumbed through to ``_search_text`` so matches align with the
    rendered display (an id that isn't shown can't drive a match).
    Returns the visible-list index of the match, or ``None`` if no
    match exists.
    """
    vis = visible_items(state)
    if not vis or not query:
        return None
    scope_id = state.scope_stack[-1] if state.scope_stack else None
    n = len(vis)
    for step in range(1, n + 1):
        idx = (start_idx + step * direction) % n
        entry = vis[idx]
        if entry.kind != 'normal':
            continue
        text = _search_text(
            entry.item,
            show_ids=show_ids,
            is_current_scope=(entry.item.id == scope_id),
        )
        if _search_matches(text, query):
            return idx
    return None


def mark_cursor_changed(browser) -> None:
    """Flag the redraw set for a cursor-position change.

    Any code path that moves ``state.cursor`` MUST call this helper —
    the list pane needs to repaint the new selected row, the children
    grid needs to refresh to reflect the new cursor item's children,
    and the preview pane needs to re-render the new cursor item's
    preview text. Forgetting any one of those leaves a stale pane
    until the next user keystroke (regression in #206 / commit
    0c8769d, fix tracked under #223).

    Centralising the set here means new cursor-move sites just call
    one function instead of recopying a hand-written triplet. Also
    latches ``_cursor_change_pending`` so the main-loop drain can
    fire the ``on_cursor_change`` hook at most once per tick even
    when the cursor moves several times between drains.
    """
    rd = browser._needs_redraw
    rd.add('list')
    rd.add('children')
    rd.add('preview')
    browser._cursor_change_pending = True


def _search_jump_nearest(browser):
    """Jump cursor to the nearest match (forward search from current pos).

    Used by the search-mode key dispatcher: each keystroke that mutates
    the query nudges the cursor onto the first match at-or-after the
    current cursor (passing ``cursor - 1`` so the cursor *itself* can
    match, mirroring plan-tui).
    """
    state = browser._state
    idx = _search_find(
        state, browser._search_query, state.cursor - 1, 1,
        show_ids=browser.show_ids,
    )
    if idx is not None:
        state.cursor = idx
        mark_cursor_changed(browser)


# ---- interactive filter (`&`) evaluator ---------------------------------
#
# See docs/superpowers/specs/2026-05-27-filter-visible-tree-only-design.md
# (supersedes the optimistic-keep-visible evaluator from the original
# 2026-05-17-filter-design.md).
#
# DFS over the *visible* tree (the same shape ``_emit_children`` would
# walk with the filter off) writes ``Item._filter_hidden`` on every
# reachable item: True when the item does not match the active filter
# stack AND none of its currently-visible descendants do either. The
# renderer (``_emit_children``) skips rows whose ``_filter_hidden`` is
# True, guarded by ``state._filter_active`` so the check short-circuits
# when no filter is active.
#
# Reuses ``_search_text`` / ``_search_matches`` so the haystack rules
# and fragment-AND matcher are identical to ``/`` search.
#
# Key semantic rules (per the design):
#
#   1. The walk descends only through items in ``state.expanded`` —
#      collapsed parents are evaluated against their own searchable
#      text alone; their children are not consulted.
#   2. ``Item.hidden=True`` rows are skipped entirely (they aren't
#      "visible" and can't match or scaffold an ancestor).
#   3. No optimistic branch: a parent with ``has_children=True`` whose
#      children aren't cached contributes nothing — visibility depends
#      on the parent's own match alone.
#   4. The current-scope row is exempt: always treated as a match.


def _filter_visit_subtree(state, item, active, scope_id, show_ids):
    """Bottom-up DFS over a visible subtree, writing ``_filter_hidden``.

    Mirrors the per-item evaluation used by ``_recompute_filter_hidden``:
    descend through expanded cached children; the item passes iff its
    own text matches every active filter OR at least one visible
    descendant passes. Returns True iff the item itself should be
    visible (``_filter_hidden=False``).

    ``active`` is the pre-filtered list of truthy filter strings;
    ``scope_id`` is the current scope row id (or ``None`` when
    unscoped). Both are passed in to avoid recomputing per recursive
    call. This is shared between the full ``_recompute_filter_hidden``
    walk and the per-op dispatch's "force-evaluate a new item's
    subtree" hook (#499).
    """
    # Rule 2: recipe-hidden rows aren't part of the visible tree. No
    # flag write; the renderer would skip them regardless and they
    # can't scaffold an ancestor.
    if getattr(item, 'hidden', False):
        return False

    # Descend only into expanded subtrees (Rule 1). Collapsed children
    # contribute nothing.
    any_visible_desc_passes = False
    if item.has_children and item.id in state.expanded:
        for child in state._children.get(item.id, ()):
            if _filter_visit_subtree(
                state, child, active, scope_id, show_ids,
            ):
                any_visible_desc_passes = True

    is_scope = (item.id == scope_id)
    text = _search_text(
        item, show_ids=show_ids, is_current_scope=is_scope,
    )
    # Scope row exemption: always treated as a match.
    self_passes = is_scope or all(
        _search_matches(text, q) for q in active
    )
    item._filter_hidden = not (
        self_passes or any_visible_desc_passes
    )
    return not item._filter_hidden


def _recompute_filter_hidden(state, filters, *, show_ids='auto') -> None:
    """Re-evaluate filter visibility across the visible tree.

    ``filters`` is an iterable of filter strings (typically
    ``Browser._filters``). Empty strings are ignored — they're the
    placeholder slot used by the filter-edit prompt before the user has
    typed anything.

    ``show_ids`` is plumbed through to ``_search_text`` so the filter
    matches against the same haystack the user would search by ``/`` —
    a row whose id wouldn't render can't drive a filter match.

    No-op when no non-empty filter is present: existing
    ``_filter_hidden`` flags become stale-but-inert because the
    renderer guards on ``state._filter_active``. The next call with a
    non-empty filter overwrites every reachable item's flag.

    The current-scope row is exempt — it's always shown, regardless of
    whether it matches the filter, because hiding the row you're
    scoped *into* makes no sense (you'd lose the context of where you
    are). See scope-root unification design.
    """
    active = [q for q in filters if q]
    state._filter_active = bool(active)
    if not active:
        return

    scope_id = state.scope_stack[-1] if state.scope_stack else None

    # Walk the same shape ``_emit_children`` would walk with the filter
    # off: the scope row (when scoped) or root's children (unscoped).
    # The scope row is normally in ``state.expanded`` (``scope_into``
    # auto-adds it) so the DFS descends into its children.
    if state.scope_stack:
        scope_root_id = state.scope_stack[-1]
        scope_item = _find_item(state, scope_root_id)
        if scope_item is None:
            scope_item = state._items_by_id.get(scope_root_id)
        if scope_item is not None:
            _filter_visit_subtree(
                state, scope_item, active, scope_id, show_ids,
            )
    else:
        for child in state._children.get(state.root_id, ()):
            _filter_visit_subtree(
                state, child, active, scope_id, show_ids,
            )


# ---- per-op incremental filter propagation (ticket #498) ----------------
#
# ``_propagate_filter_status_up`` is the per-item dual of
# ``_recompute_filter_hidden`` above. Instead of a full walk over the
# visible tree, it re-evaluates ONE item's ``_filter_hidden`` flag and
# then walks upward through ancestors, terminating at the first ancestor
# whose flag value doesn't change. This is the "per-op incremental
# update" helper from
# ``docs/superpowers/specs/2026-05-27-filter-visible-tree-only-design.md``
# ("Per-op incremental update" section).
#
# Used by:
#   * ``update_data._apply`` dispatch (#499): per affected item.
#   * ``_do_expand`` subtree-revisit (#501): after the new subtree's
#     visit() runs, propagate up from the expand-parent so any flag
#     change reaches the scope row.
#
# Semantics mirror ``_recompute_filter_hidden``'s per-item evaluation:
#   1. ``hidden=True`` items contribute nothing (can't scaffold).
#   2. Only descendants reachable through ``state.expanded`` count.
#   3. The current scope row is exempt (always treated as match).
#   4. No optimistic branch for uncached children (matches the rewrite
#      in ticket #497).
#
# The walk upward is keyed off ``state._parent_of_id`` (the reverse
# index maintained by ``_index_add_children`` / ``_index_drop_children``).
# A missing entry means "no parent known" -> we've reached the top.


def _propagate_filter_status_up(state, item, filters, *, show_ids='auto'):
    """Re-evaluate one item's ``_filter_hidden`` and walk up ancestors.

    For each item visited the helper recomputes ``_filter_hidden`` from
    the item's own searchable text plus the current ``_filter_hidden``
    flags of its visible children (descendants reachable via
    ``state.expanded`` and not ``Item.hidden=True``). If the new flag
    value equals the existing one, the walk terminates: ancestors
    above are unaffected because the only signal they receive from
    this subtree is the cur item's flag.

    ``filters`` is iterable of filter strings; empty / whitespace-only
    handling matches ``_recompute_filter_hidden``: only truthy strings
    are honoured, and an all-empty list is a no-op.

    ``show_ids`` is plumbed through to ``_search_text`` so propagation
    sees the same haystack as the full walk.

    No-op when no non-empty filter is present. Recipe-``hidden=True``
    items count as "not visible, doesn't scaffold" — propagation still
    continues through them upward (the parent gets re-evaluated against
    the rest of its children).
    """
    active = [q for q in filters if q]
    if not active:
        return

    scope_id = state.scope_stack[-1] if state.scope_stack else None

    cur = item
    while cur is not None:
        if not getattr(cur, 'hidden', False):
            # Recompute cur's flag from self-match + visible children's
            # current flags. Recipe-hidden cur is skipped: it can't
            # scaffold an ancestor and the renderer drops it anyway;
            # we still walk upward so the parent re-evaluates against
            # its other (non-hidden) children.
            any_visible_desc_passes = False
            if cur.has_children and cur.id in state.expanded:
                for child in state._children.get(cur.id, ()):
                    if getattr(child, 'hidden', False):
                        continue
                    if not child._filter_hidden:
                        any_visible_desc_passes = True
                        break
            is_scope = (cur.id == scope_id)
            text = _search_text(
                cur, show_ids=show_ids, is_current_scope=is_scope,
            )
            self_passes = is_scope or all(
                _search_matches(text, q) for q in active
            )
            new_hidden = not (self_passes or any_visible_desc_passes)

            if cur._filter_hidden == new_hidden:
                # No change at this level: ancestors above us see the
                # same signal from this subtree as before. Early-out.
                return
            cur._filter_hidden = new_hidden

        # Walk up via the reverse-parent index. ``get()`` returns
        # ``None`` for either "no entry" (item not indexed) or
        # "parent_id is None" (item is at root level): both terminate
        # the walk — nothing above the conceptual root.
        parent_id = state._parent_of_id.get(cur.id)
        if parent_id is None:
            return
        cur = state._items_by_id.get(parent_id)


# ---- per-op filter dispatch (ticket #499) -------------------------------
#
# ``update_data._apply`` calls these to drive per-op filter propagation
# in place of the full ``_recompute_filter_hidden`` walk. The pre-scan
# captures the pre-mutation state needed to resolve "what id is
# affected, under what parent" before ``apply_ops`` runs (some ops —
# ``upsert`` of an existing id with ``parent_id=None`` patch-only,
# ``mod`` with ``KEEP_PARENT``, ``remove`` — only have answers in the
# pre-state).
#
# Dispatch table (per affected op, ``visible-expanded`` parent gating
# per Rule 2 of
# ``docs/superpowers/specs/2026-05-27-filter-visible-tree-only-design.md``):
#   * ``upsert`` / ``set`` / ``mod`` of an item under visible-expanded
#     parent: ``_propagate_filter_status_up(item, ...)``. The item's
#     own match may have changed (or it's a NEW item that needs its
#     flag set); walking up re-evaluates its parent's scaffold status.
#   * ``remove`` of an item under visible-expanded parent:
#     ``_propagate_filter_status_up(parent, ...)``. The parent may
#     have lost its only matching descendant.
#   * ``clear_children`` of a visible-expanded parent: same as remove
#     for the parent itself.
#   * Reparent (existing item moves between parents): propagate from
#     BOTH old and new parents — each may flip status.
#   * Any op under a collapsed parent: skip (Rule 2 — invisible
#     change).
#
# "Visible-expanded" parent = root (``parent_id is None`` or
# ``state.root_id``) OR a non-root id in ``state.expanded``. A parent
# that has been ``expand``-ed by the user but doesn't yet have cached
# children IS still visible-expanded — its placeholder row renders
# until the children stream in, and freshly-arrived children must be
# filter-evaluated.


def _filter_dispatch_pre_scan(state, op):
    """Return a dispatch tuple describing the op's filter side effects,
    or ``None`` for ops that don't affect filter visibility.

    Captures the pre-mutation snapshot of the item's current parent
    (needed for ``mod`` with ``KEEP_PARENT``, for ``remove`` after the
    item is dropped from the indexes, and for detecting reparent on
    ``upsert``/``set``) and whether the id is a fresh insert (needed
    so the dispatcher visit()s the new subtree before propagating up
    from the parent — see ``_dispatch_filter_propagation``).

    Tuple shape:
      (kind, item_id, new_parent_id, old_parent_id, is_new)
    """
    kind = op[0]
    if kind == 'upsert':
        _, id_, op_parent_id, _fields, *_rest = op
        existed = id_ in state._items_by_id
        old_parent_id = state._parent_of_id.get(id_)
        # ``_apply_upsert`` quirk: existing id with ``parent_id=None``
        # is patch-only and leaves the parent unchanged. Also: new id
        # with ``parent_id=None`` AND ``state.root_id is not None`` is
        # a silent drop. Mirror both — otherwise the dispatcher
        # propagates against a parent that wasn't actually touched.
        if op_parent_id is None and existed:
            new_parent_id = old_parent_id
        elif op_parent_id is None and state.root_id is not None:
            # Silent drop in apply_ops — no dispatch.
            return None
        else:
            new_parent_id = op_parent_id
        return (
            'upsert', id_, new_parent_id, old_parent_id, not existed,
        )
    if kind == 'set':
        _, id_, op_parent_id, _fields, *_rest = op
        # ``_apply_set`` rebuilds the Item instance. For a NEW id, treat
        # it like an upsert (visit subtree, propagate from parent). For
        # an EXISTING id, the children list is untouched and their
        # ``_filter_hidden`` flags stay valid — propagate-up from the
        # (new) item itself; the helper will re-evaluate the item's
        # text and walk upward if its status changed.
        existed = id_ in state._items_by_id
        old_parent_id = state._parent_of_id.get(id_)
        return ('set', id_, op_parent_id, old_parent_id, not existed)
    if kind == 'mod':
        _, id_, op_parent_id, _fields, *_rest = op
        # Unknown id → ``_apply_mod`` is a silent no-op; nothing to
        # propagate.
        if id_ not in state._items_by_id:
            return None
        old_parent_id = state._parent_of_id.get(id_)
        if op_parent_id is KEEP_PARENT:
            new_parent_id = old_parent_id
        else:
            new_parent_id = op_parent_id
        # mod is patch-only; never inserts. Existing item.
        return ('mod', id_, new_parent_id, old_parent_id, False)
    if kind == 'remove':
        _, id_ = op
        # Unknown id → ``_apply_remove`` is a silent no-op; nothing to
        # propagate.
        if id_ not in state._items_by_id:
            return None
        old_parent_id = state._parent_of_id.get(id_)
        return ('remove', id_, None, old_parent_id, False)
    if kind == 'clear_children':
        _, parent_id = op
        # If the parent has no cached children, ``_apply_clear_children``
        # is effectively a no-op.
        if parent_id not in state._children:
            return None
        return ('clear_children', None, parent_id, parent_id, False)
    # complete / incomplete / set_preview / append_preview /
    # clear_preview / invalidate_preview / drop_preview_cache: no
    # filter-visibility side effects.
    return None


def _is_parent_visible_expanded(state, parent_id):
    """Return True iff ``parent_id`` is a visible-expanded parent.

    Visible-expanded per Rule 2:
      * root (``parent_id == state.root_id``) — always conceptually
        expanded; ``visible_items`` walks ``_children[root_id]``
        regardless of ``state.expanded``.
      * any other id (including the scope row) — must be in
        ``state.expanded``. ``scope_into`` auto-adds the scope id, so
        this naturally captures the scope-root case.

    A parent that's expanded but with no cached children is still
    visible — it renders with a "pending" placeholder row, and an
    op arriving under it lifts the placeholder. Hence no
    ``_children`` gate.
    """
    if parent_id == state.root_id:
        return True
    return parent_id in state.expanded


def _dispatch_filter_propagation(state, dispatch_info, filters, show_ids):
    """Apply per-op filter propagation for the captured dispatch info.

    Each entry comes from ``_filter_dispatch_pre_scan``. The visible-
    expanded gate (Rule 2) decides whether to propagate; when an op
    reparents an existing item BOTH the old and new parent get walks.
    ``_propagate_filter_status_up`` does its own early-terminate up
    the ancestor chain.

    For NEW items (``is_new=True``) the dispatcher visits the item's
    subtree first (writing ``_filter_hidden`` bottom-up) and then
    propagates from the PARENT — calling propagate-up FROM the new
    item itself would early-terminate before reaching the parent
    when the item's evaluated flag matches its dataclass default of
    ``False``. For EXISTING items (``is_new=False``) propagate-up
    starts from the item itself, since its evaluation may flip its
    flag and bubble up naturally.
    """
    # Coalesce duplicate walks within this batch.
    walked = set()
    active = [q for q in filters if q]
    scope_id = state.scope_stack[-1] if state.scope_stack else None

    def _walk_from_id(target_id):
        if target_id is None or target_id in walked:
            return
        item = state._items_by_id.get(target_id)
        if item is None:
            return
        walked.add(target_id)
        _propagate_filter_status_up(
            state, item, filters, show_ids=show_ids,
        )

    for entry in dispatch_info:
        kind, item_id, new_parent_id, old_parent_id, is_new = entry

        if kind == 'remove':
            # Item is gone post-apply — propagate from the old parent.
            if _is_parent_visible_expanded(state, old_parent_id):
                _walk_from_id(old_parent_id)
            continue

        if kind == 'clear_children':
            # Propagate from the cleared parent itself.
            if _is_parent_visible_expanded(state, new_parent_id):
                _walk_from_id(new_parent_id)
            continue

        # upsert / set / mod
        new_visible = _is_parent_visible_expanded(state, new_parent_id)
        reparented = (
            old_parent_id is not None
            and old_parent_id != new_parent_id
        )
        if reparented:
            old_visible = _is_parent_visible_expanded(
                state, old_parent_id,
            )
        else:
            old_visible = False

        if new_visible:
            if is_new:
                # NEW item: visit its subtree to set ``_filter_hidden``
                # bottom-up (covers the rare pre-seeded-children case
                # too — a leaf new item gets one self-eval). Then
                # propagate-up FROM PARENT so the parent re-evaluates
                # against the newly-flagged child. Walking from the
                # new item itself would early-terminate when its flag
                # equals the dataclass default before the parent ever
                # sees the change.
                new_item = state._items_by_id.get(item_id)
                if new_item is not None:
                    _filter_visit_subtree(
                        state, new_item, active, scope_id, show_ids,
                    )
                _walk_from_id(new_parent_id)
            else:
                # EXISTING item: propagate-up from the item itself.
                # Its evaluation may flip the flag, bubbling up.
                _walk_from_id(item_id)
        if reparented and old_visible:
            _walk_from_id(old_parent_id)


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

    ``scope_root_id`` is the id of the current scope (``None`` when not
    scoped). When non-None, depth-0 insertions are rejected — they
    would land outside the scope (as a sibling of the scope row, which
    has no visible parent). The scope row at depth 0 is still a valid
    *parent* for depth>0 insertions; the ordinary "above is parent for
    deeper depth" branch handles that case naturally.

    plan-tui has an explicit ``parent`` field on every ticket and uses
    an id_map of all_tickets to walk up ancestors. browse-tui's lazy
    model doesn't carry ``parent`` on ``VisibleEntry``, so we walk the
    visible list itself to find the ancestor at the target depth — by
    tree-DFS construction the most-recent earlier row at depth ``d`` is
    the unique ancestor at that depth.
    """
    if not vis or pos <= 0:
        return (None, None)

    # When scoped, depth 0 means "outside the scope" — reject. (See
    # scope-root unification design.)
    if scope_root_id is not None and depth == 0:
        return (None, None)

    above = vis[pos - 1]

    if depth > above.depth:
        # Inserting as child of above.
        return ('first', above.item.id)
    if depth == above.depth:
        # Inserting as sibling after above.
        return ('after', above.item.id)

    # depth < above.depth — outdented. Walk back through ``vis`` until
    # we hit a row at the target depth; that's the ancestor we want to
    # become a sibling of (relation 'after'). The depth==0 guard above
    # already short-circuited the scoped case, so we never land on the
    # scope row here.
    i = pos - 1
    while i >= 0 and vis[i].depth > depth:
        i -= 1
    if i >= 0 and vis[i].depth == depth:
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


# Input-burst coalescing budget. The main loop dispatches the first key
# that wakes ``read_key``, then drains any keystrokes already buffered
# on stdin (peeked via ``input_ready``) and dispatches each without re-
# rendering. The render fires once when the burst ends. Without a
# bound, a pasted command or a stuck-down key could starve the paint
# indefinitely; these caps guarantee an intermediate frame after at
# most ``_INPUT_BURST_MAX_KEYS`` keys or ``_INPUT_BURST_MAX_SECONDS``
# of wall time, whichever comes first.
_INPUT_BURST_MAX_KEYS = 64
_INPUT_BURST_MAX_SECONDS = 0.016


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


def _extend_or_drop_preview_render(item, chunk, ansi_on) -> None:
    """In-place extend ``item.preview_render`` for an ``append_preview`` chunk.

    Ticket #423 optimisation on top of #422: streaming appends used to
    invalidate the wrap cache wholesale, forcing the next paint to
    re-wrap the entire (potentially large) preview. This helper reuses
    the existing ``wrapped`` list by re-wrapping only the affected
    suffix — the partial last raw line that was open at the previous
    cache snapshot, plus any new raw lines contributed by ``chunk``.

    Pre-state contract: ``item.preview`` has *already* been updated to
    include the appended chunk (the caller does ``item.preview = cur +
    chunk`` before calling). The cache's ``raw_tail_offset`` still
    points into the *old* preview's tail — which is now a prefix of the
    new preview, so the offset is still valid.

    Algorithm:
      1. ``raw_tail_text = item.preview[raw_tail_offset:]`` (this is
         the previous partial last line PLUS the new chunk, since the
         old offset's suffix grew with the append).
      2. Split on ``\\n`` → new raw lines. Last may be partial.
      3. Wrap each via ``_wrap_preview_line``; flatten.
      4. Replace ``wrapped[wrapped_tail_offset:]`` with the flattened
         wrap (note: that slice IS the wrap of the previous partial
         last line — re-wrapping it together with the new content
         keeps the output byte-identical to a full re-wrap).
      5. Recompute ``raw_tail_offset`` (position after last ``\\n``
         in the new preview, or 0 if no ``\\n``) and
         ``wrapped_tail_offset`` (where the new tail-line wrap starts
         in ``wrapped``, or ``len(wrapped)`` if the new tail line is
         empty).

    Falls back to ``item.preview_render = None`` (next paint regens
    fresh) when:
      * ``preview_render`` is already None (nothing to extend),
      * ``chunk`` is empty (nothing changed; cheap cache pass-through),
      * cached ``ansi_on`` doesn't match the current Browser policy
        (eager invalidation should have dropped, defensive only),
      * width is non-positive (degenerate cache state).

    Sanitisation: the chunk goes through ``_sanitize_preview`` (same
    per-char ``str.translate`` pass the renderer uses) so the wrapped
    output is byte-identical to a fresh full re-wrap. The sanitiser
    is per-character with no cross-line state, so applying it to the
    chunk alone matches applying it to the full preview text.
    """
    cached = item.preview_render
    if cached is None:
        # No cache to extend; next paint lazy-fills.
        return
    if not chunk:
        # No-op append — keep the cache as-is.
        return
    if cached.ansi_on != ansi_on or cached.width <= 0:
        # Geometry / policy mismatch ⇒ drop and let the next paint
        # regenerate fresh. Eager invalidation hooks normally prevent
        # this branch.
        item.preview_render = None
        return

    # ``item.preview`` already includes ``chunk`` (caller updated it
    # first). The cached ``raw_tail_offset`` indexes into the old
    # preview, but since the append only grew the suffix, the offset
    # is still a valid index into the new preview.
    #
    # ``item.preview`` stores raw text (sanitisation happens at render
    # time in ``render_preview``). Re-sanitise the affected suffix here
    # so the re-wrap path produces byte-identical output to a full
    # re-wrap. ``_sanitize_preview`` is per-char via ``str.translate``,
    # so applying it to a slice matches applying it to the full text.
    preview = item.preview if item.preview is not None else ''
    raw_tail_text = _sanitize_preview(
        preview[cached.raw_tail_offset:], ansi_on=ansi_on,
    )

    # Re-wrap the affected suffix. Note: this re-wraps the previous
    # partial last line together with the new content — that's required
    # for byte-identical output because the partial line could now
    # exceed ``width`` (or its wrap geometry could change with extra
    # bytes glued on).
    raw_lines = raw_tail_text.split('\n')
    new_wrapped = []
    new_wrapped_tail_offset = None
    last_idx = len(raw_lines) - 1
    for i, line in enumerate(raw_lines):
        if i == last_idx:
            # Mark where the wrap of the new tail (open) raw line
            # starts before extending — that becomes the new
            # ``wrapped_tail_offset``.
            new_wrapped_tail_offset = (
                cached.wrapped_tail_offset + len(new_wrapped)
            )
        line = line.replace('\t', '    ')
        new_wrapped.extend(_wrap_preview_line(
            line, cached.width, ansi_on=ansi_on, drop_sgr=False))

    # Splice: discard the cached wrap of the previous partial last line
    # and append the freshly-wrapped suffix.
    wrapped = cached.wrapped[:cached.wrapped_tail_offset] + new_wrapped

    # New raw_tail_offset: position after the LAST '\n' in the new
    # preview. If there's no '\n' at all in the new preview, the
    # whole preview is one open partial line and the offset is 0.
    last_nl = preview.rfind('\n')
    if last_nl < 0:
        new_raw_tail_offset = 0
    else:
        new_raw_tail_offset = last_nl + 1

    item.preview_render = PreviewRender(
        wrapped=wrapped,
        raw_tail_offset=new_raw_tail_offset,
        wrapped_tail_offset=new_wrapped_tail_offset,
        width=cached.width,
        ansi_on=cached.ansi_on,
    )


# ---------------------------------------------------------------------------
# Default row-format handlers (design sec A) — the framework's stock chrome /
# content builders, split out of the old ``format_item_segments`` so a recipe
# can override one part (just the content for columns) while keeping the rest,
# or call a default, edit its segments, and return them. Public / module-level
# so ``sys.modules['browse_tui']`` (080-cli) re-exports them automatically.
#
# They live here (not in 050-render) so ``Browser.__init__`` can bind them
# with ``config.X or default_X`` as an intra-module name — the ~30 unit-test
# files that load this module standalone and construct ``Browser`` then need
# no change. Their *bodies* reference render-layer constants / helpers
# (``_TAG_STYLE``, ``_id_visible``, ``_ID_COLOR``, ``_MARKER_COLOR``,
# ``_PENDING_FG``, ``cell_width``) which the concatenated build resolves by
# name; isolated test loads that actually render inject them the same way
# ``Item`` is injected.


def _segments_cells(segments):
    """Total display-cell width of a ``(text, fg, bold)`` segment list.

    Sums :func:`cell_width` (wide-char aware) over each segment's text —
    colour rides ``fg``/``bold``, never the text, so the width is exact.
    Used by the chrome→content hand-off to size ``ctx.content_width``.
    """
    return sum(cell_width(text) for text, _fg, _bold in segments)


def default_row_chrome(item, ctx):
    """The framework's default row *chrome* segments for a normal row.

    The structural prefix, lifted verbatim from the old
    ``format_item_segments``: the selection marker (``'* '`` if
    ``ctx.selected`` else ``'  '``), the indentation (``'  '`` per tree
    level), and the expander glyph (``'▼ '`` if ``ctx.expanded``, ``'▶ '``
    if ``item.has_children`` else ``'  '``). Chrome stays framework-owned
    unless a recipe overrides ``format_row_chrome``, so overriding only
    ``format_row_content`` keeps the tree intact.
    """
    rel_depth = ctx.depth
    if rel_depth < 0:
        rel_depth = 0
    indent = '  ' * rel_depth

    sel_marker = '* ' if ctx.selected else '  '
    if item.has_children:
        expand_marker = '▼ ' if ctx.expanded else '▶ '   # ▼ / ▶
    else:
        expand_marker = '  '

    return [
        (sel_marker, None, False),
        (indent, None, False),
        (expand_marker, _MARKER_COLOR, False),
    ]


def default_row_content(item, ctx):
    """The framework's default row *content* segments for a normal row.

    The content region, lifted verbatim from the old
    ``format_item_segments``: the id segment (gated by
    ``ctx.browser.show_ids`` via :func:`_id_visible`), the ``tag`` chip,
    the title (with the ``ctx.is_current_scope`` → ``scope_title``
    override), and the trailing ``chips``. Output is byte-for-byte
    identical to the pre-change ``kind='normal'`` content.

    The scope row gets its id + title segments bolded so it stands apart
    from the listing below — the "you are here" indicator. The selection /
    expand markers stay non-bold (chrome, not content); the tag segment
    keeps its ``tag_style``-driven bold so explicit tag styles still
    control their own weight.
    """
    is_current_scope = ctx.is_current_scope
    show_ids = ctx.browser.show_ids

    segments = []
    if _id_visible(item, show_ids):
        segments.append(('{} '.format(item.id), _ID_COLOR, is_current_scope))

    if item.tag:
        sfg, sbold = _TAG_STYLE.get(item.tag_style, _TAG_STYLE[''])
        segments.append(('[{}] '.format(item.tag), sfg, sbold))

    scope_title = getattr(item, 'scope_title', None)
    title_text = scope_title if (is_current_scope and scope_title) else item.title
    segments.append((title_text, None, is_current_scope))

    # Trailing colored chips: one ``[{text}] `` segment per (text, style)
    # in ``item.chips``, styled through ``_TAG_STYLE`` like the tag chip.
    # Color rides the segment ``fg`` (never embedded in text) so width
    # math stays correct.
    for text, style in getattr(item, 'chips', None) or ():
        cfg, cbold = _TAG_STYLE.get(style, _TAG_STYLE[''])
        segments.append((' [{}]'.format(text), cfg, cbold))
    return segments


def default_row(item, ctx):
    """The framework's default *whole row* — chrome + content.

    Returns ``default_row_chrome(item, ctx) + default_row_content(item,
    ctx)`` and sets ``ctx.content_width`` (``list_width − chrome cells``)
    along the way, mirroring :meth:`Browser._compose_row`. This is the
    "give me a stock row to tweak" helper for a whole-row ``format_row``
    override — it composes the *framework* defaults, not any other
    resolved hook.
    """
    chrome = default_row_chrome(item, ctx)
    ctx._set_content_width(_segments_cells(chrome))
    return chrome + default_row_content(item, ctx)


@dataclass
class BrowserConfig:
    """Construction parameters for :class:`Browser`.

    Every previous ``Browser(**kwargs)`` keyword argument is a field
    here. Plugins may mutate this dataclass in ``on_before_init``
    hooks to influence what the Browser becomes; ``Browser.__init__``
    reads the fields once, after firing those hooks.
    """
    title: str = 'browse-tui'
    get_children: Optional[Callable[[Any], Any]] = None
    get_preview: Optional[Callable[[Any], Optional[str]]] = None
    actions: Optional[list] = None
    on_enter: Any = None
    # Row-format hooks (design sec A). Each is ``(item, ctx) -> segments``
    # where a segment is a ``(text, fg, bold)`` triple. Resolution is by
    # config, not by return value, and bound once in ``Browser.__init__``:
    #   * ``format_row``          — the whole row (total control).
    #   * ``format_row_chrome``   — selection marker + indent + expander.
    #   * ``format_row_content``  — the content region (id + tag + title +
    #                               chips today; arbitrary columns when set).
    # A hook left ``None`` uses the framework default for that part; a hook
    # that *is* set owns its return completely. Override only
    # ``format_row_content`` to get columns while keeping the tree chrome.
    format_row: Optional[Callable] = None
    format_row_chrome: Optional[Callable] = None
    format_row_content: Optional[Callable] = None
    root_id: Any = None
    initial_scope: Any = None
    # ``None`` means "auto" — resolved by ``Browser.__init__`` to
    # ``get_preview is not None``. Recipes/CLIs that omit a preview
    # function get a list-only layout for free; setting True or False
    # explicitly overrides the auto rule.
    show_preview: Optional[bool] = None
    show_children_pane: bool = True
    preview_ansi: bool = True
    list_ratio: float = 0.30
    split: str = 'auto'
    multi_select: bool = True
    print_format: str = '{id}'
    help_intro: Optional[str] = None
    help_outro: Optional[str] = None
    show_ids: str = 'auto'
    show_scope_crumb: bool = False
    preview_buffer_cap_chars: int = 100_000
    preview_buffer_cap_lines: int = 1000
    on_cursor_change: Optional[Callable] = None
    on_scope_change: Optional[Callable] = None
    on_selection_change: Optional[Callable] = None
    on_expand: Optional[Callable] = None
    on_collapse: Optional[Callable] = None
    on_children_loaded: Optional[Callable] = None
    on_search_change: Optional[Callable] = None
    on_filter_change: Optional[Callable] = None
    on_resize: Optional[Callable] = None
    on_quit: Optional[Callable] = None
    _headless: bool = False


# Streaming preview cap derivation (#458 / streaming-umbrella spec §3).
#
# ``_stream_preview_from_generator`` re-reads its line cap each pause
# cycle via ``Browser._preview_cap_lines()`` so a terminal resize takes
# effect on the next cap window. The cap is sized in screens of the
# preview pane:
#
#   * ``STREAM_CAP_FACTOR = 3`` → ~3 screens of buffered scrollback,
#     well past the ``_PREVIEW_DEMAND_THRESHOLD = 12`` (050-render.py)
#     so the demand-resume kicks in long before the user scrolls into
#     unbuffered territory.
#   * ``MIN_CAP_LINES = 50`` → floor for the cold-start case where the
#     pane height hasn't been measured yet (no tty, headless tests).
#
# The char cap (``preview_buffer_cap_chars``) stays as the static
# memory safety net — it doesn't scale with pane size.
STREAM_CAP_FACTOR = 3
MIN_CAP_LINES = 50

# Ops that don't change the tree structure — used by ``update_data._apply``
# to skip the O(N) maintenance pipeline for pure-preview batches. Without
# this gate, a streaming generator yielding N chunks would do N×
# ``visible_items`` / ``hide-displacement`` / ``cursor-anchor`` /
# ``expand-goal`` passes for an O(N²) cost — the 100% CPU hang reported
# when draining huge umbrellas under Shift-End tail-pin.
_PREVIEW_ONLY_OP_KINDS = frozenset([
    'set_preview',
    'append_preview',
    'clear_preview',
    'invalidate_preview',
    'drop_preview_cache',
])


class Browser:
    """The TUI engine and async coordinator.

    Holds the data caches (``_state``), the cross-thread post queue
    (``_main_queue``), the children FIFO worker, and the preview worker
    (latest-wins request slot, FIFO delivery via the post queue). All
    public mutation goes through ``post(fn)`` so background threads
    (workers, watchers, signal handlers) are safe to schedule work
    without taking locks -- the main thread drains the queue on every
    wake.

    Construction kwargs (full spec'd surface):
      title:              window title (renderer in #10).
      get_children:       (parent_id) -> Iterable[Item|str|tuple|dict].
      get_preview:        (item_id) -> str | None.
      actions:            list of Action objects (Action lands in #11;
                          phase 1 stores the list opaquely).
      on_enter:           default-action handler; #13 wires fall-back
                          print+exit when None.
      format_row /        (item, ctx) -> [(text, fg, bold), …] row-format
      format_row_chrome / hooks (design sec A); each unset hook falls back
      format_row_content: to the framework default for that part. Resolved
                          once in ``__init__``; the renderer calls
                          ``_row_segments`` with no per-row ``None`` test.
      root_id:            Any (default None).
      initial_scope:      if set, pushed onto scope_stack at construction.
      show_preview:       enable the preview pane. ``None`` (default)
                          means "auto" — preview pane is shown iff
                          ``get_preview`` is supplied. True/False
                          forces the choice.
      show_children_pane: enable the right-hand children-as-list pane.
      preview_ansi:       honour ANSI SGR sequences in preview text
                          (default True). Toggled at runtime via
                          capital-R.
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

    def __init__(self, config: Optional['BrowserConfig'] = None) -> None:
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
            format_row / format_row_chrome / format_row_content: Optional
                ``(item, ctx) -> [(text, fg, bold)…]`` row-format hooks
                (design sec A). ``format_row`` owns the whole row;
                ``format_row_chrome`` the selection marker + indent +
                expander; ``format_row_content`` the id + tag + title +
                chips (or arbitrary columns). Each is resolved once here
                (``config.X or default_X``); an unset hook uses the
                framework default for that part, a set hook owns its
                return. Override only ``format_row_content`` to render
                columns while keeping the tree chrome.
            root_id: Initial id passed to the first ``get_children``
                call. ``None`` is the default.
            initial_scope: If set, pushed onto ``scope_stack`` at
                construction so the UI starts inside that scope.
            show_preview: Whether the preview pane starts visible.
                ``None`` (default) means "auto" — the pane is visible
                iff ``get_preview`` is supplied. ``True`` / ``False``
                forces the choice regardless of ``get_preview``.
            show_children_pane: Whether the children-grid pane starts
                visible.
            preview_ansi: Whether the preview pane honours ANSI SGR
                escape sequences in source text (default ``True``).
                When ``False``, escape sequences are stripped from the
                preview output. Toggled at runtime with capital-R.
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
            show_scope_crumb: When ``True``, the bottom info bar
                renders the scope-stack crumb (``▸ a ▸ b`` …) while
                scoped. Off by default — the crumb can eat significant
                horizontal space when ids are long (file paths, jsonl
                paths). Recipes that scope into short, meaningful ids
                can flip this on at construction.
            preview_buffer_cap_chars: Soft cap on buffered preview chars
                produced by a ``get_preview`` generator before the worker
                pauses pulling. Default 100_000 (~100 KB). The pause holds
                the generator alive (no ``gen.close()``) so a future
                demand signal (#274) can resume it. A cursor-move
                meanwhile abandons the paused generator. Whichever cap
                hits first (chars or lines) triggers the pause.
            preview_buffer_cap_lines: Soft cap on buffered preview lines
                (``\\n`` count) before the worker pauses pulling. Default
                1000. Counterpart to ``preview_buffer_cap_chars``.
            on_cursor_change: Optional ``(ctx) -> None`` callback fired
                once per main-loop tick on which the cursor row id
                changed. Debounced: rapid intermediate moves coalesce
                into a single fire. Recipes use this for "react when
                the user lands on a different row" (e.g. auto-fetch
                related data, update a sidebar). Exceptions are caught
                and routed to :meth:`error`.
            on_scope_change: Optional ``(ctx) -> None`` callback fired
                after a scope-in / scope-out transition. Read
                ``ctx.state.scope_stack`` to see the new scope.
                Exceptions are caught and routed to :meth:`error`.
            on_selection_change: Optional ``(ctx) -> None`` callback
                fired whenever ``state.selected`` changes. Recipes use
                this to keep a status counter, sidebar, or external
                pane in sync. Exceptions are caught and routed to
                :meth:`error`.
            on_quit: Optional ``(ctx) -> None`` callback fired once
                during main-loop shutdown, after the screen is
                restored but before ``Browser.run`` returns. Recipes
                use this to clean up worker threads, temp files, file
                handles. Exceptions are swallowed silently (a failing
                cleanup hook should not block exit).
            _headless: Skip terminal init/teardown — used by tests.
        """
        if config is None:
            config = BrowserConfig()
        # Plugin hook: ``on_before_init`` fires with ``self`` (empty
        # at this point — barely any attributes set) and ``config``,
        # which plugins may mutate to influence what the Browser
        # becomes. Hooks run in registration order; exceptions
        # propagate. All field reads below go through ``config`` so
        # plugin mutations are picked up.
        for _plugin_cfg in registered_plugins:
            if _plugin_cfg.on_before_init is not None:
                _plugin_cfg.on_before_init(self, config)

        if config.show_ids not in ('always', 'auto', 'never'):
            raise ValueError(
                "show_ids must be one of 'always', 'auto', 'never'; "
                f"got {config.show_ids!r}"
            )
        # --- user-supplied data callbacks -------------------------------
        # Default get_children to "no children" so a Browser constructed
        # with no kwargs still works (tests, smoke checks). get_preview
        # stays None -- the preview worker treats None as "always returns
        # ''" rather than calling a no-op lambda needlessly.
        self.title = config.title
        self.get_children = config.get_children or (lambda _id, *, reload=False: [])
        self.get_preview = config.get_preview
        # actions/on_enter are stored opaquely in phase 1;
        # tickets #11 (Context) and #12 (action keymap) read them.
        self.actions = (
            list(config.actions) if config.actions is not None else []
        )
        self.on_enter = config.on_enter
        # Row-format hooks (design sec A) — resolved ONCE here, after the
        # ``on_before_init`` plugin hooks have had their chance to mutate
        # ``config``. An unset hook binds to the framework default for its
        # part, so ``render_list`` never tests a hook against ``None``.
        self.format_row_chrome = config.format_row_chrome or default_row_chrome
        self.format_row_content = config.format_row_content or default_row_content
        # Whole-row renderer: an explicit ``format_row`` override wins;
        # otherwise the (resolved) chrome + content composer, which owns
        # the chrome→content ``content_width`` hand-off (``_compose_row``).
        self._row_segments = config.format_row or self._compose_row
        # ``show_preview=None`` means "auto": show the preview pane
        # iff a ``get_preview`` callback was supplied. Explicit
        # True/False from the caller wins.
        if config.show_preview is None:
            self.show_preview = self.get_preview is not None
        else:
            self.show_preview = config.show_preview
        self.show_children_pane = config.show_children_pane
        self.show_scope_crumb = config.show_scope_crumb
        # Honour ANSI SGR escapes in the preview pane (default True).
        # Toggled at runtime via capital-R; see ``_toggle_preview_ansi``
        # in 070-actions.py. The cache invalidation is naturally handled
        # by the per-row byte-stream comparison in the differential
        # renderer — colour-bearing rows produce different bytes when
        # SGR re-emit is suppressed, so they redraw; plain rows produce
        # identical bytes and stay cache-hit (#240 design note).
        self.preview_ansi = config.preview_ansi
        # Fraction of total terminal rows allocated to the list pane.
        # Stored as a float so it survives terminal resizes without
        # rounding drift; clamped to a usable range by ``set_list_ratio``.
        # The ratio covers list / (list + children-grid + preview) per
        # the model: children pane stays content-driven, preview gets
        # the remainder.
        self.list_ratio = _clamp_list_ratio(config.list_ratio)
        # Split-layout selector — controls which family of pane geometries
        # ``layout_panes`` produces. Default ``'auto'`` resolves at
        # construction time via ``_clamp_split`` (vertical at >=230 cols,
        # else horizontal) so Python recipes that construct Browser
        # directly get the same auto behaviour as ``--split-type=auto``.
        self.split = _clamp_split(config.split)
        self.multi_select = config.multi_select
        self.print_format = config.print_format
        # help_intro/help_outro are prose blurbs shown above/below the
        # auto-generated key list in --help and the in-app help screen
        # (?). Recipes set them to explain what their tool does;
        # ``None`` (the default) elides the corresponding section.
        self.help_intro = config.help_intro
        self.help_outro = config.help_outro
        self.show_ids = config.show_ids
        # User-supplied lifecycle hooks. ``on_cursor_change`` is fired
        # at most once per main-loop tick; ``_cursor_change_pending``
        # latches between mark_cursor_changed and the drain, and
        # ``_last_cursor_id`` tracks the id we last fired for so that
        # cursor-anchor re-positioning that lands back on the same id
        # is a no-op. ``on_scope_change`` fires after every
        # scope_into / scope_out transition (main-thread paths only).
        # ``on_expand`` / ``on_collapse`` fire from a drain-time diff of
        # ``state.expanded`` against ``_last_expanded`` (see
        # ``_fire_expand_collapse_if_pending``) — no mutation site is
        # instrumented, so every expand source (keyboard, ``ctx.expand``,
        # recursive actions, ``collapse_all``, startup) is caught.
        # ``on_children_loaded`` fires once per drain with the parents
        # whose ``get_children`` fetch SETTLED this drain (the two
        # genuine-settlement sites — the ``complete`` op and
        # ``apply_children_results`` — record into
        # ``_children_loaded_pending``; ``clear_children`` deliberately
        # does NOT, since it drops the cache). ``on_search_change`` /
        # ``on_filter_change`` fire from a drain-time diff of the effective
        # query / filter tuple against ``_last_search_query`` /
        # ``_last_filters`` (so every mutation source — live edit, commit,
        # ``set_search_query`` / ``set_filters`` / ``add_filter`` /
        # ``clear_*`` — coalesces to one fire per drain). ``on_resize``
        # fires once when an observed SIGWINCH changes ``term_size()`` vs
        # ``_last_size``. ``on_quit`` fires once during shutdown after the
        # screen is restored.
        self._on_cursor_change = config.on_cursor_change
        self._on_scope_change = config.on_scope_change
        self._on_selection_change = config.on_selection_change
        self._on_expand = config.on_expand
        self._on_collapse = config.on_collapse
        self._on_children_loaded = config.on_children_loaded
        self._on_search_change = config.on_search_change
        self._on_filter_change = config.on_filter_change
        self._on_resize = config.on_resize
        self._on_quit = config.on_quit
        # Worker-slot registry for ``run_in_slot``. Each entry is the
        # currently-active CancellationToken for a named slot;
        # superseded tokens are removed lazily by the worker on exit.
        self._slots: dict = {}
        self._slots_lock = threading.Lock()
        self._cursor_change_pending = False
        self._last_cursor_id = None
        # Snapshot of ``state.expanded`` as of the last expand/collapse
        # fire. Seeded empty so any expansion issued before ``run()``
        # (recipe startup ``b.expand(...)``, or an ``initial_scope`` that
        # restores a saved set) fires ``on_expand`` on the first drain.
        # ``scope_into`` / ``scope_out`` re-baseline this after a
        # transition so per-scope set restores don't masquerade as
        # expands/collapses.
        self._last_expanded = set()
        # Parents whose ``get_children`` fetch settled since the last
        # ``_fire_children_loaded_if_pending`` drain. A set (not a diff)
        # collected by the genuine-settlement sites; drained into one
        # ``on_children_loaded(list)`` per tick. ``clear_children`` is
        # excluded — it clears loading but drops the children.
        self._children_loaded_pending = set()
        # Snapshots for the input-state / geometry diffs. ``on_search_change``
        # / ``on_filter_change`` fire when the effective query / filter tuple
        # differs from these (clearing to ``''`` / ``()`` is a real change →
        # fires once). ``_last_size`` is ``None`` until the first observed
        # resize so the first SIGWINCH always fires. ``_resize_pending`` is
        # latched at the SIGWINCH observation points (mirrors
        # ``_cursor_change_pending``); the drain consumes it so ``term_size``
        # is only read on an actual resize, not every tick.
        self._last_search_query = ''
        self._last_filters: tuple = ()
        self._last_size = None
        self._resize_pending = False
        self._headless = config._headless

        # --- domain state ------------------------------------------------
        # State stays a separate dataclass so unit tests can poke it
        # without spinning up a Browser. The preview cache lives on State
        # alongside _children for cohesion (one place to invalidate
        # everything per item id).
        self._state = State(root_id=config.root_id)
        # Apply ``initial_scope`` after State is built so scope_into can
        # do its bookkeeping (saving the empty pre-scope expanded set
        # under the prior scope key).
        if config.initial_scope is not None:
            scope_into(self._state, config.initial_scope)

        # --- cross-thread plumbing --------------------------------------
        # main_queue: any thread -> main thread. Drained by drain_main_queue
        # on every wake. queue.Queue is thread-safe and cheap; we don't
        # need its blocking semantics, just FIFO + safe puts.
        self._main_queue = queue.Queue()

        # children worker: FIFO of parent ids to fetch. The worker pops
        # from the left, fetches, and posts the result on the main
        # thread via ``update_data`` + a follow-up housekeeping
        # callable (#271). Deque ops are individually atomic under the
        # GIL — safe for single-producer / single-consumer use.
        #
        # ``_children_results`` is the legacy delivery deque used only
        # by ``set_children`` / ``apply_children_results`` (the public
        # thread-safe injection path for recipes that bypass the
        # worker). The worker itself no longer touches it.
        # Queue entries are ``(id, reload)`` tuples. ``reload=True``
        # fires only for the root refresh (``_do_refresh`` with id=None
        # or id=root_id) — a single signal that means "drop all your
        # caches and rebuild from scratch." All other enqueue paths
        # (expand, auto-prefetch, re-dispatched expanded ids) use
        # ``reload=False`` because the framework's own
        # ``cache_invalidate_all`` already wiped ``state._children``
        # and recipes' per-file caches will naturally re-populate.
        self._children_queue = deque()
        self._children_in_flight = {}     # id -> list[Pending] awaiting this fetch
        self._children_results = deque()  # FIFO of (id, items) — set_children only
        self._children_event = threading.Event()

        # Children prefetch slot: latest-wins single-flight for the
        # cursor-driven prefetch path (#481). Mirrors ``_preview_req``:
        # ``_update_children_for_cursor`` writes the current cursor's id
        # here; the children worker reads in Pass 1 of its loop and
        # decides whether to fetch (skips cache hits and FIFO-pending
        # hits). ``_children_prefetch_local_id`` is the worker's
        # "what I just fetched or decided to skip" memo — exposed as an
        # attribute so ``run_until_idle`` can observe quiescence without
        # touching worker internals. Single-attribute reads/writes are
        # GIL-safe; no lock needed. See
        # docs/superpowers/specs/2026-05-27-children-prefetch-slot-design.md.
        self._children_prefetch_req = None
        self._children_prefetch_local_id = None

        # Preview worker: post-queue delivery + local-id dedup (#442).
        # The worker keeps a ``local_id`` of the most recently fetched
        # request. On each iteration it reads ``_preview_req``: if it
        # equals ``local_id`` (already fetched) or ``None`` (nothing
        # asked), it clears the wake event and re-reads (clear-then-read
        # closes the gap with ``request_preview`` setting the event
        # while we were deciding to sleep). Otherwise it adopts the new
        # id, clears the event (to arm "next request landed during
        # fetch"), invokes ``get_preview``, and posts a
        # ``_deliver_preview`` closure that caches the result on the
        # main thread. No single-slot ``_preview_result`` lane: every
        # delivery lands via the FIFO post queue, so back-to-back
        # fetches all reach the cache even if the cursor has moved on.
        #
        # Only the main thread writes ``_preview_req`` (worker reads
        # only). Single-attribute reads/writes are GIL-safe; no lock
        # needed for this slot. ``_preview_event`` is the wake signal.
        self._preview_req = None
        self._preview_event = threading.Event()

        # Preview generator support (#273). When ``get_preview`` returns
        # a generator, the worker eagerly pulls each yield, calls
        # ``append_preview``, and tracks running buffer size. When the
        # buffer hits ``preview_buffer_cap_chars`` or
        # ``preview_buffer_cap_lines``, the worker pauses without
        # closing the generator. ``_preview_paused`` records the paused
        # generator so:
        #
        #   * a cursor-move (newer request lands in ``_preview_req``)
        #     calls ``gen.close()`` and pivots to the new id.
        #   * #274 (renderer demand signal) can resume by clearing
        #     ``_preview_paused`` + setting ``_preview_resume_event``.
        #
        # ``_preview_lock`` serialises mutations across the worker
        # thread and the main thread (request_preview uses it to
        # observe / clear the paused state when superseding).
        self._preview_buffer_cap_chars = int(config.preview_buffer_cap_chars)
        self._preview_buffer_cap_lines = int(config.preview_buffer_cap_lines)
        self._preview_lock = threading.Lock()
        self._preview_paused = None  # dict(id, gen, chars, lines) or None
        # #274: demand-resume flag. Set by ``signal_preview_demand``
        # under ``_preview_lock`` to tell the paused worker "keep
        # pulling" rather than "abandon." Distinguishes the resume
        # path from the cursor-move/abandon path inside the pause-wait
        # loop. The worker clears it when it observes the signal.
        self._preview_resume_pull = False
        # Wakes the worker out of its paused-wait. Set by:
        #   * cursor-move (request_preview) — worker re-checks
        #     ``_preview_req`` and abandons if it now points elsewhere.
        #   * stop_workers — worker re-checks ``_stop``.
        #   * #274 — consumer-near-end demand signal (resume pulling).
        self._preview_resume_event = threading.Event()
        # #274: debounce — last ``_preview_scroll`` value at which the
        # renderer signalled demand for the currently-paused id, so
        # repeated renders without scroll-motion don't re-fire.
        # Reset whenever the paused id changes.
        self._preview_demand_signal_state = None  # (id, scroll) or None

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
        # User-input dispatch mode (NORMAL / SEARCH_EDIT / FILTER_EDIT).
        # The renderer reads this to decide whether to show the search /
        # filter prompts in the info bar; the action layer uses it to
        # route keystrokes (typed chars extend the active query while a
        # prompt is open, navigation keys fall through).
        self._mode = Mode.NORMAL
        # Search prompt buffer — renderer reads it for the prompt text
        # and for highlight spans.
        self._search_query = ''
        # Filter stack — see docs/superpowers/specs/2026-05-17-filter-design.md.
        # While ``_mode is Mode.FILTER_EDIT`` the last entry is the live
        # one being typed; otherwise every entry is committed. Filtering
        # is active iff this list contains any non-empty entries.
        self._filters: list = []
        # Preview pane scroll offset (lines from top of preview content).
        # Reset whenever the preview content changes; nudged by the
        # shift-up/shift-down handlers in the action layer (#12).
        self._preview_scroll = 0
        # Tail-follow flag: when True, the renderer forces the scroll
        # to ``max_scroll`` every pass so the view sticks to the bottom
        # as preview content grows (streaming generators,
        # ``append_preview``). Engaged by ``_preview_end`` /
        # ``Browser.preview_to_tail``; cleared by any upward scroll
        # action, cursor-item change, and help-mode toggle. See
        # ``docs/superpowers/specs/2026-05-17-preview-tail-design.md``.
        self._preview_at_tail = False
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
        # Last computed width of the preview pane in terminal columns.
        # Refreshed by ``_layout_for`` (050-render) on every render pass.
        # Zero until the first paint, or while the preview pane isn't
        # visible / terminal geometry can't be read. Exposed via the
        # ``preview_width`` property so recipes can size word-wrap and
        # markdown rendering to the live pane.
        self._preview_width = 0
        # List-pane scroll offset (rows from top of the visible list).
        # Maintained by render_list to keep the cursor on-screen; lives
        # on Browser so partial redraws remember it across calls.
        self._list_scroll = 0
        # Per-pane row caches for the differential renderer (#186). Keys
        # are pane names ('list', 'children', 'preview', 'info_bar',
        # 'sep_main', 'sep_inner', …); values are ``PaneCache`` objects
        # carrying current/previous rect plus a parallel ``lines`` buffer
        # of cached ``(visible_len, bytes)`` tuples. The renderer
        # migrations in #187/#188 will populate these via the row-buffer
        # shim in 020-terminal; this ticket just wires the dict.
        self._pane_cache: dict = {}

        # --- children-grid layout (#434) --------------------------------
        # ``_sub_layout`` (050-render.py) is a pure function of
        # ``(children, width, show_ids)`` and is called twice per paint
        # by default — once in ``layout_panes`` to size the grid pane,
        # once in ``render_children_grid`` to draw it. The result is
        # stored here (last-computed) so callers that don't need to
        # re-derive inputs can read it directly.
        #
        # No cache, no invalidation: ``children_grid_layout`` recomputes
        # on every call. ``_sub_layout`` is cheap; the 2x-per-paint cost
        # is acceptable and removes a class of subtle invalidation
        # bugs. Initialised to a valid empty layout so reads never see
        # ``None`` — mirrors ``_sub_layout([], 0, 'auto')``'s canonical
        # empty shape ``(1, 0, [], [])``. Hard-coded rather than
        # calling ``_sub_layout`` so ``__init__`` doesn't pull in
        # render-module names (helps the test loader, which wires
        # modules together lazily). Falls back to a plain tuple when
        # ``ChildrenGridLayout`` isn't available in the namespace —
        # production builds concatenate all modules so the namedtuple
        # is always present; tests that don't inject it still get a
        # valid 4-tuple they can unpack the same way.
        _cgl = globals().get('ChildrenGridLayout')
        self._children_grid_layout = (
            _cgl(1, 0, [], []) if _cgl is not None else (1, 0, [], [])
        )

        # --- quit bookkeeping (read by the main loop in #13) ------------
        # quit() flips _quit_requested; the main loop watches the flag
        # and exits with _quit_code, printing _quit_output if non-empty.
        self._quit_requested = False
        self._quit_code = 0
        self._quit_output = ''

        # --- sticky cursor anchor (id-based positioning) ---------------
        # The cursor's identity is its *item id*, not its row index. The
        # ``_cursor_anchor`` snapshot is a flat priority list of ids the
        # loop should keep the cursor on, in preference order:
        #
        #   _cursor_anchor = [primary, next, prev, parent, gp, ..., root]
        #
        # ``primary`` is the cursor's intended item. ``next`` / ``prev``
        # are the ids of the neighbouring visible "normal" rows captured
        # at re-anchor time. ``parent`` and beyond walk the ancestor
        # chain to the tree root via ``state._parent_of_id``.
        #
        # The snapshot is taken by ``_reanchor_cursor`` after every
        # user-driven cursor move (and lazily on startup). It is applied
        # by ``_apply_cursor_anchor`` after every background mutation
        # that could shift visible-list indices: ``apply_children_results``,
        # ``update_data._apply``, ``_do_expand`` (cached path). The walker
        # tries each tier in order and snaps ``state.cursor`` onto the
        # first match. On a ``primary`` hit it refreshes the snapshot
        # (the neighbourhood may have shifted while the primary was
        # missing); on a fallback hit it leaves the snapshot parked, so
        # if the primary returns later the cursor jumps back.
        #
        # Set explicitly by ``cursor_to(id)`` (the snapshot starts as
        # just ``[id]`` — fallbacks fill in once placement lands).
        self._cursor_anchor = []

        # --- sticky scroll-to-fit goal on expansion --------------------
        # When a node is newly expanded, the main loop tries to adjust
        # ``_list_scroll`` so the parent row AND its newly-revealed
        # subtree both fit in the list pane. The goal is parked here
        # and re-applied after every visible-list mutation
        # (``apply_children_results``, ``update_data._apply``) so a
        # subtree that streams in over time keeps moving into view.
        #
        # Cleared when:
        #   - the subtree is fully loaded (no pending placeholders), OR
        #   - the subtree is larger than the pane minus the parent row
        #     (scroll cap reached — can't fit any more without dropping
        #     the parent), OR
        #   - the user moves the cursor (``_handle_one_key`` clears
        #     it), OR
        #   - the user wheel-scrolls the list pane (mouse handler
        #     clears it).
        #
        # Set by ``_do_expand`` when ``autoscroll=True`` and the node
        # was not already expanded. User-driven ``→`` / ``l`` passes
        # ``autoscroll=True``; ``Browser.expand`` defaults to
        # ``autoscroll=False`` so recipes doing bulk setup don't
        # surprise the user with a scroll jump.
        self._expand_goal = None

        # --- insert-mode bookkeeping (ticket #21) ----------------------
        # Insert mode is entered by ``ctx.insert(label, on_confirm)``.
        # The user moves a placement marker through the visible tree and
        # confirms a position; on confirm, ``_insert_callback`` is invoked
        # with ``(relation, dest_id)`` describing how to place the new
        # item. While ``_insert_mode`` is True the main loop routes keys
        # through ``_handle_insert_key`` instead of the regular dispatch.
        #
        # ``_insert_pos`` is a *gap* position in the visible list: 1
        # means "insert before the first row after the scope row" (or
        # the first top-level row when unscoped), ``len(vis)`` means
        # "insert at the very end". ``_insert_depth`` is the
        # indentation level for the placement marker (controlled by
        # the user via right/left).
        self._insert_mode = False
        self._insert_pos = 0
        self._insert_depth = 0
        self._insert_callback = None
        self._insert_label = ''

        # Plugin hook: ``on_after_init`` fires with the fully-built
        # Browser. Plugins use this for monkey-patching instance
        # methods or attaching per-Browser state. Hooks run in
        # registration order; exceptions propagate.
        for _plugin_cfg in registered_plugins:
            if _plugin_cfg.on_after_init is not None:
                _plugin_cfg.on_after_init(self)

    # ---- row formatting -------------------------------------------------

    def _compose_row(self, item, ctx):
        """The default whole-row builder: chrome + content (design sec A).

        Bound to ``self._row_segments`` when no ``format_row`` override is
        configured. Calls the *resolved* ``self.format_row_chrome`` /
        ``self.format_row_content`` (the module defaults when a hook is
        unset), in that order, so it owns the chrome→content
        ``content_width`` hand-off: chrome is built and measured *before*
        the content hook runs, so ``ctx.content_width`` (cells left after
        the chrome on this row) is correct when the content hook reads it.

        A whole-row ``format_row`` override bypasses this method, so under
        such an override ``ctx.content_width`` stays equal to
        ``ctx.list_width`` (the chrome split is unknown).
        """
        chrome = self.format_row_chrome(item, ctx)
        ctx._set_content_width(_segments_cells(chrome))
        return chrome + self.format_row_content(item, ctx)

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

    def watch(self, callback: Callable[['Browser'], None],
              interval: Optional[float] = None) -> threading.Thread:
        """Spawn a daemon thread that calls ``callback(self)`` repeatedly.

        Construction-time helper, not a runtime/thread-safe API: recipes
        wire watchers up before ``start_workers`` / the main loop runs.
        Lives next to ``add_action`` because both are setup affordances.

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

    # =========================================================================
    # Public, thread-safe API
    # =========================================================================
    # Every method in this block is safe to call from any thread.
    # Mutations are deferred to the main loop's drain via post(),
    # the children-results deque, or the preview-result slot.
    #
    #   post(fn)                    — schedule fn() on the next main-thread drain
    #   message(text) / error(text) — surface a status / error
    #   refresh(id)                 — schedule re-fetch of children
    #   cursor_to(id)               — move cursor (expanding ancestors)
    #   expand(id)                  — expand a parent (and fetch if needed)
    #   set_children(id, items)     — inject pre-fetched children
    #   update_data(ops)            — apply a batched list of tree-mutation ops
    #   set_preview(id, text)       — inject pre-fetched preview text
    #   append_preview(id, chunk)   — append to per-id preview cache
    #   clear_preview(id)           — drop per-id preview cache entry
    #   set_list_ratio(ratio)       — resize the list pane
    #   set_split(s)                — change the split layout
    #   select(ids, replace=False)  — set the multi-select set
    #   cancel(*pendings)           — cancel one or more Pending handles
    #   quit(code=0, output='')     — exit the run loop
    #
    # Methods OUTSIDE this block are main-thread-only.
    # =========================================================================

    def post(self, fn: Callable[[], None]) -> None:
        """(thread-safe) Schedule ``fn`` to run on the main thread on the next drain.

        The callable runs with no arguments and its return value is
        ignored. Exceptions inside ``fn`` propagate to the drain loop --
        callers should catch their own exceptions if they want to keep
        the drain going. (We may revisit and wrap in try/except once the
        renderer can surface a status line.)
        """
        self._main_queue.put(fn)
        notify_wake()

    def message(self, text: str) -> None:
        """(thread-safe) Surface ``text`` as a transient status message.

        Stored on Browser; the renderer in ticket #10 picks it up. Uses
        ``post`` under the hood so the write happens on the main thread.
        """
        self.post(lambda: setattr(self, '_message_text', text))

    def error(self, text: str) -> None:
        """(thread-safe) Surface ``text`` as an error message. Same lane as ``message``."""
        self.post(lambda: setattr(self, '_error_text', text))

    @property
    def error_text(self) -> str:
        """Most recent error message surfaced via :meth:`error`."""
        return self._error_text

    @property
    def message_text(self) -> str:
        """Most recent transient status message surfaced via :meth:`message`."""
        return self._message_text

    # ---- cache introspection ---------------------------------------------
    #
    # Read-only views into the framework's item / children cache.
    # Recipes use these to answer "what's currently loaded" without
    # forcing a refetch and without reaching into ``state._items_by_id``
    # / ``state._children`` (which remain framework-private).

    @property
    def items_by_id(self) -> dict:
        """All currently-loaded items keyed by id (live read-only view).

        The returned dict is the framework's live cache — its identity
        is stable but contents mutate as children stream in / out.
        Recipes that need a stable iteration order should snapshot via
        ``tuple(browser.items_by_id.items())``. Mutating the dict is
        unsupported; route additions / removals through
        :meth:`update_data`.
        """
        return self._state._items_by_id

    def get_item(self, id_) -> Optional['Item']:
        """Return the loaded Item with ``id`` or ``None`` if not loaded.

        O(1). Items not yet fetched (children of a collapsed parent
        that was never expanded) return ``None``. To distinguish "not
        loaded" from "loaded but has no children", pair with
        :meth:`cached_children`.
        """
        return self._state._items_by_id.get(id_)

    def cached_children(self, parent_id) -> Optional[list]:
        """Return loaded children of ``parent_id`` as a list (copy), or ``None``.

        ``None`` means the parent's children have not been fetched
        yet; ``[]`` means the parent is loaded and has no children.
        The returned list is a shallow copy — modifying it does not
        affect framework state. Use :meth:`update_data` to add /
        remove children.

        **Children-list authority.** Once ``_state._children[parent]``
        is non-None — populated by ``get_children`` delivery or any
        ``update_data`` upsert — the framework treats whatever's there
        as the parent's children list. There's no "loading more"
        indicator after the initial population; tree expansion paints
        exactly what's in the list at paint time. The framework can't
        tell "still streaming" from "forgot to push the rest", so
        recipes that push children incrementally are responsible for
        eventually pushing all siblings. See :meth:`update_data` for
        the same constraint phrased from the push side.
        """
        entry = self._state._children.get(parent_id)
        if entry is None:
            return None
        return list(entry)

    def cached_parents(self) -> list:
        """Return ids of every parent whose children list is currently cached.

        Useful for "iterate every loaded subtree" recipes (a file
        browser polling mtime per cached directory, a tail-feed
        recipe diffing every loaded session). Order is the cache's
        insertion order; sort if recipe needs stability.
        """
        return list(self._state._children.keys())

    def all_items(self):
        """Iterator over every currently-loaded Item.

        Equivalent to ``items_by_id.values()`` but returns a snapshot
        iterator that is safe under concurrent cache mutation.
        Order matches the cache's insertion order.
        """
        return iter(list(self._state._items_by_id.values()))

    def refresh(self, id: Any = None,
                on_complete: Optional[Callable[[], None]] = None) -> 'Pending':
        """(thread-safe) Schedule a refetch of one parent's children (or the full root).

        Returns a Pending that resolves on the main thread once the worker
        has delivered the new children list. The actual cache invalidation
        runs on the main thread (in ``_do_refresh``) so visible-tree state
        stays consistent.

        ``id=None`` invalidates the entire cache and refetches the root.
        ``on_complete`` is wired via ``.then`` so callers may chain in
        either style.
        """
        pending = Pending()
        if on_complete is not None:
            pending.then(on_complete)
        self.post(lambda: self._do_refresh(id, pending))
        return pending

    def cursor_to(self, id: Any,
                  on_complete: Optional[Callable[[], None]] = None) -> 'Pending':
        """(thread-safe) Move cursor to the item with the given id, expanding ancestors as needed.

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

    def nav_home(self) -> None:
        """(thread-safe) Move cursor to row 0 and pin it there.

        Posts a callable that sets ``state.cursor = 0`` and
        ``_cursor_anchor = [PIN_FIRST]``. The cursor follows new
        arrivals at the top until any non-home/non-end navigation
        clears the pin. Returns ``None`` — there is nothing to await
        (no fetches required).
        """
        def _do():
            vis = visible_items(self._state)
            self._state.cursor = 0 if vis else 0
            self._cursor_anchor = [PIN_FIRST]
            mark_cursor_changed(self)
            self._needs_redraw.add('list')
        self.post(_do)

    def nav_end(self) -> None:
        """(thread-safe) Move cursor to the last visible row and pin it there.

        Symmetric to ``nav_home``. The cursor follows new arrivals at
        the bottom until any non-home/non-end navigation clears the
        pin. Returns ``None``.
        """
        def _do():
            vis = visible_items(self._state)
            self._state.cursor = max(0, len(vis) - 1)
            self._cursor_anchor = [PIN_LAST]
            mark_cursor_changed(self)
            self._needs_redraw.add('list')
        self.post(_do)

    # ---- interactive filter API ------------------------------------------
    #
    # See docs/superpowers/specs/2026-05-17-filter-design.md. Recipes
    # mutate the filter stack via ``set_filters`` / ``add_filter`` /
    # ``clear_filters`` (all thread-safe via post); reads use the
    # ``filters`` property.
    #
    # Reads return all non-empty entries, including the in-progress
    # entry while the user is typing in FILTER_EDIT — the in-progress
    # filter already affects what the user sees on screen, so a recipe
    # rendering "current filter state" should reflect it. The transient
    # empty placeholder (open prompt, no typing yet) is filtered out;
    # it's a UI mechanism, not a filter recipes should observe.

    @property
    def filters(self) -> tuple:
        """Currently-active filter strings (committed + live), in order.

        Returns a tuple of non-empty strings. The empty placeholder
        slot used by the filter-edit prompt before the user types is
        excluded.
        """
        return tuple(q for q in self._filters if q)

    def _do_filter_change(self) -> None:
        """Refresh ``_filter_hidden`` flags and reconcile cursor + redraw.

        Shared by ``set_filters`` / ``add_filter`` / ``clear_filters`` —
        runs the same pre-snapshot + recompute + hide-displacement +
        anchor flow used by the FILTER_EDIT key handler. Reuses
        ``_apply_hide_displacement`` so cursor follows a row that
        vanished due to the filter change. The positional cursor pin
        short-circuits hide-displacement, just like in ``update_data``.
        """
        pre_vis = visible_items(self._state)
        pre_vis_ids = [entry.item.id for entry in pre_vis]
        pre_cursor = self._state.cursor
        _recompute_filter_hidden(
            self._state, self._filters, show_ids=self.show_ids,
        )
        mark_visible_dirty(self._state)
        cur_anchor = self._cursor_anchor
        pinned = cur_anchor and isinstance(
            cur_anchor[0], _AnchorSentinel
        )
        if not pinned:
            self._apply_hide_displacement(pre_vis_ids, pre_cursor)
        self._apply_cursor_anchor()
        self._needs_redraw.add('list')
        self._needs_redraw.add('info')
        mark_cursor_changed(self)

    def set_filters(self, filters) -> None:
        """(thread-safe) Replace the filter list with the given iterable.

        Empty strings in ``filters`` are dropped silently. If the user
        is currently in FILTER_EDIT, the mode is forced to NORMAL
        (the in-progress placeholder is discarded) — recipe writes are
        authoritative.
        """
        new_list = [q for q in filters if q]

        def _do():
            self._filters = list(new_list)
            self._mode = Mode.NORMAL
            self._do_filter_change()
        self.post(_do)

    def add_filter(self, text: str) -> None:
        """(thread-safe) Append ``text`` to the filter stack (no-op if empty).

        Forces FILTER_EDIT exit if active, then appends.
        """
        if not text:
            return

        def _do():
            self._mode = Mode.NORMAL
            # Drop any empty placeholder that FILTER_EDIT left behind,
            # then append the new entry.
            self._filters = [q for q in self._filters if q]
            self._filters.append(text)
            self._do_filter_change()
        self.post(_do)

    def clear_filters(self) -> None:
        """(thread-safe) Drop all filters; alias for ``set_filters([])``."""
        self.set_filters([])

    def expand(self, id: Any,
               on_complete: Optional[Callable[[], None]] = None,
               autoscroll: bool = False) -> 'Pending':
        """(thread-safe) Add ``id`` to expanded; trigger fetch if not cached.

        Pending resolves when children are cached (or immediately on the
        next drain if already cached).

        ``autoscroll`` (default ``False``): when ``True`` and ``id``
        wasn't already expanded, park a sticky scroll goal so the
        viewport adjusts to fit the parent row plus its newly-revealed
        subtree (including async deliveries that arrive in pieces).
        User-driven expansion (``→`` / ``l``) passes ``autoscroll=True``;
        recipes doing bulk-expand setup leave the default and avoid
        surprise scrolls.
        """
        pending = Pending()
        if on_complete is not None:
            pending.then(on_complete)
        self.post(lambda: self._do_expand(id, pending, autoscroll))
        return pending

    def collapse(self, id: Any) -> None:
        """(thread-safe) Remove ``id`` from expanded; collapse its subtree.

        The single-node counterpart to :meth:`expand` — discards ``id``
        from ``state.expanded`` so its children fold away, then triggers
        the same repaint path the ``←`` / ``l`` navigation key uses
        (``mark_visible_dirty`` plus a cursor-change mark). No fetch is
        involved, so this returns ``None`` rather than a ``Pending``:
        there is nothing to await.

        Collapsing an id that is not expanded is a no-op (``discard``
        never raises). The cursor is left where it is; if it sat inside
        the now-collapsed subtree the framework's cursor-anchor walk
        lands it on the nearest still-visible ancestor on the next
        render.
        """
        def _do():
            self._state.expanded.discard(id)
            mark_visible_dirty(self._state)
            mark_cursor_changed(self)
        self.post(_do)

    def set_children(self, id_, items) -> None:
        """(thread-safe) Inject pre-fetched children for ``id_`` from any thread.

        Equivalent to what the built-in children worker does after a
        successful fetch — but lets recipe-owned threads (any threading
        model: native threads, asyncio bridges, IPC daemons) deliver
        results without going through ``request_children``. Items are
        coerced via ``to_item`` so the recipe can pass plain dicts.

        A recipe that manages all fetching itself can pass
        ``get_children=None`` to disable the built-in worker.
        """
        coerced = [to_item(x) for x in items]
        self._children_results.append((id_, coerced))
        notify_wake()

    def update_data(self, ops) -> None:
        """(thread-safe) Apply a batched list of tree-mutation ops on the main thread.

        ``ops`` is an iterable of op tuples produced by the
        ``upsert`` / ``set_item`` / ``remove`` / ``clear_children`` /
        ``complete`` / ``incomplete`` helpers (see Section 2 of the
        streaming-push design doc for the vocabulary). The whole batch is
        scheduled as a single callable on the post queue, so ``apply_ops``
        runs inside one drain of the main queue — the renderer never
        observes a torn intermediate state mid-batch, and exactly one
        render is needed afterward (the callable also flags
        ``_needs_redraw`` so the main loop repaints without waiting for
        the next keystroke; see streaming-push spec Section 1).

        Returns ``None`` rather than a Pending: there is nothing to await.
        Two separate ``update_data`` calls — even from the same thread —
        are not atomic with respect to one another; each is its own
        post-queue task. Recipes that need cross-call atomicity should
        merge their ops into a single list.

        Snapshots ``ops`` to a ``list`` on the calling thread so the
        scheduled callable doesn't capture a mutating live source.

        **Children-list authority.** Once ``_state._children[parent]``
        is non-None — populated by ``get_children`` delivery or any
        ``update_data`` upsert — the framework treats whatever's there
        as the parent's children list. There's no "loading more"
        indicator after the initial population: tree expansion paints
        exactly what's in the list at paint time.

        *Implication:* if you push children for a parent via
        ``update_data(upsert(...))`` you must *eventually* push all
        siblings. Partial lists are valid as transient states (the
        tail-worker pattern streams new children over time and the
        user sees them arrive), but a permanently-incomplete list
        means tree expansion permanently hides the missing siblings.
        The framework can't tell "still streaming" from "forgot to
        push the rest" — that's a recipe-author responsibility.
        """
        ops_list = list(ops)
        # Streaming-preview hot path (#471 follow-up): when every op in
        # the batch is a pure preview op (no structural change), skip
        # the O(N) maintenance pipeline (visible_items snapshot,
        # hide-displacement, cursor-anchor, expand-goal, list/children
        # redraw flags). For a streaming generator yielding thousands
        # of chunks, this turns a per-chunk O(N) cost into O(1) and
        # avoids the O(N²) hang reported when draining huge umbrellas
        # under Shift-End tail-pin.
        preview_only_batch = bool(ops_list) and all(
            isinstance(op, tuple) and op[0] in _PREVIEW_ONLY_OP_KINDS
            for op in ops_list
        )

        def _apply():
            if preview_only_batch:
                apply_ops(
                    self._state, ops_list,
                    preview_ansi=self.preview_ansi,
                )
                if self._state._preview_dirty:
                    self._needs_redraw.add('preview')
                for kick in self._state._preview_kicks:
                    kind = kick[0]
                    if kind == 'id':
                        self._kick_after_invalidate(kick[1])
                    elif kind == 'cursor':
                        cur = self._preview_cursor_id
                        if cur is not None:
                            self._kick_after_invalidate(cur)
                    elif kind == 'cursor_if':
                        cur = self._preview_cursor_id
                        if cur is not None and kick[1] == cur:
                            self._kick_after_invalidate(cur)
                return

            # Snapshot the pre-mutation visible list and cursor index
            # so hide-driven displacement can walk back through what
            # the user was looking at. See
            # ``_apply_hide_displacement`` for the rule.
            pre_vis = visible_items(self._state)
            pre_vis_ids = [entry.item.id for entry in pre_vis]
            pre_cursor = self._state.cursor

            # Pre-scan ops for per-op filter propagation (#499). Captures
            # (kind, item_id, new_parent_id, old_parent_id) per structural
            # op BEFORE apply_ops mutates state — needed because some ops
            # (``mod`` with ``KEEP_PARENT``, ``remove``, ``upsert`` of an
            # existing id with ``parent_id=None`` patch-only) only have
            # answers in the pre-state. The dispatch table below (post-
            # apply) calls ``_propagate_filter_status_up`` per affected
            # parent/item, walking O(depth) per op instead of the full
            # visible-tree walk. Per Rule 2 of
            # ``docs/superpowers/specs/2026-05-27-filter-visible-tree-only-design.md``
            # (visible-tree-only filter): ops landing under a collapsed
            # parent contribute no visible change and skip.
            filter_dispatch = []
            if self._filters:
                for op in ops_list:
                    info = _filter_dispatch_pre_scan(self._state, op)
                    if info is not None:
                        filter_dispatch.append(info)

            apply_ops(
                self._state, ops_list, preview_ansi=self.preview_ansi,
            )

            # Children-loaded settlement (#600). ``apply_ops`` recorded
            # every parent whose ``complete`` op settled this batch on
            # ``state._settled_parents``; move them into the drain-time
            # pending set. ``_fire_children_loaded_if_pending`` delivers
            # the batch as one ``on_children_loaded(list)`` per tick. Gated
            # on the handler (#627): with no listener the pending set stays
            # empty and the fire short-circuits — no per-settlement update.
            if (self._on_children_loaded is not None
                    and self._state._settled_parents):
                self._children_loaded_pending.update(
                    self._state._settled_parents)

            # Preview-op side effects (#446). ``apply_ops`` left the
            # outcome on ``state._preview_dirty`` / ``state._preview_kicks``;
            # translate to a redraw flag + ``request_preview`` calls
            # while ``self`` is still in scope.
            if self._state._preview_dirty:
                self._needs_redraw.add('preview')
            for kick in self._state._preview_kicks:
                kind = kick[0]
                if kind == 'id':
                    self._kick_after_invalidate(kick[1])
                elif kind == 'cursor':
                    # drop_preview_cache(None) — kick the cursor id (if any).
                    cur = self._preview_cursor_id
                    if cur is not None:
                        self._kick_after_invalidate(cur)
                elif kind == 'cursor_if':
                    # drop_preview_cache(id) — kick only when the
                    # dropped id is the current preview cursor.
                    cur = self._preview_cursor_id
                    if cur is not None and kick[1] == cur:
                        self._kick_after_invalidate(cur)

            # Re-evaluate filter visibility before computing the new
            # visible list. Per-op dispatch (#499): walk ``filter_dispatch``
            # entries captured pre-apply and call
            # ``_propagate_filter_status_up`` per affected parent/item.
            # Each call walks O(depth) and early-terminates at the first
            # ancestor whose flag value doesn't change. The dispatcher
            # itself gates on the visible-expanded check (Rule 2 of
            # ``2026-05-27-filter-visible-tree-only-design``): ops
            # landing under a collapsed parent skip entirely.
            #
            # ``apply_children_results`` (worker delivery, rare path)
            # keeps its full ``_recompute_filter_hidden`` walk —
            # ``update_data`` is the primary streaming path and
            # benefits from incremental updates; the legacy path's
            # full walk is the safe fallback.
            if self._filters and filter_dispatch:
                _dispatch_filter_propagation(
                    self._state, filter_dispatch,
                    self._filters, self.show_ids,
                )

            # Positional pin owns the cursor position — skip
            # hide-displacement entirely (the pin re-clamps to the new
            # edge, which is what the user asked for by pinning).
            # Otherwise: hide-displacement runs first for the
            # row-got-hidden case, then the anchor handles the
            # row-still-visible-but-index-shifted case.
            cur_anchor = self._cursor_anchor
            pinned = cur_anchor and isinstance(
                cur_anchor[0], _AnchorSentinel
            )
            if not pinned:
                self._apply_hide_displacement(pre_vis_ids, pre_cursor)
            # Re-snap the cursor onto its anchored id (or pinned
            # edge) before flagging redraws. A streaming push that
            # inserts items above the cursor would otherwise shift
            # its index off the original item;
            # ``_apply_cursor_anchor`` keeps the cursor's identity
            # stable by walking the snapshot tiers.
            self._apply_cursor_anchor()
            # Re-apply the expand goal too — a structured push that
            # materialises a previously-loading subtree should slide
            # into view automatically.
            self._apply_expand_goal()
            # Clamp the cursor back into the visible range when the
            # anchor walk didn't find a home (e.g., every id in the
            # snapshot was removed). Mirrors the clamp in
            # ``apply_children_results``; without it, an ``update_data``
            # that shrinks the list past the cursor would leave the
            # cursor pointing past the end and the renderer would
            # silently skip the row.
            vis = visible_items(self._state)
            if vis and self._state.cursor >= len(vis):
                self._state.cursor = len(vis) - 1
            elif not vis:
                self._state.cursor = 0
            # Flag list/children for redraw so background pushes (e.g.
            # from a watcher or a websocket bridge) become visible
            # without waiting for the user to press a key. ``apply_ops``
            # already flipped ``_visible_dirty`` if anything structural
            # changed; the missing piece is converting that into a
            # ``_needs_redraw`` signal the main loop polls. Children
            # pane is included because tag/title patches on the cursor
            # row affect what the grid shows.
            self._needs_redraw.add('list')
            self._needs_redraw.add('children')

        self.post(_apply)

    def set_preview(self, id_, text) -> None:
        """(thread-safe) Cache preview text for ``id_``.

        Posts a main-thread closure that writes ``item.preview = text``
        and drops the wrap cache. Multiple calls accumulate via the
        post queue — every write lands (FIFO), so a recipe that calls
        ``set_preview`` once per leaf in a composition no longer loses
        all but the last write. ``text`` is coerced to ``''`` if None.

        Worker-race note: the framework's preview worker also delivers
        through the FIFO post queue (#442 redesign), so worker results
        and recipe ``set_preview`` calls land in submission order — no
        more "worker always wins" overwrite. If a recipe ``set_preview``
        is queued after the worker's ``_deliver_preview`` closure for
        the same id, the recipe write is the final state. Recipes that
        want a guaranteed write with no worker activity at all should
        still construct the Browser with ``get_preview=None``.

        **Registration prerequisite.** This is a no-op when ``id_`` is
        not present in ``_items_by_id``. Preview storage lives on the
        Item (``Item.preview`` / ``Item.preview_render``) so without a
        registered Item there is nowhere to write. To cache preview
        text for an id, ensure the Item exists first — the cheapest
        idiom is an idempotent upsert with no field changes::

            b.update_data([upsert(id_, parent_id)])
            b.set_preview(id_, text)

        For an existing id this is patch-merge-with-no-fields (no-op);
        for a missing id it creates a minimal Item under ``parent_id``
        with default field values (``title=''`` backfilled from
        ``str(id)``, ``tag=''``, ``has_children=False``, etc.) unless
        you pass them explicitly. Pair with the children-list
        authority rule on :meth:`update_data` — registering one item
        via upsert puts it in the parent's children list, so you must
        eventually push the full sibling set under that parent.
        """
        # Thin wrapper around ``update_data`` so single-call writes
        # share the batch-lane semantics (#446). Recipes composing a
        # large preview (e.g. ``_preview_umbrella``) can fold many
        # ``set_preview_op`` entries into one batch.
        self.update_data([set_preview_op(id_, text)])

    def append_preview(self, id_, chunk) -> None:
        """(thread-safe) Append ``chunk`` to the cached preview for ``id_``.

        The append is scheduled on the main thread via ``post()`` so the
        read-modify-write of ``item.preview`` is race-free. If ``id_``
        has no Item in the index, the append is silently dropped (the
        item is not loaded). If the Item's ``preview`` is ``None`` it
        is initialised to ``''`` before appending. ``chunk`` is coerced
        to ``''`` if None.

        Marks the preview pane dirty so the next render pass picks up
        the new content.

        Cache shape note: preview text now lives on ``Item.preview`` so
        appending to one id does not affect any other. The renderer
        reads ``item.preview`` for the cursor id; an append for a
        non-cursor id is buffered silently until the user navigates to
        that item.

        Ordering: ``set_preview`` / ``append_preview`` / ``clear_preview``
        all route through the post queue, so calls land in FIFO order
        on the main thread. The framework's preview worker also delivers
        via the post queue (#442), so worker and recipe writes share a
        single FIFO ordering — see :meth:`set_preview` for the
        worker-vs-recipe race semantics.

        **Registration prerequisite.** This is a no-op when ``id_`` is
        not present in ``_items_by_id`` (no Item, nowhere to append).
        See :meth:`set_preview` for the idempotent-ensure pattern.
        """
        # Delegates to ``update_data`` (#446) so streaming appends and
        # tree mutations can share a single batch when the caller folds
        # them together.
        self.update_data([append_preview_op(id_, chunk)])

    def clear_preview(self, id_) -> None:
        """(thread-safe) Drop cached preview text for ``id_``.

        Scheduled on the main thread via ``post()``. Idempotent — a
        clear for an unknown id is a silent no-op. Marks the preview
        pane dirty so the next render shows the cleared state (which,
        for the cursor item, is rendered as an empty pane until a
        worker fetch or push repopulates the entry).

        **Registration prerequisite.** This is a no-op when ``id_`` is
        not present in ``_items_by_id``. See :meth:`set_preview` for
        the idempotent-ensure pattern.
        """
        # Delegates to ``update_data`` (#446); a clear merged with tree
        # ops into one batch is a single post-queue wake.
        self.update_data([clear_preview_op(id_)])

    def invalidate_preview(self, id_) -> None:
        """(thread-safe) Drop cached preview for ``id_`` and re-fetch.

        Use this when the preview *text* is stale but the cursor has
        not moved — e.g., an umbrella whose composed body depends on
        children that just streamed in, or a file whose content
        changed on disk. The view state (``_preview_scroll``,
        ``_preview_at_tail``, ``_help_mode``) is preserved so a user
        who pinned the view to the bottom keeps following the tail.

        Implemented as a main-thread post that drops the cache entry
        and calls ``request_preview(id_)`` to ask the worker for a
        fresh fetch. The result lands in ``item.preview`` via a
        ``_deliver_preview`` closure on the post queue (#442); the next
        render picks it up.

        Contrast with the cursor-move path: cursor changes are a
        "fresh view" signal — scroll resets to 0 and the tail pin is
        cleared. Cache invalidation without a cursor move is a "same
        view, refreshed content" signal — view state must survive.

        Idempotent for the same id; ``id_`` need not be the cursor's
        item — recipes can pre-emptively invalidate a soon-to-be-
        visited row.

        **Registration prerequisite.** The cache-drop step is a no-op
        when ``id_`` is not present in ``_items_by_id`` (no Item,
        nothing to drop). The ``request_preview`` kick still fires so
        the worker can populate a freshly-registered id; if the id
        remains unregistered the worker's result will also no-op on
        delivery. See :meth:`set_preview` for the idempotent-ensure
        pattern.
        """
        # Delegates to ``update_data`` (#446). The worker kick is
        # plumbed through ``state._preview_kicks`` and resolved in
        # ``update_data._apply``.
        self.update_data([invalidate_preview_op(id_)])

    # ---- preview-cache introspection -------------------------------------

    def get_cached_preview(self, id_) -> Optional[str]:
        """Return cached preview text for ``id_`` or ``None``.

        Synchronous read — does **not** call ``get_preview`` and does
        not schedule a worker fetch. Useful when a recipe wants to
        hand the currently-displayed preview text to an external
        consumer (e.g. an external pager / editor) without paying the
        latency of a re-fetch.

        Returns ``None`` if there is no cached entry (the worker has
        not yet delivered for this id, the id is unknown, or the entry
        was dropped via :meth:`drop_preview_cache`).
        """
        item = self._state._items_by_id.get(id_)
        return item.preview if item is not None else None

    def drop_preview_cache(self, id_=None) -> None:
        """(thread-safe) Drop cached preview text.

        ``id_=None`` (default) drops every entry — useful after a
        bulk mutation that invalidates every composed preview (e.g.
        a global filter flip that changes which children contribute
        to umbrella previews).

        When the dropped id matches the currently-displayed preview
        cursor (or ``id_=None``), the worker is kicked for the
        current cursor and the preview pane is flagged for redraw —
        recipes do not need to combine this call with
        :meth:`invalidate_preview` or hand-managed redraw signals.

        View state (``_preview_scroll``, ``_preview_at_tail``) is
        preserved so a user pinned to the tail keeps following.
        """
        # Delegates to ``update_data`` (#446). The cursor-kick decision
        # is plumbed through ``state._preview_kicks`` and resolved in
        # ``update_data._apply``, which still has access to
        # ``self._preview_cursor_id``.
        self.update_data([drop_preview_cache_op(id_)])

    @property
    def preview_item_id(self):
        """Id whose preview is currently displayed (or ``None``).

        Tracks the preview pane's worker target, not the row cursor.
        Usually equals the row cursor's id, but lags behind during
        rapid navigation while a worker fetch is in flight. Recipes
        that want "is the user looking at this id right now?" should
        check against ``preview_item_id`` rather than the row cursor.
        """
        return self._preview_cursor_id

    @property
    def preview_width(self) -> int:
        """Current width of the preview pane in terminal columns.

        Refreshed on every render by ``_layout_for`` (050-render.py) —
        recipes calling this from ``get_preview`` see the value that
        sized the *current* paint, so resizes (SIGWINCH),
        ``set_list_ratio`` / ``set_split`` changes, and ``show_preview``
        toggles all show up on the next preview fetch.

        Returns ``0`` until the first paint, while the preview pane is
        hidden, or when terminal geometry can't be read (headless tests,
        ``term_size`` raising, no tty). Callers that want a non-zero
        fallback should pick one explicitly, e.g.
        ``browser.preview_width or 80``.
        """
        return self._preview_width

    def preview_to_tail(self) -> None:
        """(thread-safe) Pin the preview view to the bottom of its content.

        Sets ``_preview_at_tail = True`` on the main thread; the
        renderer then overrides ``_preview_scroll`` to ``max_scroll``
        on every pass while the flag is set, so the view follows
        ``append_preview`` chunks and generator pulls without further
        user input.

        The flag clears automatically on any upward scroll motion
        (Shift/Alt-Up, Alt-PgUp, Shift/Alt-Home, wheel-up), on
        cursor-item change, and on help-mode toggle. Symmetric to
        ``nav_end`` (which pins the list cursor to the bottom). See
        ``docs/superpowers/specs/2026-05-17-preview-tail-design.md``.
        """
        def _apply():
            self._preview_at_tail = True
            self._needs_redraw.add('preview')
        self.post(_apply)

    def _invalidate_all_preview_renders(self) -> None:
        """Drop ``preview_render`` on every loaded Item.

        Called when the wrap inputs change globally — terminal resize
        (width changes ⇒ wrap geometry changes) and ``preview_ansi``
        toggle (SGR re-emit policy changes ⇒ wrapped bytes change).
        The raw ``preview`` text is untouched; only the wrap cache
        goes. Cheap walk: empty when no items are loaded, and only
        live items survive (orphaned previews are impossible since
        ticket #422 moved the storage onto the Item).
        """
        for item in self._state._items_by_id.values():
            item.preview_render = None

    def children_grid_layout(self, children, width, show_ids='auto'):
        """Recompute and return the children-grid layout for the inputs.

        Public API consumed by ``layout_panes`` (sizing the grid pane)
        and ``render_children_grid`` (drawing it). Returns a
        ``ChildrenGridLayout`` namedtuple — mirrors the tuple shape
        ``_sub_layout`` produces, so existing call sites that unpack
        ``num_cols, col_width, slot_rows, entry_lines = ...`` work
        unchanged.

        Always recomputes — the layout is cheap enough that caching
        adds more complexity than it saves (#434 reverses #414's
        cache). The result is stored on ``self._children_grid_layout``
        so callers can read the last-computed layout without
        re-passing inputs.
        """
        # _sub_layout lives in 050-render.py; in the concatenated build
        # it's a bare name in the same namespace, and the test loader
        # injects it onto this module so Browser can reach it.
        layout = _sub_layout(children, width, show_ids=show_ids)
        if not isinstance(layout, ChildrenGridLayout):
            layout = ChildrenGridLayout(*layout)
        self._children_grid_layout = layout
        return layout

    def set_list_ratio(self, ratio: float) -> None:
        """(thread-safe) Set the list pane's share of total terminal rows (clamped).

        The clamp range is ``[_LIST_RATIO_MIN, _LIST_RATIO_MAX]`` —
        outside that range the layout produces degenerate panes and
        the user can't recover with hotkey nudges. The layout
        independently enforces a minimum-1 list / minimum-2 preview
        when the terminal has room; this method's clamp is a sanity
        guardrail, not the live floor. Mutation is deferred to the
        main thread via ``post`` (see ``_do_set_list_ratio``).
        """
        self.post(lambda: self._do_set_list_ratio(ratio))

    def set_split(self, s: str) -> None:
        """(thread-safe) Set the split-layout selector (clamped to ``_VALID_SPLITS``).

        Invalid values (unknown codes, non-strings, ``None``) fall back
        to the historic default ``'h'``. Mirrors ``set_list_ratio``: the
        clamp is a guardrail, not a live floor — the layout helpers in
        050-render produce sane geometries even at degenerate sizes.
        Marks the full screen for redraw so the next render pass picks
        up the new layout family. Mutation is deferred to the main
        thread via ``post`` (see ``_do_set_split``).
        """
        self.post(lambda: self._do_set_split(s))

    def select(self, ids, replace: bool = False) -> None:
        """(thread-safe) Add ``ids`` to ``selected`` (or replace existing selection if ``replace``).

        The actual mutation runs on the main thread so the renderer
        never sees a torn set. Phase 1 stores the ids verbatim; the
        renderer in #10 reads the set when emitting ``*`` markers.
        """
        # Snapshot the iterable on the calling thread so the lambda
        # doesn't capture a mutating live source.
        ids_list = list(ids)
        self.post(lambda: self._do_select(ids_list, replace))

    # ---- mode + search inspection / control -----------------------------

    @property
    def mode(self) -> 'Mode':
        """Current user-input dispatch mode (``Mode`` enum).

        ``Mode.NORMAL`` — keystrokes dispatch through the action keymap.
        ``Mode.SEARCH_EDIT`` — ``/`` prompt open, user is typing a search.
        ``Mode.FILTER_EDIT`` — ``&`` prompt open, user is typing a filter.

        Recipes can branch on this to decide whether a ``ctx.message``
        write would clobber an in-progress prompt.
        """
        return self._mode

    @property
    def search_query(self) -> str:
        """The currently-active search query string (``''`` if none).

        Mirrors what the user typed at the ``/`` prompt and what the
        renderer highlights in matching rows. Includes the live
        entry while ``Mode.SEARCH_EDIT`` is active. Empty string
        means no search is in effect.
        """
        return self._search_query

    def set_search_query(self, text: str) -> None:
        """(thread-safe) Replace the current search query with ``text``.

        Parallels :meth:`set_filters` for the search lane. Empty
        string clears the search. The framework re-highlights and
        re-anchors the cursor to the nearest match on the next
        drain. Forces ``Mode.NORMAL`` (exits any in-progress prompt).
        """
        new_query = '' if text is None else str(text)
        def _do():
            self._mode = Mode.NORMAL
            self._search_query = new_query
            if new_query:
                # Re-jump to the nearest match like a user-typed query.
                _search_jump_nearest(self)
            self._needs_redraw.add('list')
            self._needs_redraw.add('info')
        self.post(_do)

    def clear_search(self) -> None:
        """(thread-safe) Drop the search query; alias for ``set_search_query('')``."""
        self.set_search_query('')

    # ---- scope ----------------------------------------------------------

    @property
    def scope(self):
        """Current scope id, or ``None`` at the root.

        Equivalent to ``state.scope_stack[-1] if state.scope_stack else None``.
        """
        s = self._state.scope_stack
        return s[-1] if s else None

    @property
    def scope_stack(self) -> tuple:
        """Ancestor chain (root-first) of the current scope, as a tuple.

        Empty at the root. Read-only — to change scope use
        :meth:`scope_into` / :meth:`scope_out`.
        """
        return tuple(self._state.scope_stack)

    def scope_into(self, id_) -> None:
        """(thread-safe) Drill into the item with ``id_``.

        Pushes ``id_`` onto ``scope_stack``, restores the
        ``_expanded_by_scope`` set for the new scope, lands the
        cursor on row 0 of the new view, and kicks a fetch for
        ``id_``'s children if they aren't cached yet. Fires
        ``on_scope_change`` (if installed) after the transition.

        No-op when ``id_`` is already the current scope.
        """
        def _do():
            state = self._state
            if state.scope_stack and state.scope_stack[-1] == id_:
                return
            # Capture the scope we're leaving (``None`` at root) before
            # the transition mutates the stack, to thread into the hook.
            prev_scope_id = self.scope
            scope_into(state, id_)
            state.cursor = 0
            if id_ not in state._children:
                self._do_expand(id_, Pending(), False)
            # Scope change is a full-walk trigger (see
            # ``docs/superpowers/specs/2026-05-27-filter-visible-tree-only-design.md``
            # "Recompute triggers" table). The walk re-evaluates
            # ``_filter_hidden`` on the new visible tree rooted at the
            # new scope — critically, the scope-row exemption inside
            # ``_filter_visit_subtree`` fires for ``id_`` so a row that
            # was ``_filter_hidden=True`` from a prior recompute is
            # unhidden the moment the user scopes into it. No-op when
            # no filter is active. ``scope_into`` (state-layer) already
            # set ``_visible_dirty``.
            if self._filters:
                _recompute_filter_hidden(
                    self._state, self._filters, show_ids=self.show_ids,
                )
            self._needs_redraw.add('all')
            mark_cursor_changed(self)
            self._fire_scope_change(self.scope, prev_scope_id, 'in')
            # Re-baseline the expand/collapse diff: ``scope_into`` just
            # restored the new scope's per-scope expanded set, which the
            # drain-time diff would otherwise read as a burst of expands /
            # collapses. A scope transition is an ``on_scope_change``
            # event, not an expand — so anchor ``_last_expanded`` to the
            # restored set and the next diff sees no delta. Intentionally
            # left unconditional (not gated on a handler like the #627
            # fire-path skips): scope transitions are rare, not per-drain,
            # and this keeps the baseline correct if a handler is ever set.
            self._last_expanded = set(self._state.expanded)
        self.post(_do)

    def scope_out(self) -> None:
        """(thread-safe) Pop the top of ``scope_stack``.

        No-op when already at the root. After popping, lands the
        cursor on the row of the id we just drilled into (so the
        user feels "I came back from there"). Falls back to row 0
        if the popped id isn't found in the new visible list. Fires
        ``on_scope_change`` (if installed) after the transition.
        """
        def _do():
            state = self._state
            # Capture the scope we're leaving (``None`` at root, though
            # that path early-returns below) before popping the stack.
            prev_scope_id = self.scope
            popped = scope_out(state)
            if popped is None:
                return
            # Scope change is a full-walk trigger (see scope_into hook).
            # Run BEFORE the visible_items() walk below so the cursor
            # search sees the new ``_filter_hidden`` flags — without
            # this, the post-scope-out walk would pre-filter the
            # popped row when the new scope row's exemption hasn't
            # fired yet.
            if self._filters:
                _recompute_filter_hidden(
                    self._state, self._filters, show_ids=self.show_ids,
                )
            placed = False
            for i, entry in enumerate(visible_items(state)):
                if entry.kind == 'normal' and entry.item.id == popped:
                    state.cursor = i
                    placed = True
                    break
            if not placed:
                state.cursor = 0
            self._needs_redraw.add('all')
            mark_cursor_changed(self)
            self._fire_scope_change(self.scope, prev_scope_id, 'out')
            # Re-baseline the expand/collapse diff — symmetric with
            # ``scope_into``. ``scope_out`` restored the parent scope's
            # expanded set; anchor ``_last_expanded`` to it so the restore
            # doesn't masquerade as expands/collapses. Intentionally left
            # unconditional (not gated on a handler like the #627 fire-path
            # skips): runs only on a rare scope transition, not per drain.
            self._last_expanded = set(self._state.expanded)
        self.post(_do)

    # ---- expansion helpers -----------------------------------------------

    def collapse_all(self) -> None:
        """(thread-safe) Clear the expanded set for the current scope.

        Drops every entry from ``state.expanded`` — every previously
        open subtree collapses to its parent row. The current scope
        stack is unaffected; scoping is a separate concept.

        Cursor is preserved on its current id when possible. If the
        cursor was on a row whose parent was expanded, the
        framework's cursor-anchor mechanism walks back to the
        nearest still-visible ancestor.
        """
        def _do():
            self._state.expanded.clear()
            mark_visible_dirty(self._state)
            # Re-anchor cursor so it lands on a still-visible row
            # (the row it pointed at may be inside a collapsed subtree).
            self._reanchor_cursor()
            self._apply_cursor_anchor()
            self._needs_redraw.add('all')
            mark_cursor_changed(self)
        self.post(_do)

    def expand_subtree(self, id_, lazy: bool = True) -> None:
        """(thread-safe) Expand every cached descendant of ``id_``.

        Walks the cached children tree under ``id_`` and adds every
        branch (item with ``has_children=True``) to ``state.expanded``.
        ``id_`` itself is added too, so calling this on a collapsed
        row opens it and everything below it that the framework
        already knows about.

        ``lazy=True`` (default): only walks what is currently cached;
        un-fetched branches stay collapsed and will fetch the normal
        way (cursor-into / user expand) later. ``lazy=False`` is
        reserved for a future "force-fetch everything" mode and
        currently behaves the same as ``True``.
        """
        def _do():
            state = self._state
            def _walk(pid):
                state.expanded.add(pid)
                children = state._children.get(pid)
                if children is None:
                    return
                for c in children:
                    if getattr(c, 'has_children', False):
                        _walk(c.id)
            _walk(id_)
            mark_visible_dirty(state)
            self._needs_redraw.add('all')
        self.post(_do)

    # ---- worker supersede ----------------------------------------------

    def run_in_slot(self, name: str, fn) -> 'CancellationToken':
        """(thread-safe) Run ``fn(token)`` in a daemon thread; supersede prior run.

        ``name`` identifies a "slot" — if another worker is currently
        running in the same slot, its token is cancelled before the
        new worker starts. Use cases: live-as-you-type computation
        (collapse 30 keystrokes-worth of recompute into one running
        job), tail-feed refresh (cancel the slow refresh when the
        user navigates away).

        ``fn`` is called as ``fn(token)``; the recipe must call
        ``token.is_cancelled()`` at safe checkpoints (typically
        every loop iteration, every chunk read, etc.). The framework
        does NOT kill the thread — cancellation is purely
        cooperative.

        Returns the new :class:`CancellationToken`. Recipes can hold
        it to cancel manually (``token.cancel()``), or let
        re-submission to the same slot do it for them.

        Exceptions raised inside ``fn`` are caught and routed to
        :meth:`error` so a failing worker can't crash the process.
        """
        with self._slots_lock:
            prev = self._slots.get(name)
            token = CancellationToken()
            self._slots[name] = token
        if prev is not None:
            prev.cancel()

        def _runner():
            try:
                fn(token)
            except Exception as e:
                self.error(
                    f'run_in_slot({name!r}): {type(e).__name__}: {e}'
                )
            finally:
                with self._slots_lock:
                    if self._slots.get(name) is token:
                        del self._slots[name]

        t = threading.Thread(
            target=_runner, daemon=True,
            name=f'browse-tui-slot-{name}',
        )
        t.start()
        return token

    # ---- selection helpers ----------------------------------------------

    def select_all_visible(self) -> None:
        """(thread-safe) Set selection to every visible normal row.

        WYSIWYG: anything previously selected that isn't visible
        (hidden rows, children of collapsed parents, items in other
        scopes) is dropped. Placeholder rows are skipped.
        """
        def _do():
            state = self._state
            state.selected.clear()
            for entry in visible_items(state):
                if entry.kind == 'normal':
                    state.selected.add(entry.item.id)
            self._needs_redraw.add('list')
            self._needs_redraw.add('info')
            self._fire_selection_change()
        self.post(_do)

    def clear_selection(self) -> None:
        """(thread-safe) Drop every entry from ``state.selected``.

        No-op when the selection is already empty.
        """
        def _do():
            state = self._state
            if not state.selected:
                return
            state.selected.clear()
            self._needs_redraw.add('list')
            self._needs_redraw.add('info')
            self._fire_selection_change()
        self.post(_do)

    def invert_selection(self) -> None:
        """(thread-safe) Flip selection across every visible normal row.

        Visible rows that were selected become deselected and
        vice-versa. Selection state for non-visible rows is preserved
        as-is. Placeholder rows are ignored.
        """
        def _do():
            state = self._state
            changed = False
            for entry in visible_items(state):
                if entry.kind != 'normal':
                    continue
                if entry.item.id in state.selected:
                    state.selected.discard(entry.item.id)
                else:
                    state.selected.add(entry.item.id)
                changed = True
            if changed:
                self._needs_redraw.add('list')
                self._needs_redraw.add('info')
                self._fire_selection_change()
        self.post(_do)

    def cancel(self, *pendings: 'Pending') -> None:
        """(thread-safe) Mark one or more Pendings cancelled (sugar for ``p.cancel()``).

        Idempotent on already-cancelled or already-resolved Pendings.
        Worker fetches are not killed -- cancellation is non-strict and
        only suppresses chained ``.then()`` callbacks from firing. Useful
        when the user has moved on and a stale chain (e.g. cursor-to a
        no-longer-relevant id) should not fire.
        """
        for p in pendings:
            p.cancel()

    def quit(self, code: int = 0, output: str = '') -> None:
        """(thread-safe) Request the main loop to exit with the given exit code.

        Phase 1 stores ``_quit_requested``/``_quit_code``/
        ``_quit_output`` on Browser; the main loop in #13 reads these
        and shuts down once the current drain finishes.
        """
        self.post(lambda: self._do_quit(code, output))

    # =========================================================================
    # End of public, thread-safe API
    # =========================================================================

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
        # ``_preview_resume_event`` wakes a paused preview generator
        # so it can observe ``_stop`` and exit cleanly.
        self._children_event.set()
        self._preview_event.set()
        self._preview_resume_event.set()
        if self._children_thread:
            self._children_thread.join(timeout=timeout)
        if self._preview_thread:
            self._preview_thread.join(timeout=timeout)
        self._workers_running = False

    # ---- main-loop drain (production main loop + tests) ----------------

    # ---- lifecycle-hook dispatch ------------------------------------
    #
    # Each helper guards against missing hooks and catches exceptions
    # raised by the user-supplied callback so a buggy recipe hook can
    # never crash the main loop. ``on_quit`` swallows silently — it
    # fires during shutdown and there's nowhere left to surface the
    # error. The others route through :meth:`error` so the user sees
    # what went wrong.

    def _make_ctx_for_hook(self):
        """Construct a Context for hook invocation.

        Imported lazily because the Context class lives in 060-context.py
        which loads after this module; at run-time the concatenated build
        merges everything into one module so the bare ``Context`` name
        is resolvable here.
        """
        return Context(self)

    def _fire_cursor_change_if_pending(self) -> None:
        """Fire ``on_cursor_change`` once if the cursor id changed.

        Debounced: even if ``mark_cursor_changed`` was called several
        times between drains, the hook fires at most once per drain —
        and only if the *id* under the cursor differs from the last
        fire. Cursor moves that land back on the same row id (anchor
        re-positioning, hide-displacement settling) are a no-op.
        """
        if not self._cursor_change_pending:
            return
        self._cursor_change_pending = False
        if self._on_cursor_change is None:
            return
        vis = visible_items(self._state)
        cur_id = None
        if 0 <= self._state.cursor < len(vis):
            entry = vis[self._state.cursor]
            if entry.kind == 'normal':
                cur_id = entry.item.id
        if cur_id == self._last_cursor_id:
            return
        self._last_cursor_id = cur_id
        try:
            self._on_cursor_change(self._make_ctx_for_hook(), cur_id)
        except Exception as e:
            self.error(f'on_cursor_change: {type(e).__name__}: {e}')

    def _fire_scope_change(self, scope_id=None, prev_scope_id=None,
                           direction=None) -> None:
        """Fire ``on_scope_change`` after a scope transition.

        ``scope_id`` is the new current scope id (``None`` at root),
        ``prev_scope_id`` the scope just left (``None`` at root), and
        ``direction`` is ``'in'`` for a ``scope_into`` / ``'out'`` for a
        ``scope_out``. The Browser-level ``scope_into`` / ``scope_out``
        capture these and thread them through; a bare ``_fire_scope_change()``
        (no transition context) passes ``None`` for all three.
        """
        if self._on_scope_change is None:
            return
        try:
            self._on_scope_change(self._make_ctx_for_hook(),
                                  scope_id, prev_scope_id, direction)
        except Exception as e:
            self.error(f'on_scope_change: {type(e).__name__}: {e}')

    def _fire_selection_change(self) -> None:
        """Fire ``on_selection_change`` if installed.

        Passes the resulting selected ids as a list. ``state.selected``
        is a set; we sort so the payload is stable across fires (the
        underlying set order is unspecified).
        """
        if self._on_selection_change is None:
            return
        try:
            ids = sorted(self._state.selected)
            self._on_selection_change(self._make_ctx_for_hook(), ids)
        except Exception as e:
            self.error(f'on_selection_change: {type(e).__name__}: {e}')

    def _fire_expand_collapse_if_pending(self) -> None:
        """Fire ``on_collapse`` / ``on_expand`` from a drain-time set diff.

        Diffs ``state.expanded`` against ``_last_expanded``:
        ``added = expanded - last`` fires one ``on_expand(list(added))``,
        ``removed = last - expanded`` fires one ``on_collapse(list(removed))``.
        A whole burst between drains (Alt-Right / Alt-Left, ``collapse_all``,
        ``expand_subtree``) is therefore delivered as a single call; a drain
        that nets to no change fires nothing. ``on_collapse`` fires before
        ``on_expand`` per the documented drain order.

        No pending flag: when a handler is installed the diff runs every
        drain so the many places that mutate ``state.expanded`` — including
        the direct ``state.expanded.discard(...)`` in the actions layer —
        are caught with no instrumentation at the mutation sites (the design
        intent; the #500 failure mode prevented). The expanded set is small,
        so a per-drain set diff is cheap.

        When BOTH ``on_expand`` and ``on_collapse`` are unset (#627) the
        whole prep — the diff AND the ``set(expanded)`` snapshot — is
        skipped, so ``_last_expanded`` is NOT re-snapshotted and may go
        stale while there is no handler. That is fine: hooks are
        construction-time-fixed (set via ``BrowserConfig`` / before
        ``run()``), so the snapshot is only consulted once a handler exists,
        and it's re-baselined on every scope transition. This matches
        ``on_cursor_change``, which likewise doesn't advance
        ``_last_cursor_id`` when unset. (List order is unspecified — recipes
        that need a stable order should sort.)
        """
        if self._on_expand is None and self._on_collapse is None:
            return
        expanded = self._state.expanded
        if expanded != self._last_expanded:
            removed = self._last_expanded - expanded
            added = expanded - self._last_expanded
            if removed and self._on_collapse is not None:
                try:
                    self._on_collapse(self._make_ctx_for_hook(),
                                      list(removed))
                except Exception as e:
                    self.error(f'on_collapse: {type(e).__name__}: {e}')
            if added and self._on_expand is not None:
                try:
                    self._on_expand(self._make_ctx_for_hook(), list(added))
                except Exception as e:
                    self.error(f'on_expand: {type(e).__name__}: {e}')
            self._last_expanded = set(expanded)

    def _fire_children_loaded_if_pending(self) -> None:
        """Fire ``on_children_loaded`` once with the parents that settled.

        Drains ``_children_loaded_pending`` — the set populated by the
        genuine-settlement sites (the ``complete`` op tail via
        ``apply_ops``, and ``apply_children_results``) — into a single
        ``on_children_loaded(list)`` call per drain. A full refresh that
        refetches several expanded parents therefore delivers them
        batched as their fetches settle in the same tick.

        The pending set is ALWAYS cleared, even when no handler is
        installed or the handler raises, so a handler registered later
        never observes a stale historical settlement and a throw can't
        wedge the set. Exceptions route to :meth:`error` (never crash
        the loop), matching the other hooks. ``clear_children`` does not
        populate the set, so a cache-drop never fires here.
        """
        if not self._children_loaded_pending:
            return
        parent_ids = list(self._children_loaded_pending)
        self._children_loaded_pending.clear()
        if self._on_children_loaded is None:
            return
        try:
            self._on_children_loaded(self._make_ctx_for_hook(), parent_ids)
        except Exception as e:
            self.error(f'on_children_loaded: {type(e).__name__}: {e}')

    def _fire_search_change_if_pending(self) -> None:
        """Fire ``on_search_change`` once if the effective query changed.

        Diffs ``self._search_query`` against ``_last_search_query``: any
        delta fires one ``on_search_change(ctx, query)`` with the new
        string, then re-snapshots. Several keystrokes between drains
        coalesce to the final value; clearing to ``''`` is a real change
        and fires once; an identical re-set is a no-op.

        No pending flag: when a handler is installed the diff runs every
        drain so every mutation source — live ``SEARCH_EDIT`` typing,
        commit, ``set_search_query`` / ``clear_search`` — is caught without
        instrumenting the mutation sites. When ``on_search_change`` is unset
        (#627) the prep (the diff and the snapshot) is skipped entirely, so
        ``_last_search_query`` is NOT re-snapshotted and may go stale; that
        is fine because hooks are construction-time-fixed (matches
        ``on_cursor_change``, which doesn't advance its snapshot when unset).
        """
        if self._on_search_change is None:
            return
        query = self._search_query
        if query != self._last_search_query:
            self._last_search_query = query
            try:
                self._on_search_change(self._make_ctx_for_hook(), query)
            except Exception as e:
                self.error(f'on_search_change: {type(e).__name__}: {e}')

    def _fire_filter_change_if_pending(self) -> None:
        """Fire ``on_filter_change`` once if the active filter tuple changed.

        Diffs ``tuple(self.filters)`` (the committed-plus-live, empties
        dropped) against ``_last_filters``: any delta fires one
        ``on_filter_change(ctx, filters)`` with the new tuple, then
        re-snapshots. ``set_filters`` / ``add_filter`` / ``clear_filters``
        and the ``&`` edit/commit path all flow through this; an identical
        re-set is a no-op, and ``add_filter('')`` is a no-op because the
        ``filters`` property drops empty strings. Same skip-when-unset
        contract as ``_fire_search_change_if_pending``: when
        ``on_filter_change`` is unset (#627) the prep — including building
        ``tuple(self.filters)`` — is skipped, so ``_last_filters`` is NOT
        re-snapshotted and may go stale; fine because hooks are
        construction-time-fixed (matches ``on_cursor_change``).
        """
        if self._on_filter_change is None:
            return
        filters = tuple(self.filters)
        if filters != self._last_filters:
            self._last_filters = filters
            try:
                self._on_filter_change(self._make_ctx_for_hook(), filters)
            except Exception as e:
                self.error(f'on_filter_change: {type(e).__name__}: {e}')

    def _fire_resize_if_pending(self) -> None:
        """Fire ``on_resize`` once when an observed resize changed the size.

        Gated by ``_resize_pending`` — latched at the SIGWINCH observation
        points in the run loop (mirrors ``_cursor_change_pending``) so
        ``term_size()`` is only read on an actual resize, not every tick.
        When pending, reads ``term_size()`` and, if ``(cols, rows)`` differs
        from ``_last_size``, fires ``on_resize(ctx, cols, rows)`` and
        re-snapshots.

        When ``on_resize`` is unset (#627) the pending flag is still cleared
        (so a single SIGWINCH never lingers) but the method early-returns
        BEFORE reading ``term_size()`` — the syscall and the snapshot are
        skipped, so ``_last_size`` may go stale; fine because hooks are
        construction-time-fixed (matches ``on_cursor_change``).

        ``term_size`` can be missing, raise, or return ``(0, 0)`` in a
        headless / no-tty context (mirrors ``_clamp_split`` /
        ``_list_pane_height_safe``); in any of those cases we don't fire
        garbage dimensions and leave ``_last_size`` untouched.
        """
        if not self._resize_pending:
            return
        self._resize_pending = False
        if self._on_resize is None:
            return
        ts = globals().get('term_size')
        if ts is None:
            return
        try:
            cols, rows = ts()
        except Exception:
            return
        if not cols or not rows:
            return
        size = (cols, rows)
        if size != self._last_size:
            self._last_size = size
            try:
                self._on_resize(self._make_ctx_for_hook(), cols, rows)
            except Exception as e:
                self.error(f'on_resize: {type(e).__name__}: {e}')

    def _fire_on_quit(self) -> None:
        """Fire ``on_quit`` once during shutdown.

        Exceptions are swallowed silently — a failing cleanup hook
        should not block exit.
        """
        if self._on_quit is None:
            return
        cb, self._on_quit = self._on_quit, None  # arm once
        try:
            cb(self._make_ctx_for_hook(), self._quit_code)
        except Exception:
            pass

    def drain_main_queue(self) -> int:
        """Run posted callables until the queue is empty; return how many ran.

        Callables that post further work end up running in the same
        drain -- the loop keeps pulling via ``get_nowait`` until
        ``queue.Empty`` is raised. The tight-loop risk this implies is
        addressed elsewhere (callers throttle re-posting work).
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
        # Collect genuine settlements (#600) for this delivery pass.
        # Reset before populating so we read only this pass's parents
        # (the same discipline ``apply_ops`` uses); harvested into
        # ``_children_loaded_pending`` after the loop.
        self._state._settled_parents = []
        while self._children_results:
            id_, items = self._children_results.popleft()
            # Replace the cached list — drop the old entries from the
            # auxiliary indexes first so a refresh that swaps the item
            # set under ``id_`` doesn't leave stale ``_items_by_id`` /
            # ``_parent_of_id`` rows pointing at the previous list.
            _index_drop_children(self._state, id_)
            # Promote any synthetic stubs whose ids match the freshly-
            # delivered children before installing the list. The stub's
            # ``_items_by_id`` entry is preserved (identity-stable);
            # ``_index_add_children`` below then writes the same
            # identity back under the matched id.
            items = _promote_synthetics(self._state, items)
            self._state._children[id_] = items
            _index_add_children(self._state, id_, items)
            self._state._children_pending.discard(id_)
            # Worker delivered → no longer loading. Stays addressable
            # via ``_loading`` rather than implied membership in
            # ``_children_pending`` (foundation for ``update_data``).
            # ``settled=True``: the children list was just installed
            # above, so this is a genuine settlement → fires
            # ``on_children_loaded``.
            _set_loading(self._state, id_, False, settled=True)
            mark_visible_dirty(self._state)
            for p in self._children_in_flight.pop(id_, []):
                p._resolve()
            n += 1
        # Move this pass's settlements into the drain-time pending set.
        # Gated on the handler (#627): no listener → pending stays empty.
        if (self._on_children_loaded is not None
                and self._state._settled_parents):
            self._children_loaded_pending.update(self._state._settled_parents)
        if n:
            # Re-evaluate filter visibility — freshly-delivered items
            # are reachable now and need their ``_filter_hidden`` flags
            # set, otherwise an active filter wouldn't apply to them.
            # No-op when ``_filters`` is empty.
            if self._filters:
                _recompute_filter_hidden(
            self._state, self._filters, show_ids=self.show_ids,
        )
            # Re-snap the cursor onto its anchored id (or closest
            # fallback) before the index clamp runs, so the clamp only
            # fires when the entire anchor chain is missing from the
            # new visible list. See ``_apply_cursor_anchor`` for the
            # walk order (primary → next → prev → ancestors).
            self._apply_cursor_anchor()
            # Re-apply the scroll-to-fit expand goal so a streaming
            # subtree keeps moving into view as deliveries arrive.
            # No-op if no goal is parked.
            self._apply_expand_goal()
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

    def _deliver_preview(self, id_, text) -> None:
        """Main-thread closure that caches a worker-produced preview (#442).

        Posted by ``_preview_worker`` after each non-generator fetch.
        Caches ``text`` on ``Item.preview`` unconditionally — even if
        the cursor has moved on, the work is preserved so a back-cursor
        visit doesn't re-fetch. Flags the preview pane dirty only when
        the cursor is still pointing at ``id_``; an out-of-band delivery
        for a now-unseen id doesn't trigger a redundant repaint.

        Clears ``_preview_req`` when it still equals ``id_`` so the
        request slot drains naturally — both ``run_until_idle`` and
        the streaming-pause checks rely on ``_preview_req is None`` as
        the "no pending preview fetch" signal. When the slot holds a
        different id (the user moved the cursor mid-fetch), leave it
        alone — the worker will see the newer id on its next loop.

        ``text`` is coerced to ``''`` if None (defensive — the worker
        already coerces, but ``_deliver_preview`` is also a stable
        injection point recipes could call directly).
        """
        if text is None:
            text = ''
        item = self._state._items_by_id.get(id_)
        if item is not None:
            item.preview = text
            # Wrap cache (#422) — drop the stale render so the next
            # paint regenerates against the new text.
            item.preview_render = None
        # Drain the request slot for this id (latest-wins: don't clobber
        # a newer pending request landed during the fetch).
        if self._preview_req == id_:
            self._preview_req = None
        # Conditional redraw: cursor may have moved during the fetch.
        # A delivery for the still-current id is what the user is
        # waiting to see; everything else just fills the cache.
        if id_ == self._preview_cursor_id:
            self._needs_redraw.add('preview')

    def run_until_idle(self, timeout: float = 2.0) -> None:
        """Test affordance: drain queues + wait for workers, until idle.

        Idle means: main_queue empty AND children_queue empty AND
        children_results empty AND _preview_req is None AND no
        in-flight pendings. Polls every 5ms and raises ``TimeoutError``
        if not idle within ``timeout``.

        Per #273: a preview-generator-paused state (worker holding a
        live generator, waiting for cursor-move or demand signal)
        counts as idle — the worker has voluntarily stopped pulling
        and won't make progress until an external signal arrives.
        ``_preview_req`` will still equal the paused id; the test
        affordance cross-checks ``_preview_paused`` to recognise this.

        Safe to call repeatedly. Production code uses the real main loop
        (ticket #13); this exists only so tests don't have to invent
        their own pump.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain_main_queue()
            self.apply_children_results()
            paused = self._preview_paused
            preview_busy = (
                self._preview_req is not None
                and not (paused is not None
                         and paused.get('id') == self._preview_req)
            )
            # Children prefetch slot is busy when its slot value differs
            # from the worker's local-id memo — meaning either the worker
            # hasn't picked up the latest request yet, or is mid-fetch
            # for it. When they match (worker has acted on this id, even
            # if to skip because cached), the slot is quiescent.
            children_prefetch_busy = (
                self._children_prefetch_req is not None
                and self._children_prefetch_req
                    != self._children_prefetch_local_id
            )
            if (self._main_queue.empty()
                    and not self._children_queue
                    and not self._children_results
                    and not preview_busy
                    and not children_prefetch_busy
                    and not self._children_in_flight
                    and not self._state._children_pending):
                return
            time.sleep(0.005)
        raise TimeoutError(
            f'run_until_idle: still busy after {timeout}s '
            f'(queue={self._main_queue.qsize()}, '
            f'children_queue={len(self._children_queue)}, '
            f'children_results={len(self._children_results)}, '
            f'preview_req={self._preview_req!r}, '
            f'preview_paused={self._preview_paused!r}, '
            f'in_flight={list(self._children_in_flight)}, '
            f'children_pending={list(self._state._children_pending)}, '
            f'children_prefetch_req={self._children_prefetch_req!r}, '
            f'children_prefetch_local_id='
            f'{self._children_prefetch_local_id!r})'
        )

    # ---- internal: refresh dispatch (main thread) -----------------------

    def _do_refresh(self, id_, pending):
        """Run the cache-invalidate + enqueue step on the main thread.

        Posted by ``refresh`` so concurrent callers don't race on the
        cache or the in-flight registry. ``id=None`` means full refresh
        (clear all caches and refetch the root **plus every expanded
        sub-parent and the current scope root**, so previously-visible
        sub-trees don't strand on a ``⧗ loading…`` placeholder until
        the cursor happens to navigate onto them — #294).

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
            # Snapshot expanded set + scope root BEFORE invalidation so
            # we can re-dispatch fetches for them too. Without this,
            # ``visible_items`` would emit a placeholder for each
            # expanded parent missing from ``_children`` and the only
            # auto-dispatch (``_update_children_for_cursor``) only fires
            # for the cursor item — leaving siblings/ancestors stuck.
            extra = set(self._state.expanded)
            if self._state.scope_stack:
                extra.add(current_scope(self._state))
            cache_invalidate_all(self._state)
            id_ = self._state.root_id
            extra.discard(id_)
        else:
            cache_invalidate_subtree(self._state, id_)
            extra = ()
        # Force the next ``_update_preview_for_cursor`` to re-fetch.
        # The ``_preview_cursor_id`` gate (#126) skips re-requests when
        # the cursor stays on the same item, but a refresh just
        # invalidated the underlying data, so the cached preview text
        # is stale and a re-fetch is the correct action.
        self._preview_cursor_id = None
        # Always register the waiter so it resolves with the fetch result.
        self._children_in_flight.setdefault(id_, []).append(pending)
        # Keep ``_loading`` in lockstep with the dispatch tracker so the
        # upcoming ``update_data`` ops (and any future readers) can rely
        # on the flag rather than peeking at ``_children_pending``. We
        # set this even on a coalesced re-dispatch: ``cache_invalidate_*``
        # above just dropped any pre-existing entry, and an in-flight
        # fetch is still loading by definition.
        self._state._loading[id_] = True
        # Only enqueue + flag pending the first time -- a fetch already in
        # flight for this id will deliver one result that resolves every
        # registered waiter together.
        #
        # ``reload=True`` fires only on the root refresh enqueue here.
        # The re-dispatched expanded ids below get ``reload=False`` —
        # the root call already triggered the recipe's full cache wipe,
        # so per-id signals would be redundant.
        if id_ not in self._state._children_pending:
            self._state._children_pending.add(id_)
            self._children_queue.append((id_, True))
        # Re-dispatch every previously-expanded parent (and the scope
        # root) so their sub-trees don't strand on the loading
        # placeholder. These are fire-and-forget — nobody specific is
        # waiting on them, so no Pending registration. Same coalescing
        # rules: skip ids already in flight.
        for x in extra:
            self._state._loading[x] = True
            if x not in self._state._children_pending:
                self._state._children_pending.add(x)
                self._children_queue.append((x, False))
        self._children_event.set()

    def _do_initial_fetch(self):
        """Main-thread: enqueue the initial root + scope/expanded fetches.

        Unlike ``_do_refresh(None)`` this does *not* invalidate caches
        or mark anything as a reload — startup is not a refresh. Recipes
        that build expensive per-file structures during scope resolution
        (e.g. browse-claude's ``_TREE_CACHE``) keep those caches.
        Mirrors ``_do_refresh``'s scope + expanded re-dispatch so a
        recipe that pre-set ``scope_stack`` / ``expanded`` before run()
        gets the same set of fetches kicked off at startup.

        Each enqueue is gated on the standard ``_children_pending`` /
        cache checks so re-runs are idempotent.
        """
        targets = [self._state.root_id]
        if self._state.scope_stack:
            targets.append(current_scope(self._state))
        targets.extend(self._state.expanded)
        seen = set()
        for id_ in targets:
            if id_ in seen:
                continue
            seen.add(id_)
            self._ensure_children_fetched(id_)
        self._children_event.set()

    def _ensure_children_fetched(self, id_) -> None:
        """Main-thread: queue a children fetch for ``id_`` if not cached.

        No-op when ``id_`` already has cached children or a fetch is
        already in flight for it. Used by ``_do_initial_fetch`` /
        ``_do_refresh`` to seed startup fetches, and by ``_scope_up``
        to lazy-load levels of a recipe-pre-pushed scope stack that
        the user never navigated through.

        Caller must be on the main thread. The worker reads
        ``_children_queue`` / ``_children_pending`` without locking,
        relying on GIL-protected single-mutator semantics: only the
        main thread appends.
        """
        if id_ in self._state._children:
            return
        if id_ in self._state._children_pending:
            return
        self._state._children_pending.add(id_)
        self._state._loading[id_] = True
        self._children_queue.append((id_, False))
        # Worker wake is the caller's responsibility — the initial-
        # fetch path batches multiple ids and wakes once at the end,
        # so leaving the kick here would double-wake. Callers that
        # ensure a single id should ``self._children_event.set()``
        # themselves.

    def _do_cursor_to(self, id_, pending):
        """Main-thread: position the cursor at ``id_`` and resolve ``pending``.

        Sets the sticky cursor anchor to ``[id_]`` so the loop keeps
        trying to land the cursor on ``id_`` as background deliveries
        rebuild the visible list. ``_apply_cursor_anchor`` snaps the
        cursor immediately if ``id_`` is already visible; otherwise the
        anchor stays parked and the next ``apply_children_results`` /
        ``update_data`` mutation will retry. ``pending`` resolves
        best-effort right away so chained ``.then()`` callbacks don't
        strand — the Pending says "the request was processed", not
        "the cursor is on the target yet".

        The snapshot starts as just ``[id_]`` (no fallback tiers): we
        haven't seen ``id_`` in the visible list yet, so we don't know
        its neighbours. The first successful ``_apply_cursor_anchor``
        hit on the primary will fill in the next/prev/parent tiers
        from the freshly-resolved row.
        """
        self._cursor_anchor = [id_]
        self._apply_cursor_anchor()
        pending._resolve()

    def _do_expand(self, id_, pending, autoscroll=False):
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

        ``autoscroll`` parks a sticky scroll-to-fit goal on a fresh
        expansion. The goal is applied here (synchronously, for the
        cached path or to land on the loading placeholder) and re-
        applied by ``apply_children_results`` / ``update_data._apply``
        as the subtree streams in.
        """
        was_expanded = id_ in self._state.expanded
        self._state.expanded.add(id_)
        # Set the scroll-to-fit goal on a fresh expansion (the first
        # transition from collapsed to expanded). Subsequent calls on
        # an already-expanded id are no-ops here and shouldn't reset
        # the goal — particularly if a recipe rapidly re-issues the
        # same expand.
        if autoscroll and not was_expanded:
            self._expand_goal = {'parent_id': id_}
        if id_ in self._state._children:
            mark_visible_dirty(self._state)
            # Filter hook (#501): evaluate the newly-revealed subtree
            # under the just-expanded parent. Walk each child via
            # ``_filter_visit_subtree`` so its ``_filter_hidden`` flag
            # (and the flags of its visible-expanded descendants) is
            # set against the current filter. The expanded parent
            # itself is NOT re-evaluated — preserves the parent's
            # scaffold/match status from the original recompute (the
            # stale-scaffold contract; see "Accepted UX trade-offs"
            # #2 in
            # ``docs/superpowers/specs/2026-05-27-filter-visible-tree-only-design.md``).
            # Placed before ``_apply_cursor_anchor`` so the anchor walk
            # sees the updated flags. No-op when no filter is active.
            if self._filters:
                active = [q for q in self._filters if q]
                if active:
                    scope_id = (
                        self._state.scope_stack[-1]
                        if self._state.scope_stack else None
                    )
                    for child in self._state._children.get(id_, ()):
                        _filter_visit_subtree(
                            self._state, child,
                            active, scope_id, self.show_ids,
                        )
            # Children already cached — expanding inserts those rows
            # into the visible list immediately. Re-snap the cursor on
            # its anchored id so its index doesn't drift if the
            # expansion happened above the cursor row. (Uncached path
            # goes through ``apply_children_results`` which also
            # re-snaps.)
            self._apply_cursor_anchor()
            self._apply_expand_goal()
            self._needs_redraw.add('list')
            pending._resolve()
            return
        self._state._children_pending.add(id_)
        # Keep ``_loading`` in lockstep with dispatch (see ``_do_refresh``).
        self._state._loading[id_] = True
        self._children_in_flight.setdefault(id_, []).append(pending)
        self._children_queue.append((id_, False))
        self._children_event.set()
        mark_visible_dirty(self._state)
        # Uncached: the subtree only has a ⧗ placeholder right now.
        # The goal scrolls just enough to show it; re-applies as real
        # children stream in via apply_children_results.
        self._apply_expand_goal()
        self._needs_redraw.add('list')

    def _do_select(self, ids, replace):
        """Main-thread: update ``selected`` set + flag list-pane redraw."""
        before = frozenset(self._state.selected)
        if replace:
            self._state.selected = set(ids)
        else:
            self._state.selected.update(ids)
        mark_visible_dirty(self._state)
        self._needs_redraw.add('list')
        if self._state.selected != before:
            self._fire_selection_change()

    def _do_quit(self, code, output):
        """Main-thread: flip the quit flag and stash exit code/output."""
        self._quit_code = code
        self._quit_output = output
        self._quit_requested = True
        # Wake the main loop so it observes _quit_requested promptly. In
        # headless mode notify_wake is a no-op; in production it writes
        # one byte to the self-pipe.
        notify_wake()

    def _do_set_list_ratio(self, ratio):
        """Main-thread: clamp + apply list_ratio, flag full redraw."""
        self.list_ratio = _clamp_list_ratio(ratio)
        self._needs_redraw.add('all')

    def _do_set_split(self, s):
        """Main-thread: clamp + apply split selector, flag full redraw."""
        self.split = _clamp_split(s)
        self._needs_redraw.add('all')

    # ---- workers --------------------------------------------------------

    def _children_worker(self):
        """Children worker thread: two-pass slot-first loop (#481).

        Each outer iteration runs two passes:

        Pass 1: drain the cursor-prefetch slot ``_children_prefetch_req``
        until quiescent. A new slot value (``!= _children_prefetch_local_id``)
        is handled in one of three ways:

          * ``slot_id not in state._children_pending`` — fresh work.
            Add ``slot_id`` to ``_children_pending`` and call
            ``_fetch_and_deliver_children(slot_id, reload=False)``.
            The helper short-circuits on a cache hit.
          * ``slot_id in state._children_pending`` AND a matching FIFO
            entry exists — promote: ``remove`` the entry from the
            queue, copy its ``reload`` flag, and fetch via the helper
            so the cursor's work carries any explicit refresh intent
            instead of silently downgrading to ``reload=False``.
          * ``slot_id in state._children_pending`` AND no FIFO entry —
            in flight from a previous slot iteration's helper call.
            ``continue``; ``local_id == slot_id`` short-circuits the
            inner loop on the next read.

        ``local_id`` is updated whenever a new slot value is observed,
        so a stable slot (cursor parked on the same id) doesn't spin.
        Continuous cursor movement during the burst stays in this
        inner loop — Pass 2 only runs once the slot settles. See the
        "Continuous scroll" edge case in the design doc; this is
        deferred-not-lost behavior for FIFO entries during sustained
        scrolling.

        Pass 2: drain ONE entry from ``_children_queue`` (the FIFO used by
        ``_dispatch_children``, ``_do_initial_fetch``, recipe-driven
        ``refresh()``/``expand()``). One per outer iteration so a busy
        slot keeps preempting; FIFO drains as soon as the cursor settles.

        Sleep when both are quiet, with the standard clear-then-check-
        then-wait pattern so a wake that arrives between checks isn't
        lost. On wake from ``.wait()``, reset ``_children_prefetch_local_id``
        to ``None`` so a re-request of the same id (cache invalidated in
        place) refetches.

        Delivery routes (per ticket #271/#272), error handling, and
        Pending resolution all live in ``_fetch_and_deliver_children``
        — both passes share the same delivery code.
        """
        while not self._stop:
            did_work = False

            # Pass 1: drain the cursor-prefetch slot until quiescent.
            # Cache short-circuit lives in ``_fetch_and_deliver_children``
            # (#481 Stage 10) — Pass 1 doesn't gate cache itself.
            while not self._stop:
                slot_id = self._children_prefetch_req
                if (slot_id is None
                        or slot_id == self._children_prefetch_local_id):
                    break
                self._children_prefetch_local_id = slot_id
                # When the id is already in pending, an explicit FIFO
                # entry probably carries the work — take it (with its
                # ``reload`` flag) so the cursor's fetch carries any
                # refresh intent rather than silently downgrading to
                # ``reload=False``. If pending but NOT in FIFO, the
                # work is already in flight from a previous slot
                # iteration's helper call; ``local_id == slot_id``
                # will short-circuit on the next read.
                reload_ = False
                if slot_id in self._state._children_pending:
                    # ``list(queue)`` snapshots atomically under the
                    # GIL — main thread (sole appender) cannot mutate
                    # mid-snapshot, so no ``deque mutated during
                    # iteration`` risk. ``queue.remove(target)`` is
                    # itself a single atomic call; main's only FIFO
                    # mutation is ``.append()`` at the right end,
                    # which never shifts ``target``'s position.
                    target = None
                    for q_entry in list(self._children_queue):
                        if q_entry[0] == slot_id:
                            target = q_entry
                            break
                    if target is not None:
                        try:
                            self._children_queue.remove(target)
                            reload_ = target[1]
                        except ValueError:
                            target = None
                    if target is None:
                        # Pending but no FIFO entry — in-flight elsewhere.
                        continue
                else:
                    # Fresh: commit to the fetch by marking pending so
                    # a concurrent ``_dispatch_children`` for the same
                    # id short-circuits.
                    self._state._children_pending.add(slot_id)
                self._fetch_and_deliver_children(slot_id, reload_)
                did_work = True

            # Pass 2: drain ONE FIFO entry (if any). Pass 1's
            # promotion ``remove`` and this ``popleft`` are both on
            # the worker thread (sequential within an iteration —
            # never concurrent). Main thread is the only other
            # writer and only ``.append()``s at the right end,
            # which is atomic and doesn't shift the front.
            if self._children_queue and not self._stop:
                id_, reload_ = self._children_queue.popleft()
                self._fetch_and_deliver_children(id_, reload_)
                did_work = True

            if did_work:
                continue

            # No work this iteration — sleep with the clear-then-check
            # -then-wait pattern. Re-check slot + queue *after* clearing
            # the event so a producer that fires between the prior read
            # and the clear can't be lost.
            self._children_event.clear()
            slot_id = self._children_prefetch_req
            slot_quiet = (slot_id is None
                          or slot_id == self._children_prefetch_local_id)
            if (slot_quiet
                    and not self._children_queue
                    and not self._stop):
                self._children_event.wait()
                # On wake, reset local_id so a re-request of the same id
                # (e.g. cache cleared in place + main thread set event)
                # is treated as fresh work on the next iteration.
                self._children_prefetch_local_id = None

    def _fetch_and_deliver_children(self, id_, reload_):
        """Call ``get_children`` for ``id_`` and route the result.

        Three outcomes per ticket #271/#272:
        * generator → ``_stream_children_from_generator`` drains it (one
          ``update_data`` batch per yield, plus a trailing ``complete``).
        * iterable (incl. ``[]``) → coerce via ``to_item``, build a batch
          via ``_build_children_batch``, post ``update_data`` plus
          ``_post_children_delivery``.
        * ``None`` → no batch posted; ``_loading`` stays True. Still post
          ``_post_children_delivery`` so Pendings resolve and the
          ``_children_pending`` dispatch tracker clears.

        Cache-hit shortcut: when ``reload_=False`` and ``id_`` is already
        in ``state._children``, skip the ``get_children`` call entirely
        and post only the housekeeping. Defends against races where the
        cache populated between FIFO enqueue and worker pop (e.g. another
        path delivered, or the slot worker just won the race for the
        same id). Pendings registered against ``id_`` still resolve via
        ``_post_children_delivery``.

        Errors at the boundary surface via ``self._error_text`` and a
        synthesised empty delivery so the placeholder row clears.

        Worker-thread only — main-thread mutations are routed through
        the post queue (``update_data`` / ``self.post(...)``). Calls
        ``notify_wake()`` after dispatch so the renderer flips to the
        new state on the next loop iteration.
        """
        if not reload_ and id_ in self._state._children:
            # Cache hit: no fetch needed, but the housekeeping still has
            # to run so ``refresh().then()`` chains resolve and
            # ``_children_pending`` clears.
            self.post(lambda pid=id_: self._post_children_delivery(pid))
            notify_wake()
            return
        items = None
        gen = None
        error = False
        try:
            raw = self.get_children(id_, reload=reload_)
        except Exception as e:
            error = True
            # Cross-thread write to a Python str attribute is
            # safe under the GIL; the renderer reads it later
            # on the main thread.
            self._error_text = (
                f'get_children({id_!r}): {type(e).__name__}: {e}'
            )
        else:
            if raw is None:
                items = None
            elif inspect.isgenerator(raw):
                # Generator branch: stream yields as separate
                # ``update_data`` batches. Materialise here only
                # to mark ``items`` so the post-loop dispatch
                # below routes to the streaming path.
                gen = raw
            else:
                try:
                    items = [to_item(x) for x in raw]
                except Exception as e:
                    error = True
                    self._error_text = (
                        f'get_children({id_!r}): '
                        f'{type(e).__name__}: {e}'
                    )

        if error:
            # Synthesise an empty delivery so the placeholder
            # row clears and ``_loading`` flips to False; the
            # error is surfaced via ``_error_text``.
            items = []

        if gen is not None:
            self._stream_children_from_generator(id_, gen)
        elif items is None:
            # ``None`` return: no batch posted, ``_loading``
            # stays True. Still post the housekeeping so
            # Pendings resolve and the ``_children_pending``
            # dispatch tracker clears for this parent.
            self.post(lambda pid=id_: self._post_children_delivery(pid))
        else:
            batch = _build_children_batch(id_, items)
            self.update_data(batch)
            # Schedule housekeeping AFTER ``update_data``'s post
            # so ``apply_ops`` has run by the time Pendings fire
            # — chained ``.then`` callbacks observe the post-
            # batch state (queue.Queue is FIFO).
            self.post(lambda pid=id_: self._post_children_delivery(pid))
        notify_wake()

    def _stream_children_from_generator(self, parent_id, gen):
        """Drain a ``get_children`` generator, posting one batch per yield.

        Per ticket #272 / streaming-push spec Section 3:

        * Each yielded chunk becomes one ``update_data`` batch with NO
          trailing ``complete`` — partial deliveries leave the loading
          flag intact so the UI keeps showing "loading…" between
          chunks.
        * ``isinstance(chunk, list)`` → treat as a batch of items;
          coerce each via ``to_item``.
        * Anything else (``Item``, ``tuple``, ``dict``, ``str`` — all
          ``to_item`` accepts) → single-item batch.
        * On clean ``StopIteration`` → emit a final
          ``[complete(parent_id)]`` batch to clear loading, then post
          the housekeeping callback (resolves Pendings, marks dirty).
        * On a mid-stream exception → record via ``self.error(...)``;
          do NOT emit a trailing ``complete`` (loading stays True so
          the recipe can clear it explicitly via a later push). The
          housekeeping callback still runs so Pendings resolve and
          the dispatch tracker clears.

        Items already delivered before an exception remain in the
        cache — the worker only stops pulling from the generator; it
        does not roll back prior batches.
        """
        # TODO: future pagination — yield-cap of ~500 + sentinel "more…" action
        try:
            for chunk in gen:
                if isinstance(chunk, list):
                    items = [to_item(x) for x in chunk]
                else:
                    items = [to_item(chunk)]
                if not items:
                    # An empty list yield is a no-op: nothing to upsert,
                    # no trailing complete (mid-stream). Skip the post
                    # to avoid an empty drain.
                    continue
                batch = [
                    ('upsert', it.id, parent_id, _fields_of_item(it))
                    for it in items
                ]
                self.update_data(batch)
                notify_wake()
        except Exception as e:
            # Mid-stream exception: per spec, loading stays unless the
            # caller cleared it explicitly. We surface the error via
            # ``self.error(...)`` (routed through ``post``) so the
            # status line reflects the failure but partial deliveries
            # remain in the cache.
            self.error(
                f'get_children({parent_id!r}) [generator]: '
                f'{type(e).__name__}: {e}'
            )
            # Pending chain still fires — the worker has finished its
            # job (it stopped pulling from the generator). Loading is
            # NOT cleared.
            self.post(
                lambda pid=parent_id: self._post_children_delivery(pid)
            )
            return
        # Clean exhaustion: trailing ``complete`` clears loading in
        # the same drain as the post-delivery housekeeping is queued.
        self.update_data([('complete', parent_id)])
        self.post(lambda pid=parent_id: self._post_children_delivery(pid))

    def _post_children_delivery(self, parent_id) -> None:
        """Main-thread housekeeping after a ``_children_worker`` delivery.

        Runs after ``update_data`` (or directly, for the ``None`` return
        path) so the cache is fully populated by the time chained
        ``.then`` callbacks observe state. Three jobs:

        1. Ensure ``_children[parent_id]`` exists as at least an empty
           list — the trailing ``complete`` op alone doesn't create a
           cache entry, and the visible-tree builder distinguishes
           "absent" (placeholder) from "empty list" (no rows). Without
           this, an empty-iterable return would leave a placeholder
           dangling because ``cache_invalidate_subtree`` had dropped
           the entry up front in ``_do_refresh``.
        2. Discard ``_children_pending`` so a future ``refresh`` for
           the same parent dispatches a fresh worker fetch (the
           dispatch tracker is the gate, not the loading flag).
        3. Resolve every Pending registered under ``parent_id`` in
           ``_children_in_flight``. Resolution is in-order; chained
           ``.then`` callbacks see the post-batch cache.

        Also marks the visible tree dirty + flags list/children/all
        for redraw so the next render pass surfaces the freshly-
        delivered children without waiting for the next user keystroke.
        Cursor clamp guards against a watcher-driven shrink leaving
        ``state.cursor`` past the end of the visible list (regression
        for #125, preserved here).
        """
        state = self._state
        # Ensure cache entry exists. The visible-tree builder treats
        # absent entries as "fetch in flight, render placeholder"; an
        # empty list means "really empty, render nothing under this
        # parent". After the worker has finished its job we want the
        # latter unless a recipe explicitly invalidates again.
        state._children.setdefault(parent_id, [])
        state._children_pending.discard(parent_id)
        mark_visible_dirty(state)
        # Clamp cursor when the visible list shrank past it (e.g.
        # watcher-driven refresh that removes items, or empty
        # delivery). Without this, the renderer skips the row (no
        # crash) but the cursor effectively disappears until the user
        # presses j/k. Regression guard from #125.
        vis = visible_items(state)
        if vis and state.cursor >= len(vis):
            state.cursor = len(vis) - 1
        elif not vis:
            state.cursor = 0
        self._needs_redraw.add('list')
        # Cache may have just filled the cursor item's children — flag
        # the grid pane for redraw too. Render-time checks gate the
        # actual paint so this is harmless when the grid is hidden.
        self._needs_redraw.add('children')
        # Layout depends on grid sizing: when the grid was hidden
        # waiting for children to arrive, the preview now needs to
        # shrink to make room. A full repaint is cheaper than tracking
        # that delta by hand.
        self._needs_redraw.add('all')
        # Resolve waiters in the order they were registered.
        for p in self._children_in_flight.pop(parent_id, []):
            p._resolve()

    def _preview_worker(self):
        """Post-queue preview worker with local-id dedup (#442).

        Keeps a ``local_id`` of the most recently fetched request.
        Each iteration:

          1. Read ``_preview_req``. If it equals ``local_id`` (already
             fetched) or ``None`` (nothing asked), clear the wake event
             and re-read (clear-then-read closes the gap with
             ``request_preview`` setting the event during our decision).
             Then ``wait()`` if there's still nothing new. Resets
             ``local_id`` to ``None`` on sleep so a re-request of the
             same id after we've gone idle fires again.
          2. Otherwise adopt the new id, clear the event (arming "next
             request landed during fetch"), and call ``get_preview``.
          3. Generator results route through
             ``_stream_preview_from_generator`` (unchanged) which
             delivers via ``append_preview`` — also on the post queue.
          4. Non-generator results post a ``_deliver_preview`` closure
             that caches the text on ``Item.preview`` and conditionally
             flags redraw. Lambdas capture ``id_`` and ``text`` by
             default-arg to avoid the late-binding pitfall.

        No single-slot ``_preview_result`` lane: every delivery lands
        through the FIFO post queue, so back-to-back fetches all reach
        the cache (the in-flight A delivery is no longer overwritten
        when the user moves to B). Memory effect: cached previews
        accumulate but are bounded by visited items.

        Re-request of the same id while a fetch for it is in flight is
        dropped — the worker finishes the in-flight fetch, sees
        ``_preview_req == local_id`` on the next loop, and sleeps. The
        user sees stale-not-blank for that one cycle; navigating away
        and back forces a refresh. Accepted per #442 design discussion.

        Single-flight invariant preserved: at most one ``get_preview``
        executes at a time (the loop is strictly sequential).
        """
        local_id = None
        while not self._stop:
            req = self._preview_req
            if req is None or req == local_id:
                # Nothing new (or already fetched this id). Sleep.
                # Keep ``local_id`` so we don't immediately re-fetch
                # the same delivery sitting in the post queue.
                self._preview_event.clear()
                # Re-read after clear() to catch a request_preview that
                # fired in the gap between the read above and clear().
                req = self._preview_req
                if req is None or req == local_id:
                    self._preview_event.wait()
                    # On wake, drop ``local_id`` so a re-request of
                    # the same id (e.g. invalidate_preview → request)
                    # is treated as fresh work after the sleep.
                    local_id = None
                # else: a new request raced in; fall through to fetch.
                continue
            # New work — different id (or first iteration after wake).
            local_id = req
            # Arm "next request during fetch": if request_preview fires
            # while get_preview is running, the event will be set and
            # the next iteration's read will see the new id.
            self._preview_event.clear()
            # If a paused generator from an earlier request is still
            # alive (cursor moved between yields), close it so its
            # recipe's ``finally`` runs before we serve the new request.
            self._abandon_paused_preview_if_any(except_id=req)
            try:
                if self.get_preview is not None:
                    result = self.get_preview(req)
                else:
                    result = ''
            except Exception as e:
                err_text = f'[error] {type(e).__name__}: {e}'
                # Capture by value via default-arg to dodge late-binding.
                self.post(
                    lambda id_=req, t=err_text:
                        self._deliver_preview(id_, t)
                )
                notify_wake()
                continue

            if inspect.isgenerator(result):
                # Streaming branch: drain into ``append_preview`` until
                # cap or exhaustion. Returns when the generator is
                # exhausted, errored, abandoned, or left paused.
                # ``append_preview`` already routes through the post
                # queue, so no extra delivery wiring is needed here.
                self._stream_preview_from_generator(req, result)
                # #471: reset the worker's "already fetched" memo after
                # any streaming termination so a follow-up
                # ``request_preview(same_id)`` (typically fired by an
                # ``invalidate_preview`` / ``drop_preview_cache`` kick
                # after the streaming generator was externally
                # abandoned) is treated as a fresh fetch rather than
                # absorbed by the same-id dedup gate.
                local_id = None
            else:
                if result is None:
                    result = ''
                # Capture by value (id_, t) — do NOT close over req/result
                # directly, since the next loop iteration will rebind them.
                self.post(
                    lambda id_=req, t=result:
                        self._deliver_preview(id_, t)
                )
                notify_wake()

    def _kick_after_invalidate(self, id_):
        """Cache for ``id_`` was just nulled — make sure the worker
        actually re-fetches from scratch (#471).

        Without this, a same-id ``request_preview`` is silently absorbed
        by a paused streaming generator that's still holding state for
        the *previous* fetch: the cursor-move check inside the streaming
        worker compares ids and sees no change, so the paused generator
        stays alive, the cache stays null, and the renderer paints
        blank until the user navigates away.

        Fix: when a kick lands for an id whose paused generator is
        live, abandon the generator first so the next
        ``request_preview`` triggers a fresh ``get_preview`` call.

        Cache-clear from the abandon is intentionally suppressed here
        — the caller just nulled the cache; double-clearing risks
        clobbering a fresh delivery that races the abandon teardown.
        """
        with self._preview_lock:
            paused = self._preview_paused
            if paused is not None and paused.get('id') == id_:
                abandoned_gen = paused['gen']
                self._preview_paused = None
                self._preview_resume_pull = False
                self._preview_demand_signal_state = None
            else:
                abandoned_gen = None
        if abandoned_gen is not None:
            try:
                abandoned_gen.close()
            except Exception:
                # ``gen.close()`` swallows generator-internal raises per
                # Python semantics; belt-and-braces against a recipe
                # ``finally:`` that re-raises.
                pass
        self.request_preview(id_)

    def _post_clear_abandoned_preview(self, item_id):
        """Post a main-thread cache-clear for an abandoned streaming preview (#456).

        Used by ``_stream_preview_from_generator`` and
        ``_abandon_paused_preview_if_any`` when a streaming generator is
        dropped before clean ``StopIteration`` — either because the
        cursor moved off ``item_id`` or because ``_stop`` woke the
        worker. The partial buffer in ``Item.preview`` is no longer a
        useful cache entry: a subsequent visit should refetch fresh,
        not paint the truncated text as if it were the final preview.

        Sets ``Item.preview = None`` and ``Item.preview_render = None``
        on the main thread via the post queue (the worker is a daemon
        thread, so direct state mutation would race the renderer).
        Idempotent and safe for unknown ids (silent no-op).

        Cache is *preserved* on:
          * Clean ``StopIteration`` — the buffer is the final preview.
          * Mid-stream exception — partial buffer + ``[error]`` tag is
            an intentionally informative result.
          * Side-effect ops written via ``set_preview_op`` for *other*
            ids (e.g. the umbrella generator populating leaf bodies);
            those ids are not the abandoned generator's id, so they
            stay untouched.
        """
        state = self._state

        def _clear():
            item = state._items_by_id.get(item_id)
            if item is None:
                return
            item.preview = None
            item.preview_render = None
            # Repaint so the cleared pane reflects on screen if the
            # cursor happens to land back on this id before a fresh
            # fetch delivers.
            self._needs_redraw.add('preview')

        self.post(_clear)

    def _abandon_paused_preview_if_any(self, except_id=None):
        """Close any paused preview generator whose id != ``except_id``.

        Called from the preview worker before serving a new request.
        Closing a generator triggers its ``finally`` blocks via
        ``GeneratorExit`` — recipes use this for resource cleanup
        (file handles, network sockets, etc.).

        Posts a cache-clear for the abandoned id (#456) so a later
        visit refetches fresh instead of painting the truncated buffer
        as if it were the final preview.

        Defensive: ``gen.close()`` itself catches generator-internal
        exceptions, but we still wrap to keep the worker thread alive
        on a misbehaving recipe.
        """
        with self._preview_lock:
            paused = self._preview_paused
            if paused is None:
                return
            if except_id is not None and paused['id'] == except_id:
                # Caller is the same id we're paused on — keep the
                # paused generator (resume path will pick it up). Only
                # used by the resume signal codepath in #274.
                return
            abandoned_id = paused['id']
            self._preview_paused = None
            # Clear any stale demand-resume request — that paused
            # generator is going away.
            self._preview_resume_pull = False
            self._preview_demand_signal_state = None
        try:
            paused['gen'].close()
        except Exception:
            # ``gen.close()`` should swallow exceptions raised inside
            # the generator (per Python semantics) but we belt-and-
            # braces in case a recipe re-raises in ``finally``.
            pass
        # #456: discard the partial buffer for the abandoned id so a
        # re-visit refetches instead of painting the truncated cache.
        self._post_clear_abandoned_preview(abandoned_id)

    def _stream_preview_from_generator(self, item_id, gen):
        """Drain a ``get_preview`` generator into ``append_preview``.

        Per ticket #273 / streaming-push spec Section 3:

        * Each yielded chunk is coerced to ``str`` and appended via
          ``append_preview`` (post-queue, race-free read-modify-write).
        * Tracks running buffer size locally (chars + lines). Reading
          back from ``Item.preview`` would race with the main-thread
          drain — the local counter is authoritative for the cap check.
        * When cap is reached → pauses (does NOT close the generator)
          and waits on ``_preview_resume_event``. Wake conditions:
            - cursor-move (``_preview_req`` changed) → close generator,
              pivot to new request.
            - ``_stop`` → exit cleanly.
            - #274 demand signal → resume pulling (TODO; the wait wakes
              but no resume path is implemented this ticket; on wake we
              currently re-loop and only abandon-if-superseded paths
              exit, leaving the paused state intact for #274 to drive).
        * On ``StopIteration`` → done. The buffered content IS the
          preview; do not auto-clear. ``_preview_req`` is cleared so
          the slot frees for the next cursor-move.
        * On any other exception → surface as
          ``[error] ExceptionName: message`` appended to the buffer;
          partial buffered preview is retained.

        The streaming path writes directly through the per-id
        ``Item.preview`` cache via ``append_preview`` (post queue);
        the non-generator branch in ``_preview_worker`` posts a
        ``_deliver_preview`` closure (also post queue) — both routes
        share the same FIFO ordering (#442).
        """
        chars = 0
        lines = 0
        # ``cap_chars`` is the static memory safety net (config-bound,
        # doesn't move with terminal size). ``cap_lines`` is re-derived
        # from the preview pane height via ``_preview_cap_lines()`` at
        # every cap event (#458 / streaming-umbrella spec §3) so a
        # terminal resize takes effect on the next cap window without
        # restarting the generator.
        cap_chars = self._preview_buffer_cap_chars
        # Cumulative caps grow by one window per #274 demand-resume so
        # the local counters can stay running totals (matching the
        # reported ``_preview_paused['chars']`` / ``['lines']``
        # semantics from #273 — the dict records the buffer size at
        # pause time).
        next_cap_chars = cap_chars
        next_cap_lines = self._preview_cap_lines()
        try:
            while True:
                if self._stop:
                    # #456: shutdown — clear the partial buffer so a
                    # stale truncated cache can't survive into a
                    # later run if the main queue drains again.
                    self._post_clear_abandoned_preview(item_id)
                    return
                # Cursor-move abandon check before pulling: if the
                # request slot has moved off our id, drop the
                # generator (its ``finally`` fires via ``gen.close()``)
                # and let the outer worker pick up the new request.
                if self._preview_req != item_id:
                    try:
                        gen.close()
                    except Exception:
                        pass
                    # #456: clear the partial buffer so a re-visit
                    # refetches fresh instead of painting truncated.
                    self._post_clear_abandoned_preview(item_id)
                    return
                try:
                    chunk = next(gen)
                except StopIteration:
                    # Clean exhaustion. Buffered content is final.
                    if self._preview_req == item_id:
                        self._preview_req = None
                    notify_wake()
                    return
                except Exception as e:
                    # Mid-stream raise: surface inline so the user sees
                    # the partial preview followed by the error tag.
                    self.append_preview(
                        item_id,
                        f'\n[error] {type(e).__name__}: {e}',
                    )
                    if self._preview_req == item_id:
                        self._preview_req = None
                    notify_wake()
                    return

                if not isinstance(chunk, str):
                    chunk = str(chunk) if chunk is not None else ''
                if chunk:
                    self.append_preview(item_id, chunk)
                    chars += len(chunk)
                    lines += chunk.count('\n')
                    notify_wake()

                if chars >= next_cap_chars or lines >= next_cap_lines:
                    # #457: drain-without-pause while the user has
                    # pinned the preview to its tail via Shift-End.
                    # The user explicitly asked for the full content;
                    # parking between cap windows adds render-tick
                    # latency that defeats the explicit "give me
                    # everything" command. Advance the cap thresholds
                    # inline and keep pulling. Memory growth while
                    # pinned is intentional — Shift-End is a deliberate
                    # opt-in (see streaming-umbrella spec §2).
                    if self._preview_at_tail:
                        # Re-derive ``cap_lines`` per cap event (#458)
                        # so a resize between windows takes effect even
                        # while we're sailing through under tail-pin.
                        next_cap_chars = chars + cap_chars
                        next_cap_lines = lines + self._preview_cap_lines()
                        continue
                    # Pause. Record the live generator so a cursor-move
                    # can abandon it (closing fires recipe ``finally``)
                    # and so #274 can resume. ``_preview_req`` keeps
                    # pointing at ``item_id`` while paused — the
                    # cursor-move signal IS "``_preview_req`` no longer
                    # equals our id" (either set to a different id, or
                    # cleared to ``None`` when the cursor left any
                    # normal item). ``run_until_idle`` recognises the
                    # paused state explicitly so tests don't time out.
                    with self._preview_lock:
                        self._preview_paused = {
                            'id': item_id,
                            'gen': gen,
                            'chars': chars,
                            'lines': lines,
                        }
                        # New paused window — let the renderer signal
                        # demand again now that the cap window has
                        # advanced.
                        self._preview_demand_signal_state = None
                    notify_wake()
                    # Wait for: cursor-move (``_preview_req`` no longer
                    # equals our id), ``_stop``, or #274 demand signal
                    # (consumer scrolled near the buffered tail).
                    self._preview_resume_event.clear()
                    resumed = False
                    while not self._stop:
                        # Cursor moved? Either a different id landed in
                        # ``_preview_req`` (new request) or it was
                        # cleared to ``None`` (cursor left any normal
                        # item). Either way, abandon — close the
                        # generator so the recipe's ``finally`` fires.
                        if self._preview_req != item_id:
                            with self._preview_lock:
                                still_paused = (
                                    self._preview_paused is not None
                                    and self._preview_paused.get('gen')
                                    is gen
                                )
                                if still_paused:
                                    self._preview_paused = None
                                self._preview_resume_pull = False
                            if still_paused:
                                try:
                                    gen.close()
                                except Exception:
                                    pass
                            # #456: clear the partial buffer so a
                            # re-visit refetches fresh instead of
                            # painting truncated.
                            self._post_clear_abandoned_preview(item_id)
                            return
                        # #274 demand signal — renderer asked us to
                        # keep pulling. Clear paused state + the
                        # resume-pull flag under the lock; reset our
                        # local cap counters so we accumulate a fresh
                        # cap window before re-pausing; break out so
                        # the outer ``while`` resumes ``next(gen)``.
                        with self._preview_lock:
                            if self._preview_resume_pull:
                                self._preview_resume_pull = False
                                if (self._preview_paused is not None
                                        and self._preview_paused.get('gen')
                                        is gen):
                                    self._preview_paused = None
                                self._preview_demand_signal_state = None
                                resumed = True
                        if resumed:
                            # Advance cap thresholds by one window so
                            # the running ``chars``/``lines`` totals
                            # stay cumulative across resume cycles.
                            # Re-derive ``cap_lines`` per resume (#458)
                            # so the next window reflects the current
                            # pane height.
                            next_cap_chars = chars + cap_chars
                            next_cap_lines = lines + self._preview_cap_lines()
                            break
                        # Was the paused state cleared from outside?
                        # (e.g. ``_abandon_paused_preview_if_any``
                        # called by the outer worker on a new request.
                        # That path also called ``gen.close()`` already.)
                        with self._preview_lock:
                            externally_cleared = (
                                self._preview_paused is None
                                or self._preview_paused.get('gen') is not gen
                            )
                        if externally_cleared:
                            return
                        # Block until something wakes us. Short timeout
                        # keeps the loop responsive to ``_stop`` even
                        # when no event landed.
                        if self._preview_resume_event.wait(timeout=0.5):
                            self._preview_resume_event.clear()
                            # Re-check all wake conditions on the next
                            # iteration of this loop:
                            #   * cursor-move (handled at top)
                            #   * demand-resume (handled above)
                            #   * external clear (handled below)
                            #   * ``_stop`` (handled by outer ``while``)
                            continue
                    if resumed:
                        # Outer loop resumes pulling.
                        continue
                    # ``_stop`` woke us — exit without closing (the
                    # daemon thread is going away anyway).
                    # #456: still post the cache-clear so if the main
                    # loop drains once more before tear-down, a stale
                    # partial doesn't survive into a re-run scenario.
                    self._post_clear_abandoned_preview(item_id)
                    return
        finally:
            # Belt-and-braces: if anything escaped (shouldn't), make
            # sure the paused state isn't dangling pointing at this
            # generator. ``gen.close()`` is idempotent.
            with self._preview_lock:
                if (self._preview_paused is not None
                        and self._preview_paused.get('gen') is gen):
                    self._preview_paused = None
                # Whichever way we exited, no future resume applies.
                self._preview_resume_pull = False
                self._preview_demand_signal_state = None

    # ---- internal: preview request slot ---------------------------------

    def request_preview(self, id_: Any) -> None:
        """Set the latest-wins preview request slot.

        Called on the main thread (typically by cursor-move handlers in
        ticket #8). Idempotent for the same id.

        Also wakes any paused preview generator so it can observe the
        new request and abandon (closing the generator, which fires the
        recipe's ``finally``). The cursor-move signal travels through
        the same ``_preview_req`` slot the worker already polls; the
        ``_preview_resume_event`` is purely a wake mechanism for the
        in-pause wait — see ``_preview_worker``.
        """
        self._preview_req = id_
        self._preview_event.set()
        self._preview_resume_event.set()

    def signal_preview_demand(self, item_id: Any) -> None:
        """#274: tell the paused preview worker to resume pulling.

        Called from the renderer (``render_preview`` in 050-render.py)
        when the user scrolls the preview within ``DEMAND_THRESHOLD``
        rows of the buffered tail. No-op when:

          * No preview is paused (already exhausted, never paused, or
            mid-pull).
          * The paused id differs from ``item_id`` (cursor-move race —
            the cursor-move path already handles abandon).

        Otherwise: set ``_preview_resume_pull`` under
        ``_preview_lock`` and wake the worker via
        ``_preview_resume_event``. The worker observes the flag,
        clears the paused state, and breaks back out to its outer
        pull loop. Pulling continues until the next cap, then
        re-pauses; the renderer can re-signal as the buffer grows.

        Idempotent and cheap — safe to call from every preview render.
        Debounce lives at the call site (``render_preview`` tracks the
        last scroll value at which it signalled).
        """
        with self._preview_lock:
            paused = self._preview_paused
            if paused is None or paused.get('id') != item_id:
                return
            self._preview_resume_pull = True
        self._preview_resume_event.set()

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

        # Initial fetch + render. Enqueue the root (plus any
        # pre-existing scope / expanded ids) with ``reload=False`` —
        # startup is not a refresh, so the recipe's per-file caches
        # (e.g. browse-claude's ``_TREE_CACHE`` populated while
        # resolving the initial scope) should survive. The explicit
        # ``self.refresh()`` / Ctrl-R path goes through
        # ``_do_refresh`` which enqueues with ``reload=True``.
        # Wait briefly for the first results before painting so the
        # user doesn't flash a ``loading…`` placeholder for callbacks
        # that resolve in <500ms.
        self.post(self._do_initial_fetch)
        if not self._headless:
            try:
                self.run_until_idle(timeout=0.5)
            except TimeoutError:
                pass  # slow callback; render the loading state
            # Compute the layout once before the first preview fetch so
            # ``_preview_width`` is populated when the worker reads it.
            # Without this, ``get_preview`` runs with ``preview_width
            # == 0`` and recipes fall back to the 80-col default — the
            # first paint then displays a preview wrapped to 80 columns
            # regardless of the real pane width.
            _layout_for(self)
            self._update_preview_for_cursor()
            self._update_children_for_cursor()
            try:
                self.run_until_idle(timeout=0.2)
            except TimeoutError:
                pass
            # Seed the cursor anchor from the initial cursor position
            # so background deliveries that land between now and the
            # first user keypress preserve the cursor's identity (not
            # just its index). Skip when the recipe has already set an
            # explicit anchor via ``cursor_to`` before ``run()`` — the
            # recipe is chasing a still-loading row and a snapshot of
            # the default cursor position (usually row 0 = the scope
            # row at depth 0) would clobber it, leaving the cursor
            # stranded once the row actually arrives.
            if not self._cursor_anchor:
                self._reanchor_cursor()
            render_full(self)

        # Plugin hook: ``on_before_run`` fires after construction is
        # complete and the workers/render are wired, just before the
        # event loop starts. Plugins use this for last-minute key
        # binding overrides or kicking off background tasks. Hooks
        # run in registration order; exceptions propagate.
        for _plugin_cfg in registered_plugins:
            if _plugin_cfg.on_before_run is not None:
                _plugin_cfg.on_before_run(self)

        try:
            while not self._quit_requested:
                # Drain pending updates from any thread, then render if
                # something is dirty. Pre-key drain so a worker result
                # that landed before this iteration is visible by the
                # next read_key wake.
                self.drain_main_queue()
                self.apply_children_results()

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

                # Post-drain lifecycle-hook settle pass. NOTE: this is
                # only PART of the hook firing — ``on_scope_change`` and
                # ``on_selection_change`` already fired synchronously at
                # their mutation sites during the ``drain_main_queue``
                # above (``scope_into`` / ``scope_out`` and the selection
                # ops call ``_fire_*`` inline). The hooks below are the
                # ones that diff / collect state across the whole drain and
                # so must fire after it settles, each at most once per tick.
                #
                # The single ordering guarantee recipes may rely on is that
                # ``on_cursor_change`` fires LAST, after expansion has
                # settled, so a cursor-change handler sees the post-
                # expansion tree with freshly-delivered children accounted
                # for. The order of the rest is an implementation detail
                # (not a contract): geometry (on_resize) and input state
                # (on_search_change → on_filter_change) first since they may
                # reshape the visible tree, then structure (on_collapse →
                # on_expand), then children-arrival (on_children_loaded),
                # then the cursor. (on_resize consumes the ``_resize_pending``
                # flag latched at the SIGWINCH observation points below, so
                # a resize observed this iteration fires on the next drain.)
                self._fire_resize_if_pending()
                self._fire_search_change_if_pending()
                self._fire_filter_change_if_pending()
                self._fire_expand_collapse_if_pending()
                self._fire_children_loaded_if_pending()
                self._fire_cursor_change_if_pending()

                # Resize flag — set by SIGWINCH handler in 020-terminal.
                # Bare-name access works in the concatenated build; in
                # tests this attribute is injected onto the module.
                if globals().get('g_resize_flag', False):
                    globals()['g_resize_flag'] = False
                    self._needs_redraw.add('all')
                    # Wrap cache (#422): preview wrap width changed.
                    # Walk loaded items and drop every ``preview_render``
                    # so the next paint regenerates at the new width.
                    self._invalidate_all_preview_renders()
                    # Latch for the ``on_resize`` hook — the next drain's
                    # ``_fire_resize_if_pending`` reads ``term_size`` and
                    # fires if the dimensions actually changed.
                    self._resize_pending = True
                # Screen-lost flag — set by SIGCONT / term_resume after the
                # alt-screen content was destroyed externally. Drop the
                # per-pane row caches so ``end_row`` doesn't cache-hit
                # against the now-stale (and invisible) pre-suspend state.
                if globals().get('g_screen_lost_flag', False):
                    globals()['g_screen_lost_flag'] = False
                    self._pane_cache.clear()
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
                    self._invalidate_all_preview_renders()
                    self._resize_pending = True
                if globals().get('g_screen_lost_flag', False):
                    globals()['g_screen_lost_flag'] = False
                    self._pane_cache.clear()
                    self._needs_redraw.add('all')

                if key == '_notify':
                    # Worker delivered something; loop and drain.
                    continue

                # Dispatch the key. Then coalesce any keystrokes already
                # buffered on stdin into the same render cycle — a held-
                # down arrow or a paste burst dispatches all queued keys
                # back-to-back and the outer loop renders once at the
                # end. Bounded by ``_INPUT_BURST_MAX_KEYS`` and
                # ``_INPUT_BURST_MAX_SECONDS`` so an endless input
                # stream still yields intermediate frames.
                self._handle_one_key(ctx, key)
                burst_count = 1
                burst_deadline = time.monotonic() + _INPUT_BURST_MAX_SECONDS
                while (not self._quit_requested
                        and burst_count < _INPUT_BURST_MAX_KEYS
                        and time.monotonic() < burst_deadline
                        and input_ready()):
                    try:
                        key = read_key()
                    except KeyboardInterrupt:
                        key = 'ctrl-c'
                    # Worker delivery during the burst: let the outer
                    # loop drain + render so the delivered state lands
                    # on the next paint. (Signals like SIGWINCH set
                    # globals checked at the top of the outer loop, so
                    # they too get picked up there.)
                    if key == '_notify':
                        break
                    self._handle_one_key(ctx, key)
                    burst_count += 1
        finally:
            if not self._headless:
                term_restore()
            self.stop_workers()
            # Lifecycle hook: fired after screen restore, before
            # ``run`` returns. Exceptions swallowed (see
            # ``_fire_on_quit``).
            self._fire_on_quit()
            # Plugin hook: ``on_after_run`` fires inside the finally
            # so cleanup runs even when the event loop exits via
            # exception. Hooks run in registration order; exceptions
            # propagate (and may replace an in-flight exception, per
            # Python's finally semantics).
            for _plugin_cfg in registered_plugins:
                if _plugin_cfg.on_after_run is not None:
                    _plugin_cfg.on_after_run(self)

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
        ``pending`` placeholder (no real id to ask about). The scope
        row at depth 0 IS previewable — recipes commonly attach rich
        content to the scope id (browse-claude session card,
        browse-plan ticket body, …) and the user's first-glance row
        when launching with an initial scope is the scope row. Called
        by the main loop at the top of every iteration (post-#124) so
        cursor moves and worker deliveries both trigger preview fetches.

        Three behaviors (#442):

          * Same cursor + cached: no-op. The renderer paints from cache.
          * Same cursor + cache cleared (``item.preview is None``):
            re-fire ``request_preview`` so any mutation that dropped
            the cache (e.g. ``invalidate_preview``, a recipe call to
            ``clear_preview``, a children-mutation handler clearing an
            umbrella body) recovers on the next main-loop iteration.
            This fixes the "stuck blank" failure mode: previously, if
            the cursor stayed put while something nulled the cache, the
            renderer would read ``None`` and never trigger a re-fetch.
          * Cursor moved + new id already cached: skip the fetch; just
            reset scroll/help and flag redraw. The renderer paints from
            cache. Saves a redundant ``get_preview`` round-trip for
            navigation across already-visited rows.
          * Cursor moved + new id not cached (or ``None``): request a
            fetch and reset scroll/help.

        Idempotency note: the gate is ``_preview_cursor_id`` (only
        updated when the cursor moves to a different item), NOT
        ``_preview_req`` (cleared inside the worker as it processes
        deliveries). A ``_preview_req`` gate would cause a hot fetch
        loop now that this runs every iteration — see ticket #126.
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
            # Cursor still on the same item. Normally a no-op; but if
            # the cached preview has been cleared (None) we need to
            # re-fire request_preview — otherwise the renderer would
            # paint blank until the user navigates away and back.
            # See #442 "stuck blank" repro.
            if new_id is not None:
                item = state._items_by_id.get(new_id)
                if item is not None and item.preview is None:
                    self.request_preview(new_id)
            return

        self._preview_cursor_id = new_id
        self._preview_scroll = 0
        # ``_preview_at_tail`` is intentionally NOT cleared here: the
        # tail pin is a sticky user intent that carries across cursor-
        # item changes (and over recipe-driven cache invalidation, and
        # over help-toggle). The renderer's pin override re-derives
        # ``_preview_scroll = max_scroll`` on every pass, so the
        # scroll=0 reset above is meaningful only when the pin is
        # disengaged. Cleared only by explicit upward motion in the
        # action layer.
        if self._help_mode:
            self._help_mode = False
        self._needs_redraw.add('preview')

        if new_id is None:
            self._preview_req = None
            # Wake any paused preview generator so it observes the
            # cursor-off-item state and abandons. Without this, a
            # paused generator from the previously-cursored item would
            # stay alive (and its recipe ``finally`` wouldn't fire)
            # until the cursor lands on a new normal item.
            self._preview_resume_event.set()
            return

        # Cursor landed on a new id with a cached preview — paint from
        # cache, skip the round-trip. The renderer's clamp keeps
        # ``_preview_scroll`` valid even though we reset it to 0 above.
        item = state._items_by_id.get(new_id)
        if item is not None and item.preview is not None:
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

    def _preview_pane_height_safe(self) -> int:
        """Return the preview pane's content height, or 0 if unknown.

        Mirrors ``_preview_pane_height`` in 070-actions.py but lives
        here so the streaming worker (state layer) can read it without
        crossing into the action layer. ``layout_panes`` reports the
        preview rect including the separator row; subtract 1 to get the
        scrollable content height. Returns 0 in headless / no-tty
        contexts so callers can fall back to a floor.
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
            prev_rect = layout.get('preview')
            if prev_rect is None:
                return 0
            h = prev_rect.height - 1  # exclude separator row
            return h if h > 0 else 0
        except Exception:
            return 0

    def _preview_cap_lines(self) -> int:
        """Streaming preview line cap derived from the preview pane.

        Re-read once per pause cycle by
        ``_stream_preview_from_generator`` so a terminal resize takes
        effect on the next cap window without restarting the
        generator. Returns
        ``max(preview_pane_height * STREAM_CAP_FACTOR, MIN_CAP_LINES)``
        (streaming-umbrella spec §3 / ticket #458).

        The companion char cap (``_preview_buffer_cap_chars``) stays
        static — it's the memory safety net, not the screen-fit
        sizing.
        """
        height = self._preview_pane_height_safe()
        return max(height * STREAM_CAP_FACTOR, MIN_CAP_LINES)

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

    def _compute_anchor_snapshot(self) -> list:
        """Return a fresh anchor snapshot for the current cursor position.

        Layout: ``[primary, next, prev, parent, grandparent, ..., root]``
        where ``primary`` is the cursor item's id. Each successive entry
        is a fallback the cursor falls onto if its predecessor is missing
        from the visible list.

        Snapshots all VisibleEntry kinds, not just non-``pending``: the
        scope row at depth 0 (when scoped) is now itself a normal row
        carrying the real scoped-item id (anchoring there is fine — if
        the user scopes out, the same id reappears at depth 1 under
        the new top), and ``pending`` placeholders carry a deterministic
        ``__pending_<parent>`` id that's stable while the parent is
        loading. Treating synthetic rows the same as normal rows keeps
        cursor navigation continuous when the user is parked on the
        scope row or stepping through a loading subtree.

        Returns ``[]`` only when there's no row to anchor at all (insert
        mode, cursor out of range). Callers treat an empty list as
        "leave the existing anchor alone".
        """
        if self._insert_mode:
            return []
        vis = visible_items(self._state)
        cur = self._state.cursor
        if not (0 <= cur < len(vis)):
            return []
        primary = vis[cur].item.id
        out = [primary]
        # Nearest visible neighbour after the cursor.
        if cur + 1 < len(vis):
            out.append(vis[cur + 1].item.id)
        # Nearest visible neighbour before the cursor.
        if cur - 1 >= 0:
            out.append(vis[cur - 1].item.id)
        # Ancestors closest-first to the tree root. ``walked`` is the
        # cycle-detection set (a malformed parent index would otherwise
        # spin forever); ``seen`` is the dedup set (don't repeat an id
        # already in ``next`` / ``prev``, but still keep climbing past
        # it so far ancestors are recorded).
        cursor_id = primary
        walked = {primary}
        seen = set(out)
        while True:
            parent_id = self._state._parent_of_id.get(cursor_id)
            if parent_id is None or parent_id in walked:
                break
            walked.add(parent_id)
            if parent_id not in seen:
                out.append(parent_id)
                seen.add(parent_id)
            cursor_id = parent_id
        return out

    def _reanchor_cursor(self) -> None:
        """Capture a fresh anchor snapshot from the current cursor.

        Called after every user-driven cursor move (in
        ``_handle_one_key``) and after every successful ``primary``
        match in ``_apply_cursor_anchor`` (the neighbourhood may have
        shifted while we were chasing a missing primary). No-op when
        the snapshot would be empty so a transient out-of-range or
        synthetic-row state doesn't discard a still-valid anchor.

        Positional pin survives this call iff the cursor is still at
        the pinned row (row 0 for ``PIN_FIRST``, last row for
        ``PIN_LAST``). Any other cursor position drops the pin and
        captures a fresh id-based snapshot. See
        ``docs/superpowers/specs/2026-05-17-cursor-pin-design.md``.
        """
        cur = self._cursor_anchor
        if cur and isinstance(cur[0], _AnchorSentinel):
            pin = cur[0]
            vis = visible_items(self._state)
            if vis:
                target = 0 if pin is PIN_FIRST else len(vis) - 1
                if self._state.cursor == target:
                    return
            else:
                # Empty list — keep the pin parked.
                return
            # Cursor moved off the pinned row → fall through to
            # id-based snapshot capture.
        snap = self._compute_anchor_snapshot()
        if snap:
            self._cursor_anchor = snap

    def _apply_hide_displacement(self, pre_vis_ids, pre_cursor) -> bool:
        """Displace the cursor when its row was hidden (not deleted).

        Called from ``Browser.update_data._apply`` after ``apply_ops``
        has run, using a snapshot of the visible list captured before
        the mutation. Returns True iff displacement happened.

        The rule (per
        ``docs/superpowers/specs/2026-05-16-row-visibility-design.md``):

          * If the cursor's previous row id is still in visible_items
            after the mutation, do nothing.
          * Else, if the id was *deleted* (not in ``_items_by_id``),
            do nothing — the cursor anchor's fallback chain handles
            structural displacement.
          * Else (id still exists but became hidden): walk back
            through the pre-mutation visible list from ``pre_cursor``,
            find the first id still visible after the mutation, and
            move the cursor there. If no such id exists, land on
            row 0 of the new visible list (or stay at 0 if it's
            empty). Re-anchor from the new cursor.

        This is intentionally separate from the anchor's chain
        because filtering implies the user (or recipe) deliberately
        changed what's visible — cursor movement is *expected*.
        """
        if not pre_vis_ids:
            return False
        if pre_cursor < 0 or pre_cursor >= len(pre_vis_ids):
            return False
        pre_cursor_id = pre_vis_ids[pre_cursor]
        post_vis = visible_items(self._state)
        post_id_to_idx = {
            entry.item.id: i for i, entry in enumerate(post_vis)
        }
        if pre_cursor_id in post_id_to_idx:
            return False
        if pre_cursor_id not in self._state._items_by_id:
            # Row was deleted (not hidden) — leave it to the anchor.
            return False
        # Walk back through the pre-mutation visible list looking for
        # a still-visible row.
        new_idx = None
        for i in range(pre_cursor - 1, -1, -1):
            if pre_vis_ids[i] in post_id_to_idx:
                new_idx = post_id_to_idx[pre_vis_ids[i]]
                break
        if new_idx is None:
            # No earlier row survived — land on the new first row.
            new_idx = 0
        if not post_vis:
            self._state.cursor = 0
        elif self._state.cursor != new_idx:
            self._state.cursor = new_idx
            mark_cursor_changed(self)
            self._snap_list_scroll_to_row(new_idx)
        self._reanchor_cursor()
        return True

    def _apply_cursor_anchor(self) -> bool:
        """Snap ``state.cursor`` onto the anchored id (or its closest
        fallback) inside the current visible list.

        Walks ``_cursor_anchor`` tier-by-tier; the first id present in
        the visible list wins. On a ``primary`` hit (tier 0) the
        snapshot is refreshed — the cursor is back where the user last
        positioned it, so the neighbourhood is now current. On a
        fallback hit (tier ≥ 1) the snapshot is left parked so the
        cursor can return to ``primary`` when it reappears in a later
        delivery.

        Returns True iff a tier matched (cursor may or may not have
        moved — same row idx counts as a match). Used by every
        background mutation hook (``apply_children_results``,
        ``update_data._apply``, ``_do_expand``) plus
        ``_handle_one_key`` for handlers that change list structure
        without writing ``state.cursor``.
        """
        if not self._cursor_anchor:
            return False
        vis = visible_items(self._state)
        # Positional pin (``PIN_FIRST`` / ``PIN_LAST``) — short-circuit
        # before the id-based walk. Empty list is a no-op; the pin
        # stays parked until rows arrive.
        first = self._cursor_anchor[0]
        if isinstance(first, _AnchorSentinel):
            if not vis:
                return False
            new_i = 0 if first is PIN_FIRST else len(vis) - 1
            if self._state.cursor != new_i:
                self._state.cursor = new_i
                mark_cursor_changed(self)
                self._snap_list_scroll_to_row(new_i)
            return True
        id_to_idx = {}
        for i, entry in enumerate(vis):
            # Index every kind (normal / pending). Each row has a
            # stable item id; the renderer skips preview-fetches for
            # ``pending`` kinds itself, so landing the cursor on a
            # pending row is harmless.
            id_to_idx.setdefault(entry.item.id, i)
        for tier_idx, target_id in enumerate(self._cursor_anchor):
            if target_id in id_to_idx:
                new_i = id_to_idx[target_id]
                if self._state.cursor != new_i:
                    self._state.cursor = new_i
                    mark_cursor_changed(self)
                    self._snap_list_scroll_to_row(new_i)
                if tier_idx == 0:
                    snap = self._compute_anchor_snapshot()
                    if snap:
                        self._cursor_anchor = snap
                return True
        return False

    def _apply_expand_goal(self) -> None:
        """Adjust ``_list_scroll`` to fit the parked expansion goal.

        Geometry (with ``height = list pane rows``):

          * ``p_idx`` is the parent row's index in the visible list.
          * ``last_idx`` is the last row of the parent's subtree (the
            deepest descendant visible right now, possibly a ``pending``
            placeholder).
          * Acceptable scroll values satisfy "parent visible" AND
            "last row visible": ``[last_idx - height + 1, p_idx]``.
          * When the range is non-empty (subtree fits in the pane
            below the parent), pick the value closest to the current
            scroll — moves bidirectionally with the minimum amount.
          * When the range is empty (subtree larger than the pane
            minus the parent), park the parent at the top and clear
            the goal (scroll-cap reached).

        The goal also clears when every row in the subtree is
        materialised (no ``pending`` placeholders). User cursor moves
        and wheel scrolls clear it from their own dispatch sites.
        Insert mode short-circuits — the active-row concept is the
        insert marker, not the visible cursor, and the geometry above
        doesn't apply.
        """
        goal = self._expand_goal
        if goal is None or self._insert_mode:
            return
        parent_id = goal['parent_id']

        # User collapsed the parent or it disappeared from the tree —
        # the goal is meaningless either way.
        if parent_id not in self._state.expanded:
            self._expand_goal = None
            return

        vis = visible_items(self._state)
        p_idx = None
        p_depth = 0
        for i, entry in enumerate(vis):
            if entry.item.id == parent_id:
                p_idx = i
                p_depth = entry.depth
                break
        if p_idx is None:
            self._expand_goal = None
            return

        # Subtree extent: contiguous run of deeper rows after the parent.
        # ``has_pending`` tells us whether the subtree is still loading.
        last_idx = p_idx
        has_pending = False
        for i in range(p_idx + 1, len(vis)):
            if vis[i].depth <= p_depth:
                break
            last_idx = i
            if vis[i].kind == 'pending':
                has_pending = True

        height = self._list_pane_height_safe()
        if height <= 0:
            return

        lo = last_idx - height + 1
        hi = p_idx
        fits = hi >= lo
        if fits:
            # Minimal bidirectional move: nudge into the acceptable
            # range only as far as needed.
            desired = max(lo, min(hi, self._list_scroll))
        else:
            # Subtree too big for the available area; show parent at
            # top and stop the goal afterwards.
            desired = hi
        if desired < 0:
            desired = 0

        if desired != self._list_scroll:
            self._list_scroll = desired
            self._needs_redraw.add('list')

        # Stop conditions: fully loaded, or scroll-cap hit.
        if not has_pending:
            self._expand_goal = None
            return
        if not fits:
            self._expand_goal = None
            return

    def _handle_one_key(self, ctx, key) -> None:
        """Dispatch one keystroke and run the per-key bookkeeping.

        Snapshots the on-screen cursor row before dispatch so the
        viewport snaps back to follow the cursor when the key actually
        moved it (wheel-scroll handlers leave the cursor alone, so the
        comparison is False and ``_list_scroll`` keeps the user's
        scrolled position). After dispatch, kicks the idempotent
        preview / children fetches for the new cursor.

        Cursor-anchor flow: the pre-/post-dispatch ``state.cursor``
        comparison tells us whether the handler intentionally moved
        the cursor. If not, the handler may still have mutated the
        visible-list shape (e.g., ``_expand_recursive`` flips
        ``state.expanded`` without touching ``state.cursor``), which
        would leave the cursor pointing at the wrong item by index —
        ``_apply_cursor_anchor`` re-snaps it onto the anchored id.
        Either way, we then re-snapshot for the next background
        mutation.

        Used by the main loop's burst-coalescing loop: dispatch the
        first key, then keep calling this for each additional key
        already buffered on stdin before the outer loop renders. The
        per-key worker fetches stay live during the burst because
        they're keyed by cursor id (not invocation count) and gated
        cheaply.
        """
        prev_row = self._active_list_row()
        prev_insert = self._insert_mode
        if self._insert_mode:
            _handle_insert_key(self, ctx, key)
        else:
            pre_cursor = self._state.cursor
            dispatch_key(self, ctx, key)
            if self._state.cursor == pre_cursor:
                # Handler didn't touch state.cursor; it may have
                # mutated visible-list shape. Re-snap onto the anchor
                # so the cursor stays on its item rather than drifting
                # with shifted indices.
                self._apply_cursor_anchor()
            else:
                # User moved the cursor — abandon any parked
                # expand-to-fit goal so the next async delivery
                # doesn't snap the viewport back onto the
                # now-no-longer-interesting subtree.
                self._expand_goal = None
        new_row = self._active_list_row()
        if prev_insert != self._insert_mode or new_row != prev_row:
            self._snap_list_scroll_to_row(new_row)

        # Re-anchor for the next background mutation. Skipped in
        # insert mode and on synthetic rows by the helper itself.
        self._reanchor_cursor()

        # Trigger preview + children fetches for the (possibly moved)
        # cursor before the next render pass. Idempotent; safe to call
        # once per key during a burst.
        self._update_preview_for_cursor()
        self._update_children_for_cursor()

    def _update_children_for_cursor(self) -> None:
        """Request a children prefetch for the cursor item (#481).

        Writes the cursor's id into the latest-wins slot
        ``_children_prefetch_req``; the children worker reads in Pass 1
        of its loop and decides whether to fetch. Rapid cursor movement
        coalesces — only the latest cursor stop's id remains in the
        slot, intermediate positions are superseded.

        The slot always reflects the cursor's intent regardless of
        whether the id is also queued in the FIFO: the worker owns
        the FIFO-promotion path (Pass 1 scans + removes a matching
        FIFO entry and copies its ``reload`` flag). Keeping promotion
        in the worker avoids a main-thread/worker race on multi-step
        deque mutations and concentrates the lookup-and-fetch decision
        in one place.

        Early-returns:
          * pane disabled,
          * cursor not on a normal item,
          * cursor item not expandable (``has_children=False``),
          * already cached (``state._children[id]`` present).

        Idempotent re-fire: when the slot already holds the cursor's
        id, wake the worker and reset ``_children_prefetch_local_id``
        so a re-request after the worker parked refetches. Mirrors
        the preview "stuck blank" re-fire pattern at
        ``_update_preview_for_cursor`` — a cache-clear-in-place
        (e.g. ``clear_children`` while the cursor stays put) doesn't
        leave the children pane blank until the user navigates away.
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
        new_id = item.id
        if new_id in state._children:
            return  # already cached
        if self._children_prefetch_req == new_id:
            # Slot already has this id — re-fire the wake so the worker
            # re-evaluates (cache may have been cleared in place while
            # the cursor stayed put). Reset local_id so the next loop
            # iteration treats it as fresh work.
            self._children_prefetch_local_id = None
            self._children_event.set()
            return
        self._children_prefetch_req = new_id
        self._children_event.set()

    # ---- eager adapter -------------------------------------------------

    @classmethod
    def from_flat_tree(cls, rows, *, root_id: Any = None,
                       path_sep: Optional[str] = None,
                       **browser_kwargs) -> 'Browser':
        """Build a Browser whose ``_children`` cache is pre-populated from ``rows``.

        Each row may be ``Item``, ``str``, ``tuple``, or ``dict`` (per
        ``to_item`` rules). Hierarchy detection looks at the coerced
        Items, most-explicit-wins:

        * **parent-pointer mode** -- if any row has a ``parent`` field
          (or attribute) other than ``None``, every row is grouped under
          its parent's id; rows with no parent (or ``parent is None``)
          go under ``root_id``.
        * **depth-coded mode** -- otherwise, if any row has a ``depth``
          field, walk rows in iteration order maintaining a stack of
          ``(depth, item)``: a row at depth ``d+1`` is a child of the
          most recent row at depth ``d``; depth-0 rows go under
          ``root_id``.
        * **path-split mode** -- otherwise, if ``path_sep`` is set, each
          row's ``id`` is split on it (via ``expand_path_rows``) into a
          tree of prefix nodes; the resulting rows are then grouped by
          the synthesised ``parent`` exactly as in parent-pointer mode.
        * **flat mode** -- if no hint is present, all rows become direct
          children of ``root_id``.

        The synthesised ``get_children`` reads from the pre-populated
        cache, so no user callback runs at runtime. Recipes wanting
        true laziness should pass their own ``get_children`` instead.
        """
        rows = list(rows)  # materialise: path mode re-iterates raw rows
        items = [to_item(r) for r in rows]
        has_parent = any(
            getattr(it, 'parent', None) is not None for it in items
        )
        has_depth = (
            not has_parent
            and any(getattr(it, 'depth', None) is not None for it in items)
        )
        # Path-split is a derivation rule, less explicit than a per-row
        # parent/depth column; it only runs when neither is present.
        use_path = not has_parent and not has_depth and path_sep is not None
        if use_path:
            items = [to_item(r) for r in expand_path_rows(rows, path_sep)]

        children_by_parent: dict = {}

        if has_parent or use_path:
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

        if use_path:
            # In path-split mode ``has_children`` is derived from the
            # synthesised tree, ignoring any incoming column: a node is
            # expandable iff it appears as a parent of some other node.
            # Assign unconditionally so a carried ``True`` on a leaf is
            # overridden to ``False`` (the tree is fully known and eager,
            # so a leaf has nothing to lazily expand into).
            for it in items:
                it.has_children = it.id in children_by_parent

        # Synthesise the get_children callback to read from the cache.
        # Captures children_by_parent rather than self._state._children
        # so a later cache_invalidate_all() doesn't strand the recipe
        # (we still want eager reads to win).
        def _get_children_eager(pid, *, reload=False):
            return children_by_parent.get(pid, [])

        browser_kwargs.setdefault('get_children', _get_children_eager)
        browser_kwargs.setdefault('root_id', root_id)
        b = cls(BrowserConfig(**browser_kwargs))
        # Pre-populate the cache via a single ``update_data`` batch so
        # visible_items() and apply_*_results see it immediately; no
        # fetch happens at runtime unless the caller invokes ``refresh``
        # explicitly. The batch is one ``upsert`` per row followed by
        # one ``complete`` per distinct parent (which flips
        # ``_loading[parent_id]`` to False) — same vocabulary the
        # children-worker delivery uses, so the eager-built tree
        # behaves identically to a worker-delivered one and the
        # auxiliary indexes (``_items_by_id`` / ``_parent_of_id``)
        # land via the shared op machinery.
        #
        # Constructor-time pre-population is single-threaded (no other
        # thread is touching state yet), so we apply the batch
        # synchronously via ``apply_ops`` instead of routing through
        # ``update_data`` + a post-queue drain.
        batch = []
        for parent_id, kids in children_by_parent.items():
            for it in kids:
                batch.append(upsert(it.id, parent_id, **_fields_of_item(it)))
        for parent_id in children_by_parent:
            batch.append(complete(parent_id))
        if batch:
            apply_ops(b._state, batch)
        return b
