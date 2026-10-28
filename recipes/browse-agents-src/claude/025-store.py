CLAUDE_ROOT = os.path.join(
    os.environ.get('HOME', os.path.expanduser('~')), '.claude', 'projects',
)


def _decode_project_path(name):
    """Convert a Claude-encoded directory name back to a real path.

    Claude Code names a project directory by replacing every ``/`` in
    the original cwd with ``-``, so ``/home/ubuntu/sandvault/src`` becomes
    ``-home-ubuntu-sandvault-src``. We invert that for the display title
    and collapse the user's HOME prefix to ``~`` so the column stays
    readable (``~/sandvault/src`` rather than ``/home/ubuntu/sandvault/src``).

    Names that don't start with ``-`` are returned verbatim — better to
    show *something* than to crash on an unexpected layout.
    """
    if not name.startswith('-'):
        return name
    p = '/' + name[1:].replace('-', '/')
    home = os.environ.get('HOME') or os.path.expanduser('~')
    if home and p == home:
        return '~'
    if home and p.startswith(home + '/'):
        return '~' + p[len(home):]
    return p


# Populated by ``_scan_running_sessions`` at startup; indexed by
# sessionId → {pid, cwd, status, …} read from ``~/.claude/sessions/*.json``.
_RUNNING_INDEX = {}
_RUNNING_ONLY = False   # --running flag — filter to live sessions only.


def _claude_sessions_dir():
    """Sibling of CLAUDE_ROOT that holds per-PID live-session metadata."""
    return os.path.join(
        os.environ.get('HOME', os.path.expanduser('~')),
        '.claude', 'sessions',
    )


def _scan_running_sessions():
    """Refresh ``_RUNNING_INDEX`` from on-disk session metadata.

    Each running ``claude`` process writes ``~/.claude/sessions/<pid>.json``
    with at least ``{sessionId, pid, cwd, status}``. We keep only those
    whose pid is currently alive — stale files from crashed processes
    silently disappear.
    """
    _RUNNING_INDEX.clear()
    sdir = _claude_sessions_dir()
    if not os.path.isdir(sdir):
        return
    try:
        names = os.listdir(sdir)
    except OSError:
        return
    for name in names:
        if not name.endswith('.json'):
            continue
        path = os.path.join(sdir, name)
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        pid = data.get('pid')
        sid = data.get('sessionId')
        if not isinstance(pid, int) or not isinstance(sid, str):
            continue
        if not _pid_alive(pid):
            continue
        _RUNNING_INDEX[sid] = data


def _find_session_by_id(sid):
    """Return the absolute .jsonl path for ``sid``, or ``None``.

    Linear scan across project dirs — there's no central index. Each
    project directory contains its own ``<sid>.jsonl`` files; the same
    session id never appears in two project dirs simultaneously, so the
    first hit wins.
    """
    if not isinstance(sid, str) or not sid:
        return None
    if not os.path.isdir(CLAUDE_ROOT):
        return None
    target = f'{sid}.jsonl'
    try:
        names = os.listdir(CLAUDE_ROOT)
    except OSError:
        return None
    for project in names:
        proj_dir = os.path.join(CLAUDE_ROOT, project)
        candidate = os.path.join(proj_dir, target)
        if os.path.isfile(candidate):
            return candidate
    return None


def _find_session_by_pid(pid):
    """Return the .jsonl path for the live session owned by ``pid``."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    path = os.path.join(_claude_sessions_dir(), f'{pid}.json')
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _find_session_by_id(data.get('sessionId'))


# ---- agent teams -----------------------------------------------------------
#
# A session that spawns *named* agents via the Agent tool (team workers)
# gets a team: ``~/.claude/teams/<teamName>/config.json`` holds the roster
# — ``leadSessionId`` plus one member entry per agent ({name, agentType,
# isActive, …}). On the legacy backend each worker runs as its own plain
# session .jsonl in the same project dir; the only linkage on its records
# is the ``teamName`` / ``agentName`` stamped a few lines into the file.
# Workers never write ``~/.claude/sessions/<pid>.json``, so team
# membership is also the only liveness signal for them. The in-process
# backend instead stores worker transcripts under the lead session's own
# ``<sid>/subagents/agent-a<name>-<hex>.jsonl`` with an adjacent meta —
# see ``_find_inprocess_teammate``.

_TEAMS_INDEX = {}   # teamName → {'lead': leadSessionId, 'members': {name: m}}


def _claude_teams_dir():
    """Sibling of CLAUDE_ROOT that holds per-team rosters."""
    return os.path.join(
        os.environ.get('HOME', os.path.expanduser('~')),
        '.claude', 'teams',
    )


def _scan_teams():
    """Refresh ``_TEAMS_INDEX`` from ``~/.claude/teams/*/config.json``."""
    _TEAMS_INDEX.clear()
    tdir = _claude_teams_dir()
    if not os.path.isdir(tdir):
        return
    try:
        names = os.listdir(tdir)
    except OSError:
        return
    for name in names:
        cfg_path = os.path.join(tdir, name, 'config.json')
        try:
            with open(cfg_path, 'r', encoding='utf-8', errors='replace') as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(cfg, dict):
            continue
        members = {}
        for m in cfg.get('members') or []:
            if isinstance(m, dict) and m.get('name'):
                members[m['name']] = m
        _TEAMS_INDEX[cfg.get('name') or name] = {
            'lead': cfg.get('leadSessionId') or '',
            'members': members,
        }


# Byte-regex head peek for the team fields — no JSON parse, mirrors
# ``_session_cwds``. The fields appear within the first few records, so
# one bounded read is enough.
_TEAM_NAME_RE = re.compile(rb'"teamName"\s*:\s*"([^"]*)"')
_AGENT_NAME_RE = re.compile(rb'"agentName"\s*:\s*"([^"]*)"')
_WORKER_HEAD_BYTES = 65536
_WORKER_META_CACHE = {}  # path → ((size, mtime), (team, agent) | None)


def _worker_meta(session_jsonl_path):
    """``(teamName, agentName)`` stamped in the transcript head, or None.

    A positive hit is immutable (the head never changes once written);
    a negative one is re-checked whenever the file's size/mtime moves
    (a freshly-created worker file grows its head in the first ticks).
    """
    try:
        st = os.stat(session_jsonl_path)
    except OSError:
        return None
    key = (st.st_size, st.st_mtime)
    cached = _WORKER_META_CACHE.get(session_jsonl_path)
    if cached is not None and (cached[1] is not None or cached[0] == key):
        return cached[1]
    meta = None
    try:
        with open(session_jsonl_path, 'rb') as f:
            head = f.read(_WORKER_HEAD_BYTES)
    except OSError:
        head = b''
    tm = _TEAM_NAME_RE.search(head)
    am = _AGENT_NAME_RE.search(head)
    if tm and am:
        try:
            meta = (tm.group(1).decode('utf-8'),
                    am.group(1).decode('utf-8'))
        except UnicodeDecodeError:
            meta = None
    _WORKER_META_CACHE[session_jsonl_path] = (key, meta)
    return meta


def _worker_lead_jsonl(session_jsonl_path):
    """For a team-worker transcript, its lead session's sibling .jsonl.

    Lead resolution: the team's ``leadSessionId`` from the roster when
    present, else the ``session-<8-hex>`` naming convention the
    auto-generated team names carry (prefix-match against the sibling
    session files). Returns None when this session isn't a worker, IS
    its own team's lead, or the lead's .jsonl isn't in the same project
    dir.
    """
    meta = _worker_meta(session_jsonl_path)
    if not meta:
        return None
    team, _agent = meta
    own_sid = os.path.basename(session_jsonl_path)[:-len('.jsonl')]
    proj_dir = os.path.dirname(session_jsonl_path)
    entry = _TEAMS_INDEX.get(team)
    lead = (entry or {}).get('lead') or ''
    if not lead and team.startswith('session-'):
        prefix = team[len('session-'):]
        if prefix:
            try:
                names = os.listdir(proj_dir)
            except OSError:
                names = []
            for n in sorted(names):
                if (n.endswith('.jsonl') and n.startswith(prefix)
                        and n[:-len('.jsonl')] != own_sid):
                    lead = n[:-len('.jsonl')]
                    break
    if not lead or lead == own_sid:
        return None
    p = os.path.join(proj_dir, lead + '.jsonl')
    return p if os.path.isfile(p) else None


def _worker_running_member(session_jsonl_path):
    """The live roster entry for a team-worker transcript, or None.

    A worker counts as live iff its roster entry says ``isActive`` AND
    the team's lead session process is alive — roster files linger
    after teardown/crash, so the leader's own liveness is the
    staleness gate.
    """
    meta = _worker_meta(session_jsonl_path)
    if not meta:
        return None
    team, agent = meta
    entry = _TEAMS_INDEX.get(team)
    if not entry:
        return None
    member = entry['members'].get(agent)
    if not (isinstance(member, dict) and member.get('isActive')):
        return None
    if entry['lead'] not in _RUNNING_INDEX:
        return None
    return member


def _team_name_of_lead(lead_sid):
    """The team a lead session owns: roster first, then the
    ``session-<8-hex>`` naming convention auto-created teams use."""
    for team, entry in _TEAMS_INDEX.items():
        if entry.get('lead') == lead_sid:
            return team
    return f'session-{lead_sid[:8]}'


# One worker scan per project dir, not per session: listing a dir of N
# sessions calls ``_team_workers_for_session`` (via ``_count_subagents``)
# for every row, and an uncached scan would stat every sibling each time
# — O(N²) for dirs that accumulate hundreds of sessions. Keyed by the
# dir's mtime: it moves on every file create/delete/rename (the events
# that change the worker set) though not on appends — a worker whose
# head records land in a later tick than its file-create is picked up
# by the next create/delete in the dir or a root refresh.
_DIR_WORKERS_CACHE = {}   # proj_dir → (dir_mtime, {team: [(name, mtime, path)]})


def _team_workers_map(proj_dir):
    """``{teamName: [(agent_name, mtime, worker_jsonl)]}`` for one dir."""
    try:
        dir_mtime = os.stat(proj_dir).st_mtime
    except OSError:
        return {}
    cached = _DIR_WORKERS_CACHE.get(proj_dir)
    if cached is not None and cached[0] == dir_mtime:
        return cached[1]
    mapping = {}
    try:
        names = os.listdir(proj_dir)
    except OSError:
        names = []
    for n in names:
        if not n.endswith('.jsonl'):
            continue
        p = os.path.join(proj_dir, n)
        meta = _worker_meta(p)
        if not meta:
            continue
        try:
            m = os.stat(p).st_mtime
        except OSError:
            continue
        mapping.setdefault(meta[0], []).append((meta[1], m, p))
    _DIR_WORKERS_CACHE[proj_dir] = (dir_mtime, mapping)
    return mapping


def _team_workers_for_session(session_jsonl_path):
    """``[(agent_name, worker_jsonl)]`` for the team this session leads.

    Reads the per-dir worker scan — the roster carries no session ids,
    so the worker transcripts' head stamps are the only mapping. When a
    name was reused (a respawn under the same name starts a fresh
    transcript) the newest file wins; older ones stay plain top-level
    sessions.
    """
    lead_sid = os.path.basename(session_jsonl_path)[:-len('.jsonl')]
    proj_dir = os.path.dirname(session_jsonl_path)
    team = _team_name_of_lead(lead_sid)
    best = {}   # agent_name → (mtime, path)
    for name, m, p in _team_workers_map(proj_dir).get(team, ()):
        if os.path.basename(p)[:-len('.jsonl')] == lead_sid:
            continue
        prev = best.get(name)
        if prev is None or m > prev[0]:
            best[name] = (m, p)
    return sorted((name, mp[1]) for name, mp in best.items())


def _find_worker_jsonl(session_jsonl_path, worker_name):
    """The newest worker transcript named ``worker_name`` in the team
    led by this session, or None."""
    for name, p in _team_workers_for_session(session_jsonl_path):
        if name == worker_name:
            return p
    return None


# ---- session names ---------------------------------------------------------
#
# A session's display name lives in its own transcript: ``ai-title``
# records (re-generated as the session grows — the LAST one wins) and
# ``custom-title`` (the user's explicit rename, which outranks the AI
# title). Both sit near the END of the file, so the extraction reads
# backwards in growing windows instead of scanning the whole file.

# Cheap line filter for the tail windows — candidate lines are then
# json-parsed and their top-level ``type`` verified, so a title-shaped
# string EMBEDDED in some other record's payload can't false-match.
_TITLE_LINE_RE = re.compile(rb'"type"\s*:\s*"(?:ai-title|custom-title)"')
_NAME_WINDOW = 65536
_SESSION_NAME_CACHE = {}   # path → ((size, mtime), name-or-None)

# Titles render into one-line tags and the aligned scope card — squash
# control chars (newlines, ESC sequences a custom rename could carry).
_CTRL_CHARS_RE = re.compile(r'[\x00-\x1f\x7f]+')


def _clean_title(s):
    """One safe display line out of a recorded title (or '')."""
    return _CTRL_CHARS_RE.sub(' ', s).strip() if s else ''


def _session_name_from_file(path):
    """Last recorded title of a session, reading the file tail only.

    Windows grow 4× until a title record (or the file start) is hit. A
    ``custom-title`` outranks any ``ai-title`` *within the window that
    ends the walk* — a rename older than every window read is
    approximated away (the full-scan tree path has the exact rule); an
    empty ``customTitle`` (rename cleared) falls back to the AI title.
    Cached by (size, mtime) — titles change as the file grows.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_size, st.st_mtime)
    cached = _SESSION_NAME_CACHE.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    name = None
    try:
        with open(path, 'rb') as f:
            win = _NAME_WINDOW
            while True:
                off = max(0, st.st_size - win)
                f.seek(off)
                chunk = f.read()
                if off > 0:
                    # Drop the partial first line so a record split
                    # across the window boundary can't half-parse.
                    nl = chunk.find(b'\n')
                    chunk = chunk[nl + 1:] if nl >= 0 else b''
                custom = ai = None
                for line in chunk.splitlines():
                    if not _TITLE_LINE_RE.search(line):
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    if rec.get('type') == 'custom-title':
                        custom = _clean_title(rec.get('customTitle'))
                    elif rec.get('type') == 'ai-title':
                        ai = _clean_title(rec.get('aiTitle'))
                if custom:
                    name = custom
                    break
                if ai:
                    name = ai
                    break
                if off == 0:
                    break
                win *= 4
    except OSError:
        pass
    _SESSION_NAME_CACHE[path] = (key, name)
    return name


def _session_name_lookup(path):
    """``(known, name)`` for a session, without touching the file.

    ``known`` is False only when the name was never determined (no
    scanned tree, no fresh tail-scan cache entry) — the async filler's
    cue. A scanned/cached session with no title is ``(True, None)`` so
    it isn't re-scanned on every listing.
    """
    td = _TREE_CACHE.get(path)
    if td is not None:
        return True, _clean_title(td.custom_title or td.ai_title) or None
    cached = _SESSION_NAME_CACHE.get(path)
    if cached is not None:
        try:
            st = os.stat(path)
        except OSError:
            return True, None
        if cached[0] == (st.st_size, st.st_mtime):
            return True, cached[1]
    return False, None


def _session_name(path):
    """Best known display name for a session, without touching the file."""
    return _session_name_lookup(path)[1]


# ---- get_children: dispatches by id tag ----------------------------------
#
# The Browser passes the raw item id back into ``get_children``. Every id is a
# tagged tuple whose first element is a short string tag (or ``None`` for the
# root), so routing is a flat ``id[0]`` dispatch with direct field access
# (``id[1]`` = the .jsonl path, ``id[2]`` = a line / agent id). No substring
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
    #   * ``('session', jsonl)``  — bust just that file's _TreeData.
    #   * ``('prompt'|'tool'|'span', jsonl, line)`` — bust the containing
    #     file's _TreeData; one umbrella's children are derived from
    #     indexes in the same file's TreeData so anything narrower is moot.
    #   * ``('agent', jsonl, aid)`` — bust the subagent's file too.
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
            # Re-peek worker transcripts + rosters on an explicit
            # refresh — a worker whose head landed after its file
            # was scanned, or a roster flip, is picked up here.
            _WORKER_META_CACHE.clear()
            _DIR_WORKERS_CACHE.clear()
            _scan_teams()
            # Drop the shared markdown parse cache too, so a root Ctrl-R
            # re-reads any referenced ``.md`` files edited on disk. (A
            # per-file ``_bust_caches_for`` can't reach these — they live
            # outside the session tree — so the full wipe is the right
            # granularity.) No-op when md_doc isn't importable.
            if _md_doc is not None:
                _md_doc.clear_cache()
        return _list_projects()
    if not isinstance(item_id, tuple) or not item_id:
        return []
    tag = item_id[0]
    if tag == 'agent':
        # ``('agent', jsonl, aid)``. Tree mode views the subagent .jsonl as
        # its own tree, recursing into _list_tree_roots. Flat mode lists its
        # lines newest-first.
        session_path, agent_id = item_id[1], item_id[2]
        if _TREE_MODE:
            if not agent_id:
                return []
            agent_path = _resolve_agent_jsonl(session_path, agent_id)
            if not os.path.isfile(agent_path):
                return []
            return _list_tree_roots(agent_path)
        return _list_subagent_messages(item_id)
    if tag == 'prompt':
        # ``('prompt', jsonl, turn_root_line)`` — turn umbrella.
        if _TREE_MODE:
            return _list_prompt_children(item_id[1], item_id[2])
        return []
    if tag == 'tool':
        # ``('tool', jsonl, assistant_line)`` — tool-execution umbrella.
        if _TREE_MODE:
            return _list_tool_children(item_id[1], item_id[2])
        return []
    if tag == 'span':
        # ``('span', jsonl, start)`` — synthetic <system> umbrella in tree
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
        # ``('msg', jsonl, n)`` — a message leaf. When ``md_doc`` is present,
        # a message gains markdown children (its inline document + referenced
        # ``.md`` files) — built lazily here, mode-agnostic. Otherwise it is a
        # leaf in tree mode (umbrellas own all the expansion) and flat mode
        # never had children for these.
        if _md_doc is not None:
            return _md_message_children(item_id)
        return []
    if tag == 'session':
        jsonl_path = item_id[1]
        if not os.path.isfile(jsonl_path):
            return []
        if _TREE_MODE:
            # Tree mode: subagents surface in a top block above the
            # turn/span umbrellas — all handled by the roots listing.
            return _list_tree_roots(jsonl_path)
        return _list_session_children(jsonl_path)
    if tag == 'project':
        project_dir = item_id[1]
        return _list_sessions(project_dir) if os.path.isdir(project_dir) else []
    # Anything else (unknown / synthetic — 'sep', 'trunc', 'err') — empty.
    return []


def _list_projects():
    if not os.path.isdir(CLAUDE_ROOT):
        return []
    out = []
    for name in os.listdir(CLAUDE_ROOT):
        full = os.path.join(CLAUDE_ROOT, name)
        if not os.path.isdir(full):
            continue
        # Project mtime = max(.jsonl mtime) inside it, NOT the dir mtime.
        # Directory mtime only changes on file create/delete/rename — it
        # doesn't update when an existing session is appended to. Using
        # dir mtime would freeze the ordering as soon as a project's
        # session set stabilises, even while the user is actively
        # talking to one of the existing sessions.
        latest = 0.0
        sessions = 0
        running = 0
        try:
            for n in os.listdir(full):
                if not n.endswith('.jsonl'):
                    continue
                sessions += 1
                sid = n[:-len('.jsonl')]
                if sid in _RUNNING_INDEX:
                    running += 1
                try:
                    m = os.stat(os.path.join(full, n)).st_mtime
                except OSError:
                    continue
                if m > latest:
                    latest = m
        except OSError:
            pass
        # --running: hide projects with no live sessions.
        if _RUNNING_ONLY and not running:
            continue
        tag = f'{sessions} sess · {_human_time(latest)}'
        if running:
            tag = f'{running}/{sessions} live · {_human_time(latest)}'
        item = Item(
            id=('project', full),
            title=_real_project_path(full),
            tag=tag,
            tag_style=('green' if running else 'dim'),
            has_children=sessions > 0,
        )
        item.kind = 'project'
        item.encoded_name = name
        item.session_count = sessions
        item.running_count = running
        item.mtime = latest
        out.append(item)
    # Most-recently-active project first — matches what people actually
    # want to look at when opening the browser cold.
    out.sort(key=lambda it: -it.mtime)
    return out


def _session_item(full, st=None):
    """Build the rich session-row Item for the ``.jsonl`` at ``full``.

    Single source of truth for the session-row shape, shared by
    :func:`_list_sessions` (project listing) and the direct-launch
    scope-root seed (:func:`_seed_scope_root_item`, #730). ``st`` is an
    optional pre-fetched ``os.stat`` result; when omitted it's stat'd
    here. Returns ``None`` if the file can't be stat'd or ``--running``
    is active and this session has no live process.
    """
    if st is None:
        try:
            st = os.stat(full)
        except OSError:
            return None
    # Line count is a fast O(file-size) proxy for message count and
    # subsumes the "is this session non-empty?" check we need for
    # has_children. Cheaper than json-parsing every line just to count.
    try:
        with open(full, 'rb') as f:
            count = sum(1 for _ in f)
    except OSError:
        count = 0
    sid = os.path.basename(full)[:-len('.jsonl')]
    running_meta = _RUNNING_INDEX.get(sid)
    # Team workers never write a ``sessions/<pid>.json`` — their
    # liveness comes from the team roster instead.
    worker_meta = _worker_meta(full)
    worker_member = _worker_running_member(full) if worker_meta else None
    # --running: hide sessions whose claude process isn't alive.
    if _RUNNING_ONLY and not running_meta and worker_member is None:
        return None
    n_subs = _count_subagents(full)
    tag = f'{count} msg'
    if n_subs:
        tag += f' · {n_subs} sub'
    tag += f' · {_human_time(st.st_mtime)}'
    if running_meta:
        tag = f'pid {running_meta["pid"]} · ' + tag
    elif worker_meta:
        live = 'live · ' if worker_member is not None else ''
        tag = f'{worker_meta[1]} · {live}' + tag
    # Session display name — when the transcript title is already known
    # (scanned tree or a fresh tail-scan cache hit) it shows right away;
    # otherwise the running metadata's derived name stands in and the
    # async filler upgrades the row to the transcript title later.
    known, name = _session_name_lookup(full)
    display = name
    if display is None and running_meta:
        display = _clean_title(running_meta.get('name')) or None
    if display:
        tag = f'{_trim(display, 48)} · ' + tag
    item = Item(
        id=('session', full),
        title=sid,
        # When this session becomes the current scope, show the
        # full filesystem path on the synthetic scope-header row
        # (the listing-pane title stays short — the session id).
        # Matches the display you get when opening the session
        # directly from the command line.
        scope_title=full,
        tag=tag,
        tag_style=('green' if (running_meta or worker_member is not None)
                   else 'dim'),
        has_children=count > 0 or n_subs > 0,
        # A bare session row anchors a whole other file — a
        # self-contained foreign subtree. ``boundary`` is what the
        # migrated ``_is_cross_file_id`` reads to render the cheap
        # metadata card until the row is committed (scoped/expanded),
        # and what stops a recursive expand from walking the whole
        # transcript in one go.
        boundary=True,
    )
    item.kind = 'session'
    item.line_count = count
    item.subagent_count = n_subs
    item.mtime = st.st_mtime
    item.pid = running_meta['pid'] if running_meta else None
    item.status = running_meta.get('status') if running_meta else None
    item.team_name = worker_meta[0] if worker_meta else None
    item.agent_name = worker_meta[1] if worker_meta else None
    item.worker_live = worker_member is not None
    item.session_name = name or None
    item.session_name_known = known
    return item


def _list_sessions(project_dir):
    out = []
    try:
        names = os.listdir(project_dir)
    except OSError:
        return []
    for name in names:
        if not name.endswith('.jsonl'):
            continue
        full = os.path.join(project_dir, name)
        item = _session_item(full)
        if item is not None:
            out.append(item)
    # Fold team workers under their lead session: a linked worker
    # renders inside the lead's subtree (its rows are keyed by the same
    # ids), so its top-level row must go — double placement would
    # couple expansion state across the two subtrees. Workers whose
    # lead .jsonl isn't in this dir, or older transcripts from a reused
    # name, stay visible as plain sessions.
    linked = set()
    by_lead = {}
    for it in out:
        if getattr(it, 'agent_name', None):
            lead = _worker_lead_jsonl(it.id[1])
            if lead:
                by_lead.setdefault(lead, []).append(it)
    for workers in by_lead.values():
        newest = {}
        for it in workers:
            prev = newest.get(it.agent_name)
            if prev is None or it.mtime > prev.mtime:
                newest[it.agent_name] = it
        linked.update(it.id[1] for it in newest.values())
    if linked:
        out = [it for it in out if it.id[1] not in linked]
    out.sort(key=lambda it: -it.mtime)
    # Names never determined yet get back-filled off-thread: the
    # tail-scan reads real bytes per file, too slow for a big dir on
    # the listing path. (A running row showing the derived stand-in
    # name still upgrades to its transcript title — its name is
    # unknown, so it stays pending.)
    pending = [it.id[1] for it in out
               if not getattr(it, 'session_name_known', True)]
    if pending:
        _spawn_session_name_filler(project_dir, pending)
    return out


# ---- subagent discovery --------------------------------------------------
#
# Claude Code stores each session's subagent transcripts at
# ``<project_dir>/<session_id>/subagents/agent-<agent_id>.jsonl``,
# optionally with an adjacent ``agent-<agent_id>.meta.json`` containing
# ``{agentType, description}``. We surface them as sibling rows of the
# top-level messages so a session list reads as one flat timeline of
# what happened in that session — main-thread messages interleaved
# with the subagent groups it dispatched.


def _subagents_dir(session_jsonl_path):
    """Return the ``<session_id>/subagents`` dir path for a session's .jsonl.

    Doesn't check existence — caller decides what to do if it's absent.
    """
    base, _ = os.path.splitext(session_jsonl_path)
    return os.path.join(base, 'subagents')


# After a Claude Code update, a session that enters a git worktree stores
# its subagent transcripts under the project dir derived from the *cwd at
# dispatch time* — not the project dir holding the session .jsonl. So the
# co-located ``<session>/subagents`` dir (the legacy layout) is no longer
# the only place to look. The helpers below resolve subagents across both
# the legacy co-located dir AND every cwd-derived dir the session touched.


def _encode_project_path(cwd):
    """Encode a cwd into its Claude project-dir name.

    Forward of ``_decode_project_path``: Claude Code names a project dir
    by replacing every ``/`` and ``.`` in the cwd with ``-`` (so e.g.
    ``/home/u/proj/.worktrees/wt`` → ``-home-u-proj--worktrees-wt``).
    ``_decode_project_path`` is lossy (it can't tell a ``-`` apart from a
    ``/`` or ``.``), so we encode directly here rather than inverting it.
    """
    return cwd.replace('/', '-').replace('.', '-')


# Cache distinct-cwd scans by (path, size, mtime) so repeated listings
# (count + enumerate + drill-in) don't re-read the file.
_SESSION_CWDS_CACHE = {}
_CWD_RE = re.compile(rb'"cwd"\s*:\s*"([^"]*)"')


def _session_cwds(session_jsonl_path):
    """Return the distinct ``cwd`` strings recorded in a session, first-seen order.

    A session can span several working directories over its life (it starts in
    one repo, then enters a git worktree, …); each is recorded verbatim in the
    records' ``cwd`` field. We return them de-duplicated in the order they first
    appear, so callers that surface them (e.g. the context-menu directory
    cluster) list the primary cwd first.

    Cheap single-pass regex over the raw bytes (no JSON parse). Cached by
    ``(path, size, mtime)`` so we read each session file at most once per
    on-disk revision.
    """
    try:
        st = os.stat(session_jsonl_path)
    except OSError:
        return []
    key = (session_jsonl_path, st.st_size, st.st_mtime)
    cached = _SESSION_CWDS_CACHE.get(key)
    if cached is not None:
        return cached
    cwds = []
    seen = set()
    try:
        with open(session_jsonl_path, 'rb') as f:
            for m in _CWD_RE.finditer(f.read()):
                try:
                    cwd = m.group(1).decode('utf-8')
                except UnicodeDecodeError:
                    continue
                if cwd not in seen:
                    seen.add(cwd)
                    cwds.append(cwd)
    except OSError:
        cwds = []
    _SESSION_CWDS_CACHE[key] = cwds
    return cwds


def _collapse_home(path):
    """Collapse a leading ``$HOME`` to ``~`` for display (else verbatim)."""
    if not path:
        return path
    home = os.environ.get('HOME') or os.path.expanduser('~')
    if home and path == home:
        return '~'
    if home and path.startswith(home + '/'):
        return '~' + path[len(home):]
    return path


def _real_project_cwd(project_dir):
    """The true cwd a Claude project directory was encoded from, or ``None``.

    Reads the ``cwd`` recorded in the project's sessions (via the
    ``_session_cwds`` byte-regex scan) rather than inverting the dir name —
    ``_decode_project_path`` is lossy because it can't tell a real ``-``
    from an encoded ``/``/``.``. We prefer a cwd that round-trips back to
    this directory (``_encode_project_path(cwd) == basename``): that pins
    the canonical cwd even for a worktree session whose records carry extra
    dispatch cwds, and is order-independent. Lacking an exact match we
    return any recorded cwd (sorted for determinism), and ``None`` only
    when no session here carries a readable ``cwd`` (e.g. an empty dir).
    """
    try:
        names = sorted(
            n for n in os.listdir(project_dir) if n.endswith('.jsonl')
        )
    except OSError:
        return None
    base = os.path.basename(project_dir.rstrip('/'))
    fallback = None
    for name in names:
        cwds = _session_cwds(os.path.join(project_dir, name))
        for cwd in sorted(cwds):
            if _encode_project_path(cwd) == base:
                return cwd
            if fallback is None:
                fallback = cwd
    return fallback


def _real_project_path(project_dir):
    """Display path for a project dir, hyphen-faithful, HOME→``~`` collapsed.

    The accurate counterpart to ``_decode_project_path``: derives the path
    from a session's recorded ``cwd`` (see ``_real_project_cwd``) so a real
    hyphen in a directory name survives instead of being mangled into ``/``.
    Falls back to the lossy name-decode only when no session here carries a
    readable ``cwd`` (an empty project dir), so we always show *something*.
    """
    cwd = _real_project_cwd(project_dir)
    if cwd:
        return _collapse_home(cwd)
    return _decode_project_path(os.path.basename(project_dir.rstrip('/')))


def _session_cwd_and_root(session_jsonl_path):
    """``(cwd, project_root)`` for a session — the resolver's path anchors.

    ``cwd`` is the session's recorded working directory (its containing
    project dir's ``_real_project_cwd``; ``None`` if unreadable).
    ``project_root`` is the git root above ``cwd`` (nearest ``.git``
    ancestor) when one exists, else the session's real project directory —
    the actual filesystem path (not HOME-collapsed), so callers can
    ``os.path.join`` against it. Both fall back gracefully: an unreadable
    session yields ``(None, <lossy decoded project dir>)``.

    Consumed by the markdown reference resolver to anchor relative ``.md``
    references against the right roots.
    """
    project_dir = os.path.dirname(session_jsonl_path)
    cwd = _real_project_cwd(project_dir)
    if cwd:
        root = _md_doc.find_git_root(cwd) or cwd
        return cwd, root
    # No readable cwd (e.g. an empty project dir): best-effort root from the
    # lossy dir-name decode, expanded to a real absolute path so callers can
    # join against it.
    decoded = _decode_project_path(os.path.basename(project_dir.rstrip('/')))
    return None, os.path.expanduser(decoded)


def _candidate_subagents_dirs(session_jsonl_path):
    """Ordered, deduped, existing-only subagent dirs for a session.

    Legacy co-located dir first (so the common non-worktree case keeps
    today's behaviour and stays the first match), then one
    ``<CLAUDE_ROOT>/<encode(cwd)>/<sid>/subagents`` per distinct session
    cwd (where a worktree session's subagents actually land).
    """
    sid = os.path.basename(os.path.splitext(session_jsonl_path)[0])
    seen = set()
    out = []

    def _add(d):
        if d in seen:
            return
        seen.add(d)
        if os.path.isdir(d):
            out.append(d)

    _add(_subagents_dir(session_jsonl_path))
    for cwd in _session_cwds(session_jsonl_path):
        _add(os.path.join(
            CLAUDE_ROOT, _encode_project_path(cwd), sid, 'subagents'))
    return out


def _resolve_agent_jsonl(session_jsonl_path, agent_id):
    """Path to ``agent-<id>.jsonl``, searching all candidate dirs.

    Returns the first existing match across candidate dirs. When none
    exist, falls back to the legacy co-located path STRING (not checked
    for existence) so callers' ``os.path.isfile`` guards behave exactly
    as before for a truly-absent agent.
    """
    name = f'agent-{agent_id}.jsonl'
    for d in _candidate_subagents_dirs(session_jsonl_path):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    # Team worker: ``agent_id`` is the worker's name, its transcript a
    # sibling session .jsonl.
    p = _find_worker_jsonl(session_jsonl_path, agent_id)
    if p:
        return p
    return os.path.join(_subagents_dir(session_jsonl_path), name)


def _count_subagents(session_jsonl_path):
    """Cheap count of distinct subagent .jsonl files for one session.

    Fast path: the legacy co-located dir is checked first and, when it
    holds any agents, returned directly without scanning session cwds.
    Only when it's absent/empty do we fall back to the cwd-derived dirs
    (the worktree-relocated layout), deduping agent ids across them.
    """
    def _agent_ids(sub_dir):
        try:
            return {
                n[len('agent-'):-len('.jsonl')]
                for n in os.listdir(sub_dir)
                if n.startswith('agent-') and n.endswith('.jsonl')
            }
        except OSError:
            return set()

    n_workers = len(_team_workers_for_session(session_jsonl_path))
    colocated = _agent_ids(_subagents_dir(session_jsonl_path))
    if colocated:
        return len(colocated) + n_workers
    ids = set()
    for sub_dir in _candidate_subagents_dirs(session_jsonl_path):
        ids |= _agent_ids(sub_dir)
    return len(ids) + n_workers


def _read_subagent_meta(meta_path):
    """Parsed dict from a subagent meta.json (``agentType``,
    ``description``, ``name``, ``teamName``, …) or ``{}``."""
    try:
        with open(meta_path, 'r', encoding='utf-8', errors='replace') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _find_inprocess_teammate(session_jsonl_path, name, team):
    """Newest in-process teammate ``(agent_id, path)`` for a spawn, or None.

    New-backend team workers run in-process and their transcripts land
    in the session's own subagents dirs as ``agent-a<name>-<hash>.jsonl``
    with an adjacent ``.meta.json`` carrying the spawn ``name`` /
    ``teamName``. Match on the meta name, plus the team when BOTH sides
    carry one — a later spawn under a re-created team reuses the name
    but not the team, and after a resume the team name stops matching
    the transcript filename, so equality of the two recorded values is
    the only reliable check. A candidate without a meta file falls back
    to the filename convention (``a<name>-`` + hex hash — the hex guard
    keeps ``worker-1`` from claiming a meta-less ``worker-1-foo``).
    Name reuse across respawns: newest mtime wins — spawn order is NOT
    disambiguated, so every same-name/same-team spawn resolves to the
    newest transcript (parity with the legacy sibling scan).
    """
    prefix = f'agent-a{name}-'
    best = None   # (mtime, agent_id, path)
    for d in _candidate_subagents_dirs(session_jsonl_path):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not (n.startswith(prefix) and n.endswith('.jsonl')):
                continue
            meta = _read_subagent_meta(
                os.path.join(d, n[:-len('.jsonl')] + '.meta.json'))
            if meta:
                if meta.get('name') != name:
                    continue
                if (team and meta.get('teamName')
                        and meta.get('teamName') != team):
                    continue
            elif not re.fullmatch(
                    r'[0-9a-f]+', n[len(prefix):-len('.jsonl')]):
                continue
            p = os.path.join(d, n)
            try:
                m = os.stat(p).st_mtime
            except OSError:
                continue
            if best is None or m > best[0]:
                best = (m, n[len('agent-'):-len('.jsonl')], p)
    if best is None:
        return None
    return best[1], best[2]


def _list_subagents_for_session(session_jsonl_path):
    """Enumerate subagent group items for one session.

    Each subagent gets one row whose id encodes both the parent session
    and the agent id, so ``get_children`` can route it without needing
    to remember any per-row state.
    """
    out = []
    seen_ids = set()
    for sub_dir in _candidate_subagents_dirs(session_jsonl_path):
        try:
            names = os.listdir(sub_dir)
        except OSError:
            continue
        for name in names:
            if not (name.startswith('agent-') and name.endswith('.jsonl')):
                continue
            agent_id = name[len('agent-'):-len('.jsonl')]
            # Dedup across dirs: the first candidate (legacy co-located)
            # wins so the common case is untouched.
            if agent_id in seen_ids:
                continue
            agent_path = os.path.join(sub_dir, name)
            try:
                st = os.stat(agent_path)
            except OSError:
                continue
            seen_ids.add(agent_id)
            try:
                with open(agent_path, 'rb') as f:
                    count = sum(1 for _ in f)
            except OSError:
                count = 0
            # Read meta from the SAME dir the agent file was found in.
            meta = _read_subagent_meta(
                os.path.join(sub_dir, f'agent-{agent_id}.meta.json'),
            )
            agent_type = meta.get('agentType') or 'agent'
            description = meta.get('description') or ''
            # Title surfaces the description (what the agent was asked to
            # do) since the agent_id is opaque. Falls back to the id for
            # agents without a meta file.
            title = description or f'agent {agent_id}'
            item = Item(
                id=('agent', session_jsonl_path, agent_id),
                title=_link_marker(
                    ('agent', session_jsonl_path, agent_id)) + title,
                tag=f'{agent_type} · {count} msg · {_human_time(st.st_mtime)}',
                tag_style='magenta',
                has_children=count > 0,
                # Subagent transcripts live in their own .jsonl — a
                # self-contained foreign subtree. ``boundary`` stops
                # recursive/multi-expansion at the group row and keeps
                # the agent's lines out of a parent's preview cascade
                # (the attribute ``_is_cross_file_id`` / ``_walk_umbrella``
                # consult instead of the old ``id[0] == 'agent'`` tag
                # test). Orphan rows inherit it via this builder.
                boundary=True,
            )
            item.kind = 'subagent'
            item.agent_id = agent_id
            item.agent_type = agent_type
            item.agent_description = description
            item.session_path = session_jsonl_path
            item.subagent_path = agent_path
            item.line_count = count
            item.mtime = st.st_mtime
            # Voice stripe: the dispatching assistant call is voice, so
            # the subagent umbrella that represents it carries the
            # assistant-row background colour.
            bg = _ROW_BG_FOR_KIND.get('subagent')
            if bg is not None:
                item.row_bg = bg
            out.append(item)
    # Legacy team workers: on the old backend, named agents spawned via
    # the Agent tool run as sibling *sessions*, not agent-*.jsonl files
    # (in-process ones land in the dirs scanned above). Same row shape, so
    # every downstream consumer (orphan filter, previews, drill-in)
    # treats them like any other subagent group.
    for agent_name, agent_path in _team_workers_for_session(
            session_jsonl_path):
        if agent_name in seen_ids:
            continue
        try:
            st = os.stat(agent_path)
        except OSError:
            continue
        seen_ids.add(agent_name)
        try:
            with open(agent_path, 'rb') as f:
                count = sum(1 for _ in f)
        except OSError:
            count = 0
        member = _worker_running_member(agent_path)
        wmeta = _worker_meta(agent_path)
        roster = (_TEAMS_INDEX.get(wmeta[0]) or {}) if wmeta else {}
        m = roster.get('members', {}).get(agent_name) or {}
        agent_type = m.get('agentType') or 'agent'
        live = 'live · ' if member is not None else ''
        item = Item(
            id=('agent', session_jsonl_path, agent_name),
            title=_link_marker(
                ('agent', session_jsonl_path, agent_name)) + agent_name,
            tag=f'{agent_type} · {live}{count} msg · '
                f'{_human_time(st.st_mtime)}',
            tag_style=('green' if member is not None else 'magenta'),
            has_children=count > 0,
            boundary=True,
        )
        item.kind = 'subagent'
        item.agent_id = agent_name
        item.agent_type = agent_type
        item.agent_description = agent_name
        item.session_path = session_jsonl_path
        item.subagent_path = agent_path
        item.line_count = count
        item.mtime = st.st_mtime
        bg = _ROW_BG_FOR_KIND.get('subagent')
        if bg is not None:
            item.row_bg = bg
        out.append(item)
    # Newest-first ordering matches the rest of the recipe.
    out.sort(key=lambda it: -it.mtime)
    return out


_NAME_FILL_LOCK = threading.Lock()
# dir → queued batch of paths (replaced by newer listings, latest wins)
# or None while the dir's thread is running with nothing queued. A dir
# key present at all means a thread is alive for it.
_NAME_FILL_PENDING = {}


def _fill_session_names(paths):
    """Tail-scan each pending session and patch its row tag.

    The tag is rebuilt from a fresh ``_session_item`` (the tail-scan
    primes the name cache first), not from text captured at listing
    time — so a row whose counts/liveness moved while the scan ran
    isn't clobbered with stale text. Ops go out in small batches;
    ``mod`` never inserts, so rows that vanished meanwhile (refresh,
    filter) are silent no-ops.
    """
    ops = []
    for path in paths:
        if not _session_name_from_file(path):
            continue
        item = _session_item(path)
        if item is None:
            continue
        ops.append(mod(('session', path), tag=item.tag))
        if len(ops) >= 25 and _BROWSER is not None:
            _BROWSER.update_data(ops)
            ops = []
    if ops and _BROWSER is not None:
        _BROWSER.update_data(ops)


def _spawn_session_name_filler(project_dir, paths):
    """Back-fill session names for a just-listed project dir.

    One daemon thread per dir at a time; a listing that lands while
    the thread is busy replaces the dir's queued batch (latest wins)
    and the same thread drains it before exiting — no listing's rows
    are silently dropped. Headless runs (no Browser) skip — there's
    nothing to patch.
    """
    if _BROWSER is None:
        return
    with _NAME_FILL_LOCK:
        if project_dir in _NAME_FILL_PENDING:
            _NAME_FILL_PENDING[project_dir] = paths
            return
        _NAME_FILL_PENDING[project_dir] = None

    def run():
        batch = paths
        while True:
            try:
                _fill_session_names(batch)
            except Exception:
                pass    # a bad file must not kill the drain loop
            with _NAME_FILL_LOCK:
                batch = _NAME_FILL_PENDING.get(project_dir)
                if batch is None:
                    _NAME_FILL_PENDING.pop(project_dir, None)
                    return
                _NAME_FILL_PENDING[project_dir] = None

    threading.Thread(target=run, name='claude-name-fill',
                     daemon=True).start()


def _list_session_children(jsonl_path):
    """Return subagent groups + top-level messages for a session.

    Subagents come first (most users want to see what was dispatched
    before scrolling through hundreds of main-thread messages).
    Sessions without any subagents render exactly as before.
    """
    subs = _list_subagents_for_session(jsonl_path)
    msgs = _list_messages(jsonl_path)
    return subs + msgs


def _list_subagent_messages(item_id):
    """Children for a subagent group ``('agent', jsonl, aid)``: its .jsonl lines."""
    session_path, agent_id = item_id[1], item_id[2]
    if not agent_id:
        return []
    agent_path = _resolve_agent_jsonl(session_path, agent_id)
    if not os.path.isfile(agent_path):
        return []
    return _list_messages(agent_path)


def _list_messages(jsonl_path, limit=None):
    """List messages in **chronological** order (oldest first).

    Matches tree-mode ordering so the cursor-jump-to-latest-voice and
    the t-toggle don't visually flip the conversation on the user.

    Reads from the cached ``_TREE_CACHE[jsonl_path].records`` so we
    don't re-walk the whole file on every navigation. ``_scan_tree``
    populates the cache the first time we visit the path (and
    bootstraps ``_TAIL_STATE`` so the live-tail worker can resume
    from where the scan stopped). Empty/invalid lines (``None`` in
    ``records``) are skipped without surfacing a fake row — the
    ``V`` action can still extract their raw bytes via
    ``line_offsets`` if the user wants to inspect them.

    ``limit`` truncates from the **start** of the file (so the user
    keeps the most recent messages — they're scrolling toward the
    tail anyway). ``limit=0`` (the default after #475) disables
    truncation entirely.
    """
    if limit is None:
        limit = _message_limit
    if not os.path.isfile(jsonl_path):
        return [Item(
            id=('err', jsonl_path),
            title=f'[error] no such file: {jsonl_path}',
            tag='err',
            tag_style='red',
        )]
    td = _scan_tree(jsonl_path)
    total = len(td.records)
    truncated = bool(limit) and total > limit
    start = total - limit if truncated else 0

    out = []
    if truncated:
        older = start
        out.append(Item(
            id=('trunc', jsonl_path),
            title=f'({older} older entries hidden; press e to view file)',
            tag='…',
            tag_style='dim',
        ))
    for n in range(start, total):
        obj = td.records[n]
        if obj is None:
            continue
        kind = _kind_of(obj)
        item_id = ('msg', jsonl_path, n)
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
        item.session_path = jsonl_path
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
