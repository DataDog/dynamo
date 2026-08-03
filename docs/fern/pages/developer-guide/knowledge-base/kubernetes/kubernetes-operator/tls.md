---
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
title: Operator TLS
subtitle: Configure TLS once at the platform level and auto-inject it into every deployment
---

The Dynamo operator can inject TLS configuration into every
`DynamoGraphDeployment` (DGD) pod automatically, so you don't have to set the
`DYN_TCP_TLS_*` and `NATS_TLS_*` environment variables on each component. TLS
is configured once at the platform level via `InfrastructureConfiguration`, and
the operator propagates the corresponding env vars to all DGD pods it manages.

For the full list of TLS/mTLS environment variables and CLI flags, and for the
per-component configuration method, see the
[TLS reference](../reference/components/tls-configuration.mdx).

## Operator-level TLS configuration

Set the values in the operator Helm chart:

```yaml
tcpTLSCertPath: /etc/certs/server/cert.pem
tcpTLSKeyPath: /etc/certs/server/key.pem
tcpTLSCAPath: /etc/certs/ca/ca.pem
natsTLSCAPath: /etc/certs/ca/ca.pem
```

Or pass them via `--set` during `helm install`/`helm upgrade`:

```bash
helm upgrade dynamo-operator ... \
  --set tcpTLSCertPath=/etc/certs/server/cert.pem \
  --set tcpTLSKeyPath=/etc/certs/server/key.pem \
  --set tcpTLSCAPath=/etc/certs/ca/ca.pem \
  --set natsTLSCAPath=/etc/certs/ca/ca.pem
```

Per-component env vars in `podTemplate` take precedence over operator-level
values when both are set.

## Operator-level mTLS configuration

mTLS certificate paths can also be configured at the operator level:

```yaml
tcpTLSClientCertPath: /etc/certs/client/cert.pem
tcpTLSClientKeyPath: /etc/certs/client/key.pem
tcpTLSClientCAPath: /etc/certs/client-ca/ca.pem
natsTLSClientCertPath: /etc/certs/client/cert.pem
natsTLSClientKeyPath: /etc/certs/client/key.pem
```

The certificates themselves are typically delivered by a certificate management
system (e.g., cert-manager) and mounted into the pods at the paths referenced
above.
