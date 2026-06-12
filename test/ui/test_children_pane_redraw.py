"""UI tests: children-pane stale-cache regression in vertical layout (#201).

After the row-cache + synchronized-output landing (commit 88265a9), in
``v`` (vertical) layout, navigating cursor → no-children item → back to
a with-children item leaves stale preview text in the children-pane
columns. The PaneCache emits nothing because ``cache.rect`` matches and
the cached content matches — but the screen no longer holds the children
content (preview overwrote it while the pane was hidden).

These tests reproduce the regression. They should FAIL on the buggy
build and PASS once the cache is invalidated correctly when a pane
re-emerges after a hidden round-trip.
"""

import os
import shutil
import subprocess
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RECIPE = os.path.join(_REPO, 'test', 'ui', 'recipes',
                       'children_pane_redraw.py')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _body_lines(screen: str):
    """Return the body of the screen (drop the trailing info bar row)."""
    lines = screen.splitlines()
    if not lines:
        return []
    return lines[:-1]


def _vertical_separator_columns(screen: str):
    """Return a sorted set of columns (0-indexed) holding ``│`` in the body.

    A column shared by enough body rows to look like a real separator
    (i.e. a vertical run of ``│`` glyphs) is included.
    """
    body = _body_lines(screen)
    if not body:
        return []
    counts = {}
    for line in body:
        for col, ch in enumerate(line):
            if ch == '│':
                counts[col] = counts.get(col, 0) + 1
    threshold = max(3, len(body) // 4)
    return sorted(c for c, n in counts.items() if n >= threshold)


class TestChildrenPaneRedraw(unittest.TestCase):
    """Regression tests for the children-pane stale-cache bug."""

    def _launch(self, t: TmuxFixture):
        t.launch(_BIN, '--run-py', _RECIPE, '--split-type=v')
        # Wait until the initial render has the three root items.
        t.wait_for('A A')
        t.wait_for('B B')
        t.wait_for('C C')
        # Wait until the children pane for A has populated.
        t.redraw()
        t.wait_for('a1', timeout=3.0)
        t.wait_for('a2', timeout=3.0)
        t.wait_stable(timeout=3.0)

    def test_children_pane_redraws_after_returning_from_no_children(self):
        """Cursor A → B → A: a1/a2 must reappear; no stale BBBBPREVIEW."""
        with TmuxFixture(cols=240, rows=40) as t:
            self._launch(t)

            # Initial state: cursor on A, children visible.
            screen0 = t.capture()
            self.assertIn('a1', screen0,
                          f'a1 missing from initial screen:\n{screen0}')
            self.assertIn('a2', screen0,
                          f'a2 missing from initial screen:\n{screen0}')
            sep_cols0 = _vertical_separator_columns(screen0)
            self.assertTrue(
                sep_cols0,
                f'no vertical separator columns at startup:\n{screen0}')

            # Move cursor to B (no children) → preview expands. The
            # preview repaint is debounced (preview_debounce) and the
            # pane holds A's content until B's arrives — wait for the
            # replacement content itself, not just a stable screen.
            t.send('Down')
            t.wait_for('BBBBPREVIEW', timeout=3.0)

            # Move cursor back to A → children pane should redraw. The
            # held BBBBPREVIEW stays painted until A's preview repaints
            # after the debounce — wait for it before asserting.
            t.send('Up')
            t.wait_for('preview-of-A', timeout=3.0)
            t.wait_stable(timeout=3.0)
            screen_a = t.capture()

            # a1/a2 must be back in the children pane.
            self.assertIn(
                'a1', screen_a,
                f'a1 missing after returning to A (children pane stale):\n'
                f'{screen_a}')
            self.assertIn(
                'a2', screen_a,
                f'a2 missing after returning to A (children pane stale):\n'
                f'{screen_a}')

            # No leftover BBBBPREVIEW from B's preview should remain.
            self.assertNotIn(
                'BBBBPREVIEW', screen_a,
                f'stale preview text from B remains after returning to A:\n'
                f'{screen_a}')

            # Vertical separators must include the original sep columns
            # (the inner sep between children and preview re-appeared at
            # the same column it had before).
            sep_cols_a = _vertical_separator_columns(screen_a)
            for col in sep_cols0:
                self.assertIn(
                    col, sep_cols_a,
                    f'separator column {col} missing after A→B→A '
                    f'(was at {sep_cols0}, now {sep_cols_a}):\n{screen_a}')

    def test_children_pane_redraws_after_left_to_parent(self):
        """Cursor A → Right → Down (a1) → Left: a1/a2 must reappear in pane.

        Regression for ticket #183 — `_nav_left`'s parent-jump branch
        (when cursor is on a leaf and Left jumps back to its parent) only
        flagged 'list' and 'preview' for redraw, not 'children'. After
        the jump, the children pane retained whatever it was showing
        while the cursor sat on a1 (often empty or stale), instead of
        re-rendering A's children list.
        """
        with TmuxFixture(cols=240, rows=40) as t:
            self._launch(t)

            # The launch left A collapsed (the children pane shows A's
            # children as a side-preview without expanding A in the
            # list). Expand A explicitly so a1/a2 become rows in the
            # list, then Down onto a1.
            t.send('Right')
            t.wait_stable(timeout=3.0)
            # After Right, the list shows A / a1 / a2 / B / C; cursor is
            # still on A. One Down moves it onto a1 (a leaf).
            t.send('Down')
            t.wait_stable(timeout=3.0)
            screen_a1 = t.capture()
            self.assertIn(
                'a1', screen_a1,
                f'a1 missing entirely after expanding A and Down:\n'
                f'{screen_a1}')

            # Press Left — cursor should jump back to A. Children pane
            # MUST now show A's children (a1, a2) again.
            t.send('Left')
            t.wait_stable(timeout=3.0)
            screen_back = t.capture()

            # The children-pane column lives between the first and
            # second vertical-separator columns. Pull that slice out of
            # each body row and join — that's the children-pane content.
            body = _body_lines(screen_back)
            sep_cols = _vertical_separator_columns(screen_back)
            self.assertGreaterEqual(
                len(sep_cols), 2,
                f'expected two separators (list│children│preview) in v '
                f'layout after Left-to-parent:\n{screen_back}')
            cp_lo, cp_hi = sep_cols[0] + 1, sep_cols[1]
            children_pane = '\n'.join(line[cp_lo:cp_hi] for line in body)

            # With the fix: children pane shows A's children (a1, a2).
            # Without the fix: children pane retains a1's preview text
            # (e.g. "preview:a1") because no redraw was scheduled.
            self.assertIn(
                'a1', children_pane,
                f'a1 missing from children pane after Left-to-parent — '
                f'children pane was not redrawn.\n'
                f'children-pane slice ({cp_lo}..{cp_hi}):\n{children_pane}\n'
                f'full screen:\n{screen_back}')
            self.assertIn(
                'a2', children_pane,
                f'a2 missing from children pane after Left-to-parent — '
                f'children pane was not redrawn.\n'
                f'children-pane slice ({cp_lo}..{cp_hi}):\n{children_pane}\n'
                f'full screen:\n{screen_back}')
            self.assertNotIn(
                'preview:a1', children_pane,
                f'stale "preview:a1" still in children pane after '
                f'Left-to-parent — pane was not redrawn.\n'
                f'children-pane slice ({cp_lo}..{cp_hi}):\n{children_pane}\n'
                f'full screen:\n{screen_back}')

    def test_sep_main_redraws_after_layout_round_trip(self):
        """v → h → v: sep_main column must contain a vertical run again.

        Regression for ticket #216 — `_mark_disappeared_panes` only
        invalidated `children` and `sep_inner`, but `sep_main` is also
        absent in layout 'h'. After v→h the list pane expands full-width
        and overwrites the sep_main column. On the second v paint, the
        cache rect matches and the cached `│` matches → no diff emitted,
        leaving blanks where sep_main belongs.
        """
        with TmuxFixture(cols=240, rows=40) as t:
            self._launch(t)

            # Initial vertical layout: capture sep columns.
            screen_v0 = t.capture()
            sep_cols0 = _vertical_separator_columns(screen_v0)
            self.assertTrue(
                sep_cols0,
                f'no vertical separator columns at startup:\n{screen_v0}')

            # Switch to horizontal — Alt-2.
            t.send('M-2')
            t.wait_stable(timeout=3.0)
            screen_h = t.capture()
            self.assertFalse(
                _vertical_separator_columns(screen_h),
                f'expected no vertical separators in horizontal layout '
                f'after Alt-2:\n{screen_h}')

            # Switch back to vertical — Alt-1.
            t.send('M-1')
            t.wait_stable(timeout=3.0)
            screen_v1 = t.capture()

            # Vertical separator runs must be present again at the
            # original sep columns. Without the fix, sep_main is blank
            # because its cache hit prevented re-emission.
            sep_cols1 = _vertical_separator_columns(screen_v1)
            for col in sep_cols0:
                self.assertIn(
                    col, sep_cols1,
                    f'separator column {col} missing after v→h→v '
                    f'(was {sep_cols0}, now {sep_cols1}):\n{screen_v1}')

    def test_info_bar_redraws_after_v_h_v_round_trip(self):
        """v → h → v: the standalone bottom info bar must contain ``Preview``.

        Regression for ticket #221 (closed by #228). In layout 'h' the
        info bar is folded into the preview pane's first row (drawn by
        ``render_preview``); in v/m/pc it's a standalone row at the
        bottom of the screen (drawn by ``render_info_bar`` with its own
        cache). The old ``_mark_disappeared_panes`` predicate looked
        only at ``layout.get('info_bar') is None`` which is never true
        — the layout key is non-None in both cases, but the standalone
        ``info_bar`` cache is only USED in the v/m/pc case. After v→h
        the bottom row is overwritten by the list pane's expansion;
        after h→v the cache hit on the (still-cached) "Preview" label
        emits nothing, leaving blank cells.

        ``_reconcile_pane_caches`` (#228) handles this conditionally:
        when ``_info_bar_is_separate(layout)`` is False (h layout), the
        standalone ``info_bar`` cache is reconciled with ``rect=None``,
        stamping the sentinel so the next reappear goes through the
        full-pad path.
        """
        with TmuxFixture(cols=240, rows=40) as t:
            self._launch(t)

            # Initial vertical layout: bottom row should contain "Preview"
            # (the standalone info bar's label).
            screen_v0 = t.capture()
            v0_lines = screen_v0.splitlines()
            self.assertTrue(
                v0_lines, f'empty initial screen capture:\n{screen_v0}')
            self.assertIn(
                'Preview', v0_lines[-1],
                f'expected "Preview" in bottom row at startup; got:\n{screen_v0}')

            # Switch to horizontal — Alt-2.
            t.send('M-2')
            t.wait_stable(timeout=3.0)
            t.capture()  # discard intermediate state

            # Switch back to vertical — Alt-1.
            t.send('M-1')
            t.wait_stable(timeout=3.0)
            screen_v1 = t.capture()
            v1_lines = screen_v1.splitlines()
            self.assertTrue(
                v1_lines, f'empty post-bounce screen capture:\n{screen_v1}')
            # The bottom row must again contain "Preview" — this is the
            # standalone bottom info bar in v layout. Without the fix in
            # #228, the info_bar cache hit on its still-cached label
            # emits nothing; the cells were overwritten while the bar
            # was folded into the preview pane's header in h layout.
            self.assertIn(
                'Preview', v1_lines[-1],
                f'expected "Preview" in bottom row after v→h→v; '
                f'final capture:\n{screen_v1}')

    def test_children_pane_survives_repeated_bounce(self):
        """Bouncing A → B → A → B → A several times still ends clean."""
        with TmuxFixture(cols=240, rows=40) as t:
            self._launch(t)

            sep_cols0 = _vertical_separator_columns(t.capture())

            for _ in range(3):
                t.send('Down')
                t.wait_stable(timeout=3.0)
                t.send('Up')
                t.wait_stable(timeout=3.0)

            screen = t.capture()
            self.assertIn(
                'a1', screen,
                f'a1 missing after repeated A↔B bounce:\n{screen}')
            self.assertIn(
                'a2', screen,
                f'a2 missing after repeated A↔B bounce:\n{screen}')
            self.assertNotIn(
                'BBBBPREVIEW', screen,
                f'stale preview text from B after repeated bounce:\n'
                f'{screen}')
            sep_cols = _vertical_separator_columns(screen)
            for col in sep_cols0:
                self.assertIn(
                    col, sep_cols,
                    f'separator column {col} missing after repeated '
                    f'bounce (was {sep_cols0}, now {sep_cols}):\n{screen}')


if __name__ == '__main__':
    unittest.main()
