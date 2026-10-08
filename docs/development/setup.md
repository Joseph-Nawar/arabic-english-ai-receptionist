# Local development setup

The reproducible local loop uses host Python/uv for FastAPI and Docker Compose only for PostgreSQL.

## Fresh clone

1. Install Python `3.12.15`, uv `0.12.22` or another version in the project-supported range `>=0.12.22,<0.13`, Docker, and Docker Compose.
2. Clone the repository. The normal development branch is `main`:

   ```sh
   git clone https://github.com/Joseph-Nawar/arabic-english-ai-receptionist.git
   cd arabic-english-ai-receptionist
   git switch main
   ```

3. Confirm the tool versions:

   ```sh
   python --version
   uv --version
   docker compose version
   ```

4. Install the locked development environment:

   ```sh
   uv sync --frozen --all-groups
   ```

5. Create the local environment file. It contains only local placeholders and is ignored by Git:

   ```sh
   cp .env.example .env
   git status --short --ignored .env
   ```

6. Start the development database, apply the current Phase 2 Alembic schema, seed the synthetic reference catalog, and run the web process on the host:

   ```sh
   make db-up
   make migrate
   make seed
   make dev
   ```

7. In another terminal, verify the process:

   ```sh
   curl -i http://127.0.0.1:8000/health/live
   curl -i http://127.0.0.1:8000/health/ready
   ```

The application does not run migrations automatically at startup. Stop infrastructure with `make db-down` when finished. The isolated test database uses `make test-db-up`, port `55432`, and database name `receptionist_test`; use `make test-db-down` to stop it. The reference seed is synthetic/demo-only and refuses the `production` environment.

Runtime Google Calendar settings are optional for ordinary startup and provider-free tests. When enabled, configure one writable Calendar plus an OAuth authorized-user refresh token provisioned out of band with exactly these scopes:

- `https://www.googleapis.com/auth/calendar.events`
- `https://www.googleapis.com/auth/calendar.freebusy`

There is no application OAuth consent UI, browser flow, credential bootstrap, or credential file persistence. Keep all credential values out of `.env.example`, Git, logs, and exceptions.

## Optional dedicated Calendar smoke

Phase 2 includes a manual-only Calendar smoke path for a dedicated non-production Calendar. It is never part of application startup, normal tests, `make check`, `make verify`, or CI. Provision an OAuth authorized-user refresh token out of band with exactly these least-privilege scopes:

- `https://www.googleapis.com/auth/calendar.events`
- `https://www.googleapis.com/auth/calendar.freebusy`

The application does not provide an OAuth consent UI or write credentials to disk. Create a dedicated Google secondary Calendar with an obvious human-visible name such as `Receptionist Phase 2 Smoke`, then copy its provider-assigned Calendar ID from Google Calendar settings. A normal secondary Calendar ID ends with `@group.calendar.google.com`; do not use `primary`, an account email, or the runtime Calendar ID. Set the existing Google credential variables plus `RECEPTIONIST_APP_ENV=test`, `RECEPTIONIST_CALENDAR_SMOKE_ENV=synthetic`, `RECEPTIONIST_CALENDAR_SMOKE_CONFIRM=DEDICATED_NON_PRODUCTION_ONLY`, `RECEPTIONIST_CALENDAR_SMOKE_CALENDAR_ID=<the-secondary-calendar-id>`, and a database URL whose database name is exactly `receptionist_test`. The smoke ID is required separately; it never falls back to `RECEPTIONIST_GOOGLE_CALENDAR_ID`.

Run only when the dedicated Calendar and credentials are intentionally provisioned:

```sh
make calendar-smoke
```

The guarded flow uses synthetic local records and the existing booking operations, and cleanup targets only the exact deterministic event created by that run. If credentials are unavailable, record `not run: dedicated Calendar credentials unavailable`; do not substitute a live customer Calendar or claim a smoke pass.
