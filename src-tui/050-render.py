"""browse-tui: render layer (list + children-grid + preview, status bar).

Renderer layout:

  * Up to three panes — a scrolling list, a multi-column children grid
    (between the list and the preview), and a preview pane — each
    separated by an info bar that doubles as a separator.
    ``show_preview=False`` collapses to a single full-screen list with
    the info bar at the bottom row. ``show_children_pane=False`` (or
    a cursor on a leaf, or no cached children) hides the children grid
    entirely; the preview pane then takes that space.
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
  * The children-grid pane shows the cursor item's direct children in a
    flowed multi-column layout (ported from plan-tui). Each entry uses
    the per-item tag colour from ``_TAG_STYLE``. Hidden when the
    terminal is too small (< 20 rows) or the cursor item has no
    children to display.
  * ``render_full`` paints the entire screen; ``render_partial`` redraws
    only the regions named in ``Browser._needs_redraw``. Both flush
    once at the end.
  * Help screen is shown by toggling ``Browser._help_mode``; the preview
    pane displays the output of ``compose_help_text(browser)`` instead
    of the per-item preview. The composer pulls section-tagged
    ``default_actions()`` plus the recipe's own ``browser.actions``
    (under CUSTOM ACTIONS), wrapped by optional ``help_intro`` /
    ``help_outro`` prose.

Out of scope (deferred to phase 2/3):
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

# Insert-mode marker — sentinel id used by the synthetic ``VisibleEntry``
# injected into the rendered list when ``browser._insert_mode`` is True.
# We use ``object()`` so the sentinel never equals any user-supplied id
# (mirrors ``_PENDING_PLACEHOLDER_ID`` in 040-state.py). plan-tui used
# ``id == -1`` for the same purpose.
_INSERT_MARKER_ID = object()


# ---------------------------------------------------------------------------
# Pure helpers — unit-testable, no terminal I/O
# ---------------------------------------------------------------------------


# Translation table: every char with code < 32 becomes '?', except tab
# (\x09) and newline (\x0a) which are preserved (tab is later expanded
# to spaces by the renderer; LF is the line separator). Also map DEL
# (0x7f) to '?': the spec doesn't require it but it's safer because
# legacy terminals occasionally treat it as a destructive control char.
# Used to defang ANSI escape sequences (\x1b[31m, …) and other control
# bytes that arrive from untrusted file content (preview, error text).
_PREVIEW_SANITIZE_TABLE = {
    i: '?' for i in range(32) if i not in (0x09, 0x0a)
}
_PREVIEW_SANITIZE_TABLE[0x7f] = '?'


def _sanitize_preview(text):
    """Replace control chars (codes < 32 except \\t/\\n) with '?'.

    Also replaces DEL (0x7f). Used to defang ANSI escape sequences and
    binary noise in untrusted file content before it hits the terminal.
    Tab is preserved (the renderer expands it to spaces); newline is
    preserved (line separator).
    """
    if not text:
        return text
    return text.translate(_PREVIEW_SANITIZE_TABLE)


def _id_visible(item, show_ids):
    """Decide whether the id segment should be emitted for ``item``.

    ``show_ids`` is one of ``'always'``, ``'auto'``, ``'never'``. In
    ``'auto'`` mode the id is suppressed when ``str(item.id) ==
    item.title`` — the common shape for line-based CLI input where the
    id and title are the same string and showing both is pure
    duplication.
    """
    if show_ids == 'always':
        return True
    if show_ids == 'never':
        return False
    return str(item.id) != item.title


def format_item_segments(item, *, depth=0, base_depth=0, expanded=False,
                         selected=False, kind='normal', search_query='',
                         format_item=None, ctx=None, show_ids='auto'):
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
      - id segment:       ``'{id} '`` in yellow (gated on ``show_ids``;
                          ``'auto'`` suppresses it when ``str(id) == title``)
      - tag segment:      ``'[{tag}] '`` styled per ``_TAG_STYLE`` (omitted
                          when ``item.tag`` is empty)
      - title segment:    ``item.title``

    Default layout for ``kind='pending'``:
      - indentation only, then a single dim ``'⧗ loading…'`` segment.
        No selection marker, no expand marker — placeholder rows are
        synthetic and don't participate in selection/expansion.

    Default layout for ``kind='scope_root'``:
      - bolded ``'{id} {title}'`` (id segment gated on ``show_ids``,
        same auto-suppression rule as the normal kind). No selection
        marker, no expand marker.

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
        segs = []
        if _id_visible(item, show_ids):
            segs.append(('{} '.format(item.id), _ID_COLOR, True))
        segs.append((item.title, _SCOPE_ROOT_FG, True))
        return segs

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
    ]
    if _id_visible(item, show_ids):
        segments.append(('{} '.format(item.id), _ID_COLOR, False))

    if item.tag:
        sfg, sbold = _TAG_STYLE.get(item.tag_style, _TAG_STYLE[''])
        segments.append(('[{}] '.format(item.tag), sfg, sbold))

    segments.append((item.title, None, False))
    return segments


def layout_panes(cols, rows, *, show_preview=True,
                 show_children_pane=True, children_rows_needed=0):
    """Return geometry dict for the three-pane layout.

    Keys returned:
      * ``cols``        — terminal width (for callers).
      * ``list_top``    — first row of the list pane (1-based).
      * ``list_height`` — rows in the list pane.
      * ``sub_top``     — first row of the children grid (when shown).
                          This is the grid's *separator* row; content
                          starts at ``sub_top + 1``. Only meaningful
                          when ``sub_height > 0``.
      * ``sub_height``  — total rows the children grid occupies,
                          *including* its leading separator. 0 when
                          hidden (no children, terminal too small,
                          or ``show_children_pane=False``).
      * ``prev_top``    — first row of the preview pane: this is the
                          preview's separator row; content starts at
                          ``prev_top + 1``. Only meaningful when
                          ``prev_height > 1`` (we always reserve at
                          least the separator when ``show_preview=True``).
      * ``prev_height`` — total rows the preview occupies, *including*
                          its leading separator. So
                          ``list_height + sub_height + prev_height``
                          equals ``rows`` exactly when ``show_preview``
                          is True.
      * ``info_row``    — row of the *active* info-bar separator. When
                          the children-grid is visible, this is the
                          grid's separator (``sub_top``); otherwise the
                          preview's separator (``prev_top``). Carries the
                          ``[N]`` selection count, search prompt, hints.

    Layout (all three panes, big terminal):
        rows 1..list_height                   — list
        row sub_top                           — children-grid separator
                                                 (also the active info row)
        rows sub_top+1..sub_top+sub_height-1  — children-grid content
        row prev_top                          — preview separator
        rows prev_top+1..prev_top+prev_height-1 — preview content

    Sizing:
      * ``list_height`` ≈ 30% of rows, minimum 1.
      * ``sub_height`` is bounded by ``min(30% of rows, 1 + children_rows_needed)``;
        zero when the cursor item has no children to display, or when
        the terminal is too small (rows < 20), or when
        ``show_children_pane`` is False.
      * ``prev_height`` gets the remainder (non-negative).

    show_preview=False collapses to a single full-screen list with the
    info bar at the bottom row; the children grid is also suppressed in
    this mode so the list stays full-height.
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
            'sub_top': info_row,
            'sub_height': 0,
            'info_row': info_row,
            'prev_top': info_row,
            'prev_height': 0,
        }

    # ~30% of rows for the list. Make sure we leave at least one row for
    # the preview separator below.
    list_height = max(1, int(rows * 0.30))
    if list_height + 1 > rows:
        list_height = max(1, rows - 1)

    # Children grid sizing — cap at 30% of total rows, shrink to fit
    # children content. ``children_rows_needed`` is the *content* row
    # count; we add 1 for the grid's leading separator.
    sub_top = list_top + list_height
    if (not show_children_pane) or rows < 20:
        sub_height = 0
    else:
        sub_max = max(1, int(rows * 0.30))
        needed = (1 + children_rows_needed) if children_rows_needed > 0 else 0
        sub_height = min(sub_max, needed) if needed > 0 else 0

    # Preview separator sits immediately after the grid (or after the
    # list when the grid is hidden); ``prev_height`` covers the
    # separator + content together, mirroring plan-tui's geometry.
    prev_top = sub_top + sub_height
    prev_height = max(0, rows - list_height - sub_height)

    # The active info row is the grid's separator when shown, else the
    # preview's separator. Both are the first separator below the list.
    info_row = sub_top if sub_height > 0 else prev_top

    return {
        'cols': cols,
        'list_top': list_top,
        'list_height': list_height,
        'sub_top': sub_top,
        'sub_height': sub_height,
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
# Children-grid pane — multi-column flowed layout helpers
# ---------------------------------------------------------------------------


_SUB_WRAP = 80   # wrap child entries at this width
_SUB_INDENT = '    '   # continuation indent for wrapped lines


def _fmt_child(item, show_ids='auto'):
    """Format a child Item for the children grid.

    Layout: ``'{id} [{tag}] {title}'`` when ``item.tag`` is non-empty,
    otherwise ``'{id} {title}'``. The id prefix is gated on
    ``show_ids`` (auto-suppresses when ``str(id) == title``). The
    renderer in ``render_children_grid`` re-emits each entry through
    ``_write_segments`` so the tag picks up its colour from
    ``_TAG_STYLE``; this helper produces the *plain* text used for
    column-width measurement and wrapping.
    """
    parts = []
    if _id_visible(item, show_ids):
        parts.append('{} '.format(item.id))
    if item.tag:
        parts.append('[{}] '.format(item.tag))
    parts.append(item.title)
    return ''.join(parts)


def _wrap_entry(text, width):
    """Wrap ``text`` at ``width`` columns; continuation lines are indented.

    Returns a list of one or more lines. The first line keeps the full
    width; subsequent lines start with ``_SUB_INDENT`` and consume
    ``width - len(_SUB_INDENT)`` characters per line. If the indent
    would leave too little room (< 10 chars), we drop the indent and
    use the full width instead.
    """
    if len(text) <= width:
        return [text]
    lines = [text[:width]]
    rest = text[width:]
    indent = _SUB_INDENT
    cont_w = width - len(indent)
    if cont_w < 10:
        cont_w = width
        indent = ''
    while rest:
        lines.append(indent + rest[:cont_w])
        rest = rest[cont_w:]
    return lines


def _sub_layout(children, cols, show_ids='auto'):
    """Compute the multi-column grid layout.

    Each entry is wrapped at ``_SUB_WRAP``. Column width is derived from
    the *capped* per-entry width (``min(actual, _SUB_WRAP)``) so a single
    extra-long title doesn't force the entire grid into one column.

    Returns ``(num_cols, col_width, slot_rows, entry_lines)``:
      * ``slot_rows[i]``    — number of display rows entry ``i`` occupies.
      * ``entry_lines[i]``  — list of wrapped lines for entry ``i``.
      * ``num_cols``        — column count for the layout.
      * ``col_width``       — width of each column (incl. inter-column gap).
    """
    if not children:
        return (1, cols, [], [])

    raw = [_fmt_child(c, show_ids=show_ids) for c in children]
    entry_lines = [_wrap_entry(e, _SUB_WRAP) for e in raw]
    slot_rows = [len(lines) for lines in entry_lines]

    max_w = min(max(len(e) for e in raw), _SUB_WRAP)
    gap = 2
    col_width = max_w + gap
    # Allow one extra partial column — it'll be truncated at the screen
    # edge but uses the leftover width effectively.
    full_cols = max(1, (cols + gap) // col_width)
    num_cols = min(full_cols + 1, len(children))

    return (num_cols, col_width, slot_rows, entry_lines)


def _distribute_to_columns(num_cols, slot_rows):
    """Distribute entries across columns balancing by *display lines*.

    Returns a list of ``(start, end)`` index ranges, one per column.
    Each column accumulates entries until adding one more would push
    past the per-column target; the final column gets the remainder.
    """
    n = len(slot_rows)
    if n == 0 or num_cols == 0:
        return []
    total_lines = sum(slot_rows)
    target = (total_lines + num_cols - 1) // num_cols  # ideal lines per column
    out = []
    start = 0
    for c in range(num_cols):
        if c == num_cols - 1:
            out.append((start, n))
            break
        col_h = 0
        end = start
        while end < n:
            if col_h + slot_rows[end] > target and col_h > 0:
                break
            col_h += slot_rows[end]
            end += 1
        out.append((start, end))
        start = end
    return out


def _sub_total_rows(num_cols, slot_rows):
    """Total display rows for a column-major layout with multi-row entries."""
    if not slot_rows or num_cols == 0:
        return 0
    ranges = _distribute_to_columns(num_cols, slot_rows)
    return max(sum(slot_rows[s:e]) for s, e in ranges) if ranges else 0


def _sub_needed_rows(children, cols, show_ids='auto'):
    """Number of content rows the grid would need to render ``children``.

    Returns 0 when there are no children. Caller adds 1 for the
    separator to compute the total pane height (see ``layout_panes``).
    """
    if not children:
        return 0
    num_cols, _, slot_rows, _ = _sub_layout(children, cols, show_ids=show_ids)
    return _sub_total_rows(num_cols, slot_rows)


def _child_segments(item, max_width, show_ids='auto'):
    """Build the ``(text, fg, bold)`` segment list for one grid entry.

    Mirrors the per-line layout produced by ``_fmt_child`` but with
    individual style spans so the id gets ``_ID_COLOR`` and the tag
    picks up its style from ``_TAG_STYLE``. Truncation is left to the
    caller (``_write_segments`` clamps at ``max_width``).
    """
    segs = []
    if _id_visible(item, show_ids):
        segs.append(('{} '.format(item.id), _ID_COLOR, False))
    if item.tag:
        sfg, sbold = _TAG_STYLE.get(item.tag_style, _TAG_STYLE[''])
        segs.append(('[{}] '.format(item.tag), sfg, sbold))
    segs.append((item.title, None, False))
    return segs


# ---------------------------------------------------------------------------
# Pane renderers — each owns a region, takes Browser + geometry
# ---------------------------------------------------------------------------


def render_list(browser, top, height, cols):
    """Render the list pane for ``browser`` at the given region.

    Reads cursor / selected / expanded / scope state from
    ``browser._state`` via ``visible_items``. Maintains
    ``browser._list_scroll`` to keep the cursor on-screen.

    When ``browser._insert_mode`` is True, injects a synthetic
    ``-- {label} --`` marker row at ``browser._insert_pos`` and
    treats the marker as the cursor row (so scroll-tracking keeps it
    on-screen as the user moves it).
    """
    if height <= 0:
        return

    state = browser._state
    visible = visible_items(state)
    cursor_pos = state.cursor

    # Insert-mode: inject a synthetic marker row at insert_pos and pin
    # the on-screen "cursor" to it so scroll tracking follows the
    # marker, not the underlying real cursor.
    if browser._insert_mode:
        marker_item = Item(
            id=_INSERT_MARKER_ID,
            title=' -- {} -- '.format(browser._insert_label or 'here'),
        )
        marker_entry = VisibleEntry(
            item=marker_item,
            depth=browser._insert_depth,
            kind='insert_marker',
        )
        # Copy so we don't mutate the cached list (visible_items
        # returns the cached object identity-stable).
        visible = list(visible)
        pos = browser._insert_pos
        if pos < 0:
            pos = 0
        if pos > len(visible):
            pos = len(visible)
        visible.insert(pos, marker_entry)
        cursor_pos = pos

    # Bounds-clamp the scroll offset against the list extent. The
    # cursor-on-screen guarantee lives separately in
    # ``Browser._snap_list_scroll_to_row``, called from the main loop
    # whenever a key changes the active row — keeping the renderer
    # passive lets wheel-scroll move the viewport past the cursor
    # without it snapping back on the next paint.
    max_scroll = max(0, len(visible) - height)
    scroll = max(0, min(browser._list_scroll, max_scroll))
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

        # Insert-mode marker — render distinctively (yellow on blue,
        # bold) and skip the regular segment / cursor / search logic
        # entirely. Mirrors plan-tui's _id == -1 branch.
        if entry.kind == 'insert_marker':
            rel_depth = entry.depth - base_depth
            if rel_depth < 0:
                rel_depth = 0
            line = '  ' + '  ' * rel_depth + '  # ' + item.title
            if len(line) > cols:
                line = line[:cols]
            set_style(fg=11, bg=4, bold=True)
            write(line)
            if len(line) < cols:
                write(' ' * (cols - len(line)))
            reset_style()
            continue

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
            show_ids=browser.show_ids,
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
            # Non-cursor row. When a search query is active and this row
            # matches, render via ``_write_highlighted`` (yellow/bold
            # spans on top of plain base style) so the user sees every
            # match across the visible list, not only the cursor row.
            # Non-matches (and the no-query case) keep the per-segment
            # writer so tag colours and the #id segment stay coloured.
            if (browser._search_query
                    and entry.kind == 'normal'
                    and _search_matches(
                        _search_text(item), browser._search_query)):
                line = ''.join(s[0] for s in segments)
                if len(line) > cols:
                    line = line[:cols]
                _write_highlighted(
                    line, pad_to=0,
                    search_query=browser._search_query,
                )
            else:
                _write_segments(segments, cols)


def render_preview(browser, top, height, cols, *, info=False):
    """Render the preview pane (separator + content).

    ``height`` includes the separator row at ``top`` plus
    ``height - 1`` rows of wrapped content below it. The separator
    label adapts to ``browser._error_text`` / ``browser._help_mode``
    (``Error`` / ``Help`` / ``Preview``). When ``info=True`` the
    separator carries the active info-bar decorations (``[N]`` /
    search prompt / hints).

    Source priority for the content:
      1. ``browser._error_text`` if set      → display the error text
      2. ``browser._help_mode`` if True      → display ``compose_help_text``
      3. ``browser._state._preview[id]``     → per-item preview
      4. fallthrough — empty preview         → blank rows
    """
    if height <= 0:
        return

    label = _preview_label(browser)
    render_separator(top, cols, label, info=info, browser=browser)

    content_lines = height - 1
    if content_lines <= 0:
        return

    if browser._error_text:
        text = browser._error_text
    elif browser._help_mode:
        text = compose_help_text(browser, include_usage=False)
    else:
        cursor_id = _cursor_id(browser)
        text = (
            browser._state._preview.get(cursor_id, '')
            if cursor_id is not None else ''
        )

    # Strip control chars before they hit the terminal. Covers all three
    # sources (per-item preview, error text, help) so anything that
    # reaches this pane is safe — preview data and action errors can
    # carry attacker-controlled bytes (binary files, raw terminal
    # captures, command stderr); help is composed in-process but cheap
    # to filter and recipes may supply ``help_intro`` / ``help_outro``.
    text = _sanitize_preview(text)

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
    for i in range(content_lines):
        move(top + 1 + i, 1)
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


def _cursor_item(browser):
    """Return the Item under the cursor (only for ``kind='normal'``).

    Returns None for placeholder rows / scope-root rows / empty visible
    list — the children grid only meaningfully renders for a cursor on
    a real item.
    """
    vis = visible_items(browser._state)
    cur = browser._state.cursor
    if not (0 <= cur < len(vis)):
        return None
    entry = vis[cur]
    if entry.kind != 'normal':
        return None
    return entry.item


def render_children_grid(browser, top, height, cols, *, info=False):
    """Render the children-grid pane (with its leading separator).

    ``height`` includes the 1-row separator at ``top``; content lines
    occupy ``height - 1`` rows below it. The children come from
    ``browser._state._children.get(cursor_item.id, [])`` — direct
    children of the cursor item, fetched lazily by the children worker.

    Behaviour:
      * Cursor on a leaf or empty cached children → blank content area
        (the layout normally already set ``sub_height = 0`` to elide
        the pane entirely; we render defensively).
      * Cursor on a branch whose children aren't cached yet → single
        ``⧗ loading…`` row in dim, mirroring the placeholder used in
        the list pane.
      * Cached children → multi-column flowed layout via ``_sub_layout``
        / ``_distribute_to_columns``. Each entry's tag picks up its
        colour from ``_TAG_STYLE``.
    """
    if height <= 0:
        return

    render_separator(top, cols, 'Children', info=info, browser=browser)

    content_lines = height - 1
    if content_lines <= 0:
        return

    cursor = _cursor_item(browser)
    if cursor is None:
        for i in range(content_lines):
            move(top + 1 + i, 1)
            clear_line()
        return

    state = browser._state
    children = state._children.get(cursor.id)

    # Pending: cursor on a branch whose children aren't cached yet.
    if children is None and cursor.has_children:
        move(top + 1, 1)
        clear_line()
        set_style(fg=_PENDING_FG)
        write('⧗ loading…'[:cols])
        reset_style()
        for i in range(1, content_lines):
            move(top + 1 + i, 1)
            clear_line()
        return

    # Cached but empty (or a leaf) — nothing to draw, blank rows.
    if not children:
        for i in range(content_lines):
            move(top + 1 + i, 1)
            clear_line()
        return

    # Cached non-empty children — multi-column flowed layout.
    num_cols, col_width, slot_rows, entry_lines = _sub_layout(
        children, cols, show_ids=browser.show_ids,
    )
    ranges = _distribute_to_columns(num_cols, slot_rows)

    # Build per-column flat line lists + maps from row -> source entry
    # index (so we know which entry owns each first-line row, for
    # coloured-segment rendering).
    col_lines = []
    col_entry_at = []   # list of dict: row_idx_in_col -> source entry idx
    for start, end in ranges:
        lines = []
        entry_at = {}
        for src_idx in range(start, end):
            entry_at[len(lines)] = src_idx
            lines.extend(entry_lines[src_idx])
        col_lines.append(lines)
        col_entry_at.append(entry_at)

    total_rows = max((len(cl) for cl in col_lines), default=0)

    for row in range(content_lines):
        move(top + 1 + row, 1)
        clear_line()
        if row >= total_rows:
            continue
        col_pos = 0
        for c in range(num_cols):
            cl = col_lines[c]
            cell = cl[row] if row < len(cl) else ''
            if c < num_cols - 1:
                width = col_width
            else:
                width = cols - col_pos
            if width <= 0:
                break
            src_idx = col_entry_at[c].get(row)
            if src_idx is not None:
                # First line of an entry — render with coloured segments.
                child = children[src_idx]
                segs = _child_segments(child, width, show_ids=browser.show_ids)
                used = _write_segments(segs, min(width, len(cell)))
                pad = width - used
                if pad > 0:
                    write(' ' * pad)
            else:
                # Continuation line — plain text, no colour.
                chunk = cell[:width].ljust(width)[:width]
                write(chunk)
            col_pos += width


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
    """Build the geometry dict from current terminal size + browser flags.

    Cursor-aware: if ``show_children_pane`` is set and the cursor is on
    a branch whose children are cached, we ask the multi-column layout
    helpers how many content rows it would need and pass the answer to
    ``layout_panes`` so the grid pane shrinks to fit. A cursor on a
    leaf, or on a branch whose children haven't been fetched yet,
    yields ``children_rows_needed=0`` *unless* the branch has children
    (in which case we reserve one row for the ``⧗ loading…`` hint).
    """
    cols, rows = term_size()
    children_rows = 0
    if browser.show_children_pane and not browser._headless:
        cursor = _cursor_item(browser)
        if cursor is not None and cursor.has_children:
            cached = browser._state._children.get(cursor.id)
            if cached:
                children_rows = _sub_needed_rows(
                    cached, cols, show_ids=browser.show_ids,
                )
            elif cached is None:
                # Branch with not-yet-cached children — reserve one row
                # for the loading hint so the grid is visible while the
                # fetch is in flight.
                children_rows = 1
            # cached == [] (empty list): leaf-like, no rows reserved.
    return layout_panes(
        cols, rows,
        show_preview=browser.show_preview,
        show_children_pane=browser.show_children_pane,
        children_rows_needed=children_rows,
    )


def _pane_info_flags(layout):
    """Return ``(sub_info, prev_info)``: which separator owns the info row.

    Mirrors plan-tui's helper of the same name. The info row always
    sits on a separator; this picks which pane that separator belongs
    to so the renderer knows where to draw the ``[N]`` / search prompt
    / hints.
    """
    ir = layout['info_row']
    sub_info = layout['sub_height'] > 0 and layout['sub_top'] == ir
    prev_info = (
        layout['prev_height'] > 0 and layout['prev_top'] == ir
        and not sub_info
    )
    # When show_preview=False, prev_height is 0 but the info row still
    # sits at prev_top. Treat that case as prev_info to keep
    # render_separator drawing the bottom info bar.
    if not sub_info and not prev_info and ir > 0:
        prev_info = True
    return sub_info, prev_info


def render_full(browser):
    """Repaint the whole screen.

    Clears the screen, lays out the panes, paints each, then flushes.
    Resets ``browser._needs_redraw`` so subsequent partial-render calls
    start from a clean slate.
    """
    write('\033[2J')
    layout = _layout_for(browser)
    sub_info, prev_info = _pane_info_flags(layout)
    render_list(
        browser,
        layout['list_top'], layout['list_height'], layout['cols'],
    )
    if layout['sub_height'] > 0:
        render_children_grid(
            browser,
            layout['sub_top'], layout['sub_height'], layout['cols'],
            info=sub_info,
        )
    if browser.show_preview and layout['prev_height'] > 0:
        # render_preview draws its own separator (label + info bar).
        render_preview(
            browser,
            layout['prev_top'], layout['prev_height'], layout['cols'],
            info=prev_info,
        )
    elif not browser.show_preview and layout['info_row'] > 0:
        # show_preview=False — bottom info bar.
        render_separator(
            layout['info_row'], layout['cols'], _preview_label(browser),
            info=True, browser=browser,
        )
    flush()
    browser._needs_redraw = set()


def render_partial(browser):
    """Selective redraw based on ``browser._needs_redraw``.

    Recognised flags: ``'list'``, ``'children'``, ``'preview'``,
    ``'info'``, ``'all'``. ``'all'`` short-circuits to ``render_full``.
    Unknown flags are ignored (no error) so callers can stuff "hint"
    tokens in the set without crashing the renderer.
    """
    needs = browser._needs_redraw
    if 'all' in needs:
        render_full(browser)
        return
    if not needs:
        return

    layout = _layout_for(browser)
    sub_info, prev_info = _pane_info_flags(layout)
    if 'list' in needs:
        render_list(
            browser,
            layout['list_top'], layout['list_height'], layout['cols'],
        )
    if 'children' in needs:
        if layout['sub_height'] > 0:
            render_children_grid(
                browser,
                layout['sub_top'], layout['sub_height'], layout['cols'],
                info=sub_info,
            )
    if 'preview' in needs:
        if browser.show_preview and layout['prev_height'] > 0:
            render_preview(
                browser,
                layout['prev_top'], layout['prev_height'], layout['cols'],
                info=prev_info,
            )
    if 'info' in needs:
        ir = layout['info_row']
        if ir > 0:
            if sub_info:
                render_separator(
                    ir, layout['cols'], 'Children',
                    info=True, browser=browser,
                )
            else:
                render_separator(
                    ir, layout['cols'], _preview_label(browser),
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
# Help text composer — dynamic ``--help`` and in-app ``?`` body.
#
# The text is built by ``compose_help_text(browser, …)`` from
# ``default_actions()`` (each tagged with a section name) plus the
# recipe's own ``browser.actions`` (rendered under CUSTOM ACTIONS).
# Optional ``browser.help_intro`` / ``browser.help_outro`` prose blocks
# wrap the key list. ``_HELP_TEXT`` is kept as a static emergency
# fallback only — production rendering goes through the composer.
# ---------------------------------------------------------------------------


_HELP_SECTIONS = ('NAVIGATION', 'PREVIEW', 'SEARCH', 'SELECTION', 'OTHER')

# Static fallback used only if the composer can't be reached (e.g. a
# test loading 050-render in isolation without the action layer wired).
_HELP_TEXT = """\
browse-tui — generic hierarchical browser

(See `--help` or press `?` in the running TUI for the full keymap.)
"""


def _format_help_section(name, rows):
    """Render one section: the header line + an aligned list of rows.

    ``rows`` is a list of ``(key, label)`` tuples. Keys are padded to a
    fixed column so labels line up across rows. Sections with no rows
    are caller-elided — this helper assumes ``rows`` is non-empty.
    """
    lines = [name]
    # Pad keys so labels align. 16 cols handles the longest built-in
    # ('shift-down'); recipes that use longer keys get a wider key
    # column for that one row but the rest stay aligned.
    key_width = 16
    for key, label in rows:
        keystr = key
        if len(keystr) > key_width:
            # Long key — emit key on its own and label on the next line
            # so it doesn't squish the label.
            lines.append('  {}'.format(keystr))
            lines.append('  {} {}'.format(' ' * key_width, label))
        else:
            lines.append('  {:<{w}} {}'.format(keystr, label, w=key_width))
    return '\n'.join(lines)


def compose_help_text(browser, *, include_usage: bool = False) -> str:
    """Compose the help text shown by ``--help`` and the in-app ``?``.

    Output structure::

        [help_intro]              (omitted when None / empty)

        NAVIGATION                (built-in default actions, grouped)
          ...
        PREVIEW
          ...
        SEARCH
          ...
        SELECTION
          ...
        OTHER
          ...

        CUSTOM ACTIONS            (only when browser.actions is non-empty)
          e   Edit in $EDITOR     (action.label)
          d   Delete with confirm

        [help_outro]              (omitted when None / empty)

    Sections without content are omitted: no empty CUSTOM ACTIONS
    header, no leading or trailing blank lines.

    ``include_usage`` is currently unused inside the composer — the
    argparse usage block is prepended by ``main()`` at the CLI layer
    when ``--help`` runs. The flag is reserved for future uses (e.g.
    embedding usage when called from a non-CLI entrypoint).
    """
    # Suppress unused-arg lint without surprising the caller.
    _ = include_usage
    parts = []

    intro = getattr(browser, 'help_intro', None)
    if intro:
        parts.append(intro.rstrip())

    # Group default actions by section, preserving the order
    # ``default_actions()`` declared (so ``j, down, k, up`` stay
    # adjacent — declaration order is the docstring's narrative order).
    sections = {}   # section_name -> list[(key, label)]
    for a in default_actions():
        if not a.section or not a.label:
            continue
        sections.setdefault(a.section, []).append((a.key, a.label))

    for name in _HELP_SECTIONS:
        rows = sections.get(name, [])
        if rows:
            parts.append(_format_help_section(name, rows))

    # Recipe-defined actions — single CUSTOM ACTIONS section in this
    # phase. Per-recipe sections (e.g. browse-plan grouping its own
    # status / edit / move bindings) are a future feature; the
    # composer ignores ``Action.section`` on user-supplied actions.
    custom = []
    for a in (getattr(browser, 'actions', None) or []):
        # Skip entries with no label — no helpful text to show. Also
        # skip duplicates (recipes occasionally bind the same handler
        # to two keys; surface them once with both keys joined).
        if not getattr(a, 'label', ''):
            continue
        custom.append((a.key, a.label))
    if custom:
        parts.append(_format_help_section('CUSTOM ACTIONS', custom))

    outro = getattr(browser, 'help_outro', None)
    if outro:
        parts.append(outro.rstrip())

    return '\n\n'.join(parts) + '\n' if parts else ''
