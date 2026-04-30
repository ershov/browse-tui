"""browse-tui: state layer (visible tree, cursor/scope, async workers, post queue, Pending)."""

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


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
    """

    __slots__ = ('_done', '_chain')

    def __init__(self):
        self._done = False
        self._chain = []

    @property
    def done(self) -> bool:
        return self._done

    def then(self, callback) -> 'Pending':
        if self._done:
            callback()
        else:
            self._chain.append(callback)
        return self

    def _resolve(self) -> None:
        if self._done:
            return  # idempotent -- second resolve is a no-op
        self._done = True
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


class Browser:
    """The TUI engine and async coordinator.

    Holds the data caches (``_state``), the cross-thread post queue
    (``_main_queue``), the children FIFO worker, and the latest-wins
    preview worker. All public mutation goes through ``post(fn)`` so
    background threads (workers, watchers, signal handlers) are safe to
    schedule work without taking locks -- the main thread drains the
    queue on every wake.

    Construction kwargs (others arrive in ticket #8):
      get_children: callable (parent_id) -> Iterable[Item|str|tuple|dict]
      get_preview:  callable (item_id) -> str | None
      root_id:      Any (default None)
      _headless:    bool (skip terminal init; default False) -- the flag
                    is observable here for tests; the actual terminal
                    init/teardown branches on it once #9 lands.
    """

    def __init__(self, *,
                 get_children=None,
                 get_preview=None,
                 root_id=None,
                 _headless=False):
        # --- user-supplied data callbacks -------------------------------
        # Default get_children to "no children" so a Browser constructed
        # with no kwargs still works (tests, smoke checks). get_preview
        # stays None -- the preview worker treats None as "always returns
        # ''" rather than calling a no-op lambda needlessly.
        self.get_children = get_children or (lambda _id: [])
        self.get_preview = get_preview
        self._headless = _headless

        # --- domain state ------------------------------------------------
        # State stays a separate dataclass so unit tests can poke it
        # without spinning up a Browser. The preview cache lives on State
        # alongside _children for cohesion (one place to invalidate
        # everything per item id).
        self._state = State(root_id=root_id)
        self._state._preview = {}  # item_id -> preview text

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

    # ---- public, thread-safe API ---------------------------------------

    def refresh(self, id=None, on_complete=None) -> 'Pending':
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

    def post(self, fn) -> None:
        """Schedule ``fn`` to run on the main thread on the next drain.

        The callable runs with no arguments and its return value is
        ignored. Exceptions inside ``fn`` propagate to the drain loop --
        callers should catch their own exceptions if they want to keep
        the drain going. (We may revisit and wrap in try/except once the
        renderer can surface a status line.)
        """
        self._main_queue.put(fn)
        notify_wake()

    def message(self, text) -> None:
        """Surface ``text`` as a transient status message.

        Stored on Browser; the renderer in ticket #10 picks it up. Safe
        to call from any thread (uses ``post`` under the hood so the
        write happens on the main thread).
        """
        self.post(lambda: setattr(self, '_message_text', text))

    def error(self, text) -> None:
        """Surface ``text`` as an error message. Same lane as ``message``."""
        self.post(lambda: setattr(self, '_error_text', text))

    @property
    def error_text(self) -> str:
        return self._error_text

    @property
    def message_text(self) -> str:
        return self._message_text

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
        that id (Phase 1 does not coalesce -- two refresh()es of the
        same id produce two cache writes and two resolved Pendings).
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
        return n

    def apply_preview_result(self) -> bool:
        """Move the worker-produced preview text into the cache.

        Returns True if a result was applied, False if the slot was empty.
        """
        if self._preview_result is None:
            return False
        id_, text = self._preview_result
        self._preview_result = None
        self._state._preview[id_] = text
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
        """
        if id_ is None:
            cache_invalidate_all(self._state)
            id_ = self._state.root_id
        else:
            cache_invalidate_subtree(self._state, id_)
        self._state._children_pending.add(id_)
        self._children_in_flight.setdefault(id_, []).append(pending)
        self._children_queue.append(id_)
        self._children_event.set()

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

    def request_preview(self, id_) -> None:
        """Set the latest-wins preview request slot.

        Called on the main thread (typically by cursor-move handlers in
        ticket #8). Idempotent for the same id.
        """
        self._preview_req = id_
        self._preview_event.set()
