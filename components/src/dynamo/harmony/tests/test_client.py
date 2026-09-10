#  SPDX-FileCopyrightText: Copyright (c) 2026 Datadog, Inc. All rights reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Tests for the Harmony SSE bridge.

Only `client.py` is covered here: it is the one module in this backend with
no dependency on the compiled `dynamo` bindings, and it holds the parsing
logic most likely to break when Harmony's webserver changes. Engine
lifecycle and arg handling are covered by the integration path, which needs
the built wheel.
"""

from __future__ import annotations

import json

import pytest

from dynamo.harmony.client import (
    HarmonyRequestError,
    _iter_sse,
    prepare_request,
)

pytestmark = pytest.mark.unit


class _FakeContent:
    """Stands in for `aiohttp.ClientResponse.content` (async line iterator)."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for line in self._lines:
            yield line


class _FakeResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self.content = _FakeContent(lines)


def _sse(*objects: dict) -> list[bytes]:
    return [f"data: {json.dumps(obj)}\n".encode() for obj in objects]


async def _collect(lines: list[bytes]) -> list[dict]:
    return [chunk async for chunk in _iter_sse(_FakeResponse(lines))]


# --------------------------------------------------------------- prepare_request


def test_prepare_request_pins_harmony_model_name():
    # The inbound `model` is the Dynamo served name; Harmony resolves its
    # active model by this field, so it must be rewritten.
    payload = prepare_request(
        {"model": "gemma4-26b", "messages": []},
        harmony_model="gemma-4-26b-a4b-it",
        stream=True,
    )
    assert payload["model"] == "gemma-4-26b-a4b-it"
    assert payload["stream"] is True


def test_prepare_request_strips_dynamo_only_keys():
    payload = prepare_request(
        {
            "messages": [],
            "_HEALTH_CHECK": True,
            "nvext": {"ignore_eos": True},
            "temperature": 0.7,
        },
        harmony_model="m",
        stream=False,
    )
    assert "_HEALTH_CHECK" not in payload
    assert "nvext" not in payload
    # Everything Harmony does understand survives untouched.
    assert payload["temperature"] == 0.7
    assert payload["stream"] is False


def test_prepare_request_does_not_mutate_caller_dict():
    original = {"model": "a", "_HEALTH_CHECK": True}
    prepare_request(original, harmony_model="b", stream=True)
    assert original == {"model": "a", "_HEALTH_CHECK": True}


# --------------------------------------------------------------------- _iter_sse


@pytest.mark.asyncio
async def test_iter_sse_yields_each_frame():
    lines = _sse(
        {"choices": [{"delta": {"content": "he"}}]},
        {"choices": [{"delta": {"content": "llo"}}]},
    )
    lines.append(b"data: [DONE]\n")
    assert await _collect(lines) == [
        {"choices": [{"delta": {"content": "he"}}]},
        {"choices": [{"delta": {"content": "llo"}}]},
    ]


@pytest.mark.asyncio
async def test_iter_sse_stops_at_done_and_ignores_trailing_frames():
    lines = [
        b'data: {"a": 1}\n',
        b"data: [DONE]\n",
        b'data: {"never": true}\n',
    ]
    assert await _collect(lines) == [{"a": 1}]


@pytest.mark.asyncio
async def test_iter_sse_skips_keepalive_comments_and_blank_lines():
    # axum's KeepAlive emits a bare `:` comment every 5s; a long prefill makes
    # these the *only* traffic for minutes, so mis-parsing one is fatal.
    lines = [
        b"\n",
        b": keep-alive\n",
        b":\n",
        b'data: {"a": 1}\n',
        b"\n",
        b"data: [DONE]\n",
    ]
    assert await _collect(lines) == [{"a": 1}]


@pytest.mark.asyncio
async def test_iter_sse_tolerates_missing_space_after_data_colon():
    assert await _collect([b'data:{"a": 1}\n', b"data: [DONE]\n"]) == [{"a": 1}]


@pytest.mark.asyncio
async def test_iter_sse_ends_cleanly_when_stream_closes_without_done():
    # Harmony always sends [DONE], but a truncated stream must not hang or
    # raise -- the framework treats a short stream as a finished one.
    assert await _collect([b'data: {"a": 1}\n']) == [{"a": 1}]


@pytest.mark.asyncio
async def test_iter_sse_raises_on_undecodable_frame():
    # Fail loudly: silently dropping a malformed frame would surface as
    # missing output tokens, which is far harder to diagnose.
    with pytest.raises(HarmonyRequestError, match="undecodable SSE frame"):
        await _collect([b"data: {not json}\n"])


@pytest.mark.asyncio
async def test_iter_sse_ignores_unknown_sse_fields():
    lines = [
        b"event: message\n",
        b"id: 7\n",
        b'data: {"a": 1}\n',
        b"data: [DONE]\n",
    ]
    assert await _collect(lines) == [{"a": 1}]
