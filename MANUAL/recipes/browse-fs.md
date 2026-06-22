# recipes/browse-fs

Filesystem browser with mtime watcher.

**One-line summary:** lazy `os.scandir` children, file/dir preview, edit /
open / delete actions, and a background mtime watcher that auto-refreshes
changed directories.

**Demonstrates:**

- Lazy `get_children` — `os.scandir` per parent, only when the user expands.
- `get_preview` branching on dir vs file (head of file, or `os.listdir`).
- Custom `Action` handlers using `ctx.run_external` for `$EDITOR` /
  `xdg-open`.
- `ctx.confirm` as a modal Yes/No dialog before destructive operations
  (compares the returned label against `'Yes'`).
- `ctx.error` and `ctx.refresh` for error reporting and post-action UI
  refresh.
- A **left gutter** of metadata columns — permissions, size, date, and (in
  the fullest detail mode) owner user / group — via `format_row_chrome` and
  the framework chrome atoms, each sized with `ctx.max_col_width_global` so
  they align across the listing while the name stays the flexible last
  column. Owner names resolve through `pwd` / `grp` (cached per id).
- `browser.watch(callback)` — a daemon thread polling mtimes and calling
  `browser.refresh(d)` on change.
- Cross-recipe launch ("detach"): a `*.md` row expands into `[md ↗]` launcher
  rows (the document plus the `.md` files it links to), and Enter on one
  opens it in `browse-md` via `ctx.run_external`. Structure/link detection
  reuses the optional `md_doc` plugin; targets are real files, so the launch
  is plain argv with `--root` anchoring.

**Usage:**

```bash
./recipes/browse-fs            # current directory
./recipes/browse-fs ~          # any path
./recipes/browse-fs /etc
```

Keys (in addition to defaults): `e` edit (`$EDITOR`), `o` open (`xdg-open`),
`d` delete (with confirmation), `m` toggle markdown coloring (when
available). `1` / `2` / `3` switch detail level — name only (like `ls -1`) ·
permissions·size·date · plus owner user·group. `N` / `S` / `T` / `U` sort by
name (asc, default) · size · time · user — press the active key to reverse
(each remembers its direction) — and `D` toggles directories-first vs.
sorted in-line with files. Enter opens the cursor row in `$EDITOR`, except on
a `[md ↗]` markdown launcher row, where it opens the target in `browse-md`.

**Source:** [`recipes/browse-fs`](../../recipes/browse-fs)

---

*[← All recipes](../recipes.md)*
