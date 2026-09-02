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

- Last project-wide implementation review: 2026-07-29
- Project-wide implementation baseline: integration version 2.2.10 working tree
- Last knowledge-base consistency review: 2026-08-14
- Knowledge-base baseline: integration version 2.4.6
- Consistency-review scope: all entry IDs, metadata, cross-references, evidence
  paths and explicit test names, plus affected current code, README, AGENTS.md,
  and official Guesty API documentation
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

## Knowledge index

The fixed IDs below are permanent. Status is recorded on the linked entry;
retired assumptions are isolated in their own area.

| Area | Entries |
| --- | --- |
| Architecture and ownership | [ARCH-001](#kb-arch-001), [ARCH-002](#kb-arch-002), [ARCH-003](#kb-arch-003), [ARCH-004](#kb-arch-004), [ARCH-005](#kb-arch-005) |
| Guesty synchronization and API | [GUESTY-001](#kb-guesty-001), [GUESTY-002](#kb-guesty-002), [GUESTY-003](#kb-guesty-003), [GUESTY-004](#kb-guesty-004), [GUESTY-005](#kb-guesty-005), [GUESTY-006](#kb-guesty-006), [GUESTY-007](#kb-guesty-007), [GUESTY-008](#kb-guesty-008), [GUESTY-009](#kb-guesty-009), [GUESTY-010](#kb-guesty-010), [GUESTY-011](#kb-guesty-011) |
| Reservation and entity semantics | [RES-001](#kb-res-001), [RES-002](#kb-res-002), [RES-003](#kb-res-003), [RES-004](#kb-res-004) |
| Shared PIN lifecycle | [PIN-001](#kb-pin-001), [PIN-002](#kb-pin-002), [PIN-003](#kb-pin-003), [PIN-004](#kb-pin-004), [PIN-005](#kb-pin-005), [PIN-006](#kb-pin-006) |
| Door-access portal | [ACCESS-001](#kb-access-001), [ACCESS-002](#kb-access-002), [ACCESS-003](#kb-access-003) |
| Loxone | [LOXONE-001](#kb-loxone-001), [LOXONE-002](#kb-loxone-002) |
| TTLock | [TTLOCK-001](#kb-ttlock-001), [TTLOCK-002](#kb-ttlock-002) |
| Security and reliability | [SAFE-001](#kb-safe-001), [SAFE-002](#kb-safe-002), [SAFE-003](#kb-safe-003), [SAFE-004](#kb-safe-004) |
| Home Assistant configuration | [HA-001](#kb-ha-001) |
| Failover | [FAILOVER-001](#kb-failover-001) |
| Retired assumptions | [RET-001](#kb-ret-001), [RET-002](#kb-ret-002), [RET-003](#kb-ret-003), [RET-004](#kb-ret-004), [RET-005](#kb-ret-005) |
| Validation and release | [REL-001](#kb-rel-001) |

## System boundaries and ownership

<a id="kb-arch-001"></a>

### KB-ARCH-001 — One shared Guesty synchronization owner

- Status: Validated
- Last validated: 2026-07-31
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

<a id="kb-arch-002"></a>

### KB-ARCH-002 — Runtime setup and teardown are transactional

- Status: Validated
- Last validated: 2026-09-02
- Evidence: `custom_components/guesty/__init__.py`,
  `custom_components/guesty/scheduler.py`,
  `tests/test_init.py::test_partial_setup_failure_rolls_back_started_resources`,
  `tests/test_init.py::test_first_refresh_failure_shuts_down_coordinator`,
  `tests/test_init.py::test_remove_entry_cleans_every_private_and_remote_resource`,
  `tests/test_scheduler.py::test_shutdown_cancels_transition_already_in_flight`,
  provider unload tests in `tests/test_loxone.py` and `tests/test_ttlock.py`

`__init__.py` owns startup and teardown order. If setup fails after any
manager, timer, webhook, listener, platform, or background task has started,
everything already started must be rolled back. Unload and reload must cancel
owned tasks; no worker may survive its config entry. The exact occupancy
transition is tracked as an owned task and canceled even when unload overlaps
an already-fired timer. Loxone and TTLock clear pending follow-up work and task
ownership during unload; their debounced loops retain a late pending signal
until another owned pass can consume it.

<a id="kb-arch-003"></a>

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

<a id="kb-arch-004"></a>

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

<a id="kb-arch-005"></a>

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

<a id="kb-guesty-001"></a>

### KB-GUESTY-001 — Polling, full sync, and webhook traffic model

- Status: Validated
- Last validated: 2026-09-02
- Evidence: `custom_components/guesty/const.py`,
  `custom_components/guesty/coordinator.py`,
  `tests/test_coordinator.py::test_targeted_webhook_exposes_custom_field_change`,
  `tests/test_coordinator.py::test_cancellation_webhook_prunes_without_pin_enrichment`,
  `tests/test_coordinator.py::test_reservation_webhook_is_durable_before_worker_runs`,
  `tests/test_coordinator.py::test_pending_webhook_queue_resumes_after_restart`,
  `tests/test_coordinator.py::test_new_webhook_wakes_worker_waiting_for_later_retry`,
  `tests/test_loxone.py::test_native_write_webhook_echo_does_not_create_write_loop`,
  [Guesty reservation webhooks](https://open-api-docs.guesty.com/docs/webhooks-reservations),
  [Guesty webhook v2 events](https://open-api-docs.guesty.com/changelog/2026-03-12)
- External status: Current public v2 webhook contract
- Last externally checked: 2026-08-14

The default reservation poll is 300 seconds. Reservation polls are incremental
with a five-minute overlap, plus a daily full reservation sync. Listing safety
sync is daily while a signed webhook is active and at least every 15 minutes
when it is unavailable. Supported newly registered events are
`reservation.created.v2`, `reservation.updated.v2`, `listing.new`,
`listing.updated`, and `listing.removed`.

Webhooks are the fast path. A verified reservation event is written to the
private atomic general cache before Home Assistant acknowledges it. The queue
coalesces duplicate events for the same reservation by generation, preserves
distinct reservations in a burst, survives restart, and wakes immediately for
new work. Every queued reservation receives its own exact read; temporary API
or projection failure retries on minute boundaries through minute five and
then at the normal poll cadence. A newer generation cannot be completed or
delayed by an older read that was already in flight. Sufficient listing
payloads are applied directly, missing listing details alone are fetched,
removed listings are pruned immediately, and a new listing requests
reservations only for that listing.

Guesty exposes reservation alterations through the general
`reservation.updated.v2` event rather than separate Keycode, custom-field, or
cancellation event types. A targeted refresh therefore reads both enabled PIN
mirrors for an active mapped reservation, allowing a manual edit to reach PIN
reconciliation and existing Loxone/TTLock objects. A signed inactive-status
hint revokes local access immediately while the durable targeted read remains
queued to confirm Guesty's final state. An inactive exact result is merged out
and deliberately skips optional PIN enrichment, shortening the path to
provider cleanup. Guesty can also emit
this event for the integration's own confirmed PIN PUT. Per-source confirmed
baselines make that read-back idempotent; equal values perform no further PUT,
while duplicate deliveries are coalesced. The normal poll remains the safety
net because webhook delivery is not an exactly-once or hard real-time contract.

A targeted listing or reservation update does not prove that the complete
reservation snapshot is fresh. It therefore preserves the last successful
global reservation timestamp, listing safety-scan timestamp, stale/degraded
state, and prior API error. Only a successful normal reservation synchronization
resets global cache age and freshness; only a complete listing read advances the
listing safety cursor.

<a id="kb-guesty-002"></a>

### KB-GUESTY-002 — Native Keycode reads combine V2 and V3 backing models

- Status: Validated
- Last validated: 2026-09-02
- Evidence: redacted live API reproduction on 2026-07-28,
  `custom_components/guesty/api.py::async_get_reservations`,
  `custom_components/guesty/api.py::async_get_reservation_key_codes`,
  `tests/test_api.py::test_reservation_poll_reads_pin_sources_only_for_mapped_listings`,
  `tests/test_api.py::test_optional_v2_pin_read_failure_keeps_fresh_reservations`,
  `tests/test_api.py::test_sparse_per_reservation_pin_projection_is_classified_by_source`,
  `tests/test_api.py::test_native_keycode_reads_use_batched_v3_array_responses`,
  `tests/test_coordinator.py::test_disabled_native_keycode_source_skips_v3_enrichment`,
  `tests/test_coordinator.py::test_full_poll_enriches_mapped_reservations_from_v3`,
  `tests/test_coordinator.py::test_sparse_v3_channel_response_selects_observed_v2_keycode`,
  `tests/test_coordinator.py::test_channel_sparse_v3_result_does_not_force_repeated_full_polls`,
  `tests/test_coordinator.py::test_v3_keycode_failure_keeps_fresh_base_reservation_data`,
  `tests/test_coordinator.py::test_targeted_missing_pin_projection_keeps_base_update_fail_closed`,
  `tests/test_loxone.py::test_empty_channel_reservation_generates_pin_on_confirmed_v2_route`

Guesty has two native-Keycode projections. V2-backed, legacy, and
channel-imported reservations expose a top-level `keyCode` through the normal
`GET /v1/reservations` collection and exact `GET /v1/reservations/{id}` calls;
a controlled live read after the validated V2 write returned the exact value in
both surfaces when `_id keyCode` was projected. V3-backed reservations expose
`notes.keyCode` through `GET /v1/reservations-v3` using repeated
`reservationIds[]` parameters. That endpoint accepts at most ten reservation
IDs per request. Guesty identifies returned v3 reservations with either
`reservationId`, `_id`, or `id`. A redacted live comparison of 35 future
reservations proved that the v3 IDs match the internal legacy reservation IDs.

The base reservation request deliberately excludes both PIN-bearing fields.
Inside the same coordinator refresh, one minimal V2 projection requests only
`_id`, `listingId`, and the enabled `keyCode`/`customFields` sources, filtered
to Loxone/TTLock-mapped listings. Only active mapped reservations receive the
bounded V3 enrichment. This is not an independent poller. Disabled sources and
unmapped listings cause no PIN-field reads.

A populated explicit V2 Keycode is preserved when the alternate V3 response is
empty or sparse. Guesty's V2 projection can omit an empty requested `keyCode`;
an exact returned V2 reservation records that surface as observed while leaving
the route undecided. If the paired V3 response returns the exact reservation
without its V3-only `notes` container, the coordinator safely classifies the
reservation as V2-backed. A reservation missing entirely from either result is
still unreadable. If native synchronization is disabled, V2 `keyCode` and V3
notes reads are both skipped and the custom field may remain the sole PIN
authority. Failure of either optional enrichment does not discard fresh base
dates or statuses. The failed source is marked unreadable for that in-memory
snapshot so reconciliation cannot fan out into per-reservation reads, generate
a replacement against unknown remote state, or blindly overwrite it. Another
populated mirror or a confirmed private baseline may still keep providers
operational. A non-secret persistent retry marker makes the next scheduled
shared refresh a full reservation read even after restart; only a successful
full PIN enrichment clears that marker.

<a id="kb-guesty-003"></a>

### KB-GUESTY-003 — Native Keycode writes are route-matched and confirmed

- Status: Validated
- Last validated: 2026-08-04
- Evidence: redacted live API reproduction on 2026-07-28,
  `custom_components/guesty/api.py`,
  `tests/test_api.py::test_native_keycode_uses_minimal_v3_notes_payload`,
  `tests/test_api.py::test_native_keycode_v3_404_falls_back_to_confirmed_v2_write`,
  `tests/test_api.py::test_native_keycode_requires_exact_success_confirmation`

The validated V3 write route is
`PUT /v1/reservations-v3/{reservationId}/notes` with only
`{"notes":{"keyCode":"..."}}`. Some Guesty applications can read the exact
existing reservation through v3 while that dedicated notes route returns HTTP
404 because the reservation uses the older backing model. The integration
verifies the exact reservation through v3 and preserves the failed PUT's
`x-request-id` before selecting the V2 route.

A native write is successful only after Guesty confirms the exact reservation
ID and value through an authoritative response or bounded read-back. Resource
IDs must be validated before they are interpolated into paths. The independently
verified reservation custom field described by `KB-PIN-001` is a redundant PIN
mirror, not a native-route substitute.

Guesty Engineering confirmed that V2-created, legacy, and channel-imported
reservations instead use `PUT /v1/reservations/{id}` with the minimal top-level
payload `{"keyCode":"..."}`. `KB-GUESTY-011` records the live validation. The
integration probes V3 first only when the backing model is unknown, then uses
V2 only when a second persistent write slot was reserved. A confirmed route is
cached per reservation, never account-wide. Each route requires an exact
matching response or bounded route-matched read-back; HTTP 200 alone is
insufficient.

<a id="kb-guesty-004"></a>

### KB-GUESTY-004 — Sparse and empty projections have different meanings

- Status: Validated
- Last validated: 2026-09-02
- Evidence: `custom_components/guesty/models.py`,
  `custom_components/guesty/api.py`,
  `custom_components/guesty/loxone.py`,
  `tests/test_models.py::test_omitted_notes_are_not_treated_as_an_empty_native_keycode`,
  `tests/test_coordinator.py::test_v3_observed_empty_keycode_remains_authoritative`,
  `tests/test_coordinator.py::test_sparse_v3_keycode_response_is_not_an_observed_deletion`,
  `tests/test_api.py::test_targeted_sparse_pin_projection_does_not_fan_out`,
  `tests/test_api.py::test_optional_v2_pin_read_failure_keeps_fresh_reservations`,
  `tests/test_loxone.py::test_webhook_omitted_custom_projection_confirms_empty_before_staging_pin`,
  `tests/test_loxone.py::test_webhook_omitted_custom_projection_exact_read_failure_retries_safely`,
  `tests/test_loxone.py::test_poll_recovers_existing_booking_with_omitted_empty_custom_projection`

An omitted `notes` object means the V3 Keycode was not observed. It is not proof
that a user deleted the field and must not rotate or revoke an otherwise known
PIN. A requested V3 reservation missing from the response is marked temporarily
unreadable. A returned exact V3 reservation without `notes` selects the V2 route
only when the same refresh also observed that exact reservation through the V2
Keycode projection; without that pair it remains unreadable. The same
distinction applies to a sparse reservation custom-field projection: an
omitted requested `customFields` container is marked unreadable rather than
silently treated as empty.

One narrow confirmation path prevents newly created channel reservations from
remaining blocked forever when Guesty's successful exact V2 row omits an empty
`customFields` container. When that omission is source-classified, no private
canonical PIN exists, and the enabled native mirror is either disabled or was
independently observed empty, reconciliation performs one dedicated read of
the configured custom field. A confirmed empty result may stage the PIN; a
failure remains fail-closed and receives a bounded retry. Whole projection
failures, missing reservation rows, unknown native state, and records with an
existing canonical PIN never use this exception and continue through the
shared refresh path, avoiding a per-reservation fan-out.

An explicitly empty or invalid value is different from an omitted projection:
it is observed input and is repaired from the reservation's saved confirmed
PIN according to `KB-PIN-001`. This applies to one or both mirrors. An
explicitly populated top-level V2 `keyCode` must not be erased merely because
the alternate V3 projection is empty or sparse.

<a id="kb-guesty-005"></a>

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

<a id="kb-guesty-006"></a>

### KB-GUESTY-006 — Authentication and transient failures recover in place

- Status: Validated
- Last validated: 2026-07-31
- Evidence: `custom_components/guesty/api.py`,
  `custom_components/guesty/__init__.py`,
  `custom_components/guesty/config_flow.py`,
  `custom_components/guesty/coordinator.py`,
  `tests/test_init.py::test_setup_reuses_and_then_removes_transient_token`,
  `tests/test_config_flow.py::test_user_flow_stores_first_token_for_setup`,
  `tests/test_api.py::test_oauth_rate_limit_defers_without_sleep_or_repeat`,
  `tests/test_api.py::test_persisted_oauth_cooldown_survives_restart_and_expires`,
  `tests/test_api.py::test_concurrent_token_checks_create_only_one_token`,
  `tests/test_api.py::test_late_unauthorized_response_reuses_newer_token`,
  `tests/test_api.py::test_credential_validation_reuses_token_and_fetches_one_listing`,
  `tests/test_config_flow.py::test_reauth_reuses_private_token_for_unchanged_credentials`,
  `tests/test_config_flow.py::test_reauth_honors_private_oauth_cooldown_without_network`,
  `tests/test_config_flow.py::test_changed_secret_keeps_client_cooldown_but_not_previous_token`,
  `tests/test_config_flow.py::test_reconfigure_accepts_new_oauth_client_for_same_guesty_account`,
  `tests/test_config_flow.py::test_reauth_rejects_credentials_for_another_account`,
  `tests/test_config_flow.py::test_loaded_reauth_uses_update_listener_without_second_reload`,
  `tests/test_coordinator.py::test_oauth_rate_limit_starts_from_cache_in_degraded_state`,
  `tests/test_coordinator.py::test_auth_failure_starts_reauthentication`,
  `tests/test_coordinator.py::test_successful_api_sync_aborts_stale_reauthentication_flow`,
  `tests/test_coordinator.py::test_degraded_cached_sync_keeps_reauthentication_flow`,
  [Guesty Authentication](https://open-api-docs.guesty.com/docs/authentication)
- External status: Current public OAuth contract
- Last externally checked: 2026-08-14

OAuth refresh is shared and serialized. Guesty documents that an expired
Bearer token can be returned as HTTP `403`, while other paths can use `401`.
Every authenticated request first checks the cached expiration. The first
`401` or `403` response refreshes the shared token exactly once and retries the
original request; concurrent rejections of the same token consume only one
token request. Only a rejection with the fresh token, or a permanent credential
rejection from the OAuth endpoint, starts Home Assistant reauthentication. A
repeated `403` remains a real permission failure and is not retried in a loop.

Retry-safe transient failures use bounded exponential backoff and
`Retry-After`; permanent failures are not blindly retried. During a transient
outage the last valid snapshot remains visible as degraded/stale, and normal
operation resumes automatically.

Guesty Client ID and Client Secret are stable application credentials; the
derived Bearer access token is the expiring value. Reuse the shared token
lifecycle rather than repeatedly minting tokens: Guesty's OAuth endpoint can
issue at most five tokens per Client ID in a 24-hour period and can rate-limit
repeated token requests with a long `Retry-After`.

An HTTP `429` from the OAuth endpoint is handled differently from an ordinary
short in-request retry. The client performs no sleep/retry loop inside Home
Assistant config-entry setup. It records a bounded absolute cooldown in the
private general cache and returns immediately. Existing cached entities start
in a visible degraded state; without cached listings, setup exits quickly after
persisting the same deadline. Coordinator polls and Home Assistant restarts
before that deadline fail locally without another OAuth request. Once the
deadline passes, the shared client permits exactly one serialized token request
and normal polling clears the persisted cooldown.

The reauthentication and credential-replacement dialogs participate in the
same private token lifecycle. If Client ID and Client Secret are unchanged,
validation reuses the cached Bearer token and expiration instead of minting a
diagnostic token, and always honors the stored OAuth cooldown. Rotating only
the secret preserves the cooldown for that Client ID but never reuses the old
Bearer token. A different Client ID receives no old authentication state for
requests. Guesty's issued JWT contains the stable `accountId`;
`/accounts/me` describes the current API user and its `_id` can change when a
new Open API application is created. Therefore config-entry identity is the
SHA-256 fingerprint of the token's `accountId`, never the `/accounts/me` user
ID. During a Client-ID replacement, the previous cached token may be decoded
locally only to compare that account claim. It is never sent, trusted for API
access, or reused by the new client. The stable fingerprint is persisted
privately after validation so later credential rotations do not depend on the
old token remaining valid.
Before Home Assistant accepts credentials, validation proves the same API
surfaces required by the coordinator: account identity, listings, and
reservations.

A successful live coordinator synchronization is proof that a previously
started reauthentication repair is stale and aborts that active flow, which
also removes Home Assistant's repair issue. A degraded result assembled from
cache is not proof of recovery and must leave the repair intact.

#### Mandatory Guesty login and reauthentication state machine

Future authentication work must preserve this exact sequence:

1. **Initial credential validation.** Normalize Client ID and Client Secret and
   reject empty values locally. With no existing entry, request one OAuth
   access token, then prove access to `/accounts/me`, `/listings`, and
   `/reservations`. Derive the config-entry identity from the issued token's
   stable `accountId`, hash it before storage, and never substitute the current
   API user's `/accounts/me` `_id` for this account identity.
2. **Token handoff without a second login.** Put the token and expiration
   produced by validation into the new config entry only as a transient setup
   handoff. Runtime startup must reuse that exact token for its first sync,
   persist the current token, expiration, and cooldown in the private general
   store, then remove token and expiration from config-entry data. Client ID and
   Client Secret remain the stable application credentials.
3. **Normal authenticated requests.** Reuse a cached token while it is valid
   beyond the refresh margin. A missing or expiring token may trigger one
   refresh guarded by the shared async token lock. Concurrent callers and a
   late `401`/`403` for an older token must observe and reuse a newer token
   rather than mint another one.
4. **Rejected Bearer token.** On the first `401` or `403`, invalidate the
   rejected token, refresh once, and retry the original request once. A fresh
   token rejected with `401` is an authentication failure; a fresh token
   rejected with `403` is a permission failure. Neither result may enter an
   automatic refresh loop.
5. **Transient failure and OAuth `429`.** Retry only retry-safe transport,
   `408`, `429`, and server failures with bounded backoff. An OAuth `429` never
   sleeps inside config-entry setup and never starts reauthentication. Persist
   its bounded absolute `Retry-After` deadline privately, return cached data as
   degraded when available, and reject every pre-deadline refresh locally
   without network traffic. After expiry, allow exactly one serialized token
   request and clear the cooldown only on success.
6. **Reauthentication or proactive credential replacement.** Reuse private auth
   state only when the submitted Client ID matches the configured Client ID.
   If both Client ID and Secret are unchanged, reuse token, expiration, and
   cooldown. If only the Secret changes, retain the Client ID cooldown but
   never reuse the old Bearer token. If the Client ID changes, reuse none of the
   old auth state for requests. It is permitted to decode the prior token
   locally, without sending it, solely to compare its stable `accountId` with
   the newly validated token. Accept replacement credentials only after the
   three endpoint checks succeed and that account identity matches the existing
   entry; then migrate older user-based unique IDs to the stable account hash.
7. **Reload and repair completion.** Updating credentials has exactly one
   reload owner: the update listener for a loaded entry, or one explicitly
   scheduled reload for an unloaded entry. A successful live coordinator sync
   aborts any stale reauthentication flow and removes its Home Assistant repair
   issue. Cached or degraded data must not claim authentication recovery.

This sequence prevents OAuth token amplification, restart-based cooldown
bypass, false credential-expiry repairs, account switching, duplicate reloads,
and repair notices that survive after Guesty access has actually recovered.

<a id="kb-guesty-007"></a>

### KB-GUESTY-007 — Incoming webhooks are authenticated before work

- Status: Validated
- Last validated: 2026-08-14
- Evidence: `custom_components/guesty/webhook.py`,
  `tests/test_webhook.py::test_invalid_stale_and_replayed_signatures_are_rejected`,
  `tests/test_webhook.py::test_signature_rejects_incomplete_headers_timestamp_and_short_secret`,
  `tests/test_webhook.py::test_missing_secret_and_stream_size_failures_are_rejected`,
  `tests/test_webhook.py::test_signed_invalid_json_is_rejected`,
  `tests/test_webhook.py::test_failed_webhook_handoff_can_be_retried`,
  `tests/test_webhook.py::test_existing_remote_subscription_is_reused`,
  `tests/test_coordinator.py::test_webhook_registration_recovers_with_bounded_backoff`,
  `tests/test_coordinator.py::test_shutdown_cancels_webhook_registration_recovery`

Before accepting work, the Home Assistant endpoint enforces the Guesty/Standard
Webhooks HMAC, timestamp tolerance, request-size limit, and replay/message ID.
An event is marked complete only after the coordinator accepts the handoff so a
failed handoff can be retried. Existing legacy subscriptions without a signing
secret are migrated once; failure must fall back to polling rather than enter a
delete/create loop. A transient registration or secret-lookup failure starts one
owned retry task with exponential backoff capped at one hour. Once active, the
same task verifies the remote subscription hourly. An unchanged health check
does not rewrite the Home Assistant config entry or trigger a reload; a failed
check returns to bounded recovery. Success restores push delivery automatically
and config-entry unload cancels the task.

<a id="kb-guesty-008"></a>

### KB-GUESTY-008 — Live v3 responses are sparse and a notes 404 is route-specific

- Status: Validated
- Last validated: 2026-07-29
- Evidence: redacted live API and production-log reproduction on 2026-07-28,
  `custom_components/guesty/api.py`,
  `tests/test_api.py::test_native_keycode_accepts_v3_success_using_internal_id`,
  `tests/test_api.py::test_native_keycode_v3_404_falls_back_to_confirmed_v2_write`

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
is missing. Production then proved that the general V2
`PUT /v1/reservations/{reservationId}` can return success and trigger Guesty
notifications while silently ignoring a wrongly nested `notes.keyCode`
payload. Guesty Support and `KB-GUESTY-011` later confirmed that the same V2
route uses the different top-level `keyCode` payload. The integration now
verifies the exact v3 reservation, preserves the original request ID, and uses
the confirmed V2 shape only inside the shared persistent write budget.

The confirmed readable-reservation 404 is reservation-specific for scheduling
purposes. If a second slot was reserved it may be consumed immediately by the
V2 fallback; otherwise the per-reservation route cache schedules V2 for the
next bounded pass. Global
authentication, permission, transport, payload, rate-limit, and server failures
may still stop the batch.

A separate local test did successfully write and read one test reservation
through the documented v3 route. It used a different OAuth client and a
different reservation from the production failures. Per `KB-GUESTY-009`, that
is not an application-permission comparison and must not be used to attribute
the 404 to differing OAuth rights.

In a later controlled A/B reproduction with the same OAuth client and request
shape, Guesty returned 404 for the dedicated notes PUT of one readable future
`airbnb2` reservation, then accepted and confirmed an idempotent PUT of the
existing Keycode for one future `manual` reservation. This disproves an
account-wide outage, authentication failure, or generally invalid payload for
that reproduction. It narrows the remaining fault to reservation-specific or
source-specific Guesty routing/backend behavior. One pair is not sufficient to
claim that every Airbnb reservation is affected; collect more redacted
same-client comparisons before generalizing by booking source.

Another guarded same-client A/B reproduction changed, rather than merely
rewrote, the numeric portion of two already populated future Keycodes. The
reservations used different sources (`manual` and `be-api`), retained their
existing non-numeric display suffixes, and each dedicated v3 notes PUT returned
HTTP 200. Exact Reservations-v3 read-back confirmed both changed values. The
two permits were slightly more than 30 seconds apart and exhausted the
two-attempt test limit. This proves that the documented endpoint supports real
Keycode changes for those two future reservations and source types; it does not
explain or invalidate the separate readable-`airbnb2` reservation's 404.

A subsequent targeted probe used that same newly validated OAuth client against
the previously failing future `airbnb2` reservation. An exact v3 preflight
again returned the reservation without an existing Keycode. After the mandatory
30-second guard, one attempt to initialize `notes.keyCode` returned HTTP 404;
the captured request ID was retained outside the repository for Guesty
support. Exact v3 read-back still returned the same reservation and no Keycode.
No second PUT was issued. Because the same client had just confirmed real
Keycode changes on the `manual` and `be-api` controls, this reproduction rules
out a client-wide permission, authentication, endpoint, or payload problem and
isolates the failure to Guesty's handling of that reservation or an equivalent
reservation-specific backend condition.

A separate guarded probe selected an exact readable, confirmed, future
`Booking.com` reservation whose native Keycode was empty. With the same OAuth
client and request shape used by the successful `manual` and `be-api` controls,
one initialization attempt after the mandatory 30-second wait returned HTTP
404. Exact Reservations-v3 read-back still found the reservation but no
Keycode; no second PUT was issued. This establishes that `be-api` is not a
Booking.com control and that at least one Booking.com reservation exhibits the
same route-specific failure as the tested `airbnb2` reservation. It is still
insufficient evidence that every Booking.com or Airbnb reservation is affected.

An earlier comparison attempt was deferred because Guesty's OAuth endpoint
returned HTTP 429 with a long `Retry-After`; the later A/B reproduction above
ran only after that window had passed. Diagnostic tooling must still reuse one
derived token per session instead of minting a token for every request.

<a id="kb-guesty-009"></a>

### KB-GUESTY-009 — Guesty OAuth clients have equal configured rights

- Status: Validated
- Last validated: 2026-07-28
- Evidence: Guesty account-owner confirmation and Guesty API support
  correspondence on 2026-07-28; redacted local and production API
  reproductions recorded in `KB-GUESTY-008`

All OAuth clients used for this Guesty account are configured with exactly the
same API rights. A different Client ID must therefore not be presented as
evidence of different permissions, scopes, reservation visibility, or endpoint
entitlement without a concrete response proving that difference.

A local Keycode write succeeded with one OAuth client and one test reservation,
while production writes using another client and different reservations
returned 404. Those observations changed two variables at once and do not prove
that the Client ID caused the difference. The remaining causes include
reservation-specific behavior or an upstream Guesty routing/backend defect.
Isolating the cause requires the same internal reservation ID, method, path,
payload, and timing to be tested with both clients. Until that comparison
exists, do not recommend changing Guesty credentials as the root-cause fix.

That isolation has since been completed for the known failing reservation: a
new client successfully changed two other future Keycodes but received the same
route-specific 404 for the exact readable future `airbnb2` reservation. Client
credentials are therefore no longer a plausible explanation for that case.

<a id="kb-guesty-010"></a>

### KB-GUESTY-010 — Live Keycode probes use a hard write guard

- Status: Validated
- Last validated: 2026-08-04
- Evidence: `scripts/guesty_live_write_guard.py`,
  `tests/test_live_write_guard.py::test_live_token_cache_reuses_token_across_process_instances`,
  token-cache security and expiry tests in `tests/test_live_write_guard.py`

Manual and agent-driven tests against Guesty's live native-Keycode endpoint use
the repository guard rather than issuing a direct PUT. The guard is armed only
after read-only preflight and enforces a full 30-second wait before the first
write. A test run can consume no more than two write attempts. The optional
second attempt requires an explicit diagnosis acknowledgement and another full
30-second wait. Failed or ambiguous requests consume an attempt because the
remote outcome may be unknown.

The last permit is recorded atomically before network I/O in private,
cross-process state. Consequently, restarting a shell or launching a second
test process cannot create writes less than 30 seconds apart.

The same helper owns a private persistent OAuth cache. Every related live-test
process resolves its token through `GuestyLiveTokenCache` before preflight.
The cache holds the access token and absolute expiration under mode `0600`,
stores only a SHA-256 credential-context fingerprint rather than raw Client ID
or Client Secret, uses an atomic replace, and holds a cross-process lock across
the cache check and the single token fetch. A matching token remains reusable
until the refresh margin; changed credentials or an expiring token require one
new fetch. Malformed state fails closed without calling the fetcher, preventing
token amplification after a corrupt file. Diagnostic code must stop on OAuth
rate-limiting rather than bypassing this cache. These deliberately stricter
manual-test controls do not replace the integration runtime's persistent token
lifecycle or global two-writes-per-30-seconds queue.

<a id="kb-guesty-011"></a>

### KB-GUESTY-011 — V2-created reservations use a top-level Keycode

- Status: Validated
- Last validated: 2026-08-14
- Evidence: Guesty Engineering support correspondence on 2026-08-04;
  [Guesty V2 update reservation](https://open-api-docs.guesty.com/reference/put_reservations-id);
  guarded redacted live reproduction using
  `scripts/guesty_live_write_guard.py`
- External status: Deprecated public endpoint retained as a support-required
  legacy/channel compatibility contract
- Last externally checked: 2026-08-14

Guesty Engineering states that
`PUT /v1/reservations-v3/{reservationId}/notes` is intended for reservations
created through the V3 flow, while V2-created, legacy, and channel-imported
reservations must be altered through the V2 reservation API. This explains the
previous source-specific V3 404 pattern. The public V2 update endpoint defines
its request body only as an arbitrary object and does not document the Keycode
shape or an authoritative Keycode read-back. Guesty API Support subsequently
provided and tested the missing contract: V2 updates use a top-level
`{"keyCode":"..."}` payload, not the V3-style
`{"notes":{"keyCode":"..."}}` payload.

A guarded live test selected an exact readable, confirmed, future `airbnb2`
reservation. Its V3 response omitted `notes`. After the mandatory 30-second
wait, `PUT /v1/reservations/{id}` with a preserved mutable-notes object and a
temporary `keyCode` returned HTTP 200. Four exact V3 reads over the following
15 seconds never projected the temporary value. The second and final guarded
write restored the original empty value and also returned HTTP 200; subsequent
V3 reads remained sparse, so restoration could not be positively confirmed
through that surface. Both request IDs were retained outside the repository.

A second guarded end-to-end V2 test used the same future source class and one
OAuth token. Before writing, the full `GET /v1/reservations/{id}`, a
`fields=notes` projection, and a `fields=notes.keyCode` projection all returned
HTTP 200 while omitting both `notes` and `keyCode`. The V2 PUT again returned
HTTP 200. Four bounded rounds of all three V2 reads over 15 seconds still
omitted the temporary value. The second guarded PUT restored the original
empty value and returned HTTP 200; all V2 reads again matched the original
absence. The write and restore request IDs remain outside the repository.

A third isolated V2 test targeted an exact confirmed future `airbnb2`
reservation whose native Keycode was empty. It sent a plain six-digit ASCII
value with no configured suffix or Unicode character. The single guarded
`PUT /v1/reservations/{id}` again returned HTTP 200, but its response did not
confirm `notes.keyCode`; four bounded exact V3 read-backs over 15 seconds still
omitted the value, and a subsequent manual check found the Keycode empty in the
Guesty UI. This rules out the check-mark suffix as the cause of the silent V2
no-op. Guesty API Support later identified the actual cause: all three probes
used the wrong nested V3 payload shape against the V2 updater. HTTP 200 did not
mean that Guesty accepted or persisted that unsupported field shape.

For a V2-created or channel-imported reservation, the support-confirmed write
contract is `PUT /v1/reservations/{id}` with a minimal top-level
`{"keyCode":"..."}` body. Do not wrap it in `notes`. An HTTP-200 response alone
is insufficient proof of persistence because Guesty silently accepted the
previously unsupported nested shape.

A final controlled test targeted an exact confirmed future `bookingCom`
reservation with an empty native Keycode. One guarded V2 PUT used the minimal
top-level payload, including the configured suffix. Guesty returned HTTP 200
and the exact reservation plus Keycode in the write response; the user then
confirmed the same value in Guesty's UI. A subsequent read-only verification
requested top-level `keyCode` from both `GET /v1/reservations` and exact
`GET /v1/reservations/{id}`; both returned the exact stored value. This
validates the V2 write and read contracts and proves that the suffix was
accepted. Production support may therefore include top-level `keyCode` in the
existing V2 poll and use this write route for V2-backed reservations, but must
retain the global write budget, avoid unrelated reservation fields, require
exact response/read-back confirmation, and cache the confirmed route per
reservation. V3-backed reservations continue to use the dedicated V3 notes
endpoint and nested notes payload.

Guesty's public reference now marks the general V2 update endpoint as
deprecated in favor of dedicated Reservations-v3 alteration endpoints. Guesty
Engineering nevertheless requires this V2 route for legacy and
channel-imported reservations that are not writable through the V3 notes
model. Treat it as a monitored compatibility dependency: retain route-matched
tests and exact confirmation, and do not remove it until Guesty provides and
validates a replacement that can update those reservations.

## Reservation, time, and entity semantics

<a id="kb-res-001"></a>

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

<a id="kb-res-002"></a>

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

<a id="kb-res-003"></a>

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

<a id="kb-res-004"></a>

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

<a id="kb-pin-001"></a>

### KB-PIN-001 — Native Keycode and a custom field are reconciled mirrors

- Status: Validated
- Last validated: 2026-08-13
- Evidence: `custom_components/guesty/loxone.py`,
  `tests/test_loxone.py::test_existing_custom_field_pin_is_adopted_and_fills_native`,
  `tests/test_loxone.py::test_custom_only_mode_ignores_native_keycode_completely`,
  `tests/test_loxone.py::test_native_only_mode_ignores_custom_field_completely`,
  `tests/test_loxone.py::test_confirmed_v2_route_is_cached_while_custom_mirror_stays_active`,
  `tests/test_loxone.py::test_simultaneous_different_manual_edits_prefer_native_keycode`,
  `tests/test_loxone.py::test_both_empty_guesty_mirrors_restore_saved_code`,
  `tests/test_loxone.py::test_queued_saved_pin_repair_keeps_existing_provider_access`,
  `tests/test_loxone.py::test_invalid_guesty_edit_restores_saved_code`,
  `tests/test_loxone.py::test_manual_duplicate_restores_editor_previous_saved_code`,
  `tests/test_loxone.py::test_persisted_legacy_duplicate_conflict_restores_source_baseline`,
  `tests/test_config_flow.py::test_options_flow_preserves_legacy_pin_custom_field_suggestion`,
  manual-source edit, mismatch, sparse-response, and retry tests

Every reservation PIN supports up to two independently enabled Guesty mirrors:
Guesty's native Keycode (V2 top-level `keyCode` or V3 `notes.keyCode`, depending
on the reservation backing model) and a configurable reservation custom field
whose default is `{{door_code}}`. The field reference accepts a safe ID,
display name, or `{{variable}}`. Both sources retain independent confirmed
baselines, sync flags, error reasons, and retry state in the private store.
Each mirror is independently configurable and both default to enabled. At least
one must remain enabled whenever Loxone or TTLock is active. A disabled mirror
is excluded from reads, writes, merge/conflict decisions, retry scheduling, and
aggregate readiness. Its last confirmed private baseline is retained only so a
later re-enable can reconcile safely without rotating the numeric PIN. Runtime
also fails closed if malformed legacy options disable both sources.
An older `loxone_custom_field` option remains a read-only migration fallback
until the next options save, so a previously selected non-default field is not
silently replaced. Saving options moves the normalized non-blank reference to
the shared `pin_custom_field` key and removes the legacy key.

The deterministic merge rules are:

1. Both empty and no confirmed private PIN: generate one PIN once.
2. Exactly one populated and no confirmed private PIN: adopt it and fill only
   the empty mirror.
3. Both equal: adopt without a write.
4. Exactly one differs from its source baseline: treat that valid unique manual
   edit as canonical and propagate it to the other mirror and providers.
5. One or both explicitly emptied after confirmation: restore the saved PIN to
   every enabled mirror; do not delete or rotate it.
6. Both changed to different values, or an unexplained initial mismatch: native
   Keycode is authoritative and is propagated to the custom mirror. A mirror
   that is provably stale because its write is pending or failed cannot use this
   tie-break to revert a newer confirmed edit.
7. Invalid or duplicate input: reject that input and restore the reservation's
   last safe confirmed PIN. The established owner of a duplicate keeps its
   value. If a newly observed reservation has no safe stored PIN yet, generate
   one unique PIN once.

Once either mirror has confirmed the canonical six digits, Loxone/TTLock may
proceed. Failure of one Guesty endpoint does not block the other successful
mirror. The failed mirror is retried with bounded persistent backoff, and its
old value cannot be mistaken for a later manual edit. Confirmed digits never
rotate automatically. Empty/invalid/duplicate repair and ordinary propagation
share the global Guesty write budget, so recovery cannot create a write burst.
Existing provider access remains on the saved PIN while Guesty is repaired.

<a id="kb-pin-002"></a>

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

<a id="kb-pin-003"></a>

### KB-PIN-003 — Both Guesty PIN mirrors share one persistent budget

- Status: Validated
- Last validated: 2026-08-04
- Evidence: `custom_components/guesty/loxone.py`,
  `tests/test_loxone.py::test_two_keycodes_are_written_per_30_second_window`,
  `tests/test_loxone.py::test_bulk_pin_migration_is_prioritized_and_bounded`,
  `tests/test_loxone.py::test_guesty_phase_finishes_for_all_bookings_before_provider_io`,
  `tests/test_loxone.py::test_custom_backfill_is_prioritized_by_nearest_check_in`,
  `tests/test_loxone.py::test_ended_confirmed_reservation_cannot_block_future_custom_mirror`,
  `tests/test_loxone.py::test_keycode_endpoint_failure_prioritizes_custom_fallback`,
  `tests/test_loxone.py::test_guesty_write_budget_preserves_reported_api_headroom`,
  `tests/test_loxone.py::test_write_rechecks_rate_headroom_immediately_before_put`,
  `tests/test_loxone.py::test_exhausted_guesty_headroom_queues_without_a_put`,
  `tests/test_api.py::test_bounded_write_does_not_replay_after_rejected_token`,
  `tests/test_loxone.py::test_persisted_guesty_write_limit_survives_manager_restart`

All native and custom-field PIN publication paths share a persistent global
limit of at most two
write attempts in any 30-second window. Failed and ambiguous writes consume a
slot. Queue passes, webhook work, reservation-specific retries, source
migration, suffix changes, and restarts must not bypass it. Current stays,
verified webhook/manual changes, first confirmations, imminent arrivals, and
redundancy backfill form the priority order. Queue age promotes waiting work
every five minutes so distant reservations cannot starve. Guesty API headroom
remains reserved throughout.

Every V2 native, V3 native, or custom-field PUT consumes exactly one slot. An
unknown V3 route may fall back to V2 only after two slots were persisted in
advance; a confirmed one-attempt result refunds the demonstrably unused slot.
Each slot reserves API headroom for one PUT plus up to three bounded
confirmation reads. PIN PUTs, confirmation reads, and route-discovery reads do
not perform hidden authentication or transport retries. An HTTP 401/403
invalidates the rejected token so the next separately budgeted operation
refreshes it first; no request in the write envelope is replayed invisibly.
Within a newly generated reservation, one mirror is confirmed before its
redundant mirror may consume a second slot. Across reservations, every eligible
booking receives its first confirmed Guesty mirror before lower-priority
redundancy backfill consumes capacity, with current stays and nearer arrivals
ordered first. Guesty reconciliation for the complete eligible set is one
persisted phase before any potentially slow Loxone provider I/O; a provider
timeout therefore cannot postpone another booking's Guesty code. Reservations
whose complete configured access
window has ended do not participate in PIN generation, mirror repair, conflict
ownership, or queue priority even when Guesty still labels them `confirmed`;
their remote-provider and private-state cleanup remains mandatory. An
application-wide
notes-endpoint, authentication, permission, transport, or payload failure may
stop the current batch so the remaining slot and normal Guesty synchronization
headroom are not wasted on the same predictable error. A reservation-specific
missing-record response does not starve the second bounded write.

Guesty's per-second, per-minute, per-hour, per-day, and generic rate-limit
remainders are retained separately for diagnostics. Write scheduling uses the
most constrained available minute/hour/day window and always retains four
requests for normal Guesty traffic. The persistent two-writes-per-30-seconds
limit is already far below the second allowance, so a transiently low second
bucket does not pause work until the next reservation poll. The generic header
has no explicit window and is therefore diagnostic-only. With fewer than eight
requests in a long window, one complete write envelope no longer fits; with
fewer than twelve, an unknown-route two-slot fallback does not fit. Headroom is
recalculated immediately before every PUT because reads earlier in the same
pass may have reduced it. When no envelope fits, managers wait at least until
the next configured reservation poll instead of creating an immediate
no-traffic loop. A later response without valid long-window headers clears
obsolete headroom rather than retaining a stale block indefinitely.

<a id="kb-pin-004"></a>

### KB-PIN-004 — Plaintext lifetime is deliberately bounded

- Status: Validated
- Last validated: 2026-09-02
- Evidence: `custom_components/guesty/loxone.py`,
  `custom_components/guesty/storage.py`,
  `tests/test_storage_diagnostics.py`,
  `tests/test_loxone.py::test_cancel_removes_plaintext_before_retrying_remote_cleanup`

The privacy-filtered general cache never stores Keycodes. The private shared
PIN store holds plaintext only while required for delivery and cleanup.
Cancellation or access end removes the canonical PIN, confirmed/display
values, both source baselines, and rejected/duplicate-code history before
attempting remote cleanup. Cleanup ownership IDs, provider snapshots, and
retry state remain so an outage cannot orphan a managed object. Both Guesty
mirrors remain as booking documentation. Status sensors report safe states,
per-mirror booleans, counts, times, and reasons, never the PIN.

<a id="kb-pin-005"></a>

### KB-PIN-005 — Persistent Guesty retries recover across API migrations

- Status: Validated
- Last validated: 2026-08-04
- Evidence: `custom_components/guesty/loxone.py`,
  `tests/test_loxone.py::test_setup_recovers_persisted_native_404_backoff_once`,
  `tests/test_loxone.py::test_v3_retry_state_migrates_endpoint_failure_to_v2_route`,
  `tests/test_loxone.py::test_v3_retry_state_does_not_misclassify_generic_rejection_as_v2`,
  `tests/test_loxone.py::test_recovered_404_backlog_resumes_through_global_write_budget`,
  `tests/test_loxone.py::test_setup_recovers_guesty_backoff_after_client_change`

Guesty Keycode write failures retain bounded persistent retry state across
restarts. A versioned migration may reschedule a narrowly identified obsolete
failure state once, and changing the Guesty client ID reschedules pending
Guesty writes so retry state created under the previous credential context
cannot remain silently deferred. This recovery behavior is not evidence that
the OAuth clients have different rights; `KB-GUESTY-009` records that their
configured rights are equal. Obsolete account-wide route-selection state is
removed. Confirmed native routes are stored per reservation because one Guesty
account may contain both V2- and V3-backed bookings.
Only the specific `guesty_keycode_endpoint_unavailable` result from a proven
V3-notes 404 plus exact V3 read may seed V2 during migration. A generic
`guesty_keycode_rejected` retry is requeued but remains route-unknown. When
route discovery cannot reserve the fallback PUT immediately, it caches V2 and
uses the next shared write slot instead of entering generic failure backoff.
Neither recovery path rotates or discards the stored six-digit PIN, clears
confirmed Guesty state, or bypasses the shared two-writes-per-30-seconds
budget. Persisted retries that remain deferred are summarized safely in the
startup log and diagnostics.

Changing the code path that resolves a persisted failure does not invalidate an
already stored backoff automatically. The release must increment the matching
retry-state migration version and test a fixture from the immediately previous
version. Otherwise Home Assistant can load the corrected code but remain silent
until every old retry timestamp expires. The migration clears only retry
metadata, preserves each exact private PIN, and still feeds the shared bounded
write queue.

<a id="kb-pin-006"></a>

### KB-PIN-006 — New webhook PINs use a durable five-minute publication window

- Status: Validated
- Last validated: 2026-09-02
- Evidence: `custom_components/guesty/coordinator.py`,
  `custom_components/guesty/loxone.py`,
  `tests/test_coordinator.py::test_duplicate_reservation_webhooks_are_coalesced`,
  `tests/test_coordinator.py::test_webhook_retry_state_survives_normal_poll_interval`,
  `tests/test_coordinator.py::test_newer_webhook_generation_owns_inflight_queue_result`,
  `tests/test_loxone.py::test_webhook_pin_is_persisted_immediately_but_first_written_after_one_minute`,
  `tests/test_loxone.py::test_webhook_omitted_custom_projection_confirms_empty_before_staging_pin`,
  `tests/test_loxone.py::test_webhook_omitted_custom_projection_exact_read_failure_retries_safely`,
  `tests/test_loxone.py::test_poll_recovers_existing_booking_with_omitted_empty_custom_projection`,
  `tests/test_loxone.py::test_webhook_pin_fast_retry_runs_each_minute_then_uses_normal_backoff`,
  `tests/test_loxone.py::test_webhook_pin_first_write_delay_survives_manager_restart`

The verified event itself first enters the durable queue described by
`KB-GUESTY-001`. A missing reservation or temporarily unreadable PIN projection
does not discard the event, and a restart resumes the exact targeted work.
After that reservation has been fetched and at least one enabled
PIN source has been successfully observed empty, a new unique PIN is allocated
immediately in the private atomic PIN store. A failed or unread source still
blocks blind generation under `KB-PIN-001`; an already populated source is
adopted instead of being replaced. Existing confirmed reservations and ordinary
poll discoveries keep their established reconciliation behavior.

Guesty's exact V2 projection can omit an empty `customFields` container. The
narrow source-classified confirmation in `KB-GUESTY-004` counts as a successful
empty observation for this staging rule. A failed dedicated confirmation does
not allocate a PIN and schedules a bounded retry instead.

The first Guesty PUT for that newly staged PIN is persistently deferred until
one minute after webhook receipt. If publication fails, the affected mirror is
retried on the next absolute minute boundary through minute five. A failure at
or after the fifth-minute boundary starts the ordinary persistent failure
backoff at count one; fast-window failures do not inflate that later backoff.
Home Assistant reloads retain the PIN, first-write boundary, and retry window.
Successful confirmation of every enabled mirror clears the temporary schedule.

Native Keycode and custom-field writes remain independent mirrors and continue
to share `KB-PIN-003`: no webhook path may exceed two Guesty PUT attempts in any
30-second window or consume Guesty's reserved API headroom. When traffic or API
headroom cannot support a minute boundary, the global limiter may postpone it;
the fast window never authorizes a write burst or a second poller.

## Guest door-access portal

<a id="kb-access-001"></a>

### KB-ACCESS-001 — Link authorization is server-owned and fail-closed

- Status: Validated
- Last validated: 2026-08-07
- Evidence: `custom_components/guesty/access.py`,
  `tests/test_access.py::test_expired_cache_or_changed_reservation_fails_closed`

One opaque bearer URL is published per eligible reservation. Each selected
listing can expose one to six server-mapped Home Assistant `lock.*` entities;
unused slots are omitted. The browser sends only a door index and can never
choose an arbitrary entity ID.

`GET` renders only. Unlock requires `POST`, a time-bucket CSRF nonce, bounded
body, five-second per-door cooldown, at most ten actions per minute per door,
and a 15-second service timeout. Every action revalidates token, reservation
fingerprint, active status, mapping, `[check-in - early, check-out + late)`
window, and snapshot freshness.

The public page deliberately keeps every denial generic. Runtime diagnostics
retain only fixed privacy-safe rejection reason identifiers, timestamps, and
counts. They never include the bearer URL, token, guest data, raw reservation
or listing IDs, or request-specific access-window values.

<a id="kb-access-002"></a>

### KB-ACCESS-002 — Door-link rotation and cleanup rules

- Status: Validated
- Last validated: 2026-08-07
- Evidence: `custom_components/guesty/access.py`,
  `tests/test_access.py::test_guest_name_change_does_not_rotate_access_link`,
  `tests/test_access.py::test_door_label_change_does_not_rotate_access_link`,
  `tests/test_access.py::test_legacy_fingerprint_migrates_without_rotating_access_link`,
  `tests/test_access.py::test_changed_legacy_authorization_state_rotates_access_link`,
  `tests/test_access.py::test_booking_time_change_still_rotates_access_link`,
  `tests/test_access.py::test_active_remote_drift_is_repaired_without_token_rotation`,
  `tests/test_access.py::test_unknown_token_schedules_one_bounded_current_link_audit`,
  `tests/test_access.py::test_remote_link_audit_is_bounded_and_prioritized`

Tokens are unguessable and HMAC-bound to a private secret, reservation,
permission version, and relevant access inputs. Only token hashes are indexed.
Permission, timing, mapping, or field-identity changes rotate the link;
guest-name changes and every human-readable door-label change do not. Guest
names and labels are presentation data rather than authorization inputs. A
versioned fingerprint removes them while retaining listing ID, active state,
the exact access window, ordered lock entity IDs, and custom-field reference.
An exactly matching legacy presentation-sensitive fingerprint is migrated in
place without changing the token or republishing the URL; an unexplained legacy
mismatch still rotates fail-closed.

Revocation occurs locally before Guesty field cleanup. Cleanup uses persistent
bounded backoff and seven-day tombstones. A transient error must not create a
rotate/recreate loop. A stale snapshot beyond the configured threshold blocks
new unlock actions, while a single failed poll may continue from the last safe
state.

Guesty's remote custom field is also a mutable mirror rather than permanent
proof of the local record. Current stays are read back at the normal five-minute
reservation interval, stays beginning within 24 hours are checked hourly, and
more distant future links daily. Each pass checks at most two records, keeps
normal Guesty headroom, and prioritizes current access before the nearest
future stay. A missing or different remote value is repaired by republishing
the current local URL without rotating its still-valid bearer token. Remote
read failure preserves the last confirmed local authorization and uses
persistent backoff. An otherwise valid-format unknown public token can request
one current-stay audit, but this path is rate-limited to once per five minutes
and performs no network I/O in the HTTP request itself. This closes drift from
old/backup Home Assistant writers or later Guesty field changes without turning
random public tokens into Guesty traffic amplification.

<a id="kb-access-003"></a>

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

<a id="kb-loxone-001"></a>

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

<a id="kb-loxone-002"></a>

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

<a id="kb-ttlock-001"></a>

### KB-TTLOCK-001 — TTLock reuses the shared PIN and is listing-scoped

- Status: Validated
- Last validated: 2026-09-02
- Evidence: `custom_components/guesty/ttlock.py`,
  `custom_components/guesty/ttlock_api.py`, `tests/test_ttlock.py`,
  `tests/test_ttlock_api.py`,
  `tests/test_config_flow.py::test_ttlock_reconfigure_blank_password_uses_live_oauth_session`,
  `tests/test_ttlock.py::test_reconfigure_validation_persists_rotated_live_tokens`,
  `tests/test_ttlock.py::test_reconfigure_validation_keeps_rotated_token_after_list_failure`,
  `tests/test_ttlock.py::test_live_token_callback_persists_before_reconcile_completion`,
  `tests/test_ttlock_api.py::test_rotated_token_is_persisted_at_refresh_boundary`,
  `tests/test_ttlock_api.py::test_concurrent_expiry_checks_mint_one_ttlock_token`,
  `tests/test_ttlock.py::test_successful_reauthentication_requeues_auth_failures`

TTLock is an independent optional delivery target, not another Guesty/PIN
owner. A listing maps to one to six V4 locks with `keyboardPwdVersion=4` and an
online gateway (`hasGateway=1`). Only allowlisted Open Platform regions are
accepted.

Change and delete operations are successful only when their HTTP-200 JSON
contains an explicit integer `errcode` equal to zero. A missing or malformed
code is ambiguous and fails closed; cleanup ownership is retained for retry.

The TTLock App password is used once for OAuth and not stored. Tokens are
private and bound to region, client ID, and username. TTLock's mandated OAuth
protocol uses an MD5 digest of that password with `usedforsecurity=False`; this
is protocol compatibility, not a general password-hashing choice.

TTLock issues a replacement refresh token whenever a session is refreshed.
Every runtime refresh and password exchange invokes an awaited persistence
callback at the token-rotation boundary, before later API work can fail or a
restart can lose the replacement. Concurrent or sequential pre-expiry checks
recheck inside one token lock and mint at most one token for the same expiry.
An options flow for an unchanged account must validate through the live manager
client; a temporary options client must never consume the worker's refresh
token and discard its replacement. When the stored session really is invalid,
one password-based OAuth exchange is adopted into both the live client and
private store immediately. All records paused with `authentication_failed`
have their backoff cleared and are scheduled for immediate reconciliation; the
Guesty PIN and confirmed access window remain unchanged.

<a id="kb-ttlock-002"></a>

### KB-TTLOCK-002 — Passcodes are independently recoverable per lock

- Status: Validated
- Last validated: 2026-09-02
- Evidence: `custom_components/guesty/ttlock.py`, `tests/test_ttlock.py`,
  `tests/test_ttlock.py::test_current_stay_uses_one_persisted_retroactive_ttlock_start`,
  `tests/test_ttlock.py::test_planned_time_change_is_pending_until_ttlock_confirms_new_window`,
  `tests/test_ttlock.py::test_current_stay_planned_time_change_preserves_clamp_and_updates_checkout`,
  `tests/test_ttlock.py::test_upgrade_preserves_confirmed_current_stay_start`,
  `tests/test_ttlock.py::test_missing_confirmed_code_is_recreated_with_current_start`,
  `tests/test_ttlock.py::test_first_offline_lock_does_not_block_later_mapped_locks`,
  [TTLock period-passcode documentation](https://euopen.ttlock.com/doc/api/v3/keyboardPwd/get)

Provision only inside the configured lead with the shared access window.
Each lock has its own exception boundary and persists success independently, so
an offline gateway or collision does not block later mapped locks or cause
duplicate creation on an already successful sibling. The reservation remains
partial and retries only incomplete delivery. Ambiguous non-idempotent creates
recover through a privacy-safe reservation marker and remote lookup.

Before change/delete, verify that the remote passcode still carries the
expected marker; never modify a foreign or manually renamed object. Reconcile
drift at most every 30 minutes and coalesce reads per lock. Remote code
collisions never rotate a confirmed Guesty PIN.

The private record fingerprints the currently desired Guesty access window,
and every lock independently stores the exact window most recently confirmed
through TTLock. The 30-minute verification shortcut and the `provisioned`
status apply only while those fingerprints match. A new reservation object
containing changed `plannedArrival` or `plannedDeparture` therefore becomes
pending immediately, updates the existing TTLock passcode ID, and returns to
ready only after exact remote read-back. This separate proof also prevents a
recently verified old period from hiding a fresh Guesty schedule change.

If TTLock is first enabled or recovers only after an eligible stay has already
started, transmitting the historical access start can make a newly delivered
period code immediately invalid under TTLock's first-use timing rules. For each
previously undelivered lock, clamp the remote start once to the current
reconciliation time and persist it before the non-idempotent create. Every
retry, partial multi-lock continuation, and Home Assistant restart reuses that
exact value; the confirmed checkout end is never extended. A healthy passcode
confirmed by an older release keeps its original booking start. The versioned
state migration requeues only matching active-stay `ttlock_api_error` records
once and preserves PINs, tokens, ownership IDs, and global traffic limits.

## Security, reliability, and Home Assistant behavior

<a id="kb-safe-001"></a>

### KB-SAFE-001 — HTTP bodies and success envelopes are bounded and validated

- Status: Validated
- Last validated: 2026-09-02
- Evidence: `custom_components/guesty/_http.py`,
  `tests/test_http.py`, fragmented-response tests in `tests/test_api.py`,
  `tests/test_loxone_api.py`, and `tests/test_ttlock_api.py`,
  `tests/test_api.py::test_pagination_rejects_structurally_invalid_http_200`,
  `tests/test_api.py::test_invalid_reservation_row_does_not_discard_valid_sibling`

`aiohttp` may return a valid response body in fragments. Every external client
must use `_http.async_read_limited` until EOF and enforce a hard size limit. A
single `response.read(n)` is not equivalent and previously caused valid JSON
to be reported as malformed. Current limits are 10 MiB for Guesty and 1 MiB
for Loxone/TTLock.

HTTP 200 is not proof of a valid snapshot. Guesty collection responses must be
a list or contain a list-valued `results`; malformed or populated pages with no
valid objects fail instead of masquerading as an empty account. Within an
otherwise valid page, one malformed reservation is isolated and logged without
discarding valid siblings. An exact malformed reservation response is an API
failure, never a missing/cancelled booking.

<a id="kb-safe-002"></a>

### KB-SAFE-002 — Non-idempotent creates require recovery

- Status: Validated
- Last validated: 2026-07-28
- Evidence: webhook, Loxone, and TTLock create/recovery tests in
  `tests/test_api.py`, `tests/test_loxone.py`, and `tests/test_ttlock.py`

Do not blindly retry webhook creation, Loxone user creation, or TTLock
passcode creation after an ambiguous response. Persist an in-progress marker
and recover through a safe remote lookup before any second create.

<a id="kb-safe-003"></a>

### KB-SAFE-003 — Logs, diagnostics, and URLs are constrained

- Status: Validated
- Last validated: 2026-09-02
- Evidence: `custom_components/guesty/diagnostics.py`,
  `custom_components/guesty/models.py::reservation_log_marker`,
  `tests/test_storage_diagnostics.py`, `tests/test_models.py`,
  `tests/test_api.py::test_retry_debug_log_omits_resource_path_and_error_detail`,
  safe error-context tests in `tests/test_api.py`

Never expose OAuth tokens, client secrets, passwords, PINs, bearer links, guest
names, confirmation codes, private endpoints, unbounded response bodies, or
full reservation IDs in logs, diagnostics, support exports, or the knowledge
base. Operational logs use the shared 12-character SHA-256 reservation marker.
Guesty support diagnostics may contain that marker, a safe method/path label,
status, bounded `x-request-id`, retry metadata, and rate-limit headroom.
Webhook diagnostics expose only queue counts, timestamps, and fixed reason
identifiers; queued reservation IDs and payloads remain private.

Config-entry diagnostic options use a reviewed positive allowlist. Branding
URLs, listing-keyed suffixes, mappings, custom-field references, provider
configuration, and any future unreviewed option are excluded by default.
Retry debug logs use only the safe first-segment endpoint label and exception
class; they never interpolate a resource-bearing path or upstream error text.

Documented Home Assistant events may contain a reservation ID for local
automation matching. Event payloads are an intentional user-local interface and
must not be copied into logs or diagnostics.

External service URLs must be HTTPS and must not contain credentials. TTLock
hosts are allowlisted rather than user-arbitrary.

<a id="kb-safe-004"></a>

### KB-SAFE-004 — Offline grants use only an exact confirmed snapshot

- Status: Validated
- Last validated: 2026-07-31
- Evidence: `custom_components/guesty/access.py`,
  `custom_components/guesty/loxone.py`,
  `custom_components/guesty/ttlock.py`, exact-window provider outage tests

When Guesty data exceeds the configured stale threshold, never generate a PIN,
infer a reservation, accept a door-page unlock, or extend a validity window.
The PIN-provider offline option is enabled by default and may create/update a
Loxone user or TTLock passcode only when the private store contains a PIN and
reservation/listing snapshot confirmed during a fresh Guesty pass. The provider
must use exactly the stored start and end; a modified stale projection cannot
extend it. Strict operators may disable offline provisioning, which blocks all
new provider grants while stale. Previously stored end times own revocation in
both modes. Recovery uses bounded backoff and needs no manual reload.

<a id="kb-ha-001"></a>

### KB-HA-001 — Configuration is UI-driven and preserves blank secrets

- Status: Validated
- Last validated: 2026-07-31
- Evidence: `custom_components/guesty/config_flow.py`,
  `custom_components/guesty/__init__.py`,
  `tests/test_config_flow.py::test_reauth_updates_credentials_and_token`,
  `tests/test_config_flow.py::test_options_flow_uses_modern_config_entry_property`,
  `tests/test_config_flow.py::test_loaded_reauth_uses_update_listener_without_second_reload`

Setup, reauthentication, credential replacement, and all optional features are
configured through Home Assistant flows. A blank password/client-secret field
for an unchanged identity means keep the stored secret; a changed identity
requires fresh validation. Replacement Guesty credentials must belong to the
same account so options, mappings, door links, and private state remain valid.
The comparison uses the stable `accountId` claim from Guesty's issued token;
`/accounts/me` identifies the current API user and must not be used to reject a
new Open API application for the same Guesty account.

Repeated or stale frontend submissions may contain extra keys. Home
Assistant's suggested-value helper can rebuild a Voluptuous schema with
`PREVENT_EXTRA`; flows that intentionally tolerate stale submissions must
restore `REMOVE_EXTRA`. Failing to do so previously produced “extra keys not
allowed” followed by “Unknown error.”

Every frontend form schema must also pass `voluptuous_serialize.convert` with
Home Assistant's custom serializer. A plain custom Python validator used as the
schema value for the configurable PIN field caused the entire options dialog
to return HTTP 500 even though runtime synchronization remained healthy.
Serializable `vol.All`/`vol.Length` validation now defines the form contract;
whitespace normalization and the stricter semantic check run inside the flow
step. Regression tests must serialize the root options schema, not merely
submit it through the in-process flow manager.

Credential updates have one reload owner. A loaded entry is reloaded by its
registered update listener; an entry that failed setup and is not loaded is
scheduled explicitly by the config flow. Combining that listener with Home
Assistant's update-and-reload helper creates duplicate, racing reloads and is
forbidden.

<a id="kb-failover-001"></a>

### KB-FAILOVER-001 — Backup operation is active/passive only

- Status: Validated
- Last validated: 2026-07-31
- Evidence: current manager ownership design, `README.md`

Exactly one Home Assistant instance may be the active writer. During deliberate
failover, the replacement instance adopts the stable value from the Guesty PIN
mirrors and repairs an empty mirror. It may overwrite the old door-link custom
field with its own URL. Active/active is
unsupported because parallel writers can compete over URLs, remote objects,
PIN conflicts, and API capacity. A third shared custom field is not a safe
active-writer coordination mechanism; the PIN mirrors store the stable
business value, not a lease or leader lock.

## Retired assumptions

<a id="kb-ret-001"></a>

### KB-RET-001 — Native Keycode is the sole PIN authority

- Status: Retired
- Last validated: 2026-07-31
- Superseded by: KB-PIN-001
- Evidence: `custom_components/guesty/loxone.py`, `README.md`

Rejected assumption: native `notes.keyCode` is the sole PIN authority.
Releases through v2.2 treated it as the only source and destination. Guesty can
reject the native write route for individual channel reservations, so the
configured PIN custom field is now a fully verified redundant mirror with
deterministic per-source baselines. It remains separate from the door-access
URL custom field.

<a id="kb-ret-002"></a>

### KB-RET-002 — V2 reads cannot validate native Keycodes

- Status: Retired
- Last validated: 2026-07-28
- Superseded by: KB-GUESTY-002, KB-GUESTY-011
- Evidence: redacted live API reproduction on 2026-07-28,
  `tests/test_api.py::test_native_keycode_reads_use_batched_v3_array_responses`

Rejected assumption: a V2 read cannot validate a native Keycode. Adding
`notes.keyCode` to a V2 projection does not return the V3 notes field, but V2
can validate its separate top-level `keyCode` instead.

<a id="kb-ret-003"></a>

### KB-RET-003 — Confirmed PINs may rotate automatically

- Status: Retired
- Last validated: 2026-07-28
- Superseded by: KB-PIN-001
- Evidence: `tests/test_loxone.py`

Rejected assumption: a confirmed PIN may rotate automatically after a
duplicate, remote collision, sparse response, or temporary Guesty error. That
is unsafe because the guest may already have received the confirmed code.
Guesty-side empty, invalid, and duplicate input is repaired with that same
confirmed code according to `KB-PIN-001`; provider-side collisions still fail
closed without generating a replacement.

<a id="kb-ret-004"></a>

### KB-RET-004 — UTC timestamps always have the highest priority

- Status: Retired
- Last validated: 2026-07-28
- Superseded by: KB-RES-002
- Evidence:
  `tests/test_models.py::test_planned_times_override_stale_utc_timestamps`

Rejected assumption: UTC `checkIn`/`checkOut` timestamps always have the
highest priority. Guesty can retain those values after a manual
`plannedArrival`/`plannedDeparture` edit, so valid planned local times take
precedence.

<a id="kb-ret-005"></a>

### KB-RET-005 — The V2 updater cannot write native Keycodes

- Status: Retired
- Last validated: 2026-07-28
- Superseded by: KB-GUESTY-011
- Evidence: production log and Guesty UI notifications on 2026-07-28,
  `tests/test_api.py::test_native_keycode_v3_404_falls_back_to_confirmed_v2_write`

Rejected assumption: the V2 updater cannot write native Keycodes. The endpoint
can acknowledge a payload containing nested `notes.keyCode` and emit
notifications without persisting it, but Guesty Support and the controlled
live test validated the different top-level `{"keyCode":"..."}` V2 contract
recorded in `KB-GUESTY-011`.

## Validation and release knowledge

<a id="kb-rel-001"></a>

### KB-REL-001 — Supported validation baseline

- Status: Validated
- Last validated: 2026-08-14
- Evidence: `.github/workflows/validate.yml`,
  `.github/pull_request_template.md`, `scripts/validate_project.py`,
  `scripts/validate_knowledge.py`, `tests/test_validate_project.py`,
  `tests/test_knowledge_validation.py`, `requirements-test.txt`,
  `custom_components/guesty/manifest.json`, full local validation run

The integration targets Home Assistant 2025.12 or newer and currently has no
third-party runtime dependency; Home Assistant supplies `aiohttp` and
`voluptuous`. CI validates Python 3.13 and 3.14. A full local release check runs
pytest with warnings as errors and at least 80% coverage, Ruff check/format,
compileall, the knowledge-contract linter, Bandit, runtime dependency audit,
`pip check`, JSON parsing, and `git diff --check`.

`scripts/validate_project.py` is the single local/CI entry point. Its focused
profile runs affected tests and Python checks during iteration; standard runs
the complete test/static/knowledge/owned-JSON suite before code handoff; release
adds coverage, security, and dependency checks. Repository JSON discovery uses
Git's tracked and unignored-new file set, so local virtual environments and
caches are never traversed.

The knowledge-contract linter is a required CI and local validation step. It
keeps the deterministic index, anchors, lifecycle metadata, cross-references,
repository evidence, explicit Python symbols, manifest baseline, separated
review history, and contributor workflow aligned. It validates structure, not
technical truth; agents must still inspect affected production code and tests.

The enforced coverage floor is 80%. Prefer behavior-oriented regression tests
for lifecycle cleanup, webhook authentication and deduplication, coordinator
fallbacks, and external-provider error classification over line-only tests.

`pytest-homeassistant-custom-component` pins a matching Home Assistant and
pytest version exactly. Those packages form an isolated development/test
harness and are not installed by HACS. Do not override one transitive pin to
silence an audit because that creates an unsupported environment and fails
`pip check`; upgrade the harness as one unit when its upstream release moves.
Production exposure is evaluated from `requirements-runtime.txt` and the
manifest, while Dependabot continues to track the test harness.

An explicit publication request is required. A HACS release is complete only
after the manifest version is bumped, full validation passes, `main` is pushed,
Python and CodeQL/security checks pass, and a matching `vX.Y.Z` GitHub release
tag points to the same commit and manifest version.

## Review history

The chronological audit trail lives in
[`KNOWLEDGE_HISTORY.md`](KNOWLEDGE_HISTORY.md). Historical review conclusions
are not active implementation contracts; use the validated entries above.
