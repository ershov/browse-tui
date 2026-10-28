# ---- entry point ---------------------------------------------------------


_HELP_USAGE_TMPL = """\
Usage: browse-agents-claude [OPTIONS] [PROJECT]
  -n NUMBER       Cap messages enumerated per session (default: {default}).
  --session ID    Open the .jsonl for that sessionId directly.
  --pid PID       Open the .jsonl owned by the running Claude PID.
  --file PATH     Open an arbitrary .jsonl file directly.
  --running       Filter projects/sessions to those with a live PID.
  --item N        After --session/--pid/--file, position the cursor on
                  line N (1-based) of the resolved transcript.
  --tree          Show sessions as a tree, grouped by turn (default).
                  Subagents list in a top block, newest first.
  --no-tree       Show sessions as a flat reverse-time list.
  --detail LEVEL  Detail level (default: 1). LEVEL is 1-7 or a word:
                  1=summary (user prompts + end_turn responses),
                  2=voice (all speech), 3=edits (+ Edit/Write/NotebookEdit),
                  4=tools (+ all tool calls), 5=detailed (+ useful
                  metadata), 6=all (every known record kind),
                  7=unknown (+ unrecognised record kinds — maintenance
                  view). Change it live with the '1'-'7' keys.
  PROJECT         Project directory name under ~/.claude/projects/ to
                  drill into on launch. Ignored when --session / --pid /
                  --file is given.\
"""


_HELP_INTRO = """\
browse-agents-claude — Claude Code project / session / message browser.

Walks ~/.claude/projects/ to surface a four-level tree: projects ->
sessions -> messages (or subagents) -> subagent messages. Drill into
a transcript with Right/Alt-Down; preview shows the rendered turn.

  v / V   page the preview (v) / the source .jsonl (V) via $PAGER.
  e / E   open the preview (e) / the source .jsonl (E) in $EDITOR.
  m       toggle inline markdown coloring of voice content (only bound
          when the md2ansi_lib Python module is importable).
  M       render the preview via $MDCAT / mdcat (external).
  J / K   jump cursor to next / previous user-or-assistant voice.
  t       toggle flat / tree view.
  1-7     detail level: 1 summary, 2 voice, 3 edits, 4 tools,
          5 detailed, 6 all, 7 unknown (adds unrecognised kinds).
  Enter   follow a row's [↗] cross-link — every link is two-way: the
          spawn site pairs with the subagent row, message rows pair
          with their counterpart in the other transcript; a row with
          several links opens a menu of targets.

Right-click a row (or the backslash / F1 keys) for a menu: message and
turn / tool / span umbrella rows offer the source actions above
(edit / view / id, plus mdcat / toggle on a message); session and subagent
rows open the transcript and show their path / id. Every row also surfaces a
directory cluster over its project / cwd / worktree dirs — open in browse-fs,
run a shell there, browse the git history (a submenu of commits / branches /
status / stashes / reflog), or browse its .PLAN.md.\
"""


_message_limit = _MESSAGE_LIMIT


def main():
    global _message_limit, _TREE_MODE, _DETAIL_LEVEL, _BROWSER
    _init_color()
    _message_limit = _pop_limit_arg(_MESSAGE_LIMIT)

    prog = _prog_name()
    item_arg = _pop_value('--item', int)

    # View-mode flags (--tree wins over --no-tree if both are passed).
    tree_flag    = _pop_flag('--tree')
    no_tree_flag = _pop_flag('--no-tree')
    if tree_flag:
        _TREE_MODE = True
    elif no_tree_flag:
        _TREE_MODE = False

    # Detail level: summary (level 1) by default. --detail LEVEL
    # (or --detail=LEVEL) sets it, accepting the digits 1-7 or the word
    # aliases summary/voice/edits/tools/detailed/all/unknown. An invalid
    # value pops as the ``False`` sentinel — report it as a usage error.
    detail_arg = _pop_value('--detail', _parse_detail_level)
    if detail_arg is False:
        print(f'{prog}: --detail must be 1-7 or one of '
              'summary/voice/edits/tools/detailed/all/unknown',
              file=sys.stderr)
        sys.exit(2)
    if detail_arg is not None:
        _DETAIL_LEVEL = detail_arg

    # The store's startup indexes, then its own target flags.
    # ``target`` is the session to open directly, if any.
    store_init()
    args = store_pop_args()
    if args['error']:
        print(f'{prog}: {args["error"]}', file=sys.stderr)
        sys.exit(2)
    if item_arg is False:
        print(f'{prog}: invalid argument value', file=sys.stderr)
        sys.exit(2)
    target = args['target']

    # The recipe's own positionals: everything left after the flag pops
    # above, minus the framework's ``--tty`` flag (and its value).
    # ``Browser.run()`` auto-detects ``--tty`` but leaves it in
    # ``sys.argv``, so reading ``sys.argv[1]`` directly would misread
    # ``--tty -`` / ``--tty /dev/pts/N`` as the PROJECT positional (a
    # bogus ``('project', abspath('--tty'))`` scope).
    pos_args = recipe_argv()

    initial_scope = None
    if target is None and pos_args:
        # Seed conversion (§4.3 — the ONLY string→id conversion): the
        # user-typed positional becomes the structured scope root. The
        # store decides whether it names a session (opened like
        # ``--file``, cursor on the latest voice) or a project.
        kind, key = store_resolve_positional(pos_args[0])
        if kind == 'session':
            target = key
        else:
            initial_scope = (kind, key)

    if item_arg is not None and not target:
        print(f'{prog}: --item requires --session/--pid/--file',
              file=sys.stderr)
        sys.exit(2)

    # Initial scope strategy when a target session was resolved:
    #
    #   --item N:               scope INTO the session, cursor on message N.
    #
    #   no --item:              scope INTO the session in BOTH modes, cursor
    #                           on last voice (the conversation, not the
    #                           card+timeline, is what the user came for).
    #                           Two-level scope_stack ([project, session])
    #                           so alt-up pops one logical level (session →
    #                           project) instead of jumping to the root.
    parent_scope = None
    if target:
        initial_scope = ('session', target)
        parent_scope = ('project', store_project_of_session(target))

    # 'M' always pages the preview through the external $MDCAT / mdcat.
    # 'm' toggles inline markdown coloring iff the md2ansi_lib Python module
    # was importable; otherwise it's left unbound.
    actions_m = [Action('M', 'View preview via $MDCAT / mdcat',
                        _action_md_preview, 'cursor')]
    if _md2ansi_fn is not None:
        actions_m.insert(0, Action('m', 'Toggle voice markdown coloring',
                                   _action_toggle_md, 'none'))

    b = Browser(BrowserConfig(
        title=prog,
        show_children_pane=False,
        # Item ids are tagged tuples (``('project', dir)`` /
        # ``('session', sess)`` / ``('msg', sess, n)`` / …). Their
        # ``str()`` would be noisy ``repr`` chrome that duplicates or
        # contradicts the title, so suppress them from the row chrome —
        # the title alone identifies the row. ``y`` action still surfaces
        # the full id on demand.
        show_ids='never',
        # Keep the tree-mode ``--- Subagents: / --- Session:`` meta
        # dividers (emitted in ``_list_tree_roots``) visible even while a
        # reduced detail level is active, so the section labels persist.
        meta_filter_mode='show',
        get_children=get_children,
        get_preview=get_preview,
        root_id=None,
        initial_scope=initial_scope,
        # Enter follows a row's cross-link ([↗] rows on both sides of a
        # leader↔teammate interaction — spawn sites, SendMessage
        # dispatches, teammate replies, subagent group rows, and the
        # worker-side inbound / send rows); everything else stays a
        # silent no-op.
        on_enter=_on_enter_jump,
        actions=[
            # v / e are the framework defaults (page / edit the preview text).
            # V / E open the underlying source file instead.
            # Gate 'none' (rather than 'cursor') so V/E fire on the
            # scope_root row too — the user expects to view/edit the
            # currently-open file from its synthetic top row.
            Action('V',     'View source for cursor/selection in $PAGER',  _action_view_source, 'none'),
            Action('E',     'Edit source for cursor/selection in $EDITOR', _action_edit_source, 'none'),
            *actions_m,
            Action('y',     'Show full id (paste-friendly)',   _action_show_id,     'cursor'),
            # 'none' gate so J/K fire even when the cursor sits on a
            # scope_root row (where ctx.cursor returns None). The
            # handlers themselves consult ctx.cursor_index/visible_items
            # and work regardless of cursor's normal/scope_root kind.
            Action('J',     'Jump to next voice (user/asst)',  _action_next_voice,  'none'),
            Action('K',     'Jump to prev voice (user/asst)',  _action_prev_voice,  'none'),
            Action('t',     'Toggle flat / tree view',         _action_toggle_tree, 'none'),
            # Detail level (absolute setters): 1=summary, 2=voice,
            # 3=edits, 4=tools, 5=detailed, 6=all, 7=unknown. Each sets
            # _DETAIL_LEVEL and recomputes row visibility (see
            # _set_detail_level).
            Action('1',     'Detail: summary',                 lambda c: _set_detail_level(c, 1), 'none'),
            Action('2',     'Detail: voice',                   lambda c: _set_detail_level(c, 2), 'none'),
            Action('3',     'Detail: edits',                   lambda c: _set_detail_level(c, 3), 'none'),
            Action('4',     'Detail: tools',                   lambda c: _set_detail_level(c, 4), 'none'),
            Action('5',     'Detail: detailed',                lambda c: _set_detail_level(c, 5), 'none'),
            Action('6',     'Detail: all',                     lambda c: _set_detail_level(c, 6), 'none'),
            Action('7',     'Detail: unknown',                 lambda c: _set_detail_level(c, 7), 'none'),
            # Right: the framework default expands a collapsed row; the
            # ``on_expand`` / ``on_children_loaded`` hooks then jump the
            # cursor to the latest voice (for every expansion source, not
            # just this keypress). This minimal handler keeps only the
            # re-press case — drilling to the latest voice one level down
            # when the row is already expanded (no expand → no hook).
            Action('right', 'Expand + cursor to latest voice', _action_tree_right,  'cursor'),
            # alt-down (scope-down) uses the framework default; the
            # ``on_scope_change`` hook below upgrades a cross-file
            # scope_root's preview on scope-in.
        ],
        # Post-expand jump-to-latest-voice + cross-file preview upgrade,
        # composed across the two hooks so they fire for EVERY expansion
        # source (keyboard, programmatic, deep-link, startup), not only
        # the right-arrow override the recipe used to carry. The cached
        # case jumps in ``on_expand``; the uncached case defers to
        # ``on_children_loaded`` once the fetch settles.
        on_expand=_on_expand,
        on_children_loaded=_on_children_loaded,
        # Scope-in to a cross-file id upgrades the new scope_root from
        # the lightweight metadata card to the heavy umbrella cascade.
        # Fires for alt-down, programmatic scope, and startup alike.
        on_scope_change=_on_scope_change,
        # Right-click (or the backslash / F1 keys) opens a per-kind menu:
        # message (E/V/M/y/m source actions), prompt/tool/span umbrellas
        # (E/V/y on the umbrella's own source), session / subagent transcript +
        # path, and a shared directory cluster over the row's project / cwd /
        # worktree dirs (browse-fs, run-shell, a git mode submenu, browse-plan).
        # See ``on_context_menu``.
        on_context_menu=on_context_menu,
        # Preview content is width-dependent: ``_md_voice`` lays out voice
        # markdown (prose wrap AND table column widths) through md2ansi at
        # ``ctx.preview_width``. The framework caches that rendered text by
        # id (width-agnostic) and, on a layout change, only re-wraps the
        # already-laid-out text — it can't re-pack an md2ansi table to a new
        # width. So on ANY pane-layout change (terminal resize, split
        # selector, list-ratio nudge — #828 broadened on_resize to fire on
        # all of them) drop the preview cache; the framework then refetches
        # and ``get_preview`` re-renders at the now-current preview width.
        on_resize=lambda ctx, cols, rows: ctx.drop_preview_cache(),
        help_usage=_HELP_USAGE_TMPL.format(default=_MESSAGE_LIMIT),
        help_intro=_HELP_INTRO,
    ))
    _BROWSER = b

    # Two-level scope_stack: [('project', key), ('session', key)].
    # ``initial_scope`` was set above to the session id, so the framework's
    # __init__ pushed that onto the stack as the current scope. Slot the
    # project scope UNDER it so alt-up from inside the file pops one logical
    # level (file → project dir) instead of jumping straight to the project
    # root. Direct list insert preserves the framework's _expanded_by_scope
    # bookkeeping the __init__ scope_into already established.
    if target and parent_scope and parent_scope != initial_scope:
        b._state.scope_stack.insert(0, parent_scope)

    # Seed a REAL session Item for the scope_root so the framework never
    # synthesizes a raw-path placeholder for the initial scope on direct
    # launch — otherwise the top scope-root preview is blank (#730).
    # Synchronous (before ``run()``), so the real Item is in the index
    # ahead of the event loop's first ``visible_items`` pass.
    if target and parent_scope:
        _seed_scope_root_item(b, target)

    # Post-construction cursor positioning:
    #
    #   --item N:          cursor → ``('msg', sess, N-1)``. Scope already
    #                      the session, so messages render on first paint.
    #
    #   no --item:         cursor → last voice in the session. Deferred
    #                      until scope_root's children have loaded
    #                      (see ``_focus_latest_voice_when_ready``).
    #                      Works in both flat and tree mode — the only
    #                      difference is whether the chain needs to walk
    #                      ancestor umbrellas (tree) or not (flat).
    if target:
        if item_arg is not None:
            n0 = max(1, item_arg) - 1
            b.cursor_to(('msg', target, n0))
        else:
            _focus_latest_voice_when_ready(b, target)

    # Live-tail worker: polls the cursor's session through the store and
    # folds newly-appended records into the cached _TreeData, then pushes
    # update_data patches for any umbrella whose visible child list
    # grew. Daemon thread so it doesn't keep the process alive on
    # its own; _TAIL_STOP signals clean shutdown before sys.exit.
    global _TAIL_STOP, _TAIL_THREAD
    _TAIL_STOP = threading.Event()
    _TAIL_THREAD = threading.Thread(
        target=_tail_worker, args=(b,),
        name=f'{prog}-tail', daemon=True,
    )
    _TAIL_THREAD.start()

    try:
        rc = b.run()
    finally:
        _TAIL_STOP.set()
        # Give the worker a brief moment to wake from its wait()
        # before the process exits — keeps shutdown clean in tests.
        _TAIL_THREAD.join(timeout=1.0)
    sys.exit(rc)


if __name__ == '__main__':
    main()
