"""Untrusted text made safe to print on one line.

Report text, file names and git output are written by agents and by
branches. Relay prints none of it without passing it through printable().
This module imports nothing of Relay's, so any module can use it.
"""

# A line of a card that is a report's own words is cut at this many characters.
QUOTED_LIMIT = 160


def printable(text, limit: int = QUOTED_LIMIT) -> str:
    """The text on one line, without any character a terminal would act on
    or not show, cut at `limit` characters. Report text and file names are
    written by agents and by branches, so nothing of theirs is printed
    without passing through here."""
    one_line = " ".join(str(text).split())
    return "".join(character for character in one_line if character.isprintable())[:limit]
