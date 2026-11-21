"""Which files make up a repo's test harness, and which tests a branch changed.

A branch can keep the profile's test command word for word and change what
it runs: the script it calls, or the test runner's configuration. Those
files are the harness. Which files count is decided only from the commit
the branch left its base at (the fork point) and the profile at the base's
tip, never from the branch, so a branch cannot take a file off the list.

The rules, any of which makes a path harness:
  (a) the base's test command names it (relative to the repo root or to a
      folder the command cd's into), and it is not a folder at the fork;
  (b) the command runs a tool that reads it: package.json for npm, npx,
      yarn and pnpm, the Makefile for make;
  (c) its file name is a common test runner's configuration
      (DEFAULT_RUNNER_CONFIG). CI configuration is not harness: Relay's
      run does not execute it;
  (d) it matches the base profile's `test_harness` list;
  (e) it is a module that would be imported in place of the one the
      command runs with `python -m`: `X.py` or anything under `X/` in a
      folder the command runs from. Only that module: one it imports in
      turn (pytest's `_pytest`, say) is not covered;
  (f) it is `.gitmodules`, or a submodule (a gitlink) the branch added,
      removed or moved to another commit: the run loads files from inside
      it, and the diff lists it only as one path.

Names are compared in any case: the Mac's disk ignores case, so pytest
looking for pytest.ini reads a branch's PYTEST.INI, and a command naming
scripts/run-tests.sh runs SCRIPTS/RUN-TESTS.SH.

Paths from a branch are only ever compared here as text; git is given none
of them.
"""
from __future__ import annotations

import hashlib
import posixpath
import shlex
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Callable

# Test runner configuration, by file name (rule c).
DEFAULT_RUNNER_CONFIG = (
    "conftest.py", "pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "tox.ini", "setup.cfg",
    "pyproject.toml", "noxfile.py",
    "jest.config.*", "vitest.config.*", "vitest.workspace.*", "karma.conf.*", ".mocharc*",
    "playwright.config.*", "cypress.config.*", "phpunit.xml", "phpunit.xml.dist", "*.runsettings",
    "Directory.Build.props", "Directory.Build.targets",
)
# Files a tool reads when the command runs it (rule b).
TOOL_FILES = {
    "npm": ("package.json",), "npx": ("package.json",), "yarn": ("package.json",),
    "pnpm": ("package.json",), "make": ("Makefile", "makefile", "GNUmakefile"),
}
# Test files, by a folder on their path or by their file name.
TEST_FOLDERS = {"test", "tests", "__tests__", "spec", "specs"}
TEST_FILE_NAMES = (
    "test_*.py", "*_test.py", "*_test.go", "*.test.*", "*.spec.*", "*Test.java", "*Tests.java",
    "*Test.kt", "*Tests.cs", "*Test.cs", "*_spec.rb",
)
# The most entries of the base profile's `test_harness` list Relay reads.
MAX_LISTED_PATTERNS = 200
# The most changed harness paths a run stores; the digest covers them all.
MAX_STORED_PATHS = 50
# The most top-level ignored paths a run notes.
MAX_IGNORED_NOTED = 100
_GLOB_CHARACTERS = set("*?[")

# A submodule's mode in git's trees (rule f).
GITLINK_MODE = "160000"
SUBMODULES_FILE = ".gitmodules"

IsHarness = Callable[[str], bool]


class HarnessUnreadable(Exception):
    """Git could not list the files the branch changed against its fork."""


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=worktree, capture_output=True)


def _nul_separated(output: bytes) -> list[str]:
    return [entry.decode("utf-8", errors="surrogateescape") for entry in output.split(b"\0") if entry]


def command_tokens(command: str) -> list[str]:
    """The words of a shell command, with &&, ; and | as words of their own.
    A word holding spaces (sh -c "...") is split once more. A command shlex
    cannot read is split on spaces."""
    def split(text: str) -> list[str]:
        try:
            lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            return list(lexer)
        except ValueError:
            return text.split()

    tokens = []
    for token in split(command):
        tokens.extend(split(token) if any(space in token for space in " \t") else [token])
    return tokens


def _run_folders(tokens: list[str]) -> list[str]:
    """The root ("") and each folder the command cd's into."""
    return [""] + [tokens[index + 1] for index, token in enumerate(tokens[:-1]) if token == "cd"]


def _inside_the_repo(folder: str, word: str) -> str | None:
    """`word` read from `folder` as a repo-relative path, or None when it
    leaves the repo or is the root itself."""
    path = posixpath.normpath(posixpath.join(folder, word))
    if posixpath.isabs(path) or path in (".", "..") or path.startswith("../"):
        return None
    return path


def _named_paths(tokens: list[str]) -> set[str]:
    """Every repo-relative path the command's words could name, read from
    the root and from each folder the command cd's into."""
    words = []
    for token in tokens:
        if token.startswith("-"):
            if "=" not in token:
                continue
            token = token.split("=", 1)[1]
        if not token or _GLOB_CHARACTERS & set(token):
            continue
        words.append(token)
    for tool, files in TOOL_FILES.items():
        if tool in tokens:
            words.extend(files)
    paths = {_inside_the_repo(folder, word) for folder in _run_folders(tokens) for word in words}
    return paths - {None}


def _shadowing_modules(tokens: list[str]) -> set[str]:
    """Repo-relative module paths, without `.py`, that Python would import
    in place of each module the command runs with `-m` (rule e): the
    module's top-level name in each folder the command runs from."""
    modules = {tokens[index + 1].split(".")[0] for index, token in enumerate(tokens[:-1]) if token == "-m"}
    paths = {_inside_the_repo(folder, module) for folder in _run_folders(tokens) for module in modules if module}
    return paths - {None}


def _shadows(path: str, modules: set[str]) -> bool:
    """True when `path` is `M.py` or lies under the folder `M/` for one of
    the module paths."""
    return (path.endswith(".py") and path[:-3] in modules) or any(
        path.startswith(module + "/") for module in modules)


def _folders_at(worktree: Path, commit: str) -> set[str]:
    listed = _git(worktree, "ls-tree", "-r", "-d", "-z", "--name-only", commit)
    return set(_nul_separated(listed.stdout)) if listed.returncode == 0 else set()


def listed_patterns(base_data) -> list[str]:
    """The base profile's `test_harness` entries that are text, up to the
    limit. The base's profile is read without the checks a branch's gets,
    so anything else in the list is passed over."""
    listed = base_data.get("test_harness") if isinstance(base_data, dict) else None
    if not isinstance(listed, list):
        return []
    return [entry for entry in listed if isinstance(entry, str) and entry.strip()][:MAX_LISTED_PATTERNS]


def _matches_listed(path: str, pattern: str) -> bool:
    """An entry with no slash matches a file name anywhere; one with a slash
    matches the whole path, where * also crosses folders. Both are given
    casefolded."""
    if "/" in pattern:
        return fnmatchcase(path, pattern.strip("/"))
    return fnmatchcase(posixpath.basename(path), pattern)


def harness_rule(worktree: Path, fork: str, base_command: str | None, base_data) -> IsHarness:
    """The test "is this path part of the harness?", decided from the fork
    commit and the base's profile only."""
    folders = _folders_at(worktree, fork)
    tokens = command_tokens(base_command or "")
    # Everything is casefolded, both sides, so a name matches in any case.
    named = {path.casefold() for path in _named_paths(tokens) if path not in folders}
    shadowing = {module.casefold() for module in _shadowing_modules(tokens)}
    runner_config = [pattern.casefold() for pattern in DEFAULT_RUNNER_CONFIG]
    patterns = [pattern.casefold() for pattern in listed_patterns(base_data)]

    def is_harness(path: str) -> bool:
        folded = path.casefold()
        name = posixpath.basename(folded)
        return (folded == SUBMODULES_FILE
                or folded in named
                or any(fnmatchcase(name, pattern) for pattern in runner_config)
                or any(_matches_listed(folded, pattern) for pattern in patterns)
                or _shadows(folded, shadowing))

    return is_harness


# --no-renames: a renamed file shows as its old path deleted and its new path
# added, so a harness file renamed away is still seen.
# --ignore-submodules=none: without it git leaves out a submodule that a
# .gitmodules, the branch's or the base's, marks `ignore = all`, so a move of
# that submodule would be no change at all.
DIFF_EVERYTHING = ("--no-renames", "--ignore-submodules=none", "-z")


def _changed_paths(worktree: Path, fork: str, head: str, *filters: str) -> list[str]:
    """The paths changed between the fork and `head`. Raises
    HarnessUnreadable when git cannot say: an empty list would read as
    "nothing changed"."""
    changed = _git(worktree, "diff", *DIFF_EVERYTHING, "--name-only", *filters, fork, head)
    if changed.returncode != 0:
        raise HarnessUnreadable
    return _nul_separated(changed.stdout)


def _changed_submodules(worktree: Path, fork: str, head: str) -> set[str]:
    """The submodule paths changed between the fork and `head`: a gitlink
    on either side of the change (rule f). Raises HarnessUnreadable when
    git cannot say."""
    changed = _git(worktree, "diff", *DIFF_EVERYTHING, "--raw", fork, head)
    if changed.returncode != 0:
        raise HarnessUnreadable
    # Each change is ":old-mode new-mode old-object new-object status", then its path.
    fields = _nul_separated(changed.stdout)
    return {path for details, path in zip(fields[::2], fields[1::2])
            if GITLINK_MODE in details.lstrip(":").split(" ")[:2]}


def _entries_at(worktree: Path, commit: str) -> dict[str, str]:
    """{path: "mode object"} for every file at the commit. The whole tree is
    listed and filtered here, so no branch path is ever given to git."""
    listed = _git(worktree, "ls-tree", "-r", "-z", commit)
    entries = {}
    for line in _nul_separated(listed.stdout) if listed.returncode == 0 else []:
        details, _, path = line.partition("\t")
        mode, _kind, objectname = details.split(" ", 2)
        entries[path] = f"{mode} {objectname}"
    return entries


def changed_harness(worktree: Path, fork: str, head: str, is_harness: IsHarness) -> tuple[list[str], str | None]:
    """(harness paths the branch changed against the fork, at most
    MAX_STORED_PATHS of them; a digest of all of them as they are at
    `head`), or ([], None) when none changed. The digest is what an owner's
    acceptance covers: a further change to a harness file changes it."""
    submodules = _changed_submodules(worktree, fork, head)
    paths = sorted(path for path in _changed_paths(worktree, fork, head)
                   if is_harness(path) or path in submodules)
    if not paths:
        return [], None
    at_head = _entries_at(worktree, head)
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.encode("utf-8", errors="surrogateescape") + b"\0"
                      + at_head.get(path, "deleted").encode("ascii") + b"\n")
    return paths[:MAX_STORED_PATHS], digest.hexdigest()


def is_test_file(path: str) -> bool:
    folders = path.split("/")[:-1]
    name = posixpath.basename(path)
    return bool(TEST_FOLDERS & set(folders)) or any(fnmatchcase(name, pattern) for pattern in TEST_FILE_NAMES)


def changed_test_files(worktree: Path, fork: str, head: str, is_harness: IsHarness) -> list[str]:
    """Test files the branch added or changed against the fork, harness
    files and submodules left out."""
    submodules = _changed_submodules(worktree, fork, head)
    return sorted(path for path in _changed_paths(worktree, fork, head, "--diff-filter=AM")
                  if is_test_file(path) and not is_harness(path) and path not in submodules)


def ignored_top_level(worktree: Path) -> list[str]:
    """The top-level ignored paths present in the worktree, a folder ending
    in "/", sorted, at most MAX_IGNORED_NOTED."""
    listed = _git(worktree, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory")
    if listed.returncode != 0:
        return []
    top = set()
    for entry in _nul_separated(listed.stdout):
        first, slash, _rest = entry.partition("/")
        top.add(first + slash)
    return sorted(top)[:MAX_IGNORED_NOTED]
