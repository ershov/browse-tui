# browse-tui — Internals (contributor docs)

What you need to know to navigate, modify, and extend the source. Aimed at
contributors, not recipe authors.

For the user-facing surface see [MANUAL/api.md](../MANUAL/api.md), [MANUAL/cli.md](../MANUAL/cli.md),
[MANUAL/recipes.md](../MANUAL/recipes.md). For the original architectural rationale see
[docs/superpowers/specs/2026-04-30-browse-tui-design.md](superpowers/specs/2026-04-30-browse-tui-design.md).

---

## Module layout

The single-file `browse-tui` executable is built by concatenating numbered
files from `src-tui/`:

```
src-tui/
  010-prelude.py    shebang, stdlib imports, __version__
  020-terminal.py   raw mode, key reader, signal handlers, mouse, self-pipe
  030-data.py       Item dataclass, to_item coercion
  040-state.py      State, visible-tree builder, scope, Pending, Browser
  050-render.py     3-pane layout, item formatting, search highlight, status bar, _HELP_TEXT
  060-context.py    Context (main-thread-only sub-flows: input/confirm/pick/insert/run_external/page)
  070-actions.py    Action dataclass, default keymap, dispatch_key, _handle_insert_key
  080-cli.py        argparse, format parsers, recipe runners, --install/--uninstall
  090-main.py       entry point: main() → cli.main()
```

### Layering rule

```
prelude  ←  terminal  ←  data  ←  state  ←  render  ←  context  ←  actions  ←  cli  ←  main
```

Each layer references only symbols from numerically-lower files. The build
script concatenates in numeric order, so this rule is enforced by the file
order itself: a forward reference would break the build.

In practice the layer boundaries are:

| Layer    | Responsibility                                           | Holds state? |
| -------- | -------------------------------------------------------- | ------------ |
| terminal | raw I/O, key parsing, signals, mouse                     | global flags only |
| data     | record types, coercion                                   | no           |
| state    | tree caches, workers, post queue, Pending, Browser       | yes — `State` + `Browser` instances |
| render   | painting; reads state, never writes                      | no — pure-ish |
| context  | main-thread sub-flows wrapping Browser                   | no — wraps Browser |
| actions  | keymap + dispatch                                        | no           |
| cli      | argparse, parsers, install, recipe runners                     | no           |
| main     | thin wrapper; delegates to `cli.main`                    | no           |

### File sizing (current)

| File             | Lines |
| ---------------- | ----- |
| 010-prelude.py   | 7     |
| 020-terminal.py  | 468   |
| 030-data.py      | 70    |
| 040-state.py     | 1442  |
| 050-render.py    | 1205  |
| 060-context.py   | 554   |
| 070-actions.py   | 962   |
| 080-cli.py       | 1000  |
| 090-main.py      | 5     |

The single-file build is around 5,700 lines.

---

## Build script (`build-tui.sh`)

```bash
./build-tui.sh
```

Concatenates `src-tui/[0-9]*` files in lexical order. Adds source-section
markers (`# SOURCE START: file {{{` / `# }}} # SOURCE END: file`) so a stack
trace pointing into the single-file binary is greppable back to the source
module.

### Executable-section feature

Files in `src-tui/` whose stem ends in `+` and which are `chmod +x` are
*executed*; their stdout becomes the section's content. This lets you
generate sections from external sources (e.g. a help-text JSON file → Python
literal). No section currently uses this, but the build script supports it.

### Build invariants

- The shebang from `010-prelude.py` is preserved at the top of the output.
- The output is `chmod 755`.
- `chmod +x browse-tui` is the only step needed to make it runnable.

---

## Concatenated-build vs test-loaded modules

The single-file build runs every section in one shared namespace — every
function, class, and constant from every file is a name in `globals()`. So
`040-state.py`'s `Browser.run` can reference `term_init`, `read_key`,
`render_full`, `dispatch_key` as bare names: at runtime, those resolve from
the unified namespace.

But for **unit tests**, we don't want to require building. Tests load the
individual `src-tui/0*.py` files as separate modules and use a loader to
inject cross-module references:

```
test/_loader.py        loads each NNN-foo.py as a fresh module,
                       then patches expected names from earlier
                       modules onto each loaded module.
```

So `040-state.py` (the loaded test module `_state`) gets its `term_init`,
`read_key`, etc. injected by the loader. Production builds don't need any
patching — the names are already in scope.

**Practical implication for contributors:** when you add a cross-module
reference in `0NN-foo.py`, you must also tell the loader about it. The
relevant snippet in `test/_loader.py` lists which names get injected onto
which loaded module.

When in doubt: run `./run-tests.sh`. A test that fails with `NameError:
name 'X' is not defined` is the loader missing a `_state.X = _terminal.X`
line.

---

## Threading model

```
                main thread
                    │
                    ├── post queue ◀── worker threads
                    │                     ├── _children_worker (FIFO; delivers via update_data)
                    │                     └── _preview_worker  (latest-wins; streams via append_preview)
                    │
                    └── watcher threads ─▶ post queue
                        (recipe-spawned via browser.watch;
                         typically calls browser.update_data)
```

The post-queue funnel runs every closure (including the one
`update_data` builds, which calls `apply_ops` on `state`) on the main
thread.

| Thread             | Purpose                                          | Pattern               |
| ------------------ | ------------------------------------------------ | --------------------- |
| main               | run loop, render, key dispatch, all state mutation | blocking `read_key()` |
| `_children_worker` | call `get_children`, deliver via `update_data`   | FIFO of parent ids    |
| `_preview_worker`  | call `get_preview`, deliver to single-slot or stream via `append_preview` | single-slot, latest-wins |
| watcher (per recipe) | call user's polling callback                   | daemon, recipe-defined |

### The post queue

Every cross-thread mutation goes through `Browser.post(fn)`:

```python
def post(self, fn):
    self._main_queue.put(fn)    # queue.Queue, thread-safe
    notify_wake()               # write 1 byte to self-pipe → unblocks read_key
```

The main loop drains it on every wake:

```python
def drain_main_queue(self):
    while True:
        try:
            fn = self._main_queue.get_nowait()
        except queue.Empty:
            return
        fn()
```

This is the **only** path background threads use to mutate Browser state.
No locks anywhere — the GIL gives us single-op atomicity, and the
post-queue funnel keeps multi-op sequences single-threaded.

### `update_data` → post → `apply_ops`

`Browser.update_data(ops)` is the push-API entry point (see
`docs/superpowers/specs/2026-05-08-streaming-push-api-design.md`). It
snapshots `ops` to a list on the calling thread (so a mutating live
source isn't captured), then posts a single closure:

```python
def update_data(self, ops):
    ops_list = list(ops)
    def _apply():
        apply_ops(self._state, ops_list)
        self._needs_redraw.add('list')
        self._needs_redraw.add('children')
    self.post(_apply)
```

`apply_ops(state, ops)` (in `040-state.py`) walks the op list in order,
mutating `_children`, `_items_by_id`, `_parent_of_id`, `_loading` in
place and flipping `_visible_dirty` if anything structural changed.
The whole batch is one drain — atomic with respect to render. The
`_needs_redraw` flags ensure a watcher-driven push paints without
waiting for the user to press a key (regression guard #290).

The six op kinds — `upsert`, `set`, `remove`, `clear_children`,
`complete`, `incomplete` — are each implemented by a small `_apply_*`
helper plus a module-level helper constructor (`upsert`, `set_item`,
`remove`, `clear_children`, `complete`, `incomplete`). Exported from
`browse_tui` for recipes; see [MANUAL/api.md](../MANUAL/api.md) for the
reference table.

`Context` exposes pass-throughs (`update_data`, `upsert`, `set_item`,
`remove`, `set_preview`, `append_preview`, `clear_preview`,
`run_in_worker`) so action handlers don't need a separate `browser`
reference.

### Worker delivery

The `_children_worker` is built on top of `update_data`:

| User return                  | Worker delivery                                                                                                                    |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| iterable (incl. `[]`)        | One batch: `[upsert(...) for it in items] + [complete(parent_id)]` — atomic with the trailing loading-clear.                       |
| generator                    | One batch per `yield` (no `complete`); `_stream_children_from_generator` drains, posting each chunk. On `StopIteration` a final `[complete(parent_id)]` clears loading. |
| generator, mid-stream raise  | Partial batches stay; loading stays True; error surfaced via `browser.error(...)`. No `complete` op is emitted.                    |
| `None`                       | No batch posted; `_loading[parent_id]` stays True (recipe pushes from elsewhere). Pendings still resolve via `_post_children_delivery`. |

After the data batch, the worker schedules `_post_children_delivery`
which (1) ensures `_children[parent_id]` exists as at least an empty
list, (2) clears `_children_pending`, (3) marks visible dirty + clamps
the cursor, and (4) resolves every Pending registered for the parent.

Generator `get_preview` follows the same shape but uses
`append_preview` per yield — see *Caches and invalidation* below.

### `_apply_upsert` carve-out

`_apply_upsert(state, id, parent_id, fields)` treats `parent_id=None`
on an unknown id as a silent debug-level drop (out-of-order patches
from background sources are normal). The exception: when
`state.root_id is None` (the framework default), `parent_id=None` *is*
the root, so the upsert really is meant to insert under root — the
`_children_worker` delivering for a None-rooted Browser depends on
this. Disambiguation is `state.root_id is not None`: only treat
`parent_id=None` as patch-only when the Browser has a non-None root.

### Self-pipe wakeup

`notify_wake()` writes one byte to a pre-opened pipe. The terminal's
`read_key()` selects across stdin and the read end of the pipe; a wake-byte
makes it return the synthetic key name `_notify`, so the main loop runs one
more iteration to drain.

This is borrowed from plan-tui, which borrows it from the standard
self-pipe-trick playbook.

---

## Caches and invalidation

Three caches, all keyed on item id:

| Cache                  | Key       | Populated by                      | Invalidated by                |
| ---------------------- | --------- | --------------------------------- | ----------------------------- |
| `state._children`      | parent_id | `_children_worker` async          | `ctx.refresh(id)` or full     |
| `state._preview`       | item_id   | `_preview_worker` async           | same                          |
| `state._visible_cache` | n/a       | `visible_items()` DFS over above  | any `_visible_dirty=True` set |

`_children` is the canonical tree cache: a missing key means "not fetched
yet" (the visible-tree builder shows a `⧗ loading…` placeholder under any
expanded parent that's missing from the cache).

`_preview` is keyed on the item under the cursor. Latest-wins: rapid cursor
moves coalesce — only the final one's fetch survives.

`_visible_cache` is the rendered flat list of `VisibleEntry` objects. It's
identity-stable across reads — the renderer can compare by `is` to detect
"nothing has changed" between paints.

### Auxiliary indexes (push-API bookkeeping)

Three side-tables maintained alongside `_children` so `apply_ops` can do
O(1) lookups:

| Field                    | Maps                       | Purpose                                                        |
| ------------------------ | -------------------------- | -------------------------------------------------------------- |
| `state._items_by_id`     | id → `Item`                | Primary index for `update_data` lookups by id.                 |
| `state._parent_of_id`    | child id → parent id       | Reverse index for reparenting in `("upsert", id, new_parent)`. |
| `state._loading`         | parent id → bool           | Explicit loading flag, addressable by `complete`/`incomplete`. |

These are kept in lockstep with `_children` by every mutation site
(`_apply_upsert` / `_apply_set` / `_apply_remove` / `_apply_clear_children`
in `040-state.py`, plus `_index_drop_children` / `_index_add_children` for
the legacy delivery paths). `_drop_subtree_indexes` recursively cleans
descendants on remove/clear-children.

### Invalidation rules

- `cache_invalidate_subtree(state, id)` drops one key from `_children`
  (and the corresponding `_items_by_id` / `_parent_of_id` entries plus
  `_loading[id]`), flips `_visible_dirty`. Used by `ctx.refresh(id)`.
- `cache_invalidate_all(state)` clears `_children`, `_items_by_id`,
  `_parent_of_id`, `_loading`, flips `_visible_dirty`. Used by
  `ctx.refresh()` (no id).
- `mark_visible_dirty(state)` flips `_visible_dirty` only — used after
  `expanded`/`scope_stack`/`selected` mutations.

The next call to `visible_items(state)` rebuilds the visible cache.

### Preview generator pause/resume

`get_preview` returning a generator switches the preview worker into
`_stream_preview_from_generator`. Each yield is appended via
`append_preview` (post-queue, race-free); the worker tracks running
`chars` / `lines` against the configurable caps
(`Browser(preview_buffer_cap_chars=100_000, preview_buffer_cap_lines=1000)`).

When a cap is hit, the generator is **paused** (not closed) and the
worker waits on `_preview_resume_event`. The pause/resume state lives
on Browser:

| Field                                  | Purpose                                                                                          |
| -------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `_preview_paused`                      | Dict `{id, gen, chars, lines}` describing the paused generator, or `None`. Locked by `_preview_lock`. |
| `_preview_resume_pull`                 | One-shot flag set by `signal_preview_demand`; the worker checks this on wake to decide whether to resume. |
| `_preview_resume_event`                | `threading.Event` the worker blocks on; set by `request_preview` (cursor-move) or `signal_preview_demand`. |
| `_preview_demand_signal_state`         | `(id, scroll)` tuple — the renderer's debounce so it only fires the wake once per id+scroll combination. |
| `_preview_lock`                        | Guards mutations across `_preview_paused` / `_preview_resume_pull` / `_preview_demand_signal_state`. |

Wake conditions while paused:

* **Cursor-move** — `_preview_req` no longer equals the paused id.
  Worker closes the generator (firing recipe `finally`) and returns.
* **Demand resume** — `_preview_resume_pull == True`. Worker clears
  the paused state, advances cap thresholds by one window, and
  resumes pulling.
* **Stop** — `_stop == True`. Worker exits without closing (the
  daemon thread is going away anyway).

`_abandon_paused_preview_if_any(except_id)` is the path the outer
preview worker uses to close a stale paused generator before serving a
new request.

### Demand signal (renderer → worker)

`render_preview` (`050-render.py`) tracks `_PREVIEW_DEMAND_THRESHOLD` (12
wrapped rows). When the visible window is within that many rows of the
buffered tail AND the cursored id matches the paused id, it calls
`browser.signal_preview_demand(item_id)` — sets `_preview_resume_pull`
under the lock and signals `_preview_resume_event`. Idempotent and
debounced via `_preview_demand_signal_state`.

`signal_preview_demand` is technically public (no leading underscore)
so a recipe could drive it manually, but in practice it's the renderer's
hook — recipes use `append_preview` / generator caps directly.

---

## Pending registry

`Pending` is the return type of `refresh`, `cursor_to`, `expand`. It tracks
done/cancelled state and a chain of `then()` callbacks.

### Coalescing

`_children_in_flight: dict[id, list[Pending]]` is the in-flight registry.
When `refresh(id)` runs:

```python
def _do_refresh(self, id_, pending):
    cache_invalidate_subtree(self._state, id_)
    self._children_in_flight.setdefault(id_, []).append(pending)
    if id_ not in self._state._children_pending:
        self._state._children_pending.add(id_)
        self._children_queue.append(id_)        # enqueue worker
        self._children_event.set()
    # else: piggyback on the existing in-flight fetch
```

Every Pending registered for a given id is resolved together when the worker
delivers the result.

### Resolution

When the worker delivers `(id, items)`:

```python
def apply_children_results(self):
    while self._children_results:
        id_, items = self._children_results.popleft()
        self._state._children[id_] = items
        self._state._children_pending.discard(id_)
        mark_visible_dirty(self._state)
        for p in self._children_in_flight.pop(id_, []):
            p._resolve()      # fires every callback in p._chain
```

`_resolve()` snapshots-and-clears the chain before iterating so callbacks
that re-register via `then()` (which now fire synchronously since done=True)
don't re-enter the loop.

### Cancellation

`Pending.cancel()` is non-strict: the worker fetch keeps running (results
land in cache for the next visit), but `then` callbacks are dropped. This
saves the complexity of thread-killing — and "stale results in cache" is
actually useful, not harmful.

---

## Render contract

Each pane (list, preview, info, children-grid) renders independently. The
renderer reads from `Browser._needs_redraw: set[str]` to know what to
repaint:

| Flag         | Means                                       |
| ------------ | ------------------------------------------- |
| `'list'`     | The list pane needs repaint                 |
| `'preview'`  | The preview pane needs repaint              |
| `'info'`     | The info bar needs repaint                  |
| `'children'` | The children-grid pane needs repaint        |
| `'all'`      | Tear down all and full-render               |

Action handlers add to this set; the main loop calls
`render_full(self)` (when `'all'` is in the set) or `render_partial(self)`
(otherwise) at the start of each tick.

`render_full` clears the set; `render_partial` clears entries as it draws.

The renderer never writes Browser state (other than `_list_scroll` /
`_preview_scroll` for scroll bookkeeping). It's read-only for everything
else.

### Layout

`layout_panes(cols, rows, *, show_preview, show_children_pane)` returns a
dict of pane geometry (top row, height, columns). The layout is recomputed
every render — cheap, and avoids the "stale geometry after resize" class of
bug.

---

## Test layout

```
test/
  unit/                  fast, pure-Python tests
    test_item.py         to_item, Item dataclass
    test_parsers.py      tsv/csv/json/json-array/ifs/split/match
    test_visible_tree.py visible_items, scope, expanded interactions
    test_scope_stack.py  scope_into / scope_out semantics
    test_insert_resolution.py auto_insert_depth, resolve_insert
    test_pending.py      Pending lifecycle, chain resolution, cancel
    test_actions.py      default keymap, gating, dispatch
    test_cli.py          argparse surface, --action parsing
    …
  async_/                worker / post-queue / threading tests
    test_workers.py      FIFO + latest-wins worker behaviour
    test_post_queue.py   cross-thread → main thread plumbing
    test_chains.py       chain resolution order, late .then()
    test_background.py   browser.watch + refresh from threads
    …
  ui/                    tmux-driven end-to-end tests
    fixtures/
      tmux.py            TmuxFixture (private socket, polling)
      recipes.py         tiny test recipes
    test_navigation.py   nav keys
    test_search.py       search mode
    test_actions.py      custom + builtin actions
    test_async_ui.py     loading placeholder, deferred resolution
    test_resize.py       SIGWINCH
    test_suspend.py      ctrl-z / SIGTSTP / SIGCONT round-trip
    test_snapshots.py    full-screen golden snapshots (~5)
    snapshots/
```

### Running tests

```bash
./run-tests.sh                                 # all tests, serial
./run-tests-parallel.sh                        # same tests, 4 workers
./run-tests-parallel.sh -j 8                   # 8 workers
python3 -m unittest test.unit.test_item        # one module
python3 -m unittest discover -s test/unit      # one layer
python3 -m unittest discover -s test/ui        # tmux only
```

The runner is plain stdlib `unittest`. No pytest, no extra dependencies.
Total runtime is around 18s serial; the parallel runner (`tools/parallel-test.py`,
wrapped by `run-tests-parallel.sh`) fans out per-module subprocesses through a
`ProcessPoolExecutor` and finishes in ~7s with 4 workers — about a 2.5x speedup,
mostly absorbed by the tmux UI suite. Each `TmuxFixture` allocates a unique
socket per instance, so parallel UI tests don't collide. The serial runner stays
the canonical CI entry point; the parallel runner is for fast local iteration.

### Snapshot UI tests

`test/ui/test_snapshots.py` runs ~5 full-screen capture tests against goldens
in `test/ui/snapshots/`. Each test pins `cols`/`rows`, drives a deterministic
launch, captures the stable screen with trailing whitespace stripped, and
diffs against the golden text file.

To regenerate goldens after an intentional rendering change:

```bash
BROWSE_TUI_UPDATE_SNAPSHOTS=1 python3 -m unittest test.ui.test_snapshots -v
git diff test/ui/snapshots/      # inspect before committing
```

The `BROWSE_TUI_UPDATE_SNAPSHOTS=1` env var makes each test rewrite its own
golden in addition to comparing — so a single run regenerates the whole set.

### Tmux fixture

The UI tests use a `TmuxFixture` context manager:

```python
with TmuxFixture() as t:                       # private tmux socket
    t.launch('./browse-tui', '--root-cmd', 'printf "a\\nb\\n"')
    t.wait_for('a')
    t.send('Down')
    t.send('Enter')
    t.wait_for(...)
```

Per-test private sockets (`-L browse-tui-test-…`) avoid cross-test
contamination. Polling-based `wait_for` / `wait_stable` (no fixed sleeps)
keeps tests fast and deterministic.

### Test discipline

- Unit tests should not import `subprocess`, `threading`, or anything that
  spins up a worker. If they do, they belong in `async_/`.
- Async tests use `Browser._headless = True` + `start_workers / stop_workers
  / run_until_idle` for deterministic pumping.
- UI tests assume tmux is on PATH; `test/ui/__init__.py` skips the layer if
  it isn't.

---

## Adding a new feature

A worked walkthrough — add a "copy id to clipboard" action.

1. **Decide which layer.** Built-in keybinding → defaults in
   `070-actions.py`. Recipe-only → no source change; just a recipe.

2. **Implement the handler.** Pure functions of `ctx`. For "copy", you'd
   shell out via `ctx.run_external(['xclip', '-selection', 'clipboard'],
   …)` or write to stdin via `subprocess.Popen`. Add a small wrapper in
   the same file as the existing handlers.

3. **Bind the key.** Add an `Action(key, label, handler, requires)` entry
   to the list returned by `default_actions()` in `070-actions.py`.

4. **Update `_HELP_TEXT`** in `050-render.py` so `?` mentions the new
   binding.

5. **Tests.**

   - Unit test in `test/unit/test_actions.py` covering the handler.
   - If the handler reads from `ctx.cursor`/`ctx.targets`, ensure the
     `requires` gate behaves: try a handler with empty visible list; with
     placeholder cursor; with selection vs cursor.
   - If it spawns a subprocess, mock `subprocess.run` or pass a fake via a
     module-level injection seam (existing handlers use plain
     `subprocess.run`; tests stub it via `unittest.mock.patch`).
   - UI test in `test/ui/test_actions.py` if the binding has visible
     side-effects (a status message; a redrawn pane).

6. **Build + run + tests.**

   ```bash
   ./build-tui.sh
   ./run-tests.sh
   ```

7. **Update docs.** [MANUAL/cli.md](../MANUAL/cli.md) (default keybindings table),
   [MANUAL/api.md](../MANUAL/api.md) (if the action shape or context surface changed),
   [README.md](../README.md) (if it's a top-tier feature worth the
   one-screen budget).

For new flags or input formats, the touch-points are similar but
concentrated in `080-cli.py`:

- New format → add a `parse_<fmt>` function near the existing parsers,
  wire it into `parse_input`, register in `_BARE_INPUT_FORMATS` or
  `_PREFIX_INPUT_FORMATS`, add to `_validate_input_format`'s message.
- New flag → add to `build_argparser`, route through `run_tui` (or a new
  top-level branch in `main`), document in [MANUAL/cli.md](../MANUAL/cli.md).
- New action gate → extend `_gate_passes` in `070-actions.py`, document
  in [MANUAL/api.md](../MANUAL/api.md).

---

## Common gotchas

- **Re-entrancy.** `ctx.pick`, `ctx.menu`, `ctx.confirm`, `ctx.alert`,
  `ctx.input` open modal dialogs (a nested key loop). Only one modal may
  be open at a time: `run_modal` raises `RuntimeError` if one is already
  open, so every modal dialog is non-re-entrant. (`ctx.run_external` and
  `ctx.page` are fine — they suspend the entire terminal.)

- **Thread safety.** Browser methods document themselves as thread-safe
  (everything goes through `post()`). Direct `state._children[…]` writes
  from a background thread are **not** safe and will race the renderer
  with the GIL window.

- **`_notify` keys.** `read_key()` returns the synthetic key `'_notify'`
  on a self-pipe wake. Action handlers and sub-flow loops must drain on
  this key (the main loop does it automatically; the modal loop
  `run_modal` in `055-modal.py` drains `_notify` and channel events
  without rendering while a dialog is open).

- **`ctx.cursor` can be None.** When the visible list is empty, when the
  cursor sits on a placeholder (`⧗ loading…`), or when scoped to an empty
  branch. Always guard or use `ctx.targets` (which falls back gracefully).

- **Item ids must be hashable.** `state.expanded`, `state.selected`, and
  the `_children` cache all key on id. Strings, ints, frozen tuples are
  fine; lists and sets are not.

- **`get_children` runs on a worker thread.** Don't touch the Browser
  state directly. Don't call `time.sleep()` — it blocks the queue. Use
  `browser.watch()` if you need a polling thread.

- **The visible-tree builder shows placeholders.** If you set
  `has_children=True` but `get_children` returns `[]`, the user sees an
  empty expansion (no placeholder, just the parent row). If you set
  `has_children=True` and the fetch is in flight, the user sees `⧗
  loading…` until it resolves.

- **Build first, then test.** Most tests use the test loader (separate
  modules); a few rely on the built `browse-tui` binary (the tmux UI
  layer). If you forget `./build-tui.sh` after touching source, those
  fail with stale-binary mismatches.

---

## See also

- [README.md](../README.md) — quickstart.
- [MANUAL/api.md](../MANUAL/api.md) — public API for recipe authors.
- [MANUAL/cli.md](../MANUAL/cli.md) — full CLI surface.
- [MANUAL/recipes.md](../MANUAL/recipes.md) — shipped recipes + writing-your-own walkthrough.
- [docs/superpowers/specs/2026-04-30-browse-tui-design.md](superpowers/specs/2026-04-30-browse-tui-design.md) — original architectural spec.
- [docs/superpowers/specs/2026-05-08-streaming-push-api-design.md](superpowers/specs/2026-05-08-streaming-push-api-design.md) — streaming / push API design rationale.
