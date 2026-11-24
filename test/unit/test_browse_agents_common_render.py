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
                    meta={'model': 'm1', 'usage': {'input': 1234}, 'duration_ms': 500})
            out = r._record_chrome(rec, root)
            for needle in ('id', 'r1', 'offset', '+1m30s', 'm1', '1.2K', '0.50s'):
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


class TestUsageFormatting(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_fmt_cost(self):
        f = self.r._fmt_cost
        self.assertEqual(f(None), '')
        self.assertEqual(f('$0.0488'), '')
        self.assertEqual(f(True), '')
        self.assertEqual(f(0), '$0.0000')
        self.assertEqual(f(0.0488), '$0.0488')
        self.assertEqual(f(15.2955), '$15.30')
        self.assertEqual(f(1234.5), '$1,234.50')

    def test_fmt_usage(self):
        f = self.r._fmt_usage
        self.assertEqual(f(None), '')
        self.assertEqual(f({}), '')
        self.assertEqual(
            f({'cache_read': 6_500_000, 'output': 204, 'input': 34_200, 'cache_write': 0}),
            'in 34.2K · out 204 · cache r 6.5M')
        self.assertEqual(f({'reasoning': 5, 'total': 9, 'cache_write': 7}),
                         'cache w 7 · think 5 · total 9')

    def test_usage_brief(self):
        b = self.r._usage_brief
        self.assertEqual(b({'cost': 0.12, 'usage': {'total': 5}}), '$0.1200')
        self.assertEqual(b({'usage': {'total': 52_000, 'input': 1}}), '52.0K tok')
        self.assertEqual(b({'usage': {'input': 800, 'output': 200}}), '')
        self.assertEqual(b({}), '')
        self.assertEqual(b({'usage': {'cache_read': 9}}), '')


class TestUsageRendering(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def test_chrome_rows(self):
        R = self.r.Record
        with _NoExtras(self.r) as r:
            rec = R('assistant_text', {}, 1, meta={
                'model': 'm1', 'cost': 0.0488,
                'usage': {'input': 1500, 'output': 20}, 'duration_ms': 500})
            out = _plain(r._record_chrome(rec))
            self.assertRegex(out, r'cost\s*: \$0\.0488\n')
            self.assertRegex(out, r'usage\s*: in 1.5K · out 20')
            self.assertLess(out.index('model'), out.index('cost'))
            self.assertLess(out.index('usage'), out.index('duration'))
            summed = R('turn_end', {}, 2, meta={'cost': 2.5, 'cost_summed': True})
            self.assertIn('$2.50  (sum of messages)', _plain(r._record_chrome(summed)))

    def test_chrome_session_cost_row(self):
        R = self.r.Record
        with _NoExtras(self.r) as r:
            rec = R('assistant_text', {}, 1,
                    meta={'session_cost': 8.9, 'usage': {'input': 5}})
            out = _plain(r._record_chrome(rec))
            self.assertRegex(out, r'session cost\s*: \$8\.90')
            self.assertLess(out.index('session cost'), out.index('usage'))

    def test_turn_end_title_suffix(self):
        R = self.r.Record
        plain = R('turn_end', {}, 0, tag='turn', summary='done')
        self.assertEqual(self.r._summarise_message(plain), 'done')
        with_cost = R('turn_end', {}, 1, tag='turn', summary='done', meta={'cost': 0.12})
        self.assertEqual(self.r._summarise_message(with_cost), 'done · $0.1200')
        with_tok = R('turn_end', {}, 2, tag='turn', meta={'usage': {'total': 52_000}})
        self.assertTrue(self.r._summarise_message(with_tok).endswith(' · 52.0K tok'))
        other = R('assistant_text', {}, 3, tag='assistant', text='hi', meta={'cost': 1.0})
        self.assertEqual(self.r._summarise_message(other), 'assistant: hi')

    def _cascade(self, recs, n, level):
        r = self.r
        saved = (r._record_at, r._scan_tree, r._owner_call, r._DETAIL_LEVEL)
        r._record_at = lambda sess, k: recs[k]
        last = {}
        for k, rec in enumerate(recs):
            if rec is not None and rec.meta.get('msg_id') is not None:
                last[rec.meta['msg_id']] = k
        r._scan_tree = lambda sess: types.SimpleNamespace(records=recs, msg_last_key=last)
        r._owner_call = lambda sess, rec: None
        r._DETAIL_LEVEL = level
        try:
            return _plain(r._render_record_with_rule('s', n))
        finally:
            r._record_at, r._scan_tree, r._owner_call, r._DETAIL_LEVEL = saved

    def test_cascade_usage_line(self):
        R = self.r.Record
        with _NoExtras(self.r):
            rec = R('assistant_text', {}, 0, text='hi', meta={
                'cost': 0.5, 'usage': {'input': 10, 'output': 2}})
            self.assertIn('💰 $0.5000 · in 10 · out 2', self._cascade([rec], 0, 5))
            self.assertNotIn('💰', self._cascade([rec], 0, 4))
            end = R('turn_end', {}, 0, meta={'cost': 0.5})
            self.assertIn('💰 $0.5000', self._cascade([end], 0, 5))
            meta = R('meta', {}, 0, text='token_usage', meta={'usage': {'input': 10}})
            self.assertNotIn('💰', self._cascade([meta], 0, 5))
            bare = R('assistant_text', {}, 0, text='hi')
            self.assertNotIn('💰', self._cascade([bare], 0, 5))

    def test_cascade_usage_line_once_per_msg_id(self):
        R = self.r.Record
        meta = {'msg_id': 'm1', 'usage': {'input': 10}}
        a = R('thinking', {}, 0, text='t', meta=dict(meta))
        b = R('assistant_text', {}, 1, text='x', meta=dict(meta))
        c = R('assistant_text', {}, 2, text='y', meta={'msg_id': 'm2', 'usage': {'input': 3}})
        recs = [a, b, c]
        with _NoExtras(self.r):
            self.assertNotIn('💰', self._cascade(recs, 0, 5))
            self.assertIn('💰 in 10', self._cascade(recs, 1, 5))
            self.assertIn('💰 in 3', self._cascade(recs, 2, 5))

    def test_cascade_usage_line_interleaved_blocks(self):
        R = self.r.Record
        meta = {'msg_id': 'mA', 'usage': {'input': 10}}
        recs = [R('assistant_text', {}, 0, text='a0', meta=dict(meta)),
                R('tool_result', {}, 1, text='res'),
                R('user_text', {}, 2, text='u'),
                R('assistant_text', {}, 3, text='a3', meta=dict(meta)),
                R('assistant_text', {}, 4, text='a4', meta=dict(meta))]
        with _NoExtras(self.r):
            for n in (0, 3):
                self.assertNotIn('💰', self._cascade(recs, n, 5))
            self.assertIn('💰 in 10', self._cascade(recs, 4, 5))

    def test_msg_last_key_fold_and_replace(self):
        r = self.r
        R = r.Record
        td, cur = r._TreeData(), r._ScanCursor()
        recs = [R('assistant_text', {}, 0, text='a', meta={'msg_id': 'mA'}),
                R('assistant_text', {}, 1, text='b', meta={'msg_id': 'mB'}),
                R('assistant_text', {}, 2, text='c', meta={'msg_id': 'mA'})]
        for n, rec in enumerate(recs):
            rec.key = n
        r._fold_records(td, cur, list(enumerate(recs)), 's')
        self.assertEqual(td.msg_last_key, {'mA': 2, 'mB': 1})
        changed = R('assistant_text', {}, 0, text='a2', meta={'msg_id': 'mA'})
        changed.key = 0
        r._replace_record(td, changed, 0, 's')
        self.assertEqual(td.msg_last_key, {'mA': 2, 'mB': 1})
        self.assertIs(td.records[0], changed)


class TestSessionUsageFold(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def _fold(self, recs, sess='s'):
        r = self.r
        td, cur = r._TreeData(), r._ScanCursor()
        for n, rec in enumerate(recs):
            rec.key = n
        r._fold_records(td, cur, list(enumerate(recs)), sess)
        return td, cur

    def _prompt(self, **kw):
        return self.r.Record('user_prompt', {}, text='go', is_voice=True, **kw)

    def _rec(self, kind='assistant_text', **meta):
        return self.r.Record(kind, {}, text='x', meta=meta)

    def _su(self, cost=None, usage=None, by_model=None, snapshot=False):
        return {'cost': cost, 'usage': usage, 'by_model': by_model or {}, 'snapshot': snapshot}

    def _card(self, td, meta=(), is_child=False):
        r = self.r
        saved = r.store_session_info
        r.store_session_info = lambda sess: {
            'path': '/p', 'label': 's', 'is_child': is_child,
            'meta': list(meta), 'mtime': None}
        try:
            return _plain(r._fmt_scope_card('s', td))
        finally:
            r.store_session_info = saved

    def test_last_session_usage_wins(self):
        first, last = self._su(cost=1.0), self._su(cost=2.0)
        td, _ = self._fold([self._prompt(), self._rec('meta', session_usage=first),
                            self._rec(), self._rec('meta', session_usage=last)])
        self.assertIs(td.session_usage, last)
        self.assertEqual(td.session_usage_key, 3)

    def test_scope_card_cost_with_and_without_models(self):
        two = self._su(cost=1.5, by_model={'opus': {'cost': 1.0}, 'haiku': {'cost': 0.5}})
        td, _ = self._fold([self._prompt(), self._rec('meta', session_usage=two)])
        self.assertRegex(self._card(td), r'cost\s*: \$1\.50  \(opus \$1\.00 · haiku \$0\.5000\)\n')
        one = self._su(cost=1.5, by_model={'opus': {'cost': 1.5}})
        td, _ = self._fold([self._prompt(), self._rec('meta', session_usage=one)])
        self.assertRegex(self._card(td), r'cost\s*: \$1\.50\n')

    def test_scope_card_tokens_row(self):
        su = self._su(usage={'input': 1500, 'output': 20})
        td, _ = self._fold([self._prompt(), self._rec('meta', session_usage=su)])
        out = self._card(td)
        self.assertRegex(out, r'tokens\s*: in 1.5K · out 20\n')
        self.assertNotRegex(out, r'\bcost\s*:')

    def test_stale_note(self):
        snap = self._su(cost=1.0, snapshot=True)
        ts = self.r.datetime.datetime.now().timestamp() - 120
        recs = [self._prompt(ts=ts), self._rec('meta', session_usage=snap), self._prompt(), self._rec()]
        td, _ = self._fold(recs)
        self.assertIn('$1.00  as of exit 2m ago · +2 records after', self._card(td))
        td, _ = self._fold([self._prompt(), self._rec('meta', session_usage=snap), self._prompt()])
        self.assertIn('as of exit · +1 records after', self._card(td))
        td, _ = self._fold([self._prompt(), self._rec('meta', session_usage=snap), self._rec('meta')])
        self.assertNotIn('as of exit', self._card(td))
        fresh = self._su(cost=1.0)
        td, _ = self._fold([self._prompt(), self._rec('meta', session_usage=fresh), self._rec()])
        self.assertNotIn('as of exit', self._card(td))
        tok = self._su(usage={'input': 5}, snapshot=True)
        td, _ = self._fold([self._prompt(), self._rec('meta', session_usage=tok), self._rec()])
        self.assertRegex(self._card(td), r'tokens\s*: in 5  as of exit · \+1 records after')

    def test_fold_rows_replace_store_rows(self):
        su = self._su(cost=2.0, usage={'input': 10})
        td, _ = self._fold([self._prompt(), self._rec('meta', session_usage=su)])
        out = self._card(td, meta=[('model', 'm'), ('cost', '9.9999'), ('tokens', '7')])
        self.assertEqual(out.count('cost'), 1)
        self.assertEqual(out.count('tokens'), 1)
        self.assertIn('$2.00', out)
        self.assertNotIn('9.9999', out)
        self.assertRegex(out, r'model\s*: m')
        td, _ = self._fold([self._prompt()])
        self.assertIn('9.9999', self._card(td, meta=[('cost', '9.9999')]))

    def test_no_cost_row_without_usage(self):
        td, _ = self._fold([self._prompt()])
        self.assertNotRegex(self._card(td), r'(?m)^\s*cost\s*:')
        self.assertNotRegex(self._card(td, is_child=True), r'(?m)^\s*cost\s*:')

    def test_turn_cost_sum(self):
        end = self._rec('turn_end')
        self._fold([self._prompt(), self._rec(cost=0.01), self._rec('tool_result', cost=0.02), end])
        self.assertAlmostEqual(end.meta['cost'], 0.03)
        self.assertTrue(end.meta['cost_summed'])
        own = self._rec('turn_end', cost=0.5)
        self._fold([self._prompt(), self._rec(cost=0.01), own])
        self.assertEqual(own.meta, {'cost': 0.5})
        bare = self._rec('turn_end')
        self._fold([self._prompt(), self._rec(), bare])
        self.assertNotIn('cost', bare.meta)
        self.assertNotIn('cost_summed', bare.meta)

    def test_session_cost_stamp(self):
        def snap(cost):
            return self._rec('meta', session_usage=self._su(cost=cost, snapshot=True))

        def end():
            return self.r.Record('assistant_text', {}, text='x', end_turn=True)

        a = end()
        self._fold([self._prompt(), a, snap(1.5)])
        self.assertEqual(a.meta['session_cost'], 1.5)

        a1, a2 = end(), end()
        self._fold([self._prompt(), a1, self._prompt(), a2, snap(8.9)])
        self.assertNotIn('session_cost', a1.meta)
        self.assertEqual(a2.meta['session_cost'], 8.9)

        a1, a2 = end(), end()
        self._fold([self._prompt(), a1, snap(1.0), self._prompt(), a2, snap(1.75)])
        self.assertEqual(a1.meta['session_cost'], 1.0)
        self.assertEqual(a2.meta['session_cost'], 1.75)

        a1 = end()
        self._fold([self._prompt(), a1, snap(1.0), snap(2.0)])
        self.assertEqual(a1.meta['session_cost'], 1.0)

        a1, plain = end(), self._rec()
        self._fold([self._prompt(), a1, snap(1.0), self._prompt(), plain, snap(2.0)])
        self.assertEqual(a1.meta['session_cost'], 1.0)
        self.assertNotIn('session_cost', plain.meta)

        a = end()
        self._fold([self._prompt(), a, self._rec('meta', session_usage=self._su(cost=1.0))])
        self.assertNotIn('session_cost', a.meta)

        self._fold([self._prompt(), self._rec(), snap(1.0)])

    def test_tail_changed_cost_before_new_turn_end(self):
        r = self.r
        sess = 'tail-sess'
        streaming = self._rec(cost=0.01)
        td, cur = self._fold([self._prompt(), streaming], sess)
        final = self._rec(cost=0.04)
        final.key = 1
        end = self._rec('turn_end')
        end.key = 2
        saved = r.store_tail_poll
        r.store_tail_poll = lambda s, state: ([(2, end)], [(1, final)])
        r._TREE_CACHE[sess] = td
        r._TAIL_STATE[sess] = types.SimpleNamespace(cursor=cur)
        try:
            r._read_new_records(sess)
        finally:
            r.store_tail_poll = saved
            r._TREE_CACHE.pop(sess, None)
            r._TAIL_STATE.pop(sess, None)
        self.assertIs(td.records[1], final)
        self.assertAlmostEqual(end.meta['cost'], 0.04)


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
