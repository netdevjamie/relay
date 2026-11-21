"""The sizing hint: which risk categories the paths a branch changed look
like they touch.

After each Builder round Relay lists the files the task's branch changed
since it left its base (the whole branch, not just the last round) and
matches each path against general naming conventions for seven risk
categories: auth, input handling, money or payments, migrations or schema,
production data, secrets and deploy config. The result is a pointer for the
Reviewer, who judges the diff itself; it never changes routing by itself.

The defaults are general conventions only. A repo's profile may add
patterns through `risk_paths`, read from the profile at the base commit
Relay confirmed for the task (base_trust.py; never the branch's own
profile), and it can only add: nothing removes a default. The base commit
only moves forward, so a branch cannot narrow or widen its own hint. The
changed paths are counted from the fork point, the merge base of that
commit and HEAD.

Changed paths are text from the branch. They are given to git as nothing
(the diff names only two commits) and are only ever compared here as text.
Whoever shows them passes them through printable().
"""
from __future__ import annotations

import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

import evidence
import project_profile
from project_profile import RISK_CATEGORIES, RISK_CATEGORY_WORDS

# General naming conventions, matched in any case. A pattern without `/`
# is matched against each folder name and the file name of a path; one with
# `/` against the whole path, where `*` also crosses folders.
DEFAULT_PATTERNS: dict[str, tuple[str, ...]] = {
    "auth": ("*auth*", "*login*", "*logout*", "*signin*", "*sign_in*", "*signup*", "*session*",
             "*password*", "*oauth*", "*jwt*", "*token*", "*permission*", "*rbac*", "*credential*"),
    "input": ("*parse*", "*parser*", "*valid*", "*sanitiz*", "*serializ*", "*upload*", "*webhook*",
              "*request*", "*input*"),
    "payments": ("*payment*", "*billing*", "*invoice*", "*subscription*", "*pricing*", "*refund*",
                 "*purchase*", "*wallet*", "*currency*", "*stripe*", "*paypal*"),
    "schema": ("*migrat*", "*schema*", "*.sql", "alembic", "liquibase"),
    "production_data": ("*production*", "prod", "prod.*", "*.prod.*", "*backfill*", "*backup*",
                        "*restore*", "*dump*", "*purge*", "*retention*"),
    "secrets": ("*secret*", "*credential*", ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
                "*.jks", "*keychain*", "*keystore*", "*vault*", "id_rsa*", "*apikey*", "*api_key*"),
    "deploy": ("dockerfile*", "*.dockerfile", "docker-compose*", "compose.y*ml", "fly.toml",
               "vercel.json", "netlify.toml", "procfile", ".github/workflows/*", ".gitlab-ci.yml",
               "*.tf", "*.tfvars", "terraform", "helm", "k8s", "kubernetes", "ansible", "*deploy*",
               "serverless.y*ml", "cloudbuild.y*ml"),
}

# The most matched paths a hint keeps for one category (the count is of
# all of them), and the most characters of one path it keeps.
MOST_PATHS_KEPT = 20
MOST_PATH_CHARACTERS = 300

NO_WORKTREE = "the task has no working directory"
NOT_A_CHECKOUT = "the working directory is not a git checkout"
NO_BASE = "the task has no base branch"
NO_TRUSTED_BASE = "Relay has no confirmed commit for the task's base"
NO_FORK = "Relay could not find where the branch left its base"
NO_DIFF = "git could not list the changed files"


def matches(path: str, pattern: str) -> bool:
    """True when the path matches the pattern, in any case."""
    path, pattern = path.lower(), pattern.lower()
    if "/" in pattern:
        return fnmatchcase(path, pattern)
    return any(fnmatchcase(segment, pattern) for segment in path.split("/") if segment)


def profile_additions(base_data) -> tuple[dict[str, tuple[str, ...]], str | None]:
    """(the patterns the base profile adds, by category; why they could not
    be used). A base profile whose `risk_paths` does not pass the profile
    check adds nothing, and the note says why: the defaults still apply."""
    if not isinstance(base_data, dict) or "risk_paths" not in base_data:
        return {}, None
    errors: list[str] = []
    project_profile.check_risk_paths(base_data, errors)
    if errors:
        return {}, f"the base branch's risk_paths could not be used: {errors[0]}"
    return {category: tuple(patterns) for category, patterns in base_data["risk_paths"].items()}, None


def patterns_for(additions: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """The defaults with the profile's additions after them, by category."""
    return {category: DEFAULT_PATTERNS[category] + additions.get(category, ())
            for category in RISK_CATEGORIES}


def changed_paths(worktree: Path, fork: str) -> list[str] | None:
    """The paths changed between the fork and HEAD (a renamed file under
    both names), or None when git cannot say: an empty list would read as
    "nothing changed"."""
    done = subprocess.run(["git", "-c", "core.quotepath=off", "diff", "--name-only", "--no-renames",
                           "-z", fork, "HEAD"], cwd=worktree, capture_output=True)
    if done.returncode != 0:
        return None
    return [entry.decode("utf-8", errors="replace") for entry in done.stdout.split(b"\0") if entry]


def categorized(paths: list[str], patterns: dict[str, tuple[str, ...]]) -> list[dict]:
    """The categories the paths match, in RISK_CATEGORIES order: each
    {category, count, paths}, keeping at most MOST_PATHS_KEPT paths, each
    cut at MOST_PATH_CHARACTERS."""
    found = []
    for category in RISK_CATEGORIES:
        matched = [path for path in paths
                   if any(matches(path, pattern) for pattern in patterns[category])]
        if matched:
            found.append({"category": category, "count": len(matched),
                          "paths": [path[:MOST_PATH_CHARACTERS] for path in matched[:MOST_PATHS_KEPT]]})
    return found


def _unavailable(reason: str, commit: str | None = None) -> dict:
    return {"available": False, "reason": reason, "note": None, "fork": None, "commit": commit,
            "changed": 0, "categories": []}


def work_out(worktree: Path | None, base_commit: str | None, has_base: bool = True) -> dict:
    """The hint for a task's checkout and its base commit (the full commit
    id Relay confirmed; `has_base` says whether the task has a base at
    all, so a base with no confirmed commit is told apart). Never raises.

    {"available", "reason", "note", "fork", "commit", "changed",
    "categories"}: `reason` says in one line why it is not available;
    `note` why the base profile's additions were not used (the defaults
    still were); `changed` is how many paths the branch changed."""
    if worktree is None or not Path(worktree).is_dir():
        return _unavailable(NO_WORKTREE)
    worktree = Path(worktree)
    commit = evidence.head_commit(worktree)
    if commit is None:
        return _unavailable(NOT_A_CHECKOUT)
    if not has_base:
        return _unavailable(NO_BASE, commit)
    if not evidence.is_full_commit_id(base_commit):
        return _unavailable(NO_TRUSTED_BASE, commit)
    fork = evidence.fork_point(worktree, base_commit)
    if fork is None:
        return _unavailable(NO_FORK, commit)
    paths = changed_paths(worktree, fork)
    if paths is None:
        return _unavailable(NO_DIFF, commit)
    base_data, refused = evidence.profile_at(worktree, base_commit)
    additions, note = profile_additions(base_data)
    if refused:
        note = f"{evidence.BASE_PROFILE_UNREAD}: {refused}"
    return {"available": True, "reason": None, "note": note, "fork": fork, "commit": commit,
            "changed": len(paths), "categories": categorized(paths, patterns_for(additions))}


def paths_in_words(count: int) -> str:
    return "1 path" if count == 1 else f"{count} paths"


def summary(hint: dict | None) -> str:
    """The hint in one line of Relay's words, with counts and no paths."""
    if not hint:
        return "risk hint: none recorded"
    if not hint.get("available"):
        return f"risk hint: not available ({hint.get('reason')})"
    categories = hint.get("categories") or []
    if not categories:
        return "risk hint: nothing matched"
    return "risk hint: " + ", ".join(
        f"{RISK_CATEGORY_WORDS.get(found['category'], found['category'])} "
        f"{paths_in_words(found['count'])}" for found in categories)


def matched(hint: dict | None) -> bool:
    """True when the hint was worked out and matched at least one category."""
    return bool(hint and hint.get("available") and hint.get("categories"))
