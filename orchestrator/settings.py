"""Where Relay lives and what it is configured to do.

Paths (the central install), the phase and verdict vocabulary, YAML
helpers, the config file, the repo registry and the process's one ledger
connection.

Other modules read the values that tests replace (TASKS_DIR, REGISTRY_PATH,
CONFIG_PATH, load_config, the ledger connection) as `settings.NAME`, never
by importing the name, so a replaced value is seen everywhere.
"""

import copy
import os
import sqlite3
import sys
from pathlib import Path

import awake
import db
import evidence


# ---------------------------------------------------------------------------
# Paths: the central install
# ---------------------------------------------------------------------------

RELAY_HOME = Path(os.environ.get("RELAY_HOME", Path.home() / "Projects" / ".relay"))
TASKS_DIR = RELAY_HOME / "tasks"
AGENTS_DIR = RELAY_HOME / "agents"
CONFIG_PATH = RELAY_HOME / "orchestrator" / "config.yaml"
REGISTRY_PATH = RELAY_HOME / "registry.yaml"
_CLI_CMD = "relay"  # how the owner runs a command
_PIP_HINT = f"pip install -r {RELAY_HOME}/orchestrator/requirements.txt"

# ---------------------------------------------------------------------------
# YAML: requires PyYAML
# ---------------------------------------------------------------------------
# The orchestrator state (run.yaml) uses nested lists-of-dicts which cannot
# be correctly round-tripped by a naive parser. Rather than ship a broken
# fallback that silently corrupts state, we require PyYAML and fail fast.

try:
    import yaml as _yaml
except ImportError:
    print(
        "Error: PyYAML is required.\n"
        "  Install it:  pip install pyyaml\n"
        f"  Or:          {_PIP_HINT}",
        file=sys.stderr,
    )
    sys.exit(1)


def load_yaml(text: str) -> dict:
    return _yaml.safe_load(text) or {}


def dump_yaml(data: dict) -> str:
    return _yaml.dump(data, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_AGENT_TIMEOUT_SECONDS = 2 * 60 * 60

DEFAULT_CONFIG = {
    # Automatic rounds in a row into one phase before Relay stops and asks.
    # A send-back or an override into the phase starts its count again.
    "max_retries": 3,
    "auto_invoke": False,
    "model": None,  # use default
    "effort": None,  # use Claude's own default
    # How long a role started under --auto may run before Relay stops it.
    "agent_timeout_seconds": DEFAULT_AGENT_TIMEOUT_SECONDS,
    # Per-role model, effort, timeout_seconds and permission_mode (agents.launch_settings).
    "roles": {},
    # "files" | "sqlite". config.example.yaml sets sqlite for a new install. The
    # default stays files so an older config.yaml without the key, which
    # setup.sh never replaces, keeps the backend its tasks are on.
    "persistence_backend": "files",
    "test_timeout_seconds": evidence.DEFAULT_TIMEOUT_SECONDS,  # Relay's own test runs
    "keep_awake_seconds": awake.DEFAULT_SECONDS,  # hold the Mac awake per phase; 0 = never
    "human_gates": {
        "after_designer": True,
        "after_architect": True,
        "reviewer_approved_with_warnings": True,
        "tester_pass_with_concerns": True,
    },
}


def load_config() -> dict:
    # Python: deepcopy copies nested dicts too. A plain dict(...) copy would share
    #   the inner dicts, so editing the copy would edit the defaults.
    config = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            user_config = load_yaml(f.read())
        # Shallow merge
        for k, v in user_config.items():
            if isinstance(v, dict) and k in config and isinstance(config[k], dict):
                config[k].update(v)
            else:
                config[k] = v
    return config


# ---------------------------------------------------------------------------
# SQLite backend wiring
# ---------------------------------------------------------------------------
# Inert unless config["persistence_backend"] == "sqlite" (the default is
# "files"). When active, run_state.py's functions dispatch
# to db.py instead of the filesystem: load_run_state, save_run_state,
# find_latest_artifact and get_next_step, and the calls that record what
# happens (create_run, dispatch_step, record_verdict, count_retry) inside
# ledger_writes(). Routing, retries and the CLI commands call these and are
# unaware of which backend is in use; the commands also write events
# straight to db.py, which the files backend does not keep.

_db_connection: sqlite3.Connection | None = None


def _sqlite_enabled(config: dict) -> bool:
    return config.get("persistence_backend") == "sqlite"


def _db_path() -> Path:
    return Path(os.environ.get("RELAY_DB_PATH", str(RELAY_HOME / "relay.db")))


def get_db_connection():
    """Lazily open (and cache) the sqlite connection for this process.

    Uses db.connect_existing(), not db.connect() -- the canonical database
    must already exist and pass an integrity check. A missing or corrupt
    database raises db.DatabaseUnavailable rather than silently creating
    or limping along on a fresh/broken file; see cmd_recover() for the
    only sanctioned way to create or rebuild it.
    """
    global _db_connection
    if _db_connection is None:
        db.configure(VALID_PHASES, VALID_VERDICTS, VALID_STATUSES)
        _db_connection = db.connect_existing(_db_path())
    return _db_connection


def use_read_only_ledger():
    """Make this process's ledger connection read-only. The read API calls
    this first, so a reader cannot write even by mistake: SQLite refuses.
    Same fail-closed checks as get_db_connection()."""
    global _db_connection
    db.configure(VALID_PHASES, VALID_VERDICTS, VALID_STATUSES)
    _db_connection = db.connect_existing(_db_path(), read_only=True)


def close_ledger():
    """Close this process's ledger connection, if one is open, so the next
    read opens a fresh one and no connection outlives its thread."""
    global _db_connection
    if _db_connection is not None:
        _db_connection.close()
        _db_connection = None


# ---------------------------------------------------------------------------
# Phases, verdicts and statuses
# ---------------------------------------------------------------------------

# Pipeline order for new tasks. "pentest" is legacy: it stays a valid phase
# and keeps its verdicts so tasks recorded before Security absorbed it still
# load and read, but it has no routes (routing.ROUTING) and nothing routes
# into it.
PIPELINE_ORDER = ["designer", "architect", "builder", "reviewer", "tester", "security", "ops"]
LEGACY_PHASES = ["pentest"]
# The Arbiter (arbiter.py) is a step Relay calls, never a place in the
# pipeline: it is not in PIPELINE_ORDER, so no override, --to or card offers
# to move a task into it.
ARBITER = "arbiter"
VALID_PHASES = PIPELINE_ORDER + [ARBITER] + LEGACY_PHASES
FIRST_PHASE = "designer"

OPS_PREPARE_VERDICTS = ["deploy-ready", "not-ready", "needs-manual-steps"]
OPS_EXECUTE_VERDICTS = ["not-executed", "deployed", "rolled-back", "deploy-failed"]

VALID_VERDICTS = {
    "designer": ["ready-for-architect", "ready-for-builder", "needs-human-input", "recommend-no-build"],
    "architect": ["ready-for-builder", "needs-designer", "needs-human-input"],
    "builder": ["ready-for-review", "blocked", "needs-architect"],
    "reviewer": ["approved", "approved_with_warnings", "changes_requested", "blocked"],
    "tester": ["pass", "fail", "pass_with_concerns"],
    "security": ["secure", "findings", "critical-findings"],
    "ops": OPS_PREPARE_VERDICTS + OPS_EXECUTE_VERDICTS,
    "pentest": ["secure", "findings", "vulnerable"],
    ARBITER: ["move-on", "targeted-round", "re-plan", "escalate"],
}

VALID_STATUSES = ["in_progress", "completed", "blocked", "abandoned"]


def _resolve_repo_path(alias: str) -> Path | None:
    """Look up a repo alias in the registry and return its path."""
    if not REGISTRY_PATH or not REGISTRY_PATH.exists():
        return None
    with open(REGISTRY_PATH) as f:
        registry = load_yaml(f.read()) or {}
    repos = registry.get("repos", {})
    path_str = repos.get(alias)
    if path_str:
        return Path(path_str)
    return None


def _task_cwd(state: dict) -> str | None:
    """Working directory for a task's phases: the checkout `relay start`
    recorded. Falls back to the registered repo path (a task with no
    worktree_path). None when nothing resolves (the agent inherits relay's
    cwd)."""
    worktree = state.get("worktree_path")
    if worktree:
        return worktree
    repo = state.get("repo")
    if repo:
        repo_path = _resolve_repo_path(repo)
        if repo_path:
            return str(repo_path)
    return None
