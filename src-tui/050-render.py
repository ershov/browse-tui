"""browse-tui: render layer (list + children-grid + preview, status bar).

Renderer layout:

  * Up to three panes — a scrolling list, a multi-column children grid
    (between the list and the preview), and a preview pane — each
    separated by an info bar that doubles as a separator.
    ``show_preview=False`` collapses to a single full-screen list with
    the info bar at the bottom row. ``show_children_pane=False`` (or
    a cursor on a leaf, or no cached children) hides the children grid
    entirely; the preview pane then takes that space.
  * Item rows are built as a list of ``(text, fg, bold)`` segments via
    ``browser._row_segments(item, ctx)`` (resolved once in
    ``Browser.__init__``). Recipes override layout with the row-format
    hooks (design sec A): ``format_row`` (whole row), ``format_row_chrome``
    (selection marker + indent + expander), or ``format_row_content`` (the
    content region) — the framework defaults (``default_row_chrome`` /
    ``default_row_content`` / ``default_row``, in 040-state) fill any unset
    hook. ``render_list`` builds a :class:`RowContext` per row and makes
    one resolved call — no hook is ``None`` at runtime.
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

# In the concatenated production build these are already imported by an
# earlier numbered file; the re-imports are harmless no-ops there but let
# the module-level regex in ``_sanitize_ansi`` and the ``_PreviewSnapshot``
# namedtuple resolve when this file is exec'd standalone (the isolated
# unit-test load).
import re
from collections import namedtuple


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


# --- Public styles API (see design sec D) ----------------------------------
# A segment's colour *is* a raw (fg, bold) pair; ``_TAG_STYLE`` above is the
# internal table mapping the named-style vocabulary onto those pairs. Recipes
# assembling their own column segments can't reach ``_TAG_STYLE``, so expose
# both layers — the named lookup ``style()`` / ``STYLE_NAMES``, and the raw
# palette constants the default chrome uses (public faces of the ``_*_COLOR``
# / ``_PENDING_FG`` internals) — so columns match ``tag_style`` / chips
# without magic numbers.

# The valid named-style keys (mirrors the vocabulary documented on
# ``Item.tag_style``). Includes ``''`` — the plain, unstyled default.
STYLE_NAMES = frozenset(_TAG_STYLE)

# Raw palette constants — the semantic chrome colours a column is most
# likely to want to match (see design sec D / "raw styles").
MARKER_FG = _MARKER_COLOR   # 4 — blue ▼/▶ expander
ID_FG = _ID_COLOR           # 3 — yellow #id segment
DIM_FG = _PENDING_FG        # 242 — dim; the 'dim' named style's fg


def style(name):
    """Resolve a named style to its raw ``(fg, bold)`` pair.

    ``name`` is one of the keys in :data:`STYLE_NAMES`
    (``'green'``/``'red'``/``'yellow'``/``'gray'``/``'cyan'``/``'blue'``/
    ``'magenta'``/``'dim'``/``''``). ``fg`` is a 256-colour palette index
    (or ``None`` for the terminal default) and ``bold`` is a bool — the
    same pair ``tag_style`` / chips resolve to. An unknown name (or ``''``)
    returns ``(None, False)`` (plain), matching tag rendering's fallback.
    """
    return _TAG_STYLE.get(name, _TAG_STYLE[''])


# Insert-mode marker — sentinel id used by the synthetic ``VisibleEntry``
# injected into the rendered list when ``browser._insert_mode`` is True.
# We use ``object()`` so the sentinel never equals any user-supplied id
# (mirrors ``_PENDING_PLACEHOLDER_ID`` in 040-state.py). plan-tui used
# ``id == -1`` for the same purpose.
_INSERT_MARKER_ID = object()


# ---------------------------------------------------------------------------
# Pure helpers — unit-testable, no terminal I/O
# ---------------------------------------------------------------------------


# Translation tables: every char with code < 32 becomes '?', except tab
# (\x09) and newline (\x0a) which are preserved (tab is later expanded
# to spaces by the renderer; LF is the line separator). Also map DEL
# (0x7f) to '?': the spec doesn't require it but it's safer because
# legacy terminals occasionally treat it as a destructive control char.
#
# Two tables, gated by ANSI mode:
#   * ``_PREVIEW_SANITIZE_TABLE_ANSI`` keeps ESC (\x1b) — the wrap-aware
#     SGR walker (``_wrap_preview_line``, ticket #242) tokenises CSI
#     sequences and re-emits SGR / drops other CSI.
#   * ``_PREVIEW_SANITIZE_TABLE_RAW`` maps ESC to '?' — matches the
#     pre-#243 behaviour where untrusted content can never inject any
#     escape sequence into the screen, so ``\x1b[31mRED\x1b[0m`` shows
#     as ``?[31mRED?[0m``. Selected when ``preview_ansi`` is False.
_PREVIEW_SANITIZE_TABLE_ANSI = {
    i: '?' for i in range(32) if i not in (0x09, 0x0a, 0x1b)
}
_PREVIEW_SANITIZE_TABLE_ANSI[0x7f] = '?'

_PREVIEW_SANITIZE_TABLE_RAW = {
    i: '?' for i in range(32) if i not in (0x09, 0x0a)
}
_PREVIEW_SANITIZE_TABLE_RAW[0x7f] = '?'


def _sanitize_preview(text, *, ansi_on=True):
    """Replace control chars (codes < 32 except \\t/\\n) with '?'.

    Also replaces DEL (0x7f). Used to defang binary noise in untrusted
    file content before it hits the terminal. Tab is preserved (the
    renderer expands it to spaces); newline is preserved (line
    separator).

    ``ansi_on`` controls ESC (\\x1b):
      * True  — ESC passes through; the wrap-aware SGR walker
        (``_wrap_preview_line``) tokenises and re-emits SGR or strips
        non-SGR CSI.
      * False — ESC is replaced with '?', mirroring the pre-#243
        contract so untrusted content can never inject a sequence
        into the user's screen.
    """
    if not text:
        return text
    table = (_PREVIEW_SANITIZE_TABLE_ANSI if ansi_on
             else _PREVIEW_SANITIZE_TABLE_RAW)
    return text.translate(table)


# Three left-to-right alternatives, matched per ESC by ``_sanitize_ansi``:
#   1. ``\e[`` <non-final bytes> <final> — a *complete* CSI. ``[^@-~]*``
#      consumes params/intermediates (and anything else not in the 0x40-0x7e
#      final-byte range), then one final byte ``[@-~]`` captured in group 1.
#      Bounded char-classes mean it never swallows text between two escapes.
#   2. ``\e[`` <non-final bytes> end-of-string — a *dangling* CSI with no
#      final byte; ``\Z`` anchors it to the true end (never before a newline).
#   3. ``\e`` alone — a bare ESC (lone, or introducing a non-CSI escape).
# The replacement keeps the match iff alt 1 matched with final byte ``m``
# (an SGR sequence); every other match is dropped.
_SANITIZE_ANSI_RE = re.compile(r'\x1b\[[^@-~]*([@-~])|\x1b\[[^@-~]*\Z|\x1b')


def _sanitize_ansi_repl(m):
    # Keep only complete SGR sequences (alt 1 with final byte 'm'); drop
    # every other CSI, dangling ``\e[``, and bare ESC.
    return m.group(0) if m.group(1) == 'm' else ''


def _sanitize_ansi(s):
    """Strip every escape from ``s`` *except* SGR colour sequences (sec 4.2 #1).

    The shared escape-sanitiser for the two paths that pass attacker- or
    recipe-supplied ANSI to the terminal: a raw ``str`` rendered as **row
    content** (via :func:`_normalize_content`) and the **preview pane**
    text (after :func:`_sanitize_preview`'s control-char pass). Both call
    *this* function so they behave identically.

    Kept verbatim: a complete SGR sequence ``\\e[ <params> m`` (final byte
    ``m``) — that's the colour channel. Dropped:

    * any other complete CSI (``\\e[ <params> <final>`` whose final byte is
      not ``m`` — cursor moves ``H``/``A``, erases ``J``/``K``, scroll
      regions, …);
    * a **dangling** ``\\e[`` with no final byte before the end of string;
    * a **bare** ``\\e`` not introducing a CSI (a lone ESC, or ESC starting
      a non-CSI escape such as ``\\eM`` — only the ESC byte is removed).

    A single greedy :func:`re.sub` pass over :data:`_SANITIZE_ANSI_RE` (whose
    three alternatives match exactly one ESC each, with bounded ``[^@-~]*``
    runs so inter-escape text is never swallowed and the ``m`` boundary is
    exact). Strings with no ESC return unchanged (the common case — plain
    content never enters the substitution).
    """
    if '\033' not in s:
        return s
    return _SANITIZE_ANSI_RE.sub(_sanitize_ansi_repl, s)


# Public, recipe-facing name for sanitising external ANSI before embedding it
# in a segment's text (segment text bypasses the framework's on-receipt
# sanitise in ``_normalize_content``, which only sanitises *string* content).
sanitize_ansi = _sanitize_ansi


def _id_visible(item, show_ids):
    """Decide whether the id segment should be emitted for ``item``.

    ``show_ids`` is one of ``'always'``, ``'auto'``, ``'never'``. In
    ``'auto'`` mode the id is shown only when it is a *scalar* (``str`` /
    ``int``) that differs from the title — the common shape for line-based
    CLI input where the id is a user-facing identifier and showing it
    alongside an equal title would be pure duplication. A structured id
    (tuple, dataclass, …) is internal routing state, never a display
    identifier, so ``'auto'`` always suppresses it — e.g. the ``('launch',
    …)`` / ``('md-refs', …)`` rows ``md_doc`` builds. ``'always'`` /
    ``'never'`` force the id on / off regardless of its type.
    """
    if show_ids == 'always':
        return True
    if show_ids == 'never':
        return False
    return isinstance(item.id, (str, int)) and str(item.id) != item.title


# Sentinel for ``RowContext.max_col_width(field, parent_id=...)``: lets an
# omitted ``parent_id`` (default to this row's parent) be told apart from an
# explicit ``parent_id=None`` (a legitimate parent key for a ``root_id=None``
# Browser).
_PARENT_DEFAULT = object()


class RowContext:
    """Per-row geometry + tree state handed to the row-format hooks.

    Distinct from the action :class:`Context` (060-context.py): that one is
    built once per dispatched action and carries cursor/targets/confirm;
    this one is built per painted row (``height`` per frame) and carries
    per-row state. Conflating them would mutate per-row state onto a
    shared, longer-lived object.

    Per-row state (read-only):
      * ``depth``            — tree depth of the row (absolute).
      * ``selected``         — ``bool``: row is in ``state.selected``.
      * ``expanded``         — ``bool``: row is in ``state.expanded``.
      * ``is_current_scope`` — ``bool``: this item *is* the current scope.
      * ``kind``             — ``VisibleEntry.kind`` (``'normal'`` for hook
                               rows).
      * ``parent_id``        — ``state._parent_of_id.get(item.id)``.

    Dimensions (refreshed every paint, modelled on ``preview_width``):
      * ``list_width``    — content width of the list pane in cells
                            (``rect.width``).
      * ``content_width`` — cells left for the content hook after the
                            chrome on *this* row. Starts equal to
                            ``list_width``; ``_compose_row`` /
                            :func:`default_row` lower it via
                            ``_set_content_width`` once the chrome is
                            measured. Under a whole-row ``format_row``
                            override it stays equal to ``list_width`` (the
                            chrome split is unknown).

    Both dimensions are ``0`` before the first paint / in headless tests
    (the ``list_width`` passed in is ``0``), matching the ``preview_width``
    contract; recipes wanting a fallback pick one explicitly.

    Method:
      * ``max_col_width(field, parent_id=None)`` — the max display-cell width
        of a pre-formatted column string across a sibling group (design
        sec C). Cached per parent on ``State``; lazily filled.
    """

    __slots__ = (
        'depth', 'selected', 'expanded', 'is_current_scope', 'kind',
        'parent_id', 'list_width', 'content_width', '_browser',
    )

    def __init__(self, browser, item, *, depth, selected, expanded,
                 is_current_scope, kind, list_width):
        self._browser = browser
        self.depth = depth
        self.selected = selected
        self.expanded = expanded
        self.is_current_scope = is_current_scope
        self.kind = kind
        self.parent_id = browser._state._parent_of_id.get(item.id)
        self.list_width = list_width
        # content_width starts at the full pane width; the chrome→content
        # composer lowers it once the chrome is measured. A whole-row
        # override never calls _set_content_width, so it stays = list_width.
        self.content_width = list_width

    def _set_content_width(self, chrome_cells):
        """Set ``content_width`` to ``list_width − chrome_cells``.

        Called by the default composer (``Browser._compose_row`` /
        :func:`default_row`) between building the chrome and running the
        content hook, so the content hook sees the cells left after the
        chrome on this row. Clamped at 0 so a chrome wider than the pane
        never yields a negative width.
        """
        remaining = self.list_width - chrome_cells
        self.content_width = remaining if remaining > 0 else 0

    def max_col_width(self, field, parent_id=_PARENT_DEFAULT):
        """Max display-cell width of ``str(getattr(child, field, ''))`` over a
        sibling group (design sec C).

        ``parent_id`` defaults to *this row's* parent (``ctx.parent_id``), so a
        row aligns a column to its siblings; pass an explicit id (including
        ``None`` — a legitimate parent key for a ``root_id=None`` Browser) to
        measure a different group. The recipe pre-stores the *display* string
        it intends to render on each Item and passes that field name, so what
        is measured is what is rendered.

        Result is memoised per ``(parent_id, field)`` on
        ``State._col_width_cache`` and rebuilt lazily after that parent's child
        list is dropped, replaced, or mutated (see ``_index_drop_children`` /
        ``_col_width_drop``). A cache hit re-scans nothing.

        A child missing ``field`` contributes ``0`` (``getattr`` default
        ``''``); an empty or absent sibling list yields ``0``.
        """
        if parent_id is _PARENT_DEFAULT:
            parent_id = self.parent_id
        state = self._browser._state
        cache = state._col_width_cache.setdefault(parent_id, {})
        cached = cache.get(field)
        if cached is not None:
            return cached
        width = max(
            (cell_width(str(getattr(child, field, '')))
             for child in state._children.get(parent_id, ())),
            default=0,
        )
        cache[field] = width
        return width

    @property
    def browser(self):
        """The underlying :class:`Browser` (advanced; unstable surface).

        Mirrors :attr:`Context.browser` — for capabilities not yet
        promoted onto ``RowContext``. The default content hook reads
        ``ctx.browser.show_ids`` through it.
        """
        return self._browser


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


def _reconcile_pane_caches(browser, layout):
    """Reconcile per-pane row caches with the layout for this frame.

    Single dispatch site that owns the layout→cache mapping (see
    ticket #228). Called once from ``render_full`` / ``render_partial``
    immediately after ``_layout_for``.

    For each known cache key, picks the corresponding rect from the
    layout (``None`` for hidden panes) and routes it through
    :meth:`PaneCache.update_rect`, which handles all three transitions
    (hidden, steady, geometry-changed) in one place.

    The 'info_bar' cache uses ``layout.get('info_bar')`` directly even
    though the layout entry is non-None in BOTH 'h' (where the bar is
    folded into the preview/children pane header at row R_h) AND
    v/m/pc (standalone bottom row at row R_v). The two rects differ
    (different rows), so ``update_rect`` invalidates correctly across
    layout switches — the centralized rect transition catches the
    case that intermediate layouts didn't paint through this cache.
    This closes ticket #221 (info_bar v→h→v stale).

    Critical invariant: ``update_rect`` must be called EXACTLY ONCE
    per cache per frame. The renderers no longer touch
    ``cache.prev_rect`` themselves — they look up the already-reconciled
    cache from ``browser._pane_cache``.
    """
    cache_rects = {
        'list':      layout.get('list'),
        'preview':   layout.get('preview'),
        'children':  layout.get('children'),
        'sep_main':  layout.get('sep_main'),
        'sep_inner': layout.get('sep_inner'),
        'info_bar':  layout.get('info_bar'),
    }

    for name, rect in cache_rects.items():
        cache = browser._pane_cache.setdefault(name, PaneCache())
        # Visible→hidden transition (#718): the pane held a real rect on
        # the previous frame and is absent this frame. The renderers only
        # paint a pane when its rect is non-None, so nothing would clear
        # the rows it just vacated — they stay stale unless a neighbour
        # happens to grow over them AND gets repainted this frame (true
        # for cursor moves, which mark all panes dirty, but NOT for paths
        # like ``update_data`` that flag only list/children). Blank the
        # vacated cells here, the single per-frame site that already owns
        # the layout→cache mapping and runs inside the sync region for
        # both render_full and render_partial. ``isinstance(..., Rect)``
        # excludes the disappeared-pane sentinel (a real prior geometry
        # is always a Rect) so an already-hidden pane is a no-op.
        prev = cache.rect
        if rect is None and isinstance(prev, Rect):
            for row in range(prev.top, prev.bottom):
                clear_columns(row, prev.left, prev.right)
        cache.update_rect(rect)


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

# #274: demand-signal threshold. When the bottom of the visible preview
# window is within this many wrapped rows of the buffered tail AND a
# preview generator is paused for the cursored id, the renderer wakes
# the worker to keep pulling. Sized to roughly half a typical pane so
# the resume kicks in well before the user scrolls into blank space.
_PREVIEW_DEMAND_THRESHOLD = 12

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

    Children column width (per #180): CONTENT-INDEPENDENT. Whenever the
    children column is shown, its width is fixed at
    ``max(8, right_area_width // 4)`` (capped at ``right_area_width - 2``
    so sep_inner + at least 1 col of preview content always fit). The
    ``children_cols_needed`` parameter is ignored — kept only for API
    compatibility with callers that still pass it. This prevents the
    column from shifting as the cursor moves between branches with
    different child name lengths, which used to overdraw the inner
    separator (#180). Long child names are truncated by the renderer.

    Falls back to layout 'h' when preview is hidden (a vertical split
    with no preview would just be the list).
    """
    # children_cols_needed kept in signature for compat; explicitly
    # ignored per #180.
    del children_cols_needed
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
        # Per #180: width is content-INDEPENDENT and fixed at 25% of the
        # right area (with a small minimum so the column is usable even
        # at narrow terminal widths). This avoids the inner separator
        # shifting between cursor moves on items with different child
        # name lengths.
        desired = max(8, right_area_width // 4)
        # Reserve 1 col for sep_inner + 1 col of preview content.
        max_w = right_area_width - 2
        if max_w < 1:
            max_w = 0
        ch_w = min(desired, max_w)
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


def _truncate_by_cells(s, max_cols):
    """Truncate plain text ``s`` to at most ``max_cols`` *visible cells*.

    Wide-char aware: East Asian Wide/Fullwidth chars count as 2 cells.
    When a wide char would straddle the budget (1 cell left, 2-cell char
    incoming), the char is dropped and a single space fills the remaining
    cell so the caller's pad / pane-boundary math stays exact.

    Plain text only — does not skip ANSI escapes. The list renderer
    collapses styled segments to a plain string before truncating, so
    this restriction matches the call sites. (For mixed text/SGR
    truncation see ``_truncate_visible``.)

    Returns ``(truncated, cells)`` — ``cells`` is the exact cell count
    of the returned string and is always ``<= max_cols``.
    """
    if max_cols <= 0:
        return ('', 0)
    out = []
    cells = 0
    for ch in s:
        w = _char_width(ch)
        if cells + w > max_cols:
            if cells < max_cols:
                out.append(' ')
                cells += 1
            break
        out.append(ch)
        cells += w
    return (''.join(out), cells)


def _suffix_by_cells(s, max_cols):
    """Longest *suffix* of plain text ``s`` fitting in ``max_cols`` cells.

    The tail-side analogue of :func:`_truncate_by_cells` (which keeps a
    prefix): walks ``s`` from the right, accumulating characters while the
    running cell count stays within budget. Wide-char aware via
    :func:`_char_width`; a wide char that would straddle the budget is
    dropped rather than overshooting (so the result may be one cell short
    of ``max_cols``, never over). ``max_cols <= 0`` returns ``''``.
    """
    if max_cols <= 0:
        return ''
    cells = 0
    cut = len(s)
    for i in range(len(s) - 1, -1, -1):
        w = _char_width(s[i])
        if cells + w > max_cols:
            break
        cells += w
        cut = i
    return s[cut:]


def cell_width(s):
    """Display-cell width of plain text ``s`` (the public face of
    :func:`_visible_len`).

    Wide-char aware: East Asian Wide/Fullwidth characters count as 2 cells,
    others as 1. ANSI CSI escapes are not counted (recipes carry colour via
    the segment ``fg``/``bold``, never embedded SGR — but the
    escape-stripping keeps the contract identical to ``_visible_len``).
    """
    return _visible_len(s)


def _char_width_total(s):
    """Total display-cell width of ``s`` counting *every* char (no escape
    stripping) — used to validate single-cell ``fill`` / ``ellipsis`` args.

    Deliberately does **not** delegate to ``_visible_len`` / :func:`cell_width`,
    which strip ANSI CSI sequences before counting. A ``fill`` / ``ellipsis``
    is tiled into the result *verbatim*, so what matters is the cells the
    raw string actually occupies on screen, not its ANSI-stripped visible
    width. An escape-bearing value like ``'\\033[0m '`` has visible width 1
    but occupies 5 raw cells; counting it as 1 would let the single-cell
    guard wrongly accept it, and tiling those 5 chars per pad cell would
    corrupt the column's width math. Counting raw chars rejects it.
    """
    return sum(_char_width(ch) for ch in s)


def _check_fill(fill):
    """Validate a pad ``fill`` is exactly one display cell; return it.

    The cell-pad helpers tile ``fill`` one cell at a time, so a wide
    (2-cell) or empty fill would break the exact-width contract. Reject it
    loudly rather than silently mis-aligning a column.
    """
    if _char_width_total(fill) != 1:
        raise ValueError(
            'fill must be exactly one display cell, got {!r}'.format(fill))
    return fill


def cell_ljust(s, width, fill=' '):
    """Left-justify ``s`` to ``width`` display cells, padding on the right.

    Returns ``s`` unchanged if it is already ``width`` cells or wider (pad
    never shrinks). ``fill`` must be exactly one display cell.
    """
    _check_fill(fill)
    pad = width - cell_width(s)
    return s + fill * pad if pad > 0 else s


def cell_rjust(s, width, fill=' '):
    """Right-justify ``s`` to ``width`` display cells, padding on the left.

    Returns ``s`` unchanged if already ``width`` cells or wider. ``fill``
    must be exactly one display cell.
    """
    _check_fill(fill)
    pad = width - cell_width(s)
    return fill * pad + s if pad > 0 else s


def cell_center(s, width, fill=' '):
    """Centre ``s`` within ``width`` display cells, padding both sides.

    Extra padding (when the gap is odd) goes on the right, matching
    :meth:`str.center`. Returns ``s`` unchanged if already ``width`` cells
    or wider. ``fill`` must be exactly one display cell.
    """
    _check_fill(fill)
    pad = width - cell_width(s)
    if pad <= 0:
        return s
    left = pad // 2
    return fill * left + s + fill * (pad - left)


def cell_trim(s, width, *, where='end', ellipsis='…', word_boundary=False):
    """Trim plain text ``s`` to ``width`` display cells with an ellipsis.

    No-op when ``s`` already fits (``cell_width(s) <= width``). Otherwise
    the ellipsis is placed per ``where``:

      * ``'end'``   — ``'abc…'`` (keep a prefix of ``s``).
      * ``'start'`` — ``'…xyz'`` (keep a suffix of ``s``).
      * ``'middle'``— ``'ab…yz'`` (keep a prefix and a suffix; the prefix
        gets the extra cell when the content budget is odd).

    The ellipsis width is accounted within ``width`` (it is part of the
    result, not appended past it); ``ellipsis`` defaults to ``'…'`` (1
    cell) — pass ``'...'`` for three dots. Wide chars near the cut are
    dropped rather than overshooting ``width``, so the result is always
    ``<= width`` cells (it can be one cell short when a wide char straddles
    the cut). ``word_boundary`` (``'middle'`` only) prefers to end the
    prefix at a space so a word isn't split; it has no effect when no
    suitable space sits within the prefix budget.

    Returns the trimmed string. If even the ellipsis doesn't fit
    (``width`` < its width) the ellipsis itself is cell-trimmed to
    ``width`` and no content is kept.
    """
    if cell_width(s) <= width:
        return s
    ell_w = _char_width_total(ellipsis)
    budget = width - ell_w
    if budget <= 0:
        # Not even room for the ellipsis — trim the ellipsis to fit, drop
        # all content. Never overshoot the requested width.
        return _truncate_by_cells(ellipsis, width)[0]

    if where == 'start':
        return ellipsis + _suffix_by_cells(s, budget)

    if where == 'middle':
        head_budget = budget - budget // 2   # ceil — prefix gets the extra
        tail_budget = budget // 2
        head = _truncate_by_cells(s, head_budget)[0]
        if word_boundary:
            cut = head.rfind(' ')
            # Snap to the last space within the prefix, but keep it: an
            # empty prefix (space at index 0) gains nothing, so only honour
            # a space that leaves some non-space content before it.
            if cut > 0:
                head = head[:cut + 1]
        tail = _suffix_by_cells(s, tail_budget)
        return head + ellipsis + tail

    # Default / 'end': keep a prefix, ellipsis trails.
    return _truncate_by_cells(s, budget)[0] + ellipsis


def cell_fit(s, width, *, justify='left', trim='end', ellipsis='…',
             fill=' ', word_boundary=False):
    """Trim-or-pad ``s`` to **exactly** ``width`` display cells.

    The one-call column formatter: when ``s`` is too wide it is trimmed via
    :func:`cell_trim` (honouring ``trim`` = ``'end'``/``'start'``/
    ``'middle'``, ``ellipsis`` and ``word_boundary``); otherwise it is
    padded per ``justify`` (``'left'``/``'right'``/``'center'``) with
    ``fill``. The result is always exactly ``width`` cells — even when a
    wide char straddles the trim budget (the trimmed string can come back a
    cell short, so it is re-padded to ``width``). ``fill`` must be one cell.
    """
    _check_fill(fill)
    pad = {'left': cell_ljust, 'right': cell_rjust,
           'center': cell_center}.get(justify, cell_ljust)
    if cell_width(s) > width:
        trimmed = cell_trim(s, width, where=trim, ellipsis=ellipsis,
                            word_boundary=word_boundary)
        # A wide-char straddle can leave ``trimmed`` one cell short of
        # ``width``; pad it back so the column lands on an exact boundary.
        return pad(trimmed, width, fill)
    return pad(s, width, fill)


def _collapse_visible(segments):
    """Collapse ``segments`` to a single plain VISIBLE-TEXT line.

    The cursor-row and search-match overlays render each row as plain text
    under a reverse-video / highlight style, so all per-segment styling is
    dropped: segment ``fg`` / ``bold`` are discarded (only ``s[0]``, the
    text, is kept) **and** any SGR embedded in a segment's text is stripped
    with the same ``_ANSI_CSI_RE`` strip ``cell_width`` / :func:`_visible_len`
    use to measure. This keeps the overlay clean even for a row whose
    content was a raw ANSI string (design sec 4.1) — the embedded colour
    can't fight the reverse / highlight style. Width math is unaffected:
    the stripped text has exactly the visible width the styled text did.
    """
    return ''.join(_ANSI_CSI_RE.sub('', s[0]) for s in segments)


def _write_segments(segments, max_width, *, pad_to=0, row_bg=None,
                    row_fg=None):
    """Emit ``segments`` to the terminal, truncating at ``max_width`` cells.

    Each segment is a ``(text, fg, bold)`` triple. ``fg=None`` and
    ``bold=False`` means use the terminal's current style (no SGR
    sequences emitted, no reset). When fg or bold is set, we wrap the
    chunk in ``set_style`` / ``reset_style`` so adjacent segments don't
    bleed colours.

    ``row_bg`` (256-color int, optional) applies a background colour to
    every chunk *and* extends it across the trailing pad — turning the
    whole row into a coloured stripe. Recipes set ``item.row_bg`` on
    Items they want to call out (e.g. user/assistant turns in
    browse-claude); the list renderer threads it through here.

    ``row_fg`` (256-color int, optional) is the foreground analogue:
    segments without their own ``fg`` pick up ``row_fg``; segments
    that already specify a colour keep theirs. Useful for "dim the
    whole row" / "red row for failed status" effects without
    rewriting per-segment styles.

    Width is measured in *visible cells* (wide-char aware) so rows
    containing CJK / emoji / other East Asian wide chars truncate and
    pad on cell boundaries instead of overflowing the pane into the
    neighbour on the right.

    A segment whose ``text`` carries embedded SGR (a raw ANSI string
    normalised to one segment — design sec 4.1) is truncated **ANSI-aware**
    via :func:`_truncate_visible` (escapes passed through, only visible
    cells counted) instead of the plain :func:`_truncate_by_cells`, so the
    escape bytes neither corrupt the width math nor get cut mid-sequence.
    That embedded SGR was already **sanitised on receipt** by
    :func:`_normalize_content` (design sec 4.2 #1 — only colour codes
    survive), so the only escapes reaching here are SGR. This function then
    finishes the §4.2 handling for such content: a **conditional trailing
    reset** (#3 — ``\\e[m`` after the content iff any SGR was emitted, so a
    colour run can't bleed into the pad / next pane) and the **background
    restore** (#4 — re-emit the row bg (from the ``row_bg`` attribute) after
    that reset whenever a row bg is set, since the reset clears the bg the
    stripe needs). Plain segments (the overwhelming common case — colour
    rides ``fg``, never the text) emit nothing extra and keep the existing
    plain truncation byte-for-byte.

    Returns the number of cells written.
    """
    pos = 0
    # Track whether the *emitted* content carried ANY SGR (design sec 4.2
    # #3), so the trailing reset fires on what actually reached the screen
    # — not on text that fell outside the width budget.
    content_ansi = False
    for text, fg, bold in segments:
        if pos >= max_width:
            break
        remaining = max_width - pos
        if '\033[' in text:
            # ANSI-bearing text: truncate by visible cells, escapes intact.
            chunk = _truncate_visible(text, remaining)
            chunk_cells = _visible_len(chunk)
        else:
            chunk, chunk_cells = _truncate_by_cells(text, remaining)
        if not chunk:
            continue
        if '\033[' in chunk:
            content_ansi = True
        # Effective fg: segment's own ``fg`` wins; otherwise inherit
        # ``row_fg`` (when set).
        eff_fg = fg if fg is not None else row_fg
        if eff_fg is not None or bold or row_bg is not None:
            set_style(fg=eff_fg, bg=row_bg, bold=bold)
            write(chunk)
            reset_style()
        else:
            write(chunk)
        pos += chunk_cells
    # Conditional trailing reset (design sec 4.2 #3): if the emitted content
    # carried ANY SGR, close it with ``\e[m`` so an unterminated colour run
    # can't bleed into the trailing pad / the next pane on this row. Plain
    # content (the common case) emits nothing extra — byte-for-byte
    # unchanged. (A styled chunk already had its own ``reset_style``; the
    # extra reset here is harmless and keeps the rule uniform across the
    # bare-write passthrough path, which has no per-chunk reset.)
    if content_ansi:
        write('\033[m')
        # Background restore (design sec 4.2 #4): re-apply the row bg after
        # the reset whenever ``row_bg`` is set (driven by the row's bg
        # attribute, NOT a content scan). The ``\e[m`` reset clears the bg
        # regardless of whether the content set one, so restoring on the
        # attribute keeps the row stripe alive even when the content carried
        # only fg codes. Emitted as the bare background SGR (``\e[48;5;Nm``)
        # rather than via ``set_style`` so it's the exact restore byte the
        # rule specifies and is distinct from the stripe pad's own
        # ``set_style`` below.
        if row_bg is not None:
            write(f'\033[48;5;{row_bg}m')
    # ``row_bg`` extends the highlight across the trailing pad so the
    # row reads as a stripe rather than a tag-shaped patch. We bump
    # ``pad_to`` up to ``max_width`` in that case, but only locally —
    # callers that didn't ask for a stripe keep the existing behaviour
    # (no extra padding, let ``end_row`` handle pane-edge fill).
    effective_pad = pad_to if row_bg is None else max(pad_to, max_width)
    if effective_pad > pos:
        if row_bg is not None:
            set_style(bg=row_bg)
            write(' ' * (effective_pad - pos))
            reset_style()
        else:
            write(' ' * (effective_pad - pos))
        pos = effective_pad
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
    # Visible-cell count drives the pad math — wide chars (CJK / emoji)
    # consume two cells each, so ``len(line)`` under-counts and would
    # leave the row short or overflow into the next pane. Callers are
    # expected to have already truncated ``line`` to ``pad_to`` cells.
    visible = _visible_len(line)
    if not search_query:
        if base_fg is not None or base_bold or reverse:
            set_style(fg=base_fg, bold=base_bold, reverse=reverse)
        write(line)
        if pad_to > visible:
            write(' ' * (pad_to - visible))
        if base_fg is not None or base_bold or reverse:
            reset_style()
        return

    frags = search_query.lower().split()
    if not frags:
        if base_fg is not None or base_bold or reverse:
            set_style(fg=base_fg, bold=base_bold, reverse=reverse)
        write(line)
        if pad_to > visible:
            write(' ' * (pad_to - visible))
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

    if pad_to > visible:
        if reverse:
            set_style(reverse=True)
        write(' ' * (pad_to - visible))
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


def render_list(browser, rect, *, rightmost: bool = False):
    """Render the list pane for ``browser`` at the given :class:`Rect`.

    The ``rightmost`` keyword flag indicates whether the pane reaches the
    terminal's right edge (``rect.right == cols + 1``). It is plumbed
    through here in #186 so the differential renderer in #187/#188 can
    pass it into ``begin_row`` / ``end_row`` for the trailing-clear
    decision. Callers in earlier revisions may omit it; the default
    preserves the pre-#186 behaviour.

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

    # Cache state was reconciled by ``_reconcile_pane_caches`` upstream
    # (single per-frame entry point — see ticket #228). The renderer
    # just looks the cache up by name.
    cache = browser._pane_cache['list']

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

    # ``current_scope_id`` is the one piece of scope state the segment
    # builder needs — it picks ``scope_title`` over ``title`` for the
    # row that IS the current scope (see scope-root unification design).
    # Every other aspect of rendering is uniform across all rows.
    current_scope_id = state.scope_stack[-1] if state.scope_stack else None

    for row_idx in range(height):
        vis_idx = scroll + row_idx
        row = top + row_idx
        begin_row(cache, row_idx, row, left, right, rightmost=rightmost)

        if vis_idx >= len(visible):
            end_row()
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
            line = '  ' + '  ' * entry.depth + '  # ' + item.title
            line, line_cells = _truncate_by_cells(line, width)
            set_style(fg=11, bg=4, bold=True)
            write(line)
            if line_cells < width:
                write(' ' * (width - line_cells))
            reset_style()
            end_row()
            continue

        # Pending placeholder — indent + a single dim ``⧗ loading…`` glyph.
        # No selection / expand markers (the row is synthetic and doesn't
        # participate in selection / expansion). The three-hook split
        # applies to ``normal`` rows only, so this keeps its own early
        # path (moved here verbatim from the old ``format_item_segments``).
        if entry.kind == 'pending':
            indent = '  ' * (entry.depth if entry.depth > 0 else 0)
            segments = [
                (indent, None, False),
                (item.title, _PENDING_FG, False),
            ]
        else:
            # Normal row: build a per-row RowContext and make one resolved
            # call. ``browser._row_segments`` is the configured ``format_row``
            # override or the default chrome+content composer — no hook is
            # ``None`` here (they were bound once in ``Browser.__init__``).
            ctx = RowContext(
                browser, item,
                depth=entry.depth,
                selected=is_selected,
                expanded=item.id in state.expanded,
                is_current_scope=(item.id == current_scope_id),
                kind=entry.kind,
                list_width=width,
            )
            # A whole-row ``format_row`` override may return a ``str`` (ANSI
            # allowed) rather than a segment list (design sec 4.1);
            # ``_normalize_content`` coerces it to a single segment so the
            # rest of the pipeline sees a uniform segment list. The default
            # composer already normalised its content hook, so this is a
            # no-op on that path. (Same module namespace in the concatenated
            # build; injected in the isolated unit-test load.)
            segments = _normalize_content(browser._row_segments(item, ctx))

        if is_cursor_line:
            # Reverse video for the cursor line — collapse segments to a
            # plain VISIBLE-TEXT line so the search-highlighter can overlay
            # it. ``_collapse_visible`` drops segment ``fg`` (as ``s[0]``
            # always has) *and* any embedded SGR in a segment's text, so a
            # str-content row under the cursor reverses cleanly (sec 4.1).
            line = _collapse_visible(segments)
            line, _ = _truncate_by_cells(line, width)
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
            #
            # Meta rows are highlight-gated by ``meta_search_highlight``
            # (§5 meta-rows design): off by default so a meta row never
            # lights up under a query; on lets a matching meta row paint
            # spans like a normal row. Search *navigation* (n/N) always
            # skips meta regardless — that is ``_search_find``'s job.
            # ``_search_query`` gates the whole clause so the no-query
            # path stays cheap (no kind / config / match work).
            if (browser._search_query
                    and (entry.kind == 'normal'
                         or (entry.kind == 'meta'
                             and browser.meta_search_highlight))
                    and _search_matches(
                        _search_text(
                            item,
                            show_ids=browser.show_ids,
                            is_current_scope=(item.id == current_scope_id),
                        ),
                        browser._search_query)):
                # Same VISIBLE-TEXT collapse as the cursor line: drop fg and
                # any embedded SGR so the highlight overlay reads cleanly.
                line = _collapse_visible(segments)
                line, _ = _truncate_by_cells(line, width)
                _write_highlighted(
                    line, pad_to=0,
                    search_query=browser._search_query,
                )
            else:
                _write_segments(
                    segments, width,
                    row_bg=getattr(item, 'row_bg', None),
                    row_fg=getattr(item, 'row_fg', None),
                )
        end_row()


def _tokenise_line(line):
    """Yield alternating ``('text', s)`` / ``('csi', s)`` tokens for ``line``.

    Single ``_ANSI_CSI_RE.finditer`` pass — escape sequences come out as
    one ``csi`` token apiece, the runs of plain text between them come
    out as ``text`` tokens. Either kind may be absent (a line with no
    escapes is one ``text`` token; a line that's all escapes yields only
    ``csi`` tokens). Empty lines yield nothing.
    """
    pos = 0
    for m in _ANSI_CSI_RE.finditer(line):
        if m.start() > pos:
            yield ('text', line[pos:m.start()])
        yield ('csi', m.group(0))
        pos = m.end()
    if pos < len(line):
        yield ('text', line[pos:])


def _wrap_preview_line(line, width, *, ansi_on, drop_sgr=False):
    """Wrap a logical preview line into visual rows of <= ``width`` cols.

    ``ansi_on``  — keep SGR codes inline. False → strip all CSI (raw mode).
    ``drop_sgr`` — strip all SGR even in ANSI mode (used when a line
                   contains a search highlight; highlight wins).

    Each yielded row is self-contained: any active SGR is re-emitted at
    its start, and a trailing ``\\e[m`` is appended iff the row contains
    any SGR. Plain rows produce identical bytes whether ``ansi_on`` is
    True or False — preserves cache-hit invariants.

    Algorithm: regex-tokenised (one ``_ANSI_CSI_RE`` pass via
    :func:`_tokenise_line`), with a three-tier ASCII fast path on text
    tokens — the whole token, the current cut, then a per-char column
    fit only when wide chars are actually present in the cut.
    """
    if width <= 0:
        # Defensive: a zero-or-negative width can't fit anything.
        # Always emit at least one (empty) row so the caller's row
        # accounting matches the input.
        return ['']

    keep_sgr = ansi_on and not drop_sgr
    state = SgrState()
    rows = []
    seg = []          # current row's pieces
    seg_vis = 0       # visible cols accumulated in current row

    def end_row():
        # Trailing \e[m goes on iff the row's terminal SGR state would
        # bleed into the next pane / next row's pad — i.e. ``state`` is
        # non-empty. A row that opened a colour and explicitly reset it
        # mid-line (state empty at end) needs no extra reset.
        nonlocal seg, seg_vis
        if not state.is_empty():
            seg.append('\033[m')
        rows.append(''.join(seg))
        # Open the next row by re-emitting active SGR (if any).
        seg = []
        seg_vis = 0
        if keep_sgr:
            active = state.render()
            if active:
                seg.append(active)

    for kind, tok in _tokenise_line(line):
        if kind == 'csi':
            # SGR sequences (final byte 'm') feed the state when we're
            # keeping them; non-SGR CSI is always dropped (cursor moves
            # / erases shouldn't survive into preview output).
            if tok.endswith('m') and keep_sgr:
                seg.append(tok)
                state.feed(tok)
            # Else: drop. Non-SGR CSI, ansi_on=False, or drop_sgr=True.
            continue

        # text token
        token_is_ascii = tok.isascii()
        i = 0
        n = len(tok)
        while i < n:
            avail = width - seg_vis
            if avail <= 0:
                end_row()
                continue

            end = i + avail
            if end > n:
                end = n

            if token_is_ascii or tok[i:end].isascii():
                # Fast path: ASCII cut → one col per char.
                seg.append(tok[i:end])
                seg_vis += end - i
                i = end
            else:
                # Slow path: wide chars in this cut. Char-by-char fit.
                j = i
                taken = 0
                while j < end:
                    w = _char_width(tok[j])
                    if taken + w > avail:
                        break
                    taken += w
                    j += 1
                if j == i:
                    # Single wide char into avail<2 (or width=1): take it
                    # anyway — it would never fit otherwise. Caller's
                    # downstream truncation handles overflow visually.
                    j = i + 1
                    taken = _char_width(tok[i])
                seg.append(tok[i:j])
                seg_vis += taken
                i = j

            if seg_vis >= width:
                end_row()

    # Flush — always emit at least one row, even for an empty input line.
    # The "seg_vis > 0" check skips the dangling row that ``end_row``
    # opens when the input ended exactly at a wrap boundary: ``end_row``
    # re-emits the active SGR for the next segment, but if no text
    # follows we'd otherwise emit a content-free row of just the SGR
    # bytes. Always emit at least one row, though, so an empty input
    # line still produces a single empty row.
    if seg_vis > 0 or not rows:
        if not state.is_empty():
            seg.append('\033[m')
        rows.append(''.join(seg))

    return rows


# Stale-hold snapshot (preview-flicker design §B): the last successfully
# painted per-item preview, stored on ``Browser._preview_snapshot`` and
# painted in place of blank rows while the cursored row's preview is
# pending. Mirrors ``PreviewRender`` — ``wrapped`` plus the ``(width,
# ansi_on)`` geometry it was built for — extended with the raw ``text``
# (so a geometry/ANSI mismatch can re-wrap at paint time; the raw text
# is also what survives the #456 abandoned-partial cache clear) and the
# clamped ``scroll`` the paint showed (a held view is frozen there).
# Captured by reference assignment, never invalidated.
_PreviewSnapshot = namedtuple(
    '_PreviewSnapshot',
    ['text', 'scroll', 'wrapped', 'width', 'ansi_on'],
)


def _sanitize_and_wrap_preview(text, width, *, ansi_on):
    """Sanitize ``text`` and wrap it into preview rows ``width`` cols wide.

    The preview pane's shared content pipeline — the per-item and help
    paints and the stale-hold re-wrap all route through here. Returns
    ``(wrapped, wrapped_tail_offset)`` where ``wrapped_tail_offset`` is
    the index in ``wrapped`` at which the wrap of the last raw line
    starts — needed by the #423 in-place append-extension path to
    splice new wrapped rows over the previously-open tail line's wrap
    (callers that never splice ignore it).
    """
    # Strip control chars before they hit the terminal. Covers every
    # source so anything that reaches this pane is safe — preview data
    # can carry attacker-controlled bytes (binary files, raw terminal
    # captures, command stderr); help is composed in-process but cheap
    # to filter and recipes may supply ``help_intro`` / ``help_outro``.
    #
    # In raw mode (``ansi_on=False``) the sanitiser also maps ESC to
    # '?' so untrusted content can never inject SGR or other escape
    # sequences. The walker below sees no ESC bytes in that case and
    # falls through to plain wrap.
    text = _sanitize_preview(text, ansi_on=ansi_on)
    if ansi_on:
        # Escape-level sanitise (sec 4.2 #1), shared with the row-content
        # path: keep SGR, drop all other CSI and any bare/dangling ESC.
        # ``_wrap_preview_line`` also drops non-SGR CSI token-by-token,
        # but only the regex-matchable ones — this additionally removes a
        # bare ESC the tokeniser would otherwise pass through as text, and
        # makes the two paths behave identically. (Raw mode already
        # mapped every ESC to '?', so there's nothing left to strip.)
        text = _sanitize_ansi(text)

    # Wrap content to ``width`` columns.
    #
    # The wrap goes through ``_wrap_preview_line`` (#242) which
    # tokenises SGR sequences and emits self-contained visual rows
    # (active SGR re-opened at row start, trailing ``\e[m`` iff the
    # row carries any SGR). Plain rows are byte-identical whether
    # ``ansi_on`` is True or False — preserves the cache-hit
    # invariant for plain content.
    raw = text.split('\n') if text else []
    wrapped = []
    # ``wrapped_tail_offset`` is recorded just before extending
    # ``wrapped`` with the last raw line's rows. Defaults to
    # ``len(wrapped)`` (post-wrap) so an empty preview / no-raw-lines
    # branch records 0.
    wrapped_tail_offset = 0
    last_idx = len(raw) - 1
    for i, line in enumerate(raw):
        line = line.replace('\t', '    ')
        if i == last_idx:
            wrapped_tail_offset = len(wrapped)
        wrapped.extend(_wrap_preview_line(
            line, width, ansi_on=ansi_on, drop_sgr=False))
    return wrapped, wrapped_tail_offset


def render_preview(browser, rect, *, info=False, has_header=True,
                   rightmost: bool = False):
    """Render the preview pane (header + content) within ``rect``.

    ``rightmost`` is the pane-touches-right-edge flag plumbed through in
    #186 for the differential renderer (#187/#188). It defaults to False
    so existing callers continue to work; ``render_full`` /
    ``render_partial`` pass the value computed by ``_layout_for``.

    When ``has_header=True`` the first row of ``rect`` is the info-bar
    header (label + optional ``[N]`` / search prompt / hints when
    ``info=True``); content occupies ``rect.height - 1`` rows below.
    When ``has_header=False`` the entire rect is content (used by
    non-'h' layouts where the info bar lives at the bottom of the
    screen, drawn by ``render_full`` independently).

    The header label adapts to browser state (``Help`` / ``Preview`` /
    ``⧗ Preview`` while a fetch for the cursored item is outstanding —
    see ``_preview_label``). Errors no longer take over the pane — they
    surface as an info-bar notice + log entry (see ``Browser.error``).

    Source priority for the content:
      1. ``browser._help_mode`` if True      → display ``compose_help_text``
      2. ``item.preview`` (cursor item),
         delivered for this visit            → per-item preview
      3. stale-hold — cursor row's preview
         pending (undelivered, or cached but
         the visit hasn't settled) and a
         snapshot of the last painted
         preview exists                      → hold the snapshot
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

    cache = browser._pane_cache['preview']

    if has_header:
        label = _preview_label(browser)
        # In 'h' layout the preview's first row IS the full-width info
        # bar (left=1, right=cols+1). For other layouts the info bar
        # is drawn separately on the bottom row by render_full and we
        # take the ``has_header=False`` branch.
        # Header row uses begin_row/end_row to participate in the cache;
        # the info-bar writer emits its own move/clear via the captured
        # primitives so cache hits silence repeat paints.
        begin_row(cache, 0, top, left, right, rightmost=rightmost)
        render_info_bar(top, right - 1, label, info=info, browser=browser)
        end_row()
        content_top = top + 1
        content_lines = height - 1
        content_row_offset = 1
    else:
        content_top = top
        content_lines = height
        content_row_offset = 0
    if content_lines <= 0:
        return

    # ``preview_ansi`` is stored on Browser since #244; the attribute is
    # always present, no defensive default required.
    ansi_on = browser.preview_ansi

    # Resolve the cached Item (when in the per-item branch). When we
    # land on a real item, ``item.preview_render`` is the wrap cache
    # candidate; honour it when the geometry and ANSI policy still
    # match, otherwise regenerate.
    #
    # ``pending`` marks a cursor row the pane must keep holding over:
    # the synthetic ``'pending'`` placeholder row, or a ``'normal'``
    # row with ``preview is None OR not _preview_visit_delivered``
    # (#954). The two legs cover distinct waits: ``preview is None``
    # keeps its #940 meaning — a delivery is pending or imminent
    # (#442: every fetch delivers at least ``''``), which also
    # preserves the hold across invalidate-on-the-cursored-item; the
    # per-visit bit adds the cached-row settle window — content is in
    # hand but the cursor hasn't settled on it, so swapping now would
    # churn during a scroll burst over visited rows. A streaming first
    # chunk sets the bit (apply path), so progressive streaming still
    # swaps immediately. Delivered text for a settled visit — including
    # ``''`` — paints as-is. Meta rows resolve their item like normal
    # rows but never pend: ``_update_preview_for_cursor`` requests
    # nothing for them, so no delivery or nudge would ever end a hold
    # — a missing preview just blanks. An empty visible list is never
    # pending either: the pane isn't waiting for anything.
    item = None
    pending = False
    if browser._help_mode:
        text = compose_help_text(browser, include_usage=False)
    else:
        text = ''
        vis = visible_items(browser._state)
        cur = browser._state.cursor
        if 0 <= cur < len(vis):
            entry = vis[cur]
            if entry.kind == 'pending':
                pending = True
            else:
                item = browser._state._items_by_id.get(entry.item.id)
                if item is not None and item.preview is not None:
                    text = item.preview
                    if (entry.kind == 'normal'
                            and not browser._preview_visit_delivered):
                        pending = True
                elif entry.kind == 'normal':
                    pending = True

    # Stale-hold (preview-flicker design §B): while the cursor row
    # pends, keep painting the last successfully painted per-item
    # preview — instead of blanking (undelivered row) or swapping
    # early (cached row whose visit hasn't settled, #954). The real
    # content swaps in one step when the delivery or settle nudge
    # lands. The branch is self-contained: the held
    # view is frozen at the snapshot's clamped scroll, the snapshot's
    # wrap is never written into the cursored item's ``preview_render``,
    # the snapshot itself is never updated from here, and the
    # scroll/tail-pin writebacks and #274 demand signal below are
    # skipped. The row cache makes repeated identical held paints emit
    # zero bytes.
    if pending:
        snap = browser._preview_snapshot
        if snap is not None:
            wrapped = snap.wrapped
            if snap.width != width or snap.ansi_on != ansi_on:
                # Pane geometry / ANSI policy changed since the capture
                # (resize, screen-restore, ansi toggle) — re-wrap from
                # the snapshot's raw text for this paint.
                wrapped, _ = _sanitize_and_wrap_preview(
                    snap.text, width, ansi_on=ansi_on,
                )
            max_scroll = max(0, len(wrapped) - content_lines)
            scroll = max(0, min(snap.scroll, max_scroll))
            for i in range(content_lines):
                row = content_top + i
                rel_row = content_row_offset + i
                begin_row(cache, rel_row, row, left, right,
                          rightmost=rightmost)
                src_idx = i + scroll
                if src_idx < len(wrapped):
                    write(wrapped[src_idx])
                end_row()
            return

    # Wrap-cache fast path (#422) — only for per-item previews
    # (help text is composed each paint anyway), and only when
    # geometry + ANSI policy match the cache.
    wrapped = None
    if item is not None and not browser._help_mode:
        cached = item.preview_render
        if (cached is not None
                and cached.width == width
                and cached.ansi_on == ansi_on):
            wrapped = cached.wrapped

    if wrapped is None:
        wrapped, wrapped_tail_offset = _sanitize_and_wrap_preview(
            text, width, ansi_on=ansi_on,
        )

        # Cache the wrap on the Item when it's a per-item render.
        # The help branch recomputes on every paint and doesn't share
        # the cache slot.
        #
        # ``raw_tail_offset`` is the position in ``item.preview`` just
        # after the last ``\n`` (or 0 if no newline yet, or
        # ``len(preview)`` when the preview ends with a newline — both
        # falling out of ``rfind('\n') + 1`` naturally). This is the
        # start of the currently-open partial last raw line, which is
        # the splice point for #423 in-place ``append_preview``.
        if item is not None and not browser._help_mode:
            raw_text = item.preview if item.preview is not None else ''
            last_nl = raw_text.rfind('\n')
            raw_tail_offset = 0 if last_nl < 0 else last_nl + 1
            item.preview_render = PreviewRender(
                wrapped=wrapped,
                raw_tail_offset=raw_tail_offset,
                wrapped_tail_offset=wrapped_tail_offset,
                width=width,
                ansi_on=ansi_on,
            )

    # Clamp ``_preview_scroll`` so the last content row lands at the
    # bottom of the pane when fully scrolled — conventional viewport
    # semantics. ``_preview_scroll_down`` / ``_preview_page_down``
    # (070-actions.py) bump the offset without an upper bound (they
    # don't have wrap geometry in scope), so we clamp here where
    # ``wrapped`` and ``content_lines`` are both in hand and write the
    # clamped value back so subsequent shift-up presses don't have to
    # pump down through a phantom count.
    #
    # ``_preview_at_tail`` pin: when engaged (Shift/Alt-End or
    # ``Browser.preview_to_tail``), force ``scroll = max_scroll`` and
    # write it back. As ``wrapped`` grows from streaming appends or
    # generator pulls, the next render lands the view at the new
    # bottom — tail-follow without per-tick user input. The flag is
    # cleared by upward motions in the action layer; downward motions
    # are clamped here so the pin survives them naturally.
    max_scroll = max(0, len(wrapped) - content_lines)
    if browser._preview_at_tail:
        scroll = max_scroll
        if browser._preview_scroll != max_scroll:
            browser._preview_scroll = max_scroll
    else:
        scroll = max(0, min(browser._preview_scroll, max_scroll))
        if scroll != browser._preview_scroll:
            browser._preview_scroll = scroll

    # #274: demand-pull signal. If the cursored id's preview generator
    # is paused at the cap and the user has scrolled near the buffered
    # tail, wake the worker to resume pulling. Skip when showing help
    # content (text source isn't the per-item preview).
    if not browser._help_mode and len(wrapped) > 0:
        cursor_id = _cursor_id(browser)
        if cursor_id is not None:
            visible_end = scroll + content_lines  # exclusive row index
            rows_below = len(wrapped) - visible_end
            if rows_below < _PREVIEW_DEMAND_THRESHOLD:
                # Debounce: only fire when scroll has moved since the
                # last signal for this id. Re-fires unconditionally on
                # id-change so a fresh cursor-and-scroll combination
                # always gets one wake.
                state = browser._preview_demand_signal_state
                if state is None or state != (cursor_id, scroll):
                    paused = browser._preview_paused
                    if (paused is not None
                            and paused.get('id') == cursor_id):
                        browser._preview_demand_signal_state = (
                            cursor_id, scroll
                        )
                        browser.signal_preview_demand(cursor_id)

    for i in range(content_lines):
        row = content_top + i
        rel_row = content_row_offset + i
        begin_row(cache, rel_row, row, left, right, rightmost=rightmost)
        src_idx = i + scroll
        if src_idx < len(wrapped):
            write(wrapped[src_idx])
        end_row()

    # Stale-hold snapshot: capture this paint (reference assignments, no
    # copying) so a later pending row holds it. Per-item content paints
    # only — help mode never owns the snapshot, and an undelivered row
    # that fell through above (no snapshot yet) painted blank, which is
    # not content worth holding. A delivered ``''`` is: a held view must
    # match whatever the last paint actually showed. A cached-but-
    # unsettled row that fell through (#954, also only when no snapshot
    # exists yet) painted its own real cache and is captured like any
    # content paint.
    if (item is not None and not browser._help_mode
            and item.preview is not None):
        browser._preview_snapshot = _PreviewSnapshot(
            text=item.preview,
            scroll=scroll,
            wrapped=wrapped,
            width=width,
            ansi_on=ansi_on,
        )


def _cursor_id(browser):
    """Return the id of the item currently under the cursor, or None."""
    vis = visible_items(browser._state)
    cur = browser._state.cursor
    if 0 <= cur < len(vis):
        return vis[cur].item.id
    return None


def _children_displayed_item(browser):
    """Return the Item the children pane currently describes (#959).

    The pane renders ``browser._children_displayed_id`` — advanced by
    the main loop only once the preview settles on the cursored row —
    rather than the live cursor, so during a scroll burst it keeps
    matching the held preview and both panes swap in one paint.
    Resolves through ``_items_by_id`` so a displayed parent removed by
    a data update yields ``None`` and the pane hides (honest; the next
    settle re-advances).
    """
    id_ = browser._children_displayed_id
    if id_ is None:
        return None
    return browser._state._items_by_id.get(id_)


def render_children_grid(browser, rect, *, info=False, has_header=True,
                         rightmost: bool = False):
    """Render the children-grid pane (header + content) within ``rect``.

    ``rightmost`` is the pane-touches-right-edge flag plumbed through in
    #186 for the differential renderer (#187/#188). It defaults to False
    so existing callers continue to work; ``render_full`` /
    ``render_partial`` pass the value computed by ``_layout_for``.

    When ``has_header=True`` the first row of ``rect`` is the info-bar
    header (label ``Children`` plus optional ``[N]`` / search prompt /
    hints when ``info=True``); content occupies ``rect.height - 1``
    rows below. When ``has_header=False`` the entire rect is content
    (used by non-'h' layouts where the info bar lives at the bottom of
    the screen).

    The pane's subject is the DISPLAYED parent (#959) —
    ``_children_displayed_item``, which lags the cursor until the
    preview settles — and the children come from
    ``browser._state._children.get(parent.id, [])``, fetched lazily by
    the children worker (whose requests keep following the live cursor).

    Behaviour:
      * Displayed parent is a leaf or has empty cached children →
        blank content area (the layout normally already set
        ``sub_height = 0`` to elide the pane entirely; we render
        defensively).
      * Displayed branch whose children aren't cached yet → single
        ``⧗ loading…`` row in dim, mirroring the placeholder used in
        the list pane. Because the displayed id only advances at
        settle, this hint appears for the settled row — never for
        rows skimmed mid-scroll.
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

    cache = browser._pane_cache['children']

    if has_header:
        # Header row participates in the cache so a static info bar
        # doesn't repaint on every keystroke.
        begin_row(cache, 0, top, left, right, rightmost=rightmost)
        render_info_bar(top, right - 1, 'Children', info=info, browser=browser)
        end_row()
        content_top = top + 1
        content_lines = height - 1
        content_row_offset = 1
    else:
        content_top = top
        content_lines = height
        content_row_offset = 0
    if content_lines <= 0:
        return

    parent = _children_displayed_item(browser)
    if parent is None:
        for i in range(content_lines):
            row = content_top + i
            rel_row = content_row_offset + i
            begin_row(cache, rel_row, row, left, right, rightmost=rightmost)
            end_row()
        return

    state = browser._state
    children = state._children.get(parent.id)

    # Pending: displayed branch whose children aren't cached yet.
    if children is None and parent.has_children:
        begin_row(cache, content_row_offset, content_top, left, right,
                  rightmost=rightmost)
        set_style(fg=_PENDING_FG)
        write('⧗ loading…'[:width])
        reset_style()
        end_row()
        for i in range(1, content_lines):
            row = content_top + i
            rel_row = content_row_offset + i
            begin_row(cache, rel_row, row, left, right, rightmost=rightmost)
            end_row()
        return

    # Cached but empty (or a leaf) — nothing to draw, blank rows.
    if not children:
        for i in range(content_lines):
            row = content_top + i
            rel_row = content_row_offset + i
            begin_row(cache, rel_row, row, left, right, rightmost=rightmost)
            end_row()
        return

    # Cached non-empty children — multi-column flowed layout. Routed
    # through ``browser.children_grid_layout`` which recomputes via
    # ``_sub_layout`` and stores on the Browser (#434).
    num_cols, col_width, slot_rows, entry_lines = browser.children_grid_layout(
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

    for row_offset in range(content_lines):
        abs_row = content_top + row_offset
        rel_row = content_row_offset + row_offset
        begin_row(cache, rel_row, abs_row, left, right, rightmost=rightmost)
        if row_offset >= total_rows:
            end_row()
            continue
        col_pos = 0
        for c in range(num_cols):
            cl = col_lines[c]
            cell = cl[row_offset] if row_offset < len(cl) else ''
            if c < num_cols - 1:
                cell_w = col_width
            else:
                cell_w = width - col_pos
            if cell_w <= 0:
                break
            src_idx = col_entry_at[c].get(row_offset)
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
        end_row()


def render_children_list(browser, rect, *, info=False, has_header=True,
                         rightmost: bool = False):
    """Render the children pane as a one-per-row vertical list (Alt-1).

    ``rightmost`` is the pane-touches-right-edge flag plumbed through in
    #186 for the differential renderer (#187/#188). It defaults to False
    so existing callers continue to work; ``render_full`` /
    ``render_partial`` pass the value computed by ``_layout_for``.

    Used by the vertical (``split='v'``) layout per #176, where the
    children column sits between the list and the preview, occupying the
    full body height. Each direct child of the displayed parent is
    written on its own row, truncated to the column width. The parent is
    read from ``_children_displayed_item`` (#959 — it lags the cursor
    until the preview settles); the children come from
    ``browser._state._children.get(parent.id, [])``.

    The header / no-parent / loading / empty branches mirror
    :func:`render_children_grid` so the two renderers degrade identically.
    """
    if rect is None or rect.height <= 0 or rect.width <= 0:
        return

    top = rect.top
    height = rect.height
    width = rect.width
    left = rect.left
    right = rect.right

    # render_children_list and render_children_grid share the 'children'
    # cache key — the rect-based update_rect() invalidates the buffer on
    # layout style switches (vertical ↔ flowed grid) automatically. The
    # cache state is reconciled by ``_reconcile_pane_caches`` upstream.
    cache = browser._pane_cache['children']

    if has_header:
        begin_row(cache, 0, top, left, right, rightmost=rightmost)
        render_info_bar(top, right - 1, 'Children', info=info, browser=browser)
        end_row()
        content_top = top + 1
        content_lines = height - 1
        content_row_offset = 1
    else:
        content_top = top
        content_lines = height
        content_row_offset = 0
    if content_lines <= 0:
        return

    parent = _children_displayed_item(browser)
    if parent is None:
        for i in range(content_lines):
            row = content_top + i
            rel_row = content_row_offset + i
            begin_row(cache, rel_row, row, left, right, rightmost=rightmost)
            end_row()
        return

    state = browser._state
    children = state._children.get(parent.id)

    # Pending: displayed branch whose children aren't cached yet.
    if children is None and parent.has_children:
        begin_row(cache, content_row_offset, content_top, left, right,
                  rightmost=rightmost)
        set_style(fg=_PENDING_FG)
        write('⧗ loading…'[:width])
        reset_style()
        end_row()
        for i in range(1, content_lines):
            row = content_top + i
            rel_row = content_row_offset + i
            begin_row(cache, rel_row, row, left, right, rightmost=rightmost)
            end_row()
        return

    # Cached but empty (or a leaf) — nothing to draw, blank rows.
    if not children:
        for i in range(content_lines):
            row = content_top + i
            rel_row = content_row_offset + i
            begin_row(cache, rel_row, row, left, right, rightmost=rightmost)
            end_row()
        return

    # Render each child on its own row using the coloured segments path.
    for row_idx in range(content_lines):
        abs_row = content_top + row_idx
        rel_row = content_row_offset + row_idx
        begin_row(cache, rel_row, abs_row, left, right, rightmost=rightmost)
        if row_idx >= len(children):
            end_row()
            continue
        child = children[row_idx]
        segs = _child_segments(child, width, show_ids=browser.show_ids)
        _write_segments(segs, width)
        end_row()


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


def render_separator(rect, *, orientation=None, content=None,
                     rightmost: bool = False, cache_key=None, browser=None):
    """Render a plain pane-separator view.

    ``rightmost`` is the pane-touches-right-edge flag plumbed through in
    #186 for the differential renderer (#187/#188). Internal vertical
    separators in v/m/pc layouts never touch the right edge, so the
    default of False is correct for nearly all call sites; the
    ``render_full`` / ``render_partial`` orchestrators still pass the
    value explicitly for symmetry with the other renderers.

    ``cache_key`` + ``browser`` (added in #188) wire the separator into
    the per-pane row cache so unchanged-rect repaints emit zero bytes.
    Production callers in ``render_full`` / ``render_partial`` pass
    ``cache_key='sep_main'`` / ``'sep_inner'`` plus ``browser``; legacy
    / test callers may omit both, falling back to the direct-write path.

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

    use_cache = cache_key is not None and browser is not None
    if use_cache:
        # Cache state was reconciled by ``_reconcile_pane_caches``
        # upstream (#228). The renderer just looks the cache up.
        cache = browser._pane_cache[cache_key]

    if orientation == 'v':
        # Draw the vertical bar down the rect's left column. Most
        # callers pass a 1-col-wide rect; if wider, only the leftmost
        # column is filled (the rest is the pane content's
        # responsibility — the renderer doesn't repaint pane interiors).
        if use_cache:
            left = rect.left
            right = rect.right
            for rel_row in range(rect.height):
                abs_row = rect.top + rel_row
                begin_row(cache, rel_row, abs_row, left, right,
                          rightmost=rightmost)
                set_style(fg=8)
                write('│')
                reset_style()
                end_row()
        else:
            set_style(fg=8)
            for r in range(rect.top, rect.bottom):
                move(r, rect.left)
                write('│')
            reset_style()
        return

    # Horizontal — production callers always pass a 1-row rect (the
    # separator is conceptually 1-thick), but the renderer paints every
    # row of the rect with the same ``─`` content so that:
    #   * the cache is fully populated (rows 1..n-1 can't be left
    #     uninitialised, which would defeat the zero-byte invariant on
    #     the next paint), and
    #   * any future multi-row caller gets a consistent thicker bar
    #     instead of a half-painted rect.
    # This mirrors the vertical-cached branch above, which already loops
    # over all rows and writes the same ``│`` glyph on each.
    width = rect.width
    left = rect.left
    right = rect.right
    for rel_row in range(rect.height):
        abs_row = rect.top + rel_row
        if use_cache:
            begin_row(cache, rel_row, abs_row, left, right,
                      rightmost=rightmost)
        else:
            move(abs_row, left)

        set_style(fg=8)
        if content:
            # Truncate content to leave a 1-col padding on each side,
            # then centre it between two ``─`` runs. The content is
            # repeated on every row of a multi-row rect (consistent
            # with the bar-glyph treatment for vertical separators).
            max_content = max(0, width - 2)
            ctext = (content if len(content) <= max_content
                     else content[:max_content])
            ctext_len = len(ctext)
            # Surrounding ``─`` runs split the leftover width.
            leftover = width - ctext_len
            left_run = leftover // 2
            right_run = leftover - left_run
            if left_run > 0:
                write('─' * left_run)
            if ctext:
                # Reset to default fg for the content so it reads
                # clearly, then return to the dim separator colour.
                reset_style()
                write(ctext)
                set_style(fg=8)
            if right_run > 0:
                write('─' * right_run)
        else:
            write('─' * width)
        reset_style()

        if use_cache:
            end_row()


def render_info_bar(row, cols, label, *, info=False, browser=None,
                    rightmost: bool = False, manage_cache: bool = False):
    """Render the info-bar / pane-separator row with rich decoration.

    ``rightmost`` is the pane-touches-right-edge flag plumbed through in
    #186 for the differential renderer (#187/#188). The info bar always
    spans the full terminal width in current layouts, so production
    callers always pass True; the default of False is conservative for
    legacy/test call sites.

    ``manage_cache`` (added in #188): when True (and ``browser`` is
    provided), wrap the writes in a ``begin_row`` / ``end_row`` pair
    backed by ``browser._pane_cache['info_bar']`` so unchanged repaints
    emit zero bytes. Folded callers — ``render_preview`` /
    ``render_children_grid`` / ``render_children_list`` — leave it
    False because they already drive their own pane cache around the
    info-bar header row (the row participates in the parent pane's
    cache, not its own).

    Historically this function was called ``render_separator``; it was
    promoted to a dedicated ``render_info_bar`` in #147 so the simpler
    plain-divider :func:`render_separator` can carry a clean signature.

    When ``info=True`` the bar additionally shows:
      * ``[N]`` selection count (if non-zero) in bold cyan;
      * a scope crumb path (when ``scope_stack`` is non-empty) — one
        ``▸ <id>`` segment per stack entry in bright cyan;
      * the search prompt + query (when ``browser._mode is Mode.SEARCH_EDIT``);
      * an info-bar notice (``browser._notice``) in place of the hint —
        red+bold for an error, dim for a flash;
      * a dim hint string about navigation keys (when none of the above).

    Middle-region priority: search / filter prompt > notice > hint.

    The right edge ends with the pane label (``Preview``, ``Help``, …).

    No truncation is applied to the scope crumb in this phase — on a
    narrow terminal the crumb may push the hint text out of view (or
    eat into the trailing filler before the label). Writes are still
    clamped at ``cols`` so nothing spills off-screen; only hint/filler
    visibility degrades. Phase-3 can layer adaptive truncation.
    """
    use_cache = manage_cache and browser is not None and cols > 0
    if use_cache:
        # Cache state was reconciled by ``_reconcile_pane_caches``
        # upstream (#228) — the renderer just looks the cache up.
        cache = browser._pane_cache['info_bar']
        begin_row(cache, 0, row, 1, cols + 1, rightmost=rightmost)

    S = '─'  # ─
    if not use_cache:
        # In the cached path, ``begin_row`` records (row, left) and
        # ``end_row`` emits ``\e[<abs_row>;<left>H`` itself, then handles
        # trailing-cell clearing via its pad / ``\e[K`` logic — so the
        # explicit move + clear_line would be captured into the row
        # buffer and re-emitted on every cache miss as redundant bytes.
        # The legacy non-cached call sites still rely on them.
        move(row, 1)
        clear_line()

    if info and browser is not None:
        sel_count = len(browser._state.selected)
        search = browser._search_query if browser._mode is Mode.SEARCH_EDIT else None
        # Info-bar notice (flash / error) — shown in the middle region in
        # place of the hint, but below the search / filter prompt. Pane
        # headers (info=False) never surface it.
        notice = browser._notice
        crumb = (
            _scope_crumb_text(browser)
            if getattr(browser, 'show_scope_crumb', False)
            else ''
        )
        # Filter prompt — built whenever filters are active or the user
        # is editing one. Joined with ' & ' to match the display in the
        # design spec (e.g. "foo & bar & ba_" mid-edit). The trailing
        # underscore is appended when in FILTER_EDIT so the user sees
        # the cursor position. See
        # docs/superpowers/specs/2026-05-17-filter-design.md.
        filt = None
        if browser._mode is Mode.FILTER_EDIT or browser._filters:
            entries = list(browser._filters)
            if browser._mode is Mode.FILTER_EDIT and entries:
                # Last entry is the live one — render with a trailing
                # underscore so the user sees the active prompt.
                live = entries[-1]
                committed = entries[:-1]
                live_part = (live or '') + '_'
                shown = list(committed) + [live_part]
            else:
                shown = entries
            filt = ' & '.join(shown)
    else:
        sel_count = 0
        search = None
        crumb = ''
        filt = None
        notice = None

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

    # Search prompt (when searching), filter prompt (when filtering),
    # or context-sensitive hints. Search and filter cannot be active
    # simultaneously (modes are mutually exclusive), so the order of
    # checks is just a stylistic preference.
    #
    # Reverse-video the prompt only while the user is *editing*
    # (SEARCH_EDIT / FILTER_EDIT). A committed filter that's no longer
    # being typed renders in plain styled text — visible enough that
    # the user knows filtering is on, quiet enough that it doesn't
    # compete with the rest of the info row.
    filter_editing = (
        info and browser is not None
        and browser._mode is Mode.FILTER_EDIT
    )
    if search is not None and pos < cols:
        line = '/' + search
        set_style(reverse=True, bold=True)
        write(line[:cols - pos])
        pos += len(line)
        reset_style()
        set_style(fg=8)
    elif filt is not None and pos < cols:
        line = '& ' + filt
        if filter_editing:
            set_style(reverse=True, bold=True)
        else:
            set_style(fg=11, bold=True)
        write(line[:cols - pos])
        pos += len(line)
        reset_style()
        set_style(fg=8)
    elif notice is not None and pos < cols:
        # Info-bar notice — in place of the hint, truncated to the same
        # budget. Red + bold for errors (loud); a quiet dim style for
        # flashes (distinct from the dimmer hint, but not shouty).
        avail = cols - pos - len(label_str) - 3
        if avail > 10:
            text = notice.text[:avail]
            if notice.kind == 'error':
                set_style(fg=9, bold=True)
            else:
                set_style(fg=250, bold=True)
            write(text)
            pos += len(text)
        reset_style()
        set_style(fg=8)
    elif info and pos < cols:
        # Hint text — recipe-overridable via ``Browser.set_hint`` /
        # ``Context.set_hint``. Fall back to the shared default so a
        # browser without the attribute still renders the stock hint.
        hints = getattr(browser, 'hint', DEFAULT_HINT)
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

    if use_cache:
        end_row()


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def _layout_for(browser):
    """Build the geometry dict from current terminal size + browser flags.

    Children sizing follows the DISPLAYED parent (#959): if
    ``show_children_pane`` is set and the displayed parent is a branch
    whose children are cached, we ask the multi-column layout helpers
    how many content rows it would need and pass the answer to
    ``layout_panes`` so the grid pane shrinks to fit. A displayed leaf
    (or no displayed parent yet) yields ``children_rows_needed=0``; a
    displayed branch whose children haven't been fetched reserves one
    row for the ``⧗ loading…`` hint. Because the displayed id only
    advances when the preview settles, none of these transitions can
    reshape the layout mid-scroll — the pane swaps, grows, or hides in
    the settle paint, together with the preview.
    """
    cols, rows = term_size()
    children_rows = 0
    if browser.show_children_pane and not browser._headless:
        parent = _children_displayed_item(browser)
        if parent is not None and parent.has_children:
            cached = browser._state._children.get(parent.id)
            if cached:
                # Route through ``children_grid_layout`` which
                # recomputes via ``_sub_layout`` and stores the result
                # on the Browser (#434). ``render_children_grid`` will
                # recompute again on its own call — cheap, acceptable.
                layout_ = browser.children_grid_layout(
                    cached, cols, show_ids=browser.show_ids,
                )
                children_rows = _sub_total_rows(
                    layout_.num_cols, layout_.slot_rows,
                )
                # Per #180 the vertical (Alt-1) children column width is
                # CONTENT-INDEPENDENT (fixed at 25% of the right area),
                # so we no longer compute a per-cursor width hint.
            elif cached is None:
                # Branch with not-yet-cached children — reserve one row
                # for the loading hint so the grid is visible while the
                # fetch is in flight.
                children_rows = 1
            # cached == [] (empty list): leaf-like, no rows reserved.
    layout = layout_panes(
        cols, rows,
        split=getattr(browser, 'split', 'h'),
        show_preview=browser.show_preview,
        show_children_pane=browser.show_children_pane,
        children_rows_needed=children_rows,
        list_ratio=browser.list_ratio,
    )
    # Per-pane "rightmost" flags for the differential renderer (#186).
    # A pane is rightmost iff its right edge coincides with the layout's
    # right edge. ``Rect.right`` is exclusive, so for ``cols=80`` a pane
    # reaching column 80 has ``right == 81 == cols + 1``. The flags ride
    # alongside each pane rect under a sibling ``*_rightmost`` key so the
    # ``render_full`` / ``render_partial`` call sites have the bool
    # ready at zero per-row cost. Hidden panes (None rect) get False.
    right_edge = layout['cols'] + 1

    def _is_rightmost(rect):
        return rect is not None and rect.right == right_edge

    layout['list_rightmost'] = _is_rightmost(layout.get('list'))
    layout['children_rightmost'] = _is_rightmost(layout.get('children'))
    layout['preview_rightmost'] = _is_rightmost(layout.get('preview'))
    # Cache the live preview-pane width on the Browser so ``get_preview``
    # callbacks (and other recipe code) can size markdown wrap / frames
    # to the actual pane without re-deriving the layout. Zero when the
    # preview pane isn't visible.
    prect = layout.get('preview')
    browser._preview_width = (
        prect.width if prect is not None and prect.width > 0 else 0
    )
    # Layout signature for the broadened ``on_resize`` hook (#828). The
    # run loop fires ``on_resize`` whenever this differs from the
    # last-fired value, so it must change on EVERY pane-layout change the
    # hook cares about — terminal resize, split selector, list-ratio
    # nudge, pane toggle — and on nothing else. ``cols``/``rows`` are the
    # RAW ``term_size()`` values (NOT ``layout['cols']/['rows']``, which
    # ``layout_panes`` clamps to >=1): the fire path passes them to the
    # callback and skips firing when they're zero (headless / no-tty), so
    # the unclamped pair is what preserves the "don't fire garbage dims"
    # guard. The preview + children rects (position AND size) capture
    # every geometric reshape — a list-ratio change moves the preview's
    # height in 'h' and its width in 'v', and pane toggles flip a rect
    # to/from ``None`` — which is exactly when a width-dependent preview
    # may need re-rendering (height-only changes fire too, harmlessly).
    browser._layout_sig = (cols, rows, prect, layout.get('children'))
    # The info bar always spans the full width in current layouts (the
    # pane separator that owns it is full-width by construction), so
    # it's rightmost whenever it exists.
    layout['info_bar_rightmost'] = _is_rightmost(layout.get('info_bar'))
    # Separators in v/m/pc layouts are vertical bars splitting the body;
    # they sit at internal column boundaries and never reach the right
    # edge. Compute the flag explicitly for symmetry — the differential
    # renderer treats every paint target uniformly.
    layout['sep_main_rightmost'] = _is_rightmost(layout.get('sep_main'))
    layout['sep_inner_rightmost'] = _is_rightmost(layout.get('sep_inner'))
    return layout


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

    Lays out the panes, paints each, then flushes. The whole sequence is
    bracketed by a DEC mode 2026 "begin synchronized output" / "end
    synchronized output" pair so terminals that support BSU/ESU swap the
    new frame in atomically without tearing.

    Resets ``browser._needs_redraw`` so subsequent partial-render calls
    start from a clean slate. The historical ``\\e[2J`` blanket-clear
    is gone (#185–#188): the differential renderer relies on the row
    cache to detect stale columns rather than forcing a flash repaint.
    """
    begin_sync()
    layout = _layout_for(browser)
    _reconcile_pane_caches(browser, layout)
    sub_info, prev_info = _pane_info_flags(layout)
    info_separate = _info_bar_is_separate(layout)
    list_rect = layout['list']
    render_list(browser, list_rect, rightmost=layout.get('list_rightmost', False))
    children_rect = layout['children']
    if children_rect is not None:
        # Alt-1 (vertical) renders children as a one-per-row list; the
        # other splits use the multi-column flowed grid.
        if getattr(browser, 'split', 'h') == 'v':
            render_children_list(
                browser, children_rect,
                info=sub_info,
                has_header=not info_separate,
                rightmost=layout.get('children_rightmost', False),
            )
        else:
            render_children_grid(
                browser, children_rect,
                info=sub_info,
                has_header=not info_separate,
                rightmost=layout.get('children_rightmost', False),
            )
    preview_rect = layout['preview']
    if browser.show_preview and preview_rect is not None:
        render_preview(
            browser, preview_rect,
            info=prev_info,
            has_header=not info_separate,
            rightmost=layout.get('preview_rightmost', False),
        )
    if (info_separate or not browser.show_preview) and layout['info_bar'] is not None:
        # Non-'h' layouts (or show_preview=False) — info bar is a
        # standalone row drawn here. Rich label + decorations.
        render_info_bar(
            layout['info_bar'].top, layout['cols'], _preview_label(browser),
            info=True, browser=browser,
            rightmost=layout.get('info_bar_rightmost', False),
            manage_cache=True,
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
        render_separator(
            sep_main,
            rightmost=layout.get('sep_main_rightmost', False),
            cache_key='sep_main', browser=browser,
        )
    if sep_inner is not None:
        render_separator(
            sep_inner,
            rightmost=layout.get('sep_inner_rightmost', False),
            cache_key='sep_inner', browser=browser,
        )
    end_sync()
    flush()
    browser._needs_redraw = set()


def render_partial(browser):
    """Selective redraw based on ``browser._needs_redraw``.

    Recognised flags: ``'list'``, ``'children'``, ``'preview'``,
    ``'info'``, ``'all'``. ``'all'`` short-circuits to ``render_full``.
    Unknown flags are ignored (no error) so callers can stuff "hint"
    tokens in the set without crashing the renderer.

    Pane separators (``sep_main`` / ``sep_inner``) are redrawn whenever
    any pane is repainted. They're cheap (a single column / row of
    box-drawing chars) and the layout can change shape between paints
    (e.g. children pane appearing when the cursor moves onto a branch
    in Alt-1 vertical layout) — keeping them passive on partial redraws
    leaves stale gaps where the new separator should be (#180).
    """
    needs = browser._needs_redraw
    if 'all' in needs:
        render_full(browser)
        return
    if not needs:
        return

    begin_sync()
    layout = _layout_for(browser)
    _reconcile_pane_caches(browser, layout)
    sub_info, prev_info = _pane_info_flags(layout)
    info_separate = _info_bar_is_separate(layout)
    list_rect = layout['list']
    children_rect = layout['children']
    preview_rect = layout['preview']
    info_bar = layout['info_bar']
    pane_repainted = False
    if 'list' in needs:
        render_list(browser, list_rect,
                    rightmost=layout.get('list_rightmost', False))
        pane_repainted = True
    if 'children' in needs:
        if children_rect is not None:
            if getattr(browser, 'split', 'h') == 'v':
                render_children_list(
                    browser, children_rect,
                    info=sub_info,
                    has_header=not info_separate,
                    rightmost=layout.get('children_rightmost', False),
                )
            else:
                render_children_grid(
                    browser, children_rect,
                    info=sub_info,
                    has_header=not info_separate,
                    rightmost=layout.get('children_rightmost', False),
                )
        pane_repainted = True
    if 'preview' in needs:
        if browser.show_preview and preview_rect is not None:
            render_preview(
                browser, preview_rect,
                info=prev_info,
                has_header=not info_separate,
                rightmost=layout.get('preview_rightmost', False),
            )
        pane_repainted = True
    if 'info' in needs:
        ir = info_bar.top if info_bar is not None else 0
        if ir > 0:
            if sub_info:
                render_info_bar(
                    ir, layout['cols'], 'Children',
                    info=True, browser=browser,
                    rightmost=layout.get('info_bar_rightmost', False),
                    manage_cache=True,
                )
            else:
                render_info_bar(
                    ir, layout['cols'], _preview_label(browser),
                    info=True, browser=browser,
                    rightmost=layout.get('info_bar_rightmost', False),
                    manage_cache=True,
                )
    # Repaint pane separators whenever any pane was repainted. The
    # children pane in Alt-1 (#176) appears/disappears as the cursor
    # crosses leaf/branch boundaries, which moves sep_inner in and out
    # of the layout; without redrawing here, the separator is missing
    # on the row where the previous render didn't have one (#180).
    # Cheap: a single column of ``│`` (or row of ``─``) per separator.
    if pane_repainted:
        sep_main = layout.get('sep_main')
        sep_inner = layout.get('sep_inner')
        if sep_main is not None:
            render_separator(
                sep_main,
                rightmost=layout.get('sep_main_rightmost', False),
                cache_key='sep_main', browser=browser,
            )
        if sep_inner is not None:
            render_separator(
                sep_inner,
                rightmost=layout.get('sep_inner_rightmost', False),
                cache_key='sep_inner', browser=browser,
            )
    end_sync()
    flush()
    browser._needs_redraw = set()


def _preview_label(browser):
    """Pick the preview pane label based on browser state.

    ``⧗ Preview`` while a fetch for the cursored item is outstanding
    (preview-flicker design §C) — the glyph leads because the label is
    right-aligned in the divider. Help mode never shows it.
    """
    if browser._help_mode:
        return 'Help'
    if browser._preview_loading():
        return '⧗ Preview'
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
