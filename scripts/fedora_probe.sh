#!/usr/bin/env bash
set -euo pipefail

# Checked-in target-host entrypoint for issue #64 evidence. It never guesses a
# database or provider; the operator supplies the real PostgreSQL URL and an
# output path so the retained artifact is tied to this checkout.
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
: "${CLASHLENS_TEST_DATABASE_URL:?set CLASHLENS_TEST_DATABASE_URL to the disposable Fedora PostgreSQL database}"
OUTPUT=${CLASHLENS_FEDORA_PROBE_OUTPUT:-"$ROOT_DIR/results/issue-64-fedora-$(date -u +%Y%m%dT%H%M%SZ).json"}
mkdir -p "$(dirname -- "$OUTPUT")"
exec uv run --project "$ROOT_DIR/python" --python 3.12 \
  "$ROOT_DIR/scripts/performance_runner.py" \
  "${1:-duplicate-heavy}" --database-url "$CLASHLENS_TEST_DATABASE_URL" \
  --output "$OUTPUT" "${@:2}"
