# GPU Operator Integration

The NVIDIA GPU Operator consumes driver container images built by the vGPU Driver Operator. This guide explains how to wire them together.

## Overview

When the vGPU Driver Operator finishes building driver images, the GPU Operator's driver DaemonSet pods pull and run them on cluster nodes. The GPU Operator handles kernel module loading, vGPU device discovery, and persistent GPU management.

**Key point**: The two operators are decoupled. The vGPU Driver Operator builds images; the GPU Operator runs them. They communicate through the Kubernetes registry (image pull) and optional shared configuration (CRD status or ConfigMaps).

## GPU Operator version support

This guide assumes **NVIDIA GPU Operator v23.6+**. Earlier versions have different driver configuration options. Check your GPU Operator version:

```bash
helm list -n gpu-operator
```

## Flatcar version suffix behavior

On Flatcar nodes, the GPU Operator auto-appends a version ID suffix to the driver tag:

**When NFD (Node Feature Discovery) detects Flatcar:**

```text
GPU Operator config:   driver.version = "535.104.05"
Actual image tag used: "535.104.05-flatcar4230.2.3"
```

The GPU Operator detects your node's Flatcar version ID (e.g., `4230.2.3`) and appends `-flatcar<VERSION_ID>` automatically.

**Without NFD (or on non-Flatcar nodes):**

```text
GPU Operator config:   driver.version = "535.104.05"
Actual image tag used: "535.104.05" (no suffix appended)
```

This is why it's critical to coordinate Flatcar version IDs between the vGPU Driver Operator's output tags and the GPU Operator's configuration.

## Configuration steps

### 1. Prerequisites

- vGPU Driver Operator is installed and has built driver images (see [Installation](installation.md)).
- GPU Operator chart is available (installed or ready to install).
- Your cluster has Flatcar nodes (or uses a standard Linux distribution with compatible drivers).

### 2. Create VGPUDriverImage

First, create a `VGPUDriverImage` to build driver images:

```yaml
apiVersion: vgpu.flatcar.io/v1alpha1
kind: VGPUDriverImage
metadata:
  name: vgpu-drivers
  namespace: vgpu-driver-operator
spec:
  driverVersions:
    - "535.104.05"
  
  source:
    type: s3
    uriTemplate: s3://vgpu-drivers/NVIDIA-Linux-x86_64-{driverVersion}-vgpu-kvm.run
    credentialsSecretRef:
      name: s3-driver-storage-secret
  
  registry:
    repository: harbor.example.com/vgpu-driver
    authSecretRef:
      name: private-registry-secret
  
  flatcar:
    discoverFromNodes: true
    trackChannels:
      - stable
```

Apply and wait for builds to complete:

```bash
kubectl apply -f vgpu-driver-image.yaml
kubectl get vgpudriverimages -n vgpu-driver-operator -w

# Check that images were built and pushed
kubectl describe vgpudriverimage vgpu-drivers -n vgpu-driver-operator
```

Expected output in `.status.builds[]`:

```yaml
- driverVersion: 535.104.05
  flatcarVersion: 4230.2.3
  mode: compiled
  tag: 535.104.05-flatcar4230.2.3
  phase: Complete
```

### 3. Verify images in registry

Before configuring the GPU Operator, confirm the images are in your registry:

```bash
# Using crane (if installed)
crane ls harbor.example.com/vgpu-driver | grep 535.104

# Or use your registry's CLI (e.g., `harbor` CLI, `aws ecr describe-images`)
```

### 4. Install/update GPU Operator with driver config

Create a values file for GPU Operator that references your built images:

```yaml
# gpu-operator-values.yaml

driver:
  # Use the same repository and version as configured in VGPUDriverImage.spec.registry
  repository: harbor.example.com/vgpu-driver
  
  # Driver version must match one of the tags pushed by vGPU Driver Operator
  version: "535.104.05"
  
  # For runtime mode (kernel modules compile on each node)
  usePrecompiled: false
  
  # Registry auth (if private)
  imagePullSecrets:
    - name: private-registry-secret

toolkit:
  enabled: true

gfd:
  enabled: true
  
# Optional: set resources, node selectors, tolerations for driver pods
driver_manager:
  nodeSelector:
    workload: gpu
```

Install the GPU Operator:

```bash
helm repo add nvidia https://nvidia.github.io/gpu-operator
helm repo update

helm install gpu-operator nvidia/gpu-operator \
  -n gpu-operator \
  --create-namespace \
  -f gpu-operator-values.yaml
```

### 5. Verify driver pod is running

Watch the GPU Operator deploy:

```bash
kubectl get pods -n gpu-operator -w
```

You should see:

- `gpu-operator-*` (main operator pod)
- `nvidia-driver-daemonset-*` (on each Flatcar node)

Check driver pod logs:

```bash
# Get a driver pod on a Flatcar node
NODE=$(kubectl get nodes -l kubernetes.io/os=linux -o name | head -1 | cut -d/ -f2)
POD=$(kubectl get pods -n gpu-operator -o wide | grep nvidia-driver | grep $NODE | awk '{print $1}')

kubectl logs -n gpu-operator $POD
```

Expected log output (tail):

```text
nvidia-driver: Loading NVIDIA GPU driver
nvidia-persistenced: Initialized
[OK] NVIDIA driver is ready
```

### 6. (Optional) Precompiled mode

For zero-downtime deployments with precompiled kernel modules:

First, update your `VGPUDriverImage` to build precompiled images:

```yaml
apiVersion: vgpu.flatcar.io/v1alpha1
kind: VGPUDriverImage
metadata:
  name: vgpu-drivers
  namespace: vgpu-driver-operator
spec:
  # ... (same as above)
  
  registry:
    repository: harbor.example.com/vgpu-driver
    repositoryPrecompiled: harbor.example.com/vgpu-driver-precompiled  # Add this
    authSecretRef:
      name: private-registry-secret
  
  precompile: true  # Enable precompiled builds
```

Wait for precompiled builds:

```bash
kubectl get vgpudriverimages -n vgpu-driver-operator -w
```

You'll see builds with `mode: precompiled` and tags like:

```text
535.104.05-6.1.65-flatcar4230.2.3
```

Then update GPU Operator values to use precompiled mode:

```yaml
driver:
  repository: harbor.example.com/vgpu-driver-precompiled
  version: "535.104.05"
  usePrecompiled: true
```

Update the GPU Operator:

```bash
helm upgrade gpu-operator nvidia/gpu-operator \
  -n gpu-operator \
  -f gpu-operator-values.yaml
```

The driver daemonset will now pull precompiled images; kernel module compilation is skipped on each node, reducing driver deployment time from ~10 min to ~1 min per node.

## Expected behavior and sequencing

### Scenario 1: Cluster with existing Flatcar version

1. vGPU Driver Operator is installed.
2. You create a `VGPUDriverImage` with driver version `535.104.05`.
3. Operator discovers nodes are running Flatcar `4230.2.3`.
4. Operator launches a BuildKit Job that builds and pushes `535.104.05-flatcar4230.2.3`.
5. **Job takes ~5 min; image is ready.**
6. You install/update GPU Operator to use `driver.repository=harbor.example.com/vgpu-driver` and `driver.version=535.104.05`.
7. GPU Operator's driver DaemonSet pulls the image `harbor.example.com/vgpu-driver:535.104.05-flatcar4230.2.3` (Flatcar suffix auto-appended).
8. **Driver pod compiles kernel modules (~10 min); driver is ready.**

### Scenario 2: Adding a new Flatcar version

1. Cluster gets upgraded to Flatcar `4231.0.0`.
2. vGPU Driver Operator's node discovery detects the new version.
3. Operator automatically launches a new BuildKit Job to build `535.104.05-flatcar4231.0.0`.
4. **Job takes ~5 min.**
5. Meanwhile, GPU Operator's daemonset pod on the new node tries to pull `harbor.example.com/vgpu-driver:535.104.05-flatcar4231.0.0`.
6. **If the image isn't ready yet**, the pod enters `ImagePullBackOff` until the job completes.
7. Once the image is pushed, Kubernetes retries the pull.
8. Driver starts compilation.

**Workaround**: Build images for future Flatcar versions in advance using `flatcar.trackChannels`:

```yaml
flatcar:
  trackChannels:
    - stable
    - lts
    - beta
```

This polls Flatcar release feeds and builds images for upcoming versions *before* nodes upgrade.

### Scenario 3: Precompiled mode rollout

1. Precompiled images exist in the registry for your Flatcar version(s).
2. GPU Operator is currently running with `usePrecompiled: false`.
3. You update GPU Operator values: `usePrecompiled: true`, `repository: harbor.example.com/vgpu-driver-precompiled`.
4. Helm upgrade rolls out a new driver DaemonSet.
5. Pods pull the precompiled image (kernel modules already compiled).
6. Driver loads immediately; no compilation phase.

## Troubleshooting

### Driver pod stuck in `ImagePullBackOff`

**Cause**: Image tag doesn't exist in the registry.

**Solution**:
1. Check GPU Operator configuration: `helm get values gpu-operator -n gpu-operator | grep -A 5 driver`
2. Check vGPU Driver Operator status: `kubectl describe vgpudriverimage vgpu-drivers -n vgpu-driver-operator`
3. If the image hasn't been built, wait for the BuildKit Job to complete or check its logs:

   ```bash
   kubectl logs -n vgpu-driver-operator -l app=vgpu-driver-operator -f
   ```

4. Verify the image exists in the registry:

   ```bash
   crane ls harbor.example.com/vgpu-driver | grep 535.104
   ```

### Driver module load fails

**Cause**: Kernel version mismatch between the compiled driver and the node's actual kernel.

**Solution**:
1. Check the node's kernel version: `kubectl get node <node> -o json | jq '.status.nodeInfo.kernelVersion'`
2. Check the built image's kernel version (from vGPU Driver Operator status).
3. If they don't match, the vGPU Driver Operator may have discovered the wrong kernel version. Enable NFD for accurate detection:

   ```bash
   helm install nfd nvdia/node-feature-discovery -n nfd-system --create-namespace
   ```

### Precompiled image too large

**Cause**: Precompiled images include full kernel module builds; they're larger than runtime images.

**Solution**:
- Use precompiled mode selectively (only for stable, long-lived Flatcar versions).
- Or stick with runtime mode and accept the ~10 min compilation per node.

## Monitoring

### Via Kubernetes events

```bash
kubectl describe vgpudriverimage vgpu-drivers -n vgpu-driver-operator
kubectl describe ds nvidia-driver-daemonset -n gpu-operator
```

### Via logs

```bash
# vGPU Driver Operator logs
kubectl logs -n vgpu-driver-operator -l app.kubernetes.io/name=vgpu-driver-operator -f

# GPU Operator logs
kubectl logs -n gpu-operator -l app=nvidia-gpu-operator -f

# Driver DaemonSet pod logs
kubectl logs -n gpu-operator -l app=nvidia-driver-daemonset -f
```

### Via metrics (if Prometheus is enabled)

The GPU Operator exports metrics on `localhost:9445`. The vGPU Driver Operator does not currently export metrics.

## Limitations and caveats

1. **No automatic GPU Operator update**: If you change the driver version in `VGPUDriverImage`, you must manually update the GPU Operator's `driver.version` value.
2. **No image digest pinning**: The vGPU Driver Operator tags images by version, not by digest. GPU Operator pulls by tag, which can occasionally cause skew if a tag is retagged. Use immutable image tags or digest pinning for critical deployments.
3. **No automatic rollback**: If a driver build fails, the GPU Operator continues running the previous driver version (if present). Monitor build status to avoid stale drivers.
4. **Precompiled for specific kernels**: Precompiled images are keyed by kernel version. If you have heterogeneous Flatcar kernels in your cluster (e.g., due to phased upgrades), you may need multiple precompiled images. The operator handles this automatically.
