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
import shlex
import signal
import tempfile
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
        mark_cursor_changed(ctx._browser)


def _nav_up(ctx):
    """Move cursor one row up (clamped to 0)."""
    state = ctx._browser._state
    if state.cursor > 0:
        state.cursor -= 1
        mark_cursor_changed(ctx._browser)


def _nav_home(ctx):
    """Jump cursor to the first row and pin it there.

    The pin (``PIN_FIRST`` sentinel in ``_cursor_anchor``) makes the
    cursor follow new arrivals at the top until any non-home/non-end
    navigation clears it. See
    ``docs/superpowers/specs/2026-05-17-cursor-pin-design.md``.
    """
    b = ctx._browser
    b._state.cursor = 0
    b._cursor_anchor = [PIN_FIRST]
    mark_cursor_changed(b)


def _nav_end(ctx):
    """Jump cursor to the last visible row and pin it there.

    Symmetric to ``_nav_home`` — uses the ``PIN_LAST`` sentinel.
    """
    b = ctx._browser
    vis = visible_items(b._state)
    b._state.cursor = max(0, len(vis) - 1)
    b._cursor_anchor = [PIN_LAST]
    mark_cursor_changed(b)


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
    mark_cursor_changed(browser)


def _nav_pgup(ctx):
    """Move cursor up by a page (clamped to 0).

    Page size = list-pane height; same source as ``_nav_pgdn``.
    """
    browser = ctx._browser
    state = browser._state
    page = _list_pane_height(browser)
    state.cursor = max(0, state.cursor - page)
    mark_cursor_changed(browser)


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
        # User-driven expand → opt in to the scroll-to-fit goal so the
        # newly-revealed subtree slides into view (re-applied as
        # async children stream in). Recipes leave autoscroll=False.
        ctx.expand(item.id, autoscroll=True)
        return
    if (getattr(item, 'has_children', False)
            and state.cursor + 1 < len(vis)
            and vis[state.cursor + 1].depth > entry.depth):
        # Already expanded; step onto the first child if it follows.
        state.cursor += 1
        mark_cursor_changed(ctx._browser)


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
        mark_cursor_changed(ctx._browser)
        return
    # Walk back to the first row at a shallower depth — that's the parent.
    cur_depth = entry.depth
    for i in range(state.cursor - 1, -1, -1):
        if vis[i].depth < cur_depth:
            state.cursor = i
            mark_cursor_changed(ctx._browser)
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
    """Flip ``_help_mode`` and reset preview scroll.

    ``_preview_at_tail`` is preserved — the pin is a sticky user
    intent. When help is toggled off, the renderer's pin override
    re-snaps to the new preview's ``max_scroll`` automatically.
    """
    b = ctx._browser
    b._help_mode = not b._help_mode
    b._preview_scroll = 0
    b._needs_redraw.add('preview')


def _toggle_preview_ansi(ctx):
    """Flip ``preview_ansi`` (capital-R) and flag the preview pane dirty.

    The per-row line cache naturally handles invalidation: rows
    carrying SGR escape sequences emit different bytes when ANSI
    re-emit is suppressed (or re-enabled), so the byte-stream
    comparison in ``end_row`` drives a redraw on those rows; plain
    rows produce identical bytes and stay cache-hit. No explicit
    ``_pane_cache`` surgery is required (#240 design note).
    """
    ctx._browser.preview_ansi = not ctx._browser.preview_ansi
    ctx._browser._needs_redraw.add('preview')


def _reload(ctx):
    """Trigger a full refresh of the children cache."""
    ctx.refresh()


def _redraw(ctx):
    """Force a full redraw of every pane.

    Emits ``\\e[2J`` to clear the screen, drops the per-pane line cache,
    and flags every pane dirty. The next ``render_full`` then takes the
    empty-screen first-paint path: content-only writes with no trailing
    space pads and no ``\\e[K`` clear-to-EOL sequences, since each
    pane's ``prev_rect`` starts as ``None`` and ``end_row`` skips the
    padding/erase work in that case.

    Distinct from the screen-lost recovery in ``020-terminal`` (which
    handles SIGTSTP/SIGCONT) — that path lives at the loop layer; this
    is the user-initiated explicit-redraw action bound to Ctrl-L.
    """
    write('\033[2J')
    ctx._browser._pane_cache.clear()
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


def _view_in_pager(ctx):
    """``v`` — open the cursor item's preview text in ``$PAGER``.

    The pager command comes from ``$PAGER`` (default ``less -R``); the
    string is shell-split so values like ``less -R`` or ``bat --paging=always``
    work without quoting. The preview text is written to a tempfile with
    UTF-8 + ``surrogateescape`` so non-printable bytes round-trip
    faithfully (control characters, lone surrogates from filesystem
    reads, etc. — no question-mark replacements).

    Works whether the preview pane is visible or not: cached entries are
    used as-is; on cache miss the recipe's ``get_preview`` is invoked
    synchronously and the result cached. Silent no-op when the cursor
    is on a placeholder / scope_root row (no item to preview).
    """
    _run_external_on_preview(ctx, env_var='PAGER', default='less -R')


def _edit_in_editor(ctx):
    """``e`` — open the cursor item's preview text in ``$EDITOR``.

    Same temp-file plumbing as :func:`_view_in_pager`. The editor command
    comes from ``$EDITOR`` (default ``vi``).

    **Edits are discarded by default.** Recipes that want to persist
    changes must override ``e`` in their ``actions`` list with a handler
    that writes the buffer back to its data source — there is no
    cross-cutting save hook because the storage model varies per recipe
    (filesystem path, MCP tool call, plan ticket id, …).
    """
    _run_external_on_preview(ctx, env_var='EDITOR', default='vi')


def _run_external_on_preview(ctx, *, env_var, default):
    """Shared body for ``_view_in_pager`` / ``_edit_in_editor``.

    Resolves the preview text for the cursor item, writes it to a
    NamedTemporaryFile (``surrogateescape`` so bytes round-trip), then
    runs ``$<env_var> <tempfile>`` via :meth:`Context.run_external`. The
    tempfile is deleted in a ``finally`` so a crashed editor still
    cleans up.

    Resolution order for the preview text:

      1. ``state._preview[item.id]`` — used as-is when present, including
         the empty string (legitimate empty content).
      2. Synchronous fetch via ``browser.get_preview(item.id)`` — works
         even when the preview pane is hidden or the async worker has
         not delivered yet. The result is cached so a subsequent toggle
         of the preview pane skips the refetch.
      3. If neither is available — ``browser.get_preview`` is ``None``
         or the fetcher returned ``None``/raised — surface a message
         and skip the external command.
    """
    state = ctx._browser._state
    item = ctx.cursor
    if item is None:
        return
    # When the recipe has no preview source, the preview worker fills
    # the cache with '' as a placeholder (see ``_preview_worker`` in
    # 040-state). That placeholder is not meaningful content, so bail
    # before reading the cache rather than opening an empty pager.
    get_preview = getattr(ctx._browser, 'get_preview', None)
    if get_preview is None:
        ctx.message('No preview available')
        return
    text = None
    if hasattr(state, '_preview'):
        text = state._preview.get(item.id)
    if text is None:
        # Cache miss — fetch synchronously. The preview pane may be
        # hidden, or the async worker may not have run yet; either way
        # the user pressed v/e meaning "I want this now", so blocking
        # briefly on the recipe's get_preview is acceptable.
        try:
            text = get_preview(item.id)
        except Exception as e:
            ctx.error(f'preview: {type(e).__name__}: {e}')
            return
        if text is None:
            ctx.message('No preview available')
            return
        # Cache so the preview pane (if shown later) skips the refetch.
        if hasattr(state, '_preview'):
            state._preview[item.id] = text
    cmd = os.environ.get(env_var) or default
    with tempfile.NamedTemporaryFile(
            mode='wb', prefix='browse-tui-', suffix='.txt', delete=False) as f:
        f.write(text.encode('utf-8', errors='surrogateescape'))
        path = f.name
    try:
        ctx.run_external(f'{cmd} {shlex.quote(path)}')
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


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
    """Set the selection to exactly the currently-visible normal rows.

    Implementation: clear the existing selection set, then add every
    ``kind='normal'`` row in the visible list. Anything previously
    selected that isn't in the visible list — hidden rows, children
    of collapsed parents, items in other scopes — gets dropped. This
    is the WYSIWYG semantic: after Ctrl-A, "what's selected" matches
    "what the user sees".

    Placeholder rows (``kind='pending'``) and the synthetic scope-root
    row are skipped — selecting a placeholder would smuggle the
    sentinel id into the selection set and confuse downstream
    consumers.
    """
    state = ctx._browser._state
    state.selected.clear()
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
    ctx._browser._mode = Mode.SEARCH_EDIT
    ctx._browser._search_query = ''
    ctx._browser._needs_redraw.add('info')


def _search_next(ctx):
    """Jump cursor to the next match (forward, wrap-around).

    Bound to ``enter`` while ``_mode is Mode.SEARCH_EDIT``. ``_search_find``
    starts the walk *after* the current cursor, so repeated presses
    advance through every match in turn before wrapping back to the
    first.
    """
    browser = ctx._browser
    state = browser._state
    idx = _search_find(state, browser._search_query, state.cursor, 1)
    if idx is not None:
        state.cursor = idx
        mark_cursor_changed(browser)


def _filter_start(ctx):
    """Enter filter-edit mode (``&`` keybinding).

    Appends an empty placeholder to ``_filters`` (the slot the user
    types into) and sets ``_mode = Mode.FILTER_EDIT``. The placeholder
    is never evaluated — see ``_recompute_filter_hidden`` and the
    spec at ``docs/superpowers/specs/2026-05-17-filter-design.md``.
    """
    b = ctx._browser
    b._mode = Mode.FILTER_EDIT
    b._filters.append('')
    b._needs_redraw.add('info')


def _filter_recompute_and_redraw(browser):
    """Re-evaluate filter visibility and flag UI redraws.

    Called from every FILTER_EDIT keystroke that mutates the last
    entry. Delegates to ``Browser._do_filter_change`` so the dispatch
    layer and the public API write paths share one reconciliation
    routine (recompute -> hide-displacement -> anchor -> redraw).
    """
    browser._do_filter_change()


def _search_prev(ctx):
    """Jump cursor to the previous match (backward, wrap-around).

    Symmetric counterpart to ``_search_next`` — bound to ``shift-enter``
    while ``_mode is Mode.SEARCH_EDIT``.
    """
    browser = ctx._browser
    state = browser._state
    idx = _search_find(state, browser._search_query, state.cursor, -1)
    if idx is not None:
        state.cursor = idx
        mark_cursor_changed(browser)


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
    """shift-up — scroll preview up by one line (clamped at 0).

    Clears ``_preview_at_tail`` first so an upward step from the
    tail-follow state lands one row above ``max_scroll`` (the renderer
    keeps ``_preview_scroll`` synchronised with ``max_scroll`` while
    the flag is engaged, so the decrement here is meaningful).
    """
    b = ctx._browser
    b._preview_at_tail = False
    if b._preview_scroll > 0:
        b._preview_scroll -= 1
        b._needs_redraw.add('preview')


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
    """alt-pgup — scroll preview up by one preview-pane page (clamped at 0).

    Clears ``_preview_at_tail`` — upward motion is the tail-follow
    exit signal. See ``_preview_scroll_up``.
    """
    b = ctx._browser
    b._preview_at_tail = False
    page = _preview_pane_height(b)
    b._preview_scroll = max(0, b._preview_scroll - page)
    b._needs_redraw.add('preview')


def _preview_home(ctx):
    """{Shift,Alt}-Home — jump preview scroll to the top.

    Clears ``_preview_at_tail`` — Home is an explicit top jump, the
    inverse of the tail-follow intent.
    """
    b = ctx._browser
    b._preview_at_tail = False
    b._preview_scroll = 0
    b._needs_redraw.add('preview')


def _preview_end(ctx):
    """{Shift,Alt}-End — pin the preview view to the bottom (tail-follow).

    Sets ``_preview_at_tail = True``; the renderer then forces
    ``_preview_scroll = max_scroll`` on every pass while the flag is
    engaged, so streaming appends (``append_preview``, generator
    pulls) keep the view at the new bottom without further user
    input. The flag is cleared by any upward scroll motion, cursor
    change, or help-mode toggle.
    """
    b = ctx._browser
    b._preview_at_tail = True
    b._needs_redraw.add('preview')


# ---- list/preview split resize -------------------------------------------


def _resize_step(list_h, prev_content):
    """Step size for one ``-`` or ``=`` keypress.

    ``(min(list, preview_content) // 5) + 1`` — i.e. roughly 20% of the
    smaller pane's content, with a +1 floor so resizing always nudges
    by at least 1 row even when one pane is tiny. Both inputs are
    *content* row counts (the preview separator does not count).
    """
    smaller = min(list_h, prev_content)
    if smaller < 0:
        smaller = 0
    return (smaller // 5) + 1


def _shrink_list(ctx):
    """``-`` / ``_`` — shrink the list pane by one resize step.

    Updates ``browser.list_ratio`` so the new size is preserved across
    terminal resizes. No-op when the preview pane is hidden — the
    ratio doesn't apply in that mode.
    """
    _resize_list(ctx, direction=-1)


def _grow_list(ctx):
    """``=`` / ``+`` — grow the list pane by one resize step.

    Symmetric counterpart to ``_shrink_list``. Capped by the layout
    invariants: at least 1 list row and 1 row of preview content remain
    after the resize.
    """
    _resize_list(ctx, direction=+1)


def _resize_list(ctx, *, direction):
    """Shared implementation for ``-`` / ``=``.

    Reads the current layout to determine pane sizes, computes the
    step, applies the new list size, and converts back to a ratio so
    terminal resizes preserve the user's choice. Headless / unwired
    contexts fall back gracefully (silent no-op).

    Axis-aware (#166): in the horizontal layout (``'h'``) the resize
    nudges the list HEIGHT in rows; in the vertical / mixed /
    preview-children layouts (``'v'``, ``'m'``, ``'pc'``) the primary
    split axis is columns, so the resize nudges the list WIDTH in cols
    instead. ``list_ratio`` is interpreted by the layout helpers as
    "ratio along the primary split axis", so the conversion back to a
    ratio uses ``cols`` instead of ``rows`` for those layouts.
    """
    browser = ctx._browser
    if not browser.show_preview:
        return  # ratio doesn't apply when preview is hidden
    ts = globals().get('term_size')
    lp = globals().get('layout_panes')
    if ts is None or lp is None:
        return
    try:
        cols, rows = ts()
    except Exception:
        return
    split = getattr(browser, 'split', 'h')
    layout = lp(
        cols, rows,
        split=split,
        show_preview=True,
        show_children_pane=browser.show_children_pane,
        list_ratio=browser.list_ratio,
    )
    list_rect = layout['list']
    preview_rect = layout.get('preview')

    if split == 'h':
        # Primary axis is rows. Resize list HEIGHT.
        children_rect = layout.get('children')
        list_h = list_rect.height
        sub_h = children_rect.height if children_rect is not None else 0
        # The preview Rect's first row IS the separator (in layout 'h');
        # user-visible content is height - 1.
        prev_total = preview_rect.height if preview_rect is not None else 0
        prev_content = max(0, prev_total - 1)
        step = _resize_step(list_h, prev_content)
        new_list_h = list_h + direction * step
        if new_list_h < 1:
            new_list_h = 1
        max_list = rows - sub_h - 2  # sep + 1 content
        if max_list < 1:
            max_list = max(1, rows - sub_h - 1)
        if new_list_h > max_list:
            new_list_h = max_list
        if new_list_h == list_h:
            return  # at the boundary; nothing to do
        new_ratio = new_list_h / float(rows)
    else:
        # Primary axis is cols (layouts 'v', 'm', 'pc'). Resize list
        # WIDTH. The layout helpers reserve 1 col for sep_main and at
        # least 1 col of preview content on the right; mirror that
        # clamp here so the stored ratio is always realisable.
        list_w = list_rect.width
        prev_total_w = preview_rect.width if preview_rect is not None else 0
        # No separator-row deduction in col-based layouts: sep_main is
        # its own 1-col Rect and preview.width is content cols.
        prev_content_w = max(0, prev_total_w)
        step = _resize_step(list_w, prev_content_w)
        new_list_w = list_w + direction * step
        if new_list_w < 1:
            new_list_w = 1
        # Reserve 1 col for sep_main and 1 col for preview content.
        max_list_w = cols - 2
        if max_list_w < 1:
            max_list_w = 1
        if new_list_w > max_list_w:
            new_list_w = max_list_w
        if new_list_w == list_w:
            return  # at the boundary; nothing to do
        new_ratio = new_list_w / float(cols)

    browser.list_ratio = new_ratio
    # Clamp ratio into [_LIST_RATIO_MIN, _LIST_RATIO_MAX]; the layout
    # already enforces visible minimums, but the stored ratio must
    # stay sane for future resizes.
    clamp = globals().get('_clamp_list_ratio')
    if clamp is not None:
        browser.list_ratio = clamp(browser.list_ratio)
    browser._needs_redraw.add('all')


# ---- layout split selection -----------------------------------------------
#
# Five actions select / cycle between the four split layouts produced by
# ``layout_panes`` in 050-render. ``Browser.set_split`` (040-state) clamps
# unknown values back to ``'h'`` and adds ``'all'`` to ``_needs_redraw``, so
# the handlers can stay one-liners — no need to re-add ``'all'`` here.
#
# Alt-N keybindings rely on ``read_key`` (020-terminal) emitting ``'alt-1'``
# … ``'alt-4'`` for ``ESC + '1'`` … ``ESC + '4'``: the standard "Meta-prefix"
# encoding used by alacritty, kitty, gnome-terminal, iTerm2, xterm and
# vt100-class emulators by default. Terminals running with xterm's
# ``modifyOtherKeys`` (e.g. ``CSI 27;3;49~`` style) or kitty's full keyboard
# protocol won't hit this path — for those we fall back to ``\`` (cycle),
# which has no modifier and is universally reachable.

_LAYOUT_CYCLE = ('v', 'h', 'm', 'pc')


def _set_layout_v(ctx):
    """Alt-1 — vertical split (list left, preview right)."""
    ctx._browser.set_split('v')


def _set_layout_h(ctx):
    """Alt-2 — horizontal split (list top, preview bottom)."""
    ctx._browser.set_split('h')


def _set_layout_m(ctx):
    """Alt-3 — mixed split (children pane + preview)."""
    ctx._browser.set_split('m')


def _set_layout_pc(ctx):
    """Alt-4 — preview-children split."""
    ctx._browser.set_split('pc')


def _cycle_layout(ctx):
    """``\\`` — cycle through layouts in order ``v → h → m → pc → v``.

    The cycle list is the canonical ordering documented in the Alt-N
    bindings. If ``browser.split`` somehow holds a value outside the
    cycle (defensive — ``set_split`` clamps inputs) we fall back to the
    first entry so the next press lands on a known layout.
    """
    cur = getattr(ctx._browser, 'split', 'h')
    try:
        idx = _LAYOUT_CYCLE.index(cur)
        nxt = _LAYOUT_CYCLE[(idx + 1) % len(_LAYOUT_CYCLE)]
    except ValueError:
        nxt = _LAYOUT_CYCLE[0]
    ctx._browser.set_split(nxt)


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
        Action('alt-p',      'Toggle children pane',    _toggle_children_pane, 'none', 'PREVIEW'),
        Action('R',          'Toggle preview ANSI colours', _toggle_preview_ansi, 'none', 'PREVIEW'),
        Action('shift-down', 'Scroll preview line down', _preview_scroll_down, 'none', 'PREVIEW'),
        Action('shift-up',   'Scroll preview line up',   _preview_scroll_up,   'none', 'PREVIEW'),
        Action('alt-pgdn',   'Scroll preview page down', _preview_page_down,   'none', 'PREVIEW'),
        Action('alt-pgup',   'Scroll preview page up',   _preview_page_up,     'none', 'PREVIEW'),
        # Shift-PgUp/Dn are aliases for Alt-PgUp/Dn. Many terminal
        # emulators intercept Shift-PgUp/Dn for their own scrollback,
        # but we register the binding anyway so it works in emulators
        # that pass it through.
        Action('shift-pgdn', '', _preview_page_down, 'none', 'PREVIEW'),
        Action('shift-pgup', '', _preview_page_up,   'none', 'PREVIEW'),
        # Jump preview to top/bottom. Bind both Shift- and Alt- variants
        # so users have at least one combination their terminal sends.
        Action('shift-home', 'Scroll preview to top',    _preview_home, 'none', 'PREVIEW'),
        Action('shift-end',  'Scroll preview to bottom', _preview_end,  'none', 'PREVIEW'),
        Action('alt-home',   '', _preview_home, 'none', 'PREVIEW'),
        Action('alt-end',    '', _preview_end,  'none', 'PREVIEW'),
        # Resize the list/preview split. Both pairs (``-``/``_`` and
        # ``=``/``+``) are bound for keyboard ergonomics — `-` and `=`
        # are unshifted, `_` and `+` are their shifted counterparts on
        # US layouts so users don't have to release shift mid-resize.
        Action('-',          'Shrink list pane',         _shrink_list,         'none', 'PREVIEW'),
        Action('_',          'Shrink list pane',         _shrink_list,         'none', 'PREVIEW'),
        Action('=',          'Grow list pane',           _grow_list,           'none', 'PREVIEW'),
        Action('+',          'Grow list pane',           _grow_list,           'none', 'PREVIEW'),
        # Layout split selection. Alt-1..4 jump directly to a layout; ``\``
        # cycles in canonical order (v → h → m → pc → v). The Alt-N bindings
        # rely on the ``ESC + digit`` Meta-prefix encoding (see notes near
        # ``_set_layout_v`` for terminal coverage); ``\`` is the universally
        # reachable fallback for terminals that swallow Alt-modified keys.
        # The four alt-N direct-jump bindings carry an empty ``label``
        # so the help composer (050-render._format_help_section) skips
        # them — their meaning is folded into the ``\`` line below to
        # keep the help screen compact (see #163). The bindings remain
        # fully functional in the dispatcher; they just don't take five
        # lines of help-screen real estate apiece.
        Action('\\',         '\\ / alt-1..4: cycle layouts (v/h/m/pc) or jump direct', _cycle_layout, 'none', 'PREVIEW'),
        Action('alt-1',      '', _set_layout_v,  'none', 'PREVIEW'),
        Action('alt-2',      '', _set_layout_h,  'none', 'PREVIEW'),
        Action('alt-3',      '', _set_layout_m,  'none', 'PREVIEW'),
        Action('alt-4',      '', _set_layout_pc, 'none', 'PREVIEW'),
        # Search.
        Action('/',         'Enter search mode', _search_start, 'none', 'SEARCH'),
        # Filter (less-style `&`). Stacks predicates; empty Enter or
        # Ctrl-X clears all. See
        # docs/superpowers/specs/2026-05-17-filter-design.md.
        Action('&',         'Enter filter mode', _filter_start, 'none', 'SEARCH'),
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
        # View / edit the cursor item's preview text. Recipes override
        # 'e' to make edits actually persist (the default discards).
        Action('v',         'View preview in $PAGER',  _view_in_pager,   'cursor', 'OTHER'),
        Action('e',         'Edit preview in $EDITOR (changes discarded)', _edit_in_editor, 'cursor', 'OTHER'),
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

    SEARCH_EDIT mode (``browser._mode is Mode.SEARCH_EDIT``) intercepts
    most keys: ``esc`` exits search, ``enter`` and ``shift-enter`` jump
    between matches (stubs in phase 1), ``backspace`` trims the query,
    and printable characters extend it. Other keys fall through to
    normal dispatch only when not in an edit mode.

    Outside edit modes: ``enter`` runs the on_enter handler (print-exit,
    action-redirect, noop, or callable) and other keys run their bound
    Action's handler if its ``requires`` precondition is met.

    Returns ``True`` if the key was handled, ``False`` otherwise — the
    caller (main loop in #13) uses the return to decide whether to log
    the key or pass it through to a fallback.
    """
    # Mouse events (clicks + wheel) are dispatched first when in normal
    # mode. ``_dispatch_mouse`` parses the row/col, looks up the pane
    # via ``layout_panes``, and applies the per-pane behaviour. Edit
    # modes silently swallow mouse events along with other unhandled
    # keys (the multi-char ``mouse-click:R:C`` form bypasses the
    # printable-char branch of the edit-mode handler).
    if browser._mode is Mode.NORMAL and (
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
    if browser._mode is Mode.SEARCH_EDIT:
        if key in ('esc', 'ctrl-c'):
            browser._mode = Mode.NORMAL
            browser._search_query = ''
            browser._needs_redraw.add('info')
            browser._needs_redraw.add('list')
            return True
        if key == 'enter':
            _search_next(ctx)
            return True
        # Alt-Enter is offered alongside Shift-Enter for terminals that
        # swallow Shift+Enter (many of them do).
        if key in ('shift-enter', 'alt-enter'):
            _search_prev(ctx)
            return True
        if key == 'backspace':
            browser._search_query = browser._search_query[:-1]
            browser._needs_redraw.add('info')
            browser._needs_redraw.add('list')
            if browser._search_query:
                _search_jump_nearest(browser)
            return True
        # Ctrl-W kills the last whitespace-delimited word (readline
        # convention): strip trailing spaces, then strip the trailing
        # run of non-space chars. Idempotent on an empty / all-space
        # query.
        if key == 'ctrl-w':
            q = browser._search_query
            i = len(q)
            while i > 0 and q[i - 1] == ' ':
                i -= 1
            while i > 0 and q[i - 1] != ' ':
                i -= 1
            browser._search_query = q[:i]
            browser._needs_redraw.add('info')
            browser._needs_redraw.add('list')
            if browser._search_query:
                _search_jump_nearest(browser)
            return True
        # Ctrl-U clears the whole query (line-kill).
        if key == 'ctrl-u':
            browser._search_query = ''
            browser._needs_redraw.add('info')
            browser._needs_redraw.add('list')
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
        # Multi-char key names (arrows, pgdn/pgup, home/end, ctrl-*,
        # alt-*, etc.) fall through to the normal-mode dispatch below so
        # the list-pane navigation keys keep working while the user is
        # composing a query. Mouse events fall through harmlessly — the
        # mouse-dispatch branch above is gated on ``Mode.NORMAL`` and
        # the keymap has no binding for ``mouse-click:R:C``, so the
        # event ends up silently dropped (preserves the prior
        # search-mode-swallows-mouse contract).

    # Filter-edit (``&``) special handling.
    #
    # Mirrors SEARCH_EDIT in spirit but acts on ``browser._filters``
    # (a list) and re-evaluates the per-Item ``_filter_hidden`` flags
    # after every keystroke that changes the last entry. The last
    # entry is the "live" one while in FILTER_EDIT.
    #
    # Enter commits (or clears all, if empty); Ctrl-X clears all
    # unconditionally; Ctrl-C / Esc cancel the in-progress edit only.
    # Other keys (arrows, etc.) fall through to NORMAL dispatch.
    if browser._mode is Mode.FILTER_EDIT:
        if key == 'enter':
            last = browser._filters[-1] if browser._filters else ''
            browser._mode = Mode.NORMAL
            if not last:
                # Commit-empty == clear all (less-compatible).
                browser._filters = []
            # else: the last entry is already in the list as the new
            # committed top-of-stack — nothing more to do.
            _filter_recompute_and_redraw(browser)
            return True
        if key == 'ctrl-x':
            browser._mode = Mode.NORMAL
            browser._filters = []
            _filter_recompute_and_redraw(browser)
            return True
        if key in ('esc', 'ctrl-c'):
            # Cancel the in-progress edit: pop the last entry (which
            # IS the in-progress one in FILTER_EDIT). Committed
            # filters stay.
            browser._mode = Mode.NORMAL
            if browser._filters:
                browser._filters.pop()
            _filter_recompute_and_redraw(browser)
            return True
        if key == 'backspace':
            if browser._filters and browser._filters[-1]:
                browser._filters[-1] = browser._filters[-1][:-1]
                _filter_recompute_and_redraw(browser)
            return True
        # Ctrl-W kills the last whitespace-delimited word in the
        # in-progress entry (readline convention; mirrors search).
        if key == 'ctrl-w':
            if browser._filters:
                q = browser._filters[-1]
                i = len(q)
                while i > 0 and q[i - 1] == ' ':
                    i -= 1
                while i > 0 and q[i - 1] != ' ':
                    i -= 1
                browser._filters[-1] = q[:i]
                _filter_recompute_and_redraw(browser)
            return True
        # Ctrl-U clears the in-progress entry (line-kill). To clear
        # all filters use Ctrl-X.
        if key == 'ctrl-u':
            if browser._filters:
                browser._filters[-1] = ''
                _filter_recompute_and_redraw(browser)
            return True
        # ``space`` is the special name read_key uses for the bare
        # spacebar; treat it literally inside the filter prompt.
        if key == 'space':
            if browser._filters:
                browser._filters[-1] += ' '
                _filter_recompute_and_redraw(browser)
            return True
        if len(key) == 1 and key.isprintable():
            if browser._filters:
                browser._filters[-1] += key
                _filter_recompute_and_redraw(browser)
            return True
        # Multi-char keys (arrows, etc.) fall through to NORMAL
        # dispatch. The prompt stays open; the last entry is
        # unchanged.

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
            split=getattr(browser, 'split', 'h'),
            show_preview=browser.show_preview,
            show_children_pane=browser.show_children_pane,
            list_ratio=getattr(browser, 'list_ratio', 0.30),
        )
        list_rect = layout.get('list')
        h = list_rect.height if list_rect is not None else 0
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
            split=getattr(browser, 'split', 'h'),
            show_preview=browser.show_preview,
            show_children_pane=browser.show_children_pane,
            list_ratio=getattr(browser, 'list_ratio', 0.30),
        )
        preview_rect = layout.get('preview')
        prev_total = preview_rect.height if preview_rect is not None else 0
        h = prev_total - 1  # exclude separator row
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


def _cursor_item_for_dispatch(browser):
    """Return the Item under the cursor for dispatch's layout sizing.

    Mirrors render.py's ``_cursor_item`` (only ``kind='normal'`` rows
    have an Item). Returns ``None`` for placeholder / scope-root rows
    or when the visible list is empty. The dispatcher uses this only
    to size the children grid when computing the layout for hit-testing
    — a None result simply collapses the grid the same way it does in
    the renderer.
    """
    state = browser._state
    vis = visible_items(state)
    cur = state.cursor
    if not (0 <= cur < len(vis)):
        return None
    entry = vis[cur]
    if entry.kind != 'normal':
        return None
    return entry.item


def _dispatch_mouse(browser, ctx, key):
    """Route a mouse event ``key`` to the pane it lands on.

    The event payload is ``kind:R:C`` (1-based). We resolve the layout
    via the injected ``layout_panes`` helper and walk panes in defined
    order — ``list``, ``children``, ``preview``, ``info_bar`` — picking
    the first whose Rect contains ``(row, col)``. This is correct for
    all four layout families (h/v/m/pc): the row-only hit-test that
    pre-#152 used assumed full-width panes, which only holds for layout
    'h'.

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
        col = int(parts[2])
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
    # Match the rendered layout exactly: the children grid sizes itself
    # from the cursor item's cached children (see ``_layout_for`` in
    # 050-render). The dispatcher must use the same children_rows_needed
    # so a click on a column the user can see actually routes to the
    # pane that's drawn there. Falling back to 0 (no children) matches
    # the conservative pre-#152 behaviour for headless tests where the
    # render helpers aren't injected.
    children_rows_needed = 0
    if browser.show_children_pane:
        cur_item = _cursor_item_for_dispatch(browser)
        if cur_item is not None and getattr(cur_item, 'has_children', False):
            cached = browser._state._children.get(cur_item.id)
            sub_needed = globals().get('_sub_needed_rows')
            if cached:
                if sub_needed is not None:
                    try:
                        children_rows_needed = sub_needed(
                            cached, cols,
                            show_ids=getattr(browser, 'show_ids', 'auto'),
                        )
                    except Exception:
                        children_rows_needed = 1
                else:
                    children_rows_needed = 1
            elif cached is None:
                children_rows_needed = 1
    layout = lp(
        cols, rows_total,
        split=getattr(browser, 'split', 'h'),
        show_preview=browser.show_preview,
        show_children_pane=browser.show_children_pane,
        children_rows_needed=children_rows_needed,
        list_ratio=getattr(browser, 'list_ratio', 0.30),
    )
    pane = _pane_at(layout, row, col)

    if kind == 'mouse-click':
        if pane == 'list':
            _click_list_row(browser, layout, row)
        elif pane == 'preview':
            if browser._help_mode:
                browser._help_mode = False
                browser._needs_redraw.add('preview')
        # children grid / info bar / separator → no-op
        return True
    if kind in ('scroll-up', 'scroll-down'):
        delta = -_WHEEL_LINES if kind == 'scroll-up' else _WHEEL_LINES
        if pane == 'list':
            # Manual viewport change overrides any parked
            # scroll-to-fit goal — the user has taken over.
            browser._expand_goal = None
            _scroll_list(browser, delta)
        elif pane == 'preview':
            _scroll_preview(browser, delta)
        return True
    return True


def _pane_at(layout, row, col):
    """Return the pane name containing ``(row, col)``, or ``None``.

    Walks the panes in fixed order — ``list``, ``children``,
    ``preview``, ``info_bar`` — and returns the first whose Rect
    contains the point. Uses :func:`point_in_rect` from 050-render so
    the same rect convention (inclusive-top, exclusive-right/bottom,
    1-based) applies everywhere.

    The separator rows themselves are excluded from the children /
    preview hit-area in layout 'h' (where the pane's first row IS the
    separator row carrying the rich info-bar decoration). The info_bar
    Rect is returned distinctly so a click on the bottom info bar in
    v/m/pc layouts is recognised (currently the dispatcher treats it
    as a no-op, but recipes may want to overlay a prompt there).
    """
    list_rect = layout.get('list')
    children_rect = layout.get('children')
    preview_rect = layout.get('preview')
    info_bar = layout.get('info_bar')

    if point_in_rect(row, col, list_rect):
        return 'list'
    # In layout 'h' the children / preview Rect's first row IS that
    # pane's info-bar separator (full-width); exclude it from the
    # clickable area so clicks on the divider are no-ops. In v/m/pc
    # layouts the info bar is its own bottom Rect and doesn't overlap
    # with children/preview, so the +1 carve-out is harmless there.
    if children_rect is not None and children_rect.height > 0:
        carve = 1 if (info_bar is not None
                      and info_bar.top == children_rect.top
                      and info_bar.left == children_rect.left
                      and info_bar.right == children_rect.right) else 0
        if (children_rect.top + carve <= row < children_rect.bottom
                and children_rect.left <= col < children_rect.right):
            return 'children'
    if preview_rect is not None and preview_rect.height > 0:
        carve = 1 if (info_bar is not None
                      and info_bar.top == preview_rect.top
                      and info_bar.left == preview_rect.left
                      and info_bar.right == preview_rect.right) else 0
        if (preview_rect.top + carve <= row < preview_rect.bottom
                and preview_rect.left <= col < preview_rect.right):
            return 'preview'
    if point_in_rect(row, col, info_bar):
        return 'info_bar'
    return None


def _click_list_row(browser, layout, row):
    """Move ``state.cursor`` to the visible-list row at terminal ``row``."""
    state = browser._state
    visible = visible_items(state)
    list_rect = layout['list']
    new_idx = browser._list_scroll + (row - list_rect.top)
    if 0 <= new_idx < len(visible) and state.cursor != new_idx:
        state.cursor = new_idx
        mark_cursor_changed(browser)


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
    """Adjust ``_preview_scroll`` by ``delta`` rows (mirrors shift-up/-down).

    Wheel-up (``delta < 0``) clears ``_preview_at_tail`` — the tail
    pin survives downward wheel motion (renderer clamps; pin still
    engaged) but is broken by any upward gesture.
    """
    if delta < 0:
        browser._preview_at_tail = False
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
