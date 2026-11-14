#!/usr/bin/env python3
"""Ask a second AI to review a document, with Relay's project context.

Sends the context pack (context/PROJECT.md, context/DECISIONS.md, a freshly
generated state summary) plus the target document to a reviewer model, and
saves the review under reviews/ (git-ignored). Reviews are bounded spot
checks: the reviewer reports at most three issues, ranked by consequence,
and may say no changes are needed.

Two engines, both stateless (the reviewer keeps nothing between runs):
  claude      (default) a separate headless Claude Code session run from an
              empty temp folder: no tools, no settings, no CLAUDE.md, no
              memory, no MCP servers, no saved session. It sees only the
              payload (plus the account email Claude Code always attaches).
  openrouter  one plain API call to any OpenRouter model (GPT, Gemini, Grok,
              Claude). Needs an API key (see below).

Usage:
    python3 tools/review_prompt.py agents/reviewer.md
    python3 tools/review_prompt.py agents/reviewer.md --engine openrouter --model google/gemini-3.1-pro-preview
    python3 tools/review_prompt.py docs/plan.md --focus "is the rollout order safe?"
    python3 tools/review_prompt.py agents/reviewer.md --dry-run   # show what would be sent

API key: OPENROUTER_API_KEY in the environment, else the macOS Keychain item
named "openrouter-api-key" (add it with:
    security add-generic-password -s openrouter-api-key -a "$USER" -w).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTEXT_DIR = REPO_ROOT / "context"
REVIEWS_DIR = REPO_ROOT / "reviews"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-6.1-sol"
DEFAULT_CLAUDE_MODEL = "sonnet"
CLAUDE_TIMEOUT_SECONDS = 600
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
KEYCHAIN_SERVICE = "openrouter-api-key"

REVIEWER_INSTRUCTIONS = """You are an independent reviewer for Relay, a project you only know through the context below.

How to review:
- Read PROJECT.md first: it is the owner's overview and priorities. Judge the document against it.
- DECISIONS.md lists settled decisions. Do not reopen them unless you have new evidence; if you do, say what the evidence is.
- Report AT MOST three issues, ranked by consequence (what goes wrong in practice if this ships as written).
- "No changes needed" is a valid and welcome answer. Do not invent problems to have something to say.
- Prefer concrete failure scenarios over style preferences. Skip wording nits unless they change meaning.
- Separate what you are sure of from what you suspect.

Answer in this format:
## Verdict
One line: no changes needed | minor changes | significant changes.
## Issues
1. **Title.** What goes wrong, a concrete example, and a suggested fix. (Up to three.)
## Questions for the owner
Anything that is a taste or product call rather than a defect. Optional.
"""

# Patterns that look like credentials. If any match, nothing is sent.
SECRET_PATTERNS = {
    "OpenAI/OpenRouter-style key": r"\bsk-[A-Za-z0-9_\-]{20,}",
    "GitHub token": r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "Google API key": r"\bAIza[0-9A-Za-z_\-]{35}\b",
    "Slack token": r"\bxox[abpr]-[A-Za-z0-9\-]{10,}",
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "assigned password or secret": r"(?i)\b(password|passwd|secret|api_key|token)\s*[:=]\s*['\"]?[^\s'\"]{8,}",
}


def find_secrets(text: str) -> list[str]:
    """Names of the secret patterns found in text (never the matches)."""
    return [name for name, pattern in SECRET_PATTERNS.items() if re.search(pattern, text)]


def run_quietly(command: list[str]) -> str:
    """Output of a command, or a short note if it isn't available."""
    try:
        completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"(unavailable: {error.__class__.__name__})"
    if completed.returncode != 0:
        return "(unavailable)"
    return completed.stdout.strip()


def build_state_summary() -> str:
    """A fresh snapshot of where the project is: its recent commits.
    Generated on every call, never stored."""
    recent_commits = run_quietly(["git", "log", "--oneline", "-10"])
    return (f"# Current state (generated {datetime.now():%Y-%m-%d %H:%M})\n\n"
            f"## Recent commits\n{recent_commits}\n")


def build_messages(target: Path, focus: str | None) -> list[dict]:
    project = (CONTEXT_DIR / "PROJECT.md").read_text()
    decisions = (CONTEXT_DIR / "DECISIONS.md").read_text()
    document = target.read_text()
    focus_line = f"\nThe owner especially wants to know: {focus}\n" if focus else ""
    user_content = (
        f"<project>\n{project}\n</project>\n\n"
        f"<decisions>\n{decisions}\n</decisions>\n\n"
        f"<state>\n{build_state_summary()}\n</state>\n\n"
        f"<document path=\"{target.name}\">\n{document}\n</document>\n"
        f"{focus_line}\nReview the document."
    )
    return [{"role": "system", "content": REVIEWER_INSTRUCTIONS},
            {"role": "user", "content": user_content}]


def read_api_key() -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    keychain_key = completed.stdout.strip()
    return keychain_key or None


def ask_model(model: str, messages: list[dict], api_key: str) -> str:
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps({"model": model, "messages": messages}).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "X-Title": "Relay review-prompt"},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            body = json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit(f"OpenRouter returned HTTP {error.code}: {error.read().decode()[:500]}")
    except urllib.error.URLError as error:
        raise SystemExit(f"Could not reach OpenRouter: {error.reason}")
    return body["choices"][0]["message"]["content"]


def ask_isolated_claude(model: str, messages: list[dict]) -> str:
    """Run a fresh, stateless Claude Code session that sees only the payload.

    Isolation comes from the flags and the folder: an empty temp directory
    (no project files, no project memory, a path without the user's name),
    no tools, no settings sources, an explicitly empty MCP config, our own
    system prompt in place of the default, and no session persistence.
    """
    import tempfile
    system_prompt, user_content = messages[0]["content"], messages[1]["content"]
    command = ["claude", "-p", "--model", model, "--tools", "", "--setting-sources", "",
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--no-session-persistence", "--system-prompt", system_prompt]
    with tempfile.TemporaryDirectory(prefix="relay-review-") as empty_folder:
        try:
            completed = subprocess.run(command, input=user_content, cwd=empty_folder,
                                       capture_output=True, text=True,
                                       timeout=CLAUDE_TIMEOUT_SECONDS)
        except FileNotFoundError:
            raise SystemExit("The claude CLI is not installed or not on PATH.")
        except subprocess.TimeoutExpired:
            raise SystemExit(f"The isolated Claude review took over {CLAUDE_TIMEOUT_SECONDS}s.")
    if completed.returncode != 0:
        raise SystemExit(f"The isolated Claude review failed: {completed.stderr.strip()[:500]}")
    return completed.stdout.strip()


def save_review(target: Path, model: str, focus: str | None, review: str) -> Path:
    REVIEWS_DIR.mkdir(exist_ok=True)
    model_slug = model.replace("/", "_").replace(":", "-")
    path = REVIEWS_DIR / f"{datetime.now():%Y%m%d-%H%M}-{target.stem}-{model_slug}.md"
    path.write_text(
        f"# Review of {target.relative_to(REPO_ROOT) if target.is_relative_to(REPO_ROOT) else target}\n\n"
        f"- Reviewer: `{model}`\n"
        f"- Date: {datetime.now():%Y-%m-%d %H:%M}\n"
        f"- Context sent: context/PROJECT.md, context/DECISIONS.md, generated state, the document "
        f"(stateless reviewer, nothing else)\n"
        f"- Focus: {focus or '(none)'}\n\n---\n\n{review}\n"
    )
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded cross-AI review with project context.")
    parser.add_argument("target", type=Path, help="document to review")
    parser.add_argument("--engine", choices=["claude", "openrouter"], default="claude",
                        help="who reviews: an isolated Claude session (default) or an OpenRouter model")
    parser.add_argument("--model", help=f"model id (defaults: claude={DEFAULT_CLAUDE_MODEL}, "
                                        f"openrouter={DEFAULT_OPENROUTER_MODEL})")
    parser.add_argument("--focus", help="a specific question for the reviewer")
    parser.add_argument("--dry-run", action="store_true", help="print what would be sent; send nothing")
    args = parser.parse_args()

    target = args.target.resolve()
    if not target.is_file():
        print(f"Error: {args.target} is not a file")
        return 1

    messages = build_messages(target, args.focus)
    payload_text = "\n".join(message["content"] for message in messages)
    secrets = find_secrets(payload_text)
    if secrets:
        print(f"Refusing to send: the payload looks like it contains {', '.join(secrets)}.")
        return 1

    default_model = DEFAULT_CLAUDE_MODEL if args.engine == "claude" else DEFAULT_OPENROUTER_MODEL
    model = args.model or default_model
    reviewer_label = f"{args.engine}:{model}"

    if args.dry_run:
        print(payload_text)
        print(f"\n[dry run: {len(payload_text):,} characters would go to {reviewer_label}]")
        return 0

    if args.engine == "claude":
        review = ask_isolated_claude(model, messages)
    else:
        api_key = read_api_key()
        if not api_key:
            print("Error: no API key. Set OPENROUTER_API_KEY or add the Keychain item "
                  f"'{KEYCHAIN_SERVICE}' (see the top of this file).")
            return 1
        review = ask_model(model, messages, api_key)
    saved_to = save_review(target, reviewer_label, args.focus, review)
    print(review)
    print(f"\nSaved: {saved_to.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
