#!/usr/bin/env python3

"""md2ansi_lib2 — single-file, zero-dependency Markdown-to-ANSI library (v2).

A major-version rewrite of md2ansi_lib.py on a two-phase block/inline engine.
See md2ansi_lib2.design.md for architecture, naming conventions, rule tables,
and what is borrowed from v1 (§9); the two modules never import each other.
"""

import re
from dataclasses import dataclass, field, replace
from typing import Any


# ### Section: SGR color constants ##########################################

# Bare SGR codes — wrapping in `\x1b[...m` is the dispatcher's job (design §9).

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


# The single lower cap on line width, applied uniformly at every level
# INCLUDING root (design §6): `md2ansi_color(line_width=5)` renders at 20.
M2A_MIN_WIDTH = 20


@dataclass(slots=True)
class M2A_DocumentState:
    line_width: int = 150
    footnotes: dict = field(default_factory=dict)
    footnote_order: list = field(default_factory=list)
    cell_min_width: int = 20
    row_dividers: Any = None
    # The requested wrap width, or 0 when wrapping is disabled (design §6). Kept
    # distinct from `line_width` so the 150-char fallback used for HR sizing
    # doesn't enable wrapping/fitting.
    wrap_width: int = 0
    # The ambient base SGR (design §4): constant across block recursion, layered
    # on locally by the inline pass.
    current_style: str = "0"


# ### Section: Shared regex fragments #######################################

# All fragments are designed to be embedded inside re.VERBOSE patterns
# (whitespace ignored outside character classes; `#` is a comment unless
# escaped). Design §9.

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

# Width measurement, the SGR style/reset model, ANSI-aware wrapping, the
# deferred line sentinels, and the input sanitizer (design §9).

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


# Three single-char sentinels carry deferred-layout semantics between the
# inline pass and the leaf renderer that realizes them (design §6); they never
# survive past the level that emits them, and the sanitizer maps any stray copy
# in the SOURCE to U+FFFD.
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
    """Greedy ANSI-aware word-wrap: wraps at visible-character positions (with
    a small no-break zone at line start) and re-emits the last seen SGR on each
    new line so styling survives the break."""
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

# The inline rule tuple's callable formatters (design §9).

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

# The five code-highlight rule sets and their compiled contexts (design §9).

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

# The inline pattern fragments and rule tuple (design §9) — the alternation
# that runs inside every leaf (prose lines, heading titles, table cells,
# list-item content).

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

# Runs a compiled context's alternation over a leaf string (design §9): string
# `fmt` codes layer SGR (recursing per _M2A_RECURSE_SELF), callable `fmt`s
# render themselves. Inline and code contexts only — the block layer above
# dispatches by kind instead (see `_m2a_render`).


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

# The block-only alternation the two-phase engine scans with (design §5.1, §9).
# Every block pattern captures its first line's leading indent as `indent` for
# indentation-as-chrome (§5.5). Frontmatter's is empty (`\A`-anchored, root
# only §5.4); the list rule deliberately owns its `[ \t]*` for the per-line
# level cosmetic (§7.2) and captures NO `indent`, so the dispatcher skips it.
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

# Continuation lines must carry the first line's indent EXACTLY (the
# `(?P=*indent)` backreference): a differently indented `>` line starts its own
# single-bar quote at its own position (§5.5).
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

# Every renderer returns FINAL laid-out text (design §6-§7) — including `\x03`
# (nbsp) realization — WITHOUT a trailing newline; `_m2a_render` appends the
# newline each block owns under the tiling contract (§5.2).


def _m2a_fmt_heading(m, name, current_style, state, sgr):
    """Render an ATX heading (§7.5): inline pass on the title under the level
    color, realize the deferred line sentinels, then color. Never wrapped."""
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
    # A match reaching here is already dedented (the dispatcher owns indent,
    # §5.5): no indent stripping, no frame re-prefixing.
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
    # A mini-document (§7.1): recurse through the quote context narrowed by the
    # `│ ` bar, then only prefix chrome — the recursion returns final text (§6).
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
    """Render a maximal run of marker lines (design §7.2, in full): per line,
    the indent-width//2 cosmetic level and the bullet; then a remainder that IS
    a block recurses through `_m2a_render` and hangs aligned, while any other
    remainder keeps v1's combined bullet+content wrap path (byte-parity)."""
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
            if rendered:
                block_lines = rendered.split("\n")
                out_lines.append(bullet_prefix + block_lines[0])
                out_lines.extend(hang + out_ln for out_ln in block_lines[1:])
            else:
                out_lines.append(f"{'  ' * level}{styled}")
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

# One block rules table compiled into three alternations via the placement
# flags `root_only`/`in_quote` (design §5.1, §5.4): ROOT = every rule, NESTED =
# minus frontmatter, QUOTE = NESTED minus footnote_def. Rule order mirrors v1's
# combined grammar so block-boundary detection is identical (frontmatter before
# hr, etc.).

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


# Dispatch by block-rule name to a renderer taking `(match, state)`, binding
# each block's fixed arguments (color, code context, label).
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
    (design §5.2), each span extended through its trailing `\\n`; gaps yield as
    `("prose", None, start, end)`. One scanner, two consumers (§8)."""
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
    """Render a prose span (design §5.3): whole-span inline pass, then per
    output line realize the deferred sentinels and word-wrap. No paragraph
    reflow — each source line wraps independently."""
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
    floored at M2A_MIN_WIDTH (design §6); `wrap_width` stays 0 when it is 0.
    The single narrowing primitive for every kind of chrome."""
    wrap = max(M2A_MIN_WIDTH, state.wrap_width - cols) if state.wrap_width > 0 else 0
    line = max(M2A_MIN_WIDTH, state.line_width - cols)
    return replace(state, wrap_width=wrap, line_width=line)


def _m2a_leading_block(text, block_context):
    """The block-rule name `text` BEGINS with, or None if it is plain prose —
    routes a list marker-line remainder (§7.2)."""
    m = block_context.compiled.match(text)
    if m is None:
        return None
    return _m2a_first_group(m, block_context)


def _m2a_render_indented(m, indent, state, block_context):
    """Indentation as chrome (design §5.5, in full), applied once for every
    block kind: strip the literal first-line `indent` from each line carrying
    it, render narrowed by its visible width, re-prefix it onto every line."""
    dedented = "\n".join(
        ln[len(indent):] if ln.startswith(indent) else ln
        for ln in m.group(0).split("\n")
    )
    inner = _m2a_render(dedented, _m2a_narrow(state, len(indent.expandtabs(4))), block_context)
    return _m2a_prefix_lines(inner, indent)


def _m2a_render(text, state, block_context):
    """THE recursive entry (design §4): scan with block rules only, render each
    span with its renderer, reassemble by plain concatenation (§5.2). A block
    with a non-empty `indent` capture goes through indent-as-chrome (§5.5)."""
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
    """Convert Markdown text to ANSI-colored output (design §3; `md2ansi` is a
    v1-compatible alias). `line_width` > 0 enables word wrapping and sizes
    HR/tables, clamped up to M2A_MIN_WIDTH (§6); 0 (default) disables wrapping
    (HR falls back to a 150-char bar). `cell_min_width` is the table-column
    shrink floor; `row_dividers` is a tristate (None: only when a cell wraps;
    True/False: always/never)."""
    # Input sanitizer (design §6): normalize CRLF/CR to `\n`, then map every
    # remaining C0 control char except `\t`/`\n`/ESC to U+FFFD.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _M2A_C0_KILL.sub("�", text)
    # Style sanitizer: the base style must begin with reset `0` so every span's
    # close (which re-emits current_style) actually clears layered attributes.
    if current_style != "0" and not current_style.startswith("0;"):
        current_style = "0;" + current_style
    # §6: one width floor at every level INCLUDING root (an allowlisted
    # divergence from v1 at sub-20 root widths, §10.2).
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
# §8) — one scanner, two consumers, so the APIs cannot drift. Inline matches
# occur exactly in the prose gaps (block rules win at every line-start), so
# inline scanning runs the inline alternation over each gap.


@dataclass(frozen=True, slots=True)
class M2A_Span:
    """One span from `md2ansi_scan` (design §8): broad `kind`, narrow
    `subtype` (always populated), `is_block`, and `start`/`end` character
    offsets into the scanned text (`text[start:end] == text`)."""
    kind: str
    subtype: str
    is_block: bool
    start: int
    end: int
    text: str


# Outer-rule-name -> (kind, subtype) for rules whose classification differs
# from the fallback (kind == subtype == rule name).
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

# Broad-kind sets, derived from the rule tables (nothing hand-maintained).
# `prose` is deliberately NOT in the block set — gaps are opt-in (§8).
M2A_SPANS_INLINE = frozenset(
    _m2a_span_kind(name)[0] for name in _M2A_INLINE_RULE_NAMES
)
M2A_SPANS_BLOCK = frozenset(
    _m2a_span_kind(name)[0] for name, *_ in _M2A_BLOCK_RULES
)
M2A_SPANS_ALL = M2A_SPANS_BLOCK | M2A_SPANS_INLINE


def _m2a_scan(text, kinds):
    """Generator workhorse for `md2ansi_scan` (no validation): block spans and
    prose gaps from `_m2a_block_scan`, each gap re-scanned with the inline
    alternation, offsets mapped back into the source, in document order."""
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
    document order over the RAW source; flat, block spans tiling per §5.2/§8
    (opt in to gaps with `M2A_SPANS_BLOCK | {"prose"}`). `kinds` is validated
    eagerly: an unknown name raises `ValueError` before iteration."""
    unknown = set(kinds) - M2A_SPANS_ALL - {"prose"}
    if unknown:
        raise ValueError(
            f"md2ansi_scan: unknown span kind(s) {sorted(unknown)}; "
            f"valid kinds are {sorted(M2A_SPANS_ALL)} (plus 'prose')"
        )
    return _m2a_scan(text, frozenset(kinds))


# ### Section: Document ops #################################################

# Chapter-level document operations, specified in full by
# docs/superpowers/specs/2026-08-04-md-chapter-ops-design.md (all §-references
# below are to that spec). Document STRUCTURE comes exclusively from the public
# structural-scan API (`md2ansi_scan` / `M2A_Span`).


# Sigil strip — mirrors the heading grammar (`_MD_H1`..`_MD_H6`): optional
# leading indent, 1-6 `#`s, mandatory following whitespace. Anchored: only a
# LEADING sigil is a sigil.
_M2A_HEADING_SIGIL_RE = re.compile(r"\A[ \t]*\#{1,6}[ \t]+")
_M2A_WS_RUN_RE = re.compile(r"\s+")
# Display titles are already whitespace-collapsed, so one optional space per
# side of `/` is exact (§4 step 4).
_M2A_SLASH_SPACE_RE = re.compile(r" ?/ ?")


def _m2a_strip_inline(s):
    """One line of markdown with its inline markup stripped (§4 steps 2-3):
    render through the inline engine, strip SGR, sentinels to spaces, collapse
    whitespace. The throwaway `M2A_DocumentState` keeps footnote-ref side
    effects out of any real render."""
    rendered = _md2ansi(s, "0", M2A_CONTEXT_MD_INLINE, M2A_DocumentState())
    plain = _M2A_ANSI_ESCAPE_RE.sub("", rendered)
    for sentinel in (_M2A_LINEBREAK, _M2A_RULE, _M2A_NBSP):
        plain = plain.replace(sentinel, " ")
    return _M2A_WS_RUN_RE.sub(" ", plain).strip()


def md2ansi_display_title(line):
    """Display form of a heading line (§4 steps 1-3): sigil stripped, inline
    markup stripped via the renderer's own inline grammar, whitespace
    collapsed; case preserved. Harmless on non-heading text (step 1 no-ops),
    so it also titles `#text` body runs."""
    return _m2a_strip_inline(_M2A_HEADING_SIGIL_RE.sub("", line))


def md2ansi_match_title(line):
    """Match form of a heading line (§4 steps 1-4): the display title with
    spaces adjacent to `/` deleted. Case is preserved; matching is
    case-insensitive downstream, not here."""
    return _M2A_SLASH_SPACE_RE.sub("/", md2ansi_display_title(line))


@dataclass(slots=True)
class M2A_Node:
    """One node in a document's chapter tree — semantics in §3, field list in
    §9 (mirrors `md_doc.MdNode` field-for-field, plus `clean`, the §4 match
    title; offsets are character offsets, the "byte" naming kept for md_doc
    parity). A heading's `children` holds its `#text` run FIRST (always
    present, zero-width when the body is blank), then subchapter headings."""
    kind: str
    level: int
    title: str
    line_offset: int
    byte_offset: int
    byte_size: int
    clean: str
    children: list = field(default_factory=list)


@dataclass(slots=True)
class M2A_Doc:
    """The document model `md2ansi_doc` returns (§3, §9): `tree` (the root
    scope's preamble `#text` node first, then the top-level heading chapters),
    the `line_starts` index, `size` (= `len(text)`), and the `#frontmatter`
    region offsets (§3: virtual zero-width at 0 when absent). Build once and
    pass `doc=` to skip re-scanning."""
    tree: list
    line_starts: list
    size: int
    frontmatter_start: int
    frontmatter_end: int

    def line_of(self, offset):
        """0-based line number of the line containing character `offset`."""
        return _m2a_line_of(self.line_starts, offset)

    def offset_of(self, line):
        """Character offset of the start of 0-based `line`; a line one past
        the last returns `size` (the EOF insert point)."""
        return self.line_starts[line] if line < len(self.line_starts) else self.size


def _m2a_line_of(line_starts, offset):
    """0-based line number for `offset` given precomputed line starts —
    `bisect_right(line_starts, offset) - 1`, hand-rolled so the module stays
    on its existing imports."""
    lo, hi = 1, len(line_starts)
    while lo < hi:
        mid = (lo + hi) // 2
        if line_starts[mid] <= offset:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1


def _m2a_text_run(text, line_starts, scan_line, end_offset,
                  virtual_line, virtual_offset, level):
    """The §3 `#text` node for one scope: [first non-blank line at or after
    `scan_line`, `end_offset`), or the zero-width virtual point at
    (`virtual_line`, `virtual_offset`) when that window is all blank."""
    n = len(line_starts)
    line = scan_line
    while line < n and line_starts[line] < end_offset:
        start = line_starts[line]
        stop = line_starts[line + 1] if line + 1 < n else end_offset
        body = text[start:stop]
        if body.strip():
            # No sigil strip (§6.4): a `#`-led body line only occurs inside a
            # fence/quote and is not a sigil.
            title = _m2a_strip_inline(body.rstrip("\n"))
            return M2A_Node(
                kind="text",
                level=level,
                title=title,
                line_offset=line,
                byte_offset=start,
                byte_size=end_offset - start,
                clean=_M2A_SLASH_SPACE_RE.sub("/", title),
            )
        line += 1
    return M2A_Node(
        kind="text",
        level=level,
        title="",
        line_offset=virtual_line,
        byte_offset=virtual_offset,
        byte_size=0,
        clean="",
    )


def md2ansi_doc(text):
    """Build the `M2A_Doc` document model (§3): heading nodes from
    `md2ansi_scan` with extents by the boundary rule, level-stack tree
    linking, `#text` runs and the `#frontmatter` region. Offsets index the
    text exactly as passed — no normalization — so extents slice the raw
    source verbatim."""
    line_starts = [0] + [m.end() for m in re.finditer(r"\n", text)]
    size = len(text)

    # --- Scan: heading events + the frontmatter span ---
    fm_start = fm_end = 0
    events = []  # (level, byte_offset, line_offset, raw heading line)
    for span in md2ansi_scan(text, kinds=frozenset({"heading", "frontmatter"})):
        if span.kind == "frontmatter":
            fm_start, fm_end = span.start, span.end  # fm_start is always 0 (\A)
            continue
        events.append((
            int(span.subtype[1]),  # 'h3' -> 3
            span.start,
            _m2a_line_of(line_starts, span.start),
            span.text.rstrip("\n"),
        ))

    # --- Heading nodes with extents (boundary rule) ---
    nodes = []
    for i, (level, offset, line, raw) in enumerate(events):
        end = size
        for next_level, next_offset, _line, _raw in events[i + 1:]:
            if next_level <= level:
                end = next_offset
                break
        nodes.append(M2A_Node(
            kind="heading",
            level=level,
            title=md2ansi_display_title(raw),
            line_offset=line,
            byte_offset=offset,
            byte_size=end - offset,
            clean=md2ansi_match_title(raw),
        ))

    # --- Tree linking via the level stack ---
    roots = []
    stack = []  # innermost open heading last
    for node in nodes:
        while stack and stack[-1].level >= node.level:
            stack.pop()
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)

    # --- `#text` runs: one per heading, inserted as its first child ---
    # Safe in flat order: a node's text run lands in ITS children list, so
    # every list is still heading-only at the moment it is read.
    for node in nodes:
        first_sub = node.children[0] if node.children else None
        end = first_sub.byte_offset if first_sub else node.byte_offset + node.byte_size
        after_line = node.line_offset + 1
        after_offset = line_starts[after_line] if after_line < len(line_starts) else size
        node.children.insert(0, _m2a_text_run(
            text, line_starts, after_line, end, after_line, after_offset,
            first_sub.level if first_sub else node.level + 1,
        ))

    # --- Root scope: the preamble as tree[0] ---
    # Content starts on the first line at-or-after the frontmatter's end
    # (which is also the virtual insert point — offset 0 with no frontmatter).
    pre_line = _m2a_line_of(line_starts, fm_end)
    if line_starts[pre_line] < fm_end:
        pre_line += 1  # frontmatter ended mid-line (unterminated final line)
    pre_end = nodes[0].byte_offset if nodes else size
    preamble = _m2a_text_run(
        text, line_starts, pre_line, pre_end, pre_line, fm_end,
        roots[0].level if roots else 1,
    )

    return M2A_Doc(
        tree=[preamble] + roots,
        line_starts=line_starts,
        size=size,
        frontmatter_start=fm_start,
        frontmatter_end=fm_end,
    )


# --- Addressing (§5): query DSL, regex construction, resolution -----------

# The §5.1 fragment grammar, whole-string. Keyword fragments are lowercase;
# hash digits accept either case (normalized to lower at parse). Alternation
# order does not matter — `\Z` forces backtracking into the right branch
# (`12-15` falls through the bare-int branch into the range branch).
_M2A_FRAGMENT_RE = re.compile(
    r"\A(?:(?P<text>text)|(?P<frontmatter>frontmatter)"
    r"|(?P<line>\d+)|(?P<a>\d+)-(?P<b>\d+)"
    r"|h(?P<hlevel>[1-6]):(?P<hline>\d+)"
    r"|hash:(?P<hash>[0-9a-fA-F]{4,64}))\Z"
)


def _m2a_parse_fragment(frag, query):
    """Parse the (non-empty) text after a query's last `#` into a tagged
    tuple: `('text',)` | `('frontmatter',)` | `('line', n)` |
    `('range', a, b)` | `('hlevel', level, n)` | `('hash', lower_prefix)`.
    Anything else raises — a fragment or an error, never path text (§5.1)."""
    m = _M2A_FRAGMENT_RE.match(frag)
    if not m:
        raise ValueError(
            f"invalid query {query!r}: {frag!r} after the last '#' is not a "
            f"fragment (expected text | frontmatter | <line> | <a>-<b> | "
            f"h<1-6>:<line> | hash:<4-64 hex>); to address a title that "
            f"contains '#', protect it with a trailing '#'"
        )
    if m.group("text"):
        return ("text",)
    if m.group("frontmatter"):
        return ("frontmatter",)
    if m.group("line"):
        return ("line", int(m.group("line")))
    if m.group("a"):
        a, b = int(m.group("a")), int(m.group("b"))
        if a > b:
            raise ValueError(
                f"invalid query {query!r}: range #{a}-{b} has a > b"
            )
        return ("range", a, b)
    if m.group("hlevel"):
        return ("hlevel", int(m.group("hlevel")), int(m.group("hline")))
    return ("hash", m.group("hash").lower())


def _m2a_query_split(query):
    """Split a query on its LAST `#` (§5.1) into `(path_part, fragment)`,
    the fragment parsed or None (a trailing `#` is the protector — empty
    fragment ≡ no fragment). An empty query, or `#` alone, is an error."""
    if not query:
        raise ValueError(
            "invalid query: empty (expected a path, 'PATH#fragment', "
            "or '#fragment')"
        )
    cut = query.rfind("#")
    if cut < 0:
        return query, None
    path_part, frag = query[:cut], query[cut + 1:]
    if not path_part and not frag:
        raise ValueError(
            "invalid query '#': a bare '#' names nothing (expected "
            "'#<fragment>', a path, or a 'PATH#' protector)"
        )
    return path_part, (_m2a_parse_fragment(frag, query) if frag else None)


def _m2a_path_pattern(path):
    """The §5.2 regex (pattern string) for a query path, built exactly by the
    spec's rules — matched with `re.search` + `IGNORECASE` against every
    heading's path string. The root path `/` never reaches here (the caller
    short-circuits it); a trailing empty component is an error."""
    anchored = path.startswith("/")
    # Strip every component up front so a whitespace-only component is
    # empty EVERYWHERE below — in particular for the trailing-empty check,
    # which must reject 'One/ ' exactly like 'One/'.
    comps = [c.strip() for c in (path[1:] if anchored else path).split("/")]
    if comps[-1] == "":
        if len(comps) == 1:
            raise ValueError(f"invalid query {path!r}: empty path")
        raise ValueError(
            f"invalid query {path!r}: trailing empty component (a wildcard "
            f"is only valid BETWEEN components, as in 'AAA//CCC')"
        )
    pieces = []
    for i, comp in enumerate(comps):
        if not comp:
            pieces.append(".*")
            continue
        lead = comp.startswith("^")
        if lead:
            comp = comp[1:]
        trail = comp.endswith("$")
        if trail:
            comp = comp[:-1]
        # re.escape escapes spaces (`\ `); the spec's contract (and example
        # table) is a literal space, so un-escape them — safe because the
        # collapse just reduced every whitespace run to a single space.
        body = re.escape(_M2A_WS_RUN_RE.sub(" ", comp)).replace("\\ ", " ")
        head = ("(^|/)" if i == 0 and not anchored else "") if lead else "[^/]*"
        pieces.append(head + body + ("" if trail else "[^/]*"))
    return ("^" if anchored else "") + "/".join(pieces) + "$"


def _m2a_extent_hash(extent):
    """Full sha256 hex digest of the EXACT extent string, UTF-8 (§8).
    Display form is the first 12 digits; verification accepts any 4-64-hex
    prefix."""
    import hashlib
    return hashlib.sha256(extent.encode("utf-8")).hexdigest()


# Standalone `hash_prefix` verifier arguments (§6.3 `--hash`) obey the same
# shape as the `#hash:` fragment: 4-64 hex digits, either case (normalized to
# lower before comparing against the lowercase hexdigest).
_M2A_HASH_PREFIX_RE = re.compile(r"\A[0-9a-fA-F]{4,64}\Z")


@dataclass(frozen=True, slots=True)
class M2A_Target:
    """A resolved address (§5, §9) — what `md2ansi_resolve` returns and
    extract/splice consume: `kind` ('heading'|'root'|'text'|'frontmatter'|
    'range'), the §4 `path` string ('/' for root-scope targets), `start`/`end`
    character offsets (equal for virtual zero-width regions), the §8 display
    `hash`, and the underlying `node` (None where no node exists)."""
    kind: str
    path: str
    start: int
    end: int
    hash: str
    node: Any


def _m2a_make_target(text, kind, path, start, end, node=None):
    """Assemble an `M2A_Target`, hashing the region it covers."""
    return M2A_Target(kind, path, start, end,
                      _m2a_extent_hash(text[start:end])[:12], node)


def _m2a_walk_paths(nodes, prefix=""):
    """Yield `(path string, node)` for every heading node, depth-first in
    document order — the candidate universe of §5.3."""
    for node in nodes:
        if node.kind != "heading":
            continue
        path = f"{prefix}/{node.clean}" if prefix else node.clean
        yield path, node
        yield from _m2a_walk_paths(node.children, path)


def _m2a_line_exists(doc, line):
    """True iff 1-based `line` is a line of the document (its start offset
    lies strictly inside the text; the phantom empty position after a final
    newline is NOT a line)."""
    return 1 <= line <= len(doc.line_starts) and doc.line_starts[line - 1] < doc.size


def _m2a_contains(node, offset):
    """True iff `offset` falls inside the node's extent."""
    return node.byte_offset <= offset < node.byte_offset + node.byte_size


def _m2a_node_lines(doc, node):
    """1-based inclusive line range of a heading node's extent."""
    first = node.line_offset + 1
    last = doc.line_of(node.byte_offset + node.byte_size - 1) + 1
    return first, last


def _m2a_candidate_listing(text, doc, candidates):
    """Error-message listing of resolution candidates (§5.3, §7): one
    `<path> (lines a-b, hash <12hex>)` line per match, copy-pasteable back
    as a `#<a>-<b>` or `#hash:` query."""
    return "\n".join(
        "  {} (lines {}-{}, hash {})".format(
            path, *_m2a_node_lines(doc, node),
            _m2a_extent_hash(
                text[node.byte_offset:node.byte_offset + node.byte_size]
            )[:12])
        for path, node in candidates
    )


def _m2a_heading_target(text, path, node):
    """`M2A_Target` for a resolved heading node (its full extent)."""
    return _m2a_make_target(text, "heading", path, node.byte_offset,
                            node.byte_offset + node.byte_size, node)


def _m2a_text_target(text, path, run):
    """`M2A_Target` for a `#text` body run (zero-width when virtual)."""
    return _m2a_make_target(text, "text", path, run.byte_offset,
                            run.byte_offset + run.byte_size, run)


def _m2a_resolve_bare(text, doc, query, frag):
    """§5.4 direct references — `#<fragment>` with no path, a lookup against
    the document as a whole (no path matching at all)."""
    tag = frag[0]
    if tag == "text":
        return _m2a_text_target(text, "/", doc.tree[0])
    if tag == "frontmatter":
        return _m2a_make_target(text, "frontmatter", "/",
                                doc.frontmatter_start, doc.frontmatter_end)
    if tag == "range":
        a, b = frag[1], frag[2]
        if not (_m2a_line_exists(doc, a) and _m2a_line_exists(doc, b)):
            n = sum(1 for s in doc.line_starts if s < doc.size)
            raise ValueError(
                f"not found: range #{a}-{b} is out of the document "
                f"({n} line(s); lines are 1-based)"
            )
        return _m2a_make_target(text, "range", "/",
                                doc.offset_of(a - 1), doc.offset_of(b))
    if tag == "hash":
        prefix = frag[1]
        matches = [
            (p, n) for p, n in _m2a_walk_paths(doc.tree)
            if _m2a_extent_hash(
                text[n.byte_offset:n.byte_offset + n.byte_size]
            ).startswith(prefix)
        ]
        if not matches:
            raise ValueError(
                f"not found: no chapter extent hash starts with {prefix!r} "
                f"(hashes bind to the exact extent bytes — any edit changes "
                f"them; re-list to get fresh hashes)"
            )
        if len(matches) > 1:
            raise ValueError(
                f"ambiguous query {query!r}: hash prefix {prefix!r} matches "
                f"{len(matches)} chapters:\n"
                f"{_m2a_candidate_listing(text, doc, matches)}\n"
                f"use a longer prefix"
            )
        return _m2a_heading_target(text, *matches[0])
    # tag == "line" | "hlevel": the smallest / the level-N chapter whose
    # extent contains the line (heading nodes only).
    line = frag[1] if tag == "line" else frag[2]
    if not _m2a_line_exists(doc, line):
        n = sum(1 for s in doc.line_starts if s < doc.size)
        raise ValueError(
            f"not found: line {line} is not in the document "
            f"({n} line(s); lines are 1-based)"
        )
    offset = doc.offset_of(line - 1)
    if tag == "line":
        containing = [(p, n) for p, n in _m2a_walk_paths(doc.tree)
                      if _m2a_contains(n, offset)]
        if not containing:
            hint = ("the document has no headings" if len(doc.tree) == 1
                    else "it lies before the first heading — '#text' or a "
                         "'#<a>-<b>' range addresses the preamble")
            raise ValueError(
                f"not found: no chapter's extent contains line {line} "
                f"({hint})"
            )
        # Containing extents nest, so the smallest is the innermost.
        return _m2a_heading_target(
            text, *min(containing, key=lambda pn: pn[1].byte_size))
    level = frag[1]
    matches = [(p, n) for p, n in _m2a_walk_paths(doc.tree)
               if n.level == level and _m2a_contains(n, offset)]
    if not matches:
        raise ValueError(
            f"not found: no h{level} chapter's extent contains line {line}"
        )
    # Same-level extents never overlap: unique or none.
    return _m2a_heading_target(text, *matches[0])


def _m2a_verify(text, doc, node, frag):
    """§5.4 with-path fragment verifier, applied per candidate BEFORE the
    uniqueness check (so it doubles as a disambiguator): line containment,
    level + line containment, or extent-hash prefix."""
    tag = frag[0]
    if tag == "line":
        return (_m2a_line_exists(doc, frag[1])
                and _m2a_contains(node, doc.offset_of(frag[1] - 1)))
    if tag == "hlevel":
        return (node.level == frag[1] and _m2a_line_exists(doc, frag[2])
                and _m2a_contains(node, doc.offset_of(frag[2] - 1)))
    # tag == "hash"
    extent = text[node.byte_offset:node.byte_offset + node.byte_size]
    return _m2a_extent_hash(extent).startswith(frag[1])


def _m2a_reject_bare_only(query, frag):
    """§5.4: `#<a>-<b>` and `#frontmatter` are bare-only fragments — an
    error when combined with a path (including the root path `/`)."""
    if frag is not None and frag[0] in ("range", "frontmatter"):
        bare = query[query.rfind("#"):]
        raise ValueError(
            f"invalid query {query!r}: {bare!r} is bare-only — it cannot "
            f"be combined with a path (use {bare!r} alone)"
        )


def _m2a_path_candidates(text, doc, query, path_part, frag, regex):
    """The surviving candidates of a path query (§5.3-5.4): pattern-match
    over every heading's path string, then apply the with-path fragment
    verifier. Returns a non-empty `(path, node)` list; zero survivors raise
    the §7 error classes."""
    pattern = path_part if regex else _m2a_path_pattern(path_part)
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(
            f"invalid query {query!r}: bad regex {pattern!r}: {exc}"
        ) from None
    candidates = [(p, n) for p, n in _m2a_walk_paths(doc.tree)
                  if compiled.search(p)]

    if frag is not None and frag[0] != "text" and candidates:
        kept = [(p, n) for p, n in candidates
                if _m2a_verify(text, doc, n, frag)]
        if not kept:
            kind = "hash mismatch" if frag[0] == "hash" else "not found"
            raise ValueError(
                f"{kind}: fragment {query[query.rfind('#'):]!r} eliminates "
                f"every path candidate of {query!r}:\n"
                f"{_m2a_candidate_listing(text, doc, candidates)}"
            )
        candidates = kept

    if not candidates:
        hint = "" if regex else f" (matched as regex {pattern!r})"
        raise ValueError(
            f"not found: no chapter path matches {query!r}{hint}; "
            f"matching is case-insensitive over match-title paths"
        )
    return candidates


def md2ansi_resolve(text, query, doc=None, regex=False):
    """Resolve `query` against `text` to an `M2A_Target`, by the full §5
    contract: query shapes and the last-`#` split (§5.1), path-to-regex
    matching over heading path strings (§5.2), exactly-one-candidate
    resolution (§5.3), fragments as direct references or per-candidate
    verifiers (§5.4), and `regex=True` raw-pattern mode (§5.5). `doc=` takes
    the `md2ansi_doc(text)` of the SAME text to skip re-scanning. Every
    failure raises `ValueError` (§7)."""
    if doc is None:
        doc = md2ansi_doc(text)
    path_part, frag = _m2a_query_split(query)

    if not path_part:
        return _m2a_resolve_bare(text, doc, query, frag)

    _m2a_reject_bare_only(query, frag)

    if not regex and path_part == "/":
        if frag is not None and frag[0] == "text":
            # §5.4: '#text' with a path selects the chapter's text run —
            # root's is the preamble, identical to bare '#text'.
            return _m2a_text_target(text, "/", doc.tree[0])
        # §5.1: '/#<verifier>' is valid but useless — a verifier on root is
        # vacuous, so it just resolves to root.
        return _m2a_make_target(text, "root", "/", 0, doc.size)

    candidates = _m2a_path_candidates(text, doc, query, path_part, frag, regex)
    if len(candidates) > 1:
        raise ValueError(
            f"ambiguous query {query!r}: {len(candidates)} candidates:\n"
            f"{_m2a_candidate_listing(text, doc, candidates)}\n"
            f"be more specific ('^'/'$' anchors, a fuller or /-anchored "
            f"path, a '#<line>' fragment, or paste a listed '#<a>-<b>' / "
            f"'#hash:' form)"
        )

    path, node = candidates[0]
    if frag is not None and frag[0] == "text":
        return _m2a_text_target(text, path, node.children[0])
    return _m2a_heading_target(text, path, node)


# --- Operations (§6): extract and splice -----------------------------------


def md2ansi_extract(text, query, doc=None, regex=False):
    """Extract the target of `query` (§6.2): resolve it, return the exact
    region `text[t.start:t.end]` verbatim — an empty (possibly virtual)
    region extracts as `""`, NOT an error. `doc=`/`regex=` pass through to
    `md2ansi_resolve`, whose `ValueError`s (§7) are the only failures."""
    t = md2ansi_resolve(text, query, doc=doc, regex=regex)
    return text[t.start:t.end]


def md2ansi_splice(text, query, content, where="replace", hash_prefix=None,
                   doc=None, regex=False):
    """Splice `content` into `text` at the target of `query`, returning the
    new full document text — raw string surgery on the resolved offsets, so
    untouched characters survive byte-for-byte; file I/O stays in the CLI.
    `where` ∈ replace|before|after|first|last per the §6.3 table; the §6.1
    trailing-newline rule applies (one LF ensured, empty content excepted,
    nothing else massaged — no releveling). `hash_prefix` is the §6.3/§8
    replace-only safeguard against the CURRENT extent's sha256. `doc=`/
    `regex=` pass through to `md2ansi_resolve`; every failure raises
    `ValueError` (§7)."""
    if where not in ("replace", "before", "after", "first", "last"):
        raise ValueError(
            f"invalid where {where!r} (expected replace | before | after "
            f"| first | last)"
        )
    if hash_prefix is not None:
        if where != "replace":
            raise ValueError(
                f"invalid hash_prefix with where={where!r}: the hash "
                f"safeguard verifies the extent being OVERWRITTEN, so it "
                f"is only valid with where='replace'"
            )
        if not _M2A_HASH_PREFIX_RE.match(hash_prefix):
            raise ValueError(
                f"invalid hash prefix {hash_prefix!r} (expected 4-64 hex "
                f"digits)"
            )
    if doc is None:
        doc = md2ansi_doc(text)
    t = md2ansi_resolve(text, query, doc=doc, regex=regex)
    if where == "first" and t.kind not in ("heading", "root"):
        raise ValueError(
            f"invalid where='first' on the {t.kind} target of {query!r}: "
            f"no child position exists (only a chapter or the root '/' has "
            f"one); use 'before'/'after'/'replace'"
        )
    if hash_prefix is not None:
        current = _m2a_extent_hash(text[t.start:t.end])
        if not current.startswith(hash_prefix.lower()):
            raise ValueError(
                f"hash mismatch: {hash_prefix!r} is not a prefix of the "
                f"current extent's hash ({current[:12]}) — the extent "
                f"changed since it was extracted; input untouched "
                f"(re-extract to get a fresh hash)"
            )
    if content and not content.endswith("\n"):
        content += "\n"
    if where == "replace":
        start, end = t.start, t.end
    elif where == "before":
        start = end = t.start
    elif where == "first":
        # The `#text` run: `tree[0]` (the preamble) for root, else the
        # heading node's first child (§3) — zero-width when the body is
        # blank, so the insert point degenerates to just after the heading.
        run = doc.tree[0] if t.kind == "root" else t.node.children[0]
        start = end = run.byte_offset + run.byte_size
    else:  # 'after' | 'last' — same offset by design (§6.3)
        start = end = t.end
    return text[:start] + content + text[end:]


# --- CLI (§6.4, §7, §10) ---------------------------------------------------

# The doc-op verbs, by argparse dest. Any of them present selects doc-op
# mode; with none, the CLI is the v1-compatible renderer. Every option is
# regular (§10): `--hash` is a valueless flag everywhere it appears, the
# --replace verifier is the value-taking --check-hash.
_M2A_CLI_VERBS = ("list", "extract", "before", "after", "first", "last",
                  "replace")

# The per-verb options, as (canonical spelling, argparse dest): argparse
# accepts any declared flag with any verb, so the per-verb sets below are
# enforced post-parse — for the §7 error messages, not for parsing.
_M2A_CLI_VERB_OPTS = (
    ("--path", "path"), ("--line", "line"), ("--hash", "hash_col"),
    ("-o/--output", "output"), ("--from", "src"),
    ("--check-hash", "check_hash"),
)
_M2A_CLI_ALLOWED = {
    "list": {"--path", "--line", "--hash"},
    "extract": {"-o/--output", "--hash"},
    "before": {"--from"},
    "after": {"--from"},
    "first": {"--from"},
    "last": {"--from"},
    "replace": {"--from", "--check-hash"},
}


class _M2A_UsageError(Exception):
    """CLI usage error (§7): exit 2, message + synopsis on stderr."""


def _m2a_cli_parser():
    """The §10 argparse parser — one parser for both modes, regular parsing
    rules only. Parse failures raise `_M2A_UsageError` instead of exiting:
    the §7 exit codes stay with `_m2a_cli_main` (whose -h interception makes
    the help action practically unreachable — combined short flags like
    `-vh` still reach it, with the same help-on-stdout/exit-0 outcome)."""
    import argparse

    class _M2A_Parser(argparse.ArgumentParser):
        def error(self, message):
            raise _M2A_UsageError(message)

    p = _M2A_Parser(
        prog="md2ansi_lib.py",
        usage="""\
%(prog)s [FILE ...]                    render to ANSI (LINE_WIDTH env)
       %(prog)s --list [--path] [--line] [--hash] [FILE]
       %(prog)s --extract Q [-o OUT] [--hash] [FILE]
       %(prog)s (--before|--after|--first|--last) Q --from SRC FILE
       %(prog)s --replace Q [--check-hash HEX] --from SRC FILE""",
        description="""\
With no verb flag, FILE args (or stdin) render to ANSI, width from the
LINE_WIDTH env var; render mode takes no options. A verb flag selects
doc-op mode. Options and positionals may come in any order; '--' ends
options (every later argument is a FILE even if it looks like an option).
FILE/SRC '-' = stdin (write verbs edit FILE in place, real file only).""",
        epilog="""\
queries: 'Guide/Setup' (title path), '/One' (root-anchored), 'Setup#text',
'#frontmatter', '#12' (line), '#12-20' (line range), 'Setup#h2:14',
'Setup#hash:ab12'. Full query semantics:
docs/superpowers/specs/2026-08-04-md-chapter-ops-design.md""",
        formatter_class=argparse.RawTextHelpFormatter,
        allow_abbrev=False,
    )
    verbs = p.add_mutually_exclusive_group()
    verbs.add_argument(
        "--list", action="store_true",
        help="list the structure, indented, each row tagged with its\n"
             "kind — [h1]..[h6] headings, [frontmatter] when present,\n"
             "[text] for chapters with both a body and subchapters\n"
             "(filter: grep over '--list --path --line'; test a query\n"
             "with '--extract Q -v')")
    verbs.add_argument(
        "--extract", metavar="Q",
        help="write the addressed extent to stdout, verbatim")
    for flag, where in (("--before", "before"), ("--after", "after"),
                        ("--first", "as the first child of"),
                        ("--last", "as the last child of")):
        verbs.add_argument(
            flag, metavar="Q",
            help=f"insert the content of --from SRC {where} the\n"
                 f"addressed extent (FILE is edited in place)")
    verbs.add_argument(
        "--replace", metavar="Q",
        help="replace the addressed extent with the content of --from\n"
             "SRC (FILE is edited in place)")
    p.add_argument(
        "--path", action="store_true",
        help="--list: one node per line, as its full title path")
    p.add_argument(
        "--line", action="store_true",
        help="--list: append a #A-B line-range column (pastes back\n"
             "as a query)")
    p.add_argument(
        "--hash", action="store_true", dest="hash_col",
        help="--list: append a #hash:<12hex> extent-hash column\n"
             "(pastes back as a query)\n"
             "--extract: print the extent's bare 12-hex hash to stderr,\n"
             "flushed first (leads merged 2>&1 output without -v)")
    p.add_argument(
        "-o", "--output", metavar="OUT",
        help="--extract: write the extent to OUT instead ('-' = stdout)")
    p.add_argument(
        "--from", metavar="SRC", dest="src",
        help="write verbs: the content source (a file, '-' = stdin)")
    p.add_argument(
        "--check-hash", metavar="HEX", dest="check_hash",
        help="--replace: first verify HEX (4-64 hex digits) is a prefix\n"
             "of the current extent's sha256")
    p.add_argument(
        "-f", "--file", metavar="FILE",
        help="the document to operate on (or give FILE positionally)")
    p.add_argument(
        "--regex", action="store_true",
        help="query components are regular expressions")
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="stderr diagnostics: how the query parsed (compiled regex,\n"
             "fragment) and what resolved (lines, hash)")
    p.add_argument(
        "files", nargs="*", metavar="FILE",
        help="the document (doc-op mode: at most one; render mode: any)")
    return p


def _m2a_cli_read(path):
    """Read a document or content source with NO newline translation (§6.1
    — a CRLF document must survive the round trip byte-for-byte). `None` or
    `'-'` is stdin, read as raw bytes for the same reason (text-mode stdin
    would translate)."""
    import sys
    if path is None or path == "-":
        return sys.stdin.buffer.read().decode("utf-8")
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _m2a_cli_write_in_place(path, new_text):
    """The §6.1 in-place write: temp file in the SAME directory +
    `os.replace`, `newline=''` so the spliced text goes out verbatim. The
    temp file takes over the target's permission bits — mkstemp creates
    0600, and without the chmod every edited file would silently tighten
    to that."""
    import os
    import tempfile
    mode = os.stat(path).st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)),
                               prefix=os.path.basename(path) + ".")
    try:
        os.chmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _m2a_cli_query_diag(query, regex):
    """The `-v` query diagnostic (§6.1): one stderr line showing how the
    query parsed, emitted at query-compile time, BEFORE any resolution,
    output, or error. A query the splitter or the §5.2 builder rejects
    raises from here instead — the same invalid-query error resolution
    would raise, just earlier."""
    import sys
    path_part, frag = _m2a_query_split(query)
    fragdesc = (f"fragment {query[query.rfind('#'):]}" if frag is not None
                else "no fragment")
    if not path_part:
        desc = "direct reference"
    elif not regex and path_part == "/":
        desc = "root scope"
    else:
        pattern = path_part if regex else _m2a_path_pattern(path_part)
        desc = f"{'raw regex' if regex else 'regex'} {pattern} (IGNORECASE)"
    sys.stderr.write(f"md2ansi: query {query!r} -> {desc}, {fragdesc}\n")


def _m2a_cli_echo(doc, t, new_hash=None):
    """The `-v` stderr echo (§6.1): what resolved — display form, 1-based
    line range (or the zero-width insert point, §6.2), extent hash — and
    for writes the new extent's hash (the hash of the spliced content,
    §8: what a later `--replace --check-hash` verifies)."""
    import sys
    if t.kind == "text":
        disp = "/#text" if t.path == "/" else f"{t.path}#text"
    elif t.kind == "frontmatter":
        disp = "#frontmatter"
    elif t.kind == "range":
        disp = f"#{doc.line_of(t.start) + 1}-{doc.line_of(t.end - 1) + 1}"
    else:
        disp = t.path
    if t.end > t.start:
        span = "lines {}-{}".format(doc.line_of(t.start) + 1,
                                    doc.line_of(t.end - 1) + 1)
    else:
        span = f"zero-width at line {doc.line_of(t.start) + 1}"
    line = f"md2ansi: {disp}: {span}, hash {t.hash}"
    if new_hash is not None:
        line += f" -> new hash {new_hash}"
    sys.stderr.write(line + "\n")


def _m2a_cli_frontmatter_row(text, doc):
    """The §6.4 frontmatter row, or None: a synthetic node over the
    `#frontmatter` region, listed whenever the block exists and is
    non-empty — a virtual/empty region is never listed, like any other.
    Titled by the first non-blank line INSIDE the block (the fences are
    delimiters, not content; empty when the block holds only its fences)."""
    if doc.frontmatter_end <= doc.frontmatter_start:
        return None
    block = text[doc.frontmatter_start:doc.frontmatter_end]
    title = next((ln for ln in block.splitlines()[1:]
                  if ln.strip() and ln.strip() != "---"), "")
    return M2A_Node(kind="frontmatter", level=0, title=title,
                    line_offset=doc.line_of(doc.frontmatter_start),
                    byte_offset=doc.frontmatter_start,
                    byte_size=doc.frontmatter_end - doc.frontmatter_start,
                    clean="")


def _m2a_cli_rows(doc, fm):
    """The listable rows of §6.4 in document order, as `(node, depth,
    parents)` tuples — `parents` the heading chain from the top level down
    (a text row's chain ends with its OWN heading; empty for top-level
    headings, the frontmatter row, and the preamble)."""
    rows = [(fm, 0, ())] if fm is not None else []

    def rec(nodes, depth, parents):
        for node in nodes:
            if node.kind != "heading":
                continue
            rows.append((node, depth, parents))
            chain = parents + (node,)
            run = node.children[0]
            if len(node.children) > 1 and run.byte_size > 0:
                rows.append((run, depth + 1, chain))
            rec(node.children, depth + 1, chain)

    preamble = doc.tree[0]
    if len(doc.tree) > 1 and preamble.byte_size > 0:
        rows.append((preamble, 0, ()))
    rec(doc.tree, 0, ())
    return rows


def _m2a_cli_list(text, doc, args):
    """The --list verb (§6.4) — no query: indented `[kind]`-tagged rows, or
    `--path` full-path rows; the `--line`/`--hash` columns append
    tab-separated in fixed order. Filtering is grep's job (§6.4)."""
    import sys
    out = sys.stdout

    def columns(label, start, end):
        # Both columns are printed in #-address form so a value pastes back
        # verbatim as a query for that row's extent (§6.4).
        cols = [label]
        if args.line:
            cols.append("#{}-{}".format(doc.line_of(start) + 1,
                                        doc.line_of(max(end - 1, start)) + 1))
        if args.hash_col:
            cols.append("#hash:" + _m2a_extent_hash(text[start:end])[:12])
        out.write("\t".join(cols) + "\n")

    fm = _m2a_cli_frontmatter_row(text, doc)
    for node, depth, parents in _m2a_cli_rows(doc, fm):
        if args.path:
            if node.kind == "frontmatter":
                label = "#frontmatter"
            elif node.kind == "text":
                parent_path = "/".join(p.title for p in parents)
                label = f"{parent_path}#text" if parent_path else "/#text"
            else:
                label = "/".join([p.title for p in parents] + [node.title])
        else:
            tag = f"h{node.level}" if node.kind == "heading" else node.kind
            label = "  " * depth + f"[{tag}] {node.title}"
        columns(label, node.byte_offset, node.byte_offset + node.byte_size)
    return 0


def _m2a_cli_doc_op(verb, args):
    """Validate a parsed doc-op invocation (§10) and execute it (§6). The
    post-parse checks keep the §7 CLI strictness argparse cannot express
    (per-verb option sets, FILE given twice, write-verb requirements).
    Returns 0; errors propagate to `_m2a_cli_main`."""
    import sys
    for flag, dest in _M2A_CLI_VERB_OPTS:
        if (getattr(args, dest) not in (None, False)
                and flag not in _M2A_CLI_ALLOWED[verb]):
            hint = (" (the --replace verifier is --check-hash HEX)"
                    if verb == "replace" and flag == "--hash" else "")
            raise _M2A_UsageError(
                f"option {flag} does not apply to --{verb}{hint}")
    file = args.file
    if args.files:
        if len(args.files) > 1:
            raise _M2A_UsageError(f"unexpected argument {args.files[1]!r}")
        if file is not None:
            # The Q-placement hint only fits the Q verbs: --list takes no
            # query at all (§6.4), so there is nothing to misplace.
            hint = ("" if verb == "list" else
                    f" — a query is the value of its verb flag (--{verb} Q)")
            raise _M2A_UsageError(
                f"file given twice ({file!r} and {args.files[0]!r}){hint}")
        file = args.files[0]
    if verb not in ("list", "extract"):
        if args.src is None:
            raise _M2A_UsageError(f"--{verb} requires --from SRC")
        if file is None:
            raise _M2A_UsageError(
                f"--{verb} requires a FILE argument (write verbs edit "
                f"the file in place)")
        if file == "-":
            raise _M2A_UsageError(
                f"--{verb} requires a real FILE (stdin is not writable "
                f"in place)")

    text = _m2a_cli_read(file)
    doc = md2ansi_doc(text)
    if verb == "list":
        # --list has no query, hence nothing to diagnose under -v (§6.4).
        return _m2a_cli_list(text, doc, args)
    query = getattr(args, verb)
    if args.verbose:
        _m2a_cli_query_diag(query, args.regex)
    target = md2ansi_resolve(text, query, doc=doc, regex=args.regex)
    if verb == "extract":
        extent = text[target.start:target.end]
        if args.hash_col:
            sys.stderr.write(_m2a_extent_hash(extent)[:12] + "\n")
        if args.verbose:
            _m2a_cli_echo(doc, target)
        if args.hash_col or args.verbose:
            # Flush BEFORE the first content byte: stdout is block-buffered
            # under redirection and held until the exit-time flush, so this
            # is what puts the hash line (then the echo) ahead of the
            # content in merged (2>&1) output.
            sys.stderr.flush()
        if args.output in (None, "-"):
            sys.stdout.write(extent)
        else:
            # A NEW file, not an in-place edit — plain create/overwrite,
            # but the same newline='' discipline (byte-exact, §6.1).
            with open(args.output, "w", encoding="utf-8", newline="") as f:
                f.write(extent)
        return 0
    content = _m2a_cli_read(args.src)
    new_text = md2ansi_splice(text, query, content, where=verb,
                              hash_prefix=args.check_hash, doc=doc,
                              regex=args.regex)
    _m2a_cli_write_in_place(file, new_text)
    if args.verbose:
        if content and not content.endswith("\n"):
            content += "\n"  # mirror the §6.1 terminator the splice added
        _m2a_cli_echo(doc, target, _m2a_extent_hash(content)[:12])
    return 0


def _m2a_cli_render(files):
    """The default verb: render the FILE arguments (or stdin; bare '-' also
    reads stdin) to ANSI, width from the LINE_WIDTH env var —
    v1-compatible, byte-for-byte (§10)."""
    import os
    import sys
    line_width = int(os.environ["LINE_WIDTH"]) if "LINE_WIDTH" in os.environ else 0
    for path in files or ["-"]:
        if path == "-":
            sys.stdout.write(md2ansi_color(sys.stdin.read(),
                                           line_width=line_width))
        else:
            with open(path) as f:
                sys.stdout.write(md2ansi_color(f.read(), line_width=line_width))
    return 0


def _m2a_cli_dispatch(argv):
    """Parse `argv` (§10) and run the selected mode: a doc-op verb flag
    anywhere selects doc-op mode; with none, render mode — which takes no
    options at all, so any declared option without a verb is a usage error
    here (undeclared ones already died in the parser) rather than a
    baffling open() failure."""
    # Split at the first '--' up front: everything after it is positional
    # in every mode (§10). argparse owns that rule too, but its
    # parse_intermixed_args re-sees post-'--' tokens in the optionals pass
    # (option-named files come back "unrecognized", a positional '-h'
    # triggers help), so the parser only ever gets the pre-'--' part.
    cut = argv.index("--") if "--" in argv else len(argv)
    args = _m2a_cli_parser().parse_intermixed_args(argv[:cut])
    args.files += argv[cut + 1:]
    verb = next((v for v in _M2A_CLI_VERBS
                 if getattr(args, v) not in (None, False)), None)
    if verb is not None:
        return _m2a_cli_doc_op(verb, args)
    used = next(
        (flag for flag, dest in _M2A_CLI_VERB_OPTS + (
            ("-f/--file", "file"), ("--regex", "regex"),
            ("-v/--verbose", "verbose"))
         if getattr(args, dest) not in (None, False)), None)
    if used is not None:
        raise _M2A_UsageError(
            f"option {used} requires a doc-op verb (render mode takes "
            f"no options)")
    return _m2a_cli_render(args.files)


def _m2a_cli_main(argv):
    """CLI entry (§10). `-h`/`--help` anywhere before `--` = help on
    stdout, exit 0 — intercepted before parsing, since to argparse a `-h`
    where a verb's Q belongs is a missing-argument error (past `--` it IS
    a file name). Returns the §7 exit code: 0 success, 1 operation error,
    2 usage error, 130 Ctrl-C, 141 closed stdout pipe (those two silent)."""
    import os
    import sys
    try:
        cut = argv.index("--") if "--" in argv else len(argv)
        if "-h" in argv[:cut] or "--help" in argv[:cut]:
            sys.stdout.write(_m2a_cli_parser().format_help())
            code = 0
        else:
            code = _m2a_cli_dispatch(argv)
        # Flush INSIDE the try: stdout is block-buffered under a pipe, so a
        # downstream EPIPE often surfaces only at flush time — deferred to
        # interpreter shutdown it would traceback untrappably.
        sys.stdout.flush()
        return code
    except _M2A_UsageError as exc:
        sys.stderr.write(
            f"md2ansi_lib.py: {exc}\n{_m2a_cli_parser().format_usage()}")
        return 2
    except KeyboardInterrupt:
        return 130  # 128+SIGINT, the shell convention for Ctrl-C
    except BrokenPipeError:
        # Downstream closed stdout (`md-list big.md | head -1`). Repoint
        # the dangling fd at devnull before exiting so the interpreter's
        # shutdown flush of sys.stdout cannot raise a second, untrappable
        # BrokenPipeError.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141  # 128+SIGPIPE, what a signal-killed pipe member reports
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"md2ansi_lib.py: {exc}\n")
        return 1


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
    import sys
    sys.exit(_m2a_cli_main(sys.argv[1:]))
