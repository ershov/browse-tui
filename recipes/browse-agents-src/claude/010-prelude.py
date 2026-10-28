#!/usr/bin/env -S browse-tui --run-py
"""browse-agents-claude — Claude Code project / session / message browser.

Single-file recipe generated from the numbered parts in
``recipes/browse-agents-src/`` (``common/`` plus ``claude/``) by
``build-agents.sh``; edit the parts, not this file.

Lazily walks ``~/.claude/projects/<encoded-path>/<session>.jsonl`` files,
exposing a four-level hierarchy:

  level 0: projects    (the encoded-path subdirs of ~/.claude/projects)
  level 1: sessions    (the .jsonl files within a project)
  level 2: messages    (records within a session)
       OR  subagents   (when the session has dispatched subagents)
  level 3: subagent messages (records within a subagent .jsonl)

Subagents live on disk at
``~/.claude/projects/<encoded-cwd>/<session-id>/subagents/agent-<id>.jsonl``,
optionally accompanied by an ``agent-<id>.meta.json`` describing the
``agentType`` and ``description``. ``<encoded-cwd>`` is derived from the
cwd at dispatch time (every ``/`` and ``.`` becomes ``-``), so a session
that runs inside a git worktree stores its subagents under the worktree's
project dir — NOT necessarily the dir holding the main ``<session>.jsonl``.
The legacy co-located layout is just the special case where the cwd never
left the project root. Either way they appear as sibling rows alongside
the session's top-level messages — pick one to drill into its own
transcript. See ``_candidate_subagents_dirs`` / ``_resolve_agent_jsonl``
for the cross-project-dir lookup.

Identifiers are tagged tuples, dispatched on their first element
(``id[0]``):
  * ``('project', dir)``       — abspath of the project directory.
  * ``('session', jsonl)``     — abspath of the .jsonl file.
  * ``('msg', jsonl, n)``      — message at line ``n``; this holds for
    both top-level session messages (``jsonl`` is the main jsonl) and
    subagent messages (``jsonl`` is the ``agent-<id>.jsonl`` path), so
    $EDITOR actions and message previews work uniformly across both.
  * ``('agent', jsonl, agent_id)`` — subagent group; ``get_children``
    routes it to the subagent's own line listing.
  * ``('prompt'|'tool'|'span', jsonl, line)`` — synthetic tree-mode
    umbrellas grouping a turn / tool-execution / metadata span.
  * ``('md', anchor, chain, line)`` / ``('refs', anchor, chain)`` —
    a markdown subtree node / its References umbrella (anchor is the
    host ``('msg', …)``; see the markdown section).

Phase-3 simplifications (intentionally out of scope):
  * No delete-session action — too dangerous with no undo.
  * No inotify watch — refresh is manual via ctrl-r.
  * Message lists are capped at 1000 entries per session by default —
    large sessions truncate so the UI stays responsive. Override the
    cap with ``-n NUMBER``. Full content is reachable via the ``e``
    action which opens the underlying ``.jsonl`` in $EDITOR.
"""
