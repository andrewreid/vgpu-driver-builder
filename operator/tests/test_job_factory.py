"""Tests for vgpu_driver_operator.job_factory."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vgpu_driver_operator.job_factory import build_job_manifest, build_job_name
from vgpu_driver_operator.reconciler import BuildKey

# ---------------------------------------------------------------------------
# Snapshot helper
# ---------------------------------------------------------------------------

_SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


def _snapshot_assert(name: str, manifest: dict) -> None:
    """Compare *manifest* to a stored snapshot, creating it on first run.

    Set ``UPDATE_SNAPSHOTS=1`` to regenerate snapshots.
    """
    _SNAPSHOT_DIR.mkdir(exist_ok=True)
    snapshot_path = _SNAPSHOT_DIR / f"{name}.json"
    serialized = json.dumps(manifest, sort_keys=True, indent=2)

    if os.environ.get("UPDATE_SNAPSHOTS") == "1" or not snapshot_path.exists():
        snapshot_path.write_text(serialized)
        return  # First run: create the snapshot.

    expected = snapshot_path.read_text()
    assert serialized == expected, (
        f"Snapshot mismatch for {name!r}. "
        "Run with UPDATE_SNAPSHOTS=1 to regenerate."
    )


# ---------------------------------------------------------------------------
# Shared spec fixture
# ---------------------------------------------------------------------------

_SPEC = {
    "driverVersions": ["550.54.15"],
    "source": {
        "type": "s3",
        "uriTemplate": "s3://my-bucket/drivers/NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run",
    },
    "registry": {
        "repository": "registry.example.com/vgpu-driver",
        "repositoryPrecompiled": "registry.example.com/vgpu-driver-prebuilt",
        "cacheRepository": "registry.example.com/cache/vgpu-driver",
    },
    "build": {
        "buildkitImage": "moby/buildkit:rootless",
    },
}

_RUNTIME_KEY = BuildKey(driver="550.54.15", flatcar="4230.2.3", precompile=False)
_PRECOMPILE_KEY = BuildKey(driver="550.54.15", flatcar="4230.2.3", precompile=True)

_COMMON_KWARGS = dict(
    crd_namespace="vgpu-system",
    crd_name="my-vdi",
    crd_uid="abc-123-uid",
    s3_secret_name="s3-driver-storage-secret",
    registry_secret_name="private-registry-secret",
    dockerfile_configmap="driver-dockerfile-cm",
    buildfiles_configmap="driver-build-files",
    git_revision="deadbeef",
    build_created="2025-01-10T12:00:00Z",
)


# ---------------------------------------------------------------------------
# Snapshot tests
# ---------------------------------------------------------------------------


class TestSnapshots:
    def test_runtime_snapshot(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        _snapshot_assert("runtime_job", manifest)

    def test_precompile_snapshot(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_PRECOMPILE_KEY,
            **_COMMON_KWARGS,
        )
        _snapshot_assert("precompile_job", manifest)


# ---------------------------------------------------------------------------
# Job name length tests
# ---------------------------------------------------------------------------


class TestBuildJobName:
    def test_name_fits_63_chars_runtime(self):
        name = build_job_name(
            BuildKey("550.54.15", "4230.2.3", False),
            "abc123def456",
        )
        assert len(name) <= 63

    def test_name_fits_63_chars_precompile(self):
        name = build_job_name(
            BuildKey("550.54.15", "4230.2.3", True),
            "abc123def456",
        )
        assert len(name) <= 63

    def test_long_driver_version_truncated(self):
        long_driver = "550.54.15.very.long.version.string.that.should.be.truncated"
        name = build_job_name(
            BuildKey(long_driver, "4230.2.3", False),
            "abc123def456",
        )
        assert len(name) <= 63

    def test_hash_always_present_at_end(self):
        name = build_job_name(
            BuildKey("550.54.15", "4230.2.3", False),
            "deadbeef1234",
        )
        assert name.endswith("deadbe")

    @pytest.mark.parametrize(
        "driver,flatcar,precompile",
        [
            ("550.54.15", "4230.2.3", False),
            ("535.183.01", "3815.2.0", False),
            ("520.0.0", "4081.2.0", False),
            ("550.54.15", "4230.2.3", True),
            ("535.183.01", "3815.2.0", True),
        ],
    )
    def test_reasonable_inputs(self, driver, flatcar, precompile):
        key = BuildKey(driver=driver, flatcar=flatcar, precompile=precompile)
        name = build_job_name(key, "abc123def456")
        assert len(name) <= 63, f"Name too long: {name!r}"
        assert name == name.lower()


# ---------------------------------------------------------------------------
# Owner reference
# ---------------------------------------------------------------------------


class TestOwnerReference:
    def test_owner_ref_populated(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        refs = manifest["metadata"]["ownerReferences"]
        assert len(refs) == 1
        ref = refs[0]
        assert ref["apiVersion"] == "vgpu.flatcar.io/v1alpha1"
        assert ref["kind"] == "VGPUDriverImage"
        assert ref["name"] == "my-vdi"
        assert ref["uid"] == "abc-123-uid"
        assert ref["controller"] is True
        assert ref["blockOwnerDeletion"] is True

    def test_owner_ref_namespace(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        assert manifest["metadata"]["namespace"] == "vgpu-system"


# ---------------------------------------------------------------------------
# Build-arg presence tests
# ---------------------------------------------------------------------------


def _get_main_command(manifest: dict) -> str:
    return manifest["spec"]["template"]["spec"]["containers"][0]["command"][2]


class TestBuildArgs:
    def test_runtime_no_kernel_version_override(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        cmd = _get_main_command(manifest)
        assert "KERNEL_VERSION_OVERRIDE" not in cmd

    def test_precompile_no_kernel_version_override(self):
        """Precompile job must NOT pass KERNEL_VERSION_OVERRIDE — kernel is
        discovered from the base image at build time."""
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_PRECOMPILE_KEY,
            **_COMMON_KWARGS,
        )
        cmd = _get_main_command(manifest)
        assert "KERNEL_VERSION_OVERRIDE" not in cmd

    def test_precompile_has_kernel_discover_phase(self):
        """Precompile job command must include kernel discovery logic."""
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_PRECOMPILE_KEY,
            **_COMMON_KWARGS,
        )
        cmd = _get_main_command(manifest)
        assert "kernel-discover" in cmd
        assert "KERNEL_VERSION" in cmd
        assert "/tmp/kinfo" in cmd

    def test_precompile_output_tag_uses_discovered_kernel(self):
        """Precompile output tag must use the shell variable $KERNEL_VERSION."""
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_PRECOMPILE_KEY,
            **_COMMON_KWARGS,
        )
        cmd = _get_main_command(manifest)
        assert "${KERNEL_VERSION}" in cmd
        assert "flatcar4230.2.3" in cmd
        assert "550.54.15" in cmd

    def test_flatcar_image_ref_when_digest_provided(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            flatcar_image_digest="sha256:abcdef123456",
            **_COMMON_KWARGS,
        )
        cmd = _get_main_command(manifest)
        assert "FLATCAR_IMAGE_REF" in cmd
        assert "sha256:abcdef123456" in cmd

    def test_no_flatcar_image_ref_when_no_digest(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        cmd = _get_main_command(manifest)
        assert "FLATCAR_IMAGE_REF" not in cmd

    def test_git_revision_in_build_args(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        cmd = _get_main_command(manifest)
        assert "GIT_REVISION=deadbeef" in cmd

    def test_build_created_in_build_args(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        cmd = _get_main_command(manifest)
        assert "BUILD_CREATED=2025-01-10T12:00:00Z" in cmd


# ---------------------------------------------------------------------------
# Registry secret volume
# ---------------------------------------------------------------------------


def _get_volumes(manifest: dict) -> list[dict]:
    return manifest["spec"]["template"]["spec"]["volumes"]


def _get_volume_mounts(manifest: dict) -> list[dict]:
    return manifest["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]


class TestRegistrySecretVolume:
    def test_no_registry_secret_when_anonymous(self):
        """Bug 6/8: anonymous-registry CR (no authSecretRef, no env var) must
        produce no registry-secret volume or volumeMount."""
        kwargs = {**_COMMON_KWARGS, "registry_secret_name": None}
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **kwargs,
        )
        volume_names = [v["name"] for v in _get_volumes(manifest)]
        assert "docker-config" not in volume_names
        mount_names = [m["name"] for m in _get_volume_mounts(manifest)]
        assert "docker-config" not in mount_names

    def test_registry_secret_mounted_when_provided(self):
        """Bug 6/8: when registry_secret_name is set, volume + mount must appear."""
        kwargs = {**_COMMON_KWARGS, "registry_secret_name": "my-secret"}
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **kwargs,
        )
        volume_names = [v["name"] for v in _get_volumes(manifest)]
        assert "docker-config" in volume_names
        secret_vol = next(v for v in _get_volumes(manifest) if v["name"] == "docker-config")
        assert secret_vol["secret"]["secretName"] == "my-secret"
        mount_names = [m["name"] for m in _get_volume_mounts(manifest)]
        assert "docker-config" in mount_names

    def test_volume_present_when_secret_provided(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        volume_names = [v["name"] for v in _get_volumes(manifest)]
        assert "docker-config" in volume_names
        mount_paths = [m["mountPath"] for m in _get_volume_mounts(manifest)]
        assert "/kaniko/.docker/" in mount_paths

    def test_volume_absent_when_no_secret(self):
        kwargs = {**_COMMON_KWARGS, "registry_secret_name": None}
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **kwargs,
        )
        volume_names = [v["name"] for v in _get_volumes(manifest)]
        assert "docker-config" not in volume_names
        mount_paths = [m["mountPath"] for m in _get_volume_mounts(manifest)]
        assert "/kaniko/.docker/" not in mount_paths


# ---------------------------------------------------------------------------
# Basic structure tests
# ---------------------------------------------------------------------------


class TestManifestStructure:
    def test_api_version_and_kind(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        assert manifest["apiVersion"] == "batch/v1"
        assert manifest["kind"] == "Job"

    def test_backoff_limit_zero(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        assert manifest["spec"]["backoffLimit"] == 0

    def test_restart_policy_never(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        pod_spec = manifest["spec"]["template"]["spec"]
        assert pod_spec["restartPolicy"] == "Never"

    def test_dns_config_ndots_1(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        opts = manifest["spec"]["template"]["spec"]["dnsConfig"]["options"]
        assert any(o["name"] == "ndots" and o["value"] == "1" for o in opts)

    def test_labels_present(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        labels = manifest["metadata"]["labels"]
        assert labels["app"] == "vgpu-driver-builder"
        assert labels["app.kubernetes.io/component"] == "builder"
        assert labels["vgpu.flatcar.io/driver-version"] == "550.54.15"
        assert labels["vgpu.flatcar.io/flatcar-version"] == "4230.2.3"
        assert labels["vgpu.flatcar.io/mode"] == "runtime"
        pod_labels = manifest["spec"]["template"]["metadata"]["labels"]
        assert pod_labels["app.kubernetes.io/component"] == "builder"

    def test_precompile_mode_label(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_PRECOMPILE_KEY,
            **_COMMON_KWARGS,
        )
        assert manifest["metadata"]["labels"]["vgpu.flatcar.io/mode"] == "precompiled"

    def test_init_container_present(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        init_containers = manifest["spec"]["template"]["spec"]["initContainers"]
        assert len(init_containers) == 1
        assert init_containers[0]["name"] == "fetch-driver"
        assert init_containers[0]["image"] == "amazon/aws-cli:2.13.0"

    def test_runtime_output_tag(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        cmd = _get_main_command(manifest)
        assert "registry.example.com/vgpu-driver:550.54.15-flatcar4230.2.3" in cmd

    def test_s3_secret_env_vars_in_init_container(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        init_env = manifest["spec"]["template"]["spec"]["initContainers"][0]["env"]
        env_names = [e["name"] for e in init_env]
        assert "S3_ENDPOINT_URL" in env_names
        assert "AWS_ACCESS_KEY_ID" in env_names
        assert "AWS_SECRET_ACCESS_KEY" in env_names

    def test_uri_template_substituted(self):
        manifest = build_job_manifest(
            spec=_SPEC,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        init_env = manifest["spec"]["template"]["spec"]["initContainers"][0]["env"]
        s3_uri_env = next(e for e in init_env if e["name"] == "VGPU_DRIVER_S3_URI")
        assert "550.54.15" in s3_uri_env["value"]
        assert "${DRIVER_VERSION}" not in s3_uri_env["value"]

    def test_custom_buildkit_image(self):
        spec = {**_SPEC, "build": {"buildkitImage": "custom/buildkit:v0.13"}}
        manifest = build_job_manifest(
            spec=spec,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "custom/buildkit:v0.13"

    def test_resources_included_when_spec_has_them(self):
        spec = {
            **_SPEC,
            "build": {
                "resources": {
                    "requests": {"cpu": "2", "memory": "4Gi"},
                    "limits": {"cpu": "4", "memory": "8Gi"},
                }
            },
        }
        manifest = build_job_manifest(
            spec=spec,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert "resources" in container
        assert container["resources"]["requests"]["cpu"] == "2"


# ---------------------------------------------------------------------------
# URI template placeholder tests
# ---------------------------------------------------------------------------


class TestURITemplatePlaceholders:
    def test_dollar_brace_placeholder(self):
        """Test ${DRIVER_VERSION} placeholder substitution."""
        spec = {
            **_SPEC,
            "source": {
                "type": "s3",
                "uriTemplate": "s3://bucket/drivers/NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run",
            },
        }
        manifest = build_job_manifest(
            spec=spec,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        init_env = manifest["spec"]["template"]["spec"]["initContainers"][0]["env"]
        s3_uri_env = next(e for e in init_env if e["name"] == "VGPU_DRIVER_S3_URI")
        assert s3_uri_env["value"] == "s3://bucket/drivers/NVIDIA-Linux-x86_64-550.54.15.run"

    def test_format_style_placeholder(self):
        """Test {driverVersion} placeholder substitution (Python str.format style)."""
        spec = {
            **_SPEC,
            "source": {
                "type": "s3",
                "uriTemplate": "s3://bucket/drivers/NVIDIA-Linux-x86_64-{driverVersion}.run",
            },
        }
        manifest = build_job_manifest(
            spec=spec,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        init_env = manifest["spec"]["template"]["spec"]["initContainers"][0]["env"]
        s3_uri_env = next(e for e in init_env if e["name"] == "VGPU_DRIVER_S3_URI")
        assert s3_uri_env["value"] == "s3://bucket/drivers/NVIDIA-Linux-x86_64-550.54.15.run"

    def test_both_placeholders_in_template(self):
        """Test that both ${DRIVER_VERSION} and {driverVersion} can appear together."""
        spec = {
            **_SPEC,
            "source": {
                "type": "s3",
                "uriTemplate": "s3://bucket/${DRIVER_VERSION}/{driverVersion}/driver.run",
            },
        }
        manifest = build_job_manifest(
            spec=spec,
            key=_RUNTIME_KEY,
            **_COMMON_KWARGS,
        )
        init_env = manifest["spec"]["template"]["spec"]["initContainers"][0]["env"]
        s3_uri_env = next(e for e in init_env if e["name"] == "VGPU_DRIVER_S3_URI")
        assert s3_uri_env["value"] == "s3://bucket/550.54.15/550.54.15/driver.run"
