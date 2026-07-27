"""Shared bounded HTTP response helpers."""

from __future__ import annotations

from typing import Protocol

_READ_CHUNK_BYTES = 64 * 1024


class AsyncReadable(Protocol):
    """Minimal async byte-stream interface used by aiohttp responses."""

    async def read(self, n: int = -1) -> bytes:
        """Read up to n bytes from the stream."""


class ResponseTooLargeError(Exception):
    """Raised when an HTTP response exceeds its configured size limit."""


async def async_read_limited(stream: AsyncReadable, max_bytes: int) -> bytes:
    """Read a complete stream without allowing it to exceed max_bytes."""
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")

    body = bytearray()
    while True:
        remaining = max_bytes + 1 - len(body)
        chunk = await stream.read(min(_READ_CHUNK_BYTES, remaining))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > max_bytes:
            raise ResponseTooLargeError
