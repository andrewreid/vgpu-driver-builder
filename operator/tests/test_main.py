"""Tests for vgpu_driver_operator.main handlers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from vgpu_driver_operator import main as _main
from vgpu_driver_operator.registry import RegistryUnreachable


def _base_spec() -> dict:
    return {
        "driverVersions": ["550.54.15"],
        "flatcar": {"discoverFromNodes": False, "versions": ["4593.2.0"]},
        "source": {
            "type": "s3",
            "uriTemplate": "s3://bucket/${DRIVER_VERSION}.run",
        },
        "registry": {"repository": "registry.example.com/vgpu/drivers"},
    }


def test_reconcile_registry_unreachable_sets_condition(monkeypatch):
    monkeypatch.setattr(_main, "_load_k8s_config", lambda: None)
    monkeypatch.setattr(_main._crd, "operator_namespace", lambda: "vgpu-driver-operator")
    monkeypatch.setattr(_main.client, "CoreV1Api", lambda: MagicMock())
    monkeypatch.setattr(_main.client, "BatchV1Api", lambda: MagicMock())
    monkeypatch.setattr(_main.client, "CustomObjectsApi", lambda: MagicMock())
    monkeypatch.setattr(
        _main._registry,
        "list_tags",
        MagicMock(side_effect=RegistryUnreachable("registry.example.com/vgpu/drivers")),
    )

    patch = SimpleNamespace(status={})

    _main._do_reconcile(
        spec=_base_spec(),
        name="my-vdi",
        status={},
        patch=patch,  # type: ignore[arg-type]
        body={"metadata": {"uid": "uid-1", "generation": 3}},  # type: ignore[arg-type]
        logger=MagicMock(),
    )

    condition = patch.status["conditions"][0]
    assert condition["status"] == "False"
    assert condition["reason"] == "RegistryUnreachable"
    assert "registry.example.com/vgpu/drivers" in condition["message"]
    assert "builds" not in patch.status
