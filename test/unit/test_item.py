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


class TestToItemFromStr(unittest.TestCase):
    """to_item with a str produces a leaf Item."""

    def test_str_becomes_id_and_title(self):
        item = to_item('foo')
        self.assertEqual(item.id, 'foo')
        self.assertEqual(item.title, 'foo')
        self.assertFalse(item.has_children)


class TestToItemFromTuple(unittest.TestCase):
    """to_item with a tuple uses positional dataclass-init order."""

    def test_empty_tuple_raises(self):
        # Empty tuples have no id; treated as an unsupported shape.
        with self.assertRaises(TypeError):
            to_item(())

    def test_single_element(self):
        item = to_item(('x',))
        self.assertEqual(item.id, 'x')
        self.assertEqual(item.title, 'x')

    def test_two_elements(self):
        item = to_item(('x', 'X'))
        self.assertEqual(item.id, 'x')
        self.assertEqual(item.title, 'X')

    def test_three_elements_with_tag(self):
        item = to_item(('x', 'X', 'tag'))
        self.assertEqual(item.id, 'x')
        self.assertEqual(item.title, 'X')
        self.assertEqual(item.tag, 'tag')

    def test_four_elements_with_tag_style(self):
        item = to_item(('x', 'X', 'tag', 'green'))
        self.assertEqual(item.tag, 'tag')
        self.assertEqual(item.tag_style, 'green')

    def test_five_elements_with_has_children(self):
        item = to_item(('x', 'X', 'tag', 'green', True))
        self.assertEqual(item.id, 'x')
        self.assertEqual(item.title, 'X')
        self.assertEqual(item.tag, 'tag')
        self.assertEqual(item.tag_style, 'green')
        self.assertTrue(item.has_children)

    def test_six_elements_with_hidden(self):
        item = to_item(('x', 'X', 'tag', 'green', True, True))
        self.assertEqual(item.id, 'x')
        self.assertTrue(item.has_children)
        self.assertTrue(item.hidden)

    def test_seven_or_more_elements_raises(self):
        # 7+ elements have no field to land in; reject explicitly.
        with self.assertRaises(TypeError):
            to_item(('x', 'X', 'tag', 'green', True, False, 'extra'))


class TestToItemFromDict(unittest.TestCase):
    """to_item with a dict uses Item(**d) and tolerates extra keys."""

    def test_basic_dict(self):
        item = to_item({'id': 'x', 'title': 'X', 'has_children': True})
        self.assertEqual(item.id, 'x')
        self.assertEqual(item.title, 'X')
        self.assertTrue(item.has_children)

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


class TestToItemUnsupported(unittest.TestCase):
    """to_item raises TypeError for shapes outside the documented set."""

    def test_int_raises(self):
        with self.assertRaises(TypeError):
            to_item(42)

    def test_none_raises(self):
        with self.assertRaises(TypeError):
            to_item(None)

    def test_list_raises(self):
        # Lists are NOT coerced — callers iterate and call to_item per element.
        with self.assertRaises(TypeError):
            to_item(['x', 'y'])

    def test_set_raises(self):
        with self.assertRaises(TypeError):
            to_item({'x', 'y'})


class TestMixedListCoercion(unittest.TestCase):
    """Mixed iterables are coerced element-wise by the caller."""

    def test_mixed_list(self):
        raw = [
            Item(id='a'),
            'b',
            ('c', 'C'),
            ('d', 'D', 'tag'),
            {'id': 'e', 'title': 'E', 'has_children': True},
        ]
        items = [to_item(x) for x in raw]
        self.assertEqual(len(items), 5)
        # a — passthrough Item
        self.assertEqual(items[0].id, 'a')
        self.assertIs(items[0], raw[0])
        # b — string
        self.assertEqual(items[1].id, 'b')
        self.assertEqual(items[1].title, 'b')
        # c — 2-tuple
        self.assertEqual(items[2].id, 'c')
        self.assertEqual(items[2].title, 'C')
        # d — 3-tuple
        self.assertEqual(items[3].id, 'd')
        self.assertEqual(items[3].title, 'D')
        self.assertEqual(items[3].tag, 'tag')
        # e — dict
        self.assertEqual(items[4].id, 'e')
        self.assertEqual(items[4].title, 'E')
        self.assertTrue(items[4].has_children)


if __name__ == '__main__':
    unittest.main()
