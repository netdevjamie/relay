---
task_id: add-max-length
phase: builder
step: 4
date: 2026-11-14
verdict: ready-for-review
---

# Build: Cap a slug's length (fix round 1)

## Summary

This round fixes the three findings in 03-reviewer.md that the owner sent back. Nothing else was redone.

1. **Medium (slugify.py:72), fixed.** A title that starts with a hyphen prints its slug again, as it did before `--max-length` existed. `--max-length` (or `--max-length=N`) is now the only option. Every other argument is the title: `-x` → `x`, `--help` → `help`, `-h` → `h`, `--` → an empty line. `main` puts the cap flag and its value first, then `"--"`, then everything else, so argparse never reads a title as an option. The parser no longer has `-h/--help`, because before this task `--help` printed `help`.
2. **Low (test_slugify.py:149), fixed.** A cap value longer than 20 characters is now refused because of its length, on every Python version. Before, the refusal relied on `int()`'s digit limit, which only exists from Python 3.11. The refusal message is now `must be a whole number of at least 1, at most 20 characters long`. It still contains the wording criterion 10 asks for.
3. **Low (README.md:8), fixed.** A new test runs every `$ python3 slugify.py ...` line in the README and checks that it prints the line the README shows below it. A second test fails if the README has fewer than 2 such examples, so a changed prompt can't silently drop them.

A correction to the review: `"--- Draft ---"` (with spaces) already worked at ded286d. argparse treats any argument that contains a space as positional. The failing cases were hyphen titles without spaces, such as `-x`, `--help` and `--Draft--`.

## Branch and commits

Branch `add-max-length`, on top of ded286d:
- `45b6298` Add failing tests: hyphen-leading titles, long caps, README examples (tests only, seen failing)
- `0aeb38f` Treat every argument but --max-length as the title; refuse caps over 20 digits
- `8411999` Say characters, not digits, in the cap-length refusal (from the own-round check)

## Files changed

| File | Change | Why |
| --- | --- | --- |
| slugify.py | New `separate_titles(argv)`. `add_help=False`. `CAP_FLAG` constant. `LONGEST_CAP_TEXT = 20` check in `whole_number_of_at_least_one`. New `CAP_RULE` wording. Removed the parser `description` and the argument `help` text, which nothing can print once help is gone. | Findings 1 and 2 |
| test_slugify.py | 20 new test cases (49 → 69), with no lines removed | Findings 1, 2 and 3 |

README.md is unchanged.

## Tests

All tests are in test_slugify.py.

**Seen to fail before the fix existed.** Test-only commit `45b6298`, run in the worktree before the fix was written, with `python3 -m pytest -q -p no:cacheprovider` → `11 failed, 58 passed`:
- `test_cli_prints_the_slug_of_a_title_starting_with_a_hyphen` rows `-x`, `--help`, `-h`, `--`, `--max`: `argparse.ArgumentError: the following arguments are required: "Some Title"` / `SystemExit: 2`. Row `--help` failed with `SystemExit: 0` (it printed help).
- `test_cli_caps_a_title_starting_with_a_hyphen[cap-first|cap-last|cap-with-equals]` (title `-draft-notes`, cap 5 → `draft`): `SystemExit: 2`.
- `test_script_prints_the_slug_of_a_title_starting_with_a_hyphen` (`--Draft--` → `draft`): `assert 2 == 0` (returncode).
- `test_cli_refuses_two_titles_even_when_they_start_with_a_hyphen[separator-and-title]` (`-- -x` must exit 2, as it did before the task): `Failed: DID NOT RAISE <class 'SystemExit'>`.
- `test_cli_refuses_a_cap_written_with_more_than_twenty_digits` (21 nines → exit 2 naming the 20-character rule): `Failed: DID NOT RAISE <class 'SystemExit'>`.

**Seen to fail after the fix, in a clone.** The script is `scratchpad/relay-add-max-length-build/seen-to-fail.sh <worktree> 0aeb38f`. It makes its own `mktemp -d` clone, checks the clone's top level before each break, undoes each break and confirms the clone is clean, then deletes the clone (`clone deleted: True`). Each break was run with `python3 -m pytest -q -p no:cacheprovider -k <tests> test_slugify.py`:
- README cap example changed to print `the-quick-brown` → `FAILED test_readme_example_prints_what_the_readme_shows[arguments1-the-quick-brown]` (`assert 'the-quick\n' == 'the-quick-brown\n'`).
- README prompt changed to `$ python slugify.py` → `FAILED test_readme_has_examples_to_check` (`assert 1 >= 2`).
- `separate_titles` keeps only the first title → `FAILED ...two_titles...[two-hyphen-titles]`, `[separator-and-title]` and `[hyphen-titles-around-cap]` (`DID NOT RAISE`).
- `LONGEST_CAP_TEXT = 19` → `FAILED test_cli_accepts_a_cap_written_with_twenty_digits` (`SystemExit: 2`).
- Titles not placed after `--` → the hyphen tests fail with `the following arguments are required: "Some Title"`.
- After `8411999` changed the message, a second clone run (`seen-to-fail-length.sh`, at `8411999`) set `LONGEST_CAP_TEXT = 5000` → `FAILED test_cli_refuses_a_cap_written_with_more_than_twenty_digits` (`DID NOT RAISE`). Clone clean, then deleted.

**Not seen to fail (guard rows).** The `--- Draft ---`, `-`, `-3 Tips` and `--help`-free rows of `test_cli_prints_the_slug_of_a_title_starting_with_a_hyphen` already passed at ded286d. argparse treats spaces, a lone `-` and negative-number-like text as positional. These rows only guard against the new separation breaking those titles, and they fail only if `separate_titles` mangles titles. I did not break them separately.

## Existing tests changed or removed

None. `git diff ded286d..HEAD -- test_slugify.py` has 0 removed lines. The `"9" * 5000` row of `test_cli_refuses_a_bad_cap` is unchanged. It now passes because of the 20-character check on every version, not only on 3.11+. Its assertion `"must be a whole number of at least 1" in error` still holds.

## Input readers

- **Command-line argv (changed: `separate_titles` and the cap type).** Inputs tried:
  - Hyphen titles: `-x`, `-h`, `--help`, `--`, `-`, `--max`, `--Draft--`, `-draft-notes`, `--- Draft ---`, `-3 Tips`.
  - Two titles: `-x -y`, `-- -x`, and `-x --max-length 5 -y`, all exit 2.
  - Cap in each position: before the title, after it, and as `=N`.
  - A cap that is far too long: 21 nines and 5000 nines, refused with exit 2. 20 nines is accepted.
  - Already covered by earlier tests: missing cap value, cap alone, no arguments, abbreviated and short flags, `0`, `-3`, `abc`, `""`, `1.5`, `12abc`, a repeated cap.
  
  By hand on 3.12, 3.13 and 3.14: `--max-length "+" + 20 nines` exits 2 with the 20-character message, and `-- -x` exits 2 with `unrecognized arguments: -x`.
- **README.md, read by the new test** (test code, not product code). The test only reads the project's own README. If the prompt format changes, the count test fails rather than the check passing silently.

## Known-flaw rows

No row touched. The repo has no known-flaws document.

## Commands run

- `python3 --version` → `Python 3.12.8`
- `python3 -m py_compile slugify.py test_slugify.py` → `compile-ok`
- Profile test command at HEAD `8411999eb22f`: `python3 -m pytest -q --junitxml=test-results/junit.xml` → `69 passed in 0.22s`, with no warnings or errors.
- No build or lint commands are defined in `.relay/profile.yaml`.
- By hand, with `python3`, `/opt/homebrew/bin/python3.14` (3.14.7) and `~/.pyenv/shims/python3.13` (3.13.15), using `-I slugify.py`: `-x`→`x`, `--help`→`help`, `--`→empty line, `-h`→`h`, `--Draft--`→`draft`, `--max-length 5 -draft-notes`→`draft`, `-- -x`→exit 2, a 23-digit cap→exit 2. The output was the same on all three versions. 3.13 and 3.14 have no pytest, so the suite itself ran only on 3.12.
- `git status --short` → clean (test-results/ is ignored).

## Own-round check

- **Sibling:** I checked other argv shapes next to the hole. `--max-length -x` and `--max-length --` still exit 2 as a missing value. `--max-length=foo` is read as the option and refused. `-- -x` exits 2 as two titles, as on main. No new hole found. One known limit: a title that is exactly `--max-length` or starts with `--max-length=` can't be passed. That is inherent to having the flag (see Out-of-scope).
- **Bigger claim:** found and fixed. The message said "at most 20 digits", but the check counts characters, so `+` and spaces count too. Commit `8411999` changed it to "at most 20 characters long". The test names still say "twenty_digits". That is accurate, because their inputs are all digits (21 and 20 nines).
- **Test that cannot fail:** every new test was broken in a clone or failed before the fix, except the guard rows listed under Tests, which are named as such.
- **Hand-kept list:** the README examples were the hand-kept list (finding 3), and they are now tied to a test. `CAP_FLAG` replaces the repeated flag string in slugify.py.
- **Second look at `8411999`:** it changed only the message and the matching assertion. The clone break with `LONGEST_CAP_TEXT = 5000` re-proved the refusal test at that commit. Its commit message says only what changed. Nothing further.

## Deviations from the plan

No Architect plan exists (feature task). Against the brief, the Designer's artifact and the review:
- `-h/--help` were removed (`add_help=False`). Per the owner's note, any title starting with a hyphen must print a slug as before, and before this task `--help` printed `help`. The parser `description` and the argument `help` text went with them, since nothing can print them.
- The 20-character limit on the cap value is a new refusal. I chose it to make finding 2's behaviour the same on every version. A refusal is the behaviour the code already gave on 3.11+.

## Not verified

- Python 3.10 and 3.11 are not installed here (only 3.9, 3.12, 3.13 and 3.14). I did not run finding 2's fix on 3.10. That it holds there is reasoned: the length check runs before `int()`. The full suite ran only on 3.12. 3.13 and 3.14 were checked by hand for the hyphen and cap cases only.

## Out-of-scope observations

- A title that is literally `--max-length`, or starts with `--max-length=`, is always read as the flag. There is no escape. Before this task, both printed a slug. This seems unlikely to matter. If it does, a later task could add an escape.
- `-- -x` now exits 2 (two titles), matching main. The review noted that ded286d printed `x` for it. I treated `--` as just another title, to match the "as it did before" rule.

## Notes for the Reviewer

- The fix is `separate_titles` in slugify.py. It is the only place argv is reordered. The value after `--max-length` is passed through untouched, so argparse still validates it.
- Relay's recorded run should be at `8411999` with 69 tests and 0 skipped.
- My scratch scripts are in `scratchpad/relay-add-max-length-build/`. The first edit script was written to `/tmp` by mistake and then moved there. Nothing was left in the repo.
