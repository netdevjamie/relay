"""Tests for tools/review_prompt.py. No network: nothing is ever sent."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review_prompt


class TestSecretScan:
    def test_clean_text_has_no_secrets(self):
        assert review_prompt.find_secrets("A plain role prompt about reviewing code.") == []

    def test_each_kind_of_secret_is_caught(self):
        samples = {
            "OpenAI/OpenRouter-style key": "key sk-or-v1-" + "a" * 40,
            "GitHub token": "ghp_" + "A" * 36,
            "AWS access key": "AKIA" + "ABCDEFGHIJKLMNOP",
            "private key block": "-----BEGIN RSA PRIVATE KEY-----",
            "assigned password or secret": "password = hunter2hunter2",
        }
        for expected_name, text in samples.items():
            assert expected_name in review_prompt.find_secrets(text), expected_name

    def test_secret_names_are_reported_never_the_values(self):
        found = review_prompt.find_secrets("token: abcdefgh12345678")
        assert all("abcdefgh" not in name for name in found)


class TestMessages:
    def test_payload_carries_context_and_document(self, tmp_path, monkeypatch):
        context = tmp_path / "context"
        context.mkdir()
        (context / "PROJECT.md").write_text("PROJECT OVERVIEW TEXT")
        (context / "DECISIONS.md").write_text("DECISION LOG TEXT")
        target = tmp_path / "reviewer.md"
        target.write_text("ROLE PROMPT UNDER REVIEW")
        monkeypatch.setattr(review_prompt, "CONTEXT_DIR", context)
        monkeypatch.setattr(review_prompt, "build_state_summary", lambda: "STATE TEXT")

        system, user = review_prompt.build_messages(target, focus="is it clear?")

        assert "AT MOST three issues" in system["content"]
        for expected in ("PROJECT OVERVIEW TEXT", "DECISION LOG TEXT", "STATE TEXT",
                         "ROLE PROMPT UNDER REVIEW", "is it clear?"):
            assert expected in user["content"]

    def test_refuses_to_send_a_payload_with_a_secret(self, tmp_path, monkeypatch, capsys):
        context = tmp_path / "context"
        context.mkdir()
        (context / "PROJECT.md").write_text("overview")
        (context / "DECISIONS.md").write_text("decisions")
        target = tmp_path / "leaky.md"
        target.write_text("export OPENROUTER_API_KEY=sk-or-v1-" + "b" * 40)
        monkeypatch.setattr(review_prompt, "CONTEXT_DIR", context)
        monkeypatch.setattr(review_prompt, "build_state_summary", lambda: "state")
        monkeypatch.setattr(review_prompt, "ask_model",
                            lambda *args: (_ for _ in ()).throw(AssertionError("must not send")))
        monkeypatch.setattr(sys, "argv", ["review_prompt.py", str(target)])

        assert review_prompt.main() == 1
        assert "Refusing to send" in capsys.readouterr().out


class TestIsolatedClaude:
    def test_session_is_stateless_and_toolless(self, monkeypatch):
        captured = {}

        class Completed:
            returncode, stdout, stderr = 0, "## Verdict\nNo changes needed.", ""

        def fake_run(command, **kwargs):
            captured["command"], captured["kwargs"] = command, kwargs
            return Completed()

        monkeypatch.setattr(review_prompt.subprocess, "run", fake_run)
        review = review_prompt.ask_isolated_claude(
            "sonnet", [{"role": "system", "content": "SYS"}, {"role": "user", "content": "PAYLOAD"}])

        command = captured["command"]
        assert review.startswith("## Verdict")
        assert command[command.index("--tools") + 1] == ""
        assert command[command.index("--setting-sources") + 1] == ""
        assert "--strict-mcp-config" in command and "--no-session-persistence" in command
        assert command[command.index("--system-prompt") + 1] == "SYS"
        assert captured["kwargs"]["input"] == "PAYLOAD"
        assert "relay-review-" in captured["kwargs"]["cwd"]
