#!/usr/bin/env python3

"""md2ansi_lib — single-file, zero-dependency Markdown-to-ANSI library.

See md2ansi_lib.design.md for architecture, naming conventions, and rule tables.
"""

import re
from dataclasses import dataclass, field
from typing import Any


# ### Section: SGR color constants ##########################################

# Bare SGR codes — wrapping in `\x1b[...m` is the dispatcher's job.

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


@dataclass(slots=True)
class M2A_DocumentState:
    line_width: int = 150
    footnotes: dict = field(default_factory=dict)
    footnote_order: list = field(default_factory=list)
    cell_min_width: int = 20
    row_dividers: Any = None
    # The requested wrap width (the caller's `line_width`), or 0 when wrapping
    # is disabled. Drives table fitting, blockquote/list self-wrapping, and the
    # post-render prose wrap. Kept distinct from `line_width` so the 150-char
    # fallback used for HR sizing doesn't accidentally trigger any of those.
    wrap_width: int = 0


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

# Permissive multiline single/double-quoted strings — same shape as the strict
# fragments but WITHOUT the `\n` exclusion, so a string may span linebreaks.
# Used only by the unknown-language context, where over-matching an unbalanced
# quote across lines is an accepted tradeoff (the language is unknown). The
# backtick fragment (`_M2A_STR_BT`) is already newline-permissive.
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
# words read brighter by contrast. Used as the universal trailing rule in every
# code context (appended LAST, so it never steals a `.` inside a float or a `/`
# in `//`). Excludes `_` (word char), quotes/backtick (string rules), `\`
# (escape), and `#` (often a comment/preprocessor marker handled earlier).
# `-` leads the class so it's literal; `]` is escaped.
_M2A_PUNCT = r"[-+*/%=<>!&|^~.,;:?@(){}\[\]]+"

# Block-start lookahead — substituted into every cross-line inline rule's
# soft-newline branch so inline matching stops at block boundaries. The `#`
# branch requires 1–6 hashes followed by a space, matching a real ATX heading
# (_MD_H1.._MD_H6); a bare `#word` is not a heading and must not stop a span.
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

# Line-wrap "no-break zone" — within the first N visible chars of a line, an
# overflowing word is attached rather than triggering a break (there's no
# useful break point that close to the start). Capped at 20 so a long token
# in a wide line breaks instead of accumulating leading content past that
# point; for narrow widths (line_width ≤ 30) the zone shrinks linearly to 0
# so cramped columns still get aggressive breaks.
def _m2a_no_break_zone(line_width):
    return min(20, max(0, line_width - 30))

# Markdown-table cell-content matcher. Each char is in exactly one branch, tried
# in order so the longest meaningful unit wins before the catch-all:
#   \\.                       markdown escape (incl. `\|`, `\``) — kept first so
#                             an escaped backtick can't open a code span.
#   `` (?:(?!``)[^\n])* ``    double-backtick code span (may hold lone backticks).
#   ` (?:\\.|[^`\n\\])* `      single-backtick code span.
#   [^|\\\n]                   ordinary char (also a lone, unclosed `` ` ``).
# The code-span branches make an un-escaped `|` inside backticks cell content
# rather than a column divider; an unbalanced backtick falls through to the
# catch-all so `|` still splits (the row was malformed anyway). Every branch is
# tempered-greedy (the closer is excluded from its body), so matching stays
# linear in input size.
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
    """Split a markdown table row on un-escaped `|`. Honours `\\|`.
    Strips one optional leading `|` and one optional trailing un-escaped `|`,
    then walks the rest through the linear cell-content regex.
    """
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
    """Pad `content` to `width` columns according to `align`.

    Width math uses _m2a_visible_len so embedded ANSI escapes don't skew it.
    Caller is responsible for any surrounding decoration (e.g. the single
    space of inner padding inside table `│ … │` cells).
    """
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


# Block formatters that own their own layout (code frames, tables, headings,
# blockquotes, lists, footnotes, HR) wrap themselves and then mark every output
# line with this sentinel so the post-render wrap pass leaves their structure
# untouched. NUL never occurs in real input; `md2ansi` strips any stray copy
# from the source before rendering, and `_m2a_wrap_rendered` strips the markers
# it consumes.
_M2A_OPAQUE = "\x00"


def _m2a_opaque(text):
    """Mark every line of `text` as opaque — exempt from post-render wrapping."""
    return "\n".join(_M2A_OPAQUE + ln for ln in text.split("\n"))


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
    return _m2a_opaque(_m2a_inject_color(bar, current_style, current_style))


def _m2a_fmt_heading(m, name, current_style, context, state, sgr):
    """Render an ATX heading: recurse the title through inline rules under the
    level's color, then mark the line opaque so it's never wrapped (a heading
    that overflows the width stays on one line, matching its block intent)."""
    inner = m.group(f"{name}_inner")
    new_style = f"{current_style};{sgr}"
    inner = _md2ansi(inner, new_style, M2A_CONTEXT_MD_INLINE, state)
    return _m2a_opaque(_m2a_inject_color(inner, new_style, current_style))


# `\`` → bare backtick. Inside a single-backtick code span `\` escapes ONLY a
# backtick (so it can't close the span); every other backslash is left verbatim.
_M2A_INLINE_CODE_UNESCAPE = re.compile(r"\\(`)")


def _m2a_fmt_inline_code(m, name, current_style, context, state):
    text = m.group(f"{name}_inner")
    if name == "code_inline":
        # Single-backtick spans honor `\`` so a backtick can be embedded;
        # double-backtick spans keep backslashes verbatim per CommonMark.
        text = _M2A_INLINE_CODE_UNESCAPE.sub(r"\1", text)
    return _m2a_styled(text, current_style, M2A_COLOR_STRING)


def _m2a_fmt_escape(m, name, current_style, context, state):
    # `\<punct>` → the punctuation char alone; `\<newline>` → bare newline
    # (CommonMark hard-line-break).
    return m.group(f"{name}_char")


def _m2a_fmt_image(m, name, current_style, context, state):
    alt = m.group(f"{name}_alt") or ""
    return _m2a_styled(f"[IMG: {alt}]", current_style, f"3;{M2A_COLOR_DIM}")


def _m2a_fmt_blockquote(m, name, current_style, context, state):
    text = m.group(0)
    stripped = "\n".join(re.sub(r"^>[ \t]?", "", ln) for ln in text.split("\n"))
    inner = _md2ansi(stripped, current_style, M2A_CONTEXT_MD_INLINE, state)
    bar = _m2a_styled("│", current_style, M2A_COLOR_DIM) + " "
    # The quote owns its layout, so it wraps itself (visible-width aware) before
    # the bar is prefixed — width less the 2-column bar — then marks the result
    # opaque so the post-render pass won't reflow it.
    if state.wrap_width > 0:
        inner = "\n".join(
            sub
            for ln in inner.split("\n")
            for sub in _m2a_wrap_ansi_line(ln, state.wrap_width - 2)
        )
    return _m2a_opaque(_m2a_prefix_lines(inner, bar))


def _m2a_fmt_table(m, name, current_style, context, state):
    raw_rows = []
    for ln in m.group(0).strip("\n").split("\n"):
        s = ln.strip()
        if not s.startswith("|"):
            continue
        raw_rows.append(_m2a_split_table_row(s))
    if len(raw_rows) < 1:
        return m.group(0)
    header = raw_rows[0]
    # Detect separator row (e.g. `| --- | :--: |`); skip if present.
    body_start = 1
    if len(raw_rows) >= 2 and all(re.fullmatch(r":?-{2,}:?", c) for c in raw_rows[1]):
        body_start = 2
    body = raw_rows[body_start:]
    n_cols = len(header)

    # Per-column alignment from the separator row (`:--` left, `--:` right,
    # `:--:` center). Default left when no separator row or no marker.
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

    # ── Shrink-to-fit layout ─────────────────────────────────────────────
    # When the caller set a target line width, reduce wide columns so the
    # total table width fits. Columns whose natural width is already at or
    # below cell_min_width are pinned and never wrapped. The loop reassigns
    # widths proportionally; any column that would drop below cell_min_width
    # is pinned at cell_min_width and the remaining wide columns are
    # re-scaled. Per spec, if everything is pinned and the table still
    # overflows, we accept the overflow.
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

    # ── Per-cell wrapping ────────────────────────────────────────────────
    # Cells are already rendered (`rendered_header`, `rendered_body`). Wrap
    # the styled text with `_m2a_wrap_ansi_line` so inline rules (`**bold**`,
    # `` `code` ``, links, images) are matched against the FULL cell text
    # before any wrapping splits it. Width math runs on visible chars; SGR
    # escapes are preserved verbatim and re-emitted after each break.
    # Reset at the end of every wrapped sub-line so a styled span left open
    # at the break point (e.g. `**bold` ending one sub-line, `bold**` on the
    # next) can't leak into the cell padding, the `│` separator, or the next
    # cell on the same visual row. Tables don't inherit a style — they're
    # always top-level — so plain `\x1b[m` (= `\x1b[0m`) is the right reset.
    def cell_sublines(rendered, w):
        return _m2a_wrap_ansi_line(rendered, w, "", "\x1b[m") if rendered else [""]

    header_cells = [cell_sublines(rendered_header[i], widths[i]) for i in range(n_cols)]
    body_cells = [[cell_sublines(r[i], widths[i]) for i in range(n_cols)] for r in rendered_body]

    def _col_actual(i):
        actual = max(
            (_m2a_visible_len(s) for s in header_cells[i]),
            default=0,
        )
        for row in body_cells:
            for s in row[i]:
                actual = max(actual, _m2a_visible_len(s))
        return actual

    def _rewrap_column(i):
        header_cells[i] = cell_sublines(rendered_header[i], widths[i])
        for r_idx, r in enumerate(rendered_body):
            body_cells[r_idx][i] = cell_sublines(r[i], widths[i])

    def _reconcile_column(i):
        # Grow widths[i] until the column's widest sub-line fits, re-wrapping
        # in between. The grow step ISN'T idempotent — at a wider width the
        # no-break zone gives more room, which can let a long word land on a
        # line that's already past threshold, producing an even wider
        # sub-line — so iterate until stable.
        for _ in range(n_cols + 8):
            actual = _col_actual(i)
            if actual <= widths[i]:
                break
            widths[i] = actual
            _rewrap_column(i)
        else:
            widths[i] = max(widths[i], _col_actual(i))
        # If every sub-line came in below the final width, shrink to fit.
        if actual < widths[i]:
            widths[i] = max(actual, 1)

    for i in range(n_cols):
        _reconcile_column(i)

    # ── Extra fitting round ──────────────────────────────────────────────
    # If some column grew past its layout assignment (oversize) the table
    # now overflows the budget. Try to recover space by shrinking the
    # remaining (non-oversize, non-cell-min) columns proportionally into
    # whatever budget is left. After each shrink+reconcile, if a column
    # grew back above its new target it joins the oversize set and the
    # round re-runs over the rest. The loop terminates when the table fits
    # OR no further columns can be shrunk OR no column changed in the pass.
    if target_lw > 0:
        layout_widths = list(widths)
        # Anything that grew during the initial reconcile is "oversize". Track
        # the per-column target the extra-fit pass last tried — used to detect
        # bounce-back inside the loop.
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
                # The new target becomes the layout baseline for this column;
                # a re-reconcile that bounces above it marks the column oversize.
                layout_widths[i] = new_w
                _rewrap_column(i)
                _reconcile_column(i)
                progressed = True
            if not progressed:
                break

    def render_row(cells):
        # cells: list of per-column lists of rendered sub-lines.
        height = max((len(c) for c in cells), default=1)
        out = []
        for k in range(height):
            parts = []
            for i, col in enumerate(cells):
                if k < len(col):
                    parts.append(f" {_m2a_align_cell(col[k], widths[i], aligns[i])} ")
                else:
                    # Top-align: pad shorter cells with blank lines at the bottom.
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
    return _m2a_opaque("\n".join(out_lines))


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
            line = f"{'  ' * level}{styled} {rendered}"
            # The item is fully rendered before wrapping, so inline spans never
            # straddle a wrap break. Continuations hang-indent two columns past
            # the list indent (under the content, clear of the one-col bullet).
            if state.wrap_width > 0:
                out_lines.extend(
                    _m2a_wrap_ansi_line(line, state.wrap_width, "  " * level + "  ")
                )
            else:
                out_lines.append(line)
        else:
            out_lines.append(ln)
    return _m2a_opaque("\n".join(out_lines))


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
    return _m2a_opaque("\n".join(out)) + "\n"


def _m2a_fmt_code(m, name, current_style, context, state, code_context, lang=None, label=None):
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
    if label is None:
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
    return _m2a_opaque(framed)


# ### Section: Rule tables ##################################################

# Rules are 4-tuples: `(name, pattern, fmt, recurse)` where:
# - `name` — str identifier (drives `(?P<name>...)` outer group and `(?P<*...>)` rewrite)
# - `pattern` — regex source (`re.VERBOSE` mode)
# - `fmt` — either an SGR-codes string (e.g., `"1;3"`) or a callable `(match, current_style, context, state) → str`
# - `recurse` — `M2A_Context` to recurse content into, or `None` to leave content as a literal

# Universal trailing rule, appended LAST to every code ruleset: dims any run of
# punctuation the language-specific rules didn't already claim. Last position is
# load-bearing — comments/strings/numbers must match first so their internal
# punctuation isn't grabbed here.
_M2A_RULE_PUNCT = ("punct", _M2A_PUNCT, M2A_COLOR_PUNCT, None)

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
    _M2A_RULE_PUNCT,
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
    _M2A_RULE_PUNCT,
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
    _M2A_RULE_PUNCT,
)

# C / C++ — one shared ruleset for both (simplicity over precision). Keywords
# fold the C and C++ reserved words together with the fundamental types; the
# fixed-width <stdint.h> types and common library names live in builtins.
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

# Order: a `#...` preprocessor directive is its own token and must match first
# (C has no `#` comment). Comments precede strings/numbers; punct dims the rest.
# Char literals reuse the single-quote string fragment and share the string color.
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

# Unknown / unmarked blocks — no language to key off, so just the universal
# tokens: permissive (newline-spanning) strings, numbers, and dimmed punctuation.
# No comment rule (comment syntax is unknown). Strings come first so a number or
# operator inside a quoted span isn't separately colored.
_M2A_RULES_CODE_UNKNOWN = (
    ("gen_string_dq", _M2A_STR_DQ_ML, M2A_COLOR_STRING, None),
    ("gen_string_sq", _M2A_STR_SQ_ML, M2A_COLOR_STRING, None),
    ("gen_string_bt", _M2A_STR_BT,    M2A_COLOR_STRING, None),
    ("gen_number",    _M2A_NUM,       M2A_COLOR_NUMBER, None),
    _M2A_RULE_PUNCT,
)

# Generic: no rules — fenced block content passes through unchanged. Reserved
# for frontmatter, which must stay a verbatim passthrough (it is not code).
_M2A_RULES_CODE_GENERIC = ()


# ### Section: Compiled contexts ############################################

M2A_CONTEXT_CODE_PYTHON     = _m2a_build_context(_M2A_RULES_CODE_PYTHON)
M2A_CONTEXT_CODE_BASH       = _m2a_build_context(_M2A_RULES_CODE_BASH)
M2A_CONTEXT_CODE_JAVASCRIPT = _m2a_build_context(_M2A_RULES_CODE_JAVASCRIPT)
M2A_CONTEXT_CODE_C          = _m2a_build_context(_M2A_RULES_CODE_C)
M2A_CONTEXT_CODE_UNKNOWN    = _m2a_build_context(_M2A_RULES_CODE_UNKNOWN)
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

# Frontmatter — anchored to file start via `\A` so it never matches
# mid-document. Empty `(?P<*indent>)` group so the shared `_m2a_fmt_code`
# framing (which reads `{name}_indent`) works with no indent. The body is a run
# of lines that are each non-empty, non-comment (`#…`), and not the closing
# fence; the first blank line, `#` comment, or `---` ends it. Requiring a tight
# block keeps real markdown (which has blank lines / `#` headings) from being
# mistaken for frontmatter when a document opens with a `---` thematic break.
# The closing `---` ends at `$` (not consuming its trailing newline, like code
# fences) so the framed box doesn't merge with the following line. MUST precede
# `hr` in the rule table (both match a leading `---`).
_MD_FRONTMATTER = r"""
    \A (?P<*indent>) --- [ \t]* \n
    (?P<*body>
        (?: ^ (?! --- [ \t]* $ ) (?! [ \t]* \# ) (?! [ \t]* $ ) [^\n]* \n )*
    )
    ^ --- [ \t]* $
"""

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
_MD_CODE_C    = _fenced(r"(?:c\+\+|cpp|cxx|cc|hpp|hxx|h|c)")
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

# Backslash-escape token: `\<any char>`. Used as the first alternative in
# every inline-delimiter rule's inner alternation so that `\*`, `\~`, `\[`,
# `\\` etc. survive the wrapper rule as a single 2-char unit and don't
# accidentally close the span.
_MD_ESCAPED = r"\\."

# Double-backtick inline code — body may contain single backticks; closes on
# the first ``. Listed before the single-backtick rule so it wins on `` ``…`` ``.
_MD_CODE_INLINE2 = rf"""
    `` (?P<*>
        (?: (?!``) (?: [^\n] | \n (?! {_BSA} ) ) )+
    ) ``
"""
# Single-backtick inline code — like the emphasis rules, `{_MD_ESCAPED}` is the
# first alternative (and `\\` is excluded from the negated class) so `\`` is a
# literal backtick that doesn't close the span. The handler unwraps the escapes.
_MD_CODE_INLINE  = rf" ` (?P<*> (?: {_MD_ESCAPED} | [^`\n\\] | \n (?! {_BSA} ) )+ ) ` "

_MD_IMAGE = r" ! \[ (?P<*alt> [^\]\n]* ) \] \( (?P<*url> [^)\n]* ) \) "

# Standalone escape rule fires in the INLINE context so each `\<punct>`
# token gets unwrapped to just the punctuation char. The `\n` in the
# class implements CommonMark's hard-line-break syntax (`\` at end of
# line emits a newline and drops the backslash).
_MD_ESCAPE = r"""
    \\ (?P<*char>
        [ !"\#\$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~ \n ]
    )
"""

_MD_LINK = rf"""
    (?<!!) \[ (?P<*>
        (?: {_MD_ESCAPED} | [^\]\n\\] | \n (?! {_BSA} ) )+
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

# Lambdas binding the code context (and display language label) for each
# language-specific code block. The generic block passes lang=None so the
# handler reads it from the pattern's captured `_lang` group.
def _m2a_code_lambda(code_ctx, lang=None, label=None):
    return lambda m, name, cs, ctx, st: _m2a_fmt_code(m, name, cs, ctx, st, code_ctx, lang, label)

# Lambda binding the level color for each heading rule (h1..h6).
def _m2a_heading_lambda(sgr):
    return lambda m, name, cs, ctx, st: _m2a_fmt_heading(m, name, cs, ctx, st, sgr)

# Inline rules — used to build M2A_CONTEXT_MD_INLINE (where _M2A_RECURSE_SELF
# resolves to INLINE itself), and reused inside _M2A_RULES_MD after rebinding
# the sentinel to the now-built INLINE context. Block-level matches recurse
# into INLINE so heading/quote/cell text never re-triggers block rules
# (otherwise "1. Goals" inside `## 1. Goals` would render as a list).
_M2A_RULES_INLINE_RAW = (
    ("code_inline2",  _MD_CODE_INLINE2, _m2a_fmt_inline_code,  None),
    ("code_inline",   _MD_CODE_INLINE,  _m2a_fmt_inline_code,  None),
    # Backslash escapes — placed AFTER inline code so a code span is captured
    # whole (its own pattern/handler resolve any internal escapes), but BEFORE
    # every other delimiter so `\*`, `\~`, `\[` etc. don't trigger emphasis / links.
    ("escape",        _MD_ESCAPE,       _m2a_fmt_escape,       None),
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
    ("frontmatter",   _MD_FRONTMATTER,  _m2a_code_lambda(M2A_CONTEXT_CODE_GENERIC, label="Frontmatter"), None),
    ("h1",            _MD_H1,           _m2a_heading_lambda(M2A_COLOR_H1),            None),
    ("h2",            _MD_H2,           _m2a_heading_lambda(M2A_COLOR_H2),            None),
    ("h3",            _MD_H3,           _m2a_heading_lambda(M2A_COLOR_H3),            None),
    ("h4",            _MD_H4,           _m2a_heading_lambda(M2A_COLOR_H4),            None),
    ("h5",            _MD_H5,           _m2a_heading_lambda(M2A_COLOR_H5),            None),
    ("h6",            _MD_H6,           _m2a_heading_lambda(M2A_COLOR_H6),            None),
    ("hr",            _MD_HR,           _m2a_fmt_hr,                                  None),
    ("code_python",   _MD_CODE_PY,      _m2a_code_lambda(M2A_CONTEXT_CODE_PYTHON,     "python"),     None),
    ("code_bash",     _MD_CODE_BASH,    _m2a_code_lambda(M2A_CONTEXT_CODE_BASH,       "bash"),       None),
    ("code_js",       _MD_CODE_JS,      _m2a_code_lambda(M2A_CONTEXT_CODE_JAVASCRIPT, "javascript"),None),
    ("code_c",        _MD_CODE_C,       _m2a_code_lambda(M2A_CONTEXT_CODE_C,          label="C/C++"),None),
    ("code_generic",  _MD_CODE_GEN,     _m2a_code_lambda(M2A_CONTEXT_CODE_UNKNOWN),   None),
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

# Line wrapping runs as a post-render pass over the already-styled output (see
# `_m2a_wrap_rendered`). Wrapping after rendering — rather than pre-wrapping the
# source — means inline spans are fully resolved before any break is inserted,
# so a `**bold**` / `` `code` `` span can never be split mid-construct, and
# widths are measured on visible characters rather than raw markdown.


def _m2a_wrap_ansi_line(line, line_width, continuation="", reset_sgr=""):
    """Greedy word-wrap over already-styled text: wraps at visible-character
    positions (a small no-break zone at the line start, like the source wrapper
    it replaced), leaves SGR escape sequences intact, and re-emits the last seen
    SGR at the start of each new line so any styling active at the break
    point survives onto the next line.

    `reset_sgr` (e.g. `"\\x1b[0m"`) is appended to every output line so a
    styled span that's still open at the break point cannot leak into
    whatever follows on the same visual row — table-cell separators, padding,
    or the next cell on the same line.
    """
    if _m2a_visible_len(line) <= line_width:
        return [line + reset_sgr]
    threshold = _m2a_no_break_zone(line_width)
    # Tokenize: ANSI escapes first (so they're not eaten by the word class),
    # then whitespace runs, then word runs. The word class explicitly excludes
    # \x1b so an ESC sequence following a word starts a new token rather than
    # being swallowed into it.
    tokens = re.findall(r"\x1b\[[0-9;]*m|\s+|[^\s\x1b]+", line)

    lines_out = []
    current = []
    current_vlen = 0
    pending = []      # whitespace + escapes accumulated since the last word
    pending_vlen = 0  # visible width contributed by `pending`
    last_sgr = ""     # most recent SGR seen — re-emitted after a break

    for tok in tokens:
        if tok.startswith("\x1b["):
            last_sgr = tok
            pending.append(tok)
            continue
        if tok[0].isspace():
            pending.append(tok)
            pending_vlen += len(tok)
            continue
        # `tok` is a word.
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

    # Flush any trailing escapes (e.g. closing reset).
    current.extend(pending)
    lines_out.append("".join(current) + reset_sgr)
    return lines_out


def _m2a_wrap_rendered(text, line_width):
    """Post-render word-wrap over already-styled output.

    Each output line is wrapped to `line_width` measuring visible width and
    re-emitting active SGR across breaks (`_m2a_wrap_ansi_line`); a prose line's
    continuation inherits its leading whitespace. Lines a block formatter marked
    opaque (`_M2A_OPAQUE`) own their own layout (code frames, tables, headings,
    lists, blockquotes, footnotes, HR) — they pass through verbatim, with the
    marker stripped. The marker is always stripped here, so this pass also runs
    when wrapping is disabled (`line_width <= 0`) purely to remove markers.
    """
    out = []
    for ln in text.split("\n"):
        if ln.startswith(_M2A_OPAQUE):
            out.append(ln[len(_M2A_OPAQUE):])
        elif line_width > 0:
            cont = re.match(r"[ \t]*", ln).group(0)
            out.extend(_m2a_wrap_ansi_line(ln, line_width, cont))
        else:
            out.append(ln)
    return "\n".join(out)


def md2ansi(text, current_style="0", line_width=0, cell_min_width=20, row_dividers=None):
    """Convert Markdown text to ANSI-colored output.

    `line_width` > 0 enables word wrapping for paragraphs, lists, and
    blockquotes. Wrapping runs as a post-render pass over the styled output
    (`_m2a_wrap_rendered`), so inline spans are never split mid-construct and
    widths count visible characters, not raw markdown. It's also the width used
    by `_m2a_fmt_hr`. When 0 (the default) no wrapping happens and HR falls back
    to a 150-char bar.

    `cell_min_width` is the minimum width a table column can be shrunk to when
    fitting the table into `line_width`; columns whose natural width is at or
    below this are never shrunk or wrapped. `row_dividers` is a tristate:
    `None` (default) emits inter-row dividers only when any body cell wraps;
    `True` always emits them; `False` never emits them.
    """
    # NUL is the opaque-line marker; strip any stray copy from the source so it
    # can't be mistaken for one.
    text = text.replace(_M2A_OPAQUE, "")
    state_lw = line_width if line_width > 0 else 150
    state = M2A_DocumentState(
        line_width=state_lw,
        cell_min_width=cell_min_width,
        row_dividers=row_dividers,
        wrap_width=line_width,
    )
    out = _md2ansi(text, current_style, M2A_CONTEXT_MD, state)
    if state.footnote_order:
        out += _m2a_render_footnotes(state, current_style)
    return _m2a_wrap_rendered(out, line_width)


# ### Section: Structural scan API #########################################

# A non-rendering view of the matches the engine already produces. `md2ansi_scan`
# runs the same compiled MD grammar over the RAW (unwrapped) source and yields
# one `M2A_Span` per top-level match, so consumers (e.g. a markdown TOC browser)
# get heading/list/code offsets without re-implementing the grammar. The scan is
# non-recursive: it sees every block span plus any inline match at top level, but
# not inline markup nested inside a block (that stays masked in the block's span).


@dataclass(frozen=True, slots=True)
class M2A_Span:
    """One top-level match from `md2ansi_scan`.

    `kind` is the broad category ('heading', 'code', 'list', 'emphasis', …);
    `subtype` is the narrow refinement, always populated — it falls back to
    `kind` when there's no finer detail — so callers can match on it alone
    ('h1'..'h6', 'code-python'/'code-<tag>', 'bold'/'italic'/…). `is_block`
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
# the fallback (kind == subtype == rule name). Headings collapse to 'heading'
# with the level as subtype; the code_* rules collapse to 'code' with a `code-`
# prefixed language subtype (namespaced so a fence tag can never collide with
# another construct's subtype); the emphasis variants collapse to 'emphasis'.
# `code_generic`'s subtype is replaced by its fence tag at scan time when present.
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
# `md2ansi_scan(kinds=…)` takes any set of these; compose with `|` / `-` / `&`.
M2A_SPANS_INLINE = frozenset(
    _m2a_span_kind(name)[0] for name in _M2A_INLINE_RULE_NAMES
)
M2A_SPANS_BLOCK = frozenset(
    _m2a_span_kind(name)[0]
    for name, *_ in _M2A_RULES_MD
    if name not in _M2A_INLINE_RULE_NAMES
)
M2A_SPANS_ALL = M2A_SPANS_BLOCK | M2A_SPANS_INLINE


def _m2a_scan(text, kinds):
    """Generator workhorse for `md2ansi_scan` (no validation).

    One `finditer` pass over the combined MD grammar — the same regex, engine,
    and order the renderer uses — so the scan can't drift from what gets
    rendered. The outer rule is identified the same way as `_m2a_replace`: the
    first outer named group with a non-None match (NOT `m.lastgroup`, which
    would pick an inner capture).
    """
    rule_names = [name for name, *_ in M2A_CONTEXT_MD.rules]
    for m in M2A_CONTEXT_MD.compiled.finditer(text):
        groups = m.groupdict()
        rule = next(name for name in rule_names if groups.get(name) is not None)
        kind, subtype = _m2a_span_kind(rule)
        if rule == "code_generic":
            tag = (groups.get("code_generic_lang") or "").strip()
            if tag:
                subtype = f"code-{tag}"
        if kind not in kinds:
            continue
        yield M2A_Span(
            kind=kind,
            subtype=subtype,
            is_block=rule not in _M2A_INLINE_RULE_NAMES,
            start=m.start(),
            end=m.end(),
            text=m.group(0),
        )


def md2ansi_scan(text, kinds=M2A_SPANS_BLOCK):
    """Yield `M2A_Span` for top-level matches whose `kind` is in `kinds`.

    Spans come in document order. The scan runs over the RAW text (no
    line-wrapping), so `text[span.start:span.end] == span.text`. It is
    non-recursive: every block span is reported, plus any inline match at top
    level, but inline markup nested inside a block stays masked in that block's
    span (use `recursive=` — not yet implemented — for that).

    `kinds` is any set of broad kinds; compose with set arithmetic, e.g.
    `M2A_SPANS_BLOCK - {'frontmatter'}` or `{'heading', 'list'}`. Defaults to
    `M2A_SPANS_BLOCK`. Validated eagerly: passing a name not in `M2A_SPANS_ALL`
    raises `ValueError` at the call, before iteration.
    """
    unknown = set(kinds) - M2A_SPANS_ALL
    if unknown:
        raise ValueError(
            f"md2ansi_scan: unknown span kind(s) {sorted(unknown)}; "
            f"valid kinds are {sorted(M2A_SPANS_ALL)}"
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
                sys.stdout.write(md2ansi(f.read(), line_width=line_width))
    else:
        sys.stdout.write(md2ansi(sys.stdin.read(), line_width=line_width))

