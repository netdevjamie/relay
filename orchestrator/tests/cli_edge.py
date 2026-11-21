"""Run a `relay` command the way the shell does: through relay.main, the
CLI edge, the one place a command's exit code becomes the process's exit.
A test that asserts SystemExit goes through here, because the commands
themselves return their exit code instead of exiting."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import relay
import settings


def run_relay(config: dict, *arguments: str) -> None:
    """`relay <arguments>` with `config` as the loaded config. Raises
    SystemExit exactly where the real command line would exit non-zero."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "load_config", lambda: config)
        patch.setattr(sys, "argv", ["relay", *arguments])
        relay.main()
