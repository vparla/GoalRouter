<!-- SPDX-License-Identifier: MIT -->
<!-- File: docs/releasing.md -->
<!-- Purpose: Protected GoalRouter release process and recovery guidance -->

# Releasing

GoalRouter releases are published only by `.github/workflows/publish.yml` from the canonical
`vparla/GoalRouter` repository. The runtime image is `ghcr.io/vparla/goalrouter`. Version
1.0.0 is the first stable release.

## Version synchronization

Before proposing a release, synchronize the exact stable version across:

- `pyproject.toml` project version;
- `src/goalrouter/__init__.py` `__version__`;
- Dockerfile runtime default build argument;
- `compose.live.yaml` runtime build default;
- `scripts/install.ps1` default `Version` parameter;
- `.github/workflows/publish.yml` version, expected tag, image aliases, and asset names;
- release-contract fixtures and tests;
- `README.md` install examples;
- `CHANGELOG.md` release heading and date.

The release asset builder also requires matching explicit `--version`, `--tag`, image tag,
image digest, source revision, and source timestamp inputs. It rejects drift before
publishing output.

## Pre-release gates

A stable release commit must already be on a successful green `main` CI run. Run the same
Docker-only lifecycle locally:

```sh
docker compose config --quiet
docker compose build --check
docker compose build
docker compose run --rm test
docker compose run --rm lint
docker compose run --rm typecheck
docker compose run --rm package
docker compose run --rm shellcheck
docker compose run --rm powershell-test
docker compose run --rm distribution-test
```

Review the changelog, public docs, installer examples, package metadata, image labels, and
release-contract tests. Do not run a publication workflow from an unreviewed local state.

## Tag and protected publication

Create an annotated tag named exactly `vX.Y.Z` on the green `main` commit and push that tag.
The release gate verifies that it is an annotated SemVer tag, peels to the workflow commit,
is an ancestor of current `origin/main`, and has a successful `main` push CI run for the
same commit.

Stable publication uses the protected `release` environment. Configure required reviewers
and prevent unreviewed deployment. Grant only the workflow's scoped `GITHUB_TOKEN`
permissions; do not add a personal token or long-lived registry credential.

The workflow builds native `linux/amd64` and `linux/arm64` images with SBOM and maximum
provenance, pushes temporary commit-scoped images, prepares and verifies the exact future
multi-architecture index digest, builds deterministic release assets, verifies
`SHA256SUMS`, publishes a temporary index, and creates a registry attestation for the final
digest. Only then does it publish stable aliases `vX.Y.Z`, `X.Y.Z`, `X.Y`, `X`, and
`latest`, recheck every alias against the prepared digest, and create the GitHub Release.

The release tag, GitHub Release, and OCI aliases `vX.Y.Z` and `X.Y.Z` are immutable
release names. The stable path refuses to overwrite or reuse them and is deliberately
non-idempotent once any of those public names exist. OCI aliases `X.Y`, `X`, and `latest`
are approved moving aliases. For a patch release, the workflow resolves the prior
immutable patch image and advances the moving aliases only after proving that all three
still resolve to that exact prior digest. A missing, malformed, unauthorized, unexpected,
or divergent moving alias blocks publication; divergence is a release incident and must
not be repaired by overwriting immutable evidence. The workflow repeats the complete
authenticated immutable-name, GitHub Release, prior-digest, and moving-alias precondition
check immediately before the overwrite-capable stable alias command so expensive asset and
attestation work cannot leave stale publication assumptions.

## Release assets

The release contains exactly:

- `SHA256SUMS`;
- `goalrouter-X.Y.Z-unix.tar.gz`;
- `goalrouter-X.Y.Z-windows.zip`;
- `install.ps1` and `install.sh`;
- `release-manifest.json`;
- `uninstall.ps1` and `uninstall.sh`.

Archives are deterministic, root-relative, bounded, and contain only expected regular
files. The manifest binds version, launcher protocol, image, immutable digest,
architectures, source revision, and host minimums.

## Post-release registry settings and verification

After the first package exists, configure the GHCR package as public and link it to
`vparla/GoalRouter`. Confirm the image source/documentation labels point to the repository.
Verify release checksums from a fresh directory, verify the GitHub attestation for the
published digest, and perform an anonymous pull after clearing or isolating registry
credentials:

```sh
docker pull ghcr.io/vparla/goalrouter:1.0.10
docker image inspect ghcr.io/vparla/goalrouter:1.0.10
```

Also perform clean Windows and POSIX installer checks from downloaded assets. Confirm
`goalrouter version`, `goalrouter doctor`, preserve uninstall, and purge uninstall behavior.

## Release rollback

If failure occurs before stable aliases, GitHub Release creation, or other public stable
state, fix the cause on `main`, pass CI again, create a new version, and remove only verified
temporary commit-scoped registry artifacts if cleanup is needed.

If any stable alias or GitHub Release is public, do not rerun the same version and do not
overwrite it. Assess impact, mark the GitHub Release clearly, publish corrected docs if
safe, and prepare a new patch version from a new green commit. Consumers can pin the prior
known-good immutable digest while remediation proceeds.

## Moving tag correction

A moving tag is a release incident. Do not force-push a stable tag to a different commit.
If a local annotated tag is wrong and has never been pushed or published, delete only the
local tag, create it again on the correct green commit, and verify it before the first push.
If the tag has reached the remote or any release asset/image is public, leave evidence
intact, document the incident, and issue a new version. Never make a public release name
point at new bytes.
