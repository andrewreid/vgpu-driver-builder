"""Tests for vgpu_driver_operator.main handlers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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


def test_kopf_login_prefers_service_account(monkeypatch):
    connection = SimpleNamespace(token="secret-token")
    login_with_service_account = MagicMock(return_value=connection)
    login_via_client = MagicMock()
    monkeypatch.setattr(
        _main.kopf, "login_with_service_account", login_with_service_account
    )
    monkeypatch.setattr(_main.kopf, "login_via_client", login_via_client)

    logger = MagicMock()
    settings = MagicMock()
    result = _main.login_via_kubernetes_client(
        logger=logger,
        settings=settings,
        extra="passed-through",
    )

    assert result is connection
    logger.debug.assert_called_once_with("Using service account credentials for Kopf login")
    assert "secret-token" not in str(logger.debug.call_args)
    login_with_service_account.assert_called_once_with(
        logger=logger,
        settings=settings,
        extra="passed-through",
    )
    login_via_client.assert_not_called()


def test_kopf_login_falls_back_to_client_login(monkeypatch):
    connection = object()
    login_with_service_account = MagicMock(return_value=None)
    login_via_client = MagicMock(return_value=connection)
    monkeypatch.setattr(
        _main.kopf, "login_with_service_account", login_with_service_account
    )
    monkeypatch.setattr(_main.kopf, "login_via_client", login_via_client)

    logger = MagicMock()
    settings = MagicMock()
    result = _main.login_via_kubernetes_client(
        logger=logger,
        settings=settings,
        extra="passed-through",
    )

    assert result is connection
    logger.debug.assert_called_once_with(
        "Service account credentials unavailable; delegating Kopf login to client"
    )
    login_with_service_account.assert_called_once_with(
        logger=logger,
        settings=settings,
        extra="passed-through",
    )
    login_via_client.assert_called_once_with(
        logger=logger,
        settings=settings,
        extra="passed-through",
    )


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


def test_reconcile_happy_path_emits_info_logs(monkeypatch):
    logger = MagicMock()
    patch = SimpleNamespace(status={})

    monkeypatch.setattr(_main, "_load_k8s_config", lambda: None)
    monkeypatch.setattr(_main._crd, "operator_namespace", lambda: "vgpu-driver-operator")
    monkeypatch.setattr(_main.client, "CoreV1Api", lambda: MagicMock())
    monkeypatch.setattr(_main.client, "BatchV1Api", lambda: MagicMock())
    monkeypatch.setattr(_main.client, "CustomObjectsApi", lambda: MagicMock())
    monkeypatch.setattr(_main._crd, "list_owned_jobs", MagicMock(return_value=[]))
    monkeypatch.setattr(
        _main._registry,
        "list_tags",
        MagicMock(return_value={"550.54.15-flatcar4593.2.0"}),
    )
    monkeypatch.setattr(_main.kopf, "event", MagicMock())

    _main._do_reconcile(
        spec=_base_spec(),
        name="my-vdi",
        status={},
        patch=patch,  # type: ignore[arg-type]
        body={"metadata": {"uid": "uid-1", "generation": 3}},  # type: ignore[arg-type]
        logger=logger,
    )

    info_messages = [call.args[0] % call.args[1:] for call in logger.info.call_args_list]
    assert "reconcile: starting my-vdi generation=3" in info_messages
    assert any("discovered 0 flatcar version(s): []" in msg for msg in info_messages)
    assert any(
        "tag 550.54.15-flatcar4593.2.0 already in registry, skipping" in msg
        for msg in info_messages
    )
    assert any("patched status for my-vdi" in msg for msg in info_messages)
    assert "reconcile complete: 1 ready, 0 building, 0 pending, 0 failed" in info_messages


def test_reconcile_pending_build_emits_normal_event(monkeypatch):
    patch = SimpleNamespace(status={})
    batch_api = MagicMock()
    event_mock = MagicMock()

    monkeypatch.setattr(_main, "_load_k8s_config", lambda: None)
    monkeypatch.setattr(_main._crd, "operator_namespace", lambda: "vgpu-driver-operator")
    monkeypatch.setattr(_main.client, "CoreV1Api", lambda: MagicMock())
    monkeypatch.setattr(_main.client, "BatchV1Api", lambda: batch_api)
    monkeypatch.setattr(_main.client, "CustomObjectsApi", lambda: MagicMock())
    monkeypatch.setattr(_main._crd, "list_owned_jobs", MagicMock(return_value=[]))
    monkeypatch.setattr(_main._registry, "list_tags", MagicMock(return_value=set()))
    monkeypatch.setattr(_main.kopf, "event", event_mock)

    _main._do_reconcile(
        spec=_base_spec(),
        name="my-vdi",
        status={},
        patch=patch,  # type: ignore[arg-type]
        body={"metadata": {"uid": "uid-1", "generation": 3}},  # type: ignore[arg-type]
        logger=MagicMock(),
    )

    assert patch.status["conditions"][0]["reason"] == "BuildsPending"
    assert event_mock.call_args.kwargs["type"] == "Normal"
    assert event_mock.call_args.kwargs["reason"] == "Reconciled"


def test_reconcile_ignores_registry_secret_env_without_cr_auth_ref(monkeypatch):
    patch = SimpleNamespace(status={})
    batch_api = MagicMock()

    monkeypatch.setenv("REGISTRY_SECRET_NAME", "private-registry-secret")
    monkeypatch.setattr(_main, "_load_k8s_config", lambda: None)
    monkeypatch.setattr(_main._crd, "operator_namespace", lambda: "vgpu-driver-operator")
    monkeypatch.setattr(_main.client, "CoreV1Api", lambda: MagicMock())
    monkeypatch.setattr(_main.client, "BatchV1Api", lambda: batch_api)
    monkeypatch.setattr(_main.client, "CustomObjectsApi", lambda: MagicMock())
    monkeypatch.setattr(_main._crd, "list_owned_jobs", MagicMock(return_value=[]))
    monkeypatch.setattr(_main._registry, "list_tags", MagicMock(return_value=set()))
    monkeypatch.setattr(_main.kopf, "event", MagicMock())

    _main._do_reconcile(
        spec=_base_spec(),
        name="my-vdi",
        status={},
        patch=patch,  # type: ignore[arg-type]
        body={"metadata": {"uid": "uid-1", "generation": 3}},  # type: ignore[arg-type]
        logger=MagicMock(),
    )

    manifest = batch_api.create_namespaced_job.call_args.args[1]
    pod_spec = manifest["spec"]["template"]["spec"]
    assert "docker-config" not in [v["name"] for v in pod_spec["volumes"]]
    assert "docker-config" not in [
        m["name"] for m in pod_spec["containers"][0]["volumeMounts"]
    ]


def test_reconcile_replaces_stale_inflight_job(monkeypatch):
    patch = SimpleNamespace(status={})
    batch_api = MagicMock()
    stale_job = {
        "metadata": {
            "name": "vgpu-build-runtime-stale",
            "labels": {
                "vgpu.flatcar.io/driver-version": "550.54.15",
                "vgpu.flatcar.io/flatcar-version": "4593.2.0",
            },
        },
        "status": {"active": 1},
    }

    monkeypatch.setattr(_main, "_load_k8s_config", lambda: None)
    monkeypatch.setattr(_main._crd, "operator_namespace", lambda: "vgpu-driver-operator")
    monkeypatch.setattr(_main.client, "CoreV1Api", lambda: MagicMock())
    monkeypatch.setattr(_main.client, "BatchV1Api", lambda: batch_api)
    monkeypatch.setattr(_main.client, "CustomObjectsApi", lambda: MagicMock())
    monkeypatch.setattr(_main._crd, "list_owned_jobs", MagicMock(return_value=[stale_job]))
    monkeypatch.setattr(_main._registry, "list_tags", MagicMock(return_value=set()))
    monkeypatch.setattr(_main.kopf, "event", MagicMock())

    _main._do_reconcile(
        spec=_base_spec(),
        name="my-vdi",
        status={},
        patch=patch,  # type: ignore[arg-type]
        body={"metadata": {"uid": "uid-1", "generation": 3}},  # type: ignore[arg-type]
        logger=MagicMock(),
    )

    batch_api.delete_namespaced_job.assert_called_once_with(
        name="vgpu-build-runtime-stale",
        namespace="vgpu-driver-operator",
        propagation_policy="Background",
    )
    assert batch_api.create_namespaced_job.call_count == 1
    manifest = batch_api.create_namespaced_job.call_args.args[1]
    assert manifest["metadata"]["name"] != "vgpu-build-runtime-stale"


def test_reconcile_deletes_completed_job_when_registry_tag_missing(monkeypatch):
    patch = SimpleNamespace(status={})
    batch_api = MagicMock()
    completed_job = {
        "metadata": {
            "name": "vgpu-build-runtime-550-54-15-fc-4593-2-0",
            "labels": {
                "vgpu.flatcar.io/driver-version": "550.54.15",
                "vgpu.flatcar.io/flatcar-version": "4593.2.0",
            },
        },
        "status": {"succeeded": 1},
    }

    monkeypatch.setattr(_main, "_load_k8s_config", lambda: None)
    monkeypatch.setattr(_main._crd, "operator_namespace", lambda: "vgpu-driver-operator")
    monkeypatch.setattr(_main.client, "CoreV1Api", lambda: MagicMock())
    monkeypatch.setattr(_main.client, "BatchV1Api", lambda: batch_api)
    monkeypatch.setattr(_main.client, "CustomObjectsApi", lambda: MagicMock())
    monkeypatch.setattr(_main._crd, "list_owned_jobs", MagicMock(return_value=[completed_job]))
    monkeypatch.setattr(_main._registry, "list_tags", MagicMock(return_value=set()))
    monkeypatch.setattr(_main.kopf, "event", MagicMock())

    _main._do_reconcile(
        spec=_base_spec(),
        name="my-vdi",
        status={},
        patch=patch,  # type: ignore[arg-type]
        body={"metadata": {"uid": "uid-1", "generation": 3}},  # type: ignore[arg-type]
        logger=MagicMock(),
    )

    batch_api.delete_namespaced_job.assert_called_once_with(
        name="vgpu-build-runtime-550-54-15-fc-4593-2-0",
        namespace="vgpu-driver-operator",
        propagation_policy="Background",
    )


def test_reconcile_keeps_completed_job_when_registry_tag_exists(monkeypatch):
    patch = SimpleNamespace(status={})
    batch_api = MagicMock()
    completed_job = {
        "metadata": {
            "name": "vgpu-build-runtime-550-54-15-fc-4593-2-0",
            "labels": {
                "vgpu.flatcar.io/driver-version": "550.54.15",
                "vgpu.flatcar.io/flatcar-version": "4593.2.0",
            },
        },
        "status": {"succeeded": 1},
    }

    monkeypatch.setattr(_main, "_load_k8s_config", lambda: None)
    monkeypatch.setattr(_main._crd, "operator_namespace", lambda: "vgpu-driver-operator")
    monkeypatch.setattr(_main.client, "CoreV1Api", lambda: MagicMock())
    monkeypatch.setattr(_main.client, "BatchV1Api", lambda: batch_api)
    monkeypatch.setattr(_main.client, "CustomObjectsApi", lambda: MagicMock())
    monkeypatch.setattr(_main._crd, "list_owned_jobs", MagicMock(return_value=[completed_job]))
    monkeypatch.setattr(
        _main._registry,
        "list_tags",
        MagicMock(return_value={"550.54.15-flatcar4593.2.0"}),
    )
    monkeypatch.setattr(_main.kopf, "event", MagicMock())

    _main._do_reconcile(
        spec=_base_spec(),
        name="my-vdi",
        status={},
        patch=patch,  # type: ignore[arg-type]
        body={"metadata": {"uid": "uid-1", "generation": 3}},  # type: ignore[arg-type]
        logger=MagicMock(),
    )

    batch_api.delete_namespaced_job.assert_not_called()


def test_on_job_event_logs_terminal_transition(monkeypatch):
    logger = MagicMock()
    custom_api = MagicMock()
    crd_obj = {
        "metadata": {"name": "my-vdi", "uid": "owner-1"},
        "status": {
            "builds": [
                {
                    "driverVersion": "550.54.15",
                    "flatcarVersion": "4593.2.0",
                    "mode": "compiled",
                    "phase": "Building",
                }
            ]
        },
    }
    event = {
        "object": {
            "metadata": {
                "name": "vgpu-build-abc",
                "labels": {
                    "vgpu.flatcar.io/owner-uid": "owner-1",
                    "vgpu.flatcar.io/driver-version": "550.54.15",
                    "vgpu.flatcar.io/flatcar-version": "4593.2.0",
                    "vgpu.flatcar.io/mode": "compiled",
                },
            },
            "status": {"succeeded": 1},
        }
    }

    monkeypatch.setattr(_main, "_load_k8s_config", lambda: None)
    monkeypatch.setattr(_main.client, "CustomObjectsApi", lambda: custom_api)
    monkeypatch.setattr(_main._crd, "list_vgpu_driver_images", MagicMock(return_value=[crd_obj]))
    monkeypatch.setattr(_main._crd, "patch_status", MagicMock())

    _main.on_job_event(event, logger)

    logger.info.assert_any_call(
        "job %s transitioned to %s (driver=%s flatcar=%s)",
        "vgpu-build-abc",
        "Ready",
        "550.54.15",
        "4593.2.0",
    )


def test_reconcile_gc_registry_unreachable_sets_condition(monkeypatch):
    spec = _base_spec()
    spec["registry"] = {
        "repository": "registry.example.com/vgpu/drivers",
        "authSecretRef": {"name": "reg-auth"},
    }
    spec["retention"] = {"enabled": True}
    patch = SimpleNamespace(status={})

    monkeypatch.setattr(_main, "_load_k8s_config", lambda: None)
    monkeypatch.setattr(_main._crd, "operator_namespace", lambda: "vgpu-driver-operator")
    monkeypatch.setattr(_main.client, "CoreV1Api", lambda: MagicMock())
    monkeypatch.setattr(_main.client, "BatchV1Api", lambda: MagicMock())
    monkeypatch.setattr(_main.client, "CustomObjectsApi", lambda: MagicMock())
    monkeypatch.setattr(_main._crd, "get_secret", MagicMock(return_value={"config.json": b"{}"}))
    monkeypatch.setattr(
        _main._registry,
        "parse_dockerconfigjson",
        MagicMock(return_value={"username": "u", "password": "p"}),
    )
    monkeypatch.setattr(_main._registry, "list_tags", MagicMock(return_value=set()))
    monkeypatch.setattr(_main._crd, "list_owned_jobs", MagicMock(return_value=[]))
    monkeypatch.setattr(
        _main._gc,
        "run",
        MagicMock(return_value={"_registryUnreachable": "registry.example.com/vgpu/drivers"}),
    )
    event_mock = MagicMock()
    monkeypatch.setattr(_main.kopf, "event", event_mock)

    _main._do_reconcile(
        spec=spec,
        name="my-vdi",
        status={},
        patch=patch,  # type: ignore[arg-type]
        body={"metadata": {"uid": "uid-1", "generation": 3}},  # type: ignore[arg-type]
        logger=MagicMock(),
    )

    condition = patch.status["conditions"][0]
    assert condition["status"] == "False"
    assert condition["reason"] == "RegistryUnreachable"
    assert "garbage collection" in condition["message"]
    event_mock.assert_called_with(
        {"metadata": {"uid": "uid-1", "generation": 3}},
        type="Warning",
        reason="RegistryUnreachable",
        message=condition["message"],
    )


@pytest.fixture
def retention_reconcile(monkeypatch):
    monkeypatch.setattr(_main, "_load_k8s_config", lambda: None)
    monkeypatch.setattr(_main._crd, "operator_namespace", lambda: "operator")
    core = MagicMock()
    core.list_node.return_value.items = []
    monkeypatch.setattr(_main.client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(_main.client, "BatchV1Api", lambda: MagicMock())
    monkeypatch.setattr(_main._crd, "list_owned_jobs", MagicMock(return_value=[]))
    monkeypatch.setattr(_main.kopf, "event", MagicMock())
    monkeypatch.setattr(_main._registry, "list_tags",
                        MagicMock(return_value={"550.54.15-flatcar4593.2.0"}))
    from datetime import datetime, timezone
    from vgpu_driver_operator.registry import ManifestInfo, RepositoryInventory
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    d1, d2 = "sha256:" + "1"*64, "sha256:" + "2"*64
    inv = RepositoryInventory(
        {"550.54.15-flatcar4593.2.0": d1, "550.54.15-6.12.1-flatcar-flatcar4230.2.0": d2},
        {d1: ManifestInfo(d1, (), old), d2: ManifestInfo(d2, (), old)},
    )
    read = MagicMock(return_value=inv)
    delete = MagicMock()
    monkeypatch.setattr(_main._registry, "inventory_repository", read)
    monkeypatch.setattr(_main._registry, "delete_manifest", delete)

    def reconcile(spec=None, status=None):
        actual = spec if spec is not None else {**_base_spec(), "retention": {"enabled": True}}
        patch = SimpleNamespace(status={})
        _main._do_reconcile(spec=actual, name="driver", status=status or {}, patch=patch,
                            body={"metadata": {"uid": "1", "generation": 1}}, logger=MagicMock())
        return patch.status
    return reconcile, core, read, delete


def test_anonymous_reconciliation_runs_real_gc(retention_reconcile):
    reconcile, _, read, delete = retention_reconcile
    result = reconcile()
    delete.assert_called_once_with("registry.example.com/vgpu/drivers", "sha256:" + "2"*64, None)
    assert read.call_count == 2
    assert result["retention"]["result"] == "Succeeded"
    assert result["pruned"][0]["tag"].endswith("flatcar4230.2.0")
    assert result["retainedFlatcarVersions"] == ["4593.2.0"]
    assert result["conditions"][0]["reason"] == "AllBuildsReady"
    assert result["conditions"][1]["type"] == "RetentionHealthy"
    assert result["conditions"][1]["status"] == "True"


@pytest.mark.parametrize("secret", [None, {}, {"config.json": b"garbage"},
                                   {"config.json": b'{"auths":{}}'}])
def test_configured_auth_failure_never_falls_back(monkeypatch, retention_reconcile, secret):
    reconcile, _, read, delete = retention_reconcile
    getter = MagicMock(return_value=secret, side_effect=RuntimeError("missing") if secret is None
                       else None)
    monkeypatch.setattr(_main._crd, "get_secret", getter)
    spec = {**_base_spec(), "retention": {"enabled": True}}
    spec["registry"]["authSecretRef"] = {"name": "auth"}
    result = reconcile(spec)
    assert result["retention"]["reason"] == "RegistryAuthFailed"
    read.assert_not_called()
    delete.assert_not_called()
    _main._registry.list_tags.assert_not_called()


def test_authenticated_reconciliation(retention_reconcile, monkeypatch):
    reconcile, _, read, delete = retention_reconcile
    monkeypatch.setattr(_main._crd, "get_secret", MagicMock(return_value={"config.json":
        b'{"auths":{"registry.example.com":{"username":"u","password":"p"}}}'}))
    spec = {**_base_spec(), "retention": {"enabled": True}}
    spec["registry"]["authSecretRef"] = {"name": "auth"}
    assert reconcile(spec)["retention"]["result"] == "Succeeded"
    assert read.call_args.args[1] == {"username": "u", "password": "p"}
    assert delete.call_args.args[2] == {"username": "u", "password": "p"}


def test_node_discovery_failure_blocks_retention(retention_reconcile):
    reconcile, core, read, delete = retention_reconcile
    core.list_node.side_effect = RuntimeError("Kubernetes unavailable")
    spec = {**_base_spec(), "retention": {"enabled": True}}
    spec["flatcar"]["discoverFromNodes"] = True
    result = reconcile(spec)
    assert result["retention"]["reason"] == "NodeDiscoveryFailed"
    read.assert_not_called()
    delete.assert_not_called()


@pytest.mark.parametrize("disabled", [False, True])
def test_retention_early_exit_is_observable(retention_reconcile, disabled):
    reconcile, _, read, delete = retention_reconcile
    spec = {**_base_spec(), "retention": {"enabled": not disabled}}
    if not disabled:
        spec["flatcar"]["versions"] = []
    result = reconcile(spec, {"retention": {"lastAttemptTime": "previous"}})
    assert result["retention"]["reason"] == ("Disabled" if disabled else "NoFlatcarVersions")
    assert result["retention"]["lastAttemptTime"] == "previous"
    read.assert_not_called()
    delete.assert_not_called()



def test_auth_resolves_each_repository_host(retention_reconcile, monkeypatch):
    reconcile, _, read, delete = retention_reconcile
    monkeypatch.setattr(_main._crd, "get_secret", MagicMock(return_value={"config.json":
        b'{"auths":{"registry.example.com":{"username":"a","password":"one"},'
        b'"other.example.com":{"username":"b","password":"two"}}}'}))
    spec = {**_base_spec(), "precompile": True, "retention": {"enabled": True}}
    spec["registry"].update(authSecretRef={"name": "auth"},
                            repositoryPrecompiled="other.example.com/precompiled")
    result = reconcile(spec)
    assert result["retention"]["result"] == "Succeeded"
    assert read.call_count == 4
    read.assert_any_call("other.example.com/precompiled", {"username": "b", "password": "two"})
    read.assert_any_call("registry.example.com/vgpu/drivers", {"username": "a", "password": "one"})


def test_initial_anonymous_api_failure_reports_skipped_retention(retention_reconcile):
    reconcile, _, read, delete = retention_reconcile
    from vgpu_driver_operator.registry import RegistryError
    _main._registry.list_tags.side_effect = RegistryError("403")
    result = reconcile()
    assert result["retention"]["reason"] == "RegistryError"
    assert result["conditions"][1]["status"] == "False"
    read.assert_not_called()
    delete.assert_not_called()
