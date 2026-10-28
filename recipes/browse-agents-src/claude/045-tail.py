# Live-tail worker state, keyed by .jsonl path. The worker resumes
# from each entry's byte_offset on its 5s tick and parses new lines
# via _read_new_records (shared between tree + flat modes).
class _TailState:
    """Per-file state for the live-tail worker.

    Persistence semantics:
      * Survives normal navigation (``_scan_tree`` cache hits return
        the existing entry).
      * Cleared by the indirect ``get_children(None)`` reset signal —
        which fires on Ctrl-R / ``ctx.refresh()`` / initial load. The
        next per-file fetch rebuilds from scratch.

    ``error`` latches True on the first malformed complete line OR a
    size shrink (truncation / rotation). The worker skips this file
    thereafter until the user-triggered reset clears
    ``_TAIL_STATE[path]``.

    Fields:
      * ``byte_offset`` — resume point in the file for the next read.
      * ``last_size`` — file size at the previous tick; the worker
        bails when ``current_size == last_size``.
      * ``start_line`` — line index of the byte at ``byte_offset``,
        used when no ``_TREE_CACHE`` entry exists to count from
        (flat-mode lazy bootstrap).
      * ``cursor`` — the open-turn / open-span state machine, shared
        between bulk scan and the tail's incremental fold.
      * ``error`` — latched failure flag.
      * ``lock`` — held by ``_scan_tree`` (blocking) and the worker
        (trylock — bail on contention, matching best-effort).
    """

    __slots__ = ('byte_offset', 'last_size', 'start_line',
                 'cursor', 'error', 'lock')

    def __init__(self, *, byte_offset, last_size, start_line,
                 cursor, error):
        self.byte_offset = byte_offset
        self.last_size = last_size
        self.start_line = start_line
        self.cursor = cursor
        self.error = error
        self.lock = threading.Lock()


_TAIL_STATE = {}    # path → _TailState
_TAIL_STOP = None   # threading.Event; created in main()
_TAIL_THREAD = None # threading.Thread; created in main()


def _resolve_path_for_bust(item_id):
    """Map an id to the .jsonl path whose ``_TreeData`` it depends on.

    Returns ``None`` when ``item_id`` doesn't reference per-file scanned
    state (project dirs, synthetic ids).
    """
    if not isinstance(item_id, tuple) or not item_id:
        return None
    tag = item_id[0]
    # Subagent boundary: ``('agent', sess, aid)`` → subagent's own jsonl.
    if tag == 'agent':
        sess, aid = item_id[1], item_id[2]
        if not aid:
            return None
        agent_path = _resolve_agent_jsonl(sess, aid)
        return agent_path if os.path.isfile(agent_path) else None
    # Markdown ids carry the path inside their anchor ``('msg', jsonl, n)``.
    if tag in ('md', 'refs'):
        path = item_id[1][1]
        return path if os.path.isfile(path) else None
    # Any other per-file id ``(tag, jsonl, …)`` resolves to that jsonl.
    if tag in ('session', 'prompt', 'tool', 'span', 'msg', 'trunc', 'err'):
        path = item_id[1]
        return path if os.path.isfile(path) else None
    return None


def _bust_caches_for(item_id):
    """Drop cached ``_TreeData`` + ``_TailState`` for one file's tree.

    Used by ``get_children(..., reload=True)`` to refresh a single
    subtree without disturbing siblings. No-op for ids that don't
    resolve to a per-file ``_TreeData`` (project dirs, unparseable ids).
    """
    path = _resolve_path_for_bust(item_id)
    if path is None:
        return
    _TREE_CACHE.pop(path, None)
    _TAIL_STATE.pop(path, None)


def _scan_tree(path):
    """Return cached ``_TreeData`` for ``path``; bulk-scan on cache miss.

    Cache invalidation is explicit (``get_children(id, reload=True)``
    drops the entry; ``get_children(None, reload=True)`` clears
    everything). The live-tail worker keeps the cached ``td`` in
    sync with file growth — no time-based check needed. A bulk scan
    on cache miss bootstraps ``_TAIL_STATE`` from the file's current
    end so the tail resumes from the exact byte where this scan stopped.

    Concurrent callers (children worker + preview worker on file open)
    serialize on a per-path mutex so a huge transcript isn't scanned
    twice under GIL contention. The mutex is dropped on completion;
    the cache lookup is double-checked under the lock so the second
    caller returns the just-populated entry without re-scanning.
    """
    cached = _TREE_CACHE.get(path)
    if cached is not None:
        return cached
    with _SCAN_LOCKS_LOCK:
        path_lock = _SCAN_LOCKS.setdefault(path, threading.Lock())
    with path_lock:
        # Re-check after acquiring — the thread we were waiting on
        # may have populated the cache.
        cached = _TREE_CACHE.get(path)
        if cached is not None:
            return cached
        return _scan_tree_locked(path)


def _scan_tree_locked(path):
    """Body of ``_scan_tree`` — runs under the per-path mutex.

    Caller has already verified ``_TREE_CACHE`` is empty for ``path``
    and holds the per-path lock.
    """
    td = _TreeData()
    cur = _ScanCursor()
    try:
        f = open(path, 'r', encoding='utf-8', errors='replace')
    except OSError:
        _TREE_CACHE[path] = td
        return td
    # Use readline() (not ``for line in f``) so ``f.tell()`` is legal —
    # the iterator protocol disables tell() to protect its read-ahead
    # buffer. We need tell() to record byte offsets per line for the
    # live-tail worker's resume point.
    last_full_offset = 0
    n = 0
    with f:
        while True:
            line = f.readline()
            if not line:
                break
            had_nl = line.endswith('\n')
            if not had_nl:
                # Incomplete trailing line — leave offset at its start
                # so the tail picks it up when more bytes arrive.
                break
            stripped = line.rstrip('\n')
            if not stripped.strip():
                td.records.append(None)
            else:
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    rec = None
                if not isinstance(rec, dict):
                    td.records.append(None)
                else:
                    td.records.append(rec)
                    _fold_record(td, cur, rec, n, path)
                    if _is_voice(rec):
                        td.latest_voice_line = n
            last_full_offset = f.tell()
            # Sentinel pattern: each appended ``f.tell()`` becomes the
            # start of the next line. After N complete lines,
            # ``line_offsets`` has N+1 entries, and the length of line
            # k is ``line_offsets[k+1] - line_offsets[k]``.
            td.line_offsets.append(last_full_offset)
            n += 1

    # Stash the byte offset + scan cursor + last-seen size for the
    # live-tail worker. The worker resumes from exactly the byte the
    # bulk scan stopped at, with the same open-turn / open-span state.
    _TAIL_STATE[path] = _TailState(
        byte_offset=last_full_offset,
        last_size=last_full_offset,
        start_line=n,
        cursor=cur,
        error=False,
    )
    _TREE_CACHE[path] = td
    return td


def _read_new_records(path):
    """Read newly-appended records since the last call.

    The single source of truth for both tree-mode and flat-mode live
    tail. When ``_TREE_CACHE`` has an entry, the parsed records are
    also folded into ``td`` via ``_fold_record`` (so tree-mode
    umbrellas, parent_id_of, agent_link, etc. stay current) and the
    set of umbrella parents whose visible child list grew is
    returned. Flat mode ignores the dirty set and consumes
    ``records`` directly.

    Returns ``(records, dirty_parents)``:
      * ``records`` — ``[(line_no, rec_or_None), ...]`` in file order.
        ``rec`` is ``None`` for blank lines and non-dict JSON.
      * ``dirty_parents`` — set of umbrella ids whose visible child
        list grew. Empty when no ``_TREE_CACHE`` entry exists.

    Best-effort, per the design:

      * ``([], set())`` when state is missing, the error flag is set,
        the size hasn't advanced, or the per-file lock is held.
      * Sets the error flag on first malformed complete-line JSON or
        on size shrink (truncation / rotation). Records collected
        before the failure are still returned.
    """
    tail = _TAIL_STATE.get(path)
    if tail is None or tail.error:
        return ([], set())
    try:
        size = os.path.getsize(path)
    except OSError:
        return ([], set())
    if size < tail.last_size:
        # Truncation / rotation — our offset is stale; refuse to tail
        # until the user re-reads (``get_children(None)`` clears the
        # state on Ctrl-R / refresh).
        tail.error = True
        return ([], set())
    if size == tail.last_size:
        return ([], set())

    if not tail.lock.acquire(blocking=False):
        # Contention with ``_scan_tree``'s blocking acquire. Best-
        # effort: bail and retry on the next tick.
        return ([], set())
    new_records = []
    dirty = set()
    try:
        td = _TREE_CACHE.get(path)   # ``td`` or None for flat-only paths
        try:
            f = open(path, 'r', encoding='utf-8', errors='replace')
        except OSError:
            return ([], set())
        with f:
            f.seek(tail.byte_offset)
            # Starting line number: prefer ``td.records``-based count
            # (tree-mode invariant); fall back to ``tail.start_line``
            # which we track for flat-only files.
            n = len(td.records) if td is not None else tail.start_line
            while True:
                line = f.readline()
                if not line:
                    break
                if not line.endswith('\n'):
                    # Incomplete trailing line — leave the offset at
                    # its start. The tail picks it up next tick once
                    # more bytes have been appended.
                    break
                stripped = line.rstrip('\n')
                rec = None
                if stripped.strip():
                    try:
                        rec = json.loads(stripped)
                    except json.JSONDecodeError:
                        # First malformed complete line latches the
                        # error flag. Anything we already read this
                        # tick still flows back to the caller.
                        tail.error = True
                        break
                    if not isinstance(rec, dict):
                        rec = None
                new_records.append((n, rec))
                if td is not None:
                    td.records.append(rec)
                    if rec is not None:
                        dirty |= _fold_record(td, cur=tail.cursor,
                                              rec=rec, n=n,
                                              path=path)
                        if _is_voice(rec):
                            td.latest_voice_line = n
                tail.byte_offset = f.tell()
                # Sentinel pattern: append the end-of-line position
                # so ``line_offsets[n+1] - line_offsets[n]`` always
                # gives line n's length. Done both for the cached
                # ``td`` (tree mode) and on flat-only paths via the
                # ``records`` list above; the invariant is
                # ``len(td.line_offsets) == len(td.records) + 1``.
                if td is not None:
                    td.line_offsets.append(tail.byte_offset)
                n += 1
        # Keep ``tail.start_line`` in sync for flat-only paths; tree
        # mode reads it back from ``td.records`` on the next call.
        if td is None:
            tail.start_line = n
        tail.last_size = size
    finally:
        tail.lock.release()
    return (new_records, dirty)


def _refresh_hidden_in_op(op):
    """Return ``op`` with its ``hidden`` field re-evaluated against the
    live ``_DETAIL_LEVEL`` global.

    Used by the umbrella generator's flush helper so a filter toggle
    that lands between descent (when an item's ``hidden`` was sampled)
    and flush (when the upsert is posted to the framework) takes
    effect on the items being pushed. Non-upsert ops pass through
    unchanged; upsert ops without ``hidden`` in their fields also
    pass through.
    """
    if not (isinstance(op, tuple) and op and op[0] == 'upsert'):
        return op
    _, cid, parent_id, fields, *rest = op
    if not isinstance(fields, dict) or 'hidden' not in fields:
        return op
    new_fields = dict(fields)
    new_fields['hidden'] = not _passes_filter(cid)
    return (op[0], cid, parent_id, new_fields) + tuple(rest)


def _item_to_upsert_kwargs(item):
    """Extract Item fields + custom attrs into kwargs for ``upsert``.

    ``upsert`` accepts arbitrary keyword args: declared dataclass
    fields map to ``Item.__init__`` kwargs, anything else lands as
    ``setattr`` on the freshly-built Item (mirrors ``to_item``).
    Iterating ``vars(item)`` picks up both the declared fields and
    the recipe-specific attrs (kind, line_no, row_bg, …) without
    enumerating each one by name.
    """
    out = {}
    for k, v in vars(item).items():
        if k == 'id':
            continue
        out[k] = v
    return out


def _push_tail_diffs(b, dirty_parents):
    """Apply ``update_data`` patches for every dirty umbrella's new kids.

    For each parent id whose child list grew during the fold step, we
    compare the live ``get_children(parent)`` output against what the
    framework already has cached and ``upsert`` only the new entries,
    each positioned after its predecessor in the fresh listing (so a
    tail-linked subagent lands INSIDE the session's top block — the
    divider sentinel ids double as pivots — and a first subagent
    brings the whole block in at the top, refetch-identical). Ops
    apply in batch order, so a pivot upserted earlier in the same
    batch resolves; a missing pivot degrades to append. Parents not
    yet expanded by the user are skipped — their children will be
    fetched the normal way once expansion happens. Called on the main
    thread via ``Browser.update_data`` (thread-safe).
    """
    if not dirty_parents:
        return
    ops = []
    for parent_id in dirty_parents:
        # At a reduced detail level, a tail append (e.g., a voice
        # record joining a previously-hidden ``span`` umbrella) can
        # cross the parent's min level under the current threshold.
        # Reveal the parent first, in the same batch as its new
        # children. Skip when the parent isn't in our index (yet to be
        # upserted) — it'll be built with the right ``hidden`` from the
        # builder.
        parent_item = b.get_item(parent_id)
        if (
            parent_item is not None
            and getattr(parent_item, 'hidden', False)
            and _passes_filter(parent_id)
        ):
            ops.append(mod(parent_id, hidden=False))
        cached = b.cached_children(parent_id)
        if cached is None:
            continue
        existing_ids = {it.id for it in cached}
        try:
            fresh = get_children(parent_id)
        except Exception:
            continue
        prev_id = None
        for child in fresh:
            if child.id not in existing_ids:
                where = (('first', None) if prev_id is None
                         else ('after', None, prev_id))
                ops.append(upsert(child.id, parent_id, where=where,
                                  **_item_to_upsert_kwargs(child)))
            prev_id = child.id
    if ops:
        b.update_data(ops)


def _tail_worker(b):
    """Background loop: every 5s, tail the cursor's file once.

    Best-effort and single-threaded — one file per tick, the file
    being the one the cursor currently sits in (per the flat/tree
    rule encoded in ``_cursor_tail_path``). Reads new bytes via the
    shared ``_read_new_records`` step, then dispatches mode-specific
    push:

      * Tree mode consumes the ``dirty_parents`` set returned by the
        fold step (each parent umbrella's child list grew).
      * Flat mode consumes the raw ``records`` list and emits one
        ``upsert`` per new line, positioned after the last subagent
        row.

    Stopped via the module-level ``_TAIL_STOP`` event when ``main``
    exits. Marked daemon as a backstop.
    """
    while not _TAIL_STOP.is_set():
        try:
            path = _resolve_tail_path_from_browser(b)
            if path is not None:
                _maybe_bootstrap_tail_state(path)
                records, dirty = _read_new_records(path)
                if records or dirty:
                    if _TREE_MODE:
                        _push_tail_diffs(b, dirty)
                    else:
                        _push_flat_inserts(b, path, records)
                    # Bust the preview cache for the file so the
                    # session-card preview re-renders even when the
                    # cursor stayed parked on a row above the
                    # changed listing.
                    b.post(lambda p=path: _bust_preview_for_path(b, p))
        except Exception:
            # Best-effort: a malformed cursor row / get_children
            # blowup must not kill the worker. Swallow silently;
            # the next tick gets a clean shot.
            pass
        _TAIL_STOP.wait(timeout=5.0)


def _maybe_bootstrap_tail_state(path):
    """Populate ``_TAIL_STATE[path]`` if missing.

    Tree mode bootstraps via ``_scan_tree`` (already wired). Flat
    mode never calls ``_scan_tree`` so the entry would otherwise
    never appear — we lazy-init here. ``last_size=0`` is deliberate:
    the framework may have a cached children list from before the
    worker started observing this file (e.g. the file grew after
    the initial get_children call but before the worker's first
    tick on it). Forcing the first post-bootstrap tick to see
    ``size > 0`` triggers the read that syncs the cache.
    """
    if path in _TAIL_STATE:
        return
    _TAIL_STATE[path] = _TailState(
        byte_offset=0, last_size=0, start_line=0,
        cursor=_ScanCursor(), error=False,
    )


def _push_flat_inserts(b, path, records):
    """Flat-mode push: build Items for new records and append them.

    Flat mode now renders messages in chronological order (oldest
    first, latest at the bottom — same as tree mode). New tail
    records belong at the **end** of the list. ``where=None`` uses
    the framework's default "append at the end of the parent's
    children" behaviour.

    The whole batch goes through one ``update_data`` call so the
    framework's cursor anchor runs exactly once at the end and the
    user's cursor position is preserved across the insert.
    """
    if not records:
        return
    session_id = ('session', path)
    existing = b.cached_children(session_id)
    if existing is None:
        return
    existing_ids = {it.id for it in existing}

    ops = []
    for n, rec in records:
        if rec is None:
            continue
        item_id = ('msg', path, n)
        if item_id in existing_ids:
            continue
        kind = _kind_of(rec)
        fields = {
            'title': _oneline(_summarise_message(rec)),
            'tag': kind,
            'tag_style': _TAG_STYLE_FOR_KIND.get(kind, ''),
            'has_children': False,
            'hidden': not _passes_filter(item_id),
            'kind': 'message',
            'line_no': n,
            'session_path': path,
            'message_kind': kind,
        }
        if _is_voice(rec):
            bg = _ROW_BG_FOR_KIND.get(kind)
            if bg is not None:
                fields['row_bg'] = bg
        ops.append(upsert(item_id, session_id, **fields))
    if ops:
        b.update_data(ops)


def _id_jsonl(item_id):
    """The session/file .jsonl path an id is keyed under, or ``None``.

    For every per-file id this is the path that appears as the id's first
    coordinate — the file the row is *displayed under*. A subagent group
    ``('agent', sess, aid)`` resolves to ``sess`` (the session it nests in),
    matching the old ``<sess>#...`` prefix semantics. Markdown ids read the
    path out of their anchor. ``('project', dir)`` and synthetic ids yield
    ``None``.
    """
    if not isinstance(item_id, tuple) or not item_id:
        return None
    tag = item_id[0]
    if tag in ('session', 'agent', 'prompt', 'tool', 'span', 'msg',
               'trunc', 'err'):
        return item_id[1]
    if tag in ('md', 'refs'):
        return item_id[1][1]
    return None


def _bust_preview_for_path(b, path):
    """Drop cached preview text for ``path`` and force a re-fetch.

    Runs on the main thread (via ``Browser.post``). For the cursor's
    row or any descendant of ``path``, calls ``invalidate_preview``
    which re-fetches *without* resetting view state — a user who
    pinned the view to the bottom (Shift-End) keeps following the
    tail as the umbrella's preview grows. For non-cursor paths the
    cache is dropped so the next visit re-fetches.
    """
    cur_id = b.preview_item_id
    if cur_id == ('session', path) or _id_jsonl(cur_id) == path:
        b.invalidate_preview(cur_id)
    else:
        b.drop_preview_cache(('session', path))


def _resolve_tail_path_from_browser(b):
    """Return the file the cursor's current row maps to, or None.

    Reads the cursor + visible-list through a one-off Context so
    the scope_root row's id participates too — a freshly-launched
    recipe that hasn't moved the cursor still resolves a tail path.
    """
    try:
        from browse_tui import Context
        ctx = Context(b)
        vis = ctx.visible_items
        if not vis:
            return None
        idx = ctx.cursor_index
        if not (0 <= idx < len(vis)):
            return None
        item, _kind = vis[idx]
        item_id = item.id if item is not None else None
        return _cursor_tail_path(item_id)
    except Exception:
        return None


def _cursor_tail_path(item_id):
    """Resolve the .jsonl path the tailer should watch for ``item_id``.

    Mode-dependent for the subagent group row:

      * Tree mode: ``('agent', sess, aid)`` reflects activity in the
        parent session's tree, so tail the parent.
      * Flat mode: ``('agent', sess, aid)`` IS the subagent's listing
        (drill-into row), so tail the subagent jsonl.

    Other id kinds: take the id's backing .jsonl. Returns ``None`` if the
    resolved path isn't a readable ``.jsonl``.
    """
    if not isinstance(item_id, tuple) or not item_id:
        return None
    if item_id[0] == 'agent':
        sess, aid = item_id[1], item_id[2]
        if _TREE_MODE:
            return sess if os.path.isfile(sess) else None
        agent_path = _resolve_agent_jsonl(sess, aid)
        return agent_path if os.path.isfile(agent_path) else None
    path = _id_jsonl(item_id)
    if path is not None and path.endswith('.jsonl') and os.path.isfile(path):
        return path
    return None


def _last_voice_id(jsonl_path):
    """Return ``('msg', jsonl, n)`` id of the most-recent voice record, or None.

    "Voice" matches ``_is_voice`` (user prompt or assistant text).
    Reads the eagerly-tracked ``td.latest_voice_line`` populated by
    ``_scan_tree`` during its forward pass, so this is O(1) on cache
    hit (no reverse walk).
    """
    td = _scan_tree(jsonl_path)
    n = td.latest_voice_line
    if n is None:
        return None
    return ('msg', jsonl_path, n)


def _last_visible_voice_id(jsonl_path):
    """Newest voice record VISIBLE at the current detail level, or None.

    The absolute latest voice can be filtered out — a mid-turn assistant
    text at the summary level — and ``cursor_to`` silently no-ops on
    hidden rows, so the cursor-on-open jump must target a row the filter
    shows. Walks back from ``td.latest_voice_line`` to the newest voice
    that passes; when NO voice is visible, returns the absolute latest
    so the caller's chain trim lands on its nearest visible ancestor
    (the wrapping ``<prompt>``).
    """
    td = _scan_tree(jsonl_path)
    n = td.latest_voice_line
    if n is None:
        return None
    for i in range(n, -1, -1):
        rec = td.records[i]
        if not (isinstance(rec, dict) and _is_voice(rec)):
            continue
        if _passes_filter(('msg', jsonl_path, i), td):
            return ('msg', jsonl_path, i)
    return ('msg', jsonl_path, n)
