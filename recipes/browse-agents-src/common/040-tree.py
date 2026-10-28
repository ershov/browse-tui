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
    """Cached tree-structure view of one session transcript.

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
                            subagent transcript on disk.
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

    Per-record attrs:
      * ``rec['_tree_forked_from']`` — record explicitly forked from
        an earlier uuid (``forkedFrom`` field).
    """

    __slots__ = ('records', 'by_uuid', 'line_of', 'roots_in_order',
                 'turn_direct', 'tool_children', 'span_records',
                 'agent_link', 'records_line', 'parent_id_of',
                 'assistant_has_tool_use', 'tool_to_turn_root_line',
                 'agent_to_assistant_line', 'tool_use_name_of',
                 'voice_count', 'latest_voice_line',
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
        # line's leaf row. One of: ``('prompt', sess, n)``,
        # ``('tool', sess, n)``, or ``('span', sess, n)``. Top-level
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
            # toolUseResult.agentId resolves a child transcript.
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
    # (0) Explicit assistant-uuid back-pointer (subagent transcripts stamp
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
    """If ``rec`` is a tool_result that resolves a subagent transcript, link it.

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
            hit = store_resolve_spawned_child(
                path, rec.get('toolUseResult'), ap.get('input'))
            if hit is None:
                continue
            agent_id, agent_path = hit
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


def _list_tree_roots(sess):
    """Tree-mode listing for a session: root-level rows in file order.

    Each entry is either a ``<prompt>`` umbrella (one per turn) or a
    ``<system>`` umbrella collapsing a contiguous run of metadata
    records. The first message is no longer also the expandable node —
    that's the umbrella's job.

    ALL the session's subagent files surface here in a top block,
    newest-first like the flat view; agents with no dispatch site in
    the main thread come back tagged ``orphan`` and dimmed.
    """
    td = _scan_tree(sess)
    out = []
    subs = _tree_subagents_for_session(sess, td)
    # When the session has subagents, bracket their block with two
    # ``meta`` dividers so the user sees where the subagents end and
    # the turn/span umbrellas begin. A session without any renders
    # exactly as before. Ids are session-namespaced
    # (``('sep', sess, which)``) so they stay unique across cached
    # sessions AND stable, doubling as insertion sentinels for the
    # ``update_data`` positioning API (meta-rows design sec Migrations
    # → browse-claude). The framework skips the cursor over them, never
    # selects them, and (with ``meta_filter_mode='show'``) keeps them
    # visible under an active filter.
    if subs:
        out.append(Item(id=('sep', sess, 'subagents'),
                        title='--- Subagents:', meta=True))
        out.extend(subs)
        out.append(Item(id=('sep', sess, 'session'),
                        title='--- Session:', meta=True))
    for entry in td.roots_in_order:
        if entry['kind'] == 'turn':
            line = entry['line']
            rec = entry['rec']
            out.append(_prompt_umbrella_item(sess, line, rec, td))
        else:   # 'span'
            out.append(_span_umbrella_item(
                sess, entry['start'], entry['records'],
            ))
    return out


def _tree_subagents_for_session(sess, td):
    """Subagent rows for the tree-mode top block, newest-first.

    Keeps the flat-view ordering from ``store_child_sessions``
    (newest mtime first) so both views show the same row sequence.
    Agents with no dispatch site in the main thread (no
    ``td.agent_to_assistant_line`` entry, built by
    ``_maybe_link_subagent`` during scan) come back tagged
    ``orphan · …`` and dimmed so the user can see they aren't wired
    into the session's turn graph.
    """
    subs = store_child_sessions(sess)
    for item in subs:
        if item.agent_id in td.agent_to_assistant_line:
            continue
        item.tag = (f'{item.agent_type} · {item.line_count} msg · orphan · '
                    f'{_human_time(item.mtime)}')
        item.tag_style = 'dim'
        item.is_orphan = True
    return subs


def _list_prompt_children(sess, turn_root_line):
    """Children of a ``<prompt>`` umbrella.

    Returns the turn root user voice as a leaf, followed by each turn
    member in file order. Assistant members carrying a ``tool_use`` are
    wrapped in their own ``<tool:Name>`` umbrella (always, even if no
    tool_result has arrived yet — the umbrella shape stays stable as
    the transcript grows).
    """
    td = _scan_tree(sess)
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
    leaf = _tree_item(sess, rec, td)
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
            out.append(_tool_umbrella_item(sess, cline, child, td))
        else:
            it = _tree_item(sess, child, td)
            if it is not None:
                out.append(it)
    return out


def _list_tool_children(sess, assistant_line):
    """Children of a ``<tool>`` umbrella.

    Returns the assistant tool_use record as a leaf, then every paired
    tool_result / hook attachment in file order. The umbrella exists
    even when no tool_result has arrived yet so the shape is stable
    across in-flight transcripts. Subagent transcripts are NOT nested
    here — they live in the session's top block (``_list_tree_roots``).
    """
    td = _scan_tree(sess)
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
    leaf = _tree_item(sess, rec, td)
    if leaf is not None:
        out.append(leaf)
    # 2) Paired tool_children (tool_result, hook attachments, …).
    for child in td.tool_children.get(uuid, []):
        it = _tree_item(sess, child, td)
        if it is not None:
            out.append(it)
    return out


def _list_span_records(sess, start_line):
    """Children of a ``<system>`` umbrella: the records of one span."""
    td = _scan_tree(sess)
    out = []
    for rec in td.span_records.get(start_line, []):
        it = _tree_item(sess, rec, td)
        if it is not None:
            out.append(it)
    return out


def _tree_item(sess, rec, td):
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

    item_id = ('msg', sess, n)
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
    item.session_path = sess
    item.message_kind = kind
    if _is_voice(rec):
        bg = _ROW_BG_FOR_KIND.get(kind)
        if bg is not None:
            item.row_bg = bg
    return item


def _prompt_umbrella_item(sess, turn_root_line, rec, td):
    """``<prompt>`` umbrella wrapping one turn.

    Id shape: ``('prompt', sess, turn_root_line)``. Children are the turn
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
    item_id = ('prompt', sess, turn_root_line)
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
    item.session_path = sess
    item.message_kind = kind
    # Voice stripe — the umbrella visually IS the user prompt at the
    # turn level. Matches the leaf user-msg row's stripe so the two
    # adjacent rows read as one voice block.
    if _is_voice(rec):
        bg = _ROW_BG_FOR_KIND.get(item.message_kind)
        if bg is not None:
            item.row_bg = bg
    return item


def _tool_umbrella_item(sess, assistant_line, rec, td):
    """``<tool:Name>`` umbrella wrapping one tool-execution group.

    Id shape: ``('tool', sess, assistant_line)``. Children are the
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
    item_id = ('tool', sess, assistant_line)
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
    item.session_path = sess
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


def _span_umbrella_item(sess, start_line, records):
    """Synthetic ``<system>`` row collapsing a metadata span.

    Id shape: ``('span', sess, start_line)``. ``get_children`` routes
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
        id=('span', sess, start_line),
        title=f'<system>  {types_summary}',
        tag=f'{len(records)} records',
        tag_style='dim',
        has_children=len(records) > 0,
        hidden=not any_visible,
    )
    item.kind = 'span'
    item.span_start = start_line
    item.span_count = len(records)
    item.session_path = sess
    return item


# ---- live tail -----------------------------------------------------------
#
# ``_scan_tree`` reads a session once through ``store_tail_open`` and keeps
# the returned tail state; the worker thread polls the cursor's session
# every ``store_tail_interval()`` seconds through ``store_tail_poll``,
# folds the new records into the cached ``_TreeData`` and pushes
# ``update_data`` patches for the umbrellas whose visible child list grew.


_TAIL_STATE = {}    # session_key → store tail state (``.cursor`` = fold cursor)
_TAIL_STOP = None   # threading.Event; created in main()
_TAIL_THREAD = None # threading.Thread; created in main()


def _resolve_session_for_bust(item_id):
    """Map an id to the session whose ``_TreeData`` it depends on.

    Returns ``None`` when ``item_id`` doesn't reference per-session
    scanned state (projects, synthetic ids).
    """
    if not isinstance(item_id, tuple) or not item_id:
        return None
    tag = item_id[0]
    if tag == 'agent':
        return store_child_session(item_id[1], item_id[2])
    if tag in ('md', 'refs'):
        sess = item_id[1][1]
    elif tag in ('session', 'prompt', 'tool', 'span', 'msg', 'trunc', 'err'):
        sess = item_id[1]
    else:
        return None
    return sess if store_session_exists(sess) else None


def _bust_caches_for(item_id):
    """Drop cached ``_TreeData`` + tail state for one session's tree.

    Used by ``get_children(..., reload=True)`` to refresh a single
    subtree without disturbing siblings. No-op for ids that don't
    resolve to a per-session ``_TreeData``.
    """
    sess = _resolve_session_for_bust(item_id)
    if sess is None:
        return
    _TREE_CACHE.pop(sess, None)
    _TAIL_STATE.pop(sess, None)


def _scan_tree(sess):
    """Return cached ``_TreeData`` for ``sess``; bulk-read on cache miss.

    Cache invalidation is explicit (``get_children(id, reload=True)``
    drops the entry; ``get_children(None, reload=True)`` clears
    everything). The live-tail worker keeps the cached ``td`` in sync
    with the session's growth — no time-based check needed.

    Concurrent callers (children worker + preview worker on open)
    serialize on a per-session mutex so a huge transcript isn't read
    twice under GIL contention. The mutex is dropped on completion;
    the cache lookup is double-checked under the lock so the second
    caller returns the just-populated entry without re-reading.
    """
    cached = _TREE_CACHE.get(sess)
    if cached is not None:
        return cached
    with _SCAN_LOCKS_LOCK:
        lock = _SCAN_LOCKS.setdefault(sess, threading.Lock())
    with lock:
        cached = _TREE_CACHE.get(sess)
        if cached is not None:
            return cached
        return _scan_tree_locked(sess)


def _scan_tree_locked(sess):
    """Body of ``_scan_tree`` — runs under the per-session mutex.

    Reads the transcript through ``store_tail_open`` and folds every
    record. The returned tail state (watermark at the end of the read)
    is kept so the worker resumes exactly where this read stopped, with
    the same open-turn / open-span fold cursor.
    """
    td = _TreeData()
    cur = _ScanCursor()
    records, state = store_tail_open(sess)
    _fold_records(td, cur, records, sess)
    if state is not None:
        state.cursor = cur
        _TAIL_STATE[sess] = state
    _TREE_CACHE[sess] = td
    return td


def _fold_records(td, cur, records, sess):
    """Store ``[(record_key, rec_or_None), …]`` in ``td`` and fold each record.

    Returns the set of umbrella ids whose visible child list grew (the
    bulk read ignores it; the tail consumes it).
    """
    dirty = set()
    for n, rec in records:
        while len(td.records) <= n:
            td.records.append(None)
        td.records[n] = rec
        if rec is None:
            continue
        dirty |= _fold_record(td, cur, rec, n, sess)
        if _is_voice(rec):
            td.latest_voice_line = n
    return dirty


def _read_new_records(sess):
    """Records appended since the last call: ``(records, dirty_parents)``.

    ``records`` is ``[(record_key, rec_or_None), …]`` from
    ``store_tail_poll``. When ``_TREE_CACHE`` has an entry the records
    are also folded into it (so tree-mode umbrellas, parent_id_of,
    agent_link … stay current) and ``dirty_parents`` is the set of
    umbrella ids whose visible child list grew; a record the store
    reports as changed is replaced in place and its parent dirtied.
    Flat mode ignores the dirty set and consumes ``records`` directly.
    ``([], set())`` when the session has no tail state or the store
    has nothing new.
    """
    state = _TAIL_STATE.get(sess)
    if state is None:
        return ([], set())
    new, changed = store_tail_poll(sess, state)
    td = _TREE_CACHE.get(sess)
    dirty = set()
    if td is not None:
        dirty |= _fold_records(td, state.cursor, new, sess)
        for n, rec in changed:
            if 0 <= n < len(td.records):
                td.records[n] = rec
                parent = td.parent_id_of.get(n)
                if parent is not None:
                    dirty.add(parent)
    return (new, dirty)


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
    """Background loop: poll the cursor's session once per tick.

    Best-effort and single-threaded — one session per tick, the one
    the cursor currently sits in (per the flat/tree rule encoded in
    ``_cursor_tail_session``). Polls through ``_read_new_records``, then
    dispatches mode-specific push:

      * Tree mode consumes the ``dirty_parents`` set returned by the
        fold step (each parent umbrella's child list grew).
      * Flat mode consumes the raw ``records`` list and emits one
        ``upsert`` per new record, appended at the end.

    The tick period is ``store_tail_interval()``. Stopped via the
    module-level ``_TAIL_STOP`` event when ``main`` exits. Marked
    daemon as a backstop.
    """
    interval = store_tail_interval()
    while not _TAIL_STOP.is_set():
        try:
            sess = _resolve_tail_session_from_browser(b)
            if sess is not None:
                records = _maybe_bootstrap_tail_state(sess)
                new, dirty = _read_new_records(sess)
                records += new
                if records or dirty:
                    if _TREE_MODE:
                        _push_tail_diffs(b, dirty)
                    else:
                        _push_flat_inserts(b, sess, records)
                    # Bust the preview cache for the session so the
                    # session-card preview re-renders even when the
                    # cursor stayed parked on a row above the
                    # changed listing.
                    b.post(lambda s=sess: _bust_preview_for_session(b, s))
        except Exception:
            # Best-effort: a malformed cursor row / get_children
            # blowup must not kill the worker. Swallow silently;
            # the next tick gets a clean shot.
            pass
        _TAIL_STOP.wait(timeout=interval)


def _maybe_bootstrap_tail_state(sess):
    """Open the session's tail state if missing; returns the records read.

    ``_scan_tree`` normally opens it (every listing goes through the
    read). A session whose caches were dropped since its listing is
    re-read here, and the records come back so flat mode can sync the
    framework's cached children with them. ``[]`` when the state exists.
    """
    if sess in _TAIL_STATE:
        return []
    _TREE_CACHE.pop(sess, None)
    td = _scan_tree(sess)
    return list(enumerate(td.records))


def _push_flat_inserts(b, sess, records):
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
    session_id = ('session', sess)
    existing = b.cached_children(session_id)
    if existing is None:
        return
    existing_ids = {it.id for it in existing}

    ops = []
    for n, rec in records:
        if rec is None:
            continue
        item_id = ('msg', sess, n)
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
            'session_path': sess,
            'message_kind': kind,
        }
        if _is_voice(rec):
            bg = _ROW_BG_FOR_KIND.get(kind)
            if bg is not None:
                fields['row_bg'] = bg
        ops.append(upsert(item_id, session_id, **fields))
    if ops:
        b.update_data(ops)


def _id_session(item_id):
    """The session key an id is keyed under, or ``None``.

    For every per-session id this is the key that appears as the id's
    first coordinate — the session the row is *displayed under*. A
    subagent group ``('agent', sess, aid)`` resolves to ``sess`` (the
    session it nests in). Markdown ids read the key out of their anchor.
    ``('project', key)`` and synthetic ids yield ``None``.
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


def _bust_preview_for_session(b, sess):
    """Drop cached preview text for ``sess`` and force a re-fetch.

    Runs on the main thread (via ``Browser.post``). For the cursor's
    row or any descendant of ``sess``, calls ``invalidate_preview``
    which re-fetches *without* resetting view state — a user who
    pinned the view to the bottom (Shift-End) keeps following the
    tail as the umbrella's preview grows. For non-cursor sessions the
    cache is dropped so the next visit re-fetches.
    """
    cur_id = b.preview_item_id
    if cur_id == ('session', sess) or _id_session(cur_id) == sess:
        b.invalidate_preview(cur_id)
    else:
        b.drop_preview_cache(('session', sess))


def _resolve_tail_session_from_browser(b):
    """Return the session the cursor's current row maps to, or None.

    Reads the cursor + visible-list through a one-off Context so
    the scope_root row's id participates too — a freshly-launched
    recipe that hasn't moved the cursor still resolves a session.
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
        return _cursor_tail_session(item_id)
    except Exception:
        return None


def _cursor_tail_session(item_id):
    """Resolve the session the tailer should follow for ``item_id``.

    Mode-dependent for the subagent group row:

      * Tree mode: ``('agent', sess, aid)`` reflects activity in the
        parent session's tree, so tail the parent.
      * Flat mode: ``('agent', sess, aid)`` IS the subagent's listing
        (drill-into row), so tail the subagent.

    Other id kinds: take the id's session. ``None`` when the resolved
    session isn't one the store can tail.
    """
    if not isinstance(item_id, tuple) or not item_id:
        return None
    if item_id[0] == 'agent':
        sess, aid = item_id[1], item_id[2]
        if _TREE_MODE:
            return sess if store_tailable(sess) else None
        child = store_child_session(sess, aid)
        return child if child and store_tailable(child) else None
    sess = _id_session(item_id)
    return sess if sess is not None and store_tailable(sess) else None


def _last_voice_id(sess):
    """Return ``('msg', sess, n)`` id of the most-recent voice record, or None.

    "Voice" matches ``_is_voice`` (user prompt or assistant text).
    Reads the eagerly-tracked ``td.latest_voice_line`` populated by
    ``_scan_tree`` during its forward pass, so this is O(1) on cache
    hit (no reverse walk).
    """
    td = _scan_tree(sess)
    n = td.latest_voice_line
    if n is None:
        return None
    return ('msg', sess, n)


def _last_visible_voice_id(sess):
    """Newest voice record VISIBLE at the current detail level, or None.

    The absolute latest voice can be filtered out — a mid-turn assistant
    text at the summary level — and ``cursor_to`` silently no-ops on
    hidden rows, so the cursor-on-open jump must target a row the filter
    shows. Walks back from ``td.latest_voice_line`` to the newest voice
    that passes; when NO voice is visible, returns the absolute latest
    so the caller's chain trim lands on its nearest visible ancestor
    (the wrapping ``<prompt>``).
    """
    td = _scan_tree(sess)
    n = td.latest_voice_line
    if n is None:
        return None
    for i in range(n, -1, -1):
        rec = td.records[i]
        if not (isinstance(rec, dict) and _is_voice(rec)):
            continue
        if _passes_filter(('msg', sess, i), td):
            return ('msg', sess, i)
    return ('msg', sess, n)


# ---- flat-mode listings --------------------------------------------------


def _list_session_children(sess):
    """Return subagent groups + top-level messages for a session.

    Subagents come first (most users want to see what was dispatched
    before scrolling through hundreds of main-thread messages).
    Sessions without any subagents render exactly as before.
    """
    subs = store_child_sessions(sess)
    msgs = _list_messages(sess)
    return subs + msgs


def _list_subagent_messages(item_id):
    """Children for a subagent group ``('agent', sess, aid)``: its records."""
    child = store_child_session(item_id[1], item_id[2])
    return _list_messages(child) if child else []


def _list_messages(sess, limit=None):
    """List messages in **chronological** order (oldest first).

    Matches tree-mode ordering so the cursor-jump-to-latest-voice and
    the t-toggle don't visually flip the conversation on the user.

    Reads from the cached ``_TREE_CACHE[sess].records`` so we
    don't re-read the whole transcript on every navigation.
    ``_scan_tree`` populates the cache the first time we visit the
    session (and keeps its tail state so the live-tail worker resumes
    from where the read stopped). Placeholder records (``None``) are
    skipped without surfacing a fake row.

    ``limit`` truncates from the **start** of the file (so the user
    keeps the most recent messages — they're scrolling toward the
    tail anyway). ``limit=0`` (the default after #475) disables
    truncation entirely.
    """
    if limit is None:
        limit = _message_limit
    if not store_session_exists(sess):
        return [Item(
            id=('err', sess),
            title=f'[error] no such file: {sess}',
            tag='err',
            tag_style='red',
        )]
    td = _scan_tree(sess)
    total = len(td.records)
    truncated = bool(limit) and total > limit
    start = total - limit if truncated else 0

    out = []
    if truncated:
        older = start
        out.append(Item(
            id=('trunc', sess),
            title=f'({older} older entries hidden; press e to view file)',
            tag='…',
            tag_style='dim',
        ))
    for n in range(start, total):
        obj = td.records[n]
        if obj is None:
            continue
        kind = _kind_of(obj)
        item_id = ('msg', sess, n)
        item = Item(
            id=item_id,
            title=_link_marker(item_id) + _oneline(_summarise_message(obj)),
            tag=_row_tag(kind, obj),
            tag_style=_TAG_STYLE_FOR_KIND.get(kind, ''),
            # Optimistic markdown-subtree detection — see ``_tree_item``. The
            # feature is mode-agnostic, so flat-mode rows gain the same arrow;
            # ``get_children`` returns the document nodes regardless of mode.
            has_children=_md_has_children(obj),
            hidden=not _passes_filter(item_id, td),
        )
        item.kind = 'message'
        item.line_no = n
        item.session_path = sess
        item.message_kind = kind
        # Stripe rows that carry actual *speech* — the user typing, or
        # the assistant's text reply. Tool calls (assistant tool_use)
        # and tool results (user tool_result) are machinery, not voice,
        # so they keep the default no-bg look even though their record
        # type is user/assistant.
        if _is_voice(obj):
            bg = _ROW_BG_FOR_KIND.get(kind)
            if bg is not None:
                item.row_bg = bg
        out.append(item)

    return out


# ---- get_children: dispatches by id tag ----------------------------------
#
# The Browser passes the raw item id back into ``get_children``. Every id is a
# tagged tuple whose first element is a short string tag (or ``None`` for the
# root), so routing is a flat ``id[0]`` dispatch with direct field access
# (``id[1]`` = the session key, ``id[2]`` = a record / agent key). No substring
# scans, no ordering hazards — the tags are unambiguous.


def get_children(item_id, *, reload=False):
    # ``reload=True`` is the framework's explicit "this is a refresh,
    # not an initial fetch" signal — see ``_do_refresh`` in 040-state.py
    # and ticket #407. Honour it at every id shape so the framework can
    # refresh a single subtree (one session file's tree, one subagent's
    # tree, or the entire root) without disturbing the rest.
    #
    # Granularity:
    #   * ``None``                — full wipe (root refresh / Ctrl-R).
    #   * ``('session', sess)``   — bust just that session's _TreeData.
    #   * ``('prompt'|'tool'|'span', sess, key)`` — bust the containing
    #     session's _TreeData; one umbrella's children are derived from
    #     indexes in the same TreeData so anything narrower is moot.
    #   * ``('agent', sess, aid)`` — bust the subagent's tree too.
    #   * ``('project', dir)``    — nothing to bust (no _TreeData).
    if reload and item_id is not None:
        _bust_caches_for(item_id)
    if item_id is None:
        # ``get_children(None)`` fires on initial load AND on
        # ``ctx.refresh()`` / Ctrl-R. The framework distinguishes the
        # two via ``reload=True`` — only an explicit refresh wipes
        # ``_TREE_CACHE`` / ``_TAIL_STATE``. Initial-load callers leave
        # ``reload`` at the default — typical startup sequence already
        # ran ``_scan_tree`` for the user's scope before the root
        # listing fires, and we don't want to throw that work away.
        if reload:
            _TREE_CACHE.clear()
            _TAIL_STATE.clear()
            store_reset()
            # Drop the shared markdown parse cache too, so a root Ctrl-R
            # re-reads any referenced ``.md`` files edited on disk. (A
            # per-file ``_bust_caches_for`` can't reach these — they live
            # outside the session tree — so the full wipe is the right
            # granularity.) No-op when md_doc isn't importable.
            if _md_doc is not None:
                _md_doc.clear_cache()
        return store_list_projects()
    if not isinstance(item_id, tuple) or not item_id:
        return []
    tag = item_id[0]
    if tag == 'agent':
        # ``('agent', sess, aid)``. Tree mode views the subagent transcript
        # as its own tree, recursing into _list_tree_roots. Flat mode lists
        # its records.
        if _TREE_MODE:
            child = store_child_session(item_id[1], item_id[2])
            return _list_tree_roots(child) if child else []
        return _list_subagent_messages(item_id)
    if tag == 'prompt':
        # ``('prompt', sess, turn_root_line)`` — turn umbrella.
        if _TREE_MODE:
            return _list_prompt_children(item_id[1], item_id[2])
        return []
    if tag == 'tool':
        # ``('tool', sess, assistant_line)`` — tool-execution umbrella.
        if _TREE_MODE:
            return _list_tool_children(item_id[1], item_id[2])
        return []
    if tag == 'span':
        # ``('span', sess, start)`` — synthetic <system> umbrella in tree
        # mode. Children are the records of that span.
        if _TREE_MODE:
            return _list_span_records(item_id[1], item_id[2])
        return []
    if tag == 'refs':
        # ``('refs', anchor, chain)`` — a ``[links]`` References umbrella.
        # THIS is the lazy point: the full ref resolve happens here, on
        # umbrella expand. No-op when ``_md_doc is None`` (no ref id can
        # exist then).
        return _md_refs_umbrella_children(item_id) if _md_doc is not None else []
    if tag == 'md':
        # ``('md', anchor, chain, line)`` — a node inside a message's markdown
        # document subtree (inline doc, file doc, or heading). Built lazily
        # and authoritatively here; mode-agnostic (works in tree AND flat).
        return _md_subtree_children(item_id) if _md_doc is not None else []
    if tag == 'msg':
        # ``('msg', sess, n)`` — a message leaf. When ``md_doc`` is present,
        # a message gains markdown children (its inline document + referenced
        # ``.md`` files) — built lazily here, mode-agnostic. Otherwise it is a
        # leaf in tree mode (umbrellas own all the expansion) and flat mode
        # never had children for these.
        if _md_doc is not None:
            return _md_message_children(item_id)
        return []
    if tag == 'session':
        sess = item_id[1]
        if not store_session_exists(sess):
            return []
        if _TREE_MODE:
            # Tree mode: subagents surface in a top block above the
            # turn/span umbrellas — all handled by the roots listing.
            return _list_tree_roots(sess)
        return _list_session_children(sess)
    if tag == 'project':
        return store_list_sessions(item_id[1])
    # Anything else (unknown / synthetic — 'sep', 'trunc', 'err') — empty.
    return []
