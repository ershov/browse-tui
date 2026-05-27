# browse-tui — Scope-Root Unification & `Item.synthetic` Design

**Date:** 2026-05-27
**Status:** Draft (pre-implementation)
**Supersedes nothing.** Refines the `VisibleEntry.kind` enum and the
contract around framework-fabricated Items used as scope-stack
placeholders.

## Motivation

Today the rendered list has four row kinds: `normal`, `scope_root`,
`pending`, `insert_marker`. Three of those four are framework-synthesized
in different ways. `scope_root` is the leaky one — it sometimes wraps a
real Item (the common alt-down navigation path) and sometimes wraps an
Item fabricated by `visible_items` because no real one exists yet (recipe
pre-pushed `initial_scope`, recipe-injected deep `scope_stack`, lazy-
fetched alt-up into an uncached ancestor, post-`refresh` before re-fetch).

The leak shows up as:

1. **A latent reconciliation bug.** Fix `e962b20` registered the
   fabricated stub in `_items_by_id` so preview routing has a target,
   but nothing promotes the stub to a real Item when the parent's
   children fetch later arrives. The stub keeps the synthetic
   `title = str(id)` forever; recipe-supplied fields never land.
2. **Tangled discriminators.** `kind='scope_root'` drives ~10 distinct
   behaviors at the render and action layers (no selection, no expand
   glyph, no tag, bold, `scope_title` override, search-exclude, Ctrl-A-
   exclude, insertion-skip, nav-arrow no-op, dedicated render branch).
   "What is this row?" and "where did this Item come from?" got
   collapsed into one tagged enum value.

This design splits those concerns:

- **Row role** — what kind of row is this? After this change: `normal`,
  `pending`, `insert_marker`. The scope-row stops being a separate role.
- **Item provenance** — was this Item synthesized by the framework as a
  placeholder, or does it carry real data? New flag `Item.synthetic`
  with explicit promotion-on-fetch semantics.

The scope row becomes a normal row at depth 0 of a scoped view. It
participates in selection, search, expand/collapse, tag rendering, and
nav arrows the same way every other row does. One narrow exception
remains: when an Item *is* the current scope, the renderer prefers
`Item.scope_title` (if set) over `Item.title` — the "no parent context
above me, so carry more situating information in the label" rule.

## Goals

1. Add `Item.synthetic: bool = False`. When `visible_items` fabricates
   a placeholder Item for an entry not in `_items_by_id`, it sets the
   flag.
2. Add a `_promote_synthetic` helper invoked from
   `apply_children_results` (the children-fetch delivery path). When a
   delivered child's id matches an existing synthetic in
   `_items_by_id`, the helper copies the real Item's data onto the
   synthetic in place, preserves cached preview text if the real Item
   lacks it, clears the flag, and substitutes the (now-promoted)
   synthetic into the delivered children list at the position the
   incoming Item held.
3. Drop `kind='scope_root'` from `VisibleEntry`. The scope row is
   emitted with `kind='normal'`, depth 0, and gets auto-added to
   `state.expanded` by `scope_into` so it paints expanded by default.
4. Narrow `Item.scope_title` to a renderer-only label override that
   applies only when the row's item is the current scope (i.e.,
   `item.id == current_scope(state)`). No positional check
   (`depth == 0`), no scope-stack peek inside the segment builder —
   the row-emit loop computes the boolean once and passes it in.
5. Reject insertion at depth 0 when scoped (the one operation-level
   carve-out).

## Non-goals

- Bold rendering for the scope row. The visual distinction is dropped;
  if it turns out to feel unclear in practice, a future change can add
  it via an `Item`-attribute mechanism (e.g., extending `tag_style` to
  drive title styling, or a new `Item.title_style` field). Out of scope
  here.
- Touching the `update_data` apply path. `_apply_upsert` already
  mutates existing Items in place; an `update_data`-delivered patch
  that happens to share an id with a synthetic stub will patch fields
  but won't clear `synthetic=False`. Recipes that push real data via
  `update_data` rather than through `get_children` can flip the flag
  themselves if they care; the common case (worker-delivered children)
  is handled by `_promote_synthetic`.
- Identity-replacement (constructing a new Item and swapping it).
  Mutate in place — preserves references in `_items_by_id`, the per-
  Item preview cache, and any anchors. No callers do `is`-comparison
  on Items today, but the in-place mutation policy avoids creating
  that hazard going forward.
- A predicate-based discriminator on `(depth, state.scope_stack)`. The
  current-scope check uses `item.id == current_scope(state)` — a
  property of the Item's relationship to state, not of its position
  in the visible list.

## API

### `Item.synthetic` (new field)

```python
@dataclass
class Item:
    ...
    synthetic: bool = field(default=False, repr=False, compare=False)
```

- Set to `True` by `visible_items` when it fabricates a stub for a
  scope-root id not findable in `_children`.
- Cleared to `False` by `_promote_synthetic` when a real Item with the
  same id arrives via the children-fetch path.
- Recipes do not normally read or set the flag. It is descriptive of
  framework-internal Item lifecycle.
- Excluded from `__eq__` / `__hash__` / `repr` (no behavioral impact
  on equality; not part of the Item's identity).

### `_promote_synthetic(synthetic, real)` (new helper)

Copies data fields from `real` onto `synthetic` in place:

- **Item data fields** (`title`, `tag`, `tag_style`, `has_children`,
  `hidden`, `scope_title`): always taken from `real`.
- **Recipe extras** (anything in `real.__dict__` that isn't a dataclass
  field): copied from `real` to `synthetic` via `setattr`. Extras
  present on `synthetic` but not on `real` are not removed (a synthetic
  rarely has recipe extras, but if it somehow did, conservative-merge
  preserves them).
- **Framework cache slots** (`preview`, `preview_render`): if `real`
  carries them, take real's; otherwise preserve synthetic's. The common
  case is `real.preview is None` (preview is filled lazily on first
  paint), so the synthetic's cached preview survives the promotion.
- **`synthetic` flag**: set to `False`.

The function does not touch `_items_by_id` / `_parent_of_id` / any
state-level indexes. Index maintenance stays with `_index_add_children`,
which is called after promotion has substituted the synthetic into the
fetched list.

### `_promote_synthetics(state, items)` (new helper)

Walks the delivered children list. For each incoming child whose id
matches an existing `synthetic=True` entry in `_items_by_id`, calls
`_promote_synthetic` and substitutes the promoted synthetic at the
incoming child's position. Returns a new list (same length, some entries
replaced with promoted synthetics).

Called from `apply_children_results` between `_index_drop_children` and
`self._state._children[id_] = items`. Order matters: the drop happens
first so any stale `_items_by_id` entries from previous siblings are
gone; the synthetic survives the drop because its `_parent_of_id` entry
isn't `parent_id` (synthetics aren't a child of anything yet —
`visible_items` doesn't touch `_parent_of_id` when fabricating).

### `VisibleEntry.kind` (narrowed enum)

Before: `'normal' | 'scope_root' | 'pending' | 'insert_marker'`
After: `'normal' | 'pending' | 'insert_marker'`

The scope row is emitted as `kind='normal'` at `depth=0` when
`scope_stack` is non-empty.

### `Item.scope_title` (narrowed semantics)

No field change. Behavior change: the renderer applies `scope_title`
only when the row being rendered is the current scope. Determined by
the row-emit loop (the only caller that knows the scope state) and
passed into the segment builder as an `is_current_scope: bool`
parameter. The segment builder does:

```python
label = item.scope_title if (is_current_scope and item.scope_title) else item.title
```

### `scope_into` (auto-expand on enter)

`scope_into(state, id_)` calls `state.expanded.add(id_)` after pushing
onto `scope_stack`. The scope row paints with the expanded glyph (▼) on
the very first frame after a scope-down. The user can collapse with
Left like any other expanded row; the visible list shrinks to one row
(the scope row only). Right re-expands.

This change is necessary because the new behavior treats the scope row
as a normal row: without auto-expand, the scope row would paint with
the collapsed glyph (▶) and its children would not appear until the
user pressed Right, which is jarring.

### Insertion guard

`resolve_insert` rejects `pos=0` (i.e., before the scope row) when
`state.scope_stack` is non-empty. There is no row to insert *next to* at
that position — it would land outside the scope.

## Two-stage rollout

Both stages are landed as separate commits.

### Stage 1 — `Item.synthetic` + `_promote_synthetic`

No UI change. The scope row still renders as `kind='scope_root'` with
all its current behavior; the only change is that the fabricated stub
now carries `synthetic=True` and gets promoted to a real Item when the
parent's children fetch arrives. This fixes the latent
"stub-never-replaced" bug independently of the kind change.

Test coverage:

- A fabricated stub is created with `synthetic=True`.
- A delivered child with a matching id replaces the stub via in-place
  mutation: `_items_by_id[id]` is the *same object* before and after
  promotion (identity-stable).
- Recipe-attached extras on the delivered child appear on the promoted
  synthetic.
- A cached `preview` on the synthetic is preserved across promotion
  when the delivered Item has `preview=None`.
- A delivered Item with non-None `preview` overrides.

### Stage 2 — Kind collapse + `scope_title` narrowing + auto-expand + insertion guard

UI-visible change. Scope row renders with selection marker, expand
glyph, tag, normal weight; participates in Ctrl-A, search, nav arrows.
`scope_title` applies only via the `is_current_scope` predicate in the
renderer.

Test coverage:

- `visible_items` emits the scope row as `kind='normal'`.
- The renderer prefers `scope_title` for the current-scope row;
  uses `title` for the same Item when it appears as a child elsewhere
  (no concurrent dual-rendering in practice, but the predicate is
  Item-identity-independent).
- Ctrl-A selects the scope row.
- Search can land on the scope row.
- `scope_into(id_)` adds `id_` to `state.expanded`.
- Insertion at `pos=0` in a scoped view is rejected.
- Browse-claude smoke: opening a session directly via CLI still shows
  the full path in the scope header (via `scope_title`); after alt-up
  + alt-down the same row still shows the full path.

Risk note: this stage will surface tag-on-scope-row visually (e.g.,
browse-claude's session status tag is now visible in the scope header).
That's intended — it's part of the uniformity — but worth eyeballing
once before committing.

## Compatibility

- `VisibleEntry.kind` removing `'scope_root'` is a breaking change for
  any external consumer that pattern-matches on the value. No recipe
  in the tree does (verified via grep — all recipe sites do
  `item, _kind = vis[idx]` and discard the kind). Internal callers in
  `070-actions.py` and `050-render.py` are migrated as part of stage 2.
- `Item.synthetic` is new; defaults to `False`. Existing recipes are
  unaffected.
- `Item.scope_title` field is unchanged. Behavior change is renderer-
  only — the same field on the same Item carries the same intent
  (richer label when shown as the scope root). Recipes don't need to
  do anything.
