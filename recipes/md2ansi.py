#!/usr/bin/env python3

"""md2ansi_lib — single-file, zero-dependency Markdown-to-ANSI library.

See md2ansi_lib.design.md for architecture, naming conventions, and rule tables.
"""

import re
from dataclasses import dataclass, field


# ### Section: SGR color constants ##########################################

# Bare SGR codes — wrapping in `\x1b[...m` is the dispatcher's job.

# Universal code-token palette.
M2A_COLOR_COMMENT  = "38;5;245"   # gray
M2A_COLOR_STRING   = "38;5;114"   # green
M2A_COLOR_NUMBER   = "38;5;220"   # yellow
M2A_COLOR_KEYWORD  = "38;5;204"   # pink
M2A_COLOR_BUILTIN  = "38;5;147"   # purple

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


@dataclass(slots=True)
class M2A_DocumentState:
    line_width: int = 80
    footnotes: dict = field(default_factory=dict)
    footnote_order: list = field(default_factory=list)


# ### Section: Shared regex fragments #######################################

# All fragments are designed to be embedded inside
# re.VERBOSE patterns (whitespace ignored outside character classes; `#` is
# a comment unless escaped).

# String literals — linear, no atomic groups needed. Each char has exactly one
# matching branch: a non-quote non-backslash char OR a backslash + any char.
_M2A_STR_DQ  = r' " (?: [^"\\\n] | \\. )* "  '
_M2A_STR_SQ  = r" ' (?: [^'\\\n] | \\. )* '  "
_M2A_STR_BT  = r" ` (?: [^`\\]   | \\. )* `  "

# Triple-quoted strings — tempered-greedy, no escape handling subtlety.
_M2A_STR_TDQ = r' """ (?: (?!""") [\s\S] )* """ '
_M2A_STR_TSQ = r" ''' (?: (?!''') [\s\S] )* ''' "

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

# Block-start lookahead — substituted into every cross-line inline rule's
# soft-newline branch so inline matching stops at block boundaries.
_M2A_BLOCK_START_AHEAD = r"""
    [ \t]* (?:
        \#
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

# The placeholder rewrite covers both group definitions (`<`-form)
# and backreferences (`=`-form); the trailing `>` or `)` is left alone since
# we only insert the rulename prefix.
_M2A_PLACEHOLDER_RE = re.compile(r"\(\?P(?P<kind>[<=])\*(?P<suffix>\w*)")

# Sentinel meaning "recurse into the same context the rule fired in" — used so
# rules can self-recurse without a circular reference at definition time. The
# dispatcher resolves it to the live context.
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


# ### Section: Callable formatters ##########################################

# These reference M2A_CONTEXT_MD and _md2ansi which are defined later in the
# file. Forward references resolve at call time — fine for function bodies.

_M2A_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _m2a_visible_len(s):
    """Length of s with ANSI escapes stripped — used for width calculations."""
    return len(_M2A_ANSI_ESCAPE_RE.sub("", s))


def _m2a_prefix_lines(text, prefix):
    """Prepend `prefix` to every line in `text`."""
    return "\n".join(prefix + ln for ln in text.split("\n"))


def _m2a_inject_color(text, style, reset=None):
    """Wrap `text` in SGR codes so every line carries its own color setup.

    1. Prepends `\\x1b[{style}m`.
    2. After every maximal run of `\\n`s that is NOT at end-of-string, re-emits
       `\\x1b[{style}m` so each line of a multi-line span is self-styled
       (survives pagers/pipelines that don't carry SGR across newlines).
    3. If `reset` is not None, appends `\\x1b[{reset}m`.

    Trailing newlines are intentionally skipped — injecting after them would
    leave a stray SGR sitting on a non-existent next line, and (with reset)
    produce a no-op open/close pair.
    """
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


def _m2a_fmt_hr(m, name, current_style, context, state):
    bar = "─" * max(1, state.line_width - 1)
    return _m2a_inject_color(bar, current_style, current_style)


def _m2a_fmt_inline_code(m, name, current_style, context, state):
    text = m.group(0).strip("`")
    return _m2a_styled(text, current_style, M2A_COLOR_STRING)


def _m2a_fmt_image(m, name, current_style, context, state):
    alt = m.group(f"{name}_alt") or ""
    return _m2a_styled(f"[IMG: {alt}]", current_style, f"3;{M2A_COLOR_DIM}")


def _m2a_fmt_blockquote(m, name, current_style, context, state):
    text = m.group(0)
    stripped = "\n".join(re.sub(r"^>[ \t]?", "", ln) for ln in text.split("\n"))
    inner = _md2ansi(stripped, current_style, M2A_CONTEXT_MD_INLINE, state)
    bar = _m2a_styled("│", current_style, M2A_COLOR_DIM) + " "
    return _m2a_prefix_lines(inner, bar)


def _m2a_fmt_table(m, name, current_style, context, state):
    raw_rows = []
    for ln in m.group(0).strip("\n").split("\n"):
        s = ln.strip()
        if not s.startswith("|"):
            continue
        # `| a | b |` → ['', ' a ', ' b ', ''] → strip outer empties.
        parts = [c.strip() for c in s.strip("|").split("|")]
        raw_rows.append(parts)
    if len(raw_rows) < 1:
        return m.group(0)
    header = raw_rows[0]
    # Detect separator row (e.g. `| --- | :--: |`); skip if present.
    # TODO: support table cell alignment: :-- , --: , :--:
    body_start = 1
    if len(raw_rows) >= 2 and all(re.fullmatch(r":?-{2,}:?", c) for c in raw_rows[1]):
        body_start = 2
    body = raw_rows[body_start:]
    n_cols = len(header)

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

    def render_row(cells):
        parts = []
        for i, c in enumerate(cells):
            pad_n = widths[i] - _m2a_visible_len(c)
            parts.append(f" {c}{' ' * pad_n} ")
        return "│" + "│".join(parts) + "│"

    def border(left, mid, right):
        return left + mid.join("─" * (widths[i] + 2) for i in range(n_cols)) + right

    out_lines = [border("┌", "┬", "┐"), render_row(rendered_header), border("├", "┼", "┤")]
    out_lines.extend(render_row(r) for r in rendered_body)
    out_lines.append(border("└", "┴", "┘"))
    return "\n".join(out_lines)


def _m2a_fmt_list(m, name, current_style, context, state):
    out_lines = []
    for ln in m.group(0).split("\n"):
        match = re.match(r"^([ \t]*)([-*+]|\d+\.)[ \t]+(.*)$", ln)
        if match:
            indent, marker, content = match.groups()
            level = len(indent.expandtabs(4)) // 2
            bullet = "·" if marker in ("-", "*", "+") else marker
            styled = _m2a_styled(bullet, current_style, "1")
            rendered = _md2ansi(content, current_style, M2A_CONTEXT_MD_INLINE, state)
            out_lines.append(f"{'  ' * level}{styled} {rendered}")
        else:
            out_lines.append(ln)
    return "\n".join(out_lines)


def _m2a_fmt_footnote_def(m, name, current_style, context, state):
    fid = m.group(f"{name}_id")
    text = m.group(f"{name}_text")
    # Collapse continuation lines (per the multi-line pattern).
    text = re.sub(r"\n[ \t]+", " ", text).strip()
    state.footnotes[fid] = text
    return ""


def _m2a_fmt_footnote_ref(m, name, current_style, context, state):
    fid = m.group(f"{name}_id")
    if fid not in state.footnote_order:
        state.footnote_order.append(fid)
    return _m2a_styled(f"[^{fid}]", current_style, M2A_COLOR_FOOTNOTE)


def _m2a_render_footnotes(state, current_style):
    # Refs without a matching definition are silently dropped from the section.
    entries = [(fid, state.footnotes[fid]) for fid in state.footnote_order if fid in state.footnotes]
    if not entries:
        return ""
    out = ["", _m2a_styled("Footnotes:", current_style, "1")]
    for fid, text in entries:
        ref = _m2a_styled(f"[^{fid}]", current_style, M2A_COLOR_FOOTNOTE)
        out.append(f"  {ref} {text}")
    return "\n".join(out) + "\n"


def _m2a_fmt_code(m, name, current_style, context, state, code_context, lang=None):
    body = m.group(f"{name}_body")
    indent = m.group(f"{name}_indent") or ""
    if lang is None:
        # Generic block — read the language tag captured by the pattern, if any.
        lang = (m.groupdict().get(f"{name}_lang") or "").strip()
    # Strip the fence's leading indent from each body line so width/rendering
    # are computed against the de-indented content. The indent is re-applied
    # to every line of the final framed output below.
    if indent:
        body = re.sub(rf"(?m)^{re.escape(indent)}", "", body)
    rendered = _md2ansi(body, current_style, code_context, state)
    body_width = max(
        (_m2a_visible_len(ln) for ln in rendered.split("\n")),
        default=0,
    )
    label = f"Code: {lang}" if lang else "Code"
    # Layout: the frame sticks out 1 char past the body on each side, and the
    # body is indented by 1 space so it sits inside the frame. `inner` is the
    # dash count between the corners; total visible frame width = inner + 2.
    min_inner = len(label) + 6   # "── " + label + " ──"
    inner = max(body_width, min_inner)
    right_dashes = inner - 4 - len(label)
    top_text = f"┌── {label} {'─' * right_dashes}┐"
    bot_text = f"└{'─' * inner}┘"
    top = _m2a_styled(top_text, current_style, M2A_COLOR_FRAME)
    bot = _m2a_styled(bot_text, current_style, M2A_COLOR_FRAME)
    # One-space indent inside the frame (frame's left corner sits at col 0 of
    # frame-local coordinates).
    indented = _m2a_prefix_lines(rendered, " ")
    # `body` capture includes the final content line's terminator, so `indented`
    # usually ends with " " (a trailing indented empty line) — strip that one
    # space so the closing rail sits flush below the last content line.
    if indented.endswith("\n "):
        indented = indented[:-1]
    sep = "" if indented.endswith("\n") else "\n"
    framed = f"{top}\n{indented}{sep}{bot}"
    # Re-apply the source indent to every output line so a code block nested
    # inside a list/quote keeps its column.
    if indent:
        framed = _m2a_prefix_lines(framed, indent)
    return framed


# ### Section: Rule tables ##################################################

# Rules are 4-tuples: `(name, pattern, fmt, recurse)` where:
# - `name` — str identifier (drives `(?P<name>...)` outer group and `(?P<*...>)` rewrite)
# - `pattern` — regex source (`re.VERBOSE` mode)
# - `fmt` — either an SGR-codes string (e.g., `"1;3"`) or a callable `(match, current_style, context, state) → str`
# - `recurse` — `M2A_Context` to recurse content into, or `None` to leave content as a literal

# Python keyword & builtin lists. `type` appears in both lists;
# rule order ensures keyword wins.
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

# Python string with optional prefix: r/R, b/B, u/U, f/F, plus 2-char combos
# (rb, br, fr, rf, ...). The prefix is anchored at a word boundary so it can't
# attach to the tail of an identifier (`foor"x"` keeps `r` as part of `foor`,
# only `"x"` is matched). Empty prefix is allowed via the outer `?`.
# Each fragment is wrapped in its own `(?:...)` so its internal alternation
# (e.g. `[^"\\\n] | \\.`) cannot interact with the outer `|` chain.
# Triple-quoted alternatives come first so `"""..."""` never matches as `""` + DQ.
# TODO: highlight {…} interpolation inside f-strings (deferred extension).
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
    # Use [^\n] not . because re.DOTALL is set globally.
    ("py_comment",    r"\#[^\n]*",                                    M2A_COLOR_COMMENT, None),
    ("py_string",     _M2A_PY_STRING,                                 M2A_COLOR_STRING,  None),
    ("py_number",     _M2A_NUM,                                       M2A_COLOR_NUMBER,  None),
    ("py_keyword",    rf"\b(?:{_M2A_PY_KEYWORDS})\b",                 M2A_COLOR_KEYWORD, None),
    ("py_builtin",    rf"\b(?:{_M2A_PY_BUILTINS})\b",                 M2A_COLOR_BUILTIN, None),
)

# Bash keyword & builtin lists.
_M2A_SH_KEYWORDS = (
    "if|then|else|elif|fi|case|esac|for|while|until|do|done|in|function|time|"
    "select|break|continue|return|declare|readonly|local|export|set|unset|shift|"
    "exit|trap"
)
_M2A_SH_BUILTINS = (
    "echo|printf|read|cd|pwd|pushd|popd|mkdir|rmdir|rm|cp|mv|ln|ls|cat|grep|sed|"
    "awk|find|test|source|eval|exec|ulimit|umask|wait|kill|sleep"
)

# TODO: highlight $VAR / ${...} interpolation inside double-quoted strings.
_M2A_RULES_CODE_BASH = (
    # `sh_comment` requires preceding `^` or whitespace so `$#`, `$?`, etc. aren't
    # misread as comments. The preceding-character check is a non-capturing lookbehind
    # alternative since `\s` is variable-width.
    ("sh_comment",   r"(?:^|(?<=\s))\#[^\n]*",                       M2A_COLOR_COMMENT, None),
    ("sh_string_dq", _M2A_STR_DQ,                                   M2A_COLOR_STRING,  None),
    ("sh_string_sq", _M2A_STR_SQ,                                   M2A_COLOR_STRING,  None),
    ("sh_number",    _M2A_NUM,                                      M2A_COLOR_NUMBER,  None),
    ("sh_keyword",   rf"\b(?:{_M2A_SH_KEYWORDS})\b",                M2A_COLOR_KEYWORD, None),
    ("sh_builtin",   rf"\b(?:{_M2A_SH_BUILTINS})\b",                M2A_COLOR_BUILTIN, None),
)

# JavaScript keyword & builtin lists.
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

# TODO: highlight ${...} interpolation inside template literals.
_M2A_RULES_CODE_JAVASCRIPT = (
    ("js_comment_line",  r"//[^\n]*",                                M2A_COLOR_COMMENT, None),
    ("js_comment_block", r"/\*(?:(?!\*/)[\s\S])*\*/",                M2A_COLOR_COMMENT, None),
    ("js_string_dq",     _M2A_STR_DQ,                                M2A_COLOR_STRING,  None),
    ("js_string_sq",     _M2A_STR_SQ,                                M2A_COLOR_STRING,  None),
    ("js_string_bt",     _M2A_STR_BT,                                M2A_COLOR_STRING,  None),
    ("js_number",        _M2A_NUM,                                   M2A_COLOR_NUMBER,  None),
    ("js_keyword",       rf"\b(?:{_M2A_JS_KEYWORDS})\b",             M2A_COLOR_KEYWORD, None),
    ("js_builtin",       rf"\b(?:{_M2A_JS_BUILTINS})\b",             M2A_COLOR_BUILTIN, None),
)

# Generic: no rules — fenced block content passes through unchanged.
_M2A_RULES_CODE_GENERIC = ()


# ### Section: Compiled contexts ############################################

M2A_CONTEXT_CODE_PYTHON     = _m2a_build_context(_M2A_RULES_CODE_PYTHON)
M2A_CONTEXT_CODE_BASH       = _m2a_build_context(_M2A_RULES_CODE_BASH)
M2A_CONTEXT_CODE_JAVASCRIPT = _m2a_build_context(_M2A_RULES_CODE_JAVASCRIPT)
M2A_CONTEXT_CODE_GENERIC    = _m2a_build_context(_M2A_RULES_CODE_GENERIC)


# ### Section: Markdown rule table ##################################

# Inline patterns embed _M2A_BLOCK_START_AHEAD via f-string substitution so the
# soft-newline branch stops at block boundaries.
_BSA = _M2A_BLOCK_START_AHEAD

# Headings — exact-count `#` followed by space ensures mutual exclusion.
_MD_H1 = r"^ \# [ \t]+ (?P<*> [^\n]+ ) $"
_MD_H2 = r"^ \#{2} [ \t]+ (?P<*> [^\n]+ ) $"
_MD_H3 = r"^ \#{3} [ \t]+ (?P<*> [^\n]+ ) $"
_MD_H4 = r"^ \#{4} [ \t]+ (?P<*> [^\n]+ ) $"
_MD_H5 = r"^ \#{5} [ \t]+ (?P<*> [^\n]+ ) $"
_MD_H6 = r"^ \#{6} [ \t]+ (?P<*> [^\n]+ ) $"

_MD_HR = r"^ (?: -{3,} | ={3,} | _{3,} ) [ \t]* $"

# Fenced code blocks — tempered-greedy body so each char has one matching branch.
def _fenced(tag, fence=r"```"):
    return rf"""
        ^ (?P<*indent> [ \t]* ) {fence} [ \t]* {tag} [ \t]* \n
        (?P<*body> (?: (?! ^ [ \t]* {fence} [ \t]* $ ) [\s\S] )* )
        ^ [ \t]* {fence} [ \t]* $
    """

_MD_CODE_PY   = _fenced("python")
_MD_CODE_BASH = _fenced(r"(?:bash|sh)")
_MD_CODE_JS   = _fenced(r"(?:javascript|js)")
_MD_CODE_GEN  = _fenced(r"(?P<*lang> \w* )", fence=r"(?:```|~~~)")

_MD_BLOCKQUOTE = r"^ > [ \t]? [^\n]* (?: \n > [ \t]? [^\n]* )*"

_MD_TABLE = r"^ [ \t]* \| [^\n]* (?: \n [ \t]* \| [^\n]* )*"

_MD_LIST = r"""
    ^ [ \t]* (?: [-*+] | \d+\. ) [ \t]+ [^\n]*
    (?: \n [ \t]* (?: [-*+] | \d+\. ) [ \t]+ [^\n]* )*
"""

_MD_FOOTNOTE_DEF = r"""
    ^ \[ \^ (?P<*id> [^\]\n]+ ) \] : [ \t]+
    (?P<*text> [^\n]+ (?: \n [ \t]+ [^\n]+ )* )
"""

_MD_FOOTNOTE_REF = r" \[ \^ (?P<*id> [^\]\n]+ ) \] "

# Double-backtick inline code — body may contain single backticks; closes on
# the first ``. Listed before the single-backtick rule so it wins on `` ``…`` ``.
_MD_CODE_INLINE2 = rf"""
    `` (?P<*>
        (?: (?!``) (?: [^\n] | \n (?! {_BSA} ) ) )+
    ) ``
"""
_MD_CODE_INLINE  = rf" ` (?P<*> (?: [^`\n] | \n (?! {_BSA} ) )+ ) ` "

_MD_IMAGE = r" ! \[ (?P<*alt> [^\]\n]* ) \] \( (?P<*url> [^)\n]* ) \) "

_MD_LINK = rf"""
    (?<!!) \[ (?P<*>
        (?: [^\]\n] | \n (?! {_BSA} ) )+
    ) \] \( (?P<*url> [^)\n]* ) \)
"""

_MD_BOLDITALIC = rf"""
    \*\*\* (?P<*>
        (?: [^*\n] | \*(?!\*\*) | \n (?! {_BSA} ) )+
    ) \*\*\*
"""

_MD_BOLD_UNDER = rf"""
    \*\*_ (?P<*>
        (?: [^_\n] | \n (?! {_BSA} ) )+
    ) _\*\*
"""

_MD_UNDER_BOLD = rf"""
    _\*\* (?P<*>
        (?: [^*\n] | \*(?!\*) | \n (?! {_BSA} ) )+
    ) \*\*_
"""

_MD_BOLD = rf"""
    \*\* (?P<*>
        (?: [^*\n] | \*(?!\*) | \n (?! {_BSA} ) )+
    ) \*\*
"""

_MD_STRIKE = rf"""
    ~~ (?P<*>
        (?: [^~\n] | ~(?!~) | \n (?! {_BSA} ) )+
    ) ~~
"""

_MD_ITALIC = rf"""
    (?<!\*) \* (?P<*>
        (?: [^*\n] | \n (?! {_BSA} ) )+
    ) \* (?!\*)
"""

# Lambdas binding the code context (and display language label) for each
# language-specific code block. The generic block passes lang=None so the
# handler reads it from the pattern's captured `_lang` group.
def _m2a_code_lambda(code_ctx, lang=None):
    return lambda m, name, cs, ctx, st: _m2a_fmt_code(m, name, cs, ctx, st, code_ctx, lang)

# Inline rules — used to build M2A_CONTEXT_MD_INLINE (where _M2A_RECURSE_SELF
# resolves to INLINE itself), and reused inside _M2A_RULES_MD after rebinding
# the sentinel to the now-built INLINE context. Block-level matches recurse
# into INLINE so heading/quote/cell text never re-triggers block rules
# (otherwise "1. Goals" inside `## 1. Goals` would render as a list).
_M2A_RULES_INLINE_RAW = (
    ("code_inline2",  _MD_CODE_INLINE2, _m2a_fmt_inline_code,  None),
    ("code_inline",   _MD_CODE_INLINE,  _m2a_fmt_inline_code,  None),
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

# Rebind sentinel to INLINE for the full MD rule table.
_M2A_RULES_INLINE_IN_MD = tuple(
    (name, pat, fmt, M2A_CONTEXT_MD_INLINE if recurse is _M2A_RECURSE_SELF else recurse)
    for name, pat, fmt, recurse in _M2A_RULES_INLINE_RAW
)

_M2A_RULES_MD = (
    ("h1",            _MD_H1,           M2A_COLOR_H1,                                 M2A_CONTEXT_MD_INLINE),
    ("h2",            _MD_H2,           M2A_COLOR_H2,                                 M2A_CONTEXT_MD_INLINE),
    ("h3",            _MD_H3,           M2A_COLOR_H3,                                 M2A_CONTEXT_MD_INLINE),
    ("h4",            _MD_H4,           M2A_COLOR_H4,                                 M2A_CONTEXT_MD_INLINE),
    ("h5",            _MD_H5,           M2A_COLOR_H5,                                 M2A_CONTEXT_MD_INLINE),
    ("h6",            _MD_H6,           M2A_COLOR_H6,                                 M2A_CONTEXT_MD_INLINE),
    ("hr",            _MD_HR,           _m2a_fmt_hr,                                  None),
    ("code_python",   _MD_CODE_PY,      _m2a_code_lambda(M2A_CONTEXT_CODE_PYTHON,     "python"),     None),
    ("code_bash",     _MD_CODE_BASH,    _m2a_code_lambda(M2A_CONTEXT_CODE_BASH,       "bash"),       None),
    ("code_js",       _MD_CODE_JS,      _m2a_code_lambda(M2A_CONTEXT_CODE_JAVASCRIPT, "javascript"),None),
    ("code_generic",  _MD_CODE_GEN,     _m2a_code_lambda(M2A_CONTEXT_CODE_GENERIC),   None),
    ("blockquote",    _MD_BLOCKQUOTE,   _m2a_fmt_blockquote,                          None),
    ("table",         _MD_TABLE,        _m2a_fmt_table,                               None),
    ("list",          _MD_LIST,         _m2a_fmt_list,                                None),
    ("footnote_def",  _MD_FOOTNOTE_DEF, _m2a_fmt_footnote_def,                        None),
) + _M2A_RULES_INLINE_IN_MD

M2A_CONTEXT_MD = _m2a_build_context(_M2A_RULES_MD)


# ### Section: Internal _md2ansi() and replace dispatcher ###################

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


# ### Section: Public md2ansi() entry point #################################

# Line-wrapping helpers — applied to source by `md2ansi` before markdown
# processing when line_width > 0. Wrapping is intentionally NOT done inside
# `_md2ansi` because the dispatcher calls itself recursively; we want to wrap
# once at the top.


def _m2a_continuation_indent(line):
    """Compute the prefix to prepend to wrapped continuation lines so that
    the resulting block still parses as the same markdown construct.

    - Blockquote (`^[> ]*>`): copy the leading `>`/space run verbatim.
    - List item (`-`, `*`, `+`, `N.`): leading whitespace + 2 spaces.
    - Paragraph: leading whitespace only.
    """
    if re.match(r"^[> ]*>", line):
        return re.match(r"^[> ]*", line).group(0)
    m = re.match(r"^([ \t]*)(?:[-*+]|\d+\.)[ \t]+", line)
    if m:
        return m.group(1) + "  "
    return re.match(r"^[ \t]*", line).group(0)


def _m2a_wrap_line(line, line_width, continuation):
    """Greedy word-wrap with a no-break zone for the first `line_width - 30`
    characters. Long single words may overflow.
    """
    if len(line) <= line_width:
        return [line]
    threshold = max(0, line_width - 30)

    # Fast-path: the first `threshold` chars are in the no-break zone — copy
    # them verbatim, extended to the next word boundary so a word straddling
    # `threshold` is kept intact. If there is no whitespace beyond `threshold`,
    # the rest is one giant word and we can't usefully wrap.
    if threshold > 0:
        ws_after = re.search(r"\s+", line[threshold:])
        if ws_after is None:
            return [line]
        split_pos = threshold + ws_after.start()
        head = line[:split_pos]
        tail = line[split_pos:]
    else:
        head = ""
        tail = line

    tokens = re.findall(r"\s+|\S+", tail)
    if not tokens:
        return [line]

    lines_out = []
    current = [head]
    current_len = len(head)
    pending_ws = ""
    for tok in tokens:
        if tok[0].isspace():
            pending_ws = tok
            continue
        attempt_len = current_len + len(pending_ws) + len(tok)
        # Attach if it fits, we're below threshold, or the current line is
        # empty (no break possible before the first content).
        if attempt_len <= line_width or current_len < threshold or current_len == 0:
            current.append(pending_ws)
            current.append(tok)
            current_len = attempt_len
        else:
            lines_out.append("".join(current))
            current = [continuation, tok]
            current_len = len(continuation) + len(tok)
        pending_ws = ""
    lines_out.append("".join(current))
    return lines_out


def _m2a_wrap_source(text, line_width):
    """Pre-pass over raw source: wrap long paragraph / list / blockquote
    lines. Skip tables (TODO: cell-aware wrap), code blocks, headings,
    footnote-def lines.
    """
    if line_width <= 0:
        return text
    out = []
    in_code = False
    for ln in text.split("\n"):
        # Code-fence toggle — anything inside is left verbatim.
        if re.match(r"^[ \t]*(```|~~~)", ln):
            in_code = not in_code
            out.append(ln)
            continue
        if in_code:
            out.append(ln)
            continue
        # Skip: tables, headings, footnote definitions.
        if (re.match(r"^[ \t]*\|", ln)
                or re.match(r"^[ \t]*#{1,6}[ \t]+", ln)
                or re.match(r"^\[\^[^\]]+\]:", ln)):
            # TODO: cell-aware table wrapping.
            out.append(ln)
            continue
        if len(ln) <= line_width:
            out.append(ln)
            continue
        out.extend(_m2a_wrap_line(ln, line_width, _m2a_continuation_indent(ln)))
    return "\n".join(out)


def md2ansi(text, current_style="0", line_width=0):
    """Convert Markdown text to ANSI-colored output.

    `line_width` > 0 enables source-level word wrapping for paragraphs, lists,
    and blockquotes. It's also the width used by `_m2a_fmt_hr`. When 0 (the
    default) no wrapping happens and HR falls back to a 150-char bar.
    """
    if line_width > 0:
        text = _m2a_wrap_source(text, line_width)
        state_lw = line_width
    else:
        state_lw = 150
    state = M2A_DocumentState(line_width=state_lw)
    out = _md2ansi(text, current_style, M2A_CONTEXT_MD, state)
    if state.footnote_order:
        out += _m2a_render_footnotes(state, current_style)
    return out


# ### Section: Plugin registration ##########################################

# Make this file double as a browse-tui plugin: when imported under a
# browse-tui interpreter (recipe / --plugin), self-register so the
# framework knows we're loaded. The import is guarded so the file still
# works as a standalone library / CLI when browse_tui isn't on the path.

try:
    from browse_tui import register_plugin, PluginConfig
    register_plugin(PluginConfig(name='md2ansi'))
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
                sys.stdout.write(md2ansi(f.read(), line_width=line_width))
    else:
        sys.stdout.write(md2ansi(sys.stdin.read(), line_width=line_width))

