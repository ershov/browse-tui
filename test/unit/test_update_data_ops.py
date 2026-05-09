"""Tests for ``apply_ops`` and the module-level op-helper constructors.

Pins the contract documented in
``docs/superpowers/specs/2026-05-08-streaming-push-api-design.md``,
Section 2 — the six tuple-op apply paths plus their behaviour around
edge cases (orphan parents, reparenting, patch-only upsert, unknown-id
silent drop, cascade on remove, set-fields-revert-to-defaults,
incomplete-after-complete sticky-flip, batch ordering, structural-dirty
propagation).

This ticket (#268) introduces only the pure state-mutation layer —
``Browser.update_data`` itself lands in ticket #269. So the tests exercise
``apply_ops(state, ops)`` directly against a hand-built ``State``.
"""

import unittest

from test.unit._loader import load

_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')

# Inject names that the concatenated production build provides naturally
# but the standalone loader doesn't see.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

State = _state.State
Item = _data.Item
apply_ops = _state.apply_ops
upsert = _state.upsert
set_item = _state.set_item
remove = _state.remove
clear_children = _state.clear_children
complete = _state.complete
incomplete = _state.incomplete


class TestHelperConstructors(unittest.TestCase):
    """Helpers return well-formed tagged-tuple op shapes."""

    def test_upsert_shape(self):
        op = upsert('a', 'p', title='A', has_children=True, custom='x')
        self.assertEqual(op[0], 'upsert')
        self.assertEqual(op[1], 'a')
        self.assertEqual(op[2], 'p')
        self.assertEqual(
            op[3], {'title': 'A', 'has_children': True, 'custom': 'x'},
        )

    def test_set_item_shape(self):
        op = set_item('a', 'p', title='A')
        self.assertEqual(op, ('set', 'a', 'p', {'title': 'A'}))

    def test_remove_shape(self):
        self.assertEqual(remove('a'), ('remove', 'a'))

    def test_clear_children_shape(self):
        self.assertEqual(clear_children('p'), ('clear_children', 'p'))

    def test_complete_shape(self):
        self.assertEqual(complete('p'), ('complete', 'p'))

    def test_incomplete_shape(self):
        self.assertEqual(incomplete('p'), ('incomplete', 'p'))

    def test_upsert_no_fields(self):
        # No kwargs — the fields dict should be empty, not missing.
        op = upsert('a', 'p')
        self.assertEqual(op, ('upsert', 'a', 'p', {}))

    def test_upsert_parent_can_be_none(self):
        # patch-only upsert constructs the same shape, parent=None.
        op = upsert('a', None, title='A')
        self.assertEqual(op, ('upsert', 'a', None, {'title': 'A'}))


class TestUpsertNew(unittest.TestCase):
    """``upsert`` of a new id inserts under the named parent."""

    def test_inserts_under_parent(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        self.assertEqual([c.id for c in s._children['/']], ['a'])
        self.assertEqual(s._items_by_id['a'].title, 'A')
        self.assertEqual(s._parent_of_id['a'], '/')

    def test_appends_in_order(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', title='A'),
            upsert('b', '/', title='B'),
            upsert('c', '/', title='C'),
        ])
        self.assertEqual([c.id for c in s._children['/']], ['a', 'b', 'c'])

    def test_orphan_parent_creates_cache_entry(self):
        # Parent id 'unknown' is not yet a known item — orphan upsert is
        # allowed; cache entry is created on demand.
        s = State(root_id='/')
        apply_ops(s, [upsert('x', 'unknown', title='X')])
        self.assertIn('unknown', s._children)
        self.assertEqual(s._children['unknown'][0].id, 'x')
        self.assertEqual(s._parent_of_id['x'], 'unknown')

    def test_custom_attrs_attached_on_insert(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A', size=42, path='/tmp/a')])
        item = s._items_by_id['a']
        self.assertEqual(item.title, 'A')
        self.assertEqual(item.size, 42)
        self.assertEqual(item.path, '/tmp/a')

    def test_default_title_falls_back_to_id(self):
        # Item.__post_init__ defaults title to str(id) when not given.
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/')])
        self.assertEqual(s._items_by_id['a'].title, 'a')


class TestUpsertExisting(unittest.TestCase):
    """``upsert`` of an existing id patch-merges."""

    def _seed(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A', has_children=False)])
        return s

    def test_patches_in_place(self):
        s = self._seed()
        before = s._items_by_id['a']
        apply_ops(s, [upsert('a', '/', title='A2')])
        after = s._items_by_id['a']
        # Same instance — mutation in place.
        self.assertIs(before, after)
        self.assertEqual(after.title, 'A2')

    def test_unspecified_fields_preserved(self):
        s = self._seed()
        apply_ops(s, [upsert('a', None, has_children=True)])
        item = s._items_by_id['a']
        self.assertEqual(item.title, 'A')          # untouched
        self.assertTrue(item.has_children)          # patched

    def test_custom_attrs_added(self):
        s = self._seed()
        apply_ops(s, [upsert('a', None, size=100)])
        self.assertEqual(s._items_by_id['a'].size, 100)

    def test_custom_attrs_preserved_across_patch(self):
        s = self._seed()
        apply_ops(s, [upsert('a', None, size=100)])
        apply_ops(s, [upsert('a', None, title='A3')])
        item = s._items_by_id['a']
        self.assertEqual(item.title, 'A3')
        self.assertEqual(item.size, 100)            # custom attr survived

    def test_has_children_flag_changes(self):
        s = self._seed()
        self.assertFalse(s._items_by_id['a'].has_children)
        apply_ops(s, [upsert('a', None, has_children=True)])
        self.assertTrue(s._items_by_id['a'].has_children)


class TestUpsertReparent(unittest.TestCase):
    """``upsert`` with a different parent moves the item."""

    def test_reparent_moves_between_lists(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('p1', '/', has_children=True),
            upsert('p2', '/', has_children=True),
            upsert('a', 'p1', title='A'),
        ])
        self.assertEqual([c.id for c in s._children['p1']], ['a'])
        self.assertEqual(s._children.get('p2', []), [])

        apply_ops(s, [upsert('a', 'p2')])
        self.assertEqual(s._children['p1'], [])
        self.assertEqual([c.id for c in s._children['p2']], ['a'])
        self.assertEqual(s._parent_of_id['a'], 'p2')

    def test_reparent_preserves_item_identity(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('p1', '/'), upsert('p2', '/'),
            upsert('a', 'p1', title='A', size=99),
        ])
        before = s._items_by_id['a']
        apply_ops(s, [upsert('a', 'p2', title='A-moved')])
        after = s._items_by_id['a']
        self.assertIs(before, after)
        self.assertEqual(after.title, 'A-moved')
        self.assertEqual(after.size, 99)            # custom attr survived


class TestUpsertPatchOnly(unittest.TestCase):
    """``upsert`` with ``parent_id=None`` is patch-only."""

    def test_patch_only_does_not_move(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('p1', '/'),
            upsert('a', 'p1', title='A'),
        ])
        apply_ops(s, [upsert('a', None, title='A2')])
        self.assertEqual([c.id for c in s._children['p1']], ['a'])
        self.assertEqual(s._parent_of_id['a'], 'p1')
        self.assertEqual(s._items_by_id['a'].title, 'A2')

    def test_patch_only_unknown_id_silent_drop(self):
        s = State(root_id='/')
        before_items = dict(s._items_by_id)
        before_parents = dict(s._parent_of_id)
        before_children = {k: list(v) for k, v in s._children.items()}
        apply_ops(s, [upsert('ghost', None, title='nope')])
        # Silent drop — no entries appear, no exception.
        self.assertEqual(s._items_by_id, before_items)
        self.assertEqual(s._parent_of_id, before_parents)
        self.assertEqual(
            {k: list(v) for k, v in s._children.items()},
            before_children,
        )


class TestSet(unittest.TestCase):
    """``set`` is a full replace — new instance, defaults reverted."""

    def test_replaces_all_fields_to_defaults(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', title='A', tag='[t]', has_children=True),
        ])
        # Patch in a custom attr too — it must NOT survive a `set`.
        apply_ops(s, [upsert('a', None, custom='x')])
        self.assertEqual(s._items_by_id['a'].custom, 'x')

        apply_ops(s, [set_item('a', '/', title='A2')])
        item = s._items_by_id['a']
        # Specified field overrides; unspecified Item fields revert to
        # dataclass defaults.
        self.assertEqual(item.title, 'A2')
        self.assertEqual(item.tag, '')
        self.assertEqual(item.tag_style, '')
        self.assertFalse(item.has_children)
        # Custom attrs are dropped on set.
        self.assertFalse(hasattr(item, 'custom'))

    def test_set_constructs_new_instance(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        before = s._items_by_id['a']
        apply_ops(s, [set_item('a', '/', title='A2')])
        after = s._items_by_id['a']
        self.assertIsNot(before, after)

    def test_set_preserves_children_under_id(self):
        # _children[id] is the children OF id (as a parent), separate
        # from the Item instance. ``set`` replaces the instance but
        # leaves the parent's child list intact.
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', has_children=True),
            upsert('a/x', 'a'),
        ])
        before_kids = list(s._children['a'])
        apply_ops(s, [set_item('a', '/', title='A2', has_children=True)])
        self.assertEqual(s._children['a'], before_kids)

    def test_set_inserts_when_unknown(self):
        # Spec: ``set`` is insert-or-replace. Unknown id under a known
        # parent should insert.
        s = State(root_id='/')
        apply_ops(s, [set_item('a', '/', title='A')])
        self.assertEqual([c.id for c in s._children['/']], ['a'])
        self.assertEqual(s._items_by_id['a'].title, 'A')

    def test_set_reparent(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('p1', '/'), upsert('p2', '/'),
            upsert('a', 'p1', title='A'),
        ])
        apply_ops(s, [set_item('a', 'p2', title='A2')])
        self.assertEqual(s._children['p1'], [])
        self.assertEqual([c.id for c in s._children['p2']], ['a'])
        self.assertEqual(s._parent_of_id['a'], 'p2')


class TestRemove(unittest.TestCase):
    """``remove`` deletes the item and cascades into its subtree."""

    def test_removes_from_parent_list(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/'), upsert('b', '/'), upsert('c', '/'),
        ])
        apply_ops(s, [remove('b')])
        self.assertEqual([c.id for c in s._children['/']], ['a', 'c'])

    def test_drops_indexes(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/')])
        apply_ops(s, [remove('a')])
        self.assertNotIn('a', s._items_by_id)
        self.assertNotIn('a', s._parent_of_id)

    def test_cascade_drops_subtree(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', has_children=True),
            upsert('a/x', 'a'),
            upsert('a/y', 'a'),
            upsert('a/x/i', 'a/x'),
        ])
        apply_ops(s, [remove('a')])
        # The whole subtree is gone from indexes.
        for cid in ('a', 'a/x', 'a/y', 'a/x/i'):
            self.assertNotIn(cid, s._items_by_id)
            self.assertNotIn(cid, s._parent_of_id)
        # And the children-of-id cache entries are gone.
        self.assertNotIn('a', s._children)
        self.assertNotIn('a/x', s._children)

    def test_cascade_drops_loading_for_subtree(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', has_children=True),
            upsert('a/x', 'a'),
        ])
        s._loading['a'] = True
        s._loading['a/x'] = True
        apply_ops(s, [remove('a')])
        self.assertNotIn('a', s._loading)
        self.assertNotIn('a/x', s._loading)

    def test_unknown_id_no_op(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/')])
        before_items = dict(s._items_by_id)
        before_children = {k: list(v) for k, v in s._children.items()}
        apply_ops(s, [remove('ghost')])
        self.assertEqual(s._items_by_id, before_items)
        self.assertEqual(
            {k: list(v) for k, v in s._children.items()},
            before_children,
        )


class TestClearChildren(unittest.TestCase):
    """``clear_children`` resets the parent's child list and cache entry."""

    def test_empties_parents_children(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('p', '/', has_children=True),
            upsert('a', 'p'), upsert('b', 'p'),
        ])
        apply_ops(s, [clear_children('p')])
        # Entry reverts to "no fetch yet" (dict entry removed).
        self.assertNotIn('p', s._children)

    def test_drops_indexes_for_cleared_children(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('p', '/', has_children=True),
            upsert('a', 'p'), upsert('b', 'p'),
        ])
        apply_ops(s, [clear_children('p')])
        self.assertNotIn('a', s._items_by_id)
        self.assertNotIn('b', s._items_by_id)
        self.assertNotIn('a', s._parent_of_id)
        self.assertNotIn('b', s._parent_of_id)

    def test_recursive_descendant_cleanup(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('p', '/', has_children=True),
            upsert('a', 'p', has_children=True),
            upsert('a/x', 'a'),
        ])
        apply_ops(s, [clear_children('p')])
        # 'a/x' is a grandchild of 'p' — cascade through 'a'.
        self.assertNotIn('a', s._items_by_id)
        self.assertNotIn('a/x', s._items_by_id)
        self.assertNotIn('a', s._children)

    def test_resets_loading_to_false(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('p', '/'), upsert('a', 'p'),
        ])
        s._loading['p'] = True
        apply_ops(s, [clear_children('p')])
        # Spec: loading flag is reset accordingly. We chose False
        # (the parent is in a known not-loading state; future fetch
        # flips it to True via dispatch).
        self.assertEqual(s._loading['p'], False)

    def test_unknown_parent_sets_loading_false(self):
        # ``clear_children`` on a never-cached parent is a no-op for
        # ``_children`` / indexes, but still seeds ``_loading[p] = False``
        # so the addressable flag exists for downstream consumers.
        s = State(root_id='/')
        apply_ops(s, [clear_children('ghost')])
        self.assertEqual(s._loading.get('ghost'), False)


class TestCompleteIncomplete(unittest.TestCase):
    """``complete`` / ``incomplete`` flip ``_loading[parent]`` directly."""

    def test_complete_clears_loading(self):
        s = State(root_id='/')
        s._loading['p'] = True
        apply_ops(s, [complete('p')])
        self.assertEqual(s._loading['p'], False)

    def test_incomplete_sets_loading(self):
        s = State(root_id='/')
        apply_ops(s, [incomplete('p')])
        self.assertEqual(s._loading['p'], True)

    def test_incomplete_after_complete_is_sticky_flip(self):
        # Spec: "complete followed by an upsert into the same parent
        # silently flips the parent back to incomplete; framework does
        # not try to outsmart the recipe." We honour the same rule for
        # explicit incomplete-after-complete: the recipe is in charge.
        s = State(root_id='/')
        apply_ops(s, [complete('p')])
        self.assertFalse(s._loading['p'])
        apply_ops(s, [incomplete('p')])
        self.assertTrue(s._loading['p'])
        apply_ops(s, [complete('p')])
        self.assertFalse(s._loading['p'])

    def test_complete_does_not_touch_children(self):
        # Loading flips are pure flag mutations; they don't disturb the
        # cached child lists or item indexes.
        s = State(root_id='/')
        apply_ops(s, [
            upsert('p', '/'), upsert('a', 'p'),
        ])
        before_children = {k: list(v) for k, v in s._children.items()}
        apply_ops(s, [complete('p')])
        self.assertEqual(
            {k: list(v) for k, v in s._children.items()},
            before_children,
        )


class TestBatchOrdering(unittest.TestCase):
    """Ops apply in list order. Within-batch atomicity is the contract."""

    def test_upsert_then_remove_ends_with_no_item(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', title='A'),
            upsert('a', '/2', title='A-moved'),
            remove('a'),
        ])
        self.assertNotIn('a', s._items_by_id)
        self.assertNotIn('a', s._parent_of_id)
        # Both parent lists must end empty (no stale entry).
        self.assertEqual(s._children.get('/', []), [])
        self.assertEqual(s._children.get('/2', []), [])

    def test_reparent_visible_to_subsequent_op(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('p1', '/'), upsert('p2', '/'),
            upsert('a', 'p1'),
            # In-batch reparent then patch-only — the patch sees 'a'
            # under 'p2' already.
            upsert('a', 'p2', title='moved'),
            upsert('a', None, size=42),
        ])
        self.assertEqual(s._parent_of_id['a'], 'p2')
        self.assertEqual(s._items_by_id['a'].size, 42)
        self.assertEqual(s._items_by_id['a'].title, 'moved')

    def test_set_after_upsert_drops_custom_attrs(self):
        # Within the same batch, set_item drops everything upsert
        # attached.
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', title='A', size=100),
            set_item('a', '/', title='A2'),
        ])
        item = s._items_by_id['a']
        self.assertEqual(item.title, 'A2')
        self.assertFalse(hasattr(item, 'size'))


class TestVisibleDirty(unittest.TestCase):
    """Structural ops flip ``_visible_dirty``; flag-only ops do not."""

    def test_upsert_marks_dirty(self):
        s = State(root_id='/')
        s._visible_dirty = False
        apply_ops(s, [upsert('a', '/', title='A')])
        self.assertTrue(s._visible_dirty)

    def test_set_marks_dirty(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        s._visible_dirty = False
        apply_ops(s, [set_item('a', '/', title='A2')])
        self.assertTrue(s._visible_dirty)

    def test_remove_marks_dirty(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        s._visible_dirty = False
        apply_ops(s, [remove('a')])
        self.assertTrue(s._visible_dirty)

    def test_clear_children_marks_dirty(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('p', '/'), upsert('a', 'p'),
        ])
        s._visible_dirty = False
        apply_ops(s, [clear_children('p')])
        self.assertTrue(s._visible_dirty)

    def test_complete_does_not_mark_dirty(self):
        s = State(root_id='/')
        s._visible_dirty = False
        apply_ops(s, [complete('p')])
        self.assertFalse(s._visible_dirty)

    def test_incomplete_does_not_mark_dirty(self):
        s = State(root_id='/')
        s._visible_dirty = False
        apply_ops(s, [incomplete('p')])
        self.assertFalse(s._visible_dirty)

    def test_silent_drop_does_not_mark_dirty(self):
        # patch-only upsert against unknown id: no-op, no dirty flip.
        s = State(root_id='/')
        s._visible_dirty = False
        apply_ops(s, [upsert('ghost', None, title='nope')])
        self.assertFalse(s._visible_dirty)

    def test_unknown_remove_does_not_mark_dirty(self):
        s = State(root_id='/')
        s._visible_dirty = False
        apply_ops(s, [remove('ghost')])
        self.assertFalse(s._visible_dirty)


class TestUnknownOp(unittest.TestCase):
    """Unknown op kinds raise instead of silently dropping."""

    def test_unknown_op_raises(self):
        s = State(root_id='/')
        with self.assertRaises(ValueError):
            apply_ops(s, [('not-an-op', 'a')])


if __name__ == '__main__':
    unittest.main()
