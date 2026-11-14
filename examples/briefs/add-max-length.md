---
# fix: straight to the Builder. feature: the Designer first. project: Designer, then Architect.
size: feature
# yes for auth, input handling, money or production data (adds the Tester and Security).
risky: no
deploy: no
---
# Task Brief: Cap a slug's length

**Target Repo:** slugify

## Goal
Let a caller cap a slug's length, for systems that limit URL or file name
length: `slugify(text, max_length=N)` in code and `--max-length N` on the
command line.

## Context
Long titles make long slugs. A slug cut in the middle of a word reads badly
("the-quick-brown-fo"), so the cut should fall between words where it can.

## Acceptance Criteria
- [ ] `slugify(text)` with no cap behaves exactly as it does today.
- [ ] With a cap, the slug is never longer than `max_length` characters.
- [ ] The cut falls at a hyphen when one is in reach, and the slug never ends with a hyphen.
- [ ] A single word longer than the cap is cut to the cap, not dropped.
- [ ] A cap below 1 is refused with a clear error, in code and on the command line.
- [ ] `python3 slugify.py --max-length 12 "The Quick Brown Fox"` prints `the-quick`.
- [ ] Tests cover each case above.

## Relevant Files
| File | Why |
|------|-----|
| slugify.py | the function and the command line |
| test_slugify.py | the tests |
