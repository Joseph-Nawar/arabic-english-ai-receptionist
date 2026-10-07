# Arabic-English AI Receptionist

Phase 1 was completed after independent review and remediation for a production-style portfolio project. The current implementation is a deliberately small FastAPI modular monolith with:

- explicit Pydantic Settings and safe standard-library logging;
- SQLAlchemy 2.1 async resources with Psycopg 3;
- strict Phase 1 business/service configuration, E.164 Contact identity, conversation state helpers, and provider-independent persistence primitives;
- PostgreSQL liveness/readiness checks, the first real Alembic schema revision, guarded drift checks, and a synthetic reference seed;
- unit/integration tests, locked uv dependencies, CI, Docker, and security checks.

Only health routes and Phase 1 business/domain persistence primitives exist today. Booking is provider-independent and persistence-only; customer operations, AI behavior, messaging transport, WhatsApp, voice, booking workflows, CRM, and operator UI features remain future phases and are not implemented. ToolExecution, ProviderEventReceipt, and OutboxEvent have no dispatcher, publisher, webhook, or external-provider behavior. See the [Phase 1 domain model](docs/architecture/domain-model.md) for the current invariants and non-goals.

Start with [the local setup guide](docs/development/setup.md), read [the canonical roadmap](docs/roadmap.md), and review [the architecture](docs/architecture/system-overview.md). Phase 0 is complete following independent review and approval; Phase 1 is complete following independent review and remediation, and later customer-operations, AI, messaging, booking, CRM, and operator UI functionality remains future work.
