"""Tests for the hard safety guard used by live Guesty API probes."""

from __future__ import annotations

import json

import pytest

from scripts.guesty_live_write_guard import (
    GuestyLiveTokenCache,
    GuestyLiveWriteGuard,
    LiveOAuthToken,
    LiveTokenCacheStateError,
    LiveWriteDiagnosisRequiredError,
    LiveWriteGuardStateError,
    LiveWriteLimitError,
)


class FakeTime:
    """Deterministic wall clock and sleeper."""

    def __init__(self, now: float = 100.0) -> None:
        """Initialize the fake clock."""
        self.now = now
        self.sleeps: list[float] = []

    def time(self) -> float:
        """Return the current fake timestamp."""
        return self.now

    def sleep(self, delay: float) -> None:
        """Advance by the requested delay."""
        self.sleeps.append(delay)
        self.now += delay


def test_first_live_write_always_waits_thirty_seconds(tmp_path) -> None:
    """Arming a test never grants its first write immediately."""
    fake = FakeTime()
    state_path = tmp_path / "guard.json"
    guard = GuestyLiveWriteGuard(
        state_path,
        clock=fake.time,
        sleeper=fake.sleep,
    )

    permit = guard.wait_for_attempt()

    assert permit.attempt == 1
    assert permit.granted_at == 130.0
    assert fake.sleeps == [30.0]
    assert guard.attempts_used == 1
    assert guard.attempts_remaining == 1
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "last_attempt_at": 130.0,
    }
    assert state_path.stat().st_mode & 0o777 == 0o600


def test_second_write_requires_diagnosis_and_another_thirty_seconds(
    tmp_path,
) -> None:
    """A blind retry is rejected and an analysed retry remains spaced."""
    fake = FakeTime()
    guard = GuestyLiveWriteGuard(
        tmp_path / "guard.json",
        clock=fake.time,
        sleeper=fake.sleep,
    )
    guard.wait_for_attempt()

    with pytest.raises(LiveWriteDiagnosisRequiredError):
        guard.wait_for_attempt()

    permit = guard.wait_for_attempt(diagnosis_complete=True)

    assert permit.attempt == 2
    assert permit.granted_at == 160.0
    assert fake.sleeps == [30.0, 30.0]


def test_third_write_is_rejected_even_after_diagnosis(tmp_path) -> None:
    """No test run can acquire a third write permit."""
    fake = FakeTime()
    guard = GuestyLiveWriteGuard(
        tmp_path / "guard.json",
        clock=fake.time,
        sleeper=fake.sleep,
    )
    guard.wait_for_attempt()
    guard.wait_for_attempt(diagnosis_complete=True)

    with pytest.raises(LiveWriteLimitError):
        guard.wait_for_attempt(diagnosis_complete=True)


def test_persisted_state_spaces_independent_test_processes(tmp_path) -> None:
    """A second guard cannot race a recently granted permit."""
    fake = FakeTime()
    state_path = tmp_path / "guard.json"
    first = GuestyLiveWriteGuard(
        state_path,
        clock=fake.time,
        sleeper=fake.sleep,
    )
    second = GuestyLiveWriteGuard(
        state_path,
        clock=fake.time,
        sleeper=fake.sleep,
    )

    assert first.wait_for_attempt().granted_at == 130.0
    assert second.wait_for_attempt().granted_at == 160.0
    assert fake.sleeps == [30.0, 30.0]


def test_invalid_persisted_state_fails_closed(tmp_path) -> None:
    """Corrupted timing state cannot silently disable the traffic guard."""
    fake = FakeTime()
    state_path = tmp_path / "guard.json"
    state_path.write_text('{"version":1,"last_attempt_at":"bad"}', encoding="utf-8")
    guard = GuestyLiveWriteGuard(
        state_path,
        clock=fake.time,
        sleeper=fake.sleep,
    )

    with pytest.raises(LiveWriteGuardStateError):
        guard.wait_for_attempt()


def test_live_token_cache_reuses_token_across_process_instances(tmp_path) -> None:
    """Related diagnostic processes mint only one Guesty access token."""
    fake = FakeTime()
    state_path = tmp_path / "token.json"
    fetch_count = 0

    def fetch() -> LiveOAuthToken:
        nonlocal fetch_count
        fetch_count += 1
        return LiveOAuthToken("bearer-token", fake.time() + 86400)

    first = GuestyLiveTokenCache(state_path, clock=fake.time)
    second = GuestyLiveTokenCache(state_path, clock=fake.time)

    assert first.get_or_fetch("client-id", "client-secret", fetch).access_token == (
        "bearer-token"
    )
    assert second.get_or_fetch("client-id", "client-secret", fetch).access_token == (
        "bearer-token"
    )
    assert fetch_count == 1
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert state_path.parent.stat().st_mode & 0o777 == 0o700


def test_live_token_cache_does_not_store_raw_credentials(tmp_path) -> None:
    """Private cache state contains a fingerprint rather than credentials."""
    fake = FakeTime()
    state_path = tmp_path / "token.json"
    cache = GuestyLiveTokenCache(state_path, clock=fake.time)

    cache.get_or_fetch(
        "sensitive-client-id",
        "sensitive-client-secret",
        lambda: LiveOAuthToken("bearer-token", fake.time() + 86400),
    )

    raw = state_path.read_text(encoding="utf-8")
    assert "sensitive-client-id" not in raw
    assert "sensitive-client-secret" not in raw
    assert "bearer-token" in raw


def test_live_token_cache_refreshes_only_inside_margin(tmp_path) -> None:
    """A cached token remains reusable until its configured refresh margin."""
    fake = FakeTime()
    state_path = tmp_path / "token.json"
    issued = iter(("first-token", "second-token"))
    fetch_count = 0

    def fetch() -> LiveOAuthToken:
        nonlocal fetch_count
        fetch_count += 1
        return LiveOAuthToken(next(issued), fake.time() + 1000)

    cache = GuestyLiveTokenCache(
        state_path,
        clock=fake.time,
        refresh_margin=100,
    )
    assert cache.get_or_fetch("client", "secret", fetch).access_token == "first-token"
    fake.now += 899
    assert cache.get_or_fetch("client", "secret", fetch).access_token == "first-token"
    fake.now += 2
    assert cache.get_or_fetch("client", "secret", fetch).access_token == "second-token"
    assert fetch_count == 2


def test_live_token_cache_changed_credentials_do_not_reuse_token(tmp_path) -> None:
    """A changed secret cannot silently reuse another credential context."""
    fake = FakeTime()
    state_path = tmp_path / "token.json"
    fetch_count = 0

    def fetch() -> LiveOAuthToken:
        nonlocal fetch_count
        fetch_count += 1
        return LiveOAuthToken(f"token-{fetch_count}", fake.time() + 86400)

    cache = GuestyLiveTokenCache(state_path, clock=fake.time)
    assert cache.get_or_fetch("client", "first-secret", fetch).access_token == (
        "token-1"
    )
    assert cache.get_or_fetch("client", "second-secret", fetch).access_token == (
        "token-2"
    )
    assert fetch_count == 2


def test_live_token_cache_corruption_fails_without_fetching(tmp_path) -> None:
    """Corrupt state cannot amplify OAuth traffic by falling through to fetch."""
    fake = FakeTime()
    state_path = tmp_path / "token.json"
    state_path.write_text("not-json", encoding="utf-8")
    cache = GuestyLiveTokenCache(state_path, clock=fake.time)
    fetched = False

    def fetch() -> LiveOAuthToken:
        nonlocal fetched
        fetched = True
        return LiveOAuthToken("token", fake.time() + 86400)

    with pytest.raises(LiveTokenCacheStateError):
        cache.get_or_fetch("client", "secret", fetch)
    assert fetched is False


def test_live_token_cache_rejects_nearly_expired_fetch_result(tmp_path) -> None:
    """A token unusable beyond the refresh margin is never cached."""
    fake = FakeTime()
    state_path = tmp_path / "token.json"
    cache = GuestyLiveTokenCache(state_path, clock=fake.time)

    with pytest.raises(LiveTokenCacheStateError):
        cache.get_or_fetch(
            "client",
            "secret",
            lambda: LiveOAuthToken("token", fake.time() + 60),
        )
    assert not state_path.exists()
