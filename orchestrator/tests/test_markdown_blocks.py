"""markdown_blocks sorts a report's lines into blocks for display. It must
lay out the common shapes, keep every word, treat markup as text, and
never raise, whatever an agent wrote.
"""

import json
import time
from pathlib import Path

import pytest

import markdown_blocks
from markdown_blocks import to_blocks, to_spans

REPORTS = Path(__file__).resolve().parent / "fixtures" / "reports"


def kinds(text: str) -> list[str]:
    return [block["kind"] for block in to_blocks(text)]


def all_text(blocks: list[dict]) -> str:
    """Every piece of text the blocks carry, joined, for "is it shown" checks."""
    return json.dumps(blocks, ensure_ascii=False)


def words_shown(blocks: list[dict]) -> str:
    parts = []
    for block in blocks:
        if block["kind"] == "code":
            parts += [block["info"], block["text"]]
        elif block["kind"] == "list":
            for item in block["items"]:
                parts += [item["marker"] or "", markdown_blocks.spans_text(item["spans"])]
        elif block["kind"] == "table":
            for row in [block["header"], *block["rows"]]:
                parts += [markdown_blocks.spans_text(cell) for cell in row]
        elif "spans" in block:
            parts.append(markdown_blocks.spans_text(block["spans"]))
    return " ".join(parts)


class TestBlocks:
    def test_each_kind_is_recognised(self):
        text = ("# Title\n\nA paragraph\nof two lines.\n\n- one\n- two\n\n"
                "| a | b |\n| --- | --- |\n| 1 | 2 |\n\n```\ncode\n```\n\n> quoted\n\n---\n")
        assert kinds(text) == ["heading", "paragraph", "list", "table", "code", "quote", "rule"]

    def test_heading_level_and_text(self):
        assert to_blocks("### Not verified (reasoned)") == [
            {"kind": "heading", "level": 3,
             "spans": [{"kind": "text", "text": "Not verified (reasoned)"}]}]

    def test_paragraph_keeps_its_line_breaks(self):
        block = to_blocks("Summary for the owner: fine.\nCapped verdict: no")[0]
        assert block["spans"] == [
            {"kind": "text", "text": "Summary for the owner: fine.\nCapped verdict: no"}]

    def test_frontmatter_is_one_leading_code_block(self):
        blocks = to_blocks("---\nphase: reviewer\nverdict: approved\n---\n\n# Review\n")
        assert blocks[0] == {"kind": "code", "info": "",
                             "text": "---\nphase: reviewer\nverdict: approved\n---"}
        assert blocks[1]["kind"] == "heading"

    def test_table_header_rows_and_cells(self):
        table = to_blocks("| # | Severity |\n| --- | --- |\n| 1 | high |\n")[0]
        assert [markdown_blocks.spans_text(cell) for cell in table["header"]] == ["#", "Severity"]
        assert [[markdown_blocks.spans_text(cell) for cell in row] for row in table["rows"]] == [
            ["1", "high"]]

    def test_table_without_a_separator_has_no_header(self):
        table = to_blocks("| Criterion | Result |\n| one | pass |\n")[0]
        assert table["header"] == []
        assert len(table["rows"]) == 2

    def test_table_rows_with_too_many_or_too_few_cells_are_kept(self):
        table = to_blocks("| a | b |\n| --- | --- |\n| only |\n| 1 | 2 | 3 | 4 |\n")[0]
        assert [len(row) for row in table["rows"]] == [1, 4]
        assert "only" in all_text(table) and '"4"' in all_text(table)

    def test_an_escaped_bar_stays_inside_its_cell(self):
        assert markdown_blocks.split_row(r"| a \| b | c |") == ["a | b", "c"]

    def test_list_items_keep_marker_and_depth(self):
        block = to_blocks("3. third\n4. fourth\n  - nested\n     carried on\n")[0]
        assert block["ordered"] is True
        assert [(item["marker"], item["depth"]) for item in block["items"]] == [
            ("3.", 0), ("4.", 0), (None, 1)]
        assert block["items"][2]["spans"][0]["text"] == "nested\ncarried on"

    def test_code_block_text_is_as_written(self):
        block = to_blocks("```python\nif a < b:\n    **not bold**\n```\n")[0]
        assert block == {"kind": "code", "info": "python", "text": "if a < b:\n    **not bold**"}

    def test_a_fence_inside_a_longer_fence_is_text(self):
        blocks = to_blocks("````\n```\ninner\n```\n````\nafter\n")
        assert blocks[0]["text"] == "```\ninner\n```"
        assert blocks[1]["kind"] == "paragraph"

    def test_quote_lines_are_one_block(self):
        block = to_blocks("> one\n> two\n")[0]
        assert block == {"kind": "quote", "spans": [{"kind": "text", "text": "one\ntwo"}]}


class TestSpans:
    def test_code_and_bold(self):
        assert to_spans("Run `make test` at **once**, please") == [
            {"kind": "text", "text": "Run "}, {"kind": "code", "text": "make test"},
            {"kind": "text", "text": " at "}, {"kind": "strong", "text": "once"},
            {"kind": "text", "text": ", please"}]

    def test_bold_inside_code_is_code(self):
        assert to_spans("`a ** b ** c`") == [{"kind": "code", "text": "a ** b ** c"}]

    def test_double_backticks_hold_a_single_one(self):
        assert to_spans("``a ` b``") == [{"kind": "code", "text": "a ` b"}]

    @pytest.mark.parametrize("text", ["a ` b", "a ** b", "****", "``", "*", "`` ` ```", ""])
    def test_unpaired_marks_stay_text(self, text):
        assert markdown_blocks.spans_text(to_spans(text)) == text
        assert all(span["kind"] == "text" for span in to_spans(text))


class TestMarkupStaysText:
    def test_markup_stays_text(self):
        """C26: a script tag, an image with a handler and a javascript link
        come through as the characters written, in plain text spans. No
        block or span kind exists that a page could turn into a link, an
        image or raw markup."""
        blocks = to_blocks((REPORTS / "hostile.md").read_text())
        shown = words_shown(blocks)
        for written in ("<script>alert(1)</script>", "<img src=x onerror=alert(1)>",
                        "[click](javascript:alert(1))", '<div onclick="alert(1)">a raw block</div>',
                        "![picture](javascript:alert(1))",
                        '<iframe src="javascript:alert(1)"></iframe>'):
            assert written in shown

        def span_kinds(node):
            if isinstance(node, dict):
                if set(node) == {"kind", "text"}:
                    yield node["kind"]
                for value in node.values():
                    yield from span_kinds(value)
            elif isinstance(node, list):
                for value in node:
                    yield from span_kinds(value)
        assert {block["kind"] for block in blocks} <= {
            "heading", "paragraph", "list", "table", "code", "quote", "rule"}
        assert set(span_kinds(blocks)) <= {"text", "code", "strong"}

    def test_a_link_is_one_plain_span(self):
        assert to_spans("[click](javascript:alert(1))") == [
            {"kind": "text", "text": "[click](javascript:alert(1))"}]


class TestNothingThrowsAndNothingIsDropped:
    @pytest.mark.parametrize("name", sorted(path.name for path in REPORTS.glob("*.md")))
    def test_every_word_of_a_fixture_is_shown(self, name):
        text = (REPORTS / name).read_text()
        shown = words_shown(to_blocks(text))
        dropped = [word for word in text.replace("|", " ").replace("**", " ").replace("`", " ").split()
                   if word.strip("#>-:") and word not in shown]
        assert dropped == []

    def test_empty_text(self):
        assert to_blocks("") == []
        assert to_blocks("\n\n   \n") == []
        assert markdown_blocks.lines_outside_code("") == []
        assert markdown_blocks.sections([]) == []

    def test_only_frontmatter(self):
        assert kinds("---\nverdict: pass\n---\n") == ["code"]
        assert markdown_blocks.lines_outside_code("---\nverdict: pass\n---\n") == []

    def test_frontmatter_nobody_closed_is_still_shown(self):
        blocks = to_blocks("---\nverdict: pass\n\n# Title\n")
        assert kinds("---\nverdict: pass\n\n# Title\n") == ["rule", "paragraph", "heading"]
        assert "verdict: pass" in all_text(blocks)

    def test_unclosed_code_fence_runs_to_the_end(self):
        blocks = to_blocks("before\n\n```\ncode line\n## not a heading\n")
        assert kinds("before\n\n```\ncode line\n## not a heading\n") == ["paragraph", "code"]
        assert blocks[1]["text"] == "code line\n## not a heading"
        assert markdown_blocks.lines_outside_code("before\n\n```\ncode line\n") == ["before", ""]

    def test_a_fence_as_the_last_line(self):
        assert to_blocks("```") == [{"kind": "code", "info": "", "text": ""}]

    def test_very_long_lines_are_read_quickly_and_whole(self):
        long_word = "x" * 200_000
        marks = "` ** " * 40_000 + "`` " * 3 + "```` "
        started = time.monotonic()
        blocks = to_blocks(f"{long_word}\n\n{marks}\n\n| {long_word} | {long_word} |\n")
        assert time.monotonic() - started < 5
        assert markdown_blocks.spans_text(blocks[0]["spans"]) == long_word
        assert len(blocks[2]["rows"][0]) == 2

    @pytest.mark.parametrize("text", [
        "|", "||", "| |", "|\n|\n", "> ", ">", "-", "- ", "1.", "#", "# ", "######## eight",
        "\t- tabbed", "    indented", "\x00\x01\x1b[31m", "\ufffd\ufffd", "\r\n\r\n# title\r\n",
        "---", "---\n---", "---\n---\n---", "~~~\n", "```\n```\n```", "| a |\n| --- |",
        "a\x0cb", "\u2028", "- a\n\n\n- b", "* * *", "1) one\n2) two",
    ])
    def test_odd_input_never_raises(self, text):
        blocks = to_blocks(text)
        json.dumps(blocks)
        markdown_blocks.sections(blocks)
        markdown_blocks.lines_outside_code(text)


class TestSections:
    def test_a_section_runs_to_the_next_heading_of_its_level_or_higher(self):
        blocks = to_blocks("# T\n\n## A\n\none\n\n### A1\n\ntwo\n\n## B\n\nthree\n")
        found = {section["title"]: (section["first"], section["last"])
                 for section in markdown_blocks.sections(blocks)}
        assert found["T"] == (0, len(blocks) - 1)
        assert found["A"] == (1, 4)
        assert found["A1"] == (3, 4)
        assert found["B"] == (5, 6)

    def test_lines_in_a_code_block_are_left_out(self):
        text = "---\nphase: x\n---\nreal\n```\nSummary for the owner: an example\n```\nalso real\n"
        assert markdown_blocks.lines_outside_code(text) == ["real", "also real"]


class TestOneDefinitionOfALine:
    def test_a_line_ends_at_a_newline_and_nowhere_else(self):
        assert markdown_blocks.split_lines("a\nb\r\nc\n") == ["a", "b", "c"]
        assert markdown_blocks.split_lines("a\n\nb") == ["a", "", "b"]
        assert markdown_blocks.split_lines("") == []
        for inside in ("\u2028", "\u2029", "\x85", "\x0c", "\x0b", "\x1c", "\x1d", "\x1e", "\r"):
            assert markdown_blocks.split_lines(f"a{inside}b\n") == [f"a{inside}b"], repr(inside)

    @pytest.mark.parametrize("inside", ["\u2028", "\u2029", "\x85", "\x0c", "\x0b", "\x1c", "\r"])
    def test_a_separator_inside_a_line_starts_no_block(self, inside):
        """Seen before the fix: a Unicode line separator inside one written
        line began a new line, so quoted words became a heading."""
        blocks = to_blocks(f"A note that reads `x{inside}## A heading{inside}| a | b |{inside}` here.\n")
        assert [block["kind"] for block in blocks] == ["paragraph"]
        assert markdown_blocks.lines_outside_code(f"a{inside}## A heading\n") == [f"a{inside}## A heading"]


class TestFencesLeftOpen:
    @pytest.mark.parametrize("text, left_open", [
        ("text\n", False), ("```\ncode\n```\n", False), ("~~~\ncode\n~~~\ntext\n", False),
        ("```\ncode\n", True), ("text\n~~~\n", True), ("```\ncode\n~~~\n", True),
        ("```\na\n```\nb\n```\n", True), ("````\n```\n````\n", False),
        ("---\nverdict: pass\n---\n```\n", True), ("", False),
    ])
    def test_a_fence_nobody_closed_is_seen(self, text, left_open):
        assert markdown_blocks.has_unclosed_fence(text) is left_open

    def test_body_lines_leave_out_the_frontmatter_only(self):
        assert markdown_blocks.body_lines("---\nverdict: pass\n---\n# Title\n```\ncode\n") == [
            "# Title", "```", "code"]
        assert markdown_blocks.body_lines("# Title\n") == ["# Title"]

    def test_what_is_a_fence_line(self):
        assert [markdown_blocks.is_fence(line) for line in ("```", "   ~~~python", "    ```", "``")] == [
            True, True, False, False]
