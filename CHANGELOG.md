# Changelog

## 2.4.10 — 2026-11-14

- Remove sensor/calendar device-registry deprecation warnings on modern Home
  Assistant by using the owned hub's registry ID instead of `via_device`.
- Register the existing hub before either platform starts, preserving device
  identifiers, user-assigned names, entity IDs and older HA compatibility.
- Use typed DeviceInfo constructors for both older and current HA versions.
  No PIN, reservation, private-store or provider synchronization changes.

## 2.4.9 — 2026-11-13

- Use Loxone's unique User ID for recovery, retaining compatibility with the
  older NFC marker through a bounded, complete legacy scan.
- Preserve existing UUIDs, NFC IDs and PINs; reject conflicting or ambiguous
  ownership instead of blindly creating replacements.
- Confirm remote absence before completing cleanup, including absent-user
  HTTP error envelopes; generic failures retain retry ownership.
- Requeue corrected, configured-target provider failures once without changing
  Guesty mirrors, TTLock state, credentials or unknown server bindings.
