# Flux deployment example — vGPU Driver Operator on Flatcar RKE2

This directory contains example Flux v2 manifests for deploying the operator on
a Flatcar Linux + RKE2 cluster. Adapt to your environment before applying.

## What you get

| File | Purpose |
|---|---|
| `namespace.yaml` | Creates the `vgpu-driver-operator` namespace |
| `ocirepository.yaml` | Flux `OCIRepository` pulling the chart from `ghcr.io/andrewreid/charts/vgpu-driver-operator` |
| `helmrelease.yaml` | Flux `HelmRelease` that installs the chart with sensible defaults |
| `values-overrides.yaml` | Example value overrides documented inline (paste into the HelmRelease `values:` block, or reference via `valuesFrom`) |
| `secrets.example.yaml` | Placeholder for the two Secrets the operator consumes — **do not commit real credentials**; encrypt with SOPS / Sealed Secrets / External Secrets |
| `vgpudriverimage.yaml` | Example `VGPUDriverImage` custom resource that triggers a build |

## Chart source

The chart is published as an OCI Helm artifact on every push to `main`:

```
oci://ghcr.io/andrewreid/charts/vgpu-driver-operator
```

Versions follow calver: `YYYY.M.<run>` (no leading zero on month, no `v` prefix,
to satisfy Helm semver constraints). Examples: `2026.5.1`, `2026.12.3`.

`ocirepository.yaml` pins `ref.tag` to a specific version. To instead track the
latest stable release, replace the `ref` block with:

```yaml
ref:
  semver: ">=2026.0.0"
```

### Using a local chart path instead (dev mode)

If you are iterating locally or testing unreleased changes, you can swap the
`OCIRepository` for a `GitRepository` and point the `HelmRelease` at the chart
path in the repo:

```yaml
# gitrepository.yaml (local dev only)
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: vgpu-driver-operator
  namespace: flux-system
spec:
  interval: 1m
  url: https://github.com/andrewreid/vgpu-driver-builder
  ref:
    branch: main
  ignore: |
    /*
    !/charts/vgpu-driver-operator/**
```

Then in `helmrelease.yaml` change `chart.spec` to:

```yaml
chart:
  spec:
    chart: ./charts/vgpu-driver-operator
    sourceRef:
      kind: GitRepository
      name: vgpu-driver-operator
      namespace: flux-system
```

## Prerequisites

1. **Flux v2** (v0.32.0+, source-controller v0.26.0+) installed and reconciling
   against the cluster. `OCIRepository` requires these minimum versions.
2. **Node Feature Discovery (NFD)** running. The operator reads
   `feature.node.kubernetes.io/system-os_release.{ID,VERSION_ID}` labels to
   discover Flatcar versions on nodes. NFD ships with the NVIDIA GPU Operator,
   or can be installed standalone.
3. **NVIDIA GPU Operator** installed separately (or planned to be). This
   operator only builds and publishes driver images; the GPU Operator consumes
   them. Configure GPU Operator with `driver.repository=<your registry>/vgpu-driver`
   and `driver.version=<driverVersion>` so its tag construction matches what
   this operator publishes (`<driver>-flatcar<flatcar>` or
   `<driver>-<kernel>-flatcar<flatcar>` for precompile).
4. **OCI registry** reachable from the cluster, with anonymous push or
   credentials supplied via Secret. The example uses a private mirror; replace
   with your registry.
5. **S3-compatible object store** holding the NVIDIA `.run` driver installer
   blobs. The operator fetches these per build.

## Workarounds still required

See `TODO.md` for the canonical list of outstanding bugs. The two that affect
this deployment manifest set:

- **Bug 6/8**: the operator falls back to `private-registry-secret` as the
  default registry-auth Secret name even when `spec.registry.authSecretRef` is
  absent. The build-pod mount is conditional on the *name* being None, but
  the caller passes the default unconditionally — so the Secret must exist.
  `secrets.example.yaml` ships a stub with `{"auths":{}}` to satisfy the mount.
- **Bug 9**: the CR `spec.source.uriTemplate` placeholder syntax must be
  `${DRIVER_VERSION}`, not `{driverVersion}`. Other placeholders
  (`{flatcarVersion}`, `{arch}`) are honored as-is.

## Deployment order

```
1. Apply namespace.yaml
2. Apply secrets.example.yaml (replace placeholders with real creds via SOPS/SealedSecrets/ExternalSecrets)
3. Apply ocirepository.yaml + helmrelease.yaml — Flux installs operator + CRD
4. Apply vgpudriverimage.yaml — operator picks it up and dispatches a build Job
```

## Verifying

```bash
kubectl -n flux-system get ocirepository vgpu-driver-operator
kubectl -n vgpu-driver-operator get pods,vgpudriverimage,jobs
kubectl -n vgpu-driver-operator logs deploy/vgpu-driver-operator
kubectl -n vgpu-driver-operator get vgpudriverimage <name> -o yaml | yq .status
```

A successful build appears as a tag in your registry under the configured
`spec.registry.repository`.
