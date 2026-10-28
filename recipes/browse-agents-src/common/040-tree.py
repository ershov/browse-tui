# ---- tree builder (#299) -------------------------------------------------
#
# Tree mode rebuilds the in-session children listing as an actual tree
# instead of a reverse-time flat list. The structure follows ``parentUuid``
# (the only field that gives reliable chain integrity across every
# record type — see /promptId investigation notes), with one wrinkle:
# user prompts get their own ``promptId``; when a child's
# ``inheritedPromptId`` (= own pid OR parent's pid) differs from its
# parent's, the parent link is broken and the child becomes a turn-root
# at the top level. That nests each turn cleanly under its prompt while
# keeping all the non-voice metadata records as siblings at root.
#
# The scan is cached; subsequent calls within one session are O(1).
# Cache eviction is explicit — ``get_children(None)`` (Ctrl-R /
# ``ctx.refresh()``) clears ``_TREE_CACHE`` + ``_TAIL_STATE``; the
# live-tail worker keeps the cached ``_TreeData`` in sync with the
# file as it grows, so no time-based invalidation is needed.


_TREE_CACHE = {}   # path → TreeData

# Per-path mutex so two threads (children worker + preview worker) can't
# both fully-scan the same huge transcript concurrently. Without this,
# the cursor-on-open path's chain has to wait for whichever scan finishes
# *last* under GIL contention — empirically ~doubles the wait on a 77MB
# file. With the mutex, the first thread runs the scan, the second
# blocks briefly, then sees the populated _TREE_CACHE and returns.
_SCAN_LOCKS = {}            # path → threading.Lock
_SCAN_LOCKS_LOCK = threading.Lock()
_TREE_MODE = True  # default; --no-tree CLI flag or 't' action flips it.
# Detail level: a record is visible iff its *minimum* level (see
# ``_record_min_level``) is <= this. 1=summary, 2=voice, 3=edits,
# 4=tools, 5=detailed, 6=all (every known kind), 7=unknown (adds
# unrecognised record kinds — a maintenance view). Default 1 (summary
# — user prompts + end_turn responses). Set by the --detail CLI flag
# or changed live with the '1'-'7' key bindings.
_DETAIL_LEVEL = 1


_TREE_METADATA_TYPES = frozenset([
    # records that show up as their own roots — they carry no
    # parentUuid and don't participate in the conversation chain.
    'summary', 'ai-title', 'custom-title', 'last-prompt', 'task-summary',
    'tag', 'agent-name', 'agent-color', 'agent-setting', 'pr-link',
    'mode', 'permission-mode', 'worktree-state', 'content-replacement',
    'file-history-snapshot', 'attribution-snapshot', 'speculation-accept',
    'queue-operation', 'marble-origami-commit', 'marble-origami-snapshot',
    'progress',
])


class _TreeData:
    """Cached tree-structure view of one ``.jsonl`` file.

    The shape is span-based and ignores ``parentUuid`` for structure.
    ``parentUuid`` is just a write-time linked-list back-pointer (see
    ``sessionStorage.ts:1066`` in claude-code: ``parentUuid =
    message.uuid`` after every chain-participant append). Using it
    literally as a tree parent produces 15-deep cascades of attachments
    under tool_results under attachments. The actual conversation
    structure is way flatter:

      * The session is a flat sequence of "turns" interleaved with
        runs of out-of-turn metadata.
      * A turn opens at a "user voice" record (user record carrying
        text content, not a tool_result; not ``isMeta``) and closes at
        the next ``system/turn_duration`` record (or at the next user
        voice, whichever comes first).
      * Everything between open/close is a turn member.
      * Anything outside a turn — preamble, gaps between turns, file
        tail — gets folded into a synthetic ``<system>`` umbrella span
        so the top level doesn't blow up with one row per metadata
        record.

    Inside a turn, one semantic relationship is nested:

      * tool execution group: an assistant record with a ``tool_use``
        content part becomes the parent of (a) the matching user
        tool_result (paired via ``tool_use_id``) and (b) any attachment
        records carrying a matching ``toolUseID`` (PreToolUse/
        PostToolUse hook outputs).

    Fields:

    * ``records``         — parsed records in file order, indices
                            align with line numbers.
    * ``by_uuid``         — ``{uuid: record}``.
    * ``line_of``         — ``{uuid: line_number}``.
    * ``roots_in_order``  — list of root-entry dicts in file order:
                            ``{'kind':'turn','rec':<r>,'line':n}`` or
                            ``{'kind':'span','start':n,'records':[…]}``.
    * ``turn_direct``     — ``{turn_root_uuid: [recs]}`` — turn members
                            that render directly under the turn (i.e.,
                            NOT records that got re-parented under a
                            tool_use as part of the tool execution group).
    * ``tool_children``   — ``{assistant_uuid: [recs]}`` — the
                            tool_result + hook attachments + isMeta
                            back-pointers that pair with this assistant
                            record's tool_use part. Drives the second
                            layer of nesting inside a turn.
    * ``span_records``    — ``{span_start_line: [recs]}`` — members of
                            each ``<system>`` umbrella.
    * ``agent_link``      — ``{tool_use_id: {agent_id, agent_path,
                            assistant_uuid}}`` for every Agent/Task
                            tool_use whose tool_result resolved a
                            subagent .jsonl on disk.
    * ``assistant_has_tool_use`` — ``{assistant_uuid}`` set; True iff the
                            assistant record at this line carries a
                            ``tool_use`` content part (i.e. it should be
                            wrapped in a ``<tool:Name>`` umbrella).
    * ``tool_to_turn_root_line`` — ``{assistant_line: turn_root_line}``
                            — maps a ``<tool:>`` umbrella to its
                            containing ``<prompt>`` umbrella.
    * ``agent_to_assistant_line`` — ``{agent_id: assistant_line}`` —
                            maps a ``<subagent>`` group to its
                            dispatch site (the dispatching
                            assistant's line).
    * ``tool_use_name_of`` — ``{assistant_uuid: tool_name}`` — first
                            tool_use part's name (for the umbrella
                            title prefix).
    * ``line_offsets``    — ``[int]`` with one **sentinel** entry past
                            the last record. ``line_offsets[n]`` is the
                            byte position where line ``n`` starts in
                            the source file; the length of line ``n``
                            is ``line_offsets[n+1] - line_offsets[n]``.
                            Used by the ``V`` / ``E`` actions to
                            extract per-line bytes without re-reading
                            the whole file.

    Per-record attrs:
      * ``rec['_tree_forked_from']`` — record explicitly forked from
        an earlier uuid (``forkedFrom`` field).
    """

    __slots__ = ('records', 'by_uuid', 'line_of', 'roots_in_order',
                 'turn_direct', 'tool_children', 'span_records',
                 'agent_link', 'records_line', 'parent_id_of',
                 'assistant_has_tool_use', 'tool_to_turn_root_line',
                 'agent_to_assistant_line', 'tool_use_name_of',
                 'line_offsets', 'voice_count', 'latest_voice_line',
                 'ai_title', 'custom_title', 'link_map')

    def __init__(self):
        self.records = []
        self.by_uuid = {}
        self.line_of = {}
        self.roots_in_order = []
        self.turn_direct = {}
        self.tool_children = {}
        self.span_records = {}
        self.agent_link = {}
        # id(rec) → line_no — looking up the line of a record without
        # needing a uuid (metadata records often have none).
        self.records_line = {}
        # line_no → parent_id — the parent **umbrella** id for that
        # line's leaf row. One of: ``('prompt', jsonl, n)``,
        # ``('tool', jsonl, n)``, or ``('span', jsonl, n)``. Top-level
        # rows (the umbrellas themselves) are absent — their parent
        # link lives in tool_to_turn_root_line / agent_to_assistant_line
        # or is None.
        self.parent_id_of = {}
        # Assistant records that carry at least one tool_use content
        # part. Drives the ``<tool:Name>`` umbrella wrapping.
        self.assistant_has_tool_use = set()
        # ``<tool:>`` → its containing ``<prompt>`` umbrella.
        self.tool_to_turn_root_line = {}
        # ``<subagent>`` → its dispatching assistant's line (the
        # dispatch site; drives top-block ordering).
        self.agent_to_assistant_line = {}
        # assistant_uuid → name of first tool_use content part, for the
        # ``<tool:Name>`` umbrella's title prefix.
        self.tool_use_name_of = {}
        # Byte offsets of each line, with a sentinel past the end so
        # ``line_offsets[n+1] - line_offsets[n]`` always gives line n's
        # length. Starts as ``[0]`` (sentinel-only, no lines yet) and
        # grows by one entry per appended line.
        self.line_offsets = [0]
        # Cached count of voice-bearing records — populated lazily by
        # ``_fmt_scope_card`` and invalidated by tail appends that
        # bring in new voice records.
        self.voice_count = None
        # Line number of the most-recent voice record seen during the
        # scan (None if no voice record yet). Updated eagerly inside
        # the scan loop so consumers don't have to re-walk records[].
        # Used by the cursor-on-open path to land on the latest voice
        # without an upfront synchronous full-file pass on the main
        # thread.
        self.latest_voice_line = None
        # Session titles, captured during the fold (ai-title records
        # are re-generated as the session grows — the last one wins;
        # custom-title is the user's explicit rename). Tail appends
        # keep them current.
        self.ai_title = None
        self.custom_title = None
        # Lazy cross-link map (built by ``_link_map`` on first use,
        # invalidated by record growth — see its docstring).
        self.link_map = None


def _is_inbound_agent_voice(rec):
    """True iff ``rec`` is inbound agent voice — a worker's
    ``<task-notification>`` reply or a peer session's message. Both are
    ``user``-shaped records that nobody typed."""
    return _is_task_notification(rec) or _is_agent_message(rec)


def _is_turn_root(rec):
    """A turn root is a fresh user prompt — text content, not synthetic.

    Inbound agent voice is excluded even though it is user-shaped voice:
    a worker's reply or a peer's message belongs to the human request
    that spawned it, so ``_fold_record`` re-opens the last human turn
    for it instead of letting it masquerade as a new prompt (falling
    back to rooting only when the file has no prior turn).
    """
    if not isinstance(rec, dict):
        return False
    if rec.get('type') != 'user':
        return False
    if rec.get('isMeta'):
        return False
    return _is_voice(rec) and not _is_inbound_agent_voice(rec)


def _is_turn_close(rec):
    """``system/turn_duration`` is the post-turn marker."""
    return (isinstance(rec, dict)
            and rec.get('type') == 'system'
            and rec.get('subtype') == 'turn_duration')


def _is_continuation_activity(rec):
    """Leader activity that can follow a ``turn_duration`` close.

    Newer leaders keep working after the turn closes (autonomous
    continuations, marked by ``atis-latch`` records) with no new user
    prompt; ``turn_duration`` can even land between a tool_use and its
    tool_result. Such activity — an assistant record, or a user record
    carrying tool_result parts — belongs to the request that started
    it, so ``_fold_record`` re-opens the last turn for it. Pure
    metadata records (mode, last-prompt, atis-latch, ai-title,
    file-history-snapshot, ...) are not activity and keep folding into
    between-turn spans.
    """
    t = rec.get('type')
    if t == 'assistant':
        return True
    if t != 'user' or rec.get('isMeta'):
        return False
    msg = rec.get('message')
    content = msg.get('content') if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return False
    return any(isinstance(p, dict) and p.get('type') == 'tool_result'
               for p in content)


class _ScanCursor:
    """Mutable per-file state-machine cursor for the bulk + tail fold.

    Holds the open-turn / open-span context plus the per-turn
    ``tool_use_id → assistant_uuid`` registry so each newly-arrived
    record can be classified online (no second pass over a buffered
    turn). Persists in ``_TAIL_STATE`` between worker ticks so a
    record arriving in tick 2 sees the same registry the bulk scan
    or tick 1 was building.
    """

    __slots__ = ('turn_open_uuid', 'tool_owner', 'span_start',
                 'last_turn_uuid')

    def __init__(self):
        self.turn_open_uuid = None
        # tool_use_id → owning assistant_uuid (within the open turn).
        # Cleared on turn close.
        self.tool_owner = {}
        # Line number of the first record of the currently-open span,
        # or None when not inside a span.
        self.span_start = None
        # uuid of the most recent turn root — survives turn close so
        # inbound agent voice or continuation activity arriving between
        # turns can re-open it.
        self.last_turn_uuid = None


def _fold_record(td, cur, rec, n, path):
    """Fold one record at line ``n`` into ``td`` using ``cur``.

    Returns a set of parent umbrella ids whose visible child list grew
    (caller turns these into ``update_data`` patches). Bulk scan
    ignores the return value; the tailer consumes it.
    """
    dirty = set()
    if rec is None:
        return dirty
    if not isinstance(rec, dict):
        return dirty

    # Basic record bookkeeping (parallels the bulk scan's first pass).
    td.records_line[id(rec)] = n
    uuid = rec.get('uuid')
    if uuid:
        td.by_uuid[uuid] = rec
        td.line_of[uuid] = n
    if rec.get('forkedFrom'):
        rec['_tree_forked_from'] = rec['forkedFrom']
    # Session titles — fold order is file order, so the last one wins.
    if rec.get('type') == 'ai-title':
        td.ai_title = rec.get('aiTitle')
    elif rec.get('type') == 'custom-title':
        td.custom_title = rec.get('customTitle')

    is_root = _is_turn_root(rec)

    # --- inbound agent voice with no open turn: the reply belongs to
    # the human request that spawned it, so re-open the last turn — the
    # reply AND the leader's handling of it (until the next
    # turn_duration) fold under that prompt. Post-``turn_duration``
    # continuation activity (``_is_continuation_activity``) re-opens
    # the same way. A file with no prior turn (resumed/truncated
    # transcript) falls back: voice roots itself, keeping the
    # <reply>/<peer> umbrella; activity falls through to the span path.
    if (not is_root and cur.turn_open_uuid is None
            and (_is_inbound_agent_voice(rec)
                 or _is_continuation_activity(rec))):
        if (cur.last_turn_uuid is not None
                and cur.last_turn_uuid in td.turn_direct):
            # Close any open between-turns span (mirrors the turn-root
            # branch) — otherwise it would leak across the re-opened
            # turn and swallow post-turn records into one umbrella.
            cur.span_start = None
            cur.turn_open_uuid = cur.last_turn_uuid
            cur.tool_owner = {}
        elif _is_inbound_agent_voice(rec):
            is_root = True

    # --- turn root: opens a new <prompt> umbrella, closes any open span.
    if is_root:
        if cur.span_start is not None:
            # Span closed by this turn root; the records list stored
            # in td.span_records is live-shared with td.roots_in_order
            # so no copy needed here.
            cur.span_start = None
        cur.turn_open_uuid = uuid
        cur.last_turn_uuid = uuid
        cur.tool_owner = {}
        if uuid is not None:
            td.turn_direct[uuid] = []
        td.roots_in_order.append({'kind': 'turn', 'rec': rec, 'line': n})
        td.parent_id_of[n] = ('prompt', path, n)
        dirty.add(('session', path))   # new root-level umbrella
        return dirty

    # --- record inside an open turn.
    if cur.turn_open_uuid is not None:
        turn_root_line = td.line_of.get(cur.turn_open_uuid)
        turn_uuid = cur.turn_open_uuid

        # (a) Assistant with one or more tool_use parts. Register each
        # tool_use id → assistant uuid, mark the assistant as
        # ``<tool>``-wrapped, and place it as a direct child of the
        # turn (the umbrella around it lives at the listing layer).
        if rec.get('type') == 'assistant':
            msg = rec.get('message') or {}
            content = msg.get('content') if isinstance(msg, dict) else None
            has_tool_use = False
            if isinstance(content, list):
                for p in content:
                    if (isinstance(p, dict)
                            and p.get('type') == 'tool_use'
                            and p.get('id')):
                        cur.tool_owner[p['id']] = uuid
                        if uuid:
                            td.assistant_has_tool_use.add(uuid)
                            td.tool_use_name_of.setdefault(
                                uuid, p.get('name') or 'tool')
                        has_tool_use = True
            if has_tool_use:
                td.turn_direct[turn_uuid].append(rec)
                td.parent_id_of[n] = ('tool', path, n)
                if turn_root_line is not None:
                    td.tool_to_turn_root_line[n] = turn_root_line
                    dirty.add(('prompt', path, turn_root_line))
                if _is_turn_close(rec):
                    cur.turn_open_uuid = None
                    cur.tool_owner = {}
                return dirty

        # (b) Tool-child resolution: tool_result / hook attachment /
        # sourceToolUseID back-pointers.
        owner = _resolve_tool_owner(rec, cur.tool_owner)

        if owner is not None:
            owner_line = td.line_of.get(owner)
            td.tool_children.setdefault(owner, []).append(rec)
            if owner_line is not None:
                td.parent_id_of[n] = ('tool', path, owner_line)
                dirty.add(('tool', path, owner_line))
            # Subagent-link check: a tool_result whose
            # toolUseResult.agentId resolves an agent jsonl on disk.
            # A new link means a new row in the session's top block —
            # dirty the root so it gets re-listed.
            if _maybe_link_subagent(td, rec, owner, path):
                dirty.add(('session', path))
        else:
            td.turn_direct[turn_uuid].append(rec)
            if turn_root_line is not None:
                td.parent_id_of[n] = ('prompt', path, turn_root_line)
                dirty.add(('prompt', path, turn_root_line))

        if _is_turn_close(rec):
            cur.turn_open_uuid = None
            cur.tool_owner = {}
        return dirty

    # --- outside any turn: span member. Subagent linking is
    # turn-independent — a spawn tool_result landing here (no prior
    # turn to re-open, e.g. a truncated transcript) still links via
    # its ``sourceToolAssistantUUID`` back-pointer. Placement stays
    # span.
    owner = _resolve_tool_owner(rec, cur.tool_owner)
    if owner is not None and _maybe_link_subagent(td, rec, owner, path):
        dirty.add(('session', path))   # new row in the subagents block
    if cur.span_start is None:
        cur.span_start = n
        # Initialise a live-shared span records list so further
        # appends from this same call AND from future tail ticks are
        # both visible to the umbrella's preview without a copy step.
        td.span_records[n] = []
        td.roots_in_order.append({
            'kind': 'span', 'start': n,
            'records': td.span_records[n],
        })
        dirty.add(('session', path))   # new root-level span umbrella
    span_start = cur.span_start
    td.span_records[span_start].append(rec)
    td.parent_id_of[n] = ('span', path, span_start)
    dirty.add(('span', path, span_start))
    return dirty


def _resolve_tool_owner(rec, tool_owner):
    """Look up the assistant_uuid that this record back-points at, or None."""
    # (0) Explicit assistant-uuid back-pointer (subagent jsonls stamp
    # every tool-result user record with the owning assistant turn's
    # uuid). Strongest signal — bypasses the tool_owner indirection.
    asst = rec.get('sourceToolAssistantUUID')
    if asst:
        return asst
    # (1) user tool_result content: tool_use_id back-pointer.
    msg = rec.get('message')
    if isinstance(msg, dict):
        content = msg.get('content')
        if isinstance(content, list):
            for p in content:
                if (isinstance(p, dict)
                        and p.get('type') == 'tool_result'):
                    tu = p.get('tool_use_id')
                    if tu in tool_owner:
                        return tool_owner[tu]
    # (2) attachment with toolUseID — hook outputs.
    if rec.get('type') == 'attachment':
        att = rec.get('attachment')
        if isinstance(att, dict):
            tu = att.get('toolUseID')
            if tu in tool_owner:
                return tool_owner[tu]
    # (3) records with explicit back-pointer fields.
    tu = rec.get('sourceToolUseID')
    if tu in tool_owner:
        return tool_owner[tu]
    return None


def _maybe_link_subagent(td, rec, owner_uuid, path):
    """If ``rec`` is a tool_result that resolves a subagent jsonl, link it.

    Inspects the owner assistant's tool_use parts to find the matching
    Agent/Task call and records the join via ``td.agent_link`` plus the
    ``agent_to_assistant_line`` shortcut. Returns True when a link was
    recorded (so a live-tail fold can dirty the session's top block);
    no-op returning False if anything mismatches.
    """
    linked = False
    msg = rec.get('message')
    if not isinstance(msg, dict):
        return linked
    content = msg.get('content')
    if not isinstance(content, list):
        return linked
    asst = td.by_uuid.get(owner_uuid)
    if asst is None:
        return linked
    amsg = asst.get('message') or {}
    if not isinstance(amsg, dict):
        return linked
    acontent = amsg.get('content') or []
    if not isinstance(acontent, list):
        return linked
    for cp in content:
        if not (isinstance(cp, dict) and cp.get('type') == 'tool_result'):
            continue
        tu_id = cp.get('tool_use_id')
        if not tu_id:
            continue
        for ap in acontent:
            if not (isinstance(ap, dict)
                    and ap.get('type') == 'tool_use'
                    and ap.get('id') == tu_id
                    and ap.get('name') in ('Agent', 'Task')):
                continue
            tur = rec.get('toolUseResult')
            agent_id = (tur.get('agentId')
                        if isinstance(tur, dict) else None)
            if agent_id:
                agent_path = _resolve_agent_jsonl(path, agent_id)
                if not os.path.isfile(agent_path):
                    continue
            elif (isinstance(tur, dict)
                    and tur.get('status') == 'teammate_spawned'):
                # Team worker: the spawn result carries no agentId.
                # New backend first: the worker runs in-process and
                # its transcript lands in this session's own subagents
                # dir — join via the spawn name + team. The stored
                # agent_id is the filename-derived one, the same key
                # the subagent row listing and ``_resolve_agent_jsonl``
                # use, so the orphan diff and drill-in line up. Legacy
                # backend fallback: the worker runs as its own sibling
                # session, joined via the Agent call's ``name`` and
                # the team roster / head stamps.
                name = (tur.get('name')
                        or (ap.get('input') or {}).get('name'))
                if not name:
                    continue
                # Only new-backend spawn results carry ``name`` — gate
                # the in-process lookup on it so an old-shape spawn can
                # never claim a same-named in-process transcript from a
                # different (unrecorded) team.
                hit = (_find_inprocess_teammate(
                           path, name, tur.get('team_name'))
                       if tur.get('name') else None)
                if hit:
                    agent_id, agent_path = hit
                else:
                    agent_id = name
                    agent_path = _find_worker_jsonl(path, agent_id)
                    if not agent_path:
                        continue
            else:
                continue
            td.agent_link[tu_id] = {
                'agent_id': agent_id,
                'agent_path': agent_path,
                'assistant_uuid': owner_uuid,
            }
            linked = True
            asst_line = td.line_of.get(owner_uuid)
            if asst_line is not None:
                td.agent_to_assistant_line[agent_id] = asst_line
    return linked


def _list_tree_roots(jsonl_path):
    """Tree-mode listing for a session: root-level rows in file order.

    Each entry is either a ``<prompt>`` umbrella (one per turn) or a
    ``<system>`` umbrella collapsing a contiguous run of metadata
    records. The first message is no longer also the expandable node —
    that's the umbrella's job.

    ALL the session's subagent files surface here in a top block,
    newest-first like the flat view; agents with no dispatch site in
    the main thread come back tagged ``orphan`` and dimmed.
    """
    td = _scan_tree(jsonl_path)
    out = []
    subs = _tree_subagents_for_session(jsonl_path, td)
    # When the session has subagents, bracket their block with two
    # ``meta`` dividers so the user sees where the subagents end and
    # the turn/span umbrellas begin. A session without any renders
    # exactly as before. Ids are session-namespaced
    # (``('sep', jsonl, which)``) so they stay unique across cached
    # sessions AND stable, doubling as insertion sentinels for the
    # ``update_data`` positioning API (meta-rows design sec Migrations
    # → browse-claude). The framework skips the cursor over them, never
    # selects them, and (with ``meta_filter_mode='show'``) keeps them
    # visible under an active filter.
    if subs:
        out.append(Item(id=('sep', jsonl_path, 'subagents'),
                        title='--- Subagents:', meta=True))
        out.extend(subs)
        out.append(Item(id=('sep', jsonl_path, 'session'),
                        title='--- Session:', meta=True))
    for entry in td.roots_in_order:
        if entry['kind'] == 'turn':
            line = entry['line']
            rec = entry['rec']
            out.append(_prompt_umbrella_item(jsonl_path, line, rec, td))
        else:   # 'span'
            out.append(_span_umbrella_item(
                jsonl_path, entry['start'], entry['records'],
            ))
    return out


def _tree_subagents_for_session(jsonl_path, td):
    """Subagent rows for the tree-mode top block, newest-first.

    Keeps the flat-view ordering from ``_list_subagents_for_session``
    (newest mtime first) so both views show the same row sequence.
    Agents with no dispatch site in the main thread (no
    ``td.agent_to_assistant_line`` entry, built by
    ``_maybe_link_subagent`` during scan) come back tagged
    ``orphan · …`` and dimmed so the user can see they aren't wired
    into the session's turn graph.
    """
    subs = _list_subagents_for_session(jsonl_path)
    for item in subs:
        if item.agent_id in td.agent_to_assistant_line:
            continue
        item.tag = (f'{item.agent_type} · {item.line_count} msg · orphan · '
                    f'{_human_time(item.mtime)}')
        item.tag_style = 'dim'
        item.is_orphan = True
    return subs


def _list_prompt_children(jsonl_path, turn_root_line):
    """Children of a ``<prompt>`` umbrella.

    Returns the turn root user voice as a leaf, followed by each turn
    member in file order. Assistant members carrying a ``tool_use`` are
    wrapped in their own ``<tool:Name>`` umbrella (always, even if no
    tool_result has arrived yet — the umbrella shape stays stable as
    the transcript grows).
    """
    td = _scan_tree(jsonl_path)
    if not (0 <= turn_root_line < len(td.records)):
        return []
    rec = td.records[turn_root_line]
    if rec is None:
        return []
    uuid = rec.get('uuid')
    out = []
    # 1) The turn root itself, as a leaf — visible at the top of the
    # expansion so the user prompt content is readable at the same
    # indent as the assistant replies that follow. Keeps its
    # ``row_bg`` voice stripe; the user's voice deserves the marker
    # whether or not it happens to be folded under an umbrella.
    leaf = _tree_item(jsonl_path, rec, td)
    if leaf is not None:
        out.append(leaf)
    # 2) Each turn member.
    if uuid is None:
        return out
    for child in td.turn_direct.get(uuid, []):
        cuuid = child.get('uuid')
        if cuuid in td.assistant_has_tool_use:
            cline = td.line_of.get(cuuid)
            if cline is None:
                continue
            out.append(_tool_umbrella_item(jsonl_path, cline, child, td))
        else:
            it = _tree_item(jsonl_path, child, td)
            if it is not None:
                out.append(it)
    return out


def _list_tool_children(jsonl_path, assistant_line):
    """Children of a ``<tool>`` umbrella.

    Returns the assistant tool_use record as a leaf, then every paired
    tool_result / hook attachment in file order. The umbrella exists
    even when no tool_result has arrived yet so the shape is stable
    across in-flight transcripts. Subagent transcripts are NOT nested
    here — they live in the session's top block (``_list_tree_roots``).
    """
    td = _scan_tree(jsonl_path)
    if not (0 <= assistant_line < len(td.records)):
        return []
    rec = td.records[assistant_line]
    if rec is None:
        return []
    uuid = rec.get('uuid')
    out = []
    # 1) Assistant tool_use record as a leaf. Keeps its ``row_bg``
    # voice stripe if the assistant has text content (mixed text +
    # tool_use case) — same reasoning as the prompt umbrella's first
    # leaf: voice markers belong on voice rows, regardless of nesting.
    leaf = _tree_item(jsonl_path, rec, td)
    if leaf is not None:
        out.append(leaf)
    # 2) Paired tool_children (tool_result, hook attachments, …).
    for child in td.tool_children.get(uuid, []):
        it = _tree_item(jsonl_path, child, td)
        if it is not None:
            out.append(it)
    return out


def _list_span_records(jsonl_path, start_line):
    """Children of a ``<system>`` umbrella: the records of one span."""
    td = _scan_tree(jsonl_path)
    out = []
    for rec in td.span_records.get(start_line, []):
        it = _tree_item(jsonl_path, rec, td)
        if it is not None:
            out.append(it)
    return out


def _tree_item(jsonl_path, rec, td):
    """Build a leaf list-row ``Item`` for ``rec`` in tree mode.

    Conversationally a leaf — wrapping into ``<prompt>`` / ``<tool>`` umbrellas
    is the listing layer's job (``_list_prompt_children`` /
    ``_list_tool_children``); this builder produces the content-row that
    appears **inside** an umbrella's expansion. Its one expandable case is the
    markdown subtree: ``has_children`` reflects ``_md_has_children(rec)`` (a
    no-op ``False`` when ``md_doc`` is absent), so a row with markdown headings
    or a ``.md`` reference still gains the document-subtree arrow.
    """
    n = td.line_of.get(rec.get('uuid'))
    if n is None:
        for i, r in enumerate(td.records):
            if r is rec:
                n = i
                break
    if n is None:
        return None
    kind = _kind_of(rec)

    item_id = ('msg', jsonl_path, n)
    item = Item(
        id=item_id,
        title=_link_marker(item_id) + _oneline(_summarise_message(rec)),
        tag=_row_tag(kind, rec),
        tag_style=_TAG_STYLE_FOR_KIND.get(kind, ''),
        # Optimistic markdown-subtree detection (no-op when md_doc absent):
        # a heading or a ``.md`` ref earns an expansion arrow, built lazily by
        # ``get_children`` and self-healed by ``_on_children_loaded`` if false.
        has_children=_md_has_children(rec),
        hidden=not _passes_filter(item_id, td),
    )
    item.kind = 'message'
    item.line_no = n
    item.session_path = jsonl_path
    item.message_kind = kind
    if _is_voice(rec):
        bg = _ROW_BG_FOR_KIND.get(kind)
        if bg is not None:
            item.row_bg = bg
    return item


def _prompt_umbrella_item(jsonl_path, turn_root_line, rec, td):
    """``<prompt>`` umbrella wrapping one turn.

    Id shape: ``('prompt', jsonl, turn_root_line)``. Children are the turn
    root user voice (as a leaf) followed by every turn member, with each
    assistant-with-tool_use member further wrapped in a ``<tool>``
    umbrella. The umbrella's title prefix mirrors the existing
    ``<system>`` precedent so the kind tag reads consistently across all
    umbrella rows.
    """
    summary = _oneline(_summarise_message(rec))
    uuid = rec.get('uuid')
    members = td.turn_direct.get(uuid, []) if uuid else []
    # Count of direct children visible when expanded: the user voice
    # itself + each member (tool-using members collapse into one row).
    n_replies = len(members)
    item_id = ('prompt', jsonl_path, turn_root_line)
    # A turn opened by a worker→leader task-notification or an inbound
    # peer message is agent voice, not a human prompt — relabel its
    # umbrella so the row doesn't masquerade as one (a SendMessage-only
    # assistant turn can't be a turn root; only user voice opens turns).
    kind = _kind_of(rec)
    prefix = {'agent-reply': '<reply>',
              'agent-peer': '<peer>'}.get(kind, '<prompt>')
    item = Item(
        id=item_id,
        title=f'{_link_marker(item_id)}{prefix}  {summary}',
        tag=f'{n_replies} replies' if n_replies else 'no reply yet',
        tag_style='dim',
        has_children=True,
        hidden=not _passes_filter(item_id, td),
    )
    item.kind = 'prompt'
    item.line_no = turn_root_line
    item.session_path = jsonl_path
    item.message_kind = kind
    # Voice stripe — the umbrella visually IS the user prompt at the
    # turn level. Matches the leaf user-msg row's stripe so the two
    # adjacent rows read as one voice block.
    if _is_voice(rec):
        bg = _ROW_BG_FOR_KIND.get(item.message_kind)
        if bg is not None:
            item.row_bg = bg
    return item


def _tool_umbrella_item(jsonl_path, assistant_line, rec, td):
    """``<tool:Name>`` umbrella wrapping one tool-execution group.

    Id shape: ``('tool', jsonl, assistant_line)``. Children are the
    dispatching assistant itself (leaf) and the paired ``tool_result``
    + hook attachments. The tool name in the prefix is the **first**
    ``tool_use`` part's name.

    Voice stripe: inherited from the underlying assistant record when
    it qualifies as voice (mixed text + tool_use), OR from the subagent
    kind when this umbrella wraps a resolvable Agent/Task dispatch —
    dispatch sites keep the same stripe as the subagent rows in the
    session's top block.
    """
    summary = _oneline(_summarise_message(rec))
    uuid = rec.get('uuid')
    tool_name = td.tool_use_name_of.get(uuid, 'tool')
    item_id = ('tool', jsonl_path, assistant_line)
    item = Item(
        id=item_id,
        title=f'{_link_marker(item_id)}<tool:{tool_name}>  {summary}',
        tag='tool_use',
        tag_style=_TAG_STYLE_FOR_KIND.get('tool_use', ''),
        has_children=True,
        hidden=not _passes_filter(item_id, td),
    )
    item.kind = 'tool'
    item.line_no = assistant_line
    item.session_path = jsonl_path
    item.message_kind = _kind_of(rec)
    item.tool_name = tool_name
    if _is_voice(rec):
        bg = _ROW_BG_FOR_KIND.get(item.message_kind)
        if bg is not None:
            item.row_bg = bg
    elif _tool_umbrella_wraps_subagent(rec, td):
        bg = _ROW_BG_FOR_KIND.get('subagent')
        if bg is not None:
            item.row_bg = bg
    return item


def _tool_umbrella_wraps_subagent(rec, td):
    """True iff this assistant tool_use record dispatches a resolvable
    Agent/Task subagent — i.e. there is at least one tool_use part with
    name in ('Agent', 'Task') whose id has an ``agent_link`` entry.
    """
    msg = rec.get('message') if isinstance(rec, dict) else None
    if not isinstance(msg, dict):
        return False
    content = msg.get('content')
    if not isinstance(content, list):
        return False
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get('type') != 'tool_use':
            continue
        if p.get('name') not in ('Agent', 'Task'):
            continue
        if td.agent_link.get(p.get('id')):
            return True
    return False


def _span_umbrella_item(jsonl_path, start_line, records):
    """Synthetic ``<system>`` row collapsing a metadata span.

    Id shape: ``('span', jsonl, start_line)``. ``get_children`` routes
    that id to ``_list_span_records`` which returns the span's records
    as flat children. Title shows the count + dominant types so the
    row's still informative when collapsed.
    """
    from collections import Counter
    type_counts = Counter()
    for r in records:
        if not isinstance(r, dict):
            continue
        t = r.get('type', '?')
        if t == 'attachment':
            att = r.get('attachment') or {}
            sub = att.get('type', '') if isinstance(att, dict) else ''
            type_counts[f'att:{sub}'] += 1
        elif t == 'system':
            sub = r.get('subtype', '')
            type_counts[f'sys:{sub}' if sub else 'system'] += 1
        else:
            type_counts[t] += 1
    types_summary = ', '.join(
        f'{n}× {t}' if n > 1 else t
        for t, n in type_counts.most_common(4)
    )
    if len(type_counts) > 4:
        types_summary += ', …'

    # The span is visible iff any member surfaces at the current level
    # — the same min-level rule ``_passes_filter`` applies to its
    # ``('span', …)`` id, computed here off the records in hand.
    any_visible = any(
        isinstance(r, dict) and _record_min_level(r) <= _DETAIL_LEVEL
        for r in records
    )
    item = Item(
        id=('span', jsonl_path, start_line),
        title=f'<system>  {types_summary}',
        tag=f'{len(records)} records',
        tag_style='dim',
        has_children=len(records) > 0,
        hidden=not any_visible,
    )
    item.kind = 'span'
    item.span_start = start_line
    item.span_count = len(records)
    item.session_path = jsonl_path
    return item
