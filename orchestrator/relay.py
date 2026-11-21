#!/usr/bin/env python3
"""
Relay: a multi-agent workflow orchestrator.

Manages state, routing, artifact numbering, and agent invocation
across multiple registered repositories from a single install.

The `relay` command's argument parsing and entry point. Every command is
listed in the help text below (`relay --help`) and documented with an
example in docs/commands.md.
"""

import argparse
import sys
from pathlib import Path

import db
import proto
import settings
from doctor import run_doctor
from read_api import TaskNotFound
from commands import (
    JSON_COMMANDS, cmd_abort, cmd_card, cmd_decide, cmd_gate_check, cmd_list,
    cmd_next, cmd_note, cmd_override, cmd_profile, cmd_recover, cmd_register, cmd_resize,
    cmd_send_back, cmd_show, cmd_start, cmd_status, cmd_validate, cmd_waiting, run_json_command,
)
from settings import PIPELINE_ORDER

NOTE_COMMANDS = ("override", "send-back", "note", "decide")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_proto(args, parser) -> int:
    """`relay proto start|done|list` and `relay promote`: prototype mode
    (proto.py). Not runs of the chain, so they need no ledger."""
    try:
        if args.command == "promote":
            if not args.task_id:
                parser.error("'promote' requires the prototype's name")
            lines = proto.promote(args.task_id, args.size, args.risky, args.as_task,
                                  deploy=args.deploy)
        elif args.task_id == "list":
            lines = proto.list_lines()
        elif args.task_id in ("start", "done"):
            if not args.phase:
                parser.error(f"'proto {args.task_id}' requires the prototype's name")
            if args.task_id == "start":
                lines = proto.start(args.phase, args.repo, args.idea, args.hours)
            else:
                lines = proto.done(args.phase)
        else:
            parser.error("'proto' takes start, done or list")
    except proto.ProtoError as problem:
        print(f"Error: {problem}")
        return 1
    print("\n".join(lines))
    return 0


def exit_on_failure(exit_code: int) -> None:
    """End the process with a command's exit code when it is not 0. A
    command that succeeds returns normally."""
    if exit_code:
        sys.exit(exit_code)


def main():
    parser = argparse.ArgumentParser(
        description="Relay: a multi-agent workflow orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  start    <task-id> --repo <alias>  Start a new task in the current checkout (or --worktree PATH),
                                      which must be on a branch of its own; Relay makes no worktree
  next     <task-id>                 Parse latest artifact and advance workflow
  status   <task-id> [--json]        Show task status and history
  list     [--json]                  List all tasks across all repos
  gate-check <task-id> [--json]      Say whether a task is waiting, and on whom
  profile  <repo> [--json]           Check a repo's project profile (.relay/profile.yaml)
  validate <task-id>                 Check brief, run state, and artifacts for errors
  send-back <task-id> [--note TEXT]  Return the latest step for fixes (not an approval)
  note     <task-id> --note TEXT     Record a decision or a fact for later agents; moves nothing
  override <task-id> <phase>         Manually set next phase (human override)
  abort    <task-id>                 Abort a task run
  waiting                            List every task waiting on you (phone-sized)
  card     <task-id>                 One task's gate card: the gate in words, named choices
  show     <task-id> <report>        Print a report or the brief whole, by name
  decide   <task-id> <choice> --step N --gate KIND [--to PHASE] [--note TEXT]
                                      Make a choice from the card, for that step only
  resize   <task-id> [--size S] [--risky yes|no] --reason TEXT
                                      Change a task's sizing, either way (only the owner lowers one)
  register <path> [--alias <name>]   Register a repo with relay
  proto start <name> --repo <alias> --idea TEXT [--hours N]
                                      Prototype: build a sketch fast on proto/<name>, no chain
  proto done <name>                  The prototype is ready to show (needs a screenshot)
  proto list                         Every prototype and where it went
  promote <name> --size feature|project --risky yes|no [--deploy] [--as <task-id>]
                                      Turn a shown prototype into a real task; its code is never merged
  doctor                             Check the install can run a task, and say how to fix what is wrong
  recover  [--init | --from-projections [--decisions FILE] [--apply] | --regenerate-projections [task-id]]
                                      Sqlite-backend recovery/reconciliation (see below)
        """,
    )
    parser.add_argument("command", choices=["start", "next", "status", "list", "gate-check", "profile", "validate", "send-back", "override", "note", "abort", "waiting", "card", "show", "decide", "resize", "register", "doctor", "recover", "proto", "promote"])
    parser.add_argument("task_id", nargs="?", help="Task ID, repo path (register), or repo alias or path (profile)")
    parser.add_argument("phase", nargs="?", help="Phase for override; report name for show; choice for decide")
    parser.add_argument("--step", type=int, help="decide: the step the card was for")
    parser.add_argument("--gate", dest="gate_kind", help="decide: the kind of gate the card was for")
    parser.add_argument("--to", choices=PIPELINE_ORDER + ["done"], dest="to_phase",
                        help="decide move-to: the phase to move to")
    parser.add_argument("--auto", action="store_true", help="Auto-invoke agents via claude CLI")
    parser.add_argument("--deploy", action="store_true", help="Initial deploy guess; the Architect's plan decides whether Ops runs")
    parser.add_argument("--repo", help="Target repo alias for task")
    parser.add_argument("--base", help="start: the branch the task is based on (default: the repo's default branch)")
    parser.add_argument("--worktree", metavar="PATH",
                        help="start: the checkout the task builds in (default: the current folder)")
    parser.add_argument("--alias", help="Alias for register command")
    parser.add_argument("--idea", help="proto start: one paragraph: what it is, who it is for, what 'wow' looks like")
    parser.add_argument("--hours", type=float, help="proto start: the timebox in hours (default 4, at most 8)")
    parser.add_argument("--size", help="promote: how big the real build is: feature or project; "
                                       "resize: the task's new size: fix, feature or project")
    parser.add_argument("--risky", help="promote, resize: yes for auth, input handling, money or "
                                        "production data; else no")
    parser.add_argument("--reason", help="resize: why the sizing changes (required, one line)")
    parser.add_argument("--as", dest="as_task", metavar="TASK_ID",
                        help="promote: the real task's id (default <name>-build)")
    parser.add_argument("--note", help="override, send-back, note: what the owner decided or wants changed; "
                                       "recorded in the ledger and given to every later agent")
    parser.add_argument("--note-file", metavar="FILE", help="the same, read from a file (for a longer note)")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="status, list, gate-check, profile: print JSON (see docs/json-api.md)")
    parser.add_argument("--init", action="store_true", help="recover: create a fresh canonical database if none exists")
    parser.add_argument("--from-projections", action="store_true",
                         help="recover: report (or, with --apply, import) reconciliation against Markdown/YAML projections")
    parser.add_argument("--apply", action="store_true", help="recover: actually write the --from-projections import, not just report it")
    parser.add_argument("--decisions", metavar="FILE",
                         help="recover --from-projections: YAML file of explicit migration decisions")
    parser.add_argument("--regenerate-projections", action="store_true",
                         help="recover: regenerate run.yaml for one task_id, or all tasks if omitted")

    args = parser.parse_args()
    if args.note and args.note_file:
        parser.error("give --note or --note-file, not both")
    if args.note_file:
        try:
            args.note = Path(args.note_file).read_text()
        except OSError as problem:
            parser.error(f"--note-file could not be read: {problem}")
    if args.note is not None and args.command not in NOTE_COMMANDS:
        parser.error("--note and --note-file are for override, send-back, note and decide")
    if (args.step is not None or args.gate_kind or args.to_phase) and args.command != "decide":
        parser.error("--step, --gate and --to are for decide")
    if args.command == "decide" and (args.step is None or not args.gate_kind or not args.phase):
        parser.error("'decide' requires a task id, a choice, --step and --gate (the card gives all four)")
    if args.worktree and args.command != "start":
        parser.error("--worktree is for start")
    if args.command == "show" and not args.phase:
        parser.error("'show' requires a task id and a report name, such as brief or 03-builder")
    if args.as_json and args.command not in JSON_COMMANDS + ("profile",):
        parser.error(f"--json is only available for: {', '.join(JSON_COMMANDS)}, profile")
    config = settings.load_config()

    if config.get("auto_invoke"):
        args.auto = True

    if args.command == "doctor":
        sys.exit(run_doctor(settings.RELAY_HOME, Path.home() / ".claude" / "agents"))

    if args.command == "profile":
        # Reads one file in the repo; needs neither the ledger nor a task.
        if not args.task_id:
            parser.error("'profile' requires a repo alias or path")
        sys.exit(cmd_profile(args.task_id, args.as_json))

    proto_only = {"--idea": args.idea, "--hours": args.hours}
    promote_only = {"--as": args.as_task}
    promote_or_resize = {"--size": args.size, "--risky": args.risky}
    for flag, value in proto_only.items():
        if value is not None and args.command != "proto":
            parser.error(f"{flag} is for proto start")
    for flag, value in promote_only.items():
        if value is not None and args.command != "promote":
            parser.error(f"{flag} is for promote")
    for flag, value in promote_or_resize.items():
        if value is not None and args.command not in ("promote", "resize"):
            parser.error(f"{flag} is for promote and resize")
    if args.reason is not None and args.command != "resize":
        parser.error("--reason is for resize")

    if args.command in ("proto", "promote"):
        sys.exit(run_proto(args, parser))

    if args.command == "register":
        if not args.task_id:
            parser.error("'register' requires a repo path")
        exit_on_failure(cmd_register(args.task_id, alias=args.alias))
        return

    if args.command == "recover":
        exit_on_failure(cmd_recover(config, task_id=args.task_id, init=args.init,
                                    from_projections=args.from_projections, apply=args.apply,
                                    regenerate_projections=args.regenerate_projections,
                                    decisions_path=args.decisions))
        return

    if args.command == "waiting" and args.task_id:
        parser.error("'waiting' takes no task id: it lists every task waiting on you")
    if args.command not in ("list", "waiting") and not args.task_id:
        parser.error(f"Command '{args.command}' requires a task_id")

    if args.as_json:
        sys.exit(run_json_command(args.command, args.task_id, config))

    exit_code = 0
    try:
        if args.command == "list":
            cmd_list(config)
            return
        if args.command == "waiting":
            cmd_waiting(config)
            return

        if args.command == "start":
            exit_code = cmd_start(args.task_id, args.auto, config, deploy_intent=args.deploy,
                                  repo=args.repo, base=args.base, worktree=args.worktree)
        elif args.command == "next":
            exit_code = cmd_next(args.task_id, args.auto, config)
        elif args.command == "status":
            cmd_status(args.task_id, config)
        elif args.command == "gate-check":
            cmd_gate_check(args.task_id, config)
        elif args.command == "validate":
            exit_code = cmd_validate(args.task_id, config)
        elif args.command == "send-back":
            exit_code = cmd_send_back(args.task_id, config, args.auto, note=args.note)
        elif args.command == "override":
            if not args.phase:
                parser.error("'override' requires a phase argument")
            exit_code = cmd_override(args.task_id, args.phase, config, args.auto, note=args.note)
        elif args.command == "note":
            exit_code = cmd_note(args.task_id, config, args.note)
        elif args.command == "abort":
            exit_code = cmd_abort(args.task_id, config)
        elif args.command == "card":
            cmd_card(args.task_id, config)
        elif args.command == "show":
            exit_code = cmd_show(args.task_id, args.phase, config)
        elif args.command == "resize":
            exit_code = cmd_resize(args.task_id, config, args.size, args.risky, args.reason)
        elif args.command == "decide":
            exit_code = cmd_decide(args.task_id, args.phase, args.step, args.gate_kind, args.to_phase,
                                   args.note, config, args.auto)
    except TaskNotFound as e:
        print(f"Error: {e}")
        sys.exit(1)
    except db.DatabaseUnavailable as e:
        # Fail closed, cleanly -- never a raw traceback, and never a
        # silent fallback to the files backend or to projection data.
        print(f"Error: {e}")
        sys.exit(1)
    exit_on_failure(exit_code)


if __name__ == "__main__":
    main()
