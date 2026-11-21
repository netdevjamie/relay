"""Project profiles: what Relay needs to know about one repo.

Each repo commits `.relay/profile.yaml`: its test, build and lint commands,
how it deploys (with a smoke test and a rollback per target), its app-store
details, the CLIs it needs, and its secrets by name and location only.

This module only reads and checks a profile. It never runs a command from
one (evidence.py runs the test command); it asks git one question, whether
the test report is ignored. Checking is strict on purpose: a misspelt key is an error, not a
silently ignored line, because the roles and (later) the gatekeeper act on
what the profile says.

The field reference is docs/profile.md; templates/profile.yaml is a starter.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

import bounded_yaml
from printable import printable

PROFILE_RELATIVE_PATH = Path(".relay") / "profile.yaml"
PROFILE_SCHEMA_VERSION = 1
# The most of a profile Relay reads; a real one is a few kilobytes. A larger
# one is not read at all: reading it costs time and memory for every byte,
# and a branch can commit one of any size.
MAX_PROFILE_BYTES = 64 * 1024
TOO_LARGE = f"is larger than {MAX_PROFILE_BYTES // 1024} KB"
NOT_READ = "The profile could not be read as YAML"

TOP_LEVEL_KEYS = {"schema_version", "platforms", "commands", "test_report", "flaky_check_runs", "required_clis",
                  "deploy_targets", "stores", "secrets", "security_test_targets", "observability",
                  "test_files", "known_flaws_doc", "test_harness", "test_clean_checkout",
                  "risk_paths"}
COMMAND_NAMES = {"test", "build", "lint", "audit", "start"}
REQUIRED_CLI_KEYS = {"name", "check"}
DEPLOY_TARGET_KEYS = {"name", "platform", "command", "requires_cli", "smoke", "rollback",
                      "rollback_cannot_undo"}
STORE_KEYS = {"platform", "app_id", "track", "release_script"}
SECRET_KEYS = {"name", "location"}
SECURITY_TEST_TARGET_KEYS = {"name", "url"}
# The most entries a profile's test_harness list may have.
MAX_TEST_HARNESS_ENTRIES = 200
# The risk categories of Relay's sizing hint (risk_hint.py), each with its
# words. A profile's `risk_paths` may add patterns to any of them.
RISK_CATEGORY_WORDS = {
    "auth": "auth",
    "input": "input handling",
    "payments": "money or payments",
    "schema": "migrations or schema",
    "production_data": "production data",
    "secrets": "secrets",
    "deploy": "deploy config",
}
RISK_CATEGORIES = tuple(RISK_CATEGORY_WORDS)
# The most patterns a profile's risk_paths may add to one category, and the
# longest one.
MAX_RISK_PATTERNS = 50
MAX_RISK_PATTERN_CHARACTERS = 200
# Where a secret lives: a macOS Keychain item, a KeePass entry, or an
# environment variable. Never the value itself.
SECRET_LOCATION_SCHEMES = ("keychain:", "keepass:", "env:")
# After "env:" comes a variable's name and nothing else, so `env:TOKEN=abc123`
# can't smuggle a value in.
ENV_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# Python: field(default_factory=list) gives every instance its own new list.
#   A plain `= []` default would be one list shared by all instances.
@dataclass
class ProfileReport:
    """The result of reading one repo's profile."""
    path: Path
    found: bool
    profile: dict | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.found and not self.errors


class Unreadable(Exception):
    """A profile Relay could not read, or does not read. The message says
    so and why, in a sentence; `why` is the reason alone."""

    def __init__(self, why: str):
        super().__init__(f"{NOT_READ}: {why}")
        self.why = why


def load_profile(repo_path: Path) -> ProfileReport:
    """Read and check `<repo>/.relay/profile.yaml`. Never raises on a bad
    profile: problems come back as errors in the report. The worktree's
    profile is the branch's to write, so it is read within the size limit
    and bounded_yaml's."""
    path = Path(repo_path) / PROFILE_RELATIVE_PATH
    if not path.is_file():
        return ProfileReport(path=path, found=False,
                             errors=[f"No profile found at {path}."])
    try:
        data = profile_in(_text_within_the_size_limit(path))
    except (OSError, UnicodeDecodeError) as problem:
        return ProfileReport(path=path, found=True, errors=[f"{NOT_READ}: {problem}"])
    except Unreadable as problem:
        return ProfileReport(path=path, found=True, errors=[str(problem)])
    errors, warnings = check_profile(data)
    if not errors and _is_text(data.get("test_report")) and not _is_ignored(Path(repo_path), data["test_report"]):
        warnings.append("test_report is not in .gitignore. Relay's test run would leave it as an "
                        "uncommitted file and refuse the run.")
    return ProfileReport(path=path, found=True, profile=data if isinstance(data, dict) else None,
                         errors=errors, warnings=warnings)


def _text_within_the_size_limit(path: Path) -> str:
    """The profile's text, read no further than the size limit, with line
    ends as Path.read_text() gives them."""
    with open(path, "rb") as profile:
        written = profile.read(MAX_PROFILE_BYTES + 1)
    if len(written) > MAX_PROFILE_BYTES:
        raise Unreadable(f"it {TOO_LARGE}.")
    return written.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def fits_the_size_limit(size: str) -> bool:
    """Whether a profile of this size, as `git cat-file -s` gives it, is
    one Relay reads."""
    size = size.strip()
    return size.isascii() and size.isdigit() and int(size) <= MAX_PROFILE_BYTES


def profile_in(text: str):
    """What a profile's text holds, as YAML's safe loader builds it, read
    by bounded_yaml. Raises Unreadable, never YAML's errors."""
    try:
        return bounded_yaml.load(text)
    except yaml.YAMLError as problem:
        raise Unreadable(str(problem)) from None
    except bounded_yaml.NotRead as problem:
        raise Unreadable(f"it {problem}.") from None


def check_profile(data) -> tuple[list[str], list[str]]:
    """Every problem with a parsed profile, as (errors, warnings).

    Errors mean the profile can't be trusted as written. Warnings mean it
    is usable but incomplete (a deploy target with no rollback, say)."""
    if not isinstance(data, dict):
        return ["The profile must be a mapping of settings."], []
    errors: list[str] = []
    warnings: list[str] = []
    _check_unknown_keys(data, TOP_LEVEL_KEYS, "the profile", errors)
    if data.get("schema_version") != PROFILE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {PROFILE_SCHEMA_VERSION}.")
    platforms = _check_platforms(data, errors)
    _check_commands(data.get("commands"), errors)
    _check_test_report(data, errors)
    _check_flaky_check_runs(data.get("flaky_check_runs"), errors)
    cli_names = _check_required_clis(data.get("required_clis"), errors)
    _check_deploy_targets(data.get("deploy_targets"), cli_names, platforms, errors, warnings)
    _check_stores(data.get("stores"), platforms, errors)
    _check_secrets(data.get("secrets"), errors)
    _check_security_test_targets(data.get("security_test_targets"), errors)
    _check_observability(data.get("observability"), errors)
    _check_test_files(data, errors)
    _check_known_flaws_doc(data, errors)
    _check_test_harness(data, errors)
    _check_test_clean_checkout(data, errors)
    check_risk_paths(data, errors)
    return errors, warnings


def _check_platforms(data: dict, errors: list[str]) -> set[str] | None:
    """Check the platforms list and return it as a set, or None when the
    profile doesn't list platforms (then any platform name is accepted)."""
    _check_text_list(data, "platforms", "platforms", errors)
    platforms = data.get("platforms")
    if isinstance(platforms, list):
        return {platform for platform in platforms if _is_text(platform)}
    return None


def _check_listed_platform(entry: dict, where: str, platforms: set[str] | None,
                           errors: list[str]) -> None:
    """An entry's platform must be one the profile lists (exact spelling),
    so `iOS` and `ios` can't quietly be two platforms."""
    platform = entry.get("platform")
    if platforms is not None and _is_text(platform) and platform not in platforms:
        errors.append(f"{where}: platform '{platform}' is not in platforms "
                      f"({', '.join(sorted(platforms))}).")


def _check_commands(commands, errors: list[str]) -> None:
    if not isinstance(commands, dict):
        errors.append("commands is required and must be a mapping (at least commands.test).")
        return
    _check_unknown_keys(commands, COMMAND_NAMES, "commands", errors)
    if not _is_text(commands.get("test")):
        errors.append("commands.test is required: the command that runs this repo's tests.")
    for name in sorted(COMMAND_NAMES & set(commands) - {"test"}):
        if not _is_text(commands[name]):
            errors.append(f"commands.{name} must be a non-empty command.")


def _is_ignored(repo_path: Path, relative: str) -> bool:
    """True when git ignores this path, or when that can't be asked (no git,
    not a repo): only a definite "not ignored" is worth a warning."""
    try:
        asked = subprocess.run(["git", "check-ignore", "-q", relative], cwd=repo_path,
                               capture_output=True)
    except OSError:
        return True
    return asked.returncode != 1


def _check_test_report(data: dict, errors: list[str]) -> None:
    """The JUnit report the test command writes: a path inside the repo."""
    if "test_report" not in data:
        return
    report = data["test_report"]
    if not _is_text(report):
        errors.append("test_report must be text: the path of the JUnit XML file the test command writes.")
        return
    if not _is_inside_the_repo(report):
        errors.append("test_report must be a path inside the repo (no leading '/' and no '..').")


def _is_inside_the_repo(relative: str) -> bool:
    path = Path(relative)
    return not path.is_absolute() and ".." not in path.parts


def _check_test_files(data: dict, errors: list[str]) -> None:
    """Which files are tests: patterns for paths inside the repo. Accepted
    so a profile that has it stays valid; nothing reads it."""
    if "test_files" not in data:
        return
    patterns = data["test_files"]
    if not isinstance(patterns, list) or not patterns or not all(_is_text(item) for item in patterns):
        errors.append("test_files must be a list of text patterns, one or more, such as tests/*.py.")
        return
    if not all(_is_inside_the_repo(pattern) for pattern in patterns):
        errors.append("test_files patterns must match paths inside the repo "
                      "(no leading '/' and no '..').")


def _check_known_flaws_doc(data: dict, errors: list[str]) -> None:
    """The document that lists the project's known flaws: a path inside
    the repo. Accepted and not read, like test_files."""
    if "known_flaws_doc" not in data:
        return
    document = data["known_flaws_doc"]
    if not _is_text(document):
        errors.append("known_flaws_doc must be text: the path of the document that lists known flaws.")
        return
    if not _is_inside_the_repo(document):
        errors.append("known_flaws_doc must be a path inside the repo (no leading '/' and no '..').")


def _check_test_harness(data: dict, errors: list[str]) -> None:
    """Extra files that make up the test harness: names or patterns for
    paths inside the repo. Relay reads them from the base's profile, so a
    branch cannot take a file off the list for its own run."""
    if "test_harness" not in data:
        return
    entries = data["test_harness"]
    if not isinstance(entries, list) or not entries or not all(_is_text(item) for item in entries):
        errors.append("test_harness must be a list of text names or patterns, one or more, "
                      "such as scripts/run-tests.sh.")
        return
    if len(entries) > MAX_TEST_HARNESS_ENTRIES:
        errors.append(f"test_harness may list at most {MAX_TEST_HARNESS_ENTRIES} entries.")
    if not all(_is_inside_the_repo(entry) for entry in entries):
        errors.append("test_harness entries must match paths inside the repo "
                      "(no leading '/' and no '..').")


def check_risk_paths(data: dict, errors: list[str]) -> None:
    """Patterns the profile adds to the sizing hint's risk categories:
    a mapping of category to a list of 1 to MAX_RISK_PATTERNS short text
    patterns. Additions only: Relay reads them from the base's profile and
    adds them to its defaults, which nothing can remove."""
    if "risk_paths" not in data:
        return
    risk_paths = data["risk_paths"]
    if not isinstance(risk_paths, dict):
        errors.append(f"risk_paths must be a mapping of risk category to a list of path patterns; "
                      f"the categories are {', '.join(RISK_CATEGORIES)}.")
        return
    for category in risk_paths:
        if category not in RISK_CATEGORIES:
            errors.append(f"Unknown risk category '{printable(category, 60)}' in risk_paths. "
                          f"Known categories: {', '.join(RISK_CATEGORIES)}.")
            continue
        patterns = risk_paths[category]
        if (not isinstance(patterns, list) or not 1 <= len(patterns) <= MAX_RISK_PATTERNS
                or not all(_is_risk_pattern(pattern) for pattern in patterns)):
            errors.append(f"risk_paths.{category} must be a list of 1 to {MAX_RISK_PATTERNS} "
                          f"path patterns, each text of at most {MAX_RISK_PATTERN_CHARACTERS} "
                          "characters on one line.")


def _is_risk_pattern(pattern) -> bool:
    return (_is_text(pattern) and len(pattern) <= MAX_RISK_PATTERN_CHARACTERS
            and not any(character in pattern for character in "\0\n\r"))


def _check_test_clean_checkout(data: dict, errors: list[str]) -> None:
    """Whether Relay runs the tests in a fresh checkout: true or false."""
    if "test_clean_checkout" in data and not isinstance(data["test_clean_checkout"], bool):
        errors.append("test_clean_checkout must be true or false.")


def _check_flaky_check_runs(runs, errors: list[str]) -> None:
    # Python: bool is a kind of int (True == 1), so it is ruled out by name.
    if runs is not None and (isinstance(runs, bool) or not isinstance(runs, int) or runs < 1):
        errors.append("flaky_check_runs must be a whole number, 1 or more.")


def _check_required_clis(clis, errors: list[str]) -> set[str]:
    """Check the CLI list and return the names it declares."""
    names: set[str] = set()
    for where, cli in _entries(clis, "required_clis", errors):
        _check_unknown_keys(cli, REQUIRED_CLI_KEYS, where, errors)
        if _require_text(cli, "name", where, errors):
            names.add(cli["name"])
        _check_optional_text(cli, "check", where, errors)
    return names


def _check_deploy_targets(targets, cli_names: set[str], platforms: set[str] | None,
                          errors: list[str], warnings: list[str]) -> None:
    seen_names: set[str] = set()
    for where, target in _entries(targets, "deploy_targets", errors):
        _check_unknown_keys(target, DEPLOY_TARGET_KEYS, where, errors)
        if _require_text(target, "name", where, errors):
            if target["name"] in seen_names:
                errors.append(f"{where}: the name '{target['name']}' is used more than once.")
            seen_names.add(target["name"])
        _require_text(target, "command", where, errors)
        for key in ("platform", "smoke", "rollback", "rollback_cannot_undo"):
            _check_optional_text(target, key, where, errors)
        _check_listed_platform(target, where, platforms, errors)
        _check_text_list(target, "requires_cli", f"{where}.requires_cli", errors)
        for cli in target.get("requires_cli") or []:
            if _is_text(cli) and cli not in cli_names:
                errors.append(f"{where}: requires_cli names '{cli}', which is not in required_clis.")
        # Ops reports not-ready for a target with no smoke test or rollback,
        # so these are worth knowing early without making the profile unusable.
        for key in ("smoke", "rollback"):
            if key not in target:
                warnings.append(f"{where}: no {key}. Ops will report not-ready until one is agreed.")


def _check_stores(stores, platforms: set[str] | None, errors: list[str]) -> None:
    for where, store in _entries(stores, "stores", errors):
        _check_unknown_keys(store, STORE_KEYS, where, errors)
        _require_text(store, "platform", where, errors)
        _check_listed_platform(store, where, platforms, errors)
        _require_text(store, "app_id", where, errors)
        for key in ("track", "release_script"):
            _check_optional_text(store, key, where, errors)


def _check_secrets(secrets, errors: list[str]) -> None:
    for where, secret in _entries(secrets, "secrets", errors):
        extra_keys = sorted(set(secret) - SECRET_KEYS)
        if extra_keys:
            errors.append(f"{where}: only name and location are allowed (found {', '.join(extra_keys)}). "
                          "A profile holds where a secret lives, never its value.")
        _require_text(secret, "name", where, errors)
        if _require_text(secret, "location", where, errors):
            _check_secret_location(secret["location"], where, errors)


def _check_secret_location(location: str, where: str, errors: list[str]) -> None:
    # Messages never repeat the location: a value pasted here by mistake
    # must not be echoed into a terminal or a log.
    scheme = next((scheme for scheme in SECRET_LOCATION_SCHEMES if location.startswith(scheme)), None)
    if scheme is None:
        errors.append(f"{where}: location must start with one of "
                      f"{', '.join(SECRET_LOCATION_SCHEMES)}")
        return
    rest = location[len(scheme):]
    if not rest.strip():
        errors.append(f"{where}: location names no item after '{scheme}'.")
    elif scheme == "env:" and not ENV_VARIABLE_NAME.fullmatch(rest):
        errors.append(f"{where}: after 'env:' comes a variable name only "
                      "(letters, digits and underscores), never a value.")


def _check_security_test_targets(targets, errors: list[str]) -> None:
    for where, target in _entries(targets, "security_test_targets", errors):
        _check_unknown_keys(target, SECURITY_TEST_TARGET_KEYS, where, errors)
        _require_text(target, "name", where, errors)
        if _require_text(target, "url", where, errors) and not _is_http_url(target["url"]):
            # This list is the allowlist for live attacks; a loose entry
            # here could point Security at the wrong thing.
            errors.append(f"{where}: url must be a full http:// or https:// address with a host.")


def _check_observability(observability, errors: list[str]) -> None:
    if observability is None:
        return
    if not isinstance(observability, dict):
        errors.append("observability must be a mapping, such as logs: where to look.")
        return
    for name, description in observability.items():
        if not _is_text(description):
            errors.append(f"observability.{name} must be text.")


def _entries(value, section: str, errors: list[str]) -> list[tuple[str, dict]]:
    """The entries of an optional list-of-mappings section, each paired with
    a label for messages ('deploy_targets[0]'). A wrongly shaped section or
    entry is reported and left out."""
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{section} must be a list.")
        return []
    entries = []
    for index, entry in enumerate(value):
        where = f"{section}[{index}]"
        if isinstance(entry, dict):
            entries.append((where, entry))
        else:
            errors.append(f"{where} must be a mapping.")
    return entries


def _check_unknown_keys(mapping: dict, known: set[str], where: str, errors: list[str]) -> None:
    for key in sorted(str(key) for key in set(mapping) - known):
        errors.append(f"Unknown key '{key}' in {where}. Known keys: {', '.join(sorted(known))}.")


def _is_http_url(text: str) -> bool:
    try:
        parts = urlsplit(text.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _is_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_text(mapping: dict, key: str, where: str, errors: list[str]) -> bool:
    if _is_text(mapping.get(key)):
        return True
    errors.append(f"{where}: {key} is required and must be text.")
    return False


def _check_optional_text(mapping: dict, key: str, where: str, errors: list[str]) -> None:
    if key in mapping and not _is_text(mapping[key]):
        errors.append(f"{where}: {key} must be text.")


def _check_text_list(mapping: dict, key: str, where: str, errors: list[str]) -> None:
    value = mapping.get(key)
    if value is None:
        return
    if not isinstance(value, list) or not all(_is_text(item) for item in value):
        errors.append(f"{where} must be a list of text items.")
