# Testing and verification

## Boundaries

Unit tests live in `tests/unit`. They do not require Docker, PostgreSQL, network access, or external providers. They cover settings isolation/secret safety, app construction, and liveness.

Integration tests live in `tests/integration` and use real PostgreSQL. They cover SQLAlchemy/Psycopg connectivity and disposal, Alembic upgrade → downgrade → upgrade, and readiness behavior. The normal CI job provides PostgreSQL as a service and does not call external SaaS providers.

## Test database and destruction guard

Start the isolated database before integration tests:

```sh
make test-db-up
make test-integration
make test-db-down
```

Any destructive test setup must call `assert_safe_test_database` first. It requires `RECEPTIONIST_APP_ENV` to be `test` or `ci` and the parsed database name to be exactly `receptionist_test`; the normal development database is refused. Phase 0 uses this guard around the migration reset cycle and does not add general cleanup machinery.

## Markers and commands

Pytest uses strict configuration and markers:

```sh
make test-unit
make test-integration
make test
```

`unit` and `integration` are explicit markers. Async behavior is predictable through `pytest-asyncio` auto mode. `pytest-cov` is installed for useful coverage reporting, but Phase 0 intentionally has no global coverage-percentage failure threshold; behavior and acceptance criteria are the gate.

## Alembic verification

There is no application revision yet. `make alembic-verify` proves that `upgrade head`, `downgrade base`, and `upgrade head` work against a clean test database without inventing a fake empty revision. `make migrate` applies the current head to the development database.

## Quality and security

```sh
make check
make security
make verify
```

These run Ruff lint/format checks, strict mypy for `src/`, the blocking detect-secrets hook, pip-audit, tests, integration/Alembic verification, Compose validation, and the Docker build. Secret scanning includes tracked and untracked non-ignored working-tree files locally.

