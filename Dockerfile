# SPDX-License-Identifier: MIT
# File: Dockerfile
# Purpose: Python 3.14 build, test, and runtime lifecycle for GoalRouter

FROM python:3.14.6-alpine3.24@sha256:26730869004e2b9c4b9ad09cab8625e81d256d1ce97e72df5520e806b1709f92 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GOALROUTER_CONFIG=/etc/goalrouter/task-models.yaml \
    GOALROUTER_SCHEMA=/etc/goalrouter/task-models.schema.json \
    GOALROUTER_PLANNER_SCHEMA=/etc/goalrouter/planner-output.schema.json

FROM base AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /workspace

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM build AS test

RUN apk add --no-cache ca-certificates curl docker-cli docker-cli-buildx git

RUN python -m pip install --no-cache-dir ".[dev]"

COPY tests ./tests
COPY docs ./docs
COPY .github ./.github
COPY AGENTS.md CONTRIBUTING.md SECURITY.md CHANGELOG.md ./
COPY Dockerfile compose.yaml compose.live.yaml .dockerignore .gitignore ./
COPY config ./config
COPY config /etc/goalrouter
COPY scripts ./scripts

RUN chmod 0555 \
    scripts/release-assets.sh \
    scripts/install.sh \
    scripts/uninstall.sh \
    scripts/goalrouter \
    scripts/docker-resource-cleanup.sh \
    tests/fixtures/distribution/fake-release/docker \
    tests/fixtures/distribution/fake-docker \
    tests/fixtures/distribution/fake-cleanup-docker

FROM base AS release-assets

WORKDIR /workspace

COPY pyproject.toml Dockerfile ./
COPY src ./src
COPY scripts ./scripts

RUN chmod 0555 scripts/release-assets.sh

ENTRYPOINT ["/workspace/scripts/release-assets.sh"]

FROM base AS shell-test

RUN apk add --no-cache shellcheck

FROM docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44 AS posix-installer-smoke

RUN apk add --no-cache busybox-extras curl

WORKDIR /workspace

FROM mcr.microsoft.com/powershell:7.5-debian-12@sha256:7ab5bd5ca6f95a3351fa0c6a1205237d57048c94542355aab55519a0861a9b25 AS powershell-test

WORKDIR /workspace

COPY tests/distribution/powershell_contract.Tests.ps1 ./tests/distribution/
COPY tests/distribution/powershell_lifecycle_contract.Tests.ps1 ./tests/distribution/
COPY tests/fixtures/distribution/fake-wsl.ps1 ./tests/fixtures/distribution/
COPY tests/fixtures/distribution/public-launcher-contract.json ./tests/fixtures/distribution/
COPY scripts ./scripts
COPY Dockerfile ./

FROM base AS runtime

ARG VERSION=1.0.3
ARG REVISION=local
ARG CREATED=1970-01-01T00:00:00Z

LABEL org.opencontainers.image.source="https://github.com/vparla/GoalRouter" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.title="GoalRouter" \
    org.opencontainers.image.description="Task-driven model routing controller for local Codex engineering workflows" \
    org.opencontainers.image.documentation="https://github.com/vparla/GoalRouter#readme" \
    org.opencontainers.image.version="${VERSION}" \
    org.opencontainers.image.revision="${REVISION}" \
    org.opencontainers.image.created="${CREATED}"

ENV HOME=/tmp/goalrouter-home \
    CODEX_HOME=/tmp/codex-home \
    GOALROUTER_IMAGE_VERSION=${VERSION} \
    GOALROUTER_IMAGE_REVISION=${REVISION} \
    GOALROUTER_IMAGE_CREATED=${CREATED}

RUN apk add --no-cache ca-certificates git

COPY --from=build /wheels /wheels

RUN python -m pip install --root-user-action=ignore \
        --no-cache-dir --no-index --find-links=/wheels goalrouter \
    && rm -rf \
        /wheels \
        /root/.cache \
        /usr/local/bin/pip \
        /usr/local/bin/pip3 \
        /usr/local/bin/pip3.14 \
        /usr/local/lib/python3.14/ensurepip \
        /usr/local/lib/python3.14/site-packages/pip \
        /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
        /usr/local/lib/python3.14/site-packages/setuptools \
        /usr/local/lib/python3.14/site-packages/setuptools-*.dist-info \
        /usr/local/lib/python3.14/site-packages/wheel \
        /usr/local/lib/python3.14/site-packages/wheel-*.dist-info \
    && addgroup -g 10001 -S goalrouter \
    && adduser -u 10001 -S -D -H -G goalrouter goalrouter

COPY config /etc/goalrouter
COPY --chmod=0555 scripts/container-entrypoint.sh /usr/local/bin/goalrouter-container-entrypoint

WORKDIR /project

USER 10001:10001

ENTRYPOINT ["/usr/local/bin/goalrouter-container-entrypoint"]
