"""Kubernetes Job factory for driver build jobs — no kubernetes client imports."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from vgpu_driver_operator.reconciler import BuildKey, runtime_tag

_DEFAULT_BUILDKIT_IMAGE = "moby/buildkit:rootless"
_FETCH_DRIVER_IMAGE = "amazon/aws-cli:2.13.0"

# Kubernetes resource name / label value length limits.
_K8S_NAME_MAX = 63
_K8S_LABEL_MAX = 63


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def build_job_name(key: BuildKey, inputs_hash: str) -> str:
    """Generate a Job name that fits within 63 characters.

    Format (before truncation):
        ``vgpu-build-{mode}-{driver_s}-fc-{flatcar_s}-{hash6}``

    where ``mode`` is ``runtime`` or ``prec``, sanitised versions replace
    ``[._]`` with ``-`` and are lowercased, and ``hash6`` is the first 6
    chars of *inputs_hash*.
    """
    mode = "prec" if key.precompile else "runtime"
    driver_s = _sanitize(key.driver)
    flatcar_s = _sanitize(key.flatcar)
    hash6 = inputs_hash[:6]

    parts = ["vgpu-build", mode, driver_s, "fc", flatcar_s, hash6]
    name = "-".join(parts)
    if len(name) > _K8S_NAME_MAX:
        suffix = f"-{hash6}"
        max_prefix = _K8S_NAME_MAX - len(suffix)
        name = name[:max_prefix] + suffix
    return name


def build_job_manifest(
    *,
    crd_namespace: str,
    crd_name: str,
    crd_uid: str,
    spec: dict,
    key: BuildKey,
    s3_secret_name: str,
    registry_secret_name: str | None,
    dockerfile_configmap: str,
    buildfiles_configmap: str,
    flatcar_image_digest: str | None = None,
    git_revision: str = "",
    build_created: str = "",
) -> dict:
    """Return a ``batch/v1`` Job manifest dict for a driver build.

    Parameters
    ----------
    crd_namespace:
        Namespace where the VGPUDriverImage CRD (and the Job) live.
    crd_name:
        Name of the owning VGPUDriverImage resource.
    crd_uid:
        UID of the owning VGPUDriverImage resource (used in ownerReference).
    spec:
        The ``.spec`` dict from the VGPUDriverImage CR.
    key:
        ``BuildKey`` identifying the specific combination to build.
    s3_secret_name:
        Name of the Secret holding S3 credentials.
    registry_secret_name:
        Name of the Secret holding registry docker-config, or ``None`` if no
        auth is required.
    dockerfile_configmap:
        Name of the ConfigMap that holds the Dockerfile(s).
    buildfiles_configmap:
        Name of the ConfigMap that holds auxiliary build files (nvidia-driver
        script etc.).
    flatcar_image_digest:
        Optional OCI digest of the Flatcar base image; when set, the
        ``FLATCAR_IMAGE_REF`` build-arg is added.
    git_revision:
        Short git SHA passed as ``GIT_REVISION`` build-arg.
    build_created:
        RFC-3339 timestamp passed as ``BUILD_CREATED`` build-arg.
    """
    registry: dict = spec.get("registry") or {}
    build_cfg: dict = spec.get("build") or {}
    source: dict = spec.get("source") or {}

    buildkit_image: str = build_cfg.get("buildkitImage") or _DEFAULT_BUILDKIT_IMAGE

    # Compute inputs hash for the job name.
    spec_hash = hashlib.sha256(str(sorted(spec.items())).encode()).hexdigest()[:8]
    inputs_str = key.driver + key.flatcar + str(key.precompile) + spec_hash
    inputs_hash = hashlib.sha256(inputs_str.encode()).hexdigest()
    job_name = build_job_name(key, inputs_hash)

    # Choose repository and dockerfile based on mode.
    if key.precompile:
        repo = registry.get("repositoryPrecompiled") or registry.get("repository", "")
        dockerfile_key = "Dockerfile.prebuilt"
        dockerfile_mount_path = "/workspace/prebuild/Dockerfile"
        mode_label = "precompiled"
    else:
        repo = registry.get("repository", "")
        dockerfile_key = "Dockerfile"
        dockerfile_mount_path = "/workspace/Dockerfile"
        mode_label = "runtime"

    # Cache flags.
    cache_repo = registry.get("cacheRepository", "")
    cache_flags: list[str] = []
    if cache_repo:
        cache_flags = [
            f"--import-cache type=registry,ref={cache_repo}:shared",
            f"--export-cache type=registry,ref={cache_repo}:shared,mode=max",
        ]

    # S3 URI from template.
    uri_template: str = source.get("uriTemplate", "")
    driver_s3_uri = uri_template.replace("${DRIVER_VERSION}", key.driver).replace("{driverVersion}", key.driver)

    # Build the init container command.
    init_command = (
        "aws s3 --endpoint-url $S3_ENDPOINT_URL cp "
        f"$VGPU_DRIVER_S3_URI /workspace/NVIDIA-Linux-x86_64-{key.driver}.run"
    )

    # Build the main container command.
    if key.precompile:
        main_command = _build_precompile_command(
            key=key,
            repo=repo,
            cache_flags=cache_flags,
            flatcar_image_digest=flatcar_image_digest,
            git_revision=git_revision,
            build_created=build_created,
        )
    else:
        out_tag = runtime_tag(key)
        full_output_ref = f"{repo}:{out_tag}" if repo else out_tag
        build_args = _base_build_args(key, flatcar_image_digest, git_revision, build_created, repo)
        main_command = _build_runtime_command(
            cache_flags=cache_flags,
            build_args=build_args,
            full_output_ref=full_output_ref,
        )

    # Labels.
    labels: dict[str, str] = {
        "app": "vgpu-driver-builder",
        "app.kubernetes.io/component": "builder",
        "vgpu.flatcar.io/driver-version": _truncate_label(key.driver),
        "vgpu.flatcar.io/flatcar-version": _truncate_label(key.flatcar),
        "vgpu.flatcar.io/mode": mode_label,
        "vgpu.flatcar.io/owner-uid": _truncate_label(crd_uid),
    }

    # Volume mounts for the main container.
    volume_mounts: list[dict] = [
        {"name": "build-context", "mountPath": "/workspace"},
        {
            "name": "dockerfile",
            "mountPath": dockerfile_mount_path,
            "subPath": dockerfile_key,
        },
        {
            "name": "build-files",
            "mountPath": "/workspace/nvidia-driver",
            "subPath": "nvidia-driver",
        },
    ]
    if registry_secret_name:
        volume_mounts.append(
            {"name": "docker-config", "mountPath": "/kaniko/.docker/", "readOnly": True}
        )

    # Volumes.
    volumes: list[dict] = [
        {"name": "build-context", "emptyDir": {}},
        {
            "name": "dockerfile",
            "configMap": {"name": dockerfile_configmap},
        },
        {
            "name": "build-files",
            "configMap": {"name": buildfiles_configmap},
        },
    ]
    if registry_secret_name:
        volumes.append(
            {
                "name": "docker-config",
                "secret": {
                    "secretName": registry_secret_name,
                    "items": [
                        {"key": ".dockerconfigjson", "path": "config.json"}
                    ],
                },
            }
        )

    # Main container spec.
    main_container: dict[str, Any] = {
        "name": "buildkit",
        "image": buildkit_image,
        "command": ["/bin/sh", "-c", main_command],
        "volumeMounts": volume_mounts,
        "securityContext": {
            "privileged": True,
        },
    }
    if "resources" in build_cfg and build_cfg["resources"]:
        main_container["resources"] = build_cfg["resources"]

    # Init container env.
    init_env: list[dict] = [
        {
            "name": "S3_ENDPOINT_URL",
            "valueFrom": {
                "secretKeyRef": {
                    "name": s3_secret_name,
                    "key": "S3_ENDPOINT_URL",
                }
            },
        },
        {
            "name": "AWS_ACCESS_KEY_ID",
            "valueFrom": {
                "secretKeyRef": {
                    "name": s3_secret_name,
                    "key": "AWS_ACCESS_KEY_ID",
                }
            },
        },
        {
            "name": "AWS_SECRET_ACCESS_KEY",
            "valueFrom": {
                "secretKeyRef": {
                    "name": s3_secret_name,
                    "key": "AWS_SECRET_ACCESS_KEY",
                }
            },
        },
        {
            "name": "VGPU_DRIVER_S3_URI",
            "value": driver_s3_uri,
        },
        {
            "name": "DRIVER_VERSION",
            "value": key.driver,
        },
    ]

    # Owner reference.
    owner_ref: dict = {
        "apiVersion": "vgpu.flatcar.io/v1alpha1",
        "kind": "VGPUDriverImage",
        "name": crd_name,
        "uid": crd_uid,
        "controller": True,
        "blockOwnerDeletion": True,
    }

    # Assemble the manifest.
    manifest: dict = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": crd_namespace,
            "labels": labels,
            "ownerReferences": [owner_ref],
        },
        "spec": {
            "backoffLimit": 0,
            "template": {
                "metadata": {
                    "labels": labels,
                },
                "spec": {
                    "restartPolicy": "Never",
                    "dnsConfig": {
                        "options": [{"name": "ndots", "value": "1"}]
                    },
                    "initContainers": [
                        {
                            "name": "fetch-driver",
                            "image": _FETCH_DRIVER_IMAGE,
                            "command": ["/bin/sh", "-c", init_command],
                            "env": init_env,
                            "volumeMounts": [
                                {
                                    "name": "build-context",
                                    "mountPath": "/workspace",
                                }
                            ],
                        }
                    ],
                    "containers": [main_container],
                    "volumes": volumes,
                },
            },
        },
    }

    # Optional node selector and tolerations from build config.
    pod_spec = manifest["spec"]["template"]["spec"]
    if build_cfg.get("nodeSelector"):
        pod_spec["nodeSelector"] = build_cfg["nodeSelector"]
    if build_cfg.get("tolerations"):
        pod_spec["tolerations"] = build_cfg["tolerations"]

    return manifest


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sanitize(s: str) -> str:
    """Replace ``[._]`` with ``-`` and lowercase."""
    return re.sub(r"[._]", "-", s).lower()


def _truncate_label(s: str) -> str:
    """Truncate to Kubernetes label value limit of 63 chars."""
    return s[:_K8S_LABEL_MAX]


def _base_build_args(
    key: BuildKey,
    flatcar_image_digest: str | None,
    git_revision: str,
    build_created: str,
    repo: str,
) -> list[str]:
    """Return the common build-args list (no kernel-related args)."""
    args = [
        f"--opt build-arg:FLATCAR_VERSION={key.flatcar}",
        f"--opt build-arg:DRIVER_VERSION={key.driver}",
        f"--opt build-arg:GIT_REVISION={git_revision}",
        f"--opt build-arg:BUILD_CREATED={build_created}",
    ]
    if flatcar_image_digest:
        base_repo = repo.split(":")[0] if ":" in repo else repo
        args.append(
            f"--opt build-arg:FLATCAR_IMAGE_REF={base_repo}@{flatcar_image_digest}"
        )
    return args


def _build_runtime_command(
    *,
    cache_flags: list[str],
    build_args: list[str],
    full_output_ref: str,
) -> str:
    """Assemble the shell command for a runtime build job."""
    lines = [
        "if [ -f /kaniko/.docker/config.json ]; then",
        "  export DOCKER_CONFIG=/kaniko/.docker",
        "fi",
        'export BUILDKITD_FLAGS="--oci-worker-no-process-sandbox"',
        "buildctl-daemonless.sh build \\",
        "  --progress=plain \\",
        "  --frontend=dockerfile.v0 \\",
        "  --local context=/workspace \\",
        "  --local dockerfile=/workspace \\",
    ]
    for flag in cache_flags:
        lines.append(f"  {flag} \\")
    for arg in build_args:
        lines.append(f"  {arg} \\")
    lines.append(f"  --output type=image,name={full_output_ref},push=true")
    return "\n".join(lines)


def _build_precompile_command(
    *,
    key: BuildKey,
    repo: str,
    cache_flags: list[str],
    flatcar_image_digest: str | None,
    git_revision: str,
    build_created: str,
) -> str:
    """Assemble the shell command for a precompile build job.

    The command uses a two-phase approach:
    1. A quick build targeting the ``kernel-discover`` stage to export the
       kernel version string discovered from /lib/modules inside the base image.
    2. A full build using that kernel version in the output image tag.

    This avoids passing KERNEL_VERSION_OVERRIDE from the operator — the build
    job discovers the kernel that is actually present in the base image.
    """
    base_args = _base_build_args(key, flatcar_image_digest, git_revision, build_created, repo)

    # Output tag: <driver>-<kernel>-flatcar<flatcar>, shell expands KERNEL_VERSION.
    full_output_ref = f"{repo}:{key.driver}-${{KERNEL_VERSION}}-flatcar{key.flatcar}" if repo else f"{key.driver}-${{KERNEL_VERSION}}-flatcar{key.flatcar}"

    lines = [
        "if [ -f /kaniko/.docker/config.json ]; then",
        "  export DOCKER_CONFIG=/kaniko/.docker",
        "fi",
        'export BUILDKITD_FLAGS="--oci-worker-no-process-sandbox"',
        "",
        "# Phase 1: discover kernel version from the flatcar-sources base image.",
        "buildctl-daemonless.sh build \\",
        "  --progress=plain \\",
        "  --frontend=dockerfile.v0 \\",
        "  --local context=/workspace \\",
        "  --local dockerfile=/workspace/prebuild \\",
        "  --opt target=kernel-discover-export \\",
        "  " + " \\\n  ".join(base_args) + " \\",
        "  --output type=local,dest=/tmp/kinfo",
        "",
        "KERNEL_VERSION=$(cat /tmp/kinfo/kernel_version)",
        'if [ -z "${KERNEL_VERSION}" ]; then',
        '  echo "ERROR: kernel version discovery returned empty string" >&2',
        "  exit 1",
        "fi",
        'echo "Discovered kernel version: ${KERNEL_VERSION}"',
        "",
        "# Phase 2: full precompile build with the discovered kernel in the output tag.",
        "buildctl-daemonless.sh build \\",
        "  --progress=plain \\",
        "  --frontend=dockerfile.v0 \\",
        "  --local context=/workspace \\",
        "  --local dockerfile=/workspace/prebuild \\",
    ]
    if cache_flags:
        for flag in cache_flags:
            lines.append(f"  {flag} \\")
    for arg in base_args:
        lines.append(f"  {arg} \\")
    lines.append(f"  --opt build-arg:KERNEL_VERSION=${{KERNEL_VERSION}} \\")
    lines.append(f"  --output type=image,name={full_output_ref},push=true")

    return "\n".join(lines)
