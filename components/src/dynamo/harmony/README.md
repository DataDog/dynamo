<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Harmony backend

Runs [Harmony](https://github.com/DataDog/harmony) — Datadog Applied AI's
inference runtime — as a Dynamo worker.

Harmony is not an importable Python engine like vLLM or SGLang. It is a Rust
binary (`harmony-inference`, whose control plane is Mangrove) that embeds its
own Python workers through PyO3 and serves OpenAI-compatible HTTP. So this
module is a **supervisor and protocol bridge**, not an engine wrapper: it
launches `harmony-inference` as a child process, waits for it to become
healthy, registers the model with Dynamo, and forwards each request over
loopback HTTP.

```
Dynamo Frontend ──dyn://──> dynamo.harmony worker ──HTTP/SSE──> harmony-inference
                            (this module)          127.0.0.1     (child process)
```

## Why `RawEngine` and not `LLMEngine`

`components/src/dynamo/common/backend/` offers two engine base classes.
`LLMEngine` is the token pipeline: `token_ids` in, `token_ids` out. Harmony's
only inference routes are `/v1/chat/completions` and `/v1/responses`, and its
responses carry `GenerateResponseFragment::Text` — **text, never token IDs**.
There is no token-level surface to join `LLMEngine`'s contract, so this module
subclasses `RawEngine`: OpenAI request dict in, response dict out, registered
as `ModelInput.Text`.

Three consequences, all deliberate:

| Consequence | Why it's acceptable today |
|---|---|
| No KV-aware routing | Harmony publishes no KV cache events. Its prefix cache lives in the Mangrove allocator, which ranks data-parallel replicas *inside one process* and cannot see sibling pods. Routing is round-robin (Tier A). |
| No token telemetry | `RawEngineAdapter` doesn't count tokens, so there is no per-worker TTFT/ITL. Harmony's own metrics are the substitute until a token surface exists. |
| Name-only model registration | This is a **feature**. `is_raw() == true` makes Dynamo register the model name without loading a tokenizer from disk — correct, since Harmony reads its own packed checkpoint format, not a HuggingFace directory. |

Getting KV-aware routing (Tier B) requires Harmony to publish per-DP-rank KV
events and to expose token IDs; that is an upstream change, not something this
module can work around.

## Usage

```bash
python3 -m dynamo.harmony \
  --model /cache/huggingface/model \
  --served-model-name my-model \
  -- --tp 2 --kv-cache-len 65536
```

Everything after a bare `--` is forwarded **verbatim** to `harmony-inference`.
Harmony's engine flags move independently of Dynamo, so this module
deliberately does not mirror them into its own argparse surface.

### Flags

| Flag | Default | Notes |
|---|---|---|
| `--model` | required | Harmony model-registry key or checkpoint URI. **Not** a HuggingFace directory. |
| `--served-model-name` | `--model` | The name Dynamo advertises. |
| `--harmony-binary` | `harmony-inference` | Resolved on `PATH` unless absolute. |
| `--harmony-http-port` | `20053` | `HTTP_PORT` for the child. |
| `--harmony-queue-port` | `20052` | `QUEUE_PORT` for the child. |
| `--harmony-master-port` | `7777` | `MASTER_PORT` for the child. |
| `--harmony-model-registry` | `/model_registry/` | `HARMONY_SETTING_MODEL_REGISTRY_ROOT`. |
| `--harmony-working-dir` | unset | `HARMONY_SETTING_WORKING_DIR`. |
| `--startup-timeout` | `1800.0` | Seconds to wait for `/health`. Harmony compiles kernels on first start. |
| `--no-launch` | off | Attach to an already-running Harmony instead of spawning one. For local debugging. |

Plus the usual Dynamo runtime flags (`--namespace`, `--component`,
`--endpoint`, `--discovery-backend`, `--request-plane`, `--event-plane`).

## Operational notes

These are the non-obvious parts; each is commented at its site.

- **Process group, not process.** Harmony's PyO3 workers are forked children.
  `start_new_session=True` puts them in their own process group so `stop()` can
  signal the whole tree with `os.killpg`.
- **SIGTERM before SIGKILL.** `stop()` waits 30s after SIGTERM. A SIGKILL skips
  CUDA teardown and can leave the device dirty for the next pod.
- **Crash vs. slow start.** `_await_healthy()` checks `proc.returncode` before
  each `/health` poll, so a child that dies during model load surfaces
  immediately instead of after the 30-minute timeout.
- **Exit watchdog.** If Harmony exits while the worker is serving, the watchdog
  sends `SIGTERM` to *this* process so the framework deregisters and drains. A
  registered endpoint with no engine behind it is worse than a missing one.
- **Cancellation is a dropped response.** Harmony's HTTP API offers no abort
  route, so `generate()` returns early on `context.is_stopped()` and `abort()`
  stays the base-class no-op rather than pretending to do more.
- **`endpoint_types="chat"`.** Advertising `completions` would publish a route
  that 404s at the Harmony hop.
- **Single-pod tensor parallelism only.** `MASTER_ADDR`/`MASTER_PORT` are
  torchrun-shaped, but a Dynamo component is independent pods, not a gang. The
  launcher defaults `MASTER_ADDR` to `127.0.0.1`; multi-node TP would need
  gang scheduling (Grove) and is not wired up.

## Layout

```
harmony/
  args.py       # CLI surface + build_worker_config (the Tier-A registration contract)
  launcher.py   # HarmonyProcess — spawn, health-poll, watchdog, signal teardown
  client.py     # HarmonyClient — request translation + SSE parsing
  engine.py     # HarmonyEngine(RawEngine) — lifecycle wiring
  main.py       # configure_dynamo_logging() + run(HarmonyEngine)
  tests/
    test_client.py   # request translation + SSE framing
```

## Tests

`client.py` is the only module with no dependency on the compiled `dynamo`
bindings, so its tests run without a built wheel:

```bash
PYTHONPATH=components/src pytest components/src/dynamo/harmony/tests -q
```

`args.py`, `engine.py`, and `launcher.py` import `dynamo._core`, so testing
them needs `maturin develop` first.
