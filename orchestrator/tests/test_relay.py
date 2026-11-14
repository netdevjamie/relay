"""Tests for the core of the orchestrator: routing, state, frontmatter, naming, registry."""

import os
import tempfile
from pathlib import Path

import pytest
import sys

# Add orchestrator to path so we can import relay
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings
from settings import (
    VALID_PHASES,
    VALID_VERDICTS,
    DEFAULT_CONFIG,
    load_yaml,
    dump_yaml,
    _resolve_repo_path,
    _IS_LEGACY,
    _CLI_CMD,
    RELAY_HOME,
)
from run_state import (
    parse_frontmatter,
    init_run_state,
    record_phase,
    save_run_state,
    load_run_state,
    check_state_consistency,
)
from routing import ROUTING, determine_next
from agents import artifact_name
from commands import cmd_register
from tests.hostile_yaml import (  # noqa: F401  (nothing_composed is a fixture)
    BAD_VALUE_NAMES, BAD_VALUES, SECURITYS_MERGE_KEYS, nothing_composed,
)


# ---------------------------------------------------------------------------
# determine_next: the routing table
# ---------------------------------------------------------------------------

class TestDetermineNext:
    """Every phase/verdict combo in ROUTING must produce the correct result."""

    @pytest.fixture
    def config(self):
        return dict(DEFAULT_CONFIG)

    # -- Architect --
    def test_architect_ready_for_builder(self, config):
        phase, human, _ = determine_next("architect", "ready-for-builder", config)
        assert phase == "builder"

    def test_architect_ready_for_builder_gate(self, config):
        config["human_gates"]["after_architect"] = True
        _, human, _ = determine_next("architect", "ready-for-builder", config)
        assert human is True

    def test_architect_ready_for_builder_no_gate(self, config):
        config["human_gates"]["after_architect"] = False
        _, human, _ = determine_next("architect", "ready-for-builder", config)
        assert human is False

    def test_architect_needs_human_input(self, config):
        phase, human, _ = determine_next("architect", "needs-human-input", config)
        assert phase is None
        assert human is True

    # -- Builder --
    def test_builder_ready_for_review(self, config):
        phase, _, _ = determine_next("builder", "ready-for-review", config)
        assert phase == "reviewer"

    def test_builder_blocked(self, config):
        phase, human, _ = determine_next("builder", "blocked", config)
        assert phase is None
        assert human is True

    def test_builder_needs_architect(self, config):
        phase, _, _ = determine_next("builder", "needs-architect", config)
        assert phase == "architect"

    # -- Reviewer --
    def test_reviewer_approved(self, config):
        phase, human, _ = determine_next("reviewer", "approved", config)
        assert phase == "tester"
        assert human is False

    def test_reviewer_approved_with_warnings(self, config):
        phase, human, _ = determine_next("reviewer", "approved_with_warnings", config)
        assert phase == "tester"
        assert human is True

    def test_reviewer_changes_requested(self, config):
        phase, _, _ = determine_next("reviewer", "changes_requested", config)
        assert phase == "builder"

    def test_reviewer_blocked(self, config):
        phase, human, _ = determine_next("reviewer", "blocked", config)
        assert phase is None
        assert human is True

    # -- tester now runs before security --

    def test_tester_pass(self, config):
        phase, human, _ = determine_next("tester", "pass", config)
        assert phase == "security"
        assert human is False

    def test_tester_fail(self, config):
        phase, _, _ = determine_next("tester", "fail", config)
        assert phase == "builder"

    def test_tester_pass_with_concerns(self, config):
        phase, human, _ = determine_next("tester", "pass_with_concerns", config)
        assert phase == "security"
        assert human is True

    def test_security_secure(self, config):
        phase, human, _ = determine_next("security", "secure", config)
        assert phase == "ops"
        assert human is False

    def test_security_findings(self, config):
        phase, human, _ = determine_next("security", "findings", config)
        assert phase == "ops"
        assert human is True

    def test_security_critical_findings(self, config):
        phase, _, _ = determine_next("security", "critical-findings", config)
        assert phase == "builder"

    # -- pentest is legacy: still routable for old tasks, never routed into --

    def test_pentest_secure(self, config):
        phase, _, _ = determine_next("pentest", "secure", config)
        assert phase == "ops"

    def test_pentest_findings(self, config):
        phase, human, _ = determine_next("pentest", "findings", config)
        assert phase == "ops"
        assert human is True

    def test_pentest_vulnerable(self, config):
        phase, _, _ = determine_next("pentest", "vulnerable", config)
        assert phase == "builder"

    def test_nothing_routes_into_pentest(self):
        from routing import ROUTING
        destinations = {next_phase for routes in ROUTING.values()
                        for next_phase, _ in routes.values()}
        assert "pentest" not in destinations

    # -- ops prepare: nothing ships without the owner --

    def test_ops_deploy_ready_waits_for_owner(self, config):
        phase, human, _ = determine_next("ops", "deploy-ready", config)
        assert phase is None
        assert human is True

    def test_ops_not_ready(self, config):
        phase, human, _ = determine_next("ops", "not-ready", config)
        assert phase is None
        assert human is True

    def test_ops_needs_manual_steps(self, config):
        phase, human, _ = determine_next("ops", "needs-manual-steps", config)
        assert phase is None
        assert human is True

    # -- ops execute --

    def test_ops_deployed_completes(self, config):
        phase, human, _ = determine_next("ops", "deployed", config)
        assert phase is None
        assert human is False

    @pytest.mark.parametrize("verdict", ["not-executed", "rolled-back", "deploy-failed"])
    def test_ops_execute_problems_go_to_owner(self, config, verdict):
        phase, human, _ = determine_next("ops", verdict, config)
        assert phase is None
        assert human is True

    # -- designer is the first phase --

    def test_designer_ready_for_architect_gates_by_default(self, config):
        phase, human, _ = determine_next("designer", "ready-for-architect", config)
        assert phase == "architect"
        assert human is True

    def test_designer_gate_can_be_turned_off(self, config):
        config["human_gates"] = {**config["human_gates"], "after_designer": False}
        phase, human, _ = determine_next("designer", "ready-for-architect", config)
        assert phase == "architect"
        assert human is False

    @pytest.mark.parametrize("verdict", ["needs-human-input", "recommend-no-build"])
    def test_designer_owner_decisions(self, config, verdict):
        phase, human, _ = determine_next("designer", verdict, config)
        assert phase is None
        assert human is True

    def test_architect_needs_designer(self, config):
        phase, human, _ = determine_next("architect", "needs-designer", config)
        assert phase == "designer"
        assert human is False

    def test_unknown_verdict(self, config):
        phase, human, reason = determine_next("builder", "yolo", config)
        assert phase is None
        assert human is True
        assert "Unknown verdict" in reason

    def test_unknown_phase(self, config):
        phase, human, reason = determine_next("nonexistent", "pass", config)
        assert phase is None
        assert human is True


class TestRoutingTableCompleteness:
    """Verify ROUTING covers every phase and every verdict is listed."""

    def test_all_phases_have_routes(self):
        for phase in VALID_PHASES:
            assert phase in ROUTING, f"Phase {phase} missing from ROUTING"

    def test_all_verdicts_have_routes(self):
        for phase, verdicts in VALID_VERDICTS.items():
            for verdict in verdicts:
                assert verdict in ROUTING[phase], (
                    f"Verdict '{verdict}' for phase '{phase}' missing from ROUTING"
                )


class TestHumanGateSettings:
    """Every gate setting the example config offers is one routing reads."""

    EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config.example.yaml"

    def test_the_example_offers_the_settings_the_defaults_hold(self):
        example = settings.load_yaml(self.EXAMPLE_CONFIG.read_text())
        assert set(example["human_gates"]) == set(settings.DEFAULT_CONFIG["human_gates"])

    @pytest.mark.parametrize("gate", list(settings.DEFAULT_CONFIG["human_gates"]))
    def test_turning_a_gate_off_stops_at_fewer_places(self, gate):
        def stops(gates_on: bool) -> set[tuple[str, str]]:
            config = {"human_gates": {name: True for name in settings.DEFAULT_CONFIG["human_gates"]}}
            config["human_gates"][gate] = gates_on
            return {(phase, verdict) for phase, verdicts in VALID_VERDICTS.items()
                    for verdict in verdicts if determine_next(phase, verdict, config)[1]}

        assert stops(gates_on=False) < stops(gates_on=True), (
            f"human_gates.{gate} set to false changes no routing decision")


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    def _write_tmp(self, content: str) -> Path:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        f.write(content)
        f.close()
        return Path(f.name)

    def test_valid_frontmatter(self):
        p = self._write_tmp("---\ntask_id: test\nphase: builder\nverdict: ready-for-review\n---\n\n# Body\n")
        fm = parse_frontmatter(p)
        assert fm["task_id"] == "test"
        assert fm["phase"] == "builder"
        assert fm["verdict"] == "ready-for-review"
        os.unlink(p)

    def test_missing_frontmatter(self):
        p = self._write_tmp("# Just a markdown file\nNo frontmatter here.\n")
        fm = parse_frontmatter(p)
        assert fm == {}
        os.unlink(p)

    def test_empty_file(self):
        p = self._write_tmp("")
        fm = parse_frontmatter(p)
        assert fm == {}
        os.unlink(p)

    def test_no_closing_delimiter(self):
        p = self._write_tmp("---\ntask_id: test\nphase: builder\n")
        fm = parse_frontmatter(p)
        assert fm == {}
        os.unlink(p)

    def test_nonexistent_file(self):
        fm = parse_frontmatter(Path("/nonexistent/file.md"))
        assert fm == {}

    @pytest.mark.parametrize("hostile", [SECURITYS_MERGE_KEYS, *BAD_VALUES, "phase: [unclosed\n"],
                             ids=["Security's merge keys", *BAD_VALUE_NAMES, "not YAML"])
    def test_a_frontmatter_that_cannot_be_read_is_none(self, nothing_composed, hostile):
        """YAML's whole loader built the merge keys and raised its own
        errors on the rest, out of every caller."""
        p = self._write_tmp(f"---\ntask_id: test\nverdict: pass\n{hostile}---\n\n# Body\n")
        assert parse_frontmatter(p) == {}
        os.unlink(p)

    def test_a_frontmatter_that_is_not_a_mapping_is_none(self):
        """YAML's whole loader gave the list as it was, and every caller
        asks a frontmatter for a key."""
        p = self._write_tmp("---\n- task_id\n- verdict\n---\n\n# Body\n")
        assert parse_frontmatter(p) == {}
        os.unlink(p)

    def test_frontmatter_with_extra_content(self):
        p = self._write_tmp("---\ntask_id: test\nverdict: pass\n---\n\nLots of body content here.\n\n## Section\nMore stuff.\n")
        fm = parse_frontmatter(p)
        assert fm["task_id"] == "test"
        assert fm["verdict"] == "pass"
        os.unlink(p)


# ---------------------------------------------------------------------------
# State round-trip: init → record → save → load
# ---------------------------------------------------------------------------

class TestStateRoundTrip:
    def test_init_state_has_required_fields(self):
        state = init_run_state("test-task")
        assert state["task_id"] == "test-task"
        assert state["status"] == "in_progress"
        assert state["current_phase"] == "designer"
        assert state["deploy_intent"] is False
        assert isinstance(state["history"], list)
        assert state["next_step"] == 1

    def test_record_phase_appends_to_history(self):
        state = init_run_state("test-task")
        record_phase(state, "architect", 1, verdict="ready-for-builder")
        assert len(state["history"]) == 1
        assert state["history"][0]["phase"] == "architect"
        assert state["history"][0]["verdict"] == "ready-for-builder"
        assert state["history"][0]["step"] == 1

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Monkey-patch HANDOFFS_DIR temporarily
            orig = settings.TASKS_DIR
            settings.TASKS_DIR = Path(tmpdir)
            try:
                state = init_run_state("round-trip-test")
                record_phase(state, "architect", 1, verdict="ready-for-builder")
                save_run_state("round-trip-test", state)
                loaded = load_run_state("round-trip-test")
                assert loaded["task_id"] == "round-trip-test"
                assert loaded["status"] == "in_progress"
                assert len(loaded["history"]) == 1
                assert loaded["history"][0]["verdict"] == "ready-for-builder"
            finally:
                settings.TASKS_DIR = orig

    def test_load_nonexistent_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orig = settings.TASKS_DIR
            settings.TASKS_DIR = Path(tmpdir)
            try:
                assert load_run_state("does-not-exist") is None
            finally:
                settings.TASKS_DIR = orig


# ---------------------------------------------------------------------------
# artifact_name: base and retry variants
# ---------------------------------------------------------------------------

class TestArtifactName:
    def test_base_names(self):
        state = {"retries": {"builder": 0, "reviewer": 0}}
        assert artifact_name("architect", 1, state) == "01-architect.md"
        assert artifact_name("builder", 2, state) == "02-builder.md"
        assert artifact_name("reviewer", 3, state) == "03-reviewer.md"
        assert artifact_name("security", 4, state) == "04-security.md"
        assert artifact_name("tester", 5, state) == "05-tester.md"
        assert artifact_name("ops", 6, state) == "06-ops.md"

    def test_a_builder_fix_is_numbered_by_the_builder_steps_before_it(self):
        """Not by the count of automatic rounds: a send-back sets that to zero."""
        state = {"retries": {"builder": 0, "reviewer": 0},
                 "history": [{"phase": "builder", "step": 2}, {"phase": "reviewer", "step": 3}]}
        assert artifact_name("builder", 4, state) == "04-builder-fix-1.md"
        state["history"] += [{"phase": "builder", "step": 4}, {"phase": "reviewer", "step": 5}]
        assert artifact_name("builder", 6, state) == "06-builder-fix-2.md"
        # A step already in the history keeps the name it was given.
        assert artifact_name("builder", 4, state) == "04-builder-fix-1.md"
        assert artifact_name("builder", 2, state) == "02-builder.md"

    def test_reviewer_retry_names(self):
        state = {"retries": {"builder": 0, "reviewer": 1}}
        assert artifact_name("reviewer", 5, state) == "05-reviewer-pass-2.md"

    def test_tester_retry_names(self):
        state = {"retries": {"builder": 0, "reviewer": 0, "tester": 1}}
        assert artifact_name("tester", 6, state) == "06-tester-pass-2.md"


# ---------------------------------------------------------------------------
# check_state_consistency: warn but don't crash
# ---------------------------------------------------------------------------

class TestStateConsistency:
    def test_no_crash_on_empty_state(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            orig = settings.TASKS_DIR
            settings.TASKS_DIR = Path(tmpdir)
            try:
                task_dir = Path(tmpdir) / "test-task"
                task_dir.mkdir()
                state = {"current_phase": "builder", "history": []}
                # Should not raise
                check_state_consistency("test-task", state, "architect", 1)
            finally:
                settings.TASKS_DIR = orig

    def test_phase_mismatch_warns(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            orig = settings.TASKS_DIR
            settings.TASKS_DIR = Path(tmpdir)
            try:
                task_dir = Path(tmpdir) / "test-task"
                task_dir.mkdir()
                state = {"current_phase": "builder", "history": []}
                check_state_consistency("test-task", state, "reviewer", 3)
                captured = capsys.readouterr()
                assert "STATE WARNINGS" in captured.out
                assert "The recorded phase is 'builder'" in captured.out
            finally:
                settings.TASKS_DIR = orig

    @staticmethod
    def _feature_run(task_dir: Path, size: str) -> dict:
        """Designer, Builder and Reviewer reports, no Architect's, under a
        brief of the given size that is not risky."""
        (task_dir / "brief.md").write_text(f"---\nsize: {size}\nrisky: no\ndeploy: no\n---\n# Brief\n")
        reports = [(1, "designer", "ready-for-builder"), (2, "builder", "ready-for-review"),
                   (3, "reviewer", "approved")]
        for step, phase, verdict in reports:
            (task_dir / f"{step:02d}-{phase}.md").write_text(
                f"---\ntask_id: t\nphase: {phase}\nstep: {step}\nverdict: {verdict}\n---\nreport\n")
        return {"current_phase": "reviewer",
                "history": [{"step": step, "phase": phase} for step, phase, _ in reports]}

    @pytest.mark.parametrize("size, warned", [("feature", False), ("project", True)])
    def test_a_missing_architect_is_reported_only_where_the_size_needs_one(self, tmp_path, monkeypatch,
                                                                            capsys, size, warned):
        monkeypatch.setattr(settings, "TASKS_DIR", tmp_path)
        task_dir = tmp_path / "t"
        task_dir.mkdir()
        state = self._feature_run(task_dir, size)
        check_state_consistency("t", state, "reviewer", 3)
        assert ("may have been skipped" in capsys.readouterr().out) is warned


# ---------------------------------------------------------------------------
# YAML round-trip
# ---------------------------------------------------------------------------

class TestYaml:
    def test_round_trip(self):
        data = {"key": "value", "nested": {"a": 1, "b": [1, 2, 3]}}
        text = dump_yaml(data)
        loaded = load_yaml(text)
        assert loaded == data

    def test_empty_string(self):
        assert load_yaml("") == {}

    def test_none_string(self):
        assert load_yaml("null") is None or load_yaml("null") == {}


# ---------------------------------------------------------------------------
# Registry and relay-specific features
# ---------------------------------------------------------------------------

class TestResolveRepoPath:
    def test_returns_none_without_registry(self):
        orig = settings.REGISTRY_PATH
        settings.REGISTRY_PATH = None
        try:
            assert _resolve_repo_path("anything") is None
        finally:
            settings.REGISTRY_PATH = orig

    def test_returns_none_for_missing_alias(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(dump_yaml({"repos": {"existing": "/some/path"}}))
            f.flush()
            orig = settings.REGISTRY_PATH
            settings.REGISTRY_PATH = Path(f.name)
            try:
                assert _resolve_repo_path("nonexistent") is None
            finally:
                settings.REGISTRY_PATH = orig
                os.unlink(f.name)

    def test_returns_path_for_valid_alias(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(dump_yaml({"repos": {"my-repo": "/tmp/my-repo"}}))
            f.flush()
            orig = settings.REGISTRY_PATH
            settings.REGISTRY_PATH = Path(f.name)
            try:
                result = _resolve_repo_path("my-repo")
                assert result == Path("/tmp/my-repo")
            finally:
                settings.REGISTRY_PATH = orig
                os.unlink(f.name)


class TestModeAwareConstants:
    def test_cli_cmd_is_string(self):
        assert isinstance(_CLI_CMD, str)
        assert len(_CLI_CMD) > 0

    def test_legacy_mode_detected_correctly(self):
        # _IS_LEGACY should be a bool
        assert isinstance(_IS_LEGACY, bool)


class TestInitRunStateRelay:
    def test_has_schema_version(self):
        state = init_run_state("test")
        assert state["schema_version"] == 1

    def test_has_repo_field(self):
        state = init_run_state("test")
        assert "repo" in state
        assert state["repo"] is None

    def test_repo_can_be_set(self):
        state = init_run_state("test")
        state["repo"] = "example-api"
        assert state["repo"] == "example-api"


class TestCmdRegister:
    def test_register_creates_registry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_reg = settings.REGISTRY_PATH
            settings.REGISTRY_PATH = Path(tmpdir) / "registry.yaml"
            # Create a fake repo dir
            repo_dir = Path(tmpdir) / "my-repo"
            repo_dir.mkdir()
            try:
                cmd_register(str(repo_dir), alias="test-repo")
                assert settings.REGISTRY_PATH.exists()
                with open(settings.REGISTRY_PATH) as f:
                    reg = load_yaml(f.read())
                assert reg["repos"]["test-repo"] == str(repo_dir.resolve())
                # Nothing is written into the repo: an untracked file there
                # would make Relay's own test run refuse the Builder's work.
                assert list(repo_dir.iterdir()) == []
            finally:
                settings.REGISTRY_PATH = orig_reg

    def test_register_rejects_nonexistent_path(self):
        with pytest.raises(SystemExit):
            cmd_register("/nonexistent/path/that/does/not/exist")
