#!/usr/bin/env python3

"""md2ansi_lib2 — single-file, zero-dependency Markdown-to-ANSI library (v2).

A major-version rewrite of md2ansi_lib.py on a two-phase block/inline engine.
See md2ansi_lib2.design.md for architecture, naming conventions, and rule tables.
Lives side by side with v1 until the switchover (§13); the two modules never
import each other. v2 borrows from v1 deliberately (§9): the alternation engine,
the inline rule tuple, the code-highlight grammars, and the SGR/width/wrap
utilities are reused verbatim; the block layer above them is new.
"""

import re
from dataclasses import dataclass, field, replace
from typing import Any


# ### Section: SGR color constants ##########################################

# Bare SGR codes — wrapping in `\x1b[...m` is the dispatcher's job. Borrowed
# verbatim from v1 (design §9, color schema).

# Universal code-token palette.
M2A_COLOR_COMMENT  = "38;5;245"   # gray
M2A_COLOR_STRING   = "38;5;114"   # green
M2A_COLOR_NUMBER   = "38;5;220"   # yellow
M2A_COLOR_KEYWORD  = "38;5;204"   # pink
M2A_COLOR_BUILTIN  = "38;5;147"   # purple
M2A_COLOR_PUNCT    = "38;5;246"   # dim gray — operators/punctuation (one step brighter than COMMENT's 245)

# Markdown styling palette (headings, inline accents, frame chrome).
M2A_COLOR_H1       = "38;5;226"   # yellow
M2A_COLOR_H2       = "38;5;214"   # orange
M2A_COLOR_H3       = "38;5;118"   # green
M2A_COLOR_H4       = "38;5;21"    # blue
M2A_COLOR_H5       = "38;5;93"    # purple
M2A_COLOR_H6       = "38;5;239"   # dim gray
M2A_COLOR_LINK     = "38;5;45;4"  # cyan + underline
M2A_COLOR_DIM      = "38;5;245"   # blockquote bar, image label (same value as COMMENT — different intent)
M2A_COLOR_FRAME    = "38;5;239"   # code-block frame corners (same value as H6 — different intent)
M2A_COLOR_FOOTNOTE = "38;5;226"   # footnote ref + section heading


# ### Section: Dataclasses ##################################################

@dataclass(frozen=True, slots=True)
class M2A_Context:
    compiled: re.Pattern
    rules: tuple


# The single lower cap on line width, applied uniformly at every level INCLUDING
# root (design §6 — v1's nesting-only M2A_MIN_NESTED_WIDTH renamed and promoted):
# `md2ansi_color(line_width=5)` renders at 20. One constant, one rule, no
# per-level exceptions.
M2A_MIN_WIDTH = 20


@dataclass(slots=True)
class M2A_DocumentState:
    line_width: int = 150
    footnotes: dict = field(default_factory=dict)
    footnote_order: list = field(default_factory=list)
    cell_min_width: int = 20
    row_dividers: Any = None
    # The requested wrap width (the caller's `line_width`), or 0 when wrapping is
    # disabled. Drives table fitting, list self-wrapping, prose wrapping, and the
    # width a blockquote narrows its recursion to. Kept distinct from
    # `line_width` so the 150-char fallback used for HR sizing doesn't trigger any
    # of those.
    wrap_width: int = 0
    # The ambient SGR the document renders under. Constant across block recursion
    # (a quote/list prefixes chrome but doesn't change the base style); the inline
    # pass layers emphasis on top of it locally. In v1 this was a function
    # parameter threaded through every handler; v2's block renderers take
    # `(match, state)`, so the base style rides in the state.
    current_style: str = "0"


# ### Section: Shared regex fragments #######################################

# All fragments are designed to be embedded inside re.VERBOSE patterns
# (whitespace ignored outside character classes; `#` is a comment unless
# escaped). Borrowed verbatim from v1 (design §9).

# String literals — linear, no atomic groups needed. Each char has exactly one
# matching branch: a non-quote non-backslash char OR a backslash + any char.
_M2A_STR_DQ  = r' " (?: [^"\\\n] | \\. )* "  '
_M2A_STR_SQ  = r" ' (?: [^'\\\n] | \\. )* '  "
_M2A_STR_BT  = r" ` (?: [^`\\]   | \\. )* `  "

# Triple-quoted strings — tempered-greedy, no escape handling subtlety.
_M2A_STR_TDQ = r' """ (?: (?!""") [\s\S] )* """ '
_M2A_STR_TSQ = r" ''' (?: (?!''') [\s\S] )* ''' "

# Permissive multiline single/double-quoted strings — same shape as the strict
# fragments but WITHOUT the `\n` exclusion, so a string may span linebreaks.
# Used only by the unknown-language context.
_M2A_STR_DQ_ML = r' " (?: [^"\\] | \\. )* "  '
_M2A_STR_SQ_ML = r" ' (?: [^'\\] | \\. )* '  "

# Numbers — hex, binary, octal, int, float, scientific, with `_` digit grouping.
_M2A_NUM = r"""
    \b (?:
        0 [xX] [0-9a-fA-F_]+
      | 0 [bB] [01_]+
      | 0 [oO] [0-7_]+
      | (?: \d [\d_]* )? \. \d [\d_]* (?:[eE][+-]?\d+)?
      | \d [\d_]* (?:[eE][+-]?\d+)?
    ) \b
"""

# Punctuation run — a maximal run of operator/bracket/separator chars, dimmed so
# words read brighter by contrast. Appended LAST in every code context.
_M2A_PUNCT = r"[-+*/%=<>!&|^~.,;:?@(){}\[\]]+"

# Block-start lookahead — substituted into every cross-line inline rule's
# soft-newline branch so inline matching stops at block boundaries.
_M2A_BLOCK_START_AHEAD = r"""
    [ \t]* (?:
        \#{1,6} [ \t]
      | >
      | \|
      | `{3,}
      | ~{3,}
      | [-*+][ \t]
      | \d+\.[ \t]
      | $
    )
"""


# ### Section: Context-building utility #####################################

# The placeholder rewrite covers both group definitions (`<`-form) and
# backreferences (`=`-form); the trailing `>`/`)` is left alone.
_M2A_PLACEHOLDER_RE = re.compile(r"\(\?P(?P<kind>[<=])\*(?P<suffix>\w*)")

# Sentinel meaning "recurse into the same context the rule fired in".
_M2A_RECURSE_SELF = object()


def _m2a_build_context(rules):
    rules = tuple(rules)
    alternatives = []
    for name, pat, _fmt, _recurse in rules:
        def _rewrite(m, _name=name):
            suffix = m.group("suffix") or "inner"
            return f"(?P{m.group('kind')}{_name}_{suffix}"
        rewritten = _M2A_PLACEHOLDER_RE.sub(_rewrite, pat)
        alternatives.append(f"(?P<{name}>{rewritten})")
    combined = "|".join(alternatives) if alternatives else r"(?!)"
    compiled = re.compile(combined, re.VERBOSE | re.MULTILINE | re.DOTALL)
    return M2A_Context(compiled=compiled, rules=rules)


# ### Section: Width, wrap, and styling utilities ###########################

# Borrowed verbatim from v1 (design §9): visible-width measurement, the SGR
# style/reset model, ANSI-aware wrapping, the shared HR run, the deferred line
# sentinels, and the input sanitizer.

_M2A_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _m2a_no_break_zone(line_width):
    return min(20, max(0, line_width - 30))


_M2A_TABLE_CELL_RE = re.compile(
    r"""
    (
        (?:
            \\.
          | `` (?: (?! `` ) [^\n] )* ``
          | ` (?: \\. | [^`\n\\] )* `
          | [^|\\\n]
        )*
    )
    (?: \| | $ )
    """,
    re.VERBOSE,
)


def _m2a_split_table_row(s):
    """Split a markdown table row on un-escaped `|`. Honours `\\|`."""
    s = s.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells = []
    pos = 0
    end = len(s)
    while pos <= end:
        mt = _M2A_TABLE_CELL_RE.match(s, pos)
        if mt is None or mt.end() == pos:
            break
        cells.append(mt.group(1).strip())
        pos = mt.end()
    return cells


def _m2a_visible_len(s):
    """Length of s with ANSI escapes stripped — used for width calculations."""
    return len(_M2A_ANSI_ESCAPE_RE.sub("", s))


def _m2a_align_cell(content, width, align):
    """Pad `content` to `width` columns according to `align`."""
    pad_n = width - _m2a_visible_len(content)
    if pad_n <= 0:
        return content
    if align == "right":
        return " " * pad_n + content
    if align == "center":
        left = pad_n // 2
        return " " * left + content + " " * (pad_n - left)
    return content + " " * pad_n


def _m2a_prefix_lines(text, prefix):
    """Prepend `prefix` to every line in `text`."""
    return "\n".join(prefix + ln for ln in text.split("\n"))


# Three single-char sentinels carry deferred-layout semantics between the inline
# pass and the leaf renderer that realizes them (design §6). Unlike v1 there is
# NO `\x00` opaque marker and no global post-render pass — every renderer returns
# final text, so these never survive past the level that emits them. None appear
# in real input: the sanitizer maps any stray copy in the SOURCE to U+FFFD.
_M2A_LINEBREAK = "\x01"  # hard line break (`<br>`, LF/CR entity) → real `\n`
_M2A_RULE = "\x02"       # horizontal rule (`<hr>` as content) → `─`-run, container-sized
_M2A_NBSP = "\x03"       # non-breaking space (`&nbsp;`, U+00A0 entity) → `" "`

# Input sanitizer kill class: every C0 control codepoint EXCEPT `\t` (09), `\n`
# (0A), and ESC `\x1b` (1B). `\r` (0D) is absent (CR is normalized to `\n`
# first). Mapping these to U+FFFD neutralizes any stray sentinel in the source.
_M2A_C0_KILL = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1A\x1C-\x1F]")


def _m2a_split_sentinel_lines(text):
    """Yield `("text", seg)` / `("rule", None)` tokens for the deferred line
    sentinels — the single source of truth for how `<br>` (`\\x01`) and `<hr>`
    (`\\x02`) split a string into lines.
    """
    if _M2A_LINEBREAK not in text and _M2A_RULE not in text:
        yield ("text", text)
        return
    for piece in text.split(_M2A_LINEBREAK):
        segments = piece.split(_M2A_RULE)
        for s_idx, seg in enumerate(segments):
            if s_idx > 0:
                yield ("rule", None)
            if not seg and len(segments) > 1:
                continue
            yield ("text", seg)


def _m2a_rule(width):
    """A horizontal-rule run of `width` columns, floored at 1."""
    return "─" * max(1, width)


def _m2a_inject_color(text, style, reset=None):
    """Wrap `text` in SGR codes so every line carries its own color setup."""
    open_sgr = f"\x1b[{style}m"
    text_len = len(text)
    def _replace(mt):
        if mt.end() == text_len:
            return mt.group(0)
        return mt.group(0) + open_sgr
    body = re.sub(r"\n+", _replace, text)
    out = open_sgr + body
    if reset is not None:
        out += f"\x1b[{reset}m"
    return out


def _m2a_styled(text, current_style, sgr):
    """Wrap `text` with SGR `sgr` layered on top of `current_style`, then reset back."""
    return _m2a_inject_color(text, f"{current_style};{sgr}", current_style)


def _m2a_wrap_ansi_line(line, line_width, continuation="", reset_sgr=""):
    """Greedy word-wrap over already-styled text: wraps at visible-character
    positions (a small no-break zone at the line start), leaves SGR escape
    sequences intact, and re-emits the last seen SGR at the start of each new
    line so styling active at the break point survives onto the next line.
    """
    if _m2a_visible_len(line) <= line_width:
        return [line + reset_sgr]
    threshold = _m2a_no_break_zone(line_width)
    tokens = re.findall(r"\x1b\[[0-9;]*m|\s+|[^\s\x1b]+", line)

    lines_out = []
    current = []
    current_vlen = 0
    pending = []
    pending_vlen = 0
    last_sgr = ""

    for tok in tokens:
        if tok.startswith("\x1b["):
            last_sgr = tok
            pending.append(tok)
            continue
        if tok[0].isspace():
            pending.append(tok)
            pending_vlen += len(tok)
            continue
        attempt_vlen = current_vlen + pending_vlen + len(tok)
        if attempt_vlen <= line_width or current_vlen < threshold or current_vlen == 0:
            current.extend(pending)
            current.append(tok)
            current_vlen = attempt_vlen
        else:
            lines_out.append("".join(current) + reset_sgr)
            current = [continuation]
            if last_sgr:
                current.append(last_sgr)
            current.append(tok)
            current_vlen = len(continuation) + len(tok)
        pending = []
        pending_vlen = 0

    current.extend(pending)
    lines_out.append("".join(current) + reset_sgr)
    return lines_out


# ### Section: Inline handlers ##############################################

# Borrowed verbatim from v1 (design §9): the inline rule tuple's callable
# formatters. None emit the opaque marker (only v1's block handlers did), so
# they carry into v2 unchanged and are safe to run inside the inline dispatcher.

# `\`` → bare backtick inside a single-backtick code span.
_M2A_INLINE_CODE_UNESCAPE = re.compile(r"\\(`)")


def _m2a_fmt_inline_code(m, name, current_style, context, state):
    text = m.group(f"{name}_inner")
    if name == "code_inline":
        text = _M2A_INLINE_CODE_UNESCAPE.sub(r"\1", text)
    return _m2a_styled(text, current_style, M2A_COLOR_STRING)


def _m2a_fmt_escape(m, name, current_style, context, state):
    return m.group(f"{name}_char")


def _m2a_fmt_comment(m, name, current_style, context, state):
    # HTML comment `<!-- … -->` → dropped (no output).
    return ""


def _m2a_fmt_br(m, name, current_style, context, state):
    # `<br>` → the line-break sentinel, realized by the enclosing leaf renderer.
    return _M2A_LINEBREAK


def _m2a_fmt_hr_inline(m, name, current_style, context, state):
    # `<hr>` as inline content → the rule sentinel, sized by the enclosing leaf.
    return _M2A_RULE


# Seed set of named HTML entities → their SINGLE Unicode char (design §5.1
# carried from v1). Numeric entities cover everything else.
_M2A_HTML_ENTITIES = {
    "amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'",
    "nbsp": " ", "copy": "©", "reg": "®", "trade": "™",
    "mdash": "—", "ndash": "–", "hellip": "…", "bull": "•",
    "middot": "·", "sect": "§", "para": "¶", "deg": "°",
    "times": "×", "divide": "÷", "laquo": "«", "raquo": "»",
    "larr": "←", "rarr": "→", "uarr": "↑", "darr": "↓",
    "pound": "£", "euro": "€", "cent": "¢", "yen": "¥",
}


def _m2a_entity_char(cp):
    """Map a resolved entity codepoint to its rendered char, applying the same
    control-codepoint routing for the named and numeric paths."""
    if cp == 0 or 0xD800 <= cp <= 0xDFFF or cp > 0x10FFFF:
        return "�"
    if cp == 0x0A or cp == 0x0D:
        return _M2A_LINEBREAK
    if cp == 0xA0:
        return _M2A_NBSP
    if cp < 0x20 or cp == 0x7F or 0x80 <= cp <= 0x9F:
        return "�"
    return chr(cp)


def _m2a_fmt_entity(m, name, current_style, context, state):
    body = m.group(f"{name}_body")
    if body.startswith("#"):
        digits = body[1:]
        cp = int(digits[1:], 16) if digits[0] in "xX" else int(digits)
        return _m2a_entity_char(cp)
    ch = _M2A_HTML_ENTITIES.get(body)
    if ch is None:
        return m.group(0)
    return _m2a_entity_char(ord(ch))


def _m2a_fmt_image(m, name, current_style, context, state):
    alt = m.group(f"{name}_alt") or ""
    return _m2a_styled(f"[IMG: {alt}]", current_style, f"3;{M2A_COLOR_DIM}")


def _m2a_fmt_footnote_ref(m, name, current_style, context, state):
    fid = m.group(f"{name}_id")
    if fid not in state.footnote_order:
        state.footnote_order.append(fid)
    return _m2a_styled(f"[^{fid}]", current_style, M2A_COLOR_FOOTNOTE)


# ### Section: Code-highlight grammars ######################################

# Borrowed verbatim from v1 (design §9): all five code-highlight rule sets and
# their compiled contexts.

_M2A_RULE_PUNCT = ("punct", _M2A_PUNCT, M2A_COLOR_PUNCT, None)

_M2A_PY_KEYWORDS = (
    "False|None|True|and|as|assert|async|await|break|case|class|continue|def|del|"
    "elif|else|except|finally|for|from|global|if|import|in|is|lambda|match|nonlocal|"
    "not|or|pass|raise|return|try|type|while|with|yield"
)
_M2A_PY_BUILTINS = (
    "abs|aiter|all|anext|any|ascii|bin|bool|breakpoint|bytearray|bytes|callable|"
    "chr|classmethod|compile|complex|delattr|dict|dir|divmod|enumerate|eval|exec|"
    "filter|float|format|frozenset|getattr|globals|hasattr|hash|help|hex|id|input|"
    "int|isinstance|issubclass|iter|len|list|locals|map|max|memoryview|min|next|"
    "object|oct|open|ord|pow|print|property|range|repr|reversed|round|set|setattr|"
    "slice|sorted|staticmethod|str|sum|super|tuple|type|vars|zip|__import__"
)

_M2A_PY_STRING = rf"""
    (?: \b [rRbBuUfF]{{1,2}} )?
    (?:
        (?: {_M2A_STR_TDQ} )
      | (?: {_M2A_STR_TSQ} )
      | (?: {_M2A_STR_DQ}  )
      | (?: {_M2A_STR_SQ}  )
    )
"""

_M2A_RULES_CODE_PYTHON = (
    ("py_comment",    r"\#[^\n]*",                                    M2A_COLOR_COMMENT, None),
    ("py_string",     _M2A_PY_STRING,                                 M2A_COLOR_STRING,  None),
    ("py_number",     _M2A_NUM,                                       M2A_COLOR_NUMBER,  None),
    ("py_keyword",    rf"\b(?:{_M2A_PY_KEYWORDS})\b",                 M2A_COLOR_KEYWORD, None),
    ("py_builtin",    rf"\b(?:{_M2A_PY_BUILTINS})\b",                 M2A_COLOR_BUILTIN, None),
    _M2A_RULE_PUNCT,
)

_M2A_SH_KEYWORDS = (
    "if|then|else|elif|fi|case|esac|for|while|until|do|done|in|function|time|"
    "select|break|continue|return|declare|readonly|local|export|set|unset|shift|"
    "exit|trap"
)
_M2A_SH_BUILTINS = (
    "echo|printf|read|cd|pwd|pushd|popd|mkdir|rmdir|rm|cp|mv|ln|ls|cat|grep|sed|"
    "awk|find|test|source|eval|exec|ulimit|umask|wait|kill|sleep"
)

_M2A_RULES_CODE_BASH = (
    ("sh_comment",   r"(?:^|(?<=\s))\#[^\n]*",                       M2A_COLOR_COMMENT, None),
    ("sh_string_dq", _M2A_STR_DQ,                                   M2A_COLOR_STRING,  None),
    ("sh_string_sq", _M2A_STR_SQ,                                   M2A_COLOR_STRING,  None),
    ("sh_number",    _M2A_NUM,                                      M2A_COLOR_NUMBER,  None),
    ("sh_keyword",   rf"\b(?:{_M2A_SH_KEYWORDS})\b",                M2A_COLOR_KEYWORD, None),
    ("sh_builtin",   rf"\b(?:{_M2A_SH_BUILTINS})\b",                M2A_COLOR_BUILTIN, None),
    _M2A_RULE_PUNCT,
)

_M2A_JS_KEYWORDS = (
    "break|case|catch|class|const|continue|debugger|default|delete|do|else|export|"
    "extends|false|finally|for|function|if|import|in|instanceof|new|null|return|"
    "super|switch|this|throw|true|try|typeof|var|void|while|with|yield|let|static|"
    "await|async|of"
)
_M2A_JS_BUILTINS = (
    "Array|Boolean|Date|Error|Function|JSON|Math|Number|Object|RegExp|String|"
    "Symbol|Map|Set|Promise|console|document|window|fetch|setTimeout|setInterval|"
    "clearTimeout|clearInterval|globalThis|undefined|NaN|Infinity"
)

_M2A_RULES_CODE_JAVASCRIPT = (
    ("js_comment_line",  r"//[^\n]*",                                M2A_COLOR_COMMENT, None),
    ("js_comment_block", r"/\*(?:(?!\*/)[\s\S])*\*/",                M2A_COLOR_COMMENT, None),
    ("js_string_dq",     _M2A_STR_DQ,                                M2A_COLOR_STRING,  None),
    ("js_string_sq",     _M2A_STR_SQ,                                M2A_COLOR_STRING,  None),
    ("js_string_bt",     _M2A_STR_BT,                                M2A_COLOR_STRING,  None),
    ("js_number",        _M2A_NUM,                                   M2A_COLOR_NUMBER,  None),
    ("js_keyword",       rf"\b(?:{_M2A_JS_KEYWORDS})\b",             M2A_COLOR_KEYWORD, None),
    ("js_builtin",       rf"\b(?:{_M2A_JS_BUILTINS})\b",             M2A_COLOR_BUILTIN, None),
    _M2A_RULE_PUNCT,
)

_M2A_C_KEYWORDS = (
    "alignas|alignof|and|and_eq|asm|auto|bitand|bitor|bool|break|case|catch|char|"
    "char8_t|char16_t|char32_t|class|compl|concept|const|consteval|constexpr|"
    "constinit|const_cast|continue|co_await|co_return|co_yield|decltype|default|"
    "delete|double|do|dynamic_cast|else|enum|explicit|export|extern|false|final|"
    "float|for|friend|goto|if|inline|int|long|mutable|namespace|new|noexcept|"
    "not_eq|not|nullptr|operator|or_eq|or|override|private|protected|public|"
    "register|reinterpret_cast|requires|restrict|return|short|signed|sizeof|"
    "static_assert|static_cast|static|struct|switch|template|this|thread_local|"
    "throw|true|try|typedef|typeid|typename|union|unsigned|using|virtual|void|"
    "volatile|wchar_t|while|xor_eq|xor|"
    "_Alignas|_Alignof|_Atomic|_Bool|_Complex|_Generic|_Imaginary|_Noreturn|"
    "_Static_assert|_Thread_local"
)
_M2A_C_BUILTINS = (
    "size_t|ssize_t|ptrdiff_t|intptr_t|uintptr_t|"
    "int8_t|int16_t|int32_t|int64_t|uint8_t|uint16_t|uint32_t|uint64_t|"
    "FILE|NULL|EXIT_SUCCESS|EXIT_FAILURE|stdin|stdout|stderr|"
    "printf|fprintf|snprintf|sprintf|sscanf|scanf|puts|putchar|getchar|fgets|"
    "fputs|fopen|fclose|fread|fwrite|malloc|calloc|realloc|free|memcpy|memmove|"
    "memset|strlen|strncmp|strcmp|strncpy|strcpy|strncat|strcat|strchr|strstr|"
    "exit|abort|assert|"
    "std|string_view|string|wstring|vector|array|unordered_map|map|unordered_set|"
    "set|pair|tuple|optional|variant|list|deque|queue|stack|span|"
    "shared_ptr|unique_ptr|weak_ptr|make_shared|make_unique|move|forward|"
    "cout|cin|cerr|clog|endl"
)

_M2A_RULES_CODE_C = (
    ("c_preproc",       r"^ [ \t]* \# [ \t]* \w+",                  M2A_COLOR_KEYWORD, None),
    ("c_comment_line",  r"//[^\n]*",                                M2A_COLOR_COMMENT, None),
    ("c_comment_block", r"/\*(?:(?!\*/)[\s\S])*\*/",                M2A_COLOR_COMMENT, None),
    ("c_string",        _M2A_STR_DQ,                                M2A_COLOR_STRING,  None),
    ("c_char",          _M2A_STR_SQ,                                M2A_COLOR_STRING,  None),
    ("c_number",        _M2A_NUM,                                   M2A_COLOR_NUMBER,  None),
    ("c_keyword",       rf"\b(?:{_M2A_C_KEYWORDS})\b",              M2A_COLOR_KEYWORD, None),
    ("c_builtin",       rf"\b(?:{_M2A_C_BUILTINS})\b",              M2A_COLOR_BUILTIN, None),
    _M2A_RULE_PUNCT,
)

_M2A_RULES_CODE_UNKNOWN = (
    ("gen_string_dq", _M2A_STR_DQ_ML, M2A_COLOR_STRING, None),
    ("gen_string_sq", _M2A_STR_SQ_ML, M2A_COLOR_STRING, None),
    ("gen_string_bt", _M2A_STR_BT,    M2A_COLOR_STRING, None),
    ("gen_number",    _M2A_NUM,       M2A_COLOR_NUMBER, None),
    _M2A_RULE_PUNCT,
)

# Generic: no rules — passthrough. Reserved for frontmatter (verbatim).
_M2A_RULES_CODE_GENERIC = ()

M2A_CONTEXT_CODE_PYTHON     = _m2a_build_context(_M2A_RULES_CODE_PYTHON)
M2A_CONTEXT_CODE_BASH       = _m2a_build_context(_M2A_RULES_CODE_BASH)
M2A_CONTEXT_CODE_JAVASCRIPT = _m2a_build_context(_M2A_RULES_CODE_JAVASCRIPT)
M2A_CONTEXT_CODE_C          = _m2a_build_context(_M2A_RULES_CODE_C)
M2A_CONTEXT_CODE_UNKNOWN    = _m2a_build_context(_M2A_RULES_CODE_UNKNOWN)
M2A_CONTEXT_CODE_GENERIC    = _m2a_build_context(_M2A_RULES_CODE_GENERIC)


# ### Section: Inline rule table ############################################

# Borrowed verbatim from v1 (design §9): the inline pattern fragments and the
# inline rule tuple. This is the alternation engine that runs inside every leaf
# (prose lines, heading titles, table cells, list-item content).

_BSA = _M2A_BLOCK_START_AHEAD

_MD_HTML_BR = r"(?i: < br [ \t]* /? > )"
_MD_HTML_HR_INLINE = r"(?i: < hr [ \t]* /? > )"

_MD_ESCAPED = r"\\."

_MD_CODE_INLINE2 = rf"""
    `` (?P<*>
        (?: (?!``) (?: [^\n] | \n (?! {_BSA} ) ) )+
    ) ``
"""
_MD_CODE_INLINE  = rf" ` (?P<*> (?: {_MD_ESCAPED} | [^`\n\\] | \n (?! {_BSA} ) )+ ) ` "

_MD_IMAGE = r" ! \[ (?P<*alt> [^\]\n]* ) \] \( (?P<*url> [^)\n]* ) \) "
_MD_IMAGE_INLINE = r" ! \[ [^\]\n]* \] \( [^)\n]* \) "

_MD_ESCAPE = r"""
    \\ (?P<*char>
        [ !"\#\$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~ \n ]
    )
"""

_MD_HTML_COMMENT = r" <!-- (?: (?! --> ) [\s\S] )* --> "

# Standalone compiled twin, used by the table renderer to strip comments from a
# raw row line BEFORE splitting on `|`.
_M2A_HTML_COMMENT_RE = re.compile(_MD_HTML_COMMENT, re.VERBOSE | re.DOTALL)

_MD_HTML_ENTITY = r"""
    & (?P<*body>
        \# [0-9]+ | \# [xX] [0-9a-fA-F]+ | [a-zA-Z] [a-zA-Z0-9]*
    ) ;
"""

_MD_LINK = rf"""
    (?<!!) \[ (?P<*>
        (?: {_MD_IMAGE_INLINE} | {_MD_ESCAPED} | [^\]\n\\] | \n (?! {_BSA} ) )+
    ) \] \( (?P<*url> [^)\n]* ) \)
"""

_MD_BOLDITALIC = rf"""
    \*\*\* (?P<*>
        (?: {_MD_ESCAPED} | [^*\n\\] | \*(?!\*\*) | \n (?! {_BSA} ) )+
    ) \*\*\*
"""
_MD_BOLD_UNDER = rf"""
    \*\*_ (?P<*>
        (?: {_MD_ESCAPED} | [^_\n\\] | \n (?! {_BSA} ) )+
    ) _\*\*
"""
_MD_UNDER_BOLD = rf"""
    _\*\* (?P<*>
        (?: {_MD_ESCAPED} | [^*\n\\] | \*(?!\*) | \n (?! {_BSA} ) )+
    ) \*\*_
"""
_MD_BOLD = rf"""
    \*\* (?P<*>
        (?: {_MD_ESCAPED} | [^*\n\\] | \*(?!\*) | \n (?! {_BSA} ) )+
    ) \*\*
"""
_MD_STRIKE = rf"""
    ~~ (?P<*>
        (?: {_MD_ESCAPED} | [^~\n\\] | ~(?!~) | \n (?! {_BSA} ) )+
    ) ~~
"""
_MD_ITALIC = rf"""
    (?<!\*) \* (?P<*>
        (?: {_MD_ESCAPED} | [^*\n\\] | \n (?! {_BSA} ) )+
    ) \* (?!\*)
"""

# A leading guard rejects def-shaped occurrences of `[^id]:` at line start.
_MD_FOOTNOTE_REF = r"""
    (?: (?<= [^\n] ) | (?! \[ \^ [^\]\n]+ \] : ) )
    \[ \^ (?P<*id> [^\]\n]+ ) \]
"""

_M2A_RULES_INLINE_RAW = (
    ("code_inline2",  _MD_CODE_INLINE2, _m2a_fmt_inline_code,  None),
    ("code_inline",   _MD_CODE_INLINE,  _m2a_fmt_inline_code,  None),
    ("escape",        _MD_ESCAPE,       _m2a_fmt_escape,       None),
    ("html_comment",  _MD_HTML_COMMENT, _m2a_fmt_comment,      None),
    ("html_br",       _MD_HTML_BR,      _m2a_fmt_br,           None),
    ("html_hr_inline",_MD_HTML_HR_INLINE, _m2a_fmt_hr_inline,  None),
    ("html_entity",   _MD_HTML_ENTITY,  _m2a_fmt_entity,       None),
    ("image",         _MD_IMAGE,        _m2a_fmt_image,        None),
    ("link",          _MD_LINK,         M2A_COLOR_LINK,        _M2A_RECURSE_SELF),
    ("bolditalic",    _MD_BOLDITALIC,   "1;3",                 _M2A_RECURSE_SELF),
    ("bold_under",    _MD_BOLD_UNDER,   "1;3",                 _M2A_RECURSE_SELF),
    ("under_bold",    _MD_UNDER_BOLD,   "1;3",                 _M2A_RECURSE_SELF),
    ("bold",          _MD_BOLD,         "1",                   _M2A_RECURSE_SELF),
    ("strike",        _MD_STRIKE,       "9",                   _M2A_RECURSE_SELF),
    ("italic",        _MD_ITALIC,       "3",                   _M2A_RECURSE_SELF),
    ("footnote_ref",  _MD_FOOTNOTE_REF, _m2a_fmt_footnote_ref, None),
)
M2A_CONTEXT_MD_INLINE = _m2a_build_context(_M2A_RULES_INLINE_RAW)


# ### Section: Internal inline dispatcher ###################################

# Borrowed verbatim from v1 (design §9). Runs a compiled context's alternation
# over a leaf string: string `fmt` codes layer SGR (recursing per _M2A_RECURSE_SELF),
# callable `fmt`s render themselves. Used only for inline and code contexts — the
# block layer above dispatches by kind instead (see `_m2a_render`).


def _md2ansi(text, current_style, context, state):
    def _m2a_replace(m):
        groups = m.groupdict()
        for name, _pat, fmt, recurse in context.rules:
            if groups.get(name) is None:
                continue
            match fmt:
                case str() as sgr:
                    inner = groups.get(f"{name}_inner")
                    new_style = f"{current_style};{sgr}"
                    actual_recurse = context if recurse is _M2A_RECURSE_SELF else recurse
                    if actual_recurse is not None and inner is not None:
                        inner = _md2ansi(inner, new_style, actual_recurse, state)
                    elif inner is None:
                        inner = m.group(0)
                    return _m2a_inject_color(inner, new_style, current_style)
                case _ as func:
                    return func(m, name, current_style, context, state)
        return m.group(0)
    return context.compiled.sub(_m2a_replace, text)


# ### Section: Block-level pattern fragments ################################

# Block patterns borrowed verbatim from v1 (design §9). In v1 these lived in the
# single combined grammar; in v2 they populate a block-only alternation (§5.1)
# that the two-phase engine scans with.

# Every block pattern captures its first line's leading indent as `indent`, so
# the block dispatcher can apply indentation-as-chrome uniformly (design §5.5).
# Frontmatter's is empty (`\A`-anchored, root only §5.4); fences carry theirs in
# `_fenced`; the list rule deliberately owns its `[ \t]*` for the per-line level
# cosmetic (§7.2) and captures NO `indent`, so the dispatcher skips it.
_MD_H1 = r"^ (?P<*indent> [ \t]* ) \# [ \t]+ (?P<*> [^\n]+ ) $"
_MD_H2 = r"^ (?P<*indent> [ \t]* ) \#{2} [ \t]+ (?P<*> [^\n]+ ) $"
_MD_H3 = r"^ (?P<*indent> [ \t]* ) \#{3} [ \t]+ (?P<*> [^\n]+ ) $"
_MD_H4 = r"^ (?P<*indent> [ \t]* ) \#{4} [ \t]+ (?P<*> [^\n]+ ) $"
_MD_H5 = r"^ (?P<*indent> [ \t]* ) \#{5} [ \t]+ (?P<*> [^\n]+ ) $"
_MD_H6 = r"^ (?P<*indent> [ \t]* ) \#{6} [ \t]+ (?P<*> [^\n]+ ) $"

_MD_HR = r"^ (?P<*indent> [ \t]* ) (?: -{3,} | ={3,} | _{3,} ) [ \t]* $"

_MD_HTML_HR = r"^ (?P<*indent> [ \t]* ) (?i: < hr [ \t]* /? > ) [ \t]* $"

_MD_FRONTMATTER = r"""
    \A (?P<*indent>) --- [ \t]* \n
    (?P<*body>
        (?: ^ (?! --- [ \t]* $ ) (?! [ \t]* \# ) (?! [ \t]* $ ) [^\n]* \n )*
    )
    ^ --- [ \t]* $
"""


def _fenced(tag, fence=r"```"):
    return rf"""
        ^ (?P<*indent> [ \t]* ) {fence} [ \t]* {tag} [ \t]* \n
        (?P<*body> (?: (?! ^ [ \t]* {fence} [ \t]* $ ) [\s\S] )* )
        ^ [ \t]* {fence} [ \t]* $
    """


_MD_CODE_PY   = _fenced("python")
_MD_CODE_BASH = _fenced(r"(?:bash|sh)")
_MD_CODE_JS   = _fenced(r"(?:javascript|js)")
_MD_CODE_C    = _fenced(r"(?:c\+\+|cpp|cxx|cc|hpp|hxx|h|c)")
_MD_CODE_GEN  = _fenced(r"(?P<*lang> \w* )", fence=r"(?:```|~~~)")

# A blockquote is line-oriented (§5.5): continuation lines must carry the first
# line's indent EXACTLY (the `(?P=*indent)` backreference), so a differently
# indented `>` line falls out and forms its own single-bar quote at its own
# position — honest raggedness, never a spurious nested bar. Col-0 quotes have an
# empty indent, so the backreference matches nothing and the pattern is v1's.
_MD_BLOCKQUOTE = r"^ (?P<*indent> [ \t]* ) > [ \t]? [^\n]* (?: \n (?P=*indent) > [ \t]? [^\n]* )*"

_MD_TABLE = r"^ (?P<*indent> [ \t]* ) \| [^\n]* (?: \n [ \t]* \| [^\n]* )*"

_MD_LIST = r"""
    ^ [ \t]* (?: [-*+] | \d+\. ) [ \t]+ [^\n]*
    (?: \n [ \t]* (?: [-*+] | \d+\. ) [ \t]+ [^\n]* )*
"""

_MD_FOOTNOTE_DEF = r"""
    ^ (?P<*indent> [ \t]* ) \[ \^ (?P<*id> [^\]\n]+ ) \] : [ \t]+
    (?P<*text> [^\n]+ (?: \n [ \t]+ [^\n]+ )* )
"""


# ### Section: Block renderers ##############################################

# Ported from v1's block formatters (design §7), with two structural changes
# mandated by §6: the `_m2a_opaque()` wrapper is GONE (there is no post-render
# pass, so every renderer returns final laid-out text), and any `\x03` (nbsp)
# realization v1 deferred to that global pass now happens here. Renderers return
# text WITHOUT a trailing newline; `_m2a_render` appends the newline each block
# owns under the tiling contract (§5.2).


def _m2a_fmt_heading(m, name, current_style, state, sgr):
    """Render an ATX heading: inline pass on the title under the level color,
    realize the deferred line sentinels into geometry, then color. Never wrapped
    (a heading that overflows stays on one line — its block intent)."""
    inner = m.group(f"{name}_inner")
    new_style = f"{current_style};{sgr}"
    inner = _md2ansi(inner, new_style, M2A_CONTEXT_MD_INLINE, state)
    rule = _m2a_rule(state.line_width - 1)
    inner = "\n".join(
        rule if kind == "rule" else seg
        for kind, seg in _m2a_split_sentinel_lines(inner)
    )
    out = _m2a_inject_color(inner, new_style, current_style)
    return out.replace(_M2A_NBSP, " ")


def _m2a_fmt_hr(m, name, current_style, state):
    bar = _m2a_rule(state.line_width - 1)
    return _m2a_inject_color(bar, current_style, current_style)


def _m2a_fmt_code(m, name, current_style, state, code_context, lang=None, label=None):
    # Leading indent is handled once, for all block kinds, by the dispatcher
    # (design §5.5): a match reaching here is already dedented, so the fence body
    # needs no indent stripping and the frame no re-prefixing.
    body = m.group(f"{name}_body")
    if lang is None:
        lang = (m.groupdict().get(f"{name}_lang") or "").strip()
    rendered = _md2ansi(body, current_style, code_context, state)
    body_width = max(
        (_m2a_visible_len(ln) for ln in rendered.split("\n")),
        default=0,
    )
    if label is None:
        label = f"Code: {lang}" if lang else "Code"
    min_inner = len(label) + 6
    inner = max(body_width, min_inner)
    right_dashes = inner - 4 - len(label)
    top_text = f"┌── {label} {'─' * right_dashes}┐"
    bot_text = f"└{'─' * inner}┘"
    top = _m2a_styled(top_text, current_style, M2A_COLOR_FRAME)
    bot = _m2a_styled(bot_text, current_style, M2A_COLOR_FRAME)
    indented = _m2a_prefix_lines(rendered, " ")
    if indented.endswith("\n "):
        indented = indented[:-1]
    sep = "" if indented.endswith("\n") else "\n"
    return f"{top}\n{indented}{sep}{bot}"


def _m2a_fmt_blockquote(m, name, current_style, state):
    text = m.group(0)
    stripped = "\n".join(re.sub(r"^>[ \t]?", "", ln) for ln in text.split("\n"))
    # A blockquote is a mini-document (design §7.1): strip the `>` markers, then
    # recurse the body through the quote block context (the full block grammar
    # minus frontmatter and footnote_def — §5.4) at a width narrowed by the
    # `│ ` bar (2 columns), floored at M2A_MIN_WIDTH. `_m2a_render` returns final
    # laid-out text — inner blocks already wrapped, prose already wrapped — so
    # here we only prefix chrome. No opaque marker, no re-wrap (§6). The footnotes
    # dict/list are shared by reference through `replace`, so a ref inside the
    # quote still resolves against top-level defs.
    inner = _m2a_render(stripped, _m2a_narrow(state, 2), _M2A_BLOCK_CONTEXT_QUOTE)
    bar = _m2a_styled("│", current_style, M2A_COLOR_DIM) + " "
    return _m2a_prefix_lines(inner, bar)


def _m2a_fmt_table(m, name, current_style, state):
    raw_rows = []
    for ln in m.group(0).strip("\n").split("\n"):
        s = ln.strip()
        if not s.startswith("|"):
            continue
        s = _M2A_HTML_COMMENT_RE.sub("", s)
        raw_rows.append(_m2a_split_table_row(s))
    if len(raw_rows) < 1:
        return m.group(0)
    header = raw_rows[0]
    body_start = 1
    if len(raw_rows) >= 2 and all(re.fullmatch(r":?-{2,}:?", c) for c in raw_rows[1]):
        body_start = 2
    body = raw_rows[body_start:]
    n_cols = len(header)

    aligns = ["left"] * n_cols
    if body_start == 2:
        for i, c in enumerate(raw_rows[1][:n_cols]):
            left_mark = c.startswith(":")
            right_mark = c.endswith(":")
            if left_mark and right_mark:
                aligns[i] = "center"
            elif right_mark:
                aligns[i] = "right"
            else:
                aligns[i] = "left"

    def pad(row):
        return list(row[:n_cols]) + [""] * max(0, n_cols - len(row))

    header = pad(header)
    body = [pad(r) for r in body]
    rendered_header = [_md2ansi(c, current_style, M2A_CONTEXT_MD_INLINE, state) for c in header]
    rendered_body = [[_md2ansi(c, current_style, M2A_CONTEXT_MD_INLINE, state) for c in r] for r in body]
    widths = [
        max(
            _m2a_visible_len(rendered_header[i]),
            *(_m2a_visible_len(r[i]) for r in rendered_body),
            1,
        )
        for i in range(n_cols)
    ]

    target_lw = state.wrap_width
    cell_min = state.cell_min_width
    if target_lw > 0:
        overhead = 3 * n_cols + 1
        fixed = {i for i in range(n_cols) if widths[i] <= cell_min}
        wide = [i for i in range(n_cols) if i not in fixed]
        for _ in range(n_cols + 1):
            fit_w = target_lw - overhead - sum(widths[i] for i in fixed)
            wide_sum = sum(widths[i] for i in wide)
            if not wide or wide_sum <= fit_w:
                break
            factor = fit_w / wide_sum if wide_sum > 0 else 0
            progressed = False
            still_wide = []
            for i in wide:
                new = int(widths[i] * factor)
                if new <= cell_min:
                    widths[i] = cell_min
                    fixed.add(i)
                    progressed = True
                else:
                    widths[i] = new
                    still_wide.append(i)
            wide = still_wide
            if not progressed:
                break

    cell_reset = f"\x1b[{current_style}m"

    def cell_sublines(rendered, w):
        if not rendered:
            return [""]
        out = []
        for kind, seg in _m2a_split_sentinel_lines(rendered):
            if kind == "rule":
                out.append(_M2A_RULE)
            else:
                out.extend(_m2a_wrap_ansi_line(seg, w, "", cell_reset) if seg else [""])
        return out

    header_cells = [cell_sublines(rendered_header[i], widths[i]) for i in range(n_cols)]
    body_cells = [[cell_sublines(r[i], widths[i]) for i in range(n_cols)] for r in rendered_body]

    def _col_actual(i):
        def _sub_w(s):
            return 0 if s == _M2A_RULE else _m2a_visible_len(s)
        actual = max(
            (_sub_w(s) for s in header_cells[i]),
            default=0,
        )
        for row in body_cells:
            for s in row[i]:
                actual = max(actual, _sub_w(s))
        return actual

    def _rewrap_column(i):
        header_cells[i] = cell_sublines(rendered_header[i], widths[i])
        for r_idx, r in enumerate(rendered_body):
            body_cells[r_idx][i] = cell_sublines(r[i], widths[i])

    def _reconcile_column(i):
        for _ in range(n_cols + 8):
            actual = _col_actual(i)
            if actual <= widths[i]:
                break
            widths[i] = actual
            _rewrap_column(i)
        else:
            widths[i] = max(widths[i], _col_actual(i))
        if actual < widths[i]:
            widths[i] = max(actual, 1)

    for i in range(n_cols):
        _reconcile_column(i)

    if target_lw > 0:
        layout_widths = list(widths)
        for _outer in range(n_cols + 1):
            total = overhead + sum(widths)
            if total <= target_lw:
                break
            oversize = {i for i in range(n_cols) if widths[i] > layout_widths[i]}
            non_shrinkable = {i for i in range(n_cols) if widths[i] <= cell_min}
            shrinkable = [
                i for i in range(n_cols)
                if i not in oversize and i not in non_shrinkable
            ]
            if not shrinkable:
                break
            excluded_sum = sum(widths[i] for i in oversize) + sum(widths[i] for i in non_shrinkable)
            fit_w = max(0, target_lw - overhead - excluded_sum)
            cur_sum = sum(widths[i] for i in shrinkable)
            if cur_sum <= fit_w:
                break
            factor = fit_w / cur_sum if cur_sum > 0 else 0
            progressed = False
            for i in shrinkable:
                new_w = max(cell_min, int(widths[i] * factor))
                if new_w >= widths[i]:
                    continue
                widths[i] = new_w
                layout_widths[i] = new_w
                _rewrap_column(i)
                _reconcile_column(i)
                progressed = True
            if not progressed:
                break

    def render_row(cells):
        height = max((len(c) for c in cells), default=1)
        out = []
        for k in range(height):
            parts = []
            for i, col in enumerate(cells):
                if k < len(col):
                    if col[k] == _M2A_RULE:
                        parts.append(f" {_m2a_rule(widths[i])} ")
                    else:
                        parts.append(f" {_m2a_align_cell(col[k], widths[i], aligns[i])} ")
                else:
                    parts.append(" " + " " * widths[i] + " ")
            out.append("│" + "│".join(parts) + "│")
        return out, height

    def border(left, mid, right):
        return left + mid.join("─" * (widths[i] + 2) for i in range(n_cols)) + right

    out_lines = [border("┌", "┬", "┐")]
    header_lines, _ = render_row(header_cells)
    out_lines.extend(header_lines)
    out_lines.append(border("├", "┼", "┤"))

    body_blocks = []
    any_wrapped = False
    for row in body_cells:
        row_lines, height = render_row(row)
        body_blocks.append(row_lines)
        if height > 1:
            any_wrapped = True

    if state.row_dividers is True:
        emit_dividers = True
    elif state.row_dividers is False:
        emit_dividers = False
    else:
        emit_dividers = any_wrapped

    for idx, rl in enumerate(body_blocks):
        if idx > 0 and emit_dividers:
            out_lines.append(border("├", "┼", "┤"))
        out_lines.extend(rl)
    out_lines.append(border("└", "┴", "┘"))
    return "\n".join(out_lines).replace(_M2A_NBSP, " ")


def _m2a_fmt_list(m, name, current_style, state):
    """Render a maximal run of marker lines (design §7.2). The list renderer owns
    exactly the marker-line cosmetics; everything else falls out of what the
    remainder recurses into. Per marker line: visible indent width // 2 is the
    cosmetic level (nested markers are just deeper lines of the same span, no
    parent/child), `-`/`*`/`+` become a bold `·`, ordered markers stay literal
    (no renumbering, ever). The remainder after the marker then splits two ways:

    - A remainder that IS a block — a heading, rule, one-line quote, one-row
      table, or a footnote def (the nested alternation; lists are not quotes so
      a def collects, §5.4) — recurses through `_m2a_render` at a width narrowed
      by the bullet columns and hangs under the bullet. `- [^1]: note` collects
      and renders an empty item.
    - Any other remainder is prose (the overwhelmingly common case): the inline
      pass, then wrap with the bullet on the first output line and the hang
      indent on continuations. This path keeps v1's combined bullet+content wrap
      verbatim — it is byte-identical to v1 (the equivalence corpus enforces it),
      whereas recursing prose through `_m2a_render` would drop the base-style
      re-emit v1 puts on list continuation lines.
    """
    out_lines = []
    for ln in m.group(0).split("\n"):
        match = re.match(r"^([ \t]*)([-*+]|\d+\.)[ \t]+(.*)$", ln)
        if not match:
            out_lines.append(ln)
            continue
        indent, marker, content = match.groups()
        level = len(indent.expandtabs(4)) // 2
        bullet = "·" if marker in ("-", "*", "+") else marker
        styled = _m2a_styled(bullet, current_style, "1")
        hang = "  " * level + "  "
        bullet_prefix = f"{'  ' * level}{styled} "
        if _m2a_leading_block(content, _M2A_BLOCK_CONTEXT_NESTED) is not None:
            rendered = _m2a_render(content, _m2a_narrow(state, len(hang)), _M2A_BLOCK_CONTEXT_NESTED)
            out_lines.append(f"{'  ' * level}{styled}")
            if rendered:
                out_lines.extend(hang + out_ln for out_ln in rendered.split("\n"))
            continue
        rendered = _md2ansi(content, current_style, M2A_CONTEXT_MD_INLINE, state)
        content_w = (state.wrap_width if state.wrap_width > 0 else state.line_width) - len(hang)
        rule = _m2a_rule(content_w)
        first = True
        for kind, seg in _m2a_split_sentinel_lines(rendered):
            if kind == "rule":
                out_lines.append(hang + rule)
                continue
            line = (bullet_prefix if first else hang) + seg
            first = False
            if state.wrap_width > 0:
                out_lines.extend(_m2a_wrap_ansi_line(line, state.wrap_width, hang))
            else:
                out_lines.append(line)
    return "\n".join(out_lines).replace(_M2A_NBSP, " ")


def _m2a_fmt_footnote_def(m, name, current_style, state):
    fid = m.group(f"{name}_id")
    text = m.group(f"{name}_text")
    text = re.sub(r"\n[ \t]+", " ", text).strip()
    state.footnotes[fid] = text
    return ""


def _m2a_render_footnotes(state, current_style):
    entries = [(fid, state.footnotes[fid]) for fid in state.footnote_order if fid in state.footnotes]
    if not entries:
        return ""
    out = ["", _m2a_styled("Footnotes:", current_style, "1")]
    for fid, text in entries:
        ref = _m2a_styled(f"[^{fid}]", current_style, M2A_COLOR_FOOTNOTE)
        out.append(f"  {ref} {text}")
    return "\n".join(out) + "\n"


# ### Section: Block rule table & compiled block contexts ###################

# One block rules table, compiled into (at most) three alternations via placement
# flags (design §5.1, §5.4). Block rules ONLY — the inline rules are not here;
# they run in leaves via `_md2ansi`. Order mirrors v1's combined grammar so
# block-boundary detection is identical (frontmatter before hr, etc.).
#
# Placement flags per rule: `root_only` (frontmatter — its `\A` anchor misfires
# on a recursed body) and `in_quote` (a footnote_def inside a quote is quoted
# prose, not a collected note — v1 epics #95/#104). Three contexts:
#   ROOT   = every rule.
#   NESTED = every rule except root_only (frontmatter). The recursion target for
#            indent-as-chrome (§5.5) and list marker-line remainders (§7.2).
#   QUOTE  = NESTED minus footnote_def.

# (name, pattern, root_only, in_quote)
_M2A_BLOCK_RULES = (
    ("frontmatter",  _MD_FRONTMATTER, True,  False),
    ("h1",           _MD_H1,          False, True),
    ("h2",           _MD_H2,          False, True),
    ("h3",           _MD_H3,          False, True),
    ("h4",           _MD_H4,          False, True),
    ("h5",           _MD_H5,          False, True),
    ("h6",           _MD_H6,          False, True),
    ("hr",           _MD_HR,          False, True),
    ("html_hr",      _MD_HTML_HR,     False, True),
    ("code_python",  _MD_CODE_PY,     False, True),
    ("code_bash",    _MD_CODE_BASH,   False, True),
    ("code_js",      _MD_CODE_JS,     False, True),
    ("code_c",       _MD_CODE_C,      False, True),
    ("code_generic", _MD_CODE_GEN,    False, True),
    ("blockquote",   _MD_BLOCKQUOTE,  False, True),
    ("table",        _MD_TABLE,       False, True),
    ("list",         _MD_LIST,        False, True),
    ("footnote_def", _MD_FOOTNOTE_DEF, False, False),
)


def _m2a_block_context(rules):
    return _m2a_build_context(tuple((n, p, None, None) for n, p, *_ in rules))


_M2A_BLOCK_CONTEXT_ROOT   = _m2a_block_context(_M2A_BLOCK_RULES)
# Recursion target for indent-as-chrome (§5.5) and list marker-line remainders (§7.2).
_M2A_BLOCK_CONTEXT_NESTED = _m2a_block_context([r for r in _M2A_BLOCK_RULES if not r[2]])
_M2A_BLOCK_CONTEXT_QUOTE  = _m2a_block_context([r for r in _M2A_BLOCK_RULES if r[3]])


# Dispatch by block-rule name to a renderer taking `(match, state)`. The v1-shape
# handlers (which took `current_style` as a parameter) are wrapped here, passing
# `state.current_style` and binding each block's fixed arguments (color, code
# context, label).
_M2A_BLOCK_RENDERERS = {
    "frontmatter":  lambda m, st: _m2a_fmt_code(m, "frontmatter", st.current_style, st, M2A_CONTEXT_CODE_GENERIC, label="Frontmatter"),
    "h1":           lambda m, st: _m2a_fmt_heading(m, "h1", st.current_style, st, M2A_COLOR_H1),
    "h2":           lambda m, st: _m2a_fmt_heading(m, "h2", st.current_style, st, M2A_COLOR_H2),
    "h3":           lambda m, st: _m2a_fmt_heading(m, "h3", st.current_style, st, M2A_COLOR_H3),
    "h4":           lambda m, st: _m2a_fmt_heading(m, "h4", st.current_style, st, M2A_COLOR_H4),
    "h5":           lambda m, st: _m2a_fmt_heading(m, "h5", st.current_style, st, M2A_COLOR_H5),
    "h6":           lambda m, st: _m2a_fmt_heading(m, "h6", st.current_style, st, M2A_COLOR_H6),
    "hr":           lambda m, st: _m2a_fmt_hr(m, "hr", st.current_style, st),
    "html_hr":      lambda m, st: _m2a_fmt_hr(m, "html_hr", st.current_style, st),
    "code_python":  lambda m, st: _m2a_fmt_code(m, "code_python", st.current_style, st, M2A_CONTEXT_CODE_PYTHON, "python"),
    "code_bash":    lambda m, st: _m2a_fmt_code(m, "code_bash", st.current_style, st, M2A_CONTEXT_CODE_BASH, "bash"),
    "code_js":      lambda m, st: _m2a_fmt_code(m, "code_js", st.current_style, st, M2A_CONTEXT_CODE_JAVASCRIPT, "javascript"),
    "code_c":       lambda m, st: _m2a_fmt_code(m, "code_c", st.current_style, st, M2A_CONTEXT_CODE_C, label="C/C++"),
    "code_generic": lambda m, st: _m2a_fmt_code(m, "code_generic", st.current_style, st, M2A_CONTEXT_CODE_UNKNOWN),
    "blockquote":   lambda m, st: _m2a_fmt_blockquote(m, "blockquote", st.current_style, st),
    "table":        lambda m, st: _m2a_fmt_table(m, "table", st.current_style, st),
    "list":         lambda m, st: _m2a_fmt_list(m, "list", st.current_style, st),
    "footnote_def": lambda m, st: _m2a_fmt_footnote_def(m, "footnote_def", st.current_style, st),
}


# ### Section: The two-phase engine #########################################


def _m2a_first_group(m, context):
    """The outer rule name that matched — the first outer named group with a
    non-None value (as `_md2ansi` identifies the rule; NOT `m.lastgroup`)."""
    groups = m.groupdict()
    for name, *_ in context.rules:
        if groups.get(name) is not None:
            return name
    return None


def _m2a_block_scan(text, block_context):
    """Yield `(rule_name, match, start, end)` spans that TILE `text` exactly
    (design §5.2): every byte belongs to exactly one span, in document order,
    each span owning its trailing `\\n` (the final span may lack one). Block
    matches are extended through their trailing newline (block patterns don't
    consume it, keeping them v1-shaped). Interstitial gaps yield as
    `("prose", None, start, end)` — prose is a first-class block kind (§5.3).

    One scanner, two consumers (§8): `_m2a_render` and `md2ansi_scan`.
    """
    pos = 0
    n = len(text)
    for m in block_context.compiled.finditer(text):
        start, mend = m.start(), m.end()
        end = mend + 1 if mend < n and text[mend] == "\n" else mend
        if start > pos:
            yield ("prose", None, pos, start)
        yield (_m2a_first_group(m, block_context), m, start, end)
        pos = end
    if pos < n:
        yield ("prose", None, pos, n)


def _m2a_render_prose(text, state):
    """Render a prose span (design §5.3): whole-span inline pass, then per output
    line realize the deferred sentinels and word-wrap. This reproduces v1's
    combined inline pass + the non-opaque branch of its post-render wrap, applied
    to exactly the text between block matches. No paragraph reflow — each source
    line wraps independently.

    The `\\x02` rule width and the wrap width both come from `state.wrap_width`
    (which equals the `line_width` v1 threaded into `_m2a_wrap_rendered` at every
    level), so rule sizing matches v1's `line_width or 150`.
    """
    styled = _md2ansi(text, state.current_style, M2A_CONTEXT_MD_INLINE, state)
    wrap_width = state.wrap_width
    rule_w = wrap_width if wrap_width > 0 else 150
    rule_line = _m2a_rule(rule_w - 1)
    out = []
    for ln in styled.split("\n"):
        for kind, seg in _m2a_split_sentinel_lines(ln):
            if kind == "rule":
                out.append(rule_line)
                continue
            if wrap_width > 0:
                cont = re.match(r"[ \t]*", seg).group(0)
                out.extend(_m2a_wrap_ansi_line(seg, wrap_width, cont))
            else:
                out.append(seg)
    return "\n".join(out).replace(_M2A_NBSP, " ")


def _m2a_narrow(state, cols):
    """A stack-local state with both widths narrowed by `cols` visible columns,
    floored at M2A_MIN_WIDTH (design §6); `wrap_width` stays 0 when it is 0. The
    single narrowing primitive for every kind of chrome — the blockquote bar, the
    list bullet columns, and the indent prefix (§5.5)."""
    wrap = max(M2A_MIN_WIDTH, state.wrap_width - cols) if state.wrap_width > 0 else 0
    line = max(M2A_MIN_WIDTH, state.line_width - cols)
    return replace(state, wrap_width=wrap, line_width=line)


def _m2a_leading_block(text, block_context):
    """The block-rule name `text` BEGINS with, or None if it is plain prose. Used
    by the list renderer to route a marker-line remainder to block recursion
    (§7.2) versus the v1-parity prose path."""
    m = block_context.compiled.match(text)
    if m is None:
        return None
    return _m2a_first_group(m, block_context)


def _m2a_render_indented(m, indent, state, block_context):
    """Indentation as chrome (design §5.5), applied once for every block kind.
    Strip the literal first-line `indent` from each line of the match that starts
    with it (a line with more indentation keeps the excess as inner structure; a
    line with less is left untouched and comes out more indented than written —
    permissive, §5.5 PROVISIONAL), render the dedented block normally at a width
    narrowed by the indent's visible width (`expandtabs(4)`), then re-prefix the
    literal `indent` onto every output line. Identical mechanism to the blockquote
    bar, with spaces; a blockquote at indent > 0 composes the two chromes."""
    dedented = "\n".join(
        ln[len(indent):] if ln.startswith(indent) else ln
        for ln in m.group(0).split("\n")
    )
    inner = _m2a_render(dedented, _m2a_narrow(state, len(indent.expandtabs(4))), block_context)
    return _m2a_prefix_lines(inner, indent)


def _m2a_render(text, state, block_context):
    """THE recursive entry (design §4). Scan with block rules only; render each
    span with its renderer and reassemble by plain concatenation (the tiling
    contract removes any need for a joiner or global spacing). Each block owns the
    trailing newline the scanner attached; prose owns its own newlines.

    Indentation is chrome (§5.5): a block whose first line carries a non-empty
    `indent` capture is dedented, rendered narrowed, and re-prefixed — once here,
    for every kind. The list rule owns its own `[ \\t]*` (no `indent` group) so it
    is skipped; frontmatter's `indent` is always empty (§5.4).
    """
    parts = []
    for rule, m, start, end in _m2a_block_scan(text, block_context):
        if rule == "prose":
            parts.append(_m2a_render_prose(text[start:end], state))
            continue
        trailer = text[m.end():end]   # the "\n" the scanner attached, or ""
        indent = m.groupdict().get(f"{rule}_indent") or ""
        if indent:
            rendered = _m2a_render_indented(m, indent, state, block_context)
        else:
            rendered = _M2A_BLOCK_RENDERERS[rule](m, state)
        parts.append(rendered + trailer)
    return "".join(parts)


# ### Section: Public API ###################################################


def md2ansi_color(text, current_style="0", line_width=0, cell_min_width=20, row_dividers=None):
    """Convert Markdown text to ANSI-colored output. Canonical renderer name
    (design §3); `md2ansi` is a v1-compatible alias.

    `line_width` > 0 enables word wrapping for prose, lists, and blockquotes, and
    is the width HR/tables size to; below M2A_MIN_WIDTH it clamps to 20 (§6). When
    0 (default) no wrapping happens and HR falls back to a 150-char bar.
    `cell_min_width` is the minimum a table column shrinks to; `row_dividers` is a
    tristate (None: dividers only when a body cell wraps; True/False: always/never).
    """
    # Input sanitizer (design §6, carried from v1): normalize CRLF/CR to `\n`,
    # then map every remaining C0 control char except `\t`/`\n`/ESC to U+FFFD —
    # neutralizing any stray sentinel in the source.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _M2A_C0_KILL.sub("�", text)
    # Style sanitizer: the base style must begin with reset `0` so every span's
    # close (which re-emits current_style) actually clears layered attributes.
    if current_style != "0" and not current_style.startswith("0;"):
        current_style = "0;" + current_style
    # §6: one width floor at every level INCLUDING root — `line_width=5` renders
    # at 20. This diverges from v1 (which floored only container narrowing) at
    # sub-20 root widths; the equivalence allowlist covers it, and it is dormant
    # under the standard 0/30/60/100/150 sweep.
    if line_width > 0:
        line_width = max(M2A_MIN_WIDTH, line_width)
    state_lw = line_width if line_width > 0 else 150
    state = M2A_DocumentState(
        line_width=state_lw,
        cell_min_width=cell_min_width,
        row_dividers=row_dividers,
        wrap_width=line_width,
        current_style=current_style,
    )
    out = _m2a_render(text, state, _M2A_BLOCK_CONTEXT_ROOT)
    if state.footnote_order:
        out += _m2a_render_footnotes(state, current_style)
    return out


md2ansi = md2ansi_color  # v1-compatible alias


# ### Section: Structural scan API ##########################################

# `md2ansi_scan` consumes the SAME `_m2a_block_scan` the renderer uses (design
# §8) — one scanner, two consumers, so the APIs cannot drift. Block spans tile
# the raw source (§5.2). Inline-kind scanning keeps v1 behavior: top-level inline
# matches, which occur exactly in the prose gaps (block rules win at every
# line-start, so the inline grammar only ever fires in the gaps), found by
# running the inline alternation over each prose span.


@dataclass(frozen=True, slots=True)
class M2A_Span:
    """One span from `md2ansi_scan`.

    `kind` is the broad category ('heading', 'code', 'list', 'emphasis', 'prose',
    …); `subtype` is the narrow refinement, always populated. `is_block`
    separates block constructs from inline. `start`/`end` are character offsets
    into the scanned text (`text[start:end] == text`).
    """
    kind: str
    subtype: str
    is_block: bool
    start: int
    end: int
    text: str


# Outer-rule-name -> (kind, subtype) for rules whose classification differs from
# the fallback (kind == subtype == rule name). Carried from v1.
_M2A_SPAN_KINDS = {
    "h1": ("heading", "h1"), "h2": ("heading", "h2"), "h3": ("heading", "h3"),
    "h4": ("heading", "h4"), "h5": ("heading", "h5"), "h6": ("heading", "h6"),
    "code_python":  ("code", "code-python"),
    "code_bash":    ("code", "code-bash"),
    "code_js":      ("code", "code-javascript"),
    "code_c":       ("code", "code-c"),
    "code_generic": ("code", "code"),
    "code_inline2": ("code_inline", "code_inline"),
    "code_inline":  ("code_inline", "code_inline"),
    "html_comment": ("comment", "comment"),
    "html_hr":      ("hr", "hr"),
    "html_hr_inline": ("hr", "hr"),
    "html_br":      ("br", "br"),
    "html_entity":  ("entity", "entity"),
    "bolditalic":   ("emphasis", "bolditalic"),
    "bold_under":   ("emphasis", "bolditalic"),
    "under_bold":   ("emphasis", "bolditalic"),
    "bold":         ("emphasis", "bold"),
    "italic":       ("emphasis", "italic"),
    "strike":       ("emphasis", "strike"),
}


def _m2a_span_kind(rule_name):
    """Map an outer rule name to `(kind, subtype)`; fallback is `(name, name)`."""
    return _M2A_SPAN_KINDS.get(rule_name, (rule_name, rule_name))


# Names of the inline rules — drives `is_block` and the inline kind set.
_M2A_INLINE_RULE_NAMES = frozenset(name for name, *_ in _M2A_RULES_INLINE_RAW)

# Broad-kind sets, derived from the rule tables (nothing hand-maintained). The
# block set comes from the block rule table; prose is deliberately NOT in it, so
# a default scan returns exactly what v1 returned (§8) and prose is opt-in.
M2A_SPANS_INLINE = frozenset(
    _m2a_span_kind(name)[0] for name in _M2A_INLINE_RULE_NAMES
)
M2A_SPANS_BLOCK = frozenset(
    _m2a_span_kind(name)[0] for name, *_ in _M2A_BLOCK_RULES
)
M2A_SPANS_ALL = M2A_SPANS_BLOCK | M2A_SPANS_INLINE


def _m2a_scan(text, kinds):
    """Generator workhorse for `md2ansi_scan` (no validation).

    Block spans (and prose gaps) come from `_m2a_block_scan`; each prose gap is
    re-scanned with the inline alternation to surface top-level inline matches,
    offset back into the source. Document order is preserved because block and
    prose spans already interleave in order and inline matches within a gap are
    in order.
    """
    for rule, m, start, end in _m2a_block_scan(text, _M2A_BLOCK_CONTEXT_ROOT):
        if rule == "prose":
            if "prose" in kinds:
                yield M2A_Span("prose", "prose", False, start, end, text[start:end])
            if kinds & M2A_SPANS_INLINE:
                gap = text[start:end]
                for im in M2A_CONTEXT_MD_INLINE.compiled.finditer(gap):
                    iname = _m2a_first_group(im, M2A_CONTEXT_MD_INLINE)
                    kind, subtype = _m2a_span_kind(iname)
                    if kind not in kinds:
                        continue
                    yield M2A_Span(
                        kind=kind,
                        subtype=subtype,
                        is_block=False,
                        start=start + im.start(),
                        end=start + im.end(),
                        text=im.group(0),
                    )
            continue
        kind, subtype = _m2a_span_kind(rule)
        if rule == "code_generic":
            tag = (m.groupdict().get("code_generic_lang") or "").strip()
            if tag:
                subtype = f"code-{tag}"
        if kind not in kinds:
            continue
        yield M2A_Span(
            kind=kind,
            subtype=subtype,
            is_block=True,
            start=start,
            end=end,
            text=text[start:end],
        )


def md2ansi_scan(text, kinds=M2A_SPANS_BLOCK):
    """Yield `M2A_Span` per top-level construct whose `kind` is in `kinds`, in
    document order, over the RAW source (`text[span.start:span.end] == span.text`).

    Block spans tile the source (§5.2, §8): each owns its trailing newline, gaps
    surface as `kind="prose"` spans. `prose` is NOT in `M2A_SPANS_BLOCK`, so a
    default scan returns exactly the block spans v1 returned; opt in with
    `M2A_SPANS_BLOCK | {"prose"}` for a full tiling. Inline scanning
    (`M2A_SPANS_INLINE`) keeps v1's top-level-match behavior. The scan is flat
    (non-recursive). `kinds` is validated eagerly: a name not in `M2A_SPANS_ALL`
    (or "prose") raises `ValueError` at the call, before iteration.
    """
    unknown = set(kinds) - M2A_SPANS_ALL - {"prose"}
    if unknown:
        raise ValueError(
            f"md2ansi_scan: unknown span kind(s) {sorted(unknown)}; "
            f"valid kinds are {sorted(M2A_SPANS_ALL)} (plus 'prose')"
        )
    return _m2a_scan(text, frozenset(kinds))


# ### Section: Plugin registration ##########################################

# Make this file double as a browse-tui plugin: when imported under a
# browse-tui interpreter (recipe / --plugin), self-register so the
# framework knows we're loaded. The import is guarded so the file still
# works as a standalone library / CLI when browse_tui isn't on the path.

try:
    from browse_tui import register_plugin, PluginConfig
    register_plugin(PluginConfig(name='md2ansi_lib'))
except ImportError:
    pass


# ### Section: main #########################################################

if __name__ == "__main__":
    import os
    import sys
    line_width = int(os.environ["LINE_WIDTH"]) if "LINE_WIDTH" in os.environ else 0
    paths = sys.argv[1:]
    if paths:
        for path in paths:
            with open(path) as f:
                sys.stdout.write(md2ansi_color(f.read(), line_width=line_width))
    else:
        sys.stdout.write(md2ansi_color(sys.stdin.read(), line_width=line_width))
