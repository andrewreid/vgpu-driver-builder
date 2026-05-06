"""Tests for poller.run_once."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from vgpu_driver_operator.flatcar import FlatcarFeedError
from vgpu_driver_operator import poller as _poller


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_vdi(name: str, channels: list[str], existing_tracked: list[dict] | None = None):
    """Build a minimal VGPUDriverImage dict."""
    return {
        "metadata": {"name": name, "uid": "uid-" + name},
        "spec": {
            "driverVersions": ["550.54.15"],
            "flatcar": {
                "trackChannels": channels,
                "arch": "amd64",
            },
            "source": {"type": "s3", "uriTemplate": "s3://bucket/${DRIVER_VERSION}.run"},
            "registry": {"repository": "registry.example.com/vgpu/drivers"},
        },
        "status": {
            "trackedChannelVersions": existing_tracked or [],
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunOnce:
    @patch("vgpu_driver_operator.poller._configure_k8s")
    @patch("vgpu_driver_operator.flatcar.latest_release")
    @patch("vgpu_driver_operator.flatcar.kernel_for_release")
    def test_success_patches_status(self, mock_kernel, mock_latest, mock_cfg):
        mock_latest.return_value = "4081.2.1"
        mock_kernel.return_value = "6.1.120"

        custom_api = MagicMock()
        custom_api.list_cluster_custom_object.return_value = {
            "items": [_make_vdi("my-vdi", ["stable", "lts"])]
        }

        rc = _poller.run_once(custom_api=custom_api)

        assert rc == 0
        # patch_cluster_custom_object_status called once for "my-vdi"
        custom_api.patch_cluster_custom_object_status.assert_called_once()
        call_args = custom_api.patch_cluster_custom_object_status.call_args
        # 5th positional arg is the body
        body = call_args[0][4]
        entries = body["status"]["trackedChannelVersions"]
        assert len(entries) == 2  # stable + lts
        channels_seen = {e["channel"] for e in entries}
        assert channels_seen == {"stable", "lts"}
        for e in entries:
            assert e["flatcarVersion"] == "4081.2.1"
            assert e["kernelVersion"] == "6.1.120"
            assert "observedAt" in e

    @patch("vgpu_driver_operator.poller._configure_k8s")
    @patch("vgpu_driver_operator.flatcar.latest_release")
    @patch("vgpu_driver_operator.flatcar.kernel_for_release")
    def test_partial_failure_returns_1_others_still_patched(
        self, mock_kernel, mock_latest, mock_cfg
    ):
        """If one channel fails, run_once returns 1 but still patches the others."""
        def _latest(channel, arch, *, session=None):
            if channel == "alpha":
                raise FlatcarFeedError("network error")
            return "4081.2.1"

        mock_latest.side_effect = _latest
        mock_kernel.return_value = "6.1.120"

        custom_api = MagicMock()
        custom_api.list_cluster_custom_object.return_value = {
            "items": [_make_vdi("my-vdi", ["stable", "alpha"])]
        }

        rc = _poller.run_once(custom_api=custom_api)

        assert rc == 1
        # Status should still be patched (with just the "stable" entry).
        custom_api.patch_cluster_custom_object_status.assert_called_once()
        call_args = custom_api.patch_cluster_custom_object_status.call_args
        body = call_args[0][4]
        entries = body["status"]["trackedChannelVersions"]
        channels_seen = {e["channel"] for e in entries}
        assert "stable" in channels_seen
        assert "alpha" not in channels_seen

    @patch("vgpu_driver_operator.poller._configure_k8s")
    def test_no_objects_returns_0(self, mock_cfg):
        custom_api = MagicMock()
        custom_api.list_cluster_custom_object.return_value = {"items": []}

        rc = _poller.run_once(custom_api=custom_api)
        assert rc == 0

    @patch("vgpu_driver_operator.poller._configure_k8s")
    def test_list_failure_returns_1(self, mock_cfg):
        custom_api = MagicMock()
        custom_api.list_cluster_custom_object.side_effect = Exception("api down")

        rc = _poller.run_once(custom_api=custom_api)
        assert rc == 1

    @patch("vgpu_driver_operator.poller._configure_k8s")
    @patch("vgpu_driver_operator.flatcar.latest_release")
    @patch("vgpu_driver_operator.flatcar.kernel_for_release")
    def test_no_channels_skips_object(self, mock_kernel, mock_latest, mock_cfg):
        custom_api = MagicMock()
        custom_api.list_cluster_custom_object.return_value = {
            "items": [_make_vdi("my-vdi", [])]
        }

        rc = _poller.run_once(custom_api=custom_api)
        assert rc == 0
        mock_latest.assert_not_called()
        custom_api.patch_cluster_custom_object_status.assert_not_called()

    @patch("vgpu_driver_operator.poller._configure_k8s")
    @patch("vgpu_driver_operator.flatcar.latest_release")
    @patch("vgpu_driver_operator.flatcar.kernel_for_release")
    def test_patch_failure_returns_1(self, mock_kernel, mock_latest, mock_cfg):
        """If patching status fails, run_once returns 1."""
        mock_latest.return_value = "4081.2.1"
        mock_kernel.return_value = "6.1.120"

        custom_api = MagicMock()
        custom_api.list_cluster_custom_object.return_value = {
            "items": [_make_vdi("my-vdi", ["stable"])]
        }
        custom_api.patch_cluster_custom_object_status.side_effect = Exception("k8s down")

        rc = _poller.run_once(custom_api=custom_api)
        assert rc == 1

    @patch("vgpu_driver_operator.poller._configure_k8s")
    @patch("vgpu_driver_operator.flatcar.latest_release")
    @patch("vgpu_driver_operator.flatcar.kernel_for_release")
    def test_merges_with_existing_tracked_entries(
        self, mock_kernel, mock_latest, mock_cfg
    ):
        """New channel entry should merge with pre-existing entries for other channels."""
        mock_latest.return_value = "4081.2.2"
        mock_kernel.return_value = "6.1.121"

        existing = [
            {
                "channel": "lts",
                "flatcarVersion": "3815.2.0",
                "kernelVersion": "5.15.99",
                "observedAt": "2024-01-01T00:00:00+00:00",
            }
        ]
        custom_api = MagicMock()
        custom_api.list_cluster_custom_object.return_value = {
            "items": [_make_vdi("my-vdi", ["stable"], existing_tracked=existing)]
        }

        rc = _poller.run_once(custom_api=custom_api)
        assert rc == 0

        call_args = custom_api.patch_cluster_custom_object_status.call_args
        body = call_args[0][4]
        entries = body["status"]["trackedChannelVersions"]
        by_channel = {e["channel"]: e for e in entries}
        # stable updated.
        assert by_channel["stable"]["flatcarVersion"] == "4081.2.2"
        # lts preserved from existing.
        assert by_channel["lts"]["flatcarVersion"] == "3815.2.0"
