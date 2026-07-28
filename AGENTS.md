# Guesty Home Assistant Integration — Agent Guide

## Purpose and authority

This file is the durable engineering memory for the whole repository. Read it
before changing code, tests, configuration flows, documentation, or release
metadata.

- The current code and regression tests are the source of truth for implemented
  behavior. The README is the user-facing contract and must be kept aligned.
- Preserve the product decisions and safety invariants below unless the user
  explicitly changes them.
- Record only durable architecture, behavior, and maintenance lessons here.
  Never add real credentials, tokens, reservation IDs, guest data, private URLs,
  or details copied from diagnostic exports.
- Update this file when a change introduces a new long-lived integration,
  invariant, data owner, failure mode, or release requirement. Do not turn it
  into a changelog or duplicate every implementation detail.

## Product intent

This is a HACS-installable Home Assistant custom integration for Guesty. Its
primary responsibilities are:

1. Keep Guesty listings and active reservations current with low API traffic.
2. Expose occupancy sensors and one future-reservation calendar per listing.
3. Optionally expose current guest details after an explicit privacy opt-in.
4. Optionally publish a secure per-reservation web page that operates one to six
   Home Assistant `lock.*` entities.
5. Optionally create one stable six-digit reservation PIN in Guesty's native
   `notes.keyCode` field and deliver it shortly before the stay to Loxone,
   TTLock, or both.

All optional access systems are configured through the Home Assistant UI and
can be enabled only for selected listings. A listing without a mapping must not
receive that feature's side effects.

## Architecture map

- `api.py`: shared Guesty OAuth/API client, retries, pagination, webhooks,
  native Keycode writes, and reservation custom-field operations.
- `_http.py`: bounded response reader shared by every external API client.
- `coordinator.py`: the only Guesty synchronization owner; merges polling and
  webhook updates and publishes one shared reservation/listing snapshot.
- `models.py`: Guesty parsing, time precedence, reservation status rules,
  occupancy, date windows, and merge behavior.
- `storage.py`: privacy-filtered general Guesty cache. It does not own PINs.
- `scheduler.py`: exact local check-in/check-out occupancy transitions without
  another Guesty request.
- `webhook.py`: signed Home Assistant webhook endpoint and remote Guesty
  subscription lifecycle.
- `access.py`, `access_names.py`, `access_branding.py`: guest door page,
  localization, security controls, Guesty link publication, and cleanup.
- `loxone.py`: authoritative shared PIN lifecycle plus Loxone provisioning.
  Despite the historical module name, this is also the PIN source used by
  TTLock.
- `loxone_api.py`: HTTPS Loxone user-management client.
- `ttlock.py`, `ttlock_api.py`: TTLock passcode lifecycle and Open Platform
  client.
- `sensor.py`, `calendar.py`: dynamic per-listing Home Assistant entities.
- `config_flow.py`, `strings.json`, `translations/`: setup, reauthentication,
  reconfiguration, options, help text, and translations.
- `diagnostics.py`: privacy-safe operational diagnostics.
- `tests/`: the regression contract. Every production subsystem has direct
  tests; preserve that shape.

`__init__.py` owns startup and teardown order. Partial setup must roll back all
already-started schedulers, managers, webhooks, platforms, and background tasks.

## Shared Guesty synchronization and traffic

- There is exactly one `GuestyApiClient`, coordinator, OAuth token lifecycle,
  reservation cache, and normal Guesty poller per config entry. Door access,
  Loxone, and TTLock must consume the coordinator snapshot and must not add
  independent Guesty polling loops.
- Default reservation polling is every 300 seconds. The normal listing safety
  sync is daily. If a signed webhook is unavailable, listings are checked at
  least every 15 minutes.
- Run a daily full reservation sync. Other polls are incremental and retain a
  five-minute overlap to avoid losing updates at cursor boundaries.
- Signed webhooks are the fast path. Supported subscriptions are
  `reservation.created.v2`, `reservation.updated.v2`, `listing.new`,
  `listing.updated`, and `listing.removed`. Legacy reservation event names may
  be accepted locally for migration but must not be requested in a new
  subscription.
- Verify the Guesty/Standard Webhooks HMAC, timestamp tolerance, payload size,
  and replay ID before accepting work. Mark an event as completed only after
  the coordinator accepts it, so Guesty can retry a failed handoff.
- Debounce bursts. A single reservation event should use a targeted read;
  multiple reservation events should share one incremental refresh. Apply
  sufficient listing payloads directly; fetch only missing details. A removed
  listing is pruned immediately. A new listing triggers reservation traffic only
  for that listing.
- Auth or permission failures start Home Assistant reauthentication. Transient
  failures use the last valid cache in a visible degraded/stale state.
- Reservation/listing managers must use bounded, persistent backoff. Never
  create a rapid retry loop after an outage, rate limit, or malformed response.
- Preserve API headroom for normal synchronization. Guesty Keycode and door-link
  migrations are deliberately write-budgeted rather than bulk-written at once.

## HTTP and external API rules

- Read every HTTP response to EOF through `_http.async_read_limited`; enforce
  the configured hard size limit. A single `response.read(n)` is not a valid
  replacement because `aiohttp` may return a partial fragment before EOF. This
  previously caused valid JSON to be treated as repeatedly malformed.
- Current body limits are 10 MiB for Guesty and 1 MiB for Loxone/TTLock.
- Retry only operations known to be safe: transient `408`, `429`, `5xx`, and
  transport failures use bounded exponential backoff and `Retry-After` when
  available. Do not retry permanent errors.
- Do not blindly retry a non-idempotent create. Webhook creation, Loxone user
  creation, and TTLock passcode creation need a persistent in-progress marker
  plus a remote lookup/recovery path after an ambiguous response.
- Validate all resource IDs before interpolating them into URL paths.
- Restrict service base URLs to HTTPS. Reject credentials embedded in URLs.
  TTLock regions use an explicit host allowlist; do not accept an arbitrary API
  host.
- Bound and redact error context. Preserve Guesty's `x-request-id` when
  available, but never log OAuth tokens, bearer door links, passwords, PINs,
  guest names, or unbounded response bodies.
- For a Guesty support case, collect the redacted method/path/payload, response
  status, `x-request-id`, affected reservation ID, client ID confirmation, and
  timestamp. Never commit or paste the client secret or access token.

## Reservation semantics and time handling

- Active reservation statuses are the configured confirmed/reserved/checked-in/
  in-house variants. Cancelled, closed, declined, and expired reservations are
  inactive. Payment status is intentionally not a criterion.
- Future active reservations are valid input. Codes and door links can be
  created when the reservation is first observed; remote lock-system objects
  are deferred by their provisioning lead.
- Occupancy and access windows are half-open: `[start, end)`.
- Time precedence is subtle and covered by regression tests:
  1. A valid `plannedArrival`/`plannedDeparture` combined with its localized
     date overrides a possibly stale `checkIn`/`checkOut` UTC timestamp.
  2. Otherwise use a valid UTC timestamp.
  3. Otherwise combine the localized date with reservation/listing defaults.
  4. Final defaults are 15:00 check-in and 11:00 check-out.
- Use the listing timezone; only fall back to Home Assistant's timezone when the
  Guesty timezone is missing or invalid.
- Invalid intervals are skipped, not allowed to abort a complete sync.
  Overlapping current reservations must resolve deterministically.
- A Guesty change to date, planned arrival/departure, guest name, listing, or
  status must be able to update or revoke already-provisioned access.
- Keep the local transition scheduler so sensor occupancy changes exactly at
  check-in/check-out without waiting for the next API poll.

## Guesty native Keycode: authoritative PIN lifecycle

- The only source and destination for reservation PINs is Guesty's native
  Reservations v3 `notes.keyCode`. Old releases used a reservation custom field;
  that behavior is obsolete and must not be reintroduced as a fallback.
- The door-access URL is still stored in a separate configurable Guesty
  reservation custom field. Never confuse the two data paths.
- Write the native field with
  `PUT /v1/reservations-v3/{reservationId}/notes` and the minimal payload
  `{"notes":{"keyCode":"..."}}`. Do not round-trip or overwrite unrelated notes.
  Treat the write as successful only after exact confirmation.
- Read native Keycodes only from `GET /v1/reservations-v3`. The legacy
  `/reservations` endpoints do not expose `notes.keyCode` even when it is
  projected. The v3 response is a top-level array and accepts at most ten
  reservation IDs per request. Enrich only active reservations for listings
  mapped to an enabled PIN provider: changed reservations on incremental polls,
  one targeted batch after a single webhook, and all applicable reservations
  during the daily/startup full sync. Do not add a second poller.
- Guesty's returned `notes.keyCode` is authoritative. A valid, unique manual
  change must propagate to existing Loxone and TTLock objects. Once Guesty has
  confirmed a PIN, the integration must never change its six digits
  automatically; only a subsequently observed Guesty edit may replace it.
- An omitted `notes` projection means “not observed,” not “the user deleted the
  Keycode.” Never rotate or overwrite a privately cached PIN solely because a
  sparse response omitted `notes`.
- A sparse `notes` projection must not strand an explicitly pending native
  Keycode publication. After bounded backoff, retry the exact stable private PIN
  for a failed initial write, source migration, or configured suffix change;
  never rotate merely because the projection is absent.
- An explicitly empty, invalid, or duplicate observed Keycode is fail-closed.
  Revoke existing remote delivery and expose a conflict, but never manufacture
  a replacement after any PIN was confirmed. A new reservation whose observed
  Keycode is initially empty may receive its first generated PIN.
- Duplicate ownership is deterministic. The first healthy established
  reservation keeps remote delivery; later duplicates stay blocked until a
  user supplies a unique code in Guesty.
- The actual PIN is exactly six ASCII digits. Generated codes use
  `secrets.randbelow`, the configured one- or two-digit ASCII prefix, reject weak
  sequences, and exclude every known active/private/rejected code.
- A listing may append up to eight printable non-digit display characters such
  as `#`, `*`, or `☑️`. Guesty receives, for example, `723456#`; Loxone and
  TTLock always receive only `723456`. Changing the suffix rewrites the Guesty
  display without rotating the numeric PIN.
- Use one persistent global limit of at most two Guesty Keycode write attempts
  in any 30-second window. Normal queue passes, reservation-specific retries,
  webhook-triggered passes, and restarts share this limit. Failed and ambiguous
  PUTs consume a slot. Prioritize current and nearest stays.
- Every native-Keycode path must consume that same write budget, including
  sparse cached snapshots and one-time migrations from private stored PINs.
  Never bypass the queue merely because Guesty omitted the `notes` projection.
- Deferring a due reservation-specific failure because the global limit is full
  must preserve its retry count and failure reason; global queueing may move the
  next retry later but must not turn a real failure into a generic queued state.
- Loxone and TTLock collisions never rotate an already confirmed Guesty PIN.
  Delete any tentative remote object, expose a conflict, and use persistent
  retry backoff until a manual Guesty edit supplies a different PIN.
- A reservation-specific Guesty 404 consumes its own bounded write attempt and
  enters retry backoff, but must never stop unrelated reservations in the same
  batch. Account-wide authentication, permission, transport, or payload errors
  may stop the batch to avoid unnecessary traffic.
- Native Keycode failures log only the hashed reservation marker, stable
  operation/reason, safe endpoint label, HTTP status, bounded `x-request-id`,
  retry count/delay, and available rate-limit headroom. A successful write may
  log the marker and retry count, but never the PIN or full reservation ID.
- The privacy-filtered general cache never persists Keycodes. The private PIN
  store owns plaintext only while needed. Remove plaintext locally at
  cancellation/access end before attempting remote cleanup. Guesty's native
  Keycode remains as booking documentation.
- Guesty Keycode, Loxone, and TTLock status sensors report delivery state,
  counts, times, and safe error reasons only. They never expose the PIN.

## Guest door-access web page

- This feature is independent of Keycode/Loxone/TTLock and may be enabled for
  selected listings only.
- Publish one opaque bearer URL per eligible reservation to the configured
  Guesty reservation custom field. Resolve field names/IDs safely and confirm
  writes with bounded read-back retries for Guesty's eventual consistency.
- A link can show one to six configured Home Assistant `lock.*` entities. Omit
  unused slots. The browser supplies only a door index; the server owns the
  entity mapping and must never accept an arbitrary entity ID from the client.
- The portal supports German, English, Spanish, and French from
  `Accept-Language`, with English fallback. Door names may be translated per
  listing. Keep the established UI wording and capitalization, including
  “Door Access” and “Access Unavailable.”
- On successful unlock, keep all buttons usable and show a localized transient
  notification for five seconds. Do not require a page reload.
- GET renders only and must never unlock. Unlock requires POST, a current
  time-bucket CSRF nonce, a bounded request body, a five-second per-door
  cooldown, at most ten actions per minute per door, and a 15-second service
  timeout.
- Revalidate token, reservation fingerprint, active status, mapping, time
  window, and snapshot freshness for every action. Allowed time is
  `[check-in - early offset, check-out + late offset)`. A single failed poll may
  retain the last safe state; beyond the configured stale threshold fail closed.
- Keep link tokens unguessable and HMAC-bound to a private secret, reservation,
  permission version, and relevant inputs. Store/index only a token hash.
  Rotate when permissions, reservation timing, mapping, or field identity
  changes. Translation-only label changes must not rotate the URL.
- Revoke locally before Guesty field cleanup. Cleanup uses persistent backoff,
  bounded writes, and seven-day tombstones. Heal a stale custom-field ID only
  for narrowly classified field-reference failures; a generic 404 or transient
  failure must not cause an endless rotate/recreate loop.
- External Home Assistant, logo, and favicon URLs must be credential-free HTTPS.
  Keep branding origins tightly scoped in CSP. Reverse proxies must not cache
  `/api/guesty/access/` and should redact token-bearing paths from logs.
- Fire only privacy-safe audit events. Never include guest names or bearer
  tokens in the access event.

## Loxone invariants

- Loxone is optional per listing. A listing maps to one Miniserver and one or
  more normal user groups. Groups with Config or user-management privileges and
  built-in/administrative groups are not selectable.
- Use a dedicated least-privilege service account with user-management rights,
  never an administrator's normal account. Miniserver or reverse-proxy access
  must be HTTPS.
- A future reservation gets its Guesty Keycode immediately, but its Loxone user
  is created only inside the configured provisioning lead (default six hours
  before the allowed access start). Validity is exactly the shared early/late
  access window, with timespan user state and remote auto-delete.
- Preserve the stable reservation-derived user UUID and numeric PIN across time
  changes. Update validity, name, or groups in place. If a stay moves back
  outside the lead, remove the user and recreate it later with the same Guesty
  PIN. On a server/listing move, clean the old target before provisioning the
  new one.
- Guest name may be sent only when the global guest-details privacy option is
  enabled. Otherwise use the booking ID as the remote label.
- Loxone collision results such as `201` and `409` are not success. Delete any
  tentative user, retain the confirmed Guesty PIN unchanged, and expose a
  backed-off conflict until the user changes the Keycode in Guesty.
- Persist a private connection snapshot only while required to clean a remote
  user after configuration changes. On cancellation, remove plaintext first
  and retain only a code-free cleanup tombstone if deletion fails.
- Stale Guesty data must block creation and access extension, while the locally
  stored end time must still trigger cleanup.

## TTLock invariants

- TTLock is an optional independent destination that reuses the shared Guesty
  reservation and numeric PIN. It must not create another Guesty poller or a
  second PIN lifecycle.
- Support only configured Open Platform regions and compatible V4 locks with
  `keyboardPwdVersion=4` and `hasGateway=1`. A listing may map to one to six
  locks.
- The TTLock App password is used once for OAuth and is not stored. Access and
  refresh tokens live in the private store and are bound to the matching region,
  client ID, and username. Blank secrets during reconfiguration mean “keep the
  existing secret”; never put stored secrets back into UI fields.
- TTLock's OAuth protocol requires an MD5 digest of the app password. Keep the
  narrowly scoped `usedforsecurity=False` implementation and its explanation;
  do not replace it as a generic password-hashing choice or expose the password.
- Provision only within the configured lead, using the shared early/late
  validity window. Booking time changes update existing passcodes without PIN
  rotation. Moving a stay outside the lead removes the remote passcode and adds
  it later with the same Guesty PIN.
- Track every lock independently. Persist partial successes so one offline
  gateway causes targeted retry, not duplicate creation on successful locks.
- Before add, check for code conflicts. A remote conflict deletes any tentative
  managed passcode and enters backoff; it never requests an automatic Guesty
  PIN rotation.
- Use a privacy-safe hashed reservation marker for ambiguous-create recovery and
  ownership checks. Before change/delete, verify that the remote passcode ID
  still has the expected marker. Never alter foreign or manually renamed codes.
- Reconcile drift no more often than every 30 minutes and coalesce passcode
  reads per lock. Stale Guesty data blocks creation/extension but not cleanup at
  the stored end time.
- Disabling, remapping, cancellation, or integration removal deletes only
  confirmed managed passcodes. After best-effort removal, do not orphan
  unreachable OAuth credentials in a store with no future retry owner.

## Privacy, storage, and diagnostics

- Guest names and confirmation codes are hidden and not persisted by default.
  They may be exposed only after explicit opt-in and must remain unrecorded
  entity attributes where applicable.
- The current-guest sensor and door-link sensor are disabled by default. Future
  reservation calendars remain useful without guest PII.
- Every Home Assistant `Store` containing private state must remain
  `private=True` with atomic writes and validate loaded structures defensively.
- Credentials belong in config-entry/private storage only. Never place them in
  entity state, diagnostics, events, markers, log messages, or README examples.
- Diagnostics hash listing IDs and report operational state only. Preserve the
  explicit redaction/removal of Guesty credentials, webhook identifiers and
  secrets, access mappings/fields, Loxone servers, TTLock accounts/tokens,
  remote IDs, PINs, names, and confirmation codes.

## Home Assistant lifecycle, entities, and configuration

- Create occupancy sensor, calendar, and applicable disabled-by-default
  diagnostic entities for each listing. Add newly discovered listings exactly
  once and remove deleted listings from runtime and the entity registry.
- Occupancy is only `vacant`/`occupied`; the calendar contains current and
  future active bookings. Calendar and current-guest content obey the privacy
  option.
- Keep config and options flows fully UI-driven with short helpful descriptions
  and official setup links. Preserve values across multi-step forms and
  migrations.
- Use `vol.Schema` with deliberate extra-key handling in repeated/stale frontend
  submissions. Applying Home Assistant's suggested-value helper can rebuild a
  schema with `PREVENT_EXTRA`; restore `REMOVE_EXTRA` where the flow relies on
  it. This previously produced “extra keys not allowed” and “Unknown error”
  failures in Loxone/TTLock setup.
- Empty password/client-secret fields on an unchanged identity preserve the
  stored value. A changed identity requires fresh validation. Guesty credential
  reconfiguration must verify that the replacement credentials belong to the
  same Guesty account and preserve all options/mappings/private state.
- Keep setup/unload cancellation-safe. Managers own their tasks, timers, and
  listeners and must cancel them on unload. No background task should survive a
  failed setup or reload.
- The bundled brand asset is user-provided and should not be regenerated or
  replaced casually. Home Assistant integration branding and the HACS catalog
  icon have separate metadata/cache paths; do not assume one automatically
  fixes the other.

## Availability and failover

- Network and API outages are expected. Continue automatically after recovery
  with bounded backoff and the last safe snapshot; never require a manual reload
  for a normal transient outage.
- New access must fail closed when source data is too stale, while previously
  stored end times continue to revoke/clean up access.
- Supported backup design is active/passive only. Run exactly one Home Assistant
  instance as the active writer. On deliberate failover, the new instance
  adopts the stable native Guesty Keycode; it may overwrite the old door-link
  custom field with its own link. Door-link continuity is less important than
  PIN continuity.
- Do not promise active/active behavior or use a new shared custom field to
  coordinate writers. Parallel writers can create competing URLs, remote
  objects, PIN conflicts, and unnecessary API traffic.

## Change and regression discipline

Before editing, trace the entire affected path: API/model → coordinator/storage
→ manager → entity/config flow → diagnostics/documentation. Preserve
backward-compatible config-entry and private-store migrations.

For every bug:

1. Add or update a focused regression test that reproduces the failure.
2. Fix the smallest responsible layer without bypassing the invariants above.
3. Test ambiguous network outcomes, stale data, cancellation/unload, and
   cleanup when the change touches access control or external writes.
4. Verify no new secret or PII appears in logs, state, diagnostics, events, or
   persisted general cache.

High-risk regression areas include:

- planned Guesty times overriding stale UTC values;
- sparse responses omitting `notes` or `customFields`;
- fragmented HTTP bodies and size limits;
- OAuth refresh stampedes and late `401` responses;
- non-idempotent create recovery;
- duplicate PIN ownership and immutable confirmed PIN handling;
- booking/listing/mapping changes after provisioning;
- partial multi-lock success and foreign-object ownership checks;
- repeated options-form submissions and blank-secret preservation;
- webhook signing-secret migration without delete/create loops;
- task/timer cleanup during reload, failed setup, and integration removal.

## Validation

Use the repository virtual environment when available. A complete validation
before a release is:

```bash
.venv/bin/python -m pytest -W error \
  --cov=custom_components/guesty \
  --cov-report=term-missing \
  --cov-fail-under=65
.venv/bin/ruff check custom_components tests
.venv/bin/ruff format --check custom_components tests
.venv/bin/python -m compileall -q custom_components tests
.venv/bin/bandit -q -r custom_components/guesty -ll
.venv/bin/python -m pip_audit -r requirements-runtime.txt
.venv/bin/python -m pip check
git diff --check
```

Also parse every JSON file. CI runs the tests and static checks on Python 3.13
and 3.14. The integration intentionally has no third-party runtime dependency;
Home Assistant supplies `aiohttp` and `voluptuous`.

## Documentation and release rules

- Update README, config-flow strings, German/English Home Assistant
  translations, tests, and this file whenever the user-facing contract changes.
  The guest portal itself must retain German, English, Spanish, and French.
- When documenting times, use the actual tested precedence in this file. Do not
  reintroduce the older “UTC always wins over planned time” description.
- Do not publish, push, tag, or create a GitHub release merely because code was
  changed. Publication requires an explicit user request.
- For an explicit HACS release: choose and apply the semantic version in
  `custom_components/guesty/manifest.json`, run the complete validation, commit
  and push `main`, wait for both Python validation jobs and security/CodeQL
  checks to pass, then create the matching `vX.Y.Z` GitHub release/tag.
- Verify that the release tag points to the intended commit and that the tagged
  manifest contains the same version. HACS discovers releases from that tag;
  a local commit or manifest bump alone is not a publication.
