# ---- raw Codex rollout line → normalized Record --------------------------
#
# A rollout line is ``{timestamp, ordinal?, type, payload}``. Codex writes
# most facts twice: the raw model exchange as ``response_item`` lines and
# the rendered item as ``event_msg/item_completed`` (0.153+; 0.104 has
# only the raw lines plus ``event_msg/user_message``). The order is fixed:
#
#   user:       response_item message/user      → item UserMessage
#   assistant:  item AgentMessage               → response_item message/assistant
#   reasoning:  item Reasoning                  → response_item reasoning
#   tool:       response_item custom_tool_call  → item CommandExecution |
#               FileChange                      → response_item *_output
#
# so the parser keeps a ``_ParseCtx`` across lines (held in the tail
# state); DESIGN.md "Codex CLI" has the mapping.


class _ParseCtx:
    """Cross-line parse state for one rollout."""

    __slots__ = ('item_ids', 'pending', 'answered', 'model', 'tokens',
                 'turn_id')

    def __init__(self):
        self.item_ids = set()   # ids of rendered item_completed items
        self.pending = []       # (call_id, canon) awaiting a result, oldest first
        self.answered = set()   # call_ids an item already answered
        self.model = None
        self.tokens = None
        self.turn_id = None


_CANON_BY_TOOL = {
    'exec_command': 'bash', 'shell': 'bash', 'shell_command': 'bash',
    'local_shell': 'bash', 'container.exec': 'bash',
    'apply_patch': 'edit',
    'spawn_agent': 'spawn',
    'web_search': 'websearch',
    'view_image': 'read', 'read_file': 'read',
}

# Detail levels for the bookkeeping records (DESIGN.md: 5 detailed, 6 all).
_META_LEVEL = {
    'session_meta': 5, 'turn_context': 5, 'thread_settings_applied': 5,
    'token_count': 6, 'token_usage_record': 6, 'world_state': 6,
}

_TURN_END_LEVEL = 4


def _tool_canon(name):
    if name in _CANON_BY_TOOL:
        return _CANON_BY_TOOL[name]
    if isinstance(name, str) and name.startswith('mcp__'):
        return 'mcp'
    return 'other'


def _text_of(content):
    """Joined text of a content list (``input_text`` / ``output_text`` /
    ``text`` parts) or the bare string."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    return '\n\n'.join(
        p['text'] for p in content
        if isinstance(p, dict) and isinstance(p.get('text'), str) and p['text']
    )


def _iso_epoch(iso):
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


def _first_line(text, n=5000):
    return _trim((text or '').strip().split('\n', 1)[0], n)


# ---- the ``exec`` custom tool: JavaScript around tools.exec_command /
# tools.apply_patch --------------------------------------------------------

_EXEC_CALL_RE = re.compile(r'tools\.exec_command\(\s*(?=\{)')
_PATCH_LITERAL_RE = re.compile(r'const\s+patch\s*=\s*(?=")')
_PATCH_FILE_RE = re.compile(r'^\*\*\* (?:Update|Add|Delete) File: (.+)$', re.M)
_decoder = json.JSONDecoder()


def _exec_args(js):
    """The ``{cmd, workdir, …}`` object of the first ``tools.exec_command``
    call in the script, or ``None``."""
    m = _EXEC_CALL_RE.search(js)
    if m is None:
        return None
    try:
        obj, _end = _decoder.raw_decode(js, m.end())
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _patch_text(js):
    """The patch string literal of a ``const patch = "..."`` script."""
    m = _PATCH_LITERAL_RE.search(js)
    if m is None or 'tools.apply_patch' not in js:
        return None
    try:
        text, _end = _decoder.raw_decode(js, m.end())
    except ValueError:
        return None
    return text if isinstance(text, str) else None


def _patch_paths(patch):
    return _PATCH_FILE_RE.findall(patch or '')


def _tool_from_call(name, inp, call_id):
    """Tool dict for a ``custom_tool_call`` / ``function_call`` payload.

    The ``exec`` custom tool wraps JavaScript; it unwraps to
    ``exec_command`` (canon bash, ``input.command`` / ``cwd``) or
    ``apply_patch`` (canon edit, ``input.patch`` / ``file_path``).
    ``function_call`` arguments arrive as a JSON string.
    """
    if isinstance(inp, str) and name == 'exec':
        args = _exec_args(inp)
        patch = _patch_text(inp) if args is None else None
        if args is not None:
            name = 'exec_command'
            inp = {'command': args.get('cmd', ''), 'cwd': args.get('workdir')}
        elif patch is not None:
            name = 'apply_patch'
            inp = {'patch': patch, 'file_path': ', '.join(_patch_paths(patch))}
        else:
            inp = {'script': inp}
    elif isinstance(inp, str):
        try:
            parsed = json.loads(inp)
        except ValueError:
            parsed = None
        inp = parsed if isinstance(parsed, dict) else {'arguments': inp}
    elif not isinstance(inp, dict):
        inp = {}
    canon = _tool_canon(name)
    if canon == 'bash' and not isinstance(inp.get('command'), str):
        cmd = inp.get('command') or inp.get('cmd')
        inp['command'] = ' '.join(str(c) for c in cmd) if isinstance(cmd, list) else (cmd or '')
    if canon == 'edit' and 'file_path' not in inp:
        inp['file_path'] = ', '.join(_patch_paths(inp.get('patch') or inp.get('input')))
    return new_tool(canon=canon, name=name, call_id=call_id, input=inp)


# ---- item_completed items ------------------------------------------------


def _changes_diff(changes):
    """One unified diff over a ``FileChange.changes`` map (``path →
    {type, unified_diff, move_path}``); Codex omits the file headers."""
    out = []
    if not isinstance(changes, dict):
        return None
    for path, ch in changes.items():
        if not isinstance(ch, dict):
            continue
        kind = ch.get('type')
        new = ch.get('move_path') or path
        out.append('--- ' + ('/dev/null' if kind == 'add' else path))
        out.append('+++ ' + ('/dev/null' if kind == 'delete' else new))
        body = (ch.get('unified_diff') or '').rstrip('\n')
        if body:
            out.append(body)
    return '\n'.join(out) + '\n' if out else None


def _command_text(item):
    """The shell command of a ``CommandExecution`` item."""
    cmds = [pc.get('cmd') for pc in item.get('parsed_cmd') or []
            if isinstance(pc, dict) and pc.get('cmd')]
    if cmds:
        return '\n'.join(cmds)
    cmd = item.get('command')
    if isinstance(cmd, list):
        if len(cmd) == 3 and cmd[1] in ('-lc', '-c'):
            return cmd[2]
        return ' '.join(str(c) for c in cmd)
    return str(cmd or '')


def _item_result(item, call_id):
    """Tool dict of a completed ``CommandExecution`` / ``FileChange``."""
    kind = item.get('type')
    failed = item.get('status') == 'failed'
    if kind == 'CommandExecution':
        exit_code = item.get('exit_code')
        out = item.get('aggregated_output') or item.get('stdout') or ''
        if not out and item.get('stderr'):
            out = item['stderr']
        glance = _trim(out, 5000) or '(no output)'
        if exit_code not in (0, None):
            glance = f'exit {exit_code} · {glance}'
        return new_tool(
            canon='bash', name='exec_command', call_id=call_id, output=out,
            glance=glance, is_error=failed or exit_code not in (0, None),
            exit_code=exit_code, duration=item.get('duration'),
            cwd=item.get('cwd'), status=item.get('status'),
            parsed_cmd=item.get('parsed_cmd'),
        )
    changes = item.get('changes') if isinstance(item.get('changes'), dict) else {}
    paths = list(changes)
    glance = f'{len(paths)} file(s): {", ".join(paths)}' if paths else None
    return new_tool(
        canon='edit', name='apply_patch', call_id=call_id,
        output=item.get('stdout') or item.get('stderr') or None,
        glance=glance, diff=_changes_diff(changes), is_error=failed,
        status=item.get('status'),
    )


def _item_call(item):
    """A tool_call dict for an item that arrived with no pending call."""
    if item.get('type') == 'CommandExecution':
        return new_tool(canon='bash', name='exec_command', call_id=item.get('id'),
                        input={'command': _command_text(item),
                               'cwd': item.get('cwd')})
    changes = item.get('changes') if isinstance(item.get('changes'), dict) else {}
    return new_tool(canon='edit', name='apply_patch', call_id=item.get('id'),
                    input={'file_path': ', '.join(changes)})


def _parse_item(rec, item, ctx):
    """Fill ``rec`` from an ``item_completed`` item."""
    kind = item.get('type')
    rec.id = item.get('id') or None
    if rec.id:
        ctx.item_ids.add(rec.id)
    if kind == 'UserMessage':
        rec.kind, rec.tag = 'user_prompt', 'user'
        rec.text = _text_of(item.get('content'))
        rec.is_voice = True
    elif kind == 'AgentMessage':
        rec.kind, rec.tag = 'assistant_text', 'assistant'
        rec.text = _text_of(item.get('content'))
        rec.is_voice = True
        _set_phase(rec, item.get('phase'))
        _stamp_model(rec, ctx)
    elif kind == 'Reasoning':
        _fill_thinking(rec, item.get('summary_text'), ctx)
    elif kind in ('CommandExecution', 'FileChange'):
        rec.tag = 'command' if kind == 'CommandExecution' else 'file_change'
        canon = 'bash' if kind == 'CommandExecution' else 'edit'
        call_id = _take_pending(ctx, canon)
        if call_id is not None:
            rec.kind = 'tool_result'
            rec.tools = [_item_result(item, call_id)]
            if canon == 'edit':
                rec.min_level = 3    # shows with its apply_patch call
        else:
            rec.kind = 'tool_call'
            rec.tools = [_item_call(item)]
            _stamp_model(rec, ctx)
    else:
        rec.kind, rec.tag = 'unknown', f'item:{kind}'
        rec.summary = f'item {kind}'
    return rec


def _take_pending(ctx, canon):
    """Pop the oldest pending call of ``canon``; ``None`` when there is none."""
    for i, (call_id, c) in enumerate(ctx.pending):
        if c == canon:
            del ctx.pending[i]
            ctx.answered.add(call_id)
            return call_id
    return None


def _fill_thinking(rec, summary, ctx):
    rec.kind, rec.tag = 'thinking', 'reasoning'
    if isinstance(summary, list):
        text = '\n\n'.join(
            s if isinstance(s, str) else (s.get('text') or '')
            for s in summary if s)
    else:
        text = summary if isinstance(summary, str) else ''
    rec.text = text.strip()
    if rec.text:
        rec.is_voice = True
    else:
        rec.meta['encrypted'] = True
        rec.min_level = 6
        rec.summary = '🧠 reasoning (encrypted)'
    _stamp_model(rec, ctx)
    return rec


def _set_phase(rec, phase):
    """A ``final_answer`` closes the turn and shows at the summary level."""
    if phase == 'final_answer':
        rec.end_turn = True
        rec.min_level = 1


def _stamp_model(rec, ctx):
    if ctx.model:
        rec.meta['model'] = ctx.model


def _duplicate(rec, label):
    rec.kind, rec.min_level = 'meta', 6
    rec.tag = 'raw'
    rec.summary = f'raw {label} (rendered from its item)'
    return rec


# ---- response_item payloads ----------------------------------------------


def _parse_response_item(rec, p, ctx):
    pt = p.get('type')
    rec.id = p.get('id') or None
    if pt == 'message':
        role = p.get('role')
        text = _text_of(p.get('content'))
        if role == 'assistant':
            if rec.id and rec.id in ctx.item_ids:
                return _duplicate(rec, 'assistant message')
            rec.kind, rec.tag = 'assistant_text', 'assistant'
            rec.text, rec.is_voice = text, True
            _set_phase(rec, p.get('phase'))
            _stamp_model(rec, ctx)
        elif role == 'developer':
            rec.kind, rec.tag = 'system', 'developer'
            rec.text, rec.is_meta = text, True
            rec.summary = f'developer: {_first_line(text)}'
            rec.meta['span_label'] = 'developer'
        else:
            rec.kind, rec.tag, rec.min_level = 'meta', 'raw', 6
            rec.is_meta = True
            rec.text = text
            rec.summary = f'raw {role or "?"} message: {_first_line(text, 200)}'
        return rec
    if pt == 'reasoning':
        if rec.id and rec.id in ctx.item_ids:
            return _duplicate(rec, 'reasoning')
        return _fill_thinking(rec, p.get('summary'), ctx)
    if pt in ('custom_tool_call', 'function_call'):
        rec.kind, rec.tag = 'tool_call', 'assistant'
        inp = p.get('input') if pt == 'custom_tool_call' else p.get('arguments')
        tool = _tool_from_call(p.get('name'), inp, p.get('call_id'))
        rec.tools = [tool]
        if tool['call_id']:
            ctx.pending.append((tool['call_id'], tool['canon']))
        _stamp_model(rec, ctx)
        return rec
    if pt in ('custom_tool_call_output', 'function_call_output'):
        call_id = p.get('call_id')
        if call_id in ctx.answered:
            rec.call_id = call_id    # folds under the call like an annotation
            return _duplicate(rec, 'tool output')
        ctx.pending = [pc for pc in ctx.pending if pc[0] != call_id]
        rec.kind, rec.tag = 'tool_result', 'tool_output'
        out = _text_of(p.get('output'))
        rec.tools = [new_tool(call_id=call_id, output=out)]
        return rec
    rec.kind, rec.tag = 'unknown', f'response_item:{pt}'
    return rec


# ---- event_msg payloads --------------------------------------------------


def _parse_event(rec, p, ctx):
    pt = p.get('type')
    rec.turn_id = p.get('turn_id') or ctx.turn_id
    if pt == 'item_completed':
        item = p.get('item') if isinstance(p.get('item'), dict) else {}
        return _parse_item(rec, item, ctx)
    if pt == 'user_message':
        rec.kind, rec.tag = 'user_prompt', 'user'
        rec.text = p.get('message') or ''
        rec.is_voice = True
        return rec
    if pt == 'task_started':
        ctx.turn_id = rec.turn_id = p.get('turn_id')
        ctx.pending = []    # an interrupted call never answers across turns
        rec.kind, rec.tag = 'turn_start', 'turn'
        rec.summary = 'turn started'
        return rec
    if pt in ('task_complete', 'turn_aborted'):
        rec.kind, rec.tag = 'turn_end', 'turn'
        rec.min_level = _TURN_END_LEVEL
        ms = p.get('duration_ms')
        if ms is not None:
            rec.meta['duration_ms'] = ms
        if ctx.tokens is not None:
            rec.meta['tokens'] = ctx.tokens
        if pt == 'turn_aborted':
            what = f'turn aborted ({p.get("reason") or "?"})'
        elif isinstance(p.get('error'), dict):
            what = f'turn failed: {p["error"].get("message") or "?"}'
        else:
            what = 'turn complete'
        rec.summary = what + (f' · {ms / 1000:.1f}s' if ms is not None else '')
        ctx.turn_id = None
        ctx.pending = []
        return rec
    if pt == 'token_count':
        info = p.get('info') if isinstance(p.get('info'), dict) else {}
        total = (info.get('total_token_usage') or {}).get('total_tokens')
        if total is not None:
            ctx.tokens = total
        bits = ['tokens']
        if total is not None:
            bits.append(f'total {total:,}')
        last = (info.get('last_token_usage') or {}).get('total_tokens')
        if last is not None:
            bits.append(f'last {last:,}')
        if info.get('model_context_window'):
            bits.append(f'window {info["model_context_window"]:,}')
        primary = (p.get('rate_limits') or {}).get('primary') or {}
        if primary.get('used_percent') is not None:
            bits.append(f'rate {primary["used_percent"]:.0f}%')
        rec.kind, rec.tag, rec.min_level = 'meta', 'token_count', 6
        rec.summary = ' · '.join(bits)
        return rec
    if pt == 'thread_settings_applied':
        s = p.get('thread_settings') if isinstance(p.get('thread_settings'), dict) else {}
        bits = ['settings']
        for key in ('model', 'reasoning_effort', 'approval_policy'):
            if s.get(key):
                bits.append(f'{key} {s[key]}')
        if s.get('model'):
            ctx.model = s['model']
        rec.kind, rec.tag, rec.min_level = 'meta', 'settings', 5
        rec.summary = ' · '.join(bits)
        return rec
    rec.kind, rec.tag = 'unknown', f'event:{pt}'
    return rec


# ---- entry point ----------------------------------------------------------


def _parse_record(raw, key, ctx):
    """Map one rollout line to a ``Record``, updating ``ctx``."""
    t = raw.get('type')
    p = raw.get('payload') if isinstance(raw.get('payload'), dict) else {}
    rec = Record('unknown', raw, key, ts=_iso_epoch(raw.get('timestamp')),
                 tag=t or 'unknown', turn_id=ctx.turn_id)
    if t == 'response_item':
        return _parse_response_item(rec, p, ctx)
    if t == 'event_msg':
        return _parse_event(rec, p, ctx)
    if t == 'session_meta':
        rec.kind, rec.min_level = 'meta', _META_LEVEL[t]
        bits = ['session', f'codex {p.get("cli_version") or "?"}']
        for key_ in ('originator', 'source', 'cwd'):
            if p.get(key_):
                bits.append(str(p[key_]))
        rec.summary = ' · '.join(bits)
        return rec
    if t == 'turn_context':
        rec.kind, rec.min_level = 'meta', _META_LEVEL[t]
        rec.turn_id = p.get('turn_id') or rec.turn_id
        if p.get('model'):
            ctx.model = p['model']
        bits = ['turn_context']
        if p.get('model'):
            bits.append(f'model {p["model"]}')
        if p.get('approval_policy'):
            bits.append(f'approval {p["approval_policy"]}')
        sandbox = p.get('sandbox_policy')
        if isinstance(sandbox, dict) and sandbox.get('type'):
            bits.append(f'sandbox {sandbox["type"]}')
        rec.summary = ' · '.join(bits)
        return rec
    if t == 'token_usage_record':
        rec.kind, rec.min_level, rec.tag = 'meta', _META_LEVEL[t], 'token_usage'
        usage = p.get('usage') if isinstance(p.get('usage'), dict) else {}
        rec.turn_id = p.get('turn_id') or rec.turn_id
        rec.summary = (f'usage · in {usage.get("input_tokens", 0):,} · '
                       f'out {usage.get("output_tokens", 0):,} · '
                       f'total {usage.get("total_tokens", 0):,}')
        return rec
    if t == 'world_state':
        rec.kind, rec.min_level = 'meta', _META_LEVEL[t]
        rec.summary = 'world_state' + (' (full)' if p.get('full') else ' (delta)')
        return rec
    rec.summary = f'{t or "(no type)"}'
    return rec
