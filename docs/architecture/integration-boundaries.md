# Integration boundaries

This document records the implemented Phase 2 Calendar and Phase 3 HubSpot boundaries. The project remains a modular monolith with a concrete boundary for one configured writable Google Calendar and one HubSpot account integration whose production writes remain fail-closed pending capability verification.

## Boundary rules

- PostgreSQL remains the internal durable authority.
- External integrations are not universally just transports: messaging and voice primarily provide transport or capability, while Calendar and CRM own defined authoritative state in their external systems.
- Integration code translates explicit application requests and provider responses at the boundary.
- Credentials, request signing, retries, rate limits, and provider-specific errors stay at the integration boundary.
- Business configuration remains authoritative for configured services, prices or pricing wording, service areas, opening hours, and business rules/policies.
- Application policy remains authoritative for whether an operation is permitted, validated, confirmed, and safe to execute.
- Business decisions remain in application/domain code once those later phases exist.
- Web routes and future UI actions must not embed provider-specific logic.

The Phase 2 Google client uses out-of-band authorized-user refresh-token provisioning with exactly `https://www.googleapis.com/auth/calendar.events` and `https://www.googleapis.com/auth/calendar.freebusy`. The application has no OAuth consent UI and does not persist credential files. Provider calls are bounded and translated to small application-owned snapshots/errors; raw Google dictionaries, HTTP bodies, headers, and ETags do not cross the boundary.

## Authority model

- Google Calendar is authoritative for actual live appointment availability and external booking-event existence/state; it does not decide business policy.
- HubSpot CRM is authoritative for CRM customer, lead, and opportunity lifecycle state; it does not decide appointment availability.
- PostgreSQL is authoritative for local identity mapping, conversations, pending actions, ToolExecution claims/replays, audit records, idempotency records, local Booking workflow state, and reconciliation state.
- Business configuration and application policy are authoritative for service rules, hours, areas, booking policy, and whether a new operation is permitted.
- The LLM is authoritative for none of these factual or side-effect decisions. It may perform language understanding, bounded tool selection, and response generation, but it cannot bypass application authorization or validation.
- Application-level idempotency protects side effects. The Calendar boundary uses deterministic client-generated event IDs, a private opaque Booking marker, persisted Calendar identity, and transient ETag conditional patch/delete operations to reconcile external state after ambiguous writes.

## Future responsibilities

### Calendar

Own provider authentication, availability reads, event creation/update/cancellation, timezone translation, provider-specific error mapping, and the concrete Google Calendar request translation. Google Calendar owns actual live availability and external event state. The application owns booking policy, validation, confirmation state, local workflow state, deterministic event identity, and the durable local Booking record. V1 has one Calendar/capacity pool, no technician/resource assignment, and no atomic free/busy-plus-insert transaction.

### CRM

The concrete HubSpot client owns Service Key transmission, account preflight, bounded Contact/Note requests, response parsing, and safe provider errors. The application owns Contact matching rules, portal-scoped Contact links, trusted field selection, durable mutation claims, explicit Note creation, and same-key recovery. A Contact write can only set the approved identity fields through an internal trusted boundary; updates fill blank fields and do not overwrite conflicting values. Booking Notes use validated historical ToolExecutions and do not read mutable Booking state. Google Calendar remains authoritative for appointments, and a CRM failure cannot change a committed booking. See the [HubSpot integration guide](../integrations/hubspot.md) for exact behavior and activation limitations.

### Messaging

Own transport-specific inbound/outbound message delivery, webhook verification, delivery status, media handling, and provider limits. The application owns normalized conversation turns, response policy, and user-visible state.

### Voice

Own call/media transport, transcription/synthesis handoff, interruption and session metadata, and provider-specific call lifecycle. The application owns the normalized conversation and operational outcome, not the provider call object.

### LLM

Own model client configuration, prompt/tool transport, provider response parsing, token/error metadata, and safety limits. The application owns allowed actions, validation, business rules, and durable audit records. LLM output is never authority for permissions, booking truth, or customer identity.

### Asynchronous events

Own delivery mechanics, retry/backoff, deduplication, and observability only when a concrete asynchronous workload exists. The application/database owns event meaning, state transitions, and idempotency keys. A queue or event bus is not part of Phase 0.
