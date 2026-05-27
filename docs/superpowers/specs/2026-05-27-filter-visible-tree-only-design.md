# browse-tui — Filter: Visible-Tree-Only Semantic Design

**Date:** 2026-05-27
**Status:** Draft (pre-implementation)
**Supersedes** the optimistic-keep-visible rule and "walk all of
`state._children`" pass in `_recompute_filter_hidden` from the original
filter design (`docs/superpowers/specs/2026-05-17-filter-design.md`).
The user-facing keybindings, mode enum, state shape, and recipe API
from that design are unchanged.

## Motivation

The current filter evaluator walks every cached subtree in
`state._children` and applies an "optimistic" rule for parents whose
children aren't yet cached: treat them as if a descendant matches, so
they stay visible. This was a pragmatic stopgap when "uncached" meant
"about to arrive on the next tick". With streaming recipes
(browse-claude, browse-procs with subprocess outputs), it no longer
holds: umbrellas can stay uncached indefinitely until a side channel —
in browse-claude, the preview generator's eager-push — happens to
materialize their children.

The user-visible failure (`docs/superpowers/specs/...` from the bug
report):

> 1. `& Bash` filter committed → many umbrellas without "Bash" in
>    their visible text stay on screen (held up by the optimistic
>    rule + uncached subtrees).
> 2. Cursor moves with `show_preview=True` → preview generator runs
>    on the new cursor row, eager-pushes that umbrella's descendants
>    via `update_data`, filter recomputes, umbrella correctly hidden.
> 3. Ctrl-P (preview off) → preview generator never runs → no
>    eager-push → umbrellas stay optimistically visible forever.

The root cause is a semantic ambiguity: should the filter consider
items that the user hasn't asked to see? The original design quietly
chose "yes, optimistically" and tied the eventual-consistency to a
fetch path the user can disable. We're replacing that with an explicit
"no, the filter is about what's visible right now" rule.

## Goals

1. Define filter visibility purely in terms of the **currently
   visible tree** — i.e., the rows `_emit_children` would emit if the
   filter were off. Collapsed parents contribute their own text but
   nothing else; uncached subtrees contribute nothing.
2. Evaluate scaffold visibility honestly: an expanded parent is kept
   as scaffold iff at least one of its **currently visible** matching
   children (direct or transitive) exists.
3. Make the per-update recompute work proportional to what changed:
   add/modify/remove ops walk the affected subtree + propagate upward
   through visible ancestors until status stabilizes. Skip entirely
   when the change lands under a collapsed parent.
4. Preserve the existing user-facing surface: `&` keybinding, filter
   stack, AND semantics across stacked entries, scope-row exemption,
   recipe `set_filters` / `add_filter` / `clear_filters`,
   `Item._filter_hidden` flag, `state._filter_active` short-circuit.

## Non-goals

- A "deep" filter mode that eagerly fetches uncached subtrees to find
  matches. Not in scope; see Future work.
- A tentative-match UI marker (dim style / `?` glyph) for collapsed
  umbrellas. Possible follow-up once the new default ships.
- Touching `/` search semantics. Search continues to walk
  `visible_items` as today; this design only changes what's *in*
  `visible_items` when a filter is active.
- Recompute on cursor movement. Filtering is decoupled from the
  cursor — a cursor move never changes filter visibility.
- Recompute on collapse. The "stale scaffold survives collapse"
  contract is explicit (see *Accepted UX trade-offs* below).

---

## Semantic rules

The filter is defined by five rules. Together they determine
`Item._filter_hidden` for every reachable item:

1. **Filter operates on the currently visible tree only.** The
   visible tree is the set of rows `_emit_children` would emit if the
   filter were off — i.e., descend through items whose parents are
   in `state.expanded`, skipping items with `Item.hidden=True`. No
   filter logic descends into collapsed subtrees.
2. **Filter never looks inside collapsed nodes.** A collapsed parent
   is evaluated against its own searchable text alone; its children
   are not consulted. (This is the load-bearing change: the previous
   optimistic rule violated it.)
3. **A node passes the filter** iff its searchable text matches
   every active filter entry (the existing fragment-AND-of-substrings
   matcher, unchanged). Passing nodes are visible.
4. **An expanded non-matching node is kept as scaffold** iff at least
   one of its visible (recipe-not-hidden, expanded-reachable) children
   passes the filter, transitively. This is the only mechanism that
   keeps non-matching rows visible — a deep match keeps its ancestor
   chain visible up to the highest expanded ancestor.
5. **Newly appearing rows are evaluated per these rules.** "Newly
   appearing" means: streamed in via `update_data` under a visible
   expanded parent, or revealed by an `expand` of a parent the user
   just opened.

### Two consequences worth stating explicitly

- **Collapsing a scaffold-visible parent does not re-evaluate it.**
  The parent stays visible even though its scaffold reason (the
  matching descendants) is no longer in the visible tree. The flag
  catches up on the next filter change. See *Accepted UX
  trade-offs*.
- **Expanding a parent does not re-evaluate the parent itself, only
  the newly-revealed subtree.** Symmetric to collapse. Prevents the
  surprise of "I clicked expand and the row I expanded vanished."

### Three exemptions

- The current-scope row (`item.id == state.scope_stack[-1]`) is
  always treated as a match. Hiding the row the user is scoped into
  has no useful semantics.
- Items with `Item.hidden=True` are excluded from the walk entirely
  — they're not "visible" in the sense of Rule 1, so they cannot
  match and cannot scaffold an ancestor. (Today the walk visits them
  and the renderer separately skips them; cleaner to skip in one
  place.)
- The empty placeholder entry while the user is typing
  (`_filters = [..., '']`) contributes nothing to evaluation, same
  as today.

---

## Evaluation algorithm

```python
def _recompute_filter_hidden(state, filters, *, show_ids='auto') -> None:
    active = [q for q in filters if q]
    state._filter_active = bool(active)
    if not active:
        return  # flags become stale-but-inert; renderer guards on _filter_active

    scope_id = state.scope_stack[-1] if state.scope_stack else None

    def visit(item):
        """Bottom-up DFS over the *visible* subtree. Returns True iff
        ``item`` should be visible (i.e., ``_filter_hidden = False``).
        """
        # Recipe-hidden rows aren't part of the visible tree. They get
        # no _filter_hidden treatment (the renderer drops them first
        # anyway) and they don't influence ancestor scaffolds.
        if getattr(item, 'hidden', False):
            return False

        # Descend only into expanded subtrees. Collapsed children are
        # invisible (Rule 2) and contribute nothing.
        any_visible_desc_passes = False
        if item.has_children and item.id in state.expanded:
            for child in state._children.get(item.id, ()):
                if visit(child):
                    any_visible_desc_passes = True

        # Scope-row exemption (Rule).
        is_scope = (item.id == scope_id)
        text = _search_text(item, show_ids=show_ids,
                            is_current_scope=is_scope)
        self_passes = is_scope or all(
            _search_matches(text, q) for q in active
        )

        item._filter_hidden = not (self_passes or any_visible_desc_passes)
        return not item._filter_hidden

    # Start from the visible roots: the scope row when scoped, else the
    # top-level children. Symmetric to ``visible_items``.
    if state.scope_stack:
        scope_root_id = state.scope_stack[-1]
        scope_item = (
            _find_item(state, scope_root_id)
            or state._items_by_id.get(scope_root_id)
        )
        if scope_item is not None:
            visit(scope_item)
    else:
        for root in state._children.get(state.root_id, ()):
            visit(root)
```

**Properties:**

- Walks exactly the tree shape `_emit_children` would walk with the
  filter off. Same `state.expanded` gate, same `Item.hidden` skip.
- No optimistic rule. The `has_children and id not in _children`
  branch is **removed**.
- Cost: O(visible-tree size). Bounded by what's on screen plus the
  collapsed-subtree roots visible above/below the viewport. For
  browse-claude with the 410-reply tree expanded one level, this is
  ~30 rows.

---

## Per-op incremental update

For `update_data` deliveries, the full walk is unnecessary. The
streaming-push design relies on per-batch work being proportional to
batch size, not tree size. We introduce a helper:

```python
def _propagate_filter_status_up(state, item, filters, *, show_ids='auto'):
    """Re-evaluate ``item`` and walk up through visible ancestors,
    stopping at the first ancestor whose ``_filter_hidden`` doesn't
    change. Called per affected item after apply_ops.
    """
    active = [q for q in filters if q]
    if not active:
        return

    scope_id = state.scope_stack[-1] if state.scope_stack else None

    def visible_under(p):
        """``p`` is expanded? Yes iff ``p.id in state.expanded``."""
        return p.id in state.expanded

    cur = item
    while cur is not None:
        if getattr(cur, 'hidden', False):
            # Recipe-hidden: skip; can't scaffold an ancestor.
            cur_visible = False
        else:
            any_desc = False
            if cur.has_children and cur.id in state.expanded:
                for child in state._children.get(cur.id, ()):
                    if not getattr(child, 'hidden', False) \
                            and not child._filter_hidden:
                        any_desc = True
                        break
            is_scope = (cur.id == scope_id)
            text = _search_text(cur, show_ids=show_ids,
                                is_current_scope=is_scope)
            self_passes = is_scope or all(
                _search_matches(text, q) for q in active
            )
            new_hidden = not (self_passes or any_desc)
            cur_visible = not new_hidden

        if cur._filter_hidden == (not cur_visible):
            return  # no change → ancestors above unaffected
        cur._filter_hidden = not cur_visible
        cur = _parent_of(state, cur.id)  # None at root
```

**Dispatch policy in `update_data._apply`:**

```
for each op processed by apply_ops:
    affected_item = the item whose visibility might have changed
    parent_id = the parent under which it lives

    if parent_id is not visible-expanded:
        # Change is invisible under a collapsed parent (or under an
        # uncached / unscoped parent). Rule 2: ignore.
        continue

    if op is `upsert` of a NEW item:
        # Walk the new item's subtree first (bottom-up) so its flag
        # is correct, then propagate upward. For a leaf this is one
        # visit().
        visit(affected_item)
        _propagate_filter_status_up(state, parent, ...)

    elif op is `upsert` of an EXISTING item (mod):
        # Title/tag may have changed → re-evaluate self + propagate.
        _propagate_filter_status_up(state, affected_item, ...)

    elif op is `remove`:
        # Parent might lose a matching child; propagate from parent.
        _propagate_filter_status_up(state, parent, ...)
```

(Op vocabulary follows the streaming-push spec — `apply_ops` already
knows the affected ids per op; this design adds the
filter-propagation hook in the same iteration.)

**Falling back to a full walk** is always safe and correctness-
preserving; we'll use it for the broad triggers below where the
affected set is fuzzy or large.

---

## Recompute triggers

| Trigger | Action | Notes |
|---|---|---|
| Filter change (`set_filters` / typing keystroke / `clear_filters`) | full walk via `_recompute_filter_hidden` | bounded by visible-tree size |
| Scope change (`scope_into` / `scope_out`) | full walk | scope row changes; cheap |
| `update_data` add/mod/remove under a visible expanded parent | `_propagate_filter_status_up(affected)` | O(depth) per op; early-terminates |
| `update_data` ... under a collapsed or uncached parent | no-op | Rule 2: invisible change |
| `expand` of a collapsed parent | `visit()` over the newly-revealed subtree only; do **not** re-evaluate the expanded parent | preserves the parent's scaffold/match status set at filter-time |
| `collapse` of an expanded parent | no-op | Rule: shape changes never re-evaluate already-visible rows |
| Cursor movement | no-op | filtering is cursor-independent |
| `apply_children_results` (legacy delivery) | full walk if any delivery landed under a visible expanded parent | this path is rare (worker now delivers via `update_data`); cost acceptable |

The scope-row recompute that the bug-report design called for becomes
free here: `scope_into` / `scope_out` already trigger the full walk
table entry above.

---

## Accepted UX trade-offs

These are spelled out so future readers know they were considered, not
overlooked.

1. **Stale scaffold survives collapse.** Sequence: filter `&CCC` over
   `AAA → BBB → CCC`. AAA and BBB become scaffold-visible because of
   the CCC match. User collapses BBB, then collapses AAA. Per the "no
   recalc on shape change" rule, AAA stays visible despite no longer
   having a visible matching descendant. The flag catches up on the
   next filter change. Acceptable: collapse is user-driven and the
   visible row is still semantically *"I was a scaffold for a match
   at filter time"*.

2. **Expand of a stale-scaffold parent reveals an empty subtree.**
   Continuing the above: user expands AAA again. AAA's only direct
   child BBB is collapsed and doesn't itself match → BBB hides; AAA
   is *not* re-evaluated (only the newly-revealed BBB is). User sees
   AAA visible with no children under it. Acceptable: better than
   the alternative ("expand makes the row I clicked vanish"), and
   self-explanatory once the user understands the semantic.

3. **No "tentative" indicator on collapsed rows.** A collapsed
   umbrella whose hidden subtree contains matches is treated the same
   as one whose subtree contains nothing. The user can't tell from
   the row chrome alone whether expanding will reveal more matches.
   Acceptable for now; a follow-up could add a `?` / dim style if
   feedback shows demand.

4. **No deep-fetch on filter.** Typing a filter doesn't kick fetches
   for uncached subtrees. Users wanting "show me everything that
   matches across the whole transcript" still need to expand the
   relevant subtrees by hand, or rely on `/` search (which only
   navigates among visible matches today — a separate improvement).

---

## Migration / what's removed

- `_recompute_filter_hidden`'s `has_children and id not in _children`
  optimistic branch — deleted.
- The "walk every key in `state._children`" outer loop — replaced by
  the visible-tree DFS rooted at scope (or root children).
- The `visited` set guarding double-recursion — no longer needed; the
  walk is a tree DFS rooted at one place, not a multi-entry walk.
- `_emit_children`'s filter check (`if state._filter_active and
  getattr(child, '_filter_hidden', False): continue`) — unchanged.
  The flag still means "skip on render"; only its derivation changes.
- `Item._filter_hidden` field — unchanged.
- `state._filter_active` — unchanged (renderer's short-circuit gate).

Recipe-visible surface (`Browser.filters`, `set_filters`,
`add_filter`, `clear_filters`, the `&` keybinding, the FILTER_EDIT
mode) — all unchanged.

---

## Implementation order

1. **Rewrite `_recompute_filter_hidden`** to walk the visible tree
   (per the algorithm above). Delete the optimistic branch and the
   `state._children.keys()` outer loop. Wire `Item.hidden` skip into
   the walk.
2. **Add `_propagate_filter_status_up`** alongside the recompute.
3. **Wire `update_data._apply`** to call propagate-up per affected
   item instead of (or in addition to) the current full-walk hook.
   Keep the full-walk path as a fallback for broad triggers.
4. **Hook `expand`** to evaluate the newly-revealed subtree only.
   `Browser._do_expand` already has the right call site; replace the
   full-walk hook (where applicable) with a subtree-rooted `visit()`.
5. **Update tests** — see *Test plan*. Existing tests will need
   adjustments for the dropped optimistic rule (parents with
   uncached children no longer stay visible by default).
6. **Drop the side-channel coupling** in browse-claude that relied on
   the preview generator to materialize children for the filter to
   "find" them. No recipe code changes required after step 1; the new
   semantic is internally consistent without the side channel.
7. **Doc updates** — `docs/api.md`'s filter section, the existing
   `2026-05-17-filter-design.md` (mark as superseded for the
   evaluator section), and add a one-paragraph user-facing note about
   what the filter does and doesn't show.

---

## Test plan

Unit (evaluator):

- Empty filters → no-op, flags untouched, `_filter_active` False.
- Single filter, leaf matches inside an expanded parent → leaf visible,
  parent kept as scaffold.
- Single filter, leaf matches inside a **collapsed** parent →
  parent's `_filter_hidden` depends on parent's own match alone;
  parent's children are not walked.
- Single filter, deep match across multiple expanded levels →
  every expanded ancestor along the path stays visible; non-matching
  siblings of scaffold ancestors hidden.
- Recipe-hidden row containing a match → row excluded from the walk,
  doesn't scaffold its ancestors.
- Scope row exemption: scope row stays visible even when its text
  doesn't match.
- Multiple filter entries → AND semantics (unchanged from current
  matcher).

Unit (`_propagate_filter_status_up`):

- Add a matching child to a previously filter-hidden expanded parent
  → child evaluated → walk-up flips parent (and grand-parents) to
  visible. Terminates at first stable ancestor.
- Add a non-matching child to a scaffold-visible parent → walk-up
  terminates at the parent (no flag change).
- Modify a matching leaf to no longer match, last matcher in subtree
  → walk-up hides parent, then grand-parent if it had no other
  matching subtree.
- Remove the only matching child of a scaffold-visible parent →
  parent flips to hidden; walk-up propagates.
- Op landing under a collapsed parent → no walk, no flag changes.

Integration (Browser + main loop):

- Filter change after streaming items arrive in collapsed subtrees:
  collapsed parents that match self are visible; their hidden
  children don't influence them.
- `update_data` batch that adds 50 items under one expanded parent
  fires N+1 propagations (one per item + early-terminated walks at
  the parent). Walks the new items; doesn't re-walk the existing
  ones.
- Expand of a previously-collapsed parent triggers a single
  visit() of the new subtree; expanded parent's own flag is
  untouched.
- Collapse triggers no recompute; flags persist (stale-scaffold
  contract).
- Cursor moves trigger no recompute; flags persist.

UI (tmux):

- Reproduce the bug report flow: `& Bash` in browse-claude after
  expanding [410 replies]. With the new evaluator, umbrellas whose
  immediate-children-or-self don't match are hidden immediately.
  Toggling preview off (Ctrl-P) and moving the cursor doesn't change
  the visible set — because the filter no longer depends on the
  preview side channel.
- `& Bash` followed by expanding a `<tool:Read>` umbrella (whose
  immediate children don't contain "Bash") → umbrella's children
  appear briefly, are evaluated, non-matchers hidden, scaffold (or
  not) follows the rule.
- Stale-scaffold contract: deep filter, then collapse the scaffold
  chain, then re-expand → expanded parent stays, hidden child
  stays hidden.

---

## Future work

- `&&pattern` (or similar prefix) for opt-in deep-fetch + strict
  filter. Explicit user request to pay the traversal cost across
  uncached subtrees. Defers a non-trivial design question — what
  does "deep" mean for recipes that stream lazily? — to a future
  spec.
- Tentative-match indicator on collapsed umbrellas. Could be a dim
  style or a leading `?` glyph, surfaced when at least one filter is
  active AND the row has `has_children=True` AND `id not in
  state._children`. Hangs off the renderer; no evaluator changes.
- `/` search across uncached subtrees. Symmetric to the filter
  question; out of scope here.
- Hooks fired on filter change so recipes can react (e.g.,
  re-render decorations).
