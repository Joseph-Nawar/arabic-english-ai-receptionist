# Arabic-English AI Receptionist

Phase 0 engineering foundation for a production-style portfolio project. The current implementation is a deliberately small FastAPI modular monolith with:

- explicit Pydantic Settings and safe standard-library logging;
- SQLAlchemy 2.1 async resources with Psycopg 3;
- PostgreSQL liveness/readiness checks and empty-metadata Alembic wiring;
- unit/integration tests, locked uv dependencies, CI, Docker, and security checks.

Only `/health/live` and `/health/ready` exist today. Customer operations, AI behavior, messaging, WhatsApp, voice, booking, CRM, and operator UI features are future phases and are not implemented.

Start with [the local setup guide](docs/development/setup.md), read [the canonical roadmap](docs/roadmap.md), and review [the architecture](docs/architecture/system-overview.md). Phase 0 is intentionally `REVIEW`, not `COMPLETE`.
