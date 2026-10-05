#!/usr/bin/env bash
# Runs the spike CLI with E2B_API_KEY loaded from the repo's .env (never
# echoed). Sandbox-scoped tokens (envd / traffic) are cached in
# $SPIKE_STATE_DIR, which must be outside the repository.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
set -a
# shellcheck disable=SC1091
. "$here/../../.env"
set +a
: "${SPIKE_STATE_DIR:?set SPIKE_STATE_DIR to a directory outside the repo}"
: "${SPIKE_BIN:=$SPIKE_STATE_DIR/../spike}"
exec "$SPIKE_BIN" "$@"
