# Manual test plan: {task title}

Kept in the repo at `docs/manual-tests/{task-id}.md`. Written by the Tester, run by a person, and committed again with the results filled in. When you write a real plan, delete these instruction lines and replace every example row below; keep none of the examples.

{Two or three plain sentences: what is being tested, why a person has to do it, and what a pass means.}

One plan covers one thing and fits one sitting (about 15 minutes or 15 steps). If it is bigger, split it into plans that each stand alone.

| | |
|---|---|
| Task | {task-id} |
| Commit under test | {full SHA} |
| Build or version | {release build number, app version, or "deployed commit"} |
| Environment | {production, staging, local; device model and OS version; browser and version} |
| Why this can't be automated | {one sentence: real device, real phone call, store review, live payment, ...} |
| Time needed | {about N minutes} |

## Before you start

Everything the person needs in hand, so they never stop halfway:
- Accounts: {which test account, by name; where its password lives, never the password}
- Data that must exist first: {the record, and the exact query or screen that shows it is there}
- Tools: {device, cable, second phone, console login}
- Starting state: {signed out, app freshly installed, cache cleared, ...}

## Steps

One action per step. "Expected" is something the person can see or read back: the exact words on a screen, the exact value of a field, a row in a table with the query that reads it, a line in a log with where to find it. Never "works" or "looks right". Leave "Actual" and "Result" empty; the person running the plan fills them in.

| # | Proves criterion | Do exactly this | Expected | Actual | Result |
|---|---|---|---|---|---|
| 1 | {n} | Open the app from the home screen. | The sign-in screen shows, titled "Sign in", with the fields "Email" and "Password". | | |
| 2 | {n} | Type `tester@example.com` and the test password, then tap "Sign in". | The home screen shows within 3 seconds. The top bar reads "Hello, Tester". | | |
| 3 | {n} | Run: `SELECT last_login_at FROM users WHERE email = 'tester@example.com';` against {which database, how to connect}. | One row. `last_login_at` is within the last minute, in UTC. | | |

The person running the plan attaches a screenshot for any step whose "Expected" describes a screen, named `{step number}.png`, in `docs/manual-tests/{task-id}/`.

## Things that must NOT happen

| # | Check | Expected | Actual | Result |
|---|---|---|---|---|
| N1 | {After step 2, look at the device's notification shade.} | {No notification from the app.} | | |

## Clean up

What to undo afterwards (delete the test record, sign out, remove the build), each with how to confirm it is gone.

## Result

| | |
|---|---|
| Run by | |
| Date and time | |
| Device, OS, build actually used | |
| Overall | pass / fail |
| Steps that failed, and what was seen | |
