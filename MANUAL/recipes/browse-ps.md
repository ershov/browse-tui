# recipes/browse-ps

Live, auto-updating process viewer — a columnar `ps` browser with htop-style
sort, detail, flat/tree, and change-highlight modes.

**One-line summary:** builds a process hierarchy from `ps`, rendering PID,
user, CPU%, and memory as fixed columns in a left gutter (left of the tree
indent) with the full command line as the flexible last column; a background
updater refreshes the list incrementally (no flicker), and runtime keys
switch sort order, detail level, flat/tree view, and new/finished
highlighting.

**Demonstrates:**

- A **left gutter** via `format_row_chrome` composed from the framework
  chrome atoms (`default_row_selection` / `default_row_indent` /
  `default_row_expander`) with fixed columns inserted between the selection
  marker and the tree indent, sized with `ctx.max_col_width_global`.
- A **per-reload snapshot + diff**: one `ps` sample per reload feeds both the
  instantaneous CPU% (cpu-time delta vs the previous sample, lifetime
  average on the first sample) and the new/finished detection.
- **Incremental live updates** off a daemon thread: it fetches `ps`
  off-thread, then `b.post`s a single `b.update_data` batch (`mod` loaded
  rows in place, `upsert` new pids in sorted position, `remove`/tombstone
  gone ones) — no `b.refresh()` teardown, so the cursor, scroll, and
  unchanged rows don't flicker.
- **Portable system data:** `ps -eo pid,ppid,user,pcpu,rss,time,args`
  (POSIX keys; works on Linux and macOS), untruncated usernames (widened
  `ps -o user:<w>` on Linux, with a uid + `pwd` fallback), and memory as
  Linux private RSS (`/proc/<pid>/smaps_rollup`) or RSS elsewhere.
- **Soft change highlighting** via `item.row_fg` (muted green for new, muted
  red for finished/tombstone rows), held by a short retention timer so
  changes survive intervening refreshes.
- A destructive custom `Action` (`k` kill) guarded by `ctx.confirm`, and a
  right-click context menu (`on_context_menu`) for signals / `lsof` / sockets
  / environment / status / renice / `strace`.

**Usage:**

```bash
./recipes/browse-ps             # auto-updates every 4 s (default)
./recipes/browse-ps -d 1.5      # update every 1.5 s (fractional ok)
./recipes/browse-ps -d 0        # no background updates (static)
./recipes/browse-ps --no-tree   # start in flat (non-tree) view
```

Keys (in addition to defaults):

- `t` — toggle flat / tree view (cursor stays on the same pid).
- `1` / `2` / `3` — detail level: command line only · PID · PID·user·CPU%·mem
  (default).
- `N` / `P` / `M` / `T` / `U` — htop-style sort by PID (asc, default) · CPU% ·
  memory · CPU time · user; press the active key again to reverse, and each
  key remembers its own direction.
- `h` — toggle new/finished highlighting (soft green / red).
- `k` — send SIGTERM to the cursor pid (with a y/n confirm); `ctrl-r` reloads.

Right-click a process (or the `\` / F1 keys) for its context menu: send a
signal (submenu), page open files / sockets / environment / full status,
renice, or attach `strace`.

**Source:** [`recipes/browse-ps`](../../recipes/browse-ps)

---

*[← All recipes](../recipes.md)*
