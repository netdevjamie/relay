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

A throwaway home has no git identity, so set `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME` and `GIT_COMMITTER_EMAIL` before you commit in it. If your `python3` comes from a version manager that keeps its files under your home folder, put that Python's own `bin` folder on `PATH` instead. [`docs/known-limits.md`](docs/known-limits.md) lists what else the install writes.

## Code layout (`orchestrator/`)

Each module in the first table imports only from the modules above it and from the second table. The modules in the second table import none of the first when loaded.

| Module | What it holds |
| --- | --- |
| `settings.py` | Paths, the phase and verdict vocabulary, config, the repo registry, the ledger connection |
| `run_state.py` | Loading and saving a task's state, reading artifacts, retry counts, consistency checks |
| `arbiter.py` | The Arbiter's plumbing: which report calls it, what its ruling must say for Relay to act on it, and the count of review loops |
| `sizing.py` | A task's size (fix, feature, project) and whether it is risky, from its brief's frontmatter, and the raise a Reviewer's or Arbiter's report may make; how much of the chain it gets |
| `routing.py` | The routing table and `decide_routing`, the one decision `relay next` and the read API share |
| `risk_record.py` | The sizing hint and the judging roles' answers in the ledger, the owner's `relay resize`, and the words about them in prompts, status and the card |
| `proto.py` | Prototype mode: `relay proto` and `relay promote` |
| `role_context.py` | What a role is told to read: the required reading and the optional history, from the task folder's file names |
| `agents.py` | Building an agent's prompt; starting it with its model, effort, permission mode and time limit |
| `recorded_runs.py` | Relay's own test runs in the ledger (uses `evidence.py` to run them) |
| `owner_notes.py` | What the owner said at a gate or at any time, read back and turned into the paragraph every later agent is given |
| `arbiter_record.py` | What Relay hands the Arbiter, the words it hands on after a ruling, and the rulings and accepted follow-ups read back from the ledger |
| `read_api.py` | The JSON read model: task, latest step, gate |
| `board_view.py` | A gate's words, built from the read model and the report files: a task's one-line state, the headline, the ask and the findings |
| `gate_card.py` | The phone's text: what waits on you, one gate's card with named choices, a report by name |
| `commands.py` | Every `relay` command |
| `relay.py` | Argument parsing and `main` |

| Module | What it holds |
| --- | --- |
| `db.py` | The SQLite ledger: its tables, its upgrades, and every write |
| `migrate_legacy.py` | The one-time move from task files to the ledger, and `relay recover --from-projections` |
| `evidence.py` | Running a repo's test command and reading its result |
| `test_harness.py` | Which files make up a repo's test harness, and which tests a branch changed |
| `seen_to_fail.py` | Whether a branch's new or changed tests fail at its base |
| `scratch_checkout.py` | The throwaway checkouts Relay's extra test runs use |
| `project_profile.py` | Reading and checking a repo's `.relay/profile.yaml` |
| `risk_hint.py` | The sizing hint, from the paths a branch changed |
| `bounded_yaml.py` | Reading YAML Relay did not write (a profile, a report's frontmatter) with limits on size and shape |
| `printable.py` | Untrusted text made safe to print on one line |
| `report_reader.py` | A report's summary line and rated findings |
| `markdown_blocks.py` | A report's text as a list of blocks (headings, tables, code) that `report_reader.py` reads |
| `doctor.py` | `relay doctor`: checks that an install can run a task |
| `awake.py` | Keeping a Mac awake while a role runs |

Values that tests replace (`TASKS_DIR`, `REGISTRY_PATH`, `CONFIG_PATH`, `load_config`, the ledger connection) are read as `settings.NAME` everywhere, never imported by name.

## What a good change looks like

- **Readable first.** Descriptive names and small functions that each do one thing. Comments say why the code is the way it is, not the story of how it got there.
- **A test that proves it.** A fix comes with a test that fails without the fix. A behavior change updates the tests that pin the old behavior, on purpose.
- **Docs that match.** If a command, a gate or a role prompt changes, update the README, `docs/` or `context/` in the same pull request. A change that ships with something known wrong adds a row to `docs/known-limits.md`.
- **Small role prompts.** The prompts in `agents/` are short on purpose, and `orchestrator/tests/test_agent_prompts.py` holds five of them (Builder, Reviewer, Tester, Security and Arbiter) to a word ceiling. A prompt change should say what it fixes.

`context/PROJECT.md` lists the principles every change is held to, and `context/DECISIONS.md` explains why things are the way they are. Read both before proposing a change to the routing, the gates or the ledger.

## Reporting a security problem

Please don't open a public issue for a security problem. Report it privately with GitHub's private vulnerability reporting: open the **Security** tab of the repository and click **Report a vulnerability**. If that button isn't there, open an issue that asks for a private contact and leaves out the details.
