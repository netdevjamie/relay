"""What a role is told to read before it starts: the task's brief and
reports, split into what it must read and what it may read.

A role is handed the brief, the latest report of each role that has run
(the latest Architect report is the current plan) and the report its round
answers. Every other file in the task folder is listed as optional history,
oldest first, so nothing is left unmentioned. A role marked as needing the
full history (agents.FULL_HISTORY_PHASES) is handed every report, in step
order, with nothing optional.

This module is pure: it lists the task folder's file names and returns
text. It never opens a report, so a report's contents cannot change what
is listed.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import settings

REQUIRED_HEADING = "Required reading: read each of these before you start."
OPTIONAL_HEADING = "Optional history, oldest first: read one only if you need it."
FULL_HEADING = "Required reading, every report in step order: read each before you start."
ANSWERS_LINE = "The report this round answers: {path}"
STOPPED_LABEL = "stopped by the time limit; not used"

# A file in the task folder that is listed: a step number of two digits or
# more, then a plain name, ending .md or .log. Any other name (a space, a
# line break, a character a prompt could read as an instruction) is not
# listed, because a role or a branch can name a file in the folder.
TASK_FILE_NAME = re.compile(r"([0-9]{2,})-([A-Za-z0-9._-]+)\.(md|log)")

REPORT, LOG, OTHER = "report", "log", "other"


@dataclass(frozen=True)
class TaskFile:
    """One numbered file in the task folder. `phase` is set for a report."""
    step: int
    path: Path
    kind: str
    phase: str | None = None


def _task_file(entry: Path) -> TaskFile | None:
    """The entry as a TaskFile, or None when its name is not one Relay
    lists or it is not a regular file."""
    match = TASK_FILE_NAME.fullmatch(entry.name)
    if not match or entry.is_symlink() or not entry.is_file():
        return None
    step, rest, suffix = int(match.group(1)), match.group(2), match.group(3)
    if suffix == "log":
        return TaskFile(step, entry, LOG)
    phase = rest.split("-")[0]
    if phase in settings.VALID_PHASES:
        return TaskFile(step, entry, REPORT, phase)
    return TaskFile(step, entry, OTHER)


def task_files(task_dir: Path, own_output: str) -> list[TaskFile]:
    """Every listed file in the task folder except the report this step
    writes, by step and then by name."""
    try:
        entries = list(task_dir.iterdir())
    except OSError:
        return []
    files = [task_file for entry in entries if entry.name != own_output
             for task_file in [_task_file(entry)] if task_file]
    return sorted(files, key=lambda task_file: (task_file.step, task_file.path.name))


def _label(task_file: TaskFile, stopped_steps: frozenset) -> str:
    if task_file.kind == REPORT:
        label = f"{task_file.phase.capitalize()} report"
    elif task_file.kind == LOG:
        label = "Relay's test-run log"
    else:
        label = "other file"
    if task_file.kind == REPORT and task_file.step in stopped_steps:
        label += f" ({STOPPED_LABEL})"
    return label


def _numbered_line(task_file: TaskFile, stopped_steps: frozenset) -> str:
    return f"- Step {task_file.step}, {_label(task_file, stopped_steps)}: {task_file.path}"


def _latest_line(task_file: TaskFile) -> str:
    if task_file.phase == "architect":
        return f"- The current plan (the latest Architect report): {task_file.path}"
    return f"- The latest {task_file.phase.capitalize()} report: {task_file.path}"


def _latest_of_each_role(files: list[TaskFile], stopped_steps: frozenset) -> list[TaskFile]:
    """Each role's highest-step report, in pipeline order, leaving out a
    report written in a step the time limit stopped."""
    latest = {}
    for task_file in files:
        if task_file.kind == REPORT and task_file.step not in stopped_steps:
            latest[task_file.phase] = task_file
    return [latest[phase] for phase in settings.VALID_PHASES if phase in latest]


def reading_list(task_dir: Path, brief_path: Path, own_output: str, *,
                 answers: tuple[str, ...] = (), full_history: bool = False,
                 stopped_steps: frozenset = frozenset()) -> str:
    """The reading part of a role's prompt. `answers` are the paths of the
    reports (or Relay's test-run log) the round answers; `stopped_steps`
    the steps whose role the time limit stopped."""
    files = task_files(task_dir, own_output)
    answer_lines = [ANSWERS_LINE.format(path=path) for path in answers]
    brief_line = f"- The task brief: {brief_path}"
    if full_history:
        lines = [FULL_HEADING, brief_line]
        lines += [_numbered_line(task_file, stopped_steps) for task_file in files]
        return "\n".join(lines + answer_lines)

    required = _latest_of_each_role(files, stopped_steps)
    lines = [REQUIRED_HEADING, brief_line] + [_latest_line(task_file) for task_file in required]
    lines += answer_lines
    listed = {task_file.path for task_file in required} | {Path(path) for path in answers}
    optional = [task_file for task_file in files if task_file.path not in listed]
    if optional:
        lines += ["", OPTIONAL_HEADING]
        lines += [_numbered_line(task_file, stopped_steps) for task_file in optional]
    return "\n".join(lines)
