"""The task's base, as a commit Relay has confirmed.

This is the only module that hands a task's base *name* to git. Every
other reader of the base (the Ops addendum, the role prompt, the test-run
rules, the fork point, the sizing hint) takes the commit id this module
recorded, through trusted_commit(), and never looks the name up.

Why: on a case-insensitive disk (APFS) a fetched branch `MAIN` writes into
the loose ref file `refs/remotes/origin/main`, so git itself then reads the
base as a commit anyone who can push a branch chose, and no local check
can see it. So the base is confirmed against a source a fetch cannot
rewrite:

- A remote-tracking base (`origin/main`) is confirmed by asking the remote
  itself (`git ls-remote`), with no prompt and a time limit. The remote
  must name that exact branch and no branch equal to it but for case; the
  local copy must be the remote's commit or an ancestor of it; and the
  remote's commit must already be in the repo. Relay never fetches.
- A local base (`main`) is read from its exact branch, after checking that
  no other ref equals its full name but for case. A fetch never writes a
  local branch.
- A full commit id the owner typed is owner input and is taken as it is.

The confirmed commit is recorded at `relay start` (a `base_confirmed`
event, or `base_commit` in run.yaml on the files backend) and checked again
before every launch (check_before_step). It only moves forward, to a
confirmed descendant; a base that no longer contains it stops the task.
"""

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

import db
import run_state
from evidence import is_full_commit_id
from printable import printable
from settings import _resolve_repo_path, _sqlite_enabled, get_db_connection

# How long one ask of the remote may take before it counts as unreachable.
BASE_CHECK_SECONDS = 30
# How many characters of a commit id a message shows.
SHORT_ID = 7
# How many characters of a name read from git or a remote a message shows.
NAME_LIMIT = 200

# The environment the remote is asked in: no prompt of any kind, while the
# owner's credential helpers and agents still work. An empty GIT_ASKPASS
# turns off every askpass program git would otherwise run.
NO_PROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS_REQUIRE": "never",
                 "GCM_INTERACTIVE": "never"}

# Why Relay's test run did not run (recorded_runs).
NO_TRUSTED_COMMIT = ("Relay has no confirmed commit for the task's base, so it ran no tests; "
                     "run relay next again")


class BaseRefused(Exception):
    """Relay cannot trust the base. The message is one line with no
    trailing period; the caller prints `Error: <reason>.`"""


@dataclass(frozen=True)
class Base:
    """A task's base. `name` is as recorded (`origin/main`, `main`, or a
    commit id); `ref` the full ref it is read through, None for a commit
    id; `remote` the remote that confirms it, None for a local branch or a
    commit id; `branch` the branch name (on the remote, or local), None for
    a commit id."""
    name: str
    ref: str | None
    remote: str | None
    branch: str | None


@dataclass(frozen=True)
class Confirmed:
    """What one confirmation found: the confirmed `commit`; or, for a
    remote base, `stale_remote_commit`, the remote's commit when this repo
    does not have it yet; or `unreachable` when the remote did not answer."""
    commit: str | None = None
    stale_remote_commit: str | None = None
    unreachable: bool = False


def short(commit: str) -> str:
    return commit[:SHORT_ID]


def _shown(name: str) -> str:
    return printable(name, NAME_LIMIT)


# ---------------------------------------------------------------------------
# git, by exact name only
# ---------------------------------------------------------------------------

def _git(repo_path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", *args], cwd=str(repo_path), capture_output=True, text=True,
                              errors="surrogateescape")
    except OSError:
        return None


def _git_out(repo_path, *args: str) -> str | None:
    done = _git(repo_path, *args)
    return done.stdout.strip() if done is not None and done.returncode == 0 else None


def _ref_exists(repo_path, full_ref: str) -> bool:
    """True when a ref of exactly this full name exists. show-ref --verify
    applies none of git's short-name rules, so no tag answers for it."""
    done = _git(repo_path, "show-ref", "--verify", "--quiet", full_ref)
    return done is not None and done.returncode == 0


def _ref_commit(repo_path, full_ref: str) -> str | None:
    """The commit the ref of exactly this full name points to, or None."""
    object_id = _git_out(repo_path, "show-ref", "--verify", "--hash", full_ref)
    return _peeled(repo_path, object_id) if object_id else None


def _peeled(repo_path, object_id: str) -> str | None:
    """The commit `object_id` (a full id) is or points to, or None when it
    is not a commit this repo has."""
    if not is_full_commit_id(object_id):
        return None
    peeled = _git_out(repo_path, "rev-parse", "--verify", "--quiet", f"{object_id}^{{commit}}")
    return peeled if is_full_commit_id(peeled) else None


def _is_ancestor(repo_path, older: str, newer: str) -> bool:
    done = _git(repo_path, "merge-base", "--is-ancestor", older, newer)
    return done is not None and done.returncode == 0


def _remotes(repo_path) -> list[str]:
    listed = _git_out(repo_path, "remote")
    return [name for name in (listed or "").splitlines() if name and not name.startswith("-")]


def _remote_of(repo_path, remote_ref_path: str) -> str | None:
    """The longest remote this repo lists that `remote_ref_path`
    (`origin/main`) starts with, followed by a `/`."""
    matching = [name for name in _remotes(repo_path) if remote_ref_path.startswith(f"{name}/")]
    return max(matching, key=len) if matching else None


def _case_twins(repo_path, full_ref: str) -> list[str] | None:
    """Every ref in the repo, loose or packed, whose full name equals
    `full_ref` but for case and is not it; None when git cannot list them.
    A name that is not UTF-8 is kept with its odd bytes escaped."""
    listed = _git(repo_path, "for-each-ref", "--format=%(refname)")
    if listed is None or listed.returncode != 0:
        return None
    return [name for name in listed.stdout.splitlines()
            if name.casefold() == full_ref.casefold() and name != full_ref]


def _refuse_local_twin(repo_path, full_ref: str) -> None:
    twins = _case_twins(repo_path, full_ref)
    if twins is None:
        raise BaseRefused(f"git could not list the refs of {repo_path} to check the base {_shown(full_ref)}")
    if twins:
        raise BaseRefused(f"this repo has a ref {_shown(twins[0])}, which this disk cannot keep apart "
                          f"from {_shown(full_ref)}; remove it, and start again")


# ---------------------------------------------------------------------------
# Asking the remote
# ---------------------------------------------------------------------------

def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def ls_remote_heads(repo_path, remote: str) -> dict[str, str] | None:
    """{full ref name: object id} for every branch the remote lists, asked
    now, or None when it could not be asked: a non-zero exit, no answer
    within BASE_CHECK_SECONDS, or git not found. It runs in a session of
    its own with stdin closed and NO_PROMPT_ENV, so nothing can prompt; on
    the time limit its whole process group is killed."""
    if remote not in _remotes(repo_path):
        return None
    try:
        asking = subprocess.Popen(["git", "ls-remote", "--heads", remote], cwd=str(repo_path),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, errors="surrogateescape",
                                  start_new_session=True, env={**os.environ, **NO_PROMPT_ENV})
    except OSError:
        return None
    try:
        output, _ = asking.communicate(timeout=BASE_CHECK_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_group(asking)
        return None
    if asking.returncode != 0:
        return None
    listing = {}
    for line in output.splitlines():
        object_id, tab, ref = line.partition("\t")
        if tab:
            listing[ref] = object_id
    return listing


# ---------------------------------------------------------------------------
# Turning the name into a base, and confirming it
# ---------------------------------------------------------------------------

def _remote_base(name: str, remote: str) -> Base:
    branch = name[len(remote) + 1:]
    return Base(name=name, ref=f"refs/remotes/{name}", remote=remote, branch=branch)


def _local_base(name: str) -> Base:
    return Base(name=name, ref=f"refs/heads/{name}", remote=None, branch=name)


def _default_base(repo_path) -> Base:
    """The repo's default branch: origin's HEAD when it names an existing
    `refs/remotes/origin/<b>`, else the first of origin/main, origin/master,
    main and master that exists."""
    has_origin = "origin" in _remotes(repo_path)
    if has_origin:
        origin_head = _git_out(repo_path, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if (origin_head and origin_head.startswith("refs/remotes/origin/")
                and origin_head != "refs/remotes/origin/HEAD" and _ref_exists(repo_path, origin_head)):
            return _remote_base(origin_head.removeprefix("refs/remotes/"), "origin")
        for branch in ("main", "master"):
            if _ref_exists(repo_path, f"refs/remotes/origin/{branch}"):
                return _remote_base(f"origin/{branch}", "origin")
    for branch in ("main", "master"):
        if _ref_exists(repo_path, f"refs/heads/{branch}"):
            return _local_base(branch)
    raise BaseRefused(f"could not find the default branch of {repo_path}; give --base")


def resolve(repo_path, given: str | None) -> Base:
    """The base `given` names, by exact full ref names only, or the repo's
    default branch when it is None. In order: a remote-tracking branch
    (`origin/main`, when `origin` is a remote this repo lists), a local
    branch, the same branch on origin, a full commit id the repo has.
    Anything else is refused: a tag by name, else as not found."""
    if given is None:
        return _default_base(repo_path)
    remote = _remote_of(repo_path, given)
    if remote and _ref_exists(repo_path, f"refs/remotes/{given}"):
        return _remote_base(given, remote)
    if _ref_exists(repo_path, f"refs/heads/{given}"):
        return _local_base(given)
    if "origin" in _remotes(repo_path) and _ref_exists(repo_path, f"refs/remotes/origin/{given}"):
        return _remote_base(f"origin/{given}", "origin")
    if is_full_commit_id(given):
        if _peeled(repo_path, given) != given:
            raise BaseRefused(f"commit {given} is not in this repo; fetch and start again")
        return Base(name=given, ref=None, remote=None, branch=None)
    if _ref_exists(repo_path, f"refs/tags/{given}"):
        raise BaseRefused(f"the base '{_shown(given)}' is a tag, not a branch; give a branch, "
                          f"or a full commit id with --base")
    raise BaseRefused(f"base branch '{_shown(given)}' not found in {repo_path} "
                      f"(tried '{_shown(given)}' and 'origin/{_shown(given)}'). "
                      f"Fetch it or check the name")


def base_from_record(repo_path, name: str, ref: str | None) -> Base:
    """The base a recorded `ref` names. A commit id has no ref; a
    remote-tracking ref is confirmed by the remote it starts with."""
    if ref is None:
        return Base(name=name, ref=None, remote=None, branch=None)
    if ref.startswith("refs/heads/"):
        return Base(name=name, ref=ref, remote=None, branch=ref.removeprefix("refs/heads/"))
    remote_ref_path = ref.removeprefix("refs/remotes/")
    remote = _remote_of(repo_path, remote_ref_path) if ref.startswith("refs/remotes/") else None
    if remote is None:
        raise BaseRefused(f"the base {_shown(name)} was confirmed through {_shown(ref)}, which names "
                          f"no remote this repo has; check the remote, and run next again")
    return Base(name=name, ref=ref, remote=remote, branch=remote_ref_path[len(remote) + 1:])


def confirm(repo_path, base: Base) -> Confirmed:
    """Confirm the base now, by the rules in the module docstring. Raises
    BaseRefused when the base cannot be trusted."""
    if base.ref is None:
        return Confirmed(commit=base.name)
    if base.remote is None:
        _refuse_local_twin(repo_path, base.ref)
        commit = _ref_commit(repo_path, base.ref)
        if commit is None:
            raise BaseRefused(f"base branch '{_shown(base.name)}' not found in {repo_path}. "
                              f"Fetch it or check the name")
        return Confirmed(commit=commit)
    return _confirm_by_the_remote(repo_path, base)


def _confirm_by_the_remote(repo_path, base: Base) -> Confirmed:
    listing = ls_remote_heads(repo_path, base.remote)
    if listing is None:
        return Confirmed(unreachable=True)
    target = f"refs/heads/{base.branch}"
    twins = [ref for ref in listing if ref.casefold() == target.casefold() and ref != target]
    if twins:
        raise BaseRefused(f"{_shown(base.remote)} also has a branch {_shown(twins[0].removeprefix('refs/heads/'))}, "
                          f"which this disk cannot keep apart from {_shown(base.branch)}; remove or "
                          f"rename it on {_shown(base.remote)}, fetch, and start again")
    if target not in listing:
        raise BaseRefused(f"{_shown(base.remote)} has no branch {_shown(base.branch)}; "
                          f"check the name and start again")
    _refuse_local_twin(repo_path, base.ref)
    remote_commit = _peeled(repo_path, listing[target])
    if remote_commit is None:
        return Confirmed(stale_remote_commit=printable(listing[target], 64))
    local = _ref_commit(repo_path, base.ref)
    if local is not None and local != remote_commit and not _is_ancestor(repo_path, local, remote_commit):
        raise BaseRefused(f"{_shown(base.name)} here is {short(local)}, which is not on "
                          f"{_shown(base.remote)}'s {_shown(base.branch)}; fetch, and check no "
                          f"worktree was made from it")
    return Confirmed(commit=remote_commit)


def confirm_at_start(repo_path, base: Base) -> str:
    """The commit to record for a base that has none yet. Refuses when the
    remote cannot be asked, or when its commit is not in this repo yet."""
    confirmed = confirm(repo_path, base)
    if confirmed.unreachable:
        raise BaseRefused(f"could not reach {_shown(base.remote)} to confirm the base "
                          f"{_shown(base.name)}; check the connection and start again")
    if confirmed.stale_remote_commit:
        raise BaseRefused(f"{_shown(base.remote)}'s {_shown(base.branch)} is at "
                          f"{short(confirmed.stale_remote_commit)}, which this repo does not have "
                          f"yet; fetch and start again")
    return confirmed.commit


# ---------------------------------------------------------------------------
# The record, and the check before every launch
# ---------------------------------------------------------------------------

def trusted_commit(state: dict | None) -> str | None:
    """The base commit Relay confirmed for the task, or None. Every reader
    of the base takes it from here."""
    commit = state.get("base_commit") if state else None
    return commit if is_full_commit_id(commit) else None


def has_base(state: dict | None) -> bool:
    """True when the task has a base at all, confirmed or not."""
    return bool(state and state.get("base"))


def _recorded(task_id: str, state: dict, config: dict) -> tuple[str | None, str] | None:
    """(ref, commit) recorded for the task's base, or None when it has none
    that names a full commit id for this base."""
    if _sqlite_enabled(config):
        payload = db.latest_base_confirmed(get_db_connection(), task_id) or {}
        if payload.get("base") != state.get("base"):
            return None
        ref, commit = payload.get("ref"), payload.get("commit")
    else:
        ref, commit = state.get("base_ref"), state.get("base_commit")
    if not is_full_commit_id(commit) or not (ref is None or isinstance(ref, str)):
        return None
    return ref, commit


def record(task_id: str, state: dict, base: Base, commit: str, how: str, previous: str | None,
           config: dict) -> None:
    """Record `commit` as the task's base commit, and set it in `state`.
    Under sqlite it is a base_confirmed event; on the files backend it is
    the state's own `base_ref` and `base_commit`, which the caller saves."""
    state["base_ref"], state["base_commit"] = base.ref, commit
    if _sqlite_enabled(config):
        db.record_base_confirmed(get_db_connection(), task_id, base=base.name, ref=base.ref,
                                 commit=commit, previous=previous, how=how)


def check_before_step(task_id: str, state: dict, config: dict) -> int | None:
    """Confirm the task's base before a role is launched, and set its
    commit in `state` for every reader. Returns None to go on, or 1 after
    printing `Error: <reason>.`, with nothing recorded. A task with no base
    has nothing to check."""
    if not has_base(state):
        return None
    try:
        _check(task_id, state, config)
    except BaseRefused as refused:
        print(f"Error: {refused}.")
        return 1
    return None


def _check(task_id: str, state: dict, config: dict) -> None:
    repo_path = _resolve_repo_path(state["repo"]) if state.get("repo") else None
    if repo_path is None or not Path(repo_path).is_dir():
        raise BaseRefused(f"could not find the registered repo {_shown(state.get('repo') or '(none)')} "
                          f"to confirm the base")
    recorded = _recorded(task_id, state, config)
    if recorded is None:
        base = resolve(repo_path, state["base"])
        commit = confirm_at_start(repo_path, base)
        _save(task_id, state, base, commit, "first", None, config)
        return
    ref, commit = recorded
    base = base_from_record(repo_path, state["base"], ref)
    if _peeled(repo_path, commit) != commit:
        raise BaseRefused(f"the base commit {short(commit)} this task was confirmed against is not "
                          f"in this repo; fetch and run next again")
    state["base_ref"], state["base_commit"] = ref, commit
    confirmed = confirm(repo_path, base)
    if confirmed.unreachable:
        print(f"Base not re-checked (could not reach {_shown(base.remote)}); using {short(commit)}.")
    elif confirmed.stale_remote_commit:
        print(f"Base not moved ({_shown(base.remote)}'s {_shown(base.branch)} is at "
              f"{short(confirmed.stale_remote_commit)}, which this repo does not have yet; fetch "
              f"to pick it up); using {short(commit)}.")
    elif confirmed.commit == commit:
        return
    elif _is_ancestor(repo_path, commit, confirmed.commit):
        _save(task_id, state, base, confirmed.commit, "moved", commit, config)
        print(f"Base {_shown(base.name)} moved to {short(confirmed.commit)}.")
    else:
        raise BaseRefused(f"the base {_shown(base.name)} no longer contains {short(commit)}, the "
                          f"commit this task was confirmed against; the base was rewritten, so "
                          f"check it before going on")


def _save(task_id: str, state: dict, base: Base, commit: str, how: str, previous: str | None,
          config: dict) -> None:
    with run_state.ledger_writes(task_id, config):
        record(task_id, state, base, commit, how, previous, config)
        if not _sqlite_enabled(config):
            run_state.save_run_state(task_id, state, config)
