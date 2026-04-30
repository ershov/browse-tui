"""UI tests for the ``recipes/browse-plan`` plan-tui port.

Drives the recipe under tmux against a temporary ``.PLAN.md`` file
with a known ticket structure. Like the browse-fs tests, we invoke
``./browse-tui --python recipes/browse-plan`` directly so the tests
don't depend on having ``browse-tui`` on PATH.

Skipped when ``tmux`` or the ``plan`` binary aren't available.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BIN = os.path.join(_REPO, 'browse-tui')
_RECIPE = os.path.join(_REPO, 'recipes', 'browse-plan')

_PLAN_BIN = os.environ.get('PLAN_BIN') or shutil.which('plan') \
    or '/home/ubuntu/sandvault/bin/plan'


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_PLAN_BIN):
        raise unittest.SkipTest(f'plan binary not found at {_PLAN_BIN}')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _make_plan(tmp, titles):
    """Create a ``.PLAN.md`` under ``tmp`` with one ticket per title."""
    plan_path = os.path.join(tmp, '.PLAN.md')
    env = {**os.environ, 'PLAN_MD': plan_path}
    for title in titles:
        subprocess.run(
            [_PLAN_BIN, 'create', f'title="{title}"'],
            env=env, check=True, capture_output=True,
        )
    return plan_path


class TestBrowsePlan(unittest.TestCase):

    def test_lists_tickets(self):
        """The recipe enumerates root tickets and the synthetic Project entry."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_plan(tmp, ['First task', 'Second task', 'Third task'])
            with TmuxFixture(cols=120, rows=30) as t:
                t.launch(
                    'env', f'PLAN_MD={tmp}/.PLAN.md',
                    _BIN, '--python', _RECIPE,
                )
                t.wait_for('Project', timeout=5.0)
                t.wait_for('First task', timeout=5.0)
                t.wait_for('Second task', timeout=5.0)
                t.wait_for('Third task', timeout=5.0)
                t.send('q')

    def test_status_picker_changes_status(self):
        """Pressing 's' opens the picker; choosing 'done' updates the tag."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_plan(tmp, ['Solo'])
            with TmuxFixture(cols=120, rows=30) as t:
                t.launch(
                    'env', f'PLAN_MD={tmp}/.PLAN.md',
                    _BIN, '--python', _RECIPE,
                )
                # Move past the synthetic Project entry onto ticket #1.
                t.wait_for('Solo', timeout=5.0)
                t.send('Down')
                # Open status picker.
                t.send('s')
                t.wait_for('status>', timeout=3.0)
                # Filter to "done" and confirm.
                t.type('done')
                t.send('Enter')
                # The tag column updates to ``[done]`` after refresh.
                t.wait_for('[done]', timeout=5.0)
                t.send('q')

    def test_quit_exits_clean(self):
        """Pressing 'q' terminates the recipe; the wrapper sees exit code 1."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_plan(tmp, ['Just one'])
            with TmuxFixture(cols=120, rows=30) as t:
                # Wrap in bash so we can observe the exit code from the
                # subsequent prompt — the browse-tui binary exits 1 on
                # cancel (the ``q`` keybinding triggers _quit(code=1)).
                t.launch(
                    'bash', '-c',
                    f'env PLAN_MD={tmp}/.PLAN.md '
                    f'{_BIN} --python {_RECIPE}; echo BPEXIT=$?',
                )
                t.wait_for('Just one', timeout=5.0)
                t.send('q')
                t.wait_for('BPEXIT=1', timeout=5.0)


if __name__ == '__main__':
    unittest.main()
