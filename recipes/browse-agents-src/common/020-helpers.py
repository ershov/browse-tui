def _md_voice(text, current_style='0'):
    """Run voice text through md2ansi when colored mode is on.

    ``current_style`` is md2ansi's reset baseline (raw SGR params, no
    ``\x1b[``/``m``): a styled span opens as ``current_style;<sgr>`` and
    resets back to ``current_style`` rather than a hard ``\x1b[0m``. Pass
    a background (e.g. ``BLUE_BG_PARAMS``) to keep that bg behind styled
    voice; plain prose still needs a per-line bg prefix from the caller
    (see ``_band_block``).
    """
    if not _MD_COLOR or _md2ansi_fn is None or not text:
        return text
    try:
        width = (_BROWSER.preview_width if _BROWSER else 0) or 80
        return _md2ansi_fn(text, current_style, line_width=width)
    except Exception:
        return text


# Cap on messages enumerated per session in flat mode (tree mode is
# uncapped since it only lists umbrellas at the top level). 0 means
# "no cap" — the default after we unified flat with tree mode in #475;
# the framework's virtualised list pane handles 30k+ rows fine. The
# ``-n NUMBER`` argv flag still works as an explicit override for
# users who want a safety valve on truly pathological transcripts.
_MESSAGE_LIMIT = 0


# ---- ANSI palette --------------------------------------------------------
#
# Renderers below splice these into f-strings directly. To re-theme,
# change the constants. To turn color off, ``_init_color()`` zeroes them
# (which makes every f'{COLOR}…{RESET}' a no-op).
#
# 256-color codes chosen to read on a dark background; tweak if you live
# on a light terminal. browse-tui's preview pane learned to pass ANSI
# through in #240; older builds will sanitize these to '?'.

RESET = '\x1b[0m'
BOLD  = '\x1b[1m'
DIM   = '\x1b[2m'

RED     = '\x1b[38;5;167m'
GREEN   = '\x1b[38;5;108m'
YELLOW  = '\x1b[38;5;179m'
BLUE    = '\x1b[38;5;110m'
MAGENTA = '\x1b[38;5;176m'
CYAN    = '\x1b[38;5;109m'
GRAY    = '\x1b[38;5;244m'
MUTE    = '\x1b[38;5;240m'
WHITE   = '\x1b[38;5;253m'
THINK   = '\x1b[38;5;103m'

# Dark-blue background (256-index 17, the recipe's existing dark blue —
# see ``_ROW_BG_FOR_KIND['user']``) for the full-width turn-opening
# ``── user ──`` divider AND the prompt body banded beneath it, so
# successive turns pop in a composed preview. ``BLUE_BG_PARAMS`` (the raw
# SGR params) is kept separate so it can be threaded as md2ansi's
# ``current_style`` — voice-span resets then land back on the band
# instead of a hard ``\x1b[0m`` that would punch a hole in it (mirrors
# ``CMD_BG_PARAMS``).
BLUE_BG_PARAMS = '48;5;17'
BLUE_BG = f'\x1b[{BLUE_BG_PARAMS}m'


def _init_color():
    """Disable ANSI when ``NO_COLOR`` is set or stdout isn't a tty.

    ``FORCE_COLOR`` overrides both. Idempotent — once a constant has
    been zeroed it stays zeroed for the process.
    """
    if os.environ.get('FORCE_COLOR'):
        return
    if os.environ.get('NO_COLOR') or not sys.stdout.isatty():
        g = globals()
        for n in ('RESET', 'BOLD', 'DIM',
                  'RED', 'GREEN', 'YELLOW', 'BLUE', 'MAGENTA', 'CYAN',
                  'GRAY', 'MUTE', 'WHITE', 'THINK', 'BLUE_BG'):
            g[n] = ''


def _pop_limit_arg(default):
    """Pop ``-n NUMBER`` (or ``-n=NUMBER``) out of ``sys.argv`` and return it.

    Removed before Browser.run() and the recipe's positional parsing
    see argv. Falls back to ``default`` if absent or non-int.
    """
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a == '-n' and i + 1 < len(sys.argv):
            try:
                n = int(sys.argv[i + 1])
            except ValueError:
                i += 1
                continue
            del sys.argv[i:i + 2]
            return n
        if a.startswith('-n='):
            try:
                n = int(a[3:])
            except ValueError:
                i += 1
                continue
            del sys.argv[i]
            return n
        i += 1
    return default


def _pop_value(name, conv=str):
    """Pop ``--name VALUE`` (or ``--name=VALUE``) and return the value.

    Returns ``None`` if the flag is absent. Returns the sentinel
    ``False`` if the flag is present but the value can't be converted
    via ``conv`` — letting the caller report a useful error.
    """
    eq_prefix = f'{name}='
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a == name and i + 1 < len(sys.argv):
            raw = sys.argv[i + 1]
            del sys.argv[i:i + 2]
            try:
                return conv(raw)
            except (ValueError, TypeError):
                return False
        if a.startswith(eq_prefix):
            raw = a[len(eq_prefix):]
            del sys.argv[i]
            try:
                return conv(raw)
            except (ValueError, TypeError):
                return False
        i += 1
    return None


def _pop_flag(name):
    """Pop the bare flag ``--name`` and return ``True`` if it was present."""
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == name:
            del sys.argv[i]
            return True
        i += 1
    return False


# ---- helpers --------------------------------------------------------------


def _human_count(n):
    """Compact human form of a count: 15000000 → ``15.0M``, 9800 → ``9.8K``."""
    if n >= 1_000_000:
        return f'{n / 1_000_000:.1f}M'
    if n >= 1_000:
        return f'{n / 1_000:.1f}K'
    return str(n)


def _total_tokens_left(a):
    """Token count from a ``total_tokens_reminder`` attachment's text
    (``<total_tokens>N tokens left</total_tokens>``), or ``None``."""
    m = re.search(r'(\d+)\s+tokens left', a.get('text') or '')
    return int(m.group(1)) if m else None


def _human_time(epoch):
    """Compact relative-time formatter (``2m ago``, ``3d ago``, …)."""
    if not epoch:
        return ''
    delta = datetime.datetime.now().timestamp() - epoch
    if delta < 0:
        return 'just now'
    if delta < 60:
        return f'{int(delta)}s ago'
    if delta < 3600:
        return f'{int(delta // 60)}m ago'
    if delta < 86400:
        return f'{int(delta // 3600)}h ago'
    return f'{int(delta // 86400)}d ago'


def _local_ts(iso):
    """Render an ISO-8601 timestamp in the local zone (``date``(1) rules).

    Anything that doesn't parse — or that the conversion can't represent,
    such as a stamp near ``datetime.max`` shifted by a positive offset —
    is returned unchanged, as is a naive stamp (a bare date, say): without
    an offset there's no zone to convert *from*, and guessing one would
    silently shift the value.
    """
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            return iso
        return dt.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')
    except (ValueError, OverflowError, OSError):
        return iso


def _trim(s, n):
    """Strip surrounding whitespace and truncate, preserving inner newlines.

    Replaces the older ``' '.join(s.split())[:n]`` pattern that flattened
    all whitespace. We want multi-line strings (user prompts, agent
    responses, diffs, file contents) to render as multiple lines in the
    session preview rather than one mashed-together blob. Callers that
    must produce a single-line title (the message-list rows) collapse
    further on top of this with ``_oneline``.
    """
    if not isinstance(s, str):
        s = str(s) if s is not None else ''
    return s.strip()[:n]


def _oneline(s):
    """Collapse all whitespace runs to a single space.

    Used for row titles in the message list — the browser renders each
    Item.title on one line, so embedded ``\\n`` would either get
    sanitised or break the row layout.
    """
    return ' '.join(s.split())


def _pid_alive(pid):
    """True iff a process with PID ``pid`` is currently alive.

    ``PermissionError`` (signalling a process owned by another user)
    counts as alive — we can see it via ``kill -0`` semantics.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
