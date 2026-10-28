# ---- message classification + summary ------------------------------------
#
# Claude Code .jsonl files mix several record shapes:
#   1. ``type=last-prompt``     — bookmark for the most recent prompt.
#   2. ``type=permission-mode`` — permission-mode change events.
#   3. ``type=user``            — user turn (string or content-list).
#   4. ``type=assistant``       — assistant turn (always content-list).
#   5. anything else            — fall back to ``message``.
#
# The summary is one line, capped at 80 chars, and is intentionally lossy:
# the preview pane shows the full pretty-printed JSON, so we optimise the
# title for fast visual scanning.


_TAG_STYLE_FOR_KIND = {
    # Conversation
    'user':                'cyan',
    'assistant':           'green',
    'attachment':          'blue',
    'system':              'gray',
    'tool_use':            'yellow',
    'tool_result':         'yellow',
    # Inter-agent voice: leader → worker SendMessage (send), the
    # worker → leader task-notification reply (reply), and an inbound
    # peer message from another session (peer).
    'agent-send':          'green',
    'agent-reply':         'cyan',
    'agent-peer':          'green',
    # Bookmarks / state changes
    'last-prompt':         'magenta',
    'permission-mode':     'yellow',
    'pr-link':             'blue',
    'mode':                'red',
    'speculation-accept':  'green',
    'queue-operation':     'cyan',
    # Bookkeeping (folded into card; kept dim in the row list)
    'ai-title':                   'dim',
    'custom-title':               'dim',
    'tag':                        'dim',
    'agent-name':                 'dim',
    'agent-color':                'dim',
    'agent-setting':              'dim',
    'task-summary':               'dim',
    'summary':                    'dim',
    'file-history-snapshot':      'dim',
    'attribution-snapshot':       'dim',
    'content-replacement':        'dim',
    'worktree-state':             'dim',
    'marble-origami-commit':      'dim',
    'marble-origami-snapshot':    'dim',
    'progress':                   'dim',
    'cost-state':                 'dim',
    'atis-latch':                 'dim',
}


# Row-background highlights for the conversational pulse — applied via
# ``item.row_bg`` so the framework's _write_segments paints the whole
# row (incl. trailing pad) in the colour. Subtle 256-colour cells
# chosen to read on dark terminals without overpowering the foreground:
#    17 = #00005f (deep blue)           for user prompts (matches the
#                                       BLUE_BG user-voice band in preview)
#   235 = #262626 (near-black neutral)  for assistant responses
_ROW_BG_FOR_KIND = {
    'user':             17,
    'assistant':       235,
    # ``queue-operation`` is the user typing more prompts while Claude
    # is mid-turn — they're voice, just delivered out-of-band. Same
    # stripe as a normal user voice so they read as "human still
    # talking" even though they slot into the open turn rather than
    # opening a new one.
    'queue-operation':  17,
    # Subagent umbrella rows: the dispatching assistant ``Agent`` /
    # ``Task`` call IS voice (it's how the assistant tells the
    # subagent what to do), so the umbrella inherits the assistant
    # stripe.
    'subagent':        235,
    # Outbound leader→worker SendMessage: agent-to-agent traffic shares
    # the assistant stripe — the whole point is to keep it off the human
    # stripe (17).
    'agent-send':      235,
    # Inbound worker→leader task-notification reply — same reasoning as
    # the send half: redirect the reply off the human stripe.
    'agent-reply':     235,
    # Inbound peer message (another session's SendMessage) — the darkest
    # green in the 256 cube (22, #005f00): agent-to-agent voice, distinct
    # from both the human stripe (17) and the assistant stripe (235).
    'agent-peer':       22,
}


def _is_voice(obj):
    """True iff this record carries actual *speech* (text content).

    The conversational stripe should highlight only what the user or
    assistant literally *said*. A user record whose content is just a
    tool_result wrapper, or an assistant record whose only content is
    a tool_use call, is machinery — kind/tag already calls it out, we
    don't want to dye those rows. ``queue-operation`` records also
    count as voice (the user typed something while Claude was working).

    ``AskUserQuestion`` is the one tool that *is* dialog: the assistant's
    tool_use is the question itself, and the user's tool_result is the
    answer. Both halves count as voice — detected structurally by tool
    name and by the ``{questions, answers}`` shape on ``toolUseResult``.
    ``SendMessage`` (leader → worker) is the same case for the outbound
    half: the assistant's tool_use *is* the message, so it counts as
    voice. Its ``{success, message}`` delivery ack is just a status
    receipt, not voice.

    An assistant ``thinking`` part with readable text is running
    commentary (mid-turn voice) and counts too; signature-only
    (redacted) thinking does not — see ``_is_empty_thinking_only``.
    """
    t = obj.get('type')
    if t == 'queue-operation':
        return bool((obj.get('content') or '').strip())
    if t not in ('user', 'assistant'):
        return False
    msg = obj.get('message') if isinstance(obj.get('message'), dict) else {}
    content = msg.get('content', '')
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for p in content:
            if not isinstance(p, dict):
                continue
            pt = p.get('type')
            if pt == 'text' and (p.get('text') or '').strip():
                return True
            if (pt == 'thinking' and t == 'assistant'
                    and (p.get('thinking') or '').strip()):
                return True
            if pt == 'tool_use' and p.get('name') in (
                    'AskUserQuestion', 'SendMessage'):
                return True
        if t == 'user':
            tur = obj.get('toolUseResult')
            if isinstance(tur, dict) and 'questions' in tur and 'answers' in tur:
                return True
        return False
    return False


# Framing records that belong to "the conversation as lived" (level 4,
# tools) even though they aren't user/assistant turns: the per-turn
# duration stamp and the inline api_error banner. (type, subtype) keys.
_LIVE_EXTRAS = frozenset([
    ('system', 'turn_duration'),
    ('system', 'api_error'),
])

# Curated allowlist of useful metadata surfaced at level 5 (detailed).
# Plain strings match a record ``type`` outright; tuples match (type,
# subtype) for the finer-grained ``system`` / ``attachment`` families.
# Anything not listed here (and not caught by an earlier tier) falls
# through to level 6 (recognised noise) or 7 (unknown).
_L3_USEFUL = frozenset([
    'summary',
    'task-summary',
    'last-prompt',
    'pr-link',
    'worktree-state',
    'custom-title',
    'tag',
    'queue-operation',
    'marble-origami-commit',
    'cost-state',
    ('system', 'local_command'),
    ('system', 'away_summary'),
    ('system', 'agents_killed'),
    ('attachment', 'file'),
    ('attachment', 'queued_command'),
    ('attachment', 'hook_success'),
    ('attachment', 'hook_cancelled'),
    ('attachment', 'diagnostics'),
    ('attachment', 'invoked_skills'),
])


# Recognised-but-noise record kinds, pinned at level 6 (all). Level 7
# (unknown) is a maintenance view: ``_record_min_level``'s fallthrough
# returns 7 only for a type / system subtype / attachment type the
# recipe does not recognise anywhere — neither in a tier above, nor
# here, nor in ``_ATTACH_FORMATTERS`` / ``_METADATA_TYPES`` (the
# renderer registries double as the known lists).
_KNOWN_SYSTEM_NOISE = frozenset([
    'stop_hook_summary',
    'scheduled_task_fire',
    'informational',
])
_KNOWN_TYPE_NOISE = frozenset(['file-history-delta'])


def _thinking_text(rec):
    """Text of the record's first non-empty ``thinking`` part, or ``''``.

    Signature-only (redacted) thinking parts carry a ``signature`` but
    no readable text and don't count — see ``_is_empty_thinking_only``.
    """
    msg = rec.get('message') if isinstance(rec.get('message'), dict) else {}
    content = msg.get('content')
    if not isinstance(content, list):
        return ''
    for p in content:
        if isinstance(p, dict) and p.get('type') == 'thinking':
            text = (p.get('thinking') or '').strip()
            if text:
                return text
    return ''


def _is_empty_thinking_only(rec):
    """True iff this record's only content is empty ``thinking`` parts.

    Signature-only (redacted/encrypted) thinking blocks store a
    ``signature`` but no readable ``thinking`` text. A turn made up
    solely of such parts has nothing to render — ``_extract_text`` skips
    thinking, so it summarises as the bare ``<N parts>`` shape — so
    ``_record_min_level`` sinks it to the ``all`` (6) tier. A thinking
    part with actual text, or any other part (tool_use / tool_result /
    text) alongside, fails the test — readable thinking is voice
    (level 2), other machinery stays at level 4 (3 for an edit tool).
    """
    msg = rec.get('message') if isinstance(rec.get('message'), dict) else {}
    content = msg.get('content')
    if not isinstance(content, list) or not content:
        return False
    return all(
        isinstance(p, dict) and p.get('type') == 'thinking'
        and not (p.get('thinking') or '').strip()
        for p in content
    )


# File-mutating tools, surfaced at the "edits" level (3) — below the full
# "tools" view (4) — so a reviewer sees what changed without the Read /
# Bash / Grep noise.
_EDIT_TOOLS = frozenset(['Edit', 'Write', 'NotebookEdit', 'MultiEdit'])


def _is_end_turn(rec):
    """True iff this assistant record concluded its turn.

    ``message.stop_reason == 'end_turn'`` is the agent's final spoken
    response of a turn (as opposed to a ``tool_use`` stop that pauses to
    call a tool). Together with the turn-root user prompt it forms the
    ``summary`` tier.
    """
    if not isinstance(rec, dict) or rec.get('type') != 'assistant':
        return False
    msg = rec.get('message')
    return isinstance(msg, dict) and msg.get('stop_reason') == 'end_turn'


def _has_spoken_text(rec):
    """True iff the record carries actual spoken words — a non-empty
    string content or ``text`` part (thinking / tool parts don't count).
    Gates the end_turn → summary promotion: a turn that concludes on a
    thinking-only record has no final spoken response to skim.
    """
    msg = rec.get('message') if isinstance(rec.get('message'), dict) else {}
    content = msg.get('content', '')
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(p, dict) and p.get('type') == 'text'
            and (p.get('text') or '').strip()
            for p in content
        )
    return False


def _is_edit_tool_record(rec):
    """True iff this assistant record carries a tool_use for a
    file-mutating tool (``_EDIT_TOOLS``) — drives the ``edits`` tier."""
    if not isinstance(rec, dict) or rec.get('type') != 'assistant':
        return False
    msg = rec.get('message') if isinstance(rec.get('message'), dict) else {}
    content = msg.get('content')
    if not isinstance(content, list):
        return False
    return any(
        isinstance(p, dict) and p.get('type') == 'tool_use'
        and p.get('name') in _EDIT_TOOLS
        for p in content
    )


def _record_min_level(rec):
    """Minimum detail level at which ``rec`` becomes visible (1-7).

    Whitelist semantics: only records we recognise are promoted to a
    lower (more-visible) tier; recognised noise is pinned at 6 (all);
    a type / subtype / attachment type the recipe does not recognise
    at all falls through to 7 (unknown) — a maintenance view that
    surfaces only genuinely unrecognised record kinds.

      * inbound agent voice (worker reply / peer message,
        delivered or queued)                              → 2
      * ``isMeta`` (injected context; never lived)        → 6
      * turn-root prompt / queued ``enqueue`` / assistant
        ``end_turn`` response with spoken text             → 1  (summary)
      * any other voice (``_is_voice``: text or readable
        thinking)                                          → 2  (voice)
      * empty (signature-only) thinking-only assistant     → 6
      * an Edit/Write/NotebookEdit/MultiEdit tool record   → 3  (edits)
      * other user / assistant machinery (tool_use,
        tool_result)                                       → 4  (tools)
      * ``system/compact_boundary`` (structural marker) and
        ``system/model_refusal_fallback`` (the answer was
        replaced by a safeguards notice)                   → 2
      * ``_LIVE_EXTRAS`` framing (system turn_duration /
        api_error)                                         → 4
      * ``_L3_USEFUL`` curated metadata                    → 5  (detailed)
      * recognised noise (``_KNOWN_*_NOISE``,
        ``_ATTACH_FORMATTERS``, ``_METADATA_TYPES``)       → 6  (all)
      * otherwise                                          → 7  (unknown)
    """
    if not isinstance(rec, dict):
        return 6
    # Inbound agent voice IS lived conversation (another agent talking
    # to this one) — voice tier, checked first: peer messages arrive
    # stamped ``isMeta`` (the gate below would bury them) and queued
    # deliveries are ``queue-operation`` records (the enqueue rule
    # below would promote them to the human prompt tier).
    if _is_inbound_agent_voice(rec):
        return 2
    # Injected context is never part of the lived conversation — it
    # outranks even its own type, so check it first.
    if rec.get('isMeta'):
        return 6
    if _is_voice(rec):
        # The conversation skeleton — the user's prompt and the agent's
        # final (end_turn) SPOKEN response — is the summary tier;
        # intermediate voice (running commentary incl. readable
        # thinking, AskUserQuestion, inter-agent) sits one level up. An
        # end_turn record that is voice only through its thinking has
        # no final spoken words, so it stays at voice. A queued
        # ``enqueue`` is a prompt the user typed while Claude was busy,
        # so it belongs with the prompts (its ``popAll`` consumption
        # echo stays at voice to avoid a dupe).
        if (_is_turn_root(rec)
                or (_is_end_turn(rec) and _has_spoken_text(rec))
                or (rec.get('type') == 'queue-operation'
                    and rec.get('operation') == 'enqueue')):
            return 1
        return 2
    t = rec.get('type')
    # An assistant turn whose only content is empty (signature-only /
    # redacted) thinking carries no readable text — it would render as a
    # bare ``<N parts>`` row — so it sinks to ``all`` (6) rather than
    # cluttering the leaner machinery views.
    if t == 'assistant' and _is_empty_thinking_only(rec):
        return 6
    if t in ('user', 'assistant'):
        # File-mutating tool calls get their own "edits" tier; the rest
        # of a turn's machinery (other tool_use, tool_result,
        # thinking-only with text) is the tools tier.
        if _is_edit_tool_record(rec):
            return 3
        return 4
    if t == 'system':
        sub = rec.get('subtype')
        # Events the reader must see even when skimming voice: the
        # conversation was compacted, or the answer was replaced by a
        # safeguards notice.
        if sub in ('compact_boundary', 'model_refusal_fallback'):
            return 2
        if (t, sub) in _LIVE_EXTRAS:
            return 4
        if (t, sub) in _L3_USEFUL:
            return 5
        return 6 if sub in _KNOWN_SYSTEM_NOISE else 7
    if t == 'attachment':
        a = rec.get('attachment')
        sub = a.get('type') if isinstance(a, dict) else None
        if (t, sub) in _L3_USEFUL:
            return 5
        if sub in _ATTACH_FORMATTERS:
            return 6
        return 7
    if t in _L3_USEFUL:
        return 5
    if t in _METADATA_TYPES or t in _KNOWN_TYPE_NOISE:
        return 6
    return 7


def _passes_filter(item_id, td=None):
    """True iff ``item_id`` should be visible at the current detail level.

    Used by every Item builder to derive ``hidden`` and by the live-tail
    worker to detect when a hidden umbrella has crossed the visibility
    threshold. A row is shown iff its *minimum* level (the lowest
    ``_DETAIL_LEVEL`` it becomes visible at) is ``<= _DETAIL_LEVEL``.

    Predicate body, by id tag:
      * ``('msg', jsonl, n)``    → ``_record_min_level(td.records[n])``
      * ``('prompt', jsonl, n)`` → always (turns open at a turn-root
                                    user prompt, level 1)
      * ``('tool', jsonl, n)``   → its assistant record's min level
                                    (a bare tool_use is level 4, an
                                    edit/write tool level 3), but
                                    promoted to the voice tier (level 2)
                                    when the umbrella wraps a resolvable
                                    subagent (same rule as
                                    ``_tool_umbrella_item``)
      * ``('span', jsonl, n)``   → min level over the span's members
      * ``('agent', …)``         → always (subagent umbrella; we don't
                                    peek into other files at filter time)
      * any other id             → always (be permissive; framework
                                    error rows and ``('trunc', …)``
                                    fillers stay visible)
    """
    if _DETAIL_LEVEL >= 7:
        return True
    if not isinstance(item_id, tuple) or not item_id:
        return True
    tag = item_id[0]
    if tag == 'agent':
        return True
    if tag not in ('msg', 'prompt', 'tool', 'span'):
        return True
    path, n = item_id[1], item_id[2]
    if td is None:
        td = _TREE_CACHE.get(path)
    if td is None:
        return True
    if tag == 'prompt':
        return True
    if tag == 'tool':
        if 0 <= n < len(td.records):
            rec = td.records[n]
            if isinstance(rec, dict):
                # A ``<tool:Agent>`` umbrella that dispatches a
                # resolvable subagent reads as voice-tier (level 2) —
                # dispatch sites keep the same tier as the subagent
                # rows in the top block, same rule that gives them
                # their row stripe in ``_tool_umbrella_item``.
                # Otherwise it carries its assistant record's own min
                # level (a bare tool_use is level 4; an edit/write
                # tool is level 3).
                if _tool_umbrella_wraps_subagent(rec, td):
                    return _DETAIL_LEVEL >= 2
                return _record_min_level(rec) <= _DETAIL_LEVEL
        return True
    if tag == 'span':
        for rec in td.span_records.get(n, []):
            if isinstance(rec, dict) and _record_min_level(rec) <= _DETAIL_LEVEL:
                return True
        return False
    # tag == 'msg'
    if 0 <= n < len(td.records):
        rec = td.records[n]
        if isinstance(rec, dict):
            return _record_min_level(rec) <= _DETAIL_LEVEL
    return True


def _kind_of(obj):
    """Best-effort classifier for one JSONL record.

    Most records map to their raw ``type`` / message role. Three
    refinements for the inter-agent voice channel: an assistant record
    whose salient content is a ``SendMessage`` tool_use is the leader
    voicing a worker (``agent-send``); a ``<task-notification>`` is the
    worker's reply back (``agent-reply``); a peer session's message is
    ``agent-peer``. The inbound two match both the delivered ``user``
    record and its queued ``queue-operation`` echo. Each drives its own
    stripe + tag style — without this, inbound agent voice would
    inherit the human stripe and be mis-attributed.
    """
    if _is_send_message(obj):
        return 'agent-send'
    if _is_task_notification(obj):
        return 'agent-reply'
    if _is_agent_message(obj):
        return 'agent-peer'
    t = obj.get('type')
    if t:
        return t
    msg = obj.get('message') or {}
    if isinstance(msg, dict) and msg.get('role'):
        return msg['role']
    return 'message'


def _row_tag(kind, rec):
    """Row tag for ``rec``.

    Adds ``· <attributionAgent>`` to assistant rows inside subagent
    transcripts so the user can see which sub-type produced the turn
    (e.g. ``assistant · general-purpose``), ``· end_turn`` to the
    turn-concluding responses so the final word(s) of a turn stand out
    in the row list, and ``· thinking`` to running-commentary rows
    (readable thinking). Other record kinds get the bare kind label.
    """
    tag = kind
    if kind == 'assistant' and isinstance(rec, dict):
        attr = rec.get('attributionAgent')
        if attr:
            tag = f'{tag} · {attr}'
        if _is_end_turn(rec):
            tag = f'{tag} · end_turn'
        elif _thinking_text(rec):
            tag = f'{tag} · thinking'
    return tag


def _summarise_message(obj):
    """One-line summary of a JSONL entry, capped at 400 chars.

    Per-type formatters peel off the salient field without re-parsing
    the record — every input field comes from ``obj`` directly so this
    stays O(line) inside ``_list_messages``.
    """
    t = obj.get('type', '')

    # Inbound agent voice — delivered (``user`` record) or queued
    # (``queue-operation``) — summarises off the parsed wrapper fields,
    # not the raw XML. Checked ahead of the per-type formatters so the
    # queued echo reads like the delivered record.
    if _is_task_notification(obj):
        d = _parse_task_notification(_inbound_voice_text(obj) or '')
        bits = [b for b in (d['task_id'], d['status'], d['summary']) if b]
        return '← ' + ' · '.join(bits) if bits else '← task-notification'
    if _is_agent_message(obj):
        sender, body = _parse_agent_message(obj)
        body = _summarise_json_body(body) or _trim(body, 5000)
        return f'← {sender}: {body}' if sender else f'← {body}'

    # Metadata records — surface the salient field.
    if t == 'last-prompt':
        prompt = _trim(obj.get('lastPrompt', ''), 5000)
        return f'last-prompt: {prompt}' if prompt else 'last-prompt'
    if t == 'permission-mode':
        return f'permission-mode: {obj.get("permissionMode", "")}'
    if t == 'mode':
        return f'mode: {obj.get("mode", "")}'
    if t == 'ai-title':
        return f'ai-title: {obj.get("aiTitle", "")}'
    if t == 'custom-title':
        return f'custom-title: {obj.get("customTitle", "")}'
    if t == 'tag':
        return f'tag: {obj.get("tag", "")}'
    if t == 'agent-name':
        return f'agent-name: {obj.get("agentName", "")}'
    if t == 'agent-color':
        return f'agent-color: {obj.get("agentColor", "")}'
    if t == 'agent-setting':
        return f'agent-setting: {obj.get("agentSetting", "")}'
    if t == 'task-summary':
        s = _trim(obj.get('summary', ''), 5000)
        return f'task-summary: {s}'
    if t == 'summary':
        s = _trim(obj.get('summary', ''), 5000)
        return f'summary: {s}'
    if t == 'pr-link':
        return f'PR {obj.get("prRepository", "")}#{obj.get("prNumber", "")}'
    if t == 'worktree-state':
        ws = obj.get('worktreeSession')
        if ws is None:
            return 'worktree: (exited)'
        if isinstance(ws, dict):
            return (f'worktree: {ws.get("worktreePath", "")}'
                    f' @ {ws.get("worktreeBranch", "")}')
        return 'worktree-state'
    if t == 'speculation-accept':
        ms = obj.get('timeSavedMs', 0)
        return f'speculation-accept: +{ms/1000:.1f}s saved'
    if t == 'queue-operation':
        c = _trim(obj.get('content', ''), 5000)
        return f'queue {obj.get("operation", "?")}: {c}'
    if t == 'file-history-snapshot':
        snap = obj.get('snapshot') or {}
        n = len(snap) if isinstance(snap, dict) else '?'
        return (f'file-history-snapshot: {n} files'
                + (' (update)' if obj.get('isSnapshotUpdate') else ''))
    if t == 'attribution-snapshot':
        states = obj.get('fileStates') or {}
        return f'attribution-snapshot: {len(states)} files'
    if t == 'content-replacement':
        rep = obj.get('replacements') or []
        return f'content-replacement: {len(rep)} blocks'
    if t == 'marble-origami-commit':
        s = _trim(obj.get('summary', ''), 5000)
        return f'collapse-commit: {s}'
    if t == 'marble-origami-snapshot':
        return f'collapse-snapshot: {len(obj.get("staged") or [])} staged'
    if t == 'progress':
        d = obj.get('data') or {}
        dt = d.get('type', '?') if isinstance(d, dict) else '?'
        return f'progress: {dt}'
    if t == 'cost-state':
        cost = obj.get('totalCostUSD') or 0
        return (f'💰 ${cost:.2f} · {obj.get("totalLinesAdded", 0)}+'
                f'/{obj.get("totalLinesRemoved", 0)}−')
    if t == 'atis-latch':
        return 'atis-latch'

    # System events.
    if t == 'system':
        sub = obj.get('subtype', '')
        if sub == 'turn_duration':
            ms = obj.get('durationMs', 0)
            return f'⏱ turn {ms/1000:.2f}s · {obj.get("messageCount", 0)} msgs'
        if sub == 'api_error':
            return f'‼ api_error'
        if sub == 'local_command':
            cmd = _trim(obj.get('content') or obj.get('command', ''), 5000)
            return f'$ {cmd}' if cmd else 'local_command'
        if sub == 'compact_boundary':
            trig = (obj.get('compactMetadata') or {}).get('trigger', '')
            base = obj.get('content') or 'Conversation compacted'
            return f'♻ {base}' + (f' ({trig})' if trig else '')
        if sub == 'model_refusal_fallback':
            return f'⚠ refusal fallback: {_trim(obj.get("content", ""), 5000)}'
        if sub == 'away_summary':
            return f'away: {_trim(obj.get("content", ""), 5000)}'
        if sub == 'informational':
            return f'ℹ {_trim(obj.get("content", ""), 5000)}'
        if sub == 'agents_killed':
            return '■ agents killed'
        return f'system: {sub}' if sub else 'system'

    # Attachment — pick the most salient field per subtype.
    if t == 'attachment':
        a = obj.get('attachment') if isinstance(obj.get('attachment'), dict) else {}
        sub = a.get('type', '?')
        if sub == 'file':
            return f'📎 file: {a.get("displayPath", a.get("filename", ""))}'
        if sub == 'queued_command':
            p = _trim(a.get('prompt', ''), 5000)
            return f'📥 queued: {p}'
        if sub == 'hook_success':
            ok = a.get('exitCode', 0) == 0
            return (f'{"✓" if ok else "✗"} hook {a.get("hookName", "?")} '
                    f'({a.get("hookEvent", "?")})')
        if sub == 'hook_additional_context':
            return f'+ ctx from {a.get("hookName", "?")}'
        if sub == 'task_reminder':
            return f'🔔 reminder ({a.get("itemCount", 0)})'
        if sub == 'skill_listing':
            return f'📚 skills: {a.get("skillCount", 0)}'
        if sub == 'mcp_instructions_delta':
            return (f'mcp Δ: +{len(a.get("addedNames") or [])}'
                    f' -{len(a.get("removedNames") or [])}')
        if sub == 'deferred_tools_delta':
            return (f'tools Δ: +{len(a.get("addedNames") or [])}'
                    f' -{len(a.get("removedNames") or [])}')
        if sub == 'date_change':
            return f'📅 date: {a.get("newDate", "?")}'
        if sub == 'diagnostics':
            n = len(a.get('files') or [])
            return f'diagnostics: {n} files'
        if sub == 'command_permissions':
            return f'allowed: {", ".join(a.get("allowedTools") or [])}'
        if sub == 'ultrathink_effort':
            return '🧠 ultrathink'
        if sub == 'total_tokens_reminder':
            n = _total_tokens_left(a)
            return (f'⛽ {_human_count(n)} tokens left' if n is not None
                    else '⛽ tokens left')
        if sub == 'agent_listing_delta':
            return (f'agents Δ: +{len(a.get("addedTypes") or [])}'
                    f' -{len(a.get("removedTypes") or [])}')
        if sub == 'batching_reminder_sent':
            return '🔔 batching reminder'
        if sub == 'bash_output_audience_note':
            return 'bash output audience note'
        if sub == 'nested_memory':
            return f'📎 memory: {a.get("displayPath", a.get("path", ""))}'
        if sub == 'read_truncation_notice':
            return f'✂ {_trim(a.get("banner", ""), 5000)}'
        if sub in ('auto_mode', 'plan_mode'):
            return f'⇆ {sub.replace("_", " ")}'
        if sub == 'silent_turn_reminder':
            return f'🔔 {_trim(a.get("text", ""), 5000)}'
        if sub == 'already_read_file':
            return (f'📎 already read: '
                    f'{a.get("displayPath", a.get("filename", ""))}')
        if sub == 'hook_cancelled':
            return (f'✗ hook {a.get("hookName", "?")} cancelled '
                    f'({a.get("hookEvent", "?")})')
        if sub == 'invoked_skills':
            names = [s.get('name', '?') for s in a.get('skills') or []
                     if isinstance(s, dict)]
            return f'📚 invoked: {", ".join(names)}'
        return f'📎 {sub}'

    # User / assistant — peek into content for salient fragment.
    msg = obj.get('message') if isinstance(obj.get('message'), dict) else {}
    role = msg.get('role') or obj.get('role') or t or ''
    content = msg.get('content', obj.get('content', ''))

    # Assistant with only tool_use parts → format as the tool call.
    if role == 'assistant' and isinstance(content, list):
        only_tool = [p for p in content
                     if isinstance(p, dict) and p.get('type') == 'tool_use']
        any_text = any(isinstance(p, dict) and p.get('type') == 'text'
                       and (p.get('text') or '').strip()
                       for p in content)
        # Readable thinking with no spoken text → running commentary;
        # surface it under the thinking marker (spoken text wins).
        if not any_text:
            think = _thinking_text(obj)
            if think:
                return f'🧠 thinking: {_trim(think, 5000)}'
        if only_tool and not any_text:
            tu = only_tool[0]
            name = tu.get('name', '?')
            inp = tu.get('input') or {}
            extra = _tool_use_one_line(name, inp)
            return f'🔧 {name}({extra})' if extra else f'🔧 {name}'

    # User with tool_result → distinct prefix + a glance at the output.
    if role == 'user' and isinstance(content, list):
        for p in content:
            if isinstance(p, dict) and p.get('type') == 'tool_result':
                return f'↳ tool_result: {_tool_result_one_line(obj, p)}'

    text = _extract_text(content)
    text = _trim(text, 5000)
    if obj.get('isMeta') and role:
        role = f'{role}/meta'
    if role:
        return f'{role}: {text}' if text else role
    return text or '(empty)'


def _tool_use_one_line(name, inp):
    """Compact one-line summary of a tool's input for the row title."""
    if name == 'Bash':
        return _trim(inp.get('command', ''), 5000)
    if name in ('Read', 'Edit', 'Write'):
        return inp.get('file_path', '')
    if name == 'Grep':
        return f'/{inp.get("pattern", "")}/'
    if name == 'Glob':
        return inp.get('pattern', '')
    if name in ('Agent', 'Task'):
        return inp.get('subagent_type', '?')
    if name == 'Skill':
        return inp.get('skill', '?')
    if name == 'SendMessage':
        recipient, summary, _ = _send_message_fields(inp)
        return f'→ {recipient}: {summary}'
    return ''


def _tool_result_one_line(obj, part):
    """One-liner peek at a tool_result for the row title."""
    tur = obj.get('toolUseResult')
    if isinstance(tur, dict):
        if 'stdout' in tur:
            return _trim(tur.get('stdout', ''), 5000) or '(no output)'
        if 'numFiles' in tur:
            return f'{tur["numFiles"]} files'
        if 'agentType' in tur:
            return f'{tur["agentType"]} → {tur.get("status", "?")}'
        if 'taskId' in tur:
            sc = tur.get('statusChange') or {}
            return f'#{tur["taskId"]} {sc.get("from", "?")}→{sc.get("to", "?")}'
        if 'commandName' in tur:
            return tur['commandName']
    if isinstance(tur, str):
        return _trim(tur, 5000)
    # Fall back to the raw content text.
    content = part.get('content')
    if isinstance(content, str):
        return _trim(content, 5000)
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict) and p.get('type') == 'text':
                return _trim(p.get('text', ''), 5000)
    return ''


def _extract_text(content):
    """Pull the first useful text fragment out of a content payload.

    Claude Code stores message content in three flavours:
      * a bare string — return as-is.
      * a list of parts, each a dict with ``type=text|thinking|
        tool_use|tool_result`` — return the first ``text`` part's text,
        falling back to a ``[<type>]`` placeholder for non-text parts so
        the user can at least see *what* the message contained.
      * anything else — best-effort ``str()``.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get('type')
            if ptype == 'text':
                return part.get('text', '') or ''
            if ptype == 'thinking':
                # Thinking blocks are usually long; show a marker so the
                # user knows the turn was a thinking-only step before any
                # tool_use parts.
                continue
            if ptype == 'tool_use':
                name = part.get('name', '?')
                return f'[tool_use {name}]'
            if ptype == 'tool_result':
                return f'[tool_result]'
        # Nothing matched — describe the shape rather than the content.
        return f'<{len(content)} parts>'
    if content is None:
        return ''
    return str(content)


# ---- preview rendering ---------------------------------------------------
#
# Per-line preview is built by:
#
#     parse JSONL line  ->  _classify(obj) -> renderer key
#                       ->  _RENDERERS[key](obj) -> body str
#                       ->  body + _fmt_chrome(obj)
#
# Renderers are flat free functions. Each takes the parsed obj and
# returns a ready-to-display string with ANSI inlined via the palette
# constants above. No abstractions: changing a color means editing the
# constant; adding a type means writing _render_X and registering it.


# Color associated with the row tag for each kind (mirrors what the row
# title uses, but applied inside the preview header). Single source of
# truth for the kind→color decision.
_KIND_HEADER_COLOR = {
    'user':            CYAN,
    'assistant':       GREEN,
    'attachment':      BLUE,
    'system':          GRAY,
    'last-prompt':     MAGENTA,
    'permission-mode': YELLOW,
    'pr-link':         BLUE,
    'mode':            RED,
    'speculation-accept': GREEN,
    'queue-operation': CYAN,
}

_STOP_COLOR = {
    'end_turn':      GREEN,
    'tool_use':      YELLOW,
    'max_tokens':    RED,
    'stop_sequence': MUTE,
    'pause_turn':    YELLOW,
}

_PERM_COLOR = {
    'default':           MUTE,
    'plan':              BLUE,
    'acceptEdits':       YELLOW,
    'bypassPermissions': RED,
}


def _read_jsonl_line(path, n):
    """Return the parsed object for line ``n`` of ``path`` or ``None``.

    Serves from ``_TREE_CACHE[path].records[n]`` when the file has
    been scanned (every read path now triggers ``_scan_tree``), so
    previews are O(1) dict lookups rather than O(file_size) line
    iterations. Falls back to a one-off file read for paths the
    scanner hasn't visited yet (rare — covers transient setups).
    ``None`` for missing files, out-of-range lines, blank lines, and
    lines whose JSON failed to parse — re-parsing wouldn't change
    the answer, and the caller surfaces an empty preview either way.
    """
    cached = _TREE_CACHE.get(path)
    if cached is not None:
        if 0 <= n < len(cached.records):
            return cached.records[n]
        return None
    # Fallback: file not yet scanned. Cheaper than a full scan when
    # all we need is one line, but should be hit only at startup
    # races / unusual paths.
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for i, line in enumerate(f):
                if i == n:
                    line = line.rstrip('\n')
                    if not line.strip():
                        return None
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        return None
    except OSError:
        return None
    return None


_METADATA_TYPES = frozenset([
    'summary', 'ai-title', 'custom-title', 'last-prompt', 'task-summary',
    'tag', 'agent-name', 'agent-color', 'agent-setting', 'pr-link',
    'mode', 'permission-mode', 'worktree-state', 'content-replacement',
    'file-history-snapshot', 'attribution-snapshot', 'speculation-accept',
    'queue-operation', 'marble-origami-commit', 'marble-origami-snapshot',
    'progress', 'cost-state', 'atis-latch',
])


def _classify(obj):
    """Map a parsed JSONL record to a renderer key."""
    if not isinstance(obj, dict):
        return 'unknown'
    t = obj.get('type')
    if t == 'user':       return 'user'
    if t == 'assistant':  return 'assistant'
    if t == 'attachment': return 'attachment'
    if t == 'system':     return 'system'
    if t in _METADATA_TYPES:
        return 'metadata'
    return 'unknown'


# ---- per-record renderers ------------------------------------------------


def _render_user(obj, bg='0'):
    """User turn: plain prompt, tool_result, or synthetic meta entry.

    ``bg`` is the md2ansi reset baseline (raw SGR params) for the prompt
    body — ``'0'`` (plain) for every caller except the composed-preview
    turn-root path, which passes ``BLUE_BG_PARAMS`` so the prompt's voice
    markdown stays on the dark-blue band.
    """
    msg = obj.get('message') if isinstance(obj.get('message'), dict) else {}
    content = msg.get('content', '')
    is_meta = bool(obj.get('isMeta'))

    # Tool-result branch — an item in content has type=='tool_result'.
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get('type') == 'tool_result':
                return _render_tool_result(obj, part)

    # Inbound worker→leader reply — a small header + the ``<result>``
    # markdown, instead of dumping the raw XML wrapper as a prompt.
    if _is_task_notification(obj):
        return _render_task_notification(_extract_text(content))

    # Inbound peer message — sender header + the wrapped markdown body.
    if _is_agent_message(obj):
        return _render_agent_message(obj)

    head = f'{WHITE}{BOLD}▶ user{RESET}'
    chips = []
    if is_meta:
        chips.append(f'{MUTE}[meta]{RESET}')
    pmode = obj.get('permissionMode')
    if pmode:
        chips.append(f'{_PERM_COLOR.get(pmode, MUTE)}[{pmode}]{RESET}')
    out = ['  '.join([head] + chips)]
    out.append('')
    out.append(_user_body_text(content, bg))
    return '\n'.join(out)


def _user_body_text(content, bg='0'):
    """Render the textual portion of a user content payload.

    ``bg`` threads to ``_md_voice`` as md2ansi's ``current_style`` — see
    ``_render_user``.
    """
    if isinstance(content, str):
        return _md_voice(content, bg)
    if isinstance(content, list):
        chunks = []
        for part in content:
            if not isinstance(part, dict):
                continue
            t = part.get('type')
            if t == 'text':
                chunks.append(part.get('text', '') or '')
            # tool_result handled in _render_tool_result; tool_use not
            # expected on a user record.
        return _md_voice('\n\n'.join(c for c in chunks if c), bg)
    return str(content) if content is not None else ''


def _render_task_notification(text):
    """Inbound worker→leader reply: a status/summary/output-file header
    then the ``<result>`` through ``_render_message_body``."""
    d = _parse_task_notification(text)
    head = f'{CYAN}{BOLD}← task-notification{RESET}'
    if d['task_id']:
        head += f'  {MUTE}{d["task_id"]}{RESET}'
    out = [head]
    if d['status']:
        out.append(f'{MUTE}status:{RESET} {d["status"]}')
    if d['summary']:
        out.append(f'{MUTE}summary:{RESET} {d["summary"]}')
    if d['output_file']:
        out.append(f'{MUTE}output:{RESET} {d["output_file"]}')
    if d['result']:
        out.append('')
        out.append(_render_message_body(d['result']))
    return '\n'.join(out)


def _render_agent_message(obj):
    """Inbound peer message (another session's SendMessage): a sender
    header, then the wrapped body — field-wise when the body is a JSON
    object (idle notifications, protocol messages), markdown otherwise."""
    sender, body = _parse_agent_message(obj)
    head = f'{GREEN}{BOLD}← agent-message{RESET}'
    if sender:
        head += f'  {MUTE}from{RESET} {sender}'
    out = [head]
    if body:
        out.append('')
        out.append(_render_message_body(body))
    return '\n'.join(out)


def _render_assistant(obj):
    """Assistant turn: text / thinking / tool_use parts in source order.

    The ``message.usage`` block (input / output / cache tokens) is
    emitted by ``_fmt_chrome`` so all per-message metadata stays in
    one place.
    """
    msg = obj.get('message') if isinstance(obj.get('message'), dict) else {}
    parts = msg.get('content') if isinstance(msg.get('content'), list) else []
    model = msg.get('model', '?')
    stop  = msg.get('stop_reason') or ''

    # ⏺ matches Claude Code's own BLACK_CIRCLE convention for assistant
    # turns (src/constants/figures.ts). User turns keep ▶ for visual
    # contrast at a glance.
    head = f'{WHITE}{BOLD}⏺ assistant{RESET}  {MAGENTA}{model}{RESET}'
    if stop:
        head += f'  {_STOP_COLOR.get(stop, MUTE)}[{stop}]{RESET}'
    out = [head, '']

    for part in parts:
        if not isinstance(part, dict):
            continue
        t = part.get('type')
        if t == 'text':
            out.append(_md_voice(part.get('text', '') or ''))
            out.append('')
        elif t == 'thinking':
            text = part.get('thinking', '') or ''
            if not text.strip():
                continue   # signature-only/empty thinking — silent skip
            out.append(_rule('thinking'))
            out.append(f'{THINK}{text}{RESET}')
            out.append('')
        elif t == 'tool_use':
            out.append(_fmt_tool_use_part(part))
            out.append('')

    return '\n'.join(out).rstrip() + '\n'


# ---- tool umbrella previews ----------------------------------------------
#
# A ``<tool:Name>`` umbrella row's preview defaults to a raw concatenation
# of its child leaf renders (the assistant ``tool_use`` block, then each
# ``tool_result`` block — the same machinery as every other umbrella, via
# ``_collect_umbrella_preview``). For a handful of tools that pair a request
# with a result we can do better: render ONE purpose-built block that fuses
# the two (e.g. Bash as a terminal session, Edit as just the resulting diff).
#
# ``_TOOL_UMBRELLA_FORMATTERS`` maps a tool name to ``fn(inp, tur,
# raw_content, is_error) -> str | None``. The dispatcher takes the custom
# path only when EVERY tool_use part in the umbrella has a registered
# formatter AND every formatter returns a non-None string; any gap (an
# unregistered tool, an in-flight part with no result yet, a formatter that
# declines) falls back to the plain cascade. Formatters live next to their
# matching ``_fmt_tool_use_*`` request formatter and append themselves to
# this registry; it starts empty and per-tool tickets fill it in.

_TOOL_UMBRELLA_FORMATTERS = {}


def _render_tool_umbrella(item_id):
    """One purpose-built combined block for a ``('tool', path, line)`` row.

    Resolves the umbrella's assistant record, enumerates its ``tool_use``
    parts, pairs each with its ``tool_result`` by ``tool_use_id``, and asks
    the registered formatter for each part to fuse request + result. Returns
    the joined blocks ONLY when every part is registered and every formatter
    returns a non-None string; any gap (unregistered tool, in-flight part
    with no result, a declining formatter) returns ``None`` so the caller
    falls back to the plain ``_collect_umbrella_preview`` cascade.

    Defensive throughout: any malformed shape / missing key returns ``None``.
    """
    try:
        _tag, jsonl_path, assistant_line = item_id
    except (ValueError, TypeError):
        return None
    try:
        td = _scan_tree(jsonl_path)
    except Exception:
        return None
    if not (0 <= assistant_line < len(td.records)):
        return None
    rec = td.records[assistant_line]
    if not isinstance(rec, dict):
        return None
    msg = rec.get('message') or {}
    content = msg.get('content') if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return None
    tool_use_parts = [
        p for p in content
        if isinstance(p, dict) and p.get('type') == 'tool_use'
    ]
    if not tool_use_parts:
        return None

    # Index this umbrella's tool_result parts by their tool_use_id. Each
    # result record carries one (occasionally more) tool_result content
    # part; map id -> (tur, raw_content, is_error) for O(1) pairing.
    results = {}
    for child in td.tool_children.get(rec.get('uuid'), []):
        if not isinstance(child, dict):
            continue
        c_msg = child.get('message') or {}
        c_content = c_msg.get('content') if isinstance(c_msg, dict) else None
        if not isinstance(c_content, list):
            continue
        for part in c_content:
            if not isinstance(part, dict) or part.get('type') != 'tool_result':
                continue
            tu_id = part.get('tool_use_id')
            if tu_id is None or tu_id in results:
                continue
            results[tu_id] = (
                child.get('toolUseResult'),
                part.get('content'),
                bool(part.get('is_error')),
            )

    blocks = []
    for part in tool_use_parts:
        name = part.get('name')
        formatter = _TOOL_UMBRELLA_FORMATTERS.get(name)
        is_mcp = formatter is None and isinstance(name, str) \
            and name.startswith('mcp__')
        if formatter is None and not is_mcp:
            return None  # an unregistered tool — defer to the cascade
        inp = part.get('input') or {}
        tur, raw_content, is_error = results.get(part.get('id'), (None, None, False))
        try:
            # MCP tool names are dynamic (``mcp__<server>__<tool>``) so they
            # can't be registry keys; route them to the one MCP formatter,
            # which needs ``name`` for its header. Registered tools keep the
            # standard ``(inp, tur, raw_content, is_error)`` contract.
            if is_mcp:
                block = _fmt_tool_umbrella_mcp(
                    name, inp, tur, raw_content, is_error)
            else:
                block = formatter(inp, tur, raw_content, is_error)
        except Exception:
            return None
        if block is None:
            return None
        blocks.append(block)
    return '\n\n'.join(blocks)


# ---- tool_use rendering --------------------------------------------------


def _fmt_tool_use_part(part):
    """One ``tool_use`` content part — header + per-tool body."""
    name = part.get('name', '?')
    inp  = part.get('input') or {}
    head = f'{YELLOW}{BOLD}🔧 {name}{RESET}'

    formatter = _TOOL_USE_FORMATTERS.get(name)
    body = formatter(inp) if formatter else _fmt_tool_use_default(inp)
    if not body:
        return head
    return head + '\n' + body


def _fmt_tool_use_default(inp):
    """JSON dump of the tool input, dimmed."""
    if not inp:
        return ''
    try:
        text = json.dumps(inp, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(inp)
    return f'{GRAY}{text}{RESET}'


def _fmt_tool_use_bash(inp):
    cmd = inp.get('command', '')
    desc = inp.get('description', '')
    timeout = inp.get('timeout')
    bg = inp.get('run_in_background')
    out = []
    if desc:
        out.append(f'{MUTE}# {desc}{RESET}')
    out.append(f'{WHITE}$ {cmd}{RESET}')
    extras = []
    if timeout: extras.append(f'timeout={timeout}ms')
    if bg:      extras.append('background')
    if extras:
        out.append(f'{MUTE}({" ".join(extras)}){RESET}')
    return '\n'.join(out)


def _fmt_tool_umbrella_bash(inp, tur, raw_content, is_error):
    """Bash umbrella — a natural terminal session.

    The command flows straight into its output with no
    ``assistant`` / ``tool_result`` chrome: an optional ``# description``,
    then the ``$ command`` painted on a dark-grey band (``CMD_BG``) so it
    reads as a distinct prompt, then the result body. The body comes from
    the structured ``toolUseResult`` (``_fmt_tur_bash`` — keeps the
    interrupted / background / persisted lines) when present, else from the
    raw ``tool_result`` content as a fallback. With no result at all we
    still render the ``$ command`` block so an in-flight command reads
    naturally — so this formatter never declines for Bash.
    """
    cmd = inp.get('command', '')
    desc = inp.get('description', '')
    timeout = inp.get('timeout')
    bg = inp.get('run_in_background')
    out = []
    if desc:
        out.append(f'{MUTE}# {desc}{RESET}')
    # Syntax-color the WHOLE command once (correct multi-line lexing),
    # then paint each physical line on CMD_BG so it reads as one banded
    # prompt block. ``$ `` leads the first line; continuations are
    # indented two spaces to align under it. Re-asserting CMD_BG per
    # line keeps the grey band gap-free over the leading prefix, and
    # ``_bash_color`` already preserves the band across token resets so
    # there are no holes mid-line either.
    colored = _bash_color(cmd)
    cmd_lines = colored.split('\n') or ['']
    for i, line in enumerate(cmd_lines):
        prefix = '$ ' if i == 0 else '  '
        out.append(f'{CMD_BG}{prefix}{line}{RESET}')
    extras = []
    if timeout: extras.append(f'timeout={timeout}ms')
    if bg:      extras.append('background')
    if extras:
        out.append(f'{MUTE}({" ".join(extras)}){RESET}')
    if isinstance(tur, dict):
        result = _fmt_tur_bash(tur)
        if result:
            out.append(result)
    elif raw_content:
        # No structured toolUseResult but the raw tool_result content is
        # there — surface it so the command's output still shows.
        result = _fmt_tur_raw_content(raw_content)
        if result:
            out.append(result)
    return '\n'.join(out)


_TOOL_UMBRELLA_FORMATTERS['Bash'] = _fmt_tool_umbrella_bash


def _fmt_tool_use_read(inp):
    path = inp.get('file_path', '')
    offset = inp.get('offset')
    limit = inp.get('limit')
    suffix = ''
    if offset is not None or limit is not None:
        suffix = f'  {MUTE}[{offset or 0}..+{limit or "eof"}]{RESET}'
    return f'{CYAN}{path}{RESET}{suffix}'


def _fmt_tool_umbrella_read(inp, tur, raw_content, is_error):
    """Read umbrella — ONE header line, NO file content.

    The file body lives on the expanded tool_result leaf; here we just
    confirm the read happened with its path, range, and line count. Declines
    (returns ``None``) when there's no resolved file (in-flight / error) so
    the cascade shows the bare request instead.
    """
    if not isinstance(tur, dict):
        return None
    f = tur.get('file')
    if not isinstance(f, dict):
        return None
    path = inp.get('file_path', '')
    offset = inp.get('offset')
    limit = inp.get('limit')
    num_lines = f.get('numLines', '?')
    rng = f'[{offset or 0}..+{limit or "eof"}]'
    return (f'{YELLOW}{BOLD}🔧 Read{RESET}  {CYAN}{path}{RESET}  '
            f'{MUTE}{rng}{RESET}  {MUTE}·{RESET}  {MUTE}{num_lines} lines{RESET}')


_TOOL_UMBRELLA_FORMATTERS['Read'] = _fmt_tool_umbrella_read


def _fmt_tool_use_edit(inp):
    path = inp.get('file_path', '')
    old = inp.get('old_string', '')
    new = inp.get('new_string', '')
    out = [f'{CYAN}{path}{RESET}']
    if inp.get('replace_all'):
        out.append(f'{MUTE}(replace_all){RESET}')
    if old or new:
        out.append(_rule('change'))
        for line in old.splitlines() or ['']:
            out.append(f'{RED}- {line}{RESET}')
        for line in new.splitlines() or ['']:
            out.append(f'{GREEN}+ {line}{RESET}')
    return '\n'.join(out)


def _structured_patch_to_unified(patch, file_path):
    """Reconstruct unified-diff TEXT from a ``structuredPatch`` + path.

    ``patch`` is the ``toolUseResult.structuredPatch`` list (one dict per
    hunk: ``oldStart``/``newStart``, optional ``oldLines``/``newLines``,
    and ``lines`` already prefixed ' '/'-'/'+'). Emits a plain (uncolored)
    diff suitable for ``_colorize_diff``. ``oldLines``/``newLines`` are
    derived from the hunk lines when absent. Returns ``None`` when there's
    nothing to render (no hunks).
    """
    if not isinstance(patch, list) or not patch:
        return None
    fp = file_path or ''
    out = [f'--- a/{fp}', f'+++ b/{fp}']
    emitted_hunk = False
    for hunk in patch:
        if not isinstance(hunk, dict):
            continue
        lines = hunk.get('lines') or []
        if not isinstance(lines, list):
            continue
        old_start = hunk.get('oldStart', 0)
        new_start = hunk.get('newStart', 0)
        old_lines = hunk.get('oldLines')
        new_lines = hunk.get('newLines')
        if old_lines is None:
            old_lines = sum(1 for ln in lines if not ln.startswith('+'))
        if new_lines is None:
            new_lines = sum(1 for ln in lines if not ln.startswith('-'))
        out.append(f'@@ -{old_start},{old_lines} +{new_start},{new_lines} @@')
        out.extend(lines)
        emitted_hunk = True
    if not emitted_hunk:
        return None
    return '\n'.join(out) + '\n'


def _fmt_tool_umbrella_edit(inp, tur, raw_content, is_error):
    """Edit umbrella — ONLY the resulting diff (the requested old/new is dropped).

    Reconstructs a unified diff from ``toolUseResult.structuredPatch`` and
    pipes it through ``delta`` (``_colorize_diff``). When ``delta`` is absent
    we reuse the existing git-style coloring of ``_fmt_tur_edit``. Declines
    (returns ``None``) when there's no ``structuredPatch`` (e.g. an error
    result) so the cascade renders the request instead.
    """
    if not isinstance(tur, dict):
        return None
    patch = tur.get('structuredPatch')
    if not isinstance(patch, list) or not patch:
        return None
    if not HAVE_DELTA:
        return _fmt_tur_edit(tur)
    unified = _structured_patch_to_unified(patch, tur.get('filePath'))
    if unified is None:
        return None
    return _colorize_diff(unified)


_TOOL_UMBRELLA_FORMATTERS['Edit'] = _fmt_tool_umbrella_edit


def _fmt_tool_use_write(inp):
    path = inp.get('file_path', '')
    content = inp.get('content', '')
    return f'{CYAN}{path}{RESET}\n{GRAY}{content}{RESET}'


# Cap on the content lines shown inline in a Write / NotebookEdit umbrella
# preview. The full content stays available on the expanded tool_use leaf.
_WRITE_PREVIEW_LINES = 10


def _cap_content_lines(content, item_label):
    """GRAY-wrapped content capped at ``_WRITE_PREVIEW_LINES`` + a footer.

    Returns the (possibly truncated) body. When truncated, appends a muted
    ``… (N more lines — expand the <item_label> item to see the full
    content)`` line pointing the reader at the expandable leaf.
    """
    lines = content.split('\n')
    head = lines[:_WRITE_PREVIEW_LINES]
    out = [f'{GRAY}{chr(10).join(head)}{RESET}']
    rest = len(lines) - _WRITE_PREVIEW_LINES
    if rest > 0:
        out.append(f'{MUTE}… ({rest} more lines — expand the {item_label} '
                   f'item to see the full content){RESET}')
    return '\n'.join(out)


def _fmt_tool_umbrella_write(inp, tur, raw_content, is_error):
    """Write umbrella — header + a capped raw-content preview.

    One header line (path + line count), then up to ``_WRITE_PREVIEW_LINES``
    lines of the request content (GRAY, raw — NOT an all-additions diff),
    then a footer when truncated. Declines (returns ``None``) when the
    request carries no ``content`` so the cascade renders instead.
    """
    content = inp.get('content')
    if not content:
        return None
    path = inp.get('file_path', '')
    n = len(content.split('\n'))
    header = (f'{YELLOW}{BOLD}🔧 Write{RESET}  {CYAN}{path}{RESET}  '
              f'{MUTE}·{RESET}  {MUTE}{n} lines{RESET}')
    return header + '\n' + _cap_content_lines(content, 'Write')


_TOOL_UMBRELLA_FORMATTERS['Write'] = _fmt_tool_umbrella_write


def _fmt_tool_umbrella_notebook_edit(inp, tur, raw_content, is_error):
    """NotebookEdit umbrella — Jupyter cell edit, closest to Edit.

    Header names the notebook + the cell being touched
    (``[<edit_mode> <cell_type> cell <cell_id>]``). The body renders a diff
    via the SAME path as Edit when ``toolUseResult`` carries a
    ``structuredPatch``; otherwise it shows ``new_source`` capped like Write.
    Declines (returns ``None``) when neither a patch nor ``new_source`` is
    available (e.g. delete-mode with no structured result) so the cascade
    renders instead.

    NOTE: no NotebookEdit transcript was available to confirm the
    ``toolUseResult`` shape, so the patch branch is best-effort — it keys off
    the same ``structuredPatch`` field every Edit/Write result uses; if a
    real notebook result names it differently we simply fall back to the
    ``new_source`` view (and ultimately to the cascade).
    """
    notebook_path = inp.get('notebook_path', '')
    cell_id = inp.get('cell_id', '')
    cell_type = inp.get('cell_type', '')
    edit_mode = inp.get('edit_mode', 'replace')
    new_source = inp.get('new_source')

    chips = ' '.join(s for s in (edit_mode, cell_type) if s)
    cell_ref = f'cell {cell_id}' if cell_id else 'cell'
    tag = f'[{chips} {cell_ref}]' if chips else f'[{cell_ref}]'
    header = (f'{YELLOW}{BOLD}🔧 NotebookEdit{RESET}  '
              f'{CYAN}{notebook_path}{RESET}  {MUTE}{tag}{RESET}')

    patch = tur.get('structuredPatch') if isinstance(tur, dict) else None
    if isinstance(patch, list) and patch:
        unified = _structured_patch_to_unified(
            patch, tur.get('filePath') or notebook_path)
        if unified is not None:
            body = _colorize_diff(unified) if HAVE_DELTA else _fmt_tur_edit(tur)
            return header + '\n' + body
    if new_source:
        return header + '\n' + _cap_content_lines(new_source, 'NotebookEdit')
    return None


_TOOL_UMBRELLA_FORMATTERS['NotebookEdit'] = _fmt_tool_umbrella_notebook_edit


def _fmt_tool_umbrella_mcp(name, inp, tur, raw_content, is_error):
    """MCP-tool umbrella — ``🔧 <server> ▸ <tool>`` + JSON request / result.

    MCP tool names are ``mcp__<server>__<tool>`` (the tool segment may itself
    contain ``__``). The dispatcher routes any ``mcp__``-prefixed part here —
    note this formatter takes ``name`` as its first arg (the others receive
    just ``inp``) since it needs the name for the header. The request is the
    raw ``input`` JSON (colored); the result is colored when it parses as
    JSON, else passed through plain. Declines (returns ``None``) on a
    malformed name with fewer than three ``__`` segments so the cascade
    handles it.
    """
    parts = name.split('__')
    if len(parts) < 3:
        return None
    server = parts[1]
    tool = '__'.join(parts[2:])
    out = [f'{YELLOW}{BOLD}🔧 {server} ▸ {tool}{RESET}']
    if inp:
        out.append('')
        out.append(_json_color(json.dumps(inp, indent=2, ensure_ascii=False)))
    text = tur if isinstance(tur, str) else _fmt_tur_raw_content(raw_content)
    if text:
        colored = _maybe_json_color(text)
        out.append('')
        out.append(colored if colored is not None else text)
    return '\n'.join(out)


def _fmt_tool_umbrella_task(op, inp, tur, raw_content, is_error):
    """Background-task family umbrella (Task{Create,Update,Get,List,Output,Stop}).

    The fixed op name is bound by ``_task_um`` so each tool keeps the
    standard registered-formatter contract. The header is a compact,
    per-op summary of the request; the result reuses
    ``_fmt_tool_use_result`` (it already routes the task shapes to
    ``_fmt_tur_task_update`` / ``_fmt_tur_task_dump``), with a JSON-looking
    output colored. Declines (returns ``None``) only when neither a header
    nor a body can be produced.

    Field names span two conventions: the in-tree ``Task*`` CLI uses
    ``subject`` / ``taskId``; an SDK-style spawn uses
    ``description`` / ``subagent_type`` / ``task_id`` — both are accepted.
    """
    head = f'{YELLOW}{BOLD}🔧 {op}{RESET}'
    extras = []
    if op == 'TaskCreate':
        subagent = inp.get('subagent_type')
        subject = inp.get('subject') or inp.get('description') or ''
        if subagent:
            extras.append(f'{MAGENTA}{subagent}{RESET}')
        if subject:
            extras.append(f'{WHITE}{subject}{RESET}')
    elif op == 'TaskUpdate':
        tid = inp.get('task_id') or inp.get('taskId')
        if tid is not None:
            extras.append(f'{WHITE}#{tid}{RESET}')
        status = inp.get('status')
        if status:
            extras.append(f'{MUTE}status={RESET}{status}')
    else:
        # Get / List / Output / Stop — surface whatever key args are set.
        tid = inp.get('task_id') or inp.get('taskId')
        if tid is not None:
            extras.append(f'{WHITE}#{tid}{RESET}')
        for k in ('status', 'filter', 'limit'):
            if inp.get(k) is not None:
                extras.append(f'{MUTE}{k}={RESET}{inp[k]}')
    if extras:
        head += '  ' + f'  {MUTE}·{RESET}  '.join(extras)
    # TaskCreate's first prompt line, when present, hints at the work.
    prompt = inp.get('prompt') or ''
    out = [head]
    if op == 'TaskCreate' and prompt:
        first = prompt.splitlines()[0].strip()
        if first:
            out.append(f'{MUTE}{first[:200]}{RESET}')
    body = _fmt_tool_use_result(tur, raw_content) if (
        tur is not None or raw_content) else ''
    if body:
        # A str tur arrives JSON-colored from _fmt_tool_use_result.
        out.append('')
        out.append(body)
    return '\n'.join(out)


def _task_um(op):
    """Bind ``op`` so each Task* tool keeps the standard formatter contract."""
    def _f(inp, tur, raw_content, is_error):
        return _fmt_tool_umbrella_task(op, inp, tur, raw_content, is_error)
    return _f


for _op in ('TaskCreate', 'TaskUpdate', 'TaskGet', 'TaskList',
            'TaskOutput', 'TaskStop'):
    _TOOL_UMBRELLA_FORMATTERS[_op] = _task_um(_op)
del _op


def _fmt_tool_use_grep(inp):
    pattern = inp.get('pattern', '')
    path = inp.get('path') or '.'
    out = [f'{WHITE}/{pattern}/{RESET}  {MUTE}in{RESET} {CYAN}{path}{RESET}']
    extras = []
    for k in ('glob', 'type', 'output_mode', '-i', '-n', 'multiline', 'head_limit'):
        if k in inp:
            extras.append(f'{k}={inp[k]}')
    if extras:
        out.append(f'{MUTE}{"  ".join(extras)}{RESET}')
    return '\n'.join(out)


def _fmt_tool_use_glob(inp):
    pattern = inp.get('pattern', '')
    path = inp.get('path') or '.'
    return f'{WHITE}{pattern}{RESET}  {MUTE}in{RESET} {CYAN}{path}{RESET}'


def _um_head_body(head, tur, raw_content):
    """Join an umbrella ``head`` with its result body, or ``None``.

    The shared request+result shape for tools whose result side already has
    a formatter via ``_fmt_tool_use_result``: render the request ``head``
    (a ``🔧 …`` line) and, when a result is present, append its body below.
    Returns ``None`` only when there's neither a head nor a body (so an
    empty request degrades to the cascade); a head with no result yet shows
    the request alone.
    """
    body = _fmt_tool_use_result(tur, raw_content) if (
        tur is not None or raw_content) else ''
    if not head and not body:
        return None
    return head + (('\n\n' + body) if body else '')


def _fmt_tool_umbrella_grep(inp, tur, raw_content, is_error):
    """Grep umbrella — query header + matches (count / files / content)."""
    if not inp:
        return None
    head = f'{YELLOW}{BOLD}🔧 Grep{RESET}  ' + _fmt_tool_use_grep(inp)
    return _um_head_body(head, tur, raw_content)


_TOOL_UMBRELLA_FORMATTERS['Grep'] = _fmt_tool_umbrella_grep


def _fmt_tool_umbrella_glob(inp, tur, raw_content, is_error):
    """Glob umbrella — pattern header + matched filenames."""
    if not inp:
        return None
    head = f'{YELLOW}{BOLD}🔧 Glob{RESET}  ' + _fmt_tool_use_glob(inp)
    return _um_head_body(head, tur, raw_content)


_TOOL_UMBRELLA_FORMATTERS['Glob'] = _fmt_tool_umbrella_glob


def _fmt_tool_umbrella_webfetch(inp, tur, raw_content, is_error):
    """WebFetch umbrella — ``🔧 WebFetch  <url>`` (+ prompt) then the body.

    There is no structured ``toolUseResult`` for WebFetch; the fetched text
    arrives as the raw ``tool_result`` content (a model-written markdown
    summary). The header carries the URL and the question; the body renders
    through ``_md_voice`` when it looks like markdown, else plain. Declines
    (returns ``None``) when the request has no ``url``.
    """
    url = inp.get('url')
    if not url:
        return None
    prompt = inp.get('prompt', '')
    head = f'{YELLOW}{BOLD}🔧 WebFetch{RESET}  {CYAN}{url}{RESET}'
    out = [head]
    if prompt:
        out.append(f'{MUTE}{prompt}{RESET}')
    text = tur if isinstance(tur, str) else _fmt_tur_raw_content(raw_content)
    if text:
        out.append('')
        out.append(_md_voice(text) if _looks_markdown(text) else text)
    return '\n'.join(out)


_TOOL_UMBRELLA_FORMATTERS['WebFetch'] = _fmt_tool_umbrella_webfetch


def _fmt_tool_umbrella_websearch(inp, tur, raw_content, is_error):
    """WebSearch umbrella — ``🔧 WebSearch  <query>`` then the results.

    Like WebFetch the result is unstructured raw content (a text block that
    leads with ``Web search results for query: …`` and embeds a ``Links:``
    list). The header shows the query; the body is that results text. Any
    ``allowed_domains`` restriction is surfaced as a muted chip. Declines
    (returns ``None``) when the request has no ``query``.
    """
    query = inp.get('query')
    if not query:
        return None
    head = f'{YELLOW}{BOLD}🔧 WebSearch{RESET}  {WHITE}{query}{RESET}'
    out = [head]
    domains = inp.get('allowed_domains') or inp.get('blocked_domains')
    if domains:
        label = 'only' if inp.get('allowed_domains') else 'blocked'
        out.append(f'{MUTE}{label}: {", ".join(str(d) for d in domains)}{RESET}')
    text = tur if isinstance(tur, str) else _fmt_tur_raw_content(raw_content)
    if text:
        out.append('')
        out.append(text)
    return '\n'.join(out)


_TOOL_UMBRELLA_FORMATTERS['WebSearch'] = _fmt_tool_umbrella_websearch


def _fmt_tool_umbrella_toolsearch(inp, tur, raw_content, is_error):
    """ToolSearch umbrella — ``🔧 ToolSearch  <query>`` then the matches."""
    query = inp.get('query')
    if not query:
        return None
    head = f'{YELLOW}{BOLD}🔧 ToolSearch{RESET}  {WHITE}{query}{RESET}'
    return _um_head_body(head, tur, raw_content)


_TOOL_UMBRELLA_FORMATTERS['ToolSearch'] = _fmt_tool_umbrella_toolsearch


def _fmt_tool_umbrella_skill(inp, tur, raw_content, is_error):
    """Skill umbrella — skill name (+ args) header then the ok/fail ack."""
    if not inp:
        return None
    head = f'{YELLOW}{BOLD}🔧 Skill{RESET}  ' + _fmt_tool_use_skill(inp)
    return _um_head_body(head, tur, raw_content)


_TOOL_UMBRELLA_FORMATTERS['Skill'] = _fmt_tool_umbrella_skill


def _fmt_tool_umbrella_send_message(inp, tur, raw_content, is_error):
    """SendMessage umbrella — request header + delivery status, message ONCE.

    The recipient/summary/message come from the REQUEST so they render even
    when the result is missing or in-flight. The result side only contributes
    its UNIQUE bits — the delivery status (``✓ delivered`` / ``✗ <error>``)
    and, when present, ``(from <sender>)`` — never the echoed content, which
    ``routing`` duplicates; the message is rendered exactly once from the
    request. Declines (returns ``None``) on an empty request.
    """
    if not inp:
        return None
    recipient, summary, message = _send_message_fields(inp)
    head = f'{YELLOW}{BOLD}🔧 SendMessage{RESET}  {BOLD}→ {recipient}{RESET}'
    if summary:
        head += f'  {MUTE}·{RESET} {summary}'
    out = [head]
    if isinstance(tur, dict):
        out.append(_send_message_status_line(tur))
        routing = tur.get('routing')
        sender = routing.get('sender') if isinstance(routing, dict) else None
        if sender:
            out[-1] += f'  {MUTE}(from {sender}){RESET}'
    if message:
        out.append('')
        out.append(_render_message_body(message))
    return '\n'.join(out)


_TOOL_UMBRELLA_FORMATTERS['SendMessage'] = _fmt_tool_umbrella_send_message


# Per-status glyph + color for a TodoWrite checklist line. ``in_progress``
# is the active item (YELLOW); ``completed`` is done (GREEN); ``pending``
# stays an unfilled box.
_TODO_GLYPHS = {
    'pending':     ('☐', ''),
    'in_progress': ('▸', YELLOW),
    'completed':   ('✓', GREEN),
}


def _fmt_tool_umbrella_todowrite(inp, tur, raw_content, is_error):
    """TodoWrite umbrella — a glyph-prefixed checklist of the todos.

    Input ``todos`` is a list of ``{content, status, activeForm}``; each
    renders as ``<glyph> <content>`` with the glyph/color keyed on
    ``status`` (``☐`` pending, ``▸`` in_progress YELLOW, ``✓`` completed
    GREEN). The result side is only an ack, so the header + checklist is the
    whole block. Declines (returns ``None``) when there are no usable todos.

    No TodoWrite transcript was available, so this renders from the
    documented input schema; malformed entries are skipped defensively.
    """
    todos = inp.get('todos')
    if not isinstance(todos, list):
        return None
    lines = []
    for t in todos:
        if not isinstance(t, dict):
            continue
        content = t.get('content') or t.get('activeForm') or ''
        if not content:
            continue
        status = t.get('status', 'pending')
        glyph, color = _TODO_GLYPHS.get(status, ('☐', ''))
        if color:
            lines.append(f'{color}{glyph} {content}{RESET}')
        else:
            lines.append(f'{glyph} {content}')
    if not lines:
        return None
    head = f'{YELLOW}{BOLD}🔧 TodoWrite{RESET}'
    return head + '\n' + '\n'.join(lines)


_TOOL_UMBRELLA_FORMATTERS['TodoWrite'] = _fmt_tool_umbrella_todowrite


def _fmt_tool_umbrella_exit_plan_mode(inp, tur, raw_content, is_error):
    """ExitPlanMode umbrella — the proposed plan + an approval status line.

    Input ``plan`` is markdown; it renders through ``_md_voice``. When the
    result text reads as an approval / rejection, a short colored status
    line is appended below the header. Declines (returns ``None``) when no
    ``plan`` was proposed.

    No ExitPlanMode transcript was available to confirm the result shape, so
    the status line is sniffed from the result TEXT (``approv`` → approved,
    ``reject`` / ``declin`` → rejected); anything else simply omits it.
    """
    plan = inp.get('plan')
    if not plan:
        return None
    out = [f'{YELLOW}{BOLD}🔧 ExitPlanMode{RESET}']
    text = tur if isinstance(tur, str) else _fmt_tur_raw_content(raw_content)
    low = text.lower() if isinstance(text, str) else ''
    if 'approv' in low:
        out.append(f'{GREEN}✓ plan approved{RESET}')
    elif 'reject' in low or 'declin' in low:
        out.append(f'{RED}✗ plan rejected{RESET}')
    out.append('')
    out.append(_md_voice(plan))
    return '\n'.join(out)


_TOOL_UMBRELLA_FORMATTERS['ExitPlanMode'] = _fmt_tool_umbrella_exit_plan_mode


def _fmt_tool_use_agent(inp):
    subtype = inp.get('subagent_type', '?')
    desc = inp.get('description', '')
    prompt = inp.get('prompt', '')
    head = (prompt.splitlines()[0] if prompt else '').strip()
    out = [f'{MAGENTA}{subtype}{RESET}  {MUTE}—{RESET} {desc}']
    if head:
        out.append('')
        out.append(head[:400])
    return '\n'.join(out)


def _fmt_tool_use_skill(inp):
    skill = inp.get('skill', '?')
    args = inp.get('args', '')
    return f'{MAGENTA}{skill}{RESET}' + (f'  {MUTE}args:{RESET} {args}' if args else '')


def _fmt_ask_user_question(questions, answers=None, annotations=None):
    """Render an AskUserQuestion payload as markdown.

    Shared by both directions: the tool_use side passes ``answers=None``;
    the tool_result side passes ``answers``/``annotations`` from
    ``toolUseResult``. Output is plain markdown — the caller pipes it
    through ``_md_voice`` so md2ansi (when available) colors it like
    other voice content.
    """
    if not isinstance(questions, list) or not questions:
        return ''
    answers = answers if isinstance(answers, dict) else {}
    annotations = annotations if isinstance(annotations, dict) else {}
    blocks = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        qtext = q.get('question', '')
        blocks.append(_fmt_aq_one(q, answers.get(qtext), annotations.get(qtext)))
    return '\n\n'.join(b for b in blocks if b)


def _fmt_aq_one(q, answer, annotation):  # noqa: ARG001 — annotation reserved
    """Render one question + its options as a markdown block.

    ``answer`` is the user's pick (string or list for multiSelect, or
    None on the tool_use side). Options whose label matches the answer
    flip to a filled glyph. Any answer that doesn't match a known label
    is surfaced separately as ``**Other:** "..."``.
    """
    text = q.get('question', '?')
    header = q.get('header', '')
    multi = bool(q.get('multiSelect'))
    options = q.get('options') or []

    if answer is None:
        selected = set()
    elif isinstance(answer, list):
        selected = {a for a in answer if isinstance(a, str)}
    elif isinstance(answer, str):
        selected = {answer}
    else:
        selected = set()
    option_labels = {o.get('label') for o in options if isinstance(o, dict)}
    other_answers = [a for a in selected if a not in option_labels]

    glyph_off, glyph_on = ('☐', '☑') if multi else ('◯', '●')
    head = f'**{header}:** {text}' if header else f'**{text}**'
    lines = [head, '']
    for i, opt in enumerate(options, 1):
        if not isinstance(opt, dict):
            continue
        label = opt.get('label', '?')
        desc = opt.get('description', '') or ''
        glyph = glyph_on if label in selected else glyph_off
        lines.append(f'{i}. {glyph} **{label}**')
        if desc:
            for ln in desc.splitlines():
                lines.append(f'   {ln}')
        preview = opt.get('preview', '') or ''
        if preview:
            lines.append('')
            lines.append('   ```')
            for ln in preview.splitlines():
                lines.append(f'   {ln}')
            lines.append('   ```')
    for other in other_answers:
        lines.append('')
        lines.append(f'**Other:** "{other}"')
    return '\n'.join(lines)


def _fmt_tool_use_ask_user_question(inp):
    return _md_voice(_fmt_ask_user_question(inp.get('questions')))


def _send_message_fields(inp):
    """``(recipient, summary, message)`` from a ``SendMessage`` input.

    The tool duplicates its fields: prefer ``recipient``/``message``,
    fall back to ``to``/``content``. ``summary`` is a short label.
    """
    inp = inp if isinstance(inp, dict) else {}
    recipient = inp.get('recipient') or inp.get('to') or '?'
    summary = inp.get('summary', '')
    message = inp.get('message')
    if not message:
        message = inp.get('content', '')
    return recipient, summary, message


def _is_send_message(rec):
    """True iff ``rec`` is an assistant record whose salient content is a
    ``SendMessage`` tool_use (mirrors the AskUserQuestion voice branch)."""
    if not isinstance(rec, dict):
        return False
    msg = rec.get('message') if isinstance(rec.get('message'), dict) else {}
    if (rec.get('type') or msg.get('role')) != 'assistant':
        return False
    content = msg.get('content')
    if not isinstance(content, list):
        return False
    for p in content:
        if (isinstance(p, dict) and p.get('type') == 'tool_use'
                and p.get('name') == 'SendMessage'):
            return True
    return False


_TASK_NOTIFY_PREFIX = '<task-notification>'

# Map the wrapper's hyphenated tags onto the dict keys we surface.
_TASK_NOTIFY_TAGS = {
    'task-id': 'task_id',
    'tool-use-id': 'tool_use_id',
    'output-file': 'output_file',
    'status': 'status',
    'summary': 'summary',
    'result': 'result',
}


def _parse_task_notification(text):
    """Peel a ``<task-notification>`` wrapper into its tagged fields.

    Returns ``{task_id, tool_use_id, output_file, status, summary,
    result}`` — every key present, missing tags resolving to ``''``.
    ``result`` is kept as raw markdown (rendered later via ``_md_voice``).
    A single non-greedy regex per tag, not a full XML parse: the payload
    is machine-emitted with a fixed shape and ``result`` is free-form
    markdown that would choke a strict parser.
    """
    text = text or ''
    out = {v: '' for v in _TASK_NOTIFY_TAGS.values()}
    for tag, key in _TASK_NOTIFY_TAGS.items():
        m = re.search(rf'<{tag}>(.*?)</{tag}>', text, re.DOTALL)
        if m:
            out[key] = m.group(1).strip()
    return out


def _inbound_voice_text(rec):
    """Text payload used for inbound-agent-voice detection, or None.

    Two record shapes can carry an inbound agent message: a ``user``
    record (the delivered turn — text in ``message.content``) and a
    ``queue-operation`` record (the same payload queued while the
    session was busy — raw text in ``content``; queue records never
    carry an ``origin`` field, so detection there is text-shape only).
    """
    if not isinstance(rec, dict):
        return None
    if rec.get('type') == 'queue-operation':
        return rec.get('content') or ''
    msg = rec.get('message') if isinstance(rec.get('message'), dict) else {}
    if (rec.get('type') or msg.get('role')) != 'user':
        return None
    return _extract_text(msg.get('content', ''))


def _is_task_notification(rec):
    """True iff ``rec`` carries a ``<task-notification>`` wrapper (the
    worker→leader inbound reply) — as a delivered ``user`` record or a
    queued ``queue-operation`` echo.

    Mirrors ``_is_send_message`` for the outbound half: detection is by
    the wrapper prefix on the record's text content, tolerant of the
    bare-string and single-text-part content shapes.
    """
    text = _inbound_voice_text(rec)
    return text is not None and text.lstrip().startswith(_TASK_NOTIFY_PREFIX)


# Injected preamble + wrapper of an inbound peer message (another
# session's SendMessage delivered into this one as a ``user`` record).
# The wrapper tag has two known spellings: ``<agent-message from="…">``
# and the older ``<teammate-message teammate_id="…">``.
_AGENT_MSG_PREAMBLE = 'Another Claude session sent a message:'
_AGENT_MSG_RE = re.compile(
    r'<(agent-message|teammate-message)\b([^>]*)>\n?(.*?)\n?</\1>',
    re.DOTALL)
_AGENT_MSG_FROM_RE = re.compile(r'\b(?:from|teammate_id)="([^"]*)"')


def _is_agent_message(rec):
    """True iff ``rec`` carries an inbound peer message (another
    session's SendMessage → this one) — as a delivered ``user`` record
    or a queued ``queue-operation`` echo.

    Primary signal for delivered records: the structured ``origin``
    field — ``{"kind": "peer", "from": <sender>, ...}``. The text-shape
    check (fixed preamble / ``<agent-message>`` / ``<teammate-message>``
    wrapper) is the only signal for queue records (they never carry
    ``origin``) and the LEGACY fallback for ``user`` records written
    before ``origin`` existed — narrow it back to queue records once
    pre-``origin`` transcripts stop mattering.
    """
    if isinstance(rec, dict):
        origin = rec.get('origin')
        if isinstance(origin, dict) and origin.get('kind') == 'peer':
            return True
    text = _inbound_voice_text(rec)
    if text is None:
        return False
    text = text.lstrip()
    return (text.startswith(_AGENT_MSG_PREAMBLE)
            or text.startswith('<agent-message')
            or text.startswith('<teammate-message'))


def _parse_agent_message(rec):
    """Peel an inbound peer message into ``(sender, body)``.

    Sender comes from ``origin.from`` when present, else from the
    wrapper's ``from="…"`` / ``teammate_id="…"`` attribute. Body is the
    markdown inside the wrapper; a record with neither wrapper nor
    preamble falls back to its whole text.
    """
    text = _inbound_voice_text(rec) or ''
    origin = rec.get('origin')
    sender = origin.get('from', '') if isinstance(origin, dict) else ''
    m = _AGENT_MSG_RE.search(text)
    if m:
        if not sender:
            fm = _AGENT_MSG_FROM_RE.search(m.group(2))
            sender = fm.group(1) if fm else ''
        body = m.group(3).strip()
    else:
        body = text.strip()
        if body.startswith(_AGENT_MSG_PREAMBLE):
            body = body[len(_AGENT_MSG_PREAMBLE):].strip()
    return sender, body


def _summarise_json_body(body):
    """Compact row one-liner for a JSON-object peer-message body, or
    ``None`` (non-JSON, malformed almost-JSON and non-object JSON keep
    the raw text — the preview renders objects field-wise and still
    pretty-prints other JSON).

    Known shapes summarise off their salient fields (idle_notification
    → reason + summary); any other object gets a short type/first-key
    marker instead of a full raw-JSON line.
    """
    if not body.lstrip().startswith('{'):
        return None
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    if parsed.get('type') == 'idle_notification':
        bits = [b for b in ('idle', parsed.get('idleReason'),
                            parsed.get('summary')) if b]
        return _trim(' · '.join(str(b) for b in bits), 5000)
    t = parsed.get('type')
    if isinstance(t, str) and t:
        return f'{{{t}}}'
    return f'{{{next(iter(parsed))}: …}}'


def _fmt_tool_use_send_message(inp):
    """Outbound leader→worker message: header line + JSON/markdown body."""
    recipient, summary, message = _send_message_fields(inp)
    head = f'{BOLD}→ {recipient}{RESET}'
    if summary:
        head += f'  {MUTE}·{RESET} {summary}'
    out = [head]
    if message:
        out.append('')
        out.append(_render_message_body(message))
    return '\n'.join(out)


_TOOL_USE_FORMATTERS = {
    'Bash':             _fmt_tool_use_bash,
    'Read':             _fmt_tool_use_read,
    'Edit':             _fmt_tool_use_edit,
    'Write':            _fmt_tool_use_write,
    'Grep':             _fmt_tool_use_grep,
    'Glob':             _fmt_tool_use_glob,
    'Agent':            _fmt_tool_use_agent,
    'Task':             _fmt_tool_use_agent,
    'Skill':            _fmt_tool_use_skill,
    'AskUserQuestion':  _fmt_tool_use_ask_user_question,
    'SendMessage':      _fmt_tool_use_send_message,
}


# ---- tool_result rendering -----------------------------------------------


def _render_tool_result(obj, part):
    """User record carrying a tool_result content part.

    Header derived from the part; body dispatched on the toolUseResult
    shape (the structured record Claude Code stashes alongside the raw
    content for the renderer).
    """
    is_error = bool(part.get('is_error'))
    tu_id = part.get('tool_use_id', '')
    tur = obj.get('toolUseResult')

    head = f'{WHITE}{BOLD}↳ tool_result{RESET}'
    if is_error:
        head += f'  {RED}[error]{RESET}'
    if tu_id:
        head += f'  {MUTE}id={tu_id[:8]}{RESET}'
    out = [head, '']

    body = _fmt_tool_use_result(tur, part.get('content'))
    if body:
        out.append(body)
    return '\n'.join(out).rstrip() + '\n'


def _fmt_tool_use_result(tur, raw_content):
    """Pick a shape-specific formatter for ``toolUseResult``.

    The raw-text shapes — a bare string ``tur``, a LIST of text parts
    (MCP tools mirror the content parts there), or the fallback to the
    part's content — frequently carry JSON (MCP tools like Jira), so
    they run through ``_maybe_json_color`` here / in
    ``_fmt_tur_raw_content``; every consumer (the leaf tool_result
    renderer, the generic and Task umbrellas) gets the colored +
    nested-decoded form at once.
    """
    if tur is None:
        return _fmt_tur_raw_content(raw_content)
    if isinstance(tur, str):
        colored = _maybe_json_color(tur)
        return colored if colored is not None else tur
    if isinstance(tur, list):
        return _fmt_tur_raw_content(tur)
    if not isinstance(tur, dict):
        return str(tur)
    keys = set(tur.keys())
    if {'stdout', 'stderr', 'interrupted'}.issubset(keys):
        return _fmt_tur_bash(tur)
    if 'structuredPatch' in keys:
        return _fmt_tur_edit(tur)
    if tur.get('type') == 'text' and 'file' in keys:
        return _fmt_tur_read(tur)
    if 'numMatches' in keys or ('matches' in keys and 'query' in keys):
        return _fmt_tur_grep_or_search(tur)
    if {'filenames', 'numFiles'}.issubset(keys) and 'numLines' not in keys:
        return _fmt_tur_glob(tur)
    if {'filenames', 'numFiles', 'numLines'}.issubset(keys):
        return _fmt_tur_grep_content(tur)
    if 'statusChange' in keys and 'taskId' in keys:
        return _fmt_tur_task_update(tur)
    if 'task' in keys and len(keys) == 1:
        return _fmt_tur_task_dump(tur)
    if 'agentId' in keys and 'agentType' in keys:
        return _fmt_tur_subagent(tur)
    if 'commandName' in keys and 'success' in keys:
        return _fmt_tur_skill(tur)
    # SendMessage delivery ack — ``{success, message}`` for the error/ack
    # shape, plus an optional ``routing`` block on the success shape. Checked
    # after skill (whose ack adds ``commandName``) so neither match shadows
    # the other.
    if {'success', 'message'}.issubset(keys) and \
            keys <= {'success', 'message', 'routing'}:
        return _fmt_tur_send_message(tur)
    if 'questions' in keys and 'answers' in keys:
        return _fmt_tur_ask_user_question(tur)
    return _fmt_tur_default(tur)


def _fmt_tur_raw_content(content):
    """Raw tool_result content → display text.

    Each text part colors independently when it parses as JSON — MCP
    results often lead with a JSON payload part followed by prose parts
    (resource notes), so a check on the JOINED text would fail the
    parse and leave the JSON part plain.
    """
    if isinstance(content, str):
        colored = _maybe_json_color(content)
        return colored if colored is not None else content
    if isinstance(content, list):
        chunks = []
        for p in content:
            if isinstance(p, dict) and p.get('type') == 'text':
                text = p.get('text', '') or ''
                colored = _maybe_json_color(text)
                chunks.append(colored if colored is not None else text)
        return '\n'.join(chunks)
    return ''


def _fmt_tur_bash(tur):
    out = []
    if tur.get('interrupted'):
        out.append(f'{RED}{BOLD}[interrupted]{RESET}')
    bg = tur.get('backgroundTaskId')
    if bg:
        out.append(f'{MUTE}background task: {bg}{RESET}')
    persisted = tur.get('persistedOutputPath')
    if persisted:
        size = tur.get('persistedOutputSize', '?')
        out.append(f'{MUTE}persisted: {size}B → {persisted}{RESET}')
    stdout = tur.get('stdout', '')
    stderr = tur.get('stderr', '')
    if stdout:
        out.append(stdout.rstrip())
    if stderr:
        if stdout:
            out.append('')
        out.append(_rule('stderr'))
        body = stderr.rstrip()
        # Red-tint only plain stderr. If the command emitted its own ANSI
        # (e.g. a colorized compiler / linter), pass it through raw so those
        # colors survive instead of being clobbered by a blanket RED wrap.
        if '\x1b' not in body:
            body = f'{RED}{body}{RESET}'
        out.append(body)
    return '\n'.join(out)


def _fmt_tur_edit(tur):
    """Edit/Write result — show the structured patch as a unified diff."""
    patch = tur.get('structuredPatch')
    out = []
    if tur.get('userModified'):
        out.append(f'{YELLOW}[user-modified]{RESET}')
    if isinstance(patch, list):
        for hunk in patch:
            if not isinstance(hunk, dict):
                continue
            old_start = hunk.get('oldStart', '?')
            new_start = hunk.get('newStart', '?')
            out.append(f'{MUTE}@@ -{old_start} +{new_start} @@{RESET}')
            for line in hunk.get('lines') or []:
                if line.startswith('+'):   out.append(f'{GREEN}{line}{RESET}')
                elif line.startswith('-'): out.append(f'{RED}{line}{RESET}')
                else:                      out.append(f'{MUTE}{line}{RESET}')
    elif tur.get('content'):
        # Write tool — no patch, just the new content.
        out.append(f'{CYAN}{tur.get("filePath", "")}{RESET}')
        out.append(f'{GRAY}{tur["content"]}{RESET}')
    return '\n'.join(out)


def _fmt_tur_read(tur):
    f = tur.get('file') or {}
    out = [f'{CYAN}{f.get("filePath", "")}{RESET}  '
           f'{MUTE}{f.get("numLines", "?")} lines{RESET}']
    content = f.get('content', '')
    if content:
        out.append('')
        out.append(content.rstrip())
    return '\n'.join(out)


def _fmt_tur_glob(tur):
    out = [f'{MUTE}{tur.get("numFiles", 0)} files'
           f'{(" — truncated" if tur.get("truncated") else "")}{RESET}']
    for n in tur.get('filenames') or []:
        out.append(f'{CYAN}{n}{RESET}')
    return '\n'.join(out)


def _fmt_tur_grep_content(tur):
    """Grep with content output mode."""
    head = f'{MUTE}{tur.get("numFiles", 0)} files · {tur.get("numLines", 0)} lines{RESET}'
    if tur.get('appliedLimit'):
        head += f'  {YELLOW}[limit applied]{RESET}'
    out = [head, '']
    if tur.get('content'):
        out.append(tur['content'].rstrip())
    return '\n'.join(out)


def _fmt_tur_grep_or_search(tur):
    """Grep count mode or ToolSearch result."""
    if 'query' in tur and 'matches' in tur:
        # ToolSearch
        out = [f'{MUTE}query:{RESET} {WHITE}{tur["query"]}{RESET}',
               f'{MUTE}deferred tools:{RESET} {tur.get("total_deferred_tools", "?")}',
               '']
        for m in tur.get('matches') or []:
            if isinstance(m, dict):
                out.append(f'{YELLOW}{m.get("name", "?")}{RESET}  '
                           f'{MUTE}{m.get("description", "")}{RESET}')
        return '\n'.join(out)
    return f'{MUTE}{tur.get("numFiles", 0)} files · ' \
           f'{tur.get("numMatches", 0)} matches{RESET}'


def _fmt_tur_task_update(tur):
    sc = tur.get('statusChange') or {}
    fields = tur.get('updatedFields')
    head = f'{MUTE}task {RESET}{WHITE}#{tur.get("taskId", "?")}{RESET}'
    if sc:
        head += f'  {sc.get("from", "?")} → {GREEN if tur.get("success") else RED}{sc.get("to", "?")}{RESET}'
    out = [head]
    # ``updatedFields`` shape varies across CC versions: a dict
    # ``{field: new_value}`` on older builds, a list ``[field_name, ...]``
    # on newer ones. Render either without assuming one shape.
    if isinstance(fields, dict):
        for k, v in fields.items():
            out.append(f'{MUTE}{k}:{RESET} {v}')
    elif isinstance(fields, list) and fields:
        out.append(f'{MUTE}updated:{RESET} {", ".join(str(f) for f in fields)}')
    if tur.get('verificationNudgeNeeded'):
        out.append(f'{YELLOW}verification needed{RESET}')
    return '\n'.join(out)


def _fmt_tur_task_dump(tur):
    """Plan/TaskList result — just dump the task body."""
    task = tur.get('task')
    try:
        return f'{GRAY}{json.dumps(task, indent=2, ensure_ascii=False)}{RESET}'
    except (TypeError, ValueError):
        return f'{GRAY}{task}{RESET}'


def _fmt_tur_subagent(tur):
    status = tur.get('status', '?')
    color = {'completed': GREEN, 'success': GREEN,
             'failed': RED, 'error': RED}.get(status, YELLOW)
    head = (f'{MAGENTA}🤖 {tur.get("agentType", "?")}{RESET}  '
            f'{color}[{status}]{RESET}  '
            f'{MUTE}id={tur.get("agentId", "")[:8]}{RESET}')
    dur_ms = tur.get('totalDurationMs', 0)
    tokens = tur.get('totalTokens', 0)
    n_tools = tur.get('totalToolUseCount', 0)
    stats = (f'{MUTE}{dur_ms/1000:.1f}s · {n_tools} tools · '
             f'{tokens:,} tokens{RESET}')
    out = [head, stats, '']
    # The report BODY is a markdown document (it even gets a synthetic
    # ``markdown`` child) — render it through _md_voice like the voice
    # renderers, NOT raw. The head/stats above stay as-is (not markdown).
    content = tur.get('content')
    chunks = []
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict) and p.get('type') == 'text':
                chunks.append(p.get('text', '') or '')
    elif isinstance(content, str):
        chunks.append(content)
    out.append(_md_voice('\n\n'.join(c for c in chunks if c)))
    return '\n'.join(out).rstrip()


def _fmt_tur_skill(tur):
    name = tur.get('commandName', '?')
    ok = bool(tur.get('success'))
    color = GREEN if ok else RED
    return f'{MAGENTA}{name}{RESET}  {color}[{"ok" if ok else "fail"}]{RESET}'


# Longest string still shown inline in the header block of a field-wise
# JSON body render; longer (or multi-line) strings get a full section.
_JSON_FIELD_SHORT_MAX = 80


def _render_json_fields(obj):
    """Field-wise render of a JSON-object agent-message body.

    Short scalars (strings without newlines up to
    ``_JSON_FIELD_SHORT_MAX``, numbers, bools, null) form an aligned dim
    ``key: value`` header block in object key order; nested containers
    join it as compact one-line dumps. Long / multi-line strings follow,
    each under a ``── <key> ──`` rule, rendered exactly like a plain
    markdown body (``_md_voice``).
    """
    header, long_fields = [], []
    for key, val in obj.items():
        if isinstance(val, str) and ('\n' in val
                                     or len(val) > _JSON_FIELD_SHORT_MAX):
            long_fields.append((key, val))
        elif isinstance(val, str):
            header.append((key, val))
        else:
            header.append((key, json.dumps(val, ensure_ascii=False)))
    out = []
    if header:
        w = max(len(k) for k, _ in header) + 1
        out.extend(f'{MUTE}{(k + ":").ljust(w)}{RESET} {GRAY}{v}{RESET}'
                   for k, v in header)
    for key, val in long_fields:
        if out:
            out.append('')
        out.append(_rule(key))
        out.append(_md_voice(val))
    return '\n'.join(out)


def _render_message_body(message):
    """Render an agent-exchanged body: a JSON OBJECT renders field-wise
    (``_render_json_fields``), other JSON keeps the colored pretty-print,
    else through ``_md_voice`` (the body is markdown otherwise). Shared
    by SendMessage in both directions, task-notification results and
    peer messages."""
    if isinstance(message, str) and re.match(r'^\s*\{', message):
        try:
            parsed = json.loads(message)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and parsed:
            return _render_json_fields(parsed)
    colored = _maybe_json_color(message)
    return colored if colored is not None else _md_voice(message)


def _send_message_status_line(tur):
    """``✓ delivered`` (GREEN) / ``✗ <error>`` (RED) from a delivery ack.

    ``tur`` is the SendMessage ``toolUseResult``. The error/ack shape's
    ``message`` tails with an ``Output: /tmp/…`` path that's just plumbing —
    it's trimmed so the status line stays compact. Shared by the result
    formatter and the umbrella formatter so both surface delivery identically.
    """
    ok = bool(tur.get('success'))
    if ok:
        return f'{GREEN}✓ delivered{RESET}'
    message = tur.get('message', '') or ''
    idx = message.find('Output: ')
    if idx != -1:
        message = message[:idx]
    message = message.strip()
    return f'{RED}✗ {message}{RESET}' if message else f'{RED}✗{RESET}'


def _fmt_tur_send_message(tur):
    """SendMessage delivery ack — both shapes, rendered (never raw JSON).

    Error/ack shape ``{success, message}``: just the status line
    (``✓ delivered`` / ``✗ <error>``). Success shape adds a ``routing`` block
    (``sender``/``target``/``summary``/``content``, all echoing the request);
    when present we surface ``<sender> → <target>``, the summary, and the
    delivered content (``_render_message_body`` — field-wise /
    JSON-colored / markdown).
    """
    status = _send_message_status_line(tur)
    routing = tur.get('routing')
    if not isinstance(routing, dict):
        return status
    out = [status]
    sender = routing.get('sender')
    target = routing.get('target')
    if sender or target:
        out.append(f'{MUTE}{sender or "?"} → {target or "?"}{RESET}')
    summary = routing.get('summary')
    if summary:
        out.append(f'{MUTE}· {summary}{RESET}')
    content = routing.get('content')
    if content:
        out.append('')
        out.append(_render_message_body(content))
    return '\n'.join(out)


def _fmt_tur_ask_user_question(tur):
    return _md_voice(_fmt_ask_user_question(
        tur.get('questions'), tur.get('answers'), tur.get('annotations')))


def _fmt_tur_default(tur):
    """Unknown-shape ``toolUseResult``: syntax-colored JSON dump (with
    nested JSON-strings decoded), not a flat grey blob."""
    try:
        return _json_color_value(tur)
    except (TypeError, ValueError):
        return f'{GRAY}{tur}{RESET}'


# ---- attachment rendering ------------------------------------------------


def _render_attachment(obj):
    a = obj.get('attachment') if isinstance(obj.get('attachment'), dict) else {}
    sub = a.get('type', '?')
    head = f'{BLUE}{BOLD}📎 attachment{RESET}  {MAGENTA}{sub}{RESET}'
    formatter = _ATTACH_FORMATTERS.get(sub, _fmt_attach_default)
    body = formatter(a)
    return head + ('\n\n' + body if body else '')


def _fmt_attach_file(a):
    """Filename only — the body lives on disk and can be large.
    Reachable via $EDITOR, not the preview pane."""
    out = [f'{CYAN}{a.get("displayPath", a.get("filename", ""))}{RESET}']
    if a.get('filename') and a.get('filename') != a.get('displayPath'):
        out.append(f'{MUTE}{a["filename"]}{RESET}')
    return '\n'.join(out)


def _fmt_attach_compact_file_reference(a):
    """Path-only — compaction's file ref points at a file on disk."""
    out = [f'{CYAN}{a.get("displayPath", a.get("filename", ""))}{RESET}']
    if a.get('filename') and a.get('filename') != a.get('displayPath'):
        out.append(f'{MUTE}{a["filename"]}{RESET}')
    return '\n'.join(out)


def _fmt_attach_edited_text_file(a):
    fname = a.get('filename', '?')
    head = f'{CYAN}✎ edited{RESET}  {fname}'
    snip = a.get('snippet')
    if snip:
        return head + '\n\n' + snip.rstrip()
    return head


def _fmt_attach_plan_mode_exit(a):
    exists = a.get('planExists')
    arrow = f'{RED}→{RESET}'
    out = [f'{YELLOW}⇆ mode{RESET}  {arrow} exit plan mode']
    path = a.get('planFilePath')
    if path:
        kept = 'kept' if exists else 'discarded'
        out.append(f'{MUTE}plan {kept}:{RESET} {path}')
    return '\n'.join(out)


def _fmt_attach_diagnostics(a):
    out = []
    if a.get('isNew'):
        out.append(f'{YELLOW}[new]{RESET}')
    for entry in a.get('files') or []:
        if not isinstance(entry, dict):
            continue
        out.append(f'{CYAN}{entry.get("file", "?")}{RESET}')
        for d in entry.get('diagnostics') or []:
            if not isinstance(d, dict):
                continue
            sev = d.get('severity', '')
            color = {'error': RED, 'warning': YELLOW}.get(sev, MUTE)
            line = d.get('range', {}).get('start', {}).get('line', '?')
            out.append(f'  {color}{sev}{RESET} '
                       f'{MUTE}L{line}:{RESET} {d.get("message", "")}')
    return '\n'.join(out)


def _fmt_attach_command_permissions(a):
    tools = a.get('allowedTools') or []
    return f'{MUTE}allowed:{RESET} {", ".join(tools) if tools else "(none)"}'


def _fmt_attach_queued_command(a):
    mode = a.get('commandMode', '')
    head = f'{CYAN}📥 queued{RESET}'
    if mode:
        head += f'  {MUTE}({mode}){RESET}'
    return head + '\n\n' + (a.get('prompt', '') or '')


def _fmt_attach_hook_success(a):
    name = a.get('hookName', '?')
    event = a.get('hookEvent', '?')
    code = a.get('exitCode', 0)
    dur = a.get('durationMs', 0)
    color = GREEN if code == 0 else RED
    out = [f'{color}✓ {name}{RESET}  '
           f'{MUTE}{event} · exit={code} · {dur}ms{RESET}']
    for label, key, col in (('stdout', 'stdout', None),
                            ('stderr', 'stderr', RED)):
        v = a.get(key, '')
        if v:
            out.append('')
            out.append(_rule(label))
            out.append(f'{col}{v.rstrip()}{RESET}' if col else v.rstrip())
    return '\n'.join(out)


def _fmt_attach_hook_context(a):
    return (f'{MUTE}+ context from {RESET}{MAGENTA}{a.get("hookName", "?")}{RESET}'
            f'  {MUTE}({a.get("hookEvent", "?")}){RESET}\n\n'
            f'{a.get("content", "")}')


def _fmt_attach_task_reminder(a):
    return (f'{YELLOW}🔔 reminder{RESET}  '
            f'{MUTE}{a.get("itemCount", 0)} items{RESET}\n\n'
            f'{a.get("content", "")}')


def _fmt_attach_skill_listing(a):
    head = f'{MUTE}📚 {a.get("skillCount", 0)} skills'
    if a.get('isInitial'):
        head += ' (initial)'
    head += f'{RESET}'
    return head + '\n\n' + (a.get('content', '') or '')


def _fmt_attach_delta(a):
    """mcp_instructions_delta / deferred_tools_delta / agent_listing_delta
    — the same ±names shape (agent deltas key on ``*Types``)."""
    out = []
    for n in a.get('addedNames') or a.get('addedTypes') or []:
        out.append(f'{GREEN}+ {n}{RESET}')
    for n in a.get('removedNames') or a.get('removedTypes') or []:
        out.append(f'{RED}- {n}{RESET}')
    return '\n'.join(out)


def _fmt_attach_date_change(a):
    return f'{CYAN}📅 date{RESET} → {WHITE}{a.get("newDate", "?")}{RESET}'


def _fmt_attach_ultrathink(a):
    return f'{MAGENTA}🧠 ultrathink{RESET}'


def _fmt_attach_total_tokens(a):
    n = _total_tokens_left(a)
    left = f'{_human_count(n)} tokens left' if n is not None else 'tokens left'
    return f'{CYAN}⛽ {left}{RESET}'


def _fmt_attach_batching_reminder(a):
    head = f'{YELLOW}🔔 batching reminder{RESET}'
    if a.get('model'):
        head += f'  {MUTE}({a["model"]}){RESET}'
    return head + '\n\n' + (a.get('text', '') or '')


def _fmt_attach_bash_audience_note(a):
    return (f'{MUTE}bash output audience note  '
            f'· tool_use={a.get("toolUseID", "?")}{RESET}')


def _fmt_attach_nested_memory(a):
    """Injected nested CLAUDE.md — file-style header (displayPath first,
    full path on a second line, like ``file``), content inline
    (memories are small, unlike ``file`` attachments)."""
    c = a.get('content') if isinstance(a.get('content'), dict) else {}
    out = [f'{CYAN}📎 memory: {a.get("displayPath", a.get("path", "?"))}{RESET}']
    if a.get('path') and a.get('path') != a.get('displayPath'):
        out.append(f'{MUTE}{a["path"]}{RESET}')
    body = c.get('content', '') or ''
    if body:
        out.append('')
        out.append(body)
    return '\n'.join(out)


def _fmt_attach_read_truncation(a):
    return f'{YELLOW}{a.get("banner", "")}{RESET}'


def _fmt_attach_mode_flag(a):
    """auto_mode / plan_mode reminders — symmetric with plan_mode_exit."""
    out = [f'{YELLOW}⇆ mode{RESET}  '
           f'{(a.get("type") or "").replace("_", " ")}']
    if a.get('planFilePath'):
        out.append(f'{MUTE}plan:{RESET} {a["planFilePath"]}')
    return '\n'.join(out)


def _fmt_attach_silent_turn(a):
    return f'{YELLOW}🔔{RESET} {a.get("text", "")}'


def _fmt_attach_hook_cancelled(a):
    name = a.get('hookName', '?')
    event = a.get('hookEvent', '?')
    dur = a.get('durationMs', 0)
    out = [f'{RED}✗ {name} cancelled{RESET}  '
           f'{MUTE}{event} · {dur}ms'
           f'{" · timed out" if a.get("timedOut") else ""}{RESET}']
    if a.get('command'):
        out.append(f'{MUTE}$ {a["command"]}{RESET}')
    return '\n'.join(out)


def _fmt_attach_invoked_skills(a):
    """Names + paths only — the per-skill content blobs are large."""
    out = []
    for s in a.get('skills') or []:
        if isinstance(s, dict):
            out.append(f'{MAGENTA}{s.get("name", "?")}{RESET}  '
                       f'{MUTE}{s.get("path", "")}{RESET}')
    head = f'{BLUE}📚 invoked skills{RESET}'
    return head + ('\n\n' + '\n'.join(out) if out else '')


def _fmt_attach_default(a):
    try:
        return f'{GRAY}{json.dumps(a, indent=2, ensure_ascii=False)}{RESET}'
    except (TypeError, ValueError):
        return f'{GRAY}{a}{RESET}'


_ATTACH_FORMATTERS = {
    'file':                   _fmt_attach_file,
    'compact_file_reference': _fmt_attach_compact_file_reference,
    'edited_text_file':       _fmt_attach_edited_text_file,
    'plan_mode_exit':         _fmt_attach_plan_mode_exit,
    'diagnostics':            _fmt_attach_diagnostics,
    'command_permissions':    _fmt_attach_command_permissions,
    'queued_command':         _fmt_attach_queued_command,
    'hook_success':           _fmt_attach_hook_success,
    'hook_additional_context': _fmt_attach_hook_context,
    'task_reminder':          _fmt_attach_task_reminder,
    'skill_listing':          _fmt_attach_skill_listing,
    'mcp_instructions_delta': _fmt_attach_delta,
    'deferred_tools_delta':   _fmt_attach_delta,
    'agent_listing_delta':    _fmt_attach_delta,
    'date_change':            _fmt_attach_date_change,
    'ultrathink_effort':      _fmt_attach_ultrathink,
    'total_tokens_reminder':  _fmt_attach_total_tokens,
    'batching_reminder_sent': _fmt_attach_batching_reminder,
    'bash_output_audience_note': _fmt_attach_bash_audience_note,
    'nested_memory':          _fmt_attach_nested_memory,
    'read_truncation_notice': _fmt_attach_read_truncation,
    'auto_mode':              _fmt_attach_mode_flag,
    'plan_mode':              _fmt_attach_mode_flag,
    'silent_turn_reminder':   _fmt_attach_silent_turn,
    'already_read_file':      _fmt_attach_file,
    'hook_cancelled':         _fmt_attach_hook_cancelled,
    'invoked_skills':         _fmt_attach_invoked_skills,
}


# ---- system rendering ----------------------------------------------------


def _render_system(obj):
    sub = obj.get('subtype') or ''
    if sub == 'turn_duration':
        ms = obj.get('durationMs', 0)
        return (f'{MUTE}⏱ turn{RESET}  '
                f'{WHITE}{ms/1000:.2f}s{RESET}  '
                f'{MUTE}· {obj.get("messageCount", 0)} msgs{RESET}')
    if sub == 'stop_hook_summary':
        return f'{GRAY}■ stop_hook_summary{RESET}'
    if sub == 'local_command':
        cmd = obj.get('content') or obj.get('command') or ''
        return f'{GRAY}$ {cmd}{RESET}'
    if sub == 'scheduled_task_fire':
        return f'{CYAN}⏲ scheduled fire{RESET}'
    if sub == 'compact_boundary':
        # Prominent — it's a structural marker (voice tier, level 2).
        head = (f'{YELLOW}{BOLD}♻ '
                f'{obj.get("content") or "Conversation compacted"}{RESET}')
        trig = (obj.get('compactMetadata') or {}).get('trigger', '')
        if trig:
            head += f'  {MUTE}({trig}){RESET}'
        return head
    if sub == 'model_refusal_fallback':
        return (f'{YELLOW}{BOLD}⚠ model refusal fallback{RESET}\n\n'
                f'{YELLOW}{obj.get("content", "")}{RESET}')
    if sub == 'away_summary':
        return f'{CYAN}{BOLD}away summary{RESET}\n\n{obj.get("content", "")}'
    if sub == 'informational':
        return f'{MUTE}ℹ info{RESET}\n\n{obj.get("content", "")}'
    if sub == 'agents_killed':
        return f'{RED}■ agents killed{RESET}'
    if sub == 'api_error':
        try:
            body = json.dumps(obj, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            body = str(obj)
        return f'{RED}{BOLD}‼ api_error{RESET}\n\n{RED}{body}{RESET}'
    return f'{GRAY}system{RESET}  {MUTE}{sub or "(no subtype)"}{RESET}'


# ---- metadata rendering --------------------------------------------------


def _render_metadata(obj):
    t = obj.get('type', '?')
    color = _KIND_HEADER_COLOR.get(t, MUTE)
    head = f'{color}{BOLD}{t}{RESET}'

    # Type-specific salient fields. Everything falls through to a generic
    # key/value dump of the remaining fields so power users still get the
    # full record.
    if t in ('ai-title', 'custom-title'):
        title = obj.get('aiTitle') or obj.get('customTitle') or ''
        return f'{head}\n\n{WHITE}{title}{RESET}'
    if t == 'last-prompt':
        return f'{head}\n\n{obj.get("lastPrompt", "")}'
    if t == 'task-summary':
        return f'{head}\n\n{obj.get("summary", "")}'
    if t == 'tag':
        return f'{head}  {YELLOW}{obj.get("tag", "")}{RESET}'
    if t == 'agent-name':
        return f'{head}  {MAGENTA}{obj.get("agentName", "")}{RESET}'
    if t == 'agent-color':
        return f'{head}  {obj.get("agentColor", "")}'
    if t == 'agent-setting':
        return f'{head}  {obj.get("agentSetting", "")}'
    if t == 'permission-mode':
        pm = obj.get('permissionMode', '')
        col = _PERM_COLOR.get(pm, MUTE)
        return f'{head}  {col}[{pm}]{RESET}'
    if t == 'mode':
        return f'{head}  {RED if obj.get("mode") == "coordinator" else MUTE}' \
               f'[{obj.get("mode", "")}]{RESET}'
    if t == 'pr-link':
        return (f'{head}\n\n'
                f'{WHITE}{obj.get("prRepository", "")}'
                f'#{obj.get("prNumber", "")}{RESET}\n'
                f'{BLUE}{obj.get("prUrl", "")}{RESET}\n'
                f'{MUTE}linked: {_local_ts(obj.get("timestamp", ""))}{RESET}')
    if t == 'worktree-state':
        ws = obj.get('worktreeSession')
        if ws is None:
            return f'{head}  {MUTE}(exited){RESET}'
        return (f'{head}\n\n'
                f'{CYAN}{ws.get("worktreePath", "")}{RESET}  '
                f'{MUTE}@{RESET} {GREEN}{ws.get("worktreeBranch", "")}{RESET}\n'
                f'{MUTE}original:{RESET} {ws.get("originalCwd", "")} '
                f'{MUTE}(branch {ws.get("originalBranch", "")}){RESET}')
    if t == 'file-history-snapshot':
        snap = obj.get('snapshot') or {}
        n = len(snap) if isinstance(snap, dict) else '?'
        return (f'{head}  {MUTE}{n} files'
                f'{(" (update)" if obj.get("isSnapshotUpdate") else "")}{RESET}')
    if t == 'attribution-snapshot':
        states = obj.get('fileStates') or {}
        chars = sum(s.get('claudeContribution', 0)
                    for s in states.values() if isinstance(s, dict))
        return (f'{head}  {MUTE}{len(states)} files · '
                f'{chars:,} chars by Claude{RESET}')
    if t == 'content-replacement':
        rep = obj.get('replacements') or []
        agent = obj.get('agentId', '')
        return (f'{head}  {MUTE}{len(rep)} replacements'
                f'{(f" (agent={agent[:8]})" if agent else "")}{RESET}')
    if t == 'speculation-accept':
        ms = obj.get('timeSavedMs', 0)
        return f'{head}  {GREEN}+{ms/1000:.1f}s saved{RESET}'
    if t == 'queue-operation':
        op = obj.get('operation', '?')
        # A queued inbound agent message renders like the delivered
        # record (sender header + markdown), under the queue header.
        if _is_task_notification(obj):
            body = _render_task_notification(obj.get('content', ''))
        elif _is_agent_message(obj):
            body = _render_agent_message(obj)
        else:
            body = obj.get('content', '')
        return f'{head}  {WHITE}{op}{RESET}\n\n{body}'
    if t == 'marble-origami-commit':
        return (f'{head}  {MUTE}id={obj.get("collapseId", "")}{RESET}\n\n'
                f'{obj.get("summary", "")}')
    if t == 'marble-origami-snapshot':
        staged = obj.get('staged') or []
        return (f'{head}  {MUTE}{len(staged)} staged · '
                f'armed={obj.get("armed")} · '
                f'lastSpawnTokens={obj.get("lastSpawnTokens", 0)}{RESET}')
    if t == 'progress':
        d = obj.get('data') or {}
        dt = d.get('type', '?') if isinstance(d, dict) else '?'
        return f'{head}  {MUTE}{dt}{RESET}'
    if t == 'cost-state':
        cost = obj.get('totalCostUSD') or 0
        dur = (obj.get('totalDuration') or 0) / 1000
        out = [f'{head}  {YELLOW}${cost:.2f}{RESET}  '
               f'{GREEN}+{obj.get("totalLinesAdded", 0)}{RESET}/'
               f'{RED}-{obj.get("totalLinesRemoved", 0)}{RESET} lines  '
               f'{MUTE}· {dur:.0f}s{RESET}']
        usage = obj.get('modelUsage') or {}
        for model, u in usage.items():
            if isinstance(u, dict):
                out.append(f'{MAGENTA}{model}{RESET}  {MUTE}'
                           f'in={_human_count(u.get("inputTokens", 0))} '
                           f'out={_human_count(u.get("outputTokens", 0))}'
                           f'{RESET}')
        if len(out) > 1:
            out.insert(1, '')
        return '\n'.join(out)
    if t == 'atis-latch':
        return f'{GRAY}atis-latch{RESET}'
    if t == 'summary':
        return f'{head}\n\n{obj.get("summary", "")}'
    return f'{head}\n{GRAY}{json.dumps(obj, indent=2, ensure_ascii=False)}{RESET}'


def _render_unknown(obj):
    """Forward-compat fallback — unchanged from the pre-refactor preview."""
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)


# ---- chrome footer -------------------------------------------------------


def _ts_offset(root_ts, ts):
    """``+1m30s``-style elapsed time of ``ts`` since the turn's user request.

    Both are ISO-8601 stamps (``…Z``); on 3.10 ``fromisoformat`` rejects the
    bare ``Z`` so we swap it for ``+00:00``. Returns ``None`` when either is
    missing or unparseable so the chrome simply omits the row.
    """
    if not root_ts or not ts:
        return None
    try:
        a = datetime.datetime.fromisoformat(root_ts.replace('Z', '+00:00'))
        b = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None
    secs = (b - a).total_seconds()
    sign = '+' if secs >= 0 else '-'
    secs = abs(secs)
    if secs < 60:
        return f'{sign}{secs:.1f}s'
    if secs < 3600:
        m, s = divmod(int(secs), 60)
        return f'{sign}{m}m{s:02d}s'
    h, rem = divmod(int(secs), 3600)
    return f'{sign}{h}h{rem // 60:02d}m'


def _turn_root_ts(path, n):
    """ISO timestamp of the user request that opens line ``n``'s turn.

    Resolves the leaf's parent umbrella (``parent_id_of``) to the turn-root
    line and returns that record's ``timestamp``. Returns ``None`` when the
    line *is* the turn root (its own offset is always zero), when it sits in
    an out-of-turn ``<system>`` span (no umbrella request), or when no
    timestamp is on the root. Stays within ``path`` — a subagent transcript
    times against its own dispatch prompt, not the parent session's request.
    """
    td = _scan_tree(path)
    parent_id = td.parent_id_of.get(n)
    if parent_id is None:
        return None
    tag = parent_id[0]
    if tag == 'prompt':
        root_line = parent_id[2]
    elif tag == 'tool':
        root_line = td.tool_to_turn_root_line.get(parent_id[2])
    else:  # 'span' — out-of-turn metadata, no umbrella request
        return None
    if root_line is None or root_line == n:
        return None
    if not (0 <= root_line < len(td.records)):
        return None
    root = td.records[root_line]
    return root.get('timestamp') if isinstance(root, dict) else None


def _fmt_chrome(obj, turn_root_ts=None):
    """Common metadata footer appended to every per-message preview.

    ``turn_root_ts`` is the ISO timestamp of the message's umbrella user
    request (from ``_turn_root_ts``); when set, an ``offset`` row showing the
    elapsed time since that request is emitted right after ``timestamp``.
    """
    if not isinstance(obj, dict):
        return ''
    rows = []
    for key, label in (('uuid', 'uuid'),
                       ('parentUuid', 'parentUuid'),
                       ('sessionId', 'sessionId'),
                       ('promptId', 'promptId'),
                       ('forkedFrom', 'forkedFrom')):
        v = obj.get(key)
        if v:
            rows.append((label, str(v)))
    cwd = obj.get('cwd', '')
    branch = obj.get('gitBranch', '')
    if cwd or branch:
        rows.append(('cwd', f'{cwd}  {MUTE}branch:{RESET} {branch}' if branch else cwd))
    ts = obj.get('timestamp', '')
    ver = obj.get('version', '')
    if ts or ver:
        # _ts_offset parses the raw ISO strings itself — only the shown value converts.
        shown = _local_ts(ts)
        rows.append(('timestamp', f'{shown}  {MUTE}version:{RESET} {ver}' if ver else shown))
        offset = _ts_offset(turn_root_ts, ts)
        if offset:
            rows.append(('offset', offset))
    flags = []
    if obj.get('isSidechain'): flags.append('sidechain')
    if obj.get('isMeta'):      flags.append('meta')
    if flags:
        rows.append(('flags', ', '.join(flags)))

    msg = obj.get('message') if isinstance(obj.get('message'), dict) else {}
    usage = msg.get('usage') or {}

    if not rows and not usage:
        return ''
    keys = [k for k, _ in rows] + (['usage'] if usage else [])
    w = max(len(k) for k in keys)
    parts = [_rule()]
    if rows:
        parts.append('\n'.join(
            f'{MUTE}{k.ljust(w)}:{RESET} {v}' for k, v in rows
        ))
    if usage:
        usage_str = '   '.join([
            f'{MUTE}input:{RESET} {usage.get("input_tokens", 0):,}',
            f'{MUTE}output:{RESET} {usage.get("output_tokens", 0):,}',
            f'{MUTE}cache read:{RESET} {usage.get("cache_read_input_tokens", 0):,}',
            f'{MUTE}cache new:{RESET} {usage.get("cache_creation_input_tokens", 0):,}',
        ])
        parts.append(f'{MUTE}{"usage".ljust(w)}:{RESET} {usage_str}')
    return '\n'.join(parts)


_RENDERERS = {
    'user':       _render_user,
    'assistant':  _render_assistant,
    'attachment': _render_attachment,
    'system':     _render_system,
    'metadata':   _render_metadata,
    'unknown':    _render_unknown,
}
