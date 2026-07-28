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

required="POSTGRES_PASSWORD ANALYTICS_READER_PASSWORD MINIO_ROOT_PASSWORD APP_SECRET_KEY APP_ENCRYPTION_KEY SUPERSET_SECRET_KEY SUPERSET_GUEST_TOKEN_SECRET SUPERSET_ADMIN_PASSWORD COMMERCE_MAX_UPLOAD_BYTES COMMERCE_MAX_UNCOMPRESSED_BYTES COMMERCE_MAX_INPUT_ROWS"
for name in $required; do
  eval "value=\${$name:-}"
  [ -n "$value" ] || { echo "$name must not be empty" >&2; exit 1; }
done

# Upgrades from before the edge/core split have no token yet, so say exactly
# how to add one instead of letting compose fail on interpolation.
[ -n "${FA_EDGE_TOKEN:-}" ] || {
  echo "FA_EDGE_TOKEN 未设置：core 会拒绝所有上传。" >&2
  echo "补一行到 $env_file：FA_EDGE_TOKEN=\$(openssl rand -hex 32)" >&2
  exit 1
}

check_integer_range() {
  name="$1"
  minimum="$2"
  maximum="$3"
  eval "value=\${$name}"
  case "$value" in
    *[!0-9]*|'') echo "$name must be a positive integer" >&2; exit 1 ;;
  esac
  if [ "$value" -lt "$minimum" ] || [ "$value" -gt "$maximum" ]; then
    echo "$name must be between $minimum and $maximum" >&2
    exit 1
  fi
}

check_integer_range COMMERCE_MAX_UPLOAD_BYTES 1048576 10737418240
check_integer_range COMMERCE_MAX_UNCOMPRESSED_BYTES 1048576 53687091200
check_integer_range COMMERCE_MAX_INPUT_ROWS 1 100000000
[ "$COMMERCE_MAX_UNCOMPRESSED_BYTES" -ge "$COMMERCE_MAX_UPLOAD_BYTES" ] || {
  echo "COMMERCE_MAX_UNCOMPRESSED_BYTES must be at least COMMERCE_MAX_UPLOAD_BYTES" >&2
  exit 1
}

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
  [ "${SESSION_COOKIE_SECURE:-false}" = true ] || {
    echo "SESSION_COOKIE_SECURE=true is required in production" >&2
    exit 1
  }
fi

case "${DEMO_AUTO_SETUP:-false}" in
  true|false) ;;
  *) echo "DEMO_AUTO_SETUP must be true or false" >&2; exit 1 ;;
esac

if [ "${DEMO_AUTO_SETUP:-false}" = true ]; then
  [ "${APP_ENV:-development}" != production ] || {
    echo "DEMO_AUTO_SETUP is forbidden in production" >&2
    exit 1
  }
  for name in DEMO_ENTERPRISE_NAME DEMO_ADMIN_NAME DEMO_ADMIN_EMAIL DEMO_ADMIN_PASSWORD; do
    eval "value=\${$name:-}"
    [ -n "$value" ] || { echo "$name is required when DEMO_AUTO_SETUP=true" >&2; exit 1; }
  done
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
