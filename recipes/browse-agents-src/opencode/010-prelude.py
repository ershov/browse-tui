#!/usr/bin/env -S browse-tui --run-py
"""browse-agents-opencode — OpenCode project / session / record browser.

Single-file recipe generated from the numbered parts in
``recipes/browse-agents-src/`` (``common/`` plus ``opencode/``) by
``build-agents.sh``; edit the parts, not this file.

Reads the OpenCode SQLite store (``$XDG_DATA_HOME/opencode/opencode.db``,
default ``~/.local/share/opencode``; ``--db PATH`` overrides) read-only
and exposes the hierarchy

  level 0: projects    (the ``project`` rows: a git root or ``global``)
  level 1: sessions    (``session_v2`` rows, newest first; ``session``
                        rows on a 1.x store)
  level 2: records     (the session's messages, one record per content
                        element, folded into turns)
       OR  subsessions (sessions whose ``parent_id`` is this session)
  level 3: subsession records

Identifiers are tagged tuples, dispatched on their first element:
  * ``('project', project_id)``   — the ``project`` row.
  * ``('session', session_id)``   — the session row.
  * ``('msg', session_id, n)``    — record ``n`` (0-based, dense) of the
    session; the store maps it to ``(message_id, element index)``.
  * ``('agent', session_id, child_id)`` — a child session.
  * ``('prompt'|'tool'|'span', session_id, n)`` — synthetic tree umbrellas.
  * ``('md', anchor, chain, line)`` / ``('refs', anchor, chain)`` —
    markdown subtree nodes (see the markdown section in common/).
"""
