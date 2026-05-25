"""Tests for generator support in ``get_preview`` (#273).

Ticket #273 builds on #272's generator pattern: when ``get_preview``
returns a generator, the preview worker eagerly pulls each yield,
calls ``append_preview``, and tracks running buffer size. When the
buffer hits the configured cap, the worker pauses (without closing the
generator) and waits for either a cursor-move (abandon, fires recipe
``finally``) or a future #274 demand signal (resume).

Cap defaults: 100 KB chars or 1000 lines (whichever first). Configured
via ``Browser(BrowserConfig(preview_buffer_cap_chars=…, preview_buffer_cap_lines=…))``.

Pause state lives on ``Browser._preview_paused`` (None when not paused;
otherwise a dict with keys ``id``, ``gen``, ``chars``, ``lines``). It is
guarded by ``Browser._preview_lock`` since both the worker and the
main thread (cursor-move via ``request_preview``) touch it.

The cursor-move signal travels through the existing ``_preview_req``
slot the worker already polls; ``_preview_resume_event`` is a wake
mechanism for the in-pause wait.
"""

import threading
import time
import unittest

from test.async_._helpers import Item, make_browser, get_preview_text


def _wait_until(predicate, timeout=2.0, interval=0.005):
    """Poll ``predicate`` until True or timeout. Returns final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = predicate()
        if v:
            return v
        time.sleep(interval)
    return predicate()


def _drain_until(b, predicate, timeout=2.0, interval=0.005):
    """Drain main queue + poll until predicate True. Returns final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        b.drain_main_queue()
        v = predicate()
        if v:
            return v
        time.sleep(interval)
    b.drain_main_queue()
    return predicate()


class TestStreamingYields(unittest.TestCase):
    """Generator yields chunks; each yield lands incrementally in cache."""

    def test_basic_streaming_yields_assemble(self):
        def get_preview(_id):
            yield 'line 1\n'
            yield 'line 2\n'
            yield 'line 3\n'

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            b.run_until_idle()
            self.assertEqual(get_preview_text(b, 'a'), 'line 1\nline 2\nline 3\n')
        finally:
            b.stop_workers()

    def test_intermediate_visibility_between_yields(self):
        gate1 = threading.Event()
        gate2 = threading.Event()

        def get_preview(_id):
            yield 'A'
            gate1.wait(timeout=2.0)
            yield 'B'
            gate2.wait(timeout=2.0)
            yield 'C'

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='x')],
            get_preview=get_preview,
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('x')

            # Worker is now blocked on gate1 — wait until 'A' is in cache.
            self.assertTrue(
                _drain_until(b, lambda: get_preview_text(b, 'x') == 'A'),
                f"expected 'A', got {get_preview_text(b, 'x')!r}",
            )

            # Release gate1 — worker yields 'B'.
            gate1.set()
            self.assertTrue(
                _drain_until(b, lambda: get_preview_text(b, 'x') == 'AB'),
                f"expected 'AB', got {get_preview_text(b, 'x')!r}",
            )

            # Release gate2 — worker yields 'C' and exhausts.
            gate2.set()
            b.run_until_idle()
            self.assertEqual(get_preview_text(b, 'x'), 'ABC')
        finally:
            gate1.set()
            gate2.set()
            b.stop_workers()

    def test_string_chunk_only_handled(self):
        # Defensive: non-str yields coerced via str().
        def get_preview(_id):
            yield 'hello '
            yield 42  # not a string — coerced
            yield None  # treated as empty
            yield ' world'

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            b.run_until_idle()
            self.assertEqual(get_preview_text(b, 'a'), 'hello 42 world')
        finally:
            b.stop_workers()


class TestCapThenPause(unittest.TestCase):
    """Generator yields past the cap; worker pauses, no more yields pulled."""

    def test_chars_cap_pauses_pulling(self):
        yield_count = {'n': 0}

        def get_preview(_id):
            while True:
                yield_count['n'] += 1
                yield 'x' * 50  # 50 chars per yield

        # Cap at 100 chars → after 2 yields, pause.
        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
            preview_buffer_cap_chars=100,
            preview_buffer_cap_lines=1_000_000,
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            # Wait until paused.
            self.assertTrue(
                _drain_until(b, lambda: b._preview_paused is not None),
                'worker did not enter paused state',
            )
            paused = b._preview_paused
            self.assertEqual(paused['id'], 'a')
            # Sample yield_count, then wait a beat — count should not grow.
            initial = yield_count['n']
            time.sleep(0.05)
            b.drain_main_queue()
            self.assertEqual(
                yield_count['n'], initial,
                f'paused worker kept pulling: {yield_count["n"]} > {initial}',
            )
            # Buffer reflects exactly what was pulled (>= cap, but no more
            # than initial * 50).
            buf = (get_preview_text(b, 'a') or '')
            self.assertGreaterEqual(len(buf), 100)
            self.assertEqual(len(buf), initial * 50)
        finally:
            b.stop_workers()

    def test_lines_cap_pauses_pulling(self):
        yield_count = {'n': 0}

        def get_preview(_id):
            while True:
                yield_count['n'] += 1
                yield 'l\n'  # 2 chars / 1 line per yield

        # Cap at 3 lines → after 3 yields, pause.
        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
            preview_buffer_cap_chars=1_000_000,
            preview_buffer_cap_lines=3,
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            self.assertTrue(
                _drain_until(b, lambda: b._preview_paused is not None),
                'worker did not enter paused state',
            )
            initial = yield_count['n']
            self.assertGreaterEqual(initial, 3)
            time.sleep(0.05)
            b.drain_main_queue()
            self.assertEqual(yield_count['n'], initial)
            self.assertEqual(b._preview_paused['lines'], initial)
        finally:
            b.stop_workers()


class TestAbandonMidStream(unittest.TestCase):
    """Cursor-move during pause closes the generator (fires ``finally``)."""

    def test_finally_fires_when_paused_gen_abandoned(self):
        finally_fired = threading.Event()

        def get_preview(_id):
            try:
                while True:
                    yield 'x' * 50
            finally:
                finally_fired.set()

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a'), Item(id='b')],
            get_preview=get_preview,
            root_id='/',
            preview_buffer_cap_chars=100,
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            self.assertTrue(
                _drain_until(b, lambda: b._preview_paused is not None),
                'worker did not enter paused state for a',
            )
            # Move cursor to a different id — should close the paused gen.
            b.request_preview('b')
            self.assertTrue(
                finally_fired.wait(timeout=2.0),
                "recipe's finally block did not fire on cursor-move abandon",
            )
            # Paused state cleared.
            self.assertTrue(
                _drain_until(
                    b,
                    lambda: b._preview_paused is None
                    or b._preview_paused.get('id') != 'a',
                ),
                f"paused state still references 'a': {b._preview_paused!r}",
            )
        finally:
            b.stop_workers()

    def test_finally_fires_when_paused_gen_abandoned_to_none(self):
        # Cursor-off-any-item: ``request_preview`` is bypassed; the
        # ``_update_preview_for_cursor`` path sets ``_preview_req=None``
        # and wakes the resume event directly. We simulate that here.
        finally_fired = threading.Event()

        def get_preview(_id):
            try:
                while True:
                    yield 'x' * 50
            finally:
                finally_fired.set()

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
            preview_buffer_cap_chars=100,
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            self.assertTrue(
                _drain_until(b, lambda: b._preview_paused is not None),
            )
            # Simulate cursor-off-item: clear req + wake.
            b._preview_req = None
            b._preview_resume_event.set()
            self.assertTrue(
                finally_fired.wait(timeout=2.0),
                "recipe's finally block did not fire on cursor-off abandon",
            )
        finally:
            b.stop_workers()

    def test_finally_fires_mid_stream_before_pause(self):
        # Generator hasn't reached the cap yet — it's blocked on a gate.
        # Cursor-move abandons before pause.
        gate = threading.Event()
        finally_fired = threading.Event()

        def get_preview(_id):
            try:
                yield 'first chunk'
                gate.wait(timeout=2.0)
                yield 'second chunk'  # should never execute
            finally:
                finally_fired.set()

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a'), Item(id='b')],
            get_preview=get_preview,
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            # Wait until first chunk is delivered.
            self.assertTrue(
                _drain_until(
                    b, lambda: get_preview_text(b, 'a') == 'first chunk'
                ),
            )
            # Move cursor BEFORE releasing the gate. Worker is blocked
            # on next() — the gate.wait() inside the generator. Move
            # cursor + release gate so next() returns and worker
            # observes the cursor-move.
            b.request_preview('b')
            gate.set()
            self.assertTrue(
                finally_fired.wait(timeout=2.0),
                "finally did not fire on cursor-move abandon mid-stream",
            )
        finally:
            gate.set()
            b.stop_workers()


class TestStringReturnRegression(unittest.TestCase):
    """Sanity: a non-generator str return still uses the legacy path."""

    def test_plain_string_preview_works(self):
        def get_preview(_id):
            return f'preview for {_id}'

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            b.run_until_idle()
            self.assertEqual(get_preview_text(b, 'a'), 'preview for a')
            # Generator-only state stays untouched.
            self.assertIsNone(b._preview_paused)
        finally:
            b.stop_workers()

    def test_none_return_becomes_empty_string(self):
        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a')],
            get_preview=lambda _id: None,
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            b.run_until_idle()
            self.assertEqual(get_preview_text(b, 'a'), '')
            self.assertIsNone(b._preview_paused)
        finally:
            b.stop_workers()

    def test_string_return_then_generator_return(self):
        # Switch behaviour by id — make sure both paths cohabit.
        def get_preview(item_id):
            if item_id == 'str':
                return 'plain'

            def _gen():
                yield 'streamed-'
                yield item_id

            return _gen()

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='str'), Item(id='gen')],
            get_preview=get_preview,
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('str')
            b.run_until_idle()
            self.assertEqual(get_preview_text(b, 'str'), 'plain')

            b.request_preview('gen')
            b.run_until_idle()
            self.assertEqual(get_preview_text(b, 'gen'), 'streamed-gen')
        finally:
            b.stop_workers()


class TestGeneratorRaisesMidStream(unittest.TestCase):
    """Mid-stream exception: error tag appended; partial buffer retained."""

    def test_partial_preview_retained_after_raise(self):
        def get_preview(_id):
            yield 'first chunk\n'
            yield 'second chunk\n'
            raise RuntimeError('boom')

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            b.run_until_idle()
            buf = (get_preview_text(b, 'a') or '')
            self.assertIn('first chunk', buf)
            self.assertIn('second chunk', buf)
            self.assertIn('RuntimeError', buf)
            self.assertIn('boom', buf)
        finally:
            b.stop_workers()

    def test_initial_call_raise_surfaces(self):
        # Exception raised when ``get_preview(id)`` is called (before
        # any generator iteration). The legacy str-error path applies.
        def get_preview(_id):
            raise ValueError('initial boom')

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            b.run_until_idle()
            buf = (get_preview_text(b, 'a') or '')
            self.assertIn('ValueError', buf)
            self.assertIn('initial boom', buf)
        finally:
            b.stop_workers()


class TestNewPreviewSupersedesPaused(unittest.TestCase):
    """A new ``request_preview`` for a different id closes the paused gen."""

    def test_supersede_paused_with_new_request(self):
        finally_fired_a = threading.Event()
        b_started = threading.Event()

        def get_preview(item_id):
            if item_id == 'a':
                try:
                    while True:
                        yield 'x' * 50
                finally:
                    finally_fired_a.set()
            elif item_id == 'b':
                b_started.set()
                yield 'b-content'

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a'), Item(id='b')],
            get_preview=get_preview,
            root_id='/',
            preview_buffer_cap_chars=100,
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            self.assertTrue(
                _drain_until(b, lambda: b._preview_paused is not None),
                'worker did not pause on a',
            )

            # Supersede with a new request for b.
            b.request_preview('b')
            self.assertTrue(
                finally_fired_a.wait(timeout=2.0),
                "a's finally did not fire on supersede",
            )
            self.assertTrue(
                b_started.wait(timeout=2.0),
                "b's preview did not start",
            )
            b.run_until_idle()
            self.assertEqual(get_preview_text(b, 'b'), 'b-content')
            # Paused state empty (b's gen exhausted; a's was abandoned).
            self.assertIsNone(b._preview_paused)
        finally:
            b.stop_workers()


class TestEmptyAndCleanExhaustion(unittest.TestCase):
    """Empty generator / clean exhaustion behaviours."""

    def test_empty_generator_yields_empty_preview(self):
        def get_preview(_id):
            return
            yield  # noqa — makes the function a generator

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            b.run_until_idle()
            # No yields — cache stays absent (or empty).
            self.assertIn((get_preview_text(b, 'a') or ''), ('', None))
            self.assertIsNone(b._preview_paused)
        finally:
            b.stop_workers()

    def test_clean_exhaustion_clears_request_slot(self):
        def get_preview(_id):
            yield 'done'

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            b.run_until_idle()
            self.assertIsNone(b._preview_req)
            self.assertEqual(get_preview_text(b, 'a'), 'done')
        finally:
            b.stop_workers()


class TestAbandonClearsCache(unittest.TestCase):
    """#456: cursor-move / _stop on a streaming preview clears Item.preview.

    The partial buffer in ``Item.preview`` is no longer a useful cache
    once the generator is abandoned — a re-visit should refetch fresh
    rather than paint the truncated text as if it were the final
    preview. Cache is preserved on clean ``StopIteration`` and on
    mid-stream exception (partial + ``[error]`` tag is informative).
    """

    def test_cursor_move_clears_paused_partial(self):
        # Generator pauses at the cap with two chunks buffered.
        def get_preview(_id):
            while True:
                yield 'x' * 50

        b = make_browser(
            get_children=lambda _, *, reload=False: [
                Item(id='a'), Item(id='b'),
            ],
            get_preview=get_preview,
            root_id='/',
            preview_buffer_cap_chars=100,
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            # Wait until paused mid-stream — partial 'a' is cached.
            self.assertTrue(
                _drain_until(b, lambda: b._preview_paused is not None),
                'worker did not pause on a',
            )
            self.assertGreaterEqual(len(get_preview_text(b, 'a') or ''), 100)
            # Cursor-move: abandon a, switch to b.
            b.request_preview('b')
            # Wait for the post-queue cache clear + b to take over.
            self.assertTrue(
                _drain_until(b, lambda: get_preview_text(b, 'a') is None),
                f"abandoned id 'a' cache not cleared: "
                f"{get_preview_text(b, 'a')!r}",
            )
        finally:
            b.stop_workers()

    def test_cursor_move_clears_mid_stream_partial(self):
        # Generator hasn't paused yet — blocked on a gate after one chunk.
        gate = threading.Event()

        def get_preview(_id):
            yield 'first chunk'
            gate.wait(timeout=2.0)
            yield 'second chunk'  # should never execute

        b = make_browser(
            get_children=lambda _, *, reload=False: [
                Item(id='a'), Item(id='b'),
            ],
            get_preview=get_preview,
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            self.assertTrue(
                _drain_until(
                    b, lambda: get_preview_text(b, 'a') == 'first chunk'
                ),
            )
            # Cursor-move before the generator's second yield. Release
            # the gate so next(gen) returns and the worker observes
            # the cursor-move on the next loop iteration.
            b.request_preview('b')
            gate.set()
            self.assertTrue(
                _drain_until(b, lambda: get_preview_text(b, 'a') is None),
                f"mid-stream abandoned id 'a' cache not cleared: "
                f"{get_preview_text(b, 'a')!r}",
            )
        finally:
            gate.set()
            b.stop_workers()

    def test_revisit_after_abandon_refetches_fresh(self):
        # After cursor-move clears the cache, a re-request for the same
        # id should fire the worker again — not silently keep the stale
        # partial. We track call_count to confirm the second call.
        call_count = {'n': 0}

        def get_preview(item_id):
            if item_id == 'a':
                call_count['n'] += 1
                yield f"call-{call_count['n']}-chunk1 "
                yield 'x' * 50
                yield 'x' * 50  # pushes past cap
                yield 'x' * 50
            else:
                yield 'b-content'

        b = make_browser(
            get_children=lambda _, *, reload=False: [
                Item(id='a'), Item(id='b'),
            ],
            get_preview=get_preview,
            root_id='/',
            preview_buffer_cap_chars=100,
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            self.assertTrue(
                _drain_until(b, lambda: b._preview_paused is not None),
                'worker did not pause on first visit to a',
            )
            self.assertIn('call-1', get_preview_text(b, 'a') or '')
            # Move off — should abandon + clear.
            b.request_preview('b')
            self.assertTrue(
                _drain_until(b, lambda: get_preview_text(b, 'a') is None),
            )
            b.run_until_idle()
            # Revisit a: worker should re-run get_preview('a') from
            # scratch, producing 'call-2-' prefixed output.
            b.request_preview('a')
            self.assertTrue(
                _drain_until(
                    b,
                    lambda: 'call-2' in (get_preview_text(b, 'a') or ''),
                ),
                f"re-request did not refetch: "
                f"call_count={call_count['n']}, "
                f"text={get_preview_text(b, 'a')!r}",
            )
            self.assertEqual(call_count['n'], 2)
        finally:
            b.stop_workers()

    def test_clean_stopiteration_preserves_cache(self):
        # Regression guard: the abandon clear must NOT trip when the
        # generator finishes cleanly.
        def get_preview(_id):
            yield 'complete '
            yield 'preview'

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            b.run_until_idle()
            self.assertEqual(get_preview_text(b, 'a'), 'complete preview')
            # Wait a beat + drain once more — clear must not arrive.
            time.sleep(0.05)
            b.drain_main_queue()
            self.assertEqual(get_preview_text(b, 'a'), 'complete preview')
        finally:
            b.stop_workers()

    def test_mid_stream_exception_preserves_partial(self):
        # Regression guard: mid-stream raise leaves the partial +
        # ``[error]`` tag in the cache, intentionally informative.
        def get_preview(_id):
            yield 'partial '
            raise RuntimeError('boom')

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            b.run_until_idle()
            time.sleep(0.05)
            b.drain_main_queue()
            buf = get_preview_text(b, 'a') or ''
            self.assertIn('partial ', buf)
            self.assertIn('RuntimeError', buf)
            self.assertIn('boom', buf)
            # Cache is NOT None (partial+error tag is preserved).
            self.assertIsNotNone(get_preview_text(b, 'a'))
        finally:
            b.stop_workers()

    def test_side_effect_ops_on_other_ids_untouched(self):
        # The umbrella case: a generator for 'a' writes set_preview ops
        # for sibling ids 'leaf1'/'leaf2' as side effects. On abandon,
        # the framework only clears 'a' — the sibling caches survive.
        # (The framework's clear is scoped to the abandoned id; this
        # test confirms it.)
        from test.async_._helpers import _state as _state_mod
        set_preview_op = _state_mod.set_preview_op

        def get_preview(item_id):
            if item_id == 'a':
                # Side-effect: cache previews for leaves before
                # yielding our own content. update_data is thread-safe.
                b_ref[0].update_data([
                    set_preview_op('leaf1', 'leaf1-body'),
                    set_preview_op('leaf2', 'leaf2-body'),
                ])
                while True:
                    yield 'x' * 50
            else:
                yield f'{item_id}-content'

        b_ref = [None]
        b = make_browser(
            get_children=lambda _, *, reload=False: [
                Item(id='a'), Item(id='b'),
                Item(id='leaf1'), Item(id='leaf2'),
            ],
            get_preview=get_preview,
            root_id='/',
            preview_buffer_cap_chars=100,
        )
        b_ref[0] = b
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            # Wait until paused — leaves should now have their cache too.
            self.assertTrue(
                _drain_until(
                    b,
                    lambda: (
                        b._preview_paused is not None
                        and get_preview_text(b, 'leaf1') == 'leaf1-body'
                        and get_preview_text(b, 'leaf2') == 'leaf2-body'
                    ),
                ),
                f"leaves not populated: "
                f"leaf1={get_preview_text(b, 'leaf1')!r}, "
                f"leaf2={get_preview_text(b, 'leaf2')!r}",
            )
            # Abandon a — leaves must survive; only 'a' is cleared.
            b.request_preview('b')
            self.assertTrue(
                _drain_until(b, lambda: get_preview_text(b, 'a') is None),
            )
            self.assertEqual(get_preview_text(b, 'leaf1'), 'leaf1-body')
            self.assertEqual(get_preview_text(b, 'leaf2'), 'leaf2-body')
        finally:
            b.stop_workers()


class TestDrainWhenPinned(unittest.TestCase):
    """#457: tail-pin (Shift-End) drains without pausing at the cap.

    When ``browser._preview_at_tail`` is True, the user has explicitly
    asked for the full preview via Shift-End. The generator-pause logic
    would otherwise park the worker at the cap and only resume one
    window per render tick — adding render-paced latency to the
    explicit "give me everything" command. Spec §2: while pinned, the
    worker advances the cap thresholds inline and keeps pulling.
    """

    def test_pinned_drains_without_pausing(self):
        # Generator yields ~3 cap windows worth of content. With the
        # tail-pin engaged, the worker must reach StopIteration without
        # ever recording a paused state.
        yield_count = {'n': 0}
        total_yields = 30  # 30 * 50 = 1500 chars, cap=500 → ~3 windows

        def get_preview(_id):
            for _ in range(total_yields):
                yield_count['n'] += 1
                yield 'x' * 50

        # Observer thread samples ``_preview_paused`` aggressively while
        # the generator drains so a transient pause would be caught.
        paused_observations = []
        stop_observer = threading.Event()

        def observe(b):
            while not stop_observer.is_set():
                p = b._preview_paused
                if p is not None:
                    paused_observations.append(dict(p))
                time.sleep(0.0005)

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
            preview_buffer_cap_chars=500,
            preview_buffer_cap_lines=1_000_000,
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            # Engage the tail-pin BEFORE requesting the preview so the
            # worker sees it on the very first cap check.
            b._preview_at_tail = True

            t = threading.Thread(target=observe, args=(b,), daemon=True)
            t.start()
            try:
                b.request_preview('a')
                # Generator should run to exhaustion without pausing.
                self.assertTrue(
                    _drain_until(
                        b, lambda: yield_count['n'] == total_yields
                    ),
                    f"generator did not exhaust: "
                    f"yield_count={yield_count['n']}",
                )
                b.run_until_idle()
            finally:
                stop_observer.set()
                t.join(timeout=1.0)

            # Buffer holds all 30 yields concatenated.
            self.assertEqual(
                get_preview_text(b, 'a'), 'x' * (total_yields * 50)
            )
            # Request slot cleared on clean StopIteration.
            self.assertIsNone(b._preview_req)
            # Crucially: no paused state observed mid-flight, even
            # though the buffered size sailed past the cap multiple
            # times.
            self.assertEqual(
                paused_observations, [],
                f"unexpected paused state(s) while tail-pinned: "
                f"{paused_observations!r}",
            )
            self.assertIsNone(b._preview_paused)
        finally:
            b.stop_workers()

    def test_unpinned_still_pauses_at_cap(self):
        # Regression: when ``_preview_at_tail`` is False, the existing
        # cap-pause behaviour must be unchanged.
        yield_count = {'n': 0}

        def get_preview(_id):
            while True:
                yield_count['n'] += 1
                yield 'x' * 50

        b = make_browser(
            get_children=lambda _, *, reload=False: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
            preview_buffer_cap_chars=100,
            preview_buffer_cap_lines=1_000_000,
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            # Tail-pin is False by default — be explicit for clarity.
            self.assertFalse(b._preview_at_tail)

            b.request_preview('a')
            self.assertTrue(
                _drain_until(b, lambda: b._preview_paused is not None),
                'worker did not enter paused state with tail-pin off',
            )
            paused = b._preview_paused
            self.assertEqual(paused['id'], 'a')
            # Worker stopped pulling at the cap.
            initial = yield_count['n']
            time.sleep(0.05)
            b.drain_main_queue()
            self.assertEqual(yield_count['n'], initial)
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
