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


class Rect:
    """An inclusive-top, exclusive-right/bottom rectangle in 1-based screen coords.

    Use ``left, top, right, bottom`` where ``right`` and ``bottom`` are
    *exclusive* (so ``Rect(left=1, top=1, right=81, bottom=25)`` covers
    columns 1..80 and rows 1..24, i.e. width=80, height=24). Coordinates
    are 1-based to match ``move(row, col)`` in 020-terminal.

    Implemented as a small immutable class (rather than NamedTuple) so
    it can carry ``.width`` / ``.height`` properties and remain dict-
    free for cheap attribute access in hot paths.
    """

    __slots__ = ('left', 'top', 'right', 'bottom')

    def __init__(self, left, top, right, bottom):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top

    def __eq__(self, other):
        if not isinstance(other, Rect):
            return NotImplemented
        return (self.left == other.left and self.top == other.top
                and self.right == other.right and self.bottom == other.bottom)

    def __hash__(self):
        return hash((self.left, self.top, self.right, self.bottom))

    def __repr__(self):
        return ('Rect(left={}, top={}, right={}, bottom={})'
                .format(self.left, self.top, self.right, self.bottom))


def point_in_rect(row, col, rect):
    """Return True iff ``(row, col)`` lies inside ``rect``.

    Uses :class:`Rect`'s inclusive-top, exclusive-right/bottom convention:
    a point is inside iff ``rect.top <= row < rect.bottom`` AND
    ``rect.left <= col < rect.right``. Returns ``False`` when ``rect`` is
    ``None`` so callers can pass a ``layout.get('children')`` (which may
    be ``None`` when the pane is hidden) without a separate guard.

    Coordinates are 1-based to match the rest of the render layer
    (mouse events from ``020-terminal.read_key`` arrive 1-based).
    """
    if rect is None:
        return False
    return (rect.top <= row < rect.bottom
            and rect.left <= col < rect.right)


# Minimum content rows for the preview pane (separator + 1 content row).
# Used by every layout variant to drop the preview when room is too tight.
_PREV_MIN = 2

# Children pane is capped at this fraction of its sub-area along the
# relevant axis (height for h/m/pc, width for v).
_CHILDREN_CAP_FRAC = 0.25


def _layout_horizontal(cols, rows, *, list_ratio, show_preview, show_children,
                       children_rows_needed):
    """Stacked layout: list / children / preview, info bar above children/preview.

    Mirrors the historic browse-tui geometry: list at the top, children
    grid below it (when active), preview below the grid. The info bar
    sits on the topmost separator below the list (i.e. the grid's
    separator if shown, otherwise the preview's). When the preview is
    hidden, the list takes the full screen minus a one-row info bar.

    Returns a dict with keys ``list``, ``children``, ``preview``,
    ``sep_main``, ``sep_inner``, ``info_bar`` — each a ``Rect`` or
    ``None``. ``sep_main`` and ``sep_inner`` are ``None`` for layout h
    (separators are folded into the children/preview pane Rects whose
    first row IS the separator, mirroring plan-tui's geometry).
    """
    full_left, full_right = 1, cols + 1

    if not show_preview:
        list_height = max(1, rows - 1)
        list_rect = Rect(full_left, 1, full_right, 1 + list_height)
        info_bar = Rect(full_left, list_rect.bottom,
                        full_right, list_rect.bottom + 1)
        return {
            'list': list_rect,
            'children': None,
            'preview': None,
            'sep_main': None,
            'sep_inner': None,
            'info_bar': info_bar,
        }

    list_height = max(1, int(rows * list_ratio))

    # Children grid sizing — capped at 25% of rows, shrink to fit needed
    # content. ``children_rows_needed`` is content rows; +1 for separator.
    if (not show_children) or rows < 20:
        sub_height = 0
    else:
        sub_max = max(1, int(rows * _CHILDREN_CAP_FRAC))
        needed = (1 + children_rows_needed) if children_rows_needed > 0 else 0
        sub_height = min(sub_max, needed) if needed > 0 else 0

    # Honour preview min (separator + 1 content row) when the terminal
    # is large enough; otherwise fall back to "leave 1 row below list".
    if rows - sub_height >= 1 + _PREV_MIN:
        max_list = rows - sub_height - _PREV_MIN
        if list_height > max_list:
            list_height = max_list
    elif list_height + 1 > rows:
        list_height = max(1, rows - 1)

    list_rect = Rect(full_left, 1, full_right, 1 + list_height)
    sub_top = list_rect.bottom
    prev_top = sub_top + sub_height
    prev_height = max(0, rows - list_height - sub_height)

    children_rect = (
        Rect(full_left, sub_top, full_right, sub_top + sub_height)
        if sub_height > 0 else None
    )
    preview_rect = (
        Rect(full_left, prev_top, full_right, prev_top + prev_height)
        if prev_height > 0 else None
    )

    # Active info row: grid separator if shown, else preview separator.
    info_top = sub_top if sub_height > 0 else prev_top
    info_bar = Rect(full_left, info_top, full_right, info_top + 1)

    return {
        'list': list_rect,
        'children': children_rect,
        'preview': preview_rect,
        'sep_main': None,
        'sep_inner': None,
        'info_bar': info_bar,
    }


def _layout_vertical(cols, rows, *, list_ratio, show_preview, show_children,
                     children_rows_needed, children_cols_needed=0):
    """3-column layout: ``list | children | preview``, info bar at bottom.

    Per #176 Alt-1 (vertical) is a true 3-column shape with the children
    column on the LEFT of the preview, occupying the FULL HEIGHT of the
    body (above the info bar). Each child name is rendered ONE PER ROW
    (vertical list) by ``render_children_list`` — distinct from the grid
    layout used by the other splits.

    Children column width:
      * Provided by ``children_cols_needed`` (longest child name + small
        padding, computed by ``_layout_for`` from cached children).
      * When ``children_cols_needed`` is 0 but ``children_rows_needed >
        0`` we fall back to a sensible default (``max(8, …)``) so the
        column is still visible during the brief loading window.
      * Capped at 25% of the right-of-list area width and at
        ``right_area_width - 1`` so the preview always retains at least
        one content column.

    Falls back to layout 'h' when preview is hidden (a vertical split
    with no preview would just be the list).
    """
    full_left, full_right = 1, cols + 1
    info_top = rows
    info_bar = Rect(full_left, info_top, full_right, info_top + 1)
    body_bottom = info_top  # exclusive
    body_top = 1
    body_height = body_bottom - body_top

    if not show_preview or body_height < 1:
        return _layout_horizontal(
            cols, rows,
            list_ratio=list_ratio, show_preview=show_preview,
            show_children=show_children,
            children_rows_needed=children_rows_needed,
        )

    list_w = max(1, int(cols * list_ratio))
    # Reserve 1 col for sep_main; the right area must keep at least 1
    # col of preview content.
    if cols < list_w + 1 + 1:
        return _layout_horizontal(
            cols, rows,
            list_ratio=list_ratio, show_preview=show_preview,
            show_children=show_children,
            children_rows_needed=children_rows_needed,
        )

    sep_main_left = full_left + list_w
    right_area_left = sep_main_left + 1
    right_area_right = full_right
    right_area_width = right_area_right - right_area_left
    list_rect = Rect(full_left, body_top, sep_main_left, body_bottom)
    sep_main_rect = Rect(sep_main_left, body_top,
                         sep_main_left + 1, body_bottom)

    children_rect = None
    sep_inner_rect = None
    # Children column requires:
    #   * children to show (show_children flag + non-zero rows-needed
    #     hint, repurposed as a "non-zero means present" sentinel),
    #   * room for sep_inner (1 col) + at least 1 col of children +
    #     1 col of preview content within the right area.
    if (show_children and children_rows_needed > 0
            and body_height >= 1 and right_area_width >= 3):
        # Width budget — start from the requested cols, fall back to a
        # default when the caller didn't measure (e.g. ``loading…``).
        if children_cols_needed > 0:
            requested_w = children_cols_needed
        else:
            requested_w = max(8, right_area_width // 4)
        # Cap at 25% of right area width (at least 1).
        cap_w = max(1, right_area_width // 4)
        # And ensure 1 col for sep_inner + 1 col of preview content.
        max_w = right_area_width - 2
        if max_w < 1:
            max_w = 0
        ch_w = min(requested_w, cap_w, max_w)
        if ch_w >= 1:
            children_left = right_area_left
            children_right = children_left + ch_w
            sep_inner_left = children_right
            preview_left = sep_inner_left + 1
            children_rect = Rect(children_left, body_top,
                                 children_right, body_bottom)
            sep_inner_rect = Rect(sep_inner_left, body_top,
                                  sep_inner_left + 1, body_bottom)
            preview_rect = Rect(preview_left, body_top,
                                right_area_right, body_bottom)
            return {
                'list': list_rect,
                'children': children_rect,
                'preview': preview_rect,
                'sep_main': sep_main_rect,
                'sep_inner': sep_inner_rect,
                'info_bar': info_bar,
            }

    preview_rect = Rect(right_area_left, body_top,
                        right_area_right, body_bottom)
    return {
        'list': list_rect,
        'children': children_rect,
        'preview': preview_rect,
        'sep_main': sep_main_rect,
        'sep_inner': sep_inner_rect,
        'info_bar': info_bar,
    }


def _layout_mixed(cols, rows, *, list_ratio, show_preview, show_children,
                  children_rows_needed):
    """List + children stacked on the left, preview on the right.

    Within the left column: list on top, children on the bottom (when
    active). Children height is capped at 25% of the left column's
    height. Info bar at the bottom row. When the preview is hidden,
    falls back to layout 'h' (no vertical split makes sense without a
    second horizontal pane).
    """
    full_left, full_right = 1, cols + 1
    info_top = rows
    info_bar = Rect(full_left, info_top, full_right, info_top + 1)
    body_bottom = info_top
    body_top = 1
    body_height = body_bottom - body_top

    if not show_preview or body_height < 1:
        return _layout_horizontal(
            cols, rows,
            list_ratio=list_ratio, show_preview=show_preview,
            show_children=show_children,
            children_rows_needed=children_rows_needed,
        )

    left_w = max(1, int(cols * list_ratio))
    if cols < left_w + 1 + 1:
        # Too narrow for a vertical split; fall back to horizontal stack
        # with the same info-bar-at-bottom semantics handled by the
        # 'h' layout.
        return _layout_horizontal(
            cols, rows,
            list_ratio=list_ratio, show_preview=show_preview,
            show_children=show_children,
            children_rows_needed=children_rows_needed,
        )

    sep_main_left = full_left + left_w
    right_area_left = sep_main_left + 1
    right_area_right = full_right
    sep_main_rect = Rect(sep_main_left, body_top,
                         sep_main_left + 1, body_bottom)
    preview_rect = Rect(right_area_left, body_top,
                        right_area_right, body_bottom)

    # Within the left column: list on top, children on bottom.
    children_rect = None
    sep_inner_rect = None
    if show_children and children_rows_needed > 0 and body_height >= 3:
        cap = max(1, body_height // 4)
        ch_h = min(children_rows_needed, cap, body_height - 2)
        if ch_h >= 1:
            sep_inner_top = body_bottom - ch_h - 1
            children_top = sep_inner_top + 1
            list_rect = Rect(full_left, body_top,
                             sep_main_left, sep_inner_top)
            sep_inner_rect = Rect(full_left, sep_inner_top,
                                  sep_main_left, sep_inner_top + 1)
            children_rect = Rect(full_left, children_top,
                                 sep_main_left, body_bottom)
            return {
                'list': list_rect,
                'children': children_rect,
                'preview': preview_rect,
                'sep_main': sep_main_rect,
                'sep_inner': sep_inner_rect,
                'info_bar': info_bar,
            }

    list_rect = Rect(full_left, body_top, sep_main_left, body_bottom)
    return {
        'list': list_rect,
        'children': children_rect,
        'preview': preview_rect,
        'sep_main': sep_main_rect,
        'sep_inner': sep_inner_rect,
        'info_bar': info_bar,
    }


def _layout_preview_children(cols, rows, *, list_ratio, show_preview,
                             show_children, children_rows_needed):
    """List on the left, children + preview stacked on the right.

    Within the right column: children on top (when active), preview
    below. Children height is capped at 25% of the right column's
    height. Info bar at the bottom row. Falls back to layout 'h' when
    preview is hidden.
    """
    full_left, full_right = 1, cols + 1
    info_top = rows
    info_bar = Rect(full_left, info_top, full_right, info_top + 1)
    body_bottom = info_top
    body_top = 1
    body_height = body_bottom - body_top

    if not show_preview or body_height < 1:
        return _layout_horizontal(
            cols, rows,
            list_ratio=list_ratio, show_preview=show_preview,
            show_children=show_children,
            children_rows_needed=children_rows_needed,
        )

    left_w = max(1, int(cols * list_ratio))
    if cols < left_w + 1 + 1:
        return _layout_horizontal(
            cols, rows,
            list_ratio=list_ratio, show_preview=show_preview,
            show_children=show_children,
            children_rows_needed=children_rows_needed,
        )

    sep_main_left = full_left + left_w
    right_area_left = sep_main_left + 1
    right_area_right = full_right
    list_rect = Rect(full_left, body_top, sep_main_left, body_bottom)
    sep_main_rect = Rect(sep_main_left, body_top,
                         sep_main_left + 1, body_bottom)

    children_rect = None
    sep_inner_rect = None
    if show_children and children_rows_needed > 0 and body_height >= 3:
        cap = max(1, body_height // 4)
        ch_h = min(children_rows_needed, cap, body_height - 2)
        if ch_h >= 1:
            children_top = body_top
            children_bottom = body_top + ch_h
            sep_inner_top = children_bottom
            preview_top = sep_inner_top + 1
            children_rect = Rect(right_area_left, children_top,
                                 right_area_right, children_bottom)
            sep_inner_rect = Rect(right_area_left, sep_inner_top,
                                  right_area_right, sep_inner_top + 1)
            preview_rect = Rect(right_area_left, preview_top,
                                right_area_right, body_bottom)
            return {
                'list': list_rect,
                'children': children_rect,
                'preview': preview_rect,
                'sep_main': sep_main_rect,
                'sep_inner': sep_inner_rect,
                'info_bar': info_bar,
            }

    preview_rect = Rect(right_area_left, body_top,
                        right_area_right, body_bottom)
    return {
        'list': list_rect,
        'children': children_rect,
        'preview': preview_rect,
        'sep_main': sep_main_rect,
        'sep_inner': sep_inner_rect,
        'info_bar': info_bar,
    }


def layout_panes(cols, rows, *, split='h', show_preview=True,
                 show_children_pane=True, children_rows_needed=0,
                 children_cols_needed=0, list_ratio=0.30):
    """Dispatch the layout computation to a per-split helper.

    Returns a dict with keys ``list``, ``children``, ``preview``,
    ``sep_main``, ``sep_inner``, ``info_bar`` — each a ``Rect`` or
    ``None``. Coordinates are 1-based with exclusive ``right`` /
    ``bottom``.

    ``split`` controls the layout family:
      * ``'h'`` (default, current historic behaviour) — stacked panes
        with the info bar on the topmost active separator.
      * ``'v'`` — list on the left, children+preview stacked on the
        right; info bar at the bottom row. Children sits ABOVE preview
        in the right column, capped at 25% of the right area's height
        (#166: structurally identical to 'pc').
      * ``'m'`` — list+children stacked left, preview right; info bar
        at the bottom row. Children fills the bottom portion of the
        left column when active, capped at 25% of left height.
      * ``'pc'`` — list left, children+preview stacked right; info bar
        at the bottom row. Children fills the top portion of the right
        column when active, capped at 25% of right height.

    ``children_rows_needed`` is the number of content rows the children
    grid needs; the layout helpers cap it at 25% of the relevant
    sub-area (and 0 means "no children pane").

    Min-size degradation: each helper drops children first when its
    sub-area can't fit the minimums, then preview if even the list
    can't co-exist with a preview column.
    """
    cols = max(1, int(cols))
    rows = max(1, int(rows))

    if split == 'v':
        layout = _layout_vertical(
            cols, rows,
            list_ratio=list_ratio,
            show_preview=show_preview,
            show_children=show_children_pane,
            children_rows_needed=children_rows_needed,
            children_cols_needed=children_cols_needed,
        )
    elif split == 'm':
        layout = _layout_mixed(
            cols, rows,
            list_ratio=list_ratio,
            show_preview=show_preview,
            show_children=show_children_pane,
            children_rows_needed=children_rows_needed,
        )
    elif split == 'pc':
        layout = _layout_preview_children(
            cols, rows,
            list_ratio=list_ratio,
            show_preview=show_preview,
            show_children=show_children_pane,
            children_rows_needed=children_rows_needed,
        )
    else:
        layout = _layout_horizontal(
            cols, rows,
            list_ratio=list_ratio,
            show_preview=show_preview,
            show_children=show_children_pane,
            children_rows_needed=children_rows_needed,
        )

    layout['cols'] = cols
    layout['rows'] = rows
    return layout


# ---------------------------------------------------------------------------
# Low-level write helpers (call ``020-terminal`` primitives)
# ---------------------------------------------------------------------------


def _truncate_visible(s, max_cols):
    """Truncate ``s`` to ``max_cols`` *visible* columns, preserving SGR escapes.

    SGR sequences (``\\033[...m``) are emitted as-is and don't count
    toward the visible width — so cutting mid-style won't corrupt the
    terminal by leaving an unfinished escape on screen, and won't strip
    a colour run that started before the cut.

    Width is measured in characters (assumes monospace single-width;
    wide / combining chars are out of scope, matching the existing
    renderers). When the input fits, returns it unchanged. When it
    doesn't, the truncated result is suffixed with ``\\033[0m`` so SGR
    state can't leak into the next pane painted on the same row.

    Returns the truncated string. ``max_cols <= 0`` returns ``''``.
    """
    if max_cols <= 0:
        return ''
    out = []
    visible = 0
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == '\033' and i + 1 < n and s[i + 1] == '[':
            # Consume CSI ... m (SGR). Other CSI finals (e.g. cursor
            # movement) shouldn't appear in render-layer output, but if
            # they do we still pass them through unchanged.
            j = i + 2
            while j < n and s[j] != 'm':
                # Cursor / scroll escapes terminate at any 0x40-0x7E
                # final byte, not just 'm'. Stop at the first such byte.
                cj = s[j]
                if '@' <= cj <= '~':
                    break
                j += 1
            if j < n:
                out.append(s[i:j + 1])
                i = j + 1
                continue
            # Unterminated escape — drop the rest defensively.
            break
        if visible >= max_cols:
            # Past the visible budget. Stop emitting visible chars; we
            # still want to drop in a final reset so style doesn't bleed.
            break
        out.append(ch)
        visible += 1
        i += 1
    if visible >= max_cols and i < n:
        # Truncation actually occurred — append a reset so we don't
        # leak any SGR state set by the emitted prefix into adjacent
        # panes drawn on the same row.
        out.append('\033[0m')
    return ''.join(out)


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


def _children_list_cols_needed(children, show_ids='auto', *, padding=2):
    """Width hint for the vertical (Alt-1) one-per-row children column.

    Returns the longest formatted child width plus a small padding so
    short names don't sit flush against the separator. Returns 0 when
    ``children`` is empty so the layout helper can elide the column.
    The caller (``_layout_vertical`` via ``layout_panes``) caps this
    against 25% of the right area's width.
    """
    if not children:
        return 0
    longest = max(len(_fmt_child(c, show_ids=show_ids)) for c in children)
    return longest + padding


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


def render_list(browser, rect):
    """Render the list pane for ``browser`` at the given :class:`Rect`.

    Reads cursor / selected / expanded / scope state from
    ``browser._state`` via ``visible_items``. Maintains
    ``browser._list_scroll`` to keep the cursor on-screen.

    When ``browser._insert_mode`` is True, injects a synthetic
    ``-- {label} --`` marker row at ``browser._insert_pos`` and
    treats the marker as the cursor row (so scroll-tracking keeps it
    on-screen as the user moves it).

    Each row is clipped to ``rect.width``, content is positioned at
    ``rect.left``, and any unused trailing columns are blanked via
    :func:`clear_columns` so stale text from a previous render in a
    wider pane doesn't leak through.
    """
    if rect is None or rect.height <= 0 or rect.width <= 0:
        return

    top = rect.top
    height = rect.height
    width = rect.width
    left = rect.left
    right = rect.right

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
        row = top + row_idx
        # Clear only this pane's column range — preserves other panes
        # painted on the same row (e.g. the list pane on the left of
        # the preview pane in layouts v/m/pc).
        clear_columns(row, left, right)
        move(row, left)

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
            if len(line) > width:
                line = line[:width]
            set_style(fg=11, bg=4, bold=True)
            write(line)
            if len(line) < width:
                write(' ' * (width - len(line)))
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
            if len(line) > width:
                line = line[:width]
            _write_highlighted(
                line, reverse=True, pad_to=width,
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
                if len(line) > width:
                    line = line[:width]
                _write_highlighted(
                    line, pad_to=0,
                    search_query=browser._search_query,
                )
            else:
                _write_segments(segments, width)


def render_preview(browser, rect, *, info=False, has_header=True):
    """Render the preview pane (header + content) within ``rect``.

    When ``has_header=True`` the first row of ``rect`` is the info-bar
    header (label + optional ``[N]`` / search prompt / hints when
    ``info=True``); content occupies ``rect.height - 1`` rows below.
    When ``has_header=False`` the entire rect is content (used by
    non-'h' layouts where the info bar lives at the bottom of the
    screen, drawn by ``render_full`` independently).

    The header label adapts to ``browser._error_text`` /
    ``browser._help_mode`` (``Error`` / ``Help`` / ``Preview``).

    Source priority for the content:
      1. ``browser._error_text`` if set      → display the error text
      2. ``browser._help_mode`` if True      → display ``compose_help_text``
      3. ``browser._state._preview[id]``     → per-item preview
      4. fallthrough — empty preview         → blank rows

    Content is wrapped at ``rect.width`` columns. Rows shorter than
    the rect width are blanked out to ``rect.right`` so stale content
    from a previous render in a wider pane doesn't leak through.
    """
    if rect is None or rect.height <= 0 or rect.width <= 0:
        return

    top = rect.top
    height = rect.height
    width = rect.width
    left = rect.left
    right = rect.right

    if has_header:
        label = _preview_label(browser)
        # In 'h' layout the preview's first row IS the full-width info
        # bar (left=1, right=cols+1). For other layouts the info bar
        # is drawn separately on the bottom row by render_full and we
        # take the ``has_header=False`` branch.
        render_info_bar(top, right - 1, label, info=info, browser=browser)
        content_top = top + 1
        content_lines = height - 1
    else:
        content_top = top
        content_lines = height
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

    # Wrap content to the rect's width.
    raw = text.split('\n') if text else []
    wrapped = []
    for line in raw:
        line = line.replace('\t', '    ')
        if not line:
            wrapped.append('')
            continue
        while len(line) > width:
            wrapped.append(line[:width])
            line = line[width:]
        wrapped.append(line)

    scroll = browser._preview_scroll
    if scroll < 0:
        scroll = 0
    for i in range(content_lines):
        row = content_top + i
        clear_columns(row, left, right)
        src_idx = i + scroll
        if src_idx < len(wrapped):
            move(row, left)
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


def render_children_grid(browser, rect, *, info=False, has_header=True):
    """Render the children-grid pane (header + content) within ``rect``.

    When ``has_header=True`` the first row of ``rect`` is the info-bar
    header (label ``Children`` plus optional ``[N]`` / search prompt /
    hints when ``info=True``); content occupies ``rect.height - 1``
    rows below. When ``has_header=False`` the entire rect is content
    (used by non-'h' layouts where the info bar lives at the bottom of
    the screen).

    The children come from ``browser._state._children.get(cursor_item.id, [])``
    — direct children of the cursor item, fetched lazily by the
    children worker.

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

    Each row is clipped to ``rect.width`` and trailing columns are
    blanked to ``rect.right``.
    """
    if rect is None or rect.height <= 0 or rect.width <= 0:
        return

    top = rect.top
    height = rect.height
    width = rect.width
    left = rect.left
    right = rect.right

    if has_header:
        render_info_bar(top, right - 1, 'Children', info=info, browser=browser)
        content_top = top + 1
        content_lines = height - 1
    else:
        content_top = top
        content_lines = height
    if content_lines <= 0:
        return

    cursor = _cursor_item(browser)
    if cursor is None:
        for i in range(content_lines):
            clear_columns(content_top + i, left, right)
        return

    state = browser._state
    children = state._children.get(cursor.id)

    # Pending: cursor on a branch whose children aren't cached yet.
    if children is None and cursor.has_children:
        clear_columns(content_top, left, right)
        move(content_top, left)
        set_style(fg=_PENDING_FG)
        write('⧗ loading…'[:width])
        reset_style()
        for i in range(1, content_lines):
            clear_columns(content_top + i, left, right)
        return

    # Cached but empty (or a leaf) — nothing to draw, blank rows.
    if not children:
        for i in range(content_lines):
            clear_columns(content_top + i, left, right)
        return

    # Cached non-empty children — multi-column flowed layout.
    num_cols, col_width, slot_rows, entry_lines = _sub_layout(
        children, width, show_ids=browser.show_ids,
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
        clear_columns(content_top + row, left, right)
        move(content_top + row, left)
        if row >= total_rows:
            continue
        col_pos = 0
        for c in range(num_cols):
            cl = col_lines[c]
            cell = cl[row] if row < len(cl) else ''
            if c < num_cols - 1:
                cell_w = col_width
            else:
                cell_w = width - col_pos
            if cell_w <= 0:
                break
            src_idx = col_entry_at[c].get(row)
            if src_idx is not None:
                # First line of an entry — render with coloured segments.
                child = children[src_idx]
                segs = _child_segments(child, cell_w, show_ids=browser.show_ids)
                used = _write_segments(segs, min(cell_w, len(cell)))
                pad = cell_w - used
                if pad > 0:
                    write(' ' * pad)
            else:
                # Continuation line — plain text, no colour.
                chunk = cell[:cell_w].ljust(cell_w)[:cell_w]
                write(chunk)
            col_pos += cell_w


def render_children_list(browser, rect, *, info=False, has_header=True):
    """Render the children pane as a one-per-row vertical list (Alt-1).

    Used by the vertical (``split='v'``) layout per #176, where the
    children column sits between the list and the preview, occupying the
    full body height. Each direct child of the cursor item is written on
    its own row, truncated to the column width. The cursor item itself
    is read from ``_cursor_item``; the children come from
    ``browser._state._children.get(cursor.id, [])``.

    The header / no-cursor / loading / empty branches mirror
    :func:`render_children_grid` so the two renderers degrade identically.
    """
    if rect is None or rect.height <= 0 or rect.width <= 0:
        return

    top = rect.top
    height = rect.height
    width = rect.width
    left = rect.left
    right = rect.right

    if has_header:
        render_info_bar(top, right - 1, 'Children', info=info, browser=browser)
        content_top = top + 1
        content_lines = height - 1
    else:
        content_top = top
        content_lines = height
    if content_lines <= 0:
        return

    cursor = _cursor_item(browser)
    if cursor is None:
        for i in range(content_lines):
            clear_columns(content_top + i, left, right)
        return

    state = browser._state
    children = state._children.get(cursor.id)

    # Pending: cursor on a branch whose children aren't cached yet.
    if children is None and cursor.has_children:
        clear_columns(content_top, left, right)
        move(content_top, left)
        set_style(fg=_PENDING_FG)
        write('⧗ loading…'[:width])
        reset_style()
        for i in range(1, content_lines):
            clear_columns(content_top + i, left, right)
        return

    # Cached but empty (or a leaf) — nothing to draw, blank rows.
    if not children:
        for i in range(content_lines):
            clear_columns(content_top + i, left, right)
        return

    # Render each child on its own row using the coloured segments path.
    for row_idx in range(content_lines):
        clear_columns(content_top + row_idx, left, right)
        move(content_top + row_idx, left)
        if row_idx >= len(children):
            continue
        child = children[row_idx]
        segs = _child_segments(child, width, show_ids=browser.show_ids)
        _write_segments(segs, width)


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


def render_separator(rect, *, orientation=None, content=None):
    """Render a plain pane-separator view.

    ``rect`` is a :class:`Rect`. ``orientation`` is ``'h'`` (horizontal,
    fills with ``─``) or ``'v'`` (vertical, fills with ``│``); when
    omitted it's inferred from the rect shape (``height == 1`` → ``'h'``;
    ``width == 1`` → ``'v'``). ``content`` is an optional plain string
    for horizontal separators — currently unused by callers (the
    bottom info bar uses :func:`render_info_bar` for its rich layout)
    but exposed for future per-pane labels and parity with the ticket
    spec. When ``content`` is supplied for a horizontal separator it's
    centred between leading/trailing ``─`` runs and truncated to
    ``rect.width - 2`` columns. ``content`` is ignored for vertical
    separators (rotated rendering is out of scope for v3).

    The renderer writes through the existing terminal primitives
    (``move`` / ``write`` / ``set_style`` / ``reset_style``) using the
    same dim-grey palette index (``8``) as the info bar so adjacent
    separator runs blend visually. Junctions where horizontal and
    vertical separators meet are NOT special-cased in v3 (whichever
    separator is drawn last wins at the corner pixel — see
    :func:`render_full` for the draw order). Pretty connectors
    (``┼``/``┬``/``┴``/``├``/``┤``) are deferred per the ticket.
    """
    if rect is None:
        return
    if rect.width <= 0 or rect.height <= 0:
        return

    if orientation is None:
        if rect.height == 1:
            orientation = 'h'
        elif rect.width == 1:
            orientation = 'v'
        else:
            # Ambiguous shape — pick horizontal as the safer default
            # (clipped to the first row).
            orientation = 'h'

    set_style(fg=8)
    if orientation == 'v':
        # Draw the vertical bar down the rect's left column. Most
        # callers pass a 1-col-wide rect; if wider, only the leftmost
        # column is filled (the rest is the pane content's
        # responsibility — the renderer doesn't repaint pane interiors).
        for r in range(rect.top, rect.bottom):
            move(r, rect.left)
            write('│')
        reset_style()
        return

    # Horizontal — write on the first row of the rect (separators are
    # 1-thick by construction; multi-row rects are clipped to the top
    # row to keep the API forgiving).
    width = rect.width
    move(rect.top, rect.left)
    if content:
        # Truncate content to leave a 1-col padding on each side, then
        # centre it between two ``─`` runs.
        max_content = max(0, width - 2)
        ctext = content if len(content) <= max_content else content[:max_content]
        ctext_len = len(ctext)
        # Surrounding ``─`` runs split the leftover width.
        leftover = width - ctext_len
        left_run = leftover // 2
        right_run = leftover - left_run
        if left_run > 0:
            write('─' * left_run)
        if ctext:
            # Reset to default fg for the content so it reads clearly,
            # then return to the dim separator colour.
            reset_style()
            write(ctext)
            set_style(fg=8)
        if right_run > 0:
            write('─' * right_run)
    else:
        write('─' * width)
    reset_style()


def render_info_bar(row, cols, label, *, info=False, browser=None):
    """Render the info-bar / pane-separator row with rich decoration.

    Historically this function was called ``render_separator``; it was
    promoted to a dedicated ``render_info_bar`` in #147 so the simpler
    plain-divider :func:`render_separator` can carry a clean signature.

    When ``info=True`` the bar additionally shows:
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
    children_cols = 0
    if browser.show_children_pane and not browser._headless:
        cursor = _cursor_item(browser)
        if cursor is not None and cursor.has_children:
            cached = browser._state._children.get(cursor.id)
            if cached:
                children_rows = _sub_needed_rows(
                    cached, cols, show_ids=browser.show_ids,
                )
                # For the vertical (Alt-1) layout the children pane is a
                # full-height column with one item per row — its width
                # comes from the longest formatted child name. Other
                # layouts ignore this hint.
                children_cols = _children_list_cols_needed(
                    cached, show_ids=browser.show_ids,
                )
            elif cached is None:
                # Branch with not-yet-cached children — reserve one row
                # for the loading hint so the grid is visible while the
                # fetch is in flight.
                children_rows = 1
                children_cols = 0   # let _layout_vertical pick a default
            # cached == [] (empty list): leaf-like, no rows reserved.
    return layout_panes(
        cols, rows,
        split=getattr(browser, 'split', 'h'),
        show_preview=browser.show_preview,
        show_children_pane=browser.show_children_pane,
        children_rows_needed=children_rows,
        children_cols_needed=children_cols,
        list_ratio=browser.list_ratio,
    )


def _pane_info_flags(layout):
    """Return ``(sub_info, prev_info)``: which separator owns the info row.

    Mirrors plan-tui's helper of the same name. The info row always
    sits on a separator; this picks which pane that separator belongs
    to so the renderer knows where to draw the ``[N]`` / search prompt
    / hints.

    Reads the new Rect-based layout: in layout 'h' the info bar's top
    coincides with the children pane's first row (when the grid is
    visible) or the preview pane's first row (otherwise). For other
    layouts the info bar lives on the bottom row, distinct from any
    pane separator — those branches return ``(False, False)`` and the
    caller falls back to drawing a bottom info bar.
    """
    info_bar = layout.get('info_bar')
    children = layout.get('children')
    preview = layout.get('preview')
    if info_bar is None:
        return False, False
    ir = info_bar.top
    sub_info = children is not None and children.top == ir
    prev_info = (
        preview is not None and preview.top == ir and not sub_info
    )
    # When show_preview=False, preview is None but the info bar still
    # sits at the bottom. Treat that case as prev_info to keep
    # render_separator drawing the bottom info bar.
    if not sub_info and not prev_info and preview is None and ir > 0:
        prev_info = True
    return sub_info, prev_info


def _info_bar_is_separate(layout):
    """True if the info bar is a standalone bottom row (non-h layouts).

    In layout 'h' the info bar coincides with the children or preview
    pane's top row — it's drawn by ``render_children_grid`` /
    ``render_preview``. In v/m/pc layouts the info bar is its own row
    at the bottom of the screen, distinct from any pane separator.
    """
    info_bar = layout.get('info_bar')
    if info_bar is None:
        return False
    children = layout.get('children')
    preview = layout.get('preview')
    ir = info_bar.top
    if children is not None and children.top == ir:
        return False
    if preview is not None and preview.top == ir:
        return False
    return True


def render_full(browser):
    """Repaint the whole screen.

    Clears the screen, lays out the panes, paints each, then flushes.
    Resets ``browser._needs_redraw`` so subsequent partial-render calls
    start from a clean slate.
    """
    write('\033[2J')
    layout = _layout_for(browser)
    sub_info, prev_info = _pane_info_flags(layout)
    info_separate = _info_bar_is_separate(layout)
    list_rect = layout['list']
    render_list(browser, list_rect)
    children_rect = layout['children']
    if children_rect is not None:
        # Alt-1 (vertical) renders children as a one-per-row list; the
        # other splits use the multi-column flowed grid.
        if getattr(browser, 'split', 'h') == 'v':
            render_children_list(
                browser, children_rect,
                info=sub_info,
                has_header=not info_separate,
            )
        else:
            render_children_grid(
                browser, children_rect,
                info=sub_info,
                has_header=not info_separate,
            )
    preview_rect = layout['preview']
    if browser.show_preview and preview_rect is not None:
        render_preview(
            browser, preview_rect,
            info=prev_info,
            has_header=not info_separate,
        )
    if (info_separate or not browser.show_preview) and layout['info_bar'] is not None:
        # Non-'h' layouts (or show_preview=False) — info bar is a
        # standalone row drawn here. Rich label + decorations.
        render_info_bar(
            layout['info_bar'].top, layout['cols'], _preview_label(browser),
            info=True, browser=browser,
        )
    # Draw plain separators between panes (vertical sep_main / sep_inner
    # in v/m/pc layouts). For layout 'h' both are None — separators are
    # folded into the children/preview pane's first row, painted by the
    # rich ``render_info_bar`` inside ``render_preview`` /
    # ``render_children_grid``. v3 doesn't bother with junction
    # connectors; whichever separator is drawn last wins at the corner.
    sep_main = layout.get('sep_main')
    sep_inner = layout.get('sep_inner')
    if sep_main is not None:
        render_separator(sep_main)
    if sep_inner is not None:
        render_separator(sep_inner)
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
    info_separate = _info_bar_is_separate(layout)
    list_rect = layout['list']
    children_rect = layout['children']
    preview_rect = layout['preview']
    info_bar = layout['info_bar']
    if 'list' in needs:
        render_list(browser, list_rect)
    if 'children' in needs:
        if children_rect is not None:
            if getattr(browser, 'split', 'h') == 'v':
                render_children_list(
                    browser, children_rect,
                    info=sub_info,
                    has_header=not info_separate,
                )
            else:
                render_children_grid(
                    browser, children_rect,
                    info=sub_info,
                    has_header=not info_separate,
                )
    if 'preview' in needs:
        if browser.show_preview and preview_rect is not None:
            render_preview(
                browser, preview_rect,
                info=prev_info,
                has_header=not info_separate,
            )
    if 'info' in needs:
        ir = info_bar.top if info_bar is not None else 0
        if ir > 0:
            if sub_info:
                render_info_bar(
                    ir, layout['cols'], 'Children',
                    info=True, browser=browser,
                )
            else:
                render_info_bar(
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
