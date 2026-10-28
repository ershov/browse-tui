"""UI smoke tests for ``recipes/browse-agents-opencode``.

The recipe reads ``$XDG_DATA_HOME/opencode/opencode.db``. The tests point
``XDG_DATA_HOME`` at the synthetic store ``make_fake_opencode`` builds (see
the store unit test) and drive the recipe under tmux through
``./browse-tui --run-py`` so they do not depend on PATH.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from test.ui.fixtures.tmux import TmuxFixture
from test.unit.test_browse_agents_opencode_store import make_fake_opencode, S1, S3, T0


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BIN = os.path.join(_REPO, 'browse-tui')
_RECIPE = os.path.join(_REPO, 'recipes', 'browse-agents-opencode')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestBrowseAgentsOpenCode(unittest.TestCase):

    def _env(self, tmp, share):
        return {'HOME': tmp, 'XDG_DATA_HOME': share}

    def test_lists_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            share, _db = make_fake_opencode(tmp)
            with TmuxFixture(cols=120, rows=30, env=self._env(tmp, share)) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('/home/test/projA')
                t.wait_for('/home/test/projB')
                t.send('q')

    def test_drills_into_session_then_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            share, _db = make_fake_opencode(tmp)
            with TmuxFixture(cols=140, rows=30, env=self._env(tmp, share)) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('/home/test/projA')
                t.send('Right')
                t.wait_for('PROBE_TITLE_ONE', timeout=3.0)
                t.send('Down')
                t.send('Right')
                t.wait_for('PROBE_CHILD_TITLE', timeout=3.0)   # child session block
                t.wait_for('PROBE_PROMPT_ONE', timeout=3.0)    # first turn umbrella
                t.send('q')

    def test_session_flag_lands_on_latest_voice_with_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            share, _db = make_fake_opencode(tmp)
            with TmuxFixture(cols=160, rows=40, env=self._env(tmp, share)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--detail', 'tools', '--session', S1)
                t.wait_for('PROBE_PROMPT_TWO', timeout=5.0)
                t.wait_for('<tool:read>', timeout=3.0)
                t.wait_for('PROBE_PROMPT_ONE', timeout=3.0)   # the earlier, collapsed turn
                t.send('q')

    def test_child_session_by_id_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            share, _db = make_fake_opencode(tmp)
            with TmuxFixture(cols=140, rows=30, env=self._env(tmp, share)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--session', S3[:-1] + 'c')
                t.wait_for('PROBE_CHILD_ANSWER', timeout=5.0)
                t.send('q')

    def test_v1_store_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            share, _db = make_fake_opencode(tmp, gen='v1')
            with TmuxFixture(cols=140, rows=30, env=self._env(tmp, share)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--detail', 'voice', '--session', S1)
                t.wait_for('PROBE_ANSWER_ONE', timeout=5.0)
                t.send('q')

    def test_live_tail_picks_up_a_new_message_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            share, db = make_fake_opencode(tmp)
            with TmuxFixture(cols=160, rows=30, env=self._env(tmp, share)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--tree', '--session', S1)
                t.wait_for('PROBE_PROMPT_TWO', timeout=5.0)
                con = sqlite3.connect(db)
                con.execute('INSERT INTO session_message VALUES (?,?,?,?,?,?,?)', (
                    'msg_99', S1, 'user', 9, T0 + 30000, T0 + 30000,
                    json.dumps({'time': {'created': T0 + 30000}, 'text': 'PROBE_APPENDED'})))
                con.commit()
                con.close()
                t.wait_for('PROBE_APPENDED', timeout=5.0)
                t.send('q')


if __name__ == '__main__':
    unittest.main()
