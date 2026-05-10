"""CRD helpers used by main.py and poller.py.

Provides typed wrappers around the Kubernetes client for VGPUDriverImage
custom resources, Secrets, Jobs, and operator-namespace detection.
"""

from __future__ import annotations

import base64
import os

from kubernetes import client  # type: ignore[import-untyped]

GROUP = "vgpu.flatcar.io"
VERSION = "v1alpha1"
PLURAL = "vgpudriverimages"

_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
_DEFAULT_NAMESPACE = "vgpu-driver-operator"


def operator_namespace() -> str:
    """Return the namespace the operator is deployed in.

    Priority order:
    1. Environment variable ``OPERATOR_NAMESPACE``.
    2. In-cluster service-account namespace file.
    3. Hard-coded default ``"vgpu-driver-operator"``.
    """
    ns = os.environ.get("OPERATOR_NAMESPACE")
    if ns:
        return ns
    try:
        with open(_NAMESPACE_FILE) as fh:
            ns = fh.read().strip()
            if ns:
                return ns
    except OSError:
        pass
    return _DEFAULT_NAMESPACE


def list_vgpu_driver_images(api: client.CustomObjectsApi) -> list[dict]:
    """Return all VGPUDriverImage objects across the cluster."""
    result = api.list_cluster_custom_object(GROUP, VERSION, PLURAL)
    return result.get("items", [])


def patch_status(
    api: client.CustomObjectsApi,
    name: str,
    status_patch: dict,
) -> None:
    """Merge-patch ``status`` on a cluster-scoped VGPUDriverImage object.

    Uses the ``/status`` sub-resource so the main spec is not touched.
    """
    body = {"status": status_patch}
    api.patch_cluster_custom_object_status(
        GROUP,
        VERSION,
        PLURAL,
        name,
        body,
    )


def get_secret(
    api: client.CoreV1Api,
    namespace: str,
    name: str,
) -> dict[str, bytes]:
    """Return base64-decoded data map for a Kubernetes Secret.

    Returns an empty dict if the secret has no ``data`` field.
    """
    secret = api.read_namespaced_secret(name, namespace)
    raw: dict[str, str] = secret.data or {}
    return {k: base64.b64decode(v) for k, v in raw.items()}


def list_owned_jobs(
    api: client.BatchV1Api,
    namespace: str,
    owner_uid: str,
) -> list[dict]:
    """Return all Jobs in *namespace* whose ownerReference UID matches *owner_uid*.

    Only checks the ``app=vgpu-driver-builder`` label set to limit the API
    server list size, then filters locally by owner UID.
    """
    result = api.list_namespaced_job(
        namespace,
        label_selector="app=vgpu-driver-builder",
    )
    jobs = []
    for item in result.items:
        owners = (item.metadata.owner_references or [])
        for ref in owners:
            if ref.uid == owner_uid:
                # sanitize_for_serialization yields camelCase API JSON;
                # to_dict() yields snake_case which silently breaks any
                # caller reading e.g. status.conditions[].lastTransitionTime.
                jobs.append(client.ApiClient().sanitize_for_serialization(item))
                break
    return jobs


def make_owner_reference(crd: dict) -> dict:
    """Build an ownerReference dict pointing at a VGPUDriverImage CRD object."""
    meta = crd.get("metadata", {})
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "VGPUDriverImage",
        "name": meta.get("name", ""),
        "uid": meta.get("uid", ""),
        "controller": True,
        "blockOwnerDeletion": True,
    }
