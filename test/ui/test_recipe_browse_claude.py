"""UI tests for the ``recipes/browse-claude`` Claude-Code-session browser.

The recipe walks ``$HOME/.claude/projects/<encoded-path>/<session>.jsonl``
files in three levels (project → session → message). To keep the tests
hermetic we point ``HOME`` at a temp directory pre-populated with a
fake project / session / messages tree, then drive the recipe under
tmux and assert that each level renders.

The shebang ``#!/usr/bin/env -S browse-tui --python`` requires the
binary to be on PATH, which is fragile in tests; instead we invoke
``./browse-tui --python recipes/browse-claude`` directly so the tests
are independent of the user's PATH.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BIN = os.path.join(_REPO, 'browse-tui')
_RECIPE = os.path.join(_REPO, 'recipes', 'browse-claude')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _make_fake_claude(tmpdir):
    """Create a fake ``$HOME/.claude/projects`` layout for tests.

    Returns the path to the projects root. Layout::

        tmp/.claude/projects/-home-test-project/abcd1234-deadbeef.jsonl

    The .jsonl contains three records mirroring real Claude-Code shapes:
    a ``last-prompt`` bookmark, a ``permission-mode`` event, and a user
    turn. The names embed predictable substrings (``home/test/project``,
    ``abcd1234-deadbeef``, ``last-prompt``) so the tests can wait on
    them with cheap string matching.
    """
    root = os.path.join(tmpdir, '.claude', 'projects')
    proj = os.path.join(root, '-home-test-project')
    os.makedirs(proj)
    sess = os.path.join(proj, 'abcd1234-deadbeef.jsonl')
    with open(sess, 'w') as f:
        f.write(json.dumps({
            'type': 'last-prompt',
            'leafUuid': '12345678-aaaa-bbbb-cccc-dddddddddddd',
            'sessionId': 'abcd1234',
        }) + '\n')
        f.write(json.dumps({
            'type': 'permission-mode',
            'permissionMode': 'plan',
            'sessionId': 'abcd1234',
        }) + '\n')
        f.write(json.dumps({
            'type': 'user',
            'message': {'role': 'user', 'content': 'hello world'},
        }) + '\n')
    return root


class TestBrowseClaude(unittest.TestCase):

    def _launch_env(self, tmp):
        """tmux env dict pointing HOME at the temp tree.

        ``TmuxFixture`` accepts an env dict at construction time and
        merges it into every shell invocation, so the recipe sees our
        synthetic ``$HOME/.claude/projects`` rather than the real one.
        """
        return {'HOME': tmp}

    def test_lists_projects(self):
        """Top level renders the (decoded) project path."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_claude(tmp)
            with TmuxFixture(cols=120, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--python', _RECIPE)
                t.wait_for('/home/test/project')
                t.send('q')

    def test_drills_into_session(self):
        """Right-arrow expands the project and reveals the session id."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_claude(tmp)
            with TmuxFixture(cols=120, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--python', _RECIPE)
                t.wait_for('/home/test/project')
                t.send('Right')
                t.wait_for('abcd1234-deadbeef', timeout=3.0)
                t.send('q')

    def test_drills_into_messages(self):
        """Drilling into the session lists the per-line records."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_claude(tmp)
            with TmuxFixture(cols=120, rows=30, env=self._launch_env(tmp)) as t:
                t.launch(_BIN, '--python', _RECIPE)
                t.wait_for('/home/test/project')
                t.send('Right')
                t.wait_for('abcd1234-deadbeef')
                t.send('Down')
                t.send('Right')
                # Wait for any of the three message kinds to render —
                # ``last-prompt`` is the first record so it's the most
                # reliable wait target.
                t.wait_for('last-prompt', timeout=3.0)
                t.send('q')


if __name__ == '__main__':
    unittest.main()
