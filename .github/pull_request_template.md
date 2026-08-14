## Summary

Describe the behavior changed and the failure mode or user need it addresses.

## Verification

- [ ] Focused regression tests reproduce the old failure and pass with this change.
- [ ] `python scripts/validate_project.py standard` passes for completed code.
- [ ] `python scripts/validate_project.py release` passes for release or high-risk changes.
- [ ] Access-control and external-write changes cover stale data, ambiguous outcomes, restart/unload, and cleanup where applicable.
- [ ] Logs, diagnostics, state, and fixtures contain no credentials, bearer links, PINs, guest data, or real reservation/listing IDs.

## Knowledge impact

- [ ] I reviewed the affected rows in the module-to-knowledge map in `AGENTS.md`.
- [ ] I updated the affected `KNOWLEDGE.md` entries and evidence, or this change creates no durable engineering knowledge.
- [ ] New entries have a permanent ID, fixed anchor, index entry, status, validation date, and resolvable evidence.
- [ ] Retired entries state the rejected assumption and point to their replacement.
- [ ] External API assumptions record current/deprecated/provisional status and a last-check date where applicable.
- [ ] The selected validation profile includes a passing knowledge-contract check.

## User-facing and release impact

- [ ] README, Home Assistant strings/translations, diagnostics, and migrations are aligned where affected.
- [ ] Version/tag/release changes are included only when publication was explicitly requested.
