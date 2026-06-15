# Structured (hashable) IDs — implementation design

**Status:** Proposed
**Date:** 2026-06-08
**Supersedes:** the stub epic `2026-06-05-hashable-ids-design.md` (this doc carries the
concrete decisions: host-owned ids, base-free `md_doc`, no backward compatibility).

## 1. Goal & governing principles

Let recipes use any **immutable/hashable** object as `Item.id`, so structured /
namespaced ids need no string-encoding and no string-parsing. Replace today's
"encode structure into a string, re-parse it everywhere" with first-class
hashable ids (`int`, `tuple`, `namedtuple`, `frozenset`, frozen `dataclass`).

- **No backward compatibility.** Public contracts change freely (`to_item`, the
  `#md:` codec, every recipe's id shape, the action/print boundaries).
- **Hashable, not `==`-only.** Ids are dict keys / set members on the hot path
  (`_items_by_id`, `_children`, `_parent_of_id`, `expanded`, `selected`,
  `scope_stack`, preview/column caches). They MUST stay hashable for O(1)
  lookups. `list`/`dict`/`set` ids are rejected (§5.2).
- **Tagged-tuple convention.** A structured id is a tuple whose **first element
  is a short string tag** (`('msg', …)`, `('commit', …)`). Routing is
  `id[0] == 'tag'` — order-independent, never a substring scan.
- **Composing ids are tuples; standalone scalars may stay scalar.** An id that is
  ever embedded *inside another id* (an `anchor`/base — e.g. browse-md's
  file-root becomes the anchor of an `('md', anchor, …)` id) MUST be a tagged
  tuple, so composites are uniformly `id[0]`-dispatched and there is **no
  string/tuple-mixed anchor**. A bare scalar id (`int` ticket/PID, opaque server
  key, the browse-fs filesystem path) is fine where the scalar already *is* the
  natural identity, the recipe has ~1–2 id kinds, and it is **never embedded as
  an anchor**.
- **Forward conversion is IN scope but narrow.** The **only** string→structured
  conversion in the system is parsing **user-supplied command-line strings** (the
  argv seed) into a structured root/scope id. There is no general string→id
  parser and it need not cover arbitrary ids — every other id is *born*
  structured (§4.3).
- **Backward conversion is OUT of scope.** We do **not** build a structured →
  canonical-string encoder. External boundaries extract the field they need
  (a path or sha out of the tuple); display/debug uses `str(id)` and a Python
  `repr` is acceptable.

## 2. The ID model and the composition rule

The framework already treats `id` as an opaque hashable (`Item.id: Any`, no
string methods called on it anywhere; it only `str()`/`format()`s it at display
boundaries). So the model is mostly a recipe-side discipline plus a few
framework guardrails (§5).

**Composition rule (for nested / sub-document schemes).** When a host node owns
a sub-structure (markdown subtree, etc.), the **host owns the whole id**; the
sub-scheme's coordinates ride as fields *inside* the host's tuple. The host is
the outer container; the sub-scheme never wraps the host. The markdown subtree
id is:

```
('md',  anchor, chain, line)      # a heading/doc inside a markdown subtree
('refs', anchor, chain)           # the "References" umbrella for that doc
```

`anchor` is **always a tagged tuple** (per §1's composing-ids rule) — the host
node the subtree hangs off. `chain` is the breadcrumb of referenced files,
`line` the heading offset. The provider of the sub-structure (`md_doc`)
contributes **operations, not ids** — it never sees this tuple (§3).

## 3. `md_doc.py` rework — delete the codec, keep the local engine

`md_doc` is already **id-stateless and single-document-local**: it identifies
nodes by `MdNode.line_offset` (an in-doc coordinate), reads/caches files by a
**single** `abspath`, links trees by object reference, and **never follows a
reference itself** (the recipe stitches single-doc calls together). The only
"id" surface — the `#md:` codec and the refs-umbrella helpers — is **pure string
(de)serialization that is never called inside `md_doc`** and carries a
cross-document `chain` that `md_doc` never uses. So the rework is **deletion**.

### DELETE
- `import urllib.parse`.
- Section *"#md: id codec"*: `_MD_SELECTOR`, `compose_md_id`, `parse_md_id`
  (and their ~150 lines of `quote('file://'…, safe='')` / `find` / `rfind` /
  `isdigit` / `split` machinery — all of which exists only to make a flat string
  unambiguously splittable).
- Section *"References-umbrella id"*: `_REFS_MARKER`, `refs_umbrella_id`,
  `split_refs_umbrella`.

### KEEP verbatim (all single-doc-local or single-file helpers)
- `MdNode`, line/byte indexing, title stripping, the heading/list/`'text'` tree
  builder (`build_doc_tree(text)`, `node_at_line(tree, line)`).
- Ref capture & resolution: `find_md_refs(text)`, `md_heading_trigger(text)`,
  `resolve_md_ref(token, *, doc_dir, cwd, project_root) -> abspath`,
  `find_git_root(start)`.
- Parse cache: `get_doc(abspath) -> (text, tree)`, `clear_cache()`.
- Plugin registration.

**Result:** `md_doc` shrinks (~729 → ~600 lines), contains **no id, no chain, no
non-local addressing** — only `text → tree`, line lookup, ref capture, and
single-file resolution/read. The cross-document `chain` (the sole non-local
addressing) moves into the recipes' ids.

### The markdown id becomes a recipe-level shape
`('md', anchor, chain, line)` / `('refs', anchor, chain)`:
- `anchor` — the host node the markdown hangs off, **always a tuple**:
  `('msg', jsonl, n)` (browse-claude) or `('file', primary_path)` (browse-md).
  Carried so two routes to the same file under different anchors stay distinct
  nodes.
- `chain` — a **tuple** of referenced-file abspaths followed from the anchor;
  `()` means "the anchor's own inline document".
- `line` — heading `line_offset` (int) in the last doc, or `None` for the doc
  root.

Servicing such a node (recipe orchestration — structurally unchanged from today;
only the id shape flips from string to tuple):

```python
text, tree = (inline_text(anchor), md_doc.build_doc_tree(inline_text(anchor))) \
             if not chain else md_doc.get_doc(chain[-1])
node = md_doc.node_at_line(tree, line)
# child sub-headings:  ('md', anchor, chain, child.line_offset)
# followed .md refs:   abspath = md_doc.resolve_md_ref(tok, doc_dir=dir_of(chain[-1] or anchor),
#                                                       cwd=…, project_root=…)
#                      ('md', anchor, chain + (abspath,), None)
```

`md_doc` only ever receives a **single** `abspath` (via `get_doc`) and **single**
tokens (via `resolve_md_ref`); the chain arithmetic and id construction are the
recipe's. Because browse-claude and browse-md use the identical
`('md',…)`/`('refs',…)` shape with tuple anchors (and their ids never mix —
separate processes), the ~5-line pack/unpack/route helpers MAY live in a small
**shared recipe util** — but **not** in `md_doc`, which stays codec-free.

## 4. Recipe reworks + master ID table

### 4.1 Master table — every id form → structured equivalent

| Recipe | Node kind | Today (string) | Structured id |
|---|---|---|---|
| **browse-claude** | project dir | `dir` | `('project', dir)` |
| | session file | `jsonl_path` | `('session', jsonl_path)` |
| | message | `f'{jsonl}#{n}'` | `('msg', jsonl_path, n)` |
| | subagent group | `f'{jsonl}#agent:{aid}'` | `('agent', jsonl_path, agent_id)` |
| | prompt umbrella | `f'{jsonl}#prompt:{n}'` | `('prompt', jsonl_path, line)` |
| | tool umbrella | `f'{jsonl}#tool:{n}'` | `('tool', jsonl_path, line)` |
| | span umbrella | `f'{jsonl}#span:{n}'` | `('span', jsonl_path, line)` |
| | section divider (meta) | `f'{jsonl}#sep:subagents'` | `('sep', jsonl_path, which)` |
| | truncation filler | `f'{jsonl}#__truncated__'` | `('trunc', jsonl_path)` |
| | error row | `f'__err__:{jsonl}'` | `('err', jsonl_path)` |
| | markdown subtree | `<base>#md:<enc…>#<L>` | `('md', anchor, chain, line)` |
| | references umbrella | `<docid>#refs` | `('refs', anchor, chain)` |
| **browse-git** | commit | `f'commit:{sha}'` | `('commit', sha)` |
| | file leaf | `f'file:{sha}:{path}'` | `('file', sha, path)` |
| | status leaf | `f'status:{xy}:{path}'` | `('status', xy, path)` |
| | worktree group | `f'wc:{bucket}'` | `('wc', bucket)` |
| | reflog | `f'reflog:{n}:{sha}'` | `('reflog', n, sha)` *(n int)* |
| | stash node / file | `f'stash:{n}'` / `f'stash:{n}:{p}'` | `('stash', n)` / `('stash', n, path)` *(n int)* |
| | ref | `f'ref:{name}'` | `('ref', refname)` |
| | filler (meta) | `f'filler:{ns}:{n}'` | `('filler', ns, n)` |
| | "clean" sentinel | `'status:clean'` | `('status_clean',)` |
| | "no stashes" sentinel | `'stash:none'` | `('stash_none',)` |
| | error | `'__error__'` | `('err',)` |
| | root | `None` | `None` *(unchanged)* |
| **browse-md** | file root | `path` | `('file', abspath)` *(composes as the md anchor → tuple)* |
| | in-file heading | `f'{path}#{line}'` | `('content', abspath, line)` |
| | referenced markdown | `<path>#md:<enc…>` | `('md', anchor=('file', primary), chain, line)` |
| | references umbrella | `<path>#refs` | `('refs', anchor, chain)` |
| **browse-fs** | filesystem entry | `d.path` | `path` *(bare `str` — picker output + path I/O; never an anchor)* |
| | error row | `f'__err__:{path}'` | `('err', path)` |
| **browse-plan** | ticket | `int(col0)` | `int` *(unchanged — already optimal)* |
| | Project pseudo-row | `0` | `0` *(int sentinel; unchanged)* |
| **browse-procs** | process | `int(pid)` | `int` *(unchanged — already optimal)* |
| **browse-mcp** | tool | `tool['name']` | `str` *(opaque token; unchanged)* |
| | error | `'__error__'` | `_ERROR` *(module sentinel object)* |
| **browse-jira-mcp / browse-jira** | issue | `issue['key']` | `str` *(opaque key; unchanged)* |
| | error | `'__error__'` | `_ERROR` *(module sentinel object)* |
| **browse-files / -find / -ls** | filename / path | `$TUI_ID` string | `str` *(MUST stay string — see §5.5)* |

### 4.2 Per-recipe notes

- **browse-claude (biggest win).** Every id becomes a tagged tuple, so the
  ~9-way `get_children` dispatch and the ~70 string-shape inspections across ~15
  functions collapse to `id[0]` matches; all 8 `int(suffix)` reparses and their
  `ValueError` guards disappear; the load-bearing "test `#refs` before `#md:`
  before `#`" ordering hazards vanish (tuple tags are unambiguous). The one
  stringify site, `ctx.page(ctx.cursor.id)` (the `y` action), becomes
  `ctx.page(str(ctx.cursor.id))`.
- **browse-git (clean win).** `_parse_id` already *returns* tagged tuples and is
  already non-string-tolerant — so "stop encoding to string and re-parsing;
  carry the tuple." Construction sites emit tuples; `_parse_id` is deleted;
  sentinels become distinct tags; the documented colon-in-path / colon-in-ref
  hazards evaporate. Every `git`/editor call already consumes a decoded field.
- **browse-md (now uniform tuples).** The file-root flips to `('file', abspath)`
  **because it composes as the markdown anchor** (§1) — making the anchor a tuple
  in both md hosts and removing the string/tuple-mixed-anchor case. Dispatch is
  then uniformly `id[0]` (no `isinstance(id, str)` branch). In-file headings →
  `('content', abspath, line)`; referenced markdown adopts the shared
  `('md', anchor, chain, line)` shape (§3). browse-md already carries the path in
  a separate `file_path` attribute, so anything that needs to *emit* the path
  (e.g. `$EDITOR`/`$PAGER`, a `print_format`) reads that attribute/field, not the
  id.
- **browse-fs (stays bare `str`).** Unlike browse-md, browse-fs is a **picker**:
  its id *is* the filesystem path, it is consumed directly by
  `scandir`/`open`/`rmdir`/argv, **print-exit emits it verbatim**, and it is
  **never embedded as an anchor** — so the scalar-id allowance (§1) applies.
  Only the synthetic error row becomes `('err', path)` (carries the path for its
  message; distinguished from a real path by `isinstance(id, str)`), which also
  removes a real collision with a file literally named `__err__:foo`.
- **browse-plan / browse-procs.** No change: ids are already `int` (single kind,
  never an anchor). They are the proof cases that the opaque-`Any` contract
  works. Their `str(id)` calls are mandatory CLI/`/proc` boundary
  stringification, not id-shape artifacts.
- **browse-mcp / browse-jira / browse-jira-mcp.** Two id kinds only, and the key
  is an opaque server token never used as an anchor — so the key **stays a bare
  `str`** (also pinned to string by the MCP `issue_key` arg, the
  `/browse/<KEY>` URL, and the `jira`/`$BROWSER` argv). The `'__error__'` magic
  string becomes a distinct **module sentinel object** `_ERROR = object()`,
  letting each drop the `isinstance(id, str)` guard that existed solely to
  disambiguate sentinel-vs-real-token (dispatch becomes `id is _ERROR`).
- **browse-files / -find / -ls.** Unchanged — bash recipes (§5.5).

### 4.3 Seed conversion (command-line strings → structured ids) — the ONLY forward conversion

This is the entire surface of string→structured conversion (§1). It parses only
the **user-typed argv seed** into the recipe's structured root/scope id; it is
not a general parser and need not handle arbitrary ids.

- **browse-claude:** `argv` project dir / session file → `('project', abspath)` /
  `('session', abspath)` set as `root_id` / `initial_scope`.
- **browse-md:** `argv` `FILE.md[#anchor]` → `('file', abspath)` root, or
  `('content', abspath, resolved_line)` when an `#anchor` is present.
- **browse-fs:** `argv` dir → `root_id = os.path.abspath(dir)` (bare `str`).
- **browse-git / -plan / -procs:** derive ids internally — no user-supplied id
  string to convert.
- **browse-jira\* / -mcp:** CLI JQL/args are *query* inputs, not ids; issue keys
  arrive from results as opaque `str` — nothing to seed-convert.
- **bash recipes:** ids cross as the `$TUI_ID` **string** — no conversion (§5.5).

## 5. Framework changes

### 5.1 Redefine `to_item` (`030-data.py`) — *accepted*
New contract — *any hashable is an id*:
```python
def to_item(x):
    if isinstance(x, Item):
        return x
    if isinstance(x, dict):            # kwargs payload (must carry 'id')
        ... # unchanged: known fields + extras as attrs
    return Item(id=x)                  # any other value is the id
```
This subsumes `str`, `int`, `tuple`, `frozenset`, frozen `dataclass`. The
**positional-tuple shorthand is removed** (`('a','Apple')` is now an id, not
`(id, title)`). Verified safe: the CLI input pipeline yields **dicts**
(`_coerce_dict`), `--run-py` recipes return **`Item` objects** (identity path),
and nothing in-repo passes a bare positional tuple through `to_item`.
*(`expand_path_rows`/`_split_path_row` stay string-based — they back the bash
`--path-sep` path only; structured ids never flow through them.)*

### 5.2 Hashability validation at the index choke point — *not* per-construction
`Item.__post_init__` keeps **only** the title default (`if not self.title:
self.title = str(self.id)`); it does **not** call `hash()` — that would add a
redundant hash to every one of thousands of `Item` constructions, and the id is
hashed for real the moment it enters `_items_by_id`.

Instead, route the `_items_by_id` writes through a single add-side choke point
`_index_set(state, item)` (the counterpart to the existing
`_index_drop_children`) and wrap its dict write:
```python
def _index_set(state, item):
    try:
        state._items_by_id[item.id] = item
    except TypeError as e:                       # unhashable id (list/dict/set)
        raise TypeError(
            f'Item.id must be hashable; got {item.id!r} ({type(item.id).__name__})'
        ) from e
```
The hash is computed exactly once (the dict write that happens anyway); the
`try/except` is free in CPython when no exception fires, so this is a
**debug-quality error message, not a runtime cost** — it only ever triggers when
a developer hands in an unhashable id, and it names the offender instead of
surfacing a generic `unhashable type` deep in framework internals.

### 5.3 Selection ordering
Replace `state.selected: set` with an **insertion-ordered set** (dict-backed) and
emit selection in insertion order; delete `sorted(self._state.selected)` in
`_fire_selection_change` (`040-state.py:5303`). This removes the
order-comparability requirement that `sorted()` imposes on ids (tuples with
heterogeneous positional types, or mixed id kinds, would otherwise raise
`TypeError` and silently drop the `on_selection_change` fire). Mechanical churn
at `selected`'s `.add`/`.discard`/`in`/comprehension sites.

### 5.4 Boundary stringification policy (document; no reverse codec)
The framework `str()`/`format()`s an id at exactly five boundaries — title
default, `show_ids` display, `_search_text`, `print_format`, and `$TUI_ID` (bash
recipes only). All already use `str()`/`format()` and need **no code change**.
For structured ids:
- `show_ids`: a tuple renders verbosely as `str(tuple)`; recipes with tuple ids
  keep `show_ids='never'` (browse-claude/git/md already do) or supply a
  `format_row_content`. *(Updated 2026-06-15: `'auto'` now shows only scalar
  `str`/`int` ids and suppresses non-scalar ids itself, so a tuple is no longer
  displayed verbosely and `show_ids='never'` is not required solely to hide it.)*
- `print_format` `{id}`: renders `str(id)`. Recipes that print a useful value
  either keep a bare-`str` path id (browse-fs) or emit a dedicated attribute/field
  (browse-md's `file_path`) / override `on_enter`.
- We build **no** structured→string encoder. External per-recipe boundaries
  extract the field they need (browse-plan: the `int` → str for `plan` argv;
  browse-git: `sha`/`path` fields for `git` args; browse-fs: the path id used
  directly). Debug = `str(id)`.

### 5.5 What stays string (not a compat shim — fundamental)
Bash recipes (`$TUI_ID` env var, `--print-format`, `--input`,
`--path-sep`/`expand_path_rows`); opaque server/CLI tokens (MCP tool names, Jira
keys); and the real filesystem-path id in browse-fs (bare `str`, consumed as a
path, never an anchor).

## 6. Sequencing

1. **Framework groundwork** — `to_item` redefinition (§5.1), `_index_set`
   hashability choke point (§5.2), ordered selection (§5.3). Gates everything.
2. **`md_doc`** — delete the two codec sections + the `urllib` import.
3. **browse-claude** — full tagged-tuple migration + the `('md', anchor, chain,
   line)` construction + seed conversion. (Largest; shares the md shape with #4.)
4. **browse-md** — uniform tuples (file-root → `('file', abspath)`) + the shared
   md shape + seed conversion.
5. **browse-git** — tagged tuples; delete `_parse_id`'s string codec.
6. **browse-fs error id + sentinel objects** in browse-mcp/jira/jira-mcp.
7. **browse-plan / -procs** — verify only (already `int`).
8. **bash recipes** — no change.

## 7. Testing impact

- **`test/unit/test_md_doc.py`** (~30 codec assertions) — the encoding-hazard
  tests (`assertNotIn('#', …)`, `%23`-in-path, "path containing `#md:` must not
  mis-split", segment/suffix shapes) become **obsolete and are deleted** with the
  codec; round-trip tests are removed (no codec). md_doc's remaining tests cover
  tree building / ref capture / resolution, which are unchanged.
- **`test_browse_claude_render.py`** (46 codec mentions) and
  **`test_browse_md.py`** (9) — update inline ids to the tuple / `('md',…)`
  shapes (mechanical).
- **New tests:** `_index_set` raises a clear error on an unhashable id;
  `to_item(any-hashable) -> Item(id=x)` and `to_item(dict)`; ordered-selection
  emit order; per-recipe id round-trip (compose → route on `id[0]` → unwrap) via
  a headless `Browser`; seed conversion (CLI string → structured `root_id`).

## 8. Decisions
- **`to_item` "any hashable → `Item(id=x)`"** (positional-tuple shorthand
  removed) — **accepted**.
- **Composing ids are tuples; standalone scalars may stay scalar** (§1) —
  browse-md file-root → `('file', path)`; browse-fs path / plan & procs `int` /
  Jira & MCP keys stay scalar (never embedded as anchors).
- **Hashability validated at `_index_set`, not in `Item.__post_init__`** (§5.2) —
  no per-construction hash.
- **Forward conversion = command-line argv seeds only** (§4.3); backward
  conversion out of scope, `str(id)` for debug.

## 9. Non-goals
- Any structured→string round-trippable encoder (debug uses `str(id)`).
- A general string→id parser (only argv seeds are converted, §4.3).
- Re-architecting bash recipes off the `$TUI_ID` string boundary.
- Restructuring `md_doc`'s tree/resolution engine (only the codec is removed).
- Folding the markdown id-construction helpers into `md_doc` (they live in
  recipe-land; `md_doc` stays id-free).
