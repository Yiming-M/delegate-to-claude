---
name: delegate-to-claude
description: Delegate tasks to Claude Code with tools, continue the same conversation with feedback, and verify the returned work. Use when the user asks to consult Claude, collaborate with Claude, or have Claude perform a task.
---

# Delegate to Claude

Use Claude as a tool-capable collaborator. Let the user's request determine the
work, division of responsibility, sequence, and stopping point. Do not impose
model-selection menus, prompt-preparation approvals, or a plan-first workflow.

## Send the task

- Call Claude only when the user requests it or an applicable instruction requires
  it. Preparing this skill or discussing Claude does not itself require a call.
- Pass the objective, necessary source context, working directory, constraints,
  authorized actions, expected deliverable, and relevant acceptance checks.
  Preserve literal evidence and user feedback; label quoted material as input
  data. Do not send unrelated conversation history or secrets.
- Use Claude Code on `PATH` by default. Pass `--cli` for a user-specified
  executable. Use `--auth-config` when the user configures named subscription
  and API sources.
- Preserve any requested model and effort exactly. Omit unspecified arguments
  to use Claude's CLI/session defaults. Do not maintain a hard-coded model menu.

## Authentication and priority

For named accounts, API keys, or billing order, read
[authentication setup](references/authentication.md). Route configuration stores
names and paths. The user completes subscription login in Claude Code and
enters API keys in their terminal. Keep credentials outside the checkout and
out of prompts, arguments, and tool output.

The configured order starts each call. An explicit subscription limit or API
credit-balance error can advance to the next source using the saved session and
the same model, effort, and permissions. Inspect partial effects before
continuing mutations. `--auth-source` uses one named source for the call.
Authentication failures, network errors, temporary rate limits, refusals, and
timeouts end the call. Review `auth_audit` for routing separately from model
fallback. API usage may consume paid balance.

## Tools and permissions

Leave tools enabled. Claude may inspect files, search, run commands, and edit
within the task's authorization. It uses its own configured tools and MCP servers;
Codex's tools are not automatically transferred to it.

Choose native permissions for each call, including resumed calls:

- For analysis without project changes, use `--permission-mode plan` and restrict
  tools further when the request requires it. A plan permission mode does not
  impose an extra user approval step on the collaboration.
- For authorized file changes, use `--permission-mode acceptEdits` and preapprove
  necessary commands with specific `--allowed-tools` rules, such as the project's
  test command. Do not approve unrestricted shell execution merely to avoid a
  permission error.
- `--tools` restricts built-in tool availability; `--allowed-tools` preapproves
  calls; `--disallowed-tools` denies calls, including MCP tools. An allow list alone
  does not remove other tools. Apply task restrictions to relevant MCP tools too.
- Do not bypass permissions or change global permission settings. Existing user
  authorization is sufficient for actions within its scope; ask only for missing
  decisions or additional authority. The helper is unattended: unresolved tool
  permissions are denied and reported instead of waiting for an invisible prompt.

## Continue and coordinate

Start a fresh session for an independent task. Capture the returned `session_id`
and use `--resume SESSION_UUID` for subsequent feedback, questions, or revisions.
Never use “continue the most recent session.” Keep the working directory and
explicit model/effort choices consistent unless the task changes them. Each
follow-up carries the new instruction and necessary evidence; Claude's native
session holds the conversation. If resuming fails, report it rather than silently
starting a replacement conversation.

Compose collaboration from ordinary instructions:

- When independent judgment is requested, obtain each initial answer before
  sharing the other answer. Keep draft files out of the other participant's
  supplied context until the exchange begins.
- When reciprocal review is requested, use the preceding round's fixed outputs
  as inputs to each participant. Follow the requested round count; do not invent
  a fixed number of debate rounds. Preserve substantive disagreements.
- When implementation is delegated, provide acceptance criteria and return
  concrete review findings to the same session. Avoid simultaneous edits to the
  same files; isolate work only when concurrent changes require it.
- Continue within the authorized task until its requested deliverable is ready.
  Stop on user instruction, missing authority, or a blocker; do not repeat failed
  calls or revision loops without new evidence or a changed approach.

## Run and inspect

Use `scripts/delegate_to_claude.py`. It sends the prompt through stdin with
`--print --verbose --output-format stream-json`, retains resumable sessions, and
extracts the answer and execution audit. For example:

```bash
# Inspect a project; tools remain enabled. Keep prompts and transcripts in a
# private directory.
work_dir=$(mktemp -d)
python3 /path/to/delegate-to-claude/scripts/delegate_to_claude.py \
  --cwd /path/to/project --permission-mode plan \
  --prompt-file "$work_dir/task.md" --transcript "$work_dir/claude-first.json"

# Continue that task after edits have been authorized.
python3 /path/to/delegate-to-claude/scripts/delegate_to_claude.py \
  --cwd /path/to/project --resume SESSION_UUID \
  --permission-mode acceptEdits --allowed-tools 'Bash(python3 -m unittest *)' \
  --prompt-file "$work_dir/feedback.md" --transcript "$work_dir/claude-followup.json"
```

Use distinct transcript paths for separate calls. `--out` writes successful
response text; `--transcript` also retains partial output, session ID, raw events,
permission denials, and errors. `--dry-run` previews without calling Claude.
Exit 3 reports a refusal, exit 4 reports permission denials requiring review,
exit 124 reports timeout, and other nonzero exits report invocation/output errors.
Do not automatically retry refusals, switch models, or replay partially completed
mutations. Inspect partial changes before any continuation.

A successful CLI exit does not prove task completion. Inspect the actual
artifacts and run relevant checks before accepting implementation; verify
consequential factual claims against their sources. Return the useful result,
any remaining disagreement or blocker, and what Codex actually verified.
Include a compact audit of requested/served model, effort, fallback status,
completion reason, and API errors. Mark unavailable fields as unknown, including
the effective effort when the CLI does not expose it.

Claude also needs write access to its native session storage for continuation.
A successful response can arrive even when a sandbox prevents session storage;
do not claim resumability until persistence or a subsequent resume is verified.
If sandbox restrictions block the call or session persistence, follow the host's
escalation procedure without changing the requested model or effort. Inspect
completed actions before retrying; never replay mutations merely to save a session.
Diagnose sandbox access separately from login status. If authentication is
unavailable, direct the user to the login command for the selected configuration
in the authentication reference.
