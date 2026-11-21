#!/usr/bin/env bash
# PreToolUse hook for Relay's read-only roles (Designer, Architect, Reviewer,
# Security, Ops, Arbiter). They may write their own report, which lives in the task's
# folder under Relay's install, and scratch files in a temporary folder
# (the role files send long commands there), and nothing else. Any other Write is refused
# (exit 2: Claude Code blocks the call and shows the role this message).
#
# Claude Code passes the tool call as JSON on stdin; the path is
# tool_input.file_path. A call this script cannot read is refused as well.
set -u
RELAY_HOME="${RELAY_HOME:-$HOME/Projects/.relay}"
# Fail closed: with no python3 the call cannot be checked, so it is refused.
# (The role file also runs this script as `... || exit 2`, so a missing or
# broken script refuses too: Claude Code lets a call through on any other
# exit code.)
command -v python3 >/dev/null 2>&1 || { echo "Relay: python3 is missing, so this Write was refused." >&2; exit 2; }
exec python3 -I -c '
import json, os, sys
tasks = os.path.realpath(os.path.join(sys.argv[1], "tasks"))
allowed = [tasks] + [os.path.realpath(d) for d in (os.environ.get("TMPDIR"), "/tmp") if d]
try:
    call = json.load(sys.stdin)
    path = call["tool_input"]["file_path"]
    if not isinstance(path, str) or not path:
        raise ValueError
except Exception:
    print("Relay: this role writes only its own report, and the Write call could not be read, "
          "so it was refused.", file=sys.stderr)
    sys.exit(2)
target = os.path.realpath(os.path.expanduser(path))
# A role never rewrites a brief or a prototype record: Relay reads them.
if os.path.basename(target) in ("brief.md", "proto.yaml"):
    print(f"Relay: {path} belongs to the owner and Relay, so no role writes it.", file=sys.stderr)
    sys.exit(2)
if any(os.path.commonpath([root, target]) == root and target != root for root in allowed):
    sys.exit(0)
print(f"Relay: this role is read-only. It writes only its report under {tasks}/ "
      f"(and scratch files in a temporary folder), not {path}. Describe the change in your report instead.", file=sys.stderr)
sys.exit(2)
' "$RELAY_HOME"
