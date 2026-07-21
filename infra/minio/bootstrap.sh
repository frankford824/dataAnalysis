#!/bin/sh
set -eu

mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"

# Raw inputs are versioned and object-locked so a retry can never overwrite evidence.
mc mb --ignore-existing --with-lock "local/$S3_RAW_BUCKET"
mc version enable "local/$S3_RAW_BUCKET"
mc mb --ignore-existing "local/$S3_INTERMEDIATE_BUCKET"
mc version enable "local/$S3_INTERMEDIATE_BUCKET"
mc mb --ignore-existing "local/$S3_EXPORT_BUCKET"
mc version enable "local/$S3_EXPORT_BUCKET"

# Anonymous access is explicitly disabled on every product bucket.
mc anonymous set none "local/$S3_RAW_BUCKET"
mc anonymous set none "local/$S3_INTERMEDIATE_BUCKET"
mc anonymous set none "local/$S3_EXPORT_BUCKET"
