# Relay's threat model

The copy of this file on a task's base branch is the one that governs that task; a task branch's change to it governs from the next task after merge. What the file is for, and what happens when a repo has none, is in `docs/profile.md` under "Threat model". The rules for rating a finding against it are in the Builder, Reviewer, Tester and Security role files.

## Trusted

- The owner.
- The owner's machine.
- Relay's base branch, including its profile (`.relay/profile.yaml`) and this file, and the repo's git config.
- Relay's installed files.
- Relay's ledger, as a record of what happened: which role ran, when, and which gate passed. It is not trusted as instructions: any text a role wrote into it is as untrusted as that role's report.

## Attack surface

- Anything a task branch can hold: code, tests, the profile, documents, and this file.
- Every role report, and any other output Relay reads.
- Anything that reaches the command line or the phone gates other than the owner's own use, such as another local process or a web page open in the owner's browser.
- Input from outside: the network and outside services.
- Dependencies.
