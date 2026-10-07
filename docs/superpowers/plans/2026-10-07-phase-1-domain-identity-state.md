# Phase 1 — Business Domain, Identity & State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved Phase 1 business domain, identity, durable state primitives, PostgreSQL schema, synthetic seed, and verification gates, ending the phase in `REVIEW` without adding Phase 2 behavior.

**Architecture:** Keep the existing FastAPI/PostgreSQL modular monolith. Add a real `domain/` package for strict configuration, enums, pure identity/state behavior, and an explicit SQLAlchemy model module for the first durable schema. Keep transaction ownership at callers, use PostgreSQL uniqueness/upsert semantics for contact concurrency, and keep future provider seams as persistence records only.

**Tech Stack:** Python `3.12.15`, uv `0.12.22`, Pydantic v2, `phonenumbers>=9.0.40,<10`, SQLAlchemy 2.1 async, Psycopg 3, PostgreSQL `18.6`, Alembic, pytest/pytest-asyncio, Ruff, strict mypy, detect-secrets, pip-audit, Docker Compose, and the existing single CI workflow.

**Spec:** Approved Phase 1 requirements supplied in the implementation request; durable architecture in `docs/architecture/domain-model.md` at approved commit `c1cb342`, together with `docs/roadmap.md`, `docs/architecture/system-overview.md`, and `docs/architecture/integration-boundaries.md`.

## Global Constraints

- Keep the project a modular monolith and keep routes thin; no service-layer or framework refactor.
- Preserve the single-business design: `BusinessConfig.id = 1`; no tenant, organization, `tenant_id`, or membership model.
- Add exactly one Phase 1 runtime dependency: `phonenumbers>=9.0.40,<10`; update `uv.lock` with uv `0.12.22` and preserve `[tool.uv] required-version`.
- Use application-generated UUID4 values with PostgreSQL UUID columns for normal domain entities.
- Persist event timestamps as timezone-aware PostgreSQL `TIMESTAMPTZ`; business hours remain local wall-clock values interpreted in the configured IANA timezone.
- Use Python `StrEnum` values persisted as constrained strings/VARCHAR via SQLAlchemy `Enum(..., native_enum=False, create_constraint=True)` or an equivalent explicit check-constrained string; never PostgreSQL native ENUM types.
- Use JSONB only for the explicitly listed structured configuration/state documents, and validate those documents with strict Pydantic models with unknown fields forbidden.
- Use restrictive foreign keys for historical relationships; do not add broad `ON DELETE CASCADE` behavior or deletion workflows.
- Do not add generic repositories, unit-of-work abstractions, provider adapters/registries, Protocols, event-bus frameworks, workflow/state-machine libraries, or speculative interfaces.
- `Booking`, `ToolExecution`, `ProviderEventReceipt`, and `OutboxEvent` are persistence/state primitives only. There is no booking workflow, tool dispatcher, provider handling, webhook route, outbox publisher/worker, queue, or retry scheduler.
- PostgreSQL uniqueness and conflict handling are the contact identity concurrency authority; `resolve_or_create_contact` must not use `SELECT -> if absent -> INSERT` or hide broad commits.
- Ordinary application logs must use opaque IDs and must not log full phone numbers, emails, addresses, conversation text, booking payloads, raw provider payloads, credentials, or secrets.
- Destructive integration-test operations must call `assert_safe_test_database` and be limited to `test`/`ci` with database name `receptionist_test`.
- Normal CI must remain provider-free and must retain Ruff lint/format, strict mypy, fail-closed secret scan, pip-audit, unit tests, PostgreSQL integration tests, locked dependencies, and PostgreSQL `18.6`.
- Phase 1 begins with roadmap status `IN PROGRESS` and may end only at `REVIEW`; Codex must not mark it `COMPLETE`, merge, create a PR, or begin Phase 2.

## Review Focus

- A missing, blank, or withheld caller number must create a fresh unresolved Contact every time and never reuse a NULL-phone row — Task 6 tests `resolve_or_create_contact` with independent sessions.
- Strict configuration and JSONB structures must reject unknown fields and invalid pricing/catalog combinations before persistence — Task 2 and Task 10 unit tests.
- A direct invalid enum value or malformed time/pending-action/booking pairing must be rejected by PostgreSQL constraints, not only by Python validation — Task 4 metadata tests and Task 11 integration tests.
- Same-number resolution across independent transactions must converge on one Contact without fragile sleeps or a hidden commit — Task 6 and Task 11 integration tests.
- The reference seed must refuse `production`, remain synthetic and PII-conscious, and be idempotent across repeated runs — Task 8 unit/integration tests and Task 15 manual verification.

## File map and ownership

Keep these focused boundaries:

- `src/receptionist/domain/enums.py`: approved controlled vocabularies only.
- `src/receptionist/domain/config.py`: strict business/service configuration structures and catalog validation.
- `src/receptionist/domain/identity.py`: pure phone normalization and its domain error.
- `src/receptionist/domain/state.py`: only control-mode and pending-action pure helpers.
- `src/receptionist/db/models.py`: the concrete SQLAlchemy records, constraints, and operational indexes; no repository layer.
- `src/receptionist/db/contact_identity.py`: the one conflict-safe Contact resolution operation.
- `src/receptionist/seed.py`: the one synthetic reference seed path and CLI entrypoint.
- `alembic/versions/<revision>_initial_domain_schema.py`: the reviewed first real schema revision.
- `tests/unit/`: deterministic pure/domain/model metadata behavior.
- `tests/integration/`: real PostgreSQL constraints, migrations, identity isolation, and seed behavior.

Do not modify the approved `docs/architecture/domain-model.md` during implementation unless an independently reviewed architecture change is requested.

---

### Task 1: Add the Phase 1 dependency and lock/tooling baseline

**Depends on:** Approved Phase 1 design commit `c1cb342`.

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test/verification only: no production source files

**Purpose:** Make libphonenumber available through the approved locked toolchain before any domain code uses it.

**Behavior and constraints:**
- Add exactly `phonenumbers>=9.0.40,<10` to `[project].dependencies`.
- Preserve Python `>=3.12,<3.13` and `[tool.uv] required-version = ">=0.12.22,<0.13"`.
- Regenerate `uv.lock` with uv `0.12.22`; do not hand-edit dependency resolution data.
- Do not add email, country, timezone, ORM-helper, state-machine, or provider libraries.

**Acceptance criteria:**
- `pyproject.toml` contains only the approved new runtime dependency.
- `uv.lock` is consistent and resolves `phonenumbers` within the required range.
- No source or future-phase integration code is added.

**Verification:**

- [ ] Run `uv lock --check`.
- [ ] Run `uv sync --frozen --all-groups`.
- [ ] Run `uv run python -c 'import phonenumbers; print(phonenumbers.__version__)'` and confirm a 9.x version.
- [ ] Commit as `build: add locked phone normalization dependency`.

---

### Task 2: Add controlled vocabularies and strict Pydantic domain configuration

**Depends on:** Task 1.

**Files:**
- Create: `src/receptionist/domain/__init__.py`
- Create: `src/receptionist/domain/enums.py`
- Create: `src/receptionist/domain/config.py`
- Test: `tests/unit/test_domain_config.py`

**Purpose:** Define the approved domain vocabulary and validate all business/service configuration before it reaches JSONB or relational persistence.

**Interfaces:**
- `src/receptionist/domain/enums.py` exports `StrEnum` classes: `AfterHoursMode`, `BusinessLanguage`, `Weekday`, `PricingMode`, `ConversationChannel`, `ConversationStatus`, `ControlMode`, `ConversationLanguageMode`, `ConversationOutcome`, `PendingActionType`, `PendingActionStatus`, `ConversationTurnRole`, `BookingStatus`, `HandoffStatus`, `HandoffReason`, `HandoffPriority`, `ToolExecutionStatus`, and `AuditActorType`.
- `src/receptionist/domain/config.py` exports strict Pydantic models: `BilingualText`, `TimeWindow`, `WeeklyHours`, `AfterHoursPolicySpec`, `ServiceAreaSpec`, `BookingPolicySpec`, `HandoffPolicySpec`, `RetentionPolicySpec`, `PricingSpec`, `BookingRequirementSpec`, `ServiceEscalationSpec`, `BusinessConfigSpec`, and `ServiceSpec`.
- `src/receptionist/domain/config.py` exports `normalize_catalog_text(value: str) -> str`, `validate_service_areas(areas: Sequence[ServiceAreaSpec]) -> None`, and `validate_service_catalog(services: Sequence[ServiceSpec]) -> None`.

**Behavior and constraints:**
- Every Pydantic model uses `extra="forbid"`; structured values are strict and JSON-serializable.
- Pin the enum values exactly: `AfterHoursMode` = `handoff|callback|closed_message`; `BusinessLanguage` = `en|ar`; `PricingMode` = `fixed|from|range|quote_required|not_published`; `ConversationChannel` = `phone|whatsapp`; `ConversationStatus` = `open|closed`; `ControlMode` = `ai|handoff_pending|human`; `ConversationLanguageMode` = `unknown|en|ar|mixed`; `ConversationOutcome` = `information_only|booked|rescheduled|cancelled|handoff|unresolved|abandoned`; `PendingActionType` = `create_booking|reschedule_booking|cancel_booking`; `PendingActionStatus` = `awaiting_confirmation|confirmed`; `ConversationTurnRole` = `customer|assistant|human|system`; `BookingStatus` = `pending|confirmed|cancelled`; `HandoffStatus` = `pending|accepted|resolved|cancelled`; `HandoffPriority` = `normal|high|urgent`; `HandoffReason` = `explicit_request|repeated_misunderstanding|low_confidence|unknown_information|complaint|unusual_or_high_risk|urgent_or_emergency|integration_failure|cannot_safely_act`; `ToolExecutionStatus` = `started|succeeded|failed|rejected`; and `AuditActorType` = `system|customer|assistant|human|provider`.
- Define `Weekday` as the seven lowercase weekday names and use it only for weekly-hours configuration; no inferred locale-specific or speaker-language vocabulary is added.
- `BusinessConfigSpec` contains business name, IANA timezone, default phone region, uppercase three-letter currency, supported languages, default language, weekly hours, after-hours policy, service areas, booking policy, handoff policy, bilingual greetings/closings, and retention policy. It does not contain embedded Services.
- Supported business languages are limited to English and Arabic; `mixed` is only a conversation language mode, never a business preference.
- Validate timezone with `zoneinfo`, region against libphonenumber-supported region codes, currency format, and default-language membership.
- Validate weekly windows as same-day local times with `start < end`; empty days are closed; reject duplicate and overlapping windows; do not calculate availability or support cross-midnight windows.
- Validate areas by stable code, bilingual names, aliases, and active flag. Normalize only Unicode, whitespace, and case; reject duplicate/ambiguous names or aliases.
- Validate `BookingPolicySpec` with non-negative notice/buffers and positive maximum advance/slot increment.
- Validate retention fields as non-negative configurable day counts/flags without implementing deletion.
- Validate `PricingSpec` modes exactly as `fixed`, `from`, `range`, `quote_required`, `not_published`, using integer minor units and rejecting fields not allowed by the selected mode.
- Validate `ServiceSpec` with stable code, bilingual names/descriptions, aliases, active/bookable flags, positive duration, strict pricing, booking requirements, and small escalation configuration. Reject ambiguous service names/aliases.
- Keep handoff phone canonicalization wired in Task 3; Task 2 may validate the rest of `HandoffPolicySpec` without introducing an alternate phone parser.

**Tests to write first:**

- [ ] Add tests for valid/invalid timezone, invalid region, currency format, default-language membership, and forbidden unknown fields.
- [ ] Add tests for valid hours, empty closed day, overlap rejection, duplicate-window rejection, `start >= end`, and cross-midnight rejection.
- [ ] Add tests for duplicate/ambiguous service-area names/aliases and service-catalog aliases after basic normalization.
- [ ] Add tests for all five pricing modes, invalid mode/amount combinations, range minimum greater than maximum, and positive service duration.

**Acceptance criteria:**
- All approved enum values are defined once and are reusable by ORM mapping in Task 4.
- `BusinessConfigSpec` and `ServiceSpec` can be dumped to JSON-safe dictionaries for JSONB persistence.
- No Service is nested into `BusinessConfigSpec`; the authoritative catalog remains relational.
- No availability, pricing engine, escalation engine, workflow engine, or provider configuration is introduced.

**Verification:**

- [ ] Run the focused tests first and observe failure before implementing each behavior.
- [ ] Run `uv run pytest -m unit tests/unit/test_domain_config.py`.
- [ ] Run `uv run ruff check src/receptionist/domain tests/unit/test_domain_config.py` and `uv run mypy src/receptionist/domain`.
- [ ] Commit as `feat: add strict phase 1 domain configuration`.

---

### Task 3: Implement pure E.164 phone normalization

**Depends on:** Tasks 1–2.

**Files:**
- Create: `src/receptionist/domain/identity.py`
- Modify: `src/receptionist/domain/config.py`
- Test: `tests/unit/test_identity.py`

**Purpose:** Establish the only canonical phone identity function used by Contacts and handoff configuration.

**Interfaces:**
- `class PhoneNormalizationError(ValueError)` in `domain/identity.py`.
- `normalize_phone_number(raw_number: str, default_region: str | None) -> str` returns canonical E.164 or raises `PhoneNormalizationError`.

**Behavior and constraints:**
- Parse `+`-prefixed international numbers internationally.
- Require a valid explicit default region for national-format input.
- Reject parse failures, invalid/impossible numbers, blank values, and extensions.
- Return only libphonenumber-produced E.164; never compare raw phone strings.
- If `00` international-prefix behavior is supported by the library for the chosen input, test the observed behavior; do not add a custom parser.
- Wire `HandoffPolicySpec` to canonicalize a supplied handoff number through this function with international-only semantics (`default_region=None`), so a stored handoff number is E.164.
- Do not log the raw or canonical number.

**Tests to write first:**

- [ ] International number normalizes to expected E.164.
- [ ] National number with explicit region normalizes correctly.
- [ ] Missing/invalid region, invalid number, impossible number, and extension are rejected.
- [ ] Handoff policy accepts a valid international number and rejects a national/non-canonical number without a region.
- [ ] If `00` behavior is supported, assert the library’s actual result; otherwise do not invent behavior.

**Acceptance criteria:**
- Identity normalization is a pure function with no database/session dependency.
- All accepted phone identity values are E.164 strings.
- No manual country-code or telephone parsing exists.

**Verification:**

- [ ] Run the focused test file through a red-green cycle.
- [ ] Run `uv run pytest -m unit tests/unit/test_identity.py tests/unit/test_domain_config.py`.
- [ ] Commit as `feat: normalize phone identities as e164`.

---

### Task 4: Define concrete SQLAlchemy domain models and database constraints

**Depends on:** Tasks 2–3.

**Files:**
- Create: `src/receptionist/db/models.py`
- Modify: `src/receptionist/db/base.py` only for the model metadata docstring/import-safe setup if needed
- Create/modify: `tests/unit/test_models.py`

**Purpose:** Add the first real relational model surface without introducing repositories, mixins, or provider abstractions.

**Interfaces and records:**
- Export ORM classes `BusinessConfig`, `Service`, `Contact`, `Conversation`, `ConversationTurn`, `Booking`, `Handoff`, `ToolExecution`, `AuditEvent`, `ProviderEventReceipt`, and `OutboxEvent` from `receptionist.db.models`.
- Use explicit table names and explicit columns; do not create timestamp/database mixin hierarchies.
- Use application UUID4 defaults with PostgreSQL UUID columns for normal entities and timezone-aware timestamp columns for all event/lifecycle times.

**Behavior and constraints:**
- `BusinessConfig`: small-integer primary key with a check constraint restricting `id = 1`; persist validated structured configuration documents as JSONB; no `tenant_id` or Service embedding.
- `Service`: unique stable code, bilingual names/descriptions, aliases JSONB, active/bookable flags, positive duration check, pricing/booking-requirements/escalation JSONB.
- `Contact`: nullable `phone_e164`, display name, email, optional preferred language, timestamps; partial unique index on non-null phone.
- `Conversation`: Contact FK, channel/status/control/language/outcome constrained strings, bounded summary, direct pending-action columns, open/closed timestamp check, and requested operational indexes.
- Pending-action columns must enforce the approved nullability/status rules: all metadata null when no action; type/status/payload/created timestamp required when present; confirmed requires `confirmed_at`; awaiting confirmation requires it null; expiry remains optional.
- `ConversationTurn`: Conversation FK, sequence, role, normalized text, optional language, timestamp, unique `(conversation_id, sequence_number)`.
- `Booking`: Contact and Service FKs, constrained status, nullable UTC `start_at`/`end_at`, JSONB booking data, optional confirmation/cancellation timestamps; check pair-null/pair-present and `start_at < end_at`; no provider fields.
- `Handoff`: Conversation FK, constrained status/reason/priority, safe detail/context, timestamps; partial unique active (`pending`/`accepted`) index by conversation plus status/priority/requested index.
- `ToolExecution`: Conversation FK, constrained status, stable tool name, optional idempotency key, sanitized JSONB arguments/result, safe error fields, timestamps; partial unique non-null idempotency index plus conversation index.
- `AuditEvent`: controlled actor type, optional Contact/Conversation FKs, optional entity fields, sanitized metadata JSONB, occurred timestamp, no trigger-based update prevention.
- `ProviderEventReceipt`: provider and external event ID unique pair, optional type/hash, received/processed timestamps; no raw body.
- `OutboxEvent`: event type, payload JSONB, created/published timestamps, attempt count default `0`, last error code, partial index for `published_at IS NULL`; no publisher.
- Use restrictive foreign keys; do not use broad cascade deletes.
- Use one small private SQLAlchemy enum-column helper only if it reduces repetition, and ensure it always sets `native_enum=False` and `create_constraint=True`; do not create a generic model framework.

**Tests to write first:**

- [ ] Add metadata tests for the complete table set, UUID/date column types, no native enum types, and required indexes/constraints.
- [ ] Assert model metadata contains no provider identifiers on Booking and no raw webhook body/provider credential columns on future-seam records.
- [ ] Assert Contact, Conversation, Handoff, ToolExecution, ProviderEventReceipt, and OutboxEvent index definitions match the approved partial/unique/index requirements.

**Acceptance criteria:**
- `Base.metadata` contains only the Phase 1 records plus existing metadata root.
- Every approved database-enforced invariant is represented in SQLAlchemy metadata and is suitable for Alembic generation.
- No production database is edited manually and no provider integration code exists.

**Verification:**

- [ ] Run metadata tests through red-green.
- [ ] Run `uv run pytest -m unit tests/unit/test_models.py`.
- [ ] Run `uv run ruff check src/receptionist/db tests/unit/test_models.py` and `uv run mypy src/receptionist/db`.
- [ ] Commit as `feat: define phase 1 persistence models`.

---

### Task 5: Generate and review the first real Alembic schema revision

**Depends on:** Task 4.

**Files:**
- Modify: `alembic/env.py` to explicitly import/register `receptionist.db.models` before setting `target_metadata`.
- Create: `alembic/versions/<revision>_initial_domain_schema.py`
- Modify: `tests/integration/test_migrations.py`

**Purpose:** Turn the concrete metadata into the first real schema revision and prove clean downgrade back to the Phase 0 empty schema.

**Behavior and constraints:**
- Keep Alembic metadata registration explicit; do not rely on hidden package side effects.
- Generate a normal revision with message `initial domain schema`, then manually review and correct the generated operations.
- Create all Phase 1 tables, columns, foreign keys, checks, unique constraints, partial indexes, defaults, and timestamp types in dependency-safe order.
- Downgrade must remove all Phase 1 objects and return to the Phase 0 empty schema, including partial indexes and check constraints.
- Do not add migrations for provider tables beyond the approved persistence primitives and do not add a fake empty revision.

**Tests to write first:**

- [ ] Extend migration integration coverage for upgrade to head, downgrade to base, upgrade to head again, expected revision identity, and presence of the Phase 1 table set.

**Acceptance criteria:**
- The migration file is a normal reviewed Alembic revision with a clear message.
- `upgrade -> downgrade -> upgrade` succeeds against the guarded PostgreSQL test database.
- The migration contains no hand-written production-data manipulation and no Phase 2 provider behavior.

**Verification:**

- [ ] With the isolated test database running, run `RECEPTIONIST_APP_ENV=test RECEPTIONIST_DATABASE_URL="$TEST_DATABASE_URL" uv run alembic upgrade head`.
- [ ] Run `uv run pytest -m integration tests/integration/test_migrations.py` and inspect the generated schema if a failure occurs.
- [ ] Commit as `feat: add initial domain schema migration`.

---

### Task 6: Implement conflict-safe Contact resolution

**Depends on:** Tasks 3 and 5.

**Files:**
- Create: `src/receptionist/db/contact_identity.py`
- Create: `tests/integration/test_contact_identity.py`

**Purpose:** Provide the single focused database operation for canonical phone identity resolution.

**Interface:**
- `async def resolve_or_create_contact(session: AsyncSession, raw_number: str | None, default_region: str) -> Contact`.

**Behavior and constraints:**
- For a usable number, call `normalize_phone_number`, then execute a PostgreSQL `INSERT ... ON CONFLICT DO NOTHING` against the non-null phone uniqueness authority.
- If the insert wins, return the inserted Contact; if it conflicts, query and return the existing canonical Contact.
- Do not use a pre-check `SELECT -> if absent -> INSERT`; do not catch uniqueness and replace it with an application race.
- Do not commit, rollback, or own a broad transaction; caller transaction ownership remains explicit.
- For `None` or blank/withheld input, create and flush a new Contact with `phone_e164 = NULL` every time. Never reuse a NULL-phone Contact or match by name/email.
- Do not add a repository or generic identity abstraction.

**Tests to write first:**

- [ ] Equivalent international/national formats resolve to one Contact.
- [ ] Different phones resolve to different Contacts.
- [ ] Repeated same-phone resolution is idempotent.
- [ ] Two unknown/withheld resolutions create distinct Contacts.
- [ ] Two independent sessions resolving the same phone converge to one Contact using `asyncio.gather` and transaction completion, with no sleeps.
- [ ] Returned Contact queries remain within the resolved phone boundary; no name-based cross-contact behavior exists.

**Acceptance criteria:**
- The helper returns ORM `Contact` instances and never hides a commit.
- Database uniqueness, not Python locking or an application registry, is the concurrency authority.
- Invalid phone input fails through the domain normalization error before an invalid identity is persisted.

**Verification:**

- [ ] Run the focused integration tests against the isolated PostgreSQL service.
- [ ] Run `uv run mypy src/receptionist/db/contact_identity.py` and `uv run ruff check src/receptionist/db/contact_identity.py`.
- [ ] Commit as `feat: add conflict-safe contact identity resolution`.

---

### Task 7: Add control-mode and pending-action pure state helpers

**Depends on:** Task 2.

**Files:**
- Create: `src/receptionist/domain/state.py`
- Create: `tests/unit/test_state.py`

**Purpose:** Encode only the small approved conversation-control and current pending-action transitions without a generic workflow engine.

**Interfaces:**
- `class InvalidControlTransition(ValueError)` and `class PendingActionError(ValueError)`.
- `@dataclass(frozen=True, slots=True) class PendingActionState` with `type`, `status`, `payload`, `created_at`, `confirmed_at`, and optional `expires_at`.
- `transition_control_mode(current: ControlMode, target: ControlMode) -> ControlMode`.
- `stage_pending_action(current: PendingActionState | None, action_type: PendingActionType, payload: dict[str, object], created_at: datetime, expires_at: datetime | None = None) -> PendingActionState`.
- `confirm_pending_action(current: PendingActionState, confirmed_at: datetime) -> PendingActionState`.
- `clear_pending_action(current: PendingActionState | None) -> None`.

**Behavior and constraints:**
- Permit exactly AI → HANDOFF_PENDING, HANDOFF_PENDING → HUMAN, HANDOFF_PENDING → AI, and HUMAN → AI; reject all other transitions with one small domain error.
- Permit only `create_booking`, `reschedule_booking`, and `cancel_booking` pending action types.
- Staging fails when an action already exists; it creates `awaiting_confirmation` with confirmation timestamp null.
- Confirmation requires an awaiting action and sets `confirmed_at`; double confirmation fails.
- Clearing removes all current action metadata by returning `None`; no action history is created.
- Keep this pure and deterministic; no ORM session, database write, booking side effect, workflow registry, or provider call.

**Tests to write first:**

- [ ] Cover AI → handoff pending, handoff pending → human, handoff pending → AI cancellation, human → AI, and an invalid transition.
- [ ] Cover stage, confirm, double-stage rejection, double-confirm rejection, clear, invalid action type, and optional expiry.

**Acceptance criteria:**
- Every state helper is independently testable and maps directly to the Conversation columns.
- No state-machine dependency or generalized transition framework is added.

**Verification:**

- [ ] Run `uv run pytest -m unit tests/unit/test_state.py` through red-green.
- [ ] Run strict mypy and Ruff on `src/receptionist/domain/state.py`.
- [ ] Commit as `feat: add bounded conversation state helpers`.

---

### Task 8: Implement the synthetic reference seed

**Depends on:** Tasks 2–6.

**Files:**
- Create: `src/receptionist/seed.py`
- Create: `tests/unit/test_seed.py`
- Create: `tests/integration/test_seed.py`

**Purpose:** Provide one executable, validated, idempotent demo seed without creating a generic seed/fixture framework.

**Interfaces:**
- `build_reference_business() -> BusinessConfigSpec`.
- `build_reference_services() -> tuple[ServiceSpec, ...]`.
- `async def seed_reference_data(session: AsyncSession, app_env: str) -> None`.
- `async def run_seed(settings: Settings) -> None` and synchronous `main() -> None` for `python -m receptionist.seed`.

**Reference values:**
- Business name: `Riyadh HomeCare Demo`.
- Timezone: `Asia/Riyadh`; default region: `SA`; currency: `SAR`; supported languages: `en` and `ar`; default language: `en`.
- Use this deterministic synthetic weekly schedule: Sunday–Thursday `08:00-20:00`, Friday `14:00-20:00`, and Saturday `08:00-20:00`; use after-hours `closed_message`, explicit booking buffers/notice, and retention values.
- Use named synthetic Riyadh areas such as `al_olaya`, `al_malaz`, `al_nakheel`, and `al_yasmin`, with bilingual names/aliases and no geographic coordinates.
- Use a demo-only fictional handoff number such as `+1 202 555 0100`, clearly documented as test-safe synthetic data; never use a plausible Saudi subscriber number.
- Seed exactly four stable service codes: `ac_maintenance_repair`, `plumbing`, `electrical`, and `appliance_general_maintenance`. Use non-commercial `not_published` pricing and synthetic bilingual descriptions; do not imply real prices or operations.

**Behavior and constraints:**
- Validate all structures with the Task 2 Pydantic models before persistence.
- Refuse `production` before opening a database transaction; permit `local`, `test`, and `ci`.
- Insert the singleton if absent and select/reuse it if present; insert each service by stable code only when absent. Never create duplicates and do not add a generic upsert framework.
- Own the CLI transaction explicitly, commit only after all validation/persistence succeeds, and use opaque-ID logging only.
- Do not seed Contacts, Conversations, Bookings, Handoffs, tools, provider receipts, or outbox work.

**Tests to write first:**

- [ ] Assert the reference builders produce one valid BusinessConfigSpec and exactly four expected ServiceSpec codes.
- [ ] Assert `production` is refused and the builders contain clearly synthetic values.
- [ ] Integration-test one run creates one BusinessConfig and four Services; a second run creates no additional rows.

**Acceptance criteria:**
- `python -m receptionist.seed` is executable and uses existing settings/database resources.
- `make seed` can invoke it later without provider access.
- Repeated seed execution is idempotent for the singleton and stable service codes.

**Verification:**

- [ ] Run unit seed tests through red-green.
- [ ] Run the focused PostgreSQL seed integration test after Task 11 fixtures are available.
- [ ] Commit as `feat: add synthetic reference business seed`.

---

### Task 9: Add migration generation/check and seed Make targets

**Depends on:** Tasks 5 and 8.

**Files:**
- Modify: `Makefile`
- Modify: `docs/development/testing.md`

**Purpose:** Make the first real migration and drift guard reproducible through the approved project tooling.

**Behavior and constraints:**
- Add `make migration MSG="..."` using Alembic autogenerate and reject an empty `MSG` with a non-zero exit before invoking Alembic.
- Add `make migration-check` that sets `RECEPTIONIST_APP_ENV=test`, uses `TEST_DATABASE_URL`, calls the existing `assert_safe_test_database` guard, upgrades the guarded test DB to head if needed, and runs `alembic check`.
- Keep `make migrate` for the local development DB and preserve its existing explicit environment assignment.
- Add `make seed` as a thin invocation of `python -m receptionist.seed`; do not force production or add a seed framework.
- Keep `make alembic-verify` and update `make verify` to include migration drift verification without weakening existing gates.

**Tests/verification first:**

- [ ] Add shell-level verification notes/commands for empty `MSG` rejection, successful autogenerate invocation in a temporary branch state, guarded migration check, and refusal to target a non-test database for destructive checks.

**Acceptance criteria:**
- Empty migration messages are rejected.
- `make migration-check` detects SQLAlchemy metadata changes without a migration and succeeds when metadata matches the migrated test database.
- No Make target can destructively reset the development database.

**Verification:**

- [ ] Run `make migration MSG=""` and confirm non-zero failure with no revision created.
- [ ] Run `make migration-check` against the isolated test DB after it is at head.
- [ ] Run `make -n migration MSG="initial domain schema"` and inspect that it uses the approved uv/Alembic toolchain.
- [ ] Commit as `build: add guarded migration and seed commands`.

---

### Task 10: Complete the deterministic unit-test suite

**Depends on:** Tasks 2, 3, 4, 7, and 8.

**Files:**
- Modify/create: `tests/unit/test_domain_config.py`
- Modify/create: `tests/unit/test_identity.py`
- Modify/create: `tests/unit/test_models.py`
- Modify/create: `tests/unit/test_state.py`
- Modify/create: `tests/unit/test_seed.py`

**Purpose:** Ensure the unit suite covers every pure Phase 1 behavior before relying on PostgreSQL integration tests.

**Behavior to cover:**
- International and regional phone normalization, invalid/impossible numbers, and extension rejection.
- IANA timezone and phone-region validation, currency/default-language validation, strict unknown-field rejection.
- Valid/overlapping/duplicate/closed/cross-midnight business hours.
- Ambiguous service-area aliases and service-catalog aliases.
- All five pricing modes and invalid combinations; range order; positive service duration.
- All four allowed control transitions, invalid transition, pending action stage/confirm/clear/double-stage rejection.
- Metadata checks for constrained string enums, no native PostgreSQL enums, required partial indexes/checks, and absent provider-specific fields.
- Reference seed structure and production refusal without a database call.

**Acceptance criteria:**
- Unit tests assert real returned values/exceptions and metadata, not implementation mocks.
- `make test-unit` passes with no skipped required behavior and no weakened existing tests.

**Verification:**

- [ ] Run each focused unit file after its red-green cycle.
- [ ] Run `make test-unit` and record the complete result.
- [ ] Commit as `test: cover phase 1 domain invariants`.

---

### Task 11: Add guarded PostgreSQL integration coverage

**Depends on:** Tasks 5, 6, 8, 9, and 10.

**Files:**
- Modify: `tests/integration/conftest.py`
- Modify: `tests/integration/test_migrations.py`
- Modify: `tests/integration/test_database.py` only if fixture reuse is necessary
- Create: `tests/integration/test_contact_identity.py`
- Create: `tests/integration/test_domain_constraints.py`
- Create: `tests/integration/test_seed.py`

**Purpose:** Prove that PostgreSQL, rather than Python-only validation, enforces the approved persistence and concurrency invariants.

**Fixture constraints:**
- Use real PostgreSQL from the existing isolated test service.
- Ensure the database is at migration head before tests; any downgrade/reset/schema destruction calls `assert_safe_test_database` first.
- Use transaction rollback per normal test where practical.
- Use explicit independent sessions only for concurrency/constraint tests that require independent transactions; do not create a heavy database framework.

**Integration behavior:**
- Migration upgrade → downgrade → upgrade and `alembic check` with no drift.
- BusinessConfig singleton: id `1` accepted, id other than `1` rejected.
- Contact non-null E.164 uniqueness, multiple NULL-phone rows, equivalent normalized formats, different phones, repeated same-phone resolution, two unknown callers, and deterministic same-number independent-session resolution.
- Invalid Conversation Contact FK rejected.
- Duplicate sequence within one Conversation rejected; same sequence in different Conversations accepted.
- Invalid Booking time pairing/range rejected; valid null pair and valid ordered pair accepted.
- Second active Handoff for one Conversation rejected; resolved/cancelled historical rows do not block a new active handoff.
- Duplicate non-null ToolExecution idempotency key rejected; multiple NULL keys accepted.
- Duplicate `(provider, external_event_id)` rejected; same external ID across providers accepted.
- Direct invalid controlled-string values rejected by database check constraints.
- Reference seed creates one singleton and exactly four services; second run creates no duplicates.
- Assertions for queries/identity operations do not cross Contact boundaries.

**Acceptance criteria:**
- All required PostgreSQL integration behavior is deterministic and provider-free.
- Any expected `IntegrityError` is followed by an explicit rollback before session reuse.
- No test can destructively reset the development database.

**Verification:**

- [ ] Start the isolated service with `make test-db-up`.
- [ ] Run `make test-integration` and inspect all failures rather than weakening assertions.
- [ ] Stop the service with `make test-db-down` after the suite.
- [ ] Commit as `test: verify phase 1 postgres invariants`.

---

### Task 12: Run migration up/down/up and drift verification as a release gate

**Depends on:** Task 11.

**Files:**
- No production files; record verification evidence for the final implementation closeout.

**Purpose:** Prove the first real migration is reversible, repeatable, and aligned with SQLAlchemy metadata.

**Verification sequence:**

- [ ] With the guarded test database, run `alembic upgrade head`.
- [ ] Run `alembic downgrade base` and verify the Phase 1 tables/indexes/constraints are gone.
- [ ] Run `alembic upgrade head` again.
- [ ] Run `alembic check` / `make migration-check` and require no new upgrade operations.
- [ ] Run `make alembic-verify` and retain the exact exit/result.

**Acceptance criteria:**
- Up/down/up succeeds on PostgreSQL `18.6`.
- Drift check reports no differences.
- The downgrade returns to the Phase 0 empty schema without touching the development DB.

---

### Task 13: Update development and user-facing documentation

**Depends on:** Tasks 8–12.

**Files:**
- Modify: `docs/development/testing.md`
- Modify: `docs/development/setup.md`
- Modify: `README.md`
- Do not modify: approved `docs/architecture/domain-model.md` unless separately requested.

**Purpose:** Make Phase 1 operational rules and verification commands durable without claiming future functionality exists.

**Documentation content:**
- Explain E.164 identity, NULL-phone unknown-contact behavior, conflict-safe resolution, and no name/email matching.
- Explain transaction rollback/default isolation and the guarded independent-session cases.
- Document `make migration MSG="..."`, empty-message rejection, `make migration-check`, `make migrate`, `make seed`, and test database commands.
- Document seed refusal in production and synthetic/demo-only values.
- Document that Booking is provider-independent and that ToolExecution, ProviderEventReceipt, and OutboxEvent are persistence-only; explicitly state no publisher, dispatcher, provider, webhook, or workflow exists.
- Keep the Calendar/CRM/application authority hierarchy consistent and do not add Phase 2 claims.

**Acceptance criteria:**
- A new developer can run the Phase 1 tests/migration/seed path from the documentation.
- The README and testing guide do not imply booking, CRM, messaging, voice, LLM, or webhook behavior exists.

**Verification:**

- [ ] Run `git diff --check`.
- [ ] Review documentation references and commands against the final Makefile.
- [ ] Commit as `docs: document phase 1 verification and seed usage`.

---

### Task 14: Integrate migration drift into the existing CI workflow

**Depends on:** Tasks 9–13.

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `Makefile` only if the final `verify` target needs the same drift gate.

**Purpose:** Ensure normal CI detects metadata changes without a migration while preserving the single proportionate workflow.

**Behavior and constraints:**
- Add a migration-drift step invoking `make migration-check` after the integration database has been migrated to head, or use an equivalent guarded `uv run` command.
- Keep Python `3.12.15`, uv `0.12.22`, PostgreSQL `18.6`, locked installs, immutable existing action SHAs, least-privilege permissions, and all existing quality/security gates.
- Do not add live provider calls, external SaaS credentials, a second workflow, or a queue/worker job.

**Acceptance criteria:**
- CI runs `alembic check` against the isolated PostgreSQL service and fails on schema drift.
- Existing lint, format, mypy, secret scan, pip-audit, unit, integration, Compose, and Docker gates remain present.

**Verification:**

- [ ] Run `make lint`, `make format-check`, `make typecheck`, `make secrets`, `make audit`, and `make migration-check` locally where infrastructure is available.
- [ ] Inspect the workflow diff for correct ordering and no provider access.
- [ ] Commit as `ci: enforce phase 1 migration drift checks`.

---

### Task 15: Perform manual Phase 1 verification

**Depends on:** Tasks 11–14.

**Files:**
- No source files; manual verification evidence only.

**Purpose:** Exercise the user-visible local workflow and safety boundaries that unit/integration tests cannot fully demonstrate.

**Verification sequence:**

- [ ] Start the isolated PostgreSQL service with `make test-db-up`.
- [ ] Run `make migration-check` and `make test-integration` against the guarded test database.
- [ ] Run the seed path in `test` or `ci` and confirm one singleton/four services; run it twice and confirm no duplicates.
- [ ] Run `RECEPTIONIST_APP_ENV=production RECEPTIONIST_DATABASE_URL="$TEST_DATABASE_URL" uv run python -m receptionist.seed` and confirm it fails closed before persistence.
- [ ] Run `make alembic-verify` and the full quality/security gates.
- [ ] Inspect ordinary logs/output for opaque IDs only and confirm no raw phone/email/conversation/payload/secret output was introduced.
- [ ] Stop the test service with `make test-db-down`.

**Acceptance criteria:**
- Local commands are reproducible, production seed is refused, and the isolated database guard is effective.
- No external provider or SaaS call occurs.

---

### Task 16: Final quality, security, scope audit, and transition to `REVIEW`

**Depends on:** Tasks 1–15.

**Files:**
- Modify: `docs/roadmap.md` only, changing Phase 1 from `IN PROGRESS` to `REVIEW` after all evidence passes.
- Record: final closeout report in the response using the exact 12 required sections from `AGENTS.md`.

**Audit checklist:**

- [ ] Run `make check` and `make security` and record exact results.
- [ ] Run `make test`, `make test-integration`, `make alembic-verify`, `make migration-check`, `docker compose --profile test config`, and `docker build -t arabic-english-ai-receptionist:phase-1 .` where Docker is available.
- [ ] Run `git diff --check` and inspect `git diff --name-status e37d8dece8348a80efb9920889f900bc30fc4e19..HEAD`.
- [ ] Audit imports/files for Calendar, availability, booking workflows, HubSpot, Twilio/WhatsApp transport, Vapi/voice, LLMs/prompts/tool orchestration, webhooks, authentication/dashboard, n8n, retention jobs, external providers, publishers/workers, repositories, unit-of-work layers, and generic state-machine infrastructure; remove any accidental scope expansion.
- [ ] Audit schema for provider-independent Booking, persistence-only ToolExecution/ProviderEventReceipt/OutboxEvent, restrictive FKs, string-enum checks, partial indexes, PII-safe logs, and no raw webhook bodies/secrets.
- [ ] Confirm the starting SHA is recorded, ending SHA is recorded, branch is `phase/1-domain-identity-state`, and working tree is clean.
- [ ] Change only Phase 1 roadmap status to `REVIEW`; never mark `COMPLETE`, merge, create a PR, or start Phase 2.

**Acceptance criteria:**
- All required unit/integration/quality/security/manual checks have fresh evidence.
- No unresolved implementation conflict or unexplained deviation remains.
- The final report contains exactly: Status; Git; Files changed; Implementation summary; Architectural decisions; Tests executed; Quality/tooling results; Manual verification; Deviations; Known limitations; Security and cost; Scope audit.
- Phase 1 is a review candidate only.
