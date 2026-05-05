"""UI tests for the ``recipes/browse-jira-mcp`` recipe.

The recipe drives an Atlassian MCP server (``uvx mcp-atlassian``) over
stdio JSON-RPC. Tests fall into two groups:

* **Hermetic** — `test_missing_env_shows_error_row` exercises the no-
  credentials path and runs everywhere ``tmux`` is installed; no MCP
  server is spawned.
* **Live** — `test_lists_open_tickets` and `test_preview_shows_card`
  hit the real MCP server. They are skipped unless ``uvx`` is on PATH
  and ``JIRA_URL`` / ``JIRA_USERNAME`` / ``JIRA_API_TOKEN`` are all
  set in the environment.
"""

import os
import shutil
import subprocess
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BIN = os.path.join(_REPO, 'browse-tui')
_RECIPE = os.path.join(_REPO, 'recipes', 'browse-jira-mcp')

_JIRA_ENV_KEYS = ('JIRA_URL', 'JIRA_USERNAME', 'JIRA_API_TOKEN')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _live_env():
    """Return the JIRA_* credential dict, or None if any var is unset."""
    env = {k: os.environ.get(k) for k in _JIRA_ENV_KEYS}
    if not all(env.values()):
        return None
    return env


def _require_live():
    if not shutil.which('uvx'):
        raise unittest.SkipTest('uvx not available; live MCP tests skipped')
    if _live_env() is None:
        raise unittest.SkipTest(
            f'{", ".join(_JIRA_ENV_KEYS)} not set; live MCP tests skipped')


class TestBrowseJiraMcpHermetic(unittest.TestCase):

    def test_missing_env_shows_error_row(self):
        """With no credentials the recipe shows a single error row, not a traceback."""
        with TmuxFixture(cols=140, rows=30) as t:
            # Strip the JIRA_* vars from the inherited env so the recipe
            # sees an empty config even if the test runner has them set.
            t.send_line('unset ' + ' '.join(_JIRA_ENV_KEYS))
            t.launch(_BIN, '--run-py', _RECIPE)
            t.wait_for('browse-jira-mcp', timeout=5.0)
            t.wait_for('missing env', timeout=5.0)
            t.send('q')


class TestBrowseJiraMcpLive(unittest.TestCase):

    def setUp(self):
        _require_live()
        self.creds = _live_env()

    def _launch(self, t):
        # Forward credentials into the subshell so the recipe sees them.
        for k, v in self.creds.items():
            t.send_line(f"export {k}={v!r}")
        t.launch(_BIN, '--run-py', _RECIPE)

    def test_lists_open_tickets(self):
        """The recipe reaches the real server and renders at least one ticket key."""
        with TmuxFixture(cols=140, rows=40, env=self.creds) as t:
            self._launch(t)
            t.wait_for('browse-jira-mcp', timeout=5.0)
            # Cold-start of uvx + initialize handshake can take a while
            # on first run while the package is fetched / cached.
            # ``-`` appears in every Jira issue key (PROJ-123) but not
            # in the empty/error state, so it's a cheap presence check.
            import re
            t.wait_for(re.compile(r'[A-Z]+-\d+'), timeout=90.0)
            t.send('q')

    def test_preview_shows_card(self):
        """Moving the cursor renders an issue card in the preview pane."""
        with TmuxFixture(cols=140, rows=40, env=self.creds) as t:
            self._launch(t)
            import re
            t.wait_for(re.compile(r'[A-Z]+-\d+'), timeout=90.0)
            t.send('Down')
            # The issue-card preview always has a ``Status:`` line.
            t.wait_for('Status:', timeout=30.0)
            t.send('q')


if __name__ == '__main__':
    unittest.main()
