# ---- raw Claude record → normalized Record -------------------------------
#
# Claude Code .jsonl files mix several record shapes:
#   1. ``type=user`` / ``type=assistant`` — the conversation (string or
#      content-list ``message.content``; tool_use / tool_result parts).
#   2. ``type=system`` / ``type=attachment`` — framing and injected context.
#   3. bookkeeping types (``last-prompt``, ``ai-title``, …) → ``meta``.
#   4. anything else → ``unknown``.
#
# ``_parse_record`` maps one raw object to one ``Record``. Row labels keep
# the raw type names (``tag``); the summary is one line, intentionally
# lossy (the preview shows the full record), optimised for scanning.


_TAG_STYLE_FOR_KIND = {
    # Conversation
    'user':                'cyan',
    'assistant':           'green',
    'attachment':          'blue',
    'system':              'gray',
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


_CANON_BY_TOOL = {
    'Bash': 'bash',
    'Read': 'read',
    'Edit': 'edit', 'MultiEdit': 'edit', 'NotebookEdit': 'edit',
    'Write': 'write',
    'Grep': 'grep',
    'Glob': 'glob',
    'Agent': 'spawn', 'Task': 'spawn',
    'SendMessage': 'message',
    'AskUserQuestion': 'question',
    'WebFetch': 'webfetch',
    'WebSearch': 'websearch',
    'Skill': 'skill',
}


def _tool_canon(name):
    if name in _CANON_BY_TOOL:
        return _CANON_BY_TOOL[name]
    if isinstance(name, str) and name.startswith('mcp__'):
        return 'mcp'
    return 'other'


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
# (unknown) is a maintenance view: ``_min_level_of``'s fallthrough
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
    solely of such parts has nothing to render, so ``_min_level_of``
    sinks it to the ``all`` (6) tier. A thinking part with actual text,
    or any other part (tool_use / tool_result / text) alongside, fails
    the test — readable thinking is voice (level 2), other machinery
    stays at level 4 (3 for an edit tool).
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


def _is_fresh_prompt(rec):
    """A human-typed prompt: user voice that is neither injected context
    nor inbound agent voice."""
    return (isinstance(rec, dict) and rec.get('type') == 'user'
            and not rec.get('isMeta') and _is_voice(rec)
            and not (_is_task_notification(rec) or _is_agent_message(rec)))


def _min_level_of(rec):
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
    if _is_task_notification(rec) or _is_agent_message(rec):
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
        if (_is_fresh_prompt(rec)
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


def _row_label(obj):
    """Row label for one raw record: its ``type`` / message role, refined
    for the inter-agent voice channel.

    An assistant record whose salient content is a ``SendMessage``
    tool_use is the leader voicing a worker (``agent-send``); a
    ``<task-notification>`` is the worker's reply back (``agent-reply``);
    a peer session's message is ``agent-peer``. The inbound two match
    both the delivered ``user`` record and its queued ``queue-operation``
    echo. Each drives its own stripe + tag style — without this, inbound
    agent voice would inherit the human stripe and be mis-attributed.
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


def _iso_epoch(iso):
    """Epoch seconds of an ISO-8601 stamp, or ``None``."""
    if not iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _text_parts(content):
    """Every non-empty ``text`` part (or the bare string), joined."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    return '\n\n'.join(
        p['text'] for p in content
        if isinstance(p, dict) and p.get('type') == 'text' and p.get('text')
    )


def _tool_result_output(content):
    """Text of a tool_result part's ``content`` (string or text sub-parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(
            p.get('text') or '' for p in content
            if isinstance(p, dict) and p.get('type') == 'text'
        )
    return None


def _tool_result_glance(tur):
    """Structured ``toolUseResult`` glance for the row title, or ``None``
    to fall back to the raw output text."""
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
        return None
    if isinstance(tur, str):
        return _trim(tur, 5000)
    return None


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


def _tool_from_part(p):
    """Tool dict for one raw ``tool_use`` content part."""
    name = p.get('name')
    inp = p.get('input') or {}
    tool = new_tool(canon=_tool_canon(name), name=name, call_id=p.get('id'),
                    input=inp)
    if tool['canon'] == 'message':
        recipient, _s, message = _send_message_fields(inp)
        tool['peer'] = recipient
        tool['text'] = message if isinstance(message, str) else ''
    elif tool['canon'] == 'spawn' and isinstance(inp, dict):
        tool['peer'] = inp.get('name') or None
    return tool


def _tur_diff(tur):
    """Unified diff of an edit result's ``structuredPatch``, or ``None``."""
    if not (isinstance(tur, dict) and isinstance(tur.get('structuredPatch'), list)):
        return None
    try:
        return _structured_patch_to_unified(
            tur['structuredPatch'], tur.get('filePath') or '')
    except Exception:
        return None


def _parse_record(raw, key=None):
    """Map one raw Claude record to a ``Record``."""
    label = _row_label(raw)
    rec = Record(
        'unknown', raw, key,
        tag=label,
        tag_style=_TAG_STYLE_FOR_KIND.get(label, ''),
        id=raw.get('uuid') or None,
        ts=_iso_epoch(raw.get('timestamp')),
        is_voice=_is_voice(raw),
        is_meta=bool(raw.get('isMeta')),
        min_level=_min_level_of(raw),
    )
    if rec.is_voice:
        rec.row_bg = _ROW_BG_FOR_KIND.get(label)

    # Inbound agent voice (delivered ``user`` record or queued echo):
    # summary from the parsed wrapper, body as text.
    if _is_task_notification(raw):
        text = _inbound_voice_text(raw) or ''
        d = _parse_task_notification(text)
        rec.agent_voice = 'reply'
        rec.text = d['result'] or text
        bits = [b for b in (d['task_id'], d['status'], d['summary']) if b]
        rec.summary = '← ' + ' · '.join(bits) if bits else '← task-notification'
    elif _is_agent_message(raw):
        sender, body = _parse_agent_message(raw)
        rec.agent_voice = 'idle' if _is_idle_notification(body) else 'peer'
        rec.peer = sender or None
        rec.text = body
        short = _summarise_json_body(body) or _trim(body, 5000)
        rec.summary = f'← {sender}: {short}' if sender else f'← {short}'

    t = raw.get('type')
    msg = raw.get('message') if isinstance(raw.get('message'), dict) else {}
    content = msg.get('content', '')
    parts = [p for p in content if isinstance(p, dict)] \
        if isinstance(content, list) else []

    if t == 'user':
        rec.caller_id = raw.get('sourceToolAssistantUUID') or None
        rec.call_id = raw.get('sourceToolUseID') or None
        results = [p for p in parts if p.get('type') == 'tool_result']
        if results:
            rec.kind = 'tool_result'
            tur = raw.get('toolUseResult')
            diff = _tur_diff(tur)
            for p in results:
                rec.tools.append(new_tool(
                    call_id=p.get('tool_use_id'),
                    output=_tool_result_output(p.get('content')),
                    is_error=bool(p.get('is_error')),
                    diff=diff,
                    tur=tur,                     # Claude's structured result
                    content=p.get('content'),    # the raw part content
                ))
            glance = _tool_result_glance(tur)
            for tool in rec.tools:
                tool['glance'] = glance
            rec.text = _text_parts(content)
        else:
            rec.kind = 'user_prompt'
            if rec.agent_voice is None:
                rec.text = _text_parts(content)
        return rec

    if t == 'assistant':
        rec.end_turn = msg.get('stop_reason') == 'end_turn'
        if raw.get('attributionAgent'):
            rec.meta['agent'] = raw['attributionAgent']
        rec.tools = [_tool_from_part(p) for p in parts
                     if p.get('type') == 'tool_use']
        spoken = _text_parts(content)
        think = _thinking_text(raw)
        if think:
            rec.meta['thinking'] = think
        if spoken.strip():
            rec.kind = 'assistant_text'
            rec.text = spoken
        elif think:
            rec.kind = 'thinking'
            rec.text = think
        elif rec.tools:
            rec.kind = 'tool_call'
        elif _is_empty_thinking_only(raw):
            rec.kind = 'thinking'
            rec.meta['encrypted'] = True
        else:
            rec.kind = 'assistant_text'
        return rec

    if t == 'system':
        sub = raw.get('subtype', '')
        body = raw.get('content') or ''
        if sub == 'compact_boundary':
            rec.kind = 'compaction'
        elif sub == 'turn_duration':
            rec.kind = 'turn_end'
            rec.meta['duration_ms'] = raw.get('durationMs', 0)
        else:
            rec.kind = 'system'
        rec.text = body if isinstance(body, str) else ''
        rec.summary = _system_summary(raw, sub)
        rec.meta['span_label'] = f'sys:{sub}' if sub else 'system'
        return rec

    if t == 'attachment':
        a = raw.get('attachment') if isinstance(raw.get('attachment'), dict) else {}
        rec.kind = 'system'
        rec.call_id = a.get('toolUseID') or None
        rec.text = a.get('displayPath') or a.get('filename') or ''
        rec.summary = _attachment_summary(a)
        rec.meta['span_label'] = f'att:{a.get("type", "")}'
        return rec

    if t == 'queue-operation':
        rec.kind = 'meta'
        rec.text = raw.get('content') or ''
        if rec.summary is None:
            rec.summary = f'queue {raw.get("operation", "?")}: {_trim(rec.text, 5000)}'
        return rec

    if t in _METADATA_TYPES or t in _KNOWN_TYPE_NOISE:
        rec.kind = 'meta'
        rec.summary = _metadata_summary(raw, t)
        if t == 'ai-title':
            rec.meta['title'] = raw.get('aiTitle')
            rec.meta['title_by'] = 'agent'
        elif t == 'custom-title':
            rec.meta['title'] = raw.get('customTitle')
            rec.meta['title_by'] = 'user'
        return rec

    # Unrecognised: keep whatever text it carries.
    text = _extract_text(content) if content else ''
    role = msg.get('role') or raw.get('role') or t or ''
    rec.text = text
    if role and text:
        rec.summary = f'{role}: {_trim(text, 5000)}'
    elif not role and not text:
        rec.summary = '(empty)'
    return rec


def _system_summary(obj, sub):
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


def _attachment_summary(a):
    """Row title for an attachment: the most salient field per subtype."""
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


def _metadata_summary(obj, t):
    """Row title for a bookkeeping record: the salient field per type."""
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
    return t


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

def _is_idle_notification(body):
    """True for a teammate's auto-generated idle notice (not a real reply)."""
    body = body.lstrip()
    if not body.startswith('{'):
        return False
    try:
        return json.loads(body).get('type') == 'idle_notification'
    except Exception:
        return False
