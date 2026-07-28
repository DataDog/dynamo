# DataDog/dynamo fork

This is Datadog's fork of [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo),
carrying a TLS/mTLS patch (opt-in TLS for the NATS and TCP transports) that
upstream does not have. It exists because the mirrored NVIDIA images
(`registry.ddbuild.io/images/mirror/nvidia/ai-dynamo/*`) are built from stock
`dynamo_runtime`, which hard-rejects any NATS URL that isn't `nats://` — this
org's NATS is `tls://...`.

## Branching model

- **`main`** is a pure, unmodified mirror of `ai-dynamo/dynamo`'s `main`,
  kept in fast-forward sync. It must **never** carry unique commits of our
  own — that's what keeps future syncs a trivial fast-forward instead of a
  merge/rebase headache. Do not open PRs against `main`.
- **`vX.Y.Z`** (currently `v1.3.0`) is the active version branch: upstream's
  `vX.Y.Z` release plus our TLS patch on top. This is where all of our
  value-add lives, including this file. All hotfixes land here.
- Short-lived `*/tls-...` branches (e.g. `walid/tls-v1.3.0-dev.1`,
  `dkliu/tls-v1.3.0-official`) are working branches used to build/validate a
  patch before it's merged into the active `vX.Y.Z` branch.

When a new upstream version is cut, the TLS patch (and this file) must be
replayed onto the new `vX.Y.Z` branch — see the `upgrade-dynamo-release`
skill in `dd-source` (`domains/ai_platform/apps/dynamo-models/.claude/skills/upgrade-dynamo-release/`)
for the full release-cut procedure.

## Applying a hotfix to the current version

1. Branch off the active `vX.Y.Z` branch (e.g. `v1.3.0`), make the patch,
   push the branch, open a PR back into `vX.Y.Z`.
2. In `DataDog/images`, add/update the relevant Dockerfile to build from
   the patch branch at its exact commit SHA (pin the SHA, per that repo's
   supply-chain rules) and open a PR — don't merge yet.
3. Trigger the `DataDog/images` GitLab pipeline for that image manually to
   build and sign it from the patch branch, and validate the resulting
   image (e.g. deploy to staging).
4. Once validated: merge the `DataDog/images` fork PR with a **merge
   commit** (not squash) so the exact tested SHA stays reachable in
   history, then tag that merge commit `vX.Y.Z-pN` (e.g. `v1.3.0-p1`).
5. Swap the `DataDog/images` Dockerfile's `--branch` reference from the
   patch branch to `vX.Y.Z` (the SHA is already pinned and unchanged, so
   this needs no rebuild) and merge that PR. The corresponding image tag
   is `$VERSION-tls-pN` (e.g. `1.3.0-tls-p1`) — the `pN` suffix is appended
   to whichever tag/branch identifier already exists for that artifact:
   `vX.Y.Z-pN` for the git tag on this fork, `$VERSION-tls-pN` for the
   image tag in `DataDog/images`.
6. Open PRs against `dd-source` and `k8s-resources` bumping the image
   reference to `$VERSION-tls-pN` to actually roll the fix out.
7. Optionally, open an async PR upstream to `ai-dynamo/dynamo`'s `main` —
   this is not required to ship the hotfix internally and is not on the
   critical path.
