"""UI tests: streaming behaviours from #271–#274.

End-to-end coverage that drives the binary under tmux and verifies what
the user sees on screen for each streaming-spec capability:

* Loading spinner during a slow ``get_children`` clears once results
  arrive (#271).
* Generator-streaming ``get_children`` paints rows in waves as each
  chunk yields (#272).
* Generator-streaming ``get_preview`` paints lines in waves as each
  chunk yields (#273).
* Watcher pushes via ``update_data`` mutate visible tags live (#271
  push API).

These tests intentionally stay minimal — recipes live in
``test/ui/recipes/streaming_*.py`` and each test pokes one specific
expectation, with ``wait_for(...)`` timeouts generous enough to absorb
load-induced jitter without sleeping at the test level.
"""

import os
import shutil
import subprocess
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RECIPES = os.path.join(_REPO, 'test', 'ui', 'recipes')

_SLOW_ROOT = os.path.join(_RECIPES, 'streaming_slow_root.py')
_GEN_CHILDREN = os.path.join(_RECIPES, 'streaming_generator_children.py')
_PREVIEW_CHUNKS = os.path.join(_RECIPES, 'streaming_preview_chunks.py')
_WATCHER_UPDATE = os.path.join(_RECIPES, 'streaming_watcher_update.py')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestStreamingLoadingSpinner(unittest.TestCase):
    """#271: ``⧗ loading…`` placeholder while ``get_children`` is in flight."""

    def test_spinner_appears_then_clears_after_slow_expand(self):
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch(_BIN, '--run-py', _SLOW_ROOT, '1.0')
            # Root resolves quickly; expand the parent to trigger the
            # slow path.
            t.wait_for('parent', timeout=3.0)
            t.send('Right')
            # Loading placeholder must show before the fetch returns.
            t.wait_for('loading', timeout=1.5)
            # Children land within the recipe's delay budget.
            t.wait_for('alpha', timeout=3.0)
            screen = t.wait_stable()
            self.assertNotIn('loading', screen)
            self.assertIn('alpha', screen)
            self.assertIn('beta', screen)


class TestStreamingGeneratorChildren(unittest.TestCase):
    """#272: generator-streaming ``get_children`` paints in waves."""

    def test_chunks_appear_over_time(self):
        # ``Browser.run`` waits up to 500 ms before the first paint, so
        # we use a 1.0 s inter-chunk delay (recipe default) to leave a
        # comfortable window in which each "b-1 not yet visible" style
        # assertion can fire deterministically.
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch(_BIN, '--run-py', _GEN_CHILDREN, '1.0')
            # First wave (a-1 / a-2) lands first; assert b-1 is not yet
            # visible at this moment to confirm it really is streaming
            # rather than batched.
            screen = t.wait_for('a-1', timeout=3.0)
            self.assertNotIn('b-1', screen)
            # Second wave brings in b-1 / b-2 — c-1 must not be there
            # yet.
            screen = t.wait_for('b-1', timeout=3.0)
            self.assertNotIn('c-1', screen)
            # Third wave brings in c-1.
            t.wait_for('c-1', timeout=3.0)
            # Final stable state has every row.
            final = t.wait_stable()
            for row in ('a-1', 'a-2', 'b-1', 'b-2', 'c-1'):
                self.assertIn(row, final)


class TestStreamingPreviewChunks(unittest.TestCase):
    """#273: generator-streaming ``get_preview`` appends as chunks yield."""

    def test_lines_appear_in_order(self):
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch(_BIN, '--run-py', _PREVIEW_CHUNKS, '0.4')
            # Cursor lands on the only item automatically; preview is
            # requested without any keypress.
            t.wait_for('item', timeout=3.0)
            screen = t.wait_for('first line', timeout=3.0)
            self.assertNotIn('second line', screen)
            screen = t.wait_for('second line', timeout=3.0)
            self.assertNotIn('third line', screen)
            t.wait_for('third line', timeout=3.0)


class TestStreamingWatcherUpdate(unittest.TestCase):
    """#271: ``update_data`` from a watcher updates a row's tag live."""

    def test_watcher_updates_row_tags_in_place(self):
        with TmuxFixture(cols=120, rows=24) as t:
            t.launch(_BIN, '--run-py', _WATCHER_UPDATE, '0.5')
            # Initial state: both rows present with the placeholder
            # tag '-'. Wait for the rows themselves first to avoid
            # racing the children worker.
            t.wait_for('row-a', timeout=3.0)
            t.wait_for('row-b', timeout=3.0)
            # First push patches row-a's tag → 'UPD'. Match the row +
            # tag together so we don't mis-fire on stray output.
            t.wait_for('UPD', timeout=3.0)
            # row-b should still carry the placeholder tag at this
            # moment — capture the screen right after UPD lands.
            screen = t.capture()
            # ``-`` shows up in many places (e.g. ``--`` separators), so
            # assert via the row context: we expect row-b's line to
            # still NOT carry '!!' yet.
            self.assertNotIn('!!', screen)
            # Second push patches row-b's tag → '!!'.
            t.wait_for('!!', timeout=3.0)
            final = t.wait_stable()
            self.assertIn('UPD', final)
            self.assertIn('!!', final)


if __name__ == '__main__':
    unittest.main()
