# Manual test plan: relay start asks a real remote without prompting

This plan checks one thing: when `relay start` asks a real hosted remote to confirm a task's base, nothing asks you for a passphrase, a password or keychain access, in the terminal or in a window. Relay must either confirm the base without a word, or refuse within 30 seconds with one line saying it could not reach the remote. A person has to do it because it needs your real SSH key and your real macOS keychain, which no test may touch. The suite already proves the rest: a remote that hangs is stopped at the time limit, and a remote whose ssh tries to open the terminal gets none, even when Relay has a terminal (`orchestrator/tests/test_base_trust_edges.py`). A pass means each step shows what its "Expected" cell says, and nothing under "Things that must NOT happen" happens.

The set-up, steps 3 to 7 and the clean-up have been run as written with a local bare repo in place of GitHub and a stand-in ssh that fails: the "Expected" wording is what was printed then. Steps 1, 2, N1 and N2 need a real key and keychain.

Everything runs against a scratch Relay home in `/tmp` and a scratch clone. Your real ledger, tasks and registry are never read or written. The only network use is two read-only `git ls-remote` asks, plus one `git clone` you run yourself.

| | |
|---|---|
| Task | Confirming a task's base against its remote |
| Commit under test | {the commit you run, from `git rev-parse --short HEAD` in the checkout}, or the installed version. Write the one you ran under "Result". |
| Build or version | A checkout of the Relay repo, or the installed code in `~/Projects/.relay/orchestrator` |
| Environment | The Mac, one terminal window, a scratch home under `/tmp`, network on |
| Why this can't be automated | It needs your real passphrase-protected SSH key and your real keychain, and a test may not use either |
| Time needed | About 10 minutes |

## Before you start

- Accounts: your own GitHub account. You need the SSH URL and the HTTPS URL of one **private** repo you can read (any of yours). No password or token is typed anywhere in this plan.
- Your SSH key must have a passphrase. If `ssh-keygen -y -P "" -f ~/.ssh/id_ed25519` prints a key, it has none: say so under "Result" and do only steps 5 to 7. (Use your key's file name if it is not `id_ed25519`.)
- Tools: one terminal window (bash or zsh). Use the same window for every step.
- Never type plain `relay`. Every command goes through `rs`, which is pinned to the scratch home.
- Paste **one** of these two lines first. It says which code is under test.
  - **A checkout** (put its path in place of the placeholder):
    ```
    PY=python3; CODE="<your Relay checkout>/orchestrator"
    ```
  - **The installed code**:
    ```
    PY=~/Projects/.relay/orchestrator/.venv/bin/python3; CODE=~/Projects/.relay/orchestrator
    ```
- Then set the two URLs and your key's path (replace the placeholders; nothing here is secret):
  ```
  SSH_URL='git@github.com:YOUR-NAME/YOUR-PRIVATE-REPO.git'
  HTTPS_URL='https://github.com/YOUR-NAME/YOUR-PRIVATE-REPO.git'
  KEY=~/.ssh/id_ed25519
  ```
- Then paste this set-up block. It clones the repo once (with your normal credentials, so this clone may prompt as git always does), makes the scratch home and two task worktrees, and defines `rs`.

```
S=$(mktemp -d /tmp/relay-manual.XXXXXX)
mkdir -p "$S/orchestrator" "$S/tasks/bt1" "$S/tasks/bt2"
printf 'persistence_backend: sqlite\nkeep_awake_seconds: 0\nauto_invoke: false\n' > "$S/orchestrator/config.yaml"
echo "# Scratch brief" > "$S/tasks/bt1/brief.md"; echo "# Scratch brief" > "$S/tasks/bt2/brief.md"
git clone -q "$SSH_URL" "$S/clone"
git -C "$S/clone" worktree add -q -b bt1-work "$S/wt1" && git -C "$S/clone" worktree add -q -b bt2-work "$S/wt2"
rs() { RELAY_HOME="$S" RELAY_DB_PATH="$S/relay.db" "$PY" "$CODE/relay.py" "$@"; }
rs recover --init | sed 's# at .*##' && rs register "$S/clone" --alias scratch | tail -1 | sed 's# -> .*##'
```

Expected last two lines of the set-up:

```
Created new canonical database
Registered: scratch
```

If you see anything else, stop and go to "Clean up".

## Steps

Leave "Actual" and "Result" empty until you run the step. If a window opens at any step, do not click anything in it: press Cancel or close it, and record it as a fail of that step.

| # | What it proves | Do exactly this | Expected | Actual | Result |
|---|---|---|---|---|---|
| 1 | No prompt | Make the scratch clone's ssh skip your agent and keychain so the key would need its passphrase: `git -C "$S/clone" config core.sshCommand "ssh -o IdentityAgent=none -o IdentitiesOnly=yes -o UseKeychain=no -i $KEY"` | No output. | | |
| 2 | No prompt | Show that plain git would ask here: `git -C "$S/clone" ls-remote --heads origin \| head -1`. When it asks for the passphrase, press Ctrl-C. | The terminal shows `Enter passphrase for key '...':` (this is git without Relay; it proves the set-up needs a passphrase). | | |
| 3 | No prompt; an unreachable remote refuses the start | Run: `time rs start bt1 --repo scratch --worktree "$S/wt1"` and wait. Type nothing. | Within 30 seconds of real time (the `real` line), one line: `Error: could not reach origin to confirm the base origin/<your default branch>; check the connection and start again.` No `Enter passphrase` line appears. | | |
| 4 | A refused start records nothing | Run: `rs status bt1 2>&1 \| head -1` | `No run state for bt1.` | | |
| 5 | No prompt | Switch the clone to HTTPS and drop the ssh setting: `git -C "$S/clone" remote set-url origin "$HTTPS_URL" && git -C "$S/clone" config --unset core.sshCommand` | No output. | | |
| 6 | No prompt; a reachable remote confirms the base | Run: `time rs start bt2 --repo scratch --worktree "$S/wt2"` and wait. Type nothing. | Within 30 seconds, one of two outcomes. (a) Your keychain already holds a GitHub token: the output has `Task bt2 started.` and `Branch:   bt2-work (base origin/<your default branch> at <7 characters>)`. (b) It holds none: one line `Error: could not reach origin to confirm the base origin/<your default branch>; check the connection and start again.` Either way no `Username for` or `Password for` line appears. | | |
| 7 | The recorded commit is the remote's | Only if step 6 gave outcome (a): compare the 7 characters with what GitHub has: `git -C "$S/clone" ls-remote --heads origin` (this one is plain git and may use your keychain as usual). | The id on the `refs/heads/<your default branch>` line starts with the same 7 characters step 6 printed. | | |

## Things that must NOT happen

| # | Check | Expected | Actual | Result |
|---|---|---|---|---|
| N1 | During steps 3 and 6, watch the screen for a window from ssh, "git-credential-osxkeychain", Keychain Access or a GitHub sign-in. | No window opens. | | |
| N2 | During steps 3 and 6, watch the terminal. | No line asks for a passphrase, username, password or "yes/no" (host key). Nothing waits for typing. | | |
| N3 | After step 3, run `ps -eo pid,command \| grep -c '[l]s-remote'`. | `0` (the ask was stopped, not left running). | | |

## Clean up

```
git -C "$S/clone" worktree remove --force "$S/wt1"; git -C "$S/clone" worktree remove --force "$S/wt2"; rm -rf "$S"; ls -d "$S" 2>&1 | head -1
```

Expected: `ls: /tmp/relay-manual.XXXXXX: No such file or directory` (with your folder's name). Nothing else on the machine was changed: the ssh setting in step 1 lived only in the scratch clone's own config.

## Result

| | |
|---|---|
| Run by | |
| Date and time | |
| Commit or installed version actually used | |
| Key had a passphrase (yes / no) | |
| Step 6 outcome ((a) or (b)) | |
| Overall | pass / fail |
| Steps that failed, and what was seen | |
