"""Tests for search semantics (ticket #22).

Covers the search helpers in 040-state.py and the search-mode key
dispatch in 070-actions.py:

  * ``_search_text``    — composes the searchable haystack for one Item.
  * ``_search_matches`` — fragment-AND substring match (case-insensitive).
  * ``_search_find``    — walks the visible list to find next/prev match.
  * search-mode dispatch — typing/enter/shift-enter jump the cursor.

Mirrors plan-tui's pattern (plan-source/src-tui/070-main.py:6-46) ported
to browse-tui's parameterised state.
"""

import unittest

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_actions = load('_browse_tui_actions', '070-actions.py')

# Wire cross-module names — the production single-file build resolves
# them via concatenation; the loader needs them by hand.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

_actions.visible_items = _state.visible_items
_actions.mark_visible_dirty = _state.mark_visible_dirty
_actions._search_find = _state._search_find
_actions._search_jump_nearest = _state._search_jump_nearest
_actions.mark_cursor_changed = _state.mark_cursor_changed
_actions._resolve_landing = _state._resolve_landing
_actions.PIN_FIRST = _state.PIN_FIRST
_actions.PIN_LAST = _state.PIN_LAST
_actions.Mode = _state.Mode


Item = _data.Item
State = _state.State
VisibleEntry = _state.VisibleEntry
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Mode = _state.Mode
visible_items = _state.visible_items
_search_text = _state._search_text
_search_matches = _state._search_matches
_search_find = _state._search_find
_search_jump_nearest = _state._search_jump_nearest
dispatch_key = _actions.dispatch_key


def _make_browser(**kw):
    """Build a headless Browser; tests call stop_workers in tearDown."""
    kw.setdefault('_headless', True)
    return Browser(BrowserConfig(**kw))


def _ctx_for(browser):
    """Build a real Context for the given browser."""
    _context = load('_browse_tui_context', '060-context.py')
    _context.visible_items = _state.visible_items
    return _context.Context(browser)


# --- _search_text ---------------------------------------------------------


class TestSearchText(unittest.TestCase):

    def test_includes_id_and_title(self):
        item = Item(id='foo', title='Hello World')
        text = _search_text(item)
        self.assertIn('foo', text)
        self.assertIn('Hello World', text)

    def test_includes_tag_in_brackets(self):
        item = Item(id='5', title='thing', tag='open')
        text = _search_text(item)
        self.assertIn('[open]', text)
        self.assertIn('5', text)
        self.assertIn('thing', text)

    def test_no_tag_no_brackets(self):
        item = Item(id='x', title='no tag here')
        text = _search_text(item)
        self.assertNotIn('[', text)
        self.assertNotIn(']', text)

    def test_id_omitted_when_show_ids_never(self):
        # When the recipe sets show_ids='never', the id segment is never
        # rendered. Matching against it would surface "ghost matches" on
        # rows where the user can't see the id. Align with the renderer.
        item = Item(id='/path/with/foo/sess.jsonl', title='sess')
        text = _search_text(item, show_ids='never')
        self.assertNotIn('/path/with/foo/', text)
        self.assertIn('sess', text)

    def test_id_kept_when_show_ids_auto_and_differs_from_title(self):
        # Default 'auto' keeps id when it'd be rendered (id != title).
        item = Item(id='foo-id', title='Title')
        text = _search_text(item, show_ids='auto')
        self.assertIn('foo-id', text)
        self.assertIn('Title', text)

    def test_id_omitted_when_show_ids_auto_and_equals_title(self):
        # Auto suppresses id when id == title (duplication).
        item = Item(id='same', title='same')
        text = _search_text(item, show_ids='auto')
        # id segment isn't repeated; text is just 'same'.
        self.assertEqual(text, 'same')

    def test_id_omitted_when_show_ids_auto_and_non_scalar(self):
        # A structured (non-scalar) id is routing state, never rendered in
        # 'auto' — so it must not leak into the search haystack either.
        item = Item(id=('launch', 1, 'file', '/x/a.md'), title='a.md')
        text = _search_text(item, show_ids='auto')
        self.assertNotIn('launch', text)
        self.assertEqual(text, 'a.md')

    def test_id_kept_when_show_ids_always(self):
        item = Item(id='same', title='same')
        text = _search_text(item, show_ids='always')
        # Both id and title appear.
        self.assertEqual(text, 'same same')

    def test_scope_title_used_when_is_current_scope(self):
        # The scope row shows scope_title in place of title — search
        # should match against that label, not the listing-pane title.
        item = Item(id='sess-x', title='sess-x')
        item.scope_title = '/full/path/to/x.jsonl'
        text = _search_text(item, is_current_scope=True)
        self.assertIn('/full/path/to/x.jsonl', text)
        self.assertNotIn('sess-x sess-x', text)  # title replaced

    def test_scope_title_ignored_when_not_current_scope(self):
        item = Item(id='sess-x', title='sess-x')
        item.scope_title = '/full/path'
        text = _search_text(item, is_current_scope=False)
        self.assertNotIn('/full/path', text)
        self.assertIn('sess-x', text)


# --- _search_matches ------------------------------------------------------


class TestSearchMatches(unittest.TestCase):

    def test_empty_query_no_match(self):
        self.assertFalse(_search_matches('hello world', ''))

    def test_whitespace_only_query_no_match(self):
        # plan-tui treats empty fragments after split() as a no-match.
        self.assertFalse(_search_matches('hello world', '   '))

    def test_single_fragment_substring(self):
        self.assertTrue(_search_matches('hello world', 'hell'))
        self.assertFalse(_search_matches('hello world', 'xyz'))

    def test_case_insensitive(self):
        self.assertTrue(_search_matches('Hello World', 'hello'))
        self.assertTrue(_search_matches('hello world', 'WORLD'))

    def test_two_fragments_AND_match(self):
        # Both fragments must appear (in any order, anywhere).
        self.assertTrue(_search_matches('foo bar baz', 'foo baz'))
        self.assertTrue(_search_matches('foo bar baz', 'baz foo'))

    def test_two_fragments_one_missing_no_match(self):
        self.assertFalse(_search_matches('foo bar', 'foo zip'))

    def test_fragment_in_tag(self):
        # The haystack returned by _search_text includes '[tag]' so a
        # query of 'open' should match a row whose tag is 'open' even if
        # the title doesn't contain that substring.
        item = Item(id='5', title='thing', tag='open')
        self.assertTrue(_search_matches(_search_text(item), 'open'))


# --- _search_find ---------------------------------------------------------


class TestSearchFind(unittest.TestCase):

    def _state_with(self, items):
        s = State()
        s._children[None] = items
        return s

    def test_finds_forward_from_cursor(self):
        s = self._state_with([
            Item(id='alpha'), Item(id='beta'), Item(id='gamma'),
        ])
        # Visible list: alpha(0), beta(1), gamma(2). Searching 'gam'
        # forward from cursor=0 should land on idx 2.
        self.assertEqual(_search_find(s, 'gam', 0, 1), 2)

    def test_finds_backward_from_cursor(self):
        s = self._state_with([
            Item(id='alpha'), Item(id='beta'), Item(id='gamma'),
        ])
        # Backward from cursor=2 looking for 'alp' wraps to idx 0.
        self.assertEqual(_search_find(s, 'alp', 2, -1), 0)

    def test_wraps_around(self):
        s = self._state_with([
            Item(id='alpha'), Item(id='beta'), Item(id='alpha2', title='alpha2'),
        ])
        # Forward from idx 2 looking for 'alp' wraps back to idx 0
        # (wrap-around order: 0, 1, 2 again would also be 'alpha2', but
        # ``_search_find`` starts at start+1*direction so from 2 the
        # next index is 0).
        self.assertEqual(_search_find(s, 'alp', 2, 1), 0)

    def test_can_land_on_scope_row(self):
        # After scope-root unification the scope row is a normal row
        # at depth 0 and is searchable like any other.
        s = State(root_id='proj')
        s.scope_stack = ['proj']
        s._children['proj'] = [Item(id='other', title='other')]
        # Pre-build the visible list to confirm the scope row is normal.
        vis = visible_items(s)
        self.assertEqual(vis[0].kind, 'normal')
        self.assertEqual(vis[0].item.id, 'proj')
        # Searching for the scope-row id should find idx 0.
        self.assertEqual(_search_find(s, 'proj', 1, 1), 0)

    def test_show_ids_never_excludes_path_ids_from_matching(self):
        # browse-claude style: voice rows carry path-prefixed ids and
        # show_ids='never'. Searching for a path fragment must NOT
        # match every voice row (the rendered text doesn't include the
        # path). Only the scope row, whose rendered scope_title
        # carries the path, should match.
        path = '/home/u/proj/sess.jsonl'
        scope_item = Item(id=path, title='sess', has_children=True)
        scope_item.scope_title = path
        v0 = Item(id=f'{path}#0', title='user: hi')
        v1 = Item(id=f'{path}#1', title='assistant: hello')
        s = State(root_id='__ROOT__')
        s.scope_stack = [path]
        s._children[path] = [v0, v1]
        s._items_by_id[path] = scope_item
        # With show_ids='never', searching 'home' should match only
        # the scope row (its scope_title contains '/home/'); v0/v1
        # rendered titles do not contain 'home'.
        idx = _search_find(s, 'home', -1, 1, show_ids='never')
        self.assertEqual(idx, 0)  # scope row
        # Cursor advance from scope row → wrap, find scope row again.
        # (Direction-1 walk starting just past idx 0 returns to it.)
        idx2 = _search_find(s, 'home', 0, 1, show_ids='never')
        self.assertEqual(idx2, 0)  # only one match

    def test_no_match_returns_none(self):
        s = self._state_with([Item(id='alpha'), Item(id='beta')])
        self.assertIsNone(_search_find(s, 'xyz', 0, 1))

    def test_empty_query_returns_none(self):
        s = self._state_with([Item(id='alpha')])
        self.assertIsNone(_search_find(s, '', 0, 1))

    def test_skips_meta_rows_by_default(self):
        # #740: search navigation never lands on a meta row. The meta
        # row's text matches 'sep', but ``_search_find`` skips it (the
        # ``kind != 'normal'`` guard) and finds the normal row instead.
        s = self._state_with([
            Item(id='alpha'),
            Item(id='sep', title='sep divider', meta=True),
            Item(id='sep-real', title='sep content'),
        ])
        # Sanity: the visible list has the meta row at idx 1.
        vis = visible_items(s)
        self.assertEqual(vis[1].kind, 'meta')
        # Forward from idx 0 for 'sep': skip meta idx 1, land on idx 2.
        self.assertEqual(_search_find(s, 'sep', 0, 1), 2)

    def test_meta_only_match_returns_none(self):
        # When the ONLY row whose text matches is a meta row, search
        # finds nothing — meta never participates in navigation.
        s = self._state_with([
            Item(id='alpha'),
            Item(id='sep', title='unique-meta', meta=True),
            Item(id='beta'),
        ])
        self.assertIsNone(_search_find(s, 'unique-meta', 0, 1))


# --- search-mode key dispatch --------------------------------------------


class TestSearchModeDispatch(unittest.TestCase):

    def _browser_with_items(self, ids):
        b = _make_browser()
        b._state._children[None] = [Item(id=x) for x in ids]
        return b

    def test_typing_in_search_mode_jumps_cursor_to_nearest_match(self):
        b = self._browser_with_items(['foo', 'bar', 'baz', 'qux'])
        b._mode = Mode.SEARCH_EDIT
        try:
            ctx = _ctx_for(b)
            # Cursor starts at 0; typing 'baz' should land cursor on idx 2.
            dispatch_key(b, ctx, 'b')
            dispatch_key(b, ctx, 'a')
            dispatch_key(b, ctx, 'z')
            self.assertEqual(b._search_query, 'baz')
            self.assertEqual(b._state.cursor, 2)
        finally:
            b.stop_workers()

    def test_enter_jumps_to_next_match(self):
        b = self._browser_with_items(['foo-a', 'bar', 'foo-b', 'foo-c'])
        b._mode = Mode.SEARCH_EDIT
        b._search_query = 'foo'
        b._state.cursor = 0  # already on first match
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, 'enter')
            # Should advance to next match (idx 2).
            self.assertEqual(b._state.cursor, 2)
            dispatch_key(b, ctx, 'enter')
            # Then idx 3.
            self.assertEqual(b._state.cursor, 3)
            dispatch_key(b, ctx, 'enter')
            # Wraps back to idx 0.
            self.assertEqual(b._state.cursor, 0)
        finally:
            b.stop_workers()

    def test_shift_enter_jumps_to_previous(self):
        b = self._browser_with_items(['foo-a', 'bar', 'foo-b', 'foo-c'])
        b._mode = Mode.SEARCH_EDIT
        b._search_query = 'foo'
        b._state.cursor = 2
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, 'shift-enter')
            # Previous match before idx 2 is idx 0.
            self.assertEqual(b._state.cursor, 0)
            dispatch_key(b, ctx, 'shift-enter')
            # Wraps to idx 3.
            self.assertEqual(b._state.cursor, 3)
        finally:
            b.stop_workers()

    def test_esc_clears_query_and_exits_search_mode(self):
        # Mirrors plan-tui: Esc clears the query so highlights vanish,
        # but the cursor stays on whatever match the user landed on.
        b = self._browser_with_items(['foo', 'bar', 'baz'])
        b._mode = Mode.SEARCH_EDIT
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, 'b')
            dispatch_key(b, ctx, 'a')
            dispatch_key(b, ctx, 'z')
            cursor_before = b._state.cursor
            self.assertEqual(cursor_before, 2)
            dispatch_key(b, ctx, 'esc')
            self.assertIs(b._mode, Mode.NORMAL)
            self.assertEqual(b._search_query, '')
            # Cursor stays put — the user's landing position is preserved.
            self.assertEqual(b._state.cursor, cursor_before)
        finally:
            b.stop_workers()

    def test_ctrl_c_in_search_mode_exits_and_clears_query(self):
        # ctrl-c is treated as a synonym for esc inside search mode —
        # universal abort. Clears the query and exits search mode.
        b = self._browser_with_items(['foo', 'bar'])
        b._mode = Mode.SEARCH_EDIT
        b._search_query = 'fo'
        try:
            ctx = _ctx_for(b)
            handled = dispatch_key(b, ctx, 'ctrl-c')
            self.assertTrue(handled)
            self.assertIs(b._mode, Mode.NORMAL)
            self.assertEqual(b._search_query, '')
        finally:
            b.stop_workers()

    def test_alt_enter_jumps_to_previous_like_shift_enter(self):
        # Alt-Enter is bound alongside Shift-Enter (terminals that swallow
        # the latter still have a way to walk matches backwards).
        b = self._browser_with_items(['foo-a', 'bar', 'foo-b', 'foo-c'])
        b._mode = Mode.SEARCH_EDIT
        b._search_query = 'foo'
        b._state.cursor = 2
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, 'alt-enter')
            # Previous match before idx 2 is idx 0.
            self.assertEqual(b._state.cursor, 0)
            dispatch_key(b, ctx, 'alt-enter')
            # Wraps to idx 3.
            self.assertEqual(b._state.cursor, 3)
        finally:
            b.stop_workers()

    def test_ctrl_w_kills_trailing_word(self):
        b = self._browser_with_items(['foo'])
        b._mode = Mode.SEARCH_EDIT
        b._search_query = 'foo bar'
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'ctrl-w'))
            # Strip trailing 'bar' — trailing-space convention keeps the
            # ' ' before so the user can keep typing a new fragment.
            self.assertEqual(b._search_query, 'foo ')
            self.assertTrue(dispatch_key(b, ctx, 'ctrl-w'))
            # Now strip the lone trailing space + 'foo'.
            self.assertEqual(b._search_query, '')
        finally:
            b.stop_workers()

    def test_ctrl_w_strips_trailing_spaces_then_word(self):
        # Readline convention: ctrl-w on "foo bar   " (trailing spaces)
        # consumes the spaces AND the next word in one stroke.
        b = self._browser_with_items(['foo'])
        b._mode = Mode.SEARCH_EDIT
        b._search_query = 'foo bar   '
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, 'ctrl-w')
            self.assertEqual(b._search_query, 'foo ')
        finally:
            b.stop_workers()

    def test_ctrl_w_on_empty_query_is_noop(self):
        b = self._browser_with_items(['foo'])
        b._mode = Mode.SEARCH_EDIT
        b._search_query = ''
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'ctrl-w'))
            self.assertEqual(b._search_query, '')
        finally:
            b.stop_workers()

    def test_ctrl_u_clears_query(self):
        b = self._browser_with_items(['foo'])
        b._mode = Mode.SEARCH_EDIT
        b._search_query = 'foo bar baz'
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'ctrl-u'))
            self.assertEqual(b._search_query, '')
            # Stays in search mode (just the line was killed).
            self.assertIs(b._mode, Mode.SEARCH_EDIT)
        finally:
            b.stop_workers()

    def test_arrow_down_navigates_during_search(self):
        # Non-letter navigation keys fall through to the normal dispatch
        # so the user can still walk the list while a query is composed.
        b = self._browser_with_items(['a', 'b', 'c', 'd'])
        b._mode = Mode.SEARCH_EDIT
        b._search_query = ''
        b._state.cursor = 0
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'down'))
            self.assertEqual(b._state.cursor, 1)
            self.assertTrue(dispatch_key(b, ctx, 'down'))
            self.assertEqual(b._state.cursor, 2)
            # Query stays empty — arrow keys do not extend it.
            self.assertEqual(b._search_query, '')
            # Still in search mode.
            self.assertIs(b._mode, Mode.SEARCH_EDIT)
        finally:
            b.stop_workers()

    def test_pgdn_navigates_during_search(self):
        b = self._browser_with_items([f'item-{i}' for i in range(50)])
        b._mode = Mode.SEARCH_EDIT
        b._state.cursor = 0
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'pgdn'))
            # PgDn moves by a page. Cursor must have advanced past 0.
            self.assertGreater(b._state.cursor, 0)
            self.assertIs(b._mode, Mode.SEARCH_EDIT)
        finally:
            b.stop_workers()

    def test_home_navigates_during_search(self):
        b = self._browser_with_items(['a', 'b', 'c', 'd'])
        b._mode = Mode.SEARCH_EDIT
        b._state.cursor = 3
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'home'))
            self.assertEqual(b._state.cursor, 0)
        finally:
            b.stop_workers()

    def test_backspace_re_jumps_to_match(self):
        # After deleting a char, the cursor should land on a match for
        # the trimmed query. ``_search_jump_nearest`` passes ``cursor-1``
        # to ``_search_find`` so the cursor row itself is the first
        # candidate — if the current row still matches the shorter
        # query, the cursor stays put. Concretely: cursor on 'baz' (idx
        # 2), query shrinks 'baz' → 'ba'; 'baz' still matches 'ba' so
        # the cursor stays at 2.
        b = self._browser_with_items(['foo', 'bar', 'baz', 'qux'])
        b._mode = Mode.SEARCH_EDIT
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, 'b')
            dispatch_key(b, ctx, 'a')
            dispatch_key(b, ctx, 'z')
            self.assertEqual(b._state.cursor, 2)
            dispatch_key(b, ctx, 'backspace')
            self.assertEqual(b._search_query, 'ba')
            # 'baz' still matches 'ba' — cursor sticks. (Plan-tui's same
            # behaviour: the user's landing point is preserved as long
            # as the trimmed query keeps it valid.)
            self.assertEqual(b._state.cursor, 2)
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
