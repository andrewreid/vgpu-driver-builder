# Migration from Terraform

This guide explains how to migrate from the old Terraform module (`main.tf`, `variables.tf`, etc.) to the new Helm-based vGPU Driver Operator.

## Overview

**Old model**: Terraform module that created a one-time Kubernetes Job to build and push driver images. Version list hardcoded in Terraform variables.

**New model**: Kubernetes operator (deployed via Helm) that reconciles a `VGPUDriverImage` CRD. Auto-discovers Flatcar versions, rebuilds on change, garbage-collects old images.

**Key benefits**:
- No Terraform state to manage.
- Declarative Kubernetes-native config (CRD spec).
- Auto-discovery of Flatcar versions (no manual config updates).
- Continuous reconciliation (rebuilds if driver or Flatcar versions change).
- Built-in garbage collection.

## Variable mapping

Map each old Terraform variable to a new Helm value or CRD field:

| Old Terraform Variable | New Helm/CRD Location | Notes |
|------------------------|-----------------------|-------|
| `enabled` | (removed) | Helm `install` vs. skipped; no longer needed. |
| `namespace` | Chart default: `vgpu-driver-operator` | Can be overridden on `helm install`. |
| `vgpu_driver_version` | `VGPUDriverImage.spec.driverVersions[]` | Now a list; add multiple versions if desired. |
| `vgpu_driver_s3_uri` | `VGPUDriverImage.spec.source.uriTemplate` | URI template with `{driverVersion}` placeholder. |
| `s3_endpoint_url` | `S3 Secret` mounted from K8s | Create a Secret; operator reads it. |
| `s3_access_key_id` | `S3 Secret.data.accessKeyId` | Create `s3-driver-storage-secret` K8s Secret. |
| `s3_secret_access_key` | `S3 Secret.data.secretAccessKey` | Create `s3-driver-storage-secret` K8s Secret. |
| `private_registry_url` | `VGPUDriverImage.spec.registry.repository` | Repository path in the registry. |
| `private_registry_auth` | `Registry Secret` mounted from K8s | Create a docker-config Secret; operator reads it. |
| `os_tag` | `VGPUDriverImage.spec...` | No direct equivalent; tag scheme is now fixed. Images are tagged `<driver>-flatcar<flatcar>`. |
| `flatcar_version` | `VGPUDriverImage.spec.flatcar.discoverFromNodes` | Auto-discovered from nodes; no manual config needed. Optionally pin with `trackChannels`. |
| `prebuild_targets` | `VGPUDriverImage.spec.precompile` + `spec.registry.repositoryPrecompiled` | Single boolean flag; all (driver, flatcar, kernel) combinations are built. |

## Migration steps

### Step 1: Do NOT destroy the old Terraform state yet

Running `terraform destroy` will delete the old namespace, Secrets, and ConfigMaps. This is fine, but it's safer to let the old resources age out naturally or delete them manually once the new operator is confirmed working.

**Recommendation**: Leave the old Terraform state in place but do not update or re-apply it. The old namespace will persist until you manually delete it.

### Step 2: Install the new operator

```bash
helm repo add vgpu-driver-operator <chart-repo>  # (if applicable)
helm repo update

helm install vgpu-driver-operator charts/vgpu-driver-operator \
  -n vgpu-driver-operator \
  --create-namespace
```

Verify the operator is running:

```bash
kubectl get pods -n vgpu-driver-operator
kubectl logs -n vgpu-driver-operator -l app.kubernetes.io/name=vgpu-driver-operator
```

### Step 3: Create S3 secret

Create a Kubernetes Secret with the S3 endpoint URL and credentials:

```bash
kubectl create secret generic s3-driver-storage-secret \
  -n vgpu-driver-operator \
  --from-literal=accessKeyId=<S3_ACCESS_KEY> \
  --from-literal=secretAccessKey=<S3_SECRET_KEY>
```

(Adjust names and keys to match your `VGPUDriverImage` spec.)

### Step 4: Create registry secret

```bash
kubectl create secret docker-registry private-registry-secret \
  -n vgpu-driver-operator \
  --docker-server=<REGISTRY_URL> \
  --docker-username=<REGISTRY_USER> \
  --docker-password=<REGISTRY_PASSWORD>
```

Or, if you need to specify a custom S3 endpoint, create the S3 secret with additional keys. The operator uses `endpointUrl` from the S3 secret or the URI template. (This is an implementation detail; see operator source code for specifics.)

### Step 5: Create VGPUDriverImage CRD

Based on your old Terraform variables, create a `VGPUDriverImage` YAML:

**Example: migrating from this Terraform config:**

```hcl
module "vgpu_driver_builder" {
  source = "./modules/rke2/nvidia-vgpu-driver-builder"

  enabled             = true
  vgpu_driver_version = "535.104.05"
  vgpu_driver_s3_uri  = "s3://my-bucket/NVIDIA-Linux-x86_64-535.104.05-vgpu-kvm.run"
  s3_endpoint_url     = "https://minio.example.com"
  s3_access_key_id    = var.s3_key
  s3_secret_access_key = var.s3_secret

  private_registry_url = "harbor.example.com/gpu-drivers"
  private_registry_auth = {
    username = var.registry_user
    password = var.registry_pass
  }

  flatcar_version = "4230.2.3"
  os_tag          = "flatcar4230.2.3"
  prebuild_targets = []
}
```

**Create this VGPUDriverImage:**

```yaml
apiVersion: vgpu.flatcar.io/v1alpha1
kind: VGPUDriverImage
metadata:
  name: vgpu-drivers
  namespace: vgpu-driver-operator
spec:
  # Single driver version (migrate from terraform)
  driverVersions:
    - "535.104.05"

  # Source config: replace the s3:// URI with a template
  source:
    type: s3
    # Construct the URI template from the old s3:// and the vgpu_driver_s3_uri
    # Old: s3://my-bucket/NVIDIA-Linux-x86_64-535.104.05-vgpu-kvm.run
    # Template (generalized): s3://my-bucket/NVIDIA-Linux-x86_64-{driverVersion}-vgpu-kvm.run
    uriTemplate: s3://my-bucket/NVIDIA-Linux-x86_64-{driverVersion}-vgpu-kvm.run
    credentialsSecretRef:
      name: s3-driver-storage-secret

  # Registry config
  registry:
    # Use the old private_registry_url as the repository
    repository: harbor.example.com/gpu-drivers
    authSecretRef:
      name: private-registry-secret

  # Auto-discover Flatcar versions (replaces flatcar_version)
  flatcar:
    discoverFromNodes: true
    # Optionally pin to specific channels if you want to pre-build future versions
    trackChannels:
      - stable

  # precompile: false (default) — matches the old behavior (no prebuild_targets)
```

Apply this CRD:

```bash
kubectl apply -f vgpu-driver-image.yaml
```

### Step 6: Monitor the operator build

Watch the operator reconcile:

```bash
kubectl get vgpudriverimages -n vgpu-driver-operator -w

# Detailed status
kubectl describe vgpudriverimage vgpu-drivers -n vgpu-driver-operator
```

Expected output (after ~5 minutes):

```yaml
Status:
  Builds:
    - Driver Version:      535.104.05
      Flatcar Version:     4230.2.3
      Mode:                compiled
      Tag:                 535.104.05-flatcar4230.2.3
      Phase:               Complete
      Message:             Build and push completed successfully
  Conditions:
    - Type:     Reconciled
      Status:   True
      Reason:   ReconciliationComplete
```

### Step 7: Verify image is in the registry

List images to confirm the build was pushed:

```bash
# Using crane (if installed)
crane ls harbor.example.com/gpu-drivers | grep 535.104

# Or use your registry's API/CLI
```

### Step 8: Update GPU Operator (if running)

If you have the NVIDIA GPU Operator installed, update its driver configuration to use the new image:

```bash
# Old: privately configured via Terraform
# New: explicitly configure GPU Operator

helm upgrade gpu-operator nvidia/gpu-operator \
  -n gpu-operator \
  --set driver.repository=harbor.example.com/gpu-drivers \
  --set driver.version=535.104.05
```

See [GPU Operator Integration](gpu-operator-integration.md) for details.

### Step 9: Delete the old Terraform namespace (once confirmed)

After confirming the new operator builds and the GPU Operator is consuming the images, you can clean up the old Terraform state:

```bash
# Delete the old namespace created by Terraform
kubectl delete namespace vgpu-driver-builder

# (Optional) Remove Terraform state
rm -rf .terraform
rm terraform.tfstate terraform.tfstate.backup
rm -f *.tf
```

Do **not** do this until you're confident the new operator is working.

## Example: migrating with precompiled mode

If the old Terraform config had `prebuild_targets`, enable precompilation in the new CRD:

**Old Terraform:**

```hcl
prebuild_targets = [
  {
    name             = "flatcar-4230"
    flatcar_version  = "4230.2.3"
    driver_version   = "535.104.05"
    driver_s3_uri    = "s3://my-bucket/NVIDIA-Linux-x86_64-535.104.05-vgpu-kvm.run"
    os_tag           = "flatcar4230.2.3-precompiled"
  }
]
```

**New CRD (with precompile enabled):**

```yaml
spec:
  driverVersions:
    - "535.104.05"
  
  source:
    type: s3
    uriTemplate: s3://my-bucket/NVIDIA-Linux-x86_64-{driverVersion}-vgpu-kvm.run
    credentialsSecretRef:
      name: s3-driver-storage-secret

  registry:
    repository: harbor.example.com/gpu-drivers
    repositoryPrecompiled: harbor.example.com/gpu-drivers-precompiled  # Add this
    authSecretRef:
      name: private-registry-secret

  flatcar:
    discoverFromNodes: true

  precompile: true  # Enable precompiled builds
```

The operator will now build both:
- **Runtime**: `535.104.05-flatcar4230.2.3`
- **Precompiled**: `535.104.05-6.1.65-flatcar4230.2.3` (one per kernel version)

## Image tag differences

**Old Terraform** (example with `os_tag`):

```text
harbor.example.com/gpu-drivers:535.104.05
harbor.example.com/gpu-drivers:535.104.05-flatcar4230.2.3  (if os_tag was set)
```

**New operator** (always includes Flatcar version):

```text
harbor.example.com/gpu-drivers:535.104.05-flatcar4230.2.3
```

If you need the old tag without the Flatcar suffix, you must manually tag the image or use a registry alias. The operator always produces tags with `-flatcar<version>`.

## Adding new driver versions

**Old way**: Update `vgpu_driver_version` in Terraform, re-apply, wait for Job.

**New way**: Edit the CRD:

```bash
kubectl patch vgpudriverimage vgpu-drivers -n vgpu-driver-operator --type='json' -p='[
  {"op": "replace", "path": "/spec/driverVersions", "value": ["535.104.05", "535.261.03"]}
]'
```

The operator will automatically launch builds for the new version.

## Rollback plan

If the new operator has issues:

1. The old Terraform state and namespace still exist (unless you deleted them).
2. Re-apply the old Terraform: `terraform apply`.
3. Kubernetes will reprovision the old Job.
4. Once confirmed, delete the new operator: `helm uninstall vgpu-driver-operator -n vgpu-driver-operator`.

This is why it's important not to destroy the old Terraform state immediately.

## FAQ

**Q: Can I run both Terraform and the operator at the same time?**

A: Not recommended. Both will try to build and push images, causing conflicts. Migrate fully, then remove the old state.

**Q: Does the operator auto-reapply the CRD if I delete it?**

A: No. CRD deletion stops all builds. If deleted, re-apply it to resume.

**Q: What if I have multiple driver versions per Flatcar version?**

A: Add them all to `spec.driverVersions[]`. The operator builds all combinations (driver × flatcar).

**Q: Can I pin the operator to a specific Flatcar version (not auto-discover)?**

A: Yes, set `flatcar.discoverFromNodes: false` and only use `trackChannels` (or neither). Then manually apply the CRD with specific Flatcar versions if needed. (This requires CRD schema changes; not yet supported out-of-the-box.)

**Q: What about the old ConfigMap and Secret names?**

A: The old Terraform-created resources live in the `vgpu-driver-builder` namespace. The new operator uses `vgpu-driver-operator` namespace and different Secret names. You must create new Secrets in the new namespace.
