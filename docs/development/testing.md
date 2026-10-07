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

Any destructive test setup must call `assert_safe_test_database` first. It requires `RECEPTIONIST_APP_ENV` to be `test` or `ci` and the parsed database name to be exactly `receptionist_test`; the normal development database is refused. Phase 1 keeps this guard around migration verification and does not add general cleanup machinery.

## Phase 1 identity and persistence behavior

Usable phone numbers are normalized with libphonenumber to canonical E.164 values before Contact resolution. A missing or withheld caller creates a new Contact with a NULL phone identity every time; NULL identities are never reused, and resolution never matches by display name or email. Non-null identity concurrency is delegated to PostgreSQL’s partial unique index and conflict-safe insert. The helper does not commit or own the caller’s broad transaction.

Normal integration cases use isolated sessions and roll back after expected constraint failures. The same-phone concurrency case intentionally uses two independent sessions, worker-owned outer transactions, and commits each worker before it returns; it uses a bounded timeout and no sleeps or application locks.

## Markers and commands

Pytest uses strict configuration and markers:

```sh
make test-unit
make test-integration
make test
```

`unit` and `integration` are explicit markers. Async behavior is predictable through `pytest-asyncio` auto mode. `pytest-cov` is installed for useful coverage reporting, but Phase 0 intentionally has no global coverage-percentage failure threshold; behavior and acceptance criteria are the gate.

## Alembic verification

Phase 1 includes the first real Alembic revision. `make alembic-verify` proves that `upgrade head`, `downgrade base`, and `upgrade head` work against a clean guarded test database. `make migrate` applies the current head to the development database.

Migration and seed commands:

```sh
make migration MSG="describe the schema change"
make migration-check
make seed
```

`make migration` rejects an empty `MSG` before invoking Alembic. The supported autogeneration path produced the Phase 1 revision; verification must not create a disposable revision solely to test this target. `make migration-check` validates the existing test-database guard, upgrades only the guarded `receptionist_test` database to head, and runs `alembic check` to detect metadata drift. Set `TEST_DATABASE_URL` explicitly when the isolated PostgreSQL service uses a non-default port.

Booking is provider-independent in Phase 1 and has no Calendar identifiers or provider calls. ToolExecution, ProviderEventReceipt, and OutboxEvent are persistence/state primitives only: there is no tool dispatcher, outbox publisher, provider adapter, webhook handler, workflow engine, or external provider call in this phase.

The destructive migration cycle remains protected by `assert_safe_test_database`; it refuses non-test environments and any database name other than `receptionist_test`. The seed command is a thin invocation of the synthetic reference seed and has its own production refusal.

## Quality and security

```sh
make check
make security
make verify
```

These run Ruff lint/format checks, strict mypy for `src/`, the blocking detect-secrets hook, pip-audit, tests, integration/Alembic verification, Compose validation, and the Docker build. Secret scanning includes tracked and untracked non-ignored working-tree files locally.
