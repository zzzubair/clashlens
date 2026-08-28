#!/usr/bin/env bash
set -euo pipefail

# Checked-in target-host entrypoint for issue #60 Step 8 evidence. It never guesses a
# database or provider; the operator supplies the real PostgreSQL URL and an
# output path so the retained artifact is tied to this checkout.
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
: "${CLASHLENS_TEST_DATABASE_URL:?set CLASHLENS_TEST_DATABASE_URL to the disposable Fedora PostgreSQL database}"
: "${CLASHLENS_CANDIDATE_RECEIPT:?set CLASHLENS_CANDIDATE_RECEIPT to the exact-head candidate-preparation receipt}"
MODE=${1:-duplicate-heavy}
RESULTS_DIR=${CLASHLENS_FEDORA_RESULTS_DIR:-"$ROOT_DIR/../clashlens-step8-results"}
OUTPUT=${CLASHLENS_FEDORA_PROBE_OUTPUT:-"$RESULTS_DIR/${MODE}-fedora-$(date -u +%Y%m%dT%H%M%SZ).json"}
mkdir -p "$(dirname -- "$OUTPUT")"
# The bounded spool owns one lock file per shard. Fedora service sessions may
# inherit a 1024 soft limit even though the host hard limit is much larger.
# Raise only this validation process so the production-shaped spool can open
# its fixed lock set without changing host or service configuration.
CLASHLENS_FEDORA_OPEN_FILES=65536
if (( $(ulimit -Sn) < CLASHLENS_FEDORA_OPEN_FILES )); then
  ulimit -Sn "$CLASHLENS_FEDORA_OPEN_FILES"
fi
exec uv run --project "$ROOT_DIR/python" --python 3.12 \
  "$ROOT_DIR/scripts/performance_runner.py" \
  "$MODE" --database-url "$CLASHLENS_TEST_DATABASE_URL" \
  --candidate-receipt "$CLASHLENS_CANDIDATE_RECEIPT" \
  --output "$OUTPUT" "${@:2}"
