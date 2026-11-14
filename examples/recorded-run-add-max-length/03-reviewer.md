---
task_id: add-max-length
phase: reviewer
step: 3
date: 2026-11-14
verdict: approved_with_warnings
---

# Review: Cap a slug's length

Summary for the owner: The length cap works and is well tested. One thing to decide: with argparse, titles that start with a hyphen (like "-x") now fail on the command line.

## Summary

Commit ded286d meets every acceptance criterion in the brief and the Designer's 12 criteria. It also follows the owner's note: whole words only (`a-supercalifragilistic` at 10 gives `a`), one README example, argparse. The code is small and clear, and I made a sample of the tests fail again myself. No plan states a pass because this is a `feature` task with no Architect. The Designer suggested `work` and I reviewed as `work`. The one warning is a behaviour change on the command line that comes with argparse. The Builder disclosed it, and the owner should accept or reject it at the gate.

## Rated findings

| # | Severity | Finding | Place |
| --- | --- | --- | --- |
| 1 | medium | On the command line, a title starting with a hyphen ("-x", "--help") is now refused or shows help instead of printing a slug, so scripts passing such titles get exit 2. | slugify.py:72 |
| 2 | low | The 5000-nines test relies on the integer digit limit added in Python 3.11, so it fails on 3.10, which the code otherwise supports. | test_slugify.py:149 |
| 3 | low | No test checks the new README example against real output, so the two can drift apart unnoticed. | README.md:8 |

Fix now: none

Record: 2, 3

## Findings

### Blockers

None.

### Warnings

1. **slugify.py:72** (`parser.parse_args(argv)`). Before this change, `main` treated any single argument as the title: `python3 slugify.py "-x"` printed `x`, and `python3 slugify.py --help` printed `help`. Now argparse reads them as options. `"-x"` exits 2 with "the following arguments are required", and `--help`/`-h` print help. I confirmed this by hand: `python3 slugify.py "-x"` printed usage and exited 2, `python3 slugify.py -- "-x"` printed `x`, and `python3 slugify.py "-3 Tips"` still printed `3-tips`, because argparse treats negative-number-like text as positional. The brief's "behaves exactly as it does today" covers `slugify(text)`, not the command line, and the owner asked for argparse, so this is not a broken criterion. It does affect the owner at the gate and any script that passes user titles to the command line, such as a title like "--- Draft ---": that call used to succeed and now exits 2. Fix direction: accept the change as it is, or have a later task mention `--` in the usage or README. No code change is needed if the owner accepts it.

### Notes

2. **test_slugify.py:149**. The `"9" * 5000` case expects a refusal only because Python 3.11+ makes `int()` raise ValueError past 4300 digits. The `int | None` annotation runs on 3.10, and there `int()` parses the number, the cap is accepted, and this one test fails. Accepting a huge cap is not wrong behaviour, so this is low. Record.
3. **README.md:8**. The Builder ran the example by hand and it matches `test_script_with_a_cap_prints_the_cut_slug` today, but nothing ties the two together. Record.

Beyond the brief: the repeat-flag refusal, `allow_abbrev=False` and the built-in `-h` all come from the owner's argparse choice, and the Builder listed each as a deviation. I don't count them as scope creep. The README line was out of scope in the brief, but the owner's note puts it back in.

### Polish later (work pass only)

None.

## Tests

The suite went from 5 tests to 49. Every Designer criterion 1 to 11 has a dedicated test, and the owner's short-word rule has one too (`test_short_word_before_a_long_one_keeps_only_whole_words`, plus a table row with the exact `a-supercalifragilistic` input). The table test also checks the general properties on every row: length within the cap, no leading or trailing hyphen. Command-line tests cover both levels: in-process (`SystemExit.code == 2`, stdout empty) and real subprocess runs that check the exit code. No mocks stand in for the code under test. None of the assertions can pass for the wrong reason: each compares exact strings or exact exit codes. The table rows that repeat dedicated tests were not broken one by one, which is fine because their dedicated tests were.

Relay's recorded run: `Tests passed at ded286d8f152 in 0.6s: 49 tests, 0 failed, 0 skipped`. That is the commit under review (`git rev-parse HEAD` = ded286d8f152…). The count went up, with no skips.

## Security

- Input validation: pass. `--max-length` goes through an allowlist-style type function (int ≥ 1, otherwise refused). Repeats, abbreviations and unknown flags are refused. The in-code cap below 1 raises ValueError.
- Injection: not applicable (no shell, SQL or file paths).
- Auth: not applicable (a local CLI).
- Secrets: pass (none present).
- Threat model: none on `main` (`git show main:.relay/threat-model.md` gave `fatal: path '.relay/threat-model.md' does not exist in 'main'`), so the strictest default applies. Nothing here reaches beyond the caller's own process.

## Verified vs reasoned

Ran:
- `git log --oneline -5` → `ded286d` on top of `377630e`. `git diff --name-status main...HEAD` → `M README.md`, `M slugify.py`, `M test_slugify.py`. `git diff --stat`: test_slugify.py +176, 0 deletions.
- `python3 slugify.py "-x"` → usage + exit 2. `python3 slugify.py -- "-x"` → `x`. `python3 slugify.py "-3 Tips"` → `3-tips`. `python3 slugify.py "--help"` → help text. On `main`, `slugify.py` with argv `["-x"]` printed `x`.
- Seen-to-fail script (below). The worktree's `git status --short` was empty afterwards.

Reasoned from reading: `cut_at_word_boundary` against every row of the Designer's table and the owner's note. A slug never contains `--` or a leading hyphen, so every return branch ends on a letter or digit or is empty, and the result is always ≤ max_length.

Seen to fail again: these are feature tests, not fix tests, but I re-ran a sample on ded286d in a fresh clone with `bash scratchpad/relay-add-max-length-review/recheck.sh <worktree> ded286d`. The script makes its own `mktemp -d` clone, checks the clone's top level before each break, restores and checks the clone is clean after each one, and deletes the clone at the end (`clone deleted: True`). Each break ran `python3 -m pytest -q -p no:cacheprovider -k '<tests>' test_slugify.py`:
- hard cut `return slug[:max_length]` → `AssertionError: assert 'the-quick-br' == 'the-quick'`, `assert 'the-quick-' == 'the-quick'`, `assert 'a-supercal' == 'a'` (table, boundary and trailing-hyphen tests, owner-note row).
- removed the after-cut hyphen check → `FAILED test_slugify.py::test_cut_falls_back_to_a_word_boundary[9]` (`assert 'the' == 'the-quick'`).
- removed the in-code ValueError → `FAILED test_cap_below_one_is_refused_in_code[0]` / `[-1]` `Failed: DID NOT RAISE`.
- no-hyphen branch returns `""` → `FAILED test_first_word_longer_than_the_cap_is_cut_not_dropped` (`assert '' == 'super'`).
- CLI passes `max_length=None` → `FAILED test_script_with_a_cap_prints_the_cut_slug`, `FAILED test_cli_accepts_the_cap_after_the_title_and_with_equals`.
- CLI type accepts < 1 → `ValueError: max_length must be at least 1, got 0` escapes in test_cli_refuses_a_bad_cap, and the subprocess test fails with returncode 1 ≠ 2.
- removed the repeat check → `FAILED test_cli_refuses_the_cap_given_twice - Failed: DID NO…`.
I did not re-break the usage-line, abbreviation, empty-slug or already-fits tests. The Builder's table shows a failing line for each of them.
Existing tests changed or removed (my list): None. From `git diff main...HEAD -- test_slugify.py`, the 5 original tests (lines 13–31) are unchanged, and the file only gained lines. No test file was deleted or renamed.
Known-flaw rows confirmed: No row touched. The repo has no known-flaws document.

## Verdict rationale

Every criterion is met, the tests prove what their names claim, and Relay's run on ded286d is green with 49 tests. The highest rating is medium (finding 1). That is a command-line behaviour change that comes with argparse, which the owner asked for. It needs the owner's yes at the gate, not a code change. So the verdict is `approved_with_warnings`.
