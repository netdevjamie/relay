# Install, upgrade and roll back

This page is for anyone running Relay from a checkout of this repository:
where the install lives, how to upgrade it safely, and how to put the
previous version back.

## Where things live

`./setup.sh` copies this checkout into an install folder and never links to
it, so the install only changes when you run `./setup.sh` again.

| What | Where |
| --- | --- |
| Engine (`orchestrator/*.py`, `requirements.txt`, a `.venv`) | `~/Projects/.relay/orchestrator/` |
| Role prompts, hooks, templates, the `relay` wrapper | `~/Projects/.relay/agents/`, `hooks/`, `templates/`, `relay` |
| Role prompts Claude Code reads | `~/.claude/agents/` |
| The command on your `PATH` | `~/.local/bin/relay`, a link to the wrapper |
| Your settings and repos | `~/Projects/.relay/orchestrator/config.yaml`, `~/Projects/.relay/registry.yaml` |
| The ledger and task folders | `~/Projects/.relay/relay.db`, `~/Projects/.relay/tasks/` |

The install folder is `~/Projects/.relay` unless `RELAY_HOME` is set; if you
set it, read `~/Projects/.relay` below as your `RELAY_HOME`. The `relay`
command finds its own install, but the write-guard hook in each role prompt
looks in `~/Projects/.relay` unless `RELAY_HOME` is set where Claude Code
runs. So for another folder, export `RELAY_HOME` in your shell profile and
in the environment Claude Code starts in. `./setup.sh` creates
`config.yaml`, `registry.yaml` and the ledger only when they are missing,
and never changes an existing ledger or task folder, so running it again is
safe.
When a role prompt in `~/.claude/agents` differs from Relay's, the first
install keeps a copy beside it as `<name>.before-relay`.

Change Relay in this repository and install with `./setup.sh`. Never edit
the files under `~/Projects/.relay` by hand: the next install overwrites
them, and the checkout no longer describes what is running.

## First install

```
git clone https://github.com/netdevjamie/relay.git
cd relay
./setup.sh
# If relay is not found: export PATH="$HOME/.local/bin:$PATH"
relay doctor
```

`relay doctor` checks Python, git, the `claude` command, that the role
prompts in `~/.claude/agents` match the install, the write-guard hook, the
registry and the ledger. It exits 1 if any check fails.

## Upgrading

1. Pull or check out the version you want, and make sure the checkout is
   clean: `git status --short` prints nothing. `./setup.sh` copies the files
   as they are on disk.
2. Wait until no Reviewer, Tester or Security is at work: an install
   changes the rating line Relay prints for those three at once, while a
   running role keeps the prompt it started with, so it could be told two
   different things (`docs/known-limits.md`). `relay list` shows each task's
   phase and when it was last updated (in UTC); a task that sits in one of
   those phases with no agent running for it does not count.
3. Take the backup under "Backup and rollback" below and keep the folder it
   prints.
4. Run `./setup.sh`, then the checks below.
5. Dispatch the next role from a new Claude Code session, which reads the
   installed role prompts when it starts. Whether a session that was
   already open picks up new prompts is not settled: Claude Code's
   documentation says an edited file in `~/.claude/agents` is used on the
   next delegation, but older sessions have been seen running the earlier
   text.

### Checks after an install

From the root of the checkout, compare it with the three places the install
wrote:

```
diff -rq agents ~/.claude/agents
diff -rq agents ~/Projects/.relay/agents
for module in orchestrator/*.py; do diff -q "$module" ~/Projects/.relay/orchestrator; done
```

Expect no line for the seven role files or any Python file. An "Only in"
line for an agent that is not Relay's is fine.

The smoke test is the one in Relay's own profile,
`relay list && relay doctor && relay list --json`: it fails if the installed
code does not run or any doctor check fails.

To see what a session actually gives a role, start a new session, dispatch
a role and ask it to quote a sentence you changed in this install. An answer
that does not match `~/.claude/agents` means the session reads another copy
of the role (see the next section).

## Role prompts that take Relay's place

Claude Code also reads a project-level `.claude/agents` folder at or above
the folder a session starts in, and a role defined there is used instead of
the one in `~/.claude/agents`. `./setup.sh` never writes those folders.
What makes a file a Relay role is the `name:` line in its frontmatter, not
the file name, and sub-folders are read too, so an old copy renamed
`builder-old.md` or moved into `backup/` still says `name: Builder` and is
still the Builder.

So look for the names, not the file names. Set `WORKSPACE` to the folder
that holds your repos (the one you start Claude Code sessions in, such as
`WORKSPACE=~/code`). The first search covers that folder's own
`.claude/agents`:

```
grep -rliE "^name:[[:space:]]*[\"']?(designer|architect|builder|reviewer|tester|security|ops)[\"']?[[:space:]]*$" "$WORKSPACE/.claude/agents"
```

The second covers every project-level folder under the workspace, because
each repo and each worktree can hold its own:

```
find "$WORKSPACE" -maxdepth 5 -type d -path '*/.claude/agents' -exec grep -rliE "^name:[[:space:]]*[\"']?(designer|architect|builder|reviewer|tester|security|ops)[\"']?[[:space:]]*$" {} +
```

Expect nothing from the first; "No such file or directory" also means
there is nothing there. Each file the second lists takes a role's
place for sessions started in that repo or worktree. A project's own agents
under other names are not listed and can stay. If a repo tracks such a file
in git, removing it is a commit in that repo; until then, start the session
that dispatches Relay's roles in the workspace folder.

The searches miss a few cases: symlinked files or folders, YAML that spells
the name another way (`name : Builder`, a trailing comment, a folded
scalar), and folders more than five levels under the workspace. They also
list a file whose body, not frontmatter, has such a line.

### Backup and rollback

A rollback restores only what the install writes, and the backup holds only
that: the engine's Python files and `requirements.txt`, `agents/`, `hooks/`,
`templates/` and the wrapper in the install folder, and `~/.claude/agents`.

**Never restore `relay.db` or `tasks/`**, and never `orchestrator/config.yaml`
or `registry.yaml`. The install does not change them, and Relay keeps
writing the ledger and the task folders after the backup: putting back an
older copy loses everything recorded since.

The backup, before the install:

```
BK=~/relay-backups/$(date +%Y%m%d-%H%M%S)
mkdir -p "$BK/relay-install/orchestrator" "$BK/claude-agents"
cp -p ~/Projects/.relay/orchestrator/*.py ~/Projects/.relay/orchestrator/requirements.txt "$BK/relay-install/orchestrator/"
cp -Rp ~/Projects/.relay/agents ~/Projects/.relay/templates "$BK/relay-install/"
mkdir -p "$BK/relay-install/hooks"
cp -Rp ~/Projects/.relay/hooks/. "$BK/relay-install/hooks/" 2>/dev/null || true
cp -p ~/Projects/.relay/relay "$BK/relay-install/"
cp -Rp ~/.claude/agents/. "$BK/claude-agents/"
echo "$BK"
```

The rollback, with `BK` set to the folder the backup printed:

```
cp -p "$BK"/relay-install/orchestrator/*.py "$BK"/relay-install/orchestrator/requirements.txt ~/Projects/.relay/orchestrator/
cp -Rp "$BK"/relay-install/agents/. ~/Projects/.relay/agents/
cp -Rp "$BK"/relay-install/templates/. ~/Projects/.relay/templates/
cp -Rp "$BK"/relay-install/hooks/. ~/Projects/.relay/hooks/
cp -p "$BK"/relay-install/relay ~/Projects/.relay/relay
cp -Rp "$BK"/claude-agents/. ~/.claude/agents/
```

Afterwards, the comparison above, run from a checkout of the earlier
version, prints no line for a role file or a Python file.

What the rollback does not put back or undo:

- A file the newer install added: the copy lines overwrite and remove
  nothing. Remove such a file by hand.
- Packages added to the install's `.venv` when `requirements.txt` changed.
  Rebuild the venv, or run `./setup.sh` from the earlier checkout.
- A change made since the backup to any file in `~/.claude/agents`,
  including agents that are not Relay's: the last line puts each back as the
  backup has it.
- Reports roles wrote under the newer prompts.
- A change to the ledger schema. That needs its own plan.

If the backup is lost, `./setup.sh` run from a checkout of the earlier
version copies that version's files over the same places.
`orchestrator/tests/test_install_notes.py` runs the backup, the rollback and
the checks on this page against a stand-in home folder.
