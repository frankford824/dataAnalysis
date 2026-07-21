#!/bin/sh
set -eu

env_file="${1:-.env}"
[ -f "$env_file" ] || { echo "missing $env_file" >&2; exit 1; }
case "$env_file" in
  /*) ;;
  *) env_file="./$env_file" ;;
esac

set -a
# shellcheck disable=SC1090
. "$env_file"
set +a

required="POSTGRES_PASSWORD ANALYTICS_READER_PASSWORD MINIO_ROOT_PASSWORD APP_SECRET_KEY APP_ENCRYPTION_KEY SUPERSET_SECRET_KEY SUPERSET_GUEST_TOKEN_SECRET SUPERSET_ADMIN_PASSWORD"
for name in $required; do
  eval "value=\${$name:-}"
  [ -n "$value" ] || { echo "$name must not be empty" >&2; exit 1; }
done

if [ "${APP_ENV:-development}" = production ]; then
  case "${POSTGRES_PASSWORD}|${ANALYTICS_READER_PASSWORD}|${MINIO_ROOT_PASSWORD}|${APP_SECRET_KEY}|${SUPERSET_SECRET_KEY}|${SUPERSET_ADMIN_PASSWORD}" in
    *dev-only*|*development*|*change-me*)
      echo "development credentials are forbidden in production" >&2
      exit 1
      ;;
  esac
  [ "${SUPERSET_ADMIN_EMAIL:-}" != admin@example.invalid ] || {
    echo "set a real SUPERSET_ADMIN_EMAIL in production" >&2
    exit 1
  }
fi

case "${AI_MODE:-disabled}" in
  disabled|local) ;;
  cloud)
    if [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
      echo "AI_MODE=cloud requires at least one approved provider key" >&2
      exit 1
    fi
    ;;
  *) echo "AI_MODE must be disabled, local, or cloud" >&2; exit 1 ;;
esac

echo "configuration is valid for ${APP_ENV:-development}"
