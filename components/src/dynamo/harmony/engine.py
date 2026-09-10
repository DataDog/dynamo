#  SPDX-FileCopyrightText: Copyright (c) 2026 Datadog, Inc. All rights reserved.
#  SPDX-License-Identifier: Apache-2.0

"""`HarmonyEngine` -- a `RawEngine` fronting the Harmony inference runtime.

Why `RawEngine` and not `LLMEngine`:

`LLMEngine` is the token pipeline. Dynamo tokenizes, hands the engine
`token_ids`, and detokenizes what comes back. Harmony cannot join that
contract today -- its only inference surface is `/v1/chat/completions`, and
its responses carry text fragments (`GenerateResponseFragment::Text`), never
token ids. `RawEngine` is the framework's other half: the frontend forwards
the OpenAI-shaped request as a JSON object and passes each yielded object
straight back. That is an exact match for what Harmony already speaks.

The cost of that choice, stated plainly so it is not discovered later:

* No KV-aware routing. `RawEngine` registers `ModelInput.Text` with no KV
  cache, so the frontend round-robins across pods. Harmony's own
  `allocate_on_best` still does prefix-aware placement *within* a pod's
  data-parallel replicas; the two levels simply do not compose yet.
* No token telemetry. The raw adapter does not compute TTFT/ITL, because it
  never sees tokens. Request-level latency and throughput still work.
* Name-only registration. `RawEngine` sets `name_only=true` when building the
  model deployment card, so Dynamo never loads a tokenizer from disk. This is
  a feature here: Harmony consumes its own packed checkpoint format, and
  there is no HuggingFace directory for Dynamo to read.

Moving to `LLMEngine` -- the prerequisite for KV-aware routing -- requires
Harmony to accept token ids and emit token ids, plus publish KV events. Those
are Harmony-side changes, not adapter work.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import AsyncGenerator
from typing import Any

from dynamo._core import Context

from dynamo.common.backend.engine import EngineConfig, RawEngine
from dynamo.common.backend.health_check import build_raw_health_check_payload
from dynamo.common.backend.worker import WorkerConfig

from .args import build_worker_config, parse_args
from .client import HarmonyClient
from .launcher import HarmonyProcess

logger = logging.getLogger(__name__)


class HarmonyEngine(RawEngine):
    def __init__(
        self,
        args: argparse.Namespace,
        passthrough_args: list[str],
    ) -> None:
        self._args = args
        self._passthrough_args = passthrough_args
        self._process: HarmonyProcess | None = None
        self._client: HarmonyClient | None = None
        self._served_name = args.served_model_name or args.model

    # ------------------------------------------------------------- lifecycle

    @classmethod
    async def from_args(
        cls, argv: list[str] | None = None
    ) -> tuple["HarmonyEngine", WorkerConfig]:
        args, passthrough = parse_args(argv)
        return cls(args, passthrough), build_worker_config(args)

    async def start(self, worker_id: int) -> EngineConfig:
        del worker_id  # one Harmony process per pod; no per-worker sharding

        if self._args.no_launch:
            logger.info(
                "--no-launch: attaching to an existing Harmony on port %d",
                self._args.harmony_http_port,
            )
            base_url = f"http://127.0.0.1:{self._args.harmony_http_port}"
        else:
            self._process = HarmonyProcess(
                binary=self._args.harmony_binary,
                model=self._args.model,
                http_port=self._args.harmony_http_port,
                queue_port=self._args.harmony_queue_port,
                master_port=self._args.harmony_master_port,
                model_registry=self._args.harmony_model_registry,
                working_dir=self._args.harmony_working_dir,
                passthrough_args=self._passthrough_args,
                startup_timeout=self._args.startup_timeout,
            )
            await self._process.start()
            base_url = self._process.base_url

        self._client = HarmonyClient(base_url, harmony_model=self._args.model)
        await self._client.start()

        # `llm=None` is the RawEngine contract: no KV block size, no DP range,
        # no bootstrap endpoint. Everything in LlmRegistration describes a
        # token pipeline this worker does not have.
        return EngineConfig(
            model=self._served_name,
            served_model_name=self._served_name,
            llm=None,
        )

    async def cleanup(self) -> None:
        # Called exactly once, including after a failed start() -- so every
        # field must be treated as possibly unset.
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._process is not None:
            await self._process.stop()
            self._process = None

    # -------------------------------------------------------------- serving

    async def health_check_payload(self) -> dict[str, Any]:
        # The canary goes through generate() like any request, so it must be a
        # request Harmony will actually accept. One token, greedy, no stream:
        # cheap enough to run on the operator's probe interval.
        return build_raw_health_check_payload(
            {
                "model": self._served_name,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "temperature": 0.0,
                "stream": False,
            }
        )

    async def generate(
        self, request: dict[str, Any], context: Context
    ) -> AsyncGenerator[dict[str, Any], None]:
        if self._client is None:
            raise RuntimeError("HarmonyEngine.generate called before start()")

        async for chunk in self._client.chat_completion(request):
            # Harmony's HTTP API has no cancel verb, so stopping the iteration
            # is the only cancellation available: it drops the HTTP response,
            # which closes the connection and lets Harmony's own
            # ReceiverStream teardown reclaim the sequence. abort() is
            # therefore left at the base-class no-op rather than pretending to
            # do something stronger.
            if context.is_stopped() or context.is_killed():
                logger.debug(
                    "Request %s cancelled; closing Harmony stream", context.id()
                )
                return
            yield chunk
