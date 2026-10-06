# Integration boundaries

This document is conceptual in Phase 0. It defines future responsibilities and ownership; it does not authorize matching Python interfaces, classes, packages, or provider SDKs before their phase needs a real implementation.

## Boundary rules

- PostgreSQL remains the internal durable authority.
- External integrations are not universally just transports: messaging and voice primarily provide transport or capability, while Calendar and CRM own defined authoritative state in their external systems.
- Integration code translates explicit application requests and provider responses at the boundary.
- Credentials, request signing, retries, rate limits, and provider-specific errors stay at the integration boundary.
- Business configuration remains authoritative for configured services, prices or pricing wording, service areas, opening hours, and business rules/policies.
- Application policy remains authoritative for whether an operation is permitted, validated, confirmed, and safe to execute.
- Business decisions remain in application/domain code once those later phases exist.
- Web routes and future UI actions must not embed provider-specific logic.

## Authority model

- Google Calendar or the selected booking provider is authoritative for actual live appointment availability and external calendar event state; it does not decide business policy.
- HubSpot CRM is authoritative for CRM customer, lead, and opportunity lifecycle state; it does not decide appointment availability.
- PostgreSQL is authoritative for local identity mapping, conversations, handoff state, tool and audit records, idempotency records, and local workflow state.
- The LLM is authoritative for none of these concerns. It may perform language understanding, bounded tool selection, and response generation, but it cannot bypass application authorization or validation.
- Application-level idempotency protects side effects. Provider integrations later translate that protection into provider-specific idempotency and retry semantics.

## Future responsibilities

### Calendar

Own provider authentication, availability reads, event creation/update/cancellation, timezone translation, provider-specific idempotency, and provider error mapping. The provider owns actual live availability and external event state. The application owns booking policy, validation, confirmation state, local workflow state, and the durable local booking record.

### CRM

Own contact/company lookup and synchronization with the selected CRM. HubSpot owns CRM customer, lead, and opportunity lifecycle state. The application owns local identity mapping, contact identity as used by this application, conversation linkage, customer-operation decisions, and the audit of what was requested or changed.

### Messaging

Own transport-specific inbound/outbound message delivery, webhook verification, delivery status, media handling, and provider limits. The application owns normalized conversation turns, response policy, and user-visible state.

### Voice

Own call/media transport, transcription/synthesis handoff, interruption and session metadata, and provider-specific call lifecycle. The application owns the normalized conversation and operational outcome, not the provider call object.

### LLM

Own model client configuration, prompt/tool transport, provider response parsing, token/error metadata, and safety limits. The application owns allowed actions, validation, business rules, and durable audit records. LLM output is never authority for permissions, booking truth, or customer identity.

### Asynchronous events

Own delivery mechanics, retry/backoff, deduplication, and observability only when a concrete asynchronous workload exists. The application/database owns event meaning, state transitions, and idempotency keys. A queue or event bus is not part of Phase 0.
