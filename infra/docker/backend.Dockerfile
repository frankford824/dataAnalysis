FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/pyproject.toml /app/pyproject.toml
COPY backend/app/__init__.py /app/app/__init__.py
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -f requirements.txt ]; then pip install -r requirements.txt; \
    elif [ -f pyproject.toml ]; then pip install '.'; \
    else echo "backend requires requirements.txt or pyproject.toml" >&2; exit 1; fi

COPY backend/ /app/
COPY infra/docker/backend-entrypoint.sh /app/infra/docker/backend-entrypoint.sh
RUN chmod 0555 /app/infra/docker/backend-entrypoint.sh \
    && useradd --system --uid 10001 --create-home app \
    && chown -R app:app /app

USER app
EXPOSE 8000
ENTRYPOINT []
CMD ["/app/infra/docker/backend-entrypoint.sh", "api"]
