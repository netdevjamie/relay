---
task_id: add-max-length
phase: designer
step: 1
date: 2026-11-14
verdict: ready-for-builder
---

# Brief: Cap a slug's length

## Should this exist?

Yes. The other option is to have callers cut the slug themselves (`slug[:12]`). That costs nothing to build, but it gives exactly the result the owner wants to avoid: "the-quick-br", or "the-quick-" with a hyphen left on the end. Every caller would also have to write its own word-boundary logic. Leaving things as they are means slugs from long titles stay too long for systems that limit length. The feature is small, the rule is easy to state, and building it once is cheaper than having each caller rebuild it.

## Problem

Callers who feed slugs into systems with length limits (URL paths, file names) can't ask for a shorter slug. A long title gives a long slug, so they either fail the limit or cut the slug by hand and end up with a broken word or a trailing hyphen.

**Current behavior observed:** I read `slugify.py` and `test_slugify.py` on branch `add-max-length` (same as `main`, commit 377630e). `slugify(text)` takes no length argument. The command line takes exactly one argument and prints a usage line, exiting with code 2, for anything else. I ran it: `python3 slugify.py "The Quick Brown Fox"` prints `the-quick-brown-fox`, and `python3 slugify.py` with no arguments prints `usage: python3 slugify.py "Some Title"` to stderr and exits 2. The existing suite gives `5 passed`.

## Outcome

A caller can ask for a slug no longer than N characters, both in code (`slugify(text, max_length=N)`) and on the command line (`--max-length N`). The result is cut between words wherever it can be, never ends with a hyphen, and stays exactly as it is today when no cap is given.

## The cutting rule (what the criteria test)

1. Build the slug exactly as today.
2. If there is no cap, or the slug already fits within it, return it unchanged.
3. Otherwise, look at the first `max_length` characters:
   - if the character just after them is a hyphen (the cut lands exactly between two words), keep all of them;
   - else, if those characters contain a hyphen, cut at the last one and drop it and everything after it;
   - else (the first word alone is longer than the cap), keep the first `max_length` characters of that word.
4. The result never ends (or starts) with a hyphen.

Worked examples (title, cap → result):

| Title | Cap | Result | Why |
|---|---|---|---|
| The Quick Brown Fox | none | `the-quick-brown-fox` | unchanged |
| The Quick Brown Fox | 19 | `the-quick-brown-fox` | fits exactly |
| The Quick Brown Fox | 100 | `the-quick-brown-fox` | fits |
| The Quick Brown Fox | 12 | `the-quick` | cut back to the last hyphen |
| The Quick Brown Fox | 10 | `the-quick` | trailing hyphen dropped |
| The Quick Brown Fox | 9 | `the-quick` | cut lands between words |
| The Quick Brown Fox | 3 | `the` | cut lands between words |
| The Quick Brown Fox | 2 | `th` | first word longer than cap, hard cut |
| Supercalifragilistic | 5 | `super` | single long word cut, not dropped |
| Hello, World! | 1 | `h` | smallest allowed cap |
| !!! | 5 | (empty) | empty stays empty |

## Acceptance criteria

1. With no cap, every existing test still passes unchanged, and `python3 slugify.py "The Quick Brown Fox"` still prints `the-quick-brown-fox`.
2. With a cap, the result is never longer than `max_length` characters, for every row in the table above.
3. When the slug is longer than the cap and a hyphen falls within the first `max_length` characters, the result ends at a word boundary: `slugify("The Quick Brown Fox", max_length=12)` returns `the-quick`, and so do caps 10 and 9.
4. The result never ends with a hyphen: `slugify("The Quick Brown Fox", max_length=10)` returns `the-quick`, not `the-quick-`.
5. A slug that already fits is returned unchanged: caps 19 and 100 on "The Quick Brown Fox" both return `the-quick-brown-fox`.
6. When the first word alone is longer than the cap, it is cut to the cap, not dropped: `slugify("Supercalifragilistic", max_length=5)` returns `super`; `slugify("The Quick Brown Fox", max_length=2)` returns `th`.
7. An empty slug stays empty with a cap: `slugify("!!!", max_length=5)` returns `""`.
8. In code, a cap of 0 or a negative cap raises a `ValueError` whose message contains `max_length must be at least 1`. A cap of 1 is accepted (`slugify("Hello, World!", max_length=1)` returns `h`).
9. `python3 slugify.py --max-length 12 "The Quick Brown Fox"` prints `the-quick` and exits 0.
10. On the command line, `--max-length 0`, `--max-length -3` and `--max-length abc` each print an error to stderr that names `--max-length` and says it must be a whole number of at least 1, print nothing to stdout, and exit 2.
11. On the command line, a missing title (`python3 slugify.py --max-length 12`), a missing value after `--max-length`, or no arguments at all prints a usage line to stderr that mentions `--max-length`, prints nothing to stdout, and exits 2.
12. The test suite has at least one test for each of criteria 1 to 11, and the full suite passes with `python3 -m pytest -q`.

## Scope

**In:**
- An optional `max_length` argument on `slugify`; leaving it out (or passing nothing) means no cap.
- An optional `--max-length N` flag on the command line, with the title still the one positional argument.
- Error handling for bad caps in code and on the command line, plus an updated usage line.
- Tests for all of the above.

**Out (non-goals, don't add):**
- Other options people often ask for alongside this one: a custom separator, keeping case, stop-word removal, Unicode-preserving slugs.
- Adding a suffix or hash to keep cut slugs unique (for example `the-quick-2`).
- An ellipsis or any marker showing the slug was cut.
- A default cap. Behavior without a cap must not change.
- A short flag alias (such as `-m`), or reading titles from stdin or from several arguments.
- README changes (see open questions).

## Flows and states (command line)

- **Success:** the slug goes to stdout followed by a newline, exit 0.
- **Bad cap** (0, negative, not a whole number): error on stderr, nothing on stdout, exit 2. Wording guide: `error: --max-length must be a whole number of at least 1`.
- **Missing title or missing flag value:** usage on stderr, exit 2. Wording guide: `usage: python3 slugify.py [--max-length N] "Some Title"`.
- **Empty result** (for example `!!!`): an empty line on stdout, exit 0, same as today.

The exact wording above is a guide. The criteria only require the parts quoted in them.

## Data lifecycle

Not applicable. Nothing is stored.

## Contract

Not applicable. One repo only.

## Notes

- There's no `context/` folder and no `.relay/threat-model.md` on `main` (`git show main:.relay/threat-model.md` returned "does not exist"), so the strictest-default rules in the role files apply. The task is marked `risky: no`. The only new input is a number on the command line, and criterion 10 covers it.
- The owner's brief says "a single word longer than the cap is cut to the cap". I read that as "the first word". When a short word comes before a long one, the rule above keeps only the short word (`a-supercalifragilistic` capped at 10 gives `a`). That follows the "cut at a hyphen in reach" rule, and the open questions flag it in case the owner wants something else.

## Open questions for the owner

None of these block the build. The default in brackets applies unless the owner says otherwise at the gate.

1. **Short word, then a long word.** For `a-supercalifragilistic` capped at 10, return `a` (cut at the word boundary, as the rule says) or `a-supercal` (fill the cap)? [Default: `a`.]
2. **README.** Add a one-line `--max-length` example to the README? [Default: no, since it wasn't asked for.]

## Suggested pass

`work`: it's one small, self-contained change to one function and its command line, and the rule and the edge cases are fully written down here.

**Why `ready-for-builder`:** the change fits the existing single-file structure. It adds one optional argument and one command-line flag, with no new dependency, data model, boundary or deploy concern, so no structural decision is needed.
