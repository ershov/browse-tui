# browse-ps & browse-fs — Gutter Columns, Sort/Display Modes, Live Updates

**Date:** 2026-06-19
**Status:** Draft (pre-implementation)
**Builds on:** `2026-06-03-list-columns-design.md` (the three composable
row-format hooks + `max_col_width` + `cell_*` helpers).

## Motivation

Two recipes want richer, columnar, live process/file views:

- **browse-procs → browse-ps** — a process viewer with PID / user / CPU% /
  memory as *fixed columns to the left of the tree*, full command lines,
  flat and tree views, htop-style sort modes, a periodic background
  refresh, and new/finished-process highlighting.
- **browse-fs** — the file browser's perms / size / date columns move to
  the *left of the tree*, gain sort modes and selectable display modes.

Both need the same thing the current column machinery does **not**
provide: content in a **gutter** — fixed columns *between the selection
marker and the tree indent*, so they don't get pushed rightward as tree
depth grows. Today the columns added by `format_row_content`
(`browse-fs`, `browse-git`) live to the *right* of the indent/expander.

```
today  (content columns):  [* ][indent][▼/▶][perms size date][name]
wanted (gutter columns):   [* ][perms size date][indent][▼/▶][name]
```

## Goals

1. **Framework:** make the chrome composable so a recipe can put fixed
   columns in the gutter *without* re-implementing the tree markers, and
   without adding a fourth row-format callback. Add a **global** column-
   width measurement to complement the per-parent one.
2. **browse-ps:** rename; CPU% + memory; gutter columns; flat/tree toggle;
   htop-style sort modes; untruncated usernames; full command lines;
   background updater; new/finished highlighting.
3. **browse-fs:** gutter columns; sort modes; display modes.
4. **Portable** across Linux and macOS throughout.

## Non-goals

- A declarative column engine or header row (still deferred — see the
  2026-06-03 spec's Non-goals).
- Per-frame whole-*visible*-list alignment. The new global measurement is
  over all **loaded** items (cached, invalidated on data change), not a
  per-frame visible-set scan.
- Instantaneous per-core CPU breakdowns, swap, threads, I/O columns — out
  of scope; CPU% is a single aggregate number.

---

## Area A — Framework changes

### A1. Decompose `default_row_chrome` into three public atoms

`default_row_chrome` currently returns a flat 3-segment list
(`[selection, indent, expander]`). Split its body into three public,
individually-callable helpers and make `default_row_chrome` their
composition — so the meta-row blanking rule lives in exactly one place and
**any** of the three row-format hooks can call the atoms:

```python
default_row_selection(item, ctx) -> segments   # '* ' / '  ' (blank on meta)
default_row_indent(item, ctx)    -> segments   # '  ' * depth
default_row_expander(item, ctx)  -> segments   # '▼ ' / '▶ ' / '  ' (blank on meta)

def default_row_chrome(item, ctx):             # result unchanged
    return (default_row_selection(item, ctx)
            + default_row_indent(item, ctx)
            + default_row_expander(item, ctx))
```

Each returns a **list** of `(text, fg, bold)` segments (so they
concatenate cleanly; the indent/selection are one segment each, but a list
keeps the call sites uniform). All three are exported via the
`sys.modules['browse_tui']` alias, exactly like `default_row_content` /
`default_row` already are. `ctx` is the existing `RowContext`; nothing new
is stored on it.

**Why not a `format_row_gutter` hook or chrome atoms on `ctx`:** a fourth
callback is callback sprawl for what is really "let me re-order the
pieces"; and putting computed segments on `ctx` is inconsistent (`item`
arrives as an argument — why would `selection`/`indent`/`expander` arrive
through `ctx`?). Public helper *functions* are the pattern the codebase
already uses for `default_row_content`, are callable from all three hooks,
and add nothing to `ctx`.

The `default_row_chrome` docstring is updated to note that a recipe may
override `format_row_chrome` to inject fixed columns into the structural
prefix (between selection and indent) by composing the atoms.

### A2. `max_col_width_global(field)` — global column measurement

Add a global analog of the per-parent `max_col_width(field,
parent_id=None)`, named to match for clarity:

```python
def max_col_width_global(self, field) -> int:
    """Max display-cell width of ``str(getattr(item, field, ''))`` over
    ALL loaded items (every cached child list), not just one sibling
    group. The companion to per-parent ``max_col_width`` for gutter
    columns that must align across the whole list regardless of tree
    depth."""
```

- **Storage:** `browser._col_width_global_cache: dict[field, int]`. A miss
  iterates all loaded items (union of the cached child lists), computes
  `max(cell_width(str(getattr(item, field, ''))))`, memoises per field.
- **Invalidation:** cleared wholesale (`.clear()`) at the same choke
  points that drop the per-parent cache — `_index_drop_children` and the
  `cache_invalidate_subtree` / `cache_invalidate_all` paths — since the
  global max can change when any parent's children change. Rebuilt lazily
  on next access. No per-frame scan; cost is `O(loaded items)` only on a
  miss, consistent with the per-parent design's perf posture.
- **Semantics:** "global" = over all **loaded** items. In a lazy tree this
  is everything that can currently be shown; expanding a node that
  introduces a wider value re-aligns the column (correct, and cached until
  the next data change). Missing attribute → `''` → width 0.

### A3. Build artifact

`browse-tui` (the committed generated binary) is rebuilt from `src-tui/`
via `build-tui.sh` and `git add`ed in the same commit as the framework
change (committed generated artifact — never hand-edited).

---

## Area B — browse-ps

### B0. Process data source (shared foundation for B2/B6/B7)

A single snapshot function fetches the process table once per reload and
returns `{pid: ProcInfo}` where `ProcInfo` carries pid, ppid, user,
cpu_pct, mem_bytes, cpu_secs (cumulative CPU time, for the time sort),
and args (full command line).

Base command (POSIX keys, work on Linux procps and macOS/BSD ps):

```
ps -eo pid=,ppid=,user=,pcpu=,rss=,time=,args=
```

- `pcpu` → CPU% (see B2). `rss` → fallback memory (KB). `time` → cumulative
  CPU time, parsed `[[DD-]HH:]MM:SS` → seconds (sort key + delta input).
  `args` → full command line (B7).
- The line is split into 7 fields **keeping `args` intact** (split with a
  maxsplit so spaces in the command line survive).

The snapshot is taken **once per reload cycle** and shared across all
`get_children` calls in that cycle. The framework guarantees this: a full
`refresh()` enqueues the root with `reload=True` and every re-dispatched
expanded parent with `reload=False` (`040-state.py:6511-6527`). So the
rule is simply: **resample when `reload=True` (or when no snapshot exists
yet); reuse the cached snapshot when `reload=False`.** The root's
`reload=True` is the single sample per refresh; descendant re-fetches and
lazy expansions (`reload=False`) reuse it — no debounce or timing
heuristic. CPU% deltas use the wall-clock gap between this sample and the
previous one.

### B1. Rename browse-procs → browse-ps

Hard rename of the recipe and every live reference:

- `recipes/browse-procs` → `recipes/browse-ps`; module docstring, title,
  help text, the `browse-procs` name in its own prose.
- Tests: `test/unit/test_browse_procs.py` →
  `test/unit/test_browse_ps.py`; `test/ui/test_recipe_browse_procs.py` →
  `test/ui/test_recipe_browse_ps.py`; update imports/loader names.
- `MANUAL/recipes.md`, `MANUAL/cli.md`, `vars.sh`, and the
  cross-references in sibling recipes' comments
  (`browse-fs`/`browse-git`/`browse-md`/`browse-plan`/`browse-claude` and
  their context-menu tests) that name `browse-procs` as the convention
  example.
- **Leave historical `docs/superpowers/specs/*` design docs unchanged** —
  they are dated records of past work.

No backward-compat symlink (clean rename, per the request). This ticket
lands first in the browse-ps track and is otherwise behavior-neutral.

### B2. CPU% and memory

- **CPU%** — instantaneous, from the cumulative-CPU-time delta between the
  current and previous snapshot: `100 * Δcpu_secs / (Δwall_secs * ncpu)`
  (ncpu via `os.cpu_count()`). **If there is no prior sample** (first load,
  or a freshly-appeared pid), fall back to `ps pcpu` (lifetime average) —
  this naturally covers both startup and new processes without a special
  case. Displayed as an integer percent (e.g. `10%`).
- **Memory** — platform-differentiated, "private where cheap, best
  available otherwise":
  - **Linux:** private memory = `Private_Clean + Private_Dirty` from
    `/proc/<pid>/smaps_rollup` (one small read per pid). Fall back to RSS
    when the file is absent (old kernel) or unreadable (permissions).
  - **macOS / other:** RSS (from `ps rss`). Best portable option.
  - Shown human-formatted (`human_size`, e.g. `100M`).

### B3. Gutter columns (depends on A1/A2)

Replace the `tag='user pid=…'` chip with a left gutter via a
`format_row_chrome` override. Left-to-right order per the request:

```
[selection] [pid] [user] [cpu%] [mem] [indent] [expander] [name]
```

```python
def ps_chrome(item, ctx):
    return (default_row_selection(item, ctx)
            + ps_gutter_segments(item, ctx)      # pid · user · cpu% · mem
            + default_row_indent(item, ctx)
            + default_row_expander(item, ctx))
# BrowserConfig(..., format_row_chrome=ps_chrome)  # default content renders the name
```

`ps_gutter_segments` right-justifies pid / cpu% / mem and left-justifies
user, each to `ctx.max_col_width_global('col_<field>')` (global alignment —
columns don't jiggle with tree depth), via `cell_rjust` / `cell_ljust`,
rendered dim. The name stays the default content (last segment → truncates
at the pane edge). `show_ids='never'` (the pid is its own column now).

### B4. Flat / tree toggle (`t`)

Mirror browse-claude: a module global `_TREE_MODE` (default tree). `t`
flips it, flashes the mode, and `ctx.refresh()`s (restoring the cursor on
the same pid). `get_children` dispatches:

- **tree:** root → PID 1 (else ppid==0 minus kernel-thread 2); a pid →
  its direct children (current behavior).
- **flat:** root → *all* processes as one sorted list; any pid → `[]`.

A `--tree` / `--no-tree` CLI mirror sets `_TREE_MODE` before launch.

### B5. Sort modes (htop capitals)

Module state: `_SORT_KEY` (default `pid`) and a per-key direction map
`_SORT_DIR` (each key remembers its own last direction). Actions:

| Key | Field    | Default dir |
|-----|----------|-------------|
| `N` | pid      | ascending   |
| `P` | cpu_pct  | descending  |
| `M` | mem_bytes| descending  |
| `T` | cpu_secs | descending   |
| `U` | user     | ascending   |

Pressing the **active** key reverses its direction; pressing another key
switches to that key's *remembered* direction. Each action sets the
state, flashes the mode+direction, and `ctx.refresh()`s. Sorting is
recipe-side in `get_children`: **tree** mode sorts each parent's children;
**flat** mode sorts the whole list. (User sort: case-insensitive; ties
broken by pid for stability.)

### B6. Untruncated usernames

No `pwd` resolution (too much machinery per process). Use the platform's
native untruncated `user` column:

- **Linux (procps):** widen the field — `ps -o user:<w>=` (a width large
  enough for any account name, e.g. 32). Verify the `:width` form is
  honoured; if a `ps` variant ignores it (returns 8-char truncated),
  fall back to `ps -o uid=` + a per-*uid* (not per-process) `pwd` cache.
- **macOS / BSD:** `ps -o user=` is already untruncated.

The user column auto-sizes to the widest loaded username (A2).

### B7. Full command line

Use `args` (full command line) instead of `comm` (truncated executable).
It is the row title (the flexible last column) so it truncates naturally
at the list width. Update the docstring/help (the tree is built from
`pid,ppid,user,…,args`).

### B8. Background updater (`-d <seconds>`)

A daemon thread (browse-claude's pattern: `threading.Event` stop flag,
`daemon=True`, joined in a `finally` after `b.run()`) ticks every `-d`
seconds and triggers a reload (`b.refresh()` on the UI thread via
`b.post`, or a surgical `b.update_data(...)` — pick whichever keeps the
cursor/scroll stable; refresh already preserves cursor-by-id). Each tick
re-samples (B0), so CPU% deltas and new/finished detection (B9) ride the
same snapshot diff the user asked to run "on any reload."

- `-d` accepts a **fractional** number of seconds. **Default `4.0`**
  (auto-update on). A value **≤ 0 disables** updates (no thread started).
- CLI parsed via `recipe_argv()`. Manual `Ctrl-R` and any other reload
  also re-sample, so the diff is reload-source-agnostic.

### B9. New/finished highlighting (`h`)

`htop -H` style, using `item.row_fg` (soft 256-color green/red — softer
than the bright 2/1, e.g. ~108 / ~174). Driven by the same snapshot diff:

- **New** pids (in current, not previous) → soft-green row_fg.
- **Finished** pids (in previous, not current) → retained as a
  non-expandable **tombstone** leaf row with soft-red row_fg, then dropped.
- Both highlights are held by an **internal 3.5 s wall-clock retention
  timer** (hardcoded for now) so a change does not blink away across
  intervening refreshes; after 3.5 s the green clears and tombstones drop.
- `h` toggles the *display* of these colors/tombstones at runtime; the
  diff itself always runs (CPU% needs it).
- **Tombstones work in both flat and tree mode**, but only for a pid that
  was a **leaf** (no children) at death — placed under its former parent
  (dropped if that parent also vanished). A process that *had* children is
  **not** tombstoned: its row vanishes and any survivors reparent normally
  on the next build. This keeps the feature in the default tree mode while
  sidestepping reparenting ambiguity, with no reparenting machinery
  (leaf-death is the common case; in flat mode every vanished pid is
  trivially a leaf).

Expiry re-render piggybacks on the next update tick; with updates disabled
(`-d 0`) the 3.5 s retention is a *minimum* (highlights clear on the next
manual refresh) — acceptable, since highlight mode is primarily useful
with updates on.

---

## Area C — browse-fs

### C12. Display modes (number keys) — establishes the column set

A module global `_DISPLAY_MODE` (default `2`), switched by `1` / `2` / `3`,
each flashing the mode and `ctx.refresh()`ing:

| Key | Columns (gutter, left of tree)         | Mimics       |
|-----|----------------------------------------|--------------|
| `1` | *(none)* — name only                   | `ls -1`      |
| `2` | perms · size · date                    | (default)    |
| `3` | perms · size · date · user · group     |              |

User/group here **do** use `pwd` / `grp` resolution: it is a filesystem
listing (dozens of rows, not thousands of processes), `os.stat` only gives
numeric uid/gid, and there is no `ps` to lean on. Resolve via a per-uid /
per-gid cache (`pwd.getpwuid` / `grp.getgrgid`, falling back to the numeric
id when unresolved). Display strings are stored on the item
(`col_perms`/`col_size`/`col_mtime`/`col_user`/`col_group`).

### C10. Gutter columns (depends on A1/A2)

Move perms/size/date out of `format_row_content` into a `format_row_chrome`
gutter, matching browse-ps:

```
[selection] [perms] [size] [date] (· [user] [group] in mode 3) [indent] [expander] [name]
```

Mode 1 emits an empty gutter (just `selection + indent + expander + name`).
Columns align via `max_col_width_global`. The existing `fs_row_content`
override is removed (the name returns to default content). Error/synthetic
rows (no `col_perms`) emit an empty gutter and fall back to default content.

### C11. Sort modes

Module state `_FS_SORT_KEY` (default `name`), per-key `_FS_SORT_DIR`, and a
`_DIRS_FIRST` toggle (default on). Actions:

| Key | Field    | Default dir | Notes                                   |
|-----|----------|-------------|-----------------------------------------|
| `N` | name     | ascending   | default; case-insensitive               |
| `S` | size     | descending  |                                         |
| `T` | mtime    | descending  |                                         |
| `U` | user     | ascending   | by resolved owner name                  |
| `D` | —        | —           | toggles directories-first vs. in-line   |

Same reverse-on-repeat + per-key remembered direction as browse-ps.
browse-fs is always a tree (no flat mode), so sorting applies within each
directory's children in `get_children`. `D` flips whether directories are
grouped before files (current behavior) or sorted in-line with files by
the active key.

---

## Portability summary

| Concern            | Linux                                   | macOS / BSD            |
|--------------------|-----------------------------------------|------------------------|
| ps base columns    | `pid,ppid,user,pcpu,rss,time,args` (POSIX) | same                |
| Username (untrunc) | `ps -o user:<w>` (verify), else uid+pwd | `ps -o user` (full)    |
| Memory             | `/proc/<pid>/smaps_rollup` private, else RSS | RSS (`ps rss`)    |
| CPU%               | cputime delta, else `pcpu`              | same                   |
| File user/group    | `os.stat` + `pwd`/`grp`                  | same                   |

All external-command failures degrade gracefully (current browse-procs
already swallows `ps` errors → `[]`); a missing `smaps_rollup` falls back
to RSS; an unhonoured `user:width` falls back to uid+pwd.

## Testing

- **Framework (A):** `default_row_selection/indent/expander` compose to the
  byte-identical `default_row_chrome` (golden); meta-row blanking lives in
  the atoms; each is importable from `browse_tui`.
  `max_col_width_global` returns the max over all loaded items, hits the
  cache on the second call (spy), and invalidates on
  refresh/update_data/`cache_invalidate_*`.
- **browse-ps:** feed a **mocked `ps` output** (and faked `/proc` reads)
  for determinism — tree vs flat children; each sort key + reverse-on-
  repeat + remembered direction; CPU% delta vs lifetime fallback; private-
  vs-RSS memory selection by platform; untruncated username; full args as
  title; gutter column alignment; snapshot diff → new(green)/finished(red)
  with the 3.5 s retention; `-d` parsing (fractional, ≤0 disables).
- **browse-fs:** display modes 1/2/3 column sets; sort keys + `D` toggle;
  gutter alignment; user/group resolution + numeric fallback.
- **UI / full suite:** rendering and ctx changes require the full
  `./run-tests-parallel.sh` (test/ui spawns the real binary; scoped runs
  hide UI regressions). Assert dialog/menu outcomes via the recipe log,
  not tmux reverse-video. Context-menu builders stay unit-tested (not pty).

## Rollout (one commit per ticket; review subagent for non-trivial)

**T1 — Framework (A1+A2+A3).** Blocks B3 and C10. Rebuild the binary.

**browse-ps track** (sequential — one file):
B1 rename → B2+B6+B7+B0 data (cpu/mem/user/args + snapshot) →
B3 gutter columns *(needs T1)* → B4 flat/tree → B5 sort →
B8 updater → B9 highlight *(needs B8 diff)*.

**browse-fs track** (sequential — one file; **parallel to the ps track**
once T1 lands):
C12 display modes → C10 gutter columns → C11 sort.

The two recipe tracks touch different files and run in parallel after T1.

## Resolved decisions

1. **Snapshot-per-reload** (B0): resample on `reload=True` / first call;
   reuse on `reload=False`. The framework enqueues only the root with
   `reload=True` on a full refresh (`040-state.py:6511-6527`), so this is
   exactly one sample per reload cycle — no debounce.
2. **Tombstones** (B9): both modes, leaf-deaths only, placed under the
   former parent (dropped if it too vanished); processes with children are
   not tombstoned.
3. **Rename** (B1): rename live code/tests/`MANUAL`/`vars.sh` as of this
   work; leave dated `docs/superpowers/specs/*` as historical record.
