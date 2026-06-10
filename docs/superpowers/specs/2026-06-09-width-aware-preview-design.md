# browse-tui — Width-Aware Preview Cache Design

> **❌ DECLINED — NOT IMPLEMENTED.** This stamp-based design was explored,
> implemented, reviewed, and then declined in favour of a simpler approach.
> It threaded a `PreviewText(text, width)` return wrapper and a per-`Item`
> width stamp through both preview-delivery paths plus a per-paint
> staleness check (~300 LOC), and introduced per-`get_preview` module
> globals that are not thread-safe (`get_preview` runs on both the
> preview-worker and the main thread). The shipped design instead broadens
> the existing `on_resize` hook to fire on any pane-layout change and has
> recipes call the pre-existing `ctx.drop_preview_cache()` (~60 LOC, no new
> per-call state, no new return contract). See the implemented design:
> **`2026-06-10-preview-resize-refetch-design.md`**. Retained for the
> record and the comparative rationale (its "Background: the two-layer
> preview cache" section documents the (A)/(B) invalidation pair).

**Date:** 2026-06-09
**Status:** Declined — superseded by `2026-06-10-preview-resize-refetch-design.md`
**Companion to** the `Item.preview_render` wrap cache (commit `aac2740`,
"Add Item.preview_render wrap cache with eager invalidation"). This
extends that commit's *defensive width-stamp* idea one layer up: from
the wrapped-render cache to the raw preview text itself.

## Motivation

Some recipes bake the preview pane width into the *raw* preview text.
`browse-claude` (and `browse-md`) render message bodies through
md2ansi with `line_width = ctx.preview_width` — md2ansi hard-wraps
prose and lays out tables to that exact width, and the resulting ANSI
string becomes `Item.preview`.

The framework caches `get_preview`'s result as `Item.preview`, keyed
only by item id — width-agnostically. When the preview width later
changes (terminal resize, split-layout change, list-ratio nudge), the
framework refreshes only the *wrapped-render* cache (`preview_render`),
re-wrapping the already-md2ansi-wrapped text. The md2ansi line breaks
and table widths stay frozen at the old width:

* narrower pane → the old (wider) lines overflow and get ragged
  double-wrapping;
* wider pane → the old (narrower) lines never reflow, wasting space.

The raw preview is never recomputed at the new width because nothing
tells the framework that *this* preview's text is width-dependent.

## Background: the two-layer preview cache

There are two caches per item, and understanding their division of
labour is the whole basis for this design.

| Layer | Field | Width-aware today? |
|-------|-------|--------------------|
| Wrapped render | `Item.preview_render` (`PreviewRender` namedtuple) | **Yes** — stamped with the `width` it was built at |
| Raw text | `Item.preview` (from `get_preview`) | No — assumed width-independent |

`aac2740` introduced two cooperating mechanisms for the wrap layer:

* **(A) Eager invalidation** — `_invalidate_all_preview_renders()`
  walks every loaded item and nulls `preview_render`. Fires at known
  wrap-input mutations: `set_preview`/`append_preview`/`clear`/
  `invalidate`/`drop_preview_cache`, **terminal resize**, the
  `preview_ansi` toggle, and `update_data` mod/upsert. It also serves
  bulk memory reclamation (drops the stale renders of off-screen items
  the cursor previously visited).
* **(B) Per-paint defensive check** — in `render_preview`
  (`src-tui/050-render.py`), before reusing the cache:
  `cached.width == width and cached.ansi_on == ansi_on`. On mismatch it
  regenerates and re-stamps. The commit message names this the
  "defensive width/ansi mismatch" path.

**(B) is the only mechanism that covers every width-change source.**
`_do_set_split` / `_do_set_list_ratio` (`src-tui/040-state.py`) only
`_needs_redraw.add('all')` — they never call (A). Split-layout and
list-ratio changes therefore re-wrap correctly *solely* because (B)
re-checks against the live pane width at paint time. (A) is the eager
proactive layer + memory hygiene; (B) is the at-use safety net beneath
it. Neither is redundant.

This is why the fix plugs into **(B)**: it is the catch-all. Hanging the
raw-preview refresh off (A) would reproduce the split/ratio blind spot,
because the only width-relevant trigger in all of (A) is terminal
resize — i.e. SIGWINCH-only, the same coverage as a recipe `on_resize`
handler.

## Goals

1. A width-dependent raw preview is recomputed (its `get_preview`
   re-runs) whenever the live preview width differs from the width the
   cached text was rendered at — **for every width-change source**
   (resize, split, ratio), not just SIGWINCH.
2. Width-*independent* previews (and every recipe that doesn't opt in)
   keep today's behaviour exactly — no extra re-fetches.
3. Per-preview granularity: within one recipe, some previews are
   width-dependent (md2ansi voice/umbrella) and some are not (metadata
   cards, pretty-printed tool JSON, count headers). The recipe declares
   it per preview.
4. No staleness race: the cache can never end up displaying old-width
   text stamped as if it were current-width.
5. Re-fetch is async and never blocks the paint thread.

## Non-goals

* Changing how md2ansi formats (tables, wrap). We re-run it at the new
  width; we don't reimplement its wrapping.
* Eager bulk re-fetch of off-screen width-dependent previews on a
  resize. Re-fetch stays lazy — only the preview actually shown (the
  cursor's) is recomputed; others recompute when next displayed.
* Streaming/generator previews carrying a width stamp. `browse-claude`
  /`browse-md` width-dependent previews are plain strings; the
  generator/`append_preview` path is out of scope (treated as
  width-independent, exactly as today).
* A new `on_resize`-style hook, or making split/ratio fire (A). (B)
  already covers all sources; adding eager hooks at each width-changing
  site would be fragile (every site must be enumerated and maintained)
  and eager-bulk where (B) is lazy.

---

## Design overview

Mirror the wrap layer's defensive width-stamp (B) on the raw-preview
layer:

1. **Contract.** `get_preview` may return either a plain `str`
   (width-independent — today's behaviour, unchanged) **or** a
   `PreviewText(text, width)` wrapper (width-dependent — `text` was
   rendered for pane width `width`).
2. **Stamp.** The framework stores that `width` on the Item as
   `Item.preview_text_width` (the stamp). A plain `str` leaves it
   `None`, meaning "never width-stale".
3. **Detection (the (B) extension).** In `render_preview`, alongside
   the existing `preview_render` width check, add a sibling check on
   the raw layer: if the cached text is present and
   `item.preview_text_width is not None and
   item.preview_text_width != browser.preview_width`, the raw text is
   stale.
4. **Refresh.** On staleness, call the existing
   `Browser.invalidate_preview(id)` — the canonical async discard +
   re-fetch (see "Refetch wiring" below). `get_preview` re-runs on the
   worker thread and reads the now-current `ctx.preview_width`.

Because the detection runs every paint against the live pane width, it
inherits (B)'s universal coverage automatically.

### Naming

Chosen to sit consistently in the existing preview vocabulary
(`Item.preview`, `Item.preview_render`/`PreviewRender`, `set_preview`,
`invalidate_preview`, `preview_ansi`):

* **`PreviewText`** — recipe-facing return wrapper, a `namedtuple`
  parallel to `PreviewRender`: `PreviewText(text, width)`. Returning it
  is the opt-in. Exported from the package.
* **`Item.preview_text_width`** — the stamp: the pane width
  `Item.preview` was rendered for, or `None` for width-independent.
  Deliberately **not** `Item.preview_width` — `Browser.preview_width` /
  `ctx.preview_width` already means the *live pane width*, and the
  whole check is `item.preview_text_width != browser.preview_width`
  (text-rendered-width vs live-width). Distinct names keep that
  unambiguous.

### Why the recipe attaches the width (race-freedom)

The stamp must record *the width the text was actually rendered at*. The
recipe is the only party that knows it:

* `_md_voice` reads `_BROWSER.preview_width` on the **worker thread**
  (`recipes/browse-claude:92`), while the main thread rewrites
  `_preview_width` during layout. A resize *during* a fetch moves the
  width under the worker. `get_preview` also calls `_md_voice` many
  times (umbrella cascade), so different parts of one preview could
  read different widths.
* If the framework stamped at delivery time with the live width: text
  rendered at W1, resize to W2 mid-fetch, delivered and stamped W2 →
  `stamp == live` → the per-paint check thinks it's fresh → **the W1
  text is never re-fetched** (silent permanent staleness).

So the recipe snapshots `ctx.preview_width` **once** at the top of
`get_preview`, renders the whole preview at that one width, and returns
it as `PreviewText(text, width=W)`. The stamp is then exactly the bytes'
width, and the per-paint check is exact. It self-corrects under repeated
resizes: each delivery carries the width it used, and the check
re-fetches until a delivery matches the settled live width.

This is also why the per-preview flag and the race-free stamp are the
*same* mechanism: the wrapper's **presence** = "width-dependent", and
its **width** = the stamp.

---

## Refetch wiring — reuse `invalidate_preview` (async, no new machinery)

The framework already has the exact "discard a cached preview and
re-fetch it" path, and it is fully async:

* **`request_preview(id)`** (`040-state.py`) is the async primitive: it
  sets the latest-wins `_preview_req` slot and signals the worker
  events. It never runs `get_preview` itself — the worker thread
  (`_preview_worker`) does, off the paint thread.
* **`Browser.invalidate_preview(id)`** is the canonical discard +
  re-fetch: a main-thread post (via `update_data`, so it is *deferred
  to the next drain*, not applied inline) that nulls `item.preview` +
  `item.preview_render`, resets the worker's "already-fetched" memo
  (#471, via `_kick_after_invalidate`), and calls `request_preview(id)`.
  It explicitly **preserves view state** (`_preview_scroll`,
  `_preview_at_tail`, `_help_mode`).
* **`update_data` posts to the main queue** — the batch runs inside one
  later drain; the renderer never sees torn state, and it flags
  `_needs_redraw` so a repaint follows.

So the (B′) staleness check calls `browser.invalidate_preview(cursor_id)`
and returns. Sequence:

1. Paint N detects the stamp mismatch → `invalidate_preview(cursor_id)`
   (enqueue only; **paint N completes immediately, still showing the
   current cached text** — no blank, no block).
2. Next drain: `item.preview`/`preview_render` nulled, worker kicked.
3. Worker thread re-runs `get_preview` at the now-current
   `ctx.preview_width` → delivers `PreviewText(text, newW)` →
   `_deliver_preview` sets `item.preview` + `item.preview_text_width =
   newW`, flags `_needs_redraw('preview')`.
4. Paint N+1: stamp matches live width → cache hit → correct render.

**Dedup is structural, no extra state.** The check requires
`item.preview is not None`; step 2 nulls it, so any repaint between the
kick and the delivery (e.g. browse-claude's 5 s live-tail) short-circuits
the check and cannot re-post. After delivery the text is present again;
if the width moved *again* mid-fetch the stamp won't match and the check
re-fires — converging once the width settles.

---

## Framework changes (`src-tui/`)

### 1. `PreviewText` wrapper + export (`030-data.py`, prelude/exports)

A small namedtuple, symmetric with `PreviewRender`:

```python
PreviewText = namedtuple('PreviewText', ['text', 'width'])
```

Exported from the package so recipes can `from browse_tui import
PreviewText`. Returning it from `get_preview` is the opt-in; a plain
`str` is unchanged.

### 2. `Item.preview_text_width` stamp (`030-data.py`)

`Item` is a dataclass. Add one field next to `preview` /
`preview_render`:

```python
preview_text_width: Any = field(default=None, ...)  # width Item.preview was rendered for; None = width-independent
```

`None` means "no stamp" → never width-stale. Set to an int W when the
delivered preview was a `PreviewText(text, W)`.

### 3. Carry the stamp through both delivery paths (`040-state.py`)

Raw preview reaches the Item via exactly two writers, both of which
already set `item.preview` and null `item.preview_render`:

* **Worker delivery** — `_preview_worker` calls `self.get_preview(req)`
  (~`040-state.py:6485`) then posts `_deliver_preview(id_, text)`.
  Detect a `PreviewText` return, split into `(text, width)`, and thread
  `width` to `_deliver_preview(id_, text, width=None)`, which sets
  `item.preview_text_width = width` (or `None`).
* **Op application** — `set_preview_op('set_preview', id, text)` →
  `_apply_set_preview` (~`040-state.py:1469`). Extend so a `PreviewText`
  payload sets the stamp; a plain `str` clears it (`None`).
  `Browser.set_preview(id_, text)` (public, recipe-callable) accepts
  the wrapper too, for symmetry.

`append_preview` (streaming) is unchanged — it stays width-independent
(no stamp), consistent with the non-goal above. `invalidate_preview` /
`drop_preview_cache` already null `preview` + `preview_render`; the new
field is reset on the next delivery, so they need no change (a stale
stamp on a `None` preview is inert — the check requires a present text).

### 4. Per-paint staleness check (`050-render.py`, in `render_preview`)

Co-located with the existing (B) check (~`050-render.py:1988`). After
resolving the cursor's `item`/`text` and before reusing it, when the
text is present and carries a stamp that ≠ the live pane `width`, kick
the async re-fetch:

```python
# (B') raw-preview width staleness — mirror of (B) one layer up.
if (item is not None and not browser._help_mode
        and item.preview is not None
        and item.preview_text_width is not None
        and item.preview_text_width != width):
    browser.invalidate_preview(cursor_id)   # async; see "Refetch wiring"
```

`width` here is the live preview-pane width the renderer already
computed for (B). The call is a non-blocking enqueue; this paint still
renders the current cached text.

---

## Recipe changes

Both recipes opt in identically (this pass covers both).

### `browse-claude` (the reported bug)

* `_md_voice` currently re-reads `_BROWSER.preview_width` per call
  (`recipes/browse-claude:92`). Snapshot the width **once** per
  `get_preview` invocation and thread that single value through all
  `_md_voice` calls in that preview, so the whole body renders at one
  width.
* `get_preview` returns `PreviewText(text, width)` for the
  width-dependent routes (anything through `_md_voice` → md2ansi: voice
  messages, umbrella cascades) and a plain `str` for the
  width-independent ones (`_preview_file_metadata`, pretty-printed tool
  JSON, the count-header previews that are explicitly never routed
  through md2ansi — `recipes/browse-claude:2049`).

### `browse-md` (same pattern, same pass)

`browse-md`'s `_md_render` is the identical pattern
(`_md2ansi_fn(text, line_width=preview_width)`,
`recipes/browse-md:1094`) and has the same latent bug. It opts in the
same way: snapshot-once + `PreviewText` for the md2ansi routes, plain
`str` for the document-body / non-md2ansi routes.

---

## Alternatives considered

* **Recipe `on_resize` handler** (drop the preview cache on SIGWINCH).
  Simplest, but structurally SIGWINCH-only — misses split-layout /
  list-ratio changes, which fire no resize hook. Rejected for
  incomplete coverage.
* **Hang the raw-preview drop off the eager invalidation (A).** Same
  blind spot: the only width-relevant (A) trigger is terminal resize,
  and the other (A) triggers are text mutations where dropping the raw
  text is wrong (you'd nuke text you just set). Rejected.
* **Make split/ratio call (A) too, then hook (A).** Works mechanically
  but fragile (every width-changing site must be enumerated/maintained)
  and eager-bulk where (B) is lazy. (B) is inherently complete because
  it reads the *actual* width, not the *cause*. Rejected.
* **Framework stamps the width itself** (snapshot `_preview_width`
  around the `get_preview` call). Approximate — breaks under a
  mid-fetch resize or the multi-`_md_voice` read, and the dangerous
  failure is false-fresh (silent permanent staleness). Rejected in
  favour of recipe-attached width.
* **`get_preview(id, width)` — pass width as an argument.** Also
  race-free and additionally removes the worker-thread read of
  `_preview_width`, but changes the callback signature for all recipes
  and still needs a separate per-id width-dependent signal. Heavier than
  the single `PreviewText` wrapper for this codebase. Noted, not chosen.
* **Global `BrowserConfig.preview_width_dependent` flag** instead of
  per-preview. One bool, but re-fetches the recipe's *width-independent*
  previews too. Harmless (lazy, cursor-only) but imprecise; per-preview
  via `PreviewText` is the same effort and also carries the race-free
  stamp, so per-preview wins.

---

## Testing

Follow the project's failing-test-first practice; mirror
`test/unit/test_preview_render_cache.py` (the (B) test) one layer up.

**Unit (framework):**
* `PreviewText` return → `item.preview` set + `item.preview_text_width`
  stamped (both delivery paths: worker `_deliver_preview` and the
  `set_preview` op).
* Plain `str` return → `item.preview_text_width is None` (no stamp).
* `render_preview` with `preview_text_width != live width` calls
  `invalidate_preview` exactly once (spy on `invalidate_preview` /
  `request_preview`); a repaint while the text is `None` (in flight)
  does not re-post.
* `preview_text_width == live width` → no re-fetch (cache hit).
* Width-independent item (`preview_text_width is None`) → never
  re-fetched on width change.
* `invalidate_preview` re-fetch preserves `_preview_scroll` /
  `_preview_at_tail` (regression on its documented contract).

**Unit (recipe, `browse-claude` / `browse-md`):** `get_preview` returns
`PreviewText` for a voice/umbrella id and a plain `str` for a
metadata/JSON / document-body id; the attached width equals the
snapshot taken at entry.

**Integration (headless / tmux, per `TESTING.md`):** render a voice
preview at width W1, change the pane width (resize **and** a split
toggle `alt-1`/`alt-2`), assert the displayed preview reflects md2ansi
re-wrapped to the new width (not the old). The split case is the one
that would regress if the fix were hung off (A)/`on_resize`.

**Regression:** existing `test_preview_render_cache.py` and the
`browse-claude` / `browse-md` suites stay green; width-independent
recipes (browse-fs/git/plan) see zero behavioural change.

Rebuild the concatenated binary via `build-tui.sh` after `src-tui`
edits; run the suite via `run-tests.sh` / the parallel runner (per the
documented `browse_tui` import quirk).

---

## Resolved decisions

* **Scope:** framework + `browse-claude` + `browse-md` in one pass
  (identical pattern).
* **Refetch wiring:** reuse `Browser.invalidate_preview` — the existing
  async discard+refetch — called from the (B′) check. No new machinery;
  dedup is the `item.preview is not None` guard.
* **Naming:** `PreviewText(text, width)` (wrapper) + `Item.preview_text_width`
  (stamp), chosen to avoid colliding with `Browser.preview_width`.
* **Async:** the re-fetch runs on the worker thread; `render_preview`
  only enqueues via `invalidate_preview` and never blocks.

## Open questions

*(none blocking — raise during implementation if the `render_preview`
enqueue interacts badly with an in-progress drain; the expectation per
`update_data`'s contract is that posting from within a drain is safe and
simply processed on a subsequent drain.)*
