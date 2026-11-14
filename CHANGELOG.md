# Changelog

## 2.4.9 — 2026-11-13

- Use Loxone's unique User ID for recovery, retaining compatibility with the
  older NFC marker through a bounded, complete legacy scan.
- Preserve existing UUIDs, NFC IDs and PINs; reject conflicting or ambiguous
  ownership instead of blindly creating replacements.
- Confirm remote absence before completing cleanup, including absent-user
  HTTP error envelopes; generic failures retain retry ownership.
- Requeue corrected, configured-target provider failures once without changing
  Guesty mirrors, TTLock state, credentials or unknown server bindings.
