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


if __name__ == '__main__':
    unittest.main()
