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
      * ``label``: Short, one-line description of what this action
        does. Shown in the auto-generated CUSTOM ACTIONS section of
        ``--help`` and ``?``. Keep under ~60 characters; use
        ``help_outro`` for anything longer.
      * ``handler``: callable taking a single ``Context`` argument; run
        when the key is pressed in normal mode (i.e. not in search-mode
        text-entry).
      * ``requires``: precondition gate. The dispatcher silently skips
        handlers whose precondition is unmet:
          - ``'none'``      — always callable (default).
          - ``'cursor'``    — ``ctx.cursor`` must be non-None.
          - ``'selection'`` — ``ctx.selected`` must be non-empty.
          - ``'targets'``   — either selection or cursor is non-empty.
      * ``section``: optional grouping hint used by the help composer.
        Built-in default actions tag themselves with one of
        ``'NAVIGATION'`` / ``'PREVIEW'`` / ``'SEARCH'`` / ``'SELECTION'``
        / ``'OTHER'``. Recipe-supplied actions leave it empty (the
        default) and are listed together under ``CUSTOM ACTIONS``;
        per-recipe sectioning is a future feature.

    An unknown ``requires`` value behaves like ``'none'`` so a typo
    doesn't silently disable the action.
    """

    key: str
    label: str = ''
    handler: Optional[Callable[['Context'], None]] = None
    requires: str = 'none'
    section: str = ''


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
# Page size for pgup/pgdn comes from ``_list_pane_height`` /
# ``_preview_pane_height`` — both query ``term_size`` + ``layout_panes`` so
# the jump matches the visible viewport. Headless / unwired contexts (where
# ``term_size`` raises) fall back to ``_DEFAULT_PAGE_ROWS`` so the handlers
# stay usable in tests and pipelines.

_DEFAULT_PAGE_ROWS = 20


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
    """Move cursor down by a page (clamped).

    Page size = list-pane height from ``layout_panes`` so the jump
    matches the viewport. Falls back to ``_DEFAULT_PAGE_ROWS`` headless.
    """
    browser = ctx._browser
    state = browser._state
    vis = visible_items(state)
    page = _list_pane_height(browser)
    state.cursor = min(max(0, len(vis) - 1), state.cursor + page)
    browser._needs_redraw.add('list')
    browser._needs_redraw.add('children')
    browser._needs_redraw.add('preview')


def _nav_pgup(ctx):
    """Move cursor up by a page (clamped to 0).

    Page size = list-pane height; same source as ``_nav_pgdn``.
    """
    browser = ctx._browser
    state = browser._state
    page = _list_pane_height(browser)
    state.cursor = max(0, state.cursor - page)
    browser._needs_redraw.add('list')
    browser._needs_redraw.add('children')
    browser._needs_redraw.add('preview')


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
    """Jump cursor to the next match (forward, wrap-around).

    Bound to ``enter`` while ``_search_mode`` is True. ``_search_find``
    starts the walk *after* the current cursor, so repeated presses
    advance through every match in turn before wrapping back to the
    first.
    """
    browser = ctx._browser
    state = browser._state
    idx = _search_find(state, browser._search_query, state.cursor, 1)
    if idx is not None:
        state.cursor = idx
        browser._needs_redraw.add('list')
        browser._needs_redraw.add('preview')
        browser._needs_redraw.add('children')


def _search_prev(ctx):
    """Jump cursor to the previous match (backward, wrap-around).

    Symmetric counterpart to ``_search_next`` — bound to ``shift-enter``
    while ``_search_mode`` is True.
    """
    browser = ctx._browser
    state = browser._state
    idx = _search_find(state, browser._search_query, state.cursor, -1)
    if idx is not None:
        state.cursor = idx
        browser._needs_redraw.add('list')
        browser._needs_redraw.add('preview')
        browser._needs_redraw.add('children')


# ---- recursive expand/collapse + preview scroll ---------------------------
#
# The four "missing" plan-tui keybindings (alt-right/alt-left expand-or-
# collapse-siblings-recursively; shift-up/shift-down + alt-pgup/alt-pgdn
# scroll the preview pane) are core navigation aids — every recipe that
# shows a tree or a preview will want them. They live as built-in
# defaults so recipes can lean on them without re-implementing the
# tree-walk every time.


def _expand_recursive(ctx):
    """alt-right — expand siblings (and their descendants) at cursor depth.

    The "siblings" here are the children of the cursor's parent: we walk
    back through the visible list to find the first row at a shallower
    depth (that's the parent), then recursively expand every cached
    descendant of that parent that has children. For uncached branches
    we kick a fetch via ``ctx.expand`` so they resolve on the next
    drain.

    No-op when the cursor row isn't a normal item (placeholder /
    scope_root / empty list).
    """
    state = ctx._browser._state
    if ctx.cursor is None:
        return
    vis = visible_items(state)
    cur_idx = state.cursor
    if not (0 <= cur_idx < len(vis)):
        return
    cur_entry = vis[cur_idx]
    if cur_entry.kind != 'normal':
        return
    cur_depth = cur_entry.depth
    # Walk back to the parent row (first shallower depth) — fall back to
    # the current scope root when we don't find one (cursor at root).
    parent_id = current_scope(state)
    for i in range(cur_idx - 1, -1, -1):
        if vis[i].depth < cur_depth:
            if vis[i].kind == 'normal':
                parent_id = vis[i].item.id
            break

    def _expand_subtree(pid):
        children = state._children.get(pid)
        if children is None:
            # Not cached — kick a fetch; the placeholder row resolves
            # asynchronously and the user can press alt-right again to
            # drill deeper once it lands.
            ctx.expand(pid)
            return
        for c in children:
            if getattr(c, 'has_children', False):
                state.expanded.add(c.id)
                _expand_subtree(c.id)

    _expand_subtree(parent_id)
    mark_visible_dirty(state)
    ctx._browser._needs_redraw.add('all')


def _collapse_recursive(ctx):
    """alt-left — collapse siblings (and their descendants) at cursor depth.

    Mirror of ``_expand_recursive``: find the parent row, then drop
    every cached descendant of that parent from ``state.expanded``.
    """
    state = ctx._browser._state
    if ctx.cursor is None:
        return
    vis = visible_items(state)
    cur_idx = state.cursor
    if not (0 <= cur_idx < len(vis)):
        return
    cur_entry = vis[cur_idx]
    if cur_entry.kind != 'normal':
        return
    cur_depth = cur_entry.depth
    parent_id = current_scope(state)
    for i in range(cur_idx - 1, -1, -1):
        if vis[i].depth < cur_depth:
            if vis[i].kind == 'normal':
                parent_id = vis[i].item.id
            break

    def _collect_descendants(pid, out):
        children = state._children.get(pid, [])
        for c in children:
            out.add(c.id)
            _collect_descendants(c.id, out)

    descendants = set()
    _collect_descendants(parent_id, descendants)
    if descendants:
        state.expanded -= descendants
        mark_visible_dirty(state)
        ctx._browser._needs_redraw.add('all')


def _preview_scroll_down(ctx):
    """shift-down — scroll preview by one line.

    The high-end clamp is enforced by ``render_preview`` (it skips any
    src_idx beyond the wrapped-line count). We just bump the offset and
    let the renderer ignore it gracefully when off-end.
    """
    ctx._browser._preview_scroll += 1
    ctx._browser._needs_redraw.add('preview')


def _preview_scroll_up(ctx):
    """shift-up — scroll preview up by one line (clamped at 0)."""
    if ctx._browser._preview_scroll > 0:
        ctx._browser._preview_scroll -= 1
        ctx._browser._needs_redraw.add('preview')


def _preview_page_down(ctx):
    """alt-pgdn — scroll preview down by one preview-pane page.

    Same off-end semantics as ``_preview_scroll_down``: the renderer
    gracefully ignores out-of-range offsets so we don't need to thread
    layout geometry through every action.

    Page size = preview-pane height from ``layout_panes`` (so the jump
    matches the visible viewport). Headless contexts fall back to
    ``_DEFAULT_PAGE_ROWS``.
    """
    page = _preview_pane_height(ctx._browser)
    ctx._browser._preview_scroll += page
    ctx._browser._needs_redraw.add('preview')


def _preview_page_up(ctx):
    """alt-pgup — scroll preview up by one preview-pane page (clamped at 0)."""
    page = _preview_pane_height(ctx._browser)
    ctx._browser._preview_scroll = max(
        0, ctx._browser._preview_scroll - page
    )
    ctx._browser._needs_redraw.add('preview')


# ---- default keybindings list ---------------------------------------------


def default_actions() -> list:
    """Return the list of default Action objects (built-in keybindings).

    Returned fresh on each call so tests can mutate the list without
    poisoning subsequent callers (the cost is one tiny list construction
    per dispatch — negligible).
    """
    return [
        Action('j',         'Cursor down',    _nav_down,        'none', 'NAVIGATION'),
        Action('down',      'Cursor down',    _nav_down,        'none', 'NAVIGATION'),
        Action('k',         'Cursor up',      _nav_up,          'none', 'NAVIGATION'),
        Action('up',        'Cursor up',      _nav_up,          'none', 'NAVIGATION'),
        Action('g',         'First item',     _nav_home,        'none', 'NAVIGATION'),
        Action('home',      'First item',     _nav_home,        'none', 'NAVIGATION'),
        Action('G',         'Last item',      _nav_end,         'none', 'NAVIGATION'),
        Action('end',       'Last item',      _nav_end,         'none', 'NAVIGATION'),
        Action('pgdn',      'Page down',      _nav_pgdn,        'none', 'NAVIGATION'),
        Action('pgup',      'Page up',        _nav_pgup,        'none', 'NAVIGATION'),
        Action('right',     'Expand node / move to first child', _nav_right, 'cursor', 'NAVIGATION'),
        Action('left',      'Collapse node / move to parent',    _nav_left,  'cursor', 'NAVIGATION'),
        # Recursive expand/collapse — gate on 'cursor' so we don't run the
        # tree walk on an empty visible list.
        Action('alt-right', 'Expand siblings recursively',   _expand_recursive,   'cursor', 'NAVIGATION'),
        Action('alt-left',  'Collapse siblings recursively', _collapse_recursive, 'cursor', 'NAVIGATION'),
        # Scoping. alt-down requires a cursor (and silently no-ops on
        # leaves inside the handler); alt-up is gated 'none' because the
        # root-state no-op is also handled inside the handler.
        Action('alt-down',  'Scope down into item', _scope_down, 'cursor', 'NAVIGATION'),
        Action('alt-up',    'Scope up to parent',   _scope_up,   'none',   'NAVIGATION'),
        # Preview scroll — gate 'none' so they work even when the visible
        # list is empty (help/error pages still want scrolling).
        Action('ctrl-p',     'Toggle preview pane',     _toggle_preview,      'none', 'PREVIEW'),
        Action('shift-down', 'Scroll preview line down', _preview_scroll_down, 'none', 'PREVIEW'),
        Action('shift-up',   'Scroll preview line up',   _preview_scroll_up,   'none', 'PREVIEW'),
        Action('alt-pgdn',   'Scroll preview page down', _preview_page_down,   'none', 'PREVIEW'),
        Action('alt-pgup',   'Scroll preview page up',   _preview_page_up,     'none', 'PREVIEW'),
        # Search.
        Action('/',         'Enter search mode', _search_start, 'none', 'SEARCH'),
        # Multi-select bindings. ``read_key`` returns ``'space'`` for the
        # bare spacebar (special-cased in 020-terminal) but Alt+Space
        # arrives as ESC + ' ' which the alt-prefix branch turns into the
        # literal string ``'alt- '`` (alt-prefix + space character). Bind
        # accordingly.
        Action('space',     'Toggle select (cursor down)', _select_toggle_down, 'cursor', 'SELECTION'),
        Action('alt- ',     'Toggle select (cursor up)',   _select_toggle_up,   'cursor', 'SELECTION'),
        Action('ctrl-a',    'Select all visible',          _select_all_visible, 'none',   'SELECTION'),
        Action('ctrl-n',    'Deselect all',                _select_clear,       'none',   'SELECTION'),
        # Other.
        Action('?',         'Toggle help',    _toggle_help, 'none', 'OTHER'),
        Action('f1',        'Toggle help',    _toggle_help, 'none', 'OTHER'),
        Action('ctrl-r',    'Reload',         _reload,      'none', 'OTHER'),
        Action('ctrl-l',    'Redraw',         _redraw,      'none', 'OTHER'),
        Action('q',         'Quit',           _quit,        'none', 'OTHER'),
        Action('esc',       'Quit',           _quit,        'none', 'OTHER'),
        Action('ctrl-c',    'Quit',           _quit,        'none', 'OTHER'),
        Action('ctrl-z',    'Suspend',        _suspend,     'none', 'OTHER'),
    ]


# ---- dispatch --------------------------------------------------------------


def build_keymap(browser) -> dict:
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


def dispatch_key(browser, ctx: 'Context', key: str) -> bool:
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
    # Mouse events (clicks + wheel) are dispatched first when not in
    # search mode. ``_dispatch_mouse`` parses the row/col, looks up the
    # pane via ``layout_panes``, and applies the per-pane behaviour.
    # Search mode silently swallows mouse events along with other
    # unhandled keys (the multi-char ``mouse-click:R:C`` form bypasses
    # the printable-char branch of the search-mode handler).
    if not browser._search_mode and (
            key.startswith('mouse-click:')
            or key.startswith('scroll-up:')
            or key.startswith('scroll-down:')):
        return _dispatch_mouse(browser, ctx, key)

    # Search-mode special handling.
    #
    # Esc clears the query and exits search mode (matches plan-tui — the
    # query is *not* preserved across exits; the cursor stays put on the
    # last match so the user keeps the row they searched up).
    #
    # Each keystroke that mutates the query also calls ``_search_jump_nearest``
    # to nudge the cursor onto the nearest match in real time (so the
    # user sees results filter under the cursor as they type), and adds
    # ``'list'`` to the redraw set so highlight spans repaint
    # immediately.
    if browser._search_mode:
        if key in ('esc', 'ctrl-c'):
            browser._search_mode = False
            browser._search_query = ''
            browser._needs_redraw.add('info')
            browser._needs_redraw.add('list')
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
            browser._needs_redraw.add('list')
            if browser._search_query:
                _search_jump_nearest(browser)
            return True
        # Single printable characters extend the query. ``space`` is the
        # special name read_key returns for the spacebar; treat it like a
        # literal space when typing a search query.
        if key == 'space':
            browser._search_query += ' '
            browser._needs_redraw.add('info')
            browser._needs_redraw.add('list')
            _search_jump_nearest(browser)
            return True
        if len(key) == 1 and key.isprintable():
            browser._search_query += key
            browser._needs_redraw.add('info')
            browser._needs_redraw.add('list')
            _search_jump_nearest(browser)
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


def _handle_insert_key(browser, ctx: 'Context', key: str) -> bool:
    """Dispatch one keypress while ``browser._insert_mode`` is True.

    Mirrors plan-tui's ``_handle_insert_key`` (plan-source/src-tui/
    070-main.py:98-249). The marker moves through the visible list with
    j/k/up/down, jumps with home/end/pgup/pgdn, indents with right,
    outdents with left, confirms with enter, cancels with esc/ctrl-c/q.

    Returns ``True`` always (the insert-mode loop swallows every key —
    even unhandled ones — so the regular dispatcher doesn't see them).

    plan-tui's version forwards unhandled keys back to the normal-mode
    handler so e.g. ``alt-down`` (scope-in) still works during insert
    mode. browse-tui takes the conservative path: ignore unhandled keys
    inside insert mode. Recipes that want richer behaviour can extend
    via the standard action surface once they exit insert mode.
    """
    state = browser._state
    vis = visible_items(state)
    max_pos = len(vis)
    # min_pos = 1 always — gap 0 sits above the first row, which is the
    # scope_root row when scoped (and a regular row otherwise). Either
    # way the marker starts at gap 1 and never goes lower.
    min_pos = 1

    def _set_pos(new_pos):
        # Clamp + auto-depth + flag list redraw. Used by every movement
        # key. Captures `vis` from the enclosing scope so a single call
        # site keeps the contract single-sourced.
        if new_pos < min_pos:
            new_pos = min_pos
        if new_pos > max_pos:
            new_pos = max_pos
        browser._insert_pos = new_pos
        browser._insert_depth = auto_insert_depth(new_pos, vis)
        browser._needs_redraw.add('list')

    if key in ('j', 'down'):
        if browser._insert_pos < max_pos:
            _set_pos(browser._insert_pos + 1)
        return True
    if key in ('k', 'up'):
        if browser._insert_pos > min_pos:
            _set_pos(browser._insert_pos - 1)
        return True
    if key in ('g', 'home'):
        _set_pos(min_pos)
        return True
    if key in ('G', 'end'):
        _set_pos(max_pos)
        return True
    if key == 'pgdn':
        # Page size = list-pane height; fall back to a sensible default
        # in headless contexts where the layout helpers aren't wired.
        page = _list_pane_height(browser)
        _set_pos(browser._insert_pos + page)
        return True
    if key == 'pgup':
        page = _list_pane_height(browser)
        _set_pos(browser._insert_pos - page)
        return True
    if key == 'right':
        # Indent: make the marker a child of the row above. If that row
        # has not-yet-expanded children, expand them first so they
        # become visible to the marker walk.
        pos = browser._insert_pos
        if 0 < pos <= len(vis):
            above = vis[pos - 1]
            if (above.kind == 'normal'
                    and browser._insert_depth <= above.depth):
                if (above.item.has_children
                        and above.item.id not in state.expanded):
                    state.expanded.add(above.item.id)
                    mark_visible_dirty(state)
                    # Refresh the visible list and find above's new index;
                    # position the marker right after it so it lands at
                    # the start of the now-revealed subtree.
                    new_vis = visible_items(state)
                    for idx, e in enumerate(new_vis):
                        if (e.kind == 'normal'
                                and e.item.id == above.item.id):
                            browser._insert_pos = idx + 1
                            break
                    browser._insert_depth = above.depth + 1
                    browser._needs_redraw.add('all')
                    return True
                browser._insert_depth = above.depth + 1
                browser._needs_redraw.add('all')
        return True
    if key == 'left':
        # First case: the row directly *below* the marker is at the
        # marker's depth, has children, and is expanded → collapse it
        # (mirrors nav-mode left-collapses-current-row).
        pos = browser._insert_pos
        depth = browser._insert_depth
        if pos < len(vis):
            after = vis[pos]
            if (after.kind == 'normal'
                    and after.depth == depth
                    and after.item.has_children
                    and after.item.id in state.expanded):
                state.expanded.discard(after.item.id)
                mark_visible_dirty(state)
                # Re-derive the visible list size — collapsing may have
                # cut rows below us; clamp insert_pos.
                new_vis = visible_items(state)
                if browser._insert_pos > len(new_vis):
                    browser._insert_pos = max(min_pos, len(new_vis))
                browser._needs_redraw.add('all')
                return True
        # Second case: outdent and reposition before the parent. We use
        # base = base depth of the current scope. visible_items emits
        # the scope_root at depth 0 when scoped, so children start at
        # depth 1 → base = 1; otherwise base = 0.
        base = 1 if state.scope_stack else 0
        if depth > base:
            new_depth = depth - 1
            # Find the parent row at new_depth by walking back.
            parent_idx = None
            for p in range(pos - 1, -1, -1):
                if vis[p].depth == new_depth:
                    parent_idx = p
                    break
                if vis[p].depth < new_depth:
                    break
            if parent_idx is not None:
                # Already after all children of the parent? If so, slide
                # the marker to after the entire subtree (otherwise jump
                # before the parent row).
                after_last_child = True
                for s in range(pos, len(vis)):
                    if vis[s].depth <= new_depth:
                        break
                    if vis[s].depth == depth:
                        after_last_child = False
                        break
                if after_last_child:
                    sub_end = parent_idx + 1
                    for s in range(parent_idx + 1, len(vis)):
                        if vis[s].depth > new_depth:
                            sub_end = s + 1
                        else:
                            break
                    browser._insert_pos = sub_end
                else:
                    browser._insert_pos = parent_idx
            browser._insert_depth = new_depth
            browser._needs_redraw.add('list')
        return True
    if key == 'enter':
        relation, dest_id = resolve_insert(
            browser._insert_pos, browser._insert_depth, vis,
            scope_root_id=current_scope(state),
        )
        cb = browser._insert_callback
        browser._insert_mode = False
        browser._insert_callback = None
        browser._needs_redraw.add('all')
        if relation is not None and dest_id is not None and cb is not None:
            cb(relation, dest_id)
        return True
    if key in ('esc', 'ctrl-c', 'q'):
        browser._insert_mode = False
        browser._insert_callback = None
        browser._needs_redraw.add('all')
        return True
    if key == '_notify':
        browser.drain_main_queue()
        browser.apply_children_results()
        browser.apply_preview_result()
        return True
    # Unhandled — swallow silently. plan-tui forwards to the normal-mode
    # handler here; phase-2 simplification is to ignore. Recipes that
    # want extended insert-mode bindings can layer them in a later phase.
    return True


def _list_pane_height(browser):
    """Compute the list-pane height for pgup/pgdn jumps.

    Headless / unwired test contexts fall back to ``_DEFAULT_PAGE_ROWS``
    (``term_size`` raises ``OSError`` when stdout isn't a tty, and the
    cross-module name may not be wired in unit tests). In production we
    read the geometry from ``layout_panes`` so the page jump matches the
    visible viewport exactly.
    """
    try:
        cols, rows = term_size()
        layout = layout_panes(
            cols, rows,
            show_preview=browser.show_preview,
            show_children_pane=browser.show_children_pane,
        )
        h = layout['list_height']
        return h if h > 0 else _DEFAULT_PAGE_ROWS
    except Exception:
        return _DEFAULT_PAGE_ROWS


def _preview_pane_height(browser):
    """Compute the preview-pane content height for alt-pgup/pgdn.

    ``layout_panes`` returns ``prev_height`` *including* the separator
    row, so the actual scrollable content area is ``prev_height - 1``.
    Headless / unwired contexts (and configurations where the preview
    pane is hidden) fall back to ``_DEFAULT_PAGE_ROWS`` — the renderer
    clamps off-end offsets gracefully so the alt-pgdn key still feels
    responsive even when no preview is showing.
    """
    try:
        cols, rows = term_size()
        layout = layout_panes(
            cols, rows,
            show_preview=browser.show_preview,
            show_children_pane=browser.show_children_pane,
        )
        h = layout['prev_height'] - 1  # exclude separator row
        return h if h > 0 else _DEFAULT_PAGE_ROWS
    except Exception:
        return _DEFAULT_PAGE_ROWS


# ---- mouse dispatch -------------------------------------------------------
#
# read_key() (020-terminal.py) decodes SGR mouse sequences to:
#
#   * 'mouse-click:R:C' — left-button press at 1-based (row, col)
#   * 'scroll-up:R:C'   — wheel notch up
#   * 'scroll-down:R:C' — wheel notch down
#
# We dispatch these to the pane under (R, C):
#
#   * Click on list pane    → set state.cursor to that row.
#   * Click on preview pane → dismiss help mode if active, else no-op.
#   * Click elsewhere       → no-op.
#   * Wheel on list pane    → ±_WHEEL_LINES on _list_scroll. Cursor
#                             unchanged (decoupled from the viewport).
#   * Wheel on preview pane → ±_WHEEL_LINES on _preview_scroll.
#   * Wheel elsewhere       → no-op.
#
# In modal modes we do nothing: search-mode swallows all unhandled keys,
# insert-mode runs in _handle_insert_key (and silently ignores what it
# doesn't recognize), pickers in _pick_on_info_bar swallow unknowns. So
# this dispatcher only fires from the normal-mode branch of dispatch_key.

_WHEEL_LINES = 3


def _dispatch_mouse(browser, ctx, key):
    """Route a mouse event ``key`` to the pane it lands on.

    Always returns ``True`` (the event has been consumed). Malformed
    payloads, or contexts where the pane geometry can't be resolved
    (headless tests without ``term_size``/``layout_panes`` injected),
    silently no-op rather than raising.
    """
    parts = key.split(':')
    if len(parts) != 3:
        return True
    kind = parts[0]
    try:
        row = int(parts[1])
    except ValueError:
        return True

    ts = globals().get('term_size')
    lp = globals().get('layout_panes')
    if ts is None or lp is None:
        return True
    try:
        cols, rows_total = ts()
    except Exception:
        return True
    layout = lp(
        cols, rows_total,
        show_preview=browser.show_preview,
        show_children_pane=browser.show_children_pane,
    )
    pane = _pane_at(layout, row)

    if kind == 'mouse-click':
        if pane == 'list':
            _click_list_row(browser, layout, row)
        elif pane == 'preview':
            if browser._help_mode:
                browser._help_mode = False
                browser._needs_redraw.add('preview')
        # children grid / info row / separator → no-op
        return True
    if kind in ('scroll-up', 'scroll-down'):
        delta = -_WHEEL_LINES if kind == 'scroll-up' else _WHEEL_LINES
        if pane == 'list':
            _scroll_list(browser, delta)
        elif pane == 'preview':
            _scroll_preview(browser, delta)
        return True
    return True


def _pane_at(layout, row):
    """Return ``'list'``, ``'children'``, ``'preview'``, or ``None`` for ``row``.

    The separator rows themselves (info row, preview separator) return
    ``None`` — clicks on dividers are deliberately no-ops.
    """
    list_top = layout['list_top']
    list_height = layout['list_height']
    sub_top = layout['sub_top']
    sub_height = layout['sub_height']
    prev_top = layout['prev_top']
    prev_height = layout['prev_height']

    if list_top <= row < list_top + list_height:
        return 'list'
    if sub_height > 0 and sub_top + 1 <= row < sub_top + sub_height:
        return 'children'
    if prev_height > 0 and prev_top + 1 <= row < prev_top + prev_height:
        return 'preview'
    return None


def _click_list_row(browser, layout, row):
    """Move ``state.cursor`` to the visible-list row at terminal ``row``."""
    state = browser._state
    visible = visible_items(state)
    new_idx = browser._list_scroll + (row - layout['list_top'])
    if 0 <= new_idx < len(visible) and state.cursor != new_idx:
        state.cursor = new_idx
        browser._needs_redraw.add('list')


def _scroll_list(browser, delta):
    """Adjust ``_list_scroll`` by ``delta`` rows. Cursor unchanged."""
    state = browser._state
    visible = visible_items(state)
    soft_max = max(0, len(visible) - 1)
    new_scroll = max(0, min(browser._list_scroll + delta, soft_max))
    if new_scroll != browser._list_scroll:
        browser._list_scroll = new_scroll
        browser._needs_redraw.add('list')


def _scroll_preview(browser, delta):
    """Adjust ``_preview_scroll`` by ``delta`` rows (mirrors shift-up/-down)."""
    new_scroll = max(0, browser._preview_scroll + delta)
    if new_scroll != browser._preview_scroll:
        browser._preview_scroll = new_scroll
        browser._needs_redraw.add('preview')


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
