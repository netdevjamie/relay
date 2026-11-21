# Manual test plan: gates from your iPhone

This checks that you can decide a Relay gate from your own iPhone, on Wi-Fi that is not the Mac's, through the Claude app and Remote Control. A person has to do it: only your phone gets the push, and only you can say the card reads without sliding sideways. A pass means every step shows its "Expected" and nothing under "Must NOT happen" happens.

Everything else (every gate kind, the same ledger rows as the Mac, refusals) is covered by the automated tests. This plan uses two scratch tasks too, never a real one.

| | |
|---|---|
| Task | Deciding gates from a phone |
| Commit under test | {the commit you run, from `git rev-parse --short HEAD` in the checkout} |
| Build | A checkout of the Relay repo, run through the setup script's `relay` (not the installed one) |
| Environment | Your iPhone in the Claude app; the Mac awake, lid open, on power |
| Why not automated | A push to your phone and reading on its screen |
| Time needed | About 15 minutes, plus step 12 later at the Mac (2 minutes) |

## Before you start (at the Mac)

1. Remote Control is on for the session that drives Relay (`/remote-control`).
2. In that session's `/config`, **Push when actions required** is on. If it says "No mobile registered", open the Claude app on the phone once.
3. iPhone: Settings, Notifications, Claude: Allow Notifications on. Any Focus you use lets Claude through.
4. The session runs this, from the Relay checkout:
   `python3 docs/manual-tests/mobile-gates-setup.py`
   Its last line is `Ready. Run the plan's commands as: <folder>/relay <command>`. Below, "relay" means that `<folder>/relay`. Tell the session so.
5. The iPhone is on a Wi-Fi network that is not the Mac's. Write both network names under "Result".
6. Plant a role's Markdown for steps 2 and 6b. The session, with its file tool, replaces the line `Body.` in `<folder>/home/tasks/phone-review/04-reviewer.md` with these lines, exactly:

   ```
   # Review

   Summary for the owner: [tap me](https://example.com/link) ![img](https://example.com/p.png)

   Body: [tap me too](https://example.com/two) ![img2](https://example.com/q.png)
   ```

No accounts or secrets are needed beyond your own claude.ai sign-in in the app.

## Steps

Fill in Actual and Result (pass or fail). HH:MM is a time of day.

### 1. Push when Relay stops (criterion 4)
- **Do:** In the Claude app, open the driving session. Type: `Wait 60 seconds, then run relay next phone-review and ask me with your question tool.` Lock the phone at once. Write the time.
- **Expected:** A Claude push on the lock screen no later than 6 minutes after you locked it (5 minutes after `relay next` ran).
- **Actual:**
- **Result:**

### 2. The card, as Relay wrote it (criterion 1)
- **Do:** Tap the push.
- **Expected:** The card is in a code block (a box in a fixed-width font). In it, lines `phone-review`, `Step 4, Reviewer`, `Reviewer gate`, then `Reviewer's summary: [tap`, `me](https://example.com/link)`, `![img](https://example.com/p.png)` as plain characters, then `Choices:` with 1 approve, 2 send-back, 3 move-to, 4 abort, 5 note, and last `relay decide phone-review <choice> --step 4 --gate approval`. Below the box: `Relay is waiting on you: decide before this task can go on.` A question with options is open.
- **Actual:**
- **Result:**

### 3. Fits the phone (criterion 1)
- **Do:** Scroll the card from top to bottom.
- **Expected:** Every line reads whole without sliding sideways, except the one line that starts `relay decide` (the box may slide sideways for that line only).
- **Actual:**
- **Result:**

### 4. Decide with the question tool (criteria 1, 2)
- **Do:** In the question, select `send-back`, then tap Submit.
- **Expected:** The session shows, word for word: `Recorded:` / `Send back to Builder for another round.` / `Recorded as a send-back, not an approval.` / `Task phone-review, step 4.` / `Now: Builder, since HH:MM` / `No note.`
- **Actual:**
- **Result:**

### 5. What waits on you (criterion 1)
- **Do:** Type: `run relay waiting and show it as printed`
- **Expected:** Exactly three lines: `phone-close`, `  Security gate`, `  Step 6`. No `phone-review`.
- **Actual:**
- **Result:**

### 6. Read a report whole (criterion 1)
- **Do:** Type: `run relay show phone-close brief and show it as printed`
- **Expected:** In a code block: first line `# Phone test: security gate` (shown as written, not as a heading), last line `Last line of the phone test brief.` No folder path anywhere.
- **Actual:**
- **Result:**

### 6b. A role's link and image stay plain text (Security 11 #1)
- **Do:** Type: `run relay show phone-review 04-reviewer and show it as printed`
- **Expected:** In a code block, the lines `Summary for the owner: [tap me](https://example.com/link) ![img](https://example.com/p.png)` and `Body: [tap me too](https://example.com/two) ![img2](https://example.com/q.png)`, every bracket and parenthesis shown. Tap `tap me` and `tap me too`: nothing opens. No picture, broken-image icon or loading box appears.
- **Actual:**
- **Result:**

### 7. Look-alike closes in words (criterion 3)
- **Do:** Type: `run relay card phone-close`
- **Expected:** The card is in a code block. Choice 1 is `accept-close`, with `Accept the findings and close the task. Nothing is deployed.` No choice is named `deploy-done`. The `Phases:` line ends with `ops`, not `done`.
- **Actual:**
- **Result:**

### 8. Decide by a typed reply, with a note (criteria 1, 2)
- **Do:** Type exactly: `accept-close: Closed from my phone, "test" one 🚀`
- **Expected:** `Recorded:` / `Accept the findings and close the task.` / `Nothing is deployed.` / `Task phone-close, step 6.` / `Now: Done HH:MM` / `Note: Closed from my phone, "test" one 🚀`, the note character for character. HH:MM is the time you sent this reply, not when the scratch task was set up.
- **Actual:**
- **Result:**

### 9. The same decision twice is refused (criterion 2)
- **Do:** Type: `run that same relay decide again`
- **Expected:** First line `Not applied: phone-close is no longer waiting at step 6; it is now Done HH:MM.`, HH:MM the same time as in step 8, then, in a code block, `phone-close`, `Step 6, Security`, `Done on <today's date>.` and no `Choices:`.
- **Actual:**
- **Result:**

### 10. A permission prompt pushes too (criterion 4)
- **Do:** Type: `Use Bash to run: touch /tmp/relay-phone-push-test. Wait for my approval.` Lock the phone. Write the time.
- **Expected:** A Claude push within 5 minutes. Tapping it shows the command `touch /tmp/relay-phone-push-test` with a choice to allow it. Allow it: the session says it ran. (If the session runs it with no prompt, your settings already allow it: write "no prompt" and fail the step, then ask the session for a command it must ask about.)
- **Actual:**
- **Result:**

### 11. Clean up
- **Do:** Type: `run python3 docs/manual-tests/mobile-gates-setup.py --clean and rm /tmp/relay-phone-push-test`
- **Expected:** `Removed <folder>/relay-phone-gates`, and no error from `rm`.
- **Actual:**
- **Result:**

### 12. Later, at the Mac: lid closed (known-limits row "Nothing reaches the phone while the Mac sleeps")
- **Do:** Close the lid. Wait 2 minutes. On the phone, type `hello` to the session. Then open the lid.
- **Expected:** With the lid closed: no reply and no push; the app shows the session as not connected. Within 1 minute of opening the lid: the session reconnects and answers.
- **Actual:**
- **Result:**

## Must NOT happen

| # | Check | Expected | Actual | Result |
|---|---|---|---|---|
| N1 | In the app, count the `relay decide` runs the session made. | Three: steps 4, 8 and 9. None you did not ask for. | | |
| N2 | Read the output of steps 2, 5, 6, 6b, 7 and 9. | No folder path (nothing starting with your home folder's path or `/private/`). | | |
| N4 | Steps 2 and 6b. | No tappable link and no image from the report: the session shows Relay's output inside the code block, not retold outside it. | | |
| N3 | Step 9. | Nothing recorded: the session shows no `Recorded:` block. | | |

## Result

| | |
|---|---|
| Run by | |
| Date and time | |
| iPhone model, iOS, Claude app version | |
| Mac's Wi-Fi, phone's Wi-Fi | |
| Commit actually run (`git rev-parse --short HEAD` in the checkout) | |
| Overall | pass / fail |
| Steps that failed, and what was seen | |
