"""Hard safety guard for manual Guesty Keycode write tests.

Create the guard only after all read-only preflight checks have completed and
the target reservation and payload are frozen. Every permit is persisted
*before* the caller performs the network request, so failed and ambiguous
writes consume an attempt as well.

Example:

    guard = GuestyLiveWriteGuard()
    first = guard.wait_for_attempt()
    perform_one_guesty_put()

    # A second attempt is permitted only after the first result was analysed.
    second = guard.wait_for_attempt(diagnosis_complete=True)
    perform_one_corrected_guesty_put()
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterator

MIN_WAIT_SECONDS = 30.0
MAX_ATTEMPTS_PER_RUN = 2
STATE_VERSION = 1
MAX_FUTURE_CLOCK_SKEW_SECONDS = 300.0
DEFAULT_STATE_PATH = (
    Path.home() / ".cache" / "guesty-ha-plugin" / "live-keycode-write-guard.json"
)


class LiveWriteGuardError(RuntimeError):
    """Base error raised by the live-write safety guard."""


class LiveWriteLimitError(LiveWriteGuardError):
    """Raised when a test run asks for more than two write permits."""


class LiveWriteDiagnosisRequiredError(LiveWriteGuardError):
    """Raised when a second attempt has not been preceded by diagnosis."""


class LiveWriteGuardStateError(LiveWriteGuardError):
    """Raised when persisted timing state is unsafe to use."""


@dataclass(frozen=True, slots=True)
class LiveWritePermit:
    """One already-accounted Guesty write attempt."""

    attempt: int
    granted_at: float


class GuestyLiveWriteGuard:
    """Enforce conservative spacing and limits for live Guesty write tests."""

    def __init__(
        self,
        state_path: Path = DEFAULT_STATE_PATH,
        *,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """Arm a new two-attempt test run.

        Instantiate only after read-only preflight. The first permit cannot be
        granted until a full 30 seconds after this point.
        """
        self._state_path = Path(state_path)
        self._lock_path = self._state_path.with_suffix(
            f"{self._state_path.suffix}.lock"
        )
        self._clock = clock
        self._sleeper = sleeper
        self._armed_at = self._safe_now()
        self._attempts = 0
        self._last_local_attempt: float | None = None

    @property
    def attempts_used(self) -> int:
        """Return the number of permits consumed by this test run."""
        return self._attempts

    @property
    def attempts_remaining(self) -> int:
        """Return the remaining permits in this test run."""
        return MAX_ATTEMPTS_PER_RUN - self._attempts

    def wait_for_attempt(
        self,
        *,
        diagnosis_complete: bool = False,
    ) -> LiveWritePermit:
        """Wait safely, persist one attempt, and return its permit.

        The first attempt always waits 30 seconds after the guard is armed. A
        second attempt always waits at least 30 seconds after the first and
        requires the caller to confirm that the first result was analysed.
        Persisted timing also prevents two separate processes from obtaining
        permits less than 30 seconds apart.
        """
        if self._attempts >= MAX_ATTEMPTS_PER_RUN:
            raise LiveWriteLimitError(
                "A live Guesty test may perform at most two write attempts"
            )
        if self._attempts and not diagnosis_complete:
            raise LiveWriteDiagnosisRequiredError(
                "Analyse the first result before requesting a second attempt"
            )

        local_reference = (
            self._last_local_attempt
            if self._last_local_attempt is not None
            else self._armed_at
        )
        local_not_before = local_reference + MIN_WAIT_SECONDS

        while True:
            with self._locked_state():
                now = self._safe_now()
                persisted_last = self._read_last_attempt(now)
                global_not_before = (
                    persisted_last + MIN_WAIT_SECONDS
                    if persisted_last is not None
                    else now
                )
                not_before = max(local_not_before, global_not_before)
                delay = not_before - now
                if delay <= 0:
                    self._write_last_attempt(now)
                    self._attempts += 1
                    self._last_local_attempt = now
                    return LiveWritePermit(
                        attempt=self._attempts,
                        granted_at=now,
                    )

            # Do not hold the cross-process lock while sleeping. Recheck the
            # persisted timestamp afterward in case another process wrote.
            self._sleeper(delay)

    def _safe_now(self) -> float:
        """Return a finite wall-clock timestamp."""
        now = float(self._clock())
        if not math.isfinite(now) or now < 0:
            raise LiveWriteGuardStateError("The live-test clock is invalid")
        return now

    @contextmanager
    def _locked_state(self) -> Iterator[None]:
        """Hold the private cross-process state lock."""
        self._state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self._state_path.parent.chmod(0o700)
        except OSError:
            pass

        descriptor = os.open(
            self._lock_path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "a+", encoding="utf-8") as lock_file:
                descriptor = -1
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _read_last_attempt(self, now: float) -> float | None:
        """Read and validate the persisted last-attempt timestamp."""
        try:
            raw: Any = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as err:
            raise LiveWriteGuardStateError(
                "The live-write guard state is unreadable"
            ) from err

        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            raise LiveWriteGuardStateError(
                "The live-write guard state has an unsupported format"
            )
        value = raw.get("last_attempt_at")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise LiveWriteGuardStateError(
                "The persisted live-write timestamp is invalid"
            )
        timestamp = float(value)
        if timestamp > now + MAX_FUTURE_CLOCK_SKEW_SECONDS:
            raise LiveWriteGuardStateError(
                "The persisted live-write timestamp is unexpectedly in the future"
            )
        return timestamp

    def _write_last_attempt(self, timestamp: float) -> None:
        """Atomically persist a consumed attempt before the network write."""
        payload = json.dumps(
            {
                "version": STATE_VERSION,
                "last_attempt_at": timestamp,
            },
            separators=(",", ":"),
        )
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._state_path.parent,
                prefix=f".{self._state_path.name}.",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                os.fchmod(temporary.fileno(), 0o600)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self._state_path)
            temporary_path = None
            self._state_path.chmod(0o600)
        except OSError as err:
            raise LiveWriteGuardStateError(
                "Could not persist the live-write attempt"
            ) from err
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
