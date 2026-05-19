"""UI tests: Ctrl-C parity across sub-modes (ticket #74).

The contract: inside any sub-mode (search, insert, pick, input,
confirm), ctrl-c cancels the sub-mode without committing. At top
level (normal mode, no sub-mode active), ctrl-c quits the app
with the cancel exit code (1, matching q/esc behaviour).

Each sub-mode is exercised through a real terminal under tmux:
the mode is entered, ctrl-c is sent as a raw byte (\\x03), and we
confirm (a) the mode exited and (b) no commit-side-effect ran.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _read_log_when_ready(path, timeout=3.0):
    """Poll ``path`` until it exists with non-empty content."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            with open(path) as f:
                content = f.read()
            if content:
                return content
        time.sleep(0.03)
    raise AssertionError(
        f'log file {path!r} not populated within {timeout}s')


class TestCtrlCParity(unittest.TestCase):

    def test_top_level_ctrl_c_quits_with_exit_1(self):
        """At top level, ctrl-c exits the app with the cancel code (1)."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\n' | {_BIN} --show-ids always --root-cmd cat --no-children-pane ; "
                     f"echo EXIT=$?")
            t.wait_for('a a')
            t.send_bytes('\x03')
            t.wait_for('EXIT=1', timeout=3.0)

    def test_search_mode_ctrl_c_returns_to_normal(self):
        """Ctrl-C inside search mode clears the query and returns to nav."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'foo\\nbar\\n' | {_BIN} --show-ids always --root-cmd cat --no-children-pane")
            t.wait_for('foo foo')
            t.send('/')
            t.type('ba')
            t.wait_for('/ba')
            t.send_bytes('\x03')
            time.sleep(0.1)
            cap = t.wait_stable()
            self.assertNotIn('/ba', cap)
            # Normal mode resumed: q quits cleanly.
            t.send('q')

    def test_insert_mode_ctrl_c_returns_to_normal(self):
        """Ctrl-C inside insert mode hides the marker and skips the callback."""
        recipe_path = os.path.join(_REPO, 'test', 'ui', 'recipes', 'insert_demo.py')
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'insert.log')
            with TmuxFixture(cols=120, rows=40, env={'INSERT_LOG': log}) as t:
                t.launch(_BIN, '--run-py', recipe_path)
                t.wait_for('a a')
                t.send('c')
                t.wait_for('-- create --')
                t.send_bytes('\x03')
                # Marker should disappear.
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    if '-- create --' not in t.capture():
                        break
                    time.sleep(0.05)
                else:
                    self.fail('insert marker did not disappear after Ctrl-C')
                # 'x' is a nav-mode-only action; firing it confirms we
                # left insert mode AND lets the recipe write its log.
                t.send('x')
                content = _read_log_when_ready(log)
            # The insert callback never ran (would have written
            # 'after:a'); the 'x' action wrote '<cancelled>' instead.
            self.assertEqual(content, '<cancelled>')

    def test_pick_ctrl_c_returns_none(self):
        """Ctrl-C inside the pick picker returns None (cancel)."""
        recipe_path = os.path.join(_REPO, 'test', 'ui', 'recipes', 'pick_demo.py')
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'pick.log')
            with TmuxFixture(cols=120, rows=40, env={'PICK_LOG': log}) as t:
                t.launch(_BIN, '--run-py', recipe_path)
                t.wait_for('item one')
                t.send('s')
                t.wait_for('Status>')
                t.send_bytes('\x03')
                content = _read_log_when_ready(log)
            self.assertEqual(content, '<cancelled>')

    def test_input_ctrl_c_returns_none(self):
        """Ctrl-C inside ctx.input returns None (not '')."""
        with tempfile.TemporaryDirectory() as tmp:
            recipe = os.path.join(tmp, 'r.py')
            log = os.path.join(tmp, 'log')
            with open(recipe, 'w') as f:
                f.write(
                    "from browse_tui import Browser, BrowserConfig, Item, Action\n"
                    "import sys\n"
                    "def get_children(_):\n"
                    "    return [Item(id='a')]\n"
                    "def go(ctx):\n"
                    f"    val = ctx.input('Name: ')\n"
                    f"    open({log!r}, 'w').write('NONE' if val is None else 'VAL:' + repr(val))\n"
                    "    ctx.quit()\n"
                    "b = Browser(BrowserConfig(get_children=get_children, "
                    "actions=[Action('i', 'Input', go, 'cursor')], "
                    "show_ids='always'))\n"
                    "sys.exit(b.run())\n")
            with TmuxFixture(cols=80, rows=24) as t:
                t.launch(_BIN, '--run-py', recipe)
                t.wait_for('a a')
                t.send('i')
                t.wait_for('Name:')
                t.send_bytes('\x03')
                content = _read_log_when_ready(log)
            self.assertEqual(content, 'NONE')

    def test_confirm_ctrl_c_returns_false(self):
        """Ctrl-C inside ctx.confirm returns False (treats as 'no')."""
        with tempfile.TemporaryDirectory() as tmp:
            recipe = os.path.join(tmp, 'r.py')
            log = os.path.join(tmp, 'log')
            with open(recipe, 'w') as f:
                f.write(
                    "from browse_tui import Browser, BrowserConfig, Item, Action\n"
                    "import sys\n"
                    "def get_children(_):\n"
                    "    return [Item(id='a')]\n"
                    "def go(ctx):\n"
                    f"    val = ctx.confirm('Sure?')\n"
                    f"    open({log!r}, 'w').write('YES' if val else 'NO')\n"
                    "    ctx.quit()\n"
                    "b = Browser(BrowserConfig(get_children=get_children, "
                    "actions=[Action('d', 'Delete', go, 'cursor')], "
                    "show_ids='always'))\n"
                    "sys.exit(b.run())\n")
            with TmuxFixture(cols=80, rows=24) as t:
                t.launch(_BIN, '--run-py', recipe)
                t.wait_for('a a')
                t.send('d')
                t.wait_for('Sure?')
                t.send_bytes('\x03')
                content = _read_log_when_ready(log)
            self.assertEqual(content, 'NO')


if __name__ == '__main__':
    unittest.main()
