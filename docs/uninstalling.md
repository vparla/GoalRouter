<!-- SPDX-License-Identifier: MIT -->
<!-- File: docs/uninstalling.md -->
<!-- Purpose: Safe GoalRouter uninstall and exact data-purge modes -->

# Uninstalling

The default uninstall removes only owned launchers, lifecycle files, trusted install
control, and an installer-owned PATH entry. It preserves configuration and run state so a
later install can recover policy and evidence. Purge is separate, explicit, and
irreversible after successful completion.

## Preserve configuration and state

POSIX interactive:

```text
goalrouter uninstall
```

POSIX confirmed non-interactive:

```text
goalrouter uninstall --yes
```

Windows requires explicit confirmation and has no interactive uninstall form:

```text
goalrouter uninstall -Yes
```

Preserve mode leaves the installer-recorded configuration directory and state directory
in place. The downloaded runtime image is not deleted by the uninstaller.

## Purge owned configuration and state

The exact POSIX purge command is:

```text
goalrouter uninstall --purge --yes
```

The exact Windows purge command is:

```text
goalrouter uninstall -Purge -Yes
```

Purge accepts only exact installer-owned destinations with valid sentinels, ownership,
checksums, safe path relationships, and no symbolic-link/reparse escape. Windows purge
refuses foreign content. POSIX removes content beneath the exact validated owned roots
while retaining its sentinel until fallible traversal is complete. Neither mode removes
the Docker image or Codex home.

## Interrupted uninstall

Both platforms record recovery state. Retry with the same mode: use `--yes` again for a
POSIX preserve retry, `--purge --yes` for a POSIX purge retry, `-Yes` for a Windows preserve
retry, or `-Purge -Yes` for a Windows purge retry. Changing mode during recovery is
rejected. Do not delete control files or ownership sentinels to force progress.

## Remove the runtime image separately

After uninstall, inspect Docker image references and remove the exact GoalRouter digest
only if no other installation or user needs it. Image removal is intentionally outside the
uninstaller's authority and is not inferred by `--purge` or `-Purge`.
