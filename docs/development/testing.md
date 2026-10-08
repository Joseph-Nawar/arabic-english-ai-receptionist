# Testing and verification

## Boundaries

Unit tests live in `tests/unit`. They do not require Docker, PostgreSQL, network access, or external providers. They cover settings isolation/secret safety, app construction, and liveness. The shared pytest configuration blocks `httplib2` network transport, so an accidental live Google Calendar request fails immediately.

Integration tests live in `tests/integration` and use real PostgreSQL. They cover SQLAlchemy/Psycopg connectivity and disposal, Alembic upgrade → downgrade → upgrade, and readiness behavior. The normal CI job provides PostgreSQL as a service and does not call external SaaS providers.

All normal automated verification is provider-free: `make test`, `make test-unit`,
`make test-integration`, `make check`, `make verify`, and GitHub CI do not require
Calendar credentials and do not make live Google Calendar, CRM, messaging, voice,
LLM, or other SaaS calls. Booking integration tests inject the deterministic
Calendar double; Google request translation is tested with mocked service and
transport objects. Live Google verification is a separate guarded Task 16 smoke
operation and is not part of these commands.

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

Phase 2 automated tests remain provider-free. The deterministic Calendar double covers booking operations and recovery paths, PostgreSQL integration tests cover durable concurrency and transaction behavior, and the shared `httplib2` guard blocks accidental live Calendar network access. Raw Google payloads, ETags, credentials, and customer PII are tested at the application boundary and are not durable booking data.

`make migration-check` and `make alembic-verify` cover migration drift and the guarded upgrade → downgrade → upgrade cycle. Repeated integration runs use the guarded `receptionist_test` database and isolate their own state without a blanket schema-reset fixture. The non-mutating `make secrets` target scans tracked and untracked non-ignored files against a temporary baseline copy; it must not modify `.secrets.baseline`.

The separate `make calendar-smoke` command is a manual, explicitly guarded check against a dedicated non-production Calendar only. It proves the real free/busy, create, same-key replay/recovery, retrieval, reschedule, cancellation, reconciliation, and exact cleanup path. It is never part of CI, `make test`, `make check`, or `make verify`. If dedicated credentials are unavailable, report exactly `not run: dedicated Calendar credentials unavailable`; do not claim a live pass.

The destructive migration cycle remains protected by `assert_safe_test_database`; it refuses non-test environments and any database name other than `receptionist_test`. The seed command is a thin invocation of the synthetic reference seed and has its own production refusal.

## Quality and security

```sh
make check
make security
make verify
```

These run Ruff lint/format checks, strict mypy for `src/`, the blocking detect-secrets hook, pip-audit, tests, integration/Alembic verification, Compose validation, and the Docker build. Secret scanning includes tracked and untracked non-ignored working-tree files locally.
