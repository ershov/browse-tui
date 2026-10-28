"""Unit tests for the common (agent-neutral) rendering path and the trimmed
store contract in ``recipes/browse-agents-claude``: the generic canon tool
formatters on tool dicts without Claude fields, the ``extras_*`` hook /
``_TOOL_FORMATTERS`` override path, and the store functions #1282 reshaped.

Loads the generated recipe with a stubbed ``browse_tui`` like the other
browse-agents suites (full stub, fresh module every load).
"""

import importlib.util
import json
import os
import re
import sys
import tempfile
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_RECIPE = _REPO / 'recipes' / 'browse-agents-claude'


def _plain(text):
    """SGR-stripped text: md2ansi colours the command and JSON bodies when
    an earlier test module made it importable."""
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


def _stub_browse_tui():
    mod = types.ModuleType('browse_tui')

    class _Stub:
        def __init__(self, *a, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    mod.Action = mod.Browser = mod.BrowserConfig = mod.Item = _Stub
    mod.upsert = lambda id_, parent_id, *, where=None, **fields: (
        'upsert', id_, parent_id, fields, where)
    mod.mod = lambda id_, parent_id=None, *, where=None, **fields: (
        'mod', id_, parent_id, fields, where)
    mod.set_preview_op = lambda id_, text: ('set_preview', id_, text)
    mod.visible_items = lambda state: []
    mod.recipe_argv = lambda argv=None: list(sys.argv[1:] if argv is None else argv)
    sys.modules['browse_tui'] = mod


def _load_recipe():
    _stub_browse_tui()
    name = '_browse_agents_common_render'
    loader = SourceFileLoader(name, str(_RECIPE))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class _NoExtras:
    """Context manager hiding the Claude hooks so the generic path runs."""

    def __init__(self, r):
        self.r = r

    def __enter__(self):
        self.saved = (self.r.extras_render, self.r.extras_chrome)
        self.r.extras_render = None
        self.r.extras_chrome = None
        return self.r

    def __exit__(self, *exc):
        self.r.extras_render, self.r.extras_chrome = self.saved


class TestGenericToolFormatters(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_bash_umbrella_without_claude_fields(self):
        call = self.r.new_tool(canon='bash', name='shell', call_id='c1',
                               input={'command': 'ls -la', 'description': 'list'})
        result = self.r.new_tool(call_id='c1', output='a\nb\n')
        fmt = self.r._tool_formatter(call, 'umbrella')
        out = _plain(fmt(call, result))
        self.assertIn('$ ls -la', out)
        self.assertIn('# list', out)
        self.assertIn('a\nb', out)
        # In flight: the command block alone, never a decline.
        self.assertIn('ls -la', _plain(fmt(call, None)))

    def test_edit_umbrella_uses_diff(self):
        call = self.r.new_tool(canon='edit', name='patch', call_id='c1',
                               input={'file_path': 'a.py'})
        self.assertIsNone(self.r._tool_formatter(call, 'umbrella')(call, None))
        result = self.r.new_tool(call_id='c1', diff='--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n')
        saved = self.r.HAVE_DELTA
        self.r.HAVE_DELTA = False     # the built-in colouring, not delta's layout
        try:
            out = self.r._tool_formatter(call, 'umbrella')(call, result)
        finally:
            self.r.HAVE_DELTA = saved
        self.assertIn('-a', out)
        self.assertIn('+b', out)
        self.assertIn(self.r.GREEN, out)

    def test_read_write_grep_blocks(self):
        read = self.r.new_tool(canon='read', name='read', call_id='c1',
                               input={'file_path': 'x.txt', 'limit': 5})
        out = self.r._tool_formatter(read, 'umbrella')(
            read, self.r.new_tool(call_id='c1', output='1\n2\n3'))
        self.assertIn('x.txt', out)
        self.assertIn('3 lines', out)
        write = self.r.new_tool(canon='write', name='write', call_id='c2',
                                input={'file_path': 'y.txt', 'content': 'hello'})
        self.assertIn('hello', self.r._tool_formatter(write, 'umbrella')(write, None))
        grep = self.r.new_tool(canon='grep', name='grep', call_id='c3',
                               input={'pattern': 'foo'})
        out = self.r._tool_formatter(grep, 'umbrella')(
            grep, self.r.new_tool(call_id='c3', output='a.py:1:foo'))
        self.assertIn('/foo/', out)
        self.assertIn('a.py:1:foo', out)

    def test_call_body_default_is_json_and_canon_bodies_apply(self):
        other = self.r.new_tool(canon='other', name='todowrite', input={'k': 1})
        out = self.r._fmt_tool_call(other)
        self.assertIn('🔧 todowrite', out)
        self.assertIn('"k": 1', out)
        spawn = self.r.new_tool(canon='spawn', name='task',
                                input={'subagent_type': 'explore', 'prompt': 'go\nmore'})
        out = self.r._fmt_tool_call(spawn)
        self.assertIn('explore', out)
        self.assertIn('go', out)

    def test_result_body_default_colours_json_and_markdown_for_spawn(self):
        res = self.r.new_tool(call_id='c', output='{"a": 1}')
        self.assertIn('"a": 1', _plain(self.r._tool_result_body(None, res)))
        spawn = self.r.new_tool(canon='spawn', name='task', call_id='c')
        res = self.r.new_tool(call_id='c', output='# Report\n\n- done')
        self.assertIn('Report', _plain(self.r._tool_result_body(spawn, res)))

    def test_name_entry_overrides_canon_entry(self):
        call = self.r.new_tool(canon='bash', name='Demo', call_id='c1', input={'command': 'x'})
        self.r._TOOL_FORMATTERS['Demo'] = {'umbrella': lambda c, r: 'OVERRIDE'}
        try:
            self.assertEqual(self.r._tool_formatter(call, 'umbrella')(call, None), 'OVERRIDE')
            # Parts the override does not define fall back to the canon entry.
            self.assertIs(self.r._tool_formatter(call, 'call'),
                          self.r._TOOL_FORMATTERS['bash']['call'])
        finally:
            self.r._TOOL_FORMATTERS.pop('Demo', None)


class TestGenericRecordRendering(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_prompt_assistant_result_without_hooks(self):
        R = self.r.Record
        with _NoExtras(self.r) as r:
            prompt = R('user_prompt', {}, 0, text='hello **there**', is_voice=True)
            out = _plain(r._render_record(prompt))
            self.assertIn('▶ user', out)
            self.assertIn('there', out)
            peer = R('user_prompt', {}, 1, text='body', agent_voice='peer', peer='w1')
            out = r._render_record(peer)
            self.assertIn('← peer', out)
            self.assertIn('w1', out)
            call = r.new_tool(canon='bash', name='shell', call_id='c1', input={'command': 'ls'})
            asst = R('assistant_text', {}, 2, text='Looking', tools=[call],
                     meta={'model': 'm1', 'thinking': 'hmm'})
            out = _plain(r._render_record(asst))
            for needle in ('⏺ assistant', 'm1', 'hmm', 'Looking', '🔧 shell', '$ ls'):
                self.assertIn(needle, out)
            res = R('tool_result', {}, 3, tools=[r.new_tool(call_id='c1', output='out', is_error=True)])
            out = r._render_record(res, call=call)
            self.assertIn('↳ tool_result', out)
            self.assertIn('[error]', out)
            self.assertIn('id=c1', out)
            self.assertIn('out', out)
            end = R('turn_end', {}, 4, meta={'duration_ms': 1500})
            self.assertIn('1.50s', r._render_record(end))
            unknown = R('unknown', {'type': 'banana', 'x': 1}, 5, tag='banana')
            out = _plain(r._render_record(unknown))
            self.assertIn('banana', out)
            self.assertIn('"x": 1', out)

    def test_generic_chrome_rows(self):
        R = self.r.Record
        with _NoExtras(self.r) as r:
            root = R('user_prompt', {}, 0, ts=1000.0)
            rec = R('assistant_text', {}, 1, id='r1', ts=1090.0,
                    meta={'model': 'm1', 'tokens': 1234, 'duration_ms': 500})
            out = r._record_chrome(rec, root)
            for needle in ('id', 'r1', 'offset', '+1m30s', 'm1', '1,234', '0.50s'):
                self.assertIn(needle, out)
            self.assertEqual(r._record_chrome(R('meta', {}, 2)), '')

    def test_extras_hooks_take_precedence(self):
        R = self.r.Record
        rec = R('meta', {}, 0, tag='x')
        saved = (self.r.extras_render, self.r.extras_chrome)
        try:
            self.r.extras_render = lambda rec, bg='0': 'HOOK-BODY'
            self.r.extras_chrome = lambda rec, root: 'HOOK-CHROME'
            self.assertEqual(self.r._render_record(rec), 'HOOK-BODY')
            self.assertEqual(self.r._record_chrome(rec), 'HOOK-CHROME')
            # A declining hook falls back to the generic body.
            self.r.extras_render = lambda rec, bg='0': None
            self.assertIn('x', self.r._render_record(rec))
        finally:
            self.r.extras_render, self.r.extras_chrome = saved

    def test_claude_hooks_render_raw_shapes(self):
        rec = self.r._parse_record({'type': 'user', 'permissionMode': 'plan',
                                    'message': {'role': 'user', 'content': 'hi'}})
        out = self.r._render_record(rec)
        self.assertIn('▶ user', out)
        self.assertIn('[plan]', out)


class TestGenericPathSmoke(unittest.TestCase):
    """Every record of the render suite's Claude fixtures renders through
    the generic path (hooks hidden) without raising and keeps its salient
    text — the safeguard for the agent-neutral renderers until another
    agent exercises them."""

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    _EXTRA = [
        {'type': 'attachment', 'attachment': {'type': 'file', 'displayPath': 'PROBE_ATTACH.md'}},
        {'type': 'system', 'subtype': 'turn_duration', 'durationMs': 1200, 'messageCount': 3},
        {'type': 'system', 'subtype': 'compact_boundary', 'content': 'PROBE_COMPACT'},
        {'type': 'last-prompt', 'lastPrompt': 'PROBE_LAST'},
        {'type': 'user', 'uuid': 'n1', 'message': {'role': 'user', 'content': (
            '<task-notification>\n<status>completed</status>\n'
            '<result>\nPROBE_RESULT\n</result>\n</task-notification>')}},
        {'type': 'banana', 'payload': 'PROBE_UNKNOWN'},
    ]

    def test_fixtures_render_generically(self):
        from test.unit.test_browse_agents_claude_render import TestComposedToolBlock as T
        probes = {'PROBE_PROMPT', 'PROBE_BASH_CMD', 'PROBE_BASH_OUTPUT',
                  'PROBE_VOICE', 'PROBE_EDIT', 'PROBE_BASH', 'PROBE_END',
                  'PROBE_ATTACH.md', 'PROBE_COMPACT', 'PROBE_LAST',
                  'PROBE_RESULT', 'PROBE_UNKNOWN', '🔧 Bash', '🔧 Edit',
                  '↳ tool_result', '⏺ assistant', '▶ user', '← reply'}
        seen = []
        with _NoExtras(self.r) as r:
            for records in (T._BASH_TURN, T._RICH_TURN, self._EXTRA):
                root = None
                for n, raw in enumerate(records):
                    rec = r._parse_record(raw, n)
                    if r._is_turn_root(rec):
                        root = rec
                    body = r._render_record(rec)
                    chrome = r._record_chrome(rec, root if root is not rec else None)
                    self.assertIsInstance(body, str)
                    self.assertIsInstance(chrome, str)
                    seen.append(_plain(body))
        text = '\n'.join(seen)
        missing = [p for p in probes if p not in text]
        self.assertEqual(missing, [])


class TestStoreTrims(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_tail_open_refuses_non_transcripts(self):
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as f:
            f.write('{"type": "user"}\n')
        try:
            self.assertEqual(self.r.store_tail_open(f.name), ([], None))
            self.assertEqual(self.r.store_tail_open(f.name + '.missing.jsonl'), ([], None))
        finally:
            os.unlink(f.name)
        self.assertFalse(hasattr(self.r, 'store_tailable'))

    def test_session_item_wrapper_and_project_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, '-home-u-proj')
            os.makedirs(proj)
            sess = os.path.join(proj, 'abc.jsonl')
            with open(sess, 'w') as f:
                f.write(json.dumps({'type': 'user', 'cwd': '/home/u/proj',
                                    'message': {'role': 'user', 'content': 'x'}}) + '\n')
            item = self.r.store_session_item(sess)
            self.assertEqual(item.id, ('session', sess))
            self.assertEqual(item.title, 'abc')
            out = self.r._preview_project(proj)
            self.assertIn('project: /home/u/proj', out)
            self.assertIn(f'path:    {proj}', out)
            self.assertIn('1 session(s):', out)
            self.assertIn('  abc', out)
        self.assertFalse(hasattr(self.r, 'store_project_preview'))

    def test_session_dirs_and_common_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.path.join(tmp, 'repo', 'sub')
            os.makedirs(cwd)
            os.makedirs(os.path.join(tmp, 'repo', '.git'))
            proj = os.path.join(tmp, self.r._encode_project_path(cwd))
            os.makedirs(proj)
            sess = os.path.join(proj, 's.jsonl')
            with open(sess, 'w') as f:
                f.write(json.dumps({'type': 'user', 'cwd': cwd,
                                    'message': {'role': 'user', 'content': 'x'}}) + '\n')
            self.assertEqual(self.r.store_session_dirs(sess), (cwd, cwd))
            got_cwd, root = self.r._session_cwd_and_root(sess)
            self.assertEqual(got_cwd, cwd)
            if self.r._md_doc is not None:
                self.assertEqual(root, os.path.join(tmp, 'repo'))
            else:
                self.assertEqual(root, cwd)
        self.assertFalse(hasattr(self.r, 'store_session_cwd_and_root'))

    def test_child_without_transcript_reports_in_source_action(self):
        self.assertFalse(hasattr(self.r, 'store_child_source_path'))
        item = self.r.Item(id=('agent', '/tmp/none.jsonl', 'ZZZ'))
        per_line, whole = self.r._gather_line_source([item])
        self.assertEqual(per_line, {})
        self.assertEqual(whole, [(None, item)])


if __name__ == '__main__':
    unittest.main()
