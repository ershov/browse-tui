# Cache ``<sid> → session .jsonl path`` for the cross-project-dir lookup
# below. Session ids are UUID-unique, so a hit is stable for the life of
# the process; misses aren't cached (a session whose file lands later
# should still be found on a subsequent toggle).
_SESSION_PATH_BY_SID = {}


def _session_path_for_sid(sid):
    """Find ``<CLAUDE_ROOT>/*/<sid>.jsonl`` — the real session for ``sid``.

    A worktree-relocated subagent's ``<sid>`` dir sits under the
    cwd-derived project dir, NOT next to the session ``.jsonl`` (which
    stays under the project dir it was created in). Since session ids
    are UUID-unique, the first ``<sid>.jsonl`` found anywhere under
    ``CLAUDE_ROOT`` is the right one. Returns the path or ``None``.
    """
    cached = _SESSION_PATH_BY_SID.get(sid)
    if cached is not None and os.path.isfile(cached):
        return cached
    name = f'{sid}.jsonl'
    try:
        projects = os.listdir(CLAUDE_ROOT)
    except OSError:
        return None
    for project in projects:
        candidate = os.path.join(CLAUDE_ROOT, project, name)
        if os.path.isfile(candidate):
            _SESSION_PATH_BY_SID[sid] = candidate
            return candidate
    return None


def _outer_subagent_group_id(path):
    """If ``path`` is a subagent's ``.jsonl``, return its outer-row id.

    Subagent transcripts live at::

        <project_dir>/<parent_session_id>/subagents/agent-<aid>.jsonl

    and the parent session shows them as collapsible rows with id
    ``('agent', parent_session_path, aid)``. When the parent session is
    the view, that outer row is the framework-visible parent of every
    record inside the subagent — if it isn't expanded, none of the
    subagent's records are reachable. Used by ``_ancestor_ids_for`` to
    prepend it to the chain so cursor preservation works across
    t-toggle from anywhere inside a subagent. (When the subagent file is
    itself the scope root the row is above the scope;
    ``_trim_chain_to_scope`` drops it again.)

    For a worktree-relocated subagent the session ``.jsonl`` is NOT the
    co-located sibling of the ``<sid>`` dir (it lives in a different,
    cwd-derived project dir), so when the sibling is absent we fall
    back to searching ``<CLAUDE_ROOT>/*/<sid>.jsonl`` for the real one.
    """
    if not isinstance(path, str):
        return None
    parent_dir = os.path.dirname(path)
    if os.path.basename(parent_dir) != 'subagents':
        return None
    name = os.path.basename(path)
    if not name.startswith('agent-') or not name.endswith('.jsonl'):
        return None
    aid = name[len('agent-'):-len('.jsonl')]
    sess_dir = os.path.dirname(parent_dir)
    # Co-located fast path: the legacy layout keeps the session .jsonl
    # next to its ``<sid>`` dir.
    sess_path = sess_dir + '.jsonl'
    if not os.path.isfile(sess_path):
        # Relocated layout: the real session lives in a different
        # project dir — locate it by its (UUID-unique) session id.
        sid = os.path.basename(sess_dir)
        sess_path = _session_path_for_sid(sid)
        if sess_path is None:
            return None
    return ('agent', sess_path, aid)


def _ancestor_ids_for(item_id):
    """Return ids of every ancestor of ``item_id`` (root → direct parent).

    Walks the tree built in ``_scan_tree``. Every leaf in the displayed
    tree is wrapped in at least one umbrella (``prompt`` / ``tool`` /
    ``span``), so the chain consists exclusively of umbrella ids and
    subagent-group ids — never of raw message ids.

    Crosses file boundaries:

      * Records inside a subagent's ``.jsonl`` walk up through their
        in-file umbrellas, then across to the
        ``('agent', parent_session_path, aid)`` subagent group row —
        itself a top-level row in the parent session (the subagents
        block), so the chain ends there.
      * Top-level umbrellas inside a subagent jsonl still nest under
        the outer subagent group — same prepend rule.

    Without this full walk a t-toggle from inside a subagent (where
    refresh wiped the cached children) can't reach the outer chain,
    leaves it un-expanded, and cursor_to fails to find the target row
    in the visible list.
    """
    if not isinstance(item_id, tuple) or not item_id:
        return []
    tag = item_id[0]

    # Subagent group: a top-level row in the parent session's tree
    # (the subagents block above the turn umbrellas) — no ancestors.
    if tag == 'agent':
        return []

    # ``tool`` umbrella: parent is the containing ``prompt``.
    if tag == 'tool':
        path, n = item_id[1], item_id[2]
        if not os.path.isfile(path):
            return []
        td = _scan_tree(path)
        turn_line = td.tool_to_turn_root_line.get(n)
        if turn_line is None:
            # Orphan tool umbrella (shouldn't happen in practice).
            outer = _outer_subagent_group_id(path)
            return (_ancestor_ids_for(outer) + [outer]
                    if outer is not None else [])
        parent_id = ('prompt', path, turn_line)
        return _ancestor_ids_for(parent_id) + [parent_id]

    # ``prompt`` / ``span`` umbrella: top-level in its file, but cross to
    # the outer subagent chain if the file is a subagent jsonl.
    if tag in ('prompt', 'span'):
        path = item_id[1]
        outer = _outer_subagent_group_id(path)
        if outer is None:
            return []
        return _ancestor_ids_for(outer) + [outer]

    # Regular message id (leaf): look up its parent umbrella, then walk
    # up from there.
    if tag == 'msg':
        path, n = item_id[1], item_id[2]
        if not os.path.isfile(path):
            return []
        td = _scan_tree(path)
        parent_id = td.parent_id_of.get(n)
        if parent_id is None:
            # Orphan leaf — likely a top-level metadata record that the
            # scan didn't wrap in a span. Still cross to outer subagent.
            outer = _outer_subagent_group_id(path)
            return (_ancestor_ids_for(outer) + [outer]
                    if outer is not None else [])
        return _ancestor_ids_for(parent_id) + [parent_id]

    return []


# ---- cross-link jumps (#1247, two-way since #1243) -------------------------
#
# Navigation between a session's main thread and its subagent transcripts.
# Rows that participate in a leader↔teammate interaction carry ``[↗]`` in
# their title; Enter jumps across (a menu chooses when a row carries several
# links, e.g. one assistant record spawning multiple agents). Every link is
# one half of a symmetric pair:
#
#   * SESSION level — spawn site (Agent/Task tool_use) ↔ the subagent
#     GROUP row.
#   * MESSAGE level —
#       n-th SendMessage to a teammate    ↔ the teammate's (n+1)-th inbound
#                                           teammate-message line (its 1st
#                                           IS the spawn prompt);
#       inbound teammate-message reply    ↔ the teammate's SendMessage line
#                                           whose message text the reply
#                                           delivered (a content join —
#                                           duplicate texts consumed in
#                                           order; the recipient name the
#                                           worker used is irrelevant);
#       teammate's FIRST inbound line
#       (the spawn prompt delivery)       → the spawn record (its
#                                           message-level counterpart — the
#                                           spawn row itself keeps its one
#                                           forward link, the group row).
#
# The send-direction order join skips auto-generated idle notifications
# (they aren't sent via SendMessage, so they'd shift the join); an order
# join that runs past the end — or a reply whose text matches no send —
# falls back to the subagent group row (no reverse half — the group row's
# backlink is the session-level pair).


def _is_idle_notification(body):
    """True for a teammate's auto-generated idle notice (not a real reply)."""
    body = body.lstrip()
    if not body.startswith('{'):
        return False
    try:
        return json.loads(body).get('type') == 'idle_notification'
    except Exception:
        return False


def _spawn_use_name(rec, tu_id):
    """``input.name`` of the Agent/Task tool_use part ``tu_id`` in ``rec``."""
    msg = rec.get('message') if isinstance(rec, dict) else None
    content = msg.get('content') if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return None
    for p in content:
        if (isinstance(p, dict) and p.get('type') == 'tool_use'
                and p.get('id') == tu_id):
            return (p.get('input') or {}).get('name')
    return None


def _inbound_lines(std):
    """Lines of delivered inbound teammate messages, in order.

    The 1st is the spawn prompt delivery; idle notices are excluded so
    the order join stays aligned with the leader's SendMessage list.
    """
    return [wn for wn, wrec in enumerate(std.records)
            if isinstance(wrec, dict) and wrec.get('type') == 'user'
            and _is_agent_message(wrec)
            and not _is_idle_notification(_parse_agent_message(wrec)[1])]


def _link_map(path):
    """Lazy cross-link map for one session (see the section comment).

    Returns ``{'lines': {master_line: [target_id, …]},
    'agents': {agent_id: spawn_site_id},
    'rev': {(agent_jsonl, agent_line): [master_id, …]},
    'agent_counts': {agent_jsonl: record_count_at_build}}`` cached on
    ``td.link_map``; rebuilt when the record count changed (a tail
    append can add spawn/send/reply rows). Target ids are ordinary row
    ids — ``('msg', jsonl, n)`` or the ``('agent', …)`` group row for
    an unresolvable join — so the jump path reuses the existing
    ancestor/expand machinery.

    ``rev`` holds the reverse half of every message-level pair, keyed
    by the subagent-side line, so rows INSIDE a subagent transcript
    resolve their backlinks. The rebuild check here keys on the MASTER
    count only; subagent transcripts grow independently, so
    ``_links_for_id`` compares ``agent_counts`` against the live count
    of the transcript it is resolving and forces a rebuild on mismatch
    — a stale entry therefore survives only until the master grows or
    the next lookup on a row of the grown transcript (which the
    jump-to-group flow itself causes; self-healing).
    """
    td = _scan_tree(path)
    lm = td.link_map
    if lm is not None and lm['nrecords'] == len(td.records):
        return lm
    lines = {}
    agents = {}
    rev = {}
    agent_counts = {}

    # One master pass: SendMessage dispatch lines per recipient and
    # inbound teammate-message reply ``(line, body)`` per sender (idle
    # notices and queue-operation echoes excluded — only delivered
    # ``user`` records join).
    sends_to = {}
    replies_from = {}
    for n, rec in enumerate(td.records):
        if not isinstance(rec, dict):
            continue
        rtype = rec.get('type')
        if rtype == 'assistant':
            msg = rec.get('message') or {}
            content = msg.get('content') if isinstance(msg, dict) else None
            if isinstance(content, list):
                for p in content:
                    if (isinstance(p, dict) and p.get('type') == 'tool_use'
                            and p.get('name') == 'SendMessage'):
                        recipient, _s, _m = _send_message_fields(
                            p.get('input'))
                        sends_to.setdefault(recipient, []).append(n)
        elif rtype == 'user' and _is_agent_message(rec):
            sender, body = _parse_agent_message(rec)
            if sender and not _is_idle_notification(body):
                replies_from.setdefault(sender, []).append((n, body))

    # Spawn sites + teammate name join. A respawn reuses the name; the
    # latest spawn (by line) owns the send/reply order joins.
    by_name = {}
    for tu_id, info in sorted(
            td.agent_link.items(),
            key=lambda kv: td.agent_to_assistant_line.get(
                kv[1].get('agent_id'), -1)):
        aid = info.get('agent_id')
        agent_path = info.get('agent_path')
        spawn_line = td.agent_to_assistant_line.get(aid)
        if spawn_line is None or not agent_path:
            continue
        if not os.path.isfile(agent_path):
            continue
        std = _scan_tree(agent_path)
        agent_counts[agent_path] = len(std.records)
        # Session-level pair: spawn site ↔ group row (the group row's
        # half lives in ``agents``). The message-level counterpart of
        # the spawn is the teammate's FIRST inbound line — the spawn
        # prompt delivery — which backlinks to the spawn record.
        lines.setdefault(spawn_line, []).append(('agent', path, aid))
        agents[aid] = ('msg', path, spawn_line)
        inbound = _inbound_lines(std)
        if inbound:
            rev.setdefault((agent_path, inbound[0]), []).append(
                ('msg', path, spawn_line))
        rec = td.records[spawn_line]
        name = _spawn_use_name(rec, tu_id)
        if name:
            by_name[name] = (aid, agent_path, std, spawn_line, inbound)

    for name, (aid, agent_path, std, spawn_line, inbound) in by_name.items():
        group_id = ('agent', path, aid)
        # Teammate-side SendMessage lines with their message text. A
        # reply joins the send whose text it delivered — stripped, as
        # ``_parse_agent_message`` strips the body. No recipient filter:
        # the name a worker addresses ("main", "team-lead", …) need not
        # match the name the leader goes by, and sends to OTHER peers
        # simply never match a leader-side reply body.
        wsends = []
        for wn, wrec in enumerate(std.records):
            if not (isinstance(wrec, dict)
                    and wrec.get('type') == 'assistant'):
                continue
            msg = wrec.get('message') or {}
            content = msg.get('content') if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for p in content:
                if (isinstance(p, dict) and p.get('type') == 'tool_use'
                        and p.get('name') == 'SendMessage'):
                    _r, _s, wmsg = _send_message_fields(p.get('input'))
                    wsends.append(
                        (wn, wmsg.strip() if isinstance(wmsg, str) else ''))
        # Only traffic AFTER the owning spawn joins — an earlier
        # incarnation of a reused name exchanged its own messages, and
        # walking those into this transcript would link them to the
        # wrong records. They carry no link instead. Each resolved join
        # also records its reverse half; a group-row fallback does not.
        for k, mline in enumerate(
                [n for n in sends_to.get(name, []) if n > spawn_line]):
            if k + 1 < len(inbound):
                tgt = ('msg', agent_path, inbound[k + 1])
                rev.setdefault(tgt[1:], []).append(('msg', path, mline))
            else:
                tgt = group_id
            lines.setdefault(mline, []).append(tgt)
        used = set()
        for mline, body in [nb for nb in replies_from.get(name, [])
                            if nb[0] > spawn_line]:
            hit = next((i for i, (_wn, wmsg) in enumerate(wsends)
                        if i not in used and wmsg == body), None)
            if hit is not None:
                used.add(hit)
                tgt = ('msg', agent_path, wsends[hit][0])
                rev.setdefault(tgt[1:], []).append(('msg', path, mline))
            else:
                tgt = group_id
            lines.setdefault(mline, []).append(tgt)

    lm = {'nrecords': len(td.records), 'lines': lines, 'agents': agents,
          'rev': rev, 'agent_counts': agent_counts}
    td.link_map = lm
    return lm


def _links_for_id(item_id):
    """Cross-link targets for a row; ``[]`` when it carries none.

    A leaf and the umbrella wrapping it share the record line, so both
    resolve the same links (``('msg'|'tool'|'prompt', jsonl, line)``).
    The ``('agent', …)`` group row resolves its spawn-site backlink.
    Rows inside a subagent transcript resolve through the OWNING master
    session's reverse index; when this transcript grew since that map
    was built (the master's own rebuild check only watches the master
    count) the map is rebuilt first, so worker-side growth self-heals
    both directions on the next lookup here.
    """
    if not isinstance(item_id, tuple) or len(item_id) != 3:
        return []
    tag, path, key = item_id
    if not isinstance(path, str) or not os.path.isfile(path):
        return []
    if tag == 'agent':
        tgt = _link_map(path)['agents'].get(key)
        return [tgt] if tgt else []
    if tag not in ('msg', 'tool', 'prompt') or not isinstance(key, int):
        return []
    links = list(_link_map(path)['lines'].get(key, ()))
    outer = _outer_subagent_group_id(path)
    if outer is None or not os.path.isfile(outer[1]):
        return links
    lm = _link_map(outer[1])
    count = len(_scan_tree(path).records)
    if lm['agent_counts'].get(path, count) != count:
        _scan_tree(outer[1]).link_map = None
        lm = _link_map(outer[1])
    return links + list(lm['rev'].get((path, key), ()))


def _link_marker(item_id):
    """``'[↗] '`` title prefix for rows that carry cross-links."""
    return '[↗] ' if _links_for_id(item_id) else ''


def _link_label(target_id):
    """Human menu label for one jump target."""
    if target_id[0] == 'agent':
        return f'↗ subagent {target_id[2]}'
    path, n = target_id[1], target_id[2]
    name = os.path.basename(path)
    if name.startswith('agent-') and name.endswith('.jsonl'):
        name = name[len('agent-'):-len('.jsonl')]
    td = _scan_tree(path)
    rec = td.records[n] if 0 <= n < len(td.records) else None
    what = _oneline(_summarise_message(rec)) if rec else f'line {n + 1}'
    if len(what) > 60:
        what = what[:59] + '…'
    return f'↗ {name}:{n + 1}  {what}'


def _link_menu_rows(item_id):
    """``(label, ('link.jump', target_id))`` menu rows for a row's links."""
    return [(_link_label(t), ('link.jump', t))
            for t in _links_for_id(item_id)]


def _jump_to_link(ctx, target_id):
    """Reveal + move: expand the target's ancestors, then ``cursor_to``.

    Tree mode walks the full umbrella chain (crossing into a subagent's
    group row when the target lives in its .jsonl); flat mode only needs
    the group row expanded for a cross-file target. Either chain is
    trimmed to the current scope by ``_chain_expand_then_cursor``. A target the active
    detail level hides can't take the cursor — land on its nearest
    filter-visible ancestor instead (e.g. the wrapping ``<prompt>``).
    """
    if _TREE_MODE:
        ancestors = _ancestor_ids_for(target_id)
    else:
        outer = (_outer_subagent_group_id(target_id[1])
                 if target_id[0] == 'msg' else None)
        ancestors = [outer] if outer else []
    chain = ancestors + [target_id]
    last = len(chain) - 1
    while last > 0 and not _passes_filter(chain[last]):
        last -= 1
    _chain_expand_then_cursor(ctx, chain[:last], chain[last])


def _on_enter_jump(ctx):
    """Enter: follow the cursor row's cross-link.

    One link jumps straight there; several open a menu (an assistant
    record can spawn/message multiple teammates). Rows without links
    keep the silent no-op.
    """
    item = ctx.cursor
    if item is None:
        return
    links = _links_for_id(item.id)
    if not links:
        return
    if len(links) == 1:
        _jump_to_link(ctx, links[0])
        return
    choice = ctx.menu(_link_menu_rows(item.id))
    if (isinstance(choice, tuple) and len(choice) == 2
            and choice[0] == 'link.jump'):
        _jump_to_link(ctx, choice[1])


def _seed_scope_root_item(b, target_jsonl):
    """Register a REAL session Item for the initial scope_root (#730).

    On direct launch (``browse-claude <file.jsonl>`` / ``--file`` / etc.)
    the framework scopes straight into the ``('session', target_jsonl)``
    id before its children — or the parent dir's listing — have been
    fetched, so the scope-root row has no Item in the index.
    ``visible_items`` then fabricates a *synthetic* placeholder
    (``boundary`` unset), and ``_is_cross_file_id``'s tag fallback still
    classifies it cross-file → the cascade upgrade keeps working, but
    without the rich session Item the scope-header row has no metadata.

    Seed the rich session Item (id, full-path scope_title, msg/sub-count
    tag, ``boundary=True``, ``kind='session'``) into ``_items_by_id`` so
    ``visible_items`` finds a real Item instead of synthesizing one — its
    scope-row lookup falls back to ``_items_by_id`` when the Item isn't
    yet a child of any cached listing, which is exactly the case here.

    Seed ONLY ``_items_by_id`` — deliberately NOT
    ``_children[parent_scope]``. Pre-populating the parent project's
    children list with this single session would make the framework treat
    that listing as already cached, so a later scope-up (``_scope_up`` →
    ``_ensure_children_fetched``) would skip the real
    ``_list_sessions(parent_scope)`` fetch and show only this one session
    instead of all of the project's. Leaving ``_children[parent_scope]``
    unset lets that lazy fetch run on scope-up (and the per-scope-level
    re-fetch on refresh), which populates ``_children`` + ``_parent_of_id``
    and overwrites this by-id seed with the freshly-listed Item.

    Placed directly in the index — NOT via ``update_data``. That's
    deliberate: the ``update_data`` apply path snapshots ``visible_items``
    *before* applying its ops, which would fabricate the synthetic stub
    first and then merely patch it (leaving ``synthetic=True`` on the
    row). A direct seed before ``run()`` puts the real Item in the index
    ahead of any ``visible_items`` pass, so no placeholder is ever
    synthesized. This mirrors the recipe's existing direct ``scope_stack``
    manipulation at startup (just below).

    No-op when the Item can't be built (file vanished, ``--running`` with
    no live process) — the synthetic placeholder then stands and
    ``_is_cross_file_id``'s id-tag fallback keeps the preview correct.
    """
    item = _session_item(target_jsonl)
    if item is None:
        return
    b._state._items_by_id[item.id] = item


def _trim_chain_to_scope(scope_stack, ancestor_ids):
    """Drop the ancestors that sit at or above the current scope root.

    ``_ancestor_ids_for`` walks the data structure, so a record inside a
    subagent ``.jsonl`` always gets the outer ``('agent', S, aid)`` group
    row (a top-level row of the parent session ``S``) prepended. When the
    subagent file is itself the scope root — opened directly, scoped
    into one of its umbrellas, or listed from its ``subagents/`` dir as
    a project — that row is not in the displayed tree. Expanding it
    lists the same ids as the scope root and the framework's
    single-parent model reparents them away, emptying the visible list.

    Two cuts:

      * the scope id itself appears in the chain → keep only what follows
        it (scoped into the subagent group row);
      * a leading agent group row is kept only when its session ``S`` is
        part of the view — ``('session', S)`` on the scope stack, or the
        scope is the project dir that lists ``S``. Otherwise it is above
        the scope and is dropped. Every other id shape has a single
        parent, so an out-of-scope expand of it is harmless.
    """
    chain = list(ancestor_ids)
    if not scope_stack or not chain:
        return chain
    scope_id = scope_stack[-1]
    if scope_id in chain:
        return chain[chain.index(scope_id) + 1:]
    head = chain[0]
    if isinstance(head, tuple) and head[:1] == ('agent',):
        sess = head[1]
        if (('session', sess) not in scope_stack
                and scope_id != ('project', os.path.dirname(sess))):
            return chain[1:]
    return chain


def _chain_expand_then_cursor(b, ancestor_ids, target_id):
    """Sequentially expand ``ancestor_ids`` (root → leaf), then ``cursor_to``.

    Each ``expand`` resolves only after its children are cached, which
    is exactly when the next ``expand`` can read them. Without this
    chaining the expands fire in parallel, the cursor_to runs before
    any of them populate the visible list, and cursor_to no-ops
    (leaving the cursor on the scope_root). Used for cursor-on-open,
    the t-toggle's walk-up-and-expand path, and cross-link jumps.

    Every expand issued here belongs to a targeted navigation whose
    destination is the final ``cursor_to`` — so each one claims its
    landing in ``_VOICE_JUMP_SUPPRESSED`` before firing (#1243).
    Otherwise ``_on_expand``'s jump-to-latest-voice (deferred via
    ``_AWAITING_VOICE_JUMP`` when children aren't cached yet, and then
    posted from the children-loaded hook AFTER this chain's cursor_to)
    would drag the cursor off the target. Only a still-collapsed id is
    claimed: an already-expanded one never re-fires ``on_expand``, so
    its claim would go stale and eat a later manual re-expand's jump.

    The chain is first trimmed to what lies below the current scope
    root (``_trim_chain_to_scope``) — an ancestor above the scope is not
    in the displayed tree, and expanding it can steal the scope's rows.

    ``b`` is a Browser or a Context — both expose ``expand`` /
    ``cursor_to``; the expanded-set read handles either shape.
    """
    state = b._state if hasattr(b, '_state') else b.state
    ancestor_ids = _trim_chain_to_scope(state.scope_stack, ancestor_ids)
    if not ancestor_ids:
        b.cursor_to(target_id)
        return
    def _step(i):
        if i >= len(ancestor_ids):
            b.cursor_to(target_id)
            return
        if ancestor_ids[i] not in state.expanded:
            _VOICE_JUMP_SUPPRESSED.add(ancestor_ids[i])
        b.expand(ancestor_ids[i], autoscroll=True).then(lambda: _step(i + 1))
    _step(0)


def _focus_latest_voice_when_ready(b, target_jsonl):
    """Defer the cursor-on-open jump until scope_root's children land.

    Replaces the pre-``run()`` synchronous ``_last_voice_id`` precompute
    + chain, which forced a 1.6s full-file ``_scan_tree`` on the main
    thread and raced the initial fetch's FIFO ordering for huge
    transcripts.

    Algorithm:
      1. Wait for ``b.expand(target_jsonl).then(...)`` to fire —
         scope_root's children landed via ``_do_initial_fetch``'s
         standard delivery, which means ``_scan_tree`` has run on the
         children-worker thread and ``td.latest_voice_line`` is
         populated.
      2. Check the cursor's *current* position. If the user has moved
         off the scope_root row, leave them alone — they navigated
         while the file was loading and a surprise jump would be
         disorienting.
      3. Otherwise, jump (via ``_jump_to_link``) to the latest voice
         VISIBLE at the current detail level — the absolute latest can
         be a filter-hidden mid-turn text, on which ``cursor_to`` would
         silently no-op and strand the cursor on the scope row.

    The cancel-on-user-moved check uses the live cursor instead of an
    ``on_cursor_change`` subscription: the framework places the cursor
    on the scope row atomically before any user input could land, so
    "cursor still on the scope row at fire time" is a reliable proxy
    for "user hasn't touched it yet."
    """
    scope_id = ('session', target_jsonl)

    def _fire():
        last_id = _last_visible_voice_id(target_jsonl)
        if not last_id:
            return
        # Cancel if the user has navigated since startup. Post scope-
        # root unification the scope row is a normal row at depth 0
        # whose item id is ``('session', target_jsonl)``; identify it
        # that way rather than by the (deprecated) ``kind='scope_root'``.
        state = b._state
        vis = visible_items(state)
        cur = state.cursor
        if not (0 <= cur < len(vis) and vis[cur].item.id == scope_id):
            return
        # ``_jump_to_link`` owns the reveal: ancestor chain in tree
        # mode (empty in flat mode, where the leaf sits under the
        # scope_root), plus the trim to the nearest filter-visible row
        # for the all-voices-hidden fallback.
        _jump_to_link(b, last_id)
    # Claim the scope_root's cursor-on-open landing so the generic
    # ``on_expand`` jump steps aside for the deeper focus below (#720).
    # Registered before the owning ``expand`` fires its hook.
    _VOICE_JUMP_SUPPRESSED.add(scope_id)
    b.expand(scope_id).then(_fire)


def _is_cross_file_id(item_id):
    """True iff ``item_id`` anchors a *different* file's content.

    Cross-file rows render a lightweight metadata preview while the
    row is in the parent listing; the heavy cascade only fires when
    the user commits (scope_root or expanded inline). Returning True
    tells :func:`_invalidate_if_cross_file` to drop the cached
    preview whenever the row transitions into the active state, so
    the next preview pass picks up the heavy cascade instead of the
    stale metadata.

    Attribute-based since the ``boundary`` migration: a row is
    cross-file iff its loaded Item carries ``boundary=True`` — set on
    subagent-group ``('agent', …)`` rows and ``('session', …)`` rows
    (exactly the shapes the old id test matched), plus md *file*-doc
    nodes (which never reach this path — ``get_preview`` routes ``md``
    ids out before the cross-file check). When the Item isn't loaded
    (``get_item`` → ``None``: no live Browser, a Browser without the
    lookup, or a row referenced before its fetch) we fall back to the
    id-tag predicate, so behaviour is identical for ``agent`` /
    ``session`` rows regardless of load state.

    A SYNTHETIC placeholder (``synthetic=True``) is treated like "not
    loaded" too: the engine fabricates one for a scope-root id with no
    real Item yet (``boundary`` defaults to ``False`` on it), and trusting
    that flag would mis-classify a ``('session', …)`` scope_root as in-file
    and blank its preview (#730). Falling through to the id-tag test
    keeps the cascade upgrade working until a real Item lands.
    """
    if not isinstance(item_id, tuple) or not item_id:
        return False
    get_item = getattr(_BROWSER, 'get_item', None)
    item = get_item(item_id) if get_item is not None else None
    if item is not None and not getattr(item, 'synthetic', False):
        return bool(getattr(item, 'boundary', False))
    # Fallback (Item not loaded, or a synthetic placeholder): the
    # id-tag test, so the cheap-preview / cascade-upgrade contract holds
    # even before the row's real Item exists in the index.
    return item_id[0] in ('agent', 'session')


def _invalidate_if_cross_file(item_id):
    """Drop the cached preview for ``item_id`` if it's cross-file.

    Called from the ``on_expand`` and ``on_scope_change`` hooks so the
    next preview pass re-runs ``get_preview`` against the updated state
    (expanded set / scope_stack) and upgrades the lightweight
    metadata card to the heavy umbrella cascade.
    """
    if _BROWSER is None:
        return
    if _is_cross_file_id(item_id):
        _BROWSER.invalidate_preview(item_id)
