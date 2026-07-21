from __future__ import annotations

import os

from cachelib.redis import RedisCache
from celery.schedules import crontab


SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]
SQLALCHEMY_DATABASE_URI = os.environ["SUPERSET_METADATA_DATABASE_URI"]

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
    "CACHE_KEY_PREFIX": "superset_metadata_",
    "CACHE_REDIS_HOST": REDIS_HOST,
    "CACHE_REDIS_PORT": REDIS_PORT,
    "CACHE_REDIS_DB": 2,
}
DATA_CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 900,
    "CACHE_KEY_PREFIX": "superset_data_",
    "CACHE_REDIS_HOST": REDIS_HOST,
    "CACHE_REDIS_PORT": REDIS_PORT,
    "CACHE_REDIS_DB": 3,
}
RATELIMIT_STORAGE_URI = f"redis://{REDIS_HOST}:{REDIS_PORT}/6"

FEATURE_FLAGS = {
    "EMBEDDED_SUPERSET": True,
    "DASHBOARD_RBAC": True,
    "ENABLE_TEMPLATE_PROCESSING": False,
}
GUEST_ROLE_NAME = "EmbeddedViewer"
GUEST_TOKEN_JWT_SECRET = os.environ["SUPERSET_GUEST_TOKEN_SECRET"]
GUEST_TOKEN_JWT_AUDIENCE = "commerce-analytics"
GUEST_TOKEN_JWT_EXP_SECONDS = 300

# The platform backend mints short-lived guest tokens after checking its own
# enterprise/store RBAC. No permissions are assigned to Superset's default
# anonymous role; embedded viewers use the explicit role above.

WTF_CSRF_ENABLED = True
WTF_CSRF_TIME_LIMIT = None
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = os.getenv("APP_ENV", "development") == "production"
ENABLE_CORS = False
TALISMAN_ENABLED = False  # TLS/CSP terminates at the customer's trusted reverse proxy.
CONTENT_SECURITY_POLICY_WARNING = False
ENABLE_PROXY_FIX = True
ROW_LIMIT = 50_000
SQLLAB_CTAS_NO_LIMIT = False
SUPERSET_WEBSERVER_TIMEOUT = 120


class CeleryConfig:
    broker_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/4"
    result_backend = RedisCache(host=REDIS_HOST, port=REDIS_PORT, db=5)
    imports = ("superset.sql_lab", "superset.tasks.scheduler")
    worker_prefetch_multiplier = 1
    task_acks_late = True
    beat_schedule = {
        "reports.scheduler": {
            "task": "reports.scheduler",
            "schedule": crontab(minute="*", hour="*"),
        }
    }


CELERY_CONFIG = CeleryConfig
