"""browse-tui: action dispatch and default built-in actions.

The action layer is the surface recipes use to override the default
keybindings. ``Action`` is a tiny dataclass — a key string, a label, a
handler, and a ``requires`` precondition tag — and ``dispatch_key`` is
the runtime mapping from key names (as produced by ``read_key`` in
``020-terminal``) to handlers.

Built-in defaults cover navigation (j/k, arrows, home/end, pgup/pgdn),
expand/collapse on left/right, search start (/), preview toggle
(ctrl-p), help toggle (?, F1), reload (ctrl-r), redraw (ctrl-l), and
quit (q, esc, ctrl-c). User-supplied actions on a Browser override
defaults for the same key.

The actual rendering of search-mode highlights, multi-select, scoping,
and the pick/insert flows is deferred to phase 2 — phase 1 keeps the
dispatcher minimal and explicit.
"""

import os
import signal
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class Action:
    """A keybinding: key string -> handler.

    Fields:
      * ``key``: key name as produced by ``020-terminal.read_key`` —
        e.g. ``'e'``, ``'ctrl-r'``, ``'alt-down'``, ``'shift-enter'``.
      * ``label``: short text used in help screens and the info-bar
        hints in phase 2.
      * ``handler``: callable taking a single ``Context`` argument; run
        when the key is pressed in normal mode (i.e. not in search-mode
        text-entry).
      * ``requires``: precondition gate. The dispatcher silently skips
        handlers whose precondition is unmet:
          - ``'none'``     — always callable (default).
          - ``'cursor'``   — ``ctx.cursor`` must be non-None.
          - ``'selection'`` — ``ctx.selected`` must be non-empty.
          - ``'targets'`` — either selection or cursor (most common).
    """

    key: str
    label: str = ''
    handler: Optional[Callable] = None
    requires: str = 'none'


# ---- precondition gate -----------------------------------------------------


def _gate_passes(action, ctx) -> bool:
    """Return True iff ``action.requires`` is satisfied for this Context."""
    r = action.requires
    if r == 'none':
        return True
    if r == 'cursor':
        return ctx.cursor is not None
    if r == 'selection':
        return bool(ctx.selected)
    if r == 'targets':
        return bool(ctx.targets)
    # Unknown gate name — treat as no gate so a typo doesn't silently
    # disable a recipe's action.
    return True


# ---- default action handlers ----------------------------------------------
#
# Each handler receives a Context (``ctx``) and pokes Browser state directly
# via ``ctx._browser``. The handlers don't return anything — observable
# effects come through ``_needs_redraw`` flags or state mutation that the
# next render pass picks up.
#
# Page size for pgup/pgdn is hard-coded to 10 for phase 1; ticket #13 wires
# the real list-pane height from ``layout_panes`` once the main loop owns
# the geometry.

_PAGE_ROWS = 10


def _nav_down(ctx):
    """Move cursor one row down (clamped to the visible list end)."""
    state = ctx._browser._state
    vis = visible_items(state)
    if state.cursor < len(vis) - 1:
        state.cursor += 1
        ctx._browser._needs_redraw.add('list')
        ctx._browser._needs_redraw.add('children')
        ctx._browser._needs_redraw.add('preview')


def _nav_up(ctx):
    """Move cursor one row up (clamped to 0)."""
    state = ctx._browser._state
    if state.cursor > 0:
        state.cursor -= 1
        ctx._browser._needs_redraw.add('list')
        ctx._browser._needs_redraw.add('children')
        ctx._browser._needs_redraw.add('preview')


def _nav_home(ctx):
    """Jump cursor to the first row."""
    state = ctx._browser._state
    state.cursor = 0
    ctx._browser._needs_redraw.add('list')
    ctx._browser._needs_redraw.add('children')
    ctx._browser._needs_redraw.add('preview')


def _nav_end(ctx):
    """Jump cursor to the last visible row."""
    state = ctx._browser._state
    vis = visible_items(state)
    state.cursor = max(0, len(vis) - 1)
    ctx._browser._needs_redraw.add('list')
    ctx._browser._needs_redraw.add('children')
    ctx._browser._needs_redraw.add('preview')


def _nav_pgdn(ctx):
    """Move cursor down by a page (clamped)."""
    state = ctx._browser._state
    vis = visible_items(state)
    state.cursor = min(max(0, len(vis) - 1), state.cursor + _PAGE_ROWS)
    ctx._browser._needs_redraw.add('list')
    ctx._browser._needs_redraw.add('children')
    ctx._browser._needs_redraw.add('preview')


def _nav_pgup(ctx):
    """Move cursor up by a page (clamped to 0)."""
    state = ctx._browser._state
    state.cursor = max(0, state.cursor - _PAGE_ROWS)
    ctx._browser._needs_redraw.add('list')
    ctx._browser._needs_redraw.add('children')
    ctx._browser._needs_redraw.add('preview')


def _nav_right(ctx):
    """Expand the cursor item, or step into its first child if already expanded."""
    state = ctx._browser._state
    vis = visible_items(state)
    if not (0 <= state.cursor < len(vis)):
        return
    entry = vis[state.cursor]
    if entry.kind != 'normal':
        return
    item = entry.item
    if getattr(item, 'has_children', False) and item.id not in state.expanded:
        ctx.expand(item.id)
        return
    if (getattr(item, 'has_children', False)
            and state.cursor + 1 < len(vis)
            and vis[state.cursor + 1].depth > entry.depth):
        # Already expanded; step onto the first child if it follows.
        state.cursor += 1
        ctx._browser._needs_redraw.add('list')
        ctx._browser._needs_redraw.add('children')
        ctx._browser._needs_redraw.add('preview')


def _nav_left(ctx):
    """Collapse the cursor item, or jump back to its parent."""
    state = ctx._browser._state
    vis = visible_items(state)
    if not (0 <= state.cursor < len(vis)):
        return
    entry = vis[state.cursor]
    if entry.kind != 'normal':
        return
    item = entry.item
    if item.id in state.expanded:
        state.expanded.discard(item.id)
        mark_visible_dirty(state)
        ctx._browser._needs_redraw.add('list')
        ctx._browser._needs_redraw.add('children')
        ctx._browser._needs_redraw.add('preview')
        return
    # Walk back to the first row at a shallower depth — that's the parent.
    cur_depth = entry.depth
    for i in range(state.cursor - 1, -1, -1):
        if vis[i].depth < cur_depth:
            state.cursor = i
            ctx._browser._needs_redraw.add('list')
            ctx._browser._needs_redraw.add('preview')
            return


def _toggle_preview(ctx):
    """Flip ``show_preview``. Forces a full redraw (layout changes)."""
    ctx._browser.show_preview = not ctx._browser.show_preview
    ctx._browser._needs_redraw.add('all')


def _toggle_children_pane(ctx):
    """Flip ``show_children_pane``. Forces a full redraw (layout changes)."""
    ctx._browser.show_children_pane = not ctx._browser.show_children_pane
    ctx._browser._needs_redraw.add('all')


def _toggle_help(ctx):
    """Flip ``_help_mode`` and reset preview scroll."""
    ctx._browser._help_mode = not ctx._browser._help_mode
    ctx._browser._preview_scroll = 0
    ctx._browser._needs_redraw.add('preview')


def _reload(ctx):
    """Trigger a full refresh of the children cache."""
    ctx.refresh()


def _redraw(ctx):
    """Force a full redraw of every pane."""
    ctx._browser._needs_redraw.add('all')


def _quit(ctx):
    """Request the main loop to exit with the cancel exit code (1)."""
    ctx.quit(code=1)


def _suspend(ctx):
    """Raise SIGTSTP on this process so the ``020-terminal`` handler runs.

    The terminal layer enters raw mode via ``tty.setraw`` which clears
    ISIG, so the kernel no longer translates the keyboard ``\\x1a`` byte
    into SIGTSTP. ``read_key`` therefore surfaces it as the ``ctrl-z``
    key name, and we route it back through the signal so the existing
    handler (``_handle_sigtstp``) can restore the terminal, drop the
    process to the shell, and re-enter raw mode on SIGCONT.
    """
    os.kill(os.getpid(), signal.SIGTSTP)


def _scope_down(ctx):
    """Drill into the cursor item — push it onto ``scope_stack``.

    Branches only: leaves are silently ignored (phase-2 simplification —
    there's nothing to descend into). The new scope's expanded set is
    restored from ``_expanded_by_scope`` automatically by ``scope_into``;
    if its children weren't cached we kick a fetch so the placeholder
    row resolves promptly.
    """
    state = ctx._browser._state
    item = ctx.cursor
    if item is None:
        return
    if not getattr(item, 'has_children', False):
        return  # don't scope into leaves; phase-2 simplification
    scope_into(state, item.id)
    # Land the cursor on the scope_root row at the top of the new view.
    state.cursor = 0
    if item.id not in state._children:
        # Trigger a fetch — ``expand`` is the cheapest entry point;
        # adds id to expanded and enqueues the children fetch. The
        # scope_root row is rendered regardless; the placeholder under
        # it shows ``loading…`` until results land.
        ctx.expand(item.id)
    ctx._browser._needs_redraw.add('all')


def _scope_up(ctx):
    """Pop the top of ``scope_stack``; cursor lands on the popped id.

    No-op when already at the root (``scope_stack`` empty). After
    popping, walks the new visible tree to find the id we just left
    and parks the cursor on it — so the user feels "I came back from
    there" rather than landing arbitrarily.
    """
    state = ctx._browser._state
    popped = scope_out(state)
    if popped is None:
        return  # already at root
    # Place cursor on the row we drilled into earlier.
    vis = visible_items(state)
    placed = False
    for i, entry in enumerate(vis):
        if entry.kind == 'normal' and entry.item.id == popped:
            state.cursor = i
            placed = True
            break
    if not placed:
        # Fallback: clamp to a sensible position.
        state.cursor = 0
    ctx._browser._needs_redraw.add('all')


def _select_toggle_down(ctx):
    """Toggle selection of the cursor item, then move the cursor down.

    The dispatcher gates this on ``requires='cursor'`` so we can assume
    a normal-row cursor when called. After flipping membership in
    ``state.selected`` we nudge the cursor like ``_nav_down`` does — the
    common workflow in plan-tui is hold-space to mark a run of rows.
    """
    state = ctx._browser._state
    item = ctx.cursor
    if item is None:
        return
    if item.id in state.selected:
        state.selected.discard(item.id)
    else:
        state.selected.add(item.id)
    vis = visible_items(state)
    if state.cursor < len(vis) - 1:
        state.cursor += 1
    ctx._browser._needs_redraw.add('list')
    ctx._browser._needs_redraw.add('info')


def _select_toggle_up(ctx):
    """Toggle selection of the cursor item, then move the cursor up.

    Symmetric to ``_select_toggle_down``; bound to ``alt-space`` so the
    user can sweep selections in either direction without releasing the
    modifier.
    """
    state = ctx._browser._state
    item = ctx.cursor
    if item is None:
        return
    if item.id in state.selected:
        state.selected.discard(item.id)
    else:
        state.selected.add(item.id)
    if state.cursor > 0:
        state.cursor -= 1
    ctx._browser._needs_redraw.add('list')
    ctx._browser._needs_redraw.add('info')


def _select_all_visible(ctx):
    """Add every ``kind='normal'`` row in the visible list to the selection.

    Placeholder rows (``kind='pending'``) and the synthetic scope-root row
    are skipped — selecting a placeholder would smuggle the sentinel id
    into the selection set and confuse downstream consumers.
    """
    state = ctx._browser._state
    vis = visible_items(state)
    for entry in vis:
        if entry.kind == 'normal':
            state.selected.add(entry.item.id)
    ctx._browser._needs_redraw.add('list')
    ctx._browser._needs_redraw.add('info')


def _select_clear(ctx):
    """Empty the selection set. No-op on an already-empty selection."""
    state = ctx._browser._state
    if state.selected:
        state.selected.clear()
        ctx._browser._needs_redraw.add('list')
        ctx._browser._needs_redraw.add('info')


def _search_start(ctx):
    """Enter search-text-entry mode."""
    ctx._browser._search_mode = True
    ctx._browser._search_query = ''
    ctx._browser._needs_redraw.add('info')


def _search_next(ctx):
    """Search-next is a phase-1 stub; ticket #22 wires fragment matching."""
    # Intentionally a no-op in phase 1. The dispatcher only invokes us
    # when ``_search_mode`` is True (after enter is pressed); in phase 2
    # this will jump the cursor to the next match.
    pass


def _search_prev(ctx):
    """Search-previous is a phase-1 stub; ticket #22 wires fragment matching."""
    pass


# ---- default keybindings list ---------------------------------------------


def default_actions():
    """Return the list of default Action objects (built-in keybindings).

    Returned fresh on each call so tests can mutate the list without
    poisoning subsequent callers (the cost is one tiny list construction
    per dispatch — negligible).
    """
    return [
        Action('j',         'Down',           _nav_down,        'none'),
        Action('down',      'Down',           _nav_down,        'none'),
        Action('k',         'Up',             _nav_up,          'none'),
        Action('up',        'Up',             _nav_up,          'none'),
        Action('g',         'First',          _nav_home,        'none'),
        Action('home',      'First',          _nav_home,        'none'),
        Action('G',         'Last',           _nav_end,         'none'),
        Action('end',       'Last',           _nav_end,         'none'),
        Action('pgdn',      'Page down',      _nav_pgdn,        'none'),
        Action('pgup',      'Page up',        _nav_pgup,        'none'),
        Action('right',     'Expand',         _nav_right,       'cursor'),
        Action('left',      'Collapse',       _nav_left,        'cursor'),
        # Scoping. alt-down requires a cursor (and silently no-ops on
        # leaves inside the handler); alt-up is gated 'none' because the
        # root-state no-op is also handled inside the handler.
        Action('alt-down',  'Scope in',       _scope_down,      'cursor'),
        Action('alt-up',    'Scope out',      _scope_up,        'none'),
        # Multi-select bindings. ``read_key`` returns ``'space'`` for the
        # bare spacebar (special-cased in 020-terminal) but Alt+Space
        # arrives as ESC + ' ' which the alt-prefix branch turns into the
        # literal string ``'alt- '`` (alt-prefix + space character). Bind
        # accordingly.
        Action('space',     'Toggle select',  _select_toggle_down, 'cursor'),
        Action('alt- ',     'Toggle select up', _select_toggle_up, 'cursor'),
        Action('ctrl-a',    'Select all',     _select_all_visible, 'none'),
        Action('ctrl-n',    'Deselect all',   _select_clear,       'none'),
        Action('ctrl-p',    'Toggle preview', _toggle_preview,  'none'),
        Action('ctrl-r',    'Reload',         _reload,          'none'),
        Action('ctrl-l',    'Redraw',         _redraw,          'none'),
        Action('?',         'Help',           _toggle_help,     'none'),
        Action('f1',        'Help',           _toggle_help,     'none'),
        Action('/',         'Search',         _search_start,    'none'),
        Action('q',         'Quit',           _quit,            'none'),
        Action('esc',       'Quit',           _quit,            'none'),
        Action('ctrl-c',    'Quit',           _quit,            'none'),
        Action('ctrl-z',    'Suspend',        _suspend,         'none'),
    ]


# ---- dispatch --------------------------------------------------------------


def build_keymap(browser):
    """Return ``dict[key, Action]`` — defaults overridden by ``browser.actions``.

    Custom user actions take precedence over defaults: if a recipe binds
    ``q`` to a custom handler, that wins over the default quit. We use a
    plain dict (last write wins) rather than chained lookups so the
    dispatch path stays O(1) per keypress.
    """
    keymap = {}
    for a in default_actions():
        keymap[a.key] = a
    if browser.actions:
        for a in browser.actions:
            keymap[a.key] = a
    return keymap


def dispatch_key(browser, ctx, key) -> bool:
    """Dispatch ``key`` to the matching action; return True if handled.

    Search-mode (``browser._search_mode is True``) intercepts most keys:
    ``esc`` exits search, ``enter`` and ``shift-enter`` jump between
    matches (stubs in phase 1), ``backspace`` trims the query, and
    printable characters extend it. Other keys fall through to normal
    dispatch only when not in search mode.

    Outside search mode: ``enter`` runs the on_enter handler (print-exit,
    action-redirect, noop, or callable) and other keys run their bound
    Action's handler if its ``requires`` precondition is met.

    Returns ``True`` if the key was handled, ``False`` otherwise — the
    caller (main loop in #13) uses the return to decide whether to log
    the key or pass it through to a fallback.
    """
    # Search-mode special handling — phase 1 keeps it minimal.
    if browser._search_mode:
        if key == 'esc':
            browser._search_mode = False
            browser._search_query = ''
            browser._needs_redraw.add('info')
            return True
        if key == 'enter':
            _search_next(ctx)
            return True
        if key == 'shift-enter':
            _search_prev(ctx)
            return True
        if key == 'backspace':
            browser._search_query = browser._search_query[:-1]
            browser._needs_redraw.add('info')
            return True
        # Single printable characters extend the query. ``space`` is the
        # special name read_key returns for the spacebar; treat it like a
        # literal space when typing a search query.
        if key == 'space':
            browser._search_query += ' '
            browser._needs_redraw.add('info')
            return True
        if len(key) == 1 and key.isprintable():
            browser._search_query += key
            browser._needs_redraw.add('info')
            return True
        # Other keys (arrows, ctrl-* etc.) are ignored while typing a
        # search query — phase 2 may wire some of them (e.g. up/down to
        # navigate match results) but phase 1 is conservative.
        return False

    # Enter handling — outside search mode this falls to on_enter.
    if key == 'enter':
        return _handle_enter(browser, ctx)

    keymap = build_keymap(browser)
    if key in keymap:
        action = keymap[key]
        if _gate_passes(action, ctx):
            if action.handler is not None:
                action.handler(ctx)
            return True
        # Gated out — silently swallow so the caller doesn't double-log.
        return True
    return False


def _handle_enter(browser, ctx) -> bool:
    """Implement ``on_enter`` semantics: print-exit | action:KEY | noop | callable.

    * ``None`` / ``'print-exit'``: format ``ctx.targets`` via
      ``browser.print_format`` (one line each), stash the joined string
      in ``_quit_output``, exit code 0. The actual stdout flush happens
      in the main loop in #13.
    * ``'action:KEY'``: look up that key in the keymap and invoke its
      handler if its gate passes.
    * ``'noop'``: do nothing (long-running mode).
    * any callable: invoke directly with ``ctx`` (recipes can define
      arbitrary enter behaviour without registering an Action).
    """
    on_enter = browser.on_enter
    if on_enter is None or on_enter == 'print-exit':
        targets = ctx.targets
        if not targets:
            return True
        lines = []
        for it in targets:
            try:
                lines.append(browser.print_format.format_map(_item_attrs(it)))
            except (KeyError, ValueError):
                lines.append(str(it.id))
        ctx.quit(code=0, output='\n'.join(lines) + '\n')
        return True
    if isinstance(on_enter, str) and on_enter.startswith('action:'):
        target_key = on_enter[len('action:'):]
        keymap = build_keymap(browser)
        action = keymap.get(target_key)
        if action and _gate_passes(action, ctx) and action.handler is not None:
            action.handler(ctx)
        return True
    if on_enter == 'noop':
        return True
    if callable(on_enter):
        on_enter(ctx)
        return True
    return False


def _item_attrs(item):
    """Best-effort attribute dict for ``str.format_map`` on an Item.

    Includes the dataclass fields and any extra attributes the recipe
    attached. Falls back gracefully on unreadable attributes (descriptors
    that raise) — those are simply omitted.
    """
    d = {}
    for name in ('id', 'title', 'tag', 'tag_style', 'has_children'):
        d[name] = getattr(item, name, '')
    for name in dir(item):
        if name.startswith('_'):
            continue
        if name in d:
            continue
        try:
            d[name] = getattr(item, name)
        except Exception:
            pass
    return d
