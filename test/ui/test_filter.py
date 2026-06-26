"""UI tests: filter-mode prompt entry / typing / commit / clear.

Covers the `&` keybinding flow:
  * `&` enters filter-edit mode and shows the `&` prompt in the info bar
  * typed characters narrow the visible list live
  * Enter commits the filter; the prompt closes but the narrowing stays
  * `&` again stacks another filter (AND semantics)
  * Ctrl-X clears all filters and exits filter-edit mode
  * Ctrl-C cancels the in-progress edit, keeping committed filters
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RECIPE = os.path.join(_REPO, 'recipes', 'browse-claude')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestFilter(unittest.TestCase):

    def test_ampersand_enters_filter_mode_and_shows_prompt(self):
        """`&` then text shows the prompt and narrows the list."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'apple\\nbanana\\ncherry\\n' | {_BIN} "
                     f"--show-ids always --root-cmd cat")
            t.wait_for('apple apple')
            t.send('&')
            t.type('app')
            # Prompt visible in info bar.
            t.wait_for('& app_', timeout=2.0)
            screen = t.wait_stable()
            # Non-matching items dropped from view.
            self.assertNotIn('banana', screen)
            self.assertNotIn('cherry', screen)
            self.assertIn('apple', screen)

    def test_enter_commits_filter_and_closes_prompt(self):
        """After Enter the prompt loses its underscore but stays visible
        as a committed filter."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'apple\\nbanana\\ncherry\\n' | {_BIN} "
                     f"--show-ids always --root-cmd cat")
            t.wait_for('apple apple')
            t.send('&')
            t.type('app')
            t.wait_for('& app_', timeout=2.0)
            t.send('Enter')
            # Committed display: trailing underscore is gone.
            t.wait_for('& app', timeout=2.0)
            screen = t.capture()
            self.assertNotIn('& app_', screen)
            self.assertNotIn('banana', screen)

    def test_ctrl_x_clears_all_filters(self):
        """Ctrl-X drops every committed filter and exits filter-edit.

        Ctrl-X is bound inside FILTER_EDIT mode, so the user re-enters
        with ``&`` before pressing it.
        """
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'apple\\nbanana\\ncherry\\n' | {_BIN} "
                     f"--show-ids always --root-cmd cat")
            t.wait_for('apple apple')
            t.send('&')
            t.type('app')
            t.send('Enter')
            t.wait_for('& app', timeout=2.0)
            # Re-enter filter-edit then Ctrl-X clears.
            t.send('&')
            t.send_bytes('\x18')   # ctrl-x
            # Filter prompt gone; banana / cherry back.
            t.wait_for('banana', timeout=2.0)
            screen = t.capture()
            self.assertNotIn('& app', screen)

    def test_ctrl_c_cancels_in_progress_keeps_committed(self):
        """Ctrl-C drops the in-progress filter but keeps committed ones."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'apple\\nbanana\\ncherry\\n' | {_BIN} "
                     f"--show-ids always --root-cmd cat")
            t.wait_for('apple apple')
            t.send('&')
            t.type('app')
            t.send('Enter')
            # Now start another filter but cancel.
            t.send('&')
            t.type('xyz')
            t.wait_for('& app & xyz_', timeout=2.0)
            t.send_bytes('\x03')   # ctrl-c
            t.wait_for('& app', timeout=2.0)
            screen = t.capture()
            self.assertNotIn('xyz', screen)
            self.assertNotIn('banana', screen)


class TestFilterAmpBashRegression(unittest.TestCase):
    """Regression: typing `&Bash` must hide non-Bash umbrellas immediately.

    The original bug: with ``show_preview=True`` (the default), umbrellas
    whose visible text did NOT contain ``Bash`` stayed on screen until
    the cursor moved over them. Toggling preview off (Ctrl-P) broke the
    implicit fix entirely — wrong umbrellas stayed forever.

    The visible-tree-only filter evaluator (epic #496, tickets #497-#501)
    removed the preview generator as a materialization side channel.
    So the visible set after typing ``&Bash`` must be deterministic and
    insensitive to ``show_preview`` and cursor position.

    Fixture choice: synthetic .jsonl with one turn whose ``<prompt>``
    umbrella has three tool umbrella children — one ``<tool:Bash>``, one
    ``<tool:Write>``, one ``<tool:Read>``. The cursor-on-open expands
    the umbrella so all three siblings render on launch, mirroring the
    real-world repro (large turn the user expanded before filtering).

    Assertion markers chosen for list-pane uniqueness:
      * ``<tool:Bash>`` / ``<tool:Write>`` / ``<tool:Read>`` are only
        emitted in umbrella *titles* (one source line in the recipe);
        they never appear in cascaded preview output, so capture-wide
        ``assertNotIn`` is safe even when the preview pane is on.
    """

    def _make_session_fixture(self, tmp):
        """Build a single-turn .jsonl with Bash + Write + Read tool umbrellas.

        Layout (chronological on disk):
          1. user voice (turn root) — no "Bash" substring.
          2. assistant tool_use Bash + paired tool_result.
          3. assistant tool_use Write (file_path has no "Bash") +
             paired tool_result.
          4. assistant tool_use Read (file_path has no "Bash") +
             paired tool_result.

        Returns the .jsonl path. The session lives under
        ``$HOME/.claude/projects/-home-test-ampbash/`` so the recipe's
        project-scan picks it up if launched without ``--file``.

        These tools are pure tool_use (no voice text), so the launches
        below pass ``--detail 4`` to defeat browse-claude's level-1
        default — the point here is the engine's ``&`` filter, not the
        recipe's detail filter.
        """
        proj = os.path.join(tmp, '.claude', 'projects', '-home-test-ampbash')
        os.makedirs(proj)
        sess = os.path.join(proj, 'ampbash.jsonl')
        records = [
            {'type': 'user', 'uuid': 'u1', 'parentUuid': None,
             'message': {'role': 'user',
                         'content': 'PROBE_USER work on files'}},
            {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                  'input': {'command': 'PROBE_BASH_CMD'}},
             ]}},
            {'type': 'user', 'uuid': 'r1', 'parentUuid': 'a1',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 't1',
                  'content': 'PROBE_BASH_OUT'}]},
             'toolUseResult': 'PROBE_BASH_OUT'},
            {'type': 'assistant', 'uuid': 'a2', 'parentUuid': 'r1',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't2', 'name': 'Write',
                  'input': {'file_path': '/tmp/PROBE_WRITE.txt'}},
             ]}},
            {'type': 'user', 'uuid': 'r2', 'parentUuid': 'a2',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 't2',
                  'content': 'PROBE_WRITE_OUT'}]},
             'toolUseResult': 'PROBE_WRITE_OUT'},
            {'type': 'assistant', 'uuid': 'a3', 'parentUuid': 'r2',
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 't3', 'name': 'Read',
                  'input': {'file_path': '/tmp/PROBE_READ.txt'}},
             ]}},
            {'type': 'user', 'uuid': 'r3', 'parentUuid': 'a3',
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 't3',
                  'content': 'PROBE_READ_OUT'}]},
             'toolUseResult': 'PROBE_READ_OUT'},
        ]
        with open(sess, 'w') as f:
            for rec in records:
                f.write(json.dumps(rec) + '\n')
        return sess

    def test_amp_bash_filters_to_bash_umbrella_only(self):
        """Typing `&Bash` narrows the list to <tool:Bash> + its scaffold.

        Sanity step on the new evaluator: with the prompt umbrella
        auto-expanded (cursor lands inside it), the live narrowing
        must drop ``<tool:Write>`` and ``<tool:Read>`` immediately —
        no cursor move required.
        """
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_session_fixture(tmp)
            with TmuxFixture(cols=160, rows=30, env={'HOME': tmp}) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--tree', '--detail', '4', '--file', sess)
                # All three tool umbrellas visible on launch (the
                # cursor-on-open expanded the <prompt>).
                t.wait_for('<tool:Bash>', timeout=5.0)
                t.wait_for('<tool:Write>', timeout=3.0)
                t.wait_for('<tool:Read>', timeout=3.0)
                t.wait_stable()

                # Live narrowing — type without committing.
                t.send('&')
                t.type('Bash')
                t.wait_for('& Bash_', timeout=2.0)
                t.wait_stable()

                cap = t.capture()
                self.assertIn('<tool:Bash>', cap,
                              f'Bash umbrella missing during filter: '
                              f'{cap!r}')
                self.assertNotIn('<tool:Write>', cap,
                                 f'Write umbrella should be hidden: '
                                 f'{cap!r}')
                self.assertNotIn('<tool:Read>', cap,
                                 f'Read umbrella should be hidden: '
                                 f'{cap!r}')
                # Scope row + <prompt> scaffold still visible.
                self.assertIn('<prompt>', cap,
                              f'<prompt> scaffold missing: {cap!r}')
                t.send('q')

    def test_amp_bash_preview_off_then_cursor_move_keeps_visible_set(self):
        """The regression anchor: Ctrl-P + cursor moves don't un-hide.

        Original user bug:
          * In browse-claude, after typing ``& Bash``, with
            ``show_preview=True``, many non-Bash umbrellas stayed on
            screen until the cursor moved over them.
          * Toggling preview off (Ctrl-P) broke the implicit fix
            entirely — wrong umbrellas stayed forever.

        After the visible-tree-only evaluator + per-op propagation +
        recompute-on-scope/expand fixes (#497..#501), the filter no
        longer depends on the preview generator as a materialization
        side channel. So this exact flow must produce a stable
        visible set independent of preview state and cursor moves.
        """
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_session_fixture(tmp)
            with TmuxFixture(cols=160, rows=30, env={'HOME': tmp}) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--tree', '--detail', '4', '--file', sess)
                t.wait_for('<tool:Bash>', timeout=5.0)
                t.wait_for('<tool:Write>', timeout=3.0)
                t.wait_for('<tool:Read>', timeout=3.0)
                t.wait_stable()

                # & Bash + commit.
                t.send('&')
                t.type('Bash')
                t.wait_for('& Bash_', timeout=2.0)
                t.send('Enter')
                t.wait_for('& Bash', timeout=2.0)
                t.wait_stable()

                # Toggle preview OFF — the bug's kill-switch.
                t.send_bytes('\x10')   # Ctrl-P
                t.wait_stable()
                # Cursor moves — the bug's trigger.
                for _ in range(3):
                    t.send('Down')
                t.wait_stable()

                cap = t.capture()
                # Regression assertions: the visible set must STILL be
                # narrowed to Bash + scaffold.
                self.assertIn('<tool:Bash>', cap,
                              f'Bash umbrella lost after Ctrl-P + Down: '
                              f'{cap!r}')
                self.assertIn('<prompt>', cap,
                              f'<prompt> scaffold lost after '
                              f'Ctrl-P + Down: {cap!r}')
                self.assertNotIn('<tool:Write>', cap,
                                 f'Write umbrella reappeared after '
                                 f'Ctrl-P + Down (the original bug): '
                                 f'{cap!r}')
                self.assertNotIn('<tool:Read>', cap,
                                 f'Read umbrella reappeared after '
                                 f'Ctrl-P + Down: {cap!r}')
                t.send('q')

    def test_amp_bash_preview_on_cursor_move_keeps_visible_set(self):
        """Sanity companion: with preview ON, cursor moves also don't
        un-hide non-Bash umbrellas.

        The bug originally manifested with ``show_preview=True``; this
        confirms the fix holds in both preview modes (the filter no
        longer depends on preview side channels at all).
        """
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_session_fixture(tmp)
            with TmuxFixture(cols=160, rows=30, env={'HOME': tmp}) as t:
                t.launch(_BIN, '--run-py', _RECIPE,
                         '--tree', '--detail', '4', '--file', sess)
                t.wait_for('<tool:Bash>', timeout=5.0)
                t.wait_for('<tool:Write>', timeout=3.0)
                t.wait_for('<tool:Read>', timeout=3.0)
                t.wait_stable()

                t.send('&')
                t.type('Bash')
                t.wait_for('& Bash_', timeout=2.0)
                t.send('Enter')
                t.wait_for('& Bash', timeout=2.0)
                t.wait_stable()

                # NO Ctrl-P — keep show_preview=True (the original
                # bug condition). Cursor moves shouldn't un-hide.
                for _ in range(3):
                    t.send('Down')
                t.wait_stable()

                cap = t.capture()
                self.assertIn('<tool:Bash>', cap)
                self.assertIn('<prompt>', cap)
                self.assertNotIn('<tool:Write>', cap,
                                 f'Write reappeared with preview ON + '
                                 f'cursor moves: {cap!r}')
                self.assertNotIn('<tool:Read>', cap,
                                 f'Read reappeared with preview ON + '
                                 f'cursor moves: {cap!r}')
                t.send('q')


if __name__ == '__main__':
    unittest.main()
