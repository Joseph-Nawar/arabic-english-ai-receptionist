# Phase 3 — HubSpot CRM Integration: Requirements and Architecture Design

**Status:** Revised design proposal for independent review. Design only; no Phase 3 production implementation is included.
**Research checked:** 2026-10-25.
**Target CRM API version:** HubSpot CRM API 2026-09 where the exact endpoint is documented.
**Target branch:** phase/3-hubspot-crm-integration.

## 1. Product goals and user workflows

Phase 3 adds a bounded CRM capability for the one-business receptionist deployment. Its four application operations are exactly:

- `resolve_crm_contact`
- `create_crm_contact`
- `update_crm_contact`
- `record_booking_outcome`

The intended workflows are:

- Resolve a local Contact to an existing HubSpot Contact only when the configured identity evidence identifies one record without conflict.
- Create a Contact only through a trusted internal boundary with explicitly verified email identity. Phone-only creation is deferred.
- Update only the allowlisted email and phone fields when values are verified and the HubSpot value is blank or already equal.
- When explicitly invoked after a successful Phase 2 booking operation, record one short structured Note against the verified HubSpot Contact. The original Calendar booking remains successful if this separate CRM call fails or is uncertain.

The repository has no implemented authenticated operator interface or trusted conversational confirmation mechanism. No customer-originated Contact write workflow is enabled by Phase 3. Contact writes are callable only through a trusted application/operator boundary supplying explicitly verified field values; the request itself, a boolean flag, or arbitrary LLM-generated arguments do not establish trust. Until a later approved phase supplies a real confirmation mechanism, these paths are exercised only through controlled trusted internal invocations.

Phase 3 does not automatically invoke CRM after every Phase 2 booking. A future Phase 4 orchestrator may explicitly call the already-tested `record_booking_outcome` operation after booking commit. Phase 2 booking confirmation is not changed to inject HubSpot HTTP calls.

## 2. Scope, non-goals, and assumptions

### In scope

- One HubSpot account and one configured business per deployment, using the synthetic Riyadh HomeCare configuration for the demo.
- Account-level HubSpot Service Key authentication, provisioned out of band.
- The four named application operations above, with finite typed requests and results.
- One small local-to-HubSpot Contact link table, if implementation review retains the schema described below.
- Explicit post-commit booking outcome Notes and local `ToolExecution` recovery state.
- Provider-free automated tests and a separately guarded live smoke design.

### Out of scope

- Deals and Leads, including any Deal qualification subsystem, Deal configuration, scopes, HTTP client, or runtime operations. The comparison in section 3 explains why they are deferred.
- Multitenant SaaS, a general-purpose CRM synchronization engine, cross-provider identity framework, provider registry, or generic retry engine.
- OAuth consent, a Marketplace app, a project-based HubSpot app, HubSpot webhooks, contact merges, bulk synchronization, or custom HubSpot properties.
- Automatic booking-event delivery, historical backfill, queues, workers, publishers, scheduled retries, Redis, or an outbox processor.
- Calendar availability or appointment-event ownership in HubSpot; raw conversation transcripts; dashboards; Phase 4 or later work.
- Production implementation in this design task.

### Assumptions and durable ownership

- Phases 0–2 remain `COMPLETE`. PostgreSQL owns local Contact identity, canonical E.164 phone, Conversation, Booking, pending actions, `ToolExecution`, idempotency, local/provider links, and sanitized audit. Google Calendar owns appointment availability and external booking-event state. HubSpot owns CRM Contact fields and operator changes. Application policy owns permission to attempt each CRM mutation.
- Phase 1 behavior for unknown or withheld phone identity remains unchanged: it creates/uses a distinct unresolved local Contact and never resolves identity by name.
- The chosen live test account and credentials are not yet available or authorized. No HubSpot account/API was accessed in this task.
- Phase 3 design progress does not change the roadmap: Phase 3 remains `IN PROGRESS — design only`; Phases 4–12 remain `NOT STARTED`.

## 3. Current HubSpot API research

Official HubSpot sources were checked on 2026-10-25. CRM object routes below use the stable 2026-09 version only where the exact endpoint is documented. Date-based API versions are immutable snapshots with an 18-month support window. Do not mechanically relabel older endpoint paths.

### CRM endpoints proposed for Phase 3

| Operation | Documented route | Use |
| --- | --- | --- |
| Read Contact by record ID | `GET /crm/objects/2026-09/contacts/{recordId}` | Verify a mapped record and the allowlisted identity fields |
| Read Contact by email | `GET /crm/objects/2026-09/contacts/{email}?idProperty=email` | Exact read using a trusted email identity |
| Search Contacts | `POST /crm/objects/2026-09/contacts/search` | Bounded phone candidate lookup; every candidate is verified locally |
| Create Contact | `POST /crm/objects/2026-09/contacts` | Trusted, email-keyed create after a successful no-match resolution |
| Update Contact | `PATCH /crm/objects/2026-09/contacts/{contactId}` | Fill allowlisted blank fields only |
| Search Notes | `POST /crm/objects/2026-09/notes/search` | Bounded recovery candidates for a possible Note write |
| Create Note | `POST /crm/objects/2026-09/notes` | Create a structured Note associated with Contact using documented association type 202 |
| Associate existing Note | `PUT /crm/objects/2026-09/notes/{noteId}/associations/contact/{contactId}/202` | Repair only a verified missing Contact association |

The official [Contacts guide](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/contacts/guide), [CRM Search guide](https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm), [Notes guide](https://developers.hubspot.com/docs/api-reference/latest/crm/activities/notes/guide), and [Associations guide](https://developers.hubspot.com/docs/api-reference/latest/crm/associations/associate-records/guide) document these CRM contracts. Note creation uses `hs_timestamp` and `hs_note_body`; the Note can be associated with a Contact in its create request. A separate association request is reserved for a verified partial-association recovery.

HubSpot Contacts creation requires at least one of email, first name, or last name. Email is HubSpot's primary unique identifier; Contacts upsert supports email or a custom unique property, and email batch upsert does not provide partial upsert. Phone and mobilephone are searchable but are not documented as unique. Phase 3 uses no custom property and no batch upsert.

HubSpot phone search uses standardized calculated search values that compare area/local number and omit the country calling code. The public guide does not establish a stable calculated property name. The proposed request searches documented phone/mobilephone fields using the national number; returned values are parsed and compared to the local Contact's exact E.164 value. Do not query undocumented calculated property names. Search is limited to five requests per second per account, 200 results per page, and 10,000 results per query. A partial page, additional page, or incomplete candidate result set cannot prove uniqueness. Search indexing may lag after writes, and archived records are omitted; neither condition permits a blind duplicate create.

### Account binding and version uncertainty

HubSpot's official Public API Workspace currently documents account details at `GET /account-info/2026-03/details`; that response includes the authenticated account's portal ID. The [Fall 2026 Spotlight](https://developers.hubspot.com/changelog/fall-2026-spotlight) lists Account Info among APIs updated in 2026-09, but the exact 2026-09 account-details route and Service Key-compatible authorization scope were not confirmed in the public reference checked for this design. The request path must not be changed to 2026-09 by inference.

Before live activation, a fail-closed preflight must use a supported, documented account-details method with the configured Service Key and confirm the returned portal ID equals the reviewed expected account. An administrator must independently verify the key's intended scopes/permissions in HubSpot's Service Key settings. Do not depend on an undocumented token-introspection endpoint. If the account-details read cannot be performed with the provisioned key and a supported scope, or the exact credential-to-account and scope binding cannot be established, keep live writes disabled pending explicit provisioning review. The configured portal ID alone is not proof of credential binding.

### Contacts and Notes compared with Deals and Leads

Two product approaches were considered:

- **A. Contacts plus associated Notes:** resolves customer identity, permits narrow profile maintenance, and records booking/customer-operation outcomes in the Contact timeline. It is commercially useful for a receptionist while preserving Calendar as the appointment authority.
- **B. Contacts plus Notes plus Deals/Leads:** adds explicit sales opportunity tracking. Deals model transactions and require pipeline/stage configuration and account-specific validation; Leads availability is tier-sensitive. Both add qualification policy, permissions, and operational behavior that the current synthetic Riyadh HomeCare configuration does not define.

**Recommendation:** choose A for Phase 3 v1. Do not create a Deal for a booking. Defer Deals and Leads until a separate business rule, account-tier/scope verification, and design approval exist. HubSpot's current help material describes broad manual record/activity availability, but that does not prove this integration's Service Key scopes, association permission, or account validation settings. The official Leads API launch notice specifies Sales Hub Professional/Enterprise, and the 2026-09 Spotlight does not say that restriction changed. These details remain external verification gates, not Phase 3 behavior. References: [record creation](https://knowledge.hubspot.com/records/create-records-on-the-updated-index-page), [logging activities](https://knowledge.hubspot.com/records/manually-log-activities-on-records), [Leads API release](https://developers.hubspot.com/changelog/new-leads-api-released?hs_amp=true), [Deals guide](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/deals/guide), and [Fall 2026 Spotlight](https://developers.hubspot.com/changelog/fall-2026-spotlight).

### Authentication, scopes, validation, rate limits, and test accounts

HubSpot announced account-level Service Keys for system-to-system use in February 2026. They do not support webhooks, allow scoped API access, and remain public beta in the Fall 2026 release. HubSpot says keys can only receive scopes their creator already has; rotation has a seven-day overlap. The legacy private-app creation sunset is account-age dependent, with the remaining older-account cutoff scheduled for 2026-10-26. A Service Key is the preferred design for this single-account outbound integration only if the complete Contact, Note, association, and account-preflight permission set is verified. No webhook dependency is introduced. See [Service Keys](https://developers.hubspot.com/changelog/service-keys), [Fall 2026 Spotlight](https://developers.hubspot.com/changelog/fall-2026-spotlight), and [legacy private-app sunset](https://developers.hubspot.com/changelog/legacy-private-app-creation-sunset).

Proposed minimum CRM scopes, subject to confirmation in the actual Service Key picker and exact endpoint docs:

- `crm.objects.contacts.read`
- `crm.objects.contacts.write`
- `crm.objects.notes.read`
- `crm.objects.notes.write`

The account-details preflight's required Service Key scope has not been verified. Verify it using current official endpoint documentation and the real test account. If the account-details permission or any CRM/association permission is unavailable to a Service Key, or broader privileges would be required, live writes remain disabled pending provisioning review. Do not silently broaden scopes or switch to OAuth. No Deals/Leads scopes are requested.

Beginning with 2026-09, HubSpot applies administrator-configured required properties, conditional validation, and applicable association permissions to API writes. A valid application request may therefore receive HTTP 400. Treat it as a safe configuration/data rejection and do not retry the same body. General API limits depend on account subscription; respect `Retry-After` where present and allow at most one bounded retry when the response proves no write occurred and the deadline permits it. Timeouts and 5xx responses on writes are uncertain, not retry permission. See [write validation](https://developers.hubspot.com/changelog/crm-api-write-validation-enforcement), [usage guidelines](https://developers.hubspot.com/docs/developer-tooling/platform/usage-guidelines), and [error handling](https://developers.hubspot.com/docs/api-reference/latest/error-handling). The [OAuth retry article](https://developers.hubspot.com/blog/oauth-token-management-hubspot-integrations) is cited only for general Retry-After/timeout guidance, not as an OAuth design.

A free developer account can create test accounts; HubSpot says test accounts reset periodically. The standard sandbox has an Enterprise subscription requirement. Prefer a dedicated developer test account; its actual plan, Service Key availability, and endpoint permissions must be inspected before any future live verification. Sources: [Developer Platform Basics](https://developers.hubspot.com/developer-platform-basics), [sandbox requirements](https://knowledge.hubspot.com/account-management/deploy-sandbox-changes-to-production), and [HubSpot test-account guidance](https://developers.hubspot.com/blog/how-to-safely-experiment-with-ai-in-hubspot-before-you-touch-real-data).

## 4. Authentication and configuration

Use one account-level Service Key per deployment, provisioned out of band by an account administrator. No in-app key creation, OAuth consent flow, or project-based HubSpot app is proposed.

| Setting | Type and behavior |
| --- | --- |
| `RECEPTIONIST_HUBSPOT_SERVICE_KEY` | Optional Pydantic `SecretStr`; never exposed by repr, safe summary, logs, errors, `ToolExecution`, or `AuditEvent` |
| `RECEPTIONIST_HUBSPOT_PORTAL_ID` | Expected account namespace; required for live activation but does not by itself verify which account the credential targets |
| `RECEPTIONIST_HUBSPOT_REQUEST_TIMEOUT_SECONDS` | Finite connect/read timeout, default no greater than 10 seconds |

Missing/blank credentials or incomplete account preflight safely disable CRM calls; startup and Calendar booking remain available. Send the key only as a Bearer credential over TLS to `api.hubapi.com`. Never persist it in JSON or diagnostic events. Rotate by provisioning a replacement out of band, updating the secret, verifying the preflight again, and revoking the old key within HubSpot's rotation window. Do not reveal either key value.

A runtime change to a different portal must fail closed when Contact links for the previous portal exist. Configuration alone must never reinterpret those links as belonging to the new account. Normal test/CI runs disable the HubSpot transport even if a credential is present.

## 5. CRM objects and exact ownership model

| Data or decision | Authority |
| --- | --- |
| Local Contact, E.164 identity, Conversation, Booking, pending actions, `ToolExecution`, idempotency, CRM link, sanitized audit | PostgreSQL |
| CRM Contact profile/lifecycle and staff edits; CRM Notes | HubSpot |
| Appointment availability and external booking-event state | Google Calendar |
| Permission to mutate, trusted-input provenance, safe identity resolution, field allowlist, and booking-vs-CRM result semantics | Application policy |

A HubSpot Note describes a completed local operation. It does not create, reschedule, cancel, or verify a Calendar event. HubSpot Contact lifecycle fields remain HubSpot-owned; Phase 3 never sets them. HubSpot is never authoritative for Calendar availability.

## 6. Application operations and typed contracts

These are internal application operations, not HTTP routes or LLM-exposed tools. Requests use strict finite models with unknown fields forbidden. No operation accepts arbitrary HubSpot properties, object types, URLs, association IDs, or provider payloads. PII values needed for an operation exist only in memory and are excluded from durable `ToolExecution` and audit data.

Contact operations receive the local `conversation_id` and `contact_id`, plus an idempotency key. Contact write operations receive only the selected, explicitly verified email/phone values from a trusted internal/operator call boundary. A boolean such as `email_identity_confirmed=true` is never authorization; constructing the request is not proof of provenance. Until a later phase implements an actual trusted confirmation mechanism, production customer-originated Contact writes are disabled. No arbitrary LLM-generated values may reach these operations.

| Operation | Request contract | Result contract |
| --- | --- | --- |
| `resolve_crm_contact` | `conversation_id`, `contact_id`, `idempotency_key`, and optional trusted verified email value. The local Phase 1 phone comes from PostgreSQL. | `linked`, `found_and_linked`, `not_found`, `ambiguous`, `unresolved_identity`, `stale_mapping`, `identity_conflict`, `provider_unavailable`, `permission_denied`, or `not_configured`; at most an opaque Contact ID is returned. |
| `create_crm_contact` | `conversation_id`, `contact_id`, `idempotency_key`, required trusted verified email value, and optional trusted verified E.164 phone value. The application first requires a successful no-match resolution. | `created`, `replayed`, `identity_conflict`, `uncertain`, `failed`, or `not_configured`; response may include an opaque Contact ID only. |
| `update_crm_contact` | `conversation_id`, `contact_id`, `idempotency_key`, and a non-empty selected set of trusted verified email/phone values. | `updated`, `unchanged`, `conflict`, `uncertain`, `failed`, or `not_configured`; response contains safe field names only. |
| `record_booking_outcome` | `booking_id` and the immutable source booking `ToolExecution` ID. Contact and Conversation are derived and validated from local records; the deterministic CRM key is derived from the source ID. | `written`, `replayed`, `skipped_no_verified_contact`, `uncertain`, `failed`, or `not_configured`; response contains a safe result code and, where verified, an opaque Note ID. |

All results include the operation, a finite outcome, a `replayed` flag, an optional safe error code, and a bounded retry action: `none`, `retry_same_key`, `reconcile_same_key`, or `operator_review`. They never contain a Contact's name, phone, email, HubSpot properties, request/response bodies, credentials, or raw provider errors.

## 7. Contact identity and mapping strategy

### Durable mapping

One small account-scoped Contact link table is justified because the local Contact model has no HubSpot ID and repeated fuzzy lookup is unsafe. Proposed future Alembic table:

- `contact_id`: primary key and restrictive foreign key to local Contact.
- `hubspot_portal_id`: account namespace.
- `hubspot_contact_id`: opaque HubSpot record ID.
- `linked_by`: finite value `phone_e164` or `verified_email`.
- `state`: `active` or `stale`.
- `verified_at`, `created_at`, and `updated_at`: UTC timestamps.
- Unique constraint on `(hubspot_portal_id, hubspot_contact_id)`.
- No PII, provider payload, secret, or arbitrary JSON column.

Before using a mapping, require the live-activation portal preflight and exact stored/configured portal equality. A changed account fails closed; changing the runtime portal cannot reassign an existing mapping. This is the only proposed CRM table. No migration is created in this design task.

### Deterministic resolution order

1. **Existing mapping:** read the mapped Contact by ID and verify the account namespace and every known verified identity value that is present on the provider record. For a phone link, at least one nonblank returned phone field must normalize exactly to local E.164. For a verified email, a nonblank returned primary email must equal the trusted email using a documented case-insensitive comparison. A missing/blank provider field alone is not conflicting evidence; a nonblank different value is. If both verified phone and email lookups produce provider matches, they must identify the same Contact. A mismatch is `identity_conflict`; never rewrite identity to make the link pass.
2. **Stale mapping:** a 404, archived record, or merged/removed record makes the mapping stale; the response does not establish whether an operator archived, deleted, or merged it. Re-search only using the available verified identity signals. Relink only if the exact rules below identify one record and every known signal agrees. No replacement Contact is created in that invocation.
3. **Phone search:** search the local canonical E.164 number in HubSpot's supported national-number form, retrieve a bounded single page, normalize every returned phone/mobilephone value locally, and compare exact E.164. One verified record is a candidate; zero is no match; multiple candidates, incomplete results, or another page mean ambiguity. Never match by name.
4. **Verified email search:** when a trusted verified email is available, read by the documented email `idProperty`. When verified phone and email searches each produce a provider match, require both to identify the same HubSpot ID. A provider candidate with a blank phone or email field is not automatically conflicting; a nonblank value that disagrees with a known verified local identity, or separate actual matches resolving to different IDs, returns `identity_conflict` and creates no link. If an email match has a blank phone and there is no competing verified phone match, email linking may proceed after exact email verification.
5. **No phone / withheld phone:** preserve Phase 1's unresolved local Contact. A trusted verified email may independently resolve an existing Contact. Without it, return `unresolved_identity` and do not write. Do not create a substitute/fake email.
6. **No match:** return `not_found`; Contact creation is a separate explicit operation requiring trusted verified email and a successful provider read. Phone-only creation is deferred.
7. **Provider outage or permission error:** return `provider_unavailable` or `permission_denied`. Do not infer a no-match, link by guess, or create during a read outage.
8. **Conflict, merge, or reassignment uncertainty:** do not merge Contacts or disclose candidates. Require a later trusted internal resolution/review where identity remains ambiguous.

For an email-linked mapping (`linked_by=verified_email`), persist the trusted verified email in the existing local `Contact.email` as part of the controlled linking workflow, only when that local field is blank or already equal. A different or unverified preexisting local email is not trusted evidence and must not be overwritten to establish the link. After restart, reverify the link using `linked_by` metadata and the existing local Contact email, then read and compare the provider Contact. Only the trusted linking/update workflow may establish or change that local value; an inconsistent change requires trusted re-resolution. Do not add a general identity-provenance subsystem or a separate PII snapshot.

Phone is not unique in HubSpot. A matching name never resolves an identity; incomplete candidate results cannot prove uniqueness; customer records are never linked or merged by guess.

## 8. CRM field ownership and data mapping

| HubSpot property | Local/trusted source | Write rule |
| --- | --- | --- |
| `email` | Trusted verified email supplied in the current internal operation | Required identity for Contact create; for update, write only when blank or already equal. A different nonblank value is a conflict. |
| `phone` | Phase 1 local E.164 plus trusted verified value for a write | Optional on create; on update write only when blank or normalizes to the same E.164. A different nonblank value is a conflict. |
| `mobilephone` | HubSpot candidate read only | Never write in Phase 3. |
| Name, address, lifecycle, lead status, owner, company, and marketing/subscription fields | None | Never write. |

Do not split Arabic or English display names into first/last names. A blank, withheld, unverified, or invalid value is omitted, never sent as an empty string, and never clears HubSpot data. On update, read current values immediately before PATCH. If any selected nonblank value differs from trusted input, return conflict and apply none of the selected values. Preserve staff edits. A 2026-09 account-specific validation rejection is a safe failure requiring administrator review; do not retry the same invalid body.

## 9. Booking outcome Note behavior

`record_booking_outcome` is an explicitly callable post-commit application operation. It is not automatic delivery for every Phase 2 booking and does not change Phase 2 booking confirmation. A future Phase 4 orchestrator may invoke the operation after the successful Calendar/booking transaction. The CRM result is separate from the original booking result.

Before creating a Note, load and validate the immutable source records:

- The source `ToolExecution.status` is `SUCCEEDED` and `tool_name` is exactly `confirm_booking_action`.
- Derive create, reschedule, or cancel from the source execution's immutable `sanitized_arguments.action_type`; do not accept a caller-supplied operation enum.
- The immutable `sanitized_result` is a successful `ConfirmationResult` for `confirm_booking_action`, and its `data.booking_id` equals the requested Booking ID.
- The source Conversation belongs to the Booking's Contact, and the Booking still belongs to that same Contact.
- The action type is create, reschedule, or cancel, and the immutable confirmation result has the matching historical terminal result: confirmed for create/reschedule, cancelled for cancel.
- Require a non-null source execution `finished_at`. Derive Note operation, outcome, historical requested slot (when present), and `hs_timestamp` from the source action, serialized result, and that immutable terminal time. Do not consult current `Booking.status`, current `Booking.booking_data.last_operation`, current slot, or other mutable Booking fields to identify or describe the historical operation.

The Phase 2 `ConfirmationResult` contains booking ID, historical status, requested UTC start/end, and Calendar references. It does not contain a stable service code, so the Note omits service code. The short fixed body contains operation, historical outcome, local Booking UUID, historical requested UTC slot when present, and a stable opaque source-operation marker derived from the source `ToolExecution` UUID. It excludes free text and PII. Associate the Note to the already verified HubSpot Contact in the create request.

A booking can be created, rescheduled, and later cancelled. Each successful source `ToolExecution` is independently eligible for a Note even when invocation is delayed until after later Booking changes. The source operation's original result and `finished_at` remain the authority. Replaying the same source operation reuses its deterministic CRM `ToolExecution` key and does not intentionally issue another Note create.

If no verified HubSpot Contact link exists, return `skipped_no_verified_contact`; do not create a Contact from the booking. If HubSpot is unavailable, the booking remains successful. There is no automatic invocation, historical backfill, queue, worker, publisher, scheduled retry, or guaranteed delivery.

## 10. Idempotency and durable recovery

Reuse the existing `ToolExecution` and sanitized `AuditEvent` primitives. Add no outbox, job, Note link, or generic provider table. CRM execution data contains safe local UUIDs, operation enums, selected field names, outcome/error codes, and opaque provider IDs only.

- **Claim before CRM mutation:** persist a CRM `ToolExecution` with `started` status and unique idempotency key before a provider write. For `record_booking_outcome`, create this claim only after the booking transaction and source execution have committed. If claim persistence fails, do not call HubSpot and do not change the successful booking result.
- **Contact keys:** require caller-supplied idempotency keys scoped to the exact internal operation. A confirmed successful replay returns the stored result. Reconciliation of ambiguous create first reads by the trusted exact email and verifies phone/identity; if no result is visible, remain uncertain and do not POST again.
- **Note key and marker:** deterministically derive the CRM idempotency key from the immutable source booking `ToolExecution` ID. Embed a stable opaque source-operation marker in `hs_note_body`. On uncertain Note create, use Note search only for bounded candidates. Verify the exact marker, returned Note identity, and exact expected Contact association before marking success.
- **Delayed Note indexing:** if a write may have succeeded but a bounded search finds no Note, keep the `ToolExecution` uncertain. Do not issue a second create while indexing is delayed. This design does not promise exactly-once delivery or eventual delivery.
- **Partial association:** if a Note exists but the Contact association is absent, read and verify both IDs and repair only the missing association with the documented association operation; read back to verify. Never create another Note to repair an association.
- **Provider success/local persistence failure:** preserve the precommitted `started` CRM execution and reconcile by exact email or Note marker before any mutation. If the outcome remains unknowable, keep uncertainty and require a later explicit same-key invocation or review.
- **Known no-write failures:** a later explicit same-key invocation may retry only when the prior result proves no mutation occurred. Respect bounded Retry-After behavior. Invalid 400 requests are not retried unchanged.
- **Concurrency:** serialize local CRM mutations for the same Contact using a row lock and inspect for earlier started CRM executions before a new mutation. An uncertain operation blocks later CRM writes until reconciled or reviewed. This does not lock out HubSpot staff edits.
- **Audit:** record sanitized attempted, completed, and reconciled states; never persist raw request/response bodies, email, phone, name, key, or provider error text.

If the process stops after booking commit but before CRM invocation or claim persistence, no automatic recovery occurs; a later explicit invocation can derive the same source key. If it stops after claim persistence, a later same-key invocation reconciles. If no later invocation occurs, a Note may remain absent.

## 11. Provider boundary and HTTP behavior

The later implementation should use one concrete HubSpot integration module behind the application operation boundary. Do not introduce a generic adapter, protocol, registry, SDK requirement, repository framework, or provider abstraction. The module owns the fixed HubSpot host, Bearer header, TLS, documented versioned routes, finite timeout/response size, allowlisted request serialization, narrow response parsing, and safe error mapping. Raw provider data remains inside this boundary.

Bound Contact resolution to a small number of reads and at most one result page per search. If the page signals more results, uniqueness is unproven. Every mutation has a deadline, bounded request count, idempotency key, safe output code, and explicit retry/reconciliation action.

| Condition | Application behavior |
| --- | --- |
| 2xx | Validate only expected fields/association, then persist safe state |
| 400 validation/property error | No retry with same body; return safe validation failure |
| 401 | Mark credential/auth failure; no retry loop; booking unaffected |
| 403 | Mark permission/scope/tier failure; no retry loop; booking unaffected |
| 404 mapped Contact | Mark stale and apply only bounded deterministic re-resolution; no create in the same stale-map invocation |
| 404 during Note reconciliation | Search bounded marker candidates; otherwise retain uncertainty |
| 409/duplicate conflict | Read by exact email or marker and verify; never blindly create again |
| 429 | Respect `Retry-After`; at most one bounded retry only when non-mutation is known and deadline permits |
| Read timeout/connection error/5xx | At most one bounded read retry; if unresolved return safe provider failure |
| Write timeout/connection loss/5xx | Mark uncertain; read-only same-key reconciliation, never blind create retry |
| Provider success then local persistence failure | Reconcile exact email/marker before another mutation |
| Partial Note association | Verify exact records, repair only verified missing association, never recreate Note |

The central invariant is that CRM failure never rolls back, rejects, or falsely reverses a successfully confirmed Google Calendar booking.

## 12. Schema changes

No production schema change is made in this design task. The only justified future schema addition is the small account-scoped `crm_contact_link` table in section 7, added by Alembic with an upgrade and verified downgrade. Existing Contact, Conversation, Booking, `ToolExecution`, and `AuditEvent` primitives are sufficient for CRM operation/recovery state. No raw CRM payload, generic provider identity, operation queue, outbox, or Deal/Note table is proposed.

## 13. Privacy and security invariants

- HubSpot may receive only trusted verified Contact email/phone and fixed structured Note fields. Do not send display name, address, transcripts, conversation summaries, requirements, payment data, marketing fields, or free-form AI text.
- PostgreSQL persists local/provider IDs, finite operation state, safe outcomes, and sanitized audit only. Do not persist raw HubSpot payloads, response bodies, headers, Service Keys, email, phone, names, Note text, or unfiltered provider errors in `ToolExecution` or `AuditEvent`.
- Do not reveal whether a particular HubSpot Contact exists or disclose candidate values. Return only safe result categories and opaque IDs to trusted internal callers.
- Resolve every association from a verified local Contact link and the preflight-verified account. Never accept provider record IDs from a client or model.
- Validate IDs, enum values, E.164, verified email, timestamps, and fixed Note fields at the application boundary. Serialize only the finite schemas.
- Protect the key with the existing `SecretStr` pattern. Diagnostics expose only configured/activation state and bounded timeout; never key values. Missing credentials, failed portal binding, or insufficient scope disables CRM without blocking startup or booking.
- Rotate keys out of band; do not log either value. No webhooks are used or required. No cross-Contact data may escape through reads, errors, conflict results, or search.

## 14. Testing strategy and guarded live smoke

Normal automated tests and CI remain completely provider-free, even when credentials happen to exist in the environment.

- Mock HTTP boundary tests cover exact documented routes, Bearer isolation, account-preflight failure, request allowlists, response parsing, status mapping, bounded calls, and no network fallback.
- A deterministic local HTTP test double may cover Contact/Note response shapes without external calls.
- PostgreSQL tests cover link uniqueness/account mismatch, exact E.164 and email consistency, ambiguity and conflict, stale mappings, concurrent same-Contact operations, idempotency, and persistence/recovery failures.
- Trusted-write tests call the internal operation with controlled verified values and assert no request boolean or model-generated argument can grant trust. Customer-originated write paths remain disabled until a later approved trusted confirmation mechanism exists.
- Booking Note tests validate source ToolExecution status/name/action arguments/result, source Conversation/Contact ownership, Booking ownership, and immutable result-derived content/time. Explicit delayed-delivery acceptance tests must create/reschedule/cancel one Booking, then record earlier successful source operations after subsequent Booking changes and prove that each Note still reflects its own historical source result. Current Booking status and `last_operation` must not affect historical eligibility/content.
- Failure/privacy tests cover 400/401/403/404/409/429/5xx, read and write timeouts, accepted write followed by local persistence failure, ambiguous create, delayed Note indexing, partial association, staff edits, missing credentials, and absence of PII/keys/raw provider payloads in logs and durable JSON.
- Migration tests cover link-table upgrade and downgrade. Existing Phase 2 booking tests remain unchanged and retain Calendar authority.
- CI must refuse CRM HTTP calls in test/CI environments even if credentials are present.

Any future live smoke is separately invoked and requires explicit opt-in, a dedicated non-production test database with a strict database-name guard, a dedicated developer test account on a reviewed portal allowlist, the same-key account-details preflight, verified minimum Service Key permissions, and synthetic records only. It must attach a run marker, act only on exact records created/verified by that run, and clean up only records still owned by that run. Report cleanup success, failure, or partial completion honestly. If account binding, ownership marker, or cleanup guard fails, stop further writes. Never run it in normal CI and never report a live pass without actual execution. No live call or credential is authorized in this task.

## 15. Failure matrix

| Condition | Contact resolution | Contact create/update | Booking outcome Note | Durable result/retry |
| --- | --- | --- | --- | --- |
| Existing verified link | Return linked after fresh identity read | Apply fill-blank policy only | May write after explicit invocation and source validation | Persist safe link check and CRM result |
| Exactly one verified candidate | Link one Contact | Continue only with trusted selected values | Associate only to verified Contact | Persist link and operation result |
| Phone/email signals resolve differently or conflict | `identity_conflict`; no link | No mutation | No Note | Operator review; no guessing |
| No match after successful read | `not_found` | Create only with trusted verified email | No Note until Contact is safely resolved | Known no-write; separate explicit operation may create |
| Ambiguous/incomplete search | `ambiguous` | No create/update | No Note | Review; no name match or guess |
| Missing/withheld phone and no trusted email | `unresolved_identity` | No create/update | `skipped_no_verified_contact` | Preserve Phase 1 unresolved Contact |
| Stale/deleted/archived/merged mapping | Re-resolve only with verified signals | No mutation until relink verified | No Note until relink verified | Mark stale; no same-call replacement create |
| Provider read outage | `provider_unavailable` | No create/update | No create if not yet sent; uncertain if write outcome already possible | Explicit retry only when safe; booking unchanged |
| Account preflight mismatch or not verifiable | Not configured/permission denied | Disable writes | Disable Note writes | Fail closed pending provisioning review |
| 400 account validation | Read behavior unaffected | Safe validation failure | Safe validation failure | No identical retry; operator fixes account/data rule |
| 401/403 or missing scope | `permission_denied` | No mutation | No mutation | Safe failure; administrator verifies credential/scopes |
| 404 mapped Contact | Mark stale and bounded re-resolution | No write in stale-map invocation | No association until Contact verified | Reconcile by verified identity |
| 409/duplicate | Exact identity read and verify | Never blind repeat | Search exact marker and verify Contact association | Reconcile or remain uncertain |
| 429 | Respect bounded Retry-After | Retry once only if no write is proven | Same rule | Otherwise failed/uncertain with no extra create |
| Write timeout, connection loss, or 5xx | N/A | Exact email read only; no second POST if absent | Bounded marker search; no second Note create if absent | Retain `started`/uncertain; same-key read reconciliation |
| Provider success, local persistence failure | N/A | Read exact email before further mutation | Search marker and verify association | Preserve started state; no blind repeat |
| Partial Note association | N/A | N/A | Verify and repair only the missing association | Do not create another Note |
| Later Booking reschedule/cancel before Note invocation | N/A | N/A | Validate source's immutable result and record that historical operation | Do not compare mutable current status/last operation |
| Concurrent same-local-Contact CRM mutation | Serialize through local row lock | Wait/reconcile older started operation | Same | Uncertain operation blocks later mutations until resolved |
| Staff edits HubSpot identity field | Re-read candidate and return conflict when mismatched | Preserve nonblank differing value | No effect on Note source; verify linked Contact before association | No overwrite; safe field-level outcome |
| Note marker removed or not indexed | N/A | N/A | Keep uncertain; never send second create | Later bounded reconciliation/review; no exactly-once claim |

## 16. Later implementation milestone breakdown

This high-level milestone breakdown is not the task-by-task coding plan. Individual implementation tasks remain deferred until the revised design receives independent approval.

- **M3A:** Connection, account preflight contract, API boundary and transport tests.
- **M3B:** Contact link schema, deterministic lookup, identity resolution and concurrency.
- **M3C:** Trusted Contact create/update, field ownership and recovery.
- **M3D:** Structured booking outcome Notes and safe source-operation reconciliation.
- **M3E:** Security/privacy, migrations, guarded smoke, documentation and final review.

## 17. Acceptance criteria for later implementation

- Phase 3 exposes exactly `resolve_crm_contact`, `create_crm_contact`, `update_crm_contact`, and `record_booking_outcome`; no Deal or Lead runtime contract exists.
- Service Key activation fails closed unless the same credential is bound by a supported account-details preflight to the expected portal and the administrator-verified minimum scope/permission set is available. Portal changes cannot reinterpret existing links.
- Normal tests/CI cannot make live HubSpot requests, including when credentials are configured.
- Contact identity resolution checks existing mapped ID first, exact E.164, and trusted verified email; both signals must identify the same Contact. Phone is never treated as unique, and there is no name matching, guess, or Contact merge.
- Unknown/withheld phone keeps Phase 1 behavior. Phase 3 Contact creation requires trusted verified email, successful no-match reads, and controlled internal invocation; no phone-only creation or fake email exists.
- Contact writes only include allowlisted trusted values; blank values are omitted, staff-owned nonblank differences conflict, and arbitrary LLM arguments cannot authorize a write.
- `record_booking_outcome` is callable explicitly after commit and never injects an HTTP call into Phase 2 confirmation.
- A Note requires a succeeded `confirm_booking_action` source execution, action derived from immutable source arguments, matching source ConfirmationResult booking ID, source Conversation/Booking Contact ownership, and create/reschedule/cancel action.
- Note content and timestamp derive from the immutable source execution/result. Delayed delivery after subsequent reschedule/cancel still records each historical operation correctly without consulting current Booking status or `last_operation`.
- A deterministic source-execution key and opaque Note marker support bounded reconciliation. An uncertain possible success with no indexed Note remains uncertain and never triggers a second create.
- CRM failure/uncertainty never rolls back, rejects, or falsely reverses a successful Google Calendar booking. No automatic delivery, backfill, exactly-once, or eventual-delivery guarantee is claimed.
- All external mutations have a bounded deadline/request count, safe typed result, idempotency key, and reconciliation/failure contract. Partial association repair cannot create a second Note.
- PII, raw provider payloads, and credentials do not appear in durable JSON, audit, logs, or public result models.
- Only the justified account-scoped Contact link table is added by Alembic; no generic provider framework, queue, worker, webhook, retry engine, or outbox processor is introduced.
- Live smoke remains separate from CI, uses the same-key account preflight and a dedicated allowlisted developer test account/database, synthetic run-owned records, and honest cleanup reporting.

## 18. Unresolved decisions and external verification gates

1. **Account binding contract:** The official public collection currently shows `/account-info/2026-03/details`, while the Fall 2026 Spotlight lists Account Info among APIs updated in 2026-09. The exact 2026-09 route and Service Key-compatible permission were not verified. Confirm the supported route, scope, and same-key portal-ID read in the dedicated test account; otherwise keep live writes disabled pending provisioning review.
2. **Account scope and key permission:** Verify Contacts read/write, Notes read/write, association permission, and account-preflight access with the actual account. If least privilege cannot support the complete workflow, do not broaden silently or change auth models.
3. **Phone search in selected portal:** Verify documented ordinary phone/mobilephone query formatting and candidate return behavior against the developer test account. Exact local E.164 verification and ambiguity rejection remain mandatory.
4. **Note marker search:** Verify that bounded Notes search can return recovery candidates for the opaque body marker using documented search behavior. If it cannot, or indexing is delayed, uncertain writes remain uncertain and must not be recreated.
5. **Account validation and permissions:** Inspect portal-specific required fields and association permissions before activation. Do not treat untested account behavior as verified.
6. **Service Key beta:** Service Keys remain public beta in the reviewed Fall 2026 source; HubSpot may change administration or capability.
7. **Synchronous latency and interruption:** An explicitly invoked Note call adds bounded latency; no measured latency SLO exists. If the caller stops before or after the CRM claim, the Note may remain absent or uncertain until a later explicit invocation.
8. **Staff edit race:** Read-before-PATCH and fill-blank rules protect common manual edits, but standard Contact properties do not provide the compare-and-swap guarantee required to eliminate a narrow concurrent edit race.
9. **No trusted production confirmation yet:** Phase 3 write paths remain internal/test-only until a later approved phase provides a real trusted confirmation mechanism. No dashboard, OAuth/operator authentication, or confirmation service is added here.
10. **No historical delivery:** Only an explicitly invoked successful source operation is considered; there is no automatic backfill or guarantee that every Phase 2 outcome reaches HubSpot.
11. **Portal reconfiguration:** Contact links are account-scoped. A new portal requires a separate reviewed migration/reconfiguration decision; a runtime setting change cannot reuse old links.

## Authoritative HubSpot source register

Official HubSpot Developer documentation/changelog, official HubSpot Postman workspace, and HubSpot Knowledge Base sources checked on 2026-10-25.

| Source | Date where shown | Use |
| --- | --- | --- |
| [Service Keys](https://developers.hubspot.com/changelog/service-keys) | Announced 2026-02-10 | System-to-system use, scope selection, no webhooks, key rotation |
| [Legacy private-app creation sunset](https://developers.hubspot.com/changelog/legacy-private-app-creation-sunset) | Announced 2026-08-27 | Account-age-dependent sunset dates |
| [Fall 2026 developer spotlight](https://developers.hubspot.com/changelog/fall-2026-spotlight) | Announced 2026-09-15 | 2026-09 release, Service Key beta, updated API list |
| [CRM API write validation enforcement](https://developers.hubspot.com/changelog/crm-api-write-validation-enforcement) | Announced 2026-08-11; effective 2026-09-08 | Account-configured validation and association permission failures |
| [Date-based API versioning](https://developers.hubspot.com/changelog/introducing-date-based-api-versioning) | Announced 2026-03-30 | Immutable date versions and support window |
| [2026-09 API overview](https://developers.hubspot.com/docs/api-reference/latest/overview) | Last modified 2026-09-09 | Versioned API root and route pattern |
| [Contacts guide](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/contacts/guide) | Last modified 2026-09-09 | Contact create/read/update/upsert and identity behavior |
| [CRM Search guide](https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm) | Last modified 2026-09-09 | Phone search, limits, indexing caveats |
| [Notes guide](https://developers.hubspot.com/docs/api-reference/latest/crm/activities/notes/guide) | Last modified 2026-09-09 | Note fields, search, create, association |
| [Associations guide](https://developers.hubspot.com/docs/api-reference/latest/crm/associations/associate-records/guide) | Current 2026-09 reference | Note-to-Contact association operations |
| [Scopes documentation](https://developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/scopes) | Last modified 2026-09-15 | Scope catalog and tier-dependent API access |
| [API usage guidelines](https://developers.hubspot.com/docs/developer-tooling/platform/usage-guidelines) | Last modified 2026-09-04 | Account limits and rate-limit handling |
| [Error handling](https://developers.hubspot.com/docs/api-reference/latest/error-handling) | Last modified 2026-09-09 | HTTP error contracts |
| [HubSpot Public API Workspace](https://www.postman.com/hubspot/hubspot-public-api-workspace/overview) | Workspace updated 2026-10-08 | Official API collections; current account details route evidence |
| [Retrieve account details](https://www.postman.com/hubspot/hubspot-public-api-workspace/request/nzfulc2/retrieve-account-details) | Current collection lists 2026-03 | Exact documented account portal-ID read; not inferred as 2026-09 |
| [Developer Platform Basics](https://developers.hubspot.com/developer-platform-basics) | Current page | Developer test accounts and periodic reset |
| [Create records](https://knowledge.hubspot.com/records/create-records-on-the-updated-index-page) | Last updated 2026-09-02 | Broad Contact/Deal manual record availability for comparison |
| [Log activities](https://knowledge.hubspot.com/records/manually-log-activities-on-records) | Last updated 2026-09-29 | Broad activity availability |
| [Leads API release](https://developers.hubspot.com/changelog/new-leads-api-released?hs_amp=true) | Announced 2024-08-20 | Published Sales Hub Professional/Enterprise Leads tier restriction |
| [Standard sandbox requirements](https://knowledge.hubspot.com/account-management/deploy-sandbox-changes-to-production) | Last updated 2026-06-12 | Standard sandbox subscription requirement |
| [HubSpot test-account guidance](https://developers.hubspot.com/blog/how-to-safely-experiment-with-ai-in-hubspot-before-you-touch-real-data) | Current developer guidance | Dedicated test-account practice |
| [OAuth token and retry guidance](https://developers.hubspot.com/blog/oauth-token-management-hubspot-integrations) | Updated 2026-01 | General retry/timeout guidance only; OAuth lifecycle does not apply |

Research confidence: Contacts and Notes 2026-09 routes, Contact email/phone behavior, Notes fields/associations, 2026-09 write-validation changes, Service Key system-to-system positioning, and test-account guidance are documented. Account-details routing at 2026-09, Service Key-compatible account-details permission, selectable scopes in the actual Service Key picker, portal-specific phone-search behavior, and account-specific validation remain unverified until a dedicated test account is provisioned. No HubSpot API was called in this task.
