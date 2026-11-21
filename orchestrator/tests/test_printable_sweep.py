"""No text a role wrote reaches the terminal raw (printable.py's rule).

Each test hands Relay a report whose file name, `phase:` or `verdict:`
holds an erase-line code, a carriage return, a newline, a Unicode line
separator and a right-to-left override. The whole output of the command
must then hold none of them, so a report cannot wipe a line Relay printed
and put its own in its place. Because each test checks everything the
command printed, a new raw print of the same report text on the same path
fails it too.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import awake
import commands
import run_state
import settings
from tests.test_ledger_writes import files_config, start_on_files  # noqa: F401  (fixture)
from tests.test_send_back import config  # noqa: F401  (fixture)

# What a report writes, and what printable() makes of it.
HOSTILE = "\x1b[2K\rForged line\n X‮Y"
HOSTILE_IN_YAML = '"\\e[2K\\rForged line\\n\\LX\\u202eY"'
CLEANED = "[2K Forged line XY"


def assert_printed_clean(out: str) -> None:
    """Every character but the line breaks Relay itself prints is one a
    terminal shows and does not act on, and the forged text never starts
    a line of its own."""
    acted_on = sorted({repr(character) for character in out
                       if character != "\n" and not character.isprintable()})
    assert acted_on == [], f"printed raw: {acted_on}"
    assert not any(line.lstrip().startswith(("Forged", "X")) for line in out.splitlines())


def write_report(task_id: str, name: str, *, phase: str, verdict: str | None,
                 step: str = "1", extra: str = "") -> Path:
    verdict_line = f"verdict: {verdict}\n" if verdict is not None else ""
    path = settings.TASKS_DIR / task_id / name
    path.write_text(f"---\ntask_id: {task_id}\nphase: {phase}\nstep: {step}\n"
                    f"{verdict_line}{extra}---\n\nBody.\n")
    return path


def gated_after_designer(files_config: dict) -> dict:  # noqa: F811
    return dict(files_config, human_gates=dict(files_config["human_gates"], after_designer=True))


@pytest.fixture
def name_check_off(monkeypatch):
    """`relay next` refuses a report whose file name printable() would
    change before it prints anything about the report (the tests at the
    end of this file). The tests that use this switch that check off, to
    show that each place that prints a report's name prints it cleaned
    should such a name ever get past it."""
    monkeypatch.setattr(commands, "_report_names_printable_would_change",
                        lambda task_id, config: [])


# ---------------------------------------------------------------------------
# relay next
# ---------------------------------------------------------------------------

def test_next_prints_a_reports_verdict_and_routing_reason_cleaned(files_config, capsys):  # noqa: F811
    """The Verdict line and the Routing line (whose reason quotes the
    unknown verdict) come out on one line each, cleaned."""
    start_on_files("verdict", files_config)
    write_report("verdict", "01-designer.md", phase="designer", verdict=HOSTILE_IN_YAML)
    capsys.readouterr()
    assert commands.cmd_next("verdict", auto=False, config=files_config) == 0
    out = capsys.readouterr().out
    assert_printed_clean(out)
    assert f"  Verdict: {CLEANED}" in out.splitlines()
    assert f"Unknown verdict '{CLEANED}' from designer" in out


def test_next_prints_a_reports_phase_cleaned(files_config, capsys):  # noqa: F811
    """The Phase line, the state warning that names the report's phase and
    the Routing line come out cleaned."""
    start_on_files("phase", files_config)
    write_report("phase", "01-designer.md", phase=HOSTILE_IN_YAML, verdict="ready-for-architect")
    capsys.readouterr()
    commands.cmd_next("phase", auto=False, config=files_config)
    out = capsys.readouterr().out
    assert_printed_clean(out)
    assert f"  Phase: {CLEANED}" in out.splitlines()
    assert f"latest artifact is from '{CLEANED}'" in out


def test_next_prints_a_reports_file_name_cleaned(files_config, capsys, name_check_off):  # noqa: F811
    """The Latest artifact line, the step warning and the approval gate's
    path name the report's file cleaned."""
    gated = gated_after_designer(files_config)
    start_on_files("named", gated)
    write_report("named", f"01-designer{HOSTILE}.md", phase="designer",
                 verdict="ready-for-architect", step="7")
    capsys.readouterr()
    assert commands.cmd_next("named", auto=False, config=gated) == 0
    out = capsys.readouterr().out
    assert_printed_clean(out)
    lines = out.splitlines()
    assert f"  Latest artifact: 01-designer{CLEANED}.md" in lines
    assert any(line.startswith(f"  Warning: 01-designer{CLEANED}.md says it is step 7") for line in lines)
    assert f"  Review the artifact at: {settings.TASKS_DIR / 'named'}/01-designer{CLEANED}.md" in lines


def test_next_at_the_deploy_flag_gate_prints_the_plans_file_name_cleaned(files_config, capsys,  # noqa: F811
                                                                         name_check_off):
    """A plan with no deploy line stops for the owner and names the file
    to fix; the name comes out cleaned."""
    start_on_files("deploy", files_config)
    write_report("deploy", "01-designer.md", phase="designer", verdict="ready-for-architect")
    assert commands.cmd_next("deploy", auto=False, config=files_config) == 0
    write_report("deploy", f"02-architect{HOSTILE}.md", phase="architect",
                 verdict="ready-for-builder", step="2")
    capsys.readouterr()
    assert commands.cmd_next("deploy", auto=False, config=files_config) == 0
    out = capsys.readouterr().out
    assert_printed_clean(out)
    assert "the plan doesn't say whether this task deploys" in out
    assert f"    {settings.TASKS_DIR / 'deploy'}/02-architect{CLEANED}.md" in out.splitlines()


def test_next_with_no_verdict_prints_the_file_name_and_phase_cleaned(files_config, capsys,  # noqa: F811
                                                                     name_check_off):
    start_on_files("noverdict", files_config)
    write_report("noverdict", f"01-designer{HOSTILE}.md", phase=HOSTILE_IN_YAML, verdict=None)
    capsys.readouterr()
    assert commands.cmd_next("noverdict", auto=False, config=files_config) == 1
    out = capsys.readouterr().out
    assert_printed_clean(out)
    lines = out.splitlines()
    assert f"Warning: No verdict found in 01-designer{CLEANED}.md" in lines
    assert f"  Current phase: {CLEANED}" in lines


def test_next_at_an_unreadable_arbiter_prints_its_report_path_cleaned(files_config, capsys,  # noqa: F811
                                                                      name_check_off):
    """An Arbiter report with no verdict stops for the owner and names the
    report; its file name comes out cleaned."""
    start_on_files("arbiter", files_config)
    write_report("arbiter", f"05-arbiter{HOSTILE}.md", phase="arbiter", verdict=None, step="5")
    capsys.readouterr()
    assert commands.cmd_next("arbiter", auto=False, config=files_config) == 0
    out = capsys.readouterr().out
    assert_printed_clean(out)
    assert f"  The Arbiter's report: {settings.TASKS_DIR / 'arbiter'}/05-arbiter{CLEANED}.md" in out.splitlines()


def test_next_notes_a_sleep_during_a_step_with_the_reports_phase_cleaned(files_config, capsys,  # noqa: F811
                                                                         monkeypatch):
    start_on_files("slept", files_config)
    write_report("slept", "01-designer.md", phase=HOSTILE_IN_YAML, verdict="ready-for-architect")
    slept_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(awake, "sleeps_between",
                        lambda started, finished: [{"asleep_seconds": 600, "slept_at": slept_at}])
    capsys.readouterr()
    commands.cmd_next("slept", auto=False, config=files_config)
    out = capsys.readouterr().out
    assert_printed_clean(out)
    assert f"between {CLEANED} (step 1) being dispatched" in out


# ---------------------------------------------------------------------------
# relay status, relay gate and relay send-back, after such a report
# ---------------------------------------------------------------------------

def test_status_on_files_prints_the_recorded_verdict_and_artifacts_cleaned(files_config, capsys):  # noqa: F811
    start_on_files("status", files_config)
    write_report("status", "01-designer.md", phase="designer", verdict=HOSTILE_IN_YAML)
    commands.cmd_next("status", auto=False, config=files_config)
    write_report("status", f"02-designer{HOSTILE}.md", phase="designer", verdict=HOSTILE_IN_YAML)
    capsys.readouterr()
    commands.cmd_status("status", files_config)
    out = capsys.readouterr().out
    assert_printed_clean(out)
    lines = out.splitlines()
    assert any(line.startswith(f"    01 designer -> {CLEANED} (") for line in lines)
    assert f"    01-designer.md [{CLEANED}]" in lines
    assert f"    02-designer{CLEANED}.md [{CLEANED}]" in lines


def test_status_on_sqlite_prints_the_artifacts_it_finds_cleaned(config, capsys):  # noqa: F811
    """`relay status` lists the task folder's reports from the files
    themselves on either backend, so a report placed there under sqlite is
    printed cleaned too."""
    task_dir = settings.TASKS_DIR / "sq"
    task_dir.mkdir()
    (task_dir / "brief.md").write_text("# Brief\n")
    assert commands.cmd_start("sq", auto=False, config=config) == 0
    write_report("sq", f"02-designer{HOSTILE}.md", phase="designer", verdict=HOSTILE_IN_YAML)
    capsys.readouterr()
    commands.cmd_status("sq", config)
    out = capsys.readouterr().out
    assert_printed_clean(out)
    assert f"    02-designer{CLEANED}.md [{CLEANED}]" in out.splitlines()


def test_gate_and_send_back_print_a_reason_quoting_the_verdict_cleaned(files_config, capsys):  # noqa: F811
    start_on_files("reason", files_config)
    write_report("reason", "01-designer.md", phase="designer", verdict=HOSTILE_IN_YAML)
    commands.cmd_next("reason", auto=False, config=files_config)
    capsys.readouterr()
    commands.cmd_gate_check("reason", files_config)
    gate_out = capsys.readouterr().out
    commands.cmd_send_back("reason", files_config, note="fix it")
    send_back_out = capsys.readouterr().out
    assert_printed_clean(gate_out)
    assert_printed_clean(send_back_out)
    assert f"  Reason: Unknown verdict '{CLEANED}' from designer. Human must decide." in gate_out.splitlines()
    assert f"(designer verdict: {CLEANED}): fix what it raises" in send_back_out


def test_a_send_back_from_an_arbiter_quotes_its_verdict_cleaned():
    """On the files backend an Arbiter's verdict that is not one of its
    four is still recorded on its step, and a send-back from it quotes it
    in the words printed for the owner to paste."""
    words = commands._sent_back_words({"phase": "arbiter", "verdict": HOSTILE,
                                       "artifact_path": "/tasks/t/05-arbiter.md"}, None)
    assert_printed_clean(words.strip("\n"))
    assert f"the Arbiter's ruling at /tasks/t/05-arbiter.md ({CLEANED})" in words


# ---------------------------------------------------------------------------
# A report whose file name printable() would change: Relay reads none of
# the task's reports until it is renamed. On the files backend a report's
# name is the role's own choice, and it goes into the prompt printed for the
# owner to paste, where printable() cannot clean it without handing the next
# role a path that does not exist.
# ---------------------------------------------------------------------------

def play_on_files(task_id: str, config: dict, verdict: str, extra: str = "") -> Path:  # noqa: F811
    """Play the role now running: write its report, under the name Relay
    gave it, with this verdict, and return the report's path."""
    state = run_state.load_run_state(task_id, config)
    phase, step = state["history"][-1]["phase"], state["history"][-1]["step"]
    path = settings.TASKS_DIR / task_id / commands.artifact_name(phase, step, state)
    path.write_text(f"---\ntask_id: {task_id}\nphase: {phase}\nstep: {step}\n"
                    f"verdict: {verdict}\n{extra}---\n\nBody.\n")
    return path


def at_the_arbiter_on_files(task_id: str, config: dict) -> Path:  # noqa: F811
    """Walk a files-backend task to the Arbiter, called on the Reviewer's
    pass with warnings. Returns the path of the Reviewer's report."""
    start_on_files(task_id, config)
    for verdict, extra in [("ready-for-architect", ""), ("ready-for-builder", "deploy: no\n"),
                           ("ready-for-review", "")]:
        play_on_files(task_id, config, verdict, extra)
        assert commands.cmd_next(task_id, auto=False, config=config) == 0
    reviewer_report = play_on_files(task_id, config, "approved_with_warnings")
    assert commands.cmd_next(task_id, auto=False, config=config) == 0
    assert run_state.load_run_state(task_id, config)["history"][-1]["phase"] == "arbiter"
    return reviewer_report


def history_of(task_id: str, config: dict) -> list:  # noqa: F811
    return run_state.load_run_state(task_id, config)["history"]


def assert_refused_for_the_name(out: str, cleaned_name: str, command: str) -> None:
    assert_printed_clean(out)
    lines = out.splitlines()
    assert "Warning: Relay will not read this task's reports while a report's file name cannot " \
           "be printed as it is:" in lines
    assert f"  {cleaned_name}  (the name as Relay would print it)" in lines
    assert f"  Rename the file to a plain name, then run '{command}' again." in lines


def test_next_refuses_a_latest_report_whose_file_name_printable_would_change(files_config, capsys):  # noqa: F811
    """`relay next` stops before it records the verdict or builds a prompt,
    names the file as it reads cleaned and asks for it to be renamed."""
    start_on_files("hostile", files_config)
    write_report("hostile", f"01-designer{HOSTILE}.md", phase="designer",
                 verdict="ready-for-architect")
    before = history_of("hostile", files_config)
    capsys.readouterr()
    assert commands.cmd_next("hostile", auto=False, config=files_config) == 1
    assert_refused_for_the_name(capsys.readouterr().out, f"01-designer{CLEANED}.md",
                                "relay next hostile")
    assert history_of("hostile", files_config) == before


def test_next_refuses_an_older_report_whose_file_name_printable_would_change(files_config,  # noqa: F811
                                                                             capsys):
    """A name on any report counts, not only the latest's: Relay never routes
    with such a file in the folder, and never on an older report instead."""
    start_on_files("older", files_config)
    play_on_files("older", files_config, "ready-for-architect")
    assert commands.cmd_next("older", auto=False, config=files_config) == 0
    write_report("older", f"01-designer{HOSTILE}.md", phase="designer",
                 verdict="ready-for-architect")
    play_on_files("older", files_config, "ready-for-builder", "deploy: no\n")
    before = history_of("older", files_config)
    capsys.readouterr()
    assert commands.cmd_next("older", auto=False, config=files_config) == 1
    assert_refused_for_the_name(capsys.readouterr().out, f"01-designer{CLEANED}.md",
                                "relay next older")
    assert history_of("older", files_config) == before


def test_next_after_an_arbiter_ruling_refuses_when_the_report_it_ruled_on_has_such_a_name(  # noqa: F811
        files_config, capsys):
    """The targeted round's prompt names the report the Arbiter ruled on;
    `relay next` refuses rather than print that name raw."""
    reviewer_report = at_the_arbiter_on_files("ruled", files_config)
    reviewer_report.rename(reviewer_report.with_name(f"04-reviewer{HOSTILE}.md"))
    arbiter_report = play_on_files("ruled", files_config, "targeted-round",
                                   "reason: One problem.\nfix:\n- Bound the parse.\n")
    assert arbiter_report.name == "05-arbiter.md"
    before = history_of("ruled", files_config)
    capsys.readouterr()
    assert commands.cmd_next("ruled", auto=False, config=files_config) == 1
    assert_refused_for_the_name(capsys.readouterr().out, f"04-reviewer{CLEANED}.md",
                                "relay next ruled")
    assert history_of("ruled", files_config) == before


def test_override_to_the_arbiter_refuses_when_a_report_has_such_a_name(files_config, capsys):  # noqa: F811
    """Asking the Arbiter again hands it every report's name; the override
    refuses rather than print one raw."""
    reviewer_report = at_the_arbiter_on_files("again", files_config)
    reviewer_report.rename(reviewer_report.with_name(f"04-reviewer{HOSTILE}.md"))
    before = history_of("again", files_config)
    capsys.readouterr()
    assert commands.cmd_override("again", "arbiter", files_config) == 1
    assert_refused_for_the_name(capsys.readouterr().out, f"04-reviewer{CLEANED}.md",
                                "relay override again arbiter")
    assert history_of("again", files_config) == before


def test_send_back_refuses_when_the_report_sent_back_has_such_a_name(files_config, capsys):  # noqa: F811
    """The send-back prompt names the report it answers; send-back refuses
    and records nothing rather than print that name raw."""
    gated = gated_after_designer(files_config)
    start_on_files("sent", gated)
    designer_report = play_on_files("sent", gated, "ready-for-architect")
    assert commands.cmd_next("sent", auto=False, config=gated) == 0
    designer_report.rename(designer_report.with_name(f"01-designer{HOSTILE}.md"))
    before = history_of("sent", gated)
    capsys.readouterr()
    assert commands.cmd_send_back("sent", gated, note="Fix it.") == 1
    assert_refused_for_the_name(capsys.readouterr().out, f"01-designer{CLEANED}.md",
                                "relay send-back sent")
    assert history_of("sent", gated) == before


@pytest.mark.parametrize("name", ["01-designer  two-spaces.md", f"01-designer-{'a' * 170}.md"],
                         ids=["a run of spaces", "over the print limit"])
def test_next_refuses_a_plain_name_printable_would_still_change(files_config, capsys, name):  # noqa: F811
    """printable() also changes a name with a run of spaces or one over its
    limit, so the refusal covers those too, as its words say."""
    start_on_files("plain", files_config)
    write_report("plain", name, phase="designer", verdict="ready-for-architect")
    capsys.readouterr()
    assert commands.cmd_next("plain", auto=False, config=files_config) == 1
    out = capsys.readouterr().out
    assert_refused_for_the_name(out, commands.gate_card.printable(name), "relay next plain")
    assert ("  The name holds a character a terminal acts on or does not show, a run of spaces, "
            "or is over 160 characters.") in out.splitlines()
