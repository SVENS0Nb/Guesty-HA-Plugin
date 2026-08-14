# Guesty Home Assistant Integration — Agent Guide

## Purpose and authority

This file is the concise operating contract for agents and maintainers.
[`KNOWLEDGE.md`](KNOWLEDGE.md) is the canonical durable technical memory, and
[`KNOWLEDGE_HISTORY.md`](KNOWLEDGE_HISTORY.md) is only its chronological audit
trail.

- Explicit current user intent defines the requested outcome. The production
  code and regression tests define currently implemented behavior. The README
  is the user-facing contract. Resolve disagreement between these artifacts;
  never silently choose one.
- Preserve the safety boundaries and product decisions below unless the user
  explicitly changes them.
- Put subsystem facts, API contracts, failure lessons, and retired assumptions
  in `KNOWLEDGE.md`. Keep this file limited to workflow and always-on guardrails.
- Never store credentials, tokens, PINs, bearer URLs, guest data, private URLs,
  or real reservation/listing identifiers in repository documentation, tests,
  fixtures, logs, or diagnostics.

## Task mode and authorization

Classify the current request before acting. A later explicit user instruction
may broaden the mode; an ordinary diagnostic finding does not.

| User request | Authorized behavior |
| --- | --- |
| Inspect, review, explain, or report status | Read and verify only. Do not edit, publish, or perform live writes. |
| Diagnose | Reproduce safely and identify the cause. Do not implement the fix unless requested. |
| Change, fix, or build | Implement the smallest complete change, add regression coverage, update affected contracts, and validate it. |
| Test a live external write | Require explicit authorization for that live write and follow the live-test safety rules below. |
| Publish or release | Only after an explicit publication request; follow the release sequence exactly. |

If a missing decision would materially change security, data ownership, public
behavior, or an external system, stop and ask. Otherwise make the narrowest
reasonable in-scope assumption and state it.

## Repository and worktree safety

Before editing:

1. Run `git status --short` and identify pre-existing changes.
2. Preserve user-owned and unrelated changes. Never reset, checkout, overwrite,
   or reformat them merely to simplify the task.
3. Trace the affected path from API/model through coordinator/storage, manager,
   entity/config flow, diagnostics, documentation, and cleanup.
4. Identify the relevant knowledge entries using the map below.

Use `rg`/`rg --files` for search and `apply_patch` for manual file edits. Do not
use destructive Git commands unless the user explicitly requests them. Do not
commit, push, tag, create a release, or mutate live external state merely as a
convenient implementation or diagnostic step.

## Knowledge workflow

### What to read

For every task:

1. Read this file completely.
2. Read the Purpose, Review metadata, Evidence model, and Knowledge index at the
   top of `KNOWLEDGE.md`.
3. Read every entry selected by the impact map, its direct `KB-*` references,
   and any relevant Retired entry.
4. Inspect the cited production code and tests before relying on the prose.

Read the entire `KNOWLEDGE.md` when any of these applies:

- the user requests a whole-project, security, reliability, or architecture
  audit;
- a release is being prepared;
- ownership or behavior changes across three or more subsystem areas;
- the affected scope cannot be determined safely from the index and evidence;
- current code, tests, README, this file, and the selected entries disagree.

This selective workflow is deliberate: it retains evidence-based reasoning
without consuming the task context with unrelated subsystem history.

### Impact map

Review all listed knowledge areas for changed files or concerns. Update an entry
only when durable behavior, evidence, ownership, an external contract, a safety
boundary, or a reusable failure lesson changed.

| Changed path or concern | Required knowledge review |
| --- | --- |
| `custom_components/guesty/api.py`, `_http.py`, Guesty OAuth/routes/pagination | `KB-GUESTY-*`, `KB-SAFE-001`, `KB-SAFE-003` |
| `custom_components/guesty/coordinator.py`, `webhook.py`, polling/caches | `KB-ARCH-001`, `KB-GUESTY-001`, `KB-GUESTY-006`, `KB-GUESTY-007` |
| `custom_components/guesty/models.py`, `scheduler.py`, `sensor.py`, `calendar.py` | `KB-RES-*`, `KB-SAFE-004` |
| `custom_components/guesty/access*.py`, door-link field, public portal | `KB-ACCESS-*`, `KB-SAFE-003`, `KB-SAFE-004` |
| `custom_components/guesty/loxone*.py`, shared PIN state, Guesty PIN mirrors | `KB-PIN-*`, `KB-LOXONE-*`, `KB-FAILOVER-001` |
| `custom_components/guesty/ttlock*.py` | `KB-PIN-*`, `KB-TTLOCK-*`, `KB-SAFE-002`, `KB-SAFE-004` |
| `custom_components/guesty/config_flow.py`, `const.py`, strings/translations | `KB-HA-001` and every feature area whose option contract changed |
| `custom_components/guesty/__init__.py`, stores, diagnostics, setup/unload | `KB-ARCH-002`, `KB-ARCH-003`, `KB-SAFE-*` |
| `.github/`, dependencies, validation scripts, release metadata | `KB-REL-001` |

### Maintaining knowledge

- Verify affected facts against current code and tests. Reproducible evidence
  outranks old prose.
- Update durable knowledge in the same change. Routine refactors, version-only
  bumps, transient logs, and one-off debugging details do not need entries.
- Correct or retire obsolete entries instead of appending contradictory advice.
  IDs are permanent and must never be renumbered or reused.
- Every entry appears exactly once in the index and has its fixed lowercase
  anchor. Keep numeric order within each area.
- A project-wide audit reviews every active entry and updates
  `KNOWLEDGE_HISTORY.md`. A narrow check changes only affected entry dates and
  must not advance the project-wide review date.
- Run `scripts/validate_knowledge.py` after every knowledge edit. Do not weaken
  the validator to accommodate malformed documentation.

Use this entry shape:

```markdown
<a id="kb-area-nnn"></a>

### KB-AREA-NNN — Short title

- Status: Validated | Provisional | Retired
- Last validated: YYYY-MM-DD
- Evidence: `path`, `path::test_or_symbol`, or official public specification
- Replaces: KB-AREA-NNN (optional for an active replacement)
- Superseded by: KB-AREA-NNN (required for Retired)
- Validation needed: concrete procedure (required for Provisional)
- External status: current | deprecated compatibility | provisional contract
- Last externally checked: YYYY-MM-DD when External status is present

Fact, boundaries, and engineering consequence.
```

Retired titles state the rejected assumption, and their prose begins
`Rejected assumption:`. External-contract entries cite an official public
source or identify support-confirmed evidence without copying private
correspondence or customer data. The pull-request checklist in
`.github/pull_request_template.md` applies even outside a GitHub pull request.

## Product and architecture boundaries

This is a HACS-installable Home Assistant custom integration that maintains one
shared Guesty listing/reservation snapshot and optionally provides occupancy,
calendars, privacy-gated guest details, door-access links, and one stable
six-digit reservation PIN delivered to Guesty, Loxone, TTLock, or both.

Stable ownership:

- `api.py` owns Guesty HTTP/OAuth operations; `_http.py` owns bounded response
  reading.
- `coordinator.py` is the only Guesty synchronization owner and merges polling
  with signed webhook updates.
- `models.py` owns reservation/status/time semantics; `scheduler.py` owns exact
  local transitions.
- `access*.py` owns the public door portal and Guesty link lifecycle.
- `loxone.py` owns the shared PIN lifecycle as well as Loxone provisioning;
  TTLock reuses that PIN through `ttlock*.py`.
- `storage.py` is the privacy-filtered general cache. Private access, PIN, and
  TTLock stores remain separate.
- `config_flow.py` owns UI setup/reconfiguration; `__init__.py` owns
  transactional startup and teardown.

Optional features are listing-scoped. A listing without an enabled mapping must
receive no feature-specific read, write, remote object, or side effect. See
`KB-ARCH-*`.

## Non-negotiable runtime invariants

### Shared synchronization and external traffic

- Exactly one Guesty client, OAuth lifecycle, coordinator, reservation cache,
  and normal poller exist per config entry. Optional managers consume that
  snapshot and must not add independent Guesty pollers (`KB-ARCH-001`).
- Signed webhooks are the fast path; polling is the safety net. Targeted webhook
  refreshes never claim the complete snapshot fresh (`KB-GUESTY-001`).
- OAuth refresh is serialized. A `401`/`403` may refresh and retry exactly once;
  OAuth `429` uses a persisted cooldown and cached degraded state rather than a
  token-minting or sleep loop (`KB-GUESTY-006`).
- Read HTTP bodies to EOF under the configured limit. Retry only safe,
  idempotent operations with bounded backoff. Non-idempotent creates require a
  persistent in-progress marker and remote recovery lookup (`KB-SAFE-001`,
  `KB-SAFE-002`).
- Preserve normal Guesty API headroom. All Guesty PIN-mirror writes share the
  persistent maximum of two attempts per 30 seconds, including failures,
  fallbacks, webhook passes, migrations, and restarts (`KB-PIN-003`).

### Reservation, PIN, and access safety

- Payment state does not determine reservation activity. Access intervals are
  half-open. Valid planned local arrival/departure overrides stale UTC values;
  use the tested timezone/default fallback chain (`KB-RES-*`).
- A confirmed numeric PIN is stable and never rotates automatically. Empty,
  invalid, duplicate, sparse, or temporarily unreadable Guesty fields cannot
  replace it. Native Keycode wins only the documented simultaneous-edit tie
  (`KB-PIN-001`).
- Native Guesty Keycode and the configurable custom field are independently
  enabled mirrors. V2/V3 reads and writes remain route-matched and require exact
  confirmation; HTTP 200 alone is not proof (`KB-GUESTY-002`,
  `KB-GUESTY-003`, `KB-GUESTY-011`).
- The PIN is six ASCII digits. A Guesty-only display suffix never reaches
  Loxone or TTLock (`KB-PIN-002`).
- Fresh source data is required to generate a PIN, infer a booking, or extend
  access. Offline provisioning may use only a previously confirmed private PIN
  and its exact stored interval; stored end times always retain cleanup
  authority (`KB-SAFE-004`).
- Door pages accept only a server-owned door index, never an entity ID. Unlock
  is POST-only and revalidates bearer token, CSRF, reservation, mapping,
  freshness, and time window. Public failures remain generic (`KB-ACCESS-*`).

### Provider and failover safety

- Loxone and TTLock are optional delivery targets, not PIN authorities. Booking
  time changes update existing remote access without rotating the PIN.
- Remote collisions fail closed and never rotate a confirmed Guesty PIN.
  Ambiguous creates recover by privacy-safe ownership marker. Modify or delete
  only positively identified managed objects (`KB-LOXONE-*`, `KB-TTLOCK-*`).
- TTLock refresh tokens rotate. An unchanged-account options flow uses the live
  manager and persists its replacement token; a temporary client must not
  consume and discard it (`KB-TTLOCK-001`).
- TTLock's password MD5 is protocol-mandated compatibility with
  `usedforsecurity=False`, not a general password-storage choice.
- Backup operation is active/passive only. Exactly one Home Assistant instance
  may write at a time (`KB-FAILOVER-001`).

### Privacy, configuration, and lifecycle

- Private stores remain `private=True`, atomic, defensively loaded, and
  separated by purpose. The general cache never owns PINs or bearer tokens
  (`KB-ARCH-003`).
- Diagnostics are built from a reviewed safe allowlist. Logs and diagnostics
  never expose credentials, PINs, door links, guest data, full reservation IDs,
  remote IDs, or unbounded response bodies (`KB-SAFE-003`).
- Config and options flows remain UI-driven. Blank secrets preserve the stored
  value only for an unchanged identity. Frontend schemas must serialize through
  Home Assistant's custom serializer and deliberately handle stale extra keys
  (`KB-HA-001`).
- Startup, reload, setup failure, unload, and integration removal cancel every
  owned task, timer, listener, and retry worker. Partial setup rolls back all
  previously started resources (`KB-ARCH-002`).
- Credential updates have exactly one reload owner. Do not combine the update
  listener with another update-and-reload helper.

## Live external testing

Live external writes are never inferred from a diagnosis or test request that
can be satisfied locally. Obtain explicit authorization for the specific live
mutation, freeze its target and payload after read-only preflight, and never use
customer data in repository artifacts.

Every agent-driven Guesty reservation write—V2 Keycode, V3 notes, reservation
custom field, or any future write route—must use both
`GuestyLiveTokenCache` and `GuestyLiveWriteGuard` from
`scripts/guesty_live_write_guard.py`:

- reuse one credential-bound private token instead of repeatedly minting OAuth
  tokens;
- arm only after preflight; the first attempt waits 30 seconds;
- allow at most two attempts per run, with diagnosis and another 30-second wait
  before the second;
- count failed, rejected, timed-out, and ambiguous writes as attempts;
- never delete or bypass guard state to force another attempt.

Live Loxone, TTLock, lock, webhook, or other destructive external operations
also require explicit authorization and the narrowest possible target. Prefer
local mocks and regression tests.

## Change and regression discipline

For every bug fix:

1. Add or update a focused test that reproduces the old failure.
2. Fix the smallest responsible layer without bypassing the invariants above.
3. When access control, persistence, or external writes are affected, cover
   ambiguous outcomes, stale data, restart/unload, partial success, and cleanup.
4. Preserve backward-compatible config-entry/private-store migrations.
5. Verify no new secret or PII reaches logs, state, diagnostics, events, or the
   general cache.

When corrected code must reinterpret persisted failures, increment the relevant
retry-state migration version and test a fixture from the exact prior version.
Never clear stored retry state by sacrificing PINs, tokens, ownership, cleanup,
or global traffic limits.

High-risk areas requiring direct regression coverage include sparse Guesty
fields, planned-time precedence, fragmented/oversized HTTP bodies, OAuth
stampedes, non-idempotent recovery, immutable confirmed PINs, webhook one-minute
PIN publication, booking/mapping changes after provisioning, multi-lock partial
success, options-form secret preservation, and cancellation-safe teardown.

## Validation profiles

Use the shared runner so local instructions and CI cannot drift:

```bash
# During iteration: inferred/explicit affected tests, Ruff, compile, knowledge,
# and diff checks. At least one existing path or pytest node is required.
.venv/bin/python scripts/validate_project.py focused \
  custom_components/guesty/api.py tests/test_api.py::test_name

# Before handing off any completed code change: complete tests and static checks,
# knowledge validation, repository-owned JSON parsing, and diff checks.
.venv/bin/python scripts/validate_project.py standard

# Before publication and after cross-cutting/security-sensitive changes: standard
# checks plus coverage floor, Bandit, runtime dependency audit, and pip check.
.venv/bin/python scripts/validate_project.py release
```

Documentation-only changes run the focused profile with the changed document;
knowledge changes must also run `tests/test_knowledge_validation.py`. The JSON
validator checks only tracked and unignored new repository JSON files; it never
walks `.venv`, `.git`, caches, or build directories.

CI runs the release profile on Python 3.13 and 3.14. Do not weaken a check or
coverage floor to make unrelated failures disappear. Fix the cause or report
the genuine blocker.

## Documentation and release

- Update README, config-flow strings, German/English Home Assistant
  translations, tests, and affected knowledge entries whenever the user-facing
  contract changes. The guest portal retains German, English, Spanish, and
  French.
- Do not publish merely because implementation is complete. Publication needs
  an explicit user request.
- For an authorized HACS release: choose the semantic version, update
  `custom_components/guesty/manifest.json` and the knowledge baseline, run the
  release profile, commit and push `main`, wait for Python and CodeQL/security
  checks, then create the matching `vX.Y.Z` GitHub release/tag.
- Verify the release tag points to the intended commit and its manifest has the
  same version. A local commit or version bump alone is not a publication.
