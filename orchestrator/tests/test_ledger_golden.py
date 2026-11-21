"""The ledger records the same history it did before its explicit write calls.

The golden record (fixtures/ledger_golden/walk.json) was written by
ledger_golden_walk.py on the code from before this change. A fresh walk
must match it action for action: the same steps rows (phase, step, verdict,
report file), the same retry counts, the same `relay status --json` and
`relay gate-check --json`, the same run.yaml and the same events, with
exactly the changes listed in `_expected_events` and `_expected_run_yaml`.
"""

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
from tests.ledger_golden_walk import GOLDEN_PATH, walk


def _golden() -> list[dict]:
    return json.loads(GOLDEN_PATH.read_text())


def _is_a_no_op_save(event: dict) -> bool:
    payload = event.get("payload") or {}
    return (event["event_type"] == db.EVENT_STATE_SAVED
            and payload["status"][0] == payload["status"][1]
            and payload["current_phase"][0] == payload["current_phase"][1])


def _expected_events(action: dict) -> list[dict]:
    """The golden action's events, with the two listed changes applied:

    (a) a `state_saved` whose save changed neither the status nor the
        phase is no longer written (only a real change writes one);
    (b) in an automatic round, the `retry` is recorded when the round is
        counted, before the save of the new step, so it now comes just
        before that action's `state_saved` instead of after it. Both are
        written in one transaction."""
    events = [event for event in action["events"] if not _is_a_no_op_save(event)]
    if not action["dispatched"]:
        return events
    reordered = []
    for event in events:
        if event["event_type"] == db.EVENT_RETRY and reordered \
                and reordered[-1]["event_type"] == db.EVENT_STATE_SAVED:
            reordered.insert(len(reordered) - 1, event)
        else:
            reordered.append(event)
    return reordered


# The one command whose golden run.yaml disagrees with the ledger (see
# _expected_run_yaml). The walk's raw insert of an older ledger's retry
# writes no run.yaml before or after this change, so its file lags too.
SEND_BACK = "send-back with a note"
RAW_INSERT = "a reviewer retry from an older ledger"


def _run_yaml_drifted(action: dict) -> bool:
    return yaml.safe_load(action["run_yaml"])["retries"] != action["retries"]


def _expected_run_yaml(action: dict) -> str:
    """The golden action's run.yaml, with one listed change:

    (c) run.yaml is now written once, after all of a command's writes have
        committed. Before, `relay send-back` wrote it after saving the new
        step but before its sent_back event, which starts the count again,
        so the file kept the old count while the ledger read 0: the drift
        this change removes. After the send-back the expected file carries
        the ledger's counts."""
    if action["action"] != SEND_BACK:
        return action["run_yaml"]
    stale = yaml.dump({"retries": yaml.safe_load(action["run_yaml"])["retries"]},
                      default_flow_style=False, sort_keys=False)
    assert action["run_yaml"].count(stale) == 1
    return action["run_yaml"].replace(stale, yaml.dump({"retries": action["retries"]},
                                                       default_flow_style=False, sort_keys=False))


# (d) The sizing hint, added after the golden record: every Builder step that
#     reports ready records one `risk_hint` event, and `relay status --json`
#     carries four more keys. Both are compared on their own below, then left
#     out of the comparison with the record; nothing else may differ.
SIZING_STATUS_KEYS = {"sizing", "sizing_history", "risk_hint", "sizing_judgments"}


def _without_the_sizing_upgrade(action: dict) -> tuple[list[dict], dict]:
    """The action's events with no `risk_hint`, and its status without the four sizing keys."""
    events = [event for event in action["events"] if event["event_type"] != db.EVENT_RISK_HINT]
    status = {key: value for key, value in action["status"].items() if key not in SIZING_STATUS_KEYS}
    return events, status


def _a_builder_reported_ready(action: dict) -> bool:
    return any(event["event_type"] == db.EVENT_VERDICT_RECORDED and event["phase"] == "builder"
               and event["verdict"] == "ready-for-review" for event in action["events"])


def test_the_walk_matches_the_golden_record_except_the_listed_changes(tmp_path, monkeypatch):
    fresh = walk(tmp_path, monkeypatch).actions
    golden = _golden()
    assert [action["action"] for action in golden if _run_yaml_drifted(action)] == [
        RAW_INSERT, SEND_BACK]
    assert [action["action"] for action in fresh] == [action["action"] for action in golden]
    assert sum(map(_a_builder_reported_ready, fresh)) >= 2
    for now, then in zip(fresh, golden):
        name = then["action"]
        events, status = _without_the_sizing_upgrade(now)
        hints = [event for event in now["events"] if event["event_type"] == db.EVENT_RISK_HINT]
        assert len(hints) == (1 if _a_builder_reported_ready(now) else 0), name
        assert set(now["status"]) - set(status) == SIZING_STATUS_KEYS, name
        assert now["steps"] == then["steps"], name
        assert now["retries"] == then["retries"], name
        assert status == then["status"], name
        assert now["gate_check"] == then["gate_check"], name
        assert now["run_yaml"] == _expected_run_yaml(then), name
        assert now["dispatched"] == then["dispatched"], name
        assert events == _expected_events(then), name


def test_every_dispatch_records_the_filename_the_prompt_named(tmp_path, monkeypatch):
    actions = walk(tmp_path, monkeypatch).actions
    dispatched = [name for action in actions for name in action["dispatched"]]
    recorded = [row["artifact_filename"] for row in actions[-1]["steps"]]
    assert dispatched == recorded
    assert any(name.endswith("-reviewer-pass-2.md") for name in dispatched)
    assert any(name.endswith("-builder-fix-1.md") for name in dispatched)


def test_no_agent_is_launched_inside_a_ledger_transaction(tmp_path, monkeypatch):
    done = walk(tmp_path, monkeypatch)
    assert len(done.prompts) == len(_golden()[-1]["steps"])
    assert done.launched_inside_a_transaction == []
