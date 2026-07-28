#!/bin/sh
set -eu

role="${1:-api}"

case "$role" in
  api)
    alembic upgrade head
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
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
