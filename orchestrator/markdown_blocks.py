"""A report's text as a list of blocks a page can lay out.

Reports are Markdown written by agents, so they are untrusted text. This
module only sorts lines into kinds (heading, paragraph, list, table, code,
quote, rule) and marks inline code and bold. It builds no markup, follows
no link and never raises: what it does not recognise is a paragraph, and
every line of the file ends up in some block.

Standalone: it imports nothing from Relay.
"""

import re

HEADING = "heading"
PARAGRAPH = "paragraph"
LIST = "list"
TABLE = "table"
CODE = "code"
QUOTE = "quote"
RULE = "rule"

SPAN_TEXT = "text"
SPAN_CODE = "code"
SPAN_STRONG = "strong"

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*)$")
_RULE = re.compile(r"^ {0,3}(-{3,}|\*{3,}|_{3,})[ \t]*$")
_LIST_ITEM = re.compile(r"^([ \t]*)([-*+]|[0-9]{1,9}[.)])[ \t]+(.*)$")
_SEPARATOR_CELL = re.compile(r"^:?-+:?$")
_INLINE_MARK = re.compile(r"`+|\*\*")
_CELL_BREAK = re.compile(r"(?<!\\)\|")

_SPACES_PER_LIST_LEVEL = 2
_DEEPEST_LIST_LEVEL = 6


def split_lines(text: str) -> list[str]:
    """The text's lines. A line ends at a newline, with or without a
    carriage return before it, and nowhere else: a lone carriage return, a
    form feed, a vertical tab or a Unicode line separator is a character
    inside its line. This is the one definition of a line that the block
    splitter and the report reader both use, so text cannot be one line to
    one of them and several to the other."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def heading_level(line: str) -> int | None:
    """1 to 6 when the line is a heading, else None."""
    heading = _HEADING.match(line)
    return len(heading.group(1)) if heading else None


def to_spans(text: str) -> list[dict]:
    """Text as spans: plain text, `inline code` and **bold**. Nothing else
    is recognised, so a link, an image or an HTML tag stays the characters
    that were written."""
    spans = []
    marks = list(_INLINE_MARK.finditer(text))
    never_closed = set()   # a mark with no partner ahead has none further on either
    plain_from = 0
    index = 0
    while index < len(marks):
        opener = marks[index]
        closer_index = None
        if opener.group() not in never_closed:
            closer_index = _closing_mark(marks, index)
            if closer_index is None:
                never_closed.add(opener.group())
        if closer_index is None:
            index += 1
            continue
        closer = marks[closer_index]
        inner = text[opener.end():closer.start()]
        if not inner.strip():
            index += 1
            continue
        if opener.start() > plain_from:
            spans.append({"kind": SPAN_TEXT, "text": text[plain_from:opener.start()]})
        kind = SPAN_STRONG if opener.group() == "**" else SPAN_CODE
        spans.append({"kind": kind, "text": inner})
        plain_from = closer.end()
        index = closer_index + 1
    if plain_from < len(text):
        spans.append({"kind": SPAN_TEXT, "text": text[plain_from:]})
    return spans


def _closing_mark(marks: list, opener_index: int) -> int | None:
    wanted = marks[opener_index].group()
    for index in range(opener_index + 1, len(marks)):
        if marks[index].group() == wanted:
            return index
    return None


def spans_text(spans: list[dict]) -> str:
    return "".join(span["text"] for span in spans)


def split_row(line: str) -> list[str]:
    """The cells of one table line. `\\|` inside a cell is a literal bar."""
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [cell.strip().replace("\\|", "|") for cell in _CELL_BREAK.split(row)]


def is_separator_row(cells: list[str]) -> bool:
    """The `| --- | --- |` line under a table's header."""
    return bool(cells) and all(_SEPARATOR_CELL.match(cell) for cell in cells)


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def _is_quote_line(line: str) -> bool:
    return line.lstrip().startswith(">")


def _frontmatter_end(lines: list[str]) -> int | None:
    """Index of the line that closes the frontmatter, if the file has one."""
    if not lines or lines[0].rstrip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].rstrip() == "---":
            return index
    return None


def _fence_end(lines: list[str], start: int) -> int:
    """Index of the line that closes the code fence opened at `start`. A
    fence nobody closed runs to the end of the file (the last index + 1)."""
    opening = _FENCE.match(lines[start]).group(1)
    for index in range(start + 1, len(lines)):
        closing = _FENCE.match(lines[index])
        if (closing and closing.group(1)[0] == opening[0]
                and len(closing.group(1)) >= len(opening) and not closing.group(2).strip()):
            return index
    return len(lines)


def _starts_a_block(line: str) -> bool:
    return bool(_FENCE.match(line) or _HEADING.match(line) or _RULE.match(line)
                or _LIST_ITEM.match(line) or _is_table_line(line) or _is_quote_line(line))


def _table_block(lines: list[str]) -> dict:
    rows = [split_row(line) for line in lines]
    header = []
    if len(rows) > 1 and is_separator_row(rows[1]):
        header, rows = rows[0], rows[2:]
    rows = [row for row in rows if not is_separator_row(row)]
    return {"kind": TABLE,
            "header": [to_spans(cell) for cell in header],
            "rows": [[to_spans(cell) for cell in row] for row in rows]}


def _list_block(lines: list[str]) -> dict:
    """Items keep the marker that was written ("3." or "-"), so a list that
    starts at 3 or skips a number reads as its writer numbered it."""
    items = []
    for line in lines:
        item = _LIST_ITEM.match(line)
        if item is None:
            items[-1]["text"] += "\n" + line.strip()
            continue
        indent = len(item.group(1).replace("\t", " " * 4))
        marker = item.group(2)
        items.append({"text": item.group(3),
                      "depth": min(indent // _SPACES_PER_LIST_LEVEL, _DEEPEST_LIST_LEVEL),
                      "marker": marker if marker[0].isdigit() else None})
    return {"kind": LIST,
            "ordered": items[0]["marker"] is not None,
            "items": [{"spans": to_spans(item["text"]), "depth": item["depth"],
                       "marker": item["marker"]} for item in items]}


def _quote_block(lines: list[str]) -> dict:
    text = "\n".join(re.sub(r"^[ \t]*> ?", "", line) for line in lines)
    return {"kind": QUOTE, "spans": to_spans(text)}


def to_blocks(text: str) -> list[dict]:
    """The whole text as blocks, top to bottom. Frontmatter is shown as one
    leading code block, so the page shows every line of the file."""
    lines = split_lines(text)
    blocks = []
    index = 0

    frontmatter_end = _frontmatter_end(lines)
    if frontmatter_end is not None:
        blocks.append({"kind": CODE, "info": "",
                       "text": "\n".join(lines[:frontmatter_end + 1])})
        index = frontmatter_end + 1

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        fence = _FENCE.match(line)
        if fence:
            end = _fence_end(lines, index)
            blocks.append({"kind": CODE, "info": fence.group(2).strip(),
                           "text": "\n".join(lines[index + 1:end])})
            index = end + 1
            continue

        heading = _HEADING.match(line)
        if heading:
            blocks.append({"kind": HEADING, "level": len(heading.group(1)),
                           "spans": to_spans(heading.group(2).strip())})
            index += 1
            continue

        if _RULE.match(line):
            blocks.append({"kind": RULE})
            index += 1
            continue

        if _is_table_line(line):
            end = _run_end(lines, index, _is_table_line)
            blocks.append(_table_block(lines[index:end]))
        elif _is_quote_line(line):
            end = _run_end(lines, index, _is_quote_line)
            blocks.append(_quote_block(lines[index:end]))
        elif _LIST_ITEM.match(line):
            end = _run_end(lines, index, _continues_a_list)
            blocks.append(_list_block(lines[index:end]))
        else:
            end = _run_end(lines, index, lambda later: not _starts_a_block(later))
            paragraph = "\n".join(part.strip() for part in lines[index:end])
            blocks.append({"kind": PARAGRAPH, "spans": to_spans(paragraph)})
        index = end
    return blocks


def _continues_a_list(line: str) -> bool:
    """A further item, or an indented line that carries on the item above."""
    if _LIST_ITEM.match(line):
        return True
    return line[:1] in (" ", "\t") and not _FENCE.match(line) and not _is_table_line(line)


def _run_end(lines: list[str], start: int, belongs) -> int:
    """Index just past the run of non-blank lines from `start` that `belongs`
    accepts. The line at `start` is always part of the run."""
    index = start + 1
    while index < len(lines) and lines[index].strip() and belongs(lines[index]):
        index += 1
    return index


def body_lines(text: str) -> list[str]:
    """The text's lines after its frontmatter."""
    lines = split_lines(text)
    frontmatter_end = _frontmatter_end(lines)
    return lines if frontmatter_end is None else lines[frontmatter_end + 1:]


def is_fence(line: str) -> bool:
    """Whether the line opens (or could close) a code fence."""
    return bool(_FENCE.match(line))


def lines_outside_code(text: str) -> list[str]:
    """The text's lines, leaving out frontmatter and everything inside a
    code fence. An example quoted in a code block is not the real thing."""
    lines = body_lines(text)
    kept = []
    index = 0
    while index < len(lines):
        if _FENCE.match(lines[index]):
            index = _fence_end(lines, index) + 1
            continue
        kept.append(lines[index])
        index += 1
    return kept


def has_unclosed_fence(text: str) -> bool:
    """Whether a code fence is opened and never closed, so that everything
    after it is laid out as code."""
    lines = body_lines(text)
    index = 0
    while index < len(lines):
        if _FENCE.match(lines[index]):
            index = _fence_end(lines, index)
            if index == len(lines):
                return True
        index += 1
    return False


def sections(blocks: list[dict]) -> list[dict]:
    """One entry per heading: its text, its level, and the blocks it covers
    (`first` is the heading itself). A section runs to the next heading of
    the same or a higher level."""
    found = []
    for index, block in enumerate(blocks):
        if block["kind"] != HEADING:
            continue
        last = len(blocks) - 1
        for later in range(index + 1, len(blocks)):
            if blocks[later]["kind"] == HEADING and blocks[later]["level"] <= block["level"]:
                last = later - 1
                break
        found.append({"title": spans_text(block["spans"]), "level": block["level"],
                      "first": index, "last": last})
    return found
