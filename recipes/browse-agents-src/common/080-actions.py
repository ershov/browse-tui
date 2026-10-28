# ---- actions -------------------------------------------------------------


def _current_view_targets(ctx):
    """Pick the items the ``V`` / ``E`` actions should operate on.

    Falls back through:
      1. The explicit selection (``ctx.targets`` when something is
         selected with space-bar).
      2. The cursor's item.
      3. The scope_root row — so ``V`` / ``E`` work even when the
         cursor sits on the synthetic top row of an opened file.

    Returns ``[]`` when nothing resolves.
    """
    targets = list(ctx.targets) if ctx.cursor is not None else []
    if targets:
        return targets
    rows = ctx.visible_items
    if rows and 0 <= ctx.cursor_index < len(rows):
        return [rows[ctx.cursor_index][0]]
    return []


def _gather_line_source(items):
    """Group items into ``{path: [line_no, ...]}`` for byte extraction.

    Each input item is classified by id tag:

      * ``('msg', jsonl, n)`` (message leaf) → ``(jsonl, n)`` recorded.
      * Umbrella ids (``prompt`` / ``tool`` / ``span``) → recurse through the
        umbrella's same-file descendants and record each leaf's line.
        ``agent`` boundaries are NOT crossed — subagents live in their own
        ``.jsonl`` files.
      * ``('agent', sess, aid)`` (subagent group row) → the subagent's own
        ``.jsonl`` is the "whole file" to open.
      * ``('session', jsonl)`` / ``('trunc'|'err', jsonl)`` → that file
        whole. ``('project', dir)`` → that directory whole.

    Returns ``(per_line, whole_paths)`` where ``per_line`` is the
    grouping dict (lines kept in document order) and ``whole_paths``
    is a list of ``(path, item)`` tuples for non-line targets.
    """
    per_line = {}
    whole_paths = []
    for item in items:
        iid = getattr(item, 'id', None)
        if not isinstance(iid, tuple) or not iid:
            continue
        tag = iid[0]
        if tag == 'agent':
            agent_path = _resolve_agent_jsonl(iid[1], iid[2])
            whole_paths.append((agent_path, item))
            continue
        if tag in ('prompt', 'tool', 'span'):
            # Recurse through the umbrella's same-file leaves.
            path = iid[1]
            same_file_lines = _gather_umbrella_lines(iid)
            if same_file_lines:
                per_line.setdefault(path, []).extend(same_file_lines)
            else:
                # Defensive: empty umbrella → fall back to opening
                # the file as a whole rather than silently doing
                # nothing.
                whole_paths.append((path, item))
            continue
        if tag == 'msg':
            per_line.setdefault(iid[1], []).append(iid[2])
            continue
        # Whole-file / whole-dir targets (session, trunc, err, project) and
        # any other id: open the underlying path.
        path = _id_jsonl(iid)
        if path is None and tag == 'project':
            path = iid[1]
        whole_paths.append((path, item))
    # Pre-select pass: dedupe + sort each file's lines into
    # canonical (file) order. Multi-target invocations (umbrella +
    # one of its children, or two umbrellas with overlapping
    # contents) collapse to one entry per leaf in document order —
    # no surprises when ``_write_line_excerpts`` walks the list.
    for path in per_line:
        per_line[path] = sorted(set(per_line[path]))
    return per_line, whole_paths


def _gather_umbrella_lines(item_id):
    """Return [line_no, ...] for every leaf message under ``item_id``.

    Walks the umbrella's children recursively in document order via
    ``get_children`` (the same listing helper that builds the
    displayed tree). Nested umbrellas in the SAME file are followed
    transparently; ``boundary`` children (subagent groups) are NOT
    crossed — their records live in a different ``.jsonl`` and the user
    can drill into that group row separately if they want them.
    """
    out = []
    _walk_umbrella_lines(item_id, out)
    return out


def _walk_umbrella_lines(item_id, out):
    """Append leaf line numbers under ``item_id`` to ``out`` in order."""
    try:
        children = get_children(item_id)
    except Exception:
        return
    for child in children:
        cid = getattr(child, 'id', None)
        if not isinstance(cid, tuple) or not cid:
            continue
        if getattr(child, 'boundary', False):
            # Boundary child — a self-contained foreign subtree (subagent
            # group): its content lives in a different file, so the
            # export excerpt stops at the group row (the user drills into
            # it separately). md docs never appear in this cascade, so
            # this only matches the ``agent`` rows it did by id shape.
            continue
        if cid[0] in ('prompt', 'tool', 'span'):
            _walk_umbrella_lines(cid, out)
            continue
        if cid[0] == 'msg':
            out.append(cid[2])


def _write_line_excerpts(per_line):
    """Extract specific lines from one-or-more files into a temp .jsonl.

    Uses ``_TREE_CACHE[path].line_offsets`` for O(1) seek + read —
    no whole-file scan. Returns the temp path (caller is responsible
    for unlinking it after the external tool exits) or ``None`` if
    nothing could be extracted.
    """
    import tempfile
    written_any = False
    tmp = tempfile.NamedTemporaryFile(
        'wb', suffix='.jsonl', delete=False,
    )
    try:
        # ``per_line`` is already pre-selected (deduped + sorted) by
        # ``_gather_line_source`` — iterate as-is.
        for path, line_nos in per_line.items():
            cached = _TREE_CACHE.get(path)
            if cached is None:
                continue
            offsets = cached.line_offsets
            try:
                f = open(path, 'rb')
            except OSError:
                continue
            with f:
                for n in line_nos:
                    if not (0 <= n < len(offsets) - 1):
                        continue
                    start = offsets[n]
                    end = offsets[n + 1]
                    if end <= start:
                        continue
                    f.seek(start)
                    chunk = f.read(end - start)
                    if not chunk:
                        continue
                    tmp.write(chunk)
                    if not chunk.endswith(b'\n'):
                        tmp.write(b'\n')
                    written_any = True
    finally:
        tmp.close()
    if not written_any:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        return None
    return tmp.name


def _run_source_command(ctx, cmd_env_var, default_cmd):
    """Shared body of ``V`` / ``E``: resolve targets, dispatch external tool.

    Strategy:
      * All targets are message ids on the SAME file → extract just
        those lines to a temp .jsonl and open that.
      * All targets are message ids spanning multiple files → same,
        concatenated into one temp file.
      * Anything else (umbrella, subagent group, session, project) →
        open the resolved path directly. With multiple non-message
        targets we open just the first; mixing per-line + per-path
        in one external invocation isn't a sensible UX.
    """
    targets = _current_view_targets(ctx)
    if not targets:
        return
    per_line, whole_paths = _gather_line_source(targets)
    cmd = _split_cmd(os.environ.get(cmd_env_var) or default_cmd)

    # All-message-targets path: extract bytes from line_offsets.
    if per_line and not whole_paths:
        tmp_path = _write_line_excerpts(per_line)
        if tmp_path is None:
            ctx.error('could not extract line bytes (cache missing?)')
            return
        try:
            ctx.run_external(cmd + [tmp_path])
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return

    # Single non-message target (or mixed): open the first resolved
    # path. Covers cursor-on-session, cursor-on-scope_root, cursor-on-
    # umbrella, and cursor-on-project use cases.
    if whole_paths:
        path, _item = whole_paths[0]
        if not path or not os.path.exists(path):
            ctx.error(f'no such path: {path!r}')
            return
        ctx.run_external(cmd + [path])
        return


def _action_edit_source(ctx):
    """``E`` — open the source for the cursor row (or selection) in $EDITOR.

    Message rows (single or multi-selected) extract just their lines
    via ``_TreeData.line_offsets`` into a temp .jsonl. Session /
    subagent / umbrella / project rows open their underlying file
    or directory in $EDITOR directly.

    Separate from the framework's built-in ``e`` (which opens the
    *preview text* in $EDITOR with edits discarded). This one
    operates on the on-disk source.
    """
    _run_source_command(ctx, 'EDITOR', 'vim')


def _action_view_source(ctx):
    """``V`` — page the source for the cursor row (or selection) in $PAGER.

    Same target-resolution rules as ``E``. Pairs with the framework's
    built-in ``v`` (which pages the rendered *preview* — useful when
    the preview is multi-screen) versus this which is the raw
    transcript bytes.
    """
    _run_source_command(ctx, 'PAGER', 'less -R')


def _split_cmd(s):
    """Shell-split a command env var like ``less -R`` or ``bat --paging=always``."""
    import shlex
    return shlex.split(s)


def _resolve_md_pager():
    """Return the resolved markdown-pager command, or None.

    Order: ``$MDCAT`` (shell-split), then ``mdcat | less -RS`` if
    both binaries are on PATH. Recipes that want a different fallback
    can set ``$MDCAT`` to anything — including a piped chain like
    ``mdcat | less -R``, which we shell out via ``bash -c`` so the
    pipe works.
    """
    import shlex
    env_cmd = os.environ.get('MDCAT')
    if env_cmd:
        if '|' in env_cmd or '<' in env_cmd or '>' in env_cmd:
            # User-supplied pipeline — hand to a shell so the pipe runs.
            return ['bash', '-c', f'{env_cmd} "$0"']
        return shlex.split(env_cmd)
    if shutil.which('mdcat') and shutil.which('less'):
        return ['bash', '-c', 'mdcat "$0" | less -RS']
    if shutil.which('mdcat'):
        return ['mdcat']
    return None


def _action_md_preview(ctx):
    """``m`` — view the preview through a markdown-to-ANSI renderer.

    Resolves the renderer via ``_resolve_md_pager``. Reads the cursor
    row's preview (synchronously via ``get_preview`` if not cached),
    writes it verbatim to a temp .md file, and runs the renderer on
    it. Same plumbing as ``v``'s ``less -R``: the preview text is
    handed off as-is; whatever ANSI it carries is the renderer's
    problem to handle (or pass through).
    """
    import tempfile
    cmd = _resolve_md_pager()
    if cmd is None:
        ctx.flash('no markdown renderer found ($MDCAT / mdcat)', log=True)
        return

    item = ctx.cursor
    if item is None:
        return
    text = ctx.get_cached_preview(item.id)
    if text is None:
        try:
            text = get_preview(item.id)
        except Exception as e:
            ctx.error(f'preview: {type(e).__name__}: {e}')
            return
    if text is None:
        ctx.flash('No preview available')
        return

    tmp = tempfile.NamedTemporaryFile(
        'w', suffix='.md', delete=False,
        encoding='utf-8', errors='surrogateescape',
    )
    try:
        tmp.write(text)
        tmp.close()
        ctx.run_external(cmd + [tmp.name])
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _action_toggle_md(ctx):
    """Toggle md2ansi rendering of user/assistant voice content."""
    global _MD_COLOR
    _MD_COLOR = not _MD_COLOR
    ctx.drop_preview_cache()
    ctx.flash(f'voice rendering: {"colored" if _MD_COLOR else "raw"}')


def _action_show_id(ctx):
    """Page the cursor row's full id (paste-friendly, handy for debugging).

    A 1s flash can't be read or copied; the pager is full-screen and
    selectable, matching the action's "paste-friendly" label.
    """
    if ctx.cursor is None:
        return
    ctx.page(str(ctx.cursor.id))


def _jump_to_voice(ctx, direction):
    """Move the cursor to the next/prev row with a row-bg highlight.

    "Voice" rows are user prompts and assistant text replies — the
    speech-only rows ``_is_voice`` marks via ``row_bg``. Direction
    is positional in the visible list: ``+1`` walks down (which now
    means *newer* messages — the list is chronological), ``-1`` walks
    up (older).

    Walks the *visible* item list, not the on-disk tree. That means
    jumps cross expanded subtree boundaries naturally — if two
    sessions are expanded in the same project view, J keeps walking
    past the end of one session's messages into the next session's.
    Collapsed subtrees are simply skipped (their voice rows aren't
    visible); the user can expand them with ``Right`` first if they
    want to navigate inside.
    """
    rows = ctx.visible_items
    if not rows:
        return
    start = ctx.cursor_index
    rng = (range(start + 1, len(rows)) if direction > 0
           else range(start - 1, -1, -1))
    for i in rng:
        item, kind = rows[i]
        if kind != 'normal':
            continue
        if getattr(item, 'row_bg', None) is not None:
            ctx.cursor_to(item.id)
            return
    ctx.flash('no more voice messages this way')


def _action_next_voice(ctx):
    """Jump cursor down to the next user/assistant text row."""
    _jump_to_voice(ctx, +1)


def _action_prev_voice(ctx):
    """Jump cursor up to the previous user/assistant text row."""
    _jump_to_voice(ctx, -1)


def _latest_voice_among_children(item_id):
    """Latest voice id among the **direct** children of ``item_id``.

    Single-level: returns the id of the latest (last-in-listing-order)
    voice row that would become visible if ``item_id`` were expanded.
    Does NOT recurse into grandchildren. Returns ``None`` if there is
    no voice at this level, or the id doesn't resolve.

    Implemented in terms of ``get_children`` so the umbrella structure
    is honoured uniformly — ``<prompt>`` umbrellas advertise themselves
    as voice via ``row_bg``, ``<tool>`` umbrellas only do so if the
    assistant has text content, regular leaf voice rows match too.
    """
    if not isinstance(item_id, tuple):
        return None
    children = get_children(item_id)
    latest = None
    for it in children:
        if getattr(it, 'row_bg', None) is not None:
            latest = it.id
    return latest


def _first_visible_child(item_id):
    """First **visible** direct child id of ``item_id`` (or ``None``).

    The structural analog of :func:`_latest_voice_among_children` for
    rows whose children carry no ``row_bg`` voice marker — markdown
    document/heading/file nodes. Single-level (``get_children``, so it is
    mode-agnostic and honours the umbrella structure), first-in-listing
    order, and it skips children the detail filter has marked
    ``hidden`` — mirroring the visibility the voice-jump respects (a
    voice row always passes the filter, so the latest-voice lookup never
    lands on a hidden row). Returns ``None`` when there are no children
    or none are visible.
    """
    if not isinstance(item_id, tuple):
        return None
    for it in get_children(item_id):
        if not getattr(it, 'hidden', False):
            return it.id
    return None


def _on_scope_change(ctx, scope_id, prev_scope_id, direction):
    """Scope-change hook: upgrade a cross-file scope_root's preview.

    Registered as the ``on_scope_change`` lifecycle hook, so it fires on
    every runtime scope transition — the ``alt-down`` / ``alt-up`` keys
    and a programmatic ``ctx.scope_into`` / ``ctx.scope_out`` — not just
    one overridden key. (The construction-time ``initial_scope`` is set
    up before the loop starts and does not fire the hook, matching the
    old override, which likewise never ran at startup.)

    On scope-IN (``direction == 'in'``) into a cross-file id, drop the
    cached preview for the new scope_root so the next preview pass
    re-runs ``get_preview`` against the updated ``scope_stack`` and
    upgrades the row from the lightweight metadata card to the heavy
    umbrella cascade. Scope-OUT (``direction == 'out'``) needs no
    invalidation — the row being scoped out of stays as-is, and the new
    (shallower) scope_root was already rendered heavy when it was first
    scoped into.

    This replaces the old ``alt-down`` override (``_action_scope_down``),
    which mirrored the framework's ``scope_into`` internals inline just
    to append this trailing invalidation; the framework default
    ``_scope_down`` now performs the scope push / cursor reset / children
    fetch / filter recompute / redraw / fire, and this hook carries the
    one piece that was recipe-specific.
    """
    if direction == 'in':
        _invalidate_if_cross_file(scope_id)


# Ids whose expand fired while their children were still being
# fetched. ``_on_expand`` parks them here; ``_on_children_loaded``
# drains each one and performs the deferred jump-to-latest-voice once
# the children land. See ``_on_expand`` for the cached-vs-uncached
# composition.
_AWAITING_VOICE_JUMP = set()

# Ids whose next expand's jump-to-latest-voice is suppressed because a
# targeted navigation owns the landing. ``_on_expand`` skips its
# single-level jump for these (immediate AND deferred — a suppressed id
# is never parked in ``_AWAITING_VOICE_JUMP``) so the owner's deeper
# ``cursor_to`` isn't dragged off-target. Two producers, both
# registering BEFORE the owning expand fires its hook:
#
#   * ``_focus_latest_voice_when_ready`` claims the startup session
#     scope_root (the no-``--item`` deferred focus, #720);
#   * ``_chain_expand_then_cursor`` claims each collapsed ancestor it is
#     about to expand (#1243 — the chain's final cursor_to is the
#     destination; cross-link jumps and the t-toggle cursor restore
#     must not lose it to a per-ancestor voice jump).
#
# Consumed (discarded) by ``_on_expand`` as it skips, so a later manual
# collapse + re-expand of the same row still drills in normally.
_VOICE_JUMP_SUPPRESSED = set()


def _jump_to_latest_voice(ctx, item_id):
    """Move the cursor to the latest voice among ``item_id``'s children.

    Single-level: ``_latest_voice_among_children`` reads the direct
    children only, so this lands on the newest voice one level inside —
    drilling further is user-driven. Mode-agnostic since #475 (flat and
    tree both resolve through ``get_children``). No-op when there is no
    voice at this level or the latest voice is the row itself.

    Markdown fallback: a markdown-managed row (a message-with-md, the
    inline ``markdown`` node, a file-doc, or a heading) has no voice
    child — its children are document/heading nodes with no ``row_bg`` —
    so the latest-voice lookup returns ``None`` and the row would feel
    like ``→`` "does nothing". For those rows, land on the FIRST visible
    child instead (the structural analog of "latest voice"), so a single
    ``→`` moves the cursor INTO the subtree just as it does for an
    umbrella. Other rows (umbrellas, sessions) keep their latest-voice
    landing untouched. ``_first_visible_child`` returns ``None`` when the
    row has no children (e.g. a false-positive arrow ``_md_self_heal``
    is about to retract), so nothing moves there.
    """
    latest = _latest_voice_among_children(item_id)
    if latest is None and _is_md_managed_id(item_id):
        latest = _first_visible_child(item_id)
    if latest is not None and latest != item_id:
        ctx.cursor_to(latest)


def _on_expand(ctx, ids):
    """Expand hook: upgrade cross-file previews + jump to latest voice.

    Registered as the ``on_expand`` lifecycle hook, so it fires on the
    collapse→expand transition from EVERY source — the ``→`` key, the
    minimal ``right`` handler's ``ctx.expand`` for a collapsed row, a
    programmatic ``ctx.expand``, and the recipe's own deep-link /
    startup expands — not just keyboard input. For each newly-expanded
    id this:

      * drops the cached preview if the id is cross-file
        (``_invalidate_if_cross_file``), so the row upgrades from the
        lightweight metadata card to the heavy umbrella cascade on the
        next preview pass — the row has just transitioned into the
        active (expanded) state; and
      * lands the cursor on the latest voice among the now-visible
        direct children. Children may not be cached yet at fire time:
        when ``ctx.cached_children(id) is not None`` the children are
        in hand and the jump happens now; otherwise the id is parked
        in ``_AWAITING_VOICE_JUMP`` and ``_on_children_loaded`` does
        the jump once the async fetch settles.

    This replaces the old ``→`` override's ``ctx.expand(id).then(_land)``
    chain, which only fired for an expand the recipe itself issued —
    the hook composition fires for user-driven keyboard expansion too.

    Exception — ids whose landing another owner drives
    (``_VOICE_JUMP_SUPPRESSED``): a targeted navigation ends with its
    own ``cursor_to``, and this hook's single-level jump would race it
    through the post queue and win last, dragging the cursor off the
    intended target. Two owners register their expands there before
    firing them:

      * ``_focus_latest_voice_when_ready`` (#720): the no-``--item``
        startup focus lands the session scope_root on the *deepest*
        latest message; the hook's jump would strand the cursor on a
        top-level ``<prompt>`` umbrella whose heavy streaming preview
        hasn't composed yet (a blank pane until the cascade lands).
      * ``_chain_expand_then_cursor`` (#1243): cross-link jumps and the
        t-toggle cursor restore expand ancestor chains whose children
        are usually uncached — without the claim each ancestor would be
        parked in ``_AWAITING_VOICE_JUMP`` and the deferred jump would
        fire AFTER the chain's final cursor_to (e.g. yanking a backlink
        jump onto whatever voice is newest under the spawn turn).

    We skip the jump for a claimed id — parking included, so the
    deferred ``_on_children_loaded`` jump is suppressed too. The
    cross-file preview upgrade above still applies (an expanding
    boundary row must go heavy). With no owner in flight the set is
    empty and every row keeps its latest-voice landing as before.

    The claim is **consumed once**: we discard the id as we skip it, so
    only the owner's expand is suppressed. A later *manual* collapse +
    re-expand of the same row (``nav-left`` drops it from ``expanded``,
    ``nav-right`` → ``ctx.expand`` fires this hook again) must drill in
    to the latest voice like any other row — a persistent claim would
    suppress that legitimate jump forever. Add-before-the-owning-expand
    ordering guarantees the id is present when the owner's ``_on_expand``
    runs; consuming here is robust whether the owner's continuation
    proceeds or cancels (both ride the same expand resolution).
    """
    for id in ids:
        _invalidate_if_cross_file(id)
        if id in _VOICE_JUMP_SUPPRESSED:
            _VOICE_JUMP_SUPPRESSED.discard(id)
            continue
        if ctx.cached_children(id) is not None:
            _jump_to_latest_voice(ctx, id)
        else:
            _AWAITING_VOICE_JUMP.add(id)


def _on_children_loaded(ctx, parent_ids):
    """Children-loaded hook: fulfil deferred jump-to-latest-voice.

    The source-agnostic completion side of the composition started in
    ``_on_expand``. For each parent whose fetch just settled, if that
    parent was awaiting its post-expand jump, discard it from the
    pending set and land the cursor on the latest voice now that the
    children are cached. Parents that settled for any other reason
    (a background ``refresh`` / tail refetch, or an expand that did not
    request a jump) are simply not in the set, so the cursor is never
    yanked by an arrival the user did not trigger.

    The id is discarded **unconditionally** once seen — a stale entry
    must never linger and trigger a spurious jump on a later settlement.
    But the jump only fires when the parent is **still expanded**: the
    framework settles ``on_children_loaded`` when the fetch lands
    regardless of expanded state, so a node collapsed before its
    in-flight fetch delivered would otherwise have the cursor yanked
    onto a now-hidden child (installing a sticky anchor). This is the
    "recipe checks 'still expanded'" guard the composition pattern
    prescribes.
    """
    for pid in parent_ids:
        if pid in _AWAITING_VOICE_JUMP:
            _AWAITING_VOICE_JUMP.discard(pid)
            if pid in ctx.state.expanded:
                _jump_to_latest_voice(ctx, pid)
        _md_self_heal(ctx, pid)


def _md_self_heal(ctx, parent_id):
    """Retract a false-positive markdown expansion arrow once its build is in.

    Detection (the ``_md_has_children`` gate) and freshly-built file-doc nodes
    are *optimistic* — they arrow without reading the file, so a ``#`` that
    lives only inside a code fence, or a ``.md`` token that doesn't resolve,
    earns an arrow that the authoritative ``get_children`` build then reveals
    as empty. When such a markdown-managed parent (``_is_md_managed_id``)
    settles to NO children, push a ``mod(id, has_children=False)`` so the arrow
    disappears on next render. Other parents (umbrellas, sessions) are never
    md-optimistic, so we leave them untouched — a real umbrella with no visible
    children keeps its arrow as before. No-op when the children aren't cached
    yet or are non-empty, or when there's no browser to push to.
    """
    if _BROWSER is None or not _is_md_managed_id(parent_id):
        return
    kids = ctx.cached_children(parent_id)
    if kids == []:
        ctx.update_data([mod(parent_id, has_children=False)])


def _is_md_managed_id(item_id):
    """True iff ``item_id``'s children are owned by the markdown subtree.

    Markdown-managed = ``md_doc`` is importable AND the id is an ``('md', …)``
    node (inline document, file doc, or heading), a ``[links]`` References umbrella
    (``('refs', …)``), or a plain message id ``('msg', jsonl, n)`` (which gains
    markdown children when the message has a heading or references a ``.md``
    file). These are the rows whose post-expand cursor lands on the first child
    rather than a voice child (``_jump_to_latest_voice``) — so a single ``→``
    on a message-with-md / file-doc / References umbrella drills INTO the
    subtree. The message / file-doc / inline-doc arrows are set *optimistically*
    (so ``_md_self_heal`` may retract one that builds empty); the References
    umbrella is built only when a ref exists, so it never self-heals — but it is
    still md-managed for the first-child landing. Umbrellas (prompt / tool /
    span) and sessions are never md-managed. A no-op (``False``) when
    ``_md_doc is None`` — the whole markdown feature is dead code then.
    """
    if _md_doc is None or not isinstance(item_id, tuple) or not item_id:
        return False
    return item_id[0] in ('md', 'refs', 'msg')


def _action_tree_right(ctx):
    """Right-arrow handler for the already-expanded re-press only.

    The expand itself, its ``autoscroll`` scroll-to-fit goal, and the
    post-expand jump-to-latest-voice are all handled by the framework
    default plus the ``on_expand`` / ``on_children_loaded`` hooks: a
    collapsed row just delegates to ``ctx.expand(id, autoscroll=True)``
    and lets ``_on_expand`` do the cursor jump (for every source, not
    only the keyboard) and the cross-file preview upgrade.

    The one gesture the hooks cannot reproduce is the **re-press**: when
    ``→`` is pressed on a row that is *already* expanded, no state change
    occurs, so ``on_expand`` does not fire. The framework default would
    step the cursor onto the row's FIRST child (oldest, since children
    list chronologically); browse-claude's signature instead drills to
    the LATEST voice one level down. This handler keeps only that case —
    a single ``cursor_to`` — so a second ``→`` walks to the newest reply
    rather than the top of the turn.
    """
    cur = ctx.cursor
    if cur is None:
        return
    if not getattr(cur, 'has_children', False):
        # Non-expandable row: nothing to do (framework default also
        # no-ops here).
        return

    target_id = cur.id
    if target_id in ctx.state.expanded:
        # Already expanded — jump to the latest voice one level down.
        # User can press Right again to drill further; no auto-deep-dive.
        _jump_to_latest_voice(ctx, target_id)
        return

    # Collapsed expandable row: expand with the user-navigation
    # scroll-to-fit goal. ``_on_expand`` performs the post-expand jump
    # (cached now, or after ``_on_children_loaded`` settles) and the
    # cross-file preview invalidation — for this keypress and for every
    # other expansion source alike.
    ctx.expand(target_id, autoscroll=True)


def _action_toggle_tree(ctx):
    """Flip flat ↔ tree view; preserve cursor on the same message id.

    Flat→tree: the cursor's message may end up several levels deep
    inside a turn-root that's collapsed by default. Walk the ``parentUuid``
    chain from the target up to root, add each ancestor to the
    framework's ``expanded`` set, then ``refresh()`` (which refetches the
    root + every expanded parent) and ``cursor_to`` the original id.

    Tree→flat: messages are direct children of the session in flat mode,
    so no ancestors to expand — refresh + cursor_to is enough.
    """
    global _TREE_MODE
    target_id = None
    cur = ctx.cursor
    if cur is not None:
        target_id = cur.id

    _TREE_MODE = not _TREE_MODE
    ctx.flash(f"view: {'tree' if _TREE_MODE else 'flat'}")

    # Compute ancestor chain BEFORE refresh (we need a stable read of
    # the file; refresh doesn't touch the on-disk content).
    ancestors = _ancestor_ids_for(target_id) if target_id else []

    pending = ctx.refresh()
    if target_id:
        # After refresh wipes the cache, chain ancestor expands → cursor.
        # Same reasoning as cursor-on-open: cursor_to needs the row to
        # be visible, which requires each ancestor's children to be
        # cached, which is only guaranteed when each expand's Pending
        # resolves before the next runs.
        pending.then(
            lambda: _chain_expand_then_cursor(
                ctx.browser, ancestors, target_id,
            )
        )


_DETAIL_LEVEL_NAMES = {1: 'summary', 2: 'voice', 3: 'edits',
                       4: 'tools', 5: 'detailed', 6: 'all',
                       7: 'unknown'}
# Word aliases for the ``--detail`` CLI flag, derived from the level
# names so the two never drift (summary=1, voice=2, edits=3, tools=4,
# detailed=5, all=6, unknown=7).
_DETAIL_LEVEL_ALIASES = {name: level for level, name in _DETAIL_LEVEL_NAMES.items()}


def _parse_detail_level(raw):
    """Convert a ``--detail`` value to a level int (1-7).

    Accepts the digits ``'1'``-``'7'`` and the word aliases
    (``summary``/``voice``/``edits``/``tools``/``detailed``/``all``/
    ``unknown``, case-insensitively). Raises ``ValueError`` on anything
    else so ``_pop_value`` surfaces the usage error to ``main``.
    """
    key = raw.strip().lower()
    if key in _DETAIL_LEVEL_ALIASES:
        return _DETAIL_LEVEL_ALIASES[key]
    if key in ('1', '2', '3', '4', '5', '6', '7'):
        return int(key)
    raise ValueError(raw)


def _set_detail_level(ctx, level):
    """Set the detail level absolutely and recompute row visibility.

    Bound to the ``1``-``7`` keys (each an absolute setter, not a
    toggle). Sets ``_DETAIL_LEVEL``, then iterates every currently-
    loaded ``Item`` and emits a single ``mod(id, hidden=...)`` batch
    with one op per row whose target visibility differs from its
    current one. Items not yet loaded (children of collapsed umbrellas)
    get their ``hidden`` set on next expansion — the builders consult
    the live ``_DETAIL_LEVEL`` global.

    Cursor migration when rows hide is handled entirely by the
    framework's post-``apply_ops`` hide-displacement hook (walks back
    to the nearest still-visible row, falls back to the first
    visible). Selection state is preserved as-is; ``ctrl-A``
    (``_select_all_visible``) is already WYSIWYG.
    """
    global _DETAIL_LEVEL
    _DETAIL_LEVEL = level
    ops = []
    for item in ctx.all_items():
        want_hidden = not _passes_filter(item.id)
        if bool(getattr(item, 'hidden', False)) != want_hidden:
            ops.append(mod(item.id, hidden=want_hidden))
    if ops:
        ctx.update_data(ops)
    # Umbrella previews compose from non-hidden children, so every
    # cached preview is potentially stale after a level change. Drop
    # every cached entry; ``drop_preview_cache`` will kick the worker
    # for the current preview cursor and flag a redraw, preserving
    # view state (a user pinned to the tail keeps following).
    ctx.drop_preview_cache()
    ctx.flash(f"detail: {level} - {_DETAIL_LEVEL_NAMES[level]}")
