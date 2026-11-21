"""
The one-time move from task files to the SQLite ledger, and the recovery
import behind `relay recover --from-projections`.

Reads run.yaml and the numbered reports in a task folder, reconciles them
against the state-machine enums the ledger enforces, and imports a task
into the ledger in one transaction. A task whose files disagree is imported
only with an explicit, audited MigrationDecision.

The ledger (db.py) knows nothing of this module. This module reads every
ledger name through `db.` at call time, never by value, so a test that
replaces db's enums is seen here too. It imports only db and yaml from
outside the standard library.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover - settings.py already fails fast on this
    _yaml = None

import db


class MigrationExcluded(ValueError):
    """Raised when import_task() is called for a task an explicit
    MigrationDecision marks exclude=True. A distinct exception from
    InvalidTransition -- exclusion is a deliberate human decision, not a
    data-integrity failure."""


# frozen=True: a decision can't be quietly changed after it is validated.
@dataclasses.dataclass(frozen=True)
class StepVerdictOverride:
    """An explicit, human-authorized correction for ONE step's verdict.

    Deliberately narrow: names the exact step, the exact historical value
    expected to be found there, and the exact canonical value it becomes.
    It is not "set this step's verdict to X" -- it is "if this step's
    verdict is still exactly Y, treat it as X" -- so a decision authored
    against one artifact can never silently apply to a different one that
    happens to share a step number, and a decision that has stopped matching
    what's on disk fails loudly instead of applying blind.
    """
    step: int
    expected_verdict: str
    canonical_verdict: str
    note: str = ""

    def __post_init__(self):
        if not self.note:
            raise ValueError(f"StepVerdictOverride for step {self.step} must include an audit note")


@dataclasses.dataclass(frozen=True)
class MigrationDecision:
    """An explicit, human-authorized mapping for one task's migration.

    This is the *entire* migration-override mechanism -- narrowly typed on
    purpose, not a generic dict of arbitrary field overrides. At the task
    level it can only ever map a legacy value to a canonical status, a
    canonical current_phase, or "exclude this task from migration
    entirely." At the step level (`step_overrides`) it can only correct
    one named step's verdict, and only when the historical value found
    there still matches exactly what the decision expects. Nothing else
    is expressible. It is never constructed, read, or reachable from
    the ledger's live write path (db.create_task, update_task,
    record_dispatch, record_verdict), or any cmd_* function in commands.py -- ordinary
    Relay operation has no code path that could produce or accept one, so
    it cannot be activated by accident.

    Every decision (and every step override within it) requires a `note`
    -- there is no silent, unexplained override. import_task() validates
    status/current_phase/verdicts against the same canonical enums as
    everything else before writing them; a MigrationDecision can point at
    a wrong-but-plausible-looking value and it will still be rejected at
    write time, exactly like a raw legacy value would be. The decision
    doesn't bypass validation -- it's the only way to supply a
    *replacement* value for validation to check.
    """
    task_id: str
    status: str | None = None
    current_phase: str | None = None
    exclude: bool = False
    note: str = ""
    step_overrides: tuple["StepVerdictOverride", ...] = ()
    # Step numbers a human has confirmed were never delivered (a gap in the
    # artifact trail). Recorded as events; never filled in or fabricated.
    missing_steps: tuple[int, ...] = ()
    # Files in the agent-artifact numbering range that are not agent phase
    # output (prose plans, no-frontmatter docs, a second file reusing a step
    # number). Imported as historical notes, never as pipeline steps.
    note_files: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.note:
            raise ValueError(f"MigrationDecision for '{self.task_id}' must include an audit note")
        if self.exclude and (self.status is not None or self.current_phase is not None
                              or self.step_overrides or self.missing_steps or self.note_files):
            raise ValueError(
                f"MigrationDecision for '{self.task_id}' cannot both exclude the task "
                f"and override its fields -- exclusion means nothing else is imported"
            )
        for field_name, values in (("missing_steps", self.missing_steps),
                                   ("note_files", self.note_files)):
            if len(values) != len(set(values)):
                raise ValueError(
                    f"MigrationDecision for '{self.task_id}' lists the same {field_name} entry "
                    f"more than once"
                )
        steps_seen = [so.step for so in self.step_overrides]
        if len(steps_seen) != len(set(steps_seen)):
            raise ValueError(
                f"MigrationDecision for '{self.task_id}' has more than one step_override "
                f"for the same step -- each step gets exactly one explicit decision"
            )

    def step_override_for(self, step: int) -> "StepVerdictOverride | None":
        for so in self.step_overrides:
            if so.step == step:
                return so
        return None


# ---------------------------------------------------------------------------
# Migration: dry-run + conflict detection against the real file corpus.
# No writes to the source files or to `conn` happen in this section --
# dry_run_migration() is read-only by construction.
# ---------------------------------------------------------------------------

def read_file_state(task_dir: Path) -> dict:
    """Read a task's on-disk state exactly as the files backend in run_state.py does:
    run.yaml plus every numbered artifact's frontmatter. Read-only."""
    run_yaml_path = task_dir / "run.yaml"
    run_yaml = {}
    if run_yaml_path.exists():
        run_yaml = _yaml.safe_load(run_yaml_path.read_text()) or {}

    artifacts = []
    for f in sorted(task_dir.iterdir()) if task_dir.exists() else []:
        if f.suffix != ".md" or not f.name[0:2].isdigit():
            continue
        step_num = int(f.name[0:2])
        if step_num >= 90:
            continue  # human artifacts, not agent phase output
        frontmatter_error = None
        try:
            fm = db._parse_frontmatter_text(f.read_text())
        except ValueError as error:
            fm, frontmatter_error = {}, str(error)
        artifacts.append({
            "frontmatter_error": frontmatter_error,
            "step": step_num,
            "filename": f.name,
            "phase": fm.get("phase"),
            "verdict": fm.get("verdict"),
            "date": fm.get("date"),  # day-granularity fallback timestamp source
            "content_hash": db._hash_file(f),
        })
    artifacts.sort(key=lambda a: a["step"])
    return {"run_yaml": run_yaml, "artifacts": artifacts}


def reconcile_task(task_id: str, file_state: dict, *, check_step_sequence: bool = True,
                   check_phase_agreement: bool = True) -> dict:
    """Compare run.yaml against the artifact trail for one task, exactly as
    check_state_consistency() does, plus enum validation against the
    configured VALID_STATUSES/VALID_PHASES/VALID_VERDICTS.

    Never picks a winner. Returns {task_id, ok, discrepancies: [...]} --
    ok is False if anything disagrees or violates an enum; the caller
    decides what to do, this function only surfaces the finding.
    """
    db._require_configured()
    discrepancies = []
    run_yaml = file_state["run_yaml"]
    artifacts = file_state["artifacts"]

    status = run_yaml.get("status")
    if status is not None and status not in db._valid_statuses:
        discrepancies.append(f"run.yaml status '{status}' is not a valid status")

    ry_phase = run_yaml.get("current_phase")
    if ry_phase is not None and ry_phase not in db._valid_phases + ["done"]:
        discrepancies.append(f"run.yaml current_phase '{ry_phase}' is not a valid phase")

    if artifacts:
        # Every artifact is examined, not merely the latest one -- a bad
        # phase/verdict buried behind later, otherwise-clean steps (e.g.
        # web-sim-responsive-slice-1's step 3, behind steps 4-7) must be
        # visible in the classification, not just at import time. This is
        # the fix for the exact gap that let that task read as CLEAN.
        for art in artifacts:
            art_phase = art["phase"]
            if art.get("frontmatter_error"):
                discrepancies.append(
                    f"artifact ({art['filename']}) has unreadable frontmatter: "
                    f"{art['frontmatter_error']}"
                )
                continue
            if art_phase is None:
                discrepancies.append(
                    f"artifact ({art['filename']}) has no YAML frontmatter phase -- "
                    f"cannot tell which agent phase produced it"
                )
                continue
            if art_phase not in db._valid_phases:
                discrepancies.append(
                    f"artifact ({art['filename']}) phase '{art_phase}' is not a valid phase"
                )
                continue
            art_verdict = art["verdict"]
            if art_verdict and art_verdict not in db._valid_verdicts.get(art_phase, []):
                discrepancies.append(
                    f"artifact ({art['filename']}) verdict '{art_verdict}' "
                    f"is not valid for phase '{art_phase}'"
                )

        latest = artifacts[-1]
        art_phase = latest["phase"]
        if (check_phase_agreement and ry_phase and art_phase and ry_phase not in ("done",)
                and ry_phase != art_phase):
            discrepancies.append(
                f"run.yaml current_phase ('{ry_phase}') disagrees with latest artifact "
                f"phase ('{art_phase}', {latest['filename']}) -- the two sources of truth "
                f"this refactor exists to unify do not currently agree"
            )
        seen_steps = [a["step"] for a in artifacts]
        if not check_step_sequence:
            return {"task_id": task_id, "ok": not discrepancies, "discrepancies": discrepancies}
        duplicate_steps = sorted({step for step in seen_steps if seen_steps.count(step) > 1})
        for step in duplicate_steps:
            filenames = [a["filename"] for a in artifacts if a["step"] == step]
            discrepancies.append(
                f"step number {step} is used by more than one artifact: {filenames}"
            )
        unique_steps = sorted(set(seen_steps))
        expected_steps = list(range(unique_steps[0], unique_steps[0] + len(unique_steps)))
        if unique_steps != expected_steps:
            discrepancies.append(f"artifact step numbers are not a contiguous ascending sequence: {unique_steps}")
    elif status == "in_progress":
        discrepancies.append("status is in_progress but no artifacts exist")

    return {"task_id": task_id, "ok": not discrepancies, "discrepancies": discrepancies}


def dry_run_migration(tasks_dir: Path) -> dict:
    """Scan every task directory under tasks_dir and reconcile it. Purely
    read-only: no database writes, no file writes. Returns a summary fit
    for a human to review before any real import happens.

    A directory with neither run.yaml nor numbered agent artifacts is a
    brief-only task that never started, and is skipped. A directory with
    agent artifacts but no run.yaml is real historical work: it is always
    reported, and always flagged, so it can never be silently left out of
    a migration or imported as clean.
    """
    if not tasks_dir.exists():
        return {"total": 0, "with_run_yaml": 0, "without_run_yaml": 0,
                "clean": 0, "flagged": 0, "results": []}

    results = []
    with_run_yaml = 0
    without_run_yaml = 0
    for entry in sorted(tasks_dir.iterdir()):
        if not entry.is_dir():
            continue
        file_state = read_file_state(entry)
        has_run_yaml = (entry / "run.yaml").exists()
        if has_run_yaml:
            with_run_yaml += 1
            results.append(reconcile_task(entry.name, file_state))
            continue
        if not file_state["artifacts"]:
            continue  # brief-only task, never started -- nothing to migrate
        without_run_yaml += 1
        result = reconcile_task(entry.name, file_state)
        artifact_steps = [a["step"] for a in file_state["artifacts"]]
        result["discrepancies"].insert(
            0,
            f"no run.yaml, but {len(artifact_steps)} agent artifact(s) exist "
            f"(steps {artifact_steps}) -- real work with no task state to import",
        )
        result["ok"] = False
        results.append(result)

    flagged = [r for r in results if not r["ok"]]
    return {
        "total": len(results),
        "with_run_yaml": with_run_yaml,
        "without_run_yaml": without_run_yaml,
        "clean": len(results) - len(flagged),
        "flagged": len(flagged),
        "results": results,
    }


_DECISION_KEYS = frozenset({"task", "status", "current_phase", "exclude", "note",
                            "step_overrides", "missing_steps", "note_files"})
_STEP_OVERRIDE_KEYS = frozenset({"step", "expected", "canonical", "note"})


def load_decisions(path: Path) -> dict[str, MigrationDecision]:
    """Read a YAML decisions file into MigrationDecisions, keyed by task id.

    Strict on purpose: an unknown key, a wrong type, a duplicate task, or a
    missing note is an error naming the field, never coerced or ignored. A
    typo in a decisions file must not quietly become "no decision", and a
    quoted "false" must not become an exclusion.

    Format:
        decisions:
          - task: some-task
            status: completed
            current_phase: done
            note: why this mapping is right
            missing_steps: [4]
            note_files: [05-prod-promotion-plan.md]
            step_overrides:
              - {step: 1, expected: approved, canonical: ready-for-builder, note: why}
    """
    if _yaml is None:
        raise RuntimeError("PyYAML is required to read a decisions file")
    try:
        document = _yaml.safe_load(path.read_text())
    except _yaml.YAMLError as error:
        raise ValueError(f"{path}: not valid YAML ({error.__class__.__name__})") from error
    if not isinstance(document, dict) or not isinstance(document.get("decisions"), list):
        raise ValueError(f"{path}: expected a mapping with a top-level 'decisions:' list")

    decisions: dict[str, MigrationDecision] = {}
    # Counted from 1 for the error messages ('decision #3').
    for position, entry in enumerate(document["decisions"], start=1):
        where = f"{path}: decision #{position}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: expected a mapping, got {type(entry).__name__}")
        unknown_keys = set(entry) - _DECISION_KEYS
        if unknown_keys:
            raise ValueError(f"{where}: unknown keys {sorted(unknown_keys)}")
        task_id = _required_text(entry, "task", where)
        where = f"{path}: decision for '{task_id}'"
        if task_id in decisions:
            raise ValueError(f"{path}: more than one decision for '{task_id}'")

        decisions[task_id] = MigrationDecision(
            task_id=task_id,
            status=_optional_text(entry, "status", where),
            current_phase=_optional_text(entry, "current_phase", where),
            exclude=_optional_bool(entry, "exclude", where),
            note=_required_text(entry, "note", where),
            step_overrides=tuple(_load_step_override(override, where)
                                 for override in _optional_list(entry, "step_overrides", where)),
            missing_steps=tuple(_list_of(entry, "missing_steps", int, where)),
            note_files=tuple(_list_of(entry, "note_files", str, where)),
        )
    return decisions


def _load_step_override(override, where: str) -> StepVerdictOverride:
    if not isinstance(override, dict):
        raise ValueError(f"{where}: each step override must be a mapping")
    unknown_keys = set(override) - _STEP_OVERRIDE_KEYS
    if unknown_keys:
        raise ValueError(f"{where}: unknown keys {sorted(unknown_keys)} in a step override")
    step = override.get("step")
    if not isinstance(step, int) or isinstance(step, bool):
        raise ValueError(f"{where}: step override 'step' must be an integer")
    return StepVerdictOverride(
        step=step,
        expected_verdict=_required_text(override, "expected", where),
        canonical_verdict=_required_text(override, "canonical", where),
        note=_required_text(override, "note", where),
    )


def _required_text(mapping: dict, key: str, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: '{key}' must be a non-empty string")
    return value


def _optional_text(mapping: dict, key: str, where: str) -> str | None:
    if mapping.get(key) is None:
        return None
    return _required_text(mapping, key, where)


def _optional_bool(mapping: dict, key: str, where: str) -> bool:
    value = mapping.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"{where}: '{key}' must be true or false (unquoted), got {value!r}")
    return value


def _optional_list(mapping: dict, key: str, where: str) -> list:
    value = mapping.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{where}: '{key}' must be a list, got {type(value).__name__}")
    return value


# Python: isinstance(x, SomeType) checks a value's type at runtime. bool is a
#   subclass of int in Python, so True would pass as an int; that's why
#   the check below rejects bools explicitly.
def _list_of(mapping: dict, key: str, item_type: type, where: str) -> list:
    items = _optional_list(mapping, key, where)
    for item in items:
        if not isinstance(item, item_type) or isinstance(item, bool):
            raise ValueError(f"{where}: every item in '{key}' must be a {item_type.__name__}, "
                             f"got {item!r}")
    return items


def rehearse_migration(tasks_dir: Path, decisions: dict[str, MigrationDecision]) -> dict:
    """Prove what a real import would do, without touching the real database
    or any file: every task is imported into a throwaway in-memory SQLite
    database, with its decision if it has one.

    Every scanned task lands in exactly one bucket:
      clean      -- no discrepancies, no decision needed
      resolved   -- had discrepancies (or a decision) and imports cleanly with it
      excluded   -- a decision deliberately leaves it out
      unresolved -- would fail to import; the reason says why
    Decisions naming a task the scan never saw are reported, not ignored.
    """
    summary = dry_run_migration(tasks_dir)
    buckets: dict[str, list] = {"clean": [], "resolved": [], "excluded": [],
                                "unresolved": [], "unknown_decisions": []}

    # Python: ':memory:' is a database that lives only in RAM and vanishes when
    #   closed, which makes it a perfect throwaway rehearsal space.
    scratch = sqlite3.connect(":memory:")
    scratch.row_factory = sqlite3.Row
    scratch.execute("PRAGMA foreign_keys = ON")
    db.init_schema(scratch)

    scanned_task_ids = set()
    try:
        _rehearse_each_task(summary, decisions, tasks_dir, scratch, buckets, scanned_task_ids)
    finally:
        scratch.close()
    buckets["unknown_decisions"] = sorted(set(decisions) - scanned_task_ids)
    return {"scanned": summary["total"], **buckets}


def _rehearse_each_task(summary: dict, decisions: dict, tasks_dir: Path,
                        scratch: sqlite3.Connection, buckets: dict, scanned_task_ids: set) -> None:
    for result in summary["results"]:
        task_id = result["task_id"]
        scanned_task_ids.add(task_id)
        decision = decisions.get(task_id)
        if decision is not None and decision.exclude:
            buckets["excluded"].append(task_id)
            continue
        if decision is None and not result["ok"]:
            buckets["unresolved"].append((task_id, "flagged and no decision: "
                                          + "; ".join(result["discrepancies"])))
            continue
        try:
            import_task(scratch, task_id, tasks_dir / task_id, decision=decision)
        except (ValueError, sqlite3.Error) as error:
            buckets["unresolved"].append((task_id, str(error)))
            continue
        buckets["resolved" if decision is not None else "clean"].append(task_id)


def import_task(conn: sqlite3.Connection, task_id: str, task_dir: Path,
                 *, decision: "MigrationDecision | None" = None, force: bool = False) -> dict:
    """Actually import one task's file state into SQLite. Non-destructive:
    source files are never modified or deleted.

    Canonical-state guarantee: the status and current_phase actually
    written are ALWAYS validated against the same VALID_STATUSES/
    VALID_PHASES this module enforces everywhere else, with no exception.
    A legacy value that isn't already canonical can only become canonical
    by way of an explicit, audited `decision` (see MigrationDecision) --
    never by force=True alone. force=True governs only proceeding past
    OTHER discrepancies reconcile_task() may find (step-sequence gaps,
    etc.) that a `decision` doesn't address; it can never cause a
    non-canonical status/phase/verdict to be written.

    decision.exclude=True raises MigrationExcluded unconditionally --
    exclusion is absolute, not a discrepancy to force past.

    Refuses (raises InvalidTransition) if reconcile_task() found
    discrepancies that neither `decision` nor `force` resolve.

    Not wired into any CLI command in Phase 1 -- this is migration
    machinery, exercised by tests against fixture corpora. Running it
    against the live ~/Projects/.relay/tasks/ corpus is a later,
    separately-gated phase.
    """
    if decision is not None and decision.task_id != task_id:
        raise ValueError(
            f"decision.task_id ('{decision.task_id}') does not match task_id ('{task_id}')"
        )
    if decision is not None and decision.exclude:
        raise MigrationExcluded(f"'{task_id}' is explicitly excluded from migration: {decision.note}")

    if db.task_exists(conn, task_id):
        raise ValueError(f"task '{task_id}' already exists in the database")

    file_state = read_file_state(task_dir)
    result = reconcile_task(task_id, file_state)
    if not result["ok"] and not force:
        if decision is None:
            raise db.InvalidTransition(
                f"refusing to import '{task_id}': discrepancies found: {result['discrepancies']}"
            )
        unaddressed = _discrepancies_left_after_decision(task_id, file_state, decision)
        if unaddressed:
            raise db.InvalidTransition(
                f"refusing to import '{task_id}': the decision does not address: {unaddressed}"
            )

    run_yaml = file_state["run_yaml"]
    has_run_yaml = (task_dir / "run.yaml").exists()
    if not has_run_yaml and (decision is None or not decision.status or not decision.current_phase):
        raise db.InvalidTransition(
            f"cannot import '{task_id}': it has no run.yaml, so its status and current_phase "
            f"must come from an explicit migration decision"
        )

    step_artifacts, note_artifacts = _split_note_files(task_id, file_state["artifacts"], decision)
    _require_importable_steps(task_id, step_artifacts, decision, run_yaml=file_state["run_yaml"],
                              force=force)
    _require_overrides_apply(task_id, step_artifacts, decision)
    task_created, task_updated = _task_timestamps(task_id, run_yaml, file_state["artifacts"])

    ry_status = run_yaml.get("status", "in_progress")
    ry_phase = run_yaml.get("current_phase")

    final_status = decision.status if (decision is not None and decision.status) else ry_status
    final_phase = decision.current_phase if (decision is not None and decision.current_phase) else ry_phase

    # Whatever is about to be written -- the legacy value or a decision's
    # replacement -- must already be canonical. There is no fallback
    # substitution (such as a default phase): an uncorrected non-canonical
    # value fails loudly, at import time, with a message naming exactly
    # what's missing, instead of writing bad data that only crashes the
    # next `relay next` (a status such as 'complete', say).
    if final_status not in db._valid_statuses:
        raise db.InvalidTransition(
            f"cannot import '{task_id}': status '{final_status}' is not canonical "
            f"(legacy value '{ry_status}' has no migration decision mapping it to one of "
            f"{db._valid_statuses})"
        )
    if final_phase not in db._valid_phases and final_phase != "done":
        raise db.InvalidTransition(
            f"cannot import '{task_id}': current_phase '{final_phase}' is not canonical "
            f"(legacy value '{ry_phase}' has no migration decision mapping it to one of "
            f"{db._valid_phases + ['done']})"
        )

    # Step verdicts get the same treatment, as the live path
    # (db.record_verdict) gives them. A task with an invalid step verdict and
    # no decision addressing it fails here, before anything is written,
    # rather than corrupting canonical state -- and a decision only ever applies when
    # its expected_verdict matches exactly what's actually on disk, so a
    # decision that's gone stale (the file changed since it was authored)
    # fails loudly instead of silently mis-applying.
    final_verdicts: dict[int, str | None] = {}
    step_overrides_applied: list[tuple[int, str, str]] = []  # (step, legacy, canonical)
    for art in step_artifacts:
        v = art.get("verdict")
        p = art["phase"]
        step = art["step"]
        if not v or v in db._valid_verdicts.get(p, []):
            final_verdicts[step] = v
            continue

        override = decision.step_override_for(step) if decision is not None else None
        if override is None:
            raise db.InvalidTransition(
                f"cannot import '{task_id}': step {step} ({art['filename']}) verdict "
                f"'{v}' is not valid for phase '{p}'"
            )
        if override.expected_verdict != v:
            raise db.InvalidTransition(
                f"cannot import '{task_id}': step {step} override expected verdict "
                f"'{override.expected_verdict}' but found '{v}' in {art['filename']} -- "
                f"refusing to apply a decision against data it doesn't match"
            )
        if override.canonical_verdict not in db._valid_verdicts.get(p, []):
            raise db.InvalidTransition(
                f"cannot import '{task_id}': step {step} override's canonical_verdict "
                f"'{override.canonical_verdict}' is not valid for phase '{p}'"
            )
        final_verdicts[step] = override.canonical_verdict
        step_overrides_applied.append((step, v, override.canonical_verdict))

    # Per-step timestamps: prefer run.yaml's own history entries (they carry
    # a real, fine-grained timestamp per phase transition) over the task's
    # single overall `updated` value, which the pre-fix code stamped onto
    # every step, destroying chronology. Where a step's real timestamp is
    # genuinely unrecoverable (no matching history entry, no artifact `date`
    # frontmatter either), created_at is left NULL -- not fabricated.
    step_timestamps: dict[int, str] = {}
    for entry in run_yaml.get("history", []) or []:
        if not isinstance(entry, dict):
            continue
        step = entry.get("step")
        ts = _timestamp_text(entry.get("timestamp"))
        if step is not None and ts:
            step_timestamps[step] = ts  # last entry for a duplicated step wins

    # Task-row creation and every other write this import performs (the
    # UPDATE, steps, migration_override events, historical events,
    # nonstandard-field event) happen in ONE transaction via
    # _insert_task_row(), not by calling the self-committing create_task().
    # A task row committed on its own would be left orphaned by a failure
    # later in the import, with no steps or events, and task_exists() would
    # then refuse every retry. A single transaction means
    # any failure below rolls back the task-row insert too, so a retry of
    # import_task() after a failure is always possible without manual
    # cleanup.
    with conn:
        db._insert_task_row(conn, task_id, repo=run_yaml.get("repo"),
                          deploy_intent=bool(run_yaml.get("deploy_intent", False)),
                          current_phase=final_phase)
        conn.execute(
            """UPDATE tasks SET created=?, updated=?, status=?, current_phase=?,
                   worktree_path=?, branch=?, base=?, next_step=? WHERE task_id=?""",
            (task_created, task_updated,
             final_status, final_phase,
             run_yaml.get("worktree_path"), run_yaml.get("branch"), run_yaml.get("base"),
             run_yaml.get("next_step") or _next_step_from_trail(step_artifacts, decision), task_id),
        )
        for art in step_artifacts:
            created_at = step_timestamps.get(art["step"])
            if created_at is None and art.get("date"):
                created_at = f"{art['date']}T00:00:00Z"  # day-only, real, lower fidelity
            conn.execute(
                """INSERT INTO steps
                       (task_id, step, phase, verdict, artifact_filename, content_hash, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (task_id, art["step"], art["phase"], final_verdicts.get(art["step"]),
                 art["filename"], art["content_hash"], created_at),
            )

        if not has_run_yaml:
            conn.execute(
                "INSERT INTO events (task_id, event_type, payload, timestamp) VALUES (?,?,?,?)",
                (task_id, db.EVENT_HISTORICAL_NOTE,
                 json.dumps({"source": "no run.yaml on disk; status and current_phase supplied "
                                       "by migration decision", "note": decision.note}),
                 db._now()),
            )
        for note_artifact in note_artifacts:
            conn.execute(
                "INSERT INTO events (task_id, event_type, step, payload, timestamp) VALUES (?,?,?,?,?)",
                (task_id, db.EVENT_HISTORICAL_NOTE, note_artifact["step"],
                 json.dumps({"note_file": note_artifact["filename"],
                             "content_hash": note_artifact["content_hash"],
                             "note": decision.note}),
                 db._now()),
            )
        for missing_step in (decision.missing_steps if decision is not None else ()):
            conn.execute(
                "INSERT INTO events (task_id, event_type, step, payload, timestamp) VALUES (?,?,?,?,?)",
                (task_id, db.EVENT_STEP_NEVER_DELIVERED, missing_step,
                 json.dumps({"note": decision.note}), db._now()),
            )

        for step, legacy_verdict, canonical_verdict in step_overrides_applied:
            override = decision.step_override_for(step)
            conn.execute(
                "INSERT INTO events (task_id, event_type, step, payload, timestamp) VALUES (?,?,?,?,?)",
                (task_id, db.EVENT_MIGRATION_OVERRIDE, step,
                 json.dumps({"field": "verdict", "legacy": legacy_verdict,
                             "canonical": canonical_verdict, "note": override.note}),
                 db._now()),
            )

        # Audit trail: record exactly what was overridden and why, distinct
        # from ordinary state_saved/task_created events. Never fires when
        # decision is None or doesn't touch a given field.
        if decision is not None:
            if decision.status:
                conn.execute(
                    "INSERT INTO events (task_id, event_type, payload, timestamp) VALUES (?,?,?,?)",
                    (task_id, db.EVENT_MIGRATION_OVERRIDE,
                     json.dumps({"field": "status", "legacy": ry_status,
                                 "canonical": decision.status, "note": decision.note}),
                     db._now()),
                )
            if decision.current_phase:
                conn.execute(
                    "INSERT INTO events (task_id, event_type, payload, timestamp) VALUES (?,?,?,?)",
                    (task_id, db.EVENT_MIGRATION_OVERRIDE,
                     json.dumps({"field": "current_phase", "legacy": ry_phase,
                                 "canonical": decision.current_phase, "note": decision.note}),
                     db._now()),
                )

        _import_historical_events(conn, task_id, run_yaml, step_artifacts)
        _import_nonstandard_fields(conn, task_id, run_yaml)

    return result


# Python: dict(x) makes a shallow copy, so changing the copy below doesn't
#   change the caller's data.
def _discrepancies_left_after_decision(task_id: str, file_state: dict,
                                       decision: "MigrationDecision") -> list[str]:
    """Reconcile the task as it would look with the decision applied, so a
    decision only excuses the problems it actually addresses.

    Step gaps and duplicates are checked separately by
    _require_importable_steps(). A run.yaml phase that disagrees with the
    latest artifact counts as addressed only when the decision sets
    current_phase explicitly, which import_task() records as an audited
    migration_override event."""
    adjusted_run_yaml = dict(file_state["run_yaml"])
    if decision.status:
        adjusted_run_yaml["status"] = decision.status
    if decision.current_phase:
        adjusted_run_yaml["current_phase"] = decision.current_phase

    adjusted_artifacts = []
    for artifact in file_state["artifacts"]:
        if artifact["filename"] in decision.note_files:
            continue
        adjusted = dict(artifact)
        override = decision.step_override_for(artifact["step"])
        if override is not None:
            if override.expected_verdict != artifact["verdict"]:
                raise db.InvalidTransition(
                    f"cannot import '{task_id}': step {artifact['step']} override expected verdict "
                    f"'{override.expected_verdict}' but found '{artifact['verdict']}' in "
                    f"{artifact['filename']} -- refusing to apply a decision against data it "
                    f"doesn't match"
                )
            adjusted["verdict"] = override.canonical_verdict
        adjusted_artifacts.append(adjusted)

    return reconcile_task(
        task_id, {"run_yaml": adjusted_run_yaml, "artifacts": adjusted_artifacts},
        check_step_sequence=False,
        check_phase_agreement=decision.current_phase is None,
    )["discrepancies"]


def _split_note_files(task_id: str, artifacts: list[dict],
                      decision: "MigrationDecision | None") -> tuple[list[dict], list[dict]]:
    """Separate the artifacts a decision names as notes from the real
    pipeline steps. A named file that isn't on disk fails loudly: the
    decision was written against different data."""
    note_filenames = set(decision.note_files) if decision is not None else set()
    present = {artifact["filename"] for artifact in artifacts}
    unknown = sorted(note_filenames - present)
    if unknown:
        raise db.InvalidTransition(
            f"cannot import '{task_id}': note_files {unknown} are not agent artifacts on disk"
        )
    step_artifacts = [a for a in artifacts if a["filename"] not in note_filenames]
    note_artifacts = [a for a in artifacts if a["filename"] in note_filenames]
    return step_artifacts, note_artifacts


def _require_importable_steps(task_id: str, step_artifacts: list[dict],
                              decision: "MigrationDecision | None", *, run_yaml: dict,
                              force: bool) -> None:
    """Refuse anything that would make import invent or silently drop
    history: a step with no phase, two files sharing a step number, a
    declared-missing step that actually exists or has no evidence, or an
    unexplained gap. force skips only the gap check."""
    for artifact in step_artifacts:
        if artifact["phase"] is None:
            raise db.InvalidTransition(
                f"cannot import '{task_id}': {artifact['filename']} has no frontmatter phase; "
                f"list it in the decision's note_files to import it as a note"
            )

    step_numbers = [a["step"] for a in step_artifacts]
    duplicates = sorted({n for n in step_numbers if step_numbers.count(n) > 1})
    if duplicates:
        raise db.InvalidTransition(
            f"cannot import '{task_id}': step numbers {duplicates} are used by more than one "
            f"artifact; list the extra files in the decision's note_files"
        )

    missing_steps = set(decision.missing_steps) if decision is not None else set()
    contradicted = sorted(missing_steps & set(step_numbers))
    if contradicted:
        raise db.InvalidTransition(
            f"cannot import '{task_id}': missing_steps {contradicted} actually exist on disk"
        )
    if missing_steps and step_numbers:
        before_first = sorted(n for n in missing_steps if n < min(step_numbers))
        if before_first:
            raise db.InvalidTransition(
                f"cannot import '{task_id}': missing_steps {before_first} come before the "
                f"first artifact (step {min(step_numbers)})"
            )
        history_steps = {entry.get("step") for entry in run_yaml.get("history", []) or []
                         if isinstance(entry, dict)}
        unevidenced = sorted(n for n in missing_steps
                             if n > max(step_numbers) and n not in history_steps)
        if unevidenced:
            raise db.InvalidTransition(
                f"cannot import '{task_id}': missing_steps {unevidenced} come after the last "
                f"artifact and run.yaml history has no record of them"
            )

    if force or not step_numbers:
        return
    accounted_for = sorted(set(step_numbers) | missing_steps)
    expected = list(range(accounted_for[0], accounted_for[-1] + 1))
    if accounted_for != expected:
        gaps = sorted(set(expected) - set(accounted_for))
        raise db.InvalidTransition(
            f"cannot import '{task_id}': steps {gaps} are missing; declare them in the "
            f"decision's missing_steps if they were never delivered"
        )


def _next_step_from_trail(step_artifacts: list[dict], decision: "MigrationDecision | None") -> int:
    """One past the highest step on disk or declared missing."""
    numbers = [artifact["step"] for artifact in step_artifacts]
    numbers += list(decision.missing_steps) if decision is not None else []
    return max(numbers, default=0) + 1


def _require_overrides_apply(task_id: str, step_artifacts: list[dict],
                             decision: "MigrationDecision | None") -> None:
    """Every step override must hit a real step whose current verdict is the
    one it expects and is not already canonical. A stale or pointless
    override means the decision was written against different data."""
    if decision is None:
        return
    artifacts_by_step = {artifact["step"]: artifact for artifact in step_artifacts}
    for override in decision.step_overrides:
        artifact = artifacts_by_step.get(override.step)
        if artifact is None:
            raise db.InvalidTransition(
                f"cannot import '{task_id}': step override for step {override.step} "
                f"matches no step on disk"
            )
        if artifact["verdict"] != override.expected_verdict:
            raise db.InvalidTransition(
                f"cannot import '{task_id}': step {override.step} override expected verdict "
                f"'{override.expected_verdict}' but found '{artifact['verdict']}' in "
                f"{artifact['filename']} -- refusing to apply a decision against data it "
                f"doesn't match"
            )
        if artifact["verdict"] in db._valid_verdicts.get(artifact["phase"], []):
            raise db.InvalidTransition(
                f"cannot import '{task_id}': step {override.step} verdict "
                f"'{artifact['verdict']}' is already canonical; the override is stale"
            )


def _task_timestamps(task_id: str, run_yaml: dict, artifacts: list[dict]) -> tuple[str, str]:
    """created/updated for the task row: run.yaml's own values when present,
    otherwise the earliest and latest real artifact dates. Never invented:
    no dates anywhere means the import is refused."""
    if run_yaml.get("created") and run_yaml.get("updated"):
        return _timestamp_text(run_yaml["created"]), _timestamp_text(run_yaml["updated"])
    dates = sorted(str(a["date"]) for a in artifacts if a.get("date"))
    if not dates:
        raise db.InvalidTransition(
            f"cannot import '{task_id}': no run.yaml timestamps and no artifact dates to "
            f"derive created/updated from"
        )
    return (_timestamp_text(run_yaml.get("created")) or f"{dates[0]}T00:00:00Z",
            _timestamp_text(run_yaml.get("updated")) or f"{dates[-1]}T00:00:00Z")


def _timestamp_text(value) -> str | None:
    """YAML turns an unquoted timestamp into a datetime (or a date); store
    it in the same ISO-8601 'Z' form _now() writes, so every timestamp
    compares and sorts the same way."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return f"{value.isoformat()}T00:00:00Z"
    return str(value)


# Python: json.dumps(..., default=fn) calls fn for any value it can't serialize
#   on its own (like dates). Raising TypeError there keeps the normal error.
def _legacy_json_value(value):
    """json.dumps fallback for values YAML parsed out of legacy files:
    dates and datetimes become timestamp text instead of crashing."""
    if isinstance(value, (datetime, date)):
        return _timestamp_text(value)
    raise TypeError(f"cannot store {type(value).__name__} from a legacy file as JSON")


# run.yaml keys import_task()/create_task() actually read into canonical
# columns. Anything else is non-standard, task-specific bookkeeping a human
# or tool added directly to the file (calibration results, scope notes,
# parked-reason text, base-branch provenance, etc.) -- real information,
# never canonical Relay state.
_CANONICAL_RUN_YAML_KEYS = frozenset({
    "task_id", "created", "updated", "status", "current_phase", "next_step",
    "schema_version", "repo", "worktree_path", "branch", "base",
    "deploy_intent", "retries", "history",
})


def _import_nonstandard_fields(conn: sqlite3.Connection, task_id: str, run_yaml: dict) -> None:
    """Preserve any run.yaml top-level fields outside the canonical schema
    (a freeform `results:` section, say) as one historical-metadata event. Never touches tasks/steps.
    No new schema field -- events.payload already exists for exactly this.
    """
    extra = {k: v for k, v in run_yaml.items() if k not in _CANONICAL_RUN_YAML_KEYS}
    if not extra:
        return
    conn.execute(
        "INSERT INTO events (task_id, event_type, payload, timestamp) VALUES (?,?,?,?)",
        (task_id, db.EVENT_HISTORICAL_NOTE, json.dumps(extra, default=_legacy_json_value), db._now()),
    )


def _import_historical_events(conn: sqlite3.Connection, task_id: str, run_yaml: dict,
                               artifacts: list[dict]) -> None:
    """Preserve run.yaml's history[] entries as pure historical record --
    events rows, never canonical tasks/steps columns.

    This is specifically for entries that use non-canonical phase
    vocabulary Relay itself never had (human_gate, builder_fix,
    builder_commit, builder_harden, committed, closeout -- observed in the
    real corpus) or that simply have no corresponding artifact file. It
    also captures real data those entries carry that canonical state has
    nowhere else to hold, most notably git commit hashes on `committed`/
    `builder_commit` entries. Relay itself never reads this table's
    `payload` to make a routing decision -- it exists purely so "what
    happened to this task" stays a query instead of a forensic file read,
    per the persistence-v2 brief's own Problem Statement. Historical
    vocabulary never touches VALID_PHASES, never becomes a `steps.phase`
    or `tasks.current_phase` value.
    """
    imported_steps = {a["step"] for a in artifacts}
    for entry in run_yaml.get("history", []) or []:
        if not isinstance(entry, dict):
            continue
        step = entry.get("step")
        phase = entry.get("phase")
        # An entry backed by a real artifact is already faithfully
        # represented in `steps` -- only record the ones that aren't,
        # so this doesn't duplicate the normal step trail.
        if step is not None and step in imported_steps and phase in db._valid_phases:
            continue
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, db.EVENT_HISTORICAL_NOTE, phase, step,
             json.dumps(entry, default=_legacy_json_value),
             _timestamp_text(entry.get("timestamp")) or db._now()),
        )
