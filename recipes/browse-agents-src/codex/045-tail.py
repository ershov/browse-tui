# ---- file-append tail (store contract: store_tail_*) ----------------------
#
# A rollout only grows by whole appended lines, so the watermark is a byte
# offset plus the file size seen at the previous poll — the same
# mechanism as the Claude flavour, with the cross-line ``_ParseCtx`` kept
# on the state so a poll continues the dedupe / pairing where the bulk
# read stopped.


class _TailState:
    """Per-file tail state: the resume byte offset, the size at the last
    poll, the next line ordinal, every line's byte offset (for
    ``store_source_excerpt``), the common fold cursor, the latched error
    flag and the parser's cross-line context."""

    __slots__ = ('byte_offset', 'last_size', 'start_line', 'line_offsets',
                 'cursor', 'error', 'ctx')

    def __init__(self):
        self.byte_offset = 0
        self.last_size = 0
        self.start_line = 0
        self.line_offsets = [0]
        self.cursor = None
        self.error = False
        self.ctx = _ParseCtx()


# session path → the _TailState that indexed it (``store_source_excerpt``).
_FILE_INDEX = {}


def store_tail_interval():
    return 5.0


def _read_tail_lines(f, state, *, strict):
    """Complete lines from ``state.byte_offset`` as ``[(line_no, rec_or_None)]``;
    with ``strict`` a malformed line latches ``state.error`` and stops."""
    out = []
    f.seek(state.byte_offset)
    while True:
        line = f.readline()
        if not line or not line.endswith('\n'):
            break
        stripped = line.rstrip('\n')
        rec = None
        if stripped.strip():
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError:
                if strict:
                    state.error = True
                    break
                rec = None
            rec = (_parse_record(rec, state.start_line, state.ctx)
                   if isinstance(rec, dict) else None)
        out.append((state.start_line, rec))
        state.byte_offset = f.tell()
        state.line_offsets.append(state.byte_offset)
        state.start_line += 1
    return out


def store_tail_open(session_key):
    if not (isinstance(session_key, str) and session_key.endswith('.jsonl')):
        return [], None
    state = _TailState()
    try:
        f = open(session_key, 'r', encoding='utf-8', errors='replace')
    except OSError:
        return [], None
    with f:
        records = _read_tail_lines(f, state, strict=False)
    state.last_size = state.byte_offset
    _FILE_INDEX[session_key] = state
    return records, state


def store_tail_poll(session_key, state):
    if state.error:
        return [], []
    try:
        size = os.path.getsize(session_key)
    except OSError:
        return [], []
    if size < state.last_size:
        state.error = True
        return [], []
    if size == state.last_size:
        return [], []
    try:
        f = open(session_key, 'r', encoding='utf-8', errors='replace')
    except OSError:
        return [], []
    with f:
        new = _read_tail_lines(f, state, strict=True)
    state.last_size = size
    _FILE_INDEX.setdefault(session_key, state)
    return new, []


def store_source_excerpt(per_session):
    """Write the raw lines ``{session_key: [record_key, …]}`` to a temp
    file from the byte offsets recorded while reading; the path (caller
    unlinks) or ``None``."""
    import tempfile
    written_any = False
    tmp = tempfile.NamedTemporaryFile('wb', suffix='.jsonl', delete=False)
    try:
        for path, line_nos in per_session.items():
            state = _FILE_INDEX.get(path)
            if state is None:
                continue
            offsets = state.line_offsets
            try:
                f = open(path, 'rb')
            except OSError:
                continue
            with f:
                for n in line_nos:
                    if not (0 <= n < len(offsets) - 1):
                        continue
                    start, end = offsets[n], offsets[n + 1]
                    if end <= start:
                        continue
                    f.seek(start)
                    chunk = f.read(end - start)
                    if not chunk:
                        continue
                    tmp.write(chunk)
                    if not chunk.endswith(b'\n'):
                        tmp.write(b'\n')
                    written_any = True
    finally:
        tmp.close()
    if not written_any:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        return None
    return tmp.name
