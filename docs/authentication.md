<!-- SPDX-License-Identifier: MIT -->
<!-- File: docs/authentication.md -->
<!-- Purpose: GoalRouter authentication modes and credential boundaries -->

# Authentication

GoalRouter implements two explicit modes: `existing-session` and `api-key`. Authentication
selects the Codex account and billing/access context; it does not grant project write or
Docker authority.

OpenAI's current [Codex authentication documentation](https://developers.openai.com/codex/auth/)
states that local Codex supports ChatGPT sign-in for subscription/workspace access and
API-key sign-in for usage-based access. The ChatGPT desktop app, Codex CLI, and IDE
extension support both for local work. `codex login` starts the CLI browser flow.

Do not assume that signing into one surface always provisions every other surface. Codex
CLI and the IDE extension share a login cache according to the official documentation,
while other storage can vary by surface and credential-store configuration. Run
`goalrouter doctor`; if the mounted state is unavailable, use the official Codex login
flow for the Codex home selected during GoalRouter installation.

## Default existing-session mode

`existing-session` is the default and does not require an API key. The native launcher
mounts the recorded host Codex home at `/codex-auth` read-only. GoalRouter copies only
`auth.json`, `config.toml`, and `models_cache.json` when present into a mode-restricted
directory on container tmpfs. Codex runtime writes stay in that temporary staging area;
the host authentication source remains read-only.

A Codex home populated by ChatGPT or workspace SSO sign-in is sufficient for this default
mode on Windows, Linux, and macOS. GoalRouter does not need a separate API key when that
session is valid and exposes the configured models.

GoalRouter calls the account endpoint before model inventory or work. Missing, unreadable,
corrupt, expired, or unauthorized state is an authentication failure. The mode never
silently falls back to an available key.

```text
goalrouter --auth-mode existing-session doctor
goalrouter --auth-mode existing-session models
```

POSIX `goalrouter doctor --skip-account` and Windows
`goalrouter doctor -SkipAccount` can isolate installation/configuration checks, but they
do not prove account authentication or configured-model availability.

## Explicit API-key mode

Select `api-key` deliberately and expose `OPENAI_API_KEY` to the launching process
environment only. Never put a key in routing YAML, command arguments, prompts, repository
instructions, state, or a documentation file. The launcher forwards the named variable to
the runtime without placing its value in the Docker argument list. A missing key is fatal,
and explicit key mode never silently falls back to session state.

Windows PowerShell process-scoped example:

```powershell
$env:OPENAI_API_KEY = Read-Host 'OpenAI API key'
goalrouter --auth-mode api-key doctor
Remove-Item Env:OPENAI_API_KEY
```

POSIX process-scoped example:

```sh
read -r -s OPENAI_API_KEY
export OPENAI_API_KEY
goalrouter --auth-mode api-key doctor
unset OPENAI_API_KEY
```

GoalRouter redacts known secret-shaped keys and values from persisted state, events, and
reports, but users must still keep secrets out of prompts and target repositories. API-key
usage follows OpenAI Platform billing and policy; ChatGPT sign-in follows the selected
workspace's access and data controls.

## Diagnose authentication

1. Run `goalrouter doctor` and note whether failure is installation, mount, or account
   inventory.
2. Confirm the installed Codex-home path with the install record and make sure it is the
   intended local state directory.
3. For ChatGPT sign-in, run `codex login` in the official Codex CLI and complete its browser
   flow, then rerun doctor.
4. For explicit key mode, set the process variable and keep `--auth-mode api-key` on the
   invocation.
5. Never repair authentication by copying a secret into config or broadening project
   authority.
