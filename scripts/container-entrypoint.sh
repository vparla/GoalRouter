#!/bin/sh
set -eu
umask 077
mkdir -p "${HOME:?}" "${CODEX_HOME:?}"
exec python -m goalrouter "$@"
