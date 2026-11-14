import io
import unittest
from contextlib import redirect_stdout

from slugify import main, slugify


class SlugifyTest(unittest.TestCase):
    def test_words_are_lower_cased_and_joined_by_hyphens(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_accents_are_dropped_not_the_letters(self):
        self.assertEqual(slugify("Crème Brûlée"), "creme-brulee")

    def test_runs_of_punctuation_become_one_hyphen(self):
        self.assertEqual(slugify("  rock -- & -- roll  "), "rock-roll")

    def test_nothing_sluggable_gives_an_empty_slug(self):
        self.assertEqual(slugify("!!!"), "")

    def test_cli_prints_the_slug(self):
        printed = io.StringIO()
        with redirect_stdout(printed):
            self.assertEqual(main(["Ship It"]), 0)
        self.assertEqual(printed.getvalue(), "ship-it\n")


if __name__ == "__main__":
    unittest.main()
