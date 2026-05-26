"""Regression: full refresh must re-dispatch every expanded parent (#294).

Before the fix, ``Browser.refresh()`` (with no id) called
``cache_invalidate_all`` and then dispatched a fetch only for
``state.root_id``. Expanded sub-parents lost their ``_children`` entries
with no re-dispatch attached -- the visible-tree builder emitted a
``⧗ loading…`` placeholder for each and the only auto-dispatch was
``_update_children_for_cursor`` (which fires for the cursor item only).
Result: expanded sub-trees stuck "loading" until the user navigated onto
each parent.

After the fix, ``_do_refresh(None, …)`` snapshots ``state.expanded`` (and
the current scope root) before invalidation and enqueues a fetch for each.
"""

import threading
import unittest

from test.async_._helpers import Item, make_browser


def _tree_get_children(parent_id, *, reload=False):
    """Minimal tree: / → a → a/b → a/b/c, a/b/c has one child a/b/c/leaf."""
    if parent_id is None or parent_id == '/':
        return [Item(id='a', title='A', has_children=True)]
    if parent_id == 'a':
        return [Item(id='a/b', title='A/B', has_children=True)]
    if parent_id == 'a/b':
        return [Item(id='a/b/c', title='A/B/C', has_children=True)]
    if parent_id == 'a/b/c':
        return [Item(id='a/b/c/leaf', title='leaf')]
    return []


class TestInitialFetchScopeStack(unittest.TestCase):
    """Initial fetch only walks the current scope (top of stack), not
    every level. Deeper levels are lazy-fetched by ``_scope_up`` on
    demand so a pre-pushed deep stack with expensive levels doesn't
    pay for fetches the user may never reach.
    """

    def test_initial_fetch_only_fetches_current_scope(self):
        import threading
        calls = []
        lock = threading.Lock()

        def tracked(parent_id, *, reload=False):
            with lock:
                calls.append(parent_id)
            return _tree_get_children(parent_id)

        b = make_browser(get_children=tracked, root_id='/')
        # Pre-push a deep scope stack BEFORE startup.
        b._state.scope_stack[:] = ['a', 'a/b']
        try:
            b.post(b._do_initial_fetch)
            b.run_until_idle()
            # Only the current scope (top of stack) and root got fetched.
            self.assertIn('a/b', calls,
                          f'top scope level not fetched; calls={calls!r}')
            self.assertNotIn(
                'a', calls,
                f'lower scope level should NOT be pre-fetched (lazy '
                f'load on scope_up instead); calls={calls!r}',
            )
            self.assertIn('a/b', b._state._children)
            self.assertNotIn(
                'a', b._state._children,
                'lower scope level should not have cached children '
                'until the user scope-ups into it',
            )
        finally:
            b.stop_workers()


class TestFullRefreshExpanded(unittest.TestCase):

    def _expand_chain(self, b):
        """Expand / → a → a/b → a/b/c and wait until everything is cached."""
        b.refresh('/')
        b.run_until_idle()
        b.expand('a')
        b.expand('a/b')
        b.expand('a/b/c')
        b.run_until_idle()
        # Sanity: full chain is cached.
        self.assertIn('a', b._state._children)
        self.assertIn('a/b', b._state._children)
        self.assertIn('a/b/c', b._state._children)

    def test_full_refresh_redispatches_every_expanded_parent(self):
        calls = []
        lock = threading.Lock()

        def tracked(parent_id, *, reload=False):
            with lock:
                calls.append(parent_id)
            return _tree_get_children(parent_id)

        b = make_browser(get_children=tracked, root_id='/')
        try:
            self._expand_chain(b)
            calls_before = list(calls)
            self.assertEqual(
                sorted(calls_before), sorted(['/', 'a', 'a/b', 'a/b/c']),
                'expand chain should have fetched every parent once',
            )

            # Full refresh.
            b.refresh()
            b.run_until_idle()

            # Every previously-expanded parent must have been re-dispatched.
            new_calls = [pid for pid in calls if pid not in calls_before
                         or calls.index(pid) >= len(calls_before)]
            # Easier check: count occurrences after the refresh.
            after = calls[len(calls_before):]
            self.assertEqual(
                sorted(after), sorted(['/', 'a', 'a/b', 'a/b/c']),
                f'full refresh should re-fetch root and every expanded '
                f'parent; saw {after!r}',
            )
        finally:
            b.stop_workers()

    def test_full_refresh_repopulates_children_cache_for_expanded(self):
        b = make_browser(get_children=_tree_get_children, root_id='/')
        try:
            self._expand_chain(b)
            b.refresh()
            b.run_until_idle()
            # Each expanded parent has its children back in cache.
            for pid in ['/', 'a', 'a/b', 'a/b/c']:
                self.assertIn(pid, b._state._children,
                              f'_children missing entry for {pid!r}')
                self.assertEqual(b._state._loading.get(pid), False,
                                 f'_loading[{pid!r}] should be False after refresh')
        finally:
            b.stop_workers()

    def test_partial_refresh_unchanged(self):
        """Partial refresh (``refresh(specific_id)``) must NOT re-fetch
        ancestors or other expanded branches — that contract is unchanged.
        """
        calls = []
        lock = threading.Lock()

        def tracked(parent_id, *, reload=False):
            with lock:
                calls.append(parent_id)
            return _tree_get_children(parent_id)

        b = make_browser(get_children=tracked, root_id='/')
        try:
            self._expand_chain(b)
            calls_before = list(calls)

            # Partial refresh of 'a/b' only.
            b.refresh('a/b')
            b.run_until_idle()

            after = calls[len(calls_before):]
            self.assertEqual(after, ['a/b'],
                             f'partial refresh should only re-fetch the named id; '
                             f'saw {after!r}')
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
