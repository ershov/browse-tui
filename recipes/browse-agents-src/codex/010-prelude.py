#!/usr/bin/env -S browse-tui --run-py
"""browse-agents-codex — Codex CLI project / thread / record browser.

Single-file recipe generated from the numbered parts in
``recipes/browse-agents-src/`` (``common/`` plus ``codex/``) by
``build-agents.sh``; edit the parts, not this file.

Walks the rollouts under ``$CODEX_HOME/sessions/YYYY/MM/DD/`` (default
``~/.codex``), indexed by ``state_5.sqlite`` when it is present, and
exposes the hierarchy

  level 0: projects    (threads grouped by their working directory)
  level 1: threads     (one rollout .jsonl each, newest first)
  level 2: records     (the rollout lines, folded into turns)
       OR  subthreads  (threads spawned by this one, per
                        ``thread_spawn_edges``)
  level 3: subthread records

Identifiers are tagged tuples, dispatched on their first element:
  * ``('project', cwd)``          — the working directory.
  * ``('session', rollout)``      — abspath of the rollout .jsonl.
  * ``('msg', rollout, n)``       — record at line ``n`` (0-based).
  * ``('agent', rollout, tid)``   — a spawned child thread ``tid``.
  * ``('prompt'|'tool'|'span', rollout, n)`` — synthetic tree umbrellas.
  * ``('md', anchor, chain, line)`` / ``('refs', anchor, chain)`` —
    markdown subtree nodes (see the markdown section in common/).
"""
