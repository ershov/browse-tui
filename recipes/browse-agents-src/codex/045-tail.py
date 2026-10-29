# ---- file-append tail (store contract: store_tail_*) ----------------------
#
# A rollout grows by whole appended lines: the common ``_file_tail_*``
# helper does the reading; this part binds it to the Codex parser, whose
# cross-line ``_ParseCtx`` rides on ``state.ctx`` so a poll continues the
# dedupe / pairing where the bulk read stopped.


def store_tail_interval():
    return 5.0


def _parse_line(raw, key, state):
    return _parse_record(raw, key, state.ctx)


def store_tail_open(session_key):
    return _file_tail_open(session_key, _parse_line,
                           _FileTailState(ctx=_ParseCtx()))


def store_tail_poll(session_key, state):
    return _file_tail_poll(session_key, state, _parse_line)


def store_source_excerpt(per_session):
    return _file_tail_excerpt(per_session)
