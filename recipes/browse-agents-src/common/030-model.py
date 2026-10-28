# ---- store contract ------------------------------------------------------
#
# The view code in common/ never touches an agent's storage. Everything it
# needs comes through the plain module-level functions below, which each
# agent's 025-store / 035-parse / 045-tail define (DESIGN.md "Store
# contract" is the normative description). Keys are opaque here except
# for ``record_key``, a dense 0-based ordinal in transcript order that
# indexes the cached ``_TreeData.records`` list.
#
#   identity      store_name()  store_help_intro()  store_help_usage()
#   startup       store_init()  store_pop_args()  store_resolve_positional(arg)
#                 store_reset()
#   projects      store_list_projects()  store_list_sessions(project_key)
#                 store_project_path(project_key)  store_project_of_session(session_key)
#   sessions      store_session_exists(session_key)  store_session_item(session_key)
#                 store_session_info(session_key)  store_session_dirs(session_key)
#                 store_context_dirs(project_key, session_key)
#   children      store_child_sessions(session_key)  store_mark_orphan(item)
#                 store_child_session(session_key, child_key)
#                 store_parent_of_child(session_key)
#                 store_resolve_spawned_child(session_key, call, result)
#   source        store_source_path(session_key)
#                 store_source_locator(session_key, record_key)
#                 store_source_excerpt({session_key: [record_key, …]})
#   tail          store_tail_open(session_key)  store_tail_poll(session_key, state)
#                 store_tail_interval()
#
# The tail functions yield ``Record`` instances (below); the agent's
# 035-parse maps its raw objects to them. Optional extras (055-extras):
# ``extras_render(rec, bg)``, ``extras_chrome(rec, root)`` and entries in
# the ``_TOOL_FORMATTERS`` registry (050-render).


# ---- normalized record model ---------------------------------------------
#
# Every record the view handles is a ``Record``. The agent parser fills
# the kind, the row presentation hints and the facts the fold / filter /
# summaries need; ``raw`` keeps the source object for the agent's own
# preview renderers and the source view.

_KINDS = frozenset([
    'user_prompt', 'assistant_text', 'thinking', 'tool_call', 'tool_result',
    'turn_start', 'turn_end', 'compaction', 'system', 'meta', 'unknown',
])

_TOOL_CANONS = frozenset([
    'bash', 'read', 'edit', 'write', 'grep', 'glob', 'spawn', 'message',
    'question', 'webfetch', 'websearch', 'skill', 'mcp', 'other',
])

# Lowest detail level that shows a record of each kind when the parser
# leaves ``min_level`` unset. A ``tool_call`` of canon edit/write is 3.
_DEFAULT_MIN_LEVEL = {
    'user_prompt': 1, 'assistant_text': 2, 'thinking': 2,
    'tool_call': 4, 'tool_result': 4,
    'turn_start': 5, 'turn_end': 5, 'compaction': 5, 'system': 5,
    'meta': 6, 'unknown': 7,
}
_EDIT_CANONS = frozenset(['edit', 'write'])

# Row presentation per kind when the parser leaves ``tag_style`` /
# ``row_bg`` unset (``None``). The stripe applies to voice records only.
_DEFAULT_TAG_STYLE = {
    'user_prompt': 'cyan', 'assistant_text': 'green', 'thinking': 'green',
    'tool_call': 'green', 'tool_result': '', 'system': 'gray', 'meta': 'dim',
}
_DEFAULT_ROW_BG = {
    'user_prompt': 17, 'assistant_text': 235, 'thinking': 235, 'tool_call': 235,
}

# Row stripe shared by subagent child rows and the ``<tool:>`` umbrella
# that dispatched them (the assistant stripe).
_ROW_BG_SPAWN = 235


def new_tool(**fields):
    """A tool call or result dict with every key present."""
    tool = {
        'canon': 'other',   # one of _TOOL_CANONS
        'name': None,       # the agent's own tool name
        'call_id': None,    # pairs a result with its call
        'input': None,      # call arguments (dict or str)
        'output': None,     # result text
        'glance': None,     # short result text for the row title; None = output
        'diff': None,       # unified diff of an edit result
        'is_error': False,
        'peer': None,       # canon message: recipient; canon spawn: teammate name
        'text': None,       # canon message: the message body
    }
    tool.update(fields)     # an agent may add keys for its own extras
    return tool


class Record:
    """One transcript record in the agent-independent shape."""

    __slots__ = (
        'key',          # dense ordinal in the session (``None`` outside a session)
        'kind',         # one of _KINDS
        'raw',          # the agent's source object
        'id',           # the agent's record id, when it has one
        'ts',           # epoch seconds or None
        'turn_id',      # explicit turn id or None (the fold derives turns)
        'tag',          # row label; defaults to the kind
        'tag_style',    # row tag style name; None = kind default, '' = plain
        'row_bg',       # 256-colour row stripe; None = kind default for voice rows
        'text',         # plain body: prompt, assistant text, thinking, …
        'summary',      # parser-supplied row title, or None for the kind default
        'tools',        # tool call / result dicts (``new_tool``); calls for an
                        # assistant record, results for a result record
        'call_id',      # for a record that annotates a call without being a
                        # call or result (hook output): that call's id
        'caller_id',    # id of the record that issued the call this record
                        # answers, when the agent stamps it
        'peer',         # sender name of inbound agent voice
        'agent_voice',  # inbound inter-agent voice: 'reply' (a worker's task
                        # result), 'peer' (another session's message), 'idle'
                        # (a worker's automatic idle notice); else None
        'is_voice',     # the record carries speech (drives stripes and jumps)
        'is_meta',      # injected context, never typed or spoken
        'end_turn',     # an assistant record that concluded its turn
        'min_level',    # explicit detail level, or None for the kind default
        'meta',         # small facts: agent, title, title_by, duration_ms,
                        # thinking (the readable thinking text, also when
                        # the record has spoken text),
                        # span_label (finer label for <system> span titles), …
    )

    def __init__(self, kind, raw, key=None, **fields):
        self.key = key
        self.kind = kind
        self.raw = raw
        self.id = None
        self.ts = None
        self.turn_id = None
        self.tag = kind
        self.tag_style = None
        self.row_bg = None
        self.text = ''
        self.summary = None
        self.tools = []
        self.call_id = None
        self.caller_id = None
        self.peer = None
        self.agent_voice = None
        self.is_voice = False
        self.is_meta = False
        self.end_turn = False
        self.min_level = None
        self.meta = {}
        for k, v in fields.items():
            setattr(self, k, v)

    @property
    def tool(self):
        """The first tool call / result, or ``None``."""
        return self.tools[0] if self.tools else None

    def __repr__(self):
        return f'Record({self.kind!r}, key={self.key!r}, tag={self.tag!r})'


# ---- classification on normalized records --------------------------------


def _record_min_level(rec):
    """Minimum detail level (1-7) at which ``rec`` is visible."""
    if rec.min_level is not None:
        return rec.min_level
    if (rec.kind == 'tool_call' and rec.tool is not None
            and rec.tool['canon'] in _EDIT_CANONS):
        return 3
    return _DEFAULT_MIN_LEVEL.get(rec.kind, 7)


def _record_tag_style(rec):
    if rec.tag_style is not None:
        return rec.tag_style
    return _DEFAULT_TAG_STYLE.get(rec.kind, '')


def _record_row_bg(rec):
    if rec.row_bg is not None:
        return rec.row_bg
    return _DEFAULT_ROW_BG.get(rec.kind) if rec.is_voice else None


def _is_inbound_agent_voice(rec):
    """A worker's reply, a peer session's message or an idle notice."""
    return rec.agent_voice is not None


def _issues_tool_call(rec):
    """True iff ``rec`` carries tool calls (it is wrapped in a ``<tool:>`` umbrella)."""
    return bool(rec.tools) and rec.kind != 'tool_result'


def _is_turn_root(rec):
    """A fresh human prompt opens a turn; inbound agent voice does not
    (``_fold_record`` re-opens the last human turn for it)."""
    return (rec.kind == 'user_prompt' and rec.is_voice and not rec.is_meta
            and rec.agent_voice is None)


def _is_turn_close(rec):
    return rec.kind == 'turn_end'


def _is_continuation_activity(rec):
    """Assistant activity (or a tool result) after a turn closed with no
    new prompt — it belongs to the request that started it."""
    if rec.kind in ('assistant_text', 'thinking', 'tool_call'):
        return True
    return rec.kind == 'tool_result' and not rec.is_meta


# ---- row summaries -------------------------------------------------------


def _row_tag(rec):
    """Row tag: the label plus ``· <agent>`` / ``· end_turn`` / ``· thinking``
    suffixes on assistant rows."""
    tag = rec.tag
    if rec.kind in ('assistant_text', 'thinking', 'tool_call'):
        agent = rec.meta.get('agent')
        if agent:
            tag = f'{tag} · {agent}'
        if rec.end_turn:
            tag = f'{tag} · end_turn'
        elif rec.meta.get('thinking'):
            tag = f'{tag} · thinking'
    return tag


def _summarise_message(rec):
    """One-line row title (newlines kept; callers ``_oneline`` it)."""
    if rec.summary is not None:
        return rec.summary
    kind = rec.kind
    if kind == 'tool_call':
        tool = rec.tool
        name = tool['name'] or '?'
        extra = _tool_use_one_line(tool)
        return f'🔧 {name}({extra})' if extra else f'🔧 {name}'
    if kind == 'tool_result':
        return f'↳ tool_result: {_tool_result_one_line(rec.tool)}'
    if kind == 'thinking' and rec.text:
        return f'🧠 thinking: {_trim(rec.text, 5000)}'
    text = _trim(rec.text, 5000)
    if kind in ('user_prompt', 'assistant_text', 'thinking'):
        label = f'{rec.tag}/meta' if rec.is_meta else rec.tag
        return f'{label}: {text}' if text else label
    return text or rec.tag


def _tool_use_one_line(tool):
    """Compact one-line summary of a call's input, by canon."""
    inp = tool['input'] if isinstance(tool['input'], dict) else {}
    canon = tool['canon']
    if canon == 'bash':
        return _trim(inp.get('command', ''), 5000)
    if canon in ('read', 'edit', 'write'):
        return inp.get('file_path', '')
    if canon == 'grep':
        return f'/{inp.get("pattern", "")}/'
    if canon == 'glob':
        return inp.get('pattern', '')
    if canon == 'spawn':
        return inp.get('subagent_type', '?')
    if canon == 'skill':
        return inp.get('skill', '?')
    if canon == 'message':
        return f'→ {tool["peer"] or "?"}: {inp.get("summary", "")}'
    return ''


def _tool_result_one_line(tool):
    """One-liner peek at a result for the row title: its glance, else output."""
    text = tool['glance'] if tool['glance'] is not None else tool['output']
    return _trim(text or '', 5000)
