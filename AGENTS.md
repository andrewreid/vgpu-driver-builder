# AGENTS.md — vGPU Driver Operator for Flatcar Linux

This document is the entry point for any agent (Claude, Codex, etc.) picking up
work on this repository. It describes architecture, file layout, design
decisions, conventions, and how to run tests / live verification. Outstanding
bugs and work items live in [TODO.md](TODO.md). Open design plans live under
`~/.claude/plans/` (referenced where relevant).

If you only need to read one section before fixing a TODO: read **§1 Quick
orientation** + **§3 Code map**. Everything else is reference material.

---

## 1. Quick orientation

**What this is**: a Kubernetes operator that builds NVIDIA vGPU driver
container images for Flatcar Linux nodes. It watches `VGPUDriverImage` custom
resources, spawns BuildKit Jobs to compile the driver against each Flatcar
kernel, and pushes images to an OCI registry where the NVIDIA GPU Operator
consumes them.

**What this is not**: a runtime driver loader. The NVIDIA GPU Operator handles
that — this operator only produces images.

**Key facts**:

- Operator language: Python + [kopf](https://github.com/nolar/kopf).
- Install: a single Helm chart at `charts/vgpu-driver-operator/`. CRD ships in
  `charts/vgpu-driver-operator/crds/` and is `CreateReplace`-managed.
- Build runtime: BuildKit (privileged container; rootless was removed in Bug 10).
- Driver image consumers: NVIDIA GPU Operator with `driver.usePrecompiled=true`
  (precompile mode) or runtime mode.
- Tag scheme (matches GPU Operator's auto-construction):
  - Runtime: `<driver>-flatcar<flatcar_version>`
  - Precompile: `<driver>-<kernel>-flatcar<flatcar_version>` (kernel is
    auto-discovered inside the build job — see §6).
- CI: `.github/workflows/build-operator-image.yml` builds + pushes both the
  operator image and the Helm chart on every push to `main`.
  - Operator image: `ghcr.io/<owner>/vgpu-driver-operator:vYYYY.MM.<run>` plus `:latest`.
  - Chart (OCI): `oci://ghcr.io/<owner>/charts/vgpu-driver-operator` at
    version `YYYY.M.<run>` (chart version is calver minus the `v` prefix and
    leading zero on month, to satisfy semver).
  - Local-path install (`helm install ./charts/vgpu-driver-operator`) is also
    supported and is what the test-cluster runs use.

---

## 2. Repository layout

```
operator/                           Python package + operator Dockerfile
  src/vgpu_driver_operator/         Source (see §3)
  tests/                            ~200 unit tests (pytest)
  pyproject.toml                    kopf, kubernetes, requests, pytest, ruff

charts/
  vgpu-driver-operator/
    Chart.yaml
    values.yaml                     Helm defaults; documented inline
    crds/                           CRD YAML (CreateReplace)
    files/build/                    Build assets (Dockerfile + Dockerfile.prebuilt + nvidia-driver script)
    files/build/tests/              bats tests for shell helpers
    templates/                      Deployment, RBAC, ConfigMap, CronJob, secret-* (gated)

docs/
  architecture.md                   Design rationale + reconciliation diagram
  installation.md                   Helm install + secrets + values reference
  gpu-operator-integration.md       How to wire NVIDIA GPU Operator
  examples/flux/                    Flux v2 deployment exemplars (HelmRelease + CR + Secrets)

.github/workflows/                  Single workflow: operator-image build + release
AGENTS.md                           This file
TODO.md                             Outstanding bugs and work
README.md                           User-facing entry point
```

---

## 3. Code map (operator/src/vgpu_driver_operator/)

| File | Responsibility |
|---|---|
| `cli.py` | Argument parser. Subcommands: `controller` (kopf entrypoint) and `poll-flatcar` (CronJob entrypoint). Starts the kopf liveness HTTP server. |
| `main.py` | All kopf event handlers. Watches Nodes, VGPUDriverImage, Jobs. Computes `tracked_flatcars` set, calls `compute_desired()`, dispatches Jobs, patches CR status. **No kernel resolution** — that lives in the build job (Bug 13 fix). |
| `reconciler.py` | Pure logic. `BuildKey(driver, flatcar, precompile: bool)`, `compute_desired()`, `compute_missing()`, `compute_prunable()`, `runtime_tag()`, `parse_precompile_tag()` / `extract_kernel_from_precompile_tag()`. No K8s import. |
| `job_factory.py` | `build_job_name()` and `build_job_manifest()`. Two-phase buildctl orchestration for precompile (phase 1: kernel-discover stage exports `/kernel_version` via `--output type=local`; phase 2: full build with `KERNEL_VERSION` shell-expanded into the output tag). |
| `flatcar.py` | Parse Flatcar version from NFD label `feature.node.kubernetes.io/system-os_release.VERSION_ID` (preferred) or `node.status.nodeInfo.osImage` (fallback). Reject non-flatcar nodes (Bug 12 fix). Fetch `latest_release(channel)` from Flatcar release feed. **No kernel-version resolution** (Bug 13 — operator no longer cares). |
| `poller.py` | `poll-flatcar` subcommand body. Iterates `spec.flatcar.trackChannels`, fetches latest version per channel, patches `status.trackedChannelVersions` (no kernel field). |
| `registry.py` | OCI v2 client: `list_tags()` (paginated, `Link: rel="next"`), `tag_created_at()` (manifest → config-blob → `created`), `delete_tag()` (HEAD → DELETE; `TagDeletionDisabled` on 405), bearer-challenge dance, `find_matching_tags()` for precompile idempotency pattern match. `parse_dockerconfigjson()` helper. |
| `gc.py` | Garbage collection orchestration. `parse_duration()` (h/d/m/s), `run()` returns a status patch dict. Gated on `spec.retention.enabled`. |
| `crd.py` | Status patch helpers, `operator_namespace` resolution (env → file → default), `make_owner_reference()`, `get_secret()` (base64-decoded), `list_owned_jobs()`. |

**Modules with NO kubernetes import**: `flatcar.py`, `reconciler.py`,
`job_factory.py`, `gc.py` (parsing only). Keep it that way — these are the
pure-logic seams that make unit testing tractable.

**Build-asset wiring**: chart renders `charts/vgpu-driver-operator/files/build/`
into a single ConfigMap (`driver-build-files` by default). The Deployment
template injects the ConfigMap name as env vars `DOCKERFILE_CONFIGMAP` and
`BUILDFILES_CONFIGMAP`; the operator reads those (with `driver-build-files` as
the default fallback) — see `main.py:431-432`.

---

## 4. CRD spec (`vgpu.flatcar.io/v1alpha1` — `VGPUDriverImage`)

```yaml
spec:
  driverVersions: ["535.261.03", ...]    # NVIDIA driver versions to build
  source:
    type: s3 | http
    uriTemplate: "s3://.../NVIDIA-Linux-x86_64-${DRIVER_VERSION}-vgpu-kvm.run"
    # Placeholder: ONLY ${DRIVER_VERSION} is currently honored — see TODO Bug 9
    credentialsSecretRef: { name: s3-driver-storage-secret }
    # Secret keys: S3_ENDPOINT_URL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
  registry:
    repository: "registry.example.com/vgpu-driver"
    repositoryPrecompiled: "registry.example.com/vgpu-driver-precompiled"   # optional, defaults to repository
    cacheRepository: "registry.example.com/vgpu-driver-cache"               # optional
    authSecretRef: { name: private-registry-secret }                        # optional; mount is conditional
  flatcar:
    discoverFromNodes: true        # NFD-driven (default true)
    nodeSelector: { nvidia.com/gpu.present: "true" }   # optional
    trackChannels: ["stable"]      # operator polls release feed
    versions: ["4593.2.0"]         # explicit pinning (additive)
    arch: amd64                    # default amd64
  precompile: false                # if true, also build precompile variants
  retention:
    enabled: false
    keepPreviousFlatcarVersions: 2
    minAgeBeforeDelete: "168h"
  build:
    buildkitImage: "moby/buildkit:rootless"
    resources: {limits: {...}, requests: {...}}
    nodeSelector: {}
    tolerations: []

status:
  observedNodes:                  # populated when discoverFromNodes=true; flatcarVersion only (no kernel)
    - { flatcarVersion, nodeCount }
  trackedChannelVersions:
    - { channel, flatcarVersion, observedAt }
  retainedFlatcarVersions: ["..."]
  pruned: [{tag, reason, prunedAt}]
  builds:
    - { driverVersion, flatcarVersion, mode, tag, phase, jobName, lastTransitionTime, message }
  conditions:
    - { type: Reconciled, status, reason, lastTransitionTime, message }
```

**Gotchas**:

- Status `tag` for precompile builds shows the placeholder
  `<driver>-<kernel>-flatcar<flatcar>` literal until/unless the operator reads
  back the real kernel post-build (TODO).
- `kernelVersion` field is intentionally absent from all status entries (Bug 13).
- `discoverFromNodes` defaults to `true`; non-flatcar nodes are silently
  ignored after Bug 12 (previously they coined fake `flatcarVersion="12"` from
  Debian's `VERSION_ID` label).

---

## 5. Reconciliation (main.py)

```
on Node added/updated/deleted | on VGPUDriverImage changed | on Job event |
every 10 min | on Flatcar release-feed CronJob:

  tracked_flatcars = set()
  if discoverFromNodes: tracked_flatcars |= {flatcar from each flatcar-labelled Node}
  tracked_flatcars |= status.trackedChannelVersions's flatcar values
  tracked_flatcars |= spec.flatcar.versions

  desired = compute_desired(tracked_flatcars, spec.driverVersions, spec.precompile)
  existing_runtime    = registry.list_tags(spec.registry.repository)
  existing_precompile = registry.find_matching_tags(spec.registry.repositoryPrecompiled or .repository,
                                                   pattern=f"{driver}-*-flatcar{flatcar}")

  missing = compute_missing(desired, existing)
  for key in missing: spawn build Job
  for job in owned jobs: patch status.builds[key] from job phase

  if spec.retention.enabled: gc.run()
```

**Idempotency**:

- Runtime: exact tag string match against registry tag list.
- Precompile: pattern match `<driver>-*-flatcar<flatcar>` (kernel unknown until
  build runs; see §6).

**Job ownership**: all build Jobs carry an
`vgpu.flatcar.io/owner-uid: <CR uid>` label and an `OwnerReference` for cascade
delete. Component label is `app.kubernetes.io/component=builder`.

---

## 6. Two-phase build (precompile)

For precompile builds the operator does not know the kernel version when it
dispatches the Job. The shell command in the Job runs buildctl twice:

1. **Phase 1**: `--opt target=kernel-discover-export --output type=local,dest=/tmp/kinfo`
   builds only the `kernel-discover-export` stage in `Dockerfile.prebuilt` and
   exports `/kernel_version` to a tmp dir.
2. **Phase 2**: reads the kernel string, then runs the full build with
   `--opt build-arg:KERNEL_VERSION=$KERNEL_VERSION` and an output ref of
   `<repo>:<driver>-${KERNEL_VERSION}-flatcar<flatcar>` (shell expands at
   buildctl invocation time).

The kernel-discover stage validates that `/lib/modules/` in the
`flatcar-developer:<version>-sources` base image contains exactly one entry
(POSIX glob, no SIGPIPE risk under pipefail). Multiple or zero entries fail loudly.

**Why this design**: the NVIDIA GPU Operator requires kernel-in-tag for the
precompile path (we cannot drop the kernel segment from the published tag), but
kernel discovery via the operator's release-feed lookup was unreliable for
Flatcar 4593.x and later (the feed dropped `FLATCAR_KERNEL_VERSION` —
historical Bug 13). Auto-discovering inside the base image is the source of
truth.

---

## 7. Build assets (`charts/vgpu-driver-operator/files/build/`)

- `Dockerfile` — runtime-compile image. NVIDIA installer extracts userspace,
  modules compile at node init time using `make modules_prepare` against the
  installed Flatcar kernel headers.
- `Dockerfile.prebuilt` — multi-stage:
  1. `kernel-discover` (FROM flatcar-sources): writes `/kernel_version`.
  2. `kernel-discover-export` (FROM scratch): COPY the file, exposed via
     `--output type=local`.
  3. `builder` (FROM flatcar-sources): receives `KERNEL_VERSION` as build-arg,
     extracts userspace, compiles modules.
  4. `runtime` (FROM ubuntu:22.04): minimal final image; copies the extracted
     installer tree from builder + precompiled module dir; runs nvidia-installer
     to lay down userspace; entrypoint is `nvidia-driver`.
- `nvidia-driver` — bash entrypoint script. Modes: `init` (load modules on
  node) and `build-precompiled` (produce module artifacts during image build).

A planned alignment with NVIDIA upstream's
[gpu-driver-container](https://github.com/NVIDIA/gpu-driver-container) is
documented at:

`~/.claude/plans/considering-specifically-charts-vgpu-dr-nested-rabin.md`

This plan removes ~250 lines of bespoke `mkprecompiled` / `ld -r` scaffolding
in favour of a single nvidia-installer invocation, adds `KERNEL_MODULE_TYPE`
support (open vs proprietary; required for Hopper+/Blackwell), refcnt-aware
unload, flock on PID file, and modprobe-param ConfigMaps. Three sequenced PRs.
Read the plan before touching the build assets in any non-trivial way.

---

## 8. Tag scheme (locked by NVIDIA GPU Operator)

GPU Operator constructs image tags itself from `spec.image` + node labels. We
must match its format exactly:

```
runtime:    <driver>-flatcar<VERSION_ID>
            e.g.  535.261.03-flatcar4593.2.0
precompile: <driver>-<kernel>-flatcar<VERSION_ID>
            e.g.  535.261.03-6.12.81-flatcar-flatcar4593.2.0
```

Note the literal `flatcar-flatcar` substring in the precompile tag is correct:
the kernel string is `6.12.81-flatcar` and the osTag is `flatcar4593.2.0`.

Sources: NVIDIA `gpu-operator/api/nvidia/v1alpha1/nvidiadriver_types.go`
(`GetImagePath`, `GetPrecompiledImagePath`) and
`internal/state/nodepool.go` (`getOSTag`).

The only escape hatch is image digests (`spec.image: sha256:...`) — GPU
Operator skips tag construction entirely. We do not currently use that path.

---

## 9. Test infrastructure

```bash
# Run unit tests
cd operator && PYTHONPATH=src pytest

# Update snapshot tests after job spec / securityContext / env changes
cd operator && UPDATE_SNAPSHOTS=1 PYTHONPATH=src pytest tests/test_snapshots.py

# Lint chart
helm lint charts/vgpu-driver-operator

# Bats tests for the nvidia-driver script
cd charts/vgpu-driver-operator/files/build && bats tests/
```

Coverage: ~200 unit tests across reconciler, registry, gc, poller, crd,
flatcar, job_factory, snapshots. Live-cluster verification is manual — see §10.

**When changing the BuildKey shape, the precompile tag format, or the build
job command**: snapshot tests will fail. Re-record with `UPDATE_SNAPSHOTS=1`
and review the diff carefully.

---

## 10. Live-cluster verification

Two test environments exist:

- **Author's Debian k3s cluster** — used for end-to-end smoke. NFD installed,
  no GPUs, no Flatcar nodes. Used `flatcar.discoverFromNodes: false` and
  explicit `versions: ["4593.2.0"]` to drive builds. Suitable for everything
  except true on-node module load.
- **Flatcar RKE2 cluster** — exemplar manifests under `docs/examples/flux/`
  for sysadmin handoff. NFD-driven discovery works there. Used for full
  precompile + GPU Operator integration testing.

Common operational commands:

```bash
# Trigger a CI rebuild for a non-main branch
gh workflow run build-operator-image.yml --ref <branch>

# Watch a workflow run
gh run watch <RUN_ID> --exit-status

# Tail operator logs
kubectl -n vgpu-driver-operator logs deploy/vgpu-driver-operator -f

# Inspect a build Job and its pod
JOB=$(kubectl -n vgpu-driver-operator get jobs -l app.kubernetes.io/component=builder -o name | head -1)
kubectl -n vgpu-driver-operator describe "$JOB"
kubectl -n vgpu-driver-operator logs "$JOB" --all-containers --tail=200

# List registry tags (anonymous registry example)
curl -s https://registry.k3s.wp.reid.ee/v2/vgpu-driver/tags/list | jq .

# Force operator to pick up a new :latest image
kubectl -n vgpu-driver-operator delete pod -l app.kubernetes.io/name=vgpu-driver-operator,app.kubernetes.io/component!=flatcar-poll
```

If `helm upgrade` errors with field-manager conflicts on `imagePullPolicy`,
either pass `--server-side=false` (3-way merge) or strip stale `managedFields`
from the Deployment manually. This is leftover noise from earlier
`kubectl set image` workarounds during Bug 5 triage.

---

## 11. Conventions

- **Python**: ruff for linting, type hints encouraged, pure-logic modules must
  not import `kubernetes.*`.
- **Shell**: `set -eu` minimum; `set -euo pipefail` where pipelines are used
  (with care for SIGPIPE under pipefail). bats tests for any new helper.
- **Docs**: no emojis unless the user explicitly asks. Avoid creating new docs
  unless explicitly requested — prefer extending existing files in `docs/`
  (and this AGENTS.md for design context).
- **Comments**: explain *why*, not *what*. Skip restating obvious code.
- **Commits**: follow the existing terse style (see `git log --oneline -20`).
  Body explains motivation when non-obvious. Co-Authored-By trailer is fine.
- **Live-cluster destructive ops**: always confirm with the user before
  `kubectl delete`, `helm uninstall`, registry tag deletion, etc.

---

## 12. Picking up a TODO

When asked to fix something in TODO.md:

1. Read the TODO entry. It includes file paths and a one-line summary.
2. Read the affected files only — do not re-explore the whole repo.
3. If the TODO is in §3 of TODO.md ("Open design plans"), the canonical plan
   lives at `~/.claude/plans/<name>.md`. Read that first.
4. Write tests when changing behavior. Run `pytest` + `helm lint` before
   committing.
5. For changes that affect the build job, BuildKey, or tag scheme: update
   snapshot tests and re-run live verification on a test cluster (see §10).
6. Commit with a terse, conventional message. Do not push without being asked.

---

## 13. History reference

Eight live-cluster test runs preceded the merged-to-main state and produced
the bug list visible in TODO.md. Detailed run-by-run history (root causes,
attempted fixes, diagnostics) lives at:

`~/.claude/plans/cavecrew-repo-successfully-pushed-warm-goose.md`

That file is for archaeology only; do not modify it.
