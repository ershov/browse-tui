"""Tests for ``Item.synthetic`` and ``_promote_synthetic`` (#477).

When ``visible_items`` fabricates a stub for a scope-root id that has
no real Item yet (recipe-pre-pushed ``initial_scope``, deep
``scope_stack``, lazy-fetched alt-up into an uncached ancestor,
post-refresh window before re-fetch), the stub is registered in
``_items_by_id`` with ``synthetic=True``. The children-fetch delivery
path (``apply_children_results``) promotes the stub in place when the
parent's children arrive with a matching id.

See docs/superpowers/specs/2026-05-27-scope-root-unification-design.md.
"""

import unittest

from test.unit._loader import load

_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')

_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
State = _state.State
Item = _data.Item
scope_into = _state.scope_into
visible_items = _state.visible_items
_promote_synthetic = _state._promote_synthetic
_promote_synthetics = _state._promote_synthetics


class TestSyntheticFlagDefault(unittest.TestCase):
    """Items default ``synthetic=False`` and the field is non-init."""

    def test_default_is_false(self):
        it = Item(id='x')
        self.assertFalse(it.synthetic)

    def test_not_a_constructor_arg(self):
        # init=False — passing as kwarg must raise.
        with self.assertRaises(TypeError):
            Item(id='x', synthetic=True)

    def test_writable_attribute(self):
        it = Item(id='x')
        it.synthetic = True
        self.assertTrue(it.synthetic)


class TestVisibleItemsFabricatedStub(unittest.TestCase):
    """``visible_items`` flags fabricated scope-root stubs."""

    def test_fabricated_stub_carries_flag(self):
        # Scope into an id that has no cached Item and isn't in
        # _items_by_id — visible_items must synthesize a stub.
        s = State(root_id='__ROOT__')
        scope_into(s, 'unseen-id')
        rows = visible_items(s)
        # First row is the scope_root.
        self.assertEqual(rows[0].kind, 'scope_root')
        self.assertEqual(rows[0].item.id, 'unseen-id')
        self.assertTrue(rows[0].item.synthetic)
        # Registered in _items_by_id with the flag set.
        self.assertIs(s._items_by_id['unseen-id'], rows[0].item)
        self.assertTrue(s._items_by_id['unseen-id'].synthetic)

    def test_real_item_in_cache_is_not_synthetic(self):
        # When the scope item exists as a child somewhere, _find_item
        # recovers it — no synthesis happens.
        real = Item(id='A', has_children=True, title='Alpha')
        s = State(
            root_id='__ROOT__',
            _children={'__ROOT__': [real]},
        )
        scope_into(s, 'A')
        rows = visible_items(s)
        self.assertEqual(rows[0].item.id, 'A')
        self.assertFalse(rows[0].item.synthetic)
        self.assertIs(rows[0].item, real)


class TestPromoteSyntheticHelper(unittest.TestCase):
    """``_promote_synthetic`` mutates the stub in place from the real Item."""

    def test_copies_item_data_fields(self):
        stub = Item(id='X')
        stub.synthetic = True
        real = Item(
            id='X', title='Real Title', tag='running', tag_style='green',
            has_children=True, hidden=False,
        )
        real.scope_title = '/full/path/to/X'
        _promote_synthetic(stub, real)
        self.assertEqual(stub.title, 'Real Title')
        self.assertEqual(stub.tag, 'running')
        self.assertEqual(stub.tag_style, 'green')
        self.assertTrue(stub.has_children)
        self.assertEqual(stub.scope_title, '/full/path/to/X')

    def test_clears_synthetic_flag(self):
        stub = Item(id='X')
        stub.synthetic = True
        real = Item(id='X', title='Real')
        _promote_synthetic(stub, real)
        self.assertFalse(stub.synthetic)

    def test_copies_recipe_extras(self):
        stub = Item(id='X')
        stub.synthetic = True
        real = Item(id='X', title='Real')
        real.size = 1024
        real.path = '/tmp/x'
        _promote_synthetic(stub, real)
        self.assertEqual(stub.size, 1024)
        self.assertEqual(stub.path, '/tmp/x')

    def test_preserves_cached_preview_when_real_lacks_one(self):
        stub = Item(id='X', title='stub')
        stub.synthetic = True
        stub.preview = 'cached preview text'
        real = Item(id='X', title='Real')
        # real.preview defaults to None
        _promote_synthetic(stub, real)
        self.assertEqual(stub.preview, 'cached preview text')

    def test_real_preview_overrides_when_set(self):
        stub = Item(id='X', title='stub')
        stub.synthetic = True
        stub.preview = 'stale'
        real = Item(id='X', title='Real')
        real.preview = 'fresh'
        _promote_synthetic(stub, real)
        self.assertEqual(stub.preview, 'fresh')

    def test_id_unchanged(self):
        stub = Item(id='X')
        stub.synthetic = True
        real = Item(id='X', title='Real')
        _promote_synthetic(stub, real)
        self.assertEqual(stub.id, 'X')


class TestPromoteSyntheticsList(unittest.TestCase):
    """``_promote_synthetics`` substitutes promoted stubs into the list."""

    def test_unmatched_items_pass_through(self):
        s = State()
        items = [Item(id='a'), Item(id='b')]
        out = _promote_synthetics(s, items)
        self.assertEqual([it.id for it in out], ['a', 'b'])
        # Pass-through preserves identity for non-matching items.
        self.assertIs(out[0], items[0])
        self.assertIs(out[1], items[1])

    def test_matched_synthetic_is_substituted_identity_stable(self):
        s = State()
        stub = Item(id='X', title='stub')
        stub.synthetic = True
        s._items_by_id['X'] = stub
        real = Item(id='X', title='Real')
        out = _promote_synthetics(s, [real])
        # The promoted stub replaces the incoming real Item in the list.
        self.assertIs(out[0], stub)
        # Its data is the real's.
        self.assertEqual(out[0].title, 'Real')
        self.assertFalse(out[0].synthetic)

    def test_non_synthetic_existing_passes_through_real(self):
        # If _items_by_id has the id but it's NOT a synthetic, the
        # incoming real Item flows through (no in-place mutation;
        # apply_children_results' caller will install it under id).
        s = State()
        non_syn = Item(id='X', title='old')
        s._items_by_id['X'] = non_syn
        real = Item(id='X', title='new')
        out = _promote_synthetics(s, [real])
        self.assertIs(out[0], real)
        # The old Item is unchanged.
        self.assertEqual(non_syn.title, 'old')


class TestPromotionViaApplyChildrenResults(unittest.TestCase):
    """End-to-end: visible_items synthesizes, fetch delivers, promotion fires."""

    def test_stub_promoted_on_children_delivery(self):
        # Scope into an unseen id → synthetic stub gets registered.
        b = Browser(BrowserConfig(_headless=True, root_id='__ROOT__'))
        try:
            scope_into(b._state, 'A')
            rows = visible_items(b._state)
            stub = rows[0].item
            self.assertTrue(stub.synthetic)
            # Cache something on the stub to test preservation.
            stub.preview = 'cached'
            # Deliver root's children — they include the real Item with
            # id='A'.
            real_A = Item(id='A', title='Alpha', has_children=True)
            real_A.path = '/etc/alpha'
            b.set_children('__ROOT__', [real_A])
            b.apply_children_results()
            # Identity: the registered Item is still the same object.
            self.assertIs(b._state._items_by_id['A'], stub)
            # But its fields are now the real Item's.
            self.assertEqual(stub.title, 'Alpha')
            self.assertTrue(stub.has_children)
            self.assertEqual(stub.path, '/etc/alpha')
            self.assertFalse(stub.synthetic)
            # The cached preview survived (real_A.preview is None).
            self.assertEqual(stub.preview, 'cached')
            # The children list contains the promoted stub, not real_A.
            self.assertIs(b._state._children['__ROOT__'][0], stub)
        finally:
            b.stop_workers()

    def test_no_synthetic_pre_existing_uses_delivered_item(self):
        # Sanity: when no synthetic exists, normal Item flows through.
        b = Browser(BrowserConfig(_headless=True, root_id='__ROOT__'))
        try:
            real_A = Item(id='A', title='Alpha')
            b.set_children('__ROOT__', [real_A])
            b.apply_children_results()
            self.assertIs(b._state._items_by_id['A'], real_A)
            self.assertFalse(b._state._items_by_id['A'].synthetic)
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
