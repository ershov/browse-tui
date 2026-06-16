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

    Width is capped at 80% of the screen, minus the two outer margin
    columns the frame reserves (one blank cell just outside each vertical
    border — see :func:`_paint`); this keeps the box PLUS its margins
    within 80% and, in the non-tiny regime, guarantees the frame is at
    most ``cols - 2`` wide so both margins land on-screen. Height leaves
    four rows of breathing room (so the frame's two border rows never
    crowd the screen edges). The content's ``measure`` is handed these
    caps and must not exceed them. Returns ``(max_w, max_h)`` in content
    cells.
    """
    return int(0.8 * cols) - 2, rows - 4


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


def _modal_place(cols, rows, w, h, *, placement, anchor, side=None):
    """Place a ``w`` × ``h`` frame on a ``cols`` × ``rows`` screen.

    Returns the frame ``Rect`` (1-based, exclusive right/bottom).

    On a tiny terminal (:func:`_modal_is_tiny`) the frame is the entire
    screen regardless of ``placement`` / ``anchor`` — there's no room
    for centering math on a screen that can't fit a box.

    ``placement='center'`` centers the frame; an odd leftover biases the
    extra cell toward the right/bottom (floor division).

    ``placement='anchor'`` takes ``anchor=(row, col)`` in 1-based screen
    coordinates and uses ONLY the anchor's row for vertical placement.
    Horizontally the frame is CENTERED on screen — the same ``left`` the
    ``'center'`` branch computes — so a menu triggered by keyboard (whose
    anchor column is the list pane's left edge) sits closest to the screen
    center rather than hugging the left.

    The vertical SIDE of the anchor row is chosen by ``side``:

      * ``side=None`` — DECIDE fresh from the frame height (the first menu of
        a chain, or any standalone anchored use): drop just below the anchor
        row (``top = row + 1``) so that row stays visible, flipping above
        (``top = row - h``) only if dropping below would overflow the bottom
        edge.
      * ``side='below'`` — force below (``top = row + 1``) even if the frame
        overflows the bottom; the shared clamp then shifts it up so it fits.
      * ``side='above'`` — force above (``top = row - h``) even if the frame
        overflows the top; the shared clamp then shifts it down so it fits.

    The forced sides exist so a chained context menu (#1041) keeps every
    submenu on the side the FIRST menu picked: an oversized submenu SHIFTS to
    fit (clamped onto the screen, overlapping the subject row if need be)
    rather than independently flipping to the opposite side and reading as a
    disjoint box. ``side`` is ignored for ``placement='center'``.

    Both axes are then clamped so the frame stays on-screen: vertically
    ``top`` lands in ``[1, rows - h + 1]`` (so a frame taller-on-this-side
    than the room available is shifted on-screen rather than truncated);
    horizontally as below.

    The horizontal clamp also reserves one blank column just outside each
    vertical border for the #1043 outer margin: when the frame is narrow
    enough to leave room (``w <= cols - 2``) the box columns are kept
    within ``[2, cols - 1]`` (so column 1 holds the left margin and column
    ``cols`` the right). A frame that already spans the screen width
    (``w > cols - 2``, including the tiny full-screen fallback) has no room
    for margins and falls back to the plain on-screen clamp (left ≥ 1);
    :func:`_paint` then omits the margins that would land off-screen.
    """
    if _modal_is_tiny(cols, rows):
        return Rect(1, 1, cols + 1, rows + 1)

    if placement == 'anchor':
        row, _col = anchor
        # Horizontal: center on screen (closest to screen center), exactly
        # like the ``'center'`` branch — the anchor column is NOT used for
        # ``left`` (a keyboard-triggered menu anchors at the pane's left
        # edge, which would hug the screen left).
        left = 1 + (cols - w) // 2
        # Vertical: pick the side of the anchor row.
        #   * ``side is None`` → decide from this frame's height: below if it
        #     fits, else flip above (the first menu of a chain, or a
        #     standalone anchored use — unchanged behavior).
        #   * ``side == 'below'`` / ``'above'`` → forced (a chained submenu
        #     reusing the chain's side); the shared clamp below shifts an
        #     oversized box onto the screen instead of flipping it.
        # The frame's last row is ``top + h - 1``.
        if side == 'above':
            top = row - h
        elif side == 'below':
            top = row + 1
        else:  # decide fresh
            top = row + 1
            if top + h - 1 > rows:
                top = row - h
    else:  # 'center'
        left = 1 + (cols - w) // 2
        top = 1 + (rows - h) // 2

    # Clamp onto the screen: keep the frame fully visible where it fits
    # (right/bottom not past the edge), then guarantee top/left in range.
    # The second step wins when the frame is larger than the screen.
    #
    # Horizontally, reserve the #1043 outer-margin columns when the frame
    # is narrow enough (``w <= cols - 2``): keep the box within
    # ``[2, cols - w]`` so column 1 / column ``cols`` stay free for the
    # margins. Otherwise the frame spans the screen width and there's no
    # room — fall back to the plain ``[1, cols - w + 1]`` clamp.
    #
    # Vertically, ``top`` is clamped into ``[1, rows - h + 1]``. For a forced
    # SIDE (#1041) this is the SHIFT that keeps an oversized submenu on the
    # chain's side: a ``'below'`` box that would overflow the bottom slides
    # up, an ``'above'`` box that would start off the top slides down — both
    # land fully on-screen (overlapping the subject row if need be) rather
    # than flipping to the opposite side.
    if w <= cols - 2:
        left = max(2, min(left, cols - w))
    else:
        left = max(1, min(left, cols - w + 1))
    top = max(1, min(top, rows - h + 1))

    return Rect(left, top, left + w, top + h)


# ---------------------------------------------------------------------------
# Engine — the shared modal lifecycle
# ---------------------------------------------------------------------------


# Frame border glyphs (DOUBLE-line box-drawing) — centralized so a modal
# reads visually distinct from the single-line pane splitters, and so the
# style is one edit per glyph to change. Used by ``_draw_top_border``,
# ``_draw_bottom_border``, and the side-border writes in ``_paint``.
_BORDER_TL = '╔'   # top-left corner
_BORDER_TR = '╗'   # top-right corner
_BORDER_BL = '╚'   # bottom-left corner
_BORDER_BR = '╝'   # bottom-right corner
_BORDER_H = '═'    # horizontal run
_BORDER_V = '║'    # vertical side


# Restore marker planted into pane row caches on close (see ``run_modal``'s
# restore step and the design's "Close and restore: cache poisoning"). It
# contains NUL, which every content sanitizer strips, so no renderer can
# ever legitimately produce these exact bytes — the next ``end_row`` is
# guaranteed to see a cache MISS for the poisoned row and repaint it. The
# row entry's stored visible length is set to the full pane width so that
# miss pads (or ``\e[K``s) out across every cell the dialog overdrew.
_MODAL_POISON = '\x00\x00MODAL\x00\x00'


# Grace period (seconds, ``time.monotonic`` scale) after a
# ``delay_interaction=True`` dialog's first paint during which normal
# keystrokes are discarded — see ``run_modal`` and the design's "Delayed
# interaction". Stops a dialog the user didn't ask for (a background error,
# an async event) from eating keys they were typing at the previous screen.
# Injectable per-call via ``run_modal(..., _delay_threshold=...)``; tests
# pass 0.0 to disable the gate deterministically.
_MODAL_INTERACTION_DELAY = 0.5


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
              delay_interaction=False, cancel_keys=frozenset(),
              cancel_on_right_click=False, _read_key=None,
              _delay_threshold=None, _now=None, _input_ready=None):
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

    ``delay_interaction`` distinguishes a dialog the user asked for from one
    that appears on its own (a background error, an async event). When
    ``True`` (default ``False`` — unchanged behavior for everything ``ctx``
    exposes today):

      * At open, pending input is drained (``while _input_ready():
        read_key()``, discarded) so keystrokes the user typed at the
        PREVIOUS screen don't instantly dismiss the dialog.
      * Until ``_delay_threshold`` seconds have elapsed since the first
        paint, NORMAL keys are discarded; ``'_notify'`` / ``'_writable'`` /
        ``'_stdin'`` and resize/screen-lost are still serviced (a streaming
        recipe behind the dialog keeps running, the dialog still repaints on
        resize). After the window, keys dispatch as usual.

    ``cancel_keys`` / ``cancel_on_right_click`` extend the uniform esc/ctrl-c
    cancel with extra close gestures, used ONLY by the ``ctx.menu`` path (#1039)
    so pressing a context-menu trigger again toggles an open menu shut. Both
    default to "off", leaving every other modal (``ctx.pick`` / ``ctx.confirm``
    / ``ctx.input`` / ``ctx.alert``) unchanged — there ``\\`` is a valid filter /
    text character and a right-click is just swallowed:

      * ``cancel_keys`` — a frozenset of key names; any one closes the dialog
        with result ``None`` (like esc). Checked alongside esc/ctrl-c, BEFORE
        ``content.handle_key`` — so in a menu ``\\`` closes rather than being
        consumed as type-ahead (#1042).
      * ``cancel_on_right_click`` — when True, a bare ``right-click:R:C`` event
        closes the dialog with ``None`` instead of being swallowed with the
        other mouse events (other mouse events stay swallowed).

    Injection seams (all default to production behavior; tests pass
    deterministic stand-ins so there are no real sleeps):

      * ``_read_key`` — callable returning decoded key names; defaults to the
        terminal layer's ``read_key``. The injected variant is called with NO
        arguments (matching the picker's scripted-key tests); only the real
        ``read_key`` is handed the channel fds (see the read step below).
      * ``_delay_threshold`` — grace-period length in seconds; defaults to
        ``_MODAL_INTERACTION_DELAY``. Tests pass ``0.0`` to disable the gate.
      * ``_now`` — monotonic clock, a zero-arg callable returning seconds;
        defaults to ``time.monotonic``. Tests pass a controllable source.
      * ``_input_ready`` — zero-arg "is a key buffered right now?" poll used
        ONLY by the open-time drain; defaults to the terminal layer's
        ``input_ready``. Tests pass a stand-in that simulates N pending keys
        then empty. (The drain reads discarded keys through the same key
        source the loop uses — ``_read_key`` when injected, else the real
        ``read_key`` — so a deterministic test drives both from one script.)

    Raises ``RuntimeError`` if a modal is already open: one window at a time
    is a hard invariant, so re-entry is a programming error.
    """
    if getattr(browser, '_modal_open', False):
        raise RuntimeError('run_modal: a modal dialog is already open')
    browser._modal_open = True

    # The real terminal ``read_key`` takes the channel fds; the injected
    # test seam is a zero-arg callable. Branch once here so the loop body
    # stays uniform.
    injected = _read_key is not None
    rk = _read_key if injected else read_key

    # Resolve the delay-interaction seams to their production defaults. All
    # three are bare names in the concatenated build (``time`` is imported in
    # 040-state, ``input_ready`` in 020-terminal); the isolated test load
    # wires them or passes explicit stand-ins.
    threshold = _delay_threshold if _delay_threshold is not None \
        else _MODAL_INTERACTION_DELAY
    now = _now if _now is not None else time.monotonic
    poll_ready = _input_ready if _input_ready is not None else input_ready

    # Open-time input drain (design §"Delayed interaction"): eat keystrokes
    # the user typed at the previous screen so an unexpectedly-appearing
    # dialog isn't instantly dismissed. Reads go through the same key source
    # the loop uses (``rk``) so a scripted test drives drain + loop from one
    # stream; ``poll_ready`` decides when to stop. Only for self-appearing
    # dialogs — user-invoked ones (``delay_interaction=False``) skip it.
    if delay_interaction:
        while poll_ready():
            rk()  # discard

    # Private row cache: the dialog paints through the same differential
    # machinery as the panes, but its cache is NEVER registered in
    # ``browser._pane_cache`` — the regular renderer must not see it.
    cache = PaneCache()

    def _measure_frame():
        """(Re)compute the frame ``Rect`` from the current terminal size.

        For an anchored placement this is also where the per-chain menu
        SIDE (#1041) is resolved, because it's the one spot that knows BOTH
        the anchor row and the measured frame height. ``browser`` carries a
        ``_context_menu_side`` slot that ``Browser._fire_context_menu`` resets
        to ``None`` at the start of each context-menu chain and clears when it
        ends:

          * unset (``None``) → DECIDE the side from the anchor row + this
            frame's height (below if it fits, else above) and STORE it back,
            so the first ``ctx.menu`` of a chain fixes the side;
          * already set → REUSE it (a submenu opened later in the same chain
            opens on the same side, shifting to fit rather than flipping).

        Read via ``getattr`` so a Browser without the slot — or any non-chain
        anchored use (no ``on_context_menu`` fire around it) — behaves like
        "no preferred side" and decides fresh, never persisting one. Centered
        placements never touch the slot.
        """
        cols, rows = term_size()
        max_w, max_h = _modal_caps(cols, rows)
        content_w, content_h = content.measure(max_w, max_h)
        # Never let a misbehaving content exceed the caps it was handed —
        # an oversized frame would push borders off-screen.
        content_w = max(0, min(content_w, max_w))
        content_h = max(0, min(content_h, max_h))
        fw, fh = _frame_size(content_w, content_h)
        side = None
        if placement == 'anchor':
            side = getattr(browser, '_context_menu_side', None)
            if side is None:
                # First menu of the chain (or a standalone anchored use):
                # decide below-if-fits-else-above from this frame's height,
                # mirroring the fresh-decision branch in ``_modal_place``.
                row, _col = anchor
                side = 'below' if row + 1 + fh - 1 <= rows else 'above'
                # Persist only when the slot exists (a context-menu chain);
                # a Browser without it stays sideless so a standalone use
                # never leaks a side.
                if hasattr(browser, '_context_menu_side'):
                    browser._context_menu_side = side
        return _modal_place(cols, rows, fw, fh, placement=placement,
                            anchor=anchor, side=side), content_h

    def _paint(frame, content_h):
        """Paint the whole frame through the private cache, in one sync.

        Composes every row — top border, content rows wrapped in ``║``
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

        After the frame rows, one blank-space margin column is overdrawn
        just OUTSIDE each vertical border (#1043: column ``frame.left - 1``
        and column ``frame.right``) for every painted row, so the box never
        visually abuts the content behind it. Each side is painted only
        when its column is on-screen; a frame at the screen edge (the tiny
        full-screen fallback) simply omits the side(s) with no room. These
        columns are direct writes (no row capture) inside the same sync;
        the close-time restore widens its poisoned region to cover them.
        """
        inner_w = frame.width - 4  # minus left/right border + 1-col pad each
        left = frame.left
        right = frame.right
        rightmost = False  # the frame never owns the screen's right edge
        last = frame.height - 1
        cols, _rows = term_size()
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
                # Interior row: engine draws ``║ `` … ` ║``. ``content_row``
                # is the 0-based index within the content area; rows the
                # content doesn't cover are blank-filled to the inner width.
                content_row = rel - 1
                set_style(fg=8)
                write(_BORDER_V)
                reset_style()
                write(' ')
                if content_row < content_h:
                    content.draw_row(content_row, inner_w)  # fills inner_w
                else:
                    write(' ' * inner_w)
                write(' ')
                set_style(fg=8)
                write(_BORDER_V)
                reset_style()
            end_row()
        # Outer margin (#1043): one blank cell just outside each vertical
        # border, on every painted row. Direct writes (the row capture is
        # closed) into the open sync; a plain space over the background
        # opens a clean 1-col gap. Each side is painted only when on-screen
        # — column ``left - 1`` needs ``left >= 2``, column ``right`` needs
        # ``right <= cols`` — so a frame flush against an edge (tiny
        # full-screen fallback) drops that side's margin without overrunning.
        lm = left - 1
        rm = right            # box owns columns ``left..right-1``; ``right`` is just past it
        paint_lm = lm >= 1
        paint_rm = rm <= cols
        if paint_lm or paint_rm:
            for rel in range(frame.height):
                abs_row = frame.top + rel
                if paint_lm:
                    move(abs_row, lm)
                    reset_style()
                    write(' ')
                if paint_rm:
                    move(abs_row, rm)
                    reset_style()
                    write(' ')
        end_sync()
        flush()

    # Bind ``frame`` before the try so the restore loop in ``finally`` has a
    # name even if the FIRST ``_measure_frame``/``content.measure`` raises.
    # ``_rects_intersect`` returns None for a non-Rect, so the restore loop
    # is then a clean no-op, the real exception propagates (not masked by an
    # UnboundLocalError), and ``_modal_open`` is still cleared.
    frame = None
    result = None
    # Set once a resize/screen-lost cleared the pane caches (below). The
    # close-time restore reads it: cache-poisoning can't clear the dialog's
    # own cells when the caches are empty, so a resized close blanks the
    # screen instead (see ``finally``).
    caches_cleared = False
    try:
        frame, content_h = _measure_frame()
        _paint(frame, content_h)
        # Start the interaction-grace clock at the first paint. With
        # ``delay_interaction=False`` the gate below is never consulted, so
        # this is only meaningful for self-appearing dialogs.
        gate_until = now() + threshold if delay_interaction else None

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
                caches_cleared = True
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

            # Interaction-grace gate (design §"Delayed interaction"): for a
            # self-appearing dialog, discard NORMAL keys (everything that
            # reached here — channel/notify/resize already handled and
            # continued above) until the grace window since first paint
            # elapses. This sits below the channel handlers so a streaming
            # recipe keeps running during the window, and above cancel /
            # content so even esc/ctrl-c can't fire early.
            if gate_until is not None and now() < gate_until:
                continue

            # Context-menu close-on-trigger (#1039): a repeated right-click on
            # an open ``ctx.menu`` toggles it shut. Only the menu path passes
            # ``cancel_on_right_click``; for every other modal a right-click
            # falls through to the swallow below. Checked before the swallow so
            # the bare ``right-click:R:C`` closes instead of being eaten; only
            # the menu path opts in, so other modals are unaffected.
            if cancel_on_right_click and key.startswith('right-click:'):
                result = None
                break

            # Mouse events are swallowed (no in-dialog mouse this round). A
            # right-click that reaches here (no ``cancel_on_right_click``) is
            # swallowed too — it never selects or cancels in a non-menu modal.
            if (key.startswith('mouse-click:')
                    or key.startswith('right-click:')
                    or key.startswith('scroll-up:')
                    or key.startswith('scroll-down:')):
                continue

            # Uniform cancel: esc / ctrl-c close the dialog with None. The menu
            # path (#1039) extends this with ``cancel_keys`` (``\`` / F1), so a
            # repeated keyboard trigger toggles an open menu shut. This runs
            # BEFORE ``content.handle_key`` so ``\`` closes rather than being
            # consumed as menu type-ahead (#1042); ``cancel_keys`` is empty for
            # every other modal, where ``\`` stays a valid filter / text char.
            if key == 'esc' or key == 'ctrl-c' or key in cancel_keys:
                result = None
                break

            done, result = content.handle_key(key)
            if done:
                break
            # The key may have changed what the dialog shows (moved the
            # selection, edited the filter, typed into the field). Repaint
            # so the change is visible — without this the dialog is frozen
            # at the first paint. Cheap: the private cache makes it
            # differential, re-emitting only the rows whose bytes changed.
            _paint(frame, content_h)
    finally:
        # Restore (always runs, even if content raised): poison every pane
        # cache row the dialog overdrew so the next ``render_full`` repaints
        # it differentially — poisoned rows miss the cache and, with the
        # planted full-width visible length, pad/``\e[K`` out across every
        # cell the dialog touched; untouched rows cache-hit and emit
        # nothing.
        #
        # The dialog's painted footprint is one column WIDER than the frame
        # on each side: the #1043 outer margin overdraws column
        # ``frame.left - 1`` and column ``frame.right``. Poison against that
        # widened rect, not the bare frame — a pane that abuts a margin
        # column without overlapping the frame proper (e.g. a left pane
        # split exactly at ``frame.left``) would otherwise keep a stale
        # blank there. Poisoning that pane repaints its whole row, clearing
        # the margin cell. ``frame`` may be ``None`` (first measure raised),
        # so only widen a real ``Rect``; the loop's ``_rects_intersect``
        # still no-ops a ``None``.
        #
        # After a resize-while-open the caches were CLEARED, so the poison
        # loop is a no-op over empty caches — and the next ``render_full``
        # rebuilds them fresh, taking ``end_row``'s ``prev_rect is None``
        # first-paint branch, which emits no padding and so would NOT clear
        # the dialog's own cells. The resize handler's ``\e[2J`` blanked the
        # screen but the dialog repainted over it afterwards. Blank the
        # screen again here so that first-paint lands on a genuinely empty
        # screen — exactly the precondition the design's resize path assumes
        # ("with the caches cleared and the screen genuinely blank, the
        # close-time repaint's first-paint branch is valid").
        if caches_cleared:
            begin_sync()
            write('\033[2J')
            end_sync()
            flush()
        else:
            restore_rect = frame
            if isinstance(frame, Rect):
                restore_rect = Rect(max(1, frame.left - 1), frame.top,
                                    frame.right + 1, frame.bottom)
            for pane in browser._pane_cache.values():
                overlap = _rects_intersect(pane.rect, restore_rect)
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
    """Emit the top border row ``╔═ {title} ═…═╗`` to exactly ``width`` cells.

    With no title it's a solid ``╔══…══╗`` run. The box-drawing chars use
    the dim/gray separator chrome (palette index 8, matching
    ``render_separator``); the title renders bold in the default fg so it
    reads clearly against the dim frame. Must fill the full frame width so
    ``end_row``'s pad math leaves no stale cells.
    """
    set_style(fg=8)
    write(_BORDER_TL)
    if title:
        # ``╔═ title ═…═╗``: leading ``═ ``, the bold title, a trailing
        # `` `` then dashes filling the remainder before the corner.
        # Account for the four corner/space cells (``╔``, ``═``, two
        # spaces) plus the closing ``╗`` and its leading ``═``.
        write(_BORDER_H + ' ')
        reset_style()
        set_style(bold=True)
        # Clip an over-long title to whatever space the frame leaves between
        # the fixed ``╔═ `` prefix (3 cells) and the `` ═╗`` suffix (minimum
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
        used = 3 + shown_cells + 1  # ``╔═ `` + title + trailing space
        dashes = max(0, width - used - 1)   # -1 for the closing ``╗``
        write(_BORDER_H * dashes)
        write(_BORDER_TR)
    else:
        write(_BORDER_H * max(0, width - 2))
        write(_BORDER_TR)
    reset_style()


def _draw_bottom_border(width):
    """Emit the bottom border row ``╚══…══╝`` to exactly ``width`` cells."""
    set_style(fg=8)
    write(_BORDER_BL)
    write(_BORDER_H * max(0, width - 2))
    write(_BORDER_BR)
    reset_style()


# ---------------------------------------------------------------------------
# Content — selection list (picker / context menu)
# ---------------------------------------------------------------------------


# Minimum width a selection list requests, so a list of very short options
# (or empty strings) still reads as a box rather than a sliver.
_LIST_MIN_WIDTH = 8


class ListContent:
    """A selection list — backs both ``ctx.pick`` and ``ctx.menu``.

    One content kind covers the fzf-style filtered picker and the context
    menu; the difference is the ``filter`` flag, not the structure:

      * ``filter=True`` (picker) — a ``> {query}`` prompt row and a separator
        sit above the options; typing narrows the visible list. Wired
        centered behind ``ctx.pick``.
      * ``filter=False`` (menu) — no prompt/separator; the options start at
        row 0. Wired anchored behind ``ctx.menu``.

    ``options`` is a sequence whose items are each a display ``str`` OR a
    ``(display, value)`` 2-tuple. Each is normalized to a ``(display, value)``
    pair stored in ``self._options``; a bare string ``s`` becomes ``(s, s)``,
    so its value IS the string verbatim (lists have NO ``&`` hotkey
    convention — unlike :class:`ChoiceContent`, a bare option is never scanned
    for markers). The display half is shown and filtered on (it may carry
    embedded SGR — it renders normally on an unselected row and is stripped to
    plain reverse video on the selected one, exactly the list pane's rule);
    displays need not be unique, which is why display and value are kept as a
    pair rather than a ``display -> value`` dict.

    The chosen option's VALUE is returned by :meth:`handle_key` (the supplied
    value for a tuple — any type; the string itself for a bare option);
    ``None`` on an enter with an empty filtered list is left to the engine's
    cancel path.

    Implements the content protocol consumed by :func:`run_modal`
    (``title`` / ``measure`` / ``draw_row`` / ``handle_key``).
    """

    def __init__(self, options, *, filter=True, title=None):
        self.title = title
        # Normalize each option to a ``(display, value)`` pair: a bare string
        # ``s`` -> ``(s, s)`` (value is the string verbatim), a 2-tuple kept
        # as-is. Keeping pairs (not a dict) lets duplicate displays coexist.
        self._options = [o if isinstance(o, tuple) else (o, o)
                         for o in options]
        self._filter = filter
        # Two extra rows for the prompt + separator only in filter mode.
        self._chrome = 2 if filter else 0
        self.filter_query = ''
        self.cursor = 0          # index into the FILTERED list
        self._scroll = 0         # first visible filtered index (windowing)
        # Filled by ``measure`` (the engine calls it before the first paint);
        # ``draw_row`` needs the option-row count to window the list.
        self._w = 0
        self._h = 0

    # -- geometry -----------------------------------------------------------

    @property
    def _rows_visible(self):
        """Number of option rows on screen (total height minus the chrome)."""
        return max(0, self._h - self._chrome)

    def measure(self, max_w, max_h):
        """Content size: longest option (floor 8) by option-count + chrome.

        Width is the widest option DISPLAY's cell width (so wide glyphs
        measure correctly; the value half never affects layout), floored at
        :data:`_LIST_MIN_WIDTH` and capped at ``max_w``. Height is the option
        count plus the filter chrome (the prompt + separator rows when
        filtering), capped at ``max_h``. Both results are stored — ``draw_row``
        reads the height to window the list — and clamped to the caps as the
        protocol requires.
        """
        widest = max((cell_width(disp) for disp, _ in self._options),
                     default=0)
        self._w = min(max(widest, _LIST_MIN_WIDTH), max_w)
        self._h = min(len(self._options) + self._chrome, max_h)
        return self._w, self._h

    # -- filtering / windowing ---------------------------------------------

    def _filtered(self):
        """Currently visible ``(display, value)`` pairs (substring filter).

        The case-insensitive substring match is on the DISPLAY half only — a
        query that occurs solely in an option's value never makes it match. In
        menu mode (``filter=False``) the query is always empty, so this returns
        every pair. Order is preserved.
        """
        if not self.filter_query:
            return self._options
        q = self.filter_query.lower()
        return [pair for pair in self._options if q in pair[0].lower()]

    def _clamp(self, filtered):
        """Clamp ``cursor`` to ``filtered`` and scroll it back into view.

        Called after any change to the filter (narrows the list) or the
        cursor (moves the selection). Keeps the selected option inside the
        ``[_scroll, _scroll + _rows_visible)`` window so it is always drawn.
        """
        n = len(filtered)
        if n == 0:
            self.cursor = 0
            self._scroll = 0
            return
        if self.cursor >= n:
            self.cursor = n - 1
        if self.cursor < 0:
            self.cursor = 0
        rows = self._rows_visible
        if rows <= 0:
            self._scroll = 0
            return
        # Scroll the window just far enough to contain the cursor: down when
        # it fell below the bottom edge, up when it rose above the top.
        if self.cursor < self._scroll:
            self._scroll = self.cursor
        elif self.cursor >= self._scroll + rows:
            self._scroll = self.cursor - rows + 1
        # Don't scroll past the end (leaves a blank tail when the list
        # shrinks under a filter): keep the window full where possible.
        self._scroll = max(0, min(self._scroll, max(0, n - rows)))

    # -- drawing ------------------------------------------------------------

    def draw_row(self, row, width):
        """Emit ONE content row, filling exactly ``width`` cells.

        With ``filter=True``: row 0 is the ``> {query}`` prompt, row 1 the
        dim separator, rows 2.. the windowed options. With ``filter=False``
        the options start at row 0. An option row beyond the filtered list's
        end (the list is shorter than the area) is blank-filled.

        Selection follows the list pane's rule (``render_list``): the
        selected option renders as PLAIN VISIBLE text in reverse video
        (embedded SGR stripped via ``_collapse_visible`` so the highlight
        reads cleanly), while unselected rows render their embedded ANSI
        normally through ``_write_segments``. Options are single-line
        (cell-trimmed, never wrapped).
        """
        if self._filter:
            if row == 0:
                self._draw_prompt(width)
                return
            if row == 1:
                # Dim separator under the prompt.
                set_style(fg=8)
                write('─' * width)
                reset_style()
                return
        option_row = row - self._chrome
        filtered = self._filtered()
        vis_idx = self._scroll + option_row
        if not (0 <= vis_idx < len(filtered)):
            # No option maps to this row (shorter list / blank tail).
            write(' ' * width)
            return
        # Render the DISPLAY half of the pair; the value never appears.
        display = filtered[vis_idx][0]
        self._draw_option(display, vis_idx == self.cursor, width)

    def _draw_prompt(self, width):
        """Draw the ``> {query}`` filter prompt, trimmed/padded to ``width``."""
        text = '> ' + self.filter_query
        # Keep the tail visible as the query grows past the box (trim the
        # FRONT), then pad out so the row fills the inner width exactly.
        text = cell_trim(text, width, where='start')
        write(cell_ljust(text, width))

    def _draw_option(self, option, selected, width):
        """Draw one option row to exactly ``width`` cells.

        Reuses the list pane's selection machinery: a one-segment list so
        ``_collapse_visible`` / ``_write_segments`` treat the option's text
        the same way they treat a str-content list row.
        """
        segments = [(option, None, False)]
        if selected:
            # Plain visible text in reverse video — strip embedded SGR so the
            # highlight can't fight the option's own colours (sec 4.1).
            line = _collapse_visible(segments)
            line, _ = _truncate_by_cells(line, width)
            _write_highlighted(line, reverse=True, pad_to=width)
        else:
            # Unselected: embedded ANSI renders normally; pad to the full
            # width so a shorter option leaves no stale cells.
            _write_segments(segments, width, pad_to=width)

    # -- keys ---------------------------------------------------------------

    def handle_key(self, key):
        """Process one key; return ``(done, result)`` per the protocol.

        ``enter`` closes with the focused option's VALUE — the supplied value
        for a ``(display, value)`` option, or the string itself for a bare one
        (a no-op on an empty filtered list). Selection moves (``down``/
        ``ctrl-n``, ``up``/``ctrl-p``) WRAP; ``home``/``end`` jump to the ends.
        In filter mode (``filter=True``, the picker) a printable char /
        ``space`` extends the query and ``backspace`` deletes; both re-filter
        and re-clamp the cursor + scroll. In menu mode (``filter=False``) there
        is no query: a single printable char is type-ahead instead — it jumps
        the selection to the NEXT option whose visible display starts with that
        character (case-insensitive), cycling forward from the current
        selection (see :meth:`_typeahead`). Every other key is ignored.
        """
        filtered = self._filtered()

        if key in ('down', 'ctrl-n'):
            if filtered:
                self.cursor = (self.cursor + 1) % len(filtered)
                self._clamp(filtered)
            return (False, None)
        if key in ('up', 'ctrl-p'):
            if filtered:
                self.cursor = (self.cursor - 1) % len(filtered)
                self._clamp(filtered)
            return (False, None)
        if key == 'home':
            self.cursor = 0
            self._clamp(filtered)
            return (False, None)
        if key == 'end':
            if filtered:
                self.cursor = len(filtered) - 1
                self._clamp(filtered)
            return (False, None)
        if key == 'enter':
            if filtered:
                return (True, filtered[self.cursor][1])   # the pair's value
            return (False, None)   # empty filtered list — no-op

        # A printable char means different things by mode. In filter mode it
        # edits the query (with ``space`` / ``backspace``); in menu mode it is
        # type-ahead — jump to the next display starting with that char.
        if self._filter:
            if key == 'backspace':
                if self.filter_query:
                    self.filter_query = self.filter_query[:-1]
                    self._clamp(self._filtered())
                return (False, None)
            if key == 'space':
                self.filter_query += ' '
                self._clamp(self._filtered())
                return (False, None)
            if len(key) == 1 and key.isprintable():
                self.filter_query += key
                self._clamp(self._filtered())
                return (False, None)
        elif len(key) == 1 and key.isprintable():
            self._typeahead(key)
            return (False, None)

        # Unrecognized key — ignored, loop continues.
        return (False, None)

    def _typeahead(self, char):
        """Menu type-ahead: jump the selection to the next match for ``char``.

        Single-letter type-ahead (NOT a multi-character prefix buffer): search
        forward from the option AFTER the current selection, wrapping around,
        for the first option whose visible display starts with ``char``
        (case-insensitive, leading whitespace ignored). The current option is
        considered last, so pressing the same letter repeatedly cycles through
        all matches and wraps. If a match is found, ``cursor`` lands on it (and
        the window scrolls to keep it visible); with no match anywhere the
        selection is left unchanged.

        Matching is on the DISPLAY half's VISIBLE text — embedded SGR is
        stripped first (via the same ``_collapse_visible`` the rows use) so the
        comparison sees what the user sees, not escape bytes.
        """
        target = char.lower()
        # In menu mode the visible list is every option (no filter narrowing).
        options = self._filtered()
        n = len(options)
        for step in range(1, n + 1):
            idx = (self.cursor + step) % n
            display = _collapse_visible([(options[idx][0], None, False)])
            if display.lstrip()[:1].lower() == target:
                self.cursor = idx
                self._clamp(options)
                return


# ---------------------------------------------------------------------------
# Content — choice (message + button row; confirm / alert)
# ---------------------------------------------------------------------------


class _Button:
    """One parsed button: its on-screen display, hotkey, and return value.

    The raw button is either a label ``str`` or a ``(label, value)`` 2-tuple.
    Only the LABEL is parsed for the ``&`` hotkey convention; a tuple's
    ``value`` is kept verbatim (it may be any type and is never scanned for
    ``&``). The constructor resolves everything once so drawing and key
    handling read plain fields:

      * ``display`` — what's shown inside the ``[ … ]`` cell (markers
        resolved: ``&X`` → ``X``, ``&&`` → a literal ``&``).
      * ``value`` — what :meth:`ChoiceContent.handle_key` returns for this
        button. For a tuple it's the supplied ``value`` (any type); for a bare
        string it's the resolved ``display`` (so a plain label returns itself).
      * ``hotkey`` — the lowercased hotkey char (case-insensitive match), or
        ``None`` for a label with no ``&``-marked char (``&&`` doesn't count).
      * ``hot_index`` — the index of the hotkey char within ``display`` (so
        ``draw_row`` can underline exactly that cell), or ``None``.
    """

    def __init__(self, raw):
        if isinstance(raw, tuple):
            label, value = raw
            has_value = True
        else:
            label = raw
            value = None
            has_value = False
        chars = []          # display chars accumulated
        hotkey = None
        hot_index = None
        i = 0
        n = len(label)
        while i < n:
            ch = label[i]
            if ch == '&':
                nxt = label[i + 1] if i + 1 < n else ''
                if nxt == '&':
                    chars.append('&')   # literal ampersand, not a hotkey
                    i += 2
                    continue
                if nxt:
                    # ``&X`` — X is shown and (the FIRST such) is the hotkey.
                    if hotkey is None:
                        hotkey = nxt.lower()
                        hot_index = len(chars)
                    chars.append(nxt)
                    i += 2
                    continue
                # A lone trailing ``&`` — drop it (no display char, no hotkey).
                i += 1
                continue
            chars.append(ch)
            i += 1
        self.display = ''.join(chars)
        # A bare string returns its own resolved display; a tuple returns the
        # supplied value verbatim.
        self.value = value if has_value else self.display
        self.hotkey = hotkey
        self.hot_index = hot_index


def _button_cells(display):
    """Cell width of a button's ``[ {display} ]`` box (``+4`` chrome)."""
    return cell_width(display) + 4


class ChoiceContent:
    """A message + button row — backs both ``ctx.confirm`` and ``ctx.alert``.

    A wrapped message body (ANSI per the design's "Text handling": embedded
    SGR colours render, all other CSI is neutralised), a blank spacer row,
    then one centered row of buttons. The focused button is reverse-video and
    its hotkey char is underlined; the first button is focused initially.

    ``buttons`` is a sequence whose items are each a label ``str`` OR a
    ``(label, value)`` 2-tuple. The label uses the standard ``&`` hotkey
    convention (``'&Yes'`` → ``Yes`` with ``Y`` the hotkey; ``&&`` a literal
    ``&``; no ``&`` → no hotkey); a tuple's ``value`` may be any type and is
    kept verbatim (never scanned for ``&``). An empty ``buttons`` raises
    ``ValueError`` — a programming error, not a user condition. With a single
    button (the alert case) ``space`` also activates it.

    :meth:`handle_key` returns the chosen button's VALUE — the supplied
    ``value`` for a tuple, or the resolved display for a bare string
    (``'&Yes'`` → ``'Yes'``); the engine's cancel path returns ``None``.
    Implements the content protocol consumed by :func:`run_modal` (``title`` /
    ``measure`` / ``draw_row`` / ``handle_key``).
    """

    def __init__(self, message, buttons, *, title=None):
        if not buttons:
            raise ValueError('ChoiceContent: at least one button is required')
        self.title = title
        self._message = message
        self._buttons = [_Button(b) for b in buttons]
        self.focus = 0          # index of the focused button
        # Filled by ``measure`` (the engine calls it before the first paint):
        # the final size, the wrapped body lines (already clipped to fit), and
        # whether the body was clipped (last visible row carries a ``…``).
        self._w = 0
        self._h = 0
        self._body = []                 # wrapped body rows actually drawn
        self._body_lines_shown = 0
        self._clipped = False

    # -- geometry -----------------------------------------------------------

    @property
    def _button_row_cells(self):
        """Cell width of the whole button row (boxes joined by one space)."""
        cells = sum(_button_cells(b.display) for b in self._buttons)
        return cells + max(0, len(self._buttons) - 1)

    def measure(self, max_w, max_h):
        """Content size: widest of (wrapped body, button row), by line count.

        Width is the wider of the longest wrapped body line and the button
        row, capped at ``max_w``. Height is the body line count plus one
        spacer row plus one button row, capped at ``max_h``. When the height
        clamps, the body is CLIPPED to the rows left after the spacer + button
        row and the last visible body row gets a ``…`` marker (drawn by
        :meth:`draw_row`). The final size, the clipped body lines, and the
        clip flag are stored — ``draw_row`` reads them.
        """
        button_cells = self._button_row_cells
        # Wrap the body to ``max_w`` first to find the longest line, then fix
        # the content width as the wider of body / buttons (capped). The body
        # is RE-wrapped to that final width below so its rows match the box.
        probe = self._wrap_body(max_w)
        longest = max((cell_width(line) for line in probe), default=0)
        self._w = min(max(longest, button_cells), max_w)

        body = self._wrap_body(self._w)
        # Rows available for the body = everything except the spacer + button
        # row. Clip vertically when the full body wouldn't fit.
        body_room = max(0, max_h - 2)
        if len(body) > body_room:
            body = body[:body_room]
            self._clipped = True
        else:
            self._clipped = False
        self._body = body
        self._body_lines_shown = len(body)
        # Height is the drawn body + spacer + button row, capped (the body is
        # already clipped to fit, so this only re-clamps a tiny terminal).
        self._h = min(len(body) + 2, max_h)
        return self._w, self._h

    def _wrap_body(self, width):
        """Wrap the message to ``width`` cells with Preview's ANSI treatment.

        Same pipeline as the preview pane: :func:`_sanitize_preview` defangs
        control chars (ESC kept so SGR survives), then each logical line is
        run through :func:`_wrap_preview_line`, which keeps SGR inline, drops
        every other CSI, and emits self-contained visual rows. Returns the
        list of wrapped rows (pre-rendered strings, ready to ``write``).
        """
        if width <= 0:
            return []
        text = _sanitize_preview(self._message, ansi_on=True)
        rows = []
        for line in text.split('\n'):
            line = line.replace('\t', '    ')
            rows.extend(_wrap_preview_line(line, width, ansi_on=True))
        return rows

    # -- drawing ------------------------------------------------------------

    def draw_row(self, row, width):
        """Emit ONE content row, filling exactly ``width`` cells.

        The body lines occupy the top rows; then a blank spacer row; then the
        centered button row. Any leftover row (a tiny-terminal frame whose
        interior outsizes the content) is blank-filled. Every row is padded to
        exactly ``width`` so no stale cell bleeds through ``end_row``.
        """
        if row < self._body_lines_shown:
            self._draw_body_row(row, width)
            return
        # The button row is the LAST content row (index height - 1); the
        # single row between it and the body is the spacer.
        if row == self._h - 1:
            self._draw_button_row(width)
            return
        # Spacer row, or any leftover row beyond the content area.
        write(' ' * width)

    def _draw_body_row(self, row, width):
        """Draw one wrapped body row, padded to ``width`` (clip marker last).

        Unclipped rows go through :func:`_write_segments` (a single
        ANSI-bearing segment) exactly like a preview row, so embedded SGR
        renders and the row pads to ``width``. The last visible row of a
        CLIPPED body instead fills ``width - 1`` cells and ends with a dim
        ``…`` — its content is truncated (ANSI-aware) to leave room for the
        marker.
        """
        line = self._body[row]
        is_last_clipped = self._clipped and row == self._body_lines_shown - 1
        if not is_last_clipped:
            _write_segments([(line, None, False)], width, pad_to=width)
            return
        # Clipped tail: emit the content trimmed to width-1 (ANSI-aware so the
        # truncation never cuts an SGR sequence), pad it out, then a dim ``…``
        # in the final cell. ``_write_segments`` pads to ``width - 1``.
        clipped = _truncate_visible(line, max(0, width - 1))
        _write_segments([(clipped, None, False)], width - 1, pad_to=width - 1)
        set_style(fg=8)         # dim chrome, matching the frame borders
        write('…')
        reset_style()

    def _draw_button_row(self, width):
        """Compose the centered button row to EXACTLY ``width`` cells.

        The boxes (``[ {display} ]`` joined by single spaces) are centered:
        the leftover slack splits into a left pad and a right pad (extra cell
        on the right). The focused box is reverse-video; each box's hotkey
        char is underlined.

        The composed row is emitted through a single budget-bounded pass
        (:meth:`_emit_spans`) that trims to ``width`` visible cells — never
        more. ``end_row`` only PADS short rows, never truncates long ones, so
        if a box (or the whole row) is wider than ``width`` (a narrow terminal,
        or the engine handing a content width below the button row's cell
        count) the row MUST clamp itself; the box then shows clipped, but the
        frame's right border stays in its column.
        """
        # Build the row as ordered ``(text, reverse, underline)`` spans: the
        # left pad, each box (with one-space gaps), then the right pad. Pads
        # are plain spans; ``_emit_spans`` trims and pads to exactly ``width``.
        total = self._button_row_cells
        slack = max(0, width - total)
        left_pad = slack // 2
        right_pad = slack - left_pad
        spans = [(' ' * left_pad, False, False)]
        for i, button in enumerate(self._buttons):
            if i:
                spans.append((' ', False, False))   # one-space gap between boxes
            spans.extend(self._button_spans(button, focused=i == self.focus))
        spans.append((' ' * right_pad, False, False))
        self._emit_spans(spans, width)

    @staticmethod
    def _button_spans(button, *, focused):
        """Styled spans for one ``[ {display} ]`` box.

        Returns ``(text, reverse, underline)`` triples: the box carries
        reverse video when ``focused``, and the hotkey char alone carries the
        underline (both attributes compose on that char). The display is split
        around the hotkey so only its single cell is underlined.
        """
        rev = focused
        disp = button.display
        hi = button.hot_index
        spans = [('[ ', rev, False)]
        if hi is None:
            spans.append((disp, rev, False))
        else:
            if hi:
                spans.append((disp[:hi], rev, False))
            spans.append((disp[hi], rev, True))     # hotkey char, underlined
            if hi + 1 < len(disp):
                spans.append((disp[hi + 1:], rev, False))
        spans.append((' ]', rev, False))
        return spans

    @staticmethod
    def _emit_spans(spans, width):
        """Emit ``(text, reverse, underline)`` spans, clamped to ``width`` cells.

        Mirrors :func:`_write_segments`' contract for the reverse/underline
        styling the button row needs: each span's PLAIN text (no embedded SGR
        — the style rides the ``set_style`` call) is cell-trimmed to the
        remaining budget so the total visible width never exceeds ``width``;
        once the budget is spent, emission stops. A trailing ``reset_style``
        fires iff any styled span was emitted, so no SGR dangles into the
        frame border. Finally the row is padded with plain spaces to exactly
        ``width`` cells.
        """
        pos = 0
        styled_emitted = False
        for text, reverse, underline in spans:
            if pos >= width:
                break
            chunk, n = _truncate_by_cells(text, width - pos)
            if not chunk:
                continue
            if reverse or underline:
                set_style(reverse=reverse, underline=underline)
                write(chunk)
                reset_style()
                styled_emitted = True
            else:
                write(chunk)
            pos += n
        # A styled chunk already reset itself; this extra reset is harmless and
        # guarantees no SGR leaks past the row even if the last styled span was
        # trimmed away at the budget boundary.
        if styled_emitted:
            reset_style()
        if pos < width:
            write(' ' * (width - pos))

    # -- keys ---------------------------------------------------------------

    def handle_key(self, key):
        """Process one key; return ``(done, result)`` per the protocol.

        ``left``/``right``/``tab`` move focus WRAPPING (``tab`` forward like
        ``right``). ``enter`` closes with the focused button's value. A hotkey
        letter (case-insensitive) closes with THAT button's value immediately,
        regardless of focus. With a single button, ``space`` also activates it.
        Every other key is ignored. (A button's value is its resolved display
        for a bare string, or the supplied value for a ``(label, value)``
        tuple — see :class:`_Button`.)
        """
        n = len(self._buttons)
        if key in ('right', 'tab'):
            self.focus = (self.focus + 1) % n
            return (False, None)
        if key == 'left':
            self.focus = (self.focus - 1) % n
            return (False, None)
        if key == 'enter':
            return (True, self._buttons[self.focus].value)
        if key == 'space' and n == 1:
            return (True, self._buttons[0].value)
        # Hotkey: case-insensitive match against the parsed hotkeys. A
        # single-char key whose lowercase matches a button's hotkey activates
        # it. (``&&`` parsed to no hotkey, so a literal ``&`` never matches.)
        if len(key) == 1:
            k = key.lower()
            for button in self._buttons:
                if button.hotkey is not None and button.hotkey == k:
                    return (True, button.value)

        # Unrecognized key — ignored, loop continues.
        return (False, None)


# ---------------------------------------------------------------------------
# Content — single-line text entry (prompt)
# ---------------------------------------------------------------------------


# Minimum width an input dialog requests, so a short prompt + an empty field
# still reads as a box wide enough to type into rather than a sliver.
_INPUT_MIN_WIDTH = 16


class InputContent:
    """A single-line text entry — backs ``ctx.input``.

    The wrapped prompt text (ANSI per the design's "Text handling": embedded
    SGR colours render, all other CSI is neutralised) sits above a one-row
    entry field showing the edit buffer. When the buffer is wider than the
    field, its TAIL is shown (suffix-trimmed by cells) so the end the user is
    typing stays visible; a visible cursor cell (a reverse-video space) sits
    just after the last char.

    Editing is end-only this round, matching the old info-bar prompt: a
    printable char / ``space`` appends, ``backspace`` deletes the last char,
    and there is no in-buffer cursor movement. ``enter`` returns the buffer
    STRING — possibly empty (a valid result); ``None`` comes only from the
    engine's cancel path. ``default`` pre-fills the buffer.

    Implements the content protocol consumed by :func:`run_modal`
    (``title`` / ``measure`` / ``draw_row`` / ``handle_key``).
    """

    def __init__(self, prompt, *, default=''):
        self.title = None
        self._prompt = prompt
        self.buffer = default          # the edit buffer (end-only editing)
        # Filled by ``measure`` (the engine calls it before the first paint):
        # the final size and the wrapped prompt rows ``draw_row`` emits above
        # the entry field.
        self._w = 0
        self._h = 0
        self._prompt_lines = []        # wrapped prompt rows actually drawn

    # -- geometry -----------------------------------------------------------

    def measure(self, max_w, max_h):
        """Content size: wrapped prompt above one entry-field row.

        Width is the widest of (longest wrapped prompt line, the default's
        cell width, a small floor), capped at ``max_w`` — so the field is
        roomy even for a short prompt, and a long ``default`` is visible
        without forcing the box wider than the cap. The prompt is wrapped to
        ``max_w`` first to find the longest line, then RE-wrapped to the final
        width below (the same two-pass approach :class:`ChoiceContent` uses).
        Height is the prompt line count plus one field row, capped at
        ``max_h``. The final size and the re-wrapped prompt rows are stored —
        :meth:`draw_row` reads them.
        """
        probe = self._wrap_prompt(max_w)
        longest = max((cell_width(line) for line in probe), default=0)
        floor = max(longest, cell_width(self.buffer), _INPUT_MIN_WIDTH)
        self._w = min(floor, max_w)

        self._prompt_lines = self._wrap_prompt(self._w)
        # Height = prompt rows + the single field row, capped. When the cap
        # clamps it, ``draw_row`` still puts the field on the last row and the
        # prompt fills the rows above (some prompt rows then clip off-screen).
        self._h = min(len(self._prompt_lines) + 1, max_h)
        return self._w, self._h

    def _wrap_prompt(self, width):
        """Wrap the prompt to ``width`` cells with Preview's ANSI treatment.

        The same pipeline :class:`ChoiceContent` runs on its body:
        :func:`_sanitize_preview` defangs control chars (ESC kept so SGR
        survives), then each logical line goes through
        :func:`_wrap_preview_line`, which keeps SGR inline, drops every other
        CSI, and emits self-contained visual rows. Returns the wrapped rows
        (pre-rendered strings, ready to ``write``).
        """
        if width <= 0:
            return []
        text = _sanitize_preview(self._prompt, ansi_on=True)
        rows = []
        for line in text.split('\n'):
            line = line.replace('\t', '    ')
            rows.extend(_wrap_preview_line(line, width, ansi_on=True))
        return rows

    # -- drawing ------------------------------------------------------------

    def draw_row(self, row, width):
        """Emit ONE content row, filling exactly ``width`` cells.

        The wrapped prompt occupies the top rows; the entry field is the LAST
        content row (index ``height - 1``). Prompt rows go through
        :func:`_write_segments` (one ANSI-bearing segment) exactly like a
        preview row, so embedded SGR renders and the row pads to ``width``.
        """
        if row == self._h - 1:
            self._draw_field(width)
            return
        line = self._prompt_lines[row]
        _write_segments([(line, None, False)], width, pad_to=width)

    def _draw_field(self, width):
        """Compose the entry-field row to EXACTLY ``width`` cells.

        The field reserves its final cell for a visible cursor (a reverse-
        video space), so the buffer occupies the leading ``width - 1`` cells.
        When the buffer overflows that space its TAIL is shown — the suffix
        that fits in ``width - 1`` cells via :func:`_suffix_by_cells` — so the
        end the user is typing stays on screen. The layout is therefore
        ``tail`` (≤ ``width - 1`` cells) + the cursor cell + right pad, summing
        to exactly ``width``.
        """
        if width <= 0:
            return
        # Tail of the buffer that fits before the cursor cell (cell-aware
        # suffix trim — never hand-rolled codepoint slicing). The cursor sits
        # in the final cell; with ``width == 1`` there's no room for any
        # buffer, so the field is just the cursor.
        tail = _suffix_by_cells(self.buffer, width - 1)
        write(tail)
        # Reverse-video cursor cell just after the last buffer char.
        set_style(reverse=True)
        write(' ')
        reset_style()
        # Pad any remaining cells so the row fills ``width`` exactly (a wide
        # char straddling the tail budget can leave the tail one cell short).
        pad = width - cell_width(tail) - 1
        if pad > 0:
            write(' ' * pad)

    # -- keys ---------------------------------------------------------------

    def handle_key(self, key):
        """Process one key; return ``(done, result)`` per the protocol.

        A printable char (``len == 1`` and ``str.isprintable``) or ``space``
        APPENDS to the buffer; ``backspace`` deletes the last char (a no-op on
        an empty buffer). ``enter`` closes with the buffer string — which may
        be empty (a valid result; ``None`` is reserved for the engine's cancel
        path). End-only editing: there is no in-buffer cursor movement this
        round, so every other key is ignored.
        """
        if key == 'enter':
            return (True, self.buffer)
        if key == 'backspace':
            self.buffer = self.buffer[:-1]
            return (False, None)
        if key == 'space':
            self.buffer += ' '
            return (False, None)
        if len(key) == 1 and key.isprintable():
            self.buffer += key
            return (False, None)

        # Unrecognized key — ignored, loop continues.
        return (False, None)


# ---------------------------------------------------------------------------
# Convenience wrappers — the surface ``ctx`` delegates to
# ---------------------------------------------------------------------------
#
# Each builds the right content object + placement and forwards to
# ``run_modal``. The empty-collection short-circuits for the two list-backed
# dialogs live HERE (return ``None`` without opening), so ``ctx`` and any
# other caller get the no-open behavior uniformly. ``delay_interaction`` and
# the ``_read_key`` test seam are threaded straight through.


def modal_pick(browser, label, options, *, delay_interaction=False,
               _read_key=None):
    """Centered, filtered selection list (``ctx.pick``).

    ``options`` becomes a filterable list with ``label`` as the title. Each
    item is a display ``str`` OR a ``(display, value)`` 2-tuple; the filter
    matches the display and the dialog returns the chosen option's VALUE (the
    tuple's value, or the string itself for a bare option). Returns ``None`` on
    cancel. An empty ``options`` returns ``None`` WITHOUT opening a dialog.
    """
    options = list(options)
    if not options:
        return None
    content = ListContent(options, filter=True, title=label)
    return run_modal(browser, content, placement='center',
                     delay_interaction=delay_interaction, _read_key=_read_key)


def modal_menu(browser, items, *, anchor=None, delay_interaction=False,
               _read_key=None):
    """Anchored, unfiltered selection list — a context menu (``ctx.menu``).

    ``items`` is shown without a filter row. Each item is a display ``str`` OR
    a ``(display, value)`` 2-tuple; the dialog returns the chosen item's VALUE
    (the tuple's value, or the string itself for a bare item). ``anchor`` is an
    ``(row, col)`` 1-based screen cell the menu drops below (see
    :func:`_modal_place`); when ``None`` the dialog centers. Returns ``None``
    on cancel. Empty ``items`` returns ``None`` WITHOUT opening a dialog.
    """
    items = list(items)
    if not items:
        return None
    content = ListContent(items, filter=False)
    placement = 'anchor' if anchor is not None else 'center'
    # A repeated context-menu trigger (``\`` / F1 / right-click) while the menu
    # is open toggles it shut (#1039). The trigger set is centralized in
    # 070-actions (``CONTEXT_MENU_TRIGGER_KEYS``), shared with the OPEN path so
    # the gesture is defined once. ONLY ``ctx.menu`` opts in — pick/confirm/
    # input/alert leave these at their off defaults, so ``\`` / right-click keep
    # their normal meaning there.
    return run_modal(browser, content, placement=placement, anchor=anchor,
                     delay_interaction=delay_interaction,
                     cancel_keys=CONTEXT_MENU_TRIGGER_KEYS,
                     cancel_on_right_click=True, _read_key=_read_key)


def modal_confirm(browser, message, buttons=('&Yes', '&No'), *, title=None,
                  delay_interaction=False, _read_key=None):
    """Message + button row — a confirmation / single choice (``ctx.confirm``).

    Each item of ``buttons`` is a label ``str`` OR a ``(label, value)``
    2-tuple. Returns the chosen button's value: the supplied ``value`` for a
    tuple (any type — bool, int, an object, …), or the resolved label for a
    bare string (``'Yes'`` / ``'No'`` / …). Returns ``None`` on cancel. An
    empty ``buttons`` raises ``ValueError`` (a programming error — surfaced by
    :class:`ChoiceContent`).
    """
    content = ChoiceContent(message, buttons, title=title)
    return run_modal(browser, content, placement='center',
                     delay_interaction=delay_interaction, _read_key=_read_key)


def modal_input(browser, prompt, *, default='', delay_interaction=False,
                _read_key=None):
    """Single-line text entry (``ctx.input``).

    ``default`` pre-fills the field. Returns the entered string (possibly
    empty), or ``None`` on cancel.
    """
    content = InputContent(prompt, default=default)
    return run_modal(browser, content, placement='center',
                     delay_interaction=delay_interaction, _read_key=_read_key)


def modal_alert(browser, text, *, title=None, delay_interaction=False,
                _read_key=None):
    """Static notification — a message with a single OK button (``ctx.alert``).

    Always returns ``None``: the activation result (the ``'OK'`` label) is
    discarded since an alert conveys nothing back to the caller.
    """
    content = ChoiceContent(text, ('&OK',), title=title)
    run_modal(browser, content, placement='center',
              delay_interaction=delay_interaction, _read_key=_read_key)
    return None
