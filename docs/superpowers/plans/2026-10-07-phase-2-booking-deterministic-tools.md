# Phase 2 — Booking & Deterministic Business Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved Phase 2 deterministic booking operations with one real Google Calendar integration, explicit confirmation/idempotency/recovery safety, and PostgreSQL-backed local reconciliation, ending at `REVIEW` without beginning Phase 3.

**Architecture:** Keep the existing single-business FastAPI modular monolith. Add one pure `domain/booking_policy.py` module for deterministic lookup, normalization, and policy decisions; one `application/booking.py` module for strict request/result models and the finite booking operations; and one `integrations/google_calendar.py` module containing the narrow Calendar capability surface plus `GoogleCalendarClient`. Keep transaction ownership inside the explicit state-changing booking operations, use the existing singleton `BusinessConfig(id=1)` row as the single-calendar provider-write fence, and do not add repositories, UoW, workflow engines, provider registries, or background workers.

**Tech Stack:** Python `3.12.15`, uv `0.12.22`, Pydantic v2, Pydantic Settings, SQLAlchemy 2.1 async, Psycopg 3, PostgreSQL `18.6`, Alembic, `google-api-python-client`, `google-auth`, `google-auth-httplib2`, pytest/pytest-asyncio, Ruff, strict mypy, detect-secrets, pip-audit, Docker Compose, and the existing CI workflow.

**Spec:** Approved design at `docs/architecture/phase-2-booking-deterministic-tools.md` from commit `237d5c5165cc00c042494a5c231bfc84acdabff8`, plus `AGENTS.md`, `docs/roadmap.md`, `docs/architecture/system-overview.md`, `docs/architecture/integration-boundaries.md`, `docs/architecture/domain-model.md`, and the merged Phase 1 implementation.

## Global Constraints

- Work only on `phase/2-booking-deterministic-tools`; begin from the approved design SHA and keep Phase 2 `IN PROGRESS` until the final implementation candidate is ready for `REVIEW`.
- Preserve the single-business model: `BusinessConfig.id = 1`; no tenant, organization, membership, or multitenant infrastructure.
- Keep the operation surface finite: `lookup_service`, `lookup_service_area`, `check_availability`, `get_booking`, `prepare_create_booking`, `prepare_reschedule_booking`, `prepare_cancel_booking`, and `confirm_booking_action`.
- Use strict Pydantic request/result/error structures with `extra="forbid"`; do not create a generic result framework, command bus, tool registry, plugin system, or public booking API.
- Reuse Phase 1 catalog normalization, strict configuration, UTC-aware persistence, business-timezone conversion, controlled error vocabulary, pending-action helpers, and historical `RESTRICT` foreign keys.
- Add only the minimum direct Google runtime dependencies needed for the approved capability surface; do not add `google-auth-oauthlib`, an OAuth consent/web-flow UI, unrelated Google Cloud packages, or provider SDKs for other systems.
- OAuth client ID, client secret, refresh token, Calendar ID, and timeout settings remain runtime configuration. Secret values use `SecretStr` or equivalent safe handling and never appear in repr, ordinary logs, Booking JSON, audit metadata, or tests as real credentials.
- Normal CI, unit tests, and PostgreSQL integration tests never call live Google Calendar. Use one narrow deterministic Calendar double and mocked Google transport/service objects for boundary translation tests.
- The dedicated non-production Calendar smoke path is manual, guarded, and optional when credentials are unavailable; its absence is reported, never replaced with a fake claim.
- Add one Alembic revision after `58f4778324a1`; never edit the merged Phase 1 migration.
- Calendar-specific Booking references are explicit nullable columns, not fields hidden in `booking_data`. Preserve Phase 1 primitive rows and do not add another Booking status.
- A non-null `ToolExecution.idempotency_key` remains globally unique. Claim it with PostgreSQL conflict-safe insertion/selection; never catch an integrity error and continue in a poisoned transaction.
- A provider mutation uses the two-stage claim/finalization protocol. `started` is durable and recoverable; no database transaction is treated as atomic with Google Calendar.
- Use the single-calendar write fence and compatible lock order `BusinessConfig → Conversation → Contact → Booking` wherever overlapping locks are acquired. Do not use Redis, advisory locks, application mutexes, or distributed locks.
- Reschedule conflict reads must identify and exclude only the exact target event ID; never subtract the target interval from a free/busy result.
- Reschedule uses minimal application-owned Calendar patch fields with current ETag/`If-Match` protection; never overwrite unrelated operator/provider-owned event metadata.
- `Booking.status` remains `pending`, `confirmed`, `cancelled`: only a reconciled Calendar event permits `confirmed`; a definitively rejected never-created create clears the reserved Calendar reference pair.
- Ordinary logs use opaque IDs. ToolExecution, AuditEvent, and Booking metadata contain only bounded sanitized data, no raw Google payload, ETag, credential, phone, email, address, transcript, or secret.
- Destructive integration setup/reset/migration operations remain behind `assert_safe_test_database`, requiring `test`/`ci` and database name `receptionist_test`.
- No HubSpot, CRM sync, WhatsApp/Twilio, Vapi, audio, LLM, prompts, public conversational API, dashboard/UI, n8n, reminders, outbox publisher, worker, technician/resource scheduling, recurrence for application-created bookings, or Phase 3+ behavior.

## Review Focus

- Provider-free local policy validity must never be reported as real availability — Task 6 and Task 13 test that only the Calendar double's free/busy/conflict response makes the provider decision.
- A reschedule overlapping its own old event must remain allowed while another event in the same interval blocks it — Task 6 tests exact target-ID exclusion, buffer-only self-overlap, competing events, and recurring instances.
- A delayed confirmation token must not clear or execute a newer same-type pending action — Task 7 and Task 9 test token mismatch while preserving the current action.
- A stale reschedule/cancel prepared in another Conversation must not overwrite newer Booking state — Task 7 and Task 14 test the canonical expected-state fingerprint under real PostgreSQL locks.
- A Google mutation that succeeds before local finalization must be recoverable without a duplicate event — Task 10 and Task 14 test deterministic event IDs, ambiguous responses, existing events, private-marker mismatch, and same-key recovery.

## Expected module and file structure

Keep the implementation proportional to the approved design. Do not create
additional layers merely for symmetry.

- `src/receptionist/core/config.py`: add optional runtime Google Calendar/OAuth settings, bounded timeout validation, safe summary fields, and a clear “credentials configured” predicate. No consent/bootstrap flow.
- `src/receptionist/domain/booking_policy.py`: pure catalog matching, service-detail validation, duration derivation, aware-UTC normalization, business-hours/notice/advance/slot/buffer policy evaluation, and canonical expected-state/fingerprint helpers that do not open sessions or call providers.
- `src/receptionist/db/models.py`: add only `Booking.calendar_id`, `Booking.calendar_event_id`, their pair/confirmed-reference constraints, and the unique composite index.
- `alembic/versions/<revision>_add_booking_calendar_references.py`: the one reversible Phase 2 Booking-reference migration generated through the supported Make target and reviewed manually.
- `src/receptionist/integrations/google_calendar.py`: application-owned Calendar interval/event/conflict models, one narrow `CalendarClient` protocol/capability surface, bounded safe Calendar errors, and one `GoogleCalendarClient`. No provider registry or adapter hierarchy.
- `src/receptionist/application/__init__.py`: package marker only.
- `src/receptionist/application/booking.py`: strict Phase 2 request/result/error models, finite operation functions, focused ToolExecution claim helper, pending-action token handling, Booking claim/finalization orchestration, lock ordering, and sanitized persistence. No generic repository/UoW/idempotency/workflow framework.
- `tests/support/__init__.py` and `tests/support/calendar_double.py`: one deterministic test double implementing only the narrow Calendar capability surface needed by tests, including controlled ambiguous-response behavior.
- `scripts/calendar_smoke.py`: guarded manual smoke flow for a dedicated non-production Calendar only; no production credentials or IDs committed.
- `tests/unit/`: settings, request/result/error, policy, Calendar boundary translation, security, and model metadata tests.
- `tests/integration/`: migration, PostgreSQL constraint, operation, concurrency, recovery, repeatability, and guarded persistence tests.
- `README.md`, `docs/development/setup.md`, `docs/development/testing.md`, `docs/architecture/integration-boundaries.md`, and `Makefile`: updated only after behavior exists, documenting the real Phase 2 Google boundary and manual smoke path without adding Phase 3 behavior.

## Ordered implementation tasks

### Task 1: Phase 2 dependency and runtime configuration boundary

**Depends on:** Approved Phase 2 design and merged Phase 1 baseline.

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/receptionist/core/config.py`
- Modify: `.env.example`
- Test: `tests/unit/test_config.py`

**Purpose:** Add only the runtime configuration and locked dependencies needed
to call one configured Google Calendar with an already-provisioned OAuth
authorized-user refresh token.

**Interfaces:**

- Add optional settings fields so normal local/CI startup remains possible without live credentials:
  - `google_calendar_id: str | None` with non-blank validation when supplied;
  - `google_oauth_client_id: SecretStr | None`;
  - `google_oauth_client_secret: SecretStr | None`;
  - `google_oauth_refresh_token: SecretStr | None`;
  - `google_calendar_request_timeout_seconds: float` bounded to a positive value no greater than 30 seconds, defaulting to 10 seconds.
- Add `Settings.google_calendar_configured -> bool`, true only when all three OAuth secrets and the Calendar ID are present.
- Extend `safe_summary()` with only `google_calendar_configured` and the bounded timeout; never include setting values.

**Behavior and constraints:**

- Add only these direct dependencies, with these bounded ranges, and let uv resolve their transitive lockfile set: `google-api-python-client>=2.181,<3`, `google-auth>=2.40,<3`, and `google-auth-httplib2>=0.2,<1`. Do not add `google-auth-oauthlib`; consent/bootstrap is an operator task outside the application.
- Use a runtime refresh token and client credentials; do not implement an OAuth web flow, credential file loader, local token writer, or consent route.
- Freeze the authorized-user OAuth scope set to exactly `https://www.googleapis.com/auth/calendar.events` and `https://www.googleapis.com/auth/calendar.freebusy`. The out-of-band operator bootstrap grants both scopes; do not request the broader `https://www.googleapis.com/auth/calendar` scope, add an arbitrary-scope setting, or implement consent/escalation in the application.
- Keep all Calendar settings optional for ordinary unit/integration/CI execution. The real client must fail with a safe configuration error when the required values are absent.
- Keep Settings tests isolated with `_env_file=None`, explicit constructor values, and `monkeypatch` cleanup exactly as Phase 0 established. `.env.example` contains empty/placeholders only and no credential-like values.
- Preserve `SecretStr(repr=False)` behavior and safe logging conventions.

**Tests to write first:**

- [ ] `test_google_calendar_settings_are_optional_for_provider_free_runs` proves ordinary test settings can load without Calendar credentials.
- [ ] `test_google_calendar_settings_require_all_credentials_for_configured_predicate` covers partial credentials and the configured predicate.
- [ ] `test_google_calendar_secrets_are_absent_from_repr_and_logs` asserts client secret and refresh token values do not appear in `repr(settings)` or `log_configuration()` output.
- [ ] `test_settings_tests_ignore_dotenv_and_ambient_google_configuration` creates a temporary `.env`, sets ambient Calendar variables, passes `_env_file=None`, and verifies explicit settings isolation.
- [ ] `test_calendar_timeout_is_positive_and_bounded` rejects zero/negative/over-limit values.
- [ ] Configuration/bootstrap documentation and the Google client tests use exactly the two approved scopes, with no broader or caller-supplied scope set.

**Acceptance criteria:**

- Only the three approved direct Google dependencies are added and the lockfile is regenerated by uv.
- No OAuth UI, consent/bootstrap code, token persistence, source secret, or provider implementation is added.
- Normal CI can run without Google credentials and `safe_summary()` is PII/secret-safe.

**Verification commands:**

- `uv lock --check`
- `uv sync --frozen --all-groups`
- `uv run pytest -m unit tests/unit/test_config.py`
- `uv run ruff check src/receptionist/core/config.py tests/unit/test_config.py`
- `uv run mypy src/receptionist/core/config.py`

**Focused commit:** `build: add Phase 2 Calendar dependencies and settings`

### Task 2: Focused Booking Calendar-reference migration

**Depends on:** Task 1.

**Files:**

- Modify: `src/receptionist/db/models.py`
- Modify: `tests/unit/test_models.py`
- Modify: `tests/integration/test_domain_constraints.py`
- Modify: `tests/integration/test_migrations.py`
- Create: `alembic/versions/<new_revision>_add_booking_calendar_references.py`

**Purpose:** Add only the explicit Calendar references and invariants required
by Phase 2 while preserving all Phase 1 provider-independent Booking rows.

**Interfaces and schema behavior:**

- Add nullable `Booking.calendar_id: Mapped[str | None]` and nullable `Booking.calendar_event_id: Mapped[str | None]`, using bounded `String` columns sized for Google identifiers.
- Add a check constraint equivalent to “both Calendar reference columns are null or both are non-null.”
- Add a unique composite index/constraint on `(calendar_id, calendar_event_id)`. PostgreSQL may still allow multiple all-null pairs, preserving Phase 1 primitive rows.
- Add a check constraint equivalent to “`status = 'confirmed'` requires both Calendar reference columns to be non-null.” Do not require references for Phase 1-compatible pending/cancelled rows.
- Do not add ETag, raw event payload, provider credential, provider JSON, new status, or Calendar-specific fields to `booking_data`.
- Keep all existing Booking time-pair/order constraints, restrictive foreign keys, and indexes unchanged.

**Tests to write first:**

- [ ] Extend `test_booking_and_future_seams_have_no_provider_payload_or_identifier_columns` to assert the two approved explicit Calendar columns and no ETag/raw payload/credential columns.
- [ ] Add metadata assertions for pair-nullability, confirmed-reference, and composite uniqueness definitions.
- [ ] Add PostgreSQL tests that accept two reference-free Phase 1 primitive rows, reject only-one-reference rows, reject confirmed rows without the pair, accept pending rows with a reserved pair, reject duplicate non-null pairs, and accept historical cancelled rows retaining a pair.
- [ ] Extend migration-cycle assertions to verify the new columns/indexes/check constraints exist after re-upgrade and are absent after downgrade.

**Implementation and migration constraints:**

- Modify metadata first, then run the supported generator: `make migration MSG="add Calendar references to Booking"`.
- Verify the generated revision has `down_revision = "58f4778324a1"`; never edit `58f4778324a1_initial_domain_schema.py`.
- Review the generated upgrade/downgrade manually and correct only the expected Booking alterations.
- Test the guarded migration up/down/up cycle; do not use a disposable revision.

**Acceptance criteria:**

- Existing rows with both Calendar columns null remain valid.
- A confirmed Booking cannot be persisted without both explicit references.
- A pending create can persist a reserved pair before external event existence is proven.
- The migration is reversible and `alembic check` sees no drift.

**Verification commands:**

- `make migration MSG="add Calendar references to Booking"`
- `RECEPTIONIST_APP_ENV=test RECEPTIONIST_DATABASE_URL="$TEST_DATABASE_URL" uv run pytest -m integration tests/integration/test_migrations.py tests/integration/test_domain_constraints.py`
- `make migration-check`
- `make alembic-verify`

**Focused commit:** `feat: add Booking Calendar reference migration`

### Task 3: Strict application request/result/error models

**Depends on:** Task 2.

**Files:**

- Create: `src/receptionist/application/__init__.py`
- Create: `src/receptionist/application/booking.py`
- Test: `tests/unit/test_booking_models.py`

**Purpose:** Define the finite Phase 2 operation contracts before implementing
database/provider orchestration.

**Interfaces:**

- Define strict request models for service lookup, area lookup, availability, booking retrieval, create preparation, reschedule preparation, cancel preparation, and exact confirmation.
- Define strict output models for service/area lookup, availability, Booking read/reconciliation, preparation, confirmation, and `BookingError`.
- Define one stable `BookingErrorCode` vocabulary containing exactly the approved codes: `invalid_input`, `unknown_service`, `service_inactive`, `service_not_bookable`, `unsupported_service_area`, `configuration_conflict`, `invalid_requested_time`, `outside_business_policy`, `confirmation_required`, `pending_action_conflict`, `stale_pending_action`, `booking_not_found`, `booking_state_conflict`, `external_reference_missing`, `operation_in_progress`, `idempotency_conflict`, `duplicate_replay`, `calendar_interval_unavailable`, `calendar_unavailable`, `external_event_missing`, `external_state_conflict`, and `calendar_reconciliation_required`.
- Define strict bounded data models for sanitized ToolExecution/AuditEvent metadata and operation-specific output data; do not introduce a generic command/result framework.
- Confirmation input must contain `conversation_id`, expected `PendingActionType`, exact opaque `action_token`, and its own non-empty idempotency key.
- Availability input must contain a service selector and aware requested start plus optional end; reschedule requests contain Booking ID and new interval; create requests contain service/area selectors and bounded requirement values.

**Behavior and constraints:**

- Reject unknown fields, naive datetimes, blank selectors/keys, unbounded details, arbitrary nested requirement payloads, raw transcript, raw provider payload, and raw credentials.
- Keep operation errors safe, deterministic, and suitable for later text/voice channels; no Google HTTP code or raw exception crosses the model boundary.
- Keep provider event snapshots and conflict records application-owned and bounded; do not expose raw Google Event dictionaries.

**Tests to write first:**

- [ ] Assert every request rejects extra fields and naive datetimes.
- [ ] Assert exact required fields for all eight operations, including confirmation action token and separate confirmation idempotency key.
- [ ] Assert bounded requirement values/details and safe result serialization.
- [ ] Assert every stable error code serializes as a constrained string with safe message, retryable flag, and bounded details.

**Acceptance criteria:**

- Later tasks can import exact request/result/error names from `receptionist.application.booking` without inventing shape or field names.
- No generic tool registry, result framework, command bus, provider registry, or public API model is added.

**Verification commands:**

- Run focused tests red first: `uv run pytest -m unit tests/unit/test_booking_models.py`
- Then run green: `uv run pytest -m unit tests/unit/test_booking_models.py`
- `uv run ruff check src/receptionist/application/booking.py tests/unit/test_booking_models.py`
- `uv run mypy src/receptionist/application/booking.py`

**Focused commit:** `feat: define Phase 2 booking operation contracts`

### Task 4: Pure deterministic catalog, time, and booking-policy functions

**Depends on:** Tasks 2–3.

**Files:**

- Create: `src/receptionist/domain/booking_policy.py`
- Modify: `src/receptionist/application/booking.py` to import the pure policy interfaces
- Test: `tests/unit/test_booking_policy.py`

**Purpose:** Implement policy and validation decisions independently of
sessions, Calendar calls, current wall-clock time, or network state.

**Interfaces:**

- `normalize_requested_interval(start_at: datetime, end_at: datetime | None, duration_minutes: int) -> RequestedInterval` requires aware values, normalizes both to UTC, derives end from service duration when absent, and rejects non-positive/order-invalid intervals.
- `validate_required_booking_details(service: ServiceSpec | ServiceCatalogView, details: Mapping[str, str]) -> None` accepts only configured keys and requires every configured required key with bounded values.
- `match_catalog_selector(selector: str, labels_by_code: Mapping[str, Sequence[str]]) -> str | None` uses Phase 1 `normalize_catalog_text`, returns one exact code, and reports ambiguity to the caller rather than ranking matches.
- `evaluate_booking_policy(config: BusinessConfigSpec | BusinessConfigView, interval: RequestedInterval, now_utc: datetime) -> PolicyDecision` evaluates timezone, same-local-day rule, opening windows, buffers, minimum notice, maximum advance, and slot increment.
- `canonical_booking_snapshot(...) -> Mapping[str, object]` and `booking_state_fingerprint(snapshot: Mapping[str, object]) -> str` produce stable canonical JSON/SHA-256 values for the fields approved by the design.

**Behavior and constraints:**

- Use `zoneinfo` and the configured IANA timezone; never assume `Asia/Riyadh`.
- Persist/evaluate appointment instants in UTC, evaluate business windows in local wall time, use opening-inclusive/closing-exclusive windows, and use half-open interval overlap semantics.
- Apply before/after buffers only to policy boundary and Calendar conflict interval, not to local capacity inference.
- Require slot alignment from local midnight; use explicit `now_utc` for deterministic notice/advance tests.
- Reject ambiguous/nonexistent local-time conversions rather than guessing; accept operation inputs only as aware datetimes.
- Snapshot exactly Booking ID, service ID, status, UTC start/end, Calendar ID/event ID, and a digest of relevant request data. Do not create generic version fields.

**Tests to write first:**

- [ ] Test exact service/area normalization, unknown and ambiguous matches, inactive/non-bookable decisions, and unknown requirement keys.
- [ ] Test duration derivation, explicit end, UTC conversion, naive input rejection, reversed/zero intervals, and aware offset inputs.
- [ ] Test Sunday/Friday/reference business hours, opening/closing edges, buffer boundaries, minimum notice, maximum advance, and slot increments.
- [ ] Test DST nonexistent and ambiguous local times using a DST-observing IANA zone, plus `Asia/Riyadh` behavior.
- [ ] Test snapshot/fingerprint equality for equal state and inequality when each relied-upon Booking field changes.

**Acceptance criteria:**

- Pure functions have no SQLAlchemy session, Google client, ambient clock, or logging side effect.
- All business-policy decisions are deterministic from explicit inputs and can be reused by prepare and finalization paths.

**Verification commands:**

- Red/green focused cycle: `uv run pytest -m unit tests/unit/test_booking_policy.py`
- `uv run ruff check src/receptionist/domain/booking_policy.py tests/unit/test_booking_policy.py`
- `uv run mypy src/receptionist/domain/booking_policy.py`

**Focused commit:** `feat: add deterministic booking policy functions`

### Task 5: Narrow Google Calendar boundary and deterministic double

**Depends on:** Tasks 1, 3, and 4.

**Files:**

- Create: `src/receptionist/integrations/__init__.py`
- Create: `src/receptionist/integrations/google_calendar.py`
- Create: `tests/support/__init__.py`
- Create: `tests/support/calendar_double.py`
- Test: `tests/unit/test_google_calendar.py`
- Test: `tests/unit/test_calendar_double.py`

**Purpose:** Add exactly one real Google Calendar implementation and the
smallest application-owned test double needed to test operations without live
network calls.

**Interfaces:**

- Application-owned immutable models: `CalendarInterval`, `CalendarBusyInterval`, `CalendarConflict`, `CalendarEventSnapshot`, `CalendarEventCreate`, and `CalendarEventPatch`.
- One narrow async-capability protocol/surface named `CalendarClient` with:
  - `query_free_busy(calendar_id: str, time_min: datetime, time_max: datetime) -> tuple[CalendarBusyInterval, ...]`;
  - `query_conflicts(calendar_id: str, time_min: datetime, time_max: datetime, exclude_event_id: str | None = None) -> tuple[CalendarConflict, ...]`;
  - `get_event(calendar_id: str, event_id: str) -> CalendarEventSnapshot | None`;
  - `create_event(calendar_id: str, event_id: str, event: CalendarEventCreate) -> CalendarEventSnapshot`;
  - `patch_event(calendar_id: str, event_id: str, owned_fields: CalendarEventPatch, if_match_etag: str) -> CalendarEventSnapshot`;
  - `cancel_event(calendar_id: str, event_id: str, if_match_etag: str) -> None`.
- `GoogleCalendarClient.from_settings(settings: Settings) -> GoogleCalendarClient` validates configured runtime settings and constructs authorized HTTP with the explicit timeout.
- The Google client constructs authorized-user credentials with exactly `https://www.googleapis.com/auth/calendar.events` and `https://www.googleapis.com/auth/calendar.freebusy`; scopes are a private constant, not a configuration option.
- Provider-safe exceptions/models must expose only bounded application-owned error categories; raw response bodies, tokens, headers, and Google Event objects stay inside this module.
- Missing or insufficient authorization, including a missing approved scope, maps to the bounded `calendar_unavailable`/approved authentication failure result without exposing provider details.

**Behavior and constraints:**

- Use `google-api-python-client`/`google-auth`/`google-auth-httplib2`, refresh-token credentials, `build("calendar", "v3", ...)`, and no consent flow.
- Wrap the synchronous Google client calls in the existing async application boundary without blocking the event loop; use a bounded HTTP/request timeout and no unbounded retry loop.
- `query_free_busy` calls `freebusy.query` for the configured Calendar and translates RFC3339/UTC free/busy intervals. It inspects the requested Calendar entry for embedded provider errors; any error, including an unknown future reason, maps to bounded `calendar_unavailable` behavior and never to free/available.
- `query_conflicts` calls `events.list` with the configured Calendar ID, effective `timeMin`, effective `timeMax`, `singleEvents=True`, and `showDeleted=False`; follows `nextPageToken` until absent while repeating the same query parameters on every page; and collects all pages before deciding availability. It uses provider recurrence expansion, excludes only the exact event ID, and filters cancelled/non-blocking transparent events.
- `query_conflicts` translates timed `dateTime` events to aware UTC intervals and opaque all-day `date` events to the half-open `[start.date, end.date)` interval in the valid Calendar/list response timezone. An opaque all-day event blocks overlap, a transparent all-day event does not, and invalid/missing timezone translation fails safely as `calendar_unavailable`; Phase 2-created appointments remain timed events.
- `patch_event` sends only the application-owned fields and uses current ETag/`If-Match`; it does not send a full event object.
- `create_event` accepts a caller-supplied event ID and private opaque Booking marker; it does not generate a random provider identity.
- The double must model only busy/free intervals, recurring conflict instances, event retrieval, create/patch/delete, ETag conflicts, missing events, ambiguous post-write responses, private-marker mismatch, and the bounded pagination needed by conflict tests; it must not recreate Google pagination infrastructure generally.

**Tests to write first:**

- [ ] Test fake free/busy, conflict, get, create, patch, and cancellation behavior with no network.
- [ ] Test Google credential construction uses exactly the two approved OAuth scopes and no broader scope.
- [ ] Test Google request translation using mocked service/request/transport objects: Calendar ID, time bounds, `singleEvents=True`, `showDeleted=False`, recurrence expansion, exact event ID, private marker, patch fields, conditional ETag, and bounded timeout.
- [ ] Test one-page conflict results, a blocking event present only on page 2, an excluded target on one page with a different blocker on another page, and termination when `nextPageToken` is absent; repeat the same query parameters on every page.
- [ ] Test opaque one-day and multi-day all-day blocking events, transparent all-day events, half-open touching boundaries, and recurring expansion alongside all-day translation.
- [ ] Test ordinary busy/free responses plus Calendar-level `notFound`, internal/provider, and unknown future free/busy errors; none may become `available=true` and raw reasons/bodies must not escape.
- [ ] Test safe mapping of timeout/auth/quota/missing/412/ambiguous responses without raw payload leakage.
- [ ] Test the double’s ambiguous create mode persists the event before raising a recoverable response.

**Acceptance criteria:**

- There is one `GoogleCalendarClient` and one narrow double; no provider registry, adapter hierarchy, fake Google API recreation, or OAuth web flow.
- Raw Google payloads never appear in application result models or durable metadata.

**Verification commands:**

- Focused red/green tests: `uv run pytest -m unit tests/unit/test_google_calendar.py tests/unit/test_calendar_double.py`
- `uv run ruff check src/receptionist/integrations tests/support`
- `uv run mypy src/receptionist/integrations`

**Focused commit:** `feat: add narrow Google Calendar boundary`

### Task 6: Conflict-aware availability composition

**Depends on:** Tasks 4–5.

**Files:**

- Modify: `src/receptionist/domain/booking_policy.py`
- Modify: `src/receptionist/application/booking.py`
- Modify: `tests/support/calendar_double.py`
- Test: `tests/unit/test_booking_policy.py`
- Test: `tests/unit/test_calendar_double.py`

**Purpose:** Compose pure business policy with real provider-read semantics,
especially the self-conflict-safe reschedule query.

**Interfaces and behavior:**

- Add a pure half-open `intervals_overlap(left: RequestedInterval, right: CalendarInterval) -> bool` helper.
- Add a pure decision helper that accepts policy validity, a tuple of provider busy/conflict intervals, and buffers, returning availability or the appropriate stable error.
- Keep ordinary/create availability on `query_free_busy` for the effective buffered interval.
- For reschedule, call `query_conflicts` over the effective buffered interval with the exact target event ID; never subtract the target’s old interval from free/busy.
- Expand recurring blocking events through the Calendar boundary, ignore cancelled/transparent events, detect all other overlaps including another event occupying the target’s old interval, and preserve half-open edges.
- Treat any embedded Calendar-level free/busy error as `calendar_unavailable` (or the existing bounded provider-equivalent error), even when the response has an empty or absent `busy` list; unknown provider reasons fail closed rather than producing `available=true`.

**Tests to write first:**

- [ ] New reschedule interval overlapping only the target’s old event is allowed.
- [ ] Adjacent reschedule whose before/after buffer touches only the target’s old event is allowed.
- [ ] The same intervals with another blocking event are rejected.
- [ ] A recurring blocking event is expanded and rejected when an occurrence overlaps.
- [ ] Excluding a different event ID does not hide the target; excluding only the exact target ID works.
- [ ] Events ending exactly at the effective start or starting exactly at the effective end do not conflict; positive overlap does.
- [ ] Local Booking rows alone never make an interval unavailable.
- [ ] Ordinary free, ordinary busy, Calendar `notFound`, Calendar internal/provider, and unknown future free/busy errors produce the correct bounded results; provider errors never report availability.

**Acceptance criteria:**

- `check_availability` can distinguish provider-free, provider-busy, policy-invalid, and Calendar-unavailable outcomes.
- Reschedule self-overlap is fixed without weakening conflict detection.

**Verification commands:**

- `uv run pytest -m unit tests/unit/test_booking_policy.py tests/unit/test_calendar_double.py`
- `uv run ruff check src/receptionist/domain/booking_policy.py src/receptionist/application/booking.py`
- `uv run mypy src/receptionist/domain/booking_policy.py src/receptionist/application/booking.py`

**Focused commit:** `feat: add Calendar-backed availability decisions`

### Task 7: Action-token and expected-state safety

**Depends on:** Tasks 3–4.

**Files:**

- Modify: `src/receptionist/application/booking.py`
- Modify: `src/receptionist/domain/booking_policy.py` for the pure expected-state fingerprint helpers
- Test: `tests/unit/test_booking_models.py`
- Test: `tests/unit/test_booking_policy.py`

**Purpose:** Bind exact customer confirmation to one current pending action and
one Booking state snapshot without adding a pending-action table or generic
versioning framework.

**Interfaces and behavior:**

- Generate a fresh opaque UUID-based `action_token` at every prepare operation; store it in the canonical pending payload and return it in the preparation result.
- `confirm_booking_action` must require and compare Conversation ID, expected pending-action type, exact action token, and its own idempotency key.
- If type/token mismatches a different valid current action, return `stale_pending_action`, persist the rejected confirmation execution/audit safely, and preserve the current action unchanged.
- If the current action is expired or its own configuration/Booking precondition is invalid, clear that action only with an audited rejection; never clear a different valid action.
- Build reschedule/cancel expected snapshots from Booking ID, service ID, status, UTC interval, Calendar ID/event ID, and relevant request-data digest; compare the SHA-256 fingerprint after locking at confirmation.

**Tests to write first:**

- [ ] A token is non-PII, unique per preparation, persisted in the payload, and returned in the result.
- [ ] Exact type/token confirmation succeeds; missing token, wrong type, wrong token, and wrong Conversation fail safely.
- [ ] Delayed confirmation for an earlier same-type action does not clear or execute a newer valid action.
- [ ] A current action that is expired/invalid may be cleared with a rejected audit, while a mismatched caller token leaves the current valid action intact.
- [ ] Every relied-upon Booking snapshot field changes the fingerprint.

**Acceptance criteria:**

- No confirmation can execute by type alone.
- Two Conversations preparing changes to one Booking cannot use stale confirmation to overwrite the newer state.

**Verification commands:**

- Focused red/green tests: `uv run pytest -m unit tests/unit/test_booking_models.py tests/unit/test_booking_policy.py`
- After integration scaffolding exists: `uv run pytest -m integration tests/integration/test_booking_operations.py -k 'token or stale'`

**Focused commit:** `feat: enforce exact booking action identity`

### Task 8: Conflict-safe ToolExecution idempotency primitives

**Depends on:** Tasks 2–3 and the action contracts from Task 7.

**Files:**

- Modify: `src/receptionist/application/booking.py`
- Create: `tests/integration/test_booking_operations.py`
- Test: `tests/integration/test_domain_constraints.py` only for direct schema compatibility assertions

**Purpose:** Add a Phase 2-focused claim/replay helper without creating a
general idempotency service.

**Interfaces:**

- Private application helper: `_claim_tool_execution(session: AsyncSession, *, conversation_id: UUID, tool_name: str, idempotency_key: str, sanitized_arguments: dict[str, object]) -> ToolExecutionClaim`.
- The helper uses PostgreSQL `INSERT ... ON CONFLICT DO NOTHING RETURNING`; if no row is returned, it selects the existing row `FOR UPDATE` and validates tool/conversation/argument fingerprint.
- `ToolExecutionClaim` distinguishes `new`, terminal replay, durable `started` recovery, and mismatched-key conflict for the explicit booking operations only.

**Behavior and constraints:**

- Never catch `IntegrityError` from a failed unique insert and continue in the same poisoned transaction.
- New preparation executions become terminal `succeeded`/`rejected` within their transaction.
- Confirmation claims become durable `started` only after exact pending-action and Booking checks are committed; provider finalization later sets terminal status.
- `succeeded`, `rejected`, and terminal `failed` rows replay stored sanitized results; mismatched tool/conversation/arguments return `idempotency_conflict`.
- A durable `started` row is resumable by the same key and cannot be bypassed by another key; a different key returns `operation_in_progress` when it targets the active action/Booking.

**Tests to write first:**

- [ ] New key creates one claim.
- [ ] Repeating a succeeded, rejected, or terminal failed key returns the stored result without a second mutation.
- [ ] Reusing a key with a different tool, Conversation, or sanitized arguments returns `idempotency_conflict`.
- [ ] A durable started row can be selected for recovery by the same key.
- [ ] Two independent PostgreSQL sessions concurrently claiming one key produce one row and one winner/replay outcome without poisoned-session continuation.

**Acceptance criteria:**

- The helper is private to `application/booking.py`; no reusable cross-domain idempotency framework is introduced.
- PostgreSQL remains the uniqueness/concurrency authority.

**Verification commands:**

- Run focused PostgreSQL tests against the guarded database: `uv run pytest -m integration tests/integration/test_booking_operations.py -k 'idempotency or claim or replay'`
- `git diff --check`

**Focused commit:** `feat: add conflict-safe booking execution claims`

### Task 9: Prepare create/reschedule/cancel operations

**Depends on:** Tasks 3–8.

**Files:**

- Modify: `src/receptionist/application/booking.py`
- Test: `tests/integration/test_booking_operations.py`
- Test: `tests/integration/test_domain_constraints.py` for sanitized persistence assertions

**Purpose:** Implement the three deterministic preparation operations that
validate policy/provider reads and stage exactly one current pending action,
without any Calendar mutation.

**Interfaces:**

- `async def prepare_create_booking(session_factory: async_sessionmaker[AsyncSession], calendar: CalendarClient, request: CreateBookingRequest, *, now_utc: datetime) -> PreparationResult`
- `async def prepare_reschedule_booking(session_factory: async_sessionmaker[AsyncSession], calendar: CalendarClient, request: RescheduleBookingRequest, *, now_utc: datetime) -> PreparationResult`
- `async def prepare_cancel_booking(session_factory: async_sessionmaker[AsyncSession], request: CancelBookingRequest) -> PreparationResult`
- Each operation owns its narrow transaction, accepts no raw text/transcript, and returns the exact action token on success.

**Behavior and constraints:**

- Resolve services through exact relational catalog lookup; resolve areas through the singleton `BusinessConfig.service_areas`; distinguish unknown/inactive/non-bookable/ambiguous outcomes.
- Validate required service details, derive/normalize UTC interval, evaluate current policy, and call Calendar availability for create/reschedule only.
- Create uses free/busy; reschedule uses conflict-aware query excluding its exact target event ID.
- Lock the Conversation before checking/staging the empty pending-action slot. A different current action returns `pending_action_conflict`; the same prepare key replays.
- Reschedule/cancel require Phase 2-managed references and capture the expected-state fingerprint while holding the target Booking lock; Phase 1 primitive rows return safe reference/state errors.
- Persist only sanitized canonical payloads, ToolExecution terminal result, and AuditEvent with opaque IDs; no provider event is mutated and no Booking is created during preparation.
- Keep preparation idempotency key separate from confirmation idempotency key; the action token links the two explicit operations.

**Tests to write first:**

- [ ] Successful create/reschedule/cancel preparation stages exactly one action with exact token and `confirmation_required=true`.
- [ ] Preparation rejects policy-invalid, unavailable, unknown, inactive, non-bookable, unsupported-area, missing-requirement, duplicate-action, and Phase 1 primitive cases.
- [ ] Preparation calls the Calendar double only for the approved availability reads and never calls create/patch/cancel.
- [ ] Preparation persistence survives a new session and contains only sanitized values.
- [ ] Repeating the same preparation key replays; a different key cannot replace an existing action.

**Acceptance criteria:**

- No Calendar event exists solely because a prepare operation ran.
- Exactly one current action is represented directly on Conversation.
- Every successful preparation has an opaque action token and the correct expected state where applicable.

**Verification commands:**

- Red/green focused integration cycle: `uv run pytest -m integration tests/integration/test_booking_operations.py -k 'prepare'`
- `uv run ruff check src/receptionist/application/booking.py tests/integration/test_booking_operations.py`
- `uv run mypy src/receptionist/application/booking.py`

**Focused commit:** `feat: stage deterministic booking actions`

### Task 10: Create confirmation, deterministic Calendar identity, and recovery

**Depends on:** Tasks 5–9.

**Files:**

- Modify: `src/receptionist/application/booking.py`
- Modify: `tests/support/calendar_double.py`
- Test: `tests/integration/test_booking_operations.py`

**Purpose:** Implement the two-stage create claim/finalization flow and safe
same-key recovery around the non-transactional Google Calendar side effect.

**Interfaces:**

- `async def confirm_booking_action(session_factory: async_sessionmaker[AsyncSession], calendar: CalendarClient, request: ConfirmBookingActionRequest, *, now_utc: datetime) -> ConfirmationResult`
- Private helper `_deterministic_calendar_event_id(booking_id: UUID) -> str` uses a stable prefix plus lowercase base32hex UUID encoding without padding.
- Private helpers separate claim transaction from provider/finalization transaction; neither is exposed as a generic saga/workflow API.

**Claim behavior:**

- Claim/lock ToolExecution conflict-safely, lock exact Conversation/action token, and reject mismatched token without clearing a different valid action.
- Lock relevant rows in the required order `BusinessConfig → Conversation → Contact → Booking`; create has no existing Booking row, but retains the order for all rows that exist.
- Insert a local `Booking(status=pending)` with complete UTC interval and sanitized `booking_data`.
- Persist the configured Calendar ID and deterministic reserved Calendar event ID before the provider call; this pair does not prove event existence.
- Mark pending action `confirmed`, include execution identity in its sanitized payload, leave ToolExecution `started`, and commit the claim.

**Provider/finalization behavior:**

- Acquire the singleton BusinessConfig provider-write fence, then re-lock Conversation/Contact/Booking in compatible order and revalidate action/execution state.
- Re-evaluate policy and immediately query free/busy for the effective create interval; an earlier prepare read never authorizes the write.
- Create with the reserved event ID and a private marker containing only the opaque local Booking ID; no PII or raw request payload.
- On successful response, retrieve/validate the returned event identity, interval, marker, and status, then set Booking `confirmed`, set confirmation timestamp, mark ToolExecution `succeeded`, append sanitized AuditEvent, clear the pending action, and commit.
- On ambiguous provider response, retrieve the known event ID. A matching event finalizes success; a missing event retries safely with the same event ID; a mismatched marker returns `external_state_conflict` and never adopts the event.
- If final availability is busy or the operation is definitively rejected before event creation, clear both reserved Calendar reference columns, mark the local intent `cancelled`, persist a rejected/terminal failure result and audit, and clear the pending action.
- A crash after provider success but before local finalization leaves durable `started`/pending state recoverable by the same confirmation key; never insert a second event.

**Tests to write first:**

- [ ] Successful confirmed create persists Booking, pair, event marker, ToolExecution, AuditEvent, and cleared Conversation action after reload.
- [ ] No provider call occurs before exact confirmation.
- [ ] Deterministic event ID is stable for one Booking and valid for Google event ID requirements.
- [ ] Ambiguous create with provider event already present reconciles to one confirmed Booking on same-key retry.
- [ ] Ambiguous create with no event retries the same event ID without duplicate creation.
- [ ] Existing event with mismatched private marker is rejected safely and never adopted.
- [ ] Final availability becoming busy clears the reference pair and finalizes the never-created intent as cancelled/rejected.
- [ ] Same-key concurrent callers produce one provider event and one replay/recovery result.

**Acceptance criteria:**

- Only a reconciled Google event can produce `Booking.status=confirmed`.
- The reserved pair is explicit and recoverable but is not treated as proof of external existence while pending.
- Every create retry uses the same application key and Calendar event ID.

**Verification commands:**

- `uv run pytest -m integration tests/integration/test_booking_operations.py -k 'create or confirmation or ambiguous'`
- `uv run ruff check src/receptionist/application/booking.py`
- `uv run mypy src/receptionist/application/booking.py`

**Focused commit:** `feat: reconcile confirmed Calendar creates safely`

### Task 11: Reschedule and cancellation confirmation/recovery

**Depends on:** Tasks 5–10.

**Files:**

- Modify: `src/receptionist/application/booking.py`
- Modify: `tests/support/calendar_double.py`
- Test: `tests/integration/test_booking_operations.py`

**Purpose:** Implement provider-safe reschedule and cancellation using stale
state protection, identity-aware conflicts, minimal patching, and conditional
deletion.

**Reschedule behavior:**

- Require a Phase 2-managed confirmed Booking with both Calendar references; reject unsupported Phase 1 primitives deterministically.
- At claim, lock and compare the prepared expected-state fingerprint. A mismatch returns stale/state conflict with no provider mutation.
- Move the Booking to `pending` while retaining last reconciled values for recovery; store desired interval and prior state in sanitized ToolExecution arguments.
- Under the BusinessConfig fence, re-evaluate policy and query conflicts over the effective buffered interval with only the exact target event ID excluded. Do not subtract the old target interval from free/busy.
- Fetch the current event, verify the private Booking marker, and patch only application-owned fields (`start`, `end`, and marker only when explicitly required) with current ETag/`If-Match`.
- Reconcile the returned event’s ID, marker, and interval before updating local start/end, request metadata, `confirmed_at`, status, ToolExecution, AuditEvent, and pending action.
- ETag/marker/event divergence restores the previous confirmed local state and returns `external_state_conflict`; same-key retry may recover only when safe.

**Cancellation behavior:**

- Require the expected Booking snapshot and Phase 2-managed references.
- Move to `pending`, fetch the event, verify the marker, and delete conditionally with current ETag.
- Treat an already-absent event as reconciled cancellation; set `cancelled_at`, clear the pending action, mark ToolExecution terminal, and retain historical Calendar references.
- Treat ETag conflict, marker mismatch, or unexpected existing state as external conflict; restore the prior confirmed local state and do not silently delete/adopt.

**Tests to write first:**

- [ ] Reschedule success changes only application-owned event fields and preserves unrelated fake provider metadata.
- [ ] Reschedule returns stale conflict when another Conversation changed the Booking after preparation.
- [ ] Reschedule excludes only the exact target event and rejects another overlapping/recurring blocking event.
- [ ] 412/ETag failure restores local state and leaves no false confirmation.
- [ ] Missing target, missing references, cancelled Booking, active other operation, and mismatched marker are rejected deterministically.
- [ ] Cancellation succeeds when the provider event is already absent and retains historical references.
- [ ] Cancellation ETag/marker conflict restores the prior confirmed state.

**Acceptance criteria:**

- No full destructive Calendar update is used for reschedule.
- No cancellation or reschedule silently manufactures a missing external reference or overwrites divergent provider state.

**Verification commands:**

- `uv run pytest -m integration tests/integration/test_booking_operations.py -k 'reschedule or cancel'`
- `uv run ruff check src/receptionist/application/booking.py tests/integration/test_booking_operations.py`
- `uv run mypy src/receptionist/application/booking.py`

**Focused commit:** `feat: reconcile Calendar reschedules and cancellations`

### Task 12: Bind and verify create lock ordering

**Depends on:** Tasks 8–11.

**Files:**

- Modify: `src/receptionist/application/booking.py`
- Test: `tests/integration/test_booking_operations.py`

**Purpose:** Resolve the create locking ambiguity before broad concurrency
verification and prevent deadlock cycles between provider writes, customer
duplicate checks, and target Booking locks.

**Behavior and constraints:**

- Centralize only the private row-lock sequence in `application/booking.py`: `BusinessConfig(id=1)` first, then Conversation, then Contact, then Booking where a Booking exists.
- For create, lock BusinessConfig, Conversation, and Contact before checking exact active local duplicate `(contact, service, area, start, end)` rows; there is no Booking row to lock before insert.
- For reschedule/cancel, acquire BusinessConfig, Conversation, Contact, and target Booking in the same compatible order before checking snapshots/duplicates/provider state.
- Do not add advisory locks, a generic lock manager, a mutex, Redis, or a scheduling subsystem.

**Tests to write first:**

- [ ] Two Conversations for the same Contact attempting exact duplicate creates do not deadlock and produce one accepted local Booking/provider event plus one deterministic duplicate/replay result.
- [ ] Competing same-key/different-key operations complete within bounded test timeouts.
- [ ] A competing stale reschedule and create path cannot acquire overlapping rows in incompatible order.

**Acceptance criteria:**

- All paths acquiring overlapping rows document and use `BusinessConfig → Conversation → Contact → Booking`.
- Exact duplicate checks occur while the relevant locks are held.
- The global BusinessConfig row remains the only single-calendar provider-write fence.

**Verification commands:**

- `uv run pytest -m integration tests/integration/test_booking_operations.py -k 'concurrent or duplicate or lock'`
- Run the focused file twice against the same guarded database.
- `git diff --check`

**Focused commit:** `fix: bind Phase 2 booking lock ordering`

### Task 13: Public `check_availability` and `get_booking` behavior

**Depends on:** Tasks 3–6 and 10–12.

**Files:**

- Modify: `src/receptionist/application/booking.py`
- Test: `tests/integration/test_booking_operations.py`
- Test: `tests/unit/test_booking_models.py` if output-contract coverage belongs there

**Purpose:** Complete the two read operations with honest provider authority,
customer-boundary enforcement, and safe divergence reporting.

**Interfaces:**

- `async def check_availability(session: AsyncSession, calendar: CalendarClient, request: AvailabilityRequest, *, now_utc: datetime) -> AvailabilityResult` applies current policy and real free/busy without reserving or writing local state.
- `async def get_booking(session: AsyncSession, calendar: CalendarClient, request: GetBookingRequest) -> BookingReadResult` verifies the Conversation Contact boundary and fetches provider state when the Booking is Phase 2-managed.

**Behavior and constraints:**

- `check_availability` returns policy validity, normalized interval, provider-reported free/busy status, read timestamp, and safe stable error; no local Booking row alone answers availability and no alternative slots are fabricated.
- `get_booking` returns `in_sync`, `provider_divergent`, `provider_missing`, or `operation_in_progress` reconciliation status; it never silently overwrites local/provider state.
- A Booking ID belonging to another Contact is reported as `booking_not_found` without disclosure.
- Phase 1 primitive rows without a valid Calendar pair return `external_reference_missing`/`booking_state_conflict` safely and are never migrated/adopted or used to manufacture an event.
- A pending durable started operation directs same-key recovery and does not finalize through retrieval.

**Tests to write first:**

- [ ] Availability tests prove provider-free, provider-busy, policy-invalid, and Calendar-unavailable results.
- [ ] Retrieval tests prove Contact ownership, in-sync data, divergent interval/status, missing event, and started-operation results.
- [ ] Retrieval of Phase 1 primitive Booking rows is safely rejected.
- [ ] Read operations produce no ToolExecution/Booking/AuditEvent mutation.

**Acceptance criteria:**

- Real Calendar authority is visible in both read outputs without leaking raw provider payloads.
- Customer boundary and unsupported primitive behavior are deterministic and safe.

**Verification commands:**

- `uv run pytest -m unit tests/unit/test_booking_models.py tests/unit/test_booking_policy.py`
- `uv run pytest -m integration tests/integration/test_booking_operations.py -k 'availability or get_booking or ownership'`

**Focused commit:** `feat: add booking availability and retrieval operations`

### Task 14: PostgreSQL concurrency, crash, and reconciliation coverage

**Depends on:** Tasks 8–13.

**Files:**

- Modify: `tests/support/calendar_double.py`
- Modify: `tests/integration/test_booking_operations.py`
- Modify: `tests/integration/conftest.py` to provide only the narrow function-scoped operation fixtures used by this test matrix, preserving the existing database guard
- Modify: `tests/integration/test_migrations.py` to assert the final Calendar-reference migration state during the migration cycle

**Purpose:** Prove real PostgreSQL behavior for the two-stage provider protocol,
lock ordering, replay, rollback, and recovery against the same guarded test
database.

**Test matrix:**

- [ ] Two same-key confirmation callers: one claim/provider mutation, one replay/recovery, one Calendar event.
- [ ] Two different confirmation keys: second cannot bypass confirmed pending action/active execution and returns `operation_in_progress` or stale conflict.
- [ ] Competing Conversations on one Contact: lock ordering prevents deadlock; exact duplicate create is deterministic.
- [ ] Two stale reschedules: only the first matching fingerprint can mutate; the second cannot overwrite.
- [ ] Provider success followed by ambiguous response/local finalization failure: durable `started` recovery returns one event and one final local Booking.
- [ ] Provider event already exists under deterministic ID: same-key recovery reconciles it exactly once.
- [ ] Existing event with mismatched private marker: no adoption, safe conflict, local state restored.
- [ ] 412 ETag conflict: no false local update; prior local state remains confirmed.
- [ ] Final free/busy/conflict recheck becomes busy: no new event, reference pair clears for a never-created intent, terminal rejection persists.
- [ ] Cancellation when event is already absent: local cancellation reconciles successfully and retains historical references.
- [ ] Unexpected local failure before final commit rolls back local changes; durable claim remains recoverable only when provider ambiguity can be reconciled.
- [ ] Every expected `IntegrityError` rolls back explicitly before session reuse.
- [ ] Every destructive cleanup/reset calls `assert_safe_test_database`; prefer unique synthetic IDs and narrow cleanup over a reset framework.

**Acceptance criteria:**

- Tests run twice consecutively against the same guarded PostgreSQL database with no manual schema reset between runs.
- No test uses sleeps, application/advisory locks, live Calendar calls, or weakened constraints.

**Verification commands:**

- `make test-db-up`
- `make test-integration`
- `make test-integration`
- `make test-db-down`

**Focused commit:** `test: cover Phase 2 booking concurrency and recovery`

### Task 15: Automated Calendar boundary and no-live-provider CI gates

**Depends on:** Tasks 5–14.

**Files:**

- Modify: `tests/unit/test_google_calendar.py`
- Modify: `tests/support/calendar_double.py`
- Modify: `tests/integration/test_booking_operations.py` only for explicit injected-double use
- Modify: `docs/development/testing.md`

**Purpose:** Make provider-free automation explicit while still testing the
real Google boundary’s request translation and safe error mapping.

**Behavior and constraints:**

- Unit tests mock Google service/request/transport objects and assert request bodies/parameters, timeout configuration, private marker handling, ETag headers, recurrence expansion, and mapping of provider responses.
- Operation integration tests inject the deterministic double; they never construct `GoogleCalendarClient` with live credentials.
- Add a test guard that fails if normal test execution attempts to use live Calendar credentials or a network transport; do not weaken it for convenience.
- Document that `make test`, `make test-unit`, `make test-integration`, CI, `make check`, and `make verify` are provider-free.
- Keep `.github/workflows/ci.yml` unchanged unless a separately reviewed, strictly no-network test guard is demonstrably required; do not add Calendar credentials or network steps.

**Tests to write first:**

- [ ] Mocked Google translation tests for free/busy, event listing/conflicts, get, create, patch, delete, ETag/412, timeout, auth, quota, and ambiguous response mapping.
- [ ] Assert no raw Google dictionary enters application-owned result models or persisted JSON.
- [ ] Assert all operation tests receive a double explicitly.

**Acceptance criteria:**

- Normal CI has no live Calendar secret, credential, network call, or smoke step.
- The narrow double models only behavior required by the operation test matrix.

**Verification commands:**

- `make test-unit`
- `make test-integration`
- `make check`
- Inspect CI workflow for absence of Calendar credentials/network smoke steps.

**Focused commit:** `test: enforce provider-free automated Calendar coverage`

### Task 16: Guarded dedicated non-production Calendar smoke mechanism

**Depends on:** Tasks 1, 5, and 10–13.

**Files:**

- Create: `scripts/calendar_smoke.py`
- Create: `tests/unit/test_calendar_smoke.py`
- Modify: `Makefile`
- Modify: `.env.example` only for empty smoke variable names/placeholders
- Modify: `docs/development/setup.md`
- Modify: `docs/development/testing.md`

**Purpose:** Provide the smallest manual path to prove the real integration on
a dedicated non-production Calendar without making it part of CI or normal
application startup.

**Interfaces and behavior:**

- Add a guarded `make calendar-smoke` target that runs `uv run python scripts/calendar_smoke.py` only when explicit smoke opt-in and all runtime credentials are present.
- Require a dedicated smoke marker/configuration value such as `RECEPTIONIST_CALENDAR_SMOKE_CONFIRM=DEDICATED_NON_PRODUCTION_ONLY`, a dedicated Calendar ID, and an explicit synthetic smoke environment.
- Document the exact OAuth bootstrap scopes `https://www.googleapis.com/auth/calendar.events` and `https://www.googleapis.com/auth/calendar.freebusy`; do not use the broader `https://www.googleapis.com/auth/calendar` scope.
- Refuse known production/customer identifiers when configured through a conservative allowlist/prefix or explicit dedicated-calendar marker; print the refusal without printing credentials.
- The script creates or prepares a synthetic Contact/Conversation and uses the application operations to prove free/busy, confirmed create, same-key recovery/replay, `get_booking`, reschedule, cancellation, and cleanup.
- Cleanup uses the persisted deterministic event ID and guarded cancellation; it does not delete arbitrary events or touch local production databases.
- If smoke credentials are unavailable, exit with a clear “not run: credentials unavailable” result in manual verification; do not claim success.

**Tests to write first:**

- [ ] Unit-test the smoke guard with missing opt-in, missing credentials, and unsafe Calendar marker/ID.
- [ ] Unit-test that smoke output/logging redacts all SecretStr values and reports only opaque IDs.
- [ ] Keep actual live calls out of automated tests; verify the script’s request construction with the deterministic double only.

**Acceptance criteria:**

- Smoke is manual and isolated from normal CI.
- The script proves real create/recovery/get/reschedule/cancel behavior when run against the dedicated test Calendar and performs cleanup.
- No credential, Calendar ID, event ID from the smoke account, or customer data is committed.

**Verification commands:**

- `make calendar-smoke` only with explicitly provisioned dedicated non-production credentials.
- Otherwise record `not run: dedicated Calendar credentials unavailable` and verify `make check`/`make verify` remain provider-free.

**Focused commit:** `feat: add guarded Calendar smoke verification`

### Task 17: Security, privacy, dependency, and secret verification

**Depends on:** Tasks 1–16.

**Files:**

- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_booking_models.py`
- Modify: `tests/unit/test_google_calendar.py`
- Modify: `tests/integration/test_booking_operations.py`

**Purpose:** Prove that the real integration does not turn credentials,
provider payloads, ETags, or customer PII into logs or durable records.

**Tests and checks:**

- [ ] OAuth client secret and refresh token are absent from Settings repr, safe summaries, startup/configuration logs, exceptions, and smoke output.
- [ ] Google raw Event/free/busy payloads and HTTP response bodies never enter ToolExecution, AuditEvent, Booking JSON, or ordinary logs.
- [ ] ETag is used transiently for conditional patch/delete and never persisted.
- [ ] Raw phone/email/address/transcript never enters operation arguments/results/audit/booking metadata or ordinary logs; only opaque IDs and bounded canonical codes remain.
- [ ] The private Calendar marker contains only the opaque local Booking ID and no PII.
- [ ] Dependency audit, secret scan, and lock consistency cover the new Google packages without baseline weakening.

**Acceptance criteria:**

- No `.env`, token, credential JSON, live Calendar ID, event payload, or provider secret is tracked.
- `detect-secrets`, `pip-audit`, and PII-safe persistence assertions pass.

**Verification commands:**

- `make secrets`
- `make audit`
- `make security`
- `git ls-files | rg '(^|/)(token|credentials|client_secret)'` and confirm no credential file is tracked.
- `git diff --check`

**Focused commit:** `test: verify Phase 2 Calendar privacy boundaries`

### Task 18: Documentation, migration/drift gates, final verification, and REVIEW closeout

**Depends on:** Tasks 1–17.

**Files:**

- Modify: `README.md`
- Modify: `docs/development/setup.md`
- Modify: `docs/development/testing.md`
- Modify: `docs/architecture/integration-boundaries.md`
- Modify: `docs/roadmap.md` at final candidate closeout to move Phase 2 from `IN PROGRESS` to `REVIEW`, never to `COMPLETE`
- Test/verification only: no new behavior files

**Purpose:** Document the implemented Phase 2 boundary and close with fresh
evidence, preserving the reviewed architecture and exact AGENTS.md report
contract.

**Documentation behavior:**

- README describes Phase 2 as the implementation candidate under `REVIEW` only after all verification is complete; until then keep roadmap status `IN PROGRESS`.
- Setup documents optional runtime Google settings, out-of-band OAuth authorized-user refresh-token provisioning, one configured Calendar, no application consent UI, and no credential commits.
- Testing documents provider-free CI/deterministic double behavior, guarded PostgreSQL repeatability, migration/drift checks, and the separate smoke command.
- Integration-boundaries documentation records that Google Calendar is the concrete Phase 2 authority for live availability/event state while application policy/PostgreSQL remain authoritative for policy/local state; do not add HubSpot or other provider code.
- Do not alter the approved Phase 2 design document during implementation except for an independently reviewed design conflict; implementation docs must describe behavior actually present.

**Fresh final verification gates:**

- [ ] Focused unit tests for every TDD task.
- [ ] `make test-unit`.
- [ ] `make test-integration`.
- [ ] `make test-integration` a second consecutive time against the same guarded database without manual schema reset.
- [ ] Migration `upgrade head → downgrade base → upgrade head` with `assert_safe_test_database`.
- [ ] `make migration-check`.
- [ ] `make alembic-verify`.
- [ ] `make format-check` and `make lint` (Ruff format/lint).
- [ ] `make typecheck` (strict mypy).
- [ ] `make secrets` (fail-closed secret scan).
- [ ] `make audit` (dependency audit).
- [ ] `git diff --check`.
- [ ] `docker compose --profile test config`.
- [ ] `docker build -t arabic-english-ai-receptionist:local .`.
- [ ] Separate guarded live Calendar smoke when dedicated credentials are available; otherwise record explicitly that it was not run and why.
- [ ] Full diff/scope audit against the Phase 1 `main` baseline; confirm no HubSpot, messaging, voice, LLM, UI, n8n, worker, outbox publisher, Redis, multitenancy, or other Phase 3+ behavior.
- [ ] Manual durable-state reload verifies Calendar references, status transitions, ToolExecution recovery, exact pending action clearing, sanitized audit, and no raw provider/PII payload.

**Acceptance criteria:**

- Phase 2 implementation candidate is complete and ready for independent review, but roadmap status is only `REVIEW`; it is never marked `COMPLETE` by Codex.
- Phase 3 remains `NOT STARTED`.
- No PR, merge, Phase 3 work, Dependabot change, or unrelated dependency update occurs.

**Focused commit:** `docs: document Phase 2 booking and Calendar operations`

## Ordered commit strategy

Create one focused commit at the end of each task, in this exact order:

1. `build: add Phase 2 Calendar dependencies and settings`
2. `feat: add Booking Calendar reference migration`
3. `feat: define Phase 2 booking operation contracts`
4. `feat: add deterministic booking policy functions`
5. `feat: add narrow Google Calendar boundary`
6. `feat: add Calendar-backed availability decisions`
7. `feat: enforce exact booking action identity`
8. `feat: add conflict-safe booking execution claims`
9. `feat: stage deterministic booking actions`
10. `feat: reconcile confirmed Calendar creates safely`
11. `feat: reconcile Calendar reschedules and cancellations`
12. `fix: bind Phase 2 booking lock ordering`
13. `feat: add booking availability and retrieval operations`
14. `test: cover Phase 2 booking concurrency and recovery`
15. `test: enforce provider-free automated Calendar coverage`
16. `feat: add guarded Calendar smoke verification`
17. `test: verify Phase 2 Calendar privacy boundaries`
18. `docs: document Phase 2 booking and Calendar operations`

Each commit must contain only its task’s expected files and pass its focused
verification. Do not squash, amend, rebase, merge, force-push, create a PR, or
modify Dependabot PR #4/#5. If a task reveals a durable architecture conflict
that cannot be solved within this approved design, stop and report it before
changing the plan or implementation.

## Structured implementation closeout checklist

The implementing context must finish with exactly these AGENTS.md-compatible
sections, populated with fresh evidence and no unsupported claims:

1. **Status** — `Phase 2 implementation candidate complete — pending independent review and human approval.`; roadmap Phase 2 `REVIEW`, Phase 3 `NOT STARTED`.
2. **Git** — starting/ending SHA, branch, ordered commit list, relationship to `main`, and clean/dirty worktree.
3. **Files changed** — complete source/test/migration/docs/dependency list with no generated/debug residue.
4. **Implementation summary** — configuration, schema, policy, Calendar boundary, operation surface, reconciliation, and recovery.
5. **Architectural decisions** — single business/calendar, authority hierarchy, lock order, no generic abstractions, and provider boundary.
6. **Tests executed** — exact focused/unit/integration commands, counts, results, and two consecutive integration-suite results.
7. **Quality/tooling results** — format/lint, strict mypy, migration/drift, secret scan, dependency audit, Compose, and Docker results.
8. **Manual verification** — guarded non-production Calendar smoke result or explicit credential-unavailable skip, plus durable reload verification.
9. **Deviations** — every deviation from this plan/design, or `None` with evidence.
10. **Known limitations** — residual Calendar read/write race, provider quotas/timeouts, operator OAuth provisioning, and any smoke limitation.
11. **Security and cost** — secret/PII findings, provider scope, bounded calls, and explicit manual-smoke/API cost considerations.
12. **Scope audit** — explicit confirmation that no Phase 3+ behavior, Dependabot change, PR, merge, worker, outbox publisher, or unrelated dependency work was added.

Stop at `REVIEW` and wait for independent implementation review.
