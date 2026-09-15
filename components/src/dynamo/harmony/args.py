#  SPDX-FileCopyrightText: Copyright (c) 2026 Datadog, Inc. All rights reserved.
#  SPDX-License-Identifier: Apache-2.0

"""CLI surface for `python -m dynamo.harmony`.

Two argument families are deliberately kept separate:

* Dynamo-side args (`--namespace`, `--served-model-name`, ...) shape the
  `WorkerConfig` and never reach Harmony.
* Harmony-side args are *not* enumerated here. `harmony-inference` owns a
  large clap surface (tp/pp/dp/dep/kv-cache-len/spec-decode/...) that moves
  independently of Dynamo, so mirroring it would rot. Everything after `--`
  is forwarded verbatim, which also means a Harmony flag we have never heard
  of works on day one.
"""

from __future__ import annotations

import argparse

from dynamo.llm import ModelInput

from dynamo.common.backend.worker import WorkerConfig

# Harmony's launcher defaults (src/mangrove/mangrove/src/harmony_launcher.rs,
# LauncherArgs). Mirrored rather than imported: they are Rust clap defaults.
DEFAULT_HTTP_PORT = 20053
DEFAULT_QUEUE_PORT = 20052
DEFAULT_MASTER_PORT = 7777
DEFAULT_MODEL_REGISTRY = "/model_registry/"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dynamo.harmony",
        description="Dynamo worker backed by the Harmony inference runtime.",
        epilog=(
            "Arguments after a bare `--` are forwarded verbatim to "
            "harmony-inference, e.g. `-- --tp 4 --dp 2 --max-sequence-len 32768`."
        ),
    )

    # --- what Harmony serves -------------------------------------------------
    parser.add_argument(
        "--model",
        required=True,
        help=(
            "Harmony model-registry key or checkpoint URI. Passed through as "
            "harmony-inference --model. This is NOT a HuggingFace path: "
            "Harmony consumes its own packed checkpoint format."
        ),
    )
    parser.add_argument(
        "--served-model-name",
        default=None,
        help="Name advertised to the frontend and matched against request `model`.",
    )

    # --- Dynamo runtime placement -------------------------------------------
    parser.add_argument("--namespace", default="dynamo")
    parser.add_argument("--component", default="backend")
    parser.add_argument("--endpoint", default="generate")
    parser.add_argument("--discovery-backend", default="etcd")
    parser.add_argument("--request-plane", default="tcp")
    parser.add_argument("--event-plane", default=None)

    # --- how we reach the Harmony process -----------------------------------
    parser.add_argument(
        "--harmony-binary",
        default="harmony-inference",
        help="Path to (or name on PATH of) the harmony-inference binary.",
    )
    parser.add_argument(
        "--harmony-http-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help="Harmony's OpenAI webserver port (HTTP_PORT).",
    )
    parser.add_argument(
        "--harmony-queue-port",
        type=int,
        default=DEFAULT_QUEUE_PORT,
        help="Harmony's internal gRPC queue port (QUEUE_PORT).",
    )
    parser.add_argument(
        "--harmony-master-port",
        type=int,
        default=DEFAULT_MASTER_PORT,
        help="Harmony's worker rendezvous port (MASTER_PORT).",
    )
    parser.add_argument(
        "--harmony-model-registry",
        default=DEFAULT_MODEL_REGISTRY,
        help="HARMONY_SETTING_MODEL_REGISTRY_ROOT for the child process.",
    )
    parser.add_argument(
        "--harmony-working-dir",
        default="/tmp/harmony",
        help="HARMONY_SETTING_WORKING_DIR for the child process.",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=1800.0,
        help=(
            "Seconds to wait for Harmony's /health to pass. Generous by "
            "default: weight load from a cold emptyDir dominates, and the "
            "operator's own readiness budget is the real ceiling."
        ),
    )
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help=(
            "Attach to an already-running Harmony at --harmony-http-port "
            "instead of spawning one. Used for local bring-up, where Harmony "
            "runs under `task` and only the Dynamo bridge is being iterated on."
        ),
    )

    return parser


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    """Split our args from Harmony's passthrough args at the first bare `--`.

    argparse's own REMAINDER handling is famously order-sensitive, so do the
    split ourselves before parsing: everything after the first standalone
    `--` belongs to Harmony.
    """
    import sys

    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw:
        split = raw.index("--")
        ours, theirs = raw[:split], raw[split + 1 :]
    else:
        ours, theirs = raw, []

    return build_parser().parse_args(ours), theirs


def build_worker_config(args: argparse.Namespace) -> WorkerConfig:
    """Map parsed args onto the framework's WorkerConfig.

    The three non-default choices here are the whole Tier-A contract, so they
    are called out rather than left implicit:

    * `model_input=Text` — Harmony tokenizes internally and its response
      fragments are text (`GenerateResponseFragment::Text`), never token ids.
      Registering `Tokens` would require Harmony to expose a token-level
      surface it does not have today.
    * `endpoint_types="chat"` — `/v1/chat/completions` is the only inference
      route Harmony's webserver mounts (plus `/v1/responses`, which Dynamo has
      no matching surface for). Advertising `completions` would publish a
      route that 404s at the Harmony hop.
    * `enable_kv_routing=False` — Harmony publishes no KV events. Its prefix
      cache is internal to `allocate_on_best`, which ranks data-parallel
      replicas inside one process and knows nothing about sibling pods. Until
      Harmony emits events, the frontend must round-robin rather than pretend
      to route on cache state.
    """
    served = args.served_model_name or args.model

    return WorkerConfig(
        namespace=args.namespace,
        component=args.component,
        endpoint=args.endpoint,
        model_name=served,
        served_model_name=served,
        model_input=ModelInput.Text,
        endpoint_types="chat",
        discovery_backend=args.discovery_backend,
        request_plane=args.request_plane,
        event_plane=args.event_plane,
        enable_kv_routing=False,
        # No local indexer: that endpoint answers prefix-overlap queries from
        # the frontend's router, which we are not participating in.
        enable_local_indexer=False,
    )
