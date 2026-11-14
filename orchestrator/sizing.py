"""How big a task is and whether it is risky, as its brief says.

Relay sizes the chain to the task instead of running all seven roles on
everything:

- size `fix`: a one-line definition of fixed; no Designer, no Architect.
  The task starts at the Builder.
- size `feature`: the Designer writes the brief; the Architect runs only
  when the Designer says the structure changes (`ready-for-architect`).
- size `project`: the Designer, then always the Architect.
- `risky: yes` (auth, input, money, production data): the Tester and
  Security run after the Reviewer. `risky: no`: the Reviewer is the last
  check before Ops (deploy tasks) or done.

All three come from the frontmatter of the task's `brief.md`, which the
owner (or the session acting for them) writes before `relay start`: `size`,
`risky`, and, for a fix or a feature (which may never reach the Architect,
whose plan otherwise decides it), `deploy: yes|no`. A brief that leaves any
of them out, says something else, or cannot be read gets the full chain:
size `project`, risky. Nothing here is ever guessed
toward less checking.

`relay start` records what it read in the ledger (a `sized` event), and
every later read takes the recorded sizing, so a brief edited mid-run
(by a role, say) changes nothing.
"""

from dataclasses import dataclass

import json

import bounded_yaml
import db
import settings

SIZES = ("fix", "feature", "project")
FULL_CHAIN_SIZE = "project"


@dataclass(frozen=True)
class Sizing:
    size: str
    risky: bool
    # Why the full chain was used in place of what the brief says, or None
    # when the brief said all it must and all of it was read.
    note: str | None = None
    # The brief's deploy: yes|no; None on the full chain, where the
    # Architect's plan decides.
    deploy: bool | None = None


FULL_CHAIN = Sizing(FULL_CHAIN_SIZE, True)


def _full_chain(why: str) -> Sizing:
    return Sizing(FULL_CHAIN_SIZE, True, f"{why}, so the full chain runs")


def _risky(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("yes", "true"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("no", "false"):
        return False
    return None


def sizing_in(text: str) -> Sizing:
    """The sizing a brief's text gives. Any field missing or not one of
    its words gives the whole full chain, not just that part of it."""
    try:
        data = bounded_yaml.frontmatter(text)
    except bounded_yaml.NotRead as problem:
        return _full_chain(f"the brief's frontmatter {problem}")
    size, risky, deploy = data.get("size"), _risky(data.get("risky")), _risky(data.get("deploy"))
    problems = []
    if size not in SIZES:
        problems.append("size: fix|feature|project" if size is None
                        else f"size {size!r} (not fix, feature or project)")
    if risky is None:
        problems.append("risky: yes|no" if data.get("risky") is None
                        else f"risky {data.get('risky')!r} (not yes or no)")
    if size in ("fix", "feature") and deploy is None:
        problems.append("deploy: yes|no" if data.get("deploy") is None
                        else f"deploy {data.get('deploy')!r} (not yes or no)")
    if problems:
        return _full_chain(f"the brief does not give {' or '.join(problems)}")
    return Sizing(size, risky, None, deploy)


def read_brief_sizing(task_id: str) -> Sizing:
    """The sizing the task's brief gives now; the full chain when it has none."""
    brief = settings.TASKS_DIR / task_id / "brief.md"
    try:
        text = brief.read_text()
    except (OSError, UnicodeDecodeError):
        return _full_chain("the brief could not be read")
    return sizing_in(text)


def record(task_id: str, sizing: Sizing, config: dict) -> None:
    """Record the sizing `relay start` read, as a `sized` ledger event."""
    if not settings._sqlite_enabled(config):
        return
    db.record_event(settings.get_db_connection(), task_id, db.EVENT_SIZED,
                    {"size": sizing.size, "risky": sizing.risky, "deploy": sizing.deploy,
                     "note": sizing.note})


def read_sizing(task_id: str, config: dict | None = None) -> Sizing:
    """The sizing a task routes on: the one recorded at its start when there
    is one, else what its brief says (a task started before sizing was
    recorded, or the files backend)."""
    if config is not None and settings._sqlite_enabled(config):
        for event in reversed(db.get_events(settings.get_db_connection(), task_id)):
            if event["event_type"] != db.EVENT_SIZED:
                continue
            try:
                payload = event["payload"]
                payload = json.loads(payload) if isinstance(payload, str) else payload
                size, risky = payload["size"], payload["risky"]
                if size not in SIZES or not isinstance(risky, bool):
                    raise ValueError
                deploy = payload.get("deploy")
                return Sizing(size, risky, payload.get("note"),
                              deploy if isinstance(deploy, bool) else None)
            except (TypeError, ValueError, KeyError, AttributeError):
                return _full_chain("the recorded sizing could not be read")
    return read_brief_sizing(task_id)


def first_phase(sizing: Sizing) -> str:
    """Where a task of this size starts."""
    return "builder" if sizing.size == "fix" else settings.FIRST_PHASE


def phases_it_may_skip(sizing: Sizing) -> set[str]:
    """The phases a task of this sizing can pass by without anything being
    wrong: the Architect on a fix or a feature, the Designer on a fix, and
    the Tester and Security on a task that is not risky."""
    skippable = set()
    if sizing.size in ("fix", "feature"):
        skippable.add("architect")
    if sizing.size == "fix":
        skippable.add("designer")
    if not sizing.risky:
        skippable |= {"tester", "security"}
    return skippable


def summary(sizing: Sizing) -> str:
    """One line for `relay start` and `relay status`."""
    line = f"Size: {sizing.size}; risky: {'yes' if sizing.risky else 'no'}"
    return f"{line} ({sizing.note})" if sizing.note else line
