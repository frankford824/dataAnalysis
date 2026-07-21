from __future__ import annotations

from celery import Celery

from .config import get_settings


settings = get_settings()
celery_app = Celery("commerce_analytics", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    task_track_started=True,
    broker_connection_retry_on_startup=True,
)


@celery_app.task(name="health.ping")
def ping() -> dict[str, str]:
    return {"status": "ok"}
