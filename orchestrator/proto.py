"""Prototype mode: `relay proto` and `relay promote`.

A prototype answers "is this worth building?" in a few hours, with
something the owner can see. It skips the chain on purpose: one paragraph
from the owner (what it is, who it is for, what "wow" looks like), no
brief, no Architect, no role passes, no status after every step. The main
session builds it on a `proto/<name>` branch, inside a timebox, and shows
it with screenshots at real device sizes and a short clip.

The guardrails are the price of skipping the chain: never production, a
preview deploy at most, demo data only, new features flagged off, no
secrets. And prototype code is never merged: `relay promote` turns a
prototype the owner likes into a real task, whose brief takes the
prototype's screenshots as its spec and is built again on a fresh branch
through the normal chain. `relay start` refuses a `proto/` branch.

A prototype's record is `proto.yaml` in its task folder, written only by
these commands. It is not in the ledger: it is not a run of the chain.
"""

import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import settings

PROTO_BRANCH_PREFIX = "proto/"
# Where the printed worktree commands put a checkout. Relay makes none itself.
WORKTREES_ROOT = Path(os.environ.get("RELAY_WORKTREES_ROOT", Path.home() / "Projects" / "worktrees"))
RECORD = "proto.yaml"
EVIDENCE = "evidence"
SPEC = "spec"
DEFAULT_HOURS = 4
MOST_HOURS = 8
SCREENSHOT_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
CLIP_SUFFIXES = (".mp4", ".mov", ".webm", ".gif")

BUILDING = "building"
SHOWN = "shown"
PROMOTED = "promoted"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]$|^[A-Za-z0-9]$")

GUARDRAILS = (
    "Never production: no production deploy, database, store submission or real customer data.",
    "A preview deploy at most, and only to a preview or local target.",
    "Demo data only: made-up names, numbers and addresses.",
    "New features behind a flag that is off by default.",
    "No secrets: none printed, none committed, none added.",
    "This code is never merged. It is a sketch the owner looks at; the real build starts fresh.",
)


class ProtoError(Exception):
    """Why a prototype command did nothing. The message is one line."""


def is_proto_branch(branch: str | None) -> bool:
    return bool(branch) and branch.startswith(PROTO_BRANCH_PREFIX)


def _check_name(name: str) -> str:
    if not name or not _SAFE_NAME.match(name) or ".." in name or name.endswith(".lock"):
        raise ProtoError(f"'{name}' cannot name a prototype: use letters, digits, '.', '_' and '-', "
                         "starting and ending with a letter or digit")
    return name


def _folder(name: str) -> Path:
    return settings.TASKS_DIR / _check_name(name)


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def read_record(name: str) -> dict:
    path = _folder(name) / RECORD
    if not path.exists():
        raise ProtoError(f"there is no prototype named {name} (no {RECORD} in its task folder)")
    try:
        record = settings.load_yaml(path.read_text())
    except Exception as problem:  # YAML's errors, a file that is not text
        raise ProtoError(f"{path} could not be read ({problem.__class__.__name__})") from None
    if not isinstance(record, dict) or record.get("kind") != "prototype":
        raise ProtoError(f"{path} is not a prototype's record")
    return record


def _write_record(name: str, record: dict) -> None:
    (_folder(name) / RECORD).write_text(settings.dump_yaml(record))


def start(name: str, repo: str | None, idea: str | None, hours: float | None = None,
          now: datetime | None = None) -> list[str]:
    """Record a new prototype and return what to print: the branch to build
    on and the prompt for the main session."""
    folder = _folder(name)
    if folder.exists():
        raise ProtoError(f"{folder} already exists: pick another name for the prototype")
    if not repo:
        raise ProtoError("say which repo with --repo <alias>")
    repo_path = settings._resolve_repo_path(repo)
    if repo_path is None:
        raise ProtoError(f"no repo is registered as {repo}")
    idea = (idea or "").strip()
    if len(idea.split()) < 8:
        raise ProtoError("give the idea as one paragraph with --idea: what it is, who it is for, "
                         "and what 'wow' looks like")
    hours = DEFAULT_HOURS if hours is None else hours
    if not 0 < hours <= MOST_HOURS:
        raise ProtoError(f"the timebox is more than 0 and at most {MOST_HOURS} hours")
    started = _now(now)
    record = {
        "kind": "prototype",
        "name": name,
        "repo": repo,
        "repo_path": str(repo_path),
        "branch": f"{PROTO_BRANCH_PREFIX}{name}",
        "idea": idea,
        "timebox_hours": hours,
        "started": _iso(started),
        "ends": _iso(started + timedelta(hours=hours)),
        "status": BUILDING,
    }
    (folder / EVIDENCE).mkdir(parents=True)
    _write_record(name, record)
    return [f"Prototype {name} started. Timebox: {hours:g} hours, until {record['ends']}.", "",
            "Build it in its own worktree, on its own branch:",
            f"  git -C {repo_path} worktree add -b {record['branch']} "
            f"{WORKTREES_ROOT / f'{repo_path.name}-proto-{name}'}", "",
            "Give the main session this prompt:", "", *prompt_lines(record), "",
            f"When it is ready to show: {settings._CLI_CMD} proto done {name}"]


def prompt_lines(record: dict) -> list[str]:
    evidence = _folder(record["name"]) / EVIDENCE
    return [
        f"Prototype '{record['name']}' in {record['repo_path']}, on branch {record['branch']}.",
        f"The owner's idea: {record['idea']}",
        f"Timebox: until {record['ends']}. Stop at the end of it and show what there is.",
        "Build it directly: no brief, no plan, no role passes, no status after every step. "
        "Send one message when there is something to look at.",
        "Guardrails:",
        *[f"- {rule}" for rule in GUARDRAILS],
        f"Evidence: screenshots at real device sizes (a phone, a tablet or laptop where it applies) "
        f"and one short clip of the main flow, saved in {evidence}/.",
    ]


def done(name: str, now: datetime | None = None) -> list[str]:
    """The prototype is ready to show. Refused without a screenshot."""
    record = read_record(name)
    if record.get("status") == PROMOTED:
        raise ProtoError(f"{name} was already promoted to {record.get('promoted_to')}")
    shots, clips = evidence_files(name)
    if not shots:
        raise ProtoError(f"no screenshot in {_folder(name) / EVIDENCE}/: a prototype is shown, "
                         "not described")
    finished = _now(now)
    started = datetime.fromisoformat(record["started"])
    minutes = int((finished - started).total_seconds() // 60)
    record.update({"status": SHOWN, "finished": _iso(finished), "minutes": minutes})
    _write_record(name, record)
    over = minutes - int(record["timebox_hours"] * 60)
    lines = [f"Prototype {name} is ready to show: {minutes} minutes"
             + (f", {over} over its timebox." if over > 0 else ", inside its timebox."),
             f"  Screenshots: {len(shots)}; clips: {len(clips)}"]
    lines += [f"    {path}" for path in shots + clips]
    if not clips:
        lines.append("  No clip yet: the owner sees still pictures only.")
    lines.append(f"If the owner likes it: {settings._CLI_CMD} promote {name} --size feature --risky no")
    return lines


def evidence_files(name: str) -> tuple[list[Path], list[Path]]:
    folder = _folder(name) / EVIDENCE
    # Links are passed over: a prototype's evidence is files saved there,
    # and a link could pull a file from anywhere into a promoted task.
    files = (sorted(path for path in folder.rglob("*")
                    if path.is_file() and not path.is_symlink()
                    and not any(part.is_symlink() for part in path.relative_to(folder).parents
                                if part != Path(".")))
             if folder.exists() else [])
    return ([path for path in files if path.suffix.lower() in SCREENSHOT_SUFFIXES],
            [path for path in files if path.suffix.lower() in CLIP_SUFFIXES])


def promote(name: str, size: str | None, risky: str | None, new_name: str | None = None,
            now: datetime | None = None, deploy: bool = False) -> list[str]:
    """Turn a shown prototype into a real task: a brief whose spec is the
    prototype's screenshots, ready for `relay start` on a fresh branch."""
    record = read_record(name)
    if record.get("status") == PROMOTED:
        raise ProtoError(f"{name} was already promoted to {record.get('promoted_to')}")
    if record.get("status") != SHOWN:
        raise ProtoError(f"{name} has not been shown yet: run {settings._CLI_CMD} proto done {name} first")
    if size not in ("feature", "project"):
        raise ProtoError("say how big the real build is with --size feature or --size project")
    if risky not in ("yes", "no"):
        raise ProtoError("say whether the real build is risky with --risky yes or --risky no "
                         "(yes for auth, input handling, money or production data)")
    target = _check_name(new_name or f"{name}-build")
    task = settings.TASKS_DIR / target
    if task.exists():
        raise ProtoError(f"{task} already exists: give the real task another name with --as")
    shots, clips = evidence_files(name)
    (task / SPEC).mkdir(parents=True)
    copied = []
    evidence = _folder(name) / EVIDENCE
    for path in shots + clips:
        # Subfolders keep their names, so two files called phone.png stay two.
        destination = task / SPEC / "-".join(path.relative_to(evidence).parts)
        shutil.copy2(path, destination, follow_symlinks=False)
        copied.append(destination)
    (task / "brief.md").write_text(_brief(record, target, size, risky, copied, now, deploy))
    record.update({"status": PROMOTED, "promoted_to": target, "promoted": _iso(_now(now))})
    _write_record(name, record)
    return [f"Prototype {name} promoted to the task {target}.",
            f"  Brief: {task / 'brief.md'} (size: {size}; risky: {risky}; "
            f"deploy: {'yes' if deploy else 'no'})",
            f"  Spec: {len(copied)} files in {task / SPEC}/",
            f"The prototype's branch {record['branch']} is never merged. Build again from the "
            "default branch:",
            f"  git -C {record['repo_path']} worktree add -b feature/{target} "
            f"{WORKTREES_ROOT / (Path(record['repo_path']).name + '-' + target)}",
            f"  then, from that worktree: {settings._CLI_CMD} start {target} --repo {record['repo']}"]


def _brief(record: dict, target: str, size: str, risky: str, spec: list[Path],
           now: datetime | None, deploy: bool) -> str:
    pictures = "\n".join(f"- `{SPEC}/{path.name}`" for path in spec)
    return (f"---\nsize: {size}\nrisky: {risky}\ndeploy: {'yes' if deploy else 'no'}\n---\n"
            f"# Task Brief: {target}\n"
            f"**Promoted from the prototype:** {record['name']} "
            f"(branch `{record['branch']}`, shown {record.get('finished')})\n\n"
            f"## Goal\n{record['idea']}\n\n"
            f"## The spec is the prototype\n"
            f"The owner saw these and said build it. The real build matches what they show; "
            f"where the prototype cut a corner (demo data, a flag, a missing error case), the "
            f"real build does it properly.\n\n{pictures}\n\n"
            f"## Constraints\n"
            f"- The prototype's code is never merged. Read it for ideas only; build again on a "
            f"fresh branch from the default branch.\n\n"
            f"## Acceptance Criteria\n"
            f"- [ ] The Designer writes these from the spec above.\n")


def list_lines() -> list[str]:
    """Every prototype, newest first, one line each."""
    rows = []
    if settings.TASKS_DIR.exists():
        for path in settings.TASKS_DIR.glob(f"*/{RECORD}"):
            try:
                record = read_record(path.parent.name)
            except ProtoError:
                continue
            rows.append(record)
    if not rows:
        return ["No prototypes."]
    rows.sort(key=lambda record: record.get("started", ""), reverse=True)
    return [f"{record['name']}  {record.get('status')}  {record.get('repo')}  started {record.get('started')}"
            + (f"  -> {record['promoted_to']}" if record.get("promoted_to") else "")
            for record in rows]
