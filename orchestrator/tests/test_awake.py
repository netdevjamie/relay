"""Tests for keeping the Mac awake while a phase runs, and for noticing that
it slept while a step was open."""

import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import awake
import commands
import read_api
import settings
from tests.cli_edge import run_relay
from tests.test_send_back import UP_TO_REVIEW, config, finish_step, task_at  # noqa: F401  (config is a fixture)

# Taken when this file is loaded, before the shared guard in conftest.py
# replaces them for each test.
REAL_HOLD = awake.hold
REAL_SLEEPS_BETWEEN = awake.sleeps_between

# As `pmset -g log` prints it (local time with an offset, a tab after the kind).
POWER_LOG = "\n".join([
    "2026-10-05 04:50:00 -0400 Assertions          \tPID 391(dasd) Summary BackgroundTask",
    "2026-10-05 05:03:13 -0400 Sleep               \tEntering Sleep state due to 'Clamshell Sleep' Using AC 1004 secs",
    "2026-10-05 05:03:14 -0400 Wake Requests       \t[process=dasd request=SleepService deltaSecs=1036]",
    "2026-10-05 05:19:57 -0400 DarkWake            \tDarkWake from Deep Idle [CDNPB] : due to rtc/Maintenance 3924 secs",
    "2026-10-05 06:25:21 -0400 Sleep               \tEntering Sleep state due to 'Maintenance Sleep' 727 secs",
    "2026-10-05 06:37:28 -0400 Wake                \tWake from Deep Idle [CDNVA] : due to lid",
    "2026-10-05 08:00:00 -0400 Sleep               \tEntering Sleep state due to 'Idle Sleep' 600 secs",
    "2026-10-05 08:00:01 -0400 Wake Requests       \t[process=powerd request=TCPKATurnOff]",
    "2026-10-05 08:10:00 -0400 Wake                \tWake from Deep Idle [CDNVA] : due to UserActivity",
])


class FakeProcess:
    pid = 4242


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """awake.py as on a Mac with caffeinate, with every outside call recorded."""
    calls = {"spawned": [], "killed": [], "running": {}}
    monkeypatch.setattr(awake, "hold", REAL_HOLD)
    monkeypatch.setattr(awake, "available", lambda: True)
    monkeypatch.setattr(awake.subprocess, "Popen",
                        lambda command, **options: calls["spawned"].append(command) or FakeProcess())
    monkeypatch.setattr(awake, "_command_of", lambda pid: calls["running"].get(pid))
    monkeypatch.setattr(awake.os, "kill", lambda pid, sig: calls["killed"].append((pid, sig)))
    return calls


class TestHoldAndRelease:
    def test_hold_starts_caffeinate_with_a_time_limit_and_remembers_it(self, mac, tmp_path):
        assert awake.hold(tmp_path, 600) is True
        assert mac["spawned"] == [["caffeinate", "-i", "-s", "-t", "600"]]
        assert (tmp_path / awake.PID_FILE).read_text() == "4242\n"

    def test_no_hold_when_turned_off_or_not_possible(self, mac, tmp_path, monkeypatch):
        assert awake.hold(tmp_path, 0) is False
        assert awake.hold(tmp_path / "no-such-task", 600) is False
        monkeypatch.setattr(awake, "available", lambda: False)
        assert awake.hold(tmp_path, 600) is False
        assert mac["spawned"] == []
        assert not (tmp_path / awake.PID_FILE).exists()

    def test_release_stops_the_holder_and_forgets_it(self, mac, tmp_path):
        awake.hold(tmp_path, 600)
        mac["running"][4242] = "caffeinate"
        awake.release(tmp_path)
        assert mac["killed"] == [(4242, signal.SIGTERM)]
        assert not (tmp_path / awake.PID_FILE).exists()

    def test_release_never_stops_another_program_that_now_has_the_number(self, mac, tmp_path):
        awake.hold(tmp_path, 600)
        mac["running"][4242] = "Safari"
        awake.release(tmp_path)
        assert mac["killed"] == []
        assert not (tmp_path / awake.PID_FILE).exists()

    def test_release_with_nothing_held_or_a_damaged_file_does_nothing(self, mac, tmp_path):
        awake.release(tmp_path)
        (tmp_path / awake.PID_FILE).write_text("not a number")
        awake.release(tmp_path)
        assert mac["killed"] == []

    def test_a_new_hold_replaces_the_old_one(self, mac, tmp_path):
        awake.hold(tmp_path, 600)
        mac["running"][4242] = "caffeinate"
        awake.hold(tmp_path, 900)
        assert mac["killed"] == [(4242, signal.SIGTERM)]
        assert len(mac["spawned"]) == 2


class TestAsleepStretches:
    """Reading the system's power log. Times in the log carry their own offset."""

    def between(self, start: str, finish: str) -> list[dict]:
        day = "2026-10-05T"
        return awake._asleep_stretches(POWER_LOG,
                                       datetime.fromisoformat(day + start + "+00:00"),
                                       datetime.fromisoformat(day + finish + "+00:00"))

    def test_a_sleep_lasts_until_the_next_full_wake_not_the_first_stir(self):
        assert self.between("09:00:00", "11:00:00") == [
            {"slept_at": "2026-10-05T09:03:13Z", "woke_at": "2026-10-05T10:37:28Z",
             "asleep_seconds": 5655}]

    def test_two_separate_sleeps_are_two_stretches(self):
        stretches = self.between("09:00:00", "13:00:00")
        assert [(s["slept_at"], s["asleep_seconds"]) for s in stretches] == [
            ("2026-10-05T09:03:13Z", 5655), ("2026-10-05T12:00:00Z", 600)]

    def test_a_sleep_that_began_after_the_finish_is_left_out(self):
        """The agent wrote its artifact at 09:02; the lid closed at 09:03."""
        assert self.between("08:00:00", "09:02:00") == []

    def test_a_sleep_that_began_before_the_start_is_left_out(self):
        assert self.between("09:30:00", "11:00:00") == []

    def test_lines_about_wake_requests_and_assertions_are_not_wakes(self):
        assert len(self.between("11:30:00", "13:00:00")) == 1

    def test_unreadable_log_is_unknown_not_none_slept(self, monkeypatch):
        monkeypatch.setattr(awake, "sleeps_between", REAL_SLEEPS_BETWEEN)
        now = datetime.now(timezone.utc)
        monkeypatch.setattr(awake.sys, "platform", "linux")
        assert awake.sleeps_between(now - timedelta(hours=1), now) is None
        monkeypatch.setattr(awake.sys, "platform", "darwin")

        class Failed:
            returncode, stdout = 1, ""
        monkeypatch.setattr(awake.subprocess, "run", lambda *args, **options: Failed())
        assert awake.sleeps_between(now - timedelta(hours=1), now) is None


class TestRelayHoldsAndReleases:
    def test_starting_a_phase_holds_for_the_configured_time(self, config, no_real_keep_awake):
        task_at("hold", config, [])
        assert no_real_keep_awake == [(settings.TASKS_DIR / "hold", awake.DEFAULT_SECONDS)]
        config["keep_awake_seconds"] = 300
        finish_step("hold", config, "ready-for-architect")
        assert commands.cmd_next("hold", auto=False, config=config) == 0
        assert no_real_keep_awake[-1] == (settings.TASKS_DIR / "hold", 300)

    def test_next_releases_once_the_phase_has_a_verdict_and_not_before(self, config, monkeypatch):
        released = []
        monkeypatch.setattr(awake, "release", released.append)
        task_at("let-go", config, [])
        with pytest.raises(SystemExit):
            run_relay(config, "next", "let-go")  # the agent is still working
        assert released == []
        finish_step("let-go", config, "ready-for-architect")
        assert commands.cmd_next("let-go", auto=False, config=config) == 0
        assert released == [settings.TASKS_DIR / "let-go"]

    def test_abort_and_closing_a_task_release(self, config, monkeypatch):
        released = []
        monkeypatch.setattr(awake, "release", released.append)
        task_at("stop", config, [])
        assert commands.cmd_abort("stop", config) == 0
        task_at("close", config, [])
        assert commands.cmd_override("close", "done", config) == 0
        assert released == [settings.TASKS_DIR / "stop", settings.TASKS_DIR / "close"]


class TestSleepIsNoticed:
    def slept_once(self, monkeypatch, seconds=3600):
        """As if the power log showed one sleep inside whatever window is asked about."""
        asked = []

        def fake(started, finished):
            asked.append((started, finished))
            return [{"slept_at": "2026-10-05T09:03:13Z", "woke_at": "2026-10-05T10:03:13Z",
                     "asleep_seconds": seconds}]
        monkeypatch.setattr(awake, "sleeps_between", fake)
        return asked

    def test_next_says_so_and_records_it_once(self, config, monkeypatch, capsys):
        config["human_gates"]["after_designer"] = True
        task_at("nap", config, [])
        asked = self.slept_once(monkeypatch)
        finish_step("nap", config, "ready-for-architect")
        capsys.readouterr()
        assert commands.cmd_next("nap", auto=False, config=config) == 0
        out = capsys.readouterr().out
        assert ("went to sleep once, for 60 minutes in all, between designer (step 1) being "
                "dispatched and its artifact being written") in out
        assert commands.cmd_next("nap", auto=False, config=config) == 0  # at the gate again: same step

        noted = read_api.status_payload("nap", config)["interruptions"]
        assert noted == [{"phase": "designer", "step": 1, "noticed": noted[0]["noticed"],
                          "sleeps": 1, "asleep_seconds": 3600,
                          "first_slept_at": "2026-10-05T09:03:13Z",
                          "last_slept_at": "2026-10-05T09:03:13Z"}]

        # The window asked about ends when the artifact was written, not now:
        # a lid closed after the agent finished is not the step's sleep.
        started, finished = asked[0]
        artifact = settings.TASKS_DIR / "nap" / "01-designer.md"
        assert finished == datetime.fromtimestamp(artifact.stat().st_mtime, timezone.utc)
        assert started <= finished

    def test_status_lists_it(self, config, monkeypatch, capsys):
        task_at("nap-shown", config, [])
        self.slept_once(monkeypatch, seconds=7200)
        finish_step("nap-shown", config, "ready-for-architect")
        assert commands.cmd_next("nap-shown", auto=False, config=config) == 0
        capsys.readouterr()
        commands.cmd_status("nap-shown", config)
        out = capsys.readouterr().out
        assert "The Mac slept while a step was open:" in out
        assert "step 1 (designer): 1 time(s), 120 minutes in all" in out

    @pytest.mark.parametrize("answer", [[], None])
    def test_no_sleep_or_an_unreadable_log_says_and_records_nothing(self, config, monkeypatch,
                                                                    capsys, answer):
        monkeypatch.setattr(awake, "sleeps_between", lambda started, finished: answer)
        task_at("fresh", config, UP_TO_REVIEW)
        assert "went to sleep" not in capsys.readouterr().out
        assert read_api.status_payload("fresh", config)["interruptions"] == []


class TestTheSettingIsChecked:
    @pytest.mark.parametrize("bad", ["2h", -5, True, 1.5, None])
    def test_a_bad_keep_awake_value_warns_and_uses_the_default(self, config, no_real_keep_awake,
                                                               capsys, bad):
        config["keep_awake_seconds"] = bad
        task_at("typo", config, [])
        assert no_real_keep_awake == [(settings.TASKS_DIR / "typo", awake.DEFAULT_SECONDS)]
        assert "keep_awake_seconds must be a whole number of seconds" in capsys.readouterr().out
