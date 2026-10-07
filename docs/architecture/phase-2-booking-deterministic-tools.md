# Phase 2 design: Booking & Deterministic Business Tools

## Status and boundary

This document is the durable design for Phase 2. It is a design specification,
not an implementation plan. Phase 2 is `IN PROGRESS` for design only. The
implementation must remain on the single-business modular-monolith branch and
must use the Phase 1 schema and domain rules as its starting point.

Phase 2 turns the existing `Service`, `BusinessConfig`, `Conversation`,
`Booking`, `ToolExecution`, and `AuditEvent` primitives into a small set of
deterministic application operations. It does not add a public HTTP API,
provider client, worker, or conversational/LLM layer.

The authority order remains:

1. `BusinessConfig` plus the relational `Service` catalog author configured
   services, service areas, opening hours, and booking policy.
2. PostgreSQL authoritatively stores this application's internal state.
3. Application policy decides whether an operation is valid, confirmed, and
   safe.
4. A future calendar provider will author live external availability and
   external event state. It is not present in Phase 2.
5. An LLM, when introduced in a later phase, is authoritative for none of
   these facts or side effects.

There remains one business configuration and one service catalog. No tenant,
organization, membership, or multi-business infrastructure is introduced.

## Concrete operation surface

The Phase 2 surface is finite and explicit. It consists of plain request/result
models and focused application functions or classes in the smallest relevant
module. There is no generic tool registry, command bus, repository, unit of
work, workflow engine, state-machine library, or provider abstraction.

| Operation | Purpose | Writes state? | Idempotency key |
| --- | --- | --- | --- |
| `lookup_service` | Resolve one service from an exact code, canonical name, or alias. | No | Not required |
| `lookup_service_area` | Resolve one active configured area from an exact code, canonical name, or alias. | No | Not required |
| `prepare_create_booking` | Validate a create request and stage one `create_booking` pending action. | Yes, Conversation and ToolExecution/AuditEvent | Required |
| `prepare_reschedule_booking` | Validate a reschedule request and stage one `reschedule_booking` pending action. | Yes, Conversation and ToolExecution/AuditEvent | Required |
| `prepare_cancel_booking` | Validate a cancellation request and stage one `cancel_booking` pending action. | Yes, Conversation and ToolExecution/AuditEvent | Required |
| `confirm_booking_action` | Confirm the exact current booking action and apply its local Booking mutation. | Yes, Conversation, Booking, and ToolExecution/AuditEvent | Required |

`confirm_booking_action` is deliberately the only mutation entry point after
customer confirmation. It accepts an expected pending-action type and the
conversation identifier; it does not parse free text and it does not accept a
new booking payload. The caller must already have an explicit customer
confirmation signal. The operation verifies that signal's expected action type
against the action stored on the locked Conversation.

No operation claims live availability. A successful preparation result can
include `availability_status: "unresolved"`; this is an honest application
result, not a proposed or fabricated Calendar slot.

### Request and result shapes

Requests and results use strict Pydantic structures with forbidden extra
fields. Results are structured as either a success or a stable application
error; callers do not receive raw SQLAlchemy exceptions, provider payloads, or
free-form diagnostic text.

The lookup results contain canonical service/area codes and the safe configured
display fields needed by a later channel. Service lookup returns the service's
active/bookable status, duration, pricing structure, and booking requirements.
Area lookup returns the canonical area code, bilingual names, and active flag.

Create preparation accepts a canonical service selector, an active service-area
selector, a timezone-aware requested start, an optional timezone-aware end, and
the bounded values for the service's configured booking requirements. The
service duration supplies the end when the end is omitted. Reschedule accepts a
booking identifier and the new requested interval; it retains the existing
service and service area. Cancellation accepts a booking identifier and only
the bounded, sanitized cancellation context needed for the local audit.

All operation inputs use aware datetimes. Naive datetimes are rejected rather
than assigned an implicit timezone. The operation normalizes accepted values to
UTC before validation and persistence. A future channel parser may convert a
business-local phrase into an aware datetime, but that parser is outside this
phase.

Successful preparation results return the normalized action payload and
`confirmation_required: true`. Successful confirmation results return the
opaque local booking identifier, local Booking status, normalized UTC interval,
and `availability_status: "unresolved"` where relevant. The result does not
contain a Calendar event ID or provider claim.

The stable result envelope is intentionally small:

- success: `ok`, `operation`, `replayed`, and operation-specific `data`;
- error: `ok=false`, `operation`, `error.code`, `error.message`,
  `error.retryable`, and bounded safe `error.details`.

The application owns the following versioned `Booking.booking_data` shape for
Phase 2-created rows. Values are canonical and JSON-safe; the exact
implementation may use a strict model before serializing it:

```text
schema_version: 1
service_code: string
service_area_code: string
requested_start_at_utc: ISO-8601 UTC string
requested_end_at_utc: ISO-8601 UTC string
requirements: object of configured key -> bounded string value
availability_status: "unresolved"
last_operation: "create_booking" | "reschedule_booking"
```

For a cancelled row, `last_operation` becomes `cancel_booking` and the prior
requested interval remains for auditability. Cancellation context is bounded
and sanitized; it is not a transcript field. `booking_data` never stores the
idempotency key, raw phone number, raw address, or provider payload.

## Deterministic configuration lookup

### Services

The relational `Service` catalog is authoritative for services. Lookup is exact
and deterministic:

- normalize the supplied selector using the Phase 1 catalog normalization
  (`NFKC`, whitespace normalization, and case-folding);
- compare it against the service code, bilingual names, and aliases;
- return exactly one canonical service;
- reject an unknown selector with `unknown_service`;
- reject an inactive service with `service_inactive`; and
- reject a service that is active but not bookable with
  `service_not_bookable`.

There is no fuzzy matching, substring ranking, language-model interpretation,
or “best” match. If persisted catalog data produces more than one exact match,
the operation returns a deterministic `configuration_conflict` error and does
not choose one silently. Phase 2 does not add a second service vocabulary or
embed services inside `BusinessConfig`.

### Service areas

Configured areas remain the `BusinessConfig.service_areas` catalog. Lookup uses
the same exact normalization against area code, bilingual names, and aliases.
Only an active area can be used for a booking request. Unknown or inactive
areas return `unsupported_service_area`; ambiguous persisted configuration
returns `configuration_conflict`.

The lookup reads the singleton configuration and does not manufacture a local
area or infer coverage from a phone number, address, or model output.

## Business-hours and booking-policy evaluation

The policy evaluator is a pure deterministic function over a validated
configuration snapshot, a normalized interval, and an explicit `now_utc`.
The application operation supplies `now_utc`; tests do not depend on an
uncontrolled wall clock.

The evaluator:

1. converts the UTC interval to `BusinessConfig.timezone` using `zoneinfo`;
2. rejects an interval that crosses a local calendar day, because Phase 1
   weekly windows are same-day windows;
3. requires the buffered interval to fit entirely inside one configured weekly
   opening window. Opening time is inclusive and closing time is exclusive;
4. requires the requested start to align with the configured
   `slot_increment_minutes` from local midnight;
5. requires the start to be at least `minimum_notice_minutes` after `now_utc`;
6. requires the requested local date not to exceed
   `maximum_advance_days` from the current business-local date; and
7. returns `outside_business_policy` when any configured rule fails.

`buffer_before_minutes` and `buffer_after_minutes` are used only for the
opening-window boundary check. They do not calculate capacity or reserve
slots. Phase 2 does not infer conflicts from buffers or from other local rows.

For timezone and DST behavior, persisted appointment timestamps are always
timezone-aware UTC. The business-local conversion is used only for policy
evaluation. A future local-time parser must reject nonexistent or ambiguous
wall times unless it supplies an explicit fold/offset; Phase 2 never guesses.
The reference business uses `Asia/Riyadh`, but the evaluator uses the
configured IANA timezone and is not hard-coded to Riyadh.

The evaluator has no availability provider and no slot enumeration. A policy-
valid interval is only policy-valid; it is not claimed to be available.

## Booking request and local Booking semantics

### What a Phase 2 Booking means

A Phase 2 `Booking` row is an application-owned, provider-independent record
of a customer-confirmed local booking request. It records the contact, service,
normalized UTC interval, service area and bounded request details in the
existing `booking_data` JSON document. The document has a small versioned shape
owned by this application, including the canonical service/area codes and
`availability_status: "unresolved"`. It contains no provider identifiers,
Calendar event IDs, raw transcript, or raw provider payload.

The Phase 1 `Booking.status` vocabulary is retained:

- `pending` means the locally accepted request still awaits external
  availability/provider resolution. This is the only status Phase 2 can create
  or maintain for a live requested appointment;
- `confirmed` means an appointment has been confirmed by the later external
  availability integration. Phase 2 cannot reach or claim this state; and
- `cancelled` means a local pending request was explicitly cancelled. Phase 2
  does not cancel an externally confirmed appointment because it cannot act on
  the authoritative external state yet.

The existing nullable start/end pair remains legal for Phase 1 primitives, but
all Phase 2 booking creations and reschedules require a complete interval and
persist both UTC values. PostgreSQL's pair/order constraints remain the final
database guard.

### Create

`prepare_create_booking` resolves the active, bookable service and active area,
validates required service details, derives the end when necessary, evaluates
the business policy, and stages a `create_booking` action. It does not insert a
Booking. `confirm_booking_action` revalidates the stored action against current
configuration and time, then inserts one `pending` Booking and clears the
Conversation action in the same transaction.

The operation rejects a create when the service, area, required details,
interval, policy, or Conversation state is invalid. An exact existing active
local booking for the same contact, service, area, and interval is treated as a
safe `duplicate_replay` rejection when presented with a different idempotency
key. This is an exact local duplicate guard only; Phase 2 does not claim that
overlapping intervals are unavailable.

### Reschedule

Reschedule targets one existing local Booking. Preparation locks and validates
the target, preserves its service and area, validates the new interval against
current policy, and stages a `reschedule_booking` action containing the target
Booking identifier and canonical new values. Confirmation locks the Booking
again, verifies it is still `pending`, updates its UTC interval and safe request
metadata, writes the audit record, and clears the pending action.

A cancelled Booking is rejected with `booking_state_conflict`. A Booking in
`confirmed` state is rejected with `external_availability_unresolved`, because
the external appointment is authoritative and no Phase 2 provider operation
exists. A missing target is `booking_not_found`.

### Cancellation

Cancellation preparation locks the target local Booking and stages a
`cancel_booking` action only for a `pending` Booking. Confirmation re-locks it,
sets `status=cancelled` and `cancelled_at`, records the safe cancellation
metadata, and clears the pending action atomically.

Cancellation of an already-cancelled Booking is a deterministic
`booking_state_conflict`. Cancellation of a `confirmed` Booking is rejected as
`external_availability_unresolved`; it must wait for the later integration that
owns the external appointment. There is no provider cancellation request in
Phase 2.

### Pending confirmation behavior

The Phase 1 Conversation row is the sole current pending-action slot. Every
prepare operation requires that it be empty. If another critical action exists,
the operation returns `pending_action_conflict` and does not replace it. An
exact replay with the same idempotency key returns the stored result instead of
staging a new action.

Preparation stores only the canonical, sanitized action payload. Phase 2 does
not add a pending-action history table or a second confirmation mechanism. The
existing helpers are used as follows:

- `stage_pending_action` stages `awaiting_confirmation`;
- `confirm_pending_action` is called inside the confirmation transaction;
- the local Booking mutation and `clear_pending_action` occur in that same
  transaction;
- an optional non-null `expires_at` is honored if present, but Phase 2 does not
  invent a new confirmation TTL because Phase 1 has no such configuration
  field.

If confirmation finds an expired, missing, mismatched, or no-longer-valid
action, it performs no Booking mutation and returns `stale_pending_action`.
The stale action is cleared in the same transaction so a new explicit prepare
is required. Unexpected failures roll back the action and all other writes.

## Idempotency and replay

All three preparation operations and `confirm_booking_action` require a
non-empty opaque idempotency key. Lookup operations do not mutate state and do
not require one.

The existing partial unique index makes a non-null `ToolExecution.idempotency_key`
globally unique across the application. That database fact defines the key
scope: callers must generate one unique value per logical operation, not merely
one value per conversation. The operation also records the tool name,
conversation ID, and a sanitized argument fingerprint so accidental reuse can
be rejected as `idempotency_conflict` rather than silently changing meaning.

The transaction protocol is deliberately local to these operations:

1. Begin one PostgreSQL transaction owned by the application operation.
2. Try to create the `ToolExecution` as `started` with sanitized arguments.
   A unique-key conflict waits on/reads the existing row under a row lock.
3. For an existing row, verify tool, conversation, and argument fingerprint.
   A `succeeded` row returns its stored sanitized result with a replay marker;
   a `failed` or `rejected` row returns its stored terminal result and does not
   retry; a mismatched row returns `idempotency_conflict`.
4. For a newly claimed key, lock and validate the Conversation and any target
   Booking, perform the one local mutation, append the sanitized AuditEvent,
   and update ToolExecution to its terminal result before commit.
5. Commit once. The caller receives the committed result.

Committed Phase 2 executions are terminal. `started` is an in-transaction
claim, not a durable work queue. If an unexpected exception rolls back the
transaction, the claim and local mutation roll back together; no worker or
retry scheduler is introduced. A pre-existing committed `started` row is a
state conflict and is never executed blindly.

The same idempotency key always returns the same prior success or prior safe
error. A new key is not allowed to bypass an existing Conversation pending
action or a locked Booking state. Exact local duplicate create detection adds a
second safety fence for a different key, but it is not a distributed
idempotency service.

## Error vocabulary

The operations expose a small stable application-domain error code set. Each
error has a safe message, structured non-PII details where useful, and a
deterministic retryable/non-retryable classification.

- `invalid_input` — strict request shape, missing required value, naive time,
  or malformed bounded detail;
- `unknown_service` — no exact configured service match;
- `service_inactive` — exact service exists but is inactive;
- `service_not_bookable` — exact service exists but is not bookable;
- `unsupported_service_area` — area is unknown or inactive;
- `configuration_conflict` — persisted catalog/configuration is ambiguous;
- `invalid_requested_time` — malformed or impossible normalized interval;
- `outside_business_policy` — opening hours, notice, advance, slot, or buffer
  policy rejects the interval;
- `confirmation_required` — a caller attempted a critical mutation without
  first preparing and explicitly confirming the action;
- `pending_action_conflict` — another current critical action already exists;
- `stale_pending_action` — the action is absent, expired, mismatched, or no
  longer valid under current state/configuration;
- `booking_not_found` — target Booking does not exist;
- `booking_state_conflict` — target Booking is already cancelled or otherwise
  cannot accept this local transition;
- `idempotency_conflict` — a key was reused for a different operation or
  arguments;
- `duplicate_replay` — an exact local create already exists under another key;
- `external_availability_unresolved` — the requested decision requires live
  provider availability or external event state that Phase 2 does not have.

No provider-specific error codes are introduced. Database uniqueness or row
lock outcomes are mapped into this vocabulary; raw constraint names and SQL
details do not cross the operation boundary.

## Transactions and concurrency

The public application operations own their narrow transaction. Pure lookup,
normalization, and policy functions do not commit and do not own a broad
transaction. No generic unit-of-work or repository layer is added.

### Create preparation and confirmation

Preparation locks the Conversation row after claiming the idempotency key. It
checks the empty pending-action slot, resolves current configuration, then
stages the action and terminal ToolExecution/AuditEvent in one transaction.
Confirmation locks the Conversation row first and then the Contact row before
checking exact duplicate local Bookings and inserting the new Booking. The
Conversation lock serializes competing critical actions for one conversation;
the Contact lock serializes exact-duplicate checks for the same contact across
conversations. The existing PostgreSQL constraints remain authoritative.

### Reschedule and cancellation

Preparation and confirmation lock the Conversation row and then the target
Booking row in a consistent order. They re-check target status after the lock.
Concurrent operations therefore either replay their own idempotency result or
observe the first committed transition and return a deterministic state error.

### Failure and rollback

Expected validation, policy, pending-action, idempotency, and state rejections
are stored as `ToolExecution.status = rejected` with safe error fields and an
AuditEvent, then committed without the requested Booking mutation. Unexpected
database/application exceptions roll back ToolExecution, Conversation,
Booking, and AuditEvent together. No external side effect occurs inside the
transaction, so Phase 2 does not need a two-phase commit or a compensating
workflow.

There is no Redis lock, advisory lock, application mutex, or background worker.

## Persistence and audit

Phase 2 uses the existing Phase 1 tables and constraints. The initial design
does not add provider-specific columns, external IDs, a new booking table, or a
new pending-action table. The existing `Booking.booking_data` JSON is used for
a small versioned, application-owned request document because the current
schema already provides the provider-independent storage primitive.

Each state-changing operation persists:

- a `ToolExecution` with sanitized arguments and sanitized terminal result;
- the Conversation pending-action change, if applicable;
- the Booking local mutation, if applicable; and
- one sanitized `AuditEvent` with opaque IDs, operation name, outcome, and
  stable error code where relevant.

No raw phone number, email, address, conversation text, secret, raw provider
payload, or unbounded user-supplied detail is written to ordinary logs,
ToolExecution JSON, Booking metadata, or AuditEvent metadata. Logs prefer
`conversation_id`, `booking_id`, and `tool_execution_id`. Phase 2 does not
publish `OutboxEvent`, process it, or add a publisher.

## Future integration seam

The Phase 2 seam is the persisted local Booking request and its deterministic
operation result. A later Calendar phase may read the explicit local request,
perform an external availability/event operation at its real integration
boundary, and then update the local record under a separately approved design.
That future phase will own provider credentials, retries, provider idempotency,
external IDs, and provider error mapping.

Phase 2 creates no Calendar interface, protocol, adapter base class, registry,
SDK dependency, fake provider, or provider-shaped model. It also does not use
`ProviderEventReceipt` or `OutboxEvent` for hypothetical behavior.

## Testing strategy

The future implementation must use TDD for meaningful behavior and preserve
repeatability against the same guarded PostgreSQL test database. Destructive
setup or cleanup must call and remain behind `assert_safe_test_database`.

The test matrix includes:

- pure unit tests for strict request/result validation, catalog normalization,
  exact service/area lookup, UTC normalization, DST edge handling, and each
  policy boundary;
- unit tests for required service details, derived duration, complete
  interval checks, and every stable error code reachable without PostgreSQL;
- PostgreSQL integration tests for successful prepare/confirm create,
  reschedule, and cancellation, including durable reload in a new session;
- integration tests proving only explicit confirmation mutates Booking and
  that a second pending critical action is rejected;
- replay tests for successful, failed, rejected, and mismatched idempotency
  keys;
- concurrent same-key tests proving PostgreSQL uniqueness/row locks produce one
  local mutation and one replay result;
- concurrent different-operation tests where Conversation and Booking locks
  must produce one committed transition and one deterministic conflict;
- rollback tests proving a failed local mutation leaves Conversation, Booking,
  ToolExecution, and AuditEvent consistent;
- service active/bookable and service-area catalog tests, including exact
  ambiguity rejection;
- timezone/business-hours tests for minimum notice, maximum advance, window
  edges, slot increments, buffers, DST ambiguity/nonexistence, and the
  configured `Asia/Riyadh` reference values;
- tests that assert a policy-valid result reports availability as unresolved
  and never creates an external event or fabricates a live slot;
- repeatability tests that run the integration suite twice without relying on
  a manual schema reset between runs.

No test may call Calendar, CRM, messaging, voice, an LLM, or any external SaaS
provider.

## Explicit non-goals

Phase 2 does not implement:

- Google Calendar API/client, live availability, external calendar events, or
  provider booking identifiers;
- create/reschedule/cancel workflows against an external provider;
- HubSpot or any CRM;
- WhatsApp, Twilio, Vapi, audio, transcription, or text-to-speech;
- LLM/model clients, prompts, conversational orchestration, or tool dispatch;
- a public booking HTTP API, webhooks, authentication, dashboard, or UI;
- n8n, reminder jobs, outbox publishing, background workers, retry schedulers,
  or retention/deletion jobs;
- technician dispatch, capacity planning, geospatial routing, resource
  scheduling, recurrence, multiple calendars, or a complex slot engine;
- multitenancy, tenant/organization infrastructure, or business membership;
- repositories, unit-of-work frameworks, generic command/tool/workflow/state
  infrastructure, provider registries, plugin architectures, LangChain, or
  LangGraph; or
- any Phase 3–12 behavior.

## Design questions intentionally deferred

The following are explicit later-phase decisions rather than hidden gaps in
Phase 2:

1. Phase 3 must define how a provider-confirmed appointment moves a local
   `Booking` to `confirmed`, where provider identifiers belong, and how external
   availability conflicts map back into local state.
2. A later channel phase must define how Arabic/English conversational time
   expressions are parsed into aware datetimes, including an explicit policy
   for ambiguous or nonexistent DST wall times. Phase 2 accepts only aware
   datetimes and therefore does not guess.
3. If a later requirement needs durable fields beyond the versioned
   `booking_data` shape, it must be proposed as a normal schema/design change;
   Phase 2 must not smuggle provider ownership into that JSON document.

These deferrals do not authorize implementation of the later phases during
Phase 2.
