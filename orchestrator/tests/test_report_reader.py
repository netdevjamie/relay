"""The contract between a role's report and the board: the summary line,
the rated findings table and the capped-verdict line, read as written.
"""

import time
from pathlib import Path

import pytest

import report_reader
from report_reader import read_report

REPORTS = Path(__file__).resolve().parent / "fixtures" / "reports"

TABLE_HEAD = "| # | Severity | Finding | Place |\n| --- | --- | --- | --- |\n"


def fixture(name: str) -> dict:
    return read_report((REPORTS / name).read_text())


def rated(rows: str) -> dict:
    return read_report(f"# Title\n\n## Rated findings\n\n{TABLE_HEAD}{rows}\n## Findings\n")


class TestNewFormatReports:
    def test_new_format_reports_read_without_help(self):
        """C29: a Reviewer, Tester and Security report in the new format,
        every finding with its severity, statement and place."""
        reviewer = fixture("reviewer-rated.md")
        assert reviewer["rated"] is True
        assert reviewer["unreadable_rows"] == []
        assert reviewer["findings"] == [
            {"number": "1", "severity": "high", "place": "web/export.py:41",
             "statement": "An export of another team's report is allowed when its id is typed "
                          "into the address."},
            {"number": "2", "severity": "medium", "place": "web/export.py:77",
             "statement": "The export loads every row into memory, so the planned columns task "
                          "will time out on large reports."}]

        tester = fixture("tester-rated.md")
        assert [(finding["number"], finding["severity"], finding["place"])
                for finding in tester["findings"]] == [
            ("1", "medium", "docs/manual-tests/add-export-button.md, step 4"),
            ("2", "low", "web/export.py:90")]

        security = fixture("security-rated.md")
        assert [(finding["severity"], finding["place"]) for finding in security["findings"]] == [
            ("high", "web/export.py:41"), ("medium", "web/export.py:88"), ("low", "whole change")]
        assert security["findings"][2]["statement"] == "The error page names the framework."

    def test_summary_line(self):
        assert fixture("security-rated.md")["summary_line"] == (
            "Security found 3 gaps. One can be abused now.")
        assert fixture("reviewer-old.md")["summary_line"] is None

    def test_a_long_summary_line_is_kept_whole(self):
        sentence = "word " * 100
        assert read_report(f"# Title\nSummary for the owner: {sentence}")["summary_line"] == sentence.strip()

    def test_capped_line(self):
        assert fixture("security-capped.md")["capped_declared"] is True
        assert fixture("security-rated.md")["capped_declared"] is False
        assert fixture("reviewer-rated.md")["capped_declared"] is None
        assert read_report("# Title\nCapped verdict: YES")["capped_declared"] is True
        assert read_report("# Title\nCapped verdict: maybe")["capped_declared"] is None
        assert read_report("# Title\nCapped verdict:")["capped_declared"] is None

    def test_none_line_means_rated_with_no_findings(self):
        capped = fixture("security-capped.md")
        assert (capped["rated"], capped["findings"], capped["unreadable_rows"]) == (True, [], [])

    def test_an_empty_table_means_rated_with_no_findings(self):
        report = rated("")
        assert (report["rated"], report["findings"], report["unreadable_rows"]) == (True, [], [])

    def test_no_section_means_not_rated(self):
        for name in ("reviewer-old.md", "tester-old.md"):
            report = fixture(name)
            assert (report["rated"], report["findings"], report["unreadable_rows"]) == (
                False, [], [])

    def test_markers_inside_a_code_block_are_ignored(self):
        text = ("# Title\n\n```markdown\nSummary for the owner: an example\nCapped verdict: yes\n"
                f"## Rated findings\n\n{TABLE_HEAD}| 1 | high | An example. | a.py:1 |\n```\n")
        report = read_report(text)
        assert (report["summary_line"], report["capped_declared"], report["rated"]) == (
            None, None, False)

    def test_frontmatter_is_not_read_for_markers(self):
        report = read_report("---\nSummary for the owner: in frontmatter\n---\n\n# Title\n")
        assert report["summary_line"] is None

    def test_the_section_ends_at_the_next_level_two_heading(self):
        text = (f"## Rated findings\n\n{TABLE_HEAD}| 1 | low | Inside. | a.py:1 |\n"
                "### A lower heading stays inside\n| 2 | low | Also inside. | a.py:2 |\n"
                "## Next\n| 3 | high | Outside. | a.py:3 |\n")
        assert [finding["number"] for finding in read_report(text)["findings"]] == ["1", "2"]

    def test_severity_is_read_in_any_letter_case(self):
        assert [finding["severity"] for finding in rated(
            "| 1 | HIGH | One. | a.py:1 |\n| 2 | Low | Two. | a.py:2 |\n")["findings"]] == [
            "high", "low"]

    def test_an_empty_place_is_allowed(self):
        assert rated("| 1 | low | No place given. | |\n")["findings"] == [
            {"number": "1", "severity": "low", "statement": "No place given.", "place": ""}]


class TestUnreadableRows:
    @pytest.mark.parametrize("row", [
        "| 1 | critical | A severity outside the three words. | a.py:1 |",
        "| 1 | high |  | a.py:1 |",
        "| one | high | The number is a word. | a.py:1 |",
        "| 1 | high | Too few cells. |",
        "| 1 | high | Too | many | cells | here |",
        "| | | | |",
        "|",
    ])
    def test_a_row_that_breaks_the_format_is_kept_as_written(self, row):
        report = rated(f"| 1 | low | A good row. | a.py:1 |\n{row}\n")
        assert [finding["statement"] for finding in report["findings"]] == ["A good row."]
        assert report["unreadable_rows"] == [row]

    def test_rows_without_the_header_are_still_read(self):
        report = read_report("## Rated findings\n| 1 | high | No header above. | a.py:1 |\n")
        assert len(report["findings"]) == 1


class TestNothingThrows:
    @pytest.mark.parametrize("text", [
        "", "\n", "---\nverdict: pass\n---\n", "## Rated findings", "## Rated findings\n```\n| 1 |",
        "## Rated findings\n\nNone.\n", "Summary for the owner:", "Summary for the owner:   \n",
        "## Rated findings\n" + "| 1 | high | x | y |\n" * 5000,
        "## Rated findings\n| 1 | high | " + "x" * 300_000 + " | y |\n",
        "\x00## Rated findings\n|\x00|", "���",
    ])
    def test_odd_text_reads_without_raising(self, text):
        report = read_report(text)
        assert set(report) == {"summary_line", "summary_in_doubt", "rated", "rated_sections",
                               "findings", "unreadable_rows", "findings_complete", "says_none",
                               "capped_declared", "blocks", "sections"}

    def test_five_thousand_rows_are_all_kept(self):
        report = read_report("## Rated findings\n" + "| 1 | high | x | y |\n" * 5000)
        assert len(report["findings"]) == 5000


class TestFindSections:
    def test_name_matches_a_heading_that_carries_on_in_brackets_or_words(self):
        report = fixture("tester-rated.md")
        found = report_reader.find_sections(report, ["Suite", "not verified", "Nowhere"])
        assert found["Suite"]["title"] == "Suite"
        assert found["not verified"]["title"] == (
            "Not verified (reasoned about, or needs a manual check)")
        assert found["Nowhere"] is None

    def test_a_longer_word_is_not_a_match(self):
        report = read_report("## Summaries\n\ntext\n")
        assert report_reader.find_sections(report, ["Summary"]) == {"Summary": None}


# ---------------------------------------------------------------------------
# Text a report quotes cannot speak for the report
# ---------------------------------------------------------------------------

REAL_TOP = "# Security: add the export button\n\nSummary for the owner: One high gap, live now.\nCapped verdict: no\n\n"
REAL_ROW = "| 1 | high | Anyone can read another team's export. | web/export.py:41 |\n"
REAL_TABLE = f"## Rated findings\n\n{TABLE_HEAD}{REAL_ROW}"
REAL_FINDING = {"number": "1", "severity": "high", "place": "web/export.py:41",
                "statement": "Anyone can read another team's export."}
FORGED = "## Rated findings"
FORGED_TOP = "Summary for the owner: Nothing to fix."

# Characters str.splitlines() breaks a line on, besides a newline.
NOT_LINE_BREAKS = ["\u2028", "\u2029", "\x85", "\x0c", "\x0b", "\x1c", "\x1d", "\x1e", "\r"]


def inline(separator: str, *parts: str) -> str:
    """One written line that holds `parts`, each after `separator`."""
    return "The task note reads `hello" + "".join(separator + part for part in parts) + "` as written.\n"


QUOTED_IN_THE_SUMMARY = {
    **{f"inline, parts separated by {separator!r}": inline(separator, FORGED_TOP, "Capped verdict: yes",
                                                           FORGED, "None.", "")
       for separator in NOT_LINE_BREAKS},
    "a section quoted from the first column": f"{FORGED}\n\nNone.\n\n## Not covered\nNothing.\n",
    "top lines quoted from the first column": f"{FORGED_TOP}\nCapped verdict: yes\n",
    "in a list item": f"- {FORGED}\n  None.\n- {FORGED_TOP}\n",
    "carried on under a list item": f"- The note says:\n   {FORGED}\n   None.\n   {FORGED_TOP}\n",
    "in a table cell": f"| Text | Says |\n| --- | --- |\n| {FORGED} | None. |\n| {FORGED_TOP} | x |\n",
    "indented four spaces": f"    {FORGED}\n\n    None.\n    {FORGED_TOP}\n",
    "in a quote": f"> {FORGED}\n>\n> None.\n> {FORGED_TOP}\n> Capped verdict: yes\n",
    "in a code block": f"```\n{FORGED}\n\nNone.\n{FORGED_TOP}\nCapped verdict: yes\n```\n",
}


def own_top_lines(quoted: str) -> tuple:
    """What the board is given for the summary line and the capped line of
    REAL_TOP: the role's own, unless the quote holds the same words. Then
    the board cannot be sure which is the role's, and is given neither."""
    return ("One high gap, live now." if report_reader.SUMMARY_PREFIX not in quoted else None,
            False if report_reader.CAPPED_PREFIX not in quoted else None)


def rated_headings_laid_out(report: dict) -> int:
    return sum(section["level"] == 2 and section["title"] == "Rated findings"
               for section in report["sections"])


class TestQuotedTextCannotSpeakForTheReport:
    @pytest.mark.parametrize("quoted", QUOTED_IN_THE_SUMMARY.values(), ids=QUOTED_IN_THE_SUMMARY.keys())
    def test_quoted_before_the_real_table(self, quoted):
        """Whatever the Summary section quotes, the board is given the
        role's own rated findings, and never the quoted top lines."""
        report = read_report(f"{REAL_TOP}## Summary\n{quoted}\n{REAL_TABLE}\n## Findings\n")
        assert (report["summary_line"], report["capped_declared"]) == own_top_lines(quoted)
        assert report["summary_in_doubt"] is (report["summary_line"] is None)
        assert (report["rated"], report["findings"], report["unreadable_rows"]) == (
            True, [REAL_FINDING], [])

    @pytest.mark.parametrize("quoted", QUOTED_IN_THE_SUMMARY.values(), ids=QUOTED_IN_THE_SUMMARY.keys())
    def test_quoted_after_the_real_table(self, quoted):
        report = read_report(f"{REAL_TOP}{REAL_TABLE}\n## Findings\n{quoted}")
        assert (report["summary_line"], report["capped_declared"]) == own_top_lines(quoted)
        assert (report["rated"], report["findings"], report["unreadable_rows"]) == (
            True, [REAL_FINDING], [])

    @pytest.mark.parametrize("quoted", QUOTED_IN_THE_SUMMARY.values(), ids=QUOTED_IN_THE_SUMMARY.keys())
    def test_quoted_top_lines_do_not_stand_in_for_missing_ones(self, quoted):
        report = read_report(f"# Security: add the export button\n\n## Summary\n{quoted}\n{REAL_TABLE}")
        assert (report["summary_line"], report["capped_declared"]) == (None, None)
        assert report["findings"] == [REAL_FINDING]

    @pytest.mark.parametrize("quoted", QUOTED_IN_THE_SUMMARY.values(), ids=QUOTED_IN_THE_SUMMARY.keys())
    def test_the_reader_and_the_layout_agree_on_what_is_a_rated_heading(self, quoted):
        """What the reader takes for the heading is laid out as a heading on
        the page, and nothing else is: one definition of a line, used by both."""
        report = read_report(f"{REAL_TOP}## Summary\n{quoted}\n{REAL_TABLE}")
        assert report["rated_sections"] == rated_headings_laid_out(report)

    def test_a_quoted_row_can_add_a_finding_but_never_take_one_away(self):
        """A second section in the first column cannot be told from the
        role's own, so the rows of both are read."""
        quoted = f"## Rated findings\n\n{TABLE_HEAD}| 7 | low | A quoted row. | a.py:1 |\n\n## Not covered\n"
        for text in (f"{REAL_TOP}## Summary\n{quoted}\n{REAL_TABLE}",
                     f"{REAL_TOP}{REAL_TABLE}\n## Findings\n{quoted}"):
            report = read_report(text)
            assert report["rated_sections"] == 2
            assert REAL_FINDING in report["findings"] and len(report["findings"]) == 2

    def test_the_top_lines_are_read_only_under_the_title(self):
        under_a_section = read_report("# Title\n\n## Summary\nSummary for the owner: In a section.\n"
                                      "Capped verdict: yes\n")
        assert (under_a_section["summary_line"], under_a_section["capped_declared"]) == (None, None)
        no_title = read_report("Summary for the owner: No title above.\nCapped verdict: yes\n")
        assert (no_title["summary_line"], no_title["capped_declared"]) == (None, None)
        with_a_line_between = read_report("# Title\n\nReviewed at commit abc.\n\n"
                                          "Summary for the owner: Still read.\n")
        assert with_a_line_between["summary_line"] == "Still read."

    @pytest.mark.parametrize("text", [
        "# A quoted title\n\nSummary for the owner: Nothing to fix.\nCapped verdict: yes\n\n" + REAL_TOP,
        REAL_TOP + "## Summary\nQuoted:\n\n# A quoted title\n\nSummary for the owner: Nothing to fix.\n"
                   "Capped verdict: yes\n",
        "# Title\n\nSummary for the owner: Nothing to fix.\nSummary for the owner: One high gap.\n"
        "Capped verdict: yes\nCapped verdict: no\n",
    ], ids=["a made-up title above the real one", "a made-up title below it", "two lines under one title"])
    def test_two_answers_are_no_answer(self, text):
        """Two titles, or two summary lines, that disagree: the board cannot
        tell which is the role's own, so it is given neither."""
        report = read_report(text + "\n" + REAL_TABLE)
        assert (report["summary_line"], report["capped_declared"]) == (None, None)
        assert report["findings"] == [REAL_FINDING]

    def test_the_same_line_twice_is_no_answer(self):
        """Even two that agree: one of them is not the role's own line."""
        report = read_report("# Title\n\nSummary for the owner: Same.\n\n## Summary\n\n# Title\n\n"
                             "Summary for the owner: Same.\n")
        assert (report["summary_line"], report["summary_in_doubt"]) == (None, True)

    def test_windows_line_ends_read_the_same(self):
        report = read_report((REAL_TOP + REAL_TABLE).replace("\n", "\r\n"))
        assert report["summary_line"] == "One high gap, live now."
        assert report["capped_declared"] is False
        assert report["findings"] == [REAL_FINDING]


# ---------------------------------------------------------------------------
# What the board may claim: a loose look over the whole file
# ---------------------------------------------------------------------------

CLEAN_NONE = "# Title\n\n## Summary\nFine.\n\n## Rated findings\n\nNone.\n"


class TestWhatTheBoardMayClaim:
    @pytest.mark.parametrize("section", [
        "None", "none.", "**None.**", "- None.", "1. None.", "> None.", "(none)", "_None_",
        "None.\n\nFix now: none\n\nRecord: none", "- None.\n- **Fix now:** none.", "None.\n\nNone.",
        "None.\n\n---", "None.\n\n***", "___\n\nNone.\n\n- - -\n\nRecord: none\n\n>",
    ])
    def test_none_in_other_dress_is_not_no_findings_to_the_board(self, section):
        """The board says "no findings" only on the one line the prompts
        ask for."""
        assert read_report(CLEAN_NONE.replace("None.\n", section + "\n"))["says_none"] is False
        assert read_report(CLEAN_NONE)["says_none"] is True

    @pytest.mark.parametrize("section", [
        "", "n/a", "No findings.", "None of the checks ran, and that is high.",
        "- None of the checks ran, and that is high.", "None.\n\nOne thing though: it is wrong.",
        "Fix now: none\n\nRecord: none", "None.\n\nFix now: 1", "None.\n\nFix now: none of them",
        "None.\n\n> | 1 | high | Hidden. | a.py:1 |",
        "None.\n\n---\n\nOne thing though: it is wrong.", "---", "---\n\nFix now: none",
        "None.\n\n<!-- checked twice -->", "None.\n\nFix now:",
    ], ids=["empty", "n/a", "other words for it", "words that begin with none",
            "the same as a list item", "none and then a finding in prose", "mark lines alone",
            "a mark line that names a finding", "a mark line Relay cannot read",
            "a row inside a quote", "none, a rule, then a finding in prose", "a rule alone",
            "a rule and a mark line", "a comment under none", "a mark line left empty"])
    def test_a_section_that_says_more_or_other_than_none_is_not_no_findings(self, section):
        assert read_report(CLEAN_NONE.replace("None.\n", section + "\n"))["says_none"] is False

    def test_a_plain_report_is_read_with_confidence(self):
        none = read_report(CLEAN_NONE)
        assert (none["says_none"], none["findings_complete"]) == (True, True)
        rows = read_report(f"{REAL_TOP}{REAL_TABLE}")
        assert (rows["says_none"], rows["findings_complete"], rows["summary_in_doubt"]) == (
            False, True, False)
        for name in ("reviewer-rated.md", "tester-rated.md", "security-rated.md", "security-capped.md"):
            assert fixture(name)["findings_complete"] is True, name
        assert fixture("security-capped.md")["says_none"] is True

    @pytest.mark.parametrize("line", [
        "## Rated findings", " ## Rated findings", "## Rated Findings", "## Rated findings ##",
        "## Rated findings:", "##  Rated findings", "### Rated findings", "##Rated findings",
        "> ## Rated findings", "- ## Rated findings", "    ## Rated findings", "**Rated findings**",
        "Rated findings", "RATED FINDINGS", "## Rated-findings", "## Rated findings",
        "```\n## Rated findings\n```", "x ## Rated findings", "x\r## Rated findings",
        "x\x0c## Rated findings",
    ])
    def test_a_second_line_that_could_be_the_heading_takes_the_confidence_away(self, line):
        for text in (f"{CLEAN_NONE}\n## Notes\n{line}\n", f"# Title\n\n{line}\n\n{CLEAN_NONE[9:]}"):
            report = read_report(text)
            assert (report["says_none"], report["findings_complete"]) == (False, False), text

    @pytest.mark.parametrize("line", [
        "The Rated findings section is above.", "See `## Rated findings`.",
        "| 1 | low | The `## Rated findings` heading is quoted. | a.py:1 |", "## Findings", "## Rating",
    ])
    def test_words_inside_a_line_are_not_a_heading(self, line):
        report = read_report(f"{CLEAN_NONE}\n## Notes\n{line}\n")
        assert report["says_none"] is True

    @pytest.mark.parametrize("row", [
        "| 1 | high | Hidden. | a.py:1 |", "|1|HIGH|Hidden.|a.py:1|", "> | 2 | Medium | Hidden. |",
        "    | 3 | low | Hidden. | a.py:1 |", "```\n| 1 | high | Hidden. | a.py:1 |\n```",
        "1 | high | Hidden. | a.py:1", "| **1** | **high** | Hidden. | a.py:1 |", "| 1. | low | Hidden. | |",
        "x\r| 1 | high | Hidden. | a.py:1 |",
    ])
    def test_a_row_the_reader_did_not_take_takes_the_confidence_away(self, row):
        report = read_report(f"{CLEAN_NONE}\n## Notes\n{row}\n")
        assert (report["findings"], report["findings_complete"]) == ([], False)
        with_rows = read_report(f"{REAL_TOP}{REAL_TABLE}\n## Notes\n{row}\n")
        assert (with_rows["findings"], with_rows["findings_complete"]) == ([REAL_FINDING], False)

    def test_other_tables_are_not_rated_rows(self):
        report = read_report(f"{CLEAN_NONE}\n## Checks\n| 1 | Asked | yes |\n| Risk | low |\n| 2 | lower bound | x |\n")
        assert (report["says_none"], report["findings_complete"]) == (True, True)

    @pytest.mark.parametrize("fence", ["```", "~~~", "```python"])
    def test_a_fence_left_open_takes_the_confidence_away(self, fence):
        report = read_report(f"{CLEAN_NONE}\n## Notes\n{fence}\ncode\n")
        assert (report["says_none"], report["findings_complete"]) == (False, False)

    def test_the_title_is_the_first_line_of_the_report(self):
        """A level-1 heading further down is not the title: nothing under it is read."""
        report = read_report("# Security: x\n\n## Summary\n\n# Release notes\n\n"
                             "Summary for the owner: Nothing to fix.\nCapped verdict: yes\n")
        assert (report["summary_line"], report["capped_declared"]) == (None, None)
        assert report["summary_in_doubt"] is True
        after_frontmatter = read_report("---\nverdict: pass\n---\n\n# Title\nSummary for the owner: Read.\n")
        assert after_frontmatter["summary_line"] == "Read."
        in_a_fence = read_report("# Title\n```\nSummary for the owner: In code.\n```\n")
        assert in_a_fence["summary_line"] is None

    @pytest.mark.parametrize("elsewhere", [
        "> Summary for the owner: Quoted.", "```\nSummary for the owner: In code.\n```",
        "The line `summary for the owner:` is missing.", "    Summary for the owner: Indented.",
    ])
    def test_the_same_words_anywhere_else_leave_no_summary_line(self, elsewhere):
        report = read_report(f"{REAL_TOP}## Summary\n{elsewhere}\n")
        assert (report["summary_line"], report["summary_in_doubt"]) == (None, True)
        assert report["capped_declared"] is False

    def test_a_report_with_no_summary_line_is_not_in_doubt(self):
        assert fixture("reviewer-old.md")["summary_in_doubt"] is False

    @pytest.mark.parametrize("text", ["1" * 1_000_000, "1" + "*" * 1_000_000, "1 " * 500_000,
                                      "#" * 1_000_000, "1|" * 500_000],
                             ids=["digits", "stars", "numbers", "hashes", "bars"])
    def test_the_loose_look_does_not_slow_on_a_megabyte_of_near_misses(self, text):
        started = time.monotonic()
        read_report("# Title\n\n## Rated findings\n\n" + text)
        assert time.monotonic() - started < 5
