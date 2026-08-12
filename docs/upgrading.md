<!-- SPDX-License-Identifier: MIT -->
<!-- File: docs/upgrading.md -->
<!-- Purpose: Safe installed GoalRouter version upgrades and rollback guidance -->

# Upgrading

The trusted installed launcher delegates updates to the installer recorded beside it.
Updates preserve a valid existing configuration and all run state by default. They replace
owned launcher/lifecycle files and immutable image metadata only after the candidate
release, image, protocol, template, and current config validate.

## Before updating

```text
goalrouter version
goalrouter doctor
goalrouter config validate
```

Back up the user-owned configuration and state directories recorded by the installation.
Do not copy Codex credentials into the backup. Read [CHANGELOG](../CHANGELOG.md) and the
release notes for policy or schema changes.

## Update to the latest stable release

```text
goalrouter update
goalrouter version
goalrouter doctor
```

## Update to an exact version

```text
goalrouter update 1.0.6
goalrouter version
goalrouter doctor
```

On Windows the launcher invokes the recorded PowerShell installer with its exact install
root, config, state, Codex home, WSL distribution, release base, image repository, auth
mode, and PATH ownership. On POSIX it invokes the recorded shell installer with the same
owned destinations. A custom installation remains custom.

The updater downloads verified release assets, resolves the image tag to an immutable
digest, confirms the launcher protocol and application version, and validates the existing
config before activation. Failure rolls back replaced files and PATH state. A successful
update preserves run directories and does not rerun old work.

## Configuration behavior

Compatible existing config is preserved. If the candidate rejects it, the update stops.
To intentionally replace policy with the new template, invoke the installed installer
directly with `-ResetConfig` on Windows or `--reset-config` on POSIX after saving and
reviewing the existing file. The force/repair option also resets config and is not a
routine upgrade flag.

## Roll back an installed version

Use `goalrouter update X.Y.Z` only if that exact published release is still available and
its configuration/state schema is supported. Back up state first, complete the downgrade,
then run `goalrouter version`, `goalrouter config validate`, and `goalrouter doctor`.
Never move a public release tag or edit install metadata to simulate a rollback.
