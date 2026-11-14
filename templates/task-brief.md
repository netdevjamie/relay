---
# How much of Relay's chain this task gets.
# fix: one-line definition of fixed, straight to the build.
# feature: the Designer's brief; the Architect only if the structure changes.
# project: the Designer, then the Architect.
size: feature
# yes for auth, input handling, money or production data: the Tester and
# Security run after the Reviewer. no: the Reviewer is the last check.
# Left at yes unless you know it is not risky.
risky: yes
# For a fix or a feature (which may skip the Architect): does it deploy?
deploy: no
---
# Task Brief: {title}
**Task ID:** {YYYYMMDD-short-slug}
**Date:** {date}
**Priority:** high | medium | low
**Target Repo:** {repo name}

## Goal
What needs to happen, in one or two sentences. Be specific.

## Context
Why this matters. What prompted it. Link to issue/ticket if applicable.

## Constraints
- Technical constraints (must use X, can't break Y)
- Time constraints
- Compatibility requirements

## Acceptance Criteria
- [ ] Specific, testable criterion 1
- [ ] Specific, testable criterion 2
- [ ] Specific, testable criterion 3

## Relevant Files
| File | Why |
|------|-----|
| path/to/file | what role it plays |

## Dependencies
Anything that must be done first or that this blocks.

## Risks / Open Questions
- Known risks
- Things you're unsure about
- Decisions that haven't been made yet

## Notes
Anything else. Links, screenshots, prior art, related PRs.
