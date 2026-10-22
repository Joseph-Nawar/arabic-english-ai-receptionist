# Phase 3 — HubSpot CRM Integration: Requirements and Architecture Design

**Status:** Design proposal for independent review. No Phase 3 production implementation is included.
**Research checked:** 2026-10-22.
**Target API version:** HubSpot CRM API 2026-09.
**Target branch:** phase/3-hubspot-crm-integration.

## 1. Product goals and user workflows

Phase 3 gives the single-business receptionist a bounded CRM capability. It should:

1. Resolve a local customer to one verified HubSpot Contact when evidence is sufficient.
2. Create a Contact only when a stable HubSpot identity is available and the customer has confirmed the identity data.
3. Fill safe blank Contact fields without overwriting staff-managed values.
4. Write one short, structured HubSpot Note for each successfully committed booking, reschedule, or cancellation.
5. Create an associated Deal only for an explicitly qualified quote or contract opportunity under an enabled business rule.
6. Record success, failure, or uncertainty locally so an explicit later invocation can reconcile safely.

Expected workflows:

- **Known customer:** load the verified local-to-HubSpot link, read that HubSpot Contact, and confirm the stored identity key still matches. A CRM read or permission failure does not stop the receptionist from helping the customer.
- **Unlinked customer with a phone:** search HubSpot using the local canonical E.164 number in HubSpot's supported phone-search form; retrieve candidate values and verify them locally. Link only one exact E.164 match.
- **New customer:** after a successful read proves there is no candidate, create only with a customer-confirmed email and an explicit application decision to save the Contact. A phone alone is not a safe create key.
- **Booking outcome:** after the Phase 2 booking state and Google Calendar result are committed, synchronously write an associated structured Note. The booking result remains successful even if this second operation fails or is uncertain.
- **Qualified opportunity:** an explicitly authorized application caller may create an associated Deal for a quote request or commercial contract only when a configured qualification rule, pipeline, and stage allow it. A normal booking is not an opportunity.

## 2. Scope, non-goals, and assumptions

### In scope

- One configured HubSpot portal for one configured business deployment.
- Service Key authentication provisioned outside the application.
- Contact read, deterministic resolution, cautious create, and fill-blank profile update.
- Notes for committed booking/customer-operation outcomes.
- Narrow, optional Deal creation for a qualified opportunity.
- Local Contact-to-HubSpot Contact linkage and local ToolExecution recovery state.
- Provider-free automated tests and a separately guarded live smoke design.

### Out of scope

- Multitenant SaaS, a general CRM synchronization engine, cross-provider identity framework, or provider registry.
- OAuth consent, Marketplace distribution, a project-based HubSpot app, HubSpot webhooks, contact merges, or bulk synchronization.
- Calendar availability or event ownership in HubSpot.
- Automatic Deal creation for every booking, Leads, Companies, custom HubSpot properties, transcripts, messaging, dashboards, queues, workers, Redis, or a retry scheduler.
- Historical backfill of bookings created before CRM is enabled.
- Production implementation in this design task.

### Assumptions

- Phase 0–2 remain complete and unchanged in authority: PostgreSQL owns local Contact identity, Conversation, Booking, ToolExecution, audit and idempotency; Google Calendar owns its external appointment state; HubSpot owns CRM Contact and opportunity lifecycle; application policy authorizes writes.
- There is one configured business per deployment, using the synthetic Riyadh HomeCare seed for the demo.
- Phase 1's phone rules remain authoritative. Missing or withheld caller identity creates a distinct unresolved local Contact; it is never resolved by name.
- The first live verification account will be a dedicated developer test account. No HubSpot credentials or live account access are available or authorized in this task.
- The HubSpot Service Key feature remains public beta as of the research date. The exact selectable scopes and account-specific write rules must be verified in that test portal before a later implementation enables writes.

## 3. Current HubSpot API research

Research was checked against HubSpot's official developer material on 2026-10-22. The reference pages use a /latest/ documentation alias, which at the time served the 2026-09 API reference. The application must pin the actual request paths below to /2026-09/; do not derive request paths by mechanically editing legacy URLs.

### Version and route pattern

HubSpot announced date-based API versions in March 2026. Versions use YYYY-MM naming, are immutable API snapshots, and have an 18-month support window. The Fall 2026 release identifies Projects 2026.09 as generally available and the latest released date-based version in the material reviewed. HubSpot's 2026-09 reference shows the API root https://api.hubapi.com and routes such as GET /crm/objects/2026-09/contacts. See [date-based API versioning](https://developers.hubspot.com/changelog/introducing-date-based-api-versioning), the [Fall 2026 developer spotlight](https://developers.hubspot.com/changelog/fall-2026-spotlight), and the [2026-09 API overview](https://developers.hubspot.com/docs/api-reference/latest/overview).

### Verified CRM routes proposed for Phase 3

| Operation | Documented 2026-09 route | Design use |
| --- | --- | --- |
| Read Contact by record ID | GET /crm/objects/2026-09/contacts/{recordId} | Verify an existing mapping and selected fields |
| Read Contact by email | GET /crm/objects/2026-09/contacts/{email}?idProperty=email | Exact lookup for a customer-confirmed email |
| Search Contacts | POST /crm/objects/2026-09/contacts/search | Search phone candidates; verify every result locally |
| Create Contact | POST /crm/objects/2026-09/contacts | Create only after safe resolution and confirmed email |
| Update Contact | PATCH /crm/objects/2026-09/contacts/{contactId} | Fill permitted blank fields only |
| Search Notes | POST /crm/objects/2026-09/notes/search | Reconcile a Note create by its local-operation marker |
| Create Note | POST /crm/objects/2026-09/notes | Create a short Note with HubSpot-defined Note-to-Contact association type ID 202 in the same request |
| Search Deals | POST /crm/objects/2026-09/deals/search | Reconcile a Deal create by its local-operation marker |
| Create Deal | POST /crm/objects/2026-09/deals | Create a qualified Deal with HubSpot-defined Deal-to-Contact association type ID 3 in the same request |
| Associate an existing Note | PUT /crm/objects/2026-09/notes/{noteId}/associations/contact/{contactId}/202 | Repair a verified missing Note-to-Contact association after reading the created Note |

The [Contacts API guide](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/contacts/guide), [CRM Search API guide](https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm), [Notes API guide](https://developers.hubspot.com/docs/api-reference/latest/crm/activities/notes/guide), [Deals API guide](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/deals/guide), and [associations guide](https://developers.hubspot.com/docs/api-reference/latest/crm/associations/associate-records/guide) document these routes. Contacts may be created with associations; Notes require hs_timestamp and accept hs_note_body, and can be created with associations; Deals require dealname and dealstage and may be created with associations. The current association reference gives default Note-to-Contact ID 202 and Deal-to-Contact ID 3. The pipeline internal ID is required when the account has multiple pipelines.

Contacts API creation requires at least one of email, firstname, or lastname. HubSpot recommends email as its primary unique identifier. Contact upsert supports email or a custom unique property, and email-based batch upsert does not support partial upsert. No custom unique Contact property is assumed or proposed. Phone and mobilephone are searchable fields but are not documented as unique identifiers. Therefore this design does not use Contacts batch upsert and never treats a phone match as unique without read-and-verify.

HubSpot's search guide documents that phone search uses calculated standardized properties whose names begin with hs_searchable_calculated_ and compares area code/local number, excluding country code. The guide does not identify a stable calculated property name for direct use. The implementation should query the documented phone/mobilephone fields using a national significant number without the country calling code, then verify returned phone and mobilephone values by parsing them against the configured default region and comparing canonical E.164 locally. Do not hardcode an undocumented hs_searchable_calculated_* property. Verify actual search behavior in the developer test account before enabling writes.

Search is limited to five requests per second per account, at most 200 results per page and 10,000 results per query. Recently created/updated records may take moments to appear, and archived records do not appear in search results. These behaviors are part of the reconciliation contract, not reasons to retry a create blindly. See the [search limits and phone behavior](https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm).

### Activities, Leads, Deals, and account tiers

A Note is the selected activity type because it records an application outcome without claiming that HubSpot owns the actual Calendar appointment. The Notes API supports creating an associated Note in one request. The 2026-09 spotlight lists Contacts, Notes, Deals, Leads, Associations, and Appointments among updated CRM APIs. HubSpot Appointment records are not selected: they would duplicate Calendar event state, and Phase 3 does not need a second appointment object or its unverified account-tier/scope requirements. The Notes API requires hs_timestamp; hs_note_body is capped at 65,536 characters. Phase 3 writes far less than this limit and includes no raw transcript.

Two approaches were compared:

- **A. Contacts plus associated Notes:** supports matching and lifecycle profile improvements, with a simple activity timeline and broad account utility. It does not model a revenue opportunity.
- **B. Contacts plus Notes plus Deals/Leads:** also represents an explicit qualified sales opportunity. Deals are the clearer transaction object, require a configured stage (and pipeline where relevant), and can be associated with a Contact. HubSpot's current help page lists manual deal creation for all products and plans, with some related capabilities requiring higher tiers. The Leads API announcement limits Leads API availability to Sales Hub Professional or Enterprise. The exact CRM API scope and portal permissions still need test-account verification.

**Recommendation:** Phase 3 supports A as the default and B only through the narrow, explicitly qualified Deal operation described below. Do not use Leads. Do not create a Deal for a booking. HubSpot's current Knowledge Base marks Contact creation, activity logging, and Deal creation available across products/plans, although API scopes and account-specific write rules still require portal verification. The last official Leads API availability notice I found limits that API to Sales Hub Professional or Enterprise; the 2026-09 spotlight lists Leads as an updated API but does not state that this tier restriction changed. Keep Leads out of scope unless a later review verifies current broader availability. Deals are optional and unavailable until the account's Service Key, pipeline/stage, and validation rules pass preflight. Sources: [Create contacts](https://knowledge.hubspot.com/records/create-records-on-the-updated-index-page), [Create or log activities](https://knowledge.hubspot.com/records/manually-log-activities-on-records), [Create deals](https://knowledge.hubspot.com/records/create-records-on-the-updated-index-page), [Leads API availability announcement](https://developers.hubspot.com/changelog/new-leads-api-released?hs_amp=true), and [Fall 2026 Spotlight](https://developers.hubspot.com/changelog/fall-2026-spotlight).

HubSpot's Knowledge Base marks Contact creation and logging Notes on records as available across all products and plans. That establishes broad object availability, not a guarantee that every portal has the required API scope, Service Key capability, or write permission. Those still require test-portal verification. Sources: [Create contacts](https://knowledge.hubspot.com/records/create-records-on-the-updated-index-page), [Create or log activities](https://knowledge.hubspot.com/records/manually-log-activities-on-records), and [Create deals](https://knowledge.hubspot.com/records/create-records-on-the-updated-index-page).

The four Riyadh HomeCare seed services currently use NOT_PUBLISHED pricing; none is quote_required. That seed therefore supplies no automatic Deal qualification rule. A Deal can be created only from a separately explicit trusted operator/application qualification, not by inferring intent from a standard booking.

### Authentication, validation, rate limits, and test accounts

HubSpot announced account-level Service Keys in public beta on 2026-02-10 for system-to-system integrations. HubSpot recommends them where OAuth and webhooks are unnecessary. Service Keys do not support webhooks, require Developer Tools access to create, can only grant scopes the creating user already has, expose usage activity, and support key rotation with a seven-day grace period. Fall 2026 still lists Service Keys as public beta. Legacy private-app creation was disabled for new accounts created on/after 2026-09-28 and is scheduled to be disabled for older accounts on 2026-10-26. Design conclusion: a Service Key appears sufficient for the proposed outbound Contacts, Notes, Deals, and association calls because these are account-level CRM API operations and need no callbacks. This remains conditional on verifying precise Notes/association scopes and portal permissions. Provision one key out of band for the configured portal. Sources: [Service Keys announcement](https://developers.hubspot.com/changelog/service-keys), [Fall 2026 Spotlight](https://developers.hubspot.com/changelog/fall-2026-spotlight), and [legacy private-app creation sunset](https://developers.hubspot.com/changelog/legacy-private-app-creation-sunset).

Proposed minimal scope set, subject to verification in the Service Key picker and against the exact endpoint reference before activation:

- Base Contact and Note workflow: crm.objects.contacts.read, crm.objects.contacts.write, crm.objects.notes.read, crm.objects.notes.write.
- Optional qualified Deal capability only: add crm.objects.deals.read and crm.objects.deals.write.
- Do not request schema/property write, sensitive-data, marketing, webhook, or unrelated CRM scopes.
- The API scope table is account-tier sensitive; Notes/Deal scope availability and association-write authorization must be checked in the dedicated test portal. If the picker or portal requires broader privileges than this set, disable the affected capability and surface the unresolved permission requirement for review; do not silently add OAuth or broader scopes.

Starting with 2026-09, HubSpot applies admin-configured conditional required properties, required create-form properties, and applicable association permissions to API writes. The API can reject an otherwise valid application request with HTTP 400 and validation details. Treat those as safe business/configuration failures, not retryable writes. Source: [CRM API write-validation enforcement](https://developers.hubspot.com/changelog/crm-api-write-validation-enforcement).

General API limits depend on account subscription. Search has its stricter five-requests-per-second-per-account limit. HubSpot returns 429 when limits are exceeded; HubSpot developer retry guidance says to respect Retry-After when supplied and use bounded backoff, while network-timeout retries must account for idempotency. This foreground workflow caps that guidance at one delayed retry when the response proves no write occurred and the delay fits the deadline. CRM API error responses may include correlation IDs and structured details, but those provider values are not returned to the customer or persisted verbatim. Sources: [API usage guidelines and limits](https://developers.hubspot.com/docs/developer-tooling/platform/usage-guidelines), [error handling](https://developers.hubspot.com/docs/api-reference/latest/error-handling), and [HubSpot's OAuth token management and retry guidance](https://developers.hubspot.com/blog/oauth-token-management-hubspot-integrations). The latter discusses OAuth token management; only its general 429/timeout advice is applied here, not its OAuth flow.

A free HubSpot developer account includes test accounts, sample Contact/Company/Deal data and public API access; test accounts reset periodically. HubSpot developer guidance says a developer account can create up to 10 test accounts with a 90-day Enterprise trial. HubSpot describes its standard sandbox as an Enterprise subscription feature. Prefer a dedicated developer test account for this project. The exact tier and feature state of the eventual test portal cannot be asserted until the account is selected and inspected. Sources: [Developer Platform Basics](https://developers.hubspot.com/developer-platform-basics), [standard sandbox requirements](https://knowledge.hubspot.com/account-management/deploy-sandbox-changes-to-production), and [HubSpot's test-account guidance](https://developers.hubspot.com/blog/how-to-safely-experiment-with-ai-in-hubspot-before-you-touch-real-data).

## 4. Authentication and configuration

Use exactly one account-level Service Key per deployment, supplied out of band by an account administrator. No in-app key creation, OAuth consent, Marketplace distribution, or account discovery flow is proposed.

Later implementation configuration proposal:

| Setting | Type and behavior |
| --- | --- |
| RECEPTIONIST_HUBSPOT_SERVICE_KEY | Optional Pydantic SecretStr; never exposed by repr, safe_summary, logs, errors, ToolExecution, or AuditEvent |
| RECEPTIONIST_HUBSPOT_PORTAL_ID | Required when a Service Key is configured; validated opaque account identifier for the mapping namespace, not a secret |
| RECEPTIONIST_HUBSPOT_REQUEST_TIMEOUT_SECONDS | Default at most 10 seconds and hard-bounded; finite connect/read timeout |
| RECEPTIONIST_HUBSPOT_DEALS_ENABLED | False by default; only true after account validation and explicit business qualification rule |
| RECEPTIONIST_HUBSPOT_DEAL_PIPELINE_ID | Required when Deals are enabled if multiple pipelines exist; opaque configured internal ID |
| RECEPTIONIST_HUBSPOT_DEAL_STAGE_ID | Required when Deals are enabled; validated configured internal stage ID |

Missing or blank credentials disable CRM operations safely; application startup and booking remain available. The service key is sent only as a Bearer credential over TLS to api.hubapi.com. It is never included in durable JSON or provider/application logs. Credential rotation uses HubSpot's seven-day grace period: provision a replacement in the portal, update the deployment secret, verify safe connectivity, then revoke the old key after the grace period. No secret value is printed or returned during rotation.

Normal test/CI configuration must disable the HubSpot transport regardless of whether a key is present. A provider request from a test/CI environment is a local configuration error, not a fallback to live HubSpot.

## 5. CRM objects and exact ownership model

| Data or decision | Authority |
| --- | --- |
| Local Contact ID, E.164 phone normalization, Conversation, Booking, pending action, ToolExecution, idempotency, local-to-HubSpot link, sanitized audit | PostgreSQL |
| HubSpot Contact profile and lifecycle fields, CRM Deal lifecycle and operator changes, HubSpot Notes | HubSpot |
| Actual appointment availability and external Calendar event | Google Calendar |
| Whether an operation is allowed, confirmation evidence, identity conflict handling, CRM field allowlist, write decision, booking-vs-CRM result semantics | Application policy |

The HubSpot Note is a CRM activity describing an already-completed local operation. It does not create, cancel, reschedule, or verify a Google Calendar event. Deal stages and Contact lifecycle fields remain HubSpot-owned; Phase 3 never sets them. No HubSpot property becomes an alternate local identity source.

## 6. Public application operations and typed contracts

These are application-level operations and result contracts, not generic CRM actions, HTTP routes, or LLM-exposed arbitrary tools. Requests use strict typed models with unknown fields forbidden. No public operation accepts arbitrary HubSpot property maps, object types, URLs, association IDs, or provider payloads.

All requests include conversation_id, local contact_id, and a bounded idempotency_key except where a stable booking source key is derived by the application. Requests also include an invocation_mode enum: initial, retry_known_no_write, or reconcile_ambiguous. The application validates that the selected mode is permitted by the prior ToolExecution state; ambiguous writes allow reconciliation only, and a succeeded key always replays. All output models include operation, outcome, replayed, safe_error_code (optional), retry_action (none, retry_same_key, reconcile_same_key, operator_review), and provider_record_id (optional opaque ID). They exclude Contact name, phone, email, raw HubSpot properties, request/response bodies, and credentials.

### resolve_hubspot_contact

Request: conversation_id, contact_id, idempotency_key, and an application-verified email_identity_confirmed boolean. The boolean is set only when the current trusted application flow has confirmed the exact stored local email; an LLM cannot assert it. The application loads local identity fields from PostgreSQL.

Result outcomes: linked, found_and_linked, not_found, ambiguous, unresolved_identity, stale_mapping, identity_conflict, provider_unavailable, permission_denied, or not_configured. It returns at most the opaque HubSpot Contact ID and a safe error code. A read-only resolution may write the CRM link and therefore has an idempotency claim.

### sync_hubspot_contact_profile

Request: conversation_id, contact_id, idempotency_key, a finite set from {email, phone}, and application-verified confirmation flags for each selected field. The exact values come from the local Contact row and are not copied into ToolExecution arguments. Store the Contact updated_at snapshot in sanitized arguments so a retry detects changed local source data.

Result outcomes: created, updated, unchanged, skipped, conflict, uncertain, failed. For creation, the request requires a customer-confirmed email. For updates, every selected field is checked before any PATCH; if one field conflicts, do not patch any selected field in that request.

### record_booking_outcome

Request: conversation_id, contact_id, booking_id, source_booking_tool_execution_id, and a finite operation enum: booking_confirmed, booking_rescheduled, or booking_cancelled. The application verifies the Booking belongs to the Contact, the source ToolExecution belongs to the Conversation and succeeded, and its successful operation agrees with the persisted Booking status and booking_data.last_operation.

Result outcomes: written, replayed, skipped, unresolved_contact, uncertain, failed, crm_state_not_persisted. The operation uses a deterministic key derived from the source booking ToolExecution ID, so replayed booking confirmations do not create repeated Notes. It includes no arbitrary narrative. After the local booking transaction commits, the current request creates and commits the CRM ToolExecution claim in its own local transaction, then attempts CRM delivery synchronously. If the CRM claim cannot be persisted, no HubSpot call is made and the booking result remains successful. A later explicit invocation derives the same key from the source booking ToolExecution and can create/reconcile that claim. No publisher, queue, worker, or scheduled retry is introduced.

### create_qualified_deal

Request: conversation_id, contact_id, source_operation_id, idempotency_key, and a finite qualification enum (quote_request or commercial_contract), plus a service code and optional explicitly confirmed expected amount in the configured business currency. This operation is callable only by a trusted application/operator path after a documented business qualification check; it is not an LLM tool. Deal creation is rejected unless Deals are enabled and the configured pipeline/stage pass preflight. A regular booking is not evidence of qualification.

Result outcomes: created, replayed, rejected_unqualified, disabled, contact_unresolved, uncertain, failed. The response contains only an opaque Deal ID and safe result codes. The application does not update a Deal after creation.

## 7. Contact identity and mapping strategy

### Durable mapping

A small Contact-to-HubSpot link table is justified. Local Contact currently has no provider ID. A durable, account-scoped link prevents repeated fuzzy search and supports verification across process restarts and credential rotation. It is not a general identity framework.

Proposed future Alembic table:

- contact_id: primary key and restrictive foreign key to local Contact.
- hubspot_portal_id: configured portal/account ID.
- hubspot_contact_id: opaque HubSpot record ID.
- linked_by: finite enum phone_e164 or confirmed_email.
- state: active or stale.
- verified_at, created_at, updated_at: UTC timestamps.
- Unique constraint on (hubspot_portal_id, hubspot_contact_id).
- No PII, provider payload, secret, or arbitrary JSON column.

The configured portal ID must match the stored link namespace before a mapped read or write. Changing portals while a mapping exists fails closed and requires a reviewed reconfiguration; it must never silently point a local Contact at a record from another account. This table is the only proposed CRM model. No migration is created in this design task.

### Deterministic resolution order

1. **Existing active link:** GET the mapped Contact ID with only email, phone, and mobilephone. Verify the mapping belongs to the configured portal. If the response says the record is archived, treat it as stale and do not read business fields or write to it. For a phone link, at least one retrieved phone field must normalize exactly to the local Contact.phone_e164. For an email link, the retrieved primary email must equal the explicitly confirmed local email under a documented case-insensitive comparison. If a local E.164 also exists, a conflicting provider phone or customer-confirmed email blocks the link. Never overwrite the identity to make verification pass.
2. **Mapped ID returns 404:** mark the mapping stale; HubSpot's response does not prove whether the record was archived, deleted, or merged. Search again using the same bounded identity policy. For a phone-based stale link, relink only when exactly one candidate matches E.164 and the customer confirms any local email currently available; without a confirmed email, require operator review. For an email-based stale link, relink only by that same confirmed exact email. This can recover a merged survivor without treating an archived record as a clean no-match. If there is no match, more than one candidate, or conflicting values, leave the mapping stale and do not create a replacement Contact in that invocation.
3. **No link with E.164:** search phone/mobilephone using the national number without country code; fetch candidate values; normalize each value with libphonenumber using the configured default region; compare exact E.164. Exactly one distinct verified Contact is linked. Zero candidates returns not_found. More than one distinct verified Contact returns ambiguous. Same-name candidates do not affect the decision.
4. **Confirmed email:** if E.164 search returns no verified match and the current application has confirmed the locally stored email, read by email using HubSpot's documented idProperty=email route. Link only if exactly one record returns and its other known identity fields do not conflict. If phone search and email lookup resolve to different HubSpot IDs, return identity_conflict.
5. **No phone / withheld phone:** preserve Phase 1's unresolved local Contact. Do not search by name. If the customer explicitly confirms an email, the email route may resolve a HubSpot Contact or permit a separate email-keyed create; store linked_by=confirmed_email without merging or changing local Contacts. Without that confirmation, return unresolved_identity and perform no CRM write.
6. **No match:** return not_found. Contact creation is a separate, confirmed operation requiring a successful provider read and confirmed email; never auto-create from name or phone alone.
7. **Provider unavailable or 401/403:** return provider_unavailable or permission_denied, respectively. Do not create, link by guess, or expose candidate data.
8. **Existing mapped record still exists but has changed identity:** mark stale and return identity_conflict. Do not automatically relink in the same invocation, even if another candidate is found; require customer confirmation or operator review. Do not relink merely because name is similar.

HubSpot phone fields may be duplicated across records and phone search may omit country code. An exactly unique verified result is the maximum automatic match. A changed/reassigned phone number cannot prove a person's identity beyond the application's Phase 1 identity rule; conflicts are therefore surfaced safely rather than hidden through fuzzy matching. No Contacts merge endpoint is called.

## 8. CRM field ownership and data mapping

The Phase 3 Contact field allowlist is deliberately small:

| HubSpot property | Local source | Write rule |
| --- | --- | --- |
| email | Contact.email | Include only when customer-confirmed in the current operation; create identity requires it; on an existing Contact fill only if blank or already equal |
| phone | Contact.phone_e164 | Include only when customer-confirmed for CRM use; write canonical E.164 only if HubSpot value is blank or normalizes to the same E.164 |
| mobilephone | None in Phase 3 | Read only as an identity-search candidate; never write |
| firstname, lastname, lifecyclestage, hs_lead_status, hubspot_owner_id, company, address, marketing/subscription fields | None | Never write |

Do not split an Arabic or English display name into first and last names. Do not send display_name in Phase 3. A create with confirmed email may omit a name because HubSpot permits email as the required identity property. A local blank, withheld, unconfirmed, or invalid value is omitted, not sent as an empty string and never used to clear HubSpot.

For an existing Contact, read the current HubSpot fields immediately before any PATCH. If a selected nonblank provider field differs from the confirmed local value, return conflict with only safe field names; do not overwrite the value or apply a partial subset of requested fields. This preserves staff edits. No automatic lifecycle, owner, company, address, or marketing updates are permitted.

HubSpot 2026-09 write validation can reject a write because the account administrator configured extra requirements. Return a generic provider_validation_rejected code, retain no provider payload, and ask an operator to correct the account rule or data mapping. Do not retry the same invalid body automatically.

## 9. Booking outcome and qualified Deal strategy

Every successful create, reschedule, or cancellation may result in one short associated Note for the already-verified Contact. Only a successfully committed local Booking and successful source booking ToolExecution qualify. The Note describes the operation that just succeeded; it never becomes a Calendar event.

Note body fields are fixed: operation kind, outcome, local booking UUID, service code, relevant UTC appointment time (when applicable), and opaque local source-operation UUID marker. For cancellation, omit the appointment time unless needed to identify the cancelled slot. Generate the body from validated local data and fixed labels. Do not include transcripts, free-form summaries, name, phone, email, address, payment data, or customer requirements. Set required hs_timestamp from the operation's committed UTC time. Create the Note and associate it with the verified Contact in one request. Do not associate Companies or unrelated records.

A booking, a reschedule, and a cancellation are each meaningful separate customer operations, so each has its own source ToolExecution and marker. Repeating the same source operation replays the same CRM ToolExecution; it does not write another Note. The application-level booking response keeps the Phase 2 booking result unchanged and adds a separate CRM result with written, failed, uncertain, skipped, or not_configured state. CRM errors never populate the booking error field or tell the customer that a successful Calendar booking failed. The operation marker is included in hs_note_body and is searchable through the Notes search endpoint. Recovery verifies the marker and Contact association before treating a result as complete.

A Deal represents a transaction, not an appointment. The only automatic Deal candidate is a separately qualified quote request or commercial contract captured by a trusted application/operator path. Phase 3 does not introduce a new sales qualification model or arbitrary CRM action. Since the current demo catalog has no quote_required service, it creates no Deal based solely on catalog settings. If a later approved business rule qualifies an opportunity, create a Deal with configured dealname, pipeline, stage, optional explicitly confirmed amount, and association to the verified Contact. Put the opaque local source-operation marker in the Deal description so an uncertain create can be reconciled. Never set dealstage after creation or assign an owner automatically.

Do not create a separate HubSpot appointment or meeting object in this phase. Calendar retains the external event. The associated Note is the bounded CRM activity record.

## 10. Idempotency and durable recovery

Reuse ToolExecution and AuditEvent. Do not add an operation/job/outbox table for CRM delivery. CRM ToolExecution arguments/results hold only safe identifiers, approved field names, a Contact updated_at snapshot, operation enum, safe provider outcome, and opaque provider record ID. The note/deal operation marker is a local ToolExecution UUID, not customer data.

- **Claim before write:** persist the CRM ToolExecution row with status started and its unique idempotency key before the external mutation. For a booking Note, create that claim only after the booking transaction has committed. This keeps local booking success independent of CRM claim persistence. If CRM is not configured, create no pending CRM intent and report not_configured; there is no automatic historical backfill if CRM is enabled later. A later explicit same-key invocation may resolve a specific earlier booking operation.
- **Synchronous bounded attempt:** make the HubSpot call after the database commit and return separate booking and CRM outcomes. A HubSpot error never changes the successful Booking or booking ToolExecution.
- **Known no-write failure:** a later explicit invocation with the same key may repeat the operation only if the prior result proves the provider rejected it before mutation or no mutation was attempted, such as a transient outage, permission issue after repair, or unresolved identity after confirmation. A 400 validation error is not retried with the same invalid body. The CRM-specific application policy may retry an eligible failed ToolExecution after the caller explicitly requests retry; it does not change Phase 2 replay behavior.
- **Ambiguous write:** timeout, connection loss after sending, 5xx, or a local persistence failure after a possible provider success leaves the CRM ToolExecution started with a safe uncertain marker. A retry with the same key performs read-only reconciliation first. It never repeats a non-idempotent create unless provider state has been conclusively read and the original request is known not to have succeeded.
- **Contact create recovery:** use exact GET-by-email, then verify email and any known phone. A duplicate/conflict response also triggers this read. If no Contact is visible, remain uncertain; HubSpot's search index may lag. Never POST the same create blindly after a timeout.
- **Note/Deal create recovery:** search for the exact opaque marker in Note body or Deal description; read returned candidates and verify marker, object type, and expected Contact association. Exactly one verified object completes recovery. Multiple results are a duplicate conflict requiring review. No visible result remains uncertain; search lag does not authorize a second create.
- **Association recovery:** create a Note or Deal with its Contact association in the same request. If the object exists but the association is absent, retrieve and validate both record IDs and perform at most the explicit idempotent association operation; then read back and verify. Do not create another object. If association permission fails, retain the object ID and report association failure/uncertainty.
- **Same-customer concurrency:** serialize CRM operations by locking the local Contact row for the bounded CRM operation; hold the lock for no longer than the provider deadline. Before a new operation proceeds, inspect for another started CRM ToolExecution for that local Contact through its Conversation. A previous uncertain operation blocks new CRM mutations until same-key reconciliation or operator review. Database uniqueness protects local idempotency keys. This does not serialize independent changes made by HubSpot staff.
- **Local source changed:** compare Contact.updated_at with the snapshot on a profile retry. If local source fields changed after a started operation, do not retry using different values under the same key; report uncertain/operator_review. Provider identity reads remain independent of source-value updates.
- **External staff edits:** never write a stale read. Re-read before PATCH, perform no update when a selected value conflicts, and rely on HubSpot's own CRM validation. Since the API path does not provide an atomic compare-and-swap for these standard properties, a narrow race remains between read and PATCH; it is a documented limitation and such operations use only fill-blank fields.
- **Audit:** append sanitized events for attempted/finished/reconciled operations and mapping relinks. Store operation, local IDs, outcome/error code, and opaque HubSpot IDs only. Never store a raw provider request or response.

No background delivery is proposed. If the request process stops after the local booking commit but before the CRM claim is persisted, a later same-key invocation derives the key from the successful source booking ToolExecution and creates the claim. If it stops after the claim is persisted, that started CRM ToolExecution is reconciled by the same key. If no later invocation occurs, the Note may remain absent; operational UI/retry scheduling is a later approved capability, not part of this phase.

## 11. Provider boundary and HTTP behavior

Later implementation should use one concrete module, src/receptionist/integrations/hubspot.py, because the first actual HubSpot provider implementation requires a bounded HTTP boundary. Keep application policy and public typed request/result models outside it. Do not introduce a generic provider interface, protocol, registry, repository, SDK, or adapter framework.

The boundary owns:

- Bearer Service Key header, fixed api.hubapi.com root, TLS, 2026-09 paths, finite connect/read timeout, and small response-size limit.
- Allowlisted request fields and association types only.
- Parsing only requested Contact/Note/Deal properties into narrow provider snapshots.
- Provider error-to-safe-error mapping. Raw response bodies, headers, correlation details, and credentials remain inside the boundary.
- Request call budget: Contact search requests only the identity properties needed and uses one result page; if HubSpot indicates another page, uniqueness is unproven and the result is ambiguous. A bounded Contact resolution uses at most four search/read calls, one mutation per attempt, plus one read-back for uncertain or association verification. No list-all, paging through an account, bulk writes, or arbitrary URL.

HTTP outcome policy:

| Response or condition | Application behavior |
| --- | --- |
| 2xx read/write | Validate response fields and expected association; persist only safe IDs/state |
| 400 validation/property error | No retry; safe provider_validation_rejected; do not expose provider text |
| 401 | Mark credential/auth failure, no retry loop; booking unaffected |
| 403 | Mark permission/tier/scope failure, no retry loop; booking unaffected |
| 404 mapped Contact | Mark stale, then perform only the bounded deterministic lookup policy; no create in the same stale-map resolution |
| 404 Note/Deal during reconciliation | Continue bounded search by marker; otherwise remain uncertain |
| 409 or duplicate/conflict response | Read by exact email or marker and reconcile; never create again until resolved |
| 429 | Respect Retry-After when present; at most one retry when the response definitively rejected the request and delay fits the remaining deadline; otherwise fail safely |
| 5xx, timeout, connection loss | Read operations may be retried once within deadline. Writes become uncertain and require same-key read reconciliation |
| Response received, local DB persistence fails | Keep the precommitted started ToolExecution; retry same key in read-only reconciliation before any mutation |
| Partial object/association success | Verify object and association IDs, repair only the missing association after exact ownership checks, never recreate the object |

There is no global provider retry middleware. One CRM operation has a deadline, bounded request count, and explicit retry action in its result. The Calendar booking result remains successful independently of this table.

## 12. Schema changes

No production schema change is made in this design task. The only justified future schema addition is the small crm_contact_link table defined in section 7. It is added through an Alembic migration with a verified downgrade. Existing Contact.updated_at, ToolExecution idempotency/status/sanitized JSON, and AuditEvent are sufficient; no CRM payload table, deal/note link table, generic provider ID table, outbox publisher state, or queue schema is proposed.

## 13. Privacy and security invariants

- Legitimate CRM PII is limited to the already-local Contact.email and Contact.phone_e164 when a customer confirms the selected value for CRM use. Name, address, transcript, conversation summary, requirements, payment information, and marketing fields are excluded.
- HubSpot receives only the allowlisted Contact email/phone, fixed structured Notes, and (for an explicitly qualified Deal) configured service code, explicit expected amount, and opaque local-operation marker.
- Store HubSpot IDs and link metadata locally. Do not store raw HubSpot Contact payloads, copied lifecycle values, auth headers, Service Keys, request/response bodies, or unfiltered error text.
- ToolExecution sanitized_arguments/result and AuditEvent sanitized_metadata may contain local UUIDs, operation names/enums, selected field names, safe outcomes, and opaque HubSpot IDs only. No email, phone, name, body, key, or raw provider response.
- Never reveal whether a particular HubSpot Contact exists or return candidate details to a caller. Public messaging is generic; identity failures route to a safe follow-up or human review.
- Every association target must be resolved from a local Contact link and freshly verified for the configured portal and identity. Never accept arbitrary provider record IDs from an LLM or client.
- Validate all IDs, enum values, field choices, timestamps, amounts, and service codes at the application boundary. The provider module serializes only the narrow schemas.
- SecretStr protection follows the existing Settings pattern. safe_summary returns only crm_configured and the bounded timeout, not the portal ID or secret. Missing credentials skip CRM and do not break startup or Calendar booking.
- Service Key rotation must not reveal either key; logs and diagnostics report only configured/rotation state. Revocation or insufficient scopes produces a safe permission outcome.
- No webhooks are used or required. No cross-Contact data is disclosed by reads, error messages, duplicate outcomes, or CRM search.

## 14. Testing strategy and guarded live smoke

Normal automated tests and CI stay provider-free, even when environment variables happen to contain credentials.

- Unit tests use a mock HTTP transport to assert exact 2026-09 routes, Bearer header isolation, request allowlists, response parsing, status mapping, bounded calls, and no network fallback.
- A deterministic local HubSpot HTTP test double may cover provider response shapes without any external call.
- PostgreSQL integration tests cover link uniqueness, account namespace mismatch, E.164 exact-match/ambiguity, stale mapping and relink, concurrent same-Contact operations, duplicate idempotency keys, started-operation reconciliation, local Contact.updated_at changes, and transaction failures.
- Failure/privacy tests cover 400/401/403/404/409/429/5xx, timeouts before and after provider acceptance, provider success followed by local persistence failure, partial association, operator edits, missing credentials, and assertions that no key/phone/email/name/provider body leaks to logs or JSON.
- Migration tests verify upgrade and downgrade of the link table. Existing Phase 2 booking tests remain intact and must continue proving Calendar state is authoritative and CRM cannot change a committed booking result.
- CI must refuse CRM HTTP calls in test/ci environments even if a real Service Key is accidentally present.

A future, separately invoked live smoke may use only a dedicated HubSpot developer test account. It must require all of: explicit RECEPTIONIST_HUBSPOT_LIVE_SMOKE=1; app_env=test; a separately provisioned database named receptionist_hubspot_smoke (never the development or production database); exact configured HubSpot portal ID matching a reviewed test-portal allowlist; and credentials whose key is provisioned in that test portal. The script must verify the target portal before creating anything, use synthetic run-unique Contact data, attach a run marker to each record, act only on exact IDs created by that run, verify Contact/Note/association and optional Deal behavior, and delete only records that still match the run marker and portal. Report cleanup success, cleanup failure, or partial completion separately. If portal verification, ownership marker, or cleanup guard fails, stop further writes. Never run a live smoke in standard CI. A HubSpot test account resets periodically; smoke must not depend on seeded sample-record identity. No live call or credential is authorized or used in this task.

## 15. Failure matrix

| Condition | Contact resolution | Contact/profile write | Booking outcome Note / qualified Deal | Durable state and retry |
| --- | --- | --- | --- | --- |
| Existing verified mapping | Return linked | Fill blank fields if confirmed | Write only to freshly verified linked Contact | Persist link check and ToolExecution outcome |
| Exactly one E.164 verified candidate | Link one Contact | Continue only with explicit confirmed values | Associate only to verified link | Unique local link; safe audit |
| No match, provider read succeeded | not_found | Create only with confirmed email and explicit save decision | No Note until Contact resolution succeeds; Deal rejected without Contact | Known no-write; can resolve/create explicitly |
| Multiple phone/email candidates | ambiguous | No create/update | No Note/Deal | Operator review; no guessing |
| Missing/withheld phone | unresolved_identity; optional confirmed-email route | No name/phone-only create | Skip CRM unless Contact is safely email-linked | Explicit same-key retry only after new confirmation |
| Stale mapping / GET 404 | Re-search exact identity; relink only one verified candidate | No create in stale-map invocation | No write until re-linked and read back | Mark stale; no mutation until resolved |
| Mapped record exists but identity changed | identity_conflict | No write | No write | Operator review |
| Provider read outage | provider_unavailable | No create; no update | No Note/Deal | Known no-write; explicit retry same key |
| 401/403 or missing scope | permission_denied | No mutation | No mutation | Safe error; administrator fixes auth/scope |
| 400 account validation | Not applicable to read | No retry with same invalid body | No retry with same invalid body | failed with safe validation code |
| 409/duplicate | Read by exact email/marker | Verify existing record; never repeat create | Search marker and verify association | Reconcile; ambiguous remains uncertain |
| 429 | Respect Retry-After within deadline | Retry at most once only for definitive rejection | Same bounded retry policy | Otherwise safe failure |
| 5xx/timeout/connection loss on read | Bounded single read retry | No write if lookup unresolved | No create retry | provider_unavailable or uncertain |
| 5xx/timeout/connection loss on write | N/A | Started; exact email read only | Started; search marker, verify association | Same-key reconciliation only; never blind create |
| Provider success, local persistence failure | N/A | GET by confirmed email, then persist link | Search marker/ID, then persist outcome | Started remains durable |
| Partial association | N/A | Not applicable | Read object and association; repair only verified missing link | Never create a second object |
| Concurrent local CRM operations | Serialize per local Contact | Block behind another started operation | Block behind another started operation | Resolve older same-key operation first |
| Staff changes provider field | Re-read and verify | Conflict if nonblank value differs | Never alter staff-edited Contact/Deal fields | No overwrite; report safe field name |
| Staff changes/deletes reconciliation marker | Mapping unaffected | N/A | Could prevent exact recovery | Keep uncertain and require operator review; no duplicate |

## 16. Later implementation milestone breakdown

This is a high-level sequence only, not the task-by-task implementation plan. The plan is intentionally deferred until independent design review approves this specification.

1. Implement the scoped CRM settings, Service Key configuration behavior, HTTP boundary, typed errors, and provider-free transport tests.
2. Add the Alembic Contact-link table and implement deterministic Contact resolution/profile operations with PostgreSQL concurrency and recovery tests.
3. Add the foreground post-commit booking outcome ToolExecution hook and associated Notes with idempotent recovery; verify Phase 2 Calendar invariants remain unchanged.
4. Add the optional trusted Deal operation only after the account's scope, tier, validation, pipeline/stage, and qualification rule have been verified.
5. Complete provider-free CI, migration/downgrade and privacy checks; document and separately exercise the guarded live smoke in a dedicated test account if credentials are later supplied.

## 17. Acceptance criteria for a later implementation

- Service Key authentication is used for one configured portal; missing credentials are safe and never block booking.
- Normal test/CI cannot make live HubSpot requests even if credentials are present.
- All HubSpot request paths are documented 2026-09 paths; Service Key scopes and account write validation are checked before activation.
- Existing mapping is read and identity-verified before use; account mismatch, stale, ambiguous, or conflicting identity fails closed.
- Phone matches are locally normalized and compared to exact E.164; no name-based matching or merge exists.
- Withheld phone preserves Phase 1 local identity behavior; only an explicitly confirmed email may support separate CRM resolution/create.
- Contact creation requires a successful no-match read, confirmed email, and explicit application permission. Ambiguous writes are never retried blindly.
- Contact updates write only confirmed email/phone fields that are blank or already equal; conflicting staff edits are preserved.
- Each successfully committed booking operation can produce one short associated Note; repeated confirmations do not duplicate it.
- CRM failure/uncertainty never rolls back, falsely rejects, or reverses a successful local/Calendar booking.
- Every external mutation has a bounded request budget, timeout, safe result code, idempotency key, and reconciliation action.
- Note and Deal associations are verified against the exact local Contact link; a partial association does not cause a second record create.
- Deals are disabled by default and created only for a configured explicit qualification; normal bookings never create Deals. Leads remain unsupported.
- No raw transcripts, raw provider payloads, or credentials appear in ToolExecution, AuditEvent, logs, or public result models.
- Only one CRM link table is added via Alembic; no generic identity/provider system, outbox publisher, queue, worker, webhook, or CRM retry engine is introduced.
- Live smoke, if separately enabled in a later task, requires the exact test database and allowlisted developer test portal, synthetic run-owned records, and honest cleanup reporting.

## 18. Unresolved decisions and genuine tradeoffs

1. **Service Key scope-picker behavior:** Public documentation establishes scoped CRM access but the exact Notes/association scope grants and availability need to be inspected in the actual developer test portal. This must be resolved before enabling a capability.
2. **Phone search contract:** HubSpot documents its standardized search behavior but not a stable calculated property name. The test portal must confirm the ordinary phone/mobilephone search request format and exact candidate return behavior before implementation. Local E.164 verification remains mandatory.
3. **Contact creation requires email:** This sharply limits phone-only new Contact creation, but avoids depending on nonunique phone or custom portal schema and permits deterministic recovery. If the business later wants phone-only creation, it needs a separately approved HubSpot custom unique property/configuration and an explicit migration/design decision.
4. **Service Key beta:** The chosen model fits single-account, no-webhook integration, but remains public beta as of the research date. HubSpot may change administration or capabilities.
5. **Synchronous CRM latency:** The foreground Note write adds bounded latency after booking. A 10-second request timeout caps the wait but no measured latency SLO is available. Background delivery is intentionally deferred; an interrupted request may leave a durable started operation until a later same-key invocation.
6. **Staff edits and races:** Read-before-PATCH plus fill-blank policy protects most manual CRM edits. There is no atomic compare-and-swap for the selected standard properties, so a narrow concurrent staff-edit race remains.
7. **Unresolved identity and future recovery:** A withheld phone without a confirmed email safely receives no CRM activity. The customer operation remains local; a future operator path may explicitly resolve the Contact and replay the booking outcome.
8. **Deal qualification:** The synthetic service catalog currently defines no quote-required service. The Deal capability remains disabled until a real business rule and trusted caller are defined. This is a Phase 3 design gate, not a reason to create Deals from all appointments.
9. **Historical outcomes:** Only operations committed after CRM is enabled are delivered by this design. No historical booking backfill or import is proposed.
10. **Portal changes:** The mapping is account-scoped. Repointing a deployment to another portal requires an explicit reviewed migration/cleanup procedure; runtime configuration change alone cannot reuse old mappings.

## Authoritative HubSpot source register

All links below were checked on 2026-10-22. Pages are official HubSpot Developer documentation/changelog or HubSpot Knowledge Base.

| Source | Announced/modified date where shown | Use |
| --- | --- | --- |
| [Service Keys enter public beta](https://developers.hubspot.com/changelog/service-keys) | Announced 2026-02-10 | System-to-system fit, scope selection, no webhooks, key rotation |
| [Legacy private-app creation sunset](https://developers.hubspot.com/changelog/legacy-private-app-creation-sunset) | Announced 2026-08-27 | Account-age-dependent sunset dates |
| [Fall 2026 developer spotlight](https://developers.hubspot.com/changelog/fall-2026-spotlight) | Announced 2026-09-15 | 2026-09 GA state, CRM object availability, Service Key beta |
| [CRM API write validation enforcement](https://developers.hubspot.com/changelog/crm-api-write-validation-enforcement) | Announced 2026-08-11; effective 2026-09-08 | 2026-09 admin validation and association permission failures |
| [Date-based API versioning](https://developers.hubspot.com/changelog/introducing-date-based-api-versioning) | Announced 2026-03-30 | YYYY-MM scheme, immutable versions, support window |
| [2026-09 API overview](https://developers.hubspot.com/docs/api-reference/latest/overview) | Last modified 2026-09-09 | Exact versioned API route pattern and API root |
| [Contacts API guide](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/contacts/guide) | Last modified 2026-09-09 | Create/read/update/upsert contracts, email unique identifier |
| [CRM Search API guide](https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm) | Last modified 2026-09-09 | Phone search semantics, endpoints, limits and indexing caveats |
| [Notes API guide](https://developers.hubspot.com/docs/api-reference/latest/crm/activities/notes/guide) | Last modified 2026-09-09 | Note fields, create/read/search and associations |
| [Deals API guide](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/deals/guide) | Current 2026-09 reference | Deal create, stage/pipeline requirements and associations |
| [Associations guide](https://developers.hubspot.com/docs/api-reference/latest/crm/associations/associate-records/guide) | Current 2026-09 reference | Exact association endpoint pattern |
| [Scopes documentation](https://developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/scopes) | Last modified 2026-09-15 | Scope catalog and tier-dependent endpoint availability |
| [API usage guidelines and limits](https://developers.hubspot.com/docs/developer-tooling/platform/usage-guidelines) | Last modified 2026-09-04 | Account limits and rate-limit response handling |
| [Error handling](https://developers.hubspot.com/docs/api-reference/latest/error-handling) | Last modified 2026-09-09 | 400/401/403/429/5xx handling and structured response caution |
| [Developer Platform Basics](https://developers.hubspot.com/developer-platform-basics) | Current page | Free developer test accounts and periodic resets |
| [Create records on the updated index page](https://knowledge.hubspot.com/records/create-records-on-the-updated-index-page) | Last updated 2026-09-02 | Record creation availability across products/plans, including Contacts and Deals |
| [Create or log activities on a record](https://knowledge.hubspot.com/records/manually-log-activities-on-records) | Last updated 2026-09-29 | Notes/activity availability across products/plans |
| [OAuth token management and retry guidance](https://developers.hubspot.com/blog/oauth-token-management-hubspot-integrations) | Updated 2026-01 | General 429 Retry-After, bounded backoff, timeout/idempotency guidance; OAuth lifecycle content does not apply to Service Keys |
| [New Leads API released](https://developers.hubspot.com/changelog/new-leads-api-released?hs_amp=true) | Announced 2024-08-20 | Sales Hub Professional/Enterprise Leads availability |
| [Create a sandbox and deploy changes](https://knowledge.hubspot.com/account-management/deploy-sandbox-changes-to-production) | Last updated 2026-06-12 | Standard sandbox subscription requirements |
| [Safely experiment with HubSpot](https://developers.hubspot.com/blog/how-to-safely-experiment-with-ai-in-hubspot-before-you-touch-real-data) | Current developer guidance | Separate developer test account and sandbox distinction |

Research confidence: the stable 2026-09 endpoints, Contact create/upsert rules, phone search behavior, Notes association route, Deal create requirements, Service Key beta/no-webhook behavior, API validation changes, and developer test account availability are documented. Exact selectable Service Key scopes and account-specific API/write permissions, phone-search behavior against the chosen portal, and custom validation settings remain unverified until a dedicated test account is provisioned. No HubSpot API was called during this design task.
