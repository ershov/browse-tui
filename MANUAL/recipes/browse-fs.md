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
- Recipe-set Item attributes (`item.size`, `item.mode`, `item.mtime`) that
  survive the full pipeline.
- `browser.watch(callback)` — a daemon thread polling mtimes and calling
  `browser.refresh(d)` on change.
- Cross-recipe launch ("detach"): a `*.md` row expands into `»` launcher
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
`d` delete (with confirmation). Enter opens the cursor row in `$EDITOR`,
except on a `»` markdown launcher row, where it opens the target in
`browse-md`.

**Source:** [`recipes/browse-fs`](../../recipes/browse-fs)

---

*[← All recipes](../recipes.md)*
