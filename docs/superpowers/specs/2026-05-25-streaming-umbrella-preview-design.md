# Streaming umbrella preview: drain-on-tail, abandon-clears, screen-sized cap

Date: 2026-05-25

## Problem

Opening a large `.jsonl` lands the cursor on the synthetic file scope_card
row. `get_preview` runs `_preview_umbrella`, which `_collect_umbrella_preview`
walks every record in the file synchronously. The preview pane blocks until
the cascade completes — multi-second freeze on large transcripts. Any other
preview that walks heavy state has the same liability.

The framework already supports streaming previews — `get_preview` may
return a generator; the worker drains, buffers, pauses at a cap, resumes
on demand. But the recipe doesn't use it, and three gaps in the framework
make it unsuitable for our use case:

1. The cap is hardcoded (`100k chars` / `1k lines`), not screen-sized.
2. The tail-pin (Shift-End) drains one cap-window per render tick — adds
   latency proportional to render interval when the user explicitly asks
   for the whole preview now.
3. An abandoned generator (cursor moved before exhaustion) leaves a
   partial preview in `Item.preview`. The next visit treats it as
   complete and skips the re-fetch.

## Goals

* Replace the cascade-as-string path in the browse-claude umbrella with a
  generator that yields one leaf body per chunk.
* Framework: drain-without-pause while `_preview_at_tail` is engaged.
* Framework: discard `Item.preview` for the abandoned id on cursor-move
  cancel (and on stop). Keep on clean `StopIteration` and on mid-stream
  exception (partial+error stub is informative).
* Framework: derive the buffer cap from preview-pane height at each pause
  cycle.
* Recipe: keep leaf-preview side effects (`set_preview_op`) working;
  batch them in small windows so partial streams still benefit.

## Non-goals

* No cancellation-token API for recipes. The generator model already
  delivers cancellation through `gen.close()` raising `GeneratorExit`;
  recipes use `finally:` if they hold resources.
* No async preview thread. The existing single-flight worker stays.
* No change to non-generator preview returns. Recipes that return a
  string still get the current cache-on-deliver behavior.
* No change to per-leaf preview shape — leaf bodies are still complete
  atomic units; the umbrella's incompleteness is at the umbrella level.

## Design

### Framework changes (`src-tui/040-state.py`, `src-tui/050-render.py`)

1. **Discard partial preview on abandon.**

   In `_stream_preview_from_generator`, when the loop returns because
   `_preview_req != item_id` (cursor-move) or `_stop` woke us, post a
   `_deliver_preview(item_id, None)` (or equivalent cache-clear) so
   `Item.preview` is reset to `None`. Same cleanup in
   `_abandon_paused_preview_if_any`.

   Three paths preserve the cache:
   * Clean `StopIteration` — buffered content is the final preview.
   * Mid-stream exception — partial buffer plus the `[error]` tag is
     intentionally surfaced (existing behavior).
   * Side-effect ops on other items (the umbrella's `set_preview_op`
     for leaves) — those items are not the abandoned generator's id;
     the framework only clears its own id.

   Re-request of the abandoned id later refetches fresh.

2. **Drain-when-pinned.**

   In the pause check inside `_stream_preview_from_generator`, before
   recording `_preview_paused` and waiting on `_preview_resume_event`,
   check `browser._preview_at_tail`. When the tail-pin is engaged the
   worker advances the cap window in-place and continues pulling without
   parking. The user has explicitly asked for the whole preview; latency
   between cap windows is exactly the friction this removes.

   The user accepts unbounded memory growth while pinned — Shift-End is
   a deliberate command. If memory becomes an issue in practice, a hard
   secondary cap (e.g. 10× normal cap) can be added later; not in scope.

3. **Screen-derived cap.**

   Replace `cap_lines = self._preview_buffer_cap_lines` (read once) with
   a method `self._preview_cap_lines()` that returns
   `max(preview_pane_height * STREAM_CAP_FACTOR, MIN_CAP_LINES)`.

   * `STREAM_CAP_FACTOR = 3` — three screens of buffered scrollback. The
     existing `_PREVIEW_DEMAND_THRESHOLD = 12` then triggers re-pull
     well before the user scrolls off the end.
   * `MIN_CAP_LINES = 50` — guard against a freshly-launched browser
     where the pane height isn't known yet.

   The cap is re-read at each pause cycle so resizing the terminal
   widens / narrows the next window naturally. `cap_chars` stays as a
   memory safety net (current default fine).

### Recipe changes (`recipes/browse-claude`)

4. **Generator umbrella.** `_preview_umbrella` becomes a generator:

   ```python
   def _preview_umbrella(item_id):
       card_path = _scope_card_path(item_id)
       if card_path is not None:
           try:
               td = _scan_tree(card_path)
               yield _fmt_scope_card(card_path, td) + '\n\n'
           except Exception as e:
               yield _rule(f'error in scope card: {e}', RED) + '\n\n'
       yield from _collect_umbrella_preview(item_id)
   ```

5. **Generator collector.** `_collect_umbrella_preview` becomes a
   generator yielding `<rendered_body> + '\n\n'` per leaf. Side-effect
   accumulation (`ops`, `leaf_previews`) is moved to a module-local
   helper that flushes every `STREAM_BATCH = 25` records via
   `_BROWSER.update_data`. A `finally:` flushes any remainder on
   `StopIteration` or `GeneratorExit`.

   Children iteration order is preserved (document order); the existing
   recursive descent through inner umbrellas stays intact.

6. **`get_preview` returns the generator** when the heavy umbrella
   branch fires:

   ```python
   if _is_cross_file_id(item_id):
       if _item_is_active(item_id):
           return _preview_umbrella(item_id)   # generator
       file_path = _scope_card_path(item_id)
       return _preview_file_metadata(file_path) if file_path else ''
   if '#prompt:' in item_id or '#tool:' in item_id or '#span:' in item_id:
       return _preview_umbrella(item_id)        # generator
   ```

   All other branches still return strings.

7. **Cache flow.** No recipe-side accounting of "did the umbrella
   finish?" The framework's per-id `Item.preview` is the cache:
   * Yielded chunks land in cache via `append_preview` mid-stream.
   * `StopIteration` leaves the buffered concatenation as the final
     cached preview.
   * Cursor-move abandon clears the id's cache (framework change 1).
   * Side-effect `set_preview_op` writes for leaf rows are atomic and
     stay valid regardless of umbrella's completion state.

## Behavior summary

| Event | Cursored id's `Item.preview` | Visible UI |
|---|---|---|
| Open large file, cursor on scope_card | grows incrementally; cap-paused at `~3 * pane_height` lines | shows first window immediately, expands as worker pulls |
| User scrolls to tail-1 of buffered | demand signal fires, worker resumes one window | another window appended |
| User presses Shift-End | tail-pin set; worker drains without pausing | preview grows as fast as worker can pull |
| User moves cursor off | framework clears the abandoned id's preview to `None`; generator's `finally` flushes any pending side-effect ops | new id's preview begins fetching |
| User comes back to the same id | cache is `None`, fresh fetch | streams again from scratch |
| Generator runs to `StopIteration` | full content stays in cache | next visit paints instantly from cache |

## Open questions

None. All branch points decided in the prior discussion:
* drain-without-pause when pinned (no memory cap while pinned)
* framework clears abandoned id's cache, leaves side-effect writes
* screen-sized cap re-read per pause
* batch flush every 25 records inside the recipe
* no spec for per-leaf invalidation when umbrella is abandoned (leaves
  stay; their content is complete)
