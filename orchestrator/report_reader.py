"""What Relay lifts out of a role's report as written, for the gate card:
the summary line for the owner, the rated findings table and Security's
capped-verdict line.

The strings below are the contract between the Reviewer, Tester and Security
prompts (`agents/*.md`) and Relay. They live here once; the prompts carry
the same text and tests/test_agent_prompts.py fails if they drift.

Standalone: pure text in, data out. It imports nothing layered.
"""

import re

import markdown_blocks

SUMMARY_PREFIX = "Summary for the owner:"
RATED_HEADING = "## Rated findings"
CAPPED_PREFIX = "Capped verdict:"

NO_FINDINGS_LINE = "None."
SEVERITIES = ("high", "medium", "low")   # highest first
SEVERITY_COLUMN = "severity"
RATED_ROW_CELLS = 4   # number, severity, finding, place

# The most of a report file Relay reads. A larger one is not read at all.
MAX_REPORT_BYTES = 1024 * 1024
_FINDING_NUMBER = re.compile(r"^[0-9]+$")
_TITLE_LEVEL = 1
_SECTION_END_LEVELS = (1, 2)

# Loose patterns, used only to decide what the board may claim, never to
# read anything. Each matches more than the strict reader accepts.
# A line that begins with the words "rated findings", whatever markup,
# spacing or letter case it is written with.
_RATED_HEADING_LIKE = re.compile(r"[\W_\d]*rated[\W_]*findings", re.IGNORECASE)
# A number, a bar, then a severity word: what a rated row looks like,
# wherever it sits in a line.
_RATED_ROW_LIKE = re.compile(r"(?<![0-9])[0-9]+\.?[\s*_`]*\|[\s*_`]*(?:high|medium|low)\b",
                             re.IGNORECASE)


def read_report(text: str) -> dict:
    """Everything the board needs from one report. Never raises on odd
    text: what cannot be read is reported as missing, not guessed.

    A report quotes other people's text, and no rule about where a line
    sits can tell every quote from the role's own words. So the reader does
    two things. It reads each thing strictly, from the one place the role
    prompts put it. And it looks over the whole file loosely (frontmatter,
    code and quotes included) for anything else that could be the same
    thing; when it finds any, it says so (`summary_in_doubt`,
    `findings_complete`, `says_none`) and the board claims less.

    `says_none` is the single line the prompts ask for and nothing else."""
    lines = markdown_blocks.lines_outside_code(text)
    blocks = markdown_blocks.to_blocks(text)
    top = _top_lines(markdown_blocks.body_lines(text))
    rated_sections = _rated_sections(lines)
    findings, unreadable_rows = _read_rows(
        [line for section in rated_sections for line in section])
    summary_line = _said_once(text, top, SUMMARY_PREFIX)
    read_one_section_cleanly = (len(rated_sections) == 1
                                and _rated_heading_like_lines(text) == 1
                                and not markdown_blocks.has_unclosed_fence(text))
    return {
        "summary_line": summary_line,
        "summary_in_doubt": summary_line is None and _mentions(text, SUMMARY_PREFIX) > 0,
        "rated": bool(rated_sections),
        "rated_sections": len(rated_sections),
        "findings": findings,
        "unreadable_rows": unreadable_rows,
        "findings_complete": (read_one_section_cleanly
                              and len(_RATED_ROW_LIKE.findall(text)) == len(findings)),
        "says_none": (read_one_section_cleanly and not findings and not unreadable_rows
                      and _says_only_none(rated_sections[0])),
        "capped_declared": _capped_declared(text, top),
        "blocks": blocks,
        "sections": markdown_blocks.sections(blocks),
    }


def _is_title(line: str) -> bool:
    return line.startswith("#") and markdown_blocks.heading_level(line) == _TITLE_LEVEL


def _top_lines(body: list[str]) -> list[str]:
    """The lines directly under the report's title, up to the next heading
    or code fence: where the summary line and the capped line belong. The
    title is the first line of the report that is not blank; a level-1
    heading further down is not the title, so nothing under it is read."""
    first = next((index for index, line in enumerate(body) if line.strip()), None)
    if first is None or not _is_title(body[first]):
        return []
    top = []
    for line in body[first + 1:]:
        if markdown_blocks.heading_level(line) is not None or markdown_blocks.is_fence(line):
            break
        top.append(line)
    return top


def _mentions(text: str, words: str) -> int:
    """How often the words appear anywhere in the file, in any letter case."""
    return text.casefold().count(words.casefold())


def _said_once(text: str, top: list[str], prefix: str) -> str | None:
    """What follows `prefix` on the line under the title that starts with
    it. None when no line there does, when nothing follows it, or when the
    same words appear anywhere else in the file, in a quote or in code: the
    board cannot be sure which is the role's own, so it shows neither."""
    said = [line[len(prefix):].strip() for line in top if line.startswith(prefix)]
    if len(said) != 1 or _mentions(text, prefix) != 1:
        return None
    return said[0] or None


def _capped_declared(text: str, top: list[str]) -> bool | None:
    answer = (_said_once(text, top, CAPPED_PREFIX) or "").lower()
    return {"yes": True, "no": False}.get(answer)


def _rated_sections(lines: list[str]) -> list[list[str]]:
    """The lines under each rated findings heading (see _sections_headed).
    Empty when the report has no such heading: it was written before
    findings were rated."""
    return _sections_headed(lines, RATED_HEADING)


def _sections_headed(lines: list[str], heading: str) -> list[list[str]]:
    """The lines under each `heading`, up to the next heading of level 1
    or 2.

    The heading counts only as a line of its own starting in the first
    column, which the block splitter always lays out as a heading; its
    letters may be in any case, as a part's name may. A report
    should have one. A copy quoted at the start of a line cannot be told
    from the role's own, so every such section is read and its rows are
    added together: a copy can add rows, never take the role's rows away."""
    found = []
    section = None
    for line in lines:
        if line.rstrip().casefold() == heading.casefold():
            section = []
            found.append(section)
        elif markdown_blocks.heading_level(line) in _SECTION_END_LEVELS:
            section = None
        elif section is not None:
            section.append(line)
    return found


def _rated_heading_like_lines(text: str) -> int:
    """How many lines of the whole file could be a rated findings heading:
    the one the reader took, one written slightly off, one in a quote or a
    code block. Lines are split on every character anything treats as a
    line end, so this never counts fewer than a stricter reader would."""
    return sum(bool(_RATED_HEADING_LIKE.match(line)) for line in text.splitlines())


def _says_only_none(section: list[str]) -> bool:
    """Whether the section's own words are the single line the prompts ask
    for when there is nothing to list."""
    said = [line.strip() for line in section
            if line.strip() and not line.lstrip().startswith("|")]
    return said == [NO_FINDINGS_LINE]


def _read_rows(section_lines: list[str]) -> tuple[list[dict], list[str]]:
    """Table rows as (rated findings, rows that could not be read). A row
    that breaks the format is kept as written, never dropped."""
    findings, unreadable = [], []
    for line in section_lines:
        if not line.lstrip().startswith("|"):
            continue
        cells = markdown_blocks.split_row(line)
        if markdown_blocks.is_separator_row(cells) or _is_header_row(cells):
            continue
        finding = _rated_finding(cells)
        if finding is None:
            unreadable.append(line.strip())
        else:
            findings.append(finding)
    return findings, unreadable


def _is_header_row(cells: list[str]) -> bool:
    return len(cells) > 1 and cells[1].lower() == SEVERITY_COLUMN


def _rated_finding(cells: list[str]) -> dict | None:
    if len(cells) != RATED_ROW_CELLS:
        return None
    number, severity, statement, place = cells
    if not _FINDING_NUMBER.match(number) or severity.lower() not in SEVERITIES or not statement:
        return None
    return {"number": number, "severity": severity.lower(), "statement": statement,
            "place": place}


def find_sections(report: dict, names: list[str]) -> dict[str, dict | None]:
    """For each wanted name, the report's first section whose heading is
    that name, or starts with it followed by a space or "(" (so "Not
    verified" finds "Not verified (reasoned about, not executed)"). Letter
    case is ignored. None where the report has no such heading."""
    return {name: next((section for section in report["sections"]
                        if _heading_matches(section["title"], name)), None)
            for name in names}


def _heading_matches(title: str, name: str) -> bool:
    title, name = title.strip().casefold(), name.casefold()
    return title == name or title.startswith((name + " ", name + "("))
