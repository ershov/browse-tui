"""End-to-end fd hygiene (spec §3.1): the shipped binary's tty-stdout path.

The unit suite (``test/unit/test_terminal_fd_hygiene``) verifies the
``term_init`` fd mechanics directly. This module exercises the *shipped
binary* under a real terminal (tmux pane, so stdout is a tty), proving the
teardown output delivery actually carries the print-exit result: with
stdout redirected to ``/dev/null`` mid-session, the selection still lands
in the user's normal scrollback after the UI exits — written to the saved
real stdout via ``term_result_fd()`` (the fzf model).

stdin is fed from a file so the UI rides ``/dev/tty`` and stdout stays the
pane tty (the case under test). The launch goes through a tiny shell
script (kept short so it never line-wraps in the capture) that runs the
binary and then prints a sentinel on its own line; the binary's print-exit
output therefore sits on the line directly above that sentinel.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A sentinel that cannot be confused with the (echoed) launch command:
# it appears verbatim only in the program's *output*, never in the short
# ``bash run.sh`` line tmux echoes. Kept shell-token-safe (no redirection
# / here-string metacharacters) so the runner script prints it cleanly.
_SENTINEL = 'SENTINEL-RUN-DONE'


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _write_runner(tmp, items):
    """Write items.txt + a short run.sh into *tmp*; return the script path.

    ``run.sh`` runs the binary with stdin from the file (so the UI device
    is /dev/tty and stdout stays the pane tty) and then prints the sentinel
    on its own line.
    """
    stdin_file = os.path.join(tmp, 'items.txt')
    with open(stdin_file, 'w') as f:
        f.write(items)
    script = os.path.join(tmp, 'run.sh')
    with open(script, 'w') as f:
        f.write(
            '#!/bin/bash\n'
            f'{_BIN} --root-cmd cat --show-ids never < {stdin_file}\n'
            f'echo {_SENTINEL}\n')
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
    return script


def _result_line_above_sentinel(cap):
    """Return the stripped scrollback line directly above the sentinel."""
    lines = [line.rstrip() for line in cap.splitlines()]
    sentinel_idx = next(i for i, line in enumerate(lines)
                        if _SENTINEL in line)
    return lines[sentinel_idx - 1].strip()


class TestTtyStdoutResultDelivery(unittest.TestCase):
    """print-exit selection reaches tty scrollback after the UI exits.

    Regression guard for the teardown output delivery: ``term_init``
    redirects a tty fd 1 to ``/dev/null``, so a result written through
    ``sys.stdout`` would silently vanish. The teardown must instead write
    it to the saved real stdout (``term_result_fd()``) once the alt-screen
    is gone, so a bare interactive run still echoes its result.
    """

    def test_print_exit_selection_in_scrollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = _write_runner(tmp, 'alpha\nbeta\n')
            with TmuxFixture(cols=120, rows=40) as t:
                t.launch('bash', script)
                t.wait_for('alpha')
                # Move the cursor to 'beta' and print-exit on it.
                t.send('Down')
                t.send('Enter')
                # The run finished once the sentinel prints on its own
                # line; the selection must sit directly above it in normal
                # scrollback (not inside the torn-down alt-screen).
                cap = t.wait_for(_SENTINEL)
        self.assertEqual(
            _result_line_above_sentinel(cap), 'beta',
            f'print-exit result must land in scrollback above the '
            f'sentinel; capture was:\n{cap}')

    def test_first_row_default_selection(self):
        """Enter with no navigation print-exits the first (cursor) row.

        Complements the navigated case: confirms the saved-fd delivery is
        not specific to a moved cursor and that the result is a single
        clean scrollback line.
        """
        with tempfile.TemporaryDirectory() as tmp:
            script = _write_runner(tmp, 'only-one\nsecond\n')
            with TmuxFixture(cols=120, rows=40) as t:
                t.launch('bash', script)
                t.wait_for('only-one')
                t.send('Enter')
                cap = t.wait_for(_SENTINEL)
        self.assertEqual(_result_line_above_sentinel(cap), 'only-one',
                         f'capture was:\n{cap}')


if __name__ == '__main__':
    unittest.main()
