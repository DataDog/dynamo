#  SPDX-FileCopyrightText: Copyright (c) 2026 Datadog, Inc. All rights reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Entry point: `python3 -m dynamo.harmony --model <harmony-model> [-- ...]`."""

from dynamo.common.backend.run import run
from dynamo.runtime.logging import configure_dynamo_logging

from .engine import HarmonyEngine


def main() -> None:
    configure_dynamo_logging()
    run(HarmonyEngine)


if __name__ == "__main__":
    main()
