"""Prototype mode: `relay proto` builds a
sketch fast inside guardrails, and `relay promote` turns one the owner
likes into a real task whose spec is the sketch's screenshots. Prototype
code is never merged."""

from datetime import datetime, timedelta, timezone

import pytest

import proto
import settings
import sizing

IDEA = ("A one-tap check-in for crews: the foreman taps a job, the app says who is there, "
        "and James sees it on their phone without calling anyone. Wow is no phone calls.")
T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def home(tmp_path, monkeypatch):
    repo = tmp_path / "repos" / "demo"
    repo.mkdir(parents=True)
    registry = tmp_path / "registry.yaml"
    registry.write_text(f"repos:\n  demo: {repo}\n")
    monkeypatch.setattr(settings, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(settings, "REGISTRY_PATH", registry)
    monkeypatch.setattr(proto, "WORKTREES_ROOT", tmp_path / "worktrees")
    (tmp_path / "tasks").mkdir()
    return tmp_path


def shoot(name, *files):
    for file in files:
        (settings.TASKS_DIR / name / proto.EVIDENCE / file).write_bytes(b"x")


def test_start_records_the_idea_the_branch_and_the_timebox(home):
    lines = proto.start("checkin", "demo", IDEA, 3, now=T0)
    record = proto.read_record("checkin")
    assert record["branch"] == "proto/checkin"
    assert record["ends"] == (T0 + timedelta(hours=3)).isoformat()
    assert record["status"] == proto.BUILDING
    text = "\n".join(lines)
    assert "worktree add -b proto/checkin" in text
    assert IDEA in text
    for rule in proto.GUARDRAILS:
        assert rule in text
    assert "no role passes" in text


@pytest.mark.parametrize("repo, idea, hours, why", [
    (None, IDEA, 4, "--repo"),
    ("nowhere", IDEA, 4, "no repo is registered"),
    ("demo", "make it pop", 4, "one paragraph"),
    ("demo", IDEA, 0, "timebox"),
    ("demo", IDEA, 9, "timebox"),
])
def test_start_refuses_what_it_cannot_build_from(home, repo, idea, hours, why):
    with pytest.raises(proto.ProtoError, match=why):
        proto.start("checkin", repo, idea, hours, now=T0)
    assert not (settings.TASKS_DIR / "checkin").exists()


@pytest.mark.parametrize("name", ["../up", "a b", "-x", "x.lock", ""])
def test_a_name_that_cannot_be_a_branch_or_folder_is_refused(home, name):
    with pytest.raises(proto.ProtoError):
        proto.start(name, "demo", IDEA, 4, now=T0)


def test_start_will_not_reuse_a_task_folder(home):
    (settings.TASKS_DIR / "taken").mkdir()
    with pytest.raises(proto.ProtoError, match="already exists"):
        proto.start("taken", "demo", IDEA, 4, now=T0)


def test_done_needs_a_screenshot(home):
    proto.start("checkin", "demo", IDEA, 4, now=T0)
    shoot("checkin", "notes.txt")
    with pytest.raises(proto.ProtoError, match="no screenshot"):
        proto.done("checkin", now=T0 + timedelta(hours=1))
    assert proto.read_record("checkin")["status"] == proto.BUILDING


def test_done_counts_the_minutes_and_says_when_it_ran_over(home):
    proto.start("checkin", "demo", IDEA, 1, now=T0)
    shoot("checkin", "phone-390x844.png", "flow.mp4")
    lines = proto.done("checkin", now=T0 + timedelta(minutes=75))
    assert "75 minutes, 15 over its timebox." in lines[0]
    assert lines[1] == "  Screenshots: 1; clips: 1"
    assert proto.read_record("checkin")["status"] == proto.SHOWN


def test_done_says_when_there_is_no_clip(home):
    proto.start("checkin", "demo", IDEA, 4, now=T0)
    shoot("checkin", "phone.png")
    assert any("No clip" in line for line in proto.done("checkin", now=T0))


def test_promote_needs_the_prototype_shown_first(home):
    proto.start("checkin", "demo", IDEA, 4, now=T0)
    with pytest.raises(proto.ProtoError, match="not been shown"):
        proto.promote("checkin", "feature", "no")


@pytest.mark.parametrize("size, risky, why", [(None, "no", "--size"), ("fix", "no", "--size"),
                                              ("feature", None, "--risky"), ("feature", "maybe", "--risky")])
def test_promote_needs_a_size_and_a_risk(home, size, risky, why):
    proto.start("checkin", "demo", IDEA, 4, now=T0)
    shoot("checkin", "phone.png")
    proto.done("checkin", now=T0)
    with pytest.raises(proto.ProtoError, match=why):
        proto.promote("checkin", size, risky)


def test_promote_writes_a_brief_whose_spec_is_the_screenshots(home):
    proto.start("checkin", "demo", IDEA, 4, now=T0)
    shoot("checkin", "phone.png", "tablet.png", "flow.mp4")
    proto.done("checkin", now=T0)
    lines = proto.promote("checkin", "feature", "yes", now=T0)
    task = settings.TASKS_DIR / "checkin-build"
    brief = (task / "brief.md").read_text()
    assert sorted(p.name for p in (task / proto.SPEC).iterdir()) == ["flow.mp4", "phone.png", "tablet.png"]
    assert "`spec/phone.png`" in brief and IDEA in brief and "never merged" in brief
    got = sizing.sizing_in(brief)
    assert (got.size, got.risky, got.note) == ("feature", True, None)
    assert "worktree add -b feature/checkin-build" in "\n".join(lines)
    record = proto.read_record("checkin")
    assert (record["status"], record["promoted_to"]) == (proto.PROMOTED, "checkin-build")


def test_a_prototype_is_promoted_once(home):
    proto.start("checkin", "demo", IDEA, 4, now=T0)
    shoot("checkin", "phone.png")
    proto.done("checkin", now=T0)
    proto.promote("checkin", "feature", "no")
    with pytest.raises(proto.ProtoError, match="already promoted"):
        proto.promote("checkin", "feature", "no", new_name="again")


def test_promote_will_not_overwrite_a_task(home):
    proto.start("checkin", "demo", IDEA, 4, now=T0)
    shoot("checkin", "phone.png")
    proto.done("checkin", now=T0)
    (settings.TASKS_DIR / "checkin-build").mkdir()
    with pytest.raises(proto.ProtoError, match="already exists"):
        proto.promote("checkin", "feature", "no")
    assert proto.read_record("checkin")["status"] == proto.SHOWN


def test_proto_branches_are_recognised():
    assert proto.is_proto_branch("proto/checkin")
    assert not proto.is_proto_branch("feature/checkin")
    assert not proto.is_proto_branch(None)


def test_list_shows_every_prototype_and_where_it_went(home):
    assert proto.list_lines() == ["No prototypes."]
    proto.start("one", "demo", IDEA, 4, now=T0)
    proto.start("two", "demo", IDEA, 4, now=T0 + timedelta(hours=1))
    lines = proto.list_lines()
    assert lines[0].startswith("two  building") and lines[1].startswith("one  building")


# ---------------------------------------------------------------------------
# Through the command line, start to finish
# ---------------------------------------------------------------------------

import subprocess
import sys
from pathlib import Path

RELAY = Path(__file__).resolve().parent.parent / "relay.py"


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def relay(home, cwd, *args):
    env = {"RELAY_HOME": str(home), "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
           "HOME": str(home.parent), "RELAY_WORKTREES_ROOT": str(home.parent / "worktrees")}
    return subprocess.run([sys.executable, str(RELAY), *args], cwd=cwd, env=env,
                          capture_output=True, text=True)


def test_proto_to_promote_through_the_cli_and_start_refuses_the_proto_branch(tmp_path):
    home = tmp_path / "relay-home"
    (home / "tasks").mkdir(parents=True)
    (home / "orchestrator").mkdir()
    repo = tmp_path / "demo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "start")
    (home / "registry.yaml").write_text(f"repos:\n  demo: {repo}\n")

    started = relay(home, tmp_path, "proto", "start", "checkin", "--repo", "demo", "--idea", IDEA)
    assert started.returncode == 0, started.stderr + started.stdout
    assert "worktree add -b proto/checkin" in started.stdout

    git(repo, "switch", "-q", "-c", "proto/checkin")
    (home / "tasks" / "checkin" / "brief.md").write_text("---\nsize: fix\nrisky: no\n---\nx\n")
    refused = relay(home, repo, "start", "checkin", "--repo", "demo")
    assert refused.returncode != 0
    assert "never merged" in refused.stdout

    assert relay(home, tmp_path, "proto", "done", "checkin").returncode == 1
    (home / "tasks" / "checkin" / "evidence" / "phone.png").write_bytes(b"x")
    assert relay(home, tmp_path, "proto", "done", "checkin").returncode == 0
    promoted = relay(home, tmp_path, "promote", "checkin", "--size", "feature", "--risky", "no")
    assert promoted.returncode == 0, promoted.stdout
    assert (home / "tasks" / "checkin-build" / "spec" / "phone.png").exists()
    assert "checkin  promoted" in relay(home, tmp_path, "proto", "list").stdout


def test_proto_flags_belong_to_proto(tmp_path):
    home = tmp_path / "relay-home"
    (home / "orchestrator").mkdir(parents=True)
    result = relay(home, tmp_path, "list", "--idea", "x")
    assert result.returncode == 2 and "--idea is for proto start" in result.stderr


def test_a_corrupt_record_is_an_error_not_a_crash(home):
    proto.start("checkin", "demo", IDEA, 4, now=T0)
    (settings.TASKS_DIR / "checkin" / proto.RECORD).write_text("kind: [prototype\n")
    with pytest.raises(proto.ProtoError, match="could not be read"):
        proto.read_record("checkin")
    assert proto.list_lines() == ["No prototypes."]


def test_links_in_the_evidence_are_not_copied_and_same_names_both_survive(home, tmp_path):
    proto.start("checkin", "demo", IDEA, 4, now=T0)
    evidence = settings.TASKS_DIR / "checkin" / proto.EVIDENCE
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"secret")
    (evidence / "leak.png").symlink_to(secret)
    (evidence / "phone").mkdir()
    (evidence / "tablet").mkdir()
    (evidence / "phone" / "home.png").write_bytes(b"p")
    (evidence / "tablet" / "home.png").write_bytes(b"t")
    proto.done("checkin", now=T0)
    proto.promote("checkin", "feature", "no")
    spec = settings.TASKS_DIR / "checkin-build" / proto.SPEC
    assert sorted(p.name for p in spec.iterdir()) == ["phone-home.png", "tablet-home.png"]
