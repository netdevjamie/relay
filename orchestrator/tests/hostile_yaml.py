"""YAML a branch or a role can write to stall or break a reader of it, for
the tests of every place Relay reads YAML it did not write."""

import pytest
import yaml

# A 535-byte merge-key bomb: eight levels, each merging ten copies of the
# one before. YAML's own loader takes tens of seconds and gigabytes of
# memory to build it.
SECURITYS_MERGE_KEYS = "m0: &m0 {k: 1}\n" + "".join(
    f"m{level}: &m{level}\n  <<: [" + ", ".join([f"*m{level - 1}"] * 10) + "]\n"
    for level in range(1, 9))

# Four values YAML's own loader raises ValueError or AttributeError on, not
# one of its own errors.
BAD_VALUES = ["released: 2026-99-99\n", "count: " + "1" * 5_000 + "\n",
              "ratio: !!float 'abc'\n", "when: !!timestamp 'x'\n"]
BAD_VALUE_NAMES = ["a date that does not exist", "an integer of 5,000 digits",
                   "!!float abc", "!!timestamp x"]


@pytest.fixture
def nothing_composed(monkeypatch):
    """Fails the test if YAML's loader builds a node of any text: the text
    must be refused, or read, from its parser's events alone."""
    def compose_node(*arguments):
        raise AssertionError("YAML's loader built a node")
    monkeypatch.setattr(yaml.composer.Composer, "compose_node", compose_node)
