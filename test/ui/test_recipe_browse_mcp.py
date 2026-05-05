"""UI tests for the ``recipes/browse-mcp`` generic-MCP-tools recipe.

The recipe takes an MCP server command on its argv and lists the tools
it exposes. To exercise it under tmux we point it at the same Atlassian
MCP server (``uvx mcp-atlassian``) the ``browse-jira-mcp`` recipe uses,
since that's already a confirmed-working stdio MCP server in this repo.

* **Hermetic** — `test_usage_error_when_no_args` runs everywhere
  ``tmux`` is installed; no MCP server is spawned.
* **Live** — `test_lists_tools` and `test_preview_shows_input_schema`
  hit the real MCP server and are skipped unless ``uvx`` is on PATH
  and ``JIRA_URL`` / ``JIRA_USERNAME`` / ``JIRA_API_TOKEN`` are set.
"""

import os
import shutil
import subprocess
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BIN = os.path.join(_REPO, 'browse-tui')
_RECIPE = os.path.join(_REPO, 'recipes', 'browse-mcp')

_JIRA_ENV_KEYS = ('JIRA_URL', 'JIRA_USERNAME', 'JIRA_API_TOKEN')
_MCP_SERVER = ('uvx', 'mcp-atlassian@v0.21.1')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _live_env():
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


class TestBrowseMcpHermetic(unittest.TestCase):

    def test_usage_error_when_no_args(self):
        """Run with no command -> usage line on stderr and a clean exit."""
        with TmuxFixture(cols=120, rows=20) as t:
            t.launch(_BIN, '--run-py', _RECIPE)
            t.wait_for('usage:', timeout=5.0)


class TestBrowseMcpLive(unittest.TestCase):

    def setUp(self):
        _require_live()
        self.creds = _live_env()

    def _launch(self, t):
        for k, v in self.creds.items():
            t.send_line(f"export {k}={v!r}")
        t.launch(_BIN, '--run-py', _RECIPE, *_MCP_SERVER)

    def test_lists_tools(self):
        """Top-level row list contains a known mcp-atlassian tool name."""
        with TmuxFixture(cols=140, rows=40, env=self.creds) as t:
            self._launch(t)
            t.wait_for('browse-mcp', timeout=5.0)
            # ``jira_get_issue`` is one of the stable, always-present
            # tools in mcp-atlassian — cheaper than a regex.
            t.wait_for('jira_get_issue', timeout=90.0)
            t.send('q')

    def test_preview_shows_input_schema(self):
        """Cursor change renders a tool's input schema in the preview pane."""
        with TmuxFixture(cols=140, rows=40, env=self.creds) as t:
            self._launch(t)
            t.wait_for('jira_get_issue', timeout=90.0)
            t.send('Down')
            # The preview formatter always emits an ``Input schema``
            # section header — the cheapest signal that get_preview
            # fired for the new cursor.
            t.wait_for('Input schema', timeout=15.0)
            t.send('q')


if __name__ == '__main__':
    unittest.main()
