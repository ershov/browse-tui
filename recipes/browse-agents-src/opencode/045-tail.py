# ---- SQLite poll tail (store contract: store_tail_*) -----------------------
#
# The watermark is ``(row count, max seq, max time_updated)`` of the
# session's ``session_message`` rows (v1: ``(part count, max
# part.time_updated)``). On change, rows past the watermark become
# ``new`` records; rows updated in place (an assistant message still
# streaming) are re-expanded and reported as ``changed`` under their
# existing keys, and extra elements of the LAST row are appended as new.
# Extra elements of an earlier row would shift every later ordinal, so
# they are dropped until the next full read (DESIGN.md, ordinal note).


class _TailState:
    """Per-session tail state: the watermark, each record's ``(row id,
    element index)`` and raw object, the keys each row owns, the common
    fold cursor and the latched error flag."""

    __slots__ = ('wm', 'locs', 'raws', 'row_keys', 'cursor', 'error')

    def __init__(self):
        self.wm = None
        self.locs = []
        self.raws = []
        self.row_keys = {}
        self.cursor = None
        self.error = False


_TAIL = {}      # session_key → _TailState (also serves the source excerpts)


def store_tail_interval():
    return 1.0


def _gen():
    return _index()['gen']


def _v2_rows(sid, where='', params=()):
    rows = []
    for mid, typ, seq, created, updated, data in _query(
            'SELECT id, type, seq, time_created, time_updated, data FROM '
            f'session_message WHERE session_id = ? {where} ORDER BY seq',
            (sid, *params)):
        d = _json(data)
        rows.append({'id': mid, 'type': typ, 'seq': seq, 'created': created,
                     'updated': updated, 'data': d,
                     'raw': {'id': mid, 'type': typ, 'seq': seq, 'data': d}})
    return rows


def _v1_rows(sid):
    """One row per part, in message then part order, with the message
    envelope; ``last_text`` marks a message's final text part."""
    rows = []
    for pid, mid, created, updated, pdata, mdata in _query(
            'SELECT p.id, p.message_id, p.time_created, p.time_updated, p.data, '
            'm.data FROM part p JOIN message m ON m.id = p.message_id '
            'WHERE p.session_id = ? ORDER BY m.time_created, m.id, p.id', (sid,)):
        part, msg = _json(pdata), _json(mdata)
        rows.append({'id': pid, 'mid': mid, 'created': created, 'updated': updated,
                     'data': part, 'message': msg, 'last_text': False,
                     'raw': {'id': pid, 'message_id': mid, 'message': msg, 'part': part}})
    last = {}
    for row in rows:
        if row['data'].get('type') == 'text':
            last[row['mid']] = row
    for row in last.values():
        row['last_text'] = True
    return rows


def _session_rows(sid):
    return _v2_rows(sid) if _gen() == 'v2' else _v1_rows(sid)


def _watermark(sid):
    if _gen() == 'v2':
        rows = _query('SELECT COUNT(*), MAX(seq), MAX(time_updated) FROM '
                      'session_message WHERE session_id = ?', (sid,))
    else:
        rows = _query('SELECT COUNT(*), MAX(time_updated) FROM part '
                      'WHERE session_id = ?', (sid,))
    return tuple(rows[0]) if rows else None


def _append_rows(state, rows):
    out = []
    for row in rows:
        keys = state.row_keys.setdefault(row['id'], [])
        for idx, rec in enumerate(_expand(_gen(), row)):
            rec.key = len(state.raws)
            state.raws.append(rec.raw)
            state.locs.append((row['id'], idx))
            keys.append(rec.key)
            out.append((rec.key, rec))
    return out


def _replace_row(state, row):
    """Re-expand an updated row: ``(changed, new)`` records."""
    changed, new = [], []
    keys = state.row_keys.get(row['id'], [])
    owns_tail = bool(keys) and keys[-1] == len(state.raws) - 1
    for idx, rec in enumerate(_expand(_gen(), row)):
        if idx < len(keys):
            rec.key = keys[idx]
            state.raws[rec.key] = rec.raw
            changed.append((rec.key, rec))
        elif owns_tail:
            rec.key = len(state.raws)
            state.raws.append(rec.raw)
            state.locs.append((row['id'], idx))
            keys.append(rec.key)
            new.append((rec.key, rec))
    return changed, new


def store_tail_open(session_key):
    if not store_session_exists(session_key) or _gen() is None:
        return [], None
    state = _TailState()
    records = _append_rows(state, _session_rows(session_key))
    state.wm = _watermark(session_key)
    _TAIL[session_key] = state
    return records, state


def store_tail_poll(session_key, state):
    if state.error:
        return [], []
    wm = _watermark(session_key)
    if wm is None or wm == state.wm:
        return [], []
    if _gen() == 'v2':
        _n, max_seq, max_upd = state.wm or (0, None, None)
        fresh = _v2_rows(session_key, 'AND (seq > ? OR time_updated > ?)',
                         (max_seq if max_seq is not None else -1, max_upd or 0))
    else:
        _n, max_upd = state.wm or (0, None)
        fresh = [r for r in _v1_rows(session_key)
                 if r['id'] not in state.row_keys or (r['updated'] or 0) > (max_upd or 0)]
    changed, new = [], []
    for row in fresh:
        if row['id'] in state.row_keys:
            c, n = _replace_row(state, row)
            changed.extend(c)
            new.extend(n)
    new.extend(_append_rows(state, [r for r in fresh if r['id'] not in state.row_keys]))
    state.wm = wm
    return new, changed
