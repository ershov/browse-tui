# browse-tui — Width-Dependent Preview Refetch via Broadened `on_resize`

**Date:** 2026-06-10
**Status:** Draft (pre-implementation)
**Alternative to** the parked `width-aware-preview` branch (stamp-based
design, `docs/superpowers/specs/2026-06-09-width-aware-preview-design.md`
on that branch). Same bug, much simpler mechanism — see "Why this over
the stamp approach".

## Motivation

`browse-claude` and `browse-md` render previews through md2ansi with
`line_width = ctx.preview_width` — md2ansi hard-wraps prose and lays out
tables to that exact width, so the resulting text is **width-dependent**.
The framework caches `get_preview`'s result as `Item.preview` keyed only
by id (width-agnostic). On a preview-width change (terminal resize,
split-layout change, list-ratio nudge) the framework only refreshes its
*wrapped-render* cache (`Item.preview_render`) — it re-wraps the
already-md2ansi-wrapped text, so the md2ansi line breaks / table widths
stay frozen at the old width.

## Approach

Three pieces, each reusing existing machinery:

1. **Framework:** broaden the existing `on_resize` callback to fire on
   **any pane-layout change** (terminal resize, split selector,
   list-ratio, pane toggles) — not just SIGWINCH. Signature unchanged:
   `on_resize(ctx, cols, rows)`. No `reason` argument — a recipe that
   cares re-reads the environment (`ctx.preview_width`) itself.
2. **Recipes:** register `on_resize=lambda ctx, cols, rows:
   ctx.drop_preview_cache()`. Nothing else changes — `get_preview` stays
   a plain `str`-returning function that reads `_BROWSER.preview_width`
   live (its existing form).
3. **Framework (existing behaviour):** `drop_preview_cache()` nulls the
   cached previews and auto-refetches the cursor (others lazily on
   display); the refetched `get_preview` reads the now-current
   `_BROWSER.preview_width` and re-renders at the new width.

Flow: width changes → `_layout_for` recomputes the layout → framework
fires `on_resize` → recipe drops the preview cache → framework refetches
→ `get_preview` re-renders at the new width.

## Why this over the stamp approach (the parked v1)

The v1 design added a `PreviewText(text, width)` return wrapper, an
`Item.preview_text_width` stamp threaded through both delivery paths, a
per-paint `(B′)` staleness check, and a framework `v`/`e` unwrap fix —
~300 LOC. It also introduced per-`get_preview`-call module globals
(`_PREVIEW_WIDTH`, `_PREVIEW_USED_MD2ANSI`) which are **not thread-safe**:
`get_preview` runs on two threads (the preview worker and the main thread
via the `v`/`e`/`m` actions), so concurrent calls race on those globals.

This variant:

* **Removes that entire surface.** No `PreviewText`, no stamp, no
  delivery-path threading, no `v`/`e` unwrap gap. `get_preview` is a
  plain `str` function with **no per-call state** → the thread-safety
  problem doesn't exist (it's dissolved, not patched).
* **Keeps full coverage.** The v1 reason to avoid `on_resize` was that it
  was SIGWINCH-only and missed split/ratio. Broadening `on_resize` to
  fire on any layout change closes exactly that gap — the layout is
  recomputed every paint, so detecting a layout change there catches all
  sources, the same coverage the v1 per-paint check had.
* **Net LOC goes down** (~60 vs ~300) and the change is one framework
  primitive + two one-line recipe registrations.

## Framework change — broaden `on_resize`

Today `on_resize` fires only via `_fire_resize_if_pending`
(`src-tui/040-state.py`), gated on a SIGWINCH-latched `_resize_pending`
and a `(cols, rows) != _last_size` check — so a split/ratio change (which
leaves `cols`/`rows` unchanged) never fires it.

Broaden it to fire whenever the **pane layout** changes between paints:

* `_layout_for` (`src-tui/050-render.py`, where `browser._preview_width`
  is already set ~line 2848) computes the layout every paint. Record a
  layout **signature** there — include `cols`, `rows`, and the pane
  geometry that matters (e.g. preview-pane width + children-pane width,
  or the split/ratio/visibility inputs). Including `cols`/`rows` keeps
  the old "terminal resize always fires" contract.
* Fire `on_resize(ctx, cols, rows)` from the run loop (where
  `_fire_resize_if_pending` is called today — after render, not
  mid-paint) when the signature differs from the last-fired one; update
  the stored signature. This generalizes `_fire_resize_if_pending` from
  "size changed" to "layout changed"; the SIGWINCH path still triggers
  the redraw that recomputes the layout. Implementer's choice whether to
  fold this into `_fire_resize_if_pending` or sit beside it.
* Update the `on_resize` docstring + `BrowserConfig.on_resize` doc to the
  broadened contract. No recipe uses `on_resize` today, so broadening it
  (a superset of fires) breaks no existing consumer.

## Recipe change — register the handler

In `recipes/browse-claude` and `recipes/browse-md`, add to the
`BrowserConfig`:

```python
on_resize=lambda ctx, cols, rows: ctx.drop_preview_cache(),
```

That is the whole recipe change. `get_preview` / `_md_voice` /
`_md_render` are untouched — they already read `_BROWSER.preview_width`
live and return `str`.

## Tradeoffs

* **Drop-all, not per-item.** `drop_preview_cache()` drops every cached
  preview, so the recipe's few width-*independent* previews (project
  listing, metadata card, refs list) also refetch on a layout change.
  This is fine: those are cheap and refetch is lazy (only the cursor is
  eager; the rest refetch on display). A recipe whose previews are
  expensive to generate (e.g. a future `jira`) would add its own
  raw-preview cache and reformat for the current width — its concern, not
  the framework's.
* **Transient on the cursor preview.** Dropping nulls the cursor's
  cached text, so it shows a brief "loading" for one paint until the
  refetch lands. One frame, on a rare (human-paced) event.
* **Fires on layout changes that don't affect preview width** (e.g. a
  height-only resize). The recipe drops + refetches anyway, producing
  identical previews — harmless, and simpler than width-specific gating.

## Testing

* **Framework (TDD):** `on_resize` fires on a split-selector change and a
  list-ratio change (not just SIGWINCH); still fires on terminal resize;
  does not fire when the layout is unchanged; unset `on_resize` is a
  no-op. Mirror the existing resize-fire tests.
* **Recipe (unit):** with `on_resize` registered, a simulated layout
  change invokes `drop_preview_cache` (spy).
* **Recipe (integration, tmux):** mirror the regression guard — a
  markdown **table** preview (un-re-flowable by the framework's generic
  display-wrap, so a frozen cache is observably wrong) re-renders to the
  new width after BOTH a terminal resize AND a split toggle
  (`alt-1`/`alt-2`). Verify it fails without the `on_resize` registration
  and passes with it.
* **Regression:** existing framework + recipe suites stay green;
  width-independent recipes (browse-fs/git/plan) unchanged.

Rebuild via `build-tui.sh` after `src-tui` edits; run via
`run-tests.sh` / the parallel runner.
