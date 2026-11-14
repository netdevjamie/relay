# Security policy

## Reporting a vulnerability

Please don't open a public issue for a security problem. Report it privately with GitHub's private vulnerability reporting: open the **Security** tab of this repository and click **Report a vulnerability**. Only the maintainer sees the report.

If that button isn't there, open an issue that asks for a private contact and leaves out the details.

A useful report says:

- what an attacker can do, and what they need first (local access, a crafted brief, a malicious repo, and so on)
- the steps to reproduce it, with the Relay version or commit
- what you expected to happen instead

## What to expect

Relay is maintained by one person, so these are goals, not guarantees:

- an acknowledgement within 7 days
- an assessment (accepted, or not a vulnerability, with the reason) within 30 days
- for an accepted report, a fix or a documented workaround, a note in `CHANGELOG.md`, and credit in the advisory if you want it

Please give a reasonable time to fix a problem before you disclose it publicly.

## Supported versions

Only the latest release gets security fixes. Relay is at 0.1.x.

## In scope

Problems in the code and files this repository ships:

- the orchestrator (`orchestrator/`) and the `relay` command, including the ledger, the JSON API and `relay decide`
- `setup.sh` and the files it installs
- the hook in `hooks/` that limits the read-only roles to writing their own report: any way around it is in scope
- the role prompts in `agents/`, where a prompt itself tells a role to do something unsafe
- the templates and the sample in `examples/`, where following them as documented exposes you

## Out of scope

- Claude Code itself, the Claude models, and Claude Code's permission system: report those to Anthropic
- vulnerabilities in your own project that a Relay role missed or introduced; Relay's roles are a review aid, not a guarantee
- a role that ignores its prompt; `docs/known-limits.md` lists what Relay checks in code and what it only asks of the roles
- the flaws already listed in `docs/known-limits.md`
- vulnerabilities in dependencies such as PyYAML; report those upstream (Dependabot watches them here)
- anything that needs an attacker who already controls your user account
