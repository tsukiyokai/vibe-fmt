#!/usr/bin/env python3
"""
Deterministic formatting engine for Chinese-English mixed markdown documents.

Reads a markdown file, applies formatting rules in order, writes the result
back, and prints a JSON summary of changes to stdout.

Rules (execution order optimized for idempotency):
  R2: Fix full-width/half-width punctuation by context
  R3: Convert full-width digits to half-width
  R1: Insert space between CJK and Latin/digit characters
  R1c: Remove spaces adjacent to full-width punctuation
  R4: Remove consecutive duplicate punctuation (preserve ……)
  R5: Ensure blank line before and after headings
  R6: Normalize list indentation
  R7: Align markdown table columns

R2/R3 run before R1 so that character substitutions are finalized
before spacing decisions are made, ensuring single-pass idempotency.
"""

import json
import re
import sys
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Unicode ranges
# ---------------------------------------------------------------------------

# CJK Unified Ideographs + Extension A + Compatibility Ideographs
CJK = r"\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"

# CJK full-width punctuation (these should NOT get a space inserted next to CJK)
CJK_PUNCT = r"\u3000-\u303f\uff00-\uffef"

# Full-width punctuation used in Chinese context
FW_COMMA = "\uff0c"      # ，
FW_PERIOD = "\u3002"     # 。
FW_SEMICOLON = "\uff1b"  # ；
FW_COLON = "\uff1a"      # ：
FW_QUESTION = "\uff1f"   # ？
FW_EXCLAIM = "\uff01"    # ！
FW_LPAREN = "\uff08"     # （
FW_RPAREN = "\uff09"     # ）
FW_LBRACKET = "\u3010"   # 【
FW_RBRACKET = "\u3011"   # 】

# Half-width equivalents
HW_PAIRS = {
    ",": FW_COMMA,
    ".": FW_PERIOD,      # only when in Chinese context
    ";": FW_SEMICOLON,
    ":": FW_COLON,
    "?": FW_QUESTION,
    "!": FW_EXCLAIM,
    "(": FW_LPAREN,
    ")": FW_RPAREN,
}

FW_TO_HW = {v: k for k, v in HW_PAIRS.items()}
# Add brackets (no simple half-width equivalent pair via HW_PAIRS)
FW_TO_HW[FW_LBRACKET] = "["
FW_TO_HW[FW_RBRACKET] = "]"

# Full-width digits ０-９
FW_DIGITS = {chr(0xFF10 + i): str(i) for i in range(10)}

# ---------------------------------------------------------------------------
# Protected region detection
# ---------------------------------------------------------------------------

class Span(NamedTuple):
    start: int
    end: int


def _find_protected_spans(text: str) -> list[Span]:
    """Return sorted, non-overlapping spans that must not be modified.

    Protected regions (detected in a single pass to avoid overlap issues):
      - YAML frontmatter (--- ... --- at file start)
      - Fenced code blocks (``` or ~~~)
      - Inline code (backtick spans within a single line)
      - URLs
      - Markdown link/image syntax
    """
    spans: list[Span] = []

    # YAML frontmatter (must be at the very start of the file)
    m = re.match(r"\A---\r?\n.*?\r?\n---\r?\n", text, re.DOTALL)
    if m:
        spans.append(Span(m.start(), m.end()))

    # Single-pass line-by-line parser for fenced code blocks and inline code
    lines = text.split("\n")
    pos = 0  # byte offset tracking
    in_fence = False
    fence_marker = ""
    fence_start = 0

    for line in lines:
        line_start = pos
        line_end = pos + len(line)

        if in_fence:
            # Check if this line closes the fence
            stripped = line.strip()
            if stripped == fence_marker or (
                stripped.startswith(fence_marker) and
                stripped == fence_marker + stripped[len(fence_marker):]
                and all(c == fence_marker[0] for c in stripped)
            ):
                # Close fence: protect from fence_start to end of this line
                spans.append(Span(fence_start, line_end))
                in_fence = False
            # else: still inside fence, continue
        else:
            # Check if this line opens a fence
            fence_m = re.match(r"^(\s*)(`{3,}|~{3,})(.*)", line)
            if fence_m:
                marker = fence_m.group(2)
                info = fence_m.group(3).strip()
                # Opening fence: marker followed by optional info string (no closing marker on same line)
                # A line with just ``` is ambiguous but we treat it as opening
                in_fence = True
                fence_marker = marker[0] * len(marker)  # normalize: same char, same count
                fence_start = line_start
            else:
                # Inline code detection within this line (no cross-line matching)
                # Handle `` code `` and ` code ` patterns
                for cm in re.finditer(r"(`{1,2})(?!`)(.*?[^`])\1(?!`)", line):
                    spans.append(Span(line_start + cm.start(), line_start + cm.end()))

        pos = line_end + 1  # +1 for the \n separator

    # If we ended inside a fence (unclosed), protect from fence_start to end
    if in_fence:
        spans.append(Span(fence_start, len(text)))

    # URLs (http/https/ftp)
    for m in re.finditer(r"https?://[^\s\)\]>\"']+|ftp://[^\s\)\]>\"']+", text):
        spans.append(Span(m.start(), m.end()))

    # Markdown link/image syntax: [text](url) and ![text](url)
    for m in re.finditer(r"!?\[[^\]]*\]\([^\)]+\)", text):
        spans.append(Span(m.start(), m.end()))

    # Sort and merge overlapping spans
    spans.sort()
    merged: list[Span] = []
    for s in spans:
        if merged and s.start <= merged[-1].end:
            merged[-1] = Span(merged[-1].start, max(merged[-1].end, s.end))
        else:
            merged.append(s)
    return merged


def _is_protected(pos: int, spans: list[Span]) -> bool:
    """Binary search to check if a position falls inside a protected span."""
    lo, hi = 0, len(spans) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if pos < spans[mid].start:
            hi = mid - 1
        elif pos >= spans[mid].end:
            lo = mid + 1
        else:
            return True
    return False


def _apply_to_unprotected(text: str, fn, spans: list[Span]) -> str:
    """Apply a transformation function only to unprotected regions.

    `fn(segment) -> str` receives an unprotected text segment and returns
    the replacement.  Protected segments are passed through unchanged.
    """
    parts: list[str] = []
    cursor = 0
    for span in spans:
        if cursor < span.start:
            parts.append(fn(text[cursor:span.start]))
        parts.append(text[span.start:span.end])
        cursor = span.end
    if cursor < len(text):
        parts.append(fn(text[cursor:]))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------

def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (0x4E00 <= cp <= 0x9FFF or
            0x3400 <= cp <= 0x4DBF or
            0xF900 <= cp <= 0xFAFF)


def _is_cjk_punct(ch: str) -> bool:
    cp = ord(ch)
    return (0x3000 <= cp <= 0x303F or
            0xFF00 <= cp <= 0xFFEF)


def rule_r1_spacing(text: str, spans: list[Span]) -> tuple[str, int]:
    """R1: Insert space between CJK and Latin/digit characters.

    Does NOT insert space between CJK and full-width punctuation.
    """
    count = 0

    def process(seg: str) -> str:
        nonlocal count
        # CJK followed by Latin/digit (no full-width punct in between)
        seg, n = re.subn(
            rf"([{CJK}])([A-Za-z0-9])",
            r"\1 \2",
            seg,
        )
        count += n
        # Latin/digit followed by CJK
        seg, n = re.subn(
            rf"([A-Za-z0-9])([{CJK}])",
            r"\1 \2",
            seg,
        )
        count += n
        return seg

    result = _apply_to_unprotected(text, process, spans)
    return result, count


def rule_r1c_fw_punct_spacing(text: str, spans: list[Span]) -> tuple[str, int]:
    """R1c: Remove spaces adjacent to full-width punctuation.

    Full-width punctuation already occupies visual space, so extra
    whitespace is redundant and violates Chinese typesetting convention.

    Only removes spaces when the adjacent non-space character is CJK,
    Latin letter, digit, or another full-width punctuation mark.
    Preserves spaces next to operators (+, >, =, etc.) and markdown
    syntax markers (-, #, |, etc.) to avoid breaking formatting.
    """
    count = 0

    fw_puncts = (FW_COMMA + FW_PERIOD + FW_SEMICOLON + FW_COLON +
                 FW_QUESTION + FW_EXCLAIM + FW_LPAREN + FW_RPAREN +
                 FW_LBRACKET + FW_RBRACKET)
    escaped = re.escape(fw_puncts)

    # Content characters: CJK, Latin, digits, full-width punctuation
    adj = CJK + r'A-Za-z0-9' + escaped

    def process(seg: str) -> str:
        nonlocal count
        # Remove spaces before full-width punctuation (only after content chars)
        seg, n = re.subn(r'(?<=[' + adj + r']) +([' + escaped + r'])', r'\1', seg)
        count += n
        # Remove spaces after full-width punctuation (only before content chars)
        seg, n = re.subn(r'([' + escaped + r']) +(?=[' + adj + r'])', r'\1', seg)
        count += n
        return seg

    result = _apply_to_unprotected(text, process, spans)
    return result, count


def rule_r2_punctuation(text: str, spans: list[Span]) -> tuple[str, int]:
    """R2: Fix full-width/half-width punctuation by context.

    Conservative: only fix when context is clearly Chinese or clearly English.
    - Half-width punct surrounded by CJK -> full-width
    - Full-width punct surrounded by ASCII/Latin -> half-width
    """
    count = 0

    def _has_cjk_nearby(s: str, pos: int, direction: int, window: int = 5) -> bool:
        """Check if there's a CJK character within `window` chars in given direction."""
        i = pos + direction
        steps = 0
        while 0 <= i < len(s) and steps < window:
            ch = s[i]
            if _is_cjk(ch):
                return True
            if ch in ("\n", "\r"):
                break
            i += direction
            steps += 1
        return False

    def _has_latin_nearby(s: str, pos: int, direction: int, window: int = 5) -> bool:
        """Check if there's a Latin/digit character within `window` chars in given direction."""
        i = pos + direction
        steps = 0
        while 0 <= i < len(s) and steps < window:
            ch = s[i]
            if re.match(r"[A-Za-z0-9]", ch):
                return True
            if ch in ("\n", "\r"):
                break
            i += direction
            steps += 1
        return False

    def process(seg: str) -> str:
        nonlocal count
        chars = list(seg)
        for i, ch in enumerate(chars):
            # Half-width -> full-width in Chinese context
            if ch in HW_PAIRS:
                # Special care: '.' is too ambiguous (decimal points, abbreviations, etc.)
                # Only convert if it's clearly sentence-ending in Chinese context
                if ch == ".":
                    # Skip if part of '...' (ellipsis) or other consecutive dots
                    prev_dot = (i > 0 and seg[i - 1] == ".")
                    next_dot = (i + 1 < len(seg) and seg[i + 1] == ".")
                    if prev_dot or next_dot:
                        continue
                    # Skip if between digits: decimal point, version number,
                    # or section reference (e.g. "3.1", "v1.0", "0.39")
                    prev_digit = (i > 0 and seg[i - 1].isdigit())
                    next_digit = (i + 1 < len(seg) and seg[i + 1].isdigit())
                    if prev_digit and next_digit:
                        continue
                    # Only convert bare '.' that sits between CJK on both sides
                    left_cjk = _has_cjk_nearby(seg, i, -1, 2)
                    right_cjk = _has_cjk_nearby(seg, i, 1, 2)
                    if left_cjk and right_cjk:
                        chars[i] = HW_PAIRS[ch]
                        count += 1
                elif ch in (",", "?", "!", ";", ":"):
                    left_cjk = _has_cjk_nearby(seg, i, -1)
                    right_cjk = _has_cjk_nearby(seg, i, 1)
                    if left_cjk and right_cjk:
                        chars[i] = HW_PAIRS[ch]
                        count += 1
                elif ch in ("(", ")"):
                    left_cjk = _has_cjk_nearby(seg, i, -1)
                    right_cjk = _has_cjk_nearby(seg, i, 1)
                    if left_cjk and right_cjk:
                        chars[i] = HW_PAIRS[ch]
                        count += 1

            # Full-width -> half-width in English context
            elif ch in FW_TO_HW:
                left_latin = _has_latin_nearby(seg, i, -1)
                right_latin = _has_latin_nearby(seg, i, 1)
                left_cjk = _has_cjk_nearby(seg, i, -1)
                right_cjk = _has_cjk_nearby(seg, i, 1)
                # Only convert if both sides are Latin/digit and neither side is CJK
                if left_latin and right_latin and not left_cjk and not right_cjk:
                    chars[i] = FW_TO_HW[ch]
                    count += 1

        return "".join(chars)

    result = _apply_to_unprotected(text, process, spans)
    return result, count


def rule_r2b_bracket_pairing(text: str, spans: list[Span]) -> tuple[str, int]:
    """R2b: Fix mismatched bracket pairs.

    After R2 converts individual brackets by context, open/close brackets
    may end up mismatched (e.g., （...） or (...）).  This pass uses a
    stack to pair brackets and normalizes each pair:
      - Content contains CJK -> full-width （）
      - Content is pure ASCII  -> half-width ()
    """
    count = 0

    def process(seg: str) -> str:
        nonlocal count
        chars = list(seg)
        stack: list[tuple[int, str]] = []  # (position, char)

        for i, ch in enumerate(chars):
            if ch in ("(", "\uff08"):       # ( or （
                stack.append((i, ch))
            elif ch in (")", "\uff09"):      # ) or ）
                if not stack:
                    continue
                open_pos, open_ch = stack.pop()
                # Already matched?
                if (open_ch == "(" and ch == ")") or (open_ch == "\uff08" and ch == "\uff09"):
                    continue
                # Mismatched — decide width from content between brackets
                content = "".join(chars[open_pos + 1 : i])
                use_fullwidth = any(_is_cjk(c) for c in content)
                if use_fullwidth:
                    if chars[open_pos] != "\uff08":
                        chars[open_pos] = "\uff08"
                        count += 1
                    if chars[i] != "\uff09":
                        chars[i] = "\uff09"
                        count += 1
                else:
                    if chars[open_pos] != "(":
                        chars[open_pos] = "("
                        count += 1
                    if chars[i] != ")":
                        chars[i] = ")"
                        count += 1

        return "".join(chars)

    result = _apply_to_unprotected(text, process, spans)
    return result, count


def rule_r3_fullwidth_digits(text: str, spans: list[Span]) -> tuple[str, int]:
    """R3: Convert full-width digits to half-width (０１２ -> 012)."""
    count = 0
    fw_digit_pattern = re.compile("[" + "".join(FW_DIGITS.keys()) + "]")

    def process(seg: str) -> str:
        nonlocal count

        def replace_digit(m):
            nonlocal count
            count += 1
            return FW_DIGITS[m.group()]

        return fw_digit_pattern.sub(replace_digit, seg)

    result = _apply_to_unprotected(text, process, spans)
    return result, count


def rule_r4_duplicate_punctuation(text: str, spans: list[Span]) -> tuple[str, int]:
    """R4: Remove consecutive duplicate punctuation.

    Preserves …… (correct Chinese ellipsis) and ... (English ellipsis).
    """
    count = 0

    # Punctuation that should not be duplicated
    dedup_chars = (
        FW_EXCLAIM + FW_QUESTION + FW_COMMA + FW_PERIOD +
        FW_SEMICOLON + FW_COLON +
        "!?,.;:"
    )
    # Build pattern: 2+ consecutive identical punctuation from our set
    # But NOT '.' repeated (to avoid breaking '...' -> '.')
    # And NOT '…' repeated (to preserve '……')
    pattern = re.compile(
        r"([" + re.escape(dedup_chars) + r"])\1+"
    )

    def process(seg: str) -> str:
        nonlocal count

        def dedup(m):
            nonlocal count
            ch = m.group(1)
            # Preserve '...' and '……'
            if ch == "." or ch == "\u2026":
                return m.group(0)
            count += 1
            return ch

        return pattern.sub(dedup, seg)

    # Also handle '……' separately: it's fine, but '…………' (3+ …) should reduce to ……
    def process_ellipsis(seg: str) -> str:
        nonlocal count
        def fix_long_ellipsis(m):
            nonlocal count
            if len(m.group(0)) > 2:
                count += 1
                return "\u2026\u2026"
            return m.group(0)
        return re.sub(r"\u2026{2,}", fix_long_ellipsis, seg)

    result = _apply_to_unprotected(text, process, spans)
    # Recompute spans after first pass (text length may have changed)
    spans2 = _find_protected_spans(result)
    result = _apply_to_unprotected(result, process_ellipsis, spans2)
    return result, count


def rule_r5_heading_spacing(text: str) -> tuple[str, int]:
    """R5: Ensure blank line before and after headings.

    Operates on whole lines, respects code blocks by tracking fenced state.
    """
    lines = text.split("\n")
    result: list[str] = []
    count = 0
    in_code_block = False
    in_frontmatter = False

    # Detect frontmatter
    if lines and lines[0].strip() == "---":
        in_frontmatter = True

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Track frontmatter
        if in_frontmatter:
            result.append(line)
            if i > 0 and stripped == "---":
                in_frontmatter = False
            continue

        # Track fenced code blocks
        if re.match(r"^(`{3,}|~{3,})", stripped):
            in_code_block = not in_code_block
            result.append(line)
            continue

        if in_code_block:
            result.append(line)
            continue

        # Check if current line is a heading
        is_heading = bool(re.match(r"^#{1,6}\s", stripped))

        if is_heading:
            # Ensure blank line before heading (unless it's the first line or
            # preceded by frontmatter end)
            if result and result[-1].strip() != "":
                result.append("")
                count += 1
            result.append(line)
            # We'll handle "after" when processing the next line
        else:
            # Check if previous non-blank line was a heading
            if (result and result[-1].strip() != "" and
                    re.match(r"^#{1,6}\s", result[-1].strip()) and
                    stripped != ""):
                result.append("")
                count += 1
            result.append(line)

    return "\n".join(result), count


def rule_r6_list_indentation(text: str) -> tuple[str, int]:
    """R6: Normalize list indentation to consistent 2-space levels.

    Uses an indent stack to detect nesting levels from the original
    indentation, then re-indents each level to exactly 2 spaces.
    Operates line-by-line, respects code blocks.
    """
    lines = text.split("\n")
    result: list[str] = []
    count = 0
    in_code_block = False

    # Pattern for list items: optional whitespace + marker (- * + or 1. 2. etc.) + space
    list_pattern = re.compile(r"^(\s*)([-*+]|\d+\.)\s")

    # indent_stack tracks the raw indentation widths we've seen for each nesting level.
    # E.g., [0, 4, 8] means level 0 = 0 spaces, level 1 = 4 spaces, level 2 = 8 spaces.
    indent_stack: list[int] = []

    for line in lines:
        stripped = line.strip()

        if re.match(r"^(`{3,}|~{3,})", stripped):
            in_code_block = not in_code_block
            result.append(line)
            continue

        if in_code_block:
            result.append(line)
            continue

        m = list_pattern.match(line)
        if m:
            indent = m.group(1)
            marker = m.group(2)
            rest = line[m.end():]

            raw_width = len(indent.replace("\t", "    "))

            # Determine nesting level by comparing to the indent stack
            if not indent_stack:
                # First list item
                indent_stack = [raw_width]
                level = 0
            elif raw_width > indent_stack[-1]:
                # Deeper nesting
                indent_stack.append(raw_width)
                level = len(indent_stack) - 1
            elif raw_width <= indent_stack[0]:
                # Back to top level (or less)
                indent_stack = [raw_width]
                level = 0
            else:
                # Find the closest matching level
                # Pop levels that are deeper than current
                while len(indent_stack) > 1 and indent_stack[-1] > raw_width:
                    indent_stack.pop()
                level = len(indent_stack) - 1
                # If current width doesn't exactly match, it's a new sub-level
                if indent_stack[-1] < raw_width:
                    indent_stack.append(raw_width)
                    level = len(indent_stack) - 1

            new_indent = "  " * level
            new_line = f"{new_indent}{marker} {rest}"

            if new_line != line:
                count += 1
                result.append(new_line)
            else:
                result.append(line)
        else:
            # Non-list line resets the indent stack
            if stripped == "":
                pass  # blank lines don't reset (lists can have blank lines between items)
            else:
                indent_stack = []
            result.append(line)

    return "\n".join(result), count


def rule_r7_table_alignment(text: str) -> tuple[str, int]:
    """R7: Align markdown table columns by padding cells with spaces.

    Operates on consecutive table lines, respects code blocks.
    """
    lines = text.split("\n")
    result: list[str] = []
    count = 0
    in_code_block = False

    table_line_pattern = re.compile(r"^\s*\|.*\|\s*$")
    separator_pattern = re.compile(r"^\s*\|[\s:-]+(\|[\s:-]+)*\|\s*$")

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        if re.match(r"^(`{3,}|~{3,})", stripped):
            in_code_block = not in_code_block
            result.append(lines[i])
            i += 1
            continue

        if in_code_block:
            result.append(lines[i])
            i += 1
            continue

        # Detect start of a table block
        if table_line_pattern.match(lines[i]):
            table_lines: list[str] = []
            while i < len(lines) and table_line_pattern.match(lines[i]):
                table_lines.append(lines[i])
                i += 1

            # Parse table into cells
            parsed: list[list[str]] = []
            sep_indices: list[int] = []
            for ti, tl in enumerate(table_lines):
                # Split by | but ignore leading/trailing
                inner = tl.strip()
                if inner.startswith("|"):
                    inner = inner[1:]
                if inner.endswith("|"):
                    inner = inner[:-1]
                cells = [c.strip() for c in inner.split("|")]
                parsed.append(cells)
                if separator_pattern.match(tl):
                    sep_indices.append(ti)

            if not parsed:
                result.extend(table_lines)
                continue

            # Find max column count
            max_cols = max(len(row) for row in parsed)

            # Pad rows to have same number of columns
            for row in parsed:
                while len(row) < max_cols:
                    row.append("")

            # Compute display width for each cell (CJK chars count as 2)
            def display_width(s: str) -> int:
                w = 0
                for ch in s:
                    cp = ord(ch)
                    if (_is_cjk(ch) or _is_cjk_punct(ch) or
                            0x2E80 <= cp <= 0x9FFF or
                            0xF900 <= cp <= 0xFAFF or
                            0xFE30 <= cp <= 0xFE4F or
                            0xFF00 <= cp <= 0xFFEF):
                        w += 2
                    else:
                        w += 1
                return w

            # Compute max width per column
            col_widths = [0] * max_cols
            for ri, row in enumerate(parsed):
                if ri in sep_indices:
                    continue  # skip separator row for width calculation
                for ci, cell in enumerate(row):
                    w = display_width(cell)
                    if w > col_widths[ci]:
                        col_widths[ci] = w

            # Ensure minimum width of 3 for separator dashes
            col_widths = [max(w, 3) for w in col_widths]

            # Rebuild table
            new_table_lines: list[str] = []
            for ri, row in enumerate(parsed):
                if ri in sep_indices:
                    # Rebuild separator: preserve alignment markers
                    parts: list[str] = []
                    orig_inner = table_lines[ri].strip()
                    if orig_inner.startswith("|"):
                        orig_inner = orig_inner[1:]
                    if orig_inner.endswith("|"):
                        orig_inner = orig_inner[:-1]
                    orig_cells = [c.strip() for c in orig_inner.split("|")]
                    for ci in range(max_cols):
                        w = col_widths[ci]
                        oc = orig_cells[ci] if ci < len(orig_cells) else "---"
                        left_colon = oc.startswith(":")
                        right_colon = oc.endswith(":")
                        dash_count = w - (1 if left_colon else 0) - (1 if right_colon else 0)
                        sep_cell = (":" if left_colon else "") + "-" * dash_count + (":" if right_colon else "")
                        parts.append(f" {sep_cell} ")
                    new_line = "|" + "|".join(parts) + "|"
                else:
                    parts = []
                    for ci, cell in enumerate(row):
                        w = display_width(cell)
                        padding = col_widths[ci] - w
                        parts.append(f" {cell}{' ' * padding} ")
                    new_line = "|" + "|".join(parts) + "|"

                new_table_lines.append(new_line)

            # Count changes
            for old, new in zip(table_lines, new_table_lines):
                if old != new:
                    count += 1

            result.extend(new_table_lines)
        else:
            result.append(lines[i])
            i += 1

    return "\n".join(result), count


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def format_document(text: str) -> tuple[str, dict[str, int]]:
    """Apply all formatting rules in order and return (formatted_text, change_counts)."""
    changes: dict[str, int] = {}

    # Run character-level transformations (R2, R3) before spacing (R1)
    # so that R1 sees the final characters when deciding where to add spaces.

    # R2: Punctuation context fix
    spans = _find_protected_spans(text)
    text, n = rule_r2_punctuation(text, spans)
    changes["R2_punctuation"] = n

    # R2b: Fix mismatched bracket pairs (must run after R2)
    spans = _find_protected_spans(text)
    text, n = rule_r2b_bracket_pairing(text, spans)
    changes["R2b_bracket_pairing"] = n

    # R3: Full-width digits
    spans = _find_protected_spans(text)
    text, n = rule_r3_fullwidth_digits(text, spans)
    changes["R3_digits"] = n

    # R1: CJK-Latin spacing (after R2/R3 so all characters are finalized)
    spans = _find_protected_spans(text)
    text, n = rule_r1_spacing(text, spans)
    changes["R1_spacing"] = n

    # R1c: Remove spaces around full-width punctuation (cleanup after R1)
    spans = _find_protected_spans(text)
    text, n = rule_r1c_fw_punct_spacing(text, spans)
    changes["R1c_fw_punct_spacing"] = n

    # R4: Duplicate punctuation
    spans = _find_protected_spans(text)
    text, n = rule_r4_duplicate_punctuation(text, spans)
    changes["R4_dedup_punct"] = n

    # R5: Heading spacing (line-based, handles code blocks internally)
    text, n = rule_r5_heading_spacing(text)
    changes["R5_heading_spacing"] = n

    # R6: List indentation (line-based, handles code blocks internally)
    text, n = rule_r6_list_indentation(text)
    changes["R6_list_indent"] = n

    # R7: Table alignment (line-based, handles code blocks internally)
    text, n = rule_r7_table_alignment(text)
    changes["R7_table_align"] = n

    return text, changes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: format.py <file>", file=sys.stderr)
        sys.exit(1)

    filepath = sys.argv[1]

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            original = f.read()
    except FileNotFoundError:
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error reading file: {e}", file=sys.stderr)
        sys.exit(1)

    formatted, changes = format_document(original)

    # Write back
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(formatted)

    total = sum(changes.values())
    summary = {
        "total_changes": total,
        "by_rule": changes,
        "file": filepath,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
