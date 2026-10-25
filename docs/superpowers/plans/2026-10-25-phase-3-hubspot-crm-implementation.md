# Phase 3 — HubSpot CRM Integration Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Acceptance criteria use checkbox syntax for tracking.

**Goal:** Implement the independently approved Phase 3 HubSpot Contact and booking-outcome capability, ending at REVIEW without beginning Phase 4.

**Architecture:** Preserve the existing single-business Python/FastAPI/PostgreSQL modular monolith. Implement only the concrete, bounded HubSpot integration and the four application operations in the approved design. Calendar remains authoritative for appointment state, and CRM work remains a separate failure boundary.

**Tech Stack:** Existing Python 3.12.15, uv, FastAPI, Pydantic v2/Pydantic Settings, SQLAlchemy async with Psycopg 3, PostgreSQL 18.6, Alembic, pytest/pytest-asyncio, Ruff, strict mypy, detect-secrets, pip-audit, Docker Compose, and existing CI. Task 2 uses one concrete HTTP client at the integration boundary; no new dependency is assumed by this plan.

**Spec:** Approved design at docs/superpowers/specs/2026-10-22-phase-3-hubspot-crm-integration-design.md, commit d0a1909d091870eaad5bdc2d701c363d0506a841, including the two narrow identity clarifications recorded alongside this plan.
**Implementation branch:** phase/3-hubspot-crm-integration, starting from the approved design commit above.
**Main baseline:** e5d8b31876d46cc17f24855f298c74eeb1093209.

## Roadmap state at plan approval

- Phases 0–2: COMPLETE.
- Phase 3: IN PROGRESS — design only.
- Phases 4–12: NOT STARTED.
- Only Task 17's final gate may move Phase 3 to REVIEW. Independent human review is required before COMPLETE.

## Global implementation invariants

These requirements apply to every later implementation task:

1. Keep the existing Python/FastAPI/PostgreSQL modular monolith.
2. No production HubSpot API call without verified configuration and account binding.
3. Normal automated tests and CI must be provider-free.
4. Google Calendar remains authoritative for appointment state.
5. CRM failures never reverse or falsely reject a committed booking.
6. No Deal, Lead, webhook, OAuth consent flow, operator UI, LLM integration, worker, queue, Redis, outbox publisher, or multitenant platform.
7. No arbitrary HubSpot record IDs or raw property dictionaries accepted by application operations.
8. No Contact merge or fuzzy customer identity matching.
9. No blind retry of potentially successful non-idempotent creates.
10. No raw customer PII, credentials, provider payloads, or unfiltered provider errors in ToolExecution, AuditEvent, or application logs.
11. Existing Phase 0–2 tests and contracts must remain intact.
12. Implementation must follow approved tasks, not speculative abstractions.

Each later implementation tranche commits and pushes to phase/3-hubspot-crm-integration in the same task, then stops for independent review. Codex does not advance a milestone to approved/complete merely because tests pass.

## Review focus

- Matching expected portal ID is insufficient by itself; an unverifiable same-key account preflight keeps live writes disabled. Task 3 covers matching, mismatch, changed key, and missing access.
- Missing provider phone/email fields are not conflicts by themselves; actual conflicting values or actual matches to different IDs block linking. Tasks 5–6 cover these cases.
- An email-linked Contact must be re-verifiable after restart without trusting an arbitrary preexisting Contact.email. Task 6 covers durable reload and controlled provenance.
- A possibly successful Contact or Note create is reconciled without a blind second create. Tasks 9 and 13 cover timeouts, delayed indexing, and persistence failure.
- Historical Note delivery after reschedule/cancellation uses the immutable successful source execution, not current Booking state. Tasks 11 and 14 cover delayed and out-of-order recording.

---

# M3A — HubSpot connection and boundary

## Task 1 — Runtime configuration and safety

Implement only the minimal HubSpot settings required by the approved design:

- Optional RECEPTIONIST_HUBSPOT_SERVICE_KEY using SecretStr.
- Expected account/portal ID.
- Bounded request timeout.
- Safe configured/disabled state.
- Safe settings repr and logging.
- No effect on Calendar or application startup when HubSpot is unconfigured.

No actual provider calls yet.

**Acceptance:**

- [ ] Secret values never appear in settings representations or logs.
- [ ] Missing HubSpot configuration does not break startup or bookings.
- [ ] Test/CI cannot accidentally reach live HubSpot even if credentials are present.
- [ ] Settings have strict validation and focused unit tests.
- [ ] No production HubSpot credential is required.

## Task 2 — Concrete HubSpot HTTP boundary

Implement one narrowly scoped provider module, for example src/receptionist/integrations/hubspot.py. Use a concrete HTTP client, not a generic provider framework.

Support only the exact calls required for:

- Contact get-by-ID.
- Contact get-by-email.
- Bounded Contact search.
- Contact create.
- Contact update.
- Note create.
- Note search/reconciliation.
- Note-to-Contact association read and narrowly scoped repair.

Use the documented 2026-09 CRM paths from the approved specification. If a supporting Account Info endpoint has a different published version, use its exact documented version. Do not mechanically rewrite its path.

Map provider responses to narrow typed snapshots and safe errors.

Enforce:

- Fixed HubSpot host and TLS.
- Bearer key isolation.
- Strict request-field allowlists.
- Finite deadlines.
- Limited response sizes.
- Bounded request counts.
- Safe handling of 400, 401, 403, 404, 409, 429, 5xx, and timeouts.

No automatic retry of an ambiguous write.

**Acceptance:**

- [ ] Mocked transport tests assert exact HTTP method, path, headers, and body.
- [ ] Raw provider dictionaries/errors remain inside the integration boundary.
- [ ] Contact and Note functionality has no Deal/Lead code.
- [ ] An accidental real network call in normal tests fails immediately.
- [ ] No generic CRM action dispatcher or provider registry is introduced.

## Task 3 — Account binding and capability preflight

Implement a fail-closed activation/preflight contract.

Use an officially documented account-details API method when supported by the provisioned Service Key.

The actual credential must prove it targets the configured expected portal ID before live writes are enabled.

Retain the researched Account Info uncertainty; do not invent an endpoint, scope, or token-introspection method.

The actual test portal must later verify:

- Same credential can retrieve the expected account identity.
- Contact read/write scopes.
- Note read/write scopes.
- Association permissions.
- Relevant account-specific validation requirements.

If any mandatory property is unverifiable, disable live writes and report a safe configuration/permission outcome.

Credential rotation invalidates previous in-memory preflight success.

**Acceptance:**

- [ ] Mock tests cover matching portal, mismatched portal, missing access, changed key, and incomplete preflight.
- [ ] Configured portal ID alone never enables writes.
- [ ] No secret or raw account response is persisted.
- [ ] Production activation remains disabled without real provisioning verification.

**M3A review gate:** Configuration, HTTP boundary, and mocked preflight contracts pass independent review before proceeding.

# M3B — Customer identity and mapping

## Task 4 — Contact-link table and migration

Create exactly one small CRM link table through Alembic.

Fields follow the approved design:

- Local Contact foreign key and primary key.
- Portal/account ID.
- HubSpot Contact ID.
- Linked-by provenance enum with the approved values phone_e164 and verified_email.
- Active/stale state.
- Verification timestamp.
- Created/updated timestamps.

Enforce uniqueness of (hubspot_portal_id, hubspot_contact_id), where hubspot_portal_id is the portal/account ID.

No free-form JSON, provider payload, PII copy, or generic external-record table.

**Acceptance:**

- [ ] Migration upgrade/downgrade works.
- [ ] Duplicate links are rejected by PostgreSQL.
- [ ] Foreign-key restrictions preserve historical integrity.
- [ ] Existing Phase 2 migrations remain valid.
- [ ] Migration drift check passes.

## Task 5 — Deterministic identity matching

Implement reusable application-level identity decisions:

- Exact local E.164 phone verification.
- Trusted verified email comparison.
- Bounded provider candidate handling.
- No name-based matching.
- Missing/withheld phone behavior.
- Ambiguity and conflicting-record detection.

**Important approved clarification:**

When both verified phone and email signals produce actual provider matches, they must identify the same Contact.

However, an absent HubSpot field is not automatically a conflicting value.

Examples:

- HubSpot Contact has matching verified email but blank phone, with no competing verified phone match: email linking may be permitted.
- Provider phone and verified email point to different HubSpot IDs: conflict.
- Provider record has a nonblank, different verified identity value: conflict.
- Search returns incomplete or ambiguous candidates: do not link automatically.

Never fabricate matching evidence for a missing field. Phone-only Contact creation remains out of scope.

**Acceptance:**

- [ ] Deterministic unit matrix for missing/matching/conflicting phone and email.
- [ ] Duplicate-candidate and incomplete-pagination tests.
- [ ] No match by name.
- [ ] No customer PII exposed through errors.

## Task 6 — resolve_crm_contact

Implement the approved finite application operation.

Verify:

- Source Conversation exists.
- It belongs to the requested Contact.
- Existing Contact link matches the preflight-verified portal.
- Mapped provider record still matches known trusted identity.
- Stale or missing provider record follows bounded re-resolution.
- Conflicting identities never produce a link.
- Provider failure never becomes a false no-match.

Only persist a mapping after the identity decision is unambiguous.

**Email provenance invariant:**

An email-linked Contact must remain re-verifiable across restarts using the trusted email established by the linking workflow and the existing local Contact record.

Do not create a link based on an ephemeral trusted email value and then discard all ability to verify that email later.

Use existing Contact email storage and narrowly controlled verified updates where appropriate. An arbitrary unverified preexisting Contact.email must not silently become trusted evidence.

Any later inconsistent identity change must fail closed until trusted re-resolution.

Do not introduce a general identity-provenance subsystem.

**Acceptance:**

- [ ] Mapped, unmapped, stale, archived, conflict, and provider-failure cases.
- [ ] Durable reload successfully verifies an email-linked Contact.
- [ ] Cross-Contact ownership and portal mismatch fail closed.
- [ ] Ambiguous candidates never create mappings.

## Task 7 — Mapping concurrency and database recovery

Add PostgreSQL integration tests for:

- Two Conversations of the same Contact resolving simultaneously.
- Two local Contacts competing for one HubSpot ID.
- Duplicate mapping insert races.
- Transaction rollback and session reuse.
- Stale-link relinking.
- Provider read failure during resolution.
- Idempotent same-key replay.

Use real PostgreSQL uniqueness/transactions rather than application-only deduplication.

No speculative locking framework.

**M3B review gate:** Identity decisions, mapping correctness, concurrency, and migration pass independent review.

# M3C — Controlled Contact writes

## Task 8 — Trusted internal write boundary

Implement the finite request/result contracts for:

- create_crm_contact.
- update_crm_contact.

These are internal application operations.

No public HTTP route, operator authentication system, new dashboard, or LLM-exposed write tool.

The implementation may be invoked only through a controlled trusted application/test path.

An arbitrary boolean or user-provided claim of confirmation must never authorize a mutation.

The approved trusted values must be validated, bound to the owning local Contact, and consistent with the existing identity rules.

**Acceptance:**

- [ ] Strict typed models.
- [ ] Unknown fields rejected.
- [ ] Arbitrary property maps rejected.
- [ ] Cross-Contact access rejected.
- [ ] Unsafe/unverified values cannot reach the provider write boundary.
- [ ] No production customer-originated write path is exposed.

## Task 9 — Safe Contact creation and recovery

Implement Contact create only with a trusted verified email and explicit trusted invocation.

Before creation:

- Resolve or verify absence using the approved bounded lookup.
- Reject ambiguity and identity conflict.
- Validate the portal binding.
- Create and commit a durable ToolExecution claim.
- Ensure provider-side create is not attempted when claim persistence fails.

After an ambiguous create:

- Reconcile using exact email and any verified identity evidence.
- Never blindly issue a second POST.
- Remain uncertain if the provider outcome cannot be proven.

Ensure newly established email provenance is durable enough for subsequent mapped verification.

**Acceptance:**

- [ ] Successful create and link.
- [ ] Existing record found instead of duplication.
- [ ] Provider success followed by DB failure.
- [ ] Timeout after potential provider success.
- [ ] Concurrent create attempts.
- [ ] Stale/no-match/search-index-lag cases.
- [ ] Same-key recovery without duplicate provider writes.

## Task 10 — Safe Contact updates

Implement fill-blank-only behavior for approved email/phone fields.

Read the mapped provider record before mutation.

- Missing field: eligible for verified fill.
- Already equal: unchanged.
- Nonblank different value: conflict, no overwrite.
- Missing trusted value: omit.
- Account validation failure: safe rejection.
- Staff-edited lifecycle/owner/company fields: never touched.

Preserve the limitation that a staff edit may race a read-before-PATCH without provider compare-and-swap.

**Acceptance:**

- [ ] Full field-ownership test matrix.
- [ ] No overwrite of conflicting staff values.
- [ ] No unintended clearing.
- [ ] Uncertain PATCH results handled without unsafe retries.
- [ ] PII excluded from durable execution/audit payloads.

**M3C review gate:** Trusted create/update, provider validation, and recovery pass independent review.

# M3D — Booking outcome Notes

## Task 11 — Immutable booking source validation

Implement a focused validator that loads:

- The source Booking ToolExecution.
- Its Conversation.
- The Booking.
- The owning Contact.
- The immutable successful source result.

Require all of the following before the Note operation:

- Source ToolExecution status is SUCCEEDED and tool_name is confirm_booking_action.
- Derive the create/reschedule/cancel action from immutable sanitized_arguments.action_type; do not accept a caller-supplied action.
- The immutable sanitized_result is a successful ConfirmationResult for confirm_booking_action and its data.booking_id equals the requested Booking ID.
- The source Conversation belongs to the Booking's Contact, and the Booking still belongs to that Contact.
- The source action is create_booking, reschedule_booking, or cancel_booking, with the matching historical result status: confirmed for create/reschedule and cancelled for cancel.
- Note content and hs_timestamp use the immutable source result and terminal ToolExecution.finished_at.

Never use current Booking status or booking_data.last_operation to determine a historical operation's eligibility.

Use the original terminal execution timestamp and historical result values for Note content.

**Acceptance:**

- [ ] Confirmed create is eligible.
- [ ] Successful reschedule is eligible.
- [ ] Successful cancellation is eligible.
- [ ] Started, failed, rejected, wrong tool, wrong Contact, and mismatched Booking ID are ineligible.
- [ ] Earlier create/reschedule remains eligible after subsequent Booking changes.

## Task 12 — Structured Note creation

Implement record_booking_outcome as an explicitly invoked, post-commit CRM operation.

It must:

- Derive its deterministic CRM execution key from the source booking ToolExecution ID.
- Require an existing verified CRM Contact mapping.
- Create a durable CRM execution claim before provider mutation.
- Produce only bounded structured Note content.
- Use an opaque source-operation marker.
- Associate the Note with the exact verified Contact.
- Return a safe result independent of the original booking result.

No automatic hook in Phase 2 confirmation. No CRM write during Calendar booking transactions.

**Acceptance:**

- [ ] Exact Note body allowlist and historical timestamp tests.
- [ ] Correct association ID/Contact tests.
- [ ] No duplicate Note on successful same-key replay.
- [ ] No Note for missing/unverified Contact.
- [ ] No raw transcripts, requirements, name, email, phone, or provider payload in Note content.

## Task 13 — Ambiguous Note and association recovery

Implement recovery for:

- Possible provider success followed by timeout.
- HTTP 5xx with uncertain write.
- Local persistence failure after provider success.
- Note exists but Contact association is incomplete.
- Note marker search returns zero, one, multiple, or incomplete results.

Reconciliation must verify exact marker and Contact association.

If a Note is not found, uncertainty remains; a second create is not authorized.

No guarantee of exactly-once or eventual delivery is claimed.

**Acceptance:**

- [ ] No blind duplicate Note creation.
- [ ] Exact association repair only.
- [ ] Wrong Contact association is never accepted.
- [ ] No cleanup or mutation of unrelated provider records.
- [ ] Provider search-index delay is represented honestly.

## Task 14 — Full booking history and failure matrix

Add PostgreSQL integration scenarios:

1. Create Booking.
2. Reschedule it.
3. Cancel it.
4. Invoke the corresponding historical CRM Notes out of order.
5. Verify each Note describes its original successful source operation.
6. Retry each Note invocation with the same key.
7. Verify no extra Note write is issued.

Also cover:

- CRM provider unavailable after successful booking.
- Claim persistence failure.
- Same-key concurrency.
- Contact reassignment/stale mapping.
- Ambiguous provider success.
- Local transaction rollback.
- Provider-free repeated test execution.

**M3D review gate:** Historical correctness, recovery, and complete separation from Phase 2 booking outcomes pass independent review.

# M3E — Security, verification and closeout

## Task 15 — Dedicated non-production HubSpot smoke mechanism

Build a manual-only, explicitly guarded test-account smoke.

Requirements:

- Dedicated non-production HubSpot developer test account.
- Independently verified account/portal binding.
- Minimum scopes.
- Exact smoke confirmation marker.
- Strict separate smoke database.
- Synthetic run-unique Contact data.
- Exact run-owned record identification and cleanup.
- No other account or unrelated record touched.
- No live calls in normal CI.

Do not reuse or weaken the existing destructive receptionist_test guard to accommodate the separate CRM smoke database. Add only the smallest dedicated safe guard needed.

If credentials are unavailable, record honestly that live smoke was not run.

**Acceptance:**

- [ ] Guard rejection tests.
- [ ] Wrong-portal and unsafe-DB refusals.
- [ ] No network before safeguards pass.
- [ ] Exact-record cleanup after partial failure.
- [ ] No credential leakage.
- [ ] No claim of live verification without real execution.

## Task 16 — Privacy and security closeout

Audit:

- Service Key secrecy.
- Key rotation/preflight invalidation.
- Contact PII.
- Raw HubSpot errors and headers.
- Note body content.
- Durable ToolExecution and AuditEvent JSON.
- Cross-Contact access.
- Provider-result sanitization.
- Portal mismatch.
- Dependency advisories.

Prove the existing non-mutating secret gate still behaves correctly.

No weakening of CI or audit gates.

**Acceptance:**

- [ ] Secret and PII sentinel tests.
- [ ] No raw provider data persisted.
- [ ] Secret-gate fail-closed proof.
- [ ] Dependency audit.
- [ ] Provider-free automated tests.
- [ ] No unrelated files or scopes introduced.

## Task 17 — Final documentation and release candidate

Update only the documentation needed to describe the implemented behavior.

Document:

- HubSpot authority and boundaries.
- Account-level Service Key provisioning.
- Required scopes and live activation preflight.
- Permitted Contact fields.
- Identity ambiguity and stale links.
- Trusted write restriction.
- Explicit booking Note invocation.
- Same-key recovery.
- CRM failure independence from Calendar.
- Guarded smoke instructions.
- Limitations and deferred capabilities.

Run final verification:

- [ ] Full unit tests.
- [ ] Full PostgreSQL integration tests twice consecutively.
- [ ] Migration drift.
- [ ] Alembic upgrade/downgrade/upgrade.
- [ ] Ruff.
- [ ] Mypy.
- [ ] Secret scan.
- [ ] Dependency audit.
- [ ] make check.
- [ ] Docker Compose configuration.
- [ ] Docker image build.
- [ ] Branch scope check against the merged Phase 2 main baseline.

Only after gates pass, move Phase 3 to REVIEW. Do not mark it COMPLETE.

No PR or merge without a later explicit owner authorization.

**M3E review gate:** The complete Phase 3 implementation candidate stops for final independent review.

# Implementation sequencing and Git discipline

Each milestone is divided into the focused tasks above.

The later implementation process is:

1. Implement a small approved group of tasks.
2. Execute its acceptance tests.
3. Commit and push the feature branch automatically in that same Codex task.
4. Return exact SHAs, changed files, results, deviations, and outstanding work.
5. Stop for independent technical review before continuing.

Do not implement all 17 tasks in one unreviewed coding run.

Do not add features merely because a provider SDK supports them.

The overall Phase 3 branch remains phase/3-hubspot-crm-integration. The fixed implementation endpoint is Phase 3 REVIEW, pending independent human approval.

# Current task boundary

This commit records the approved plan and the two narrow clarifications in the design document. It does not authorize production implementation, live HubSpot calls, a PR, a merge, or a change to roadmap status. Stop for independent review of the plan before implementation.
