"""String replacement engine for the edit_file tool.

Provides a single public function `replace()` that finds and replaces text
in file content using multi-pass matching: exact first, then line-trimmed,
then Unicode-normalized.
"""

from __future__ import annotations

import difflib
import re


# ---------------------------------------------------------------------------
# Unicode normalization (moved from patch.py)
# ---------------------------------------------------------------------------

_UNICODE_SINGLE_QUOTES = re.compile(r"[\u2018\u2019\u201a\u201b]")
_UNICODE_DOUBLE_QUOTES = re.compile(r"[\u201c\u201d\u201e\u201f]")
_UNICODE_DASHES = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015]")


def _normalize_unicode(s: str) -> str:
    """Normalize Unicode punctuation to ASCII equivalents."""
    s = _UNICODE_SINGLE_QUOTES.sub("'", s)
    s = _UNICODE_DOUBLE_QUOTES.sub('"', s)
    s = _UNICODE_DASHES.sub("-", s)
    s = s.replace("\u2026", "...")
    s = s.replace("\u00a0", " ")
    return s


# ---------------------------------------------------------------------------
# Span helpers
# ---------------------------------------------------------------------------


def _exact_match_spans(content: str, old_string: str) -> list[tuple[int, int]]:
    """Return all non-overlapping (start, end) spans of exact matches."""
    spans = []
    start = 0
    step = len(old_string) or 1
    while True:
        idx = content.find(old_string, start)
        if idx == -1:
            break
        spans.append((idx, idx + len(old_string)))
        start = idx + step
    return spans


def _line_at_offset(content: str, offset: int) -> int:
    """Return the 1-based line number for a character offset."""
    return content.count("\n", 0, offset) + 1


def _span_lines(content: str, start: int, end: int) -> tuple[int, int]:
    """Return (first_line, last_line) as 1-based line numbers for a span."""
    first = _line_at_offset(content, start)
    last = _line_at_offset(content, max(start, end - 1))
    return first, last


# ---------------------------------------------------------------------------
# Line-level fuzzy matching helpers
# ---------------------------------------------------------------------------


def _prepare_fuzzy(
    content: str, old_string: str, normalize=None
) -> tuple[list[str], list[str], int, object]:
    """Shared prep for fuzzy matching: split lines, apply normalize + strip."""
    content_lines = content.split("\n")
    old_lines = old_string.split("\n")
    prep = normalize or (lambda s: s)
    prepped_old = [prep(line.strip()) for line in old_lines]
    return content_lines, prepped_old, len(old_lines), prep


def _fuzzy_match_indices(content_lines, prepped_old, old_len, prep):
    """Yield starting line indices where old_string fuzzy-matches."""
    for i in range(len(content_lines) - old_len + 1):
        if all(
            prep(content_lines[i + j].strip()) == prepped_old[j] for j in range(old_len)
        ):
            yield i


def _fuzzy_match_spans(
    content: str, old_string: str, normalize=None, limit: int | None = None
) -> list[tuple[int, int]]:
    """Return (start, end) character-offset spans for fuzzy matches.

    When *limit* is set, stop after collecting that many spans.
    """
    content_lines, prepped_old, old_len, prep = _prepare_fuzzy(
        content, old_string, normalize
    )
    if old_len == 0:
        return []
    spans = []
    for i in _fuzzy_match_indices(content_lines, prepped_old, old_len, prep):
        start = sum(len(content_lines[k]) + 1 for k in range(i))
        end = start + sum(len(content_lines[i + k]) + 1 for k in range(old_len))
        if not old_string.endswith("\n") and end > 0 and end <= len(content) + 1:
            end -= 1
        spans.append((start, end))
        if limit is not None and len(spans) >= limit:
            break
    return spans


def _absorb_trailing_newline(
    content: str, end: int, old_string: str, new_string: str
) -> int:
    """Return the end offset to splice at, absorbing a trailing file newline
    when old_string excludes it but new_string ends with one.

    The model commonly omits the trailing newline from old_string while still
    treating its new_string as a complete replacement line. Without this
    adjustment the splice would produce a double newline at the boundary.
    """
    if (
        old_string
        and not old_string.endswith("\n")
        and new_string.endswith("\n")
        and end < len(content)
        and content[end] == "\n"
    ):
        return end + 1
    return end


def _replace_span(
    content: str,
    span: tuple[int, int],
    new_string: str,
    old_string: str = "",
) -> str:
    """Replace a single span in content, absorbing a trailing file newline
    when *old_string* and *new_string* disagree on the line terminator."""
    start, end = span
    end = _absorb_trailing_newline(content, end, old_string, new_string)
    return content[:start] + new_string + content[end:]


def _replace_all_exact(content: str, old_string: str, new_string: str) -> str:
    """Replace every exact occurrence of *old_string* with *new_string*,
    applying the trailing-newline absorption rule at each splice.

    When the absorption rule cannot fire (old_string ends with \\n, or
    new_string doesn't), we fall back to the C-implemented str.replace
    which is materially faster on files with many occurrences.
    """
    if old_string.endswith("\n") or not new_string.endswith("\n"):
        return content.replace(old_string, new_string)
    parts: list[str] = []
    i = 0
    while True:
        idx = content.find(old_string, i)
        if idx == -1:
            parts.append(content[i:])
            break
        parts.append(content[i:idx])
        parts.append(new_string)
        i = _absorb_trailing_newline(
            content, idx + len(old_string), old_string, new_string
        )
    return "".join(parts)


def _replace_all_fuzzy(
    content: str, old_string: str, new_string: str, normalize=None
) -> str:
    """Replace all fuzzy matches, re-scanning after each replacement."""
    result = content
    while True:
        spans = _fuzzy_match_spans(result, old_string, normalize=normalize)
        if not spans:
            break
        result = _replace_span(result, spans[0], new_string, old_string)
    return result


# ---------------------------------------------------------------------------
# Line-number filtering
# ---------------------------------------------------------------------------

_MAX_CANDIDATE_LINES = 5
_CLOSEST_MATCH_RATIO_THRESHOLD = 0.55
_CLOSEST_MATCH_LINE_DISPLAY_CAP = 200
_CLOSEST_MATCH_MAX_FILE_LINES = 5000


def _truncate_display(s: str, cap: int = _CLOSEST_MATCH_LINE_DISPLAY_CAP) -> str:
    return s if len(s) <= cap else s[:cap] + "…"


def _closest_single_line_match(
    content: str, old_string: str
) -> tuple[int, str, float] | None:
    """Find the line in *content* most similar to *old_string*.

    Returns (1-based line number, line content, similarity ratio) or None
    when *old_string* is multi-line, when the file has no non-empty lines,
    or when the best similarity is below the threshold.

    Uses quick_ratio() as a cheap gate before the O(n*m) ratio() call, and
    skips entirely on files larger than _CLOSEST_MATCH_MAX_FILE_LINES so
    the error path stays bounded.
    """
    if "\n" in old_string:
        return None
    needle = old_string.strip()
    if not needle:
        return None
    content_lines = content.split("\n")
    if len(content_lines) > _CLOSEST_MATCH_MAX_FILE_LINES:
        return None
    best: tuple[int, str, float] | None = None
    matcher = difflib.SequenceMatcher(a=needle, autojunk=False)
    threshold = _CLOSEST_MATCH_RATIO_THRESHOLD
    for i, line in enumerate(content_lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        matcher.set_seq2(stripped)
        if matcher.quick_ratio() < threshold:
            continue
        ratio = matcher.ratio()
        if best is None or ratio > best[2]:
            best = (i, line, ratio)
    if best is None or best[2] < threshold:
        return None
    return best


def _closest_window_match(
    content: str, old_string: str
) -> tuple[int, list[str], float] | None:
    """Find the line window in *content* most similar to a multi-line *old_string*.

    Returns (1-based start line, window lines, similarity ratio) or None
    when *old_string* is single-line, when the file is shorter than the
    window, or when the best similarity is below the threshold.

    Strips lines once up front, gates each window with quick_ratio(), and
    skips files larger than _CLOSEST_MATCH_MAX_FILE_LINES.
    """
    old_lines = old_string.split("\n")
    if len(old_lines) < 2:
        return None
    content_lines = content.split("\n")
    if len(content_lines) < len(old_lines):
        return None
    if len(content_lines) > _CLOSEST_MATCH_MAX_FILE_LINES:
        return None
    stripped_lines = [line.strip() for line in content_lines]
    needle = "\n".join(line.strip() for line in old_lines)
    matcher = difflib.SequenceMatcher(a=needle, autojunk=False)
    best: tuple[int, list[str], float] | None = None
    threshold = _CLOSEST_MATCH_RATIO_THRESHOLD
    window_size = len(old_lines)
    for i in range(len(content_lines) - window_size + 1):
        hay = "\n".join(stripped_lines[i : i + window_size])
        matcher.set_seq2(hay)
        if matcher.quick_ratio() < threshold:
            continue
        ratio = matcher.ratio()
        if best is None or ratio > best[2]:
            best = (i + 1, content_lines[i : i + window_size], ratio)
    if best is None or best[2] < threshold:
        return None
    return best


def _format_closest_match_block(old_string: str, content: str) -> str:
    """Build a human-readable error tail describing the closest match.

    Returns an empty string when no close-enough match exists.
    """
    if "\n" in old_string:
        m = _closest_window_match(content, old_string)
        if m is None:
            return ""
        start, window, ratio = m
        end = start + len(window) - 1
        old_lines = old_string.split("\n")
        body_lines = [
            f"closest window in file (lines {start}-{end}, similarity {ratio:.2f}):",
            "  your old_string:",
        ]
        for line in old_lines:
            body_lines.append("    " + _truncate_display(line))
        body_lines.append("  actual in file:")
        for line in window:
            body_lines.append("    " + _truncate_display(line))
        return "\n" + "\n".join(body_lines)
    m = _closest_single_line_match(content, old_string)
    if m is None:
        return ""
    line_no, actual, ratio = m
    return (
        f"\nclosest line in file (line {line_no}, similarity {ratio:.2f}):"
        f"\n  your old_string: {_truncate_display(old_string)!r}"
        f"\n  actual in file:  {_truncate_display(actual)!r}"
    )


def _line_snippet_at(content: str, offset: int, cap: int = 80) -> str:
    """Return the line containing *offset* as a short display snippet."""
    nl_before = content.rfind("\n", 0, offset)
    nl_after = content.find("\n", offset)
    start = nl_before + 1 if nl_before != -1 else 0
    end = nl_after if nl_after != -1 else len(content)
    line = content[start:end]
    return _truncate_display(line, cap)


def _filter_by_line(
    spans: list[tuple[int, int]],
    content: str,
    line_number: int,
) -> tuple[list[tuple[int, int]], list[int]]:
    """Filter spans to those intersecting *line_number*.

    Returns (matching_spans, all_candidate_lines).
    """
    matching = []
    candidate_lines: list[int] = []
    for s, e in spans:
        first, last = _span_lines(content, s, e)
        candidate_lines.append(first)
        if first <= line_number <= last:
            matching.append((s, e))
    return matching, candidate_lines


def _format_candidate_lines(lines: list[int]) -> str:
    """Format candidate line numbers for error messages."""
    unique = sorted(set(lines))
    if len(unique) <= _MAX_CANDIDATE_LINES:
        return ", ".join(str(n) for n in unique)
    return ", ".join(str(n) for n in unique[:_MAX_CANDIDATE_LINES]) + ", ..."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def replace(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    line_number: int | None = None,
) -> str:
    """Replace old_string with new_string in content.

    Matching strategies (tried in order):
      1. Exact — str.find spans
      2. Line-trimmed — sliding window, comparing .strip() per line
      3. Unicode-normalized — strip + smart quotes/dashes/ellipsis → ASCII

    Parameters
    ----------
    line_number:
        Optional 1-based line number from read_file.  When old_string matches
        multiple times, only replace the match whose span includes this line.
        Ignored when replace_all is True or when the value is not a positive
        integer.

    Raises ValueError:
      - "old_string and new_string are identical ..." if old_string == new_string
      - "old_string not found ..." if no match in any pass
      - "multiple matches; add line_number ..." if >1 match and no line targeting
      - "no match at line N; matches found at lines ..." if line targeting misses
      - "multiple matches on line N; add more context ..." if line targeting is ambiguous
    """
    if old_string == new_string:
        raise ValueError(
            "old_string and new_string are identical, so the edit would be a no-op"
        )

    if not old_string:
        raise ValueError("old_string must not be empty")

    if (
        isinstance(line_number, bool)
        or not isinstance(line_number, int)
        or line_number <= 0
    ):
        line_number = None

    if replace_all:
        line_number = None

    any_candidates_found = False
    all_candidate_lines: list[int] = []

    # When line_number is set we need all spans for filtering; otherwise
    # 2 is enough to detect ambiguity (or 1 for replace_all).
    fuzzy_limit: int | None = None if line_number else 2

    passes: list[tuple[str, object]] = [
        ("exact", None),
        ("fuzzy", None),
        ("unicode", _normalize_unicode),
    ]

    for pass_name, normalize in passes:
        if pass_name == "exact":
            spans = _exact_match_spans(content, old_string)
        else:
            spans = _fuzzy_match_spans(
                content, old_string, normalize=normalize, limit=fuzzy_limit
            )
        if not spans:
            continue

        any_candidates_found = True

        if replace_all:
            if pass_name == "exact":
                return _replace_all_exact(content, old_string, new_string)
            return _replace_all_fuzzy(
                content, old_string, new_string, normalize=normalize
            )

        if line_number is not None:
            matching, cand_lines = _filter_by_line(spans, content, line_number)
            all_candidate_lines.extend(cand_lines)
            if len(matching) == 1:
                return _replace_span(content, matching[0], new_string, old_string)
            if len(matching) > 1:
                raise ValueError(
                    f"multiple matches on line {line_number}; "
                    f"add more context to old_string"
                )
            continue

        if len(spans) == 1:
            return _replace_span(content, spans[0], new_string, old_string)

        if pass_name == "exact":
            full_spans = spans
        else:
            full_spans = _fuzzy_match_spans(content, old_string, normalize=normalize)
        line_nos = [_span_lines(content, s, e)[0] for s, e in full_spans]
        all_candidate_lines.extend(line_nos)
        snippet_pairs = [
            (n, _line_snippet_at(content, s))
            for n, (s, _e) in zip(line_nos, full_spans)
        ]
        shown = snippet_pairs[:_MAX_CANDIDATE_LINES]
        extra = len(snippet_pairs) - len(shown)
        snippet_block = "\nmatches at:\n" + "\n".join(
            f"  line {n}: {text!r}" for n, text in shown
        )
        if extra > 0:
            snippet_block += f"\n  ... and {extra} more"
        raise ValueError(
            f"multiple matches ({len(full_spans)} found); add line_number "
            "from read_file to target one match, or set replace_all=true."
            + snippet_block
        )

    if line_number is not None and any_candidates_found:
        raise ValueError(
            f"no match at line {line_number}; "
            f"matches found at lines {_format_candidate_lines(all_candidate_lines)}. "
            "Pass one of those line numbers, or reread the section."
        )

    base = (
        "old_string not found. The file's whitespace, casing, or punctuation "
        "may differ from what you sent."
    )
    closest = _format_closest_match_block(old_string, content)
    if closest:
        raise ValueError(
            base
            + " A near-match is shown below. If it is the intended target, retry "
            "with the actual text from the file. Otherwise reread the relevant section."
            + closest
        )
    raise ValueError(
        base
        + " No close match was found in the file; reread it with read_file to "
        "verify the exact text."
    )
