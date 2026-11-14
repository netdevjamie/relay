"""Running an agent for a phase: building its prompt and invoking it (or
printing the command).
"""

import subprocess
import sys
from datetime import datetime

import awake
import report_reader
import settings
from settings import (
    _IS_LEGACY, _resolve_repo_path, _task_cwd, load_yaml,
)


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
}


def rating_line(phase: str) -> str:
    """What the prompt of a phase that rates its findings says about
    ratings; empty for any other phase. The role file defines the ratings
    and gives the verdict for each, so this names them and says no more."""
    if phase not in RATING_PHASES:
        return ""
    *earlier, last = (f"`{severity}`" for severity in report_reader.SEVERITIES)
    return (f"Rate every finding {', '.join(earlier)} or {last}, as your agent definition "
            f"defines them. The highest rating among your findings decides your verdict.")


def build_prompt(phase: str, task_id: str, step: int, artifact_name: str,
                 state: dict | None = None) -> str:
    """Build the prompt to send to the agent."""
    task_dir = str(settings.TASKS_DIR / task_id)
    brief_path = str(settings.TASKS_DIR / task_id / "brief.md")
    if _IS_LEGACY:
        task_dir = f".workflow/handoffs/{task_id}"
        brief_path = f".workflow/briefs/{task_id}.md"
    output_path = f"{task_dir}/{artifact_name}"

    base = (
        f"You are executing phase '{phase}' for task '{task_id}'.\n\n"
        f"Follow your agent definition exactly.\n\n"
        f"Read the task brief at {brief_path}.\n"
        f"Read all existing artifacts in {task_dir}/ for context.\n\n"
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
            base_ref = state.get("base")
            loc += f"This is the task's checkout, on branch '{branch}'"
            if base_ref:
                loc += f" (based on '{base_ref}')"
            loc += ". Do not edit the repo anywhere else.\n"
        base += loc

    # ops_addendum: inject per-repo deploy context for the ops phase
    if phase == "ops" and state and state.get("repo") and settings.REGISTRY_PATH:
        repo_path = _resolve_repo_path(state["repo"])
        if repo_path:
            relay_yaml = repo_path / ".relay.yaml"
            if relay_yaml.exists():
                with open(relay_yaml) as f:
                    repo_config = load_yaml(f.read()) or {}
                addendum_file = repo_config.get("ops_addendum")
                if addendum_file:
                    addendum_path = repo_path / addendum_file
                    if addendum_path.exists():
                        addendum = addendum_path.read_text()
                        base += (
                            f"\n\n# Project-specific deploy context\n"
                            f"The following is from the target repo's {addendum_file}:\n\n"
                            f"{addendum}"
                        )

    return base


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


def invoke_agent(phase: str, task_id: str, prompt: str, config: dict,
                 auto: bool = False, cwd: str | None = None, then_run: str | None = None):
    """Invoke a Claude Code agent either automatically or by printing the command.

    When cwd is set (the task's worktree), the agent subprocess runs there, so code work lands on the relay branch rather than in whatever
    directory relay happened to be invoked from. `then_run` is the command
    printed for after the agent has finished, in manual mode: `relay next`
    unless the caller says otherwise.
    """
    agent_name = AGENT_NAMES.get(phase)
    if auto and agent_name is None:
        print(f"\n  Relay cannot start {phase} automatically: no role file is named for it. "
              f"Nothing was started.")
        sys.exit(1)

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
        print(f"\n  Invoking @{phase} automatically...")
        if cwd:
            print(f"  Working directory: {cwd}")
        cmd = ["claude", "--agent", agent_name, "--print", prompt]
        model = config.get("model")
        if model:
            cmd.extend(["--model", model])
        try:
            result = subprocess.run(cmd, cwd=cwd, capture_output=False, text=True)
        finally:
            awake.release(task_dir)
        if result.returncode != 0:
            _stop_after_a_failed_agent(phase, agent_name, task_id, result.returncode)
    else:
        # Claude Code hands a request to a role it names in words; the role's
        # name is its `name:` line, not the phase.
        asks_for_the_role = f"Use the {agent_name} subagent:" if agent_name else f"@{phase}"
        print(f"\n  Run this in Claude Code:\n")
        print(f"    {asks_for_the_role} {prompt}\n")
        if cwd:
            print(f"  Working directory: {cwd}")
        print(f"  Then run: {then_run or f'relay next {task_id}'}")

    return True


def _stop_after_a_failed_agent(phase: str, agent_name: str, task_id: str, exit_code: int):
    """Stop Relay with code 1 when claude exited non-zero. The exit code
    cannot tell a role that never started from one that failed part way,
    so the line says both. The step stays recorded as
    dispatched, with no report."""
    print(f"\n  The {agent_name} role did not start or did not finish: claude exited with code "
          f"{exit_code}. Relay stopped here; nothing moved past this step.")
    if phase in settings.PIPELINE_ORDER:
        print(f"  To start it again: {settings._CLI_CMD} override {task_id} {phase} --auto")
    else:
        print(f"  To see where the task stands: {settings._CLI_CMD} status {task_id}")
    sys.exit(1)

