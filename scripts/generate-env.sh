#!/bin/sh
set -eu

command -v openssl >/dev/null 2>&1 || {
  echo "openssl is required" >&2
  exit 1
}

random_hex() { openssl rand -hex "$1"; }
random_b64() { openssl rand -base64 "$1" | tr -d '\n'; }

cat <<EOF
APP_ENV=production
POSTGRES_DB=commerce
POSTGRES_USER=commerce
POSTGRES_PASSWORD=$(random_hex 24)
ANALYTICS_READER_PASSWORD=$(random_hex 24)
MINIO_ROOT_USER=commerce_storage
MINIO_ROOT_PASSWORD=$(random_hex 24)
APP_SECRET_KEY=$(random_hex 32)
APP_ENCRYPTION_KEY=$(random_b64 32)
SUPERSET_SECRET_KEY=$(random_hex 42)
SUPERSET_GUEST_TOKEN_SECRET=$(random_hex 42)
SUPERSET_ADMIN_USERNAME=admin
SUPERSET_ADMIN_PASSWORD=$(random_hex 24)
SUPERSET_ADMIN_EMAIL=admin@example.invalid
AI_MODE=disabled
LITELLM_MASTER_KEY=$(random_hex 24)
LITELLM_SALT_KEY=$(random_hex 24)
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
BACKEND_PORT=8000
WEB_PORT=3000
SUPERSET_PORT=8088
MINIO_API_PORT=9000
MINIO_CONSOLE_PORT=9001
POSTGRES_PORT=5432
LITELLM_PORT=4000
S3_REGION=us-east-1
S3_RAW_BUCKET=commerce-raw
S3_INTERMEDIATE_BUCKET=commerce-intermediate
S3_EXPORT_BUCKET=commerce-exports
PUBLIC_API_BASE_URL=/api/v1
EOF
