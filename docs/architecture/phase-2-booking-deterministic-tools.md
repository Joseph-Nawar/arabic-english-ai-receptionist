# Phase 2 design: Booking & Deterministic Business Tools

## Status and boundary

This document is the durable design for Phase 2. It is a design specification,
not an implementation plan. Phase 2 is `IN PROGRESS` for design only. The
implementation must remain on the single-business modular-monolith branch and
must use the Phase 1 schema and domain rules as its starting point.

Phase 2 turns the existing `Service`, `BusinessConfig`, `Conversation`,
`Booking`, `ToolExecution`, and `AuditEvent` primitives into a small set of
deterministic application operations and one concrete Google Calendar
integration. It does not add a public HTTP API, worker, or conversational/LLM
layer.

The authority order remains:

1. `BusinessConfig` plus the relational `Service` catalog author configured
   services, service areas, opening hours, and booking policy.
2. PostgreSQL authoritatively stores this application's internal state.
3. Application policy decides whether an operation is valid, confirmed, and
   safe.
4. Google Calendar authors live external availability and external event
   existence/state for the single configured calendar in this phase.
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
| `check_availability` | Apply local policy and query Google Calendar free/busy for one requested interval. | No | Not required |
| `get_booking` | Read a local Booking within its customer boundary and fetch authoritative Calendar state. | No local mutation | Not required |
| `prepare_create_booking` | Validate a create request and stage one `create_booking` pending action. | Yes, Conversation and ToolExecution/AuditEvent | Required |
| `prepare_reschedule_booking` | Validate a reschedule request and stage one `reschedule_booking` pending action. | Yes, Conversation and ToolExecution/AuditEvent | Required |
| `prepare_cancel_booking` | Validate a cancellation request and stage one `cancel_booking` pending action. | Yes, Conversation and ToolExecution/AuditEvent | Required |
| `confirm_booking_action` | Confirm the exact current booking action and run the bounded Calendar mutation plus local reconciliation. | Yes, Conversation, Booking, ToolExecution/AuditEvent | Required |

`confirm_booking_action` is deliberately the only mutation entry point after
customer confirmation. It accepts a conversation identifier, expected pending-
action type, exact opaque action token, and its own idempotency key; it does not
parse free text and does not accept a new booking payload. The caller must
already have an explicit customer confirmation signal. The operation verifies
the type and token against the action stored on the locked Conversation before
claiming or mutating Calendar state.

`check_availability` returns real Google Calendar free/busy information for the
requested interval after local policy validation. A `true` result means the
configured calendar reported no busy interval for the effective buffered
interval at that read; it is not a reservation. Create and reschedule always
repeat the free/busy check immediately before the Calendar write.

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
`confirmation_required: true`, the exact action token, and the availability
read result used for the proposal. Successful confirmation results return the
opaque local booking identifier, local Booking status, normalized UTC interval,
and the persisted Calendar reference. `get_booking` returns both a safe local
snapshot and an explicit external-state/reconciliation result; it never hides
a provider/local divergence.

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
last_operation: "create_booking" | "reschedule_booking" | "cancel_booking"
```

The Calendar identifier and event identifier are explicit Booking columns, not
part of `booking_data`. For a cancelled row, `last_operation` becomes
`cancel_booking` and the prior requested interval remains for auditability.
Cancellation context is bounded and sanitized; it is not a transcript field.
`booking_data` never stores the idempotency key, raw phone number, raw address,
Calendar payload, ETag, credential, or provider error.

## Google Calendar boundary

Phase 2 contains exactly one concrete external integration: a narrow
`CalendarClient` capability surface used directly by the booking operations and
implemented by `GoogleCalendarClient`. A small in-memory test double may
implement the same capability surface for deterministic tests. There is no
provider registry, adapter hierarchy, plugin system, or second Calendar
implementation.

The capability surface has only these operations:

- `query_free_busy(calendar_id, time_min, time_max)`;
- `get_event(calendar_id, event_id)`;
- `create_event(calendar_id, event_id, event_request)`;
- `update_event(calendar_id, event_id, event_request, if_match_etag)`; and
- `cancel_event(calendar_id, event_id, if_match_etag)`.

The Google implementation uses the official Python Google API client and
Google authentication libraries. `query_free_busy` uses Calendar's free/busy
endpoint, and event writes use the events API. The implementation maps Google
responses into small application-owned event/free-busy models and maps raw
provider failures into the application error vocabulary. Raw Google payloads
never cross the application boundary or enter durable JSON.

The single-business deployment uses one operator-owned OAuth 2.0 authorized-
user refresh token, provisioned out-of-band during deployment, plus a runtime
`GOOGLE_CALENDAR_ID`. Client ID, client secret, refresh token, and calendar ID
are environment/runtime configuration; none is committed, stored in Booking
JSON, or logged. The runtime requests only the Calendar scopes needed for free/
busy reads and event mutation. Phase 2 has no credential-consent UI; a local
operator performs the one-time consent/bootstrap step outside the application.

Every Google request has an explicit bounded timeout, with a bounded overall
operation deadline for one booking mutation. Timeout, authentication, quota,
and provider availability failures are mapped to safe application errors and
never retried indefinitely inside a request.

Google's documented capabilities support the design: free/busy queries return
busy intervals, client-supplied event IDs prevent duplicate creation on retry,
and event ETags support conditional update/delete. The implementation should
follow the official [free/busy query](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query),
[event creation](https://developers.google.com/workspace/calendar/api/guides/create-events),
and [resource-version/ETag](https://developers.google.com/calendar/api/guides/version-resources)
semantics.

### Single-calendar V1 rules

V1 has one configured writable calendar and one capacity pool. It does not
introduce technicians, resources, routing, recurrence, multiple calendars, or
capacity planning. Calendar free/busy is authoritative for that one calendar;
local Booking rows alone never answer availability.

`check_availability` receives one normalized interval, applies service duration,
business hours, notice/advance, buffers, and slot increment, then queries
Google free/busy for the interval expanded by the configured buffers. It
returns the normalized interval, policy result, busy/free result, and a
read-timestamped safe status. It does not invent alternative slots or persist
a reservation.

Immediately before Calendar create or update, the finalization step repeats
the policy evaluation and free/busy query. An earlier `check_availability`
result or preparation result is advisory only and cannot authorize the write.

The application serializes its own create/reschedule/cancel provider writes by
locking the existing singleton `BusinessConfig(id=1)` row for the bounded
provider/finalization transaction. This is the narrow single-business write
fence; it is not a general scheduler or distributed lock. The provider call is
made while that short, bounded transaction holds the fence so two application
workers cannot concurrently create/update/delete the same single-calendar
capacity state. Google does not provide a transactional “reserve only if still
free” operation, so an out-of-band human Calendar edit can still race the
read/write sequence. The immediate recheck plus application write fence is the
strongest guarantee available in this V1 design; provider conflict responses
remain authoritative and are surfaced safely.

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

The evaluator itself remains pure and has no provider side effect or slot
enumeration. `check_availability` composes it with the narrow Google Calendar
free/busy read. A policy-valid interval is only eligible for a provider read;
it is not claimed to be available until the configured Calendar reports it
free. Busy interval overlap uses half-open interval semantics: a busy event
whose end equals the requested effective start does not conflict, while any
positive overlap does.

## Booking request and local Booking semantics

### What a Phase 2 Booking means

A Phase 2 `Booking` row is an application-owned record reconciled with one
Google Calendar event. It records the contact, service, normalized UTC
interval, service area, bounded request details, and explicit Calendar
references. Calendar owns whether the external event exists and its current
external state; PostgreSQL owns the local identity, workflow, confirmation,
idempotency, and audit record.

Phase 2 adds these nullable Booking columns:

| Column | Meaning | Invariant |
| --- | --- | --- |
| `calendar_id` | Calendar containing the event, copied from the runtime configuration at claim time. | Null only when `calendar_event_id` is null. |
| `calendar_event_id` | Client-generated Google event ID for this local Booking. | Null only when `calendar_id` is null; unique with `calendar_id`. |

The migration adds a pair check constraint and a unique `(calendar_id,
calendar_event_id)` index. It also adds a check that a `confirmed` Booking has
both Calendar references; Phase 1-compatible `pending`/`cancelled` rows may
remain reference-free. ETags are not persisted: the Calendar boundary fetches
the current event immediately before a conditional update or delete.
The configured calendar ID remains runtime configuration, while the persisted
copy lets retrieval and reconciliation detect a configuration change instead
of looking in the wrong calendar.

The Phase 1 `Booking.status` vocabulary is retained:

- `pending` means a locally accepted/in-progress booking intent whose Calendar
  operation has not yet been durably finalized. A committed `started`
  ToolExecution and the pending action identify how it is resumed;
- `confirmed` means the Google Calendar event exists, has been reconciled to
  the local Booking, and the local interval matches the event returned by
  Calendar; and
- `cancelled` means cancellation has completed consistently with Calendar,
  including the safe case where the event was already absent.

Phase 2 may reach `confirmed`. It never marks a row confirmed before the
Calendar create/update response has been reconciled. It also does not add a
fourth status to represent provider uncertainty.

For timestamps, a pending create has no `confirmed_at`; a pending
reschedule/cancellation temporarily clears the active confirmation marker and
keeps the prior value in the expected-state/ToolExecution snapshot for safe
restoration. Successful provider reconciliation sets `confirmed_at`; a
definitive reschedule/cancellation rejection restores the prior confirmed
state. A successful cancellation sets `cancelled_at` and may retain the prior
`confirmed_at` as historical evidence.

The existing nullable start/end pair remains legal for Phase 1 primitives, but
all Phase 2-managed booking creates and reschedules require a complete
interval and persist both UTC values. PostgreSQL's pair/order constraints
remain the final database guard.

### Phase 1 primitive compatibility

Phase 1 may have created provider-independent Booking rows with no Calendar
references or the Phase 2 `booking_data` shape. `get_booking`, reschedule, and
cancel never manufacture an event reference for such a row. They return the
safe `external_reference_missing`/`booking_state_conflict` result, depending
on the operation, unless the row already proves all required Phase 2 fields
and references are present. Such a row is not treated as a Calendar event and
is not silently adopted.

### Create

`prepare_create_booking` resolves the active, bookable service and active area,
validates required service details, derives the end when necessary, evaluates
the business policy, queries real Calendar availability, and stages a
`create_booking` action only when the current interval is policy-valid and
free. It does not insert a Booking or Calendar event. The returned free result
is advisory and includes its read timestamp; confirmation must repeat it.

After exact confirmation, the claim transaction creates one `pending` Booking,
copies the configured Calendar ID, derives a valid deterministic Calendar
event ID from the Booking ID, records the action token and desired request in
the ToolExecution, marks the pending action `confirmed`, and commits. The
provider/finalization step then locks the single-business write fence, rechecks
policy and free/busy, inserts the event with that event ID and a private
application marker containing the opaque Booking ID, and reconciles the
response before setting the Booking `confirmed`.

The operation rejects a create when the service, area, required details,
interval, policy, or Conversation state is invalid. An exact existing active
local booking for the same contact, service, area, and interval is treated as a
safe `duplicate_replay` rejection when presented with a different idempotency
key. Calendar free/busy remains the authority for actual capacity. If the
final availability recheck is busy, no event is created and the pending local
intent is finalized as cancelled with a rejected ToolExecution. If the
provider result is ambiguous, the row remains pending and the same key resumes
reconciliation; no second event is inserted.

### Reschedule

Reschedule targets one existing local Booking. Preparation locks and validates
the target, requires a Phase 2-managed confirmed Booking with both Calendar
references, preserves its service and area, validates the new interval against
current policy and Calendar free/busy, and stages a `reschedule_booking`
action containing the target Booking identifier, canonical new values, and an
expected-state fingerprint. Confirmation locks the Booking again and compares
that fingerprint before establishing the pending provider operation.

The claim transaction changes the Booking to `pending` while retaining the
last reconciled interval in its normal columns; the desired new interval and
the expected prior state remain in the ToolExecution arguments. The provider
step locks the single-business fence, rechecks free/busy, fetches the current
event, and updates it with the current ETag. Only the reconciled Calendar
response changes the Booking interval, request metadata, and status back to
`confirmed`.

A cancelled Booking is rejected with `booking_state_conflict`. A pending
Booking with a different active operation is `operation_in_progress`. A
missing or mismatched Calendar reference is `external_reference_missing`. A
missing target is `booking_not_found`. An ETag conflict or divergent event is
`external_state_conflict`; the old local confirmed state is preserved and the
customer must receive a fresh decision.

### Cancellation

Cancellation preparation locks the target local Booking and stages a
`cancel_booking` action only for a Phase 2-managed `confirmed` Booking. It
captures the same minimal expected-state fingerprint. Confirmation verifies
the fingerprint, changes the Booking to `pending`, and records the desired
cancellation in the ToolExecution before committing the claim. The provider
step fetches the event and deletes it with its current ETag. A 404/already
absent event is reconciled as a successful cancellation; only then is the
Booking set to `cancelled` and `cancelled_at` recorded.

Cancellation of an already-cancelled Booking is a deterministic
`booking_state_conflict`. Cancellation of a `confirmed` Booking is rejected as
`booking_state_conflict` only when the expected state or Calendar reference is
invalid; otherwise confirmed cancellation is a supported Phase 2 operation. A
pending Booking with another active operation returns `operation_in_progress`.
An ETag conflict or unexpected existing event state restores the local
confirmed state and returns `external_state_conflict`.

### Expected Booking snapshot

Reschedule and cancellation preparation capture a canonical expected-state
document while holding the target Booking lock. It contains exactly the state
those operations rely on: Booking ID, service ID, status, UTC `start_at`, UTC
`end_at`, `calendar_id`, `calendar_event_id`, and a digest of the relevant
versioned request data. The fields are serialized in a stable key order and
hashed into an opaque `expected_state_fingerprint` stored in the pending
payload and sanitized ToolExecution arguments. This is a focused optimistic
precondition, not a generic versioning framework.

At claim confirmation the operation locks the Booking and recomputes the
fingerprint. A mismatch returns `stale_pending_action` or
`booking_state_conflict` without provider mutation and without applying the old
customer confirmation to the newer Booking. This is what prevents two
Conversations from preparing reschedules and successively overwriting one
another.

### Retrieval

`get_booking` requires a conversation ID and booking ID. The local Booking is
returned only when its Contact matches the Conversation Contact; a mismatch is
reported as `booking_not_found` without disclosing another customer's state.
For a Phase 2-managed row, the operation fetches the persisted Calendar event
reference and returns a safe external snapshot alongside an explicit
`in_sync`, `provider_divergent`, `provider_missing`, or `operation_in_progress`
reconciliation status. It does not silently overwrite local state when the
provider differs. A pending row with a durable started execution directs the
caller to retry that same operation key for reconciliation; retrieval itself
does not finalize a mutation.

### Pending confirmation behavior

The Phase 1 Conversation row is the sole current pending-action slot. Every
prepare operation requires that it be empty. If another critical action exists,
the operation returns `pending_action_conflict` and does not replace it. An
exact replay with the same idempotency key returns the stored result instead of
staging a new action.

Preparation generates an opaque action token (UUID-based, non-PII), stores it
in the canonical sanitized pending-action payload, and returns it in the
successful preparation result. Phase 2 does not add a pending-action history
table or a second confirmation mechanism. The existing helpers are used as
follows:

- `stage_pending_action` stages `awaiting_confirmation`;
- `confirm_booking_action` requires the Conversation ID, expected action type,
  exact action token, and its own idempotency key;
- the current pending action must match both type and token before
  `confirm_pending_action` is called in the claim transaction;
- the provider mutation occurs only after that committed exact confirmation;
- `clear_pending_action` occurs only after successful or definitively rejected
  finalization, never before the external operation is recoverable; and
- an optional non-null `expires_at` is honored, but Phase 2 does not invent a
  new confirmation TTL because Phase 1 has no such configuration field.

If the caller's type or token mismatches a different current valid pending
action, confirmation returns `stale_pending_action` and records a safe rejected
ToolExecution without clearing the current valid action. This prevents a
delayed confirmation for an earlier same-type action from authorizing a later
action.

If the current action itself is expired, its target Booking snapshot is stale,
its configuration is no longer valid, or its external reference is invalid,
the claim transaction performs no provider mutation, clears that now-invalid
action, and records an audited rejection. Unexpected failures roll back the
claim and leave the action recoverable.

## Idempotency and replay

All three preparation operations and `confirm_booking_action` require a
non-empty opaque idempotency key. Lookup and availability-read operations do
not mutate state and do not require one. A caller that wants to retry a
Calendar mutation must retry the same logical confirmation key.

The preparation key identifies the explicit staging operation. The confirmation
key identifies the separate external-side-effect operation linked by the
action token. Once confirmation begins, that one confirmation key is carried
through claim, Google mutation, reconciliation, and every recovery retry; no
new key is generated for a provider retry.

The existing partial unique index makes a non-null `ToolExecution.idempotency_key`
globally unique across the application. That database fact defines the key
scope: callers must generate one unique value per logical operation, not merely
one value per conversation. The operation also records the tool name,
conversation ID, and a sanitized argument fingerprint so accidental reuse can
be rejected as `idempotency_conflict` rather than silently changing meaning.

The PostgreSQL claim is conflict-safe and never catches a uniqueness error in a
poisoned outer transaction. It uses an `INSERT ... ON CONFLICT DO NOTHING
RETURNING`-style operation for a new `ToolExecution`; when no row is returned,
it selects the existing key `FOR UPDATE`. A concurrent insert waits on the
unique index, then the loser reads the committed winner. A savepoint is an
acceptable equivalent if the SQLAlchemy implementation needs one, but the
outer transaction remains usable.

External Calendar work uses two explicit stages rather than pretending the
database and provider share a transaction.

### Claim transaction

1. Begin a short PostgreSQL transaction and claim or lock the
   `ToolExecution`.
2. For an existing `succeeded`, `rejected`, or terminal `failed` row, verify
   tool, conversation, and argument fingerprint, then replay its stored safe
   result. A mismatch returns `idempotency_conflict`.
3. For a new confirmation, lock the Conversation and the target Booking when
   applicable, then verify the exact pending action type and action token plus
   any expected Booking snapshot.
4. For create, insert the local Booking as `pending`, copy the configured
   Calendar ID, and establish its deterministic client-generated event ID. For
   reschedule/cancel, change the managed Booking to `pending` while retaining
   the last reconciled values and store the desired provider mutation in the
   sanitized ToolExecution arguments.
5. Mark the pending action `confirmed`, add the opaque execution identifier to
   its payload, and leave ToolExecution `started`. Commit this durable claim.

No Google call occurs before this claim commits. A committed `started`
execution therefore means that a provider operation may have happened or may
still need to happen; it is a recoverable state, not a work queue.

### Provider/finalization step

1. Begin a bounded transaction and lock `BusinessConfig(id=1)` as the
   single-calendar application write fence. Then lock Conversation and Booking
   in that order and revalidate the execution/action state.
2. Re-run current local policy and query Google free/busy immediately before
   create or reschedule. An earlier availability result never authorizes a
   write.
3. Perform the bounded Google create/update/delete operation using the known
   Calendar and event identity. For update/delete, fetch the current event and
   use its current ETag for conditional mutation.
4. Reconcile the response. If the response is ambiguous, fetch the known
   event identity and verify the private Booking marker and expected interval.
   Leave the execution `started` and the Booking pending when more retry is
   required; do not clear the action.
5. On a reconciled success, update Booking status/references/interval,
   append the sanitized AuditEvent, mark ToolExecution `succeeded`, clear the
   pending action, and commit.
6. On a definitive safe rejection, restore the prior confirmed state or mark a
   never-created local intent cancelled as appropriate, mark ToolExecution
   `rejected`/terminal `failed`, append the audit record, clear the invalid
   action, and commit.

The same idempotency key resumes a durable `started` execution. Create retry
uses the same client-generated event ID and retrieves/reconciles it before any
new insert. Reschedule retry fetches the known event and desired state before
retrying conditional update. Cancellation retry treats an already-absent event
as reconciled cancellation. A different key cannot bypass a confirmed pending
action or a Booking with another active execution; it returns
`operation_in_progress`.

An unexpected crash may leave Google changed while the finalization transaction
rolls back, but the committed claim preserves enough identity to recover. No
background worker is required: the same logical operation is synchronously
resumed when invoked again. No generic saga/workflow engine is introduced.

The same idempotency key always returns the same prior terminal result after
finalization. Exact local duplicate create detection adds a second safety fence
for a different key, but it is not a distributed idempotency service.

### Google-specific identity and mutation rules

Create derives a valid Google event ID deterministically from the local Booking
UUID using a stable prefix and lowercase base32hex encoding without padding.
The ID is persisted before the provider call. The event includes a small
private application marker containing the opaque local Booking ID, but no PII.
If an insert response is ambiguous or reports that the ID already exists, the
boundary retrieves that exact event, verifies the marker and expected request,
and treats a matching event as the original success. A mismatched event is
`external_state_conflict` and is never adopted.

For reschedule, the boundary gets the current event, performs a full update
with `If-Match`/current ETag semantics, and persists the reconciled returned
interval. A 412/precondition failure is `external_state_conflict`; the local
Booking is not overwritten. For cancellation, the boundary gets the event and
deletes it conditionally. A 404 means the event is already absent and is a
successful reconciliation; an ETag conflict is an external state conflict.

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
- `booking_state_conflict` — target Booking is a Phase 1 primitive, already
  cancelled, or otherwise cannot accept this local transition;
- `external_reference_missing` — a requested Calendar operation requires a
  Phase 2 Calendar/event reference that the row does not have;
- `operation_in_progress` — another committed ToolExecution is currently
  recovering or finalizing the Booking;
- `idempotency_conflict` — a key was reused for a different operation or
  arguments;
- `duplicate_replay` — an exact local create already exists under another key;
- `calendar_interval_unavailable` — the configured Calendar reports a busy
  overlap for the effective requested interval;
- `calendar_unavailable` — the Calendar read/write could not complete within
  the bounded request policy;
- `external_event_missing` — a referenced event cannot be retrieved when the
  operation requires it;
- `external_state_conflict` — Calendar state, ETag, event marker, or returned
  interval conflicts with the expected local operation;
- `calendar_reconciliation_required` — the provider response is ambiguous and
  the same idempotency key must resume recovery.

No raw Google error codes or payloads cross the operation boundary. Database
uniqueness/row-lock outcomes and Google failures are mapped into this small
vocabulary; raw constraint names, HTTP details, and credentials do not cross
the operation boundary.

## Transactions and concurrency

The public application operations own their narrow claim/finalization
transactions. Pure lookup, normalization, policy, and Calendar translation
functions do not commit and do not own a broad transaction. No generic
unit-of-work or repository layer is added.

### Create preparation and confirmation

Preparation locks the Conversation row after claiming the idempotency key. It
checks the empty pending-action slot, resolves current configuration, then
queries Calendar availability before staging the action and terminal
ToolExecution/AuditEvent in one transaction. Confirmation first runs the claim
transaction. The provider/finalization transaction locks the singleton
BusinessConfig fence, then Conversation and Booking, rechecks availability,
and performs the Calendar insert before final local reconciliation. The
Conversation lock serializes competing critical actions for one conversation;
the Contact lock serializes exact-duplicate checks for the same contact across
conversations. The existing PostgreSQL constraints remain authoritative.

### Reschedule and cancellation

Preparation and claim confirmation lock Conversation and then the target
Booking in a consistent order and compare the canonical expected-state
fingerprint. The provider/finalization transaction locks BusinessConfig first,
then Conversation and Booking, rechecks the fingerprint and provider state,
and uses ETag conditional mutation. Concurrent operations therefore either
replay their own idempotency result, resume the same started execution, or
observe the first committed transition and return a deterministic stale/state
error. Two conversations cannot successively overwrite a Booking based on
stale preparations.

### Failure and rollback

Expected validation, policy, pending-action, idempotency, and state rejections
are stored as `ToolExecution.status = rejected` with safe error fields and an
AuditEvent, then committed without the requested Booking mutation or provider
call. A provider busy response or conditional conflict is finalized as a safe
terminal rejection with the local state restored/cancelled as defined above.
An ambiguous provider response leaves ToolExecution `started`, the Booking
pending, and the action confirmed for same-key recovery. An unexpected failure
before claim commit rolls back local claim state; a failure after Calendar may
have responded but before finalization is recovered using the persisted event
identity and marker.

There is no Redis lock, advisory lock, application mutex, distributed lock, or
background worker. The singleton-row fence is the only V1 application booking
write fence, and the bounded Calendar call is the only external side effect.

## Persistence and audit

Phase 2 uses the existing Phase 1 tables and constraints plus one focused
Alembic migration for explicit Calendar references on `Booking`. It does not
add a new booking table or pending-action table. The existing
`Booking.booking_data` JSON remains a small versioned, application-owned
request document; provider ownership is represented only by the explicit
`calendar_id` and `calendar_event_id` columns.

Each state-changing operation persists:

- a `ToolExecution` with sanitized arguments and sanitized terminal result;
- the Conversation pending-action change, if applicable;
- the Booking local mutation and explicit Calendar reference, if applicable;
- one sanitized `AuditEvent` with opaque IDs, operation name, outcome, and
  stable error code where relevant.

No raw phone number, email, address, conversation text, secret, raw provider
payload, ETag, OAuth token, or unbounded user-supplied detail is written to
ordinary logs, ToolExecution JSON, Booking metadata, or AuditEvent metadata.
Logs prefer `conversation_id`, `booking_id`, and `tool_execution_id`. Phase 2
does not publish `OutboxEvent`, process it, or add a publisher; Calendar
side-effect recovery is synchronous through the durable ToolExecution claim.

## Integration boundary and later seams

The Phase 2 seam is the persisted local Booking request plus the explicit
Calendar reference and deterministic operation result. The concrete Google
Calendar boundary owns credentials, provider calls, time translation at the
API edge, client event IDs, ETags, provider retries within one bounded request,
and provider error mapping. Application code owns policy, confirmation,
locking, local state, and reconciliation decisions.

Phase 2 creates no provider registry, adapter hierarchy, SDK-shaped model
graph, CRM interface, messaging interface, or generic integration framework.
`ProviderEventReceipt` and `OutboxEvent` remain unused persistence primitives;
there is no webhook or asynchronous provider processor.

Phase 3 remains HubSpot CRM Integration. Any later CRM or channel phase must
consume explicit application state at its own approved boundary rather than
making Calendar or the LLM authoritative for local booking policy.

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
  reschedule, cancellation, and retrieval, including durable reload in a new
  session and Calendar-reference pair/uniqueness constraints;
- Calendar-boundary tests using a narrow deterministic test double for free/busy,
  get, create, update, and cancel, with explicit timeout/error mapping;
- tests that prove `check_availability` uses service duration, business hours,
  buffers, slot increment, notice/advance policy, and provider busy intervals;
- integration tests proving only explicit confirmation mutates Booking and
  that a second pending critical action is rejected;
- exact action-token tests, including delayed confirmation for an earlier
  same-type action and mismatched-token preservation of the current valid
  action;
- stale Booking fingerprint tests across two Conversations proving a prepared
  reschedule/cancel cannot overwrite a newer state;
- replay tests for successful, failed, rejected, and mismatched idempotency
  keys, including conflict-safe same-key concurrent execution/replay;
- crash/retry reconciliation tests for provider success followed by local
  finalization failure, including create without duplicate event creation;
- availability recheck tests immediately before create/reschedule, provider
  busy/conflict/ETag failure tests, and cancellation of an already-absent
  event;
- rollback tests proving a failed local mutation leaves Conversation, Booking,
  ToolExecution, and AuditEvent consistent;
- service active/bookable and service-area catalog tests, including exact
  ambiguity rejection;
- `get_booking` tests for customer-boundary enforcement and local/provider
  missing or divergent state without silent overwrite;
- timezone/business-hours tests for minimum notice, maximum advance, window
  edges, slot increments, buffers, DST ambiguity/nonexistence, and the
  configured `Asia/Riyadh` reference values;
- tests that assert no provider mutation occurs during preparation, no slots
  are fabricated, and a provider-reported `free` result is based on the real
  free/busy response supplied by the Calendar boundary;
- repeatability tests that run the integration suite twice without relying on
  a manual schema reset between runs.

Normal CI must not call live Google Calendar, CRM, messaging, voice, an LLM, or
any external SaaS provider. The narrow Calendar double is used for automated
tests. A separate manual smoke path against a dedicated non-production Google
Calendar must prove real free/busy retrieval, exactly-once event creation,
retrieval, reschedule, cancellation, and safe same-key replay without a
duplicate event. Production/customer Calendars are prohibited for smoke or
integration testing.

## Explicit non-goals

Phase 2 does not implement:

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
  LangGraph;
- Calendar providers other than the one concrete Google Calendar boundary and
  one configured single-business calendar; or
- any Phase 3–12 behavior.

## Design questions intentionally deferred

The following are explicit later-phase decisions rather than hidden gaps in
Phase 2:

1. A later channel phase must define how Arabic/English conversational time
   expressions are parsed into aware datetimes, including an explicit policy
   for ambiguous or nonexistent DST wall times. Phase 2 accepts only aware
   datetimes and therefore does not guess.
2. If a later requirement needs durable fields beyond the versioned
   `booking_data` shape, it must be proposed as a normal schema/design change;
   Phase 2 must not smuggle provider ownership into that JSON document.
3. Calendar OAuth refresh-token provisioning and dedicated non-production
   calendar setup remain deployment/operator tasks, not application auth/UI
   work in Phase 2.

None of these questions blocks implementation planning. They do not authorize
implementation of later phases during Phase 2.
