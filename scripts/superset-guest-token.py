#!/usr/bin/env python3
"""Mint a short-lived demo token with mandatory enterprise/store RLS.

Production code must do this only after application RBAC and must never expose
the signing secret to a browser or desktop client.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
import uuid


def encoded(value: dict[str, object]) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def valid_uuid(value: str) -> str:
    return str(uuid.UUID(value))


parser = argparse.ArgumentParser()
parser.add_argument("--enterprise-id", required=True, type=valid_uuid)
parser.add_argument("--store-id", action="append", default=[], type=valid_uuid)
parser.add_argument("--dashboard-id", default="741fec6d-5c6b-4f81-8df2-ec59cf16fb55")
parser.add_argument("--user", default="embedded-demo")
parser.add_argument("--ttl", type=int, default=300)
args = parser.parse_args()

if not 1 <= args.ttl <= 600:
    parser.error("--ttl must be between 1 and 600 seconds")
secret = os.environ.get("SUPERSET_GUEST_TOKEN_SECRET")
if not secret:
    parser.error("SUPERSET_GUEST_TOKEN_SECRET is required")

tenant_clause = f"enterprise_id = '{args.enterprise_id}'"
if args.store_id:
    stores = ",".join(f"'{store_id}'" for store_id in args.store_id)
    tenant_clause += f" AND store_id IN ({stores})"

now = int(time.time())
header = encoded({"alg": "HS256", "typ": "JWT"})
payload = encoded(
    {
        "user": {"username": args.user, "first_name": "Embedded", "last_name": "Viewer"},
        "resources": [{"type": "dashboard", "id": args.dashboard_id}],
        "rls_rules": [{"clause": tenant_clause}],
        "iat": now,
        "exp": now + args.ttl,
        "aud": "commerce-analytics",
        "type": "guest",
    }
)
signature = base64.urlsafe_b64encode(
    hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
).rstrip(b"=").decode()
print(f"{header}.{payload}.{signature}")
