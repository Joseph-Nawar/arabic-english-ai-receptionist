# Phase 0 — Engineering Foundation & Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a small, production-style modular-monolith foundation for the Arabic-English AI Receptionist project, ending Phase 0 in `REVIEW` with no Phase 1 behavior.

**Architecture:** A FastAPI application factory owns process-lifetime logging and SQLAlchemy async engine/session-factory resources. Health routes remain thin; liveness has no database dependency and readiness performs a bounded `SELECT 1`. Alembic is wired to empty declarative metadata without inventing a migration revision.

**Tech Stack:** Python 3.12.15, uv, FastAPI, Uvicorn, Pydantic v2/pydantic-settings, SQLAlchemy async with Psycopg 3, Alembic, PostgreSQL 18.6, pytest/HTTPX, Ruff, mypy, pip-audit, detect-secrets, Docker Compose, and GitHub Actions.

**Spec:** User-supplied Phase 0 requirements in the implementation request; durable project decisions are recorded in `docs/roadmap.md` and `docs/architecture/`.

## Global Constraints

- Use Python `3.12.15` and a committed `uv.lock`.
- Use Hatchling only as the build backend and the approved dependency ranges.
- Keep the application a modular monolith with explicit, small code.
- Do not create future domain, provider, adapter, repository, event-bus, UI, or authentication scaffolding.
- Implement only `/health/live` and `/health/ready`.
- Do not run migrations automatically during application startup.
- Never expose database credentials in settings representations, logs, responses, or committed files.
- Phase 0 closes as `REVIEW`, never `COMPLETE`.

## Review Focus

- Missing or malformed configuration must fail clearly without reading a developer `.env` during isolated unit tests — `tests/unit/test_config.py`.
- A temporarily unavailable PostgreSQL must not block app construction or liveness, while readiness must fail safely and recover without an app restart — `tests/unit/test_app.py` and `tests/integration/test_readiness.py`.
- Alembic must support an empty-schema upgrade/downgrade cycle without a fake revision — `tests/integration/test_migrations.py`.
- Destructive test setup must refuse the development database — `tests/unit/test_config.py`.
- The normal secret check must inspect untracked non-ignored files and fail closed — manual verification plus `make secrets`.

---

### Task 1: Repository plan, project metadata, and safe configuration

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`, `.dockerignore`, `.env.example`, `README.md`, `Makefile`
- Create: `src/receptionist/__init__.py`, `src/receptionist/core/__init__.py`, `src/receptionist/core/config.py`, `src/receptionist/core/logging.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces `Settings`, `get_settings()`, `safe_summary()`, logging configuration, locked project metadata, and transparent Make targets.

- [ ] Write failing settings tests for valid loading, missing DB configuration, invalid environment/log level, `.env` isolation, safe repr/log output, and non-test database guard.
- [ ] Run the focused tests and observe expected missing-module/behavior failures.
- [ ] Implement Pydantic Settings with `RECEPTIONIST_` prefix, explicit `_env_file=None` test control, `SecretStr`/non-repr database URL treatment, and the narrowly scoped test-database guard.
- [ ] Add approved dependencies, tool configuration, and Make targets without a migration-generation target.
- [ ] Run focused unit tests and the configured linter/formatter checks.
- [ ] Commit the repository/configuration foundation.

### Task 2: Database resources, application factory, and health behavior

**Files:**
- Create: `src/receptionist/db/__init__.py`, `src/receptionist/db/base.py`, `src/receptionist/db/session.py`
- Create: `src/receptionist/api/__init__.py`, `src/receptionist/api/health.py`, `src/receptionist/main.py`
- Test: `tests/unit/test_app.py`

**Interfaces:**
- Consumes `Settings` from Task 1.
- Produces `create_app(settings: Settings | None = None) -> FastAPI`, module-level `app`, async engine/session-factory lifecycle, `SELECT 1` readiness probing, and only the two health endpoints.

- [ ] Write failing tests for explicit-settings app construction, HTTP 200 liveness, and liveness without database connectivity.
- [ ] Run the focused tests and observe expected failures.
- [ ] Implement the minimal application factory, FastAPI lifespan, async SQLAlchemy resources, safe logging, and health routes.
- [ ] Add bounded readiness probe behavior and clean engine disposal without startup migrations/provider initialization.
- [ ] Run unit tests and inspect liveness behavior.
- [ ] Commit application/runtime behavior.

### Task 3: Alembic, Compose infrastructure, and integration tests

**Files:**
- Create: `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/.gitkeep`, `compose.yaml`
- Create: `tests/conftest.py`, `tests/integration/conftest.py`, `tests/integration/test_database.py`, `tests/integration/test_migrations.py`, `tests/integration/test_readiness.py`
- Modify: `Makefile`, `pyproject.toml`

**Interfaces:**
- Consumes database engine/base and test-database guard from Tasks 1–2.
- Produces separate development/test PostgreSQL services, real PostgreSQL tests, and an empty-metadata Alembic upgrade/downgrade/upgrade path.

- [ ] Write integration tests for PostgreSQL connectivity, clean disposal, readiness 200/503/recovery, and Alembic upgrade → downgrade → upgrade.
- [ ] Run them against the isolated test service and observe expected infrastructure/implementation failures.
- [ ] Implement Alembic settings/metadata wiring with no revision file, Compose services for `receptionist` and `receptionist_test`, and test fixtures that enforce the guard before destructive operations.
- [ ] Run the full integration test set and verify the development DB is not used for destructive setup.
- [ ] Commit database infrastructure and integration coverage.

### Task 4: Security tooling, Docker, CI, and documentation

**Files:**
- Create: `Dockerfile`, `.secrets.baseline`, `.github/workflows/ci.yml`, `.github/dependabot.yml`
- Create: `AGENTS.md`, `docs/roadmap.md`, `docs/architecture/system-overview.md`, `docs/architecture/integration-boundaries.md`, `docs/development/setup.md`, `docs/development/testing.md`
- Modify: `README.md`, `Makefile`, `pyproject.toml`

**Interfaces:**
- Consumes all runtime/tooling interfaces from Tasks 1–3.
- Produces reproducible local/CI/Docker workflows, truthful project documentation, fail-closed secret scanning, and Phase 0 status `REVIEW`.

- [ ] Add the security and CI configuration with immutable action SHAs, minimum token permissions, locked installs, PostgreSQL 18.6 service, and all required gates.
- [ ] Add the understandable non-root Python 3.12.15 Docker image with pinned uv tooling and no migration auto-run.
- [ ] Write the roadmap, architecture boundaries, development guides, AGENTS rules, and truthful README.
- [ ] Run secret scanner, audit, static checks, Compose validation, and image build; perform the temporary uncommitted fake-secret negative test and remove the file.
- [ ] Commit documentation and delivery tooling.

### Task 5: Final verification and closeout

**Files:**
- Modify: `docs/roadmap.md` only if verification evidence requires status correction.
- Record: final Git/diff/verification evidence in the closeout response.

- [ ] Run the complete unit/integration/quality/verification commands from a fresh local setup.
- [ ] Perform the database-down liveness/readiness/recovery check without restarting FastAPI.
- [ ] Inspect the final diff against the starting SHA for residue, secrets, speculative code, and scope violations.
- [ ] Commit only any necessary verification-driven fixes, then report Phase 0 as `REVIEW` pending independent human approval.

