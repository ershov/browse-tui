"""browse-tui: CLI parser, format parsers, recipe runners, --install hooks."""

import argparse
import csv as _csv
import json
import os
import re as _re
import shutil
import subprocess
import sys
import tempfile
from typing import Optional


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


def coerce_has_children(raw) -> bool:
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


def parse_csv(data, fields=None, record_sep=b'\n', strict=False):
    """Yield dicts from RFC 4180 CSV-encoded bytes.

    Records are split on ``record_sep`` (default ``b'\\n'``) at the bytes
    level; each record is decoded as UTF-8 and parsed by the stdlib
    ``csv`` module so quoted fields, embedded commas, and embedded
    quotes (``""``) are handled correctly. Limitation: because record
    splitting happens at the bytes layer first, a quoted CSV field that
    contains a literal newline matching ``record_sep`` will break the
    record into two — proper CSV-aware record splitting is a future
    enhancement.

    Columns map to ``fields`` positionally; columns beyond ``len(fields)``
    are silently dropped, mirroring ``parse_tsv``.
    """
    if fields is None:
        fields = ['id', 'title']
    for record in _split_records(data, record_sep):
        if not record:
            continue
        try:
            line = record.decode('utf-8')
        except UnicodeDecodeError:
            if strict:
                raise
            continue
        try:
            row = next(_csv.reader([line]))
        except (_csv.Error, StopIteration):
            if strict:
                raise
            continue
        d = {}
        for i, name in enumerate(fields):
            if i < len(row):
                d[name] = row[i]
        yield _coerce_dict(d)


def _ifs_split(line, ifs_chars, *, collapse):
    """Split ``line`` on any character in ``ifs_chars``.

    ``collapse=True`` mimics bash IFS=' \\t\\n' behaviour: consecutive
    delimiter characters act as a single boundary, and leading/trailing
    delimiters are stripped (no empty fields produced).

    ``collapse=False`` (the non-whitespace IFS case) treats each
    delimiter as a real boundary, yielding empty strings for runs of
    consecutive delimiters and for leading/trailing delimiters.
    """
    if collapse:
        cur = []
        cols = []
        for ch in line:
            if ch in ifs_chars:
                if cur:
                    cols.append(''.join(cur))
                    cur = []
            else:
                cur.append(ch)
        if cur:
            cols.append(''.join(cur))
        return cols
    cur = []
    cols = []
    for ch in line:
        if ch in ifs_chars:
            cols.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
    cols.append(''.join(cur))
    return cols


def parse_ifs(data, fields=None, record_sep=b'\n', *, ifs_chars, strict=False):
    """Split each record on any character in ``ifs_chars`` (bash-IFS style).

    ``ifs_chars`` is a string of single-character delimiters. If every
    char in ``ifs_chars`` is whitespace (``str.isspace()``), runs of
    delimiters collapse into one boundary and leading/trailing
    delimiters are stripped — matching ``IFS=' \\t\\n'`` in bash.
    Otherwise each delimiter is a real boundary and consecutive
    delimiters yield empty fields — matching ``IFS=':'`` for
    ``/etc/passwd``.

    ``ifs_chars`` must be non-empty; an empty value raises
    ``ValueError`` because there is no sensible split rule for it.
    """
    if fields is None:
        fields = ['id', 'title']
    if not ifs_chars:
        raise ValueError('parse_ifs: ifs_chars cannot be empty')
    is_whitespace_only = all(c.isspace() for c in ifs_chars)
    for record in _split_records(data, record_sep):
        if not record:
            continue
        try:
            line = record.decode('utf-8').rstrip('\r')
        except UnicodeDecodeError:
            if strict:
                raise
            continue
        cols = _ifs_split(line, ifs_chars, collapse=is_whitespace_only)
        d = {}
        for i, name in enumerate(fields):
            if i < len(cols):
                d[name] = cols[i]
        yield _coerce_dict(d)


def parse_split(data, fields=None, record_sep=b'\n', *, pattern, strict=False):
    """Split each record using a regex pattern (``re.split`` semantics).

    ``pattern`` may be a compiled regex or a string (compiled lazily).
    Columns map to ``fields`` positionally as with ``parse_tsv`` /
    ``parse_csv`` / ``parse_ifs``. Useful for awk-style splitting on
    e.g. ``\\s+``.
    """
    if isinstance(pattern, str):
        pattern = _re.compile(pattern)
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
        cols = pattern.split(line)
        d = {}
        for i, name in enumerate(fields):
            if i < len(cols):
                d[name] = cols[i]
        yield _coerce_dict(d)


def parse_match(data, record_sep=b'\n', *, pattern, strict=False):
    """Match each record against a named-group regex; groups become fields.

    ``pattern`` may be a compiled regex or a string. Each record is
    matched with ``re.match`` (anchored at the start). Named groups
    (``(?P<name>...)``) become keys in the yielded dict. Records that
    don't match are skipped (``strict=False``) or raise
    ``ValueError`` (``strict=True``). Optional groups that didn't
    capture (``None``) are excluded from the dict so downstream
    layers don't see ``None`` where they expect a string.

    Field-mapping is implicit through the named groups, so this
    parser does not take a ``fields`` argument.
    """
    if isinstance(pattern, str):
        pattern = _re.compile(pattern)
    for record in _split_records(data, record_sep):
        if not record:
            continue
        try:
            line = record.decode('utf-8').rstrip('\r')
        except UnicodeDecodeError:
            if strict:
                raise
            continue
        m = pattern.match(line)
        if m is None:
            if strict:
                raise ValueError(
                    f'parse_match: line did not match pattern: {line!r}'
                )
            continue
        d = {k: v for k, v in m.groupdict().items() if v is not None}
        yield _coerce_dict(d)


def parse_input(data: bytes, *, fmt: str, fields=None,
                record_sep: bytes = b'\n', strict: bool = False):
    """Parse raw bytes into an iterator of candidate Item-kwargs dicts.

    ``fmt``        — ``'tsv'`` | ``'csv'`` | ``'json'`` | ``'json-array'``
                     | ``'ifs:CHARS'`` | ``'split:REGEX'`` | ``'match:REGEX'``.
                     The colon-separated formats embed their argument
                     directly (e.g. ``'ifs::'`` for ``/etc/passwd``).
    ``fields``     — for tsv/csv/ifs/split; column names (default
                     ``['id', 'title']``). Extra columns beyond
                     ``len(fields)`` are dropped silently. Ignored by
                     ``match:`` (named groups define the fields).
    ``record_sep`` — ``b'\\n'`` (default) or ``b'\\0'`` (or other literal).
                     Ignored for ``'json-array'`` (whole input is one array).
    ``strict``     — ``False`` (default) skips malformed records silently;
                     ``True`` raises on the first malformed record.
    """
    if fmt == 'tsv':
        yield from parse_tsv(
            data, fields=fields, record_sep=record_sep, strict=strict,
        )
    elif fmt == 'csv':
        yield from parse_csv(
            data, fields=fields, record_sep=record_sep, strict=strict,
        )
    elif fmt == 'json':
        yield from parse_json_lines(
            data, record_sep=record_sep, strict=strict,
        )
    elif fmt == 'json-array':
        yield from parse_json_array(data, strict=strict)
    elif fmt.startswith('ifs:'):
        ifs_chars = fmt[len('ifs:'):]
        yield from parse_ifs(
            data, fields=fields, record_sep=record_sep,
            ifs_chars=ifs_chars, strict=strict,
        )
    elif fmt.startswith('split:'):
        pat = fmt[len('split:'):]
        yield from parse_split(
            data, fields=fields, record_sep=record_sep,
            pattern=pat, strict=strict,
        )
    elif fmt.startswith('match:'):
        pat = fmt[len('match:'):]
        yield from parse_match(
            data, record_sep=record_sep, pattern=pat, strict=strict,
        )
    else:
        raise ValueError(f'unknown input format: {fmt!r}')


# ---- argument parsing -----------------------------------------------------
#
# The argument surface mirrors the design spec's "CLI surface" table.
# All seven input formats (tsv, csv, json, json-array, ifs:CHARS,
# split:REGEX, match:REGEX) ship in phase 2 (#23). Most action plumbing
# lives below; parsing here just captures raw strings and lets the caller
# wire things up.


_BARE_INPUT_FORMATS = ('tsv', 'csv', 'json', 'json-array')
_PREFIX_INPUT_FORMATS = ('ifs:', 'split:', 'match:')


def _validate_input_format(value):
    """argparse ``type=`` callback for ``--input``.

    Accepts the four bare format names (``tsv``/``csv``/``json``/
    ``json-array``) and the three colon-prefixed forms
    (``ifs:CHARS``/``split:REGEX``/``match:REGEX``); the prefix forms
    require a non-empty argument after the colon. Anything else raises
    ``argparse.ArgumentTypeError`` so argparse emits a clean error.
    """
    if value in _BARE_INPUT_FORMATS:
        return value
    for prefix in _PREFIX_INPUT_FORMATS:
        if value.startswith(prefix):
            if not value[len(prefix):]:
                raise argparse.ArgumentTypeError(
                    f'{value!r}: format {prefix!r} requires an argument '
                    f'after the colon'
                )
            return value
    raise argparse.ArgumentTypeError(
        f'invalid --input value {value!r}; expected '
        f'tsv|csv|json|json-array|ifs:CHARS|split:REGEX|match:REGEX'
    )


def build_argparser() -> argparse.ArgumentParser:
    """Construct the argparse parser for the CLI surface.

    ``add_help=False`` — we manage ``-h``/``--help`` by hand because the
    standard argparse help action calls ``sys.exit(0)`` directly which
    bypasses our ``main()`` return-code contract (and our test harness).
    """
    p = argparse.ArgumentParser(
        prog='browse-tui',
        description='Generic hierarchical browser TUI. See docs for the API.',
        epilog=(
            'recipe runners (must be the first argument, no other flags):\n'
            '  browse-tui SCRIPT [args…]            auto-detect (same as --run)\n'
            '  browse-tui --run SCRIPT [args…]      auto-detect by shebang/+x\n'
            '  browse-tui --run-py SCRIPT [args…]   run as a Python recipe (in-process)\n'
            '  browse-tui --run-cli SCRIPT [args…]  exec the script (TUI_BIN exported,\n'
            '                                       browse-tui dir prepended to PATH)\n'
            'Args after SCRIPT are forwarded to the recipe as sys.argv.\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    # Data source
    p.add_argument('-c', '--children-cmd', metavar='CMD',
                   help='Bash command listing children of $TUI_ID (lazy mode).')
    p.add_argument('--root-id', metavar='ID', default='',
                   help='Initial id passed to --children-cmd (default empty).')
    p.add_argument('-p', '--preview-cmd', metavar='CMD',
                   help='Bash command for the preview pane.')
    p.add_argument('--root-cmd', metavar='CMD',
                   help='Eager mode — CMD emits the entire tree on stdout. '
                        "Use '-' to read the tree directly from stdin "
                        "('cat' is accepted as an alias).")
    # Input format
    p.add_argument('-i', '--input', metavar='FMT', default='tsv',
                   type=_validate_input_format,
                   help='Input parser: '
                        'tsv|csv|json|json-array|ifs:CHARS|split:REGEX|match:REGEX.')
    p.add_argument('--fields', metavar='LIST', default='id,title',
                   help='Comma-separated field names for tsv/csv/ifs/split.')
    p.add_argument('--record-sep', metavar='SEP', default='nl',
                   help='Record separator: nl (default) | null | LITERAL.')
    p.add_argument('--path-sep', metavar='CHARS', default=None,
                   help='Split each row id on CHARS to synthesize a tree '
                        '(eager --root-cmd only). Ignored if rows carry '
                        'explicit parent/depth.')
    # Actions
    p.add_argument('-a', '--action', metavar='KEY:LABEL:CMD',
                   action='append', default=[],
                   help='Register a custom action; repeatable.')
    p.add_argument('--action-timeout', metavar='SECS', type=float, default=600.0,
                   help='Per-action timeout in seconds (default 600).')
    p.add_argument('--on-enter', metavar='MODE', default='print-exit',
                   help='What Enter does: print-exit | action:KEY | noop.')
    p.add_argument('--print-format', metavar='FMT', default='{id}',
                   help='Format for print-exit. str.format syntax over Item attrs.')
    # Layout
    # Tri-state preview pane: --preview forces it on, --no-preview
    # forces it off, neither is the default (None) and lets the
    # Browser auto-decide based on whether a preview source exists.
    p.add_argument('--preview', dest='preview', default=None,
                   action=argparse.BooleanOptionalAction,
                   help='Force the preview pane visible (--preview) '
                        'or hidden (--no-preview). Without either '
                        'flag, the pane is shown when --preview-cmd '
                        '(or the recipe) supplies a preview source, '
                        'and hidden otherwise.')
    p.add_argument('--children-pane', dest='children_pane', default=True,
                   action=argparse.BooleanOptionalAction,
                   help='Show (--children-pane, default) or hide '
                        '(--no-children-pane) the children-grid pane '
                        'at startup.')
    p.add_argument('--preview-ansi', default=True,
                   action=argparse.BooleanOptionalAction,
                   help='Honour ANSI SGR escape sequences in the '
                        'preview pane (default: enabled). Use '
                        '--no-preview-ansi to strip colours from '
                        'preview output. Toggled at runtime with R.')
    p.add_argument('--multi-select', dest='multi_select', default=True,
                   action=argparse.BooleanOptionalAction,
                   help='Enable (--multi-select, default) or disable '
                        '(--no-multi-select) multi-row selection.')
    p.add_argument('--list-size', metavar='N|N%', default=None,
                   help='Initial list-pane size. Either an integer line '
                        'count (interpreted relative to startup terminal '
                        'height — and therefore scales when the terminal '
                        'resizes) or a percentage with a trailing %% '
                        '(e.g. 30%%). Default: 30%% of rows. Adjustable '
                        'at runtime with - / _ (shrink) and = / + (grow).')
    p.add_argument('--split-type', metavar='TYPE', default='auto',
                   help='Initial layout split type. Values: '
                        'h|horizontal, v|vertical, m|mixed, '
                        'pc|preview-children, a|auto (default). auto '
                        'picks vertical if terminal is at least 230 '
                        'columns wide, else horizontal. Resolved at '
                        'startup; not auto-recomputed on resize.')
    p.add_argument('--tty', metavar='TTY_PATH', default='/dev/tty',
                   help='Terminal device for the UI. The UI is painted '
                        'to, and keys are read from, this device while '
                        'stdin/stdout stay free for content/results, so '
                        'a command substitution captures only the '
                        'selection. Default /dev/tty. The sentinel - '
                        'runs the UI over the process std streams '
                        '(fd 0/1), which must be a terminal.')
    p.add_argument('--show-ids', metavar='MODE', default='auto',
                   choices=('always', 'auto', 'never'),
                   help='Whether to render the per-row id before the '
                        'title: always | auto (default; suppress id '
                        'when it equals the title) | never.')
    p.add_argument('--scope-crumb', dest='show_scope_crumb',
                   action='store_true', default=False,
                   help='Show the scope drill-down crumb (▸ a ▸ b …) '
                        'in the info bar. Off by default — ids can be '
                        'long. Recipes that scope into short, '
                        'meaningful ids can pin this on at construction.')
    p.add_argument('--title', metavar='TITLE', default='browse-tui')
    p.add_argument('--initial-scope', metavar='ID', default=None,
                   help='Start scoped to this id.')
    # Help-screen prose. ``@PATH`` loads from a file (handy when the
    # blurb is long enough to bump up against shell length limits);
    # ``@@text`` escapes a literal leading ``@``. See
    # ``_resolve_help_text``.
    p.add_argument('--help-intro', metavar='TEXT_OR_@PATH', default=None,
                   help='Prose shown at the top of --help and ?. '
                        'Use @PATH to load from a file. '
                        '@@text escapes a literal leading @.')
    p.add_argument('--help-outro', metavar='TEXT_OR_@PATH', default=None,
                   help='Prose shown at the bottom of --help and ?. '
                        'Same @PATH rules as --help-intro.')
    # Install / uninstall
    p.add_argument('--install', metavar='TARGET',
                   choices=('local', 'user', 'system', 'env'))
    p.add_argument('--uninstall', metavar='TARGET',
                   choices=('local', 'user', 'system', 'env'))
    p.add_argument('--force', action='store_true',
                   help='Overwrite existing installed binary if it differs.')
    # Recipe runners — described in build_argparser's epilog rather than
    # as argparse arguments because they take a recipe path AND swallow
    # everything after it. The pre-scan in parse_args handles dispatch
    # before argparse runs; argparse never sees these flags. They are
    # listed in --help via the epilog text below.
    # Plugin loading. Extracted from argv before parse_args runs so
    # this entry exists only to make ``--help`` list the flag; the
    # action is a no-op (argparse never sees ``--plugin`` because
    # ``_extract_plugins`` already removed it).
    p.add_argument('--plugin', metavar='SPEC', action='append', default=[],
                   help='Load a Python plugin. SPEC is a module name '
                        '(import-time) or a filesystem path (loaded via '
                        'spec_from_file_location). Repeatable. Rejected '
                        'with --run-cli or --run resolving to a non-Python '
                        'recipe.')
    # Debug / ops
    p.add_argument('--command-log', action='store_true',
                   help='Show command log on quit.')
    p.add_argument('--version', action='store_true', help='Print version and exit.')
    p.add_argument('-h', '--help', action='store_true', help='Show help and exit.')
    return p


_RECIPE_FLAGS = ('--run', '--run-py', '--run-cli')


def _setup_plugin_sys_path(script_path: Optional[str] = None) -> None:
    """Prepend the standard module-discovery directories to ``sys.path``.

    Idempotent — each candidate is added at most once. Called from
    ``main`` before plugin loading and from ``cmd_run_py`` before
    running a Python recipe so:

    1. The directory containing the running ``browse-tui`` binary is
       searchable. Plugins shipped alongside the binary become
       importable by short name.
    2. The directory of the main Python recipe (when there is one) is
       searchable. A recipe and its companion plugins can sit in the
       same directory; the recipe's own ``import`` and
       ``--plugin SHORTNAME`` both resolve.

    Path-form ``--plugin`` SPECs add their own parent directory in
    ``_load_plugins`` (#3 in the spec).
    """
    candidates = []
    own = sys.argv[0] if sys.argv else None
    if own:
        own_dir = os.path.dirname(os.path.realpath(own))
        if own_dir:
            candidates.append(own_dir)
    if script_path:
        candidates.append(os.path.dirname(os.path.abspath(script_path)) or '.')
    for d in candidates:
        if d and d not in sys.path:
            sys.path.insert(0, d)


def _load_plugins(specs: list) -> None:
    """Import each ``--plugin`` SPEC, in CLI order.

    SPEC classification:

    * Contains ``/`` or ends in ``.py`` → filesystem path. Loaded
      via ``importlib.util.spec_from_file_location`` and named after
      the basename (``.py`` stripped). The file's parent directory is
      prepended to ``sys.path`` so the plugin can import its own
      sibling modules.
    * Otherwise → module name. Loaded via
      ``importlib.import_module``. Must be on ``sys.path``.

    Failures propagate as plain ``ImportError`` (or any other
    exception the plugin's module body raises). The framework
    deliberately does not catch — silent skips would be much harder
    to debug than a clear traceback.
    """
    import importlib
    import importlib.util
    for spec in specs:
        if '/' in spec or spec.endswith('.py'):
            path = os.path.abspath(spec)
            parent = os.path.dirname(path) or '.'
            if parent not in sys.path:
                sys.path.insert(0, parent)
            name = os.path.basename(path)
            if name.endswith('.py'):
                name = name[:-3]
            module_spec = importlib.util.spec_from_file_location(name, path)
            if module_spec is None or module_spec.loader is None:
                raise ImportError(f'cannot load plugin from {path}')
            module = importlib.util.module_from_spec(module_spec)
            sys.modules[name] = module
            module_spec.loader.exec_module(module)
        else:
            importlib.import_module(spec)


def _extract_plugins(argv: list) -> tuple:
    """Strip ``--plugin SPEC`` pairs out of ``argv``.

    Returns ``(plugins, remaining)`` where ``plugins`` is the ordered
    list of SPEC strings and ``remaining`` is ``argv`` with the
    ``--plugin`` flag and its value removed wherever they appeared.

    Also supports ``--plugin=SPEC`` (no whitespace).

    Done up-front so plugin loading is orthogonal to argparse vs.
    recipe-mode dispatch. Plugins apply to TUI mode, ``--run`` (when
    it resolves to Python), and ``--run-py``; ``--run-cli`` is
    rejected later in ``main`` per the spec.
    """
    plugins: list = []
    remaining: list = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == '--plugin':
            if i + 1 >= len(argv):
                sys.stderr.write('browse-tui: --plugin requires a SPEC argument\n')
                sys.exit(2)
            plugins.append(argv[i + 1])
            i += 2
            continue
        if tok.startswith('--plugin='):
            spec = tok[len('--plugin='):]
            if not spec:
                sys.stderr.write('browse-tui: --plugin requires a SPEC argument\n')
                sys.exit(2)
            plugins.append(spec)
            i += 1
            continue
        remaining.append(tok)
        i += 1
    return plugins, remaining


def parse_args(argv: list) -> tuple:
    """Parse ``argv`` (list[str], excluding program name).

    Returns ``(args, extras)``. In TUI mode ``args`` is the argparse
    namespace and ``extras`` is empty. In recipe mode argparse is
    bypassed entirely and ``args`` is a small namespace carrying
    ``run`` (the script path) and ``run_mode`` (one of ``'auto'``,
    ``'py'``, ``'cli'``); ``extras`` is the recipe's argv.

    Recipe-mode dispatch (must be first):

    * ``argv[0]`` is one of ``--run``, ``--run-py``, ``--run-cli`` —
      ``argv[1]`` is the script path; ``argv[2:]`` are recipe args.
    * ``argv[0]`` is a non-flag token — treated as a recipe path in
      auto-detect mode (``--run``); ``argv[1:]`` are recipe args.

    No browse-tui flags may precede a recipe path. The constraint is
    deliberate: most binary flags would silently no-op (the recipe
    builds its own Browser) and the ones that take shell commands
    would be a security smell. If the user wants to mix, they put the
    flag *inside* the recipe code.

    Otherwise (``argv[0]`` is a flag), argparse handles the whole argv
    in TUI mode and ``extras`` stays empty.

    ``--plugin SPEC`` (repeatable) is extracted from anywhere in
    ``argv`` before either dispatch path runs, so it may appear before
    the recipe path or anywhere among the TUI-mode flags.
    """
    plugins, argv = _extract_plugins(argv)

    if argv:
        first = argv[0]

        if first in _RECIPE_FLAGS:
            mode = {'--run': 'auto', '--run-py': 'py', '--run-cli': 'cli'}[first]
            if len(argv) < 2 or argv[1].startswith('-'):
                sys.stderr.write(
                    f'browse-tui: {first} requires a recipe path '
                    f'as the next argument\n'
                )
                sys.exit(2)
            ns = _recipe_namespace(mode, argv[1])
            ns.plugins = plugins
            return ns, list(argv[2:])

        # Bare positional → auto-detect mode.
        if not first.startswith('-'):
            ns = _recipe_namespace('auto', first)
            ns.plugins = plugins
            return ns, list(argv[1:])

    # TUI mode — full argparse.
    p = build_argparser()
    args = p.parse_args(argv)
    args.run = None
    args.run_mode = None
    args.plugins = plugins
    return args, []


def _recipe_namespace(mode: str, script: str) -> argparse.Namespace:
    """Build a minimal namespace for recipe-mode dispatch.

    Carries just the fields ``main()`` checks; argparse-only fields
    (``--children-cmd`` etc.) are absent on purpose so any downstream
    code that touches them in recipe mode raises ``AttributeError``
    rather than silently using a stale default.
    """
    return argparse.Namespace(
        run=script,
        run_mode=mode,
        # main()'s special-mode dispatch reads these — keep them False/None.
        help=False,
        version=False,
        install=None,
        uninstall=None,
        force=False,
    )


# ---- record separator decoding --------------------------------------------


def decode_record_sep(s: str) -> bytes:
    """Translate the ``--record-sep`` flag value into raw bytes.

    ``'nl'`` → ``b'\\n'``; ``'null'`` → ``b'\\0'``; anything else is a
    literal sequence (UTF-8 encoded). The parsers in #4 already accept
    arbitrary byte separators.
    """
    if s == 'nl':
        return b'\n'
    if s == 'null':
        return b'\0'
    return s.encode('utf-8')


# ---- TUI_* env-var helpers ------------------------------------------------
#
# Action templates run as ``bash -c CMD`` with a curated environment.
# Standard fields export under ``TUI_<UPPERCASE>``; arbitrary recipe-set
# attributes follow the same rule when they're valid identifiers, except
# the four reserved names below — those are owned by the dispatcher and
# must never be clobbered by an item.

_RESERVED_TUI_NAMES = {
    'TUI_BIN', 'TUI_IDS_FILE', 'TUI_IDS_COUNT', 'TUI_TARGETS',
}

_IDENT = _re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

_STANDARD_FIELDS = ('id', 'title', 'tag', 'tag_style', 'has_children')


def item_env(item, *, ids_file=None, ids_count=0, targets='cursor', bin_path=None):
    """Build the env-var dict for running an action's bash CMD.

    Inherits ``os.environ``, then overlays ``TUI_<FIELD>`` for each of the
    standard ``Item`` fields plus any extra attributes whose names are
    valid Python identifiers. Reserved ``TUI_*`` slots (``TUI_BIN``,
    ``TUI_IDS_FILE``, ``TUI_IDS_COUNT``, ``TUI_TARGETS``) are *only* set
    by this function, never by an item attribute — a recipe that defines
    ``Item.bin = '...'`` will not leak into ``$TUI_BIN``.
    """
    env = dict(os.environ)
    # Standard fields first.
    for fname in _STANDARD_FIELDS:
        v = getattr(item, fname, '')
        if isinstance(v, bool):
            v = '1' if v else '0'
        env[f'TUI_{fname.upper()}'] = '' if v is None else str(v)
    # Arbitrary attributes (recipe-set extras on the Item).
    for name in dir(item):
        if name.startswith('_'):
            continue
        if not _IDENT.match(name):
            continue
        if name in _STANDARD_FIELDS:
            continue
        env_name = f'TUI_{name.upper()}'
        if env_name in _RESERVED_TUI_NAMES:
            # Reserved names are dispatcher-owned; never let an item
            # attribute (e.g. ``bin``, ``ids_file``) clobber them.
            continue
        try:
            v = getattr(item, name)
        except Exception:
            continue
        if callable(v) or isinstance(v, type):
            continue
        if isinstance(v, bool):
            v = '1' if v else '0'
        env[env_name] = '' if v is None else str(v)
    # Reserved fields — written last so item attrs cannot override them.
    if bin_path is not None:
        env['TUI_BIN'] = bin_path
    if ids_file is not None:
        env['TUI_IDS_FILE'] = ids_file
    env['TUI_IDS_COUNT'] = str(ids_count)
    env['TUI_TARGETS'] = targets
    return env


def write_ids_file(ids):
    """Persist ``ids`` as a NUL-separated file. Caller deletes; returns path.

    The NUL separator is unambiguous for arbitrary id strings (paths,
    titles with newlines, etc.). Action CMDs read it via ``$TUI_IDS_FILE``
    plus e.g. ``xargs -0`` or ``readarray -d ''``.
    """
    fd, path = tempfile.mkstemp(prefix='browse-tui-ids-', suffix='.bin')
    try:
        data = b'\0'.join(str(i).encode('utf-8') for i in ids)
        os.write(fd, data)
    finally:
        os.close(fd)
    return path


def run_action_cmd(cmd, item, *,
                   targets='cursor', target_ids=None, bin_path=None,
                   timeout=600.0, stdin=None, stdout=None, stderr=None):
    """Execute ``bash -c CMD`` with TUI_* env vars set. Returns exit code.

    Creates and cleans up the ``$TUI_IDS_FILE`` temp file; honours the
    per-action ``timeout`` (returning 124 — GNU-timeout convention — on
    expiry).

    ``stdin`` / ``stdout`` / ``stderr`` are passed straight to
    ``subprocess.run``; ``None`` (the default) inherits the parent's fds.
    The caller is responsible for suspending/resuming the terminal around
    this call when running under a real TTY, and for handing the terminal
    fds in so an interactive action paints to the terminal rather than a
    captured ``stdout`` (see :func:`make_cli_action`).
    """
    target_ids = target_ids or []
    ids_path = write_ids_file(target_ids) if target_ids else None
    try:
        env = item_env(item,
                       ids_file=ids_path,
                       ids_count=len(target_ids),
                       targets=targets,
                       bin_path=bin_path)
        try:
            result = subprocess.run(
                ['/bin/bash', '-c', cmd],
                env=env,
                timeout=timeout,
                stdin=stdin, stdout=stdout, stderr=stderr,
            )
            return result.returncode
        except subprocess.TimeoutExpired:
            return 124  # GNU timeout convention.
    finally:
        if ids_path is not None:
            try:
                os.unlink(ids_path)
            except OSError:
                pass


# ---- --action 'KEY:LABEL:CMD' parsing -------------------------------------


def parse_action_spec(spec: str) -> tuple:
    """Parse ``'KEY:LABEL:CMD'`` into ``(key, label, cmd)``.

    Splits on the first two colons only — the CMD may itself contain
    colons freely (paths, sed expressions, URLs, …). LABEL may be empty
    (``'k::echo hi'`` is fine). Raises ``ValueError`` if the spec has
    fewer than two colons.
    """
    parts = spec.split(':', 2)
    if len(parts) < 3:
        raise ValueError(f'--action {spec!r}: expected KEY:LABEL:CMD')
    return parts[0], parts[1], parts[2]


def make_cli_action(spec: str, *, bin_path=None,
                    timeout: float = 600.0) -> 'Action':
    """Build an ``Action`` whose handler runs the CLI-supplied bash CMD.

    Action runs only when ``ctx.targets`` is non-empty (gate
    ``'targets'``). The handler suspends the terminal in non-headless
    mode and hands the terminal fds to the child (so an interactive
    action -- an editor/pager -- paints to the terminal, not a captured
    ``stdout``, without touching the parent's fd 0/1), calls
    ``run_action_cmd`` (which manages the temp ids file), then resumes
    and triggers a full redraw. Non-zero exit codes are surfaced via
    ``ctx.error``.

    ``Action`` is referenced by name — the test harness injects it (the
    concatenated build resolves it from earlier modules).
    """
    key, label, cmd = parse_action_spec(spec)

    def _handler(ctx):
        primary = ctx.cursor or (ctx.selected[0] if ctx.selected else None)
        if primary is None:
            ctx.error(f'action {key!r}: no target')
            return
        target_ids = [t.id for t in ctx.targets]
        targets_label = 'selection' if ctx.selected else 'cursor'
        child_fds = {}
        if not ctx._browser._headless:
            term_suspend()
            in_fd, out_fd = term_child_fds()
            child_fds = {'stdin': in_fd, 'stdout': out_fd, 'stderr': out_fd}
        try:
            rc = run_action_cmd(cmd, primary,
                                targets=targets_label,
                                target_ids=target_ids,
                                bin_path=bin_path,
                                timeout=timeout,
                                **child_fds)
        finally:
            if not ctx._browser._headless:
                term_resume()
                ctx._browser._needs_redraw.add('all')
        ctx.refresh()
        if rc != 0:
            ctx.error(f'action {key!r} exited with code {rc}')

    return Action(key=key, label=label, handler=_handler, requires='targets')


# ---- help-screen prose: --help-intro / --help-outro ----------------------
#
# Each flag accepts either a literal string or ``@PATH`` (where PATH is
# read from disk). ``@@`` escapes a literal leading ``@`` so prose that
# really wants to start with one isn't accidentally treated as a path.
# File-not-found is fatal — the user passed an explicit path, surfacing
# the error early is much friendlier than silently treating it as text.


def _resolve_help_text(value: str) -> str:
    """Resolve a ``--help-intro`` / ``--help-outro`` flag value.

    * ``'@@foo'`` → literal ``'@foo'`` (escape).
    * ``'@PATH'`` → ``open(PATH).read()``; ``SystemExit`` on failure.
    * anything else → returned verbatim.
    """
    if value.startswith('@@'):
        return value[1:]   # @@foo -> @foo
    if value.startswith('@'):
        path = value[1:]
        try:
            with open(path, encoding='utf-8') as f:
                return f.read()
        except OSError as e:
            raise SystemExit(
                f'error: could not read help text from {path!r}: {e}'
            )
    return value


def _build_browser_for_help(args) -> 'Browser':
    """Build a transient headless Browser populated from CLI ``args``.

    Used by ``--help`` so the composed help text reflects whatever
    ``--action`` / ``--help-intro`` / ``--help-outro`` flags were on
    the command line, *without* actually launching the TUI. Recipes
    that ship custom actions get a tailored ``--help`` for free.
    """
    intro = _resolve_help_text(args.help_intro) if args.help_intro else None
    outro = _resolve_help_text(args.help_outro) if args.help_outro else None
    bin_path = os.path.abspath(sys.argv[0])
    actions = []
    for spec in args.action:
        try:
            actions.append(make_cli_action(
                spec, bin_path=bin_path, timeout=args.action_timeout,
            ))
        except ValueError:
            # The TUI path will reject malformed specs with a clear
            # error; for --help we keep going so the rest of the help
            # still renders even if a spec is busted.
            continue
    return Browser(BrowserConfig(
        title=args.title,
        actions=actions,
        help_intro=intro,
        help_outro=outro,
        show_ids=args.show_ids,
        show_scope_crumb=args.show_scope_crumb,
        _headless=True,
    ))


# ---- --install / --uninstall ----------------------------------------------
#
# Four targets, mapping to four common installation paths. ``env`` requires
# an active virtualenv (``$VIRTUAL_ENV``); ``system`` requires root and
# emits a sudo hint when run unprivileged. ``local`` writes alongside the
# CWD (handy for testing); ``user`` lands in ``~/.local/bin``.


def _install_path(target):
    """Resolve install ``target`` (local|user|system|env) to an absolute path."""
    if target == 'local':
        return os.path.abspath('./browse-tui')
    if target == 'user':
        return os.path.expanduser('~/.local/bin/browse-tui')
    if target == 'system':
        return '/usr/local/bin/browse-tui'
    if target == 'env':
        venv = os.environ.get('VIRTUAL_ENV')
        if not venv:
            raise SystemExit('--install env: $VIRTUAL_ENV is not set')
        return os.path.join(venv, 'bin', 'browse-tui')
    raise SystemExit(f'unknown install target: {target!r}')


def cmd_install(target: str, force: bool = False) -> int:
    """Install (copy) the running binary to the target path.

    Returns 0 on success or no-op (already-installed identical binary),
    2 if the destination exists with different content and ``--force``
    wasn't passed, 3 if the target requires privileges we don't have
    (with a sudo hint printed). The running binary is read from
    ``sys.argv[0]``.
    """
    src = os.path.abspath(sys.argv[0])
    dst = _install_path(target)
    parent = os.path.dirname(dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(dst):
        try:
            with open(src, 'rb') as a, open(dst, 'rb') as b:
                same = a.read() == b.read()
        except OSError:
            same = False
        if same:
            print(f'browse-tui already installed at {dst} (identical)')
            return 0
        if not force:
            print(f'{dst} exists and differs; pass --force to overwrite')
            return 2
    if target == 'system' and os.geteuid() != 0:
        # Don't escalate privileges silently — print sudo hint instead.
        print(f'system target requires root. Run:\n'
              f'  sudo cp {src} {dst}\n  sudo chmod 755 {dst}')
        return 3
    shutil.copy2(src, dst)
    os.chmod(dst, 0o755)
    print(f'installed: {dst}')
    print(f'run with: {os.path.basename(dst)} --help')
    return 0


def cmd_uninstall(target: str) -> int:
    """Remove the installed binary at ``target``. Returns exit code."""
    dst = _install_path(target)
    if not os.path.exists(dst):
        print(f'browse-tui not present at {dst}')
        return 0
    if target == 'system' and os.geteuid() != 0:
        print(f'system target requires root. Run:\n  sudo rm {dst}')
        return 3
    os.unlink(dst)
    print(f'uninstalled: {dst}')
    return 0


# ---- recipe runners (--run / --run-py / --run-cli) -----------------------


_PY_SHEBANG_RE = _re.compile(r'\bpython\d*\b')


def _detect_recipe_mode(script: str) -> str:
    """Auto-detect mode for ``--run SCRIPT``: returns 'py', 'cli', or 'error'.

    A ``python`` word in the shebang (matched at word boundaries to skip
    false positives like ``/opt/cpython/...``) wins regardless of the
    executable bit — Python recipes work in-process via ``runpy`` and
    don't need ``+x``. Otherwise the file must be executable for the
    exec path. Anything else is an error the caller surfaces with a
    helpful message.
    """
    try:
        with open(script, 'rb') as f:
            first = f.readline(256)
    except OSError:
        return 'error'
    if first.startswith(b'#!'):
        try:
            line = first.decode('utf-8', errors='replace')
        except Exception:
            line = ''
        if _PY_SHEBANG_RE.search(line):
            return 'py'
    if os.access(script, os.X_OK):
        return 'cli'
    return 'error'


def cmd_run_py(script: str, extras: list, *, version: Optional[str] = None) -> int:
    """Run a Python recipe with the running binary self-injected as ``browse_tui``.

    The recipe imports ``from browse_tui import Browser, Item, Action`` and
    gets back the running interpreter's globals — same module that
    backed the concatenated build. ``sys.argv`` is rewritten to
    ``[script, *extras]`` so the recipe sees its own argv. ``SystemExit``
    raised by the recipe is converted to the matching return code.
    """
    import runpy
    sys.modules['browse_tui'] = sys.modules[__name__]
    sys.argv = [script] + list(extras)
    try:
        runpy.run_path(script, run_name='__main__')
    except SystemExit as e:
        return int(e.code) if e.code is not None else 0
    return 0


def cmd_run_cli(script: str, extras: list, *, version: Optional[str] = None) -> int:
    """Exec a non-Python recipe with the binary's dir on PATH.

    Exports ``TUI_BIN`` to the absolute path of the running binary and
    prepends its containing directory to ``PATH`` so a recipe that
    invokes ``browse-tui`` by name resolves to *this* build (handy
    when running from a build tree without installing). Replaces the
    process via ``os.execvpe``; if the file is missing or not
    executable we emit a clear error and return 2 instead of letting
    a raw OSError surface.
    """
    if not os.path.exists(script):
        sys.stderr.write(f'browse-tui: recipe not found: {script}\n')
        return 2
    if not os.access(script, os.X_OK):
        sys.stderr.write(
            f'browse-tui: {script}: not executable. '
            f"Run 'chmod +x {script}', or pass --run-py if it's Python.\n"
        )
        return 2

    # Resolve the absolute path of the running binary (sys.argv[0]) so
    # the child can find us even if invoked via a relative path or
    # symlink. realpath collapses symlinks; a missing argv[0] (e.g.
    # under runpy) leaves both knobs untouched.
    env = dict(os.environ)
    own = sys.argv[0] if sys.argv else None
    if own:
        own_real = os.path.realpath(own)
        env['TUI_BIN'] = own_real
        own_dir = os.path.dirname(own_real)
        if own_dir:
            existing = env.get('PATH', '')
            env['PATH'] = own_dir + os.pathsep + existing if existing else own_dir

    abs_script = os.path.abspath(script)
    try:
        os.execvpe(abs_script, [abs_script, *extras], env)
    except OSError as e:
        sys.stderr.write(f'browse-tui: cannot exec {script}: {e}\n')
        return 2


def cmd_run(script: str, mode: str, extras: list,
            *, version: Optional[str] = None) -> int:
    """Dispatch a recipe by mode ('auto', 'py', 'cli')."""
    if mode == 'py':
        if not os.path.exists(script):
            sys.stderr.write(f'browse-tui: recipe not found: {script}\n')
            return 2
        return cmd_run_py(script, extras, version=version)
    if mode == 'cli':
        return cmd_run_cli(script, extras, version=version)
    # auto
    if not os.path.exists(script):
        sys.stderr.write(f'browse-tui: recipe not found: {script}\n')
        return 2
    detected = _detect_recipe_mode(script)
    if detected == 'py':
        return cmd_run_py(script, extras, version=version)
    if detected == 'cli':
        return cmd_run_cli(script, extras, version=version)
    sys.stderr.write(
        f'browse-tui: cannot run {script}: not executable and shebang '
        f"doesn't name python. Run 'chmod +x {script}', or pass "
        f'--run-py / --run-cli explicitly.\n'
    )
    return 2


# ---- TUI mode -------------------------------------------------------------


def _resolve_list_size(spec, default=0.30):
    """Resolve a ``--list-size`` spec to a list-pane ratio (float in (0, 1)).

    Accepted forms:
      * ``None`` / empty — return ``default``.
      * ``"N%"`` — percentage form; ``ratio = N / 100``.
      * ``"N"`` — absolute line count; ``ratio = N / startup_rows``
        using the current terminal height. The ratio (not the line
        count) is what persists, so a later resize scales the list
        proportionally — pass a percentage to lock the proportion.

    Invalid input falls back to ``default`` and writes a warning to
    stderr so the user knows their flag was ignored. Headless contexts
    where ``os.get_terminal_size`` fails on absolute-line input also
    fall back to ``default`` (the user can retune at runtime with
    -/=).
    """
    if not spec:
        return default
    s = str(spec).strip()
    if s.endswith('%'):
        try:
            pct = float(s[:-1])
        except ValueError:
            sys.stderr.write(
                f'warning: --list-size {spec!r}: invalid percentage; '
                f'using default\n'
            )
            return default
        return max(0.001, min(0.999, pct / 100.0))
    try:
        lines = int(s)
    except ValueError:
        sys.stderr.write(
            f'warning: --list-size {spec!r}: not an integer or N%; '
            f'using default\n'
        )
        return default
    if lines < 1:
        sys.stderr.write(
            f'warning: --list-size {spec!r}: must be at least 1; '
            f'using default\n'
        )
        return default
    try:
        rows = os.get_terminal_size().lines
    except OSError:
        # Headless / piped — can't resolve absolute lines; fall back.
        return default
    if rows < 2:
        return default
    return max(0.001, min(0.999, lines / float(rows)))


# ---- --split-type resolution ---------------------------------------------
#
# The CLI flag accepts long-forms (``horizontal``, ``vertical``, ``mixed``,
# ``preview-children``, ``auto``) and their short codes (``h``, ``v``,
# ``m``, ``pc``, ``a``). ``auto`` is resolved at startup by terminal width:
# wide terminals (>=230 cols) get the vertical side-by-side layout, narrow
# ones get the historic horizontal stack. Resolution is one-shot; later
# resizes do NOT re-pick — the user can switch interactively.

_SPLIT_ALIASES = {
    'h': 'h', 'horizontal': 'h',
    'v': 'v', 'vertical': 'v',
    'm': 'm', 'mixed': 'm',
    'pc': 'pc', 'preview-children': 'pc',
    'a': 'a', 'auto': 'a',
}


def _terminal_cols_for_auto(tty_path=None, default=80):
    """Best-effort terminal width for ``--split-type=auto`` resolution.

    Runs *before* ``term_init`` (auto must be resolved before the
    terminal device is opened), so it probes the same device ``--tty``
    will resolve to (§3.5) rather than guessing from the std streams.
    ``tty_path`` is the resolved ``--tty`` value: ``None`` / ``/dev/tty``
    / a device path mean "probe that terminal device"; the sentinel
    ``'-'`` means "the UI rides on the std streams, so probe fd 0/1".

    Order of attempts (each is checked for ``cols > 0`` before being
    accepted — a zero return from the OS is as good as a failure):

    1. The resolved terminal device via ``TIOCGWINSZ`` — for the
       ``/dev/tty`` default (or an explicit path) this works even when
       stdin AND stdout are pipes (e.g.
       ``printf … | browse-tui --root-cmd - | cat``), because the
       terminal is independent of the std streams. For ``--tty -`` the
       device *is* the std streams, so this probes fd 0 then fd 1.
    2. ``shutil.get_terminal_size()`` — honours an explicit
       ``$COLUMNS`` / ``$LINES`` override and runs its own fallback
       chain.
    3. ``stty size`` via subprocess against the resolved device —
       last-resort external probe; some restricted environments expose
       tty info via stty even when ioctl is filtered.
    4. ``default`` (80) — last resort. Auto then resolves to
       horizontal, which is the safer choice for a narrow display.

    Deliberately does *not* fall back to probing the std fds in the
    ``/dev/tty`` / path modes: the UI device is a deliberate choice, not
    "whichever std fd happens to be a tty" (§1.4). When ``/dev/tty`` is
    unopenable there is no terminal, and ``term_init`` will surface a
    clean error moments later.

    Set ``BROWSE_TUI_DEBUG_AUTO=1`` in the environment to print a
    one-line trace of which probes succeeded / failed (and the value
    finally chosen) to stderr — handy when diagnosing a wrong split
    pick on a user's terminal.

    Returning the default when no source agrees is acceptable: the
    user can override with ``--split-type=v`` (or cycle with ``\\``)
    at runtime.
    """
    std_streams = tty_path == '-'
    device = '/dev/tty' if tty_path in (None, '-') else tty_path
    debug = os.environ.get('BROWSE_TUI_DEBUG_AUTO') == '1'
    trace = [] if debug else None

    def _record(source, value, err=None):
        if debug:
            if err is not None:
                trace.append(f'{source}=ERR({type(err).__name__}:{err})')
            else:
                trace.append(f'{source}={value}')

    def _emit(cols):
        if debug:
            sys.stderr.write(f'[browse-tui auto] {" ".join(trace)} -> {cols}\n')
        return cols

    # 1. The resolved terminal device. In --tty - mode the device is the
    # std streams, so query fd 0 then fd 1 directly; otherwise open the
    # device path (/dev/tty or an explicit --tty) and ask via ioctl.
    if std_streams:
        for fd, name in ((0, 'stdin'), (1, 'stdout')):
            try:
                size = os.get_terminal_size(fd)
                _record(f'os_termsize_{name}', size.columns)
                if size.columns > 0:
                    return _emit(size.columns)
            except OSError as e:
                _record(f'os_termsize_{name}', None, err=e)
    else:
        try:
            import fcntl
            import struct
            import termios
            with open(device, 'rb') as f:
                buf = fcntl.ioctl(f.fileno(), termios.TIOCGWINSZ, b'\0' * 8)
                _rows, cols, _, _ = struct.unpack('HHHH', buf)
                _record('tty_ioctl', cols)
                if cols > 0:
                    return _emit(cols)
        except Exception as e:
            _record('tty_ioctl', None, err=e)

    # 2. shutil.get_terminal_size — honours $COLUMNS, has its own
    # fallback chain. Returns the fallback (default 80,24) rather
    # than raising, so accept its answer only if it looks plausible.
    try:
        size = shutil.get_terminal_size()
        _record('shutil_termsize', size.columns)
        if size.columns > 0:
            return _emit(size.columns)
    except Exception as e:
        _record('shutil_termsize', None, err=e)

    # 3. stty size — external probe against the resolved device.
    # Spawning a subprocess is expensive, but this only runs once at
    # startup and only when the cheaper probes have all failed.
    try:
        with open(device, 'rb') as tty_in:
            proc = subprocess.run(
                ['stty', 'size'],
                stdin=tty_in,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
        if proc.returncode == 0:
            parts = proc.stdout.strip().split()
            if len(parts) == 2 and parts[1].isdigit():
                cols = int(parts[1])
                _record('stty_size', cols)
                if cols > 0:
                    return _emit(cols)
        else:
            _record('stty_size', None, err=RuntimeError(
                f'rc={proc.returncode}'))
    except Exception as e:
        _record('stty_size', None, err=e)

    if debug:
        sys.stderr.write(
            f'[browse-tui auto] {" ".join(trace)} -> default={default}\n'
        )
    return default


def _resolve_split_type(spec, term_cols):
    """Resolve ``--split-type`` ``spec`` to one of ``'h'|'v'|'m'|'pc'``.

    Accepts long-forms (``horizontal``/``vertical``/``mixed``/
    ``preview-children``/``auto``) and short codes (``h``/``v``/``m``/
    ``pc``/``a``); case-insensitive. ``None`` is treated as ``auto``.
    Anything else raises ``ValueError``.

    ``auto`` resolves to ``'v'`` when ``term_cols >= 230`` (wide enough
    for a comfortable side-by-side layout), else ``'h'``.
    """
    if spec is None:
        spec = 'auto'
    if not isinstance(spec, str):
        raise ValueError(f'invalid --split-type: {spec!r}')
    short = _SPLIT_ALIASES.get(spec.lower())
    if short is None:
        raise ValueError(f'invalid --split-type: {spec!r}')
    if short == 'a':
        return 'v' if term_cols >= 230 else 'h'
    return short


def _make_preview_fetcher(preview_cmd, timeout):
    """Return a ``get_preview(item_id)`` closure for ``--preview-cmd``, or None.

    Each invocation runs ``/bin/bash -c <preview_cmd>`` with
    ``TUI_ID=<item_id>`` in the environment and returns stdout as text.
    Errors and timeouts surface as inline ``[error] ...`` strings rather
    than raising, so a flaky preview doesn't crash the UI.

    Used by both the eager (``--root-cmd``) and lazy (``--children-cmd``)
    browser builders so ``--preview-cmd`` works regardless of how the
    children were sourced.
    """
    if not preview_cmd:
        return None

    def _get_preview(item_id):
        env = {**os.environ, 'TUI_ID': str(item_id) if item_id is not None else ''}
        try:
            proc = subprocess.run(
                ['/bin/bash', '-c', preview_cmd],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except Exception as e:
            return f'[error] {type(e).__name__}: {e}'
        return proc.stdout.decode('utf-8', errors='replace')
    return _get_preview


def _build_lazy_browser(args, fields, record_sep, *, split='h'):
    """Build a Browser whose children/preview lookups shell out lazily.

    Used when ``--children-cmd`` is set. Each ``get_children`` call runs
    the command with ``$TUI_ID`` set; ``--preview-cmd`` (if any) drives
    the preview pane the same way. Errors and non-zero exits map to an
    empty list / inline error string so a flaky child doesn't crash the
    UI.
    """
    children_cmd = args.children_cmd
    timeout = args.action_timeout
    fmt = args.input

    def get_children(parent_id, *, reload=False):
        env = {**os.environ, 'TUI_ID': str(parent_id) if parent_id is not None else ''}
        try:
            proc = subprocess.run(
                ['/bin/bash', '-c', children_cmd],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except Exception:
            return []
        if proc.returncode != 0:
            return []
        return list(parse_input(
            proc.stdout, fmt=fmt, fields=fields, record_sep=record_sep,
        ))

    get_preview = _make_preview_fetcher(args.preview_cmd, timeout)

    return Browser(BrowserConfig(
        title=args.title,
        get_children=get_children,
        get_preview=get_preview,
        root_id=args.root_id,
        initial_scope=args.initial_scope,
        show_preview=args.preview,
        show_children_pane=args.children_pane,
        preview_ansi=args.preview_ansi,
        list_ratio=_resolve_list_size(args.list_size),
        multi_select=args.multi_select,
        on_enter=args.on_enter,
        print_format=args.print_format,
        help_intro=_resolve_help_text(args.help_intro) if args.help_intro else None,
        help_outro=_resolve_help_text(args.help_outro) if args.help_outro else None,
        show_ids=args.show_ids,
        show_scope_crumb=args.show_scope_crumb,
        split=split,
    ))


def _build_eager_browser(args, fields, record_sep, *, split='h'):
    """Build a Browser whose root data was produced eagerly by ``--root-cmd``.

    The canonical ``--root-cmd -`` reads stdin verbatim (so a pipe like
    ``printf 'a\\nb\\nc\\n' | browse-tui --root-cmd -`` works without
    spawning anything); bare ``--root-cmd cat`` is kept as an alias for
    ``-`` (exactly ``cat`` — ``--root-cmd 'cat file'`` still runs as a
    command). Any other value runs the command via bash and consumes its
    stdout. The parsed rows feed ``Browser.from_flat_tree`` — hierarchy
    detection (parent / depth / flat) is handled there.
    """
    if args.root_cmd in ('-', 'cat'):
        data = sys.stdin.buffer.read()
        # The UI reads keys from the terminal device (``--tty``, default
        # /dev/tty), not from sys.stdin — so consuming the piped stdin
        # here (the common case, ``printf '…' | browse-tui --root-cmd
        # -``) leaves fd 0 alone and the keyboard still works. No stdin
        # reopen is needed (and none happens).
    else:
        try:
            proc = subprocess.run(
                ['/bin/bash', '-c', args.root_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=args.action_timeout,
            )
        except subprocess.TimeoutExpired:
            sys.stderr.write(f'error: --root-cmd timed out after {args.action_timeout}s\n')
            return None
        except Exception as e:
            sys.stderr.write(f'error: --root-cmd failed: {type(e).__name__}: {e}\n')
            return None
        if proc.returncode != 0:
            sys.stderr.write(
                f'error: --root-cmd exited with code {proc.returncode}\n'
            )
            return None
        data = proc.stdout

    rows = list(parse_input(
        data, fmt=args.input, fields=fields, record_sep=record_sep,
    ))

    # The user typed --path-sep deliberately; if the rows also carry an
    # explicit parent/depth column, from_flat_tree's precedence silently
    # drops path_sep. Surface that here so the override isn't a mystery.
    # Match the precedence's value-semantics: a null-valued column does
    # NOT override path-split (path_sep still runs), so it must not warn.
    if args.path_sep is not None and any(
        (r.get('parent') is not None or r.get('depth') is not None)
        for r in rows if isinstance(r, dict)
    ):
        sys.stderr.write(
            'browse-tui: --path-sep ignored: rows carry explicit '
            'parent/depth\n'
        )

    return Browser.from_flat_tree(
        rows,
        root_id=args.root_id,
        path_sep=args.path_sep,
        title=args.title,
        initial_scope=args.initial_scope,
        show_preview=args.preview,
        show_children_pane=args.children_pane,
        preview_ansi=args.preview_ansi,
        list_ratio=_resolve_list_size(args.list_size),
        multi_select=args.multi_select,
        on_enter=args.on_enter,
        print_format=args.print_format,
        help_intro=_resolve_help_text(args.help_intro) if args.help_intro else None,
        help_outro=_resolve_help_text(args.help_outro) if args.help_outro else None,
        show_ids=args.show_ids,
        show_scope_crumb=args.show_scope_crumb,
        get_preview=_make_preview_fetcher(args.preview_cmd, args.action_timeout),
        split=split,
    )


def run_tui(args):
    """Build a Browser from CLI args and run the TUI main loop.

    Returns the exit code propagated from ``Browser.run`` (or 2 when the
    arguments are insufficient — exactly one of ``--children-cmd`` and
    ``--root-cmd`` must be given).
    """
    fields = [f.strip() for f in args.fields.split(',') if f.strip()]
    record_sep = decode_record_sep(args.record_sep)

    # Resolve --split-type once at startup. Width is read here (not in
    # the builders) so the auto threshold is evaluated against the
    # actual launching terminal; later resizes don't re-pick. The width
    # is probed from the resolved --tty device (default /dev/tty), the
    # same device the UI will paint to — not from the std streams,
    # which may be pipes carrying content/results.
    tty_path = getattr(args, 'tty', None)
    cols = _terminal_cols_for_auto(tty_path)
    try:
        split = _resolve_split_type(getattr(args, 'split_type', None), cols)
    except ValueError as e:
        sys.stderr.write(f'error: {e}\n')
        return 2
    if os.environ.get('BROWSE_TUI_DEBUG_AUTO') == '1':
        sys.stderr.write(
            f'[browse-tui auto] spec={getattr(args, "split_type", None)!r} '
            f'cols={cols} -> split={split!r}\n'
        )

    # --path-sep synthesizes a tree from the fully-known eager row set;
    # it has no meaning for lazy per-parent listing.
    if args.path_sep is not None and args.children_cmd:
        sys.stderr.write(
            'error: --path-sep requires --root-cmd (eager mode)\n'
        )
        return 2

    if args.children_cmd:
        b = _build_lazy_browser(args, fields, record_sep, split=split)
    elif args.root_cmd:
        b = _build_eager_browser(args, fields, record_sep, split=split)
        if b is None:
            return 2
    else:
        sys.stderr.write(
            'error: --children-cmd or --root-cmd is required\n'
        )
        return 2

    # Wire CLI ``--action`` specs onto the Browser. The bin path lets
    # action CMDs invoke the running binary recursively (``$TUI_BIN``).
    bin_path = os.path.abspath(sys.argv[0])
    for spec in args.action:
        try:
            b.add_action(make_cli_action(
                spec, bin_path=bin_path, timeout=args.action_timeout,
            ))
        except ValueError as e:
            sys.stderr.write(f'error: {e}\n')
            return 2

    # ``Browser.run`` opens the terminal via ``term_init`` (default
    # /dev/tty). When no controlling terminal is available ``term_init``
    # raises a clean ``SystemExit`` (``browse-tui: no controlling
    # terminal; pass --tty - to run over stdin/stdout``) — printed to
    # stderr, non-zero exit, no traceback. We deliberately do NOT wrap
    # this: SystemExit must propagate out of ``main`` untouched.
    return b.run()


# ---- top-level entry point ------------------------------------------------


def main(argv=None) -> int:
    """Top-level dispatcher: parse argv, route to the right mode, return rc."""
    if argv is None:
        argv = sys.argv[1:]

    # ``browse-tui`` with no arguments prints help and exits 0 — there's
    # no useful default action (TUI mode needs a data source), so the
    # friendly thing is to show the user what's available rather than
    # surface argparse's "argument required" error.
    if not argv:
        argv = ['--help']

    args, extras = parse_args(argv)

    # Module-discovery setup: prepend the binary directory (and the
    # main-recipe directory if applicable) to ``sys.path`` so plugins
    # shipped alongside the binary and recipe-local helpers can be
    # imported by short name. Runs unconditionally — applies even
    # when no plugins are specified, since recipes commonly import
    # sibling files from their own directory.
    recipe_path_for_syspath = None
    if getattr(args, 'run', None):
        rm = getattr(args, 'run_mode', None)
        effective_mode_for_syspath = rm
        if rm == 'auto':
            effective_mode_for_syspath = _detect_recipe_mode(args.run)
        if effective_mode_for_syspath == 'py':
            recipe_path_for_syspath = args.run
    _setup_plugin_sys_path(recipe_path_for_syspath)

    # Load plugins ahead of any dispatch. ``--run-cli`` (and ``--run``
    # auto-detected as ``cli``) cannot host plugins — the Python
    # process is replaced by the external recipe — so combining the
    # two is rejected here before any import. See the plugin spec
    # for rationale.
    if getattr(args, 'plugins', None):
        run_mode = getattr(args, 'run_mode', None)
        effective_mode = run_mode
        if run_mode == 'auto':
            effective_mode = _detect_recipe_mode(args.run)
        if effective_mode == 'cli':
            sys.stderr.write(
                'browse-tui: --plugin requires an in-process recipe host '
                '(CLI mode or a Python recipe).\n'
                '            --run-cli (or --run resolved as \'cli\') '
                'replaces the Python process with the recipe, so plugins\n'
                '            imported by the launcher would be discarded.\n'
                '            To use plugins with an external CLI recipe, '
                'pass --plugin to the inner \'browse-tui\' invocation\n'
                '            inside the recipe script.\n'
            )
            return 2
        # Make ``import browse_tui`` resolve to the running module so
        # plugins can pull ``Browser``, ``PluginConfig`` etc. without
        # needing the binary to be on ``sys.path`` as a regular
        # importable package. Mirrors what ``cmd_run_py`` does for
        # Python recipes.
        sys.modules.setdefault('browse_tui', sys.modules[__name__])
        _load_plugins(args.plugins)

    # Recipe mode (--run / --run-py / --run-cli / bare positional) is
    # checked first. parse_args has already enforced "recipe must be
    # the first argument with no other binary flags" — there is no
    # mixing to negotiate here.
    if getattr(args, 'run', None):
        return cmd_run(args.run, args.run_mode, extras, version=__version__)

    if args.help:
        build_argparser().print_help()
        print()
        # Build a transient Browser-like proxy so the composed help
        # reflects whatever keys/intro/outro were on the command line
        # (e.g. ``browse-tui --help -a 'e:Edit:true'`` shows ``Edit``
        # under CUSTOM ACTIONS without spinning up the TUI).
        b = _build_browser_for_help(args)
        text = compose_help_text(b, include_usage=True)
        if text:
            sys.stdout.write(text)
        return 0
    if args.version:
        print(__version__)
        return 0

    # Special modes are mutually exclusive with TUI mode; checked in order
    # of "least-likely-to-also-want-TUI" so a user can't accidentally
    # launch the TUI when they meant to install.
    if args.install:
        return cmd_install(args.install, force=args.force)
    if args.uninstall:
        return cmd_uninstall(args.uninstall)

    return run_tui(args)
