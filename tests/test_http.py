"""Tests for shared bounded HTTP response reading."""

from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest

from custom_components.guesty._http import (
    ResponseTooLargeError,
    async_read_limited,
)


@pytest.mark.asyncio
async def test_reader_rejects_negative_limit() -> None:
    """A programming error cannot turn the hard limit into an unbounded read."""
    with pytest.raises(ValueError, match="must not be negative"):
        await async_read_limited(AsyncMock(), -1)


@pytest.mark.asyncio
async def test_reader_accepts_exact_limit_and_waits_for_eof() -> None:
    """Exactly max_bytes is valid only after a final EOF read."""
    stream = AsyncMock()
    stream.read = AsyncMock(side_effect=[b"1234", b"5678", b""])

    assert await async_read_limited(stream, 8) == b"12345678"
    assert stream.read.await_args_list == [call(9), call(5), call(1)]


@pytest.mark.asyncio
async def test_reader_rejects_limit_exceeded_across_fragments() -> None:
    """Many small reads cannot bypass the aggregate response limit."""
    stream = AsyncMock()
    stream.read = AsyncMock(side_effect=[b"123", b"456", b"789"])

    with pytest.raises(ResponseTooLargeError):
        await async_read_limited(stream, 8)

    assert stream.read.await_args_list == [call(9), call(6), call(3)]
