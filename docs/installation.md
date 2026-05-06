# Installation

## Prerequisites

- **Kubernetes cluster**: v1.27 or later. The operator uses standard Kubernetes Job APIs and CRD features.
- **Helm 3**: To install the chart.
- **(Optional) Node Feature Discovery**: For accurate Flatcar and kernel version labels on nodes. Without NFD, the operator falls back to parsing `node.status.nodeInfo.osImage` and `node.status.nodeInfo.kernelVersion`, which is less reliable but works in most cases.
- **BuildKit access**: Build Jobs use `moby/buildkit:rootless` images. Ensure your container registry (or default registry) is reachable from cluster nodes.
- **S3 credentials**: For fetching NVIDIA vGPU driver `.run` files. Can be AWS S3, MinIO, or any S3-compatible endpoint.
- **Private registry credentials**: For pushing built images. Must support standard Docker authentication.

### Flatcar-specific notes

The operator works on any Kubernetes cluster but is designed for clusters running Flatcar Container Linux:

- On Flatcar nodes, the operator automatically discovers Flatcar versions and kernel versions from node metadata.
- Without Flatcar nodes, `flatcar.discoverFromNodes` should be set to `false`, and `flatcar.trackChannels` can still fetch new releases for `VGPUDriverImage` reconciliation.

## Helm installation

### Basic installation (development)

```bash
helm install vgpu-driver-operator charts/vgpu-driver-operator \
  -n vgpu-driver-operator \
  --create-namespace
```

This creates the operator Deployment, CRD, and RBAC roles, but **does not** provision S3 or registry secrets. You must create these separately.

### Production installation with external-secrets

For production, use [external-secrets-operator](https://external-secrets.io/) to manage S3 and registry credentials:

```yaml
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: aws-secret-store
  namespace: vgpu-driver-operator
spec:
  provider:
    aws:
      service: SecretsManager
      region: us-east-1
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets-operator

---
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: s3-driver-storage-secret
  namespace: vgpu-driver-operator
spec:
  secretStoreRef:
    name: aws-secret-store
    kind: SecretStore
  target:
    name: s3-driver-storage-secret
    creationPolicy: Owner
  data:
    - secretKey: accessKeyId
      remoteRef:
        key: vgpu-driver-s3
        property: accessKeyId
    - secretKey: secretAccessKey
      remoteRef:
        key: vgpu-driver-s3
        property: secretAccessKey

---
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: private-registry-secret
  namespace: vgpu-driver-operator
spec:
  secretStoreRef:
    name: aws-secret-store
    kind: SecretStore
  target:
    name: private-registry-secret
    creationPolicy: Owner
    template:
      type: kubernetes.io/dockercfg
      data:
        .dockercfg: |
          {"{{ .registryUrl }}": {"auth": "{{ .registryAuth | b64enc }}"}}
  data:
    - secretKey: registryUrl
      remoteRef:
        key: vgpu-driver-registry
        property: url
    - secretKey: registryAuth
      remoteRef:
        key: vgpu-driver-registry
        property: auth
```

Then install the Helm chart:

```bash
helm install vgpu-driver-operator charts/vgpu-driver-operator \
  -n vgpu-driver-operator \
  --create-namespace
```

### Inline secret provisioning (development only)

To create secrets from the Helm values (not recommended for production):

```bash
helm install vgpu-driver-operator charts/vgpu-driver-operator \
  -n vgpu-driver-operator \
  --create-namespace \
  --set s3.create=true \
  --set s3.endpointUrl=https://s3.amazonaws.com \
  --set s3.accessKeyId=<YOUR_KEY> \
  --set s3.secretAccessKey=<YOUR_SECRET> \
  --set registry.create=true \
  --set registry.url=harbor.example.com \
  --set registry.username=<USERNAME> \
  --set registry.password=<PASSWORD>
```

## Values reference

### Operator image settings

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `image.repository` | string | `ghcr.io/andrewreid/vgpu-driver-operator` | OCI repository for the operator image. |
| `image.tag` | string | `""` (defaults to chart appVersion) | Image tag. |
| `image.pullPolicy` | string | `IfNotPresent` | Kubernetes imagePullPolicy. |
| `imagePullSecrets` | list | `[]` | Optional list of image pull secrets for operator pod. |

### Resource naming

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `nameOverride` | string | `""` | Override the chart name fragment in resource names. |
| `fullnameOverride` | string | `""` | Fully override the resource name. |

### ServiceAccount

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `serviceAccount.create` | bool | `true` | Create a dedicated ServiceAccount. |
| `serviceAccount.annotations` | object | `{}` | Annotations for the ServiceAccount (e.g., IRSA role ARN for AWS). |
| `serviceAccount.name` | string | `""` | ServiceAccount name (auto-generated if blank). |

### Pod and container security

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `podSecurityContext` | object | See below | Pod-level security context. |
| `securityContext` | object | See below | Container-level security context. |

**Default podSecurityContext:**

```yaml
runAsNonRoot: true
runAsUser: 65532
seccompProfile:
  type: RuntimeDefault
```

**Default securityContext:**

```yaml
allowPrivilegeEscalation: false
capabilities:
  drop:
    - ALL
readOnlyRootFilesystem: true
```

### Resource requests and limits

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `resources.requests.cpu` | string | `100m` | CPU request. |
| `resources.requests.memory` | string | `128Mi` | Memory request. |
| `resources.limits.cpu` | string | `500m` | CPU limit. |
| `resources.limits.memory` | string | `512Mi` | Memory limit. |

### Node scheduling

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `nodeSelector` | object | `{}` | Node selector for operator pod. |
| `tolerations` | list | `[]` | Tolerations for operator pod. |
| `affinity` | object | `{}` | Affinity rules for operator pod. |

### Flatcar poller

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `flatcar.pollSchedule` | string | `@hourly` | Cron schedule for polling Flatcar release feeds. |
| `flatcar.pollEnabled` | bool | `true` | Enable/disable Flatcar poller CronJob. |

### S3 secret (development)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `s3.create` | bool | `false` | Create a Secret from the values below. |
| `s3.endpointUrl` | string | `""` | S3-compatible endpoint URL (e.g., `https://s3.amazonaws.com`). |
| `s3.accessKeyId` | string | `""` | AWS/S3 access key ID. |
| `s3.secretAccessKey` | string | `""` | AWS/S3 secret access key. |

### Registry secret (development)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `registry.create` | bool | `false` | Create a Secret from the values below. |
| `registry.url` | string | `""` | Registry URL (e.g., `ghcr.io`). |
| `registry.username` | string | `""` | Registry username. |
| `registry.password` | string | `""` | Registry password or token. |

### Logging

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `kopfLogLevel` | string | `INFO` | Log level forwarded to kopf. Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

### Build assets

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `buildAssets.configMapName` | string | `driver-build-files` | Name of the ConfigMap containing build scripts and Dockerfiles. This ConfigMap is automatically rendered from `charts/vgpu-driver-operator/files/build/`. |

## CRD reference

### VGPUDriverImage spec

See the [VGPUDriverImage CRD definition](../charts/vgpu-driver-operator/crds/vgpudriverimages.vgpu.flatcar.io.yaml) for the complete OpenAPI v3 schema.

#### Required fields

| Field | Type | Description |
|-------|------|-------------|
| `driverVersions` | `string[]` | List of NVIDIA vGPU driver versions to build. Example: `["535.104.05", "535.261.03"]`. |
| `source` | object | Source configuration for driver assets. |
| `source.type` | `s3` \| `http` | Protocol. |
| `source.uriTemplate` | string | URI template with placeholders `{driverVersion}`, `{flatcarVersion}`, `{arch}`. Example: `s3://my-bucket/drivers/NVIDIA-Linux-x86_64-{driverVersion}-vgpu-kvm.run`. |
| `registry` | object | Target OCI registry. |
| `registry.repository` | string | Target repository for runtime images. Example: `harbor.example.com/vgpu-driver`. |

#### Optional fields (common)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `source.credentialsSecretRef.name` | string | (none) | Secret name for S3 credentials (keys: `accessKeyId`, `secretAccessKey`). Required if S3 is private. |
| `registry.repositoryPrecompiled` | string | (none) | Target repository for precompiled images. Only used if `precompile: true`. |
| `registry.cacheRepository` | string | (none) | BuildKit cache repository. Speeds up rebuilds. |
| `registry.authSecretRef.name` | string | (none) | Secret name for registry credentials (key: `.dockerconfigjson`). Required if registry is private. |

#### Flatcar discovery

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `flatcar.discoverFromNodes` | bool | `true` | Watch cluster nodes for Flatcar versions. |
| `flatcar.nodeSelector` | object | (none) | Label selector to filter nodes when discovering. |
| `flatcar.trackChannels` | `string[]` | (none) | Flatcar channels to track: `stable`, `lts`, `beta`, `alpha`. |
| `flatcar.arch` | string | `amd64` | CPU architecture for release lookups. |

#### Build modes

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `precompile` | bool | `false` | Also build precompiled (kernel modules pre-baked) images. |
| `build.buildkitImage` | string | `moby/buildkit:rootless` | BuildKit container image. |
| `build.resources` | object | (none) | Resource requests/limits for build Job containers. |
| `build.nodeSelector` | object | (none) | Node selector for build Jobs. |
| `build.tolerations` | list | (none) | Tolerations for build Jobs. |

#### Garbage collection

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `retention.enabled` | bool | `false` | Enable automatic GC. |
| `retention.keepPreviousFlatcarVersions` | int | `0` | Number of previous Flatcar versions to keep (per driver). |
| `retention.minAgeBeforeDelete` | string | `168h` | Go duration; minimum age before a tag is eligible for deletion. |

### VGPUDriverImage status

The operator updates `.status` with reconciliation results:

| Field | Type | Description |
|-------|------|-------------|
| `observedNodes` | object[] | Flatcar and kernel versions discovered on cluster nodes. Each entry: `{flatcarVersion, kernelVersion, nodeCount}`. |
| `trackedChannelVersions` | object[] | Latest releases per tracked channel. Each entry: `{channel, flatcarVersion, kernelVersion, observedAt}`. |
| `builds` | object[] | Per-combination build results. Each entry: `{driverVersion, flatcarVersion, kernelVersion?, mode, tag, phase, jobName, lastTransitionTime, message}`. |
| `conditions` | object[] | Standard Kubernetes conditions. Examples: `Reconciled`, `BuildsComplete`, `GCComplete`. |
| `pruned` | object[] | Record of tags deleted by GC. Each entry: `{tag, reason, prunedAt}`. |

### Example VGPUDriverImage

```yaml
apiVersion: vgpu.flatcar.io/v1alpha1
kind: VGPUDriverImage
metadata:
  name: vgpu-drivers
  namespace: vgpu-driver-operator
spec:
  # Driver versions to build
  driverVersions:
    - "535.104.05"
    - "535.261.03"

  # Where to find NVIDIA driver .run files
  source:
    type: s3
    uriTemplate: s3://vgpu-drivers/NVIDIA-Linux-x86_64-{driverVersion}-vgpu-kvm.run
    credentialsSecretRef:
      name: s3-driver-storage-secret

  # Where to push built images
  registry:
    repository: harbor.example.com/vgpu-driver
    repositoryPrecompiled: harbor.example.com/vgpu-driver-precompiled
    cacheRepository: harbor.example.com/vgpu-driver-cache
    authSecretRef:
      name: private-registry-secret

  # Auto-discover Flatcar versions from cluster nodes
  flatcar:
    discoverFromNodes: true
    trackChannels:
      - stable
      - lts
    arch: amd64

  # Build both runtime and precompiled images
  precompile: true

  # BuildKit job tuning
  build:
    buildkitImage: moby/buildkit:rootless
    resources:
      requests:
        cpu: 2
        memory: 4Gi
      limits:
        cpu: 4
        memory: 8Gi
    nodeSelector:
      workload: build

  # Garbage collection: keep 2 previous Flatcar versions per driver
  retention:
    enabled: true
    keepPreviousFlatcarVersions: 2
    minAgeBeforeDelete: "168h"
```

Then apply:

```bash
kubectl apply -f vgpu-driver-image.yaml

# Watch reconciliation progress
kubectl get vgpudriverimages -n vgpu-driver-operator -w

# Check status
kubectl describe vgpudriverimage vgpu-drivers -n vgpu-driver-operator

# View operator logs
kubectl logs -n vgpu-driver-operator -l app.kubernetes.io/name=vgpu-driver-operator -f
```

## Build Job lifecycle

When the operator reconciles a `VGPUDriverImage`:

1. **Check existing builds**: Query the registry for images matching the desired driver + Flatcar combinations.
2. **Identify missing**: For any missing combination, create a Kubernetes Job.
3. **Job manifest includes**:
   - `spec.template.spec.containers[0].image`: BuildKit image
   - `volumeMounts`:
     - `/buildkit/run/secrets/docker` — docker-config Secret (if auth required)
     - `/buildfiles` — ConfigMap with Dockerfiles and scripts
   - `env`:
     - `DRIVER_VERSION`, `FLATCAR_VERSION`, `PRECOMPILE` — build parameters
     - `S3_*`, `REGISTRY_*` — credentials from Secrets
   - `serviceAccountName` — with permission to read Secrets and ConfigMaps
4. **BuildKit pod** fetches driver from S3, builds the image, pushes to registry.
5. **Job completes**: Operator updates `.status.builds[]` with result (success, error, retry).

### Expected timing

- **Runtime build**: ~5 minutes (download driver, install NVIDIA user-space, embed scripts).
- **Precompiled build**: ~20 minutes per version (compile kernel modules upfront).
- **Registry push**: ~1 minute for a small image.

If a build takes much longer, check:
- BuildKit pod logs: `kubectl logs -n vgpu-driver-operator <pod-name>`
- Network connectivity: can the pod reach S3 and the registry?
- Registry quota: is the registry full?
