"""Contract tests for the JSON read API: relay status|list|gate-check --json.

Front ends are written against these shapes (docs/json-api.md). A test here
failing means a front end would break: change the shape only together with
JSON_SCHEMA_VERSION.
"""

import json
import os
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
import relay
import settings
import run_state
import routing
import read_api
import commands
from settings import DEFAULT_CONFIG, VALID_PHASES, VALID_STATUSES, VALID_VERDICTS


ENVELOPE_KEYS = {"schema", "schema_version", "ok"}
TASK_SUMMARY_KEYS = {"task_id", "repo", "status", "current_phase", "deploy_intent",
                     "created", "updated"}
GATE_KEYS = {"state", "kind", "next_phase", "reason", "approve_command",
             "send_back_command", "send_back_recommended"}
LATEST_STEP_KEYS = {"step", "phase", "verdict", "recorded", "started", "artifact",
                    "artifact_path"}
LIST_ROW_KEYS = TASK_SUMMARY_KEYS | {"gate_state", "gate_kind", "automatic_rounds", "fix_rounds",
                                     "latest_step"}
LIST_LATEST_STEP_KEYS = {"step", "phase", "verdict", "started"}
HISTORY_ENTRY_KEYS = {"step", "phase", "verdict", "timestamp", "artifact"}


@pytest.fixture
def sqlite_config(tmp_path, monkeypatch):
    """An isolated, already-initialised ledger and task folder."""
    monkeypatch.setattr(settings, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(settings, "_db_connection", None)
    db_path = tmp_path / "relay.db"
    monkeypatch.setenv("RELAY_DB_PATH", str(db_path))
    (tmp_path / "tasks").mkdir()
    db.configure(VALID_PHASES, VALID_VERDICTS, VALID_STATUSES)
    db.init_database(db_path).close()
    config = dict(DEFAULT_CONFIG)
    config["persistence_backend"] = "sqlite"
    config["human_gates"] = dict(DEFAULT_CONFIG["human_gates"])
    yield config
    if settings._db_connection is not None:
        settings._db_connection.close()


def start_task(task_id: str, config: dict) -> Path:
    task_dir = settings.TASKS_DIR / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "brief.md").write_text("# Brief\n")
    commands.cmd_start(task_id, auto=False, config=config)
    return task_dir


def write_artifact(task_dir: Path, name: str, phase: str, step: int, verdict: str,
                   extra_frontmatter: str = "") -> None:
    (task_dir / name).write_text(
        f"---\ntask_id: {task_dir.name}\nphase: {phase}\nstep: {step}\n"
        f"verdict: {verdict}\n{extra_frontmatter}---\n\nBody.\n")


def advance_to_architect(task_id: str, config: dict) -> Path:
    """Start a task and move it past the brief gate, leaving the Architect running."""
    task_dir = start_task(task_id, config)
    write_artifact(task_dir, "01-designer.md", "designer", 1, "ready-for-architect")
    commands.cmd_override(task_id, "architect", config)
    return task_dir


def finish_step(task_id: str, config: dict, verdict: str, extra_frontmatter: str = "") -> None:
    """Play the agent: write the current step's artifact with this verdict."""
    latest = read_api.status_payload(task_id, config)["latest_step"]
    write_artifact(settings.TASKS_DIR / task_id, latest["artifact"], latest["phase"],
                   latest["step"], verdict, extra_frontmatter)


def gate_then_next(task_id: str, config: dict, capsys) -> tuple[dict, str, dict]:
    """Ask gate-check, then really run `relay next`. Returns (gate, next's output, state after)."""
    gate = read_api.gate_check_payload(task_id, config)["gate"]
    capsys.readouterr()
    commands.cmd_next(task_id, auto=False, config=config)
    return gate, capsys.readouterr().out, run_state.load_run_state(task_id, config)


def ledger_snapshot(config: dict) -> dict:
    conn = settings.get_db_connection()
    return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
            for table in ("tasks", "steps", "events")}


class TestSchemas:
    """Exact key sets: the contract front ends are written against."""

    def test_status_schema(self, sqlite_config):
        start_task("shape-status", sqlite_config)
        payload = read_api.status_payload("shape-status", sqlite_config)
        assert set(payload) == ENVELOPE_KEYS | {"task", "retries", "max_retries", "history",
                                                 "latest_step", "gate", "approvals", "test_runs",
                                                 "interruptions", "owner_notes"}
        assert payload["owner_notes"] == []
        assert payload["test_runs"] == []
        assert payload["interruptions"] == []
        assert payload["schema"] == "relay.status"
        assert payload["schema_version"] == 3
        assert payload["ok"] is True
        assert set(payload["task"]) == TASK_SUMMARY_KEYS | {"next_step", "worktree_path",
                                                            "branch", "base"}
        assert set(payload["gate"]) == GATE_KEYS
        assert set(payload["latest_step"]) == LATEST_STEP_KEYS
        assert [set(entry) for entry in payload["history"]] == [HISTORY_ENTRY_KEYS]

    def test_gate_check_schema(self, sqlite_config):
        start_task("shape-gate", sqlite_config)
        payload = read_api.gate_check_payload("shape-gate", sqlite_config)
        assert set(payload) == ENVELOPE_KEYS | {"task_id", "status", "current_phase",
                                                 "latest_step", "gate", "approvals"}
        assert payload["schema"] == "relay.gate-check"
        assert set(payload["gate"]) == GATE_KEYS
        assert set(payload["latest_step"]) == LATEST_STEP_KEYS

    def test_list_schema(self, sqlite_config):
        start_task("shape-list", sqlite_config)
        payload = read_api.list_payload(sqlite_config)
        assert set(payload) == ENVELOPE_KEYS | {"tasks"}
        assert payload["schema"] == "relay.list"
        assert [set(task) for task in payload["tasks"]] == [LIST_ROW_KEYS]
        assert set(payload["tasks"][0]["latest_step"]) == LIST_LATEST_STEP_KEYS

    def test_error_codes_are_the_documented_names(self):
        assert (read_api.ERROR_TASK_NOT_FOUND, read_api.ERROR_LEDGER_UNAVAILABLE,
                read_api.ERROR_REPO_NOT_FOUND, read_api.ERROR_INTERNAL) == (
            "task_not_found", "ledger_unavailable", "repo_not_found", "internal_error")

    def test_error_schema(self):
        payload = read_api.error_payload(read_api.ERROR_TASK_NOT_FOUND, "No run state for x.")
        assert payload == {"schema": "relay.error", "schema_version": 3, "ok": False,
                           "error": {"code": "task_not_found", "message": "No run state for x."}}

    def test_approval_entry_schema(self, sqlite_config):
        advance_to_architect("shape-approval", sqlite_config)
        approvals = read_api.status_payload("shape-approval", sqlite_config)["approvals"]
        assert len(approvals) == 1
        assert set(approvals[0]) == {"phase", "next_phase", "method", "note", "timestamp"}
        assert approvals[0]["note"] is None
        assert approvals[0]["phase"] == "designer"
        assert approvals[0]["next_phase"] == "architect"
        assert approvals[0]["method"] == "override"

    def test_every_payload_is_json_serialisable(self, sqlite_config):
        advance_to_architect("shape-json", sqlite_config)
        for payload in (read_api.status_payload("shape-json", sqlite_config),
                        read_api.gate_check_payload("shape-json", sqlite_config),
                        read_api.list_payload(sqlite_config)):
            assert json.loads(json.dumps(payload)) == payload

    def test_gate_states_and_kinds_are_the_documented_names(self):
        assert (read_api.GATE_READY, read_api.GATE_WAITING_ON_AGENT, read_api.GATE_WAITING_ON_OWNER,
                read_api.GATE_CLOSED) == ("ready", "waiting_on_agent", "waiting_on_owner", "closed")
        assert (routing.GATE_APPROVAL, routing.GATE_DEPLOY_APPROVAL, routing.GATE_DEPLOY_FLAG_MISSING,
                routing.GATE_OWNER_DECISION, routing.GATE_RETRY_LIMIT, routing.GATE_BLOCKED) == (
            "approval", "deploy_approval", "deploy_flag_missing", "owner_decision", "retry_limit",
            "blocked")


class TestStepTimesAndFiles:
    """What a reader needs beyond the gate: when the newest step was
    dispatched, and which file each step was told to write."""

    def test_latest_step_started_is_its_history_timestamp(self, sqlite_config):
        advance_to_architect("times", sqlite_config)
        status = read_api.status_payload("times", sqlite_config)
        assert status["latest_step"]["step"] == 2
        assert status["latest_step"]["started"] == status["history"][1]["timestamp"]
        assert status["latest_step"]["started"] is not None
        gate_check = read_api.gate_check_payload("times", sqlite_config)
        assert gate_check["latest_step"]["started"] == status["latest_step"]["started"]

    def test_list_row_carries_the_latest_step_without_a_path(self, sqlite_config):
        advance_to_architect("times-list", sqlite_config)
        row = read_api.list_payload(sqlite_config)["tasks"][0]
        latest = read_api.status_payload("times-list", sqlite_config)["latest_step"]
        assert row["latest_step"] == {"step": 2, "phase": "architect", "verdict": None,
                                      "started": latest["started"]}

    def test_history_names_the_file_recorded_for_each_step(self, sqlite_config):
        """The name is recorded at dispatch: step 2's file is not written yet."""
        task_dir = advance_to_architect("files", sqlite_config)
        history = read_api.status_payload("files", sqlite_config)["history"]
        assert [(entry["step"], entry["artifact"]) for entry in history] == [
            (1, "01-designer.md"), (2, "02-architect.md")]
        assert not (task_dir / "02-architect.md").exists()

    def test_the_file_name_is_not_added_to_run_yaml(self, sqlite_config):
        task_dir = advance_to_architect("projection", sqlite_config)
        assert "artifact" not in (task_dir / "run.yaml").read_text()
        assert all("artifact" not in entry
                   for entry in db.get_history(settings.get_db_connection(), "projection"))

    def test_close_ledger_drops_the_connection_and_is_safe_to_repeat(self, sqlite_config):
        first = settings.get_db_connection()
        settings.close_ledger()
        assert settings._db_connection is None
        with pytest.raises(sqlite3.ProgrammingError):
            first.execute("SELECT 1")
        settings.close_ledger()
        assert settings.get_db_connection() is not first


class TestGateStates:
    def gate(self, task_id, config):
        return read_api.gate_check_payload(task_id, config)["gate"]

    def test_waiting_on_agent_until_the_artifact_has_a_verdict(self, sqlite_config):
        start_task("gate-agent", sqlite_config)
        payload = read_api.gate_check_payload("gate-agent", sqlite_config)
        assert payload["gate"]["state"] == "waiting_on_agent"
        assert payload["gate"]["kind"] is None
        assert payload["latest_step"]["verdict"] is None
        assert payload["latest_step"]["recorded"] is False

    def test_brief_gate_waits_on_owner_with_the_approve_command(self, sqlite_config):
        task_dir = start_task("gate-brief", sqlite_config)
        write_artifact(task_dir, "01-designer.md", "designer", 1, "ready-for-architect")
        payload = read_api.gate_check_payload("gate-brief", sqlite_config)
        assert payload["gate"] == {
            "state": "waiting_on_owner", "kind": "approval", "next_phase": "architect",
            "reason": "designer verdict: ready-for-architect, requires human approval to proceed",
            "approve_command": f"{settings._CLI_CMD} override gate-brief architect",
            "send_back_command": f"{settings._CLI_CMD} send-back gate-brief",
            "send_back_recommended": False,
        }
        assert payload["latest_step"]["verdict"] == "ready-for-architect"
        assert payload["latest_step"]["artifact"] == "01-designer.md"
        assert payload["latest_step"]["artifact_path"] == str(task_dir / "01-designer.md")

    def test_ready_when_next_would_advance_without_the_owner(self, sqlite_config):
        sqlite_config["human_gates"]["after_designer"] = False
        task_dir = start_task("gate-ready", sqlite_config)
        write_artifact(task_dir, "01-designer.md", "designer", 1, "ready-for-architect")
        gate = self.gate("gate-ready", sqlite_config)
        assert (gate["state"], gate["kind"], gate["next_phase"]) == ("ready", None, "architect")
        assert gate["approve_command"] is None

    def test_owner_decision_when_there_is_no_suggested_phase(self, sqlite_config):
        task_dir = start_task("gate-decision", sqlite_config)
        write_artifact(task_dir, "01-designer.md", "designer", 1, "needs-human-input")
        gate = self.gate("gate-decision", sqlite_config)
        assert (gate["state"], gate["kind"], gate["next_phase"]) == (
            "waiting_on_owner", "owner_decision", None)
        assert gate["approve_command"] is None

    def test_plan_without_a_deploy_flag_waits_on_owner(self, sqlite_config):
        sqlite_config["human_gates"]["after_architect"] = False
        task_dir = advance_to_architect("gate-noflag", sqlite_config)
        write_artifact(task_dir, "02-architect.md", "architect", 2, "ready-for-builder")
        gate = self.gate("gate-noflag", sqlite_config)
        assert (gate["state"], gate["kind"]) == ("waiting_on_owner", "deploy_flag_missing")

    def test_plan_with_a_deploy_flag_is_ready(self, sqlite_config):
        sqlite_config["human_gates"]["after_architect"] = False
        task_dir = advance_to_architect("gate-flag", sqlite_config)
        write_artifact(task_dir, "02-architect.md", "architect", 2, "ready-for-builder",
                       extra_frontmatter="deploy: no\n")
        gate = self.gate("gate-flag", sqlite_config)
        assert (gate["state"], gate["next_phase"]) == ("ready", "builder")

    def test_closed_once_the_task_is_completed_or_abandoned(self, sqlite_config):
        start_task("gate-done", sqlite_config)
        commands.cmd_override("gate-done", "done", sqlite_config)
        assert self.gate("gate-done", sqlite_config)["state"] == "closed"

        start_task("gate-aborted", sqlite_config)
        commands.cmd_abort("gate-aborted", sqlite_config)
        assert self.gate("gate-aborted", sqlite_config)["state"] == "closed"

    def test_blocked_task_is_never_ready_even_when_routing_would_advance(self, sqlite_config):
        """A paused task whose latest verdict would route straight on must
        still read as waiting on the owner: a gatekeeper acts on `ready`."""
        sqlite_config["human_gates"]["after_designer"] = False
        task_dir = start_task("gate-blocked", sqlite_config)
        write_artifact(task_dir, "01-designer.md", "designer", 1, "ready-for-architect")
        assert self.gate("gate-blocked", sqlite_config)["state"] == "ready"

        state = run_state.load_run_state("gate-blocked", sqlite_config)
        state["status"] = "blocked"
        run_state.save_run_state("gate-blocked", state, sqlite_config)

        gate = self.gate("gate-blocked", sqlite_config)
        assert (gate["state"], gate["kind"], gate["next_phase"], gate["approve_command"]) == (
            "waiting_on_owner", "blocked", None, None)
        listed = read_api.list_payload(sqlite_config)["tasks"][0]
        assert (listed["gate_state"], listed["gate_kind"]) == ("waiting_on_owner", "blocked")

    def test_unreadable_artifact_reads_as_still_waiting_on_the_agent(self, sqlite_config):
        task_dir = start_task("gate-broken", sqlite_config)
        (task_dir / "01-designer.md").write_text("---\nverdict: [unclosed\n---\n\nBody.\n")
        assert self.gate("gate-broken", sqlite_config)["state"] == "waiting_on_agent"

    def test_an_unfinished_step_is_not_read_past_the_size_cap(self, sqlite_config):
        """This read runs for every task on every refresh of the card, so
        it stops at a fixed size. Frontmatter that closes past the cap is
        not found, which shows the rest of the file was not read."""
        task_dir = start_task("gate-huge", sqlite_config)
        padding = "x" * read_api.FRONTMATTER_READ_BYTES
        (task_dir / "01-designer.md").write_text(
            f"---\nnote: {padding}\nverdict: ready-for-architect\n---\n\nBody.\n")
        assert self.gate("gate-huge", sqlite_config)["state"] == "waiting_on_agent"

    def test_a_long_report_with_short_frontmatter_still_reads(self, sqlite_config):
        """The cap may cut a character in two; that is not an unreadable file."""
        task_dir = start_task("gate-long", sqlite_config)
        head = "---\nverdict: ready-for-architect\n---\n\n"
        body = "é" * read_api.FRONTMATTER_READ_BYTES
        for lead in ("", "a"):      # one of the two cuts an "é" in half
            (task_dir / "01-designer.md").write_text(head + lead + body, encoding="utf-8")
            assert self.gate("gate-long", sqlite_config)["state"] == "waiting_on_owner"

    def test_a_named_pipe_in_place_of_the_artifact_is_not_opened(self, sqlite_config):
        """Opening a named pipe waits for a writer that never comes. A
        reader must refuse it unopened and read it as no verdict yet."""
        task_dir = start_task("gate-pipe", sqlite_config)
        pipe = task_dir / "01-designer.md"
        os.mkfifo(pipe)
        settings.close_ledger()     # a connection belongs to the thread that opened it
        answer = []

        def read():
            try:
                answer.append(self.gate("gate-pipe", sqlite_config))
            finally:
                settings.close_ledger()
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            reader.join(timeout=3)
            assert not reader.is_alive(), "the read is stuck opening the pipe"
        finally:
            try:
                os.close(os.open(pipe, os.O_WRONLY | os.O_NONBLOCK))    # frees a stuck reader
            except OSError:
                pass
            reader.join(timeout=3)
        assert answer[0]["state"] == "waiting_on_agent"

    def test_verdict_not_valid_for_the_phase_is_not_reported(self, sqlite_config):
        task_dir = start_task("gate-wrong", sqlite_config)
        write_artifact(task_dir, "01-designer.md", "designer", 1, "pass")
        payload = read_api.gate_check_payload("gate-wrong", sqlite_config)
        assert payload["latest_step"]["verdict"] is None
        assert payload["gate"]["state"] == "waiting_on_agent"

    def test_list_reports_each_tasks_gate(self, sqlite_config):
        task_dir = start_task("list-a", sqlite_config)
        write_artifact(task_dir, "01-designer.md", "designer", 1, "ready-for-architect")
        start_task("list-b", sqlite_config)
        tasks = {task["task_id"]: task for task in read_api.list_payload(sqlite_config)["tasks"]}
        assert (tasks["list-a"]["gate_state"], tasks["list-a"]["gate_kind"]) == (
            "waiting_on_owner", "approval")
        assert (tasks["list-b"]["gate_state"], tasks["list-b"]["gate_kind"]) == (
            "waiting_on_agent", None)


class TestGateCheckAgreesWithNext:
    """gate-check must predict what `relay next` then actually does."""

    def test_gate_then_next_stops_at_the_same_gate(self, sqlite_config, capsys):
        task_dir = start_task("agree-gate", sqlite_config)
        write_artifact(task_dir, "01-designer.md", "designer", 1, "ready-for-architect")
        gate = read_api.gate_check_payload("agree-gate", sqlite_config)["gate"]
        capsys.readouterr()
        commands.cmd_next("agree-gate", auto=False, config=sqlite_config)
        out = capsys.readouterr().out
        assert "HUMAN GATE" in out
        assert gate["approve_command"] in out
        assert run_state.load_run_state("agree-gate", sqlite_config)["current_phase"] == "designer"

    def test_ready_then_next_advances_to_the_predicted_phase(self, sqlite_config, capsys):
        sqlite_config["human_gates"]["after_designer"] = False
        task_dir = start_task("agree-ready", sqlite_config)
        write_artifact(task_dir, "01-designer.md", "designer", 1, "ready-for-architect")
        gate = read_api.gate_check_payload("agree-ready", sqlite_config)["gate"]
        commands.cmd_next("agree-ready", auto=False, config=sqlite_config)
        state = run_state.load_run_state("agree-ready", sqlite_config)
        assert state["current_phase"] == gate["next_phase"] == "architect"


    def test_every_step_of_a_non_deploy_task_goes_where_gate_check_said(self, sqlite_config, capsys):
        """Walk a whole task with the owner gates off. Before each `relay next`,
        gate-check must name the phase next then starts, ending in done
        (Ops skipped: the plan says deploy: no)."""
        for gate_name in sqlite_config["human_gates"]:
            sqlite_config["human_gates"][gate_name] = False
        start_task("agree-walk", sqlite_config)
        verdicts = [("ready-for-architect", ""), ("ready-for-builder", "deploy: no\n"),
                    ("ready-for-review", ""), ("approved", ""), ("pass", ""), ("secure", "")]
        visited = []
        for verdict, extra_frontmatter in verdicts:
            finish_step("agree-walk", sqlite_config, verdict, extra_frontmatter)
            gate, _, state = gate_then_next("agree-walk", sqlite_config, capsys)
            assert gate["state"] == "ready"
            assert state["current_phase"] == gate["next_phase"]
            visited.append(gate["next_phase"])
        assert visited == ["architect", "builder", "reviewer", "tester", "security", "done"]
        assert state["status"] == "completed"
        assert read_api.gate_check_payload("agree-walk", sqlite_config)["gate"]["state"] == "closed"

    def test_missing_deploy_flag_stops_next_where_gate_check_said(self, sqlite_config, capsys):
        sqlite_config["human_gates"]["after_architect"] = False
        advance_to_architect("agree-noflag", sqlite_config)
        finish_step("agree-noflag", sqlite_config, "ready-for-builder")
        gate, out, state = gate_then_next("agree-noflag", sqlite_config, capsys)
        assert (gate["state"], gate["kind"]) == ("waiting_on_owner", "deploy_flag_missing")
        assert "the plan doesn't say whether this task deploys" in out
        assert state["current_phase"] == "architect"

    def test_retry_limit_stops_next_where_gate_check_said_and_still_reads_retry_limit(
            self, sqlite_config, capsys):
        sqlite_config["max_retries"] = 1
        advance_to_architect("agree-retry", sqlite_config)
        commands.cmd_override("agree-retry", "builder", sqlite_config)
        finish_step("agree-retry", sqlite_config, "ready-for-review")
        commands.cmd_next("agree-retry", auto=False, config=sqlite_config)
        finish_step("agree-retry", sqlite_config, "changes_requested")

        gate, _, state = gate_then_next("agree-retry", sqlite_config, capsys)  # first loop back
        assert (gate["state"], gate["next_phase"]) == ("ready", "builder")
        assert state["current_phase"] == "builder"

        finish_step("agree-retry", sqlite_config, "ready-for-review")
        commands.cmd_next("agree-retry", auto=False, config=sqlite_config)
        finish_step("agree-retry", sqlite_config, "changes_requested")

        gate, out, state = gate_then_next("agree-retry", sqlite_config, capsys)  # over the limit
        assert (gate["state"], gate["kind"], gate["next_phase"]) == (
            "waiting_on_owner", "retry_limit", "builder")
        assert "STOPPED: builder has gone back for fixes 1 time in a row" in out
        assert state["status"] == "blocked"
        assert read_api.gate_check_payload("agree-retry", sqlite_config)["gate"] == gate

    def test_owner_decision_stops_next_where_gate_check_said(self, sqlite_config, capsys):
        start_task("agree-decision", sqlite_config)
        finish_step("agree-decision", sqlite_config, "needs-human-input")
        gate, out, state = gate_then_next("agree-decision", sqlite_config, capsys)
        assert (gate["state"], gate["kind"]) == ("waiting_on_owner", "owner_decision")
        assert "HUMAN GATE" in out
        assert (state["status"], state["current_phase"]) == ("in_progress", "designer")

    def test_deploy_ready_stops_next_where_gate_check_said(self, sqlite_config, capsys):
        for gate_name in sqlite_config["human_gates"]:
            sqlite_config["human_gates"][gate_name] = False
        start_task("agree-deploy", sqlite_config)
        for verdict, extra_frontmatter in [("ready-for-architect", ""),
                                           ("ready-for-builder", "deploy: yes\n"),
                                           ("ready-for-review", ""), ("approved", ""),
                                           ("pass", ""), ("secure", "")]:
            finish_step("agree-deploy", sqlite_config, verdict, extra_frontmatter)
            gate, _, state = gate_then_next("agree-deploy", sqlite_config, capsys)
            assert state["current_phase"] == gate["next_phase"]
        assert state["current_phase"] == "ops"

        finish_step("agree-deploy", sqlite_config, "deploy-ready")
        gate, out, state = gate_then_next("agree-deploy", sqlite_config, capsys)
        assert (gate["state"], gate["kind"]) == ("waiting_on_owner", "deploy_approval")
        assert gate["approve_command"] == f"{settings._CLI_CMD} override agree-deploy done"
        assert gate["approve_command"] in out
        assert state["status"] == "in_progress"


class TestNextRefusesBlockedTasks:
    """A blocked task moves again only when the owner says so (override or abort)."""

    def blocked_task_with_a_verdict_that_would_route_on(self, task_id, config):
        config["human_gates"]["after_designer"] = False
        task_dir = start_task(task_id, config)
        write_artifact(task_dir, "01-designer.md", "designer", 1, "ready-for-architect")
        state = run_state.load_run_state(task_id, config)
        state["status"] = "blocked"
        run_state.save_run_state(task_id, state, config)

    def test_next_exits_1_and_changes_nothing(self, sqlite_config, capsys):
        self.blocked_task_with_a_verdict_that_would_route_on("refuse", sqlite_config)
        before = ledger_snapshot(sqlite_config)
        capsys.readouterr()
        with pytest.raises(SystemExit) as exit_info:
            commands.cmd_next("refuse", auto=False, config=sqlite_config)
        out = capsys.readouterr().out
        assert exit_info.value.code == 1
        assert "Task refuse is blocked" in out
        assert "It stopped in phase: designer" in out
        assert f"{settings._CLI_CMD} override refuse <phase>" in out
        assert f"{settings._CLI_CMD} abort refuse" in out
        assert ledger_snapshot(sqlite_config) == before

    def test_override_unblocks_and_next_works_again(self, sqlite_config, capsys):
        self.blocked_task_with_a_verdict_that_would_route_on("unblock", sqlite_config)
        commands.cmd_override("unblock", "architect", sqlite_config)
        state = run_state.load_run_state("unblock", sqlite_config)
        assert (state["status"], state["current_phase"]) == ("in_progress", "architect")

        finish_step("unblock", sqlite_config, "ready-for-builder", "deploy: no\n")
        sqlite_config["human_gates"]["after_architect"] = False
        gate, _, state = gate_then_next("unblock", sqlite_config, capsys)
        assert state["current_phase"] == gate["next_phase"] == "builder"

    def test_next_after_the_retry_limit_refuses_instead_of_rechecking(self, sqlite_config, capsys):
        sqlite_config["max_retries"] = 0
        advance_to_architect("limit", sqlite_config)
        commands.cmd_override("limit", "builder", sqlite_config)
        finish_step("limit", sqlite_config, "ready-for-review")
        commands.cmd_next("limit", auto=False, config=sqlite_config)
        finish_step("limit", sqlite_config, "changes_requested")
        commands.cmd_next("limit", auto=False, config=sqlite_config)  # hits the limit: blocked
        assert run_state.load_run_state("limit", sqlite_config)["status"] == "blocked"

        capsys.readouterr()
        with pytest.raises(SystemExit) as exit_info:
            commands.cmd_next("limit", auto=False, config=sqlite_config)
        assert exit_info.value.code == 1
        assert ("Task limit is stopped: builder has gone back for fixes 0 times in a row "
                "with nobody deciding (limit 0)." in capsys.readouterr().out)

    def task_stopped_at_the_retry_limit(self, task_id, config):
        config["max_retries"] = 0
        advance_to_architect(task_id, config)
        commands.cmd_override(task_id, "builder", config)
        finish_step(task_id, config, "ready-for-review")
        commands.cmd_next(task_id, auto=False, config=config)
        finish_step(task_id, config, "changes_requested")
        commands.cmd_next(task_id, auto=False, config=config)
        assert run_state.load_run_state(task_id, config)["status"] == "blocked"

    def test_override_gets_a_task_past_the_retry_limit(self, sqlite_config, capsys):
        self.task_stopped_at_the_retry_limit("limit-override", sqlite_config)
        commands.cmd_override("limit-override", "builder", sqlite_config)
        state = run_state.load_run_state("limit-override", sqlite_config)
        assert (state["status"], state["current_phase"]) == ("in_progress", "builder")
        finish_step("limit-override", sqlite_config, "ready-for-review")
        gate, _, state = gate_then_next("limit-override", sqlite_config, capsys)
        assert state["current_phase"] == gate["next_phase"] == "reviewer"

    def test_abort_closes_a_task_stopped_at_the_retry_limit(self, sqlite_config):
        self.task_stopped_at_the_retry_limit("limit-abort", sqlite_config)
        commands.cmd_abort("limit-abort", sqlite_config)
        assert run_state.load_run_state("limit-abort", sqlite_config)["status"] == "abandoned"
        assert read_api.gate_check_payload("limit-abort", sqlite_config)["gate"]["state"] == "closed"


class TestReadsNeverWrite:
    def test_reading_leaves_the_ledger_and_task_folder_untouched(self, sqlite_config):
        """An artifact with a verdict is waiting to be picked up. Reading must
        report it without recording it: that is `relay next`'s job."""
        task_dir = start_task("readonly", sqlite_config)
        write_artifact(task_dir, "01-designer.md", "designer", 1, "ready-for-architect")
        before_ledger = ledger_snapshot(sqlite_config)
        before_files = {f.name: f.read_bytes() for f in task_dir.iterdir()}

        status = read_api.status_payload("readonly", sqlite_config)
        read_api.gate_check_payload("readonly", sqlite_config)
        read_api.list_payload(sqlite_config)

        assert ledger_snapshot(sqlite_config) == before_ledger
        assert {f.name: f.read_bytes() for f in task_dir.iterdir()} == before_files
        assert status["latest_step"]["verdict"] == "ready-for-architect"
        assert status["latest_step"]["recorded"] is False

    def test_json_commands_leave_the_ledger_files_byte_identical(self, sqlite_config,
                                                                 monkeypatch, capsys):
        task_dir = start_task("bytes", sqlite_config)
        write_artifact(task_dir, "01-designer.md", "designer", 1, "ready-for-architect")
        ledger_files = [settings._db_path(), Path(str(settings._db_path()) + "-wal")]
        before = [f.read_bytes() for f in ledger_files]
        monkeypatch.setattr(settings, "load_config", lambda: sqlite_config)
        for argv in (["list", "--json"], ["status", "bytes", "--json"],
                     ["gate-check", "bytes", "--json"]):
            monkeypatch.setattr(sys, "argv", ["relay", *argv])
            with pytest.raises(SystemExit) as exit_info:
                relay.main()
            assert exit_info.value.code == 0
        assert [f.read_bytes() for f in ledger_files] == before

    def test_json_commands_hold_a_connection_that_cannot_write(self, sqlite_config):
        start_task("locked", sqlite_config)
        assert commands.run_json_command("status", "locked", sqlite_config) == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            settings.get_db_connection().execute("UPDATE tasks SET status = 'completed'")

    def test_recorded_turns_true_once_next_has_picked_the_verdict_up(self, sqlite_config):
        task_dir = start_task("recorded", sqlite_config)
        write_artifact(task_dir, "01-designer.md", "designer", 1, "ready-for-architect")
        commands.cmd_next("recorded", auto=False, config=sqlite_config)
        latest = read_api.status_payload("recorded", sqlite_config)["latest_step"]
        assert (latest["verdict"], latest["recorded"]) == ("ready-for-architect", True)


class TestCommandLine:
    def run_main(self, monkeypatch, capsys, config, *argv):
        monkeypatch.setattr(settings, "load_config", lambda: config)
        monkeypatch.setattr(sys, "argv", ["relay", *argv])
        capsys.readouterr()
        with pytest.raises(SystemExit) as exit_info:
            relay.main()
        return exit_info.value.code, capsys.readouterr()

    def test_stdout_is_exactly_one_json_document(self, sqlite_config, monkeypatch, capsys):
        start_task("cli-json", sqlite_config)
        for argv, schema in ((["status", "cli-json", "--json"], "relay.status"),
                             (["gate-check", "cli-json", "--json"], "relay.gate-check"),
                             (["list", "--json"], "relay.list")):
            code, captured = self.run_main(monkeypatch, capsys, sqlite_config, *argv)
            assert code == 0
            assert json.loads(captured.out)["schema"] == schema

    def test_unknown_task_is_a_json_error_with_exit_code_1(self, sqlite_config, monkeypatch, capsys):
        for command in ("status", "gate-check"):
            code, captured = self.run_main(monkeypatch, capsys, sqlite_config,
                                           command, "no-such-task", "--json")
            payload = json.loads(captured.out)
            assert code == 1
            assert payload["ok"] is False
            assert payload["error"]["code"] == "task_not_found"

    def test_missing_ledger_fails_closed_as_a_json_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(settings, "TASKS_DIR", tmp_path / "tasks")
        monkeypatch.setattr(settings, "_db_connection", None)
        monkeypatch.setenv("RELAY_DB_PATH", str(tmp_path / "missing.db"))
        config = dict(DEFAULT_CONFIG)
        config["persistence_backend"] = "sqlite"
        for argv in (["list", "--json"], ["status", "any-task", "--json"],
                     ["gate-check", "any-task", "--json"]):
            code, captured = self.run_main(monkeypatch, capsys, config, *argv)
            payload = json.loads(captured.out)
            assert code == 1
            assert payload["error"]["code"] == "ledger_unavailable"
        assert not (tmp_path / "missing.db").exists()

    def test_unexpected_failure_is_still_a_json_error(self, sqlite_config, monkeypatch, capsys):
        """Whatever breaks, stdout stays one JSON document and the exit code is 1."""
        start_task("cli-crash", sqlite_config)

        def explode(*args, **kwargs):
            raise RuntimeError("boom")
        monkeypatch.setattr(read_api, "read_gate", explode)
        for argv in (["list", "--json"], ["status", "cli-crash", "--json"],
                     ["gate-check", "cli-crash", "--json"]):
            code, captured = self.run_main(monkeypatch, capsys, sqlite_config, *argv)
            payload = json.loads(captured.out)
            assert code == 1
            assert payload["error"] == {"code": "internal_error", "message": "RuntimeError: boom"}

    def test_values_json_has_no_type_for_are_printed_as_text(self, tmp_path, monkeypatch, capsys):
        """Old run.yaml files hold YAML dates in places the API passes through."""
        monkeypatch.setattr(settings, "TASKS_DIR", tmp_path / "tasks")
        task_dir = tmp_path / "tasks" / "old-task"
        task_dir.mkdir(parents=True)
        (task_dir / "run.yaml").write_text(
            "task_id: old-task\nstatus: in_progress\ncurrent_phase: builder\n"
            "created: 2026-04-01\nrepo: 2026-04-01\nhistory: []\n")
        config = dict(DEFAULT_CONFIG)
        code, captured = self.run_main(monkeypatch, capsys, config, "status", "old-task", "--json")
        payload = json.loads(captured.out)
        assert code == 0
        assert payload["task"]["created"] == "2026-04-01"
        assert payload["task"]["repo"] == "2026-04-01"

    def test_json_flag_is_refused_on_commands_that_change_state(self, sqlite_config,
                                                                monkeypatch, capsys):
        start_task("cli-refuse", sqlite_config)
        before = ledger_snapshot(sqlite_config)
        code, captured = self.run_main(monkeypatch, capsys, sqlite_config,
                                       "abort", "cli-refuse", "--json")
        assert code == 2
        assert "--json is only available for" in captured.err
        assert ledger_snapshot(sqlite_config) == before

    def test_gate_check_plain_text(self, sqlite_config, capsys):
        task_dir = start_task("cli-text", sqlite_config)
        write_artifact(task_dir, "01-designer.md", "designer", 1, "ready-for-architect")
        capsys.readouterr()
        commands.cmd_gate_check("cli-text", sqlite_config)
        out = capsys.readouterr().out
        assert "Gate: waiting_on_owner (approval)" in out
        assert f"To approve: {settings._CLI_CMD} override cli-text architect" in out

    def test_gate_check_plain_text_unknown_task_exits_1(self, sqlite_config, monkeypatch, capsys):
        code, captured = self.run_main(monkeypatch, capsys, sqlite_config,
                                       "gate-check", "no-such-task")
        assert code == 1
        assert "No run state for no-such-task." in captured.out


class TestFilesBackend:
    """The older files backend answers the same questions in the same shapes."""

    @pytest.fixture
    def files_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "TASKS_DIR", tmp_path / "tasks")
        (tmp_path / "tasks").mkdir()
        config = dict(DEFAULT_CONFIG)
        config["human_gates"] = dict(DEFAULT_CONFIG["human_gates"])
        return config

    def test_same_shapes_and_gate_as_the_sqlite_backend(self, files_config):
        task_dir = start_task("files-task", files_config)
        assert read_api.gate_check_payload("files-task", files_config)["gate"]["state"] == "waiting_on_agent"

        write_artifact(task_dir, "01-designer.md", "designer", 1, "ready-for-architect")
        status = read_api.status_payload("files-task", files_config)
        assert set(status["latest_step"]) == LATEST_STEP_KEYS
        assert status["latest_step"]["artifact"] == "01-designer.md"
        assert (status["gate"]["state"], status["gate"]["kind"]) == ("waiting_on_owner", "approval")
        assert status["approvals"] == []

        tasks = read_api.list_payload(files_config)["tasks"]
        assert [set(task) for task in tasks] == [LIST_ROW_KEYS]
        assert tasks[0]["latest_step"] == {
            "step": 1, "phase": "designer", "verdict": "ready-for-architect",
            "started": status["history"][0]["timestamp"]}
        assert status["latest_step"]["started"] == status["history"][0]["timestamp"]
        assert [entry["artifact"] for entry in status["history"]] == ["01-designer.md"]

    def test_a_step_with_no_file_yet_has_no_artifact_name(self, files_config):
        start_task("files-early", files_config)
        status = read_api.status_payload("files-early", files_config)
        assert [set(entry) for entry in status["history"]] == [HISTORY_ENTRY_KEYS]
        assert [entry["artifact"] for entry in status["history"]] == [None]

    def test_odd_history_entries_do_not_break_the_answer(self, files_config):
        """An old run.yaml can hold anything. Entries that are not mappings
        are left out, and a step that is not a number has no file name."""
        task_dir = settings.TASKS_DIR / "files-odd"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: files-odd\nstatus: in_progress\ncurrent_phase: builder\n"
            "history:\n- just text\n- {phase: builder, step: [1, 2]}\n- {phase: builder}\n")
        status = read_api.status_payload("files-odd", files_config)
        assert [entry["artifact"] for entry in status["history"]] == [None, None]
        assert status["latest_step"]["started"] is None


class TestDecideRouting:
    """The decision `relay next` and gate-check share."""

    def decide(self, phase, verdict, *, state=None, config=None, **options):
        config = config or {"human_gates": dict(DEFAULT_CONFIG["human_gates"]), "max_retries": 3}
        return routing.decide_routing(state or {"history": []}, phase, verdict, config, **options)

    def test_loopback_counts_as_a_retry(self):
        decision = self.decide("reviewer", "changes_requested")
        assert (decision.outcome, decision.next_phase, decision.counts_as_retry) == (
            "advance", "builder", True)

    def test_loopback_at_the_limit_stops_for_the_owner(self):
        decision = self.decide("reviewer", "changes_requested",
                               state={"history": [], "retries": {"builder": 3}})
        assert (decision.outcome, decision.gate_kind) == ("retry_limit", "retry_limit")

    NON_DEPLOY_TESTED = {"deploy_intent": False,
                         "history": [{"phase": "builder", "step": 3},
                                     {"phase": "tester", "step": 5, "verdict": "pass"}]}

    def test_non_deploy_task_completes_after_a_clean_security_pass(self):
        decision = self.decide("security", "secure", state=self.NON_DEPLOY_TESTED)
        assert decision.outcome == "complete_skipping_ops"

    @pytest.mark.parametrize("phase", ["security", "pentest"])
    def test_non_deploy_task_with_security_findings_stops_for_the_owner(self, phase):
        decision = self.decide(phase, "findings", state=self.NON_DEPLOY_TESTED)
        assert (decision.outcome, decision.gate_kind, decision.next_phase,
                decision.send_back_recommended) == ("owner_gate", "approval", "done", True)

    def test_deploy_task_with_security_findings_gates_on_moving_to_ops(self):
        deploying = dict(self.NON_DEPLOY_TESTED, deploy_intent=True)
        decision = self.decide("security", "findings", state=deploying)
        assert (decision.outcome, decision.gate_kind, decision.next_phase,
                decision.send_back_recommended) == ("owner_gate", "approval", "ops", True)

    def test_old_order_task_is_sent_to_the_tester_before_ops(self):
        untested = {"deploy_intent": True, "history": [{"phase": "builder", "step": 3}]}
        decision = self.decide("security", "secure", state=untested)
        assert (decision.outcome, decision.next_phase, decision.redirected_to_tester,
                decision.counts_as_retry) == ("advance", "tester", True, False)

    def test_deploy_ready_is_a_deploy_approval_gate(self):
        decision = self.decide("ops", "deploy-ready")
        assert (decision.outcome, decision.gate_kind, decision.next_phase) == (
            "owner_gate", "deploy_approval", None)

    def test_deployed_completes_the_workflow(self):
        assert self.decide("ops", "deployed").outcome == "complete"

    def test_force_phase_skips_every_gate(self):
        decision = self.decide("designer", "needs-human-input", force_phase="builder")
        assert (decision.outcome, decision.next_phase) == ("advance", "builder")


class TestTheFixRoundsKey:
    """The list of highs per fix round is in a gate only where the
    tripwire stopped the task; every other gate reads as it did."""

    @pytest.mark.parametrize("walk, kind", [
        ([("ready-for-architect", "")], "approval"),
        ([("needs-human-input", "")], "owner_decision"),
        ([("ready-for-architect", ""), ("ready-for-builder", "")], "deploy_flag_missing"),
        ([("ready-for-architect", ""), ("needs-designer", "")], "retry_limit"),
    ])
    def test_every_other_gate_has_no_fix_rounds_key(self, sqlite_config, walk, kind):
        sqlite_config["max_retries"] = 0
        start_task("other-gate", sqlite_config)
        for verdict, extra in walk:
            finish_step("other-gate", sqlite_config, verdict, extra)
            commands.cmd_next("other-gate", auto=False, config=sqlite_config)
            if verdict == "ready-for-architect" and len(walk) > 1:
                commands.cmd_override("other-gate", "architect", sqlite_config)
        gate = read_api.gate_check_payload("other-gate", sqlite_config)["gate"]
        assert gate["kind"] == kind
        assert set(gate) == GATE_KEYS
        assert read_api.list_payload(sqlite_config)["tasks"][0]["fix_rounds"] is None
