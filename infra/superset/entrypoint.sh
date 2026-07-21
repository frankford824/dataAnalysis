#!/bin/bash
set -euo pipefail

superset db upgrade

superset fab create-admin \
  --username "${SUPERSET_ADMIN_USERNAME}" \
  --firstname Platform \
  --lastname Administrator \
  --email "${SUPERSET_ADMIN_EMAIL}" \
  --password "${SUPERSET_ADMIN_PASSWORD}" || true

superset init
python /app/pythonpath/register_analytics.py

if compgen -G "/app/pythonpath/assets/*.zip" >/dev/null; then
  for archive in /app/pythonpath/assets/*.zip; do
    superset import-dashboards -p "$archive" -u "${SUPERSET_ADMIN_USERNAME}" || {
      echo "Could not import optional dashboard asset: $archive" >&2
    }
  done
fi

exec gunicorn \
  --bind 0.0.0.0:8088 \
  --workers "${SUPERSET_WORKERS:-2}" \
  --worker-class gthread \
  --threads "${SUPERSET_THREADS:-4}" \
  --timeout "${SUPERSET_TIMEOUT:-120}" \
  --limit-request-line 0 \
  --limit-request-field_size 0 \
  "superset.app:create_app()"
