# Phase 1 domain model

Phase 1 establishes the first real domain and persistence model for the
single-business receptionist. It is a modular-monolith domain model backed by
PostgreSQL. The model is intentionally small: it records configuration,
identity, conversation state, durable operational state, and future seams; it
does not execute provider workflows.

## Business configuration

`BusinessConfig` is a singleton whose only legal primary-key value is `1`.
There is no tenant, organization, or business-membership model. The reference
configuration is validated as a strict Pydantic document before persistence.

Business configuration owns the business name, IANA timezone, default phone
region, currency, supported languages (`en` and `ar`), default language,
weekly local wall-clock hours, after-hours mode, named service areas, booking
policy values, handoff policy, greeting/closing templates, and retention
settings. Timezone values are validated with `zoneinfo`; phone regions use
libphonenumber; currency is an uppercase three-letter code. Weekly windows
must be non-overlapping, non-duplicated, and same-day with `start < end`.

Services are relational records with stable unique codes, bilingual names and
descriptions, aliases, active/bookable flags, a positive default duration,
and strictly validated JSONB for pricing, booking requirements, and the small
service-escalation configuration. Money is integer minor units. Service and
area catalog names/aliases reject ambiguity after Unicode normalization,
whitespace normalization, and case-folding.

## Identity

`Contact.phone_e164` is the canonical cross-channel identity and has a unique
non-null database constraint. `normalize_phone_number` accepts international
or explicitly regional national input, rejects invalid/impossible numbers and
extensions, and returns E.164. Raw phone strings and display names are never
identity keys.

`resolve_or_create_contact` owns only the focused PostgreSQL conflict-safe
insert/retrieve operation. Database uniqueness is the concurrency authority;
the helper does not hide broad transaction commits. A missing or withheld
number always creates a new unresolved Contact, including when other
unresolved Contacts already exist.

## Conversations and state

`Conversation` belongs to a Contact and records one of the channels `phone` or
`whatsapp`, lifecycle `open` or `closed`, control mode `ai`,
`handoff_pending`, or `human`, language mode `unknown`, `en`, `ar`, or `mixed`,
an optional outcome, a bounded summary, and one current pending critical
action. Open conversations have no close timestamp; closed conversations have
one.

The control state transitions are deliberately limited:

```text
AI -> HANDOFF_PENDING -> HUMAN -> AI
                    \-> AI  (handoff cancelled)
```

Invalid transitions raise one domain-state error. Pending actions are stored
directly on Conversation. Only booking-critical action types are allowed:
`create_booking`, `reschedule_booking`, and `cancel_booking`; status is either
`awaiting_confirmation` or `confirmed`. Staging requires no existing action,
confirmation requires an awaiting action and a confirmation timestamp, and
clearing removes all action metadata. There is no pending-action history or
workflow engine in Phase 1.

Conversation turns are append-oriented records with roles `customer`,
`assistant`, `human`, or `system`, and unique sequence numbers per
conversation.

## Durable operational records

- `Booking` records provider-independent contact/service booking state with
  optional UTC start/end pairs and statuses `pending`, `confirmed`, and
  `cancelled`. It has no calendar/provider identifiers and no booking
  workflow.
- `Handoff` records a controlled operational request with status, reason, and
  priority. At most one `pending` or `accepted` handoff may exist for a
  conversation; resolved and cancelled history may coexist.
- `ToolExecution` records the future side-effect boundary, including
  sanitized arguments/results and an optional unique idempotency key. It does
  not dispatch tools.
- `AuditEvent` is the application append-only-by-convention audit trail with
  sanitized metadata and controlled actor types. Secrets, credentials, raw
  provider payloads, and unnecessary PII are excluded.
- `ProviderEventReceipt` stores provider/event identifiers and optional safe
  hashes for future deduplication. It does not receive webhooks.
- `OutboxEvent` stores a future asynchronous intent, publication marker,
  attempt count, and safe error code. It has no publisher, worker, queue, or
  retry scheduler in Phase 1.

Historical relationships use restrictive foreign keys. Phase 1 has no
customer-data deletion or anonymization workflow.

## Authority boundaries

PostgreSQL is authoritative for local identity mapping, conversations,
handoff state, tool/audit records, receipts, outbox records, and local
workflow state. Business configuration is authoritative for configured
services, areas, opening hours, and policies. Calendar and CRM remain future
external authorities for their explicitly assigned state; no integration code
is introduced here. Application/domain validation owns permission and state
invariants. No LLM or provider is authoritative for these records.

## Reference seed and privacy

`python -m receptionist.seed` / `make seed` validates and idempotently persists
the clearly synthetic Riyadh HomeCare Demo configuration and four service
records. It permits only local/test/ci environments and refuses production.

Ordinary logs should use opaque identifiers such as `contact_id` and
`conversation_id`, not full phone numbers, email, addresses, conversation
text, booking payloads, raw provider payloads, or secrets.

## Explicit Phase 1 non-goals

Phase 1 does not implement Calendar, HubSpot, messaging, WhatsApp transport,
voice, Vapi, Twilio, LLM behavior, authentication, UI, webhooks, n8n, booking
or availability workflows, slot calculation, conflict checking, provider
handling, tool orchestration, outbox processing, retries, analytics, fuzzy
matching, or deletion jobs. It also does not add repositories, unit-of-work
frameworks, provider adapters, generic workflow/state-machine infrastructure,
or speculative interfaces.
