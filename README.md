# Delegate to Claude

A Codex skill for handing a task to Claude Code, reviewing the result, and sending feedback to the same Claude session. Give Claude a project directory, the actions it may take, and the checks it should run. Codex can then inspect the changes and verify the result.

The skill calls the `claude` executable on your `PATH` and uses its existing login by default. You can also configure an ordered list of subscription accounts and API keys for quota fallback.

## Install

You'll need Codex, Python 3.10 or later, and a current [Claude Code CLI](https://code.claude.com/docs/en/setup) on macOS or Linux. Use the current CLI for `--permission-prompts none` and for resuming sessions by transcript path. [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)

1. Sign in to Claude Code on the machine that will run the skill.

   ```bash
   claude auth login
   claude auth status
   ```

2. From the root of this repository, copy the skill into your user skills directory. [Codex skill locations](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)

   ```bash
   skill_dir="$HOME/.agents/skills/delegate-to-claude"
   mkdir -p "$skill_dir"
   cp -R SKILL.md agents scripts references examples "$skill_dir/"
   ```

3. In Codex, invoke the skill with a request such as this one.

   > Use $delegate-to-claude to inspect this project for missing input validation. Ask Claude to cite file paths and keep the review read-only.

## Run from the terminal

From the repository root, make a read-only request and save its session ID in a private transcript.

```bash
audit_dir=$(mktemp -d)
python3 scripts/delegate_to_claude.py \
  --cwd /path/to/project --permission-mode plan \
  --prompt 'Review input validation and cite relevant files.' \
  --transcript "$audit_dir/first.trace.json"
```

Use the reported session UUID for a follow-up from the same working directory.

```bash
python3 scripts/delegate_to_claude.py \
  --cwd /path/to/project --permission-mode plan --resume SESSION_UUID \
  --prompt 'Check whether those findings are covered by existing tests.' \
  --transcript "$audit_dir/follow-up.trace.json"
```

For implementation, use `--permission-mode acceptEdits` and preapprove the specific commands the task needs with `--allowed-tools`, for example `--allowed-tools 'Bash(pytest -q)'`. Use `--model` and `--effort` to pass an explicit choice to Claude Code, and `--cli` to select an executable outside `PATH`. `--dry-run` previews the command and route order without starting Claude.

## Optional four-source priority

The [example route file](examples/four-source.routes.json) tries `subscription1 → api1 → subscription2 → api2`. The order is configurable, including API-first sequences. Each subscription uses Claude Code's native login. Each API source references a locally stored key by profile name.

Follow the [authentication setup](references/authentication.md) to log in to a second subscription, save two API keys, and copy the example to a local configuration file. Use it with `--auth-config "$HOME/.config/delegate-to-claude/routes.json"`, or give Codex the path when invoking the skill.

An explicit subscription limit or exhausted API credit balance can trigger a switch. The helper resumes the saved session with the same model and permission arguments. The transcript records which source was selected, Claude Code's reported authentication method, and any switch. Keep transcripts private since they contain task prompts and tool output.

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests use simulated Claude Code events and a local test executable.
