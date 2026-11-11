"""Load named Claude Code authentication sources and prepare isolated launches."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from claude_api_key import key_profile_path, require_outside_checkout


@dataclass(frozen=True)
class AuthSource:
    name: str
    config_dir: Path | None
    key_profile: str | None = None

    @property
    def kind(self) -> str:
        return "api" if self.key_profile else "subscription"

    @property
    def auth_method(self) -> str:
        return "api_key_helper" if self.key_profile else "claude.ai"

    def launch(self) -> tuple[list[str], dict[str, str]]:
        env = os.environ.copy()
        for name in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_PROFILE",
            "ANTHROPIC_WORKSPACE_ID", "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
        ):
            env.pop(name, None)
        env["ANTHROPIC_BASE_URL"] = "https://api.anthropic.com"
        if self.config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        helper = ""
        if self.key_profile:
            helper = shlex.join([sys.executable, str(Path(__file__).with_name("claude_api_key.py").resolve()),
                                 "--profile", self.key_profile, "--emit"])
        # A configured API helper takes precedence over the login in the same directory.
        settings = {"apiKeyHelper": helper, "env": {"ANTHROPIC_BASE_URL": env["ANTHROPIC_BASE_URL"]}}
        return ["--settings", json.dumps(settings)], env


def effective_auth_method(cli: str, settings: list[str], env: dict[str, str], cwd: Path, timeout: int) -> str:
    try:
        result = subprocess.run([cli, *settings, "auth", "status"], env=env, cwd=cwd,
                                text=True, capture_output=True, timeout=timeout)
        status = json.loads(result.stdout)
    except (OSError, ValueError) as exc:
        raise ValueError("Claude Code did not return an authentication status") from exc
    if not isinstance(status, dict) or not isinstance(status.get("authMethod"), str):
        raise ValueError("Claude Code did not report its authentication method")
    return status["authMethod"] if result.returncode == 0 and status.get("loggedIn") else "none"


def load_auth_config(path: Path) -> tuple[dict[str, AuthSource], list[str]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Cannot read the authentication configuration as JSON") from exc
    if not isinstance(document, dict) or document.keys() != {"sources", "order"}:
        raise ValueError("Authentication configuration must contain sources and order")
    sources, order = document["sources"], document["order"]
    if not isinstance(sources, dict) or not sources or not isinstance(order, list) or not order:
        raise ValueError("Authentication configuration requires named sources and a nonempty order")
    parsed = {}
    for name, fields in sources.items():
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
            raise ValueError("Invalid authentication source name")
        if not isinstance(fields, dict) or fields.keys() - {"type", "config_dir", "key_profile"}:
            raise ValueError(f"Invalid fields for authentication source {name}")
        kind = fields.get("type")
        profile = fields.get("key_profile")
        if kind == "api" and isinstance(profile, str):
            key_profile_path(profile)
        elif kind != "subscription" or "key_profile" in fields:
            raise ValueError(f"Authentication source {name} needs a subscription or named API key profile")
        directory = None
        if "config_dir" in fields:
            value = fields["config_dir"]
            if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
                raise ValueError(f"Authentication source {name} needs an absolute config_dir")
            directory = Path(value).expanduser().resolve()
        require_outside_checkout(directory or Path.home() / ".claude")
        parsed[name] = AuthSource(name, directory, profile)
    if any(not isinstance(name, str) or name not in parsed for name in order) or len(set(order)) != len(order):
        raise ValueError("Authentication order must list distinct configured source names")
    return parsed, order


def session_transcript(session_id: str, sources: list[AuthSource]) -> Path | None:
    matches = {}
    for directory in {source.config_dir or Path.home() / ".claude" for source in sources}:
        for path in (directory / "projects").glob(f"*/{session_id}.jsonl"):
            try:
                matches[path] = path.stat().st_mtime_ns
            except FileNotFoundError:
                continue
    return max(matches, key=lambda path: (matches[path], str(path)), default=None)
