# Integration boundaries

This document is conceptual in Phase 0. It defines future responsibilities and ownership; it does not authorize matching Python interfaces, classes, packages, or provider SDKs before their phase needs a real implementation.

## Boundary rules

- PostgreSQL remains the internal durable authority.
- An external provider is a transport or capability, not the system of record.
- Integration code translates explicit application requests and provider responses at the boundary.
- Credentials, request signing, retries, rate limits, and provider-specific errors stay at the integration boundary.
- Business decisions remain in application/domain code once those later phases exist.
- Web routes and future UI actions must not embed provider-specific logic.

## Future responsibilities

### Calendar

Own provider authentication, availability reads, event creation/update/cancellation, timezone translation, idempotency, and provider error mapping. The application owns booking policy, confirmation state, and the durable booking record.

### CRM

Own contact/company lookup and synchronization with the selected CRM. The application owns contact identity, conversation linkage, customer-operation decisions, and the audit of what was requested or changed.

### Messaging

Own transport-specific inbound/outbound message delivery, webhook verification, delivery status, media handling, and provider limits. The application owns normalized conversation turns, response policy, and user-visible state.

### Voice

Own call/media transport, transcription/synthesis handoff, interruption and session metadata, and provider-specific call lifecycle. The application owns the normalized conversation and operational outcome, not the provider call object.

### LLM

Own model client configuration, prompt/tool transport, provider response parsing, token/error metadata, and safety limits. The application owns allowed actions, validation, business rules, and durable audit records. LLM output is never authority for permissions, booking truth, or customer identity.

### Asynchronous events

Own delivery mechanics, retry/backoff, deduplication, and observability only when a concrete asynchronous workload exists. The application/database owns event meaning, state transitions, and idempotency keys. A queue or event bus is not part of Phase 0.

