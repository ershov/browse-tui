# ---- context menu --------------------------------------------------------
#
# A right-click / backslash-F1 menu over the cursor's row, branching on its KIND
# (``id[0]``) — message / session / subagent / project, plus the tree-mode
# ``prompt`` / ``tool`` / ``span`` umbrellas (whose rows act on the umbrella's
# own grouped source). Following the committed convention (browse-ps the
# pilot, browse-git the rich multi-kind case): a PURE ``context_menu_options(ctx)``
# builds the ``(label, token)`` rows by kind (opening no modal, so it is
# unit-testable), a flat ``{token: handler}`` table (``_MENU_ACTIONS``)
# dispatches the chosen token, and a thin ``on_context_menu(ctx)`` ties the
# two together. Submenus are just a handler re-invoking ``ctx.menu`` (the
# ``git`` row opens a mode submenu this way). Hotkey
# hints are LITERAL text — menus don't parse ``&`` — so an entry duplicating a
# global keybinding shows it in parens (``Edit source in $EDITOR (E)``). The
# existing per-row actions (V / E / M / y / m) are reused directly so the menu
# rows stay single-sourced with the keybindings. No clipboard rows (the values
# aren't visible — paths/ids surface via ``ctx.alert``); pop-ups via
# ``ctx.alert``. The menu is read-only-friendly: a missing tool flashes via
# ``run_external``'s own error rather than crashing (browse-tui swallows
# ``on_context_menu`` exceptions besides).
#
# The DIRECTORY cluster is the actions-first hierarchy described in the ticket.
# A session/project/message row resolves its CONTEXT DIRS — the project source
# dir, every distinct working dir the session recorded (a session can span more
# than one, e.g. when it enters a git worktree), and each one's enclosing git
# work-tree root — which usually coincide. They are deduped by
# ``os.path.realpath`` (``_dedup_dirs``); each Level-1 action (Open in
# browse-fs, Run shell here, git, Browse plan, Show full path) is shown only if
# it applies to ≥1 deduped dir, and a directory chooser appears only when >1
# DISTINCT dir qualifies for the chosen action (so a session that worked in two
# directories offers both). The single ``git`` row opens a Level-2 mode submenu
# (commits / branches / status / stashes / reflog) before that dir chooser. The
# action set is built once and shared across the dirs (not duplicated per
# directory). See ``_dir_action_rows`` / ``_run_dir_action``.


# -- context dirs: project / cwd(s) / worktree ------------------------------


def _cursor_context_dirs(item_id):
    """``[(role, abspath), …]`` context dirs for ``item_id`` — project / cwd(s) / worktree.

    A project / session / message / subagent row anchors on a Claude project
    dir (a session row's project dir is its .jsonl's parent). A single session
    can span SEVERAL working directories over its life (it starts in one repo,
    then enters a git worktree, …) — each recorded in the records' ``cwd`` — so
    we surface every distinct one, in role order:

      * ``project``   — the Claude project's real source directory
        (``_real_project_cwd``), the canonical anchor.
      * ``cwd``       — the session's PRIMARY recorded working directory (the
        one that round-trips to the project dir name). For the common case this
        is the same dir as ``project``; both roles are surfaced so the chooser
        can label a single dir by every role it fills, and dedup collapses them.
      * ``workspace`` — each ADDITIONAL distinct working directory the session
        recorded (e.g. a git worktree it entered). This is the case the chooser
        exists for: a session that genuinely worked in two directories offers
        both.
      * ``worktree``  — the enclosing git work-tree root of each cwd
        (``md_doc.find_git_root``; a ``.git`` *file* is how a worktree records
        its gitdir, so this resolves worktree sessions too). Omitted for a cwd
        that isn't under a git work tree, or when md_doc is unavailable.

    The session's cwds are read from its OWN .jsonl (``_session_cwds``) for a
    session / message / subagent row, so a row resolves the dirs *that session*
    touched. A ``project`` row has no single session, so it falls back to the
    project-wide ``_real_project_cwd`` (one representative cwd) — unchanged.

    Returns raw (un-deduped) ``(role, abspath)`` pairs with falsy paths
    dropped; ``_dedup_dirs`` collapses coincident roles by realpath, so a
    single-cwd session still yields ONE dir (no chooser). A project / session /
    message row always resolves at least one dir (its real cwd, or the decoded
    project dir as a fallback); a synthetic / markdown / non-tuple cursor
    yields ``[]`` so the directory cluster is simply omitted.
    """
    if not isinstance(item_id, tuple) or not item_id:
        return []
    kind = item_id[0]
    session_jsonl = None
    if kind == 'project':
        project_dir = item_id[1]
    elif kind in ('session', 'agent', 'msg', 'prompt', 'tool', 'span'):
        # A session/message/subagent row is keyed under its .jsonl, whose
        # containing dir is the Claude project dir.
        session_jsonl = item_id[1]
        project_dir = os.path.dirname(session_jsonl)
    else:
        return []

    # project: the Claude project's canonical source dir. Read straight from
    # the project dir's sessions (no md_doc dependency); fall back to the lossy
    # name-decode when no session records a readable cwd (an empty/unreadable
    # project dir) so the row still browses *something*.
    project = _real_project_cwd(project_dir) or os.path.expanduser(
        _decode_project_path(os.path.basename(project_dir.rstrip('/'))))

    # The session's recorded working dirs, first-seen order. For a session /
    # message / subagent row read THIS session's .jsonl (the dirs that session
    # touched); a project row has no single session, so use its primary cwd.
    if session_jsonl is not None:
        cwds = _session_cwds(session_jsonl)
    else:
        cwds = [project] if project else []
    # The primary cwd (the one that round-trips to the project dir name) is the
    # ``cwd`` role; any other distinct cwd is a ``workspace``. Lacking that the
    # first recorded cwd is primary.
    base = os.path.basename(project_dir.rstrip('/'))
    primary = next((c for c in cwds if _encode_project_path(c) == base),
                   cwds[0] if cwds else None)

    out = [('project', project)]
    for cwd in cwds:
        out.append(('cwd' if cwd == primary else 'workspace', cwd))
        # worktree: the enclosing git work-tree root of this cwd. Guarded on
        # md_doc (the only piece here that needs it) so the cluster still
        # resolves project / cwd(s) when the md_doc plugin failed to import.
        if _md_doc is not None:
            root = _md_doc.find_git_root(cwd)
            if root:
                out.append(('worktree', root))
    return [(role, path) for role, path in out if path]


def _dedup_dirs(role_paths):
    """Collapse ``[(role, path), …]`` by ``os.path.realpath``, merging roles.

    Returns ``[(roles, realpath), …]`` in first-seen order, where ``roles`` is
    the list of roles that resolved to the same real path (e.g.
    ``['project', 'cwd', 'worktree']`` when all three coincide — the common
    case). Merging the roles lets a chooser entry label a single dir by every
    role it fills (``project / cwd: /p``) instead of listing it thrice.
    """
    by_real = {}
    order = []
    for role, path in role_paths:
        real = os.path.realpath(path)
        if real not in by_real:
            by_real[real] = []
            order.append(real)
        by_real[real].append(role)
    return [(by_real[real], real) for real in order]


def _is_git_repo(path):
    """True iff ``path`` is inside a git work tree (``find_git_root`` hits).

    Reuses md_doc's ``.git``-walk (a worktree's ``.git`` file counts), so the
    ``git`` submenu lights up for both a plain repo and a worktree. ``False``
    when md_doc is unavailable — the ``git`` row then never appears.
    """
    if _md_doc is None or not path:
        return False
    return _md_doc.find_git_root(path) is not None


def _has_plan_md(path):
    """True iff ``path`` contains a ``.PLAN.md`` file (gates the browse-plan row)."""
    return bool(path) and os.path.isfile(os.path.join(path, '.PLAN.md'))


# Git-mode submenu rows: ``(submenu_label, mode)`` (mode -> browse-git's
# ``--<mode>`` flag). The single Level-1 ``git`` row (shown when ≥1 context dir
# is a git repo) opens a Level-2 mode submenu built from these; the chosen mode
# launches ``browse-git <dir> --<mode>`` on the chosen dir (browse-git's
# verified ``<dir> --<mode>`` interface).
_DIR_GIT_MODES = [
    ('commits',  'commits'),
    ('branches', 'branches'),
    ('status',   'status'),
    ('stashes',  'stash'),
    ('reflog',   'reflog'),
]


def _dir_action_rows(dirs):
    """Level-1 directory-action rows for the deduped ``dirs`` (pure — no modal).

    ``dirs`` is the ``_dedup_dirs`` output (``[(roles, realpath), …]``). Builds
    the SHARED action set once (not per directory); each row is shown only if
    its action applies to ≥1 dir:

      * Open in browse-fs        — always (every dir is browsable).
      * Run shell here           — always (every dir is shell-able).
      * git                      — when ≥1 dir is a git repo (``_is_git_repo``);
        a single row whose handler opens a Level-2 mode submenu.
      * Browse plan in browse-plan — when ≥1 dir has ``.PLAN.md``.
      * Show full path           — always (pops up every distinct dir's path).

    Returns ``(label, token)`` rows; the token half is dispatched by
    ``_run_dir_action`` (via ``_MENU_ACTIONS``). ``[]`` when ``dirs`` is empty,
    so a row with no resolvable context dir gets no directory cluster.
    """
    if not dirs:
        return []
    paths = [path for _roles, path in dirs]
    rows = [('Open in browse-fs', 'dir.fs'), ('Run shell here', 'dir.shell')]
    if any(_is_git_repo(p) for p in paths):
        rows.append(('git ▸', 'dir.git'))
    if any(_has_plan_md(p) for p in paths):
        rows.append(('Browse plan in browse-plan', 'dir.plan'))
    rows.append(('Show full path', 'dir.path'))
    return rows


def _dir_chooser_rows(dirs):
    """``(label, realpath)`` chooser rows for ``dirs`` — role-labelled paths.

    Each entry is labelled by the dir's role(s) plus its path
    (``project / cwd: /p``, ``worktree: /w``), so a user disambiguating two
    distinct dirs sees what each is. Pure; the Level-2 chooser handler
    (``_choose_dir``) re-invokes ``ctx.menu`` over these.
    """
    return [(f"{' / '.join(roles)}: {path}", path) for roles, path in dirs]


def _choose_dir(ctx, dirs):
    """Resolve which dir an action runs on: skip the chooser when only one qualifies.

    With exactly one ``dirs`` entry, return its path directly (no modal — the
    Level-2 chooser only appears when >1 DISTINCT dir applies). With more, open
    the role-labelled chooser via ``ctx.menu`` and return the chosen path (or
    ``None`` on cancel). Pure list-building lives in ``_dir_chooser_rows`` so
    the chooser content is unit-testable without a modal.
    """
    if len(dirs) == 1:
        return dirs[0][1]
    return ctx.menu(_dir_chooser_rows(dirs))


def _run_shell(ctx, cwd):
    """Open an interactive ``$SHELL`` with ``cwd`` as the working directory.

    ``ctx.run_external`` has no ``cwd`` knob, so the directory change rides in a
    shell-string command (run via ``sh -c``): ``cd <dir> && exec <$SHELL> -l -i``.
    ``exec`` replaces the wrapping ``sh`` with the user's shell so signals / the
    exit code pass through; ``-i`` makes it interactive and ``-l`` a login shell
    so it loads the user's login startup files (and thus their real prompt) — a
    plain ``-i`` non-login shell reads only ``~/.bashrc`` and misses a PS1 set up
    only on login. The ``cd`` still wins (it runs before ``exec`` and login
    startup doesn't change directory); ``$SHELL`` defaults to ``sh``. Mirrors
    browse-fs's ``_run_shell``.
    """
    import shlex
    shell = os.environ.get('SHELL') or 'sh'
    ctx.run_external(f'cd {shlex.quote(cwd)} && exec {shell} -l -i')


def _run_git_mode(ctx, dirs, mode):
    """Launch ``browse-git <dir> --<mode>`` on a chosen repo dir.

    Filters ``dirs`` to the git repos, picks one via ``_choose_dir`` (Level-3
    chooser only when >1 repo dir qualifies), and runs browse-git with
    ``keep_screen=True`` (a sibling recipe gets the alt screen). No-op when no
    dir is a repo or the chooser is cancelled.
    """
    applicable = [d for d in dirs if _is_git_repo(d[1])]
    if not applicable:
        return
    path = _choose_dir(ctx, applicable)
    if path is None:
        return
    ctx.run_external(['browse-git', path, f'--{mode}'], keep_screen=True)


def _run_dir_action(ctx, token, dirs):
    """Dispatch a Level-1 directory action ``token`` against the deduped ``dirs``.

    Filters ``dirs`` to those the action applies to (browse-plan → dirs with
    ``.PLAN.md``; browse-fs / run-shell / show-path apply to all), then:

      * "Show full path" pops up every distinct dir's path (one per line) — no
        chooser, per the ticket exception.
      * ``dir.git`` opens a Level-2 mode submenu (commits / branches / …) and
        threads the chosen mode into ``_run_git_mode`` (repo dirs only).
      * Otherwise picks the dir via ``_choose_dir`` (which skips the chooser
        when exactly one qualifies) and launches the external tool through
        ``ctx.run_external``. browse-fs / browse-git / browse-plan are other
        browse-tui recipes, so ``keep_screen=True`` hands them the alt screen;
        run-shell replaces the wrapping ``sh`` with the user's interactive
        ``$SHELL`` (so no ``keep_screen``).
    """
    if token == 'dir.path':
        ctx.alert('\n'.join(path for _roles, path in dirs), title='paths')
        return

    if token == 'dir.git':
        # Level-2 mode submenu, then the shared git launch on a repo dir.
        mode = ctx.menu(list(_DIR_GIT_MODES))
        if mode is not None:
            _run_git_mode(ctx, dirs, mode)
        return

    if token == 'dir.plan':
        applicable = [d for d in dirs if _has_plan_md(d[1])]
    else:  # dir.fs / dir.shell apply to every dir
        applicable = dirs
    if not applicable:
        return

    path = _choose_dir(ctx, applicable)
    if path is None:
        return

    if token == 'dir.fs':
        ctx.run_external(['browse-fs', path], keep_screen=True)
    elif token == 'dir.shell':
        _run_shell(ctx, path)
    elif token == 'dir.plan':
        ctx.run_external(['browse-plan', '-f', os.path.join(path, '.PLAN.md')],
                         keep_screen=True)


# -- per-kind menu builders -------------------------------------------------


def context_menu_message(ctx, item_id):
    """``(label, token)`` rows for a ``('msg', jsonl, n)`` cursor row.

    Reuses the existing message actions (E / V / M / y / m), so the duplicate
    rows carry the literal hotkey hint per the convention. "Toggle markdown
    coloring" is offered only when md2ansi_lib loaded (the ``m`` action is only
    bound then). The directory cluster is appended (the message's session
    resolves project/cwd/worktree).
    """
    rows = [
        ('Edit source in $EDITOR (E)',     'msg.edit'),
        ('View source in $PAGER (V)',      'msg.view'),
        ('Render markdown via mdcat (M)',  'msg.mdcat'),
        ('Show full id (y)',               'msg.id'),
    ]
    if _md2ansi_fn is not None:
        rows.append(('Toggle markdown coloring (m)', 'msg.toggle_md'))
    return rows


def context_menu_session(ctx, item_id):
    """``(label, token)`` rows for a ``('session', jsonl)`` cursor row.

    Open the transcript in $PAGER (reuses the ``V`` source path) and show the
    session path (pop-up). The directory cluster is appended.
    """
    return [
        ('Open transcript in $PAGER (V)', 'session.view'),
        ('Show session path',             'session.path'),
    ]


def context_menu_agent(ctx, item_id):
    """``(label, token)`` rows for an ``('agent', jsonl, aid)`` subagent row.

    Open the subagent transcript in $PAGER and show the agent id (pop-up). The
    directory cluster is appended (a subagent shares its session's dirs).
    """
    return [
        ('Open agent transcript in $PAGER (V)', 'agent.view'),
        ('Show agent id',                       'agent.id'),
    ]


def context_menu_project(ctx, item_id):
    """``(label, token)`` rows for a ``('project', dir)`` cursor row.

    A project row carries no per-row actions of its own beyond the directory
    cluster — which is exactly the point of surfacing it here (the project /
    cwd / worktree dirs are most directly resolvable from a project row). The
    cluster is appended by ``context_menu_options``.
    """
    return []


def context_menu_umbrella(ctx, item_id):
    """``(label, token)`` rows for a ``('prompt'|'tool'|'span', jsonl, line)`` row.

    A tree-mode umbrella groups a turn / tool-execution / metadata span; its
    source actions act on the umbrella ITSELF — the grouped lines, resolved by
    ``_gather_line_source`` / ``_gather_umbrella_lines``. Reuses the message
    ``msg.*`` source tokens (E / V / y) so the rows stay single-sourced with
    those handlers (which read ``ctx.cursor`` and already handle umbrellas). The
    directory cluster is appended by ``context_menu_options`` (the umbrella's
    session resolves project/cwd/worktree).
    """
    return [
        ('Edit source in $EDITOR (E)', 'msg.edit'),
        ('View source in $PAGER (V)',  'msg.view'),
        ('Show full id (y)',           'msg.id'),
    ]


# -- dispatch + the hook ----------------------------------------------------
#
# ``_MENU_ACTIONS`` maps every token a ``context_menu_*`` builder can emit to
# its handler ``(ctx, item_id)``. The per-row message/session/agent actions
# reuse the recipe's existing action handlers directly (``_action_edit_source``
# / ``_action_view_source`` / ``_action_md_preview`` / ``_action_show_id`` /
# ``_action_toggle_md``), which read ``ctx.cursor`` themselves — so the menu
# rows stay single-sourced with the E / V / M / y / m keybindings. The
# directory tokens route through ``_run_dir_action`` against the cursor's
# deduped context dirs.


def _dispatch_dir(ctx, item_id, token):
    """Run a ``dir.*`` token against the cursor's deduped context dirs."""
    dirs = _dedup_dirs(_cursor_context_dirs(item_id))
    _run_dir_action(ctx, token, dirs)


_MENU_ACTIONS = {
    # message — reuse the existing E / V / M / y / m action handlers.
    'msg.edit':      lambda c, i: _action_edit_source(c),
    'msg.view':      lambda c, i: _action_view_source(c),
    'msg.mdcat':     lambda c, i: _action_md_preview(c),
    'msg.id':        lambda c, i: _action_show_id(c),
    'msg.toggle_md': lambda c, i: _action_toggle_md(c),
    # session
    'session.view':  lambda c, i: _action_view_source(c),
    'session.path':  lambda c, i: c.alert(i[1], title='session'),
    # agent / subagent
    'agent.view':    lambda c, i: _action_view_source(c),
    'agent.id':      lambda c, i: c.alert(i[2], title='agent'),
    # directory cluster (shared across project / session / message / umbrella
    # rows). ``dir.git`` opens a mode submenu inside ``_run_dir_action``.
    'dir.fs':           lambda c, i: _dispatch_dir(c, i, 'dir.fs'),
    'dir.shell':        lambda c, i: _dispatch_dir(c, i, 'dir.shell'),
    'dir.git':          lambda c, i: _dispatch_dir(c, i, 'dir.git'),
    'dir.plan':         lambda c, i: _dispatch_dir(c, i, 'dir.plan'),
    'dir.path':         lambda c, i: _dispatch_dir(c, i, 'dir.path'),
}


def context_menu_options(ctx):
    """``(label, token)`` rows for the browse-claude context menu (pure — no modal).

    Branches on the cursor item's KIND (``id[0]``): a message
    (``('msg', jsonl, n)``), session (``('session', jsonl)``), subagent
    (``('agent', jsonl, aid)``), project (``('project', dir)``) or tree-mode
    umbrella (``('prompt'|'tool'|'span', jsonl, line)``) row gets its per-kind
    rows, then the SHARED directory cluster (``_dir_action_rows`` against the
    cursor's deduped context dirs) when those rows resolve. A synthetic /
    markdown / cursorless row has no menu (``[]``), so the hook opens nothing.
    The token half is dispatched by ``_MENU_ACTIONS`` / ``on_context_menu``.
    """
    item_id = ctx.cursor.id if ctx.cursor else None
    if not isinstance(item_id, tuple) or not item_id:
        return []
    kind = item_id[0]
    if kind == 'msg' and len(item_id) == 3:
        rows = context_menu_message(ctx, item_id)
    elif kind == 'session':
        rows = context_menu_session(ctx, item_id)
    elif kind == 'agent' and len(item_id) == 3:
        rows = context_menu_agent(ctx, item_id)
    elif kind == 'project':
        rows = context_menu_project(ctx, item_id)
    elif kind in ('prompt', 'tool', 'span') and len(item_id) == 3:
        rows = context_menu_umbrella(ctx, item_id)
    else:
        return []
    # Cross-link jump rows first (they're the row-specific action), then
    # the per-kind rows, then the shared directory cluster, deduped.
    dirs = _dedup_dirs(_cursor_context_dirs(item_id))
    return _link_menu_rows(item_id) + list(rows) + _dir_action_rows(dirs)


def on_context_menu(ctx):
    """Open the browse-claude context menu and dispatch the chosen entry.

    The hook the framework fires on right-click / backslash-F1: build the (pure)
    per-kind option list, open it with ``ctx.menu``, and route the chosen token
    through ``_MENU_ACTIONS`` (passing the cursor's id so each handler reads its
    own fields). A cancel (``None``) or an empty list is a no-op. The cursor id
    is re-read at dispatch time so a token always pairs with the row it came
    from.
    """
    token = ctx.menu(context_menu_options(ctx))
    # Cross-link jump rows carry their target in the token (they're
    # per-row values, not fixed actions — see ``_link_menu_rows``).
    if (isinstance(token, tuple) and len(token) == 2
            and token[0] == 'link.jump'):
        _jump_to_link(ctx, token[1])
        return
    handler = _MENU_ACTIONS.get(token)
    if handler is None:
        return
    item = ctx.cursor
    handler(ctx, item.id if item is not None else None)
