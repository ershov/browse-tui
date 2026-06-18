"""Context async dialog variants — ticket #1043.

The public cross-thread dialog surface: ``ctx.confirm_async`` /
``alert_async`` / ``pick_async`` / ``menu_async`` / ``input_async``. Each is
callable FROM ANY THREAD: it builds the SAME content object its blocking
sibling builds (``ChoiceContent`` / ``ListContent`` / ``InputContent``) and
delegates to :meth:`Browser.open_dialog_async`, which posts the open onto the
main thread and fires ``on_result`` there (exactly once, caught) with the
chosen value or ``None`` for any no-answer path.

Two layers of coverage:

* **Unit** — spy on ``browser.open_dialog_async`` to capture the content +
  ``placement`` / ``anchor`` each method builds (so the async dialog looks and
  behaves identically to its blocking sibling), plus the two special cases the
  design calls out: ``alert_async`` always delivers ``None`` (an alert conveys
  nothing back), and an EMPTY ``pick_async`` / ``menu_async`` fires
  ``on_result(None)`` WITHOUT opening — once, on the main thread, caught,
  mirroring the blocking wrappers' empty short-circuit.
* **End-to-end** — a real ``ctx.confirm_async`` invoked from a background
  thread against a real ``Browser`` whose ``run()`` loop is driven (stubbed
  ``run_modal`` + scripted ``read_key``, the #1042 ``TestRunServicing``
  harness), asserting the callback fires ON THE MAIN THREAD with the scripted
  result — exercising the full post -> drain -> servicing -> run_modal ->
  callback path, which the headless inline short-circuit would otherwise skip.
"""

import threading
import unittest

from test.async_._helpers import (
    Browser, BrowserConfig, make_browser, _state, _term, Context,
)
from test.unit._loader import load


# The content classes the ctx async methods build live in 055-modal.py; the
# concatenated build resolves them by bare name, the isolated test load injects
# them. Constructing them touches no terminal/render names (only ``__init__``),
# so no further wiring is needed to assert the content a method built.
_modal = load('_browse_tui_modal_ctxasync', '055-modal.py')
_context = load('_browse_tui_context_ctxasync', '060-context.py')
_context.visible_items = _state.visible_items
_context.ChoiceContent = _modal.ChoiceContent
_context.ListContent = _modal.ListContent
_context.InputContent = _modal.InputContent

ChoiceContent = _modal.ChoiceContent
ListContent = _modal.ListContent
InputContent = _modal.InputContent
CtxAsync = _context.Context


class _Recorder:
    """A callback that records every call, for asserting count + value(s)."""

    def __init__(self):
        self.calls = []

    def __call__(self, value):
        self.calls.append(value)


class _Spy:
    """Records each ``open_dialog_async`` call's content + kwargs.

    A test installs it over ``browser.open_dialog_async`` so a ctx method's
    delegation is captured without driving the real post -> servicing path.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, content, *, on_result=None, placement='center',
                 anchor=None, bounds=None):
        self.calls.append({
            'content': content, 'on_result': on_result,
            'placement': placement, 'anchor': anchor, 'bounds': bounds,
        })


# --- unit: delegation + content construction -------------------------------


class _CtxAsyncCase(unittest.TestCase):
    """Shared setUp: a headless Browser + a ctx with a spied open_dialog_async.

    Each method builds its content and forwards to ``open_dialog_async``; the
    spy lets us assert the exact content object + placement/anchor without the
    real loop. (Use ``self.ctx`` for delegation assertions; the headless and
    empty short-circuit cases construct their own browsers.)
    """

    def setUp(self):
        self.b = make_browser()  # headless
        self.spy = _Spy()
        self.b.open_dialog_async = self.spy
        self.ctx = CtxAsync(self.b)

    def tearDown(self):
        self.b.stop_workers()

    def _only_call(self):
        self.assertEqual(len(self.spy.calls), 1)
        return self.spy.calls[0]


class TestConfirmAsync(_CtxAsyncCase):

    def test_returns_none_immediately(self):
        self.assertIsNone(self.ctx.confirm_async('go?'))

    def test_builds_choice_content_centered(self):
        rec = _Recorder()
        self.ctx.confirm_async('proceed?', on_result=rec)
        call = self._only_call()
        content = call['content']
        self.assertIsInstance(content, ChoiceContent)
        self.assertEqual(content._message, 'proceed?')
        # Default buttons -> two parsed buttons with the &-hotkey labels.
        self.assertEqual([b.display for b in content._buttons], ['Yes', 'No'])
        self.assertIsNone(content.title)
        self.assertEqual(call['placement'], 'center')
        self.assertIsNone(call['anchor'])
        # on_result is forwarded verbatim (no wrapper for confirm).
        self.assertIs(call['on_result'], rec)

    def test_custom_buttons_and_title_forwarded(self):
        self.ctx.confirm_async('q', buttons=[('&Yes', True), ('&No', False)],
                               title='Danger')
        content = self._only_call()['content']
        self.assertEqual(content.title, 'Danger')
        self.assertEqual([b.value for b in content._buttons], [True, False])

    def test_callable_from_background_thread(self):
        # The spy stands in for the (thread-safe) open_dialog_async; calling
        # the ctx method off the main thread must not raise.
        rec = _Recorder()
        t = threading.Thread(
            target=lambda: self.ctx.confirm_async('bg?', on_result=rec))
        t.start()
        t.join()
        self.assertEqual(len(self.spy.calls), 1)


class TestAlertAsync(_CtxAsyncCase):

    def test_returns_none_immediately(self):
        self.assertIsNone(self.ctx.alert_async('done'))

    def test_builds_single_ok_choice_centered(self):
        self.ctx.alert_async('saved', title='Note')
        call = self._only_call()
        content = call['content']
        self.assertIsInstance(content, ChoiceContent)
        self.assertEqual(content._message, 'saved')
        self.assertEqual([b.display for b in content._buttons], ['OK'])
        self.assertEqual(content.title, 'Note')
        self.assertEqual(call['placement'], 'center')

    def test_on_result_receives_none_not_ok(self):
        # ChoiceContent('&OK',) resolves to 'OK' on activation, but an alert
        # conveys nothing back: the callback the engine fires must deliver
        # None regardless of what the dialog resolved to. The ctx method wraps
        # the user's callback so on_result(<value>) -> user_cb(None).
        rec = _Recorder()
        self.ctx.alert_async('hi', on_result=rec)
        wrapper = self._only_call()['on_result']
        self.assertIsNotNone(wrapper)
        # Simulate the engine resolving the &OK dialog to 'OK'.
        wrapper('OK')
        self.assertEqual(rec.calls, [None])

    def test_no_callback_alert_is_safe(self):
        # alert_async with no on_result: still delegates; the wrapper (if any)
        # must be safe to invoke with a resolved value.
        self.ctx.alert_async('hi')
        wrapper = self._only_call()['on_result']
        if wrapper is not None:
            wrapper('OK')  # must not raise


class TestPickAsync(_CtxAsyncCase):

    def test_returns_none_immediately(self):
        self.assertIsNone(self.ctx.pick_async('label', ['a', 'b']))

    def test_builds_filterable_list_anchored_to_slot(self):
        rec = _Recorder()
        self.ctx.pick_async('pick one', ['a', ('B', 2)], on_result=rec)
        call = self._only_call()
        content = call['content']
        self.assertIsInstance(content, ListContent)
        self.assertTrue(content._filter)            # picker => filter row
        self.assertEqual(content.title, 'pick one')  # label is the title
        self.assertEqual(content._options, [('a', 'a'), ('B', 2)])
        # #1101: pick_async enqueues the 'slot' sentinel so the main thread
        # anchors it to the modal-anchor slot (cursor row / previous selection)
        # — live geometry a worker can't read. With no active slot the servicing
        # step resolves it to centered.
        self.assertEqual(call['anchor'], 'slot')
        self.assertIs(call['on_result'], rec)

    def test_accepts_iterator_options(self):
        # options is materialized (list()) so a one-shot iterator works.
        self.ctx.pick_async('p', iter(['x', 'y']))
        content = self._only_call()['content']
        self.assertEqual(content._options, [('x', 'x'), ('y', 'y')])


class TestMenuAsync(_CtxAsyncCase):

    def test_returns_none_immediately(self):
        self.assertIsNone(self.ctx.menu_async(['a', 'b']))

    def test_builds_unfiltered_list_anchored_to_slot_without_anchor(self):
        rec = _Recorder()
        self.ctx.menu_async(['open', ('Del', 'delete')], on_result=rec)
        call = self._only_call()
        content = call['content']
        self.assertIsInstance(content, ListContent)
        self.assertFalse(content._filter)           # menu => no filter row
        self.assertIsNone(content.title)
        self.assertEqual(content._options,
                         [('open', 'open'), ('Del', 'delete')])
        # #1101: with no explicit anchor, menu_async enqueues the 'slot'
        # sentinel — the main thread anchors it to the modal-anchor slot
        # (cursor row / previous selection), live geometry a worker can't read.
        # With no active slot the servicing step resolves it to centered.
        self.assertEqual(call['anchor'], 'slot')

    def test_anchor_forwarded_with_anchor_placement(self):
        self.ctx.menu_async(['a'], anchor=(5, 9))
        call = self._only_call()
        self.assertEqual(call['placement'], 'anchor')
        self.assertEqual(call['anchor'], (5, 9))


class TestInputAsync(_CtxAsyncCase):

    def test_returns_none_immediately(self):
        self.assertIsNone(self.ctx.input_async('name?'))

    def test_builds_input_content_centered(self):
        rec = _Recorder()
        self.ctx.input_async('rename: ', default='old', on_result=rec)
        call = self._only_call()
        content = call['content']
        self.assertIsInstance(content, InputContent)
        self.assertEqual(content._prompt, 'rename: ')
        self.assertEqual(content.buffer, 'old')      # default pre-fills buffer
        self.assertEqual(call['placement'], 'center')
        self.assertIsNone(call['anchor'])
        self.assertIs(call['on_result'], rec)


# --- unit: headless callback (real open_dialog_async, not spied) -----------


class TestHeadlessFiresNone(unittest.TestCase):
    """Headless: open_dialog_async fires the callback with None inline, so the
    async ctx variants resolve to None immediately without a render loop."""

    def setUp(self):
        self.b = make_browser()       # headless, REAL open_dialog_async
        self.ctx = CtxAsync(self.b)

    def tearDown(self):
        self.b.stop_workers()

    def test_confirm_async_headless_fires_none(self):
        rec = _Recorder()
        self.ctx.confirm_async('go?', on_result=rec)
        self.assertEqual(rec.calls, [None])
        self.assertIsNone(self.b._pending_dialog)

    def test_alert_async_headless_fires_none(self):
        rec = _Recorder()
        self.ctx.alert_async('hi', on_result=rec)
        self.assertEqual(rec.calls, [None])

    def test_pick_async_headless_fires_none(self):
        rec = _Recorder()
        self.ctx.pick_async('p', ['a', 'b'], on_result=rec)
        self.assertEqual(rec.calls, [None])

    def test_menu_async_headless_fires_none(self):
        rec = _Recorder()
        self.ctx.menu_async(['a', 'b'], on_result=rec)
        self.assertEqual(rec.calls, [None])

    def test_input_async_headless_fires_none(self):
        rec = _Recorder()
        self.ctx.input_async('name?', on_result=rec)
        self.assertEqual(rec.calls, [None])


# --- unit: empty pick/menu short-circuit -----------------------------------


class TestEmptyShortCircuit(unittest.TestCase):
    """Empty options/items fire on_result(None) WITHOUT opening a dialog,
    once, on the main thread, caught — mirroring the blocking wrappers'
    empty short-circuit (which returns None without opening)."""

    def test_pick_async_empty_headless_fires_none_no_open(self):
        b = make_browser()           # headless
        spy = _Spy()
        b.open_dialog_async = spy
        try:
            rec = _Recorder()
            CtxAsync(b).pick_async('label', [], on_result=rec)
            # Fired None, and open_dialog_async was NOT called (no dialog).
            self.assertEqual(rec.calls, [None])
            self.assertEqual(spy.calls, [])
        finally:
            b.stop_workers()

    def test_menu_async_empty_headless_fires_none_no_open(self):
        b = make_browser()
        spy = _Spy()
        b.open_dialog_async = spy
        try:
            rec = _Recorder()
            CtxAsync(b).menu_async([], on_result=rec)
            self.assertEqual(rec.calls, [None])
            self.assertEqual(spy.calls, [])
        finally:
            b.stop_workers()

    def test_empty_throwing_callback_is_caught(self):
        # The empty short-circuit fires through the caught site, so a throwing
        # callback is routed to error, not propagated.
        b = make_browser()
        try:
            def boom(_v):
                raise ValueError('kaboom')
            CtxAsync(b).pick_async('l', [], on_result=boom)  # must not raise
            # _fire_dialog_cb catches and routes to error(), which posts the
            # log append; drain so it lands, then read the log.
            b.drain_main_queue()
            log = '\n'.join(b._log)
            self.assertIn('dialog callback', log)
            self.assertIn('kaboom', log)
        finally:
            b.stop_workers()

    def test_empty_pick_live_posts_then_fires_none_on_drain(self):
        # Non-headless empty pick: the None must be delivered on the MAIN
        # thread (a posted, caught closure), NOT inline on the calling thread.
        b = Browser(BrowserConfig(_headless=False))
        b.start_workers()
        try:
            rec = _Recorder()
            CtxAsync(b).pick_async('l', [], on_result=rec)
            # Not fired inline — it was posted to the main queue.
            self.assertEqual(rec.calls, [])
            self.assertFalse(b._main_queue.empty())
            # The drain (main thread) delivers None, and opens nothing.
            b.drain_main_queue()
            self.assertEqual(rec.calls, [None])
            self.assertIsNone(b._pending_dialog)
        finally:
            b.stop_workers()

    def test_empty_menu_live_posts_then_fires_none_on_drain(self):
        b = Browser(BrowserConfig(_headless=False))
        b.start_workers()
        try:
            rec = _Recorder()
            CtxAsync(b).menu_async([], on_result=rec)
            self.assertEqual(rec.calls, [])
            b.drain_main_queue()
            self.assertEqual(rec.calls, [None])
            self.assertIsNone(b._pending_dialog)
        finally:
            b.stop_workers()


# --- end-to-end: worker thread -> driven run loop -> main-thread callback --


class _StubModal:
    """``run_modal`` stand-in for the end-to-end servicing path (mirrors the
    #1042 ``TestRunServicing`` stub): records each call and returns scripted
    results, holding ``_modal_open`` True for the call's duration so the
    servicing while-loop's ``not self._modal_open`` guard behaves as in
    production. Captures the calling thread so the test can assert the
    callback (fired right after this returns) runs on the main thread."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def __call__(self, browser, content, *, placement='center', anchor=None,
                 delay_interaction=False, **kw):
        self.calls.append({'content': content, 'placement': placement,
                           'anchor': anchor,
                           'delay_interaction': delay_interaction})
        browser._modal_open = True
        try:
            return self._results.pop(0) if self._results else None
        finally:
            browser._modal_open = False


class _ServicingDriver:
    """Drives a real NON-headless ``Browser.run`` loop for the end-to-end test.

    Built on #1042's ``TestRunServicing`` harness (stub ``run_modal`` + a
    scripted ``read_key`` that returns ``'_notify'`` to drive top-of-loop
    drain + servicing passes). Two differences:

    * The Browser is NON-headless so a worker's ``open_dialog_async`` actually
      POSTS (the headless path fires ``None`` inline and never exercises the
      post -> drain -> servicing chain we want to test). A non-headless
      ``run()`` calls a few terminal/render names by bare identifier
      (``term_init`` at entry, ``render_full`` / ``render_partial`` per dirty
      tick, ``term_restore`` at teardown) — the isolated test load has no real
      tty, so those are stubbed to no-ops here. ``run_modal`` (stubbed) stands
      in for the dialog, so no real screen is touched.
    * We do NOT arm the quit on the first read — the worker's request must be
      posted, drained and serviced first, so ``read_key`` spins (returning
      ``'_notify'``) until ``done_predicate`` (the callback fired), then quits.
    """

    _STUBS = ('term_init', 'term_restore', 'render_full', 'render_partial',
              '_layout_for')

    def __init__(self, stub_modal, *, done_predicate):
        self._stub = stub_modal
        self._done = done_predicate

    def __enter__(self):
        names = ('run_modal', 'read_key', 'input_ready',
                 'term_stdout_was_tty') + self._STUBS
        self._orig = {n: getattr(_state, n, None) for n in names}
        _state.run_modal = self._stub
        _state.input_ready = lambda: False
        _state.term_stdout_was_tty = _term.term_stdout_was_tty
        # No real terminal under the isolated load: neutralize the
        # non-headless terminal/render calls ``run()`` makes.
        for name in self._STUBS:
            setattr(_state, name, lambda *a, **k: None)
        return self

    def run(self, browser):
        def rk(*a, **k):
            # Spin servicing the queue until the worker's dialog resolved
            # (done_predicate), THEN quit. Each '_notify' return drives one
            # top-of-loop drain + servicing pass.
            if self._done():
                browser._quit_requested = True
            return '_notify'

        _state.read_key = rk
        return browser.run()

    def __exit__(self, *exc):
        for name, val in self._orig.items():
            if val is None:
                _state.__dict__.pop(name, None)
            else:
                setattr(_state, name, val)


class TestConfirmAsyncEndToEnd(unittest.TestCase):
    """A real ctx.confirm_async from a BACKGROUND thread against a driven
    Browser.run loop: assert the callback fires on the MAIN thread with the
    scripted result, exercising post -> drain -> servicing -> run_modal ->
    callback for real (not the headless inline short-circuit)."""

    def test_worker_confirm_async_callback_on_main_thread(self):
        # Non-headless so open_dialog_async POSTS (rather than firing None
        # inline); the driver stubs the terminal/render bare names so the loop
        # runs without a real tty, and the stub run_modal stands in for the
        # dialog.
        b = Browser(BrowserConfig(_headless=False))
        b.start_workers()

        main_ident = threading.get_ident()
        result_box = {}
        fired = threading.Event()

        def on_result(value):
            result_box['value'] = value
            result_box['thread'] = threading.get_ident()
            fired.set()

        # The worker calls the PUBLIC ctx async surface from its own thread.
        ctx = CtxAsync(b)

        def worker():
            ctx.confirm_async('proceed?', on_result=on_result)

        stub = _StubModal(results=['confirmed'])

        # Kick the worker before run() so its post is queued (or arrives while
        # the loop spins). The driver quits once the callback has fired.
        with _ServicingDriver(stub, done_predicate=fired.is_set) as drv:
            t = ctx.run_in_worker(worker)
            try:
                drv.run(b)
            finally:
                t.join(timeout=2)
                b.stop_workers()

        # The dialog was opened once with the confirm content, centered, and
        # delay_interaction=True (the async-open default).
        self.assertEqual(len(stub.calls), 1)
        self.assertIsInstance(stub.calls[0]['content'], ChoiceContent)
        self.assertEqual(stub.calls[0]['placement'], 'center')
        self.assertTrue(stub.calls[0]['delay_interaction'])
        # The callback fired with the scripted result, ON THE MAIN THREAD.
        self.assertTrue(fired.is_set())
        self.assertEqual(result_box['value'], 'confirmed')
        self.assertEqual(result_box['thread'], main_ident)


if __name__ == '__main__':
    unittest.main()
