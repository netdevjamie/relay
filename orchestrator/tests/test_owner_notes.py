"""Tests for the owner's notes: what the owner says at a gate, or at any
time, is kept in the ledger and given to every later agent."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commands
import db
import owner_notes
import read_api
import relay
from tests.cli_edge import run_relay
import settings
from tests.test_send_back import (  # noqa: F401
    UP_TO_REVIEW, config, events_of_type, finish_step, task_at, the_arbiter_escalates,
)

HEADER = "The owner's notes on this task, oldest first."


def at_the_brief_gate(task_id: str, config: dict) -> None:
    config["human_gates"]["after_designer"] = True
    task_at(task_id, config, [("ready-for-architect", "")])


def notes(task_id: str, config: dict) -> list[tuple]:
    return [(n["kind"], n["phase"], n["next_phase"], n["note"])
            for n in read_api.status_payload(task_id, config)["owner_notes"]]


class TestApprovingWithANote:
    def test_the_note_is_recorded_with_the_approval(self, config):
        at_the_brief_gate("approve", config)
        assert commands.cmd_override("approve", "architect", config, note="Keep 5 contacts, not 3.") == 0
        approval = events_of_type("approve", db.EVENT_APPROVAL)[0]
        assert approval["payload"] == {"next_phase": "architect", "method": "override",
                                       "note": "Keep 5 contacts, not 3."}
        assert read_api.status_payload("approve", config)["approvals"][0]["note"] == "Keep 5 contacts, not 3."
        assert notes("approve", config) == [("approval", "designer", "architect", "Keep 5 contacts, not 3.")]

    def test_the_next_agent_and_every_later_one_is_given_it(self, config, capsys):
        at_the_brief_gate("handed", config)
        capsys.readouterr()
        assert commands.cmd_override("handed", "architect", config, note="Keep 5 contacts, not 3.") == 0
        architect_prompt = capsys.readouterr().out
        assert HEADER in architect_prompt
        assert "approving designer's work and moving to architect:" in architect_prompt
        assert "    Keep 5 contacts, not 3." in architect_prompt

        finish_step("handed", config, "ready-for-builder", "deploy: no\n")
        assert commands.cmd_next("handed", auto=False, config=config) == 0
        builder_prompt = capsys.readouterr().out
        assert "@builder" in builder_prompt and "Keep 5 contacts, not 3." in builder_prompt

    def test_an_approval_without_a_note_adds_nothing(self, config, capsys):
        at_the_brief_gate("plain", config)
        capsys.readouterr()
        assert commands.cmd_override("plain", "architect", config) == 0
        assert HEADER not in capsys.readouterr().out
        assert notes("plain", config) == []


class TestANoteAtAnyTime:
    def test_it_is_recorded_and_moves_nothing(self, config, capsys):
        task_at("fact", config, UP_TO_REVIEW)
        before = read_api.status_payload("fact", config)
        assert commands.cmd_note("fact", config, "An unpaid call still rings. Settled; do not reopen.") == 0
        after = read_api.status_payload("fact", config)
        assert notes("fact", config) == [
            ("note", "reviewer", None, "An unpaid call still rings. Settled; do not reopen.")]
        assert (after["task"]["status"], after["task"]["current_phase"], after["latest_step"],
                after["gate"], after["approvals"]) == (
            before["task"]["status"], before["task"]["current_phase"], before["latest_step"],
            before["gate"], before["approvals"])
        assert "Note recorded on fact (1 from the owner so far)" in capsys.readouterr().out

    def test_the_next_agent_is_given_it_with_earlier_notes_first(self, config, capsys):
        at_the_brief_gate("order", config)
        assert commands.cmd_override("order", "architect", config, note="First decision.") == 0
        assert commands.cmd_note("order", config, "Second decision,\nwith a second line.") == 0
        finish_step("order", config, "ready-for-builder", "deploy: no\n")
        capsys.readouterr()
        assert commands.cmd_next("order", auto=False, config=config) == 0
        out = capsys.readouterr().out
        assert out.index("First decision.") < out.index("Second decision,")
        assert "    Second decision,\n    with a second line." in out
        assert "a note left while the task was in architect:" in out
        assert "a later note stands over an earlier one" in out

    def test_status_shows_the_notes(self, config, capsys):
        task_at("shown", config, [])
        assert commands.cmd_note("shown", config, "Line one.\nLine two.") == 0
        capsys.readouterr()
        commands.cmd_status("shown", config)
        out = capsys.readouterr().out
        assert "The owner's notes:" in out
        assert "(note, at designer): Line one. [...]" in out

    def test_refused_without_a_task_or_without_words(self, config, capsys):
        with pytest.raises(SystemExit):
            run_relay(config, "note", "no-such-task", "--note", "x")
        task_at("empty", config, [])
        for nothing in (None, "", "   \n"):
            with pytest.raises(SystemExit):
                run_relay(config, "note", "empty", *([] if nothing is None else ["--note", nothing]))
        assert events_of_type("empty", db.EVENT_OWNER_NOTE) == []
        assert "Nothing to record." in capsys.readouterr().out

    def test_the_files_backend_says_it_cannot_record_one(self, config, capsys):
        task_at("files", config, [])
        files_config = dict(config, persistence_backend="files")
        with pytest.raises(SystemExit):
            run_relay(files_config, "note", "files", "--note", "x")
        assert "files backend keeps no events" in capsys.readouterr().out
        assert owner_notes.read_owner_notes("files", files_config) == []


class TestLimitsOnANote:
    TOO_LONG = "x" * (owner_notes.MAX_NOTE_CHARACTERS + 1)

    def test_a_note_too_long_to_keep_stops_the_command_before_anything_moves(self, config, capsys):
        at_the_brief_gate("long", config)
        before = read_api.status_payload("long", config)
        for arguments in (["note", "long"], ["override", "long", "architect"], ["send-back", "long"]):
            with pytest.raises(SystemExit):
                run_relay(config, *arguments, "--note", self.TOO_LONG)
        assert "the limit is 4000" in capsys.readouterr().out
        assert read_api.status_payload("long", config) == before

    def test_a_note_at_the_limit_is_kept(self, config):
        task_at("fits", config, [])
        assert commands.cmd_note("fits", config, "y" * owner_notes.MAX_NOTE_CHARACTERS) == 0
        assert len(notes("fits", config)[0][3]) == owner_notes.MAX_NOTE_CHARACTERS

    def test_agents_are_told_a_note_cannot_waive_their_checks(self, config, capsys):
        at_the_brief_gate("waive", config)
        capsys.readouterr()
        assert commands.cmd_override("waive", "architect", config, note="Skip the tests for now.") == 0
        out = capsys.readouterr().out
        assert "A note cannot waive a check your role requires" in out

    def test_what_was_just_said_reaches_the_agent_even_if_the_read_back_misses_it(self, config,
                                                                                capsys, monkeypatch):
        at_the_brief_gate("miss", config)
        monkeypatch.setattr(owner_notes, "read_owner_notes", lambda task_id, cfg: [])
        capsys.readouterr()
        assert commands.cmd_override("miss", "architect", config, note="Do not lose this.") == 0
        out = capsys.readouterr().out
        assert "The owner said, with this step:\nDo not lose this." in out

    def test_a_note_on_a_closed_task_is_kept_and_says_no_agent_will_get_it(self, config, capsys):
        task_at("closed", config, [])
        assert commands.cmd_abort("closed", config) == 0
        capsys.readouterr()
        assert commands.cmd_note("closed", config, "Abandoned because the idea was wrong.") == 0
        assert "so no agent will be given it; it is on the record" in capsys.readouterr().out
        assert notes("closed", config) == [("note", "designer", None, "Abandoned because the idea was wrong.")]


class TestSendingBackWithANote:
    def test_it_is_for_that_one_fix_and_is_not_carried_to_later_agents(self, config, capsys):
        """ "Fix these warnings and nothing else" must not be handed to the
        Reviewer, the Tester and every Builder after them as a standing order."""
        task_at("back", config, UP_TO_REVIEW)
        the_arbiter_escalates("back", config)  # where the owner sends a warnings verdict back
        capsys.readouterr()
        assert commands.cmd_send_back("back", config, note="Fix the error handling.") == 0
        assert "The owner sent this back with a note:\nFix the error handling." in capsys.readouterr().out
        # On the record, as a send-back:
        assert notes("back", config) == [("sent_back", "arbiter", "builder", "Fix the error handling.")]
        finish_step("back", config, "ready-for-review")
        assert commands.cmd_next("back", auto=False, config=config) == 0
        out = capsys.readouterr().out
        assert "@reviewer" in out and "Fix the error handling." not in out and HEADER not in out

    def test_the_fixing_agent_also_gets_the_standing_notes(self, config, capsys):
        task_at("both", config, UP_TO_REVIEW)
        assert commands.cmd_note("both", config, "Public API stays as it is.") == 0
        the_arbiter_escalates("both", config)
        capsys.readouterr()
        assert commands.cmd_send_back("both", config, note="Fix warning 2.") == 0
        out = capsys.readouterr().out
        assert out.index("The owner sent this back with a note:\nFix warning 2.") < out.index(HEADER)
        assert "Public API stays as it is." in out


class TestAHandEditedLedger:
    """Only a hand-edited ledger holds such a row."""

    def add_event(self, task_id: str, payload: str) -> None:
        with settings.get_db_connection() as conn:
            conn.execute("INSERT INTO events (task_id, event_type, phase, payload, timestamp) "
                         "VALUES (?,?,?,?,?)", (task_id, db.EVENT_OWNER_NOTE, "architect", payload,
                                                "2026-11-21T12:00:00Z"))

    @pytest.mark.parametrize("payload", ['"Keep it small."', "[1, 2]", "7"],
                             ids=["text", "list", "number"])
    def test_a_payload_that_is_not_a_mapping_holds_no_note(self, config, payload):
        task_at("edited", config, [])
        commands.cmd_note("edited", config, "Kept.")
        self.add_event("edited", payload)
        assert [n[3] for n in notes("edited", config)] == ["Kept."]
        assert "Kept." in owner_notes.owner_notes_prompt("edited", config)

    @pytest.mark.parametrize("payload", ['{"note": "cut sh', "not json", '{"note": "\\xff"'],
                             ids=["cut short", "not json", "bad escape"])
    def test_a_payload_that_is_not_json_stops_with_a_plain_message(self, config, payload, capsys):
        task_at("broken", config, [])
        finish_step("broken", config, "ready-for-architect")   # a verdict: the list reads events
        self.add_event("broken", payload)
        capsys.readouterr()
        with pytest.raises(SystemExit) as stopped:
            run_relay(config, "status", "broken")
        assert stopped.value.code == 1
        out = capsys.readouterr().out
        assert out.splitlines()[-1].startswith("Error: ")
        assert "an event of task broken (owner_note)" in out and "cannot read as JSON" in out
        with pytest.raises(db.DatabaseUnavailable):
            owner_notes.read_owner_notes("broken", config)
        for arguments in (["status", "broken", "--json"], ["list", "--json"]):
            with pytest.raises(SystemExit):
                run_relay(config, *arguments)
            assert json.loads(capsys.readouterr().out)["error"]["code"] == "ledger_unavailable"


class TestCommandLine:
    def run(self, monkeypatch, config, *arguments):
        monkeypatch.setattr(settings, "load_config", lambda: config)
        monkeypatch.setattr(sys, "argv", ["relay", *arguments])
        relay.main()

    def test_note_from_the_command_line_and_from_a_file(self, config, monkeypatch, tmp_path):
        task_at("cli", config, [])
        self.run(monkeypatch, config, "note", "cli", "--note", "Typed on the line.")
        long_note = tmp_path / "note.txt"
        long_note.write_text("From a file.\nSecond line.\n")
        self.run(monkeypatch, config, "note", "cli", "--note-file", str(long_note))
        assert [n[3] for n in notes("cli", config)] == ["Typed on the line.", "From a file.\nSecond line."]

    def test_override_with_a_note_from_the_command_line(self, config, monkeypatch):
        at_the_brief_gate("cli-approve", config)
        self.run(monkeypatch, config, "override", "cli-approve", "architect", "--note", "Go, but keep it small.")
        assert notes("cli-approve", config) == [
            ("approval", "designer", "architect", "Go, but keep it small.")]

    @pytest.mark.parametrize("arguments", [
        ["next", "cli-bad", "--note", "x"],
        ["note", "cli-bad", "--note", "x", "--note-file", "y"],
        ["note", "cli-bad", "--note-file", "/no/such/file"],
    ])
    def test_misuse_is_a_usage_error_and_records_nothing(self, config, monkeypatch, arguments):
        task_at("cli-bad", config, [])
        with pytest.raises(SystemExit) as stopped:
            self.run(monkeypatch, config, *arguments)
        assert stopped.value.code == 2
        assert notes("cli-bad", config) == []
