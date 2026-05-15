"""browse-tui: state layer (visible tree, cursor/scope, async workers, post queue, Pending)."""

import inspect
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field, fields as _dc_fields
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

def upsert(id, parent_id, **fields):
    """Construct an ``("upsert", id, parent_id, fields)`` op tuple."""
    return ('upsert', id, parent_id, fields)


def set_item(id, parent_id, **fields):
    """Construct a ``("set", id, parent_id, fields)`` op tuple."""
    return ('set', id, parent_id, fields)


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


def _apply_upsert(state: State, id_, parent_id, fields) -> bool:
    """Apply one ``upsert`` op. Returns True if structure changed."""
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
        state._children.setdefault(parent_id, []).append(item)
        state._items_by_id[id_] = item
        state._parent_of_id[id_] = parent_id
        return True

    # Existing id. Patch-merge: matching keys override Item fields,
    # unmatched keys land as custom attrs. Mutate in place so other
    # references (visible cache, selection set) keep working.
    for k, v in fields.items():
        setattr(existing, k, v)

    if parent_id is None:
        # Patch-only: leave parent unchanged. Mutating fields like
        # ``has_children`` / ``title`` affects what the visible-tree
        # builder emits, so this is structural too.
        return True

    old_parent = state._parent_of_id.get(id_)
    if old_parent != parent_id:
        # Reparent: remove from old parent's child list (if cached),
        # append to the new parent's, update the reverse index.
        old_list = state._children.get(old_parent)
        if old_list:
            for i, child in enumerate(old_list):
                if child.id == id_:
                    del old_list[i]
                    break
        state._children.setdefault(parent_id, []).append(existing)
        state._parent_of_id[id_] = parent_id
    return True


def _apply_set(state: State, id_, parent_id, fields) -> bool:
    """Apply one ``set`` op. Returns True if structure changed."""
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

    old_parent = state._parent_of_id.get(id_)
    if old_parent is None and id_ not in state._items_by_id:
        # Insert under the supplied parent_id.
        state._children.setdefault(parent_id, []).append(item)
        state._items_by_id[id_] = item
        state._parent_of_id[id_] = parent_id
        return True

    # Replace existing. The id keeps its place in the parent's child
    # list when the parent is unchanged; on reparent it moves to the
    # new parent's tail.
    if old_parent == parent_id:
        old_list = state._children.get(old_parent)
        if old_list is not None:
            for i, child in enumerate(old_list):
                if child.id == id_:
                    old_list[i] = item
                    break
            else:
                old_list.append(item)
        else:
            state._children[parent_id] = [item]
    else:
        old_list = state._children.get(old_parent)
        if old_list:
            for i, child in enumerate(old_list):
                if child.id == id_:
                    del old_list[i]
                    break
        state._children.setdefault(parent_id, []).append(item)
        state._parent_of_id[id_] = parent_id

    state._items_by_id[id_] = item
    # ``_children[id_]`` (the children OF this id, as a parent) is
    # preserved — they belong to the id, not to the Item instance.
    return True


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
    # flip it back to True via dispatch).
    state._loading[parent_id] = False
    return had_entry or bool(children)


def apply_ops(state: State, ops) -> None:
    """Apply a list of ``update_data`` ops to ``state`` in order.

    Pure state mutation — no threading, no rendering. Each op is a
    tagged tuple; see Section 2 of the streaming-push design doc for
    the vocabulary. Ops apply in list order; reparenting in one op is
    visible to subsequent ops in the same batch. Flips
    ``_visible_dirty`` if any op affected the visible structure
    (anything that touched ``_children``).

    Unknown ops raise ``ValueError`` — silent drops would mask recipe
    typos.
    """
    structural = False
    for op in ops:
        kind = op[0]
        if kind == 'upsert':
            _, id_, parent_id, fields = op
            if _apply_upsert(state, id_, parent_id, fields):
                structural = True
        elif kind == 'set':
            _, id_, parent_id, fields = op
            if _apply_set(state, id_, parent_id, fields):
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
            state._loading[parent_id] = False
        elif kind == 'incomplete':
            _, parent_id = op
            state._loading[parent_id] = True
        else:
            raise ValueError(f'apply_ops: unknown op kind {kind!r}')
    if structural:
        state._visible_dirty = True


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
    one function instead of recopying a hand-written triplet.
    """
    rd = browser._needs_redraw
    rd.add('list')
    rd.add('children')
    rd.add('preview')


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
        mark_cursor_changed(browser)


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
                 preview_ansi: bool = True,
                 list_ratio: float = 0.30,
                 split: str = 'auto',
                 multi_select: bool = True,
                 print_format: str = '{id}',
                 help_intro: Optional[str] = None,
                 help_outro: Optional[str] = None,
                 show_ids: str = 'auto',
                 preview_buffer_cap_chars: int = 100_000,
                 preview_buffer_cap_lines: int = 1000,
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
        # Honour ANSI SGR escapes in the preview pane (default True).
        # Toggled at runtime via capital-R; see ``_toggle_preview_ansi``
        # in 070-actions.py. The cache invalidation is naturally handled
        # by the per-row byte-stream comparison in the differential
        # renderer — colour-bearing rows produce different bytes when
        # SGR re-emit is suppressed, so they redraw; plain rows produce
        # identical bytes and stay cache-hit (#240 design note).
        self.preview_ansi = preview_ansi
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
        # from the left, fetches, and posts the result on the main
        # thread via ``update_data`` + a follow-up housekeeping
        # callable (#271). Deque ops are individually atomic under the
        # GIL — safe for single-producer / single-consumer use.
        #
        # ``_children_results`` is the legacy delivery deque used only
        # by ``set_children`` / ``apply_children_results`` (the public
        # thread-safe injection path for recipes that bypass the
        # worker). The worker itself no longer touches it.
        self._children_queue = deque()
        self._children_in_flight = {}     # id -> list[Pending] awaiting this fetch
        self._children_results = deque()  # FIFO of (id, items) — set_children only
        self._children_event = threading.Event()

        # preview worker: single-slot latest-wins. The worker reads
        # _preview_req atomically, fetches, writes _preview_result, then
        # checks if _preview_req is still the same id. If not, loops to
        # serve the newer request. See _preview_worker for the snippet
        # we ported verbatim from plan-tui.
        self._preview_req = None
        self._preview_result = None  # (id, text)
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
        self._preview_buffer_cap_chars = int(preview_buffer_cap_chars)
        self._preview_buffer_cap_lines = int(preview_buffer_cap_lines)
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
        # Per-pane row caches for the differential renderer (#186). Keys
        # are pane names ('list', 'children', 'preview', 'info_bar',
        # 'sep_main', 'sep_inner', …); values are ``PaneCache`` objects
        # carrying current/previous rect plus a parallel ``lines`` buffer
        # of cached ``(visible_len, bytes)`` tuples. The renderer
        # migrations in #187/#188 will populate these via the row-buffer
        # shim in 020-terminal; this ticket just wires the dict.
        self._pane_cache: dict = {}

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

    def expand(self, id: Any,
               on_complete: Optional[Callable[[], None]] = None) -> 'Pending':
        """(thread-safe) Add ``id`` to expanded; trigger fetch if not cached.

        Pending resolves when children are cached (or immediately on the
        next drain if already cached).
        """
        pending = Pending()
        if on_complete is not None:
            pending.then(on_complete)
        self.post(lambda: self._do_expand(id, pending))
        return pending

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
        """
        ops_list = list(ops)

        def _apply():
            apply_ops(self._state, ops_list)
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
        """(thread-safe) Inject pre-fetched preview text for ``id_`` from any thread.

        Latest-wins: a later ``set_preview`` overwrites an unconsumed
        earlier one. Recipes using this should construct the Browser
        with ``get_preview=None`` so the built-in worker doesn't race.
        ``text`` is coerced to ``''`` if None.
        """
        if text is None:
            text = ''
        self._preview_result = (id_, text)
        notify_wake()

    def append_preview(self, id_, chunk) -> None:
        """(thread-safe) Append ``chunk`` to the cached preview for ``id_``.

        The append is scheduled on the main thread via ``post()`` so the
        read-modify-write of ``_state._preview[id_]`` is race-free. If
        ``id_`` has no cached entry yet, the chunk becomes the entire
        preview. ``chunk`` is coerced to ``''`` if None.

        Marks the preview pane dirty so the next render pass picks up
        the new content.

        Cache shape note: ``_state._preview`` is a per-id ``dict``, so
        appending to one id does not affect any other. The renderer reads
        ``_state._preview.get(cursor_id, '')`` so an append for a
        non-cursor id is buffered silently until the user navigates to
        that item.

        Ordering caveat: ``set_preview`` (above) routes through the
        single-slot worker pipeline (``_preview_result`` + ``apply_preview_result``)
        while ``append_preview`` / ``clear_preview`` route through the
        post queue. Both land on the main thread, but the post queue
        drains before ``apply_preview_result`` each iteration, so a
        ``set_preview`` posted *after* an ``append_preview`` may still
        land *after* the append on the same iteration — i.e. the set
        wins. Recipes that need strict ordering should pick one path
        per id (typically ``append_preview`` + ``clear_preview`` for
        streaming, ``set_preview`` for one-shot replacements).
        """
        if chunk is None:
            chunk = ''
        def _apply():
            self._state._preview[id_] = (
                self._state._preview.get(id_, '') + chunk
            )
            self._needs_redraw.add('preview')
        self.post(_apply)

    def clear_preview(self, id_) -> None:
        """(thread-safe) Drop cached preview text for ``id_``.

        Scheduled on the main thread via ``post()``. Idempotent — a
        clear for an unknown id is a silent no-op. Marks the preview
        pane dirty so the next render shows the cleared state (which,
        for the cursor item, is rendered as an empty pane until a
        worker fetch or push repopulates the entry).
        """
        def _apply():
            self._state._preview.pop(id_, None)
            self._needs_redraw.add('preview')
        self.post(_apply)

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
            # Replace the cached list — drop the old entries from the
            # auxiliary indexes first so a refresh that swaps the item
            # set under ``id_`` doesn't leave stale ``_items_by_id`` /
            # ``_parent_of_id`` rows pointing at the previous list.
            _index_drop_children(self._state, id_)
            self._state._children[id_] = items
            _index_add_children(self._state, id_, items)
            self._state._children_pending.discard(id_)
            # Worker delivered → no longer loading. Stays addressable
            # via ``_loading`` rather than implied membership in
            # ``_children_pending`` (foundation for ``update_data``).
            self._state._loading[id_] = False
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
            self.apply_preview_result()
            paused = self._preview_paused
            preview_busy = (
                self._preview_req is not None
                and not (paused is not None
                         and paused.get('id') == self._preview_req)
            )
            if (self._main_queue.empty()
                    and not self._children_queue
                    and not self._children_results
                    and not preview_busy
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
            f'preview_paused={self._preview_paused!r}, '
            f'in_flight={list(self._children_in_flight)})'
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
        if id_ not in self._state._children_pending:
            self._state._children_pending.add(id_)
            self._children_queue.append(id_)
        # Re-dispatch every previously-expanded parent (and the scope
        # root) so their sub-trees don't strand on the loading
        # placeholder. These are fire-and-forget — nobody specific is
        # waiting on them, so no Pending registration. Same coalescing
        # rules: skip ids already in flight.
        for x in extra:
            self._state._loading[x] = True
            if x not in self._state._children_pending:
                self._state._children_pending.add(x)
                self._children_queue.append(x)
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

        Also snaps the list viewport so the new cursor row is on-screen.
        The main loop's automatic snap (in ``run()``) only fires when a
        synchronous key handler moves ``state.cursor``; ``cursor_to``
        defers the move via ``post`` so it lands *after* that snap
        check. Without an explicit snap here, programmatic moves would
        slide the cursor off-screen and force the user to scroll
        manually — surprising for what reads as a navigation API.
        """
        # Build/refresh the visible list; visible_items honours the
        # dirty bit so this is a no-op if nothing changed.
        vis = visible_items(self._state)
        for i, entry in enumerate(vis):
            if entry.item.id == id_:
                self._state.cursor = i
                mark_cursor_changed(self)
                self._snap_list_scroll_to_row(i)
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
        # Keep ``_loading`` in lockstep with dispatch (see ``_do_refresh``).
        self._state._loading[id_] = True
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
        """FIFO worker thread: drain ``_children_queue`` one id at a time.

        Per ticket #271, delivery now goes through ``update_data`` rather
        than the legacy ``_children_results`` deque. Per ticket #272, a
        generator return is iterated and each yield is delivered as its
        own ``update_data`` batch. The worker calls the user's
        ``get_children``, coerces the return via ``to_item``, and posts:

        * ``None`` return → no batch is posted; ``_loading`` stays True
          (the recipe will push from elsewhere). Pendings still resolve
          via ``_post_children_delivery`` so ``refresh().then()`` chains
          fire after the worker call returned.
        * generator return → each yielded chunk becomes its own
          ``update_data`` batch (no trailing ``complete`` per chunk).
          A ``list`` yield is treated as a batch of items; anything
          else (``Item``, ``tuple``, ``dict``, ``str``) is treated as a
          single item, coerced via ``to_item``. On clean exhaustion
          (``StopIteration``) the worker emits a final
          ``[complete(parent_id)]`` batch to clear loading. On a
          mid-stream exception the partial deliveries stay in place,
          ``_loading`` is NOT cleared (per the streaming-push spec:
          "loading stays unless caller cleared explicitly"), and the
          error is surfaced via ``self.error(...)``.
        * iterable (incl. ``[]``) return → batch is
          ``[upsert(it.id, parent_id, **fields_of(it)) for it in items]``
          followed by a trailing ``complete(parent_id)`` op (atomic with
          the data: the trailing ``complete`` clears
          ``_loading[parent_id]`` in the same drain). For ``[]``, the
          batch is just ``[complete(parent_id)]``.

        The Pending registry (``_children_in_flight``) is resolved on
        the main thread by ``_post_children_delivery``, posted *after*
        the ``update_data`` callable so ``apply_ops`` has run by the
        time chains fire — a ``then`` callback observes the post-batch
        cache state. For generators the housekeeping is scheduled when
        the generator EXHAUSTS (or raises); mid-stream batches do NOT
        fire the pending chain.

        Errors caught at the boundary -- a misbehaving ``get_children``
        must not crash the worker thread. On error during the initial
        call or while coercing a non-generator iterable: ``error_text``
        is updated, no batch is posted (an empty synthesised delivery
        is sent so the placeholder clears), and the post-delivery
        housekeeping still runs. On a generator mid-stream error the
        items already yielded survive, ``_loading`` stays True, and
        ``_post_children_delivery`` still runs to resolve Pendings.
        """
        while not self._stop:
            self._children_event.wait()
            self._children_event.clear()
            while self._children_queue and not self._stop:
                id_ = self._children_queue.popleft()
                items = None
                gen = None
                error = False
                try:
                    raw = self.get_children(id_)
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
        """Latest-wins single-slot worker (ported from plan-tui).

        Reads ``_preview_req`` atomically, fetches, writes the result.
        Only the main thread writes ``_preview_req`` -- the worker
        clears it only after confirming no newer request landed during
        the fetch. If a newer request did land, we loop immediately to
        serve it without blocking on the event.

        Per #273: when ``get_preview`` returns a generator, the worker
        switches to a streaming branch — see
        ``_stream_preview_from_generator``. Each yield is delivered via
        ``append_preview``; when the running buffer hits the configured
        cap, the worker pauses (without closing the generator). A
        cursor-move closes the generator and pivots; a future #274
        demand signal will resume it.
        """
        while not self._stop:
            self._preview_event.wait()
            self._preview_event.clear()
            while not self._stop:
                req_id = self._preview_req
                if req_id is None:
                    break
                # If a paused generator from an earlier request is
                # still alive (cursor moved between yields), close it
                # so its recipe's ``finally`` runs before we serve the
                # new request.
                self._abandon_paused_preview_if_any(except_id=req_id)
                try:
                    if self.get_preview is not None:
                        result = self.get_preview(req_id)
                    else:
                        result = ''
                except Exception as e:
                    self._preview_result = (
                        req_id, f'[error] {type(e).__name__}: {e}'
                    )
                    notify_wake()
                    if self._preview_req == req_id:
                        self._preview_req = None
                    continue

                if inspect.isgenerator(result):
                    # Streaming branch: drain into ``append_preview``
                    # until cap or exhaustion. Returns when the
                    # generator is exhausted, errored, abandoned, or
                    # left paused. ``_preview_req`` is cleared inside
                    # the streamer at appropriate points (matching the
                    # latest-wins semantics).
                    self._stream_preview_from_generator(req_id, result)
                else:
                    if result is None:
                        result = ''
                    self._preview_result = (req_id, result)
                    notify_wake()
                    # Latest-wins: only clear the slot if no newer
                    # request landed during the fetch. Otherwise loop
                    # immediately to serve the newer one (we'll
                    # overwrite _preview_result on the next iteration
                    # -- the main thread may have already consumed the
                    # previous one, or it may not; either way the
                    # latest result is what wins).
                    if self._preview_req == req_id:
                        self._preview_req = None
                    # else: keep looping with the new req_id

    def _abandon_paused_preview_if_any(self, except_id=None):
        """Close any paused preview generator whose id != ``except_id``.

        Called from the preview worker before serving a new request.
        Closing a generator triggers its ``finally`` blocks via
        ``GeneratorExit`` — recipes use this for resource cleanup
        (file handles, network sockets, etc.).

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

    def _stream_preview_from_generator(self, item_id, gen):
        """Drain a ``get_preview`` generator into ``append_preview``.

        Per ticket #273 / streaming-push spec Section 3:

        * Each yielded chunk is coerced to ``str`` and appended via
          ``append_preview`` (post-queue, race-free read-modify-write).
        * Tracks running buffer size locally (chars + lines). Reading
          back from ``_state._preview`` would race with the main-thread
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

        ``_preview_result`` is NOT used in this branch — the streaming
        path writes directly through the per-id ``_state._preview``
        cache via ``append_preview``. The single-slot result lane is
        only for non-generator returns.
        """
        chars = 0
        lines = 0
        cap_chars = self._preview_buffer_cap_chars
        cap_lines = self._preview_buffer_cap_lines
        # Cumulative caps grow by one window per #274 demand-resume so
        # the local counters can stay running totals (matching the
        # reported ``_preview_paused['chars']`` / ``['lines']``
        # semantics from #273 — the dict records the buffer size at
        # pause time).
        next_cap_chars = cap_chars
        next_cap_lines = cap_lines
        try:
            while not self._stop:
                # Cursor-move abandon check before pulling: if the
                # request slot has moved off our id, drop the
                # generator (its ``finally`` fires via ``gen.close()``)
                # and let the outer worker pick up the new request.
                if self._preview_req != item_id:
                    try:
                        gen.close()
                    except Exception:
                        pass
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
                            next_cap_chars = chars + cap_chars
                            next_cap_lines = lines + cap_lines
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
        root row IS previewable — recipes commonly attach rich content
        to the scope id (browse-claude session card, browse-plan ticket
        body, …) and the user's first-glance row when launching with
        an initial scope is the scope_root. Called by the main loop at
        the top of every iteration (post-#124) so cursor moves and
        worker deliveries both trigger latest-wins preview fetches.

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
            if entry.kind in ('normal', 'scope_root'):
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
            # Wake any paused preview generator so it observes the
            # cursor-off-item state and abandons. Without this, a
            # paused generator from the previously-cursored item would
            # stay alive (and its recipe ``finally`` wouldn't fire)
            # until the cursor lands on a new normal item.
            self._preview_resume_event.set()
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

    def _handle_one_key(self, ctx, key) -> None:
        """Dispatch one keystroke and run the per-key bookkeeping.

        Snapshots the on-screen cursor row before dispatch so the
        viewport snaps back to follow the cursor when the key actually
        moved it (wheel-scroll handlers leave the cursor alone, so the
        comparison is False and ``_list_scroll`` keeps the user's
        scrolled position). After dispatch, kicks the idempotent
        preview / children fetches for the new cursor.

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
            dispatch_key(self, ctx, key)
        new_row = self._active_list_row()
        if prev_insert != self._insert_mode or new_row != prev_row:
            self._snap_list_scroll_to_row(new_row)

        # Trigger preview + children fetches for the (possibly moved)
        # cursor before the next render pass. Idempotent; safe to call
        # once per key during a burst.
        self._update_preview_for_cursor()
        self._update_children_for_cursor()

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
        # Keep ``_loading`` in lockstep with dispatch (see ``_do_refresh``).
        state._loading[item.id] = True
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
