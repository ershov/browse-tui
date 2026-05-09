"""Tests for renderer-driven demand resume of paused preview generators (#274).

Builds on #273 (eager-pull-then-pause). When the renderer detects the
preview viewport is within ``_PREVIEW_DEMAND_THRESHOLD`` rows of the
buffered tail, it calls ``Browser.signal_preview_demand(item_id)`` to
wake the paused worker. The worker resumes pulling until the next cap,
then re-pauses.

These tests drive ``signal_preview_demand`` directly (the renderer's
detection is exercised in the UI test). They cover:

  * Resume on demand → worker pulls more, pauses again at next cap.
  * Cursor-move during a resume cycle still abandons cleanly (recipe
    ``finally`` fires).
  * Multiple resume cycles march the buffer forward by ~one cap window
    each.
  * Demand signal for a non-paused id is a no-op.
  * Generator exhaustion after resume clears the paused state (preserves
    #273 behaviour).
"""

import threading
import time
import unittest

from test.async_._helpers import Item, make_browser


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


class TestResumeOnDemand(unittest.TestCase):
    """``signal_preview_demand`` wakes a paused worker; pulling resumes."""

    def test_demand_signal_resumes_pulling_until_next_cap(self):
        yield_count = {'n': 0}

        def get_preview(_id):
            while True:
                yield_count['n'] += 1
                yield 'x' * 50  # 50 chars per yield

        # Cap at 100 chars → 2 yields per cap window.
        b = make_browser(
            get_children=lambda _: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
            preview_buffer_cap_chars=100,
            preview_buffer_cap_lines=1_000_000,
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
            self.assertGreaterEqual(initial, 2)

            # Demand signal — worker resumes, pulls one more cap window,
            # pauses again.
            b.signal_preview_demand('a')

            # Wait for the next pause to settle (chars grew past the
            # next cap window). _preview_paused stays non-None across
            # re-pause; check via the recorded chars count growing.
            prev_chars = b._preview_paused['chars']
            self.assertTrue(
                _drain_until(
                    b,
                    lambda: (
                        b._preview_paused is not None
                        and b._preview_paused['chars'] > prev_chars
                    ),
                ),
                f'worker did not advance past prev_chars={prev_chars}; '
                f'paused={b._preview_paused!r}',
            )

            new_chars = b._preview_paused['chars']
            self.assertGreaterEqual(new_chars - prev_chars, 100)
            # And the worker is paused again (not still pulling).
            sample = yield_count['n']
            time.sleep(0.05)
            b.drain_main_queue()
            self.assertEqual(yield_count['n'], sample)
        finally:
            b.stop_workers()

    def test_no_resume_without_signal(self):
        # Sanity: without a demand signal, paused worker stays put.
        yield_count = {'n': 0}

        def get_preview(_id):
            while True:
                yield_count['n'] += 1
                yield 'x' * 50

        b = make_browser(
            get_children=lambda _: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
            preview_buffer_cap_chars=100,
            preview_buffer_cap_lines=1_000_000,
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            self.assertTrue(
                _drain_until(b, lambda: b._preview_paused is not None),
            )
            initial = yield_count['n']
            # Wait a beat without signalling — count must not grow.
            time.sleep(0.1)
            b.drain_main_queue()
            self.assertEqual(yield_count['n'], initial)
        finally:
            b.stop_workers()

    def test_demand_signal_for_unpaused_id_is_noop(self):
        # No preview paused at all → signal is harmless.
        b = make_browser(
            get_children=lambda _: [Item(id='a')],
            get_preview=lambda _id: 'plain',
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            # No active preview yet.
            b.signal_preview_demand('a')
            # State unchanged.
            self.assertIsNone(b._preview_paused)
            self.assertFalse(b._preview_resume_pull)
        finally:
            b.stop_workers()

    def test_demand_signal_for_other_id_while_paused_is_noop(self):
        def get_preview(_id):
            while True:
                yield 'x' * 50

        b = make_browser(
            get_children=lambda _: [Item(id='a'), Item(id='b')],
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
            # Signal for the wrong id — no-op.
            b.signal_preview_demand('b')
            self.assertFalse(b._preview_resume_pull)
            # And paused state preserved.
            self.assertEqual(b._preview_paused.get('id'), 'a')
        finally:
            b.stop_workers()


class TestMultipleResumeCycles(unittest.TestCase):
    """Resume → pause → resume → pause across many cap windows."""

    def test_three_resume_cycles_advance_buffer(self):
        def get_preview(_id):
            while True:
                yield 'x' * 50

        b = make_browser(
            get_children=lambda _: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
            preview_buffer_cap_chars=100,
            preview_buffer_cap_lines=1_000_000,
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            self.assertTrue(
                _drain_until(b, lambda: b._preview_paused is not None),
            )

            milestones = [b._preview_paused['chars']]
            for _ in range(3):
                prev = b._preview_paused['chars']
                b.signal_preview_demand('a')
                self.assertTrue(
                    _drain_until(
                        b,
                        lambda prev=prev: (
                            b._preview_paused is not None
                            and b._preview_paused['chars'] > prev
                        ),
                    ),
                    f'cycle did not advance past {prev}',
                )
                milestones.append(b._preview_paused['chars'])

            # Each cycle adds at least one cap window (100 chars).
            for prev, cur in zip(milestones, milestones[1:]):
                self.assertGreaterEqual(cur - prev, 100)

            # Buffer reflects the milestones (>= last milestone chars).
            buf_len = len(b._state._preview.get('a', ''))
            self.assertGreaterEqual(buf_len, milestones[-1])
        finally:
            b.stop_workers()


class TestCursorMoveDuringResume(unittest.TestCase):
    """Cursor-move while resume is in flight still abandons cleanly."""

    def test_cursor_move_after_demand_signal_fires_finally(self):
        # Tighter race: gate the post-resume yields so we can move the
        # cursor while the worker is mid-pull.
        finally_fired = threading.Event()
        gate = threading.Event()

        def get_preview(item_id):
            if item_id == 'a':
                try:
                    # Fill the first cap window unconditionally so we
                    # can pause deterministically.
                    yield 'x' * 50
                    yield 'x' * 50
                    # Past the cap → worker pauses here.
                    # When the demand signal lands, the worker calls
                    # next() again — block on the gate so the cursor
                    # has time to move.
                    gate.wait(timeout=2.0)
                    yield 'x' * 50  # may or may not be reached
                finally:
                    finally_fired.set()
            elif item_id == 'b':
                yield 'b-content'

        b = make_browser(
            get_children=lambda _: [Item(id='a'), Item(id='b')],
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

            # Trigger resume — the worker wakes, breaks out of the
            # pause loop, and calls next(gen) which blocks on ``gate``.
            b.signal_preview_demand('a')

            # Wait until the worker is actually mid-pull (paused state
            # cleared because resume took it). Then move cursor.
            self.assertTrue(
                _drain_until(
                    b,
                    lambda: b._preview_paused is None
                    or b._preview_paused.get('id') != 'a',
                ),
                'worker did not leave paused state on resume',
            )

            # Now cursor-move while gen is blocked on gate.
            b.request_preview('b')
            # Release the gate so next() returns and the worker can
            # observe the cursor-move.
            gate.set()
            self.assertTrue(
                finally_fired.wait(timeout=2.0),
                "a's finally did not fire on cursor-move during resume",
            )
            # Worker should now be serving b.
            self.assertTrue(
                _drain_until(
                    b, lambda: b._state._preview.get('b') == 'b-content'
                ),
            )
        finally:
            gate.set()
            b.stop_workers()


class TestExhaustionAfterResume(unittest.TestCase):
    """Generator exhaustion after one or more resumes clears paused state."""

    def test_finite_generator_exhausts_after_resume(self):
        # 5 yields of 50 chars = 250 chars total. Cap=100 means pause
        # after yield 2; resume pulls 3 more then exhausts.
        def get_preview(_id):
            for _ in range(5):
                yield 'x' * 50

        b = make_browser(
            get_children=lambda _: [Item(id='a')],
            get_preview=get_preview,
            root_id='/',
            preview_buffer_cap_chars=100,
            preview_buffer_cap_lines=1_000_000,
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            b.request_preview('a')
            self.assertTrue(
                _drain_until(b, lambda: b._preview_paused is not None),
            )

            # Walk the demand signal until exhaustion.
            for _ in range(10):
                b.signal_preview_demand('a')
                # Either paused-but-advanced, or exhausted (paused=None,
                # req=None).
                _drain_until(
                    b,
                    lambda: (
                        b._preview_paused is None and b._preview_req is None
                    ) or (
                        b._preview_paused is not None
                        and b._preview_paused['chars'] >= 250
                    ),
                    timeout=1.0,
                )
                if b._preview_paused is None and b._preview_req is None:
                    break

            b.run_until_idle()
            self.assertIsNone(b._preview_paused,
                              f'still paused: {b._preview_paused!r}')
            self.assertIsNone(b._preview_req)
            # Final buffer = 5 * 50 = 250 chars.
            self.assertEqual(len(b._state._preview.get('a', '')), 250)
        finally:
            b.stop_workers()


class TestRunUntilIdleStillRecognisesResume(unittest.TestCase):
    """Sanity: ``run_until_idle`` treats post-resume re-pause as idle too."""

    def test_run_until_idle_returns_during_paused_resume_state(self):
        def get_preview(_id):
            while True:
                yield 'x' * 50

        b = make_browser(
            get_children=lambda _: [Item(id='a')],
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
            # Resume once.
            b.signal_preview_demand('a')
            # ``run_until_idle`` must return — even though the worker
            # is paused at a different cap window.
            b.run_until_idle()
            self.assertIsNotNone(b._preview_paused)
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
