# browse-tui — Tree-Event Lifecycle Hooks Design

**Date:** 2026-06-01
**Status:** Approved (2026-06-01) — ready for implementation

Extend the lifecycle-hook family to cover the events recipes can
currently observe only by replacing built-in keybindings and copying
framework internals: tree expansion / collapse, asynchronous children
arrival, scope direction, and the live search / filter / resize
inputs. As part of this work the **existing four hooks are made
uniform** with the new payload convention — there is no
backward-compatibility shim; recipes are updated in lockstep.

## Motivation

The framework already ships a coherent family of **state-change
lifecycle hooks**. They share three properties that make them the
right shape for recipe extension:

1. **Source-agnostic** — they fire on the *state transition*, not the
   keystroke, so a recipe reacts identically whether the change came
   from the keyboard, the mouse, a programmatic call, or startup.
2. **Drain-time / debounced** — `_fire_cursor_change_if_pending`
   (`src-tui/040-state.py:4473`) coalesces rapid changes to one fire
   per main-loop tick, diffing the current value against the last one
   fired.
3. **Centralised** — one firing site each (`_fire_*` at
   `src-tui/040-state.py:4501`-`4517`).

The family stops short of the one part of state recipes most want to
react to: **the expanded set.** `_do_expand`
(`src-tui/040-state.py:4887`) mutates `state.expanded`, runs the
internal filter recompute and cursor anchoring, but fires nothing
recipe-facing. Collapse is worse — `_nav_left`
(`src-tui/070-actions.py:205`) and the recursive variants
(`src-tui/070-actions.py:685,726`) `discard` from `state.expanded`
*directly in the actions layer*, never passing through a Browser
method that could fire a hook.

With no state-level hook, recipes are forced one layer too low — to
the **input layer.** To react to an expand they rebind `→`/`l` and
re-implement the default handler, then append their addition. Four
shipped examples, all carrying the cost:

| Recipe | Workaround | Cost |
| ------ | ---------- | ---- |
| `browse-md` `_action_expand` (`recipes/browse-md:1375`) | Overrides `→`; docstring: *"It reproduces both default behaviours"* before adding the lone-heading cascade. | Copies the framework default; only catches keyboard. |
| `browse-md` startup (`recipes/browse-md:1604`) | Runs the **same** cascade again at the auto-expand path; comment: *"Same lone-heading cascade as the Right-arrow override."* | Identical logic duplicated a second time within one recipe — because the key override can't see programmatic expansion. |
| `browse-claude` `_action_tree_right` (`recipes/browse-claude:4815`) | Overrides `→`; uses `ctx.expand(id).then(_land)` to jump to the latest voice once children arrive. | Re-implements the default; the `.then()` only works because *the recipe itself* issued the expand — user-driven expansion is invisible. |
| `browse-claude` `_action_scope_down` (`recipes/browse-claude:4779`) | Calls the internal free-function `scope_into()`, manually re-runs `_recompute_filter_hidden`, pokes `_needs_redraw`, calls the private `_fire_scope_change()`. Comment: *"Mirrors the hook in `Browser.scope_into._do`"*. | Copies framework internals; the copy **drifted and broke** — that is ticket #500. |

The last row is the cautionary tale: even where a hook exists
(`on_scope_change`), the recipe could not compose with it cleanly —
because it could not tell scope-*in* from scope-*out* — fell back to
mirroring internals, and the copy rotted. `on_expand` / `on_collapse`
remove the override-and-duplicate class for the expansion path;
`on_children_loaded` removes the per-call `.then()` blind spot for
user-driven expansion; and adding a **direction** payload to
`on_scope_change` lets `_action_scope_down` be deleted outright.

A secondary group — `on_search_change`, `on_filter_change` — closes
input-state gaps the docs already imply. `docs/api.md` advertises the
pattern `def on_query_change(text): ctx.run_in_slot('preview-search',
…)` in the `run_in_slot` section, but no such hook exists, so
live-search-driven preview is impossible today without overriding
every key in search mode. `on_resize` is a smaller convenience for
width-dependent render caches (e.g. `md2ansi` wrapped to
`preview_width`).

## Goals

1. Add hooks for the tree-structure and input-state transitions the
   family omits, using the **same drain-time-diff mechanism** as
   `on_cursor_change`, so every mutation source is caught with no
   changes to the many mutation sites.
2. **Unify the payload convention across all hooks** — old and new —
   so every hook is `(ctx, <subject>)`. No `(ctx)`-only carve-out, no
   compatibility shim; the existing recipes that would be affected are
   updated in the same change (in practice none register the existing
   four — see *What this retires*).
3. `on_expand` / `on_collapse` retire the `→`/`l` override-and-copy
   pattern and catch programmatic / startup expansion that key
   overrides miss; both carry an **array of ids** so a single
   multi-node action (Alt-Right / Alt-Left, `collapse_all`) delivers
   as one call rather than fanning out.
4. `on_children_loaded` gives recipes a source-agnostic "children for
   these parents are now available" signal — the global counterpart to
   the per-call `Pending.then()`.
5. `on_scope_change` gains a **direction** and the **previous scope
   id** so recipes can act on scope-in vs scope-out without overriding
   keys or tracking the prior scope themselves.
6. `on_search_change` / `on_filter_change` / `on_resize` let recipes
   react to the user's live search / filter edits and to terminal
   resizes.
7. Each hook follows the established contract: optional
   `BrowserConfig` field, fired at most once per drain per logical
   change, exceptions surfaced via `Browser.error` (never crash the
   loop), no-op when unset.

## Non-goals

- A general event bus or pub/sub registry. These are fixed, named
  `BrowserConfig` fields, exactly like the existing four.
- Multiple subscribers per hook. One callable per hook (a recipe or
  plugin that needs fan-out composes callables itself — the plugin
  `on_before_init` wrap pattern already covers this).
- Cancellable / vetoing hooks. Hooks observe transitions that have
  already happened; they cannot prevent an expand or a search.
- Per-yield children streaming callbacks. `on_children_loaded` fires
  once per parent on fetch completion, not per generator `yield`.
- Mouse / raw-key hooks. The input layer stays internal.

---

## API

The hooks, with their uniform signatures. All are optional
`BrowserConfig` fields (mirrored as `Browser(...)` keyword arguments),
`None` by default, documented together in `docs/api.md`.

```python
@dataclass
class BrowserConfig:
    ...
    # existing four — signatures updated to the uniform convention
    on_cursor_change:    Callable | None = None   # (ctx, id) -> None
    on_selection_change: Callable | None = None   # (ctx, ids) -> None
    on_scope_change:     Callable | None = None   # (ctx, scope_id, prev_scope_id, direction) -> None
    on_quit:             Callable | None = None   # (ctx, code) -> None
    # new — Phase 1 (tree structure)
    on_expand:           Callable | None = None   # (ctx, ids) -> None
    on_collapse:         Callable | None = None   # (ctx, ids) -> None
    on_children_loaded:  Callable | None = None   # (ctx, parent_ids) -> None
    # new — Phase 2 (input state)
    on_search_change:    Callable | None = None   # (ctx, query) -> None
    on_filter_change:    Callable | None = None   # (ctx, filters) -> None
    # new — Phase 3 (geometry)
    on_resize:           Callable | None = None   # (ctx, cols, rows) -> None
```

### Uniform payload convention

Every hook receives `ctx` followed by **the subject of the change** —
no hook is `ctx`-only. The subject's shape follows the event:

| Hook | Signature | Subject |
| ---- | --------- | ------- |
| `on_cursor_change` | `(ctx, id)` | cursor id, or `None` (placeholder / empty list) |
| `on_selection_change` | `(ctx, ids)` | current selected ids (list; the resulting set) |
| `on_scope_change` | `(ctx, scope_id, prev_scope_id, direction)` | new + previous scope id (either `None` at root); `direction` is `'in'` or `'out'` |
| `on_quit` | `(ctx, code)` | exit code stashed by `quit()` |
| `on_expand` | `(ctx, ids)` | list of ids newly expanded this drain (≥1) |
| `on_collapse` | `(ctx, ids)` | list of ids newly collapsed this drain (≥1) |
| `on_children_loaded` | `(ctx, parent_ids)` | list of parent ids whose fetch settled this drain (≥1) |
| `on_search_change` | `(ctx, query)` | new effective query string |
| `on_filter_change` | `(ctx, filters)` | active filter tuple |
| `on_resize` | `(ctx, cols, rows)` | new terminal dimensions |

Rationale: a single value is forced for the new tree hooks anyway
(the expanded/loaded id is not derivable from `ctx`), so rather than
leave the old four as a `(ctx)`-only special case we pass each one's
subject too. **Set / burst events pass a list** (expand, collapse,
children-loaded, selection); single-subject events pass the value
directly. Recipes can still read the same data off `ctx`
(`ctx.cursor`, `ctx.selected`, `ctx.filters`, …) — the payload is the
convenient, uniform path.

### Phase 1 — tree structure

#### `on_expand(ctx, ids) -> None`

Fires when one or more ids newly enter `state.expanded` — the
collapse→expand transition. `ids` is the list of nodes expanded in
this drain: length 1 for a single `→`, longer for Alt-Right
(`_expand_recursive`) or a programmatic `expand_subtree`. The ids are
not necessarily related to `ctx.cursor` (a programmatic
`ctx.expand(other)` fires for `other`).

Fires for **every** expansion source: `→`/`l`, `ctx.expand()` /
`browser.expand()`, `expand_subtree`, the recursive expand action,
and startup auto-expands issued before `run()`.

At fire time a node's children **may or may not be cached** — an
expand of an uncached node kicks an async fetch. Per id, read
`ctx.cached_children(id)`: `None` means a fetch is in flight (react in
`on_children_loaded`); a list means children are available now. See
*Cached vs. uncached expansion* and the composition pattern under
Semantics.

Re-pressing `→` on an already-expanded node does **not** fire
`on_expand` (the expanded set did not change) — that is a navigation
gesture, not a state change.

```python
# browse-md: auto-expand any file whose only child is a heading.
def on_expand(ctx, ids):
    for id in ids:
        cascade_id = _lone_heading_child_id(id)   # reads the pre-built tree
        if cascade_id is not None:
            ctx.expand(cascade_id)
```

This single handler replaces both the `→` override
(`recipes/browse-md:1375`) *and* the duplicated startup cascade
(`recipes/browse-md:1604`): the startup `b.expand(auto_expand_id)`
fires `on_expand` like any other expansion, so the cascade lives in
exactly one place.

#### `on_collapse(ctx, ids) -> None`

Fires when one or more ids newly leave `state.expanded`. Symmetric
with `on_expand`. `ids` batches a burst: `←`/`h` (length 1), the
recursive collapse action, or `collapse_all` (the whole expanded set
in one call). Source-agnostic.

```python
def on_collapse(ctx, ids):
    log.debug('collapsed %s', ids)
```

#### `on_children_loaded(ctx, parent_ids) -> None`

Fires when one or more `get_children` fetches **settle** — the moment
`state._loading[parent_id]` transitions `True → False`
(`src-tui/040-state.py:4586` worker delivery; the `complete` op at
`:1297`). `parent_ids` batches every parent that settled in this
drain (length 1 for a lone expand; longer when a full refresh
refetches several expanded parents at once). Per id,
`ctx.cached_children(parent_id)` returns the full children list
(possibly `[]`).

This is the source-agnostic, global counterpart to
`ctx.expand(id).then(cb)`: `.then()` only fires for an expand *the
recipe itself* issued, whereas `on_children_loaded` fires for
user-driven expansion, `ctx.refresh(id)` refetches, generator
completion, and prefetches alike.

```python
# browse-claude: jump to the latest voice once children are in.
def on_children_loaded(ctx, parent_ids):
    for pid in parent_ids:
        latest = _latest_voice_among_children(pid)
        if latest and latest != pid:
            ctx.cursor_to(latest)
```

Firing rules:

- **Settles via fetch only.** Expanding a node whose children are
  *already cached* does **not** fire `on_children_loaded` (no fetch
  ran). Use `on_expand` + `ctx.cached_children` for the cached case —
  see the composition pattern below.
- **Empty result fires.** `get_children` returning `[]` settles
  loading → fires with `cached_children == []`.
- **`None` result does not fire.** Returning `None` leaves loading
  `True`; the hook fires only if and when the recipe later clears
  loading (e.g. a `complete` op).
- **Errors fire.** A `get_children` exception is caught at the worker
  boundary, children become `[]`, loading clears → the hook fires
  with an empty list.
- **Refresh refires.** `ctx.refresh(parent)` invalidates then
  refetches → fires again on the new completion.

### Phase 2 — input state

#### `on_search_change(ctx, query) -> None`

Fires when the effective search query changes — live, per keystroke in
`SEARCH_EDIT` mode, and on programmatic `set_search_query` /
`clear_search`. `query` is the new query string (also `ctx.search_query`).
Debounced to one fire per drain on the final value; clearing to `''`
fires once.

```python
def on_search_change(ctx, query):
    ctx.run_in_slot('preview-search', lambda tok: _rebuild_preview(query, tok))
```

#### `on_filter_change(ctx, filters) -> None`

Fires when the committed-plus-live filter list changes (`&` typing,
commit, clear, `set_filters` / `add_filter` / `clear_filters`).
`filters` is the `tuple[str, ...]` also returned by `ctx.filters`.
Debounced per drain.

### Phase 3 — geometry

#### `on_resize(ctx, cols, rows) -> None`

Fires when the terminal dimensions change (SIGWINCH path; the
`g_resize_flag` checked at `src-tui/040-state.py:5982`). `cols` /
`rows` are the new dimensions from `term_size()`. Lets recipes drop
width-dependent caches up front rather than lazily on the next
`get_preview`.

```python
def on_resize(ctx, cols, rows):
    _md_cache.clear()                 # md2ansi wrap width changed
    ctx.invalidate_preview(ctx.preview_item_id)
```

---

## Semantics

### Firing mechanism: drain-time diff

Every hook reuses the `on_cursor_change` machinery — a
`_fire_<name>_if_pending` method invoked from the main-loop drain next
to `_fire_cursor_change_if_pending` (`src-tui/040-state.py:4473`,
called at the `:5976` drain point). Each compares current state to a
`_last_*` snapshot held on the Browser and fires only on a real delta.
Two consequences match the existing contract:

- **Coalescing / batching.** Several mutations between drains produce
  at most one fire per drain. For the set-valued hooks this means the
  whole burst is delivered in one list: the `expanded` diff yields
  `added = expanded - last` and `removed = last - expanded`;
  `on_expand` fires once with `list(added)`, `on_collapse` once with
  `list(removed)`, then `last` is re-snapshotted. A drain that nets to
  no change fires neither. (List order is unspecified; recipes that
  need a stable order should sort.)
- **No mutation-site surgery.** Because the diff runs at drain time,
  the many places that mutate `state.expanded` — including the direct
  `state.expanded.discard(...)` in the actions layer — are caught
  automatically. This is the explicit fix for the #500 failure mode:
  no recipe and no framework call site has to remember to fire
  anything.

`on_children_loaded` is collected, not diffed: loading-clear sites add
the parent id to a `_children_loaded_pending` set, drained once per
tick into one `parent_ids` list. Search / filter / resize diff their
scalar / tuple value against `_last_*`.

### Cached vs. uncached expansion (the composition pattern)

`on_expand` and `on_children_loaded` are orthogonal events, and a
recipe that wants to "act once an expanded node's children are
visible" composes them. This is the one pattern worth documenting
explicitly, because it is more code than the `.then()` it replaces —
but it works for **user-driven** expansion, which `.then()` cannot
observe at all, and it correctly excludes refresh / prefetch arrivals:

```python
_awaiting = set()

def on_expand(ctx, ids):
    for id in ids:
        if ctx.cached_children(id) is not None:
            _react(ctx, id)            # children already cached: act now
        else:
            _awaiting.add(id)          # fetch in flight: defer

def on_children_loaded(ctx, parent_ids):
    for pid in parent_ids:
        if pid in _awaiting:
            _awaiting.discard(pid)
            _react(ctx, pid)
```

`browse-claude`'s `_action_tree_right` collapses to this and — unlike
the current override — also fires when the user expands with the
keyboard. (See Open questions for the considered single-hook
alternative and why it is *not* the recommendation.)

### Scope transitions do not fire expand/collapse

`scope_into` / `scope_out` save and restore per-scope expanded sets via
`_expanded_by_scope` (`src-tui/040-state.py:393,400,423,424`). A naive
diff would read every restored id as a fresh expand/collapse. That is
wrong — a scope transition is an `on_scope_change` event, not a burst
of expansions.

**Rule:** immediately after a scope transition restores
`state.expanded`, the framework re-baselines `_last_expanded =
set(state.expanded)` so the next diff sees no delta. `on_scope_change`
fires for the transition (with its direction); `on_expand` /
`on_collapse` stay silent. The re-baseline lives in
`Browser.scope_into` / `scope_out` (`:4138` / `:4176`), the methods
that already call `_fire_scope_change`.

### Refresh

A full `ctx.refresh()` (Ctrl-R) preserves `state.expanded` (expansion
is id-keyed; only the children caches are wiped), so **no**
expand/collapse fires. It does re-fetch every expanded parent, so
`on_children_loaded` fires with all of them batched as each refetch
settles — the correct "children reloaded" signal.

### Ordering within a drain

When several hooks are pending in one drain they fire in a fixed
order after `apply_ops` / cursor settling: `on_resize` →
`on_search_change` → `on_filter_change` → `on_collapse` → `on_expand`
→ `on_children_loaded` → `on_scope_change` → `on_cursor_change` →
`on_selection_change`. Rationale: geometry and input-state first (they
may reshape the visible tree), then structure, then children arrival,
then cursor/selection settling last so a cursor-change handler sees
the post-expansion tree. The exact order is documented and
test-pinned; recipes should not depend on cross-hook ordering beyond
what is specified.

### Error handling

Identical to the existing hooks: each `_fire_*` wraps the call in
`try/except` and routes exceptions to `Browser.error(f'on_<name>:
{type(e).__name__}: {e}')` (`src-tui/040-state.py:4496`-`4499`). A
throwing hook surfaces a red info-bar message and never crashes the
loop. (`on_quit` remains the one swallow-silently exception — a
failing cleanup must not block exit.)

### Headless / test pumping

The `_headless` pump (`start_workers` / `run_until_idle` per
`docs/internals.md`) drains the post queue, so the new
`_fire_*_if_pending` calls run under the deterministic test harness
exactly as `on_cursor_change` does today. No new test scaffolding.

---

## What this retires

Concrete deletions enabled in the shipped recipes (performed as the
follow-up migration once the hooks land):

- **`browse-md`**: delete `_action_expand` (`recipes/browse-md:1375`)
  and its `→` rebinding; move the lone-heading cascade into
  `on_expand`. The startup duplicate (`recipes/browse-md:1604`)
  collapses to the same handler firing on the startup `b.expand(...)`.
  Two copies → one.
- **`browse-claude`**: delete the expand half of `_action_tree_right`
  (`recipes/browse-claude:4815`) and its `right` rebinding; the
  jump-to-latest-voice moves to `on_children_loaded` (+ `on_expand`
  for the cached case). The hand-rolled `_chain_expand_then_cursor`
  `.then()` chains used for *user-driven* expansion are no longer
  needed. (Recipe-initiated deep-link chains may keep `.then()` where
  per-call sequencing reads better.)
- **`browse-claude`**: delete `_action_scope_down`
  (`recipes/browse-claude:4779`) and its `alt-down` rebinding
  entirely. Its cross-file preview invalidation moves to
  `on_scope_change`, gated on `direction == 'in'` — the framework's
  own `scope_into` now does the cursor reset / filter recompute /
  redraw / `_fire_scope_change` the override had been mirroring (the
  #500 source).
- The **#500 class** of bug — recipe copies of framework internals
  drifting out of sync — is structurally prevented: recipes layer
  behavior on hooks instead of mirroring `_do_expand` / `_nav_left` /
  `scope_into`.

**Existing-hook uniformity is framework + docs only.** No shipped
recipe registers `on_cursor_change` / `on_scope_change` /
`on_selection_change` / `on_quit` today (verified across `recipes/`),
so changing their signatures to the uniform convention breaks no
recipe code — it only updates the framework firing sites and
`docs/api.md`.

---

## Test plan

New unit coverage in `test/unit/test_lifecycle_hooks.py` (extend the
existing file); async coverage in `test/async_/` for the
worker-delivery timing of `on_children_loaded`.

### Uniform payloads (existing four)

- `on_cursor_change` receives `(ctx, id)`; `id is None` on a
  placeholder / empty list.
- `on_selection_change` receives `(ctx, ids)` with the resulting
  selected id list.
- `on_scope_change` receives `(ctx, scope_id, prev_scope_id, direction)`:
  `scope_into` → `direction == 'in'`, `prev_scope_id` is the scope left;
  `scope_out` → `'out'`; scoping out to root → `scope_id is None`; the
  initial scope-in from root → `prev_scope_id is None`.
- `on_quit` receives `(ctx, code)` with the stashed exit code.

### `on_expand` / `on_collapse`

- Keyboard `→` on a collapsed expandable row fires `on_expand([id])`
  once; `←` fires `on_collapse([id])` once.
- `ctx.expand(other)` (not the cursor) fires `on_expand([other])`.
- Re-pressing `→` on an already-expanded row fires **nothing**.
- Alt-Right (`_expand_recursive`) / `expand_subtree` fire **one**
  `on_expand(ids)` with every newly-added id; Alt-Left and
  `collapse_all` fire **one** `on_collapse(ids)` with every removed
  id.
- A drain that both adds and removes ids fires `on_expand(added)` and
  `on_collapse(removed)`.
- Rapid expand+collapse of the same id within one drain nets to the
  correct result (no fire if it returns to baseline).
- Startup `b.expand(x)` before `run()` fires `on_expand([x])` on the
  first drain.
- Missing (`None`) handlers are silent no-ops; a throwing handler
  surfaces via `Browser.error` and the loop survives.

### Scope re-baseline

- `scope_into(child)` whose target scope has a restored expanded set
  fires `on_scope_change(.., 'in')` and **no** `on_expand` /
  `on_collapse`.
- `scope_out` likewise fires only `on_scope_change(.., 'out')`.
- After a scope transition, the next genuine expand fires `on_expand`
  normally (baseline correctly re-anchored).

### `on_children_loaded`

- Expanding an uncached parent fires `on_children_loaded([parent])`
  after the worker delivers, with `cached_children(parent)` populated.
- Expanding an **already-cached** parent fires `on_expand` but **not**
  `on_children_loaded`.
- `get_children` returning `[]` fires with empty children; returning
  `None` does **not** fire until loading is later cleared.
- A generator `get_children` fires once at completion, not per yield.
- A `get_children` raising fires once with `cached_children == []`.
- `ctx.refresh()` over several expanded parents fires one
  `on_children_loaded(parent_ids)` batch per drain as fetches settle.
- Collapsing a node before its in-flight fetch delivers: `on_collapse`
  fires; `on_children_loaded` still fires when the result lands (the
  fetch did settle) — pinned as documented.

### `on_search_change` / `on_filter_change`

- Typing in search fires `on_search_change` per drain with the live
  query; multiple keystrokes in one drain coalesce to the final value;
  `clear_search` fires once with `''`.
- `set_filters(['a','b'])` fires `on_filter_change(('a','b'))`; an
  identical re-set fires nothing; `add_filter('')` is a no-op.

### `on_resize` and ordering

- A simulated SIGWINCH with changed `term_size` fires
  `on_resize(cols, rows)` once; unchanged size fires nothing.
- A single drain that resizes, changes search, expands an uncached
  node, and moves the cursor fires the hooks in the documented order.

---

## Decisions from the 2026-06-01 review

1. **No backward compatibility.** All hooks adopt the uniform
   `(ctx, <subject>)` convention; the existing four are migrated in
   the same change. (Closed.)
2. **Separate `on_expand` / `on_collapse`, array-of-ids payload.**
   Bursts (Alt-Right/Left, `collapse_all`, `expand_subtree`) deliver
   as a single call with the id list — the drain-time diff batches
   them naturally, so there is no per-id fan-out. (Closed — Open
   question 1.)
3. **Payload convention unified** across all hooks (Open question 3).
   (Closed.)
4. **Phase shaping deferred to planning** (Open question 4). (Closed.)
5. **`on_scope_change` direction in scope** (Open question 5): adds
   `direction` and retires `_action_scope_down`. (Closed.)
6. **`on_scope_change` also carries the previous scope id**
   (`(ctx, scope_id, prev_scope_id, direction)`): cheap to capture
   before the transition, saves recipes tracking it across calls.
   (Closed.)
7. **`on_expand` stays immediate and batched — not fused with
   children-readiness.** Recommendation accepted in review; rationale
   retained below as the design record. (Closed.)

## Design rationale: immediate vs. children-ready `on_expand`

Raised in review and **resolved: keep `on_expand` immediate and
batched** (Decision 7). The analysis is retained here as the design
record.

**The alternative.** Define `on_expand` with `Pending.then()` timing:
fire immediately for an already-cached node, fire after delivery for an
uncached one, so the handler is *guaranteed* children at fire time.
This removes the cached/uncached split and the composition pattern.

**Is it generally useful?** Yes — it serves a family, not one recipe:

- *Drill-to-child* — move cursor/selection to a specific child after
  expanding (browse-claude's jump-to-latest-voice).
- *Cascade-expand* — expand a distinguished child (browse-md's
  lone-heading; today it only works because the tree is synchronous,
  so children are always cached — an async source wanting the same
  cascade would need exactly this timing).
- *Rollup badge* — when a parent expands and children load, compute a
  count / size / status summary and patch the parent's tag.
- *Child decoration* — recolor / hide / sort children based on parent
  context as they appear under an expansion.

Its **unique** benefit over the separate hooks: it collapses the
cached-vs-uncached split into one handler with children guaranteed
present, *and* it is expansion-scoped — it does not fire on a plain
`refresh` / prefetch of an already-expanded node (which
`on_children_loaded` does), so a "jump to child" never yanks the
cursor on a background refresh.

**Why it is *not* the recommendation.** It conflicts with the
array-payload decision (#2 above) and adds a wart:

1. **Loses clean batching.** Post-children timing must fire
   per-settlement for uncached nodes (they arrive across different
   drains), so an Alt-Right burst can no longer be delivered as one
   `on_expand(ids)` call — it fragments. The immediate state-diff
   model delivers the whole burst at once. You cannot have both clean
   batching *and* children-guaranteed timing in a single hook.
2. **Asymmetric timing** vs `on_collapse` (which has no children to
   wait for). Defensible but a wart.
3. **Removes the pure "toggled" signal** (pre-children). No current
   recipe needs it, but it is a capability dropped.

The unique benefit is reconstructable with the documented composition
(`on_expand` stashes intent, `on_children_loaded` fulfils, recipe
checks "still expanded") — about six lines, fully flexible, and it
keeps both events clean and batched.

**Resolution.** Keep `on_expand` / `on_collapse` immediate,
symmetric, and batched (array payload), plus `on_children_loaded`;
ship the composition pattern in the docs. If, after migrating the
recipes, the composition proves to be copy-pasted everywhere, promote
it then — either as a fused-timing hook or, better, a small framework
helper (e.g. `ctx.when_expanded_ready(id, cb)` that internally tracks
the same pending set) — rather than baking the timing into `on_expand`
preemptively.

---

## Implementation outline (informational)

1. **`BrowserConfig` fields** (`src-tui/040-state.py`, dataclass near
   `:2595`) — add the five new fields; the existing four stay but their
   stored handlers are now called with payloads. Mirror in the
   `Browser(...)` keyword surface; store as `self._on_*` (near `:2868`).

2. **State snapshots** (`Browser.__init__`) — `self._last_expanded =
   set()`, `_last_search_query = ''`, `_last_filters = ()`,
   `_last_size = None`, `self._children_loaded_pending = set()`. Seed
   `_last_expanded` empty so pre-`run()` / `initial_scope` expansions
   fire on the first drain (per the "startup" test).

3. **Fire methods** (`src-tui/040-state.py`, beside `:4473`-`4517`) —
   `_fire_expand_collapse_if_pending` (set diff → one `on_expand(list)`
   and/or one `on_collapse(list)`, then re-snapshot),
   `_fire_children_loaded_if_pending` (drain `_children_loaded_pending`
   → one `on_children_loaded(list)`), `_fire_search_change_if_pending`,
   `_fire_filter_change_if_pending`, `_fire_resize_if_pending`. Update
   the existing `_fire_cursor_change_if_pending` /
   `_fire_selection_change` / `_fire_scope_change` / `_fire_on_quit` to
   pass payloads. Each guards on `self._on_* is None` and wraps in
   `try/except → self.error(...)`.

4. **Loading-clear instrumentation** (`src-tui/040-state.py:1297`,
   `:4586`) — at each `True → False` transition of `_loading[pid]`,
   add `pid` to `_children_loaded_pending`. Prefer a single
   `_set_loading(pid, value)` helper so new sites can't forget.

5. **Drain wiring** (`src-tui/040-state.py`, the `:5976` block) — call
   the `_fire_*_if_pending` methods in the documented order.

6. **Scope direction + prev id + re-baseline** (`Browser.scope_into`
   `:4138`, `scope_out` `:4176`) — capture `current_scope(state)`
   *before* the transition; pass `(scope_id, prev_scope_id, direction)`
   to `_fire_scope_change` (`'in'` / `'out'`); after the transition
   work, set `self._last_expanded = set(self._state.expanded)`.

7. **Resize hook** (`src-tui/040-state.py:5982`/`:6012`) — when
   `g_resize_flag` is observed, compare `term_size()` to `_last_size`;
   on change stage an `on_resize` fire and update `_last_size`.

8. **Docs** (`docs/api.md`) — replace the *Lifecycle hooks* table with
   the uniform-payload version (all ten hooks), add the cached-vs-
   uncached composition pattern, the scope-re-baseline rule, and the
   direction payload.

9. **Tests** — extend `test/unit/test_lifecycle_hooks.py`; add an
   `on_children_loaded` timing test under `test/async_/`.

10. **Recipe migration** (follow-up) — convert `browse-md`
    `_action_expand` → `on_expand`; `browse-claude` `_action_tree_right`
    → `on_expand` + `on_children_loaded`; `browse-claude`
    `_action_scope_down` → `on_scope_change(direction='in')`. Delete the
    duplicated cascade / `.then()` chains / mirrored `scope_into` listed
    under *What this retires*.
