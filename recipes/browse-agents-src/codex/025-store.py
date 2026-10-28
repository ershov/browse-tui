# ---- Codex store ----------------------------------------------------------
#
# Rollouts live at ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``
# (``$CODEX_HOME`` defaults to ``~/.codex``). ``state_5.sqlite`` indexes
# them (``threads``: id, rollout_path, cwd, title, model, git_branch, …;
# ``thread_spawn_edges``: parent → child; ``name`` is the short thread
# name, ``title`` the first prompt). The index below is built from
# the DB when it opens, then topped up with rollouts found on disk that
# the DB does not list (or every rollout when the DB or the ``sqlite3``
# module is missing): their facts come from line 1 (``session_meta``) and
# the first user prompt.
#
# Keys: ``project_key`` = cwd, ``session_key`` = rollout path,
# ``record_key`` = 0-based line ordinal, ``child_key`` = child thread id.

import pathlib

try:
    import sqlite3
except ImportError:   # a Python built without the sqlite3 module
    sqlite3 = None


def _codex_home():
    return (os.environ.get('CODEX_HOME')
            or os.path.join(os.environ.get('HOME', os.path.expanduser('~')),
                            '.codex'))


def _sessions_dir():
    return os.path.join(_codex_home(), 'sessions')


def _state_db_path():
    return os.path.join(_codex_home(), 'state_5.sqlite')


class _Thread:
    """One indexed thread (rollout file)."""

    __slots__ = ('id', 'path', 'cwd', 'name', 'title', 'model', 'branch',
                 'source', 'version', 'parent', 'children', 'child_status',
                 'mtime')

    def __init__(self, id, path, cwd=None, title=None, model=None,
                 branch=None, source=None, version=None, name=None):
        self.id = id
        self.path = path
        self.cwd = cwd or None
        self.name = name or None          # the short thread name, if set
        self.title = title or None        # the first prompt
        self.model = model or None
        self.branch = branch or None
        self.source = source or None
        self.version = version or None
        self.parent = None        # parent thread id (spawned threads)
        self.children = []        # child thread ids, in edge order
        self.child_status = None  # the spawn edge status for a child
        try:
            self.mtime = os.stat(path).st_mtime
        except OSError:
            self.mtime = 0.0


# Built lazily by ``_index``; dropped by ``store_reset`` and rebuilt on
# every root listing so new rollouts show up on refresh.
_INDEX = None   # {'threads': {id: _Thread}, 'by_path': {path: _Thread}}


def _db_rows():
    """``(threads, edges)`` from ``state_5.sqlite``, or ``([], [])``."""
    if sqlite3 is None or not os.path.isfile(_state_db_path()):
        return [], []
    try:
        uri = pathlib.Path(_state_db_path()).resolve().as_uri() + '?mode=ro'
        con = sqlite3.connect(uri, uri=True)
    except (sqlite3.Error, ValueError):
        return [], []
    try:
        threads = con.execute(
            'SELECT id, rollout_path, cwd, title, model, git_branch, source, '
            'cli_version, name FROM threads').fetchall()
        edges = con.execute(
            'SELECT parent_thread_id, child_thread_id, status '
            'FROM thread_spawn_edges').fetchall()
    except sqlite3.Error:
        return [], []
    finally:
        con.close()
    return threads, edges


def _first_record(path):
    """The parsed first line of a rollout (``session_meta``), or ``{}``."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            rec = json.loads(f.readline() or 'null')
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


_HEAD_PROMPT_LINES = 64


def _head_prompt(path):
    """The first user prompt within the head of a rollout, or ``None``;
    the title fallback for a thread the DB does not know."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for _ in range(_HEAD_PROMPT_LINES):
                line = f.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get('type') != 'event_msg':
                    continue
                p = rec.get('payload') or {}
                if p.get('type') == 'user_message':
                    return p.get('message') or None
                if p.get('type') == 'item_completed':
                    item = p.get('item') or {}
                    if item.get('type') == 'UserMessage':
                        return _text_of(item.get('content')) or None
    except OSError:
        pass
    return None


def _thread_from_file(path):
    """A ``_Thread`` built from the rollout head alone."""
    meta = _first_record(path)
    p = meta.get('payload') if meta.get('type') == 'session_meta' else None
    p = p if isinstance(p, dict) else {}
    tid = p.get('id') or p.get('session_id') or os.path.basename(path)[:-6]
    git = p.get('git') if isinstance(p.get('git'), dict) else {}
    return _Thread(tid, path, cwd=p.get('cwd'), title=_head_prompt(path),
                   branch=git.get('branch'), source=p.get('source'),
                   version=p.get('cli_version'))


def _scan_rollouts():
    """Every ``rollout-*.jsonl`` under the sessions dir."""
    out = []
    for root, _dirs, files in os.walk(_sessions_dir()):
        for name in files:
            if name.startswith('rollout-') and name.endswith('.jsonl'):
                out.append(os.path.join(root, name))
    return out


def _build_index():
    threads, by_path = {}, {}
    rows, edges = _db_rows()
    for tid, path, cwd, title, model, branch, source, version, name in rows:
        if not os.path.isfile(path):
            continue
        th = _Thread(tid, path, cwd=cwd, title=title, model=model,
                     branch=branch, source=source, version=version, name=name)
        threads[tid] = th
        by_path[path] = th
    for path in _scan_rollouts():
        if path in by_path:
            continue
        th = _thread_from_file(path)
        if th.id in threads:
            continue
        threads[th.id] = th
        by_path[path] = th
    for parent, child, status in edges:
        p, c = threads.get(parent), threads.get(child)
        if p is None or c is None or c is p:
            continue
        c.parent = parent
        c.child_status = status
        p.children.append(child)
    return {'threads': threads, 'by_path': by_path}


def _index(rebuild=False):
    global _INDEX
    if _INDEX is None or rebuild:
        _INDEX = _build_index()
    return _INDEX


def _thread_for(path):
    """The indexed thread for a rollout path; an arbitrary ``--file`` /
    positional rollout outside the index is read on demand."""
    idx = _index()
    th = idx['by_path'].get(path)
    if th is None and os.path.isfile(path):
        th = _thread_from_file(path)
        idx['by_path'][path] = th
        idx['threads'].setdefault(th.id, th)
    return th


def _thread_title(th):
    """Display title: the thread name, else the first prompt."""
    return _clean_title(th.name or th.title) if th is not None else ''


def _line_count(path):
    try:
        with open(path, 'rb') as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


# ---- listings ------------------------------------------------------------


def store_list_projects():
    groups = {}
    for th in _index(rebuild=True)['threads'].values():
        if th.parent is not None:
            continue
        groups.setdefault(th.cwd or '/', []).append(th)
    out = []
    for cwd, ths in groups.items():
        latest = max(t.mtime for t in ths)
        item = Item(
            id=('project', cwd),
            title=_collapse_home(cwd),
            tag=f'{len(ths)} thread · {_human_time(latest)}',
            tag_style='dim',
            has_children=True,
        )
        item.kind = 'project'
        item.session_count = len(ths)
        item.mtime = latest
        out.append(item)
    out.sort(key=lambda it: -it.mtime)
    return out


def _session_item(th):
    count = _line_count(th.path)
    n_subs = len(th.children)
    bits = [th.model] if th.model else []
    bits.append(f'{count} rec')
    if n_subs:
        bits.append(f'{n_subs} sub')
    bits.append(_human_time(th.mtime))
    item = Item(
        id=('session', th.path),
        title=_trim(_oneline(_thread_title(th)), 80) or th.id,
        scope_title=th.path,
        tag=' · '.join(bits),
        tag_style='dim',
        has_children=count > 0 or n_subs > 0,
        boundary=True,
    )
    item.kind = 'session'
    item.thread_id = th.id
    item.line_count = count
    item.subagent_count = n_subs
    item.mtime = th.mtime
    return item


def store_list_sessions(project_key):
    out = [_session_item(th) for th in _index()['threads'].values()
           if (th.cwd or '/') == project_key and th.parent is None]
    out.sort(key=lambda it: -it.mtime)
    return out


def store_session_item(session_key):
    th = _thread_for(session_key)
    return _session_item(th) if th is not None else None


def _child_item(parent_path, th):
    count = _line_count(th.path)
    status = th.child_status or 'thread'
    item = Item(
        id=('agent', parent_path, th.id),
        title=_link_marker(('agent', parent_path, th.id))
              + (_trim(_oneline(_thread_title(th)), 80) or th.id),
        tag=f'{status} · {count} rec · {_human_time(th.mtime)}',
        tag_style='magenta',
        has_children=count > 0,
        boundary=True,
    )
    item.kind = 'subagent'
    item.agent_id = th.id
    item.agent_type = status
    item.session_path = parent_path
    item.subagent_path = th.path
    item.line_count = count
    item.mtime = th.mtime
    item.row_bg = _ROW_BG_SPAWN
    return item


def store_child_sessions(session_key):
    th = _thread_for(session_key)
    if th is None:
        return []
    threads = _index()['threads']
    out = [_child_item(session_key, threads[c]) for c in th.children
           if c in threads]
    out.sort(key=lambda it: -it.mtime)
    return out


def store_mark_orphan(item):
    item.tag = (f'{item.agent_type} · {item.line_count} rec · orphan · '
                f'{_human_time(item.mtime)}')


def store_child_session(session_key, child_key):
    th = _index()['threads'].get(child_key)
    if th is None or th.parent is None:
        return None
    parent = _index()['threads'].get(th.parent)
    if parent is None or parent.path != session_key:
        return None
    return th.path if os.path.isfile(th.path) else None


def store_parent_of_child(session_key):
    th = _thread_for(session_key)
    if th is None or th.parent is None:
        return None
    parent = _index()['threads'].get(th.parent)
    if parent is None or not os.path.isfile(parent.path):
        return None
    return parent.path, th.id


def store_resolve_spawned_child(session_key, call, result):
    """Join a spawn call to the child thread whose id its result (or the
    call input) mentions; ``None`` when no spawned child matches."""
    th = _thread_for(session_key)
    if th is None or not th.children:
        return None
    hay = ' '.join(str(x) for x in (
        [t['output'] for t in result.tools] + [call['input']]) if x)
    threads = _index()['threads']
    for cid in th.children:
        child = threads.get(cid)
        if child is not None and cid in hay and os.path.isfile(child.path):
            return cid, child.path
    return None


# ---- store contract -------------------------------------------------------


_HELP_USAGE_TMPL = """\
Usage: browse-agents-codex [OPTIONS] [PROJECT]
  -n NUMBER       Cap records enumerated per thread (default: {default}).
  --thread ID     Open the rollout of that thread id (a unique prefix works).
  --file PATH     Open an arbitrary rollout .jsonl file directly.
  --item N        After --thread/--file, position the cursor on line N
                  (1-based) of the rollout.
  --tree          Show threads as a tree, grouped by turn (default).
  --no-tree       Show threads as a flat list.
  --detail LEVEL  Detail level (default: 1). LEVEL is 1-7 or a word:
                  1=summary (user prompts + final answers),
                  2=voice (all speech), 3=edits (+ apply_patch),
                  4=tools (+ all commands and tool calls), 5=detailed
                  (+ turn context, settings, injected instructions),
                  6=all (every known record kind, raw duplicates, token
                  counts), 7=unknown (+ unrecognised record kinds).
                  Change it live with the '1'-'7' keys.
  PROJECT         Working directory whose threads to drill into on
                  launch. Ignored when --thread / --file is given.\
"""


_HELP_INTRO = """\
browse-agents-codex — Codex CLI project / thread / record browser.

Walks the rollouts under $CODEX_HOME/sessions (default ~/.codex), grouped
by working directory: projects -> threads -> records (or spawned
subthreads) -> subthread records. Drill into a rollout with
Right/Alt-Down; the preview shows the rendered turn.

  v / V   page the preview (v) / the source .jsonl (V) via $PAGER.
  e / E   open the preview (e) / the source .jsonl (E) in $EDITOR.
  m       toggle inline markdown coloring of voice content (only bound
          when the md2ansi_lib Python module is importable).
  M       render the preview via $MDCAT / mdcat (external).
  J / K   jump cursor to next / previous user-or-assistant voice.
  t       toggle flat / tree view.
  1-7     detail level: 1 summary, 2 voice, 3 edits, 4 tools,
          5 detailed, 6 all, 7 unknown (adds unrecognised kinds).
  Enter   follow a row's [↗] cross-link to a spawned subthread.

Right-click a row (or the backslash / F1 keys) for a menu: record and
turn / tool / span umbrella rows offer the source actions above; thread
rows open the rollout and show their path / id. Every row also surfaces a
directory cluster over its project / cwd dirs — open in browse-fs, run a
shell there, browse the git history, or browse its .PLAN.md.\
"""


def store_help_usage():
    return _HELP_USAGE_TMPL.format(default=_MESSAGE_LIMIT)


def store_help_intro():
    return _HELP_INTRO


def store_name():
    return 'codex'


def store_init():
    pass


def store_reset():
    global _INDEX
    _INDEX = None
    _FILE_INDEX.clear()


def _find_thread(prefix):
    """Rollout path of the thread with id ``prefix`` or a unique id prefix."""
    threads = _index()['threads']
    if prefix in threads:
        return threads[prefix].path
    hits = [th for tid, th in threads.items() if tid.startswith(prefix)]
    return hits[0].path if len(hits) == 1 else None


def store_pop_args():
    """Pop ``--thread`` / ``--file``; ``{'target', 'error'}``."""
    thread_arg = _pop_value('--thread')
    file_arg = _pop_value('--file')
    if thread_arg and file_arg:
        return {'target': None,
                'error': '--thread and --file are mutually exclusive'}
    if thread_arg is False or file_arg is False:
        return {'target': None, 'error': 'invalid argument value'}
    target = None
    if file_arg:
        target = os.path.abspath(file_arg)
        if not os.path.isfile(target):
            return {'target': None, 'error': f'no such file: {file_arg}'}
    elif thread_arg:
        target = _find_thread(thread_arg)
        if not target:
            return {'target': None,
                    'error': f'no thread with id {thread_arg!r}'}
    return {'target': target, 'error': None}


def store_resolve_positional(arg):
    arg_abs = os.path.abspath(arg)
    if arg.endswith('.jsonl') and os.path.isfile(arg_abs):
        return ('session', arg_abs)
    return ('project', arg_abs)


def store_session_exists(session_key):
    return isinstance(session_key, str) and os.path.isfile(session_key)


def store_project_path(project_key):
    return project_key


def store_project_of_session(session_key):
    th = _thread_for(session_key)
    return (th.cwd if th is not None and th.cwd else None) or '/'


def store_source_path(session_key):
    return session_key


def store_source_locator(session_key, record_key):
    return session_key, record_key + 1


def store_session_info(session_key):
    th = _thread_for(session_key)
    meta = []
    if th is not None:
        meta.append(('thread', th.id))
        if th.parent:
            meta.append(('parent', th.parent))
        for key in ('name', 'title', 'cwd', 'model', 'branch', 'source',
                    'version'):
            v = getattr(th, key)
            if v:
                meta.append((key, _trim(_oneline(_clean_title(v)), 120)
                             if key in ('name', 'title') else v))
    try:
        mtime = os.stat(session_key).st_mtime
    except OSError:
        mtime = None
    return {'path': session_key,
            'label': th.id if th is not None else os.path.basename(session_key),
            'is_child': th is not None and th.parent is not None,
            'meta': meta, 'mtime': mtime}


def store_session_dirs(session_key):
    th = _thread_for(session_key)
    cwd = th.cwd if th is not None else None
    return cwd, cwd or '/'


def store_context_dirs(project_key, session_key=None):
    out = [('project', project_key)]
    if session_key is not None:
        th = _thread_for(session_key)
        if th is not None and th.cwd:
            out.append(('cwd', th.cwd))
    return out
