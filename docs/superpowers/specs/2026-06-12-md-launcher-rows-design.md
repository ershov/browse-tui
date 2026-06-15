# browse-claude: markdown launcher rows (replacing inline markdown subtrees)

**Date:** 2026-06-12
**Status:** approved — rebased onto the landed stdin/stdout content-channel
work (browse-md `-` stdin documents + repeatable `--root` resolution bases);
launch mechanics below are pinned against that surface. Ready for planning.
**Supersedes:** `2026-06-03-markdown-document-subtrees-design.md` *for browse-claude only* — browse-md keeps its full inline markdown feature unchanged and remains the single home of the deep markdown-browsing UX.

## Decision

Remove the inline markdown *content* subtrees from browse-claude (heading
trees, file→refs→file recursion, byte-slice previews) and replace them with
thin **launcher rows**: a message that carries browseable markdown keeps its
expansion arrow, but expanding it reveals a flat menu of browse targets, and
pressing Enter on a target launches `browse-md` on it as an external process
(`ctx.run_external` suspend/resume).

Rationale (from the composition-model discussion): embedding *structure* is
cheap but embedding *behavior* is expensive, and process composition via
`run_external` + argv deep links already provides the full browse-md feature
set (lists, anchors, multi-file, its own References umbrellas) with zero
cross-recipe coupling. The inline feature's deep machinery was duplicated
between browse-md and browse-claude and carried a recurring tax (optimistic
arrows + self-heal, chain recursion, first-child-landing interplay in the
expand hooks). The only UX genuinely lost is seeing transcript rows and
markdown outline rows mixed in one tree at the same time, which we judged not
worth the cost. Discoverability — the part worth keeping — is preserved by
the arrow + launcher rows.

## UX

Discovery is unchanged: a message whose text has markdown headings, or which
references an existing `.md` file, carries an expansion arrow (the existing
optimistic `_md_has_children` gate: `md_heading_trigger` + first-existing-ref
short-circuit; no disk reads at delivery time).

Expanding shows **flat leaf rows** — browse targets, not content. Rows render
through the framework default content (`[tag]` chip, then title — browse-claude
has no columns):

```
▼ [assistant] Here's the design...
      [md] » message markdown
      [md] » docs/design.md
      [md] » MANUAL/api.md
```

* Titles carry a `» ` prefix to signal "Enter launches something" (no other
  row in the recipe launches). The glyph is a one-char constant, trivially
  swappable; chosen single-cell and font-safe, deliberately avoiding glyphs
  that mimic the expander (`▶`/`▸`) or the expand key's arrow (`→`).
* One `↗ message markdown` row when the message's own text has headings
  (verified — see Mechanics); one row per resolved `.md` reference
  (existing-only, deduped by abspath, sorted by label; labels via the
  existing `_md_ref_label` anchoring). No grouping parent — refs are direct
  siblings of the inline row.
* Launcher rows are leaves: `has_children=False`, no recursion, no chains.
* **Enter** on a launcher row launches browse-md (see Mechanics). Enter on
  every other row keeps the framework default print-and-exit behavior — the
  picker contract is untouched. Implemented as an `on_enter` callable that
  dispatches on the cursor id and falls through to the default otherwise.
* **Preview** on a launcher row shows what would open: the message's
  markdown text (inline row) or the referenced file's contents (ref row),
  rendered through the existing `_md_voice` toggle path. The user sees the
  content before committing to a launch.

Deliberate non-features: when a message has exactly one target, the subtree
is a one-row menu (one extra keystroke vs. the old inline tree). Uniformity
wins; the preview pane already shows the content. Enter on the *message* row
itself is NOT overloaded to launch — it keeps print-and-exit. Alt-Down
(scope-into) is not overloaded either: it stays an in-app navigation
everywhere and keeps its standard no-op on launcher leaves; Enter is the one
launch trigger.

## Mechanics

**Kept from today (browse-claude side):** `_message_md_text`,
`_md_has_children` + `_record_has_md_ref` (the optimistic gate),
`_session_cwd_and_root`, `_md_resolved_refs` + `_md_ref_label` +
`_has_any_existing_ref`, the small self-heal (`kids == [] → mod(id,
has_children=False)`), and `_md_voice` (which is the general message preview
renderer anyway). `md_doc` is still imported (optional, as today) for
`md_heading_trigger` / `find_md_refs` / `resolve_md_ref` / `find_git_root`
plus one verifying `build_doc_tree` call. With `md_doc` absent the whole
feature degrades to "messages are leaves", exactly as today.

**Expand (children builder).** For a message id, build launcher rows
authoritatively:

1. Inline row: run `build_doc_tree` once over `_message_md_text(rec)`; emit
   the row only if the tree is non-empty (kills the `#`-inside-code-fence
   false positive).
2. Ref rows: `_md_resolved_refs` over the record's string leaves, anchored on
   `_session_cwd_and_root` as today.
3. Both empty → `[]`; the retained self-heal retracts the arrow on
   `on_children_loaded`, same honesty as the current feature.

Launcher ids use a generic tag so future rows can launch other tools, not
just browse-md: `('launch', anchor, kind, *params)` — e.g.
`('launch', anchor, 'md-inline')` and `('launch', anchor, 'md-file',
abspath)`. Params are flat tuple elements (ids must stay hashable — never
lists). The id stores *what to launch*, not the command line: argv is derived
in the Enter handler at launch time, so ids stay stable across rebuilds and
environment changes (cwd anchoring, stdin-vs-file delivery) and command
strings never leak into identity, cursor anchors, or the `y` show-id action.
The old `('md', …)` / `('refs', …)` id shapes disappear.

**Launch.**

Both row kinds anchor browse-md's cross-file resolution at the session's
world via the repeatable `--root` flag — the session's recorded cwd and its
git root (the same `_session_cwd_and_root` pair), in that order, skipping
whichever is unset. No `cd` wrapper is needed.

* Ref row → `browse-md <abspath> --root <cwd> --root <project_root>` (plain
  argv via `run_external`). The file's own directory remains the first
  resolution base (the CommonMark norm — `--root` bases are tried after it),
  so a self-contained doc tree behaves identically to opening it directly.
* Inline row → pipe `_message_md_text(rec)` to `browse-md - --root …` on
  **stdin** (no temp files). A stdin document's reference-following is
  suppressed unless `--root` bases are supplied, so the roots are precisely
  what lets the message's own refs resolve — the manual's
  `git show … | browse-md - --root "$(git rev-parse --show-toplevel)"`
  pattern. Delivery uses `run_external`'s shell-string form with the document
  passed through an env var (`printf '%s' "$VAR" | browse-md - …`, roots
  shell-quoted) rather than quoting the document into the command line.
  Env-var delivery is ARG_MAX-bounded (~2 MB) — a pathological message fails
  the launch through the normal error path. Keys come from the terminal as
  always (that is the stdio-separation contract); suspend/resume unchanged.
* `browse-md` is resolved like the other external tools the recipe shells out
  to (PATH); a launch failure surfaces through `run_external`'s normal
  `ctx.error` path. No babysitting beyond that.

**Removed.** The markdown-document-subtrees section of browse-claude: the
MdNode→Item mapping (`_md_heading_item` / `_md_text_item` / `_md_node_item` /
`_md_file_item`), chain ids and `_md_chain_doc`, the References umbrella
items + recursive children, `_md_subtree_children` / `_md_message_children`,
byte-slice previews (`_preview_md_node`, `_preview_md_refs_umbrella`),
`node_at_line` usage, the `'md'` / `'refs'` dispatch arms, the
`_is_md_managed_id` first-child-landing branch in the expand hooks, and the
`TestMarkdownSubtrees` coverage of all of it.

**Unchanged.** browse-md (full inline feature stays; it is the launch
target). `md_doc.py` (browse-md still consumes all of it; browse-claude now
consumes the detection/resolution half — no trimming required). The
framework.

## Testing

Replace `TestMarkdownSubtrees` with launcher-row coverage: detection gate
unchanged-behavior checks, children-builder shapes (inline-only / refs-only /
both / neither + self-heal), id routing, `↗ ` titles, preview routing,
on_enter dispatch (launcher row launches, other rows keep default), and
launch invocation construction (`--root` ordering and omission when the
session has no cwd; stdin env-var pipeline for inline content) with
`run_external` stubbed. Headless `Browser` / existing harness as per
TESTING.md; no real TTY needed since `run_external` is stubbed.

## Out of scope / follow-ups

* Equivalent launch affordances in browse-fs (`.md` → browse-md, `.jsonl` →
  browse-claude, repo dir → browse-git) and browse-git (blob → browse-md via
  temp file, outline implications) — same pattern, separate tasks.
* Shrinking browse-md's own cross-file recursion in favor of launches —
  explicitly NOT decided; browse-md stays the full-featured markdown home.
* Extra launch triggers (a mnemonic action key, or overloading Alt-Down on
  launcher leaves where it currently no-ops) — possible later sugar, omitted
  for minimalism.

## Alternatives considered (and why not)

* **Shared markdown-subtree library** (de-duplicating the inline feature
  between browse-md and browse-claude): solves duplication but keeps all the
  deep machinery alive in two hosts; superseded by removing one host's copy.
* **Generic recipe embedding / Browser-in-Browser:** embedding behavior needs
  per-namespace keymaps, row renderers, hook fan-out, and a recipes-become-
  components rewrite — rebuilding what the process boundary already provides.
* **`ctx.pick` overlay on Enter:** fewer rows, but modal, preview-less, and
  less discoverable than the tree idiom the recipe already lives in.
* **Launching on expansion (`→`) instead of Enter:** no pre-expand veto hook
  exists, so it would need leaf-faking plus a global key override; and a
  high-frequency navigation key that suspends the UI is a surprise. Enter on
  an explicitly `» `-marked leaf is the legitimate version of the instinct.
