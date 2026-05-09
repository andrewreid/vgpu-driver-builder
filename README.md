# NVIDIA vGPU Driver Operator for Flatcar Linux

In-cluster Kubernetes operator that builds and pushes NVIDIA vGPU driver images compatible with Flatcar Linux.

## What it does

- **Automatic image builds**: Fetch NVIDIA vGPU driver binaries from S3 and compile rootless BuildKit images on-cluster
- **Runtime compilation**: Kernel modules compile on each node at deploy time using Flatcar's built-in developer toolchain
- **Precompiled support**: Optionally build pre-baked images with kernel modules precompiled for specific Flatcar versions
- **Multiarch support**: Build for any Flatcar release and driver version combination; operator auto-discovers active Flatcar versions on cluster nodes
- **GPU Operator integration**: Works seamlessly with NVIDIA's official GPU Operator for runtime management and node scheduling

## Quickstart

### Install the operator

```bash
helm install vgpu-driver-operator charts/vgpu-driver-operator \
  -n vgpu-driver-operator --create-namespace
```

### Create an S3 secret

```bash
kubectl create secret generic s3-driver-storage-secret \
  -n vgpu-driver-operator \
  --from-literal=accessKeyId=<YOUR_KEY> \
  --from-literal=secretAccessKey=<YOUR_SECRET>
```

### Create a registry secret

```bash
kubectl create secret docker-registry private-registry-secret \
  -n vgpu-driver-operator \
  --docker-server=<REGISTRY_URL> \
  --docker-username=<USERNAME> \
  --docker-password=<PASSWORD>
```

### Provision a VGPUDriverImage

```yaml
apiVersion: vgpu.flatcar.io/v1alpha1
kind: VGPUDriverImage
metadata:
  name: vgpu-535
  namespace: vgpu-driver-operator
spec:
  driverVersions:
    - "535.104.05"
    - "535.261.03"
  
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

Apply and monitor:

```bash
kubectl apply -f vgpu-driver-image.yaml
kubectl get vgpudriverimages -n vgpu-driver-operator -w
kubectl logs -n vgpu-driver-operator -l app.kubernetes.io/name=vgpu-driver-operator -f
```

The operator will automatically build images and push them to your registry. Tags follow the pattern: `<registry>/<repository>:<driverVersion>-flatcar<flatcarVersionId>`.

## Architecture

An operator-driven approach to building Flatcar-compatible NVIDIA drivers on-cluster. See [docs/architecture.md](docs/architecture.md) for detailed design rationale, component interactions, and reconciliation flow.

## Status

**Version:** v0.1.0

**In scope:**
- Runtime-compiled driver images (dynamic kernel module compilation on each node)
- Precompiled driver images (kernel modules baked during build)
- Automatic Flatcar version discovery via node inspection
- Garbage collection of old images by Flatcar version
- Multi-driver-version support per VGPUDriverImage

**Out of scope:**
- Kernel module signing
- Direct GPU Operator integration (GPU Operator consumes built images independently)
- Custom driver patches or modifications

## Documentation

- **[Installation](docs/installation.md)** — Prerequisites, Helm configuration, secrets provisioning, and values reference
- **[GPU Operator Integration](docs/gpu-operator-integration.md)** — Wiring the NVIDIA GPU Operator to use built images
- **[Architecture](docs/architecture.md)** — Design rationale, component diagram, and reconciliation pseudocode

## License

This project is licensed under the [Apache License 2.0](LICENSE).
