# ---- OpenCode store -------------------------------------------------------
#
# One SQLite file holds everything: ``project`` → ``session_v2`` →
# ``session_message`` (opencode 2.x; ``type`` + ``seq`` + JSON ``data``),
# or the 1.x generation ``session`` → ``message`` → ``part`` when
# ``session_v2`` is absent. The file is WAL-mode and may be written by a
# running opencode, so every query opens a short read-only connection
# and a locked database simply yields nothing until the next call.
#
# Keys: ``project_key`` = project id, ``session_key`` = session id,
# ``record_key`` = dense ordinal over the expanded records (the store
# maps it to ``(message_id, element index)``), ``child_key`` = child
# session id.

import atexit
import pathlib

try:
    import sqlite3
except ImportError:
    sqlite3 = None

_DB_OVERRIDE = None     # --db PATH


def _data_dir():
    base = os.environ.get('XDG_DATA_HOME') or os.path.join(
        os.environ.get('HOME', os.path.expanduser('~')), '.local', 'share')
    return os.path.join(base, 'opencode')


def _db_path():
    """``--db``, else ``$OPENCODE_DB``, else the newest ``opencode*.db``
    in the data dir (``opencode.db`` when none exists yet)."""
    if _DB_OVERRIDE:
        return _DB_OVERRIDE
    if os.environ.get('OPENCODE_DB'):
        return os.environ['OPENCODE_DB']
    d = _data_dir()
    best, best_m = os.path.join(d, 'opencode.db'), -1.0
    try:
        names = os.listdir(d)
    except OSError:
        return best
    for n in names:
        if n == 'opencode.db' or (n.startswith('opencode-') and n.endswith('.db')):
            m = _mtime(os.path.join(d, n))
            if m > best_m:
                best, best_m = os.path.join(d, n), m
    return best


def _mtime(db):
    """Newest of the DB file and its WAL (the main file lags in WAL mode)."""
    out = -1.0
    for p in (db, db + '-wal'):
        try:
            out = max(out, os.stat(p).st_mtime)
        except OSError:
            pass
    return out


def _query(sql, params=()):
    """Rows of one read-only query; ``[]`` when the DB is missing, locked
    or lacks the table."""
    path = _db_path()
    if sqlite3 is None or not os.path.isfile(path):
        return []
    try:
        uri = pathlib.Path(path).resolve().as_uri() + '?mode=ro'
        con = sqlite3.connect(uri, uri=True, timeout=0.5)
    except (sqlite3.Error, ValueError):
        return []
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _json(text):
    try:
        v = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _model_name(model):
    """``provider/model`` from a JSON or dict model field."""
    m = _json(model) if isinstance(model, str) else (model or {})
    mid = m.get('id') or m.get('modelID') or ''
    prov = m.get('providerID') or ''
    return '/'.join(p for p in (prov, mid) if p) or None


class _Session:
    __slots__ = ('id', 'project', 'parent', 'directory', 'title', 'version',
                 'agent', 'model', 'created', 'updated', 'cost', 'tokens',
                 'count', 'children')

    def __init__(self, id, project, parent, directory, title, version, agent,
                 model, created, updated, cost, tokens_in, tokens_out):
        self.id = id
        self.project = project
        self.parent = parent or None
        self.directory = directory or None
        self.title = title or None
        self.version = version or None
        self.agent = agent or None
        self.model = _model_name(model)
        self.created = (created or 0) / 1000
        self.updated = (updated or 0) / 1000
        self.cost = cost or 0
        self.tokens = (tokens_in or 0) + (tokens_out or 0)
        self.count = 0
        self.children = []


_SESSION_COLS = ('id, project_id, parent_id, directory, title, version, agent, '
                 'model, time_created, time_updated, cost, tokens_input, '
                 'tokens_output')

# Built lazily; dropped by ``store_reset`` / ``--db``, rebuilt on each root
# listing. ``gen`` is ``'v2'``, ``'v1'`` or ``None`` (no readable store).
_INDEX = None


def _build_index():
    tables = {r[0] for r in _query("SELECT name FROM sqlite_master WHERE type='table'")}
    gen = 'v2' if 'session_v2' in tables else 'v1' if 'session' in tables else None
    projects = {}
    for pid, worktree, vcs, name in _query('SELECT id, worktree, vcs, name FROM project'):
        projects[pid] = {'id': pid, 'worktree': worktree or '/', 'vcs': vcs, 'name': name}
    sessions = {}
    if gen == 'v2':
        rows = _query(f'SELECT {_SESSION_COLS} FROM session_v2')
        counts = _query('SELECT session_id, COUNT(*), MAX(time_updated) '
                        'FROM session_message GROUP BY session_id')
    elif gen == 'v1':
        rows = _query(f'SELECT {_SESSION_COLS} FROM session')
        counts = _query('SELECT session_id, COUNT(*), MAX(time_updated) '
                        'FROM message GROUP BY session_id')
    else:
        rows, counts = [], []
    for row in rows:
        s = _Session(*row)
        sessions[s.id] = s
    for sid, n, updated in counts:
        s = sessions.get(sid)
        if s is not None:
            s.count = n
            s.updated = max(s.updated, (updated or 0) / 1000)
    for s in sessions.values():
        if s.parent in sessions and s.parent != s.id:
            sessions[s.parent].children.append(s.id)
        else:
            s.parent = None
    for s in sessions.values():
        s.children.sort(key=lambda c: -sessions[c].updated)
    return {'gen': gen, 'projects': projects, 'sessions': sessions}


def _index(rebuild=False):
    global _INDEX
    if _INDEX is None or rebuild:
        _INDEX = _build_index()
    return _INDEX


def _session(session_key):
    return _index()['sessions'].get(session_key)


def _project_dir(pid):
    p = _index()['projects'].get(pid)
    return p['worktree'] if p else None


# ---- listings ------------------------------------------------------------


def store_list_projects():
    idx = _index(rebuild=True)
    latest, counts = {}, {}
    for s in idx['sessions'].values():
        if s.parent is None:
            counts[s.project] = counts.get(s.project, 0) + 1
            latest[s.project] = max(latest.get(s.project, 0), s.updated)
    out = []
    for pid, n in counts.items():      # projects without sessions are skipped
        p = idx['projects'].get(pid) or {'worktree': pid, 'name': None}
        title = _collapse_home(p['worktree'])
        if p.get('name'):
            title += f' ({p["name"]})'
        item = Item(
            id=('project', pid),
            title=title,
            tag=f'{n} sess · {_human_time(latest.get(pid))}',
            tag_style='dim',
            has_children=True,
        )
        item.kind = 'project'
        item.session_count = n
        item.mtime = latest.get(pid, 0)
        out.append(item)
    out.sort(key=lambda it: -it.mtime)
    return out


def _session_item(s):
    bits = [b for b in (s.agent, s.model) if b]
    bits.append(f'{s.count} msg')
    if s.children:
        bits.append(f'{len(s.children)} sub')
    bits.append(_human_time(s.updated))
    title = _trim(_oneline(_clean_title(s.title)), 80) or s.id
    item = Item(
        id=('session', s.id),
        title=title,
        scope_title=f'{s.id}  {title}',
        tag=' · '.join(bits),
        tag_style='dim',
        has_children=s.count > 0 or bool(s.children),
        boundary=True,
    )
    item.kind = 'session'
    item.line_count = s.count
    item.subagent_count = len(s.children)
    item.mtime = s.updated
    return item


def store_list_sessions(project_key):
    out = [_session_item(s) for s in _index()['sessions'].values()
           if s.project == project_key and s.parent is None]
    out.sort(key=lambda it: -it.mtime)
    return out


def store_session_item(session_key):
    s = _session(session_key)
    return _session_item(s) if s is not None else None


def _child_item(parent_key, s):
    kind = s.agent or 'session'
    item = Item(
        id=('agent', parent_key, s.id),
        title=_link_marker(('agent', parent_key, s.id))
              + (_trim(_oneline(_clean_title(s.title)), 80) or s.id),
        tag=f'{kind} · {s.count} msg · {_human_time(s.updated)}',
        tag_style='magenta',
        has_children=s.count > 0,
        boundary=True,
    )
    item.kind = 'subagent'
    item.agent_id = s.id
    item.agent_type = kind
    item.session_path = parent_key
    item.line_count = s.count
    item.mtime = s.updated
    item.row_bg = _ROW_BG_SPAWN
    return item


def store_child_sessions(session_key):
    s = _session(session_key)
    if s is None:
        return []
    sessions = _index()['sessions']
    return [_child_item(session_key, sessions[c]) for c in s.children]


def store_mark_orphan(item):
    item.tag = (f'{item.agent_type} · {item.line_count} msg · orphan · '
                f'{_human_time(item.mtime)}')


def store_child_session(session_key, child_key):
    c = _session(child_key)
    return child_key if c is not None and c.parent == session_key else None


def store_parent_of_child(session_key):
    s = _session(session_key)
    if s is None or s.parent is None:
        return None
    return s.parent, s.id


def store_resolve_spawned_child(session_key, call, result):
    """Join a canon ``spawn`` (``task``) call to the child session whose id
    the result (output or metadata) or the call input mentions."""
    s = _session(session_key)
    if s is None or not s.children:
        return None
    hay = ' '.join(json.dumps(x, default=str) for x in (
        [t['output'] for t in result.tools]
        + [t.get('metadata') for t in result.tools] + [call['input']]) if x)
    for cid in s.children:
        if cid in hay:
            return cid, cid
    return None


# ---- store contract -------------------------------------------------------


_HELP_USAGE_TMPL = """\
Usage: browse-agents-opencode [OPTIONS] [PROJECT]
  -n NUMBER       Cap records enumerated per session (default: {default}).
  --db PATH       Read this OpenCode SQLite file instead of the newest
                  opencode*.db under $XDG_DATA_HOME/opencode.
  --session ID    Open that session directly (a unique id prefix works).
  --item N        After --session, position the cursor on record N (1-based).
  --tree          Show sessions as a tree, grouped by turn (default).
  --no-tree       Show sessions as a flat list.
  --detail LEVEL  Detail level (default: 1). LEVEL is 1-7 or a word:
                  1=summary (user prompts + final replies), 2=voice (all
                  speech + reasoning), 3=edits (+ edit/write/patch),
                  4=tools (+ all tool calls, step ends), 5=detailed
                  (+ system text, selections, retries), 6=all (every known
                  record kind), 7=unknown (+ unrecognised record kinds).
                  Change it live with the '1'-'7' keys.
  PROJECT         A project worktree path (or a session id) to drill into
                  on launch. Ignored when --session is given.\
"""


_HELP_INTRO = """\
browse-agents-opencode — OpenCode project / session / record browser.

Reads the OpenCode SQLite store read-only: projects -> sessions ->
records (or child sessions) -> child records. Drill into a session with
Right/Alt-Down; the preview shows the rendered turn.

  v / V   page the preview (v) / the session dump (V) via $PAGER.
  e / E   open the preview (e) / the session dump (E) in $EDITOR.
  m       toggle inline markdown coloring of voice content (only bound
          when the md2ansi_lib Python module is importable).
  M       render the preview via $MDCAT / mdcat (external).
  J / K   jump cursor to next / previous user-or-assistant voice.
  t       toggle flat / tree view.
  1-7     detail level: 1 summary, 2 voice, 3 edits, 4 tools,
          5 detailed, 6 all, 7 unknown (adds unrecognised kinds).
  Enter   follow a row's [↗] cross-link to a child session.

Right-click a row (or the backslash / F1 keys) for a menu: record and
turn / tool / span umbrella rows offer the source actions above (the
source is a JSON dump of the message rows); session rows show their id
and project. Every row also surfaces a directory cluster over its
project / cwd dirs — open in browse-fs, run a shell there, browse the
git history, or browse its .PLAN.md.\
"""


def store_help_usage():
    return _HELP_USAGE_TMPL.format(default=_MESSAGE_LIMIT)


def store_help_intro():
    return _HELP_INTRO


def store_name():
    return 'opencode'


def store_init():
    pass


def store_reset():
    global _INDEX
    _INDEX = None
    _TAIL.clear()


def _find_session(prefix):
    sessions = _index()['sessions']
    if prefix in sessions:
        return prefix
    hits = [sid for sid in sessions if sid.startswith(prefix)]
    return hits[0] if len(hits) == 1 else None


def store_pop_args():
    """Pop ``--db`` / ``--session``; ``{'target', 'error'}``."""
    global _DB_OVERRIDE, _INDEX
    db_arg = _pop_value('--db')
    session_arg = _pop_value('--session')
    if db_arg is False or session_arg is False:
        return {'target': None, 'error': 'invalid argument value'}
    if db_arg:
        db_arg = os.path.abspath(db_arg)
        if not os.path.isfile(db_arg):
            return {'target': None, 'error': f'no such file: {db_arg}'}
        _DB_OVERRIDE = db_arg
        _INDEX = None
    target = None
    if session_arg:
        target = _find_session(session_arg)
        if not target:
            return {'target': None,
                    'error': f'no session with id {session_arg!r}'}
    return {'target': target, 'error': None}


def store_resolve_positional(arg):
    if _session(arg) is not None:
        return ('session', arg)
    arg_abs = os.path.abspath(arg)
    for pid, p in _index()['projects'].items():
        if p['worktree'] == arg_abs:
            return ('project', pid)
    return ('project', arg_abs)


def store_session_exists(session_key):
    return isinstance(session_key, str) and _session(session_key) is not None


def store_project_path(project_key):
    return _project_dir(project_key) or project_key


def store_project_of_session(session_key):
    s = _session(session_key)
    return s.project if s is not None else '/'


def store_session_info(session_key):
    s = _session(session_key)
    meta = []
    if s is not None:
        meta.append(('session', s.id))
        if s.parent:
            meta.append(('parent', s.parent))
        if s.title:
            meta.append(('title', _trim(_oneline(_clean_title(s.title)), 120)))
        for key in ('directory', 'agent', 'model', 'version'):
            if getattr(s, key):
                meta.append((key, getattr(s, key)))
        if s.cost:
            meta.append(('cost', f'{s.cost:.4f}'))
        if s.tokens:
            meta.append(('tokens', f'{s.tokens:,}'))
    return {'path': _db_path(), 'label': session_key,
            'is_child': s is not None and s.parent is not None,
            'meta': meta, 'mtime': s.updated if s is not None else None}


def store_session_dirs(session_key):
    s = _session(session_key)
    cwd = s.directory if s is not None else None
    return cwd, cwd or (_project_dir(s.project) if s is not None else None) or '/'


def store_context_dirs(project_key, session_key=None):
    out = [('project', _project_dir(project_key) or project_key)]
    if session_key is not None:
        s = _session(session_key)
        if s is not None and s.directory:
            out.append(('cwd', s.directory))
    return out


# ---- source dumps ---------------------------------------------------------
#
# The transcript lives in DB rows, so the source actions get pretty JSON
# written to temp files: a whole-session dump per ``store_source_path``
# (kept for the process, refreshed when the session grew) and the
# excerpt of selected records (caller unlinks).

_DUMP_DIR = None
_DUMPS = {}     # session_key → (watermark, path)


def _dump_dir():
    global _DUMP_DIR
    if _DUMP_DIR is None:
        _DUMP_DIR = tempfile.mkdtemp(prefix='browse-agents-opencode-')
        atexit.register(shutil.rmtree, _DUMP_DIR, True)
    return _DUMP_DIR


def _dump_json(obj, path):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, default=str)
        f.write('\n')


def store_source_path(session_key):
    """A JSON dump of the session's message rows (v1: messages with
    their parts), refreshed when the session grew."""
    wm = _watermark(session_key)
    cached = _DUMPS.get(session_key)
    if cached is not None and cached[0] == wm and os.path.isfile(cached[1]):
        return cached[1]
    path = os.path.join(_dump_dir(), f'{session_key}.json')
    _dump_json([row['raw'] for row in _session_rows(session_key)], path)
    _DUMPS[session_key] = (wm, path)
    return path


def store_source_locator(session_key, record_key):
    return _db_path(), None


def store_source_excerpt(per_session):
    """The raw objects of the selected records, one JSON document per
    record, to a temp file; ``None`` when nothing could be extracted."""
    docs = []
    for sid, keys in per_session.items():
        state = _TAIL.get(sid)
        if state is None:
            continue
        docs.extend(state.raws[n] for n in keys if 0 <= n < len(state.raws))
    if not docs:
        return None
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    with open(path, 'w', encoding='utf-8') as f:
        for d in docs:
            json.dump(d, f, indent=2, ensure_ascii=False, default=str)
            f.write('\n')
    return path
