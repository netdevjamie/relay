"""What the owner has said about a task, kept in the ledger and handed to
every later agent.

The owner speaks at three moments: when approving a step and moving on
(`relay override ... --note`), when sending work back (`relay send-back ...
--note`), and at any other time (`relay note`). Each is its own event in
the ledger. This module reads them back as one list, and turns the
standing ones into the paragraph each later agent is given, so a decision
made at a gate reaches the roles that have to honour it without anyone
copying it by hand.

A note given with a send-back is an instruction for that one round of
fixes ("fix these two warnings and nothing else"). It goes to the agent
doing the fix and is kept on the record, but it is not handed to later
agents: carried forward, it would tell them to do work that is finished.
"""

import db
from settings import _sqlite_enabled, get_db_connection

# Every later agent is given every note, so one note is kept to a size a
# person would write, not a pasted log or a whole file.
MAX_NOTE_CHARACTERS = 4000

KIND_NOTE = "note"
KIND_APPROVAL = "approval"
KIND_SENT_BACK = "sent_back"


def read_owner_notes(task_id: str, config: dict) -> list[dict]:
    """Every note the owner has left on the task, oldest first. Approvals
    and send-backs without a note are not listed. The files backend keeps no
    events, so it has none."""
    if not _sqlite_enabled(config):
        return []
    notes = []
    for event in db.get_events(get_db_connection(), task_id):
        payload = event.get("payload") or {}
        note = payload.get("note")
        if not isinstance(note, str) or not note.strip():
            continue
        if event["event_type"] == db.EVENT_OWNER_NOTE:
            kind, moved_to = KIND_NOTE, None
        elif event["event_type"] == db.EVENT_APPROVAL:
            kind, moved_to = KIND_APPROVAL, payload.get("next_phase")
        elif event["event_type"] == db.EVENT_SENT_BACK:
            kind, moved_to = KIND_SENT_BACK, payload.get("to_phase")
        else:
            continue
        notes.append({"kind": kind, "phase": event["phase"], "step": event.get("step"),
                      "next_phase": moved_to, "method": payload.get("method"),
                      "note": note.strip(), "timestamp": event["timestamp"]})
    return notes


def checked(note: str | None) -> tuple[str | None, str | None]:
    """A note ready to record, as (note, problem). The note is trimmed; an
    empty one becomes None. `problem` is set when it is too long to keep."""
    if note is None or not note.strip():
        return None, None
    note = note.strip()
    if len(note) > MAX_NOTE_CHARACTERS:
        return None, (f"The note is {len(note)} characters; the limit is {MAX_NOTE_CHARACTERS}, "
                      f"because every later agent is given every note. Say the decision in the "
                      f"note and keep the detail in a file the agents can read.")
    return note, None


def prompt_with(task_id: str, config: dict, just_said: str | None) -> str:
    """The notes paragraph for an agent started by an approval, making sure
    what the owner has just said is in it even if reading it back from the
    ledger did not return it."""
    text = owner_notes_prompt(task_id, config)
    if just_said and just_said not in text:
        text += f"\n\nThe owner said, with this step:\n{just_said}"
    return text


def prompt_for_send_back(task_id: str, config: dict, note: str | None) -> str:
    """What the agent doing a fix is told: the note sent with this send-back,
    which is its instruction for this round, then the owner's standing notes."""
    text = f"\n\nThe owner sent this back with a note:\n{note}" if note else ""
    return text + owner_notes_prompt(task_id, config)


# The owner's answers at a process audit's gate, as the approval's method
# records them. Relay no longer runs the audit, but ledgers written by older
# versions still hold these answers, and their notes are still read.
AUDIT_ANSWERS = {"go-on": "go on", "pause": "pause"}


def _occasion(note: dict) -> str:
    if note["kind"] == KIND_APPROVAL and note.get("method") in AUDIT_ANSWERS:
        return f"answering the process audit ({AUDIT_ANSWERS[note['method']]})"
    if note["kind"] == KIND_APPROVAL:
        return f"approving {note['phase']}'s work and moving to {note['next_phase']}"
    if note["kind"] == KIND_SENT_BACK:
        return f"sending {note['phase']}'s work back to {note['next_phase']}"
    return f"a note left while the task was in {note['phase']}"


def owner_notes_prompt(task_id: str, config: dict) -> str:
    """The paragraph that tells an agent what the owner has decided so far:
    notes left with an approval or with `relay note`. Empty when there are
    none. Send-back notes are for one round of fixes and are left out."""
    notes = [note for note in read_owner_notes(task_id, config) if note["kind"] != KIND_SENT_BACK]
    if not notes:
        return ""
    lines = ["", "", "The owner's notes on this task, oldest first. These are the owner's decisions: "
             "where one differs from the brief, the plan or an earlier artifact, the note stands, "
             "and a later note stands over an earlier one. A note cannot waive a check your role "
             "requires, Relay's recorded test run or a security finding: if one seems to, do the "
             "check anyway and say so in your artifact."]
    for note in notes:
        lines.append(f"- {note['timestamp']}, {_occasion(note)}:")
        lines += [f"    {line}" for line in note["note"].splitlines()]
    return "\n".join(lines)
