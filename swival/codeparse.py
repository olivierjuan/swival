"""Shared lexical helpers for regex-based source scanning.

Comment/string/regex-literal tokenization used by both ``audit.py``
(import/export extraction) and ``outline.py`` (symbol span extraction).
Two families of transforms live here:

* offset-shifting: :func:`strip_comments` removes comment text outright.
  Suitable only for consumers that look at what text remains, never where.
* position-preserving: :func:`mask_noncode` and
  :func:`redact_string_contents` replace non-code text with spaces while
  keeping newlines, so line and column positions of all remaining code are
  unchanged.
"""

import bisect
import re

# JS/TS regex literal: `/pattern/flags`. Treated as an opaque span so
# `import`/`require` text inside the pattern isn't picked up.
#
# Two arms separate strong context from weak context:
#
# * Strong context — start of input, right after `\n`, after the punctuation
#   set `=([{,;:?!&|^`, after `=>`, or after a JS keyword that introduces an
#   expression (`return`, `typeof`). A `/` in these positions is
#   unambiguously a regex literal, so the pattern may begin with whitespace
#   and any whitespace between the operator/keyword and `/` is consumed.
# * Weak context — whitespace alone, or after an arithmetic operator
#   (`+`, `-`, `*`, `/`, `%`, `~`, `<`, `>`). The `/` is ambiguous (it could
#   be division), so the `(?!\s)` lookahead rejects patterns starting with
#   whitespace, which is the shape division-with-call-between-slashes takes
#   (`a / require('x') / b`).
# `(?!\*)` after `/` keeps `/* ... */` block comments from being mis-tokenized
# as a regex starting with `*`. Regex patterns that legitimately begin with
# `*` are vanishingly rare and accepted as a known gap.
_REGEX_BODY = r"/(?!\*)(?:[^/\\\n]|\\.)+/[gimsuyd]*"
# JS expression-introducing keywords that may precede a regex literal. For
# each, two fixed-width lookbehinds are emitted: `(?<=[^\w.]KEYWORD)` for the
# common case (keyword preceded by whitespace or punctuation other than `.`)
# and `(?<=^KEYWORD)` for the start-of-input case. The explicit `[^\w.]`
# rejects the dot, so member-access shapes like `obj.return /x/` fall
# through to weak-context handling and don't mask later real imports.
_JS_REGEX_KEYWORDS: tuple[str, ...] = (
    "in",
    "new",
    "void",
    "case",
    "else",
    "throw",
    "yield",
    "await",
    "return",
    "typeof",
    "delete",
    "instanceof",
)
_KEYWORD_LOOKBEHINDS = "|".join(
    f"(?<=[^\\w.]{kw})|(?<=^{kw})" for kw in _JS_REGEX_KEYWORDS
)

# `]` lives in the weak set even though it can syntactically precede a
# regex literal. In real JS, division after an array/property index
# (`arr[i] / arr[j]`) is overwhelmingly more common than a regex literal
# starting with whitespace after `]`. The weak arm's `(?!\s)` still allows
# non-ws regex like `arr[i] /foo/g` to be tokenized correctly.
_REGEX_LITERAL_RE = (
    r"(?:"
    r"(?:"
    r"(?<=\n)|(?<=^)|"
    r"(?<=[=(,;:?!&|^\[{}])|"
    r"(?<==>)|"
    + _KEYWORD_LOOKBEHINDS
    + r")\s*"
    + _REGEX_BODY
    + r"|(?<=[\s+\-*/%~<>\]])/(?!\s)(?!\*)(?:[^/\\\n]|\\.)+/[gimsuyd]*"
    r")"
)

# Opaque-span tokens: triple/single/double/backtick string literals plus the
# JS regex literal. Shared by `STRING_LITERAL_RE` (span detection) and
# `STRIP_TOKEN_RE` (comment stripping), so both stay in sync.
_OPAQUE_SPAN_PATTERN = (
    r'"""[\s\S]*?"""'
    r"|'''[\s\S]*?'''"
    r'|"(?:[^"\\]|\\.)*"'
    r"|'(?:[^'\\]|\\.)*'"
    r"|`(?:[^`\\]|\\.)*`"
)

STRING_LITERAL_RE = re.compile(_OPAQUE_SPAN_PATTERN + r"|" + _REGEX_LITERAL_RE)

# Block/line comments are listed before the regex literal so `/* ... */` and
# `// ...` are tried first and never mis-tokenized as `/.../`.
STRIP_TOKEN_RE = re.compile(
    _OPAQUE_SPAN_PATTERN + r"|/\*[\s\S]*?\*/" + r"|//[^\n]*" + r"|" + _REGEX_LITERAL_RE
)


def strip_comments(content: str) -> str:
    """Remove ``/* ... */`` block comments and ``// ...`` line comments while
    keeping the contents of string literals untouched.

    Run before regex-based import/export extraction so commented-out
    declarations (e.g. ``// require('fake')`` or ``/* class Fake {} */``)
    do not pollute the indices. The matcher consumes string literals first
    so markers that happen to appear inside ``"..."``, ``'...'``, or
    backtick strings are kept verbatim. ``#`` is intentionally not stripped
    because it carries semantic weight in C/C++ (``#include``, ``#define``).

    Comment text is deleted, which shifts byte offsets and line numbers of
    everything after a block comment. Consumers that need stable positions
    must use :func:`mask_noncode` instead.
    """

    def repl(m: re.Match) -> str:
        text = m.group(0)
        if text.startswith("/*") or text.startswith("//"):
            return ""
        return text

    return STRIP_TOKEN_RE.sub(repl, content)


def _blank_preserving_newlines(text: str) -> str:
    return "".join(c if c == "\n" else " " for c in text)


def _blank_string_literal(text: str) -> str | None:
    """Blank a quoted literal's interior, keeping the delimiters.

    Returns ``None`` when ``text`` is not a quoted string (e.g. a regex
    literal), so callers can apply their own fallback.
    """
    if text.startswith(('"""', "'''")) and len(text) >= 6:
        return text[:3] + _blank_preserving_newlines(text[3:-3]) + text[-3:]
    if len(text) >= 2 and text[0] in "\"'`" and text[-1] == text[0]:
        return text[0] + _blank_preserving_newlines(text[1:-1]) + text[-1]
    return None


def mask_noncode(content: str) -> str:
    """Return ``content`` with comment text and string-literal interiors
    replaced by spaces. Newlines are preserved and
    ``len(result) == len(content)``, so line and column positions of all
    remaining code are unchanged.

    String delimiters are kept (a masked ``"foo"`` stays ``"   "``) so the
    token structure remains visible; comment and regex-literal text is
    blanked entirely. As with :func:`strip_comments`, ``#`` comments are
    left alone because ``#`` carries semantic weight in C/C++.
    """

    def repl(m: re.Match) -> str:
        text = m.group(0)
        if text.startswith("/*") or text.startswith("//"):
            return _blank_preserving_newlines(text)
        blanked = _blank_string_literal(text)
        if blanked is not None:
            return blanked
        return _blank_preserving_newlines(text)

    return STRIP_TOKEN_RE.sub(repl, content)


def redact_string_contents(content: str) -> str:
    """Return ``content`` with the interior of every string literal replaced
    by spaces (newlines preserved, delimiters kept), so position-based
    secondary scans can ignore code-shaped text trapped inside string
    literals while leaving byte offsets stable. Comments are left in place;
    use :func:`mask_noncode` to blank both.
    """

    def repl(m: re.Match) -> str:
        s = m.group(0)
        blanked = _blank_string_literal(s)
        if blanked is not None:
            return blanked
        if len(s) < 2:
            return s
        return s[0] + _blank_preserving_newlines(s[1:-1]) + s[-1]

    return STRING_LITERAL_RE.sub(repl, content)


def string_literal_spans(
    content: str,
) -> tuple[list[int], list[int]]:
    """Return parallel ``(starts, ends)`` lists for every opaque span in
    ``content``, sorted by start position (the natural ``re.finditer`` order).
    Kept as two lists so :func:`starts_inside_string` can binary-search."""
    starts: list[int] = []
    ends: list[int] = []
    for m in STRING_LITERAL_RE.finditer(content):
        starts.append(m.start())
        ends.append(m.end())
    return starts, ends


def starts_inside_string(pos: int, spans: tuple[list[int], list[int]]) -> bool:
    starts, ends = spans
    idx = bisect.bisect_right(starts, pos) - 1
    if idx < 0:
        return False
    return starts[idx] < pos < ends[idx] - 1


def is_zig_multiline_string_line(content: str, pos: int) -> bool:
    """Return True when ``pos`` falls on a Zig multiline-string literal line.

    Zig has no block comments; instead each line of a multiline string starts
    with ``\\\\``. A match landing on such a line is part of string content,
    not code.
    """
    line_start = content.rfind("\n", 0, pos) + 1
    prefix = content[line_start:pos].lstrip()
    return prefix.startswith("\\\\")
