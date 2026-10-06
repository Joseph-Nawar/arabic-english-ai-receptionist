UV ?= uv
DATABASE_URL ?= postgresql+psycopg://receptionist:receptionist@localhost:5432/receptionist
TEST_DATABASE_URL ?= postgresql+psycopg://receptionist:receptionist@localhost:55432/receptionist_test

.PHONY: install dev db-up db-down test-db-up test-db-down migrate test test-unit \
	test-integration lint format format-check typecheck secrets audit security \
	alembic-verify docker-config docker-build check verify

install:
	$(UV) sync --all-groups --frozen

dev:
	$(UV) run uvicorn receptionist.main:app --reload

db-up:
	docker compose up -d db

db-down:
	docker compose stop db
	docker compose rm -f db

test-db-up:
	docker compose --profile test up -d test-db

test-db-down:
	docker compose --profile test stop test-db
	docker compose --profile test rm -f test-db

migrate:
	RECEPTIONIST_APP_ENV=local RECEPTIONIST_DATABASE_URL="$(DATABASE_URL)" $(UV) run alembic upgrade head

test:
	RECEPTIONIST_APP_ENV=test RECEPTIONIST_DATABASE_URL="$(TEST_DATABASE_URL)" $(UV) run pytest

test-unit:
	$(UV) run pytest -m unit tests/unit

test-integration:
	RECEPTIONIST_APP_ENV=test RECEPTIONIST_DATABASE_URL="$(TEST_DATABASE_URL)" $(UV) run pytest -m integration tests/integration

alembic-verify:
	RECEPTIONIST_APP_ENV=test RECEPTIONIST_DATABASE_URL="$(TEST_DATABASE_URL)" $(UV) run python -c 'from alembic import command; from alembic.config import Config; from receptionist.core.config import Settings, assert_safe_test_database; settings = Settings(_env_file=None); assert_safe_test_database(settings); config = Config("alembic.ini"); command.upgrade(config, "head"); command.downgrade(config, "base"); command.upgrade(config, "head")'

lint:
	$(UV) run ruff check .

format:
	$(UV) run ruff format .

format-check:
	$(UV) run ruff format --check .

typecheck:
	$(UV) run mypy src

secrets:
	git ls-files -co --exclude-standard -z | xargs -0 $(UV) run detect-secrets-hook --baseline .secrets.baseline

audit:
	$(UV) run pip-audit

security: lint secrets audit

docker-config:
	docker compose config

docker-build:
	docker build -t arabic-english-ai-receptionist:phase-0 .

check: format-check lint typecheck secrets audit test-unit

verify: check test-integration alembic-verify docker-config docker-build
