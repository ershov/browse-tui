# ---- file-append tail (store contract: store_tail_*) ----------------------
#
# A Claude transcript only ever grows by whole appended lines, so the
# watermark is a byte offset plus the file size seen at the previous
# poll. ``store_tail_open`` reads the whole file once and leaves the
# state at the last complete line; ``store_tail_poll`` resumes from
# there. A partial trailing line is left for the next poll.


class _TailState:
    """Per-file tail state.

    ``error`` latches True on the first malformed complete line seen by
    a poll OR on a size shrink (truncation / rotation); the file is then
    skipped until the user-triggered reset drops the state.

    Fields:
      * ``byte_offset`` — resume point for the next read.
      * ``last_size`` — file size at the previous poll; a poll bails
        when ``current_size == last_size``.
      * ``start_line`` — line ordinal of the byte at ``byte_offset``,
        i.e. the record key of the next line read.
      * ``line_offsets`` — byte position of every line read so far plus
        one sentinel past the end, so line ``n`` spans
        ``line_offsets[n]:line_offsets[n+1]``. Serves the source
        excerpts (``store_source_excerpt``).
      * ``cursor`` — slot for the common fold cursor (opaque here).
      * ``error`` — latched failure flag.
    """

    __slots__ = ('byte_offset', 'last_size', 'start_line', 'line_offsets',
                 'cursor', 'error')

    def __init__(self, *, byte_offset, last_size, start_line,
                 cursor, error):
        self.byte_offset = byte_offset
        self.last_size = last_size
        self.start_line = start_line
        self.line_offsets = [0]
        self.cursor = cursor
        self.error = error


# session path → the _TailState that indexed it; read by
# ``store_source_excerpt`` for byte-exact line extraction.
_FILE_INDEX = {}


def store_tail_interval():
    return 5.0


def store_tailable(session_key):
    """True iff the session is a readable transcript the tail can follow."""
    return (isinstance(session_key, str) and session_key.endswith('.jsonl')
            and os.path.isfile(session_key))


def _read_tail_lines(f, state, *, strict):
    """Read complete lines from ``f`` at ``state.byte_offset``.

    Returns ``[(line_no, record_or_None), …]``; a ``Record`` parsed by
    ``_parse_record`` for each JSON object, ``None`` for blank lines and
    non-dict JSON. With ``strict`` a malformed line latches
    ``state.error`` and stops the read (live-tail semantics); without it
    the line yields ``None`` and the read goes on (bulk read).
    """
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
            rec = (_parse_record(rec, state.start_line)
                   if isinstance(rec, dict) else None)
        out.append((state.start_line, rec))
        state.byte_offset = f.tell()
        state.line_offsets.append(state.byte_offset)
        state.start_line += 1
    return out


def store_tail_open(session_key):
    """Read the whole transcript; returns ``(records, state)``.

    ``records`` is ``[(record_key, rec_or_None), …]`` in file order and
    ``state`` the tail state positioned at the last complete line
    (``last_size`` equal to that offset, so a partial trailing line is
    re-polled as soon as the file grows). ``([], None)`` when the file
    cannot be opened.
    """
    state = _TailState(byte_offset=0, last_size=0, start_line=0,
                       cursor=None, error=False)
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
    """Records appended since the last poll: ``(new, changed)``.

    ``changed`` is always empty for a file-append store. Best-effort:
    ``([], [])`` when the error flag is set, the size has not advanced,
    or the file cannot be read; a shrink latches the error flag.
    """
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
    """Write the raw lines ``{session_key: [record_key, …]}`` to a temp file.

    Uses the byte offsets recorded while reading each transcript, so no
    file is re-scanned. Returns the temp path (caller unlinks) or
    ``None`` when nothing could be extracted (unread session, keys out
    of range).
    """
    import tempfile
    written_any = False
    tmp = tempfile.NamedTemporaryFile(
        'wb', suffix='.jsonl', delete=False,
    )
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
                    start = offsets[n]
                    end = offsets[n + 1]
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
