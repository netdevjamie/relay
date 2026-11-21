"""A scripted walk of one task through `relay start`, `relay next` and the
owner's commands on a temp sqlite ledger, recording what the ledger holds
after every action: the new events, every steps row, the retry counts,
`relay status --json`, `relay gate-check --json` and run.yaml, and for each
dispatch the report file the prompt told the agent to write.

test_ledger_golden.py compares a fresh walk with the record in
fixtures/ledger_golden/walk.json, which was written by this script on the
code from before the ledger's explicit write calls:

    cd orchestrator && python3 tests/ledger_golden_walk.py --record

Nothing here opens a real ledger: the walk builds its own in a temp folder.
"""

import copy
import json
import re
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents
import commands
import db
import read_api
import settings
from settings import DEFAULT_CONFIG, VALID_PHASES, VALID_STATUSES, VALID_VERDICTS
from tests.arbiter_fixtures import finish_arbiter, passing_runs

GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "ledger_golden" / "walk.json"
TASK_ID = "golden"
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
OUTPUT_PATH = re.compile(r"Write your handoff artifact to (\S+\.md)\.\n")


class Walk:
    """One task on a temp ledger, and the record of what each action left."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp_path = tmp_path
        self.config = _sqlite_config(tmp_path, monkeypatch)
        self.prompts: list[tuple[str, str]] = []
        self.launched_inside_a_transaction: list[str] = []
        self.actions: list[dict] = []
        self.events_seen = 0
        monkeypatch.setattr(agents, "invoke_agent", self._capture_launch)
        passing_runs(monkeypatch)

    def _capture_launch(self, phase, task_id, prompt, config, auto, cwd=None, step=None, **_):
        if settings.get_db_connection().in_transaction:
            self.launched_inside_a_transaction.append(phase)
        self.prompts.append((phase, prompt))
        return 0

    def finish(self, verdict: str, extra_frontmatter: str = "") -> None:
        """Play the agent: write the latest step's report with this verdict."""
        latest = read_api.status_payload(TASK_ID, self.config)["latest_step"]
        (settings.TASKS_DIR / TASK_ID / latest["artifact"]).write_text(
            f"---\ntask_id: {TASK_ID}\nphase: {latest['phase']}\nstep: {latest['step']}\n"
            f"verdict: {verdict}\n{extra_frontmatter}---\n\nBody.\n")

    def act(self, name: str, run) -> None:
        """Run one action, check it succeeded, and record what it left."""
        prompts_before = len(self.prompts)
        assert run() in (0, None), f"{name} did not succeed"
        dispatched = [_reported_file(prompt) for _, prompt in self.prompts[prompts_before:]]
        self.actions.append(self._snapshot(name, dispatched))

    def _snapshot(self, name: str, dispatched: list[str]) -> dict:
        conn = settings.get_db_connection()
        events = db.get_events(conn, TASK_ID)
        new_events, self.events_seen = events[self.events_seen:], len(events)
        steps = [dict(row) for row in conn.execute(
            "SELECT step, phase, verdict, artifact_filename FROM steps "
            "WHERE task_id = ? ORDER BY step", (TASK_ID,))]
        run_yaml = (settings.TASKS_DIR / TASK_ID / "run.yaml").read_text()
        return self._normalized({
            "action": name,
            "events": new_events,
            "steps": steps,
            "retries": db.get_retry_counts(conn, TASK_ID),
            "status": read_api.status_payload(TASK_ID, self.config),
            "gate_check": read_api.gate_check_payload(TASK_ID, self.config),
            "run_yaml": run_yaml,
            "dispatched": dispatched,
        })

    def _normalized(self, value):
        text = json.dumps(value, sort_keys=True)
        text = text.replace(str(self.tmp_path), "<TMP>")
        return json.loads(TIMESTAMP.sub("<T>", text))


def _reported_file(prompt: str) -> str:
    """The report file a prompt told its agent to write, by name."""
    match = OUTPUT_PATH.search(prompt)
    assert match, "the prompt names no report file"
    return Path(match.group(1)).name


def _sqlite_config(tmp_path: Path, monkeypatch) -> dict:
    """A sqlite config on a fresh temp ledger, with the owner's gate after
    the plan on and the gate after the brief off."""
    monkeypatch.setattr(settings, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(settings, "_db_connection", None)
    db_path = tmp_path / "relay.db"
    monkeypatch.setenv("RELAY_DB_PATH", str(db_path))
    (tmp_path / "tasks").mkdir()
    db.configure(VALID_PHASES, VALID_VERDICTS, VALID_STATUSES)
    db.init_database(db_path).close()
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["persistence_backend"] = "sqlite"
    config["human_gates"]["after_designer"] = False
    config["human_gates"]["after_architect"] = True
    return config


def _write_a_reviewer_retry_from_an_older_ledger() -> int:
    """A `retry` event for the Reviewer, as a ledger written before the
    automatic-rounds rule holds one. No route on this code goes back to the
    Reviewer, so this is the only way a walk dispatches a reviewer pass 2."""
    conn = settings.get_db_connection()
    with conn:
        conn.execute("INSERT INTO events (task_id, event_type, phase, timestamp) VALUES (?,?,?,?)",
                     (TASK_ID, db.EVENT_RETRY, "reviewer", "2026-10-07T12:00:00Z"))
    return 0


def walk(tmp_path: Path, monkeypatch) -> Walk:
    """The walk: every action type the save path has, in one task's life."""
    walk = Walk(tmp_path, monkeypatch)
    config = walk.config
    task_dir = settings.TASKS_DIR / TASK_ID
    task_dir.mkdir()
    (task_dir / "brief.md").write_text("# Brief\n")

    def next_():
        return commands.cmd_next(TASK_ID, auto=False, config=config)

    def finish_then_next(verdict: str, extra_frontmatter: str = ""):
        def run():
            walk.finish(verdict, extra_frontmatter)
            return next_()
        return run

    walk.act("start", lambda: commands.cmd_start(TASK_ID, auto=False, config=config))
    walk.act("designer ready-for-architect", finish_then_next("ready-for-architect"))
    walk.act("architect ready-for-builder, deploy yes", finish_then_next("ready-for-builder",
                                                                         "deploy: yes\n"))
    walk.act("override builder with a note",
             lambda: commands.cmd_override(TASK_ID, "builder", config, note="Go ahead."))
    walk.act("builder ready-for-review", finish_then_next("ready-for-review"))
    walk.act("reviewer changes_requested", finish_then_next("changes_requested"))
    walk.act("a reviewer retry from an older ledger", _write_a_reviewer_retry_from_an_older_ledger)
    walk.act("builder fix ready-for-review", finish_then_next("ready-for-review"))
    walk.act("reviewer approved_with_warnings", finish_then_next("approved_with_warnings"))

    def arbiter_escalates():
        finish_arbiter(TASK_ID, config, "escalate")
        return next_()

    walk.act("arbiter escalates", arbiter_escalates)
    walk.act("send-back with a note",
             lambda: commands.cmd_send_back(TASK_ID, config, note="Fix the warnings."))
    walk.act("builder fix ready-for-review again", finish_then_next("ready-for-review"))
    walk.act("reviewer approved", finish_then_next("approved"))
    walk.act("tester pass", finish_then_next("pass"))
    walk.act("security secure", finish_then_next("secure"))
    walk.act("ops deploy-ready", finish_then_next("deploy-ready"))
    walk.act("override done with a note",
             lambda: commands.cmd_override(TASK_ID, "done", config, note="Shipped."))
    walk.act("note", lambda: commands.cmd_note(TASK_ID, config, "Recorded after the close."))
    settings.close_ledger()
    return walk


def record() -> None:
    """Write the golden record from a walk on the code as it is now."""
    with tempfile.TemporaryDirectory() as folder, pytest.MonkeyPatch.context() as monkeypatch:
        done = walk(Path(folder), monkeypatch)
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(json.dumps(done.actions, indent=1, sort_keys=True) + "\n")
    print(f"Recorded {len(done.actions)} actions to {GOLDEN_PATH}")


if __name__ == "__main__":
    if sys.argv[1:] != ["--record"]:
        sys.exit("usage: python3 tests/ledger_golden_walk.py --record")
    record()
