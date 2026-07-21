#!/bin/sh
set -eu

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${ANALYTICS_READER_PASSWORD:?ANALYTICS_READER_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-SQL
SELECT 'CREATE DATABASE superset' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'superset')\gexec
SELECT 'CREATE DATABASE litellm' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'litellm')\gexec
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=reader_password="$ANALYTICS_READER_PASSWORD" \
  --set=app_database="$POSTGRES_DB" <<-'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analytics_reader') THEN
    CREATE ROLE analytics_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END
$$;
ALTER ROLE analytics_reader PASSWORD :'reader_password';
REVOKE ALL ON DATABASE :"app_database" FROM analytics_reader;
GRANT CONNECT ON DATABASE :"app_database" TO analytics_reader;
CREATE SCHEMA IF NOT EXISTS certified;
REVOKE ALL ON SCHEMA public FROM analytics_reader;
GRANT USAGE ON SCHEMA certified TO analytics_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA certified TO analytics_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA certified GRANT SELECT ON TABLES TO analytics_reader;
ALTER ROLE analytics_reader SET default_transaction_read_only = on;
ALTER ROLE analytics_reader SET statement_timeout = '60s';
SQL
