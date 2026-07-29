"""Tests for the hard safety guard used by live Guesty API probes."""

from __future__ import annotations

import json

import pytest

from scripts.guesty_live_write_guard import (
    GuestyLiveWriteGuard,
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
