---
task_id: add-max-length
phase: builder
step: 2
date: 2026-11-14
verdict: ready-for-review
---

# Build: Cap a slug's length

## Summary

`slugify(text, max_length=None)` now takes an optional cap, and the command line takes `--max-length N`. With no cap the output is the same as before. With a cap the slug is cut between words when a hyphen is within reach. It never ends with a hyphen. A first word longer than the cap is cut to the cap. Following the owner's note, a short word followed by a long one keeps whole words only, so `a-supercalifragilistic` at 10 gives `a`. A cap below 1 raises `ValueError("max_length must be at least 1, got N")` in code. On the command line, a bad cap exits 2 with `argument --max-length: must be a whole number of at least 1`. The command line now uses argparse, as the owner's note asked, and the README has one `--max-length` example, also from the owner's note.

This is a `feature` task, so there is no Architect plan. I built from the Designer's brief (01-designer.md) and the owner's notes.

## Branch and commits

Branch `add-max-length` (worktree `~/code/slugify`), based on `main` 377630e.

- `ded286d` Add max_length cap to slugify and --max-length to the command line

## Files changed

| File | Change | Why |
|---|---|---|
| slugify.py | Added the `max_length` parameter and its check. Added `cut_at_word_boundary`, plus `whole_number_of_at_least_one` as the argparse type for the flag. Added `build_parser` and rewrote `main` to use argparse. | Criteria 1 to 11. The owner's note asked for argparse. |
| test_slugify.py | 5 existing tests kept unchanged. Added 17 new test functions (44 new cases with parametrisation). | Criterion 12 and the input-reader tests. |
| README.md | One example: `--max-length 12 "The Quick Brown Fox"` → `the-quick` | The owner's note overrides the brief's "README out of scope". |

## Tests

Suite: 5 tests before, 49 after. All new tests were seen to fail after the fix existed, using the clone method. Each run cloned the worktree into a new temp folder, checked out `ded286d` there, broke one rule in the clone's `slugify.py`, ran the named tests, restored the file and checked the clone was clean. The scripts that did this are `scratchpad/relay-add-max-length/seen_to_fail.py` and `seen_to_fail_round2.py`. Each one makes its own `mktemp` clone, checks that `git rev-parse --show-toplevel` is that clone before every break, and deletes the clone at the end. Both runs printed `clone deleted: True`. Every failure below is an assertion failure, `DID NOT RAISE`, or the code's own ValueError escaping where a refusal was expected. None is an import or collection error. The commands were `python3 -I .../seen_to_fail.py <worktree> ded286d` and `python3 -I .../seen_to_fail_round2.py <worktree> ded286d`, and each break ran `pytest -q -k '<tests>' test_slugify.py` in the clone.

| Test (criterion) | Break in clone | Failing line |
|---|---|---|
| test_no_cap_leaves_a_long_slug_whole (1); existing test_words_are_lower_cased… | default `max_length=5` in `slugify` | `FAILED …test_no_cap_leaves_a_long_slug_whole - AssertionError` |
| test_script_without_a_cap_prints_the_whole_slug (1); existing test_cli_prints_the_slug | `main` defaults the cap to 5 (`caps[0] if caps else 5`) | `FAILED …test_script_without_a_cap_prints_the_whole_slug - Ass…` |
| test_capped_slug_matches_the_cutting_rule_and_fits (2, table) | hard cut `return slug[:max_length]` | `FAILED …[The Quick Brown Fox-12-the-quick]`, `[…-10-…]`, `[a-supercalifragilistic-10-a]` |
| same, [9] case; test_cut_falls_back_to_a_word_boundary[9] | removed the "character after cut is a hyphen" check | `FAILED …test_cut_falls_back_to_a_word_boundary[9] - Assertion…` |
| test_cut_falls_back_to_a_word_boundary[12,10] (3) | hard cut | `FAILED …test_cut_falls_back_to_a_word_boundary[12] - Assertio…` |
| test_cut_never_leaves_a_trailing_hyphen (4) | hard cut | `FAILED …test_cut_never_leaves_a_trailing_hyphen - AssertionEr…` |
| test_short_word_before_a_long_one_keeps_only_whole_words (owner note) | hard cut | `FAILED …test_short_word_before_a_long_one_keeps_only_whole_words` |
| test_slug_that_already_fits_is_unchanged[19,100] (5) | removed the "already fits" guard | `FAILED …test_slug_that_already_fits_is_unchanged[19] - Assert…`, `[100]` |
| test_first_word_longer_than_the_cap_is_cut_not_dropped (6) | no-hyphen case returns `""` | `FAILED …test_first_word_longer_than_the_cap_is_cut_not_dropped` |
| test_empty_slug_stays_empty_with_a_cap (7) | empty slug replaced with `"untitled"` | `FAILED …test_empty_slug_stays_empty_with_a_cap - AssertionErr…` |
| test_cap_below_one_is_refused_in_code[0,-1,-100] (8) | removed the ValueError check | `FAILED …[0] - Failed: DID NOT RAISE` (and -1, -100) |
| test_cap_of_one_is_accepted (8) | check changed to `< 2` | `FAILED …test_cap_of_one_is_accepted - ValueError: max_length …` |
| test_script_with_a_cap_prints_the_cut_slug (9); test_cli_accepts_the_cap_after_the_title_and_with_equals | `main` passes `max_length=None` | `FAILED …test_script_with_a_cap_prints_the_cut_slug - Assertio…` |
| test_cli_refuses_a_bad_cap[0,-3]; test_script_refuses_a_bad_cap_with_exit_2 (10) | removed the `< 1` check in the argparse type | `FAILED …test_cli_refuses_a_bad_cap[0] - ValueError…`, `FAILED …test_script_refuses_a_bad_cap_with_exit_2 - assert 1 …` |
| test_cli_refuses_a_bad_cap[abc,'',1.5,12abc,9×5000] (10) | removed the `except ValueError` that gives the rule's message | `FAILED …test_cli_refuses_a_bad_cap[abc] - assert 'must be a w…` (all 5) |
| test_cli_shows_usage_with_the_flag_when_arguments_are_missing ×4 (11) | `help=argparse.SUPPRESS` on the flag | 4 × `FAILED …[missing-title]`, `[missing-cap-value]`, `[flag-alone]`, `[no-arguments]` |
| test_cli_refuses_the_cap_given_twice | removed the repeat check | `FAILED …test_cli_refuses_the_cap_given_twice - Failed: DID NO…` |
| test_cli_refuses_extra_or_unknown_arguments[abbreviated-flag] | `allow_abbrev=True` | `FAILED …[abbreviated-flag]` |
| test_cli_refuses_extra_or_unknown_arguments[two-titles, short-flag] | `parse_known_args` instead of `parse_args` | `FAILED …[two-titles]`, `[short-flag]` |

My first try at the usage break added a second `help=` keyword. That is a SyntaxError, so the result did not count, and I redid it in round 2 as shown above. The table cases `[19]`, `[100]`, `[2]`, `[Supercalifragilistic-5]`, `[Hello-1]` and `[!!!-5]` were not run under their own breaks. Each one repeats a dedicated test that was seen to fail under the matching break (C, D, G, E).

## Existing tests changed or removed

None. The 5 existing tests are byte-for-byte unchanged. The test file only gained lines (`git diff --stat`: test_slugify.py +176, 0 deletions).

## Input readers

The command line (`main` / `build_parser` / `whole_number_of_at_least_one`) is an input reader. These are the inputs I tried, all through `main`, each ending in `SystemExit(2)` with nothing on stdout:

- `--max-length 0`, `-3`: refused, `must be a whole number of at least 1`. Test: test_cli_refuses_a_bad_cap.
- Wrong type `abc`, `1.5`, `12abc`: same refusal. Same test.
- Empty `""`: same refusal. Same test.
- Far too long, 5000 nines (over Python's int-digit limit, so `int()` raises ValueError): same refusal. Same test.
- Field repeated, `--max-length 5 --max-length 9`: refused, `may be given only once`. Test: test_cli_refuses_the_cap_given_twice.
- Value cut off (`--max-length` with nothing after it), title missing, no arguments: usage plus exit 2. Test: test_cli_shows_usage_with_the_flag_when_arguments_are_missing.
- Forged or abbreviated flag (`--max`, `-m`), two titles: refused. Test: test_cli_refuses_extra_or_unknown_arguments.
- I also ran one by hand through the real script: `python3 slugify.py --max-length 0 "x"` printed `usage: …` and `python3 slugify.py: error: argument --max-length: must be a whole number of at least 1`, exit 2.

`slugify(max_length=...)` is a function argument from the caller's own code, not an outsider's input. Its below-1 refusal is tested in test_cap_below_one_is_refused_in_code. Its handling of the wrong type is under Out-of-scope observations.

## Known-flaw rows

No row touched. The repo has no known-flaws document.

## Commands run

The profile defines only `test`. There is no build or lint command. pyflakes is not installed (`No module named pyflakes`), so I used a compile check instead.

```
$ python3 -m py_compile slugify.py test_slugify.py && echo compiled
compiled

$ python3 -m pytest -q --junitxml=test-results/junit.xml
.................................................                        [100%]
49 passed in 0.12s

$ python3 slugify.py "Crème Brûlée, Two Ways!"          # README example 1
creme-brulee-two-ways
$ python3 slugify.py --max-length 12 "The Quick Brown Fox"   # README example 2
the-quick
```

Base-branch threat model: `git show main:.relay/threat-model.md` returned `fatal: path '.relay/threat-model.md' does not exist in 'main'`, so the strictest default applies.

## Own-round check

- **Sibling:** I compared the code path with the command-line path. The CLI refuses `<1`, non-integers and repeats. The code path refuses `<1` only. `max_length=True` acts as 1, and a float cap on a slug that already fits comes back unchanged. That is normal Python duck typing for a caller's own argument and is not in the brief, so I recorded it under Out-of-scope observations and did not fix it. I found one real sibling of the argparse switch: a title starting with `-` (for example `-x`) is now read as an option and refused, where `main` used to print `x`. Titles like that still work after `--`. I recorded this under Notes rather than changing it, because the owner chose argparse.
- **Bigger claim:** The commit message says the flag "refuses anything but a whole number of at least 1". `int()` also accepts ` 12 ` (spaces around it), `+5` and `1_000`. All of those are whole numbers of at least 1, so the claim holds. The docstring "never ends with a hyphen" is held by the code: a slug never has `--` or a leading hyphen, so every return branch ends on a letter or digit, or is empty.
- **Test that cannot fail:** Every new test failed under a break (table above). Two first-round gaps turned up: `test_script_without_a_cap…` passed under the default-5 break, and `[9]` passed under the hard cut. I closed both with targeted breaks in round 2, and both then failed.
- **Hand-kept list:** The README example's output is not checked by any test. It matches `test_script_with_a_cap_prints_the_cut_slug` today, by hand (I ran it above). I recorded this under Out-of-scope observations.

The check changed no code or test. Its only new work was the round-2 breaks, and their commands and results are above.

## Deviations from the plan

There is no Architect plan for this `feature` task. These are deviations from the Designer's brief:

- **README example added**, which the brief listed as out of scope. The owner's note asked for it.
- **argparse** is used, which the owner's note asked for. Because of it:
  - Usage reads `usage: python3 slugify.py [-h] [--max-length N] "Some Title"`. The brief's wording guide has no `[-h]`, and `-h/--help` now exist.
  - Errors read `python3 slugify.py: error: argument --max-length: must be a whole number of at least 1`.
  - The brief's "no short alias" was about `--max-length`, and `-h` is argparse's built-in help, so I kept it.
- **Repeated `--max-length` is refused.** The brief doesn't cover it. Plain argparse would silently let the last value win, which the input-reader rule doesn't allow.
- **`allow_abbrev=False`.** This keeps `--max` from being silently read as `--max-length`, which the brief never asked for.
- **Two titles** were refused before (usage, exit 2) and are still refused (argparse "unrecognized arguments", exit 2).

## Not verified

- I didn't run any linter because the profile has none.
- The table cases listed under Tests that duplicate dedicated tests were not given their own breaks.
- I only checked behaviour on Python 3.12.8. The `int | None` annotation needs Python 3.10 or later.

## Out-of-scope observations

- `slugify(max_length=...)` doesn't check the type: `True` acts as 1, and a float cap on a slug that already fits comes back unchanged. Other wrong types raise Python's own TypeError. If wanted, a later task could add an `isinstance` check.
- No test checks the README examples against the real output.
- Titles that start with `-` now need `--` before them on the command line, and `--help` now shows help instead of printing the slug `help`. Both come from the argparse choice.

## Notes for the Reviewer

- Criterion 10 says "exit 2". In `main`, argparse raises `SystemExit(2)` rather than returning 2, and `sys.exit(main(...))` turns that into exit 2. The subprocess tests check the real exit code (test_script_refuses_a_bad_cap_with_exit_2) and the main-level tests check `SystemExit.code == 2`. The existing `main(["Ship It"]) == 0` contract is unchanged.
- I used `slug[max_length:max_length + 1]` on purpose rather than `slug[max_length]`, so the boundary check can't raise IndexError if the fits-guard is ever changed.
- Behaviour change to weigh at the gate: `python3 slugify.py "-x"` used to print `x` and now exits 2 with usage. `python3 slugify.py -- "-x"` prints `x`.
