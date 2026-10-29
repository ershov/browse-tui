"""The common live-tail ``changed`` path (``_read_new_records`` /
``_push_tail_diffs`` / ``_push_flat_inserts``) on a store whose tail
reports in-place changes: the OpenCode recipe over the synthetic DB of
``test_browse_agents_opencode_store`` (a streaming assistant row whose
tool completes and gains a text).
"""

import json
import os
import sqlite3
import tempfile
import unittest

from test.unit.test_browse_agents_opencode_store import (
    _load_recipe, _v2_rows, make_fake_opencode, S1, S3, T0,
)


class _FakeBrowser:
    """Just enough of ``Browser`` for the tail pushes."""

    def __init__(self):
        self.children = {}
        self.ops = []

    def cache(self, recipe, parent_id):
        self.children[parent_id] = recipe.get_children(parent_id)

    def get_item(self, item_id):
        for items in self.children.values():
            for it in items:
                if it.id == item_id:
                    return it
        return None

    def cached_children(self, parent_id):
        return self.children.get(parent_id)

    def update_data(self, ops):
        self.ops.extend(ops)


class CommonTailChangedTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _load_recipe()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.share, self.db = make_fake_opencode(self.tmp.name)
        self._xdg = os.environ.get('XDG_DATA_HOME')
        os.environ['XDG_DATA_HOME'] = self.share
        self._mode = self.r._TREE_MODE
        self.r.store_reset()
        self.r._TREE_CACHE.clear()
        self.r._TAIL_STATE.clear()

    def tearDown(self):
        self.r._TREE_MODE = self._mode
        if self._xdg is None:
            os.environ.pop('XDG_DATA_HOME', None)
        else:
            os.environ['XDG_DATA_HOME'] = self._xdg
        self.r.store_reset()
        self.r._TREE_CACHE.clear()
        self.r._TAIL_STATE.clear()
        self.tmp.cleanup()

    def _complete_streaming_row(self):
        rows = {mid: data for sid, mid, _t, _s, _c, _u, data in _v2_rows() if sid == S1}
        grown = rows['msg_05']
        grown['content'][0]['state'] = {
            'status': 'completed', 'input': {'path': '/home/test/projA/b.py'},
            'content': [{'type': 'text', 'text': 'PROBE_B_BODY'}], 'metadata': {}}
        grown['content'].append({'type': 'text', 'text': 'PROBE_ANSWER_TWO b is fine'})
        grown['finish'] = 'stop'
        con = sqlite3.connect(self.db)
        con.execute('UPDATE session_message SET data = ?, time_updated = ? WHERE id = ?',
                    (json.dumps(grown), T0 + 25000, 'msg_05'))
        con.commit()
        con.close()

    def test_tree_mode_changed_record_refreshes_cached_rows(self):
        r = self.r
        r._TREE_MODE = True
        td = r._scan_tree(S1)
        b = _FakeBrowser()
        for parent in (('session', S1), ('prompt', S1, 8), ('tool', S1, 9)):
            b.cache(r, parent)
        old_result = td.tool_children[9][0]
        self.assertEqual(old_result.tool['glance'], '(running)')
        self._complete_streaming_row()
        records, dirty = r._read_new_records(S1)
        self.assertEqual([(n, rec.kind) for n, rec in records],
                         [(11, 'assistant_text'), (9, 'tool_call'), (10, 'tool_result')])
        self.assertEqual(dirty, {('tool', S1, 9), ('prompt', S1, 8)})
        # The structural copies hold the new objects, not the stale ones.
        self.assertIs(td.tool_children[9][0], td.records[10])
        self.assertEqual(td.records[10].tool['output'], 'PROBE_B_BODY')
        self.assertEqual([rec.key for rec in td.turn_direct[8]], [9, 11])
        self.assertIs(td.turn_direct[8][0], td.records[9])
        self.assertEqual(td.latest_voice_line, 11)
        self.assertIsNone(td.voice_count)
        r._push_tail_diffs(b, dirty)
        mods = {op[1]: op[3] for op in b.ops if op[0] == 'mod'}
        ups = {op[1]: op[3] for op in b.ops if op[0] == 'upsert'}
        self.assertEqual(mods[('msg', S1, 10)]['title'], '↳ tool_result: PROBE_B_BODY')
        self.assertEqual(set(mods[('msg', S1, 10)]), {'title'})
        self.assertIn(('drop_preview_cache', ('msg', S1, 10)), b.ops)
        self.assertIn(('msg', S1, 11), ups)
        self.assertEqual(ups[('msg', S1, 11)]['title'], 'assistant: PROBE_ANSWER_TWO b is fine')
        self.assertNotIn(('msg', S1, 9), mods)         # the call row did not change
        # Nothing to push once the framework has caught up.
        for parent in (('prompt', S1, 8), ('tool', S1, 9)):
            b.cache(r, parent)
        b.ops.clear()
        r._push_tail_diffs(b, dirty)
        self.assertEqual(b.ops, [])

    def test_flat_mode_changed_record_gets_a_mod(self):
        r = self.r
        r._TREE_MODE = False
        r._scan_tree(S1)
        b = _FakeBrowser()
        b.cache(r, ('session', S1))
        self._complete_streaming_row()
        records, _dirty = r._read_new_records(S1)
        r._push_flat_inserts(b, S1, records)
        kinds = [(op[0], op[1][2]) for op in b.ops]
        self.assertEqual(kinds, [('upsert', 11), ('mod', 10), ('drop_preview_cache', 10)])
        self.assertEqual(b.ops[1][3], {'title': '↳ tool_result: PROBE_B_BODY'})
        self.assertFalse(b.ops[0][3]['has_children'])

    def test_spawn_result_arriving_after_its_placeholder_links_the_child(self):
        r = self.r
        running = {'time': {'created': T0 + 40000}, 'agent': 'build',
                   'model': {'id': 'glm', 'providerID': 'fw'},
                   'content': [{'type': 'tool', 'id': 'call_task_1', 'name': 'task',
                                'state': {'status': 'running',
                                          'input': {'description': 'run suite', 'prompt': 'go'}},
                                'time': {'created': T0 + 40000}}]}
        con = sqlite3.connect(self.db)
        con.execute('INSERT INTO session_message VALUES (?,?,?,?,?,?,?)',
                    ('msg_07', S1, 'assistant', 7, T0 + 40000, T0 + 40000, json.dumps(running)))
        con.commit()
        td = r._scan_tree(S1)
        self.assertEqual(td.agent_link, {})
        running['content'][0]['state'] = {
            'status': 'completed', 'input': {'description': 'run suite', 'prompt': 'go'},
            'content': [{'type': 'text', 'text': 'child done'}], 'metadata': {'sessionId': S3}}
        con.execute('UPDATE session_message SET data = ?, time_updated = ? WHERE id = ?',
                    (json.dumps(running), T0 + 45000, 'msg_07'))
        con.commit()
        con.close()
        _records, dirty = r._read_new_records(S1)
        self.assertIn(('session', S1), dirty)
        self.assertEqual(td.agent_link['call_task_1']['child_session'], S3)
        self.assertEqual(td.agent_to_assistant_line[S3], 11)

    def test_link_label_without_a_line_number(self):
        r = self.r
        r._scan_tree(S1)
        label = r._link_label(('msg', S1, 0))
        self.assertTrue(label.startswith(f'↗ {S1}  user: PROBE_PROMPT_ONE'), label)
        self.assertNotIn(':None', label)


if __name__ == '__main__':
    unittest.main()
