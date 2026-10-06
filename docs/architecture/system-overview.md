# System overview

## Product shape

The project is a configurable template for one business at a time: a portfolio demonstration of how an Arabic-English receptionist and customer-operations agent could be built for a single business configuration. It is intentionally not a multi-tenant SaaS platform and does not yet contain business or AI features.

## Core architecture

The system is a modular monolith. FastAPI is the process boundary, PostgreSQL is the durable system of record, and the Python modules are organized by responsibility rather than by speculative future packages. Phase 0 contains only application configuration, standard-library logging, database resources/metadata, Alembic wiring, and health routes.

Authority is explicit and follows this hierarchy:

- business configuration is authoritative for configured services, prices or pricing wording, service areas, opening hours, and business rules/policies;
- Google Calendar or the selected booking provider is authoritative for actual live appointment availability and external calendar event state;
- HubSpot CRM is authoritative for CRM customer, lead, and opportunity lifecycle state;
- PostgreSQL is authoritative for this application's internal operational state, including local identity mapping, conversations, handoff state, tool and audit records, idempotency records, and local workflow state;
- application policy is authoritative for whether an operation is permitted, validated, confirmed, and safe to execute;
- the LLM is authoritative for none of the above; it performs language understanding, bounded tool selection, and response generation.

Application code owns request coordination, configuration validation, and lifecycle side effects. Asynchronous work, when later required, must be introduced only with a concrete use case and a documented owner.

## PostgreSQL responsibility

PostgreSQL will own durable business, conversation, booking, handoff, and audit data in later phases. Phase 0 creates no domain tables. SQLAlchemy declarative metadata exists only so Alembic and later schema work have a stable metadata root. The development and test databases are separate infrastructure.

## Realtime and asynchronous architecture

Realtime request/response work should remain in the web process when a user is waiting for an answer or an operator action. Long-running, retryable, or externally-triggered work may become asynchronous later, but only after the concrete workload, retry semantics, idempotency, and state owner are known. Phase 0 deliberately adds no queue, broker, worker, outbox, or event bus.

## Future channels and integrations

Future channels may include web/operator interactions, messaging/WhatsApp, voice/voice notes, and provider webhooks. These channel and media integrations primarily transport messages, audio, and provider lifecycle events. Calendar and CRM integrations are different: they own specific authoritative state in their external systems, while the application remains authoritative for policy and its local operational record. Their responsibilities and ownership rules are documented in `integration-boundaries.md`. No provider SDK or integration interface is present in Phase 0.

## Future operator UI

The default Phase 10 decision is server-rendered FastAPI/Jinja2 pages with minimal progressive JavaScript when necessary. A separate React/Next.js SPA is not the default. Jinja2 is not installed in Phase 0 because there are no pages yet.

## Non-goals for Phase 0

- no domain models, business APIs, authentication, webhooks, dashboard, or UI;
- no Calendar, CRM, messaging, WhatsApp, Twilio, voice, Vapi, LLM, retrieval, or n8n implementation;
- no external provider SDKs or live SaaS calls;
- no queues, background workers, event bus, Redis, Kubernetes, Terraform, or observability platform;
- no speculative adapters, Protocols, repositories, unit-of-work layer, registries, or provider packages.
