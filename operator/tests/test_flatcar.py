"""Tests for vgpu_driver_operator.flatcar."""

import pytest
import responses as rsps_lib

from vgpu_driver_operator.flatcar import (
    FlatcarFeedError,
    flatcar_version_from_node,
    latest_release,
    parse_flatcar_version_from_os_image,
)

# ---------------------------------------------------------------------------
# parse_flatcar_version_from_os_image
# ---------------------------------------------------------------------------


class TestParseOsImage:
    def test_standard_flatcar_4230(self):
        result = parse_flatcar_version_from_os_image(
            "Flatcar Container Linux by Kinvolk 4230.2.3 (Oklo)"
        )
        assert result == "4230.2.3"

    def test_standard_flatcar_3815(self):
        result = parse_flatcar_version_from_os_image(
            "Flatcar Container Linux by Kinvolk 3815.2.0 (Oklo)"
        )
        assert result == "3815.2.0"

    def test_standard_flatcar_4081(self):
        result = parse_flatcar_version_from_os_image(
            "Flatcar Container Linux by Kinvolk 4081.2.0 (Oklo)"
        )
        assert result == "4081.2.0"

    def test_ubuntu_returns_none(self):
        result = parse_flatcar_version_from_os_image(
            "Ubuntu 22.04.3 LTS"
        )
        assert result is None

    def test_rhcos_returns_none(self):
        result = parse_flatcar_version_from_os_image(
            "Red Hat Enterprise Linux CoreOS 414.92.202310170514-0"
        )
        assert result is None

    def test_empty_string_returns_none(self):
        assert parse_flatcar_version_from_os_image("") is None

    def test_garbage_returns_none(self):
        assert parse_flatcar_version_from_os_image("not-an-os-string") is None


# ---------------------------------------------------------------------------
# flatcar_version_from_node
# ---------------------------------------------------------------------------

_NFD_OS_LABEL = "feature.node.kubernetes.io/system-os_release.VERSION_ID"


def _node(labels=None, os_image=""):
    return {
        "metadata": {"labels": labels or {}},
        "status": {
            "nodeInfo": {
                "osImage": os_image,
            }
        },
    }


class TestFlatcarVersionFromNode:
    def test_nfd_label_wins(self):
        node = _node(
            labels={_NFD_OS_LABEL: "4230.2.3"},
            os_image="Flatcar Container Linux by Kinvolk 4000.0.0 (Old)",
        )
        assert flatcar_version_from_node(node) == "4230.2.3"

    def test_fallback_to_os_image(self):
        node = _node(os_image="Flatcar Container Linux by Kinvolk 4230.2.3 (Oklo)")
        assert flatcar_version_from_node(node) == "4230.2.3"

    def test_none_when_ubuntu_and_no_label(self):
        node = _node(os_image="Ubuntu 22.04.3 LTS")
        assert flatcar_version_from_node(node) is None

    def test_none_when_no_info(self):
        node = _node()
        assert flatcar_version_from_node(node) is None

    def test_empty_label_falls_back(self):
        node = _node(
            labels={_NFD_OS_LABEL: ""},
            os_image="Flatcar Container Linux by Kinvolk 4081.2.0 (Oklo)",
        )
        assert flatcar_version_from_node(node) == "4081.2.0"

    def test_debian_nfd_label_not_mistaken_for_flatcar(self):
        """Debian 12 node: NFD sets VERSION_ID='12'. Must return None."""
        node = _node(
            labels={_NFD_OS_LABEL: "12"},
            os_image="Debian GNU/Linux 12 (bookworm)",
        )
        assert flatcar_version_from_node(node) is None

    def test_k3s_debian_node_returns_none(self):
        """k3s node running Debian: no Flatcar label, non-Flatcar osImage."""
        node = _node(os_image="Debian GNU/Linux 12 (bookworm)")
        assert flatcar_version_from_node(node) is None

    def test_nfd_triple_label_accepted_when_os_image_is_flatcar(self):
        """NFD label with X.Y.Z format on a Flatcar node is accepted."""
        node = _node(
            labels={_NFD_OS_LABEL: "4593.2.0"},
            os_image="Flatcar Container Linux by Kinvolk 4593.2.0 (Oklo)",
        )
        assert flatcar_version_from_node(node) == "4593.2.0"

    def test_nfd_triple_label_with_no_os_image_accepted(self):
        """NFD label with X.Y.Z format and no osImage is accepted (pure NFD node)."""
        node = _node(
            labels={_NFD_OS_LABEL: "4593.2.0"},
            os_image="",
        )
        assert flatcar_version_from_node(node) == "4593.2.0"


# ---------------------------------------------------------------------------
# latest_release (HTTP-stubbed with responses library)
# ---------------------------------------------------------------------------

STABLE_VERSION_TXT = (
    "FLATCAR_VERSION=4230.2.3\n"
    "FLATCAR_BUILD_ARCH=amd64\n"
)


class TestLatestRelease:
    @rsps_lib.activate
    def test_happy_path(self):
        rsps_lib.add(
            rsps_lib.GET,
            "https://stable.release.flatcar-linux.net/amd64-usr/current/version.txt",
            body=STABLE_VERSION_TXT,
            status=200,
        )
        assert latest_release("stable") == "4230.2.3"

    @rsps_lib.activate
    def test_404_raises(self):
        rsps_lib.add(
            rsps_lib.GET,
            "https://stable.release.flatcar-linux.net/amd64-usr/current/version.txt",
            status=404,
        )
        with pytest.raises(FlatcarFeedError):
            latest_release("stable")

    @rsps_lib.activate
    def test_malformed_body_raises(self):
        rsps_lib.add(
            rsps_lib.GET,
            "https://stable.release.flatcar-linux.net/amd64-usr/current/version.txt",
            body="THIS=GARBAGE\n",
            status=200,
        )
        with pytest.raises(FlatcarFeedError, match="FLATCAR_VERSION"):
            latest_release("stable")

    @rsps_lib.activate
    def test_arm64_arch(self):
        rsps_lib.add(
            rsps_lib.GET,
            "https://stable.release.flatcar-linux.net/arm64-usr/current/version.txt",
            body="FLATCAR_VERSION=4230.2.3\n",
            status=200,
        )
        assert latest_release("stable", "arm64") == "4230.2.3"


# ---------------------------------------------------------------------------
# Integration: V1Node serialization shape (regression for snake_case bug)
# ---------------------------------------------------------------------------


class TestFlatcarVersionFromNodeIntegration:
    """V1Node objects from kubernetes client must be sanitized to API-shape
    JSON (camelCase) before flatcar_version_from_node() can parse them.

    Regression: a real Flatcar RKE2 cluster reported NoFlatcarVersions for
    every node because main.py was calling V1Node.to_dict(), which emits
    snake_case (os_image / node_info), not camelCase (osImage / nodeInfo)
    that the parser expects.
    """

    def _v1_flatcar_node(self):
        from kubernetes import client

        return client.V1Node(
            metadata=client.V1ObjectMeta(name="gpu-01", labels={}),
            status=client.V1NodeStatus(
                node_info=client.V1NodeSystemInfo(
                    architecture="amd64",
                    boot_id="bid",
                    container_runtime_version="containerd://2.2.3",
                    kernel_version="6.12.81-flatcar",
                    kube_proxy_version="v1.35.4",
                    kubelet_version="v1.35.4",
                    machine_id="mid",
                    operating_system="linux",
                    os_image="Flatcar Container Linux by Kinvolk 4593.2.0 (Oklo)",
                    system_uuid="uuid",
                )
            ),
        )

    def test_sanitize_for_serialization_parses(self):
        from kubernetes import client

        node = self._v1_flatcar_node()
        sanitized = client.ApiClient().sanitize_for_serialization(node)
        assert flatcar_version_from_node(sanitized) == "4593.2.0"

    def test_to_dict_does_not_parse(self):
        """Permanent guard: anyone who regresses to V1Node.to_dict() will
        see this assertion fail."""
        node = self._v1_flatcar_node()
        snake = node.to_dict()
        assert flatcar_version_from_node(snake) is None
