#  SPDX-FileCopyrightText: Copyright (c) 2026 Datadog, Inc. All rights reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Supervision of the `harmony-inference` child process.

Harmony's launcher is the whole engine: it starts Mangrove's control plane,
forks per-GPU workers that embed Python through PyO3, loads the packed
checkpoint, and only then mounts its OpenAI webserver. We cannot import any
of that, so this module treats it as a service dependency that happens to
live in our own PID namespace.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time

import aiohttp

logger = logging.getLogger(__name__)

# Poll cadence while waiting for /health. Weight load is minutes-scale, so a
# tight poll buys nothing; 2s keeps the "still waiting" logs readable.
_HEALTH_POLL_INTERVAL = 2.0

# Grace given to the whole process group on shutdown before SIGKILL. Harmony
# workers hold GPU memory, and a SIGKILL that skips CUDA teardown can leave
# the device dirty for the next pod scheduled onto it.
_TERM_GRACE_SECONDS = 30.0


class HarmonyLaunchError(RuntimeError):
    """Harmony failed to start, or exited before it became healthy."""


class HarmonyProcess:
    """Owns the lifetime of one `harmony-inference` process group."""

    def __init__(
        self,
        *,
        binary: str,
        model: str,
        http_port: int,
        queue_port: int,
        master_port: int,
        model_registry: str,
        working_dir: str,
        passthrough_args: list[str],
        startup_timeout: float,
    ) -> None:
        self._binary = binary
        self._model = model
        self._http_port = http_port
        self._queue_port = queue_port
        self._master_port = master_port
        self._model_registry = model_registry
        self._working_dir = working_dir
        self._passthrough_args = passthrough_args
        self._startup_timeout = startup_timeout

        self._proc: asyncio.subprocess.Process | None = None
        self._watchdog: asyncio.Task | None = None
        self._stopping = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._http_port}"

    # ------------------------------------------------------------------ start

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "HTTP_PORT": str(self._http_port),
                "QUEUE_PORT": str(self._queue_port),
                "MASTER_PORT": str(self._master_port),
                "HARMONY_SETTING_MODEL_REGISTRY_ROOT": self._model_registry,
                "HARMONY_SETTING_WORKING_DIR": self._working_dir,
            }
        )
        # MASTER_ADDR has no clap default and Harmony refuses to start without
        # it. A DGD component is a set of independent pods, not a gang, so the
        # only rendezvous target that is correct for every replica is the pod
        # itself -- single-pod tensor parallelism only. Multi-node TP would
        # need a StatefulSet-shaped identity the operator does not give us.
        env.setdefault("MASTER_ADDR", "127.0.0.1")
        # Bind loopback rather than Harmony's `::` default: nothing outside the
        # pod should reach the engine directly, and the Dynamo endpoint is the
        # only intended ingress.
        env.setdefault("HARMONY_BIND_ADDR", "127.0.0.1")
        # N_GPUS is deliberately left unset so Harmony's own get_gpu_count()
        # reads the device list the NVIDIA runtime hook gave this container.
        return env

    def _argv(self) -> list[str]:
        return [self._binary, "--model", self._model, *self._passthrough_args]

    async def start(self) -> None:
        argv = self._argv()
        logger.info("Launching Harmony: %s", " ".join(argv))

        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            env=self._child_env(),
            # Inherit stdout/stderr: Harmony logs are the engine logs, and the
            # pod's `ad.datadoghq.com/main.logs` annotation already collects
            # this container's streams. Piping them would mean re-emitting
            # every line through Python for no gain.
            stdout=None,
            stderr=None,
            # New session so Harmony's forked GPU workers land in a process
            # group we can signal as a unit. Without this, SIGTERM reaches the
            # launcher only and the workers survive holding GPU memory.
            start_new_session=True,
        )
        logger.info("Harmony launched with pid %d", self._proc.pid)

        try:
            await self._await_healthy()
        except Exception:
            # Startup failure must not leave a half-initialized process group
            # holding GPUs -- cleanup() may never run if start() raised before
            # the framework took ownership.
            await self.stop()
            raise

        self._watchdog = asyncio.create_task(
            self._watch_for_exit(), name="harmony-watchdog"
        )

    async def _await_healthy(self) -> None:
        deadline = time.monotonic() + self._startup_timeout
        url = f"{self.base_url}/health"
        last_error: str | None = None

        timeout = aiohttp.ClientTimeout(total=5.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while time.monotonic() < deadline:
                # Check liveness first: a crashed Harmony would otherwise be
                # indistinguishable from a slow one until the timeout expires,
                # turning a 20-second config error into a 30-minute one.
                if self._proc is not None and self._proc.returncode is not None:
                    raise HarmonyLaunchError(
                        f"Harmony exited with code {self._proc.returncode} "
                        f"before becoming healthy (last probe: {last_error})"
                    )
                try:
                    async with session.get(url) as response:
                        if response.status == 200:
                            waited = self._startup_timeout - (
                                deadline - time.monotonic()
                            )
                            logger.info("Harmony reported healthy after %.1fs", waited)
                            return
                        last_error = f"HTTP {response.status}"
                except aiohttp.ClientError as exc:
                    last_error = repr(exc)
                except asyncio.TimeoutError:
                    last_error = "probe timed out"

                logger.info(
                    "Waiting for Harmony /health (%s)", last_error or "no response yet"
                )
                await asyncio.sleep(_HEALTH_POLL_INTERVAL)

        raise HarmonyLaunchError(
            f"Harmony did not become healthy within {self._startup_timeout}s "
            f"(last probe: {last_error})"
        )

    # --------------------------------------------------------------- watchdog

    async def _watch_for_exit(self) -> None:
        """Turn an unexpected Harmony exit into worker shutdown.

        Without this the Dynamo endpoint stays registered and the frontend
        keeps routing to a pod whose engine is gone. Raising here would only
        fault a background task, so signal ourselves instead and let the
        framework's handler deregister, drain, and exit -- which the operator
        then sees as a pod restart.
        """
        assert self._proc is not None
        returncode = await self._proc.wait()
        if self._stopping:
            return
        logger.error(
            "Harmony exited unexpectedly with code %s; shutting down worker",
            returncode,
        )
        os.kill(os.getpid(), signal.SIGTERM)

    # ------------------------------------------------------------------- stop

    async def stop(self) -> None:
        """Terminate the process group. Idempotent and null-safe."""
        self._stopping = True

        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

        proc = self._proc
        if proc is None or proc.returncode is not None:
            self._proc = None
            return

        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            self._proc = None
            return

        logger.info("Sending SIGTERM to Harmony process group %d", pgid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            self._proc = None
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_SECONDS)
            logger.info("Harmony exited with code %s", proc.returncode)
        except asyncio.TimeoutError:
            logger.warning(
                "Harmony did not exit within %.0fs; sending SIGKILL",
                _TERM_GRACE_SECONDS,
            )
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()

        self._proc = None
