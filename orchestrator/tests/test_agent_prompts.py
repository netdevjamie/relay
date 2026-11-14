"""Every role prompt carries the same shared rules, word for word.

Each role file is self-contained by design (no shared include), so these
lines are duplicated on purpose. This test keeps the copies from drifting.

The sections several roles share are compared with each other and with the
code that reads or routes on them, never with a copy of the prose kept
here: rewording every copy alike passes, changing one fails.
"""
import re
import shlex
import shutil
import subprocess
from itertools import combinations
from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"
# The install notes give two commands that find a copy of a role by the name
# inside the file. The first searches one project-level folder, its last
# word. The second searches every such folder under the workspace, its
# second word.
INSTALL_NOTES = AGENTS_DIR.parent / "DEPLOYMENT.md"
SEARCH_FOR_ROLE_NAMES = "grep -rliE"
SEARCH_EVERY_PROJECT_FOLDER = 'find "$WORKSPACE" '
# The install checks copy the smoke command from Relay's own profile.
PROFILE = AGENTS_DIR.parent / ".relay" / "profile.yaml"
ROLE_FILES = ["designer.md", "architect.md", "builder.md", "reviewer.md",
              "tester.md", "security.md", "ops.md"]
SHARED_RULES = """## Rules for every role

- Never print or commit secrets.
- No AI or assistant references in commits.
- If something outside your role or scope needs doing, flag it; don't do it.
- Evidence or it didn't happen: show the command and its output, or say plainly that you didn't run it.
- A shell command longer than about 10 lines, any heredoc, and any inline Python or Node goes first into a named file in your session's scratchpad folder (else `$TMPDIR/relay-<task-id>/`), never a repo or Relay's install folder, and runs by name in one line; one-line commands stay inline. If you may write nothing but your report, keep every command to one line.
"""


def test_the_install_notes_search_finds_every_role_by_the_name_inside_the_file(tmp_path):
    """Every role file, copied into a sub-folder under another file name, is
    listed, and a file named for a role that holds another name is not."""
    commands = [line for line in INSTALL_NOTES.read_text().splitlines()
                if line.startswith(SEARCH_FOR_ROLE_NAMES)]
    assert len(commands) == 1, "DEPLOYMENT.md must give the search for role names once"
    backup = tmp_path / "backup"
    backup.mkdir()
    for role_file in ROLE_FILES:
        shutil.copy(AGENTS_DIR / role_file, backup / f"old-{role_file}")
    (tmp_path / "builder.md").write_text("---\nname: deploy-helper\n---\n")
    search = shlex.split(commands[0])[:-1] + [str(tmp_path)]
    listed = subprocess.run(search, capture_output=True, text=True, timeout=30).stdout.split()
    assert sorted(Path(path).name for path in listed) == sorted(
        f"old-{role_file}" for role_file in ROLE_FILES), (
        "the search in DEPLOYMENT.md does not list each role by its name: line, and only those")


def test_the_search_over_every_project_folder_finds_every_role_in_each(tmp_path):
    """In a stand-in workspace, every role file under two project-level
    folders is listed. A file that holds another name, a copy outside a
    `.claude/agents` folder and a copy deeper than the command's own limit
    are not."""
    commands = [line for line in INSTALL_NOTES.read_text().splitlines()
                if line.startswith(SEARCH_EVERY_PROJECT_FOLDER)]
    assert len(commands) == 1, (
        "DEPLOYMENT.md must give the search over every project-level folder once")
    near = tmp_path / "one" / ".claude" / "agents"
    nested = tmp_path / "group" / "two" / ".claude" / "agents" / "backup"
    for folder in (near, nested):
        folder.mkdir(parents=True)
        for role_file in ROLE_FILES:
            shutil.copy(AGENTS_DIR / role_file, folder / f"old-{role_file}")
    (near / "builder.md").write_text("---\nname: deploy-helper\n---\n")
    for not_searched in (tmp_path / "one" / "agents",
                         tmp_path / "a" / "b" / "c" / "d" / ".claude" / "agents"):
        not_searched.mkdir(parents=True)
        shutil.copy(AGENTS_DIR / "builder.md", not_searched / "builder.md")
    words = shlex.split(commands[0])
    search = [words[0], str(tmp_path)] + words[2:]
    listed = subprocess.run(search, capture_output=True, text=True, timeout=30).stdout.split()
    expected = [str(folder / f"old-{role_file}")
                for folder in (near, nested) for role_file in ROLE_FILES]
    assert sorted(listed) == sorted(expected), (
        "the search over every project-level folder in DEPLOYMENT.md does not list "
        "each role in each folder, and only those")


def test_the_install_checks_quote_the_profiles_smoke_command():
    notes = " ".join(INSTALL_NOTES.read_text().split())
    smoke_commands = [line.split("smoke:", 1)[1].strip()
                      for line in PROFILE.read_text().splitlines()
                      if line.strip().startswith("smoke:")]
    assert len(smoke_commands) == 1, "the profile must name one smoke command"
    assert f"`{smoke_commands[0]}`" in notes, (
        "the smoke command in DEPLOYMENT.md's install checks is not the profile's")


@pytest.mark.parametrize("role_file", ROLE_FILES)
def test_role_file_contains_the_shared_rules_verbatim(role_file):
    text = (AGENTS_DIR / role_file).read_text()
    assert SHARED_RULES in text, f"{role_file} is missing or has altered the shared rules"


# The owner approves a role's commands from their phone, where a long script
# cannot be judged: each part of the named-scripts rule, by its words.
NAMED_SCRIPT_RULE_PARTS = ("about 10 lines", "any heredoc", "inline Python or Node", "named file",
                           "runs by name in one line", "one-line commands stay inline",
                           "never a repo or Relay's install folder", "keep every command to one line")


@pytest.mark.parametrize("role_file", ROLE_FILES)
def test_every_role_runs_long_commands_from_a_named_file(role_file):
    text = (AGENTS_DIR / role_file).read_text()
    missing = [part for part in NAMED_SCRIPT_RULE_PARTS if part not in text]
    assert not missing, f"{role_file} lacks the named-scripts rule's: {missing}"


def test_every_role_file_is_listed():
    on_disk = sorted(path.name for path in AGENTS_DIR.glob("*.md"))
    assert on_disk == sorted(ROLE_FILES), "a role file was added or removed; update ROLE_FILES"


@pytest.mark.parametrize("role_file", ROLE_FILES)
def test_role_file_frontmatter_names_its_phase(role_file):
    text = (AGENTS_DIR / role_file).read_text()
    phase = role_file.removesuffix(".md")
    assert text.startswith("---\nname: "), f"{role_file} must start with frontmatter"
    assert f"phase: {phase}" in text, f"{role_file} output template must declare phase: {phase}"


@pytest.mark.parametrize("role_file", ROLE_FILES)
def test_prompt_verdicts_match_the_router(role_file):
    """Nit 3: the verdicts a prompt offers must be exactly the ones the
    orchestrator routes, so prompts and code can't drift apart."""
    import sys
    sys.path.insert(0, str(AGENTS_DIR.parent / "orchestrator"))
    import settings
    phase = role_file.removesuffix(".md")
    text = (AGENTS_DIR / role_file).read_text()
    match = re.search(r"^verdict: (.+)$", text, re.MULTILINE)
    assert match, f"{role_file} output template has no verdict line"
    offered = [verdict.strip() for verdict in match.group(1).split("|")]
    assert sorted(offered) == sorted(settings.VALID_VERDICTS[phase])


# The three roles whose findings the card lists carry one more shared
# section, word for word: how to write the summary line and the rated
# findings. report_reader.py reads exactly what this section asks for.
# test_rating_phases_are_the_roles_with_a_rated_section holds this list to
# the one the printed prompt uses.
RATING_ROLE_FILES = ["reviewer.md", "tester.md", "security.md"]
RATED_FINDINGS_HEADING = "## Summary line and rated findings"
VERDICT_HEADING = "## Verdict (exactly one)"
RETIRED_RATING_SENTENCES = (r"A rating never changes", r"gets? no row")
# An existing test that now proves less is rated in one place, the shared
# definition, under this name. A second sentence elsewhere that gave it a
# rating is how two readers came to two verdicts.
WEAKENED_TEST = "proves less"
# The rating is given once, by a table in the shared section: one row per
# rating. Its rows are indented under the Severity bullet, which is what
# tells them from the example table a report copies.
WEAKENED_TEST_TABLE_ROW = "  |"
# The plan or a send-back excuses it only by naming that change to the test.
# Any rework they did not name is rated under this name, whether a fix, a
# planned feature or anything else forced it.
FORCED_REWORK = "whatever made it necessary"

# The three roles that prove a test carry one procedure, word for word.
SEEN_TO_FAIL_ROLE_FILES = ["builder.md", "reviewer.md", "tester.md"]
SEEN_TO_FAIL_HEADING = "## Seen to fail"
SEEN_TO_FAIL_NAME = "Seen to fail"
# A shell variable that held the clone's path is empty in the next shell, and
# git then runs the break in the current folder. The rule against that.
CLONE_PATH_RULE = "**the clone's literal path**"
# A script that runs the breaks cannot type a path. It may hold the path of
# a clone on these conditions, said under the step that forbids a variable,
# and no other path; a command typed by hand gets no such leave.
SCRIPT_RULE = "**A script**"
WHAT_LETS_A_SCRIPT_HOLD_THE_PATH = ("it made the clone itself", "in that run",
                                    "new temporary folder", "before each break",
                                    "resolved top-level folder is that folder")
NO_OTHER_PATH = "Any other path, never."
TYPED_BY_HAND = "typed one at a time never may"
# The step that keeps a lost path from sending the break into the worktree.
STOP_STEP = "**Stop**"
TAKES_THE_FIX_OUT = "git revert"
COMMANDS_THAT_LOSE_WORK = ("git stash", "git checkout", "git restore", "git reset")
OTHER_WAYS_TO_FAIL_A_TEST = (r"git\s+(?:stash|clone|revert|worktree|checkout|restore|reset)\b",
                             "parent commit", "temporary checkout", "test-only commit")

# Each standing rule has a short name that other roles and later readers
# use. Deleting the rule from a role file removes the name; rewording the
# rule keeps it. Per rule and role file: the names its text must hold before
# the Output section, then the names its report skeleton must hold.
ATTACK_YOUR_OWN_ROUND = "**Attack your own round**"
# Forged or unexpected input goes only where it can hurt nothing.
THROWAWAY_TARGETS = "**Throwaway targets only"
# The Tester's step 4 tries input readers first, then these.
OTHER_EDGE_CASES = "Then pick the other edge cases"
STEP_AFTER_TRY_TO_BREAK_IT = "**5."
# The Builder and the Tester decide which code the input rule covers by one
# definition, on a line of its own in each file.
INPUT_READER = "**An input reader**"
KINDS_OF_FLAW_TO_LOOK_FOR = 4
# What stops Security's `secure` besides a finding, one per line.
STOPS_OF_SECURE = "**These also stop `secure`**"
WAYS_SECURE_IS_STOPPED = 3
# The one thing Security may attack that the profile does not list, and
# when: said on one line, directly after the allowlist.
THROWAWAY_COPY = "local copy this run made and will delete"
WHERE_A_THROWAWAY_COPY_COUNTS = "When targets are listed but none reaches the change"
ALLOWLIST = "That list is an allowlist"
# Security's cap is defined under this name, and nowhere else says when it
# applies.
THE_CAP = "**The cap.**"
WHEN_THE_CAP_APPLIES = "no safe"
# What the lists of a review that approves cover: said where the
# Reviewer builds its list and where the report's three labels are named.
ROUNDS_AN_APPROVAL_COVERS = "every round since the last review that approved"
# What that comes to in the commonest fix round, where every earlier review
# asked for changes: said once, where the Reviewer builds its list.
ROUNDS_COVERED_WHEN_NONE_APPROVED = "or since the task's base when none has"
STANDING_RULES = {
    "1-builder": ([ATTACK_YOUR_OWN_ROUND], ["## Own-round check"]),
    "2-builder": ([SEEN_TO_FAIL_HEADING, "**Seen to fail**", CLONE_PATH_RULE],
                  [SEEN_TO_FAIL_NAME]),
    "2-reviewer": ([SEEN_TO_FAIL_HEADING, "**Seen to fail**", CLONE_PATH_RULE],
                   ["saw fail again"]),
    "2-tester": ([SEEN_TO_FAIL_HEADING, "**Seen to fail**", CLONE_PATH_RULE],
                 ["Taken out or broken"]),
    "3-builder": (["**Input it was not written for.**", INPUT_READER, THROWAWAY_TARGETS,
                   "No input reader in this change"], ["## Input readers"]),
    "3-tester": (["backstop for input", INPUT_READER, THROWAWAY_TARGETS,
                  "No input reader in this change"], ["No input reader in this change"]),
    "4-builder": (["**Known-flaw rows are confirmed.**"], ["## Known-flaw rows"]),
    "4-reviewer": (["**Known-flaw rows.**", "is not a finding"], ["known-flaw row"]),
    "5-builder": (["**Existing tests changed or removed.**",
                   '"Existing tests changed or removed"'],
                  ["## Existing tests changed or removed"]),
    "5-reviewer": (["**Existing tests changed or removed.**",
                    "**the plan or the send-back note itself**", ROUNDS_AN_APPROVAL_COVERS,
                    ROUNDS_COVERED_WHEN_NONE_APPROVED],
                   ["existing tests changed or removed", ROUNDS_AN_APPROVAL_COVERS]),
    "6-security": ([STOPS_OF_SECURE, "**listed, not rated**", THE_CAP], ["## Not verified"]),
}

# How much a role file may hold: the length of a line of prose and the
# words in the file. The four rating and building roles have a word
# ceiling; the other three are named so that a new role file has to be put
# in one list or the other. A ceiling is what `wc -w` gave for the file when
# it was set, so a change that adds a sentence has to tighten another. List
# markers and the bars of a table count as words. Raising a ceiling is
# recorded in context/DECISIONS.md with the reason.
LONGEST_LINE_OF_PROSE = 450
WORD_CEILINGS = {"builder.md": 2596, "reviewer.md": 3512, "tester.md": 3594,
                 "security.md": 2967}
ROLE_FILES_WITH_NO_SIZE_LIMIT = ["architect.md", "designer.md", "ops.md"]


def section(role_file: str, heading: str) -> str:
    """The text under a `## ` heading of a role file, up to the next `## `
    heading. Empty when the file has no such heading."""
    text = (AGENTS_DIR / role_file).read_text()
    if f"{heading}\n" not in text:
        return ""
    return text.split(f"{heading}\n", 1)[1].split("\n## ", 1)[0].strip()


def before_output(role_file: str) -> str:
    """A role file's instructions: everything above its Output section."""
    return (AGENTS_DIR / role_file).read_text().split("## Output\n", 1)[0]


def output_template(role_file: str) -> str:
    """The report skeleton inside a role file's Output section."""
    text = (AGENTS_DIR / role_file).read_text()
    return text.split("## Output\n", 1)[1].split("```markdown\n", 1)[1].rsplit("```", 1)[0]


def lines_of_prose(role_file: str) -> list[tuple[int, str]]:
    """A role file's lines with their numbers, leaving out table rows and
    what is inside a code block: those are wide by nature. A code block
    that never closes would leave out the rest of the file, so it fails."""
    prose, inside_code_block = [], False
    for number, line in enumerate((AGENTS_DIR / role_file).read_text().splitlines(), 1):
        if line.startswith("```"):
            inside_code_block = not inside_code_block
        elif not inside_code_block and not line.lstrip().startswith("|"):
            prose.append((number, line))
    assert not inside_code_block, f"{role_file} has a code block that never closes"
    return prose


def numbered_steps(role_file: str, heading: str) -> list[str]:
    """The numbered steps under a heading, in order, without their numbers."""
    return [step.group(1) for step in
            (re.match(r"\s+\d+\. (.+)", line) for line in section(role_file, heading).splitlines())
            if step]


def weakened_test_ratings(role_file: str) -> list[list[str]]:
    """The rows of the table that rates an existing test that proves less:
    the answers to its two questions, then the rating."""
    table = [line for line in section(role_file, RATED_FINDINGS_HEADING).splitlines()
             if line.startswith(WEAKENED_TEST_TABLE_ROW)]
    return [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in table[2:]]


def rating_word() -> re.Pattern:
    """Matches any one of the ratings the card reads, as a whole word."""
    import report_reader
    return re.compile(rf"\b(?:{'|'.join(report_reader.SEVERITIES)})\b", re.IGNORECASE)


def defined_ratings(role_file: str) -> list[str]:
    """The ratings a role file defines, in its order: the sub-bullets under
    the Severity bullet of its rated-findings section."""
    lines = section(role_file, RATED_FINDINGS_HEADING).splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("- **Severity**")]
    if not starts:
        return []
    ratings = []
    for line in lines[starts[0] + 1:]:
        definition = re.match(r"  - \*\*(\w+):\*\* \S", line)
        if not definition:
            break
        ratings.append(definition.group(1).lower())
    return ratings


def verdict_table(role_file: str) -> list[tuple[str, str]]:
    """The rows of a role file's verdict table as (highest rating, verdict).
    A row with no verdict cell is left out, so a table cut short reads as a
    table with rows missing, not as an error."""
    rows = [[cell.strip() for cell in line.strip("|").split("|")]
            for line in section(role_file, VERDICT_HEADING).splitlines() if line.startswith("|")]
    return [(cells[0], cells[1].strip("`")) for cells in rows[2:] if len(cells) > 1]


@pytest.mark.parametrize("first, second", list(combinations(RATING_ROLE_FILES, 2)))
def test_rated_findings_section_is_identical(first, second):
    assert section(first, RATED_FINDINGS_HEADING), f"{first} has no rated findings section"
    assert section(first, RATED_FINDINGS_HEADING) == section(second, RATED_FINDINGS_HEADING), (
        f"{first} and {second} differ in the rated findings section")
    for role_file in (first, second):
        text = (AGENTS_DIR / role_file).read_text()
        assert text.index(SHARED_RULES) < text.index(RATED_FINDINGS_HEADING)
        next_section = text.split(f"{RATED_FINDINGS_HEADING}\n", 1)[1].partition("\n## ")[2]
        assert next_section.startswith("Output\n"), (
            f"{role_file}: the rated findings section must be the last before Output")


@pytest.mark.parametrize("role_file", sorted(set(ROLE_FILES) - set(RATING_ROLE_FILES)))
def test_other_roles_are_not_asked_for_rated_findings(role_file):
    assert "Rated findings" not in (AGENTS_DIR / role_file).read_text()


def test_rating_phases_are_the_roles_with_a_rated_section():
    """The printed prompt tells a phase to rate its findings exactly when
    that phase's role file says how."""
    import agents
    assert [f"{phase}.md" for phase in agents.RATING_PHASES] == RATING_ROLE_FILES
    for role_file in ROLE_FILES:
        has_section = RATED_FINDINGS_HEADING in (AGENTS_DIR / role_file).read_text()
        assert has_section == (role_file.removesuffix(".md") in agents.RATING_PHASES), role_file


def test_prompt_and_reader_agree():
    """The section names the strings the reader looks for, and the example
    table in it, written into a report, reads as one high finding."""
    import report_reader
    rated_findings_rules = section("reviewer.md", RATED_FINDINGS_HEADING)
    for marker in (report_reader.SUMMARY_PREFIX, report_reader.RATED_HEADING,
                   report_reader.NO_FINDINGS_LINE):
        assert marker in rated_findings_rules
    for severity in report_reader.SEVERITIES:
        assert f"`{severity}`" in rated_findings_rules

    table = "\n".join(line for line in rated_findings_rules.splitlines() if line.startswith("|"))
    example = "Summary for the owner: Security found 7 gaps. None is live."
    assert f"Example: `{example}`" in rated_findings_rules
    report = report_reader.read_report(
        f"# Review: a task\n\n{example}\n\n{report_reader.RATED_HEADING}\n\n{table}\n\n## Findings\n")
    assert report["summary_line"] == "Security found 7 gaps. None is live."
    assert report["unreadable_rows"] == []
    assert report["findings"] == [
        {"number": "1", "severity": "high", "place": "api/auth.py:88",
         "statement": "The reset link still works after it has been used, so an old email "
                      "lets anyone sign in."}]


@pytest.mark.parametrize("role_file", RATING_ROLE_FILES)
def test_rating_definition_is_the_readers_three_words(role_file):
    """Each rating has its own definition, and the ratings defined are the
    ones the card reads, highest first."""
    import report_reader
    assert defined_ratings(role_file) == list(report_reader.SEVERITIES)


@pytest.mark.parametrize("role_file", RATING_ROLE_FILES)
def test_verdict_table_agrees_with_the_router(role_file):
    """The highest rating decides the verdict, and each verdict the table
    gives does what the table's rating means: high goes back to the Builder,
    medium stops for the owner, low or nothing moves on."""
    import board_view
    import report_reader
    import routing
    import settings
    phase = role_file.removesuffix(".md")
    table = verdict_table(role_file)
    assert len(table) == len(report_reader.SEVERITIES), f"{role_file} has no verdict table"
    for (rating, verdict), severity in zip(table, report_reader.SEVERITIES):
        assert rating.startswith(severity)
        assert verdict in settings.VALID_VERDICTS[phase]
    (_, when_high), (_, when_medium), (_, when_low) = table

    assert routing.ROUTING[phase][when_high] == ("builder", False)
    assert (phase, when_medium) in routing.WARNING_VERDICTS
    assert routing.ROUTING[phase][when_medium][1] is True
    assert when_low == board_view._NOTHING_FOUND_VERDICT[phase]
    moves_on_to, stops_for_the_owner = routing.ROUTING[phase][when_low]
    assert moves_on_to != "builder" and stops_for_the_owner is False


@pytest.mark.parametrize("role_file", RATING_ROLE_FILES)
def test_retired_rating_sentences_are_gone(role_file):
    """A rating decides the verdict and every finding has a row, so the two
    sentences that said otherwise must not come back."""
    text = (AGENTS_DIR / role_file).read_text()
    for retired in RETIRED_RATING_SENTENCES:
        assert not re.search(retired, text), f"{role_file} still says {retired!r}"


@pytest.mark.parametrize("role_file", RATING_ROLE_FILES)
def test_a_weakened_test_is_rated_in_the_shared_definition_only(role_file):
    """The High and the Medium definitions both name it, and no sentence
    outside the shared section that names it holds a rating word."""
    rated = section(role_file, RATED_FINDINGS_HEADING)
    for rating in ("High", "Medium"):
        definition = [line for line in rated.splitlines()
                      if line.startswith(f"  - **{rating}:**")]
        assert len(definition) == 1, f"{role_file} has no {rating} definition"
        assert WEAKENED_TEST in definition[0], (
            f"{role_file}: the {rating} definition no longer rates a test that {WEAKENED_TEST}")
    elsewhere = (AGENTS_DIR / role_file).read_text().replace(rated, "")
    for sentence in re.split(r"(?<=\.)\s+", elsewhere):
        if WEAKENED_TEST in sentence:
            assert not rating_word().search(sentence), (
                f"{role_file} rates a test that {WEAKENED_TEST} outside the shared definition: "
                f"{sentence!r}")


@pytest.mark.parametrize("role_file", RATING_ROLE_FILES)
def test_a_weakened_test_is_rated_by_one_table(role_file):
    """The table has one row for each rating the card reads, highest first.
    A rework nobody named, whatever forced it, is named once in the shared
    section, in a row that answers no and is rated highest."""
    import report_reader
    highest = report_reader.SEVERITIES[0]
    rows = weakened_test_ratings(role_file)
    assert [row[-1] for row in rows] == list(report_reader.SEVERITIES), (
        f"{role_file}: the table for a test that {WEAKENED_TEST} does not give each rating once")
    forced = [row for row in rows if FORCED_REWORK in row[0]]
    assert len(forced) == 1, f"{role_file}: no row of the table rates a rework, {FORCED_REWORK}"
    assert forced[0][0].startswith("no") and forced[0][-1] == highest, (
        f"{role_file}: a rework, {FORCED_REWORK}, is not rated {highest}")
    assert section(role_file, RATED_FINDINGS_HEADING).count(FORCED_REWORK) == 1, (
        f"{role_file} rates a rework, {FORCED_REWORK}, in a second place")


@pytest.mark.parametrize("role_file", RATING_ROLE_FILES)
def test_report_has_a_place_for_every_finding_in_full(role_file):
    """The rated table holds one sentence per finding. The skeleton has a
    Findings heading for the long text, and no rating word under it."""
    place = re.search(r"^## Findings\b.*\n(?:(?!## ).*\n)*", output_template(role_file),
                      re.MULTILINE)
    assert place, f"{role_file}'s report has no place for the findings in full"
    assert not rating_word().search(place.group(0)), (
        f"{role_file}'s place for the findings in full is kept for one rating")


@pytest.mark.parametrize("first, second", list(combinations(SEEN_TO_FAIL_ROLE_FILES, 2)))
def test_seen_to_fail_is_one_text_in_three_files(first, second):
    assert section(first, SEEN_TO_FAIL_HEADING), f"{first} has no Seen to fail section"
    assert section(first, SEEN_TO_FAIL_HEADING) == section(second, SEEN_TO_FAIL_HEADING), (
        f"{first} and {second} differ in the Seen to fail section")


def test_seen_to_fail_forbids_what_loses_work():
    bullets = section("builder.md", SEEN_TO_FAIL_HEADING).splitlines()
    never = [line for line in bullets if line.startswith("- **Never**")]
    assert len(never) == 1, "Seen to fail must say once what a role never does"
    for command in COMMANDS_THAT_LOSE_WORK:
        assert f"`{command}`" in never[0]
    assert any(line.startswith("- **Record**") for line in bullets)


def test_seen_to_fail_stops_in_a_step_of_its_own():
    """The clone procedure is numbered steps. One of them is the stop, after
    the step that names the literal path and before the one that breaks
    anything."""
    steps = numbered_steps("builder.md", SEEN_TO_FAIL_HEADING)
    stops = [index for index, step in enumerate(steps) if step.startswith(STOP_STEP)]
    assert len(stops) == 1, "Seen to fail must have one step that is the stop"
    names_the_path = [index for index, step in enumerate(steps) if CLONE_PATH_RULE in step]
    breaks = [index for index, step in enumerate(steps) if TAKES_THE_FIX_OUT in step]
    assert names_the_path and breaks, "Seen to fail has no step for the path or for the break"
    assert names_the_path[0] < stops[0] < breaks[0], (
        "the stop must come after the path is named and before anything is broken")


def test_a_script_may_hold_only_the_path_of_a_clone_it_made_and_checks():
    """Said once, directly under the step that forbids a variable, which
    still forbids it; the other two files hold the same text."""
    lines = [line.strip() for line in section("builder.md", SEEN_TO_FAIL_HEADING).splitlines()]
    for_a_script = [index for index, line in enumerate(lines) if SCRIPT_RULE in line]
    assert len(for_a_script) == 1, "Seen to fail must say once what a script may do with the path"
    rule, step_above = lines[for_a_script[0]], lines[for_a_script[0] - 1]
    assert CLONE_PATH_RULE in step_above and "never the variable" in step_above, (
        "the rule for a script belongs under the step that forbids a variable")
    for condition in WHAT_LETS_A_SCRIPT_HOLD_THE_PATH:
        assert condition in rule, (
            f"the rule for a script does not name a condition for holding the path: {condition}")
    assert NO_OTHER_PATH in rule, "the rule for a script must refuse every path but its own clone's"
    assert TYPED_BY_HAND in rule, "the leave for a script must not reach a command typed by hand"


@pytest.mark.parametrize("role_file", SEEN_TO_FAIL_ROLE_FILES)
def test_no_other_passage_gives_a_way_to_fail_a_test(role_file):
    """Outside the shared section a role file points to Seen to fail by name
    and describes no second method."""
    text = (AGENTS_DIR / role_file).read_text()
    elsewhere = text.replace(section(role_file, SEEN_TO_FAIL_HEADING), "").replace(
        SEEN_TO_FAIL_HEADING, "")
    for other_way in OTHER_WAYS_TO_FAIL_A_TEST:
        assert not re.search(other_way, elsewhere), f"{role_file} still says {other_way!r}"
    assert SEEN_TO_FAIL_NAME in elsewhere, f"{role_file} never points to Seen to fail"


@pytest.mark.parametrize("rule", STANDING_RULES)
def test_standing_rule_is_in_its_role_file(rule):
    in_the_instructions, in_the_report = STANDING_RULES[rule]
    role_file = f"{rule.split('-')[1]}.md"
    for name in in_the_instructions:
        assert name in before_output(role_file), f"{role_file} does not say {name}"
    for name in in_the_report:
        assert name in output_template(role_file), f"{role_file}'s report has no place for {name}"


def test_own_round_check_names_four_kinds_of_flaw():
    """The Builder looks for each kind by name, and reports one line each."""
    lines = before_output("builder.md").splitlines()
    named = [index for index, line in enumerate(lines) if ATTACK_YOUR_OWN_ROUND in line]
    assert len(named) == 1, "builder.md must say once to attack your own round"
    kinds = 0
    for line in lines[named[0] + 1:]:
        if not line.startswith("- **"):
            break
        kinds += 1
    assert kinds == KINDS_OF_FLAW_TO_LOOK_FOR


def test_an_input_reader_is_defined_alike_for_the_builder_and_the_tester():
    """The line that defines it and the line of examples under it."""
    definitions = {}
    for role_file in ("builder.md", "tester.md"):
        lines = [line.strip() for line in before_output(role_file).splitlines()]
        defined = [index for index, line in enumerate(lines) if INPUT_READER in line]
        assert len(defined) == 1, f"{role_file} must define an input reader on one line"
        definitions[role_file] = lines[defined[0]:defined[0] + 2]
    assert definitions["builder.md"] == definitions["tester.md"], (
        "builder.md and tester.md define an input reader in different words")


def test_the_testers_throwaway_rule_comes_after_everything_step_4_tries():
    """The rule on where a Tester may aim covers the input readers and the
    other edge cases, so it is a line of its own after both, not an item in
    the list about input readers."""
    lines = before_output("tester.md").splitlines()
    rule = [index for index, line in enumerate(lines) if THROWAWAY_TARGETS in line]
    assert len(rule) == 1, "tester.md must say once: throwaway targets only"
    assert lines[rule[0]].startswith(THROWAWAY_TARGETS), (
        "tester.md keeps the throwaway-targets rule inside a list")
    other_edge_cases = [index for index, line in enumerate(lines)
                        if line.startswith(OTHER_EDGE_CASES)]
    next_step = [index for index, line in enumerate(lines)
                 if line.startswith(STEP_AFTER_TRY_TO_BREAK_IT)]
    assert other_edge_cases and next_step, "tester.md has no other edge cases or no step 5"
    assert other_edge_cases[0] < rule[0] < next_step[0], (
        "the throwaway-targets rule must come after the other edge cases, inside step 4")


def test_each_stop_of_secure_has_a_line_of_its_own():
    lines = before_output("security.md").splitlines()
    named = [index for index, line in enumerate(lines) if STOPS_OF_SECURE in line]
    assert len(named) == 1, f"security.md must say once: {STOPS_OF_SECURE}"
    stops = 0
    for line in lines[named[0] + 1:]:
        if not line.startswith("- "):
            break
        stops += 1
    assert stops == WAYS_SECURE_IS_STOPPED, (
        f"security.md gives {stops} stops a line of their own, not {WAYS_SECURE_IS_STOPPED}")


def test_a_throwaway_copy_is_a_safe_target_only_where_no_listed_target_reaches_the_change():
    """Said once in security.md, on the line after the allowlist, with its
    limit on the same line."""
    lines = [line for line in before_output("security.md").splitlines() if line]
    named = [index for index, line in enumerate(lines) if THROWAWAY_COPY in line]
    assert len(named) == 1, "security.md must say once what a throwaway copy counts as"
    assert WHERE_A_THROWAWAY_COPY_COUNTS in lines[named[0]], (
        "a throwaway copy must count as a safe target for one kind of change only")
    assert ALLOWLIST in lines[named[0] - 1], (
        "what a throwaway copy counts as belongs directly after the allowlist")


def test_securitys_cap_is_defined_in_one_place():
    """Only the paragraph that defines the cap says when it applies; the
    verdict rules point to it by name."""
    says_when = [line for line in before_output("security.md").splitlines()
                 if WHEN_THE_CAP_APPLIES in line]
    assert len(says_when) == 1, (
        f"security.md says when the cap applies on {len(says_when)} lines, not one")
    assert says_when[0].startswith(THE_CAP), f"security.md does not define the cap under {THE_CAP}"


@pytest.mark.parametrize("role_file", WORD_CEILINGS)
def test_no_line_of_prose_is_over_the_limit(role_file):
    too_long = [f"{role_file}:{number} ({len(line)} characters)"
                for number, line in lines_of_prose(role_file) if len(line) > LONGEST_LINE_OF_PROSE]
    assert not too_long, (
        f"over {LONGEST_LINE_OF_PROSE} characters, give each rule a line: {', '.join(too_long)}")


@pytest.mark.parametrize("role_file", WORD_CEILINGS)
def test_role_file_is_under_its_word_ceiling(role_file):
    words = len((AGENTS_DIR / role_file).read_text().split())
    assert words <= WORD_CEILINGS[role_file], (
        f"{role_file} has {words} words, over its ceiling of {WORD_CEILINGS[role_file]}: "
        "tighten a passage, or raise the ceiling and say why in context/DECISIONS.md")


def test_every_role_file_has_a_size_limit_or_is_named_as_having_none():
    in_neither_or_both = set(ROLE_FILES) ^ {*WORD_CEILINGS, *ROLE_FILES_WITH_NO_SIZE_LIMIT}
    in_both = set(WORD_CEILINGS) & set(ROLE_FILES_WITH_NO_SIZE_LIMIT)
    assert not in_neither_or_both and not in_both, (
        "give each role file a word ceiling or name it as having no size limit, not both: "
        f"{sorted(in_neither_or_both | in_both)}")


@pytest.mark.parametrize("role_file", RATING_ROLE_FILES)
def test_output_template_has_the_summary_line_and_the_rated_section(role_file):
    """A report written by filling in the template is one the reader takes
    as rated, with its summary line found."""
    import report_reader
    report = report_reader.read_report(output_template(role_file))
    assert report["summary_line"] == "{one sentence, at most 160 characters}"
    assert report["rated"] is True
    assert (report["findings"], report["unreadable_rows"]) == ([], [])


def test_only_security_declares_a_capped_verdict():
    import report_reader
    assert f"{report_reader.CAPPED_PREFIX} yes | no" in output_template("security.md")
    assert f"`{report_reader.CAPPED_PREFIX} yes`" in (AGENTS_DIR / "security.md").read_text()
    for role_file in ("reviewer.md", "tester.md"):
        assert report_reader.CAPPED_PREFIX not in (AGENTS_DIR / role_file).read_text()


# ---------------------------------------------------------------------------
# What the role files say about lows
# ---------------------------------------------------------------------------

# Relay routes on a report's verdict alone and reads nothing else from it
# but what the card shows (report_reader.py). A role file that says Relay
# stops for a mark, a heading or a label tells the role a check exists that
# does not. Each pattern is one wording of such a claim.
CLAIMS_OF_A_CHECK_RELAY_DOES_NOT_MAKE = (
    r"Relay stops", r"stops the task as", r"then a human looks first", r"Relay finds",
    r"Relay's check", r"polish card", r"card can be filed", r"fix-now gate")


@pytest.mark.parametrize("role_file", RATING_ROLE_FILES)
def test_the_low_row_and_definition_say_a_marked_low_moves_on(role_file):
    text = (AGENTS_DIR / role_file).read_text()
    low_rows = [line for line in section(role_file, "## Verdict (exactly one)").splitlines()
                if line.startswith("| low, or no findings |")]
    assert len(low_rows) == 1 and 'even when a low is marked "fix now"' in low_rows[0], (
        f"{role_file}'s verdict table says a low marked fix now stops the task")
    low_definitions = [line for line in text.splitlines() if line.startswith("  - **Low:**")]
    assert len(low_definitions) == 1 and "or stops the task" in low_definitions[0], (
        f"{role_file}'s definition of low does not say a low never stops the task")


@pytest.mark.parametrize("role_file", ROLE_FILES)
def test_no_role_file_claims_a_check_relay_does_not_make(role_file):
    text = " ".join((AGENTS_DIR / role_file).read_text().split())
    claims = [claim for claim in CLAIMS_OF_A_CHECK_RELAY_DOES_NOT_MAKE if re.search(claim, text)]
    assert not claims, f"{role_file} claims a check Relay does not make: {claims}"


# A fix and most features reach the Builder with no Architect's plan
# (sizing.py). Every role that reads the plan says what to do without one, in
# the same words.
NO_PLAN_LINE_PREFIX = "No plan ("
ROLES_THAT_READ_THE_PLAN = ["builder.md", "reviewer.md", "tester.md", "security.md", "ops.md"]


def test_every_role_that_reads_the_plan_says_what_to_do_without_one():
    lines = {role_file: [line for line in section(role_file, READ_FIRST_HEADING).splitlines()
                         if line.startswith(NO_PLAN_LINE_PREFIX)]
             for role_file in ROLES_THAT_READ_THE_PLAN}
    for role_file, found in lines.items():
        assert len(found) == 1, f"{role_file}'s Read first does not say once what to do with no plan"
    assert len({found[0] for found in lines.values()}) == 1, (
        "the roles say in different words what to do with no plan")


def test_the_verdict_each_table_gives_for_a_high_is_one_the_tripwire_counts():
    """Each role's own verdict for a high is in routing.HIGH_VERDICTS."""
    import routing
    for role_file in RATING_ROLE_FILES:
        rows = [[cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
                for line in section(role_file, "## Verdict (exactly one)").splitlines()
                if line.startswith("| ") and "---" not in line][1:]
        in_the_file = {row[0].split(",")[0]: row[1] for row in rows}
        assert (role_file.removesuffix(".md"), in_the_file["high"]) in routing.HIGH_VERDICTS, (
            f"{role_file}'s verdict for a high is not one routing.HIGH_VERDICTS holds")


# ---------------------------------------------------------------------------
# The threat model and the ratings that follow from it
# ---------------------------------------------------------------------------

# Each repo's own threat model, beside its profile. Every role reads the copy
# on the task's base branch; the rules for rating against it are one block,
# word for word, in the four files whose roles rate or answer findings.
THREAT_MODEL_FILE = ".relay/threat-model.md"
RELAYS_THREAT_MODEL = AGENTS_DIR.parent / THREAT_MODEL_FILE
PROFILE_DOC = AGENTS_DIR.parent / "docs" / "profile.md"
READ_FIRST_HEADING = "## Read first"
THE_BASE_GOVERNS = "base branch"
THREAT_MODEL_HEADING = "## Threat model and ratings"
THREAT_MODEL_ROLE_FILES = ["builder.md", "reviewer.md", "tester.md", "security.md"]
# The rules of the block, by the bold label each one starts with.
THREAT_MODEL_RULES = ("The threat model", "The strictest default",
                      "A trusted party attacking themselves", "The bar inside the surface is unchanged",
                      "High for a hole", "An untrue statement", "Outside the threat model",
                      "A finding names its consequence", "Security on the file")
HIGH_FOR_A_HOLE, AN_UNTRUE_STATEMENT = "High for a hole", "An untrue statement"
SECURITY_ON_THE_FILE = "Security on the file"
OUTSIDE_THE_THREAT_MODEL = "Outside the threat model"
# What a high hole is, in the words every definition of one must carry.
HIGH_FOR_A_HOLE_IN_FULL = "an attacker inside the threat model can reach it now and gains something"
# The words each central rating rule cannot lose and still be the rule.
RULES_BY_THEIR_WORDS = [
    (HIGH_FOR_A_HOLE, (HIGH_FOR_A_HOLE_IN_FULL, "reading, changing or running what they could not",
                       "beyond the task's own branch")),
    (AN_UNTRUE_STATEMENT, ("is high only when", "a wrong decision or wrong behaviour",
                           "the finding names that decision", "it is low")),
    (OUTSIDE_THE_THREAT_MODEL, ("is at most low",)),
]
RETIRED_HIGH_FOR_A_HOLE = "can be abused now"
RETIRED_SECURITY_MEDIUM = ("should be fixed but doesn't allow abuse",
                           "a low you would have fixed now is medium")
RETIRED_UNTRUE_ROW = "A row that is untrue is high."
FIX_ROUND_RULE = "**A fix round answers what it was sent for.**"
# A list item of a threat model shorter than this says too little to tell
# one repo's model from another's ("The owner.").
WORDS_THAT_MARK_A_MODEL = 4


def rule_lines(role_file: str) -> dict[str, str]:
    """The block's lines, by the bold label each starts with."""
    lines = section(role_file, THREAT_MODEL_HEADING).splitlines()
    return {label: line for label in THREAT_MODEL_RULES for line in lines
            if line.lstrip("- ").startswith(f"**{label}")}


def shared_bullet(role_file: str, label: str) -> str:
    """The one line of the shared rated-findings section that starts the
    bullet or sub-bullet named `label`."""
    lines = [line for line in section(role_file, RATED_FINDINGS_HEADING).splitlines()
             if line.lstrip().startswith(f"- **{label}:**")]
    assert len(lines) == 1, f"{role_file} has {len(lines)} {label} bullets, not one"
    return lines[0]


def test_relays_threat_model_file_and_its_description():
    """Relay's own model is a file of its own with the two lists, and the
    profile document says what such a file is and which copy governs."""
    model = RELAYS_THREAT_MODEL.read_text()
    assert "\n## Trusted\n" in model and "\n## Attack surface\n" in model
    description = PROFILE_DOC.read_text().split("\n## Threat model\n", 1)
    assert len(description) == 2, "docs/profile.md has no Threat model section"
    said = description[1].split("\n## ", 1)[0]
    for words in (THREAT_MODEL_FILE, "base branch", "strictest default", "When there is none",
                  f"git show {{base}}:{THREAT_MODEL_FILE}"):
        assert words in said, f"docs/profile.md's Threat model section does not say {words!r}"


@pytest.mark.parametrize("role_file", ROLE_FILES)
def test_every_role_reads_the_threat_model_at_the_base(role_file):
    read_first = section(role_file, READ_FIRST_HEADING)
    items = [line for line in read_first.splitlines() if THREAT_MODEL_FILE in line]
    assert len(items) == 1, f"{role_file} does not name {THREAT_MODEL_FILE} once in Read first"
    assert THE_BASE_GOVERNS in items[0], f"{role_file} does not say the base branch's copy governs"


@pytest.mark.parametrize("first, second", list(combinations(THREAT_MODEL_ROLE_FILES, 2)))
def test_the_threat_model_block_is_one_text_in_four_files(first, second):
    assert section(first, THREAT_MODEL_HEADING), f"{first} has no threat model block"
    assert section(first, THREAT_MODEL_HEADING) == section(second, THREAT_MODEL_HEADING), (
        f"{first} and {second} differ in the threat model block")
    for role_file in (first, second):
        text = (AGENTS_DIR / role_file).read_text()
        assert text.index(f"{THREAT_MODEL_HEADING}\n") < text.index(SHARED_RULES)


@pytest.mark.parametrize("role_file", ROLE_FILES)
def test_only_the_four_files_hold_the_block(role_file):
    holds_it = THREAT_MODEL_HEADING in (AGENTS_DIR / role_file).read_text()
    assert holds_it == (role_file in THREAT_MODEL_ROLE_FILES), role_file


def test_the_block_holds_each_rule_by_name():
    found = rule_lines("security.md")
    missing = [label for label in THREAT_MODEL_RULES if label not in found]
    assert not missing, f"the threat model block has no rule named {missing}"


@pytest.mark.parametrize("label, words", RULES_BY_THEIR_WORDS)
def test_each_rating_rule_says_what_it_rates(label, words):
    """A rule held by its label alone could lose the words that make it a
    rule, in all four copies at once, and the equality test would still pass."""
    rule = rule_lines("security.md")[label]
    for word in words:
        assert word in rule, f"{label} does not say {word!r}"


@pytest.mark.parametrize("role_file", THREAT_MODEL_ROLE_FILES)
def test_every_definition_of_high_for_a_hole_is_the_full_one(role_file):
    """A role file that defines a high hole twice must not let one copy drop
    a part of it, such as what the attacker gains."""
    definitions = [line for line in (AGENTS_DIR / role_file).read_text().splitlines()
                   if line.startswith("- **High for ")]
    assert definitions, f"{role_file} defines no high hole"
    for line in definitions:
        assert HIGH_FOR_A_HOLE_IN_FULL in line, (
            f"{role_file} defines a high hole without {HIGH_FOR_A_HOLE_IN_FULL!r}: {line}")


def test_security_on_the_file_rates_a_missing_file_and_a_loosening_change():
    """A missing file: low, Record, outside the model. A change on the
    branch: medium when it loosens the model, low when it only tightens or
    rewords it."""
    rule = rule_lines("security.md")[SECURITY_ON_THE_FILE]
    for words in ("no threat-model file", "low, marked `Record`", "`no` to",
                  "medium when it narrows the attack surface or widens what is trusted",
                  "low when it only makes the model stricter or only rewords it"):
        assert words in rule, f"Security on the file does not say {words!r}"


def test_the_strictest_default_and_the_block_hold_no_repos_own_model():
    """A repo's own trusted list and attack surface live in its file, never
    in a role file: no item of Relay's own model is pasted into one."""
    items = [line.lstrip("- ").strip() for line in RELAYS_THREAT_MODEL.read_text().splitlines()
             if line.startswith("- ")]
    telling = [item for item in items if len(item.split()) >= WORDS_THAT_MARK_A_MODEL]
    assert telling, "Relay's threat model has no item to look for"
    for role_file in ROLE_FILES:
        text = (AGENTS_DIR / role_file).read_text()
        pasted = [item for item in telling if item in text]
        assert not pasted, f"{role_file} holds Relay's own threat model: {pasted}"


@pytest.mark.parametrize("role_file", RATING_ROLE_FILES)
def test_the_shared_high_points_to_the_threat_model_rules(role_file):
    high = shared_bullet(role_file, "High")
    for rule in (HIGH_FOR_A_HOLE, AN_UNTRUE_STATEMENT):
        assert rule in high, f"{role_file}'s High does not point to {rule!r}"
    assert RETIRED_HIGH_FOR_A_HOLE not in (AGENTS_DIR / role_file).read_text()
    low = shared_bullet(role_file, "Low")
    assert "outside the threat model" in low and "consequence" in low, (
        f"{role_file}'s Low does not hold a finding outside the model to low")


def test_securitys_medium_is_a_gain_nothing_reaches_now():
    text = (AGENTS_DIR / "security.md").read_text()
    for retired in RETIRED_SECURITY_MEDIUM:
        assert retired not in text, f"security.md still says {retired!r}"
    medium = [line for line in text.splitlines() if line.startswith("- **Medium for Security:**")]
    assert len(medium) == 1 and "threat-model file" in medium[0] and "nothing can reach now" in medium[0]


def test_the_reviewers_untrue_row_is_rated_by_the_shared_rule():
    text = (AGENTS_DIR / "reviewer.md").read_text()
    assert RETIRED_UNTRUE_ROW not in text
    rows = section("reviewer.md", "## What to check").split("**Known-flaw rows.**", 1)[1]
    first_rule = rows.split("\n  - ", 2)[1]
    assert AN_UNTRUE_STATEMENT in first_rule, (
        "reviewer.md's known-flaw rule does not rate an untrue row by the shared rule")


def test_a_fix_round_answers_what_it_was_sent_for():
    lines = [line for line in before_output("builder.md").splitlines() if FIX_ROUND_RULE in line]
    assert len(lines) == 1, f"builder.md must say once: {FIX_ROUND_RULE}"
    rule = before_output("builder.md").split(FIX_ROUND_RULE, 1)[1].split("\n- **", 1)[0]
    for words in ("high and medium findings", "send-back note asks for", "recorded lows",
                  "a round that is running anyway", "No round runs just to write a row"):
        assert words in rule, f"the fix-round rule does not say {words!r}"


@pytest.mark.parametrize("role_file", RATING_ROLE_FILES)
def test_record_is_the_default_mark(role_file):
    record = shared_bullet(role_file, "Record")
    fix_now = shared_bullet(role_file, "Fix now")
    assert "the default" in record, f"{role_file} does not make Record the default for a low"
    assert "override" in fix_now and "reason" in fix_now, (
        f"{role_file}: Fix now does not ask the owner to override, with a reason")


