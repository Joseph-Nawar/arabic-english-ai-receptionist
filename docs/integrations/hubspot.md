# HubSpot integration

## Boundary and authority

Phase 3 adds one concrete HubSpot HTTP client and four internal application operations: `resolve_crm_contact`, `create_crm_contact`, `update_crm_contact`, and `record_booking_outcome`. The integration is a single-account modular-monolith boundary, not a general CRM sync service. No operation is exposed as a public route or LLM tool.

Google Calendar remains authoritative for appointment availability and external booking events. PostgreSQL owns local Contact identity, portal-scoped CRM mapping, Booking workflow, ToolExecution claims, idempotency, and sanitized audit. HubSpot owns CRM Contact records and Notes. A HubSpot error or uncertain Note delivery does not reverse or invalidate a successful Calendar booking.

## Service Key and activation

Configure the optional `RECEPTIONIST_HUBSPOT_SERVICE_KEY`, `RECEPTIONIST_HUBSPOT_PORTAL_ID`, and `RECEPTIONIST_HUBSPOT_REQUEST_TIMEOUT_SECONDS` settings. Store the account-level Service Key in a local secret manager or deployment secret store. It is represented as `SecretStr`, sent only as a Bearer credential over TLS to `api.hubapi.com`, and excluded from application results, ToolExecution data, audit metadata, and safe configuration summaries. Do not copy real values into this repository.

Before a provider operation, the same Service Key reads `GET /account-info/2026-03/details`; the returned portal ID must match the configured ID. The code does not infer a newer Account Info route or call an undocumented token-introspection API. Account admins must review the key's permissions in HubSpot. The approved minimum CRM scopes are `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.notes.read`, and `crm.objects.notes.write`; account-details preflight permission and the exact Service Key scope contract still require provisioning review. Do not broaden scopes to make a failed preflight pass.

The runtime currently reports CRM capabilities as unverified. Ordinary production Contact and Note writes remain fail-closed; a portal ID or successful mocked test is not capability evidence. Test and CI writes are permitted only through `httpx.MockTransport`, and those mocks do not mark capabilities verified. The dedicated developer smoke has a separate local-only activation path described below. Changing the configured Service Key or portal invalidates an in-memory preflight result; after key rotation, rerun account binding with the new key. Existing mappings remain scoped to their original portal and are not silently rebound.

## Identity and Contact fields

An active CRM link binds a local Contact UUID to one HubSpot Contact ID and portal. A stored provider ID is not sufficient by itself: operations freshly read the provider Contact and check the approved original phone or trusted verified-email provenance. Missing, archived, stale, ambiguous, cross-contact, portal-mismatched, or conflicting evidence stops the operation for review. Stale links are not silently reassigned. Names are not identity keys, and a caller cannot supply a HubSpot record ID to select a Contact.

Contact creation requires an internal trusted application boundary and a verified email identity. Request shape or a `trusted_*` field name does not establish trust; this repository has no customer confirmation UI, public Contact-write route, or LLM CRM write tool. Phone-only creation is not supported. Contact updates are limited to verified email and phone values: blank provider fields may be filled, equal values are unchanged, and nonblank conflicting values are not overwritten. Current HubSpot identity fields and provider links are not a free-form profile synchronization surface.

## Booking outcome Notes

`record_booking_outcome` is invoked explicitly after the Phase 2 booking transaction commits. Phase 2 does not call HubSpot automatically. The caller supplies only local Booking and source ToolExecution UUIDs. The application validates the original successful `confirm_booking_action` execution, its historical `ConfirmationResult`, owning Conversation, Booking/Contact relationship, and terminal `finished_at`. Action, status, requested slot, and UTC timestamp come from that immutable execution result and timestamp, never from the current mutable Booking.

The Note contains only a fixed operation description, historical action/outcome, local Booking UUID, historical requested UTC slot when present, and `booking-execution:<source-tool-execution-uuid>`. It excludes names, emails, phones, transcript, generated summaries, requirements, payment data, and provider payloads. `hs_timestamp` uses the source execution's historical terminal timestamp.

A durable CRM ToolExecution claim binds the source execution, Booking, Conversation, Contact, portal, mapped HubSpot Contact, and fixed Note operation before POST. Its idempotency key is deterministically namespaced from the source ToolExecution UUID. A successful same-key replay does not create another Note. A possible successful POST never triggers a second Note POST: an explicit same-key invocation performs one bounded Note search, fetches and verifies an exact canonical body/marker/timestamp candidate, and either terminalizes the original claim or retains uncertainty for review/retry of reconciliation.

Association evidence distinguishes an explicitly complete empty Contact list from missing or incomplete association metadata. Only a fully verified Note with complete evidence of no Contact association may use the narrow association repair. Wrong, multiple, or incomplete associations are not rewritten. Repair is read back and accepted only after the Note and exact intended Contact association verify. HubSpot search indexing may be delayed; zero candidates preserve an unresolved claim and do not authorize another POST.

## Privacy and retention

Provider responses are parsed into small snapshots. Raw HTTP bodies, response headers, Service Keys, customer identity values, and provider exceptions are not copied into durable CRM claims or audit metadata. Contact-write claims use booleans and keyed identity tags rather than raw trusted email/phone values. Booking Note claims contain opaque/local identifiers and finite historical action/status values, not the Note body or customer PII. HubSpot itself receives only the approved Contact fields and structured Note content.

This implementation defines no automatic deletion, retention schedule, historical backfill, queue, worker, or background retry. Existing local Contact and Booking retention remains governed by the application database policy.

## Manual developer-account smoke

The HubSpot smoke is manual only and is not part of CI, `make test`, `make check`, or `make verify`. It has not been run against a live provider (`LIVE_SMOKE_NOT_RUN`). The checked-in `REVIEWED_DEVELOPER_PORTAL_IDS` allowlist is intentionally empty. Do not add a portal until an owner has reviewed that it is a dedicated developer test account and its Service Key permissions; never use a production/customer portal.

Before enabling a run:

1. Provision a dedicated HubSpot developer test account and a Service Key out of band.
2. In the account's Service Key settings, verify Contact read/write and the documented account-preflight access with the account administrator. Record any remaining Notes scope uncertainty separately; this smoke exercises Contact create/read/archive only.
3. Independently review the portal ID, then add only that reviewed ID to `scripts/hubspot_smoke_allowlist.py` in a reviewed change.
4. Start the isolated, ephemeral smoke database with `make hubspot-smoke-db-up`. It listens only on `127.0.0.1:55433`; the guard also checks the connected database name `receptionist_hubspot_smoke`, database user `hubspot_smoke`, and PostgreSQL server port. It does not reuse the destructive `receptionist_test` guard or require application tables.
5. Supply these values to the command environment from a local secret manager. Do not put a real Service Key in shell history, `.env.example`, Git, logs, or command output:

   ```text
   RECEPTIONIST_APP_ENV=local
   RECEPTIONIST_HUBSPOT_SERVICE_KEY=<dedicated developer Service Key>
   RECEPTIONIST_HUBSPOT_PORTAL_ID=<reviewed developer portal ID>
   RECEPTIONIST_HUBSPOT_SMOKE_ENV=developer_test
   RECEPTIONIST_HUBSPOT_SMOKE_CONFIRM=RUN_DEDICATED_HUBSPOT_DEVELOPER_TEST_SMOKE
   RECEPTIONIST_HUBSPOT_SMOKE_CAPABILITIES_CONFIRM=I_VERIFIED_HUBSPOT_DEVELOPER_CONTACT_READ_WRITE_AND_ACCOUNT_PREFLIGHT
   ```

   Also set `RECEPTIONIST_DATABASE_URL` to a `postgresql+psycopg` URL for host `localhost`, port `55433`, user `hubspot_smoke`, and database `receptionist_hubspot_smoke`, using the same password as `HUBSPOT_SMOKE_POSTGRES_PASSWORD`. Compose defaults that local-only password to `hubspot_smoke`; a developer can override it through a secure environment source.

6. Run `make hubspot-smoke`, then `make hubspot-smoke-db-down`.

Every guard must pass before Contact access: local environment, explicit opt-in and capability attestation, reviewed portal, exact dedicated database URL and live database identity, and configured credentials. The same key then verifies the account portal. The run creates one Contact with only a UUID-scoped `hubspot-smoke+<run-uuid>@example.com` email, verifies that exact record, and archives only the exact ID freshly read back for that run. It does not create Notes or touch any other Contact. A timeout after possible creation is reconciled by exact email readback; it never repeats the Contact POST. Cleanup refuses any record whose ID/email no longer matches the current run. The command reports cleanup failure or uncertainty instead of claiming success.

The smoke profile uses a local-only Compose password and a temporary filesystem for PostgreSQL; it is not a HubSpot credential. Automated smoke tests use mocked HTTP transports only. No live HubSpot call, Contact creation, or cleanup has occurred in this implementation batch.

## Implemented and deferred

Mocked-provider tests and real isolated-PostgreSQL integration tests cover CRM identity, Contact writes, historical Note claims/recovery, concurrency, privacy boundaries, and Calendar independence. Those tests establish application behavior, not the developer account's actual scopes or provider-specific tier/configuration behavior.

Deals, Leads, OAuth, webhooks, automatic booking callbacks, historical backfill, workers, queues, operators' UI, Note deletion/duplicate cleanup, Contact merge, and production capability activation are not implemented. Phase 4 and later work has not started. Phase 3 is awaiting independent review.
