#!/bin/sh
set -eu

mkdir -p "${KD_DATA_DIR:-/var/lib/khanij-drishti/data}" "${KD_MODELS_DIR:-/var/lib/khanij-drishti/models}"

if [ ! -f "${KD_MODELS_DIR:-/var/lib/khanij-drishti/models}/shortfall_card.json" ]; then
  cp -R /seed/models/. "${KD_MODELS_DIR:-/var/lib/khanij-drishti/models}/"
fi

python -m kd.migrate

# Only processed tables and virtual telemetry are present in /seed/data. Authentication, audit,
# notification settings, raw device secrets and simulator keys are never baked into the image.
if [ -n "${KD_DATABASE_URL:-}" ]; then
  if ! python -c "from kd import db; raise SystemExit(0 if db.table_exists('production_daily','warehouse') else 1)"; then
    python -m kd.migrate import-sqlite /seed/data
  fi
fi

exec "$@"
