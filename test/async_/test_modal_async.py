"""Cross-thread modal open core — ticket #1042.

Engine mechanism for opening a dialog from any thread: the single
``_pending_dialog`` slot, the ``_enqueue_dialog`` servicing-prep step (run
during a main-thread drain), the ``_fire_dialog_cb`` exactly-once/caught
callback site, the thread-safe ``open_dialog_async`` entry, and the
``Browser.run`` main-loop servicing step that turns a pending request into a
``run_modal`` call and fires the callback with its result.

These exercise the REAL ``Browser`` (via ``_helpers.make_browser``), not the
``run_modal`` fake in ``test_modal_engine.py``. The servicing-loop tests drive
the real ``Browser.run`` loop headless with a stubbed ``run_modal`` (the loop
calls it by bare name) and a scripted ``read_key`` that quits after one
iteration, so the top-of-loop drain → servicing → callback path runs for real
without a tty.

The callback contract (design ``Callback contract``): every ``on_result``
fires exactly once, on the main thread, with exceptions caught and routed to
``browser.error``. The result is the chosen value, or ``None`` for every
no-answer path (cancel / override-close / displaced-while-pending / headless),
per the one-rule result contract.
"""

import unittest

from test.async_._helpers import (
    Browser, BrowserConfig, make_browser, _state, _term, _context,
)


class _Recorder:
    """A callback that records every call so a test can assert call count
    and the value(s) delivered."""

    def __init__(self):
        self.calls = []

    def __call__(self, value):
        self.calls.append(value)


def _drain_log(b):
    """Run a drain so ``error`` (which posts ``_set_notice``) lands in the
    log, then return the accumulated log text."""
    b.drain_main_queue()
    return '\n'.join(b._log)


# --- _fire_dialog_cb -------------------------------------------------------


class TestFireDialogCb(unittest.TestCase):
    """The single callback-firing site: None no-op, exactly-once, caught."""

    def setUp(self):
        self.b = make_browser()

    def tearDown(self):
        self.b.stop_workers()

    def test_none_callback_is_noop(self):
        # No callback registered → nothing happens, no error logged.
        self.b._fire_dialog_cb(None, 'ignored')
        self.assertEqual(_drain_log(self.b), '')

    def test_fires_exactly_once_with_value(self):
        rec = _Recorder()
        self.b._fire_dialog_cb(rec, 'chosen')
        self.assertEqual(rec.calls, ['chosen'])

    def test_fires_with_none(self):
        rec = _Recorder()
        self.b._fire_dialog_cb(rec, None)
        self.assertEqual(rec.calls, [None])

    def test_throwing_callback_is_caught_and_routed_to_error(self):
        def boom(_value):
            raise ValueError('kaboom')

        # Must NOT propagate.
        self.b._fire_dialog_cb(boom, 'x')
        log = _drain_log(self.b)
        # Routed through browser.error with the exception type + message.
        self.assertIn('dialog callback', log)
        self.assertIn('ValueError', log)
        self.assertIn('kaboom', log)


# --- _enqueue_dialog -------------------------------------------------------


class TestEnqueueDialog(unittest.TestCase):
    """Main-thread enqueue: set the slot, displace a pending request, and
    override an active dialog."""

    def setUp(self):
        self.b = make_browser()

    def tearDown(self):
        self.b.stop_workers()

    def test_sets_pending_when_empty(self):
        rec = _Recorder()
        self.b._enqueue_dialog('content-a', rec, 'center', None)
        self.assertEqual(
            self.b._pending_dialog, ('content-a', rec, 'center', None, None))
        # Nothing fired yet — it hasn't been shown.
        self.assertEqual(rec.calls, [])
        # No active dialog → no force armed.
        self.assertIsNone(self.b._modal_force)

    def test_displaces_pending_firing_old_cb_none(self):
        first = _Recorder()
        second = _Recorder()
        self.b._enqueue_dialog('first', first, 'center', None)
        self.b._enqueue_dialog('second', second, 'center', None)
        # The displaced (never-shown) first request's cb fired once with None.
        self.assertEqual(first.calls, [None])
        # The slot now holds the survivor; its cb has NOT fired.
        self.assertEqual(
            self.b._pending_dialog, ('second', second, 'center', None, None))
        self.assertEqual(second.calls, [])

    def test_override_active_arms_close_dialog_none(self):
        # An active dialog is open; enqueueing overrides it.
        self.b._modal_open = True
        rec = _Recorder()
        self.b._enqueue_dialog('new', rec, 'center', None)
        # The active dialog is force-closed with None (close_dialog(None)).
        self.assertEqual(self.b._modal_force, (None,))
        # The new request is now pending; not fired.
        self.assertEqual(
            self.b._pending_dialog, ('new', rec, 'center', None, None))
        self.assertEqual(rec.calls, [])

    def test_placement_anchor_and_bounds_preserved(self):
        # #1101: the 5-tuple carries ``bounds`` alongside placement/anchor.
        rec = _Recorder()
        self.b._enqueue_dialog('m', rec, 'anchor', ('id-7', 3), (3, 30))
        self.assertEqual(
            self.b._pending_dialog,
            ('m', rec, 'anchor', ('id-7', 3), (3, 30)))


# --- open_dialog_async -----------------------------------------------------


class TestOpenDialogAsyncHeadless(unittest.TestCase):
    """Headless: fire the callback with None immediately, open nothing."""

    def setUp(self):
        self.b = make_browser()  # headless

    def tearDown(self):
        self.b.stop_workers()

    def test_headless_fires_none_immediately_no_pending(self):
        rec = _Recorder()
        self.b.open_dialog_async('content', on_result=rec)
        # Fired inline with None; nothing queued, nothing pending.
        self.assertEqual(rec.calls, [None])
        self.assertIsNone(self.b._pending_dialog)
        self.assertTrue(self.b._main_queue.empty())

    def test_headless_none_callback_is_safe(self):
        # No callback + headless → silent no-op, no crash.
        self.b.open_dialog_async('content')
        self.assertIsNone(self.b._pending_dialog)


class TestOpenDialogAsyncLive(unittest.TestCase):
    """Non-headless: post a closure that enqueues on the next drain."""

    def setUp(self):
        # A non-headless Browser, but we never call run()/term_init — we just
        # exercise the post → drain → _enqueue_dialog path directly.
        self.b = Browser(BrowserConfig(_headless=False))
        self.b.start_workers()

    def tearDown(self):
        self.b.stop_workers()

    def test_posts_and_enqueues_on_drain(self):
        rec = _Recorder()
        self.b.open_dialog_async('content-x', on_result=rec,
                                 placement='anchor', anchor=('a', 1),
                                 bounds=(2, 40))
        # Not enqueued yet — it was posted, not run inline.
        self.assertIsNone(self.b._pending_dialog)
        self.assertFalse(self.b._main_queue.empty())
        # Draining runs the posted closure → _enqueue_dialog.
        self.b.drain_main_queue()
        self.assertEqual(
            self.b._pending_dialog,
            ('content-x', rec, 'anchor', ('a', 1), (2, 40)))
        self.assertEqual(rec.calls, [])  # not shown yet

    def test_two_posts_collapse_in_one_drain_earlier_displaced(self):
        # Two async requests both queued before a drain: the drain runs both
        # _enqueue_dialog calls, the earlier is displaced-while-pending (cb
        # None, never shown), only the later survives in the slot.
        first = _Recorder()
        second = _Recorder()
        self.b.open_dialog_async('first', on_result=first)
        self.b.open_dialog_async('second', on_result=second)
        self.b.drain_main_queue()
        self.assertEqual(first.calls, [None])
        self.assertEqual(second.calls, [])
        self.assertEqual(self.b._pending_dialog[0], 'second')


# --- Browser.run servicing loop --------------------------------------------


class _StubModal:
    """A stand-in for ``run_modal`` wired into the state module for the
    servicing-loop tests.

    Records each call's ``(content, placement, anchor, bounds,
    delay_interaction)`` and returns scripted results in order. Mimics the real
    engine's
    ``_modal_open`` discipline: sets the flag True for the duration of the
    call and clears it on return, so the servicing while-loop's
    ``not self._modal_open`` guard behaves as in production. A per-call
    ``side_effect`` (a callable taking the browser) lets a test mutate state
    mid-"dialog" (e.g. arm a quit) before the result is returned.
    """

    def __init__(self, results, side_effects=None):
        self._results = list(results)
        self._side_effects = list(side_effects or [])
        self.calls = []

    def __call__(self, browser, content, *, placement='center', anchor=None,
                 bounds=None, delay_interaction=False, **kw):
        self.calls.append({
            'content': content, 'placement': placement, 'anchor': anchor,
            'bounds': bounds, 'delay_interaction': delay_interaction,
        })
        browser._modal_open = True
        try:
            if self._side_effects:
                eff = self._side_effects.pop(0)
                if eff is not None:
                    eff(browser)
            return self._results.pop(0) if self._results else None
        finally:
            browser._modal_open = False


class _ServicingDriver:
    """Drives a real headless ``Browser.run`` for exactly one loop iteration.

    Wires the bare-name run-loop deps the isolated state load needs
    (``read_key``/``input_ready``), installs a stub ``run_modal``, then runs
    ``run()``. ``read_key`` arms a quit and returns ``'_notify'`` on its first
    call so the loop services any pending dialog at the top of iteration 1,
    then exits cleanly before any key dispatch.
    """

    def __init__(self, stub_modal):
        self._stub = stub_modal

    def __enter__(self):
        self._orig = {
            'run_modal': getattr(_state, 'run_modal', None),
            'read_key': getattr(_state, 'read_key', None),
            'input_ready': getattr(_state, 'input_ready', None),
            'term_stdout_was_tty': getattr(_state, 'term_stdout_was_tty', None),
        }
        _state.run_modal = self._stub
        _state.input_ready = lambda: False
        # ``_teardown_output`` consults this by bare name; headless never
        # term_init'd, so stdout was not a tty (takes the sys.stdout branch).
        _state.term_stdout_was_tty = _term.term_stdout_was_tty
        return self

    def run(self, browser):
        def rk(*a, **k):
            browser._quit_requested = True
            return '_notify'  # continue → top of loop → while-guard exits

        _state.read_key = rk
        return browser.run()

    def __exit__(self, *exc):
        for name, val in self._orig.items():
            if val is None:
                _state.__dict__.pop(name, None)
            else:
                setattr(_state, name, val)


class TestRunServicing(unittest.TestCase):
    """The ``Browser.run`` servicing step: open a pending dialog via
    ``run_modal`` and fire its callback with the result (dropped on quit)."""

    def _browser(self):
        # Headless, no get_children → empty tree; the servicing while-loop has
        # no _headless guard, so it still runs in the headless loop.
        b = make_browser()
        return b

    def test_choice_fires_callback_once_with_value(self):
        b = self._browser()
        rec = _Recorder()
        b._pending_dialog = ('content', rec, 'center', None, None)
        stub = _StubModal(results=['chosen'])
        try:
            with _ServicingDriver(stub) as drv:
                drv.run(b)
        finally:
            b.stop_workers()
        # run_modal called once with delay_interaction=True and the request's
        # content/placement/anchor; callback fired exactly once with 'chosen'.
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(stub.calls[0]['content'], 'content')
        self.assertTrue(stub.calls[0]['delay_interaction'])
        self.assertEqual(rec.calls, ['chosen'])
        # Slot cleared.
        self.assertIsNone(b._pending_dialog)

    def test_cancel_fires_callback_once_with_none(self):
        b = self._browser()
        rec = _Recorder()
        b._pending_dialog = ('content', rec, 'center', None, None)
        stub = _StubModal(results=[None])  # esc/cancel → None
        try:
            with _ServicingDriver(stub) as drv:
                drv.run(b)
        finally:
            b.stop_workers()
        self.assertEqual(rec.calls, [None])

    def test_placement_anchor_and_bounds_forwarded(self):
        b = self._browser()
        rec = _Recorder()
        b._pending_dialog = ('m', rec, 'anchor', ('id-3', 9), (3, 30))
        stub = _StubModal(results=['ok'])
        try:
            with _ServicingDriver(stub) as drv:
                drv.run(b)
        finally:
            b.stop_workers()
        self.assertEqual(stub.calls[0]['placement'], 'anchor')
        self.assertEqual(stub.calls[0]['anchor'], ('id-3', 9))
        self.assertEqual(stub.calls[0]['bounds'], (3, 30))

    def test_slot_sentinel_resolved_on_main_thread(self):
        # #1101: an async menu/pick posts ``anchor='slot'`` (it can't read the
        # live layout from a worker). The servicing step resolves it HERE via
        # ``_modal_anchor_placement`` — with the slot set to (y, L, R) it yields
        # placement='anchor', anchor=(y, L), bounds=(L, R).
        b = self._browser()
        rec = _Recorder()
        b._modal_anchor = (7, 3, 30)
        b._pending_dialog = ('m', rec, 'center', 'slot', None)
        stub = _StubModal(results=['ok'])
        try:
            with _ServicingDriver(stub) as drv:
                drv.run(b)
        finally:
            b.stop_workers()
        self.assertEqual(stub.calls[0]['placement'], 'anchor')
        self.assertEqual(stub.calls[0]['anchor'], (7, 3))
        self.assertEqual(stub.calls[0]['bounds'], (3, 30))
        self.assertEqual(rec.calls, ['ok'])

    def test_slot_sentinel_falls_back_to_centered(self):
        # With nothing derivable (no slot, no resolvable list pane) the
        # sentinel resolves to a centered placement. Stub the two layout
        # bare-names the standalone seed path consults so it returns no list
        # pane (the isolated async load doesn't wire 050-render).
        b = self._browser()
        rec = _Recorder()
        b._modal_anchor = None
        b._pending_dialog = ('m', rec, 'center', 'slot', None)
        stub = _StubModal(results=['ok'])
        # ``_modal_anchor_placement`` and the ``_list_pane_rect`` chain live in
        # 060-context, so the bare-name layout deps resolve in that module.
        orig_ts = getattr(_context, 'term_size', None)
        orig_lp = getattr(_context, 'layout_panes', None)
        _context.term_size = lambda: (80, 24)
        _context.layout_panes = lambda *a, **k: {}   # no 'list' pane
        try:
            with _ServicingDriver(stub) as drv:
                drv.run(b)
        finally:
            b.stop_workers()
            if orig_ts is None:
                _context.__dict__.pop('term_size', None)
            else:
                _context.term_size = orig_ts
            if orig_lp is None:
                _context.__dict__.pop('layout_panes', None)
            else:
                _context.layout_panes = orig_lp
        self.assertEqual(stub.calls[0]['placement'], 'center')
        self.assertIsNone(stub.calls[0]['anchor'])

    def test_quit_during_dialog_drops_callback(self):
        # The dialog resolves but a quit was requested while it was open: the
        # servicing step must NOT fire the callback (quit contract).
        b = self._browser()
        rec = _Recorder()
        b._pending_dialog = ('content', rec, 'center', None, None)
        stub = _StubModal(
            results=['would-be-result'],
            side_effects=[lambda br: setattr(br, '_quit_requested', True)])
        try:
            with _ServicingDriver(stub) as drv:
                drv.run(b)
        finally:
            b.stop_workers()
        # run_modal ran, but its callback was dropped on the quit-break.
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(rec.calls, [])

    def test_override_active_then_new_shown(self):
        # An override that arrives while a dialog is on screen: the active
        # dialog's callback fires None (from close_dialog → run_modal returns
        # None), then the new dialog is shown and its callback fires its value.
        #
        # Model the real sequence end-to-end through the servicing loop:
        #   * pending #1 (active.cb) is shown first; while it's "open", its
        #     side-effect enqueues pending #2 (new.cb), which overrides it —
        #     close_dialog(None) arms _modal_force, so #1's run_modal returns
        #     None (the stub honors the force the same way the engine does).
        #   * the servicing while-loop then shows #2 and fires new.cb.
        b = self._browser()
        active = _Recorder()
        new = _Recorder()
        b._pending_dialog = ('active-content', active, 'center', None, None)

        def enqueue_override(br):
            # Runs "inside" dialog #1; overrides it with dialog #2.
            br._enqueue_dialog('new-content', new, 'center', None)

        # First run_modal: honor the force armed by the override (return its
        # value, like the real engine). Second run_modal: normal choice.
        class _OverrideStub(_StubModal):
            def __call__(self, browser, content, **kw):
                self.calls.append({'content': content,
                                   'placement': kw.get('placement', 'center'),
                                   'anchor': kw.get('anchor'),
                                   'delay_interaction':
                                       kw.get('delay_interaction', False)})
                browser._modal_open = True
                try:
                    if self._side_effects:
                        eff = self._side_effects.pop(0)
                        if eff is not None:
                            eff(browser)
                    # Honor a force armed during this call (override path).
                    if browser._modal_force is not None:
                        val = browser._modal_force[0]
                        browser._modal_force = None
                        return val
                    return self._results.pop(0) if self._results else None
                finally:
                    browser._modal_open = False

        stub = _OverrideStub(results=['new-result'],
                             side_effects=[enqueue_override, None])
        try:
            with _ServicingDriver(stub) as drv:
                drv.run(b)
        finally:
            b.stop_workers()
        # Two dialogs shown: active then new.
        self.assertEqual([c['content'] for c in stub.calls],
                         ['active-content', 'new-content'])
        # Active dialog's callback fired exactly once with None (overridden);
        # the new dialog's callback fired once with its value.
        self.assertEqual(active.calls, [None])
        self.assertEqual(new.calls, ['new-result'])
        self.assertIsNone(b._pending_dialog)


if __name__ == '__main__':
    unittest.main()
