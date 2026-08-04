"""Hard safety guard for manual Guesty Keycode write tests.

Create the guard only after all read-only preflight checks have completed and
the target reservation and payload are frozen. Every permit is persisted
*before* the caller performs the network request, so failed and ambiguous
writes consume an attempt as well.

Example:

    token_cache = GuestyLiveTokenCache()
    token = token_cache.get_or_fetch(
        client_id,
        client_secret,
        fetch_one_oauth_token,
    ).access_token

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
import hashlib
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
TOKEN_CACHE_STATE_VERSION = 1
TOKEN_REFRESH_MARGIN_SECONDS = 300.0
DEFAULT_TOKEN_CACHE_PATH = (
    Path.home() / ".cache" / "guesty-ha-plugin" / "live-oauth-token-cache.json"
)


class LiveWriteGuardError(RuntimeError):
    """Base error raised by the live-write safety guard."""


class LiveWriteLimitError(LiveWriteGuardError):
    """Raised when a test run asks for more than two write permits."""


class LiveWriteDiagnosisRequiredError(LiveWriteGuardError):
    """Raised when a second attempt has not been preceded by diagnosis."""


class LiveWriteGuardStateError(LiveWriteGuardError):
    """Raised when persisted timing state is unsafe to use."""


class LiveTokenCacheError(RuntimeError):
    """Base error raised by the private live-test OAuth token cache."""


class LiveTokenCacheStateError(LiveTokenCacheError):
    """Raised when cached authentication state cannot be trusted."""


@dataclass(frozen=True, slots=True)
class LiveWritePermit:
    """One already-accounted Guesty write attempt."""

    attempt: int
    granted_at: float


@dataclass(frozen=True, slots=True)
class LiveOAuthToken:
    """One reusable OAuth token and its absolute expiration timestamp."""

    access_token: str
    expires_at: float


class GuestyLiveTokenCache:
    """Reuse one Guesty token across related manual live-test processes."""

    def __init__(
        self,
        state_path: Path = DEFAULT_TOKEN_CACHE_PATH,
        *,
        clock: Callable[[], float] = time.time,
        refresh_margin: float = TOKEN_REFRESH_MARGIN_SECONDS,
    ) -> None:
        """Initialize a private cross-process token cache."""
        self._state_path = Path(state_path)
        self._lock_path = self._state_path.with_suffix(
            f"{self._state_path.suffix}.lock"
        )
        self._clock = clock
        self._refresh_margin = float(refresh_margin)
        if not math.isfinite(self._refresh_margin) or self._refresh_margin < 0:
            raise ValueError("Token refresh margin must be finite and non-negative")

    def get_or_fetch(
        self,
        client_id: str,
        client_secret: str,
        fetcher: Callable[[], LiveOAuthToken],
    ) -> LiveOAuthToken:
        """Return a reusable token or fetch and atomically cache exactly one.

        The fetcher runs while the cross-process lock is held. This is
        intentional: two diagnostic processes must never mint separate tokens
        for the same credentials merely because they started concurrently.
        """
        normalized_client_id = client_id.strip()
        normalized_client_secret = client_secret.strip()
        if not normalized_client_id or not normalized_client_secret:
            raise ValueError("Guesty Client ID and Client Secret are required")
        if not callable(fetcher):
            raise TypeError("Token fetcher must be callable")

        credential_fingerprint = hashlib.sha256(
            f"{normalized_client_id}\0{normalized_client_secret}".encode()
        ).hexdigest()
        with self._locked_state():
            now = self._safe_now()
            cached = self._read_cached_token(credential_fingerprint, now)
            if cached is not None:
                return cached

            fetched = fetcher()
            token = self._validate_fetched_token(fetched, now)
            self._write_cached_token(credential_fingerprint, token)
            return token

    def _safe_now(self) -> float:
        """Return a finite wall-clock timestamp."""
        now = float(self._clock())
        if not math.isfinite(now) or now < 0:
            raise LiveTokenCacheStateError("The live-test clock is invalid")
        return now

    @contextmanager
    def _locked_state(self) -> Iterator[None]:
        """Hold the private cross-process token-cache lock."""
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

    def _read_cached_token(
        self,
        credential_fingerprint: str,
        now: float,
    ) -> LiveOAuthToken | None:
        """Return one valid matching token, or None when refresh is required."""
        try:
            raw: Any = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as err:
            raise LiveTokenCacheStateError(
                "The live OAuth token cache is unreadable"
            ) from err

        if not isinstance(raw, dict) or raw.get("version") != TOKEN_CACHE_STATE_VERSION:
            raise LiveTokenCacheStateError(
                "The live OAuth token cache has an unsupported format"
            )
        stored_fingerprint = raw.get("credential_fingerprint")
        access_token = raw.get("access_token")
        expires_at = raw.get("expires_at")
        if (
            not isinstance(stored_fingerprint, str)
            or len(stored_fingerprint) != 64
            or not isinstance(access_token, str)
            or not access_token
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
            or float(expires_at) < 0
        ):
            raise LiveTokenCacheStateError("The live OAuth token cache is invalid")
        if stored_fingerprint != credential_fingerprint:
            return None
        if float(expires_at) <= now + self._refresh_margin:
            return None
        return LiveOAuthToken(access_token=access_token, expires_at=float(expires_at))

    def _validate_fetched_token(
        self,
        token: LiveOAuthToken,
        now: float,
    ) -> LiveOAuthToken:
        """Validate a newly issued token before it reaches private storage."""
        if not isinstance(token, LiveOAuthToken):
            raise LiveTokenCacheStateError(
                "The token fetcher returned an unsupported result"
            )
        access_token = token.access_token.strip()
        try:
            expires_at = float(token.expires_at)
        except (TypeError, ValueError) as err:
            raise LiveTokenCacheStateError(
                "The token fetcher returned an invalid expiration"
            ) from err
        if (
            not access_token
            or not math.isfinite(expires_at)
            or expires_at <= now + self._refresh_margin
        ):
            raise LiveTokenCacheStateError(
                "The token fetcher returned an unusable token"
            )
        return LiveOAuthToken(access_token=access_token, expires_at=expires_at)

    def _write_cached_token(
        self,
        credential_fingerprint: str,
        token: LiveOAuthToken,
    ) -> None:
        """Atomically persist private token state without raw credentials."""
        payload = json.dumps(
            {
                "version": TOKEN_CACHE_STATE_VERSION,
                "credential_fingerprint": credential_fingerprint,
                "access_token": token.access_token,
                "expires_at": token.expires_at,
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
            raise LiveTokenCacheStateError(
                "Could not persist the live OAuth token"
            ) from err
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


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
