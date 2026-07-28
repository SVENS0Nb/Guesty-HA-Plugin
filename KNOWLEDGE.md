# Guesty Home Assistant Integration — Knowledge Base

## Purpose

This file is the canonical, durable technical memory for this repository. It
captures facts and decisions that future work must not have to rediscover from
chat history, logs, or old releases. [`AGENTS.md`](AGENTS.md) defines the
mandatory workflow for reading, validating, and maintaining this knowledge.

This is not a changelog, issue tracker, copy of the README, or storage location
for live customer data. The current production code and regression tests remain
the ultimate source of truth. When they disagree with this file, investigate
the discrepancy and update all affected artifacts together.

## Review metadata

- Last project-wide review: 2026-07-28
- Baseline reviewed: integration version 2.2.6
- Review scope: production modules, configuration constants, persistence
  boundaries, README behavior, and regression-test inventory
- Current status: no known provisional entries

Update the project-wide review date only after all active entries have been
checked. Narrow feature work updates the affected entries and their individual
validation dates, but not this global date.

## Evidence and status model

Evidence is weighted in this order:

1. Reproducible regression tests together with the production code they cover.
2. A redacted live API reproduction or an official public API specification.
3. The README as the user-facing contract.
4. Historical issue, release, or conversation context.

`Validated` means the fact is supported by current evidence and may guide
implementation. `Provisional` means more evidence is required and the entry
must state how to obtain it. `Retired` preserves only a previously plausible
assumption whose reuse would cause a meaningful regression.

## System boundaries and ownership

### KB-ARCH-001 — One shared Guesty synchronization owner

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/coordinator.py`,
  `custom_components/guesty/__init__.py`,
  `tests/test_coordinator.py`

Each Home Assistant config entry owns exactly one `GuestyApiClient`, OAuth
token lifecycle, `GuestyDataUpdateCoordinator`, reservation/listing snapshot,
normal poller, and Guesty webhook intake. Door access, Loxone, TTLock, sensors,
and calendars consume that shared snapshot. They must not introduce independent
Guesty polling loops. Targeted reads performed through the shared client and
coordinator are allowed when a webhook or a field-specific API limitation
requires them.

### KB-ARCH-002 — Runtime setup and teardown are transactional

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/__init__.py`,
  `tests/test_init.py::test_partial_setup_failure_rolls_back_started_resources`,
  `tests/test_init.py::test_first_refresh_failure_shuts_down_coordinator`

`__init__.py` owns startup and teardown order. If setup fails after any
manager, timer, webhook, listener, platform, or background task has started,
everything already started must be rolled back. Unload and reload must cancel
owned tasks; no worker may survive its config entry.

### KB-ARCH-003 — Persistence has separate privacy domains

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/storage.py`,
  `custom_components/guesty/access.py`,
  `custom_components/guesty/loxone.py`,
  `custom_components/guesty/ttlock.py`,
  `tests/test_storage_diagnostics.py`

All Home Assistant stores are private and defensively loaded:

- `guesty_cache_*` is the privacy-filtered general listing/reservation cache. It
  never persists native Keycodes or door-link bearer tokens.
- `guesty_access_*` owns door-link token hashes, revocation, cleanup, and
  minimal link-publication state.
- `guesty_loxone_*` owns the shared private numeric PIN lifecycle and Loxone
  provisioning/cleanup state.
- `guesty_ttlock_*` owns TTLock OAuth/passcode delivery and cleanup state but
  reuses the shared PIN rather than creating another PIN authority.

Do not merge these stores or leak their private fields into diagnostics,
entities, events, or the general cache.

### KB-ARCH-004 — Module responsibility map

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/`, `tests/`

The stable responsibility boundaries are:

| Module | Responsibility |
| --- | --- |
| `api.py` | Guesty OAuth/API, retries, pagination, webhooks, Keycode and reservation custom-field operations |
| `_http.py` | EOF-aware bounded response reading for every external client |
| `coordinator.py` | Poll/webhook merge and shared runtime snapshot |
| `models.py` | Parsing, status and time semantics, occupancy, merge/window logic |
| `storage.py` | Privacy-filtered general cache |
| `scheduler.py` | Exact local check-in/check-out transitions without API traffic |
| `webhook.py` | Signed incoming webhook endpoint and remote subscription lifecycle |
| `access*.py` | Guest door portal, localization, branding, token security, link publication |
| `loxone.py` / `loxone_api.py` | Shared PIN authority and Loxone user lifecycle/API |
| `ttlock.py` / `ttlock_api.py` | TTLock passcode lifecycle and Open Platform API |
| `sensor.py` / `calendar.py` | Dynamic per-listing Home Assistant entities |
| `config_flow.py` | Setup, reauthentication, reconfiguration, and all UI options |
| `diagnostics.py` | Privacy-safe operational diagnostics |

### KB-ARCH-005 — Optional side effects require an enabled listing mapping

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/__init__.py`,
  `custom_components/guesty/access.py`,
  `custom_components/guesty/loxone.py`,
  `custom_components/guesty/ttlock.py`, provider tests

Door access, Loxone, and TTLock are independently optional and listing-scoped.
A listing without an active mapping for a feature must receive none of that
feature's writes, remote objects, or extra field-specific reads. Stale mappings
for a disabled provider must not trigger an unnecessary startup full sync.

## Guesty synchronization and API behavior

### KB-GUESTY-001 — Polling, full sync, and webhook traffic model

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/const.py`,
  `custom_components/guesty/coordinator.py`,
  `tests/test_coordinator.py`

The default reservation poll is 300 seconds. Reservation polls are incremental
with a five-minute overlap, plus a daily full reservation sync. Listing safety
sync is daily while a signed webhook is active and at least every 15 minutes
when it is unavailable. Supported newly registered events are
`reservation.created.v2`, `reservation.updated.v2`, `listing.new`,
`listing.updated`, and `listing.removed`.

Webhooks are the fast path: one reservation event uses a targeted read, bursts
are debounced/coalesced, sufficient listing payloads are applied directly,
missing details alone are fetched, removed listings are pruned immediately, and
a new listing requests reservations only for that listing.

### KB-GUESTY-002 — Native Keycodes require Reservations v3

- Status: Validated
- Last validated: 2026-07-28
- Evidence: redacted live API reproduction on 2026-07-28,
  `custom_components/guesty/api.py::async_get_reservation_key_codes`,
  `tests/test_api.py::test_native_keycode_reads_use_batched_v3_array_responses`,
  `tests/test_coordinator.py::test_full_poll_enriches_mapped_reservations_from_v3`

Guesty's legacy `/reservations` endpoints do not expose `notes.keyCode`, even
when requested in their field projection. Native Keycodes must be read with
`GET /v1/reservations-v3` using repeated `reservationIds[]` query parameters.
That endpoint accepts at most ten reservation IDs per request. Guesty identifies
returned v3 reservations with either `reservationId`, `_id`, or `id`. A
redacted live comparison of 35 future reservations proved that the v3 `_id`
values exactly match the internal legacy reservation IDs. Observed responses
use a top-level JSON array, while Guesty's public example also shows a single
reservation object; the parser therefore supports both shapes without
associating an unexpected reservation with a requested ID.

Only active reservations belonging to listings mapped to an enabled Loxone or
TTLock provider are enriched: changed reservations during incremental polls, a
targeted reservation after a single webhook, and all applicable reservations
during startup/daily full synchronization. This is an enrichment step inside
the existing coordinator, not another poller.

### KB-GUESTY-003 — Native Keycode writes are minimal and confirmed

- Status: Validated
- Last validated: 2026-07-28
- Evidence: redacted live API reproduction on 2026-07-28,
  `custom_components/guesty/api.py`,
  `tests/test_api.py::test_native_keycode_uses_minimal_v3_notes_payload`,
  `tests/test_api.py::test_native_keycode_404_verifies_reservation_without_legacy_write`,
  `tests/test_api.py::test_native_keycode_requires_exact_success_confirmation`

The only supported write route is
`PUT /v1/reservations-v3/{reservationId}/notes` with only
`{"notes":{"keyCode":"..."}}`. Some Guesty applications can read the exact
existing reservation through v3 while that dedicated notes route consistently
returns HTTP 404. The integration verifies the exact reservation through v3 so
that condition is reported as an unavailable Keycode endpoint rather than a
missing reservation, and preserves the failed PUT's `x-request-id`.

A write is successful only after Guesty confirms the exact reservation ID and
value in the response or a bounded v3 read-back. Resource IDs must be validated
before they are interpolated into paths. Never use the legacy general
reservation updater or a custom field as a Keycode fallback.

### KB-GUESTY-004 — Sparse and empty projections have different meanings

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/models.py`,
  `tests/test_models.py::test_omitted_notes_are_not_treated_as_an_empty_native_keycode`,
  `tests/test_coordinator.py::test_v3_observed_empty_keycode_remains_authoritative`,
  `tests/test_coordinator.py::test_sparse_v3_keycode_response_is_not_an_observed_deletion`

An omitted `notes` object means the Keycode was not observed. It is not proof
that a user deleted the field and must not rotate or revoke an otherwise known
PIN. An explicitly returned empty or invalid Keycode is authoritative and must
fail closed according to the PIN lifecycle. The same general distinction
applies to sparse reservation custom-field responses.

### KB-GUESTY-005 — Door links use reservation custom fields, not Keycode

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/api.py`,
  `custom_components/guesty/access.py`, `tests/test_api.py`,
  `tests/test_access.py`

The guest door-access URL and numeric access PIN are separate data paths. Door
links use a configurable Guesty reservation custom field through Reservations
v3 custom-field endpoints. Field names, IDs, and `{{variables}}` are resolved
safely. Publication is considered synchronized only after bounded read-back
confirmation. Field-ID healing is allowed only for narrowly classified
field-reference failures, not generic 404, timeout, rate-limit, or server
errors.

### KB-GUESTY-006 — Authentication and transient failures recover in place

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/api.py`,
  `custom_components/guesty/coordinator.py`, `tests/test_api.py`,
  `tests/test_coordinator.py::test_auth_failure_starts_reauthentication`

OAuth refresh is shared and serialized. Authentication or permission failure
starts Home Assistant reauthentication. Retry-safe transient failures use
bounded exponential backoff and `Retry-After`; permanent failures are not
blindly retried. During a transient outage the last valid snapshot remains
visible as degraded/stale, and normal operation resumes automatically.

Guesty Client ID and Client Secret are stable application credentials; the
derived Bearer access token is the expiring value. Reuse the shared token
lifecycle rather than repeatedly minting tokens: Guesty's OAuth endpoint can
rate-limit repeated token requests with a long `Retry-After`.

### KB-GUESTY-007 — Incoming webhooks are authenticated before work

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/webhook.py`,
  `tests/test_webhook.py::test_invalid_stale_and_replayed_signatures_are_rejected`,
  `tests/test_webhook.py::test_failed_webhook_handoff_can_be_retried`

Before accepting work, the Home Assistant endpoint enforces the Guesty/Standard
Webhooks HMAC, timestamp tolerance, request-size limit, and replay/message ID.
An event is marked complete only after the coordinator accepts the handoff so a
failed handoff can be retried. Existing legacy subscriptions without a signing
secret are migrated once; failure must fall back to polling rather than enter a
delete/create loop.

### KB-GUESTY-008 — Live v3 responses are sparse and a notes 404 is route-specific

- Status: Validated
- Last validated: 2026-07-28
- Evidence: redacted live API and production-log reproduction on 2026-07-28,
  `custom_components/guesty/api.py`,
  `tests/test_api.py::test_native_keycode_accepts_v3_success_using_internal_id`,
  `tests/test_api.py::test_native_keycode_404_verifies_reservation_without_legacy_write`

A redacted live comparison requested 35 future reservations by their internal
IDs. Reservations v3 returned every exact requested reservation, used `_id`,
and introduced no unexpected ID. Of those responses, 33 omitted `notes`
entirely and two returned a `notes` object with a native Keycode. Therefore
`_id`, `reservationId`, and `id` are all valid identity fields, and omitted
`notes` is a common sparse response rather than proof that Guesty deleted a
Keycode.

For the affected Open API application, the same exact reservations remained
readable through Reservations v3 while repeated dedicated
`PUT /v1/reservations-v3/{reservationId}/notes` calls returned HTTP 404 with
distinct request IDs. That 404 is not sufficient evidence that a reservation
is missing. Production then proved that the general legacy
`PUT /v1/reservations/{reservationId}` can return success and trigger Guesty
reservation-change notifications while a bounded v3 read-back remains empty.
That route is a no-op for `notes.keyCode` and must never be called as a
fallback. The integration now verifies the exact v3 reservation, reports a
stable endpoint-unavailable error with the original request ID, and retries
only the documented v3 route with bounded persistent backoff.

A direct test-access comparison could not be repeated during this validation
because Guesty's OAuth endpoint returned HTTP 429 with a long `Retry-After`.
Do not mint more diagnostic tokens until that window expires.

## Reservation, time, and entity semantics

### KB-RES-001 — Reservation activity ignores payment state

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/const.py`,
  `custom_components/guesty/models.py`, `tests/test_models.py`

Confirmed, reserved, awaiting-payment, checked-in, and in-house variants are
active. Cancelled, closed, declined, and expired variants are inactive.
Payment status is intentionally not a criterion. Future active reservations can
receive a Guesty Keycode and door link immediately; remote lock provisioning is
separately deferred by provider lead time.

### KB-RES-002 — Planned local times override stale UTC timestamps

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/models.py`,
  `tests/test_models.py::test_planned_times_override_stale_utc_timestamps`,
  `tests/test_models.py::test_invalid_planned_time_falls_back_to_utc_timestamp`

Time precedence is:

1. A valid `plannedArrival`/`plannedDeparture` combined with its localized date.
2. A valid `checkIn`/`checkOut` UTC timestamp.
3. The localized date combined with reservation/listing defaults.
4. Final defaults of 15:00 check-in and 11:00 check-out.

Use the listing timezone, with Home Assistant timezone only as fallback. Manual
Guesty time changes therefore update already provisioned access even if Guesty
leaves an older UTC timestamp in the reservation.

### KB-RES-003 — Time windows are half-open and locally scheduled

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/models.py`,
  `custom_components/guesty/scheduler.py`,
  `tests/test_models.py::test_occupancy_boundaries`,
  `tests/test_scheduler.py`

Occupancy and access windows use `[start, end)`. Invalid intervals are skipped
rather than aborting a whole sync, and overlapping current reservations are
resolved deterministically. A local transition scheduler changes occupancy
exactly at check-in/check-out without another Guesty request.

### KB-RES-004 — Listing entities are dynamic and privacy-aware

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/sensor.py`,
  `custom_components/guesty/calendar.py`, `tests/test_entities.py`

Every discovered listing gets an occupancy sensor and reservation calendar.
Applicable diagnostic sensors expose Guesty Keycode, Loxone, TTLock, access
link, and sync status. Newly discovered listings are added once and removed
listings are pruned from runtime and the entity registry. Current-guest and
door-link sensors are disabled by default. Guest names and confirmation codes
are available only after explicit privacy opt-in.

## Shared PIN lifecycle

### KB-PIN-001 — Guesty's native Keycode is authoritative

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/loxone.py`,
  `tests/test_loxone.py`, `tests/test_models.py`

The sole source and destination for reservation PINs is Guesty's native
Reservations-v3 `notes.keyCode`. Once Guesty has confirmed six numeric digits,
the integration never rotates them automatically. A valid unique manual edit
in Guesty becomes the new authority and propagates to existing Loxone and
TTLock objects.

If a confirmed Keycode is explicitly cleared, invalid, or duplicated, remote
delivery is revoked and a conflict is shown. The integration does not invent a
replacement after confirmation. A new reservation whose observed Keycode is
initially empty may receive its first generated PIN. Duplicate ownership is
deterministic: the first healthy established reservation keeps delivery; later
duplicates remain blocked until manually corrected in Guesty.

### KB-PIN-002 — Generated PINs and display suffixes are separate

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/loxone.py`,
  `custom_components/guesty/config_flow.py`, `tests/test_loxone.py`,
  `tests/test_config_flow.py`

The operational PIN is exactly six ASCII digits. Generation uses
`secrets.randbelow`, a configured one- or two-digit ASCII prefix, rejects weak
sequences, and excludes all known active/private/rejected codes.

A listing may append up to eight printable non-digit characters such as `#`,
`*`, or `☑️` for Guesty's display. Loxone and TTLock always receive only the
six digits. Changing a suffix republishes the display value without rotating
the PIN.

### KB-PIN-003 — Guesty Keycode writes share one persistent budget

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/loxone.py`,
  `tests/test_loxone.py::test_two_keycodes_are_written_per_30_second_window`,
  `tests/test_loxone.py::test_keycode_endpoint_failure_stops_the_current_write_batch`,
  `tests/test_loxone.py::test_persisted_guesty_write_limit_survives_manager_restart`

All Keycode publication paths share a persistent global limit of at most two
write attempts in any 30-second window. Failed and ambiguous writes consume a
slot. Queue passes, webhook work, reservation-specific retries, source
migration, suffix changes, and restarts must not bypass it. Current and nearest
stays are prioritized while preserving Guesty API headroom.

Every documented v3 Keycode PUT consumes exactly one slot. An application-wide
notes-endpoint, authentication, permission, transport, or payload failure may
stop the current batch so the remaining slot and normal Guesty synchronization
headroom are not wasted on the same predictable error. A reservation-specific
missing-record response does not starve the second bounded write.

### KB-PIN-004 — Plaintext lifetime is deliberately bounded

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/loxone.py`,
  `custom_components/guesty/storage.py`,
  `tests/test_storage_diagnostics.py`

The privacy-filtered general cache never stores Keycodes. The private shared
PIN store holds plaintext only while required for delivery and cleanup.
Cancellation or access end removes local plaintext before attempting remote
cleanup. Guesty's native Keycode remains as booking documentation. Status
sensors report safe states, counts, times, and reasons, never the PIN.

### KB-PIN-005 — Persistent Guesty retries recover across API migrations

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/loxone.py`,
  `tests/test_loxone.py::test_setup_recovers_persisted_native_404_backoff_once`,
  `tests/test_loxone.py::test_recovered_404_backlog_resumes_through_global_write_budget`,
  `tests/test_loxone.py::test_setup_recovers_guesty_backoff_after_client_change`

Guesty Keycode write failures retain bounded persistent retry state across
restarts. A versioned migration may reschedule a narrowly identified obsolete
failure state once, and changing the Guesty client ID reschedules pending
Guesty writes because the new application can have different reservation
visibility. Obsolete persisted route-selection state is removed because every
application now uses only the documented v3 notes route. Neither recovery path
rotates or discards the stored six-digit PIN, clears confirmed Guesty state, or
bypasses the shared two-writes-per-30-seconds budget. Persisted retries that
remain deferred are summarized safely in the startup log and diagnostics.

Changing the code path that resolves a persisted failure does not invalidate an
already stored backoff automatically. The release must increment the matching
retry-state migration version and test a fixture from the immediately previous
version. Otherwise Home Assistant can load the corrected code but remain silent
until every old retry timestamp expires. The migration clears only retry
metadata, preserves each exact private PIN, and still feeds the shared bounded
write queue.

## Guest door-access portal

### KB-ACCESS-001 — Link authorization is server-owned and fail-closed

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/access.py`, `tests/test_access.py`

One opaque bearer URL is published per eligible reservation. Each selected
listing can expose one to six server-mapped Home Assistant `lock.*` entities;
unused slots are omitted. The browser sends only a door index and can never
choose an arbitrary entity ID.

`GET` renders only. Unlock requires `POST`, a time-bucket CSRF nonce, bounded
body, five-second per-door cooldown, at most ten actions per minute per door,
and a 15-second service timeout. Every action revalidates token, reservation
fingerprint, active status, mapping, `[check-in - early, check-out + late)`
window, and snapshot freshness.

### KB-ACCESS-002 — Door-link rotation and cleanup rules

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/access.py`, `tests/test_access.py`

Tokens are unguessable and HMAC-bound to a private secret, reservation,
permission version, and relevant access inputs. Only token hashes are indexed.
Permission, timing, mapping, or field-identity changes rotate the link;
translation-only label changes do not.

Revocation occurs locally before Guesty field cleanup. Cleanup uses persistent
bounded backoff and seven-day tombstones. A transient error must not create a
rotate/recreate loop. A stale snapshot beyond the configured threshold blocks
new unlock actions, while a single failed poll may continue from the last safe
state.

### KB-ACCESS-003 — Portal localization and branding contract

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/access.py`,
  `custom_components/guesty/access_names.py`,
  `custom_components/guesty/access_branding.py`,
  `tests/test_access_names.py`, `tests/test_access_branding.py`

The portal selects German, English, Spanish, or French from
`Accept-Language`, with English fallback. Door labels may be configured in all
four languages. Established English capitalization includes “Door Access” and
“Access Unavailable.” A successful unlock leaves buttons visible and shows a
localized five-second notification.

Home Assistant external URL, logo, and favicon must be credential-free HTTPS.
Branding origins are narrowly added to CSP. Reverse proxies must not cache or
log token-bearing `/api/guesty/access/` paths.

## Loxone

### KB-LOXONE-001 — Loxone provisioning is optional and listing-scoped

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/loxone.py`,
  `custom_components/guesty/loxone_api.py`, `tests/test_loxone.py`,
  `tests/test_loxone_api.py`

A listing maps to one HTTPS Miniserver and one or more normal user groups.
Administrative, built-in, Config-capable, or user-management-capable groups are
not selectable. Use a dedicated least-privilege service account with user
management rights, never an everyday administrator account.

The Guesty Keycode may exist far in advance, but the Loxone user is created
only within the configured provisioning lead, default six hours before allowed
access. It uses the shared early/late validity window, timespan state, and
remote auto-delete.

### KB-LOXONE-002 — Existing users are updated, not replaced

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/loxone.py`, `tests/test_loxone.py`

The reservation-derived user UUID and confirmed PIN remain stable across time
changes. Validity, label, and groups update in place. Moving outside the lead
removes the user and later recreates it with the same Guesty PIN. Moving to
another listing/server cleans the old target before provisioning the new one.
Guest name is sent only when privacy opt-in is enabled; otherwise the booking
ID is the label.

Loxone collision results `201` and `409` are not success. Tentative users are
deleted, the confirmed Guesty PIN remains unchanged, and a persistent conflict
waits for a manual Guesty edit.

## TTLock

### KB-TTLOCK-001 — TTLock reuses the shared PIN and is listing-scoped

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/ttlock.py`,
  `custom_components/guesty/ttlock_api.py`, `tests/test_ttlock.py`,
  `tests/test_ttlock_api.py`

TTLock is an independent optional delivery target, not another Guesty/PIN
owner. A listing maps to one to six V4 locks with `keyboardPwdVersion=4` and an
online gateway (`hasGateway=1`). Only allowlisted Open Platform regions are
accepted.

The TTLock App password is used once for OAuth and not stored. Tokens are
private and bound to region, client ID, and username. TTLock's mandated OAuth
protocol uses an MD5 digest of that password with `usedforsecurity=False`; this
is protocol compatibility, not a general password-hashing choice.

### KB-TTLOCK-002 — Passcodes are independently recoverable per lock

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/ttlock.py`, `tests/test_ttlock.py`

Provision only inside the configured lead with the shared access window.
Persist each lock's success independently so an offline gateway cannot cause
duplicate creation elsewhere. Ambiguous non-idempotent creates recover through
a privacy-safe reservation marker and remote lookup.

Before change/delete, verify that the remote passcode still carries the
expected marker; never modify a foreign or manually renamed object. Reconcile
drift at most every 30 minutes and coalesce reads per lock. Remote code
collisions never rotate a confirmed Guesty PIN.

## Security, reliability, and Home Assistant behavior

### KB-SAFE-001 — HTTP bodies must be read to EOF under hard limits

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/_http.py`,
  `tests/test_http.py`, fragmented-response tests in `tests/test_api.py`,
  `tests/test_loxone_api.py`, and `tests/test_ttlock_api.py`

`aiohttp` may return a valid response body in fragments. Every external client
must use `_http.async_read_limited` until EOF and enforce a hard size limit. A
single `response.read(n)` is not equivalent and previously caused valid JSON
to be reported as malformed. Current limits are 10 MiB for Guesty and 1 MiB
for Loxone/TTLock.

### KB-SAFE-002 — Non-idempotent creates require recovery

- Status: Validated
- Last validated: 2026-07-28
- Evidence: webhook, Loxone, and TTLock create/recovery tests in
  `tests/test_api.py`, `tests/test_loxone.py`, and `tests/test_ttlock.py`

Do not blindly retry webhook creation, Loxone user creation, or TTLock
passcode creation after an ambiguous response. Persist an in-progress marker
and recover through a safe remote lookup before any second create.

### KB-SAFE-003 — Logs, diagnostics, and URLs are constrained

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/diagnostics.py`,
  `tests/test_storage_diagnostics.py`, safe error-context tests in
  `tests/test_api.py`

Never expose OAuth tokens, client secrets, passwords, PINs, bearer links, guest
names, confirmation codes, private endpoints, unbounded response bodies, or
full reservation IDs. Guesty support diagnostics may contain a hashed
reservation marker, safe method/path label, status, bounded `x-request-id`,
retry metadata, and rate-limit headroom.

External service URLs must be HTTPS and must not contain credentials. TTLock
hosts are allowlisted rather than user-arbitrary.

### KB-SAFE-004 — Stale data blocks grants but not scheduled cleanup

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/access.py`,
  `custom_components/guesty/loxone.py`,
  `custom_components/guesty/ttlock.py`, provider stale-data tests

When Guesty data exceeds the configured stale threshold, do not create new
access, extend access, or accept a door unlock. Previously stored end times
still own revocation and remote cleanup so an outage cannot leave access active
past the last confirmed checkout. Recovery uses bounded backoff and resumes
without a manual integration reload.

### KB-HA-001 — Configuration is UI-driven and preserves blank secrets

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `custom_components/guesty/config_flow.py`,
  `tests/test_config_flow.py`

Setup, reauthentication, credential replacement, and all optional features are
configured through Home Assistant flows. A blank password/client-secret field
for an unchanged identity means keep the stored secret; a changed identity
requires fresh validation. Replacement Guesty credentials must belong to the
same account so options, mappings, door links, and private state remain valid.

Repeated or stale frontend submissions may contain extra keys. Home
Assistant's suggested-value helper can rebuild a Voluptuous schema with
`PREVENT_EXTRA`; flows that intentionally tolerate stale submissions must
restore `REMOVE_EXTRA`. Failing to do so previously produced “extra keys not
allowed” followed by “Unknown error.”

### KB-FAILOVER-001 — Backup operation is active/passive only

- Status: Validated
- Last validated: 2026-07-28
- Evidence: current manager ownership design, `README.md`

Exactly one Home Assistant instance may be the active writer. During deliberate
failover, the replacement instance adopts the stable native Guesty Keycode and
may overwrite the old door-link custom field with its own URL. Active/active is
unsupported because parallel writers can compete over URLs, remote objects,
PIN conflicts, and API capacity. A separate shared custom field is not a safe
coordination mechanism.

## Retired assumptions

### KB-RET-001 — Reservation custom fields are not the PIN authority

- Status: Retired
- Last validated: 2026-07-28
- Superseded by: KB-PIN-001
- Evidence: `custom_components/guesty/loxone.py`, `README.md`

Older releases stored PINs in a configurable reservation custom field. Do not
restore that fallback. Reservation custom fields remain valid only for the
separate door-access URL.

### KB-RET-002 — Legacy reservation reads cannot validate native Keycodes

- Status: Retired
- Last validated: 2026-07-28
- Superseded by: KB-GUESTY-002
- Evidence: redacted live API reproduction on 2026-07-28,
  `tests/test_api.py::test_native_keycode_reads_use_batched_v3_array_responses`

Adding `notes.keyCode` to a legacy field projection does not make the legacy
endpoint return it. Do not infer an empty Keycode from that response.

### KB-RET-003 — Confirmed PINs must not rotate automatically

- Status: Retired
- Last validated: 2026-07-28
- Superseded by: KB-PIN-001
- Evidence: `tests/test_loxone.py`

Automatic rotation after a duplicate, remote collision, sparse response, or
temporary Guesty error is unsafe because the guest may already have received
the confirmed code. Fail closed and wait for a manual Guesty correction.

### KB-RET-004 — UTC timestamps are not always the highest-priority times

- Status: Retired
- Last validated: 2026-07-28
- Superseded by: KB-RES-002
- Evidence:
  `tests/test_models.py::test_planned_times_override_stale_utc_timestamps`

Guesty can retain stale `checkIn`/`checkOut` timestamps after a manual
`plannedArrival`/`plannedDeparture` edit. Valid planned local times therefore
take precedence.

### KB-RET-005 — The legacy general updater cannot write native Keycodes

- Status: Retired
- Last validated: 2026-07-28
- Superseded by: KB-GUESTY-003
- Evidence: production log and Guesty UI notifications on 2026-07-28,
  `tests/test_api.py::test_native_keycode_404_verifies_reservation_without_legacy_write`

The general `PUT /v1/reservations/{reservationId}` endpoint can acknowledge a
payload containing `notes.keyCode` and emit reservation-change notifications
without persisting the Keycode. Never treat HTTP success from this endpoint as
a compatibility route, even with a later v3 read-back.

## Validation and release knowledge

### KB-REL-001 — Supported validation baseline

- Status: Validated
- Last validated: 2026-07-28
- Evidence: `.github/workflows/validate.yml`, `requirements-test.txt`,
  `custom_components/guesty/manifest.json`

The integration targets Home Assistant 2025.12 or newer and currently has no
third-party runtime dependency; Home Assistant supplies `aiohttp` and
`voluptuous`. CI validates Python 3.13 and 3.14. A full local release check runs
pytest with warnings as errors and at least 65% coverage, Ruff check/format,
compileall, Bandit, runtime dependency audit, `pip check`, JSON parsing, and
`git diff --check`.

An explicit publication request is required. A HACS release is complete only
after the manifest version is bumped, full validation passes, `main` is pushed,
Python and CodeQL/security checks pass, and a matching `vX.Y.Z` GitHub release
tag points to the same commit and manifest version.

## Review register

| Date | Scope | Result |
| --- | --- | --- |
| 2026-07-28 | Initial project-wide knowledge extraction from all production modules, configuration constants, README, and regression-test inventory | Established validated architecture, API, lifecycle, security, provider, and retired-assumption records |
| 2026-07-28 | Focused Guesty Reservations-v3 Keycode compatibility validation for v2.2.9 | Confirmed exact live v3 ID mapping and sparse-note behavior; production disproved the legacy fallback because it emitted change events without persisting Keycode, so only the documented v3 route remains |
| 2026-07-28 | Post-release v2.2.9 retry-state diagnosis | Production log showed 40 corrected v2.2.8 404 records still waiting on old backoff timestamps; added a version-2-to-3 migration that immediately requeues them without PIN rotation or a write burst |
