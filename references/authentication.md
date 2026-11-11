# Configure authentication and priority

Without `--auth-config`, the helper inherits your native Claude Code authentication. Use a route file when you want to choose among subscriptions or API keys.

## Set up two subscription accounts

On the machine running the skill, sign in to your first account in the default Claude Code configuration. Sign in to a different account in a separate directory. Select the intended account in each browser flow.

```bash
claude auth login
CLAUDE_CONFIG_DIR="$HOME/.claude-secondary" claude auth login
```

Check each login locally using `claude auth status` with the same environment you used for login. Claude Code manages its own subscription credentials and associates each configuration directory with its own login and session history. [Claude Code authentication](https://code.claude.com/docs/en/authentication)

## Add two named API keys

Run the helper from the repository root and enter the key at its hidden terminal prompt. Profile names use lowercase letters, digits, hyphens, and underscores, with a letter or digit first. `--check` checks a profile without printing the key.

```bash
python3 scripts/claude_api_key.py --profile api1
python3 scripts/claude_api_key.py --profile api2
python3 scripts/claude_api_key.py --profile api2 --check
```

Keys are stored in owner-only files under `~/.config/delegate-to-claude/keys/`. Claude Code receives the selected key through a per-call `apiKeyHelper`. Route files contain profile names and configuration paths. Keep key values and OAuth credentials outside the repository, route files, prompts, shell arguments, and transcripts.

## Choose the order

Copy the [four-source example](../examples/four-source.routes.json) to a local file and edit its `order` array to select a sequence or subset of the named sources. This example starts with the default subscription, then tries the first API key, the secondary subscription, and the second API key.

```bash
mkdir -p "$HOME/.config/delegate-to-claude"
cp examples/four-source.routes.json "$HOME/.config/delegate-to-claude/routes.json"

python3 scripts/delegate_to_claude.py \
  --auth-config "$HOME/.config/delegate-to-claude/routes.json" \
  --cwd /path/to/project --permission-mode plan \
  --prompt 'Inspect the repository and list its test commands.'
```

Omit `config_dir` to use the default Claude Code configuration. Set it to an absolute or home-relative path for a separate account. An API source can share a configuration directory with a subscription and selects its own API key for that call. Add `--auth-source NAME` to use a single configured source.

Every call starts at the first selected source. An explicit quota or credit-balance error advances to the next source when the session is saved. The helper checks the effective authentication method before each model request and resumes from the native transcript, including when switching configuration directories. Each route should use accounts authorized to process the task's existing conversation and tool output. [Claude Code session management](https://code.claude.com/docs/en/sessions)

Authentication failures, unexpected authentication methods, missing sessions, temporary rate limits, network errors, refusals, and timeouts end the call. API calls can draw from paid balances. API keys in the same linked Console organization can share credits. [Claude API credits](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers)
