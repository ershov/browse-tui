"""Tests for browse-tui Item dataclass and to_item coercion."""

import importlib.util
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_DATA = _ROOT / 'src-tui' / '030-data.py'

_spec = importlib.util.spec_from_file_location('_browse_tui_data', _DATA)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

Item = _mod.Item
to_item = _mod.to_item
_item_extras = _mod._item_extras


class TestItemConstruction(unittest.TestCase):
    """Item dataclass construction and field defaults."""

    def test_id_only_string_defaults(self):
        item = Item(id='foo')
        self.assertEqual(item.id, 'foo')
        self.assertEqual(item.title, 'foo')
        self.assertEqual(item.tag, '')
        self.assertEqual(item.tag_style, '')
        self.assertFalse(item.has_children)

    def test_title_falls_back_to_str_of_id(self):
        item = Item(id=42)
        self.assertEqual(item.id, 42)
        self.assertEqual(item.title, '42')

    def test_explicit_title_not_overridden(self):
        item = Item(id='x', title='Custom')
        self.assertEqual(item.title, 'Custom')

    def test_has_children_flag(self):
        item = Item(id='x', has_children=True)
        self.assertTrue(item.has_children)

    def test_boundary_defaults_false(self):
        item = Item(id='x')
        self.assertFalse(item.boundary)

    def test_boundary_flag_settable(self):
        item = Item(id='x', boundary=True)
        self.assertTrue(item.boundary)

    def test_meta_defaults_false(self):
        item = Item(id='x')
        self.assertFalse(item.meta)

    def test_meta_flag_settable(self):
        item = Item(id='x', meta=True)
        self.assertTrue(item.meta)

    def test_filter_hidden_defaults_false(self):
        item = Item(id='x')
        self.assertFalse(item._filter_hidden)

    def test_filter_hidden_excluded_from_init(self):
        # init=False keeps the attribute out of the constructor signature.
        with self.assertRaises(TypeError):
            Item(id='x', _filter_hidden=True)

    def test_filter_hidden_settable_after_construction(self):
        item = Item(id='x')
        item._filter_hidden = True
        self.assertTrue(item._filter_hidden)

    def test_filter_hidden_excluded_from_repr(self):
        item = Item(id='x')
        item._filter_hidden = True
        self.assertNotIn('_filter_hidden', repr(item))

    def test_filter_hidden_excluded_from_equality(self):
        a = Item(id='x')
        b = Item(id='x')
        a._filter_hidden = True
        # compare=False keeps __eq__ insensitive to the flag.
        self.assertEqual(a, b)

    def test_tag_and_tag_style(self):
        item = Item(id='x', tag='in-progress', tag_style='green')
        self.assertEqual(item.tag, 'in-progress')
        self.assertEqual(item.tag_style, 'green')

    def test_extra_attrs_survive(self):
        item = Item(id='x')
        item.size = 1234
        item.path = '/tmp/foo'
        self.assertEqual(item.size, 1234)
        self.assertEqual(item.path, '/tmp/foo')


class TestToItemFromItem(unittest.TestCase):
    """to_item with an existing Item passes it through unchanged."""

    def test_passthrough_returns_same_instance(self):
        original = Item(id='x', title='X')
        result = to_item(original)
        self.assertIs(result, original)

    def test_passthrough_preserves_meta(self):
        original = Item(id='x', meta=True)
        result = to_item(original)
        self.assertIs(result, original)
        self.assertTrue(result.meta)


class TestItemExtras(unittest.TestCase):
    """_item_extras carries recipe attrs but excludes declared fields."""

    def test_declared_meta_not_in_extras(self):
        # ``meta`` is a declared dataclass field, so it is excluded from
        # the carried-extras set (same as ``hidden`` / ``boundary``).
        item = Item(id='x', meta=True)
        extras = _item_extras(item)
        self.assertNotIn('meta', extras)

    def test_recipe_attr_in_extras_but_not_declared(self):
        item = Item(id='x', meta=True)
        item.size = 7
        extras = _item_extras(item)
        self.assertEqual(extras.get('size'), 7)
        self.assertNotIn('meta', extras)


class TestToItemFromStr(unittest.TestCase):
    """to_item with a str produces a leaf Item (str is a hashable id)."""

    def test_str_becomes_id_and_title(self):
        item = to_item('foo')
        self.assertEqual(item.id, 'foo')
        self.assertEqual(item.title, 'foo')
        self.assertFalse(item.has_children)


class TestToItemAnyHashable(unittest.TestCase):
    """to_item with any non-Item, non-dict value takes it as the id.

    New contract (spec 5.1): *any hashable is an id* — ``to_item(x)`` is
    ``Item(id=x)`` for ``str``, ``int``, ``tuple``, ``frozenset``, a frozen
    dataclass, etc. There is NO positional-tuple shorthand: a tuple is an
    id, not ``(id, title, ...)``.
    """

    def test_int_becomes_id(self):
        item = to_item(42)
        self.assertEqual(item.id, 42)
        self.assertEqual(item.title, '42')
        self.assertFalse(item.has_children)

    def test_none_becomes_id(self):
        item = to_item(None)
        self.assertIsNone(item.id)

    def test_tuple_is_the_id_not_positional_fields(self):
        # Was the (id, title) shorthand; now the whole tuple IS the id.
        item = to_item(('a', 'Apple'))
        self.assertEqual(item.id, ('a', 'Apple'))
        self.assertEqual(item.title, str(('a', 'Apple')))
        self.assertEqual(item.tag, '')          # no positional spill-over
        self.assertFalse(item.has_children)

    def test_tagged_tuple_id(self):
        item = to_item(('msg', '/p.jsonl', 7))
        self.assertEqual(item.id, ('msg', '/p.jsonl', 7))

    def test_empty_tuple_is_a_valid_id(self):
        # An empty tuple is a perfectly good (hashable) id now.
        item = to_item(())
        self.assertEqual(item.id, ())

    def test_frozenset_id(self):
        fs = frozenset({1, 2, 3})
        item = to_item(fs)
        self.assertEqual(item.id, fs)

    def test_frozen_dataclass_id(self):
        from dataclasses import dataclass as _dc

        @_dc(frozen=True)
        class Key:
            kind: str
            n: int

        k = Key('commit', 3)
        item = to_item(k)
        self.assertEqual(item.id, k)


class TestToItemFromDict(unittest.TestCase):
    """to_item with a dict uses Item(**d) and tolerates extra keys."""

    def test_basic_dict(self):
        item = to_item({'id': 'x', 'title': 'X', 'has_children': True})
        self.assertEqual(item.id, 'x')
        self.assertEqual(item.title, 'X')
        self.assertTrue(item.has_children)

    def test_meta_via_dict(self):
        item = to_item({'id': 'x', 'meta': True})
        self.assertEqual(item.id, 'x')
        self.assertTrue(item.meta)
        # Declared field, so it is coerced — never carried as an extra.
        self.assertNotIn('meta', _item_extras(item))

    def test_meta_defaults_false_via_dict(self):
        item = to_item({'id': 'x'})
        self.assertFalse(item.meta)

    def test_extra_keys_become_attrs(self):
        item = to_item({'id': 'x', 'size': 1234})
        self.assertEqual(item.id, 'x')
        self.assertEqual(item.size, 1234)

    def test_extra_keys_alongside_dataclass_fields(self):
        item = to_item({'id': 'x', 'title': 'X', 'size': 1234, 'mtime': 99})
        self.assertEqual(item.id, 'x')
        self.assertEqual(item.title, 'X')
        self.assertEqual(item.size, 1234)
        self.assertEqual(item.mtime, 99)

    def test_empty_dict_raises(self):
        with self.assertRaises(TypeError):
            to_item({})

    def test_dict_missing_id_raises(self):
        with self.assertRaises(TypeError):
            to_item({'title': 'no id here'})


class TestToItemUnhashableDeferred(unittest.TestCase):
    """An unhashable id is NOT rejected by ``to_item`` (spec 5.1/5.2).

    ``to_item`` does not hash the id — it just wraps it. A ``list`` (that
    is not a dict payload) becomes ``Item(id=<list>)``; the hashability
    error is raised later, at the ``_index_set`` choke point, when the id
    actually enters the index (covered in ``test_state_indexes.py``).
    """

    def test_list_becomes_id_no_raise(self):
        item = to_item(['x', 'y'])
        self.assertEqual(item.id, ['x', 'y'])

    def test_set_becomes_id_no_raise(self):
        s = {'x', 'y'}
        item = to_item(s)
        self.assertEqual(item.id, s)


class TestMixedListCoercion(unittest.TestCase):
    """Mixed iterables are coerced element-wise by the caller."""

    def test_mixed_list(self):
        raw = [
            Item(id='a'),
            'b',
            ('c', 'Apple'),          # the whole tuple is the id now
            42,
            {'id': 'e', 'title': 'E', 'has_children': True},
        ]
        items = [to_item(x) for x in raw]
        self.assertEqual(len(items), 5)
        # a — passthrough Item
        self.assertEqual(items[0].id, 'a')
        self.assertIs(items[0], raw[0])
        # b — string id
        self.assertEqual(items[1].id, 'b')
        self.assertEqual(items[1].title, 'b')
        # c — tuple id (NOT (id, title))
        self.assertEqual(items[2].id, ('c', 'Apple'))
        self.assertEqual(items[2].title, str(('c', 'Apple')))
        # d — int id
        self.assertEqual(items[3].id, 42)
        self.assertEqual(items[3].title, '42')
        # e — dict
        self.assertEqual(items[4].id, 'e')
        self.assertEqual(items[4].title, 'E')
        self.assertTrue(items[4].has_children)


if __name__ == '__main__':
    unittest.main()
