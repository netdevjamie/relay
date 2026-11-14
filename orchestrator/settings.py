"""Where Relay lives and what it is configured to do.

Paths (central install, or a legacy per-project .workflow/), the phase and
verdict vocabulary, YAML helpers, the config file, the repo registry and
the process's one ledger connection.

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
# Paths: central install with fallback to per-project .workflow/
# ---------------------------------------------------------------------------

_CENTRAL_HOME = Path(os.environ.get("RELAY_HOME", Path.home() / "Projects" / ".relay"))

# Detect if we're running from a per-project .workflow/ install (legacy fallback)
_SCRIPT_DIR = Path(__file__).resolve().parent
_LEGACY_WORKFLOW_DIR = _SCRIPT_DIR.parent
_IS_LEGACY = (_LEGACY_WORKFLOW_DIR / "handoffs").exists() and not _CENTRAL_HOME.exists()

if _IS_LEGACY:
    # Legacy per-project mode: resolve paths relative to .workflow/
    RELAY_HOME = _LEGACY_WORKFLOW_DIR
    TASKS_DIR = _LEGACY_WORKFLOW_DIR / "handoffs"
    AGENTS_DIR = _LEGACY_WORKFLOW_DIR.parent / ".claude" / "agents"
    CONFIG_PATH = _SCRIPT_DIR / "config.yaml"
    REGISTRY_PATH = None  # no registry in legacy mode
    # Mode-aware user-facing strings
    _CLI_CMD = ".workflow/flow"           # how user invokes commands
    _PIP_HINT = "pip install -r .workflow/orchestrator/requirements.txt"
else:
    # Central mode: resolve paths from /Projects/.relay/
    RELAY_HOME = _CENTRAL_HOME
    TASKS_DIR = RELAY_HOME / "tasks"
    AGENTS_DIR = RELAY_HOME / "agents"
    CONFIG_PATH = RELAY_HOME / "orchestrator" / "config.yaml"
    REGISTRY_PATH = RELAY_HOME / "registry.yaml"
    # Mode-aware user-facing strings
    _CLI_CMD = "relay"
    _PIP_HINT = f"pip install -r {RELAY_HOME}/orchestrator/requirements.txt"

# ---------------------------------------------------------------------------
# YAML (requires PyYAML)
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

DEFAULT_CONFIG = {
    # Automatic rounds in a row into one phase before Relay stops and asks.
    # A send-back or an override into the phase starts its count again.
    "max_retries": 3,
    "auto_invoke": False,
    "model": None,  # use default
    "persistence_backend": "files",  # "files" | "sqlite"; config.example.yaml sets sqlite
    "test_timeout_seconds": evidence.DEFAULT_TIMEOUT_SECONDS,  # Relay's own test runs
    "keep_awake_seconds": awake.DEFAULT_SECONDS,  # hold the Mac awake per phase; 0 = never
    "human_gates": {
        "after_designer": True,
        "after_architect": True,
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
# "files"). When active, exactly four functions in
# run_state.py dispatch to db.py instead of the filesystem: load_run_state,
# save_run_state, find_latest_artifact, get_next_step. Everything else in
# Relay (routing, retries, worktrees, CLI commands) is unchanged and
# unaware of which backend is in use -- it just calls these four functions.

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
# so tasks recorded before Security absorbed it still load and route, but
# nothing routes into it any more.
PIPELINE_ORDER = ["designer", "architect", "builder", "reviewer", "tester", "security", "ops"]
LEGACY_PHASES = ["pentest"]
VALID_PHASES = PIPELINE_ORDER + LEGACY_PHASES
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
    worktree_path), then to the legacy .workflow parent. None when nothing
    resolves (the agent inherits relay's cwd)."""
    worktree = state.get("worktree_path")
    if worktree:
        return worktree
    repo = state.get("repo")
    if repo:
        repo_path = _resolve_repo_path(repo)
        if repo_path:
            return str(repo_path)
    if _IS_LEGACY:
        return str(_LEGACY_WORKFLOW_DIR.parent)
    return None
