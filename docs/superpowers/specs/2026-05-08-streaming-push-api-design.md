# browse-tui — Streaming / Push API Design

**Date:** 2026-05-08
**Status:** Draft (pre-implementation)
**Supersedes nothing.** Layers a new push-based primitive under the existing API; existing recipes are unaffected.

## Overview

The current Python API has recipes implement synchronous callbacks
(`get_children`, `get_preview`) that the framework runs on worker threads.
Returning a list — the only delivery path — forces "fetch everything before
showing anything" semantics. This works for the simple case but is awkward for
data that arrives over time: paginated remote sources, streaming LLM previews,
external push channels (websockets, MCP-push, inotify), and live per-row
updates.

This design adds a single batched mutation primitive — `browser.update_data(ops)`
— that any thread can call to push items, patches, removals, and tree
metadata into the browser. The existing callback API (`get_children`,
`get_preview`, `Action`, `from_flat_tree`, etc.) is reimplemented as sugar on
top of `update_data` and remains the canonical entry point for simple recipes.

## Goals

1. Recipes can stream items into the tree as data arrives, instead of returning
   one canonical list.
2. Recipes can push data from any thread (watchers, websockets, async tasks)
   without rolling their own dispatch glue.
3. Per-row updates and removals become first-class — no more "invalidate whole
   parent and refetch" for a one-row change.
4. Atomic batched mutations: a recipe can apply N changes in one tick, one
   render — no torn intermediate state.
5. Generators are supported anywhere a callback returns data, both for
   `get_children` (paginated remote) and `get_preview` (streaming LLM, lazy
   large-file read).
6. **Existing recipes change zero lines of code.** The push API is the new
   primitive; the callback API is documented sugar with identical behaviour
   for the cases existing recipes exercise.

## Non-goals

- Custom keystroke / event surface beyond `Action`. The existing `Action`
  registry covers every shipped recipe; raw `on('key', …)` is deferred until a
  concrete need arises.
- `asyncio` integration. Threads + the existing post queue + the self-pipe
  wakeup remain the concurrency model.
- Automatic pagination. Recipes that want demand-driven "load more" implement
  it via a sentinel item bound to an `Action` (Section 4, Example 4). A future
  iteration may automate this for `get_children` generators (>500 items →
  emit a sentinel automatically); marked as a TODO breadcrumb in the
  implementation, not in scope here.
- Cross-API atomicity with preview. `set_preview` / `append_preview` /
  `clear_preview` remain their own surface; preview ops cannot be batched
  inside `update_data`.

---

## Mental model

There are three surfaces, each with one job:

1. **Sync callbacks (sugar over the push API).** Run on worker threads. Return
   data, the framework handles the rest. The familiar today's-API entry point.
2. **Event handlers (notifications).** Run on the main thread. Pure side-effect
   hooks — cursor moved, item expanded, selection changed.
3. **Push API (`browser.update_data`).** Thread-safe. The recipe imperatively
   mutates the tree in atomic batches. Used directly by streaming recipes,
   external watchers, and Action handlers; used internally by the sync-callback
   sugar.

A recipe picks whichever combination it needs. The simple case stays simple:
`Browser(get_children=fn).run()` works exactly as today. The advanced cases
become expressible without leaving the framework: a paginated remote source is
a generator returned from `get_children`; a websocket-driven live tree calls
`update_data` from a watcher thread; a per-row update patches in place.

---

## Section 1 — surface map

### Sync callbacks (worker thread)

| Callable | Purpose | Return values |
| --- | --- | --- |
| `get_children(parent_id)` | Provide children for a parent. | `Iterable[Item]` (incl. `[]`) → appended; `Generator` → each yield is appended; `None` → recipe will push from elsewhere. |
| `get_preview(item_id)` | Provide preview text. Single-slot latest-wins worker (preserved from today). | `str` → set as full preview; `Generator[str]` → each yield is appended (eager pull until buffer cap, then pull on consumer demand); `None` → empty preview. |

### Event handlers (main thread, notifications only)

Subscribed via constructor kwargs (e.g. `Browser(on_cursor_changed=fn, …)`)
or runtime registration (`browser.on('cursor_changed', fn)`; multiple handlers
allowed, fired in registration order).

| Event | Args | When |
| --- | --- | --- |
| `cursor_changed` | `item` | Cursor lands on a non-placeholder row. |
| `expanded` | `item_id` | Expansion state flipped to expanded. |
| `collapsed` | `item_id` | Expansion state flipped to collapsed. |
| `selection_changed` | `items` | Selection set changed. |
| `started` | — | Just before first render — recipe can pre-populate via `update_data`. |
| `quit_requested` | — | About to exit; cleanup hook. |

### Push API (any thread)

| Call | Purpose |
| --- | --- |
| `browser.update_data(ops)` | Apply a batched list of tree-mutation ops on the main thread, atomically (one render afterward). See Section 2. |
| `browser.set_preview(item_id, text)` | Replace preview content. |
| `browser.append_preview(item_id, chunk)` | Append a chunk to the current preview. |
| `browser.clear_preview(item_id)` | Drop cached preview content for an id. |
| `browser.refresh(id=None)` | Invalidate the subtree (drops items from `_items_by_id` too) and re-fire `get_children`. Unchanged. |
| `browser.cursor_to`, `expand`, `select`, `message`, `error`, `quit`, `post`, `watch` | Unchanged from today. |

The previously-considered `set_children` / `append_children` / `update_items` /
`remove_items` are subsumed by `update_data` ops.

---

## Section 2 — `update_data` ops

`update_data` accepts a list of ops. Each op is a tagged tuple. Ops are applied
in list order on the main thread, in a single post-queue task — atomic with
respect to render.

| Op | Shape | Effect |
| --- | --- | --- |
| `("upsert", id, parent_id, fields)` | 4-tuple | If `id` is new → insert under `parent_id`. If `id` exists with a different parent → **move** to `parent_id`. If `parent_id` is `None` → patch fields only, leave parent unchanged (silent no-op if `id` is unknown). `fields` is a dict; matching keys override `Item` fields, unmatched keys land as custom attrs. **Patch-merge:** unspecified fields are preserved; the `Item` instance is mutated in place. |
| `("set", id, parent_id, fields)` | 4-tuple | Insert-or-replace. `fields` is the entire record; unspecified `Item` fields revert to dataclass defaults; custom attrs are dropped. A new `Item` instance is constructed. Children stored under `_children[id]` are preserved (they belong to `id` as a parent, not to the `Item` instance). |
| `("remove", id)` | 2-tuple | Remove the item with this id. Cascades: `_children[id]` is also dropped. |
| `("clear_children", parent_id)` | 2-tuple | Remove all known children of `parent_id`. The cache entry for `parent_id` reverts to "no fetch yet"; loading flag is reset accordingly. |
| `("complete", parent_id)` | 2-tuple | Mark "no more children coming" — clears any "loading more" indicator. |
| `("incomplete", parent_id)` | 2-tuple | Mark "more children coming" — explicit override of the inferred loading flag. |

### Helpers

The framework exports helper constructors so recipes don't hand-roll tuples:

```python
from browse_tui import upsert, set_item, remove, clear_children, complete, incomplete
```

| Helper | Returns |
| --- | --- |
| `upsert(id, parent_id, **fields)` | `("upsert", id, parent_id, fields)` |
| `set_item(id, parent_id, **fields)` | `("set", id, parent_id, fields)` |
| `remove(id)` | `("remove", id)` |
| `clear_children(parent_id)` | `("clear_children", parent_id)` |
| `complete(parent_id)` | `("complete", parent_id)` |
| `incomplete(parent_id)` | `("incomplete", parent_id)` |

The helper for `set` is named `set_item` because `set` is a Python builtin and
shadowing it via `from browse_tui import set` would be hostile to recipes.

### Behaviours

**Identity.** Items are keyed by `item.id` (must be hashable; same invariant
as today's `state.expanded`, `state.selected`). A new `_items_by_id` index is
maintained next to `_children` for O(1) lookups by `update_data`.

**Reparenting.** `("upsert", "x", "new_parent", …)` when `"x"` already lives
under `"old_parent"`: framework removes from `old_parent`'s children list,
appends to `new_parent`'s, updates `_parent_of_id["x"]`. Atomic within the
batch; if subsequent ops in the same batch reference `"x"`, they see it under
its new parent.

**Patch-only upsert.** `("upsert", "x", None, fields)` patches `"x"`'s fields
in place without touching parent. Ignored (silent debug-level log) if `"x"` is
unknown — out-of-order pushes from background sources will hit this naturally.

**Orphan items.** Pushing `("upsert", "x", "unknown_parent", …)` is allowed.
The item lands in `_children["unknown_parent"]`. If an item with
`id="unknown_parent"` later becomes known and is expanded, `"x"` becomes
visible. No `get_children` is fired for an already-populated parent. Recipes
that want a fresh fetch call `browser.refresh(parent_id)`.

**Order within a batch.** Ops apply in list order. The framework does not
reorder or deduplicate. `[upsert("a", "/", …), remove("a")]` yields no
`"a"` — recipe's responsibility. `complete` followed by an upsert into the
same parent silently flips the parent back to incomplete; framework does not
try to outsmart the recipe.

**Cross-batch atomicity.** A single `update_data(ops)` call is atomic. Two
separate `update_data` calls — even from the same thread — are not; each is
its own post-queue task and renders separately.

### Loading flag rule

| Trigger | Effect |
| --- | --- |
| `get_children(parent_id)` is dispatched (main thread, before worker runs) | `loading[parent_id] = True` |
| Handler returns `None` | Loading stays set (recipe will push from elsewhere). |
| Handler returns any iterable, even `[]` | Worker appends a trailing op to its delivery batch that clears `loading[parent_id]`. Atomic with the data items. |
| Generator: each yield | Yielded chunk is delivered as its own batch (no loading change). |
| Generator: `StopIteration` | Worker emits a final batch whose only op clears `loading[parent_id]`. |
| `("complete", parent_id)` op | `loading[parent_id] = False`. |
| `("incomplete", parent_id)` op | `loading[parent_id] = True`. |
| Any other op (`upsert`, `set`, `remove`, `clear_children`, preview ops) | No effect on the loading flag. |

The auto-clear is implemented entirely on the worker side as the *last op of
the batch* — main thread never adjusts loading except via dispatch and via
explicit ops. This keeps state mutation in one place.

**Documented corner case.** A recipe that returns initial data from
`get_children` *and* expects later watcher updates to re-show "loading…" must
explicitly call `update_data([incomplete(parent_id)])` after returning. The
auto-clear on return-with-iterable is unconditional. Acceptable trade-off for
now; revisit if it becomes painful.

---

## Section 3 — sugar layer (existing API, unchanged for recipes)

### Public surface (unchanged)

| Today | Tomorrow | Notes |
| --- | --- | --- |
| `Browser(get_children=fn, get_preview=fn, actions=…, on_enter=…, format_item=…, …)` | identical | constructor signature preserved |
| `Action`, `Context`, `Pending`, `Item` | identical | all dataclasses / classes keep their public surface |
| `to_item` coercion | identical | mixed `Item | str | tuple | dict` lists still accepted |
| `Browser.from_flat_tree(rows, …)` | identical | internally one `update_data` batch |
| All thread-safe ops: `refresh`, `cursor_to`, `expand`, `select`, `message`, `error`, `quit`, `post`, `watch` | identical | `refresh(p)` invalidates the subtree (drops `_items_by_id` entries too) and re-fires `get_children(p)` |
| All `Context` sub-flows: `run_external`, `page`, `input`, `confirm`, `pick`, `insert` | identical | main-thread blocking sub-flows are unaffected |

### Internal mapping for `get_children`

Pseudo-code for the sugar wrapper that runs on the existing `_children_worker`:

```
main thread: dispatch get_children(parent_id):
  loading[parent_id] = True
  enqueue worker task: run_user_get_children(parent_id, user_fn)

worker thread: run_user_get_children(parent_id, user_fn):
  result = user_fn(parent_id)
  if result is None:
    return                      # loading stays set; recipe pushes elsewhere

  if isgenerator(result):
    # TODO: future pagination — when total yielded items >= 500, stop pulling
    # and append a sentinel "more…" item bound to an action that resumes
    # the generator. For now, drain the generator fully.
    for chunk in result:
      items = [to_item(x) for x in iter_chunk(chunk)]
      browser.update_data([upsert(it.id, parent_id, **fields_of(it)) for it in items])
    browser.update_data([complete(parent_id)])     # clears loading on exhaustion
  else:
    items = [to_item(x) for x in result]
    browser.update_data(
      [upsert(it.id, parent_id, **fields_of(it)) for it in items]
      + [complete(parent_id)]                      # trailing clear, atomic with items
    )
```

`fields_of(item)` materialises the item's dataclass fields plus any custom
attributes so they survive the upsert.

`iter_chunk(chunk)` distinguishes "one item" from "many items" by type:
`isinstance(chunk, list)` → iterate as a batch; anything else (including
`Item`, `tuple`, `dict`, `str`) → treat as a single item, coerced via
`to_item`. So `yield ('a', 'A')` is one item (the tuple shorthand);
`yield [Item('a'), Item('b')]` is two. Same flexibility as today's mixed
return lists, applied per-yield.

### Internal mapping for `get_preview`

Largely unchanged from today — single-slot latest-wins worker, cursor-move
abandons by no longer pulling from the generator (or discarding the str
result). Generator support adds a buffered eager-pull policy:

```
worker: run_user_get_preview(item_id, user_fn):
  result = user_fn(item_id)
  if result is None: result = ''

  if isgenerator(result):
    for chunk in result:
      browser.append_preview(item_id, chunk)
      while preview_buffer_size(item_id) >= cap and not consumer_near_end(item_id):
        sleep_briefly_or_wait_on_demand_signal()
        if cursor_moved_to_other_item(): return    # abandon
    # generator exhausted
  else:
    browser.set_preview(item_id, result)
```

Cap is configurable; default ~ a few screens of content. `consumer_near_end`
is signaled by the renderer when the user scrolls the preview within N rows
of the buffered tail.

### `Context` additions

Pass-throughs to the new push API, so action handlers don't need a `browser`
reference to do batched work:

```python
ctx.update_data(ops)
ctx.set_preview(id, text)
ctx.append_preview(id, chunk)
ctx.clear_preview(id)
ctx.upsert(id, parent, **fields)         # convenience for one upsert
ctx.set_item(id, parent, **fields)       # convenience for one set
ctx.remove(id)                           # convenience for one remove
ctx.run_in_worker(fn)                    # one-shot worker submission
```

All existing `ctx.*` methods are preserved.

### Observable semantic change for old recipes

If a recipe has both `get_children(p)` and a watcher that pushes for the same
`p`, and they overlap, today the `get_children` return *clobbers* the watcher's
pushes. Tomorrow it *appends* — items may interleave, none are lost.

None of the shipped recipes (`browse-fs`, `browse-claude`, `browse-plan`,
`browse-jira`, `browse-mcp`, `browse-procs`, etc.) do this, so existing test
suites pass unchanged. A third-party recipe relying on clobber semantics
would need to switch its watcher to push only into already-fetched parents,
or use `update_data([clear_children(p), …upserts…])` to do its own atomic
replace.

---

## Section 4 — worked examples

### Example 1 — paginated remote source as a streaming generator

The natural shape for jira / databases / any cursor-paginated API.

```python
def get_children(parent_id):
    page = 0
    while True:
        rows = jira_search(parent_id, offset=page * 100, limit=100)
        if not rows:
            return
        yield [Item(id=r.key, title=r.summary, tag=r.status) for r in rows]
        page += 1

Browser(get_children=get_children).run()
```

Items appear screen-by-screen as pages arrive. Loading flag clears when the
generator exhausts. Cursor-move-abandons close the generator (recipe's
`finally` releases any held resource).

### Example 2 — external push driving the UI

Inotify, websocket, MCP-push, etc. — the recipe runs `update_data` from a
watcher thread.

```python
from browse_tui import Browser, upsert, remove

def initial_load(parent_id):
    return [Item(id=p, title=os.path.basename(p), has_children=os.path.isdir(p))
            for p in sorted(os.listdir(parent_id))]

def inotify_watcher(browser):
    for ev in inotify_subscribe():
        if ev.kind == 'created':
            browser.update_data([
                upsert(ev.path, ev.parent,
                       title=os.path.basename(ev.path),
                       has_children=ev.is_dir),
            ])
        elif ev.kind == 'deleted':
            browser.update_data([remove(ev.path)])
        elif ev.kind == 'modified':
            browser.update_data([
                upsert(ev.path, None, mtime=ev.mtime),    # parent_id=None: patch only
            ])

b = Browser(get_children=initial_load)
b.watch(inotify_watcher)
b.run()
```

The watcher mutates the tree from outside `get_children` entirely. The
`parent_id=None` patch updates `mtime` without touching tree shape.

### Example 3 — live per-row updates layered on a snapshot fetch

Process browser with live CPU% in the tag.

```python
def get_children(_):
    return [Item(id=str(p.pid), title=p.name(), tag='0%') for p in psutil.process_iter()]

def cpu_pulse(browser):
    while True:
        time.sleep(1.0)
        ops = []
        for p in psutil.process_iter():
            try:
                cpu = p.cpu_percent()
            except psutil.NoSuchProcess:
                ops.append(remove(str(p.pid)))
                continue
            ops.append(upsert(str(p.pid), None,
                              tag=f'{cpu:.0f}%',
                              tag_style='red' if cpu > 50 else 'dim'))
        browser.update_data(ops)              # one batch, one render

b = Browser(get_children=get_children)
b.watch(cpu_pulse)
b.run()
```

Single `update_data` per tick = one atomic render of all the per-row updates.
No flicker.

### Example 4 — demand-driven pagination via sentinel + Action

When pure-push semantics meet a UX need for explicit "load more."

```python
from browse_tui import Browser, Action, Item, upsert, remove

PAGE = 100
_next_offset = {}    # parent_id -> next offset to fetch

def get_children(parent_id):
    _next_offset[parent_id] = PAGE
    rows = fetch(parent_id, 0, PAGE)
    out = [Item(id=r.id, title=r.title) for r in rows]
    if len(rows) >= PAGE:
        out.append(Item(id=f'__more__:{parent_id}', title='⟳ Load more…', tag='+'))
    return out

def load_more(ctx):
    if not ctx.cursor.id.startswith('__more__:'):
        return
    parent = ctx.cursor.id.split(':', 1)[1]
    off = _next_offset[parent]
    rows = fetch(parent, off, PAGE)
    _next_offset[parent] = off + PAGE

    ops = [remove(ctx.cursor.id)]
    ops += [upsert(r.id, parent, title=r.title) for r in rows]
    if len(rows) >= PAGE:
        ops.append(upsert(f'__more__:{parent}', parent, title='⟳ Load more…', tag='+'))
    ctx.update_data(ops)
    if rows:
        ctx.cursor_to(rows[0].id)

Browser(
    get_children=get_children,
    actions=[Action('enter', 'Load more / open', load_more, 'cursor')],
).run()
```

The sentinel is a regular `Item` with a recognisable id prefix. The handler
replaces it with the next page atomically — one batch, one render. Composes
cleanly with Example 2 if a watcher also pushes; each is its own batch.

---

## Implementation impact

### New state in `Browser` / `state`

- `_items_by_id: dict[Hashable, Item]` — primary index for `update_data`
  lookups by id. Maintained by every op that adds, replaces, or removes
  items.
- `_parent_of_id: dict[Hashable, Hashable]` — reverse index for reparenting
  in `("upsert", id, new_parent, …)`. Cheap, O(1).
- `_loading: dict[Hashable, bool]` — explicit loading flag per parent. Today
  this is implied by membership in `_children_pending`; with the new ops
  it becomes an addressable piece of state.

`_children: dict[parent_id, list[Item]]` keeps its current shape. The
visible-tree builder does not change.

### Where ops apply

`update_data(ops)` posts a single callable to the main-thread queue:

```python
def update_data(self, ops):
    self.post(lambda: self._apply_ops(ops))
```

`_apply_ops` walks the list, mutating `_children`, `_items_by_id`,
`_parent_of_id`, `_loading` in place; flips `_visible_dirty`; returns. The
next render rebuilds the visible cache and paints.

### Worker-thread changes

The `_children_worker` keeps its FIFO; the difference is what it does with the
return value:

- Old: `state._children[id] = items` directly + signal via `_children_results`.
- New: builds an `update_data` batch and calls `browser.update_data(...)`,
  which goes through the same post queue. The trailing "loading clear" is
  appended to the batch.

The `_preview_worker` keeps its single-slot latest-wins shape; generator
support adds the buffered eager-pull loop with a cap and a
"consumer-near-end" demand signal driven by the renderer.

### Tests

Existing tests should pass without changes (per the unchanged-public-surface
guarantee). New tests cover:

- Each `update_data` op type, including reparenting, orphan upserts, patch-only
  upserts, cascade on remove.
- Atomic batch application (no in-between renders observable).
- Loading-flag transitions for: list return, empty list return, generator
  return, `None` return, explicit `complete`/`incomplete` ops.
- Generator preview: streaming append, cursor-move abandon, buffer cap +
  consumer-demand resume.
- Concurrent watcher push + `get_children` return: no items lost, ordering
  documented (append).

---

## Migration

There is no migration. Existing recipes work unchanged. The new API is
additive — recipes opt in by using generators in `get_children` /
`get_preview`, by calling `update_data` from watcher threads, or by mixing
the two.

The single observable semantic change (concurrent watcher push + handler
return now appends rather than clobbers) does not affect any shipped recipe;
documented in Section 3 for third-party recipe authors.

---

## Future work (deferred)

- **Automatic pagination for `get_children` generators.** Drain up to N
  items (default ~500), then synthesise a sentinel "more…" item bound to a
  built-in action that resumes the generator. Removes the boilerplate from
  Example 4. Gated behind a `Browser(auto_paginate=True)` kwarg or similar.
  Marked as a TODO in the generator code path.
- **Loading-flag corner case for return + later watcher updates.** The
  auto-clear on return-with-iterable is unconditional today. If a recipe
  pattern emerges where this is painful, consider making the auto-clear
  conditional on "no `incomplete(parent_id)` op is queued in the same tick"
  or similar.
- **Preview ops inside `update_data`.** If a recipe needs cross-domain
  atomicity (e.g., remove an item *and* its preview in one tick), fold
  `("preview_set", id, text)`, `("preview_append", id, chunk)`,
  `("preview_clear", id)` into the op vocabulary. Not in scope until a
  recipe needs it.
- **Cancellation hint event.** A `children_no_longer_needed(parent_id)`
  notification could let recipes stop expensive in-flight work when the
  user scopes away. Recipe-driven cancellation (drop a watcher subscription,
  cancel a future) suffices for now.

---

## See also

- [docs/api.md](../../api.md) — current public Python API.
- [docs/internals.md](../../internals.md) — module layout, threading model,
  caches, post queue.
- [docs/superpowers/specs/2026-04-30-browse-tui-design.md](2026-04-30-browse-tui-design.md)
  — original architectural spec.
