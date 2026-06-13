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
