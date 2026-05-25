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
mod = _state.mod
KEEP_PARENT = _state.KEEP_PARENT
remove = _state.remove
clear_children = _state.clear_children
complete = _state.complete
incomplete = _state.incomplete
set_preview_op = _state.set_preview_op
append_preview_op = _state.append_preview_op
clear_preview_op = _state.clear_preview_op
invalidate_preview_op = _state.invalidate_preview_op
drop_preview_cache_op = _state.drop_preview_cache_op


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


class TestUpsertPreviewCacheGate(unittest.TestCase):
    """``upsert`` invalidates the preview cache only when fields change (#445)."""

    def _seed(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        item = s._items_by_id['a']
        item.preview = 'cached preview text'
        item.preview_render = 'cached render sentinel'
        return s, item

    def test_no_fields_preserves_preview_cache(self):
        # The documented idempotent-ensure idiom used before set_preview:
        #   update_data([upsert(id, parent)]) + set_preview(id, text)
        # must not nuke a cached preview.
        s, item = self._seed()
        apply_ops(s, [upsert('a', '/')])
        self.assertEqual(item.preview, 'cached preview text')
        self.assertEqual(item.preview_render, 'cached render sentinel')

    def test_no_fields_patch_only_preserves_preview_cache(self):
        # parent_id=None (patch-only) with no fields — same gate.
        s, item = self._seed()
        apply_ops(s, [upsert('a', None)])
        self.assertEqual(item.preview, 'cached preview text')
        self.assertEqual(item.preview_render, 'cached render sentinel')

    def test_field_mutation_drops_preview_cache(self):
        # Existing behaviour preserved: a real patch invalidates the
        # cache (the displayed body may depend on the patched field).
        s, item = self._seed()
        apply_ops(s, [upsert('a', None, title='A2')])
        self.assertIsNone(item.preview)
        self.assertIsNone(item.preview_render)

    def test_custom_attr_patch_drops_preview_cache(self):
        # Custom attrs count as field changes too (recipes that compose
        # previews from custom attrs need the invalidation).
        s, item = self._seed()
        apply_ops(s, [upsert('a', None, size=42)])
        self.assertIsNone(item.preview)
        self.assertIsNone(item.preview_render)


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


# ---- Positioning (`where` descriptor) ---------------------------------------


def _ids(s, parent):
    """Return the list of child ids under ``parent`` for assertions."""
    return [c.id for c in s._children.get(parent, [])]


def _seed_parent(*ids):
    """Build a state with the given ids appended under '/'."""
    s = State(root_id='/')
    apply_ops(s, [upsert(i, '/', title=i.upper()) for i in ids])
    return s


class TestHelperWhere(unittest.TestCase):
    """Helper constructors honour ``where=`` kwarg."""

    def test_upsert_without_where_legacy_4tuple(self):
        op = upsert('a', 'p', title='A')
        self.assertEqual(len(op), 4)
        self.assertEqual(op[0], 'upsert')

    def test_upsert_with_where_5tuple(self):
        op = upsert('a', 'p', where=('first', None), title='A')
        self.assertEqual(len(op), 5)
        self.assertEqual(op[4], ('first', None))

    def test_set_item_without_where_legacy(self):
        op = set_item('a', 'p', title='A')
        self.assertEqual(len(op), 4)

    def test_set_item_with_where_5tuple(self):
        op = set_item('a', 'p', where=('last', None), title='A')
        self.assertEqual(len(op), 5)
        self.assertEqual(op[4], ('last', None))

    def test_where_only_keyword_only(self):
        # Cannot pass ``where`` positionally; must be keyword.
        with self.assertRaises(TypeError):
            upsert('a', 'p', ('first', None))  # type: ignore


class TestPositioningFirst(unittest.TestCase):
    """``where=("first", ...)`` inserts at index 0."""

    def test_first_on_nonempty(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [upsert('x', '/', where=('first', None), title='X')])
        self.assertEqual(_ids(s, '/'), ['x', 'a', 'b', 'c'])

    def test_first_on_empty(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('x', '/', where=('first', None), title='X')])
        self.assertEqual(_ids(s, '/'), ['x'])

    def test_first_via_set_item(self):
        s = _seed_parent('a', 'b')
        apply_ops(s, [set_item('x', '/', where=('first', None), title='X')])
        self.assertEqual(_ids(s, '/'), ['x', 'a', 'b'])

    def test_first_with_3tuple_silently_drops_reference(self):
        # "first" with a length-3 tuple — reference slot is ignored.
        s = _seed_parent('a', 'b')
        apply_ops(s, [
            upsert('x', '/', where=('first', None, 'ignored'), title='X'),
        ])
        self.assertEqual(_ids(s, '/'), ['x', 'a', 'b'])


class TestPositioningLast(unittest.TestCase):
    """``where=("last", ...)`` inserts at end (same as default)."""

    def test_last_on_nonempty(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [upsert('x', '/', where=('last', None), title='X')])
        self.assertEqual(_ids(s, '/'), ['a', 'b', 'c', 'x'])

    def test_last_on_empty(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('x', '/', where=('last', None), title='X')])
        self.assertEqual(_ids(s, '/'), ['x'])


class TestPositioningBeforeAfterById(unittest.TestCase):
    """``where=("before"/"after", None, str_id)`` resolves to pivot's index."""

    def test_before_existing_id(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [upsert('x', '/', where=('before', None, 'b'), title='X')])
        self.assertEqual(_ids(s, '/'), ['a', 'x', 'b', 'c'])

    def test_after_existing_id(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [upsert('x', '/', where=('after', None, 'b'), title='X')])
        self.assertEqual(_ids(s, '/'), ['a', 'b', 'x', 'c'])

    def test_before_first(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [upsert('x', '/', where=('before', None, 'a'), title='X')])
        self.assertEqual(_ids(s, '/'), ['x', 'a', 'b', 'c'])

    def test_after_last(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [upsert('x', '/', where=('after', None, 'c'), title='X')])
        self.assertEqual(_ids(s, '/'), ['a', 'b', 'c', 'x'])


class TestPositioningBeforeAfterByIndex(unittest.TestCase):
    """``where=("before"/"after", None, int_idx)`` uses index lookup."""

    def test_before_index_0(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [upsert('x', '/', where=('before', None, 0), title='X')])
        self.assertEqual(_ids(s, '/'), ['x', 'a', 'b', 'c'])

    def test_after_index_0(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [upsert('x', '/', where=('after', None, 0), title='X')])
        self.assertEqual(_ids(s, '/'), ['a', 'x', 'b', 'c'])

    def test_before_middle_index(self):
        s = _seed_parent('a', 'b', 'c', 'd')
        apply_ops(s, [upsert('x', '/', where=('before', None, 2), title='X')])
        self.assertEqual(_ids(s, '/'), ['a', 'b', 'x', 'c', 'd'])

    def test_after_middle_index(self):
        s = _seed_parent('a', 'b', 'c', 'd')
        apply_ops(s, [upsert('x', '/', where=('after', None, 2), title='X')])
        self.assertEqual(_ids(s, '/'), ['a', 'b', 'c', 'x', 'd'])


class TestPositioningClampAndFallback(unittest.TestCase):
    """Out-of-range index and missing pivot collapse to nearest edge."""

    def test_before_negative_index_first(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [upsert('x', '/', where=('before', None, -1), title='X')])
        self.assertEqual(_ids(s, '/'), ['x', 'a', 'b', 'c'])

    def test_after_negative_index_also_first(self):
        # Asymmetric clamp: out-of-range goes to nearest edge regardless
        # of direction. -1 collapses to "first" even with "after".
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [upsert('x', '/', where=('after', None, -1), title='X')])
        self.assertEqual(_ids(s, '/'), ['x', 'a', 'b', 'c'])

    def test_before_too_big_index_last(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [upsert('x', '/', where=('before', None, 999), title='X')])
        self.assertEqual(_ids(s, '/'), ['a', 'b', 'c', 'x'])

    def test_after_too_big_index_last(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [upsert('x', '/', where=('after', None, 999), title='X')])
        self.assertEqual(_ids(s, '/'), ['a', 'b', 'c', 'x'])

    def test_before_missing_id_first(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [
            upsert('x', '/', where=('before', None, 'nope'), title='X'),
        ])
        self.assertEqual(_ids(s, '/'), ['x', 'a', 'b', 'c'])

    def test_after_missing_id_last(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [
            upsert('x', '/', where=('after', None, 'nope'), title='X'),
        ])
        self.assertEqual(_ids(s, '/'), ['a', 'b', 'c', 'x'])

    def test_before_on_empty_with_missing_id(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('x', '/', where=('before', None, 'nope'), title='X'),
        ])
        self.assertEqual(_ids(s, '/'), ['x'])

    def test_after_on_empty_with_int(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('x', '/', where=('after', None, 5), title='X')])
        self.assertEqual(_ids(s, '/'), ['x'])


class TestRepositionFlag(unittest.TestCase):
    """Existing ids only move when the ``"reposition"`` flag is set."""

    def test_existing_without_flag_keeps_position(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [
            # ``b`` already at index 1; this should NOT move it.
            upsert('b', '/', where=('first', None), title='B-updated'),
        ])
        self.assertEqual(_ids(s, '/'), ['a', 'b', 'c'])
        self.assertEqual(s._items_by_id['b'].title, 'B-updated')

    def test_reposition_to_first(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [
            upsert('c', '/',
                   where=('first', frozenset({'reposition'})),
                   title='C-moved'),
        ])
        self.assertEqual(_ids(s, '/'), ['c', 'a', 'b'])
        self.assertEqual(s._items_by_id['c'].title, 'C-moved')

    def test_reposition_to_last(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [
            upsert('a', '/',
                   where=('last', frozenset({'reposition'})),
                   title='A-moved'),
        ])
        self.assertEqual(_ids(s, '/'), ['b', 'c', 'a'])

    def test_reposition_before_id(self):
        s = _seed_parent('a', 'b', 'c', 'd')
        apply_ops(s, [
            upsert('a', '/',
                   where=('before', frozenset({'reposition'}), 'd'),
                   title='A'),
        ])
        self.assertEqual(_ids(s, '/'), ['b', 'c', 'a', 'd'])

    def test_reposition_after_id(self):
        s = _seed_parent('a', 'b', 'c', 'd')
        apply_ops(s, [
            upsert('a', '/',
                   where=('after', frozenset({'reposition'}), 'd'),
                   title='A'),
        ])
        self.assertEqual(_ids(s, '/'), ['b', 'c', 'd', 'a'])

    def test_reposition_via_set_item(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [
            set_item('c', '/',
                     where=('first', frozenset({'reposition'})),
                     title='C'),
        ])
        self.assertEqual(_ids(s, '/'), ['c', 'a', 'b'])

    def test_reposition_same_id_pivot_is_noop_by_str(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [
            upsert('b', '/',
                   where=('before', frozenset({'reposition'}), 'b'),
                   title='B'),
        ])
        # Same-id pivot — position unchanged, but fields still patched.
        self.assertEqual(_ids(s, '/'), ['a', 'b', 'c'])

    def test_reposition_same_id_pivot_is_noop_by_int(self):
        s = _seed_parent('a', 'b', 'c')
        # ``b`` is at index 1; pointing at index 1 is the same-id case.
        apply_ops(s, [
            upsert('b', '/',
                   where=('after', frozenset({'reposition'}), 1),
                   title='B'),
        ])
        self.assertEqual(_ids(s, '/'), ['a', 'b', 'c'])

    def test_reposition_to_already_correct_position_is_noop(self):
        # "before c" where b is already before c → no movement.
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [
            upsert('b', '/',
                   where=('before', frozenset({'reposition'}), 'c'),
                   title='B'),
        ])
        self.assertEqual(_ids(s, '/'), ['a', 'b', 'c'])

    def test_reposition_adjusts_for_self_removal(self):
        # Move ``b`` from index 1 to "after d" (index 3 originally) →
        # post-removal target is index 2 → final: [a, c, d, b].
        s = _seed_parent('a', 'b', 'c', 'd')
        apply_ops(s, [
            upsert('b', '/',
                   where=('after', frozenset({'reposition'}), 'd'),
                   title='B'),
        ])
        self.assertEqual(_ids(s, '/'), ['a', 'c', 'd', 'b'])


class TestPositioningSameIdNewItem(unittest.TestCase):
    """Same-id pivot for a NEW id falls back to first/last (id not present)."""

    def test_new_id_before_self_first(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [
            upsert('x', '/', where=('before', None, 'x'), title='X'),
        ])
        self.assertEqual(_ids(s, '/'), ['x', 'a', 'b', 'c'])

    def test_new_id_after_self_last(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [
            upsert('x', '/', where=('after', None, 'x'), title='X'),
        ])
        self.assertEqual(_ids(s, '/'), ['a', 'b', 'c', 'x'])


class TestPositioningBatchOrder(unittest.TestCase):
    """Pivot resolution sees only ids already present when the op runs."""

    def test_pivot_in_later_op_treated_as_missing(self):
        # A references B; B is added later in the same batch. A's pivot
        # is missing at the time it's processed → collapses to "first".
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', where=('before', None, 'b'), title='A'),
            upsert('b', '/', title='B'),
        ])
        # A inserts as the first child of empty list, then B appends.
        self.assertEqual(_ids(s, '/'), ['a', 'b'])

    def test_pivot_in_earlier_op_resolves(self):
        # B references A; A is already present when B is processed.
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', title='A'),
            upsert('b', '/', where=('before', None, 'a'), title='B'),
        ])
        self.assertEqual(_ids(s, '/'), ['b', 'a'])

    def test_reposition_within_same_batch(self):
        s = _seed_parent('a', 'b', 'c')
        apply_ops(s, [
            upsert('x', '/', where=('after', None, 'a'), title='X'),
            upsert('x', '/',
                   where=('last', frozenset({'reposition'})),
                   title='X'),
        ])
        # First op inserts x after a: [a, x, b, c].
        # Second op repositions x to the end: [a, b, c, x].
        self.assertEqual(_ids(s, '/'), ['a', 'b', 'c', 'x'])


class TestPositioningReparent(unittest.TestCase):
    """``where`` applies in the new parent when reparenting."""

    def test_reparent_with_where_first(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', title='A'),
            upsert('b', 'a', title='B-child-1'),
            upsert('c', 'a', title='C-child-2'),
            upsert('d', '/', title='D'),
        ])
        # Reparent ``d`` under ``a`` at the start of a's children.
        apply_ops(s, [upsert('d', 'a', where=('first', None), title='D')])
        self.assertEqual(_ids(s, 'a'), ['d', 'b', 'c'])
        self.assertEqual(_ids(s, '/'), ['a'])

    def test_reparent_with_where_before_id(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', title='A'),
            upsert('b', 'a', title='B'),
            upsert('c', 'a', title='C'),
            upsert('d', '/', title='D'),
        ])
        apply_ops(s, [
            upsert('d', 'a', where=('before', None, 'c'), title='D'),
        ])
        self.assertEqual(_ids(s, 'a'), ['b', 'd', 'c'])

    def test_reparent_set_item_with_where(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', title='A'),
            upsert('b', 'a', title='B'),
            upsert('d', '/', title='D'),
        ])
        apply_ops(s, [set_item('d', 'a', where=('first', None), title='D')])
        self.assertEqual(_ids(s, 'a'), ['d', 'b'])


class TestPositioningValidation(unittest.TestCase):
    """Malformed ``where`` descriptors raise ``ValueError``."""

    def test_not_a_tuple(self):
        s = State(root_id='/')
        with self.assertRaises(ValueError):
            apply_ops(s, [
                ('upsert', 'x', '/', {'title': 'X'}, 'first'),
            ])

    def test_wrong_length(self):
        s = State(root_id='/')
        with self.assertRaises(ValueError):
            apply_ops(s, [
                ('upsert', 'x', '/', {'title': 'X'}, ('first',)),
            ])
        with self.assertRaises(ValueError):
            apply_ops(s, [
                ('upsert', 'x', '/', {'title': 'X'},
                 ('before', None, 'a', 'extra')),
            ])

    def test_unknown_keyword(self):
        s = State(root_id='/')
        with self.assertRaises(ValueError):
            apply_ops(s, [
                ('upsert', 'x', '/', {'title': 'X'}, ('around', None, 'a')),
            ])

    def test_options_wrong_type(self):
        s = State(root_id='/')
        with self.assertRaises(ValueError):
            apply_ops(s, [
                ('upsert', 'x', '/', {'title': 'X'},
                 ('first', 'reposition')),
            ])

    def test_unknown_option(self):
        s = State(root_id='/')
        with self.assertRaises(ValueError):
            apply_ops(s, [
                ('upsert', 'x', '/', {'title': 'X'},
                 ('first', frozenset({'force'}))),
            ])

    def test_before_without_reference(self):
        s = State(root_id='/')
        with self.assertRaises(ValueError):
            apply_ops(s, [
                ('upsert', 'x', '/', {'title': 'X'}, ('before', None)),
            ])

    def test_after_without_reference(self):
        s = State(root_id='/')
        with self.assertRaises(ValueError):
            apply_ops(s, [
                ('upsert', 'x', '/', {'title': 'X'}, ('after', None)),
            ])

    def test_reference_wrong_type(self):
        s = State(root_id='/')
        with self.assertRaises(ValueError):
            apply_ops(s, [
                ('upsert', 'x', '/', {'title': 'X'},
                 ('before', None, 3.14)),
            ])

    def test_reference_none_for_before_after(self):
        # None is not a valid reference (we use "first"/"last" instead).
        s = State(root_id='/')
        with self.assertRaises(ValueError):
            apply_ops(s, [
                ('upsert', 'x', '/', {'title': 'X'},
                 ('before', None, None)),
            ])


class TestPositioningStructuralDirty(unittest.TestCase):
    """Positioning changes flip ``_visible_dirty`` like other mutations."""

    def test_new_insert_with_where_marks_dirty(self):
        s = _seed_parent('a', 'b')
        s._visible_dirty = False
        apply_ops(s, [upsert('x', '/', where=('first', None), title='X')])
        self.assertTrue(s._visible_dirty)

    def test_reposition_marks_dirty(self):
        s = _seed_parent('a', 'b', 'c')
        s._visible_dirty = False
        apply_ops(s, [
            upsert('a', '/',
                   where=('last', frozenset({'reposition'})),
                   title='A'),
        ])
        self.assertTrue(s._visible_dirty)


# ---- `mod` op + KEEP_PARENT sentinel ---------------------------------------


class TestKeepParentSentinel(unittest.TestCase):
    """The ``KEEP_PARENT`` module-level sentinel."""

    def test_repr(self):
        self.assertEqual(repr(KEEP_PARENT), 'KEEP_PARENT')

    def test_distinct_from_none(self):
        self.assertIsNot(KEEP_PARENT, None)

    def test_distinct_from_string(self):
        self.assertNotEqual(KEEP_PARENT, 'KEEP_PARENT')

    def test_singleton_identity(self):
        # ``KEEP_PARENT`` is a module-level singleton; recipes use
        # identity (``is``) to detect it.
        from test.unit._loader import load as _load
        other = _load('_browse_tui_state_again', '040-state.py').KEEP_PARENT
        # Loaded again into a different module — not the same instance,
        # but the doc API only guarantees one importable name. So we
        # just assert the in-process sentinel is consistent with itself.
        self.assertIs(KEEP_PARENT, _state.KEEP_PARENT)
        # Different module load creates a different sentinel; that's
        # expected for the loader trick but irrelevant for production.
        del other


class TestModHelper(unittest.TestCase):
    """``mod()`` constructor shapes."""

    def test_default_parent_is_keep_parent(self):
        op = mod('a', hidden=True)
        self.assertEqual(op[0], 'mod')
        self.assertEqual(op[1], 'a')
        self.assertIs(op[2], KEEP_PARENT)
        self.assertEqual(op[3], {'hidden': True})

    def test_explicit_parent_id(self):
        op = mod('a', 'new_parent', hidden=False)
        self.assertEqual(op[2], 'new_parent')

    def test_explicit_none_parent(self):
        # ``None`` means root (or explicit None-parent) — not the
        # "don't touch" sentinel.
        op = mod('a', None, hidden=False)
        self.assertIsNone(op[2])

    def test_keep_parent_explicit(self):
        op = mod('a', KEEP_PARENT, hidden=True)
        self.assertIs(op[2], KEEP_PARENT)

    def test_with_where_5tuple(self):
        op = mod('a', where=('first', None))
        self.assertEqual(len(op), 5)
        self.assertEqual(op[4], ('first', None))

    def test_where_keyword_only(self):
        with self.assertRaises(TypeError):
            mod('a', KEEP_PARENT, ('first', None))  # type: ignore


class TestModUnknownId(unittest.TestCase):
    """``mod`` against an unknown id is a silent no-op."""

    def test_unknown_id_is_noop(self):
        s = State(root_id='/')
        apply_ops(s, [mod('ghost', hidden=True)])
        self.assertNotIn('ghost', s._items_by_id)
        self.assertFalse(s._children.get('/', []))

    def test_unknown_id_does_not_mark_dirty(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        s._visible_dirty = False
        apply_ops(s, [mod('ghost', hidden=True)])
        self.assertFalse(s._visible_dirty)


class TestModPatchKeepParent(unittest.TestCase):
    """``mod`` with ``KEEP_PARENT`` patches fields and leaves parent alone."""

    def _seed(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', title='A'),
            upsert('b', '/', title='B'),
            upsert('c', '/', title='C'),
        ])
        return s

    def test_patches_fields(self):
        s = self._seed()
        apply_ops(s, [mod('b', hidden=True, tag='X')])
        self.assertTrue(s._items_by_id['b'].hidden)
        self.assertEqual(s._items_by_id['b'].tag, 'X')

    def test_parent_unchanged(self):
        s = self._seed()
        apply_ops(s, [mod('b', hidden=True)])
        self.assertEqual(s._parent_of_id['b'], '/')

    def test_position_unchanged(self):
        s = self._seed()
        apply_ops(s, [mod('b', hidden=True)])
        self.assertEqual([c.id for c in s._children['/']], ['a', 'b', 'c'])

    def test_custom_attrs_added(self):
        s = self._seed()
        apply_ops(s, [mod('b', custom_attr='value')])
        self.assertEqual(s._items_by_id['b'].custom_attr, 'value')

    def test_id_field_silently_dropped(self):
        # Hand-rolled op tuple — passing ``id=`` to ``mod()`` would
        # collide with the positional id arg, so build the tuple
        # directly. The ``id`` key inside fields is dropped by
        # ``_apply_mod`` (the op tuple's id is authoritative).
        s = self._seed()
        apply_ops(s, [
            ('mod', 'b', KEEP_PARENT, {'id': 'should-be-ignored', 'title': 'B2'}),
        ])
        self.assertEqual(s._items_by_id['b'].id, 'b')
        self.assertEqual(s._items_by_id['b'].title, 'B2')

    def test_hidden_flip_marks_dirty(self):
        s = self._seed()
        s._visible_dirty = False
        apply_ops(s, [mod('b', hidden=True)])
        self.assertTrue(s._visible_dirty)

    def test_field_patch_marks_dirty(self):
        # All structural-or-rendering field patches mark dirty (same
        # posture as upsert's existing-id branch).
        s = self._seed()
        s._visible_dirty = False
        apply_ops(s, [mod('b', title='B-new')])
        self.assertTrue(s._visible_dirty)


class TestModReparent(unittest.TestCase):
    """``mod`` with an explicit parent_id reparents the existing row."""

    def _seed(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('A', '/', title='A', has_children=True),
            upsert('B', '/', title='B', has_children=True),
            upsert('x', 'A', title='X'),
        ])
        return s

    def test_reparent_moves_to_new_parent(self):
        s = self._seed()
        apply_ops(s, [mod('x', 'B', tag='moved')])
        self.assertNotIn(
            'x', [c.id for c in s._children.get('A', [])]
        )
        self.assertIn('x', [c.id for c in s._children['B']])
        self.assertEqual(s._parent_of_id['x'], 'B')
        self.assertEqual(s._items_by_id['x'].tag, 'moved')

    def test_reparent_same_parent_is_noop_for_position(self):
        s = self._seed()
        apply_ops(s, [mod('x', 'A', tag='same')])
        self.assertEqual([c.id for c in s._children['A']], ['x'])

    def test_reparent_with_where(self):
        s = self._seed()
        apply_ops(s, [
            upsert('y', 'B', title='Y'),
            upsert('z', 'B', title='Z'),
        ])
        # Reparent x into B at position 'first'.
        apply_ops(s, [mod('x', 'B', where=('first', None))])
        self.assertEqual([c.id for c in s._children['B']], ['x', 'y', 'z'])

    def test_reparent_to_none(self):
        # ``parent_id=None`` is an explicit reparent (not KEEP_PARENT).
        s = self._seed()
        apply_ops(s, [mod('x', None, tag='rooted')])
        self.assertEqual(s._parent_of_id['x'], None)


class TestModReposition(unittest.TestCase):
    """``mod`` with ``where`` repositions the existing row in its parent."""

    def _seed(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', title='A'),
            upsert('b', '/', title='B'),
            upsert('c', '/', title='C'),
            upsert('d', '/', title='D'),
        ])
        return s

    def test_where_first(self):
        s = self._seed()
        apply_ops(s, [mod('c', where=('first', None))])
        self.assertEqual([c.id for c in s._children['/']], ['c', 'a', 'b', 'd'])

    def test_where_last(self):
        s = self._seed()
        apply_ops(s, [mod('a', where=('last', None))])
        self.assertEqual([c.id for c in s._children['/']], ['b', 'c', 'd', 'a'])

    def test_where_before_id(self):
        s = self._seed()
        apply_ops(s, [mod('a', where=('before', None, 'd'))])
        self.assertEqual([c.id for c in s._children['/']], ['b', 'c', 'a', 'd'])

    def test_where_after_id(self):
        s = self._seed()
        apply_ops(s, [mod('a', where=('after', None, 'd'))])
        self.assertEqual([c.id for c in s._children['/']], ['b', 'c', 'd', 'a'])

    def test_where_same_id_pivot_is_noop(self):
        s = self._seed()
        apply_ops(s, [mod('b', where=('before', None, 'b'))])
        self.assertEqual([c.id for c in s._children['/']], ['a', 'b', 'c', 'd'])

    def test_where_on_unknown_id_is_noop(self):
        s = self._seed()
        apply_ops(s, [mod('ghost', where=('first', None))])
        self.assertEqual([c.id for c in s._children['/']], ['a', 'b', 'c', 'd'])


class TestModValidation(unittest.TestCase):
    """``mod`` rejects malformed shape."""

    def test_bad_parent_id_type(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        with self.assertRaises(ValueError):
            apply_ops(s, [('mod', 'a', 42, {'hidden': True})])

    def test_bad_where(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        with self.assertRaises(ValueError):
            apply_ops(s, [
                ('mod', 'a', KEEP_PARENT, {}, ('not-a-keyword', None)),
            ])


class TestModPreviewCacheGate(unittest.TestCase):
    """``mod`` invalidates the preview cache only when fields change (#445)."""

    def _seed(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        item = s._items_by_id['a']
        item.preview = 'cached preview text'
        item.preview_render = 'cached render sentinel'
        return s, item

    def test_no_fields_preserves_preview_cache(self):
        s, item = self._seed()
        apply_ops(s, [mod('a')])
        self.assertEqual(item.preview, 'cached preview text')
        self.assertEqual(item.preview_render, 'cached render sentinel')

    def test_only_id_field_preserves_preview_cache(self):
        # ``_apply_mod`` drops the ``id`` key from the patch; an
        # id-only patch is effectively a no-op and must not invalidate.
        s, item = self._seed()
        apply_ops(s, [
            ('mod', 'a', KEEP_PARENT, {'id': 'ignored'}),
        ])
        self.assertEqual(item.preview, 'cached preview text')
        self.assertEqual(item.preview_render, 'cached render sentinel')

    def test_field_mutation_drops_preview_cache(self):
        s, item = self._seed()
        apply_ops(s, [mod('a', title='A2')])
        self.assertIsNone(item.preview)
        self.assertIsNone(item.preview_render)


# ---- `hidden` field + dirty propagation -----------------------------------


class TestHiddenFieldOnItem(unittest.TestCase):
    """``Item.hidden`` is a declared dataclass field, default False."""

    def test_default_false(self):
        it = Item(id='x')
        self.assertFalse(it.hidden)

    def test_explicit_true(self):
        it = Item(id='x', hidden=True)
        self.assertTrue(it.hidden)

    def test_upsert_with_hidden_kwarg(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A', hidden=True)])
        self.assertTrue(s._items_by_id['a'].hidden)

    def test_upsert_default_hidden_false(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        self.assertFalse(s._items_by_id['a'].hidden)

    def test_set_item_with_hidden_kwarg(self):
        s = State(root_id='/')
        apply_ops(s, [set_item('a', '/', title='A', hidden=True)])
        self.assertTrue(s._items_by_id['a'].hidden)

    def test_set_item_without_hidden_reverts_to_default(self):
        # ``set`` builds a fresh Item; fields not specified revert to
        # dataclass defaults — including ``hidden=False``.
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A', hidden=True)])
        apply_ops(s, [set_item('a', '/', title='A')])
        self.assertFalse(s._items_by_id['a'].hidden)


# ---- preview-cache ops (#446) ----------------------------------------------


class TestPreviewOpHelpers(unittest.TestCase):
    """Preview-cache op constructors produce well-formed tagged tuples."""

    def test_set_preview_op_shape(self):
        self.assertEqual(
            set_preview_op('a', 'hi'), ('set_preview', 'a', 'hi'),
        )

    def test_set_preview_op_passes_none_through(self):
        # Constructor leaves None as-is; ``apply_ops`` coerces to ''.
        self.assertEqual(
            set_preview_op('a', None), ('set_preview', 'a', None),
        )

    def test_append_preview_op_shape(self):
        self.assertEqual(
            append_preview_op('a', 'chunk'),
            ('append_preview', 'a', 'chunk'),
        )

    def test_clear_preview_op_shape(self):
        self.assertEqual(clear_preview_op('a'), ('clear_preview', 'a'))

    def test_invalidate_preview_op_shape(self):
        self.assertEqual(
            invalidate_preview_op('a'), ('invalidate_preview', 'a'),
        )

    def test_drop_preview_cache_op_single_id(self):
        self.assertEqual(
            drop_preview_cache_op('a'), ('drop_preview_cache', 'a'),
        )

    def test_drop_preview_cache_op_default_none(self):
        # Default arg means "drop all".
        self.assertEqual(
            drop_preview_cache_op(), ('drop_preview_cache', None),
        )


class TestApplySetPreviewOp(unittest.TestCase):
    """``set_preview_op`` writes ``Item.preview`` and drops the wrap cache."""

    def _seed(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        item = s._items_by_id['a']
        item.preview_render = 'cached render sentinel'
        return s, item

    def test_writes_text(self):
        s, item = self._seed()
        apply_ops(s, [set_preview_op('a', 'hello')])
        self.assertEqual(item.preview, 'hello')

    def test_drops_preview_render(self):
        s, item = self._seed()
        apply_ops(s, [set_preview_op('a', 'hello')])
        self.assertIsNone(item.preview_render)

    def test_none_coerces_to_empty_string(self):
        # Matches Browser.set_preview's contract.
        s, item = self._seed()
        apply_ops(s, [set_preview_op('a', None)])
        self.assertEqual(item.preview, '')

    def test_unknown_id_is_silent_noop(self):
        s = State(root_id='/')
        apply_ops(s, [set_preview_op('ghost', 'x')])
        self.assertNotIn('ghost', s._items_by_id)

    def test_sets_preview_dirty_flag(self):
        s, _ = self._seed()
        s._preview_dirty = False
        apply_ops(s, [set_preview_op('a', 'hello')])
        self.assertTrue(s._preview_dirty)

    def test_does_not_kick(self):
        # set_preview never schedules a worker kick.
        s, _ = self._seed()
        apply_ops(s, [set_preview_op('a', 'hello')])
        self.assertEqual(s._preview_kicks, [])

    def test_does_not_mark_visible_dirty(self):
        s, _ = self._seed()
        s._visible_dirty = False
        apply_ops(s, [set_preview_op('a', 'hello')])
        self.assertFalse(s._visible_dirty)


class TestApplyAppendPreviewOp(unittest.TestCase):
    """``append_preview_op`` does an rmw on ``Item.preview``."""

    def _seed(self, initial=None):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        item = s._items_by_id['a']
        item.preview = initial
        return s, item

    def test_appends_to_none(self):
        s, item = self._seed(initial=None)
        apply_ops(s, [append_preview_op('a', 'hello')])
        self.assertEqual(item.preview, 'hello')

    def test_appends_to_empty(self):
        s, item = self._seed(initial='')
        apply_ops(s, [append_preview_op('a', 'hello')])
        self.assertEqual(item.preview, 'hello')

    def test_appends_to_existing(self):
        s, item = self._seed(initial='foo')
        apply_ops(s, [append_preview_op('a', 'bar')])
        self.assertEqual(item.preview, 'foobar')

    def test_multiple_appends_in_batch(self):
        # FIFO within a batch — appends concatenate in order.
        s, item = self._seed(initial='')
        apply_ops(s, [
            append_preview_op('a', 'a'),
            append_preview_op('a', 'b'),
            append_preview_op('a', 'c'),
        ])
        self.assertEqual(item.preview, 'abc')

    def test_none_chunk_coerces_to_empty(self):
        s, item = self._seed(initial='foo')
        apply_ops(s, [append_preview_op('a', None)])
        self.assertEqual(item.preview, 'foo')

    def test_unknown_id_is_silent_noop(self):
        s = State(root_id='/')
        apply_ops(s, [append_preview_op('ghost', 'x')])
        self.assertNotIn('ghost', s._items_by_id)

    def test_sets_preview_dirty_flag(self):
        s, _ = self._seed()
        s._preview_dirty = False
        apply_ops(s, [append_preview_op('a', 'x')])
        self.assertTrue(s._preview_dirty)

    def test_passes_through_with_no_cached_render(self):
        # ``_extend_or_drop_preview_render`` is a no-op when
        # ``preview_render`` is already None (the next paint regenerates
        # fresh). Verify the append doesn't accidentally set it to
        # something non-None.
        s, item = self._seed(initial='foo')
        self.assertIsNone(item.preview_render)
        apply_ops(s, [append_preview_op('a', 'bar')])
        self.assertIsNone(item.preview_render)


class TestApplyClearPreviewOp(unittest.TestCase):
    """``clear_preview_op`` drops the raw text and wrap cache."""

    def _seed(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        item = s._items_by_id['a']
        item.preview = 'cached'
        item.preview_render = 'cached render'
        return s, item

    def test_clears_preview(self):
        s, item = self._seed()
        apply_ops(s, [clear_preview_op('a')])
        self.assertIsNone(item.preview)

    def test_clears_preview_render(self):
        s, item = self._seed()
        apply_ops(s, [clear_preview_op('a')])
        self.assertIsNone(item.preview_render)

    def test_unknown_id_is_silent_noop(self):
        s = State(root_id='/')
        apply_ops(s, [clear_preview_op('ghost')])
        self.assertNotIn('ghost', s._items_by_id)

    def test_sets_preview_dirty_flag(self):
        s, _ = self._seed()
        s._preview_dirty = False
        apply_ops(s, [clear_preview_op('a')])
        self.assertTrue(s._preview_dirty)

    def test_does_not_kick(self):
        s, _ = self._seed()
        apply_ops(s, [clear_preview_op('a')])
        self.assertEqual(s._preview_kicks, [])


class TestApplyInvalidatePreviewOp(unittest.TestCase):
    """``invalidate_preview_op`` drops the cache and schedules a worker kick."""

    def _seed(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        item = s._items_by_id['a']
        item.preview = 'cached'
        item.preview_render = 'cached render'
        return s, item

    def test_clears_cache(self):
        s, item = self._seed()
        apply_ops(s, [invalidate_preview_op('a')])
        self.assertIsNone(item.preview)
        self.assertIsNone(item.preview_render)

    def test_schedules_kick_for_id(self):
        s, _ = self._seed()
        apply_ops(s, [invalidate_preview_op('a')])
        self.assertEqual(s._preview_kicks, [('id', 'a')])

    def test_kick_fires_even_when_id_unknown(self):
        # invalidate_preview always kicks the worker — the cache-drop
        # is a no-op for unknown ids but the request fires anyway
        # (mirrors Browser.invalidate_preview's docstring).
        s = State(root_id='/')
        apply_ops(s, [invalidate_preview_op('ghost')])
        self.assertEqual(s._preview_kicks, [('id', 'ghost')])

    def test_sets_preview_dirty_flag(self):
        s, _ = self._seed()
        s._preview_dirty = False
        apply_ops(s, [invalidate_preview_op('a')])
        self.assertTrue(s._preview_dirty)


class TestApplyDropPreviewCacheOp(unittest.TestCase):
    """``drop_preview_cache_op`` drops one or all entries with a cursor kick."""

    def _seed_two(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', title='A'),
            upsert('b', '/', title='B'),
        ])
        a = s._items_by_id['a']
        b = s._items_by_id['b']
        a.preview = 'A-text'
        a.preview_render = 'A-render'
        b.preview = 'B-text'
        b.preview_render = 'B-render'
        return s, a, b

    def test_drop_single_id_clears_only_that_one(self):
        s, a, b = self._seed_two()
        apply_ops(s, [drop_preview_cache_op('a')])
        self.assertIsNone(a.preview)
        self.assertEqual(b.preview, 'B-text')

    def test_drop_none_clears_all(self):
        s, a, b = self._seed_two()
        apply_ops(s, [drop_preview_cache_op()])
        self.assertIsNone(a.preview)
        self.assertIsNone(b.preview)
        self.assertIsNone(a.preview_render)
        self.assertIsNone(b.preview_render)

    def test_drop_unknown_id_is_silent_noop(self):
        s, a, _ = self._seed_two()
        apply_ops(s, [drop_preview_cache_op('ghost')])
        # a/b untouched.
        self.assertEqual(a.preview, 'A-text')

    def test_kick_intent_for_single_id(self):
        # Recorded as ``cursor_if`` — Browser kicks only when the
        # dropped id matches the current preview cursor.
        s, _, _ = self._seed_two()
        apply_ops(s, [drop_preview_cache_op('a')])
        self.assertEqual(s._preview_kicks, [('cursor_if', 'a')])

    def test_kick_intent_for_drop_all(self):
        # Recorded as ``cursor`` — Browser kicks the cursor id (if any).
        s, _, _ = self._seed_two()
        apply_ops(s, [drop_preview_cache_op()])
        self.assertEqual(s._preview_kicks, [('cursor', None)])

    def test_kick_intent_for_unknown_single_id(self):
        # Unknown id still records the intent — the
        # cursor-match guard in Browser will filter it out.
        s, _, _ = self._seed_two()
        apply_ops(s, [drop_preview_cache_op('ghost')])
        self.assertEqual(s._preview_kicks, [('cursor_if', 'ghost')])

    def test_sets_preview_dirty_flag(self):
        s, _, _ = self._seed_two()
        s._preview_dirty = False
        apply_ops(s, [drop_preview_cache_op('a')])
        self.assertTrue(s._preview_dirty)


class TestPreviewOpsSideEffectReset(unittest.TestCase):
    """Each ``apply_ops`` call resets ``_preview_dirty`` / ``_preview_kicks``."""

    def test_resets_dirty_when_no_preview_ops(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        # No preview op in this batch — dirty stays False.
        self.assertFalse(s._preview_dirty)

    def test_resets_kicks_when_no_preview_ops(self):
        s = State(root_id='/')
        apply_ops(s, [upsert('a', '/', title='A')])
        self.assertEqual(s._preview_kicks, [])

    def test_subsequent_batch_resets_dirty(self):
        # Batch 1 sets dirty; Batch 2 (no preview ops) clears it.
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', title='A'),
            set_preview_op('a', 'x'),
        ])
        self.assertTrue(s._preview_dirty)
        apply_ops(s, [upsert('a', '/', title='A2')])
        self.assertFalse(s._preview_dirty)

    def test_subsequent_batch_resets_kicks(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', title='A'),
            invalidate_preview_op('a'),
        ])
        self.assertEqual(s._preview_kicks, [('id', 'a')])
        apply_ops(s, [upsert('a', '/', title='A2')])
        self.assertEqual(s._preview_kicks, [])

    def test_multiple_kicks_accumulate_in_order(self):
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', title='A'),
            upsert('b', '/', title='B'),
            invalidate_preview_op('a'),
            invalidate_preview_op('b'),
            drop_preview_cache_op(),
        ])
        self.assertEqual(s._preview_kicks, [
            ('id', 'a'),
            ('id', 'b'),
            ('cursor', None),
        ])


class TestMixedBatchUpsertAndSetPreview(unittest.TestCase):
    """A single batch can carry tree ops and preview ops; both visible after drain."""

    def test_upsert_then_set_preview_in_one_batch(self):
        # The registering upsert lands before the set_preview op within
        # the same batch — the umbrella composer relies on this
        # ordering to register leaves before writing their previews.
        s = State(root_id='/')
        apply_ops(s, [
            upsert('a', '/', title='A'),
            set_preview_op('a', 'hello'),
        ])
        self.assertEqual(s._items_by_id['a'].preview, 'hello')

    def test_set_preview_before_upsert_is_silent_noop(self):
        # Set_preview targeting a not-yet-registered id is dropped.
        s = State(root_id='/')
        apply_ops(s, [
            set_preview_op('a', 'hello'),
            upsert('a', '/', title='A'),
        ])
        # The set_preview op silently no-op'd; the upsert created
        # the Item with default preview=None.
        self.assertIsNone(s._items_by_id['a'].preview)

    def test_many_leaves_with_previews_in_one_batch(self):
        # The umbrella composer's hot path: a batch of upserts
        # followed by per-leaf set_preview_op entries.
        s = State(root_id='/')
        batch = []
        for i in range(50):
            batch.append(upsert(f'leaf{i}', '/', title=f'L{i}'))
        for i in range(50):
            batch.append(set_preview_op(f'leaf{i}', f'preview{i}'))
        apply_ops(s, batch)
        for i in range(50):
            self.assertEqual(
                s._items_by_id[f'leaf{i}'].preview, f'preview{i}',
            )

    def test_dirty_flags_set_for_mixed_batch(self):
        s = State(root_id='/')
        s._visible_dirty = False
        apply_ops(s, [
            upsert('a', '/', title='A'),
            set_preview_op('a', 'x'),
        ])
        # Tree op flips _visible_dirty; preview op flips _preview_dirty.
        self.assertTrue(s._visible_dirty)
        self.assertTrue(s._preview_dirty)


if __name__ == '__main__':
    unittest.main()
