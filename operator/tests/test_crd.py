"""Tests for crd.py helpers."""

from __future__ import annotations

import base64
import os
from unittest.mock import MagicMock, patch

import pytest

from vgpu_driver_operator import crd as _crd


# ---------------------------------------------------------------------------
# make_owner_reference
# ---------------------------------------------------------------------------


class TestMakeOwnerReference:
    def test_shape(self):
        crd_obj = {
            "metadata": {
                "name": "my-vdi",
                "uid": "abc-123",
            }
        }
        ref = _crd.make_owner_reference(crd_obj)
        assert ref["apiVersion"] == "vgpu.flatcar.io/v1alpha1"
        assert ref["kind"] == "VGPUDriverImage"
        assert ref["name"] == "my-vdi"
        assert ref["uid"] == "abc-123"
        assert ref["controller"] is True
        assert ref["blockOwnerDeletion"] is True

    def test_empty_metadata(self):
        ref = _crd.make_owner_reference({})
        assert ref["name"] == ""
        assert ref["uid"] == ""


# ---------------------------------------------------------------------------
# operator_namespace
# ---------------------------------------------------------------------------


class TestOperatorNamespace:
    def test_env_var_takes_priority(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPERATOR_NAMESPACE", "from-env")
        # Even if the file exists, env wins.
        ns_file = tmp_path / "namespace"
        ns_file.write_text("from-file")
        with patch.object(_crd, "_NAMESPACE_FILE", str(ns_file)):
            assert _crd.operator_namespace() == "from-env"

    def test_file_used_when_no_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPERATOR_NAMESPACE", raising=False)
        ns_file = tmp_path / "namespace"
        ns_file.write_text("from-file")
        with patch.object(_crd, "_NAMESPACE_FILE", str(ns_file)):
            assert _crd.operator_namespace() == "from-file"

    def test_default_when_neither(self, monkeypatch):
        monkeypatch.delenv("OPERATOR_NAMESPACE", raising=False)
        # Point at a non-existent file.
        with patch.object(_crd, "_NAMESPACE_FILE", "/nonexistent/path/namespace"):
            assert _crd.operator_namespace() == "vgpu-driver-operator"


# ---------------------------------------------------------------------------
# get_secret
# ---------------------------------------------------------------------------


class TestGetSecret:
    def test_decodes_base64_values(self):
        raw_value = base64.b64encode(b"supersecret").decode()
        mock_secret = MagicMock()
        mock_secret.data = {".dockerconfigjson": raw_value}

        core_api = MagicMock()
        core_api.read_namespaced_secret.return_value = mock_secret

        result = _crd.get_secret(core_api, "default", "my-secret")
        assert result[".dockerconfigjson"] == b"supersecret"

    def test_empty_data_returns_empty_dict(self):
        mock_secret = MagicMock()
        mock_secret.data = None

        core_api = MagicMock()
        core_api.read_namespaced_secret.return_value = mock_secret

        result = _crd.get_secret(core_api, "default", "my-secret")
        assert result == {}


# ---------------------------------------------------------------------------
# list_vgpu_driver_images
# ---------------------------------------------------------------------------


class TestListVgpuDriverImages:
    def test_returns_items(self):
        api = MagicMock()
        api.list_cluster_custom_object.return_value = {
            "items": [{"metadata": {"name": "vdi-1"}}, {"metadata": {"name": "vdi-2"}}]
        }
        result = _crd.list_vgpu_driver_images(api)
        assert len(result) == 2
        api.list_cluster_custom_object.assert_called_once_with(
            _crd.GROUP, _crd.VERSION, _crd.PLURAL
        )

    def test_empty_items_key(self):
        api = MagicMock()
        api.list_cluster_custom_object.return_value = {}
        result = _crd.list_vgpu_driver_images(api)
        assert result == []


# ---------------------------------------------------------------------------
# patch_status
# ---------------------------------------------------------------------------


class TestPatchStatus:
    def test_calls_status_subresource(self):
        api = MagicMock()
        _crd.patch_status(api, "my-vdi", {"builds": []})
        api.patch_cluster_custom_object_status.assert_called_once_with(
            _crd.GROUP,
            _crd.VERSION,
            _crd.PLURAL,
            "my-vdi",
            {"status": {"builds": []}},
        )


# ---------------------------------------------------------------------------
# list_owned_jobs
# ---------------------------------------------------------------------------


class TestListOwnedJobs:
    def _make_job(self, name: str, owner_uid: str | None):
        job = MagicMock()
        job.metadata.name = name
        job.metadata.labels = {"app": "vgpu-driver-builder"}
        if owner_uid:
            ref = MagicMock()
            ref.uid = owner_uid
            job.metadata.owner_references = [ref]
        else:
            job.metadata.owner_references = []
        job.to_dict.return_value = {"metadata": {"name": name}}
        return job

    def test_filters_by_owner_uid(self):
        j1 = self._make_job("job-1", "uid-abc")
        j2 = self._make_job("job-2", "uid-xyz")
        j3 = self._make_job("job-3", "uid-abc")

        batch_api = MagicMock()
        batch_api.list_namespaced_job.return_value = MagicMock(items=[j1, j2, j3])

        result = _crd.list_owned_jobs(batch_api, "default", "uid-abc")
        assert len(result) == 2

    def test_no_matching_jobs(self):
        j1 = self._make_job("job-1", "uid-xyz")

        batch_api = MagicMock()
        batch_api.list_namespaced_job.return_value = MagicMock(items=[j1])

        result = _crd.list_owned_jobs(batch_api, "default", "uid-abc")
        assert result == []
