"""UI smoke tests for ``recipes/browse-agents-codex``.

The recipe walks ``$CODEX_HOME/sessions/**/rollout-*.jsonl`` indexed by
``state_5.sqlite``. The tests point ``CODEX_HOME`` at the synthetic tree
``make_fake_codex`` builds (see the store unit test) and drive the recipe
under tmux through ``./browse-tui --run-py`` so they do not depend on PATH.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from test.ui.fixtures.tmux import TmuxFixture
from test.unit.test_browse_agents_codex_store import make_fake_codex, T1, T3


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BIN = os.path.join(_REPO, 'browse-tui')
_RECIPE = os.path.join(_REPO, 'recipes', 'browse-agents-codex')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestBrowseAgentsCodex(unittest.TestCase):

    def _env(self, tmp, home):
        # HOME too, so a stray default never reaches the real ~/.codex.
        return {'HOME': tmp, 'CODEX_HOME': home}

    def test_lists_projects_by_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, _paths = make_fake_codex(tmp)
            with TmuxFixture(cols=120, rows=30, env=self._env(tmp, home)) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('/home/test/projA')
                t.wait_for('/home/test/projB')
                t.send('q')

    def test_drills_into_thread_then_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, _paths = make_fake_codex(tmp)
            with TmuxFixture(cols=140, rows=30, env=self._env(tmp, home)) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('/home/test/projA')
                t.send('Right')
                t.wait_for('PROBE_TITLE_ONE', timeout=3.0)
                t.send('Down')
                t.send('Right')
                # Tree mode: the child thread in the top block, then the
                # prompt umbrella of the one turn.
                t.wait_for('PROBE_CHILD_TITLE', timeout=3.0)
                t.wait_for('PROBE_PROMPT_ONE', timeout=3.0)
                t.send('q')

    def test_direct_launch_lands_on_latest_voice_with_tool_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, paths = make_fake_codex(tmp)
            with TmuxFixture(cols=160, rows=40, env=self._env(tmp, home)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--detail', 'tools',
                         '--file', paths[T1])
                t.wait_for('PROBE_ANSWER_ONE', timeout=5.0)
                t.wait_for('<tool:exec_command>', timeout=3.0)
                t.wait_for('turn complete · 9.0s', timeout=3.0)
                t.send('q')

    def test_thread_flag_opens_by_id_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, _paths = make_fake_codex(tmp)
            with TmuxFixture(cols=140, rows=30, env=self._env(tmp, home)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--thread', T3[:8])
                t.wait_for('PROBE_CHILD_ANSWER', timeout=5.0)
                t.send('q')

    def test_live_tail_picks_up_appended_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, paths = make_fake_codex(tmp)
            with TmuxFixture(cols=160, rows=30, env=self._env(tmp, home)) as t:
                t.launch(_BIN, '--run-py', _RECIPE, '--tree', '--file', paths[T1])
                t.wait_for('PROBE_ANSWER_ONE', timeout=5.0)
                with open(paths[T1], 'a') as f:
                    f.write(json.dumps({
                        'timestamp': '2026-10-08T00:30:00.000Z', 'type': 'event_msg',
                        'payload': {'type': 'item_completed', 'turn_id': 'turn-2',
                                    'item': {'type': 'UserMessage', 'id': 'u2',
                                             'content': [{'type': 'text',
                                                          'text': 'PROBE_APPENDED'}]}},
                    }) + '\n')
                t.wait_for('PROBE_APPENDED', timeout=7.5)
                t.send('q')


if __name__ == '__main__':
    unittest.main()
