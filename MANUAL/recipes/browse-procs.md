# recipes/browse-procs

Live process tree from `ps` with kill action.

**One-line summary:** builds a hierarchy from
`ps -eo pid,ppid,user,comm`, with PID 1 as the root and per-process
`/proc/<pid>/status` previews; custom `k:Kill` action sends SIGTERM
after a y/n confirm.

**Demonstrates:**

- Hierarchical children synthesised from a flat external CLI — group
  by `ppid` once, dispatch by `parent_id`.
- Live system data with manual reload (`ctrl-r`) — no watcher needed
  because process state changes faster than any polling cadence.
- Reading auxiliary metadata from `/proc/<pid>/status` for the
  preview pane.
- A destructive custom `Action` guarded by `ctx.confirm`, with
  `ctx.refresh` to redraw after the kill lands.

**Usage:**

```bash
./recipes/browse-procs
```

Keys: `k` send SIGTERM (with confirmation), `ctrl-r` reload tree.

**Source:** [`recipes/browse-procs`](../../recipes/browse-procs)

---

*[← All recipes](../recipes.md)*
