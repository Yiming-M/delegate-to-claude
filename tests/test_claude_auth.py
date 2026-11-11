from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

keys = load("claude_api_key")
routes = load("claude_auth_routes")
runner = load("delegate_to_claude")
SESSION = "1f84af4a-e50c-44ec-9801-62b431543e7b"

def stream(message, error=True, session=SESSION):
    return json.dumps({"type": "result", "subtype": "error_during_execution" if error else "success",
                       "is_error": error, "result": message, "session_id": session}) + "\n"

class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / ".config/delegate-to-claude/keys/default"
        self.fake = "test-only-nonsecret-value-123456"

    def test_hidden_input_normalization_and_private_storage(self):
        self.assertEqual(keys.normalize_input('export ANTHROPIC_API_KEY="' + self.fake + '"'), self.fake)
        keys.store_key(self.path, self.fake)
        self.assertEqual(keys.read_key(self.path), self.fake)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            keys.read_key(self.path)

    def test_refuses_oauth_and_symlinks(self):
        with self.assertRaises(ValueError):
            keys.validate_key("sk-ant-oat-test-only-123456789")
        keys.store_key(self.path, self.fake)
        self.path.unlink()
        self.path.symlink_to(self.home / "missing")
        with self.assertRaises(ValueError):
            keys.store_key(self.path, self.fake)

    def test_named_api_keys_use_separate_private_files(self):
        with patch.object(Path, "home", return_value=self.home):
            first = keys.key_profile_path("work")
            second = keys.key_profile_path("backup")
            keys.store_key(first, self.fake)
            keys.store_key(second, self.fake + "-other")
            self.assertEqual(keys.read_key(first), self.fake)
            self.assertEqual(keys.read_key(second), self.fake + "-other")
            with self.assertRaises(ValueError):
                keys.key_profile_path("../escape")


class OrderedBillingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / "routes.json"
        self.source_names = ["sub1", "api1", "sub2", "api2"]
        self.document = {"order": self.source_names, "sources": {
            "sub1": {"type": "subscription", "config_dir": str(self.root / "first")},
            "api1": {"type": "api", "key_profile": "first-key", "config_dir": str(self.root / "first")},
            "sub2": {"type": "subscription", "config_dir": str(self.root / "second")},
            "api2": {"type": "api", "key_profile": "second-key", "config_dir": str(self.root / "second")},
        }}
        self.args = argparse.Namespace(timeout=10, cli="/fake/claude", model="opus", effort="high",
            resume=None, tools="Read,Edit", permission_mode="acceptEdits", allowed_tools=["Read"],
            disallowed_tools=["Bash"], add_dir=[])

    @staticmethod
    def effective_method(cli, settings, env, cwd, timeout):
        return "api_key_helper" if json.loads(settings[-1])["apiKeyHelper"] else "claude.ai"

    def read_config(self):
        self.config.write_text(json.dumps(self.document))
        sources, order = routes.load_auth_config(self.config)
        return [sources[name] for name in order], list(sources.values())

    def test_four_sources_resume_same_session_across_directories(self):
        ordered, sources = self.read_config()
        calls = []
        quota = "You've hit your session limit · resets 4pm"
        messages = [quota, "Credit balance too low", quota, "done"]
        transcripts = [self.root / config / "projects/project" / f"{SESSION}.jsonl"
                       for config in ("first", "second")]

        def run(command, prompt, cwd, timeout, *, env):
            index = len(calls)
            calls.append((command, prompt, env))
            transcript = transcripts[index // 2]
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.touch()
            os.utime(transcript, (100 + index, 100 + index))
            init = {"type": "system", "subtype": "init", "apiKeySource": "apiKeyHelper" if index % 2 else "none"}
            return int(index < 3), json.dumps(init) + "\n" + stream(messages[index], index < 3), ""

        with patch.object(runner, "effective_auth_method", side_effect=self.effective_method), \
                patch.object(runner, "run_claude", side_effect=run), patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "test-only-ignored", "CLAUDE_CODE_OAUTH_TOKEN": "test-only-ignored",
        }):
            code, _, _, audit = runner.run_ordered_auth(self.args, ordered, sources, "Original mutation", self.root)
        self.assertEqual(code, 0)
        self.assertEqual([step["auth"] for step in audit["attempts"]], self.source_names)
        self.assertEqual([step["session_id"] for step in audit["attempts"]], [SESSION] * 4)
        self.assertEqual([step["api_key_source"] for step in audit["attempts"]], ["none", "apiKeyHelper"] * 2)
        self.assertEqual([step["auth_method"] for step in audit["attempts"]], ["claude.ai", "api_key_helper"] * 2)
        self.assertEqual([step.get("fallback_reason") for step in audit["attempts"]], [
            "subscription_quota_exhausted", "api_balance_exhausted", "subscription_quota_exhausted", None])
        self.assertNotIn("--resume", calls[0][0])
        for index, (command, prompt, env) in enumerate(calls):
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(transcripts[index // 2].parents[2]))
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertEqual(command[command.index("--model") + 1], "opus")
            self.assertEqual(command[command.index("--effort") + 1], "high")
            self.assertEqual(command[command.index("--permission-mode") + 1], "acceptEdits")
            if index:
                self.assertEqual(command[command.index("--resume") + 1], str(transcripts[(index - 1) // 2]))
                self.assertEqual(prompt, runner.CONTINUE_AFTER_QUOTA)
        self.assertIn("--profile first-key --emit", json.loads(calls[1][0][-1])["apiKeyHelper"])
        self.assertIn("--profile second-key --emit", json.loads(calls[3][0][-1])["apiKeyHelper"])
        self.assertEqual(json.loads(calls[2][0][-1])["apiKeyHelper"], "")

    def test_order_can_start_with_api_and_stops_when_session_is_missing(self):
        self.document["order"] = ["api1", "sub2", "api2"]
        ordered, sources = self.read_config()
        with patch.object(runner, "effective_auth_method", side_effect=self.effective_method), \
                patch.object(runner, "run_claude", return_value=(1, stream("Credit balance too low"), "")) as call:
            _, _, _, audit = runner.run_ordered_auth(self.args, ordered, sources, "Original mutation", self.root)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(audit["initial"], "api1")
        self.assertEqual(audit["reason"], "api_balance_exhausted_without_resumable_session")

    def test_wrong_credential_stops_before_model_request(self):
        ordered, sources = self.read_config()
        with patch.object(runner, "effective_auth_method", return_value="api_key_helper"), \
                patch.object(runner, "run_claude") as call:
            code, _, _, audit = runner.run_ordered_auth(self.args, ordered, sources, "Task", self.root)
        call.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(audit["reason"], "auth_source_mismatch")

    def test_rejects_config_directory_under_skill_checkout(self):
        self.document["sources"]["sub1"]["config_dir"] = str(SCRIPTS.parent / "example-credentials")
        with self.assertRaisesRegex(ValueError, "outside the skill checkout"):
            self.read_config()

if __name__ == "__main__":
    unittest.main()
