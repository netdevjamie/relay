# Contributing to Relay

Thanks for taking a look. Relay is a small, opinionated tool, so the best first step for anything bigger than a bug fix is an issue that describes the problem. That way we can agree on the shape before you write code.

## Setting up

```bash
git clone https://github.com/netdevjamie/relay.git
cd relay
python3 -m venv .venv
.venv/bin/pip install -r orchestrator/requirements.txt pytest
.venv/bin/pytest -q orchestrator/tests tools/tests
```

The suite needs Python 3.11 or newer. If `python3 --version` says older, make the venv with a newer one, such as `python3.12 -m venv .venv`.

To try your change as a user would, install it into a throwaway home folder. `./setup.sh` always writes the role prompts to `~/.claude/agents` and the `relay` link to `~/.local/bin`, whatever `RELAY_HOME` says, so a throwaway `RELAY_HOME` alone still changes your real install. A throwaway `HOME`, with `RELAY_HOME` unset, keeps all three apart:

```bash
TRY_HOME="$(mktemp -d)"
env -u RELAY_HOME HOME="$TRY_HOME" ./setup.sh
env -u RELAY_HOME HOME="$TRY_HOME" "$TRY_HOME/.local/bin/relay" doctor
```

[`docs/known-limits.md`](docs/known-limits.md) lists what else the install writes.

## What a good change looks like

- **Readable first.** Descriptive names and small functions that each do one thing. Comments say why the code is the way it is, not the story of how it got there.
- **A test that proves it.** A fix comes with a test that fails without the fix. A behavior change updates the tests that pin the old behavior, on purpose.
- **Docs that match.** If a command, a gate or a role prompt changes, update the README, `docs/` or `context/` in the same pull request.
- **Small role prompts.** The prompts in `agents/` are short on purpose, and `orchestrator/tests/test_agent_prompts.py` holds four of them (Builder, Reviewer, Tester and Security) to a word ceiling. A prompt change should say what it fixes.

`context/PROJECT.md` lists the principles every change is held to, and `context/DECISIONS.md` explains why things are the way they are. Read both before proposing a change to the routing, the gates or the ledger.

## Reporting a security problem

Please don't open a public issue. Use GitHub's private vulnerability reporting on this repository instead: the "Report a vulnerability" button on the Security tab. If you can't find it, open an issue that asks for a private contact and leaves out the details.
