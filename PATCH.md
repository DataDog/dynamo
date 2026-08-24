# Dynamo Fork Patches — v1.4.1

This file tracks every patch carried on the `v1.4.1` branch of DataDog/dynamo,
based on upstream `release/1.4.1`. Replayed from the `v1.4.0` branch.

## NATS TLS Support

| Commit | Purpose |
|--------|---------|
| `135f7a1aef` | feat(runtime): add NATS TLS/mTLS support (v1.3.0-dev.1 backport) |
| `ff37f45974` | fix(runtime): install ring crypto provider before NATS connect and detect tls:// URL |
| `bdec624959` | feat(operator): inject NATS_TLS_CA_CERT_PATH from operator config |
| `326d462d94` | feat(helm): add natsTLSCAPath to operator chart for NATS TLS CA injection |
| `ce109f2593` | feat(operator): auto-inject TCP TLS env vars from operator config |
| `685c85deab` | fix(operator): use individual if blocks for TCP TLS env injection |
| `6805f1c89d` | fix: remove duplicate tokio-rustls/rustls entries from workspace Cargo.toml |
| `3cc2666f57` | fix(runtime): fix variable rename inconsistency in nats connect() |
| `b93309ec18` | fix(runtime): restore dropped NATS TLS fix from Walid's fc612b9b48 |

## TCP TLS / mTLS

| Commit | Purpose |
|--------|---------|
| `e1a5ff307b` | feat(runtime): add opt-in TLS and mTLS for NATS and TCP transports |
| `703af5bbf5` | docs: add HOTFIX.md explaining fork branching model and hotfix flow |
| `7ff5cae7a3` | docs: document fork branching model and hotfix flow in README |
| `977d6d83bd` | feat(runtime): add TLS support to TCP request plane |
| `97c1300d3e` | feat(runtime): address review feedback — CLI flags, timeout, and cleanup |
| `6c0bfdd352` | fix(runtime): honor --no-tcp-tls-insecure over inherited env var |
| `4c195b4747` | style: fix black formatting for long env var assignment |
| `69928062fb` | refactor(frontend): decouple HTTP TLS from TCP TLS |
| `80a65275e0` | docs(kubernetes): add TCP TLS configuration guide |
| `7c165cce1b` | docs(kubernetes): clarify TCP TLS scope covers response stream path only |
| `5ecbe3cc0a` | fix(runtime): use dynamo-truthy for TCP TLS boolean parsing |
| `fcf34418aa` | docs(kubernetes): fix TLS page navigation and k8s deployment example |
| `733c1d7f1d` | feat(runtime): add TLS to TCP request plane (egress/ingress) |
| `9659880e1f` | feat(runtime): add TLS support to NATS transport |
| `3b3da89bc7` | feat(runtime): add TLS support to NATS transport |
| `06973f4fbf` | feat(runtime): add mutual TLS (mTLS) support for TCP and NATS transports |
| `ba75c3b6d3` | feat(operator): add TLS/mTLS auto-injection via InfrastructureConfiguration |

## Documentation

| Commit | Purpose |
|--------|---------|
| `3f6bc0459b` | docs(tls): generalize page title from TCP TLS to TLS |
| `288ef57223` | docs(tls): split operator-level TLS config into a Kubernetes Operator page |
| `c305e21d82` | docs(request-plane): link to the TLS reference from TCP config options |

## Request-Plane mTLS Hardening

| Commit | Purpose |
|--------|---------|
| `86b71209a9` | fix(runtime): return error instead of panicking on invalid request-plane TLS config |
| `511c81e6dc` | fix(runtime): enforce request-plane mTLS |
| `b953cacafa` | fix(runtime): complete request-plane mTLS |
| `df6c07ce4a` | test(runtime): cover request-plane mTLS configuration |
| `efe4de1bbc` | fix(runtime): flush request-plane TLS writes |
| `60e43eefb2` | test(runtime): soften request-plane mTLS client reject assert |

## TLS Identity Reload (Certificate Rotation)

| Commit | Purpose |
|--------|---------|
| `911d20902c` | fix(runtime): reload rotated TLS identities |
| `2a393ed6c7` | fix(runtime): retry TLS identity reload after transient failures |
| `ec9b63d058` | Potential fix for pull request finding |
| `b64f7ba837` | fix(runtime): reload TLS identities from content |

## Notes

- 40 commits cherry-picked from `v1.4.0` branch onto `upstream/release/1.4.1`.
- Zero conflicts during cherry-pick (patch release — minimal upstream changes to
  touched files).
- v1.4.1 upstream changes (11 commits) include: router overload reconciliation fix,
  NIXL writable buffer fix, logprob_token_ids forwarding, classify/pooling stack,
  Go module alignment, and release plumbing. None overlap with our TLS patches.
- All TLS/mTLS/NATS tests pass: 10 request_plane tests, 8 connector/acceptor tests,
  10 NATS TLS tests — 28 total, 0 failures.
