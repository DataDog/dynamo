#  SPDX-FileCopyrightText: Copyright (c) 2026 Datadog, Inc. All rights reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Dynamo backend for Harmony (Applied AI's inference runtime).

Harmony is not an engine library Dynamo can import: it is a Rust control
plane (Mangrove) that embeds Python workers via PyO3 and exposes an
OpenAI-compatible HTTP surface. This backend therefore supervises the
`harmony-inference` process tree as a child and bridges its
`/v1/chat/completions` endpoint into a Dynamo endpoint.
"""
