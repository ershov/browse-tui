# ---- Claude extras: preview rendering from the raw record ----------------
#
# Claude's raw records carry detail the normalized ``Record`` does not
# (permission mode, structured ``toolUseResult`` shapes, attachments,
# bookkeeping types), so the agent renders every record itself through
# the ``extras_render`` hook:
#
#     rec.raw  ->  _classify(obj) -> renderer key
#              ->  _RENDERERS[key](obj) -> body str
#
# plus ``extras_chrome`` for the metadata footer and name-keyed entries
# in the common ``_TOOL_FORMATTERS`` registry for the fused tool
# umbrellas. Renderers are flat free functions returning ready-to-display
# strings with ANSI inlined via the palette constants.

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
            out.append(_fmt_tool_call(_tool_from_part(part)))
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
# ``_TOOL_UMBRELLA_FORMATTERS`` maps a Claude tool name to ``fn(inp, tur,
# raw_content, is_error) -> str | None`` (``tur`` is the structured
# ``toolUseResult``, ``raw_content`` the tool_result part's content). The
# entries are adapted into the common ``_TOOL_FORMATTERS`` registry at the
# end of this part; the common dispatcher takes the custom path only when
# EVERY call in the umbrella has a formatter AND every formatter returns a
# non-None string; any gap (an unregistered tool, an in-flight part with
# no result yet, a formatter that declines) falls back to the plain cascade.

_TOOL_UMBRELLA_FORMATTERS = {}

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

# ---- chrome footer (``extras_chrome``) -----------------------------------

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
    return _fmt_elapsed((b - a).total_seconds())

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


# ---- extras hooks + registry entries -------------------------------------


def extras_render(rec, bg='0'):
    """Every Claude record renders from its raw shape (see the banner)."""
    obj = rec.raw
    kind = _classify(obj)
    if kind == 'user':
        return _render_user(obj, bg)
    return _RENDERERS.get(kind, _render_unknown)(obj)


def extras_chrome(rec, root):
    return _fmt_chrome(
        rec.raw, root.raw.get('timestamp') if root is not None else None)


def _claude_umbrella(fmt):
    """Adapt a ``(inp, tur, raw_content, is_error)`` umbrella formatter to
    the common ``(call, result)`` contract."""
    def _f(call, result):
        inp = call['input'] or {}
        if result is None:
            return fmt(inp, None, None, False)
        return fmt(inp, result['tur'], result['content'], result['is_error'])
    return _f


for _name, _fmt in _TOOL_UMBRELLA_FORMATTERS.items():
    _TOOL_FORMATTERS.setdefault(_name, {})['umbrella'] = _claude_umbrella(_fmt)
del _name, _fmt
_TOOL_FORMATTERS.setdefault('mcp', {})['umbrella'] = lambda call, result: (
    _fmt_tool_umbrella_mcp(
        call['name'], call['input'] or {},
        result['tur'] if result is not None else None,
        result['content'] if result is not None else None,
        bool(result is not None and result['is_error'])))
_TOOL_FORMATTERS.setdefault('SendMessage', {})['call'] = (
    lambda call, result: _fmt_tool_use_send_message(call['input'] or {}))
# Notebook / multi edits keep the plain JSON request body; the ``edit``
# canon's path+change body would show an empty path for them.
for _name in ('NotebookEdit', 'MultiEdit'):
    _TOOL_FORMATTERS.setdefault(_name, {})['call'] = _fmt_call_default
del _name
