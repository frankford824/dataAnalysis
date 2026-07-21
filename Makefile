.PHONY: help configure validate up down logs migrate seed test smoke backup sbom offline clean

help:
	@echo "configure  create a local .env"
	@echo "up         build and start the core stack (AI remains disabled)"
	@echo "test       run backend and web tests"
	@echo "smoke      check running services"
	@echo "backup     back up databases, objects, and configuration"
	@echo "sbom       generate SPDX dependency inventory"

configure:
	@test -f .env || cp .env.example .env

validate:
	./scripts/validate-config.sh .env
	docker compose config --quiet

up: configure validate
	docker compose up -d --build postgres redis minio minio-bootstrap backend worker scheduler superset web

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

migrate:
	docker compose run --rm backend /app/infra/docker/backend-entrypoint.sh migrate

seed:
	docker compose run --rm backend /app/infra/docker/backend-entrypoint.sh seed

test:
	docker compose run --rm --no-deps backend pytest
	cd apps/web && npm test -- --run

smoke:
	./scripts/smoke-test.sh

backup:
	./scripts/backup.sh

sbom:
	./scripts/generate-sbom.sh

offline:
	./scripts/offline-pack.sh

clean:
	docker compose down --remove-orphans
