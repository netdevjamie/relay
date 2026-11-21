"""Running an agent for a phase: building its prompt and invoking it (or
printing the command).

Under --auto each role runs with the model, effort, permission mode and
time limit Relay's own config gives it (launch_settings), in a process
group of its own, so a role that runs past its limit is stopped with
every process it started.
"""

import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import yaml

import arbiter
import awake
import base_trust
import db
import report_reader
import role_context
import run_state
import settings
from printable import printable
from settings import _resolve_repo_path, _task_cwd, load_yaml


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------

# The phases whose reports rate their findings.
RATING_PHASES = ("reviewer", "tester", "security")

# The name `claude --agent` is given for each phase Relay dispatches under
# --auto. Claude Code finds a role by the `name:` line of its file, so each
# value must equal that line in agents/<phase>.md; tests/test_agent_dispatch.py
# reads the real files and fails when one differs. The legacy `pentest` phase
# has no role file and so no entry: Relay refuses to start it automatically.
AGENT_NAMES = {
    "designer": "Designer",
    "architect": "Architect",
    "builder": "Builder",
    "reviewer": "Reviewer",
    "tester": "Tester",
    "security": "Security",
    "ops": "Ops",
    "arbiter": "Arbiter",
}


# What `claude --permission-mode` and `claude --effort` accept, as
# `claude --help` lists them at Claude Code 2.1.330. A newer Claude Code may
# accept more; a value outside these is refused with a warning.
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Every role runs in the mode that never asks, because a question under
# --print cannot be answered. The roles that only write reports keep Bash,
# which their Write hook does not check, so only their role files' words
# keep them read-only through the shell.
DEFAULT_PERMISSION_MODES = {phase: "bypassPermissions" for phase in AGENT_NAMES}

# Roles handed every report in step order instead of the curated list
# (role_context.reading_list). The Arbiter reads the whole loop with fresh
# eyes, so it is handed all of it.
FULL_HISTORY_PHASES: frozenset = frozenset({"arbiter"})

# How long a stopped role's process group has to exit after TERM before KILL.
STOP_GRACE_SECONDS = 10

# The signals that end Relay which a role, in a session of its own, does not
# get: a closed terminal's and a plain `kill`'s. Relay stops the role first.
RELAY_STOP_SIGNALS = (signal.SIGHUP, signal.SIGTERM)

# The signals ignored once a stop of the role has begun, so that a second
# one cannot cut the grace short and leave the role running: the two above
# and Ctrl-C's.
STOP_IGNORES_SIGNALS = (*RELAY_STOP_SIGNALS, signal.SIGINT)

# The settings a role may have under `roles:` in Relay's config.
ROLE_SETTING_KEYS = ("model", "effort", "timeout_seconds", "permission_mode")

# What reading a task's events raises when one payload is not the JSON
# object expected; only a hand-edited ledger holds such a row.
_EVENTS_UNREADABLE = (ValueError, RecursionError, TypeError, AttributeError)


def rating_line(phase: str) -> str:
    """What the prompt of a phase that rates its findings says about
    ratings; empty for any other phase. The role file defines the ratings
    and gives the verdict for each, so this names them and says no more."""
    if phase not in RATING_PHASES:
        return ""
    *earlier, last = (f"`{severity}`" for severity in report_reader.SEVERITIES)
    return (f"Rate every finding {', '.join(earlier)} or {last}, as your agent definition "
            f"defines them. The highest rating among your findings decides your verdict.")


def _stopped_steps(task_id: str, config: dict | None) -> frozenset:
    """The steps whose role the time limit stopped; none when they cannot
    be read."""
    if config is None:
        return frozenset()
    try:
        return frozenset(stop["step"] for stop in read_time_limit_stops(task_id, config))
    except _EVENTS_UNREADABLE:
        return frozenset()


def build_prompt(phase: str, task_id: str, step: int, artifact_name: str,
                 state: dict | None = None, *, config: dict | None = None,
                 answers: tuple[str, ...] = ()) -> str:
    """Build the prompt to send to the agent. `answers` are the paths of
    the reports this round answers; `config` lets the reading list leave
    out a report written in a step the time limit stopped."""
    task_dir = settings.TASKS_DIR / task_id
    output_path = f"{task_dir}/{artifact_name}"
    reading = role_context.reading_list(
        task_dir, task_dir / "brief.md", artifact_name, answers=answers,
        full_history=phase in FULL_HISTORY_PHASES, stopped_steps=_stopped_steps(task_id, config))

    base = (
        f"You are executing phase '{phase}' for task '{task_id}'.\n\n"
        f"Follow your agent definition exactly.\n\n"
        f"{reading}\n\n"
        f"Write your handoff artifact to {output_path}.\n\n"
        f"IMPORTANT: Your artifact MUST begin with YAML frontmatter in this exact format:\n"
        f"---\n"
        f"task_id: {task_id}\n"
        f"phase: {phase}\n"
        f"step: {step}\n"
        f"date: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"verdict: <your verdict>\n"
        f"---\n\n"
        f"Use the verdict values defined in your agent definition. "
        f"The orchestrator reads this frontmatter to route the workflow."
    )
    if rating_line(phase):
        base += f"\n\n{rating_line(phase)}"

    # Tell the agent where code work must happen: the checkout `relay start`
    # recorded, on the task's own branch.
    work_dir = _task_cwd(state) if state else None
    if work_dir:
        loc = (
            f"\n\nAll code changes for this task must be made in this working directory:\n"
            f"  {work_dir}\n"
        )
        branch = state.get("branch") if state else None
        if branch:
            loc += f"This is a dedicated git worktree on branch '{branch}'"
            loc += _based_on(state)
            loc += ". Do not edit the repo anywhere else.\n"
        base += loc

    if phase == "ops" and state:
        addendum = _ops_addendum(state)
        if addendum:
            addendum_file, text = addendum
            base += (
                f"\n\n# Project-specific deploy context\n"
                f"The following is from the target repo's {addendum_file}:\n\n"
                f"{text}"
            )

    return base


def _based_on(state: dict) -> str:
    """What the location line says of the base: its name and the commit
    Relay confirmed for it, which every role reads the threat model at.
    The name is the owner's, shown through printable() all the same."""
    if not base_trust.has_base(state):
        return ""
    name = printable(state["base"], base_trust.NAME_LIMIT)
    commit = base_trust.trusted_commit(state)
    if commit is None:
        return f" (based on '{name}'; Relay has no confirmed commit for it)"
    return f" (based on '{name}' at {commit})"


def _committed_text(repo_path, commit: str, relative_path: str) -> str | None:
    """The UTF-8 text of the file at `relative_path` in `commit` (a commit
    id), or None when there is no such file there (a folder, a path out of
    the repo) or it is not UTF-8. Git reads the commit, never the working
    tree, so no checkout's branch can change it."""
    try:
        shown = subprocess.run(["git", "cat-file", "blob", f"{commit}:{relative_path}"],
                               cwd=repo_path, capture_output=True)
    except OSError:
        return None
    if shown.returncode != 0:
        return None
    try:
        return shown.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _ops_addendum(state: dict) -> tuple[str, str] | None:
    """(file name, text) of the repo's Ops addendum: the file its
    `.relay.yaml` names as `ops_addendum`, both read in the registered repo
    at the base commit Relay confirmed for the task (base_trust.py), so a
    task's branch cannot plant either, wherever the task builds, and no ref
    of any name (a tag, a case twin of the base) can stand in for the base.
    None when there is none, the task has no confirmed base commit, or
    either file cannot be read safely.
    The text is pasted word for word: a prompt-injection surface
    docs/known-limits.md records."""
    base_commit = base_trust.trusted_commit(state)
    if base_commit is None or not state.get("repo") or not settings.REGISTRY_PATH:
        return None
    repo_path = _resolve_repo_path(state["repo"])
    if not repo_path:
        return None
    config_text = _committed_text(repo_path, base_commit, ".relay.yaml")
    if config_text is None:
        return None
    try:
        repo_config = load_yaml(config_text)
    except yaml.YAMLError:
        return None
    addendum_file = repo_config.get("ops_addendum") if isinstance(repo_config, dict) else None
    if not isinstance(addendum_file, str) or not addendum_file:
        return None
    text = _committed_text(repo_path, base_commit, addendum_file)
    return (addendum_file, text) if text is not None else None


def _earlier_builds(state: dict, step: int) -> int:
    """How many Builder steps the task's history has before `step`. A fix
    report is numbered by this, not by the count of automatic rounds, so the
    report after a send-back is named like any other fix."""
    return sum(1 for entry in state.get("history") or []
               if isinstance(entry, dict) and entry.get("phase") == "builder"
               and isinstance(entry.get("step"), int) and entry["step"] < step)


def artifact_name(phase: str, step: int, state: dict) -> str:
    """Generate artifact filename."""
    retries = state.get("retries", {})
    retry_count = retries.get(phase, 0)

    fix_number = _earlier_builds(state, step) if phase == "builder" else 0

    if fix_number > 0:
        return f"{step:02d}-builder-fix-{fix_number}.md"
    elif phase == "reviewer" and retry_count > 0:
        return f"{step:02d}-reviewer-pass-{retry_count + 1}.md"
    elif phase == "tester" and retry_count > 0:
        return f"{step:02d}-tester-pass-{retry_count + 1}.md"
    else:
        return f"{step:02d}-{phase}.md"


# ---------------------------------------------------------------------------
# What a role runs with under --auto
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LaunchSettings:
    """What one role runs with under --auto. `warnings` are the config
    values that were refused, one line each, for the caller to print."""
    model: str | None
    effort: str | None
    permission_mode: str
    timeout_seconds: int
    warnings: tuple[str, ...]


def _is_whole_seconds(value) -> bool:
    # Python: bool is a kind of int, so it is ruled out by name.
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _own_settings(phase: str, config: dict, warnings: list[str]) -> dict:
    """This role's entry under `roles:`, after warning about every entry
    and key Relay does not know. {} when it has none."""
    roles = config.get("roles") or {}
    if not isinstance(roles, dict):
        warnings.append(f"Warning: roles must be a mapping of role names to settings "
                        f"(found {roles!r}); ignored.")
        return {}
    for name in roles:
        if name not in AGENT_NAMES:
            warnings.append(f"Warning: roles.{name} is not one of Relay's roles "
                            f"({', '.join(AGENT_NAMES)}); ignored.")
    own = roles.get(phase)
    if own is None:
        return {}
    if not isinstance(own, dict):
        warnings.append(f"Warning: roles.{phase} must be a mapping of settings "
                        f"(found {own!r}); ignored.")
        return {}
    for key in own:
        if key not in ROLE_SETTING_KEYS:
            warnings.append(f"Warning: roles.{phase}.{key} is not a setting Relay knows "
                            f"({', '.join(ROLE_SETTING_KEYS)}); ignored.")
    return own


def _first_valid(candidates: list[tuple[str, object]], is_valid, rule: str,
                 fallback, warnings: list[str]):
    """The first set candidate that is valid, else `fallback`. Each
    candidate is (its name in the config, its value); a value that is set
    but not valid is warned about and the next one is tried."""
    for position, (name, value) in enumerate(candidates):
        if value is None:
            continue
        if is_valid(value):
            return value
        later = next((later_value for _, later_value in candidates[position + 1:]
                      if later_value is not None and is_valid(later_value)), fallback)
        shown = "the default" if later is None else later
        warnings.append(f"Warning: {name} must be {rule} (found {value!r}). Using {shown}.")
    return fallback


def launch_settings(phase: str, config: dict) -> LaunchSettings:
    """The model, effort, permission mode and time limit `phase` runs with
    under --auto. A role's own value under `roles:` wins; an unset one falls
    back to the global value, and then to Claude's own default (model and
    effort) or Relay's (permission mode and time limit). Pure: refused
    values come back as warnings, nothing is printed."""
    warnings: list[str] = []
    own = _own_settings(phase, config, warnings)
    model = _first_valid(
        [(f"roles.{phase}.model", own.get("model")), ("model", config.get("model") or None)],
        lambda value: isinstance(value, str) and value.strip() != "", "a model name",
        None, warnings)
    effort = _first_valid(
        [(f"roles.{phase}.effort", own.get("effort")), ("effort", config.get("effort"))],
        lambda value: value in EFFORT_LEVELS, f"one of {', '.join(EFFORT_LEVELS)}",
        None, warnings)
    permission_mode = _first_valid(
        [(f"roles.{phase}.permission_mode", own.get("permission_mode"))],
        lambda value: value in PERMISSION_MODES, f"one of {', '.join(PERMISSION_MODES)}",
        DEFAULT_PERMISSION_MODES[phase], warnings)
    timeout_seconds = _first_valid(
        [(f"roles.{phase}.timeout_seconds", own.get("timeout_seconds")),
         (f"agent_timeout_seconds (for {phase})", config.get("agent_timeout_seconds"))],
        _is_whole_seconds, "a whole number of seconds above 0",
        settings.DEFAULT_AGENT_TIMEOUT_SECONDS, warnings)
    return LaunchSettings(model, effort, permission_mode, timeout_seconds, tuple(warnings))


def claude_command(agent_name: str, prompt: str, launch: LaunchSettings) -> list[str]:
    """The `claude` command line for one role: the agent, the prompt and
    the model, then the effort and the permission mode."""
    command = ["claude", "--agent", agent_name, "--print", prompt]
    if launch.model:
        command += ["--model", launch.model]
    if launch.effort:
        command += ["--effort", launch.effort]
    return command + ["--permission-mode", launch.permission_mode]


def _minutes_or_seconds(seconds: int, minute_word: str, second_word: str) -> str:
    """`120 min` or `90 s` (or the words given for each): minutes when the
    limit is a whole number of them."""
    if seconds % 60 == 0:
        return f"{seconds // 60}{minute_word}"
    return f"{seconds}{second_word}"


def limit_in_words(seconds) -> str:
    """The limit as a sentence names it: "120-minute", or "90-second"; just
    "time" for a limit a hand-edited ledger left unreadable."""
    if not _is_whole_seconds(seconds):
        return "time"
    return _minutes_or_seconds(seconds, "-minute", "-second")


def _launch_line(phase: str, launch: LaunchSettings) -> str:
    return (f"Invoking @{phase} automatically (model: {launch.model or 'default'}, "
            f"effort: {launch.effort or 'default'}, permission: {launch.permission_mode}, "
            f"limit {_minutes_or_seconds(launch.timeout_seconds, ' min', ' s')})...")


# ---------------------------------------------------------------------------
# Running a role with a time limit
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoleRun:
    """How a role's run ended: its exit code (None when it was stopped),
    whether the time limit stopped it, and how long it ran, in seconds."""
    returncode: int | None
    timed_out: bool
    ran_seconds: int


def _signal_group(group: int, signal_number: int) -> None:
    try:
        os.killpg(group, signal_number)
    except ProcessLookupError:
        pass  # every process in the group has already gone


def _stop_group(role: subprocess.Popen) -> None:
    """Stop the role's whole process group: TERM, a grace period, then KILL
    whatever is left, so a child that ignores TERM still goes once the
    role itself has exited."""
    _signal_group(role.pid, signal.SIGTERM)
    try:
        role.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(role.pid, signal.SIGKILL)
    role.wait()


class RelayStopped(BaseException):
    """Relay got one of RELAY_STOP_SIGNALS while a role ran."""

    def __init__(self, signal_number: int):
        super().__init__(signal_number)
        self.signal_number = signal_number


def _raise_relay_stopped(signal_number, frame):
    raise RelayStopped(signal_number)


def _on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


@contextmanager
def _stop_signals_raised():
    """While the block runs, each of RELAY_STOP_SIGNALS raises RelayStopped,
    and on leaving it the handlers Relay had for STOP_IGNORES_SIGNALS are
    put back. A signal Relay was told to ignore (as under `nohup`) stays
    ignored, as it is for the role. Only the main thread can set a handler;
    elsewhere nothing changes."""
    if not _on_main_thread():
        yield
        return
    previous = {number: signal.getsignal(number) for number in STOP_IGNORES_SIGNALS}
    for number in RELAY_STOP_SIGNALS:
        if previous[number] != signal.SIG_IGN:
            signal.signal(number, _raise_relay_stopped)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _stop_group_whatever_comes(role: subprocess.Popen) -> None:
    """_stop_group with STOP_IGNORES_SIGNALS ignored from here on, so a
    signal during the grace cannot leave the role running. Inside
    _stop_signals_raised, whose end puts Relay's handlers back."""
    if _on_main_thread():
        for number in STOP_IGNORES_SIGNALS:
            signal.signal(number, signal.SIG_IGN)
    _stop_group(role)


def run_role(command: list[str], cwd: str | None, timeout_seconds: int) -> RoleRun:
    """Run a role's command in a process group of its own and wait up to
    `timeout_seconds` for it. Past that, or on an interrupt (Ctrl-C now
    reaches Relay, not the role), the whole group is stopped. So it is
    when Relay gets SIGHUP or SIGTERM, which the role, in a session of
    its own, does not get: Relay then ends by that signal, as it would
    have without a role running."""
    started = time.monotonic()
    ending_signal = None
    with _stop_signals_raised():
        role = subprocess.Popen(command, cwd=cwd, start_new_session=True)
        try:
            returncode = role.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _stop_group_whatever_comes(role)
            return RoleRun(None, True, int(time.monotonic() - started))
        except RelayStopped as stopped:
            _stop_group_whatever_comes(role)
            ending_signal = stopped.signal_number
        except BaseException:
            _stop_group_whatever_comes(role)
            raise
    if ending_signal is not None:
        os.kill(os.getpid(), ending_signal)  # with Relay's own handler back in place
        raise RelayStopped(ending_signal)  # reached only when that handler returns
    return RoleRun(returncode, False, int(time.monotonic() - started))


def invoke_agent(phase: str, task_id: str, prompt: str, config: dict,
                 auto: bool = False, cwd: str | None = None, then_run: str | None = None,
                 step: int | None = None):
    """Invoke a Claude Code agent either automatically or by printing the command.

    When cwd is set (the task's worktree), the agent subprocess runs there, so code work lands on the relay branch rather than in whatever
    directory relay happened to be invoked from. `then_run` is the command
    printed for after the agent has finished, in manual mode: `relay next`
    unless the caller says otherwise. `step` is the step the role runs
    for, which a time-limit stop records.

    Returns the exit code: 1 when an automatic run cannot start the role,
    claude exits non-zero, or the role runs past its time limit, else 0.
    The caller passes it on.
    """
    agent_name = AGENT_NAMES.get(phase)
    if auto and agent_name is None:
        print(f"\n  Relay cannot start {phase} automatically: no role file is named for it. "
              f"Nothing was started.")
        return 1

    # The phase may run for a long time with nobody at the keyboard. In manual
    # mode Relay exits now and the agent runs elsewhere, so the hold is left
    # in place for the next relay command (or its own time limit) to end.
    task_dir = settings.TASKS_DIR / task_id
    seconds = config.get("keep_awake_seconds", awake.DEFAULT_SECONDS)
    # Python: bool is a kind of int, so it is ruled out by name.
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
        print(f"  Warning: keep_awake_seconds must be a whole number of seconds, 0 or more "
              f"(found {seconds!r}). Using {awake.DEFAULT_SECONDS}.")
        seconds = awake.DEFAULT_SECONDS
    if awake.hold(task_dir, seconds):
        print(f"  Keeping this Mac awake for up to {seconds // 60} minutes while {phase} runs "
              f"(a closed lid still sleeps it).")

    if auto:
        launch = launch_settings(phase, config)
        for warning in launch.warnings:
            print(f"  {warning}")
        print(f"\n  {_launch_line(phase, launch)}")
        if cwd:
            print(f"  Working directory: {cwd}")
        try:
            run = run_role(claude_command(agent_name, prompt, launch), cwd, launch.timeout_seconds)
        finally:
            awake.release(task_dir)
        if run.timed_out:
            return _stop_after_time_limit(phase, task_id, step, run, launch.timeout_seconds, config)
        if run.returncode != 0:
            return _stop_after_a_failed_agent(phase, agent_name, task_id, run.returncode)
    else:
        print(f"\n  Run this in Claude Code:\n")
        print(f"    @{phase} {prompt}\n")
        if cwd:
            print(f"  Working directory: {cwd}")
        print(f"  Then run: {then_run or f'relay next {task_id}'}")

    return 0


# ---------------------------------------------------------------------------
# When a role stops without finishing
# ---------------------------------------------------------------------------

def time_limit_stop_lines(task_id: str, phase: str, limit_seconds: int) -> list[str]:
    """What Relay says about a role the time limit stopped, and the
    owner's ways on: start it again, or abort. `relay next` prints the
    same lines again for as long as the stop stands."""
    agent_name = AGENT_NAMES.get(phase, str(phase).capitalize())
    return [f"The {agent_name} role ran past its {limit_in_words(limit_seconds)} limit and was "
            f"stopped. Relay stopped here; nothing moved past this step.",
            f"To start it again: {settings._CLI_CMD} override {task_id} {phase} --auto",
            f"To abort: {settings._CLI_CMD} abort {task_id}"]


def _stop_after_time_limit(phase: str, task_id: str, step: int | None, run: RoleRun,
                           limit_seconds: int, config: dict) -> int:
    """Block the task, record the stop and say so, when a role ran past its
    limit. The task is saved as blocked first: should Relay die before the
    event is written, the task reads as paused, never as running for good.
    A blocked task is refused by `relay next` before any report is read, so
    whatever the stopped role left is never routed on. Returns 1."""
    state = run_state.load_run_state(task_id, config)
    if state is not None:
        state["status"] = "blocked"
        state["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_state.save_run_state(task_id, state, config)
    recorded = settings._sqlite_enabled(config)
    if recorded:
        db.record_time_limit_stop(
            settings.get_db_connection(), task_id, phase=phase,
            step=step if step is not None else arbiter.latest_step((state or {}).get("history")),
            limit_seconds=limit_seconds, ran_seconds=run.ran_seconds)
    print()
    for line in time_limit_stop_lines(task_id, phase, limit_seconds):
        print(f"  {line}")
    if not recorded:
        print("  The files backend keeps no events, so this stop is not recorded.")
    return 1


def read_time_limit_stops(task_id: str, config: dict) -> list[dict]:
    """Every time the time limit stopped one of the task's roles, oldest
    first. The files backend keeps no events, so it has none."""
    if not settings._sqlite_enabled(config):
        return []
    stops = []
    for event in db.get_events(settings.get_db_connection(), task_id):
        if event["event_type"] != db.EVENT_TIME_LIMIT_STOP:
            continue
        payload = event.get("payload") or {}
        stops.append({"phase": event["phase"], "step": event["step"],
                      "limit_seconds": payload.get("limit_seconds"),
                      "ran_seconds": payload.get("ran_seconds"),
                      "stopped_at": event["timestamp"]})
    return stops


def _stop_after_a_failed_agent(phase: str, agent_name: str, task_id: str, exit_code: int) -> int:
    """Say Relay stops, and return its exit code 1, when claude exited non-zero. The exit code
    cannot tell a role that never started from one that failed part way,
    so the line says both. The step stays recorded as
    dispatched, with no report."""
    print(f"\n  The {agent_name} role did not start or did not finish: claude exited with code "
          f"{exit_code}. Relay stopped here; nothing moved past this step.")
    if phase in settings.PIPELINE_ORDER or phase == settings.ARBITER:
        print(f"  To start it again: {settings._CLI_CMD} override {task_id} {phase} --auto")
    else:
        print(f"  To see where the task stands: {settings._CLI_CMD} status {task_id}")
    return 1

