"""Throwaway checkouts for the test runs Relay makes outside a task's worktree.

Relay runs a task's tests at the commit the branch left its base (to count
them there, and to see new tests fail there) and, when a profile asks, at
the branch's commit with no ignored files about. None of that may happen in
the task's worktree or the owner's main checkout. Each such run gets a
`git clone --shared` in a new folder under the system temp folder, which is
deleted afterwards whatever happens.

Paths that come from the branch reach git only literally (never as pathspec
magic) and only through standard input. Every git call is an argument list;
nothing here stashes, resets or checks out anything in the source checkout.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from printable import printable

FOLDER_PREFIX = "relay-scratch-"


class ScratchCheckoutFailed(Exception):
    """The scratch checkout could not be made. The message says why, in a line."""


def _git(*args: str, cwd: Path | None = None, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, input=stdin, capture_output=True)


def _first_line(output: bytes) -> str:
    lines = output.decode("utf-8", errors="replace").strip().splitlines()
    return lines[0] if lines else "no output"


@contextmanager
def scratch_checkout(source: Path, commit: str, overlay_from: str | None = None,
                     overlay_paths: list[str] | tuple[str, ...] = ()):
    """Yield a folder holding `commit` of the repo at `source`, checked out
    detached, with no ignored or untracked files. With `overlay_paths`,
    those files are then taken from `overlay_from` (the branch's versions of
    its changed tests over the base's code, say). The folder is deleted when
    the block ends, also on an error. Raises ScratchCheckoutFailed when git
    cannot make it."""
    folder = Path(tempfile.mkdtemp(prefix=FOLDER_PREFIX))
    try:
        # --shared borrows the source's objects instead of copying them, so
        # the commit is there even when no branch points at it. Hooks are
        # switched off: nothing of the repo's runs before the test command.
        _checked(_git("clone", "--quiet", "--shared", "--no-checkout",
                      "-c", "core.hooksPath=/dev/null", str(source), str(folder)), "clone")
        _checked(_git("checkout", "--quiet", "--detach", commit, cwd=folder), "check out the commit")
        if overlay_paths:
            listed = _nul_listed(overlay_paths)
            _checked(_git("--literal-pathspecs", "checkout", "--quiet", overlay_from,
                          "--pathspec-from-file=-", "--pathspec-file-nul", cwd=folder, stdin=listed),
                     "check out the branch's files")
        yield folder
    finally:
        remove_folder(folder)


def _nul_listed(paths: list[str] | tuple[str, ...]) -> bytes:
    """The paths as UTF-8, each ended by a NUL, for --pathspec-file-nul.
    Raises ScratchCheckoutFailed for a name read from git that is not UTF-8
    (its undecodable bytes are held as surrogates, which UTF-8 cannot hold)."""
    listed = []
    for path in paths:
        try:
            listed.append(path.encode("utf-8") + b"\0")
        except UnicodeEncodeError:
            raise ScratchCheckoutFailed(f"a changed test file's name is not UTF-8: {printable(path)}") from None
    return b"".join(listed)


def _checked(result: subprocess.CompletedProcess, what: str) -> None:
    if result.returncode != 0:
        raise ScratchCheckoutFailed(f"git could not {what} in a scratch folder: {_first_line(result.stderr)}")


def remove_folder(folder: Path) -> None:
    """Delete a folder this module made, read-only entries included (a test
    command may leave some). Only ever called with a folder mkdtemp made."""
    def make_writable_and_retry(function, path, _problem):
        try:
            # A link is never chmod-ed: that would change the file it points
            # to, which may be anywhere. Its folder is inside this one.
            if not os.path.islink(path):
                os.chmod(path, stat.S_IRWXU)
            os.chmod(os.path.dirname(path), stat.S_IRWXU)
            function(path)
        except OSError:
            pass

    # Python: shutil.rmtree's onerror became onexc in 3.12.
    if sys.version_info >= (3, 12):
        shutil.rmtree(folder, onexc=make_writable_and_retry)
    else:
        shutil.rmtree(folder, onerror=make_writable_and_retry)
