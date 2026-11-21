"""Scratch checkouts: made from a commit, overlaid literally, always deleted."""

import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scratch_checkout
from tests.git_fixtures import git, repo  # noqa: F401  (repo is a fixture)


def test_it_holds_the_commit_and_nothing_ignored_and_is_deleted(repo):
    (repo / "reports").mkdir()
    (repo / "reports" / "old.xml").write_text("ignored\n")
    commit = git(repo, "rev-parse", "HEAD")
    with scratch_checkout.scratch_checkout(repo, commit) as folder:
        assert (folder / ".relay" / "profile.yaml").exists()
        assert not (folder / "reports").exists()
        assert git(folder, "rev-parse", "HEAD") == commit
    assert not folder.exists()
    assert git(repo, "status", "--porcelain") == ""


def test_it_is_deleted_when_the_block_raises(repo):
    with pytest.raises(RuntimeError):
        with scratch_checkout.scratch_checkout(repo, git(repo, "rev-parse", "HEAD")) as folder:
            raise RuntimeError("the run broke")
    assert not folder.exists()


def test_a_commit_git_cannot_find_is_refused_and_leaves_nothing(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "temp"))
    (tmp_path / "temp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", None)
    with pytest.raises(scratch_checkout.ScratchCheckoutFailed, match="could not check out the commit"):
        with scratch_checkout.scratch_checkout(repo, "0" * 40):
            pass
    assert list((tmp_path / "temp").iterdir()) == []


def test_read_only_entries_are_deleted_and_a_link_out_is_never_followed(tmp_path):
    outside = tmp_path / "owners-file"
    outside.write_text("keep\n")
    outside.chmod(0o640)
    folder = tmp_path / "relay-scratch-test"
    (folder / "locked").mkdir(parents=True)
    (folder / "readonly-file").write_text("x\n")
    (folder / "readonly-file").chmod(0o400)
    # The link is alone in a folder it cannot be unlinked from, so the
    # retry is what removes it.
    os.symlink(outside, folder / "locked" / "link")
    (folder / "locked").chmod(stat.S_IRUSR | stat.S_IXUSR)
    scratch_checkout.remove_folder(folder)
    assert not folder.exists()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o640
    assert outside.read_text() == "keep\n"


def test_an_overlay_path_that_is_not_utf8_is_refused_and_leaves_nothing(repo, tmp_path, monkeypatch):
    """Branch file names are read from git with undecodable bytes kept as
    surrogates; such a name cannot be written back as UTF-8 text."""
    monkeypatch.setenv("TMPDIR", str(tmp_path / "temp"))
    (tmp_path / "temp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", None)
    commit = git(repo, "rev-parse", "HEAD")
    with pytest.raises(scratch_checkout.ScratchCheckoutFailed, match="name is not UTF-8: tests/test_.txt"):
        with scratch_checkout.scratch_checkout(repo, commit, overlay_from=commit,
                                               overlay_paths=["tests/test_\udcff.txt"]):
            pass
    assert list((tmp_path / "temp").iterdir()) == []
