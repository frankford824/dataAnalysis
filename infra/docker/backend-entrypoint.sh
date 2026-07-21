#!/bin/sh
set -eu

role="${1:-api}"

case "$role" in
  api)
    alembic upgrade head
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
    ;;
  worker)
    exec celery -A app.worker.celery_app worker --loglevel=INFO --concurrency="${WORKER_CONCURRENCY:-2}"
    ;;
  scheduler)
    exec celery -A app.worker.celery_app beat --loglevel=INFO --schedule=/tmp/celerybeat-schedule
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  seed)
    exec python -m app.seed
    ;;
  *)
    echo "unknown backend role: $role" >&2
    exit 64
    ;;
esac
