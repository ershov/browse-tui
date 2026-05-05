# browse-tui — Internals (contributor docs)

What you need to know to navigate, modify, and extend the source. Aimed at
contributors, not recipe authors.

For the user-facing surface see [docs/api.md](api.md), [docs/cli.md](cli.md),
[docs/recipes.md](recipes.md). For the original architectural rationale see
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
                    │                     ├── _children_worker (FIFO)
                    │                     └── _preview_worker  (latest-wins)
                    │
                    └── watcher threads ─▶ post queue
                        (recipe-spawned via browser.watch)
```

| Thread             | Purpose                                          | Pattern               |
| ------------------ | ------------------------------------------------ | --------------------- |
| main               | run loop, render, key dispatch, all state mutation | blocking `read_key()` |
| `_children_worker` | call `get_children`, deliver to results FIFO     | FIFO of parent ids    |
| `_preview_worker`  | call `get_preview`, deliver to single-slot       | single-slot, latest-wins |
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

### Invalidation rules

- `cache_invalidate_subtree(state, id)` drops one key from `_children`,
  flips `_visible_dirty`. Used by `ctx.refresh(id)`.
- `cache_invalidate_all(state)` clears `_children`, flips `_visible_dirty`.
  Used by `ctx.refresh()` (no id).
- `mark_visible_dirty(state)` flips `_visible_dirty` only — used after
  `expanded`/`scope_stack`/`selected` mutations.

The next call to `visible_items(state)` rebuilds the visible cache.

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

7. **Update docs.** [docs/cli.md](cli.md) (default keybindings table),
   [docs/api.md](api.md) (if the action shape or context surface changed),
   [README.md](../README.md) (if it's a top-tier feature worth the
   one-screen budget).

For new flags or input formats, the touch-points are similar but
concentrated in `080-cli.py`:

- New format → add a `parse_<fmt>` function near the existing parsers,
  wire it into `parse_input`, register in `_BARE_INPUT_FORMATS` or
  `_PREFIX_INPUT_FORMATS`, add to `_validate_input_format`'s message.
- New flag → add to `build_argparser`, route through `run_tui` (or a new
  top-level branch in `main`), document in [docs/cli.md](cli.md).
- New action gate → extend `_gate_passes` in `070-actions.py`, document
  in [docs/api.md](api.md).

---

## Common gotchas

- **Re-entrancy.** `ctx.pick`, `ctx.input`, `ctx.confirm` block reading
  keys. Calling one inside another's handler is undefined; `ctx.pick` is
  documented as not-re-entrant. (`ctx.run_external` and `ctx.page` are
  fine — they suspend the entire terminal.)

- **Thread safety.** Browser methods document themselves as thread-safe
  (everything goes through `post()`). Direct `state._children[…]` writes
  from a background thread are **not** safe and will race the renderer
  with the GIL window.

- **`_notify` keys.** `read_key()` returns the synthetic key `'_notify'`
  on a self-pipe wake. Action handlers and sub-flow loops must drain on
  this key (the main loop does it automatically; sub-flows like
  `_pick_on_info_bar` do it explicitly).

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
- [docs/api.md](api.md) — public API for recipe authors.
- [docs/cli.md](cli.md) — full CLI surface.
- [docs/recipes.md](recipes.md) — shipped recipes + writing-your-own walkthrough.
- [docs/superpowers/specs/2026-04-30-browse-tui-design.md](superpowers/specs/2026-04-30-browse-tui-design.md) — original architectural spec.
