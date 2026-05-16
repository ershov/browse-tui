# browse-tui — Row Visibility (`hidden` flag) Design

**Date:** 2026-05-16
**Status:** Draft (pre-implementation)
**Supersedes nothing.** Adds a per-row visibility flag and a new
`update_data` op (`mod`) for safely mutating existing items.

## Motivation

Recipes today have no first-class way to filter the rendered tree. Two
workarounds exist, both awkward:

1. **Remove + re-insert.** To temporarily hide a row, the recipe
   `remove`s it and later re-`upsert`s it. Cascades drop the subtree
   indexes and the cursor anchor, scroll state, expansion state, and
   per-row preview cache are lost. This is destructive and slow.
2. **Predicate inside `get_children`.** The recipe filters during the
   children callback. This works for one-shot loads but is invisible
   to streamed and patched items; toggling the filter requires a
   full re-fetch.

A persistent per-row visibility flag — owned by the recipe, applied at
render time — makes filtering cheap and reversible. Use cases:

- Toggle "show debug rows" / "show hidden files".
- Per-tag visibility filters in dashboards.
- Per-status filters (e.g., hide closed tickets) in issue browsers.
- Coarse user-driven filters via toggle hotkeys.

Search mode (the existing `/` query) stays orthogonal; it is *additive*
to `hidden`, not interchangeable with it.

## Goals

1. A per-row `hidden` flag, recipe-owned, that defaults to visible.
2. Settable at row creation (kwarg on `upsert` / `set_item`) and
   dynamically (via a new `mod` op).
3. A hidden expandable row renders nothing for its entire subtree;
   the subtree's own per-row flags are remembered, so re-showing the
   parent reveals descendants with their individual visibility states.
4. Cursor moves automatically when the cursor's row becomes hidden —
   "previous visible row in display order", with a defined fallback
   when none exists.
5. A new `mod` op that patches an existing row's fields without
   risking accidental insert. Required for safe dynamic visibility
   toggling under streaming.

## Non-goals

- A predicate-based filter API (e.g., `set_filter(lambda i: ...)`).
  The flag-per-row model is more explicit and composes cleanly with
  patching; a future predicate layer could compute and push flags.
- Replacing search-mode filtering. Search and `hidden` compose
  (Section *Search-mode composition*).
- Hide-state surviving a full `set` replace. `set` constructs a new
  `Item` from the field set; if `hidden` is not included, it reverts
  to the dataclass default (`False`). Recipes that use `set` and want
  to preserve hide-state should include `hidden=<current>` in the
  fields, or use `upsert`/`mod`.
- Hiding framework-synthetic rows (the scope-root row, pending
  placeholder rows). `hidden` lives on `Item`; synthetic rows that
  are not backed by an `Item` are not affected. Hiding a real item
  *parent* of a pending row still hides the pending row, because the
  whole subtree is skipped at render time.
- `asyncio` integration or new concurrency model — same threading
  posture as today.

---

## API

### `Item.hidden`

A new dataclass field:

```python
@dataclass
class Item:
    id: str
    title: str = ''
    has_children: bool = False
    hidden: bool = False         # NEW — default visible
    ...
```

Default `False` means visible. The explicit attribute marks the
exception ("this row is hidden"), which is what's notable.

### Setting `hidden` at creation

Just another kwarg on `upsert` / `set_item`:

```python
ctx.upsert('debug-row', 'parent', title='Debug', hidden=True)
```

Recipes can build batches that interleave normal upserts and hidden
ones, taking advantage of streaming-tolerant out-of-order inserts.

### The `mod` op

A new tuple-op that patches fields on an existing id only — never
inserts. Used for safe dynamic visibility toggling under streaming
(where the recipe doesn't want a "the id is missing, accidentally
create it" surprise).

```python
('mod', id, parent_id, fields)               # no positioning
('mod', id, parent_id, fields, where)        # with positioning
```

#### Helper

```python
def mod(id, parent_id=KEEP_PARENT, *, where=None, **fields):
    if where is None:
        return ('mod', id, parent_id, fields)
    return ('mod', id, parent_id, fields, where)
```

`parent_id` defaults to the `KEEP_PARENT` sentinel — "don't touch the
parent". An explicit value (a real id, or `None` for `None`-rooted
setups) triggers a reparent of the existing row.

#### `KEEP_PARENT` sentinel

A new module-level singleton, exported alongside the op helpers:

```python
# At module load
class _KeepParent:
    """Sentinel passed as ``parent_id`` to ``mod`` to leave parent untouched.

    Reads as ``KEEP_PARENT`` at recipe call sites. Distinct from
    ``None`` (which means "root" for ``state.root_id is None`` setups
    or "explicit None-parent" otherwise) and any string id (real
    parent).
    """
    __slots__ = ()
    def __repr__(self):
        return 'KEEP_PARENT'

KEEP_PARENT = _KeepParent()
```

Self-documenting at the call site:

```python
from browse_tui import mod, KEEP_PARENT
mod('x', hidden=True)                       # patch hidden, leave parent
mod('x', parent_id='new', hidden=False)     # patch + reparent
```

The verbose name (over a plain `KEEP`) is intentional — it makes the
single-purpose nature explicit at the call site, and leaves room for
sibling sentinels later (`KEEP_FIELDS`, etc.) without renaming.

#### No `Context.mod()` wrapper

Unlike `upsert` / `set_item` / `remove`, `mod` does *not* get a
`Context` convenience method. Visibility toggles tend to come in
batches (e.g., "hide all rows with kind=closed"), and exposing only
the batched route encourages recipes to compose them efficiently:

```python
ops = [mod(id, hidden=True) for id in to_hide]
ctx.update_data(ops)         # one batch, one render
```

A future `Context.mod()` can be added if recipes hit a real
one-at-a-time need; for now its absence is a feature.

#### `where=` on `mod`

`mod` never inserts, so the only meaningful positioning behavior is
"move the existing row". Therefore `where` on `mod` *always* implies
reposition (the `"reposition"` flag in the options slot is
unnecessary). This is a small consistency departure from
`upsert`/`set` — call out in docs.

```python
mod('x', where=('first', None))         # if x exists, move to top
mod('x', where=('before', None, 'y'))   # if x exists, place before y
```

If `x` doesn't exist, the whole op is a no-op (consistent with the
"never insert" rule).

#### Op-tuple shape change

`apply_ops` gains one more branch:

| Op                                          | Inserts? | Patches? | Replaces? | Reparents? |
| ------------------------------------------- | -------- | -------- | --------- | ---------- |
| `upsert`                                    | yes      | yes      | no        | yes        |
| `set`                                       | yes      | no       | yes       | yes        |
| `mod` (new)                                 | no       | yes      | no        | yes\*      |
| `remove` / `clear_children` / `complete` / `incomplete` | (unchanged) |          |           |            |

\* only when `parent_id` is not `KEEP_PARENT`.

### Module exports

Added to the public surface:

| Name           | Kind     | Purpose                                |
| -------------- | -------- | -------------------------------------- |
| `mod`          | function | Helper constructor for `mod` op.       |
| `KEEP_PARENT`  | sentinel | "Don't touch parent" marker for `mod`. |

`Item.hidden` is reachable via the existing `Item` re-export.

---

## Semantics

### Render-only subtree cascade

`hidden` is per-row; child flags are independent of ancestor flags.
The cascade is applied at render time only:

- `visible_items(state)` walks the tree top-down. When it encounters
  an item with `hidden=True`, it emits no row for that item and
  *skips recursion into its children*. The children's own
  `hidden` values are preserved in the data model; they simply
  don't render while the ancestor is hidden.
- Re-showing the ancestor (`mod(id, hidden=False)`) reveals
  descendants with their individual `hidden` states intact.

This mirrors expand/collapse: a collapsed parent doesn't rewrite its
children's state, it just stops rendering them.

### Search-mode composition

Search and `hidden` compose with **AND**:

```
visible(item) := not item.hidden
              AND no ancestor of item has hidden=True
              AND (search inactive OR item matches OR item is on the
                   ancestor chain of a match that is itself not hidden)
```

`hidden` is absolute — search never elevates a hidden row. If a
matching descendant has a hidden ancestor, the chain is broken and
the descendant stays invisible during search (because the ancestor's
hide cascade already excludes the subtree at render time). If a
recipe wants matches to override hide, it can flip `hidden=False` on
those rows itself in response to search events.

### Cursor displacement on hide

When the cursor's row becomes hidden (either directly via
`hidden=True` on that row, or via a hidden ancestor), the cursor
must move. The rule is **intentionally separate from the sticky
cursor anchor's fallback chain**:

> Filtering implies that cursor movement is *expected* — the user (or
> recipe) deliberately changed what's visible. The anchor is for
> preserving identity through *structural* mutations the user didn't
> ask for (delete, refresh, reorder).

Rule, applied in `apply_ops`' post-mutation hook when the cursor's
anchored id is in `state._items_by_id` (i.e., the item still exists)
but is no longer in `visible_items`:

1. **Walk back through the pre-mutation visible list.** Starting from
   the cursor's previous row index, scan upward. The first id that is
   still in the post-mutation visible list becomes the new cursor.
2. **If no such id exists** (cursor was on the topmost visible row,
   or every row above it was also hidden) **→ go to the first visible
   row** (new `visible[0]`).
3. **If the new visible list is empty** (everything is hidden) → the
   cursor parks at row 0; no displacement happens until something
   becomes visible.

After displacement, the cursor anchor re-snapshots from the new
cursor (`_reanchor_cursor()`), so the existing fallback chain kicks
in on subsequent *structural* mutations.

#### Distinguishing hide-displacement from anchor displacement

The two paths run on different premises:

| Cursor's id status after `apply_ops`              | Path                                            |
| ------------------------------------------------- | ----------------------------------------------- |
| Present in `visible_items`                        | No displacement.                                |
| Absent from `visible_items`, present in `_items_by_id` (hidden) | **Hide-displacement** (walk back, then first visible). |
| Absent from `_items_by_id` (deleted)              | Existing anchor fallback chain.                 |

#### Bulk hides in one batch

If a single `update_data` batch hides multiple rows including the
cursor's row, the rule still applies: the walk-back is computed
against the post-batch visible list, using the pre-batch row index.
Single decision point, single hook.

### `mod` op semantics

For each `('mod', id, parent_id, fields[, where])` op processed by
`apply_ops`:

1. **Unknown id** → silent no-op. Matches today's
   `upsert(id, None, …)`-against-unknown behavior; preserves streaming
   tolerance.
2. **Known id, `parent_id is KEEP_PARENT`** → patch-merge `fields`
   onto the existing `Item` (matching keys override declared Item
   fields, others land as custom attrs — identical machinery to
   `upsert`'s existing-id branch). No reparent.
3. **Known id, `parent_id is not KEEP_PARENT`** → patch-merge
   `fields` + reparent. If `parent_id == old_parent`, no list shuffle;
   if different, remove from old parent's children list and insert
   into the new parent's children list. `None` means root for
   `state.root_id is None` setups, or an explicit None-parent
   reparent otherwise (same convention as `upsert`).
4. **`where` provided** → resolve insertion index in the resolved
   target parent's children list. Since the row exists by definition,
   reposition semantics apply (same algorithm as
   `upsert`-with-`reposition`). Same-id pivot rule applies as well.
5. **`fields` contains `id`** → `id` is stripped (matches
   `upsert`/`set` behavior; the op's `id` is authoritative).

Validation rules carried over from existing ops:
- Unknown op shape (wrong tuple length, etc.) → `ValueError`.
- Malformed `where` → existing `_validate_where` rules apply.

`mod` flips `state._visible_dirty` whenever the structure changed
(reparented, repositioned, or — critically — `hidden` field flipped).

### Hidden-aware dirtying

A field patch via `upsert` / `set` / `mod` that flips `hidden` from
False to True or vice versa is a structural change. The dispatcher
already marks dirty when `_apply_upsert` returns True; we extend the
return-True conditions to include "the `hidden` field of an existing
row changed value".

This is needed because the visible-items cache is keyed off
`_visible_dirty`; a hide patch must invalidate it.

---

## Validation & error handling

| Condition                                                | Outcome                                             |
| -------------------------------------------------------- | --------------------------------------------------- |
| `mod` with unknown id                                    | Silent no-op. Structure not marked dirty.           |
| `mod` with malformed `where`                             | `ValueError` (existing `_validate_where`).          |
| `mod` with `parent_id` that's neither `KEEP_PARENT`, `None`, nor str | `ValueError("mod parent_id must be a str, None, or KEEP_PARENT")` |
| `hidden` in fields with non-bool value                   | Cast via `bool(...)` (forgiving — same posture as `has_children` today). |

---

## Test plan

Tests live in `test/unit/test_update_data_ops.py` (the existing
positioning suite plus new classes), and in
`test/unit/test_visible_tree.py` for render-time cascade.

### `Item.hidden` field

- Default `hidden=False`.
- `to_item` / `upsert` accept `hidden=True` kwarg.
- Custom-attr passthrough unaffected.

### `mod` op — basic

- Unknown id → silent no-op; state unchanged.
- Known id, `parent_id=KEEP_PARENT`, fields → field patch, parent
  unchanged, position unchanged.
- Known id, `parent_id=new_parent` → reparent + field patch.
- Known id, `parent_id` matches current parent → patch only,
  no list shuffle.
- `mod` with `where` → repositions existing row; no-op if id unknown.
- `mod` flips `_visible_dirty` on structural change.
- `mod(id, hidden=True)` on existing visible row → flips visibility,
  marks dirty, `visible_items` excludes the row.

### `KEEP_PARENT` sentinel

- `from browse_tui import KEEP_PARENT` works.
- `KEEP_PARENT` is not equal to `None`, `'KEEP_PARENT'`, or any str.
- `mod('x')` (default) → `parent_id=KEEP_PARENT` in the op tuple.
- `mod('x', parent_id=KEEP_PARENT)` → same as above.

### Render-only cascade

In `test_visible_tree.py`:

- Hidden leaf → not in `visible_items`.
- Hidden parent with visible children → parent and subtree both
  absent.
- Hidden parent, child also `hidden=True` → parent and subtree
  absent; unhide parent → child remains hidden, grandchildren visible.
- Hidden parent, child `hidden=False` → unhide parent restores child
  too.
- Two hidden siblings, parent visible → siblings absent, parent
  present.
- Search-mode + hidden → AND composition: hidden rows stay hidden
  during search, including when they are matches and when they are
  ancestors of matches.

### Cursor displacement on hide

In `test_browser.py` (the async/integration suite):

- Cursor on row N; row N is hidden via `mod` → cursor lands on what
  was row N-1 (if still visible).
- Cursor on row N; rows N-1 and N hidden together → cursor lands on
  row N-2 (or further back).
- Cursor on row 0; row 0 hidden → cursor lands on new row 0.
- Cursor on a row whose ancestor is hidden → same walk-back.
- All visible rows hidden in one op → cursor parks at row 0
  (no-op until visibility returns).
- Cursor on visible row, another visible row hidden → cursor doesn't
  move.
- Hide + show in same batch (no net cursor displacement) → cursor
  stays put.
- After hide-displacement, the cursor anchor is re-snapshotted (so
  subsequent structural mutations use the new cursor as the primary).

### Streaming / mod-vs-upsert distinction

- `mod` on an id that *will* arrive in a later batch → no-op now; the
  later `upsert` creates the row with default `hidden=False`. This
  is the safety property; recipes should re-issue `mod` after the
  arrival.
- `upsert` on an existing id with `hidden=True` → patches the field
  (existing-id branch); same as `mod` for this case.

### `Context` & module surface

- No `Context.mod()` method exists (regression — guards against
  accidental addition).
- `browse_tui.mod` and `browse_tui.KEEP_PARENT` are importable.
- `browse_tui.__all__` (if defined) includes both.

---

## Open questions

None blocking. Two to revisit after first implementation:

1. **`Context.mod()`.** If recipe authors ask for it, we can add it;
   for now its absence steers them to batched usage.
2. **A predicate-based filter layer** (`ctx.set_filter(...)`)
   stacked on top of `hidden`. Useful if a recipe wants
   "view-defined" filtering (e.g., a UI toggle that derives flags
   from item attrs). Out of scope here; the flag is the primitive
   and recipes can compute it themselves.

---

## Implementation outline (informational)

Roughly:

1. **Data layer** (`src-tui/030-data.py`)
   - Add `hidden: bool = False` field on `Item`.
   - `to_item` accepts the kwarg (free — `**fields` already covers it).

2. **State layer** (`src-tui/040-state.py`)
   - Add `KEEP_PARENT` sentinel class + module-level instance.
   - Add `_apply_mod(state, id_, parent_id, fields, where=None) -> bool`.
     - Unknown id → return False (no dirty).
     - Known id → similar code to `_apply_upsert` existing-id branch,
       but never inserts. `parent_id is KEEP_PARENT` → skip reparent.
   - Extend `apply_ops` dispatch with a `mod` branch.
   - Add `mod(id, parent_id=KEEP_PARENT, *, where=None, **fields)`
     helper.
   - Hide-dirty detection: in `_apply_upsert` / `_apply_set` /
     `_apply_mod`, if the patched `hidden` value differs from the
     pre-patch value, return True (structural).
   - Extend `_compute_visible_items` (or equivalent) to skip subtrees
     rooted at `hidden=True` items.
   - Add post-`apply_ops` cursor-hide hook:
     - Snapshot pre-mutation `visible_items` *before* `apply_ops`
       inside `Browser.update_data` (it's the only public entry).
     - After `apply_ops`, if cursor's id is in `_items_by_id` but not
       in new `visible_items`, run walk-back displacement.
     - After displacement, call `_reanchor_cursor()`.

3. **Module exports** (`src-tui/090-public-api.py` or wherever the
   top-of-file exports live)
   - Re-export `mod` and `KEEP_PARENT`.

4. **Docs** (`docs/api.md`)
   - Add `Item.hidden` to the dataclass field table.
   - Add `mod` row to the `update_data` op table.
   - Add `mod` and `KEEP_PARENT` to helper / module-export tables.
   - Add a Visibility section (mirror this design doc's
     *Semantics*, condensed).

5. **Tests** (per plan above).

### Files unaffected by this change

- Search-mode rendering: the existing search filter wraps
  `visible_items`; once that function skips hidden subtrees, search
  inherits the AND composition for free.
- `expand`/`refresh`/`Pending` machinery — these operate on cached
  children, not visible items.
- The new positioning descriptor (`where`) machinery — composes
  cleanly with `mod` since the same `_resolve_where_index` is reused.

---

## Recipe migration

None required. All existing recipes continue to work; recipes that
want filtering opt in by setting `hidden=True` on rows (via `upsert`
at creation, or via `mod` for dynamic toggling) and re-fetching
nothing else.
