#!/usr/bin/env python3
"""Delegate a task to an authenticated Claude CLI and resume it by session ID."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from copy import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from claude_auth_routes import AuthSource, effective_auth_method, load_auth_config, session_transcript

CONTINUE_AFTER_QUOTA = (
    "The previous attempt stopped because its usage allowance or API credit balance was exhausted. "
    "Continue the existing task using this session's instructions and tool history. "
    "Inspect any partial changes or command results before continuing; do not repeat "
    "completed or potentially non-idempotent actions. Keep the existing scope and constraints."
)


@dataclass(frozen=True)
class ParsedClaudeOutput:
    result_text: str
    result_subtype: str | None
    is_error: bool
    stop_reason: str | None
    stop_details: dict[str, Any] | None
    model_usage: dict[str, Any]
    served_models: list[str]
    final_served_model: str | None
    fallback_hops: list[dict[str, str | None]]
    initial_model: str | None
    session_id: str | None
    permission_denials: list[Any]
    errors: list[Any]
    rate_limit_info: dict[str, Any] | None
    api_key_source: str | None


def build_command(args: argparse.Namespace) -> list[str]:
    command = [
        str(Path(args.cli).expanduser().resolve()),
        "--print", "--input-format", "text", "--output-format", "stream-json",
        "--verbose", "--permission-mode", args.permission_mode,
        "--permission-prompts", "none",
    ]
    for name in ("model", "effort", "resume", "tools"):
        value = getattr(args, name)
        if value is not None:
            command.extend(["--" + name, value])
    for name in ("allowed_tools", "disallowed_tools", "add_dir"):
        for value in getattr(args, name):
            command.extend(["--" + name.replace("_", "-"), value])
    return command


def _append_unique(values: list[str], value: Any) -> None:
    if (
        isinstance(value, str)
        and value
        and value != "<synthetic>"
        and value not in values
    ):
        values.append(value)


def _block_model(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        model = value.get("model")
        return model if isinstance(model, str) else None
    return None


def parse_claude_stream(stdout: str) -> ParsedClaudeOutput:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"stream-json line {line_number} is invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(event, dict):
            raise ValueError(f"stream-json line {line_number} is not a JSON object")
        events.append(event)

    if not events:
        raise ValueError("Claude returned an empty stream-json response")

    result_events = [event for event in events if event.get("type") == "result"]
    if not result_events:
        raise ValueError("Claude stream-json response has no final result event")
    result_event = result_events[-1]

    raw_result_text = result_event.get("result", "")
    if raw_result_text is None:
        result_text = ""
    elif isinstance(raw_result_text, str):
        result_text = raw_result_text
    else:
        raise ValueError("Claude final result field is not text")

    assistant_messages: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        if isinstance(message, dict) and not event.get("parent_tool_use_id"):
            assistant_messages.append(message)

    stop_reason = result_event.get("stop_reason")
    stop_details = result_event.get("stop_details")
    assistant_stop_reason: str | None = None
    assistant_stop_details: dict[str, Any] | None = None
    for message in assistant_messages:
        message_stop_reason = message.get("stop_reason")
        if isinstance(message_stop_reason, str):
            assistant_stop_reason = message_stop_reason
        if (
            message_stop_reason == "refusal"
            and isinstance(message.get("stop_details"), dict)
        ):
            assistant_stop_details = message["stop_details"]
    if stop_reason is None:
        stop_reason = assistant_stop_reason
    if stop_details is None and stop_reason == "refusal":
        stop_details = assistant_stop_details

    raw_model_usage = result_event.get("modelUsage")
    if raw_model_usage is None:
        raw_model_usage = result_event.get("model_usage")
    model_usage = raw_model_usage if isinstance(raw_model_usage, dict) else {}

    served_models: list[str] = []
    fallback_hops: list[dict[str, str | None]] = []
    for message in assistant_messages:
        _append_unique(served_models, message.get("model"))
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "fallback":
                continue
            fallback_hops.append(
                {
                    "from": _block_model(block.get("from")),
                    "to": _block_model(block.get("to")),
                }
            )

    _append_unique(served_models, result_event.get("model"))
    for model in model_usage:
        _append_unique(served_models, model)

    final_served_model: str | None = None
    for message in assistant_messages:
        model = message.get("model")
        if isinstance(model, str) and model and model != "<synthetic>":
            final_served_model = model
    result_model = result_event.get("model")
    if (
        isinstance(result_model, str)
        and result_model
        and result_model != "<synthetic>"
    ):
        final_served_model = result_model

    return ParsedClaudeOutput(
        result_text=result_text,
        result_subtype=(
            result_event.get("subtype")
            if isinstance(result_event.get("subtype"), str)
            else None
        ),
        is_error=bool(result_event.get("is_error", False)),
        stop_reason=stop_reason if isinstance(stop_reason, str) else None,
        stop_details=stop_details if isinstance(stop_details, dict) else None,
        model_usage=model_usage,
        served_models=served_models,
        final_served_model=final_served_model,
        fallback_hops=fallback_hops,
        initial_model=next((e["model"] for e in events
                            if e.get("type") == "system" and e.get("subtype") == "init"
                            and isinstance(e.get("model"), str)), None),
        session_id=next((e["session_id"] for e in reversed(events) if e.get("session_id")), None),
        permission_denials=result_event.get("permission_denials") or [
            e for e in events if e.get("subtype") == "permission_denied"
        ],
        errors=result_event.get("errors") or [],
        rate_limit_info=next((e["rate_limit_info"] for e in reversed(events)
                              if e.get("type") == "rate_limit_event"
                              and not e.get("parent_tool_use_id")
                              and isinstance(e.get("rate_limit_info"), dict)), None),
        api_key_source=next((e["apiKeySource"] for e in events if e.get("type") == "system"
                             and e.get("subtype") == "init"
                             and isinstance(e.get("apiKeySource"), str)), None),
    )


def _canonical_model_name(model: str) -> str:
    return re.sub(r"\[[^\]]+\]$", "", model.strip().lower())


def requested_model_matches(requested: str | None, served: str | None) -> bool:
    if not requested or not served:
        return True
    requested_name = _canonical_model_name(requested)
    served_name = _canonical_model_name(served)
    if requested_name in {"fable", "opus", "sonnet", "haiku"}:
        return served_name == requested_name or served_name.startswith(
            f"claude-{requested_name}-"
        )
    return requested_name == served_name


def fallback_detected(requested_model: str | None, parsed: ParsedClaudeOutput) -> bool | None:
    if parsed.fallback_hops:
        return True
    expected_model = requested_model or parsed.initial_model
    if not expected_model or not parsed.final_served_model:
        return None
    return not requested_model_matches(expected_model, parsed.final_served_model)


def model_audit(requested_model: str | None, parsed: ParsedClaudeOutput) -> dict[str, Any]:
    return {
        "requested_model": requested_model,
        "initial_model": parsed.initial_model,
        "served_models": parsed.served_models,
        "final_served_model": parsed.final_served_model,
        "fallback_detected": fallback_detected(requested_model, parsed),
        "fallback_hops": parsed.fallback_hops,
        "stop_reason": parsed.stop_reason,
        "stop_details": parsed.stop_details,
        "model_usage": parsed.model_usage,
    }


def write_text(path_value: str | None, text: str) -> None:
    if path_value:
        path = Path(path_value).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Prompts, responses, and tool output stay readable by the owner only.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)


def run_claude(command: list[str], prompt: str, cwd: Path, timeout: int, *,
               env: dict[str, str] | None = None) -> tuple[int, str, str]:
    # A tool command may outlive the CLI. Terminate the process group on timeout.
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, cwd=cwd,
                          start_new_session=True, env=env) as process:
        try:
            stdout, stderr = process.communicate(prompt, timeout=timeout)
            return process.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            return 124, stdout, stderr + f"\nClaude timed out after {timeout}s."
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise


def subscription_exhausted(parsed: ParsedClaudeOutput) -> bool:
    # A warning or ordinary server 429 must not spend API credits. Require a
    # failed turn plus a rejected subscription window or the CLI's quota message.
    if not parsed.is_error or parsed.stop_reason == "refusal":
        return False
    info = parsed.rate_limit_info or {}
    window = info.get("rateLimitType", "")
    if (info.get("status") == "rejected" and isinstance(window, str)
            and (window == "five_hour" or window == "seven_day" or window.startswith("seven_day_"))):
        return True
    message = parsed.result_text + " " + json.dumps(parsed.errors, ensure_ascii=False)
    return bool(re.search(r"you(?:['’]ve| have) hit your (?:(?:session|weekly|opus|sonnet) )?limit\b.*?\bresets\b",
                          message, re.IGNORECASE | re.DOTALL))


def api_balance_exhausted(parsed: ParsedClaudeOutput) -> bool:
    if not parsed.is_error or parsed.stop_reason == "refusal":
        return False
    message = parsed.result_text + " " + json.dumps(parsed.errors, ensure_ascii=False)
    return bool(re.search(r"\bcredit balance (?:is )?too low\b", message, re.IGNORECASE))


def run_ordered_auth(args: argparse.Namespace, routes: list[AuthSource], sources: list[AuthSource],
                     prompt: str, cwd: Path) -> tuple[int, str, str, dict[str, Any]]:
    session_id = args.resume
    audit: dict[str, Any] = {"requested": [source.name for source in routes], "initial": routes[0].name,
                             "final": routes[0].name, "fallback": False, "reason": None, "attempts": []}
    deadline = time.monotonic() + args.timeout
    stdout, stderr = "", ""
    for index, source in enumerate(routes):
        remaining = max(1, int(deadline - time.monotonic()))
        settings, env = source.launch()
        attempt_args = copy(args)
        transcript = session_transcript(session_id, sources) if session_id else None
        attempt_args.resume = str(transcript) if transcript else session_id
        command = build_command(attempt_args) + settings
        audit.update(final=source.name, fallback=index > 0)
        attempt: dict[str, Any] = {"auth": source.name, "auth_type": source.kind, "command": command}
        audit["attempts"].append(attempt)
        try:
            method = effective_auth_method(command[0], settings, env, cwd, remaining)
        except subprocess.TimeoutExpired:
            audit["reason"] = "auth_status_timeout"
            return 124, stdout, stderr + "\nClaude Code authentication status timed out.\n", audit
        except ValueError as exc:
            audit["reason"] = "auth_status_unavailable"
            return 1, stdout, stderr + f"\n{exc}\n", audit
        attempt["auth_method"] = method
        if method != source.auth_method:
            audit["reason"] = "auth_source_mismatch"
            return 1, stdout, stderr + f"\nSource {source.name} expected {source.auth_method}, got {method}.\n", audit
        if time.monotonic() >= deadline:
            audit["reason"] = "auth_status_timeout"
            return 124, stdout, stderr + "\nClaude Code authentication check exhausted the timeout.\n", audit
        remaining = max(1, int(deadline - time.monotonic()))
        try:
            code, stdout, stderr = run_claude(command, prompt, cwd, remaining, env=env)
        except OSError as exc:
            code, stdout, stderr = 1, "", str(exc)
        attempt.update(returncode=code, raw_stdout=stdout, stderr=stderr)
        try:
            parsed = parse_claude_stream(stdout)
        except ValueError:
            break
        attempt.update(session_id=parsed.session_id, api_key_source=parsed.api_key_source)
        expected_key_source = "apiKeyHelper" if source.key_profile else "none"
        if parsed.api_key_source is not None and parsed.api_key_source != expected_key_source:
            audit["reason"] = "auth_source_mismatch"
            return 1, stdout, stderr + f"\nClaude Code used an unexpected key source for {source.name}.\n", audit
        if session_id and parsed.session_id != session_id and (parsed.session_id or code == 0):
            audit["reason"] = "resume_session_mismatch"
            return 1, stdout, stderr + "\nClaude returned a different session on resume.\n", audit
        if index + 1 == len(routes) or code == 124:
            break
        if source.kind == "subscription" and subscription_exhausted(parsed):
            reason = "subscription_quota_exhausted"
        elif source.kind == "api" and api_balance_exhausted(parsed):
            reason = "api_balance_exhausted"
        else:
            break
        try:
            session_id = str(UUID(parsed.session_id or ""))
        except ValueError:
            session_id = None
        if not session_id or not session_transcript(session_id, sources):
            audit["reason"] = reason + "_without_resumable_session"
            break
        if time.monotonic() >= deadline:
            break
        audit["reason"] = attempt["fallback_reason"] = reason
        prompt = CONTINUE_AFTER_QUOTA
        print(f"Claude billing: {reason}; resuming the same session using {routes[index + 1].name}.",
              file=sys.stderr)
    return code, stdout, stderr, audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    prompt_source = parser.add_mutually_exclusive_group()
    prompt_source.add_argument("--prompt", help="Task or follow-up text; otherwise read stdin")
    prompt_source.add_argument("--prompt-file", help="UTF-8 task or follow-up file")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--cli", help="Path to Claude Code (default: claude on PATH)")
    parser.add_argument("--model", help="Exact model or alias; omit to use CLI/session defaults")
    parser.add_argument("--auth-config", type=Path, help="JSON authentication sources and priority order")
    parser.add_argument("--auth-source", help="Use one named source from --auth-config for this call")
    parser.add_argument("--effort", help="Exact effort; omit to use CLI/session defaults")
    parser.add_argument("--resume", help="Session UUID returned by an earlier call")
    parser.add_argument("--permission-mode", choices=["default", "manual", "plan", "acceptEdits", "dontAsk", "auto"],
                        default="default", help="Native Claude permissions for this call, including on resume")
    parser.add_argument("--tools", help="Available built-in tools; omitted leaves the native default set enabled")
    parser.add_argument("--allowed-tools", action="append", default=[], help="Preapproved tool rule; repeatable")
    parser.add_argument("--disallowed-tools", action="append", default=[], help="Denied tool rule; repeatable")
    parser.add_argument("--add-dir", action="append", default=[], help="Additional authorized directory; repeatable")
    parser.add_argument("--out", help="Write successful response text")
    parser.add_argument("--transcript", help="Write session ID, audit, response and raw CLI stream")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--dry-run", action="store_true", help="Preview without starting Claude")
    args = parser.parse_args()

    args.cli = args.cli or shutil.which("claude")
    if not args.cli:
        parser.error("Claude Code was not found on PATH. Install it or specify --cli.")

    sources = {}
    routes = []
    if args.auth_config:
        try:
            sources, order = load_auth_config(args.auth_config.expanduser())
        except ValueError as exc:
            parser.error(str(exc))
        if args.auth_source is not None:
            if args.auth_source not in sources:
                parser.error("--auth-source must name a configured source")
            order = [args.auth_source]
        routes = [sources[name] for name in order]
    elif args.auth_source is not None:
        parser.error("--auth-source requires --auth-config")

    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        parser.error(f"cwd is not a directory: {cwd}")
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    if args.resume:
        try:
            args.resume = str(UUID(args.resume))
        except ValueError:
            parser.error("--resume requires an explicit session UUID")
    try:
        prompt = (Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
                  if args.prompt_file else args.prompt if args.prompt is not None else sys.stdin.read())
    except OSError as exc:
        parser.error(str(exc))
    if not prompt.strip():
        parser.error("prompt is empty")
    command = build_command(args)
    if args.dry_run:
        auth = [source.name for source in routes] if routes else "native"
        print(json.dumps({"cwd": str(cwd), "command": command, "auth": auth,
                          "prompt_chars": len(prompt)}, indent=2))
        return
    if not os.access(command[0], os.X_OK):
        parser.error(f"Claude Code executable is missing or not executable: {command[0]}")

    if routes:
        returncode, stdout, stderr, auth_audit = run_ordered_auth(args, routes, list(sources.values()), prompt, cwd)
    else:
        try:
            returncode, stdout, stderr = run_claude(command, prompt, cwd, args.timeout)
        except OSError as exc:
            returncode, stdout, stderr = 1, "", str(exc)
        auth_audit = {"requested": "native", "initial": "native", "final": "native",
                      "fallback": False, "reason": None, "attempts": []}
    parsed = None
    parse_error = None
    try:
        parsed = parse_claude_stream(stdout)
    except ValueError as exc:
        parse_error = str(exc)

    # CLI success is only transport success; the caller must verify the task.
    if returncode:
        exit_code = returncode if 1 <= returncode <= 255 else 1
    elif parsed is None:
        exit_code = 1
    elif parsed.stop_reason == "refusal":
        exit_code = 3
    elif parsed.is_error or parsed.result_subtype != "success" or parsed.errors:
        exit_code = 1
    elif parsed.permission_denials:
        exit_code = 4
    elif not parsed.result_text:
        exit_code = 1
    else:
        exit_code = 0
    if parsed and parsed.stop_reason == "refusal" and returncode != 124:
        exit_code = 3

    # Retain the session ID even if the final result is missing or interrupted.
    session_id = parsed.session_id if parsed else None
    if not session_id:
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("session_id"):
                session_id = event["session_id"]
    if not routes:
        auth_audit["attempts"].append({"auth": "native", "returncode": returncode, "session_id": session_id,
                                       "api_key_source": parsed.api_key_source if parsed else None})
    audit = model_audit(args.model, parsed) if parsed else None
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "cwd": str(cwd),
        "command": command, "prompt": prompt, "session_id": session_id,
        "resumed_from": args.resume, "model": args.model, "effort": args.effort,
        "permission_mode": args.permission_mode, "returncode": exit_code,
        "claude_returncode": returncode, "stdout": parsed.result_text if parsed else None,
        "stderr": stderr, "parse_error": parse_error, "model_audit": audit,
        "auth_audit": auth_audit,
        "result_subtype": parsed.result_subtype if parsed else None,
        "permission_denials": parsed.permission_denials if parsed else [],
        "errors": parsed.errors if parsed else [], "raw_stdout": stdout,
    }
    write_text(args.transcript, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if stderr:
        print(stderr, file=sys.stderr)
    print(f"Claude billing: auth={auth_audit['final']} fallback={auth_audit['fallback']} "
          f"reason={auth_audit['reason'] or 'none'}", file=sys.stderr)
    fallback = audit["fallback_detected"] if audit else None
    fallback_text = "unknown" if fallback is None else "yes" if fallback else "no"
    print(
        f"Claude audit: requested={args.model or 'CLI/session default'} "
        f"served={parsed.final_served_model if parsed and parsed.final_served_model else 'unknown'} "
        f"fallback={fallback_text} effort={args.effort or 'CLI/session default (effective unknown)'} "
        f"session={session_id or 'unknown'} "
        f"stop={parsed.stop_reason if parsed and parsed.stop_reason else 'unknown'} "
        f"result={parsed.result_subtype if parsed else 'unknown'}", file=sys.stderr,
    )
    if fallback:
        print("warning: Claude model fallback detected; see audit for the served model and path.", file=sys.stderr)
    if exit_code:
        if parse_error:
            print(f"error: unable to parse Claude stream-json output: {parse_error}", file=sys.stderr)
        if parsed:
            if parsed.stop_reason == "refusal":
                print("paused: Claude safety refusal; no automatic retry was attempted.", file=sys.stderr)
                print(json.dumps(parsed.stop_details, ensure_ascii=False), file=sys.stderr)
            if parsed.permission_denials:
                print("needs review: tool permissions were denied; do not claim full completion.", file=sys.stderr)
                print(json.dumps(parsed.permission_denials, ensure_ascii=False), file=sys.stderr)
            if parsed.errors:
                print(json.dumps(parsed.errors, ensure_ascii=False), file=sys.stderr)
            if parsed.result_text:
                print(f"Claude error/partial response: {parsed.result_text}", file=sys.stderr)
        print(f"error: Claude call incomplete (CLI exit={returncode}, wrapper exit={exit_code}).", file=sys.stderr)
        raise SystemExit(exit_code)
    write_text(args.out, parsed.result_text)
    print(parsed.result_text, end="" if parsed.result_text.endswith("\n") else "\n")


if __name__ == "__main__":
    main()
