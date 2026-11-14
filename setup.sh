#!/usr/bin/env bash
# Install Relay, or update an existing install, from this checkout.
#
# Usage:
#   ./setup.sh
#   RELAY_HOME=/some/other/place ./setup.sh
#
# What it does:
#   1. Copies the engine, role prompts, hooks and templates to $RELAY_HOME
#      (default ~/Projects/.relay).
#   2. Copies the role prompts to ~/.claude/agents/ so Claude Code finds them.
#      A file of your own already there under the same name is backed up
#      once (to <name>.before-relay); an older Relay prompt is just replaced.
#   3. Creates a Python 3.11+ virtual environment and installs requirements.
#   4. Links the `relay` command into ~/.local/bin/. The installed wrapper
#      runs this install whenever RELAY_HOME is not set.
#
# Steps 2 and 4 always write under $HOME, whatever RELAY_HOME says. To try a
# change without touching a real install, give it a throwaway HOME and unset
# RELAY_HOME:
#   env -u RELAY_HOME HOME="$(mktemp -d)" ./setup.sh
#
# What it never does: register a repo, edit your shell rc files, or change
# anything in your projects. Re-run it after pulling a new version; your
# config, registry, tasks and ledger are kept.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RELAY_HOME="${RELAY_HOME:-$HOME/Projects/.relay}"
CLAUDE_AGENTS_DIR="${HOME}/.claude/agents"
BIN_DIR="${HOME}/.local/bin"
MIN_PYTHON="3.11"

if [ "$#" -gt 0 ]; then
    echo "setup.sh takes no options (set RELAY_HOME to choose where Relay goes)." >&2
    exit 1
fi

# The first interpreter on PATH that is Python 3.11 or newer.
find_python() {
    local candidate
    for candidate in python3 python3.13 python3.12 python3.11; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

if ! PYTHON="$(find_python)"; then
    echo "Relay needs Python ${MIN_PYTHON} or newer, and none was found on PATH." >&2
    echo "On macOS: brew install python@3.12   (then run ./setup.sh again)" >&2
    exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
    echo "Note: the claude CLI is not on PATH. Relay drives Claude Code; install it before your first task."
fi

echo "Installing Relay to ${RELAY_HOME} (Python: ${PYTHON})"

echo "  Copying the engine..."
mkdir -p "${RELAY_HOME}/orchestrator"
mkdir -p "${RELAY_HOME}/agents"
mkdir -p "${RELAY_HOME}/templates"
mkdir -p "${RELAY_HOME}/hooks"
mkdir -p "${RELAY_HOME}/tasks"
cp "${SCRIPT_DIR}/orchestrator/"*.py "${RELAY_HOME}/orchestrator/"
cp "${SCRIPT_DIR}/orchestrator/requirements.txt" "${RELAY_HOME}/orchestrator/"
# A module left in the install from an older version would still be importable.
for installed in "${RELAY_HOME}/orchestrator/"*.py; do
    if [ ! -f "${SCRIPT_DIR}/orchestrator/$(basename "${installed}")" ]; then
        echo "  Warning: ${installed} is not part of this version. Remove it if nothing needs it."
    fi
done

if [ ! -f "${RELAY_HOME}/orchestrator/config.yaml" ]; then
    cp "${SCRIPT_DIR}/orchestrator/config.example.yaml" "${RELAY_HOME}/orchestrator/config.yaml"
    echo "  Created config.yaml from the example."
fi

# True when a file under a role's name in ~/.claude/agents is one Relay put
# there: the copy the last install left in $RELAY_HOME/agents, or any
# version of that prompt this checkout's history holds.
is_relay_prompt() {
    local file="$1" name history
    name="$(basename "${file}")"
    if cmp -s "${file}" "${RELAY_HOME}/agents/${name}"; then
        return 0
    fi
    history="$(git -C "${SCRIPT_DIR}" log --format= --raw --no-abbrev -- "agents/${name}" 2>/dev/null)" || return 1
    [[ -n "${history}" && "${history}" == *" $(git hash-object "${file}") "* ]]
}

# Make an installed copy of the `relay` wrapper default to the install it
# sits in, so the command runs that install when RELAY_HOME is not set.
point_wrapper_at_install() {
    local wrapper="$1" install="$2" quoted
    quoted="$(printf '%q' "${install}")"
    if ! DEFAULT_LINE='RELAY_HOME="${RELAY_HOME:-$HOME/Projects/.relay}"' \
        INSTALL_LINES="INSTALLED_AT=${quoted}"$'\n''RELAY_HOME="${RELAY_HOME:-$INSTALLED_AT}"' \
        awk '$0 == ENVIRON["DEFAULT_LINE"] { print ENVIRON["INSTALL_LINES"]; found = 1; next }
             { print }
             END { exit !found }' "${wrapper}" > "${wrapper}.new"; then
        rm -f "${wrapper}.new"
        echo "The relay wrapper has no default RELAY_HOME line to point at ${install}." >&2
        return 1
    fi
    chmod +x "${wrapper}.new"
    mv "${wrapper}.new" "${wrapper}"
}

# Back up a file of the user's own before a role prompt replaces it. This
# runs before the copy into $RELAY_HOME/agents, so that copy is still the
# last install's.
mkdir -p "${HOME}/.claude/agents"
for prompt in "${SCRIPT_DIR}/agents/"*.md; do
    existing="${CLAUDE_AGENTS_DIR}/$(basename "${prompt}")"
    backup="${existing}.before-relay"
    if [ -f "${existing}" ] && [ ! -e "${backup}" ] && ! cmp -s "${prompt}" "${existing}" \
            && ! is_relay_prompt "${existing}"; then
        cp -p "${existing}" "${backup}"
        echo "  Backed up your own $(basename "${existing}") to ${backup}"
    fi
done

echo "  Copying role prompts, hooks and templates..."
cp "${SCRIPT_DIR}/agents/"*.md "${RELAY_HOME}/agents/"
cp "${SCRIPT_DIR}/hooks/"*.sh "${RELAY_HOME}/hooks/"
chmod +x "${RELAY_HOME}/hooks/"*.sh
cp "${SCRIPT_DIR}/templates/"* "${RELAY_HOME}/templates/"
cp "${SCRIPT_DIR}/relay" "${RELAY_HOME}/relay"
chmod +x "${RELAY_HOME}/relay"
point_wrapper_at_install "${RELAY_HOME}/relay" "${RELAY_HOME}"

echo "  Copying role prompts to ${CLAUDE_AGENTS_DIR}..."
cp "${SCRIPT_DIR}/agents/"*.md "${HOME}/.claude/agents/"


VENV="${RELAY_HOME}/orchestrator/.venv"
if [ -x "${VENV}/bin/python" ] &&
    ! "${VENV}/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    echo "  The existing virtual environment uses an older Python; rebuilding it..."
    rm -rf "${VENV}"
fi
if [ ! -d "${VENV}" ]; then
    echo "  Creating the virtual environment..."
    if ! "${PYTHON}" -m venv "${VENV}"; then
        rm -rf "${VENV}"
        echo "Could not create a virtual environment with ${PYTHON}." >&2
        exit 1
    fi
fi
echo "  Installing requirements..."
if command -v uv >/dev/null 2>&1; then
    uv pip install -q --python "${VENV}/bin/python" -r "${RELAY_HOME}/orchestrator/requirements.txt"
else
    "${VENV}/bin/pip" install -q -r "${RELAY_HOME}/orchestrator/requirements.txt"
fi

if [ ! -f "${RELAY_HOME}/registry.yaml" ]; then
    echo "repos: {}" > "${RELAY_HOME}/registry.yaml"
fi

if [ ! -e "${RELAY_HOME}/relay.db" ]; then
    echo "  Creating the ledger..."
    RELAY_HOME="${RELAY_HOME}" "${RELAY_HOME}/relay" recover --init >/dev/null
fi

mkdir -p "${BIN_DIR}"
ln -sf "${RELAY_HOME}/relay" "${BIN_DIR}/relay"

echo ""
echo "Relay is installed. The \`relay\` command is in ${BIN_DIR}."
case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) echo "Add ${BIN_DIR} to your PATH to use it." ;;
esac
echo "Next: relay doctor, then follow the quickstart in README.md."
