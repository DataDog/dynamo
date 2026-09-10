#  SPDX-FileCopyrightText: Copyright (c) 2026 Datadog, Inc. All rights reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Bridge from a Dynamo raw request to Harmony's `/v1/chat/completions`.

Harmony emits textbook OpenAI SSE (`data: {...}` frames terminated by
`data: [DONE]`, with `:`-prefixed keep-alive comments every 5s), so this is a
transport shim rather than a translation layer. The two things it does own are
request sanitation -- Dynamo adds keys Harmony's serde would reject -- and
mapping a mid-stream Harmony failure onto an exception the framework can
surface as a `BackendError`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Keys Dynamo attaches to a raw request that Harmony must never see. Harmony's
# request struct is not `#[serde(deny_unknown_fields)]` today, but relying on
# that would make us break the day it tightens.
_DYNAMO_ONLY_KEYS = frozenset(
    {
        "_HEALTH_CHECK",
        "nvext",
        "dynamo",
    }
)


class HarmonyRequestError(RuntimeError):
    """Harmony rejected the request or failed mid-stream."""


def prepare_request(
    request: dict[str, Any], *, harmony_model: str, stream: bool
) -> dict[str, Any]:
    """Strip Dynamo-only keys and pin the fields Harmony resolves by name."""
    payload = {k: v for k, v in request.items() if k not in _DYNAMO_ONLY_KEYS}
    # The inbound `model` is the Dynamo served name, which need not match the
    # Harmony registry key the engine actually loaded. Harmony resolves its
    # own active model by this field, so it must be the Harmony-side name.
    payload["model"] = harmony_model
    payload["stream"] = stream
    return payload


class HarmonyClient:
    """Long-lived HTTP client against one Harmony webserver."""

    def __init__(self, base_url: str, *, harmony_model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._harmony_model = harmony_model
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        # No total timeout: a long generation is not a stuck one, and the
        # frontend's own request deadline plus Dynamo's cancellation path are
        # the correct place to bound a request. `sock_connect` still catches a
        # Harmony that has stopped listening.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10.0)
        self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def _live_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise HarmonyRequestError("Harmony client used before start()")
        return self._session

    async def health(self) -> bool:
        try:
            async with self._live_session.get(f"{self._base_url}/health") as response:
                return response.status == 200
        except aiohttp.ClientError:
            return False

    async def chat_completion(
        self, request: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield Harmony's response object(s) for one chat request.

        Mirrors the caller's `stream` flag rather than always streaming: with
        `stream=false` Harmony returns one `chat.completion` object, which is
        exactly the single terminal object `RawEngine.generate` is specified to
        yield in the non-streaming case.
        """
        stream = bool(request.get("stream", False))
        payload = prepare_request(
            request, harmony_model=self._harmony_model, stream=stream
        )
        url = f"{self._base_url}/v1/chat/completions"

        try:
            async with self._live_session.post(url, json=payload) as response:
                if response.status != 200:
                    body = await response.text()
                    raise HarmonyRequestError(
                        f"Harmony returned HTTP {response.status}: {body[:2048]}"
                    )
                if not stream:
                    yield await response.json()
                    return
                async for chunk in _iter_sse(response):
                    yield chunk
        except aiohttp.ClientError as exc:
            # A dropped connection mid-generation is the signature of a Harmony
            # worker crash. Surface it as an engine error so the frontend can
            # migrate the request instead of returning a truncated success.
            raise HarmonyRequestError(f"Harmony transport failure: {exc!r}") from exc


async def _iter_sse(
    response: aiohttp.ClientResponse,
) -> AsyncGenerator[dict[str, Any], None]:
    """Decode Harmony's SSE frames into response dicts.

    Reads line-oriented rather than using `response.content.iter_chunked`,
    because a chunk boundary can land mid-frame. Harmony sends one `data:`
    field per event, so multi-line data folding is not implemented -- if that
    ever changes, this loop is where it breaks, loudly, on a JSON parse error.
    """
    async for raw_line in response.content:
        line = raw_line.decode("utf-8").strip()
        if not line:
            continue
        if line.startswith(":"):
            # Keep-alive comment (axum's KeepAlive, 5s interval).
            continue
        if not line.startswith("data:"):
            logger.debug("Ignoring non-data SSE line from Harmony: %r", line)
            continue

        data = line[len("data:") :].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError as exc:
            raise HarmonyRequestError(
                f"Harmony sent an undecodable SSE frame: {data[:512]!r}"
            ) from exc
