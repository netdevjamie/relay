# Session Handoff: {task title}
**Task ID:** {id}
**Session date:** {YYYY-MM-DD}
**Session ended:** {time}

## Task State (from disk, not memory)
- **Phase:** {phase from `relay status`}
- **Status:** {status from `relay status`}
- **Latest artifact:** {filename} [{verdict}]
- **Deploy intent:** {yes/no}
- **Retries:** {counts, or "none"}

## What Happened This Session
- Concise list of actions actually taken (changes made, agents run, decisions reached)
- Do NOT pad this with work that didn't happen

## Uncommitted Changes
```
{exact output of git status --short, or "Clean working tree"}
```

## Branch State
- **Branch:** {name}
- **Commit:** {short hash} {first line of message}

## Next Step
The single most important thing to do next, and why.

## Files to Read First
| File | Why |
|------|-----|
| path | reason this file matters for resuming |

## Open Questions / Blockers
- Unresolved items, or "None"
