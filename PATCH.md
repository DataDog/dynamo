# Dynamo Fork Patches — v1.4.0

This file tracks every patch carried on the `v1.4.0` branch of DataDog/dynamo,
based on upstream `release/1.4.0`. Replayed from the `v1.3.0` branch.

## NATS TLS Support

| Commit | Purpose |
|--------|---------|
| `50d8959a00` | feat(runtime): add NATS TLS/mTLS support (v1.3.0-dev.1 backport) |
| `69e57afd68` | fix(runtime): install ring crypto provider before NATS connect and detect tls:// URL |
| `db0c75216a` | feat(operator): inject NATS_TLS_CA_CERT_PATH from operator config |
| `1ee0dedc3c` | feat(helm): add natsTLSCAPath to operator chart for NATS TLS CA injection |
| `008f657bf9` | feat(operator): auto-inject TCP TLS env vars from operator config |
| `259c8457c4` | fix(operator): use individual if blocks for TCP TLS env injection |
| `8d018a0dc6` | fix: remove duplicate tokio-rustls/rustls entries from workspace Cargo.toml |
| `adf4a1558c` | fix(runtime): fix variable rename inconsistency in nats connect() |
| `390b4d8511` | fix(runtime): restore dropped NATS TLS fix from Walid's fc612b9b48 |

## TCP TLS / mTLS

| Commit | Purpose |
|--------|---------|
| `8a0f6b1a92` | feat(runtime): add opt-in TLS and mTLS for NATS and TCP transports |
| `a9d9ceff61` | docs: add HOTFIX.md explaining fork branching model and hotfix flow |
| `1f3e2ff0f8` | docs: document fork branching model and hotfix flow in README |
| `ce733e5404` | feat(runtime): add TLS support to TCP request plane |
| `437a67d758` | feat(runtime): address review feedback — CLI flags, timeout, and cleanup |
| `0b45dea5d2` | fix(runtime): honor --no-tcp-tls-insecure over inherited env var |
| `02cd1fe435` | style: fix black formatting for long env var assignment |
| `f2fb9bac32` | refactor(frontend): decouple HTTP TLS from TCP TLS |
| `b66e15e697` | docs(kubernetes): add TCP TLS configuration guide |
| `4c774f7af0` | docs(kubernetes): clarify TCP TLS scope covers response stream path only |
| `6751516648` | fix(runtime): use dynamo-truthy for TCP TLS boolean parsing |
| `97ceff0734` | docs(kubernetes): fix TLS page navigation and k8s deployment example |
| `acb0f664ff` | feat(runtime): add TLS to TCP request plane (egress/ingress) |
| `c071bb55eb` | feat(runtime): add TLS support to NATS transport |
| `dec21edcc9` | feat(runtime): add TLS support to NATS transport |
| `8c0472299b` | feat(runtime): add mutual TLS (mTLS) support for TCP and NATS transports |
| `1c997272ad` | feat(operator): add TLS/mTLS auto-injection via InfrastructureConfiguration |

## Documentation

| Commit | Purpose |
|--------|---------|
| `2d9240fd8a` | docs(tls): generalize page title from TCP TLS to TLS |
| `9f70a6d079` | docs(tls): split operator-level TLS config into a Kubernetes Operator page |
| `bc29d3fa56` | docs(request-plane): link to the TLS reference from TCP config options |

## Request-Plane mTLS Hardening

| Commit | Purpose |
|--------|---------|
| `bb89dd315c` | fix(runtime): return error instead of panicking on invalid request-plane TLS config |
| `0c31556358` | fix(runtime): enforce request-plane mTLS |
| `800a0e208e` | fix(runtime): complete request-plane mTLS |
| `786d2e9d6b` | test(runtime): cover request-plane mTLS configuration |
| `c5def078c1` | fix(runtime): flush request-plane TLS writes |
| `8e41041bcd` | test(runtime): soften request-plane mTLS client reject assert |

## TLS Identity Reload (Certificate Rotation)

| Commit | Purpose |
|--------|---------|
| `40794fd806` | fix(runtime): reload rotated TLS identities |
| `f3bcaf42c7` | fix(runtime): retry TLS identity reload after transient failures |
| `10c938cf8b` | Potential fix for pull request finding |
| `46b9023da0` | fix(runtime): reload TLS identities from content |

## Notes

- 3 merge commits (PRs #14, #15, #16) were skipped during cherry-pick as their
  constituent commits were already applied individually.
- 1 doc-move commit (`007caaa4c5`) was skipped as empty — the docs directory
  was reorganized differently in v1.4.0.
- Conflicts in `lib/runtime/src/pipeline/network/tcp/server.rs` and
  `lib/runtime/src/pipeline/network/egress/tcp_client.rs` were resolved by
  merging our TLS additions with upstream's `parking_lot::Mutex` migration
  and TCP client restructuring.
- Conflict resolution fixes (verified via `cargo check` + `cargo test`):
  - Added missing `}` closing brace in `build_request_plane_tls_connector`
  - Renamed `send_buf` → `write_buf` in error handling paths (upstream renamed the variable)
  - Deduplicated test module imports (`AsyncReadExt`, `TcpListener`, `TcpStream`)
- All TLS/mTLS/NATS tests pass: 10 request_plane tests, 8 connector/acceptor tests,
  10 NATS TLS tests — 28 total, 0 failures.
