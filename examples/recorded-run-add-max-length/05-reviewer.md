---
task_id: add-max-length
phase: reviewer
step: 5
date: 2026-11-14
verdict: approved
---

# Review: Cap a slug's length (fix round 1)

Summary for the owner: All three sent-back findings are fixed and proven. Titles starting with a hyphen print a slug again. Only a title that is exactly "--max-length" can't.

## Summary

This is a re-review of commits 45b6298, 0aeb38f and 8411999 on top of ded286d. It answers the owner's send-back of 14:48: "Fix the medium: a title that starts with a hyphen must still print a slug, as it did before. Fix both lows too." I checked each of the three fixes, made every new fix test fail again in a throwaway clone, and read Relay's run for 8411999 (69 passed, 0 skipped). I applied the `work` pass the Designer suggested, because this `feature` task has no Architect plan. The only new finding is a low that can't be avoided: a title that is literally the flag can't be passed. The Builder disclosed it.

## Rated findings

| # | Severity | Finding | Place |
| --- | --- | --- | --- |
| 1 | low | A title that is exactly "--max-length" or starts with "--max-length=" is read as the flag, so it can't be slugged on the command line, unlike before the task. | slugify.py:89 |

Fix now: none

Record: 1

## Findings

### Blockers

None.

### Warnings

None.

### Notes

1. **slugify.py:89-93** (`separate_titles`). The send-back asked that any title starting with a hyphen print a slug as before. That now holds for every such title except one that is exactly `--max-length` or starts with `--max-length=`. Those two are always read as the flag, and there is no escape (`--` is now a title too). This comes with having the flag at all, the Builder disclosed it under Out-of-scope, and no realistic title looks like that. If it ever matters, a later task could add an escape. Record.

How the three findings from 03-reviewer.md were answered:
- **Medium, hyphen titles (fixed).** `separate_titles` moves `--max-length` and its value to the front and puts every other argument after `--`, so argparse never reads a title as an option. `add_help=False` drops `-h/--help`, because on `main` `--help` printed `help`. This is in scope: the owner's note asks for "as it did before". By hand on 3.12: `-x` → `x`, `--help` → `help`, `--` → an empty line, `--max-length=5 -draft-notes` → `draft`. `-- -x` exits 2 on 3.12, 3.13 and 3.14, which matches `main`, where two arguments were refused. `--max-length -x t`, `--max-length -- t` and a bare `--max-length` exit 2 with `expected one argument`, so the value-swallowing path can't be used to sneak a title in as the cap. The usage line is now `usage: python3 slugify.py [--max-length N] "Some Title"`, which matches the brief's wording guide exactly.
- **Low, the 5000-nines row on 3.10 (fixed).** A length check (more than 20 characters) runs before `int()`. I simulated a Python with no digit limit (`PYTHONINTMAXSTRDIGITS=0`). At ded286d the `"9" * 5000` row fails, and at 8411999 all 8 bad-cap rows pass. The new message still contains the criterion-10 wording.
- **Low, README drift (fixed).** `test_readme_example_prints_what_the_readme_shows` runs each `$ python3 slugify.py` line from the README as a real subprocess. `test_readme_has_examples_to_check` stops the parser from silently finding nothing.

The Builder's correction is right: `"--- Draft ---"` (with spaces) already worked at ded286d. My earlier warning used a poor example. It doesn't change that finding.

### Polish later (work pass only)

None.

## Tests

The suite went from 49 to 69 tests, with no lines removed from test_slugify.py. The new tests check exact stdout strings and exact exit codes, in process and through a real subprocess, and they mock nothing. Before the fix, 11 tests failed. The test-only commit 45b6298 shows that, and my revert in a clone gave the same 11. The rows that pass without the fix (`--- Draft ---`, `-`, `-3 Tips`, and `two-hyphen-titles` and `hyphen-titles-around-cap` under the revert) are guards against the new reordering mangling titles. The Builder labels them as guards, and I made the two-title guards fail with their own break (B below). With an empty example list, the parametrised README test is skipped rather than failing. The count test covers that case, and break F shows it failing.

Relay's recorded run: `Tests passed at 8411999eb22f in 0.7s: 69 tests, 0 failed, 0 skipped`. HEAD is `8411999eb22fef31d59a2c772b661294404956cd`, the commit under review. The count went up, with no skips.

## Security

- Input validation: pass. The cap goes through an allowlist type function (an integer of at least 1, at most 20 characters of text). The reordering passes the cap's value through untouched for argparse to check. Two titles and repeated caps are still refused.
- Injection: not applicable. The README test runs `sys.executable` with an argument list, not a shell, and it reads only the repo's own README.
- Auth: not applicable (a local CLI).
- Secrets: pass (none).
- Threat model: `git show main:.relay/threat-model.md` → `fatal: path '.relay/threat-model.md' does not exist in 'main'`, so the strictest default applies. Nothing here reaches beyond the caller's own process.

## Verified vs reasoned

Ran:
- `relay status add-max-length`: read the send-back note quoted in the Summary.
- `git log --oneline -8` → 8411999, 0aeb38f, 45b6298, ded286d, 377630e. `git diff --name-status main..HEAD` → `M README.md`, `M slugify.py`, `M test_slugify.py`. `git diff main..HEAD -- test_slugify.py | grep -c '^-[^-]'` → `0`. `git diff ded286d..HEAD -- slugify.py`, read in full along with the whole of slugify.py, test_slugify.py and README.md.
- The command-line edge cases by hand, as listed under Findings, on python3 3.12, 3.13 and 3.14.
- `git status --short` in the worktree → empty, before and after.

Reasoned: on Python 3.10/3.11 the length check runs before `int()`, so the refusal doesn't depend on the version. That is shown by the `PYTHONINTMAXSTRDIGITS=0` simulation (G and H below), but I didn't run 3.10 or 3.11 themselves because neither is installed.

Seen to fail again: `bash scratchpad/relay-add-max-length-review-2/recheck.sh <worktree> 8411999`. The script makes its own `mktemp -d` clone (`/var/folders/.../tmp.vwaXim7UoY`) and checks that the clone's resolved top level is that folder before each break. After each break it resets, and it prints `clean: []` each time. At the end it deletes the clone (`clone deleted: True`). Each break ran `python3 -m pytest -q -p no:cacheprovider -k <tests> test_slugify.py`.
- A, `git revert --no-commit 8411999 0aeb38f`: `11 failed, 9 passed`. Failed: `test_cli_prints_the_slug_of_a_title_starting_with_a_hyphen[-x-x]`, `[--help-help]`, `[-h-h]`, `[---]`, `[--max-max]`; `test_cli_caps_a_title_starting_with_a_hyphen[cap-first|cap-last|cap-with-equals]`; `test_script_prints_the_slug_of_a_title_starting_with_a_hyphen`; `test_cli_refuses_two_titles_even_when_they_start_with_a_hyphen[separator-and-title]`; `test_cli_refuses_a_cap_written_with_more_than_twenty_digits`.
- B, `separate_titles` keeps only the first title: `FAILED ...two_titles...[two-hyphen-titles]`, `[separator-and-title]`, `[hyphen-titles-around-cap]`.
- C, `LONGEST_CAP_TEXT = 19`: `FAILED test_cli_accepts_a_cap_written_with_twenty_digits - S...` (SystemExit).
- D, `LONGEST_CAP_TEXT = 5000`: `FAILED test_cli_refuses_a_cap_written_with_more_than_twenty_digits`.
- E, README cap example changed to `the-quick-brown`: `FAILED test_readme_example_prints_what_the_readme_shows[arguments1-the-quick-brown]`.
- F, README prompt changed to `$ python slugify.py`: `FAILED test_readme_has_examples_to_check - assert 0 >= 2`.
- G and H, the finding-2 fix with no int digit limit (`PYTHONINTMAXSTRDIGITS=0`): at 8411999 `8 passed`; at ded286d `FAILED test_cli_refuses_a_bad_cap[999...9]` (the 5000-nines row).
- I, the full suite at 8411999 in the clone: `69 passed in 0.23s`.

Existing tests changed or removed (my list): None. This covers every round since the base `main`, because no earlier review has passed the gate. `git diff main..HEAD -- test_slugify.py` removes no lines, and no test file was deleted or renamed. The `"9" * 5000` row is unchanged. It now passes through the length check instead of `int()`'s digit limit and still asserts the same refusal and wording, so it proves no less.

Known-flaw rows confirmed: No row touched. The repo has no known-flaws document.

## Verdict rationale

All three findings the owner sent back are fixed. Each fix test was seen failing in a clone, and Relay's run at 8411999 is green with 69 tests and no skips. Nothing in the fix goes beyond the send-back: dropping `-h/--help` and the 20-character cap limit are direct answers to the medium and the first low. The only finding is a low, recorded and not marked fix now, so the verdict is `approved`.
