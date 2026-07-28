FROM node:22-bookworm-slim AS web-build

WORKDIR /build/web
COPY harness/web/package.json harness/web/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY harness/web/ ./
RUN npm test -- --run \
    && npm run typecheck \
    && npm run build

FROM python:3.12-slim-bookworm AS runtime

ARG HARNESS_UID=1000
ARG HARNESS_GID=1000
ARG HARNESS_GIT_SHA=uncommitted-docker-build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/harness \
    FA_SOURCE_READONLY_ENFORCED=1 \
    FA_CONTAINER_BIND=1 \
    FA_WEB_DIST=/app/harness/web/dist \
    FA_CODE_IDENTITY_FILE=/app/.fa-code-identity.json \
    PYTHONPATH=/app/harness:/app/host-agent

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${HARNESS_GID}" harness \
    && useradd --uid "${HARNESS_UID}" --gid harness \
        --create-home --home-dir /home/harness harness \
    && install -d -m 0700 -o harness -g harness /home/harness/.ssh \
    && install -d -m 0755 -o root -g root /run/ssh \
    && install -m 0600 -o harness -g harness /dev/null /run/ssh/config \
    && install -m 0600 -o harness -g harness /dev/null /run/ssh/finance_agent_deploy \
    && install -m 0600 -o harness -g harness /dev/null /run/ssh/known_hosts

WORKDIR /app
COPY harness/ /app/harness/
COPY host-agent/ /app/host-agent/
COPY --from=web-build /build/web/dist/ /app/harness/web/dist/
RUN HARNESS_GIT_SHA="${HARNESS_GIT_SHA}" python - <<'PY'
import hashlib
import json
import os
from pathlib import Path

digest = hashlib.sha256()
roots = (
    Path("/app/harness/commerce_harness"),
    Path("/app/harness/pyproject.toml"),
    Path("/app/host-agent/finance_agent"),
    Path("/app/host-agent/pyproject.toml"),
)
paths = []
for root in roots:
    paths.extend(root.rglob("*") if root.is_dir() else (root,))
for path in sorted(paths):
    if not path.is_file() or "__pycache__" in path.parts:
        continue
    relative = path.relative_to("/app").as_posix()
    digest.update(relative.encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.read_bytes())
payload = {
    "commit_sha": os.environ["HARNESS_GIT_SHA"],
    "worktree_sha256": digest.hexdigest(),
}
Path("/app/.fa-code-identity.json").write_text(
    json.dumps(payload, sort_keys=True),
    encoding="utf-8",
)
PY
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install /app/host-agent /app/harness \
    && chmod +x /app/harness/docker-entrypoint.sh

USER harness:harness
EXPOSE 8765
HEALTHCHECK --interval=10s --timeout=10s --start-period=20s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/readyz', timeout=8).read()"]

ENTRYPOINT ["/app/harness/docker-entrypoint.sh"]
