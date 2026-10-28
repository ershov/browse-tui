# ---- record rendering on normalized records ------------------------------
#
# A record preview is ``_render_record(rec)`` + ``_record_chrome(rec)``.
# Both consult the agent's optional extras first (``extras_render(rec,
# bg)`` / ``extras_chrome(rec, root)``, looked up by name at call time);
# a hook that is absent or returns ``None`` leaves the generic path below
# in charge. Tool bodies come from ``_TOOL_FORMATTERS``, keyed by canon
# (the generic entries here) or by an agent's own tool name (entries the
# agent's 055-extras adds); the name entry is tried first.


# canon or tool name → {'call': fn, 'result': fn, 'umbrella': fn}; each
# ``fn(call, result)`` takes the tool dicts (``result`` may be None) and
# returns a str, or None to decline (the umbrella then cascades).
_TOOL_FORMATTERS = {}


def _tool_formatter(tool, part):
    for key in (tool['name'], tool['canon']):
        entry = _TOOL_FORMATTERS.get(key)
        if entry is not None and part in entry:
            return entry[part]
    return None


def _call_input(call):
    return call['input'] if isinstance(call['input'], dict) else {}


def _from_input(fmt):
    """Adapt an ``fmt(input_dict)`` body formatter to the ``(call, result)`` contract."""
    return lambda call, result: fmt(_call_input(call))


# ---- call bodies (the request, under a ``🔧 Name`` header) ----------------


def _fmt_call_default(call, result):
    """JSON dump of the tool input, dimmed."""
    inp = call['input']
    if not inp:
        return ''
    try:
        text = json.dumps(inp, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(inp)
    return f'{GRAY}{text}{RESET}'


def _fmt_tool_call(tool):
    """``🔧 Name`` header plus the call body for one tool call."""
    head = f'{YELLOW}{BOLD}🔧 {tool["name"] or "?"}{RESET}'
    fmt = _tool_formatter(tool, 'call') or _fmt_call_default
    body = fmt(tool, None)
    return head + ('\n' + body if body else '')


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


def _fmt_tool_use_read(inp):
    path = inp.get('file_path', '')
    offset = inp.get('offset')
    limit = inp.get('limit')
    suffix = ''
    if offset is not None or limit is not None:
        suffix = f'  {MUTE}[{offset or 0}..+{limit or "eof"}]{RESET}'
    return f'{CYAN}{path}{RESET}{suffix}'


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


def _fmt_tool_use_write(inp):
    path = inp.get('file_path', '')
    content = inp.get('content', '')
    return f'{CYAN}{path}{RESET}\n{GRAY}{content}{RESET}'


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
    """Render a question payload (``[{question, header, options, multiSelect}]``)
    as markdown.

    Shared by both directions: the call side passes ``answers=None``; the
    result side passes the user's ``answers`` / ``annotations``. Output is
    plain markdown — the caller pipes it through ``_md_voice``.
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
    None on the call side). Options whose label matches the answer flip
    to a filled glyph. Any answer that doesn't match a known label is
    surfaced separately as ``**Other:** "..."``.
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


def _fmt_call_message(call, result):
    """Outbound agent message: ``→ recipient · summary`` + the body."""
    head = f'{BOLD}→ {call["peer"] or "?"}{RESET}'
    summary = _call_input(call).get('summary', '')
    if summary:
        head += f'  {MUTE}·{RESET} {summary}'
    out = [head]
    if call['text']:
        out.append('')
        out.append(_render_message_body(call['text']))
    return '\n'.join(out)


# ---- result bodies (under a ``↳ tool_result`` header) --------------------


def _color_unified_diff(diff):
    """Git-style colouring of unified-diff text (the no-``delta`` path)."""
    out = []
    for line in diff.rstrip('\n').split('\n'):
        if line.startswith('+') and not line.startswith('+++'):
            out.append(f'{GREEN}{line}{RESET}')
        elif line.startswith('-') and not line.startswith('---'):
            out.append(f'{RED}{line}{RESET}')
        elif line.startswith('@@'):
            out.append(f'{MUTE}{line}{RESET}')
        else:
            out.append(line)
    return '\n'.join(out)


def _result_text(result, markdown=False):
    """Display text of a result: its diff, else its output (JSON coloured
    when it parses; markdown through ``_md_voice`` when asked and it looks
    like markdown). ``''`` for a pending result."""
    if result is None:
        return ''
    if result['diff']:
        return _colorize_diff(result['diff']) if HAVE_DELTA \
            else _color_unified_diff(result['diff'])
    out = result['output'] or ''
    colored = _maybe_json_color(out)
    if colored is not None:
        return colored
    if markdown and _looks_markdown(out):
        return _md_voice(out)
    return out


def _fmt_result_default(call, result):
    return _result_text(result)


def _fmt_result_markdown(call, result):
    return _result_text(result, markdown=True)


def _tool_result_body(call, result):
    """Body of a result; an orphan result (no known call) resolves through
    its own canon-less dict, so it always gets the default body."""
    fmt = _tool_formatter(call or result, 'result') or _fmt_result_default
    return fmt(call, result)


# ---- umbrella blocks (request + result fused) ----------------------------


def _fmt_umbrella_bash(call, result):
    """A terminal session: ``$ command`` on the grey band, then the output.
    Never declines, so an in-flight command still reads naturally."""
    inp = _call_input(call)
    out = []
    desc = inp.get('description', '')
    if desc:
        out.append(f'{MUTE}# {desc}{RESET}')
    for i, line in enumerate(_bash_color(inp.get('command', '')).split('\n')):
        out.append(f'{CMD_BG}{"$ " if i == 0 else "  "}{line}{RESET}')
    extras = []
    if inp.get('timeout'):           extras.append(f'timeout={inp["timeout"]}ms')
    if inp.get('run_in_background'): extras.append('background')
    if extras:
        out.append(f'{MUTE}({" ".join(extras)}){RESET}')
    body = _result_text(result)
    if body:
        out.append(body.rstrip())
    return '\n'.join(out)


def _fmt_umbrella_read(call, result):
    """One header line — path, range and line count; no file content.
    Declines while the read is in flight."""
    if result is None or not result['output']:
        return None
    inp = _call_input(call)
    rng = f'[{inp.get("offset") or 0}..+{inp.get("limit") or "eof"}]'
    n = result['output'].count('\n') + 1
    return (f'{YELLOW}{BOLD}🔧 {call["name"]}{RESET}  '
            f'{CYAN}{inp.get("file_path", "")}{RESET}  '
            f'{MUTE}{rng}{RESET}  {MUTE}·{RESET}  {MUTE}{n} lines{RESET}')


def _fmt_umbrella_edit(call, result):
    """Only the resulting diff; declines without one."""
    if result is None or not result['diff']:
        return None
    return _result_text(result)


# Cap on the content lines shown inline in a Write umbrella preview. The
# full content stays available on the expanded tool_use leaf.
_WRITE_PREVIEW_LINES = 10


def _cap_content_lines(content, item_label):
    """GRAY-wrapped content capped at ``_WRITE_PREVIEW_LINES`` + a footer
    pointing the reader at the expandable leaf when truncated."""
    lines = content.split('\n')
    head = lines[:_WRITE_PREVIEW_LINES]
    out = [f'{GRAY}{chr(10).join(head)}{RESET}']
    rest = len(lines) - _WRITE_PREVIEW_LINES
    if rest > 0:
        out.append(f'{MUTE}… ({rest} more lines — expand the {item_label} '
                   f'item to see the full content){RESET}')
    return '\n'.join(out)


def _fmt_umbrella_write(call, result):
    """Header (path + line count) and a capped raw-content preview;
    declines when the request carries no content."""
    inp = _call_input(call)
    content = inp.get('content')
    if not content:
        return None
    header = (f'{YELLOW}{BOLD}🔧 {call["name"]}{RESET}  '
              f'{CYAN}{inp.get("file_path", "")}{RESET}  '
              f'{MUTE}·{RESET}  {MUTE}{len(content.split(chr(10)))} lines{RESET}')
    return header + '\n' + _cap_content_lines(content, call['name'])


def _umbrella_head_body(fmt_input, markdown=False):
    """``🔧 Name  <request>`` then the result body; declines on an empty request."""
    def _f(call, result):
        inp = _call_input(call)
        if not inp:
            return None
        head = f'{YELLOW}{BOLD}🔧 {call["name"]}{RESET}  ' + fmt_input(inp)
        body = _result_text(result, markdown)
        return head + ('\n\n' + body if body else '')
    return _f


def _fmt_umbrella_message(call, result):
    """Outbound message: header, the body once, then the delivery output."""
    if not call['input']:
        return None
    out = [f'{YELLOW}{BOLD}🔧 {call["name"]}{RESET}  '
           + _fmt_call_message(call, None)]
    body = _result_text(result)
    if body:
        out.append('')
        out.append(body)
    return '\n'.join(out)


def _fmt_umbrella_mcp(call, result):
    """``🔧 <server> ▸ <tool>`` + JSON request / result."""
    name = call['name'] or ''
    parts = name.split('__')
    label = f'{parts[1]} ▸ {"__".join(parts[2:])}' if len(parts) >= 3 else name
    out = [f'{YELLOW}{BOLD}🔧 {label}{RESET}']
    if call['input']:
        out.append('')
        out.append(_json_color(json.dumps(call['input'], indent=2,
                                          ensure_ascii=False)))
    body = _result_text(result)
    if body:
        out.append('')
        out.append(body)
    return '\n'.join(out)


_TOOL_FORMATTERS.update({
    'bash':      {'call': _from_input(_fmt_tool_use_bash), 'umbrella': _fmt_umbrella_bash},
    'read':      {'call': _from_input(_fmt_tool_use_read), 'umbrella': _fmt_umbrella_read},
    'edit':      {'call': _from_input(_fmt_tool_use_edit), 'umbrella': _fmt_umbrella_edit},
    'write':     {'call': _from_input(_fmt_tool_use_write), 'umbrella': _fmt_umbrella_write},
    'grep':      {'call': _from_input(_fmt_tool_use_grep),
                  'umbrella': _umbrella_head_body(_fmt_tool_use_grep)},
    'glob':      {'call': _from_input(_fmt_tool_use_glob),
                  'umbrella': _umbrella_head_body(_fmt_tool_use_glob)},
    'spawn':     {'call': _from_input(_fmt_tool_use_agent), 'result': _fmt_result_markdown},
    'skill':     {'call': _from_input(_fmt_tool_use_skill),
                  'umbrella': _umbrella_head_body(_fmt_tool_use_skill)},
    'question':  {'call': _from_input(_fmt_tool_use_ask_user_question)},
    'message':   {'call': _fmt_call_message, 'umbrella': _fmt_umbrella_message,
                  'result': _fmt_result_markdown},
    'webfetch':  {'umbrella': _umbrella_head_body(
                      lambda inp: f'{CYAN}{inp.get("url", "")}{RESET}', markdown=True),
                  'result': _fmt_result_markdown},
    'websearch': {'umbrella': _umbrella_head_body(
                      lambda inp: f'{WHITE}{inp.get("query", "")}{RESET}')},
    'mcp':       {'umbrella': _fmt_umbrella_mcp},
})


def _render_tool_umbrella(item_id):
    """One fused request+result block for a ``('tool', sess, key)`` row.

    Pairs each call of the record with its result by ``call_id`` and asks
    the registered umbrella formatter for each. Returns the joined blocks
    ONLY when every call has a formatter and every formatter returns a
    string; any gap (unregistered tool, declining formatter, failure)
    returns ``None`` so the caller falls back to the leaf cascade.
    """
    try:
        _tag, sess, key = item_id
        td = _scan_tree(sess)
    except Exception:
        return None
    rec = td.records[key] if 0 <= key < len(td.records) else None
    if rec is None or not _issues_tool_call(rec):
        return None
    results = {}
    for child in td.tool_children.get(key, []):
        if child.kind == 'tool_result':
            for t in child.tools:
                results.setdefault(t['call_id'], t)
    blocks = []
    for call in rec.tools:
        fmt = _tool_formatter(call, 'umbrella')
        if fmt is None:
            return None
        try:
            block = fmt(call, results.get(call['call_id']))
        except Exception:
            return None
        if block is None:
            return None
        blocks.append(block)
    return '\n\n'.join(blocks)


# ---- message bodies ------------------------------------------------------

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
    by outbound messages, worker replies and peer messages."""
    if isinstance(message, str) and re.match(r'^\s*\{', message):
        try:
            parsed = json.loads(message)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and parsed:
            return _render_json_fields(parsed)
    colored = _maybe_json_color(message)
    return colored if colored is not None else _md_voice(message)


# ---- record bodies -------------------------------------------------------


def _render_record(rec, bg='0', call=None):
    """Preview body for one record: the agent's ``extras_render`` when it
    answers, else the generic rendering by kind.

    ``bg`` is the md2ansi reset baseline for a banded turn-root prompt;
    ``call`` is the tool dict a ``tool_result`` answers, when known.
    """
    hook = globals().get('extras_render')
    if hook is not None:
        out = hook(rec, bg)
        if out is not None:
            return out
    kind = rec.kind
    if kind == 'user_prompt':
        return _render_prompt(rec, bg)
    if kind in ('assistant_text', 'thinking', 'tool_call'):
        return _render_assistant_record(rec)
    if kind == 'tool_result':
        return _render_result_record(rec, call)
    if kind == 'turn_start':
        return f'{MUTE}▷ turn start{RESET}'
    if kind == 'turn_end':
        ms = rec.meta.get('duration_ms')
        dur = f'  {WHITE}{ms / 1000:.2f}s{RESET}' if ms is not None else ''
        return f'{MUTE}⏱ turn{RESET}{dur}'
    if kind == 'compaction':
        return f'{YELLOW}{BOLD}♻ {rec.text or "Conversation compacted"}{RESET}'
    return _render_generic(rec)


def _render_prompt(rec, bg='0'):
    if rec.agent_voice is not None:
        if rec.agent_voice == 'reply':
            head = f'{CYAN}{BOLD}← reply{RESET}'
        else:
            head = f'{GREEN}{BOLD}← peer{RESET}'
        if rec.peer:
            head += f'  {MUTE}from{RESET} {rec.peer}'
        body = _render_message_body(rec.text) if rec.text else ''
    else:
        head = f'{WHITE}{BOLD}▶ user{RESET}'
        if rec.is_meta:
            head += f'  {MUTE}[meta]{RESET}'
        body = _md_voice(rec.text, bg)
    return f'{head}\n\n{body}' if body else head


def _render_assistant_record(rec):
    """Thinking, spoken text and tool calls, in that order."""
    head = f'{WHITE}{BOLD}⏺ assistant{RESET}'
    model = rec.meta.get('model')
    if model:
        head += f'  {MAGENTA}{model}{RESET}'
    if rec.end_turn:
        head += f'  {GREEN}[end_turn]{RESET}'
    out = [head, '']
    think = rec.meta.get('thinking') or (rec.text if rec.kind == 'thinking' else '')
    if isinstance(think, str) and think.strip():
        out.append(_rule('thinking'))
        out.append(f'{THINK}{think}{RESET}')
        out.append('')
    elif rec.kind == 'thinking' and rec.meta.get('encrypted'):
        out.append(f'{MUTE}(encrypted thinking){RESET}')
        out.append('')
    if rec.kind != 'thinking' and rec.text:
        out.append(_md_voice(rec.text))
        out.append('')
    for call in rec.tools:
        out.append(_fmt_tool_call(call))
        out.append('')
    return '\n'.join(out).rstrip() + '\n'


def _render_result_record(rec, call=None):
    result = rec.tool
    head = f'{WHITE}{BOLD}↳ tool_result{RESET}'
    if result is not None and result['is_error']:
        head += f'  {RED}[error]{RESET}'
    if result is not None and result['call_id']:
        head += f'  {MUTE}id={str(result["call_id"])[:8]}{RESET}'
    body = _tool_result_body(call, result) if result is not None else ''
    return (f'{head}\n\n{body}' if body else head).rstrip() + '\n'


def _render_generic(rec):
    """``system`` / ``meta`` / ``unknown`` records without an agent renderer:
    the label, the text or summary, and the raw source object as JSON for
    ``unknown`` records (and for the others at detail level 6+)."""
    head = f'{GRAY}{BOLD}{rec.tag}{RESET}'
    body = rec.text or (rec.summary if rec.summary and rec.summary != rec.tag else '')
    out = [head]
    if body:
        out.append('')
        out.append(body)
    if rec.kind == 'unknown' or _DETAIL_LEVEL >= 6:
        try:
            dump = json.dumps(rec.raw, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            dump = str(rec.raw)
        out.append('')
        out.append(f'{GRAY}{dump}{RESET}')
    return '\n'.join(out)


# ---- chrome footer -------------------------------------------------------


def _fmt_elapsed(secs):
    """``+1m30s``-style signed duration."""
    sign = '+' if secs >= 0 else '-'
    secs = abs(secs)
    if secs < 60:
        return f'{sign}{secs:.1f}s'
    if secs < 3600:
        m, s = divmod(int(secs), 60)
        return f'{sign}{m}m{s:02d}s'
    h, rem = divmod(int(secs), 3600)
    return f'{sign}{h}h{rem // 60:02d}m'


def _record_chrome(rec, root=None):
    """Metadata footer of a record preview: the agent's ``extras_chrome``
    when it answers, else the generic rows from ``id``, ``ts`` and ``meta``
    (``model``, ``agent``, ``duration_ms``, ``tokens``, ``cost``).

    ``root`` is the turn-root record (``_turn_root_record``) whose ``ts``
    anchors the ``offset`` row; ``''`` when there is nothing to show.
    """
    hook = globals().get('extras_chrome')
    if hook is not None:
        out = hook(rec, root)
        if out is not None:
            return out
    rows = []
    if rec.id:
        rows.append(('id', str(rec.id)))
    if rec.ts is not None:
        try:
            shown = datetime.datetime.fromtimestamp(rec.ts).astimezone() \
                .strftime('%Y-%m-%d %H:%M:%S %Z')
        except (OverflowError, OSError, ValueError):
            shown = str(rec.ts)
        rows.append(('timestamp', shown))
        if root is not None and root.ts is not None:
            rows.append(('offset', _fmt_elapsed(rec.ts - root.ts)))
    for key, label in (('model', 'model'), ('agent', 'agent'),
                       ('tokens', 'tokens'), ('cost', 'cost')):
        v = rec.meta.get(key)
        if v not in (None, ''):
            rows.append((label, f'{v:,}' if isinstance(v, int) else str(v)))
    ms = rec.meta.get('duration_ms')
    if ms is not None:
        rows.append(('duration', f'{ms / 1000:.2f}s'))
    if not rows:
        return ''
    w = max(len(k) for k, _ in rows)
    return '\n'.join([_rule()] + [
        f'{MUTE}{k.ljust(w)}:{RESET} {v}' for k, v in rows])


def _turn_root_record(sess, n):
    """The turn-root record that opened record ``n``'s turn, or ``None``.

    ``None`` when the record *is* the turn root, when it sits in an
    out-of-turn ``<system>`` span, or when the session is unknown. Stays
    within ``sess`` — a child transcript times against its own dispatch
    prompt, not the parent session's request.
    """
    td = _scan_tree(sess)
    parent_id = td.parent_id_of.get(n)
    if parent_id is None:
        return None
    if parent_id[0] == 'prompt':
        root_key = parent_id[2]
    elif parent_id[0] == 'tool':
        root_key = td.tool_to_turn_root_line.get(parent_id[2])
    else:
        return None
    if root_key is None or root_key == n or not (0 <= root_key < len(td.records)):
        return None
    return td.records[root_key]


def _owner_call(sess, rec):
    """The tool dict a ``tool_result`` record answers, or ``None``."""
    if rec.kind != 'tool_result' or rec.tool is None:
        return None
    td = _scan_tree(sess)
    parent_id = td.parent_id_of.get(rec.key)
    if parent_id is None or parent_id[0] != 'tool':
        return None
    owner = td.records[parent_id[2]] if 0 <= parent_id[2] < len(td.records) else None
    if owner is None:
        return None
    return next((t for t in owner.tools if t['call_id'] == rec.tool['call_id']), None)


# ---- leaf previews -------------------------------------------------------


def _preview_message(sess, n):
    """Per-record preview for a leaf: body + chrome footer."""
    rec = _record_at(sess, n)
    if rec is None:
        return ''
    body = _render_record(rec, call=_owner_call(sess, rec))
    chrome = _record_chrome(rec, _turn_root_record(sess, n))
    return body + ('\n' + chrome if chrome else '')


def _render_record_with_rule(sess, n):
    """Read one record, render its body, prefix with a section rule.

    Most leaves get the fixed ~60-wide bright-yellow rule titled with the
    record's label. A turn-opening user prompt gets a FULL-WIDTH dark-blue
    band for its divider (bold-white title) AND has its whole prompt
    painted on the same dark-blue background — ``bg`` keeps the band
    behind the voice markdown's styled spans, ``_band_block`` re-asserts
    it per logical line — so successive turns stand out as the visual
    anchors of a composed multi-turn preview.
    """
    rec = _record_at(sess, n)
    if rec is None:
        return None
    if _is_turn_root(rec):
        width = getattr(_BROWSER, 'preview_width', 0) or 60
        rule = _rule(rec.tag, BOLD + WHITE, width=width, bg=BLUE_BG)
        cs = BLUE_BG_PARAMS if BLUE_BG else '0'
        body = _band_block(_render_record(rec, bg=cs), BLUE_BG)
        return rule + '\n' + body
    body = _render_record(rec, call=_owner_call(sess, rec))
    return _rule(rec.tag, YELLOW) + '\n' + body
