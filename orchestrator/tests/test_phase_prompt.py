"""What the prompt Relay prints for a phase says about rating findings.

The role file is the one home of the rating scale and the verdict rule.
The printed prompt carries one sentence for the phases that rate findings,
in the role file's own words, and nothing for any other phase.
"""
import re
import subprocess
from pathlib import Path

import pytest

import agents
import base_trust
import role_context
import report_reader
import settings
from tests.test_agent_prompts import RATING_ROLE_FILES, ROLE_FILES, defined_ratings

RATING_PHASES = [role_file.removesuffix(".md") for role_file in RATING_ROLE_FILES]
OTHER_PHASES = [role_file.removesuffix(".md") for role_file in ROLE_FILES
                if role_file not in RATING_ROLE_FILES]
# Words other scales use for a rating. None of them may reach a role through
# the prompt, where it would read as a second scale.
OTHER_RATING_WORDS = ("nit", "nits", "info", "blocker", "critical", "warning", "concern")


def printed_prompt(phase: str, tasks_dir, monkeypatch) -> str:
    monkeypatch.setattr(settings, "TASKS_DIR", tasks_dir)
    return agents.build_prompt(phase, "a-task", 4, f"04-{phase}.md")


def whole_words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


@pytest.mark.parametrize("phase", RATING_PHASES)
def test_rating_phase_is_told_the_three_ratings(phase, tmp_path, monkeypatch):
    line = agents.rating_line(phase)
    named = re.findall(r"`([^`]+)`", line)
    assert named == list(report_reader.SEVERITIES)
    assert set(named) <= set(defined_ratings(f"{phase}.md"))
    assert "highest" in line and "verdict" in line
    assert f"\n\n{line}" in printed_prompt(phase, tmp_path, monkeypatch)


@pytest.mark.parametrize("phase", RATING_PHASES)
def test_rating_phase_prompt_names_no_other_rating_word(phase, tmp_path, monkeypatch):
    prompt = printed_prompt(phase, tmp_path, monkeypatch)
    assert not whole_words(prompt) & set(OTHER_RATING_WORDS)


@pytest.mark.parametrize("phase", OTHER_PHASES)
def test_other_phases_are_told_nothing_about_ratings(phase, tmp_path, monkeypatch):
    assert agents.rating_line(phase) == ""
    prompt = printed_prompt(phase, tmp_path, monkeypatch)
    assert not whole_words(prompt) & set(report_reader.SEVERITIES)


# ---------------------------------------------------------------------------
# What a role is told to read, and the Ops addendum
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phase", list(agents.AGENT_NAMES))
def test_the_prompt_no_longer_says_read_all_existing_artifacts(phase, tmp_path, monkeypatch):
    """Every role is given a reading list; the Arbiter's is every report
    in step order (agents.FULL_HISTORY_PHASES)."""
    prompt = printed_prompt(phase, tmp_path, monkeypatch)
    assert "Read all existing artifacts" not in prompt
    heading = (role_context.FULL_HEADING if phase in agents.FULL_HISTORY_PHASES
               else role_context.REQUIRED_HEADING)
    assert heading in prompt


def git(repo, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                          cwd=repo, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def repo_with_addendum(tmp_path, monkeypatch, files: dict) -> Path:
    """A registered repo whose `main` commits `files` (name: text or bytes),
    with the registry and the tasks folder pointed at temporary copies."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for name, content in files.items():
        written = repo / name
        written.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            written.write_bytes(content)
        else:
            written.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "main")
    registry = tmp_path / "registry.yaml"
    registry.write_text(f"repos:\n  the-repo: {repo}\n")
    monkeypatch.setattr(settings, "REGISTRY_PATH", registry)
    monkeypatch.setattr(settings, "TASKS_DIR", tmp_path / "tasks")
    return repo


def ops_prompt(worktree, base="main") -> str:
    """The Ops prompt for a task on `base`, with the base commit `relay
    start` would record: confirmed through base_trust, or none at all when
    Relay refuses the base (then there is no addendum)."""
    state = {"repo": "the-repo", "worktree_path": str(worktree), "branch": "feature/x",
             "base": base}
    repo_path = settings._resolve_repo_path("the-repo")
    if base:
        try:
            trusted = base_trust.resolve(repo_path, base)
            state["base_ref"] = trusted.ref
            state["base_commit"] = base_trust.confirm_at_start(repo_path, trusted)
        except base_trust.BaseRefused:
            pass
    return agents.build_prompt("ops", "a-task", 9, "09-ops.md", state=state)


MAIN_FILES = {".relay.yaml": "ops_addendum: OPS.md\n", "OPS.md": "MAIN-ADDENDUM\n"}


def plant_on_the_branch(checkout: Path) -> None:
    """What a task's branch can do: commit its own addendum, and leave a
    config naming another file uncommitted."""
    (checkout / "OPS.md").write_text("BRANCH-ADDENDUM\n")
    git(checkout, "commit", "-q", "-am", "plant")
    (checkout / "PLANTED.md").write_text("PLANTED-ADDENDUM\n")
    (checkout / ".relay.yaml").write_text("ops_addendum: PLANTED.md\n")


def test_ops_addendum_comes_from_the_base_branch_when_the_task_builds_in_the_repo_itself(
        tmp_path, monkeypatch):
    repo = repo_with_addendum(tmp_path, monkeypatch, MAIN_FILES)
    git(repo, "switch", "-q", "-c", "feature/x")
    plant_on_the_branch(repo)
    prompt = ops_prompt(repo)
    assert "MAIN-ADDENDUM" in prompt
    assert "BRANCH-ADDENDUM" not in prompt and "PLANTED-ADDENDUM" not in prompt


def test_ops_addendum_comes_from_the_base_branch_not_a_separate_worktree(tmp_path, monkeypatch):
    repo = repo_with_addendum(tmp_path, monkeypatch, MAIN_FILES)
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "-b", "feature/x", str(worktree))
    plant_on_the_branch(worktree)
    prompt = ops_prompt(worktree)
    assert "MAIN-ADDENDUM" in prompt
    assert "BRANCH-ADDENDUM" not in prompt and "PLANTED-ADDENDUM" not in prompt


def plant_a_tag_named(repo: Path, tag: str) -> None:
    """What someone who can push a branch and a tag can do: a commit off
    the base whose config and addendum are their own, tagged with the
    base's short name. A plain fetch brings such a tag in."""
    git(repo, "switch", "-q", "-c", "pushed-branch")
    (repo / "OPS.md").write_text("TAG-ADDENDUM\n")
    (repo / ".relay.yaml").write_text("ops_addendum: OPS.md\n")
    git(repo, "commit", "-q", "-am", "plant")
    git(repo, "tag", tag)
    git(repo, "switch", "-q", "main")


def register(tmp_path, repo: Path) -> None:
    (tmp_path / "registry.yaml").write_text(f"repos:\n  the-repo: {repo}\n")


def test_a_tag_named_like_the_remote_base_does_not_shadow_it(tmp_path, monkeypatch):
    upstream = repo_with_addendum(tmp_path, monkeypatch, MAIN_FILES)
    plant_a_tag_named(upstream, "origin/main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(upstream), str(clone))
    assert git(clone, "tag", "--list") == "origin/main"  # the clone fetched the tag
    register(tmp_path, clone)
    git(clone, "switch", "-q", "-c", "feature/x")
    prompt = ops_prompt(clone, base="origin/main")
    assert "MAIN-ADDENDUM" in prompt
    assert "TAG-ADDENDUM" not in prompt


def test_a_tag_named_like_the_local_base_does_not_shadow_it(tmp_path, monkeypatch):
    repo = repo_with_addendum(tmp_path, monkeypatch, MAIN_FILES)
    plant_a_tag_named(repo, "main")
    git(repo, "switch", "-q", "-c", "feature/x")
    prompt = ops_prompt(repo, base="main")
    assert "MAIN-ADDENDUM" in prompt
    assert "TAG-ADDENDUM" not in prompt


def test_a_tag_named_like_the_remote_tracking_ref_does_not_shadow_a_local_base(
        tmp_path, monkeypatch):
    """`refs/remotes/main` is a valid tag name, and git's short-name rules
    read it as `refs/tags/refs/remotes/main` when no such remote-tracking
    branch exists. Relay must look `refs/remotes/main` up exactly, find
    nothing, and go on to `refs/heads/main`."""
    repo = repo_with_addendum(tmp_path, monkeypatch, MAIN_FILES)
    plant_a_tag_named(repo, "refs/remotes/main")
    git(repo, "switch", "-q", "-c", "feature/x")
    prompt = ops_prompt(repo, base="main")
    assert "MAIN-ADDENDUM" in prompt
    assert "TAG-ADDENDUM" not in prompt


def test_a_tag_named_like_a_pruned_remote_base_does_not_stand_in_for_it(tmp_path, monkeypatch):
    """With `refs/remotes/origin/main` gone (pruned), a tag named
    `refs/remotes/origin/main` must not answer for the base: there is then
    no base commit, so no addendum at all."""
    upstream = repo_with_addendum(tmp_path, monkeypatch, MAIN_FILES)
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(upstream), str(clone))
    register(tmp_path, clone)
    git(clone, "switch", "-q", "-c", "feature/x")
    git(clone, "update-ref", "-d", "refs/remotes/origin/main")
    plant_a_tag_named(clone, "refs/remotes/origin/main")
    git(clone, "switch", "-q", "feature/x")
    prompt = ops_prompt(clone, base="origin/main")
    assert "TAG-ADDENDUM" not in prompt
    assert "Project-specific deploy context" not in prompt


def commit_off_main(repo: Path, addendum: str) -> str:
    """A commit off `main` whose config and addendum are someone else's,
    on a throwaway branch that is then deleted. Returns its id."""
    git(repo, "switch", "-q", "-c", "throwaway")
    (repo / "OPS.md").write_text(addendum)
    (repo / ".relay.yaml").write_text("ops_addendum: OPS.md\n")
    git(repo, "commit", "-q", "-am", "plant")
    commit = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "main")
    git(repo, "branch", "-q", "-D", "throwaway")
    return commit


def add_packed_ref(repo: Path, ref_line: bytes) -> None:
    """Write `ref_line` (`<id> <full ref name>`) into the repo's packed
    refs, as a host that keeps refs on a case-sensitive disk holds them:
    there `refs/heads/MAIN` and `refs/heads/main` are two refs. Every ref
    is packed first, so the file is the repo's whole ref store. Git reads
    the file as sorted by ref name, so it is kept that way."""
    git(repo, "pack-refs", "--all")
    packed = repo / ".git" / "packed-refs"
    lines = packed.read_bytes().splitlines()
    header = [line for line in lines if line.startswith(b"#")]
    refs = sorted([line for line in lines if not line.startswith(b"#")] + [ref_line],
                  key=lambda line: line.split(b" ", 1)[1])
    packed.write_bytes(b"\n".join(header + refs) + b"\n")


def disk_ignores_case(folder: Path) -> bool:
    (folder / "case-probe").write_text("")
    return (folder / "CASE-PROBE").exists()


def test_a_remote_branch_named_like_the_base_but_for_case_does_not_stand_in_for_it(
        tmp_path, monkeypatch):
    """On a case-insensitive disk (this Mac's default), fetching a remote
    branch `MAIN` writes the loose ref file `refs/remotes/origin/MAIN`,
    which is the same file as `refs/remotes/origin/main`, so the exact
    lookup of the base then returns the pushed commit."""
    if not disk_ignores_case(tmp_path):
        pytest.skip("this disk keeps MAIN and main apart, so a fetched MAIN cannot "
                    "overwrite the base's ref file")
    upstream = repo_with_addendum(tmp_path, monkeypatch, MAIN_FILES)
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(upstream), str(clone))
    pushed = commit_off_main(upstream, "CASE-ADDENDUM\n")
    add_packed_ref(upstream, f"{pushed} refs/heads/MAIN".encode())
    git(clone, "fetch", "-q", "origin")
    # The trick took: git itself now reads the base as the pushed commit.
    assert git(clone, "show-ref", "--verify", "--hash", "refs/remotes/origin/main") == pushed
    register(tmp_path, clone)
    git(clone, "switch", "-q", "-c", "feature/x")
    prompt = ops_prompt(clone, base="origin/main")
    assert "CASE-ADDENDUM" not in prompt
    assert "Project-specific deploy context" not in prompt


def test_a_remote_branch_named_like_the_base_but_for_case_does_not_stand_in_for_a_loose_base(
        tmp_path, monkeypatch):
    """The base has moved since the clone, so
    a fetch has already written `refs/remotes/origin/main` as a loose file.
    On a case-insensitive disk a later fetch of a remote branch `MAIN`
    writes into that same file, which keeps its old name, so git lists
    only `refs/remotes/origin/main` and the exact lookup returns the
    pushed commit. Origin itself lists the twin, so Relay refuses the base."""
    if not disk_ignores_case(tmp_path):
        pytest.skip("this disk keeps MAIN and main apart, so a fetched MAIN cannot "
                    "overwrite the base's ref file")
    upstream = repo_with_addendum(tmp_path, monkeypatch, MAIN_FILES)
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(upstream), str(clone))
    git(upstream, "commit", "-q", "--allow-empty", "-m", "the base moves on")
    git(clone, "fetch", "-q", "origin")
    assert (clone / ".git" / "refs" / "remotes" / "origin" / "main").is_file()
    pushed = commit_off_main(upstream, "CASE-ADDENDUM\n")
    add_packed_ref(upstream, f"{pushed} refs/heads/MAIN".encode())
    git(clone, "fetch", "-q", "origin")
    # The trick took: git itself now reads the base as the pushed commit.
    assert git(clone, "show-ref", "--verify", "--hash", "refs/remotes/origin/main") == pushed
    register(tmp_path, clone)
    git(clone, "switch", "-q", "-c", "feature/x")
    prompt = ops_prompt(clone, base="origin/main")
    assert "CASE-ADDENDUM" not in prompt
    assert "Project-specific deploy context" not in prompt


@pytest.mark.parametrize("twin", ["refs/heads/MAIN", "refs/heads/Main", "refs/HEADS/main"])
def test_a_ref_named_like_the_base_but_for_case_means_no_addendum(twin, tmp_path, monkeypatch):
    """Which of two such refs a case-insensitive disk answers with depends
    on which was written last, so with a twin present Relay trusts neither."""
    repo = repo_with_addendum(tmp_path, monkeypatch, MAIN_FILES)
    pushed = commit_off_main(repo, "CASE-ADDENDUM\n")
    add_packed_ref(repo, f"{pushed} {twin}".encode())
    git(repo, "switch", "-q", "-c", "feature/x")
    prompt = ops_prompt(repo, base="main")
    assert "CASE-ADDENDUM" not in prompt
    assert "Project-specific deploy context" not in prompt


def test_a_ref_name_that_is_not_utf8_does_not_stop_the_addendum(tmp_path, monkeypatch):
    """Git allows any byte above 0x7f in a ref name, and a fetch can bring
    one in; listing the refs must not fail on it."""
    repo = repo_with_addendum(tmp_path, monkeypatch, MAIN_FILES)
    other = commit_off_main(repo, "OTHER-ADDENDUM\n")
    add_packed_ref(repo, other.encode() + b" refs/heads/caf\xe9")
    git(repo, "switch", "-q", "-c", "feature/x")
    prompt = ops_prompt(repo, base="main")
    assert "MAIN-ADDENDUM" in prompt


@pytest.mark.parametrize("files, base", [
    ({".relay.yaml": "ops_addendum: ../outside.md\n"}, "main"),
    ({".relay.yaml": "ops_addendum: OUTSIDE_ABSOLUTE\n"}, "main"),
    ({".relay.yaml": "ops_addendum: docs\n", "docs/OPS.md": "IN-A-FOLDER\n"}, "main"),
    ({".relay.yaml": "ops_addendum: [OPS.md]\n", "OPS.md": "LISTED\n"}, "main"),
    ({".relay.yaml": "ops_addendum: MISSING.md\n"}, "main"),
    ({".relay.yaml": "ops_addendum: OPS.md\n", "OPS.md": b"\xff\xfeNOT-UTF-8\n"}, "main"),
    ({".relay.yaml": ": : [ not yaml\n", "OPS.md": "UNREAD\n"}, "main"),
    ({".relay.yaml": "- a list, not a mapping\n", "OPS.md": "UNREAD\n"}, "main"),
    (MAIN_FILES, None),
    (MAIN_FILES, "-main"),
    (MAIN_FILES, "no-such-branch"),
    (MAIN_FILES, "HEAD"),
], ids=["path out of the repo", "absolute path", "a folder", "not a string", "missing file",
        "not utf-8", "config not yaml", "config not a mapping", "no base recorded",
        "base like an option", "base that does not exist", "base that is not a branch"])
def test_an_addendum_relay_cannot_read_safely_is_left_out(files, base, tmp_path, monkeypatch):
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE-THE-REPO\n")
    files = {name: content.replace("OUTSIDE_ABSOLUTE", str(outside))
             if isinstance(content, str) else content for name, content in files.items()}
    repo = repo_with_addendum(tmp_path, monkeypatch, files)
    prompt = ops_prompt(repo, base=base)
    assert "Project-specific deploy context" not in prompt
