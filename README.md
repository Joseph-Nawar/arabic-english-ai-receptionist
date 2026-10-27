# Arabic-English AI Receptionist

Phase 2 is complete following independent technical review, remediation, and repository-owner approval. Phase 3 implementation is prepared for independent review; it is not marked complete.

Phase 1 was completed after independent review and remediation for a production-style portfolio project. The current implementation is a deliberately small FastAPI modular monolith with:

- explicit Pydantic Settings and safe standard-library logging;
- SQLAlchemy 2.1 async resources with Psycopg 3;
- strict Phase 1 business/service configuration, E.164 Contact identity, conversation state helpers, and provider-independent persistence primitives;
- PostgreSQL liveness/readiness checks, the first real Alembic schema revision, guarded drift checks, and a synthetic reference seed;
- deterministic booking policy and finite booking operations;
- one narrow Google Calendar boundary with exact least-privilege scopes, deterministic event IDs, private opaque Booking markers, ETag-conditional writes, and crash/ambiguous-write recovery;
- one concrete HubSpot boundary for Contact resolution and controlled Contact writes, plus explicitly invoked historical booking Notes with durable claims and same-key reconciliation;
- PostgreSQL-backed action confirmation, idempotency, reconciliation, concurrency tests, locked uv dependencies, CI, Docker, and security checks.

Google Calendar is authoritative for appointment availability and external event state. HubSpot owns CRM records and Notes; it does not own appointments. PostgreSQL is authoritative for local identity, workflow, pending actions, ToolExecution idempotency, audit records, and reconciliation state; application configuration and policy decide whether a new operation is permitted. The LLM is authoritative for none of these factual or side-effect decisions.

Start with [the local setup guide](docs/development/setup.md), read [the canonical roadmap](docs/roadmap.md), and review [the architecture](docs/architecture/system-overview.md). Phase 2 uses one configured writable Calendar and out-of-band OAuth authorized-user refresh-token provisioning; it has no application consent UI and no credentials in the repository. The HubSpot integration boundaries, write restrictions, recovery rules, and guarded manual smoke instructions are documented in [the HubSpot integration guide](docs/integrations/hubspot.md).

Neither the dedicated live Google Calendar smoke nor HubSpot live smoke has been executed. Phase 3 is awaiting independent review. Messaging, voice, LLM orchestration, n8n, workers, outbox processing, dashboard/UI, Redis, multitenancy, and other later-phase behavior remain not started. This implementation does not imply production readiness.
