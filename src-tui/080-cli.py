"""browse-tui: CLI parser, format parsers, --python loader, --install hooks."""

import json


# ---- parsers --------------------------------------------------------------
# Phase 1 ships tsv, json (newline-delimited objects), and json-array.
# csv / ifs / split / match land in phase 2 (tickets #23+).
#
# Each parser takes raw bytes (as captured from a child process or stdin) and
# yields plain dicts — candidate kwargs payloads for ``to_item``. Field-level
# coercion (e.g. has_children → bool) happens here so downstream layers see
# uniform types.

_TRUTHY = {'1', 'true', 'yes', 'y', 'on'}
# Falsy tokens are recognised explicitly for documentation; in phase 1 any
# unknown string is also treated as falsy (no exception). If we tighten this
# in a later phase we can compare against a _FALSY set and raise on unknown.
_FALSY = {'0', 'false', 'no', 'n', 'off', ''}


def coerce_has_children(raw):
    """Coerce a string/None/bool to bool for the has_children field.

    Truthy: ``'1'``, ``'true'``, ``'yes'``, ``'y'``, ``'on'``
    (case-insensitive), or ``True``.
    Falsy:  ``'0'``, ``'false'``, ``'no'``, ``'n'``, ``'off'``, ``''``,
    ``None``, ``False``.

    Phase 1: any other string returns ``False`` rather than raising — keeps
    parsing tolerant of upstream noise. A stricter mode can be added later.
    """
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    s = str(raw).strip().lower()
    return s in _TRUTHY


def _coerce_dict(d):
    """Apply per-field coercion to a record dict in place; return it."""
    if 'has_children' in d:
        d['has_children'] = coerce_has_children(d['has_children'])
    return d


def _split_records(data, record_sep):
    """Split raw bytes into record byte-strings, dropping empty trailing record."""
    if not data:
        return
    parts = data.split(record_sep)
    # If data ended in record_sep, the last part is empty — drop it.
    if parts and not parts[-1]:
        parts = parts[:-1]
    for p in parts:
        yield p


def parse_tsv(data, fields=None, record_sep=b'\n', strict=False):
    """Yield dicts from TSV-encoded bytes.

    Records are split on ``record_sep`` (default ``b'\\n'``); each record is
    decoded as UTF-8, has trailing ``\\r`` stripped (CRLF tolerance), and is
    split on tab into columns. Columns map to ``fields`` positionally; columns
    beyond ``len(fields)`` are silently dropped. Rows shorter than ``fields``
    leave the trailing field names absent from the dict.

    ``fields`` defaults to ``['id', 'title']``.
    """
    if fields is None:
        fields = ['id', 'title']
    for record in _split_records(data, record_sep):
        if not record:
            continue
        try:
            line = record.decode('utf-8').rstrip('\r')
        except UnicodeDecodeError:
            if strict:
                raise
            continue
        cols = line.split('\t')
        d = {}
        for i, name in enumerate(fields):
            if i < len(cols):
                d[name] = cols[i]
        yield _coerce_dict(d)


def parse_json_lines(data, record_sep=b'\n', strict=False):
    """Yield dicts from newline-delimited JSON objects.

    One JSON object per record. Empty/whitespace records are skipped.
    Malformed records are skipped silently when ``strict=False`` (the default)
    or raise when ``strict=True``. Records that are valid JSON but not objects
    (e.g. arrays, scalars) are likewise skipped or raised.
    """
    for record in _split_records(data, record_sep):
        if not record.strip():
            continue
        try:
            obj = json.loads(record)
        except json.JSONDecodeError:
            if strict:
                raise
            continue
        if not isinstance(obj, dict):
            if strict:
                raise ValueError(
                    f'expected JSON object, got {type(obj).__name__}'
                )
            continue
        yield _coerce_dict(obj)


def parse_json_array(data, strict=False):
    """Yield dicts from a single top-level JSON array.

    The whole input must be one JSON array of objects. Non-array input or
    malformed JSON yields nothing when ``strict=False``, raises when
    ``strict=True``. Non-dict array elements are skipped (or raise in strict).
    """
    try:
        arr = json.loads(data)
    except json.JSONDecodeError:
        if strict:
            raise
        return
    if not isinstance(arr, list):
        if strict:
            raise ValueError(f'expected JSON array, got {type(arr).__name__}')
        return
    for obj in arr:
        if isinstance(obj, dict):
            yield _coerce_dict(obj)
        elif strict:
            raise ValueError(
                f'array element not a dict: {type(obj).__name__}'
            )


def parse_input(data, *, fmt, fields=None, record_sep=b'\n', strict=False):
    """Parse raw bytes into an iterator of candidate Item-kwargs dicts.

    ``fmt``        — ``'tsv'`` | ``'json'`` | ``'json-array'``.
    ``fields``     — for tsv only; column names (default ``['id', 'title']``).
                     Extra columns beyond ``len(fields)`` are dropped silently.
    ``record_sep`` — ``b'\\n'`` (default) or ``b'\\0'`` (or other literal).
                     Ignored for ``'json-array'`` (whole input is one array).
    ``strict``     — ``False`` (default) skips malformed records silently;
                     ``True`` raises on the first malformed record.
    """
    if fmt == 'tsv':
        yield from parse_tsv(
            data, fields=fields, record_sep=record_sep, strict=strict,
        )
    elif fmt == 'json':
        yield from parse_json_lines(
            data, record_sep=record_sep, strict=strict,
        )
    elif fmt == 'json-array':
        yield from parse_json_array(data, strict=strict)
    else:
        raise ValueError(f'unknown input format: {fmt!r}')
