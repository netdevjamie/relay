"""Tests for project profiles (.relay/profile.yaml) and `relay profile`."""

import copy
import json
import sys
import time
import unicodedata
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bounded_yaml
import project_profile
import relay
import settings
import commands
from project_profile import check_profile, load_profile
from tests.hostile_yaml import (  # noqa: F401  (nothing_composed is a fixture)
    BAD_VALUE_NAMES, BAD_VALUES, SECURITYS_MERGE_KEYS, nothing_composed,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

FULL_PROFILE = {
    "schema_version": 1,
    "platforms": ["web", "api", "ios"],
    "commands": {"test": "npm test", "build": "npm run build", "lint": "npm run lint",
                 "audit": "npm audit", "start": "npm run dev"},
    "flaky_check_runs": 3,
    "required_clis": [{"name": "vercel", "check": "vercel whoami"}],
    "deploy_targets": [{"name": "production", "platform": "web", "command": "vercel deploy --prod",
                        "requires_cli": ["vercel"], "smoke": "curl -fsS https://example.com/health",
                        "rollback": "vercel rollback", "rollback_cannot_undo": "Migrations."}],
    "stores": [{"platform": "ios", "app_id": "com.example.app", "track": "TestFlight",
                "release_script": "scripts/release-ios.sh"}],
    "secrets": [{"name": "DEPLOY_TOKEN", "location": "keychain:example-deploy-token"}],
    "security_test_targets": [{"name": "local", "url": "http://localhost:3000"}],
    "observability": {"logs": "vercel logs", "errors": "Sentry project example-web"},
}


def profile_with(**changes) -> dict:
    """The full valid profile with some top-level keys replaced."""
    profile = copy.deepcopy(FULL_PROFILE)
    profile.update(changes)
    return profile


def errors_for(profile) -> list[str]:
    return check_profile(profile)[0]


def write_profile(repo: Path, text: str) -> None:
    (repo / ".relay").mkdir(parents=True, exist_ok=True)
    (repo / ".relay" / "profile.yaml").write_text(text)


class TestValidProfiles:
    def test_full_profile_has_no_errors_or_warnings(self):
        assert check_profile(FULL_PROFILE) == ([], [])

    def test_smallest_profile_is_a_version_and_a_test_command(self):
        assert check_profile({"schema_version": 1, "commands": {"test": "pytest"}}) == ([], [])

    def test_relays_own_profile_is_valid(self):
        report = load_profile(REPO_ROOT)
        assert report.found
        assert (report.errors, report.warnings) == ([], [])

    def test_the_starter_template_is_valid(self):
        template = yaml.safe_load((REPO_ROOT / "templates" / "profile.yaml").read_text())
        assert check_profile(template) == ([], [])

    def test_the_templates_commented_examples_are_valid_too(self):
        """Someone who uncomments every example must end up with a valid profile."""
        lines = (REPO_ROOT / "templates" / "profile.yaml").read_text().splitlines()
        uncommented = [line[2:] if line.startswith("# ") and (
            line[2:3] in (" ", "-") or line[2:].split(":")[0] in project_profile.TOP_LEVEL_KEYS)
            else line for line in lines]
        profile = yaml.safe_load("\n".join(uncommented))
        # The two settings nothing reads are accepted but not offered.
        assert set(profile) == project_profile.TOP_LEVEL_KEYS - {"test_files", "known_flaws_doc"}
        assert check_profile(profile) == ([], [])


class TestStrictChecking:
    def test_profile_must_be_a_mapping(self):
        assert errors_for(["commands"]) == ["The profile must be a mapping of settings."]
        assert errors_for(None) == ["The profile must be a mapping of settings."]

    def test_unknown_top_level_key_is_an_error(self):
        errors = errors_for(profile_with(comands={"test": "x"}))
        assert any("Unknown key 'comands' in the profile" in error for error in errors)

    @pytest.mark.parametrize("version", [None, 2, "1"])
    def test_schema_version_must_be_1(self, version):
        assert "schema_version must be 1." in errors_for(profile_with(schema_version=version))

    def test_commands_and_a_test_command_are_required(self):
        missing = {"schema_version": 1}
        assert any("commands is required" in error for error in errors_for(missing))
        for empty in ({}, {"test": ""}, {"test": "   "}, {"test": ["pytest"]}):
            assert any("commands.test is required" in error
                       for error in errors_for(profile_with(commands=empty)))

    def test_misspelt_command_name_is_an_error(self):
        errors = errors_for(profile_with(commands={"test": "pytest", "tests": "pytest"}))
        assert any("Unknown key 'tests' in commands" in error for error in errors)

    def test_optional_command_must_be_text(self):
        errors = errors_for(profile_with(commands={"test": "pytest", "build": None}))
        assert errors == ["commands.build must be a non-empty command."]

    @pytest.mark.parametrize("runs", [0, -1, 1.5, "2", True])
    def test_flaky_check_runs_must_be_a_positive_whole_number(self, runs):
        assert errors_for(profile_with(flaky_check_runs=runs)) == [
            "flaky_check_runs must be a whole number, 1 or more."]

    def test_sections_must_be_lists_of_mappings(self):
        for section in ("required_clis", "deploy_targets", "stores", "secrets",
                        "security_test_targets"):
            assert f"{section} must be a list." in errors_for(profile_with(**{section: {"a": 1}}))
            assert f"{section}[0] must be a mapping." in errors_for(profile_with(**{section: ["x"]}))

    def test_platforms_must_be_a_list_of_text(self):
        assert errors_for(profile_with(platforms="web")) == ["platforms must be a list of text items."]

    def test_observability_must_map_names_to_text(self):
        assert errors_for(profile_with(observability=["logs"])) == [
            "observability must be a mapping, such as logs: where to look."]
        assert errors_for(profile_with(observability={"logs": 3})) == [
            "observability.logs must be text."]


class TestDeployTargets:
    def target(self, **changes) -> dict:
        target = copy.deepcopy(FULL_PROFILE["deploy_targets"][0])
        target.update(changes)
        return target

    def test_name_and_command_are_required(self):
        errors = errors_for(profile_with(deploy_targets=[{"smoke": "x", "rollback": "y"}]))
        assert errors == ["deploy_targets[0]: name is required and must be text.",
                          "deploy_targets[0]: command is required and must be text."]

    def test_names_must_be_unique(self):
        errors = errors_for(profile_with(deploy_targets=[self.target(), self.target()]))
        assert errors == ["deploy_targets[1]: the name 'production' is used more than once."]

    def test_required_cli_must_be_declared(self):
        errors = errors_for(profile_with(deploy_targets=[self.target(requires_cli=["fly"])]))
        assert errors == ["deploy_targets[0]: requires_cli names 'fly', which is not in required_clis."]

    def test_missing_smoke_or_rollback_is_a_warning_not_an_error(self):
        bare = {"name": "production", "command": "vercel deploy --prod"}
        errors, warnings = check_profile(profile_with(deploy_targets=[bare]))
        assert errors == []
        assert warnings == [
            "deploy_targets[0]: no smoke. Ops will report not-ready until one is agreed.",
            "deploy_targets[0]: no rollback. Ops will report not-ready until one is agreed."]

    def test_there_is_no_setting_that_turns_owner_approval_off(self):
        errors = errors_for(profile_with(deploy_targets=[self.target(approval="none")]))
        assert any("Unknown key 'approval' in deploy_targets[0]" in error for error in errors)


class TestSecrets:
    @pytest.mark.parametrize("extra_key", ["value", "token", "password"])
    def test_anything_beyond_name_and_location_is_refused(self, extra_key):
        secret = {"name": "DEPLOY_TOKEN", "location": "keychain:item", extra_key: "hunter2"}
        errors = errors_for(profile_with(secrets=[secret]))
        assert len(errors) == 1
        assert "never its value" in errors[0]
        assert "hunter2" not in errors[0]

    def test_location_must_say_where_the_secret_lives(self):
        secret = {"name": "DEPLOY_TOKEN", "location": "sk-live-abc123"}
        errors = errors_for(profile_with(secrets=[secret]))
        assert errors == ["secrets[0]: location must start with one of keychain:, keepass:, env:"]

    @pytest.mark.parametrize("location", ["env:TOKEN=abc123", "env:TOKEN abc123", "env:"])
    def test_env_location_is_a_variable_name_and_never_a_value(self, location):
        errors = errors_for(profile_with(secrets=[{"name": "T", "location": location}]))
        assert len(errors) == 1
        assert "abc123" not in errors[0]

    def test_a_location_must_name_something(self):
        errors = errors_for(profile_with(secrets=[{"name": "T", "location": "keychain:  "}]))
        assert errors == ["secrets[0]: location names no item after 'keychain:'."]

    def test_a_pasted_value_is_never_echoed_back(self):
        errors = errors_for(profile_with(secrets=[{"name": "T", "location": "sk-live-abc123"}]))
        assert "sk-live-abc123" not in " ".join(errors)

    @pytest.mark.parametrize("location", ["keychain:item", "keepass:Deploy/token", "env:DEPLOY_TOKEN"])
    def test_known_locations_are_accepted(self, location):
        assert errors_for(profile_with(secrets=[{"name": "T", "location": location}])) == []


class TestSecurityTestTargets:
    @pytest.mark.parametrize("url", ["prod.example.com", "file:///etc/passwd", "ftp://example.com",
                                     "http://", "localhost:3000", "https:///path"])
    def test_url_must_be_a_full_http_address(self, url):
        errors = errors_for(profile_with(security_test_targets=[{"name": "t", "url": url}]))
        assert errors == ["security_test_targets[0]: url must be a full http:// or https:// "
                          "address with a host."]

    @pytest.mark.parametrize("url", ["http://localhost:3000", "https://staging.example.com/api",
                                     "http://127.0.0.1:8000"])
    def test_full_http_addresses_are_accepted(self, url):
        assert errors_for(profile_with(security_test_targets=[{"name": "t", "url": url}])) == []


class TestPlatforms:
    def test_target_and_store_platforms_must_be_listed(self):
        profile = profile_with(platforms=["web"])  # the target ships web, the store says ios
        assert errors_for(profile) == ["stores[0]: platform 'ios' is not in platforms (web)."]
        profile["deploy_targets"][0]["platform"] = "Web"
        assert "deploy_targets[0]: platform 'Web' is not in platforms (web)." in errors_for(profile)

    def test_any_platform_name_is_accepted_when_none_are_listed(self):
        profile = profile_with()
        del profile["platforms"]
        assert errors_for(profile) == []


class TestLoadProfile:
    def test_missing_profile_is_reported_not_raised(self, tmp_path):
        report = load_profile(tmp_path)
        assert (report.found, report.valid, report.profile) == (False, False, None)
        assert report.errors == [f"No profile found at {tmp_path / '.relay' / 'profile.yaml'}."]

    def test_broken_yaml_is_reported_not_raised(self, tmp_path):
        write_profile(tmp_path, "commands: [unclosed\n")
        report = load_profile(tmp_path)
        assert (report.found, report.valid, report.profile) == (True, False, None)
        assert report.errors[0].startswith("The profile could not be read as YAML")

    def test_valid_profile_loads(self, tmp_path):
        write_profile(tmp_path, yaml.safe_dump(FULL_PROFILE))
        report = load_profile(tmp_path)
        assert report.valid
        assert report.profile == FULL_PROFILE

    def test_checking_a_profile_runs_none_of_its_commands(self, tmp_path):
        marker = tmp_path / "ran"
        write_profile(tmp_path, yaml.safe_dump(
            {"schema_version": 1, "commands": {"test": f"touch {marker}"},
             "required_clis": [{"name": "x", "check": f"touch {marker}"}]}))
        assert load_profile(tmp_path).valid
        assert commands.cmd_profile(str(tmp_path), as_json=False) == 0
        assert not marker.exists()


class TestAProfileRelayDoesNotRead:
    """Hostile YAML in the reader of the
    worktree's profile, which `relay profile` and Relay's test run use:
    YAML's whole loader expanded what a small file asked it to, and raised
    errors of its own on a value it could not build."""

    REFUSED = "The profile could not be read as YAML: it "

    def test_securitys_merge_key_profile_is_refused_before_anything_is_built(
            self, tmp_path, nothing_composed):
        write_profile(tmp_path, SECURITYS_MERGE_KEYS + "schema_version: 1\ncommands: {test: pytest}\n")
        started = time.monotonic()
        report = load_profile(tmp_path)
        assert time.monotonic() - started < 2
        assert (report.found, report.valid, report.profile) == (True, False, None)
        assert report.errors == [f"{self.REFUSED}{bounded_yaml.HOLDS_A_REFERENCE}."]

    @pytest.mark.parametrize("bad_value", BAD_VALUES, ids=BAD_VALUE_NAMES)
    def test_a_value_yamls_loader_raised_on_is_said_and_never_raised(
            self, tmp_path, nothing_composed, bad_value):
        write_profile(tmp_path, "schema_version: 1\ncommands: {test: pytest}\n" + bad_value)
        report = load_profile(tmp_path)
        assert (report.found, report.valid, report.profile) == (True, False, None)
        assert report.errors[0].startswith(self.REFUSED)

    def test_a_profile_over_the_size_limit_is_never_parsed(self, tmp_path, monkeypatch):
        parsed = []
        monkeypatch.setattr(project_profile.bounded_yaml.yaml, "parse",
                            lambda text, **options: parsed.append(len(text)) or iter(()))
        text = yaml.safe_dump(FULL_PROFILE)
        write_profile(tmp_path, text + "#" + "x" * (project_profile.MAX_PROFILE_BYTES - len(text)))
        report = load_profile(tmp_path)
        assert report.errors == [f"{self.REFUSED}is larger than 64 KB."]
        assert parsed == []

    def test_a_profile_at_the_size_limit_is_read(self, tmp_path):
        text = yaml.safe_dump(FULL_PROFILE)
        write_profile(tmp_path, text + "#" + "x" * (project_profile.MAX_PROFILE_BYTES - len(text) - 1))
        assert (tmp_path / ".relay" / "profile.yaml").stat().st_size == project_profile.MAX_PROFILE_BYTES
        assert load_profile(tmp_path).profile == FULL_PROFILE

    @pytest.mark.parametrize("written", ["templates/profile.yaml", ".relay/profile.yaml"])
    def test_the_template_and_relays_own_profile_read_as_yamls_loader_reads_them(
            self, tmp_path, written):
        text = (REPO_ROOT / written).read_text(encoding="utf-8")
        write_profile(tmp_path, text)
        assert load_profile(tmp_path).profile == yaml.safe_load(text)

    @pytest.mark.parametrize("hostile", [SECURITYS_MERGE_KEYS, *BAD_VALUES],
                             ids=["Security's merge keys", *BAD_VALUE_NAMES])
    def test_relay_profile_says_why_and_ends_in_exit_code_1(
            self, tmp_path, monkeypatch, capsys, nothing_composed, hostile):
        monkeypatch.setattr(settings, "REGISTRY_PATH", None)
        write_profile(tmp_path, "schema_version: 1\ncommands: {test: pytest}\n" + hostile)
        assert commands.cmd_profile(str(tmp_path), as_json=False) == 1
        assert f"  ERROR: {self.REFUSED}" in capsys.readouterr().out


class TestProfileCommand:
    PAYLOAD_KEYS = {"schema", "schema_version", "ok", "repo", "repo_path", "profile_path",
                    "found", "valid", "errors", "warnings", "profile"}

    @pytest.fixture(autouse=True)
    def no_registry(self, monkeypatch):
        monkeypatch.setattr(settings, "REGISTRY_PATH", None)

    def run_main(self, monkeypatch, capsys, *argv):
        monkeypatch.setattr(sys, "argv", ["relay", *argv])
        capsys.readouterr()
        with pytest.raises(SystemExit) as exit_info:
            relay.main()
        return exit_info.value.code, capsys.readouterr()

    def test_json_schema_for_a_valid_profile(self, tmp_path, monkeypatch, capsys):
        write_profile(tmp_path, yaml.safe_dump(FULL_PROFILE))
        code, captured = self.run_main(monkeypatch, capsys, "profile", str(tmp_path), "--json")
        payload = json.loads(captured.out)
        assert code == 0
        assert set(payload) == self.PAYLOAD_KEYS
        assert (payload["schema"], payload["schema_version"], payload["ok"]) == ("relay.profile", 4, True)
        assert (payload["found"], payload["valid"]) == (True, True)
        assert payload["profile"] == FULL_PROFILE
        assert payload["profile_path"] == str(tmp_path / ".relay" / "profile.yaml")

    def test_invalid_profile_is_an_answer_with_exit_code_1(self, tmp_path, monkeypatch, capsys):
        write_profile(tmp_path, "schema_version: 1\ncommands: {}\n")
        code, captured = self.run_main(monkeypatch, capsys, "profile", str(tmp_path), "--json")
        payload = json.loads(captured.out)
        assert code == 1
        assert set(payload) == self.PAYLOAD_KEYS
        assert (payload["ok"], payload["found"], payload["valid"]) == (True, True, False)
        assert payload["errors"] == [
            "commands.test is required: the command that runs this repo's tests."]

    def test_missing_profile_is_an_answer_with_exit_code_1(self, tmp_path, monkeypatch, capsys):
        code, captured = self.run_main(monkeypatch, capsys, "profile", str(tmp_path), "--json")
        payload = json.loads(captured.out)
        assert code == 1
        assert (payload["ok"], payload["found"], payload["valid"], payload["profile"]) == (
            True, False, False, None)

    def test_unknown_repo_is_a_json_error(self, tmp_path, monkeypatch, capsys):
        code, captured = self.run_main(monkeypatch, capsys, "profile",
                                       str(tmp_path / "no-such-repo"), "--json")
        payload = json.loads(captured.out)
        assert code == 1
        assert (payload["ok"], payload["error"]["code"]) == (False, "repo_not_found")

    def test_registered_alias_is_resolved(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "example-web"
        write_profile(repo, yaml.safe_dump(FULL_PROFILE))
        registry = tmp_path / "registry.yaml"
        registry.write_text(yaml.safe_dump({"repos": {"example": str(repo)}}))
        monkeypatch.setattr(settings, "REGISTRY_PATH", registry)
        code, captured = self.run_main(monkeypatch, capsys, "profile", "example", "--json")
        payload = json.loads(captured.out)
        assert code == 0
        assert (payload["repo"], payload["repo_path"]) == ("example", str(repo))

    def test_plain_text_lists_errors_and_warnings(self, tmp_path, monkeypatch, capsys):
        write_profile(tmp_path, yaml.safe_dump(
            {"schema_version": 1, "commands": {"test": "pytest"}, "oops": 1,
             "deploy_targets": [{"name": "prod", "command": "deploy"}]}))
        code, captured = self.run_main(monkeypatch, capsys, "profile", str(tmp_path))
        assert code == 1
        assert "ERROR: Unknown key 'oops' in the profile" in captured.out
        assert "Warning: deploy_targets[0]: no smoke." in captured.out
        assert "Valid." not in captured.out

    @pytest.mark.parametrize("written", [
        "schema_version: 1\ncommands: [‮evil​﻿\n  test\n",
        "schema_version: 1\ncommands: {test: pytest}\n‮evil​﻿" + "k" * 1_000 + ": 1\n",
    ], ids=["YAML's error quotes them", "an unknown key's name holds them"])
    def test_plain_text_prints_each_problem_on_one_line_without_format_characters_and_capped(
            self, tmp_path, monkeypatch, capsys, written):
        """In the worktree's profile: a right-to-left
        override the profile holds could reorder a printed line."""
        write_profile(tmp_path, written)
        code, captured = self.run_main(monkeypatch, capsys, "profile", str(tmp_path))
        errors = [line for line in captured.out.splitlines() if line.startswith("  ERROR: ")]
        assert code == 1
        assert len(errors) == 1 and "evil" in errors[0]
        assert [character for character in errors[0] if unicodedata.category(character) == "Cf"] == []
        assert len(errors[0]) <= len("  ERROR: ") + commands.PROFILE_PROBLEM_PRINT_LIMIT

    def test_plain_text_prints_relays_sentence_on_an_unknown_key_whole(self, tmp_path, monkeypatch, capsys):
        write_profile(tmp_path, "schema_version: 1\ncommands: {test: pytest}\noops: 1\n")
        code, captured = self.run_main(monkeypatch, capsys, "profile", str(tmp_path))
        known = ", ".join(sorted(project_profile.TOP_LEVEL_KEYS))
        assert f"  ERROR: Unknown key 'oops' in the profile. Known keys: {known}.\n" in captured.out

    def test_plain_text_for_a_valid_profile(self, tmp_path, monkeypatch, capsys):
        write_profile(tmp_path, yaml.safe_dump(FULL_PROFILE))
        code, captured = self.run_main(monkeypatch, capsys, "profile", str(tmp_path))
        assert code == 0
        assert "Valid. Test command: npm test" in captured.out
        assert "Deploy targets: production" in captured.out

    def test_profile_needs_no_ledger(self, tmp_path, monkeypatch, capsys):
        """`relay profile` must work on a machine with no database at all."""
        write_profile(tmp_path, yaml.safe_dump(FULL_PROFILE))
        monkeypatch.setenv("RELAY_DB_PATH", str(tmp_path / "missing.db"))
        monkeypatch.setattr(settings, "load_config", lambda: {"persistence_backend": "sqlite"})
        code, _ = self.run_main(monkeypatch, capsys, "profile", str(tmp_path), "--json")
        assert code == 0
        assert not (tmp_path / "missing.db").exists()


class TestReportCheckSettings:
    """`test_files` and `known_flaws_doc`: what Relay compares a report with."""

    def test_both_settings_are_accepted(self):
        profile = profile_with(test_files=["tests/*", "*.test.ts"],
                               known_flaws_doc="docs/known-limits.md")
        assert check_profile(profile) == ([], [])

    @pytest.mark.parametrize("patterns", [
        "tests/*", [], [""], ["tests/*", 3], [None], {"tests": "*"}, True, None,
    ], ids=["text, not a list", "an empty list", "an empty pattern", "a number in the list",
            "a null in the list", "a mapping", "a boolean", "a null"])
    def test_test_files_of_the_wrong_type_are_refused(self, patterns):
        assert errors_for(profile_with(test_files=patterns)) == [
            "test_files must be a list of text patterns, one or more, such as tests/*.py."]

    @pytest.mark.parametrize("pattern", ["/tests/*", "../other-repo/tests/*", "tests/../../*"])
    def test_a_test_files_pattern_outside_the_repo_is_refused(self, pattern):
        assert errors_for(profile_with(test_files=["tests/*", pattern])) == [
            "test_files patterns must match paths inside the repo (no leading '/' and no '..')."]

    @pytest.mark.parametrize("document", [["docs/known-limits.md"], "", "   ", 7, None, True])
    def test_a_known_flaws_doc_of_the_wrong_type_is_refused(self, document):
        assert errors_for(profile_with(known_flaws_doc=document)) == [
            "known_flaws_doc must be text: the path of the document that lists known flaws."]

    @pytest.mark.parametrize("document", ["/etc/passwd", "../known-limits.md", "docs/../../x.md"])
    def test_a_known_flaws_doc_outside_the_repo_is_refused(self, document):
        assert errors_for(profile_with(known_flaws_doc=document)) == [
            "known_flaws_doc must be a path inside the repo (no leading '/' and no '..')."]


class TestTestRunSettings:
    """`test_harness` and `test_clean_checkout`: how Relay's own run is guarded."""

    def test_both_settings_are_accepted(self):
        profile = profile_with(test_harness=["scripts/run-tests.sh", "*.testsettings"],
                               test_clean_checkout=True)
        assert check_profile(profile) == ([], [])

    @pytest.mark.parametrize("entries", [
        "scripts/run-tests.sh", [], [""], ["   "], ["a.sh", 3], [None], {"a": "b"}, True, None,
    ], ids=["text, not a list", "an empty list", "an empty entry", "a blank entry",
            "a number in the list", "a null in the list", "a mapping", "a boolean", "a null"])
    def test_test_harness_of_the_wrong_type_is_refused(self, entries):
        assert errors_for(profile_with(test_harness=entries)) == [
            "test_harness must be a list of text names or patterns, one or more, such as scripts/run-tests.sh."]

    @pytest.mark.parametrize("entry", ["/etc/passwd", "../other/run.sh", "a/../../b.sh"])
    def test_test_harness_must_be_paths_inside_the_repo(self, entry):
        assert errors_for(profile_with(test_harness=["ok.sh", entry])) == [
            "test_harness entries must match paths inside the repo (no leading '/' and no '..')."]

    def test_test_harness_may_not_be_too_long(self):
        too_many = [f"file{number}.sh" for number in range(project_profile.MAX_TEST_HARNESS_ENTRIES + 1)]
        assert errors_for(profile_with(test_harness=too_many)) == [
            f"test_harness may list at most {project_profile.MAX_TEST_HARNESS_ENTRIES} entries."]
        assert errors_for(profile_with(test_harness=too_many[:-1])) == []

    @pytest.mark.parametrize("value", ["true", "yes", 1, 0, None, [True], {"on": True}])
    def test_test_clean_checkout_must_be_true_or_false(self, value):
        assert errors_for(profile_with(test_clean_checkout=value)) == [
            "test_clean_checkout must be true or false."]

    def test_test_clean_checkout_false_is_accepted(self):
        assert errors_for(profile_with(test_clean_checkout=False)) == []

    def test_a_repeated_key_reads_as_its_last_value(self, tmp_path):
        """As every key does: YAML's loader keeps the last. A branch cannot
        use this to turn clean mode off, because the base's true wins
        (test_evidence_clean_checkout.py)."""
        write_profile(tmp_path, "schema_version: 1\ncommands:\n  test: pytest\n"
                                "test_clean_checkout: true\ntest_clean_checkout: false\n")
        report = load_profile(tmp_path)
        assert (report.valid, report.profile["test_clean_checkout"]) == (True, False)


class TestRiskPaths:
    """`risk_paths` adds patterns to the sizing hint's categories (risk_hint.py)."""

    def test_risk_paths_valid_passes(self):
        assert errors_for(profile_with(risk_paths={"auth": ["gatekeeper_*"],
                                                   "deploy": ["ops/release/*"]})) == []

    def test_risk_paths_unknown_category_is_an_error(self):
        errors = errors_for(profile_with(risk_paths={"everything": ["*"], "auth": ["gate*"]}))
        assert errors == [f"Unknown risk category 'everything' in risk_paths. Known categories: "
                          f"{', '.join(project_profile.RISK_CATEGORIES)}."]

    def test_an_unknown_category_with_control_characters_is_shown_printable(self):
        errors = errors_for(profile_with(risk_paths={"au\x1b[2Jth": ["*"]}))
        assert len(errors) == 1 and "\x1b" not in errors[0]

    @pytest.mark.parametrize("patterns", [
        [], "gate*", [1], [""], ["   "], ["a" * 201], ["two\nlines"], ["nul\0"],
        [f"p{number}" for number in range(51)], None, {"a": "b"},
    ])
    def test_risk_paths_entries_must_be_short_text(self, patterns):
        assert errors_for(profile_with(risk_paths={"auth": patterns})) == [
            "risk_paths.auth must be a list of 1 to 50 path patterns, each text of at most 200 "
            "characters on one line."]

    @pytest.mark.parametrize("risk_paths", [["auth"], "auth", 3, None])
    def test_risk_paths_must_be_a_mapping(self, risk_paths):
        errors = errors_for(profile_with(risk_paths=risk_paths))
        assert len(errors) == 1 and errors[0].startswith("risk_paths must be a mapping")

    def test_relay_profile_reports_an_unknown_category(self, tmp_path):
        write_profile(tmp_path, "schema_version: 1\ncommands:\n  test: pytest\n"
                                "risk_paths:\n  money: ['*cash*']\n")
        report = load_profile(tmp_path)
        assert not report.valid
        assert any("Unknown risk category 'money'" in error for error in report.errors)
