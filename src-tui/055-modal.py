"""browse-tui: modal dialogs (one blocking overlay window at a time).

A modal dialog is a single overlay window drawn over the regular UI
that blocks all other repaints and receives all input until it closes.
It is composed through the same differential row cache as the panes, so
it paints minimal bytes and restores flicker-free. Three content kinds
plug into one shared loop: a selection list (picker / context menu), a
choice (confirm / alert), and a single-line input (prompt).

This module sits between ``050-render.py`` (whose ``Rect``, cell-width
helpers, and preview wrap pipeline it reuses) and ``060-context.py``
(whose ``ctx`` methods delegate to it). See
``docs/superpowers/specs/2026-06-12-modal-dialogs-design.md`` for the
full design.

This first slice is the pure geometry: the size caps, the frame-size
arithmetic, the tiny-terminal predicate, and the placement math. None
of it touches the terminal, so it is fully unit-testable. The engine
(``run_modal``) and the content classes are added by later tickets.
"""


# Below this usable minimum a centered box makes no sense — the frame
# fills the whole screen instead (see ``_modal_place``). Tuned small so
# only genuinely cramped terminals trip it.
_MODAL_MIN_COLS = 20
_MODAL_MIN_ROWS = 8


def _modal_caps(cols, rows):
    """Maximum content size a dialog may request, given the screen.

    Width is capped at 80% of the screen; height leaves four rows of
    breathing room (so the frame's two border rows never crowd the
    screen edges). The content's ``measure`` is handed these caps and
    must not exceed them. Returns ``(max_w, max_h)`` in content cells.
    """
    return int(0.8 * cols), rows - 4


def _frame_size(content_w, content_h):
    """Frame (outer) size for a content area of ``content_w`` × ``content_h``.

    The frame adds a one-column border plus one column of inner padding
    on each side (``+4`` wide) and a one-row border top and bottom
    (``+2`` tall). Returns ``(w, h)`` — the full on-screen footprint.
    """
    return content_w + 4, content_h + 2


def _modal_is_tiny(cols, rows):
    """Whether the screen is too small to host a centered framed box.

    True when either dimension is below its minimum; the caller then
    falls back to a whole-screen frame.
    """
    return cols < _MODAL_MIN_COLS or rows < _MODAL_MIN_ROWS


def _modal_place(cols, rows, w, h, *, placement, anchor):
    """Place a ``w`` × ``h`` frame on a ``cols`` × ``rows`` screen.

    Returns the frame ``Rect`` (1-based, exclusive right/bottom).

    On a tiny terminal (:func:`_modal_is_tiny`) the frame is the entire
    screen regardless of ``placement`` / ``anchor`` — there's no room
    for centering math on a screen that can't fit a box.

    ``placement='center'`` centers the frame; an odd leftover biases the
    extra cell toward the right/bottom (floor division).

    ``placement='anchor'`` takes ``anchor=(row, col)`` in 1-based screen
    coordinates and lands the frame's top-left just below the anchor, at
    ``(row + 1, col)``, so the anchor row stays visible. If the frame
    would overflow the bottom edge it flips above the anchor; if it would
    overflow the right edge it shifts left. Both axes are then clamped so
    the frame stays on-screen (top/left ≥ 1, fully visible where it fits).
    """
    if _modal_is_tiny(cols, rows):
        return Rect(1, 1, cols + 1, rows + 1)

    if placement == 'anchor':
        row, col = anchor
        left = col
        top = row + 1
        # Flip above the anchor if dropping below would overflow the
        # bottom edge — the frame's last row is ``top + h - 1``.
        if top + h - 1 > rows:
            top = row - h
        # Shift left so the right edge fits — last column is ``left + w - 1``.
        if left + w - 1 > cols:
            left = cols - w + 1
    else:  # 'center'
        left = 1 + (cols - w) // 2
        top = 1 + (rows - h) // 2

    # Clamp onto the screen: keep the frame fully visible where it fits
    # (right/bottom not past the edge), then guarantee top/left ≥ 1. The
    # second step wins when the frame is larger than the screen.
    left = max(1, min(left, cols - w + 1))
    top = max(1, min(top, rows - h + 1))

    return Rect(left, top, left + w, top + h)


# ---------------------------------------------------------------------------
# Engine — the shared modal lifecycle
# ---------------------------------------------------------------------------


# Restore marker planted into pane row caches on close (see ``run_modal``'s
# restore step and the design's "Close and restore: cache poisoning"). It
# contains NUL, which every content sanitizer strips, so no renderer can
# ever legitimately produce these exact bytes — the next ``end_row`` is
# guaranteed to see a cache MISS for the poisoned row and repaint it. The
# row entry's stored visible length is set to the full pane width so that
# miss pads (or ``\e[K``s) out across every cell the dialog overdrew.
_MODAL_POISON = '\x00\x00MODAL\x00\x00'


def _rects_intersect(a, b):
    """Return the overlapping :class:`Rect`, or ``None`` if they're disjoint.

    Both rects use the render layer's inclusive-top, exclusive-right/bottom
    convention. The result is the intersection rect (same convention); it is
    ``None`` when either input is ``None``, when either is not a real
    ``Rect`` (e.g. the disappeared-pane sentinel a hidden pane's cache
    holds), or when the rectangles do not overlap.
    """
    if not isinstance(a, Rect) or not isinstance(b, Rect):
        return None
    left = max(a.left, b.left)
    top = max(a.top, b.top)
    right = min(a.right, b.right)
    bottom = min(a.bottom, b.bottom)
    if right <= left or bottom <= top:
        return None
    return Rect(left, top, right, bottom)


def run_modal(browser, content, *, placement='center', anchor=None,
              delay_interaction=False, _read_key=None):
    """Run one blocking modal dialog to completion; return its result.

    A modal dialog is the single overlay window the framework allows at a
    time. While it runs it owns the screen and all input: the panes do not
    repaint and every key is routed through ``content``. Content channels
    (``ctx.print`` output, streaming stdin) keep flowing so a recipe behind
    the dialog never stalls. On close the regular UI repaints differentially
    with no leftover dialog cells (see the cache-poison restore below).

    ``content`` is a duck-typed object — the **content protocol**:

      * ``title`` — ``str`` or ``None``; embedded bold in the top border.
      * ``measure(max_w, max_h) -> (w, h)`` — desired content area, at most
        ``(max_w, max_h)``. Called at open and after a resize, never between
        keystrokes (so the frame doesn't jiggle while the user types).
      * ``draw_row(row, width)`` — emit the writes for ONE content row
        (``row`` is 0-based within the content area), filling exactly
        ``width`` cells. Called inside an active ``begin_row`` capture; the
        engine writes the borders and inner padding around it.
      * ``handle_key(key) -> (done, result)`` — process one key.
        ``(False, _)`` continues the loop; ``(True, result)`` closes the
        dialog and makes ``run_modal`` return ``result``.

    ``placement`` / ``anchor`` are forwarded to :func:`_modal_place`.
    ``delay_interaction`` is accepted and threaded for API stability but is
    a no-op in this slice (its behavior — draining/ignoring keystrokes the
    user typed at the previous screen — lands in a later ticket; the default
    ``False`` changes nothing). ``_read_key`` is the test-injection seam: a
    callable returning decoded key names, defaulting to the terminal layer's
    ``read_key``. The injected variant is called with NO arguments (matching
    the picker's scripted-key tests); only the real ``read_key`` is handed
    the channel fds — see the read step below.

    Raises ``RuntimeError`` if a modal is already open: one window at a time
    is a hard invariant, so re-entry is a programming error.
    """
    if getattr(browser, '_modal_open', False):
        raise RuntimeError('run_modal: a modal dialog is already open')
    browser._modal_open = True

    # ``delay_interaction`` is threaded but unused this slice; bind it so
    # linters/readers see it's intentional rather than dropped.
    _ = delay_interaction

    # The real terminal ``read_key`` takes the channel fds; the injected
    # test seam is a zero-arg callable. Branch once here so the loop body
    # stays uniform.
    injected = _read_key is not None
    rk = _read_key if injected else read_key

    # Private row cache: the dialog paints through the same differential
    # machinery as the panes, but its cache is NEVER registered in
    # ``browser._pane_cache`` — the regular renderer must not see it.
    cache = PaneCache()

    def _measure_frame():
        """(Re)compute the frame ``Rect`` from the current terminal size."""
        cols, rows = term_size()
        max_w, max_h = _modal_caps(cols, rows)
        content_w, content_h = content.measure(max_w, max_h)
        # Never let a misbehaving content exceed the caps it was handed —
        # an oversized frame would push borders off-screen.
        content_w = max(0, min(content_w, max_w))
        content_h = max(0, min(content_h, max_h))
        fw, fh = _frame_size(content_w, content_h)
        return _modal_place(cols, rows, fw, fh,
                            placement=placement, anchor=anchor), content_h

    def _paint(frame, content_h):
        """Paint the whole frame through the private cache, in one sync.

        Composes every row — top border, content rows wrapped in ``│``
        borders + one column of inner padding, bottom border — to EXACTLY
        the frame width so no stale cell can bleed through ``end_row``'s
        pad math. ``update_rect`` runs exactly once per frame (the
        cache's per-frame invariant), so the first paint emits the full
        frame and later paints re-emit only rows whose bytes changed.

        ``content_h`` is the (capped) row count the content actually owns.
        Only those rows go to ``content.draw_row``; any extra interior
        rows the frame has — when ``_modal_place`` returned a full-screen
        frame on a tiny terminal whose interior outsizes the content — are
        blank-filled here. Without this the loop would ask the content for
        out-of-range rows and a list-backed content would ``IndexError``.
        """
        inner_w = frame.width - 4  # minus left/right border + 1-col pad each
        left = frame.left
        right = frame.right
        rightmost = False  # the frame never owns the screen's right edge
        last = frame.height - 1
        begin_sync()
        cache.update_rect(frame)
        for rel in range(frame.height):
            abs_row = frame.top + rel
            begin_row(cache, rel, abs_row, left, right, rightmost=rightmost)
            if rel == 0:
                _draw_top_border(content.title, frame.width)
            elif rel == last:
                _draw_bottom_border(frame.width)
            else:
                # Interior row: engine draws ``│ `` … ` │``. ``content_row``
                # is the 0-based index within the content area; rows the
                # content doesn't cover are blank-filled to the inner width.
                content_row = rel - 1
                set_style(fg=8)
                write('│')
                reset_style()
                write(' ')
                if content_row < content_h:
                    content.draw_row(content_row, inner_w)  # fills inner_w
                else:
                    write(' ' * inner_w)
                write(' ')
                set_style(fg=8)
                write('│')
                reset_style()
            end_row()
        end_sync()
        flush()

    # Bind ``frame`` before the try so the restore loop in ``finally`` has a
    # name even if the FIRST ``_measure_frame``/``content.measure`` raises.
    # ``_rects_intersect`` returns None for a non-Rect, so the restore loop
    # is then a clean no-op, the real exception propagates (not masked by an
    # UnboundLocalError), and ``_modal_open`` is still cleared.
    frame = None
    result = None
    try:
        frame, content_h = _measure_frame()
        _paint(frame, content_h)

        while True:
            if injected:
                key = rk()
            else:
                # fd 1 joins the write-set only while a live pipe/file
                # stdout has bytes to drain; fd 0 joins the read-set only
                # while the streaming-input hook is armed — the SAME
                # conditions as the main loop, so a dialog never stalls a
                # streaming recipe and adds nothing to the select set on
                # an idle channel.
                wfd = (1 if (browser._out_stream_live
                             and not browser._out_dead and browser._out_buf)
                       else None)
                rfd = 0 if browser._stdin_live else None
                key = rk(write_fd=wfd, aux_read_fd=rfd)

            # Signal flags, checked after every read exactly as the main
            # loop does. Both route to the resize/screen-lost repaint.
            resized = False
            if globals().get('g_resize_flag', False):
                globals()['g_resize_flag'] = False
                resized = True
            if globals().get('g_screen_lost_flag', False):
                globals()['g_screen_lost_flag'] = False
                resized = True

            if resized:
                # Simplest correct behavior (design §"Resize / screen-lost
                # while open"): blank the whole screen, drop the pane
                # caches so the close-time repaint's first-paint branch is
                # valid, recompute geometry against the new size, and
                # repaint the dialog. The UI underneath stays blank until
                # the dialog closes — resizes are rare.
                write('\033[2J')
                browser._pane_cache.clear()
                cache = PaneCache()
                frame, content_h = _measure_frame()
                _paint(frame, content_h)
                continue

            # Channel + background events, handled before content sees the
            # key — mirrors the main loop's ordering.
            if key == '_writable':
                browser._drain_output()
                continue
            if key == '_stdin':
                browser._pump_stdin()
                continue
            if key == '_notify':
                # Drain background work WITHOUT rendering: the modal owns
                # the screen, so pane redraw flags the drain sets just
                # accumulate in ``browser._needs_redraw`` and the close-
                # time ``'all'`` absorbs them.
                browser.drain_main_queue()
                browser.apply_children_results()
                continue

            # Mouse events are swallowed (no in-dialog mouse this round).
            if (key.startswith('mouse-click:')
                    or key.startswith('scroll-up:')
                    or key.startswith('scroll-down:')):
                continue

            # Uniform cancel: esc / ctrl-c close the dialog with None.
            if key == 'esc' or key == 'ctrl-c':
                result = None
                break

            done, result = content.handle_key(key)
            if done:
                break
    finally:
        # Restore (always runs, even if content raised): poison every pane
        # cache row the dialog overdrew so the next ``render_full`` repaints
        # it differentially — poisoned rows miss the cache and, with the
        # planted full-width visible length, pad/``\e[K`` out across every
        # cell the dialog touched; untouched rows cache-hit and emit
        # nothing. After a resize-while-open ``browser._pane_cache`` was
        # cleared, so this loop is a harmless no-op over empty caches.
        for pane in browser._pane_cache.values():
            overlap = _rects_intersect(pane.rect, frame)
            if overlap is None:
                continue
            pane_width = pane.rect.width
            for abs_row in range(overlap.top, overlap.bottom):
                rel = abs_row - pane.rect.top
                if 0 <= rel < len(pane.lines):
                    pane.lines[rel] = (pane_width, _MODAL_POISON)
        browser._needs_redraw.add('all')
        browser._modal_open = False

    return result


def _draw_top_border(title, width):
    """Emit the top border row ``┌─ {title} ─…─┐`` to exactly ``width`` cells.

    With no title it's a solid ``┌──…──┐`` run. The box-drawing chars use
    the dim/gray separator chrome (palette index 8, matching
    ``render_separator``); the title renders bold in the default fg so it
    reads clearly against the dim frame. Must fill the full frame width so
    ``end_row``'s pad math leaves no stale cells.
    """
    set_style(fg=8)
    write('┌')
    if title:
        # ``┌─ title ─…─┐``: leading ``─ ``, the bold title, a trailing
        # `` `` then dashes filling the remainder before the corner.
        # Account for the four corner/space cells (``┌``, ``─``, two
        # spaces) plus the closing ``┐`` and its leading ``─``.
        write('─ ')
        reset_style()
        set_style(bold=True)
        # Clip an over-long title to whatever space the frame leaves between
        # the fixed ``┌─ `` prefix (3 cells) and the `` ─┐`` suffix (minimum
        # 3 cells: a space, one dash, the corner). ``_truncate_by_cells``
        # clips by *cells* (wide-char aware) and is a no-op when it already
        # fits — a plain ``title[:avail]`` slice would emit up to 2×avail
        # cells for CJK/fullwidth titles and overflow the frame, which
        # ``end_row`` never truncates.
        avail = max(0, width - 6)
        shown, shown_cells = _truncate_by_cells(title, avail)
        write(shown)
        reset_style()
        set_style(fg=8)
        write(' ')
        # Fill the gap to the closing corner with dashes (using the exact
        # cell count, which may differ from ``len(shown)`` for wide glyphs).
        used = 3 + shown_cells + 1  # ``┌─ `` + title + trailing space
        dashes = max(0, width - used - 1)   # -1 for the closing ``┐``
        write('─' * dashes)
        write('┐')
    else:
        write('─' * max(0, width - 2))
        write('┐')
    reset_style()


def _draw_bottom_border(width):
    """Emit the bottom border row ``└──…──┘`` to exactly ``width`` cells."""
    set_style(fg=8)
    write('└')
    write('─' * max(0, width - 2))
    write('┘')
    reset_style()
