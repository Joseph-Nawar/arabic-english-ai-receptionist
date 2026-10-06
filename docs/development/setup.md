# Local development setup

The reproducible local loop uses host Python/uv for FastAPI and Docker Compose only for PostgreSQL.

## Fresh clone

1. Install Python `3.12.15`, uv `0.12.2`, Docker, and Docker Compose.
2. Clone the repository and switch to the requested branch:

   ```sh
   git clone https://github.com/Joseph-Nawar/arabic-english-ai-receptionist.git
   cd arabic-english-ai-receptionist
   git switch phase/0-foundation
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

6. Start the development database, apply the currently empty Alembic schema, and run the web process on the host:

   ```sh
   make db-up
   make migrate
   make dev
   ```

7. In another terminal, verify the process:

   ```sh
   curl -i http://127.0.0.1:8000/health/live
   curl -i http://127.0.0.1:8000/health/ready
   ```

The application does not run migrations automatically at startup. Stop infrastructure with `make db-down` when finished. The isolated test database uses `make test-db-up`, port `55432`, and database name `receptionist_test`; use `make test-db-down` to stop it.

