# Architecture

The vGPU Driver Operator is a Kubernetes operator that reconciles `VGPUDriverImage` custom resources to automatically build and push NVIDIA vGPU driver container images compatible with Flatcar Linux.

## Why an operator?

A declarative operator model replaces ad-hoc build scripts and manual image management:

- **Desired-state reconciliation**: Define once, reconcile continuously. The operator re-runs builds if a cluster gains new Flatcar versions or driver versions are added to the spec.
- **Automatic Flatcar discovery**: Watch cluster nodes to discover in-use Flatcar versions. No manual version lists in configuration.
- **In-cluster builds**: BuildKit jobs run unprivileged and rootless, with no host or Docker daemon access. Images are built where they will run.
- **Garbage collection**: Automatically prune old images from the registry based on retention policy, freeing storage.
- **Debugging via Kubernetes**: Build logs live in Pod events and container logs; monitor via `kubectl logs`.

## Why Python + kopf?

**Python**: vGPU driver builds are I/O-heavy (fetching drivers from S3, pushing to registry, polling Flatcar release feeds). Python's asyncio and rich third-party libraries (boto3, kubernetes) make async orchestration straightforward. The codebase is readable and easy to extend (e.g., adding a new source protocol or registry type).

**kopf**: The Kubernetes Operator Framework handles boilerplate resource watching, queuing, retries, and condition management. It lets us focus on reconciliation logic without writing Kubernetes event loops.

## Components

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ Kubernetes Cluster                                                       │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │ vgpu-driver-operator Namespace                                  │   │
│  │                                                                  │   │
│  │  ┌──────────────────────────────────────────────────────────┐  │   │
│  │  │ vgpu-driver-operator Deployment (kopf handler)            │  │   │
│  │  │  • Watches VGPUDriverImage CRDs                          │  │   │
│  │  │  • Reconciles desired vs. actual builds                  │  │   │
│  │  │  • Launches BuildKit Job manifests                       │  │   │
│  │  │  • Polls Flatcar release channels (async)                │  │   │
│  │  │  • Runs garbage collection on intervals                  │  │   │
│  │  └──────────────────────────────────────────────────────────┘  │   │
│  │                           │                                      │   │
│  │                           ├─→ Secrets (S3, registry auth)       │   │
│  │                           ├─→ ConfigMaps (Dockerfile, scripts)  │   │
│  │                           └─→ Jobs (BuildKit runners)           │   │
│  │                                                                  │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │ Node 1                  Node 2                    Node N         │   │
│  │  Flatcar 4230.2.3       Flatcar 4230.3.0         Flatcar 4231   │   │
│  │  Kernel 6.1.65          Kernel 6.1.65            Kernel 6.2.0   │   │
│  │  [GPU hardware]         [GPU hardware]           [GPU hardware] │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  └─→ External systems:
                                      • S3 (driver .run files)
                                      • Private registry (push images)
                                      • Flatcar release feed (poll versions)
```

### Core modules

**`main.py`** — Kopf entry point and reconciliation loop.
- Watches `VGPUDriverImage` resources cluster-wide.
- On change or periodic timer, calls the reconciler.
- Updates CRD status with build results, condition messages, and GC records.

**`reconciler.py`** — Pure reconciliation logic (no Kubernetes client).
- `compute_desired()`: Given driver versions, node Flatcar versions, and tracked channel versions, compute the set of `BuildKey` tuples (driver, flatcar, [kernel]) that should exist.
- `compute_missing()`: Compare desired vs. existing registry tags to find which builds need to run.
- `compute_retained_flatcar_set()`: For GC, determine which Flatcar versions should be kept (current nodes, tracked channels, plus N previous historical versions).
- `compute_prunable()`: Identify registry tags eligible for deletion (old Flatcar versions, past min age threshold).
- Tag parsing: `parse_runtime_tag()`, `parse_precompile_tag()` — Extract driver, flatcar, kernel from tag strings.

**`job_factory.py`** — Generate Kubernetes Job manifests (no K8s client).
- `build_job_manifest()`: Create a `batch/v1.Job` that mounts Dockerfiles and scripts from ConfigMaps, fetches the driver binary from S3, and runs BuildKit to build and push the image.
- `build_job_name()`: Generate a deterministic, K8s-safe Job name short enough to fit the 63-char limit.

**`flatcar.py`** — Extract Flatcar version and kernel version from node metadata.
- Parse `osImage` label (e.g., `"Flatcar Container Linux by CoreOS 4230.2.3"`).
- Fall back to kernel version from `nodeInfo.kernelVersion` for precompile builds (kernel-specific).
- Without Node Feature Discovery, falls back to parsing node labels.

**`poller.py`** — Async polling of Flatcar release feeds (JSON).
- Fetch latest release for each tracked channel (stable, lts, beta, alpha).
- Cache and age the results to avoid hammering the feed.

**`registry.py`** — OCI registry client (list, push, delete tags; inspect image metadata).
- Supports auth via docker-config Secret.
- List tags in a repository to find existing images.
- Inspect image manifests to extract push timestamps for GC age checks.

**`gc.py`** — Garbage collection logic.
- Given a set of prunable tags, delete them from the registry.
- Log each deletion with reason and timestamp.

**`crd.py`** — CRD type definitions (Pydantic models) for spec and status.

## Reconciliation flow

```text
┌─ VGPUDriverImage change event (kopf) ──┐
│                                         │
├─ Lookup S3 secret (credentials)        │
├─ Lookup registry secret (docker-config)│
├─ Lookup ConfigMaps (Dockerfiles)       │
│                                         │
├─ Inspect cluster nodes for Flatcar versions
│  └─ Call flatcar.parse_flatcar_version() per node
│                                         │
├─ Poll Flatcar release channels         │
│  └─ If trackChannels is set, fetch latest from JSON feeds
│                                         │
├─ List existing images in registry      │
│                                         │
├─ RECONCILE DESIRED STATE:              │
│  ├─ compute_desired() → desired BuildKeys
│  ├─ compute_missing() → BuildKeys needing a Job
│  └─ Launch Job for each missing BuildKey
│                                         │
├─ GARBAGE COLLECTION (if enabled):      │
│  ├─ compute_retained_flatcar_set()     │
│  ├─ compute_prunable() → tags to delete│
│  └─ Call gc.delete_tags()              │
│                                         │
└─ Update CRD status.conditions          │
   └─ .status.builds[] = build results   │
   └─ .status.conditions[]=ready/error   │
```

## Naming and tag scheme

### Runtime builds (default mode)

Build once per driver + Flatcar version combination. Kernel modules compile on each node at deploy time.

- **Build Job name**: `vgpu-build-runtime-{driver_s}-fc-{flatcar_s}-{hash6}`
  - `{driver_s}`: Driver version with `.` and `_` replaced by `-` (e.g., `535-104-05`)
  - `{flatcar_s}`: Flatcar version sanitized (e.g., `4230-2-3`)
  - `{hash6}`: First 6 chars of SHA256(spec content) for idempotency
  
- **Image tag**: `<registry>/<repository>:<driver>-flatcar<flatcar>`
  - Example: `harbor.example.com/vgpu-driver:535.104.05-flatcar4230.2.3`

### Precompiled builds (optional)

Also compile kernel modules during the build, baking them into the image.

- **Build Job name**: `vgpu-build-prec-{driver_s}-fc-{flatcar_s}-k{kernel6}-{hash6}`
  - `{kernel6}`: First 6 chars of SHA256(kernel version)
  
- **Image tag**: `<registry>/<repository>:<driver>-<kernel>-flatcar<flatcar>`
  - Example: `harbor.example.com/vgpu-driver:535.104.05-6.1.65-flatcar4230.2.3`

### Garbage collection

When `retention.enabled: true`:

1. **Identify retained Flatcar versions**:
   - All versions currently running on cluster nodes
   - All versions from tracked channel feeds
   - (Optional) N previous historical versions per the `keepPreviousFlatcarVersions` policy

2. **Find prunable tags** (all must be true):
   - Image's Flatcar version is NOT in retained set
   - Image's push timestamp is known
   - Image age ≥ `minAgeBeforeDelete` (default 168h = 7 days)

3. **Delete** and record in `.status.pruned[]`

## Why separate runtime and precompiled modes?

| Mode | Build time | Deploy time | Use case |
|------|-----------|-------------|----------|
| **Runtime** | ~5 min (fetch + user-space only) | ~10 min per node (compile kernel mods) | Flexible; tolerates kernel changes |
| **Precompiled** | ~20 min per version (compile all kernel mods upfront) | Immediate (modules pre-baked) | Zero-downtime deployments; consistent build artifacts |

A single `VGPUDriverImage` can run both: runtime builds are always created; precompiled is opt-in with `precompile: true`. GPU Operator can switch between them by changing `driver.usePrecompiled` and `driver.repository` / `driver.repositoryPrecompiled`.

## Registry authentication

Credentials are mounted from Kubernetes Secrets:

- **S3 credentials**: Secret in operator namespace with keys `accessKeyId` and `secretAccessKey`
  - Referenced via `spec.source.credentialsSecretRef.name`
  - Injected into BuildKit Job env vars

- **Registry credentials**: Secret with key `.dockerconfigjson` (standard `docker-config` format)
  - Referenced via `spec.registry.authSecretRef.name`
  - Injected into BuildKit Job as `/buildkit/run/secrets/docker` mount

Both are optional; if omitted, the operator assumes:
- S3 is public or implicit (e.g., EC2 instance role).
- Registry is public or uses node-level auth (e.g., K8s imagePullSecrets).

## Observability

- **kopf logs**: Container logs show reconciliation events, errors, retries.
- **CRD status**: `.status.builds[]` lists each (driver, flatcar, kernel?, status) combination.
- **Condition array**: `.status.conditions[]` tracks `Reconciled`, `BuildsComplete`, `GCComplete` conditions.
- **Job logs**: `kubectl logs -l <selector>` shows BuildKit output.
- **Flatcar poller**: `.status.trackedChannelVersions[]` shows latest release per channel.

## Security model

- **RBAC**: Operator Pod runs as non-root (`runAsUser: 65532`) with restricted security context.
- **Network**: BuildKit Jobs do not inherit host network; pull base images and push to registry over standard OCI/HTTP.
- **Storage**: Jobs run rootless BuildKit; no host mount or privileged containers.
- **Secrets**: S3 and registry credentials are mounted as K8s Secrets (not embedded in Job specs or logs).
- **Image verification**: Optional: Jobs can verify image digest after push (not yet implemented).
