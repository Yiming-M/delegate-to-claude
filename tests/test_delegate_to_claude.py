from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "delegate_to_claude.py"
)


def json_lines(*events: dict[str, object]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def assistant_event(
    model: str,
    *,
    text: str = "",
    stop_reason: str = "end_turn",
    stop_details: dict[str, object] | None = None,
    fallback: tuple[str, str] | None = None,
) -> dict[str, object]:
    content: list[dict[str, object]] = []
    if fallback:
        content.append(
            {
                "type": "fallback",
                "from": {"model": fallback[0]},
                "to": {"model": fallback[1]},
            }
        )
    if text:
        content.append({"type": "text", "text": text})
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_details": stop_details,
        },
    }


def result_event(
    text: str,
    *,
    model_usage: dict[str, object],
    stop_reason: str = "end_turn",
    stop_details: dict[str, object] | None = None,
    subtype: str = "success",
    is_error: bool = False,
) -> dict[str, object]:
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": text,
        "stop_reason": stop_reason,
        "stop_details": stop_details,
        "modelUsage": model_usage,
    }


class DelegateToClaudeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.fake_claude = self.root / "claude"
        self.fake_claude.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                from pathlib import Path
                import sys

                record = os.environ.get("FAKE_CLAUDE_RECORD")
                if record:
                    Path(record).write_text(
                        json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}),
                        encoding="utf-8",
                    )
                else:
                    sys.stdin.read()

                sys.stdout.write(os.environ.get("FAKE_CLAUDE_STDOUT", ""))
                sys.stdout.flush()
                import time
                time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
                sys.stderr.write(os.environ.get("FAKE_CLAUDE_STDERR", ""))
                raise SystemExit(int(os.environ.get("FAKE_CLAUDE_RETURNCODE", "0")))
                """
            ),
            encoding="utf-8",
        )
        self.fake_claude.chmod(0o755)

    def run_helper(
        self,
        stdout: str,
        *,
        model: str | None = "fable",
        effort: str | None = "medium",
        returncode: int = 0,
        stderr: str = "",
        sleep: float = 0,
        extra_args: list[str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
        transcript = self.root / "transcript.json"
        output = self.root / "answer.txt"
        record = self.root / "fake-record.json"
        command = [
            sys.executable,
            str(SCRIPT),
            "--prompt",
            "Evaluate the supplied prompt.",
            "--cwd",
            str(self.root),
            "--out",
            str(output),
            "--transcript",
            str(transcript),
        ]
        if model is not None:
            command.extend(["--model", model])
        if effort is not None:
            command.extend(["--effort", effort])
        if extra_args:
            command.extend(extra_args)

        env = os.environ.copy()
        env["PATH"] = str(self.root) + os.pathsep + env.get("PATH", "")
        env["FAKE_CLAUDE_STDOUT"] = stdout
        env["FAKE_CLAUDE_STDERR"] = stderr
        env["FAKE_CLAUDE_RETURNCODE"] = str(returncode)
        env["FAKE_CLAUDE_RECORD"] = str(record)
        env["FAKE_CLAUDE_SLEEP"] = str(sleep)
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        return result, transcript, output, record

    def test_normal_fable_response_preserves_text_and_audits_model(self) -> None:
        stream = json_lines(
            assistant_event("claude-fable-5", text="Fable answer"),
            result_event(
                "Fable answer",
                model_usage={"claude-fable-5": {"inputTokens": 10}},
            ),
        )
        result, transcript_path, output_path, record_path = self.run_helper(
            stream
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "Fable answer\n")
        self.assertIn(
            "requested=fable served=claude-fable-5 fallback=no",
            result.stderr,
        )
        self.assertEqual(output_path.read_text(encoding="utf-8"), "Fable answer")
        for private in (transcript_path, output_path):
            self.assertEqual(private.stat().st_mode & 0o777, 0o600)

        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        self.assertEqual(transcript["returncode"], 0)
        self.assertEqual(transcript["effort"], "medium")
        self.assertEqual(
            transcript["model_audit"]["final_served_model"],
            "claude-fable-5",
        )
        self.assertFalse(transcript["model_audit"]["fallback_detected"])

        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertIn("stream-json", record["argv"])
        self.assertIn("--verbose", record["argv"])
        self.assertNotIn("--fallback-model", record["argv"])

    def test_normal_opus_response_matches_alias(self) -> None:
        stream = json_lines(
            assistant_event("claude-opus-5", text="Opus answer"),
            result_event(
                "Opus answer",
                model_usage={"claude-opus-5": {"inputTokens": 10}},
            ),
        )
        result, _, _, _ = self.run_helper(stream, model="opus")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "Opus answer\n")
        self.assertIn("fallback=no", result.stderr)

    def test_refusal_pauses_without_writing_empty_answer(self) -> None:
        details = {
            "type": "refusal",
            "category": "frontier_llm",
            "explanation": "This request was declined.",
        }
        stream = json_lines(
            assistant_event(
                "claude-fable-5",
                text="Earlier completed turn",
            ),
            assistant_event(
                "claude-fable-5",
                stop_reason="refusal",
                stop_details=details,
            ),
            result_event(
                "",
                model_usage={"claude-fable-5": {"inputTokens": 10}},
                stop_reason="refusal",
                stop_details=details,
                subtype="error_during_execution",
                is_error=True,
            ),
        )
        result, transcript_path, output_path, _ = self.run_helper(stream)

        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertFalse(output_path.exists())
        self.assertIn("paused: Claude safety refusal", result.stderr)
        self.assertIn("frontier_llm", result.stderr)
        self.assertNotIn("fallback detected", result.stderr)

        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        self.assertEqual(transcript["returncode"], 3)
        self.assertEqual(
            transcript["model_audit"]["stop_details"]["category"],
            "frontier_llm",
        )

    def test_unexpected_fallback_is_labeled_and_answer_is_preserved(self) -> None:
        stream = json_lines(
            assistant_event(
                "claude-opus-4-8",
                text="Fallback answer",
                fallback=("claude-fable-5", "claude-opus-4-8"),
            ),
            result_event(
                "Fallback answer",
                model_usage={
                    "claude-fable-5": {"inputTokens": 10},
                    "claude-opus-4-8": {"inputTokens": 10},
                },
            ),
        )
        result, transcript_path, output_path, _ = self.run_helper(stream)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "Fallback answer\n")
        self.assertIn("Claude model fallback detected", result.stderr)
        self.assertIn("served=claude-opus-4-8", result.stderr)
        self.assertEqual(
            output_path.read_text(encoding="utf-8"),
            "Fallback answer",
        )

        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        audit = transcript["model_audit"]
        self.assertTrue(audit["fallback_detected"])
        self.assertEqual(
            audit["fallback_hops"],
            [{"from": "claude-fable-5", "to": "claude-opus-4-8"}],
        )

    def test_malformed_stream_fails_explicitly(self) -> None:
        result, transcript_path, output_path, _ = self.run_helper(
            "not-json\n"
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertFalse(output_path.exists())
        self.assertIn("unable to parse Claude stream-json output", result.stderr)

        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        self.assertIn("invalid JSON", transcript["parse_error"])

    def test_missing_result_event_fails_explicitly(self) -> None:
        stream = json_lines(
            assistant_event("claude-fable-5", text="Partial answer")
        )
        result, _, output_path, _ = self.run_helper(stream)

        self.assertEqual(result.returncode, 1)
        self.assertFalse(output_path.exists())
        self.assertIn("no final result event", result.stderr)

    def test_nonzero_claude_exit_is_preserved(self) -> None:
        stream = json_lines(
            assistant_event("claude-fable-5", text="Untrusted answer"),
            result_event(
                "Untrusted answer",
                model_usage={"claude-fable-5": {}},
            ),
        )
        result, transcript_path, output_path, _ = self.run_helper(
            stream,
            returncode=7,
            stderr="fake failure\n",
        )

        self.assertEqual(result.returncode, 7)
        self.assertFalse(output_path.exists())
        self.assertIn("fake failure", result.stderr)
        self.assertIn("CLI exit=7", result.stderr)

        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        self.assertEqual(transcript["returncode"], 7)
        self.assertEqual(transcript["claude_returncode"], 7)

    def test_nonzero_json_error_is_visible_without_synthetic_fallback(self) -> None:
        stream = json_lines(
            assistant_event("<synthetic>"),
            result_event(
                "Failed to authenticate. API Error: 401.",
                model_usage={},
                stop_reason="stop_sequence",
                subtype="error_during_execution",
                is_error=True,
            ),
        )
        result, transcript_path, output_path, _ = self.run_helper(
            stream,
            returncode=1,
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertFalse(output_path.exists())
        self.assertIn(
            "Claude error/partial response: Failed to authenticate. API Error: 401.",
            result.stderr,
        )

        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        audit = transcript["model_audit"]
        self.assertEqual(audit["served_models"], [])
        self.assertIsNone(audit["final_served_model"])
        self.assertIsNone(audit["fallback_detected"])

    def test_dry_run_keeps_tools_and_never_enables_fallback(self) -> None:
        command = [
            sys.executable,
            str(SCRIPT),
            "--dry-run",
            "--prompt",
            "Dry run only.",
            "--cwd",
            str(self.root),
        ]
        env = os.environ.copy()
        env["PATH"] = str(self.root) + os.pathsep + env.get("PATH", "")
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        preview = json.loads(result.stdout)
        self.assertNotIn("--tools", preview["command"])
        self.assertNotIn("--no-session-persistence", preview["command"])
        self.assertEqual(preview["command"][0], str(self.fake_claude.resolve()))
        self.assertIn("stream-json", preview["command"])
        self.assertNotIn("--fallback-model", preview["command"])

    def test_unspecified_model_and_effort_leave_cli_defaults_and_tools_enabled(self) -> None:
        stream = json_lines(assistant_event("claude-opus-5", text="Done"),
                            result_event("Done", model_usage={"claude-opus-5": {}}))
        result, transcript, _, record = self.run_helper(stream, model=None, effort=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(record.read_text())["argv"]
        for flag in ("--model", "--effort", "--tools", "--no-session-persistence",
                     "--dangerously-skip-permissions", "--continue"):
            self.assertNotIn(flag, argv)
        self.assertIsNone(json.loads(transcript.read_text())["model_audit"]["fallback_detected"])
        self.assertIn("effective unknown", result.stderr)

    def test_session_id_and_tool_events_are_retained(self) -> None:
        session = "1f84af4a-e50c-44ec-9801-62b431543e7b"
        tool = assistant_event("claude-fable-5")
        tool["message"]["content"] = [{"type": "tool_use", "id": "tool1", "name": "Read",
                                      "input": {"file_path": "sample.txt"}}]
        stream = json_lines({"type": "system", "subtype": "init", "session_id": session}, tool,
                            assistant_event("claude-fable-5", text="Found it"),
                            result_event("Found it", model_usage={}))
        result, transcript, _, _ = self.run_helper(stream)
        self.assertEqual(result.returncode, 0)
        report = json.loads(transcript.read_text())
        self.assertEqual(report["session_id"], session)
        self.assertEqual(report["raw_stdout"], stream)

    def test_resume_uses_explicit_id_and_current_permissions(self) -> None:
        session = "1f84af4a-e50c-44ec-9801-62b431543e7b"
        stream = json_lines(result_event("Revised", model_usage={}))
        result, transcript, _, record = self.run_helper(stream, extra_args=[
            "--resume", session, "--permission-mode", "acceptEdits",
            "--tools", "Read,Edit,Bash", "--allowed-tools", "Bash(python3 -m unittest *)",
            "--disallowed-tools", "mcp__*", "--add-dir", str(self.root)])
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(record.read_text())["argv"]
        for flag, value in (("--resume", session), ("--permission-mode", "acceptEdits"),
                            ("--tools", "Read,Edit,Bash"),
                            ("--allowed-tools", "Bash(python3 -m unittest *)"),
                            ("--disallowed-tools", "mcp__*")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertEqual(json.loads(transcript.read_text())["resumed_from"], session)

    def test_permission_denial_preserves_partial_result_without_success(self) -> None:
        event = result_event("Could not run tests", model_usage={})
        event["permission_denials"] = [{"tool_name": "Bash", "tool_input": {"command": "pytest"}}]
        result, transcript, output, _ = self.run_helper(json_lines(event))
        self.assertEqual(result.returncode, 4)
        self.assertFalse(output.exists())
        report = json.loads(transcript.read_text())
        self.assertTrue(report["permission_denials"])
        self.assertEqual(report["stdout"], "Could not run tests")
        self.assertIn("do not claim full completion", result.stderr)

    def test_api_errors_are_visible_even_when_process_exits_zero(self) -> None:
        event = result_event("", model_usage={}, subtype="error_during_execution", is_error=True)
        event["errors"] = ["API Error: 429 rate limit"]
        result, transcript, output, _ = self.run_helper(json_lines(event))
        self.assertEqual(result.returncode, 1)
        self.assertFalse(output.exists())
        self.assertIn("429 rate limit", result.stderr)
        self.assertEqual(json.loads(transcript.read_text())["errors"], event["errors"])

    def test_subagent_model_does_not_replace_parent_model(self) -> None:
        subagent = assistant_event("claude-sonnet-5", text="Child result")
        subagent["parent_tool_use_id"] = "child1"
        stream = json_lines(assistant_event("claude-opus-5", text="Parent result"), subagent,
                            result_event("Parent result", model_usage={"claude-opus-5": {}, "claude-sonnet-5": {}}))
        result, transcript, _, _ = self.run_helper(stream, model="opus")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(transcript.read_text())["model_audit"]["final_served_model"], "claude-opus-5")

    def test_invalid_session_id_does_not_start_claude(self) -> None:
        result, _, _, record = self.run_helper("", extra_args=["--resume", "most-recent"])
        self.assertEqual(result.returncode, 2)
        self.assertFalse(record.exists())

    def test_incomplete_stream_retains_session_for_deliberate_recovery(self) -> None:
        session = "1f84af4a-e50c-44ec-9801-62b431543e7b"
        stream = json_lines({"type": "system", "session_id": session},
                            assistant_event("claude-fable-5", text="Partial"))
        result, transcript, output, _ = self.run_helper(stream)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(output.exists())
        report = json.loads(transcript.read_text())
        self.assertEqual(report["session_id"], session)
        self.assertEqual(report["raw_stdout"], stream)

    def test_default_model_audit_uses_observed_initial_model(self) -> None:
        for served, expected_fallback in (("claude-opus-5", False), ("claude-sonnet-5", True)):
            with self.subTest(served=served):
                stream = json_lines(
                    {"type": "system", "subtype": "init", "model": "claude-opus-5[1m]"},
                    assistant_event(served, text="Done"), result_event("Done", model_usage={}))
                result, transcript, _, _ = self.run_helper(stream, model=None, effort=None)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(json.loads(transcript.read_text())["model_audit"]["fallback_detected"],
                                 expected_fallback)

    def test_timeout_retains_partial_stream_and_session(self) -> None:
        session = "1f84af4a-e50c-44ec-9801-62b431543e7b"
        stream = json_lines({"type": "system", "session_id": session})
        result, transcript, output, _ = self.run_helper(stream, sleep=30, extra_args=["--timeout", "1"])
        self.assertEqual(result.returncode, 124)
        self.assertFalse(output.exists())
        report = json.loads(transcript.read_text())
        self.assertEqual(report["session_id"], session)
        self.assertEqual(report["raw_stdout"], stream)
        self.assertIn("timed out", result.stderr)


if __name__ == "__main__":
    unittest.main()
