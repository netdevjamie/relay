"""The sizing hint (risk_hint.py): which risk categories the paths a branch
changed look like they touch. A pointer for the Reviewer, never a rule.

Every repo here is a throwaway one in tmp_path. Changed paths are text from
the branch, so hostile names are fed in too: they must come back as plain
text, and nothing the branch writes may change what the hint reads."""

from pathlib import Path

import pytest

import project_profile
import risk_hint
from tests.git_fixtures import commit_all, commit_of, git, repo, write_profile  # noqa: F401  (a fixture)

REPO_ROOT = Path(__file__).resolve().parents[2]


def on_a_branch(repo_path: Path) -> Path:
    git(repo_path, "switch", "-q", "-c", "task")
    return repo_path


def add(repo_path: Path, relative: str, text: str = "x\n") -> None:
    path = repo_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def categories(hint: dict) -> dict:
    return {found["category"]: found["paths"] for found in hint["categories"]}


# ---------------------------------------------------------------------------
# What the branch changed
# ---------------------------------------------------------------------------

def test_the_hint_covers_the_whole_branch_not_the_last_round(repo):
    on_a_branch(repo)
    add(repo, "auth/login.py")
    commit_all(repo, "round 1")
    add(repo, "README.md", "words\n")
    commit_all(repo, "round 2")
    hint = risk_hint.work_out(repo, commit_of(repo))
    assert hint["available"] and hint["changed"] == 2
    assert categories(hint) == {"auth": ["auth/login.py"]}
    assert risk_hint.summary(hint) == "risk hint: auth 1 path"


@pytest.mark.parametrize("category, path", [
    ("auth", "app/session_store.py"),
    ("input", "lib/request_parser.ts"),
    ("payments", "billing/invoices.rb"),
    ("schema", "db/migrations/0002_add.py"),
    ("production_data", "scripts/backfill_users.sh"),
    ("secrets", "config/.env.production"),
    ("deploy", ".github/workflows/release.yml"),
])
def test_every_category_matches_its_conventional_names(repo, category, path):
    on_a_branch(repo)
    add(repo, path)
    commit_all(repo)
    assert category in categories(risk_hint.work_out(repo, commit_of(repo)))


def test_a_diff_matching_nothing_says_so(repo):
    on_a_branch(repo)
    add(repo, "docs/guide.md")
    commit_all(repo)
    hint = risk_hint.work_out(repo, commit_of(repo))
    assert hint["available"] and hint["categories"] == []
    assert risk_hint.summary(hint) == "risk hint: nothing matched"
    assert not risk_hint.matched(hint)


def test_a_renamed_file_counts_by_both_names(repo):
    add(repo, "auth.py")
    commit_all(repo, "on main")
    on_a_branch(repo)
    git(repo, "mv", "auth.py", "gate.py")
    commit_all(repo, "rename")
    hint = risk_hint.work_out(repo, commit_of(repo))
    assert hint["changed"] == 2
    assert categories(hint) == {"auth": ["auth.py"]}


def test_paths_beyond_the_cap_are_counted_not_kept(repo):
    on_a_branch(repo)
    for number in range(risk_hint.MOST_PATHS_KEPT + 5):
        add(repo, f"tokens/t{number:02d}.txt")
    commit_all(repo)
    found = risk_hint.work_out(repo, commit_of(repo))["categories"][0]
    assert found["count"] == risk_hint.MOST_PATHS_KEPT + 5
    assert len(found["paths"]) == risk_hint.MOST_PATHS_KEPT


def test_a_far_too_long_path_is_cut(repo):
    on_a_branch(repo)
    name = "auth_" + "a" * 240
    add(repo, f"{name}/{name}.py")
    commit_all(repo)
    found = risk_hint.work_out(repo, commit_of(repo))["categories"][0]
    assert len(found["paths"][0]) == risk_hint.MOST_PATH_CHARACTERS


def test_pathspec_magic_names_are_plain_text(repo):
    on_a_branch(repo)
    add(repo, ":(glob)*token*")
    add(repo, "-p --output=x")
    commit_all(repo)
    hint = risk_hint.work_out(repo, commit_of(repo))
    assert hint["changed"] == 2
    assert categories(hint) == {"auth": [":(glob)*token*"]}


def test_a_name_with_control_characters_and_odd_bytes_comes_back_as_text(repo):
    on_a_branch(repo)
    add(repo, "auth\x1b[31mred.py")
    add(repo, "secret_é.txt")
    commit_all(repo)
    found = categories(risk_hint.work_out(repo, commit_of(repo)))
    assert found["auth"] == ["auth\x1b[31mred.py"]
    assert found["secrets"] == ["secret_é.txt"]


# ---------------------------------------------------------------------------
# The defaults and the profile's additions
# ---------------------------------------------------------------------------

def test_matching_is_by_folder_or_file_name_in_any_case_and_slash_patterns_by_path():
    assert risk_hint.matches("src/Auth/Views.py", "*auth*")
    assert risk_hint.matches("PROD/config.yml", "prod")
    assert not risk_hint.matches("product/config.yml", "prod")
    assert risk_hint.matches(".github/workflows/a/b.yml", ".github/workflows/*")
    assert not risk_hint.matches("x/.github/workflows/b.yml", ".github/workflows/*")


def test_defaults_name_no_project():
    for category, patterns in risk_hint.DEFAULT_PATTERNS.items():
        assert category in project_profile.RISK_CATEGORIES
        for pattern in patterns:
            lowered = pattern.lower()
            assert not any(name in lowered for name in ("relay", "orchestrator", "example-api",
                                                        "my-app")), pattern
            if "/" in pattern:
                assert pattern.startswith((".github/", ".gitlab")), pattern
    assert set(risk_hint.DEFAULT_PATTERNS) == set(project_profile.RISK_CATEGORIES)


def test_profile_doc_says_where_the_defaults_live():
    doc = (REPO_ROOT / "docs" / "profile.md").read_text()
    assert "risk_hint.py" in doc and "DEFAULT_PATTERNS" in doc and "risk_paths" in doc
    for category in project_profile.RISK_CATEGORIES:
        assert f"`{category}`" in doc, f"docs/profile.md does not name the category {category}"


def test_a_base_profile_pattern_applies_and_a_branch_one_does_not(repo):
    write_profile(repo, "echo all good", 'risk_paths:\n  auth: ["gatekeeper_*"]\n')
    commit_all(repo, "the base adds a pattern")
    on_a_branch(repo)
    add(repo, "gatekeeper_x.py")
    commit_all(repo)
    assert categories(risk_hint.work_out(repo, commit_of(repo))) == {"auth": ["gatekeeper_x.py"]}


def test_a_pattern_the_branch_adds_to_its_own_profile_does_not_apply(repo):
    on_a_branch(repo)
    write_profile(repo, "echo all good", 'risk_paths:\n  payments: ["ledger_*"]\n')
    add(repo, "ledger_book.py")
    commit_all(repo)
    found = categories(risk_hint.work_out(repo, commit_of(repo)))
    assert "payments" not in found
    # The profile file itself is a changed path, matched by nothing here.
    assert found == {}


def test_a_profile_cannot_remove_a_default(repo):
    write_profile(repo, "echo all good", "risk_paths:\n  auth: []\n")
    commit_all(repo, "the base tries to empty auth")
    on_a_branch(repo)
    add(repo, "login.py")
    commit_all(repo)
    hint = risk_hint.work_out(repo, commit_of(repo))
    assert categories(hint) == {"auth": ["login.py"]}
    assert "risk_paths could not be used" in hint["note"]


@pytest.mark.parametrize("risk_paths", [
    "risk_paths: [auth]\n",
    'risk_paths:\n  everything: ["*"]\n',
    'risk_paths:\n  auth: "gate*"\n',
    "risk_paths:\n  auth: [1, 2]\n",
    'risk_paths:\n  auth: ["' + "a" * 201 + '"]\n',
    'risk_paths:\n  auth: ["bad\\nline"]\n',
    "risk_paths:\n  auth: [" + ", ".join(f'"p{n}"' for n in range(51)) + "]\n",
])
def test_a_base_profile_with_unusable_risk_paths_adds_nothing_and_says_so(repo, risk_paths):
    write_profile(repo, "echo all good", risk_paths)
    commit_all(repo, "the base's risk_paths are wrong")
    on_a_branch(repo)
    add(repo, "login.py")
    commit_all(repo)
    hint = risk_hint.work_out(repo, commit_of(repo))
    assert hint["available"] and categories(hint) == {"auth": ["login.py"]}
    assert hint["note"].startswith("the base branch's risk_paths could not be used")


def test_an_unreadable_base_profile_still_uses_the_defaults_and_says_so(repo):
    (repo / ".relay" / "profile.yaml").write_text("commands: [unclosed\n")
    commit_all(repo, "a broken profile on the base")
    on_a_branch(repo)
    add(repo, "payments/refund.py")
    commit_all(repo)
    hint = risk_hint.work_out(repo, commit_of(repo))
    assert categories(hint) == {"payments": ["payments/refund.py"]}
    assert hint["note"].startswith("The base branch's profile could not be read")


# ---------------------------------------------------------------------------
# When there is no hint
# ---------------------------------------------------------------------------

def test_each_unavailable_case_gives_one_line_and_no_categories(repo, tmp_path):
    not_git = tmp_path / "plain"
    not_git.mkdir()
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    git(orphan, "init", "-q", "-b", "main")
    add(orphan, "a.txt")
    commit_all(orphan)
    cases = [
        (risk_hint.work_out(None, commit_of(repo)), risk_hint.NO_WORKTREE),
        (risk_hint.work_out(tmp_path / "missing", commit_of(repo)), risk_hint.NO_WORKTREE),
        (risk_hint.work_out(not_git, commit_of(repo)), risk_hint.NOT_A_CHECKOUT),
        (risk_hint.work_out(repo, None, has_base=False), risk_hint.NO_BASE),
        (risk_hint.work_out(repo, None), risk_hint.NO_TRUSTED_BASE),
        # A branch name is never looked up: only a confirmed commit id is used.
        (risk_hint.work_out(repo, "main"), risk_hint.NO_TRUSTED_BASE),
        (risk_hint.work_out(repo, commit_of(orphan)), risk_hint.NO_FORK),
    ]
    for hint, reason in cases:
        assert (hint["available"], hint["reason"], hint["categories"]) == (False, reason, [])
        assert risk_hint.summary(hint) == f"risk hint: not available ({reason})"
        assert "\n" not in risk_hint.summary(hint)


def test_git_failing_to_list_the_changes_is_not_read_as_nothing_changed(repo, monkeypatch):
    on_a_branch(repo)
    add(repo, "auth.py")
    commit_all(repo)
    monkeypatch.setattr(risk_hint, "changed_paths", lambda worktree, fork: None)
    hint = risk_hint.work_out(repo, commit_of(repo))
    assert (hint["available"], hint["reason"]) == (False, risk_hint.NO_DIFF)
