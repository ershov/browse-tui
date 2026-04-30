"""browse-tui: render layer (list + preview, item formatting, status bar).

Phase 1 subset of plan-tui's renderer:

  * Two panes — a scrolling list and a preview pane — separated by an
    info bar that doubles as a separator. ``show_preview=False`` collapses
    to a single full-screen list with the info bar at the bottom row.
  * Item rows are built as a list of ``(text, fg, bold)`` segments by
    ``format_item_segments``. Recipes can override the whole row layout
    by supplying a ``format_item=lambda item, ctx: …`` hook on Browser
    (the renderer passes through the hook's return value verbatim).
  * Tag styling: ``Item.tag_style`` is a string that maps via
    ``_TAG_STYLE`` to a (fg, bold) pair. The eight named styles in the
    spec (green/red/yellow/gray/cyan/blue/magenta/dim) plus ``''`` (no
    styling) are recognised; unknown names fall back to ``''``.
  * ``⧗ loading…`` placeholder rows render dimmed without the usual
    selection / expand markers.
  * ``render_full`` paints the entire screen; ``render_partial`` redraws
    only the regions named in ``Browser._needs_redraw``. Both flush
    once at the end.
  * Help screen is shown by toggling ``Browser._help_mode``; the preview
    pane displays ``_HELP_TEXT`` instead of the per-item preview.

Out of scope (deferred to phase 2):
  * Children-grid pane (the multi-column ``render_subtickets`` from
    plan-tui — ticket #19).
  * Insert-mode marker — ticket #21.
  * Search-fragment highlight in the *list* — ticket #22 fills in
    ``_write_highlighted`` with a non-empty search query. The hook is
    already wired here so phase 2 only changes one site.
"""


# tag_style name → (fg color, bold flag). Values are 256-colour palette
# indices that ``020-terminal.set_style`` understands. The empty-string
# entry is the default — callers do ``_TAG_STYLE.get(name, _TAG_STYLE[''])``
# so unknown style names render as plain text rather than crashing.
_TAG_STYLE = {
    'green':   (2, False),
    'red':     (1, True),
    'yellow':  (3, False),
    'gray':    (8, False),
    'cyan':    (6, False),
    'blue':    (4, False),
    'magenta': (5, False),
    'dim':     (242, False),
    '':        (None, False),  # default — no styling
}

_ID_COLOR = 3   # yellow for the '#id' segment
_MARKER_COLOR = 4   # blue for the ▼/▶ expand glyph
_PENDING_FG = 242   # dim for the ⧗ loading… placeholder
_SCOPE_ROOT_FG = None   # default fg, bolded by the segment's bold=True


# ---------------------------------------------------------------------------
# Pure helpers — unit-testable, no terminal I/O
# ---------------------------------------------------------------------------


def format_item_segments(item, *, depth=0, base_depth=0, expanded=False,
                         selected=False, kind='normal', search_query='',
                         format_item=None, ctx=None):
    """Compute the (text, fg, bold) segment list for one row.

    Returns a list of ``(text, fg, bold)`` triples. When the user-supplied
    ``format_item`` hook is non-None, it's called with ``(item, ctx)`` and
    its return value is used verbatim — the default segment layout below
    is bypassed entirely. This is the override point recipes use to
    surface domain-specific rows (file size, mtime, … see browse-fs).

    Default layout for ``kind='normal'``:
      - selection marker: ``'* '`` if ``selected``, else ``'  '``
      - indentation:      ``2 * (depth - base_depth)`` spaces
      - expand marker:    ``'▼ '`` if ``expanded``,
                          ``'▶ '`` if ``item.has_children`` else ``'  '``
      - id segment:       ``'#{id} '`` in yellow
      - tag segment:      ``'[{tag}] '`` styled per ``_TAG_STYLE`` (omitted
                          when ``item.tag`` is empty)
      - title segment:    ``item.title``

    Default layout for ``kind='pending'``:
      - indentation only, then a single dim ``'⧗ loading…'`` segment.
        No selection marker, no expand marker — placeholder rows are
        synthetic and don't participate in selection/expansion.

    Default layout for ``kind='scope_root'``:
      - bolded ``'#{id} {title}'`` — the scope row at the top of the
        list. No selection marker (the scope item itself isn't part of
        the user's selection), no expand marker (it's always "expanded"
        by definition of being the scope root).

    ``search_query`` is accepted for forward-compatibility with phase-2
    ticket #22; phase 1 ignores it (highlighting is applied later by
    the renderer, not by this segment builder).
    """
    if format_item is not None:
        # User override wins entirely. We don't validate the shape —
        # if the hook returns something the writer can't handle, that
        # surfaces as a clearer error in the writer.
        return format_item(item, ctx)

    rel_depth = depth - base_depth
    if rel_depth < 0:
        rel_depth = 0
    indent = '  ' * rel_depth

    if kind == 'pending':
        # Placeholder rows: indent + dim glyph. Title carries the
        # ⧗ loading… text from ``_make_pending_placeholder`` in 040-state.
        return [
            (indent, None, False),
            (item.title, _PENDING_FG, False),
        ]

    if kind == 'scope_root':
        return [
            ('#{} '.format(item.id), _ID_COLOR, True),
            (item.title, _SCOPE_ROOT_FG, True),
        ]

    # ----- normal kind ------------------------------------------------
    sel_marker = '* ' if selected else '  '

    if item.has_children:
        expand_marker = '▼ ' if expanded else '▶ '   # ▼ / ▶
    else:
        expand_marker = '  '

    segments = [
        (sel_marker, None, False),
        (indent, None, False),
        (expand_marker, _MARKER_COLOR, False),
        ('#{} '.format(item.id), _ID_COLOR, False),
    ]

    if item.tag:
        sfg, sbold = _TAG_STYLE.get(item.tag_style, _TAG_STYLE[''])
        segments.append(('[{}] '.format(item.tag), sfg, sbold))

    segments.append((item.title, None, False))
    return segments


def layout_panes(cols, rows, *, show_preview=True):
    """Return geometry dict for the two-pane layout.

    Keys returned:
      * ``cols``        — terminal width (for callers).
      * ``list_top``    — first row of the list pane (1-based).
      * ``list_height`` — rows in the list pane.
      * ``info_row``    — row of the info-bar separator (0 if none).
      * ``prev_top``    — first row of the preview pane.
      * ``prev_height`` — rows in the preview pane (incl. separator? no
                          — separator is the info_row, content starts at
                          ``prev_top + 1``; ``prev_height`` is the content
                          row count).

    Two-pane (show_preview=True):
      * ``list_height``  ≈ 30% of rows, minimum 1.
      * ``info_row``     = ``list_top + list_height`` (the separator).
      * ``prev_top``     = ``info_row + 1`` (preview content starts the
                            row after the separator). The separator
                            itself is owned by ``render_full`` /
                            ``render_partial`` via ``render_separator``.
      * ``prev_height``  = remaining rows (≥ 0); content-only count.

    One-pane (show_preview=False):
      * ``list_height``  = ``rows - 1`` (last row reserved for info bar).
      * ``info_row``     = ``rows``.
      * ``prev_height``  = 0.

    All returned heights are clamped non-negative; very small terminals
    get a degenerate but valid layout.
    """
    cols = max(1, int(cols))
    rows = max(1, int(rows))
    list_top = 1

    if not show_preview:
        list_height = max(1, rows - 1)
        info_row = list_top + list_height
        return {
            'cols': cols,
            'list_top': list_top,
            'list_height': list_height,
            'info_row': info_row,
            'prev_top': info_row,
            'prev_height': 0,
        }

    # ~30% of rows for the list, separator on the boundary, remainder
    # for preview content.
    list_height = max(1, int(rows * 0.30))
    # Make sure the separator + at least the preview-separator row fit.
    if list_height + 1 > rows:
        list_height = max(1, rows - 1)
    info_row = list_top + list_height
    # Separator at info_row, content starts at info_row+1.
    prev_top = info_row + 1
    prev_height = max(0, rows - list_height - 1)

    return {
        'cols': cols,
        'list_top': list_top,
        'list_height': list_height,
        'info_row': info_row,
        'prev_top': prev_top,
        'prev_height': prev_height,
    }


# ---------------------------------------------------------------------------
# Low-level write helpers (call ``020-terminal`` primitives)
# ---------------------------------------------------------------------------


def _write_segments(segments, max_width, *, pad_to=0):
    """Emit ``segments`` to the terminal, truncating at ``max_width`` columns.

    Each segment is a ``(text, fg, bold)`` triple. ``fg=None`` and
    ``bold=False`` means use the terminal's current style (no SGR
    sequences emitted, no reset). When fg or bold is set, we wrap the
    chunk in ``set_style`` / ``reset_style`` so adjacent segments don't
    bleed colours.

    Returns the number of characters written (so callers can pad to a
    fixed column).
    """
    pos = 0
    for text, fg, bold in segments:
        if pos >= max_width:
            break
        remaining = max_width - pos
        chunk = text[:remaining]
        if not chunk:
            continue
        if fg is not None or bold:
            set_style(fg=fg, bold=bold)
            write(chunk)
            reset_style()
        else:
            write(chunk)
        pos += len(chunk)
    if pad_to > pos:
        write(' ' * (pad_to - pos))
        pos = pad_to
    return pos


def _write_highlighted(line, base_fg=None, base_bold=False, reverse=False,
                       pad_to=0, search_query=''):
    """Write ``line`` with search-fragment highlights overlaid on the base style.

    Lifted from plan-tui's renderer with one tweak: ``search_query`` is
    a parameter rather than a module global, so the function is callable
    from a Browser-aware caller. When ``search_query`` is empty the line
    is written with the base style only — same fast path as plan-tui.

    Phase 1 callers pass ``search_query=''`` from ``render_list``; the
    highlight branch lights up in phase-2 ticket #22 (search highlight
    in the list).
    """
    if not search_query:
        if base_fg is not None or base_bold or reverse:
            set_style(fg=base_fg, bold=base_bold, reverse=reverse)
        write(line)
        if pad_to > len(line):
            write(' ' * (pad_to - len(line)))
        if base_fg is not None or base_bold or reverse:
            reset_style()
        return

    frags = search_query.lower().split()
    if not frags:
        if base_fg is not None or base_bold or reverse:
            set_style(fg=base_fg, bold=base_bold, reverse=reverse)
        write(line)
        if pad_to > len(line):
            write(' ' * (pad_to - len(line)))
        if base_fg is not None or base_bold or reverse:
            reset_style()
        return

    # Build a mask of which character positions are inside any fragment.
    low = line.lower()
    mask = [False] * len(line)
    for frag in frags:
        flen = len(frag)
        if flen == 0:
            continue
        start = 0
        while True:
            pos = low.find(frag, start)
            if pos < 0:
                break
            for i in range(pos, pos + flen):
                mask[i] = True
            start = pos + 1

    # Emit alternating spans, switching styles at each boundary.
    i = 0
    while i < len(line):
        if mask[i]:
            j = i
            while j < len(line) and mask[j]:
                j += 1
            if reverse:
                set_style(reverse=True, underline=True)
            else:
                set_style(fg=3, bold=True)
            write(line[i:j])
            reset_style()
            i = j
        else:
            j = i
            while j < len(line) and not mask[j]:
                j += 1
            if base_fg is not None or base_bold or reverse:
                set_style(fg=base_fg, bold=base_bold, reverse=reverse)
            write(line[i:j])
            if base_fg is not None or base_bold or reverse:
                reset_style()
            i = j

    if pad_to > len(line):
        if reverse:
            set_style(reverse=True)
        write(' ' * (pad_to - len(line)))
        if reverse:
            reset_style()


# ---------------------------------------------------------------------------
# Pane renderers — each owns a region, takes Browser + geometry
# ---------------------------------------------------------------------------


def render_list(browser, top, height, cols):
    """Render the list pane for ``browser`` at the given region.

    Reads cursor / selected / expanded / scope state from
    ``browser._state`` via ``visible_items``. Maintains
    ``browser._list_scroll`` to keep the cursor on-screen.
    """
    if height <= 0:
        return

    state = browser._state
    visible = visible_items(state)
    cursor_pos = state.cursor

    # Adjust scroll offset to keep the cursor on-screen.
    scroll = browser._list_scroll
    if cursor_pos < scroll:
        scroll = cursor_pos
    if cursor_pos >= scroll + height:
        scroll = cursor_pos - height + 1
    if scroll < 0:
        scroll = 0
    browser._list_scroll = scroll

    # base_depth: the scope-root row sits at depth 0 by construction
    # (visible_items sets ``kind='scope_root', depth=0`` when scoped),
    # so children below it want their indent measured from depth 1.
    if state.scope_stack:
        base_depth = 1
    else:
        base_depth = 0

    for row_idx in range(height):
        vis_idx = scroll + row_idx
        move(top + row_idx, 1)
        clear_line()

        if vis_idx >= len(visible):
            continue

        entry = visible[vis_idx]
        item = entry.item
        is_cursor_line = (vis_idx == cursor_pos)
        is_selected = (
            entry.kind == 'normal' and item.id in state.selected
        )

        segments = format_item_segments(
            item,
            depth=entry.depth,
            base_depth=base_depth,
            expanded=item.id in state.expanded,
            selected=is_selected,
            kind=entry.kind,
            search_query=browser._search_query,
            format_item=browser.format_item,
            ctx=None,
        )

        if is_cursor_line:
            # Reverse video for the cursor line — collapse segments to a
            # plain line so the search-highlighter can overlay it.
            line = ''.join(s[0] for s in segments)
            if len(line) > cols:
                line = line[:cols]
            _write_highlighted(
                line, reverse=True, pad_to=cols,
                search_query=browser._search_query,
            )
        else:
            # Regular row: write segments directly, truncating at ``cols``.
            # Search-highlight is wired here for phase-2 #22; phase 1's
            # empty search_query short-circuits to the no-op fast path
            # in ``_write_highlighted``, so we keep the multi-segment
            # write for proper colouring.
            _write_segments(segments, cols)


def render_preview(browser, top, height, cols, *, info=False):
    """Render the preview pane content at rows ``top .. top+height-1``.

    The separator/info-bar above is *not* drawn here — that's owned by
    ``render_full`` / ``render_partial`` (they call ``render_separator``
    at ``info_row`` independently). This function paints content rows
    only.

    Source priority:
      1. ``browser._error_text`` if set      → display the error text
      2. ``browser._help_mode`` if True      → display ``_HELP_TEXT``
      3. ``browser._state._preview[id]``     → per-item preview
      4. fallthrough — empty preview         → blank rows
    """
    if height <= 0:
        return

    if browser._error_text:
        text = browser._error_text
    elif browser._help_mode:
        text = _HELP_TEXT
    else:
        cursor_id = _cursor_id(browser)
        text = (
            browser._state._preview.get(cursor_id, '')
            if cursor_id is not None else ''
        )

    # Wrap content to terminal width.
    raw = text.split('\n') if text else []
    wrapped = []
    for line in raw:
        line = line.replace('\t', '    ')
        if not line:
            wrapped.append('')
            continue
        while len(line) > cols:
            wrapped.append(line[:cols])
            line = line[cols:]
        wrapped.append(line)

    scroll = browser._preview_scroll
    if scroll < 0:
        scroll = 0
    for i in range(height):
        move(top + i, 1)
        clear_line()
        src_idx = i + scroll
        if src_idx < len(wrapped):
            write(wrapped[src_idx])


def _cursor_id(browser):
    """Return the id of the item currently under the cursor, or None."""
    vis = visible_items(browser._state)
    cur = browser._state.cursor
    if 0 <= cur < len(vis):
        return vis[cur].item.id
    return None


_SB_BG = 236   # dark gray status-bar background
_SB_FG = 252   # light gray status-bar text


def _scope_crumb_text(browser):
    """Return the scope-crumb plain-text string (or '' when unscoped).

    Format: ``' ▸ a ▸ b ▸ c '`` — one ``▸ <id>`` segment per stack
    entry, with leading and trailing single spaces. Returns empty
    string when ``scope_stack`` is empty so callers can short-circuit.

    Phase-2 deliberately renders the full crumb without truncation —
    on narrow terminals the crumb may eat hint space or the label
    fillers (the renderer still clamps writes at ``cols`` so nothing
    spills off-screen, just hint/label visibility degrades). Phase-3
    can layer adaptive truncation on top.
    """
    if browser is None:
        return ''
    stack = browser._state.scope_stack
    if not stack:
        return ''
    return ' ' + ' '.join('▸ {}'.format(sid) for sid in stack) + ' '


def render_separator(row, cols, label, *, info=False, browser=None):
    """Render the info-bar / pane-separator row.

    When ``info=True`` the separator additionally shows:
      * ``[N]`` selection count (if non-zero) in bold cyan;
      * a scope crumb path (when ``scope_stack`` is non-empty) — one
        ``▸ <id>`` segment per stack entry in bright cyan;
      * the search prompt + query (when ``browser._search_mode``);
      * a dim hint string about navigation keys (when not searching).

    The right edge ends with the pane label (``Preview``, ``Help``, …).

    No truncation is applied to the scope crumb in this phase — on a
    narrow terminal the crumb may push the hint text out of view (or
    eat into the trailing filler before the label). Writes are still
    clamped at ``cols`` so nothing spills off-screen; only hint/filler
    visibility degrades. Phase-3 can layer adaptive truncation.
    """
    S = '─'  # ─
    move(row, 1)
    clear_line()

    if info and browser is not None:
        sel_count = len(browser._state.selected)
        search = browser._search_query if browser._search_mode else None
        crumb = _scope_crumb_text(browser)
    else:
        sel_count = 0
        search = None
        crumb = ''

    label_str = ' {} '.format(label)
    pos = 0

    # Leading separator.
    set_style(fg=8)
    n = min(2, cols)
    write(S * n)
    pos = n

    # Selection count, only when info row.
    if sel_count > 0 and pos < cols:
        sel_str = '[{}]'.format(sel_count)
        set_style(fg=6, bold=True)
        write(sel_str[:cols - pos])
        pos += len(sel_str)
        set_style(fg=8)

    # Scope crumb (between selection count and the spacer-to-7). Bright
    # cyan, no bold — distinguishable from the selection count without
    # being shouty. Written *before* the spacer so the crumb sits
    # adjacent to the selection count when both are present.
    if crumb and pos < cols:
        set_style(fg=14)  # bright cyan
        write(crumb[:cols - pos])
        pos += min(len(crumb), cols - pos)
        set_style(fg=8)

    # Spacer to position 7 (where the search prompt / hints start).
    if pos < 7 and pos < cols:
        write(S * min(7 - pos, cols - pos))
        pos = min(7, cols)

    # Search prompt (when searching) or context-sensitive hints.
    if search is not None and pos < cols:
        prompt = '/'
        set_style(fg=11, bg=4, bold=True)
        write(prompt[:cols - pos])
        pos += len(prompt)
        if pos < cols:
            reset_style()
            write(search[:cols - pos])
            pos += len(search)
        set_style(fg=8)
    elif info and pos < cols:
        # Hint text — kept generic; phase-3 ticket #32 will let recipes
        # surface their own action keys here.
        hints = ' /:search  ?:help  q:quit '
        avail = cols - pos - len(label_str) - 3
        if avail > 10:
            h = hints[:avail]
            set_style(fg=242)
            write(h)
            pos += len(h)
        set_style(fg=8)

    # Filler to where the label sits.
    label_start = cols - len(label_str) - 1
    if label_start < pos + 1:
        label_start = pos + 1
    if pos < label_start and pos < cols:
        write(S * min(label_start - pos, cols - pos))
        pos = min(label_start, cols)

    # Label.
    if pos < cols:
        avail = cols - pos
        write(label_str[:avail])
        pos += min(len(label_str), avail)

    # Trailing fill.
    if pos < cols:
        write(S * (cols - pos))

    reset_style()


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def _layout_for(browser):
    """Build the geometry dict from current terminal size + browser flags."""
    cols, rows = term_size()
    return layout_panes(cols, rows, show_preview=browser.show_preview)


def render_full(browser):
    """Repaint the whole screen.

    Clears the screen, lays out the panes, paints each, then flushes.
    Resets ``browser._needs_redraw`` so subsequent partial-render calls
    start from a clean slate.
    """
    write('\033[2J')
    layout = _layout_for(browser)
    render_list(
        browser,
        layout['list_top'], layout['list_height'], layout['cols'],
    )
    label = _preview_label(browser)
    if layout['info_row'] > 0:
        render_separator(
            layout['info_row'], layout['cols'], label,
            info=True, browser=browser,
        )
    if browser.show_preview and layout['prev_height'] > 0:
        render_preview(
            browser,
            layout['prev_top'], layout['prev_height'], layout['cols'],
        )
    flush()
    browser._needs_redraw = set()


def render_partial(browser):
    """Selective redraw based on ``browser._needs_redraw``.

    Recognised flags: ``'list'``, ``'preview'``, ``'info'``, ``'all'``.
    ``'all'`` short-circuits to ``render_full``. Unknown flags are
    ignored (no error) so callers can stuff "hint" tokens in the set
    without crashing the renderer.
    """
    needs = browser._needs_redraw
    if 'all' in needs:
        render_full(browser)
        return
    if not needs:
        return

    layout = _layout_for(browser)
    if 'list' in needs:
        render_list(
            browser,
            layout['list_top'], layout['list_height'], layout['cols'],
        )
    if 'preview' in needs:
        if browser.show_preview and layout['prev_height'] > 0:
            render_preview(
                browser,
                layout['prev_top'], layout['prev_height'], layout['cols'],
            )
    if 'info' in needs:
        if layout['info_row'] > 0:
            render_separator(
                layout['info_row'], layout['cols'], _preview_label(browser),
                info=True, browser=browser,
            )
    flush()
    browser._needs_redraw = set()


def _preview_label(browser):
    """Pick the preview pane label based on browser state."""
    if browser._error_text:
        return 'Error'
    if browser._help_mode:
        return 'Help'
    return 'Preview'


# ---------------------------------------------------------------------------
# Help text shown in the preview pane when ``browser._help_mode`` is True.
# Kept short and generic — recipes surface their own action keys via the
# help screen in phase 3 (#32).
# ---------------------------------------------------------------------------


_HELP_TEXT = """\
browse-tui — generic hierarchical browser

NAVIGATION
  j, Down          Cursor down
  k, Up            Cursor up
  g, Home          First item
  G, End           Last item
  PgUp, PgDn       Page up/down
  Right            Expand node / move to first child
  Left             Collapse node / move to parent

PREVIEW
  Ctrl-P           Toggle preview pane
  Shift-Down       Scroll preview line down
  Shift-Up         Scroll preview line up

SEARCH
  /                Enter search mode
  Enter            Next match
  Shift-Enter      Previous match
  Esc              Exit search mode

ACTIONS
  Custom           Recipe-specific (see app's --help)

OTHER
  ?                Help (toggle)
  q, Esc, Ctrl-C   Quit
  Ctrl-R           Reload
  Ctrl-L           Redraw
"""
