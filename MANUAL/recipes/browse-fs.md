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
- `ctx.confirm` as a y/n prompt before destructive operations.
- `ctx.error` and `ctx.refresh` for error reporting and post-action UI
  refresh.
- Recipe-set Item attributes (`item.size`, `item.mode`, `item.mtime`) that
  survive the full pipeline.
- `browser.watch(callback)` — a daemon thread polling mtimes and calling
  `browser.refresh(d)` on change.

**Usage:**

```bash
./recipes/browse-fs            # current directory
./recipes/browse-fs ~          # any path
./recipes/browse-fs /etc
```

Keys (in addition to defaults): `e` edit (`$EDITOR`), `o` open (`xdg-open`),
`d` delete (with confirmation). Enter is wired to `action:e`.

**Source:** [`recipes/browse-fs`](../../recipes/browse-fs)

---

*[← All recipes](../recipes.md)*
