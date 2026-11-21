"""The reader of YAML Relay did not write: it gives what YAML's safe loader
gives for every text it reads, and refuses, in time and without building
anything, what a small text can make that loader do."""

import datetime
import math
import re
import time

import pytest
import yaml

import bounded_yaml
from tests.hostile_yaml import (  # noqa: F401  (nothing_composed is a fixture)
    BAD_VALUE_NAMES, BAD_VALUES, SECURITYS_MERGE_KEYS, nothing_composed,
)

# Every kind of value and layout a profile or a frontmatter can be written in.
TEXTS_READ = {
    "a frontmatter": "task_id: t\nphase: builder\nstep: 32\ndate: 2026-10-14\nverdict: ready-for-review\n",
    "a profile": ("schema_version: 1\nplatforms: [cli, ios]\ncommands:\n  test: pytest -q\n"
                  "deploy_targets:\n  - name: x\n    command: ./go\n    smoke: 'yes'\n"),
    "block and flow": "a:\n  - 1\n  - [2, 3]\n  - {b: c}\nd: {e: [f, g]}\n",
    "quoted": "a: 'single ''quoted'''\nb: \"double \\t\\u00e9 \\x41\"\nc: \"line\\nbreak\"\n",
    "integers": "a: 0x1F\nb: 0o17\nc: 017\nd: 0b101\ne: -1_000\nf: 1:20:30\ng: +7\n",
    "floats": "a: 1.5\nb: -.5e+3\nc: .inf\nd: -.Inf\ne: 1:30.5\nf: 1_000.0\n",
    "not a number": "a: .nan\n",
    "booleans and nulls": "a: yes\nb: No\nc: on\nd: OFF\ne: true\nf: ~\ng: null\nh:\n",
    "dates and times": ("a: 2026-10-14\nb: 2026-10-14T10:20:30Z\nc: 2026-10-14 10:20:30.5 +02:00\n"
                        "d: 2026-1-4T1:2:3\n"),
    "keys of every kind": "1: one\nyes: true key\n~: null key\n2026-10-14: date key\n=: equals\n'<<': text\n",
    "a key written twice": "a: 1\na: 2\n",
    "block scalars": "a: |\n  kept\n  lines\nb: >-\n  folded\n  lines\nc: |+\n  kept end\n\n",
    "multi-line plain": "a: one\n  two\n\n  three\n",
    "comments and markers": "%YAML 1.1\n---\n# a comment\na: b # trailing\n...\n",
    "a byte-order mark": "\ufeffa: b\n",
    "line ends": "a: b\r\nc: |\r\n  d\r\n  e\r\n",
    "explicit keys": "? a\n: b\n? c\n",
    "a list at the top": "- a\n- b\n",
    "text at the top": "just text\n",
    "a number at the top": "42\n",
    "empty": "",
    "only a start": "---\n",
    "nested at the limit": "a: " + "[" * 9 + "b" + "]" * 9 + "\n",
}
TEXTS_YAML_REFUSES = {
    "not closed": "a: [b\n",
    "a tab": "a:\tb\n\tc: d\n",
    "a value that is =": "a: =\n",
}


def refused(reason: str):
    """Expects the text to be refused, for this reason."""
    return pytest.raises(bounded_yaml.NotRead, match=f"^{re.escape(reason)}$")


def same(first, second) -> bool:
    """Equal in value and in type all the way down; NaN is NaN."""
    if type(first) is not type(second):
        return False
    if isinstance(first, dict):
        return (list(first) == list(second)
                and all(same(first[key], second[key]) for key in first))
    if isinstance(first, list):
        return len(first) == len(second) and all(map(same, first, second))
    if isinstance(first, float) and math.isnan(first):
        return math.isnan(second)
    return first == second


class TestWhatIsRead:
    @pytest.mark.parametrize("text", TEXTS_READ.values(), ids=TEXTS_READ.keys())
    def test_a_text_is_read_as_yamls_safe_loader_reads_it(self, text):
        assert same(bounded_yaml.load(text), yaml.safe_load(text))

    def test_the_types_a_frontmatter_holds_are_kept(self):
        """Relay routes on the step as a number and dates the step by its date."""
        written = bounded_yaml.frontmatter("---\n" + TEXTS_READ["a frontmatter"] + "---\n\n# Body\n")
        assert written == {"task_id": "t", "phase": "builder", "step": 32,
                           "date": datetime.date(2026, 10, 14), "verdict": "ready-for-review"}
        assert type(written["step"]) is int

    @pytest.mark.parametrize("text", TEXTS_YAML_REFUSES.values(), ids=TEXTS_YAML_REFUSES.keys())
    def test_a_text_yamls_safe_loader_refuses_is_refused_with_yamls_error(self, text):
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load(text)
        with pytest.raises(yaml.YAMLError):
            bounded_yaml.load(text)


class TestWhatIsNotRead:
    def test_securitys_merge_key_file_is_refused_before_anything_is_built(self, nothing_composed):
        started = time.monotonic()
        with refused(bounded_yaml.HOLDS_A_REFERENCE):
            bounded_yaml.load(SECURITYS_MERGE_KEYS)
        assert time.monotonic() - started < 1

    @pytest.mark.parametrize("text", ["a: &here b\n", "a: b\nc: *a\n", "a:\n  <<: {b: c}\n", "a: [<<]\n"],
                             ids=["an anchor", "an alias", "a merge key", "an unquoted << as a value"])
    def test_an_anchor_alias_or_merge_key_is_not_read(self, text, nothing_composed):
        with refused(bounded_yaml.HOLDS_A_REFERENCE):
            bounded_yaml.load(text)

    @pytest.mark.parametrize("text, reason", zip(BAD_VALUES, [
        bounded_yaml.CANNOT_BE_BUILT, bounded_yaml.CANNOT_BE_BUILT,
        bounded_yaml.HOLDS_A_TAG, bounded_yaml.HOLDS_A_TAG]), ids=BAD_VALUE_NAMES)
    def test_a_value_yamls_loader_raised_on_is_not_read(self, text, reason, nothing_composed):
        """YAML's loader raised ValueError or
        AttributeError on these, not one of its own errors."""
        with refused(reason):
            bounded_yaml.load(text)

    @pytest.mark.parametrize("text", ["a: !!str b\n", "a: ! b\n", "a: !!python/object/apply:os.system ['true']\n"],
                             ids=["!!str", "the tag !", "a Python object"])
    def test_a_tag_is_not_read(self, text, nothing_composed):
        with refused(bounded_yaml.HOLDS_A_TAG):
            bounded_yaml.load(text)

    @pytest.mark.parametrize("over, over_the_limit", [(0, False), (1, True)], ids=["as many as are read", "one more"])
    def test_more_values_than_are_read_is_not_read(self, over, over_the_limit):
        """The top-level list is one of the values."""
        text = "[" + ", ".join(["a"] * (bounded_yaml.MOST_NODES - 1 + over)) + "]"
        if over_the_limit:
            with refused(bounded_yaml.TOO_MANY_NODES):
                bounded_yaml.load(text)
        else:
            assert len(bounded_yaml.load(text)) == bounded_yaml.MOST_NODES - 1

    @pytest.mark.parametrize("over, over_the_limit", [(0, False), (1, True)], ids=["as deep as is read", "one deeper"])
    def test_nesting_deeper_than_is_read_is_not_read(self, over, over_the_limit):
        depth = bounded_yaml.MOST_DEPTH + over
        text = "[" * depth + "]" * depth
        if over_the_limit:
            with refused(bounded_yaml.TOO_DEEP):
                bounded_yaml.load(text)
        else:
            assert bounded_yaml.load(text) is not None

    def test_nesting_30_000_deep_is_refused_in_a_second(self):
        started = time.monotonic()
        with refused(bounded_yaml.TOO_DEEP):
            bounded_yaml.load("a: " + "[" * 30_000 + "]" * 30_000 + "\n")
        assert time.monotonic() - started < 1

    def test_a_second_document_is_not_read(self):
        with refused(bounded_yaml.MORE_THAN_ONE_DOCUMENT):
            bounded_yaml.load("a: b\n---\nc: d\n")

    def test_a_list_or_mapping_as_a_key_is_not_read(self):
        with refused(bounded_yaml.UNHASHABLE_KEY):
            bounded_yaml.load("? [a, b]\n: c\n")

    def test_any_other_error_in_reading_is_not_read_and_never_raised_as_it_is(self, monkeypatch):
        def parse(text, **options):
            raise MemoryError("made up for the test")
            yield
        monkeypatch.setattr(bounded_yaml.yaml, "parse", parse)
        with refused(bounded_yaml.UNREADABLE):
            bounded_yaml.load("a: b\n")


class TestFrontmatter:
    def test_a_text_with_no_frontmatter_has_none(self):
        assert bounded_yaml.frontmatter("# Just a heading\n") == {}
        assert bounded_yaml.frontmatter("---\n\n---\n") == {}

    def test_a_frontmatter_longer_than_is_read_is_never_parsed(self, monkeypatch):
        parsed = []
        monkeypatch.setattr(bounded_yaml.yaml, "parse", lambda text, **options: parsed.append(text))
        written = "a: " + "x" * bounded_yaml.MOST_FRONTMATTER_CHARACTERS
        with refused(bounded_yaml.FRONTMATTER_TOO_LONG):
            bounded_yaml.frontmatter(f"---\n{written}\n---\n")
        assert parsed == []

    def test_a_frontmatter_that_is_not_yaml_is_not_read_and_never_raises_yamls_error(self):
        with pytest.raises(bounded_yaml.NotRead, match=r"^is not valid YAML \(\w+Error\)$"):
            bounded_yaml.frontmatter("---\nphase: [unclosed\n---\n")

    def test_a_frontmatter_that_is_not_a_mapping_is_not_read(self):
        with refused("is a list, not a mapping"):
            bounded_yaml.frontmatter("---\n- a\n---\n")

    def test_securitys_merge_key_file_as_a_frontmatter_is_refused(self, nothing_composed):
        with refused(bounded_yaml.HOLDS_A_REFERENCE):
            bounded_yaml.frontmatter(f"---\n{SECURITYS_MERGE_KEYS}verdict: pass\n---\n")
